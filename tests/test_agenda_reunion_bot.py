# -*- coding: utf-8 -*-
"""Lado del bot de la reunión de agenda (fase 4 v2.0). Sin red, sin token, sin base: el acceso al harness es de mentira,
salvo la última prueba (integración con la copia del harness, si está al lado)."""
import asyncio
import os
import sys
import types

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import agenda_reunion_bot as B  # noqa: E402


def run(coro):
    return asyncio.run(coro)


# ── un harness de mentira ─────────────────────────────────────────────────────

class FakeHarness:
    def __init__(self, abierta=True):
        self.abierta = abierta
        self.llamadas = []
        self.orden = []                                  # orden de lo que pasó (callback contestado, trabajo del harness…)
        self.relojes = []
        self.agenda_dia = types.SimpleNamespace(reunion_abierta=self._abierta, get_reunion=lambda f: {"estado": "deliberando"},
                                                fecha_de=lambda ahora=None: "2026-11-16")
        self.jefe = types.SimpleNamespace(deliberar=self._deliberar, accion_boton=self._boton, texto_plan=lambda f, t: ("PLAN", None),
                                          post_cierre=self._post_cierre)
        self.boton_resp = {"ok": True, "toast": "Aprobados 2", "texto": "<b>Plan aprobado</b>", "botones": {"inline_keyboard": []}, "editar": True}
        self.demora_post_cierre = 0.0
        self.frases = types.SimpleNamespace(elegir=self._elegir)
        self.respuesta = {"ok": True, "texto": "<b>Plan revisado</b>", "botones": {"inline_keyboard": []}}

    def _abierta(self, ahora=None):
        self.relojes.append(ahora)
        self.llamadas.append("reunion_abierta")
        if self.abierta == "error":
            raise RuntimeError("base caída")
        return "2026-11-16" if self.abierta else None

    def _deliberar(self, texto, tipo="texto", ahora=None):
        self.llamadas.append(("deliberar", texto, tipo))
        self.relojes.append(ahora)
        return self.respuesta

    def _boton(self, data, ahora=None):
        self.llamadas.append(("boton", data))
        self.orden.append("harness:boton")
        self.relojes.append(ahora)
        return self.boton_resp

    def _post_cierre(self, fecha):
        import time as _t
        self.orden.append("post_cierre:inicio")
        _t.sleep(self.demora_post_cierre)
        self.orden.append("post_cierre:fin")
        return "💬 La agenda cambió: te mandé las frases de nuevo."

    def _elegir(self, fid):
        self.llamadas.append(("elegir", fid))
        return {"id": 7, "autor": "Javier Milei", "url": "https://mundoempresarial.ar/n1", "texto_placa": "«Una frase» — Javier Milei"} if fid == "7" else None


@pytest.fixture()
def h(monkeypatch):
    monkeypatch.setenv("AGENDA_REUNION", "1")
    fh = FakeHarness()
    B.set_harness(fh)
    yield fh
    B.set_harness(None)
    B.set_reloj(None)


# ── objetos de Telegram de mentira ────────────────────────────────────────────

class Msg:
    def __init__(self, text=None, voice=None):
        self.text, self.voice, self.audio = text, voice, None
        self.respuestas, self.ediciones = [], []

    async def reply_text(self, texto, **kw):
        self.respuestas.append((texto, kw))
        return self


class Upd:
    def __init__(self, message=None, query=None):
        self.message, self.callback_query = message, query


class Ctx:
    def __init__(self, user_data=None):
        self.user_data = user_data or {}


def _fallback_registrador():
    llamadas = []

    async def fb(update, context):
        llamadas.append(update)
    return fb, llamadas


# ── la bandera y el filtro ────────────────────────────────────────────────────

