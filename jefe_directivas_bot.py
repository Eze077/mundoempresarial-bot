# -*- coding: utf-8 -*-
"""Leo le habla al Jefe de redacción durante todo el día — el lado del bot (v2.0, 17/11/2026).

El Jefe (agents/jefe_directivas.py, en el harness) interpreta lo que Leo dice y lo reparte en acciones que ya existen: plan del
día, veto («hoy nada de X»), enfoque para el redactor, una nota concreta (con clic), una regla permanente (con clic), frases.
Desde la v2.0 el Jefe razona con Claude Opus, pregunta si duda y sigue el hilo: cuando Leo contesta con un reply, el bot le pasa el id y el
texto del mensaje al que contesta (`hilo_kw`). Los botones nuevos (`jfd_otra`, `jfd_op`) entran por el mismo patrón `^jfd_`.
Este módulo solo decide CUÁNDO un mensaje de Leo es para el Jefe, sin robarle mensajes a ningún otro flujo:

  (a) un texto o un audio que es una RESPUESTA (reply de Telegram) a un mensaje del Jefe (plan, parte de las 20:00, informe del
      editor automático, avisos, frases del día, y sus propias respuestas)
  (b) un texto que empieza con «Jefe» («Jefe, hoy nada de Milei»), o un audio cuya transcripción empieza con «Jefe»
  (c) el comando oculto /jefe <texto>, de respaldo

Lo demás (un link, un mensaje normal, un audio cualquiera) sigue adonde iba: `handle_link` / `handle_evento_media`.
Con la reunión de agenda ABIERTA (antes de las 11:00) todo sigue como hoy: la captura la hace agenda_reunion_bot.
Mientras Leo está en medio de otro flujo (`awaiting_*`, un evento en curso) el mensaje tampoco se toca.

Bandera JEFE_DIRECTIVAS (0 | dry | 1; sin variable = 1): con 0 ningún handler captura nada ni responde; dry contesta con lo que
HARÍA, sin aplicar ni anotar nada (lo hace el harness).

Audios: fuera de la reunión el bot no transcribía ningún audio suelto (handle_evento_media solo lo hace con un evento en curso), así
que acá se transcribe UNA vez con el mismo `_whisper_from_file` y, si no es para el Jefe, el audio sigue su camino sin haberlo
molestado (con Leo fuera de otro flujo, ese camino no hace nada).

El estado vive en harness.db (tabla jefe_directivas): un reinicio del bot no corta nada. Sin claves ni datos personales: repo público.
"""
import asyncio
import logging
import os
import re
import sys
import tempfile

from telegram import Update
from telegram.ext import CallbackQueryHandler, CommandHandler, ContextTypes, MessageHandler, filters

import agenda_reunion_bot as _AB

logger = logging.getLogger("jefe_directivas_bot")

HARNESS_DIR = os.environ.get("HARNESS_DIR", "/opt/me-harness")
PLAZO_BOTON_S = 150            # frases del día baja páginas y llama modelos; el resto contesta al instante
_CONTEXTO_AUDIO = "directivas del director de un diario de negocios para pymes argentinas a su jefe de redacción: temas, empresas, políticos"

# «Jefe, …» / «jefe: …». «Jefe de Gabinete dijo…» NO es un llamado al Jefe (salvo «Jefe de redacción»).
_RE_JEFE = re.compile(r"^\s*jefe\b(?!\s+de\s+(?!redacci[oó]n))[\s,:;.\-–—¡!]*\S", re.I)


# ── bandera ───────────────────────────────────────────────────────────────────

def modo() -> str:
    """'0' | 'dry' | '1' — mismo criterio que agents/jefe_directivas.modo() del harness. Sin variable = '1'; lo desconocido = '0'."""
    v = (os.environ.get("JEFE_DIRECTIVAS") or "1").strip().lower()
    if v in ("1", "on", "si", "sí", "true", "vivo"):
        return "1"
    if v in ("dry", "prueba", "sombra"):
        return "dry"
    return "0"


