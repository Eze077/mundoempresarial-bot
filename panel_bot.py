# -*- coding: utf-8 -*-
"""panel_bot.py — /estado, /pausa y los botones del harness que el bot solo despacha (fase 4 v2.0, Anexo O). v1.0 · 18/11/2026

Por qué existe
--------------
· /pausa: hoy no hay UN freno. /STOP solo frena el bot, no al harness, y se pierde en cada reinicio. La pausa general vive en
  el harness (`core/pausa.py`: un archivo en su carpeta que leen los dos) y frena bot, publicación, colas de difusión,
  boletín y mails, y la capa automática; persiste a los reinicios. Detrás de la bandera PAUSA_GENERAL (sin variable = apagada:
  /pausa contesta que todavía no está prendida y nada cambia). La bandera la leen el bot y el harness del mismo .env.
· /estado: en un mensaje, lo que hace falta para saber si el sistema anda (pausa, pipeline, editor automático, boletín,
  colas de X y Meta, gasto, incidentes, banderas). Lo arma el harness (`core/estado.py`, solo lee); acá solo se pide.
  No cambia nada de lo que existe: no lleva bandera.
· Botones que emite el harness y el bot solo despacha:
    h_bv2_…  «⛔ Frenar» del boletín v2  → agents/boletin.callback(data)   (el prefijo h_bol_ es del esquema viejo)
    h_ea_…   editor automático (Publicar una nota que espera, Revertir, Volver a vivo) → agents/editor_auto.callback(data)
  Ninguno se emite hoy (BOLETIN_AUTO y EDITOR_AUTO no están en 1): no cambia nada de lo que existe.

Sin claves ni datos personales: el repo del bot es público. Toda llamada al harness va en un hilo con tope de tiempo.
"""
import asyncio
import html
import logging
import os
import sys

logger = logging.getLogger("panel_bot")

VERSION = "v1.0"
FECHA_VERSION = "18/11/2026"
HARNESS_DIR = os.environ.get("HARNESS_DIR", "/opt/me-harness")
PLAZO_S = 25
_PRENDIDA = {"1", "on", "si", "sí", "true", "vivo"}
_LEVANTAR = {"off", "no", "sigue", "seguir", "reanudar", "resume", "fin", "sacar", "saca", "levantar"}


# ── acceso al harness (reemplazable en las pruebas) ───────────────────────────────────────────────────────────────────
class _Harness:
    def __init__(self):
        if HARNESS_DIR not in sys.path:
            sys.path.insert(0, HARNESS_DIR)

    @property
    def pausa(self):
        from core import pausa
        return pausa

    @property
    def estado(self):
        from core import estado
        return estado

    def callback(self, data):
        """El despacho de los botones del harness. Devuelve el texto de la respuesta, o None si nadie lo atiende."""
        if data.startswith("h_bv2_"):
            from agents import boletin
            return boletin.callback(data, quien="leo")
        if data.startswith("h_ea_"):
            from agents import editor_auto
            return editor_auto.callback(data, quien="leo")
        return None


_H = None


def harness():
    global _H
    if _H is None:
        _H = _Harness()
    return _H


def set_harness(h):
    """Para las pruebas."""
    global _H
    _H = h


# ── la pausa general ──────────────────────────────────────────────────────────────────────────────────────────────────
def bandera_pausa() -> bool:
    try:
        return (os.environ.get("PAUSA_GENERAL") or "").strip().lower() in _PRENDIDA
    except Exception:
        return False


def _ruta_pausa() -> str:
    return os.environ.get("PAUSA_GENERAL_PATH") or os.path.join(HARNESS_DIR, "pausa_general.flag")


def pausa_activa() -> bool:
    """La mira el bot en cada link y en cada programada: bandera prendida Y el archivo del harness puesto. No importa nada del
    harness (es barata y anda aunque el harness esté a medio desplegar). Nunca lanza."""
    try:
        return bandera_pausa() and os.path.exists(_ruta_pausa())
    except Exception:
        return False


def _teclado(pausada: bool) -> dict:
    if pausada:
        return {"inline_keyboard": [[{"text": "▶️ Reanudar todo", "callback_data": "pz_off"}]]}
    return {"inline_keyboard": [[{"text": "⏸ Pausar todo", "callback_data": "pz_on"}]]}


def _cubre() -> str:
    try:
        return html.escape(harness().pausa.texto_cubre(), quote=False)
    except Exception:
        return ""


def _texto_pausada(st: dict, recien: bool) -> str:
    t = ("⏸ <b>Pausa general puesta</b>" if recien else "⏸ <b>La pausa general ya estaba puesta</b>")
    t += " desde %s (%s)." % (html.escape(str(st.get("desde") or "?")[5:16], quote=False), html.escape(str(st.get("por") or "?"), quote=False))
    if st.get("motivo"):
        t += "\nMotivo: %s" % html.escape(str(st["motivo"])[:200], quote=False)
    t += "\nSigue puesta aunque se reinicie todo. /pausa off o el botón la sacan.\n\n" + _cubre()
    return t


async def _hilo(fn, *a, **k):
    return await asyncio.wait_for(asyncio.to_thread(fn, *a, **k), PLAZO_S)


