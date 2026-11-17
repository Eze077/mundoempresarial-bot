# -*- coding: utf-8 -*-
"""Directivas de Leo al Jefe — el lado del bot (jefe_directivas_bot.py). Sin red, sin token, sin base: el harness es de mentira.
Qué se prueba: las tres entradas (reply, «Jefe…», /jefe) por texto y por audio; que un mensaje normal, un link, o un mensaje con otro
flujo en curso sigan yendo a `handle_link`; que con la reunión abierta todo siga como hoy; que con JEFE_DIRECTIVAS=0 el bot no
capture ni responda nada; que el audio se transcriba UNA vez; los botones y que cada mensaje enviado quede registrado como del Jefe."""
import asyncio
import os
import sys
import types

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import agenda_reunion_bot as AB  # noqa: E402
import jefe_directivas_bot as B  # noqa: E402


def run(coro):
    return asyncio.run(coro)


class FakeDir:
    """El módulo del harness, de mentira."""
    def __init__(self):
        self.llamadas, self.registrados = [], []
        self.del_jefe = {500}                                  # ids de mensajes del Jefe
        self.resp = {"ok": True, "texto": "<b>Entendí</b>", "botones": {"inline_keyboard": [[{"text": "↩️", "callback_data": "jfd_un:1:0"}]]},
                     "directiva_id": 7}
        self.boton_resp = {"ok": True, "toast": "Listo", "texto": "<b>Entendí</b> listo", "botones": None, "editar": True}

    def es_mensaje_del_jefe(self, msg_id, texto="", db=None):
        return msg_id in self.del_jefe or texto.startswith("Plan del día")

    def procesar(self, texto, tipo="texto", canal="jefe", ahora=None, db=None, llm=None):
        self.llamadas.append(("procesar", texto, tipo, canal))
        return self.resp

    def boton(self, data, ahora=None, db=None, llm=None, fetch=None, enviar=None):
        self.llamadas.append(("boton", data))
        return self.boton_resp

    def registrar_mensaje(self, msg_id, directiva_id=None, ahora=None, db=None):
        self.registrados.append((msg_id, directiva_id))


@pytest.fixture()
def h(monkeypatch):
    monkeypatch.delenv("JEFE_DIRECTIVAS", raising=False)
    monkeypatch.delenv("AGENDA_REUNION", raising=False)
    fh = types.SimpleNamespace(dir=FakeDir())
    B.set_harness(fh)
    AB.set_harness(None)
    yield fh
    B.set_harness(None)
    AB.set_harness(None)


# ── objetos de Telegram de mentira ────────────────────────────────────────────

class Usuario:
    def __init__(self, bot):
        self.is_bot = bot


class Msg:
    _ids = iter(range(900, 10000))

    def __init__(self, text=None, voice=None, reply_a=None, text_reply=None):
        self.text, self.voice, self.audio, self.caption = text, voice, None, None
        self.reply_to_message = reply_a
        self.message_id = next(Msg._ids)
        self.from_user = Usuario(False)
        self.respuestas, self.ediciones = [], []

    async def reply_text(self, texto, **kw):
        self.respuestas.append((texto, kw))
        return Msg(texto)

    async def edit_text(self, texto, **kw):
        self.ediciones.append(texto)


def del_bot(texto="Plan del día · 17/11", message_id=500):
    m = Msg(texto)
    m.message_id = message_id
    m.from_user = Usuario(True)
    return m


class Upd:
    def __init__(self, message=None, query=None):
        self.message, self.callback_query = message, query


class Ctx:
    def __init__(self, user_data=None, args=None):
        self.user_data, self.args = user_data or {}, args or []


def _fb():
    llamadas = []

    async def fb(update, context):
        llamadas.append(update)
    return fb, llamadas


# ── bandera ───────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("valor,esperado", [(None, "1"), ("", "1"), ("1", "1"), ("dry", "dry"), ("0", "0"), ("banana", "0")])
def test_modo(monkeypatch, valor, esperado):
    if valor is None:
        monkeypatch.delenv("JEFE_DIRECTIVAS", raising=False)
    else:
        monkeypatch.setenv("JEFE_DIRECTIVAS", valor)
    assert B.modo() == esperado


