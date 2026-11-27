# -*- coding: utf-8 -*-
"""Link de Leo directo, sin la tarjeta — el lado del bot (decisión de Leo, 16/11/2026).

Antes, un link externo que Leo pegaba llegaba como TARJETA y esperaba su segundo clic en «Publicar»; las que no tocó las
rechazó el siguiente «Liberar»/«Proponer» sin que nadie decidiera (4 de 87 links en 14 días). Ahora el link queda aprobado al
crearse y Leo recibe un aviso corto con UN botón «⛔ Frenar». Toda la lógica de negocio vive en el harness
(`agents/curador.py`: tomar_directo, aviso_directo, frenar_directo); este módulo solo la llama y manda el mensaje.

Bandera `LINK_LEO_DIRECTO` (en /etc/mundoempresarial-bot.env + reinicio del bot): por defecto prendida; `0`, `false`, `off`, `no`
la apagan y vuelve la tarjeta de siempre. Si el harness todavía no tiene `tomar_directo` (el bot se actualiza solo y el
harness se baja a mano) tampoco pasa nada: se manda la tarjeta de siempre.

Lo que NO cambia: notas escritas a mano o PDF, tweets, YouTube/Instagram y eventos (cada uno tiene su camino).

«⛔ Frenar» de una nota que YA SALIÓ (27/11/2026, Leo: «quiero que el jefe se ocupe de derivar las gestiones a quien
corresponda»): en vez de «usá /editar o /borrar», el Jefe del harness (`agents/jefe_directivas.derivar_retiro`) se la deriva al
subagente de notas publicadas y le manda a Leo, en un mensaje aparte y suyo, «📝 Pasar a borrador» / «🗑 A la papelera» (1 clic,
nunca un borrado definitivo). Con el Jefe apagado (JEFE_DIRECTIVAS=0) o un harness que no lo tiene, el aviso es el de antes.
Los comandos /editar y /borrar siguen como estaban.
Sin claves ni datos personales: el repo del bot es público.
"""
import asyncio
import html
import inspect
import logging
import os
import sys
import threading

logger = logging.getLogger("link_directo_bot")

HARNESS_DIR = os.environ.get("HARNESS_DIR", "/opt/me-harness")
PLAZO_S = 50                     # tope de espera de tomar_directo (corre la cola con un modelo); pasado, se mira en qué quedó
PLAZO_JEFE_S = 60                # tope de espera del Jefe para armar el «sacarla de la web» (lee la nota en WordPress); pasado,
                                 # su mensaje llega igual, aparte
_TAREAS = set()                  # las respuestas tardías (si no, el recolector puede matarlas)
_APAGADO = {"0", "false", "no", "off", "apagado", "n"}


def activo() -> bool:
    """True salvo que LINK_LEO_DIRECTO esté en 0/false/no/off. Se lee en el momento de usar. Nunca lanza."""
    try:
        v = os.environ.get("LINK_LEO_DIRECTO")
        if v is None or not v.strip():
            return True
        return v.strip().lower() not in _APAGADO
    except Exception:
        return True


class _Harness:
    def __init__(self):
        if HARNESS_DIR not in sys.path:
            sys.path.insert(0, HARNESS_DIR)
        import broker
        from agents import curador
        self.broker, self.curador = broker, curador


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


def _jefe(h):
    """El Jefe del harness (agents/jefe_directivas), o None si este harness no lo tiene. Nunca lanza."""
    j = getattr(h, "jefe", None)
    if j is not None or not isinstance(h, _Harness):
        return j
    try:
        from agents import jefe_directivas
        h.jefe = jefe_directivas
        return jefe_directivas
    except Exception as e:
        logger.warning("frenar: no pude cargar el Jefe del harness: %s", e)
        return None


async def _mandar_jefe(query, der) -> None:
    """El mensaje del Jefe (con sus botones), aparte y registrado como suyo: un reply le sigue hablando al Jefe."""
    msg = getattr(query, "message", None)
    if msg is None:
        return
    sent = await msg.reply_text(der.get("texto") or "", parse_mode="HTML", reply_markup=der.get("botones"), disable_web_page_preview=True)
    try:
        j = _jefe(harness())
        if j is not None and getattr(sent, "message_id", None):
            j.registrar_mensaje(sent.message_id, der.get("directiva_id"))
    except Exception as e:
        logger.debug("frenar: registrar el mensaje del Jefe: %s", e)


