# -*- coding: utf-8 -*-
"""GA4 = personas de Argentina — el lado del bot (pedido de Leo, 22/11/2026).

Leo: «filtra los informes y pone la nota al pie para saber del numero original que dejamos afuera». El informe semanal de GA4
del bot (lunes 08:00, `_ga4_weekly_report` en main.py) mide el objetivo de 1.000 usuarios por día, y ese objetivo es de usuarios
ARGENTINOS: las visitas de otros países y de centros de datos no cuentan para el número principal.

Es la misma lógica que core/ga4_personas.py del harness, en chico y sin depender de /opt/me-harness (las pruebas del bot corren
sin el harness al lado):
  · `pedido(req)`: el filtro country = «Argentina», en AND con el filtro que ya tenga el pedido (también si el país está en
    un AND anidado). Con la bandera apagada, el MISMO objeto (byte a byte lo de antes);
  · `sin_filtrar(req)`: el mismo pedido sin el filtro de país, para la nota al pie (UN pedido extra por informe);
  · `nota(...)`: «ℹ️ Solo Argentina. Sin filtrar: N sesiones; quedaron afuera M (otros países y centros de datos).»
  · `nota_semanal(...)`: UNA nota general al pie del informe: dice que TODO el informe (usuarios, sesiones, horas pico,
    fuentes, top notas y dispositivos) es solo Argentina, con los números sin filtrar de usuarios y sesiones.

La interacción no filtra (bajaría a los argentinos que leen rápido). Bandera GA4_SOLO_AR, la misma del harness y con el mismo
criterio que core/flags.activa: sin variable = PRENDIDA; 0, false, no, off, apagado o n la apagan. Sin claves ni datos
personales: repo público.
"""
import os

BANDERA = "GA4_SOLO_AR"
CAMPO = "country"
PAIS = "Argentina"
AFUERA = "otros países y centros de datos"
PREFIJO = "ℹ️ Solo Argentina."
ALCANCE_SEMANAL = "todo el informe: usuarios, sesiones, horas pico, fuentes, top notas y dispositivos"
_APAGADO = {"0", "false", "no", "off", "apagado", "n"}

ETIQUETAS = {
    "sessions": ("sesión", "sesiones"),
    "totalUsers": ("usuario", "usuarios"),
    "activeUsers": ("usuario", "usuarios"),
    "screenPageViews": ("página vista", "páginas vistas"),
}


def activo() -> bool:
    try:
        v = os.environ.get(BANDERA)
        if v is None or not v.strip():
            return True
        return v.strip().lower() not in _APAGADO
    except Exception:
        return True


def _tipos():
    from google.analytics.data_v1beta import types
    return types


def filtro_pais():
    T = _tipos()
    return T.FilterExpression(filter=T.Filter(field_name=CAMPO, string_filter=T.Filter.StringFilter(value=PAIS)))


def es_filtro_pais(fe) -> bool:
    try:
        if fe is None or "filter" not in fe:
            return False
        return fe.filter.field_name == CAMPO and fe.filter.string_filter.value == PAIS
    except Exception:
        return False


def _vacio(fe) -> bool:
    try:
        return fe is None or not any(k in fe for k in ("filter", "and_group", "or_group", "not_expression"))
    except Exception:
        return fe is None


def tiene_pais(fe) -> bool:
    """El país solo o dentro de un AND (también anidado); dentro de un OR o un NOT no exige nada."""
    if es_filtro_pais(fe):
        return True
    try:
        return "and_group" in fe and any(tiene_pais(x) for x in fe.and_group.expressions)
    except Exception:
        return False


def con_pais(fe=None):
    if _vacio(fe):
        return filtro_pais()
    if tiene_pais(fe):
        return fe
    T = _tipos()
    return T.FilterExpression(and_group=T.FilterExpressionList(expressions=[fe, filtro_pais()]))


