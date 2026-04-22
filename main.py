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

TELEGRAM_CHANNEL   = os.environ.get("TELEGRAM_CHANNEL", "@MundoEmpresarial_AR")
# Chat ID del operador para reportes diarios (se detecta del primer mensaje)
ADMIN_CHAT_ID      = os.environ.get("ADMIN_CHAT_ID", "")

# OpenAI API key (opcional, solo para fallback de transcripción Whisper)
OPENAI_API_KEY     = os.environ.get("OPENAI_API_KEY", "")

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
    """Acorta el titulo a <=60 chars sin truncarlo a lo bruto.
    Saca parentesis/citas, corta en : o coma, busca mantener el keyword y sentido.
    """
    t = " ".join(title.strip().split())
    if len(t) <= 60:
        return t

    # 1. Sacar parentesis (aclaraciones)
    t = re.sub(r"\s*\([^)]*\)\s*", " ", t).strip()
    t = " ".join(t.split())
    if len(t) <= 60:
        return t

    # 2. Si tiene ":", preferir la parte mas corta que contenga contenido util
    if ":" in t:
        before, after = t.split(":", 1)
        before = before.strip()
        after  = after.strip().strip('"\'«»')
        # Preferir la parte principal (mas corta pero >=25 chars)
        for part in (before, after):
            if 25 <= len(part) <= 60:
                return part

    # 3. Sacar citas entrecomilladas si las hay
    t2 = re.sub(r'["«»][^"«»]*["«»]', '', t).strip()
    t2 = " ".join(t2.split()).strip(" ,.:;—-")
    if 25 <= len(t2) <= 60:
        return t2
    if len(t2) <= 60 and t2:
        t = t2

    # 4. Si sigue largo y tiene coma (no decimal), quedarse con la primera clausula
    if len(t) > 60 and re.search(r",(?!\d)", t):
        first = re.split(r",(?!\d)", t)[0].strip()
        if 25 <= len(first) <= 60:
            return first

    # 5. Fallback: cortar en limite de palabra y limpiar conectores colgantes
    if len(t) <= 60:
        return t
    cut = t[:60]
    boundary = cut.rfind(" ")
    out = cut[:boundary] if boundary > 40 else cut
    DANGLERS = {"de", "del", "la", "el", "los", "las", "en", "con", "por", "para",
                "a", "al", "y", "o", "u", "e", "que", "un", "una", "su", "sus",
                "lo", "se", "entre", "sobre", "sin", "tras", "mas", "más"}
    words = out.split()
    while words and words[-1].lower().strip(".,;:") in DANGLERS:
        words.pop()
    return " ".join(words).rstrip(" ,.:;—-") or out


def get_title(data: dict) -> str:
    """Devuelve el titulo a mostrar/publicar segun los flags del preview.

    Prioridad: editado manual > toggle 'titulo original' > seo_title(original).
    """
    if data.get("title_edited"):
        return data["title"]
    original = data.get("original_title") or data.get("title", "")
    if data.get("orig_title_on"):
        return original
    return seo_title(original)


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


