# -*- coding: utf-8 -*-
"""botones_harness_bot (20/11/2026): fpd_, pfc_ y (21/11) h_coti_salto: llegan al harness y la respuesta se suma al mensaje.
Sin red ni Telegram; la integración usa la copia local del harness con una base temporal (si está)."""
import asyncio
import json
import logging
import os
import subprocess
import sys
import textwrap
import types

import pytest

import botones_harness_bot as B
import borrado_redes as R

RAIZ = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


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


def test_registrar_pone_los_tres_patrones_en_el_grupo_normal():
    agregados = []
    app = types.SimpleNamespace(add_handler=lambda h, *a, **k: agregados.append((h.pattern.pattern, a, k)))
    B.registrar(app)
    assert [p for p, _, _ in agregados] == [r"^fpd_(si|no):\d+$", r"^pfc_(si|no):\d+$", r"^h_coti_salto:[a-z0-9_]{1,20}:\d{8}$"]
    # grupo 0 (sin `group`): el filtro de admin del grupo -1 corre antes y frena a cualquier otro
    assert all(a == () and k == {} for _, a, k in agregados)


# ── h_coti_salto: «Aceptar como movimiento real» de cotizaciones (21/11/2026) ──

COTI_OK = "h_coti_salto:blue:20261119"
AVISO = "⚠️ <b>Cotizaciones</b>: el <b>Dólar blue</b> llegó dos días con el mismo salto. <b>NO se publica</b>."


def _patron(prefijo):
    agregados = []
    B.registrar(types.SimpleNamespace(add_handler=lambda h, *a, **k: agregados.append(h)))
    return next(h.pattern for h in agregados if h.pattern.pattern == B.PATRONES[prefijo])


@pytest.mark.parametrize("data", [COTI_OK, "h_coti_salto:riesgo_pais:20261231", "h_coti_salto:a3500:20260102",
                                  "h_coti_salto:tasa_30d:20261119", "h_coti_salto:pf_promedio:20261119"])
def test_el_patron_de_coti_toma_el_boton_del_harness(data):
    assert _patron(B.COTI).match(data) and len(data.encode()) <= 64


@pytest.mark.parametrize("data", ["h_coti_salto:blue:2026-11-19", "h_coti_salto:blue", "h_coti_salto::20261119",
                                  "h_coti_salto:BLUE:20261119", "h_coti_salto:blue:20261119:9", "h_coti_salto:blue:2026111",
                                  "h_coti_saltos:blue:20261119", "xh_coti_salto:blue:20261119", "h_coti_salto:dólar:20261119",
                                  "h_bv2_frenar:7", "h_cola_main_panel", "h_cur_kw:3", "h_c1_undo:5", "fpd_si:1", "pfc_no:2"])
def test_el_patron_de_coti_no_toma_otros_botones(data):
    assert not _patron(B.COTI).match(data)


def test_ningun_handler_anterior_se_queda_con_el_boton_de_coti():
    """Lo que se registra antes con pattern= (main.py y los módulos del bot) no puede tomar h_coti_salto: ni h_bv2_ (este sigue
    yendo al handle_button → panel_bot.boton_harness); y fpd_/pfc_ no tocan h_coti_salto:."""
    import re
    patrones = []
    for f in os.listdir(RAIZ):
        if f.endswith(".py") and f != "botones_harness_bot.py":
            patrones += re.findall(r'pattern\s*=\s*r?"([^"]+)"', open(os.path.join(RAIZ, f), encoding="utf-8").read())
    patrones += [B.PATRONES["fpd_"], B.PATRONES["pfc_"]]
    assert {"^jfd_", "^pz_", "^h_pesos", "^(agd_|agf_)", r"^ortit:\d+$"} <= set(patrones)     # el barrido los encuentra
    for p in patrones:
        assert not re.match(p, COTI_OK) and not re.match(p, "h_bv2_frenar:7"), p


