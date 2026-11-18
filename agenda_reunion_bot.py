# -*- coding: utf-8 -*-
"""Reunión de agenda diaria — el lado del bot (fase 4 v2.0).

El Jefe de redacción (agents/jefe_redaccion.py, en el harness) manda a las 8:15 la propuesta de agenda con botones. Este
módulo hace que el bot escuche la respuesta de Leo mientras la reunión está abierta:

  · un TEXTO de Leo  → el Jefe lo interpreta y contesta UNA vez con el plan revisado
  · un AUDIO de Leo  → se transcribe con el mecanismo que ya tiene el bot (`_whisper_from_file`), se le muestra lo que se
                       entendió y se trata como texto
  · un LINK pegado   → entra como tema del plan (no como nota: para publicar la nota hay que cerrar la agenda)
  · los botones `agd_*` (aprobar, ajustar, vetar, opinión, cerrar, irrupciones) y `agf_*` (elegir frase del día)
  · /agenda          → muestra el plan de hoy

Todo detrás de AGENDA_REUNION (0 | dry | 1), la misma bandera del harness; con `0` o `dry` ningún handler captura nada:
los mensajes de Leo siguen yendo adonde iban. NO cambia ningún comando ni botón existente: los filtros de acá solo se
cumplen con la reunión de HOY abierta (estado propuesta / deliberando / jefe, antes de las 11:00) y cuando Leo no está
en medio de otro flujo del bot (cualquier `awaiting_*`, un evento en curso); en esos casos el mensaje sigue su camino.

La conversación y el plan viven en la base del harness (tablas agenda_*), no en la memoria del bot: un reinicio no los corta.
Este archivo no tiene claves ni datos personales: el repo del bot es público.
"""
import asyncio
import logging
import os
import re
import sys
import tempfile

from telegram import Update
from telegram.ext import CallbackQueryHandler, CommandHandler, ContextTypes, MessageHandler, filters

logger = logging.getLogger("agenda_reunion_bot")

HARNESS_DIR = os.environ.get("HARNESS_DIR", "/opt/me-harness")
_URL = re.compile(r"https?://\S+", re.I)
_CONTEXTO_AUDIO = ("reunión de agenda de un diario de negocios para pymes argentinas; temas, políticos, empresas, valores y mensajes "
                   "de la comunidad")


# ── bandera ───────────────────────────────────────────────────────────────────

def modo() -> str:
    """'0' | 'dry' | '1' — igual criterio que core/agenda_dia.modo() del harness."""
    v = (os.environ.get("AGENDA_REUNION") or "0").strip().lower()
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
        from core import agenda_dia
        from agents import jefe_redaccion, frases_dia
        self.agenda_dia, self.jefe, self.frases = agenda_dia, jefe_redaccion, frases_dia


_H = None
_RELOJ = None          # para las pruebas: función sin argumentos que devuelve el «ahora» (datetime con zona); None = reloj real


def set_reloj(fn):
    """Para las pruebas: la hora que el bot le pasa al harness (así nada depende de la hora real)."""
    global _RELOJ
    _RELOJ = fn


def _ahora():
    return _RELOJ() if _RELOJ else None


def harness():
    global _H
    if _H is None:
        _H = _Harness()
    return _H


def set_harness(h):
    """Para las pruebas: reemplaza el acceso al harness."""
    global _H
    _H = h


# ── cuándo captura el bot ─────────────────────────────────────────────────────

def abierta() -> bool:
    """True si la bandera está en `1` y hay una reunión de hoy abierta. Nunca lanza."""
    try:
        if modo() != "1":
            return False
        return harness().agenda_dia.reunion_abierta(_ahora()) is not None
    except Exception as e:
        logger.debug("abierta(): %s", e)
        return False


class FiltroReunion(filters.MessageFilter):
    """Se cumple solo con la reunión de hoy abierta (y la bandera en 1)."""
    name = "agenda_reunion_abierta"

    def filter(self, message) -> bool:
        return abierta()


# Claves de `user_data` con las que el bot espera que Leo conteste OTRA cosa en su próximo mensaje. `handle_link` usa
# `awaiting_*` (65 claves) y `waiting_*` (waiting_for_frase_ht, waiting_for_frase_custom_hour, waiting_for_frase_sched_ht,
# waiting_for_edit_title, waiting_for_edit_photo, waiting_for_new_source; las del flujo de artículo viejo y del ECO se
# borraron el 18/11/2026): todo lo que empiece así cuenta. Más estas, que se leen con pop() en el mismo handler.
_PREFIJOS_OTRO_FLUJO = ("awaiting", "waiting")
_CLAVES_OTRO_FLUJO = ("evento", "input_evento", "edito_rename_id", "edito_add_tema")


def en_otro_flujo(user_data) -> bool:
    """Leo está contestando otra cosa del bot (una pregunta `awaiting_*`/`waiting_*`, una cobertura de evento): el mensaje
    no es de la reunión y sigue su camino."""
    try:
        ud = user_data or {}
        for k, v in ud.items():
            if str(k).startswith(_PREFIJOS_OTRO_FLUJO) and v:
                return True
        return any(ud.get(k) is not None for k in _CLAVES_OTRO_FLUJO)
    except Exception:
        return False