async def cmd_pausa(update, context):
    """/pausa [motivo] pone la pausa general; /pausa off la saca."""
    msg = update.message
    args = [a for a in (getattr(context, "args", None) or [])]
    if not bandera_pausa():
        await msg.reply_text("El freno general todavía no está prendido (falta PAUSA_GENERAL=1 en el .env del bot y del harness). "
                             "Mientras tanto /STOP frena solo el bot y se pierde al reiniciar.")
        return
    try:
        p = harness().pausa
    except Exception as e:
        logger.warning("/pausa: no pude cargar core.pausa del harness: %s", e)
        await msg.reply_text("⚠️ El harness todavía no tiene el freno general (falta desplegarlo). /STOP frena solo el bot.")
        return
    try:
        if args and args[0].strip().lower() in _LEVANTAR:
            r = await _hilo(p.levantar, "leo")
            if not r.get("ok"):
                await msg.reply_text("⚠️ No pude sacar la pausa: %s" % r.get("motivo", "?"))
            elif r.get("ya_estaba"):
                await msg.reply_text("▶️ No había pausa general.", reply_markup=_teclado(False))
            else:
                await msg.reply_text("▶️ <b>Pausa general levantada.</b> Todo vuelve a correr: las colas de X y Meta retoman "
                                     "lo que no venció y los slots salteados no salen tarde.",
                                     parse_mode="HTML", reply_markup=_teclado(False))
            return
        r = await _hilo(p.poner, "leo", " ".join(args)[:200])
    except Exception as e:
        logger.warning("/pausa: %s", e)
        await msg.reply_text("⚠️ No pude tocar la pausa general (%s). Probá de nuevo; /STOP frena al menos el bot." % type(e).__name__)
        return
    if not r.get("ok"):
        await msg.reply_text("⚠️ No pude poner la pausa: %s" % r.get("motivo", "?"))
        return
    await msg.reply_text(_texto_pausada(r.get("estado") or {}, not r.get("ya_estaba")), parse_mode="HTML",
                         reply_markup=_teclado(True))


async def _responder(query, texto=None, alerta=False):
    """query.answer sin explotar: main.py ya puede haber respondido el callback (Telegram deja una sola vez)."""
    try:
        await query.answer(texto, show_alert=alerta)
    except Exception as e:
        logger.debug("answer: %s", e)


async def boton_pausa(update, context):
    """Botones pz_on / pz_off de /pausa y /estado."""
    query = update.callback_query
    if not bandera_pausa():
        await _responder(query, "El freno general no está prendido (PAUSA_GENERAL).", True)
        return
    try:
        p = harness().pausa
        if query.data == "pz_off":
            r = await _hilo(p.levantar, "leo")
            texto = "▶️ <b>Pausa general levantada.</b>" if r.get("ok") else "⚠️ No pude sacarla: %s" % html.escape(str(r.get("motivo")))
            kb = _teclado(False) if r.get("ok") else _teclado(True)
        elif query.data == "pz_on":
            r = await _hilo(p.poner, "leo", "")
            texto = _texto_pausada(r.get("estado") or {}, not r.get("ya_estaba")) if r.get("ok") else \
                "⚠️ No pude ponerla: %s" % html.escape(str(r.get("motivo")))
            kb = _teclado(bool(r.get("ok")))
        else:
            await _responder(query, "Botón desconocido.", True)
            return
    except Exception as e:
        logger.warning("boton pausa %s: %s", query.data, e)
        await _responder(query, "No pude tocar la pausa: probá de nuevo.", True)
        return
    await _responder(query)
    try:
        await query.edit_message_text(texto, parse_mode="HTML", reply_markup=kb)
    except Exception as e:
        logger.debug("boton pausa: no pude editar: %s", e)


# ── /estado ───────────────────────────────────────────────────────────────────────────────────────────────────────────
async def cmd_estado(update, context):
    msg = update.message
    try:
        texto = await _hilo(harness().estado.texto)
    except Exception as e:
        logger.warning("/estado: %s", e)
        await msg.reply_text("⚠️ No pude armar el estado (%s). ¿El harness tiene core/estado.py?" % type(e).__name__)
        return
    kb = _teclado(pausa_activa()) if bandera_pausa() else None
    await msg.reply_text(texto, parse_mode="HTML", reply_markup=kb, disable_web_page_preview=True)


# ── botones del harness ───────────────────────────────────────────────────────────────────────────────────────────────
async def boton_harness(query) -> None:
    """h_bv2_… y h_ea_…: le pasa el callback al harness y le dice a Leo lo que el harness contestó (no lo que se esperaba).
    Saca el teclado del mensaje para que no se toque dos veces."""
    data = query.data or ""
    try:
        texto = await _hilo(harness().callback, data)
    except Exception as e:
        logger.warning("boton harness %s: %s", data[:20], e)
        await _responder(query, "No pude hacerlo ahora (%s): probá de nuevo." % type(e).__name__, True)
        return
    if texto is None:
        await _responder(query, "Este botón ya no hace nada.", True)
        return
    await _responder(query)
    try:
        await query.edit_message_reply_markup(reply_markup=None)
    except Exception as e:
        logger.debug("boton harness: no pude sacar el teclado: %s", e)
    try:
        await query.message.reply_text(str(texto)[:3500], disable_web_page_preview=True)
    except Exception as e:
        logger.debug("boton harness: no pude contestar: %s", e)


def registrar(app):
    """Registra /estado, /pausa y los botones pz_ (antes del handle_button general)."""
    from telegram.ext import CallbackQueryHandler, CommandHandler
    app.add_handler(CommandHandler("estado", cmd_estado))
    app.add_handler(CommandHandler("pausa", cmd_pausa))
    app.add_handler(CallbackQueryHandler(boton_pausa, pattern="^pz_"))
