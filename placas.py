"""
placas.py — Placas de NOTICIA con la identidad ME por red social.

Implementa en PIL el sistema de marca del handoff de Claude Design
(Logos/design_handoff_mundoempresarial): tokens, logo circular tipográfico,
wordmark, chip de categoría, título autoescalado y layouts por plataforma.

Formatos: ig 1080×1080 · story 1080×1920 · fb 1200×630 · li 1200×627 · x 1600×900
API: generar_placa(formato, titulo, categoria, foto_bytes=None) -> bytes PNG
Fuentes: Poppins + Source Sans 3 en assets/fonts (se bajan con ensure_fonts()).
"""
import io
import os
import logging

from PIL import Image, ImageDraw, ImageFont

log = logging.getLogger("placas")

BASE = os.path.dirname(os.path.abspath(__file__))
FONT_DIR = os.path.join(BASE, "assets", "fonts")

# ── Design tokens (README del handoff) ────────────────────────────────────────
NAVY     = "#15487F"
CELESTE  = "#0FA0DF"
NARANJA  = "#E97C1E"
ROJO     = "#BE3A2E"
BLANCO   = "#FFFFFF"
CELESTE_CLARO = "#7FC4EC"
SALMON   = "#E59A8E"
PERI     = "#9DC2E6"
HUMO     = "#F4F7FA"

_FONTS = {
    "Poppins-Regular.ttf":  "https://raw.githubusercontent.com/google/fonts/main/ofl/poppins/Poppins-Regular.ttf",
    "Poppins-Medium.ttf":   "https://raw.githubusercontent.com/google/fonts/main/ofl/poppins/Poppins-Medium.ttf",
    "Poppins-SemiBold.ttf": "https://raw.githubusercontent.com/google/fonts/main/ofl/poppins/Poppins-SemiBold.ttf",
    "Poppins-Bold.ttf":     "https://raw.githubusercontent.com/google/fonts/main/ofl/poppins/Poppins-Bold.ttf",
}


def ensure_fonts():
    """Baja las Poppins a assets/fonts si faltan (una sola vez)."""
    import requests
    os.makedirs(FONT_DIR, exist_ok=True)
    for name, url in _FONTS.items():
        p = os.path.join(FONT_DIR, name)
        if not os.path.exists(p):
            r = requests.get(url, timeout=60)
            r.raise_for_status()
            open(p, "wb").write(r.content)
            log.info(f"fuente bajada: {name}")


def _f(weight: str, size: int) -> ImageFont.FreeTypeFont:
    name = {"400": "Poppins-Regular.ttf", "500": "Poppins-Medium.ttf",
            "600": "Poppins-SemiBold.ttf", "700": "Poppins-Bold.ttf"}[weight]
    return ImageFont.truetype(os.path.join(FONT_DIR, name), size)


# ── Piezas del sistema ─────────────────────────────────────────────────────────

def logo_circular(d: int) -> Image.Image:
    """Logo circular institucional: anillo celeste (~4.6% d) sobre blanco, 3 líneas."""
    img = Image.new("RGBA", (d, d), (0, 0, 0, 0))
    dr = ImageDraw.Draw(img)
    ring = max(3, int(d * 0.046))
    dr.ellipse([0, 0, d - 1, d - 1], fill=BLANCO, outline=CELESTE, width=ring)
    f1 = _f("600", int(d * 0.20))   # Mundo
    f2 = _f("700", int(d * 0.106))  # Empresarial.AR
    f3 = _f("500", int(d * 0.068))  # La Voz de las Pymes
    cy = d // 2
    w1 = dr.textlength("Mundo", font=f1)
    dr.text(((d - w1) / 2, cy - int(d * 0.26)), "Mundo", font=f1, fill=NAVY)
    w2a = dr.textlength("Empresarial", font=f2)
    w2b = dr.textlength(".AR", font=f2)
    x2 = (d - w2a - w2b) / 2
    y2 = cy - int(d * 0.028)
    dr.text((x2, y2), "Empresarial", font=f2, fill=CELESTE)
    dr.text((x2 + w2a, y2), ".AR", font=f2, fill=NARANJA)
    w3 = dr.textlength("La Voz de las Pymes", font=f3)
    dr.text(((d - w3) / 2, cy + int(d * 0.11)), "La Voz de las Pymes", font=f3, fill=ROJO)
    return img


def _wordmark(dr, x, y, size, dark_bg=True):
    """Wordmark horizontal. Sobre navy: Mundo blanco / Empresarial celeste claro / .AR naranja."""
    f = _f("700", size)
    c1 = BLANCO if dark_bg else NAVY
    c2 = CELESTE_CLARO if dark_bg else CELESTE
    dr.text((x, y), "Mundo", font=f, fill=c1)
    x += dr.textlength("Mundo", font=f)
    dr.text((x, y), "Empresarial", font=f, fill=c2)
    x += dr.textlength("Empresarial", font=f)
    dr.text((x, y), ".AR", font=f, fill=NARANJA)
    return x + dr.textlength(".AR", font=f)


