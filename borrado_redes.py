# -*- coding: utf-8 -*-
"""Borrar una nota o una frase de TODAS las redes donde salió (/publicador → Borrar).

Por qué existe (17/11/2026): Leo publicó una frase, quiso bajarla y el publicador solo ofrecía WordPress, Telegram, X y
LinkedIn, y solo con lo que estuviera anotado en el comentario <!-- mebot:... --> de la nota. La frase había salido
también en la página de LinkedIn y en Instagram (feed e historia) y ninguno de esos ids se guardaba en ningún lado:
el bot borró WP y Telegram y dio por terminado el trabajo.

De dónde salen los ids (todos los que existan):
  · comentario <!-- mebot:tweet_id=…;tg_msg=…;li_urn=…;li_org_urn=…;ig_feed=…;ig_story=…;fb_post=… -->
      tweet_id / tg_msg / li_urn los escribe el bot desde siempre (li_urn: lo que se publicó con el token personal, o con el
      de la página si lo puso el harness). li_org_urn, ig_feed, ig_story y fb_post los escribe el bot desde el 17/11/2026
      cuando publica una frase (antes no se guardaban).
  · /opt/me-harness/meta_post_queue.json → ["posted"][wp_post_id] = {"fb": …, "ig": …, "ig_story": …}: lo que la cola de
      Meta del harness publicó de una nota (misma fuente que agents/lector._delete_meta_posts desde el 16/11).

Cada borrado se VERIFICA mirando el objeto después, nunca por el código del DELETE (el DELETE de Facebook devuelve 500
aunque borre bien). Resultado por red: borrado / sigue publicado / no había posteo / no pude confirmar.

Sin claves en el código: todo sale del entorno en el momento de usarlo. Ningún mensaje de error incluye el texto de una
excepción de `requests` (esa lleva la URL entera, y la de Telegram lleva el token del bot).
"""
import json
import logging
import os
import re
import sys
import urllib.parse
from collections import namedtuple

import requests

log = logging.getLogger("borrado_redes")

Resultado = namedtuple("Resultado", "estado detalle")
BORRADO, SIGUE, NO_HABIA, DUDOSO = "borrado", "sigue", "no_habia", "dudoso"

# Orden en que se borra. WordPress al final: invalida la URL pública y es lo único que se puede restaurar a mano.
ORDEN = ("telegram", "twitter", "linkedin", "li_pag", "facebook", "ig_feed", "ig_story", "wp")
ETIQUETA = {
    "telegram": "Telegram (canal)", "twitter": "X (Twitter)", "linkedin": "LinkedIn",
    "li_pag": "LinkedIn (página)", "facebook": "Facebook", "ig_feed": "Instagram (feed)",
    "ig_story": "Instagram (historia)", "wp": "WordPress",
}
# red → clave dentro del comentario mebot
CLAVE_META = {
    "telegram": "tg_msg", "twitter": "tweet_id", "linkedin": "li_urn", "li_pag": "li_org_urn",
    "facebook": "fb_post", "ig_feed": "ig_feed", "ig_story": "ig_story",
}
# red → clave dentro de meta_post_queue.json["posted"][wp_post_id]
CLAVE_COLA = {"facebook": "fb", "ig_feed": "ig", "ig_story": "ig_story"}

HARNESS_DIR = os.environ.get("HARNESS_DIR", "/opt/me-harness")
RUTA_COLA_META = os.path.join(HARNESS_DIR, "meta_post_queue.json")
GRAPH = "https://graph.facebook.com/v21.0"
WARP = {"https": "socks5h://127.0.0.1:40000", "http": "socks5h://127.0.0.1:40000"}
LI_VERSIONES = ("202609", "202606", "202603")

_RE_ID_GRAPH = re.compile(r"^[0-9A-Za-z_.\-]{3,80}$")
_RE_ID_NUM = re.compile(r"^\d{1,25}$")
_RE_URN = re.compile(r"^urn:li:[A-Za-z]{3,20}:[0-9A-Za-z_\-]{3,60}$")


