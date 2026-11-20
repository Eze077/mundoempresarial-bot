# -*- coding: utf-8 -*-
"""Pruebas de fase1_robustez (sin red, sin token). Correr: python -m pytest tests -q"""
import asyncio
import datetime as dt
import os
import sys
import types

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import fase1_robustez as f1  # noqa: E402
from telegram import error as tg_error  # noqa: E402


# ── Día de los informes ───────────────────────────────────────────────────────
def test_lunes_en_ptb_es_1_y_domingo_0():
    from telegram.ext import JobQueue
    assert f1.DIA_DOMINGO == 0 and f1.DIA_LUNES == 1
    assert JobQueue._CRON_MAPPING[f1.DIA_LUNES] == "mon"
    assert JobQueue._CRON_MAPPING[0] == "sun"      # el valor viejo (0) era domingo


def test_run_daily_con_dia_lunes_dispara_un_lunes():
    pytest.importorskip("apscheduler")
    from telegram.ext import JobQueue
    tz = dt.timezone(dt.timedelta(hours=-3))
    jq = JobQueue()

    async def cb(ctx):
        return None

    job = jq.run_daily(cb, time=dt.time(8, 0, tzinfo=tz), days=(f1.DIA_LUNES,), name="t")
    trig = job._job.trigger
    # domingo 15/11/2026 07:00 ARG → el próximo disparo es el lunes 16/11 08:00
    desde = dt.datetime(2026, 11, 15, 7, 0, tzinfo=tz)
    prox = trig.get_next_fire_time(None, desde)
    assert prox.astimezone(tz).weekday() == 0          # lunes en Python
    assert prox.astimezone(tz).strftime("%Y-%m-%d %H:%M") == "2026-11-16 08:00"


# ── Manejador de errores ──────────────────────────────────────────────────────
class _Bot:
    def __init__(self, falla=False):
        self.enviados = []
        self.falla = falla

    async def send_message(self, **kw):
        if self.falla:
            raise tg_error.NetworkError("Bad Gateway")
        self.enviados.append(kw)


def _ctx(exc, bot):
    return types.SimpleNamespace(error=exc, bot=bot, job=None)


def _upd(data=None, text=None):
    if data is not None:
        return types.SimpleNamespace(callback_query=types.SimpleNamespace(data=data), message=None,
                                     edited_message=None)
    return types.SimpleNamespace(callback_query=None, edited_message=None,
                                 message=types.SimpleNamespace(text=text, caption=None, photo=None,
                                                               voice=None, audio=None, video=None,
                                                               document=None))


def _correr(handler, upd, exc, bot):
    asyncio.run(handler(upd, _ctx(exc, bot)))


def _boom():
    raise ValueError("dato privado 5491100000000")


def _exc_codigo():
    try:
        _boom()
    except ValueError as e:
        return e


def test_error_de_red_no_avisa_y_no_relanza():
    bot = _Bot()
    h = f1.crear_manejador_errores(lambda: "123")
    for exc in (tg_error.NetworkError("Bad Gateway"), tg_error.TimedOut("t"), tg_error.RetryAfter(3),
                tg_error.Conflict("409"), ConnectionError("x")):
        _correr(h, _upd(data="edito_tema_12"), exc, bot)
    assert bot.enviados == []


def test_error_de_codigo_un_aviso_por_huella_y_hora():
    bot = _Bot()
    avisos = f1.AvisosLimitados(cada_s=3600)
    h = f1.crear_manejador_errores(lambda: "123", avisos=avisos)
    exc = _exc_codigo()
    _correr(h, _upd(data="edito_tema_12"), exc, bot)
    _correr(h, _upd(data="edito_tema_99"), _exc_codigo(), bot)     # misma huella → no repite
    assert len(bot.enviados) == 1
    texto = bot.enviados[0]["text"]
    assert "ValueError" in texto and "callback:edito_tema" in texto
    assert "5491100000000" not in texto and "dato privado" not in texto   # sin datos del mensaje
    # pasada la hora, vuelve a avisar
    assert avisos.debe_avisar(f1.huella_error(exc), ahora=__import__("time").time() + 7200) is True


