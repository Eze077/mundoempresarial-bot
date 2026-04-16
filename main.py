import os
import re
import logging
import asyncio
import base64
import unicodedata
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

HEADERS_BROWSER = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0 Safari/537.36"
    )
}

# Palabras vacías en español para extracción de keywords
STOP_WORDS = {
    "el", "la", "los", "las", "un", "una", "de", "del", "en", "y", "o", "a",
    "que", "por", "con", "se", "su", "es", "al", "para", "este", "esta",
    "esto", "ese", "esa", "más", "pero", "como", "son", "fue", "ser", "ha",
    "han", "hay", "no", "si", "ya", "le", "lo", "les", "me", "mi", "sus",
    "nos", "ante", "bajo", "hasta", "sobre", "tras", "entre", "sin", "sus",
    "también", "cuando", "donde", "quien", "cuyo", "aunque", "porque",
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
    """
    Detecta hasta 3 categorías relevantes comparando keywords del contenido
    con el mapa CATEGORY_KEYWORDS. El título tiene triple peso.
    Devuelve lista de IDs ordenada por relevancia.
    """
    corpus = (title + " " + title + " " + title + " " + excerpt + " " + (text[:600] or "")).lower()
    scores = {}
    for cat_id, kws in CATEGORY_KEYWORDS.items():
        score = sum(corpus.count(kw) for kw in kws)
        if score > 0:
            scores[cat_id] = score

    if not scores:
        return [98]  # fallback: Nacional

    ranked = sorted(scores, key=scores.get, reverse=True)
    return ranked[:3]


# ── Helpers SEO ────────────────────────────────────────────────────────────────

def seo_title(title: str) -> str:
    """Título de hasta 60 caracteres, corta en límite de palabra. Sin puntos suspensivos."""
    if len(title) <= 60:
        return title
    cut = title[:60]
    boundary = cut.rfind(" ")
    return cut[:boundary] if boundary > 40 else cut


def meta_description(excerpt: str, text: str, kw: str = "") -> str:
    """
    Meta descripción 120-155 chars.
    Si el keyword no aparece, lo antepone para que Rank Math lo detecte.
    """
    raw = (excerpt or text or "").strip()
    if kw and kw.lower() not in raw.lower():
        raw = f"{kw}: {raw}"
    if len(raw) <= 155:
        return raw
    cut = raw[:152]
    boundary = cut.rfind(" ")
    return (cut[:boundary] if boundary > 100 else cut) + "..."


def focus_keyword(title: str) -> str:
    """
    Devuelve UNA sola palabra clave — la más significativa del título.
    Una palabra corta aparece naturalmente en el contenido y obtiene
    mayor densidad, lo que sube el score de Rank Math.
    """
    for w in title.split():
        clean = w.strip('.,;:!?()[]"\'«»—:')
        if clean.lower() not in STOP_WORDS and len(clean) > 3:
            return clean
    return title.split()[0]


def url_slug(title: str) -> str:
    """Slug URL limpio, sin tildes, máximo 50 caracteres (recomendación Rank Math)."""
    slug = title.lower()
    slug = unicodedata.normalize("NFKD", slug)
    slug = "".join(c for c in slug if not unicodedata.combining(c))
    slug = re.sub(r"[^a-z0-9]+", "-", slug).strip("-")
    if len(slug) > 50:
        slug = slug[:50].rsplit("-", 1)[0]
    return slug


def extract_tags(title: str) -> list:
    """Genera hasta 6 etiquetas desde el título."""
    words = [
        w.strip('.,;:!?()[]"\'«»—').capitalize()
        for w in title.split()
        if w.lower().strip('.,;:!?()[]"\'«»—') not in STOP_WORDS and len(w) > 3
    ]
    return list(dict.fromkeys(words))[:6]


def pyme_summary(text: str, excerpt: str) -> str:
    """
    Genera un resumen de hasta 240 caracteres en lenguaje simple
    para empresarios pyme, monotributistas y profesionales.
    Usa el excerpt (og:description) como base por ser ya un resumen.
    """
    raw = (excerpt or text or "").strip()
    # Quedarse con la primera oración completa si cabe en 240 chars
    for sep in (".", "?", "!"):
        idx = raw.find(sep)
        if 60 < idx <= 237:
            return raw[: idx + 1]
    # Si no, cortar en límite de palabra
    if len(raw) <= 240:
        return raw
    cut = raw[:237]
    boundary = cut.rfind(" ")
    return (cut[:boundary] if boundary > 150 else cut) + "..."


def pyme_box(text: str, excerpt: str) -> str:
    """Recuadro 'RESUMEN PARA PYMES' con estilo visual inline."""
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


def format_content(data: dict, kw: str = "") -> str:
    """
    Estructura SEO del contenido:
    - Párrafo de apertura en negrita (lead)
    - Primer H2 incluye el keyword de enfoque (requerido por Rank Math)
    - H2 cada 5 párrafos para facilitar la lectura
    - Recuadro RESUMEN PARA PYMES
    - Fuente al pie con rel=noopener
    """
    paragraphs = [p.strip() for p in data["text"].split("\n") if p.strip()]

    # H2 labels: el primero incluye el keyword para satisfacer Rank Math
    first_h2 = f"{kw}: lo que necesitás saber" if kw else "Lo que necesitás saber"
    h2_labels = [first_h2, "En profundidad", "Más detalles",
                 "Contexto", "Análisis", "Datos clave"]

    if not paragraphs:
        return (
            f"<h2>{first_h2}</h2>\n"
            f'<p>{data["excerpt"]}</p>\n'
            + pyme_box(data["text"], data["excerpt"])
            + f'<p><em>Fuente: <a href="{data["source_url"]}" '
            f'target="_blank" rel="noopener noreferrer">Ver nota original</a></em></p>'
        )

    parts = []
    h2_index = 0

    for i, para in enumerate(paragraphs):
        if i == 0:
            # Lead en negrita + primer H2 inmediatamente después
            parts.append(f"<p><strong>{para}</strong></p>")
            parts.append(f"<h2>{h2_labels[h2_index]}</h2>")
            h2_index += 1
        else:
            if i % 5 == 0 and h2_index < len(h2_labels):
                parts.append(f"<h2>{h2_labels[h2_index]}</h2>")
                h2_index += 1
            parts.append(f"<p>{para}</p>")

    parts.append(pyme_box(data["text"], data["excerpt"]))
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
    """Crea etiquetas en WordPress si no existen. Devuelve lista de IDs."""
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
    """Sube imagen a la galería de WordPress con alt text SEO. Devuelve ID o None."""
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
    """
    Publica en WordPress con categorías auto-detectadas, etiquetas,
    SEO completo y Rank Math. Si destacado=True agrega cat. Destacados.
    """
    s_title  = data["title"] if data.get("title_edited") else seo_title(data["title"])
    s_kw     = focus_keyword(data["title"])
    s_desc   = meta_description(data["excerpt"], data["text"], kw=s_kw)
    s_slug   = url_slug(data["title"])
    content  = format_content(data, kw=s_kw)

    # Categorías auto-detectadas + opcional Destacados
    cat_ids = detect_categories(data["title"], data["text"], data["excerpt"])
    if destacado and CAT_DESTACADOS not in cat_ids:
        cat_ids = [CAT_DESTACADOS] + cat_ids

    # Etiquetas: del título + primeras palabras clave del texto
    tag_names = extract_tags(data["title"])
    first_para = (data["text"].split("\n")[0] if data["text"] else "")
    tag_names += [
        w.strip('.,;:!?()[]"\'«»—').capitalize()
        for w in first_para.split()
        if w.lower().strip('.,;:!?()[]"\'«»—') not in STOP_WORDS and len(w) > 4
    ]
    tag_names = list(dict.fromkeys(tag_names))[:8]  # sin duplicados, máx 8
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
    """
    Arma el texto del tweet:
    - Título (hasta 200 chars para dejar espacio al URL y hashtags)
    - URL del post en WordPress (Twitter lo acorta a ~23 chars)
    - Hasta 4 hashtags derivados del título + #Pymes fijo (o hashtags_override)
    """
    title = data["title"] if data.get("title_edited") else seo_title(data["title"])

    if hashtags_override is not None:
        hashtags = hashtags_override
    else:
        raw_tags = extract_tags(data["title"])[:3]
        hashtags = " ".join(f"#{t}" for t in raw_tags) + " #Pymes"

    tweet = f"{title}\n\n{wp_url}\n\n{hashtags}"

    # Twitter tiene límite de 280 chars; si excede, acortar el título
    if len(tweet) > 280:
        max_title = 280 - len(wp_url) - len(hashtags) - 6  # 6 = saltos de línea
        title = title[:max_title].rsplit(" ", 1)[0]
        tweet = f"{title}\n\n{wp_url}\n\n{hashtags}"

    return tweet


def post_tweet(data: dict, wp_url: str, hashtags_override: str = None) -> str | None:
    """Publica en Twitter/X via API v2 con OAuth 1.0a. Devuelve URL del tweet o None."""
    try:
        tweet_text = build_tweet(data, wp_url, hashtags_override=hashtags_override)
        auth = OAuth1(
            TWITTER_API_KEY,
            TWITTER_API_SECRET,
            TWITTER_TOKEN,
            TWITTER_SECRET,
        )
        r = requests.post(
            "https://api.twitter.com/2/tweets",
            json={"text": tweet_text},
            auth=auth,
        )
        if r.status_code == 201:
            tweet_id = r.json()["data"]["id"]
            return f"https://twitter.com/i/web/status/{tweet_id}"
        logger.error(f"Twitter {r.status_code}: {r.text[:400]}")
        return None
    except Exception as e:
        logger.error(f"Twitter error: {e}")
        return None


# ── Limpieza de texto scrapeado ────────────────────────────────────────────────

# Frases de ruido que trafilatura no siempre filtra
NOISE_FRAGMENTS = [
    "your browser doesn",
    "html5 audio",
    "html5 video",
    "compartir esta noticia",
    "compartir en",
    "dejanos tu comentario",
    "dejar un comentario",
    "leé más notas",
    "lee mas notas",
    "leer más",
    "más notas de",
    "notas relacionadas",
    "seguinos en",
    "seguinos",
    "suscribite",
    "suscríbete",
    "newsletter",
    "publicidad",
    "advertisement",
    "también te puede interesar",
    "te puede interesar",
    "artículos relacionados",
    "tags:",
    "etiquetas:",
    "compartir:",
    "volver arriba",
    "cargar más",
    "ver más",
    "todos los derechos reservados",
    "términos y condiciones",
    "política de privacidad",
    "cookies",
    "javascript",
]


def clean_text(raw: str) -> str:
    """
    Elimina del texto scrapeado:
    - Líneas con fragmentos de ruido conocidos (UI, share buttons, etc.)
    - Mensajes de error de browser/HTML5
    - Líneas muy cortas que no son oraciones
    - Líneas con encoding roto (Ã, Â, etc.)
    """
    if not raw:
        return ""

    clean = []
    for line in raw.split("\n"):
        s = line.strip()
        if not s:
            continue
        low = s.lower()

        # Saltar líneas con fragmentos de ruido
        if any(frag in low for frag in NOISE_FRAGMENTS):
            continue

        # Saltar líneas con encoding roto (caracteres típicos de doble-encoding)
        if any(c in s for c in ("Ã", "Â", "â€", "Ã©", "Ã¡", "Ã³", "Ã±")):
            continue

        # Saltar líneas muy cortas que no terminan como oración
        if len(s) < 25 and not s[-1] in ".?!:":
            continue

        clean.append(s)

    return "\n".join(clean)


# ── Scraper ────────────────────────────────────────────────────────────────────

def scrape(url: str) -> dict:
    resp = requests.get(url, headers=HEADERS_BROWSER, timeout=15)
    resp.raise_for_status()

    # Corregir encoding: muchos sitios declaran latin-1 pero sirven UTF-8
    if resp.encoding and resp.encoding.lower() in ("iso-8859-1", "latin-1", "windows-1252"):
        resp.encoding = resp.apparent_encoding or "utf-8"
    html = resp.text

    text = clean_text(trafilatura.extract(html) or "")
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
    excerpt   = meta("og:description") or (text[:200] + "..." if text else "")

    return {
        "title":      title.strip(),
        "text":       text,
        "excerpt":    excerpt,
        "image_url":  image_url,
        "source_url": url,
    }


# ── Canal de Telegram ─────────────────────────────────────────────────────────

async def publish_to_channel(bot, data: dict, wp_url: str):
    """Publica la nota en el canal de Telegram con imagen, título y link."""
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
        "/borrar <URL o ID> → manda una nota a la papelera"
    )


