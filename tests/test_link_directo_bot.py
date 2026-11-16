# -*- coding: utf-8 -*-
"""Link de Leo directo — el lado del bot (decisión de Leo, 16/11/2026). Sin red, sin token, sin Telegram: el harness y el
mensaje son de mentira, salvo la última prueba (integración con la copia del harness, si está al lado)."""
import asyncio
import json
import os
import subprocess
import sys
import textwrap
import types

import pytest

RAIZ_BOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, RAIZ_BOT)

import link_directo_bot as B  # noqa: E402


def run(coro):
    return asyncio.run(coro)


class Mensaje:
    """El mensaje de Leo (update.message): guarda lo que el bot le contesta."""
    def __init__(self, falla_al_responder=False):
        self.enviados, self.borrados = [], []
        self.chat_id = 77
        self.falla = falla_al_responder
        self.bot = types.SimpleNamespace(delete_message=self._borrar)

    async def reply_text(self, texto, **kw):
        if self.falla:
            raise RuntimeError("Telegram caído")
        self.enviados.append((texto, kw))

    def get_bot(self):
        return self.bot

    async def _borrar(self, chat_id, message_id):
        self.borrados.append((chat_id, message_id))


class Consulta:
    """El callback_query del botón."""
    def __init__(self):
        self.respuestas, self.ediciones = [], []

    async def answer(self, texto=None, show_alert=False):
        self.respuestas.append((texto, show_alert))

    async def edit_message_text(self, texto, **kw):
        self.ediciones.append((texto, kw))


def harness_falso(res=None, frenar=None, con_tomar=True, tomar=None):
    llamadas = []
    cur = types.SimpleNamespace()
    if con_tomar:
        def _tomar(jid):
            llamadas.append(("tomar", jid))
            if tomar:
                return tomar(jid)
            return dict(res)
        cur.tomar_directo = _tomar
    cur.aviso_directo = lambda r: ("AVISO %s" % r["job_id"], {"inline_keyboard": [[{"text": "⛔ Frenar", "callback_data": "h_cur_frenar:%s" % r["job_id"]}]]})
    cur.frenar_directo = lambda jid: (llamadas.append(("frenar", jid)) or frenar)
    h = types.SimpleNamespace(curador=cur, broker=types.SimpleNamespace(get_job=lambda jid: {"stage": "redaccion", "title": "T", "hilo": 2, "pub_dest": "hoy_normal"}),
                              llamadas=llamadas)
    return h


@pytest.fixture(autouse=True)
def _limpio(monkeypatch):
    monkeypatch.delenv("LINK_LEO_DIRECTO", raising=False)
    B.set_harness(None)
    yield
    B.set_harness(None)


DIRECTO = {"ok": True, "modo": "directo", "job_id": 5, "titulo": "Nota", "capa": 2, "destino": "hoy_normal", "etapa": "redaccion", "agenda": True}


# ── bandera ───────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("valor,esperado", [(None, True), ("", True), ("1", True), ("0", False), ("off", False), ("false", False),
                                            ("No", False), ("  0 ", False)])
def test_la_bandera_vale_1_por_defecto_y_0_la_apaga(monkeypatch, valor, esperado):
    if valor is not None:
        monkeypatch.setenv("LINK_LEO_DIRECTO", valor)
    assert B.activo() is esperado


def test_con_la_bandera_en_0_no_se_toca_nada_y_va_la_tarjeta_de_hoy(monkeypatch):
    monkeypatch.setenv("LINK_LEO_DIRECTO", "0")
    h = harness_falso(DIRECTO)
    B.set_harness(h)
    m = Mensaje()
    assert run(B.resolver(m, 5)) is False                     # False = el llamador manda run_briefing_single, como hoy
    assert h.llamadas == [] and m.enviados == []              # ni se aprobó ni se avisó


# ── directo ───────────────────────────────────────────────────────────────────