@pytest.mark.parametrize("valor,esperado", [(None, "0"), ("0", "0"), ("banana", "0"), ("dry", "dry"), ("1", "1")])
def test_modo(monkeypatch, valor, esperado):
    if valor is None:
        monkeypatch.delenv("AGENDA_REUNION", raising=False)
    else:
        monkeypatch.setenv("AGENDA_REUNION", valor)
    assert B.modo() == esperado


@pytest.mark.parametrize("valor", [None, "0", "dry"])
def test_con_la_bandera_apagada_o_en_dry_el_bot_no_captura_ni_toca_la_base(monkeypatch, valor):
    if valor is None:
        monkeypatch.delenv("AGENDA_REUNION", raising=False)
    else:
        monkeypatch.setenv("AGENDA_REUNION", valor)
    fh = FakeHarness(abierta=True)
    B.set_harness(fh)
    try:
        assert B.FiltroReunion().filter(Msg("hola")) is False
        assert B.abierta() is False
        assert fh.llamadas == []                                  # ni siquiera consultó la base
        run(B.cmd_agenda(Upd(Msg("/agenda")), Ctx()))             # el comando no responde con la bandera en 0
    finally:
        B.set_harness(None)


def test_el_filtro_sigue_a_la_reunion_y_falla_cerrado(monkeypatch):
    monkeypatch.setenv("AGENDA_REUNION", "1")
    for estado, esperado in ((True, True), (False, False), ("error", False)):
        B.set_harness(FakeHarness(abierta=estado))
        assert B.FiltroReunion().filter(Msg("x")) is esperado
    B.set_harness(None)


def test_en_otro_flujo():
    assert B.en_otro_flujo({"awaiting_frase_header": "kicker"}) is True
    assert B.en_otro_flujo({"awaiting_x": False, "otra": 1}) is False
    assert B.en_otro_flujo({"evento": {"nombre": "x"}}) is True and B.en_otro_flujo({"input_evento": {}}) is True
    assert B.en_otro_flujo({"frase_pending": {}}) is False and B.en_otro_flujo({}) is False and B.en_otro_flujo(None) is False


def test_es_solo_links():
    assert B.es_solo_links("https://www.ambito.com/nota") and B.es_solo_links("mirá https://x.com/a")
    assert not B.es_solo_links("sumá el aguinaldo, mirá https://x.com/a porque es clave para las pymes hoy")
    assert not B.es_solo_links("vetá el 2")


# ── texto, links y audio ──────────────────────────────────────────────────────

def test_el_texto_de_leo_va_al_jefe_y_el_jefe_contesta_una_vez(h):
    fb, fbs = _fallback_registrador()
    m = Msg("vetá el 2")
    run(B.hacer_handler_texto(fb)(Upd(m), Ctx()))
    assert h.llamadas[-1] == ("deliberar", "vetá el 2", "texto")
    assert len(m.respuestas) == 1 and m.respuestas[0][1]["parse_mode"] == "HTML" and m.respuestas[0][1]["reply_markup"] == {"inline_keyboard": []}
    assert fbs == []                                            # no se coló en el flujo de «pegar un link = nota»


def test_un_link_pegado_entra_como_tema(h):
    fb, fbs = _fallback_registrador()
    run(B.hacer_handler_texto(fb)(Upd(Msg("https://www.ambito.com/aguinaldo-pymes")), Ctx()))
    assert h.llamadas[-1] == ("deliberar", "https://www.ambito.com/aguinaldo-pymes", "link") and fbs == []


def test_si_leo_esta_en_otro_flujo_o_el_bot_esta_en_pausa_el_mensaje_sigue_su_camino(h):
    fb, fbs = _fallback_registrador()
    run(B.hacer_handler_texto(fb)(Upd(Msg("Mi kicker")), Ctx({"awaiting_frase_header": "kicker"})))
    run(B.hacer_handler_texto(fb, pausado=lambda: True)(Upd(Msg("hola")), Ctx()))
    assert len(fbs) == 2 and not any(isinstance(x, tuple) and x[0] == "deliberar" for x in h.llamadas)


