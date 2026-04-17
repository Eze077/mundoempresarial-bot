import os
import re
import json
import logging
import asyncio
import base64
import unicodedata
from datetime import datetime, time as dtime
import requests
from requests_oauthlib import OAuth1
from bs4 import BeautifulSoup
import trafilatura
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    filters,
    CallbackQueryHandler,
    ContextTypes,
)

logging.basicConfig(
    format="%(asctime)s - %(levelname)s - %(message)s", level=logging.INFO
)
logger = logging.getLogger(__name__)

TELEGRAM_TOKEN = os.environ["TELEGRAM_TOKEN"]
WP_URL  = os.environ.get("WP_URL", "https://mundoempresarial.ar").rstrip("/")
WP_USER = os.environ["WP_USER"]
WP_PASS = os.environ["WP_PASS"]

TWITTER_API_KEY    = os.environ.get("TW_KEY", "") or os.environ.get("TWITTER_API_KEY", "")
TWITTER_API_SECRET = os.environ.get("TW_SECRET", "") or os.environ.get("TWITTER_API_SECRET", "")
TWITTER_TOKEN      = os.environ.get("TW_TOKEN", "") or os.environ.get("TWITTER_ACCESS_TOKEN", "")
TWITTER_SECRET     = os.environ.get("TW_TSECRET", "") or os.environ.get("TWITTER_ACCESS_SECRET", "")

TELEGRAM_CHANNEL   = os.environ.get("TELEGRAM_CHANNEL", "@EmpresarialARG")
# Chat ID del operador para reportes diarios (se detecta del primer mensaje)
ADMIN_CHAT_ID      = os.environ.get("ADMIN_CHAT_ID", "")

HEADERS_BROWSER = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/131.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8",
    "Accept-Language": "es-AR,es;q=0.9,en-US;q=0.8,en;q=0.7",
    "Accept-Encoding": "gzip, deflate",
    "Referer": "https://www.google.com/",
    "DNT": "1",
    "Connection": "keep-alive",
    "Upgrade-Insecure-Requests": "1",
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "cross-site",
    "Sec-Fetch-User": "?1",
    "Sec-Ch-Ua": '"Chromium";v="131", "Not_A Brand";v="24"',
    "Sec-Ch-Ua-Mobile": "?0",
    "Sec-Ch-Ua-Platform": '"Windows"',
    "Cache-Control": "max-age=0",
}

# Palabras vacías en español para extracción de keywords
STOP_WORDS = {
    "el", "la", "los", "las", "un", "una", "de", "del", "en", "y", "o", "a",
    "que", "por", "con", "se", "su", "es", "al", "para", "este", "esta",
    "esto", "ese", "esa", "más", "pero", "como", "son", "fue", "ser", "ha",
    "han", "hay", "no", "si", "ya", "le", "lo", "les", "me", "mi", "sus",
    "nos", "ante", "bajo", "hasta", "sobre", "tras", "entre", "sin", "sus",
    "también", "cuando", "donde", "quien", "cuyo", "aunque", "porque",
    "puede", "desde", "cada", "todo", "toda", "todos", "todas", "muy",
    "cómo", "qué", "será", "sido", "están", "están",
}

# ID de la categoría Destacados
CAT_DESTACADOS = 337

# Mapa de categorías WordPress → palabras clave (minúsculas)
CATEGORY_KEYWORDS = {
    95:  ["afip", "arca", "impuesto", "impuestos", "factura", "facturación",
          "monotributo", "monotributista", "iva", "ganancias", "declaración jurada",
          "fisco", "recaudación", "blanqueo", "renta", "retención", "percepción"],
    88:  ["agro", "campo", "agricultura", "ganadería", "soja", "trigo", "maíz",
          "cosecha", "agroexportación", "rural", "agroindustria", "granos", "bovino",
          "porcino", "tambero", "siembra"],
    1048:["cobertura", "seguro", "seguros", "aseguradora", "póliza", "reaseguro",
          "superintendencia de seguros"],
    89:  ["comercio", "retail", "venta", "ventas", "consumo", "consumidor",
          "minorista", "mayorista", "shopping", "supermercado", "inflación de precios"],
    99:  ["congreso", "diputados", "senado", "senadores", "legislatura",
          "proyecto de ley", "cámara", "sesión", "legislativo"],
    239: ["digital", "digitalización", "tecnología", "software", "app", "aplicación",
          "ecommerce", "e-commerce", "startup", "inteligencia artificial", "ia",
          "fintech", "blockchain", "automatización", "plataforma"],
    94:  ["economía", "inflación", "dólar", "tipo de cambio", "reservas",
          "banco central", "bcra", "pbi", "recesión", "crecimiento", "cepo",
          "devaluación", "tasas", "tasa", "deuda", "déficit", "superávit",
          "ajuste fiscal", "fmi", "bono", "bonos", "merval"],
    96:  ["empresa", "empresas", "pyme", "pymes", "negocio", "negocios",
          "emprendimiento", "ceo", "directivo", "corporativo", "holding",
          "fusión", "adquisición", "inversión", "exportador"],
    100: ["gobierno", "ministerio", "ministro", "presidencia", "jefatura",
          "milei", "decreto", "resolución", "secretaría", "subsecretaría",
          "licitación", "obra pública", "estado"],
    90:  ["industria", "manufactura", "fábrica", "producción", "acero",
          "petroquímica", "automotriz", "autopartista", "textil", "metalmecánica",
          "pymes industriales", "parque industrial"],
    103: ["informe", "encuesta", "estadística", "datos", "relevamiento",
          "estudio", "ranking", "índice", "indec", "ipc", "emae"],
    97:  ["internacional", "mundial", "global", "exterior", "exportación",
          "importación", "china", "eeuu", "estados unidos", "brasil", "trump",
          "unión europea", "fondo monetario", "banco mundial", "mercosur"],
    98:  ["argentina", "nacional", "país", "nación", "porteño", "bonaerense"],
    91:  ["opinión", "análisis", "columna", "reflexión", "editorial", "perspectiva"],
    101: ["judicial", "juicio", "tribunal", "corte suprema", "juez", "causa",
          "condena", "fallo", "imputado", "procesado", "fiscalía"],
    87:  ["política", "político", "elecciones", "partido", "candidato",
          "kirchner", "peronismo", "oficialismo", "oposición", "coalición",
          "campaña electoral", "gobernador", "intendente"],
    102: ["provincia", "provincial", "municipal", "ciudad", "gobernación",
          "municipio", "intendencia", "presupuesto provincial"],
    92:  ["servicio", "servicios", "salud", "educación", "transporte",
          "energía", "luz", "gas", "agua", "tarifas", "utilities"],
    93:  ["sindicato", "gremio", "sindical", "paritaria", "salario", "sueldo",
          "convenio colectivo", "huelga", "paro", "cgt", "uom", "camioneros"],
}