def test_directo_aprueba_avisa_con_un_boton_y_no_manda_tarjeta():
    h = harness_falso(DIRECTO)
    B.set_harness(h)
    m = Mensaje()
    assert run(B.resolver(m, 5)) is True
    assert h.llamadas == [("tomar", 5)]
    assert len(m.enviados) == 1
    texto, kw = m.enviados[0]
    assert texto == "AVISO 5" and kw["parse_mode"] == "HTML" and kw["disable_web_page_preview"] is True
    botones = [b for fila in kw["reply_markup"]["inline_keyboard"] for b in fila]
    assert [b["text"] for b in botones] == ["⛔ Frenar"]       # UN solo botón


def test_ya_venia_en_camino_tambien_avisa_y_no_manda_tarjeta():
    h = harness_falso(dict(DIRECTO, modo="en_camino", etapa="publicacion"))
    B.set_harness(h)
    m = Mensaje()
    assert run(B.resolver(m, 5)) is True and len(m.enviados) == 1


def test_la_tarjeta_vieja_de_un_briefing_anterior_se_borra():
    h = harness_falso(dict(DIRECTO, card_msg_id=321))
    B.set_harness(h)
    m = Mensaje()
    assert run(B.resolver(m, 5)) is True
    assert m.borrados == [(77, 321)]


def test_ya_publicada_antes_va_la_tarjeta_y_se_explica():
    h = harness_falso({"ok": False, "modo": "tarjeta", "job_id": 5, "motivo": "ya_publicada"})
    B.set_harness(h)
    m = Mensaje()
    assert run(B.resolver(m, 5)) is False
    assert len(m.enviados) == 1 and "ya salió publicada" in m.enviados[0][0]


def test_si_el_harness_todavia_no_tiene_tomar_directo_va_la_tarjeta():
    """El bot se actualiza solo y el harness se baja a mano: con el bot nuevo y el harness viejo, nada cambia."""
    h = harness_falso(con_tomar=False)
    B.set_harness(h)
    m = Mensaje()
    assert run(B.resolver(m, 5)) is False and m.enviados == []


def test_si_tomar_directo_falla_va_la_tarjeta():
    h = harness_falso(tomar=lambda jid: (_ for _ in ()).throw(RuntimeError("base bloqueada")))
    B.set_harness(h)
    m = Mensaje()
    assert run(B.resolver(m, 5)) is False


def test_si_no_se_puede_importar_el_harness_va_la_tarjeta(monkeypatch):
    monkeypatch.setattr(B, "HARNESS_DIR", "/no/existe")
    m = Mensaje()
    assert run(B.resolver(m, 5)) is False


def test_si_el_aviso_no_sale_el_job_ya_aprobado_no_recibe_otra_tarjeta():
    h = harness_falso(DIRECTO)
    B.set_harness(h)
    m = Mensaje(falla_al_responder=True)
    assert run(B.resolver(m, 5)) is True                       # True: el llamador NO manda la tarjeta de un job ya aprobado


def test_si_se_pasa_del_plazo_se_mira_en_que_quedo(monkeypatch):
    import time
    monkeypatch.setattr(B, "PLAZO_S", 0.05)
    h = harness_falso(tomar=lambda jid: time.sleep(0.4) or dict(DIRECTO))
    B.set_harness(h)
    m = Mensaje()
    assert run(B.resolver(m, 5)) is True                       # el job ya salió de 'curado': se avisa con lo que hay


# ── ⛔ Frenar ─────────────────────────────────────────────────────────────────

def test_frenar_antes_de_publicar_edita_el_aviso_sin_boton():
    h = harness_falso(frenar={"ok": True, "estado": "frenada", "titulo": "Nota <x>"})
    B.set_harness(h)
    q = Consulta()
    run(B.frenar(q, 5))
    assert h.llamadas == [("frenar", 5)] and q.respuestas == [("⛔ Frenada", False)]
    texto, kw = q.ediciones[0]
    assert texto == "⛔ <b>Frenada:</b> «Nota &lt;x&gt;». No se publica." and "reply_markup" not in kw


