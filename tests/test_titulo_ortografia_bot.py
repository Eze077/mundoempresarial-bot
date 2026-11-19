# -*- coding: utf-8 -*-
"""El título que Leo escribe en una tarjeta pasa por el corrector (19/11/2026: «Diputados tratará la ley fe emergencia pyme» salió
así a la web y a las redes porque los tres caminos de título de la tarjeta no llamaban al corrector). Sale TAL CUAL; el corrector
solo avisa, con un botón «✅ Aplicar corrección». Sin red, sin claves: el corrector y WordPress son de mentira."""
import asyncio
import json
import os
import sqlite3
import sys

import pytest

RAIZ_BOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, RAIZ_BOT)

MAL = "Diputados tratará la ley fe emergencia pyme"
BIEN = "Diputados tratará la ley de emergencia pyme"


@pytest.fixture
def main(monkeypatch, tmp_path):
    for k in ("TELEGRAM_TOKEN", "WP_USER", "WP_PASS"):
        monkeypatch.setenv(k, "falso")
    import main as m
    db = str(tmp_path / "harness.db")
    with sqlite3.connect(db) as c:
        c.execute("CREATE TABLE jobs (id INTEGER PRIMARY KEY, title TEXT, content_json TEXT, wp_post_id INTEGER, stage TEXT,"
                  " updated_at TEXT)")
    monkeypatch.setattr(m, "_HDB", db)
    m._wp = []
    monkeypatch.setattr(m, "update_post", lambda pid, payload: m._wp.append((pid, payload)) or True)
    monkeypatch.setattr(m, "_ortografia_cambios", lambda t: [{"de": "fe", "a": "de"}] if " fe " in t else [])
    return m


def _job(m, jid, title=MAL, stage="curado", wp=None, cj=None):
    with sqlite3.connect(m._HDB) as c:
        c.execute("INSERT INTO jobs (id, title, content_json, wp_post_id, stage) VALUES (?,?,?,?,?)",
                  (jid, title, json.dumps(dict(cj or {}, title=title, title_locked=True)), wp, stage))


def _fila(m, jid):
    with sqlite3.connect(m._HDB) as c:
        t, cj = c.execute("SELECT title, content_json FROM jobs WHERE id=?", (jid,)).fetchone()
    return t, json.loads(cj)


class _Msg:
    def __init__(self):
        self.enviados = []

    async def reply_text(self, texto, **kw):
        self.enviados.append((texto, kw))


class _Upd:
    def __init__(self):
        self.message = _Msg()


def test_avisa_con_boton_y_deja_el_titulo_tal_cual(main):
    _job(main, 1)
    u = _Upd()
    assert asyncio.run(main._titulo_orto_revisar(u, 1, MAL)) is True
    texto, kw = u.message.enviados[0]
    assert "Lo dejé tal cual" in texto and "fe → de" in texto and f"Quedaría: <b>{BIEN}</b>" in texto
    boton = kw["reply_markup"].inline_keyboard[0][0]
    assert boton.text == "✅ Aplicar corrección" and boton.callback_data == "ortit:1"
    t, cj = _fila(main, 1)
    assert t == MAL and cj["title"] == MAL                       # manual gana: no se cambia solo
    assert cj["orto_titulo"] == {"de": MAL, "a": BIEN}


def test_sin_errores_o_con_el_corrector_caido_no_molesta(main, monkeypatch):
    _job(main, 2, title=BIEN)
    u = _Upd()
    assert asyncio.run(main._titulo_orto_revisar(u, 2, BIEN)) is False and u.message.enviados == []

    def roto(t):
        raise RuntimeError("sin red")
    monkeypatch.setattr(main, "_ortografia_cambios", roto)
    assert asyncio.run(main._titulo_orto_revisar(u, 2, MAL)) is False and u.message.enviados == []


def test_aplicar_en_una_nota_que_no_salio_cambia_solo_la_nota(main):
    _job(main, 3)
    asyncio.run(main._titulo_orto_revisar(_Upd(), 3, MAL))
    ok, txt = main._titulo_orto_aplicar(3)
    t, cj = _fila(main, 3)
    assert ok and BIEN in txt and t == BIEN and cj["title"] == BIEN and cj["title_locked"] is True and "orto_titulo" not in cj
    assert main._wp == []
    assert main._titulo_orto_aplicar(3)[0] is False               # dos clics: el segundo no hace nada


def test_aplicar_en_una_nota_publicada_corrige_la_web_sin_tocar_el_slug(main):
    _job(main, 4, stage="done", wp=57670)
    asyncio.run(main._titulo_orto_revisar(_Upd(), 4, MAL))
    ok, txt = main._titulo_orto_aplicar(4)
    assert ok and "también en la web" in txt
    pid, payload = main._wp[0]
    assert pid == 57670 and payload["title"] == BIEN and payload["meta"]["rank_math_title"] == BIEN and "slug" not in payload


def test_si_leo_cambio_el_titulo_despues_no_se_aplica_nada(main):
    _job(main, 5)
    asyncio.run(main._titulo_orto_revisar(_Upd(), 5, MAL))
    with sqlite3.connect(main._HDB) as c:
        c.execute("UPDATE jobs SET title=? WHERE id=5", ("Otro título que escribió Leo",))
    ok, txt = main._titulo_orto_aplicar(5)
    assert not ok and "cambió" in txt and _fila(main, 5)[0] == "Otro título que escribió Leo"


def test_los_tres_caminos_de_titulo_de_la_tarjeta_llaman_al_corrector_y_el_boton_esta_enrutado():
    src = open(os.path.join(RAIZ_BOT, "main.py"), encoding="utf-8").read()
    assert src.count("await _titulo_orto_revisar(update, job_id,") == 3
    assert 'CallbackQueryHandler(handle_ortit_button, pattern=r"^ortit:\\d+$")' in src
    # tiene que ir ANTES del manejador general, que se queda con todo lo que no matchea antes
    assert src.index("handle_ortit_button, pattern") < src.index("app.add_handler(CallbackQueryHandler(handle_button))")
