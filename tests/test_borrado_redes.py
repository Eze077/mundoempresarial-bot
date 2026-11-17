# -*- coding: utf-8 -*-
"""Borrar en TODAS las redes (/publicador → Borrar), 17/11/2026. Sin red, sin claves, sin Telegram: `requests` es de mentira.

Caso real que lo motivó: la frase #57490 salió en WordPress, canal de Telegram, página de LinkedIn e Instagram (feed e
historia). El publicador solo conocía Telegram (el único id que se guardaba), así que borró WordPress y Telegram y dio por
terminado el trabajo; LinkedIn e Instagram siguieron publicados."""
import json
import os
import sys
import types

import pytest
import requests as requests_real

RAIZ_BOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, RAIZ_BOT)

import borrado_redes as bd  # noqa: E402


# ── un `requests` de mentira con rutas ───────────────────────────────────────

class Resp:
    def __init__(self, status=200, datos=None, texto="", headers=None):
        self.status_code = status
        self._datos = datos
        self.text = texto or (json.dumps(datos) if datos is not None else "")
        self.headers = headers or {}
        self.ok = 200 <= status < 300

    def json(self):
        if self._datos is None:
            raise ValueError("sin json")
        return self._datos


class FalsoRequests:
    """Cada ruta: (método, fragmento de la URL, respuesta o lista de respuestas que se van gastando, última se repite)."""
    exceptions = requests_real.exceptions

    def __init__(self):
        self.rutas, self.llamadas = [], []

    def ruta(self, metodo, fragmento, *respuestas):
        self.rutas.append([metodo, fragmento, list(respuestas)])

    def request(self, metodo, url, **kw):
        self.llamadas.append((metodo, url, kw))
        for m, frag, resp in self.rutas:
            if m == metodo and frag in url:
                r = resp[0] if len(resp) == 1 else resp.pop(0)
                if isinstance(r, Exception):
                    raise r
                return r
        raise AssertionError("ruta no prevista: %s %s" % (metodo, url))

    def get(self, url, **kw):
        return self.request("GET", url, **kw)

    def post(self, url, **kw):
        return self.request("POST", url, **kw)

    def delete(self, url, **kw):
        return self.request("DELETE", url, **kw)

    def metodos(self):
        return [(m, u.split("?")[0]) for m, u, _ in self.llamadas]


NO_EXISTE = Resp(400, {"error": {"code": 100, "error_subcode": 33, "message": "Object does not exist"}})


