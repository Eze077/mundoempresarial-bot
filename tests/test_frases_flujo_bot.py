# -*- coding: utf-8 -*-
"""/frases (corregir el texto y publicar) y /publicador → Borrar, de punta a punta con el main.py REAL y un Telegram, un
harness y unas redes de mentira (17/11/2026). Sin red, sin claves.

Lo que se prueba:
  · el texto corregido llega a la placa, a lo que se publica y a la vista previa; si la placa no sale, no cambia nada;
  · la vista previa vieja deja de servir; un «mandame el texto» colgado no se come el próximo mensaje ni deja publicar a ciegas;
  · al publicar una frase se guardan los ids de LinkedIn (página) e Instagram (feed e historia) en la nota;
  · /publicador → Borrar ofrece todas las redes, borra lo elegido y le dice a Leo red por red qué pasó."""
import asyncio
import os
import sys
import types

import pytest

RAIZ_BOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, RAIZ_BOT)


@pytest.fixture
def main(monkeypatch):
    for k in ("TELEGRAM_TOKEN", "WP_USER", "WP_PASS"):
        monkeypatch.setenv(k, "falso")
    import main as m
    return m


def run(coro):
    return asyncio.run(coro)


# ── Telegram de mentira ──────────────────────────────────────────────────────

class Msg:
    _n = 100

    def __init__(self, chat_id=7, texto="", foto=False, enviados=None):
        Msg._n += 1
        self.message_id = Msg._n
        self.chat_id = chat_id
        self.text = texto
        self.photo = [object()] if foto else []
        self.chat = types.SimpleNamespace(type="private")
        self.enviados = enviados if enviados is not None else []

    async def reply_text(self, texto, **kw):
        self.enviados.append(("texto", texto, kw))
        return Msg(self.chat_id, texto, enviados=self.enviados)

    async def reply_photo(self, photo=None, caption=None, **kw):
        m = Msg(self.chat_id, caption or "", foto=True, enviados=self.enviados)
        self.enviados.append(("foto", caption, dict(kw, foto=photo.getvalue() if photo else b"", id=m.message_id)))
        return m

    async def delete(self):
        pass


class Bot:
    def __init__(self):
        self.ediciones = []

    async def edit_message_caption(self, chat_id=None, message_id=None, caption=None, **kw):
        self.ediciones.append(("caption", message_id, caption))

    async def edit_message_reply_markup(self, chat_id=None, message_id=None, reply_markup=None, **kw):
        self.ediciones.append(("teclado", message_id, reply_markup))

    async def send_message(self, chat_id=None, text=None, **kw):
        self.ediciones.append(("mensaje", chat_id, text))


class Ctx:
    def __init__(self, user_data=None):
        self.user_data = user_data if user_data is not None else {}
        self.bot = Bot()
        self.chat_data = {}
        self.application = types.SimpleNamespace(job_queue=types.SimpleNamespace(run_once=lambda *a, **k: None))


class Consulta:
    def __init__(self, data, mensaje):
        self.data = data
        self.message = mensaje
        self.respuestas, self.ediciones = [], []

    async def answer(self, texto=None, show_alert=False, **kw):
        self.respuestas.append((texto, show_alert))

    async def edit_message_caption(self, caption=None, **kw):
        self.ediciones.append(("caption", caption, kw))

    async def edit_message_text(self, texto=None, **kw):
        self.ediciones.append(("texto", texto, kw))

    async def edit_message_reply_markup(self, reply_markup=None, **kw):
        self.ediciones.append(("teclado", None, dict(kw, reply_markup=reply_markup)))


def upd_msg(texto):
    m = Msg(texto=texto)
    return types.SimpleNamespace(message=m, callback_query=None, effective_user=types.SimpleNamespace(id=1)), m


def upd_btn(data, mensaje):
    q = Consulta(data, mensaje)
    return types.SimpleNamespace(callback_query=q, message=None, effective_user=types.SimpleNamespace(id=1)), q