class Coti:
    """Doble de agents/cotizaciones: lo que usa el bot (aceptar_salto, ACTIVOS, fmt_valor)."""
    ACTIVOS = {"blue": {"nombre": "Dólar blue"}, "riesgo_pais": {"nombre": "Riesgo país"}}

    def __init__(self, r=None, falla=None):
        self.r, self.falla, self.llamadas = r, falla, []

    def aceptar_salto(self, activo, dia, **k):
        self.llamadas.append((activo, dia, k))
        if self.falla:
            raise self.falla
        return self.r

    @staticmethod
    def fmt_valor(item):
        return "$" + format(int(item["valor"]), ",").replace(",", ".")


def test_coti_acepta_el_salto_y_lo_suma_al_aviso():
    mod = Coti({"ok": True, "aceptadas": 2, "valor": 1975.0})
    q = Q(COTI_OK, html=AVISO)
    _correr(B.COTI, q, mod)
    assert mod.llamadas == [("blue", "20261119", {})] and q.contestado
    assert q.editado.startswith(AVISO + "\n\n✅ <b>Aceptado como movimiento real</b>: Dólar blue $1.975 del 19/11 (2 lecturas).")
    assert q.message.respuestas == []                               # editado en el lugar y sin teclado: un solo uso


def test_coti_si_el_harness_no_lo_acepta_lo_dice_y_no_promete_nada():
    mod = Coti({"ok": False, "motivo": "no hay un salto rechazado de riesgo_pais el 2026-11-19"})
    q = Q("h_coti_salto:riesgo_pais:20261119", html=AVISO)
    _correr(B.COTI, q, mod)
    assert mod.llamadas == [("riesgo_pais", "20261119", {})]
    assert "⚠️ No acepté el salto del Riesgo país (19/11): no hay un salto rechazado" in q.editado
    assert "Si ya lo habías aceptado" in q.editado and "Aceptado" not in q.editado


def test_coti_escapa_lo_que_devuelve_el_harness():
    q = Q(COTI_OK, html=AVISO)
    _correr(B.COTI, q, Coti({"ok": False, "motivo": "database <locked> & co"}))
    assert "database &lt;locked&gt; &amp; co" in q.editado


def test_coti_si_el_harness_falla_lo_dice_y_no_toca_el_aviso():
    q = Q(COTI_OK, html=AVISO)
    _correr(B.COTI, q, Coti(falla=RuntimeError("base bloqueada")))
    assert q.editado is None and q.message.respuestas == ["⚠️ No pude resolver el botón (base bloqueada). No toqué nada."]


def test_coti_si_tarda_avisa_sin_colgar(monkeypatch):
    import time
    monkeypatch.setitem(B.PLAZO_S, B.COTI, 0.05)
    mod = Coti({"ok": True, "aceptadas": 1, "valor": 1975.0})
    mod.aceptar_salto = lambda activo, dia: time.sleep(0.3) or mod.r
    q = Q(COTI_OK, html=AVISO)
    _correr(B.COTI, q, mod)
    assert q.editado is None and q.message.respuestas[0].startswith("⏱ Está tardando más de")


def _guardia():
    """_admin_ids y _solo_admin de main.py, sin importar las ~18 mil líneas ni telegram."""
    import ast
    src = open(os.path.join(RAIZ, "main.py"), encoding="utf-8").read()
    nodos = [n for n in ast.parse(src).body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
             and n.name in ("_admin_ids", "_solo_admin")]
    assert len(nodos) == 2

    class Stop(Exception):
        pass

    anotados = []
    ns = {"ADMIN_CHAT_ID": "1001", "ApplicationHandlerStop": Stop, "asyncio": asyncio, "logger": logging.getLogger("prueba"),
          "_uso_clave": lambda u: ("boton", "h_coti_salto"), "_uso_anotar": lambda *k: anotados.append(k)}
    exec(compile(ast.Module(body=nodos, type_ignores=[]), "main_extracto", "exec"), ns)
    return ns["_solo_admin"], Stop, src