def test_si_la_reunion_se_cerro_justo_antes_el_mensaje_sigue_su_camino(h):
    h.respuesta = {"ok": False, "motivo": "sin_reunion_abierta"}
    fb, fbs = _fallback_registrador()
    run(B.hacer_handler_texto(fb)(Upd(Msg("hola")), Ctx()))
    assert len(fbs) == 1


def test_un_error_del_harness_no_se_traga_el_mensaje_en_silencio(h):
    def boom(*a, **k):
        raise RuntimeError("falló")
    h.jefe.deliberar = boom
    fb, fbs = _fallback_registrador()
    m = Msg("hola")
    run(B.hacer_handler_texto(fb)(Upd(m), Ctx()))
    assert "No pude procesar tu mensaje" in m.respuestas[0][0] and fbs == []


class Estado:
    def __init__(self):
        self.textos = []

    async def edit_text(self, t, **kw):
        self.textos.append(t)


class Media:
    file_unique_id = "abc123"
    file_name = None

    async def get_file(self, **kw):
        class F:
            async def download_to_drive(self_, path):
                open(path, "wb").write(b"ogg")
        return F()


class MsgVoz(Msg):
    def __init__(self):
        super().__init__(None, voice=Media())
        self.estado = Estado()

    async def reply_text(self, texto, **kw):
        self.respuestas.append((texto, kw))
        return self.estado


def test_el_audio_se_transcribe_con_el_mecanismo_del_bot_y_se_trata_como_texto(h):
    pedidos = []

    def transcribir(path, contexto):
        pedidos.append((os.path.exists(path), contexto))
        return "sumá una opinión sobre el riesgo país"
    fb, fbs = _fallback_registrador()
    m = MsgVoz()
    run(B.hacer_handler_voz(transcribir, fb)(Upd(m), Ctx()))
    assert pedidos and pedidos[0][0] is True and "agenda" in pedidos[0][1]
    assert h.llamadas[-1] == ("deliberar", "sumá una opinión sobre el riesgo país", "audio")
    assert any("Entendí: «sumá una opinión" in t for t in m.estado.textos)     # Leo ve qué se entendió
    assert fbs == []


def test_audio_que_no_se_puede_transcribir_no_llega_al_jefe(h):
    m = MsgVoz()
    run(B.hacer_handler_voz(lambda p, c: "", _fallback_registrador()[0])(Upd(m), Ctx()))
    assert not any(isinstance(x, tuple) and x[0] == "deliberar" for x in h.llamadas)
    assert any("No pude transcribir" in t for t in m.estado.textos)


def test_audio_con_otro_flujo_abierto_sigue_su_camino(h):
    fb, fbs = _fallback_registrador()
    run(B.hacer_handler_voz(lambda p, c: "x", fb)(Upd(MsgVoz()), Ctx({"awaiting_nota_manual": True})))
    assert len(fbs) == 1


# ── botones ───────────────────────────────────────────────────────────────────

class Query:
    orden = None                      # lista compartida con el FakeHarness (para probar que el callback se contesta ANTES del trabajo)

    def __init__(self, data):
        self.data = data
        self.respondida = None
        self.editado = None
        self.sin_teclado = False
        self.message = Msg("x")

    async def answer(self, texto="", **kw):
        self.respondida = texto
        if self.orden is not None:
            self.orden.append("answer")

    async def edit_message_text(self, texto, **kw):
        self.editado = (texto, kw)

    async def edit_message_reply_markup(self, reply_markup=None):
        self.sin_teclado = reply_markup is None


def test_boton_de_agenda_se_resuelve_en_el_harness_y_edita_el_mensaje(h):
    async def ff(*a):
        raise AssertionError
    q = Query("agd_ok:20261116")
    q.orden = h.orden
    run(B.hacer_handler_boton(ff)(Upd(query=q), Ctx()))
    assert ("boton", "agd_ok:20261116") in h.llamadas
    assert h.orden == ["answer", "harness:boton"]                       # primero se contesta el callback, después se trabaja
    assert q.editado[0] == "<b>Plan aprobado</b>" and q.editado[1]["parse_mode"] == "HTML"