def test_frenar_despues_de_publicar_manda_a_editar_o_borrar():
    h = harness_falso(frenar={"ok": False, "estado": "publicada", "titulo": "Nota", "wp_url": "https://mundoempresarial.ar/nota"})
    B.set_harness(h)
    q = Consulta()
    run(B.frenar(q, 5))
    assert q.respuestas == [("Ya se publicó: usá /editar o /borrar.", True)]
    assert "Ya se publicó" in q.ediciones[0][0] and "/editar" in q.ediciones[0][0] and "/borrar" in q.ediciones[0][0]
    assert "https://mundoempresarial.ar/nota" in q.ediciones[0][0]


def test_frenar_un_job_que_no_existe_o_con_error_no_rompe():
    B.set_harness(harness_falso(frenar={"ok": False, "estado": "no_existe", "titulo": ""}))
    q = Consulta()
    run(B.frenar(q, 5))
    assert q.respuestas == [("No encontré esa nota.", True)] and q.ediciones == []
    h = harness_falso()
    h.curador.frenar_directo = lambda jid: (_ for _ in ()).throw(RuntimeError("boom"))
    B.set_harness(h)
    q = Consulta()
    run(B.frenar(q, 5))
    assert q.respuestas[0][1] is True


# ── revisión Opus: respuestas del callback, plazo y aviso de duplicado ────────────────────────────

class ConsultaYaRespondida(Consulta):
    """main.py ya respondió el callback: un segundo query.answer() lo rechaza Telegram."""
    async def answer(self, texto=None, show_alert=False):
        raise RuntimeError("Query is too old / already answered")


def test_frenar_no_se_rompe_si_el_callback_ya_estaba_respondido():
    B.set_harness(harness_falso(frenar={"ok": True, "estado": "frenada", "titulo": "Nota"}))
    q = ConsultaYaRespondida()
    run(B.frenar(q, 5))                                        # no lanza
    assert q.ediciones and "Frenada" in q.ediciones[0][0]      # y el aviso igual se edita (sin botón)
    B.set_harness(harness_falso(frenar={"ok": False, "estado": "publicada", "titulo": "Nota", "wp_url": ""}))
    q = ConsultaYaRespondida()
    run(B.frenar(q, 5))
    assert q.ediciones and "/editar" in q.ediciones[0][0]


def test_frenar_con_error_no_dice_que_no_encontro_la_nota():
    B.set_harness(harness_falso(frenar={"ok": False, "estado": "error", "titulo": "Nota"}))
    q = Consulta()
    run(B.frenar(q, 5))
    texto, alerta = q.respuestas[0]
    assert "No encontré" not in texto and "No pude frenarla" in texto and alerta is True and q.ediciones == []


def test_frenar_durante_la_publicacion_le_avisa_que_si_ya_subio_la_pasa_a_borrador():
    B.set_harness(harness_falso(frenar={"ok": True, "estado": "frenada", "titulo": "Nota", "etapa": "publicacion"}))
    q = Consulta()
    run(B.frenar(q, 5))
    assert "borrador" in q.ediciones[0][0] and "redes" in q.ediciones[0][0]
    B.set_harness(harness_falso(frenar={"ok": True, "estado": "frenada", "titulo": "Nota", "etapa": "redaccion"}))
    q = Consulta()
    run(B.frenar(q, 5))
    assert "borrador" not in q.ediciones[0][0]


def test_si_se_vence_el_plazo_el_hilo_recibe_la_orden_de_no_aprobar(monkeypatch):
    """El bot manda la tarjeta; el hilo que seguía corriendo no puede aprobar después."""
    import time
    monkeypatch.setattr(B, "PLAZO_S", 0.05)
    visto = {}

    def tomar(jid, correr_cola=True, cancelar=None):
        visto["cancelar"] = cancelar
        time.sleep(0.4)
        visto["cancelado_al_despertar"] = cancelar.is_set()
        return dict(DIRECTO)
    h = harness_falso(tomar=lambda jid: None)
    h.curador.tomar_directo = tomar
    h.broker = types.SimpleNamespace(get_job=lambda jid: {"stage": "curado", "title": "T"})     # sigue en 'curado': va la tarjeta
    B.set_harness(h)
    m = Mensaje()
    assert run(B.resolver(m, 5)) is False and m.enviados == []
    assert visto["cancelar"] is not None and visto["cancelado_al_despertar"] is True