# ── lo que el bot anota en la nota ────────────────────────────────────────────

def parse_meta(contenido: str) -> dict:
    """Pares clave=valor del comentario <!-- mebot:... --> (vacío si no hay)."""
    m = re.search(r"<!--\s*mebot:([^>]+)-->", contenido or "")
    if not m:
        return {}
    out = {}
    for parte in m.group(1).split(";"):
        if "=" in parte:
            k, v = parte.split("=", 1)
            out[k.strip()] = v.strip()
    return out


def construir_meta(d: dict) -> str:
    """'<!-- mebot:k=v;… -->' con los pares no vacíos (los tres de siempre primero, para no cambiar lo que ya existe)."""
    claves = [k for k in ("tweet_id", "tg_msg", "li_urn") if d.get(k)]
    claves += sorted(k for k in d if k not in ("tweet_id", "tg_msg", "li_urn") and d.get(k))
    if not claves:
        return ""
    return "<!-- mebot:" + ";".join("%s=%s" % (k, str(d[k]).replace(";", "").replace(">", "")) for k in claves) + " -->"


def leer_cola_meta(ruta: str = None):
    try:
        with open(ruta or RUTA_COLA_META, encoding="utf-8") as f:
            d = json.load(f)
        return d if isinstance(d, dict) else None
    except Exception as e:
        log.warning("no pude leer la cola de Meta (%s): %s", ruta or RUTA_COLA_META, type(e).__name__)
        return None


def registro_de_nota(contenido_wp: str, wp_id, cola: dict = None) -> dict:
    """{red: id} de todo lo que está anotado para esa nota. `cola` se lee de disco si no se pasa."""
    meta = parse_meta(contenido_wp)
    if cola is None:
        cola = leer_cola_meta()
    posted = (cola or {}).get("posted") or {}
    ent = posted.get(str(wp_id))
    if not isinstance(ent, dict):
        ent = (cola or {}).get(str(wp_id))          # forma vieja (raíz de la cola), por si acaso
    if not isinstance(ent, dict):
        ent = {}
    reg = {}
    for red, k in CLAVE_META.items():
        if meta.get(k):
            reg[red] = str(meta[k])
    for red, k in CLAVE_COLA.items():
        v = ent.get(k)
        if v:
            reg[red] = str(v)        # lo que anotó la cola de Meta manda sobre el comentario
    return reg


def dudosos_de_cola(wp_id, cola: dict = None) -> list:
    """Canales de Meta que la cola dejó como DUDOSOS para esa nota: fallaron al publicar y pudieron salir igual, sin id."""
    if cola is None:
        cola = leer_cola_meta()
    return sorted({str(d.get("canal")) for d in ((cola or {}).get("dudosos") or [])
                   if isinstance(d, dict) and str(d.get("clave")) == str(wp_id)})


# ── Meta (Facebook / Instagram) ───────────────────────────────────────────────

def _graph(metodo: str, oid: str, token: str, **params):
    """(status, json|None). Directo y, si no conecta, por WARP. Nunca devuelve texto de excepciones."""
    p = dict(params)
    p["access_token"] = token
    for px in (None, WARP):
        try:
            r = requests.request(metodo, "%s/%s" % (GRAPH, oid), params=p, proxies=px, timeout=30)
        except requests.exceptions.RequestException:
            continue
        try:
            return r.status_code, r.json()
        except ValueError:
            return r.status_code, None
    return 0, None


def _graph_existe(oid: str, token: str, campos: str):
    """(existe, link): existe True/False/None (None = no pude saber: token vencido, sin red)."""
    st, j = _graph("GET", oid, token, fields=campos)
    if st == 200 and isinstance(j, dict) and j.get("id"):
        return True, (j.get("permalink") or j.get("permalink_url") or "")
    err = (j or {}).get("error") if isinstance(j, dict) else None
    if isinstance(err, dict):
        if err.get("code") == 190:
            return None, ""
        if st in (400, 404) and (err.get("code") == 100 or err.get("error_subcode") in (33, 2018108)
                                 or "does not exist" in str(err.get("message", ""))):
            return False, ""
    if st == 404:
        return False, ""
    return None, ""