CAT_NAMES = {
    95: "AFIP", 88: "Agro", 1048: "Coberturas", 89: "Comercio",
    99: "Congreso", 337: "Destacados", 239: "Digitalización Pymes",
    94: "Economía", 96: "Empresas", 100: "Gobierno", 90: "Industria",
    103: "Informes", 97: "Internacional", 98: "Nacional", 91: "Opinión",
    101: "Poder Judicial", 87: "Política", 338: "Principales",
    102: "Provincias", 92: "Servicios", 93: "Sindicatos",
}

def build_preview_kb(tw_on: bool = True, tg_on: bool = True, dest_on: bool = False) -> InlineKeyboardMarkup:
    """Teclado de previsualización con toggles ON/OFF para Twitter, Canal TG y Destacado."""
    tw_label = "✅ Twitter" if tw_on else "❌ Twitter"
    tg_label = "✅ Canal TG" if tg_on else "❌ Canal TG"
    dest_label = "⭐ Destacado" if dest_on else "☆ Destacado"
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton(tw_label, callback_data="toggle_tw"),
            InlineKeyboardButton(tg_label, callback_data="toggle_tg"),
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
    """Genera el texto de preview con los datos SEO calculados."""
    s_title = data["title"] if data.get("title_edited") else seo_title(data["title"])
    s_kw    = focus_keyword(data["title"])
    s_desc  = meta_description(data["excerpt"], data["text"], kw=s_kw)
    s_slug  = url_slug(data["title"])
    words   = len(data["text"].split())
    cat_ids = detect_categories(data["title"], data["text"], data["excerpt"])
    cats_str    = " · ".join(CAT_NAMES.get(c, str(c)) for c in cat_ids)
    tag_preview = " · ".join(extract_tags(data["title"])[:5])
    return (
        f"*{s_title}*\n\n"
        f"*Keyword:* {s_kw}\n"
        f"*Slug:* /{s_slug}\n"
        f"*Categorias:* {cats_str}\n"
        f"*Etiquetas:* {tag_preview}\n\n"
        f"_{s_desc}_\n\n"
        f"Imagen: {'Si' if data['image_url'] else 'No'}  |  Palabras: ~{words}"
    )


