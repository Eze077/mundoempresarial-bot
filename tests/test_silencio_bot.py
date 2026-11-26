# -*- coding: utf-8 -*-
"""Silencio real en el bot (Jefe v3, etapa 3a; Leo, 26/11/2026: «silenciá el bot»). registro_tg_bot.RequestConRegistro con
python-telegram-bot 21.6 de verdad y sin red (httpx.MockTransport). Qué se prueba:
  · con_cambios (la API interna de PTB, aislada): arma un RequestData nuevo con los cambios, con y sin archivos, sin tocar el original
  · INVERSIÓN: con JEFE_SILENCIO en 0 el pedido que sale es byte a byte el de siempre
  · la ventana de 60 s: lo que responde a Leo va sin sonido; pasada la ventana, como hoy; una edición, como hoy
  · lo que siempre suena (core/entrega.siempre_sonido) va con sonido y con «🔴 IMPORTANTE»
  · un harness viejo (sin core/silencio.puerta_bot) o sin harness: como hoy
  · si Telegram rechaza con un 400 lo cambiado, sale el original
Las que usan el harness corren contra la copia de la rama jefe-silencio si está al lado (si no, se saltean). Tokens e ids de
prueba: el repo es público."""
import asyncio
import json
import os
import sys
import time
import types

import httpx
import pytest

RAIZ_BOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RAIZ_H = os.environ.get("HARNESS_DIR_PRUEBA_SILENCIO") or os.path.join(os.path.dirname(RAIZ_BOT), "jefe-silencio")
sys.path.insert(0, RAIZ_BOT)
import registro_tg_bot as R  # noqa: E402
from telegram import Bot, InputFile  # noqa: E402
from telegram.request import RequestData  # noqa: E402
from telegram.request._requestparameter import RequestParameter  # noqa: E402

TOKEN = "123456789:AAFakeTokenParaPruebas_abcdefghijklmnop"
ADMIN = 111
HAY_HARNESS = os.path.exists(os.path.join(RAIZ_H, "core", "silencio.py"))


# ── con_cambios: la API interna de PTB, aislada ───────────────────────────────────────────────────────────────────────

def _rd(**kw):
    return RequestData([RequestParameter.from_input(k, v) for k, v in kw.items()])


def test_con_cambios_arma_uno_nuevo_y_no_toca_el_original():
    rd = _rd(chat_id=ADMIN, text="hola", parse_mode="HTML", disable_notification=None)
    antes = (rd.parameters, rd.json_parameters)
    n = R.con_cambios(rd, {"disable_notification": True})
    assert n is not rd and (rd.parameters, rd.json_parameters) == antes
    assert n.parameters == dict(antes[0], disable_notification=True)
    assert n.json_parameters["disable_notification"] == "true" and json.loads(n.json_payload)["text"] == "hola"
    n = R.con_cambios(rd, {"text": "🔴 IMPORTANTE\n\nhola"})
    assert n.parameters["text"] == "🔴 IMPORTANTE\n\nhola" and n.parameters["chat_id"] == ADMIN
    assert R.con_cambios(rd, {}) is None and R.con_cambios(None, {"x": 1}) is None


def test_con_cambios_con_archivos_sigue_siendo_multipart():
    rd = _rd(chat_id=ADMIN, caption="foto", photo=InputFile(b"\x89PNG", filename="a.png"))
    assert rd.contains_files
    n = R.con_cambios(rd, {"caption": "🔴 IMPORTANTE\n\nfoto"})
    assert n.contains_files and n.parameters["caption"] == "🔴 IMPORTANTE\n\nfoto"
    assert set(n.multipart_data) == set(rd.multipart_data)


def test_con_cambios_si_la_api_interna_cambia_devuelve_none(monkeypatch):
    import telegram.request
    monkeypatch.setattr(telegram.request, "RequestData", lambda params: types.SimpleNamespace(parameters={}, contains_files=False))
    assert R.con_cambios(_rd(chat_id=ADMIN, text="x"), {"disable_notification": True}) is None


# ── con PTB y el harness ──────────────────────────────────────────────────────────────────────────────────────────────

@pytest.fixture
def harness(tmp_path, monkeypatch):
    if not HAY_HARNESS:
        pytest.skip("no está la copia del harness con core/silencio.py (rama jefe-silencio)")
    monkeypatch.setattr(R, "HARNESS_DIR", RAIZ_H)
    monkeypatch.syspath_prepend(RAIZ_H)
    for k in [k for k in sys.modules if k in ("core", "config", "agents", "tg_api", "tg_notify", "log_filtro")
              or k.startswith(("core.", "agents."))]:
        monkeypatch.delitem(sys.modules, k)          # otra prueba pudo cargar el core de otra copia del harness
    monkeypatch.setenv("HARNESS_DB", str(tmp_path / "h.db"))
    monkeypatch.setenv("GUARDIA", "1")
    monkeypatch.setenv("JEFE_SILENCIO", "1")
    monkeypatch.setenv("OPENAI_API_KEY", "test-no-real")
    tr = R._registro()
    assert tr is not None
    import config
    monkeypatch.setattr(config, "TELEGRAM_TOKEN", TOKEN)
    monkeypatch.setattr(config, "ADMIN_CHAT_ID", ADMIN)
    monkeypatch.setattr(tr, "HILO", False)
    tr._cola.clear()
    from core import silencio
    silencio.reiniciar()
    return tr, silencio


