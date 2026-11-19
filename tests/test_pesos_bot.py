# -*- coding: utf-8 -*-
"""/pesos y los botones h_pesos:… (panel_bot.py v1.1, 19/11/2026). El bot solo enruta: el texto, el teclado y lo que se guarda
los arma el harness (agents/editor_auto.panel_pesos / callback_pesos). Sin red, sin token, sin Telegram: el harness es de
mentira, salvo la última prueba (integración con una copia del harness al lado, si está)."""
import json
import os
import subprocess
import sys
import textwrap
import types

import pytest

from test_panel_bot import RAIZ_BOT, Consulta, Mensaje, ctx, run, upd

import panel_bot as B  # noqa: E402

PANEL = {"texto": "⚖️ <b>Pesos</b> · prueba", "teclado": {"inline_keyboard": [[{"text": "➕", "callback_data": "h_pesos:palabras:+"}]]}}


@pytest.fixture
def h():
    falso = types.SimpleNamespace(botones=[])
    falso.pesos_panel = lambda: PANEL
    falso.pesos_boton = lambda data: falso.botones.append(data) or dict(PANEL, texto="⚖️ nuevo", aviso="Tu gusto: ×1 → ×1,25")
    B.set_harness(falso)
    yield falso
    B.set_harness(None)


def test_pesos_muestra_el_panel_que_arma_el_harness(h):
    m = Mensaje()
    run(B.cmd_pesos(upd(m), ctx()))
    texto, kw = m.enviados[0]
    assert texto == PANEL["texto"] and kw["parse_mode"] == "HTML" and kw["reply_markup"] == PANEL["teclado"]


def test_un_boton_guarda_en_el_harness_avisa_y_redibuja_el_mismo_mensaje(h):
    q = Consulta("h_pesos:palabras:+")
    run(B.boton_pesos(upd(query=q), None))
    assert h.botones == ["h_pesos:palabras:+"]
    assert q.respuestas == [("Tu gusto: ×1 → ×1,25", False)]
    assert q.ediciones[0][0] == "⚖️ nuevo" and q.ediciones[0][1]["reply_markup"] == PANEL["teclado"]
    assert q.message.enviados == []                                          # no manda un mensaje nuevo: edita el panel


def test_botones_que_nadie_atiende_o_que_fallan_no_rompen(h):
    h.pesos_boton = lambda data: None
    q = Consulta("h_pesos:raro")
    run(B.boton_pesos(upd(query=q), None))
    assert q.respuestas == [("Este botón ya no hace nada.", True)] and q.ediciones == []
    h.pesos_boton = lambda data: (_ for _ in ()).throw(RuntimeError("base bloqueada"))
    q = Consulta("h_pesos:eco:-")
    run(B.boton_pesos(upd(query=q), None))
    assert q.respuestas[0][1] is True and "probá de nuevo" in q.respuestas[0][0] and q.ediciones == []
    h.pesos_panel = lambda: (_ for _ in ()).throw(RuntimeError("boom"))
    m = Mensaje()
    run(B.cmd_pesos(upd(m), ctx()))
    assert "No pude armar el panel" in m.enviados[0][0]


def test_con_un_harness_viejo_avisa_que_falta_desplegar(monkeypatch):
    viejo = types.ModuleType("agents.editor_auto")          # un editor_auto v1.1: sin panel_pesos ni callback_pesos
    paquete = types.ModuleType("agents")
    paquete.editor_auto = viejo
    monkeypatch.setitem(sys.modules, "agents", paquete)
    monkeypatch.setitem(sys.modules, "agents.editor_auto", viejo)
    B.set_harness(B._Harness())
    try:
        m = Mensaje()
        run(B.cmd_pesos(upd(m), ctx()))
        assert m.enviados[0][0] == B._SIN_PESOS
        q = Consulta("h_pesos:palabras:+")
        run(B.boton_pesos(upd(query=q), None))
        assert q.respuestas == [(B._SIN_PESOS, True)] and q.ediciones == []
    finally:
        B.set_harness(None)


