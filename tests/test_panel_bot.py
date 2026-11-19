# -*- coding: utf-8 -*-
"""/estado, /pausa y los botones h_bv2_/h_ea_ (panel_bot.py, fase 4 v2.0). Sin red, sin token, sin Telegram: el harness y los
mensajes son de mentira, salvo la última prueba (integración con la copia del harness al lado, si está).

Lo central: con PAUSA_GENERAL sin prender (como hoy) el bot hace EXACTAMENTE lo de siempre: _pausado() es BOT_PAUSED, /STOP y
/RESUME no cambian, y /pausa solo contesta que el freno general no está prendido."""
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


def run(coro):
    return asyncio.run(coro)


class Mensaje:
    def __init__(self):
        self.enviados = []

    async def reply_text(self, texto, **kw):
        self.enviados.append((texto, kw))


class Consulta:
    def __init__(self, data):
        self.data = data
        self.respuestas, self.ediciones, self.teclados = [], [], []
        self.message = Mensaje()

    async def answer(self, texto=None, show_alert=False):
        self.respuestas.append((texto, show_alert))

    async def edit_message_text(self, texto, **kw):
        self.ediciones.append((texto, kw))

    async def edit_message_reply_markup(self, reply_markup=None):
        self.teclados.append(reply_markup)


def upd(msg=None, query=None):
    return types.SimpleNamespace(message=msg, callback_query=query)


def ctx(*args):
    return types.SimpleNamespace(args=list(args))


class PausaFalsa:
    def __init__(self, ruta):
        self.ruta, self.llamadas = ruta, []

    def poner(self, por="leo", motivo=""):
        self.llamadas.append(("poner", por, motivo))
        ya = os.path.exists(self.ruta)
        open(self.ruta, "w").write("{}")
        return {"ok": True, "ya_estaba": ya, "estado": {"desde": "2026-11-18 21:00:00", "por": por, "motivo": motivo}}

    def levantar(self, por="leo"):
        self.llamadas.append(("levantar", por))
        ya = not os.path.exists(self.ruta)
        if not ya:
            os.remove(self.ruta)
        return {"ok": True, "ya_estaba": ya}

    def texto_cubre(self):
        return "Frena:\n• todo"


@pytest.fixture
def h(tmp_path, monkeypatch):
    ruta = str(tmp_path / "pausa_general.flag")
    monkeypatch.setenv("PAUSA_GENERAL_PATH", ruta)
    monkeypatch.delenv("PAUSA_GENERAL", raising=False)
    falso = types.SimpleNamespace(pausa=PausaFalsa(ruta), estado=types.SimpleNamespace(texto=lambda: "📊 <b>Estado</b> · prueba"),
                                  despachos=[])
    falso.callback = lambda data: falso.despachos.append(data) or ("⛔ Listo: el boletín no sale." if data.startswith("h_bv2_")
                                                                   else ("✅ Listo" if data.startswith("h_ea_") else None))
    B.set_harness(falso)
    yield falso
    B.set_harness(None)


# ═══ 1. con la bandera apagada no cambia nada ════════════════════════════════════════════════════════════════════════

@pytest.mark.parametrize("valor", [None, "", "0", "off", "quizas"])
def test_con_la_bandera_apagada_pausa_no_toca_nada(h, monkeypatch, valor):
    if valor is not None:
        monkeypatch.setenv("PAUSA_GENERAL", valor)
    open(os.environ["PAUSA_GENERAL_PATH"], "w").write("{}")          # aunque haya un archivo viejo
    assert B.bandera_pausa() is False and B.pausa_activa() is False
    m = Mensaje()
    run(B.cmd_pausa(upd(m), ctx()))
    assert "todavía no está prendido" in m.enviados[0][0] and h.pausa.llamadas == []
    q = Consulta("pz_on")
    run(B.boton_pausa(upd(query=q), None))
    assert q.respuestas[0][1] is True and h.pausa.llamadas == []


def _funcs_main(*nombres):
    """Extrae funciones de main.py sin importar las ~20 mil líneas (ni telegram)."""
    import ast
    src = open(os.path.join(RAIZ_BOT, "main.py"), encoding="utf-8").read()
    nodos = [n for n in ast.parse(src).body if isinstance(n, ast.FunctionDef) and n.name in nombres]
    assert len(nodos) == len(nombres)
    return nodos


def test_pausado_es_bot_paused_si_la_bandera_esta_apagada(h, monkeypatch):
    import ast
    ns = {"_panel": B, "BOT_PAUSED": False}
    exec(compile(ast.Module(body=_funcs_main("_pausado", "_txt_pausado"), type_ignores=[]), "main_extracto", "exec"), ns)
    open(os.environ["PAUSA_GENERAL_PATH"], "w").write("{}")
    for bp in (False, True):
        ns["BOT_PAUSED"] = bp
        assert ns["_pausado"]() is bp                                      # bandera apagada: exactamente /STOP
        assert ns["_txt_pausado"]() == "⏸ Bot en pausa. Usá /RESUME para reactivar."
    monkeypatch.setenv("PAUSA_GENERAL", "1")
    ns["BOT_PAUSED"] = False
    assert ns["_pausado"]() is True and "/pausa off" in ns["_txt_pausado"]()