# ── generador de placas y harness de mentira ─────────────────────────────────

@pytest.fixture
def placas(monkeypatch):
    """frases_gen sin imagen base: la «placa» son los bytes del texto, así se ve QUÉ texto trae."""
    import frases_gen
    llamadas = []

    def gen(frase, kicker=None, tag=None):
        llamadas.append((frase, kicker, tag))
        return ("PLACA[%s|%s|%s]" % (frase, kicker, tag)).encode()

    monkeypatch.setattr(frases_gen, "generate_frase_image", gen)
    monkeypatch.setattr(frases_gen, "pad_placa", lambda png, w, h: png + (b"@%dx%d" % (w, h)))
    return llamadas


@pytest.fixture
def harness(monkeypatch):
    """agents.frases.publish y agents.redes.ig_publicar_bytes de mentira; anotan lo que reciben."""
    pub, ig = [], []
    agents = types.ModuleType("agents")
    agents.__path__ = []
    frases = types.ModuleType("agents.frases")

    def publish(frase, img_bytes, tw_on=True, tg_on=True, hashtags=""):
        pub.append({"frase": frase, "img": img_bytes})
        return {"wp_id": 57490, "wp_link": "https://mundoempresarial.ar/nota/", "tg_msg_id": 14583, "job_id": 1}
    frases.publish = publish
    redes = types.ModuleType("agents.redes")

    def ig_publicar_bytes(png, caption, story=False, titulo=""):
        ig.append({"story": story, "png": png})
        return {"ok": True, "id": "IGSTORY1" if story else "IGFEED1"}
    redes.ig_publicar_bytes = ig_publicar_bytes
    agents.frases, agents.redes = frases, redes
    for nombre, mod in (("agents", agents), ("agents.frases", frases), ("agents.redes", redes)):
        monkeypatch.setitem(sys.modules, nombre, mod)
    return types.SimpleNamespace(publicaciones=pub, ig=ig)


def frase_pendiente(main, texto="Hay quienes leen para crecer Walter Peitna"):
    fp = {"texto": texto, "tw_on": True, "tg_on": True, "wp_on": True, "igf_on": True, "igs_on": True, "wa_on": False,
          "li_on": True, "rev": 1}
    fp["img_bytes"] = ("PLACA[%s|None|None]" % texto).encode()
    fp["img_clave"] = main._frase_clave_placa(fp)
    return fp


# ══ 1. el texto corregido llega a todo ═══════════════════════════════════════

def test_el_texto_corregido_llega_a_la_placa_y_a_la_vista_previa(main, placas, monkeypatch):
    async def sin_correccion(update, texto):
        return texto
    monkeypatch.setattr(main, "_ortografia_corto", sin_correccion)
    fp = frase_pendiente(main)
    vieja = Msg()
    fp["preview"] = (vieja.chat_id, vieja.message_id)
    ctx = Ctx({"frase_pending": fp, "awaiting_frase_header": "texto"})
    upd, m = upd_msg("Hay quienes leen para crecer Walter Peitina")

    run(main.handle_link(upd, ctx))

    assert fp["texto"] == "Hay quienes leen para crecer Walter Peitina"
    assert b"Walter Peitina" in fp["img_bytes"] and b"Peitna" not in fp["img_bytes"]         # la placa nueva, no la vieja
    assert fp["img_clave"] == main._frase_clave_placa(fp) and fp["rev"] == 2
    assert "awaiting_frase_header" not in ctx.user_data
    fotos = [e for e in m.enviados if e[0] == "foto"]
    assert len(fotos) == 1 and "Walter Peitina" in fotos[0][1] and "versión 2" in fotos[0][1]   # la vista previa muestra el texto FINAL
    assert b"Walter Peitina" in fotos[0][2]["foto"]
    assert fp["preview"] == (m.chat_id, fotos[0][2]["id"])
    assert any(e[0] == "caption" and e[1] == vieja.message_id for e in ctx.bot.ediciones)   # la vista previa anterior se retira