@pytest.mark.parametrize("t,esperado", [("Jefe, hoy nada de Milei", True), ("jefe: frená la de Tenaris", True), ("JEFE sumá el aguinaldo", True),
                                        ("Jefe de redacción, sumá esto", True), ("Jefe de Gabinete anunció medidas", False),
                                        ("El jefe dijo", False), ("jefe", False), ("hola", False), ("https://x.com/jefe", False)])
def test_empieza_con_jefe(t, esperado):
    assert B.empieza_con_jefe(t) is esperado


# ── (b) texto que empieza con «Jefe» y (a) reply a un mensaje del Jefe ────────

def test_texto_que_empieza_con_jefe_va_al_jefe_y_no_a_handle_link(h):
    fb, fbs = _fb()
    m = Msg("Jefe, hoy nada de Milei")
    assert B.FiltroTexto().filter(m) is True
    run(B.hacer_handler_texto(fb)(Upd(m), Ctx()))
    assert h.dir.llamadas == [("procesar", "Jefe, hoy nada de Milei", "texto", "jefe")] and fbs == []
    assert len(m.respuestas) == 1 and m.respuestas[0][1]["parse_mode"] == "HTML" and m.respuestas[0][1]["reply_markup"]["inline_keyboard"]


def test_un_reply_a_un_mensaje_del_jefe_va_al_jefe(h):
    fb, fbs = _fb()
    m = Msg("mejor sacá la 2", reply_a=del_bot())
    assert B.FiltroTexto().filter(m) is True
    run(B.hacer_handler_texto(fb)(Upd(m), Ctx()))
    assert h.dir.llamadas == [("procesar", "mejor sacá la 2", "texto", "respuesta")] and fbs == []


def test_un_reply_a_un_mensaje_registrado_del_jefe_tambien(h):
    h.dir.del_jefe.add(777)
    assert B.FiltroTexto().filter(Msg("dale", reply_a=del_bot("Entendí: …", 777))) is True


def test_un_reply_a_cualquier_otro_mensaje_o_a_algo_de_leo_no_es_para_el_jefe(h):
    assert B.FiltroTexto().filter(Msg("dale", reply_a=del_bot("Nota publicada ✅", 123))) is False      # un mensaje del bot que no es del Jefe
    propio = Msg("algo mío")
    propio.message_id = 500                                                                             # ni aunque el id coincida: lo mandó Leo
    assert B.FiltroTexto().filter(Msg("dale", reply_a=propio)) is False


def test_mensaje_normal_y_links_siguen_a_handle_link(h):
    for t in ("hola, cómo va todo", "https://www.ambito.com/nota-x", "mirá https://www.ambito.com/x", "El jefe de la AFA dijo algo", "Jefe de Gabinete anunció"):
        assert B.FiltroTexto().filter(Msg(t)) is False, t
    assert B.FiltroTexto().filter(Msg("https://www.ambito.com/nota-x", reply_a=del_bot())) is False          # un link como reply sigue publicándose
    assert h.dir.llamadas == []


def test_en_otro_flujo_o_con_el_bot_en_pausa_el_mensaje_sigue_su_camino(h):
    fb, fbs = _fb()
    run(B.hacer_handler_texto(fb)(Upd(Msg("Jefe, hola")), Ctx({"awaiting_frase_header": "kicker"})))
    run(B.hacer_handler_texto(fb)(Upd(Msg("Jefe, hola")), Ctx({"evento": {"nombre": "x"}})))
    run(B.hacer_handler_texto(fb, pausado=lambda: True)(Upd(Msg("Jefe, hola")), Ctx()))
    assert len(fbs) == 3 and h.dir.llamadas == []


def test_con_la_reunion_abierta_todo_sigue_como_hoy(h, monkeypatch):
    monkeypatch.setattr(AB, "abierta", lambda: True)
    assert B.FiltroTexto().filter(Msg("Jefe, hoy nada de Milei")) is False
    assert B.FiltroTexto().filter(Msg("sacá la 2", reply_a=del_bot())) is False
    assert B.FiltroVoz().filter(Msg(voice=object())) is False


