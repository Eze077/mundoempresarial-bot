# -*- coding: utf-8 -*-
"""«🧭 El editor publicaría» (harness BRIEFING_EDITOR, 24/11/2026): el bloque usa el checklist de siempre (h_cur_auto_tog /
h_cur_auto_okm) con la marca del harness al final de la máscara. El bot aprueba igual; lo único distinto es que, al aprobar un
bloque, NO borra el encabezado del briefing de arriba (que sigue esperando su propio checklist). Sin red, sin token, sin Telegram:
la función se saca de main.py con ast, como en test_botones_harness_bot."""
import ast
import asyncio
import io
import json
import os
import types

RAIZ = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC = open(os.path.join(RAIZ, "main.py"), encoding="utf-8").read()


def _borrar():
    nodos = [n for n in ast.parse(SRC).body if isinstance(n, ast.AsyncFunctionDef) and n.name == "_borrar_cards_briefing"]
    assert len(nodos) == 1
    real_open = open

    def falso_open(p, *a, **k):
        if str(p).endswith("auto_briefing_hdr.json"):
            return io.StringIO(json.dumps({"header_msg_id": 55}))
        return real_open(p, *a, **k)
    ns = {"open": falso_open}
    exec(compile(ast.Module(body=nodos, type_ignores=[]), "main_extracto", "exec"), ns)
    return ns["_borrar_cards_briefing"]


class Bot:
    def __init__(self):
        self.borrados = []

    async def delete_message(self, chat_id, message_id):
        self.borrados.append(message_id)


def test_aprobar_el_checklist_de_siempre_borra_el_encabezado_como_antes():
    f, bot = _borrar(), Bot()
    asyncio.run(f(types.SimpleNamespace(bot=bot), 1, []))
    assert bot.borrados == [55]


def test_aprobar_un_bloque_del_editor_no_borra_el_encabezado():
    f, bot = _borrar(), Bot()
    asyncio.run(f(types.SimpleNamespace(bot=bot), 1, [], encabezado=False))
    assert bot.borrados == []


def _tramo(desde, hasta):
    i = SRC.index(desde)
    return SRC[i:SRC.index(hasta, i + len(desde))]


def test_el_aprobar_marcadas_de_un_bloque_lo_resuelve_el_harness():
    tramo = _tramo('elif action == "h_cur_auto_okm"', 'elif action == "h_cur_auto_renew"')
    # el bloque se reconoce por la máscara (no solo 0/1), no por una constante del harness que un proceso viejo no tiene
    assert "_es_bloque_editor = bool(mask) and not mask.isdigit()" in tramo and "MARCA_EDITOR" not in tramo
    # el harness decide (aprueba las ☑, descarta las sin marcar, no toca lo viejo): fuera del loop de eventos
    assert '_aprobar_bloque = getattr(_ea_bl, "aprobar_bloque", None)' in tramo
    assert "await asyncio.to_thread(_aprobar_bloque, ids, mask)" in tramo
    assert "encabezado=False" in tramo and 'get("quitar_teclado")' in tramo
    # sin aprobar_bloque (harness viejo en memoria): avisa y NO aprueba nada (antes caía en la recorrida de siempre y la
    # destildada quedaba en curado para el editor en vivo)
    j = tramo.index("if _es_bloque_editor and _aprobar_bloque is None:")
    k = tramo.index("elif _aprobar_bloque is not None:")
    assert "reiniciá el bot" in tramo[j:k] and "approve" not in tramo[j:k]
    # la recorrida de siempre (por _cur.approve, como Leo) queda solo para el checklist de siempre
    assert tramo.count("_cur.approve(int(jid))") == 1 and tramo.index("_cur.approve(int(jid))") > k


