# -*- coding: utf-8 -*-
"""registro_tg_bot.py — lo que el bot de Mundo Empresarial manda y recibe, anotado para la Guardia del harness. v1.0 · 22/11/2026

Leo, 22/11/2026: «que registre todo lo de mundoempresarial en telegram, ningun otro bot». El harness ya anota lo que sale por
su puerta (tg_api); esto suma lo que el bot hace por su cuenta, con el MISMO módulo del harness (core/tg_registro.py, tabla
`tg_registro` de harness.db):

  · Saliente: `RequestConRegistro`, una subclase de telegram.request.HTTPXRequest (PTB 21.6) que se le pasa a la Application
    con `.request(...)`. Es el único punto de red de PTB para lo que manda el bot (reply_text, send_message, edit_…, los
    botones): después de cada pedido llama a tg_registro.anotar con el método, los parámetros, lo que contestó Telegram y el
    token de la URL (que tg_registro solo compara con el del bot de ME y tapa al limpiar: nunca se guarda). getUpdates va por
    otro request (el de la Application): no pasa por acá y tampoco lo anotaría (solo send*, edit*, delete*…).
  · Entrante: `manejador_entrante(es_operador)`, un TypeHandler de solo lectura para el grupo -2 (antes del control de acceso,
    que va en el -1): anota lo que el OPERADOR le escribe al bot y los botones que toca (del botón, solo el prefijo del
    callback_data). No corta la cadena de handlers ni contesta nada. Lo de un extraño no se anota.

Mismas garantías que el gancho del harness: solo encola (tg_registro no toca la base ni limpia en el momento), nunca frena ni
tira al bot (todo va en try/except y devuelve lo mismo que habría devuelto PTB), y con la bandera GUARDIA apagada no hace
nada. Si el harness no está o no tiene core/tg_registro.py, el bot sigue igual: no se anota nada.
Sin claves ni datos personales: el repo del bot es público.
"""
import json
import logging
import os
import re
import sys

from telegram.request import HTTPXRequest

logger = logging.getLogger("registro_tg_bot")

VERSION = "v1.0"
FECHA_VERSION = "22/11/2026"
HARNESS_DIR = os.environ.get("HARNESS_DIR", "/opt/me-harness")
_RE_TOKEN_URL = re.compile(r"/bot([^/]+)/")


def _registro():
    """core.tg_registro del harness, o None si no se puede cargar (entonces no se anota nada)."""
    try:
        if HARNESS_DIR not in sys.path:
            sys.path.insert(0, HARNESS_DIR)
        from core import tg_registro
        return tg_registro
    except Exception:
        return None


class _Respuesta:
    """Lo que tg_registro.anotar lee de una respuesta: el código y el JSON que contestó Telegram."""
    __slots__ = ("status_code", "_cuerpo")

    def __init__(self, status_code, cuerpo):
        self.status_code, self._cuerpo = status_code, cuerpo

    def json(self):
        return json.loads(self._cuerpo)


def _anotar(url, request_data, respuesta=None, error=None):
    """Nunca lanza. El token sale de la URL y solo viaja a tg_registro (que lo compara y lo tapa)."""
    try:
        r = _registro()
        if r is None:
            return
        u = str(url or "")
        metodo = u.rstrip("/").rsplit("/", 1)[-1]
        m = _RE_TOKEN_URL.search(u)
        try:
            params = dict(request_data.parameters) if request_data is not None else {}
        except Exception:
            params = {}
        resp = _Respuesta(*respuesta) if respuesta is not None else None
        r.anotar(metodo, params, respuesta=resp, error=error, token=m.group(1) if m else "")
    except Exception:
        pass


class RequestConRegistro(HTTPXRequest):
    """HTTPXRequest que, además, anota en tg_registro lo que manda el bot (ver arriba). El pedido es el mismo."""

    async def do_request(self, url, method, request_data=None, **kw):
        try:
            res = await super().do_request(url, method, request_data=request_data, **kw)
        except Exception as e:
            _anotar(url, request_data, error=e)
            raise
        _anotar(url, request_data, respuesta=res)
        return res


def request():
    """El request de la Application con los mismos valores por defecto que arma PTB 21.6 (256 conexiones, HTTP/1.1), o None
    si no se pudo armar (entonces la Application usa el suyo de siempre)."""
    try:
        return RequestConRegistro(connection_pool_size=256, http_version="1.1")
    except Exception as e:
        logger.warning("registro de Telegram: no armé el request (%s); el bot sigue con el de siempre", type(e).__name__)
        return None


def _tipo(update):
    """(tipo, chat_id, texto, prefijo, comando) de lo que llegó; None si no es algo del operador para anotar."""
    q = getattr(update, "callback_query", None)
    if q is not None:
        msg = getattr(q, "message", None)
        return "callback", getattr(getattr(msg, "chat", None), "id", None), "", str(q.data or ""), ""
    msg = getattr(update, "message", None) or getattr(update, "edited_message", None)
    if msg is None:
        return None
    chat = getattr(getattr(msg, "chat", None), "id", None)
    texto = getattr(msg, "text", None) or getattr(msg, "caption", None) or ""
    if texto.startswith("/"):
        comando = texto[1:].split()[0].split("@")[0] if texto[1:].strip() else ""
        return "comando", chat, texto, "", comando
    for attr, tipo in (("photo", "foto"), ("voice", "audio"), ("audio", "audio"), ("document", "documento"),
                       ("video", "video")):
        if getattr(msg, attr, None):
            return tipo, chat, texto, "", ""
    return ("mensaje" if texto else "otro"), chat, texto, "", ""


def manejador_entrante(es_operador):
    """El callback del TypeHandler de solo lectura (grupo -2). `es_operador(update) -> bool`: solo se anota lo del operador.
    No contesta, no corta la cadena (devuelve None) y nunca lanza."""
    async def _entrante(update, context):
        try:
            if not es_operador(update):
                return None
            t = _tipo(update)
            if t is None:
                return None
            r = _registro()
            if r is None:
                return None
            tipo, chat, texto, prefijo, comando = t
            r.anotar_entrante(tipo, chat_id=chat, texto=texto, prefijo=prefijo, comando=comando)
        except Exception:
            pass
        return None
    return _entrante


def registrar_entrantes(app, es_operador):
    """Suma el TypeHandler de solo lectura en el grupo -2 (antes del control de acceso del grupo -1)."""
    from telegram import Update
    from telegram.ext import TypeHandler
    app.add_handler(TypeHandler(Update, manejador_entrante(es_operador)), group=-2)
