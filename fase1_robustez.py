# -*- coding: utf-8 -*-
"""Arreglos de robustez del bot (fase 1). Lógica pura, sin token ni red, para poder probarla.

- Manejador global de errores (no re-lanza; red transitoria en silencio; error de código: UN aviso
  por huella y por hora).
- Día de la semana de los jobs: en python-telegram-bot >= 20, run_daily(days=...) cuenta 0 = DOMINGO.
"""
import asyncio
import logging
import os
import re
import time as _time

from telegram import error as tg_error

# python-telegram-bot 21.6, JobQueue.run_daily: «0-6 correspond to sunday - saturday»
DIA_DOMINGO = 0
DIA_LUNES = 1

AVISO_ERROR_CADA_S = 3600    # un aviso por huella y por hora


# ── Descripción de un update SIN datos personales ─────────────────────────────
def describir_update(update):
    """«callback:edito_tema», «comando:briefing», «mensaje:texto»... Nunca el contenido."""
    if update is None:
        return "sin_update"
    try:
        q = getattr(update, "callback_query", None)
        if q is not None:
            m_ = re.match(r"[a-z_]+", q.data or "")   # solo el prefijo: lo demás puede traer ids/IPs
            return "callback:" + ((m_.group(0).rstrip("_")[:40]) if m_ else "?")
        m = getattr(update, "message", None) or getattr(update, "edited_message", None)
        if m is not None:
            txt = (getattr(m, "text", None) or getattr(m, "caption", None) or "")
            if txt.startswith("/"):
                return "comando:" + txt.split()[0][1:].split("@")[0][:40]
            if getattr(m, "photo", None):
                return "mensaje:foto"
            if getattr(m, "voice", None) or getattr(m, "audio", None) or getattr(m, "video", None):
                return "mensaje:audio_video"
            if getattr(m, "document", None):
                return "mensaje:documento"
            return "mensaje:link" if re.search(r"https?://", txt) else "mensaje:texto"
    except Exception:
        pass
    return "update:" + type(update).__name__


# ── Clasificación y huella de errores ─────────────────────────────────────────
_BENIGNOS = ("message is not modified", "query is too old", "query id is invalid")


def es_benigno(exc):
    """Errores de Telegram sin consecuencia (botón repetido, query vencida)."""
    if isinstance(exc, tg_error.BadRequest):
        msg = str(exc).lower()
        return any(b in msg for b in _BENIGNOS)
    return False


def _es_red_http(exc):
    """requests.Timeout/ConnectionError y los de lectura o conexión de httpx/httpcore."""
    try:
        import requests
        if isinstance(exc, (requests.Timeout, requests.ConnectionError)):
            return True
    except ImportError:
        pass
    for mod, nombres in (("httpx", ("TimeoutException", "ConnectError", "ReadError", "NetworkError")),
                         ("httpcore", ("TimeoutException", "ConnectError", "ReadError", "NetworkError"))):
        try:
            m = __import__(mod)
            clases = tuple(getattr(m, n) for n in nombres if hasattr(m, n))
            if clases and isinstance(exc, clases):
                return True
        except ImportError:
            pass
    return False


def es_transitorio(exc):
    """Red / Telegram caído / flood / otra instancia: se reintenta solo, no hay que avisar.

    OJO: en python-telegram-bot BadRequest hereda de NetworkError, pero es un error nuestro
    (p. ej. Markdown mal armado), no de red: NO es transitorio.
    """
    if isinstance(exc, tg_error.BadRequest):
        return False
    if isinstance(exc, (tg_error.NetworkError, tg_error.RetryAfter, tg_error.Conflict)):
        return True
    if isinstance(exc, (asyncio.TimeoutError, TimeoutError, ConnectionError)):
        return True
    return _es_red_http(exc)


_DIR_BOT = os.path.dirname(os.path.abspath(__file__))


def _es_del_bot(ruta):
    ruta = os.path.abspath(ruta)
    return ruta.startswith(_DIR_BOT + os.sep) and "site-packages" not in ruta


def huella_error(exc):
    """Tipo + archivo:línea del frame más profundo que sea del bot (no de la librería).

    Para BadRequest suma ~40 caracteres del texto de Telegram sin números (distingue «can't parse
    entities» de «chat not found»). Sin otro mensaje: puede traer datos.
    """
    tb = getattr(exc, "__traceback__", None)
    lugar = ultimo = None
    while tb is not None:
        c = tb.tb_frame.f_code.co_filename
        ultimo = f"{os.path.basename(c)}:{tb.tb_lineno}"
        if _es_del_bot(c):
            lugar = ultimo
        tb = tb.tb_next
    h = f"{type(exc).__name__}@{lugar or ultimo or '?'}"
    if isinstance(exc, tg_error.BadRequest):
        h += "|" + re.sub(r"\d+", "", str(exc))[:40].strip()
    return h


class AvisosLimitados:
    """Deja pasar un aviso por huella cada `cada_s` segundos."""

    def __init__(self, cada_s=AVISO_ERROR_CADA_S, maximo=200):
        self.cada_s = cada_s
        self.maximo = maximo
        self._ultimo = {}

    def debe_avisar(self, huella, ahora=None):
        ahora = _time.time() if ahora is None else ahora
        previo = self._ultimo.get(huella)
        if previo is not None and (ahora - previo) < self.cada_s:
            return False
        if len(self._ultimo) >= self.maximo:
            vencidas = [k for k, t in self._ultimo.items() if (ahora - t) >= self.cada_s]
            for k in vencidas:
                self._ultimo.pop(k, None)
            if len(self._ultimo) >= self.maximo:
                self._ultimo.clear()
        self._ultimo[huella] = ahora
        return True


def crear_manejador_errores(admin_id_getter, logger=None, avisos=None):
    """Devuelve la corrutina para app.add_error_handler. Nunca lanza."""
    log = logger or logging.getLogger("fase1_robustez")
    limitador = avisos if avisos is not None else AvisosLimitados()

    async def _on_error(update, context):
        try:
            exc = getattr(context, "error", None)
            donde = describir_update(update)
            job = getattr(context, "job", None)
            if job is not None and update is None:
                donde = "job:" + str(getattr(job, "name", "?"))[:40]
            if exc is None:
                log.error("Error sin excepción en %s", donde)
                return
            if es_benigno(exc):
                log.info("Error benigno de Telegram en %s: %s", donde, type(exc).__name__)
                return
            if es_transitorio(exc):
                log.warning("Error transitorio en %s: %s (sin aviso)", donde, type(exc).__name__)
                return
            huella = huella_error(exc)
            log.error("Error no controlado en %s [%s]", donde, huella, exc_info=exc)
            if limitador.debe_avisar(huella):
                admin = admin_id_getter()
                if admin and str(admin).lstrip("-").isdigit():
                    texto = ("⚠️ Error de código en el bot\n"
                             f"Qué: {type(exc).__name__}\n"
                             f"Dónde: {huella.split('@', 1)[-1]}\n"
                             f"Al procesar: {donde}\n"
                             "Detalle en el journal. Un solo aviso por error y por hora.")
                    await context.bot.send_message(chat_id=int(admin), text=texto)
        except Exception as e:  # el manejador de errores jamás puede tirar el proceso
            try:
                log.error("Falló el manejador de errores: %s", type(e).__name__)
            except Exception:
                pass

    return _on_error