def test_elegir_frase_pasa_por_el_flujo_de_frases_y_no_publica(h):
    vistas = []

    async def frase_flow(message, context, texto):
        vistas.append(texto)
    q = Query("agf_el:7")
    run(B.hacer_handler_boton(frase_flow)(Upd(query=q), Ctx()))
    assert vistas == ["«Una frase» — Javier Milei"]            # la placa sale por la vista previa de /frases (ahí Leo confirma)
    assert q.sin_teclado and "Elegiste a Javier Milei" in q.message.respuestas[0][0]
    q2 = Query("agf_el:99")
    run(B.hacer_handler_boton(frase_flow)(Upd(query=q2), Ctx()))
    assert len(vistas) == 1 and q2.message.respuestas[0][0] == "Esa frase ya no está."


def test_ninguna_frase_hoy_no_manda_nada(h):
    async def frase_flow(*a):
        raise AssertionError("no tiene que armar ninguna placa")
    q = Query("agf_no:20261116")
    run(B.hacer_handler_boton(frase_flow)(Upd(query=q), Ctx()))
    assert q.sin_teclado and q.message.respuestas[0][0] == "Hoy no sale ninguna frase."


def test_botones_con_la_bandera_en_0_no_hacen_nada(monkeypatch):
    monkeypatch.setenv("AGENDA_REUNION", "0")
    fh = FakeHarness()
    B.set_harness(fh)
    q = Query("agd_ok:20261116")
    monkeypatch.setattr(B, "harness", lambda: (_ for _ in ()).throw(AssertionError("con la bandera en 0 no se toca el harness")))
    run(B.hacer_handler_boton(lambda *a: None)(Upd(query=q), Ctx()))
    B.set_harness(None)
    assert q.respondida == "La reunión de agenda no está activa." and q.editado is None
    assert not any(isinstance(x, tuple) for x in fh.llamadas)


# ── registro en la aplicación ─────────────────────────────────────────────────

def test_registrar_agrega_handlers_que_solo_toman_lo_de_la_reunion():
    from telegram.ext import Application, CallbackQueryHandler, CommandHandler, MessageHandler
    app = Application.builder().token("123456:ABC-fake-token-for-tests").build()
    fb, _ = _fallback_registrador()
    B.registrar(app, transcribir=lambda p, c: "", fallback_texto=fb, fallback_media=fb, frase_flow=None)
    hs = app.handlers[0]
    assert [type(x).__name__ for x in hs] == ["CommandHandler", "CallbackQueryHandler", "MessageHandler", "MessageHandler"]
    cb = next(x for x in hs if isinstance(x, CallbackQueryHandler))
    assert cb.pattern.match("agd_ok:20261116") and cb.pattern.match("agf_el:7")
    for ajeno in ("edito_x", "pubx_1", "src_1", "nm_a", "desbloq_1", "vv1", "ev_process", "thread_1", "fap_1", "efem_1"):
        assert not cb.pattern.match(ajeno), ajeno             # no pisa ningún botón que ya existe
    assert next(x for x in hs if isinstance(x, CommandHandler)).commands == frozenset({"agenda"})


def test_main_py_registra_la_reunion_antes_de_handle_link_y_sin_tocar_comandos():
    raiz = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    src = open(os.path.join(raiz, "main.py"), encoding="utf-8").read()
    i_agd = src.index("_agenda_bot.registrar(")
    i_link = src.index("MessageHandler(filters.TEXT & ~filters.COMMAND & ~filters.UpdateType.EDITED, handle_link)")
    i_media = src.index("MessageHandler(filters.VOICE | filters.AUDIO | filters.VIDEO, handle_evento_media)")
    i_btn = src.index("app.add_handler(CallbackQueryHandler(handle_button))")
    assert i_agd < i_link < i_media and i_agd < i_btn
    assert src.count('CommandHandler("agenda"') == 0 and src.count('CommandHandler("frases", cmd_frases)') == 1   # /frases intacto