def test_main_py_usa_pausado_en_todos_los_lugares_de_bot_paused():
    src = open(os.path.join(RAIZ_BOT, "main.py"), encoding="utf-8").read()
    assert "if BOT_PAUSED:" not in src and "lambda: BOT_PAUSED" not in src
    assert src.count("if _pausado():") == 6 and src.count("pausado=_pausado") == 2
    assert 'CommandHandler("STOP", cmd_stop)' in src and 'CommandHandler("RESUME", cmd_resume)' in src
    # el despacho de los botones del harness va ANTES del de h_bol_ (que contesta «botón viejo»)
    i = src.index('if query.data.startswith(("h_bv2_", "h_ea_")):')
    assert i < src.index('if query.data.startswith("h_bol_"):') and "await _panel.boton_harness(query)" in src[i:i + 200]
    # /estado y /pausa registrados antes del handle_button general
    assert src.index("_panel.registrar(app)") < src.index("app.add_handler(CallbackQueryHandler(handle_button))")


def test_estado_y_pausa_estan_en_el_menu():
    import comandos_menu
    nombres = [c for c, _d in comandos_menu.OCULTOS]
    assert "estado" in nombres and "pausa" in nombres


def test_registrar_agrega_los_comandos_y_los_botones_pz_y_h_pesos():
    from telegram.ext import Application, CallbackQueryHandler, CommandHandler
    app = Application.builder().token("1:x").build()
    B.registrar(app)
    hs = app.handlers[0]
    assert [type(x) for x in hs] == [CommandHandler, CommandHandler, CallbackQueryHandler, CommandHandler, CallbackQueryHandler]
    assert {c for x in hs if isinstance(x, CommandHandler) for c in x.commands} == {"estado", "pausa", "pesos"}
    assert [x.pattern.pattern for x in hs if isinstance(x, CallbackQueryHandler)] == ["^pz_", "^h_pesos"]


# ═══ 2. /pausa con la bandera prendida ═══════════════════════════════════════════════════════════════════════════════

def test_pausa_pone_y_saca_con_boton_y_persiste_en_el_archivo(h, monkeypatch):
    monkeypatch.setenv("PAUSA_GENERAL", "1")
    m = Mensaje()
    run(B.cmd_pausa(upd(m), ctx("raro", "lo", "de", "X")))
    assert h.pausa.llamadas == [("poner", "leo", "raro lo de X")] and B.pausa_activa() is True
    texto, kw = m.enviados[0]
    assert "Pausa general puesta" in texto and "raro lo de X" in texto and kw["parse_mode"] == "HTML"
    assert kw["reply_markup"]["inline_keyboard"][0][0]["callback_data"] == "pz_off"
    m2 = Mensaje()
    run(B.cmd_pausa(upd(m2), ctx()))
    assert "ya estaba puesta" in m2.enviados[0][0]
    q = Consulta("pz_off")
    run(B.boton_pausa(upd(query=q), None))
    assert B.pausa_activa() is False and "levantada" in q.ediciones[0][0]
    m3 = Mensaje()
    run(B.cmd_pausa(upd(m3), ctx("off")))
    assert "No había pausa" in m3.enviados[0][0]
    q = Consulta("pz_on")
    run(B.boton_pausa(upd(query=q), None))
    assert B.pausa_activa() is True


def test_pausa_si_el_harness_no_tiene_el_freno(monkeypatch, tmp_path):
    monkeypatch.setenv("PAUSA_GENERAL", "1")

    class SinPausa:
        @property
        def pausa(self):
            raise ImportError("no core.pausa")
    B.set_harness(SinPausa())
    try:
        m = Mensaje()
        run(B.cmd_pausa(upd(m), ctx()))
        assert "falta desplegarlo" in m.enviados[0][0]
    finally:
        B.set_harness(None)


# ═══ 3. /estado ══════════════════════════════════════════════════════════════════════════════════════════════════════

def test_estado_muestra_lo_que_arma_el_harness(h, monkeypatch):
    m = Mensaje()
    run(B.cmd_estado(upd(m), ctx()))
    assert m.enviados[0][0] == "📊 <b>Estado</b> · prueba" and m.enviados[0][1]["reply_markup"] is None
    monkeypatch.setenv("PAUSA_GENERAL", "1")
    m = Mensaje()
    run(B.cmd_estado(upd(m), ctx()))
    assert m.enviados[0][1]["reply_markup"]["inline_keyboard"][0][0]["callback_data"] == "pz_on"
    h.estado.texto = lambda: (_ for _ in ()).throw(RuntimeError("boom"))
    m = Mensaje()
    run(B.cmd_estado(upd(m), ctx()))
    assert "No pude armar el estado" in m.enviados[0][0]