async def _retiro(query, job_id) -> dict:
    """Le pide al Jefe que derive el retiro de la web (en un hilo y con tope). Si tarda, el mensaje del Jefe llega igual, tarde."""
    j = _jefe(harness())
    if j is None or not hasattr(j, "derivar_retiro"):
        return {"ok": False, "motivo": "sin_jefe"}
    fut = asyncio.ensure_future(asyncio.to_thread(j.derivar_retiro, job_id))
    try:
        return await asyncio.wait_for(asyncio.shield(fut), PLAZO_JEFE_S) or {}
    except asyncio.TimeoutError:
        async def tarde():
            try:
                der = await fut
                if (der or {}).get("ok"):
                    await _mandar_jefe(query, der)
            except Exception as e:
                logger.warning("frenar #%s: respuesta tardía del Jefe: %s", job_id, e)
        t = asyncio.ensure_future(tarde())
        _TAREAS.add(t)
        t.add_done_callback(_TAREAS.discard)
        return {"ok": False, "motivo": "tarde"}
    except Exception as e:
        logger.warning("frenar #%s: el Jefe no pudo armar el retiro: %s", job_id, e)
        return {"ok": False, "motivo": str(e)[:80]}


def puede_directo() -> bool:
    """True solo si el directo va a correr de verdad: bandera prendida Y el harness ya tiene `tomar_directo` (el bot se actualiza
    solo y el harness se baja a mano). Lo usa el aviso de posible duplicado para no prometer un «⛔ Frenar» que no va a llegar."""
    try:
        return activo() and getattr(harness().curador, "tomar_directo", None) is not None
    except Exception:
        return False


async def _tomar(fn, job_id, cancelar):
    """Corre `tomar_directo` en un hilo. Le pasa `cancelar` si el harness lo entiende (uno más viejo no lo trae)."""
    try:
        con_cancelar = "cancelar" in inspect.signature(fn).parameters
    except (TypeError, ValueError):
        con_cancelar = False
    if con_cancelar:
        return await asyncio.wait_for(asyncio.to_thread(fn, job_id, True, cancelar), PLAZO_S)
    return await asyncio.wait_for(asyncio.to_thread(fn, job_id), PLAZO_S)


async def resolver(message, job_id) -> bool:
    """Un link externo de Leo ya quedó creado como job en 'curado'. True = quedó tomado y ya se le avisó (el llamador NO
    manda la tarjeta); False = el llamador manda la tarjeta de siempre (`run_briefing_single`)."""
    if not activo():
        return False
    try:
        h = harness()
        fn = getattr(h.curador, "tomar_directo", None)
        if fn is None:                              # harness viejo todavía sin el cambio
            logger.info("link de Leo #%s: el harness no tiene tomar_directo, va la tarjeta", job_id)
            return False
        cancelar = threading.Event()
        try:
            res = await _tomar(fn, job_id, cancelar)
        except asyncio.TimeoutError:
            # se pasó del plazo: el hilo sigue corriendo. Se le avisa que se cancele ANTES de mirar en qué quedó el job (si
            # todavía no aprobó, no va a aprobar después de que mandemos la tarjeta). Si ya salió de 'curado' se aprobó.
            cancelar.set()
            j = await asyncio.to_thread(h.broker.get_job, job_id) or {}
            if not j or j.get("stage") == "curado":
                return False
            res = {"ok": True, "modo": "directo", "job_id": job_id, "titulo": j.get("title") or "", "capa": j.get("hilo"),
                   "destino": j.get("pub_dest") or None, "etapa": j.get("stage"), "agenda": False}
    except Exception as e:
        logger.warning("link de Leo #%s: tomar_directo falló (%s: %s), va la tarjeta", job_id, type(e).__name__, str(e)[:160])
        return False

    if res.get("modo") not in ("directo", "en_camino"):
        if res.get("motivo") == "ya_publicada":
            try:
                await message.reply_text(
                    "ℹ️ Esa fuente ya salió publicada antes. No la publico sola (reescribiría la nota): "
                    "te dejo la tarjeta por si querés actualizarla.")
            except Exception:
                pass
        return False

    try:
        texto, kb = h.curador.aviso_directo(res)
        await message.reply_text(texto, parse_mode="HTML", reply_markup=kb, disable_web_page_preview=True)
    except Exception as e:                          # el job ya está aprobado: avisar mal no puede mandar otra tarjeta
        logger.warning("link de Leo #%s: tomado, pero no pude mandar el aviso: %s", job_id, e)
    # si el link ya estaba en un briefing, esa tarjeta quedó vieja (sus botones ahora dirían «ya no está en curado»)
    mid = res.get("card_msg_id")
    if mid:
        try:
            await message.get_bot().delete_message(chat_id=message.chat_id, message_id=mid)
        except Exception:
            pass
    return True


