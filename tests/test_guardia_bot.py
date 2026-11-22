# -*- coding: utf-8 -*-
"""Botones h_gd_ de la Guardia (panel_bot.boton_guardia → agents/guardia.callback del harness, 22/11/2026). Sin red, sin token,
sin Telegram: el harness y los mensajes son de mentira, salvo la última prueba (integración con la copia del harness de la rama
guardia, si está al lado).

Lo central: el bot solo despacha y le dice a Leo lo que contestó el harness; un aviso (▶️ ✅ 🔕) es UNA decisión y se le saca
todo el teclado; en el parte diario se saca solo la fila de la mejora tocada, así Leo sigue con las otras."""
import asyncio
import os
import subprocess
import sys
import textwrap
import types

import pytest

RAIZ_BOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, RAIZ_BOT)

import panel_bot as B  # noqa: E402
from telegram import InlineKeyboardButton, InlineKeyboardMarkup  # noqa: E402


def run(coro):
    return asyncio.run(coro)


class Mensaje:
    def __init__(self, reply_markup=None):
        self.enviados = []
        self.reply_markup = reply_markup

    async def reply_text(self, texto, **kw):
        self.enviados.append((texto, kw))


class Consulta:
    def __init__(self, data, reply_markup=None):
        self.data = data
        self.respuestas, self.teclados = [], []
        self.message = Mensaje(reply_markup)

    async def answer(self, texto=None, show_alert=False):
        self.respuestas.append((texto, show_alert))

    async def edit_message_reply_markup(self, reply_markup=None):
        self.teclados.append(reply_markup)


@pytest.fixture
def h():
    falso = types.SimpleNamespace(despachos=[])
    falso.callback = lambda data: falso.despachos.append(data) or ("✅ Listo (harness)" if data.startswith("h_gd_") else None)
    B.set_harness(falso)
    yield falso
    B.set_harness(None)


def _parte(*ids):
    return InlineKeyboardMarkup([[InlineKeyboardButton("✅ #%d" % i, callback_data="h_gd_ap:%d" % i),
                                  InlineKeyboardButton("❌ #%d" % i, callback_data="h_gd_no:%d" % i),
                                  InlineKeyboardButton("📦 #%d" % i, callback_data="h_gd_pa:%d" % i)] for i in ids])


@pytest.mark.parametrize("data", ["h_gd_run:3", "h_gd_ok:3", "h_gd_mute:3"])
def test_un_aviso_es_una_decision_se_saca_todo_el_teclado(h, data):
    aviso = InlineKeyboardMarkup([[InlineKeyboardButton("▶️ Correrlo ahora", callback_data="h_gd_run:3")],
                                  [InlineKeyboardButton("✅ Dejarlo así", callback_data="h_gd_ok:3"),
                                   InlineKeyboardButton("🔕 No avisar más", callback_data="h_gd_mute:3")]])
    q = Consulta(data, aviso)
    run(B.boton_guardia(q))
    assert h.despachos == [data] and q.teclados == [None]
    assert q.message.enviados[0][0] == "✅ Listo (harness)"


def test_en_el_parte_se_saca_solo_la_fila_de_la_mejora_tocada(h):
    q = Consulta("h_gd_pa:12", _parte(11, 12, 13))
    run(B.boton_guardia(q))
    nuevo = q.teclados[0]
    assert isinstance(nuevo, InlineKeyboardMarkup)
    assert [[b.callback_data for b in fila] for fila in nuevo.inline_keyboard] == [
        ["h_gd_ap:11", "h_gd_no:11", "h_gd_pa:11"], ["h_gd_ap:13", "h_gd_no:13", "h_gd_pa:13"]]
    q = Consulta("h_gd_no:13", _parte(13))
    run(B.boton_guardia(q))
    assert q.teclados == [None]                                              # la última: sin teclado


def test_si_nadie_lo_atiende_o_el_harness_falla_leo_igual_se_entera(h):
    h.callback = lambda data: None
    q = Consulta("h_gd_ap:1", _parte(1))
    run(B.boton_guardia(q))
    assert q.message.enviados[0][0] == "Este botón ya no hace nada." and q.teclados == []
    h.callback = lambda data: (_ for _ in ()).throw(RuntimeError("base bloqueada"))
    q = Consulta("h_gd_run:1")
    run(B.boton_guardia(q))
    assert "probá de nuevo" in q.message.enviados[0][0] and "RuntimeError" in q.message.enviados[0][0] and q.teclados == []


