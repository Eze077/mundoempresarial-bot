# -*- coding: utf-8 -*-
"""jefe_criterio_bot.py v1.0 · 25/11/2026 — Jefe v3, etapa 5: los criterios del Jefe, el lado del bot.

Los lunes el harness (agents/jefe_criterio.py, bandera JEFE_CRITERIO) le manda a Leo, sin sonido, los criterios que salen de sus
decisiones de la semana, cada uno con ✅ / ❌. Acá se atienden esos botones y el comando para verlos:

  · jcr_ok:<id> / jcr_no:<id>   aprobar o rechazar un criterio propuesto (lo que no se toca en 7 días vence)
  · jcr_ret:<id>                retirar uno vigente desde el mensaje de la semana
  · jcr_rtl:<id>                retirarlo desde la lista de /criterios
  · /criterios                  los vigentes, con «🗑 Retirar»

Todo lo decide el harness (`boton(data)` y `lista_vigentes()`, mismo contrato que jefe_directivas.boton: {ok, texto, botones, toast});
el bot contesta el callback con el aviso corto y repinta el mismo mensaje con el estado nuevo. Con la bandera apagada el harness
contesta «no están activos» y no toca nada. Se registra ANTES del catch-all handle_button; el acceso es el del bot (el filtro global
de admin corre antes que todo). Sin claves ni datos internos: repo público.
"""
import asyncio
import logging
import os
import sys

from telegram import Update
from telegram.ext import CallbackQueryHandler, CommandHandler, ContextTypes

logger = logging.getLogger("jefe_criterio_bot")

HARNESS_DIR = os.environ.get("HARNESS_DIR", "/opt/me-harness")
PATRON = r"^jcr_(ok|no|ret|rtl):\d{1,9}$"
PLAZO_S = 30                   # todo es local (sqlite): si tarda más, algo anda mal
LIMITE_TG = 4000


def partir(texto, n=LIMITE_TG) -> list:
    """Pedazos de hasta `n` caracteres cortando en saltos de línea (los tags HTML del harness no cruzan líneas), como
    agenda_reunion_bot.partir: un corte a lo bruto puede dejar «<i>cura» abierto y Telegram rechaza el mensaje entero."""
    import agenda_reunion_bot as _AB          # el mismo corte que usan las respuestas del Jefe (jefe_directivas_bot)
    return _AB.partir(texto, n)


async def _mandar(message, texto, botones):
    """Uno o más mensajes (los botones van en el último pedazo)."""
    pedazos = partir(texto or "")
    for i, t in enumerate(pedazos):
        await message.reply_text(t, parse_mode="HTML", reply_markup=botones if i == len(pedazos) - 1 else None,
                                 disable_web_page_preview=True)


def cargar():
    """El módulo del harness (inyectable en las pruebas)."""
    if HARNESS_DIR not in sys.path:
        sys.path.insert(0, HARNESS_DIR)
    from agents import jefe_criterio
    return jefe_criterio


async def _contestar(q, toast):
    try:
        await q.answer((toast or "")[:190] or None, show_alert=False)
    except Exception as e:        # clic tardío tras un reinicio («query is too old»): igual se atiende
        logger.debug("jcr answer: %s", e)


def hacer_handler_boton(cargar_fn=None):
    async def handle(update: Update, context: ContextTypes.DEFAULT_TYPE):
        q = update.callback_query
        try:
            mod = (cargar_fn or cargar)()
            r = await asyncio.wait_for(asyncio.to_thread(mod.boton, q.data), PLAZO_S)
        except asyncio.TimeoutError:
            await _contestar(q, "")
            await q.message.reply_text("⏱ Está tardando más de %d s. Si termina, el cambio queda hecho igual." % PLAZO_S)
            return
        except Exception as e:
            logger.exception("botón %s: %s", q.data, e)
            await _contestar(q, "")
            await q.message.reply_text("⚠️ No pude resolver el botón (%s). No toqué nada." % str(e)[:80])
            return
        r = r or {}
        await _contestar(q, r.get("toast"))
        texto = r.get("texto")
        if not r.get("ok") or not texto:
            return
        if len(texto) <= LIMITE_TG:
            try:
                await q.edit_message_text(texto, parse_mode="HTML", reply_markup=r.get("botones"), disable_web_page_preview=True)
                return
            except Exception as e:      # «message is not modified» u otro: se manda aparte
                logger.debug("jcr editar: %s", e)
        await _mandar(q.message, texto, r.get("botones"))
    return handle


def hacer_cmd_criterios(cargar_fn=None):
    async def cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
        m = update.message
        try:
            mod = (cargar_fn or cargar)()
            r = await asyncio.wait_for(asyncio.to_thread(mod.lista_vigentes), PLAZO_S) or {}
        except Exception as e:
            logger.exception("/criterios: %s", e)
            await m.reply_text("⚠️ No pude leer los criterios (%s)." % str(e)[:80])
            return
        if not r.get("ok"):
            await m.reply_text(r.get("toast") or "Los criterios del Jefe no están activos.")
            return
        await _mandar(m, r.get("texto") or "", r.get("botones"))
    return cmd


def registrar(app, cargar_fn=None):
    """Registra /criterios y los botones jcr_. Llamar ANTES del catch-all handle_button."""
    app.add_handler(CommandHandler("criterios", hacer_cmd_criterios(cargar_fn)))
    app.add_handler(CallbackQueryHandler(hacer_handler_boton(cargar_fn), pattern=PATRON))
    logger.info("criterios del Jefe registrados (jcr_, /criterios)")
