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


# Cajas del header de la plantilla (para reemplazar los textos quemados si piden otros)
_KICKER_BOX = (90, 54, 380, 94)     # "FRASE DESTACADA" (a la derecha del cuadradito naranja)
_TAG_BOX    = (760, 54, 1016, 94)   # "INSPIRACIÓN" (alineado a la derecha)


def _replace_header(img, draw, kicker: str, tag: str):
    """Cubre los textos del header de la plantilla y dibuja los nuevos (mismo estilo).
    AUTO-FIT: si kicker + etiqueta no entran sin tocarse, baja la letra del header."""
    bg = img.getpixel((500, 74))    # navy del fondo en la franja del header
    # celeste del texto original de la derecha, ANTES de cubrir (pixel más claro de la caja)
    x0, y0, x1, y1 = _TAG_BOX
    celeste = bg
    for xx in range(x0, x1, 6):
        for yy in range(y0, y1, 6):
            p = img.getpixel((xx, yy))
            if sum(p) > sum(celeste):
                celeste = p

    kick_txt = (kicker or "FRASE DESTACADA").upper()
    tag_txt  = (tag or "INSPIRACIÓN").upper()
    kx, right, gap = _KICKER_BOX[0] + 4, _TAG_BOX[2] - 2, 28
    size = 30
    while size > 18:
        f = _load_font(size)
        if kx + draw.textlength(kick_txt, font=f) + gap + draw.textlength(tag_txt, font=f) <= right:
            break
        size -= 2
    f = _load_font(size)

    # Cubrir AMBOS textos originales (franja completa del header) y redibujar
    draw.rectangle((_KICKER_BOX[0], _KICKER_BOX[1], _TAG_BOX[2], _TAG_BOX[3]), fill=bg)
    y_txt = _KICKER_BOX[1] + 6 + (30 - size) // 2
    draw.text((kx, y_txt), kick_txt, fill="#ffffff", font=f)
    w = draw.textlength(tag_txt, font=f)
    draw.text((right - w, y_txt), tag_txt, fill=celeste, font=f)


def pad_placa(png_bytes: bytes, target_w: int, target_h: int) -> bytes:
    """Adapta la placa a otro formato SIN tocar el diseño: extiende el fondo con el
    color del borde (navy) y centra la placa en el canvas destino. Usos:
    IG feed 3:4 (1080, 1440) · IG story / estado de WhatsApp 9:16 (1080, 1920)."""
    img = Image.open(io.BytesIO(png_bytes)).convert("RGB")
    w, h = img.size
    scale = min(target_w / w, target_h / h)
    if scale < 1:
        img = img.resize((max(1, int(w * scale)), max(1, int(h * scale))))
        w, h = img.size
    bg = img.getpixel((3, h // 2))
    canvas = Image.new("RGB", (target_w, target_h), bg)
    canvas.paste(img, ((target_w - w) // 2, (target_h - h) // 2))
    buf = io.BytesIO()
    canvas.save(buf, format="PNG")
    return buf.getvalue()


def generate_frase_image(frase: str, kicker: str = None, tag: str = None) -> bytes:
    """kicker/tag: reemplazan los textos del header de la plantilla ("FRASE DESTACADA" /
    "INSPIRACIÓN") — p.ej. para eventos: kicker="Día de la Independencia", tag="9 de julio"."""
    if not os.path.exists(BASE_PATH):
        raise FileNotFoundError(
            "No hay imagen base. Mandá la plantilla con /set_frases_base"
        )

    img  = Image.open(BASE_PATH).convert("RGB")
    draw = ImageDraw.Draw(img)
    if kicker or tag:
        _replace_header(img, draw, kicker, tag)

    # Separar la ATRIBUCIÓN (autor) de la cita: si el texto trae una comilla de cierre
    # con algo corto después ('..." Leo Bilanski'), el autor va FUERA de las «» como
    # "— Autor" (antes quedaba doble comilla y el autor adentro — feo).
    raw = frase.strip()
    autor = ""
    for qch in ('"', '”', '»'):
        pos = raw.rfind(qch)
        if 0 < pos < len(raw) - 1:
            resto = raw[pos + 1:].strip(" -—–")
            if 0 < len(resto) <= 50 and not resto.endswith((".", "!", "?")):
                autor = resto
                raw = raw[:pos]
                break
    clean = raw.strip().strip('«»"“”').strip()
    texto = "«" + clean + "»"

    # AUTO-FIT: arranca en FONT_SIZE (máximo) y baja hasta que el bloque (cita + autor)
    # entre en el área libre [FRASE_Y_TOP, FRASE_Y_BOT] — nunca pisa el footer.
    max_w  = img.width - FRASE_X_PAD * 2
    area_h = FRASE_Y_BOT - FRASE_Y_TOP
    size   = FONT_SIZE
    while True:
        font    = _load_font(size)
        lines   = _wrap(draw, texto, font, max_w)
        line_h  = int(size * 1.32)
        autor_h = int(size * 0.75 * 1.6) if autor else 0   # línea del autor (más chica + aire)
        block_h = len(lines) * line_h + autor_h
        if block_h <= area_h or size <= FONT_MIN:
            break
        size -= 2

    # Centrado vertical en el área, pero nunca por encima del tope ni pisando el footer.
    area_cy = (FRASE_Y_TOP + FRASE_Y_BOT) // 2
    y0 = max(FRASE_Y_TOP, min(area_cy - block_h // 2, FRASE_Y_BOT - block_h))

    for i, line in enumerate(lines):
        draw.text((FRASE_X_PAD, y0 + i * line_h), line, fill=TEXT_COLOR, font=font)

    if autor:
        f_autor = _load_font(int(size * 0.75))
        y_autor = y0 + len(lines) * line_h + int(size * 0.75 * 0.6)
        draw.text((FRASE_X_PAD, y_autor), "— " + autor, fill=TEXT_COLOR, font=f_autor)

    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()