# ═══ 4. botones del harness ══════════════════════════════════════════════════════════════════════════════════════════

@pytest.mark.parametrize("data", ["h_bv2_frenar:7", "h_ea_lib:12", "h_ea_rev:12", "h_ea_vivo:2"])
def test_los_botones_del_harness_se_despachan_y_se_dice_lo_que_contesto(h, data):
    q = Consulta(data)
    run(B.boton_harness(q))
    assert h.despachos == [data] and q.teclados == [None]
    assert q.message.enviados and q.message.enviados[0][0] in ("⛔ Listo: el boletín no sale.", "✅ Listo")


def test_un_boton_que_nadie_atiende_o_que_falla_no_rompe(h):
    h.callback = lambda data: None
    q = Consulta("h_ea_raro:1")
    run(B.boton_harness(q))
    assert q.respuestas == [("Este botón ya no hace nada.", True)] and q.teclados == []
    h.callback = lambda data: (_ for _ in ()).throw(RuntimeError("base bloqueada"))
    q = Consulta("h_bv2_frenar:1")
    run(B.boton_harness(q))
    assert q.respuestas[0][1] is True and "probá de nuevo" in q.respuestas[0][0] and q.teclados == []


def test_callback_data_de_los_botones_entra_en_64_bytes():
    for d in ("pz_on", "pz_off", "h_ea_lib:999999999", "h_ea_rev:999999999", "h_ea_vivo:0", "h_bv2_frenar:999999999"):
        assert len(d.encode()) <= 64


def test_sin_claves_en_el_modulo():
    src = open(os.path.join(RAIZ_BOT, "panel_bot.py"), encoding="utf-8").read()
    import re
    assert not re.search(r"(sk-[A-Za-z0-9]{10,}|\d{9,}:[A-Za-z0-9_-]{30,}|\+?54 ?9? ?11)", src)


# ═══ 5. integración con el harness real (copia al lado) ═════════════════════════════════════════════════════════════

HARNESS = os.environ.get("HARNESS_DIR_PRUEBA") or os.path.join(os.path.dirname(RAIZ_BOT), "fase4")


@pytest.mark.skipif(not os.path.exists(os.path.join(HARNESS, "core", "pausa.py")), reason="no está la copia del harness con core/pausa.py")
def test_integracion_con_el_harness_real(tmp_path):
    script = textwrap.dedent("""
        import asyncio, json, os, sys, types
        sys.path.insert(0, %(bot)r)
        import panel_bot as B
        B.HARNESS_DIR = %(harness)r
        class M:
            def __init__(s): s.enviados = []
            async def reply_text(s, t, **k): s.enviados.append(t)
        u = lambda m: types.SimpleNamespace(message=m)
        m1, m2, m3 = M(), M(), M()
        asyncio.run(B.cmd_pausa(u(m1), types.SimpleNamespace(args=["prueba"])))
        activa = B.pausa_activa()
        asyncio.run(B.cmd_estado(u(m2), types.SimpleNamespace(args=[])))
        asyncio.run(B.cmd_pausa(u(m3), types.SimpleNamespace(args=["off"])))
        h = B.harness()
        print(json.dumps({"activa": activa, "puesta": m1.enviados[0], "estado": m2.enviados[0], "fuera": m3.enviados[0],
                          "despues": B.pausa_activa(), "boletin": h.callback("h_bv2_frenar:999"),
                          "editor": h.callback("h_ea_vivo:2"), "ajeno": h.callback("h_cur_frenar:1")}))
    """) % {"bot": RAIZ_BOT, "harness": HARNESS}
    env = dict(os.environ, HARNESS_DB=str(tmp_path / "h.db"), PAUSA_GENERAL="1", PAUSA_GENERAL_PATH=str(tmp_path / "p.flag"),
               OPENAI_API_KEY="x", HARNESS_DIR=HARNESS, PYTHONIOENCODING="utf-8")
    p = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True, env=env, cwd=HARNESS, timeout=120,
                       encoding="utf-8")
    assert p.returncode == 0, p.stderr[-2000:]
    import json
    out = json.loads(p.stdout.strip().splitlines()[-1])
    assert out["activa"] is True and "Pausa general puesta" in out["puesta"] and "prueba" in out["puesta"]
    assert "PAUSA GENERAL PUESTA" in out["estado"] and "levantada" in out["fuera"] and out["despues"] is False
    assert out["boletin"] == "No encontré esa edición."                       # el despacho llega a agents/boletin.callback
    assert out["editor"] == "La capa 2 no estaba frenada."                    # y a agents/editor_auto.callback
    assert out["ajeno"] is None