# ── integración con la copia del harness (si está al lado) ────────────────────

HARNESS = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), "agenda")


@pytest.mark.skipif(not os.path.exists(os.path.join(HARNESS, "agents", "jefe_redaccion.py")), reason="no está la copia del harness")
def test_integracion_con_el_harness_real_reinicio_incluido(tmp_path, monkeypatch):
    """Con el reloj inyectado: no depende de la hora real en que se corran las pruebas."""
    import datetime as _dt
    db = str(tmp_path / "h.db")
    monkeypatch.setenv("HARNESS_DB", db)
    monkeypatch.setenv("AGENDA_REUNION", "1")
    monkeypatch.setenv("OPENAI_API_KEY", "ci-sin-clave")
    monkeypatch.setattr(B, "HARNESS_DIR", HARNESS)
    B.set_harness(None)
    harness = B.harness()
    A = harness.agenda_dia
    ahora = _dt.datetime(2026, 11, 16, 8, 40, tzinfo=A.AR)
    B.set_reloj(lambda: ahora)
    fecha = A.fecha_de(ahora)
    A.abrir_reunion(fecha, db=db, ahora=ahora)
    A.guardar_items(fecha, [{"orden": 1, "tema": "Riesgo país y crédito", "capa": 2}, {"orden": 2, "tema": "Tía Maruca", "capa": 1}], db=db)
    real = harness.jefe.deliberar
    harness.jefe.deliberar = lambda texto, tipo="texto", ahora=None: real(texto, tipo, ahora=ahora, db=db)
    monkeypatch.setattr(A, "reunion_abierta", lambda ahora=None, db_=None, _f=A.reunion_abierta: _f(ahora, db=db))
    assert B.abierta() is True                                              # 8:40 del 16/11, reunión abierta
    m = Msg("vetá el 2")
    fb, fbs = _fallback_registrador()
    run(B.hacer_handler_texto(fb)(Upd(m), Ctx()))
    assert fbs == [] and "Vetado: 2." in m.respuestas[0][0]
    # «reinicio» del bot: se descarta todo lo que había en memoria; el estado sigue en la base
    B.set_harness(None)
    assert [i["estado"] for i in A.listar_items(fecha, db=db)] == ["propuesto", "vetado"]
    assert [x["rol"] for x in A.mensajes(fecha, db=db)] == ["leo", "jefe"]
    # a las 11:00 la reunión deja de capturar; y una reunión FALLIDA nunca captura
    B.set_reloj(lambda: ahora.replace(hour=11, minute=0))
    assert B.abierta() is False
    B.set_reloj(lambda: ahora)
    A.actualizar_reunion(fecha, db=db, estado="fallida")
    assert B.abierta() is False
    B.set_harness(None)
    B.set_reloj(None)


# ══ segunda pasada ═══════════════════════════════════════════════════════════

@pytest.mark.parametrize("clave", [
    "waiting_for_frase_ht", "waiting_for_frase_custom_hour", "waiting_for_frase_sched_ht", "waiting_for_edit_title",
    "waiting_for_new_source", "waiting_eco_title", "waiting_eco_bajada", "waiting_for_hashtags",
    "waiting_for_edit_photo", "waiting_for_custom_hour", "waiting_for_manual_text", "waiting_for_pre_sched_ht", "waiting_for_title",
    "awaiting_frase_header", "awaiting_nota_manual", "awaiting_ciclaje_n", "awaiting_vv_titulo", "awaiting_evt_insumos",
    "awaiting_tweet_directiva", "awaiting_evento_instr"])
