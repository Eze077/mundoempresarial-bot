# -*- coding: utf-8 -*-
"""registro_tg_bot.py: lo que el bot de ME manda (subclase de HTTPXRequest) y lo que le escribe el operador (TypeHandler de solo
lectura en el grupo -2) van a tg_registro del harness (Guardia, 22/11/2026). Sin red ni Telegram: el request de PTB y el
harness son de mentira, salvo la última prueba (integración con la copia del harness de la rama guardia, si está al lado).
Los tokens son de prueba: el repo es público."""
import asyncio
import os
import subprocess
import sys
import textwrap
import types

import pytest
from telegram.request import HTTPXRequest

RAIZ_BOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, RAIZ_BOT)

import registro_tg_bot as R  # noqa: E402

TOKEN = "123456789:AAFakeTokenDePruebaNoEsReal_0123456789"
URL = "https://api.telegram.org/bot%s/sendMessage" % TOKEN


class _Datos:
    def __init__(self, params):
        self.parameters = params


@pytest.fixture
def registro(monkeypatch):
    vistos = types.SimpleNamespace(salientes=[], entrantes=[])
    falso = types.SimpleNamespace(
        anotar=lambda metodo, params, respuesta=None, error=None, token="": vistos.salientes.append(
            (metodo, params, respuesta.json() if respuesta is not None else None, type(error).__name__ if error else None,
             token)),
        anotar_entrante=lambda tipo, chat_id=None, texto="", prefijo="", comando="": vistos.entrantes.append(
            (tipo, chat_id, texto, prefijo, comando)))
    monkeypatch.setattr(R, "_registro", lambda: falso)
    return vistos


def run(coro):
    return asyncio.run(coro)


def test_lo_que_manda_el_bot_se_anota_y_el_pedido_es_el_mismo(registro, monkeypatch):
    llamados = []

    async def padre(self, url, method, request_data=None, **kw):
        llamados.append((url, method, kw))
        return 200, b'{"ok": true, "result": {"message_id": 5}}'
    monkeypatch.setattr(HTTPXRequest, "do_request", padre)
    req = R.request()
    assert isinstance(req, R.RequestConRegistro)
    res = run(req.do_request(URL, "POST", request_data=_Datos({"chat_id": 1, "text": "hola"}), read_timeout=5))
    assert res == (200, b'{"ok": true, "result": {"message_id": 5}}')                  # lo mismo que habría devuelto PTB
    assert llamados == [(URL, "POST", {"read_timeout": 5})]
    assert registro.salientes == [("sendMessage", {"chat_id": 1, "text": "hola"}, {"ok": True, "result": {"message_id": 5}},
                                   None, TOKEN)]


def test_si_telegram_falla_se_anota_y_la_excepcion_sale_igual(registro, monkeypatch):
    async def padre(self, url, method, request_data=None, **kw):
        raise TimeoutError("timed out")
    monkeypatch.setattr(HTTPXRequest, "do_request", padre)
    with pytest.raises(TimeoutError):
        run(R.request().do_request(URL, "POST", request_data=_Datos({"chat_id": 1})))
    assert registro.salientes[0][3] == "TimeoutError"


def test_si_el_registro_falla_el_bot_ni_se_entera(monkeypatch):
    async def padre(self, url, method, request_data=None, **kw):
        return 200, b"no es json"
    monkeypatch.setattr(HTTPXRequest, "do_request", padre)
    monkeypatch.setattr(R, "_registro", lambda: types.SimpleNamespace(anotar=lambda *a, **k: 1 / 0))
    assert run(R.request().do_request(URL, "POST")) == (200, b"no es json")
    monkeypatch.setattr(R, "_registro", lambda: None)                              # sin harness: nada, y anda igual
    assert run(R.request().do_request(URL, "POST")) == (200, b"no es json")


def _update(callback=None, texto=None, foto=False, usuario=1):
    chat = types.SimpleNamespace(id=-100)                                          # un id de mentira
    if callback is not None:
        return types.SimpleNamespace(callback_query=types.SimpleNamespace(data=callback, message=types.SimpleNamespace(chat=chat)),
                                     message=None, effective_user=types.SimpleNamespace(id=usuario))
    msg = types.SimpleNamespace(chat=chat, text=texto, caption=None, photo=[1] if foto else None, voice=None, audio=None,
                                document=None, video=None)
    return types.SimpleNamespace(callback_query=None, message=msg, edited_message=None,
                                 effective_user=types.SimpleNamespace(id=usuario))


def test_lo_que_escribe_y_toca_el_operador_se_anota_y_lo_de_un_extraño_no(registro):
    h = R.manejador_entrante(lambda u: u.effective_user.id == 1)
    assert run(h(_update(callback="h_cur_frenar:22140"), None)) is None             # no corta la cadena ni contesta
    run(h(_update(texto="/estado ahora"), None))
    run(h(_update(texto="esto está mal"), None))
    run(h(_update(texto=None, foto=True), None))
    run(h(_update(texto="soy un extraño", usuario=2), None))
    assert registro.entrantes == [("callback", -100, "", "h_cur_frenar:22140", ""), ("comando", -100, "/estado ahora", "", "estado"),
                                  ("mensaje", -100, "esto está mal", "", ""), ("foto", -100, "", "", "")]


def test_el_manejador_nunca_lanza(monkeypatch):
    monkeypatch.setattr(R, "_registro", lambda: (_ for _ in ()).throw(RuntimeError("x")))
    h = R.manejador_entrante(lambda u: 1 / 0)
    assert run(h(_update(texto="hola"), None)) is None