def sin_pais(fe):
    if _vacio(fe) or es_filtro_pais(fe):
        return None
    if "and_group" in fe and tiene_pais(fe):
        resto = [y for y in (sin_pais(x) for x in fe.and_group.expressions) if y is not None]
        if not resto:
            return None
        if len(resto) == 1:
            return resto[0]
        T = _tipos()
        return T.FilterExpression(and_group=T.FilterExpressionList(expressions=resto))
    return fe


def _copia(req):
    return type(req).deserialize(type(req).serialize(req))


def pedido(req):
    """El pedido con el filtro de país (copia). Bandera apagada: el MISMO objeto, sin tocar."""
    if not activo():
        return req
    nuevo = _copia(req)
    nuevo.dimension_filter = con_pais(req.dimension_filter if "dimension_filter" in req else None)
    return nuevo


def sin_filtrar(req):
    nuevo = _copia(req)
    if "dimension_filter" in nuevo:
        resto = sin_pais(nuevo.dimension_filter)
        if resto is None:
            del nuevo.dimension_filter
        else:
            nuevo.dimension_filter = resto
    return nuevo


def miles(n) -> str:
    try:
        return format(int(n), ",").replace(",", ".")
    except Exception:
        return str(n)


def _etiqueta(m, n) -> str:
    sing, plur = ETIQUETAS.get(m, (m, m))
    return sing if int(n or 0) == 1 else plur


def nota(partes, periodo="", alcance="") -> str:
    """partes = [(total sin filtrar, afuera, métrica GA4 o etiqueta)]. `alcance` = qué es solo Argentina, entre paréntesis
    después del «Solo Argentina». Texto plano: no lleva _ * ` [ (sirve en Markdown)."""
    try:
        partes = [(int(t or 0), max(0, int(a or 0)), m) for t, a, m in partes]
        if not partes:
            return nota_fallida()
        cab = "Sin filtrar%s:" % ((", " + periodo) if periodo else "")
        sf = " y ".join("%s %s" % (miles(t), _etiqueta(m, t)) for t, _a, m in partes)
        if len(partes) == 1:
            a = partes[0][1]
            fuera = "%s %s" % ("quedó afuera" if a == 1 else "quedaron afuera", miles(a))
        else:
            fuera = "quedaron afuera " + " y ".join("%s %s" % (miles(a), _etiqueta(m, a)) for _t, a, m in partes)
        pre = PREFIJO if not alcance else "%s (%s)." % (PREFIJO[:-1], alcance)
        return "%s %s %s; %s (%s)." % (pre, cab, sf, fuera, AFUERA)
    except Exception:
        return nota_fallida()


def nota_fallida() -> str:
    return "%s No se pudo medir el número sin filtrar." % PREFIJO


def nota_semanal(daily, daily_sf) -> str:
    """UNA nota general al pie del informe semanal: dice que todo el informe es solo Argentina y da el promedio de
    usuarios por día y las sesiones de la semana del mismo pedido sin filtrar (sumados igual que el informe). '' si no hay
    pedido sin filtrar (bandera apagada)."""
    if daily_sf is None:
        return ""
    if isinstance(daily_sf, dict):                 # {"error": ...}
        return "%s (%s). No se pudo medir el número sin filtrar." % (PREFIJO[:-1], ALCANCE_SEMANAL)
    try:
        prom = lambda filas: round(sum(d["users"] for d in filas) / len(filas)) if filas else 0  # noqa: E731
        ses = lambda filas: sum(d["sessions"] for d in filas)                                       # noqa: E731
        u_ar, u_sf = prom(daily), prom(daily_sf)
        s_ar, s_sf = ses(daily), ses(daily_sf)
        return nota([(u_sf, u_sf - u_ar, "usuarios por día en promedio"), (s_sf, s_sf - s_ar, "sessions")],
                    "última semana", ALCANCE_SEMANAL)
    except Exception:
        return nota_fallida()