def test_la_correccion_de_leo_sale_tal_cual_aunque_el_corrector_sugiera_otra_cosa(main, placas, monkeypatch):
    # 17/11: el corrector automatico podia volver a meter el error que Leo estaba sacando (un nombre propio «corregido»)
    monkeypatch.setattr(main, "_ortografia_cambios", lambda t: [{"de": "Peitina", "a": "Peitna"}])
    fp = frase_pendiente(main)
    ctx = Ctx({"frase_pending": fp, "awaiting_frase_header": "texto"})
    upd, m = upd_msg("Hay quienes leen para crecer Walter Peitina")

    run(main.handle_link(upd, ctx))

    assert fp["texto"] == "Hay quienes leen para crecer Walter Peitina"
    assert b"Walter Peitina" in fp["img_bytes"]
    assert any(e[0] == "texto" and "no lo apliqué" in e[1] for e in m.enviados)     # solo avisa


def test_si_la_placa_no_sale_no_cambia_nada_y_se_puede_reintentar(main, placas, monkeypatch):
    async def sin_correccion(update, texto):
        return texto
    monkeypatch.setattr(main, "_ortografia_corto", sin_correccion)
    import frases_gen
    monkeypatch.setattr(frases_gen, "generate_frase_image", lambda *a, **k: (_ for _ in ()).throw(FileNotFoundError("sin base")))
    fp = frase_pendiente(main)
    antes = dict(fp)
    ctx = Ctx({"frase_pending": fp, "awaiting_frase_header": "texto"})
    upd, m = upd_msg("texto nuevo")

    run(main.handle_link(upd, ctx))

    assert fp == antes                                              # NI el texto NI la placa: antes quedaba texto nuevo con placa vieja
    assert ctx.user_data["awaiting_frase_header"] == "texto"        # puede mandarlo de nuevo
    assert any("NO cambié nada" in e[1] for e in m.enviados if e[0] == "texto")


def test_corregir_kicker_o_etiqueta_tambien_regenera_la_placa(main, placas, monkeypatch):
    async def sin_correccion(update, texto):
        return texto
    monkeypatch.setattr(main, "_ortografia_corto", sin_correccion)
    fp = frase_pendiente(main)
    ctx = Ctx({"frase_pending": fp, "awaiting_frase_header": "kicker"})
    run(main.handle_link(upd_msg("Día de la Pyme")[0], ctx))
    assert fp["kicker"] == "Día de la Pyme" and b"D\xc3\xada de la Pyme" in fp["img_bytes"]


def test_publicar_manda_el_texto_y_la_placa_corregidos_a_todas_las_redes_y_guarda_los_ids(main, placas, harness, monkeypatch):
    async def sin_correccion(update, texto):
        return texto
    monkeypatch.setattr(main, "_ortografia_corto", sin_correccion)
    guardados = []
    monkeypatch.setattr(main, "append_social_meta", lambda *a, **k: guardados.append((a, k)) or True)
    monkeypatch.setattr(main, "post_linkedin", lambda data, url: (setattr(main, "_LAST_LINKEDIN_URN", "") or None))
    monkeypatch.setattr(main, "post_linkedin_org",
                        lambda data, url: (setattr(main, "_LAST_LINKEDIN_ORG_URN", "urn:li:share:555") or "https://li/555"))
    fp = frase_pendiente(main)
    fp["wa_on"] = False
    vieja = Msg()
    fp["preview"] = (vieja.chat_id, vieja.message_id)
    ctx = Ctx({"frase_pending": fp, "awaiting_frase_header": "texto"})
    run(main.handle_link(upd_msg("Hay quienes leen para crecer Walter Peitina")[0], ctx))
    nueva = fp["preview"]

    msg_prev = Msg(chat_id=nueva[0])
    msg_prev.message_id = nueva[1]
    upd, q = upd_btn("frase_pub", msg_prev)
    run(main.handle_button(upd, ctx))

    # lo que se publicó es lo corregido, en la nota (WP/TG) y en las placas de Instagram
    assert harness.publicaciones == [{"frase": "Hay quienes leen para crecer Walter Peitina",
                                      "img": fp["img_bytes"]}]
    assert b"Walter Peitina" in harness.publicaciones[0]["img"] and b"Peitna" not in harness.publicaciones[0]["img"]
    assert [i["story"] for i in harness.ig] == [False, True] and all(b"Walter Peitina" in i["png"] for i in harness.ig)
    # los ids de TODAS las redes quedan anotados en la nota (antes solo el de Telegram)
    ids = {}
    for a, k in guardados:
        ids.update(k)
        if len(a) > 3 and a[3]:
            ids["tg_msg"] = a[3]
    assert ids["tg_msg"] == 14583 and ids["li_org_urn"] == "urn:li:share:555"
    assert ids["ig_feed"] == "IGFEED1" and ids["ig_story"] == "IGSTORY1"
    resumen = q.ediciones[-1][1]
    assert resumen.startswith("📝 Salió: «Hay quienes leen para crecer Walter Peitina»")