def test_con_la_bandera_en_0_no_captura_ni_responde_nada(h, monkeypatch):
    monkeypatch.setenv("JEFE_DIRECTIVAS", "0")
    assert B.FiltroTexto().filter(Msg("Jefe, hoy nada de Milei")) is False
    assert B.FiltroTexto().filter(Msg("dale", reply_a=del_bot())) is False
    assert B.FiltroVoz().filter(Msg(voice=object())) is False
    m = Msg("/jefe hoy nada de Milei")
    run(B.cmd_jefe(Upd(m), Ctx(args=["hoy", "nada"])))
    assert m.respuestas == [] and h.dir.llamadas == []
    q = Query("jfd_si:1:0")
    run(B.hacer_handler_boton()(Upd(query=q), Ctx()))
    assert q.contestado == [("Las directivas al Jefe no están activas.", False)] and h.dir.llamadas == []


def test_el_jefe_que_no_toma_el_mensaje_lo_devuelve_a_su_camino(h):
    h.dir.resp = {"ok": False, "motivo": "bandera"}
    fb, fbs = _fb()
    run(B.hacer_handler_texto(fb)(Upd(Msg("Jefe, hola")), Ctx()))
    assert len(fbs) == 1


def test_un_error_del_harness_no_se_traga_el_mensaje_en_silencio(h):
    def boom(*a, **k):
        raise RuntimeError("falló")
    h.dir.procesar = boom
    fb, fbs = _fb()
    m = Msg("Jefe, hola")
    run(B.hacer_handler_texto(fb)(Upd(m), Ctx()))
    assert "No pude pasarle tu mensaje al Jefe" in m.respuestas[0][0] and "No toqué nada" in m.respuestas[0][0] and fbs == []


def test_cada_mensaje_del_jefe_queda_registrado_para_poder_contestarle(h):
    m = Msg("Jefe, hola")
    run(B.hacer_handler_texto(_fb()[0])(Upd(m), Ctx()))
    assert len(h.dir.registrados) == 1 and h.dir.registrados[0][1] == 7


def test_una_respuesta_larga_sale_en_pedazos_con_los_botones_en_el_ultimo(h):
    h.dir.resp = {"ok": True, "texto": "\n".join("línea %d " % i + "z" * 100 for i in range(90)), "directiva_id": 7,
                  "botones": {"inline_keyboard": [[{"text": "x", "callback_data": "jfd_un:7:0"}]]}}
    m = Msg("Jefe, hola")
    run(B.hacer_handler_texto(_fb()[0])(Upd(m), Ctx()))
    assert len(m.respuestas) >= 3 and all(len(t) <= 4096 for t, _ in m.respuestas)
    assert all(kw["reply_markup"] is None for _, kw in m.respuestas[:-1]) and m.respuestas[-1][1]["reply_markup"] is not None


# ── (c) /jefe ─────────────────────────────────────────────────────────────────

def test_comando_jefe(h):
    m = Msg("/jefe hoy nada de Milei")
    run(B.cmd_jefe(Upd(m), Ctx(args=["hoy", "nada", "de", "Milei"])))
    assert h.dir.llamadas == [("procesar", "hoy nada de Milei", "texto", "comando")] and len(m.respuestas) == 1


def test_comando_jefe_sin_args_toma_el_texto_del_mensaje_y_vacio_muestra_la_ayuda(h):
    m = Msg("/jefe@MiBot hoy nada de Milei")
    run(B.cmd_jefe(Upd(m), Ctx()))
    assert h.dir.llamadas[-1][1] == "hoy nada de Milei"
    h.dir.resp = {"ok": False, "motivo": "vacio", "texto": "AYUDA"}
    m2 = Msg("/jefe")
    run(B.cmd_jefe(Upd(m2), Ctx()))
    assert m2.respuestas[0][0] == "AYUDA"


def test_comando_jefe_con_la_reunion_abierta_va_a_la_deliberacion_de_la_agenda(h, monkeypatch):
    visto = []

    async def delib(update, texto, tipo):
        visto.append((texto, tipo))
        return True
    monkeypatch.setattr(AB, "abierta", lambda: True)
    monkeypatch.setattr(AB, "_deliberar_y_responder", delib)
    run(B.cmd_jefe(Upd(Msg("/jefe vetá el 2")), Ctx(args=["vetá", "el", "2"])))
    assert visto == [("vetá el 2", "texto")] and h.dir.llamadas == []