def _header(img, dr, x, y, logo_d=56, wm_size=30):
    """Mini logo circular + wordmark (header de las placas)."""
    img.paste(logo_circular(logo_d), (x, y), logo_circular(logo_d))
    _wordmark(dr, x + logo_d + 18, y + (logo_d - int(wm_size * 1.3)) // 2, wm_size)


def _chip(dr, x, y, text, size=26):
    f = _f("600", size)
    t = text.upper()
    w = dr.textlength(t, font=f)
    pad_x, pad_y = int(size * 0.85), int(size * 0.45)
    dr.rounded_rectangle([x, y, x + w + pad_x * 2, y + size + pad_y * 2],
                         radius=int(size * 0.3), fill=NARANJA)
    dr.text((x + pad_x, y + pad_y), t, font=f, fill=BLANCO)
    return y + size + pad_y * 2


def _rings(dr, w, h):
    """Anillos decorativos sutiles (blanco 5%)."""
    for cx, cy, r in ((int(w * 0.85), int(h * 0.15), int(w * 0.25)),
                      (int(w * 0.08), int(h * 0.85), int(w * 0.18))):
        dr.ellipse([cx - r, cy - r, cx + r, cy + r], outline=(255, 255, 255, 13),
                   width=max(8, int(w * 0.02)))


def _photo(img, foto_bytes, box, radius=24):
    """Pega la foto en cover-crop con esquinas redondeadas. Sin foto → panel humo."""
    x0, y0, x1, y1 = box
    bw, bh = x1 - x0, y1 - y0
    if foto_bytes:
        ph = Image.open(io.BytesIO(foto_bytes)).convert("RGB")
        s = max(bw / ph.width, bh / ph.height)
        ph = ph.resize((int(ph.width * s) + 1, int(ph.height * s) + 1))
        left, top = (ph.width - bw) // 2, (ph.height - bh) // 2
        ph = ph.crop((left, top, left + bw, top + bh))
    else:
        ph = Image.new("RGB", (bw, bh), HUMO)
    mask = Image.new("L", (bw, bh), 0)
    ImageDraw.Draw(mask).rounded_rectangle([0, 0, bw, bh], radius=radius, fill=255)
    img.paste(ph, (x0, y0), mask)


def _title_size(text, base):
    """Autoescalado del handoff: len>80→.70 · >55→.80 · >35→.91 · <22→1.20 · resto 1.0 (×base)."""
    n = len(text)
    k = 0.70 if n > 80 else 0.80 if n > 55 else 0.91 if n > 35 else 1.20 if n < 22 else 1.0
    return int(base * k)


def _wrap(dr, text, font, max_w):
    words, lines, cur = text.split(), [], ""
    for w in words:
        t = (cur + " " + w).strip()
        if dr.textlength(t, font=font) <= max_w:
            cur = t
        else:
            if cur:
                lines.append(cur)
            cur = w
    if cur:
        lines.append(cur)
    return lines


def _draw_title(dr, text, x, y, max_w, base_size, max_lines=4, color=BLANCO):
    size = _title_size(text, base_size)
    while size > 24:
        f = _f("700", size)
        lines = _wrap(dr, text, f, max_w)
        if len(lines) <= max_lines:
            break
        size -= 4
    lh = int(size * 1.22)
    for i, line in enumerate(lines[:max_lines]):
        dr.text((x, y + i * lh), line, font=f, fill=color)
    return y + min(len(lines), max_lines) * lh


# Dirección de cada red (pie de placa) — pedido de Leo 6/7
HANDLES = {
    "x":  "@EmpresarialAR",
    "fb": "fb.com/MundoEmpresarial.AR",
    "li": "linkedin.com/company/mundoempresarial-ar",
    "ig": "@empresarialar",
    "tg": "t.me/MundoEmpresarial_AR",
    "wa": "www.mundoempresarial.ar",
    "story": "www.mundoempresarial.ar",
}


# ── Layouts por formato ────────────────────────────────────────────────────────

def _placa_cuadrada(w, h, titulo, categoria, foto):
    """IG feed / Telegram (1080×1080): header, foto, chip, título, footer."""
    img = Image.new("RGB", (w, h), NAVY)
    dr = ImageDraw.Draw(img, "RGBA")
    _rings(dr, w, h)
    _header(img, dr, 60, 48, logo_d=64, wm_size=34)
    _chip(dr, 60, 158, categoria, size=26)
    _photo(img, foto, (60, 238, w - 60, int(h * 0.60)))
    y = _draw_title(dr, titulo, 60, int(h * 0.63), w - 120, 62, max_lines=4)
    f = _f("500", 28)
    dr.text((60, h - 78), "La Voz de las Pymes", font=f, fill=SALMON)
    t = "@empresarialar"
    dr.text((w - 60 - dr.textlength(t, font=f), h - 78), t, font=f, fill=PERI)
    return img


def _placa_apaisada(w, h, titulo, categoria, foto, handle=""):
    """FB 1200×630 / LinkedIn 1200×627 / X 1600×900: columna navy izq + foto der."""
    img = Image.new("RGB", (w, h), NAVY)
    dr = ImageDraw.Draw(img, "RGBA")
    _rings(dr, int(w * 0.55), h)
    col = int(w * 0.52)
    pad = int(w * 0.045)
    _header(img, dr, pad, int(h * 0.07), logo_d=int(h * 0.10), wm_size=int(h * 0.048))
    _chip(dr, pad, int(h * 0.24), categoria, size=int(h * 0.038))
    _draw_title(dr, titulo, pad, int(h * 0.36), col - pad * 2, int(h * 0.088), max_lines=5)
    f = _f("500", int(h * 0.036))
    y_foot = h - int(h * 0.10)
    dr.text((pad, y_foot), "La Voz de las Pymes", font=f, fill=SALMON)
    if handle:
        dr.text((pad, y_foot + int(h * 0.048)), handle, font=f, fill=PERI)
    # divisor naranja + foto full-bleed derecha
    dr.rectangle([col, 0, col + max(5, int(w * 0.005)), h], fill=NARANJA)
    _photo(img, foto, (col + max(5, int(w * 0.005)), 0, w, h), radius=0)
    return img


def _placa_ig34(w, h, titulo, categoria, foto, handle="@empresarialar"):
    """Instagram NATIVA 3:4 (1080×1440): header y pie compactos → foto y título más grandes
    (feedback de Leo 6/7). Sin padding: el formato es 3:4 de origen (la grilla no recorta)."""
    img = Image.new("RGB", (w, h), NAVY)
    dr = ImageDraw.Draw(img, "RGBA")
    _rings(dr, w, h)
    # header compacto en UNA línea: logo chico + wordmark + chip a la derecha
    _header(img, dr, 50, 36, logo_d=56, wm_size=30)
    f_ch = _f("600", 24)
    ch_t = categoria.upper()
    ch_w = dr.textlength(ch_t, font=f_ch) + 40
    dr.rounded_rectangle([w - 50 - ch_w, 42, w - 50, 42 + 46], radius=8, fill=NARANJA)
    dr.text((w - 50 - ch_w + 20, 52), ch_t, font=f_ch, fill=BLANCO)
    # foto GRANDE
    _photo(img, foto, (50, 128, w - 50, 900))
    # título GRANDE
    _draw_title(dr, titulo, 50, 940, w - 100, 78, max_lines=5)
    # pie compacto en una línea
    f = _f("500", 26)
    dr.text((50, h - 62), "La Voz de las Pymes", font=f, fill=SALMON)
    dr.text((w - 50 - dr.textlength(handle, font=f), h - 62), handle, font=f, fill=PERI)
    return img


def _placa_story(w, h, titulo, categoria, foto):
    """Story / estado WhatsApp-Telegram (1080×1920)."""
    img = Image.new("RGB", (w, h), NAVY)
    dr = ImageDraw.Draw(img, "RGBA")
    _rings(dr, w, h)
    _header(img, dr, 60, 90, logo_d=84, wm_size=40)
    _chip(dr, 60, 250, categoria, size=30)
    _photo(img, foto, (60, 350, w - 60, 1080))
    _draw_title(dr, titulo, 60, 1140, w - 120, 72, max_lines=5)
    # barra naranja inferior con la URL (pieza institucional del handoff)
    dr.rectangle([0, h - 110, w, h], fill=NARANJA)
    f = _f("600", 36)
    t = "🌐 www.mundoempresarial.ar"
    t = t.replace("🌐 ", "")
    tw = dr.textlength(t, font=f)
    dr.text(((w - tw) / 2, h - 82), t, font=f, fill=BLANCO)
    return img


FORMATOS = {
    "ig":    (1080, 1440, _placa_ig34),      # nativa 3:4 (grilla IG sin recorte)
    "tg":    (1080, 1080, _placa_cuadrada),
    "fb":    (1200, 630, _placa_apaisada),
    "li":    (1200, 627, _placa_apaisada),
    "x":     (1600, 900, _placa_apaisada),
    "story": (1080, 1920, _placa_story),
    "wa":    (1080, 1920, _placa_story),     # estado de WhatsApp (misma pieza que story)
}


def generar_placa(formato: str, titulo: str, categoria: str = "Noticias",
                  foto_bytes: bytes = None) -> bytes:
    """Genera la placa PNG del formato pedido con la identidad ME."""
    ensure_fonts()
    w, h, fn = FORMATOS[formato]
    titulo, categoria = (titulo or "").strip(), (categoria or "Noticias").strip()
    handle = HANDLES.get(formato, "")
    try:
        img = fn(w, h, titulo, categoria, foto_bytes, handle)
    except TypeError:
        img = fn(w, h, titulo, categoria, foto_bytes)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()