def test_si_la_placa_quedo_vieja_se_regenera_antes_de_publicar(main, placas, harness, monkeypatch):
    monkeypatch.setattr(main, "append_social_meta", lambda *a, **k: True)
    fp = frase_pendiente(main)
    fp["texto"] = "Texto corregido por otro camino"           # la placa guardada sigue siendo la del texto de antes
    fp["li_on"] = False
    ctx = Ctx({"frase_pending": fp})
    msg_prev = Msg()
    fp["preview"] = (msg_prev.chat_id, msg_prev.message_id)
    run(main.handle_button(*upd_btn("frase_pub", msg_prev)[:1], ctx))
    assert b"Texto corregido por otro camino" in harness.publicaciones[0]["img"]
    assert b"Peitna" not in harness.publicaciones[0]["img"]


def test_si_no_se_puede_regenerar_la_placa_no_se_publica_nada(main, placas, harness, monkeypatch):
    import frases_gen
    monkeypatch.setattr(frases_gen, "generate_frase_image", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("roto")))
    fp = frase_pendiente(main)
    fp["texto"] = "Otro texto"
    ctx = Ctx({"frase_pending": fp})
    msg_prev = Msg()
    fp["preview"] = (msg_prev.chat_id, msg_prev.message_id)
    upd, q = upd_btn("frase_pub", msg_prev)
    run(main.handle_button(upd, ctx))
    assert harness.publicaciones == [] and harness.ig == []
    assert "No publiqué nada" in q.ediciones[-1][1]


# ══ 2. vistas previas viejas y pedidos colgados ═════════════════════════════

def test_los_botones_de_una_vista_previa_vieja_no_publican(main, placas, harness):
    fp = frase_pendiente(main)
    actual, vieja = Msg(), Msg()
    fp["preview"] = (actual.chat_id, actual.message_id)
    ctx = Ctx({"frase_pending": fp})
    upd, q = upd_btn("frase_pub", vieja)
    run(main.handle_button(upd, ctx))
    assert harness.publicaciones == []
    assert "quedó vieja" in q.ediciones[-1][1]


def test_ediciones_pendientes_no_dejan_publicar_a_ciegas_ni_se_comen_el_proximo_mensaje(main, placas, harness, monkeypatch):
    monkeypatch.setattr(main, "append_social_meta", lambda *a, **k: True)
    fp = frase_pendiente(main)
    fp["li_on"] = False
    prev = Msg()
    fp["preview"] = (prev.chat_id, prev.message_id)
    ctx = Ctx({"frase_pending": fp, "awaiting_frase_header": "texto"})        # tocó ✏️ y no mandó nada
    upd, q = upd_btn("frase_pub", prev)
    run(main.handle_button(upd, ctx))
    assert harness.publicaciones == []                                         # primero: avisa y cancela el pedido
    assert "awaiting_frase_header" not in ctx.user_data
    assert "cancelé" in q.ediciones[-1][1]
    run(main.handle_button(*upd_btn("frase_pub", prev)[:1], ctx))              # segundo toque: ahí sí sale
    assert len(harness.publicaciones) == 1