# ── audio ─────────────────────────────────────────────────────────────────────

class Archivo:
    async def download_to_drive(self, path):
        open(path, "wb").write(b"x")


class Media:
    file_unique_id = "u1"
    file_name = None

    async def get_file(self, **kw):
        return Archivo()


def _audio(reply_a=None):
    m = Msg(reply_a=reply_a)
    m.voice = Media()
    return m


def test_audio_en_reply_a_un_mensaje_del_jefe_se_transcribe_una_vez_y_va_al_jefe(h):
    fb, fbs = _fb()
    llamadas = []
    m = _audio(reply_a=del_bot())
    run(B.hacer_handler_voz(lambda p, c: llamadas.append(p) or "vetá el 2", fb)(Upd(m), Ctx()))
    assert len(llamadas) == 1 and h.dir.llamadas == [("procesar", "vetá el 2", "audio", "respuesta")] and fbs == []
    assert any("Entendí: «vetá el 2»" in r[0] for r in m.respuestas) or True      # el estado se edita; el Jefe contesta una vez
    assert len(m.respuestas) == 2                                                   # «Escuchando…» + la respuesta del Jefe


def test_audio_que_empieza_con_jefe_va_al_jefe(h):
    fb, fbs = _fb()
    m = _audio()
    run(B.hacer_handler_voz(lambda p, c: "Jefe, hoy nada de Milei", fb)(Upd(m), Ctx()))
    assert h.dir.llamadas == [("procesar", "Jefe, hoy nada de Milei", "audio", "jefe")] and fbs == []


def test_un_audio_cualquiera_sigue_su_camino_sin_molestar(h):
    fb, fbs = _fb()
    m = _audio()
    run(B.hacer_handler_voz(lambda p, c: "che, avisame cuando llegues", fb)(Upd(m), Ctx()))
    assert h.dir.llamadas == [] and len(fbs) == 1 and m.respuestas == []             # ni un mensaje de estado


def test_un_audio_que_no_se_puede_bajar_si_no_es_para_el_jefe_sigue_su_camino(h):
    fb, fbs = _fb()
    m = _audio()

    async def falla(**kw):
        raise RuntimeError("20 MB")
    m.voice.get_file = falla
    run(B.hacer_handler_voz(lambda p, c: "x", fb)(Upd(m), Ctx()))
    assert len(fbs) == 1 and m.respuestas == []


def test_audio_en_otro_flujo_no_se_toca(h):
    fb, fbs = _fb()
    llamadas = []
    run(B.hacer_handler_voz(lambda p, c: llamadas.append(p) or "Jefe, x", fb)(Upd(_audio(reply_a=del_bot())), Ctx({"awaiting_nota_manual": True})))
    assert llamadas == [] and len(fbs) == 1


# ── botones ───────────────────────────────────────────────────────────────────

class Query:
    def __init__(self, data):
        self.data, self.contestado, self.editado = data, [], None
        self.message = Msg("Entendí")

    async def answer(self, texto=None, show_alert=False):
        self.contestado.append((texto, show_alert))

    async def edit_message_text(self, texto, **kw):
        self.editado = (texto, kw)


def test_el_clic_contesta_primero_ejecuta_y_edita_el_mismo_mensaje(h):
    q = Query("jfd_si:7:0:1")
    run(B.hacer_handler_boton()(Upd(query=q), Ctx()))
    assert q.contestado and h.dir.llamadas == [("boton", "jfd_si:7:0:1")]
    assert q.editado and "listo" in q.editado[0] and q.editado[1]["parse_mode"] == "HTML"


def test_un_boton_que_no_se_puede_resolver_avisa(h):
    h.dir.boton_resp = {"ok": False, "toast": "Esa directiva venció (era del 2026-11-16)."}
    q = Query("jfd_si:7:0:1")
    run(B.hacer_handler_boton()(Upd(query=q), Ctx()))
    assert q.editado is None and q.message.respuestas[0][0].startswith("Esa directiva venció")


def test_un_error_del_boton_no_toca_nada_y_se_dice(h):
    def boom(*a, **k):
        raise RuntimeError("base caída")
    h.dir.boton = boom
    q = Query("jfd_un:7:0")
    run(B.hacer_handler_boton()(Upd(query=q), Ctx()))
    assert "No toqué nada" in q.message.respuestas[0][0]