def detect_categories(title: str, text: str, excerpt: str) -> list:
    corpus = (title + " " + title + " " + title + " " + excerpt + " " + (text[:600] or "")).lower()
    scores = {}
    for cat_id, kws in CATEGORY_KEYWORDS.items():
        score = sum(corpus.count(kw) for kw in kws)
        if score > 0:
            scores[cat_id] = score
    if not scores:
        return [98]
    ranked = sorted(scores, key=scores.get, reverse=True)
    return ranked[:3]


# ── Estadísticas diarias ─────────────────────────────────────────────────────

_daily_stats = {
    "published": 0,
    "cancelled": 0,
    "errors": 0,
    "sites": {},       # dominio → cantidad
    "titles": [],      # títulos publicados
    "date": datetime.now().strftime("%Y-%m-%d"),
}


def _reset_stats_if_new_day():
    today = datetime.now().strftime("%Y-%m-%d")
    if _daily_stats["date"] != today:
        _daily_stats["published"] = 0
        _daily_stats["cancelled"] = 0
        _daily_stats["errors"] = 0
        _daily_stats["sites"] = {}
        _daily_stats["titles"] = []
        _daily_stats["date"] = today


def stat_publish(title: str, source_url: str):
    _reset_stats_if_new_day()
    _daily_stats["published"] += 1
    _daily_stats["titles"].append(title[:60])
    from urllib.parse import urlparse
    domain = urlparse(source_url).netloc.replace("www.", "")
    _daily_stats["sites"][domain] = _daily_stats["sites"].get(domain, 0) + 1


def stat_cancel():
    _reset_stats_if_new_day()
    _daily_stats["cancelled"] += 1


def stat_error():
    _reset_stats_if_new_day()
    _daily_stats["errors"] += 1


def build_daily_report() -> str:
    _reset_stats_if_new_day()
    s = _daily_stats
    sites_str = "\n".join(f"  • {d}: {c}" for d, c in sorted(s["sites"].items(), key=lambda x: -x[1])) or "  (ninguno)"
    titles_str = "\n".join(f"  {i+1}. {t}" for i, t in enumerate(s["titles"])) or "  (ninguna)"
    return (
        f"📊 *Reporte diario — {s['date']}*\n\n"
        f"✅ Publicadas: *{s['published']}*\n"
        f"❌ Canceladas: *{s['cancelled']}*\n"
        f"⚠️ Errores scraping: *{s['errors']}*\n\n"
        f"*Fuentes:*\n{sites_str}\n\n"
        f"*Notas publicadas:*\n{titles_str}"
    )


# ── Helpers SEO ────────────────────────────────────────────────────────────────

def seo_title(title: str) -> str:
    if len(title) <= 60:
        return title
    cut = title[:60]
    boundary = cut.rfind(" ")
    return cut[:boundary] if boundary > 40 else cut


def meta_description(excerpt: str, text: str, kw: str = "") -> str:
    raw = (excerpt or text or "").strip()
    if kw and kw.lower() not in raw.lower():
        raw = f"{kw}: {raw}"
    if len(raw) <= 155:
        return raw
    cut = raw[:152]
    boundary = cut.rfind(" ")
    return (cut[:boundary] if boundary > 100 else cut) + "..."


def focus_keyword(title: str) -> str:
    for w in title.split():
        clean = w.strip('.,;:!?()[]"\'«»—:')
        if clean.lower() not in STOP_WORDS and len(clean) > 3:
            return clean
    return title.split()[0]


def url_slug(title: str) -> str:
    slug = title.lower()
    slug = unicodedata.normalize("NFKD", slug)
    slug = "".join(c for c in slug if not unicodedata.combining(c))
    slug = re.sub(r"[^a-z0-9]+", "-", slug).strip("-")
    if len(slug) > 50:
        slug = slug[:50].rsplit("-", 1)[0]
    return slug


def extract_tags(title: str) -> list:
    words = [
        w.strip('.,;:!?()[]"\'«»—').capitalize()
        for w in title.split()
        if w.lower().strip('.,;:!?()[]"\'«»—') not in STOP_WORDS and len(w) > 3
    ]
    return list(dict.fromkeys(words))[:6]


def pyme_summary(text: str, excerpt: str) -> str:
    raw = (excerpt or text or "").strip()
    for sep in (".", "?", "!"):
        idx = raw.find(sep)
        if 60 < idx <= 237:
            return raw[: idx + 1]
    if len(raw) <= 240:
        return raw
    cut = raw[:237]
    boundary = cut.rfind(" ")
    return (cut[:boundary] if boundary > 150 else cut) + "..."


def pyme_box(text: str, excerpt: str) -> str:
    summary = pyme_summary(text, excerpt)
    return (
        '\n<div style="'
        "background:#eaf4fb;"
        "border-left:5px solid #1a6fa8;"
        "padding:16px 20px;"
        "margin:32px 0 16px 0;"
        "border-radius:0 6px 6px 0;"
        '">'
        '<p style="margin:0 0 8px 0;font-size:13px;font-weight:700;'
        'letter-spacing:1px;color:#1a6fa8;text-transform:uppercase;">'
        "&#128196; Resumen para Pymes"
        "</p>"
        f'<p style="margin:0;font-size:15px;line-height:1.6;color:#222;">'
        f"{summary}"
        "</p>"
        "</div>\n"
    )