def test_cancelar_y_una_frase_nueva_limpian_el_pedido_colgado(main, placas):
    fp = frase_pendiente(main)
    prev = Msg()
    fp["preview"] = (prev.chat_id, prev.message_id)
    ctx = Ctx({"frase_pending": fp, "awaiting_frase_header": "texto"})
    run(main.handle_button(*upd_btn("frase_cancel", prev)[:1], ctx))
    assert "awaiting_frase_header" not in ctx.user_data and "frase_pending" not in ctx.user_data


# ══ 3. /publicador → Borrar ═════════════════════════════════════════════════

CONTENIDO_57490 = ("<!-- wp:html -->\n<p>x</p>\n<!-- /wp:html -->\n"
                   "<!-- mebot:tg_msg=14583;li_org_urn=urn:li:share:7528554215590420480;ig_feed=17937103527148059;"
                   "ig_story=18118358789161405 -->")


def teclado_de(q):
    kb = [e for e in q.ediciones if e[0] == "teclado" or "reply_markup" in e[2]][-1]
    rm = kb[2]["reply_markup"]
    return [(b.text, b.callback_data) for fila in rm.inline_keyboard for b in fila]


def montar_publicador(main, monkeypatch, contenido=CONTENIDO_57490, cola=None):
    import borrado_redes as bd
    post = {"id": 57490, "title": "La importancia del conocimiento", "link": "https://mundoempresarial.ar/x/", "content": contenido}
    monkeypatch.setattr(main, "find_post", lambda q: dict(post))
    monkeypatch.setattr(bd, "leer_cola_meta", lambda ruta=None: cola if cola is not None else {"posted": {}})
    return post


def test_borrar_ofrece_todas_las_redes_con_lo_registrado_tildado(main, monkeypatch):
    post = montar_publicador(main, monkeypatch)
    ctx = Ctx({"pubx_post": post, "pubx_del_sel": set()})
    msg = Msg()
    upd, q = upd_btn("pubx_del", msg)
    run(main.handle_pubx_button(upd, ctx))
    botones = teclado_de(q)
    etiquetas = [t for t, _ in botones]
    assert "✅ Telegram (canal)" in etiquetas and "✅ LinkedIn (página)" in etiquetas
    assert "✅ Instagram (feed)" in etiquetas and "✅ Instagram (historia)" in etiquetas and "✅ WordPress" in etiquetas
    assert "☐ X (Twitter) (sin posteo registrado)" in etiquetas and "☐ Facebook (sin posteo registrado)" in etiquetas
    assert {c for _, c in botones} >= {"pubx_dt_telegram", "pubx_dt_twitter", "pubx_dt_linkedin", "pubx_dt_li_pag",
                                       "pubx_dt_facebook", "pubx_dt_ig_feed", "pubx_dt_ig_story", "pubx_dt_wp", "pubx_del_confirm"}
    assert ctx.user_data["pubx_del_sel"] == {"wp", "telegram", "li_pag", "ig_feed", "ig_story"}


def test_un_boton_sin_posteo_avisa_en_vez_de_no_hacer_nada_y_no_cambia_la_seleccion(main, monkeypatch):
    post = montar_publicador(main, monkeypatch)
    ctx = Ctx({"pubx_post": post, "pubx_del_sel": {"wp"}})
    upd, q = upd_btn("pubx_dt_twitter", Msg())
    run(main.handle_pubx_button(upd, ctx))
    assert ctx.user_data["pubx_del_sel"] == {"wp"}
    assert len(q.respuestas) == 1 and q.respuestas[0][1] is True and "no hay posteo registrado" in q.respuestas[0][0]