def test_un_no_admin_no_llega_al_boton_de_coti():
    solo_admin, Stop, src = _guardia()

    def upd(uid):
        return types.SimpleNamespace(effective_user=types.SimpleNamespace(id=uid, username="x"),
                                     callback_query=Q(COTI_OK), message=None)
    with pytest.raises(Stop):                                       # un extraño: se frena TODA la cadena de handlers
        asyncio.run(solo_admin(upd(2002), None))
    assert asyncio.run(solo_admin(upd(1001), None)) is None         # el admin sigue
    # y el filtro corre antes: grupo -1, registrado antes que estos botones y que el handle_button general
    i_guardia = src.index("app.add_handler(TypeHandler(Update, _solo_admin), group=-1)")
    i_botones = src.index("_bhb.registrar(app)")
    assert i_guardia < i_botones < src.index("app.add_handler(CallbackQueryHandler(handle_button))")


# ── integración con la copia del harness al lado (si está): agents/cotizaciones.aceptar_salto de verdad, base temporal ──

def _harness_local():
    for d in (os.environ.get("HARNESS_DIR_PRUEBA"), os.path.join(os.path.dirname(RAIZ), "me-harness"),
              os.path.join(os.path.dirname(os.path.dirname(RAIZ)), "me-harness")):
        if d and os.path.exists(os.path.join(d, "agents", "cotizaciones.py")):
            return d
    return None


@pytest.mark.skipif(not _harness_local(), reason="no está la copia del harness con agents/cotizaciones.py")
def test_integracion_coti_con_el_harness_real(tmp_path):
    harness = _harness_local()
    script = textwrap.dedent("""
        import asyncio, json, re, sqlite3, sys, types
        sys.path.insert(0, %(bot)r)
        import botones_harness_bot as B
        B.HARNESS_DIR = %(harness)r
        co = B.cargar("cotizaciones")
        if not hasattr(co, "aceptar_salto"):
            print(json.dumps({"sin_aceptar": True})); sys.exit(0)
        from core import cotizaciones_hist as hist
        hist.registrar("blue", 1630.0, "dolarapi", "2026-11-17 16:00", verificacion="una_fuente")
        hist.registrar("blue", 1960.0, "dolarapi", "2026-11-18 16:00", ok=False, verificacion="rechazado_salto")
        hist.registrar("blue", 1975.0, "dolarapi", "2026-11-19 13:00", ok=False, verificacion="rechazado_salto")
        class M:
            text_html = "Aviso"
            def __init__(s): s.r = []
            async def reply_text(s, t, **k): s.r.append(t)
        class Q:
            def __init__(s, d): s.data, s.message, s.editado = d, M(), None
            async def answer(s, *a, **k): pass
            async def edit_message_text(s, t, **k): s.editado = t
        def tocar(d):
            q = Q(d)
            asyncio.run(B.hacer_handler(B.COTI)(types.SimpleNamespace(callback_query=q), None))
            return q.editado or (q.message.r or [""])[0]
        ok = tocar("h_coti_salto:blue:20261119")
        nada = tocar("h_coti_salto:mep:20261119")
        c = sqlite3.connect(%(db)r)
        filas = c.execute("SELECT dato_dia FROM cotizaciones_hist WHERE activo='blue' AND ok=1 "
                          "AND verificacion='confirmado:leo' ORDER BY dato_dia").fetchall()
        claves = [a for a in co.ACTIVOS if not re.match(B.PATRONES[B.COTI], "h_coti_salto:%%s:20261119" %% a)]
        print(json.dumps({"ok": ok, "nada": nada, "filas": [f[0] for f in filas], "claves_fuera": claves}))
    """) % {"bot": RAIZ, "harness": harness, "db": str(tmp_path / "h.db")}
    env = dict(os.environ, HARNESS_DB=str(tmp_path / "h.db"), HARNESS_DIR=harness, PYTHONIOENCODING="utf-8")
    p = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True, env=env, cwd=str(tmp_path),
                       timeout=120, encoding="utf-8")
    assert p.returncode == 0, p.stderr[-2000:]
    out = json.loads(p.stdout.strip().splitlines()[-1])
    if out.get("sin_aceptar"):
        pytest.skip("la copia del harness es anterior a cotizaciones.aceptar_salto")
    assert "✅ <b>Aceptado como movimiento real</b>: Dólar blue $1.975 del 19/11 (2 lecturas)" in out["ok"]
    assert out["filas"] == ["2026-11-18", "2026-11-19"]            # el de hoy y el del día anterior a ±2 %
    assert "No acepté el salto del Dólar MEP (19/11): no hay un salto rechazado" in out["nada"]
    assert out["claves_fuera"] == []                               # todo activo del harness entra en el patrón del bot


