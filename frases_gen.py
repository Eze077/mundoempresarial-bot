"""Overlay de frase sobre imagen base de MundoEmpresarial."""
import io
import os
from PIL import Image, ImageDraw, ImageFont

BASE_PATH = "/opt/mundoempresarial-bot/assets/frases_base.png"

# Área del placeholder en la imagen base (1080x1080)
# Rectángulo a borrar (cubre "[ Insertá aquí tu frase motivacional ]")
PLACEHOLDER_RECT = (40, 425, 1040, 520)
# Color de fondo del área de contenido
BG_COLOR = "#f0f0f0"

# Área donde va la frase (centro vertical del bloque de contenido)
FRASE_X_PAD = 60       # padding horizontal
FRASE_Y_TOP = 430      # inicio del área de contenido disponible
FRASE_Y_BOT = 970      # fin del área de contenido
FONT_SIZE   = 62       # ~doble del placeholder original

# Rutas de fuente en orden de preferencia
_FONT_PATHS = [
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf",
    "/usr/share/fonts/truetype/ubuntu/Ubuntu-R.ttf",
    "/usr/share/fonts/truetype/freefont/FreeSans.ttf",
]


def _load_font(size: int) -> ImageFont.FreeTypeFont:
    for path in _FONT_PATHS:
        if os.path.exists(path):
            try:
                return ImageFont.truetype(path, size)
            except Exception:
                continue
    return ImageFont.load_default()


def _wrap(draw, text: str, font, max_w: int) -> list[str]:
    words, lines, cur = text.split(), [], ""
    for w in words:
        test = (cur + " " + w).strip()
        if draw.textlength(test, font=font) <= max_w:
            cur = test
        else:
            if cur:
                lines.append(cur)
            cur = w
    if cur:
        lines.append(cur)
    return lines


def generate_frase_image(frase: str) -> bytes:
    if not os.path.exists(BASE_PATH):
        raise FileNotFoundError(
            "No hay imagen base. Mandá la plantilla con /set_frases_base"
        )

    img  = Image.open(BASE_PATH).convert("RGB")
    draw = ImageDraw.Draw(img)
    font = _load_font(FONT_SIZE)

    # 1. Borrar el área del placeholder
    draw.rectangle(PLACEHOLDER_RECT, fill=BG_COLOR)

    # 2. Calcular área disponible y centrar el texto
    max_w  = img.width - FRASE_X_PAD * 2
    lines  = _wrap(draw, frase, font, max_w)
    line_h = int(FONT_SIZE * 1.3)
    block_h = len(lines) * line_h

    area_cy = (FRASE_Y_TOP + FRASE_Y_BOT) // 2
    y0 = area_cy - block_h // 2

    for i, line in enumerate(lines):
        lw = int(draw.textlength(line, font=font))
        x  = (img.width - lw) // 2
        draw.text((x, y0 + i * line_h), line, fill="#333333", font=font)

    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()
