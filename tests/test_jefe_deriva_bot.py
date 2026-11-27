# -*- coding: utf-8 -*-
"""Jefe 27/11/2026, deriva — el lado del bot. Leo: «quiero que el jefe se ocupe de derivar las gestiones a quien corresponda […]
además de que queden los comandos para estudiar a futuro desactivarlos».
  · los links de X, YouTube e Instagram que el Jefe deriva (`derivar_bot`) se corren por el camino del link pegado solo
    (handle_link), como si Leo los hubiera pegado solos: el Jefe ya no le dice «ese mandalo solo»;
  · la respuesta a un botón de los links de la reunión de agenda va aparte (no pisa el plan);
  · el «⛔ Frenar» de una nota que ya salió: el Jefe deriva el «sacarla de la web» y manda «📝 Pasar a borrador» / «🗑 A la
    papelera» en un mensaje suyo, en vez de «usá /editar o /borrar» (con el Jefe apagado, el aviso de antes);
  · los comandos /editar y /borrar siguen registrados.
Sin red, sin token: el harness es de mentira."""
import asyncio
import os
import sys
import time
import types

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import agenda_reunion_bot as AB  # noqa: E402
import jefe_directivas_bot as B  # noqa: E402
import link_directo_bot as L  # noqa: E402
from test_jefe_directivas_bot import FakeDir, Msg, Upd, Ctx, Query, run  # noqa: E402

TWEET = "https://x.com/caputoluis/status/1234567890"


@pytest.fixture()
def h(monkeypatch):
    monkeypatch.delenv("JEFE_DIRECTIVAS", raising=False)
    monkeypatch.delenv("AGENDA_REUNION", raising=False)
    fh = types.SimpleNamespace(dir=FakeDir())
    B.set_harness(fh)
    AB.set_harness(None)
    yield fh
    B.set_harness(None)
    AB.set_harness(None)


def _link():
    corridos = []

    async def handle_link(update, context):
        corridos.append(update.message.text)
        await update.message.reply_text("🐦 Leyendo el tweet…")
    return handle_link, corridos


# ══ links de X, YouTube e Instagram: al camino del link pegado solo ═══════════════════════════════════════════════════

def test_el_link_que_deriva_el_jefe_se_corre_por_handle_link_con_solo_el_link(h):
    h.dir.resp = dict(h.dir.resp, derivar_bot=[TWEET])
    link, corridos = _link()
    m = Msg("Jefe, hacé una nota con este tweet " + TWEET)
    run(B.hacer_handler_texto(link)(Upd(m), Ctx()))
    assert corridos == [TWEET]                                         # el camino del link pegado solo, con SOLO el link
    assert m.respuestas[0][0] == "<b>Entendí</b>" and m.respuestas[1][0] == "🐦 Leyendo el tweet…"   # primero el Jefe, después el camino


def test_sin_derivar_no_se_corre_nada(h):
    link, corridos = _link()
    run(B.hacer_handler_texto(link)(Upd(Msg("Jefe, hoy nada de Milei")), Ctx()))
    assert corridos == []


def test_en_medio_de_otro_flujo_el_link_queda_pendiente_y_se_dice(h):
    link, corridos = _link()
    m = Msg("x")
    run(B.derivar_links(m, Upd(m), Ctx({"awaiting_tweet_directiva": True}), [TWEET], link))
    assert corridos == [] and "quedó pendiente" in m.respuestas[0][0]


def test_sin_el_camino_a_mano_lo_dice_claro(h):
    m = Msg("x")
    run(B.derivar_links(m, Upd(m), Ctx(), [TWEET], None))
    assert "queda pendiente" in m.respuestas[0][0] and "mandalo solo" not in m.respuestas[0][0]


def test_si_el_camino_falla_se_dice_y_no_rompe(h):
    async def roto(update, context):
        raise RuntimeError("X caído")
    m = Msg("x")
    run(B.derivar_links(m, Upd(m), Ctx(), [TWEET], roto))
    assert "No pude pasar" in m.respuestas[0][0] and "X caído" in m.respuestas[0][0]


