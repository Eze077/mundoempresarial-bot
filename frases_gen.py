"""Overlay de frase sobre imagen base de MundoEmpresarial."""
import io
import os
from PIL import Image, ImageDraw, ImageFont

BASE_PATH = "/opt/mundoempresarial-bot/assets/frases_base.png"
_FONT_DIR = "/usr/share/fonts/truetype/liberation/"

# Área donde va la frase (coordenadas dentro de la imagen base 1080x1080)
# Centro vertical del bloque de contenido, entre la línea roja y el footer
FRASE_AREA = {"x": 55, "y_center": 650, "max_w": 970}
FONT_SIZE   = 62   # ~doble del placeholder original (~30px)


def _font(name: str, size: int) -> ImageFont.FreeTypeFont:
    try:
        return ImageFont.truetype(_FONT_DIR + name, size)
    except Exception:
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
            f"No hay imagen base. Mandá la plantilla con /set_frases_base"
        )

    img  = Image.open(BASE_PATH).convert("RGB")
    draw = ImageDraw.Draw(img)
    font = _font("LiberationSans-Regular.ttf", FONT_SIZE)

    lines  = _wrap(draw, frase, font, FRASE_AREA["max_w"])
    line_h = int(FONT_SIZE * 1.25)
    block_h = len(lines) * line_h
    y0 = FRASE_AREA["y_center"] - block_h // 2

    for i, line in enumerate(lines):
        lw = int(draw.textlength(line, font=font))
        x  = (img.width - lw) // 2
        draw.text((x, y0 + i * line_h), line, fill="#333333", font=font)

    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()
