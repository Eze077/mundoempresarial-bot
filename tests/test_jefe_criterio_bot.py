# -*- coding: utf-8 -*-
"""jefe_criterio_bot (Jefe v3, etapa 5): los botones jcr_ y /criterios llegan al harness y el mensaje se repinta. Sin red ni Telegram;
la integración usa la copia local del harness (rama jefe-criterio) con una base temporal, si está al lado."""
import asyncio
import json
import os
import re
import sqlite3
import sys
import types

import pytest

import jefe_criterio_bot as B

RAIZ = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class Msg:
    def __init__(self):
        self.respuestas = []

    async def reply_text(self, t, **kw):
        self.respuestas.append((t, kw))


class Q:
    def __init__(self, data, editar_falla=False):
        self.data, self.message, self.editado, self.toast, self._falla = data, Msg(), None, "sin contestar", editar_falla

    async def answer(self, texto=None, **kw):
        self.toast = texto

    async def edit_message_text(self, t, **kw):
        if self._falla:
            raise RuntimeError("Message is not modified")
        self.editado = (t, kw)


def _boton(q, mod):
    asyncio.run(B.hacer_handler_boton(lambda: mod)(types.SimpleNamespace(callback_query=q), None))


@pytest.mark.parametrize("data", ["jcr_ok:1", "jcr_no:12", "jcr_ret:123456789", "jcr_rtl:7"])
def test_el_patron_toma_los_botones_del_harness(data):
    assert re.match(B.PATRON, data) and len(data.encode()) <= 64


@pytest.mark.parametrize("data", ["jcr_ok:", "jcr_si:1", "jcr_ok:1:2", "jcr_ok:a", "xjcr_ok:1", "jcr_ok:1234567890", "jfd_si:1:0"])
def test_el_patron_no_toma_otros(data):
    assert not re.match(B.PATRON, data)


def test_el_clic_va_al_harness_contesta_el_aviso_y_repinta_el_mensaje():
    vistos = []
    kb = {"inline_keyboard": [[{"text": "🗑 Retirar 1", "callback_data": "jcr_ret:4"}]]}
    mod = types.SimpleNamespace(boton=lambda d: vistos.append(d) or {"ok": True, "texto": "🧠 <b>Criterios</b> ✅ vigente", "botones": kb,
                                                                       "toast": "✅ Vigente"})
    q = Q("jcr_ok:4")
    _boton(q, mod)
    assert vistos == ["jcr_ok:4"] and q.toast == "✅ Vigente"
    assert q.editado[0] == "🧠 <b>Criterios</b> ✅ vigente" and q.editado[1]["reply_markup"] == kb and q.editado[1]["parse_mode"] == "HTML"


def test_si_el_harness_no_hace_nada_solo_avisa():
    q = Q("jcr_ok:4")
    _boton(q, types.SimpleNamespace(boton=lambda d: {"ok": False, "toast": "Los criterios del Jefe no están activos."}))
    assert q.toast == "Los criterios del Jefe no están activos." and q.editado is None and q.message.respuestas == []


def test_si_no_se_puede_editar_va_aparte_y_un_error_se_dice():
    q = Q("jcr_no:4", editar_falla=True)
    _boton(q, types.SimpleNamespace(boton=lambda d: {"ok": True, "texto": "❌ rechazado", "botones": None, "toast": "❌"}))
    assert q.message.respuestas[0][0] == "❌ rechazado"
    q = Q("jcr_no:4")
    _boton(q, types.SimpleNamespace(boton=lambda d: (_ for _ in ()).throw(RuntimeError("base trabada"))))
    assert "No pude resolver el botón" in q.message.respuestas[0][0] and q.editado is None


def test_criterios_lista_lo_que_devuelve_el_harness():
    m = Msg()
    kb = {"inline_keyboard": [[{"text": "🗑 Retirar #3", "callback_data": "jcr_rtl:3"}]]}
    mod = types.SimpleNamespace(lista_vigentes=lambda: {"ok": True, "texto": "🧠 vigentes", "botones": kb})
    asyncio.run(B.hacer_cmd_criterios(lambda: mod)(types.SimpleNamespace(message=m), None))
    assert m.respuestas[0][0] == "🧠 vigentes" and m.respuestas[0][1]["reply_markup"] == kb
    m = Msg()
    mod = types.SimpleNamespace(lista_vigentes=lambda: {"ok": False, "toast": "Los criterios del Jefe no están activos."})
    asyncio.run(B.hacer_cmd_criterios(lambda: mod)(types.SimpleNamespace(message=m), None))
    assert m.respuestas[0][0] == "Los criterios del Jefe no están activos."


def test_registra_el_comando_y_los_botones_antes_del_catch_all():
    from telegram.ext import Application, CallbackQueryHandler, CommandHandler
    app = Application.builder().token("123456:ABC-fake-token-for-tests").build()
    B.registrar(app)
    hs = app.handlers[0]
    assert [type(x) for x in hs] == [CommandHandler, CallbackQueryHandler] and "criterios" in hs[0].commands
    src = open(os.path.join(RAIZ, "main.py"), encoding="utf-8").read()
    assert src.index("_jefe_bot.registrar(") < src.index("_jcr_bot.registrar(app)") < src.index(
        "app.add_handler(CallbackQueryHandler(handle_button))")


