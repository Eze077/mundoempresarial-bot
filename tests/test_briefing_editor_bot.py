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


def test_el_aprobar_marcadas_reconoce_el_bloque_por_la_marca_del_harness():
    i = SRC.index('elif action == "h_cur_auto_okm"')
    tramo = SRC[i:SRC.index('elif action == "h_cur_auto_renew"', i)]
    assert '_marca_ed = getattr(_cur, "MARCA_EDITOR", None)' in tramo          # sin la constante (harness viejo): como siempre
    assert "_es_bloque_editor = bool(_marca_ed) and mask.endswith(_marca_ed)" in tramo
    assert "encabezado=not _es_bloque_editor" in tramo
    assert tramo.count("_cur.approve(int(jid))") == 1                         # aprueba por el mismo camino, como Leo
    # el toggle no cambió: repinta con el teclado que arma el harness (que reconoce la marca)
    j = SRC.index('elif action == "h_cur_auto_tog"')
    assert "_cur._checklist_kb(ids, newmask, n)" in SRC[j:i]