# ── acceso al harness (inyectable en las pruebas) ─────────────────────────────

class _Harness:
    def __init__(self):
        if HARNESS_DIR not in sys.path:
            sys.path.insert(0, HARNESS_DIR)
        from agents import jefe_directivas
        self.dir = jefe_directivas


_H = None


def harness():
    global _H
    if _H is None:
        _H = _Harness()
    return _H


def set_harness(h):
    """Para las pruebas: reemplaza el acceso al harness."""
    global _H
    _H = h


# ── cuándo es para el Jefe ────────────────────────────────────────────────────

def empieza_con_jefe(texto) -> bool:
    return bool(_RE_JEFE.match(texto or ""))


def es_respuesta_al_jefe(message) -> bool:
    """¿Leo está contestando (reply) a un mensaje del Jefe? Falla cerrado: ante cualquier problema, no."""
    try:
        rt = getattr(message, "reply_to_message", None)
        if rt is None:
            return False
        autor = getattr(rt, "from_user", None)
        if autor is not None and not getattr(autor, "is_bot", False):
            return False
        texto = getattr(rt, "text", None) or getattr(rt, "caption", None) or ""
        return bool(harness().dir.es_mensaje_del_jefe(getattr(rt, "message_id", None), texto))
    except Exception as e:
        logger.debug("es_respuesta_al_jefe: %s", e)
        return False


def reunion_abierta() -> bool:
    """Con la reunión de agenda abierta captura agenda_reunion_bot: esto no se mete."""
    try:
        return _AB.abierta()
    except Exception:
        return False


class FiltroTexto(filters.MessageFilter):
    """Texto para el Jefe: empieza con «Jefe» o es la respuesta a un mensaje suyo. Un link pegado solo no (sigue al flujo de publicar)."""
    name = "jefe_directivas_texto"

    def filter(self, message) -> bool:
        try:
            if modo() == "0" or reunion_abierta():
                return False
            t = (getattr(message, "text", None) or "").strip()
            if not t or _AB.es_solo_links(t):
                return False
            return empieza_con_jefe(t) or es_respuesta_al_jefe(message)
        except Exception:
            return False


class FiltroVoz(filters.MessageFilter):
    """Cualquier audio del chat privado con la bandera prendida y sin reunión abierta. El handler decide si es para el Jefe
    (reply a un mensaje suyo o transcripción que empieza con «Jefe») y, si no, lo devuelve a su camino."""
    name = "jefe_directivas_voz"

    def filter(self, message) -> bool:
        try:
            return modo() != "0" and not reunion_abierta()
        except Exception:
            return False


# ── respuesta ─────────────────────────────────────────────────────────────────

def _registrar(sent, res):
    try:
        mid = getattr(sent, "message_id", None)
        if mid and res.get("directiva_id") is not None:
            harness().dir.registrar_mensaje(mid, res.get("directiva_id"))
        elif mid:
            harness().dir.registrar_mensaje(mid, None)
    except Exception as e:
        logger.debug("registrar mensaje del Jefe: %s", e)


async def responder(message, res):
    """UNA respuesta del Jefe (partida solo si pasa los 4096 caracteres de Telegram; los botones van en el último pedazo).
    Cada mensaje enviado queda registrado como del Jefe: si Leo le contesta con un reply, el bot sabe a quién le habla."""
    pedazos = _AB.partir(res.get("texto") or "")
    for i, t in enumerate(pedazos):
        sent = await message.reply_text(t, parse_mode="HTML", reply_markup=res.get("botones") if i == len(pedazos) - 1 else None,
                                        disable_web_page_preview=True)
        _registrar(sent, res)