def test_cada_clave_de_otro_flujo_que_usa_handle_link_deja_pasar_el_mensaje(h, clave):
    assert B.en_otro_flujo({clave: True}) is True and B.en_otro_flujo({clave: "algo"}) is True
    assert B.en_otro_flujo({clave: None}) is False and B.en_otro_flujo({clave: False}) is False      # pop() las deja en None/ausentes
    fb, fbs = _fallback_registrador()
    run(B.hacer_handler_texto(fb)(Upd(Msg("mi respuesta a otra pregunta")), Ctx({clave: True})))
    assert len(fbs) == 1 and not any(isinstance(x, tuple) and x[0] == "deliberar" for x in h.llamadas)


@pytest.mark.parametrize("clave", ["evento", "input_evento", "edito_rename_id", "edito_add_tema"])
def test_claves_exactas_de_otro_flujo(clave):
    assert B.en_otro_flujo({clave: {"x": 1}}) is True and B.en_otro_flujo({clave: 42}) is True
    assert B.en_otro_flujo({}) is False


def test_las_claves_waiting_y_awaiting_del_main_estan_cubiertas():
    """Cualquier clave `waiting*`/`awaiting*` que main.py use hoy o mañana queda cubierta por el prefijo."""
    import re
    raiz = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    claves = set(re.findall(r'["\']((?:waiting|awaiting)[a-z_0-9]*)["\']', open(os.path.join(raiz, "main.py"), encoding="utf-8").read()))
    assert len(claves) > 60
    assert all(B.en_otro_flujo({k: True}) for k in claves)


def test_el_trabajo_largo_de_cerrar_va_aparte_despues_de_contestar_el_boton(h):
    h.boton_resp = {"ok": True, "toast": "Agenda cerrada", "texto": "<b>Agenda cerrada</b>", "botones": None, "editar": True, "post_cierre": "2026-11-16"}
    h.demora_post_cierre = 0.05
    q = Query("agd_cerrar:20261116")
    q.orden = h.orden

    async def todo():
        await B.hacer_handler_boton(None)(Upd(query=q), Ctx())
        assert q.editado is not None and "post_cierre:fin" not in h.orden       # el mensaje ya se editó; lo largo todavía corre
        await asyncio.gather(*list(B._TAREAS))
    run(todo())
    assert h.orden == ["answer", "harness:boton", "post_cierre:inicio", "post_cierre:fin"]
    assert q.message.respuestas[-1][0] == "💬 La agenda cambió: te mandé las frases de nuevo."


def test_si_las_frases_tardan_mas_de_90_segundos_se_avisa_y_no_se_cuelga_el_bot(h, monkeypatch):
    monkeypatch.setattr(B, "PLAZO_POST_CIERRE_S", 0.05)
    h.boton_resp = {"ok": True, "texto": "<b>Agenda cerrada</b>", "botones": None, "editar": True, "post_cierre": "2026-11-16"}
    h.demora_post_cierre = 0.4
    q = Query("agd_cerrar:20261116")

    async def todo():
        await B.hacer_handler_boton(None)(Upd(query=q), Ctx())
        await asyncio.gather(*list(B._TAREAS))
    run(todo())
    assert "no alcanzaron a rehacerse" in q.message.respuestas[-1][0]


def test_cerrar_la_agenda_escribiendo_tambien_lanza_el_post_cierre_aparte(h):
    h.respuesta = {"ok": True, "texto": "<b>Agenda cerrada</b>", "botones": None, "post_cierre": "2026-11-16"}
    fb, _ = _fallback_registrador()
    m = Msg("cerrá la agenda")

    async def todo():
        await B.hacer_handler_texto(fb)(Upd(m), Ctx())
        await asyncio.gather(*list(B._TAREAS))
    run(todo())
    assert "post_cierre:fin" in h.orden and m.respuestas[0][0] == "<b>Agenda cerrada</b>"