def test_el_comando_figura_en_la_ayuda():
    import comandos_menu
    assert "/criterios" in comandos_menu.texto_ayuda()


# ── integración con la copia del harness, si está al lado ─────────────────────

HARNESS = os.environ.get("HARNESS_DIR_TEST") or os.path.join(os.path.dirname(RAIZ), "jefe-criterio")


@pytest.mark.skipif(not os.path.exists(os.path.join(HARNESS, "agents", "jefe_criterio.py")), reason="no está la copia del harness")
def test_integracion_con_el_harness_real(tmp_path, monkeypatch):
    db = str(tmp_path / "h.db")
    monkeypatch.setenv("HARNESS_DB", db)
    monkeypatch.setenv("OPENAI_API_KEY", "ci-sin-clave")
    monkeypatch.setattr(B, "HARNESS_DIR", HARNESS)
    monkeypatch.setattr(sys, "path", list(sys.path))                    # cargar() suma el harness al path: se restaura al final
    for k in [k for k in sys.modules if k == "agents" or k.startswith("agents.") or k == "core" or k.startswith("core.")]:
        monkeypatch.delitem(sys.modules, k)
    JC = B.cargar()
    c = sqlite3.connect(db)
    c.executescript(JC._DDL)
    ev = {"descripcion": "No elegiste 7 de 7 notas de política en capa 2 en 4 días.", "ejemplos": [{"ref": "job #1", "titulo": "Una"}]}
    c.execute("INSERT INTO jefe_criterios (texto, alcance, evidencia, estado, propuesto_en, origen, clave, semana) VALUES "
              "('Política fuera de la capa 2','curador',?,'propuesto','2099-01-05 07:10:00','semanal:2099-W02','politica_capa:2:-','2099-W02')",
              (json.dumps(ev),))
    c.execute("INSERT INTO jefe_criterio_corridas (semana, ts, estado, ids, detalle) VALUES ('2099-W02','x','enviada','[1]','{}')")
    c.commit()
    c.close()
    monkeypatch.delenv("JEFE_CRITERIO", raising=False)
    q = Q("jcr_ok:1")
    _boton(q, JC)
    assert q.toast == "Los criterios del Jefe no están activos." and q.editado is None       # apagada: no toca nada
    monkeypatch.setenv("JEFE_CRITERIO", "1")
    q = Q("jcr_ok:1")
    _boton(q, JC)
    assert "Vigente" in q.toast and "✅ vigente" in q.editado[0]
    assert q.editado[1]["reply_markup"]["inline_keyboard"][0][0]["callback_data"] == "jcr_ret:1"
    m = Msg()
    asyncio.run(B.hacer_cmd_criterios(lambda: JC)(types.SimpleNamespace(message=m), None))
    assert "Política fuera de la capa 2" in m.respuestas[0][0]
    for k in [k for k in sys.modules if k == "agents" or k.startswith("agents.") or k == "core" or k.startswith("core.")]:
        monkeypatch.delitem(sys.modules, k)


# ── revisión adversarial (25/11, ⚪5): /criterios con muchos vigentes pasa los 4000 y un corte a lo bruto rompe el HTML ──

def _lista_larga(n=45, largo=75):
    L = ["🧠 <b>Criterios vigentes del Jefe</b> (%d)" % n, ""]
    for i in range(n):
        L.append("<b>#%d</b> Criterio %02d %s · <i>curador</i> · desde el 23/11" % (i, i, "y" * largo))
    return "\n".join(L)


@pytest.mark.parametrize("largo", list(range(60, 90, 3)))
def test_rev5_criterios_largo_sale_en_pedazos_con_el_html_entero(largo):
    texto = _lista_larga(largo=largo)
    assert len(texto) > 4000
    kb = {"inline_keyboard": [[{"text": "🗑 Retirar #1", "callback_data": "jcr_rtl:1"}]]}
    m = Msg()
    mod = types.SimpleNamespace(lista_vigentes=lambda: {"ok": True, "texto": texto, "botones": kb})
    asyncio.run(B.hacer_cmd_criterios(lambda: mod)(types.SimpleNamespace(message=m), None))
    assert len(m.respuestas) >= 2 and "\n".join(t for t, _ in m.respuestas) == texto        # no se pierde nada
    for t, kw in m.respuestas:
        assert len(t) <= 4000 and t.count("<b>") == t.count("</b>") and t.count("<i>") == t.count("</i>")
    assert [kw["reply_markup"] for _t, kw in m.respuestas] == [None] * (len(m.respuestas) - 1) + [kb]   # botones en el último


def test_rev5_un_boton_con_un_repintado_largo_tambien_va_en_pedazos():
    texto = _lista_larga()
    q = Q("jcr_rtl:3")
    _boton(q, types.SimpleNamespace(boton=lambda d: {"ok": True, "texto": texto, "botones": None, "toast": "🗑 Retirado"}))
    assert q.editado is None and len(q.message.respuestas) >= 2 and all(len(t) <= 4000 for t, _ in q.message.respuestas)