def _generate_h2(paragraphs: list, kw: str) -> list:
    """
    Genera H2 descriptivos basados en el contenido real de los párrafos.
    Analiza cada grupo de párrafos y extrae la idea principal para el H2.
    El primer H2 siempre incluye el keyword.
    """
    labels = []

    # Primer H2 con keyword (Rank Math: keyword en subheadings)
    labels.append(f"{kw}: lo que hay que saber" if kw else "Lo que hay que saber")

    # Para los siguientes H2, extraer la idea principal del párrafo siguiente
    # Buscar declaraciones con comillas, datos numéricos, o temas clave
    quote_re = re.compile(r'[""«](.{10,60}?)[""»]')
    number_re = re.compile(r'\d+[.,]?\d*\s*%|\$\s*[\d.,]+')

    for i, para in enumerate(paragraphs):
        if i < 3:
            continue  # los primeros párrafos ya están cubiertos por el primer H2
        if i % 3 != 0:
            continue  # solo generar H2 cada ~3 párrafos

        # Intentar extraer una frase descriptiva
        # 1) Si tiene una cita, usar la atribución
        qm = quote_re.search(para)
        if qm:
            labels.append("Qué dicen los protagonistas")
            continue

        # 2) Si tiene datos numéricos
        nm = number_re.search(para)
        if nm:
            labels.append("Los números clave")
            continue

        # 3) Buscar palabras temáticas en el párrafo
        low = para.lower()
        if any(w in low for w in ("futuro", "próximo", "vendrá", "proyección", "perspectiva")):
            labels.append("Qué se espera")
        elif any(w in low for w in ("impacto", "consecuencia", "efecto", "afecta", "repercus")):
            labels.append("El impacto en la economía real")
        elif any(w in low for w in ("pyme", "empresa", "negocio", "comercio", "industria")):
            labels.append("Cómo afecta a las pymes")
        elif any(w in low for w in ("gobierno", "oficial", "ministerio", "estado")):
            labels.append("La posición oficial")
        elif any(w in low for w in ("mercado", "bolsa", "dólar", "inversión", "bonos")):
            labels.append("El panorama del mercado")
        elif any(w in low for w in ("contexto", "historia", "antecedente", "origen")):
            labels.append("Contexto y antecedentes")
        else:
            # Genérico temático: usar las primeras palabras significativas del párrafo
            words = [w for w in para.split()[:8]
                     if w.lower().strip('.,;:!?"\'') not in STOP_WORDS and len(w) > 3]
            if words:
                phrase = " ".join(words[:4])
                if len(phrase) > 5:
                    labels.append(phrase.capitalize().rstrip('.,;:'))

    # Eliminar duplicados consecutivos
    deduped = [labels[0]]
    for lb in labels[1:]:
        if lb != deduped[-1]:
            deduped.append(lb)

    return deduped


def format_content(data: dict, kw: str = "") -> str:
    """
    Estructura SEO del contenido según el Manual de Estilo:
    - Lead en negrita (primer párrafo, con keyword)
    - H2 descriptivos cada 3 párrafos (no genéricos)
    - Primer H2 incluye keyword (Rank Math)
    - Párrafos cortos (<= 120 palabras)
    - Citas en párrafo propio
    - Datos en negrita
    - Recuadro RESUMEN PARA PYMES
    - Link externo dofollow a la fuente (Rank Math: external link)
    """
    raw_text = data["text"]

    # Dividir texto largo en párrafos reales (max ~100 palabras cada uno)
    raw_paragraphs = [p.strip() for p in raw_text.split("\n") if p.strip()]

    # Si el texto viene como un bloque sin saltos, dividir por oraciones
    if len(raw_paragraphs) <= 2 and len(raw_text) > 500:
        sentences = re.split(r'(?<=[.!?])\s+', raw_text.strip())
        raw_paragraphs = []
        current = []
        word_count = 0
        for sent in sentences:
            wc = len(sent.split())
            if word_count + wc > 100 and current:
                raw_paragraphs.append(" ".join(current))
                current = [sent]
                word_count = wc
            else:
                current.append(sent)
                word_count += wc
        if current:
            raw_paragraphs.append(" ".join(current))

    # Asegurar que ningún párrafo exceda 120 palabras (Rank Math: short paragraphs)
    paragraphs = []
    for p in raw_paragraphs:
        words = p.split()
        if len(words) > 120:
            chunks = []
            for i in range(0, len(words), 100):
                chunk = " ".join(words[i:i+100])
                # Buscar punto para cortar limpio
                last_dot = chunk.rfind(". ")
                if last_dot > len(chunk) * 0.5:
                    chunks.append(chunk[:last_dot + 1])
                    remaining = chunk[last_dot + 2:].strip()
                    if remaining:
                        chunks.append(remaining)
                else:
                    chunks.append(chunk)
            paragraphs.extend(chunks)
        else:
            paragraphs.append(p)

    if not paragraphs:
        first_h2 = f"{kw}: lo que hay que saber" if kw else "Lo que hay que saber"
        return (
            f"<h2>{first_h2}</h2>\n"
            f'<p>{data["excerpt"]}</p>\n'
            + pyme_box(data["text"], data["excerpt"])
            + f'<p><em>Fuente: <a href="{data["source_url"]}" '
            f'target="_blank" rel="noopener noreferrer">Ver nota original</a></em></p>'
        )

    # Generar H2 descriptivos basados en el contenido
    h2_labels = _generate_h2(paragraphs, kw)

    # Resaltar cifras y datos numéricos en negrita
    number_pattern = re.compile(
        r'(\$\s*[\d.,]+(?:\s*(?:millones|billones|mil))?'
        r'|\d+[.,]\d+\s*%'
        r'|\d+\s*%'
        r'|\d+[.,]\d+\s*(?:puntos|pb))'
    )

    parts = []
    h2_index = 0

    for i, para in enumerate(paragraphs):
        # Resaltar números/datos
        para_html = number_pattern.sub(r'<strong>\1</strong>', para)

        if i == 0:
            # Lead en negrita
            parts.append(f"<p><strong>{para_html}</strong></p>")
            if h2_index < len(h2_labels):
                parts.append(f"<h2>{h2_labels[h2_index]}</h2>")
                h2_index += 1
        else:
            # H2 cada 3 párrafos
            if i % 3 == 0 and h2_index < len(h2_labels):
                parts.append(f"<h2>{h2_labels[h2_index]}</h2>")
                h2_index += 1
            parts.append(f"<p>{para_html}</p>")

    # Recuadro Pymes
    parts.append(pyme_box(data["text"], data["excerpt"]))

    # Fuente (link externo dofollow — Rank Math: 4+2 pts)
    parts.append(
        f'<p><em>Fuente: <a href="{data["source_url"]}" '
        f'target="_blank" rel="noopener noreferrer">Ver nota original</a></em></p>'
    )
    return "\n".join(parts)


# ── WordPress API ──────────────────────────────────────────────────────────────

def wp_auth():
    token = base64.b64encode(f"{WP_USER}:{WP_PASS}".encode()).decode()
    return {"Authorization": f"Basic {token}"}


def get_or_create_tags(names: list) -> list:
    ids = []
    h = {**wp_auth(), "Content-Type": "application/json"}
    for name in names:
        try:
            r = requests.post(
                f"{WP_URL}/wp-json/wp/v2/tags", headers=h,
                json={"name": name}, timeout=10
            )
            if r.status_code == 201:
                ids.append(r.json()["id"])
            elif r.status_code == 400 and "term_exists" in r.text:
                existing_id = r.json().get("data", {}).get("term_id")
                if existing_id:
                    ids.append(existing_id)
        except Exception as e:
            logger.warning(f"Tag '{name}': {e}")
    return ids