def hilo_kw(message) -> dict:
    """Si Leo contesta (reply) a un mensaje, el Jefe necesita saber a cuál para seguir el hilo de una pregunta abierta (v2.0): el id del
    mensaje y su texto (por si el Jefe no lo tenía registrado: el parte de las 20:00, un informe). Nunca lanza."""
    try:
        rt = getattr(message, "reply_to_message", None)
        if rt is None or not getattr(rt, "message_id", None):
            return {}
        return {"reply_a": rt.message_id, "reply_texto": (getattr(rt, "text", None) or getattr(rt, "caption", None) or "")[:900]}
    except Exception:
        return {}


async def _procesar_y_responder(message, texto, tipo, canal) -> bool:
    """Pasa el mensaje al Jefe y contesta. False si el Jefe no lo tomó (bandera apagada justo antes): el llamador sigue su camino."""
    kw = hilo_kw(message)
    try:
        res = await asyncio.to_thread(harness().dir.procesar, texto, tipo, canal, **kw)
    except TypeError:
        if not kw:
            raise
        res = await asyncio.to_thread(harness().dir.procesar, texto, tipo, canal)      # un harness anterior a la v2.0 no sigue hilos
    if not res.get("ok"):
        if res.get("motivo") == "vacio" and res.get("texto"):
            await message.reply_text(res["texto"], parse_mode="HTML")
            return True
        return False
    await responder(message, res)
    return True


# ── handlers ──────────────────────────────────────────────────────────────────

def hacer_handler_texto(fallback, pausado=None):
    async def handle(update: Update, context: ContextTypes.DEFAULT_TYPE):
        if (pausado and pausado()) or _AB.en_otro_flujo(context.user_data):
            return await fallback(update, context)
        texto = (update.message.text or "").strip()
        canal = "jefe" if empieza_con_jefe(texto) else "respuesta"
        try:
            if await _procesar_y_responder(update.message, texto, "texto", canal):
                return
        except Exception as e:
            logger.exception("directiva al Jefe (texto): %s", e)
            try:
                await update.message.reply_text("⚠️ No pude pasarle tu mensaje al Jefe (%s). No toqué nada." % str(e)[:80])
            except Exception:
                pass
            return
        return await fallback(update, context)
    return handle


def hacer_handler_voz(transcribir, fallback, pausado=None):
    """`transcribir(path, contexto) -> str` es `_whisper_from_file` del bot. Se transcribe una sola vez."""
    async def handle(update: Update, context: ContextTypes.DEFAULT_TYPE):
        if (pausado and pausado()) or _AB.en_otro_flujo(context.user_data):
            return await fallback(update, context)
        m = update.message
        media = m.voice or m.audio
        a_jefe = es_respuesta_al_jefe(m)               # un reply a un mensaje del Jefe es para él sin más
        path, status = None, None
        try:
            if a_jefe:
                status = await m.reply_text("🎙️ Escuchando…")
            f = await media.get_file(read_timeout=90, connect_timeout=30)
            ext = os.path.splitext(getattr(media, "file_name", None) or "")[1].lower() or ".ogg"
            path = os.path.join(tempfile.gettempdir(), "jfd_%s%s" % (media.file_unique_id, ext))
            await f.download_to_drive(path)
            texto = await asyncio.to_thread(transcribir, path, _CONTEXTO_AUDIO)
        except Exception as e:
            logger.debug("audio para el Jefe: %s", e)
            texto = ""
            if a_jefe and status is not None:
                await status.edit_text("❌ No pude bajar o transcribir el audio (%s)." % str(e)[:80])
                return
        finally:
            if path:
                try:
                    os.remove(path)
                except Exception:
                    pass
        texto = (texto or "").strip()
        if not a_jefe and not empieza_con_jefe(texto):
            return await fallback(update, context)    # no era para el Jefe: sigue su camino, ya transcripto y sin molestar a nadie
        if not texto:
            await status.edit_text("⚠️ No pude transcribir el audio. Probá reenviarlo o escribilo.")
            return
        if status is None:
            status = await m.reply_text("🎙️ …")
        await status.edit_text("🎙️ Entendí: «%s»" % texto[:900])
        canal = "jefe" if empieza_con_jefe(texto) else "respuesta"
        try:
            if not await _procesar_y_responder(m, texto, "audio", canal):
                return await fallback(update, context)
        except Exception as e:
            logger.exception("directiva al Jefe (audio): %s", e)
            await m.reply_text("⚠️ No pude pasarle tu audio al Jefe (%s). No toqué nada." % str(e)[:80])
    return handle


