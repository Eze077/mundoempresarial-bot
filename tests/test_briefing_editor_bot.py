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
    assert "encabezado=False" in tramo
    j = tramo.index("if _es_bloque_editor and _aprobar_bloque is None:")
    k = tramo.index("elif _aprobar_bloque is not None:")
    m = tramo.index("else:" + chr(10) + " " * 20 + "done = 0")
    # sin aprobar_bloque (harness viejo en memoria): avisa y NO aprueba nada
    assert "reiniciá el bot" in tramo[j:k] and "approve" not in tramo[j:k]
    # las ramas del bloque: handle_button ya contestó la callback (una sola respuesta por toque), así que los avisos van por
    # _aviso_bloque (mensaje sin sonido), nunca por query.answer; el teclado se saca o se repinta con lo guardado ANTES del aviso
    bloque = tramo[j:m]
    assert "query.answer" not in bloque and "show_alert" not in bloque and bloque.count("await _aviso_bloque(query,") == 2
    assert 'get("quitar_teclado")' in bloque and 'reply_markup=_res_bl["kb"]' in bloque
    assert bloque.index('reply_markup=_res_bl["kb"]') < bloque.index('_aviso_bloque(query, (_res_bl or {}).get("alerta")')
    # la recorrida de siempre (por _cur.approve, como Leo) queda solo para el checklist de siempre
    assert tramo.count("_cur.approve(int(jid))") == 1 and tramo.index("_cur.approve(int(jid))") > m


def test_el_toggle_de_un_bloque_va_al_harness_y_el_de_siempre_no_cambio():
    tramo = _tramo('elif action == "h_cur_auto_tog"', 'elif action == "h_cur_auto_okm"')
    assert "if mask and not mask.isdigit():" in tramo and "await _tog_bloque_editor(query, ids, i)" in tramo
    assert "_cur._checklist_kb(ids, newmask, n)" in tramo                    # el checklist de siempre, igual que antes


class YaContestada(Exception):
    pass


class Mensaje:
    def __init__(self, eventos):
        self.eventos = eventos

    async def reply_text(self, texto, **kw):
        self.eventos.append(("aviso", texto, kw.get("disable_notification")))


class Query:
    """Como la de PTB después del `await query.answer()` del principio de handle_button: otra respuesta lanza."""

    def __init__(self, falla_edicion=False):
        self.eventos, self.falla_edicion = [], falla_edicion
        self.message = Mensaje(self.eventos)

    async def answer(self, *a, **k):
        self.eventos.append(("answer",))
        raise YaContestada("Query is too old and response timeout expired or query id is invalid")

    async def edit_message_reply_markup(self, reply_markup=None):
        if self.falla_edicion:
            raise RuntimeError("429 Too Many Requests")
        self.eventos.append(("teclado", reply_markup))


def _tog(monkeypatch, alternar):
    """_tog_bloque_editor y _aviso_bloque de main.py con un agents.editor_auto de mentira."""
    import logging
    import sys
    nodos = [n for n in ast.parse(SRC).body if isinstance(n, ast.AsyncFunctionDef) and n.name in ("_tog_bloque_editor", "_aviso_bloque")]
    assert len(nodos) == 2
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
    assert llamadas == [("1-2", 1)] and q.eventos == [("teclado", {"inline_keyboard": [["guardado"]]})]


def test_si_el_repintado_falla_no_rompe(monkeypatch):
    f, q = _tog(monkeypatch, lambda ids, i: {"ok": True, "kb": {"inline_keyboard": []}}), Query(falla_edicion=True)
    asyncio.run(f(q, "1-2", 1))                                                # quedó guardado; «Aprobar» repinta con lo guardado
    assert q.eventos == []


def test_si_la_base_falla_no_se_repinta_y_leo_lo_ve(monkeypatch):
    def falla(ids, i):
        raise RuntimeError("database is locked")
    f, q = _tog(monkeypatch, falla), Query()
    asyncio.run(f(q, "1-2", 0))
    assert q.eventos == [("aviso", "No pude marcar la casilla: probá de nuevo.", True)]


def test_un_bloque_que_ya_no_vale_se_queda_sin_teclado_y_despues_avisa(monkeypatch):
    f, q = _tog(monkeypatch, lambda ids, i: {"ok": False, "alerta": "Este bloque ya lo cerraste.", "quitar_teclado": True}), Query()
    asyncio.run(f(q, "1-2", 0))
    assert q.eventos == [("teclado", None), ("aviso", "Este bloque ya lo cerraste.", True)]


def test_con_el_harness_viejo_en_memoria_pide_reiniciar(monkeypatch):
    f, q = _tog(monkeypatch, None), Query()
    asyncio.run(f(q, "1-2", 0))
    assert len(q.eventos) == 1 and q.eventos[0][0] == "aviso" and "reiniciá el bot" in q.eventos[0][1]


def test_proponer_no_rearma_el_briefing_con_un_bloque_del_editor():
    """Bot sin reiniciar con el curador viejo en memoria: el bloque se repinta con «Proponer». La máscara del bloque no es solo
    0/1, así que ese «Proponer» avisa (visible: la callback ya se contestó) y no descarta ni rearma nada; el de siempre sigue igual."""
    guardia = 'elif action == "h_cur_auto_renew" and arg and not arg.isdigit():'
    i, j = SRC.index(guardia), SRC.index('elif action == "h_cur_auto_renew" and arg is not None:')
    assert i < j                                                               # va antes que el de siempre
    tramo = SRC[i:j]
    assert "await _aviso_bloque(query," in tramo and "query.answer" not in tramo
    assert "reject_job" not in tramo and "run_briefing_auto" not in tramo
    assert "11111".isdigit() and not "10111e".isdigit()