def test_limitador_por_huella_y_ventana():
    a = f1.AvisosLimitados(cada_s=3600)
    assert a.debe_avisar("A", 0) is True
    assert a.debe_avisar("A", 3599) is False
    assert a.debe_avisar("B", 10) is True
    assert a.debe_avisar("A", 3600) is True


def test_bad_request_de_markdown_no_es_transitorio_pero_message_not_modified_es_benigno():
    assert f1.es_transitorio(tg_error.BadRequest("Can't parse entities")) is False
    assert f1.es_benigno(tg_error.BadRequest("Message is not modified: exactly the same")) is True
    assert f1.es_transitorio(tg_error.NetworkError("x")) is True


def test_el_manejador_no_relanza_ni_si_falla_el_aviso():
    bot = _Bot(falla=True)          # send_message lanza
    h = f1.crear_manejador_errores(lambda: "123")
    _correr(h, _upd(text="hola"), _exc_codigo(), bot)       # no debe lanzar
    _correr(h, None, _exc_codigo(), _Bot())                 # update None (error de job/polling)
    _correr(h, _upd(text="x"), None, _Bot())                # sin excepción


def test_sin_admin_no_manda_nada():
    bot = _Bot()
    h = f1.crear_manejador_errores(lambda: "")
    _correr(h, _upd(text="hola"), _exc_codigo(), bot)
    assert bot.enviados == []


def test_describir_update_no_incluye_contenido():
    assert f1.describir_update(_upd(text="/briefing algo secreto")) == "comando:briefing"
    assert f1.describir_update(_upd(text="mirá https://x.com/a?token=zz")) == "mensaje:link"
    assert f1.describir_update(_upd(data="edito_tema_12:abc")) == "callback:edito_tema"
    # punto 4: solo el prefijo [a-z_]+, sin pedazos de IP ni ids
    assert f1.describir_update(_upd(data="desbloq_10.20.30.40")) == "callback:desbloq"
    assert f1.describir_update(_upd(data="123")) == "callback:?"
    assert f1.describir_update(None) == "sin_update"


# ── Huella (punto 2) ──────────────────────────────────────────────────────────
def test_huella_usa_el_frame_del_bot_no_el_de_la_libreria():
    import json
    try:
        json.loads("{no es json")       # el frame más profundo está en la librería estándar
    except ValueError as e:
        h = f1.huella_error(e)
    assert h.startswith("JSONDecodeError@test_fase1_bot.py:")


def test_huella_badrequest_suma_texto_sin_numeros():
    h1 = f1.huella_error(tg_error.BadRequest("Can't parse entities: byte offset 123"))
    h2 = f1.huella_error(tg_error.BadRequest("Chat not found"))
    assert "Can't parse entities: byte offset" in h1 and "123" not in h1
    assert h1 != h2


def test_clasifica_como_red_requests_y_httpx():
    import httpx
    import requests
    for exc in (requests.Timeout("t"), requests.ConnectionError("c"), httpx.ReadTimeout("r"),
                httpx.ConnectError("c"), httpx.ReadError("r")):
        assert f1.es_transitorio(exc) is True, type(exc)
    assert f1.es_transitorio(ValueError("x")) is False
    assert f1.es_transitorio(requests.HTTPError("500")) is False


# ── Créditos con o sin sonido (punto 3) y mensajes editados (punto 5) ─────────
def _fuente():
    return open(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "main.py"),
                encoding="utf-8").read()


def test_creditos_solo_silencioso_sin_acciones():
    src = _fuente()
    assert ('disable_notification=("❌" not in report and "Acciones sugeridas" not in report)' in src)


def test_handler_de_texto_excluye_ediciones_y_no_hay_filtro_de_botones_viejos():
    src = _fuente()
    assert "filters.TEXT & ~filters.COMMAND & ~filters.UpdateType.EDITED, handle_link" in src
    assert "boton_es_viejo" not in src and "registrar_arranque" not in src
    # 20/11/2026: lo pendiente se descarta SOLO tras una caída larga (_soltar_pendientes); un reinicio corto lo procesa
    assert '"drop_pending_updates": bool(soltar)' in src and "drop_pending_updates=soltar," in src
    assert "drop_pending_updates=True" not in src and '"drop_pending_updates": True' not in src
