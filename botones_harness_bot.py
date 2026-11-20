# -*- coding: utf-8 -*-
"""botones_harness_bot.py v1.0 · 20/11/2026 — botones del harness que el bot no atendía.

Leo (20/11): «1 2 3 si» (los arreglos del bot del plan v2.0). Sin estos handlers los botones no hacían nada:
  · fpd_si:<id> / fpd_no:<id>  versión retenida de una ficha (harness agents/ficha_pendiente.boton, fase 5, FICHA_REGIMEN):
                               publicarla tal cual o descartarla.
  · pfc_si:<id> / pfc_no:<id>  cuenta «dudosa» de un perfil (harness agents/perfiles_botones.boton, fase 5b): confirmarla o
                               rechazarla.
Los dos devuelven {ok, texto, toast} (mismo contrato que jefe_directivas.boton). El texto se suma al mensaje del botón; si no
entra (o no se puede editar), va aparte. Se registran ANTES del catch-all handle_button. El acceso es el del bot: el filtro
global de admin (`_solo_admin`, group=-1) corre antes que todo.
"""
import asyncio
import importlib
import logging
import os
import sys

from telegram import Update
from telegram.ext import CallbackQueryHandler, ContextTypes

logger = logging.getLogger(__name__)

HARNESS_DIR = os.environ.get("HARNESS_DIR", "/opt/me-harness")
MODULOS = {"fpd_": "ficha_pendiente", "pfc_": "perfiles_botones"}
PLAZO_S = {"fpd_": 90, "pfc_": 60}          # publicar una ficha relee y escribe WordPress; confirmar una cuenta es local
LIMITE_TG = 4000


def cargar(nombre):
    """El módulo del harness (inyectable en las pruebas)."""
    if HARNESS_DIR not in sys.path:
        sys.path.insert(0, HARNESS_DIR)
    return importlib.import_module("agents." + nombre)


def hacer_handler(prefijo, cargar_fn=None):
    async def handle(update: Update, context: ContextTypes.DEFAULT_TYPE):
        q = update.callback_query
        await q.answer()
        try:
            mod = (cargar_fn or cargar)(MODULOS[prefijo])
            r = await asyncio.wait_for(asyncio.to_thread(mod.boton, q.data), PLAZO_S[prefijo])
        except asyncio.TimeoutError:
            await q.message.reply_text("⏱ Está tardando más de %d s. Si termina, el cambio queda hecho igual." % PLAZO_S[prefijo])
            return
        except Exception as e:
            logger.exception("botón %s: %s", q.data, e)
            await q.message.reply_text("⚠️ No pude resolver el botón (%s). No toqué nada." % str(e)[:80])
            return
        texto = (r or {}).get("texto") or (r or {}).get("toast") or ""
        if not texto:
            return
        nuevo = ((q.message.text_html or "") + "\n\n" + texto).strip()
        if len(nuevo) <= LIMITE_TG:
            try:
                await q.edit_message_text(nuevo, parse_mode="HTML", disable_web_page_preview=True)   # sin teclado: un solo uso
                return
            except Exception as e:
                logger.debug("botón %s: no pude editar: %s", q.data, e)
        await q.message.reply_text(texto[:LIMITE_TG], parse_mode="HTML", disable_web_page_preview=True)
    return handle


def registrar(app):
    for prefijo in MODULOS:
        app.add_handler(CallbackQueryHandler(hacer_handler(prefijo), pattern=r"^%s(si|no):\d+$" % prefijo))