def test_el_aviso_de_duplicado_solo_promete_frenar_si_el_directo_corre_de_verdad(monkeypatch):
    assert B.puede_directo() is False or True                  # (sin harness real no se sabe: no debe lanzar)
    B.set_harness(harness_falso(DIRECTO))
    assert B.puede_directo() is True
    B.set_harness(harness_falso(con_tomar=False))              # harness viejo
    assert B.puede_directo() is False
    B.set_harness(harness_falso(DIRECTO))
    monkeypatch.setenv("LINK_LEO_DIRECTO", "0")                # bandera apagada
    assert B.puede_directo() is False
    src = open(os.path.join(RAIZ_BOT, "main.py"), encoding="utf-8").read()
    assert "return _ldb.puede_directo()" in src


# ── acciones de Leo sobre la cola: mirar lo que devuelve confirm_cola ───────────────────────────

def _funciones_cola_de_main():
    """Extrae del main.py las dos funciones puras nuevas, sin importar las ~20 mil líneas (ni telegram)."""
    import ast
    src = open(os.path.join(RAIZ_BOT, "main.py"), encoding="utf-8").read()
    arbol = ast.parse(src)
    nodos = [n for n in arbol.body if isinstance(n, ast.FunctionDef) and n.name in ("_confirmar_cola_leo", "_cola_no_cambio_txt")]
    assert len(nodos) == 2
    ns = {}
    exec(compile(ast.Module(body=nodos, type_ignores=[]), "main_extracto", "exec"), ns)
    return ns["_confirmar_cola_leo"], ns["_cola_no_cambio_txt"]


class _BrNuevo:
    def __init__(self, filas, etapa="done"):
        self.filas, self.etapa, self.llamadas = filas, etapa, []

    def confirm_cola(self, job_id, dest, por_leo=False, **kw):
        self.llamadas.append((job_id, dest, por_leo, kw))
        return self.filas

    def get_job(self, jid):
        return {"stage": self.etapa}


class _BrViejo:
    def __init__(self):
        self.llamadas = []

    def confirm_cola(self, job_id, dest, pub_date=None, anchor_date=None, recurrence_type=None):
        self.llamadas.append((job_id, dest))


def test_las_acciones_de_leo_piden_por_leo_y_devuelven_las_filas():
    conf, _ = _funciones_cola_de_main()
    br = _BrNuevo(1)
    assert conf(br, 7, "finde", anchor_date="x") == 1
    assert br.llamadas == [(7, "finde", True, {"anchor_date": "x"})]
    assert conf(_BrNuevo(0), 7, "finde") == 0                  # 0 filas: el bot tiene que decirlo y no aprender


def test_con_un_harness_viejo_las_acciones_de_leo_siguen_andando_como_antes():
    conf, _ = _funciones_cola_de_main()
    br = _BrViejo()
    assert conf(br, 7, "finde", anchor_date="x") is None and br.llamadas == [(7, "finde")]


@pytest.mark.parametrize("etapa,frase", [("done", "ya se publicó"), ("rejected", "rechazada o frenada"),
                                         ("publicacion", "ya no está en la cola"), (None, "no encontré")])
def test_si_no_cambio_nada_el_bot_dice_la_verdad(etapa, frase):
    _, txt = _funciones_cola_de_main()
    br = _BrNuevo(0, etapa)
    br.get_job = lambda jid: ({"stage": etapa} if etapa else None)
    assert frase in txt(br, 7)