def _mandar(llamadas, respuestas=None):
    """Corre `llamadas` (async fn(bot)) con un Bot de PTB 21.6 sobre MockTransport; devuelve [(metodo, cuerpo JSON o form)]."""
    vistos, resp = [], list(respuestas or [])

    def tg(request):
        m = str(request.url.path).rsplit("/", 1)[-1]
        if m == "getMe":
            return httpx.Response(200, json={"ok": True, "result": {"id": 123456789, "is_bot": True, "first_name": "ME",
                                                                    "username": "me_bot"}})
        cuerpo = request.content.decode("utf-8", "replace")
        vistos.append((m, cuerpo))
        if resp and resp.pop(0) == 400:
            return httpx.Response(400, json={"ok": False, "error_code": 400, "description": "Bad Request: prueba"})
        return httpx.Response(200, json={"ok": True, "result": {"message_id": 5, "date": 0, "chat": {"id": ADMIN, "type": "private"},
                                                                "text": "x"}})

    async def correr():
        req = R.request()
        req._client = httpx.AsyncClient(transport=httpx.MockTransport(tg))
        bot = Bot(TOKEN, request=req, get_updates_request=R.HTTPXRequest())
        await bot.initialize()
        try:
            await llamadas(bot)
        finally:
            await bot.shutdown()
    asyncio.run(correr())
    return vistos


def _json(cuerpo):
    """Los parámetros del pedido (PTB los manda como formulario urlencoded): {nombre: valor}, con «true» → True."""
    from urllib.parse import parse_qsl
    d = dict(parse_qsl(cuerpo, keep_blank_values=True))
    return {k: (True if v == "true" else v) for k, v in d.items()}


async def _dos(bot):
    await bot.send_message(chat_id=ADMIN, text="✅ <b>Listo</b>: la nota entró a la cola", parse_mode="HTML")
    await bot.send_message(chat_id=ADMIN, text="🔴 el token de LinkedIn está vencido")
    await bot.edit_message_text(chat_id=ADMIN, message_id=3, text="✅ editado")


def test_inversion_con_la_bandera_en_0_sale_byte_a_byte_igual(harness, monkeypatch):
    tr, _s = harness
    monkeypatch.setattr(tr, "ultimo_leo", time.time() - 5)
    monkeypatch.setenv("JEFE_SILENCIO", "0")
    apagada = _mandar(_dos)
    monkeypatch.setattr(R, "_cambiado", lambda url, rd: rd)           # el bot de hoy, sin la puerta
    hoy = _mandar(_dos)
    assert apagada == hoy and len(hoy) == 3
    assert not any("disable_notification" in c or "IMPORTANTE" in c for _m, c in hoy)


def test_dentro_de_60s_lo_que_responde_a_leo_va_sin_sonido_y_lo_urgente_con_rotulo(harness, monkeypatch):
    tr, _s = harness
    monkeypatch.setattr(tr, "ultimo_leo", time.time() - 10)
    (m1, c1), (m2, c2), (m3, c3) = _mandar(_dos)
    j1, j2, j3 = _json(c1), _json(c2), _json(c3)
    assert j1["disable_notification"] is True and j1["text"] == "✅ <b>Listo</b>: la nota entró a la cola"
    assert "disable_notification" not in j2 and j2["text"] == "🔴 IMPORTANTE\n\n🔴 el token de LinkedIn está vencido"
    assert m3 == "editMessageText" and "disable_notification" not in j3 and j3["text"] == "✅ editado"


def test_fuera_de_la_ventana_como_hoy(harness, monkeypatch):
    tr, _s = harness
    monkeypatch.setattr(tr, "ultimo_leo", time.time() - 120)
    vistos = _mandar(_dos)
    assert not any("disable_notification" in c or "IMPORTANTE" in c for _m, c in vistos)
    monkeypatch.setattr(tr, "ultimo_leo", None)
    vistos = _mandar(_dos)
    assert not any("disable_notification" in c or "IMPORTANTE" in c for _m, c in vistos)


def test_una_foto_que_responde_a_leo_va_sin_sonido(harness, monkeypatch):
    tr, _s = harness
    monkeypatch.setattr(tr, "ultimo_leo", time.time() - 3)

    async def foto(bot):
        await bot.send_photo(chat_id=ADMIN, photo=InputFile(b"\x89PNG", filename="a.png"), caption="✅ placa lista")
    ((m, cuerpo),) = _mandar(foto)
    assert m == "sendPhoto" and 'name="disable_notification"' in cuerpo and "true" in cuerpo.split('name="disable_notification"')[1][:40]


def test_el_400_de_lo_cambiado_manda_el_original(harness, monkeypatch):
    tr, _s = harness
    monkeypatch.setattr(tr, "ultimo_leo", time.time() - 3)

    async def uno(bot):
        await bot.send_message(chat_id=ADMIN, text="✅ listo")
    vistos = _mandar(uno, respuestas=[400])
    assert len(vistos) == 2
    assert _json(vistos[0][1])["disable_notification"] is True and "disable_notification" not in _json(vistos[1][1])


def test_un_harness_viejo_o_ninguno_sale_como_hoy(harness, monkeypatch):
    tr, _s = harness
    monkeypatch.setattr(tr, "ultimo_leo", time.time() - 3)
    with monkeypatch.context() as m:
        m.setattr(R, "_cambiado", lambda url, rd: rd)                                  # el bot de hoy
        hoy = _mandar(_dos)
    import core
    viejo = types.ModuleType("core.silencio")                                         # un harness viejo: sin puerta_bot
    with monkeypatch.context() as m:
        m.setitem(sys.modules, "core.silencio", viejo)
        m.setattr(core, "silencio", viejo, raising=False)
        assert R._silencio() is None
        assert _mandar(_dos) == hoy
    with monkeypatch.context() as m:                                                   # sin harness
        m.setattr(R, "_registro", lambda: None)
        assert R._silencio() is None
        assert _mandar(_dos) == hoy