def es_solo_links(texto) -> bool:
    return bool(_URL.search(texto or "")) and len(re.findall(r"\w+", _URL.sub("", texto or ""))) < 4


# ── respuesta ─────────────────────────────────────────────────────────────────

LIMITE_TG = 4096
PLAZO_POST_CIERRE_S = 90
_TAREAS = set()        # referencias a las tareas en segundo plano (si no, el recolector puede matarlas)


def partir(texto, n=4000) -> list:
    """Parte un texto en pedazos de hasta `n` caracteres, cortando en saltos de línea (los tags HTML del Jefe no cruzan líneas)."""
    texto = texto or ""
    if len(texto) <= n:
        return [texto]
    out, cur = [], ""
    for linea in texto.split("\n"):
        while len(linea) > n:                                  # una línea sola más larga que el tope: se corta dura
            if cur:
                out.append(cur)
                cur = ""
            out.append(linea[:n])
            linea = linea[n:]
        if cur and len(cur) + 1 + len(linea) > n:
            out.append(cur)
            cur = linea
        else:
            cur = (cur + "\n" + linea) if cur else linea
    if cur:
        out.append(cur)
    return out


async def responder(message, texto, botones=None):
    """reply_text partido en pedazos de ≤4000; los botones van en el último."""
    pedazos = partir(texto)
    for i, t in enumerate(pedazos):
        await message.reply_text(t, parse_mode="HTML", reply_markup=botones if i == len(pedazos) - 1 else None,
                                 disable_web_page_preview=True)


def _lanzar_post_cierre(message, fecha):
    """Rehacer las frases al cerrar la agenda es lento (bajar páginas, modelos): va en una tarea aparte, con plazo de 90 s,
    DESPUÉS de haber contestado el botón o el mensaje."""
    async def tarea():
        h = harness()
        try:
            linea = await asyncio.wait_for(asyncio.to_thread(h.jefe.post_cierre, fecha), PLAZO_POST_CIERRE_S)
        except asyncio.TimeoutError:
            linea = "⏱ Las frases del día no alcanzaron a rehacerse en %d s; quedan las de las 8:15." % PLAZO_POST_CIERRE_S
        except Exception as e:
            logger.exception("post cierre: %s", e)
            return
        if linea:
            try:
                await message.reply_text(linea)
            except Exception as e:
                logger.debug("post cierre aviso: %s", e)
    t = asyncio.ensure_future(tarea())
    _TAREAS.add(t)
    t.add_done_callback(_TAREAS.discard)
    return t


async def _deliberar_y_responder(update, texto, tipo):
    """Pasa el mensaje de Leo al Jefe y contesta. Devuelve False si no había reunión abierta (el llamador sigue su camino)."""
    h = harness()
    res = await asyncio.to_thread(h.jefe.deliberar, texto, tipo, ahora=_ahora())
    if not res.get("ok"):
        return False
    await responder(update.message, res["texto"], res.get("botones"))
    if res.get("post_cierre"):
        _lanzar_post_cierre(update.message, res["post_cierre"])
    return True


def hacer_handler_texto(fallback, pausado=None):
    async def handle(update: Update, context: ContextTypes.DEFAULT_TYPE):
        if (pausado and pausado()) or en_otro_flujo(context.user_data):
            return await fallback(update, context)
        texto = (update.message.text or "").strip()
        try:
            tipo = "link" if es_solo_links(texto) else "texto"
            if await _deliberar_y_responder(update, texto, tipo):
                return
        except Exception as e:
            logger.exception("reunión de agenda (texto): %s", e)
            try:
                await update.message.reply_text("⚠️ No pude procesar tu mensaje en la reunión (%s). Quedó guardado; usá los botones." % str(e)[:80])
            except Exception:
                pass
            return
        return await fallback(update, context)       # la reunión se cerró justo antes: el mensaje sigue su camino
    return handle


def hacer_handler_voz(transcribir, fallback, pausado=None):
    """`transcribir(path, contexto) -> str` es `_whisper_from_file` del bot (gpt-4o-transcribe con respaldo whisper-1)."""
    async def handle(update: Update, context: ContextTypes.DEFAULT_TYPE):
        if (pausado and pausado()) or en_otro_flujo(context.user_data):
            return await fallback(update, context)
        m = update.message
        media = m.voice or m.audio
        status = await m.reply_text("🎙️ Escuchando…")
        path = None
        try:
            f = await media.get_file(read_timeout=90, connect_timeout=30)
            ext = os.path.splitext(getattr(media, "file_name", None) or "")[1].lower() or ".ogg"
            path = os.path.join(tempfile.gettempdir(), "agd_%s%s" % (media.file_unique_id, ext))
            await f.download_to_drive(path)
        except Exception as e:
            await status.edit_text("❌ No pude bajar el audio (%s). Telegram limita los archivos del bot a ~20 MB." % str(e)[:80])
            return
        try:
            texto = await asyncio.to_thread(transcribir, path, _CONTEXTO_AUDIO)
        finally:
            try:
                os.remove(path)
            except Exception:
                pass
        if not texto:
            await status.edit_text("⚠️ No pude transcribir el audio. Probá reenviarlo o escribilo.")
            return
        await status.edit_text("🎙️ Entendí: «%s»" % texto[:900])
        try:
            if not await _deliberar_y_responder(update, texto, "audio"):
                await m.reply_text("La reunión de agenda ya está cerrada; no lo sumé al plan.")
        except Exception as e:
            logger.exception("reunión de agenda (audio): %s", e)
            await m.reply_text("⚠️ No pude procesar el audio en la reunión (%s). Quedó guardado." % str(e)[:80])
    return handle