# ── posteos reemplazados al re-publicar (20/11/2026) ──────────────────────────

def test_anteriores_de_nota_lee_las_claves_ant():
    c = "<p>x</p><!-- mebot:tweet_id=222;tg_msg=88;tweet_id_ant=111,100;li_org_urn_ant=urn:li:share:9 -->"
    assert R.anteriores_de_nota(c) == {"twitter": ["111", "100"], "li_pag": ["urn:li:share:9"]}


def test_borrar_anteriores_solo_las_redes_elegidas(monkeypatch):
    vistos = []
    monkeypatch.setattr(R, "borrar_red", lambda red, oid: vistos.append((red, oid)) or R.Resultado(R.BORRADO, "verificado"))
    out = R.borrar_anteriores({"twitter"}, {"twitter": ["111"], "li_pag": ["urn:li:share:9"]})
    assert vistos == [("twitter", "111")] and out[0][1].detalle == "posteo anterior · verificado"


# ── Revisión adversarial del 21/11 ─────────────────────────────────────────────────────────────────────────────────────
class QConTeclado(Q):
    def __init__(self, data, answer_falla=False):
        super().__init__(data, html="⚠️ Cotizaciones: el MEP llegó dos días con el mismo salto")
        self.message.reply_markup = {"inline_keyboard": [[{"text": "✅ Aceptar", "callback_data": data}]]}
        self.kw, self._answer_falla = None, answer_falla

    async def answer(self, *a, **k):
        if self._answer_falla:
            raise RuntimeError("Query is too old and response timeout expired")
        self.contestado = True

    async def edit_message_text(self, t, **kw):
        self.editado, self.kw = t, kw


def test_si_el_harness_no_lo_hizo_el_boton_queda_para_reintentar():
    mod = types.SimpleNamespace(aceptar_salto=lambda a, d: {"ok": False, "motivo": "la base no tomó la aceptación"},
                                ACTIVOS={"mep": {"nombre": "dólar MEP"}})
    q = QConTeclado("h_coti_salto:mep:20261120")
    _correr("h_coti_salto:", q, mod)
    assert "No acepté el salto" in q.editado and q.kw["reply_markup"] == q.message.reply_markup


def test_si_el_harness_lo_hizo_el_boton_se_saca():
    mod = types.SimpleNamespace(aceptar_salto=lambda a, d: {"ok": True, "aceptadas": 2, "valor": 1500.0},
                                ACTIVOS={"mep": {"nombre": "dólar MEP"}}, fmt_valor=lambda x: "$1.500")
    q = QConTeclado("h_coti_salto:mep:20261120")
    _correr("h_coti_salto:", q, mod)
    assert "Aceptado como movimiento real" in q.editado and q.kw["reply_markup"] is None


def test_un_clic_tardio_igual_se_atiende():
    vistos = []
    mod = types.SimpleNamespace(boton=lambda d: vistos.append(d) or {"ok": True, "texto": "✅ Listo."})
    q = QConTeclado("fpd_si:12", answer_falla=True)
    _correr("fpd_", q, mod)
    assert vistos == ["fpd_si:12"] and "✅ Listo." in q.editado