def upload_image(image_url: str, alt: str = "") -> int | None:
    try:
        img = requests.get(image_url, headers=HEADERS_BROWSER, timeout=15)
        img.raise_for_status()
        ctype = img.headers.get("Content-Type", "image/jpeg").split(";")[0]
        ext = ctype.split("/")[-1]

        h = {**wp_auth(), "Content-Disposition": f"attachment; filename=nota.{ext}",
             "Content-Type": ctype}
        r = requests.post(
            f"{WP_URL}/wp-json/wp/v2/media", headers=h, data=img.content, timeout=30
        )
        if r.status_code == 201:
            media_id = r.json()["id"]
            if alt:
                requests.post(
                    f"{WP_URL}/wp-json/wp/v2/media/{media_id}",
                    headers={**wp_auth(), "Content-Type": "application/json"},
                    json={"alt_text": alt, "caption": alt},
                    timeout=10,
                )
            return media_id
        logger.warning(f"Media {r.status_code}: {r.text[:200]}")
    except Exception as e:
        logger.warning(f"upload_image: {e}")
    return None


def publish_post(data: dict, image_id: int | None, destacado: bool = False) -> str | None:
    s_title  = data["title"] if data.get("title_edited") else seo_title(data["title"])
    s_kw     = focus_keyword(data["title"])
    s_desc   = meta_description(data["excerpt"], data["text"], kw=s_kw)
    s_slug   = url_slug(data["title"])
    content  = format_content(data, kw=s_kw)

    cat_ids = detect_categories(data["title"], data["text"], data["excerpt"])
    if destacado and CAT_DESTACADOS not in cat_ids:
        cat_ids = [CAT_DESTACADOS] + cat_ids

    tag_names = extract_tags(data["title"])
    first_para = (data["text"].split("\n")[0] if data["text"] else "")
    tag_names += [
        w.strip('.,;:!?()[]"\'«»—').capitalize()
        for w in first_para.split()
        if w.lower().strip('.,;:!?()[]"\'«»—') not in STOP_WORDS and len(w) > 4
    ]
    tag_names = list(dict.fromkeys(tag_names))[:8]
    tag_ids = get_or_create_tags(tag_names)

    payload = {
        "title":      s_title,
        "content":    content,
        "excerpt":    s_desc,
        "status":     "publish",
        "slug":       s_slug,
        "categories": cat_ids,
        "tags":       tag_ids,
        "meta": {
            "rank_math_title":            s_title,
            "rank_math_description":      s_desc,
            "rank_math_focus_keyword":    s_kw,
            "rank_math_robots":           ["index", "follow"],
            "rank_math_og_content_image": data.get("image_url", ""),
        },
    }
    if image_id:
        payload["featured_media"] = image_id

    h = {**wp_auth(), "Content-Type": "application/json"}
    r = requests.post(f"{WP_URL}/wp-json/wp/v2/posts", headers=h, json=payload, timeout=30)
    if r.status_code == 201:
        return r.json().get("link")
    logger.error(f"WP {r.status_code}: {r.text[:400]}")
    return None


# ── Twitter / X ───────────────────────────────────────────────────────────────

def build_tweet(data: dict, wp_url: str, hashtags_override: str = None) -> str:
    title = data["title"] if data.get("title_edited") else seo_title(data["title"])
    if hashtags_override is not None:
        hashtags = hashtags_override
    else:
        raw_tags = extract_tags(data["title"])[:3]
        hashtags = " ".join(f"#{t}" for t in raw_tags) + " #Pymes"

    tweet = f"{title}\n\n{wp_url}\n\n{hashtags}"
    if len(tweet) > 280:
        max_title = 280 - len(wp_url) - len(hashtags) - 6
        title = title[:max_title].rsplit(" ", 1)[0]
        tweet = f"{title}\n\n{wp_url}\n\n{hashtags}"
    return tweet


def post_tweet(data: dict, wp_url: str, hashtags_override: str = None) -> str | None:
    try:
        tweet_text = build_tweet(data, wp_url, hashtags_override=hashtags_override)
        auth = OAuth1(TWITTER_API_KEY, TWITTER_API_SECRET, TWITTER_TOKEN, TWITTER_SECRET)
        r = requests.post("https://api.twitter.com/2/tweets", json={"text": tweet_text}, auth=auth)
        if r.status_code == 201:
            tweet_id = r.json()["data"]["id"]
            return f"https://twitter.com/i/web/status/{tweet_id}"
        logger.error(f"Twitter {r.status_code}: {r.text[:400]}")
        return None
    except Exception as e:
        logger.error(f"Twitter error: {e}")
        return None


# ── Limpieza de texto scrapeado ────────────────────────────────────────────────

NOISE_FRAGMENTS = [
    "your browser doesn", "html5 audio", "html5 video",
    "compartir esta noticia", "compartir en",
    "dejanos tu comentario", "dejar un comentario",
    "leé más notas", "lee mas notas", "leer más",
    "más notas de", "notas relacionadas",
    "seguinos en", "seguinos", "suscribite", "suscríbete",
    "newsletter", "publicidad", "advertisement",
    "también te puede interesar", "te puede interesar",
    "artículos relacionados", "tags:", "etiquetas:", "compartir:",
    "volver arriba", "cargar más", "ver más",
    "todos los derechos reservados", "términos y condiciones",
    "política de privacidad", "cookies", "javascript",
    "whatsapp", "facebook", "twitter", "telegram",
    "copiar enlace", "imprimir", "guardar",
    "minutos de lectura", "min read",
]


def clean_text(raw: str) -> str:
    if not raw:
        return ""

    clean = []
    for line in raw.split("\n"):
        s = line.strip()
        if not s:
            continue
        low = s.lower()

        if any(frag in low for frag in NOISE_FRAGMENTS):
            continue

        if any(c in s for c in ("Ã", "Â", "â€", "Ã©", "Ã¡", "Ã³", "Ã±")):
            continue

        if len(s) < 25 and s[-1] not in ".?!:":
            continue

        clean.append(s)

    return "\n".join(clean)


# ── Scraper ────────────────────────────────────────────────────────────────────

def _fix_encoding(resp: requests.Response) -> str:
    raw = resp.content
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        pass
    return raw.decode("latin-1")