def test_el_despacho_del_harness_llega_a_agents_guardia(monkeypatch):
    vistos = []
    agents = types.ModuleType("agents")
    guardia = types.ModuleType("agents.guardia")
    guardia.callback = lambda data, quien=None: vistos.append((data, quien)) or "ok"
    agents.guardia = guardia
    monkeypatch.setitem(sys.modules, "agents", agents)
    monkeypatch.setitem(sys.modules, "agents.guardia", guardia)
    assert B._Harness().callback("h_gd_ap:5") == "ok" and vistos == [("h_gd_ap:5", "leo")]
    assert B._Harness().callback("h_cur_frenar:1") is None


def test_main_py_despacha_h_gd_a_la_guardia_antes_de_los_botones_viejos():
    src = open(os.path.join(RAIZ_BOT, "main.py"), encoding="utf-8").read()
    i = src.index('if query.data.startswith("h_gd_"):')
    assert "await _panel.boton_guardia(query)" in src[i:i + 120]
    assert i < src.index('if query.data.startswith("h_bol_"):')
    assert src.index('if query.data.startswith(("h_bv2_", "h_ea_")):') < i


def test_callback_data_entra_en_64_bytes():
    for d in ("h_gd_run:999999999", "h_gd_mute:999999999", "h_gd_ap:999999999", "h_gd_pa:999999999"):
        assert len(d.encode()) <= 64


# ═══ integración con el harness de la rama (copia al lado) ═══════════════════════════════════════════════════════════

HARNESS = os.environ.get("HARNESS_DIR_PRUEBA_GUARDIA") or os.path.join(os.path.dirname(RAIZ_BOT), "guardia")


@pytest.mark.skipif(not os.path.exists(os.path.join(HARNESS, "agents", "guardia.py")),
                    reason="no está la copia del harness con agents/guardia.py")
def test_integracion_con_el_harness_real(tmp_path):
    script = textwrap.dedent("""
        import json, sys
        sys.path.insert(0, %(bot)r)
        import panel_bot as B
        B.HARNESS_DIR = %(harness)r
        h = B.harness()
        print(json.dumps({"mejora": h.callback("h_gd_ap:999"), "aviso": h.callback("h_gd_run:999"),
                          "raro": h.callback("h_gd_ap:x"), "ajeno": h.callback("h_cur_frenar:1")}))
    """) % {"bot": RAIZ_BOT, "harness": HARNESS}
    env = dict(os.environ, HARNESS_DB=str(tmp_path / "h.db"), HARNESS_DIR=str(tmp_path), OPENAI_API_KEY="x", GUARDIA="1",
               GUARDIA_MEJORAS_PATH=str(tmp_path / "mejoras.md"), PYTHONIOENCODING="utf-8")
    p = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True, env=env, cwd=HARNESS, timeout=120,
                       encoding="utf-8")
    assert p.returncode == 0, p.stderr[-2000:]
    import json
    out = json.loads(p.stdout.strip().splitlines()[-1])
    assert out == {"mejora": "No encontré esa mejora.", "aviso": "No encontré ese aviso.", "raro": "Botón inválido.",
                   "ajeno": None}
    env["GUARDIA"] = "0"                                                     # apagada: el botón lo dice y no anota nada
    p = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True, env=env, cwd=HARNESS, timeout=120,
                       encoding="utf-8")
    assert p.returncode == 0, p.stderr[-2000:]
    out = json.loads(p.stdout.strip().splitlines()[-1])
    assert "apagada" in out["mejora"] and "apagada" in out["aviso"] and out["ajeno"] is None


def test_con_la_guardia_apagada_el_teclado_del_aviso_queda(h):
    h.guardia_activa = lambda: False
    h.callback = lambda data: "🛡 La Guardia está apagada (GUARDIA=0): no anoté nada."
    aviso = InlineKeyboardMarkup([[InlineKeyboardButton("▶️ Correrlo ahora", callback_data="h_gd_run:3")]])
    q = Consulta("h_gd_run:3", aviso)
    run(B.boton_guardia(q))
    assert q.teclados == [] and "apagada" in q.message.enviados[0][0]       # no se tocó el teclado