async def cmd_testtwitter(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Diagnostica credenciales de Twitter: muestra valores cargados y prueba la API."""

    def mask(s: str) -> str:
        if not s:
            return "(VACIO)"
        s = s.strip()
        return f"{s[:4]}...{s[-4:]} (len={len(s)})"

    # Variables Twitter presentes en el entorno
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
            TWITTER_API_KEY.strip(),
            TWITTER_API_SECRET.strip(),
            TWITTER_TOKEN.strip(),
            TWITTER_SECRET.strip(),
        )
        r = requests.get("https://api.twitter.com/2/users/me", auth=auth)
        return f"GET /users/me → {r.status_code}: {r.text[:200]}"

    result = await asyncio.to_thread(run_test)
    await update.message.reply_text(result)


async def handle_link(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text_in = update.message.text.strip()

    # ── Si el bot espera hashtags nuevos ──
    if context.user_data.get("waiting_for_hashtags"):
        context.user_data["waiting_for_hashtags"] = False
        stored = context.user_data.get("published")
        if not stored:
            await update.message.reply_text("No hay nota activa.")
            return
        # Normalizar: agregar # si no tienen
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

    # ── Si el bot espera un nuevo título, procesar como título ──
    if context.user_data.get("waiting_for_title"):
        context.user_data["waiting_for_title"] = False
        data = context.user_data.get("article")
        if not data:
            await update.message.reply_text("No hay nota activa. Manda un link primero.")
            return
        data["title"] = text_in          # actualizar título
        data["title_edited"] = True       # no recortar título manual
        context.user_data["article"] = data
        preview = build_preview(data)
        kb = build_preview_kb(context.user_data.get("tw_on", True), context.user_data.get("tg_on", True), context.user_data.get("dest_on", False))
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
    except Exception as e:
        logger.error(f"scrape: {e}")
        await msg.edit_text("No pude leer la nota. El link funciona?")
        return

    context.user_data["article"] = data
    context.user_data.setdefault("tw_on", True)
    context.user_data.setdefault("tg_on", True)
    context.user_data.setdefault("dest_on", False)
    kb = build_preview_kb(context.user_data["tw_on"], context.user_data["tg_on"], context.user_data["dest_on"])
    await msg.edit_text(build_preview(data), parse_mode="Markdown", reply_markup=kb)


async def handle_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    if query.data == "cancel":
        context.user_data.pop("waiting_for_title", None)
        await query.edit_message_text("Cancelado.")
        return

    if query.data == "change_title":
        context.user_data["waiting_for_title"] = True
        await query.edit_message_text(
            "Escribi el nuevo titulo para la nota\n"
            "(solo escribilo como mensaje normal):"
        )
        return

    # ── Toggles Twitter / Canal TG ──
    if query.data == "toggle_tw":
        context.user_data["tw_on"] = not context.user_data.get("tw_on", True)
        kb = build_preview_kb(context.user_data["tw_on"], context.user_data.get("tg_on", True), context.user_data.get("dest_on", False))
        await query.edit_message_reply_markup(reply_markup=kb)
        return

    if query.data == "toggle_tg":
        context.user_data["tg_on"] = not context.user_data.get("tg_on", True)
        kb = build_preview_kb(context.user_data.get("tw_on", True), context.user_data["tg_on"], context.user_data.get("dest_on", False))
        await query.edit_message_reply_markup(reply_markup=kb)
        return

    if query.data == "toggle_dest":
        context.user_data["dest_on"] = not context.user_data.get("dest_on", False)
        kb = build_preview_kb(context.user_data.get("tw_on", True), context.user_data.get("tg_on", True), context.user_data["dest_on"])
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
        # Twittear la nota ya publicada
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
                f"WP: {stored['url']}\n"
                f"Tweet: {tweet_url}"
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
        # Guardar para el paso de Twitter
        context.user_data["published"] = {"url": post_url, "data": data}
        context.user_data.pop("custom_hashtags", None)  # limpiar HT previos
        suffix = " (Destacados)" if destacado else ""

        tw_on = context.user_data.get("tw_on", True)
        tg_on = context.user_data.get("tg_on", True)

        results = [f"✅ Publicado en WordPress{suffix}!\n{post_url}"]

        # Canal de Telegram
        if tg_on:
            tg_ok = await publish_to_channel(context.bot, data, post_url)
            results.append("✅ Publicado en canal @EmpresarialARG" if tg_ok
                           else "❌ Error al publicar en canal TG")

        # Twitter
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
    else:
        await query.edit_message_text("Error al publicar. Revisa los logs en Railway.")


# ── Borrar nota ───────────────────────────────────────────────────────────────

def find_post(query: str) -> dict | None:
    """
    Busca un post en WordPress por ID numérico o por URL/slug.
    Devuelve dict con 'id' y 'title', o None si no lo encuentra.
    """
    h = wp_auth()

    # Si es un número, buscar por ID directamente
    if query.strip().isdigit():
        r = requests.get(f"{WP_URL}/wp-json/wp/v2/posts/{query.strip()}", headers=h, timeout=10)
        if r.status_code == 200:
            p = r.json()
            return {"id": p["id"], "title": p["title"]["rendered"], "link": p["link"]}
        return None

    # Si es una URL, extraer el slug (último segmento no vacío)
    clean = query.strip().rstrip("/")
    slug = clean.split("/")[-1]

    r = requests.get(f"{WP_URL}/wp-json/wp/v2/posts?slug={slug}&per_page=1", headers=h, timeout=10)
    if r.status_code == 200 and r.json():
        p = r.json()[0]
        return {"id": p["id"], "title": p["title"]["rendered"], "link": p["link"]}
    return None


def trash_post(post_id: int) -> bool:
    """Mueve el post a la papelera de WordPress. Devuelve True si ok."""
    h = {**wp_auth(), "Content-Type": "application/json"}
    r = requests.delete(f"{WP_URL}/wp-json/wp/v2/posts/{post_id}", headers=h, timeout=15)
    return r.status_code in (200, 201)


async def cmd_borrar(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Uso: /borrar <URL o ID de la nota>"""
    args = " ".join(context.args).strip()
    if not args:
        await update.message.reply_text(
            "Uso: /borrar <URL o ID>\n"
            "Ejemplo: /borrar https://mundoempresarial.ar/mi-nota/\n"
            "O: /borrar 123"
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


# ── Main ───────────────────────────────────────────────────────────────────────

def main():
    app = Application.builder().token(TELEGRAM_TOKEN).build()
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("borrar", cmd_borrar))
    app.add_handler(CommandHandler("testtwitter", cmd_testtwitter))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_link))
    app.add_handler(CallbackQueryHandler(handle_delete_button, pattern="^del_"))
    app.add_handler(CallbackQueryHandler(handle_button))
    logger.info("Bot iniciado y esperando links...")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
