"""Overlay de frase sobre imagen base de MundoEmpresarial."""
import io
import os
from PIL import Image, ImageDraw, ImageFont

BASE_PATH = "/opt/mundoempresarial-bot/assets/frases_base.png"

# Marco navy nuevo (diseño Claude Design): texto BLANCO, sin placeholder que borrar.
# La comilla naranja está arriba; la frase va en el área libre debajo, alineada a la izq.
# Ajuste 2026-07-06 (Leo): frases largas ENTRAN — área más arriba y auto-fit de letra:
# 60 es el tamaño MÁXIMO; si el bloque no entra, la letra baja sola hasta FONT_MIN.
FRASE_X_PAD = 72       # padding horizontal (igual que el diseño)
FRASE_Y_TOP = 330      # debajo de la comilla naranja (antes 400 — subido)
FRASE_Y_BOT = 780      # arriba del footer de marca
FONT_SIZE   = 60       # máximo
FONT_MIN    = 38       # mínimo del auto-fit
TEXT_COLOR  = "#ffffff"

# Rutas de fuente (bold primero, para acercarse a Poppins del diseño)
_FONT_PATHS = [
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
    "/usr/share/fonts/truetype/freefont/FreeSansBold.ttf",
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

    # Frase entre comillas angulares, como el diseño
    clean = frase.strip().strip('«»"“”')
    texto = "«" + clean + "»"

    # AUTO-FIT: arranca en FONT_SIZE (máximo) y baja hasta que el bloque entre en el
    # área libre [FRASE_Y_TOP, FRASE_Y_BOT] — las frases largas nunca pisan el footer.
    max_w  = img.width - FRASE_X_PAD * 2
    area_h = FRASE_Y_BOT - FRASE_Y_TOP
    size   = FONT_SIZE
    while True:
        font    = _load_font(size)
        lines   = _wrap(draw, texto, font, max_w)
        line_h  = int(size * 1.32)
        block_h = len(lines) * line_h
        if block_h <= area_h or size <= FONT_MIN:
            break
        size -= 2

    # Centrado vertical en el área, pero nunca por encima del tope ni pisando el footer.
    area_cy = (FRASE_Y_TOP + FRASE_Y_BOT) // 2
    y0 = max(FRASE_Y_TOP, min(area_cy - block_h // 2, FRASE_Y_BOT - block_h))

    for i, line in enumerate(lines):
        draw.text((FRASE_X_PAD, y0 + i * line_h), line, fill=TEXT_COLOR, font=font)

    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()