async def cmd_jefe(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/jefe <texto> — respaldo. Con la bandera en 0 no responde (el comando no existe todavía para Leo)."""
    if modo() == "0":
        return
    m = update.message
    texto = " ".join(context.args or []).strip() if getattr(context, "args", None) else ""
    if not texto:
        texto = re.sub(r"^/jefe(?:@\w+)?\s*", "", (m.text or "").strip(), flags=re.I)
    try:
        if reunion_abierta() and texto:       # con la reunión abierta, todo sigue como hoy: la deliberación de la agenda
            if await _AB._deliberar_y_responder(update, texto, "texto"):
                return
        if not await _procesar_y_responder(m, texto, "texto", "comando"):
            return
    except Exception as e:
        logger.exception("/jefe: %s", e)
        await m.reply_text("⚠️ No pude pasarle tu mensaje al Jefe (%s). No toqué nada." % str(e)[:80])


def hacer_handler_boton():
    """Botones `jfd_si` (el clic), `jfd_no` (descartar) y `jfd_un` (deshacer). Se contesta el callback primero y recién ahí se trabaja."""
    async def handle(update: Update, context: ContextTypes.DEFAULT_TYPE):
        q = update.callback_query
        data = q.data or ""
        if modo() != "1":
            await q.answer("Las directivas al Jefe no están activas.", show_alert=False)
            return
        await q.answer()
        try:
            res = await asyncio.wait_for(asyncio.to_thread(harness().dir.boton, data), PLAZO_BOTON_S)
        except asyncio.TimeoutError:
            await q.message.reply_text("⏱ Está tardando más de %d s. Sigue en segundo plano; si llega, te lo mando aparte." % PLAZO_BOTON_S)
            return
        except Exception as e:
            logger.exception("botón del Jefe %s: %s", data, e)
            await q.message.reply_text("⚠️ No pude resolver el botón (%s). No toqué nada." % str(e)[:80])
            return
        if not res.get("ok") or not res.get("texto"):
            if res.get("toast"):
                await q.message.reply_text(res["toast"])
            return
        texto, botones = res["texto"], res.get("botones")
        if len(texto) <= _AB.LIMITE_TG:
            try:
                await q.edit_message_text(texto, parse_mode="HTML", reply_markup=botones, disable_web_page_preview=True)
                _registrar(q.message, res)
                return
            except Exception as e:      # «message is not modified» u otro: se manda aparte
                logger.debug("editar: %s", e)
        await responder(q.message, res)
    return handle


def registrar(app, *, transcribir, fallback_texto, fallback_media, pausado=None):
    """Registra los handlers. Llamar DESPUÉS de registrar agenda_reunion_bot y ANTES de `handle_link` y `handle_evento_media`:
    dentro de un mismo grupo gana el primero que coincide, y estos solo coinciden con lo que es para el Jefe."""
    app.add_handler(CommandHandler("jefe", cmd_jefe))
    app.add_handler(CallbackQueryHandler(hacer_handler_boton(), pattern=r"^jfd_"))
    privado = filters.ChatType.PRIVATE
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND & ~filters.UpdateType.EDITED & privado & FiltroTexto(),
                                   hacer_handler_texto(fallback_texto, pausado)))
    app.add_handler(MessageHandler((filters.VOICE | filters.AUDIO) & ~filters.UpdateType.EDITED & privado & FiltroVoz(),
                                   hacer_handler_voz(transcribir, fallback_media, pausado)))
    logger.info("directivas al Jefe registradas (JEFE_DIRECTIVAS=%s)", modo())
