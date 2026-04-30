"""Generador de imágenes de frases para MundoEmpresarial."""
import io
from PIL import Image, ImageDraw, ImageFont

W, H = 1080, 1080

C_DARK  = "#1a2538"
C_BLUE  = "#1e4d8c"
C_TEAL  = "#29b6c5"
C_RED   = "#c0392b"
C_BG    = "#f5f5f5"
C_WHITE = "#ffffff"

_FONT_DIR = "/usr/share/fonts/truetype/liberation/"

def _font(name: str, size: int) -> ImageFont.FreeTypeFont:
    try:
        return ImageFont.truetype(_FONT_DIR + name, size)
    except Exception:
        return ImageFont.load_default()

def _wrap_text(draw, text: str, font, max_width: int) -> list[str]:
    words = text.split()
    lines, current = [], ""
    for word in words:
        test = (current + " " + word).strip()
        if draw.textlength(test, font=font) <= max_width:
            current = test
        else:
            if current:
                lines.append(current)
            current = word
    if current:
        lines.append(current)
    return lines


def generate_frase_image(frase: str) -> bytes:
    img  = Image.new("RGB", (W, H), C_BG)
    draw = ImageDraw.Draw(img)

    # ── Header ──────────────────────────────────────────────────────────
    draw.rectangle([(0, 0), (W, 65)], fill=C_DARK)
    f_hdr = _font("LiberationSans-Bold.ttf", 22)
    draw.text((25, 20), "MUNDO EMPRESARIAL", fill=C_WHITE, font=f_hdr)
    icon_colors = ["#555555", C_RED, "#555555", "#555555"]
    for i, col in enumerate(icon_colors):
        x = W - 185 + i * 44
        draw.rectangle([(x, 16), (x + 34, 50)], fill=col)

    # ── Logo ─────────────────────────────────────────────────────────────
    draw.rectangle([(0, 65), (W, 250)], fill=C_WHITE)
    f_big  = _font("LiberationSans-Bold.ttf", 72)
    f_sub  = _font("LiberationSans-Regular.ttf", 28)
    draw.text((55, 80), "Mundo", fill=C_BLUE, font=f_big)
    mundo_w = int(draw.textlength("Mundo", font=f_big))
    draw.text((55 + mundo_w, 80), "Empresarial", fill=C_TEAL, font=f_big)
    draw.text((58, 178), "La voz de las pymes", fill=C_TEAL, font=f_sub)

    # ── Nav ──────────────────────────────────────────────────────────────
    draw.rectangle([(0, 250), (W, 315)], fill=C_DARK)
    f_nav = _font("LiberationSans-Bold.ttf", 17)
    nav_items = ["POLÍTICA", "ECONOMÍA", "NACIONAL", "INFORMES", "MUNDO DEL VINO"]
    x = 45
    for item in nav_items:
        draw.text((x, 279), item, fill=C_WHITE, font=f_nav)
        x += int(draw.textlength(item, font=f_nav)) + 38

    # ── Contenido ────────────────────────────────────────────────────────
    draw.rectangle([(0, 315), (W, 980)], fill=C_BG)

    # Sección "FRASE DESTACADA"
    draw.rectangle([(45, 358), (52, 408)], fill=C_RED)
    f_sec = _font("LiberationSans-Bold.ttf", 24)
    draw.text((65, 362), "FRASE DESTACADA", fill="#333333", font=f_sec)
    draw.line([(65, 406), (315, 406)], fill=C_RED, width=2)

    # Texto de la frase — centrado vertical y horizontal
    f_frase = _font("LiberationSans-Regular.ttf", 52)
    lines   = _wrap_text(draw, frase, f_frase, W - 120)
    line_h  = 68
    block_h = len(lines) * line_h
    area_cy = 315 + (980 - 315) // 2
    y0      = area_cy - block_h // 2

    for i, line in enumerate(lines):
        lw = draw.textlength(line, font=f_frase)
        draw.text(((W - lw) // 2, y0 + i * line_h), line, fill="#444444", font=f_frase)

    # ── Footer ───────────────────────────────────────────────────────────
    draw.rectangle([(0, 980), (W, H)], fill=C_WHITE)
    draw.line([(0, 980), (W, 980)], fill="#dddddd", width=1)
    f_foot = _font("LiberationSans-Regular.ttf", 22)
    draw.text((45, 1018), "La voz de las pymes", fill=C_BLUE, font=f_foot)
    site = "mundoempresarial.ar"
    draw.text((W - int(draw.textlength(site, font=f_foot)) - 45, 1018), site, fill="#666666", font=f_foot)

    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()