def test_main_py_no_aplica_el_aprendizaje_ni_dice_ok_si_confirm_cola_no_cambio_nada():
    src = open(os.path.join(RAIZ_BOT, "main.py"), encoding="utf-8").read()
    # ningún llamador usa confirm_cola «a ciegas» salvo el helper
    import re
    usos = [l for l in src.splitlines() if re.search(r"_br_(cola|prog)\.confirm_cola\(", l)]
    assert usos == [], usos
    # h_cola_confirm confirma ANTES del aprendizaje
    i = src.index("_n_cf = _confirmar_cola_leo(")
    assert i < src.index("# ── Aprendizaje: comparar sugerencia del agente", i - 10) and "if _n_cf == 0:" in src[i:i + 200]


# ── que main.py esté cableado como corresponde ─────────────────────────────────

def test_main_py_usa_el_resolver_en_los_dos_caminos_del_link_externo_y_maneja_el_boton():
    src = open(os.path.join(RAIZ_BOT, "main.py"), encoding="utf-8").read()
    assert src.count("await _link_leo_resolver(update, job_id_hl)") == 1
    assert src.count("await _link_leo_resolver(update, _jid_mh)") == 1
    assert 'action == "h_cur_frenar"' in src and "_ldb_f.frenar(query, int(arg))" in src
    # los otros caminos manuales NO cambian: siguen mandando la tarjeta
    for resto in ("_cur_ok.run_briefing_single", "_cur_nmj.run_briefing_single", "_cur_tw.run_briefing_single"):
        assert resto in src
    assert "_start_tweet_nota(update, context, url)" in src and 'kind in ("youtube", "instagram")' in src


# ── integración con el harness real (copia al lado) ────────────────────────────

HARNESS = os.environ.get("HARNESS_DIR_PRUEBA") or os.path.join(os.path.dirname(RAIZ_BOT), "links")


@pytest.mark.skipif(not os.path.exists(os.path.join(HARNESS, "agents", "curador.py")), reason="no está la copia del harness")
def test_integracion_con_el_curador_real(tmp_path):
    db = str(tmp_path / "h.db")
    script = textwrap.dedent("""
        import asyncio, json, os, sys, types
        sys.path.insert(0, %(bot)r)
        import link_directo_bot as B
        B.HARNESS_DIR = %(harness)r
        import broker
        from agents import cola
        broker.init_db(%(db)s)
        cola._suggest = lambda t, x, h: {'pub_dest': 'finde', 'reasoning': 'x', 'anchor_date': None, 'recurrence_type': None}
        j = broker.enqueue('curado', 'https://medio.com.ar/uno', 'Una nota de prueba', {'manual_submission': True, 'text': 'x ' * 400},
                           score=7.5, hilo=2, force=True)
        class M:
            chat_id = 1
            enviados = []
            async def reply_text(self, t, **k): self.enviados.append((t, k))
            def get_bot(self): return types.SimpleNamespace(delete_message=None)
        m = M()
        ok = asyncio.run(B.resolver(m, j))
        job = broker.get_job(j)
        print(json.dumps({'ok': ok, 'stage': job['stage'], 'by': job['approved_by'], 'dest': job['pub_dest'],
                          'aviso': m.enviados[0][0], 'boton': m.enviados[0][1]['reply_markup']}))
    """) % {"bot": RAIZ_BOT, "harness": HARNESS, "db": repr(db)}
    env = dict(os.environ, HARNESS_DB=db, OPENAI_API_KEY="x", HARNESS_DIR=HARNESS, AGENDA_REUNION="0")
    env.pop("LINK_LEO_DIRECTO", None)
    p = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True, env=env, cwd=HARNESS, timeout=120)
    assert p.returncode == 0, p.stderr[-2000:]
    out = json.loads(p.stdout.strip().splitlines()[-1])
    assert out["ok"] is True and out["stage"] == "redaccion" and out["by"] == "leo" and out["dest"] == "finde"
    assert "La tomé" in out["aviso"] and "el finde" in out["aviso"]
    assert out["boton"]["inline_keyboard"][0][0]["text"] == "⛔ Frenar" and len(out["boton"]["inline_keyboard"]) == 1