def _extract_jsonld(soup: BeautifulSoup) -> dict | None:
    """Extrae datos del artículo desde JSON-LD (schema.org), si existe."""
    for script in soup.find_all("script", type="application/ld+json"):
        try:
            data = json.loads(script.string or "")
            # Manejar @graph (usado por muchos sitios)
            if isinstance(data, dict) and "@graph" in data:
                data = data["@graph"]
            if isinstance(data, list):
                data = next(
                    (d for d in data if d.get("@type") in
                     ("NewsArticle", "Article", "WebPage", "ReportageNewsArticle")),
                    None
                )
            if not data:
                continue
            if data.get("@type") in ("NewsArticle", "Article", "WebPage", "ReportageNewsArticle"):
                body = (data.get("articleBody") or "").replace("\xa0", " ").strip()
                if not body:
                    continue
                # Imagen: puede ser string, dict, o lista
                img = data.get("image", "")
                if isinstance(img, dict):
                    img = img.get("url", "")
                elif isinstance(img, list):
                    img = img[0] if img else ""
                    if isinstance(img, dict):
                        img = img.get("url", "")
                # Author
                author = data.get("author", "")
                if isinstance(author, dict):
                    author = author.get("name", "")
                elif isinstance(author, list) and author:
                    author = author[0].get("name", "") if isinstance(author[0], dict) else str(author[0])

                return {
                    "title": data.get("headline", ""),
                    "text": body,
                    "author": author,
                    "image_url": img,
                }
        except (json.JSONDecodeError, StopIteration):
            continue
    return None


def _detect_media(soup: BeautifulSoup, url: str) -> dict:
    """Detecta si la nota tiene video o foto destacada embebida."""
    media = {"has_video": False, "has_photo": False, "video_url": "", "photo_url": ""}

    # Detectar videos embebidos
    for tag in soup.find_all(["iframe", "video"]):
        src = tag.get("src", "") or tag.get("data-src", "")
        if any(v in src for v in ("youtube", "youtu.be", "vimeo", "dailymotion", "twitter.com/i/videos")):
            media["has_video"] = True
            media["video_url"] = src
            break

    # Detectar videos por meta tags
    og_video = soup.find("meta", property="og:video")
    if og_video and og_video.get("content"):
        media["has_video"] = True
        media["video_url"] = og_video["content"]

    # og:type = video indica video
    og_type = soup.find("meta", property="og:type")
    if og_type and "video" in (og_type.get("content") or "").lower():
        media["has_video"] = True

    # La foto de portada siempre se captura via og:image
    og_image = soup.find("meta", property="og:image")
    if og_image and og_image.get("content"):
        media["has_photo"] = True
        media["photo_url"] = og_image["content"]

    return media


def scrape(url: str) -> dict:
    session = requests.Session()
    session.headers.update(HEADERS_BROWSER)
    resp = session.get(url, timeout=20)
    resp.raise_for_status()

    html = _fix_encoding(resp)

    # Detectar SPA (React/Vue/Angular) — contenido cargado por JS
    if len(html) < 5000 and ('id="root"' in html or 'id="app"' in html or 'id="__next"' in html):
        # Intentar con Google Cache como fallback para SPAs
        from urllib.parse import quote
        cache_url = f"https://webcache.googleusercontent.com/search?q=cache:{quote(url)}"
        try:
            cache_resp = session.get(cache_url, timeout=15)
            if cache_resp.status_code == 200 and len(cache_resp.text) > 5000:
                html = _fix_encoding(cache_resp)
                logger.info(f"SPA detectado, usando Google Cache para {url}")
        except Exception:
            pass

    soup = BeautifulSoup(html, "html.parser")

    def meta(prop):
        tag = soup.find("meta", property=prop) or soup.find("meta", attrs={"name": prop})
        return (tag.get("content") or "").strip() if tag else ""

    title = (
        meta("og:title")
        or (soup.find("h1") or soup.new_tag("x")).get_text().strip()
        or (soup.find("title") or soup.new_tag("x")).get_text().strip()
        or "Sin título"
    )
    image_url = meta("og:image")
    excerpt   = meta("og:description")

    # Detectar media (video/foto)
    media_info = _detect_media(soup, url)

    # 1) Intentar JSON-LD primero
    ld = _extract_jsonld(soup)
    if ld and len(ld["text"]) > 100:
        text = clean_text(ld["text"])
        title = ld["title"] or title
        image_url = ld["image_url"] or image_url
    else:
        # 2) Fallback a trafilatura
        text = clean_text(trafilatura.extract(html) or "")

    # 3) Si trafilatura también falla, intentar selectores de noticias comunes
    if not text or len(text) < 100:
        article_selectors = [
            "article", ".article-body", ".article-content", ".entry-content",
            ".post-content", ".story-body", ".nota-body", '[itemprop="articleBody"]',
            ".body-nota", ".article__body", "#article-body", ".cuerpo-nota",
        ]
        for sel in article_selectors:
            el = soup.select_one(sel)
            if el and len(el.get_text(strip=True)) > 200:
                paras = [p.get_text(strip=True) for p in el.find_all("p") if len(p.get_text(strip=True)) > 20]
                if paras:
                    text = clean_text("\n".join(paras))
                    break

    excerpt = excerpt or (text[:200] + "..." if text else "")

    return {
        "title":      title.strip(),
        "text":       text,
        "excerpt":    excerpt,
        "image_url":  image_url,
        "source_url": url,
        "media":      media_info,
    }


# ── Canal de Telegram ─────────────────────────────────────────────────────────

async def publish_to_channel(bot, data: dict, wp_url: str):
    s_title = data["title"] if data.get("title_edited") else seo_title(data["title"])
    text = f"📰 *{s_title}*\n\n{data['excerpt'][:200]}\n\n🔗 [Leer nota completa]({wp_url})"
    try:
        if data.get("image_url"):
            await bot.send_photo(
                chat_id=TELEGRAM_CHANNEL,
                photo=data["image_url"],
                caption=text,
                parse_mode="Markdown",
            )
        else:
            await bot.send_message(
                chat_id=TELEGRAM_CHANNEL,
                text=text,
                parse_mode="Markdown",
                disable_web_page_preview=False,
            )
        return True
    except Exception as e:
        logger.error(f"Canal TG: {e}")
        return False


# ── Handlers Telegram ──────────────────────────────────────────────────────────

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Hola! Comandos disponibles:\n\n"
        "Pega un link → analiza y publica la nota\n"
        "/borrar <URL o ID> → manda una nota a la papelera\n"
        "/stats → ver estadísticas del día"
    )


