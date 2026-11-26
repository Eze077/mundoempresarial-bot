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
  · Los requests.post directos al API que quedan en el bot (alert_admin_sync de main.py, el borrado del canal en
    borrado_redes.py): `anotar_post(url, payload, respuesta|error)`, con el mismo filtro (solo el token del bot de ME).
  · Entrante: `manejador_entrante(es_operador)`, un TypeHandler de solo lectura para el grupo -2 (antes del control de acceso,
    que va en el -1): anota lo que el OPERADOR le escribe al bot y los botones que toca (del botón, solo el prefijo del
    callback_data). No corta la cadena de handlers ni contesta nada. Lo de un extraño no se anota.

Mismas garantías que el gancho del harness: solo encola (tg_registro no toca la base ni limpia en el momento), nunca frena ni
tira al bot (todo va en try/except y devuelve lo mismo que habría devuelto PTB), y con la bandera GUARDIA apagada no hace
nada. Si el harness no está o no tiene core/tg_registro.py, el bot sigue igual: no se anota nada.
Sin claves ni datos personales: el repo del bot es público.

Silencio real (Jefe v3, etapa 3a, v1.1 · 26/11/2026; Leo: «silenciá el bot»): con JEFE_SILENCIO (y GUARDIA) prendidas en el env del
bot, `RequestConRegistro.do_request` le pregunta al harness (core/silencio.puerta_bot) por cada envío NUEVO al chat de Leo: lo
que sale hasta 60 s después de un botón, un mensaje o un comando de Leo va sin sonido (disable_notification), salvo lo que el
criterio del Jefe dice que siempre suena (core/entrega.siempre_sonido), que va con sonido y con el rótulo «🔴 IMPORTANTE» como
primera línea. Fuera de esa ventana, como hoy. El cambio del pedido usa la API interna de PTB 21.6 (RequestData/RequestParameter)
y vive aislado en `con_cambios`, que se verifica a sí mismo: si algo no da, sale el pedido original. Si Telegram rechaza con un
400 el pedido cambiado, sale el original. Con la bandera en 0, o un harness viejo sin core/silencio.puerta_bot, byte a byte
como antes.
"""
import json
import logging
import os
import re
import sys

from telegram.request import HTTPXRequest

logger = logging.getLogger("registro_tg_bot")

VERSION = "v1.1"
FECHA_VERSION = "26/11/2026"
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


def _silencio():
    """core.silencio del harness si tiene la puerta del bot (etapa 3a), o None (un harness viejo: el bot sale como siempre)."""
    try:
        if _registro() is None:
            return None
        from core import silencio
        return silencio if callable(getattr(silencio, "puerta_bot", None)) else None
    except Exception:
        return None


def con_cambios(request_data, cambios):
    """Un RequestData NUEVO con los parámetros de `request_data` y `cambios` ({nombre: valor}) encima, o None si no se pudo armar
    igual (entonces sale el original). Es la única parte que usa la API interna de python-telegram-bot 21.6 (RequestData(...) y
    RequestParameter.from_input): por eso se verifica a sí mismo antes de devolver. Nunca toca el original. Nunca lanza."""
    try:
        if request_data is None or not cambios:
            return None
        from telegram.request import RequestData
        from telegram.request._requestparameter import RequestParameter
        viejos = list(request_data._parameters)
        nuevos = [x for x in viejos if x.name not in cambios] + [RequestParameter.from_input(k, v) for k, v in cambios.items()]
        nueva = RequestData(nuevos)
        esperado = dict(request_data.parameters)
        esperado.update(cambios)
        if nueva.parameters != esperado or nueva.contains_files != request_data.contains_files:
            return None
        return nueva
    except Exception:
        return None


def _cambiado(url, request_data):
    """El pedido con lo que pide la puerta del silencio (sin sonido o con el rótulo), o el MISMO objeto si sale como hoy."""
    try:
        if request_data is None:
            return request_data
        s = _silencio()
        if s is None:
            return request_data
        u = str(url or "")
        m = _RE_TOKEN_URL.search(u)
        cambios = s.puerta_bot(u.rstrip("/").rsplit("/", 1)[-1], m.group(1) if m else "", dict(request_data.parameters))
        if not cambios:
            return request_data
        return con_cambios(request_data, cambios) or request_data
    except Exception:
        return request_data


class RequestConRegistro(HTTPXRequest):
    """HTTPXRequest que, además, anota en tg_registro lo que manda el bot (ver arriba) y, con JEFE_SILENCIO, silencia lo que
    responde a Leo (ver arriba). Con la bandera en 0, el pedido es el mismo."""

    async def do_request(self, url, method, request_data=None, **kw):
        enviar = _cambiado(url, request_data)
        try:
            res = await super().do_request(url, method, request_data=enviar, **kw)
            if enviar is not request_data and res and res[0] == 400:
                enviar = request_data                      # Telegram no aceptó lo cambiado: sale el original, tal cual
                res = await super().do_request(url, method, request_data=enviar, **kw)
        except Exception as e:
            _anotar(url, enviar, error=e)
            raise
        _anotar(url, enviar, respuesta=res)
        return res


def anotar_post(url, payload, respuesta=None, error=None):
    """Para los requests.post directos al API de Telegram que quedan en el bot (alert_admin_sync de main.py, el borrado del
    canal): anota igual que el request de PTB, solo si es el token del bot de ME (lo decide tg_registro). Nunca lanza."""
    try:
        r = _registro()
        if r is None:
            return
        u = str(url or "")
        m = _RE_TOKEN_URL.search(u)
        r.anotar(u.rstrip("/").rsplit("/", 1)[-1], payload if isinstance(payload, dict) else {}, respuesta=respuesta,
                 error=error, token=m.group(1) if m else "")
    except Exception:
        pass


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