def test_el_toggle_de_un_bloque_va_al_harness_y_el_de_siempre_no_cambio():
    tramo = _tramo('elif action == "h_cur_auto_tog"', 'elif action == "h_cur_auto_okm"')
    assert "if mask and not mask.isdigit():" in tramo and "await _tog_bloque_editor(query, ids, i)" in tramo
    assert "_cur._checklist_kb(ids, newmask, n)" in tramo                    # el checklist de siempre, igual que antes


class Query:
    def __init__(self):
        self.respuestas, self.teclados = [], []

    async def answer(self, texto=None, show_alert=False):
        self.respuestas.append((texto, show_alert))

    async def edit_message_reply_markup(self, reply_markup=None):
        self.teclados.append(reply_markup)


def _tog(monkeypatch, alternar):
    """_tog_bloque_editor de main.py con un agents.editor_auto de mentira."""
    import logging
    import sys
    nodos = [n for n in ast.parse(SRC).body if isinstance(n, ast.AsyncFunctionDef) and n.name == "_tog_bloque_editor"]
    assert len(nodos) == 1
    ns = {"asyncio": asyncio, "logger": logging.getLogger("prueba")}
    exec(compile(ast.Module(body=nodos, type_ignores=[]), "main_extracto", "exec"), ns)
    ea = types.ModuleType("agents.editor_auto")
    if alternar is not None:
        ea.alternar_casilla = alternar
    ag = types.ModuleType("agents")
    ag.editor_auto = ea
    monkeypatch.setitem(sys.modules, "agents", ag)
    monkeypatch.setitem(sys.modules, "agents.editor_auto", ea)
    return ns["_tog_bloque_editor"]


def test_la_casilla_se_repinta_con_lo_que_guardo_el_harness(monkeypatch):
    llamadas = []
    f = _tog(monkeypatch, lambda ids, i: llamadas.append((ids, i)) or {"ok": True, "kb": {"inline_keyboard": [["guardado"]]}})
    q = Query()
    asyncio.run(f(q, "1-2", 1))
    assert llamadas == [("1-2", 1)] and q.teclados == [{"inline_keyboard": [["guardado"]]}] and q.respuestas == []


def test_si_la_base_falla_no_se_repinta(monkeypatch):
    def falla(ids, i):
        raise RuntimeError("database is locked")
    f, q = _tog(monkeypatch, falla), Query()
    asyncio.run(f(q, "1-2", 0))
    assert q.teclados == [] and q.respuestas == [("No pude marcar la casilla: probá de nuevo.", True)]


def test_un_bloque_que_ya_no_vale_avisa_y_se_queda_sin_teclado(monkeypatch):
    f, q = _tog(monkeypatch, lambda ids, i: {"ok": False, "alerta": "Este bloque ya lo cerraste.", "quitar_teclado": True}), Query()
    asyncio.run(f(q, "1-2", 0))
    assert q.respuestas == [("Este bloque ya lo cerraste.", True)] and q.teclados == [None]


def test_con_el_harness_viejo_en_memoria_pide_reiniciar(monkeypatch):
    f, q = _tog(monkeypatch, None), Query()
    asyncio.run(f(q, "1-2", 0))
    assert q.teclados == [] and "reiniciá el bot" in q.respuestas[0][0]


def test_proponer_no_rearma_el_briefing_con_un_bloque_del_editor():
    """Bot sin reiniciar con el curador viejo en memoria: el bloque se repinta con «Proponer». La máscara del bloque no es solo
    0/1, así que ese «Proponer» avisa y no descarta ni rearma nada; el de siempre (solo 0/1) sigue igual."""
    guardia = 'elif action == "h_cur_auto_renew" and arg and not arg.isdigit():'
    i, j = SRC.index(guardia), SRC.index('elif action == "h_cur_auto_renew" and arg is not None:')
    assert i < j                                                               # va antes que el de siempre
    tramo = SRC[i:j]
    assert "show_alert=True" in tramo and "reject_job" not in tramo and "run_briefing_auto" not in tramo
    assert "11111".isdigit() and not "10111e".isdigit()
