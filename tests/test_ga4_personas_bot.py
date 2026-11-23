# -*- coding: utf-8 -*-
"""GA4 = personas de Argentina, lado del bot (ga4_personas_bot.py + el informe semanal de main.py). Pedido de Leo, 22/11/2026.

Sin red: el cliente de GA4 es de mentira (anota cada pedido y contesta con respuestas armadas con los tipos REALES de la
librería) y Telegram/OpenAI también. Se prueba que los cinco pedidos del informe semanal lleven country = Argentina, que la
nota al pie tenga el número sin filtrar y lo que quedó afuera, y que con GA4_SOLO_AR apagada los pedidos sean byte a byte
los de hoy (copias literales de los de 6fa0253)."""
import asyncio
import os
import sys
import types

import pytest

RAIZ_BOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, RAIZ_BOT)

T = pytest.importorskip("google.analytics.data_v1beta.types")
import google.analytics.data_v1beta as ga4  # noqa: E402

import ga4_personas_bot as G  # noqa: E402

PAIS_FE = T.FilterExpression(filter=T.Filter(field_name="country", string_filter=T.Filter.StringFilter(value="Argentina")))


def _b(req):
    return T.RunReportRequest.serialize(req)


def _resp(rows, metricas):
    return T.RunReportResponse(
        metric_headers=[T.MetricHeader(name=m) for m in metricas],
        rows=[T.Row(dimension_values=[T.DimensionValue(value=str(d)) for d in dims],
                    metric_values=[T.MetricValue(value=str(v)) for v in mets]) for dims, mets in rows])


class Cliente:
    """BetaAnalyticsDataClient de mentira: con país, 60 % de lo que da sin país."""
    instancias = []

    def __init__(self, *a, **k):
        self.pedidos = []
        Cliente.instancias.append(self)

    def run_report(self, req):
        self.pedidos.append(T.RunReportRequest.deserialize(T.RunReportRequest.serialize(req)))
        ar = "dimension_filter" in req and G.es_filtro_pais(req.dimension_filter)
        k = 6 if ar else 10
        dims = [d.name for d in req.dimensions]
        mets = [m.name for m in req.metrics]
        if dims == ["date"]:
            return _resp([(["20261116"], [k * 100] * len(mets)), (["20261117"], [k * 50] * len(mets))], mets)
        if dims == ["hour"]:
            return _resp([([h], [k * (h + 1)] * len(mets)) for h in (9, 13, 20)], mets)
        return _resp([(["x"] * len(dims), [k] * len(mets))], mets)


@pytest.fixture
def cliente(monkeypatch):
    Cliente.instancias = []
    monkeypatch.setattr(ga4, "BetaAnalyticsDataClient", Cliente)
    monkeypatch.setenv("GOOGLE_APPLICATION_CREDENTIALS", "")       # _ga4_fetch_data la pisa: así queda como estaba
    return Cliente


@pytest.fixture(autouse=True)
def _sin_red(monkeypatch):
    import google.analytics.data_v1beta.services.beta_analytics_data.client as real

    def _no(*a, **k):
        raise AssertionError("una prueba intentó crear el cliente REAL de GA4")
    monkeypatch.setattr(real.BetaAnalyticsDataClient, "__init__", _no)


@pytest.fixture
def main(monkeypatch):
    for k in ("TELEGRAM_TOKEN", "WP_USER", "WP_PASS"):
        monkeypatch.setenv(k, "falso")
    import main as m
    monkeypatch.setattr(m, "GA4_PROPERTY_ID", "123456789")
    monkeypatch.setattr(m, "GA4_SA_PATH", "/no/existe.json")
    monkeypatch.setattr(m, "ADMIN_CHAT_ID", "77")
    monkeypatch.setattr(m, "OPENAI_API_KEY", "")
    return m


def _viejos(prop):                 # 6fa0253: main._ga4_fetch_data, los cinco pedidos tal cual
    R, D, M, DR, O = T.RunReportRequest, T.Dimension, T.Metric, T.DateRange, T.OrderBy
    sem = [DR(start_date="7daysAgo", end_date="yesterday")]
    return [
        R(property=prop, dimensions=[D(name="date")], metrics=[M(name="totalUsers"), M(name="sessions")], date_ranges=sem),
        R(property=prop, dimensions=[D(name="hour")], metrics=[M(name="sessions")], date_ranges=sem,
          order_bys=[O(metric=O.MetricOrderBy(metric_name="sessions"), desc=True)]),
        R(property=prop, dimensions=[D(name="sessionSource"), D(name="sessionMedium")], metrics=[M(name="sessions")],
          date_ranges=sem, order_bys=[O(metric=O.MetricOrderBy(metric_name="sessions"), desc=True)], limit=8),
        R(property=prop, dimensions=[D(name="pageTitle")], metrics=[M(name="screenPageViews")], date_ranges=sem,
          order_bys=[O(metric=O.MetricOrderBy(metric_name="screenPageViews"), desc=True)], limit=5),
        R(property=prop, dimensions=[D(name="deviceCategory")], metrics=[M(name="sessions")], date_ranges=sem),
    ]