def test_pesos_esta_en_el_menu_y_en_la_ayuda():
    import comandos_menu
    assert "pesos" in [c for _g, c, _d in comandos_menu.MENU]
    assert "/pesos" in comandos_menu.texto_ayuda()


def test_main_no_atiende_h_pesos_por_su_cuenta():
    """El único que atiende h_pesos es panel_bot (registrado antes del handle_button general): main.py no lo toca."""
    src = open(os.path.join(RAIZ_BOT, "main.py"), encoding="utf-8").read()
    assert "h_pesos" not in src
    assert src.index("_panel.registrar(app)") < src.index("app.add_handler(CallbackQueryHandler(handle_button))")


# ═══ integración con el harness real (copia al lado) ═════════════════════════════════════════════════════════════════

HARNESS = os.environ.get("HARNESS_DIR_PRUEBA") or os.path.join(os.path.dirname(RAIZ_BOT), "me-harness")


def _tiene_pesos():
    try:
        return "def panel_pesos" in open(os.path.join(HARNESS, "agents", "editor_auto.py"), encoding="utf-8").read()
    except Exception:
        return False


@pytest.mark.skipif(not _tiene_pesos(), reason="no está la copia del harness con editor_auto v1.2 (panel_pesos)")
def test_integracion_con_el_harness_real(tmp_path):
    script = textwrap.dedent("""
        import asyncio, json, sys, types
        sys.path.insert(0, %(bot)r)
        import panel_bot as B
        B.HARNESS_DIR = %(harness)r
        class M:
            def __init__(s): s.enviados = []
            async def reply_text(s, t, **k): s.enviados.append((t, k.get("reply_markup")))
        class Q:
            def __init__(s, d): s.data, s.respuestas, s.ediciones = d, [], []
            async def answer(s, t=None, show_alert=False): s.respuestas.append(t)
            async def edit_message_text(s, t, **k): s.ediciones.append((t, k.get("reply_markup")))
        m = M()
        asyncio.run(B.cmd_pesos(types.SimpleNamespace(message=m), types.SimpleNamespace(args=[])))
        out = {"panel": m.enviados[0][0], "teclado": m.enviados[0][1], "botones": []}
        for d in ("h_pesos:palabras:+", "h_pesos:a_min:+", "h_pesos:fabrica"):
            q = Q(d)
            asyncio.run(B.boton_pesos(types.SimpleNamespace(callback_query=q), None))
            out["botones"].append({"aviso": q.respuestas[0], "texto": q.ediciones[0][0]})
        print(json.dumps(out, ensure_ascii=False))
    """) % {"bot": RAIZ_BOT, "harness": HARNESS}
    env = dict(os.environ, HARNESS_DB=str(tmp_path / "h.db"), OPENAI_API_KEY="x", HARNESS_DIR=HARNESS, PYTHONIOENCODING="utf-8")
    env.pop("EDITOR_AUTO_A_MIN", None)
    open(env["HARNESS_DB"], "wb").close()                     # la base existe (en el VPS siempre); el harness crea la tabla
    p = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True, env=env, cwd=HARNESS, timeout=120,
                       encoding="utf-8")
    assert p.returncode == 0, p.stderr[-2000:]
    out = json.loads(p.stdout.strip().splitlines()[-1])
    assert "Pesos" in out["panel"] and "2.850 tarjetas" in out["panel"] and "🏛️ Política: no sale sola, la decidís vos" in out["panel"]
    datos = [b["callback_data"] for fila in out["teclado"]["inline_keyboard"] for b in fila]
    assert "h_pesos:palabras:+" in datos and "h_pesos:fabrica" in datos and all(len(d.encode()) <= 64 for d in datos)
    gusto, piso, fabrica = out["botones"]
    assert "×1,25" in gusto["aviso"] and "×1,25" in gusto["texto"]
    assert piso["aviso"].endswith("→ 40 %") and "lo fijaste vos" in piso["texto"]
    assert "de fábrica" in fabrica["aviso"].lower() and "×1,25" not in fabrica["texto"]