def test_el_boton_que_deriva_un_link_lo_corre_despues_de_editar(h):
    h.dir.boton_resp = dict(h.dir.boton_resp, derivar_bot=[TWEET])
    link, corridos = _link()
    q = Query("jfd_si:7:0:0")
    run(B.hacer_handler_boton(link)(Upd(query=q), Ctx()))
    assert q.editado and corridos == [TWEET]


def test_el_boton_de_la_reunion_contesta_aparte_y_no_pisa_el_plan(h):
    h.dir.boton_resp = dict(h.dir.boton_resp, aparte=True, directiva_id=9)
    q = Query("jfd_si:9:0:0")
    run(B.hacer_handler_boton()(Upd(query=q), Ctx()))
    assert q.editado is None and q.message.respuestas[0][0] == "<b>Entendí</b> listo" and h.dir.registrados


def test_un_boton_lento_que_deriva_un_link_lo_corre_cuando_termina(h, monkeypatch):
    monkeypatch.setattr(B, "PLAZO_BOTON_S", 0.05)
    h.dir.boton_resp = dict(h.dir.boton_resp, derivar_bot=[TWEET], directiva_id=7)
    h.dir.boton = lambda data, **k: time.sleep(0.3) or h.dir.boton_resp
    link, corridos = _link()
    q = Query("jfd_si:7:0:0")

    async def todo():
        await B.hacer_handler_boton(link)(Upd(query=q), Ctx())
        await asyncio.gather(*list(AB._TAREAS))
    run(todo())
    assert corridos == [TWEET]


def test_registrar_le_pasa_handle_link_a_los_botones_y_los_comandos_siguen():
    src = open(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "main.py"), encoding="utf-8").read()
    assert 'CommandHandler("editar", cmd_editar)' in src and 'CommandHandler("borrar", cmd_borrar)' in src   # los comandos quedan
    assert "fallback_texto=handle_link" in src


# ══ ⛔ Frenar de una nota que ya salió (link directo) ═══════════════════════════════════════════════════════════════

class Consulta:
    def __init__(self):
        self.respuestas, self.ediciones = [], []
        self.message = Msg("aviso")

    async def answer(self, texto=None, show_alert=False):
        self.respuestas.append((texto, show_alert))

    async def edit_message_text(self, texto, **kw):
        self.ediciones.append((texto, kw))


RETIRO = {"ok": True, "directiva_id": 31, "texto": "🧑‍💼 <b>Jefe</b> · ¿La saco de la web?",
          "botones": {"inline_keyboard": [[{"text": "📝 Pasar a borrador", "callback_data": "jfd_pr:31:0:0"}],
                                          [{"text": "🗑 A la papelera", "callback_data": "jfd_pr:31:0:1"}]]}}


def _harness(retiro=None, lento=0.0, modo="1"):
    registrados = []

    def derivar(jid):
        if lento:
            time.sleep(lento)
        return retiro
    jefe = types.SimpleNamespace(derivar_retiro=derivar, registrar_mensaje=lambda mid, did=None: registrados.append((mid, did)),
                                 modo=lambda: modo)
    cur = types.SimpleNamespace(frenar_directo=lambda jid: {"ok": False, "estado": "publicada", "titulo": "Nota", "wp_url": "https://mundoempresarial.ar/nota"})
    return types.SimpleNamespace(curador=cur, broker=None, jefe=jefe), registrados


@pytest.fixture(autouse=True)
def _limpio():
    L.set_harness(None)
    yield
    L.set_harness(None)


def test_frenar_una_que_ya_salio_manda_el_retiro_del_jefe_sin_comandos():
    hh, registrados = _harness(RETIRO)
    L.set_harness(hh)
    q = Consulta()
    run(L.frenar(q, 5))
    assert q.respuestas == [("Ya se publicó: te pregunto abajo si la saco de la web.", False)]
    aviso = q.ediciones[0][0]
    assert "Ya se publicó" in aviso and "/editar" not in aviso and "/borrar" not in aviso and "https://mundoempresarial.ar/nota" in aviso
    texto, kw = q.message.respuestas[0]
    assert texto == RETIRO["texto"] and kw["reply_markup"] == RETIRO["botones"] and kw["parse_mode"] == "HTML"
    assert registrados and registrados[0][1] == 31                     # un reply le sigue hablando al Jefe