# ── la pieza ──────────────────────────────────────────────────────────────────────────────────────────────────────────

def test_bandera_como_core_flags(monkeypatch):
    monkeypatch.delenv("GA4_SOLO_AR", raising=False)
    assert G.activo() is True                                          # sin variable = prendida (Leo ya lo pidió)
    for v in ("0", "false", "no", "off", "apagado", "n", " OFF "):
        monkeypatch.setenv("GA4_SOLO_AR", v)
        assert G.activo() is False, v
    for v in ("1", "si", "true", ""):
        monkeypatch.setenv("GA4_SOLO_AR", v)
        assert G.activo() is True, v


def test_pedido_and_y_sin_filtrar(monkeypatch):
    monkeypatch.delenv("GA4_SOLO_AR", raising=False)
    camp = T.FilterExpression(filter=T.Filter(field_name="pagePath", string_filter=T.Filter.StringFilter(value="/a/")))
    r = T.RunReportRequest(property="p", dimension_filter=camp)
    n = G.pedido(r)
    assert list(n.dimension_filter.and_group.expressions) == [camp, PAIS_FE] and r.dimension_filter == camp
    assert G.pedido(n).dimension_filter == n.dimension_filter
    assert _b(G.sin_filtrar(n)) == _b(r)
    assert G.pedido(T.RunReportRequest(property="p")).dimension_filter == PAIS_FE
    monkeypatch.setenv("GA4_SOLO_AR", "0")
    assert G.pedido(r) is r


def test_pais_en_and_anidado(monkeypatch):
    monkeypatch.delenv("GA4_SOLO_AR", raising=False)
    a = T.FilterExpression(filter=T.Filter(field_name="pagePath", string_filter=T.Filter.StringFilter(value="/a/")))
    b = T.FilterExpression(filter=T.Filter(field_name="sessionSource", string_filter=T.Filter.StringFilter(value="x")))
    dentro = T.FilterExpression(and_group=T.FilterExpressionList(expressions=[a, PAIS_FE]))
    fe = T.FilterExpression(and_group=T.FilterExpressionList(expressions=[dentro, b]))
    assert "country" not in str(G.sin_pais(fe)) and list(G.sin_pais(fe).and_group.expressions) == [a, b]
    assert G.con_pais(fe) == fe                                        # ya exige Argentina: no lo suma otra vez
    o = T.FilterExpression(or_group=T.FilterExpressionList(expressions=[PAIS_FE, a]))
    assert not G.tiene_pais(o) and G.sin_pais(o) == o                  # dentro de un OR no es nuestro filtro


def test_nota_texto():
    assert G.nota([(1234, 567, "sessions")]) == (
        "ℹ️ Solo Argentina. Sin filtrar: 1.234 sesiones; quedaron afuera 567 (otros países y centros de datos).")
    t = G.nota_semanal([{"users": 600, "sessions": 600}, {"users": 300, "sessions": 300}],
                       [{"users": 1000, "sessions": 1000}, {"users": 500, "sessions": 500}])
    assert t == ("ℹ️ Solo Argentina (todo el informe: usuarios, sesiones, horas pico, fuentes, top notas y dispositivos). "
                 "Sin filtrar, última semana: 750 usuarios por día en promedio y 1.500 sesiones; quedaron afuera 300 "
                 "usuarios por día en promedio y 600 sesiones (otros países y centros de datos).")
    assert G.nota_semanal([], None) == ""
    assert G.nota_semanal([], {"error": "x"}) == ("ℹ️ Solo Argentina (todo el informe: usuarios, sesiones, horas pico, "
                                                   "fuentes, top notas y dispositivos). No se pudo medir el número sin filtrar.")
    assert not any(ch in t for ch in "_*`[")                          # no rompe el Markdown del informe


def test_repo_publico_sin_cifras_de_trafico():
    """El repo del bot es público: ga4_personas_bot.py explica el criterio, sin números de tráfico de ME (el único número
    con punto de miles que puede aparecer es el objetivo, 1.000) ni proporciones del tráfico real."""
    import re
    src = open(os.path.join(RAIZ_BOT, "ga4_personas_bot.py"), encoding="utf-8").read()
    assert set(re.findall(r"\b\d{1,3}(?:\.\d{3})+\b", src)) <= {"1.000"}
    assert not re.search(r"\d\s?%", src) and "mitad" not in src                  # ni porcentajes del tráfico


