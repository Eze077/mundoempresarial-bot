import os
import logging
import asyncio
import base64
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


# ── WordPress helpers ──────────────────────────────────────────────────────────

def wp_auth_headers():
    token = base64.b64encode(f"{WP_USER}:{WP_PASS}".encode()).decode()
    return {"Authorization": f"Basic {token}"}


def upload_image(image_url: str):
    """Sube una imagen a la biblioteca de medios de WordPress. Devuelve el ID o None."""
    try:
        img = requests.get(image_url, headers=HEADERS_BROWSER, timeout=15)
        img.raise_for_status()
        content_type = img.headers.get("Content-Type", "image/jpeg").split(";")[0]
        ext = content_type.split("/")[-1]

        headers = wp_auth_headers()
        headers["Content-Disposition"] = f"attachment; filename=featured.{ext}"
        headers["Content-Type"] = content_type

        r = requests.post(
            f"{WP_URL}/wp-json/wp/v2/media",
            headers=headers,
            data=img.content,
            timeout=30,
        )
        if r.status_code == 201:
            return r.json()["id"]
        logger.warning(f"Media upload error {r.status_code}: {r.text[:200]}")
    except Exception as e:
        logger.warning(f"Image upload failed: {e}")
    return None


def publish_post(data: dict, image_id):
    """Crea el post en WordPress. Devuelve la URL pública o None."""
    paragraphs = [p.strip() for p in data["text"].split("\n") if p.strip()]
    content = "\n".join(f"<p>{p}</p>" for p in paragraphs)
    content += (
        f'\n<p><em>Fuente: <a href="{data["source_url"]}" '
        f'target="_blank" rel="noopener">Ver nota original</a></em></p>'
    )

    payload = {
        "title": data["title"],
        "content": content,
        "excerpt": data["excerpt"],
        "status": "publish",
    }
    if image_id:
        payload["featured_media"] = image_id

    headers = {**wp_auth_headers(), "Content-Type": "application/json"}
    r = requests.post(
        f"{WP_URL}/wp-json/wp/v2/posts", headers=headers, json=payload, timeout=30
    )
    if r.status_code == 201:
        return r.json().get("link")
    logger.error(f"WP publish error {r.status_code}: {r.text[:400]}")
    return None


# ── Scraper ────────────────────────────────────────────────────────────────────

def scrape(url: str) -> dict:
    resp = requests.get(url, headers=HEADERS_BROWSER, timeout=15)
    resp.raise_for_status()
    html = resp.text

    # Extraer texto limpio del artículo
    text = trafilatura.extract(html) or ""

    # Parsear metadatos con BeautifulSoup
    soup = BeautifulSoup(html, "html.parser")

    def meta(prop):
        tag = soup.find("meta", property=prop) or soup.find("meta", attrs={"name": prop})
        return (tag.get("content") or "").strip() if tag else ""

    title = meta("og:title") or (soup.find("h1") or soup.find("title") or soup.new_tag("x")).get_text().strip() or "Sin título"
    image_url = meta("og:image")
    excerpt = meta("og:description") or (text[:200] + "..." if text else "")

    return {
        "title": title,
        "text": text,
        "excerpt": excerpt,
        "image_url": image_url,
        "source_url": url,
    }


# ── Handlers de Telegram ───────────────────────────────────────────────────────

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
        logger.error(f"Scrape error: {e}")
        await msg.edit_text("No pude leer la nota. El link funciona?")
        return

    context.user_data["article"] = data

    excerpt_preview = data["excerpt"][:280]
    if len(data["excerpt"]) > 280:
        excerpt_preview += "..."

    word_count = len(data["text"].split())
    image_status = "Si" if data["image_url"] else "No"

    preview = (
        f"*{data['title']}*\n\n"
        f"{excerpt_preview}\n\n"
        f"Imagen destacada: {image_status}\n"
        f"Texto: ~{word_count} palabras"
    )

    keyboard = InlineKeyboardMarkup([[
        InlineKeyboardButton("Publicar", callback_data="pub"),
        InlineKeyboardButton("Cancelar", callback_data="cancel"),
    ]])

    await msg.edit_text(preview, parse_mode="Markdown", reply_markup=keyboard)


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
        image_id = await asyncio.to_thread(upload_image, data["image_url"])

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
