# -*- coding: utf-8 -*-
"""Regresión de la segunda revisión (002a6b5): registro_tg_bot.py con python-telegram-bot 21.6 de verdad y sin red
(httpx.MockTransport). El request es el de PTB por defecto, lo que manda PTB llega igual y sin el token en la tabla, el grupo -2
no corta ni contesta, y bajar un archivo no se anota. Las que usan core/tg_registro corren contra la copia del harness de la
rama guardia si está al lado (si no, se saltean). Tokens e ids de prueba: el repo es público."""
import asyncio
import datetime as dt
import os
import sqlite3
import sys

import httpx
import pytest

RAIZ_BOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RAIZ_H = os.environ.get("HARNESS_DIR_PRUEBA_GUARDIA") or os.path.join(os.path.dirname(RAIZ_BOT), "guardia")
sys.path.insert(0, RAIZ_BOT)
import registro_tg_bot as R  # noqa: E402
from telegram import Bot, Chat, Message, Update, User  # noqa: E402
from telegram.ext import Application, ApplicationHandlerStop, MessageHandler, TypeHandler, filters  # noqa: E402

TOKEN = "123456789:AAFakeTokenParaPruebas_abcdefghijklmnop"
ADMIN, EXTRANO = 111, 999
HAY_HARNESS = os.path.exists(os.path.join(RAIZ_H, "core", "tg_registro.py"))


def test_el_request_es_igual_al_que_arma_ptb_por_defecto():
    base = Application.builder().token(TOKEN).build().bot._request[1]
    nuevo = R.request()
    for a, b in ((base._client.timeout, nuevo._client.timeout), (base._media_write_timeout, nuevo._media_write_timeout)):
        assert a == b
    pa, pb = base._client._transport._pool, nuevo._client._transport._pool
    assert (pa._max_connections, pa._max_keepalive_connections, pa._http2) == (pb._max_connections, pb._max_keepalive_connections,
                                                                            pb._http2)
    assert base._client._mounts.keys() == nuevo._client._mounts.keys()
    app = Application.builder().token(TOKEN).request(nuevo).build()
    gu = app.bot._request[0]                                          # getUpdates sigue con el suyo (pool 1), sin registro
    assert type(gu).__name__ == "HTTPXRequest" and gu._client._transport._pool._max_connections == 1


@pytest.fixture
def harness(tmp_path, monkeypatch):
    if not HAY_HARNESS:
        pytest.skip("no está la copia del harness con core/tg_registro.py")
    monkeypatch.setattr(R, "HARNESS_DIR", RAIZ_H)
    monkeypatch.syspath_prepend(RAIZ_H)
    for k in [k for k in sys.modules if k in ("core", "config") or k.startswith("core.")]:
        monkeypatch.delitem(sys.modules, k)          # otra prueba pudo cargar el core de otra copia del harness
    monkeypatch.setenv("HARNESS_DB", str(tmp_path / "h.db"))
    monkeypatch.setenv("GUARDIA", "1")
    tr = R._registro()
    assert tr is not None
    import config
    monkeypatch.setattr(config, "TELEGRAM_TOKEN", TOKEN)
    monkeypatch.setattr(config, "ADMIN_CHAT_ID", ADMIN)
    monkeypatch.setattr(tr, "HILO", False)
    tr._cola.clear()
    tr._tablas_ok.clear()
    return tr, str(tmp_path / "h.db")


def _bot(manejador):
    req = R.request()
    req._client = httpx.AsyncClient(transport=httpx.MockTransport(manejador))
    return Bot(TOKEN, request=req, get_updates_request=R.HTTPXRequest())


