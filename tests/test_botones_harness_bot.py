# -*- coding: utf-8 -*-
"""botones_harness_bot (20/11/2026): fpd_ y pfc_ llegan al harness y la respuesta se suma al mensaje. Sin red ni Telegram."""
import asyncio
import types

import botones_harness_bot as B
import borrado_redes as R


class Msg:
    def __init__(self, html="Ficha retenida"):
        self.text_html, self.respuestas = html, []

    async def reply_text(self, t, **kw):
        self.respuestas.append(t)


class Q:
    def __init__(self, data, html="Ficha retenida", editar_falla=False):
        self.data, self.message, self.editado, self.contestado, self._falla = data, Msg(html), None, False, editar_falla

    async def answer(self, *a, **k):
        self.contestado = True

    async def edit_message_text(self, t, **kw):
        if self._falla:
            raise RuntimeError("Message is not modified")
        self.editado = t


def _correr(prefijo, q, mod):
    upd = types.SimpleNamespace(callback_query=q)
    asyncio.run(B.hacer_handler(prefijo, lambda nombre: mod)(upd, None))


def test_fpd_publica_y_suma_el_resultado_al_mensaje():
    vistos = []
    mod = types.SimpleNamespace(boton=lambda d: vistos.append(d) or {"ok": True, "texto": "✅ Publiqué la versión retenida."})
    q = Q("fpd_si:12")
    _correr("fpd_", q, mod)
    assert vistos == ["fpd_si:12"] and q.contestado and q.editado == "Ficha retenida\n\n✅ Publiqué la versión retenida."


def test_pfc_usa_el_modulo_de_perfiles():
    assert B.MODULOS["pfc_"] == "perfiles_botones" and B.MODULOS["fpd_"] == "ficha_pendiente"
    mod = types.SimpleNamespace(boton=lambda d: {"ok": True, "texto": "Cuenta confirmada."})
    q = Q("pfc_no:3", html="Cuenta dudosa", editar_falla=True)
    _correr("pfc_", q, mod)
    assert q.message.respuestas == ["Cuenta confirmada."]          # si no se puede editar, va aparte


def test_si_el_harness_falla_lo_dice_y_no_toca_nada():
    def falla(d):
        raise ValueError("base bloqueada")
    q = Q("fpd_no:1")
    _correr("fpd_", q, types.SimpleNamespace(boton=falla))
    assert q.editado is None and "No pude resolver el botón" in q.message.respuestas[0]


def test_registrar_pone_los_dos_patrones():
    agregados = []
    app = types.SimpleNamespace(add_handler=lambda h, *a, **k: agregados.append(h.pattern.pattern))
    B.registrar(app)
    assert agregados == [r"^fpd_(si|no):\d+$", r"^pfc_(si|no):\d+$"]


# ── posteos reemplazados al re-publicar (20/11/2026) ──────────────────────────

def test_anteriores_de_nota_lee_las_claves_ant():
    c = "<p>x</p><!-- mebot:tweet_id=222;tg_msg=88;tweet_id_ant=111,100;li_org_urn_ant=urn:li:share:9 -->"
    assert R.anteriores_de_nota(c) == {"twitter": ["111", "100"], "li_pag": ["urn:li:share:9"]}


def test_borrar_anteriores_solo_las_redes_elegidas(monkeypatch):
    vistos = []
    monkeypatch.setattr(R, "borrar_red", lambda red, oid: vistos.append((red, oid)) or R.Resultado(R.BORRADO, "verificado"))
    out = R.borrar_anteriores({"twitter"}, {"twitter": ["111"], "li_pag": ["urn:li:share:9"]})
    assert vistos == [("twitter", "111")] and out[0][1].detalle == "posteo anterior · verificado"