def test_cada_toque_se_contesta_una_sola_vez(main, monkeypatch):
    """Telegram acepta un solo answerCallbackQuery por toque (el segundo da BadRequest)."""
    post = montar_publicador(main, monkeypatch)
    for data, sel in (("pubx_dt_telegram", {"wp"}), ("pubx_del", set()), ("pubx_main", set()),
                      ("pubx_del_confirm", set()), ("pubx_links_send", set())):
        ctx = Ctx({"pubx_post": post, "pubx_del_sel": sel, "pubx_links_sel": set()})
        upd, q = upd_btn(data, Msg())
        run(main.handle_pubx_button(upd, ctx))
        assert len(q.respuestas) == 1, (data, q.respuestas)


def test_encender_y_apagar_redes_se_refleja_en_lo_que_se_borra(main, monkeypatch):
    post = montar_publicador(main, monkeypatch)
    ctx = Ctx({"pubx_post": post, "pubx_del_sel": set()})
    run(main.handle_pubx_button(upd_btn("pubx_del", Msg())[0], ctx))
    run(main.handle_pubx_button(upd_btn("pubx_dt_ig_story", Msg())[0], ctx))        # apaga la historia
    run(main.handle_pubx_button(upd_btn("pubx_dt_telegram", Msg())[0], ctx))        # apaga Telegram
    run(main.handle_pubx_button(upd_btn("pubx_dt_telegram", Msg())[0], ctx))        # lo vuelve a prender
    assert ctx.user_data["pubx_del_sel"] == {"wp", "telegram", "li_pag", "ig_feed"}


def test_confirmar_borra_lo_elegido_verifica_y_le_informa_red_por_red(main, monkeypatch, caplog):
    import borrado_redes as bd
    post = montar_publicador(main, monkeypatch)
    llamadas = []

    def borrar_red(red, oid):
        llamadas.append((red, oid))
        if red == "ig_feed":
            return bd.Resultado(bd.SIGUE, "Instagram no deja borrar por API: borralo desde la app · https://instagram.com/p/x/")
        return bd.Resultado(bd.BORRADO, "borrado y verificado")
    monkeypatch.setattr(bd, "borrar_red", borrar_red)
    estados = iter(["publish", "trash"])
    monkeypatch.setattr(main, "_wp_estado_post", lambda pid: next(estados))
    papelera = []
    monkeypatch.setattr(main, "trash_post", lambda pid: papelera.append(pid) or True)
    ctx = Ctx({"pubx_post": post, "pubx_del_sel": {"wp", "telegram", "li_pag", "ig_feed", "ig_story"}})
    upd, q = upd_btn("pubx_del_confirm", Msg())

    with caplog.at_level("INFO"):
        run(main.handle_pubx_button(upd, ctx))

    assert llamadas == [("telegram", "14583"), ("li_pag", "urn:li:share:7528554215590420480"),
                        ("ig_feed", "17937103527148059"), ("ig_story", "18118358789161405")]
    assert papelera == [57490]                                               # WordPress, al final
    informe = q.ediciones[-1][1]
    assert "✅ Telegram (canal): borrado" in informe and "✅ LinkedIn (página): borrado" in informe
    assert "❌ Instagram (feed): SIGUE PUBLICADO" in informe and "https://instagram.com/p/x/" in informe
    assert "ℹ️ X (Twitter): no había posteo" in informe and "ℹ️ Facebook: no había posteo" in informe
    assert "✅ WordPress: borrado — a la papelera" in informe
    assert "pubx_post" not in ctx.user_data and len(q.respuestas) == 1
    # y todo queda en el journal (antes el borrado no dejaba ni una línea)
    log = caplog.text
    assert "pubx borrar nota #57490" in log and "terminado" in log and "ig_feed" in log