def test_main_py_usa_el_request_y_registra_lo_entrante_antes_del_control_de_acceso():
    src = open(os.path.join(RAIZ_BOT, "main.py"), encoding="utf-8").read()
    assert "import registro_tg_bot as _rtg" in src and "_constructor = _constructor.request(_req_registro)" in src
    i = src.index("app.add_handler(TypeHandler(Update, _solo_admin), group=-1)")
    assert "_rtg.registrar_entrantes(app," in src[i:i + 400]
    src_r = open(os.path.join(RAIZ_BOT, "registro_tg_bot.py"), encoding="utf-8").read()
    assert "group=-2" in src_r and "getUpdates" in src_r                              # documentado: getUpdates no pasa por acá


# ═══ integración con el harness de la rama (copia al lado) ═══════════════════════════════════════════════════════════

HARNESS = os.environ.get("HARNESS_DIR_PRUEBA_GUARDIA") or os.path.join(os.path.dirname(RAIZ_BOT), "guardia")


@pytest.mark.skipif(not os.path.exists(os.path.join(HARNESS, "core", "tg_registro.py")),
                    reason="no está la copia del harness con core/tg_registro.py")
def test_integracion_con_el_harness_real(tmp_path):
    otro = "987654321:AAOtroBotDePruebaNoEsReal_9876543210"
    script = textwrap.dedent("""
        import asyncio, json, os, sqlite3, sys, types
        sys.path.insert(0, %(bot)r)
        import registro_tg_bot as R
        R.HARNESS_DIR = %(harness)r
        from telegram.request import HTTPXRequest
        async def padre(self, url, method, request_data=None, **kw):
            return 200, b'{"ok": true, "result": {"message_id": 9}}'
        HTTPXRequest.do_request = padre
        req = R.request()
        class D:
            def __init__(s, p): s.parameters = p
        # un handler «del bot» (archivo fuera del harness, como main.py) que manda por PTB
        ns = {"__name__": "__main__", "req": req, "D": D}
        exec(compile("async def handle_button():\\n    await req.do_request(%(url_me)r, 'POST', request_data=D("
                     "{'chat_id': 1, 'text': '<b>Listo</b> llamá al 11 5555 1234', 'reply_markup': "
                     "{'inline_keyboard': [[{'text': 'x', 'callback_data': 'h_cur_ok:7'}]]}}))\\n"
                     "    await req.do_request(%(url_otro)r, 'POST', request_data=D({'chat_id': 1, 'text': 'otro bot'}))\\n",
                     os.path.join(os.sep, "opt", "mundoempresarial-bot", "main.py"), "exec"), ns)
        asyncio.run(ns["handle_button"]())
        h = R.manejador_entrante(lambda u: True)
        cb = types.SimpleNamespace(callback_query=types.SimpleNamespace(data="h_cur_frenar:22140",
                                   message=types.SimpleNamespace(chat=types.SimpleNamespace(id=1))), message=None)
        asyncio.run(h(cb, None))
        from core import tg_registro
        tg_registro.volcar()
        c = sqlite3.connect(os.environ["HARNESS_DB"]); c.row_factory = sqlite3.Row
        filas = [dict(r) for r in c.execute("SELECT direccion, metodo, emisor, funcion, botones, texto, ok, message_id "
                                            "FROM tg_registro ORDER BY id")]
        print(json.dumps(filas))
    """) % {"bot": RAIZ_BOT, "harness": HARNESS, "url_me": URL,
            "url_otro": "https://api.telegram.org/bot%s/sendMessage" % otro}
    env = dict(os.environ, HARNESS_DB=str(tmp_path / "h.db"), GUARDIA="1", TELEGRAM_TOKEN=TOKEN, OPENAI_API_KEY="x",
               PYTHONIOENCODING="utf-8")
    p = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True, env=env, cwd=HARNESS, timeout=120,
                       encoding="utf-8")
    assert p.returncode == 0, p.stderr[-2000:]
    import json
    filas = json.loads(p.stdout.strip().splitlines()[-1])
    assert filas == [
        {"direccion": "saliente", "metodo": "sendMessage", "emisor": "bot:main", "funcion": "handle_button",
         "botones": "h_cur_ok", "texto": "«Listo» llamá al <tel>", "ok": 1, "message_id": 9},
        {"direccion": "entrante", "metodo": "callback", "emisor": "leo", "funcion": "h_cur_frenar", "botones": "h_cur_frenar",
         "texto": "", "ok": 1, "message_id": None}]                               # el otro bot no se anotó; el token, nunca
    assert TOKEN.split(":")[1] not in p.stdout and otro.split(":")[1] not in p.stdout


def test_los_posts_directos_al_api_tambien_se_anotan(registro):
    class _Resp:
        status_code = 200

        def json(self):
            return {"ok": True, "result": {"message_id": 8}}
    R.anotar_post(URL, {"chat_id": 1, "text": "🚨 alerta"}, respuesta=_Resp())
    R.anotar_post(URL.replace("sendMessage", "deleteMessage"), {"chat_id": 1, "message_id": 3}, error=TimeoutError("x"))
    assert registro.salientes == [("sendMessage", {"chat_id": 1, "text": "🚨 alerta"}, {"ok": True, "result": {"message_id": 8}},
                                   None, TOKEN), ("deleteMessage", {"chat_id": 1, "message_id": 3}, None, "TimeoutError", TOKEN)]
    src = open(os.path.join(RAIZ_BOT, "main.py"), encoding="utf-8").read()
    i = src.index("def alert_admin_sync(")
    assert "_rtg.anotar_post(_url, _payload, respuesta=_r)" in src[i:i + 1200]
    assert "_anotar_post(url, payload, respuesta=r)" in open(os.path.join(RAIZ_BOT, "borrado_redes.py"), encoding="utf-8").read()