def test_frenar_con_el_jefe_apagado_es_el_aviso_de_antes():
    hh, _ = _harness({"ok": False, "motivo": "bandera"}, modo="0")
    L.set_harness(hh)
    q = Consulta()
    run(L.frenar(q, 5))
    assert "/editar" in q.ediciones[0][0] and q.message.respuestas == [] and q.respuestas == [("Ya se publicó: usá /editar o /borrar.", True)]


def test_frenar_contesta_el_callback_antes_de_esperar_al_jefe():
    orden = []
    hh, _ = _harness(RETIRO)
    jefe = hh.jefe
    jefe.derivar_retiro = lambda jid: orden.append("jefe") or RETIRO
    L.set_harness(hh)

    class Q(Consulta):
        async def answer(self, texto=None, show_alert=False):
            orden.append("answer")
            await super().answer(texto, show_alert)
    q = Q()
    run(L.frenar(q, 5))
    assert orden[:2] == ["answer", "jefe"] and len(q.respuestas) == 1


def test_frenar_dos_veces_mientras_lo_arma_no_arma_otra():
    hh, _ = _harness(RETIRO, lento=0.3)
    L.set_harness(hh)
    q1, q2 = Consulta(), Consulta()

    async def todo():
        t = asyncio.ensure_future(L.frenar(q1, 6))
        await asyncio.sleep(0.05)
        await L.frenar(q2, 6)
        await t
    run(todo())
    assert q2.respuestas == [("Ya lo estoy armando: te lo mando abajo.", False)] and q2.ediciones == []
    assert len(q1.message.respuestas) == 1 and 6 not in L._EN_CURSO


def test_frenar_si_la_respuesta_tardia_falla_lo_avisa(monkeypatch):
    monkeypatch.setattr(L, "PLAZO_JEFE_S", 0.05)
    hh, _ = _harness({"ok": False, "motivo": "WordPress no contestó"}, lento=0.3)
    L.set_harness(hh)
    q = Consulta()

    async def todo():
        await L.frenar(q, 7)
        await asyncio.gather(*list(L._TAREAS))
    run(todo())
    assert "No pude armar cómo sacarla de la web (WordPress no contestó)" in q.message.respuestas[0][0] and 7 not in L._EN_CURSO


def test_frenar_si_el_jefe_falla_lo_dice_sin_comandos():
    hh, _ = _harness({"ok": False, "motivo": "WordPress no contestó"})
    L.set_harness(hh)
    q = Consulta()
    run(L.frenar(q, 5))
    aviso = q.ediciones[0][0]
    assert "no pude armar cómo sacarla de la web (WordPress no contestó)" in aviso and "/editar" not in aviso and "/borrar" not in aviso


def test_frenar_si_el_jefe_tarda_avisa_y_el_retiro_llega_igual(monkeypatch):
    monkeypatch.setattr(L, "PLAZO_JEFE_S", 0.05)
    hh, registrados = _harness(RETIRO, lento=0.3)
    L.set_harness(hh)
    q = Consulta()

    async def todo():
        await L.frenar(q, 5)
        assert "te lo mando abajo cuando termine" in q.ediciones[0][0] and q.message.respuestas == []
        await asyncio.gather(*list(L._TAREAS))
    run(todo())
    assert q.message.respuestas[0][0] == RETIRO["texto"] and registrados


def test_frenar_con_error_pide_tocar_de_nuevo_sin_comandos():
    cur = types.SimpleNamespace(frenar_directo=lambda jid: {"ok": False, "estado": "error", "titulo": "Nota"})
    L.set_harness(types.SimpleNamespace(curador=cur, broker=None))
    q = Consulta()
    run(L.frenar(q, 5))
    assert q.respuestas == [("No pude frenarla (falló): tocá ⛔ Frenar de nuevo.", True)]