def hacer_handler_boton(frase_flow):
    """Botones `agd_*` (reunión) y `agf_*` (frases). `frase_flow(message, context, texto)` arma la vista previa de /frases.
    Primero se mira la bandera (sin tocar el harness), después se contesta el callback (`q.answer()`) y recién ahí se trabaja:
    lo largo (rehacer frases al cerrar) va en una tarea aparte con plazo."""
    async def handle(update: Update, context: ContextTypes.DEFAULT_TYPE):
        q = update.callback_query
        data = q.data or ""
        if modo() != "1":
            await q.answer("La reunión de agenda no está activa.", show_alert=False)
            return
        await q.answer()
        h = harness()
        if data.startswith("agf_el:"):
            e = await asyncio.to_thread(h.frases.elegir, data.split(":", 1)[1])
            if not e:
                await q.message.reply_text("Esa frase ya no está.")
                return
            try:
                await q.edit_message_reply_markup(reply_markup=None)
            except Exception:
                pass
            await q.message.reply_text("💬 Elegiste a %s. Fuente: %s" % (e["autor"], e["url"]), disable_web_page_preview=True)
            await frase_flow(q.message, context, e["texto_placa"])
            return
        if data.startswith("agf_no:"):
            try:
                await q.edit_message_reply_markup(reply_markup=None)
            except Exception:
                pass
            await q.message.reply_text("Hoy no sale ninguna frase.")
            return
        res = await asyncio.to_thread(h.jefe.accion_boton, data, ahora=_ahora())
        if not res.get("ok") or not res.get("texto"):
            if res.get("toast"):
                await q.message.reply_text(res["toast"])
            return
        texto, botones = res["texto"], res.get("botones")
        if res.get("editar") and len(texto) <= LIMITE_TG:
            try:
                await q.edit_message_text(texto, parse_mode="HTML", reply_markup=botones, disable_web_page_preview=True)
            except Exception as e:      # «message is not modified», o el mensaje es una foto: se manda aparte
                logger.debug("editar: %s", e)
                await responder(q.message, texto, botones)
        else:
            await responder(q.message, texto, botones)
        if res.get("post_cierre"):
            _lanzar_post_cierre(q.message, res["post_cierre"])
    return handle


async def cmd_agenda(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/agenda → el plan de hoy. Con la bandera en 0 no responde (el comando no existe todavía para Leo)."""
    m = modo()
    if m == "0":
        return
    if m == "dry":
        await update.message.reply_text("La reunión de agenda está en modo prueba (dry): arma la propuesta pero no manda nada.")
        return
    try:
        h = harness()
        fecha = h.agenda_dia.fecha_de(_ahora())
        if not h.agenda_dia.get_reunion(fecha):
            await update.message.reply_text("Hoy todavía no hubo reunión de agenda (la propuesta sale a las 8:15).")
            return
        texto, kb = await asyncio.to_thread(h.jefe.texto_plan, fecha, "Plan del día")
        await responder(update.message, texto, kb)
    except Exception as e:
        logger.exception("/agenda: %s", e)
        await update.message.reply_text("⚠️ No pude leer el plan: %s" % str(e)[:100])


def registrar(app, *, transcribir, fallback_texto, fallback_media, frase_flow, pausado=None):
    """Registra los handlers. Llamar ANTES de registrar `handle_link` y `handle_evento_media`: dentro de un mismo grupo
    gana el primero que coincide, y estos solo coinciden con la reunión abierta."""
    app.add_handler(CommandHandler("agenda", cmd_agenda))
    app.add_handler(CallbackQueryHandler(hacer_handler_boton(frase_flow), pattern=r"^(agd_|agf_)"))
    filtro = FiltroReunion()
    privado = filters.ChatType.PRIVATE                         # la reunión es en el chat privado de Leo, no en grupos ni canales
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND & ~filters.UpdateType.EDITED & privado & filtro,
                                   hacer_handler_texto(fallback_texto, pausado)))
    app.add_handler(MessageHandler((filters.VOICE | filters.AUDIO) & ~filters.UpdateType.EDITED & privado & filtro,
                                   hacer_handler_voz(transcribir, fallback_media, pausado)))
    logger.info("reunión de agenda registrada (AGENDA_REUNION=%s)", modo())