def test_el_reloj_inyectado_llega_al_harness(h):
    import datetime as dt
    fijo = dt.datetime(2026, 11, 16, 8, 40, tzinfo=dt.timezone(dt.timedelta(hours=-3)))
    B.set_reloj(lambda: fijo)
    assert B.abierta() is True
    run(B.hacer_handler_texto(_fallback_registrador()[0])(Upd(Msg("hola")), Ctx()))
    assert h.relojes and all(r == fijo for r in h.relojes)


def test_partir_respuestas_de_mas_de_4096_caracteres():
    largo = "\n".join("línea %03d " % i + "x" * 90 for i in range(120))          # ~12.000 caracteres
    ps = B.partir(largo)
    assert len(ps) >= 3 and all(len(p) <= 4000 for p in ps) and "\n".join(ps) == largo
    assert B.partir("corto") == ["corto"]
    unica = B.partir("y" * 9000)                                                    # una sola línea gigante: corte duro
    assert [len(x) for x in unica] == [4000, 4000, 1000]


def test_una_respuesta_larga_sale_en_varios_mensajes_con_los_botones_en_el_ultimo(h):
    h.respuesta = {"ok": True, "texto": "\n".join("tema %d " % i + "z" * 100 for i in range(90)), "botones": {"inline_keyboard": [[{"text": "x", "callback_data": "y"}]]}}
    m = Msg("hola")
    run(B.hacer_handler_texto(_fallback_registrador()[0])(Upd(m), Ctx()))
    assert len(m.respuestas) >= 3 and all(len(t) <= 4096 for t, _ in m.respuestas)
    assert all(kw["reply_markup"] is None for _, kw in m.respuestas[:-1]) and m.respuestas[-1][1]["reply_markup"] is not None


def test_un_boton_con_texto_de_mas_de_4096_no_edita_manda_en_pedazos(h):
    h.boton_resp = {"ok": True, "toast": "x", "texto": "\n".join("l%d " % i + "w" * 80 for i in range(100)), "botones": None, "editar": True}
    q = Query("agd_ok:20261116")
    run(B.hacer_handler_boton(None)(Upd(query=q), Ctx()))
    assert q.editado is None and len(q.message.respuestas) >= 2


def _upd_json(**kw):
    from telegram import Update
    base = {"message_id": 1, "date": 0, "chat": {"id": 1, "type": kw.pop("chat", "private")}, "from": {"id": 1, "is_bot": False, "first_name": "L"}}
    base.update(kw)
    clave = "edited_message" if base.pop("_editado", False) else "message"
    return Update.de_json({"update_id": 1, clave: base}, None)


def test_los_handlers_son_del_chat_privado_y_no_toman_mensajes_editados(h):
    from telegram.ext import Application, MessageHandler
    app = Application.builder().token("123456:ABC-fake-token-for-tests").build()
    fb, _ = _fallback_registrador()
    B.registrar(app, transcribir=lambda p, c: "", fallback_texto=fb, fallback_media=fb, frase_flow=None)
    texto_h, voz_h = [x for x in app.handlers[0] if isinstance(x, MessageHandler)]
    voz = {"voice": {"file_id": "a", "file_unique_id": "b", "duration": 1}}
    assert texto_h.check_update(_upd_json(text="hola"))
    assert not texto_h.check_update(_upd_json(text="hola", chat="supergroup"))          # un grupo no es la reunión
    assert not texto_h.check_update(_upd_json(text="hola", _editado=True))              # un mensaje editado tampoco
    assert not texto_h.check_update(_upd_json(text="/vivo", entities=[{"type": "bot_command", "offset": 0, "length": 5}]))                            # los comandos no
    assert voz_h.check_update(_upd_json(**voz))
    assert not voz_h.check_update(_upd_json(chat="group", **voz))
    assert not voz_h.check_update(_upd_json(_editado=True, **voz))
    h.abierta = False                                                                   # sin reunión abierta, ninguno toma nada
    assert not texto_h.check_update(_upd_json(text="hola")) and not voz_h.check_update(_upd_json(**voz))