def borrar_meta(red: str, oid: str) -> Resultado:
    token = os.environ.get("FB_PAGE_TOKEN", "")
    if not token:
        return Resultado(DUDOSO, "falta FB_PAGE_TOKEN en el bot")
    if not _RE_ID_GRAPH.match(oid or ""):
        return Resultado(DUDOSO, "el id anotado no tiene forma de id de Meta")
    campos = "id,permalink_url" if red == "facebook" else "id,permalink"
    antes, link = _graph_existe(oid, token, campos)
    if antes is False:
        return Resultado(NO_HABIA, "ya no estaba publicado")
    if antes is None:
        return Resultado(DUDOSO, "no pude consultar a Meta (token vencido o sin red)")
    _graph("DELETE", oid, token)            # el código de respuesta no importa: el de Facebook es 500 aunque borre
    despues, _ = _graph_existe(oid, token, "id")
    if despues is False:
        return Resultado(BORRADO, "borrado y verificado")
    if despues is True:
        extra = ("Instagram no lo borró: borralo desde la app" if red.startswith("ig_")
                 else "Meta no lo dejó borrar")
        return Resultado(SIGUE, "%s%s" % (extra, (" · " + link) if link else ""))
    return Resultado(DUDOSO, "mandé el borrado pero no pude confirmarlo")


# ── Telegram (canal) ──────────────────────────────────────────────────────────

def _tg_embed_existe(msg_id: str):
    """Vista web pública del mensaje (GET a t.me, no toca la API del bot). True/False/None."""
    canal = os.environ.get("TELEGRAM_CHANNEL", "@MundoEmpresarial_AR")
    if not canal.startswith("@"):
        return None
    try:
        r = requests.get("https://t.me/%s/%s?embed=1" % (canal[1:], msg_id), timeout=15)
    except requests.exceptions.RequestException:
        return None
    if r.status_code != 200:
        return None
    return "Post not found" not in r.text


def _anotar_post(url, payload, respuesta=None, error=None):
    """Guardia: el borrado del canal también queda en tg_registro (registro_tg_bot.anotar_post; solo el token de ME). Nunca lanza."""
    try:
        import registro_tg_bot
        registro_tg_bot.anotar_post(url, payload, respuesta=respuesta, error=error)
    except Exception:
        pass


def borrar_telegram(msg_id: str) -> Resultado:
    token = os.environ.get("TELEGRAM_TOKEN", "")
    canal = os.environ.get("TELEGRAM_CHANNEL", "@MundoEmpresarial_AR")
    if not token:
        return Resultado(DUDOSO, "falta TELEGRAM_TOKEN en el bot")
    if not _RE_ID_NUM.match(msg_id or ""):
        return Resultado(DUDOSO, "el id anotado no es un número de mensaje")
    url, payload = "https://api.telegram.org/bot%s/deleteMessage" % token, {"chat_id": canal, "message_id": int(msg_id)}
    try:
        r = requests.post(url, json=payload, timeout=15)
        _anotar_post(url, payload, respuesta=r)
        j = r.json()
    except (requests.exceptions.RequestException, ValueError) as e:
        _anotar_post(url, payload, error=e)
        j = {}
    if j.get("ok") and j.get("result"):
        return Resultado(BORRADO, "mensaje borrado del canal")
    desc = str(j.get("description", ""))
    if "not found" in desc.lower():
        return Resultado(NO_HABIA, "ya no estaba en el canal")
    existe = _tg_embed_existe(msg_id)
    if existe is False:
        return Resultado(NO_HABIA, "ya no estaba en el canal")
    return Resultado(SIGUE if existe else DUDOSO, desc[:100] or "Telegram no confirmó el borrado")


# ── X (Twitter) ───────────────────────────────────────────────────────────────

