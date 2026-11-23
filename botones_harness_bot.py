# -*- coding: utf-8 -*-
"""botones_harness_bot.py v1.2 · 23/11/2026 — botones del harness que el bot no atendía.

Leo (20/11): «1 2 3 si» (los arreglos del bot del plan v2.0). Sin estos handlers los botones no hacían nada:
  · fpd_si:<id>:<rev> / fpd_no:<id>:<rev>  versión retenida de una ficha (harness agents/ficha_pendiente.boton, fase 5,
                               FICHA_REGIMEN): publicarla tal cual o descartarla. (v1.2, 23/11) El botón lleva la revisión de la
                               versión: el harness contesta «ya fue reemplazada» si no es la última. Los de antes, sin `:<rev>`,
                               se siguen atendiendo (el harness los toma como revisión 1). Este patrón va desplegado ANTES que el
                               harness que manda `:<rev>`: si no, esos botones caen al handle_button.
  · pfc_si:<id> / pfc_no:<id>  cuenta «dudosa» de un perfil (harness agents/perfiles_botones.boton, fase 5b): confirmarla o
                               rechazarla.
  · h_coti_salto:<activo>:<AAAAMMDD>  (v1.1, 21/11) «✅ Aceptar como movimiento real» del aviso de un salto de cotización que
                               llegó dos días y NO se publica solo (harness agents/cotizaciones._pedir_aceptar_salto, fase 5c,
                               bandera COTI_BOTON_SALTO). Va a agents/cotizaciones.aceptar_salto(activo, dia). Ese módulo no
                               tiene boton(data): el texto lo arma acá `aceptar_salto` con lo que devuelve el harness
                               ({ok, aceptadas, valor} o {ok: False, motivo}). Antes caía al final de handle_button y pisaba el
                               aviso con «Error: no hay nota pendiente.».
Los de ficha y perfil devuelven {ok, texto, toast} (mismo contrato que jefe_directivas.boton). El texto se suma al mensaje del
botón; si no entra (o no se puede editar), va aparte. Se registran ANTES del catch-all handle_button. El acceso es el del bot: el
filtro global de admin (`_solo_admin`, group=-1) corre antes que todo.

El «⛔ Frenar» del boletín v2 (h_bv2_…) NO va acá: lo despacha panel_bot.boton_harness desde handle_button desde el 18/11
(→ agents/boletin.callback).
"""
import asyncio
import html
import importlib
import logging
import os
import sys

from telegram import Update
from telegram.ext import CallbackQueryHandler, ContextTypes

logger = logging.getLogger(__name__)

HARNESS_DIR = os.environ.get("HARNESS_DIR", "/opt/me-harness")
COTI = "h_coti_salto:"
MODULOS = {"fpd_": "ficha_pendiente", "pfc_": "perfiles_botones", COTI: "cotizaciones"}
# publicar una ficha relee y escribe WordPress; confirmar una cuenta y aceptar un salto son locales (sqlite)
PLAZO_S = {"fpd_": 90, "pfc_": 60, COTI: 30}
PATRONES = {"fpd_": r"^fpd_(si|no):\d+(?::\d+)?$",
            "pfc_": r"^pfc_(si|no):\d+$",
            COTI: r"^h_coti_salto:[a-z0-9_]{1,20}:\d{8}$"}     # el activo es una clave de cotizaciones.ACTIVOS
LIMITE_TG = 4000


def cargar(nombre):
    """El módulo del harness (inyectable en las pruebas)."""
    if HARNESS_DIR not in sys.path:
        sys.path.insert(0, HARNESS_DIR)
    return importlib.import_module("agents." + nombre)


def aceptar_salto(mod, data):
    """h_coti_salto:<activo>:<AAAAMMDD> → cotizaciones.aceptar_salto(activo, dia) → {ok, texto} para Leo."""
    _, activo, dia = data.split(":")
    r = mod.aceptar_salto(activo, dia) or {}
    try:
        nombre = mod.ACTIVOS[activo]["nombre"]
    except Exception:
        nombre = activo
    nombre = html.escape(str(nombre), quote=False)
    fecha = "%s/%s" % (dia[6:8], dia[4:6])
    if r.get("ok"):
        try:
            valor = mod.fmt_valor({"activo": activo, "valor": r["valor"]})
        except Exception:
            valor = str(r.get("valor", "?"))
        n = int(r.get("aceptadas") or 0)
        return {"ok": True, "texto": "✅ <b>Aceptado como movimiento real</b>: %s %s del %s (%d lectura%s). Ya cuenta como dato "
                                     "bueno: el tablero, el resumen y las fichas lo toman la próxima vez que se armen."
                                     % (nombre, html.escape(valor, quote=False), fecha, n, "" if n == 1 else "s")}
    motivo = str(r.get("motivo") or "no quedó ninguna lectura aceptada")
    extra = " Si ya lo habías aceptado, no hay nada más que hacer." if motivo.startswith("no hay un salto rechazado") else ""
    return {"ok": False, "texto": "⚠️ No acepté el salto del %s (%s): %s. No cambié nada.%s"
                                  % (nombre, fecha, html.escape(motivo, quote=False), extra)}


LLAMAR = {COTI: aceptar_salto}          # los que no tienen boton(data) en el harness


def _llamar(prefijo, mod, data):
    fn = LLAMAR.get(prefijo)
    return fn(mod, data) if fn else mod.boton(data)


def hacer_handler(prefijo, cargar_fn=None):
    async def handle(update: Update, context: ContextTypes.DEFAULT_TYPE):
        q = update.callback_query
        try:
            await q.answer()
        except Exception as e:      # clic tardío tras un reinicio («query is too old»): igual se atiende (revisión 21/11)
            logger.debug("botón %s: answer: %s", q.data, e)
        try:
            mod = (cargar_fn or cargar)(MODULOS[prefijo])
            r = await asyncio.wait_for(asyncio.to_thread(_llamar, prefijo, mod, q.data), PLAZO_S[prefijo])
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
        # si el harness dijo que NO se hizo (ok False), el botón queda para reintentar; si se hizo, se saca (un solo uso)
        teclado = getattr(q.message, "reply_markup", None) if (r or {}).get("ok") is False else None
        if len(nuevo) <= LIMITE_TG:
            try:
                await q.edit_message_text(nuevo, parse_mode="HTML", disable_web_page_preview=True, reply_markup=teclado)
                return
            except Exception as e:
                logger.debug("botón %s: no pude editar: %s", q.data, e)
        await q.message.reply_text(texto[:LIMITE_TG], parse_mode="HTML", disable_web_page_preview=True)
    return handle


def registrar(app):
    for prefijo, patron in PATRONES.items():
        app.add_handler(CallbackQueryHandler(hacer_handler(prefijo), pattern=patron))