def normalize_text(text: str) -> str:
    """
    Normalización tipográfica básica antes de render (skill redactor 4.b/4.c):
    - Un solo espacio después de punto seguido, coma, etc.
    - Comillas rectas → tipográficas
    - Guiones dobles → em-dash
    - Porcentajes con espacio no-rompible entre número y %
    """
    if not text:
        return ""

    t = text

    # Colapsar espacios múltiples (preservando saltos de línea)
    t = re.sub(r'[ \t]{2,}', ' ', t)

    # Quitar espacio antes de puntuación
    t = re.sub(r'\s+([,.;:!?])', r'\1', t)

    # Asegurar UN espacio después de punto seguido (no de decimales tipo "3.4" o "3,4")
    # Solo agregamos espacio cuando la puntuación es seguida de MAYÚSCULA o letra (no dígito)
    t = re.sub(r'([.;:!?])([A-Za-zÁÉÍÓÚÑáéíóúñ])', r'\1 \2', t)
    # Coma: mismo tratamiento, pero sin romper decimales. Si la coma está entre dígitos, NO tocar
    t = re.sub(r',([A-Za-zÁÉÍÓÚÑáéíóúñ])', r', \1', t)

    # Guiones dobles -- → em-dash
    t = re.sub(r'\s--\s', ' — ', t)
    t = re.sub(r'(?<=\w)--(?=\w)', '—', t)

    # Comillas rectas → tipográficas (pareadas)
    # Simple heuristic: alternar apertura/cierre
    def _curly_quotes(s: str) -> str:
        out = []
        open_q = True
        for ch in s:
            if ch == '"':
                out.append('"' if open_q else '"')
                open_q = not open_q
            else:
                out.append(ch)
        return "".join(out)
    t = _curly_quotes(t)

    # Apóstrofes rectos → tipográficos
    t = re.sub(r"(\w)'(\w)", r"\1’\2", t)

    # Porcentaje: asegurar espacio no-rompible entre número y %
    t = re.sub(r'(\d)\s*%', r'\1 %', t)

    # Normalizar varios saltos de línea seguidos a máximo 2
    t = re.sub(r'\n{3,}', '\n\n', t)

    return t.strip()


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
    - Normalización tipográfica (espaciado, comillas, porcentajes)
    """
    raw_text = normalize_text(data["text"])

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
    hilo = data.get("hilo", 2)

    # Tag visual [OPINIÓN] / [ANÁLISIS] al inicio si es Hilo 3
    if hilo == 3:
        parts.append(
            '<p style="color:#c0392b;font-weight:700;letter-spacing:1px;'
            'font-size:12px;margin:0 0 8px;">[OPINIÓN / ANÁLISIS]</p>'
        )

    for i, para in enumerate(paragraphs):
        # Resaltar números/datos
        para_html = number_pattern.sub(r'<strong>\1</strong>', para)

        if i == 0:
            # Lead en negrita
            parts.append(f"<p><strong>{para_html}</strong></p>")

            # Si es YouTube, embebemos el video justo después del lead
            if data.get("is_youtube") and data.get("source_url"):
                parts.append(
                    f'<figure class="wp-block-embed is-type-video is-provider-youtube">'
                    f'<div class="wp-block-embed__wrapper">{data["source_url"]}</div>'
                    f'</figure>'
                )

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

    # Fuente — formato especial para YouTube
    if data.get("is_youtube"):
        channel = data.get("youtube_channel", "")
        channel_html = f"del canal *{channel}* " if channel else ""
        parts.append(
            f'<p><em>Fuente: Video {channel_html}— '
            f'<a href="{data["source_url"]}" target="_blank" rel="noopener noreferrer">'
            f'Ver en YouTube</a></em></p>'
        )
    else:
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
    s_title  = get_title(data)
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
        body = r.json()
        return {"link": body.get("link"), "id": body.get("id"), "content": content}
    logger.error(f"WP {r.status_code}: {r.text[:400]}")
    return None


def append_social_meta(post_id: int, content: str, tweet_id: str = "", tg_msg_id: int = 0) -> bool:
    """
    Agrega un HTML comment al final del post con los IDs de Twitter y Telegram
    para poder borrarlos después desde /editar.
    Format: <!-- mebot:tweet_id=X;tg_msg=Y -->
    """
    try:
        # Remover comentarios previos si existen
        clean_content = re.sub(r'<!--\s*mebot:[^>]*-->', '', content)
        meta_parts = []
        if tweet_id:
            meta_parts.append(f"tweet_id={tweet_id}")
        if tg_msg_id:
            meta_parts.append(f"tg_msg={tg_msg_id}")
        if not meta_parts:
            return True
        meta_comment = f"\n<!-- mebot:{';'.join(meta_parts)} -->"
        new_content = clean_content + meta_comment
        return update_post(post_id, {"content": new_content})
    except Exception as e:
        logger.error(f"append_social_meta: {e}")
        return False


def parse_social_meta(content: str) -> dict:
    """Extrae tweet_id y tg_msg del comentario HTML en el contenido."""
    m = re.search(r'<!--\s*mebot:([^>]+)-->', content)
    if not m:
        return {}
    result = {}
    for part in m.group(1).split(";"):
        if "=" in part:
            k, v = part.split("=", 1)
            result[k.strip()] = v.strip()
    return result


# ── UTM tracking ──────────────────────────────────────────────────────────────

UTM_CONFIG = {
    "telegram":  ("social", "canal_empresarialarg"),
    "twitter":   ("social", "organico"),
    "whatsapp":  ("social", "compartir"),
    "newsletter": ("email", "semanal"),
}


def utm_url(url: str, source: str) -> str:
    """Agrega parámetros UTM al URL para tracking en GA4."""
    medium, campaign = UTM_CONFIG.get(source, ("social", "bot"))
    params = f"utm_source={source}&utm_medium={medium}&utm_campaign={campaign}"
    sep = "&" if "?" in url else "?"
    return f"{url}{sep}{params}"


def md_escape(s: str) -> str:
    """Escapa caracteres especiales de Markdown v1 de Telegram (_ * [ `).
    Usar en valores dinamicos (URLs con UTMs, nombres con underscore, etc)
    antes de meterlos en un mensaje con parse_mode='Markdown'."""
    if not s:
        return s
    return (s.replace("\\", "\\\\")
             .replace("_", "\\_")
             .replace("*", "\\*")
             .replace("[", "\\[")
             .replace("`", "\\`"))


# ── Twitter / X ───────────────────────────────────────────────────────────────

def build_tweet(data: dict, wp_url: str, hashtags_override: str = None) -> str:
    title = get_title(data)
    if hashtags_override is not None:
        hashtags = hashtags_override
    else:
        raw_tags = extract_tags(data["title"])[:3]
        hashtags = " ".join(f"#{t}" for t in raw_tags) + " #Pymes"

    tracked_url = utm_url(wp_url, "twitter")
    tweet = f"{title}\n\n{tracked_url}\n\n{hashtags}"
    if len(tweet) > 280:
        max_title = 280 - len(tracked_url) - len(hashtags) - 6
        title = title[:max_title].rsplit(" ", 1)[0]
        tweet = f"{title}\n\n{tracked_url}\n\n{hashtags}"
    return tweet


def upload_twitter_media(image_url: str, auth: OAuth1) -> str | None:
    """
    Sube una imagen a Twitter via API v1.1 media/upload y devuelve el media_id.
    Necesario para que los tweets muestren preview de imagen.
    """
    try:
        # Descargar imagen
        img_resp = requests.get(image_url, headers=HEADERS_BROWSER, timeout=15)
        img_resp.raise_for_status()
        img_bytes = img_resp.content

        # Twitter acepta hasta 5MB por imagen. Si es más grande, intentar bajarla.
        if len(img_bytes) > 5 * 1024 * 1024:
            logger.warning(f"Imagen muy grande para Twitter ({len(img_bytes)} bytes), salteando")
            return None

        # Subir a Twitter v1.1 media/upload
        r = requests.post(
            "https://upload.twitter.com/1.1/media/upload.json",
            files={"media": img_bytes},
            auth=auth,
            timeout=30,
        )
        if r.status_code == 200:
            media_id = r.json().get("media_id_string")
            logger.info(f"Twitter media uploaded: {media_id}")
            return media_id
        logger.error(f"Twitter media upload {r.status_code}: {r.text[:300]}")
    except Exception as e:
        logger.error(f"Twitter media upload error: {e}")
    return None


def post_tweet(data: dict, wp_url: str, hashtags_override: str = None) -> str | None:
    try:
        tweet_text = build_tweet(data, wp_url, hashtags_override=hashtags_override)
        auth = OAuth1(TWITTER_API_KEY, TWITTER_API_SECRET, TWITTER_TOKEN, TWITTER_SECRET)

        payload = {"text": tweet_text}

        # Subir imagen si está disponible (para que Twitter muestre preview)
        image_url = data.get("image_url", "")
        if image_url:
            media_id = upload_twitter_media(image_url, auth)
            if media_id:
                payload["media"] = {"media_ids": [media_id]}

        r = requests.post("https://api.twitter.com/2/tweets", json=payload, auth=auth)
        if r.status_code == 201:
            tweet_id = r.json()["data"]["id"]
            return f"https://twitter.com/i/web/status/{tweet_id}"
        logger.error(f"Twitter {r.status_code}: {r.text[:400]}")
        return None
    except Exception as e:
        logger.error(f"Twitter error: {e}")
        return None


def delete_tweet(tweet_id: str) -> bool:
    """Elimina un tweet via API v2. Necesita tweet_id (no URL)."""
    try:
        auth = OAuth1(TWITTER_API_KEY, TWITTER_API_SECRET, TWITTER_TOKEN, TWITTER_SECRET)
        r = requests.delete(
            f"https://api.twitter.com/2/tweets/{tweet_id}",
            auth=auth, timeout=15,
        )
        if r.status_code == 200:
            return r.json().get("data", {}).get("deleted", False)
        logger.error(f"delete_tweet {tweet_id}: {r.status_code} {r.text[:200]}")
        return False
    except Exception as e:
        logger.error(f"delete_tweet error: {e}")
        return False


def tweet_id_from_url(url: str) -> str | None:
    """Extrae el tweet_id de una URL tipo https://twitter.com/i/web/status/12345"""
    if not url:
        return None
    m = re.search(r'/status/(\d+)', url)
    return m.group(1) if m else None


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


# ── Parseo de input + detección de YouTube ────────────────────────────────────

_URL_RE = re.compile(r'https?://[^\s<>"\']+', re.IGNORECASE)
_YOUTUBE_ID_RE = re.compile(r'(?:v=|youtu\.be/|shorts/|embed/|/v/)([A-Za-z0-9_-]{11})')
_YOUTUBE_HOST_RE = re.compile(r'(?://|\.)(?:youtube\.com|youtu\.be|m\.youtube\.com)/', re.IGNORECASE)


def extract_url_from_text(text: str) -> str | None:
    """Extrae el primer URL del mensaje. Devuelve None si no hay ninguno."""
    m = _URL_RE.search(text or "")
    return m.group(0).rstrip('.,;:!?)]}') if m else None


def detect_url_kind(url: str) -> str:
    """Devuelve 'youtube' | 'article' | 'tweet' | 'instagram' | 'unknown'."""
    if not url:
        return "unknown"
    low = url.lower()
    if _YOUTUBE_HOST_RE.search(low):
        return "youtube"
    if "twitter.com" in low or "x.com/" in low:
        if "/status/" in low:
            return "tweet"
    if "instagram.com/p/" in low or "instagram.com/reel/" in low:
        return "instagram"
    if low.startswith(("http://", "https://")):
        return "article"
    return "unknown"


def youtube_video_id(url: str) -> str | None:
    m = _YOUTUBE_ID_RE.search(url or "")
    return m.group(1) if m else None


def _parse_vtt(content: str) -> str:
    """Extrae texto plano de un VTT de YouTube, dedupeando líneas contiguas."""
    lines = []
    for line in content.split("\n"):
        line = line.strip()
        if not line or line.startswith(("WEBVTT", "NOTE", "Kind:", "Language:", "STYLE")):
            continue
        if "-->" in line:
            continue
        if re.match(r"^\d+$", line):
            continue
        line = re.sub(r"<[^>]+>", "", line)
        if line:
            lines.append(line)
    # Dedup contiguos (YouTube auto-subs repiten líneas en el scroll)
    dedup = []
    for l in lines:
        if not dedup or l != dedup[-1]:
            dedup.append(l)
    return " ".join(dedup)


def _transcript_via_whisper(video_id: str) -> str:
    """
    Fallback final: baja el audio con yt-dlp y lo transcribe con OpenAI Whisper API.
    Requiere OPENAI_API_KEY en env vars. Costo: ~$0.006/min de audio.
    """
    if not OPENAI_API_KEY:
        logger.info("Whisper: OPENAI_API_KEY no configurada, salteando")
        return ""

    try:
        import yt_dlp
    except ImportError:
        return ""

    import tempfile
    import glob

    video_url = f"https://www.youtube.com/watch?v={video_id}"

    with tempfile.TemporaryDirectory() as tmpdir:
        audio_opts = {
            "format": "bestaudio[abr<=64]/bestaudio/best",
            "outtmpl": os.path.join(tmpdir, "audio.%(ext)s"),
            "quiet": True,
            "no_warnings": True,
            "postprocessors": [{
                "key": "FFmpegExtractAudio",
                "preferredcodec": "mp3",
                "preferredquality": "64",
            }],
        }
        try:
            with yt_dlp.YoutubeDL(audio_opts) as ydl:
                ydl.download([video_url])
        except Exception as e:
            # Si ffmpeg no está disponible, intentar sin conversión
            logger.warning(f"yt-dlp audio con ffmpeg falló: {e}, probando sin conversión")
            audio_opts.pop("postprocessors", None)
            try:
                with yt_dlp.YoutubeDL(audio_opts) as ydl:
                    ydl.download([video_url])
            except Exception as e2:
                logger.error(f"yt-dlp audio falló: {e2}")
                return ""

        files = glob.glob(os.path.join(tmpdir, "audio.*"))
        if not files:
            logger.error("Whisper: no se descargó el audio")
            return ""

        audio_path = files[0]
        size_mb = os.path.getsize(audio_path) / (1024 * 1024)
        logger.info(f"Whisper: audio descargado {audio_path} ({size_mb:.1f} MB)")

        # Whisper API tiene límite de 25 MB por archivo
        if size_mb > 24.5:
            logger.error(f"Whisper: audio {size_mb:.1f} MB excede 25 MB. No soportado por ahora.")
            return ""

        try:
            with open(audio_path, "rb") as f:
                files_upload = {"file": (os.path.basename(audio_path), f, "audio/mpeg")}
                data = {
                    "model": "whisper-1",
                    "language": "es",
                    "response_format": "text",
                }
                r = requests.post(
                    "https://api.openai.com/v1/audio/transcriptions",
                    headers={"Authorization": f"Bearer {OPENAI_API_KEY}"},
                    data=data,
                    files=files_upload,
                    timeout=120,
                )
            if r.status_code == 200:
                text = r.text.strip()
                logger.info(f"Whisper OK: {len(text)} chars")
                return text
            logger.error(f"Whisper API {r.status_code}: {r.text[:200]}")
        except Exception as e:
            logger.error(f"Whisper request falló: {e}")
    return ""


def _transcript_via_ytdlp(video_id: str) -> str:
    """
    Fallback cuando youtube-transcript-api falla. yt-dlp accede a la API
    interna de YouTube (innertube) y suele conseguir subs auto-generados
    incluso cuando el endpoint público dice "TranscriptsDisabled".
    """
    try:
        import yt_dlp
    except ImportError:
        logger.warning("yt-dlp no está instalado")
        return ""

    video_url = f"https://www.youtube.com/watch?v={video_id}"
    opts = {"skip_download": True, "quiet": True, "no_warnings": True}

    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(video_url, download=False)
    except Exception as e:
        logger.error(f"yt-dlp extract_info falló: {e}")
        return ""

    # Preferencia: manual es > manual es-AR > auto es-orig > auto es > auto en
    manual = info.get("subtitles") or {}
    auto = info.get("automatic_captions") or {}

    candidates = []
    for lang in ("es", "es-AR", "es-419", "es-ES", "es-MX"):
        if lang in manual:
            candidates.append((manual[lang], lang, "manual"))
    for lang in ("es-orig", "es", "es-AR", "es-419", "es-ES", "es-MX"):
        if lang in auto:
            candidates.append((auto[lang], lang, "auto"))
    for lang in ("en", "en-US", "en-GB"):
        if lang in manual:
            candidates.append((manual[lang], lang, "manual-en"))
        if lang in auto:
            candidates.append((auto[lang], lang, "auto-en"))

    for fmts, lang, source in candidates:
        vtt_fmt = next((f for f in fmts if f.get("ext") == "vtt"), None)
        if not vtt_fmt:
            continue
        try:
            r = requests.get(vtt_fmt["url"], timeout=15)
            if r.status_code != 200:
                continue
            text = _parse_vtt(r.text)
            if text and len(text) > 200:
                logger.info(f"yt-dlp transcript OK via {source} ({lang}): {len(text)} chars")
                if source.endswith("-en"):
                    text += "\n[Nota: transcripción en inglés, revisar traducción]"
                return text
        except Exception as e:
            logger.warning(f"fetch VTT {lang}: {e}")
            continue
    return ""


def scrape_youtube(url: str) -> dict:
    """
    Extrae un video de YouTube: metadata via oEmbed + transcripción via
    youtube-transcript-api con fallback a yt-dlp. Devuelve el dict listo para la fase 2.
    """
    video_id = youtube_video_id(url)
    if not video_id:
        raise ValueError("No se pudo extraer el video_id de la URL de YouTube")

    # Metadata via oEmbed (sin API key, siempre funciona para públicos)
    video_url_canon = f"https://www.youtube.com/watch?v={video_id}"
    title = "Video de YouTube"
    author = ""
    thumbnail = f"https://img.youtube.com/vi/{video_id}/maxresdefault.jpg"
    try:
        oembed = requests.get(
            f"https://www.youtube.com/oembed?url={video_url_canon}&format=json",
            timeout=10,
        )
        if oembed.status_code == 200:
            meta = oembed.json()
            title = meta.get("title", title)
            author = meta.get("author_name", "")
            thumbnail = meta.get("thumbnail_url", thumbnail)
    except Exception as e:
        logger.warning(f"YouTube oEmbed falló: {e}")

    # Transcripción: 1) youtube-transcript-api  2) yt-dlp fallback  3) Whisper
    transcript_text = ""
    try:
        from youtube_transcript_api import YouTubeTranscriptApi
        try:
            for langs in (["es", "es-AR", "es-419", "es-MX", "es-ES"], ["en", "en-US", "en-GB"]):
                try:
                    segments = YouTubeTranscriptApi.get_transcript(video_id, languages=langs)
                    transcript_text = " ".join(seg["text"] for seg in segments)
                    if "en" in langs[0]:
                        transcript_text += "\n[Nota: transcripción en inglés, revisar traducción]"
                    break
                except Exception:
                    continue
            if not transcript_text:
                try:
                    transcript_list = YouTubeTranscriptApi.list_transcripts(video_id)
                    for t in transcript_list:
                        try:
                            segments = t.fetch()
                            transcript_text = " ".join(seg["text"] for seg in segments)
                            transcript_text += f"\n[Nota: transcripción en {t.language_code}]"
                            break
                        except Exception:
                            continue
                except Exception:
                    pass
        except Exception as e:
            logger.info(f"youtube-transcript-api no disponible ({type(e).__name__}: {e}), probando fallbacks")
    except ImportError:
        logger.warning("youtube-transcript-api no está instalado")

    # Fallback 2: yt-dlp (subs oficiales / auto-generados via innertube)
    if not transcript_text or len(transcript_text) < 200:
        logger.info("Intentando fallback con yt-dlp...")
        transcript_text = _transcript_via_ytdlp(video_id)

    # Fallback 3: Whisper (baja audio y transcribe, $0.006/min)
    if not transcript_text or len(transcript_text) < 200:
        logger.info("Intentando fallback con Whisper API...")
        transcript_text = _transcript_via_whisper(video_id)

    if not transcript_text or len(transcript_text) < 200:
        raise RuntimeError(
            "No se pudo obtener la transcripción de este video. "
            "Probá con otro o pegá el link del artículo que lo cubrió."
        )

    # Limpiar muletillas y marcadores
    transcript_clean = _clean_transcript(transcript_text)

    # Resumir a tono periodístico (sin LLM — heurística de extracción de frases clave)
    summary = _summarize_transcript(transcript_clean, author=author, title=title)

    excerpt = summary[:200] + "..." if len(summary) > 200 else summary

    return {
        "title":               title,
        "original_title":      title,
        "text":                summary,
        "excerpt":             excerpt,
        "image_url":           thumbnail,
        "source_url":          video_url_canon,
        "media": {
            "has_video":       True,
            "video_url":       video_url_canon,
            "has_photo":       True,
            "photo_url":       thumbnail,
        },
        "is_youtube":          True,
        "youtube_channel":     author,
        "youtube_video_id":    video_id,
        "youtube_transcript":  transcript_clean,
    }


def _clean_transcript(text: str) -> str:
    """Limpia muletillas y marcadores de transcripción."""
    # Sacar marcadores tipo [Música], [Aplausos], [Risas]
    text = re.sub(r'\[[^\]]{1,30}\]', '', text)
    # Sacar muletillas frecuentes
    fillers = [
        r'\b(?:eh|em|este|o sea|digamos|viste|no\??|sabés|mirá|bueno)\b',
        r'\b(?:you know|I mean|like|uh|um)\b',
    ]
    for f in fillers:
        text = re.sub(f, '', text, flags=re.IGNORECASE)
    # Colapsar espacios
    text = re.sub(r'\s+', ' ', text).strip()
    return text


def _summarize_transcript(transcript: str, author: str = "", title: str = "") -> str:
    """
    Genera un resumen periodístico de la transcripción sin usar LLM.
    Heurística: selecciona oraciones con mayor densidad de palabras significativas,
    mantiene citas textuales (palabras entre comillas o con verbos declarativos),
    arma párrafos narrativos en tercera persona.
    """
    if not transcript:
        return ""

    # Dividir en oraciones
    sentences = re.split(r'(?<=[.!?])\s+', transcript)
    sentences = [s.strip() for s in sentences if len(s.strip()) > 30]

    if not sentences:
        return transcript[:800]

    # Scoring simple: longitud moderada (40-200 chars) + presencia de keywords significativos
    signal_words = {
        "pyme", "empresa", "industria", "economía", "inflación", "dólar",
        "gobierno", "ley", "trabajo", "empleo", "producción", "exportación",
        "importación", "inversión", "mercado", "impuesto", "tasa", "crédito",
        "afip", "arca", "bcra", "fmi", "milei", "kicillof", "caputo",
        "cámara", "sindicato", "sector", "país", "argentina",
    }
    scored = []
    for i, s in enumerate(sentences):
        low = s.lower()
        length = len(s)
        if length < 40 or length > 260:
            continue
        score = sum(1 for w in signal_words if w in low)
        # Bonus por frases con cifras
        if re.search(r'\d+[.,]?\d*\s*%|\$\s*[\d.,]+', s):
            score += 2
        # Bonus por citas/declaraciones
        if re.search(r'"[^"]{10,}"|afirm|señal|sostuv|advirt|denunc|explic|indic', low):
            score += 2
        # Bonus por posición al inicio del video
        if i < len(sentences) * 0.2:
            score += 1
        scored.append((score, i, s))

    # Tomar top ~40% de las oraciones, reordenadas por posición original
    scored.sort(key=lambda x: -x[0])
    n_keep = max(12, int(len(sentences) * 0.4))
    top = sorted(scored[:n_keep], key=lambda x: x[1])

    # Reemitir como párrafos narrativos (hasta 800 palabras total)
    body_sentences = []
    word_count = 0
    for _, _, s in top:
        body_sentences.append(s)
        word_count += len(s.split())
        if word_count > 800:
            break

    body = " ".join(body_sentences)

    # Dividir en párrafos cada ~100 palabras
    words = body.split()
    paragraphs = []
    current = []
    for w in words:
        current.append(w)
        if len(current) >= 100 and w.endswith(('.', '!', '?')):
            paragraphs.append(" ".join(current))
            current = []
    if current:
        paragraphs.append(" ".join(current))

    # Intro contextual si hay autor/título
    intro_parts = []
    if author:
        intro_parts.append(f"En una entrevista publicada en el canal *{author}* de YouTube,")
    elif title:
        intro_parts.append(f"En el video *{title}*,")

    if intro_parts:
        intro = " ".join(intro_parts) + " se abordaron los siguientes puntos principales:"
        return intro + "\n\n" + "\n\n".join(paragraphs)

    return "\n\n".join(paragraphs)


# ── Detección de hilo editorial ──────────────────────────────────────────────

HILO_KEYWORDS = {
    1: [  # "Informarse es respetarse" — info útil
        "afip", "arca", "monotributo", "vencimiento", "ganancias", "iva",
        "moratoria", "blanqueo", "régimen", "decreto", "resolución general",
        "ley de", "alícuota", "categoría", "plan de pago", "factura electrónica",
        "percepción", "retención", "convenio colectivo", "paritaria",
        "jubilación", "anses", "cuit", "cbu", "plazo", "presentación",
        "declaración jurada", "tarifas",
    ],
    2: [  # "La voz de las pymes" — sectorial/empresarial
        "empresario", "empresaria", "pyme", "industria", "industrial",
        "exportación", "importación", "mercado interno", "producción",
        "cámara", "cadena de valor", "agro", "textil", "calzado",
        "metalmecánica", "automotriz", "vitivinicultura", "minería",
        "construcción", "comercio", "retail", "balanza comercial",
        "inversión productiva", "empleo industrial", "parque industrial",
        "clúster", "cooperativa",
    ],
    3: [  # Opinión/posición política
        "editorial", "opinión", "análisis", "crítica", "debate",
        "modelo económico", "ajuste", "neoliberal", "desarrollo nacional",
        "soberanía", "concentración", "monopolio", "oligopolio", "fmi",
        "reforma laboral", "reforma tributaria", "rigi",
        "libertario", "kirchnerismo", "peronismo", "milei", "caputo",
        "kicillof", "unión industrial", "aea",
        "denuncia", "cuestiona", "advierte", "repudia", "defiende",
    ],
}

_HILO_HINT_RE = re.compile(
    r'\b(?:hilo\s*([123])|h([123])|informarse|voz\s*pymes|opini[oó]n)\b',
    re.IGNORECASE,
)


def extract_hilo_hint(text: str) -> int | None:
    """Si el operador mencionó hilo explícito en el mensaje, devolver 1/2/3."""
    m = _HILO_HINT_RE.search(text or "")
    if not m:
        return None
    if m.group(1):
        return int(m.group(1))
    if m.group(2):
        return int(m.group(2))
    txt = m.group(0).lower()
    if "informarse" in txt:
        return 1
    if "voz" in txt:
        return 2
    if "opinión" in txt or "opinion" in txt:
        return 3
    return None


def detect_hilo(data: dict) -> int:
    """Detecta el hilo editorial por keyword matching."""
    corpus = (
        data.get("title", "") + " " +
        data.get("title", "") + " " +  # doble peso al título
        data.get("excerpt", "") + " " +
        (data.get("text", "")[:800] or "")
    ).lower()
    scores = {}
    for hilo, kws in HILO_KEYWORDS.items():
        scores[hilo] = sum(corpus.count(kw) for kw in kws)
    # YouTube raramente es hilo 1 salvo tutoriales
    if data.get("is_youtube") and scores[1] < scores[2] + scores[3]:
        scores[1] = max(0, scores[1] - 2)
    if max(scores.values()) == 0:
        return 2  # fallback: voz de las pymes
    return max(scores, key=scores.get)


HILO_NAMES = {1: "Informarse es respetarse", 2: "La voz de las pymes", 3: "Opinión / Análisis"}


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

    clean_title = title.strip()
    return {
        "title":          clean_title,
        "original_title": clean_title,
        "text":           text,
        "excerpt":        excerpt,
        "image_url":      image_url,
        "source_url":     url,
        "media":          media_info,
    }


# ── Canal de Telegram ─────────────────────────────────────────────────────────

async def publish_to_channel(bot, data: dict, wp_url: str):
    """Publica en el canal. Devuelve message_id (int) o None si falló."""
    s_title = get_title(data)
    tracked_url = utm_url(wp_url, "telegram")
    text = f"📰 *{s_title}*\n\n{data['excerpt'][:200]}\n\n🔗 [Leer nota completa]({tracked_url})"
    try:
        if data.get("image_url"):
            msg = await bot.send_photo(
                chat_id=TELEGRAM_CHANNEL,
                photo=data["image_url"],
                caption=text,
                parse_mode="Markdown",
            )
        else:
            msg = await bot.send_message(
                chat_id=TELEGRAM_CHANNEL,
                text=text,
                parse_mode="Markdown",
                disable_web_page_preview=False,
            )
        return msg.message_id
    except Exception as e:
        logger.error(f"Canal TG: {e}")
        return None


async def delete_from_channel(bot, message_id: int) -> bool:
    """Borra un mensaje del canal de Telegram."""
    try:
        await bot.delete_message(chat_id=TELEGRAM_CHANNEL, message_id=message_id)
        return True
    except Exception as e:
        logger.error(f"delete_from_channel {message_id}: {e}")
        return False


# ── Handlers Telegram ──────────────────────────────────────────────────────────

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Hola! Comandos disponibles:\n\n"
        "Pega un link → analiza y publica la nota\n"
        "/editar <URL o ID> → editar título, categoría o foto de una nota\n"
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

def _preview_kb_from_ctx(context) -> InlineKeyboardMarkup:
    ud = context.user_data
    return build_preview_kb(
        tw_on   = ud.get("tw_on", True),
        tg_on   = ud.get("tg_on", True),
        wa_on   = ud.get("wa_on", False),
        dest_on = ud.get("dest_on", False),
        orig_on = ud.get("orig_title_on", False),
    )


def build_preview_kb(tw_on: bool = True, tg_on: bool = True, wa_on: bool = False, dest_on: bool = False, orig_on: bool = False) -> InlineKeyboardMarkup:
    tw_label = "✅ Twitter" if tw_on else "❌ Twitter"
    tg_label = "✅ Canal TG" if tg_on else "❌ Canal TG"
    wa_label = "✅ WhatsApp" if wa_on else "❌ WhatsApp"
    dest_label = "⭐ Destacado" if dest_on else "☆ Destacado"
    orig_label = "✅ Titulo original" if orig_on else "❌ Titulo original"
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
            InlineKeyboardButton(orig_label, callback_data="toggle_orig_title"),
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
    s_title = get_title(data)
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

    # Hilo editorial
    hilo = data.get("hilo", 2)
    hilo_emoji = {1: "📋", 2: "🗣️", 3: "💭"}.get(hilo, "")
    hilo_line = f"*Hilo:* {hilo_emoji} {hilo} — {HILO_NAMES.get(hilo, '?')}\n"

    # YouTube?
    yt_line = ""
    if data.get("is_youtube"):
        yt_line = f"*YouTube:* 🎬 canal _{data.get('youtube_channel','?')}_\n"

    return (
        f"*{md_escape(s_title)}*\n\n"
        f"{yt_line}"
        f"{hilo_line}"
        f"*Keyword:* {s_kw}\n"
        f"*Slug:* /{s_slug}\n"
        f"*Categorias:* {cats_str}\n"
        f"*Etiquetas:* {tag_preview}\n\n"
        f"_{md_escape(s_desc)}_\n\n"
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
            f"Vista previa actualizada:\n\n`{md_escape(tweet_preview)}`",
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
        kb = _preview_kb_from_ctx(context)
        await update.message.reply_text(
            preview, parse_mode="Markdown", reply_markup=kb
        )
        return

    # ── Si el bot espera nuevo título para edición de nota existente ──
    if context.user_data.get("waiting_for_edit_title"):
        context.user_data["waiting_for_edit_title"] = False
        post = context.user_data.get("edit_post")
        if not post:
            await update.message.reply_text("No hay nota en edición.")
            return
        new_title = text_in
        ok = await asyncio.to_thread(
            update_post, post["id"],
            {"title": new_title, "slug": url_slug(new_title)}
        )
        if ok:
            post["title"] = new_title
            context.user_data["edit_post"] = post
            await update.message.reply_text(
                f"✅ Título actualizado.\n\n*{new_title}*\n\n{post['link']}",
                parse_mode="Markdown",
            )
        else:
            await update.message.reply_text("❌ Error al actualizar el título.")
        return

    # ── Si el bot espera URL de foto para edición ──
    if context.user_data.get("waiting_for_edit_photo"):
        # Si es URL (http/https), usarla; si no, pedir imagen
        if text_in.startswith(("http://", "https://")):
            context.user_data["waiting_for_edit_photo"] = False
            post = context.user_data.get("edit_post")
            if not post:
                await update.message.reply_text("No hay nota en edición.")
                return
            msg = await update.message.reply_text("Descargando y subiendo foto...")
            ok = await _handle_edit_photo_url(text_in, post)
            if ok:
                await msg.edit_text(f"✅ Foto actualizada.\n\n{post['link']}")
            else:
                await msg.edit_text("❌ Error al actualizar la foto.")
            return
        else:
            await update.message.reply_text(
                "Mandame la foto como imagen en Telegram, o una URL que empiece con http."
            )
            return

    # ── Flujo normal: extraer URL del mensaje (acepta texto + link) ──
    url = extract_url_from_text(text_in)
    if not url:
        await update.message.reply_text(
            "No encontré un link en el mensaje. Mandame una URL (http/https)."
        )
        return

    # Hint de hilo si el operador lo mencionó
    hilo_hint = extract_hilo_hint(text_in)
    kind = detect_url_kind(url)

    if kind == "instagram":
        await update.message.reply_text(
            "Instagram todavía no lo soporto. Pegá el link del artículo original."
        )
        return

    msg = await update.message.reply_text(
        "🎬 Bajando transcripción de YouTube..." if kind == "youtube"
        else "Analizando la nota..."
    )

    try:
        if kind == "youtube":
            data = await asyncio.to_thread(scrape_youtube, url)
        else:
            data = await asyncio.to_thread(scrape, url)
    except requests.exceptions.HTTPError as e:
        code = e.response.status_code if e.response is not None else "?"
        logger.error(f"scrape HTTP {code}: {e}")
        stat_error()
        await msg.edit_text(f"El sitio devolvió error {code}. Puede estar bloqueando bots.")
        return
    except requests.exceptions.Timeout:
        logger.error(f"scrape timeout: {url}")
        stat_error()
        await msg.edit_text("Timeout: el sitio tardó demasiado en responder.")
        return
    except RuntimeError as e:
        logger.error(f"scrape runtime: {e}")
        stat_error()
        await msg.edit_text(f"⚠️ {e}")
        return
    except Exception as e:
        logger.error(f"scrape: {type(e).__name__}: {e}")
        stat_error()
        await msg.edit_text(f"No pude leer la nota: {type(e).__name__}: {e}")
        return

    # Si no se extrajo texto
    if not data.get("text") or len(data["text"]) < 200:
        stat_error()
        await msg.edit_text(
            "No pude extraer el texto de la nota. "
            "Puede ser un sitio que carga contenido con JavaScript (SPA)."
        )
        return

    # Determinar hilo (hint del operador o auto-detect)
    hilo = hilo_hint or detect_hilo(data)
    data["hilo"] = hilo

    context.user_data["article"] = data
    context.user_data.setdefault("tw_on", True)
    context.user_data.setdefault("tg_on", True)
    context.user_data.setdefault("wa_on", False)
    context.user_data.setdefault("dest_on", False)
    context.user_data.setdefault("orig_title_on", False)
    data["orig_title_on"] = context.user_data["orig_title_on"]

    # Si es YouTube, embed del video ON por defecto
    if data.get("is_youtube"):
        context.user_data["yt_embed_on"] = True

    # Mostrar preview
    kb = _preview_kb_from_ctx(context)
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
        await query.edit_message_reply_markup(reply_markup=_preview_kb_from_ctx(context))
        return

    if query.data == "toggle_tg":
        context.user_data["tg_on"] = not context.user_data.get("tg_on", True)
        await query.edit_message_reply_markup(reply_markup=_preview_kb_from_ctx(context))
        return

    if query.data == "toggle_wa":
        context.user_data["wa_on"] = not context.user_data.get("wa_on", False)
        await query.edit_message_reply_markup(reply_markup=_preview_kb_from_ctx(context))
        return

    if query.data == "toggle_dest":
        context.user_data["dest_on"] = not context.user_data.get("dest_on", False)
        await query.edit_message_reply_markup(reply_markup=_preview_kb_from_ctx(context))
        return

    if query.data == "toggle_orig_title":
        new_val = not context.user_data.get("orig_title_on", False)
        context.user_data["orig_title_on"] = new_val
        data = context.user_data.get("article")
        if data:
            data["orig_title_on"] = new_val
            context.user_data["article"] = data
            await query.edit_message_text(
                build_preview(data),
                parse_mode="Markdown",
                reply_markup=_preview_kb_from_ctx(context),
            )
        else:
            await query.edit_message_reply_markup(reply_markup=_preview_kb_from_ctx(context))
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
            # Guardar tweet_id en el post para poder borrarlo luego via /editar
            tw_id = tweet_id_from_url(tweet_url)
            if tw_id and stored.get("id"):
                tg_msg_id = stored.get("tg_msg_id", 0)
                await asyncio.to_thread(
                    append_social_meta, stored["id"], stored["content"],
                    tw_id, tg_msg_id
                )
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
        alt = f"{kw} - {get_title(data)}"
        image_id = await asyncio.to_thread(upload_image, data["image_url"], alt)

    published = await asyncio.to_thread(publish_post, data, image_id, destacado)

    if published:
        post_url = published["link"]
        post_id = published["id"]
        post_content = published["content"]

        # Estadísticas
        stat_publish(data["title"], data.get("source_url", ""))

        context.user_data["published"] = {
            "url": post_url, "data": data, "id": post_id, "content": post_content
        }
        context.user_data.pop("custom_hashtags", None)
        suffix = " (Destacados)" if destacado else ""

        tw_on = context.user_data.get("tw_on", True)
        tg_on = context.user_data.get("tg_on", True)
        wa_on = context.user_data.get("wa_on", False)

        results = [f"✅ Publicado en WordPress{suffix}!\n{md_escape(post_url)}"]

        # Publicar en canal TG y guardar message_id
        tg_msg_id = 0
        if tg_on:
            tg_msg_id = await publish_to_channel(context.bot, data, post_url)
            if tg_msg_id:
                results.append("✅ Publicado en canal @MundoEmpresarial\\_AR")
                context.user_data["published"]["tg_msg_id"] = tg_msg_id
            else:
                results.append("❌ Error al publicar en canal TG")

        # Guardar tg_msg_id inmediatamente en el post
        if tg_msg_id:
            await asyncio.to_thread(
                append_social_meta, post_id, post_content,
                "", tg_msg_id
            )

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
                f"`{md_escape(tweet_preview)}`",
                parse_mode="Markdown",
                reply_markup=kb_tweet,
            )
        else:
            await query.edit_message_text("\n".join(results), parse_mode="Markdown")

        if wa_on:
            s_title = get_title(data)
            wa_text = f"📰 {s_title}\n\n{data['excerpt'][:200]}\n\n🔗 {utm_url(post_url, 'whatsapp')}"
            await query.message.reply_text(
                f"— Copiá y pegá en WhatsApp —\n\n{wa_text}"
            )
    else:
        await query.edit_message_text("Error al publicar. Revisa los logs en Railway.")


# ── Borrar nota ───────────────────────────────────────────────────────────────

def find_post(query: str) -> dict | None:
    """Busca un post en WP. Usa context=edit para obtener raw content (con comments)."""
    h = wp_auth()
    if query.strip().isdigit():
        r = requests.get(
            f"{WP_URL}/wp-json/wp/v2/posts/{query.strip()}?context=edit",
            headers=h, timeout=10
        )
        if r.status_code == 200:
            p = r.json()
            return {
                "id": p["id"],
                "title": p["title"].get("rendered", ""),
                "link": p["link"],
                "categories": p.get("categories", []),
                "featured_media": p.get("featured_media", 0),
                "content": p.get("content", {}).get("raw", "") or p.get("content", {}).get("rendered", ""),
            }
        return None

    clean = query.strip().rstrip("/")
    slug = clean.split("/")[-1]
    r = requests.get(
        f"{WP_URL}/wp-json/wp/v2/posts?slug={slug}&per_page=1&context=edit",
        headers=h, timeout=10
    )
    if r.status_code == 200 and r.json():
        p = r.json()[0]
        return {
            "id": p["id"],
            "title": p["title"].get("rendered", ""),
            "link": p["link"],
            "categories": p.get("categories", []),
            "featured_media": p.get("featured_media", 0),
            "content": p.get("content", {}).get("raw", "") or p.get("content", {}).get("rendered", ""),
        }
    return None


def trash_post(post_id: int) -> bool:
    h = {**wp_auth(), "Content-Type": "application/json"}
    r = requests.delete(f"{WP_URL}/wp-json/wp/v2/posts/{post_id}", headers=h, timeout=15)
    return r.status_code in (200, 201)


def update_post(post_id: int, payload: dict) -> bool:
    """Actualiza un post existente en WordPress. Devuelve True si ok."""
    h = {**wp_auth(), "Content-Type": "application/json"}
    r = requests.post(
        f"{WP_URL}/wp-json/wp/v2/posts/{post_id}",
        headers=h, json=payload, timeout=30
    )
    if r.status_code in (200, 201):
        return True
    logger.error(f"update_post {post_id}: {r.status_code} {r.text[:300]}")
    return False


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


# ── Editar nota ───────────────────────────────────────────────────────────────

def _build_edit_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("✏️ Cambiar título", callback_data="edit_title"),
            InlineKeyboardButton("📂 Cambiar categoría", callback_data="edit_cat"),
        ],
        [
            InlineKeyboardButton("🖼️ Cambiar foto", callback_data="edit_photo"),
            InlineKeyboardButton("📡 Publicar en redes", callback_data="edit_publish"),
        ],
        [
            InlineKeyboardButton("🗑️ Borrar", callback_data="edit_delete"),
            InlineKeyboardButton("Cancelar", callback_data="edit_cancel"),
        ],
    ])


def _build_publish_social_kb(tw_on: bool, tg_on: bool, wa_on: bool) -> InlineKeyboardMarkup:
    """Teclado para republicar una nota existente en redes."""
    tw_label = ("✅" if tw_on else "❌") + " Twitter"
    tg_label = ("✅" if tg_on else "❌") + " Canal TG"
    wa_label = ("✅" if wa_on else "❌") + " WhatsApp"
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(tw_label, callback_data="pubtoggle_tw")],
        [InlineKeyboardButton(tg_label, callback_data="pubtoggle_tg")],
        [InlineKeyboardButton(wa_label, callback_data="pubtoggle_wa")],
        [
            InlineKeyboardButton("📡 Publicar", callback_data="pub_execute"),
            InlineKeyboardButton("Cancelar", callback_data="edit_cancel"),
        ],
    ])


def _post_to_data(post: dict) -> dict:
    """Convierte un post de WP (devuelto por find_post) en un data dict
    compatible con publish_to_channel() y post_tweet()."""
    content = post.get("content", "")
    # Sacar comentarios HTML mebot y tags para derivar excerpt
    clean = re.sub(r'<!--[^>]*-->', '', content)
    text_only = re.sub(r'<[^>]+>', ' ', clean)
    text_only = re.sub(r'\s+', ' ', text_only).strip()
    excerpt = text_only[:200] + ("..." if len(text_only) > 200 else "")

    image_url = ""
    fm = post.get("featured_media") or 0
    if fm:
        try:
            r = requests.get(
                f"{WP_URL}/wp-json/wp/v2/media/{fm}",
                headers=wp_auth(), timeout=10,
            )
            if r.status_code == 200:
                image_url = r.json().get("source_url", "")
        except Exception as e:
            logger.warning(f"No pude obtener featured media {fm}: {e}")

    title = post.get("title", "")
    return {
        "title":          title,
        "original_title": title,
        "title_edited":   True,
        "excerpt":        excerpt,
        "text":           text_only,
        "image_url":      image_url,
        "source_url":     post.get("link", ""),
    }


def _build_delete_kb(del_tw: bool, del_wp: bool, del_tg: bool, has_tw: bool, has_tg: bool) -> InlineKeyboardMarkup:
    """Teclado con toggles on/off para elegir qué borrar."""
    tw_label = ("✅" if del_tw else "❌") + " Borrar de Twitter" + ("" if has_tw else " (N/A)")
    wp_label = ("✅" if del_wp else "❌") + " Borrar de WordPress"
    tg_label = ("✅" if del_tg else "❌") + " Borrar del canal TG" + ("" if has_tg else " (N/A)")
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(wp_label, callback_data="deltoggle_wp")],
        [InlineKeyboardButton(tw_label, callback_data="deltoggle_tw")],
        [InlineKeyboardButton(tg_label, callback_data="deltoggle_tg")],
        [
            InlineKeyboardButton("🗑️ Ejecutar borrado", callback_data="del_execute"),
            InlineKeyboardButton("Cancelar", callback_data="edit_cancel"),
        ],
    ])


def _build_category_kb() -> InlineKeyboardMarkup:
    """Genera teclado con las categorías disponibles (2 columnas)."""
    buttons = []
    row = []
    sorted_cats = sorted(CAT_NAMES.items(), key=lambda x: x[1])
    for cat_id, name in sorted_cats:
        row.append(InlineKeyboardButton(name, callback_data=f"setcat_{cat_id}"))
        if len(row) == 2:
            buttons.append(row)
            row = []
    if row:
        buttons.append(row)
    buttons.append([InlineKeyboardButton("Cancelar", callback_data="edit_cancel")])
    return InlineKeyboardMarkup(buttons)


async def cmd_editar(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Uso: /editar <URL o ID de la nota>"""
    args = " ".join(context.args).strip()
    if not args:
        await update.message.reply_text(
            "Uso: /editar <URL o ID>\n"
            "Ejemplo: /editar https://mundoempresarial.ar/mi-nota/\nO: /editar 123"
        )
        return

    msg = await update.message.reply_text("Buscando nota...")
    post = await asyncio.to_thread(find_post, args)
    if not post:
        await msg.edit_text("No encontre la nota. Verifica la URL o el ID.")
        return

    context.user_data["edit_post"] = post
    # Limpiar estados previos
    context.user_data.pop("waiting_for_edit_title", None)
    context.user_data.pop("waiting_for_edit_photo", None)

    cats_str = ", ".join(CAT_NAMES.get(c, str(c)) for c in post.get("categories", [])) or "Ninguna"
    await msg.edit_text(
        f"✏️ Editando nota:\n\n*{post['title']}*\n\n"
        f"ID: `{post['id']}`\n"
        f"Categorías: {cats_str}\n"
        f"Imagen destacada: {'Sí' if post.get('featured_media') else 'No'}\n\n"
        f"¿Qué querés cambiar?",
        parse_mode="Markdown",
        reply_markup=_build_edit_kb(),
    )


async def handle_edit_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    post = context.user_data.get("edit_post")

    if query.data == "edit_cancel":
        context.user_data.pop("edit_post", None)
        context.user_data.pop("waiting_for_edit_title", None)
        context.user_data.pop("waiting_for_edit_photo", None)
        await query.edit_message_text("Edición cancelada.")
        return

    if not post:
        await query.edit_message_text("No hay nota en edición. Usá /editar <URL o ID>")
        return

    if query.data == "edit_title":
        context.user_data["waiting_for_edit_title"] = True
        await query.edit_message_text(
            f"Título actual:\n_{post['title']}_\n\n"
            "Escribí el nuevo título (mandalo como mensaje normal):",
            parse_mode="Markdown",
        )
        return

    if query.data == "edit_cat":
        await query.edit_message_text(
            "Seleccioná la nueva categoría principal:",
            reply_markup=_build_category_kb(),
        )
        return

    if query.data == "edit_photo":
        context.user_data["waiting_for_edit_photo"] = True
        await query.edit_message_text(
            "Mandame la nueva foto (como imagen en Telegram) "
            "o pegá la URL de la imagen:",
        )
        return

    # Cambio de categoría: callback setcat_<id>
    if query.data.startswith("setcat_"):
        cat_id = int(query.data.split("_", 1)[1])
        ok = await asyncio.to_thread(
            update_post, post["id"], {"categories": [cat_id]}
        )
        if ok:
            post["categories"] = [cat_id]
            context.user_data["edit_post"] = post
            await query.edit_message_text(
                f"✅ Categoría actualizada a: *{CAT_NAMES.get(cat_id, cat_id)}*\n\n"
                f"{post['link']}",
                parse_mode="Markdown",
            )
        else:
            await query.edit_message_text("❌ Error al actualizar la categoría.")
        return

    # ── Borrar: mostrar toggles ──
    if query.data == "edit_delete":
        meta = parse_social_meta(post.get("content", ""))
        has_tw = bool(meta.get("tweet_id"))
        has_tg = bool(meta.get("tg_msg"))

        # Estado inicial: WP encendido, TW y TG encendidos si tienen ID
        context.user_data["del_wp"] = True
        context.user_data["del_tw"] = has_tw
        context.user_data["del_tg"] = has_tg
        context.user_data["del_has_tw"] = has_tw
        context.user_data["del_has_tg"] = has_tg

        tw_info = f"Tweet ID: `{meta.get('tweet_id','-')}`" if has_tw else "Tweet: no registrado"
        tg_info = f"TG msg: `{meta.get('tg_msg','-')}`" if has_tg else "TG canal: no registrado"

        await query.edit_message_text(
            f"🗑️ *Borrar nota*\n\n*{post['title']}*\n\n"
            f"{tw_info}\n{tg_info}\n\n"
            "Elegí qué borrar:",
            parse_mode="Markdown",
            reply_markup=_build_delete_kb(
                context.user_data["del_tw"],
                context.user_data["del_wp"],
                context.user_data["del_tg"],
                has_tw, has_tg,
            ),
        )
        return

    # Toggles de delete
    if query.data == "deltoggle_wp":
        context.user_data["del_wp"] = not context.user_data.get("del_wp", True)
        await query.edit_message_reply_markup(
            reply_markup=_build_delete_kb(
                context.user_data.get("del_tw", False),
                context.user_data["del_wp"],
                context.user_data.get("del_tg", False),
                context.user_data.get("del_has_tw", False),
                context.user_data.get("del_has_tg", False),
            )
        )
        return

    if query.data == "deltoggle_tw":
        if not context.user_data.get("del_has_tw"):
            return  # no toggleable si no hay tweet
        context.user_data["del_tw"] = not context.user_data.get("del_tw", False)
        await query.edit_message_reply_markup(
            reply_markup=_build_delete_kb(
                context.user_data["del_tw"],
                context.user_data.get("del_wp", True),
                context.user_data.get("del_tg", False),
                context.user_data.get("del_has_tw", False),
                context.user_data.get("del_has_tg", False),
            )
        )
        return

    if query.data == "deltoggle_tg":
        if not context.user_data.get("del_has_tg"):
            return
        context.user_data["del_tg"] = not context.user_data.get("del_tg", False)
        await query.edit_message_reply_markup(
            reply_markup=_build_delete_kb(
                context.user_data.get("del_tw", False),
                context.user_data.get("del_wp", True),
                context.user_data["del_tg"],
                context.user_data.get("del_has_tw", False),
                context.user_data.get("del_has_tg", False),
            )
        )
        return

    if query.data == "del_execute":
        del_wp = context.user_data.get("del_wp", False)
        del_tw = context.user_data.get("del_tw", False)
        del_tg = context.user_data.get("del_tg", False)

        if not any([del_wp, del_tw, del_tg]):
            await query.edit_message_text("Nada seleccionado. Edición cancelada.")
            return

        await query.edit_message_text("Borrando...")

        meta = parse_social_meta(post.get("content", ""))
        results = []

        # Borrar de Twitter
        if del_tw and meta.get("tweet_id"):
            ok = await asyncio.to_thread(delete_tweet, meta["tweet_id"])
            results.append("✅ Tweet borrado" if ok else "❌ Error borrando tweet")

        # Borrar del canal TG
        if del_tg and meta.get("tg_msg"):
            try:
                msg_id = int(meta["tg_msg"])
                ok = await delete_from_channel(context.bot, msg_id)
                results.append("✅ Mensaje del canal borrado" if ok
                               else "❌ Error borrando del canal (puede ser muy viejo)")
            except ValueError:
                results.append("❌ tg_msg inválido en el post")

        # Borrar de WordPress (último, porque cambia la URL)
        if del_wp:
            ok = await asyncio.to_thread(trash_post, post["id"])
            results.append("✅ Nota enviada a la papelera de WordPress" if ok
                           else "❌ Error borrando de WordPress")

        # Limpiar estado
        context.user_data.pop("edit_post", None)
        context.user_data.pop("del_wp", None)
        context.user_data.pop("del_tw", None)
        context.user_data.pop("del_tg", None)
        context.user_data.pop("del_has_tw", None)
        context.user_data.pop("del_has_tg", None)

        await query.edit_message_text("\n".join(results) or "Nada que borrar.")
        return

    # ── Publicar en redes: mostrar toggles ──
    if query.data == "edit_publish":
        meta = parse_social_meta(post.get("content", ""))
        has_tw = bool(meta.get("tweet_id"))
        has_tg = bool(meta.get("tg_msg"))
        # Default: ON los destinos que todavía no se publicaron
        context.user_data["pub_tw"] = not has_tw
        context.user_data["pub_tg"] = not has_tg
        context.user_data["pub_wa"] = False

        status_lines = []
        if has_tw:
            status_lines.append(f"⚠️ Ya tuiteado (id `{meta.get('tweet_id')}`) — se creará uno nuevo si lo marcás")
        if has_tg:
            status_lines.append(f"⚠️ Ya en canal TG (msg `{meta.get('tg_msg')}`) — se publicará un segundo mensaje")
        status_str = "\n".join(status_lines) if status_lines else "Sin publicaciones previas."

        await query.edit_message_text(
            f"📡 *Publicar en redes*\n\n*{post['title']}*\n\n"
            f"{status_str}\n\n"
            "Elegí destinos:",
            parse_mode="Markdown",
            reply_markup=_build_publish_social_kb(
                context.user_data["pub_tw"],
                context.user_data["pub_tg"],
                context.user_data["pub_wa"],
            ),
        )
        return

    # Toggles de publicar en redes
    if query.data == "pubtoggle_tw":
        context.user_data["pub_tw"] = not context.user_data.get("pub_tw", False)
        await query.edit_message_reply_markup(
            reply_markup=_build_publish_social_kb(
                context.user_data["pub_tw"],
                context.user_data.get("pub_tg", False),
                context.user_data.get("pub_wa", False),
            )
        )
        return

    if query.data == "pubtoggle_tg":
        context.user_data["pub_tg"] = not context.user_data.get("pub_tg", False)
        await query.edit_message_reply_markup(
            reply_markup=_build_publish_social_kb(
                context.user_data.get("pub_tw", False),
                context.user_data["pub_tg"],
                context.user_data.get("pub_wa", False),
            )
        )
        return

    if query.data == "pubtoggle_wa":
        context.user_data["pub_wa"] = not context.user_data.get("pub_wa", False)
        await query.edit_message_reply_markup(
            reply_markup=_build_publish_social_kb(
                context.user_data.get("pub_tw", False),
                context.user_data.get("pub_tg", False),
                context.user_data["pub_wa"],
            )
        )
        return

    if query.data == "pub_execute":
        pub_tw = context.user_data.get("pub_tw", False)
        pub_tg = context.user_data.get("pub_tg", False)
        pub_wa = context.user_data.get("pub_wa", False)

        if not any([pub_tw, pub_tg, pub_wa]):
            await query.edit_message_text("Nada seleccionado. Cancelado.")
            return

        await query.edit_message_text("Publicando en redes...")
        data = await asyncio.to_thread(_post_to_data, post)
        post_url = post["link"]

        results = []
        current_meta = parse_social_meta(post.get("content", ""))
        new_tweet_id = current_meta.get("tweet_id", "")
        try:
            new_tg_msg = int(current_meta.get("tg_msg", 0) or 0)
        except (ValueError, TypeError):
            new_tg_msg = 0

        if pub_tg:
            tg_msg_id = await publish_to_channel(context.bot, data, post_url)
            if tg_msg_id:
                results.append("✅ Publicado en canal @MundoEmpresarial_AR")
                new_tg_msg = tg_msg_id
            else:
                results.append("❌ Error al publicar en canal TG")

        if pub_tw:
            tweet_url = await asyncio.to_thread(post_tweet, data, post_url)
            if tweet_url:
                new_tweet_id = tweet_url.rsplit("/", 1)[-1]
                results.append(f"✅ Tuit publicado: {tweet_url}")
            else:
                results.append("❌ Error al publicar en Twitter")

        if pub_wa:
            s_title = get_title(data)
            wa_text = f"📰 {s_title}\n\n{data['excerpt'][:200]}\n\n🔗 {utm_url(post_url, 'whatsapp')}"
            await query.message.reply_text(f"— Copiá y pegá en WhatsApp —\n\n{wa_text}")
            results.append("✅ Texto para WhatsApp preparado")

        # Persistir ids nuevos en el post
        if (pub_tg and new_tg_msg) or (pub_tw and new_tweet_id):
            await asyncio.to_thread(
                append_social_meta,
                post["id"], post.get("content", ""),
                new_tweet_id, new_tg_msg,
            )

        # Limpiar estado
        context.user_data.pop("edit_post", None)
        context.user_data.pop("pub_tw", None)
        context.user_data.pop("pub_tg", None)
        context.user_data.pop("pub_wa", None)

        await query.edit_message_text("\n".join(results) or "Nada ejecutado.")
        return


async def _handle_edit_photo_url(url: str, post: dict) -> bool:
    """Descarga imagen desde URL y la setea como destacada del post."""
    kw = focus_keyword(post["title"])
    alt = f"{kw} - {post['title']}"
    media_id = upload_image(url, alt)
    if not media_id:
        return False
    return update_post(post["id"], {"featured_media": media_id})


async def _handle_edit_photo_bytes(img_bytes: bytes, ctype: str, post: dict) -> bool:
    """Sube bytes de imagen a WordPress y la setea como destacada."""
    try:
        ext = ctype.split("/")[-1] if "/" in ctype else "jpg"
        h = {**wp_auth(), "Content-Disposition": f"attachment; filename=editada.{ext}",
             "Content-Type": ctype}
        r = requests.post(
            f"{WP_URL}/wp-json/wp/v2/media", headers=h, data=img_bytes, timeout=30
        )
        if r.status_code != 201:
            logger.error(f"Upload photo: {r.status_code} {r.text[:200]}")
            return False
        media_id = r.json()["id"]

        kw = focus_keyword(post["title"])
        alt = f"{kw} - {post['title']}"
        requests.post(
            f"{WP_URL}/wp-json/wp/v2/media/{media_id}",
            headers={**wp_auth(), "Content-Type": "application/json"},
            json={"alt_text": alt, "caption": alt},
            timeout=10,
        )
        return update_post(post["id"], {"featured_media": media_id})
    except Exception as e:
        logger.error(f"edit_photo_bytes: {e}")
        return False


async def handle_photo_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Maneja foto enviada por Telegram durante edición."""
    if not context.user_data.get("waiting_for_edit_photo"):
        return
    post = context.user_data.get("edit_post")
    if not post:
        await update.message.reply_text("No hay nota en edición.")
        return

    context.user_data["waiting_for_edit_photo"] = False
    msg = await update.message.reply_text("Subiendo foto...")

    try:
        # Obtener la foto más grande
        photo = update.message.photo[-1]
        file = await photo.get_file()
        img_bytearr = await file.download_as_bytearray()
        img_bytes = bytes(img_bytearr)

        ok = await _handle_edit_photo_bytes(img_bytes, "image/jpeg", post)
        if ok:
            await msg.edit_text(f"✅ Foto actualizada.\n\n{post['link']}")
        else:
            await msg.edit_text("❌ Error al actualizar la foto.")
    except Exception as e:
        logger.error(f"handle_photo: {e}")
        await msg.edit_text(f"❌ Error: {e}")


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

def _wait_for_lock_release(max_wait: int = 20):
    """
    Al iniciar, espera a que la otra instancia (contenedor viejo de Railway)
    libere el getUpdates lock. Si pasa más del tiempo, forzamos webhook delete.
    """
    import time
    for attempt in range(max_wait):
        try:
            # deleteWebhook con drop_pending_updates libera el polling lock
            r = requests.post(
                f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/deleteWebhook",
                json={"drop_pending_updates": True},
                timeout=10,
            )
            if r.status_code == 200:
                # Probar getUpdates con timeout corto
                r2 = requests.post(
                    f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/getUpdates",
                    json={"timeout": 1, "limit": 1},
                    timeout=15,
                )
                if r2.status_code == 200:
                    logger.info(f"Telegram lock liberado (intento {attempt+1})")
                    return True
                if r2.status_code == 409:
                    logger.warning(f"Lock ocupado, reintentando en 3s (intento {attempt+1})")
                    time.sleep(3)
                    continue
        except Exception as e:
            logger.warning(f"Error en wait_for_lock: {e}")
            time.sleep(2)
    logger.warning("No se pudo confirmar que el lock esté libre, siguiendo igual")
    return False


def main():
    # Esperar a que la instancia anterior libere el lock (evita 409 Conflict)
    _wait_for_lock_release()

    app = Application.builder().token(TELEGRAM_TOKEN).build()
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("borrar", cmd_borrar))
    app.add_handler(CommandHandler("editar", cmd_editar))
    app.add_handler(CommandHandler("testtwitter", cmd_testtwitter))
    app.add_handler(CommandHandler("stats", cmd_stats))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_link))
    app.add_handler(MessageHandler(filters.PHOTO, handle_photo_message))
    # Patrón más específico para /borrar (confirmar/cancelar), antes del nuevo flow de /editar
    app.add_handler(CallbackQueryHandler(handle_delete_button, pattern="^del_(confirm|cancel)$"))
    app.add_handler(CallbackQueryHandler(handle_edit_button, pattern="^(edit_|setcat_|deltoggle_|del_execute|pubtoggle_|pub_execute)"))
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