def _x_auth():
    from requests_oauthlib import OAuth1
    g = os.environ.get
    return OAuth1(g("TW_KEY", "") or g("TWITTER_API_KEY", ""), g("TW_SECRET", "") or g("TWITTER_API_SECRET", ""),
                  g("TW_TOKEN", "") or g("TWITTER_ACCESS_TOKEN", ""), g("TW_TSECRET", "") or g("TWITTER_ACCESS_SECRET", ""))


def _x_existe(tweet_id: str):
    try:
        r = requests.get("https://api.twitter.com/2/tweets/%s" % tweet_id, auth=_x_auth(), timeout=15)
        j = r.json()
    except Exception:
        return None
    if r.status_code == 200 and (j.get("data") or {}).get("id"):
        return True
    if r.status_code == 404 or (r.status_code == 200 and j.get("errors")
                               and any("not found" in str(e.get("title", "")).lower() for e in j["errors"])):
        return False
    return None


def _x_borrar(tweet_id: str) -> bool:
    """Borra el tweet (y su hilo, si es la raíz de uno) con x_client del harness; si no carga, con la API directa."""
    try:
        if HARNESS_DIR not in sys.path:
            sys.path.insert(0, HARNESS_DIR)
        from agents import x_client
        return bool(x_client.delete(tweet_id))
    except Exception as e:
        log.warning("x_client.delete no disponible (%s); borro con la API directa", type(e).__name__)
    try:
        r = requests.delete("https://api.twitter.com/2/tweets/%s" % tweet_id, auth=_x_auth(), timeout=15)
        return r.status_code == 200
    except Exception:
        return False


def borrar_twitter(tweet_id: str) -> Resultado:
    if not _RE_ID_NUM.match(tweet_id or ""):
        return Resultado(DUDOSO, "el id anotado no es un id de tweet")
    antes = _x_existe(tweet_id)
    if antes is False:
        return Resultado(NO_HABIA, "ya no estaba publicado")
    _x_borrar(tweet_id)
    despues = _x_existe(tweet_id)
    if despues is False:
        return Resultado(BORRADO, "borrado y verificado")
    if despues is True:
        return Resultado(SIGUE, "X no lo borró (revisá los créditos de la API) · https://x.com/i/status/%s" % tweet_id)
    return Resultado(DUDOSO, "mandé el borrado pero no pude confirmarlo (la API de X no contestó)")


# ── LinkedIn ──────────────────────────────────────────────────────────────────

def _token_de_estado(ruta_env: str, defecto: str, env_token: str) -> str:
    """El harness renueva el token y lo deja en un archivo: ése es la fuente de verdad; el env es el respaldo."""
    try:
        with open(os.environ.get(ruta_env, defecto), encoding="utf-8") as f:
            t = (json.load(f) or {}).get("access_token", "")
            if t:
                return t
    except Exception:
        pass
    return os.environ.get(env_token, "")


def token_pagina() -> str:
    return _token_de_estado("LINKEDIN_TOKEN_STATE", os.path.join(HARNESS_DIR, "linkedin_org_token.json"), "LINKEDIN_ORG_TOKEN")


def token_personal() -> str:
    return _token_de_estado("LINKEDIN_MEMBER_STATE", os.path.join(HARNESS_DIR, "linkedin_member_token.json"), "LINKEDIN_TOKEN")


def _li(metodo: str, urn: str, token: str):
    url = "https://api.linkedin.com/rest/posts/" + urllib.parse.quote(urn, safe="")
    h = {"Authorization": "Bearer " + token, "X-Restli-Protocol-Version": "2.0.0"}
    r = None
    for v in LI_VERSIONES:                     # LinkedIn da de baja cada versión al año (426)
        h["LinkedIn-Version"] = v
        r = requests.request(metodo, url, headers=h, timeout=20)
        if r.status_code != 426:
            return r
    return r