async def _responder(query, texto=None, alerta=False):
    """query.answer sin explotar: main.py ya puede haber respondido el callback (Telegram solo deja una vez)."""
    try:
        await query.answer(texto, show_alert=alerta)
    except Exception as e:
        logger.debug("frenar: answer: %s", e)


async def frenar(query, job_id) -> None:
    """Botón «⛔ Frenar» (callback `h_cur_frenar:<id>`): rechaza el job si todavía no se publicó; si ya salió, dice cómo
    corregirla o sacarla. Lo que le dice a Leo es lo que `frenar_directo` devolvió, no lo que se esperaba."""
    try:
        h = harness()
        res = await asyncio.to_thread(h.curador.frenar_directo, job_id)
    except Exception as e:
        logger.warning("frenar #%s: %s", job_id, e)
        await _responder(query, "No pude frenarla ahora: probá de nuevo.", True)
        return
    t = html.escape((res.get("titulo") or "esa nota")[:120], quote=False)
    estado = res.get("estado")
    if estado in ("frenada", "ya_frenada"):
        await _responder(query, "⛔ Frenada")
        texto = "⛔ <b>Frenada:</b> «%s». No se publica." % t
        if estado == "frenada" and res.get("etapa") == "publicacion":
            texto += "\n<i>Estaba en publicación: si justo se había subido a WordPress, la paso a borrador y no sale a redes.</i>"
    elif estado == "publicada":
        url = res.get("wp_url") or ""
        enlace = "\n" + html.escape(url, quote=False) if url else ""
        der = await _retiro(query, job_id)
        if der.get("ok") or der.get("motivo") == "tarde":
            await _responder(query, "Ya se publicó: te pregunto abajo si la saco de la web.")
            texto = "✅ <b>Ya se publicó:</b> «%s»%s\nNo se puede frenar: %s" % (
                t, enlace, "abajo te pregunto si la saco de la web." if der.get("ok") else
                "estoy armando cómo sacarla de la web; te lo mando abajo cuando termine.")
            try:
                await query.edit_message_text(texto, parse_mode="HTML", disable_web_page_preview=True)
            except Exception as e:
                logger.debug("frenar #%s: no pude editar el aviso: %s", job_id, e)
            if der.get("ok"):
                try:
                    await _mandar_jefe(query, der)
                except Exception as e:
                    logger.warning("frenar #%s: no pude mandar el mensaje del Jefe: %s", job_id, e)
            return
        if der.get("motivo") in ("bandera", "sin_jefe"):      # el Jefe apagado (o un harness sin él): el aviso de antes
            await _responder(query, "Ya se publicó: usá /editar o /borrar.", True)
            texto = "✅ <b>Ya se publicó:</b> «%s»%s\nPara corregirla usá /editar; para sacarla, /borrar." % (t, enlace)
        else:
            await _responder(query, "Ya se publicó: no pude armar cómo sacarla de la web.", True)
            texto = ("✅ <b>Ya se publicó:</b> «%s»%s\nNo se puede frenar y no pude armar cómo sacarla de la web (%s): decime "
                     "«Jefe, sacá esta nota de la web» y lo hago." % (t, enlace, html.escape(der.get("motivo") or "sin detalle", quote=False)))
    elif estado == "no_existe":
        await _responder(query, "No encontré esa nota.", True)
        return
    else:      # 'error': no se pudo frenar y no se sabe por qué; no decir lo que no es cierto
        await _responder(query, "No pude frenarla (falló): tocá ⛔ Frenar de nuevo.", True)
        return
    try:
        await query.edit_message_text(texto, parse_mode="HTML", disable_web_page_preview=True)   # sin teclado: el botón se va
    except Exception as e:
        logger.debug("frenar #%s: no pude editar el aviso: %s", job_id, e)