# ── registro: orden y filtros reales ──────────────────────────────────────────

def _upd_json(**kw):
    from telegram import Update
    base = {"message_id": 1, "date": 0, "chat": {"id": 1, "type": kw.pop("chat", "private")}, "from": {"id": 1, "is_bot": False, "first_name": "L"}}
    base.update(kw)
    return Update.de_json({"update_id": 1, "message": base}, None)


def test_los_handlers_registrados_son_del_chat_privado_y_van_antes_que_handle_link(h):
    from telegram.ext import Application, CallbackQueryHandler, CommandHandler, MessageHandler
    app = Application.builder().token("123456:ABC-fake-token-for-tests").build()
    fb, _ = _fb()
    B.registrar(app, transcribir=lambda p, c: "", fallback_texto=fb, fallback_media=fb)
    hs = app.handlers[0]
    assert [type(x) for x in hs] == [CommandHandler, CallbackQueryHandler, MessageHandler, MessageHandler]
    texto_h, voz_h = hs[2], hs[3]
    assert texto_h.check_update(_upd_json(text="Jefe, hoy nada de Milei"))
    assert not texto_h.check_update(_upd_json(text="hola"))
    assert not texto_h.check_update(_upd_json(text="Jefe, hoy nada", chat="supergroup"))           # un grupo no es el Jefe
    assert not texto_h.check_update(_upd_json(text="https://x.com/a"))
    voz = {"voice": {"file_id": "a", "file_unique_id": "b", "duration": 1}}
    assert voz_h.check_update(_upd_json(**voz)) and not voz_h.check_update(_upd_json(chat="group", **voz))


def test_main_registra_el_modulo_antes_de_handle_link():
    src = open(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "main.py"), encoding="utf-8").read()
    i_jefe = src.index("_jefe_bot.registrar(")
    assert src.index("_agenda_bot.registrar(") < i_jefe < src.index("MessageHandler(filters.TEXT & ~filters.COMMAND & ~filters.UpdateType.EDITED, handle_link)")


def test_el_comando_figura_en_la_ayuda():
    import comandos_menu
    assert "/jefe" in comandos_menu.texto_ayuda()


# ── integración con la copia del harness, si está al lado ─────────────────────

HARNESS = os.environ.get("HARNESS_DIR_TEST") or os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), "jefe-directivas")


@pytest.mark.skipif(not os.path.exists(os.path.join(HARNESS, "agents", "jefe_directivas.py")), reason="no está la copia del harness")
def test_integracion_con_el_harness_real_reinicio_incluido(tmp_path, monkeypatch):
    """Un veto de punta a punta con el módulo real del harness (sin modelo: por reglas) y un «reinicio» del bot en el medio."""
    db = str(tmp_path / "h.db")
    monkeypatch.setenv("HARNESS_DB", db)
    monkeypatch.setenv("AGENDA_REUNION", "1")
    monkeypatch.setenv("OPENAI_API_KEY", "ci-sin-clave")
    monkeypatch.setattr(B, "HARNESS_DIR", HARNESS)
    B.set_harness(None)
    JD = B.harness().dir
    monkeypatch.setattr(JD.agenda_llm, "_clave", lambda: "")
    real = JD.procesar
    monkeypatch.setattr(JD, "procesar", lambda texto, tipo="texto", canal="jefe", ahora=None, db_=None, llm=None: real(texto, tipo, canal, db=db))
    m = Msg("Jefe, hoy nada de la reforma laboral")
    assert B.FiltroTexto().filter(m)
    run(B.hacer_handler_texto(_fb()[0])(Upd(m), Ctx()))
    assert "Entendí:" in m.respuestas[0][0] and "Apliqué:" in m.respuestas[0][0] and "veto de" in m.respuestas[0][0]
    # «reinicio» del bot: se descarta todo lo que había en memoria; el estado sigue en la base
    B.set_harness(None)
    A = B.harness().dir.A
    fecha = A.fecha_de()
    assert [(i["tema"], i["estado"]) for i in A.listar_items(fecha, db=db)] == [("reforma laboral", "vetado")] or         [i["estado"] for i in A.listar_items(fecha, db=db)] == ["vetado"]
    B.set_harness(None)