def _li_gone(r) -> bool:
    if r.status_code == 404:
        return True
    if r.status_code == 200:
        try:
            return str((r.json() or {}).get("lifecycleState", "")).upper() == "DELETED"
        except ValueError:
            return False
    return False


def borrar_linkedin(urn: str, tokens: list) -> Resultado:
    """`tokens`: [(nombre, token)] en el orden en que se prueban (la página, la persona). El post es de uno solo."""
    if not _RE_URN.match(urn or ""):
        return Resultado(DUDOSO, "el id anotado no tiene forma de URN de LinkedIn")
    con_token = [(n, t) for n, t in tokens if t]
    if not con_token:
        return Resultado(DUDOSO, "no hay token de LinkedIn en el bot")
    vistos, dueno, prohibidos = [], None, []
    for nombre, tok in con_token:
        try:
            r = _li("GET", urn, tok)
        except requests.exceptions.RequestException:
            vistos.append("sin red")
            continue
        if r.status_code == 200 and not _li_gone(r):
            dueno = (nombre, tok)
            break
        vistos.append(404 if _li_gone(r) else r.status_code)
        if r.status_code == 403:
            prohibidos.append((nombre, tok))
    if not dueno:
        if vistos and all(v == 404 for v in vistos):
            return Resultado(NO_HABIA, "ya no estaba publicado")
        # Los posts del perfil PERSONAL que el bot publica con el token unificado (desde el 17/11/2026) no se pueden LEER con ese
        # token (403: falta r_member_social), pero sí borrar. 19/11/2026: el post personal de «ley fe emergencia» quedó «dudoso»
        # sin que se mandara el DELETE; mandado a mano dio 204 y después 404. Se prueba el DELETE con cada token que dio 403 y
        # solo cuenta como borrado si después el GET da 404 (verificado, como todo lo demás). El 401 (vencido) no se prueba.
        for nombre, tok in prohibidos:
            try:
                _li("DELETE", urn, tok)
                despues = _li("GET", urn, tok)
            except requests.exceptions.RequestException:
                continue
            if _li_gone(despues):
                return Resultado(BORRADO, "borrado y verificado (%s; LinkedIn no deja leerlo antes de borrar)" % nombre)
        return Resultado(DUDOSO, "no pude leerlo con ningún token (%s): vencido o sin permiso" % ", ".join(str(v) for v in vistos))
    nombre, tok = dueno
    try:
        _li("DELETE", urn, tok)
        despues = _li("GET", urn, tok)
    except requests.exceptions.RequestException:
        return Resultado(DUDOSO, "mandé el borrado pero no pude confirmarlo")
    if _li_gone(despues):
        return Resultado(BORRADO, "borrado y verificado (%s)" % nombre)
    if despues.status_code == 200:
        return Resultado(SIGUE, "LinkedIn no lo borró · https://www.linkedin.com/feed/update/%s/" % urn)
    return Resultado(DUDOSO, "mandé el borrado pero no pude confirmarlo (HTTP %s)" % despues.status_code)


# ── despachador ───────────────────────────────────────────────────────────────

def borrar_red(red: str, oid: str) -> Resultado:
    try:
        if red == "telegram":
            return borrar_telegram(oid)
        if red == "twitter":
            return borrar_twitter(oid)
        if red == "linkedin":
            return borrar_linkedin(oid, [("página", token_pagina()), ("personal", token_personal())])
        if red == "li_pag":
            return borrar_linkedin(oid, [("página", token_pagina())])
        if red in ("facebook", "ig_feed", "ig_story"):
            return borrar_meta(red, oid)
    except Exception as e:          # una red rota no frena a las demás
        log.error("borrar_red %s: %s", red, type(e).__name__)
        return Resultado(DUDOSO, "falló el borrado (%s)" % type(e).__name__)
    return Resultado(DUDOSO, "red desconocida")