def test_todo_pedido_del_bot_pasa_por_la_pieza():
    """Control estático de main.py: cada pedido a GA4 (run_report o los otros métodos de la Data API) pasa por
    pedido(); sin_filtrar() solo en el pedido de la nota al pie de _ga4_fetch_data."""
    import ast
    metodos = {"run_report", "batch_run_reports", "run_realtime_report", "run_pivot_report", "batch_run_pivot_reports"}
    arbol = ast.parse(open(os.path.join(RAIZ_BOT, "main.py"), encoding="utf-8").read())
    vistos, malos = [], []
    for fn in ast.walk(arbol):
        if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for n in ast.walk(fn):
            if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute) and n.func.attr in metodos:
                a = n.args[0] if n.args else None
                pieza = a.func.attr if isinstance(a, ast.Call) and isinstance(a.func, ast.Attribute) else None
                vistos.append((fn.name, n.func.attr, pieza))
                mal = n.func.attr != "run_report" or pieza not in ("pedido", "sin_filtrar")
                mal = mal or (pieza == "sin_filtrar" and fn.name != "_ga4_fetch_data")
                if mal:
                    malos.append((fn.name, n.lineno))
    assert not malos, malos
    assert sorted(p for _f, _m, p in vistos) == ["pedido"] * 5 + ["sin_filtrar"]


# ── el informe semanal de main.py ─────────────────────────────────────────────────────────────────────────────────────

def test_fetch_apagada_igual_a_hoy(main, cliente, monkeypatch):
    monkeypatch.setenv("GA4_SOLO_AR", "0")
    data = main._ga4_fetch_data()
    (c,) = Cliente.instancias
    assert [_b(p) for p in c.pedidos] == [_b(r) for r in _viejos("properties/123456789")]
    assert set(data) == {"daily", "hourly", "sources", "pages", "devices"}
    assert [d["users"] for d in data["daily"]] == [1000, 500]


def test_fetch_prendida_cinco_filtrados_y_uno_para_la_nota(main, cliente, monkeypatch):
    monkeypatch.delenv("GA4_SOLO_AR", raising=False)
    data = main._ga4_fetch_data()
    (c,) = Cliente.instancias
    viejos = _viejos("properties/123456789")
    filtrados = [p for p in c.pedidos if "dimension_filter" in p]
    assert len(c.pedidos) == 6 and len(filtrados) == 5
    assert all(p.dimension_filter == PAIS_FE for p in filtrados)
    assert [_b(G.sin_filtrar(p)) for p in filtrados] == [_b(r) for r in viejos]
    sin = [p for p in c.pedidos if "dimension_filter" not in p]
    assert [_b(p) for p in sin] == [_b(viejos[0])]                   # el de usuarios por día, sin el país
    assert [d["users"] for d in data["daily"]] == [600, 300]
    assert [d["users"] for d in data["daily_sin_filtrar"]] == [1000, 500]


class _Bot:
    def __init__(self):
        self.enviados = []

    async def send_message(self, **k):
        self.enviados.append(k)


def _informe(m, data):
    bot = _Bot()
    m_fetch = m._ga4_fetch_data
    try:
        m._ga4_fetch_data = lambda: data
        asyncio.run(m._ga4_weekly_report(types.SimpleNamespace(bot=bot)))
    finally:
        m._ga4_fetch_data = m_fetch
    return bot.enviados


def _datos(con_sf):
    d = {"daily": [{"date": "20261116", "users": 600, "sessions": 600}, {"date": "20261117", "users": 300, "sessions": 300}],
         "hourly": [{"hour": 9, "sessions": 60}], "sources": [{"source": "google", "medium": "organic", "sessions": 6}],
         "pages": [{"title": "Una nota", "views": 6}], "devices": {"mobile": 6}}
    if con_sf:
        d["daily_sin_filtrar"] = [{"date": "20261116", "users": 1000, "sessions": 1000},
                                  {"date": "20261117", "users": 500, "sessions": 500}]
    return d


def test_informe_lleva_la_nota_al_pie(main, monkeypatch):
    (env,) = _informe(main, _datos(True))
    assert env["parse_mode"] == "Markdown" and "*450 usuarios/día*" in env["text"]
    assert env["text"].endswith("_ℹ️ Solo Argentina (todo el informe: usuarios, sesiones, horas pico, fuentes, top notas y "
                                "dispositivos). Sin filtrar, última semana: 750 usuarios por día en promedio y 1.500 "
                                "sesiones; quedaron afuera 300 usuarios por día en promedio y 600 sesiones (otros países y "
                                "centros de datos)._")
    assert env["text"].count("Solo Argentina") == 1                    # UNA nota general, no una por bloque


def test_informe_apagado_como_hoy(main):
    (env,) = _informe(main, _datos(False))
    assert "Solo Argentina" not in env["text"] and env["text"].endswith("📄 *Top notas:*\n  • Una nota: 6 vistas\n\n")


def test_el_modelo_sabe_que_es_argentina(main, monkeypatch):
    vistos = []

    class _R:
        status_code = 500
    monkeypatch.setattr(main, "OPENAI_API_KEY", "x" * 3)
    monkeypatch.setattr(main, "openai_post", lambda url, **k: vistos.append(k["json"]["messages"][0]["content"]) or _R())
    _informe(main, _datos(True))
    _informe(main, _datos(False))
    assert "Datos de la última semana (solo Argentina" in vistos[0]
    assert "Datos de la última semana:\n" in vistos[1]