def test_lo_que_manda_ptb_llega_igual_y_en_la_tabla_no_queda_el_token(harness):
    tr, db = harness
    vistos = []

    def tg(request):
        vistos.append(request)
        if request.url.path.endswith("/getMe"):
            return httpx.Response(200, json={"ok": True, "result": {"id": 123456789, "is_bot": True, "first_name": "ME",
                                                                    "username": "me_bot"}})
        if "falla" in request.content.decode():
            raise httpx.ConnectError("no conecta a %s" % request.url)          # el peor caso: la URL con el token en el error
        return httpx.Response(200, json={"ok": True, "result": {"message_id": 5, "date": 0, "chat": {"id": ADMIN, "type": "private",
                                                                "first_name": "Leo", "username": "leo_x"}, "text": "hola"}})

    async def correr():
        bot = _bot(tg)
        await bot.initialize()
        m = await bot.send_message(chat_id=ADMIN, text="hola %s" % TOKEN)
        with pytest.raises(Exception) as e:
            await bot.send_message(chat_id=ADMIN, text="falla")
        await bot.shutdown()
        return m, e.value
    m, err = asyncio.run(correr())
    assert m.message_id == 5 and type(err).__name__ == "NetworkError"          # PTB devuelve y lanza lo mismo que siempre
    tr.volcar()
    filas = [dict(zip(("metodo", "chat", "texto", "ok", "error", "emisor"), r)) for r in sqlite3.connect(db).execute(
        "SELECT metodo, chat, texto, ok, error, emisor FROM tg_registro ORDER BY id")]
    assert [f["metodo"] for f in filas] == ["sendMessage", "sendMessage"] and filas[0]["chat"] == "leo"
    crudo = open(db, "rb").read()
    assert TOKEN.encode() not in crudo and TOKEN.split(":")[1].encode() not in crudo
    assert b"leo_x" not in crudo and b"Leo" not in crudo.replace(b"leo", b"")    # ni el username ni el nombre del chat


def test_grupo_menos2_no_corta_ni_contesta_y_lo_del_extrano_no_se_anota(harness):
    tr, db = harness
    pedidos = []

    def tg(request):
        m = str(request.url.path).rsplit("/", 1)[-1]
        if m == "getMe":
            return httpx.Response(200, json={"ok": True, "result": {"id": 123456789, "is_bot": True, "first_name": "ME",
                                                                    "username": "me_bot"}})
        pedidos.append(m)
        return httpx.Response(200, json={"ok": True, "result": True})
    corridos = []

    async def solo_admin(update, context):
        corridos.append("-1")
        if update.effective_user.id != ADMIN:
            raise ApplicationHandlerStop

    async def eco(update, context):
        corridos.append("0")

    async def correr():
        app = Application.builder().bot(_bot(tg)).updater(None).build()
        app.add_handler(TypeHandler(Update, solo_admin), group=-1)
        R.registrar_entrantes(app, lambda u: getattr(getattr(u, "effective_user", None), "id", None) == ADMIN)
        app.add_handler(MessageHandler(filters.ALL, eco))
        await app.initialize()
        for i, uid in enumerate((ADMIN, EXTRANO)):
            msg = Message(message_id=i, date=dt.datetime.now(dt.timezone.utc), chat=Chat(uid, "private"),
                          from_user=User(uid, "x", False), text="hola desde %d" % uid)
            await app.process_update(Update(update_id=i, message=msg))
        await app.shutdown()
    asyncio.run(correr())
    assert corridos == ["-1", "0", "-1"]                                     # -2 no corta; el -1 frena al extraño
    assert pedidos == []                                                     # el -2 no contesta nada
    tr.volcar()
    filas = list(sqlite3.connect(db).execute("SELECT direccion, texto FROM tg_registro"))
    assert filas == [("entrante", "hola desde 111")]


def test_bajar_un_archivo_pasa_por_el_request_y_no_se_anota(harness):
    tr, db = harness

    def tg(request):
        return httpx.Response(200, content=b"\x89PNG...")
    async def correr():
        bot = _bot(tg)
        return await bot._request[1].retrieve("https://api.telegram.org/file/bot%s/photos/file_1.jpg" % TOKEN)
    assert asyncio.run(correr()) == b"\x89PNG..."
    assert tr.pendientes() == 0