@pytest.fixture
def falso(monkeypatch):
    f = FalsoRequests()
    monkeypatch.setattr(bd, "requests", f)
    for k in ("FB_PAGE_TOKEN", "TELEGRAM_TOKEN", "TELEGRAM_CHANNEL", "LINKEDIN_TOKEN", "LINKEDIN_ORG_TOKEN",
              "LINKEDIN_TOKEN_STATE", "LINKEDIN_MEMBER_STATE"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("FB_PAGE_TOKEN", "token-falso-meta")
    monkeypatch.setenv("TELEGRAM_TOKEN", "token-falso-tg")
    monkeypatch.setenv("TELEGRAM_CHANNEL", "@CanalDePrueba")
    monkeypatch.setenv("LINKEDIN_TOKEN_STATE", os.path.join(RAIZ_BOT, "no_existe_pagina.json"))
    monkeypatch.setenv("LINKEDIN_MEMBER_STATE", os.path.join(RAIZ_BOT, "no_existe_persona.json"))
    monkeypatch.setenv("LINKEDIN_ORG_TOKEN", "li-pagina-falso")
    monkeypatch.setenv("LINKEDIN_TOKEN", "li-persona-falso")
    monkeypatch.setattr(bd, "WARP", None)          # nada de proxys en las pruebas
    return f


# ── los ids registrados ──────────────────────────────────────────────────────

CONTENIDO_FRASE = ("<!-- wp:html -->\n<p>x</p>\n<!-- /wp:html -->\n"
                   "<!-- mebot:tg_msg=14583;li_org_urn=urn:li:share:7528554215590420480;ig_feed=17937103527148059;"
                   "ig_story=18118358789161405 -->")


def test_registro_lee_el_comentario_con_los_ids_nuevos():
    reg = bd.registro_de_nota(CONTENIDO_FRASE, 57490, cola={})
    assert reg == {"telegram": "14583", "li_pag": "urn:li:share:7528554215590420480",
                   "ig_feed": "17937103527148059", "ig_story": "18118358789161405"}


def test_registro_suma_la_cola_de_meta_por_wp_post_id_en_posted_no_en_la_raiz():
    cola = {"items": [], "posted": {"900": {"fb": "123_456", "ig": "999111", "ig_story": "555"}}, "900": {"fb": "RAIZ_NO"}}
    reg = bd.registro_de_nota("<!-- mebot:tweet_id=11;tg_msg=22;li_urn=urn:li:share:33 -->", 900, cola=cola)
    assert reg == {"twitter": "11", "telegram": "22", "linkedin": "urn:li:share:33",
                   "facebook": "123_456", "ig_feed": "999111", "ig_story": "555"}
    assert bd.registro_de_nota("", 901, cola=cola) == {}


def test_construir_meta_conserva_las_tres_de_siempre_primero_y_no_deja_pasar_separadores():
    txt = bd.construir_meta({"ig_feed": "1", "tweet_id": "2", "tg_msg": "3", "raro": "a;b>c"})
    assert txt.startswith("<!-- mebot:tweet_id=2;tg_msg=3;") and txt.endswith(" -->")
    assert bd.parse_meta(txt) == {"tweet_id": "2", "tg_msg": "3", "ig_feed": "1", "raro": "abc"}
    assert bd.construir_meta({}) == ""


def test_dudosos_de_la_cola_se_informan():
    cola = {"dudosos": [{"canal": "ig", "clave": "7"}, {"canal": "fb", "clave": "8"}]}
    assert bd.dudosos_de_cola(7, cola) == ["ig"] and bd.dudosos_de_cola(9, cola) == []


# ── Meta: se juzga por el objeto, no por el DELETE ──────────────────────────

def test_facebook_delete_da_500_pero_borro_y_se_verifica_con_un_get(falso):
    falso.ruta("GET", "/123_456", Resp(200, {"id": "123_456", "permalink_url": "https://fb/x"}), NO_EXISTE)
    falso.ruta("DELETE", "/123_456", Resp(500, {"error": {"message": "unknown error"}}))
    r = bd.borrar_meta("facebook", "123_456")
    assert r.estado == bd.BORRADO
    assert [m for m, _ in falso.metodos()] == ["GET", "DELETE", "GET"]       # existe → borra → verifica


def test_instagram_que_sigue_arriba_se_dice_y_se_da_el_link(falso):
    falso.ruta("GET", "/17937103527148059", Resp(200, {"id": "17937103527148059", "permalink": "https://instagram.com/p/x/"}))
    falso.ruta("DELETE", "/17937103527148059", Resp(400, {"error": {"message": "Unsupported delete request"}}))
    r = bd.borrar_meta("ig_feed", "17937103527148059")
    assert r.estado == bd.SIGUE and "app" in r.detalle and "https://instagram.com/p/x/" in r.detalle


def test_meta_objeto_que_ya_no_existe_es_no_habia_y_no_se_manda_ningun_delete(falso):
    falso.ruta("GET", "/555", NO_EXISTE)
    r = bd.borrar_meta("ig_story", "555")
    assert r.estado == bd.NO_HABIA and "DELETE" not in [m for m, _ in falso.metodos()]


def test_meta_con_token_vencido_no_afirma_nada(falso):
    falso.ruta("GET", "/555", Resp(400, {"error": {"code": 190, "message": "token expired"}}))
    assert bd.borrar_meta("facebook", "555").estado == bd.DUDOSO
    assert "DELETE" not in [m for m, _ in falso.metodos()]


def test_meta_sin_token_o_con_id_raro(falso, monkeypatch):
    assert bd.borrar_meta("facebook", "../me/accounts").estado == bd.DUDOSO
    monkeypatch.delenv("FB_PAGE_TOKEN")
    assert bd.borrar_meta("facebook", "123_4").estado == bd.DUDOSO


# ── Telegram ─────────────────────────────────────────────────────────────────

def test_telegram_borrado_ok(falso):
    falso.ruta("POST", "/deleteMessage", Resp(200, {"ok": True, "result": True}))
    assert bd.borrar_telegram("14583").estado == bd.BORRADO
    _, url, kw = falso.llamadas[0]
    assert kw["json"] == {"chat_id": "@CanalDePrueba", "message_id": 14583}


def test_telegram_mensaje_que_ya_no_estaba(falso):
    falso.ruta("POST", "/deleteMessage", Resp(400, {"ok": False, "description": "Bad Request: message to delete not found"}))
    assert bd.borrar_telegram("1").estado == bd.NO_HABIA


def test_telegram_que_no_deja_borrar_y_sigue_visible_en_la_web_publica(falso):
    falso.ruta("POST", "/deleteMessage", Resp(400, {"ok": False, "description": "Bad Request: message can't be deleted"}))
    falso.ruta("GET", "t.me/CanalDePrueba/7", Resp(200, texto="<div class='tgme_widget_message_text'>hola</div>"))
    r = bd.borrar_telegram("7")
    assert r.estado == bd.SIGUE and "can't be deleted" in r.detalle


# ── X ────────────────────────────────────────────────────────────────────────

def test_x_borrado_verificado(monkeypatch):
    estados = iter([True, False])
    monkeypatch.setattr(bd, "_x_existe", lambda t: next(estados))
    borrados = []
    monkeypatch.setattr(bd, "_x_borrar", lambda t: borrados.append(t) or True)
    assert bd.borrar_twitter("1234567890").estado == bd.BORRADO and borrados == ["1234567890"]


def test_x_que_sigue_publicado_aunque_la_api_dijo_ok(monkeypatch):
    monkeypatch.setattr(bd, "_x_existe", lambda t: True)
    monkeypatch.setattr(bd, "_x_borrar", lambda t: True)
    assert bd.borrar_twitter("1234567890").estado == bd.SIGUE


def test_x_ya_borrado_no_gasta_un_delete(monkeypatch):
    monkeypatch.setattr(bd, "_x_existe", lambda t: False)
    monkeypatch.setattr(bd, "_x_borrar", lambda t: (_ for _ in ()).throw(AssertionError("no debía borrar")))
    assert bd.borrar_twitter("1234567890").estado == bd.NO_HABIA


# ── LinkedIn ─────────────────────────────────────────────────────────────────

URN = "urn:li:share:7528554215590420480"


def test_linkedin_pagina_borrado_y_verificado(falso):
    falso.ruta("GET", "/rest/posts/", Resp(200, {"id": URN, "lifecycleState": "PUBLISHED"}), Resp(404, {"message": "not found"}))
    falso.ruta("DELETE", "/rest/posts/", Resp(204))
    r = bd.borrar_linkedin(URN, [("página", "li-pagina-falso")])
    assert r.estado == bd.BORRADO
    assert "urn%3Ali%3Ashare%3A7528554215590420480" in falso.llamadas[0][1]        # el urn va codificado en la URL
    assert falso.llamadas[0][2]["headers"]["Authorization"] == "Bearer li-pagina-falso"


def test_linkedin_prueba_con_el_token_de_la_persona_si_no_es_de_la_pagina(falso):
    falso.ruta("GET", "/rest/posts/", Resp(403), Resp(200, {"lifecycleState": "PUBLISHED"}), Resp(404))
    falso.ruta("DELETE", "/rest/posts/", Resp(204))
    r = bd.borrar_linkedin(URN, [("página", "tok-pag"), ("personal", "tok-per")])
    assert r.estado == bd.BORRADO and "personal" in r.detalle
    assert falso.llamadas[-2][2]["headers"]["Authorization"] == "Bearer tok-per"      # el DELETE fue con el token dueño


def test_linkedin_con_el_token_vencido_no_dice_que_borro(falso):
    falso.ruta("GET", "/rest/posts/", Resp(401, {"code": "EXPIRED_ACCESS_TOKEN"}))
    r = bd.borrar_linkedin(URN, [("página", "a"), ("personal", "b")])
    assert r.estado == bd.DUDOSO and "401" in r.detalle
    assert "DELETE" not in [m for m, _ in falso.metodos()]


def test_linkedin_que_sigue_publicado_tras_el_delete(falso):
    falso.ruta("GET", "/rest/posts/", Resp(200, {"lifecycleState": "PUBLISHED"}))
    falso.ruta("DELETE", "/rest/posts/", Resp(204))
    assert bd.borrar_linkedin(URN, [("página", "a")]).estado == bd.SIGUE


def test_linkedin_ya_no_estaba(falso):
    falso.ruta("GET", "/rest/posts/", Resp(404))
    assert bd.borrar_linkedin(URN, [("página", "a"), ("personal", "b")]).estado == bd.NO_HABIA


def test_linkedin_prueba_la_version_siguiente_si_da_426(falso):
    falso.ruta("GET", "/rest/posts/", Resp(426), Resp(200, {"lifecycleState": "PUBLISHED"}), Resp(404))
    falso.ruta("DELETE", "/rest/posts/", Resp(204))
    assert bd.borrar_linkedin(URN, [("página", "a")]).estado == bd.BORRADO


def test_linkedin_urn_con_forma_rara_no_llega_a_la_red(falso):
    assert bd.borrar_linkedin("urn:li:share:1/../../x", [("página", "a")]).estado == bd.DUDOSO
    assert falso.llamadas == []


# ── el despachador y el informe ──────────────────────────────────────────────

def test_borrar_nota_recorre_todas_las_redes_y_deja_wordpress_para_el_final(monkeypatch):
    orden = []
    monkeypatch.setattr(bd, "borrar_red", lambda red, oid: orden.append(red) or bd.Resultado(bd.BORRADO, "ok"))
    reg = {"telegram": "1", "li_pag": URN, "ig_feed": "2", "ig_story": "3"}
    res = bd.borrar_nota({"telegram", "li_pag", "ig_feed", "ig_story", "facebook", "wp"}, reg,
                         wp=lambda: orden.append("wp") or bd.Resultado(bd.BORRADO, "papelera"))
    assert orden == ["telegram", "li_pag", "ig_feed", "ig_story", "wp"]          # facebook no tenía id → no se llamó
    por_red = {r: x for r, x, _ in res}
    assert por_red["facebook"].estado == bd.NO_HABIA and "no hay posteo registrado" in por_red["facebook"].detalle
    assert por_red["twitter"] .estado == bd.NO_HABIA   # no elegida y sin id: «no había posteo»


def test_una_red_que_explota_no_frena_a_las_demas(monkeypatch):
    def borrar(red, oid):
        if red == "telegram":
            raise RuntimeError("se cayó")
        return bd.Resultado(bd.BORRADO, "ok")
    monkeypatch.setattr(bd, "borrar_telegram", lambda oid: borrar("telegram", oid))
    monkeypatch.setattr(bd, "borrar_meta", lambda red, oid: bd.Resultado(bd.BORRADO, "ok"))
    res = dict((r, x) for r, x, _ in bd.borrar_nota({"telegram", "ig_feed"}, {"telegram": "1", "ig_feed": "2"}))
    assert res["telegram"].estado == bd.DUDOSO and res["ig_feed"].estado == bd.BORRADO


def test_el_informe_dice_red_por_red(monkeypatch):
    monkeypatch.setattr(bd, "borrar_telegram", lambda oid: bd.Resultado(bd.BORRADO, "mensaje borrado del canal"))
    monkeypatch.setattr(bd, "borrar_meta", lambda red, oid: bd.Resultado(bd.SIGUE, "Instagram no deja borrar por API"))
    res = bd.borrar_nota({"telegram", "ig_feed", "twitter", "wp"}, {"telegram": "1", "ig_feed": "2", "ig_story": "3"},
                         wp=lambda: bd.Resultado(bd.BORRADO, "a la papelera"))
    txt = bd.formatear("La importancia del conocimiento", res, dudosos=["ig"])
    assert "✅ Telegram (canal): borrado" in txt
    assert "❌ Instagram (feed): SIGUE PUBLICADO" in txt
    assert "ℹ️ X (Twitter): no había posteo — no hay posteo registrado" in txt
    assert "➖ Instagram (historia): no lo elegiste (sigue publicado)" in txt
    assert "✅ WordPress: borrado" in txt
    assert "DUDOSO" in txt and "reintentar" in txt


def test_el_texto_de_una_excepcion_de_requests_nunca_llega_al_resultado(falso):
    """La excepción de requests lleva la URL entera; la del canal de Telegram lleva el token del bot."""
    secreto = "1234567890:" + "AA" + "x" * 35
    falso.ruta("POST", "/deleteMessage", requests_real.exceptions.ConnectionError("fallo en https://api.telegram.org/bot%s/deleteMessage" % secreto))
    falso.ruta("GET", "t.me/", requests_real.exceptions.ConnectionError("sin red " + secreto))
    r = bd.borrar_telegram("5")
    assert secreto not in repr(r) and r.estado in (bd.DUDOSO, bd.SIGUE)