async def cmd_stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Muestra las estadísticas del día."""
    await update.message.reply_text(build_daily_report(), parse_mode="Markdown")


CAT_NAMES = {
    95: "AFIP", 88: "Agro", 1048: "Coberturas", 89: "Comercio",
    99: "Congreso", 337: "Destacados", 239: "Digitalización Pymes",
    94: "Economía", 96: "Empresas", 100: "Gobierno", 90: "Industria",
    103: "Informes", 97: "Internacional", 98: "Nacional", 91: "Opinión",
    101: "Poder Judicial", 87: "Política", 338: "Principales",
    102: "Provincias", 92: "Servicios", 93: "Sindicatos",
}

def build_preview_kb(tw_on: bool = True, tg_on: bool = True, wa_on: bool = False, dest_on: bool = False) -> InlineKeyboardMarkup:
    tw_label = "✅ Twitter" if tw_on else "❌ Twitter"
    tg_label = "✅ Canal TG" if tg_on else "❌ Canal TG"
    wa_label = "✅ WhatsApp" if wa_on else "❌ WhatsApp"
    dest_label = "⭐ Destacado" if dest_on else "☆ Destacado"
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton(tw_label, callback_data="toggle_tw"),
            InlineKeyboardButton(tg_label, callback_data="toggle_tg"),
        ],
        [
            InlineKeyboardButton(wa_label, callback_data="toggle_wa"),
            InlineKeyboardButton(dest_label, callback_data="toggle_dest"),
        ],
        [
            InlineKeyboardButton("Publicar", callback_data="pub"),
        ],
        [
            InlineKeyboardButton("Cambiar titulo", callback_data="change_title"),
            InlineKeyboardButton("Cancelar", callback_data="cancel"),
        ],
    ])


def build_preview(data: dict) -> str:
    s_title = data["title"] if data.get("title_edited") else seo_title(data["title"])
    s_kw    = focus_keyword(data["title"])
    s_desc  = meta_description(data["excerpt"], data["text"], kw=s_kw)
    s_slug  = url_slug(data["title"])
    words   = len(data["text"].split())
    cat_ids = detect_categories(data["title"], data["text"], data["excerpt"])
    cats_str    = " · ".join(CAT_NAMES.get(c, str(c)) for c in cat_ids)
    tag_preview = " · ".join(extract_tags(data["title"])[:5])

    # Indicar media detectada
    media = data.get("media", {})
    media_str = "No"
    if media.get("has_video") and media.get("has_photo"):
        media_str = "📸 Foto + 🎬 Video"
    elif media.get("has_video"):
        media_str = "🎬 Video"
    elif media.get("has_photo"):
        media_str = "📸 Foto"

    return (
        f"*{s_title}*\n\n"
        f"*Keyword:* {s_kw}\n"
        f"*Slug:* /{s_slug}\n"
        f"*Categorias:* {cats_str}\n"
        f"*Etiquetas:* {tag_preview}\n\n"
        f"_{s_desc}_\n\n"
        f"Imagen: {media_str}  |  Palabras: ~{words}"
    )


async def cmd_testtwitter(update: Update, context: ContextTypes.DEFAULT_TYPE):
    def mask(s: str) -> str:
        if not s:
            return "(VACIO)"
        s = s.strip()
        return f"{s[:4]}...{s[-4:]} (len={len(s)})"

    import os as _os
    tw_vars = [k for k in _os.environ if "TWITTER" in k or k.startswith("TW_")]
    all_vars_str = ", ".join(tw_vars) if tw_vars else "(ninguna con TWITTER ni TW_)"

    creds = (
        f"Vars detectadas: {all_vars_str}\n\n"
        f"TW\\_KEY (API\\_KEY):       `{mask(TWITTER_API_KEY)}`\n"
        f"TW\\_SECRET (API\\_SECRET): `{mask(TWITTER_API_SECRET)}`\n"
        f"TW\\_TOKEN (ACCESS):       `{mask(TWITTER_TOKEN)}`\n"
        f"TW\\_TSECRET (A.SECRET):   `{mask(TWITTER_SECRET)}`"
    )
    await update.message.reply_text(
        f"Credenciales en Railway:\n{creds}", parse_mode="Markdown"
    )

    def run_test():
        auth = OAuth1(
            TWITTER_API_KEY.strip(), TWITTER_API_SECRET.strip(),
            TWITTER_TOKEN.strip(), TWITTER_SECRET.strip(),
        )
        r = requests.get("https://api.twitter.com/2/users/me", auth=auth)
        return f"GET /users/me → {r.status_code}: {r.text[:200]}"

    result = await asyncio.to_thread(run_test)
    await update.message.reply_text(result)


async def handle_link(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global ADMIN_CHAT_ID
    text_in = update.message.text.strip()

    # Guardar chat_id del operador para reportes
    if not ADMIN_CHAT_ID:
        ADMIN_CHAT_ID = str(update.message.chat_id)

    # ── Si el bot espera hashtags nuevos ──
    if context.user_data.get("waiting_for_hashtags"):
        context.user_data["waiting_for_hashtags"] = False
        stored = context.user_data.get("published")
        if not stored:
            await update.message.reply_text("No hay nota activa.")
            return
        words = text_in.split()
        hashtags = " ".join(w if w.startswith("#") else f"#{w}" for w in words if w)
        context.user_data["custom_hashtags"] = hashtags
        tweet_preview = build_tweet(stored["data"], stored["url"], hashtags_override=hashtags)
        kb_tweet = InlineKeyboardMarkup([
            [
                InlineKeyboardButton("Twittear", callback_data="tweet"),
                InlineKeyboardButton("No twittear", callback_data="no_tweet"),
            ],
            [InlineKeyboardButton("Cambiar HT", callback_data="change_ht")],
        ])
        await update.message.reply_text(
            f"Vista previa actualizada:\n\n`{tweet_preview}`",
            parse_mode="Markdown",
            reply_markup=kb_tweet,
        )
        return

    # ── Si el bot espera un nuevo título ──
    if context.user_data.get("waiting_for_title"):
        context.user_data["waiting_for_title"] = False
        data = context.user_data.get("article")
        if not data:
            await update.message.reply_text("No hay nota activa. Manda un link primero.")
            return
        data["title"] = text_in
        data["title_edited"] = True
        context.user_data["article"] = data
        preview = build_preview(data)
        kb = build_preview_kb(context.user_data.get("tw_on", True), context.user_data.get("tg_on", True), context.user_data.get("wa_on", False), context.user_data.get("dest_on", False))
        await update.message.reply_text(
            preview, parse_mode="Markdown", reply_markup=kb
        )
        return

    # ── Flujo normal: procesar URL ──
    if not text_in.startswith(("http://", "https://")):
        await update.message.reply_text("Enviame un link valido (que empiece con http)")
        return

    msg = await update.message.reply_text("Analizando la nota...")

    try:
        data = await asyncio.to_thread(scrape, text_in)
    except requests.exceptions.HTTPError as e:
        code = e.response.status_code if e.response is not None else "?"
        logger.error(f"scrape HTTP {code}: {e}")
        stat_error()
        await msg.edit_text(f"El sitio devolvió error {code}. Puede estar bloqueando bots.")
        return
    except requests.exceptions.Timeout:
        logger.error(f"scrape timeout: {text_in}")
        stat_error()
        await msg.edit_text("Timeout: el sitio tardó demasiado en responder.")
        return
    except Exception as e:
        logger.error(f"scrape: {type(e).__name__}: {e}")
        stat_error()
        await msg.edit_text(f"No pude leer la nota: {type(e).__name__}")
        return

    # Si no se extrajo texto
    if not data.get("text") or len(data["text"]) < 200:
        stat_error()
        await msg.edit_text(
            "No pude extraer el texto de la nota. "
            "Puede ser un sitio que carga contenido con JavaScript (SPA)."
        )
        return

    context.user_data["article"] = data
    context.user_data.setdefault("tw_on", True)
    context.user_data.setdefault("tg_on", True)
    context.user_data.setdefault("wa_on", False)
    context.user_data.setdefault("dest_on", False)

    # Mostrar preview
    kb = build_preview_kb(context.user_data["tw_on"], context.user_data["tg_on"], context.user_data["wa_on"], context.user_data["dest_on"])
    await msg.edit_text(build_preview(data), parse_mode="Markdown", reply_markup=kb)

    # Si hay video o foto, preguntar si incorporarla
    media = data.get("media", {})
    if media.get("has_video"):
        vid_url = media.get("video_url", "video detectado")
        await update.message.reply_text(
            f"🎬 La nota tiene un *video* embebido.\n`{vid_url[:100]}`\n\n"
            "¿Querés incorporarlo a la publicación?",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([
                [
                    InlineKeyboardButton("Sí, incluir video", callback_data="media_include_video"),
                    InlineKeyboardButton("No", callback_data="media_skip"),
                ]
            ]),
        )


async def handle_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    # ── Media buttons ──
    if query.data == "media_include_video":
        data = context.user_data.get("article")
        if data and data.get("media", {}).get("video_url"):
            vid_url = data["media"]["video_url"]
            # Agregar video al texto como embed
            data["text"] += f"\n\n[Video relacionado: {vid_url}]"
            context.user_data["article"] = data
        await query.edit_message_text("✅ Video incluido en la nota.")
        return

    if query.data == "media_skip":
        await query.edit_message_text("OK, sin video.")
        return

    if query.data == "cancel":
        context.user_data.pop("waiting_for_title", None)
        stat_cancel()
        await query.edit_message_text("Cancelado.")
        return

    if query.data == "change_title":
        context.user_data["waiting_for_title"] = True
        await query.edit_message_text(
            "Escribi el nuevo titulo para la nota\n"
            "(solo escribilo como mensaje normal):"
        )
        return

    # ── Toggles ──
    if query.data == "toggle_tw":
        context.user_data["tw_on"] = not context.user_data.get("tw_on", True)
        kb = build_preview_kb(context.user_data["tw_on"], context.user_data.get("tg_on", True), context.user_data.get("wa_on", False), context.user_data.get("dest_on", False))
        await query.edit_message_reply_markup(reply_markup=kb)
        return

    if query.data == "toggle_tg":
        context.user_data["tg_on"] = not context.user_data.get("tg_on", True)
        kb = build_preview_kb(context.user_data.get("tw_on", True), context.user_data["tg_on"], context.user_data.get("wa_on", False), context.user_data.get("dest_on", False))
        await query.edit_message_reply_markup(reply_markup=kb)
        return

    if query.data == "toggle_wa":
        context.user_data["wa_on"] = not context.user_data.get("wa_on", False)
        kb = build_preview_kb(context.user_data.get("tw_on", True), context.user_data.get("tg_on", True), context.user_data["wa_on"], context.user_data.get("dest_on", False))
        await query.edit_message_reply_markup(reply_markup=kb)
        return

    if query.data == "toggle_dest":
        context.user_data["dest_on"] = not context.user_data.get("dest_on", False)
        kb = build_preview_kb(context.user_data.get("tw_on", True), context.user_data.get("tg_on", True), context.user_data.get("wa_on", False), context.user_data["dest_on"])
        await query.edit_message_reply_markup(reply_markup=kb)
        return

    data = context.user_data.get("article")
    if not data:
        await query.edit_message_text("Error: no hay nota pendiente.")
        return

    if query.data == "change_ht":
        stored = context.user_data.get("published")
        current_ht = context.user_data.get("custom_hashtags")
        if not current_ht and stored:
            raw_tags = extract_tags(stored["data"]["title"])[:3]
            current_ht = " ".join(f"#{t}" for t in raw_tags) + " #Pymes"
        context.user_data["waiting_for_hashtags"] = True
        await query.edit_message_text(
            f"Hashtags actuales: {current_ht}\n\n"
            "Escribí los nuevos hashtags (con o sin #, separados por espacios):"
        )
        return

    if query.data == "tweet":
        stored = context.user_data.get("published")
        if not stored:
            await query.edit_message_text("No encontre la nota publicada.")
            return
        await query.edit_message_text("Publicando en Twitter/X...")
        custom_ht = context.user_data.get("custom_hashtags")
        tweet_url = await asyncio.to_thread(post_tweet, stored["data"], stored["url"], custom_ht)
        if tweet_url:
            await query.edit_message_text(
                f"Publicado en WordPress y en Twitter/X!\n\n"
                f"WP: {stored['url']}\nTweet: {tweet_url}"
            )
        else:
            await query.edit_message_text(
                f"Publicado en WordPress pero fallo Twitter.\n\n{stored['url']}"
            )
        return

    if query.data == "no_tweet":
        stored = context.user_data.get("published")
        url = stored["url"] if stored else ""
        await query.edit_message_text(f"Publicado!\n\n{url}")
        return

    # ── Publicar ──
    destacado = context.user_data.get("dest_on", False)
    label = "destacada " if destacado else ""
    await query.edit_message_text(f"Publicando nota {label}...")

    image_id = None
    if data["image_url"]:
        kw  = focus_keyword(data["title"])
        alt = f"{kw} - {seo_title(data['title'])}"
        image_id = await asyncio.to_thread(upload_image, data["image_url"], alt)

    post_url = await asyncio.to_thread(publish_post, data, image_id, destacado)

    if post_url:
        # Estadísticas
        stat_publish(data["title"], data.get("source_url", ""))

        context.user_data["published"] = {"url": post_url, "data": data}
        context.user_data.pop("custom_hashtags", None)
        suffix = " (Destacados)" if destacado else ""

        tw_on = context.user_data.get("tw_on", True)
        tg_on = context.user_data.get("tg_on", True)
        wa_on = context.user_data.get("wa_on", False)

        results = [f"✅ Publicado en WordPress{suffix}!\n{post_url}"]

        if tg_on:
            tg_ok = await publish_to_channel(context.bot, data, post_url)
            results.append("✅ Publicado en canal @EmpresarialARG" if tg_ok
                           else "❌ Error al publicar en canal TG")

        if tw_on:
            tweet_preview = build_tweet(data, post_url)
            kb_tweet = InlineKeyboardMarkup([
                [
                    InlineKeyboardButton("Twittear", callback_data="tweet"),
                    InlineKeyboardButton("No twittear", callback_data="no_tweet"),
                ],
                [InlineKeyboardButton("Cambiar HT", callback_data="change_ht")],
            ])
            await query.edit_message_text(
                "\n".join(results) + "\n\n"
                f"— Vista previa del tweet —\n"
                f"`{tweet_preview}`",
                parse_mode="Markdown",
                reply_markup=kb_tweet,
            )
        else:
            await query.edit_message_text("\n".join(results), parse_mode="Markdown")

        if wa_on:
            s_title = data["title"] if data.get("title_edited") else seo_title(data["title"])
            wa_text = f"📰 {s_title}\n\n{data['excerpt'][:200]}\n\n🔗 {post_url}"
            await query.message.reply_text(
                f"— Copiá y pegá en WhatsApp —\n\n{wa_text}"
            )
    else:
        await query.edit_message_text("Error al publicar. Revisa los logs en Railway.")


# ── Borrar nota ───────────────────────────────────────────────────────────────

def find_post(query: str) -> dict | None:
    h = wp_auth()
    if query.strip().isdigit():
        r = requests.get(f"{WP_URL}/wp-json/wp/v2/posts/{query.strip()}", headers=h, timeout=10)
        if r.status_code == 200:
            p = r.json()
            return {"id": p["id"], "title": p["title"]["rendered"], "link": p["link"]}
        return None

    clean = query.strip().rstrip("/")
    slug = clean.split("/")[-1]
    r = requests.get(f"{WP_URL}/wp-json/wp/v2/posts?slug={slug}&per_page=1", headers=h, timeout=10)
    if r.status_code == 200 and r.json():
        p = r.json()[0]
        return {"id": p["id"], "title": p["title"]["rendered"], "link": p["link"]}
    return None


def trash_post(post_id: int) -> bool:
    h = {**wp_auth(), "Content-Type": "application/json"}
    r = requests.delete(f"{WP_URL}/wp-json/wp/v2/posts/{post_id}", headers=h, timeout=15)
    return r.status_code in (200, 201)


async def cmd_borrar(update: Update, context: ContextTypes.DEFAULT_TYPE):
    args = " ".join(context.args).strip()
    if not args:
        await update.message.reply_text(
            "Uso: /borrar <URL o ID>\n"
            "Ejemplo: /borrar https://mundoempresarial.ar/mi-nota/\nO: /borrar 123"
        )
        return

    msg = await update.message.reply_text("Buscando nota...")
    post = await asyncio.to_thread(find_post, args)
    if not post:
        await msg.edit_text("No encontre la nota. Verifica la URL o el ID.")
        return

    context.user_data["delete_post"] = post
    kb = InlineKeyboardMarkup([[
        InlineKeyboardButton("Confirmar borrado", callback_data="del_confirm"),
        InlineKeyboardButton("Cancelar", callback_data="del_cancel"),
    ]])
    await msg.edit_text(
        f"Estas por mandar a la papelera:\n\n*{post['title']}*\n\nID: {post['id']}",
        parse_mode="Markdown",
        reply_markup=kb,
    )


async def handle_delete_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    if query.data == "del_cancel":
        await query.edit_message_text("Cancelado, la nota sigue publicada.")
        return

    post = context.user_data.get("delete_post")
    if not post:
        await query.edit_message_text("Error: no hay nota pendiente de borrar.")
        return

    ok = await asyncio.to_thread(trash_post, post["id"])
    if ok:
        await query.edit_message_text(
            f"Nota enviada a la papelera.\n\n_{post['title']}_\n\n"
            f"Podes recuperarla desde el panel de WordPress si fue un error.",
            parse_mode="Markdown",
        )
    else:
        await query.edit_message_text("Error al borrar. Revisa los logs en Railway.")


# ── Reporte diario programado ────────────────────────────────────────────────

async def send_daily_report(context: ContextTypes.DEFAULT_TYPE):
    """Envía el reporte diario a las 23:00 ARG."""
    chat_id = ADMIN_CHAT_ID
    if not chat_id:
        logger.warning("No hay ADMIN_CHAT_ID para enviar reporte diario")
        return
    try:
        report = build_daily_report()
        await context.bot.send_message(
            chat_id=int(chat_id),
            text=report,
            parse_mode="Markdown",
        )
        logger.info("Reporte diario enviado")
    except Exception as e:
        logger.error(f"Error enviando reporte diario: {e}")


# ── Main ───────────────────────────────────────────────────────────────────────

def main():
    app = Application.builder().token(TELEGRAM_TOKEN).build()
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("borrar", cmd_borrar))
    app.add_handler(CommandHandler("testtwitter", cmd_testtwitter))
    app.add_handler(CommandHandler("stats", cmd_stats))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_link))
    app.add_handler(CallbackQueryHandler(handle_delete_button, pattern="^del_"))
    app.add_handler(CallbackQueryHandler(handle_button))

    # Programar reporte diario a las 23:00 Argentina (UTC-3)
    from datetime import timezone, timedelta
    tz_arg = timezone(timedelta(hours=-3))
    job_queue = app.job_queue
    job_queue.run_daily(
        send_daily_report,
        time=dtime(hour=23, minute=0, tzinfo=tz_arg),
        name="daily_report",
    )
    logger.info("Reporte diario programado para las 23:00 ARG")

    logger.info("Bot iniciado y esperando links...")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