def test_borrar_una_nota_con_ids_del_harness_incluye_facebook_y_la_historia_de_la_cola_de_meta(main, monkeypatch):
    import borrado_redes as bd
    cola = {"posted": {"900": {"fb": "123_456", "ig": "999111", "ig_story": "555"}}}
    post = montar_publicador(main, monkeypatch, contenido="<p>x</p>\n<!-- mebot:tweet_id=11;tg_msg=22;li_urn=urn:li:share:33 -->",
                             cola=cola)
    post["id"] = 900
    monkeypatch.setattr(main, "find_post", lambda q: dict(post))
    ctx = Ctx({"pubx_post": post, "pubx_del_sel": set()})
    run(main.handle_pubx_button(upd_btn("pubx_del", Msg())[0], ctx))
    assert ctx.user_data["pubx_del_sel"] == {"wp", "telegram", "twitter", "linkedin", "facebook", "ig_feed", "ig_story"}
    assert ctx.user_data["pubx_del_reg"]["facebook"] == "123_456"


def test_append_social_meta_no_borra_ids_que_no_conoce_y_suma_los_nuevos(main, monkeypatch):
    escrito = {}
    monkeypatch.setattr(main, "wp_auth", lambda: {})
    monkeypatch.setattr(main.requests, "get", lambda *a, **k: types.SimpleNamespace(
        status_code=200, json=lambda: {"content": {"raw": "<p>x</p>\n<!-- mebot:tg_msg=5;ig_feed=77;algo_nuevo=abc -->"}}))
    monkeypatch.setattr(main, "update_post", lambda pid, payload: escrito.update(payload) or True)
    assert main.append_social_meta(1, "", "T9", 0, "", li_org_urn="urn:li:share:1", ig_story="S1")
    import borrado_redes as bd
    assert bd.parse_meta(escrito["content"]) == {"tweet_id": "T9", "tg_msg": "5", "ig_feed": "77", "algo_nuevo": "abc",
                                                 "li_org_urn": "urn:li:share:1", "ig_story": "S1"}
    assert escrito["content"].count("mebot:") == 1 and escrito["content"].startswith("<p>x</p>")


def test_append_social_meta_no_escribe_si_el_contenido_esta_vacio(main, monkeypatch):
    monkeypatch.setattr(main, "wp_auth", lambda: {})
    monkeypatch.setattr(main.requests, "get", lambda *a, **k: types.SimpleNamespace(status_code=500, json=lambda: {}))
    monkeypatch.setattr(main, "update_post", lambda *a, **k: (_ for _ in ()).throw(AssertionError("no debía escribir")))
    assert main.append_social_meta(1, "", "", 5) is False


# ══ 4. /borrar y /editar → Borrar usan el mismo borrado que /publicador ═════════

def test_borrar_y_editar_abren_la_misma_pantalla_que_publicador(main, monkeypatch):
    post = montar_publicador(main, monkeypatch)

    async def edit_text(self, texto, **kw):
        self.enviados.append(("edicion", texto, kw))
    monkeypatch.setattr(Msg, "edit_text", edit_text, raising=False)

    ctx = Ctx({"pubx_del_sel": {"wp"}})
    upd, m = upd_msg("/borrar 57490")
    ctx.args = ["57490"]
    run(main.cmd_borrar(upd, ctx))
    ed = [e for e in m.enviados if e[0] == "edicion"][-1]
    etiquetas = [b.text for fila in ed[2]["reply_markup"].inline_keyboard for b in fila]
    assert "✅ Instagram (feed)" in etiquetas and "✅ LinkedIn (página)" in etiquetas   # antes: solo la papelera de WP
    assert ctx.user_data["pubx_post"]["id"] == 57490
    assert ctx.user_data["pubx_del_sel"] == {"wp", "telegram", "li_pag", "ig_feed", "ig_story"}   # selección vieja anulada

    ctx2 = Ctx({"edit_post": dict(post)})
    upd2, q = upd_btn("edit_delete", Msg())
    run(main.handle_edit_button(upd2, ctx2))
    assert "✅ Instagram (historia)" in [t for t, _ in teclado_de(q)]
    assert ctx2.user_data["pubx_post"]["id"] == 57490 and "edit_post" not in ctx2.user_data