def borrar_nota(seleccion, registro: dict, wp=None, dudosos: list = None) -> list:
    """Recorre TODAS las redes en `ORDEN`. Devuelve [(red, Resultado|None, id)]:
       · elegida y con id → el borrado verificado
       · sin id (elegida o no) → NO_HABIA («no hay posteo registrado»)
       · con id pero no elegida → None (se muestra como «no lo elegiste»)
       · wp: callable que devuelve Resultado (la papelera); solo si 'wp' fue elegida.
    Cada resultado queda en el journal."""
    sel = set(seleccion or ())
    out = []
    for red in ORDEN:
        oid = registro.get(red)
        if red == "wp":
            if "wp" in sel and wp:
                res = wp()
                log.info("borrado %s: %s — %s", red, res.estado, res.detalle)
                out.append((red, res, ""))
            continue
        if red not in sel:
            # con id pero sin elegir: se avisa que sigue publicado; sin id: no había posteo, no hay nada que decidir
            out.append((red, None if oid else Resultado(NO_HABIA, "no hay posteo registrado"), oid or ""))
            continue
        if not oid:
            res = Resultado(NO_HABIA, "no hay posteo registrado")
        else:
            res = borrar_red(red, oid)
        log.info("borrado %s id=%s: %s — %s", red, oid or "-", res.estado, res.detalle)
        out.append((red, res, oid or ""))
    return out


def formatear(titulo: str, resultados: list, dudosos: list = None) -> str:
    """El informe que ve Leo: una línea por red."""
    icono = {BORRADO: "✅", SIGUE: "❌", NO_HABIA: "ℹ️", DUDOSO: "⚠️"}
    txt = {BORRADO: "borrado", SIGUE: "SIGUE PUBLICADO", NO_HABIA: "no había posteo", DUDOSO: "no pude confirmarlo"}
    lineas = ["🗑️ %s" % titulo]
    for red, res, oid in resultados:
        if res is None:
            if oid:
                lineas.append("➖ %s: no lo elegiste (sigue publicado)" % ETIQUETA[red])
            continue
        lineas.append("%s %s: %s — %s" % (icono[res.estado], ETIQUETA[red], txt[res.estado], res.detalle))
    if dudosos:
        lineas.append("⚠️ Meta dejó como DUDOSO (pudo salir sin guardar el id): %s. Revisalo a mano en la página." % ", ".join(dudosos))
    if any(r is not None and r.estado in (SIGUE, DUDOSO) for _, r, _ in resultados):
        lineas.append("↻ Lo que sigue arriba se puede reintentar con /publicador <id> → Borrar.")
    lineas.append("Si algo salió en una red antes de que el bot guardara su id (frases hasta el 17/11), no lo veo: borralo a mano.")
    return "\n".join(lineas)


# ── posteos reemplazados (20/11/2026) ─────────────────────────────────────────
# Cuando el Jefe de redacción re-publica una nota en una red (harness agents/redes._anotar_ids, 19/11), el id del posteo viejo
# no se pierde: queda en «<clave>_ant» (lista con comas) del comentario mebot. Acá se leen y se borran junto con el actual, para
# que /publicador → Borrar no deje publicado el posteo anterior.

def anteriores_de_nota(contenido_wp: str) -> dict:
    """{red: [ids]} de los posteos reemplazados al re-publicar («<clave>_ant»)."""
    meta = parse_meta(contenido_wp)
    out = {}
    for red, k in CLAVE_META.items():
        ids = [x.strip() for x in (meta.get(k + "_ant") or "").split(",") if x.strip()]
        if ids:
            out[red] = ids
    return out


def borrar_anteriores(seleccion, anteriores: dict) -> list:
    """[(red, Resultado, id)] borrando los posteos anteriores de las redes elegidas (verificado, como el resto)."""
    sel = set(seleccion or ())
    out = []
    for red in ORDEN:
        if red == "wp" or red not in sel:
            continue
        for oid in (anteriores or {}).get(red, []):
            res = borrar_red(red, oid)
            log.info("borrado anterior %s id=%s: %s — %s", red, oid, res.estado, res.detalle)
            out.append((red, Resultado(res.estado, "posteo anterior · " + res.detalle), oid))
    return out
