import os
import re
import logging
import asyncio
import base64
import unicodedata
import requests
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
WP_URL = os.environ.get("WP_URL", "https://mundoempresarial.ar").rstrip("/")
WP_USER = os.environ["WP_USER"]
WP_PASS = os.environ["WP_PASS"]

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


# ── Helpers SEO ────────────────────────────────────────────────────────────────

def seo_title(title: str) -> str:
    """Título entre 50-60 caracteres, corta en límite de palabra."""
    if len(title) <= 60:
        return title
    cut = title[:57]
    boundary = cut.rfind(" ")
    return (cut[:boundary] if boundary > 40 else cut) + "..."


def meta_description(excerpt: str, text: str) -> str:
    """Meta descripción entre 120-155 caracteres."""
    raw = (excerpt or text or "").strip()
    if len(raw) <= 155:
        return raw
    cut = raw[:152]
    boundary = cut.rfind(" ")
    return (cut[:boundary] if boundary > 100 else cut) + "..."


def focus_keyword(title: str) -> str:
    """Extrae 2-3 palabras clave principales del título."""
    words = [
        w.strip('.,;:!?()[]"\'«»—')
        for w in title.split()
        if w.lower().strip('.,;:!?()[]"\'«»—') not in STOP_WORDS and len(w) > 3
    ]
    return " ".join(words[:3]) if words else title.split()[0]


def url_slug(title: str) -> str:
    """Slug URL limpio, sin tildes, máximo 60 caracteres."""
    slug = title.lower()
    slug = unicodedata.normalize("NFKD", slug)
    slug = "".join(c for c in slug if not unicodedata.combining(c))
    slug = re.sub(r"[^a-z0-9]+", "-", slug).strip("-")
    if len(slug) > 60:
        slug = slug[:60].rsplit("-", 1)[0]
    return slug


def extract_tags(title: str) -> list:
    """Genera hasta 6 etiquetas desde el título."""
    words = [
        w.strip('.,;:!?()[]"\'«»—').capitalize()
        for w in title.split()
        if w.lower().strip('.,;:!?()[]"\'«»—') not in STOP_WORDS and len(w) > 3
    ]
    return list(dict.fromkeys(words))[:6]


def format_content(data: dict) -> str:
    """
    Estructura SEO del contenido:
    - Párrafo de apertura en negrita (lead)
    - Cuerpo en párrafos limpios
    - H2 cada 5 párrafos para facilitar la lectura
    - Fuente al pie con rel=noopener
    """
    paragraphs = [p.strip() for p in data["text"].split("\n") if p.strip()]

    if not paragraphs:
        return (
            f'<p>{data["excerpt"]}</p>\n'
            f'<p><em>Fuente: <a href="{data["source_url"]}" '
            f'target="_blank" rel="noopener noreferrer">Ver nota original</a></em></p>'
        )

    parts = []
    h2_labels = ["Más detalles", "En profundidad", "Lo que hay que saber",
                 "Contexto", "Análisis", "Datos clave"]
    h2_index = 0

    for i, para in enumerate(paragraphs):
        if i == 0:
            # Lead paragraph en negrita — incluye keyword natural
            parts.append(f"<p><strong>{para}</strong></p>")
        else:
            # H2 cada 5 párrafos (mejora estructura y tiempo en página)
            if i % 5 == 0 and h2_index < len(h2_labels):
                parts.append(f"<h2>{h2_labels[h2_index]}</h2>")
                h2_index += 1
            parts.append(f"<p>{para}</p>")

    parts.append(
        f'\n<p><em>Fuente: <a href="{data["source_url"]}" '
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


def publish_post(data: dict, image_id: int | None) -> str | None:
    """
    Publica en WordPress con:
    - Título SEO (50-60 chars)
    - Slug limpio
    - Excerpt = meta descripción (120-155 chars)
    - Etiquetas automáticas
    - Rank Math: title, description, focus_keyword, robots
    - Estructura de contenido con H2 y lead en negrita
    """
    s_title = seo_title(data["title"])
    s_desc  = meta_description(data["excerpt"], data["text"])
    s_kw    = focus_keyword(data["title"])
    s_slug  = url_slug(data["title"])
    content = format_content(data)
    tag_ids = get_or_create_tags(extract_tags(data["title"]))

    payload = {
        "title":   s_title,
        "content": content,
        "excerpt": s_desc,
        "status":  "publish",
        "slug":    s_slug,
        "tags":    tag_ids,
        "meta": {
            # Rank Math SEO
            "rank_math_title":         s_title,
            "rank_math_description":   s_desc,
            "rank_math_focus_keyword": s_kw,
            "rank_math_robots":        ["index", "follow"],
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


# ── Scraper ────────────────────────────────────────────────────────────────────

def scrape(url: str) -> dict:
    resp = requests.get(url, headers=HEADERS_BROWSER, timeout=15)
    resp.raise_for_status()
    html = resp.text

    text = trafilatura.extract(html) or ""
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


# ── Handlers Telegram ──────────────────────────────────────────────────────────

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Hola! Mandame el link de una nota y la publico en mundoempresarial.ar"
    )


async def handle_link(update: Update, context: ContextTypes.DEFAULT_TYPE):
    url = update.message.text.strip()
    if not url.startswith(("http://", "https://")):
        await update.message.reply_text("Enviame un link valido (que empiece con http)")
        return

    msg = await update.message.reply_text("Analizando la nota...")

    try:
        data = await asyncio.to_thread(scrape, url)
    except Exception as e:
        logger.error(f"scrape: {e}")
        await msg.edit_text("No pude leer la nota. El link funciona?")
        return

    context.user_data["article"] = data

    # Preview con datos SEO generados
    s_title = seo_title(data["title"])
    s_kw    = focus_keyword(data["title"])
    s_desc  = meta_description(data["excerpt"], data["text"])
    s_slug  = url_slug(data["title"])
    words   = len(data["text"].split())

    preview = (
        f"*Titulo SEO:* {s_title}\n"
        f"*Keyword:* {s_kw}\n"
        f"*Slug:* {s_slug}\n\n"
        f"*Meta descripcion:*\n{s_desc}\n\n"
        f"Imagen: {'Si' if data['image_url'] else 'No'}  |  "
        f"Palabras: ~{words}"
    )

    kb = InlineKeyboardMarkup([[
        InlineKeyboardButton("Publicar", callback_data="pub"),
        InlineKeyboardButton("Cancelar", callback_data="cancel"),
    ]])
    await msg.edit_text(preview, parse_mode="Markdown", reply_markup=kb)


async def handle_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    if query.data == "cancel":
        await query.edit_message_text("Cancelado.")
        return

    data = context.user_data.get("article")
    if not data:
        await query.edit_message_text("Error: no hay nota pendiente.")
        return

    await query.edit_message_text("Publicando...")

    image_id = None
    if data["image_url"]:
        alt = seo_title(data["title"])
        image_id = await asyncio.to_thread(upload_image, data["image_url"], alt)

    post_url = await asyncio.to_thread(publish_post, data, image_id)

    if post_url:
        await query.edit_message_text(f"Publicado!\n\n{post_url}")
    else:
        await query.edit_message_text("Error al publicar. Revisa los logs en Railway.")


# ── Main ───────────────────────────────────────────────────────────────────────

def main():
    app = Application.builder().token(TELEGRAM_TOKEN).build()
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_link))
    app.add_handler(CallbackQueryHandler(handle_button))
    logger.info("Bot iniciado y esperando links...")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
