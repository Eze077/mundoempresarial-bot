import os
import re
import io
import json
import logging
import asyncio
import base64
import unicodedata
import uuid
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
    TypeHandler,
    ApplicationHandlerStop,
)

logging.basicConfig(
    format="%(asctime)s - %(levelname)s - %(message)s", level=logging.INFO
)
logger = logging.getLogger(__name__)

TELEGRAM_TOKEN = os.environ["TELEGRAM_TOKEN"]
WP_URL  = os.environ.get("WP_URL", "https://mundoempresarial.ar").rstrip("/")
WP_USER = os.environ["WP_USER"]
WP_PASS = os.environ["WP_PASS"]

# Path a la DB del harness. Global de módulo: algunos handlers (ej. pip_stage_sin_imagen,
# L6451) lo usaban ANTES de su definición local → NameError. Definido acá una vez.
_HDB = "/opt/me-harness/harness.db"

TWITTER_API_KEY    = os.environ.get("TW_KEY", "") or os.environ.get("TWITTER_API_KEY", "")
TWITTER_API_SECRET = os.environ.get("TW_SECRET", "") or os.environ.get("TWITTER_API_SECRET", "")
TWITTER_TOKEN      = os.environ.get("TW_TOKEN", "") or os.environ.get("TWITTER_ACCESS_TOKEN", "")
TWITTER_SECRET     = os.environ.get("TW_TSECRET", "") or os.environ.get("TWITTER_ACCESS_SECRET", "")

TELEGRAM_CHANNEL   = os.environ.get("TELEGRAM_CHANNEL", "@MundoEmpresarial_AR")

LINKEDIN_TOKEN     = os.environ.get("LINKEDIN_TOKEN", "")   # member (perfil personal de Leo)
LINKEDIN_CLIENT_ID = os.environ.get("LINKEDIN_CLIENT_ID", "77jyyt3btvsjwv")
LINKEDIN_ORG_ID    = os.environ.get("LINKEDIN_ORG_ID", "67751917")
LINKEDIN_MEMBER_ID = os.environ.get("LINKEDIN_MEMBER_ID", "")
# Token org (página Mundo Empresarial AR). El harness lo auto-renueva y lo persiste
# en LINKEDIN_TOKEN_STATE; el bot lo lee de ahí (fuente de verdad) o del env como fallback.
LINKEDIN_ORG_TOKEN   = os.environ.get("LINKEDIN_ORG_TOKEN", "")
LINKEDIN_TOKEN_STATE = os.environ.get("LINKEDIN_TOKEN_STATE", "/opt/me-harness/linkedin_org_token.json")
_LAST_LINKEDIN_ERROR = ""
_LAST_LINKEDIN_URN = ""
_LINKEDIN_MEMBER_URN_CACHE: str | None = None
_LAST_WP_ERROR = ""
# Modo pausa: cuando True, el bot ignora links y scheduled posts
BOT_PAUSED = False
# Chat ID del operador para reportes diarios (se detecta del primer mensaje)
ADMIN_CHAT_ID      = os.environ.get("ADMIN_CHAT_ID", "")

# ── Control de acceso (2026-07-07) ────────────────────────────────────────────
# El bot NO tenía filtro de identidad: cualquiera que lo encontrara podía mandar
# /borrar, /publicador, tocar botones destructivos. Este guard global (registrado
# con TypeHandler en group=-1) corre ANTES que todo y frena a cualquiera que no sea
# el operador. Un solo lugar cubre comandos + botones + texto + fotos.
def _admin_ids() -> set:
    """Ids autorizados. Set (fácil de extender a lista); hoy solo ADMIN_CHAT_ID."""
    ids = set()
    for raw in (ADMIN_CHAT_ID or "").replace(";", ",").split(","):
        raw = raw.strip()
        if raw.isdigit():
            ids.add(int(raw))
    return ids


async def _solo_admin(update, context):
    """Guard global: solo el operador opera el bot. Fail-closed (sin admin configurado,
    nadie pasa). Silencio + log ante un extraño (no confirmarle que el bot existe)."""
    user = getattr(update, "effective_user", None)
    if user is None:
        return  # channel post / my_chat_member: no es un comando de usuario, seguir
    if user.id in _admin_ids():
        return  # operador → flujo normal
    logger.warning("ACCESO DENEGADO: user=%s (@%s) tipo=%s",
                   user.id, getattr(user, "username", "?"),
                   "callback" if update.callback_query else "mensaje")
    raise ApplicationHandlerStop  # frena TODA la cadena de handlers para este update


_ENV_FILE = "/etc/mundoempresarial-bot.env"

def _persist_admin_chat_id(chat_id: str):
    """Guarda ADMIN_CHAT_ID en el env file para sobrevivir reinicios."""
    global ADMIN_CHAT_ID
    ADMIN_CHAT_ID = chat_id
    try:
        try:
            with open(_ENV_FILE, "r") as f:
                lines = f.readlines()
        except FileNotFoundError:
            lines = []
        new_lines = [l for l in lines if not l.startswith("ADMIN_CHAT_ID=")]
        new_lines.append(f"ADMIN_CHAT_ID={chat_id}\n")
        with open(_ENV_FILE, "w") as f:
            f.writelines(new_lines)
        logger.info(f"ADMIN_CHAT_ID persistido: {chat_id}")
    except Exception as e:
        logger.warning(f"No se pudo persistir ADMIN_CHAT_ID: {e}")

# OpenAI API key (opcional, solo para fallback de transcripción Whisper)
OPENAI_API_KEY     = os.environ.get("OPENAI_API_KEY", "")
GA4_PROPERTY_ID    = os.environ.get("GA4_PROPERTY_ID", "")
GA4_SA_PATH        = os.environ.get("GA4_SA_PATH", "")

# Cookies de YouTube para bypassar ban de IP en VPS (archivo Netscape)
_YT_COOKIES_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "youtube_cookies.txt")
YOUTUBE_COOKIES  = _YT_COOKIES_PATH if os.path.isfile(_YT_COOKIES_PATH) else None

# Proxy WARP (Cloudflare) en el VPS para bypassar ban de IP de YouTube
# warp-cli mode proxy + warp-cli proxy port 40000 + warp-cli connect
YOUTUBE_PROXY = "socks5://127.0.0.1:40000"

# Cookies de Instagram (sesión exportada en formato Netscape desde Chrome)
_IG_COOKIES_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "instagram_cookies.txt")
INSTAGRAM_COOKIES = _IG_COOKIES_PATH if os.path.isfile(_IG_COOKIES_PATH) else None

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

# Sufijos de verbos conjugados en español — se usan como etiquetas por error
_VERB_ENDINGS = (
    "ó", "ió", "aron", "ieron", "aba", "ían", "ará", "arán", "ería",
    "ando", "iendo", "aste", "imos", "aron",
)

# Adjetivos y palabras genéricas que no aportan valor como etiqueta
_GENERIC_TAGS = {
    "nuevo", "nueva", "nuevos", "nuevas", "gran", "grande", "grandes",
    "primer", "primera", "primero", "último", "última", "últimos",
    "porteño", "porteña", "porteños", "nacional", "federal", "oficial",
    "propio", "propia", "mayor", "menor", "mismo", "misma",
    "total", "pleno", "plena", "clave", "real", "alto", "alta", "baja", "bajo",
    "durante", "parte", "caso", "nivel", "tipo", "vez", "año", "años",
    "millones", "miles", "pesos", "dólares", "dolar", "más", "menos",
    "medida", "sector", "ámbito", "campo", "marco",
}

# Mapeo de términos → hashtag normalizado (entidades y temas argentinos)
_HT_NORMALIZE = {
    "afip":         "ARCA",
    "arca":         "ARCA",
    "bcra":         "BCRA",
    "bancocentral": "BCRA",
    "gcba":         "GCBA",
    "caba":         "CABA",
    "indec":        "INDEC",
    "anses":        "ANSES",
    "vtv":          "VTV",
    "smvm":         "SMVM",
    "conicet":      "CONICET",
    "ypf":          "YPF",
    "inflación":    "inflacion",
    "inflacion":    "inflacion",
    "dólar":        "dolar",
    "dolar":        "dolar",
    "merval":       "Merval",
    "monotributo":  "Monotributo",
    "facturación":  "facturacion",
    "facturacion":  "facturacion",
    "empleo":       "Empleo",
    "salario":      "Salarios",
    "salarios":     "Salarios",
    "exportación":  "Exportaciones",
    "exportaciones":"Exportaciones",
    "importación":  "Importaciones",
    "pyme":         "Pymes",
    "pymes":        "Pymes",
    "autónomos":    "Autonomos",
    "autonomos":    "Autonomos",
}

_HT_FEEDBACK_FILE = "/opt/mundoempresarial-bot/ht_feedback.json"
_HT_FREQ_FILE     = "/opt/mundoempresarial-bot/ht_freq.json"
_HT_MAPPING_FILE  = "/opt/mundoempresarial-bot/ht_mapping.json"

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
    239: [  # Digitalización Pymes — SOLO tecnología aplicada a pymes / innovación empresarial
          # No usar palabras genéricas como "digital" / "plataforma" / "app" / "aplicación" sueltas
          "digitalización de pymes", "transformación digital",
          "pyme digital", "pymes digitales",
          "ecommerce", "e-commerce", "comercio electrónico", "tienda online",
          "fintech", "neobanco", "billetera virtual", "billetera electrónica",
          "software empresarial", "sistema de gestión", "erp", "crm",
          "facturación electrónica",
          "marketplace", "plataforma b2b", "plataforma b2c",
          "automatización industrial", "robótica industrial",
          "ciberseguridad empresarial", "ciberataque empresa",
          "inteligencia artificial aplicada", "ia generativa", "chatgpt empresa",
          "machine learning empresarial",
          "cloud computing", "servicios en la nube", "saas",
          "startup argentina", "startups", "unicornio argentino",
          "innovación empresarial", "i+d empresarial",
          "blockchain empresarial", "tokenización",
          "big data", "data analytics",
          "iot industrial", "industria 4.0"],
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
    # Mundo del vino — SIN 'vino'/'vinos'/'bodega'/'bodegas' sueltos (matcheaban el verbo
    # 'vino'/'provino' y depósitos). Solo términos inequívocos. Fix 2026-07-15.
    1139:["malbec", "torrontés", "torrontes", "cabernet", "chardonnay", "syrah", "pinot",
          "viñedo", "viñedos", "vitivinícola", "vitivinicola", "enología", "enólogo",
          "enólogos", "sommelier", "sommeliers", "vinoteca", "maridaje", "cepa", "cepas",
          "coviar", "industria vitivinícola", "exportación de vinos", "vino tinto",
          "vino blanco", "vendimia", "cosecha vitivinícola"],
}

def detect_categories(title: str, text: str, excerpt: str) -> list:
    import re as _re
    corpus = (title + " " + title + " " + title + " " + excerpt + " " + (text[:600] or "")).lower()
    scores = {}
    for cat_id, kws in CATEGORY_KEYWORDS.items():
        # word-boundary: antes 'corpus.count(kw)' era substring ('gas'→'gaseosa', 'vino'→verbo)
        score = sum(len(_re.findall(r"\b" + _re.escape(kw) + r"\b", corpus)) for kw in kws)
        if score > 0:
            scores[cat_id] = score
    if not scores:
        return [98]
    ranked = sorted(scores, key=scores.get, reverse=True)
    return ranked[:3]


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


def rewrite_excerpt_with_gpt(title: str, text: str, original_excerpt: str, keyword: str = "") -> str:
    """
    Reescribe la bajada (excerpt) en el estilo editorial de MundoEmpresarial.
    Igual que con el título: no recorta, reelabora.
    Fallback a meta_description() si no hay OPENAI_API_KEY o si GPT falla.
    """
    if not OPENAI_API_KEY:
        return meta_description(original_excerpt, text, kw=keyword)

    prompt = (
        "Sos el editor de MundoEmpresarial.ar, medio económico argentino "
        "para pymes. Te paso el título de una nota, la bajada original de la "
        "fuente y los primeros párrafos del texto. Escribí una NUEVA bajada "
        "en el estilo del medio.\n\n"
        "REGLAS OBLIGATORIAS:\n"
        "1. Largo: ENTRE 120 y 155 caracteres (Rank Math lo premia).\n"
        f"2. Debe contener el keyword: \"{keyword}\"\n"
        "3. COMPLEMENTA el título, NO lo repite palabra por palabra.\n"
        "4. Aporta un dato fresco, gancho, contexto o consecuencia que "
        "invite a leer.\n"
        "5. Español rioplatense (vos), directo, informativo. Sin clickbait.\n"
        "6. Tercera persona, voz activa.\n"
        "7. Sin puntos suspensivos. Terminá con punto o sin puntuación final.\n"
        "8. No envuelvas la respuesta en comillas.\n\n"
        f"Título: {title}\n\n"
        f"Bajada original (referencia, no copiar):\n{(original_excerpt or '')[:400]}\n\n"
        f"Primeros párrafos:\n{(text or '')[:1500]}\n\n"
        "Devolvé SOLO la bajada, una sola línea, nada más."
    )

    try:
        r = openai_post(
            "https://api.openai.com/v1/chat/completions",
            headers={
                "Authorization": f"Bearer {OPENAI_API_KEY}",
                "Content-Type": "application/json",
            },
            json={
                "model": "gpt-4o-mini",
                "messages": [{"role": "user", "content": prompt}],
                "temperature": 0.45,
            },
            timeout=30,
        )
        if r.status_code == 200:
            result = r.json()["choices"][0]["message"]["content"].strip()
            # Limpiar comillas por si GPT las puso
            result = result.strip('"').strip("'").strip("«»").strip()
            # Sacar prefijos tipo "Bajada:" si GPT los metió
            result = re.sub(r'^(?:Bajada|Excerpt|Subtítulo)\s*:\s*', '', result, flags=re.IGNORECASE)
            # Forzar largo <=156
            if len(result) > 156:
                cut = result[:153]
                boundary = cut.rfind(" ")
                result = (cut[:boundary] if boundary > 100 else cut) + "..."
            # Si el keyword no quedó, anteponerlo
            if keyword and keyword.lower() not in result.lower():
                result = meta_description(result, text, kw=keyword)
            logger.info(f"GPT bajada OK: {len(result)} chars")
            return result
        logger.warning(f"GPT excerpt {r.status_code}: {r.text[:200]}")
    except Exception as e:
        logger.warning(f"GPT excerpt error: {e}")

    return meta_description(original_excerpt, text, kw=keyword)


def detect_content_type(title: str) -> tuple:
    """Detecta si el título es un listicle con cantidad explícita. Retorna ('listicle', N) o ('standard', 0)."""
    m = re.search(
        r'\b(\d+)\s+(?:lugares|tips|consejos|formas|claves|razones|errores|pasos|ideas|opciones|'
        r'recursos|platos|bodegas|vinos|recetas|herramientas|apps|productos|empresas|marcas|'
        r'tendencias|datos|destinos|ciudades|barrios|restaurantes|actividades|propuestas|'
        r'cosas|puntos|aspectos|ventajas|beneficios|museos|hoteles|cafés|cafes)\b',
        title, re.I
    )
    if m:
        return ('listicle', int(m.group(1)))
    return ('standard', 0)


def _is_valid_h2(content: str) -> bool:
    """Verifica que un H2 heading sea válido para el ToC (filtra URLs, corchetes, vacíos)."""
    if not content or len(content) < 3:
        return False
    if 'http' in content.lower():
        return False
    if '[' in content or ']' in content:
        return False
    if len(content) > 100:
        return False
    return True


def _gpt_format_article(title: str, text: str, source_url: str,
                        kw: str = "", redactor_instr: str = "",
                        content_type: str = "standard", expected_count: int = 0) -> dict | None:
    """
    Usa GPT-4o-mini para formatear texto libre en HTML estructurado para WP.
    Devuelve {"html": str, "h2_headings": list, "bullets": list} o None si falla.
    content_type: 'standard' o 'listicle'. expected_count: nro de items esperados en listicle.
    """
    if not OPENAI_API_KEY:
        return None

    instr_extra = f"\nINSTRUCCIÓN ADICIONAL DEL EDITOR: {redactor_instr}" if redactor_instr else ""
    pyme_box_template = (
        '<div style="background:#eaf4fb;border-left:5px solid #1a6fa8;padding:16px 20px;'
        'margin:32px 0 16px 0;border-radius:0 6px 6px 0;">'
        '<p style="margin:0 0 8px 0;font-size:13px;font-weight:700;letter-spacing:1px;'
        'color:#1a6fa8;text-transform:uppercase;">&#128196; Resumen para Pymes</p>'
        '<p style="margin:0;font-size:15px;line-height:1.6;color:#222;">RESUMEN_AQUI</p></div>'
    )

    if content_type == "listicle" and expected_count > 0:
        prompt = f"""Sos el redactor de MundoEmpresarial.ar. El siguiente texto es un LISTADO de {expected_count} items.
Formatealo en HTML limpio para WordPress preservando TODOS los items del texto fuente.
El lector es un empresario pyme, monotributista o profesional independiente argentino con poco tiempo.

REGLAS OBLIGATORIAS:
0. ANTES del HTML, generá 3 a 5 bullets que destaquen las mejores opciones o categorías del listado.
   Encerralos entre estas marcas exactas (JSON array, strings sin HTML):
   BULLETS_START
   ["bullet sobre categoría o top pick 1", "bullet 2", "bullet 3"]
   BULLETS_END
1. Incluí TODOS los {expected_count} items del texto fuente, numerados. NO omitas ninguno.
   Formato de cada item: <p><strong>N. Nombre</strong>: descripción basada en el texto fuente.</p>
2. Agrupalos en MÍNIMO 4 secciones temáticas con <h2 id="slug-kebab-case">Título temático</h2>
   Los H2 son categorías reales (no "Items 1-5"). Cada H2 agrupa items relacionados.
3. NO inventés datos no presentes en el texto fuente (teléfono, dirección, precio, horario, etc.).
   Si el texto original no los tiene, no los incluyas.
4. Datos numéricos relevantes en <strong>dato</strong>.
5. Al final incluí este bloque exacto (reemplazá RESUMEN_AQUI con máx. 200 caracteres):
{pyme_box_template}
6. Tono: directo, informativo, español rioplatense. Sin "cabe destacar", "en el marco de".
7. NO incluyas: ToC, fuente, scripts, comentarios HTML.
8. Devolvé: BULLETS_START...BULLETS_END y luego el HTML del cuerpo. Nada más.{instr_extra}

TÍTULO: {title}
KEYWORD SEO: {kw}
FUENTE: {source_url}

TEXTO A FORMATEAR:
{text[:8000]}"""
    else:
        prompt = f"""Sos el redactor de MundoEmpresarial.ar. Formateá el siguiente texto en HTML limpio para WordPress.
El lector es un empresario pyme, monotributista o profesional independiente argentino con poco tiempo.

REGLAS OBLIGATORIAS:
0. ANTES del HTML principal, generá 3 a 5 bullets analíticos para el resumen "Lo que tenés que saber".
   Cada bullet debe resumir UN dato clave, cambio concreto o impacto para pymes — NO copies frases del texto.
   Encerralos entre estas marcas exactas (JSON array, strings sin HTML):
   BULLETS_START
   ["bullet analítico 1", "bullet con dato clave 2", "bullet impacto pyme 3"]
   BULLETS_END
1. Dividí el contenido en MÍNIMO 4 secciones con <h2 id="slug-en-kebab-case">Título analítico</h2>
   — el id debe ser único, en minúsculas, sin tildes, con guiones. Cada sección responde UNA pregunta concreta del lector.
2. Cada párrafo en <p>...</p>
3. Datos numéricos, porcentajes y cifras clave en <strong>dato</strong>
4. Al menos 1 link interno a otra nota de mundoempresarial.ar si el contexto lo permite
5. Al final incluí este bloque exacto (reemplazá RESUMEN_AQUI con máx. 200 caracteres):
{pyme_box_template}
6. Tono: directo, informativo, español rioplatense. Sin "cabe destacar", "en el marco de", "en pos de".
7. NO incluyas: ToC, fuente, scripts, comentarios HTML — eso lo agrega el sistema.
8. Devolvé: BULLETS_START...BULLETS_END y luego el HTML del cuerpo (H2s + párrafos + pyme-box). Nada más.{instr_extra}

TÍTULO: {title}
KEYWORD SEO: {kw}
FUENTE: {source_url}

TEXTO A FORMATEAR:
{text[:6000]}"""

    try:
        r = openai_post(
            "https://api.openai.com/v1/chat/completions",
            headers={"Authorization": f"Bearer {OPENAI_API_KEY}", "Content-Type": "application/json"},
            json={
                "model":       "gpt-4o-mini",
                "messages":    [{"role": "user", "content": prompt}],
                "temperature": 0.3,
            },
            timeout=45,
        )
        if r.status_code != 200:
            logger.warning(f"_gpt_format_article {r.status_code}: {r.text[:200]}")
            return None

        raw_response = r.json()["choices"][0]["message"]["content"].strip()
        # Limpiar markdown fences si GPT los puso
        raw_response = re.sub(r'^```(?:html|json)?\s*', '', raw_response)
        raw_response = re.sub(r'\s*```$', '', raw_response).strip()

        # Extraer bullets analíticos del bloque BULLETS_START...BULLETS_END
        bullets_list = []
        bullets_match = re.search(r'BULLETS_START\s*(\[.*?\])\s*BULLETS_END', raw_response, re.DOTALL)
        if bullets_match:
            try:
                bullets_list = json.loads(bullets_match.group(1))
                if not isinstance(bullets_list, list):
                    bullets_list = []
            except Exception:
                bullets_list = []
            html = raw_response[:bullets_match.start()].strip() + "\n" + raw_response[bullets_match.end():].strip()
            html = html.strip()
        else:
            html = raw_response

        # Block 2: verificar completitud para listicles — reintentar si faltan items
        if content_type == "listicle" and expected_count > 0:
            found_count = len(re.findall(r'<strong>\s*\d+\s*\.', html))
            if found_count < int(expected_count * 0.8):
                logger.warning(f"_gpt_format_article listicle incompleto: {found_count}/{expected_count}, reintentando")
                retry_prompt = (
                    prompt
                    + f"\n\n⚠️ VERIFICACIÓN: el texto fuente tiene {expected_count} items numerados. "
                    f"Tu respuesta anterior incluye solo ~{found_count}. "
                    f"Incluí TODOS los {expected_count} items, sin excepción."
                )
                r2 = openai_post(
                    "https://api.openai.com/v1/chat/completions",
                    headers={"Authorization": f"Bearer {OPENAI_API_KEY}", "Content-Type": "application/json"},
                    json={"model": "gpt-4o-mini", "messages": [{"role": "user", "content": retry_prompt}],
                          "temperature": 0.3},
                    timeout=60,
                )
                if r2.status_code == 200:
                    raw2 = r2.json()["choices"][0]["message"]["content"].strip()
                    raw2 = re.sub(r'^```(?:html|json)?\s*', '', raw2)
                    raw2 = re.sub(r'\s*```$', '', raw2).strip()
                    bm2 = re.search(r'BULLETS_START\s*(\[.*?\])\s*BULLETS_END', raw2, re.DOTALL)
                    if bm2:
                        try:
                            bl2 = json.loads(bm2.group(1))
                            bullets_list = bl2 if isinstance(bl2, list) else bullets_list
                        except Exception:
                            pass
                        html = raw2[:bm2.start()].strip() + "\n" + raw2[bm2.end():].strip()
                        html = html.strip()
                    else:
                        html = raw2
                    found_after = len(re.findall(r'<strong>\s*\d+\s*\.', html))
                    logger.info(f"_gpt_format_article retry listicle: {found_after}/{expected_count} items")

        # Extraer H2 headings para el ToC
        h2_headings = []
        for m in re.finditer(r'<h2[^>]*\s+id="([^"]+)"[^>]*>(.*?)</h2>', html, re.DOTALL | re.IGNORECASE):
            anchor = m.group(1).strip()
            label  = re.sub(r'<[^>]+>', '', m.group(2)).strip()
            if anchor and label and _is_valid_h2(label):
                h2_headings.append({"content": label, "anchor": anchor})

        logger.info(f"_gpt_format_article OK: {len(html)} chars, {len(h2_headings)} H2s, {len(bullets_list)} bullets")
        return {"html": html, "h2_headings": h2_headings, "bullets": bullets_list}

    except Exception as e:
        logger.warning(f"_gpt_format_article error: {e}")
        return None


def _gpt_html_to_gutenberg(html: str) -> str:
    """Convierte HTML crudo de GPT a bloques Gutenberg nativos (para notas NO desplegables)."""
    blocks = []
    remaining = html.strip()
    _PAT = [
        (re.compile(r'^(<h2\b[^>]*>.*?</h2>)', re.DOTALL | re.IGNORECASE),
         '<!-- wp:heading {{"level":2}} -->\n{}\n<!-- /wp:heading -->'),
        (re.compile(r'^(<p\b[^>]*>.*?</p>)', re.DOTALL | re.IGNORECASE),
         '<!-- wp:paragraph -->\n{}\n<!-- /wp:paragraph -->'),
        (re.compile(r'^(<ul\b[^>]*>.*?</ul>)', re.DOTALL | re.IGNORECASE),
         '<!-- wp:list -->\n{}\n<!-- /wp:list -->'),
        (re.compile(r'^(<div\b.*?</div>)', re.DOTALL | re.IGNORECASE),
         '<!-- wp:html -->\n{}\n<!-- /wp:html -->'),
    ]
    while remaining:
        matched = False
        for pat, tpl in _PAT:
            m = pat.match(remaining)
            if m:
                blocks.append(tpl.format(m.group(1)))
                remaining = remaining[m.end():].strip()
                matched = True
                break
        if not matched:
            nxt = re.search(r'<(?:h2|p\b|ul|div)\b', remaining[1:], re.IGNORECASE)
            if nxt:
                remaining = remaining[1 + nxt.start():]
            else:
                break
    return '\n\n'.join(blocks)


def get_excerpt(data: dict, kw: str = "") -> str:
    """Devuelve la bajada a usar según los flags del preview.

    Prioridad:
    1. Editada manualmente (data['excerpt_edited']) → tal cual.
    2. Toggle 'bajada original' ON → la del og:description de la fuente.
    3. Reescrita por GPT (cacheada en data['rewritten_excerpt']).
    4. Fallback: meta_description() de la original.
    """
    if data.get("excerpt_edited"):
        return data.get("excerpt", "")
    original = data.get("original_excerpt") or data.get("excerpt", "")
    if data.get("orig_excerpt_on"):
        return meta_description(original, data.get("text", ""), kw=kw)
    cached = data.get("rewritten_excerpt", "")
    if cached:
        return cached
    return meta_description(original, data.get("text", ""), kw=kw)


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
    """Extrae actores, entidades y temas del título. Filtra verbos, adjetivos y genéricos."""
    tags = []
    for w in title.split():
        clean = w.strip('.,;:!?()[]"\'«»—%$')
        lower = clean.lower()
        if not clean or len(clean) <= 3:
            continue
        if lower in STOP_WORDS:
            continue
        if lower in _GENERIC_TAGS:
            continue
        # Filtrar verbos conjugados por sufijo
        if any(lower.endswith(suf) for suf in _VERB_ENDINGS):
            continue
        # Preservar siglas (todo mayúsculas), capitalizar el resto
        tag = clean if clean.isupper() and len(clean) >= 2 else clean.capitalize()
        # Normalizar entidades conocidas
        tag = _HT_NORMALIZE.get(lower, tag)
        tags.append(tag)
    return list(dict.fromkeys(tags))[:6]


def _load_ht_mapping() -> dict:
    try:
        with open(_HT_MAPPING_FILE) as f:
            return json.load(f)
    except Exception:
        return {}


def _update_ht_freq(tags: list, used_hts: str):
    try:
        try:
            with open(_HT_FREQ_FILE) as f:
                freq = json.load(f)
        except Exception:
            freq = {}
        for ht in used_hts.split():
            if not ht.startswith("#"):
                continue
            for tag in tags:
                tag_l = tag.lower()
                if tag_l not in freq:
                    freq[tag_l] = {}
                freq[tag_l][ht] = freq[tag_l].get(ht, 0) + 1
        with open(_HT_FREQ_FILE, "w") as f:
            json.dump(freq, f, ensure_ascii=False)
    except Exception as e:
        logger.warning(f"_update_ht_freq: {e}")


def _save_ht_feedback(data: dict, suggested: str, used: str):
    """Guarda corrección de HT solo si el usuario cambió algo. Actualiza freq."""
    if not data or suggested.strip() == used.strip():
        return
    import datetime as _dt
    tags = data.get("tags") or extract_tags(data.get("title", ""))
    entry = {
        "ts":        _dt.datetime.now().isoformat(),
        "tags":      tags,
        "cats":      data.get("categories", []),
        "title":     (data.get("title") or "")[:80],
        "suggested": suggested,
        "used":      used,
    }
    try:
        try:
            with open(_HT_FEEDBACK_FILE) as f:
                records = json.load(f)
        except Exception:
            records = []
        records.append(entry)
        records = records[-500:]
        with open(_HT_FEEDBACK_FILE, "w") as f:
            json.dump(records, f, ensure_ascii=False)
    except Exception as e:
        logger.warning(f"_save_ht_feedback: {e}")
    _update_ht_freq(tags, used)


def _build_hashtags(data: dict) -> str:
    """Genera hashtags: freq aprendida > mapping GPT > entidades hardcoded > tags > #Pymes."""
    title    = (data.get("title") or "").lower()
    excerpt  = (data.get("excerpt") or data.get("text") or "")[:400].lower()
    combined = title + " " + excerpt

    post_tags = data.get("tags") or extract_tags(data.get("title", ""))

    # Freq y mapping aprendidos
    try:
        with open(_HT_FREQ_FILE) as _f:
            freq = json.load(_f)
    except Exception:
        freq = {}
    learned = _load_ht_mapping()
    merged_map = {**_HT_NORMALIZE, **learned}

    tags = []

    # 1. Freq: para cada tag del post, el HT más usado (mínimo 2 ocurrencias)
    for t in post_tags[:4]:
        t_l = t.lower()
        if t_l in freq and freq[t_l]:
            best = max(freq[t_l], key=lambda h: freq[t_l][h])
            if freq[t_l][best] >= 2:
                candidate = best if best.startswith("#") else f"#{best}"
                if candidate not in tags:
                    tags.append(candidate)

    # 2. Mapping (hardcoded + aprendido GPT): entidades que aparecen en el texto
    for term, ht in merged_map.items():
        if term in combined:
            candidate = f"#{ht}"
            if candidate not in tags:
                tags.append(candidate)

    # 3. Tags extraídos del título como fallback
    for t in post_tags[:4]:
        candidate = f"#{t}"
        if candidate not in tags:
            tags.append(candidate)

    # 4. Anchor #Pymes
    if "#Pymes" not in tags:
        tags.append("#Pymes")

    return " ".join(tags[:5])


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


def _slugify_h2(text: str) -> str:
    text = unicodedata.normalize('NFKD', text).encode('ascii', 'ignore').decode('ascii')
    text = re.sub(r'[^\w\s-]', '', text).strip().lower()
    return re.sub(r'[\s_-]+', '-', text)


def _build_rank_math_toc(headings: list, hilo: int) -> str:
    """Build Rank Math ToC block from pre-collected heading data (content+anchor pairs)."""
    if len(headings) < 2:
        return ""
    TOC_TITLES = {
        1: "Informarse es respetarse, ejes que vas a leer acá:",
        2: "La voz de las pymes, ejes que vas a leer acá:",
        3: "Opiniones y análisis, ejes que vas a leer acá:",
    }
    title = TOC_TITLES.get(hilo, TOC_TITLES[2])
    attrs = {
        "title": title,
        "headings": [
            {"key": str(uuid.uuid4()), "content": h["content"], "level": 2,
             "link": f"#{h['anchor']}", "disable": False, "isUpdated": False, "isGeneratedLink": True}
            for h in headings
        ],
        "listStyle": "ul",
    }
    attrs_json = json.dumps(attrs, ensure_ascii=False, separators=(',', ':'))
    toc_items = ''.join(f'<li class=""><a href="#{h["anchor"]}">{h["content"]}</a></li>' for h in headings)
    return (
        f'<!-- wp:rank-math/toc-block {attrs_json} -->\n'
        f'<div class="wp-block-rank-math-toc-block" id="rank-math-toc">'
        f'<h2>{title}</h2><nav><ul>{toc_items}</ul></nav></div>\n'
        f'<!-- /wp:rank-math/toc-block -->'
    )


def _wrap_nota_desplegable(post_id: int, slug: str, content_html: str, data: dict) -> str:
    """Envuelve contenido en formato nota desplegable (bullets + ToC + expand btn + div oculto + JS)."""
    # 1. Extraer bloque ToC generado por _build_rank_math_toc
    toc_match = re.search(
        r'<!-- wp:rank-math/toc-block[^\n]*-->\s*(<div class="wp-block-rank-math-toc-block".*?</div>)\s*<!-- /wp:rank-math/toc-block -->',
        content_html, re.DOTALL
    )
    toc_raw = toc_match.group(1).strip() if toc_match else ""

    # 2. Extraer fuente (último wp:paragraph con <em>Fuente:)
    fuente_matches = list(re.finditer(
        r'<!-- wp:paragraph -->\s*(<p><em>Fuente:.*?</em></p>)\s*<!-- /wp:paragraph -->',
        content_html, re.DOTALL
    ))
    fuente_html = fuente_matches[-1].group(1).strip() if fuente_matches else ""

    # 3. Body = contenido sin ToC ni fuente, con Gutenberg comments eliminados
    body = content_html
    if toc_match:
        body = body[:toc_match.start()] + body[toc_match.end():]
    if fuente_matches:
        last_f = fuente_matches[-1]
        body = body[:last_f.start()] + body[last_f.end():]
    body = re.sub(r'<!-- /?wp:[^>]+ -->\n?', '', body).strip()

    # 4. Bullets: prioridad → GPT analíticos → párrafos GPT → excerpt
    gpt_bullets = [b for b in data.get("_gpt_bullets", []) if isinstance(b, str) and len(b) > 25]
    if gpt_bullets:
        bullets = [f'<li>{b}</li>' for b in gpt_bullets[:5]]
    else:
        gpt_html_src = data.get("_gpt_html", "")
        if gpt_html_src:
            bullets = []
            for m in re.finditer(r'<p(?:\s[^>]*)?>(.+?)</p>', gpt_html_src, re.DOTALL):
                p_text = re.sub(r'<[^>]+>', '', m.group(1)).strip()
                if len(p_text) < 30:
                    continue
                first_sent = re.match(r'([^.!?]+[.!?])', p_text)
                bullet_text = first_sent.group(1).strip() if first_sent else p_text[:200]
                if len(bullet_text) > 25:
                    bullets.append(f'<li>{bullet_text}</li>')
                if len(bullets) >= 5:
                    break
            if not bullets:
                bullets = [f'<li>{p_text[:200]}</li>']
        else:
            raw = (data.get("excerpt") or data.get("text", ""))[:700]
            sentences = re.split(r'(?<=[.!?])\s+', raw.strip())
            bullets = []
            for s in sentences:
                s = s.strip()
                if len(s) > 25:
                    bullets.append(f'<li>{s}</li>')
                if len(bullets) >= 5:
                    break
            if not bullets:
                bullets = [f'<li>{raw[:200]}</li>']
    bullets_html = '<ul>\n' + '\n'.join(bullets) + '\n</ul>'

    # 5. Reconstruir ToC con <p><strong>, estilos bordó y onclick expandir
    hilo = data.get("hilo", 2)
    toc_titles = {
        1: "Informarse es respetarse, ejes que vas a leer acá:",
        2: "La voz de las pymes, ejes que vas a leer acá:",
        3: "Opiniones y análisis, ejes que vas a leer acá:",
    }
    toc_title = toc_titles.get(hilo, toc_titles[2])
    toc_html = ""
    if toc_raw:
        items_m = re.search(r'<ul>(.*?)</ul>', toc_raw, re.DOTALL)
        if items_m:
            items = re.sub(
                r'<a href="#([^"]+)">',
                lambda m: (
                    f'<a href="#{m.group(1)}" '
                    f'onclick="return irA{post_id}(\'{m.group(1)}\')" '
                    f'style="font-weight:700;color:#800020;">'
                ),
                items_m.group(1)
            )
            toc_html = (
                f'<div class="wp-block-rank-math-toc-block" id="rank-math-toc">\n'
                f'<p><strong>{toc_title}</strong></p>\n'
                f'<nav><ul>{items}</ul></nav></div>'
            )

    # 6. JS compacto con toggle, GA4 (expand + collapse) y scroll-to-anchor
    js = (
        f'function expandirNota{post_id}(){{'
        f'var el=document.getElementById("nota-ampliada-{post_id}");'
        f'var btn=document.getElementById("btn-expandir-{post_id}");'
        f'if(el.style.display==="none"){{'
        f'el.style.display="block";'
        f'if(btn)btn.innerHTML="&#128214; Cerrar nota";'
        f'if(typeof gtag!=="undefined")gtag("event","expand_nota",{{"event_category":"nota_completa","event_label":"{slug}","value":1}});'
        f'}}}}\n'
        f'function toggleNota{post_id}(){{'
        f'var el=document.getElementById("nota-ampliada-{post_id}");'
        f'var btn=document.getElementById("btn-expandir-{post_id}");'
        f'var abre=el.style.display==="none";'
        f'el.style.display=abre?"block":"none";'
        f'if(btn)btn.innerHTML=abre?"&#128214; Cerrar nota":"&#128214; Leer nota completa";'
        f'if(typeof gtag!=="undefined")gtag("event",abre?"expand_nota":"collapse_nota",{{"event_category":"nota_completa","event_label":"{slug}","value":abre?1:0}});'
        f'}}\n'
        f'function irA{post_id}(id){{'
        f'expandirNota{post_id}();'
        f'setTimeout(function(){{var t=document.getElementById(id);if(t)t.scrollIntoView({{behavior:"smooth"}})}},50);'
        f'return false;}}'
    )

    result = (
        f'<p><strong>Lo que tenés que saber:</strong></p>\n'
        f'{bullets_html}\n\n'
        + (f'{toc_html}\n\n' if toc_html else '')
        + f'<style>#btn-expandir-{post_id}{{background:#1a73e8;color:#fff;padding:14px 20px;cursor:pointer;font-weight:700;font-size:1em;text-align:center;border-radius:6px;margin:8px 0;user-select:none;}}</style>\n'
        f'<div id="btn-expandir-{post_id}" onclick="toggleNota{post_id}()">'
        f'&#128214; Leer nota completa</div>\n\n'
        f'<div id="nota-ampliada-{post_id}" style="display:none;margin-top:8px;">\n'
        f'{body}\n'
        f'</div>\n\n'
        + (f'{fuente_html}\n\n' if fuente_html else '')
        + f'<script>\n{js}\n</script>'
    )
    return f'<!-- wp:html -->\n{result}\n<!-- /wp:html -->'


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

    hilo = data.get("hilo", 2)

    if not paragraphs:
        first_h2 = f"{kw}: lo que hay que saber" if kw else "Lo que hay que saber"
        if data.get("is_youtube"):
            ch = data.get("youtube_channel", "")
            fuente_html = (f'<p><em>Fuente: Video {ch + " " if ch else ""}— '
                           f'<a href="{data["source_url"]}" target="_blank" rel="noopener noreferrer">Ver en YouTube</a></em></p>')
        elif data.get("is_instagram"):
            ig = data.get("instagram_username", "")
            fuente_html = (f'<p><em>Fuente: {ig + " en " if ig else ""}Instagram — '
                           f'<a href="{data["source_url"]}" target="_blank" rel="noopener noreferrer">Ver en Instagram</a></em></p>')
        else:
            fuente_html = (f'<p><em>Fuente: <a href="{data["source_url"]}" '
                           f'target="_blank" rel="noopener noreferrer">Ver nota original</a></em></p>')
        return (
            f"<h2>{first_h2}</h2>\n"
            f'<p>{data["excerpt"]}</p>\n'
            + pyme_box(data["text"], data["excerpt"])
            + fuente_html
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
    h2_headings = []  # (content, anchor) para el ToC

    def _append_h2(label):
        nonlocal h2_index
        if not _is_valid_h2(label):
            return
        anchor = _slugify_h2(label)
        h2_headings.append({'content': label, 'anchor': anchor})
        parts.append(
            f'<!-- wp:heading {{"level":2,"anchor":"{anchor}"}} -->\n'
            f'<h2 class="wp-block-heading" id="{anchor}">{label}</h2>\n'
            f'<!-- /wp:heading -->'
        )
        h2_index += 1

    # Tag visual [OPINIÓN] / [ANÁLISIS] al inicio si es Hilo 3
    if hilo == 3:
        parts.append(
            '<!-- wp:html -->\n'
            '<p style="color:#c0392b;font-weight:700;letter-spacing:1px;'
            'font-size:12px;margin:0 0 8px;">[OPINIÓN / ANÁLISIS]</p>\n'
            '<!-- /wp:html -->'
        )

    for i, para in enumerate(paragraphs):
        # Resaltar números/datos
        para_html = number_pattern.sub(r'<strong>\1</strong>', para)

        if i == 0:
            # Lead en negrita
            parts.append(f'<!-- wp:paragraph -->\n<p><strong>{para_html}</strong></p>\n<!-- /wp:paragraph -->')

            # Si es YouTube, embebemos el video justo después del lead
            if data.get("is_youtube") and data.get("youtube_video_id"):
                yt_id = data["youtube_video_id"]
                parts.append(
                    f'<!-- wp:html -->\n'
                    f'<figure class="wp-block-embed is-type-video is-provider-youtube wp-block-embed-youtube aligncenter" '
                    f'style="margin:24px 0;">'
                    f'<div style="position:relative;padding-bottom:56.25%;height:0;overflow:hidden;">'
                    f'<iframe src="https://www.youtube.com/embed/{yt_id}" '
                    f'style="position:absolute;top:0;left:0;width:100%;height:100%;border:0;" '
                    f'title="Video de YouTube" '
                    f'allow="accelerometer; autoplay; clipboard-write; encrypted-media; gyroscope; picture-in-picture" '
                    f'allowfullscreen></iframe>'
                    f'</div></figure>\n'
                    f'<!-- /wp:html -->'
                )

            # Si es Instagram, embebemos el reel/post justo después del lead
            if data.get("is_instagram") and data.get("source_url"):
                ig_url = data["source_url"]
                ig_url_embed = ig_url.rstrip("/") + "/?utm_source=ig_embed&utm_campaign=loading"
                parts.append(
                    f'<!-- wp:html -->\n'
                    f'<blockquote class="instagram-media" data-instgrm-captioned '
                    f'data-instgrm-permalink="{ig_url_embed}" data-instgrm-version="14" '
                    f'style="background:#FFF;border:0;border-radius:3px;box-shadow:0 0 1px 0 rgba(0,0,0,.5),'
                    f'0 1px 10px 0 rgba(0,0,0,.15);margin:1px auto 24px;max-width:540px;min-width:326px;'
                    f'padding:0;width:calc(100% - 2px);">'
                    f'<a href="{ig_url_embed}" target="_blank">Ver publicación en Instagram</a>'
                    f'</blockquote>'
                    f'<script async src="//www.instagram.com/embed.js"></script>\n'
                    f'<!-- /wp:html -->'
                )

            if h2_index < len(h2_labels):
                _append_h2(h2_labels[h2_index])
        else:
            # H2 cada 3 párrafos
            if i % 3 == 0 and h2_index < len(h2_labels):
                _append_h2(h2_labels[h2_index])
            parts.append(f'<!-- wp:paragraph -->\n<p>{para_html}</p>\n<!-- /wp:paragraph -->')

    # Recuadro Pymes
    parts.append('<!-- wp:html -->\n' + pyme_box(data["text"], data["excerpt"]) + '<!-- /wp:html -->')

    # Fuente — formato especial por plataforma
    if data.get("is_youtube"):
        channel = data.get("youtube_channel", "")
        channel_html = f"del canal {channel} " if channel else ""
        parts.append(
            f'<!-- wp:paragraph -->\n'
            f'<p><em>Fuente: Video {channel_html}— '
            f'<a href="{data["source_url"]}" target="_blank" rel="noopener noreferrer">'
            f'Ver en YouTube</a></em></p>\n<!-- /wp:paragraph -->'
        )
    elif data.get("is_instagram"):
        ig_user = data.get("instagram_username", "")
        user_html = f"{ig_user} en " if ig_user else ""
        parts.append(
            f'<!-- wp:paragraph -->\n'
            f'<p><em>Fuente: {user_html}Instagram — '
            f'<a href="{data["source_url"]}" target="_blank" rel="noopener noreferrer">'
            f'Ver en Instagram</a></em></p>\n<!-- /wp:paragraph -->'
        )
    else:
        parts.append(
            f'<!-- wp:paragraph -->\n'
            f'<p><em>Fuente: <a href="{data["source_url"]}" '
            f'target="_blank" rel="noopener noreferrer">Ver nota original</a></em></p>\n'
            f'<!-- /wp:paragraph -->'
        )
    content = "\n".join(parts)
    toc_block = _build_rank_math_toc(h2_headings, hilo)
    if toc_block:
        content = toc_block + "\n" + content
    return content


# ── WordPress API ──────────────────────────────────────────────────────────────

def wp_auth():
    token = base64.b64encode(f"{WP_USER}:{WP_PASS}".encode()).decode()
    return {"Authorization": f"Basic {token}"}


# Ferozo bloquea los REST directos del VPS (responde 415 + HTML del WAF): upload de media,
# update/delete/create de posts y tags Y TAMBIÉN los GET (desde 2026-07, el WAF endureció y
# bloquea lectura además de escritura). TODA llamada a wp-json va por WARP (Cloudflare), mismo
# patrón que el harness (_WARP en publicador.py). Ver [[bot_wp_writes_warp]].
_WP_PROXIES = {"http": "socks5://127.0.0.1:40000", "https": "socks5://127.0.0.1:40000"}


def get_or_create_tags(names: list) -> list:
    ids = []
    h = {**wp_auth(), "Content-Type": "application/json"}
    for name in names:
        try:
            r = requests.post(
                f"{WP_URL}/wp-json/wp/v2/tags", headers=h,
                json={"name": name}, proxies=_WP_PROXIES, timeout=10
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


def upload_image(image_url: str, alt: str = "", watermark: bool = False) -> int | None:
    try:
        img = requests.get(image_url, headers=HEADERS_BROWSER, timeout=15)
        img.raise_for_status()
        ctype = img.headers.get("Content-Type", "image/jpeg").split(";")[0]
        ext = ctype.split("/")[-1]
        content_bytes = img.content
        if watermark:
            try:
                import sys as _sw
                _sw.path.insert(0, "/opt/me-harness")
                from agents import marca_agua as _ma
                content_bytes = _ma.aplicar_watermark(content_bytes)
                ctype, ext = "image/jpeg", "jpg"
            except Exception as _we:
                logger.warning(f"watermark upload_image: {_we}")

        h = {**wp_auth(), "Content-Disposition": f"attachment; filename=nota.{ext}",
             "Content-Type": ctype}
        r = requests.post(
            f"{WP_URL}/wp-json/wp/v2/media", headers=h, data=content_bytes,
            proxies=_WP_PROXIES, timeout=60
        )
        if r.ok:
            media_id = r.json()["id"]
            if alt:
                requests.post(
                    f"{WP_URL}/wp-json/wp/v2/media/{media_id}",
                    headers={**wp_auth(), "Content-Type": "application/json"},
                    json={"alt_text": alt, "caption": alt},
                    proxies=_WP_PROXIES, timeout=10,
                )
            return media_id
        logger.warning(f"Media {r.status_code}: {r.text[:200]}")
    except Exception as e:
        logger.warning(f"upload_image: {e}")
    return None


def upload_image_bytes(img_bytes: bytes, ext: str = "jpg", alt: str = "", watermark: bool = False) -> int | None:
    """Sube bytes de imagen directamente a la media library de WP."""
    try:
        if watermark:
            try:
                import sys as _sw
                _sw.path.insert(0, "/opt/me-harness")
                from agents import marca_agua as _ma
                img_bytes = _ma.aplicar_watermark(img_bytes)
                ext = "jpg"
            except Exception as _we:
                logger.warning(f"watermark upload_image_bytes: {_we}")
        ctype = f"image/{ext}"
        h = {**wp_auth(), "Content-Disposition": f"attachment; filename=nota.{ext}",
             "Content-Type": ctype}
        r = requests.post(f"{WP_URL}/wp-json/wp/v2/media", headers=h, data=img_bytes,
                          proxies=_WP_PROXIES, timeout=60)
        if r.ok:
            media_id = r.json()["id"]
            if alt:
                requests.post(
                    f"{WP_URL}/wp-json/wp/v2/media/{media_id}",
                    headers={**wp_auth(), "Content-Type": "application/json"},
                    json={"alt_text": alt, "caption": alt}, proxies=_WP_PROXIES, timeout=10,
                )
            return media_id
        logger.warning(f"upload_image_bytes {r.status_code}: {r.text[:200]}")
    except Exception as e:
        logger.warning(f"upload_image_bytes: {e}")
    return None


def _target_datetime_for_slot(slot: str):
    """
    Devuelve datetime ARG para el slot pedido.
    slot: 'morning' (8:00), 'noon' (12:00), 'evening' (18:00)
    Si la hora ya pasó hoy, va a mañana.
    """
    from datetime import datetime, timezone, timedelta
    tz_arg = timezone(timedelta(hours=-3))
    now = datetime.now(tz_arg)

    slots = {"morning": 8, "noon": 12, "evening": 18}
    hour = slots.get(slot, 8)

    target = now.replace(hour=hour, minute=0, second=0, microsecond=0)
    # Morning siempre es mañana (convención)
    if slot == "morning" or target <= now + timedelta(minutes=5):
        target = target + timedelta(days=1)
    return target


def find_scheduled_collision(target_dt, window_minutes: int = 5):
    """
    Busca en WP si ya hay otro post programado dentro de la ventana ±N minutos.
    Devuelve el datetime ajustado (offset +3 min por cada colisión, hasta 20 min).
    """
    from datetime import timedelta
    try:
        h = wp_auth()
        # Traer futuros ordenados por fecha
        r = requests.get(
            f"{WP_URL}/wp-json/wp/v2/posts"
            f"?status=future&orderby=date&order=asc&per_page=50",
            headers=h, timeout=10,
        )
        if r.status_code != 200:
            return target_dt
        scheduled = r.json()
    except Exception as e:
        logger.warning(f"find_scheduled_collision: {e}")
        return target_dt

    # Parsear las fechas de los posts programados (formato 'YYYY-MM-DDTHH:MM:SS')
    from datetime import datetime, timezone, timedelta
    tz_arg = timezone(timedelta(hours=-3))
    scheduled_dts = []
    for post in scheduled:
        date_str = post.get("date", "")
        if not date_str:
            continue
        try:
            dt = datetime.fromisoformat(date_str)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=tz_arg)
            scheduled_dts.append(dt)
        except ValueError:
            continue

    # Offset si hay colisión dentro de la ventana
    adjusted = target_dt
    for _ in range(10):  # máximo 10 offsets = +30 min
        collision = any(
            abs((s - adjusted).total_seconds()) < window_minutes * 60
            for s in scheduled_dts
        )
        if not collision:
            return adjusted
        adjusted = adjusted + timedelta(minutes=3)
    return adjusted


def _meta_safe(text: str, max_len: int = 160) -> str:
    """Sanitiza valor para wp_postmeta: elimina chars de 4 bytes (emojis) que rompen MySQL utf8."""
    return re.sub(r'[\U00010000-\U0010FFFF]', '', text or '')[:max_len].strip()


def _enqueue_to_harness(data: dict, image_id: int | None = None,
                        destacado: bool = False, scheduled_dt=None) -> int:
    """Inserta job en harness.db stage='publicacion'. El publicador lo procesa automáticamente."""
    import sqlite3 as _sq_h, json as _json_h
    from datetime import datetime as _dt_h
    _HARNESS_DB = "/opt/me-harness/harness.db"
    source = data.get("source_url") or data.get("source", "")
    content_json = {
        "title":             data.get("title", ""),
        "excerpt":           data.get("excerpt", "") or (data.get("text", "") or "")[:280],
        "content_html":      data.get("html") or data.get("content_html", ""),
        "bullets":           data.get("bullets", []),
        "h2_headings":       data.get("h2_headings", []),
        "image_url":         data.get("image_url"),
        "image_id_override": image_id,
        "category_ids":      data.get("category_ids") or detect_categories(
                                 data.get("title", ""), data.get("text", ""),
                                 data.get("excerpt", "")),
        "matched_kw":        data.get("matched_kw", []),
        # Estos jobs entran directo en stage='publicacion' y se saltean al redactor, así que
        # la keyword que ya tenga la nota tiene que VIAJAR: si no, el publicador escribe
        # rank_math_focus_keyword vacía y Google nunca la ve. Vacía no es drama —el publicador
        # la resuelve desde matched_kw—, pero lo que el bot ya sabe no se pierde más.
        "focus_keyword":     data.get("focus_keyword", ""),
        "formato":           "continua",
        "portada":           destacado,
        "source":            source,
        "source_url":        source,
    }
    instructions = f"programar:{scheduled_dt.isoformat()}" if scheduled_dt else None
    source_key = source or f"bot_manual_{data.get('title','')[:50]}"
    with _sq_h.connect(_HARNESS_DB) as conn:
        cur = conn.execute(
            "INSERT INTO jobs (stage, source_url, title, content_json, score, hilo, instructions, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            ("publicacion", source_key, data.get("title", ""),
             _json_h.dumps(content_json, ensure_ascii=False),
             0.0, 0, instructions, _dt_h.utcnow().isoformat())
        )
        return cur.lastrowid


def publish_post(data: dict, image_id: int | None, destacado: bool = False,
                 scheduled_date=None) -> str | None:
    """
    Publica o programa un post en WordPress.
    Si scheduled_date es un datetime (timezone-aware), el post se crea con
    status=future y date=scheduled_date (ISO en tz del sitio, Argentina UTC-3).
    """
    s_title  = get_title(data)
    s_kw     = focus_keyword(data["title"])
    s_desc   = get_excerpt(data, kw=s_kw)
    s_slug   = url_slug(data["title"])
    content  = format_content(data, kw=s_kw)

    cat_ids = data.get("_cat_override") or detect_categories(data["title"], data["text"], data["excerpt"])
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

    # Si hay scheduled_date → programar. Si no → publicar ya.
    if scheduled_date:
        status = "future"
        # WP espera ISO en la tz del sitio, formato "YYYY-MM-DDTHH:MM:SS"
        date_str = scheduled_date.strftime("%Y-%m-%dT%H:%M:%S")
    else:
        status = "publish"
        date_str = None

    payload = {
        "title":      s_title,
        "content":    content,
        "excerpt":    s_desc,
        "status":     status,
        "slug":       s_slug,
        "categories": cat_ids,
        "tags":       tag_ids,
        "meta": {
            "rank_math_title":            _meta_safe(s_title),
            "rank_math_description":      _meta_safe(s_desc, 320),
            "rank_math_focus_keyword":    s_kw,
            "rank_math_robots":           ["index", "follow"],
            "rank_math_og_content_image": data.get("image_url", ""),
        },
    }
    if date_str:
        payload["date"] = date_str
    if image_id:
        payload["featured_media"] = image_id

    h = {**wp_auth(), "Content-Type": "application/json"}
    global _LAST_WP_ERROR
    _LAST_WP_ERROR = ""
    r = requests.post(f"{WP_URL}/wp-json/wp/v2/posts", headers=h, json=payload, proxies=_WP_PROXIES, timeout=30)
    if r.status_code == 201:
        body = r.json()
        return {"link": body.get("link"), "id": body.get("id"), "content": content, "slug": s_slug}
    _LAST_WP_ERROR = f"HTTP {r.status_code}: {r.text[:300]}"
    logger.error(f"WP publish falló: {_LAST_WP_ERROR}")
    return None


def append_social_meta(post_id: int, content: str, tweet_id: str = "", tg_msg_id: int = 0, li_urn: str = "") -> bool:
    """
    Agrega un HTML comment al final del post con los IDs de Twitter, Telegram y LinkedIn
    para poder borrarlos después desde /editar.
    Format: <!-- mebot:tweet_id=X;tg_msg=Y;li_urn=Z -->
    """
    try:
        # Siempre fetchear contenido fresco de WP para no sobreescribir con versión cacheada/truncada
        try:
            h = wp_auth()
            r = requests.get(
                f"{WP_URL}/wp-json/wp/v2/posts/{post_id}?context=edit",
                headers=h, timeout=10
            )
            if r.status_code == 200:
                p = r.json()
                content = p.get("content", {}).get("raw", "") or p.get("content", {}).get("rendered", "") or content
        except Exception as fetch_err:
            logger.warning(f"append_social_meta: no se pudo fetchear contenido fresco ({fetch_err}), usando el cacheado")

        # Remover comentarios previos si existen
        clean_content = re.sub(r'<!--\s*mebot:[^>]*-->', '', content)
        meta_parts = []
        if tweet_id:
            meta_parts.append(f"tweet_id={tweet_id}")
        if tg_msg_id:
            meta_parts.append(f"tg_msg={tg_msg_id}")
        if li_urn:
            meta_parts.append(f"li_urn={li_urn}")
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
    "telegram":   ("social", "canal_empresarialarg"),
    "twitter":    ("social", "organico"),
    "whatsapp":   ("social", "compartir"),
    "newsletter": ("email",  "semanal"),
    "linkedin":   ("social", "organico"),
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
        hashtags = _build_hashtags(data)

    tracked_url = utm_url(wp_url, "twitter")
    tweet = f"{title}\n\n{tracked_url}\n\n{hashtags}"
    if len(tweet) > 280:
        max_title = 280 - len(tracked_url) - len(hashtags) - 6
        title = title[:max_title].rsplit(" ", 1)[0]
        tweet = f"{title}\n\n{tracked_url}\n\n{hashtags}"
    return tweet


_TWITTER_MIME_MAP = {
    b"\xff\xd8\xff": "image/jpeg",
    b"\x89PNG":      "image/png",
    b"GIF87":        "image/gif",
    b"GIF89":        "image/gif",
    b"RIFF":         "image/webp",   # magic bytes[0:4]; verificamos [8:12] == WEBP abajo
}

def _twitter_mime(img_bytes: bytes) -> str | None:
    """
    Detecta MIME por magic bytes.
    Retorna el tipo soportado por Twitter, o None si no está soportado.
    """
    for magic, mime in _TWITTER_MIME_MAP.items():
        if img_bytes[:len(magic)] == magic:
            # WebP tiene 'WEBP' en offset 8
            if mime == "image/webp" and img_bytes[8:12] != b"WEBP":
                return None
            return mime
    return None  # AVIF, SVG, TIFF, etc.


def upload_twitter_media(image_url: str, auth: OAuth1) -> str | None:
    """
    Sube una imagen a Twitter via API v1.1 media/upload y devuelve el media_id.
    Solo formatos soportados: JPEG, PNG, GIF, WebP.
    """
    try:
        # Descargar imagen
        img_resp = requests.get(image_url, headers=HEADERS_BROWSER, timeout=15)
        img_resp.raise_for_status()
        img_bytes = img_resp.content

        # Verificar tamaño
        if len(img_bytes) > 5 * 1024 * 1024:
            logger.warning(f"Imagen muy grande para Twitter ({len(img_bytes)} bytes), salteando")
            return None

        # Verificar formato soportado por Twitter (descarta AVIF, SVG, etc.)
        mime = _twitter_mime(img_bytes)
        if not mime:
            ct = img_resp.headers.get("Content-Type", "desconocido")
            logger.warning(f"Formato de imagen no soportado por Twitter: {ct} — salteando")
            return None

        # Subir a Twitter v1.1 media/upload
        r = requests.post(
            "https://upload.twitter.com/1.1/media/upload.json",
            files={"media": ("image", img_bytes, mime)},
            auth=auth,
            timeout=30,
        )
        if r.status_code == 200:
            media_id = r.json().get("media_id_string")
            logger.info(f"Twitter media uploaded: {media_id} ({mime})")
            return media_id
        logger.error(f"Twitter media upload {r.status_code}: {r.text[:300]}")
    except Exception as e:
        logger.error(f"Twitter media upload error: {e}")
    return None


# ── Sistema de alertas para servicios pagos ──────────────────────────────────

_ALERT_COOLDOWN: dict = {}  # {alert_key: timestamp_utc} para no spamear


def alert_admin_sync(message: str):
    """Manda alerta al ADMIN_CHAT_ID. Versión sync para usar desde funciones no-async."""
    if not ADMIN_CHAT_ID or not TELEGRAM_TOKEN:
        logger.warning(f"alert_admin sin destino: {message[:100]}")
        return
    try:
        requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
            json={
                "chat_id":    int(ADMIN_CHAT_ID),
                "text":       f"🚨 *ALERTA*\n\n{message}",
                "parse_mode": "Markdown",
            },
            timeout=10,
        )
    except Exception as e:
        logger.error(f"alert_admin_sync: {e}")


def alert_admin_throttled(key: str, message: str, cooldown_minutes: int = 60) -> bool:
    """Manda alerta solo si pasaron N minutos desde la última con la misma key."""
    from datetime import datetime
    now = datetime.utcnow().timestamp()
    last = _ALERT_COOLDOWN.get(key, 0)
    if now - last < cooldown_minutes * 60:
        return False
    _ALERT_COOLDOWN[key] = now
    alert_admin_sync(message)
    return True


def openai_post(endpoint: str, **kwargs):
    """
    Wrapper de requests.post para endpoints de OpenAI que detecta automáticamente
    errores de billing/quota/auth y alerta al admin. Devuelve el Response normal.
    """
    r = requests.post(endpoint, **kwargs)
    _check_openai_billing(r)
    return r


def _check_openai_billing(r: requests.Response, label: str = "OpenAI"):
    """Si la respuesta indica problema de billing/quota, alertar al admin."""
    try:
        body_lower = r.text.lower()
    except Exception:
        body_lower = ""

    if r.status_code == 429 and "insufficient_quota" in body_lower:
        alert_admin_throttled(
            "openai_quota",
            "⚠️ *OpenAI sin crédito*\n"
            "Recargar en https://platform.openai.com/account/billing\n\n"
            "Mientras no haya crédito:\n"
            "• Curador no genera sugerencias top 3\n"
            "• Reescritura de bajadas y resúmenes cae a heurística\n"
            "• Comando /hilo no funciona\n"
            "• Whisper fallback YouTube falla\n"
            "• Dedup semántico del curador deshabilitado",
        )
    elif r.status_code == 401:
        alert_admin_throttled(
            "openai_auth",
            "⚠️ *OpenAI auth falló*\n"
            "La API key fue revocada o no es válida.\n"
            "Revisar `OPENAI_API_KEY` en Railway.",
        )


def _alert_twitter_billing(error_msg: str, status_code: int):
    """Detecta errores de billing/rate limit en Twitter y alerta al admin."""
    if status_code == 402:
        alert_admin_throttled(
            "twitter_402",
            "⚠️ *Twitter sin crédito*\n"
            "La cuenta de X (vinculada a xAI) se quedó sin créditos pre-pagos.\n\n"
            "Recargar en: https://console.x.ai → Facturación → Créditos\n"
            "(o developer.x.com → Facturación → Créditos)\n\n"
            f"Detalle: `{error_msg[:200]}`",
        )
    elif status_code == 429:
        alert_admin_throttled(
            "twitter_429",
            "⚠️ *Twitter rate limit alcanzado*\n"
            "Esperá 15-30 min antes de seguir tweeteando.",
            cooldown_minutes=30,
        )


def _increment_tweet_count():
    """Incrementa el counter mensual de tweets emitidos (info, no quota)."""
    from datetime import datetime
    fb = _load_feedback()
    key = "tweets_count_" + datetime.now().strftime("%Y%m")
    fb[key] = fb.get(key, 0) + 1
    _save_feedback(fb)
    # Cuenta xAI: créditos pre-pagos, no hay threshold fijo conocido.
    # El aviso real se dispara con el 402 cuando se acaban los créditos.


# Variable global para que el caller pueda leer el último error de Twitter
_LAST_TWITTER_ERROR: str = ""


def get_last_twitter_error() -> str:
    return _LAST_TWITTER_ERROR


def _do_tweet(payload: dict, auth: OAuth1) -> tuple[str | None, str]:
    """Hace la POST a /2/tweets. Devuelve (tweet_url, error_msg)."""
    try:
        r = requests.post(
            "https://api.twitter.com/2/tweets",
            json=payload, auth=auth, timeout=20,
        )
        logger.info(f"Twitter POST /2/tweets → {r.status_code}: {r.text[:300]}")
        if r.status_code == 201:
            tweet_id = r.json()["data"]["id"]
            return f"https://twitter.com/i/web/status/{tweet_id}", ""
        # Parsear error de Twitter
        try:
            err = r.json()
            detail = err.get("detail") or err.get("title") or err.get("error", "")
            if isinstance(err.get("errors"), list) and err["errors"]:
                detail = err["errors"][0].get("message", detail)
        except Exception:
            detail = r.text[:200]
        # Alertar al admin de errores de billing/rate-limit
        _alert_twitter_billing(detail or r.text[:200], r.status_code)
        # Mensajes amigables para errores típicos
        if r.status_code == 402:
            detail = (
                "Cuenta X sin créditos pre-pagos (xAI Console). "
                "Recargar en console.x.ai → Facturación → Créditos. "
                "Detalle: " + detail
            )
        elif r.status_code == 429:
            detail = "Rate limit de X. Esperá 15 min y reintentá. " + detail
        elif r.status_code == 403:
            detail = "Permiso denegado por X (puede ser duplicate / app no configurada). " + detail
        return None, f"HTTP {r.status_code}: {detail}"
    except Exception as e:
        return None, f"{type(e).__name__}: {e}"


def post_tweet(data: dict, wp_url: str, hashtags_override: str = None) -> str | None:
    global _LAST_TWITTER_ERROR
    _LAST_TWITTER_ERROR = ""

    try:
        tweet_text = build_tweet(data, wp_url, hashtags_override=hashtags_override)
        auth = OAuth1(TWITTER_API_KEY, TWITTER_API_SECRET, TWITTER_TOKEN, TWITTER_SECRET)

        payload = {"text": tweet_text}

        # Intentar subir imagen para preview
        image_url = data.get("image_url", "")
        media_id = None
        if image_url:
            media_id = upload_twitter_media(image_url, auth)
            if media_id:
                payload["media"] = {"media_ids": [media_id]}

        # 1er intento: con imagen si la conseguimos subir
        url, err = _do_tweet(payload, auth)
        if url:
            try:
                _increment_tweet_count()
            except Exception:
                pass
            return url

        # Si falló y teníamos media, retry sin media (puede ser que upload v1.1
        # esté deprecado o el media_id no es válido)
        if media_id:
            logger.warning(f"Tweet con media falló ({err}), reintentando sin imagen…")
            payload.pop("media", None)
            url2, err2 = _do_tweet(payload, auth)
            if url2:
                try:
                    _increment_tweet_count()
                except Exception:
                    pass
                _LAST_TWITTER_ERROR = f"⚠️ Tuit OK pero sin imagen (media falló: {err})"
                return url2
            err = err2

        _LAST_TWITTER_ERROR = err
        logger.error(f"Twitter post_tweet falló: {err}")
        return None
    except Exception as e:
        _LAST_TWITTER_ERROR = f"{type(e).__name__}: {e}"
        logger.error(f"post_tweet exception: {e}")
        return None


def _get_linkedin_author_urn() -> str:
    """Devuelve el URN del author: persona si LINKEDIN_MEMBER_ID está seteado, organización si no."""
    global _LINKEDIN_MEMBER_URN_CACHE
    if LINKEDIN_MEMBER_ID:
        return f"urn:li:person:{LINKEDIN_MEMBER_ID}"
    if _LINKEDIN_MEMBER_URN_CACHE:
        return _LINKEDIN_MEMBER_URN_CACHE
    # Auto-detección vía OIDC userinfo (requiere scope openid+profile)
    if LINKEDIN_TOKEN:
        try:
            r = requests.get(
                "https://api.linkedin.com/v2/userinfo",
                headers={"Authorization": f"Bearer {LINKEDIN_TOKEN}"},
                timeout=8,
            )
            if r.status_code == 200:
                sub = r.json().get("sub", "")
                if sub:
                    _LINKEDIN_MEMBER_URN_CACHE = f"urn:li:person:{sub}"
                    logger.info(f"LinkedIn member URN auto-detectado: {_LINKEDIN_MEMBER_URN_CACHE}")
                    return _LINKEDIN_MEMBER_URN_CACHE
        except Exception:
            pass
    return f"urn:li:organization:{LINKEDIN_ORG_ID}"


def post_linkedin(data: dict, wp_url: str) -> str | None:
    """Publica en LinkedIn. Usa perfil personal si LINKEDIN_MEMBER_ID está seteado, organización si no."""
    global _LAST_LINKEDIN_ERROR, _LAST_LINKEDIN_URN
    _LAST_LINKEDIN_ERROR = ""
    _LAST_LINKEDIN_URN = ""
    if not LINKEDIN_TOKEN:
        _LAST_LINKEDIN_ERROR = "LINKEDIN_TOKEN no configurado"
        return None
    try:
        tracked_url = utm_url(wp_url, "linkedin")
        title = (data.get("title") or "").strip()
        excerpt = (data.get("excerpt") or data.get("rewritten_excerpt") or "").strip()[:300]
        commentary = f"{title}\n\n{excerpt}\n\n🔗 {tracked_url}" if excerpt else f"{title}\n\n🔗 {tracked_url}"
        author_urn = _get_linkedin_author_urn()

        h = {
            "Authorization": f"Bearer {LINKEDIN_TOKEN}",
            "Content-Type": "application/json",
            "LinkedIn-Version": "202503",
        }
        payload = {
            "author": author_urn,
            "commentary": commentary,
            "visibility": "PUBLIC",
            "distribution": {"feedDistribution": "MAIN_FEED"},
            "content": {
                "article": {
                    "source": tracked_url,
                    "title": title,
                    "description": excerpt,
                }
            },
            "lifecycleState": "PUBLISHED",
            "isReshareDisabledByAuthor": False,
        }
        r = requests.post("https://api.linkedin.com/rest/posts", headers=h, json=payload, timeout=15)
        if r.status_code in (200, 201):
            post_urn = r.headers.get("x-linkedin-id") or r.json().get("id", "")
            _LAST_LINKEDIN_URN = post_urn
            post_url_li = f"https://www.linkedin.com/feed/update/{post_urn}/" if post_urn else tracked_url
            logger.info(f"LinkedIn post OK ({author_urn}): {post_url_li}")
            return post_url_li
        _LAST_LINKEDIN_ERROR = f"HTTP {r.status_code}: {r.text[:200]}"
        logger.error(f"LinkedIn post_linkedin falló: {_LAST_LINKEDIN_ERROR}")
        return None
    except Exception as e:
        _LAST_LINKEDIN_ERROR = f"{type(e).__name__}: {e}"
        logger.error(f"post_linkedin exception: {e}")
        return None


def _get_org_token() -> str:
    """Token de la PÁGINA (org). Prioriza el estado que mantiene el harness (auto-refresh)."""
    try:
        import json as _json
        with open(LINKEDIN_TOKEN_STATE, encoding="utf-8") as f:
            tok = (_json.load(f) or {}).get("access_token", "")
            if tok:
                return tok
    except Exception:
        pass
    return LINKEDIN_ORG_TOKEN


def post_linkedin_org(data: dict, wp_url: str) -> str | None:
    """Publica en la PÁGINA de LinkedIn (organización). Independiente del posteo personal."""
    tok = _get_org_token()
    if not tok or not LINKEDIN_ORG_ID:
        logger.warning("post_linkedin_org: sin token org u org_id")
        return None
    try:
        tracked_url = utm_url(wp_url, "linkedin")
        title = (data.get("title") or "").strip()
        excerpt = (data.get("excerpt") or data.get("rewritten_excerpt") or "").strip()[:300]
        commentary = f"{title}\n\n{excerpt}\n\n🔗 {tracked_url}" if excerpt else f"{title}\n\n🔗 {tracked_url}"
        h = {
            "Authorization": f"Bearer {tok}",
            "Content-Type": "application/json",
            "LinkedIn-Version": "202503",
        }
        payload = {
            "author": f"urn:li:organization:{LINKEDIN_ORG_ID}",
            "commentary": commentary,
            "visibility": "PUBLIC",
            "distribution": {"feedDistribution": "MAIN_FEED"},
            "content": {"article": {"source": tracked_url, "title": title, "description": excerpt}},
            "lifecycleState": "PUBLISHED",
            "isReshareDisabledByAuthor": False,
        }
        r = requests.post("https://api.linkedin.com/rest/posts", headers=h, json=payload, timeout=15)
        if r.status_code in (200, 201):
            post_urn = r.headers.get("x-linkedin-id") or r.json().get("id", "")
            post_url_li = f"https://www.linkedin.com/feed/update/{post_urn}/" if post_urn else tracked_url
            logger.info(f"LinkedIn ORG post OK: {post_url_li}")
            return post_url_li
        logger.error(f"post_linkedin_org falló HTTP {r.status_code}: {r.text[:200]}")
        return None
    except Exception as e:
        logger.error(f"post_linkedin_org exception: {e}")
        return None


def delete_linkedin_post(urn: str) -> bool:
    """Borra un post de LinkedIn por su URN (ej: urn:li:share:xxx)."""
    if not LINKEDIN_TOKEN or not urn:
        return False
    try:
        encoded = requests.utils.quote(urn, safe="")
        h = {"Authorization": f"Bearer {LINKEDIN_TOKEN}", "LinkedIn-Version": "202503"}
        r = requests.delete(f"https://api.linkedin.com/rest/posts/{encoded}", headers=h, timeout=15)
        if r.status_code in (200, 204):
            logger.info(f"LinkedIn delete OK: {urn}")
            return True
        logger.error(f"LinkedIn delete falló {r.status_code}: {r.text[:200]}")
        return False
    except Exception as e:
        logger.error(f"delete_linkedin_post: {e}")
        return False


def _fit_tweet(text: str, limit: int = 280) -> str:
    """Recorta un tweet al límite respetando límite de palabra."""
    if len(text) <= limit:
        return text
    cut = text[:limit - 1]
    boundary = cut.rfind(" ")
    return (cut[:boundary] if boundary > limit * 0.6 else cut).rstrip() + "…"


def generate_thread_with_gpt(title: str, body_text: str, wp_url: str, hashtags: str = "") -> list[str]:
    """
    Genera un hilo de Twitter de 3-5 tweets usando gpt-4o-mini.
    Primer tweet lleva URL de la nota. Último lleva hashtags.
    Devuelve lista de strings (ya recortados a 280).
    """
    if not OPENAI_API_KEY:
        raise RuntimeError(
            "OPENAI_API_KEY no configurada. No puedo generar hilos sin GPT."
        )

    prompt = (
        "Sos el community manager de @MundoEmpresarial_AR, medio digital de noticias "
        "económicas argentinas para pymes. Te paso una nota ya publicada y tu tarea es "
        "convertirla en un HILO de Twitter/X de 3 a 5 tweets.\n\n"
        "REGLAS OBLIGATORIAS:\n"
        "1. Cada tweet máximo 240 caracteres (dejamos margen para URLs y numeración).\n"
        "2. Primer tweet: gancho + dato fuerte de la nota. Funciona como titular potente.\n"
        "3. Tweets del medio: puntos clave, datos concretos, cifras, citas importantes con "
        "atribución.\n"
        "4. Último tweet: cierre con reflexión o llamado a leer más. NO URL acá.\n"
        "5. Numerá con (1/n), (2/n), etc. al FINAL de cada tweet.\n"
        "6. Tono directo, informativo. Español rioplatense (vos, ustedes), sin clickbait.\n"
        "7. Máximo 2 emojis relevantes por tweet. No abusar.\n"
        "8. NO pongas hashtags — se agregan aparte.\n"
        "9. Comillas tipográficas \"…\", nunca rectas.\n"
        "10. NO empieces con 🧵 ni con Abro hilo ni similares.\n"
        "11. Separá los tweets con una línea '---' sola.\n\n"
        f"Título de la nota: {title}\n\n"
        "Contenido de la nota:\n"
        "---\n"
        f"{body_text[:5000]}\n"
        "---\n\n"
        "Devolvé SOLO los tweets separados por '---' (cada uno en su propio bloque). "
        "Sin explicaciones ni encabezados."
    )

    try:
        r = openai_post(
            "https://api.openai.com/v1/chat/completions",
            headers={
                "Authorization": f"Bearer {OPENAI_API_KEY}",
                "Content-Type": "application/json",
            },
            json={
                "model": "gpt-4o-mini",
                "messages": [{"role": "user", "content": prompt}],
                "temperature": 0.45,
            },
            timeout=60,
        )
    except Exception as e:
        raise RuntimeError(f"GPT API error: {e}")

    if r.status_code != 200:
        raise RuntimeError(f"GPT API {r.status_code}: {r.text[:200]}")

    content = r.json()["choices"][0]["message"]["content"].strip()
    raw = [t.strip() for t in re.split(r'\n\s*-{3,}\s*\n', content) if t.strip()]

    if not raw:
        # Fallback: split por líneas en blanco si GPT no usó ---
        raw = [t.strip() for t in re.split(r'\n{2,}', content) if t.strip()]

    if not raw:
        raise RuntimeError("GPT no devolvió tweets parseables")

    tweets = raw[:5]  # Hard cap 5 tweets

    # Agregar URL al primer tweet
    if tweets:
        tweets[0] = _fit_tweet(tweets[0] + "\n\n" + wp_url, 280)

    # Agregar hashtags al último tweet si hay espacio
    if hashtags and len(tweets) > 1:
        last_with_ht = tweets[-1] + "\n\n" + hashtags
        tweets[-1] = _fit_tweet(last_with_ht, 280)

    # Asegurar que ninguno pase 280
    tweets = [_fit_tweet(t, 280) for t in tweets]
    return tweets


def post_twitter_thread(tweets: list[str], image_url: str = "") -> list[str]:
    """
    Publica una cadena de tweets como hilo. El primero lleva la imagen.
    Devuelve lista de URLs (en orden). Si alguno falla, corta el hilo ahí.
    """
    auth = OAuth1(TWITTER_API_KEY, TWITTER_API_SECRET, TWITTER_TOKEN, TWITTER_SECRET)
    urls = []
    prev_id = None

    media_id = None
    if image_url:
        try:
            media_id = upload_twitter_media(image_url, auth)
        except Exception as e:
            logger.warning(f"post_twitter_thread: upload_media falló: {e}")

    for i, text in enumerate(tweets):
        payload = {"text": text}
        if prev_id:
            payload["reply"] = {"in_reply_to_tweet_id": prev_id}
        if i == 0 and media_id:
            payload["media"] = {"media_ids": [media_id]}
        try:
            r = requests.post(
                "https://api.twitter.com/2/tweets",
                json=payload, auth=auth, timeout=20,
            )
            if r.status_code != 201:
                logger.error(f"Thread tweet {i+1}/{len(tweets)} falló: {r.status_code} {r.text[:200]}")
                break
            tweet_id = r.json()["data"]["id"]
            urls.append(f"https://twitter.com/i/web/status/{tweet_id}")
            prev_id = tweet_id
        except Exception as e:
            logger.error(f"Thread tweet {i+1} excepción: {e}")
            break

    return urls


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
    """
    Filtra líneas de ruido del texto scrapeado.
    IMPORTANTE: el filtro de noise_fragments solo se aplica a líneas CORTAS
    (< 200 chars). Una línea larga que menciona 'twitter' o 'seguinos' de
    pasada es contenido legítimo, no ruido del footer/sidebar.
    """
    if not raw:
        return ""

    # Si todo el texto viene en una sola línea (ej. JSON-LD articleBody),
    # partirlo en oraciones antes de filtrar, así el filtro de noise no
    # descarta párrafos completos por una palabra suelta.
    if "\n" not in raw and len(raw) > 500:
        sentences = re.split(r'(?<=[.!?])\s+(?=[A-ZÁÉÍÓÚÑ¿¡])', raw)
        raw = "\n".join(sentences)

    clean = []
    for line in raw.split("\n"):
        s = line.strip()
        if not s:
            continue
        low = s.lower()

        # Filtrar noise SOLO en líneas cortas (típicamente CTAs, footer, share)
        if len(s) < 200 and any(frag in low for frag in NOISE_FRAGMENTS):
            continue

        if any(c in s for c in ("Ã", "Â", "â€", "Ã©", "Ã¡", "Ã³", "Ã±")):
            continue

        if len(s) < 25 and s[-1] not in ".?!:":
            continue

        clean.append(s)

    return "\n".join(clean)


# ── Scraper ────────────────────────────────────────────────────────────────────

def _fix_encoding(resp: requests.Response) -> str:
    """
    Decodifica el body en el encoding más probable.
    Muchos sitios argentinos son 'mixed encoding': 99% UTF-8 pero con uno o dos
    bytes sueltos (0x95, 0x92, etc) de Windows-1252. Si caemos a latin-1 completo,
    los bytes UTF-8 válidos se re-interpretan mal (ej. 'Ã³' en vez de 'ó').
    Estrategia:
    1. UTF-8 estricto si funciona limpio.
    2. Si falla, probar con 'errors=replace' → reemplaza inválidos con � pero
       preserva bien los caracteres UTF-8 correctos. Si los � son pocos (<0.1%),
       usamos esto.
    3. Fallback final a latin-1 si todo lo anterior falla.
    """
    raw = resp.content
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        pass
    # Mixed-encoding: preservar UTF-8 válido y reemplazar los pocos bytes rotos
    replaced = raw.decode("utf-8", errors="replace")
    total = len(replaced)
    bad = replaced.count("�")
    if total > 0 and bad / total < 0.01:  # <1% de bytes rotos → preferir UTF-8
        return replaced
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
                    (d for d in data if isinstance(d, dict) and d.get("@type") in
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


def _clean_tweet_text(t: str) -> str:
    """Saca los https://t.co/... del texto del tweet."""
    return re.sub(r'https://t\.co/\S+', '', (t or "")).strip()


def scrape_tweet(url: str) -> dict | None:
    """Trae el contenido de un tweet SIN API paga vía fxtwitter, con fallback a vxtwitter.
    Devuelve {id, url, text, author_name, handle, date, media_urls} o None."""
    m = re.search(r'(?:twitter\.com|x\.com)/([^/]+)/status/(\d+)', url)
    if not m:
        return None
    user, tid = m.group(1), m.group(2)
    ua = {"User-Agent": "Mozilla/5.0"}
    # 1) fxtwitter (probado OK)
    try:
        r = requests.get(f"https://api.fxtwitter.com/{user}/status/{tid}", headers=ua, timeout=15)
        if r.status_code == 200:
            tw = (r.json() or {}).get("tweet") or {}
            if tw:
                media = [p["url"] for p in ((tw.get("media") or {}).get("photos") or []) if p.get("url")]
                a = tw.get("author") or {}
                return {"id": tid, "url": tw.get("url") or url, "text": _clean_tweet_text(tw.get("text")),
                        "author_name": a.get("name") or user, "handle": a.get("screen_name") or user,
                        "date": tw.get("created_at") or "", "media_urls": media}
    except Exception as e:
        logger.warning(f"scrape_tweet fxtwitter: {e}")
    # 2) fallback vxtwitter (mismo estilo, sin token)
    try:
        r = requests.get(f"https://api.vxtwitter.com/{user}/status/{tid}", headers=ua, timeout=15)
        if r.status_code == 200:
            j = r.json() or {}
            media = [u for u in (j.get("mediaURLs") or []) if u and re.search(r'\.(jpg|jpeg|png|webp)', u, re.I)]
            return {"id": tid, "url": url, "text": _clean_tweet_text(j.get("text")),
                    "author_name": j.get("user_name") or user, "handle": j.get("user_screen_name") or user,
                    "date": j.get("date") or "", "media_urls": media}
    except Exception as e:
        logger.warning(f"scrape_tweet vxtwitter: {e}")
    return None


def _describe_tweet_image(image_url: str) -> str:
    """Lee la imagen (gráfico/foto) de un tweet con GPT-4o visión y extrae su info como texto,
    para nutrir la nota. Devuelve '' si no hay key o falla."""
    if not OPENAI_API_KEY or not image_url:
        return ""
    try:
        prompt = ("Describí con precisión qué muestra esta imagen, para usarla como FUENTE de una nota "
                  "periodística. Si es un gráfico o tabla: título, qué mide cada eje/serie, los VALORES "
                  "numéricos clave, el período y la fuente citada. Si es una foto: qué se ve y todo el texto "
                  "visible. NO interpretes ni inventes: reportá SOLO lo que está en la imagen. En español.")
        r = openai_post(
            "https://api.openai.com/v1/chat/completions",
            headers={"Authorization": f"Bearer {OPENAI_API_KEY}", "Content-Type": "application/json"},
            json={"model": "gpt-4o", "max_tokens": 700, "temperature": 0,
                  "messages": [{"role": "user", "content": [
                      {"type": "text", "text": prompt},
                      {"type": "image_url", "image_url": {"url": image_url}}]}]},
            timeout=90)
        if r.status_code == 200:
            return (r.json()["choices"][0]["message"]["content"] or "").strip()
        logger.warning(f"_describe_tweet_image {r.status_code}: {r.text[:200]}")
    except Exception as e:
        logger.warning(f"_describe_tweet_image: {e}")
    return ""


def youtube_video_id(url: str) -> str | None:
    m = _YOUTUBE_ID_RE.search(url or "")
    return m.group(1) if m else None


def _parse_json3(content: str) -> str:
    """Extrae texto plano del formato json3 de YouTube (captions API)."""
    import json as _json
    try:
        data = _json.loads(content)
    except Exception:
        return ""
    texts = []
    for event in data.get("events", []):
        for seg in event.get("segs", []):
            t = seg.get("utf8", "").strip()
            if t and t != "\n":
                texts.append(t)
    dedup = []
    for t in texts:
        if not dedup or t != dedup[-1]:
            dedup.append(t)
    return " ".join(dedup)


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
    logger.info(f"Whisper: iniciando (key configurada, len={len(OPENAI_API_KEY)})")

    try:
        import yt_dlp
    except ImportError:
        logger.error("Whisper: yt-dlp no instalado")
        return ""

    import tempfile
    import glob
    import shutil

    # Verificar que ffmpeg esté disponible
    has_ffmpeg = shutil.which("ffmpeg") is not None
    logger.info(f"Whisper: ffmpeg disponible={has_ffmpeg}")

    video_url = f"https://www.youtube.com/watch?v={video_id}"

    with tempfile.TemporaryDirectory() as tmpdir:
        audio_opts = {
            "format": "bestaudio/best",
            "outtmpl": os.path.join(tmpdir, "audio.%(ext)s"),
            "quiet": True,
            "no_warnings": True,
            "proxy": YOUTUBE_PROXY,
            "http_headers": {
                "User-Agent": HEADERS_BROWSER["User-Agent"],
                "Accept-Language": "es-AR,es;q=0.9,en;q=0.8",
            },
        }
        if YOUTUBE_COOKIES:
            audio_opts["cookiefile"] = YOUTUBE_COOKIES
        if has_ffmpeg:
            audio_opts["postprocessors"] = [{
                "key": "FFmpegExtractAudio",
                "preferredcodec": "mp3",
                "preferredquality": "64",
            }]

        download_ok = False
        for attempt_opts in (audio_opts, {**audio_opts, "extractor_args": {"youtube": {"player_client": ["android_vr"]}}}):
            try:
                with yt_dlp.YoutubeDL(attempt_opts) as ydl:
                    ydl.download([video_url])
                download_ok = True
                break
            except Exception as e:
                logger.warning(
                    f"Whisper yt-dlp intento falló: {type(e).__name__}: {str(e)[:200]}"
                )
                continue
        if not download_ok:
            logger.error("Whisper: todos los intentos de download fallaron")
            return ""

        files = glob.glob(os.path.join(tmpdir, "audio.*"))
        logger.info(f"Whisper: archivos descargados: {[os.path.basename(f) for f in files]}")
        if not files:
            logger.error("Whisper: no se descargó el audio")
            return ""

        audio_path = files[0]
        size_mb = os.path.getsize(audio_path) / (1024 * 1024)
        logger.info(f"Whisper: audio {audio_path} ({size_mb:.2f} MB)")

        if size_mb > 24.5:
            logger.error(f"Whisper: audio {size_mb:.1f} MB excede 25 MB, necesita split (no implementado)")
            return ""

        try:
            with open(audio_path, "rb") as f:
                files_upload = {"file": (os.path.basename(audio_path), f, "audio/mpeg")}
                data = {
                    "model": "whisper-1",
                    "language": "es",
                    "response_format": "text",
                }
                r = openai_post(
                    "https://api.openai.com/v1/audio/transcriptions",
                    headers={"Authorization": f"Bearer {OPENAI_API_KEY}"},
                    data=data,
                    files=files_upload,
                    timeout=180,
                )
            logger.info(f"Whisper API response: HTTP {r.status_code}, {len(r.text)} bytes")
            if r.status_code == 200:
                text = r.text.strip()
                logger.info(f"Whisper OK: {len(text)} chars")
                return text
            logger.error(f"Whisper API {r.status_code}: {r.text[:400]}")
        except Exception as e:
            logger.error(f"Whisper request falló: {type(e).__name__}: {e}")
    return ""


def _transcript_via_html(video_id: str) -> str:
    """
    Extrae captions directo del HTML de YouTube (ytInitialPlayerResponse).
    No requiere cookies, JS runtime ni yt-dlp. Funciona para videos públicos.
    """
    import json as _json
    from xml.etree import ElementTree as ET

    yt_url = f"https://www.youtube.com/watch?v={video_id}"
    hdrs = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
        "Accept-Language": "es-AR,es;q=0.9,en;q=0.8",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    }
    try:
        r = requests.get(yt_url, headers=hdrs, timeout=15)
        html = r.text
    except Exception as e:
        logger.warning(f"_transcript_via_html: fetch falló: {e}")
        return ""

    # Extraer el JSON embebido con raw_decode para no rompernos con regex
    player_data = None
    for marker in ("ytInitialPlayerResponse = ", "ytInitialPlayerResponse="):
        idx = html.find(marker)
        if idx == -1:
            continue
        start = html.find("{", idx)
        if start == -1:
            continue
        try:
            player_data, _ = _json.JSONDecoder().raw_decode(html[start:])
            break
        except Exception:
            continue

    if not player_data:
        logger.warning(f"_transcript_via_html: ytInitialPlayerResponse no encontrado para {video_id}")
        return ""

    tracks = (
        player_data.get("captions", {})
        .get("playerCaptionsTracklistRenderer", {})
        .get("captionTracks", [])
    )
    if not tracks:
        logger.info(f"_transcript_via_html: sin captionTracks para {video_id}")
        return ""

    # Preferir español, luego cualquier idioma
    es_track = next((t for t in tracks if t.get("languageCode", "").startswith("es")), None)
    en_track = next((t for t in tracks if t.get("languageCode", "").startswith("en")), None)
    track = es_track or en_track or tracks[0]
    base_url = track.get("baseUrl", "")
    if not base_url:
        return ""

    try:
        sep = "&" if "?" in base_url else "?"
        # Intentar VTT
        r2 = requests.get(base_url + sep + "fmt=vtt", headers=hdrs, timeout=15)
        if r2.status_code == 200 and r2.text:
            text = _parse_vtt(r2.text)
            if text:
                logger.info(f"_transcript_via_html: OK via VTT ({len(text)} chars)")
                return text
        # Fallback: XML nativo
        r3 = requests.get(base_url, headers=hdrs, timeout=15)
        if r3.status_code == 200:
            root = ET.fromstring(r3.text)
            parts = [t.text for t in root.iter("text") if t.text]
            combined = " ".join(parts)
            if combined.strip():
                logger.info(f"_transcript_via_html: OK via XML ({len(combined)} chars)")
                return combined
    except Exception as e:
        logger.warning(f"_transcript_via_html: cap fetch falló: {e}")

    return ""


def _transcript_via_ytdlp(video_id: str, min_len: int = 200) -> str:
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
    opts = {
        "skip_download": True,
        "quiet": True,
        "no_warnings": True,
        "proxy": YOUTUBE_PROXY,
        "http_headers": {
            "User-Agent": HEADERS_BROWSER["User-Agent"],
            "Accept-Language": "es-AR,es;q=0.9,en;q=0.8",
        },
    }
    if YOUTUBE_COOKIES:
        opts["cookiefile"] = YOUTUBE_COOKIES

    info = None
    for attempt_opts in (opts, {**opts, "extractor_args": {"youtube": {"player_client": ["android_vr"]}}}):
        try:
            with yt_dlp.YoutubeDL(attempt_opts) as ydl:
                info = ydl.extract_info(video_url, download=False)
            break
        except Exception as e:
            logger.warning(
                f"yt-dlp extract_info intento falló: {type(e).__name__}: {str(e)[:200]}"
            )
            continue
    if info is None:
        logger.error("yt-dlp: todos los intentos de extract_info fallaron")
        return ""

    manual = info.get("subtitles") or {}
    auto = info.get("automatic_captions") or {}
    logger.info(
        f"yt-dlp info OK: manual_langs={list(manual.keys())[:5]}, "
        f"auto_langs_total={len(auto)}, es_auto={'es' in auto or 'es-orig' in auto}"
    )

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

    logger.info(f"yt-dlp candidates: {[(lang, src) for _, lang, src in candidates[:5]]}")

    for fmts, lang, source in candidates:
        # Preferir json3 (más confiable desde IPs de servidor); luego vtt/ttml/srt
        fmt = None
        fmt_ext = None
        for ext in ("json3", "vtt", "ttml", "srt"):
            fmt = next((f for f in fmts if f.get("ext") == ext), None)
            if fmt:
                fmt_ext = ext
                break
        if not fmt:
            logger.info(f"yt-dlp {lang}/{source}: sin formatos de texto disponibles")
            continue
        try:
            r = requests.get(
                fmt["url"], timeout=15,
                headers={"User-Agent": HEADERS_BROWSER["User-Agent"]},
            )
            logger.info(f"yt-dlp fetch {lang}/{source}/{fmt_ext}: HTTP {r.status_code}, {len(r.text)} bytes")
            if r.status_code != 200 or not r.text:
                continue
            text = _parse_json3(r.text) if fmt_ext == "json3" else _parse_vtt(r.text)
            if text and len(text) >= min_len:
                logger.info(f"yt-dlp transcript OK via {source} ({lang}/{fmt_ext}): {len(text)} chars")
                if source.endswith("-en"):
                    text += "\n[Nota: transcripción en inglés, revisar traducción]"
                return text
            logger.info(f"yt-dlp {lang}/{source}/{fmt_ext}: texto muy corto ({len(text)} chars)")
        except Exception as e:
            logger.warning(f"fetch {fmt_ext} {lang}/{source}: {type(e).__name__}: {e}")
            continue
    logger.warning("yt-dlp: ningún candidato devolvió texto válido")
    # Fallback interno: descripción del video (suele tener el contenido clave)
    desc = (info.get("description") or "").strip()
    if desc and len(desc) >= min_len:
        logger.info(f"yt-dlp: usando descripción del video ({len(desc)} chars) como fallback")
        return f"[Descripción del canal]\n{desc}"
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

    # Shorts tienen mucho menos texto — umbral más bajo
    is_short = "/shorts/" in url.lower()
    min_len = 30 if is_short else 200

    # Transcripción: 1) youtube-transcript-api  2) yt-dlp fallback  3) Whisper
    transcript_text = ""
    try:
        from youtube_transcript_api import YouTubeTranscriptApi
        try:
            # API 1.x: cookies= acepta path a Netscape cookies file (None = sin cookies)
            _yapi = YouTubeTranscriptApi(
                cookies=YOUTUBE_COOKIES,
                proxies={"https": YOUTUBE_PROXY, "http": YOUTUBE_PROXY},
            )
            _tlist = _yapi.list(video_id)
            # Preferir español, luego inglés
            _preferred = None
            _fallback = None
            for _t in _tlist:
                lc = _t.language_code.lower()
                if lc.startswith("es") and _preferred is None:
                    _preferred = _t
                elif lc.startswith("en") and _fallback is None:
                    _fallback = _t
            for _t in ([_preferred] if _preferred else []) + ([_fallback] if _fallback else []):
                try:
                    segs = _t.fetch()
                    transcript_text = " ".join(
                        (s.text if hasattr(s, "text") else s["text"]) for s in segs
                    )
                    if _t.language_code.lower().startswith("en"):
                        transcript_text += "\n[Nota: transcripción en inglés, revisar traducción]"
                    break
                except Exception:
                    continue
        except Exception as e:
            logger.info(f"youtube-transcript-api no disponible ({type(e).__name__}: {e}), probando fallbacks")
    except ImportError:
        logger.warning("youtube-transcript-api no está instalado")

    tier_status = {"t1_api": "no_text", "t1b_html": "skipped", "t2_ytdlp": "skipped", "t3_whisper": "skipped"}
    if transcript_text and len(transcript_text) >= min_len:
        tier_status["t1_api"] = "ok"

    # Fallback 1b: HTML directo (ytInitialPlayerResponse → captionTracks)
    if not transcript_text or len(transcript_text) < min_len:
        logger.info("Intentando extracción directa desde HTML de YouTube…")
        try:
            transcript_text = _transcript_via_html(video_id)
            tier_status["t1b_html"] = "ok" if transcript_text and len(transcript_text) >= min_len else "no_text"
        except Exception as e:
            tier_status["t1b_html"] = f"err: {type(e).__name__}"
            logger.error(f"HTML tier 1b falló: {e}")

    # Fallback 2: yt-dlp (subs oficiales / auto-generados via innertube)
    if not transcript_text or len(transcript_text) < min_len:
        logger.info("Intentando fallback con yt-dlp...")
        try:
            transcript_text = _transcript_via_ytdlp(video_id, min_len=min_len)
            tier_status["t2_ytdlp"] = "ok" if transcript_text and len(transcript_text) >= min_len else "no_text"
        except Exception as e:
            tier_status["t2_ytdlp"] = f"err: {type(e).__name__}"
            logger.error(f"yt-dlp tier 2 falló: {e}")

    # Fallback 3: Whisper (baja audio y transcribe, $0.006/min)
    if not transcript_text or len(transcript_text) < min_len:
        if not OPENAI_API_KEY:
            tier_status["t3_whisper"] = "no_api_key"
        else:
            logger.info("Intentando fallback con Whisper API...")
            try:
                transcript_text = _transcript_via_whisper(video_id)
                tier_status["t3_whisper"] = "ok" if transcript_text and len(transcript_text) >= min_len else "no_text"
            except Exception as e:
                tier_status["t3_whisper"] = f"err: {type(e).__name__}"
                logger.error(f"Whisper tier 3 falló: {e}")

    if not transcript_text or len(transcript_text) < min_len:
        status_str = ", ".join(f"{k}={v}" for k, v in tier_status.items())
        logger.error(f"Todos los fallbacks YouTube fallaron: {status_str}")
        raise RuntimeError(
            f"No pude obtener transcripción. Estado: {status_str}. "
            f"Probá con otro video o pegá el link del artículo que lo cubrió."
        )

    # Limpiar muletillas y marcadores
    transcript_clean = _clean_transcript(transcript_text)

    # Resumir a tono periodístico. Preferimos GPT (tercera persona atribuida),
    # con fallback heurístico si no hay API key o si falla.
    summary = ""
    if OPENAI_API_KEY:
        summary = _summarize_with_gpt(transcript_clean, speaker=author, title=title)
    if not summary or len(summary) < 200:
        summary = _summarize_transcript(transcript_clean, author=author, title=title)

    excerpt = summary[:200] + "..." if len(summary) > 200 else summary

    return {
        "title":               title,
        "original_title":      title,
        "text":                summary,
        "excerpt":             excerpt,
        "original_excerpt":    excerpt,
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


def _summarize_with_gpt(transcript: str, speaker: str = "", title: str = "") -> str:
    """
    Resumen periodístico en tercera persona usando OpenAI gpt-4o-mini.
    Atribuye afirmaciones al hablante por nombre. Costo ~$0.001 por video.
    """
    if not OPENAI_API_KEY:
        return ""
    if not transcript or len(transcript) < 200:
        return ""

    speaker_hint = speaker or "el hablante principal"
    is_desc = transcript.startswith("[Descripción del canal]")
    source_label = "la descripción" if is_desc else "la transcripción"
    prompt = (
        "Sos el editor periodístico de MundoEmpresarial.ar, medio de noticias económicas "
        f"argentinas para pymes y empresarios. Te paso {source_label} de un video de YouTube "
        "y tu tarea es convertirla en un resumen periodístico publicable.\n\n"
        "REGLAS OBLIGATORIAS:\n"
        "1. Escribí en TERCERA PERSONA. Nunca uses primera persona del hablante "
        "(yo, me, mi, nosotros). Todo va atribuido por nombre o por cargo.\n"
        f"2. El hablante principal del video es: {speaker_hint}. Usalo como sujeto de las "
        "afirmaciones. Ejemplo: 'Alejandro Bercovich sostuvo que...'  'El periodista "
        "describió...' 'Bercovich criticó...'\n"
        "3. Largo: 400-700 palabras en 4-6 párrafos cortos (máx 100 palabras por párrafo).\n"
        "4. Lead (primer párrafo) con el hecho o tesis central en 1-2 oraciones.\n"
        "5. Citas textuales entre comillas tipográficas cuando la frase vale, con atribución.\n"
        "6. Español rioplatense (vos, ustedes), sin muletillas ni tics del oral.\n"
        "7. Incluí cifras, nombres propios, fechas si aparecen.\n"
        "8. Ningún H2 ni formato HTML: devolvé texto plano con párrafos separados por doble "
        "salto de línea. El HTML lo agrego yo después.\n\n"
        f"Título del video: {title}\n\n"
        f"{source_label.capitalize()} original:\n"
        "---\n"
        f"{transcript[:12000]}\n"
        "---\n\n"
        "Devolvé SOLO el resumen, sin explicaciones ni encabezados."
    )

    try:
        r = openai_post(
            "https://api.openai.com/v1/chat/completions",
            headers={
                "Authorization": f"Bearer {OPENAI_API_KEY}",
                "Content-Type": "application/json",
            },
            json={
                "model": "gpt-4o-mini",
                "messages": [{"role": "user", "content": prompt}],
                "temperature": 0.3,
            },
            timeout=90,
        )
        if r.status_code == 200:
            content = r.json()["choices"][0]["message"]["content"].strip()
            logger.info(f"GPT resumen OK: {len(content)} chars")
            return content
        logger.error(f"GPT resumen {r.status_code}: {r.text[:300]}")
    except Exception as e:
        logger.error(f"GPT resumen falló: {type(e).__name__}: {e}")
    return ""


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


_ENTREVISTA_RE = re.compile(
    r'\b(entrevista|mano a mano|cara a cara|en di[aá]logo con|columna de opini[oó]n|'
    r'opini[oó]n de|el an[aá]lisis de|editorial de|palabra de|habl[oó] con)\b', re.I)


def es_entrevista_opinion(url: str, title: str, text: str) -> bool:
    """Entrevistas y opiniones en 1ª persona = CAPA 3 (regla de Leo, 2026-07-06). Detecta por
    fuente de video (YouTube = casi siempre entrevista/opinión) o marcadores en título/texto."""
    u = (url or "").lower()
    if "youtube.com" in u or "youtu.be" in u:
        return True
    blob = f"{title or ''} {(text or '')[:600]}"
    return bool(_ENTREVISTA_RE.search(blob))


HILO_NAMES = {1: "Informarse es respetarse", 2: "La voz de las pymes", 3: "Opinión / Análisis"}


def _looks_like_url(s: str) -> bool:
    """True si el texto es (o arranca con) una URL — no sirve como TÍTULO de nota.
    Evita el bug de que una URL de video/fuente quede como título y salga cruda al canal."""
    s = (s or "").strip()
    return bool(re.match(r'^(https?://|www\.|youtu\.be/|youtube\.com/)', s, re.IGNORECASE))


def _decode_google_news_path(google_url: str) -> str | None:
    """
    Los URLs de Google News (news.google.com/rss/articles/<ID> o /articles/<ID>)
    tienen el URL original codificado en el path en protobuf base64url.
    Lo decodeamos offline buscando un 'http(s)://...' en los bytes decodeados.
    Devuelve el URL extraído o None.
    """
    m = re.search(r'/(?:rss/)?articles/([A-Za-z0-9_-]+)', google_url or "")
    if not m:
        return None
    encoded = m.group(1)
    # Normalizar padding base64url
    encoded += "=" * (-len(encoded) % 4)
    try:
        raw = base64.urlsafe_b64decode(encoded)
    except Exception:
        return None
    # Buscar el primer URL http(s) en los bytes; limpiar control chars al final
    m = re.search(rb'https?://[^\s\x00-\x1f"<>]+', raw)
    if not m:
        return None
    candidate = m.group(0).decode("utf-8", errors="ignore")
    # Recortar caracteres de padding comunes del protobuf
    candidate = re.sub(r'[\\\x00-\x1f\x7f-\xff]+$', '', candidate)
    # Rechazar si el dominio sigue siendo google
    if "google.com" in candidate.lower():
        return None
    return candidate


def resolve_google_redirect(url: str) -> str:
    """
    Los links de Google News RSS (news.google.com/rss/articles/...) apuntan
    a un proxy de Google que redirige al artículo real.

    Estrategia:
    1. googlenewsdecoder lib — pega al endpoint interno de Google para
       resolver el ID encriptado a URL real (método actual 2024+).
    2. Decode offline del base64 en el path (funciona para URLs viejas).
    3. Si falla, seguir redirects con cookie CONSENT=YES.
    """
    if not url:
        return url

    low = url.lower()
    is_gnews = (
        "news.google.com/" in low
        or "consent.google.com" in low
        or "google.com/url" in low
    )
    if not is_gnews:
        return url

    # 1) googlenewsdecoder (más confiable para URLs modernas)
    if "news.google.com/" in low:
        try:
            from googlenewsdecoder import gnewsdecoder
            result = gnewsdecoder(url, interval=1)
            if isinstance(result, dict) and result.get("status") and result.get("decoded_url"):
                decoded_url = result["decoded_url"]
                logger.info(f"Google News decoded: {decoded_url[:100]}")
                return decoded_url
            logger.warning(f"gnewsdecoder no pudo: {result}")
        except ImportError:
            logger.warning("googlenewsdecoder no instalado")
        except Exception as e:
            logger.warning(f"gnewsdecoder error: {e}")

    # 2) Decode offline del base64 (formato viejo)
    decoded = _decode_google_news_path(url)
    if decoded:
        logger.info(f"Google News decodeado offline: {decoded[:80]}")
        return decoded

    # 3) Fallback online — seguir redirects
    try:
        session = requests.Session()
        session.headers.update({
            **HEADERS_BROWSER,
            "Cookie": (
                "CONSENT=YES+cb.20210328-17-p0.es+FX+666; "
                "SOCS=CAESHAgBEhJnd3NfMjAyNDAxMDItMF9SQzIaAmVzIAEaBgiAn7SuBg"
            ),
        })
        r = session.get(url, timeout=15, allow_redirects=True)
        final_url = r.url

        if "google.com" not in final_url.lower():
            return final_url

        # 3) Google sirvió HTML: extraer URL final
        from urllib.parse import unquote, urlparse, parse_qs
        qs = parse_qs(urlparse(final_url).query)
        if "url" in qs:
            return unquote(qs["url"][0])
        if "continue" in qs:
            return unquote(qs["continue"][0])

        # meta refresh
        m = re.search(
            r'<meta[^>]+http-equiv=["\']refresh["\'][^>]+url=([^"\'>\s]+)',
            r.text, re.IGNORECASE,
        )
        if m:
            return m.group(1)
        # location.href JS
        m = re.search(r'location(?:\.replace\(|\.href\s*=\s*)["\']([^"\']+)["\']', r.text)
        if m and "google.com" not in m.group(1).lower():
            return m.group(1)
        # Anchor a dominio externo
        m = re.search(
            r'<a[^>]+href="(https?://(?!(?:www\.)?(?:google|consent|support|youtube)\.)[^"]+)"',
            r.text,
        )
        if m:
            return m.group(1)

        logger.warning(f"Google redirect NO resuelto para {url[:80]}")
        return final_url
    except Exception as e:
        logger.warning(f"resolve_google_redirect: {e}")
        return url


def _try_amp_url(url: str, session: requests.Session) -> str | None:
    """
    Muchos sitios AR tienen versión AMP de la nota en /amp o ?amp=1 que
    suele NO estar detrás del mismo anti-bot.
    """
    from urllib.parse import urlparse, urlunparse
    variants = []
    p = urlparse(url)
    path = p.path.rstrip("/")
    # Variantes comunes
    variants.append(urlunparse(p._replace(path=path + "/amp")))
    variants.append(urlunparse(p._replace(path=path + "/amp/")))
    variants.append(urlunparse(p._replace(query="amp=1")))
    variants.append(urlunparse(p._replace(query="outputType=amp")))

    for v in variants:
        try:
            r = session.get(v, timeout=15, allow_redirects=True)
            if r.status_code == 200 and len(r.text) > 5000:
                logger.info(f"AMP version OK: {v[:80]}")
                return _fix_encoding(r)
        except Exception:
            continue
    return None


def _try_bot_user_agents(url: str) -> str | None:
    """
    Retry con User-Agents de crawlers conocidos. Muchos medios permiten
    Googlebot/Bingbot para SEO aunque bloqueen User-Agents de browser
    genéricos desde IPs cloud.
    """
    bots = [
        ("Googlebot", "Mozilla/5.0 (compatible; Googlebot/2.1; +http://www.google.com/bot.html)"),
        ("Bingbot", "Mozilla/5.0 (compatible; bingbot/2.0; +http://www.bing.com/bingbot.htm)"),
        ("GoogleNews", "Googlebot-News"),
        ("facebookexternalhit", "facebookexternalhit/1.1 (+http://www.facebook.com/externalhit_uatext.php)"),
        ("TwitterBot", "Twitterbot/1.0"),
    ]
    for name, ua in bots:
        try:
            r = requests.get(
                url,
                headers={
                    "User-Agent": ua,
                    "Accept": "text/html,application/xhtml+xml,*/*",
                    "Accept-Language": "es-AR,es;q=0.9",
                },
                timeout=15,
                allow_redirects=True,
            )
            if r.status_code == 200 and len(r.text) > 5000:
                logger.info(f"Acceso via {name} UA: {url[:60]}")
                return _fix_encoding(r)
        except Exception:
            continue
    return None


def _fetch_wayback(url: str, session: requests.Session) -> str | None:
    """
    Si el sitio nos bloquea, probá Wayback Machine (archive.org) que suele
    tener un snapshot reciente y no bloquea IPs cloud.
    """
    try:
        api = requests.get(
            f"https://archive.org/wayback/available?url={url}",
            timeout=10,
        )
        if api.status_code == 200:
            data = api.json()
            snap = data.get("archived_snapshots", {}).get("closest", {})
            snap_url = snap.get("url", "")
            if snap_url and snap.get("available"):
                logger.info(f"Wayback snapshot encontrado: {snap_url}")
                # Wayback Machine sirve el contenido original con su chrome al lado,
                # usar el modificador 'id_' que devuelve el HTML tal cual fue capturado
                snap_url = snap_url.replace("/web/", "/web/").replace(
                    f"/web/{snap.get('timestamp')}/",
                    f"/web/{snap.get('timestamp')}id_/"
                )
                r = session.get(snap_url, timeout=20, allow_redirects=True)
                if r.status_code == 200 and len(r.text) > 5000:
                    return _fix_encoding(r)
    except Exception as e:
        logger.warning(f"Wayback fallback falló: {e}")
    return None


def _fetch_google_cache(url: str, session: requests.Session) -> str | None:
    """Último recurso: Google Cache."""
    from urllib.parse import quote
    try:
        r = session.get(
            f"https://webcache.googleusercontent.com/search?q=cache:{quote(url)}",
            timeout=15,
        )
        if r.status_code == 200 and len(r.text) > 5000:
            return _fix_encoding(r)
    except Exception as e:
        logger.warning(f"Google Cache falló: {e}")
    return None


def _scrape_pagina12(html: str) -> str:
    """Extrae cuerpo de artículo de Página 12 via Fusion.globalContent (Arc Publishing)."""
    import re as _re, json as _json
    m = _re.search(r'Fusion\.globalContent\s*=\s*(\{.+?\});\s*(?:Fusion\.|</script>)', html, _re.DOTALL)
    if not m:
        return ""
    try:
        data = _json.loads(m.group(1))
    except Exception:
        return ""
    elements = data.get("content_elements", [])
    paragraphs = []
    for el in elements:
        if el.get("type") in ("text", "raw_html"):
            raw = el.get("content", "")
            text = BeautifulSoup(raw, "html.parser").get_text(" ", strip=True)
            if text.strip():
                paragraphs.append(text.strip())
    return "\n\n".join(paragraphs)


def _whisper_from_url(url: str, proxy: str | None = None, cookies: str | None = None) -> str:
    """Baja audio de cualquier URL con yt-dlp y lo transcribe con Whisper API."""
    if not OPENAI_API_KEY:
        return ""
    try:
        import yt_dlp
    except ImportError:
        return ""
    import tempfile, glob, shutil

    has_ffmpeg = shutil.which("ffmpeg") is not None
    with tempfile.TemporaryDirectory() as tmpdir:
        opts = {
            "format": "bestaudio/best",
            "outtmpl": os.path.join(tmpdir, "audio.%(ext)s"),
            "quiet": True,
            "no_warnings": True,
            "socket_timeout": 60,
            "http_headers": {"User-Agent": HEADERS_BROWSER["User-Agent"]},
        }
        if proxy:
            opts["proxy"] = proxy
        if cookies:
            opts["cookiefile"] = cookies
        if has_ffmpeg:
            opts["postprocessors"] = [{"key": "FFmpegExtractAudio", "preferredcodec": "mp3", "preferredquality": "64"}]
        try:
            with yt_dlp.YoutubeDL(opts) as ydl:
                ydl.download([url])
        except Exception as e:
            logger.warning(f"_whisper_from_url yt-dlp failed: {type(e).__name__}: {str(e)[:200]}")
            return ""
        files = glob.glob(os.path.join(tmpdir, "audio.*"))
        if not files:
            return ""
        audio_path = files[0]
        size_mb = os.path.getsize(audio_path) / (1024 * 1024)
        if size_mb > 24.5:
            logger.error(f"_whisper_from_url: {size_mb:.1f} MB excede límite de 25 MB")
            return ""
        try:
            with open(audio_path, "rb") as f:
                r = openai_post(
                    "https://api.openai.com/v1/audio/transcriptions",
                    headers={"Authorization": f"Bearer {OPENAI_API_KEY}"},
                    data={"model": "whisper-1", "language": "es", "response_format": "text"},
                    files={"file": (os.path.basename(audio_path), f, "audio/mpeg")},
                    timeout=180,
                )
            if r.status_code == 200:
                return r.text.strip()
            logger.error(f"_whisper_from_url API {r.status_code}: {r.text[:400]}")
        except Exception as e:
            logger.error(f"_whisper_from_url request falló: {e}")
    return ""


def _scrape_instagram(url: str) -> dict:
    """Descarga un Reel o post con video de Instagram y retorna data dict compatible con publish_post."""
    from urllib.parse import urlparse, urlunparse
    import yt_dlp

    # Limpiar tracking params de la URL
    parsed = urlparse(url)
    clean_url = urlunparse(parsed._replace(query="", fragment=""))

    logger.info(f"Instagram scrape: iniciando para {clean_url[:80]} (cookies={'sí' if INSTAGRAM_COOKIES else 'no'})")

    # 1) Extraer metadata sin descargar el video
    info = {}
    try:
        meta_opts = {
            "quiet": True,
            "no_warnings": True,
            "skip_download": True,
            "socket_timeout": 30,
            "http_headers": {"User-Agent": HEADERS_BROWSER["User-Agent"]},
        }
        if INSTAGRAM_COOKIES:
            meta_opts["cookiefile"] = INSTAGRAM_COOKIES
        logger.info("Instagram: extrayendo metadata con yt-dlp...")
        with yt_dlp.YoutubeDL(meta_opts) as ydl:
            info = ydl.extract_info(clean_url, download=False) or {}
        logger.info(f"Instagram metadata OK: keys={list(info.keys())[:8]}")
    except Exception as e:
        logger.warning(f"Instagram yt-dlp metadata: {type(e).__name__}: {str(e)[:300]}")

    caption   = info.get("description") or info.get("title") or ""
    thumbnail = info.get("thumbnail", "")
    uploader  = info.get("uploader") or info.get("channel") or ""
    username  = uploader or info.get("uploader_id") or info.get("channel_id") or ""
    if username and not username.startswith("@"):
        username = f"@{username}"
    duration = info.get("duration") or 0

    logger.info(f"Instagram yt-dlp: caption={len(caption)}ch, thumbnail={'sí' if thumbnail else 'no'}, duration={duration}s, user={username}")

    # Fallback HTTP: OG tags — Instagram los sirve sin login para contenido público
    if not caption or not thumbnail:
        logger.info("Instagram: yt-dlp insuficiente, intentando OG tags vía HTTP...")
        try:
            r = requests.get(clean_url, headers=HEADERS_BROWSER, timeout=15, allow_redirects=True)
            if r.status_code == 200:
                soup_ig = BeautifulSoup(r.text, "html.parser")
                def _og(prop):
                    t = soup_ig.find("meta", property=prop) or soup_ig.find("meta", attrs={"name": prop})
                    return (t.get("content") or "").strip() if t else ""
                caption   = caption   or _og("og:description") or _og("description") or _og("og:title")
                thumbnail = thumbnail or _og("og:image")
                if not username:
                    # og:title suele ser: "Nombre de usuario on Instagram: ..."
                    og_title = _og("og:title")
                    if " on Instagram" in og_title:
                        username = "@" + og_title.split(" on Instagram")[0].strip().lstrip("@")
                logger.info(f"Instagram OG fallback OK: caption={len(caption)}ch, thumbnail={'sí' if thumbnail else 'no'}, user={username}")
            else:
                logger.warning(f"Instagram HTTP {r.status_code} para {clean_url[:60]}")
        except Exception as e:
            logger.warning(f"Instagram OG fallback: {type(e).__name__}: {e}")

    logger.info(f"Instagram final: caption={len(caption)}ch, thumbnail={'sí' if thumbnail else 'no'}, duration={duration}s, user={username}")

    # Limpiar caption: quitar hashtags, @mentions y CTAs sociales
    def _clean_caption(raw: str) -> str:
        import re as _re
        # Eliminar hashtags y @mentions
        cleaned = _re.sub(r'#\S+', '', raw)
        cleaned = _re.sub(r'@\S+', '', cleaned)
        # Eliminar líneas de CTA social típicas
        cta_patterns = [
            r'(?i)(dale\s+like|compartí|seguime|seguinos|link\s+en\s+bio|swipe|activ[aá]\s+la\s+campana|suscribite|turn\s+on\s+notif)',
        ]
        for pat in cta_patterns:
            cleaned = _re.sub(pat, '', cleaned)
        # Comprimir espacios y líneas vacías
        cleaned = _re.sub(r'\n{3,}', '\n\n', cleaned)
        return cleaned_text if (cleaned_text := cleaned.strip()) else ""

    clean_caption = _clean_caption(caption) if caption else ""

    # 2) Transcribir audio con Whisper (límite 10 min para no disparar costos)
    transcript = ""
    if OPENAI_API_KEY and 0 < duration <= 600:
        logger.info(f"Instagram Whisper: iniciando ({duration:.0f}s)")
        transcript = _whisper_from_url(clean_url, cookies=INSTAGRAM_COOKIES)
        logger.info(f"Instagram Whisper: {len(transcript)} chars")
    elif duration > 600:
        logger.info(f"Instagram Whisper: omitido, duración {duration:.0f}s > 10 min")
    else:
        logger.info("Instagram Whisper: omitido (sin OPENAI_API_KEY o duración=0)")

    # 3) Texto principal = transcripción (lo que se dijo en el video)
    #    Caption limpio solo como contexto adicional si el transcript es corto
    if transcript and len(transcript) >= 200:
        # Transcript es la fuente principal
        text = transcript
        if clean_caption and len(clean_caption) > 50:
            text = transcript + "\n\n" + clean_caption
    elif clean_caption:
        text = clean_caption
        if transcript:
            text = clean_caption + "\n\n" + transcript
    else:
        text = transcript

    # 4) Título: generado por GPT a partir del transcript (no del caption social)
    #    Fallback: primera oración del transcript o del caption limpio
    def _ig_title_from_gpt(transcript_text: str, caption_text: str, author: str) -> str:
        if not OPENAI_API_KEY or not transcript_text:
            # Fallback sin GPT: primera oración del transcript
            first = (transcript_text or caption_text or "").split(".")[0].strip()
            return (first[:97] + "...") if len(first) > 100 else (first or f"Reel de {author or 'Instagram'}")
        ctx = transcript_text[:1200]
        prompt = (
            "Sos editor de MundoEmpresarial.ar, medio económico argentino para pymes.\n"
            "Te paso la transcripción de un video de Instagram de contenido económico/empresarial.\n"
            "Escribí UN TÍTULO periodístico para una nota en el sitio web.\n\n"
            "REGLAS:\n"
            "- Máximo 70 caracteres\n"
            "- Basado ESTRICTAMENTE en lo que se dice en la transcripción\n"
            "- Sin clickbait, sin preguntas retóricas\n"
            "- Español rioplatense, voz activa, tercera persona\n"
            "- No menciones Instagram, likes, follows ni redes sociales\n"
            "- Devolvé SOLO el título, sin comillas, sin punto final\n\n"
            f"Autor del video: {author or 'desconocido'}\n\n"
            f"Transcripción:\n{ctx}\n\n"
            "Título:"
        )
        try:
            r = openai_post(
                "https://api.openai.com/v1/chat/completions",
                headers={"Authorization": f"Bearer {OPENAI_API_KEY}", "Content-Type": "application/json"},
                json={"model": "gpt-4o-mini", "messages": [{"role": "user", "content": prompt}], "temperature": 0.3},
                timeout=20,
            )
            if r.status_code == 200:
                t = r.json()["choices"][0]["message"]["content"].strip().strip('"').strip("'")
                return t[:100]
        except Exception as e:
            logger.warning(f"Instagram GPT title falló: {e}")
        first = transcript_text.split(".")[0].strip()
        return (first[:97] + "...") if len(first) > 100 else first or f"Reel de {author or 'Instagram'}"

    title = _ig_title_from_gpt(transcript, clean_caption, uploader)

    excerpt = (transcript[:200] if transcript else clean_caption[:200] if clean_caption else "").strip()

    logger.info(f"Instagram scrape OK: title='{title[:60]}', text={len(text)} chars")

    return {
        "title":              title,
        "original_title":     title,
        "text":               text,
        "excerpt":            excerpt,
        "original_excerpt":   excerpt,
        "image_url":          thumbnail,
        "source_url":         clean_url,
        "is_instagram":       True,
        "instagram_username": username,
        "media":              {"has_video": bool(duration)},
    }


def scrape(url: str) -> dict:
    # Resolver redirect de Google News si aplica
    url = resolve_google_redirect(url)

    # Scrapers específicos por plataforma
    if "instagram.com" in url:
        return _scrape_instagram(url)

    session = requests.Session()
    session.headers.update(HEADERS_BROWSER)

    html = None
    try:
        resp = session.get(url, timeout=20)
        if resp.status_code == 403:
            # Cascada de fallbacks para bypass de bloqueos
            logger.warning(f"403 en {url[:80]}, probando fallbacks…")
            html = _try_bot_user_agents(url)
            if not html:
                logger.warning("Bot UAs rechazados, probando AMP…")
                html = _try_amp_url(url, session)
            if not html:
                logger.warning("AMP no disponible, probando Wayback…")
                html = _fetch_wayback(url, session)
            if not html:
                logger.warning("Wayback sin snapshot, probando Google Cache…")
                html = _fetch_google_cache(url, session)
            if not html:
                resp.raise_for_status()
        else:
            resp.raise_for_status()
            html = _fix_encoding(resp)
    except requests.exceptions.HTTPError:
        if not html:
            raise

    # Detectar SPA (React/Vue/Angular) — contenido cargado por JS
    if html and len(html) < 5000 and ('id="root"' in html or 'id="app"' in html or 'id="__next"' in html):
        # Intentar con Wayback o Google Cache como fallback para SPAs
        spa_html = _fetch_wayback(url, session) or _fetch_google_cache(url, session)
        if spa_html:
            html = spa_html
            logger.info(f"SPA detectado, usando fallback para {url}")

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

    # 0) Extractor específico Página 12 (Fusion.globalContent, Arc Publishing)
    text = ""
    extraction_method = ""
    if "pagina12.com.ar" in url:
        p12_text = _scrape_pagina12(html)
        if p12_text and len(p12_text) > 100:
            text = clean_text(p12_text)
            extraction_method = f"pagina12-fusion ({len(text)} chars)"

    # 1) Intentar JSON-LD primero (solo si el extractor específico no ya tiene texto)
    if not text:
        ld = _extract_jsonld(soup)
        if ld and len(ld["text"]) > 100:
            text = clean_text(ld["text"])
            title = ld["title"] or title
            image_url = ld["image_url"] or image_url
            extraction_method = f"json-ld ({len(text)} chars)"
        else:
            # 2) Fallback a trafilatura
            traf_raw = trafilatura.extract(html) or ""
            text = clean_text(traf_raw)
            if text:
                extraction_method = f"trafilatura ({len(text)} chars, raw {len(traf_raw)})"

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
                    extraction_method = f"css-selector '{sel}' ({len(text)} chars)"
                    break

    # 4) Si el texto sigue vacío, intentar Wayback, Google Cache y trafilatura fetch_url
    if not text or len(text) < 150:
        for label, fallback_html in [
            ("wayback", _fetch_wayback(url, session)),
            ("gcache",  _fetch_google_cache(url, session)),
        ]:
            if not fallback_html:
                continue
            fb_soup = BeautifulSoup(fallback_html, "html.parser")
            fb_traf = trafilatura.extract(fallback_html) or ""
            fb_text = clean_text(fb_traf)
            if not fb_text or len(fb_text) < 150:
                for sel in ["article", ".article-body", ".entry-content", '[itemprop="articleBody"]']:
                    el = fb_soup.select_one(sel)
                    if el and len(el.get_text(strip=True)) > 200:
                        paras = [p.get_text(strip=True) for p in el.find_all("p") if len(p.get_text(strip=True)) > 20]
                        if paras:
                            fb_text = clean_text("\n".join(paras))
                            break
            if fb_text and len(fb_text) >= 150:
                text = fb_text
                extraction_method = f"{label} ({len(text)} chars)"
                logger.info(f"Texto recuperado via {label} para {url[:60]}")
                break

    # 4b) Último intento: trafilatura.fetch_url (descarga propia con mejor User-Agent)
    if not text or len(text) < 150:
        try:
            traf_fetched = trafilatura.fetch_url(url)
            if traf_fetched:
                tf2 = clean_text(trafilatura.extract(traf_fetched) or "")
                if tf2 and len(tf2) >= 150:
                    text = tf2
                    extraction_method = f"trafilatura-fetch ({len(text)} chars)"
                    logger.info(f"Texto recuperado via trafilatura.fetch_url para {url[:60]}")
        except Exception as e:
            logger.debug(f"trafilatura.fetch_url falló: {e}")

    # 5) Detectar muro de pago si el texto sigue vacío
    paywall = False
    paywall_trigger = None
    if not text or len(text) < 150:
        # Solo señales inequívocas — palabras genéricas como "subscription" aparecen en
        # footers/newsletters de cualquier diario y generan falsos positivos
        paywall_signals = [
            "muro de pago", "contenido exclusivo", "para suscriptores",
            "acceso exclusivo", "solo para socios", "registrate para leer",
            "iniciar sesión para leer",
            "p12-paywall", "paywall-container", "article--locked", "article-locked",
            "nota-exclusiva", "nota exclusiva",
        ]
        html_lower = html.lower()
        for sig in paywall_signals:
            if sig in html_lower:
                paywall = True
                paywall_trigger = sig
                break
        # Señal adicional: página verdaderamente vacía (solo estructura HTML, sin contenido)
        body_text = soup.get_text(separator=" ", strip=True)
        if not paywall and title and len(body_text) < 80:
            paywall = True
            paywall_trigger = f"body_text={len(body_text)}"

    if paywall and (not text or len(text) < 150):
        logger.warning(f"Paywall detectado — url={url[:80]} trigger='{paywall_trigger}' text_len={len(text) if text else 0}")
        raise ValueError(
            f"🔒 Artículo detrás de muro de pago: no se puede leer el contenido.\n"
            f"Título: {title}"
        )

    if not text or len(text) < 50:
        raise ValueError(
            f"No se pudo extraer texto de la nota (method={extraction_method or 'none'}).\n"
            f"Título: {title}"
        )

    excerpt = excerpt or (text[:200] + "..." if text else "")

    # Log del método de extracción (útil para debug en Railway)
    logger.info(
        f"scrape({url[:60]}): method={extraction_method or 'NONE'}, "
        f"html_size={len(html)}, title='{title[:50]}'"
    )

    clean_title = title.strip()
    return {
        "title":            clean_title,
        "original_title":   clean_title,
        "text":             text,
        "excerpt":          excerpt,
        "original_excerpt": excerpt,
        "image_url":        image_url,
        "source_url":       url,
        "media":            media_info,
        "_extraction_method": extraction_method or "none",
        "_html_size":         len(html),
    }


# ── Canal de Telegram ─────────────────────────────────────────────────────────

async def publish_to_channel(bot, data: dict, wp_url: str):
    """Publica en el canal. Devuelve message_id (int) o None si falló."""
    s_title = get_title(data)
    kw = focus_keyword(data.get("original_title") or data.get("title", ""))
    s_excerpt = get_excerpt(data, kw=kw)
    tracked_url = utm_url(wp_url, "telegram")
    text = f"📰 *{s_title}*\n\n{s_excerpt[:200]}\n\n🔗 [Leer nota completa]({tracked_url})"
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

def _build_commands_text() -> str:
    """Texto de ayuda con todos los comandos disponibles. Reusable para /start y /comandos."""
    return (
        "🤖 *Bot MundoEmpresarial — Comandos disponibles*\n\n"
        "📥 *Publicar*\n"
        "• Pegar link (con o sin texto alrededor) → scrape + preview + publicación.\n"
        "  Soporta artículos, YouTube (con transcripción), URLs de Google News.\n"
        "• `/editar <URL o ID>` → editar título, categoría, foto, redes sociales o borrar.\n"
        "• `/hilo <URL o ID>` → generar hilo de Twitter desde una nota ya publicada.\n"
        "• `/borrar <URL o ID>` → mandar una nota directo a la papelera de WP.\n"
        "\n"
        "📰 *Curaduría diaria*\n"
        "• `/horarios` → ver y configurar cuántas veces por día y a qué hora se envía el curador.\n"
        "• `/feedback_ver` → ver qué dominios y keywords aprendió el curador a "
        "favorecer/penalizar en base a tus 👍👎 sobre las notas.\n"
        "\n"
        "📂 *Datos del ecosistema*\n"
        "• `/fuentes [dominio]` → repositorio de medios que usás (orientación editorial, "
        "afinidad, hilo típico, quirks técnicos).\n"
        "• `/cola` → notas programadas pendientes de publicar.\n"
        "• `/stats` → publicadas / canceladas / errores del día y fuentes.\n"
        "  (Auto: reporte diario 23:00 ARG)\n"
        "\n"
        "💳 *Servicios y créditos*\n"
        "• `/creditos` → estado de OpenAI, X/Twitter, RapidAPI, EnvíaloSimple, DonWeb, "
        "Telegram, WordPress, dolarapi, etc.\n"
        "  (Auto: reporte semanal lunes 09:00 ARG + alertas reactivas ante 402/quota)\n"
        "• `/testtwitter` → diagnóstico OAuth 1.0a de Twitter (whoami).\n"
        "\n"
        "ℹ️ *Ayuda*\n"
        "• `/start` o `/comandos` → este listado.\n"
    )


async def cmd_stop(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Pausa el bot: deja de procesar links y posts programados sin apagar el servicio."""
    global BOT_PAUSED
    BOT_PAUSED = True
    await update.message.reply_text(
        "⏸ *Bot pausado.* No voy a procesar links ni posts programados hasta que uses /RESUME.",
        parse_mode="Markdown",
    )


async def cmd_resume(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Reactiva el bot después de /STOP."""
    global BOT_PAUSED
    BOT_PAUSED = False
    await update.message.reply_text("▶️ *Bot reactivado.* Listo para procesar links.", parse_mode="Markdown")


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not ADMIN_CHAT_ID:
        _persist_admin_chat_id(str(update.message.chat_id))
    await update.message.reply_text(_build_commands_text(), parse_mode="Markdown")


async def cmd_comandos(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(_build_commands_text(), parse_mode="Markdown")


async def cmd_stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Estadísticas del día — migrado al harness."""
    await update.message.reply_text("📊 Las estadísticas del pipeline están en el harness (/coladepublicacion).")


def _check_credits_status() -> dict:
    """
    Health check sync de TODOS los servicios usados por el bot.
    Devuelve dict {service_key: (emoji, detail_str, tier)}.
    tier: 'pago' | 'gratuito' | 'freemium'
    """
    from datetime import datetime
    result = {}

    # ═══ PAGOS CON API DE QUOTA ═══

    # 1. OpenAI (chat / embeddings / Whisper)
    if OPENAI_API_KEY:
        try:
            r = requests.post(
                "https://api.openai.com/v1/chat/completions",
                headers={
                    "Authorization": f"Bearer {OPENAI_API_KEY}",
                    "Content-Type": "application/json",
                },
                json={
                    "model": "gpt-4o-mini",
                    "messages": [{"role": "user", "content": "ok"}],
                    "max_tokens": 1,
                },
                timeout=15,
            )
            body_low = r.text.lower()
            if r.status_code == 200:
                result["openai"] = ("✅", "OK", "pago")
            elif r.status_code == 429 and "insufficient_quota" in body_low:
                result["openai"] = ("❌", "SIN CRÉDITO", "pago")
            elif r.status_code == 401:
                result["openai"] = ("❌", "API key inválida o revocada", "pago")
            elif r.status_code == 429:
                result["openai"] = ("⚠️", "Rate limit (temporal)", "pago")
            else:
                result["openai"] = ("⚠️", f"HTTP {r.status_code}", "pago")
        except Exception as e:
            result["openai"] = ("❌", f"Error: {type(e).__name__}", "pago")
    else:
        result["openai"] = ("⚙️", "OPENAI_API_KEY sin configurar", "pago")

    # 2. Twitter auth + counter mensual
    if TWITTER_API_KEY and TWITTER_API_SECRET and TWITTER_TOKEN and TWITTER_SECRET:
        try:
            auth = OAuth1(TWITTER_API_KEY, TWITTER_API_SECRET, TWITTER_TOKEN, TWITTER_SECRET)
            r = requests.get("https://api.twitter.com/2/users/me", auth=auth, timeout=10)
            if r.status_code == 200:
                result["twitter_auth"] = ("✅", f"@{r.json().get('data', {}).get('username', '?')}", "pago")
            else:
                result["twitter_auth"] = ("❌", f"HTTP {r.status_code}", "pago")
        except Exception as e:
            result["twitter_auth"] = ("❌", f"Error: {type(e).__name__}", "pago")
    else:
        result["twitter_auth"] = ("⚙️", "Credenciales incompletas", "pago")

    fb = _load_feedback()
    month_key = "tweets_count_" + datetime.now().strftime("%Y%m")
    tw_count = fb.get(month_key, 0)
    # Cuenta xAI: pre-paid credits, no free tier mensual fijo. Mostramos sólo el
    # contador de tweets emitidos en el mes como info útil, no como % de un cap.
    result["twitter_quota"] = (
        "📊", f"{tw_count} tweets emitidos este mes", "pago",
    )

    # 3. RapidAPI — chequeo via headers x-ratelimit-* del endpoint que use el user
    rapidapi_key = os.environ.get("RAPIDAPI_KEY", "")
    rapidapi_host = os.environ.get("RAPIDAPI_TEST_HOST", "")
    rapidapi_path = os.environ.get("RAPIDAPI_TEST_PATH", "/")
    if rapidapi_key:
        # Si configuró un host de test puntual (ej. el que usa para CRM Bodegas),
        # usamos ese. Si no, probamos contra un endpoint público de testing.
        candidates = []
        if rapidapi_host:
            candidates.append((rapidapi_host, rapidapi_path))
        candidates.extend([
            ("httpbin-rapidapi.p.rapidapi.com", "/get"),
            ("rapidapi-developer.p.rapidapi.com", "/"),
        ])
        ok = False
        for host, path in candidates:
            try:
                r = requests.get(
                    f"https://{host}{path}",
                    headers={"X-RapidAPI-Key": rapidapi_key, "X-RapidAPI-Host": host},
                    timeout=10,
                )
                # Aunque devuelva 4xx, los headers x-ratelimit-* suelen estar presentes
                limit = r.headers.get("x-ratelimit-requests-limit") or r.headers.get("x-ratelimit-limit")
                remaining = r.headers.get("x-ratelimit-requests-remaining") or r.headers.get("x-ratelimit-remaining")
                if r.status_code in (200, 401, 403, 404, 429) and (limit or remaining):
                    if r.status_code == 429:
                        result["rapidapi"] = ("❌", f"Rate limit / quota agotada (rest {remaining}/{limit})", "pago")
                    elif r.status_code in (401, 403):
                        result["rapidapi"] = ("⚠️", f"Endpoint sin permisos pero key activa (rest {remaining}/{limit})", "pago")
                    else:
                        result["rapidapi"] = ("✅", f"key OK · quota mensual restante {remaining}/{limit}", "pago")
                    ok = True
                    break
                if r.status_code == 200:
                    result["rapidapi"] = ("✅", f"key OK ({host} no expone ratelimit headers)", "pago")
                    ok = True
                    break
            except Exception:
                continue
        if not ok:
            result["rapidapi"] = (
                "⚠️",
                "No pude testear (configurá RAPIDAPI_TEST_HOST con un endpoint del que tengas suscripción)",
                "pago",
            )
    else:
        result["rapidapi"] = ("⚙️", "RAPIDAPI_KEY sin configurar en Railway", "pago")

    # 4. Instagram scraper — providers conocidos: Apify, RapidAPI Instagram-scraper, ScrapingBee
    ig_key = os.environ.get("IG_SCRAPER_KEY", "") or os.environ.get("INSTAGRAM_API_KEY", "")
    ig_provider = (os.environ.get("IG_SCRAPER_PROVIDER", "") or "").lower()
    if ig_key:
        # Chequeo según provider
        if ig_provider == "apify":
            try:
                r = requests.get(
                    f"https://api.apify.com/v2/users/me?token={ig_key}",
                    timeout=10,
                )
                if r.status_code == 200:
                    j = r.json().get("data", {})
                    plan = j.get("plan", {}).get("id", "?")
                    result["ig_scraper"] = ("✅", f"Apify OK · plan {plan}", "freemium")
                elif r.status_code == 401:
                    result["ig_scraper"] = ("❌", "Apify token inválido", "freemium")
                else:
                    result["ig_scraper"] = ("⚠️", f"Apify HTTP {r.status_code}", "freemium")
            except Exception as e:
                result["ig_scraper"] = ("❌", f"Apify error: {type(e).__name__}", "freemium")
        elif ig_provider == "scrapingbee":
            try:
                r = requests.get(
                    f"https://app.scrapingbee.com/api/v1/usage?api_key={ig_key}",
                    timeout=10,
                )
                if r.status_code == 200:
                    j = r.json()
                    used = j.get("used_api_credit", "?")
                    max_c = j.get("max_api_credit", "?")
                    result["ig_scraper"] = ("✅", f"ScrapingBee {used}/{max_c} créditos usados", "freemium")
                else:
                    result["ig_scraper"] = ("⚠️", f"ScrapingBee HTTP {r.status_code}", "freemium")
            except Exception as e:
                result["ig_scraper"] = ("❌", f"ScrapingBee: {type(e).__name__}", "freemium")
        elif ig_provider == "rapidapi":
            # Va a través de RapidAPI, ya cubierto arriba pero hacemos un check mínimo
            ig_host = os.environ.get("IG_RAPIDAPI_HOST", "instagram-scraper-api2.p.rapidapi.com")
            try:
                r = requests.get(
                    f"https://{ig_host}/v1/info?username_or_id_or_url=mundoempresarialar",
                    headers={"X-RapidAPI-Key": ig_key, "X-RapidAPI-Host": ig_host},
                    timeout=10,
                )
                remaining = r.headers.get("x-ratelimit-requests-remaining", "?")
                limit = r.headers.get("x-ratelimit-requests-limit", "?")
                emoji = "✅" if r.status_code == 200 else "⚠️"
                result["ig_scraper"] = (emoji, f"RapidAPI IG · rest {remaining}/{limit}", "freemium")
            except Exception as e:
                result["ig_scraper"] = ("❌", f"IG-RapidAPI: {type(e).__name__}", "freemium")
        else:
            result["ig_scraper"] = (
                "⚙️",
                "Configurado pero sin IG_SCRAPER_PROVIDER (apify/scrapingbee/rapidapi)",
                "freemium",
            )
    else:
        result["ig_scraper"] = (
            "⚙️",
            "IG_SCRAPER_KEY + IG_SCRAPER_PROVIDER (apify|scrapingbee|rapidapi) sin configurar",
            "freemium",
        )

    # 5. EnvíaloSimple — email transaccional newsletter
    es_token = (
        os.environ.get("ENVIALO_SIMPLE_TOKEN", "")
        or os.environ.get("ENVIALOSIMPLE_TOKEN", "")
        or os.environ.get("ENVIALO_SIMPLE_API_KEY", "")
    )
    # Vencimiento (env var YYYY-MM-DD opcional, viene del panel DonWeb/EnvíaloSimple)
    es_exp_suffix = ""
    es_exp = os.environ.get("ENVIALO_SIMPLE_EXPIRES", "")
    if es_exp:
        try:
            from datetime import datetime as _dt2
            exp_dt = _dt2.strptime(es_exp, "%Y-%m-%d").date()
            days_left = (exp_dt - _dt2.now().date()).days
            if days_left < 0:
                es_exp_suffix = f" · 🔴 VENCIDO hace {-days_left}d"
            elif days_left <= 3:
                es_exp_suffix = f" · 🔴 vence en {days_left}d"
            elif days_left <= 7:
                es_exp_suffix = f" · 🟡 vence en {days_left}d"
            else:
                es_exp_suffix = f" · vence en {days_left}d"
            if days_left <= 7:
                alert_admin_throttled(
                    f"envialo_exp_{es_exp}",
                    f"⚠️ *EnvíaloSimple por vencer*\n"
                    f"Vence el {es_exp} ({days_left} días).\n"
                    f"Renovar en https://donweb.com (panel servicios)",
                    cooldown_minutes=24 * 60,
                )
        except ValueError:
            es_exp_suffix = " · vencimiento inválido"

    if es_token:
        # EnvíaloSimple tiene API REST en envialosimple.email/api/v1
        # con auth bearer. Endpoint /api/v1/account/info devuelve plan + saldo.
        try:
            r = requests.get(
                "https://api.envialosimple.com/v1/account",
                headers={"Authorization": f"Bearer {es_token}"},
                timeout=10,
            )
            if r.status_code == 200:
                j = r.json() if r.text.startswith("{") else {}
                # Distintas versiones del endpoint usan distintos campos; mostrar lo crudo
                detail_parts = []
                for k in ("plan", "credits", "credits_remaining", "saldo", "balance"):
                    v = j.get(k)
                    if v is not None:
                        detail_parts.append(f"{k}={v}")
                detail = " · ".join(detail_parts) if detail_parts else "OK"
                result["envialo_simple"] = ("✅", detail + es_exp_suffix, "pago")
            elif r.status_code in (401, 403):
                result["envialo_simple"] = ("❌", "Token inválido o expirado" + es_exp_suffix, "pago")
            else:
                result["envialo_simple"] = (
                    "⚙️",
                    f"HTTP {r.status_code} (endpoint puede haber cambiado){es_exp_suffix}",
                    "pago",
                )
        except Exception as e:
            result["envialo_simple"] = (
                "⚙️",
                f"No pude consultar API ({type(e).__name__}){es_exp_suffix}",
                "pago",
            )
    else:
        result["envialo_simple"] = (
            "⚙️",
            f"ENVIALO_SIMPLE_TOKEN sin configurar (panel: envialosimple.email){es_exp_suffix}",
            "pago",
        )

    # 6. DonWeb hosting (sitio mundoempresarial.ar)
    # 6a. Uptime check
    try:
        r = requests.head(WP_URL, timeout=10, allow_redirects=True)
        site_status = "online" if r.status_code in (200, 301, 302) else f"HTTP {r.status_code}"
    except Exception as e:
        site_status = f"caído ({type(e).__name__})"

    # 6b. Vencimiento (env var YYYY-MM-DD opcional)
    donweb_exp = os.environ.get("DONWEB_HOSTING_EXPIRES", "")
    if donweb_exp:
        try:
            from datetime import datetime as _dt
            exp_dt = _dt.strptime(donweb_exp, "%Y-%m-%d").date()
            today = _dt.now().date()
            days_left = (exp_dt - today).days
            if days_left < 0:
                emoji = "🔴"
                exp_str = f"VENCIDO hace {-days_left}d"
            elif days_left <= 3:
                emoji = "🔴"
                exp_str = f"vence en {days_left}d ({donweb_exp}) — RENOVAR YA"
            elif days_left <= 7:
                emoji = "🟡"
                exp_str = f"vence en {days_left}d ({donweb_exp})"
            else:
                emoji = "🟢"
                exp_str = f"vence en {days_left}d ({donweb_exp})"
            result["donweb"] = (emoji, f"{site_status} · {exp_str}", "pago anual")
            # Alerta automática si vence pronto
            if days_left <= 7:
                alert_admin_throttled(
                    f"donweb_exp_{donweb_exp}",
                    f"⚠️ *Hosting DonWeb por vencer*\n"
                    f"Vence el {donweb_exp} ({days_left} días).\n"
                    f"Si no renovás, el sitio mundoempresarial.ar se cae y "
                    f"todo el ecosistema queda offline.\n"
                    f"Renovar en https://donweb.com",
                    cooldown_minutes=24 * 60,
                )
        except ValueError:
            result["donweb"] = ("⚠️", f"{site_status} · DONWEB_HOSTING_EXPIRES inválido", "pago anual")
    else:
        emoji = "✅" if site_status == "online" else "❌"
        result["donweb"] = (
            emoji,
            f"{site_status} · vencimiento sin trackear (cargar DONWEB_HOSTING_EXPIRES YYYY-MM-DD)",
            "pago anual",
        )

    # ═══ GRATUITOS ═══

    # Telegram
    try:
        r = requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/getMe",
            timeout=10,
        )
        if r.status_code == 200:
            username = r.json().get("result", {}).get("username", "?")
            result["telegram"] = ("✅", f"@{username}", "gratuito")
        else:
            result["telegram"] = ("❌", f"HTTP {r.status_code}", "gratuito")
    except Exception as e:
        result["telegram"] = ("❌", f"Error: {type(e).__name__}", "gratuito")

    # WordPress REST (en realidad pago indirecto via DonWeb pero el API es libre)
    try:
        r = requests.get(f"{WP_URL}/wp-json/wp/v2/posts?per_page=1", timeout=10)
        if r.status_code == 200:
            result["wordpress"] = ("✅", "REST API OK", "gratuito")
        else:
            result["wordpress"] = ("⚠️", f"HTTP {r.status_code}", "gratuito")
    except Exception as e:
        result["wordpress"] = ("❌", f"Error: {type(e).__name__}", "gratuito")

    # DolarAPI
    try:
        r = requests.get("https://dolarapi.com/v1/dolares/oficial", timeout=10)
        result["dolarapi"] = (("✅", "OK", "gratuito") if r.status_code == 200
                              else ("⚠️", f"HTTP {r.status_code}", "gratuito"))
    except Exception:
        result["dolarapi"] = ("❌", "No responde", "gratuito")

    # ArgentinaDatos
    try:
        r = requests.get(
            "https://api.argentinadatos.com/v1/finanzas/indices/riesgo-pais/ultimo",
            timeout=10,
        )
        result["argentinadatos"] = (("✅", "OK", "gratuito") if r.status_code == 200
                                     else ("⚠️", f"HTTP {r.status_code}", "gratuito"))
    except Exception:
        result["argentinadatos"] = ("❌", "No responde", "gratuito")

    # YouTube scrapers (yt-dlp + transcript-api + googlenewsdecoder)
    result["youtube_stack"] = ("✅", "yt-dlp + transcript-api + googlenewsdecoder", "gratuito")

    return result


# Etiquetas + descripción de cada servicio (orden de aparición en reporte)
SERVICE_LABELS = {
    "openai":         ("OpenAI", "gpt-4o-mini, embeddings, Whisper"),
    "twitter_auth":   ("Twitter / X auth", "OAuth 1.0a developer.x.com"),
    "twitter_quota":  ("Twitter free tier", "500 tweets/mes"),
    "rapidapi":       ("RapidAPI", "marketplace de APIs (CRM Bodegas y otros)"),
    "ig_scraper":     ("Instagram scraper", "servicio externo para IG"),
    "envialo_simple": ("EnvíaloSimple", "email transaccional newsletter"),
    "donweb":         ("DonWeb", "hosting de mundoempresarial.ar"),
    "telegram":       ("Telegram Bot API", "bot + canal"),
    "wordpress":      ("WordPress REST", "API de mundoempresarial.ar"),
    "dolarapi":       ("DolarAPI", "cotizaciones AR"),
    "argentinadatos": ("ArgentinaDatos", "riesgo país"),
    "youtube_stack":  ("YouTube + Google News", "yt-dlp, transcript-api, decoder"),
}


def _format_credits_report(status: dict) -> str:
    """Arma el mensaje de reporte de créditos agrupado por tier (pago / gratuito)."""
    from datetime import datetime
    lines = [f"💳 *Estado de servicios — {datetime.now().strftime('%d/%m/%Y %H:%M')}*", ""]

    # Agrupar por tier
    paid_keys = [k for k, v in status.items() if v[2] in ("pago", "pago anual", "freemium")]
    free_keys = [k for k, v in status.items() if v[2] == "gratuito"]

    # Pagos primero
    if paid_keys:
        lines.append("🟥 *Servicios pagos*")
        for key in SERVICE_LABELS:
            if key not in paid_keys:
                continue
            emoji, detail, tier = status[key]
            label, desc = SERVICE_LABELS[key]
            tier_tag = f" _({tier})_" if tier != "pago" else ""
            lines.append(f"  {emoji} *{label}*{tier_tag}: {detail}")
            lines.append(f"      _{desc}_")
        lines.append("")

    # Recomendaciones accionables
    recs = []
    if status.get("openai", (None,))[0] == "❌":
        recs.append("• Recargar OpenAI: https://platform.openai.com/account/billing")
    if status.get("twitter_auth", (None,))[0] == "❌":
        recs.append("• Twitter auth roto — chequear OAuth en developer.x.com")
    if status.get("donweb", (None,))[0] == "❌":
        recs.append("• Sitio caído — entrar al panel de DonWeb")
    # Tip permanente sobre dónde recargar X (cuenta vinculada a xAI)
    recs.append("• Créditos de X: https://console.x.ai → Facturación → Créditos")
    if recs:
        lines.append("⚠️ *Acciones sugeridas:*")
        lines.extend(recs)
        lines.append("")

    # Gratuitos
    if free_keys:
        lines.append("🟩 *Servicios gratuitos*")
        for key in SERVICE_LABELS:
            if key not in free_keys:
                continue
            emoji, detail, _ = status[key]
            label, desc = SERVICE_LABELS[key]
            lines.append(f"  {emoji} *{label}*: {detail}")

    return "\n".join(lines)


async def cmd_creditos(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Health check on-demand de servicios pagos."""
    msg = await update.message.reply_text("💳 Chequeando estado de servicios pagos…")
    status = await asyncio.to_thread(_check_credits_status)
    await msg.edit_text(_format_credits_report(status), parse_mode="Markdown", disable_web_page_preview=True)


async def send_weekly_credits_check(context: ContextTypes.DEFAULT_TYPE):
    """Job semanal (lunes 9 AM ARG) que manda al admin el estado de créditos."""
    chat_id = ADMIN_CHAT_ID
    if not chat_id:
        logger.warning("send_weekly_credits_check: sin ADMIN_CHAT_ID")
        return
    try:
        status = await asyncio.to_thread(_check_credits_status)
        report = _format_credits_report(status)
        await context.bot.send_message(
            chat_id=int(chat_id),
            text=f"📅 *Reporte semanal*\n\n{report}",
            parse_mode="Markdown",
            disable_web_page_preview=True,
        )
        logger.info("Weekly credits check enviado")
    except Exception as e:
        logger.error(f"weekly_credits_check: {e}")


async def cmd_reglas(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Muestra el registro de reglas editoriales activas, desactivadas y patrones ignorados."""
    import sys as _sys_r
    _sys_r.path.insert(0, "/opt/me-harness")
    try:
        import broker as _br_r
        reg = _br_r.get_rules_registry()
    except Exception as _e_r:
        await update.message.reply_text(f"Error cargando registro: {_e_r}")
        return

    active   = reg.get("active", [])
    inactive = reg.get("inactive", [])
    ignored  = reg.get("ignored", [])

    parts = []

    if active:
        parts.append(f"<b>✅ Reglas activas ({len(active)})</b>")
        for r in active:
            cat = r.get("category", "")
            rule = (r.get("rule") or "")[:120]
            parts.append(f"  <b>#{r['id']}</b> <code>[{cat}]</code> {rule}")
    else:
        parts.append("<b>✅ Reglas activas</b>\n  <i>Ninguna</i>")

    if inactive:
        parts.append(f"\n<b>⏸ Reglas desactivadas ({len(inactive)})</b>")
        for r in inactive:
            cat  = r.get("category", "")
            rule = (r.get("rule") or "")[:80]
            parts.append(f"  <b>#{r['id']}</b> <code>[{cat}]</code> {rule}")

    if ignored:
        parts.append(f"\n<b>❌ Patrones ignorados para siempre ({len(ignored)})</b>")
        for ig in ignored:
            parts.append(f"  • <code>{ig['keyword']}</code>")
    else:
        parts.append("\n<b>❌ Patrones ignorados</b>\n  <i>Ninguno</i>")

    msg = "\n".join(parts) or "Registro vacío."
    await update.message.reply_text(msg, parse_mode="HTML")


async def cmd_publinotas(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Lista todas las notas marcadas como comerciales/publicitarias."""
    import sys as _sys_pl
    _sys_pl.path.insert(0, "/opt/me-harness")
    try:
        import broker as _br_pl
        items = _br_pl.get_publinotas()
    except Exception as _e_pl:
        await update.message.reply_text(f"Error: {_e_pl}")
        return

    if not items:
        await update.message.reply_text("📢 <b>Publinotas</b>\n\n<i>Sin registros aún.</i>",
                                        parse_mode="HTML")
        return

    parts = [f"📢 <b>Publinotas registradas ({len(items)})</b>\n"]
    for it in items:
        date_str = (it.get("created_at") or "")[:10]
        title    = (it.get("title") or "sin título")[:80]
        url      = it.get("url") or ""
        job_id   = it.get("job_id", "")
        parts.append(f"<b>#{it['id']}</b> <i>{date_str}</i> — job {job_id}\n"
                     f"  {title}\n"
                     f"  <a href='{url}'>{url[:60]}</a>\n")

    msg = "\n".join(parts)
    # Telegram límite 4096 chars
    for chunk in [msg[i:i+4000] for i in range(0, len(msg), 4000)]:
        await update.message.reply_text(chunk, parse_mode="HTML",
                                        disable_web_page_preview=True)


# ── Keywords temporales de agenda ────────────────────────────────────────────

async def cmd_kwtemp(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    /kwtemp <keyword> [capa:N] [dias:N]  — agrega keyword temporal a la agenda
    /kwtemp list                          — lista keywords activas del editor
    /kwtemp del <id>                      — desactiva una keyword por ID
    """
    import sys as _sys_kw
    _sys_kw.path.insert(0, "/opt/me-harness")
    try:
        import broker as _br_kw
    except Exception as _e_kw:
        await update.message.reply_text(f"Error importando broker: {_e_kw}")
        return

    args = (context.args or [])
    text_args = " ".join(args).strip()

    if not text_args or text_args.lower() == "list":
        # Listar keywords activas del editor
        items = _br_kw.get_agenda_keywords_editor()
        if not items:
            await update.message.reply_text(
                "🔑 <b>Keywords temporales</b>\n\n<i>Sin keywords activas.</i>",
                parse_mode="HTML")
            return
        lines = [f"🔑 <b>Keywords temporales ({len(items)})</b>\n"]
        for it in items:
            capa_str = f" CAPA {it['capa']}" if it['capa'] else ""
            days_str = f" ({it['days_left']}d)" if it['days_left'] is not None else ""
            lines.append(f"<b>#{it['id']}</b>{capa_str}{days_str} — {it['keyword']}")
        await update.message.reply_text(
            "\n".join(lines), parse_mode="HTML")
        return

    if text_args.lower().startswith("del "):
        # Desactivar por ID
        try:
            kw_id = int(text_args[4:].strip())
            _br_kw.deactivate_agenda_keyword(kw_id)
            await update.message.reply_text(f"✅ Keyword #{kw_id} desactivada.")
        except Exception as _e:
            await update.message.reply_text(f"Error: {_e}\nUso: /kwtemp del <id>")
        return

    # Agregar keyword temporal
    # Parsing: /kwtemp cheques rechazados capa:1 dias:7
    import re as _re_kw
    capa_match = _re_kw.search(r"capa:(\d)", text_args, _re_kw.IGNORECASE)
    dias_match = _re_kw.search(r"dias?:(\d+)", text_args, _re_kw.IGNORECASE)

    capa = int(capa_match.group(1)) if capa_match else None
    dias = int(dias_match.group(1)) if dias_match else 7

    keyword = text_args
    if capa_match:
        keyword = keyword[:capa_match.start()].strip()
    if dias_match:
        keyword = keyword[:dias_match.start()].strip()
    keyword = keyword.strip()

    if not keyword or len(keyword) < 3:
        await update.message.reply_text(
            "Uso: /kwtemp <keyword> [capa:1] [dias:7]\n"
            "     /kwtemp list\n"
            "     /kwtemp del <id>")
        return

    from datetime import datetime as _dt_kw, timedelta as _td_kw
    expires_at = (_dt_kw.utcnow() + _td_kw(days=dias)).strftime("%Y-%m-%dT%H:%M:%S")

    try:
        _br_kw.add_agenda_keyword(
            keyword=keyword,
            capa=capa,
            source="editor",
            weight=1.0,
            expires_at=expires_at,
        )
        capa_str = f" CAPA {capa}" if capa else " (cross-capa)"
        await update.message.reply_text(
            f"✅ Keyword agregada{capa_str} por {dias} días:\n"
            f"<b>{keyword}</b>",
            parse_mode="HTML")
    except Exception as _e:
        await update.message.reply_text(f"Error: {_e}")


# ── Fuentes (sources.json) ───────────────────────────────────────────────────

_SOURCES_CACHE = None


def _load_sources() -> dict:
    """
    Lee sources.json desde el disco + overlay del feedback store (entradas
    agregadas/borradas por el operador via /fuentes). Cacheado en memoria.
    """
    global _SOURCES_CACHE
    if _SOURCES_CACHE is not None:
        return _SOURCES_CACHE
    try:
        path = os.path.join(os.path.dirname(__file__), "sources.json")
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        sources = {k: v for k, v in data.items() if not k.startswith("_")}
    except Exception as e:
        logger.error(f"Error cargando sources.json: {e}")
        sources = {}

    # Aplicar overlay (agregadas/borradas en runtime, persiste en feedback store WP)
    try:
        fb = _load_feedback()
        overlay = fb.get("sources_overlay", {}) or {}
        for domain, src_data in overlay.items():
            if not isinstance(src_data, dict):
                continue
            if src_data.get("_deleted"):
                sources.pop(domain, None)
            else:
                sources[domain] = src_data
    except Exception as e:
        logger.warning(f"Sources overlay: {e}")

    _SOURCES_CACHE = sources
    return _SOURCES_CACHE


def _invalidate_sources_cache():
    global _SOURCES_CACHE
    _SOURCES_CACHE = None


def _add_source(domain: str, source_data: dict) -> bool:
    """Agrega o actualiza una fuente en el overlay del feedback store."""
    fb = _load_feedback()
    overlay = fb.setdefault("sources_overlay", {})
    overlay[domain] = source_data
    ok = _save_feedback(fb)
    _invalidate_sources_cache()
    return ok


def _delete_source(domain: str) -> bool:
    """Marca una fuente como borrada en el overlay (sobrevive a redeploys)."""
    fb = _load_feedback()
    overlay = fb.setdefault("sources_overlay", {})
    overlay[domain] = {"_deleted": True}
    ok = _save_feedback(fb)
    _invalidate_sources_cache()
    return ok


def _parse_source_with_gpt(text: str) -> dict | None:
    """
    Parsea descripción libre de una fuente nueva a JSON estructurado vía GPT.
    Devuelve dict con _domain + campos de sources.json schema, o None si falla.
    """
    if not OPENAI_API_KEY:
        return None
    if not text or len(text.strip()) < 5:
        return None

    prompt = (
        "Sos asistente del bot de MundoEmpresarial.ar (medio AR para pymes "
        "alineado con ENAC, desarrollismo nacional). Te paso una descripción "
        "libre de una fuente nueva. Devolvé SOLO un JSON válido con estos campos:\n\n"
        '{\n'
        '  "_domain": "dominio.com.ar",  // dominio raíz sin www, sin protocolo, sin path\n'
        '  "name": "Nombre Display",\n'
        '  "tipo": "Generalista|Economía|Política|Pyme|Agro|Internacional|Opinión|Sectorial|Oficial",\n'
        '  "orientacion": "Descripción breve del sesgo editorial del medio",\n'
        '  "distancia_editorial": 5,    // 1-10. 1=alineado ENAC, 10=opuesto\n'
        '  "hilo_tipico": 2,             // 1=info útil, 2=voz pymes, 3=opinión\n'
        '  "confiabilidad": 7,           // 1-10\n'
        '  "notas": "Observaciones editoriales libres",\n'
        '  "quirks": "",                 // problemas técnicos conocidos al scrapear\n'
        '  "rss_url": ""                 // URL del RSS si la conocés, vacío si no\n'
        '}\n\n'
        f"Descripción del operador:\n{text}\n\n"
        "Devolvé SOLO el JSON, sin markdown ni explicaciones."
    )

    try:
        r = openai_post(
            "https://api.openai.com/v1/chat/completions",
            headers={
                "Authorization": f"Bearer {OPENAI_API_KEY}",
                "Content-Type": "application/json",
            },
            json={
                "model": "gpt-4o-mini",
                "messages": [{"role": "user", "content": prompt}],
                "response_format": {"type": "json_object"},
                "temperature": 0.2,
            },
            timeout=30,
        )
        if r.status_code == 200:
            content = r.json()["choices"][0]["message"]["content"].strip()
            data = json.loads(content)
            # Sanitizar dominio
            dom = (data.get("_domain") or "").strip().lower()
            dom = dom.replace("https://", "").replace("http://", "").replace("www.", "")
            dom = dom.split("/")[0]
            if not dom:
                return None
            data["_domain"] = dom
            return data
    except Exception as e:
        logger.error(f"_parse_source_with_gpt: {e}")
    return None


def _domain_of(url: str) -> str:
    """Extrae el dominio raíz de un URL (sin www, sin subdominios m./amp.)."""
    from urllib.parse import urlparse
    try:
        host = urlparse(url).netloc.lower()
    except Exception:
        return ""
    host = host.replace("www.", "").replace("m.", "").replace("amp.", "")
    return host


def find_source(url_or_domain: str) -> tuple[str, dict] | None:
    """Busca una fuente por URL completa o por dominio. Devuelve (domain, data) o None."""
    sources = _load_sources()
    needle = url_or_domain if "." in url_or_domain else ""
    if url_or_domain.startswith(("http://", "https://")):
        needle = _domain_of(url_or_domain)
    # Match exacto
    if needle in sources:
        return needle, sources[needle]
    # Match por sufijo (lapoliticaonline.com vs lapoliticaonline.com.ar)
    for domain, data in sources.items():
        if needle and (needle.endswith(domain) or domain.endswith(needle)):
            return domain, data
    return None


async def cmd_fuentes(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    /fuentes           → lista todas las fuentes registradas
    /fuentes <domain>  → detalle de una fuente específica
    """
    args = " ".join(context.args).strip()
    sources = _load_sources()

    if args:
        # Buscar una fuente puntual
        found = find_source(args)
        if not found:
            await update.message.reply_text(
                f"No encontré *{md_escape(args)}* en el repositorio.\n"
                f"Probá con /fuentes para ver la lista completa.",
                parse_mode="Markdown",
            )
            return
        domain, d = found
        msg = (
            f"📡 *{md_escape(d.get('name', domain))}* "
            f"`({md_escape(domain)})`\n\n"
            f"*Tipo:* {md_escape(d.get('tipo', '?'))}\n"
            f"*Distancia editorial:* {d.get('distancia_editorial', '?')}/10 "
            f"(1=alineado ENAC, 10=opuesto)\n"
            f"*Confiabilidad:* {d.get('confiabilidad', '?')}/10\n\n"
            f"*Orientación:*\n_{md_escape(d.get('orientacion', '?'))}_\n\n"
            f"*Notas:*\n{md_escape(d.get('notas', '-'))}\n"
        )
        quirks = d.get("quirks", "")
        if quirks:
            msg += f"\n*Quirks técnicos:*\n{md_escape(quirks)}\n"
        await update.message.reply_text(msg, parse_mode="Markdown")
        return

    # Lista completa, ordenada por distancia editorial (más afines primero)
    if not sources:
        await update.message.reply_text("No hay fuentes registradas.")
        return

    entries = sorted(
        sources.items(),
        key=lambda x: x[1].get("distancia_editorial", 99)
    )

    parts = [f"📡 *Repositorio de fuentes* ({len(sources)} medios)\n"]
    for domain, d in entries:
        dist = d.get("distancia_editorial", "?")
        tipo = d.get("tipo", "")
        tipo_str = f" · {md_escape(tipo)}" if tipo else ""
        parts.append(
            f"• *{md_escape(d.get('name', domain))}* "
            f"`{md_escape(domain)}`{tipo_str} — dist {dist}/10"
        )

    parts.append("\n_Usá_ `/fuentes <dominio>` _para ver detalle._")
    kb = InlineKeyboardMarkup([[
        InlineKeyboardButton("➕ Agregar fuente", callback_data="src_add"),
        InlineKeyboardButton("🗑️ Borrar fuente", callback_data="src_del"),
    ]])
    await update.message.reply_text(
        "\n".join(parts), parse_mode="Markdown", reply_markup=kb,
    )


async def handle_sources_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    if query.data == "src_add":
        context.user_data["waiting_for_new_source"] = True
        await query.edit_message_text(
            "➕ *Agregar fuente nueva*\n\n"
            "Mandame en un mensaje el dominio + descripción libre. Ejemplos:\n\n"
            "`lanueva.com.ar - Diario de Bahía Blanca, generalista, mirada equilibrada provincial. RSS estándar.`\n\n"
            "`mdzol.com - Mendoza, generalista, línea pro-mercado tradicional, scoring distancia 7.`\n\n"
            "GPT te arma el JSON con tipo, orientación, distancia editorial, hilo típico, etc. "
            "Después podés ajustar con /fuentes <dominio>.\n\n"
            "_Cancelá ignorando el mensaje y usando otro comando._",
            parse_mode="Markdown",
        )
        return

    if query.data == "src_del":
        sources = _load_sources()
        if not sources:
            await query.edit_message_text("No hay fuentes para borrar.")
            return
        sorted_doms = sorted(sources.keys())
        # Telegram limita botones por mensaje. Si >40 fuentes, paginar (no necesario por ahora).
        rows = []
        context.chat_data["src_del_map"] = {}
        for i, dom in enumerate(sorted_doms[:30]):
            name = sources[dom].get("name", dom)
            label = f"❌ {name[:30]}"
            rows.append([InlineKeyboardButton(label, callback_data=f"srcdel_{i}")])
            context.chat_data["src_del_map"][i] = dom
        rows.append([InlineKeyboardButton("Cancelar", callback_data="src_cancel")])
        await query.edit_message_text(
            "🗑️ *Elegí qué fuente borrar*",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup(rows),
        )
        return

    if query.data == "src_cancel":
        await query.edit_message_text("Cancelado.")
        return

    if query.data.startswith("srcdel_"):
        try:
            idx = int(query.data.split("_", 1)[1])
        except ValueError:
            return
        domain = context.chat_data.get("src_del_map", {}).get(idx)
        if not domain:
            await query.answer("Fuente no encontrada", show_alert=True)
            return
        context.chat_data["src_del_pending"] = domain
        sources = _load_sources()
        info = sources.get(domain, {})
        kb = InlineKeyboardMarkup([[
            InlineKeyboardButton(f"❌ Confirmar borrado", callback_data="src_del_confirm"),
            InlineKeyboardButton("Cancelar", callback_data="src_cancel"),
        ]])
        await query.edit_message_text(
            f"🗑️ Borrar fuente:\n\n"
            f"*{md_escape(info.get('name', domain))}* (`{md_escape(domain)}`)\n"
            f"_{md_escape((info.get('orientacion') or '')[:200])}_\n\n"
            f"¿Confirmás?",
            parse_mode="Markdown",
            reply_markup=kb,
        )
        return

    if query.data == "src_del_confirm":
        domain = context.chat_data.get("src_del_pending", "")
        if not domain:
            await query.edit_message_text("No hay borrado pendiente.")
            return
        ok = await asyncio.to_thread(_delete_source, domain)
        if ok:
            await query.edit_message_text(
                f"✅ Fuente *{md_escape(domain)}* borrada del repositorio.",
                parse_mode="Markdown",
            )
        else:
            await query.edit_message_text(
                f"❌ Error al borrar *{md_escape(domain)}*.",
                parse_mode="Markdown",
            )
        context.chat_data.pop("src_del_pending", None)
        return


CAT_NAMES = {
    95: "AFIP", 88: "Agro", 1048: "Coberturas", 89: "Comercio",
    99: "Congreso", 337: "Destacados", 239: "Digitalización Pymes",
    94: "Economía", 96: "Empresas", 100: "Gobierno", 90: "Industria",
    103: "Informes", 97: "Internacional", 98: "Nacional", 91: "Opinión",
    101: "Poder Judicial", 87: "Política", 338: "Principales",
    102: "Provincias", 92: "Servicios", 93: "Sindicatos",
}

# ── ECO helpers ───────────────────────────────────────────────────────────────

def _eco_data_with_alt(eco: dict) -> dict:
    """Copia de data con alt_title/alt_bajada del ECO aplicados."""
    d = dict(eco["data"])
    if eco.get("alt_title"):
        d["title"] = eco["alt_title"]
        d["title_edited"] = True
    if eco.get("alt_bajada"):
        d["excerpt"] = eco["alt_bajada"]
        d["excerpt_edited"] = True
    return d


def _eco_preview_text(eco: dict) -> str:
    d = _eco_data_with_alt(eco)
    title  = get_title(d)
    bajada = get_excerpt(d)[:180]
    tw = "✅" if eco.get("tw_on", True) else "❌"
    tg = "✅" if eco.get("tg_on", True) else "❌"
    li = "✅" if eco.get("li_on", False) else "❌"
    alt_note = " _(editado)_" if eco.get("alt_title") else ""
    ex_note  = " _(editada)_" if eco.get("alt_bajada") else ""
    return (
        f"📣 *ECO — Configuración*\n\n"
        f"*Título:* {md_escape(title)}{alt_note}\n"
        f"*Bajada:* {md_escape(bajada)}{ex_note}\n\n"
        f"{tw} Twitter  {tg} Canal TG  {li} LinkedIn"
    )


def _build_eco_kb(eco: dict) -> InlineKeyboardMarkup:
    tw_label = "✅ Twitter" if eco.get("tw_on", True) else "❌ Twitter"
    tg_label = "✅ Canal TG" if eco.get("tg_on", True) else "❌ Canal TG"
    li_label = "✅ LinkedIn" if eco.get("li_on", False) else "❌ LinkedIn"
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("✏️ Título alt.", callback_data="eco_edit_title"),
            InlineKeyboardButton("✏️ Bajada alt.", callback_data="eco_edit_bajada"),
        ],
        [
            InlineKeyboardButton(tw_label, callback_data="eco_toggle_tw"),
            InlineKeyboardButton(tg_label, callback_data="eco_toggle_tg"),
        ],
        [
            InlineKeyboardButton(li_label, callback_data="eco_toggle_li"),
        ],
        [
            InlineKeyboardButton("⏰ Programar eco", callback_data="eco_schedule"),
        ],
        [
            InlineKeyboardButton("🔄 Restaurar originales", callback_data="eco_restore"),
            InlineKeyboardButton("❌ Cancelar eco", callback_data="eco_cancel"),
        ],
    ])


def _build_eco_schedule_kb() -> InlineKeyboardMarkup:
    from datetime import datetime, timezone, timedelta
    tz_arg = timezone(timedelta(hours=-3))
    now = datetime.now(tz_arg)
    noon_day    = "hoy" if now.hour < 11 else "mañana"
    evening_day = "hoy" if now.hour < 17 else "mañana"
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(f"🌅 Mañana — 08:00 (mañana)", callback_data="ecosch_morning")],
        [InlineKeyboardButton(f"☀️ Mediodía — 12:00 ({noon_day})", callback_data="ecosch_noon")],
        [InlineKeyboardButton(f"🌇 Tarde — 18:00 ({evening_day})", callback_data="ecosch_evening")],
        [InlineKeyboardButton("🕐 Fijar hora", callback_data="ecosch_custom")],
        [InlineKeyboardButton("↩️ Volver", callback_data="eco_schedule_back")],
    ])


def _build_eco_sched_day_kb() -> InlineKeyboardMarkup:
    from datetime import datetime, timezone, timedelta
    tz_arg = timezone(timedelta(hours=-3))
    now = datetime.now(tz_arg)
    labels = [
        (f"Hoy {now.strftime('%d/%m')}", "ecosch_day_0"),
        (f"Mañana {(now + timedelta(days=1)).strftime('%d/%m')}", "ecosch_day_1"),
        (f"Pasado {(now + timedelta(days=2)).strftime('%d/%m')}", "ecosch_day_2"),
    ]
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(label, callback_data=cd) for label, cd in labels],
        [InlineKeyboardButton("↩️ Volver", callback_data="eco_schedule")],
    ])


def _build_eco_sched_hour_kb() -> InlineKeyboardMarkup:
    hours = ["06", "08", "10", "12", "14", "16", "18", "20", "22"]
    rows = []
    for i in range(0, len(hours), 3):
        rows.append([
            InlineKeyboardButton(f"{h}:00", callback_data=f"ecosch_h_{h}")
            for h in hours[i:i+3]
        ])
    rows.append([InlineKeyboardButton("↩️ Volver", callback_data="ecosch_custom")])
    return InlineKeyboardMarkup(rows)


def _preview_kb_from_ctx(context) -> InlineKeyboardMarkup:
    ud = context.user_data
    return build_preview_kb(
        tw_on   = ud.get("tw_on", True),
        tg_on   = ud.get("tg_on", True),
        wa_on   = ud.get("wa_on", False),
        li_on   = ud.get("li_on", False),
        dest_on = ud.get("dest_on", False),
        orig_on = ud.get("orig_title_on", False),
        orig_excerpt_on = ud.get("orig_excerpt_on", False),
        eco_on  = ud.get("eco_on", False),
        desp_on = ud.get("desp_on", False),
    )


def build_preview_kb(tw_on: bool = True, tg_on: bool = True, wa_on: bool = False, li_on: bool = False, dest_on: bool = False, orig_on: bool = False, orig_excerpt_on: bool = False, eco_on: bool = False, desp_on: bool = False) -> InlineKeyboardMarkup:
    tw_label     = "✅ Twitter" if tw_on else "❌ Twitter"
    tg_label     = "✅ Canal TG" if tg_on else "❌ Canal TG"
    wa_label     = "✅ WhatsApp" if wa_on else "❌ WhatsApp"
    li_label     = "✅ LinkedIn" if li_on else "❌ LinkedIn"
    dest_label   = "⭐ Destacado" if dest_on else "☆ Destacado"
    orig_label   = "✅ Titulo original" if orig_on else "❌ Titulo original"
    orig_ex_label = "✅ Bajada original" if orig_excerpt_on else "❌ Bajada original"
    eco_label    = "📣 ECO ON" if eco_on else "📣 ECO OFF"
    desp_label   = "📖 Desplegable ON" if desp_on else "📖 Desplegable OFF"
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton(tw_label, callback_data="toggle_tw"),
            InlineKeyboardButton(tg_label, callback_data="toggle_tg"),
        ],
        [
            InlineKeyboardButton(wa_label, callback_data="toggle_wa"),
            InlineKeyboardButton(li_label, callback_data="toggle_li"),
        ],
        [
            InlineKeyboardButton(dest_label, callback_data="toggle_dest"),
        ],
        [
            InlineKeyboardButton(orig_label, callback_data="toggle_orig_title"),
            InlineKeyboardButton(orig_ex_label, callback_data="toggle_orig_excerpt"),
        ],
        [
            InlineKeyboardButton(eco_label, callback_data="toggle_eco"),
        ],
        [
            InlineKeyboardButton(desp_label, callback_data="toggle_desp"),
        ],
        [
            InlineKeyboardButton("⚡ Publicar auto", callback_data="pub_auto"),
        ],
        [
            InlineKeyboardButton("🚀 Publicar ahora", callback_data="pub"),
            InlineKeyboardButton("⏰ Programar", callback_data="pub_schedule"),
        ],
        [
            InlineKeyboardButton("Cambiar titulo", callback_data="change_title"),
            InlineKeyboardButton("Cancelar", callback_data="cancel"),
        ],
    ])


def build_schedule_kb() -> InlineKeyboardMarkup:
    """Sub-menú de programación: 3 turnos fijos + Fijar hora."""
    from datetime import datetime, timezone, timedelta
    tz_arg = timezone(timedelta(hours=-3))
    now = datetime.now(tz_arg)

    morning_day = "mañana"
    noon_day = "hoy" if now.hour < 11 else "mañana"
    evening_day = "hoy" if now.hour < 17 else "mañana"

    return InlineKeyboardMarkup([
        [InlineKeyboardButton(f"🌅 Turno mañana — 08:00 ({morning_day})", callback_data="sched_morning")],
        [InlineKeyboardButton(f"☀️ Mediodía — 12:00 ({noon_day})", callback_data="sched_noon")],
        [InlineKeyboardButton(f"🌇 Tarde — 18:00 ({evening_day})", callback_data="sched_evening")],
        [InlineKeyboardButton("🕐 Fijar hora", callback_data="sched_custom")],
        [InlineKeyboardButton("↩️ Volver", callback_data="sched_to_ht")],
    ])


def build_sched_day_kb() -> InlineKeyboardMarkup:
    """Picker de día para programación personalizada."""
    from datetime import datetime, timezone, timedelta
    tz_arg = timezone(timedelta(hours=-3))
    now = datetime.now(tz_arg)
    labels = [
        (f"Hoy {now.strftime('%d/%m')}", "sched_day_0"),
        (f"Mañana {(now + timedelta(days=1)).strftime('%d/%m')}", "sched_day_1"),
        (f"Pasado {(now + timedelta(days=2)).strftime('%d/%m')}", "sched_day_2"),
    ]
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(label, callback_data=cd) for label, cd in labels],
        [InlineKeyboardButton("↩️ Volver", callback_data="sched_confirm_ht")],
    ])


def build_sched_hour_kb() -> InlineKeyboardMarkup:
    """Picker de hora para programación personalizada."""
    hours = ["06", "08", "10", "12", "14", "16", "18", "20", "22"]
    rows = []
    for i in range(0, len(hours), 3):
        rows.append([
            InlineKeyboardButton(f"{h}:00", callback_data=f"sched_h_{h}")
            for h in hours[i:i+3]
        ])
    rows.append([
        InlineKeyboardButton("✏️ Escribir hora", callback_data="sched_hour_write"),
        InlineKeyboardButton("↩️ Volver", callback_data="sched_custom"),
    ])
    return InlineKeyboardMarkup(rows)


def _build_sched_pre_ht_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("✅ Confirmar HT", callback_data="sched_confirm_ht"),
            InlineKeyboardButton("✏️ Cambiar HT", callback_data="sched_change_ht_pre"),
        ],
        [InlineKeyboardButton("↩️ Volver", callback_data="sched_back")],
    ])


def build_preview(data: dict) -> str:
    s_title = get_title(data)
    s_kw    = focus_keyword(data["title"])
    s_desc  = get_excerpt(data, kw=s_kw)
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

    hts = _build_hashtags(data)
    return (
        f"*{md_escape(s_title)}*\n\n"
        f"{yt_line}"
        f"{hilo_line}"
        f"*Keyword:* {s_kw}\n"
        f"*Slug:* /{s_slug}\n"
        f"*Categorias:* {cats_str}\n"
        f"*Etiquetas:* {tag_preview}\n"
        f"*HT:* `{md_escape(hts)}`\n\n"
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


def _dup_manual_hl(titulo: str):
    """Posible duplicado de un link manual: compara el título contra los jobs recientes
    del harness (7 días, cualquier stage vivo) por Jaccard de palabras. El dedup
    semántico corre en curador/briefing pero los links manuales lo bypassean — este
    es el aviso mínimo (caso Tenreyro 8/7: la misma noticia subida dos veces).
    Devuelve (jaccard, {id, stage, title}) o None."""
    import sqlite3 as _sq3
    stop = {"de", "la", "el", "los", "las", "del", "en", "y", "a", "un", "una", "que",
            "por", "con", "para", "al", "se", "su", "es", "como", "más", "mas", "le",
            "sera", "será", "nueva", "nuevo"}

    def _stems(t):
        # stems de 5 chars: "economistas"/"economista" y "jefa"/"jefe" cuentan igual
        return {w[:5] for w in re.findall(r"[a-záéíóúüñ0-9]+", (t or "").lower())
                if w not in stop and len(w) > 2}

    def _nombres(t):
        # nombres propios (dos palabras capitalizadas seguidas): señal fuerte de misma noticia
        return set(re.findall(r"\b[A-ZÁÉÍÓÚÑ][a-záéíóúüñ]+ [A-ZÁÉÍÓÚÑ][a-záéíóúüñ]+\b", t or ""))

    w1, n1 = _stems(titulo), _nombres(titulo)
    if not w1:
        return None
    db = _sq3.connect("/opt/me-harness/harness.db", timeout=10)
    db.row_factory = _sq3.Row
    rows = db.execute(
        "SELECT id, stage, title FROM jobs "
        "WHERE stage IN ('curado','cola','redaccion','publicacion','done') "
        "AND created_at >= datetime('now','-7 days') ORDER BY id DESC LIMIT 300").fetchall()
    db.close()
    best = None
    for r in rows:
        w2 = _stems(r["title"])
        if not w2:
            continue
        ov = len(w1 & w2) / min(len(w1), len(w2))          # overlap coefficient
        nombre_comun = bool(n1 & _nombres(r["title"]))
        score = max(ov, 0.99 if (nombre_comun and ov >= 0.3) else 0)
        if score >= 0.55 and (best is None or score > best[0]):
            best = (score, dict(r))
    return best


async def handle_link(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global ADMIN_CHAT_ID
    # Ignorar mensajes de canales (el bot es admin del canal; no debe procesar sus posts)
    if update.message and update.message.chat.type == "channel":
        return
    if BOT_PAUSED:
        await update.message.reply_text("⏸ Bot en pausa. Usá /RESUME para reactivar.")
        return
    text_in = update.message.text.strip()

    # ── /evento: capturar instrucción / texto / link de una cobertura activa ────
    if context.user_data.get("awaiting_evento_instr"):
        context.user_data.pop("awaiting_evento_instr", None)
        _ev = context.user_data.get("evento")
        if _ev is not None:
            _ev["instr"] = text_in
            await update.message.reply_text(
                "📝 Instrucción guardada para el evento.",
                parse_mode="HTML", reply_markup=_evento_kb())
        return
    if context.user_data.get("evento") is not None:
        await _evento_add_text(update, context, text_in)
        return

    # ── /vivo: capturar campos del panel de preparación (título, placa, enfoque, links) ──
    _vvc = next((c for c in ("titulo", "placa", "enfoque", "links")
                 if context.user_data.get(f"awaiting_vv_{c}")), None)
    if _vvc:
        context.user_data.pop(f"awaiting_vv_{_vvc}", None)
        await _vv_capturar_texto(update, context, _vvc, text_in)
        return

    # ── /vivo: ajuste puntual sobre un bloque redactado en /input_evento ─────────
    if context.user_data.get("awaiting_vvb_adj"):
        context.user_data.pop("awaiting_vvb_adj", None)
        await _vvb_ajustar(update, context, text_in)
        return

    # ── /vivo mail: ajuste puntual sobre la propuesta de newsletter ──────────────
    if context.user_data.get("awaiting_vv_mail_adj"):
        context.user_data.pop("awaiting_vv_mail_adj", None)
        await _vv_mail_ajustar(update, context, text_in)
        return

    # ── /vivo difundir: nuevo texto de la re-difusión ─────────────────────────────
    if context.user_data.get("awaiting_vv_dif_txt"):
        context.user_data.pop("awaiting_vv_dif_txt", None)
        await _vv_dif_texto_capturar(update, context, text_in)
        return

    # ── /input_evento: texto de una tanda EN VIVO ────────────────────────────────
    if context.user_data.get("input_evento") is not None:
        await _iev_add_text(update, context, text_in)
        return

    # ── Nota desde tweet: capturar la directiva (el ángulo) ──────────────────────
    if context.user_data.get("awaiting_tweet_directiva"):
        context.user_data.pop("awaiting_tweet_directiva", None)
        _tw = context.user_data.pop("tweet_nota", None)
        if not _tw:
            await update.message.reply_text("⚠️ Se perdió el tweet. Reenviá el link.")
            return
        _directiva = "" if text_in.strip() == "-" else text_in.strip()
        await _crear_nota_tweet(update, context, _tw, _directiva)
        return

    # ── Panel de nueva efeméride: capturar nombre/fecha/ángulo/segmento por texto ──
    _efc = next((c for c in ("nombre", "fecha", "angulo", "segmento")
                 if context.user_data.get(f"awaiting_efem_{c}")), None)
    if _efc:
        context.user_data.pop(f"awaiting_efem_{_efc}", None)
        _efd = context.user_data.get("efem")
        if _efd is not None:
            if _efc == "fecha":
                import sys as _syse2
                _syse2.path.insert(0, "/opt/me-harness")
                _syse2.path.insert(0, "/opt/me-harness/agents")
                import eventos as _eve
                _nf = _eve._norm_fecha(text_in)
                if not _nf:
                    await update.message.reply_text(
                        "Fecha inválida. Usá DD/MM (ej: 29/07). Tocá 📅 Fecha y probá de nuevo.")
                    return
                _efd["fecha"] = _nf
            else:
                _efd[_efc] = text_in.strip()
            await _efem_redraw(context, _efd)
        return

    # ── ✏️ Ajustar sobre una ALERTA del auditor: Leo escribe qué quiere que se haga ──
    # No hay propuesta que reformular acá (una alerta es un hecho, no una sugerencia), así
    # que su texto se guarda como pedido con el detalle técnico adjunto. Directiva Leo 29/7.
    if context.user_data.get("awaiting_ajuste_aud"):
        _ajau = context.user_data.pop("awaiting_ajuste_aud")
        import json as _jsau2, datetime as _dtau2
        _Hau = "/opt/me-harness"
        try:
            try:
                _ctxau = _jsau2.load(open(f"{_Hau}/alertas_ctx.json", encoding="utf-8"))
            except Exception:
                _ctxau = {}
            try:
                _pedau = _jsau2.load(open(f"{_Hau}/alertas_pedidos.json", encoding="utf-8"))
            except Exception:
                _pedau = {}
            _cau = _ctxau.get(_ajau["fp"], {})
            _pedau[_ajau["fp"]] = {
                "ts": _dtau2.datetime.now(_dtau2.timezone.utc).isoformat(),
                "señales": _cau.get("señales", []),
                "resumen": _cau.get("resumen", ""),
                "detalle": _cau.get("detalle", {}),
                "pedido_de_leo": text_in,
                "estado": "pendiente",
            }
            _jsau2.dump(_pedau, open(f"{_Hau}/alertas_pedidos.json", "w", encoding="utf-8"),
                        ensure_ascii=False)
            await update.message.reply_text(
                "📝 Anotado. Queda en la cola de pedidos con el detalle técnico de la alerta.")
        except Exception as _e:
            await update.message.reply_text(f"No pude guardarlo: {str(_e)[:120]}")
        return

    # ── ✏️ Ajustar (opinar/complementar) una propuesta: editor/supervisor/reco/impacto ──
    # Leo tocó "✏️ Ajustar" en una propuesta y ahora escribe su criterio. El agente reformula
    # la propuesta incorporándolo, la persiste revisada y la RE-MANDA con los botones (por
    # tg_notify, desde el harness). Acá solo disparamos + avisamos (freeze-safe: to_thread).
    if context.user_data.get("awaiting_ajuste"):
        _aj   = context.user_data.pop("awaiting_ajuste")
        _flow = _aj.get("flow"); _aid = _aj.get("id"); _aidx = _aj.get("idx", 0)
        import sys as _sysaj
        _sysaj.path.insert(0, "/opt/me-harness"); _sysaj.path.insert(0, "/opt/me-harness/agents")
        await update.message.reply_text("🔄 Reformulando la propuesta con tu ajuste…")

        def _do_reformular():
            if _flow == "edit":
                import editor as _m; return _m.reformular(_aid, _aidx, text_in)
            if _flow == "sup":
                import supervisor as _m; return _m.reformular(_aid, _aidx, text_in)
            if _flow == "reco":
                import direccion as _m; return _m.reformular_reco(_aid, text_in)
            if _flow == "esq":
                import direccion as _m; return _m.reformular_esquema(_aid, text_in)
            if _flow == "imp":
                import impacto as _m; return _m.reformular(_aid, _aidx, text_in)
            if _flow == "gsc":
                import gsc as _m; return _m.reformular(_aid, text_in)
            if _flow == "gscln":
                import gsc as _m; return _m.reformular_ln(_aid, text_in)
            return None

        try:
            res = await asyncio.wait_for(asyncio.to_thread(_do_reformular), timeout=90)
            if not res:
                await update.message.reply_text("⚠️ No pude reformular (GPT o dato faltante). Probá de nuevo.")
        except Exception as _e:
            await update.message.reply_text(f"⚠️ Error reformulando: {str(_e)[:150]}")
        return

    # Guardar chat_id del operador para reportes
    if not ADMIN_CHAT_ID:
        _persist_admin_chat_id(str(update.message.chat_id))

    # ── Resumen finde (flujo 8AM): correcciones / reprogramar hora ──────────────
    if context.user_data.get("awaiting_finde_corr"):
        _fdate = context.user_data.pop("awaiting_finde_corr")
        import sqlite3 as _sqf
        from datetime import datetime as _dtf
        with _sqf.connect("/opt/me-harness/harness.db") as _c:
            _c.execute("UPDATE finde_approval SET status='corregir', feedback=?, updated_at=? WHERE date=?",
                       (text_in, _dtf.utcnow().isoformat(), _fdate))
        await update.message.reply_text("✏️ Listo, rehago el texto con tus indicaciones y te lo reenvío para revisar.")
        return
    if context.user_data.get("awaiting_finde_reprog"):
        _fdate = context.user_data.pop("awaiting_finde_reprog")
        import re as _ref, sqlite3 as _sqf
        from datetime import datetime as _dtf
        m = _ref.match(r'^(\d{1,2}):(\d{2})$', text_in.strip())
        if not m:
            context.user_data["awaiting_finde_reprog"] = _fdate
            await update.message.reply_text("Hora inválida. Escribila como HH:MM (ej: 10:30):")
            return
        hh, mm = int(m.group(1)), int(m.group(2))
        with _sqf.connect("/opt/me-harness/harness.db") as _c:
            _c.execute("UPDATE finde_approval SET publish_at=?, updated_at=? WHERE date=?",
                       (f"{_fdate}T{hh:02d}:{mm:02d}:00", _dtf.utcnow().isoformat(), _fdate))
        await update.message.reply_text(f"🕘 Reprogramado: el resumen sale a las {hh:02d}:{mm:02d}.")
        return

    # ── Ciclaje del briefing: recibir los horarios nuevos y guardar la config ──
    if context.user_data.get("awaiting_ciclaje_horarios"):
        context.user_data.pop("awaiting_ciclaje_horarios")
        import re as _rec
        slots = [s.strip() for s in text_in.replace(";", ",").split(",") if s.strip()]
        ok = [s for s in slots if _rec.fullmatch(r"([01]?\d|2[0-3]):[0-5]\d", s)]
        ok = [f"{int(s.split(':')[0]):02d}:{s.split(':')[1]}" for s in ok]
        if not ok or len(ok) != len(slots):
            # NO re-armar el flag: si el texto no es una lista de horarios, se cancela
            # (si se re-armara, este bloque se tragaría URLs/títulos que Leo mande después).
            await update.message.reply_text(
                "Eso no parece una lista de horarios — ciclaje SIN cambios. "
                "Para reintentar, tocá 🕘 Cambiar horarios de nuevo (ej: 08:00, 13:00, 19:00).")
            return
        import sys as _sysc
        _sysc.path.insert(0, "/opt/me-harness")
        from agents import curador as _curc
        _curc.save_briefing_config({"horarios": sorted(set(ok))})
        await update.message.reply_text(
            _curc.ciclaje_texto() + "\n\n✅ Horarios actualizados — el tick ya corre con esto.",
            parse_mode="HTML", reply_markup=None)
        return

    # -- Ciclaje: recibir la CANTIDAD de notas por corrida (numero manual) --
    if context.user_data.get("awaiting_ciclaje_n"):
        context.user_data.pop("awaiting_ciclaje_n")
        _num = "".join(ch for ch in text_in if ch.isdigit())
        if not _num or not (1 <= int(_num) <= 20):
            await update.message.reply_text(
                "Ese no parece un numero valido (1 a 20). Toca Notas de nuevo para reintentar.")
            return
        import sys as _sysn
        _sysn.path.insert(0, "/opt/me-harness")
        from agents import curador as _curn
        _curn.save_briefing_config({"n": int(_num)})
        await update.message.reply_text(
            _curn.ciclaje_texto(), parse_mode="HTML", reply_markup=_curn.ciclaje_kb())
        return

    # ── Frases: reemplazar los textos del header (kicker / etiqueta) y regenerar ──
    if context.user_data.get("awaiting_frase_header"):
        campo = context.user_data.pop("awaiting_frase_header")
        fp = context.user_data.get("frase_pending")
        if not fp:
            await update.message.reply_text("Error: no hay frase pendiente (arrancá con /frases).")
            return
        fp[campo] = text_in.strip()
        await update.message.reply_text("🎨 Regenerando placa…")
        try:
            from frases_gen import generate_frase_image
            img_bytes = await asyncio.to_thread(
                generate_frase_image, fp["texto"], fp.get("kicker"), fp.get("tag"))
        except Exception as e:
            await update.message.reply_text(f"❌ Error generando imagen: {e}")
            return
        fp["img_bytes"] = img_bytes
        context.user_data["frase_pending"] = fp
        bio = io.BytesIO(img_bytes)
        bio.name = "frase.png"
        await update.message.reply_photo(
            photo=bio,
            caption=f"💬 *{md_escape(fp['texto'])}*\n\n_Elegí las redes y acción:_",
            parse_mode="Markdown",
            reply_markup=_build_frase_kb(fp),
        )
        return

    # ── Eventos: insumos del Editor para la nota principal → finalizar la campaña ──
    if context.user_data.get("awaiting_evt_insumos"):
        evid = context.user_data.pop("awaiting_evt_insumos")
        await update.message.reply_text(
            "⏳ Componiendo la nota principal + armando el newsletter y el copy de redes…")
        import sys as _sev2
        _sev2.path.insert(0, "/opt/me-harness"); _sev2.path.insert(0, "/opt/me-harness/agents")
        try:
            import eventos as _ev
            res = await asyncio.to_thread(_ev.finalizar_campania, evid, text_in)
            if res.get("ok"):
                nl = res.get("newsletter", {}); np = res.get("nota_principal") or {}
                msg = f"✅ Campaña {evid} armada — newsletter DRAFT #{nl.get('campaign_id')}."
                if np.get("url"):
                    msg += f"\n📝 Nota principal: {np['url']}"
                await update.message.reply_text(msg, disable_web_page_preview=True)
            else:
                await update.message.reply_text(
                    f"❌ No se pudo armar: {res.get('error') or res.get('newsletter')}")
        except Exception as e:
            await update.message.reply_text(f"❌ Error: {str(e)[:150]}")
        return

    # ── Boletín semanal a Lectores: conversación (pregunta → opciones → slugs) ──
    if context.user_data.get("awaiting_bol_pregunta"):
        context.user_data.pop("awaiting_bol_pregunta")
        context.user_data["bol_pregunta"] = text_in
        context.user_data["awaiting_bol_opciones"] = True
        await update.message.reply_text(
            "📊 Ahora las OPCIONES de la encuesta, una por línea (2 a 4).\n\n"
            "Ej:\nLa designación del jefe de Gabinete\nLa subida del dólar\nLos vencimientos de ARCA")
        return
    if context.user_data.get("awaiting_bol_opciones"):
        context.user_data.pop("awaiting_bol_opciones")
        ops = [l.strip(" -•\t") for l in text_in.split("\n") if l.strip(" -•\t")]
        if len(ops) < 2:
            context.user_data["awaiting_bol_opciones"] = True
            await update.message.reply_text("Necesito al menos 2 opciones (una por línea). Reenviá:")
            return
        context.user_data["bol_opciones"] = ops
        context.user_data["awaiting_bol_slugs"] = True
        await update.message.reply_text(
            f"✅ {len(ops)} opciones cargadas.\n📰 Ahora pasame los SLUGS o URLs de las notas, "
            "en orden, separados por coma.")
        return
    if context.user_data.get("awaiting_bol_slugs"):
        context.user_data.pop("awaiting_bol_slugs")
        context.user_data["bol_slugs"] = text_in
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("👥 Base completa", callback_data="h_bol_pub:base")],
            [InlineKeyboardButton("👀 Lectores (abren)", callback_data="h_bol_pub:lectores")],
            [InlineKeyboardButton("⚡ Activos (clickean)", callback_data="h_bol_pub:activos")]])
        await update.message.reply_text(
            "🎯 ¿A qué público de la base lo mando?", reply_markup=kb)
        return
    if context.user_data.get("awaiting_bol_hora"):
        import re as _reh
        info = context.user_data.get("awaiting_bol_hora")
        m = _reh.match(r'^(\d{1,2}):(\d{2})$', text_in.strip())
        if not m:
            await update.message.reply_text("Hora inválida. Escribila como HH:MM (ej: 14:00):")
            return
        hh, mm = int(m.group(1)), int(m.group(2))
        # Guard: hora ya pasada hoy → NO disparar; pedir una futura (o usar "Enviar ahora").
        from datetime import datetime as _dtb, timezone as _tzb, timedelta as _tdb
        _now_ar = _dtb.now(_tzb.utc) - _tdb(hours=3)
        if (hh, mm) <= (_now_ar.hour, _now_ar.minute):
            await update.message.reply_text(
                f"⚠️ Las {hh:02d}:{mm:02d} ya pasaron (ahora {_now_ar.hour:02d}:{_now_ar.minute:02d}).\n"
                "Mandá una hora FUTURA de hoy, o volvé a la tarjeta y tocá «✅ Enviar ahora».")
            return   # sigue esperando la hora
        context.user_data.pop("awaiting_bol_hora")
        # Guardar hora y volver al menú: elegir la hora NO agenda nada. Solo «Confirmar envío»
        # dispara el schedule. Podés cambiar la hora o cancelar sin mandar (pedido Leo 10/8).
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton(f"✅ Confirmar envío {hh:02d}:{mm:02d}",
                                  callback_data=f"h_bol_confirm:{info['cid']}:{info['publico']}:{hh:02d}{mm:02d}")],
            [InlineKeyboardButton("🕐 Cambiar hora",
                                  callback_data=f"h_bol_horaask:{info['cid']}:{info['publico']}")],
            [InlineKeyboardButton("❌ Cancelar", callback_data=f"h_bol_cancel:{info['cid']}")]])
        await update.message.reply_text(
            f"🗓️ <b>Boletín #{info['cid']}</b> — hora elegida: <b>{hh:02d}:{mm:02d}</b> ({info['publico']})\n"
            f"No se manda hasta que toques <b>Confirmar envío</b>. Podés cambiar la hora o cancelar.",
            reply_markup=kb, parse_mode="HTML")
        return

    # ── /encuesta: pregunta → opciones (2-6) → notas (1-4) → redes/acción ───────
    if context.user_data.get("awaiting_enc_pregunta"):
        context.user_data.pop("awaiting_enc_pregunta")
        context.user_data.setdefault("enc", {"canales": {"tg": True, "x": True, "li": True, "fb": True, "ig": True, "wa": True}})
        context.user_data["enc"]["pregunta"] = text_in.strip()
        context.user_data["awaiting_enc_opciones"] = True
        await update.message.reply_text(
            "📊 Ahora las <b>OPCIONES</b>, una por línea (2 a 6).\n\n"
            "Ej:\nSí, ya frené\nAjusto precios\nTodavía aguanto", parse_mode="HTML")
        return
    if context.user_data.get("awaiting_enc_opciones"):
        ops = [l.strip(" -•\t") for l in text_in.split("\n") if l.strip(" -•\t")]
        if not (2 <= len(ops) <= 6):
            await update.message.reply_text("Necesito entre 2 y 6 opciones (una por línea). Reenviá:")
            return
        context.user_data.pop("awaiting_enc_opciones")
        context.user_data["enc"]["opciones"] = ops
        context.user_data["awaiting_enc_notas"] = True
        await update.message.reply_text(
            f"✅ {len(ops)} opciones.\n📰 Ahora 1 a 4 <b>NOTAS</b> relacionadas (links o slugs, "
            "separados por coma).\nMandá «-» si no querés notas.", parse_mode="HTML")
        return
    if context.user_data.get("awaiting_enc_notas"):
        context.user_data.pop("awaiting_enc_notas")
        raw = text_in.strip()
        notas = [] if raw.lower() in ("-", "no", "none", "") else [x.strip() for x in raw.split(",") if x.strip()][:4]
        fp = context.user_data.get("enc", {})
        fp["notas"] = notas
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("🖼️ Mando una foto", callback_data="enc_foto_manual")],
            [InlineKeyboardButton("🤖 Que la elija el agente", callback_data="enc_foto_auto")]])
        await update.message.reply_text(
            f"🗳️ {len(fp.get('opciones',[]))} opciones · {len(notas)} nota(s).\n📷 ¿Foto de la nota?",
            reply_markup=kb)
        return
    if context.user_data.get("awaiting_enc_hora"):
        import re as _reh
        from datetime import datetime as _dte, timezone as _tze, timedelta as _tde
        _AR = _tze(_tde(hours=-3))
        s = text_in.strip()
        now = _dte.now(_AR)
        m2 = _reh.match(r'^(\d{1,2})/(\d{1,2})(?:/(\d{2,4}))?\s+(\d{1,2}):(\d{2})$', s)
        m1 = _reh.match(r'^(\d{1,2}):(\d{2})$', s)
        if m2:
            d, mo = int(m2.group(1)), int(m2.group(2))
            y = int(m2.group(3) or now.year); y = y + 2000 if y < 100 else y
            hh, mm = int(m2.group(4)), int(m2.group(5))
            try:
                target = _dte(y, mo, d, hh, mm, tzinfo=_AR)
            except ValueError:
                await update.message.reply_text("Fecha inválida. Reenviá DD/MM HH:MM:"); return
        elif m1:
            hh, mm = int(m1.group(1)), int(m1.group(2))
            target = now.replace(hour=hh, minute=mm, second=0, microsecond=0)
        else:
            await update.message.reply_text("Formato inválido. DD/MM HH:MM (o HH:MM para hoy):"); return
        if target <= now + _tde(minutes=2):
            await update.message.reply_text("⚠️ Esa hora ya pasó. Mandá una futura, o volvé y tocá «🚀 Publicar ahora».")
            return
        context.user_data.pop("awaiting_enc_hora")
        fp = context.user_data.get("enc")
        if not fp:
            await update.message.reply_text("⚠️ Se perdió la encuesta. Reiniciá con /encuesta."); return
        await update.message.reply_text(f"⏳ Programando para {target.strftime('%d/%m %H:%M')}…")
        txt, r = await _do_encuesta_publish(context, fp, target, chat_id=update.message.chat_id)
        nl_on = fp.get("canales", {}).get("nl")
        if nl_on and r and r.get("ok"):
            context.user_data["enc_nl"] = {"enc": r["enc"], "pregunta": fp["pregunta"],
                                           "opciones": fp["opciones"], "notas": fp.get("notas"),
                                           "nota_url": r.get("wp_url")}   # → botones del mail a la nota-landing
            context.user_data.pop("enc", None)
            await update.message.reply_text(txt + "\n\n📧 Newsletter — ¿a qué base?",
                                            reply_markup=_enc_nl_base_kb())
            return
        context.user_data.pop("enc", None)
        await update.message.reply_text(txt, disable_web_page_preview=True)
        return

    # ── /encuesta → Newsletter: asunto custom + hora ────────────────────────────
    if context.user_data.get("awaiting_enc_nl_asunto"):
        context.user_data.pop("awaiting_enc_nl_asunto")
        nl = context.user_data.get("enc_nl")
        if not nl:
            await update.message.reply_text("⚠️ Se perdió el newsletter."); return
        nl["subject"] = text_in.strip()[:120]
        await update.message.reply_text(
            f"✅ Asunto: «{nl['subject']}». ¿Cuándo lo mando?",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("✅ Enviar ahora", callback_data="enc_nl_send")],
                [InlineKeyboardButton("📅 Programar hora", callback_data="enc_nl_sched")],
                [InlineKeyboardButton("❌ Cancelar", callback_data="enc_nl_cancel")]]))
        return
    if context.user_data.get("awaiting_enc_nl_hora"):
        import re as _rehn
        from datetime import datetime as _dtn, timezone as _tzn, timedelta as _tdn
        _ARn = _tzn(_tdn(hours=-3)); s = text_in.strip(); now = _dtn.now(_ARn)
        m2 = _rehn.match(r'^(\d{1,2})/(\d{1,2})(?:/(\d{2,4}))?\s+(\d{1,2}):(\d{2})$', s)
        m1 = _rehn.match(r'^(\d{1,2}):(\d{2})$', s)
        if m2:
            d, mo = int(m2.group(1)), int(m2.group(2)); y = int(m2.group(3) or now.year); y = y + 2000 if y < 100 else y
            try:
                target = _dtn(y, mo, d, int(m2.group(4)), int(m2.group(5)), tzinfo=_ARn)
            except ValueError:
                await update.message.reply_text("Fecha inválida. DD/MM HH:MM:"); return
        elif m1:
            target = now.replace(hour=int(m1.group(1)), minute=int(m1.group(2)), second=0, microsecond=0)
        else:
            await update.message.reply_text("Formato inválido. DD/MM HH:MM (o HH:MM):"); return
        if target <= now + _tdn(minutes=2):
            await update.message.reply_text("⚠️ Esa hora ya pasó. Mandá una futura."); return
        context.user_data.pop("awaiting_enc_nl_hora")
        nl = context.user_data.get("enc_nl")
        if not nl:
            await update.message.reply_text("⚠️ Se perdió el newsletter."); return
        await update.message.reply_text(f"⏳ Programando el newsletter para {target.strftime('%d/%m %H:%M')}…")
        txt = await _do_enc_newsletter(context, nl, target.strftime("%Y-%m-%d %H:%M:%S"))
        context.user_data.pop("enc_nl", None)
        await update.message.reply_text(txt, disable_web_page_preview=True)
        return

    # ── /notamanual: enfoque manual / ajuste del enfoque (flujo estilo ME) ─────
    if context.user_data.pop("awaiting_nm_enfoque", None):
        context.user_data["nm_enfoque"] = text_in.strip()
        context.user_data.pop("nm_capa", None)
        await _nm_estilo_procesar(update, context)
        return
    if context.user_data.pop("awaiting_nm_ajuste", None):
        _prev = context.user_data.get("nm_enfoque") or ""
        context.user_data["nm_enfoque"] = (_prev + "\nAJUSTE del editor (prioridad): "
                                           + text_in.strip()).strip()
        await _nm_estilo_procesar(update, context)
        return
    # ── /notamanual: columna de autor pegada como texto (o LINK de video IG/YT) ─
    if context.user_data.get("awaiting_nota_manual"):
        context.user_data.pop("awaiting_nota_manual")
        _mv = _NM_VIDLINK_RE.match(text_in or "")
        if _mv:
            await _nm_from_video_link(update, context, _mv.group(1))
            return
        await _process_nota_manual(update, context, text_in)
        return
    # ── Editor: esperando nuevo nombre para renombrar tema ───────────────────
    rename_id = context.user_data.pop("edito_rename_id", None)
    if rename_id:
        await asyncio.to_thread(_edito_rename_tema, rename_id, text_in)
        t         = await asyncio.to_thread(_edito_get_tema, rename_id)
        nsub      = await asyncio.to_thread(_edito_count_subtemas, rename_id)
        parent_id = t.get("parent_id") if t else None
        pnombre   = ""
        if parent_id:
            p = await asyncio.to_thread(_edito_get_tema, parent_id)
            pnombre = p["nombre"] if p else ""
        await update.message.reply_text(
            _edito_tema_detail_text(t, nsub, pnombre),
            parse_mode="HTML",
            reply_markup=_edito_tema_detail_kb(rename_id, nsub, parent_id),
        )
        return

    # ── Editor: esperando nombre del nuevo tema ──────────────────────────────
    if context.user_data.pop("edito_add_tema", None):
        context.user_data["edito_add_nombre"] = text_in
        await update.message.reply_text(
            f"➕ <b>{text_in}</b>\n\nElegí el nivel temporal:",
            parse_mode="HTML",
            reply_markup=_edito_add_nivel_kb(),
        )
        return

    # ── Harness: esperando título exacto para nota del BRIEFING (lo bloquea) ─
    if context.user_data.get("awaiting_brief_title_for"):
        new_title = text_in.strip()
        if _looks_like_url(new_title):
            await update.message.reply_text(
                "⚠️ Eso es un link, no un título. Escribí el <b>título</b> de la nota (texto). "
                "Si querés agregar un video, avisame aparte.", parse_mode="HTML")
            return  # sigue esperando el título (no lo pop-eamos)
        job_id = context.user_data.pop("awaiting_brief_title_for")
        import json as _js, sqlite3 as _sq
        try:
            with _sq.connect("/opt/me-harness/harness.db") as _c:
                row = _c.execute("SELECT title, content_json FROM jobs WHERE id=?", (job_id,)).fetchone()
                if row:
                    cj = _js.loads(row[1] or "{}")
                    if "original_title" not in cj:
                        cj["original_title"] = row[0] or ""
                    cj["title"] = new_title
                    cj["title_locked"] = True
                    _c.execute(
                        "UPDATE jobs SET title=?, content_json=?, updated_at=datetime('now') WHERE id=?",
                        (new_title, _js.dumps(cj), job_id))
            await update.message.reply_text(
                f"✏️ Título fijado y <b>bloqueado</b> — nota #{job_id}:\n<b>{new_title}</b>\n"
                f"El redactor lo usa tal cual.", parse_mode="HTML")
        except Exception as _e:
            await update.message.reply_text(f"❌ Error: {_e}")
        return

    # ── Harness: esperando fecha para programar nota desde el BRIEFING ──────
    # Guarda el override en content_json (pub_dest_override='fecha' + fecha); el
    # agente cola lo prioriza cuando Leo aprueba la nota. No confirma todavía.
    if context.user_data.get("awaiting_brief_prog_for"):
        job_id = context.user_data.pop("awaiting_brief_prog_for")
        import re as _re, json as _js, sqlite3 as _sq
        from datetime import datetime as _dt, timedelta as _td
        try:
            txt = text_in.strip().lower()
            now = _dt.now(); pub_dt = None
            if "mañana" in txt or "manana" in txt:
                base = now + _td(days=1)
                m = _re.search(r'(\d{1,2}):(\d{2})', txt)
                if m:
                    pub_dt = base.replace(hour=int(m.group(1)), minute=int(m.group(2)),
                                          second=0, microsecond=0)
            if not pub_dt:
                m = _re.search(r'(\d{1,2})/(\d{1,2})(?:/(\d{4}))?\s+(\d{1,2}):(\d{2})', txt)
                if m:
                    d, mo, yr, hh, mm = int(m.group(1)), int(m.group(2)), m.group(3), int(m.group(4)), int(m.group(5))
                    pub_dt = _dt(int(yr) if yr else now.year, mo, d, hh, mm)
            if not pub_dt:
                await update.message.reply_text(
                    "⚠️ No entendí la fecha. Usá: <code>30/05 18:30</code>", parse_mode="HTML")
                return
            pub_date_str = pub_dt.strftime("%Y-%m-%dT%H:%M:00")
            with _sq.connect("/opt/me-harness/harness.db") as _c:
                row = _c.execute("SELECT content_json FROM jobs WHERE id=?", (job_id,)).fetchone()
                st = _js.loads(row[0]) if row and row[0] else {}
                st["pub_dest_override"] = "fecha"
                st["pub_date_override"] = pub_date_str
                _c.execute("UPDATE jobs SET content_json=? WHERE id=?", (_js.dumps(st), job_id))
            await update.message.reply_text(
                f"🗓 Programada para <b>{pub_dt.strftime('%d/%m %H:%M')}</b> — nota #{job_id}.\n"
                f"Aprobala con ✅ Publicar y sale en esa fecha.", parse_mode="HTML")
        except Exception as _e:
            await update.message.reply_text(f"❌ Error: {_e}")
        return

    # ── Harness: esperando fecha de programación para nota en cola ──────────
    if context.user_data.get("awaiting_cola_prog_for"):
        job_id = context.user_data.pop("awaiting_cola_prog_for")
        import sys as _sys, re as _re
        from datetime import datetime as _dt, timedelta as _td
        _sys.path.insert(0, "/opt/me-harness")
        try:
            import broker as _br_prog
            txt = text_in.strip().lower()
            now = _dt.now()
            pub_dt = None

            # Parsear "mañana HH:MM"
            if "mañana" in txt or "manana" in txt:
                base = now + _td(days=1)
                m = _re.search(r'(\d{1,2}):(\d{2})', txt)
                if m:
                    pub_dt = base.replace(hour=int(m.group(1)), minute=int(m.group(2)),
                                          second=0, microsecond=0)
            # Parsear "DD/MM HH:MM" o "DD/MM/YYYY HH:MM"
            if not pub_dt:
                m = _re.search(r'(\d{1,2})/(\d{1,2})(?:/(\d{4}))?\s+(\d{1,2}):(\d{2})', txt)
                if m:
                    d, mo, yr, hh, mm = (int(m.group(i)) for i in range(1, 6))
                    yr = yr or now.year
                    pub_dt = _dt(yr, mo, d, hh, mm)

            if not pub_dt:
                await update.message.reply_text(
                    "⚠️ No entendí la fecha. Usá formato: <code>30/05 18:30</code>",
                    parse_mode="HTML"
                )
                return

            pub_date_str = pub_dt.strftime("%Y-%m-%dT%H:%M:00")
            _br_prog.confirm_cola(job_id, "fecha", pub_date=pub_date_str)
            await update.message.reply_text(
                f"🗓 Programado para <b>{pub_dt.strftime('%d/%m %H:%M')}</b> — job #{job_id} → redacción.",
                parse_mode="HTML"
            )
        except Exception as _e:
            await update.message.reply_text(f"❌ Error: {_e}")
        return

    # ── Harness: ajustar etiquetas desde panel de cola (Volver → cola) ─────
    if context.user_data.get("awaiting_cola_kw_for"):
        job_id        = context.user_data.pop("awaiting_cola_kw_for")
        panel_msg_id  = context.user_data.pop("awaiting_cola_kw_panel_msg_id",  None)
        panel_chat_id = context.user_data.pop("awaiting_cola_kw_panel_chat_id", None)
        prompt_msg_id = context.user_data.pop("awaiting_cola_kw_prompt_msg_id", None)
        import sys as _sys, sqlite3 as _sq, json as _js, re as _re
        _sys.path.insert(0, "/opt/me-harness")
        try:
            import broker as _br_ckw
            kws = [k.strip().lower() for k in _re.split(r'[,.]| - ', text_in)
                   if k.strip() and len(k.strip()) > 1]
            if kws:
                with _sq.connect("/opt/me-harness/harness.db") as _c:
                    row = _c.execute("SELECT content_json FROM jobs WHERE id=?", (job_id,)).fetchone()
                    state = _js.loads(row[0]) if row and row[0] else {}
                    current_kw = state.get("matched_kw", [])
                    for kw in kws:
                        if kw not in current_kw:
                            current_kw.append(kw)
                        _br_ckw.update_keyword_weight(kw, +0.5)
                    state["matched_kw"] = current_kw
                    _c.execute("UPDATE jobs SET content_json=? WHERE id=?",
                               (_js.dumps(state), job_id))
                # Borrar el prompt
                if prompt_msg_id:
                    try:
                        await context.bot.delete_message(
                            chat_id=update.message.chat_id, message_id=prompt_msg_id
                        )
                    except Exception:
                        pass
                # Actualizar el panel de keywords
                if panel_msg_id and panel_chat_id:
                    rows = []
                    for k in current_kw:
                        ww = _br_ckw.get_keyword_weight(k)
                        em = "🟢" if ww > 0.5 else ("🔴" if ww < -0.5 else "🟡")
                        rows.append([
                            {"text": f"{em} {k}  {ww:+.1f}",
                             "callback_data": f"h_cola_tags:{job_id}"},
                            {"text": "👍",  "callback_data": f"h_cola_kw_up:{job_id}:{k}"},
                            {"text": "👎",  "callback_data": f"h_cola_kw_dn:{job_id}:{k}"},
                            {"text": "✖️",  "callback_data": f"h_cola_kw_rm:{job_id}:{k}"},
                        ])
                    rows.append([{"text": "✏️ Agregar etiquetas",
                                  "callback_data": f"h_cola_kw_add:{job_id}"}])
                    rows.append([{"text": "↩ Volver",
                                  "callback_data": f"h_cola_back:{job_id}"}])
                    try:
                        await context.bot.edit_message_reply_markup(
                            chat_id=panel_chat_id, message_id=panel_msg_id,
                            reply_markup={"inline_keyboard": rows}
                        )
                    except Exception:
                        pass
            else:
                await update.message.reply_text("⚠️ No se reconoció ninguna etiqueta.")
        except Exception as _e:
            await update.message.reply_text(f"❌ Error: {_e}")
        return

    # ── Harness: esperando etiquetas para nota en cola de publicación ──────
    if context.user_data.get("awaiting_cola_tags_for"):
        job_id = context.user_data.pop("awaiting_cola_tags_for")
        import sys as _sys, sqlite3 as _sq, json as _js, re as _re
        _sys.path.insert(0, "/opt/me-harness")
        try:
            tags = [t.strip().lower() for t in _re.split(r'[,.]| - ', text_in)
                    if t.strip() and len(t.strip()) > 1]
            with _sq.connect("/opt/me-harness/harness.db") as _c:
                row = _c.execute("SELECT content_json FROM jobs WHERE id=?", (job_id,)).fetchone()
                state = _js.loads(row[0]) if row and row[0] else {}
                state["matched_kw"] = tags
                _c.execute("UPDATE jobs SET content_json=? WHERE id=?", (_js.dumps(state), job_id))
            await update.message.reply_text(
                f"🏷 Etiquetas guardadas: <code>{', '.join(t.title() for t in tags)}</code>",
                parse_mode="HTML"
            )
        except Exception as _e:
            await update.message.reply_text(f"❌ Error: {_e}")
        return

    # ── Harness: esperando keyword manual para agregar al panel de keywords ──
    if context.user_data.get("awaiting_kw_for"):
        job_id        = context.user_data.pop("awaiting_kw_for")
        panel_msg_id  = context.user_data.pop("awaiting_kw_panel_msg_id",  None)
        panel_chat_id = context.user_data.pop("awaiting_kw_panel_chat_id", None)
        prompt_msg_id = context.user_data.pop("awaiting_kw_prompt_msg_id", None)
        import sys as _sys, sqlite3 as _sq, json as _js, re as _re
        _sys.path.insert(0, "/opt/me-harness")
        try:
            import broker as _br_kw
            kws = [k.strip().lower() for k in _re.split(r'[,.]| - ', text_in)
                   if k.strip() and len(k.strip()) > 1]
            if kws:
                with _sq.connect("/opt/me-harness/harness.db") as _c:
                    row = _c.execute("SELECT content_json FROM jobs WHERE id=?", (job_id,)).fetchone()
                    state = _js.loads(row[0]) if row and row[0] else {}
                    current_kw = state.get("matched_kw", [])
                    for kw in kws:
                        if kw not in current_kw:
                            current_kw.append(kw)
                        _br_kw.update_keyword_weight(kw, +0.5)
                    state["matched_kw"] = current_kw
                    _c.execute("UPDATE jobs SET content_json=? WHERE id=?",
                               (_js.dumps(state), job_id))
                # Borrar el prompt
                if prompt_msg_id:
                    try:
                        await context.bot.delete_message(
                            chat_id=update.message.chat_id, message_id=prompt_msg_id
                        )
                    except Exception:
                        pass
                # Actualizar el panel de keywords con las nuevas etiquetas
                if panel_msg_id and panel_chat_id:
                    rows = []
                    for k in current_kw:
                        ww = _br_kw.get_keyword_weight(k)
                        em = "🟢" if ww > 0.5 else ("🔴" if ww < -0.5 else "🟡")
                        rows.append([
                            {"text": f"{em} {k}  {ww:+.1f}",
                             "callback_data": f"h_cur_kw:{job_id}"},
                            {"text": "👍",  "callback_data": f"h_cola_kw_up:{job_id}:{k}"},
                            {"text": "👎",  "callback_data": f"h_cola_kw_dn:{job_id}:{k}"},
                            {"text": "✖️",  "callback_data": f"h_cola_kw_rm:{job_id}:{k}"},
                        ])
                    rows.append([{"text": "✏️ Agregar etiquetas",
                                  "callback_data": f"h_cur_kw_add:{job_id}"}])
                    rows.append([{"text": "↩ Volver",
                                  "callback_data": f"h_cur_volver:{job_id}"}])
                    try:
                        await context.bot.edit_message_reply_markup(
                            chat_id=panel_chat_id, message_id=panel_msg_id,
                            reply_markup={"inline_keyboard": rows}
                        )
                    except Exception:
                        pass
            else:
                await update.message.reply_text("⚠️ No se reconoció ninguna etiqueta.")
        except Exception as _e:
            await update.message.reply_text(f"❌ Error: {_e}")
        return

    if context.user_data.get("awaiting_tags_for"):
        job_id = context.user_data.pop("awaiting_tags_for")
        import sys as _sys, sqlite3 as _sq, json as _js
        _sys.path.insert(0, "/opt/me-harness")
        try:
            from agents import curador as _cur
            import re as _re
            tags = [t.strip() for t in _re.split(r'[,.]| - ', text_in) if t.strip()]
            with _sq.connect("/opt/me-harness/harness.db") as _c:
                row = _c.execute("SELECT content_json FROM jobs WHERE id=?", (job_id,)).fetchone()
                state = _js.loads(row[0]) if row and row[0] else {}
                state["tags"] = tags
                _c.execute("UPDATE jobs SET content_json=? WHERE id=?", (_js.dumps(state), job_id))
            # Actualizar tarjeta original
            card_msg_id = state.get("card_msg_id")
            if card_msg_id:
                try:
                    new_kb = _cur.build_card_keyboard(job_id, state)
                    await context.bot.edit_message_reply_markup(
                        chat_id=update.message.chat_id,
                        message_id=card_msg_id,
                        reply_markup=new_kb,
                    )
                except Exception:
                    pass
            tags_str = ", ".join(tags)
            await update.message.reply_text(f"🏷 Etiquetas guardadas: {tags_str}")
        except Exception as _e:
            await update.message.reply_text(f"❌ Error guardando etiquetas: {_e}")
        return

    # ── Harness: esperando instrucción editorial para nota del curador ──
    if context.user_data.get("awaiting_inst_for"):
        job_id = context.user_data.pop("awaiting_inst_for")
        import sys as _sys, sqlite3 as _sq, json as _js
        _sys.path.insert(0, "/opt/me-harness")
        try:
            import re as _re_inst
            from agents import curador as _cur
            _txt = text_in.strip()
            # Atajo: si arranca con "titulo/título/title", es un TÍTULO → fijar y
            # bloquear (no guardarlo como instrucción de enfoque). Cubre la costumbre de Leo.
            _is_title = bool(_re_inst.match(r'^(t[íi]tulo|title)\b', _txt, _re_inst.IGNORECASE))
            with _sq.connect("/opt/me-harness/harness.db") as _c:
                row = _c.execute("SELECT title, content_json FROM jobs WHERE id=?", (job_id,)).fetchone()
                state = _js.loads(row[1]) if row and row[1] else {}
                if _is_title:
                    _new_title = _re_inst.sub(r'^(t[íi]tulo|title)\s*:?\s*', '', _txt,
                                              flags=_re_inst.IGNORECASE).strip()
                    if _looks_like_url(_new_title):
                        await update.message.reply_text(
                            "⚠️ Eso es un link, no un título. Mandá el título como texto.")
                        return
                    if "original_title" not in state:
                        state["original_title"] = (row[0] or "") if row else ""
                    state["title"] = _new_title
                    state["title_locked"] = True
                    _c.execute("UPDATE jobs SET title=?, content_json=?, updated_at=datetime('now') WHERE id=?",
                               (_new_title, _js.dumps(state), job_id))
                else:
                    state["instructions"] = text_in
                    _c.execute("UPDATE jobs SET content_json=?, instructions=? WHERE id=?",
                               (_js.dumps(state), text_in, job_id))
            # Actualizar tarjeta original
            card_msg_id = state.get("card_msg_id")
            if card_msg_id:
                try:
                    new_kb = _cur.build_card_keyboard(job_id, state)
                    await context.bot.edit_message_reply_markup(
                        chat_id=update.message.chat_id,
                        message_id=card_msg_id,
                        reply_markup=new_kb,
                    )
                except Exception:
                    pass
            if _is_title:
                await update.message.reply_text(
                    f"✏️ Detecté un título → fijado y <b>bloqueado</b>:\n<b>{_new_title}</b>\n"
                    f"Usá ✅ Publicar para aprobar.", parse_mode="HTML")
            else:
                await update.message.reply_text("📝 Instrucción guardada. Usá ✅ Publicar para aprobar.")
        except Exception as _e:
            await update.message.reply_text(f"❌ Error guardando instrucción: {_e}")
        return

    # ── Si el bot espera hashtags nuevos ──
    if context.user_data.get("waiting_for_hashtags"):
        context.user_data["waiting_for_hashtags"] = False
        stored = context.user_data.get("published")
        if not stored:
            await update.message.reply_text("No hay nota activa.")
            return
        words = text_in.split()
        hashtags = " ".join(w if w.startswith("#") else f"#{w}" for w in words if w)
        suggested = context.user_data.pop("_ht_suggested_direct", None) or _build_hashtags(stored["data"])
        _save_ht_feedback(stored["data"], suggested, hashtags)
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

    # ── ECO: título alternativo ──
    if context.user_data.get("waiting_eco_title"):
        context.user_data["waiting_eco_title"] = False
        eco = context.user_data.get("eco")
        if eco:
            eco["alt_title"] = text_in
            context.user_data["eco"] = eco
            await update.message.reply_text(
                _eco_preview_text(eco), parse_mode="Markdown", reply_markup=_build_eco_kb(eco)
            )
        return

    # ── ECO: bajada alternativa ──
    if context.user_data.get("waiting_eco_bajada"):
        context.user_data["waiting_eco_bajada"] = False
        eco = context.user_data.get("eco")
        if eco:
            eco["alt_bajada"] = text_in
            context.user_data["eco"] = eco
            await update.message.reply_text(
                _eco_preview_text(eco), parse_mode="Markdown", reply_markup=_build_eco_kb(eco)
            )
        return

    # ── Si el bot espera la descripción de una fuente nueva ──
    if context.user_data.get("waiting_for_new_source"):
        context.user_data["waiting_for_new_source"] = False
        if not OPENAI_API_KEY:
            await update.message.reply_text(
                "❌ Necesito OPENAI_API_KEY configurada para parsear fuentes nuevas."
            )
            return
        msg = await update.message.reply_text("🤖 Parseando descripción con GPT…")
        try:
            source_data = await asyncio.to_thread(_parse_source_with_gpt, text_in)
        except Exception as e:
            await msg.edit_text(f"❌ Error: {e}")
            return
        if not source_data:
            await msg.edit_text(
                "❌ No pude parsear. Intentá de nuevo con más info "
                "(empezá por el dominio, ej. `lanueva.com.ar`).",
                parse_mode="Markdown",
            )
            return
        domain = source_data.pop("_domain", "")
        if not domain:
            await msg.edit_text("❌ No detecté el dominio. Empezá el mensaje con el dominio.")
            return
        ok = await asyncio.to_thread(_add_source, domain, source_data)
        if not ok:
            await msg.edit_text("❌ Error al guardar en el feedback store.")
            return
        # Mostrar resumen de lo guardado
        await msg.edit_text(
            f"✅ *Fuente agregada al repositorio*\n\n"
            f"*{md_escape(source_data.get('name', domain))}* `({md_escape(domain)})`\n\n"
            f"*Tipo:* {md_escape(source_data.get('tipo', '?'))}\n"
            f"*Distancia editorial:* {source_data.get('distancia_editorial', '?')}/10\n"
            f"*Confiabilidad:* {source_data.get('confiabilidad', '?')}/10\n\n"
            f"*Orientación:*\n_{md_escape(source_data.get('orientacion', '?'))}_\n\n"
            f"_Para ajustar: borrala con /fuentes y agregala de nuevo, "
            f"o editá `sources.json` directo._",
            parse_mode="Markdown",
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

    # ── Frase: cambiar hashtags para tweet inmediato ──
    if context.user_data.get("waiting_for_frase_ht"):
        context.user_data["waiting_for_frase_ht"] = False
        words = text_in.split()
        hashtags = " ".join(w if w.startswith("#") else f"#{w}" for w in words if w)
        context.user_data["frase_custom_ht"] = hashtags
        ft = context.user_data.get("frase_tweeting", {})
        frase    = ft.get("frase", "")
        post_url = ft.get("post_url", "")
        tweet_text = frase
        if post_url:
            tweet_text += f"\n\n{utm_url(post_url, 'twitter')}"
        tweet_text += f"\n\n{hashtags}"
        if len(tweet_text) > 280:
            tweet_text = frase[:200] + "…\n\n" + hashtags
        kb_tweet = InlineKeyboardMarkup([
            [
                InlineKeyboardButton("Twittear", callback_data="frase_tweet"),
                InlineKeyboardButton("No twittear", callback_data="frase_no_tweet"),
            ],
            [InlineKeyboardButton("Cambiar HT", callback_data="frase_change_ht")],
        ])
        await update.message.reply_text(
            f"— Preview del tweet —\n`{md_escape(tweet_text)}`",
            parse_mode="Markdown",
            reply_markup=kb_tweet,
        )
        return

    # ── Frase programada: cambiar hashtags pre-programación ──
    if context.user_data.get("waiting_for_frase_sched_ht"):
        context.user_data["waiting_for_frase_sched_ht"] = False
        words = text_in.split()
        hashtags = " ".join(w if w.startswith("#") else f"#{w}" for w in words if w)
        context.user_data["frase_sched_ht"] = hashtags
        fp    = context.user_data.get("frase_pending")
        frase = fp["texto"] if fp else ""
        await update.message.reply_text(
            f"💬 *{md_escape(frase)}*\n\n*🐦 Twitter — Hashtags:*\n`{md_escape(hashtags)}`\n\nConfirmá los hashtags antes de elegir cuándo publicar:",
            parse_mode="Markdown",
            reply_markup=_build_frase_sched_pre_ht_kb(),
        )
        return

    # ── Frase programada: hora personalizada ──
    if context.user_data.get("waiting_for_frase_custom_hour"):
        import re as _re
        m = _re.match(r"^(\d{1,2}):(\d{2})$", text_in.strip())
        if not m:
            await update.message.reply_text("No entendí la hora. Usá HH:MM (ej: 14:30).")
            return
        hour, minute = int(m.group(1)), int(m.group(2))
        if not (0 <= hour <= 23 and 0 <= minute <= 59):
            await update.message.reply_text("Hora inválida. Usá HH:MM entre 00:00 y 23:59.")
            return
        context.user_data["waiting_for_frase_custom_hour"] = False
        from datetime import datetime, timezone, timedelta
        tz_arg  = timezone(timedelta(hours=-3))
        now_arg = datetime.now(tz_arg)
        day_offset = context.user_data.get("frase_sched_day", 0)
        target = (now_arg + timedelta(days=day_offset)).replace(
            hour=hour, minute=minute, second=0, microsecond=0
        )
        if target <= now_arg + timedelta(minutes=5):
            target += timedelta(days=1)
        context.user_data["frase_sched_target"] = target.isoformat()
        day_label = target.strftime("%A %d/%m")
        kb_confirm = InlineKeyboardMarkup([
            [
                InlineKeyboardButton(
                    f"✅ Programar para el {day_label} {hour:02d}:{minute:02d}",
                    callback_data="fs_confirm_custom",
                ),
            ],
            [InlineKeyboardButton("↩️ Cancelar", callback_data="fs_custom")],
        ])
        await update.message.reply_text(
            f"📅 Confirmás programar para el *{day_label} a las {hour:02d}:{minute:02d}*?",
            parse_mode="Markdown",
            reply_markup=kb_confirm,
        )
        return

        if not plan["url"]:
            plan["url"] = old_plan.get("url", "")
        if not plan.get("raw_input"):
            plan["raw_input"] = old_plan.get("raw_input", "")
        context.user_data["cmd_c_plan"] = plan
        kb = InlineKeyboardMarkup([[
            InlineKeyboardButton("✅ Ejecutar", callback_data="cmdc_exec"),
            InlineKeyboardButton("✏️ Ajustar",  callback_data="cmdc_adjust"),
            InlineKeyboardButton("❌ Cancelar", callback_data="cmdc_cancel"),
        ]])
        await msg_edit.edit_text(_cmd_c_plan_text(plan), parse_mode="Markdown", reply_markup=kb)
        return

    # ── Corregir actualización pendiente ────────────────────────────────────

    # ── Reprogramar post WP: Leo escribe la nueva fecha ─────────────────────
    if context.user_data.get("awaiting_reschedule_wp_id"):
        wp_id_rs = context.user_data.pop("awaiting_reschedule_wp_id")
        context.user_data.pop("awaiting_reschedule_msg_id", None)
        import re as _re_rs, requests as _req_rs, base64 as _b64_rs
        from config import WP_URL as _WP_RS, WP_USER as _WPU_RS, WP_PASS as _WPP_RS
        raw_date = text_in.strip()
        # Parsear "DD/MM HH:MM" o ISO directo
        iso_date = None
        m_rs = _re_rs.match(r"(\d{1,2})/(\d{1,2})(?:/(\d{4}))?\s+(\d{1,2}):(\d{2})", raw_date)
        if m_rs:
            day, mon, yr, hh, mm = m_rs.groups()
            yr = yr or "2026"
            iso_date = f"{yr}-{int(mon):02d}-{int(day):02d}T{int(hh):02d}:{mm}:00"
        elif _re_rs.match(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}", raw_date):
            iso_date = raw_date
        if not iso_date:
            await update.message.reply_text(
                f"⚠️ Formato no reconocido: <code>{raw_date}</code>\n"
                f"Usá: <code>15/07 10:00</code>",
                parse_mode="HTML"
            )
            context.user_data["awaiting_reschedule_wp_id"] = wp_id_rs
            return
        _tok_rs = _b64_rs.b64encode(f"{_WPU_RS}:{_WPP_RS}".encode()).decode()
        r_rs = _req_rs.post(
            f"{_WP_RS}/wp-json/wp/v2/posts/{wp_id_rs}",
            headers={"Authorization": f"Basic {_tok_rs}", "Content-Type": "application/json"},
            json={"date": iso_date, "status": "future"}, timeout=15
        )
        if r_rs.ok:
            await update.message.reply_text(
                f"✅ WP #{wp_id_rs} reprogramado para <b>{iso_date.replace('T',' ')}</b>",
                parse_mode="HTML"
            )
        else:
            await update.message.reply_text(f"❌ Error WP {r_rs.status_code}: {r_rs.text[:100]}")
        return

    # ── URL de imagen para nota sin foto ────────────────────────────────────
    if context.user_data.get("awaiting_wm_foto_for"):
        _wmf_job = context.user_data.pop("awaiting_wm_foto_for")
        _u = text_in.strip()
        if not (_u.startswith("http://") or _u.startswith("https://")):
            context.user_data["awaiting_wm_foto_for"] = _wmf_job
            await update.message.reply_text("Mandá una URL válida (https://) o enviá la foto directamente.")
            return
        msg = await update.message.reply_text("⏳ Subiendo la foto (con watermark)…")
        try:
            import sys as _sy_wu, json as _js_wu, sqlite3 as _sq_wu
            _sy_wu.path.insert(0, "/opt/me-harness")
            import broker as _br_wu
            _job = _br_wu.get_job(_wmf_job)
            _cj = {}
            try: _cj = _js_wu.loads(_job.get("content_json") or "{}")
            except Exception: pass
            _wmon = not _cj.get("sin_watermark", False)
            mid = await asyncio.to_thread(upload_image, _u, "", _wmon)
        except Exception as e:
            await msg.edit_text(f"❌ No pude subir la foto: {e}")
            return
        if not mid:
            await msg.edit_text("❌ No pude subir esa imagen. Probá otra URL o enviá la foto.")
            return
        _cj["image_id_override"] = mid
        with _sq_wu.connect("/opt/me-harness/harness.db") as _c_wu:
            _c_wu.execute("UPDATE jobs SET content_json=? WHERE id=?", (_js_wu.dumps(_cj), _wmf_job))
        await msg.edit_text(f"✅ Foto cargada (#{mid}) para la nota #{_wmf_job}" + (" con watermark." if _wmon else " sin watermark."))
        return

    if context.user_data.get("awaiting_img_url_for"):
        job_id_iv = context.user_data.pop("awaiting_img_url_for")
        context.user_data.pop("awaiting_img_msg_id", None)
        img_url_iv = text_in.strip()
        if img_url_iv.startswith("http://") or img_url_iv.startswith("https://"):
            # Guardar en user_data — la URL puede ser muy larga para callback_data (límite 64 bytes)
            context.user_data[f"pending_img_url_{job_id_iv}"] = img_url_iv
            await update.message.reply_text(
                f"🟢 <b>URL recibida</b> — <code>{img_url_iv[:80]}</code>",
                parse_mode="HTML",
                reply_markup={"inline_keyboard": [
                    [{"text": "✅ Usar esta imagen",  "callback_data": f"h_img_use:{job_id_iv}"}],
                    [{"text": "🔗 Probar otra URL",   "callback_data": f"h_img_url:{job_id_iv}"},
                     {"text": "🔍 Buscar en ME.ar",   "callback_data": f"h_img_search:{job_id_iv}"}],
                ]}
            )
        else:
            await update.message.reply_text(
                f"🔴 <b>URL inválida</b> — debe empezar con https://\n<code>{img_url_iv[:80]}</code>",
                parse_mode="HTML",
                reply_markup={"inline_keyboard": [
                    [{"text": "🔗 Probar otra URL",  "callback_data": f"h_img_url:{job_id_iv}"},
                     {"text": "🔍 Buscar en ME.ar",  "callback_data": f"h_img_search:{job_id_iv}"}],
                    [{"text": "⏭ Publicar sin foto", "callback_data": f"h_img_skip:{job_id_iv}"}],
                ]}
            )
        return

    # ── Link manual sin título: Leo escribe el título ─────────────────────────
    if context.user_data.get("awaiting_manual_harness_title"):
        context.user_data.pop("awaiting_manual_harness_title")
        _url_mh    = context.user_data.pop("pending_manual_url", "")
        _exc_mh    = context.user_data.pop("pending_manual_excerpt", "")
        _hilo_mh   = context.user_data.pop("pending_manual_hilo", 2)
        _title_mh  = text_in.strip()
        import sys as _sys_mh
        _sys_mh.path.insert(0, "/opt/me-harness")
        # Si Leo pegó una URL como título, usarla como source_url y re-scrapear
        if _title_mh.startswith("http://") or _title_mh.startswith("https://"):
            _url_mh = _title_mh
            try:
                from agents.redactor import scrape as _scrape_mh2
                _scraped2 = await asyncio.to_thread(_scrape_mh2, _url_mh)
                _title_mh = (_scraped2.get("title") or "").strip()
                if not _title_mh:
                    # slug como fallback
                    _title_mh = _url_mh.rstrip("/").split("/")[-1].replace("-", " ").capitalize()
                if not _exc_mh:
                    _exc_mh = _scraped2.get("text", "")[:300]
            except Exception:
                _title_mh = _url_mh.rstrip("/").split("/")[-1].replace("-", " ").capitalize()
        try:
            import broker as _br_mh
            _content_mh = {"title": _title_mh, "excerpt": _exc_mh, "manual_submission": True,
                           "source_name": _url_mh.split("/")[2] if "/" in _url_mh else _url_mh}
            _jid_mh = _br_mh.enqueue("curado", source_url=_url_mh, title=_title_mh,
                                      content=_content_mh, score=8.0, hilo=_hilo_mh, force=True)
            from agents import curador as _cur_mh
            await asyncio.to_thread(_cur_mh.run_briefing_single, _jid_mh)
        except Exception as _e_mh:
            await update.message.reply_text(f"❌ Error: {_e_mh}")
        return

    # ── Cola: nuevo título ingresado por Leo ───────────────────────────────────
    if context.user_data.get("awaiting_cola_title_for"):
        new_title = text_in.strip()
        if _looks_like_url(new_title):
            await update.message.reply_text(
                "⚠️ Eso es un link, no un título. Escribí el título de la nota (texto).")
            return  # sigue esperando el título
        job_id    = context.user_data.pop("awaiting_cola_title_for")
        msg_id    = context.user_data.pop("awaiting_cola_title_msg_id", None)
        prompt_id = context.user_data.pop("awaiting_cola_title_prompt_id", None)
        import sys as _sys_ct, json as _js_ct
        _sys_ct.path.insert(0, "/opt/me-harness")
        try:
            import sqlite3 as _sq_ct
            _wp_pid_ct = None
            _stage_ct  = ""
            with _sq_ct.connect("/opt/me-harness/harness.db") as _c_ct:
                # Guardar título original antes de cambiar
                row_ct = _c_ct.execute(
                    "SELECT title, content_json, wp_post_id, stage FROM jobs WHERE id=?", (job_id,)
                ).fetchone()
                if row_ct:
                    orig_title = row_ct[0] or ""
                    cj_ct = _js_ct.loads(row_ct[1] or "{}")
                    _wp_pid_ct = row_ct[2]
                    _stage_ct  = row_ct[3] or ""
                    if "original_title" not in cj_ct:  # guardar solo la primera vez
                        cj_ct["original_title"] = orig_title
                    # Título de Leo = máxima prioridad: candado para que el redactor NO lo pise,
                    # y lo dejamos en content_json.title para que viaje con la nota.
                    cj_ct["title"]        = new_title
                    cj_ct["title_locked"] = True
                    _c_ct.execute(
                        "UPDATE jobs SET title=?, content_json=?, updated_at=datetime('now') WHERE id=?",
                        (new_title, _js_ct.dumps(cj_ct), job_id)
                    )
            # Si la nota YA está publicada, actualizar el post vivo en WP (sin tocar el slug → URL estable)
            _wp_updated = False
            if _wp_pid_ct and _stage_ct == "done":
                try:
                    # Sin [:60]: el ≤60 es dogma FALSO y cortaba el <title> a mitad de palabra
                    # justo en el camino por el que Leo ARREGLA un título malo. _meta_safe ya
                    # limpia los emojis de 4 bytes que rompen wp_postmeta. Ver [[title_policy]].
                    _wp_updated = await asyncio.to_thread(
                        update_post, int(_wp_pid_ct),
                        {"title": new_title, "meta": {"rank_math_title": _meta_safe(new_title)}}
                    )
                except Exception as _ewp:
                    logger.warning(f"update_post título #{job_id}: {_ewp}")
            if prompt_id:
                try: await context.bot.delete_message(update.message.chat_id, prompt_id)
                except Exception: pass
            _extra_ct = "\n\n🌐 Actualizado también en la web." if _wp_updated else ""
            await update.message.reply_text(
                f"✅ Título actualizado:\n<b>{new_title}</b>{_extra_ct}\n\n"
                f"<i>Usá '↩ Restaurar original' en el menú para volver al título anterior.</i>",
                parse_mode="HTML"
            )
        except Exception as _e_ct:
            await update.message.reply_text(f"❌ Error: {_e_ct}")
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

    # ── Tweet → nota (leo el tweet + su imagen, te pido la directiva) ─────────
    if kind == "tweet":
        await _start_tweet_nota(update, context, url)
        return

    # Check de duplicados: si el slug de la URL ya existe en WP, avisar y salir
    try:
        clean_url = url.split("?")[0].rstrip("/")
        candidate_slug = clean_url.split("/")[-1]
        if candidate_slug:
            dup_r = requests.get(
                f"{WP_URL}/wp-json/wp/v2/posts?slug={candidate_slug}&_fields=id,link",
                headers=wp_auth(), timeout=8
            )
            if dup_r.status_code == 200 and dup_r.json():
                dup = dup_r.json()[0]
                await update.message.reply_text(
                    f"⚠️ Esta nota ya está publicada:\n{dup['link']}\n\nSi querés editarla usá /editar."
                )
                return
    except Exception:
        pass  # Si falla el check, seguir normalmente

    # ── URLs externas → harness briefing (no pipeline viejo) ──────────────────
    # URLs de mundoempresarial.ar van al flujo de edición (/editar)
    # ── YouTube / Instagram → agente social (semáforos + briefing) ───────────
    if kind in ("youtube", "instagram"):
        import sys as _sys_soc
        _sys_soc.path.insert(0, "/opt/me-harness")
        try:
            from agents.social import analyze as _soc_analyze, format_panel as _soc_panel
            msg_soc = await update.message.reply_text("🔍 Analizando contenido...")
            data_soc = await asyncio.to_thread(_soc_analyze, url)
            # Encolar en curado para tener job_id
            import broker as _br_soc
            content_soc = {
                "title":       data_soc["title"],
                "text":        data_soc["text"],
                "excerpt":     data_soc["excerpt"],
                "image_url":   data_soc["image_url"],
                "source_name": data_soc["source"],
                "source_url":  data_soc["source_url"],
                "source":      data_soc["source_url"],
            }
            _job_soc = _br_soc.enqueue(
                "curado", source_url=data_soc["source_url"],
                title=data_soc["title"],
                content=content_soc,
                score=7.0,
                hilo=hilo_hint or 2,
            )
            panel_text, panel_kb = _soc_panel(data_soc, job_id=_job_soc)
            await msg_soc.delete()
            await update.message.reply_text(
                panel_text, parse_mode="HTML",
                reply_markup={"inline_keyboard": panel_kb},
                disable_web_page_preview=True,
            )
        except Exception as _e_soc:
            await update.message.reply_text(f"❌ Error analizando: {_e_soc}")
        return

    if "mundoempresarial.ar" not in url:
        import sys as _sys_hl
        _sys_hl.path.insert(0, "/opt/me-harness")
        try:
            import broker as _br_hl
            from agents.ingesta import score_article as _score_hl
            msg_hl = await update.message.reply_text("⏳ Leyendo artículo...")
            # Usa el scraper del bot (og:title + JSON-LD + fallbacks AMP/Wayback/GCache)
            scraped_hl = await asyncio.to_thread(scrape, url)
            title_hl   = (scraped_hl.get("title") or "").strip()
            if title_hl.lower() in ("sin título", "sin titulo", ""):
                title_hl = ""
            excerpt_hl = (scraped_hl.get("excerpt") or scraped_hl.get("text", "")[:300]).strip()
            text_hl    = scraped_hl.get("text", "")
            if not title_hl:
                context.user_data["pending_manual_url"]     = url
                context.user_data["pending_manual_excerpt"] = excerpt_hl
                context.user_data["pending_manual_hilo"]    = hilo_hint or (
                    3 if es_entrevista_opinion(url, "", text_hl) else 2)
                await msg_hl.edit_text(
                    "⚠️ No pude extraer el título. Escribí el título para esta nota:",
                    parse_mode="HTML"
                )
                context.user_data["awaiting_manual_harness_title"] = True
                return

            # Aviso de posible duplicado (no bloquea: Leo decide)
            try:
                dup_hl = await asyncio.wait_for(asyncio.to_thread(_dup_manual_hl, title_hl), timeout=10)
                if dup_hl:
                    _, dj = dup_hl
                    await update.message.reply_text(
                        f"⚠️ Posible duplicado: ya hay un job parecido en el pipeline:\n"
                        f"«{dj['title'][:90]}» (#{dj['id']}, {dj['stage']})\n"
                        f"La sigo procesando igual — si es la misma noticia, rechazá una de las dos.")
            except Exception:
                pass

            # Link MANUAL de Leo: NO calcular score_article (cargaba el cache de entidades
            # vía WP/Ferozo y se colgaba — freezes del 7/7). Leo ya curó la nota con mandarla:
            # score fijo alto. Todo en thread + timeout para que un cuelgue nunca trabe la fila.
            def _mk_job_hl():
                hilo = hilo_hint or (3 if es_entrevista_opinion(url, title_hl, text_hl) else 2)
                content = {"title": title_hl, "excerpt": excerpt_hl,
                           "source_name": url.split("/")[2] if "/" in url else url,
                           "manual_submission": True,
                           # 8000 (antes 3000): con poco contexto GPT alucina más — caso
                           # Ayerra 7/7 ("secretario de Macri" + "20 mil pymes" inventados)
                           "text": text_hl[:8000]}
                return _br_hl.enqueue("curado", source_url=url, title=title_hl,
                                      content=content, score=7.5, hilo=hilo, force=True)
            job_id_hl = await asyncio.wait_for(asyncio.to_thread(_mk_job_hl), timeout=60)
            await msg_hl.delete()
            from agents import curador as _cur_hl
            await asyncio.to_thread(_cur_hl.run_briefing_single, job_id_hl)
        except Exception as _e_hl:
            await update.message.reply_text(f"❌ Error al agregar al harness: {_e_hl}")
        return

    # URLs de mundoempresarial.ar → usar /editar para editar notas existentes
    await update.message.reply_text(
        f"Para editar una nota de ME.ar usá /editar\n{url}"
    )


async def _do_schedule(query, context, data, target):
    """Factoriza el flujo completo de programar una nota: colisión → WP → job_queue."""
    await query.edit_message_text("🔍 Verificando colisiones con otras notas programadas…")
    adjusted = await asyncio.to_thread(find_scheduled_collision, target)

    offset_msg = ""
    if adjusted != target:
        delta_min = int((adjusted - target).total_seconds() / 60)
        offset_msg = f" (ajustado +{delta_min} min para evitar colisión)"

    await query.edit_message_text(
        f"📤 Programando para *{adjusted.strftime('%A %d/%m %H:%M')}*{offset_msg}…",
        parse_mode="Markdown",
    )

    image_id = None
    if data.get("image_url"):
        kw = focus_keyword(data["title"])
        alt = f"{kw} - {get_title(data)}"
        image_id = await asyncio.to_thread(upload_image, data["image_url"], alt)

    destacado = context.user_data.get("dest_on", False)
    job_id = await asyncio.to_thread(_enqueue_to_harness, data, image_id, destacado, adjusted)

    context.user_data.pop("article", None)
    context.user_data.pop("pre_sched_hashtags", None)
    context.user_data.pop("sched_custom_day", None)
    context.user_data.pop("sched_custom_target", None)

    await query.edit_message_text(
        f"✅ *En cola del harness* — job \\#{job_id}\n"
        f"Programado: {adjusted.strftime('%A %d/%m a las %H:%M')}{offset_msg}\n"
        f"El publicador crea el post en WP y difunde a esa hora\\.",
        parse_mode="Markdown",
    )

    # ECO: si está activado, abrir menú de configuración del eco
    if eco_on:
        eco = {
            "post_id":    post_id,
            "wp_url":     post_url,
            "data":       dict(data),
            "alt_title":  None,
            "alt_bajada": None,
            "tw_on":      True,
            "tg_on":      True,
            "li_on":      False,
        }
        context.user_data["eco"] = eco
        await query.message.reply_text(
            _eco_preview_text(eco),
            parse_mode="Markdown",
            reply_markup=_build_eco_kb(eco),
        )


def _boletin_enviar_ahora(cid, publico):
    """Envía el boletín #cid YA (programa a la hora actual → sale de inmediato). (ok, texto)."""
    import sys as _s
    _s.path.insert(0, "/opt/me-harness"); _s.path.insert(0, "/opt/me-harness/agents")
    try:
        import newsletter as _nl
        from datetime import datetime, timezone, timedelta
        now_ar = datetime.now(timezone.utc) - timedelta(hours=3)
        sched = now_ar.strftime("%Y-%m-%d %H:%M:%S")
        r = _nl.schedule_send(cid, sched)
        if r.get("ok"):
            warn = ("\n⚠️ " + r["warn"]) if r.get("warn") else ""
            return True, (f"✅ <b>Boletín #{cid} ENVIADO</b> → {publico} ({r.get('recipients')} dest.){warn}\n"
                          f"A +24h: feedback del funnel + nutrir bases + mejoras.")
        return False, f"❌ No se pudo enviar #{cid}: {r.get('error')}"
    except Exception as e:
        return False, f"❌ Error enviando #{cid}: {str(e)[:160]}"


def _boletin_programar(cid, publico, hh, mm):
    """Programa el envío del boletín #cid HOY a las hh:mm (AR) al público dado. (ok, texto)."""
    import sys as _s
    _s.path.insert(0, "/opt/me-harness"); _s.path.insert(0, "/opt/me-harness/agents")
    try:
        import newsletter as _nl
        from datetime import datetime, timezone, timedelta
        now_ar = datetime.now(timezone.utc) - timedelta(hours=3)
        sched = now_ar.replace(hour=hh, minute=mm, second=0, microsecond=0).strftime("%Y-%m-%d %H:%M:%S")
        r = _nl.schedule_send(cid, sched)
        if r.get("ok"):
            warn = ("\n⚠️ " + r["warn"]) if r.get("warn") else ""
            return True, (f"✅ <b>Boletín #{cid} programado</b> → {publico} ({r.get('recipients')} dest.)\n"
                          f"🕐 Sale: {sched} (AR).\nA +24h: feedback del funnel + nutrir bases + mejoras.{warn}")
        return False, f"❌ No se pudo programar #{cid}: {r.get('error')}"
    except Exception as e:
        return False, f"❌ Error programando #{cid}: {str(e)[:160]}"


async def handle_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    try:
        await query.answer()
    except Exception as _qa_err:
        if "query is too old" in str(_qa_err).lower():
            return  # callback expirado, ignorar silenciosamente
        raise

    # ── /pipeline callbacks ───────────────────────────────────────────────────
    if query.data == "pip_close":
        try:
            await query.message.delete()
        except Exception:
            pass
        await query.answer()
        return

    if query.data == "pip_refresh":
        try:
            text, counts = await asyncio.to_thread(_pipeline_stats)
            kb = _pipeline_kb(counts)
            await query.edit_message_text(text, parse_mode="HTML", reply_markup=kb)
        except Exception as _pe:
            await query.answer(f"Error: {_pe}", show_alert=True)
        return
    if query.data.startswith("pip_stage_"):
        stage_key = query.data[len("pip_stage_"):]
        if stage_key == "sin_imagen":
            # Flujo estándar: card con botones de imagen para cada nota sin foto
            import sqlite3 as _sq_si, json as _js_si
            with _sq_si.connect(_HDB) as _c_si_pip:
                _si_pip_rows = _c_si_pip.execute(
                    "SELECT id, title, source_url, content_json FROM jobs WHERE stage='sin_imagen' ORDER BY created_at"
                ).fetchall()
            if not _si_pip_rows:
                await query.answer("No hay notas sin imagen.", show_alert=True)
                return
            await query.answer(f"{len(_si_pip_rows)} nota(s) sin imagen", show_alert=False)
            for _si_pip_id, _si_pip_title, _si_pip_src, _si_pip_cj in _si_pip_rows:
                _si_pip_c = {}
                try: _si_pip_c = _js_si.loads(_si_pip_cj or "{}")
                except Exception: pass
                _si_pip_url = _si_pip_c.get("source") or _si_pip_c.get("source_url") or _si_pip_src or ""
                _si_pip_link = f'\n<a href="{_si_pip_url}">Ver fuente</a>' if _si_pip_url else ""
                await context.bot.send_message(
                    chat_id=query.message.chat_id,
                    text=f"🖼 <b>Sin imagen — elegí foto (job #{_si_pip_id})</b>\n\n<b>{(_si_pip_title or '')[:80]}</b>{_si_pip_link}",
                    parse_mode="HTML", disable_web_page_preview=True,
                    reply_markup={"inline_keyboard": [
                        [{"text": "🔍 Buscar en ME.ar", "callback_data": f"h_img_search:{_si_pip_id}"},
                         {"text": "🔗 Agregar URL",     "callback_data": f"h_img_url:{_si_pip_id}"}],
                        [{"text": "➖ Publicar sin foto", "callback_data": f"h_img_skip:{_si_pip_id}"}],
                        [{"text": "↩ Volver",            "callback_data": "pip_close"}],
                    ]}
                )
            return
        _PIP_ACTION_BTN = {
            "curado":      ("📰 Ir a Briefing",           "pip_cmd_briefing"),
            "cola":        ("📌 Ir a Cola de publicación", "h_cola_main_panel"),
            "ingesta":     ("📡 Ejecutar Ingesta",         "pip_cmd_ingesta"),
            "redaccion":   ("✍️ Procesar Redacción",       "pip_cmd_redaccion"),
            "programadas": ("📅 Ver Programadas",          "h_cola_ver_programadas"),
        }
        try:
            text = await asyncio.to_thread(_pipeline_stage_detail, stage_key)
            _volver_btn = {"text": "↩ Volver", "callback_data": "pip_close"}
            if stage_key in _PIP_ACTION_BTN:
                lbl, cb = _PIP_ACTION_BTN[stage_key]
                await query.message.reply_text(
                    text, parse_mode="HTML",
                    reply_markup={"inline_keyboard": [
                        [{"text": lbl, "callback_data": cb}],
                        [_volver_btn],
                    ]}
                )
            else:
                await query.message.reply_text(
                    text, parse_mode="HTML",
                    reply_markup={"inline_keyboard": [[_volver_btn]]}
                )
        except Exception as _pe:
            await query.answer(f"Error: {_pe}", show_alert=True)
        return

    # ── Pipeline — comandos de acción desde botones del stage ────────────────
    if query.data in ("pip_cmd_briefing", "pip_cmd_ingesta", "pip_cmd_redaccion"):
        import sys as _sys_pip
        _sys_pip.path.insert(0, "/opt/me-harness")
        if query.data == "pip_cmd_briefing":
            msg_pip = await context.bot.send_message(query.message.chat_id, "📰 Generando briefing…")
            try:
                from agents import curador as _cur_pip
                await asyncio.to_thread(_cur_pip.run_briefing, 0)
                await msg_pip.delete()
            except Exception as _e_pip:
                await msg_pip.edit_text(f"⚠️ Error en briefing: {_e_pip}")
        elif query.data == "pip_cmd_ingesta":
            msg_pip = await context.bot.send_message(query.message.chat_id, "📡 Corriendo ingesta RSS…")
            try:
                from agents import ingesta as _ing_pip
                n_pip = await asyncio.to_thread(_ing_pip.run)
                await msg_pip.edit_text(
                    f"✅ <b>Ingesta completada</b>\n{n_pip} notas nuevas encoladas.\nUsá /briefing para ver el briefing.",
                    parse_mode="HTML"
                )
            except Exception as _e_pip:
                await msg_pip.edit_text(f"⚠️ Error en ingesta: {_e_pip}")
        elif query.data == "pip_cmd_redaccion":
            msg_pip = await context.bot.send_message(query.message.chat_id, "✍️ Procesando redacción…")
            try:
                from agents import redactor as _red_pip
                await asyncio.to_thread(_red_pip.run_once)
                await msg_pip.edit_text("✅ Redacción procesada.")
            except Exception as _e_pip:
                await msg_pip.edit_text(f"⚠️ Error en redacción: {_e_pip}")
        await query.answer()
        return

    # ── Harness — Tips de corrección sobre la nota (TIPS_NOTA) ──────────────
    # h_tip_accept:{tip_id}  → Leo acepta la corrección (la hará manual)
    # h_tip_skip:{tip_id}    → Leo ignora el tip
    # h_tip_fix:{job_id}     → desde alerta score bajo, registra tarea
    # h_tip_qaok:{job_id}    → descarta alerta QA
    # ── Impacto (Fase 11): guardar/descartar aprendizajes propuestos al playbook ──
    if query.data.startswith("h_imp_save:") or query.data.startswith("h_imp_skip:"):
        import sys as _sysimp
        _sysimp.path.insert(0, "/opt/me-harness"); _sysimp.path.insert(0, "/opt/me-harness/agents")
        try:
            pi = query.data.split(":")
            acti = pi[0]; aidi = int(pi[1]); idxi = int(pi[2]) if len(pi) >= 3 else 0
            if acti == "h_imp_save":
                import impacto as _imp
                lec = _imp.aplicar_aprendizaje(aidi, idxi)
                footer = (f"\n\n✅ <b>Guardado #{idxi+1}</b> al playbook — la próxima campaña lo aplica."
                          if lec else f"\n\n⚠️ No se pudo guardar #{idxi+1}.")
            else:
                footer = f"\n\n❌ Descartado #{idxi+1}."
            kb = query.message.reply_markup.inline_keyboard if query.message.reply_markup else []
            nueva = [r for r in kb if not (r and (r[0].callback_data or "").endswith(f":{aidi}:{idxi}"))]
            base = query.message.text_html if query.message.text else (query.message.caption_html or "")
            await query.edit_message_text(
                base + footer, parse_mode="HTML",
                reply_markup=InlineKeyboardMarkup(nueva) if nueva else None,
                disable_web_page_preview=True)
        except Exception:
            try:
                await query.edit_message_reply_markup(None)
            except Exception:
                pass
        return

    # ── EDITOR (gran articulador): aplicar/descartar correcciones del esquema ──
    if query.data.startswith("h_edit_apply:") or query.data.startswith("h_edit_skip:"):
        import sys as _sysed
        _sysed.path.insert(0, "/opt/me-harness"); _sysed.path.insert(0, "/opt/me-harness/agents")
        try:
            pe = query.data.split(":")
            acte = pe[0]; eide = int(pe[1]); idxe = int(pe[2]) if len(pe) >= 3 else 0
            if acte == "h_edit_apply":
                import editor as _ed
                res = _ed.aplicar_correccion(eide, idxe)
                footer = (f"\n\n✅ <b>Aplicada #{idxe+1}</b> — {res}" if res
                          else f"\n\n⚠️ No se pudo aplicar #{idxe+1}.")
            else:
                footer = f"\n\n❌ Descartada #{idxe+1}."
            kb = query.message.reply_markup.inline_keyboard if query.message.reply_markup else []
            nueva = [r for r in kb if not (r and (r[0].callback_data or "").endswith(f":{eide}:{idxe}"))]
            base = query.message.text_html if query.message.text else (query.message.caption_html or "")
            await query.edit_message_text(
                base + footer, parse_mode="HTML",
                reply_markup=InlineKeyboardMarkup(nueva) if nueva else None,
                disable_web_page_preview=True)
        except Exception:
            try:
                await query.edit_message_reply_markup(None)
            except Exception:
                pass
        return

    # ── SUPERVISOR (mejora diaria del harness): aplicar/descartar propuesta ──
    # ── VENTANA de tareas que esperan decisión: avanzar / más tarde / saltear / no proponer más ──
    if query.data.startswith("h_vent_"):
        import sys as _sv
        _sv.path.insert(0, "/opt/me-harness"); _sv.path.insert(0, "/opt/me-harness/agents")
        try:
            pv = query.data.split(":")
            actv, tid = pv[0], (pv[1] if len(pv) > 1 else "")
            base = query.message.text_html if query.message.text else (query.message.caption_html or "")
            if actv == "h_vent_later":
                import ventana as _vn
                await query.edit_message_text(base + "\n\n⏰ ¿Para cuándo?", parse_mode="HTML",
                                              reply_markup=_vn.kb_later(tid))
                return
            if actv == "h_vent_snz":
                _h = int(pv[2]) if len(pv) > 2 else 2
                def _snz():
                    import ventana as _v1; return _v1.accion(tid, "snooze", _h)
                res = await asyncio.to_thread(_snz)
            elif actv == "h_vent_go":
                await query.answer("Avanzando…", show_alert=False)
                def _go():
                    import ventana as _v2; return _v2.accion(tid, "go")
                res = await asyncio.wait_for(asyncio.to_thread(_go), timeout=150)
            elif actv == "h_vent_skip":
                def _sk():
                    import ventana as _v3; return _v3.accion(tid, "skip")
                res = await asyncio.to_thread(_sk)
            else:  # h_vent_never
                def _nv():
                    import ventana as _v4; return _v4.accion(tid, "never")
                res = await asyncio.to_thread(_nv)
            # Se conservan los botones (pedido de Leo: poder volver / cambiar de opinión).
            await query.edit_message_text(base + f"\n\n{res}", parse_mode="HTML",
                                          reply_markup=query.message.reply_markup,
                                          disable_web_page_preview=True)
        except Exception as _ev:
            try:
                await query.answer(f"Error: {str(_ev)[:150]}", show_alert=True)
            except Exception:
                pass
        return

    # ── /rutina: chequeo de salud en un toque (semáforo en criollo) ──
    if query.data == "rutina_check":
        await query.answer("Chequeando…")
        try:
            txt = await asyncio.wait_for(asyncio.to_thread(_chequeo_rapido), timeout=45)
        except Exception as _erc:
            txt = f"⚠️ No pude chequear ahora ({str(_erc)[:70]}). Probá de nuevo."
        kb = InlineKeyboardMarkup([[InlineKeyboardButton("🔄 Chequear de nuevo", callback_data="rutina_check")]])
        try:
            await query.message.reply_text(txt, parse_mode="HTML", reply_markup=kb,
                                            disable_web_page_preview=True)
        except Exception:
            pass
        return

    # ── AUTO-PARCHE del supervisor: generar diff / aplicar+deploy / revertir (async, con rollback) ──
    if (query.data.startswith("h_sup_patch:") or query.data.startswith("h_sup_patchok:")
            or query.data.startswith("h_sup_revert:")):
        import sys as _spx
        _spx.path.insert(0, "/opt/me-harness"); _spx.path.insert(0, "/opt/me-harness/agents")
        ps = query.data.split(":")
        act = ps[0]
        base = query.message.text_html if query.message.text else (query.message.caption_html or "")
        kb_after = None
        try:
            if act == "h_sup_patch":
                pid = int(ps[1]); idx = int(ps[2])
                await query.answer("Generando el parche… (GPT lee el código)", show_alert=False)
                def _gen():
                    import supervisor as _s; return _s.generar_parche(pid, idx)
                res = await asyncio.wait_for(asyncio.to_thread(_gen), timeout=160)
                footer = ("\n\n🔧 Parche generado ⤵️ (revisalo y aplicá abajo)" if res and res.get("ok")
                          else f"\n\n⚠️ No pude generar un parche exacto: "
                               f"{res.get('motivo') if res else 'error'}\n(queda anotado en pendientes)")
                # NO borrar los botones del original: dejarlos para poder VOLVER (re-generar / ajustar / descartar)
                kb_after = query.message.reply_markup
            elif act == "h_sup_patchok":
                pid = int(ps[1]); idx = int(ps[2])
                await query.answer("Aplicando… compile · import · restart · health", show_alert=False)
                def _app():
                    import supervisor as _s; return _s.aplicar_parche(pid, idx)
                res = await asyncio.wait_for(asyncio.to_thread(_app), timeout=220)
                footer = f"\n\n{res.get('msg')}" if res else "\n\n⚠️ error aplicando"
                if res and res.get("ok") and res.get("patch_id"):
                    kb_after = InlineKeyboardMarkup([[InlineKeyboardButton(
                        "↩️ Revertir", callback_data=f"h_sup_revert:{res['patch_id']}")]])
            else:  # h_sup_revert
                pidr = int(ps[1])
                await query.answer("Revirtiendo + restart…", show_alert=False)
                def _rev():
                    import autopatch as _ap; return _ap.revert(pidr)
                res = await asyncio.wait_for(asyncio.to_thread(_rev), timeout=140)
                footer = f"\n\n{res.get('msg')}" if res else "\n\n⚠️ error revirtiendo"
            await query.edit_message_text(base + footer, parse_mode="HTML",
                                          reply_markup=kb_after, disable_web_page_preview=True)
        except Exception as _epx:
            try:
                await query.answer(f"Error: {str(_epx)[:150]}", show_alert=True)
            except Exception:
                pass
        return

    if query.data.startswith("h_sup_apply:") or query.data.startswith("h_sup_skip:"):
        import sys as _syssup
        _syssup.path.insert(0, "/opt/me-harness"); _syssup.path.insert(0, "/opt/me-harness/agents")
        try:
            ps = query.data.split(":")
            acts = ps[0]; pids = int(ps[1]); idxs = int(ps[2]) if len(ps) >= 3 else 0
            if acts == "h_sup_apply":
                import supervisor as _sup
                res = _sup.aplicar_accion(pids, idxs)
                footer = (f"\n\n✅ <b>Aplicada #{idxs+1}</b> — {res}" if res
                          else f"\n\n⚠️ No se pudo aplicar #{idxs+1}.")
            else:
                footer = f"\n\n❌ Descartada #{idxs+1}."
            kb = query.message.reply_markup.inline_keyboard if query.message.reply_markup else []
            nueva = [r for r in kb if not (r and (r[0].callback_data or "").endswith(f":{pids}:{idxs}"))]
            base = query.message.text_html if query.message.text else (query.message.caption_html or "")
            await query.edit_message_text(
                base + footer, parse_mode="HTML",
                reply_markup=InlineKeyboardMarkup(nueva) if nueva else None,
                disable_web_page_preview=True)
        except Exception:
            try:
                await query.edit_message_reply_markup(None)
            except Exception:
                pass
        return

    # ── GSC: aplicar el título SEO propuesto (PATCH a WP) / descartar ──
    if query.data.startswith("h_gsc_ok:") or query.data.startswith("h_gsc_no:"):
        import sys as _sysgsc
        _sysgsc.path.insert(0, "/opt/me-harness"); _sysgsc.path.insert(0, "/opt/me-harness/agents")
        try:
            pg = query.data.split(":")
            actg = pg[0]; pidg = int(pg[1])
            if actg == "h_gsc_ok":
                await query.answer("Aplicando el título SEO…", show_alert=False)
                def _apply_gsc():
                    import gsc as _g; return _g.aplicar_prop(pidg)
                res = await asyncio.wait_for(asyncio.to_thread(_apply_gsc), timeout=120)
                footer = f"\n\n✅ <b>Aplicado</b> — {res}"
            else:
                import broker as _bkg
                _bkg.set_gsc_prop_status(pidg, "dismissed")
                footer = "\n\n❌ Descartada."
            base = query.message.text_html if query.message.text else (query.message.caption_html or "")
            await query.edit_message_text(base + footer, parse_mode="HTML",
                                          reply_markup=query.message.reply_markup, disable_web_page_preview=True)
        except Exception as _eg:
            try:
                await query.answer(f"Error: {str(_eg)[:150]}", show_alert=True)
            except Exception:
                pass
        return

    # ── Auditor: acuse de recibo + corregir (directiva Leo 29/7) ──
    # El auditor avisaba al vacío: mandaba la alerta y nunca sabía si Leo se enteró, así que
    # o repetía o se callaba por cooldown. Con estos botones el loop cierra. "Visto" silencia
    # ese estado (mientras no cambie el fingerprint); "Corregir" intenta la autocura mecánica
    # y lo que no tenga fix automático lo deja anotado en alertas_pedidos.json para Claude.
    if (query.data.startswith("h_aud_ok:") or query.data.startswith("h_aud_fix:")
            or query.data.startswith("h_aud_ajustar:")):
        import sys as _sysau, json as _jsau, datetime as _dtau
        _sysau.path.insert(0, "/opt/me-harness"); _sysau.path.insert(0, "/opt/me-harness/agents")
        _Hau = "/opt/me-harness"
        try:
            _actau, _fpau = query.data.split(":", 1)

            def _loadau(p):
                try:
                    return _jsau.load(open(p, encoding="utf-8"))
                except Exception:
                    return {}

            def _saveau(p, d):
                _jsau.dump(d, open(p, "w", encoding="utf-8"), ensure_ascii=False)

            _ahoraau = _dtau.datetime.now(_dtau.timezone.utc).isoformat()
            _ctxau = _loadau(f"{_Hau}/alertas_ctx.json").get(_fpau, {})

            # Cualquier botón implica acuse: si lo tocó, se enteró.
            _ackau = _loadau(f"{_Hau}/alertas_ack.json")
            _ackau[_fpau] = {"ts": _ahoraau, "accion": _actau}
            _saveau(f"{_Hau}/alertas_ack.json", _ackau)

            if _actau == "h_aud_ajustar":
                context.user_data["awaiting_ajuste_aud"] = {"fp": _fpau}
                await query.answer()
                await query.message.reply_text(
                    "✏️ Escribí qué querés que se haga con esto. Lo guardo como pedido "
                    "junto con el detalle técnico de la alerta.")
                return

            if _actau == "h_aud_ok":
                await query.answer("Anotado. No te molesto más con esto.")
                _footau = "\n\n✅ <b>Visto</b> — silenciado hasta que cambie la condición."
            else:
                await query.answer("Viendo si hay autocura…")
                _senau = _ctxau.get("señales", [])
                _hechosau = []
                if "stuck" in _senau:
                    def _curaau():
                        import supervisor as _supau
                        return _supau.autocura()
                    _hechosau = list(await asyncio.wait_for(
                        asyncio.to_thread(_curaau), timeout=120) or [])
                _sinfixau = [s for s in _senau if s != "stuck"]
                if _sinfixau:
                    _pedau = _loadau(f"{_Hau}/alertas_pedidos.json")
                    _pedau[_fpau] = {"ts": _ahoraau, "señales": _sinfixau,
                                     "resumen": _ctxau.get("resumen", ""),
                                     "detalle": _ctxau.get("detalle", {}),
                                     "estado": "pendiente"}
                    _saveau(f"{_Hau}/alertas_pedidos.json", _pedau)
                if _hechosau and _sinfixau:
                    _footau = (f"\n\n🔧 <b>Autocura</b>: {len(_hechosau)} acción(es).\n"
                               f"📝 Sin fix automático ({', '.join(_sinfixau)}) → anotado para Claude.")
                elif _hechosau:
                    _footau = ("\n\n🔧 <b>Autocura aplicada</b>: "
                               + "; ".join(str(a)[:70] for a in _hechosau[:3]))
                else:
                    _footau = (f"\n\n📝 <b>Sin fix automático</b> ({', '.join(_sinfixau) or 'n/d'}). "
                               f"Anotado para Claude en la próxima sesión.")

            _baseau = query.message.text_html if query.message.text else (query.message.caption_html or "")
            await query.edit_message_text(_baseau + _footau, parse_mode="HTML",
                                          reply_markup=None, disable_web_page_preview=True)
        except Exception as _eau:
            try:
                await query.answer(f"Error: {str(_eau)[:150]}", show_alert=True)
            except Exception:
                pass
        return

    # ── Reciclador: nota vencida con tráfico → 301 / banner / dejar (Fase 2 ciclo de vida) ──
    if (query.data.startswith("h_recic_301:") or query.data.startswith("h_recic_banner:")
            or query.data.startswith("h_recic_no:")):
        try:
            pr = query.data.split(":")
            actr = pr[0]; post_id = int(pr[1])
            guia_id = int(pr[2]) if len(pr) > 2 else 0
            import os as _osr, requests as _rqr
            _tok = _osr.environ.get("ME_REDIRECTS_TOKEN", "")
            _ep = f"https://mundoempresarial.ar/?me_redir=1&token={_tok}"
            if not _tok:
                footer = "\n\n⚠️ Falta ME_REDIRECTS_TOKEN en el env."
            elif actr == "h_recic_301":
                await query.answer("Redirigiendo a la guía…", show_alert=False)
                r = await asyncio.to_thread(
                    lambda: _rqr.post(_ep, data={"from": post_id, "to": guia_id}, timeout=40))
                footer = ("\n\n🔀 <b>301 aplicado</b> — el tráfico de esta nota va a la guía."
                          if r.ok else f"\n\n⚠️ Error: {r.text[:80]}")
            elif actr == "h_recic_banner":
                await query.answer("Poniendo el banner…", show_alert=False)
                r = await asyncio.to_thread(
                    lambda: _rqr.post(_ep + "&banner=1", data={"from": post_id, "to": guia_id}, timeout=40))
                footer = ("\n\n📌 <b>Banner puesto</b> — la nota queda viva, con aviso a la guía."
                          if r.ok else f"\n\n⚠️ Error: {r.text[:80]}")
            else:
                await query.answer("Dejada como está", show_alert=False)
                footer = "\n\n✖️ Dejada como está."
            base = query.message.text_html if query.message.text else (query.message.caption_html or "")
            await query.edit_message_text(base + footer, parse_mode="HTML", disable_web_page_preview=True)
        except Exception as _er:
            try:
                await query.answer(f"Error: {str(_er)[:150]}", show_alert=True)
            except Exception:
                pass
        return

    # ── GSC Living Notes: crear borrador / esqueleto / refrescar / ajustar / descartar ──
    if (query.data.startswith("h_gsc_ln_ok:") or query.data.startswith("h_gsc_ln_esqueleto:")
            or query.data.startswith("h_gsc_ln_no:") or query.data.startswith("h_gsc_lnref:")):
        import sys as _sysln
        _sysln.path.insert(0, "/opt/me-harness"); _sysln.path.insert(0, "/opt/me-harness/agents")
        try:
            pl = query.data.split(":")
            actl = pl[0]; pidl = int(pl[1])
            if actl in ("h_gsc_ln_ok", "h_gsc_ln_esqueleto"):
                modo = "borrador" if actl == "h_gsc_ln_ok" else "esqueleto"
                await query.answer("Armando el borrador (combino las crónicas)…", show_alert=False)
                def _apply_ln():
                    import gsc as _g; return _g.aplicar_ln_prop(pidl, modo)
                res = await asyncio.wait_for(asyncio.to_thread(_apply_ln), timeout=240)
                footer = f"\n\n✅ <b>{'Borrador' if modo=='borrador' else 'Esqueleto'} creado</b> — {res}"
            elif actl == "h_gsc_lnref":
                # Wire 18/8: el botón dispara el motor periodista (antes era un placeholder).
                # pl[1]=ln_id, pl[2]=job_id de la crónica nueva.
                nid = int(pl[2]) if len(pl) > 2 else 0
                await query.answer("🔄 Refrescando: extraigo el dato y busco ratificación…",
                                   show_alert=False)
                def _ref_ln():
                    import living_update as _lu
                    return _lu.evaluar_y_corregir(pidl, nid)
                resr = await asyncio.wait_for(asyncio.to_thread(_ref_ln), timeout=280)
                if isinstance(resr, dict):
                    _det = resr.get("detalle") or resr.get("motivo") or str(resr)
                    footer = ("\n\n🔄 " + ("✅ " if resr.get("ok") else "⚠️ ") + str(_det)[:280])
                else:
                    footer = f"\n\n🔄 {str(resr)[:280]}"
            else:
                import broker as _bkl
                _bkl.set_ln_prop_status(pidl, "dismissed")
                footer = "\n\n❌ Descartada."
            base = query.message.text_html if query.message.text else (query.message.caption_html or "")
            await query.edit_message_text(base + footer, parse_mode="HTML",
                                          reply_markup=query.message.reply_markup, disable_web_page_preview=True)
        except Exception as _el:
            try:
                await query.answer(f"Error: {str(_el)[:150]}", show_alert=True)
            except Exception:
                pass
        return

    # ── GSC Living Notes: ✏️ Ajustar (flow explícito "gscln", NO derivar de split) ──
    if query.data.startswith("h_gsc_ln_ajustar:"):
        try:
            _idln = int(query.data.split(":")[1])
            context.user_data["awaiting_ajuste"] = {"flow": "gscln", "id": _idln, "idx": 0}
            await query.message.reply_text(
                "✏️ Escribí tu ajuste (título, ángulo o estructura). Reformulo la living note y te la re-muestro.")
        except Exception:
            pass
        return

    # ── ✏️ Ajustar (opinar/complementar): editor/supervisor/reco/impacto → pide el texto ──
    if (query.data.startswith("h_edit_ajustar:") or query.data.startswith("h_sup_ajustar:")
            or query.data.startswith("h_reco_ajustar:") or query.data.startswith("h_imp_ajustar:")
            or query.data.startswith("h_esq_ajustar:") or query.data.startswith("h_gsc_ajustar:")):
        try:
            _pa = query.data.split(":")
            _flow = _pa[0].split("_")[1]   # edit | sup | reco | imp
            _id = int(_pa[1]); _idx = int(_pa[2]) if len(_pa) >= 3 else 0
            context.user_data["awaiting_ajuste"] = {"flow": _flow, "id": _id, "idx": _idx}
            await query.message.reply_text(
                "✏️ Escribí tu ajuste u opinión. El agente reformula la propuesta con tu criterio "
                "y te la re-muestra para Aplicar/Descartar (podés seguir ajustando).")
        except Exception:
            pass
        return

    # ── Reco capa×vertical (Fase C): aplicar a directiva / rehacer nota ejemplo / descartar ──
    if (query.data.startswith("h_reco_ok:") or query.data.startswith("h_reco_no:")
            or query.data.startswith("h_reco_fix:")):
        import sys as _sysrc
        _sysrc.path.insert(0, "/opt/me-harness"); _sysrc.path.insert(0, "/opt/me-harness/agents")
        try:
            prc = query.data.split(":")
            actrc = prc[0]; rid = int(prc[1])
            if actrc == "h_reco_ok":
                import direccion as _dir
                rc = _dir.aprobar_reco_cv(rid)
                footer = (f"\n\n✅ <b>Aplicada a la directiva §13</b> (Capa {rc['hilo']} × {rc['vertical']})"
                          if rc else "\n\n⚠️ No se pudo aplicar.")
            elif actrc == "h_reco_fix":
                import direccion as _dir
                res = _dir.rehacer_nota_ejemplo(rid)
                footer = (f"\n\n🔧 <b>Rehaciendo la nota ejemplo</b> (job #{res['job_id']}) — se regenera "
                          "con la mejora, sin inventar, y se actualiza el post vivo. Te aviso cuando salga."
                          if res else "\n\n⚠️ No se pudo rehacer (sin nota ejemplo).")
            else:
                import broker as _bkrc
                _bkrc.set_reco_cv_status(rid, "skipped")
                footer = "\n\n❌ Descartada."
            base = query.message.text_html if query.message.text else (query.message.caption_html or "")
            await query.edit_message_text(base + footer, parse_mode="HTML",
                                          disable_web_page_preview=True)
        except Exception:
            try:
                await query.edit_message_reply_markup(None)
            except Exception:
                pass
        return

    # ── Loop editorial: expirar reco sin efecto (sale del prompt del redactor) ──
    if query.data.startswith("h_reco_exp:"):
        import sys as _sysre
        _sysre.path.insert(0, "/opt/me-harness"); _sysre.path.insert(0, "/opt/me-harness/agents")
        try:
            rid = int(query.data.split(":")[1])
            import direccion as _dirx
            res = _dirx.expirar_reco(rid)
            base = query.message.text_html if query.message.text else ""
            await query.edit_message_text(base + f"\n\n🗑 <b>Reco #{rid}</b> {res}",
                                          parse_mode="HTML", disable_web_page_preview=True)
        except Exception as e:
            try:
                await query.answer(f"Error: {str(e)[:150]}", show_alert=True)
            except Exception:
                pass
        return

    # ── Loop de ESQUEMA: aplicar/descartar/revertir propuestas del comité ──
    if (query.data.startswith("h_esq_ok:") or query.data.startswith("h_esq_no:")
            or query.data.startswith("h_esq_rev:")):
        import sys as _syseq
        _syseq.path.insert(0, "/opt/me-harness"); _syseq.path.insert(0, "/opt/me-harness/agents")
        try:
            peq = query.data.split(":")
            acteq = peq[0]; pid = int(peq[1])
            import direccion as _direq
            if acteq == "h_esq_ok":
                res = _direq.aplicar_esquema_prop(pid)
                footer = f"\n\n✅ <b>Esquema #{pid}</b>: {res} — el ciclaje ya corre con la config nueva."
            elif acteq == "h_esq_rev":
                res = _direq.revertir_esquema_prop(pid)
                footer = f"\n\n↩️ <b>Esquema #{pid}</b>: {res}"
            else:
                import broker as _bkeq
                _bkeq.set_esquema_prop_status(pid, "dismissed")
                footer = f"\n\n❌ Propuesta #{pid} descartada."
            base = query.message.text_html if query.message.text else ""
            await query.edit_message_text(base + footer, parse_mode="HTML",
                                          disable_web_page_preview=True)
        except Exception as e:
            try:
                await query.answer(f"Error: {str(e)[:150]}", show_alert=True)
            except Exception:
                pass
        return

    # ── Eventos: armar la campaña de un evento propuesto (mínima/media/máxima) ──
    if query.data.startswith("h_evt_build:") or query.data.startswith("h_evt_no:"):
        import sys as _sysev
        _sysev.path.insert(0, "/opt/me-harness"); _sysev.path.insert(0, "/opt/me-harness/agents")
        try:
            pv = query.data.split(":")
            if pv[0] == "h_evt_build":
                evid = pv[1]; esf = pv[2] if len(pv) >= 3 else "media"
                await query.edit_message_text(
                    f"⏳ Preparando campaña <b>{evid}</b> ({esf})… curando notas al ángulo.", parse_mode="HTML")
                import eventos as _ev
                r = await asyncio.to_thread(_ev.build_campaign, evid, esf)
                if r.get("needs_insumos"):
                    cur = r.get("curadas", {})
                    kb = InlineKeyboardMarkup([[InlineKeyboardButton(
                        "📝 Aportar insumos", callback_data=f"h_evt_insumos:{evid}")]])
                    await query.edit_message_text(
                        f"🗂 <b>Campaña {evid} ({esf})</b> — junté "
                        f"{len(cur.get('opinion', []))} nota(s) de opinión + "
                        f"{len(cur.get('noticias', []))} noticia(s) afines al ángulo.\n\n"
                        f"Para la <b>NOTA PRINCIPAL</b> necesito tus insumos como Editor: tu mirada/opinión, "
                        f"fuentes (links) y ángulos. Tocá para aportarlos:",
                        parse_mode="HTML", reply_markup=kb, disable_web_page_preview=True)
                elif r.get("ok"):
                    nl = r.get("newsletter", {})
                    await query.edit_message_text(
                        f"✅ <b>Campaña {evid} ({esf})</b> armada — newsletter DRAFT #{nl.get('campaign_id')}.\n"
                        f"Revisala y aprobá el envío. El copy de redes te llegó aparte.",
                        parse_mode="HTML", disable_web_page_preview=True)
                else:
                    await query.edit_message_text(
                        f"❌ No se pudo armar: {r.get('error') or r.get('newsletter')}", parse_mode="HTML")
            else:
                await query.edit_message_text("❌ Campaña descartada esta vez.", parse_mode="HTML")
        except Exception as e:
            try:
                await query.edit_message_text(f"❌ Error armando la campaña: {str(e)[:150]}", parse_mode="HTML")
            except Exception:
                pass
        return

    # ── Eventos: postear el copy de la campaña en las redes disponibles ──
    if query.data.startswith("h_evt_redes:"):
        import sys as _sysrd
        _sysrd.path.insert(0, "/opt/me-harness"); _sysrd.path.insert(0, "/opt/me-harness/agents")
        try:
            evid = query.data.split(":")[1]
            await query.answer("Posteando…")
            import eventos as _evr
            res = await asyncio.to_thread(_evr.postear_campania_redes, evid)
            if res.get("ok"):
                lineas = []
                for canal, r in (res.get("resultados") or {}).items():
                    if r.get("ok"):
                        lineas.append(f"✅ {canal}" + (f" — {r.get('url')}" if r.get("url") else ""))
                    elif r.get("pendiente_manual"):
                        lineas.append(f"✋ {canal}: manual (faltan claves)")
                    else:
                        lineas.append(f"❌ {canal}: {str(r.get('error'))[:80]}")
                base = query.message.text_html if query.message.text else ""
                await query.edit_message_text(
                    base + "\n\n📤 <b>Resultado:</b>\n" + "\n".join(lineas),
                    parse_mode="HTML", disable_web_page_preview=True)
            else:
                await query.answer(f"Error: {res.get('error')}", show_alert=True)
        except Exception as e:
            try:
                await query.answer(f"Error: {str(e)[:150]}", show_alert=True)
            except Exception:
                pass
        return

    # ── Eventos: aportar insumos del Editor para la nota principal ──
    if query.data.startswith("h_evt_insumos:"):
        evid = query.data.split(":")[1]
        context.user_data["awaiting_evt_insumos"] = evid
        await query.edit_message_text(
            "📝 <b>Insumos para la nota principal</b>\n\n"
            "Pasame en UN mensaje: tu <b>mirada/opinión</b> sobre el evento, las <b>fuentes</b> "
            "(pegá los links) y los <b>ángulos</b> que quieras. El agente compone la nota con eso "
            "— sin inventar nada.", parse_mode="HTML")
        return

    # ── Nota rechazada por el gate: publicar igual (release → publicacion) ──
    if query.data.startswith("h_prerelease:"):
        jid = int(query.data.split(":")[1])
        import sys as _syspr
        _syspr.path.insert(0, "/opt/me-harness"); _syspr.path.insert(0, "/opt/me-harness/agents")
        try:
            import lector_pre as _lp
            ok = await asyncio.to_thread(_lp.release, jid)
            if ok:
                await query.edit_message_text(
                    f"✅ Nota #{jid} liberada — se publica en el próximo ciclo (~2 min).",
                    parse_mode="HTML")
            else:
                await query.edit_message_text(f"❌ No encontré el job #{jid}.", parse_mode="HTML")
        except Exception as e:
            await query.edit_message_text(
                f"❌ Error al liberar #{jid}: {str(e)[:150]}", parse_mode="HTML")
        return

    # ── Eventos: armar sin nota principal / cancelar la campaña en curso ──
    if query.data.startswith("h_evt_finalizar:") or query.data.startswith("h_evt_cancel:"):
        act, evid = query.data.split(":")[0], query.data.split(":")[1]
        import sys as _sevf
        _sevf.path.insert(0, "/opt/me-harness"); _sevf.path.insert(0, "/opt/me-harness/agents")
        try:
            import eventos as _ev
            if act == "h_evt_cancel":
                await asyncio.to_thread(_ev.cancelar_campania)
                await query.edit_message_text("❌ Campaña cancelada.", parse_mode="HTML")
            else:
                await query.edit_message_text(
                    "⏳ Armando la campaña (sin nota principal)…", parse_mode="HTML")
                res = await asyncio.to_thread(_ev.finalizar_campania, evid)
                if res.get("ok"):
                    nl = res.get("newsletter", {})
                    await query.edit_message_text(
                        f"✅ Campaña {evid} armada — newsletter DRAFT #{nl.get('campaign_id')}.\n"
                        f"Revisala y aprobá el envío. El copy de redes te llegó aparte.",
                        parse_mode="HTML", disable_web_page_preview=True)
                else:
                    await query.edit_message_text(
                        f"❌ No se pudo armar: {res.get('error') or res.get('newsletter')}",
                        parse_mode="HTML")
        except Exception as e:
            await query.edit_message_text(f"❌ Error: {str(e)[:150]}", parse_mode="HTML")
        return

    # ── Boletín semanal: arrancar la conversación (pregunta → opciones → slugs) ──
    if query.data == "h_bol_start":
        context.user_data["awaiting_bol_pregunta"] = True
        await query.edit_message_text(
            "🗳️ <b>Boletín semanal — Lectores</b>\n\nPasame la <b>PREGUNTA</b> de la encuesta.\n"
            "Ej: ¿Qué va a influir más esta semana en tu empresa o negocio?", parse_mode="HTML")
        return

    # ── /encuesta: toggles de redes + publicar / programar / cancelar ───────────
    if query.data in ("enc_tg", "enc_x", "enc_li", "enc_fb", "enc_ig", "enc_wa", "enc_tognl"):
        fp = context.user_data.get("enc")
        if not fp:
            await query.answer("Sesión perdida — reiniciá /encuesta", show_alert=True); return
        k = "nl" if query.data == "enc_tognl" else query.data[4:]
        fp.setdefault("canales", {})[k] = not fp.get("canales", {}).get(k)
        await query.edit_message_reply_markup(reply_markup=_build_enc_kb(fp))
        return
    # ── /encuesta → Newsletter: base → asunto/hora → armar draft + programar ────
    if query.data.startswith("enc_nl_base:"):
        nl = context.user_data.get("enc_nl")
        if not nl:
            await query.answer("Sesión perdida", show_alert=True); return
        nl["base"] = query.data.split(":")[1]
        await query.edit_message_text(
            f"📧 Newsletter → base <b>{nl['base']}</b>. Asunto: <i>{nl.get('subject') or 'automático'}</i>.\n"
            "El encabezado lo redacta la IA. ¿Cuándo lo mando?", parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("✅ Enviar ahora", callback_data="enc_nl_send")],
                [InlineKeyboardButton("📅 Programar hora", callback_data="enc_nl_sched")],
                [InlineKeyboardButton("✏️ Escribir asunto", callback_data="enc_nl_asunto")],
                [InlineKeyboardButton("❌ Cancelar", callback_data="enc_nl_cancel")]]))
        return
    if query.data == "enc_nl_asunto":
        if not context.user_data.get("enc_nl"):
            await query.answer("Sesión perdida", show_alert=True); return
        context.user_data["awaiting_enc_nl_asunto"] = True
        await query.edit_message_text("✏️ Escribí el asunto del mail:")
        return
    if query.data == "enc_nl_cancel":
        context.user_data.pop("enc_nl", None)
        await query.edit_message_text("Newsletter cancelado (la nota-encuesta sigue publicada).")
        return
    if query.data == "enc_nl_send":
        nl = context.user_data.get("enc_nl")
        if not nl:
            await query.answer("Sesión perdida", show_alert=True); return
        await query.edit_message_text("📤 Armando y enviando el newsletter…")
        txt = await _do_enc_newsletter(context, nl, None)
        context.user_data.pop("enc_nl", None)
        await query.edit_message_text(txt, disable_web_page_preview=True)
        return
    if query.data == "enc_nl_sched":
        if not context.user_data.get("enc_nl"):
            await query.answer("Sesión perdida", show_alert=True); return
        context.user_data["awaiting_enc_nl_hora"] = True
        await query.edit_message_text("📅 ¿Cuándo? <b>DD/MM HH:MM</b> (o <b>HH:MM</b> hoy).", parse_mode="HTML")
        return
    if query.data == "enc_cancel":
        context.user_data.pop("enc", None)
        await query.edit_message_text("Encuesta cancelada.")
        return
    if query.data == "enc_foto_auto":
        fp = context.user_data.get("enc")
        if not fp:
            await query.answer("Sesión perdida — reiniciá /encuesta", show_alert=True); return
        fp["image_id"] = None
        await query.edit_message_text(
            "🤖 La foto la elige el agente (biblioteca → IA → logo).\nElegí redes y acción:",
            reply_markup=_build_enc_kb(fp))
        return
    if query.data == "enc_foto_manual":
        if not context.user_data.get("enc"):
            await query.answer("Sesión perdida — reiniciá /encuesta", show_alert=True); return
        context.user_data["awaiting_enc_foto"] = True
        await query.edit_message_text("🖼️ Mandame la foto para la portada de la nota.")
        return
    if query.data == "enc_sched":
        if not context.user_data.get("enc"):
            await query.answer("Sesión perdida — reiniciá /encuesta", show_alert=True); return
        context.user_data["awaiting_enc_hora"] = True
        await query.edit_message_text(
            "📅 Escribí cuándo publicarla:\n<b>DD/MM HH:MM</b> (o solo <b>HH:MM</b> para hoy).",
            parse_mode="HTML")
        return
    if query.data == "enc_pub":
        fp = context.user_data.get("enc")
        if not fp:
            await query.answer("Sesión perdida — reiniciá /encuesta", show_alert=True); return
        await query.edit_message_text("📤 Publicando la encuesta y difundiendo…")
        txt, r = await _do_encuesta_publish(context, fp, None, chat_id=query.message.chat_id)
        nl_on = fp.get("canales", {}).get("nl")
        if nl_on and r and r.get("ok"):
            context.user_data["enc_nl"] = {"enc": r["enc"], "pregunta": fp["pregunta"],
                                           "opciones": fp["opciones"], "notas": fp.get("notas"),
                                           "nota_url": r.get("wp_url")}   # → botones del mail a la nota-landing
            context.user_data.pop("enc", None)
            await query.edit_message_text(txt + "\n\n📧 Newsletter — ¿a qué base?",
                                          reply_markup=_enc_nl_base_kb(), disable_web_page_preview=True)
            return
        context.user_data.pop("enc", None)
        await query.edit_message_text(txt, disable_web_page_preview=True)
        return

    # ── Boletín: elegido el público → armar el draft (GPT redacta la intro) ─────
    if query.data.startswith("h_bol_pub:"):
        publico = query.data.split(":")[1]
        slugs = context.user_data.pop("bol_slugs", "")
        preg  = context.user_data.pop("bol_pregunta", "")
        ops   = context.user_data.pop("bol_opciones", None)
        await query.edit_message_text(
            f"⏳ Armando el boletín a <b>{publico}</b> (GPT redacta la intro con las notas)…",
            parse_mode="HTML")
        import sys as _sbp
        _sbp.path.insert(0, "/opt/me-harness"); _sbp.path.insert(0, "/opt/me-harness/agents")
        try:
            import eventos as _ev
            res = await asyncio.to_thread(_ev.build_boletin, slugs, preg, ops, None, publico)
            if not res.get("ok"):
                await query.edit_message_text(
                    f"❌ No se pudo armar: {res.get('mensaje') or res.get('issues') or res.get('status')}",
                    parse_mode="HTML")
                return
            cid = res["campaign_id"]
            kb = InlineKeyboardMarkup([
                [InlineKeyboardButton("✅ Enviar ahora", callback_data=f"h_bol_send:{cid}:{publico}")],
                [InlineKeyboardButton("📅 Programar (elegí la hora)", callback_data=f"h_bol_horaask:{cid}:{publico}")],
                [InlineKeyboardButton("❌ Cancelar", callback_data=f"h_bol_cancel:{cid}")]])
            await query.edit_message_text(
                f"📋 <b>Boletín listo — DRAFT #{cid}</b> → <b>{publico}</b>\n"
                f"Asunto: {res.get('subject','')}\n"
                f"Encuesta: {len(ops or [])} opciones · Notas: {slugs.count(',') + 1} · Intro GPT ✅\n\n"
                f"Previsualizá: {res.get('preview')}\n\n¿Lo <b>envío ahora</b> o lo <b>programás</b> para una hora?",
                parse_mode="HTML", disable_web_page_preview=True, reply_markup=kb)
        except Exception as e:
            await query.edit_message_text(f"❌ Error armando el boletín: {str(e)[:160]}", parse_mode="HTML")
        return

    # ── Boletín: ajustar hora (pedir HH:MM) ─────────────────────────────────────
    if query.data.startswith("h_bol_horaask:"):
        pv = query.data.split(":")
        context.user_data["awaiting_bol_hora"] = {"cid": pv[1], "publico": pv[2] if len(pv) >= 3 else "lectores"}
        await query.edit_message_text("🕐 Escribí la hora de envío (hoy), formato HH:MM. Ej: 14:00",
                                      parse_mode="HTML")
        return

    # ── Boletín: CONFIRMAR la programación (recién acá dispara el schedule) ───────
    if query.data.startswith("h_bol_confirm:"):
        pv = query.data.split(":"); cid = pv[1]
        publico = pv[2] if len(pv) >= 3 else "lectores"
        hhmm = pv[3] if len(pv) >= 4 else "1030"
        hh, mm = int(hhmm[:2]), int(hhmm[2:])
        await query.edit_message_text(f"⏳ Programando #{cid} ({hh:02d}:{mm:02d})…", parse_mode="HTML")
        ok, txt = await asyncio.to_thread(_boletin_programar, cid, publico, hh, mm)
        await query.edit_message_text(txt, parse_mode="HTML", disable_web_page_preview=True)
        return

    # ── Boletín: programar el envío (hora fija 10:30 o ajustada) o cancelar ──────
    # ── Boletín: ENVIAR ahora ───────────────────────────────────────────────────
    if query.data.startswith("h_bol_send:"):
        pv = query.data.split(":"); cid = pv[1]; publico = pv[2] if len(pv) >= 3 else "lectores"
        await query.edit_message_text(f"⏳ Enviando boletín #{cid} a {publico}…", parse_mode="HTML")
        ok, txt = await asyncio.to_thread(_boletin_enviar_ahora, cid, publico)
        await query.edit_message_text(txt, parse_mode="HTML", disable_web_page_preview=True)
        return

    if query.data.startswith("h_bol_sched:") or query.data.startswith("h_bol_cancel:"):
        pv = query.data.split(":"); cid = pv[1]
        if pv[0] == "h_bol_cancel":
            await query.edit_message_text(f"❌ Boletín #{cid} cancelado (queda como draft en FluentCRM).",
                                          parse_mode="HTML")
            return
        publico = pv[2] if len(pv) >= 3 else "lectores"
        hhmm = pv[3] if len(pv) >= 4 else "1030"
        hh, mm = int(hhmm[:2]), int(hhmm[2:])
        await query.edit_message_text(f"⏳ Programando #{cid} ({hh:02d}:{mm:02d})…", parse_mode="HTML")
        ok, txt = await asyncio.to_thread(_boletin_programar, cid, publico, hh, mm)
        await query.edit_message_text(txt, parse_mode="HTML", disable_web_page_preview=True)
        return

    if (query.data.startswith("h_tip_accept:") or
            query.data.startswith("h_tip_skip:") or
            query.data.startswith("h_tip_fix:") or
            query.data.startswith("h_tip_qaok:")):
        import sys as _sys2
        _sys2.path.insert(0, "/opt/me-harness")
        try:
            import broker as _br2
            parts2 = query.data.split(":")
            action2 = parts2[0]
            arg2   = int(parts2[1]) if len(parts2) >= 2 else None

            if action2 == "h_tip_accept" and arg2:
                tip2 = _br2.get_tip(arg2)
                tip_text2 = (tip2 or {}).get("tip", "")[:200]
                _br2.update_tip_status(arg2, "accepted")
                await query.edit_message_text(
                    f"🔧 <b>Corrección pendiente</b>\n<i>{tip_text2}</i>\n\n<code>→ Recordar corregir manualmente en WP</code>",
                    parse_mode="HTML",
                )

            elif action2 == "h_tip_skip" and arg2:
                _br2.update_tip_status(arg2, "ignored")
                await query.edit_message_reply_markup(None)

            elif action2 == "h_tip_fix" and arg2:
                await query.edit_message_text(
                    f"⏳ Corrigiendo job #{arg2}…", parse_mode="HTML"
                )
                try:
                    import json as _jfix
                    _job_fix = _br2.get_job(arg2)
                    if not _job_fix or not _job_fix.get("wp_url"):
                        await query.edit_message_text(
                            f"❌ Job #{arg2} sin wp_url — no se puede auto-corregir.",
                            parse_mode="HTML"
                        )
                    else:
                        from agents.lector import (
                            run_qa_checks, _auto_fix_article,
                            _get_post_id_from_slug, _get_wp_post_text,
                        )
                        _wp_url_fix = _job_fix["wp_url"]
                        _cj_fix  = _jfix.loads(_job_fix.get("content_json") or "{}")
                        _src_url  = _job_fix.get("source_url") or _cj_fix.get("source") or _cj_fix.get("source_url") or ""
                        _src_name = _cj_fix.get("source_name") or ""
                        _pid_fix  = _get_post_id_from_slug(_wp_url_fix)
                        _qa_iss, _qa_meta = run_qa_checks(_wp_url_fix, arg2)
                        _hard = [i for i in _qa_iss if i.startswith("❌")]
                        if not _hard:
                            await query.edit_message_text(
                                f"✅ Job #{arg2}: sin errores duros detectados en QA.",
                                parse_mode="HTML"
                            )
                        elif not _pid_fix:
                            await query.edit_message_text(
                                f"❌ No se pudo obtener post_id para job #{arg2}.",
                                parse_mode="HTML"
                            )
                        else:
                            _txt = _get_wp_post_text(_wp_url_fix) or ""
                            _fixes = await _auto_fix_article(
                                _pid_fix, _wp_url_fix,
                                _job_fix.get("title", ""), _txt,
                                _qa_iss, _qa_meta, 5, [],
                                source_url=_src_url,
                                source_name=_src_name,
                            )
                            if _fixes:
                                await query.edit_message_text(
                                    f"✅ <b>Auto-fix — job #{arg2}:</b>\n"
                                    + "\n".join(f"• {f}" for f in _fixes),
                                    parse_mode="HTML"
                                )
                            else:
                                await query.edit_message_text(
                                    f"⚠️ Job #{arg2}: QA errors sin fix automático disponible:\n"
                                    + "\n".join(f"• {i}" for i in _hard[:5]),
                                    parse_mode="HTML"
                                )
                except Exception as _fix_err:
                    await query.edit_message_text(
                        f"❌ Error en auto-fix job #{arg2}: {_fix_err}",
                        parse_mode="HTML"
                    )

            elif action2 == "h_tip_qaok" and arg2:
                await query.message.delete()

        except Exception as _he2:
            logger.warning(f"h_tip handler error: {_he2}")
            await query.answer(f"Error: {_he2}")
        return

    # ── Harness — Reescritor quirúrgico (F3 autoajuste 13/8) ────────────────────────────────
    # h_rw_apply:{id} → aplica el patch YA PREPARADO (botón efectivo) · h_rw_skip:{id} → saltear
    if query.data.startswith("h_rw_apply:") or query.data.startswith("h_rw_skip:"):
        import sys as _sysrw
        _sysrw.path.insert(0, "/opt/me-harness")
        try:
            rid = int(query.data.split(":", 1)[1])
            from agents import reescritor as _rw
            if query.data.startswith("h_rw_apply:"):
                _res = await asyncio.wait_for(asyncio.to_thread(_rw.aplicar, rid), timeout=90)
                if _res.get("ok"):
                    await query.edit_message_text(
                        f"✅ <b>Reescritura aplicada</b> — {_res.get('titulo','')[:70]}\n"
                        f"El efecto se mide a +14 días (informe del lunes).", parse_mode="HTML")
                else:
                    # NO editar el mensaje: conservar los botones para reintentar (review 13/8)
                    await query.answer(f"⚠️ No se pudo aplicar: {str(_res.get('motivo','?'))[:170]}",
                                       show_alert=True)
            else:
                await asyncio.to_thread(_rw.saltear, rid)
                await query.edit_message_text("❌ Reescritura salteada.", parse_mode="HTML")
        except asyncio.TimeoutError:
            # el thread sigue corriendo: el patch PUEDE haberse aplicado igual (review 13/8)
            try:
                await query.answer("⏳ WP tarda en responder; el patch puede haberse aplicado "
                                   "igual — verificá antes de reintentar.", show_alert=True)
            except Exception:
                pass
        except Exception as _rwe:
            logger.warning(f"h_rw handler: {_rwe}")
            try:
                await query.answer("Error aplicando — ver logs", show_alert=True)
            except Exception:
                pass
        return

    # ── Harness — Briefing nutrición: ↩️ devolver una nota al briefing normal (13/8) ─────────
    # Registra el ejemplo NEGATIVO (destino='briefing') para que el router aprenda que esa
    # familia de notas NO se desvía, limpia el destino y reenvía el card del curador.
    if query.data.startswith("h_nut_rebrief:"):
        import sys as _sysnb
        _sysnb.path.insert(0, "/opt/me-harness")
        try:
            _jidn = int(query.data.split(":", 1)[1])
            import broker as _brn
            from agents import curador as _curn
            def _volver(_j=_jidn):
                try:
                    _brn.add_ruteo_ejemplo_desde_job(_j, "briefing")
                except Exception as _e_ej:
                    logger.debug(f"ejemplo negativo: {_e_ej}")
                _job = _brn.get_job(_j) or {}
                try:
                    _st = json.loads(_job.get("content_json") or "{}")
                    _st.pop("living_note_id", None)
                    import sqlite3 as _sqn
                    with _sqn.connect("/opt/me-harness/harness.db") as _cn:
                        _cn.execute("UPDATE jobs SET content_json=? WHERE id=?",
                                    (json.dumps(_st, ensure_ascii=False), _j))
                except Exception:
                    pass
                _brn.update_stage(_j, "curado")
                _curn.run_briefing_single(_j)
            await asyncio.wait_for(asyncio.to_thread(_volver), timeout=90)
            await query.answer("↩️ Devuelta al briefing (el router toma nota)", show_alert=False)
        except Exception as _e_nb:
            logger.warning(f"h_nut_rebrief: {_e_nb}")
            try:
                await query.answer(f"Error: {_e_nb}", show_alert=True)
            except Exception:
                pass
        return

    # ── Harness — Reglas de agentes (RETIRADO 2026-08-13, decisión de Leo) ──────────────────
    # El loop "recomendación general → ¿convertir en regla?" se eliminó del Lector: generalizar
    # reglas desde opiniones de notas puntuales era mala señal, y las reglas de agente_curador/
    # publicador no las leía nadie. Este handler queda SOLO para botones viejos en el historial.
    if query.data.startswith("h_agtrule_add:") or query.data.startswith("h_agtrule_skip:"):
        try:
            await query.edit_message_text(
                "ℹ️ <b>Este mecanismo se retiró (13/8).</b>\n"
                "Ya no se generan reglas a partir de opiniones del Lector sobre notas puntuales. "
                "Los patrones se miden contra tráfico real en el informe de calidad de los lunes; "
                "si querés fijar una regla editorial, charlala en sesión.",
                parse_mode="HTML",
            )
        except Exception as _age:
            logger.warning(f"h_agtrule handler (legacy): {_age}")
        return

    # ── Harness — SysAdmin ───────────────────────────────────────────────────
    if query.data.startswith("h_sa_"):
        parts  = query.data.split(":")
        action = parts[0]
        arg    = parts[1] if len(parts) >= 2 else ""

        if action == "h_sa_disk_clean":
            import subprocess as _sp
            # Mostrar uso de disco
            out = _sp.run(["df", "-h", "/"], capture_output=True, text=True).stdout
            du_log = _sp.run(["du", "-sh", "/var/log", "/tmp", "/opt",
                              "/root/.cache"], capture_output=True, text=True).stdout
            await query.edit_message_reply_markup(None)
            await context.bot.send_message(
                chat_id=query.message.chat_id,
                text=f"<b>Uso de disco:</b>\n<pre>{out.strip()}</pre>\n\n"
                     f"<b>Directorios principales:</b>\n<pre>{du_log.strip()[:600]}</pre>",
                parse_mode="HTML"
            )
        elif action == "h_sa_waf_check":
            await query.edit_message_reply_markup(None)
            await context.bot.send_message(
                chat_id=query.message.chat_id,
                text="⚠️ <b>WAF / IP bloqueada</b>\n\n"
                     "1. Entrá al panel Ferozo\n"
                     "2. Soporte → reportar IP del VPS (179.43.122.186) como falso positivo\n"
                     "3. O activar WARP como proxy para WP en config.py",
                parse_mode="HTML"
            )
        elif action == "h_sa_ssl_renew":
            host = arg or "mundoempresarial.ar"
            await query.edit_message_reply_markup(None)
            await context.bot.send_message(
                chat_id=query.message.chat_id,
                text=f"🔐 <b>SSL — {host}</b>\n\n"
                     f"Renovar con script ACME ya existente en el VPS.\n"
                     f"Si es Ferozo: entrar al panel → SSL → Let's Encrypt → renovar.",
                parse_mode="HTML"
            )
        elif action == "h_sa_warp_manual":
            import subprocess as _sp
            out = _sp.run(["systemctl", "status", "warp-connect", "--no-pager", "-n", "10"],
                          capture_output=True, text=True).stdout
            await query.edit_message_reply_markup(None)
            await context.bot.send_message(
                chat_id=query.message.chat_id,
                text=f"<b>Estado warp-connect:</b>\n<pre>{out.strip()[:600]}</pre>",
                parse_mode="HTML"
            )
        elif action == "h_sa_upd_plugins":
            await query.edit_message_reply_markup(None)
            await context.bot.send_message(chat_id=query.message.chat_id,
                text="🔄 Actualizando los plugins de WordPress… verifico el sitio al final.")
            import sys as _sysu, asyncio as _aiou
            _sysu.path.insert(0, "/opt/me-harness"); _sysu.path.insert(0, "/opt/me-harness/agents")
            def _do_upd():
                import sysadmin as _sa
                return _sa.apply_wp_updates("all_plugins")
            try:
                res = await _aiou.wait_for(_aiou.to_thread(_do_upd), timeout=300)
            except Exception as _e:
                await context.bot.send_message(chat_id=query.message.chat_id,
                    text=f"⚠️ Error actualizando: {str(_e)[:150]}")
                return
            if res.get("site_ok"):
                await context.bot.send_message(chat_id=query.message.chat_id,
                    text=f"✅ <b>Plugins actualizados</b>: {res.get('updated','?')}/{res.get('requested','?')}. "
                         f"Sitio OK (200).", parse_mode="HTML")
            else:
                await context.bot.send_message(chat_id=query.message.chat_id,
                    text=f"🚨 <b>Actualicé plugins pero el sitio devuelve {res.get('site_code')}</b> — "
                         f"REVISAR YA.\n<code>{str(res)[:250]}</code>", parse_mode="HTML")
        elif action == "h_sa_skip":
            await query.edit_message_reply_markup(None)
        return

    # ── Harness — Cola de publicación + agente Social ───────────────────────
    if query.data.startswith(("h_cola_", "h_social_")):
        import sys as _sys, json as _js, sqlite3 as _sq
        _sys.path.insert(0, "/opt/me-harness")
        _HDB = "/opt/me-harness/harness.db"
        try:
            import broker as _br_cola
            parts  = query.data.split(":")
            action = parts[0]
            arg    = parts[1] if len(parts) >= 2 else None

            _DEST_LABELS = {
                "ahora":         "⚡ AHORA",
                "hoy_portada":   "📌 HOY PORTADA",
                "hoy_normal":    "📰 HOY",
                "mejor_horario": "📊 MEJOR HORARIO",
                "finde":         "📅 FINDE",
                "fecha":         "🗓 FECHA ESPECÍFICA",
                "recurrencia_a": "🔁 FECHA ANUAL",
                "recurrencia_b": "📋 GUÍA NORMATIVA",
            }

            # ── Confirmar sugerencia del agente ──────────────────────────────
            if action == "h_cola_confirm" and arg:
                job_id = int(arg)
                job    = _br_cola.get_job(job_id)
                if not job:
                    await query.answer("Job no encontrado", show_alert=True)
                    return
                sg = {}
                if job.get("agent_suggestion"):
                    try: sg = _js.loads(job["agent_suggestion"])
                    except Exception: pass
                cj_conf = {}
                if job.get("content_json"):
                    try: cj_conf = _js.loads(job["content_json"])
                    except Exception: pass

                pub_dest = sg.get("pub_dest") or job.get("pub_dest") or "hoy_normal"
                anchor   = sg.get("anchor_date")
                rec_type = sg.get("recurrence_type")

                # ── Aprendizaje: comparar sugerencia del agente vs lo que publicó Leo ──
                import sys as _sys_lrn
                _sys_lrn.path.insert(0, "/opt/me-harness")
                try:
                    import broker as _br_lrn
                    from agents.curador import _extract_domain
                    _domain = _extract_domain(job.get("source_url", ""))
                    _title  = job.get("title", "")
                    _agent_dest  = sg.get("pub_dest", "")
                    _final_dest  = pub_dest
                    _title_changed = bool(cj_conf.get("original_title") and
                                         cj_conf.get("original_title") != _title)
                    _dest_changed  = bool(_agent_dest and _agent_dest != _final_dest)
                    _cats_changed  = bool(cj_conf.get("category_ids"))  # Leo tocó cats
                    _changed = _title_changed or _dest_changed

                    if not _changed:
                        # Leo publicó sin cambios → confianza en la selección del agente
                        _br_lrn.update_domain_weight(_domain, +0.15)
                        _br_lrn.update_keywords_for_title(_title, +0.5)
                    else:
                        # Leo cambió algo → corrección al agente
                        if _dest_changed:
                            _br_lrn.record_feedback(job_id, "cola", "dest_corrected",
                                before={"pub_dest": _agent_dest},
                                after={"pub_dest": _final_dest, "title": _title})
                        if _title_changed:
                            _br_lrn.record_feedback(job_id, "cola", "title_corrected",
                                before={"title": cj_conf.get("original_title", "")},
                                after={"title": _title})
                        # Feedback negativo leve al dominio (nota requirió corrección)
                        _br_lrn.update_domain_weight(_domain, -0.05)
                except Exception:
                    pass

                _br_cola.confirm_cola(job_id, pub_dest, anchor_date=anchor, recurrence_type=rec_type)
                label = _DEST_LABELS.get(pub_dest, pub_dest)
                try:
                    await query.edit_message_text(
                        f"✅ <b>{(job.get('title') or '')[:60]}</b>\n"
                        f"→ Redacción  ·  {label}",
                        parse_mode="HTML"
                    )
                except Exception:
                    pass
                await query.answer(f"✅ {label}", show_alert=False)

            # ── Cambiar destino — submenu ─────────────────────────────────────
            elif action == "h_cola_change" and arg:
                job_id = int(arg)
                # Leer destino actual para marcar con ✅
                _job_ch = _br_cola.get_job(job_id)
                _sg_ch = {}
                if _job_ch and _job_ch.get("agent_suggestion"):
                    try: _sg_ch = _js.loads(_job_ch["agent_suggestion"])
                    except Exception: pass
                _cur_dest = _sg_ch.get("pub_dest") or (_job_ch or {}).get("pub_dest") or ""
                def _lbl(key, text):
                    return ("✅ " if _cur_dest == key else "") + text
                rows = [
                    [{"text": _lbl("ahora",          "⚡ Ahora"),                "callback_data": f"h_cola_set:{job_id}:ahora"}],
                    [{"text": _lbl("hoy_portada",    "📌 Portada de hoy"),       "callback_data": f"h_cola_set:{job_id}:hoy_portada"}],
                    [{"text": _lbl("hoy_normal",     "🕐 Próximo turno"),        "callback_data": f"h_cola_set:{job_id}:hoy_normal"}],
                    [{"text": _lbl("mejor_horario",  "📊 Mejor horario (GA4)"),  "callback_data": f"h_cola_set:{job_id}:mejor_horario"}],
                    [{"text": _lbl("finde",          "📅 El finde"),             "callback_data": f"h_cola_set:{job_id}:finde"}],
                    [{"text": _lbl("fecha",          "🗓 Programar día y hora"), "callback_data": f"h_cola_set:{job_id}:fecha"}],
                    [{"text": _lbl("recurrencia_a",  "🔁 Fecha anual"),          "callback_data": f"h_cola_set:{job_id}:recurrencia_a"}],
                    [{"text": _lbl("recurrencia_b",  "📋 Guía normativa"),       "callback_data": f"h_cola_set:{job_id}:recurrencia_b"}],
                    [{"text": _lbl("auto",           "🤖 Auto (agente decide)"), "callback_data": f"h_cola_set:{job_id}:auto"}],
                    [{"text": "↩ Volver",                                         "callback_data": f"h_cola_back:{job_id}"}],
                ]
                try:
                    await query.edit_message_reply_markup(
                        reply_markup={"inline_keyboard": rows}
                    )
                except Exception:
                    pass
                await query.answer()

            # ── Portada del día toggle ────────────────────────────────────────
            elif action == "h_cola_portada" and arg:
                job_id = int(arg)
                job    = _br_cola.get_job(job_id)
                cj = {}
                try:
                    cj = _js.loads(job.get("content_json") or "{}") if job else {}
                except Exception:
                    pass
                portada = not cj.get("portada", False)
                cj["portada"] = portada
                import sqlite3 as _sq3
                with _sq3.connect("/opt/me-harness/harness.db") as _c3:
                    _c3.execute("UPDATE jobs SET content_json=? WHERE id=?",
                                (_js.dumps(cj), job_id))
                # Si se activa portada → pub_dest=hoy_portada
                if portada:
                    _br_cola.set_cola_suggestion(job_id, pub_dest="hoy_portada",
                                                  reasoning="Marcado como portada del día por Leo")
                await query.answer("📌 Portada activada" if portada else "Portada desactivada",
                                   show_alert=False)
                # Rebuild card con nuevo estado
                # (mismo h_cola_back pero inline)
                try:
                    from agents import publicador as _pub_dummy  # solo para trigger cola
                    pass
                except Exception:
                    pass
                # Refrescar keyboard
                _portada_lbl = "☑️ Portada del día" if portada else "📌 Portada del día"
                _fmt         = cj.get("formato", "desplegable")
                _fmt_lbl     = "☑️ Continua" if _fmt == "continua" else "☑️ Desplegable"
                tags2    = cj.get("matched_kw") or []
                cat_ids2 = cj.get("category_ids") or []
                _CN2 = {94:"Economía",87:"Política",96:"Empresas",97:"Internacional",
                        100:"Gobierno",95:"AFIP/ARCA",90:"Industria",91:"Opinión",
                        89:"Comercio",88:"Agro",103:"Informes",102:"Provincias",
                        93:"Sindicatos",92:"Servicios",239:"Digital pymes",1139:"Mundo del vino",
                        101:"Poder Judicial",99:"Congreso",98:"Nacional",104:"ME TV",1048:"Coberturas"}
                _tl = (f"🏷 {', '.join(t.title() for t in tags2[:3])}" if tags2 else "🏷 Etiquetas")
                _cl = (f"🗂 {'+'.join(_CN2.get(c,str(c))[:8] for c in cat_ids2[:2])}" if cat_ids2 else "🗂 Categoría")
                try:
                    await query.edit_message_reply_markup(reply_markup={"inline_keyboard": [
                        [{"text":"✅ Publicar","callback_data":f"h_cola_confirm:{job_id}"},
                         {"text":"🔄 Cambiar destino","callback_data":f"h_cola_change:{job_id}"}],
                        [{"text":_portada_lbl,"callback_data":f"h_cola_portada:{job_id}"},
                         {"text":_fmt_lbl,"callback_data":f"h_cola_formato:{job_id}"}],
                        [{"text":"✏️ Cambiar título","callback_data":f"h_cola_change_title:{job_id}"}],
                        [{"text":_cl,"callback_data":f"h_cola_cats:{job_id}"},
                         {"text":_tl,"callback_data":f"h_cola_tags:{job_id}"}],
                        [{"text":"🚫 Descartar","callback_data":f"h_cola_discard:{job_id}"},
                         {"text":"⏭ Ir a redacción","callback_data":f"h_cola_skip:{job_id}"}],
                    ]})
                except Exception:
                    pass

            # ── Formato nota (continua / desplegable) toggle ──────────────────
            elif action == "h_cola_formato" and arg:
                job_id = int(arg)
                job    = _br_cola.get_job(job_id)
                cj = {}
                try:
                    cj = _js.loads(job.get("content_json") or "{}") if job else {}
                except Exception:
                    pass
                fmt_actual = cj.get("formato", "desplegable")
                fmt_nuevo  = "continua" if fmt_actual == "desplegable" else "desplegable"
                cj["formato"] = fmt_nuevo
                import sqlite3 as _sq4
                with _sq4.connect("/opt/me-harness/harness.db") as _c4:
                    _c4.execute("UPDATE jobs SET content_json=? WHERE id=?",
                                (_js.dumps(cj), job_id))
                await query.answer(f"☑️ Formato: {fmt_nuevo}", show_alert=False)
                portada2 = cj.get("portada", False)
                tags3    = cj.get("matched_kw") or []
                cat_ids3 = cj.get("category_ids") or []
                _CN3 = {94:"Economía",87:"Política",96:"Empresas",97:"Internacional",
                        100:"Gobierno",95:"AFIP/ARCA",90:"Industria",91:"Opinión",
                        89:"Comercio",88:"Agro",103:"Informes",102:"Provincias",
                        93:"Sindicatos",92:"Servicios",239:"Digital pymes",1139:"Mundo del vino",
                        101:"Poder Judicial",99:"Congreso",98:"Nacional",104:"ME TV",1048:"Coberturas"}
                _pl = "☑️ Portada del día" if portada2 else "📌 Portada del día"
                _fl = "☑️ Continua" if fmt_nuevo == "continua" else "☑️ Desplegable"
                _tl3 = (f"🏷 {', '.join(t.title() for t in tags3[:3])}" if tags3 else "🏷 Etiquetas")
                _cl3 = (f"🗂 {'+'.join(_CN3.get(c,str(c))[:8] for c in cat_ids3[:2])}" if cat_ids3 else "🗂 Categoría")
                try:
                    await query.edit_message_reply_markup(reply_markup={"inline_keyboard": [
                        [{"text":"✅ Publicar","callback_data":f"h_cola_confirm:{job_id}"},
                         {"text":"🔄 Cambiar destino","callback_data":f"h_cola_change:{job_id}"}],
                        [{"text":_pl,"callback_data":f"h_cola_portada:{job_id}"},
                         {"text":_fl,"callback_data":f"h_cola_formato:{job_id}"}],
                        [{"text":"✏️ Cambiar título","callback_data":f"h_cola_change_title:{job_id}"}],
                        [{"text":_cl3,"callback_data":f"h_cola_cats:{job_id}"},
                         {"text":_tl3,"callback_data":f"h_cola_tags:{job_id}"}],
                        [{"text":"🚫 Descartar","callback_data":f"h_cola_discard:{job_id}"},
                         {"text":"⏭ Ir a redacción","callback_data":f"h_cola_skip:{job_id}"}],
                    ]})
                except Exception:
                    pass

            # ── Setear destino manualmente ────────────────────────────────────
            elif action == "h_cola_set" and len(parts) >= 3:
                job_id   = int(parts[1])
                pub_dest = parts[2]

                if pub_dest == "fecha":
                    # Pide texto con fecha/hora — único caso que no vuelve al card directo
                    context.user_data["awaiting_cola_prog_for"] = job_id
                    try:
                        await context.bot.send_message(
                            chat_id=query.message.chat_id,
                            text=(f"🗓 <b>Programar — nota #{job_id}</b>\n\n"
                                  f"Escribí la fecha y hora:\n"
                                  f"<code>30/05 18:30</code>  ·  <code>mañana 10:00</code>"),
                            parse_mode="HTML",
                            reply_markup={"inline_keyboard": [[
                                {"text": "↩ Volver", "callback_data": f"h_cola_back:{job_id}"}
                            ]]}
                        )
                    except Exception:
                        pass
                    await query.answer()
                elif pub_dest == "auto":
                    # Restaura sugerencia original del agente
                    job2 = _br_cola.get_job(job_id)
                    sg2  = {}
                    if job2 and job2.get("agent_suggestion"):
                        try: sg2 = _js.loads(job2["agent_suggestion"])
                        except Exception: pass
                    pub_dest = sg2.get("pub_dest") or "hoy_normal"
                    _br_cola.set_cola_suggestion(job_id, pub_dest=pub_dest,
                                                  reasoning=sg2.get("reasoning",""))
                    await query.answer(f"🤖 {_DEST_LABELS.get(pub_dest, pub_dest)}", show_alert=False)
                    await query.data.__class__  # trigger h_cola_back inline
                    # Simular h_cola_back
                    query.data = f"h_cola_back:{job_id}"
                    # Forzar re-render del card via answer vacío + edit
                    job2 = _br_cola.get_job(job_id)
                    if job2:
                        import importlib as _imp
                        # reusar lógica de back disparando el mismo evento
                        pass
                    # Simplemente re-editamos el reply_markup con el destino marcado en la sugerencia
                    # El usuario puede ver el cambio al volver con ↩
                    await query.answer(f"🤖 Auto → {_DEST_LABELS.get(pub_dest, pub_dest)}", show_alert=False)
                else:
                    # Actualizar destino en la sugerencia SIN confirmar (no manda a redacción aún)
                    _br_cola.set_cola_suggestion(job_id, pub_dest=pub_dest,
                                                  reasoning=f"Destino cambiado manualmente: {pub_dest}")
                    label = _DEST_LABELS.get(pub_dest, pub_dest)
                    await query.answer(f"✅ {label}", show_alert=False)
                    # Volver al card principal con el destino actualizado
                    query.data = f"h_cola_back:{job_id}"
                    job_b = _br_cola.get_job(job_id)
                    if job_b:
                        _title_b = job_b.get("title") or "Sin título"
                        _hilo_b  = job_b.get("hilo") or 2
                        _url_b   = job_b.get("source_url","")
                        _dom_b   = (_url_b.split("//",1)[-1].split("/")[0].replace("www.","") if _url_b else "")
                        _cap_b   = {1:"CAPA 1",2:"CAPA 2",3:"CAPA 3"}.get(_hilo_b, f"CAPA {_hilo_b}")
                        cj_b = {}
                        try: cj_b = _js.loads(job_b.get("content_json") or "{}")
                        except Exception: pass
                        tags_b   = cj_b.get("matched_kw") or []
                        cat_b    = cj_b.get("category_ids") or []
                        fmt_b    = cj_b.get("formato","desplegable")
                        port_b   = cj_b.get("portada",False)
                        inst_b   = cj_b.get("instructions","")
                        orig_b   = cj_b.get("original_title","")
                        has_c_b  = bool(orig_b and orig_b != _title_b)
                        _CN_B = {94:"Economía",87:"Política",96:"Empresas",97:"Internacional",
                                 100:"Gobierno",95:"AFIP/ARCA",90:"Industria",91:"Opinión",
                                 89:"Comercio",88:"Agro",103:"Informes",102:"Provincias",
                                 93:"Sindicatos",92:"Servicios",239:"Digital pymes",1139:"Mundo del vino",
                                 101:"Poder Judicial",99:"Congreso",98:"Nacional",104:"ME TV",1048:"Coberturas"}
                        tl_b  = (f"🏷 {', '.join(t.title() for t in tags_b[:3])}" if tags_b else "🏷 Etiquetas")
                        cl_b  = (f"🗂 {'+'.join(_CN_B.get(c,str(c))[:8] for c in cat_b[:2])}" if cat_b else "🗂 Categoría")
                        fl_b  = "☑️ Continua" if fmt_b == "continua" else "☑️ Desplegable"
                        pl_b  = "☑️ Portada del día" if port_b else "📌 Portada del día"
                        tcb_b = f"h_cola_restore_title:{job_id}" if has_c_b else f"h_cola_change_title:{job_id}"
                        tbtn_b= "↩ Restaurar original" if has_c_b else "✏️ Cambiar título"
                        sug_b = {}
                        if job_b.get("agent_suggestion"):
                            try: sug_b = _js.loads(job_b["agent_suggestion"])
                            except Exception: pass
                        dest_b  = sug_b.get("pub_dest") or job_b.get("pub_dest") or "sin_asignar"
                        reas_b  = sug_b.get("reasoning","")
                        dl_b    = _DEST_LABELS.get(dest_b, dest_b)
                        sl_b    = f"\n🤖 <b>{dl_b}</b>" + (f"\n<i>{reas_b}</i>" if reas_b else "")
                        tlines_b= ""
                        if orig_b and orig_b != _title_b: tlines_b += f"\n<s>{orig_b[:70]}</s>"
                        pline_b = "\n📌 <b>PORTADA DEL DÍA</b>" if port_b else ""
                        fline_b = f"\n📄 <i>{fmt_b.title()}</i>" if fmt_b != "desplegable" else ""
                        iline_b = f"\n📝 <i>{inst_b}</i>" if inst_b else ""
                        tgline_b= (f"\n🏷 <i>{' · '.join(t.title() for t in tags_b[:5])}</i>" if tags_b else "")
                        ctline_b= (f"\n🗂 <i>{' · '.join(_CN_B.get(c,str(c)) for c in cat_b[:3])}</i>" if cat_b else "")
                        dt_b    = (f"\n🗓 <i>{job_b.get('pub_date','')}</i>" if job_b.get("pub_date") else "")
                        card_b  = (f"<b>{_title_b}</b>{tlines_b}\n{_cap_b}  ·  {_dom_b}"
                                   f"{sl_b}{pline_b}{ctline_b}{tgline_b}{fline_b}{dt_b}{iline_b}\n"
                                   f"<a href='{_url_b}'>📎 fuente</a>")
                        _kb_b = [
                            [{"text":"✅ Publicar","callback_data":f"h_cola_confirm:{job_id}"},
                             {"text":"🔄 Cambiar destino","callback_data":f"h_cola_change:{job_id}"}],
                            [{"text":pl_b,"callback_data":f"h_cola_portada:{job_id}"},
                             {"text":fl_b,"callback_data":f"h_cola_formato:{job_id}"}],
                            [{"text":tbtn_b,"callback_data":tcb_b}],
                            [{"text":cl_b,"callback_data":f"h_cola_cats:{job_id}"},
                             {"text":tl_b,"callback_data":f"h_cola_tags:{job_id}"}],
                            [{"text":"🚫 Descartar","callback_data":f"h_cola_discard:{job_id}"},
                             {"text":"⏭ Ir a redacción","callback_data":f"h_cola_skip:{job_id}"}],
                        ]
                        if job_b.get("wp_post_id"):
                            _kb_b.append([{"text":"📢 Publicar en redes","callback_data":f"h_cola_redes:{job_id}"}])
                        try:
                            await query.edit_message_text(card_b, parse_mode="HTML",
                                reply_markup={"inline_keyboard":_kb_b},
                                disable_web_page_preview=True)
                        except Exception:
                            pass

            # ── Ir directo a redacción (sin pub_dest específico) ─────────────
            elif action == "h_cola_skip" and arg:
                job_id = int(arg)
                _br_cola.confirm_cola(job_id, "hoy_normal")
                job = _br_cola.get_job(job_id)
                try:
                    await query.edit_message_text(
                        f"⏭ <b>{(job.get('title') or '')[:60]}</b>\n"
                        f"→ Redacción directa",
                        parse_mode="HTML"
                    )
                except Exception:
                    pass
                await query.answer("⏭ Enviado a redacción", show_alert=False)

            # ── Categorías (multi-select) ─────────────────────────────────────
            elif action == "h_cola_cats" and arg:
                job_id = int(arg)
                job    = _br_cola.get_job(job_id)
                cj = {}
                try:
                    cj = _js.loads(job.get("content_json") or "{}") if job else {}
                except Exception:
                    pass
                sel_ids = cj.get("category_ids", [])
                _CAT_MAP = {
                    94: "Economia", 87: "Politica", 96: "Empresas", 97: "Internacional",
                    100: "Gobierno", 95: "AFIP/ARCA", 90: "Industria", 91: "Opinion",
                    89: "Comercio", 88: "Agro", 103: "Informes", 102: "Provincias",
                    93: "Sindicatos", 92: "Servicios", 239: "Digital pymes", 1139: "Mundo del vino",
                    101: "Poder Judicial", 99: "Congreso", 98: "Nacional",
                    104: "ME TV", 1048: "Coberturas",
                }
                rows = []
                items = list(_CAT_MAP.items())
                for i in range(0, len(items), 2):
                    row = []
                    for cid, cname in items[i:i+2]:
                        prefix = "☑️ " if cid in sel_ids else ""
                        row.append({"text": f"{prefix}{cname}",
                                    "callback_data": f"h_cola_setcat:{job_id}:{cid}"})
                    rows.append(row)
                rows.append([{"text": "↩ Volver", "callback_data": f"h_cola_back:{job_id}"}])
                try:
                    await query.edit_message_text(
                        f"🗂 <b>Categorías — nota #{job_id}</b>",
                        parse_mode="HTML",
                        reply_markup={"inline_keyboard": rows},
                    )
                except Exception:
                    pass
                await query.answer()

            elif action == "h_cola_setcat" and len(parts) >= 3:
                job_id = int(parts[1])
                cat_id = int(parts[2])
                job    = _br_cola.get_job(job_id)
                cj = {}
                try:
                    cj = _js.loads(job.get("content_json") or "{}") if job else {}
                except Exception:
                    pass
                cat_ids = cj.get("category_ids", [])
                if cat_id in cat_ids:
                    cat_ids.remove(cat_id)
                else:
                    cat_ids.append(cat_id)
                cj["category_ids"] = cat_ids
                import sqlite3 as _sq3
                with _sq3.connect("/opt/me-harness/harness.db") as _c3:
                    _c3.execute("UPDATE jobs SET content_json=? WHERE id=?",
                                (_js.dumps(cj), job_id))
                # Refrescar el multi-select
                _CAT_MAP = {
                    94: "Economia", 87: "Politica", 96: "Empresas", 97: "Internacional",
                    100: "Gobierno", 95: "AFIP/ARCA", 90: "Industria", 91: "Opinion",
                    89: "Comercio", 88: "Agro", 103: "Informes", 102: "Provincias",
                    93: "Sindicatos", 92: "Servicios", 239: "Digital pymes", 1139: "Mundo del vino",
                    101: "Poder Judicial", 99: "Congreso", 98: "Nacional",
                    104: "ME TV", 1048: "Coberturas",
                }
                rows = []
                for i in range(0, len(list(_CAT_MAP.items())), 2):
                    row = []
                    for cid, cname in list(_CAT_MAP.items())[i:i+2]:
                        prefix = "☑️ " if cid in cat_ids else ""
                        row.append({"text": f"{prefix}{cname}",
                                    "callback_data": f"h_cola_setcat:{job_id}:{cid}"})
                    rows.append(row)
                rows.append([{"text": "↩ Volver", "callback_data": f"h_cola_back:{job_id}"}])
                try:
                    await query.edit_message_reply_markup(reply_markup={"inline_keyboard": rows})
                except Exception:
                    pass
                await query.answer("✓", show_alert=False)

            # ── Programar fecha ───────────────────────────────────────────────
            elif action == "h_cola_prog" and arg:
                job_id = int(arg)
                try:
                    await query.edit_message_reply_markup(
                        reply_markup={"inline_keyboard": [
                            [{"text": "⚡ Ahora",
                              "callback_data": f"h_cola_prog_set:{job_id}:ahora"}],
                            [{"text": "🕐 Próximo turno",
                              "callback_data": f"h_cola_prog_set:{job_id}:turno"}],
                            [{"text": "📊 Mejor horario del día (GA4)",
                              "callback_data": f"h_cola_prog_set:{job_id}:mejor_horario"}],
                            [{"text": "📅 El finde",
                              "callback_data": f"h_cola_prog_set:{job_id}:finde"}],
                            [{"text": "🗓 Programar día y hora",
                              "callback_data": f"h_cola_prog_set:{job_id}:fecha"}],
                            [{"text": "🤖 Auto (el agente decide)",
                              "callback_data": f"h_cola_prog_set:{job_id}:auto"}],
                            [{"text": "↩ Volver",
                              "callback_data": f"h_cola_back:{job_id}"}],
                        ]}
                    )
                except Exception:
                    pass
                await query.answer()

            elif action == "h_cola_prog_set" and len(parts) >= 3:
                job_id = int(parts[1])
                opcion = parts[2]

                if opcion == "fecha":
                    # Único caso que necesita texto — pide fecha/hora
                    context.user_data["awaiting_cola_prog_for"] = job_id
                    try:
                        await context.bot.send_message(
                            chat_id=query.message.chat_id,
                            text=(
                                f"🗓 <b>Programar — nota #{job_id}</b>\n\n"
                                f"Escribí la fecha y hora:\n"
                                f"<code>30/05 18:30</code>  ·  <code>mañana 10:00</code>"
                            ),
                            parse_mode="HTML",
                            reply_markup={"inline_keyboard": [[
                                {"text": "↩ Volver", "callback_data": f"h_cola_back:{job_id}"}
                            ]]}
                        )
                    except Exception:
                        pass
                    await query.answer()
                else:
                    # Todas las otras opciones se resuelven directamente
                    _PROG_DEST = {
                        "ahora":         "ahora",
                        "turno":         "hoy_normal",
                        "mejor_horario": "mejor_horario",
                        "finde":         "finde",
                        "auto":          None,  # usar sugerencia del agente
                    }
                    _PROG_LABELS = {
                        "ahora":         "⚡ Ahora",
                        "turno":         "🕐 Próximo turno",
                        "mejor_horario": "📊 Mejor horario del día",
                        "finde":         "📅 El finde",
                        "auto":          "🤖 Auto",
                    }
                    pub_dest = _PROG_DEST.get(opcion)
                    if pub_dest is None:
                        # Auto: leer sugerencia del agente
                        job2 = _br_cola.get_job(job_id)
                        sg2  = {}
                        if job2 and job2.get("agent_suggestion"):
                            try:
                                sg2 = _js.loads(job2["agent_suggestion"])
                            except Exception:
                                pass
                        pub_dest = sg2.get("pub_dest") or "hoy_normal"

                    _br_cola.confirm_cola(job_id, pub_dest)
                    label = _PROG_LABELS.get(opcion, opcion)
                    try:
                        await query.edit_message_text(
                            f"✅ <b>{(_br_cola.get_job(job_id) or {}).get('title', '')[:60]}</b>\n"
                            f"→ Redacción  ·  {label}",
                            parse_mode="HTML"
                        )
                    except Exception:
                        pass
                    await query.answer(f"✅ {label}", show_alert=False)

            # ── Ajustar etiquetas desde cola (Volver → cola card) ─────────────
            elif action == "h_cola_kw_add" and arg:
                job_id = int(arg)
                context.user_data["awaiting_cola_kw_for"]         = job_id
                context.user_data["awaiting_cola_kw_panel_msg_id"] = query.message.message_id
                context.user_data["awaiting_cola_kw_panel_chat_id"] = query.message.chat_id
                try:
                    sent = await context.bot.send_message(
                        chat_id=query.message.chat_id,
                        text=(
                            f"✏️ <b>Agregar etiquetas — nota #{job_id}</b>\n\n"
                            f"Separalas con coma, punto o <code> - </code>:\n"
                            f"<code>comercio exterior, plazo fijo, pymes</code>\n"
                            f"<i>Se suman a las actuales. Usá ✖️ en el panel para borrar.</i>"
                        ),
                        parse_mode="HTML",
                        reply_markup={"inline_keyboard": [[
                            {"text": "↩ Volver", "callback_data": f"h_cola_kw_back:{job_id}"}
                        ]]}
                    )
                    context.user_data["awaiting_cola_kw_prompt_msg_id"] = sent.message_id
                except Exception:
                    pass
                await query.answer()

            # ── Volver al panel de keywords desde prompt agregar (cola) ─────────
            elif action == "h_cola_kw_back" and arg:
                job_id        = int(arg)
                panel_msg_id  = context.user_data.pop("awaiting_cola_kw_panel_msg_id",  None)
                panel_chat_id = context.user_data.pop("awaiting_cola_kw_panel_chat_id", None)
                context.user_data.pop("awaiting_cola_kw_for", None)
                context.user_data.pop("awaiting_cola_kw_prompt_msg_id", None)
                try:
                    await query.message.delete()
                except Exception:
                    pass
                if panel_msg_id and panel_chat_id:
                    job2 = _br_cola.get_job(job_id)
                    cj2  = {}
                    if job2 and job2.get("content_json"):
                        try: cj2 = _js.loads(job2["content_json"])
                        except Exception: pass
                    kws2 = cj2.get("matched_kw", [])
                    rows = []
                    for k in kws2:
                        ww = _br_cola.get_keyword_weight(k)
                        em = "🟢" if ww > 0.5 else ("🔴" if ww < -0.5 else "🟡")
                        rows.append([
                            {"text": f"{em} {k}  {ww:+.1f}",
                             "callback_data": f"h_cola_tags:{job_id}"},
                            {"text": "👍",  "callback_data": f"h_cur_kw_up:{job_id}:{k}"},
                            {"text": "👎",  "callback_data": f"h_cur_kw_dn:{job_id}:{k}"},
                            {"text": "✖️",  "callback_data": f"h_cur_kw_rm:{job_id}:{k}"},
                            {"text": "🗑",  "callback_data": f"h_cur_kw_del:{job_id}:{k}"},
                        ])
                    rows.append([{"text": "✏️ Ajustar etiquetas",
                                  "callback_data": f"h_cola_kw_add:{job_id}"}])
                    rows.append([{"text": "↩ Volver",
                                  "callback_data": f"h_cola_back:{job_id}"}])
                    try:
                        await context.bot.edit_message_reply_markup(
                            chat_id=panel_chat_id, message_id=panel_msg_id,
                            reply_markup={"inline_keyboard": rows},
                        )
                    except Exception:
                        pass
                await query.answer()

            # ── Etiquetas cola: 👍/👎/✖️ ─────────────────────────────────────────
            elif action in ("h_cola_kw_up", "h_cola_kw_dn", "h_cola_kw_rm", "h_cola_kw_del") and len(parts) >= 3:
                job_id = int(parts[1])
                kw     = parts[2]
                if action == "h_cola_kw_up":
                    _br_cola.update_keyword_weight(kw, +0.3)
                elif action == "h_cola_kw_dn":
                    _br_cola.update_keyword_weight(kw, -0.3)
                elif action in ("h_cola_kw_rm", "h_cola_kw_del"):
                    # Quitar de matched_kw del job
                    _j_rm = _br_cola.get_job(job_id)
                    if _j_rm:
                        try:
                            _cj_rm = _js.loads(_j_rm.get("content_json") or "{}")
                            _kws_rm = [k for k in _cj_rm.get("matched_kw", []) if k != kw]
                            _cj_rm["matched_kw"] = _kws_rm
                            with _sq.connect(_HDB) as _c_rm:
                                _c_rm.execute("UPDATE jobs SET content_json=? WHERE id=?",
                                              (_js.dumps(_cj_rm), job_id))
                        except Exception:
                            pass
                    # ✖️ = solo de esta nota; 🗑 = descalificada del diario para siempre
                    if action == "h_cola_kw_del":
                        _br_cola.blacklist_keyword(kw)
                # Refrescar panel
                await query.answer({"h_cola_kw_up": "👍 +0.3", "h_cola_kw_dn": "👎 -0.3",
                                    "h_cola_kw_rm": "✖️ Quitada de esta nota",
                                    "h_cola_kw_del": "🗑 Descalificada para siempre"}.get(action, ""))
                # Re-render panel (reusar h_cola_tags)
                _j2 = _br_cola.get_job(job_id)
                _cj2 = _js.loads(_j2.get("content_json") or "{}") if _j2 else {}
                _kws2 = _cj2.get("matched_kw", [])
                _rows2 = []
                for k in _kws2:
                    ww = _br_cola.get_keyword_weight(k)
                    em = "🟢" if ww > 0.5 else ("🔴" if ww < -0.5 else "🟡")
                    _rows2.append([
                        {"text": f"{em} {k}  {ww:+.1f}", "callback_data": f"h_cola_tags:{job_id}"},
                        {"text": "👍", "callback_data": f"h_cola_kw_up:{job_id}:{k}"},
                        {"text": "👎", "callback_data": f"h_cola_kw_dn:{job_id}:{k}"},
                        {"text": "✖️", "callback_data": f"h_cola_kw_rm:{job_id}:{k}"},
                        {"text": "🗑", "callback_data": f"h_cola_kw_del:{job_id}:{k}"},
                    ])
                _rows2.append([{"text": "✏️ Ajustar etiquetas", "callback_data": f"h_cola_kw_add:{job_id}"}])
                _rows2.append([{"text": "↩ Volver", "callback_data": f"h_cola_back:{job_id}"}])
                try:
                    await query.edit_message_reply_markup(reply_markup={"inline_keyboard": _rows2})
                except Exception:
                    pass

            # ── Panel etiquetas/keywords (igual al briefing) ─────────────────
            elif action == "h_cola_tags" and arg:
                # Panel idéntico al del briefing (h_cur_kw) — mismos botones y handlers.
                # Solo cambia "Volver" que va a h_cola_back en lugar de h_cur_volver.
                job_id = int(arg)
                job    = _br_cola.get_job(job_id)
                cj = {}
                try:
                    cj = _js.loads(job.get("content_json") or "{}") if job else {}
                except Exception:
                    pass
                matched_kw = cj.get("matched_kw", [])
                _jtitle = (job.get("title") or "")[:55] if job else ""
                _jurl   = (job.get("source_url") or "") if job else ""
                _hdr = (
                    f"🔑 <b><a href='{_jurl}'>{_jtitle}</a></b>\n"
                    f"<i>👍👎 ajustar peso  ·  ✖️ quitar de nota  ·  🗑 borrar de la base</i>"
                )
                rows = []
                for k in matched_kw:
                    ww = _br_cola.get_keyword_weight(k)
                    em = "🟢" if ww > 0.5 else ("🔴" if ww < -0.5 else "🟡")
                    rows.append([
                        {"text": f"{em} {k}  {ww:+.1f}",
                         "callback_data": f"h_cola_tags:{job_id}"},
                        {"text": "👍",  "callback_data": f"h_cola_kw_up:{job_id}:{k}"},
                        {"text": "👎",  "callback_data": f"h_cola_kw_dn:{job_id}:{k}"},
                        {"text": "✖️",  "callback_data": f"h_cola_kw_rm:{job_id}:{k}"},
                        {"text": "🗑",  "callback_data": f"h_cola_kw_del:{job_id}:{k}"},
                    ])
                rows.append([{"text": "✏️ Ajustar etiquetas",
                              "callback_data": f"h_cola_kw_add:{job_id}"}])
                rows.append([{"text": "↩ Volver",
                              "callback_data": f"h_cola_back:{job_id}"}])
                try:
                    await query.edit_message_text(
                        _hdr, parse_mode="HTML",
                        reply_markup={"inline_keyboard": rows},
                        disable_web_page_preview=True,
                    )
                except Exception:
                    pass
                await query.answer()

            # ── Social agent: mandar al briefing o descartar ─────────────────
            elif action == "h_social_brief" and arg:
                job_id = int(arg)
                # El job ya está en curado — confirmar a Leo
                import broker as _br_soc2
                _j_soc = _br_soc2.get_job(job_id)
                _title_soc = (_j_soc.get("title") or "")[:60] if _j_soc else "?"
                await query.edit_message_text(
                    f"✅ <b>Nota enviada al briefing</b> (job #{job_id})\n"
                    f"<b>{_title_soc}</b>\n\n"
                    f"<i>Aparecerá en el próximo ciclo del curador.</i>",
                    parse_mode="HTML",
                )
                await query.answer("📋 En el briefing")

            elif action == "h_social_drop" and arg:
                job_id = int(arg)
                import broker as _br_soc3
                _br_soc3.update_stage(job_id, "rejected")
                try:
                    await query.message.delete()
                except Exception:
                    pass
                await query.answer("❌ Descartado")

            # ── Restaurar título original ─────────────────────────────────────
            elif action == "h_cola_restore_title" and arg:
                job_id = int(arg)
                import sqlite3 as _sq_rt, json as _js_rt
                try:
                    with _sq_rt.connect("/opt/me-harness/harness.db") as _c_rt:
                        row_rt = _c_rt.execute(
                            "SELECT content_json FROM jobs WHERE id=?", (job_id,)
                        ).fetchone()
                        if row_rt:
                            cj_rt = _js_rt.loads(row_rt[0] or "{}")
                            orig = cj_rt.pop("original_title", "")
                            if orig:
                                _c_rt.execute(
                                    "UPDATE jobs SET title=?, content_json=?, updated_at=datetime('now') WHERE id=?",
                                    (orig, _js_rt.dumps(cj_rt), job_id)
                                )
                                await query.answer(f"↩ Título restaurado", show_alert=False)
                            else:
                                await query.answer("No hay título original guardado", show_alert=True)
                        else:
                            await query.answer("Job no encontrado", show_alert=True)
                except Exception as _e_rt:
                    await query.answer(f"Error: {_e_rt}", show_alert=True)

            # ── Cambiar título del job en cola ───────────────────────────────
            elif action == "h_cola_change_title" and arg:
                job_id = int(arg)
                context.user_data["awaiting_cola_title_for"]    = job_id
                context.user_data["awaiting_cola_title_msg_id"] = query.message.message_id
                sent = await query.message.reply_text("✏️ Escribí el nuevo título:")
                context.user_data["awaiting_cola_title_prompt_id"] = sent.message_id
                await query.answer()

            # ── Descartar de la cola ──────────────────────────────────────────
            elif action == "h_cola_discard" and arg:
                job_id = int(arg)
                _br_cola.update_stage(job_id, "rejected")
                try:
                    await query.message.delete()
                except Exception:
                    pass
                await query.answer("🚫 Descartado", show_alert=False)

            # ── Listado compacto de la cola ───────────────────────────────────
            elif action == "h_cola_list":
                jobs_list = _br_cola.dequeue_cola(limit=30)
                _DL = {"hoy_portada":"📌","hoy_normal":"📰","finde":"📅",
                       "fecha":"🗓","recurrencia_a":"🔁","recurrencia_b":"📋"}
                lines = [f"<b>Cola — {len(jobs_list)} nota(s)</b>"]
                for j in jobs_list:
                    dest  = j.get("pub_dest") or "—"
                    icon  = _DL.get(dest, "·")
                    t     = (j.get("title") or "")[:55]
                    lines.append(f"{icon} #{j['id']}  {t}")
                try:
                    await query.edit_message_text(
                        "\n".join(lines), parse_mode="HTML",
                        reply_markup={"inline_keyboard": [[
                            {"text": "↩ Cerrar", "callback_data": "h_cola_close_list"}
                        ]]}
                    )
                except Exception:
                    pass
                await query.answer()

            elif action == "h_cola_close_list":
                try:
                    await query.message.delete()
                except Exception:
                    pass
                await query.answer()

            # ── Publicar en redes (nota ya publicada en WP) ───────────────────
            elif action == "h_cola_redes" and arg:
                job_id = int(arg)
                job    = _br_cola.get_job(job_id)
                if not job or not job.get("wp_post_id"):
                    await query.answer("⚠️ La nota no está publicada en WP aún", show_alert=True)
                else:
                    await query.answer("📢 Difundiendo...", show_alert=False)
                    try:
                        from agents import publicador as _pub
                        cj     = {}
                        if job.get("content_json"):
                            try: cj = _js.loads(job["content_json"])
                            except Exception: pass
                        tags    = cj.get("tag_names", []) or cj.get("matched_kw", []) or []
                        excerpt = cj.get("excerpt", "") or cj.get("meta_desc", "") or ""
                        social  = _pub.post_to_social(
                            wp_post_id=job["wp_post_id"],
                            wp_url=job["wp_url"],
                            title=job.get("title", ""),
                            excerpt=excerpt[:280],
                            tags=tags,
                        )
                        tg_ok = "✅" if social.get("tg_msg_id") else "❌"
                        tw_ok = "✅" if social.get("tweet_id")  else "❌"
                        try:
                            await query.edit_message_text(
                                f"📢 <b>Difundido</b> — {(job.get('title') or '')[:60]}\n"
                                f"📱 Telegram canal: {tg_ok}  ·  🐦 Twitter: {tw_ok}",
                                parse_mode="HTML"
                            )
                        except Exception:
                            pass
                    except Exception as _e:
                        await query.answer(f"❌ Error: {_e}", show_alert=True)

            # ── Panel principal (volver desde submenús) ───────────────────────
            elif action == "h_cola_main_panel":
                jobs_mp = _br_cola.dequeue_cola(limit=20)
                with _sq.connect(_HDB) as _c_mp:
                    n_pend_mp = _c_mp.execute(
                        "SELECT COUNT(*) FROM jobs WHERE stage IN ('redaccion','publicacion','sin_imagen')"
                    ).fetchone()[0]
                    n_prog_mp = _c_mp.execute(
                        "SELECT COUNT(*) FROM jobs WHERE pub_date IS NOT NULL AND pub_date != '' "
                        "AND stage NOT IN ('done','rejected')"
                    ).fetchone()[0]
                man_mp = sum(
                    1 for j in jobs_mp
                    if j.get("agent_suggestion") and
                    "cambiado manualmente" in (
                        _js.loads(j["agent_suggestion"]).get("reasoning", "")
                        if j["agent_suggestion"] else ""
                    )
                )
                res_mp = "🗂 <b>Cola de publicación</b>"
                if jobs_mp:
                    res_mp += f"\n📋 A procesar: <b>{len(jobs_mp)}</b>"
                if n_pend_mp:
                    res_mp += f"\n⏳ Pendientes en pipeline: <b>{n_pend_mp}</b>"
                if n_prog_mp:
                    res_mp += f"\n🗓 Programadas: <b>{n_prog_mp}</b>"
                kb_mp = []
                if jobs_mp:
                    kb_mp.append([{"text": f"📋 Notas a procesar ({len(jobs_mp)})",
                                   "callback_data": "h_cola_notas_a_procesar"}])
                _proc_row_mp = []
                if jobs_mp:
                    _proc_row_mp.append({"text": f"📤 Procesar listado ({man_mp})", "callback_data": "h_cola_process_list"})
                if n_pend_mp:
                    _proc_row_mp.append({"text": f"🔄 Procesar pendientes ({n_pend_mp})", "callback_data": "h_cola_process_pend"})
                if _proc_row_mp:
                    kb_mp.append(_proc_row_mp)
                if jobs_mp or n_pend_mp:
                    kb_mp.append([{"text": "🚀 Procesar todo", "callback_data": "h_cola_process_all"}])
                if n_pend_mp:
                    kb_mp.append([{"text": f"⏳ Ver pendientes ({n_pend_mp})",
                                   "callback_data": "h_cola_ver_pendientes"}])
                kb_mp.append([{"text": "🗓 Ver notas programadas", "callback_data": "h_cola_ver_programadas"}])
                kb_mp.append([{"text": "🛑 Cancelar publicador",   "callback_data": "h_cola_stop_pub"}])
                try:
                    await query.edit_message_text(
                        res_mp, parse_mode="HTML",
                        reply_markup={"inline_keyboard": kb_mp}
                    )
                except Exception:
                    pass
                await query.answer()

            # ── Notas a procesar — despliega cards individuales ─────────────
            elif action == "h_cola_notas_a_procesar":
                context.user_data["cola_back_src"] = "notas"
                jobs_ap = _br_cola.dequeue_cola(limit=20)
                if not jobs_ap:
                    await query.answer("No hay notas en la cola", show_alert=True)
                    return
                chat_id_ap = query.message.chat_id
                try:
                    await query.edit_message_text(
                        f"📋 <b>Notas a procesar</b> — {len(jobs_ap)} nota(s)\n"
                        "<i>Configurá cada una y luego usá 'Procesar todo'.</i>",
                        parse_mode="HTML",
                        reply_markup={"inline_keyboard": [[
                            {"text": "🔄 Actualizar", "callback_data": "h_cola_notas_a_procesar"},
                            {"text": "🚀 Procesar todo", "callback_data": "h_cola_process_all"},
                        ]]}
                    )
                except Exception:
                    pass
                _HILO_N_AP = {1:"CAPA 1", 2:"CAPA 2", 3:"CAPA 3"}
                _CN_AP = {94:"Economía",87:"Política",96:"Empresas",97:"Internacional",
                           100:"Gobierno",95:"AFIP/ARCA",90:"Industria",91:"Opinión",
                           89:"Comercio",88:"Agro",103:"Informes",102:"Provincias",
                           93:"Sindicatos",92:"Servicios",239:"Digital pymes",1139:"Mundo del vino",
                           101:"Poder Judicial",99:"Congreso",98:"Nacional",
                           104:"ME TV",1048:"Coberturas"}
                card_msg_ids_ap = context.user_data.get("cola_msg_ids", [])
                for j_ap in jobs_ap:
                    jid_ap   = j_ap["id"]
                    ttl_ap   = j_ap.get("title") or "Sin título"
                    hilo_ap  = j_ap.get("hilo") or 2
                    url_ap   = j_ap.get("source_url", "")
                    dom_ap   = (url_ap.split("//",1)[-1].split("/")[0].replace("www.","") if url_ap else "")
                    capa_ap  = _HILO_N_AP.get(hilo_ap, f"CAPA {hilo_ap}")
                    sg_ap = {}
                    if j_ap.get("agent_suggestion"):
                        try: sg_ap = _js.loads(j_ap["agent_suggestion"])
                        except Exception: pass
                    pd_ap    = sg_ap.get("pub_dest") or j_ap.get("pub_dest") or "sin_asignar"
                    rs_ap    = sg_ap.get("reasoning", "")
                    dl_ap    = _DEST_LABELS.get(pd_ap, pd_ap)
                    sl_ap    = f"\n🤖 <b>{dl_ap}</b>" + (f"\n<i>{rs_ap}</i>" if rs_ap else "")
                    cj_ap = {}
                    if j_ap.get("content_json"):
                        try: cj_ap = _js.loads(j_ap["content_json"])
                        except Exception: pass
                    tags_ap    = cj_ap.get("matched_kw") or []
                    cats_ap    = cj_ap.get("category_ids") or []
                    fmt_ap     = cj_ap.get("formato", "desplegable")
                    port_ap    = cj_ap.get("portada", False)
                    ot_ap      = cj_ap.get("original_title", "")
                    pt_ap      = cj_ap.get("proposed_title", "")
                    hc_ap      = bool(ot_ap and ot_ap != ttl_ap)
                    inst_ap    = cj_ap.get("instructions", "")
                    tl_ap   = (f"\n🏷 <i>{' · '.join(t.title() for t in tags_ap[:5])}</i>" if tags_ap else "")
                    cl_ap   = (f"\n🗂 <i>{' · '.join(_CN_AP.get(c,str(c)) for c in cats_ap[:3])}</i>" if cats_ap else "")
                    pdl_ap  = (f"\n🗓 <i>{j_ap.get('pub_date','')}</i>" if j_ap.get("pub_date") else "")
                    porl_ap = "\n📌 <b>PORTADA DEL DÍA</b>" if port_ap else ""
                    fml_ap  = f"\n📄 <i>{fmt_ap.title()}</i>" if fmt_ap != "desplegable" else ""
                    il_ap   = f"\n📝 <i>{inst_ap}</i>" if inst_ap else ""
                    titl_ap = ""
                    if ot_ap and ot_ap != ttl_ap:
                        titl_ap += f"\n<s>{ot_ap[:70]}</s>"
                    if pt_ap and pt_ap != ttl_ap:
                        titl_ap += f"\n💡 <i>{pt_ap[:70]}</i>"
                    tlbl_ap = (f"🏷 {', '.join(t.title() for t in tags_ap[:3])}" if tags_ap else "🏷 Etiquetas")
                    clbl_ap = (f"🗂 {'+'.join(_CN_AP.get(c,str(c))[:8] for c in cats_ap[:2])}" if cats_ap else "🗂 Categoría")
                    flbl_ap = "☑️ Continua" if fmt_ap == "continua" else "☑️ Desplegable"
                    plbl_ap = "☑️ Portada del día" if port_ap else "📌 Portada del día"
                    tbtn_ap = "↩ Restaurar original" if hc_ap else "✏️ Cambiar título"
                    tcb_ap  = f"h_cola_restore_title:{jid_ap}" if hc_ap else f"h_cola_change_title:{jid_ap}"
                    card_txt_ap = (
                        f"<b>{ttl_ap}</b>{titl_ap}\n"
                        f"{capa_ap}  ·  {dom_ap}"
                        f"{sl_ap}{porl_ap}{cl_ap}{tl_ap}{fml_ap}{pdl_ap}{il_ap}\n"
                        f"<a href='{url_ap}'>📎 fuente</a>"
                    )
                    kb_ap = [
                        [{"text":"✅ Publicar","callback_data":f"h_cola_confirm:{jid_ap}"},
                         {"text":"🔄 Cambiar destino","callback_data":f"h_cola_change:{jid_ap}"}],
                        [{"text":plbl_ap,"callback_data":f"h_cola_portada:{jid_ap}"},
                         {"text":flbl_ap,"callback_data":f"h_cola_formato:{jid_ap}"}],
                        [{"text":tbtn_ap,"callback_data":tcb_ap}],
                        [{"text":clbl_ap,"callback_data":f"h_cola_cats:{jid_ap}"},
                         {"text":tlbl_ap,"callback_data":f"h_cola_tags:{jid_ap}"}],
                        [{"text":"🚫 Descartar","callback_data":f"h_cola_discard:{jid_ap}"},
                         {"text":"⏭ Ir a redacción","callback_data":f"h_cola_skip:{jid_ap}"}],
                    ]
                    if j_ap.get("wp_post_id"):
                        kb_ap.append([{"text":"📢 Publicar en redes","callback_data":f"h_cola_redes:{jid_ap}"}])
                    try:
                        cm_ap = await context.bot.send_message(
                            chat_id=chat_id_ap, text=card_txt_ap, parse_mode="HTML",
                            reply_markup={"inline_keyboard": kb_ap},
                            disable_web_page_preview=True,
                        )
                        card_msg_ids_ap.append(cm_ap.message_id)
                    except Exception:
                        pass
                context.user_data["cola_msg_ids"] = card_msg_ids_ap
                await query.answer(f"📋 {len(jobs_ap)} nota(s) cargadas")

            # ── Procesar pendientes — re-ejecuta agentes de pipeline ─────────
            elif action == "h_cola_process_pend":
                with _sq.connect(_HDB) as _c_pp0:
                    n_pp0 = _c_pp0.execute(
                        "SELECT COUNT(*) FROM jobs WHERE stage IN ('redaccion','publicacion','sin_imagen')"
                    ).fetchone()[0]
                _ran = []
                for _ag_name in ("redaccion", "publicacion"):
                    try:
                        _ag_mod = __import__(f"agents.{_ag_name}", fromlist=["run_once"])
                        await asyncio.to_thread(_ag_mod.run_once)
                        _ran.append(_ag_name)
                    except Exception:
                        pass
                with _sq.connect(_HDB) as _c_pp1:
                    n_pp1 = _c_pp1.execute(
                        "SELECT COUNT(*) FROM jobs WHERE stage IN ('redaccion','publicacion','sin_imagen')"
                    ).fetchone()[0]
                _mov = max(0, n_pp0 - n_pp1)
                _ran_txt = " · ".join(_ran) if _ran else "ninguno"
                try:
                    await query.edit_message_text(
                        f"🔄 <b>Pendientes procesados</b>\n"
                        f"Agentes: {_ran_txt}\n"
                        f"Antes: {n_pp0}  ·  Después: {n_pp1}"
                        + (f"  ·  Avanzaron: {_mov}" if _mov else ""),
                        parse_mode="HTML",
                        reply_markup={"inline_keyboard": [[
                            {"text": "↩ Volver", "callback_data": "h_cola_main_panel"}
                        ]]}
                    )
                except Exception:
                    pass
                await query.answer(f"🔄 {n_pp1} pendientes restantes", show_alert=False)
                # Enviar card de resolución para cada job sin_imagen
                with _sq.connect(_HDB) as _c_si:
                    _si_rows = _c_si.execute(
                        "SELECT id, title, source_url, content_json FROM jobs WHERE stage='sin_imagen' ORDER BY created_at"
                    ).fetchall()
                for _si_id, _si_title, _si_src, _si_cj in _si_rows:
                    _si_c = {}
                    try: _si_c = _js.loads(_si_cj or "{}")
                    except Exception: pass
                    _si_url = _si_c.get("source") or _si_c.get("source_url") or _si_src or ""
                    _si_link = f'\n<a href="{_si_url}">Ver fuente</a>' if _si_url else ""
                    await context.bot.send_message(
                        chat_id=query.message.chat_id,
                        text=f"🖼 <b>Sin imagen — elegí foto (job #{_si_id})</b>\n\n<b>{(_si_title or '')[:80]}</b>{_si_link}",
                        parse_mode="HTML", disable_web_page_preview=True,
                        reply_markup={"inline_keyboard": [
                            [{"text": "🔍 Buscar en ME.ar", "callback_data": f"h_img_search:{_si_id}"},
                             {"text": "🔗 Agregar URL",      "callback_data": f"h_img_url:{_si_id}"}],
                            [{"text": "➖ Publicar sin foto","callback_data": f"h_img_skip:{_si_id}"}],
                        ]}
                    )

            # ── Procesar listado — solo las que Leo configuró manualmente ────
            elif action == "h_cola_process_list":
                jobs_all = _br_cola.dequeue_cola(limit=50)
                procesadas = 0
                for j in jobs_all:
                    sg = {}
                    if j.get("agent_suggestion"):
                        try: sg = _js.loads(j["agent_suggestion"])
                        except Exception: pass
                    if "cambiado manualmente" not in sg.get("reasoning", ""):
                        continue  # saltear las no tocadas por Leo
                    dest = sg.get("pub_dest") or j.get("pub_dest") or "hoy_normal"
                    _br_cola.confirm_cola(j["id"], dest,
                                          anchor_date=sg.get("anchor_date"),
                                          recurrence_type=sg.get("recurrence_type"))
                    procesadas += 1
                try:
                    await query.edit_message_text(
                        f"📤 <b>{procesadas} nota(s) del listado enviadas a redacción</b>\n"
                        f"<i>Las no configuradas siguen en la cola.</i>",
                        parse_mode="HTML"
                    )
                except Exception:
                    pass
                await query.answer(f"📤 {procesadas} notas procesadas", show_alert=False)

            # ── Procesar todo — confirma cola + re-ejecuta agentes pipeline ──
            elif action == "h_cola_process_all":
                jobs_all = _br_cola.dequeue_cola(limit=50)
                procesadas = 0
                for j in jobs_all:
                    jid2 = j["id"]
                    sg = {}
                    if j.get("agent_suggestion"):
                        try:
                            sg = _js.loads(j["agent_suggestion"])
                        except Exception:
                            pass
                    dest = sg.get("pub_dest") or j.get("pub_dest") or "hoy_normal"
                    _br_cola.confirm_cola(jid2, dest,
                                          anchor_date=sg.get("anchor_date"),
                                          recurrence_type=sg.get("recurrence_type"))
                    procesadas += 1
                # También re-ejecutar agentes de pipeline
                for _ag_pa in ("redaccion", "publicacion"):
                    try:
                        _ag_pa_mod = __import__(f"agents.{_ag_pa}", fromlist=["run_once"])
                        await asyncio.to_thread(_ag_pa_mod.run_once)
                    except Exception:
                        pass
                with _sq.connect(_HDB) as _c_pa:
                    n_pend_pa = _c_pa.execute(
                        "SELECT COUNT(*) FROM jobs WHERE stage IN ('redaccion','publicacion','sin_imagen')"
                    ).fetchone()[0]
                try:
                    await query.edit_message_text(
                        f"🚀 <b>{procesadas} nota(s) de cola enviadas a redacción</b>\n"
                        f"<i>Pipeline: {n_pend_pa} jobs en curso.</i>",
                        parse_mode="HTML"
                    )
                except Exception:
                    pass
                await query.answer(f"✅ {procesadas} notas procesadas", show_alert=False)
                # Enviar card de resolución para cada job sin_imagen
                with _sq.connect(_HDB) as _c_si2:
                    _si_rows2 = _c_si2.execute(
                        "SELECT id, title, source_url, content_json FROM jobs WHERE stage='sin_imagen' ORDER BY created_at"
                    ).fetchall()
                for _si_id2, _si_title2, _si_src2, _si_cj2 in _si_rows2:
                    _si_c2 = {}
                    try: _si_c2 = _js.loads(_si_cj2 or "{}")
                    except Exception: pass
                    _si_url2 = _si_c2.get("source") or _si_c2.get("source_url") or _si_src2 or ""
                    _si_link2 = f'\n<a href="{_si_url2}">Ver fuente</a>' if _si_url2 else ""
                    await context.bot.send_message(
                        chat_id=query.message.chat_id,
                        text=f"🖼 <b>Sin imagen — elegí foto (job #{_si_id2})</b>\n\n<b>{(_si_title2 or '')[:80]}</b>{_si_link2}",
                        parse_mode="HTML", disable_web_page_preview=True,
                        reply_markup={"inline_keyboard": [
                            [{"text": "🔍 Buscar en ME.ar", "callback_data": f"h_img_search:{_si_id2}"},
                             {"text": "🔗 Agregar URL",      "callback_data": f"h_img_url:{_si_id2}"}],
                            [{"text": "➖ Publicar sin foto","callback_data": f"h_img_skip:{_si_id2}"}],
                        ]}
                    )

            # ── Notas programadas ─────────────────────────────────────────────
            elif action == "h_cola_ver_programadas":
                import requests as _req_prog, base64 as _b64_prog
                from config import WP_URL as _WP_PROG, WP_USER as _WPU_PROG, WP_PASS as _WPP_PROG
                _tok_prog = _b64_prog.b64encode(f"{_WPU_PROG}:{_WPP_PROG}".encode()).decode()
                _hdrs_prog = {"Authorization": f"Basic {_tok_prog}"}

                # Jobs en DB con pub_date pendiente
                with _sq.connect(_HDB) as _c_p:
                    rows_p = _c_p.execute(
                        "SELECT id, title, pub_date, stage FROM jobs "
                        "WHERE pub_date IS NOT NULL AND pub_date != '' "
                        "AND stage NOT IN ('done','rejected') ORDER BY pub_date ASC LIMIT 10"
                    ).fetchall()

                # Posts WP con status=future
                wp_future = []
                try:
                    _rf = _req_prog.get(
                        f"{_WP_PROG}/wp-json/wp/v2/posts?status=future&per_page=20"
                        "&_fields=id,title,date,link",
                        headers=_hdrs_prog, timeout=10
                    )
                    if _rf.ok:
                        wp_future = _rf.json()
                except Exception:
                    pass

                if not rows_p and not wp_future:
                    await query.answer("No hay notas programadas", show_alert=True)
                    return

                lines = ["🗓 <b>Notas programadas</b>\n"]
                kb_p  = []

                # Jobs en pipeline con pub_date
                for r in rows_p:
                    jid, title, pub_date, stage = r
                    lines.append(f"⏳ <b>{(title or '')[:55]}</b>\n  📅 {pub_date[:16]}  ·  {stage}")
                    kb_p.append([
                        {"text": f"✏️ #{jid} {(title or '')[:25]}", "callback_data": f"h_cola_back:{jid}"},
                        {"text": "❌ Cancelar",                      "callback_data": f"h_cola_cancel_job:{jid}"},
                    ])

                # Posts WP future
                for wp in wp_future:
                    wp_id    = wp["id"]
                    wp_title = wp.get("title", {}).get("rendered", "")[:55]
                    wp_date  = wp.get("date", "")[:16].replace("T", " ")
                    lines.append(f"📅 <b>{wp_title}</b>\n  🗓 {wp_date}")
                    kb_p.append([
                        {"text": f"🕐 Reprogramar",  "callback_data": f"h_wp_reschedule:{wp_id}"},
                        {"text": f"⚡ Publicar ya",   "callback_data": f"h_wp_publish_now:{wp_id}"},
                        {"text": f"📝 Borrador",      "callback_data": f"h_wp_to_draft:{wp_id}"},
                    ])

                kb_p.append([
                    {"text": "🔄 Actualizar", "callback_data": "h_cola_ver_programadas"},
                    {"text": "↩ Volver",      "callback_data": "h_cola_main_panel"},
                ])
                try:
                    await query.edit_message_text(
                        "\n".join(lines), parse_mode="HTML",
                        reply_markup={"inline_keyboard": kb_p},
                        disable_web_page_preview=True,
                    )
                except Exception:
                    pass
                await query.answer()

            # ── Acciones sobre posts WP future ───────────────────────────────
            elif action in ("h_wp_publish_now", "h_wp_to_draft", "h_wp_reschedule") and arg:
                wp_id = int(arg)
                import requests as _req_wp, base64 as _b64_wp
                from config import WP_URL as _WP_ACT, WP_USER as _WPU_ACT, WP_PASS as _WPP_ACT
                _tok_wp = _b64_wp.b64encode(f"{_WPU_ACT}:{_WPP_ACT}".encode()).decode()
                _hdrs_wp = {"Authorization": f"Basic {_tok_wp}", "Content-Type": "application/json"}

                if action == "h_wp_publish_now":
                    r_wp = _req_wp.post(
                        f"{_WP_ACT}/wp-json/wp/v2/posts/{wp_id}",
                        headers=_hdrs_wp, json={"status": "publish"}, timeout=15
                    )
                    if r_wp.ok:
                        await query.answer("⚡ Publicado ahora", show_alert=False)
                    else:
                        await query.answer(f"❌ Error: {r_wp.status_code}", show_alert=True)
                    # Refrescar listado
                    query.data = "h_cola_ver_programadas"
                    await query.answer()

                elif action == "h_wp_to_draft":
                    r_wp = _req_wp.post(
                        f"{_WP_ACT}/wp-json/wp/v2/posts/{wp_id}",
                        headers=_hdrs_wp, json={"status": "draft"}, timeout=15
                    )
                    if r_wp.ok:
                        await query.answer("📝 Movido a borrador", show_alert=False)
                    else:
                        await query.answer(f"❌ Error: {r_wp.status_code}", show_alert=True)

                elif action == "h_wp_reschedule":
                    context.user_data["awaiting_reschedule_wp_id"]     = wp_id
                    context.user_data["awaiting_reschedule_msg_id"]     = query.message.message_id
                    await query.answer()
                    await context.bot.send_message(
                        chat_id=query.message.chat_id,
                        text=(f"🗓 <b>Reprogramar WP #{wp_id}</b>\n\n"
                              f"Escribí la nueva fecha y hora:\n"
                              f"<code>15/07 10:00</code>  ·  <code>2026-07-15T10:00:00</code>"),
                        parse_mode="HTML",
                        reply_markup={"inline_keyboard": [[
                            {"text": "↩ Volver", "callback_data": "h_cola_ver_programadas"}
                        ]]}
                    )

            # ── Notas pendientes de procesamiento ─────────────────────────────
            elif action == "h_cola_ver_pendientes":
                context.user_data["cola_back_src"] = "pendientes"
                _STAGE_EMOJI = {
                    "redaccion":  "✍️", "publicacion": "📤",
                    "sin_imagen": "🖼", "cola": "🗂",
                }
                with _sq.connect(_HDB) as _c_pend:
                    rows_pend = _c_pend.execute(
                        "SELECT id, title, stage, updated_at FROM jobs "
                        "WHERE stage IN ('redaccion','publicacion','sin_imagen') "
                        "ORDER BY updated_at ASC LIMIT 20"
                    ).fetchall()
                if not rows_pend:
                    await query.answer("No hay notas pendientes", show_alert=True)
                    return
                lines_pend = ["⏳ <b>Pendientes de procesamiento</b>\n"]
                kb_pend = []
                for r in rows_pend:
                    jid, title, stage, upd = r
                    em = _STAGE_EMOJI.get(stage, "•")
                    lines_pend.append(f"{em} <b>{(title or '')[:55]}</b>  ·  {stage}")
                    row_btns = [{"text": f"✏️ #{jid} {(title or '')[:20]}", "callback_data": f"h_cola_back:{jid}"}]
                    if stage == "sin_imagen":
                        row_btns.append({"text": "🖼 Foto",          "callback_data": f"h_img_url:{jid}"})
                    row_btns.append({"text": "❌ Cancelar",           "callback_data": f"h_cola_cancel_job:{jid}"})
                    kb_pend.append(row_btns)
                kb_pend.append([
                    {"text": "🔄 Actualizar", "callback_data": "h_cola_ver_pendientes"},
                    {"text": "↩ Volver",      "callback_data": "h_cola_main_panel"},
                ])
                try:
                    await query.edit_message_text(
                        "\n".join(lines_pend), parse_mode="HTML",
                        reply_markup={"inline_keyboard": kb_pend}
                    )
                except Exception:
                    pass
                await query.answer()

            # ── Cancelar job individual desde pendientes ──────────────────────
            elif action == "h_cola_cancel_job" and arg:
                job_id = int(arg)
                with _sq.connect(_HDB) as _c_cj:
                    _c_cj.execute(
                        "UPDATE jobs SET stage='rejected', updated_at=datetime('now') WHERE id=?",
                        (job_id,)
                    )
                await query.answer(f"❌ Job #{job_id} cancelado", show_alert=False)
                # Refrescar el listado de pendientes
                _STAGE_EMOJI2 = {"redaccion": "✍️", "publicacion": "📤", "sin_imagen": "🖼"}
                with _sq.connect(_HDB) as _c_ref:
                    rows_ref = _c_ref.execute(
                        "SELECT id, title, stage, updated_at FROM jobs "
                        "WHERE stage IN ('redaccion','publicacion','sin_imagen') "
                        "ORDER BY updated_at ASC LIMIT 20"
                    ).fetchall()
                if not rows_ref:
                    try:
                        await query.edit_message_text(
                            "⏳ <b>Pendientes</b> — ninguna.", parse_mode="HTML",
                            reply_markup={"inline_keyboard": [[
                                {"text": "↩ Volver", "callback_data": "h_cola_main_panel"}
                            ]]}
                        )
                    except Exception:
                        pass
                else:
                    lines_r = ["⏳ <b>Pendientes de procesamiento</b>\n"]
                    kb_r = []
                    for r in rows_ref:
                        jid2, title2, stage2, _ = r
                        em2 = _STAGE_EMOJI2.get(stage2, "•")
                        lines_r.append(f"{em2} <b>{(title2 or '')[:55]}</b>  ·  {stage2}")
                        row2 = [{"text": f"✏️ #{jid2} {(title2 or '')[:20]}", "callback_data": f"h_cola_back:{jid2}"}]
                        if stage2 == "sin_imagen":
                            row2.append({"text": "🖼 Foto", "callback_data": f"h_img_url:{jid2}"})
                        row2.append({"text": "❌ Cancelar", "callback_data": f"h_cola_cancel_job:{jid2}"})
                        kb_r.append(row2)
                    kb_r.append([
                        {"text": "🔄 Actualizar", "callback_data": "h_cola_ver_pendientes"},
                        {"text": "↩ Volver",      "callback_data": "h_cola_main_panel"},
                    ])
                    try:
                        await query.edit_message_text(
                            "\n".join(lines_r), parse_mode="HTML",
                            reply_markup={"inline_keyboard": kb_r}
                        )
                    except Exception:
                        pass

            # ── Cancelar publicador — borra cards y pausa publicacion ────────
            elif action == "h_cola_stop_pub":
                chat_id = query.message.chat_id
                # Borrar todos los cards de esta sesión + el resumen
                msg_ids = context.user_data.pop("cola_msg_ids", [])
                msg_ids.append(query.message.message_id)
                for _mid in set(msg_ids):
                    try:
                        await context.bot.delete_message(chat_id=chat_id, message_id=_mid)
                    except Exception:
                        pass
                # Mover jobs en publicacion de vuelta a cola
                with _sq.connect(_HDB) as _c3:
                    _c3.execute(
                        "UPDATE jobs SET stage='cola', updated_at=datetime('now') "
                        "WHERE stage='publicacion'"
                    )
                await query.answer("🛑 Cancelado", show_alert=False)

            # ── Volver al card original ───────────────────────────────────────
            elif action == "h_cola_back" and arg:
                job_id = int(arg)
                job    = _br_cola.get_job(job_id)
                if not job:
                    await query.answer()
                    return
                # Reconstruir card idéntico al de cmd_coladepublicacion
                _title  = job.get("title") or "Sin título"
                _hilo   = job.get("hilo") or 2
                _url    = job.get("source_url", "")
                _domain = (_url.split("//",1)[-1].split("/")[0].replace("www.","") if _url else "")
                _HILO_N = {1:"CAPA 1", 2:"CAPA 2", 3:"CAPA 3"}
                _capa   = _HILO_N.get(_hilo, f"CAPA {_hilo}")
                sg = {}
                if job.get("agent_suggestion"):
                    try: sg = _js.loads(job["agent_suggestion"])
                    except Exception: pass
                pub_dest  = sg.get("pub_dest") or job.get("pub_dest") or "sin_asignar"
                reasoning = sg.get("reasoning", "")
                dest_lbl  = _DEST_LABELS.get(pub_dest, pub_dest)
                sug_line  = f"\n🤖 <b>{dest_lbl}</b>" + (f"\n<i>{reasoning}</i>" if reasoning else "")
                cj = {}
                if job.get("content_json"):
                    try: cj = _js.loads(job["content_json"])
                    except Exception: pass
                tags    = cj.get("matched_kw") or []
                cat_ids = cj.get("category_ids") or []
                _CN = {94:"Economía",87:"Política",96:"Empresas",97:"Internacional",
                       100:"Gobierno",95:"AFIP/ARCA",90:"Industria",91:"Opinión",
                       89:"Comercio",88:"Agro",103:"Informes",102:"Provincias",
                       93:"Sindicatos",92:"Servicios",239:"Digital pymes",1139:"Mundo del vino",
                       101:"Poder Judicial",99:"Congreso",98:"Nacional",
                       104:"ME TV",1048:"Coberturas"}
                _formato_b      = cj.get("formato", "desplegable")
                _portada_b      = cj.get("portada", False)
                original_title  = cj.get("original_title", "")
                proposed_title  = cj.get("proposed_title", "")
                has_custom      = bool(original_title and original_title != _title)
                inst            = cj.get("instructions", "")
                tags_line     = (f"\n🏷 <i>{' · '.join(t.title() for t in tags[:5])}</i>" if tags else "")
                cats_line     = (f"\n🗂 <i>{' · '.join(_CN.get(c,str(c)) for c in cat_ids[:3])}</i>" if cat_ids else "")
                pub_date_line = (f"\n🗓 <i>{job.get('pub_date','')}</i>" if job.get("pub_date") else "")
                portada_line  = "\n📌 <b>PORTADA DEL DÍA</b>" if _portada_b else ""
                fmt_line      = f"\n📄 <i>{_formato_b.title()}</i>" if _formato_b != "desplegable" else ""
                inst_line     = f"\n📝 <i>{inst}</i>" if inst else ""
                title_lines   = ""
                if original_title and original_title != _title:
                    title_lines += f"\n<s>{original_title[:70]}</s>"
                if proposed_title and proposed_title != _title:
                    title_lines += f"\n💡 <i>{proposed_title[:70]}</i>"
                tags_lbl    = (f"🏷 {', '.join(t.title() for t in tags[:3])}" if tags else "🏷 Etiquetas")
                cats_lbl    = (f"🗂 {'+'.join(_CN.get(c,str(c))[:8] for c in cat_ids[:2])}" if cat_ids else "🗂 Categoría")
                fmt_lbl     = "☑️ Continua" if _formato_b == "continua" else "☑️ Desplegable"
                port_lbl    = "☑️ Portada del día" if _portada_b else "📌 Portada del día"
                titulo_btn  = "↩ Restaurar original" if has_custom else "✏️ Cambiar título"
                titulo_cb   = f"h_cola_restore_title:{job_id}" if has_custom else f"h_cola_change_title:{job_id}"
                card_msg = (
                    f"<b>{_title}</b>{title_lines}\n"
                    f"{_capa}  ·  {_domain}"
                    f"{sug_line}{portada_line}{cats_line}{tags_line}{fmt_line}{pub_date_line}{inst_line}\n"
                    f"<a href='{_url}'>📎 fuente</a>"
                )
                _kb_rows = [
                    [{"text":"✅ Publicar","callback_data":f"h_cola_confirm:{job_id}"},
                     {"text":"🔄 Cambiar destino","callback_data":f"h_cola_change:{job_id}"}],
                    [{"text":port_lbl,"callback_data":f"h_cola_portada:{job_id}"},
                     {"text":fmt_lbl,"callback_data":f"h_cola_formato:{job_id}"}],
                    [{"text":titulo_btn,"callback_data":titulo_cb}],
                    [{"text":cats_lbl,"callback_data":f"h_cola_cats:{job_id}"},
                     {"text":tags_lbl,"callback_data":f"h_cola_tags:{job_id}"}],
                    [{"text":"🚫 Descartar","callback_data":f"h_cola_discard:{job_id}"},
                     {"text":"⏭ Ir a redacción","callback_data":f"h_cola_skip:{job_id}"}],
                ]
                if job.get("wp_post_id"):
                    _kb_rows.append([{"text":"📢 Publicar en redes","callback_data":f"h_cola_redes:{job_id}"}])
                _back_src = context.user_data.get("cola_back_src", "")
                if _back_src == "pendientes":
                    _kb_rows.append([{"text":"↩ Ver pendientes","callback_data":"h_cola_ver_pendientes"}])
                else:
                    _kb_rows.append([{"text":"↩ Menú","callback_data":"h_cola_main_panel"}])
                keyboard = {"inline_keyboard": _kb_rows}
                try:
                    await query.edit_message_text(
                        card_msg, parse_mode="HTML",
                        reply_markup=keyboard,
                        disable_web_page_preview=True,
                    )
                except Exception:
                    pass
                await query.answer()

        except Exception as _e:
            logger.exception(f"h_cola_ error: {_e}")
            await query.answer("Error interno", show_alert=True)
        return

    # ── Harness — Imagen pendiente ───────────────────────────────────────────
    if query.data.startswith("h_img_"):
        import sys as _sys_img, json as _js_img, sqlite3 as _sq_img
        _sys_img.path.insert(0, "/opt/me-harness")
        try:
            import broker as _br_img
            parts_img = query.data.split(":")
            action_img = parts_img[0]
            job_id_img = int(parts_img[1]) if len(parts_img) > 1 else 0

            if action_img == "h_img_url":
                # Pedir URL o foto directa
                context.user_data["awaiting_img_url_for"]     = job_id_img
                context.user_data["awaiting_img_msg_id"]      = query.message.message_id
                await query.answer()
                await context.bot.send_message(
                    chat_id=query.message.chat_id,
                    text=f"🖼 <b>Imagen para la nota #{job_id_img}</b>\n\nPegá la URL <i>o enviá la foto directamente</i> acá:",
                    parse_mode="HTML",
                    reply_markup={"inline_keyboard": [[
                        {"text": "↩ Volver", "callback_data": f"h_img_cancel:{job_id_img}"}
                    ]]}
                )

            elif action_img == "h_img_cancel":
                await query.answer()
                context.user_data.pop("awaiting_img_url_for", None)
                context.user_data.pop("awaiting_img_msg_id", None)
                try:
                    await query.message.delete()
                except Exception:
                    pass

            elif action_img == "h_img_search":
                # Buscar en media library de WP por keywords de la nota
                await query.answer("🔍 Buscando...")
                job_i = _br_img.get_job(job_id_img)
                cj_i = {}
                try: cj_i = _js_img.loads(job_i.get("content_json") or "{}")
                except Exception: pass
                kws = cj_i.get("matched_kw") or []
                query_str = " ".join(kws[:3]) if kws else (job_i.get("title") or "")[:30]
                import requests as _req_img
                from config import WP_URL as _WP_IMG, WP_USER as _WPU_IMG, WP_PASS as _WPP_IMG
                import base64 as _b64_img
                _tok_img = _b64_img.b64encode(f"{_WPU_IMG}:{_WPP_IMG}".encode()).decode()
                _hdrs_img = {"Authorization": f"Basic {_tok_img}"}
                r_media = _req_img.get(
                    f"{_WP_IMG}/wp-json/wp/v2/media",
                    params={"search": query_str, "per_page": 5, "media_type": "image"},
                    headers=_hdrs_img, timeout=10
                )
                media_items = r_media.json() if r_media.ok else []
                if not media_items:
                    await context.bot.send_message(
                        chat_id=query.message.chat_id,
                        text=f"❌ No encontré imágenes para <i>{query_str[:40]}</i> en ME.ar.\n"
                             f"Probá con una URL propia.",
                        parse_mode="HTML",
                        reply_markup={"inline_keyboard": [[
                            {"text": "🔗 Agregar URL", "callback_data": f"h_img_url:{job_id_img}"}
                        ]]}
                    )
                else:
                    nums = ["1️⃣","2️⃣","3️⃣","4️⃣","5️⃣"]
                    rows_media = []
                    lines = [f"🔍 <b>Imágenes en ME.ar</b> para <i>{query_str[:40]}</i>:\n"]
                    for idx, m in enumerate(media_items[:5]):
                        m_id  = m.get("id")
                        m_url = m.get("source_url") or ""
                        m_ttl = (m.get("title", {}).get("rendered") or m.get("slug") or str(m_id))[:40]
                        n = nums[idx]
                        lines.append(f'{n} <a href="{m_url}">{m_ttl}</a>')
                        rows_media.append([{"text": f"{n} Usar esta",
                                            "callback_data": f"h_img_wpid:{job_id_img}:{m_id}"}])
                    rows_media.append([{"text": "🔗 Agregar URL", "callback_data": f"h_img_url:{job_id_img}"},
                                       {"text": "↩ Cancelar",    "callback_data": f"h_img_cancel:{job_id_img}"}])
                    await context.bot.send_message(
                        chat_id=query.message.chat_id,
                        text="\n".join(lines),
                        parse_mode="HTML",
                        disable_web_page_preview=True,
                        reply_markup={"inline_keyboard": rows_media}
                    )

            elif action_img == "h_img_wpid" and len(parts_img) >= 3:
                # Leo eligió una imagen de la media library por su WP ID
                wp_media_id = int(parts_img[2])
                job_i = _br_img.get_job(job_id_img)
                cj_i = {}
                try: cj_i = _js_img.loads(job_i.get("content_json") or "{}")
                except Exception: pass
                cj_i["image_id_override"] = wp_media_id
                with _sq_img.connect("/opt/me-harness/harness.db") as _c_img:
                    _c_img.execute("UPDATE jobs SET stage='publicacion', content_json=?, updated_at=datetime('now') WHERE id=?",
                                   (_js_img.dumps(cj_i), job_id_img))
                await query.answer("✅ Imagen seleccionada — reintentando publicación", show_alert=False)
                try:
                    await query.edit_message_text(
                        f"🟢 <b>Imagen seleccionada (WP #{wp_media_id})</b>\n"
                        f"Nota #{job_id_img} vuelve a la cola de publicación.",
                        parse_mode="HTML"
                    )
                except Exception:
                    pass

            elif action_img == "h_img_use":
                # Subir la URL validada como featured image y reintentar publicación
                # URL guardada en user_data (callback_data tiene límite 64 bytes)
                import urllib.parse as _up_img
                img_url_to_use = (
                    context.user_data.pop(f"pending_img_url_{job_id_img}", None)
                    or (_up_img.unquote(parts_img[2]) if len(parts_img) >= 3 else None)
                )
                if not img_url_to_use:
                    await query.answer("No se encontró la URL — pegá la imagen de nuevo", show_alert=True)
                    return
                await query.answer("⏳ Subiendo imagen...")
                from agents.publicador import upload_image as _upload_img
                media_id = _upload_img(img_url_to_use)
                if not media_id:
                    try:
                        await query.edit_message_text(
                            f"🔴 <b>No pude subir esa imagen a WP.</b>\n"
                            f"Probá con otra URL:",
                            parse_mode="HTML",
                            reply_markup={"inline_keyboard": [
                                [{"text": "🔗 Otra URL",        "callback_data": f"h_img_url:{job_id_img}"},
                                 {"text": "🔍 Buscar en ME.ar", "callback_data": f"h_img_search:{job_id_img}"}],
                            ]}
                        )
                    except Exception:
                        pass
                else:
                    job_i = _br_img.get_job(job_id_img)
                    cj_i = {}
                    try: cj_i = _js_img.loads(job_i.get("content_json") or "{}")
                    except Exception: pass
                    cj_i["image_id_override"] = media_id
                    with _sq_img.connect("/opt/me-harness/harness.db") as _c_img:
                        _c_img.execute("UPDATE jobs SET stage='publicacion', content_json=?, updated_at=datetime('now') WHERE id=?",
                                       (_js_img.dumps(cj_i), job_id_img))
                    try:
                        await query.edit_message_text(
                            f"🟢 <b>Imagen subida — publicando nota #{job_id_img}</b>",
                            parse_mode="HTML"
                        )
                    except Exception:
                        pass

            elif action_img == "h_img_skip":
                # Publicar sin imagen (Leo lo acepta explícitamente)
                job_i = _br_img.get_job(job_id_img)
                cj_i = {}
                try: cj_i = _js_img.loads(job_i.get("content_json") or "{}")
                except Exception: pass
                cj_i["skip_image"] = True
                with _sq_img.connect("/opt/me-harness/harness.db") as _c_img:
                    _c_img.execute("UPDATE jobs SET stage='publicacion', content_json=?, updated_at=datetime('now') WHERE id=?",
                                   (_js_img.dumps(cj_i), job_id_img))
                await query.answer("⏭ Publicando sin foto")
                try:
                    await query.edit_message_text(f"⏭ Nota #{job_id_img} → publicando sin imagen.", parse_mode="HTML")
                except Exception:
                    pass

        except Exception as _e_img:
            logger.exception(f"h_img_ error: {_e_img}")
            await query.answer("Error interno", show_alert=True)
        return

    # ── Harness — Curador (Fase 2) ───────────────────────────────────────────
    if query.data.startswith("h_cur_"):
        import sys as _sys, sqlite3 as _sq, json as _js
        _sys.path.insert(0, "/opt/me-harness")
        try:
            from agents import curador as _cur
            import broker as _br
            parts  = query.data.split(":")
            action = parts[0]
            arg    = parts[1] if len(parts) >= 2 else None

            _HDB = "/opt/me-harness/harness.db"

            def _load_state(jid):
                with _sq.connect(_HDB) as _c:
                    row = _c.execute("SELECT content_json FROM jobs WHERE id=?", (jid,)).fetchone()
                    return _js.loads(row[0]) if row and row[0] else {}

            def _save_state(jid, state):
                with _sq.connect(_HDB) as _c:
                    _c.execute("UPDATE jobs SET content_json=? WHERE id=?",
                               (_js.dumps(state), jid))

            async def _update_card(jid, state, chat_id):
                """Edita el keyboard de la tarjeta original si tenemos su message_id."""
                card_msg_id = state.get("card_msg_id")
                if card_msg_id:
                    try:
                        new_kb = _cur.build_card_keyboard(jid, state)
                        await context.bot.edit_message_reply_markup(
                            chat_id=chat_id,
                            message_id=card_msg_id,
                            reply_markup=new_kb,
                        )
                    except Exception:
                        pass

            # ── Aprobar ──────────────────────────────────────────────────────
            if action == "h_cur_approve" and arg:
                job_id = int(arg)
                _jb = _br.get_job(job_id)
                if not _jb or _jb.get("stage") != "curado":
                    await query.answer(f"La nota #{job_id} ya no está en curado "
                                       f"({(_jb or {}).get('stage', 'inexistente')}).", show_alert=True)
                    return
                state  = _load_state(job_id)
                # ── Nota manual: el contenido YA es final → publicación DIRECTA (sin redactor,
                # que la reescribiría). Misma ruta de fotos del publicador (img_gen + watermark). ──
                if state.get("es_nota_manual"):
                    await query.message.delete()
                    _txt_nm = await asyncio.to_thread(_publicar_nota_manual_directo, job_id, state)
                    await context.bot.send_message(chat_id=query.message.chat_id, text=_txt_nm)
                    return
                inst   = state.get("instructions", "")
                ok     = _cur.approve(job_id, instructions=inst)
                await query.message.delete()
                if ok:
                    # Asignar sugerencia de cola en background (silencioso)
                    try:
                        from agents import cola as _cola_a
                        import threading as _thr
                        _thr.Thread(target=_cola_a.run_once, daemon=True).start()
                    except Exception:
                        pass
                    # Confirmación simple — el job espera en la cola hasta /coladepublicacion
                    await context.bot.send_message(
                        chat_id=query.message.chat_id,
                        text=f"🗂 Nota #{job_id} → cola"
                             + (f"\n📝 <i>{inst[:80]}</i>" if inst else ""),
                        parse_mode="HTML"
                    )

            # ── Briefing AUTO (Fase 3): box de checklist ──────────────────────
            elif action == "h_cur_auto_tog" and arg is not None:
                # toggle de una casilla: h_cur_auto_tog:{i}:{mask}:{ids}
                try:
                    i    = int(arg)
                    mask = parts[2]
                    ids  = parts[3]
                    ml = list(mask)
                    ml[i] = "0" if ml[i] == "1" else "1"
                    newmask = "".join(ml)
                    n = len(ids.split("-"))
                    await query.edit_message_reply_markup(
                        reply_markup=_cur._checklist_kb(ids, newmask, n))
                except Exception:
                    pass

            elif action == "h_cur_auto_okm" and arg is not None:
                # aprobar marcadas: h_cur_auto_okm:{mask}:{ids}
                mask = arg
                ids  = parts[2] if len(parts) >= 3 else ""
                jids = [x for x in ids.split("-") if x.strip().isdigit()]
                done = 0
                for idx_j, jid in enumerate(jids):
                    if idx_j < len(mask) and mask[idx_j] == "1":
                        try:
                            jb = _br.get_job(int(jid))
                            if jb and jb.get("stage") == "curado" and _cur.approve(int(jid)):
                                done += 1
                        except Exception:
                            pass
                if done:
                    try:
                        from agents import cola as _cola_a
                        import threading as _thr
                        _thr.Thread(target=_cola_a.run_once, daemon=True).start()
                    except Exception:
                        pass
                if done:
                    await _borrar_cards_briefing(context, query.message.chat_id, jids)
                try:
                    if done:
                        await query.edit_message_text(
                            f"✅ <b>{done} nota(s) aprobadas</b> → cola de redacción.",
                            parse_mode="HTML")
                    else:
                        # 0 marcadas: avisar SIN borrar el teclado (si no, Leo no puede marcar)
                        await query.answer("No marcaste ninguna. Tocá ☐ para marcar y reintentá.",
                                           show_alert=True)
                except Exception:
                    pass

            elif action == "h_cur_auto_renew" and arg is not None:
                # proponer: conserva las MARCADAS (☑) y reemplaza las sin marcar.
                # h_cur_auto_renew:{mask}:{ids}
                mask = arg
                ids  = parts[2] if len(parts) >= 3 else ""
                jids = [x for x in ids.split("-") if x.strip().isdigit()]
                keep = [int(jid) for i, jid in enumerate(jids)
                        if i < len(mask) and mask[i] == "1"]
                drop = [int(jid) for i, jid in enumerate(jids)
                        if not (i < len(mask) and mask[i] == "1")]
                _n_cfg = int(_cur.get_briefing_config().get("n", 5))
                needed = max(0, _n_cfg - len(keep))
                if needed == 0:
                    try:
                        # aviso SIN borrar el teclado (el botón que necesita sigue ahí)
                        await query.answer(f"Ya marcaste las {_n_cfg} — tocá «Aprobar marcadas».",
                                           show_alert=True)
                    except Exception:
                        pass
                else:
                    for jid in drop:
                        try:
                            jb = _br.get_job(jid)
                            if jb and jb.get("stage") == "curado":
                                _br.update_stage(jid, "rejected")
                        except Exception:
                            pass
                    try:
                        await query.edit_message_text(
                            f"🔄 Conservo {len(keep)}, busco {needed} para completar el trío…")
                    except Exception:
                        pass
                    try:
                        import threading as _thr
                        _keep = keep or None
                        _thr.Thread(target=lambda: _cur.run_briefing_auto(None, _keep), daemon=True).start()
                    except Exception:
                        pass

            elif action == "h_cur_auto_cancel":
                _jids_c = [x for x in (arg or "").split("-") if x.strip().isdigit()]
                await _borrar_cards_briefing(context, query.message.chat_id, _jids_c)
                try:
                    await query.edit_message_text("✖️ Briefing cancelado. Las dejo en la cola.")
                except Exception:
                    pass

            # ── ⚙️ Ciclaje del briefing auto (config editable desde TG) ────
            elif action == "h_cur_auto_cfg":
                await context.bot.send_message(
                    chat_id=query.message.chat_id,
                    text=_cur.ciclaje_texto(), parse_mode="HTML",
                    reply_markup=_cur.ciclaje_kb())

            elif action == "h_cur_auto_cfg_noop":
                await query.answer("Elegí 3, 5 u 8 →")

            elif action == "h_cur_auto_cfg_n" and arg:
                _cur.save_briefing_config({"n": int(arg)})
                await query.edit_message_text(_cur.ciclaje_texto(), parse_mode="HTML",
                                              reply_markup=_cur.ciclaje_kb())

            elif action == "h_cur_auto_cfg_pause":
                _cfgp = _cur.get_briefing_config()
                _cur.save_briefing_config({"pausado": not _cfgp.get("pausado")})
                await query.edit_message_text(_cur.ciclaje_texto(), parse_mode="HTML",
                                              reply_markup=_cur.ciclaje_kb())

            elif action == "h_cur_auto_cfg_hor":
                context.user_data["awaiting_ciclaje_horarios"] = True
                await context.bot.send_message(
                    chat_id=query.message.chat_id,
                    text=("🕘 Pasame los HORARIOS de corrida separados por coma (hora ARG).\n"
                          "Ej: <code>08:00, 13:00, 19:00</code>"), parse_mode="HTML")
                await query.answer("Esperando horarios…")

            elif action == "h_cur_auto_cfg_n_ask":
                context.user_data.pop("awaiting_ciclaje_horarios", None)
                context.user_data["awaiting_ciclaje_n"] = True
                await context.bot.send_message(
                    chat_id=query.message.chat_id,
                    text="🔢 Cuantas notas por corrida? Pasame un numero (ej: <code>6</code>).",
                    parse_mode="HTML")
                await query.answer("Esperando el numero...")

            elif action == "h_cur_auto_cfg_cancel":
                try:
                    await query.message.delete()
                except Exception:
                    pass
                await query.answer("Menu cerrado")

            elif action == "h_cur_auto_cfg_run":
                await query.answer("Corriendo briefing…")
                import threading as _thrc
                _thrc.Thread(target=_cur.run_briefing_auto, daemon=True).start()

            # ── Programar: override del destino desde el briefing ─────────
            elif action == "h_cur_program" and arg:
                job_id = int(arg)
                _dest_rows = [
                    [{"text": "📌 Hoy portada",  "callback_data": f"h_cur_setdest:{job_id}:hoy_portada"},
                     {"text": "📰 Hoy",          "callback_data": f"h_cur_setdest:{job_id}:hoy_normal"}],
                    [{"text": "📊 Mejor horario","callback_data": f"h_cur_setdest:{job_id}:mejor_horario"},
                     {"text": "📅 Finde",         "callback_data": f"h_cur_setdest:{job_id}:finde"}],
                    [{"text": "🗓 Fecha específica","callback_data": f"h_cur_setdest:{job_id}:fecha"}],
                    [{"text": "🤖 Que decida el agente","callback_data": f"h_cur_setdest:{job_id}:auto"}],
                    [{"text": "↩ Volver",        "callback_data": f"h_cur_setdest:{job_id}:back"}],
                ]
                try:
                    await query.edit_message_reply_markup(reply_markup={"inline_keyboard": _dest_rows})
                except Exception:
                    pass
                await query.answer()

            # ── Título exacto desde el briefing (lo fija Leo y se bloquea) ─
            elif action == "h_cur_title" and arg:
                job_id = int(arg)
                context.user_data["awaiting_brief_title_for"] = job_id
                try:
                    await context.bot.send_message(
                        chat_id=query.message.chat_id,
                        text=(f"✏️ <b>Título — nota #{job_id}</b>\n\n"
                              f"Escribí el título exacto. Se usa tal cual "
                              f"(el redactor no lo cambia)."),
                        parse_mode="HTML")
                except Exception:
                    pass
                await query.answer("Escribí el título…")

            elif action == "h_cur_setdest" and len(parts) >= 3:
                job_id = int(parts[1]); dest = parts[2]
                import sys as _sys_sd
                _sys_sd.path.insert(0, "/opt/me-harness")
                from agents import curador as _cur
                _DEST_SHORT = {"hoy_portada": "📌 Hoy portada", "hoy_normal": "📰 Hoy",
                               "mejor_horario": "📊 Mejor horario", "finde": "📅 Finde"}
                state = _load_state(job_id)
                if dest == "fecha":
                    context.user_data["awaiting_brief_prog_for"] = job_id
                    await context.bot.send_message(
                        chat_id=query.message.chat_id,
                        text=(f"🗓 <b>Programar — nota #{job_id}</b>\n\n"
                              f"Escribí la fecha y hora:\n"
                              f"<code>30/05 18:30</code>  ·  <code>mañana 10:00</code>"),
                        parse_mode="HTML")
                    await query.answer("Esperando fecha…")
                else:
                    _msg = ""
                    if dest == "auto":
                        state.pop("pub_dest_override", None)
                        state.pop("pub_date_override", None)
                        _save_state(job_id, state)
                        _msg = "🤖 Decide el agente"
                    elif dest != "back":
                        state["pub_dest_override"] = dest
                        state.pop("pub_date_override", None)
                        _save_state(job_id, state)
                        _msg = f"✅ {_DEST_SHORT.get(dest, dest)}"
                    await query.answer(_msg) if _msg else await query.answer()
                # Redibujar la tarjeta (el botón Programar muestra el override)
                try:
                    await query.edit_message_reply_markup(
                        reply_markup=_cur.build_card_keyboard(job_id, _load_state(job_id)))
                except Exception:
                    pass

            # ── Consolidar fuentes similares en una sola nota ────────────
            elif action == "h_cur_consolidar" and arg:
                job_id      = int(arg)
                state       = _load_state(job_id)
                similar_ids = state.get("similar_job_ids", [])
                # Recopilar URLs de fuentes secundarias
                multi_urls = []
                with _sq.connect(_HDB) as _c:
                    for sid in similar_ids:
                        row = _c.execute(
                            "SELECT source_url FROM jobs WHERE id=?", (sid,)
                        ).fetchone()
                        if row and row[0]:
                            multi_urls.append(row[0])
                    # Rechazar los jobs secundarios para que no reaparezcan
                    if similar_ids:
                        _c.execute(
                            f"UPDATE jobs SET stage='rejected', updated_at=datetime('now') "
                            f"WHERE id IN ({','.join('?'*len(similar_ids))})",
                            similar_ids
                        )
                # Guardar URLs multi-fuente en el job primario.
                # ⚠️ Se MERGEA, no se pisa: el agente `ecos` ya pudo haber adjuntado fuentes
                # solo (pre_briefing 07:15/11:15/17:15) y un '=' las borraba (fix 2026-07-21).
                _ya = state.get("multi_source_urls") or []
                state["multi_source_urls"] = _ya + [u for u in multi_urls if u not in _ya]
                _save_state(job_id, state)
                # Aprobar el job primario → va a cola
                inst = state.get("instructions", "")
                _cur.approve(job_id, instructions=inst)
                await query.message.delete()
                await context.bot.send_message(
                    chat_id=query.message.chat_id,
                    text=(
                        f"🔀 <b>Consolidando {len(multi_urls)+1} fuentes</b> → cola\n"
                        f"<i>El redactor combinará todas las fuentes en una sola nota.</i>"
                    ),
                    parse_mode="HTML"
                )

            # ── Actualizar una nota YA publicada en vez de publicar otra ──────────
            # El curador detectó que esta noticia continúa algo que publicamos hace 24-48h
            # (agents/nutrir.py). Le suma un bloque de ACTUALIZACIÓN a la nota viva: NO la
            # reescribe, así conserva URL, fecha, SEO, comentarios y ranking.
            elif action == "h_cur_nutrir" and arg:
                job_id = int(arg)
                state  = _load_state(job_id)
                cand   = state.get("nutre")
                if not cand:
                    await query.answer("Ya no tengo la nota candidata", show_alert=True)
                else:
                    await query.answer("Actualizando la nota…")
                    import sys as _sys
                    _sys.path.insert(0, "/opt/me-harness")
                    from agents import nutrir as _nut
                    import broker as _br_n

                    def _do():
                        return _nut.nutrir(_br_n.get_job(job_id), cand, auto=False)

                    res = await asyncio.get_event_loop().run_in_executor(None, _do)
                    if res.get("ok") and res.get("dry"):
                        txt = ("🧪 <b>Modo shadow</b> — no toqué WordPress.\n"
                               "Te mandé aparte cómo habría quedado. Para activarlo de verdad: "
                               "<code>touch /opt/me-harness/nutrir_live.flag</code>")
                    elif res.get("ok"):
                        txt = (f"🔄 <b>Nota actualizada</b>\n"
                               f"<a href=\"{res['wp_url']}\">{cand['titulo'][:70]}</a>\n\n"
                               f"<b>Lo que sumé:</b> {res['parrafo'][:400]}")
                        await query.message.delete()
                    else:
                        _motivos = {
                            "no_aporta": "la noticia nueva no agrega ningún dato que la nota no tenga",
                            "max_bloques": "esa nota ya tiene 3 actualizaciones (no es un feed)",
                            "ya_actualizada_hoy": "ya la actualicé hoy",
                            "fuente_repetida": "esa misma fuente ya se usó para actualizarla",
                            "sin_dato_duro": "el párrafo no traía ninguna cifra",
                            "cifra_inventada": "el párrafo traía una cifra que no está en la fuente",
                            "es_living_note": "es una living note (se actualiza por su propio flujo)",
                            "invariante": "el guard de no-destrucción falló — no toqué nada",
                        }
                        _g = res.get("gate")
                        txt = (f"⛔ No la actualicé: {_motivos.get(_g, _g)}."
                               f"{chr(10) + res['motivo'][:200] if res.get('motivo') else ''}\n\n"
                               f"<i>La propuesta sigue en pie si querés publicarla como nota aparte.</i>")
                    await context.bot.send_message(chat_id=query.message.chat_id, text=txt,
                                                   parse_mode="HTML",
                                                   disable_web_page_preview=True)

            # ── Like toggle (👍/☑️ Es tema ME.ar) ────────────────────
            elif action == "h_cur_like" and arg:
                job_id = int(arg)
                state  = _load_state(job_id)
                state["liked"] = True if state.get("liked") is not True else None
                _save_state(job_id, state)
                keyboard = _cur.build_card_keyboard(job_id, state)
                await query.edit_message_reply_markup(reply_markup=keyboard)
                await query.answer(
                    "☑️ Es tema ME.ar" if state.get("liked") is True else "Desmarcado",
                    show_alert=False
                )

            # ── Dislike toggle (👎/☑️ No es nuestro) ───────────────
            elif action == "h_cur_dislike" and arg:
                job_id = int(arg)
                state  = _load_state(job_id)
                state["liked"] = False if state.get("liked") is not False else None
                _save_state(job_id, state)
                keyboard = _cur.build_card_keyboard(job_id, state)
                await query.edit_message_reply_markup(reply_markup=keyboard)
                await query.answer(
                    "☑️ No es nuestro" if state.get("liked") is False else "Desmarcado",
                    show_alert=False
                )

            # ── Rechazar — guarda aprendizaje y saca la tarjeta ──────────────
            # -- Portada toggle --------------------------------------------------
            elif action == "h_cur_portada" and arg:
                job_id = int(arg)
                state = _load_state(job_id)
                state["portada"] = not state.get("portada", False)
                _save_state(job_id, state)
                await _update_card(job_id, state, query.message.chat_id)
                label = "Portada activada" if state["portada"] else "Portada desactivada"
                await query.answer(label, show_alert=False)

            elif action == "h_cur_formato" and arg:
                job_id = int(arg)
                state = _load_state(job_id)
                _cf = state.get("formato") or ("desplegable" if state.get("hilo") == 1 else "continua")
                state["formato"] = "continua" if _cf == "desplegable" else "desplegable"
                _save_state(job_id, state)
                await _update_card(job_id, state, query.message.chat_id)
                await query.answer(f"Formato: {state['formato']}", show_alert=False)

            elif action == "h_cur_nopub" and arg:
                job_id = int(arg)
                reasons = [
                    ("✅ Ya publicada en ME",        "ya_publicada"),
                    ("📋 Nota repetida",             "repetida"),
                    ("😐 Nota de poco interés",      "poco_interes"),
                    ("🚫 No es tema del diario",     "offtopic"),
                    ("🎭 Contenido no confiable",    "manipulado"),
                    ("🔍 Fuente no confiable",       "fuente"),
                    ("🌐 Tema periférico",            "periferico"),
                ]
                rows = [[{"text": r[0], "callback_data": f"h_cur_reject_why:{job_id}:{r[1]}"}]
                        for r in reasons]
                rows.append([{"text": "📢 Publinota",
                              "callback_data": f"h_cur_publinota:{job_id}"}])
                rows.append([{"text": "↩ Volver", "callback_data": f"h_cur_volver:{job_id}"}])
                try:
                    await query.edit_message_text(
                        f"🚫 <b>¿Qué hacemos con esta nota?</b> — job #{job_id}",
                        parse_mode="HTML",
                        reply_markup={"inline_keyboard": rows},
                    )
                except Exception:
                    pass

            elif action == "h_cur_living" and arg:
                job_id = int(parts[1])
                # Picker en 2 niveles (21/8, pedido de Leo): primero GRUPOS temáticos, después
                # las notas del grupo paginadas. Antes: lista plana con tope [:32] que escondía
                # las últimas entradas (42 candidatas: las cotizaciones quedaban casi todas afuera).
                _grp_lv = parts[2] if len(parts) >= 3 else ""
                _pag_lv = int(parts[3]) if len(parts) >= 4 else 0
                try:
                    def _grupo_ln(_tema):
                        _t = _tema.lower()
                        # GUÍAS de trámite (cómo hacer algo) ANTES que ARCA: si no, caen
                        # todas en "impuestos" mezcladas con las fichas de dato (categorías,
                        # calendarios). Pedido de Leo 21/8 al sumar la guía del VEP.
                        if any(k in _t for k in ("cómo", "como emitir", "paso a paso",
                                                 "constancia", "clave fiscal", "factura electr",
                                                 "vep", "recategor", "plan de pagos",
                                                 "obra social", "trámite", "tramite")):
                            return "gui"
                        if any(k in _t for k in ("escala", "salari", "sueldo", "recibo", "smvm")):
                            return "esc"
                        if any(k in _t for k in ("monotribut", "autónom", "autonom", "arca", "afip",
                                                 "ganancias", "clave fiscal", "factura", "recategor",
                                                 "plan de pagos", "obra social", "vencimiento")):
                            return "arca"
                        if any(k in _t for k in ("plazo fijo", "crédito", "credito", "préstamo",
                                                 "prestamo", "cheque", "tasa")):
                            return "fin"
                        return "otr"
                    # Living notes de DATO (id numérico) + COTIZACIÓN de living_topics
                    # (sentinel cot_<asset>, sin ':' porque el callback_data se parsea con split(':')).
                    _grupos_lv = {"esc": [], "arca": [], "gui": [], "fin": [], "cot": [], "otr": []}
                    _seen_lv = set()
                    for _ln in _br.get_living_notes():
                        _wp = _ln.get("wp_post_id")
                        if not _wp or _wp in _seen_lv:
                            continue
                        _seen_lv.add(_wp)
                        _grupos_lv[_grupo_ln(_ln["tema"])].append((_ln["id"], f"📄 {_ln['tema'][:44]}"))
                    for _tp in _br.get_living_topics():
                        if not _tp.get("wp_post_id"):
                            continue
                        _grupos_lv["cot"].append((f"cot_{_tp['asset']}", f"📈 {_tp['nombre'][:40]}"))
                    _META_LV = [("esc", "💼 Escalas salariales"), ("arca", "🧾 ARCA e impuestos"),
                                ("gui", "📘 Guías y trámites"), ("fin", "💳 Finanzas y créditos"),
                                ("cot", "📈 Cotizaciones"), ("otr", "🗂 Otras")]
                    if not _grp_lv or _grp_lv not in _grupos_lv:
                        # Nivel 1: menú de grupos con conteo
                        rows = [[{"text": f"{_lbl} ({len(_grupos_lv[_k])})",
                                  "callback_data": f"h_cur_living:{job_id}:{_k}:0"}]
                                for _k, _lbl in _META_LV if _grupos_lv[_k]]
                    else:
                        # Nivel 2: notas del grupo, paginadas
                        _opts_lv = _grupos_lv[_grp_lv]
                        _PSZ_LV = 14
                        _tot_lv = max(1, (len(_opts_lv) + _PSZ_LV - 1) // _PSZ_LV)
                        _pag_lv = max(0, min(_pag_lv, _tot_lv - 1))
                        rows = [[{"text": _t, "callback_data": f"h_cur_setliving:{job_id}:{_lid}"}]
                                for _lid, _t in _opts_lv[_pag_lv * _PSZ_LV:(_pag_lv + 1) * _PSZ_LV]]
                        if _tot_lv > 1:
                            _nav = []
                            if _pag_lv > 0:
                                _nav.append({"text": "◀", "callback_data": f"h_cur_living:{job_id}:{_grp_lv}:{_pag_lv - 1}"})
                            _nav.append({"text": f"{_pag_lv + 1}/{_tot_lv}", "callback_data": f"h_cur_living:{job_id}:{_grp_lv}:{_pag_lv}"})
                            if _pag_lv < _tot_lv - 1:
                                _nav.append({"text": "▶", "callback_data": f"h_cur_living:{job_id}:{_grp_lv}:{_pag_lv + 1}"})
                            rows.append(_nav)
                        rows.append([{"text": "↩ Grupos", "callback_data": f"h_cur_living:{job_id}"}])
                    rows.append([{"text": "➕ Proponer nueva living note (mandá un link)", "callback_data": f"h_cur_propln:{job_id}"}])
                    rows.append([{"text": "↩ Volver", "callback_data": f"h_cur_volver:{job_id}"}])
                    await query.edit_message_text(
                        f"📌 <b>¿A qué living note corresponde esta noticia?</b> — job #{job_id}\n"
                        f"<i>📄 DATO: el agente ratifica con 2 fuentes y corrige la ficha. "
                        f"📈 Cotización: va a la cola de nutrición (causas/dato, ≤15 min).</i>",
                        parse_mode="HTML", reply_markup={"inline_keyboard": rows})
                except Exception as _e_lv:
                    await query.answer(f"Error: {_e_lv}", show_alert=True)

            elif action == "h_cur_propln" and arg:
                job_id = int(arg)
                import json as _jpl, os as _opl, datetime as _dpl
                _jb = _br.get_job(job_id) or {}
                try:
                    _cjp = _jpl.loads(_jb.get("content_json") or "{}")
                except Exception:
                    _cjp = {}
                _linkp = _jb.get("source_url") or _cjp.get("source") or _cjp.get("source_url") or ""
                _titp = _cjp.get("title") or _jb.get("title") or ""
                _PROP = "/opt/me-harness/living_notes_propuestas.json"
                try:
                    _lst = _jpl.load(open(_PROP)) if _opl.path.exists(_PROP) else []
                except Exception:
                    _lst = []
                _lst.append({"link": _linkp, "titulo": _titp, "from_job": job_id,
                             "fecha": _dpl.date.today().isoformat()})
                try:
                    open(_PROP, "w").write(_jpl.dumps(_lst, ensure_ascii=False, indent=2))
                except Exception:
                    pass
                await query.answer("➕ Propuesta guardada")
                try:
                    await query.message.delete()   # borra el menú del curador
                except Exception:
                    pass
                await context.bot.send_message(
                    chat_id=query.message.chat_id,
                    text=("➕ <b>Living note propuesta guardada</b> (" + str(len(_lst)) + " en cola)"
                          + chr(10) + "📄 " + _titp[:70] + chr(10) + "🔗 " + _linkp
                          + chr(10) + chr(10) + "La ejecutamos cuando la conversemos."),
                    parse_mode="HTML")

            elif action == "h_cur_setliving" and len(parts) >= 3:
                job_id = int(parts[1]); _dest_raw = parts[2]
                # aprendizaje del ruteo (13/8): toda elección manual queda como ejemplo
                def _registrar_ejemplo(_jid, _destino):
                    try:
                        _br.add_ruteo_ejemplo_desde_job(_jid, _destino)
                    except Exception as _e_re:
                        logger.debug(f"ruteo_ejemplo: {_e_re}")
                if _dest_raw.startswith("cot_"):
                    # COTIZACIÓN (living_topics): va por la cola nutrir — mismo circuito que el
                    # desvío automático (process_nutrir la procesa en ≤15 min).
                    _asset = _dest_raw[4:]
                    state = _load_state(job_id); state["living_note_id"] = f"cot:{_asset}"
                    _save_state(job_id, state)
                    _br.update_stage(job_id, "nutrir")
                    await asyncio.to_thread(_registrar_ejemplo, job_id, f"cot:{_asset}")
                    _nom = next((t["nombre"] for t in _br.get_living_topics()
                                 if t.get("asset") == _asset), _asset)
                    await query.edit_message_text(
                        f"🍃 <b>Enviada a nutrición</b> → {_nom} — job #{job_id}\n"
                        f"<i>Se procesa en ≤15 min (causas/dato). Registré tu elección para que el "
                        f"router aprenda. Vela en /nutricion.</i>", parse_mode="HTML")
                    return
                ln_id = int(_dest_raw)
                state = _load_state(job_id); state["living_note_id"] = ln_id; _save_state(job_id, state)
                await asyncio.to_thread(_registrar_ejemplo, job_id, str(ln_id))
                _fs_txt = ""
                try:
                    from agents import living_update as _lu0
                    _lnx = next((x for x in _br.get_living_notes() if x["id"] == ln_id), None)
                    _fs = _lu0.fuentes_de_ln(_lnx) if _lnx else []
                    if _fs:
                        _fs_txt = "\n\n📋 <b>Fuentes calificadas:</b> " + "; ".join(_fs)
                except Exception:
                    pass
                await query.answer("🔎 Analizando la novedad…", show_alert=False)
                try:
                    await query.edit_message_text(
                        f"🔎 <b>Evaluando la novedad…</b> — job #{job_id}\n"
                        f"<i>Busco 2 fuentes que ratifiquen el dato y, si se confirma, corrijo la ficha solo. "
                        f"Te aviso el resultado.</i>{_fs_txt}", parse_mode="HTML")
                except Exception:
                    pass
                _chat_lv = query.message.chat_id
                async def _run_lv(_lid=ln_id, _jid=job_id, _ch=_chat_lv):
                    try:
                        import sys as _s2; _s2.path.insert(0, "/opt/me-harness")
                        from agents import living_update as _lu
                        await asyncio.wait_for(
                            asyncio.to_thread(_lu.evaluar_y_corregir, _lid, _jid), timeout=280)
                    except Exception as _e2:
                        try:
                            await context.bot.send_message(_ch, f"⚠️ Living Note: error evaluando #{_jid}: {_e2}")
                        except Exception:
                            pass
                asyncio.create_task(_run_lv())

            elif action == "h_cur_rebrief" and arg:
                job_id = int(arg)
                await query.answer("📰 Reenviando el briefing de esa nota…", show_alert=False)
                try:
                    await asyncio.wait_for(asyncio.to_thread(_cur.run_briefing_single, job_id), timeout=60)
                except Exception as _e_rb:
                    try:
                        await context.bot.send_message(query.message.chat_id,
                                                       f"⚠️ No pude reenviar el briefing #{job_id}: {_e_rb}")
                    except Exception:
                        pass

            elif action == "h_cur_publinota" and arg:
                job_id = int(arg)
                try:
                    import sys as _sys_pn
                    _sys_pn.path.insert(0, "/opt/me-harness")
                    import broker as _br_pn
                    _job_pn = _br_pn.get_job(job_id)
                    _url_pn   = (_job_pn.get("source_url") or "") if _job_pn else ""
                    _title_pn = (_job_pn.get("title") or "")      if _job_pn else ""
                    _br_pn.add_publinota(job_id, _url_pn, _title_pn)
                    _br_pn.update_stage(job_id, "publinota")
                    await query.answer("📢 Guardada como publinota", show_alert=False)
                    await query.edit_message_text(
                        f"📢 <b>Publinota registrada</b> — job #{job_id}\n"
                        f"<i>{_title_pn[:120]}</i>\n"
                        f"Usá /publinotas para ver el listado.",
                        parse_mode="HTML",
                    )
                except Exception as _e_pn:
                    await query.answer(f"Error: {_e_pn}", show_alert=True)

            elif action == "h_cur_reject_why" and len(parts) >= 3:
                job_id = int(parts[1])
                reason = parts[2]
                try:
                    import sys as _sys_rj
                    _sys_rj.path.insert(0, "/opt/me-harness")
                    import broker as _br_rj
                    import sqlite3 as _sq_rj, json as _js_rj

                    # Obtener dominio y título del job
                    _job_rj = _br_rj.get_job(job_id)
                    _domain_rj = ""
                    _title_rj  = ""
                    if _job_rj:
                        from agents.curador import _extract_domain as _exd_rj
                        _domain_rj = _exd_rj(_job_rj.get("source_url", ""))
                        _title_rj  = _job_rj.get("title", "")

                    def _cascade_reject(jid):
                        """Rechaza el job y todos sus similares en curado."""
                        _br_rj.update_stage(jid, "rejected")
                        try:
                            with _sq_rj.connect(_HDB) as _cc:
                                _row = _cc.execute("SELECT content_json FROM jobs WHERE id=?", (jid,)).fetchone()
                                if _row and _row[0]:
                                    for sid in _js_rj.loads(_row[0]).get("similar_job_ids", []):
                                        _br_rj.update_stage(sid, "rejected")
                        except Exception:
                            pass

                    if reason == "ya_publicada":
                        # Tema YA publicado en ME: señal fuerte de dedup. Cascade reject del
                        # grupo de similares + feedback para el aprendizaje de temas
                        # (consolidar/actualizar/descartar — ver plan dedup). El título queda
                        # registrado para que el dedup frene futuras notas del mismo tema.
                        _cascade_reject(job_id)
                        _br_rj.record_feedback(job_id, "curador", "reject_ya_publicada",
                                               after={"reason": "ya_publicada", "title": _title_rj})
                    elif reason == "repetida":
                        # Sin penalización. Cascade reject grupo.
                        _cascade_reject(job_id)
                        _br_rj.record_feedback(job_id, "curador", "reject_repetida",
                                               after={"reason": "repetida"})

                    elif reason == "poco_interes":
                        # Dominio leve (-0.1), keywords moderado (-0.5/kw)
                        _br_rj.update_stage(job_id, "rejected")
                        _br_rj.update_domain_weight(_domain_rj, -0.1)
                        _br_rj.update_keywords_for_title(_title_rj, -1.0)
                        _br_rj.record_feedback(job_id, "curador", "reject_poco_interes",
                                               after={"reason": "poco_interes", "domain": _domain_rj})

                    elif reason == "offtopic":
                        # Sin penalizar dominio. Keywords muy fuerte (-2.0/kw). Cascade.
                        _cascade_reject(job_id)
                        _br_rj.update_keywords_for_title(_title_rj, -4.0)
                        _br_rj.record_feedback(job_id, "curador", "reject_offtopic",
                                               after={"reason": "offtopic"})

                    elif reason == "manipulado":
                        # Penalización media en dominio (-0.5). Sin tocar keywords.
                        _br_rj.update_stage(job_id, "rejected")
                        _br_rj.update_domain_weight(_domain_rj, -0.5)
                        _br_rj.record_feedback(job_id, "curador", "reject_manipulado",
                                               after={"reason": "manipulado", "domain": _domain_rj})

                    elif reason == "fuente":
                        # Penalización fuerte en dominio (-1.5). Sin tocar keywords.
                        _br_rj.update_stage(job_id, "rejected")
                        _br_rj.update_domain_weight(_domain_rj, -1.5)
                        _br_rj.record_feedback(job_id, "curador", "reject_fuente",
                                               after={"reason": "fuente", "domain": _domain_rj})

                    elif reason == "periferico":
                        # Dominio leve (-0.1), keywords leve (-0.3/kw)
                        _br_rj.update_stage(job_id, "rejected")
                        _br_rj.update_domain_weight(_domain_rj, -0.1)
                        _br_rj.update_keywords_for_title(_title_rj, -0.6)
                        _br_rj.record_feedback(job_id, "curador", "reject_periferico",
                                               after={"reason": "periferico", "domain": _domain_rj})

                    else:
                        # Fallback genérico
                        _cur.reject(job_id)
                        _br.record_feedback(job_id, "curador", f"reject_{reason}",
                                            after={"reason": reason})

                except Exception as _e_rj:
                    log.warning(f"reject_why error: {_e_rj}")
                    try:
                        _cur.reject(job_id)
                    except Exception:
                        pass
                try:
                    await query.message.delete()
                except Exception:
                    pass
                reason_labels = {
                    "repetida":      "nota repetida",
                    "poco_interes":  "nota de poco interés",
                    "offtopic":      "no es tema del diario",
                    "manipulado":    "contenido no confiable (operación/publinota)",
                    "fuente":        "fuente no confiable",
                    "periferico":    "tema periférico",
                }
                label = reason_labels.get(reason, reason)
                try:
                    await context.bot.send_message(
                        chat_id=query.message.chat_id,
                        text=f"🚫 Job #{job_id} rechazado — {label}",
                    )
                except Exception:
                    pass

            elif action == "h_cur_reject" and arg:
                job_id = int(arg)
                _cur.reject(job_id)
                await query.message.delete()

            # ── CAPA toggle ──────────────────────────────────────────────────
            elif action == "h_cur_capa" and len(parts) >= 3:
                job_id  = int(parts[1])
                capa_id = int(parts[2])
                state   = _load_state(job_id)
                if state.get("hilo") == capa_id:
                    state["hilo"] = None
                    with _sq.connect(_HDB) as _c:
                        _c.execute("UPDATE jobs SET hilo=NULL WHERE id=?", (job_id,))
                else:
                    state["hilo"] = capa_id
                    with _sq.connect(_HDB) as _c:
                        _c.execute("UPDATE jobs SET hilo=? WHERE id=?", (capa_id, job_id))
                _save_state(job_id, state)
                keyboard = _cur.build_card_keyboard(job_id, state)
                await query.edit_message_reply_markup(reply_markup=keyboard)
                _capa_names = {1: "Informarse es respetarse", 2: "Mundo empresarial", 3: "La Voz de las pymes"}
                ans = (f"✅ CAPA: {_capa_names[capa_id]}" if state.get("hilo")
                       else "Capa deseleccionada")
                await query.answer(ans, show_alert=False)

            # Watermark toggle (marca_agua) por nota del briefing
            elif action == "h_cur_wm" and len(parts) >= 2:
                job_id = int(parts[1])
                state  = _load_state(job_id)
                state["sin_watermark"] = not state.get("sin_watermark", False)
                _save_state(job_id, state)
                await query.edit_message_reply_markup(reply_markup=_cur.build_card_keyboard(job_id, state))
                await query.answer("Sin marca de agua" if state["sin_watermark"] else "Con marca de agua", show_alert=False)

            # Cambiar la foto de la nota del briefing (con watermark, sin tocar el stage)
            elif action == "h_cur_foto" and len(parts) >= 2:
                job_id = int(parts[1])
                context.user_data["awaiting_wm_foto_for"] = job_id
                await query.answer()
                await context.bot.send_message(
                    chat_id=query.message.chat_id,
                    text=f"📷 Foto para la nota #{job_id}. Pegá la URL o enviá la foto directamente. Sale con la marca de agua salvo que la hayas apagado con 🏷.")

            # ── Sub-menú categorías (con preseleción marcada) ───────────────────
            # -- Sub-menu categorias multi-select ----------------------------------
            elif action == "h_cur_cats" and arg:
                job_id = int(arg)
                state = _load_state(job_id)
                # Migrar formato viejo (single -> multi)
                if "category_id" in state and "category_ids" not in state:
                    state["category_ids"] = [state["category_id"]]
                sel_ids = state.get("category_ids", [])
                _CAT_NAMES_LOCAL = {
                    94: "Economia", 87: "Politica", 96: "Empresas", 97: "Internacional",
                    100: "Gobierno", 95: "AFIP/ARCA", 90: "Industria", 91: "Opinion",
                    89: "Comercio", 88: "Agro", 103: "Informes", 102: "Provincias",
                    93: "Sindicatos", 92: "Servicios", 239: "Digital pymes", 1139: "Mundo del vino",
                    101: "Poder Judicial", 99: "Congreso", 98: "Nacional",
                    104: "ME TV", 1048: "Coberturas",
                }
                rows = []
                cat_items = list(_CAT_NAMES_LOCAL.items())
                for i in range(0, len(cat_items), 2):
                    row = []
                    for cat_id, cat_name in cat_items[i:i+2]:
                        prefix = "☑️ " if cat_id in sel_ids else ""
                        row.append({"text": prefix+cat_name, "callback_data": f"h_cur_setcat:{job_id}:{cat_id}"})
                    rows.append(row)
                rows.append([{"text": "↩ Volver", "callback_data": f"h_cur_volver:{job_id}"}])
                kb = {"inline_keyboard": rows}
                await query.edit_message_text(
                    f"🗂 <b>Categorias</b> (job #{job_id}) — toca para seleccionar/deseleccionar:",
                    parse_mode="HTML",
                    reply_markup=kb,
                )

            # -- Setear/deseleccionar categoria (multi-select) --------------------
            elif action == "h_cur_setcat" and len(parts) >= 3:
                job_id = int(parts[1])
                cat_id = int(parts[2])
                state  = _load_state(job_id)
                if "category_id" in state and "category_ids" not in state:
                    state["category_ids"] = [state["category_id"]]
                cat_ids = state.get("category_ids", [])
                if cat_id in cat_ids:
                    cat_ids.remove(cat_id)
                    label = "Categoria removida"
                else:
                    cat_ids.append(cat_id)
                    label = "Categoria agregada"
                state["category_ids"] = cat_ids
                _save_state(job_id, state)
                # Actualizar el sub-menu de categorias (mantener abierto)
                _CAT_NAMES_LOCAL = {
                    94: "Economia", 87: "Politica", 96: "Empresas", 97: "Internacional",
                    100: "Gobierno", 95: "AFIP/ARCA", 90: "Industria", 91: "Opinion",
                    89: "Comercio", 88: "Agro", 103: "Informes", 102: "Provincias",
                    93: "Sindicatos", 92: "Servicios", 239: "Digital pymes", 1139: "Mundo del vino",
                    101: "Poder Judicial", 99: "Congreso", 98: "Nacional",
                    104: "ME TV", 1048: "Coberturas",
                }
                rows = []
                cat_items = list(_CAT_NAMES_LOCAL.items())
                for i in range(0, len(cat_items), 2):
                    row = []
                    for cid, cname in cat_items[i:i+2]:
                        prefix = "☑️ " if cid in cat_ids else ""
                        row.append({"text": prefix+cname, "callback_data": f"h_cur_setcat:{job_id}:{cid}"})
                    rows.append(row)
                rows.append([{"text": "↩ Volver", "callback_data": f"h_cur_volver:{job_id}"}])
                kb = {"inline_keyboard": rows}
                await query.edit_message_reply_markup(reply_markup=kb)
                await _update_card(job_id, state, query.message.chat_id)
                await query.answer(label, show_alert=False)

            elif action == "h_cur_tags" and arg:
                job_id = int(arg)
                state  = _load_state(job_id)
                context.user_data["awaiting_tags_for"] = job_id
                current_tags = state.get("tags", [])
                current_text = (f"\nActuales: <code>{', '.join(current_tags)}</code>"
                                if current_tags else "")
                await context.bot.send_message(
                    chat_id=query.message.chat_id,
                    text=(
                        f"🏷 <b>Etiquetas para nota #{job_id}</b>{current_text}\n\n"
                        f"Escribí las etiquetas separadas por coma:\n"
                        f"<code>pymes, monotributo, ARCA, exportaciones</code>"
                    ),
                    parse_mode="HTML",
                    reply_markup={"inline_keyboard": [[
                        {"text": "↩ Volver", "callback_data": "h_cur_volver"}
                    ]]}
                )

            # ── Instrucción — muestra prompt con instrucción actual y botón Volver ─
            elif action == "h_cur_instruct" and arg:
                job_id = int(arg)
                state  = _load_state(job_id)
                context.user_data["awaiting_inst_for"] = job_id
                current_inst = state.get("instructions", "")
                current_text = (f"\nActual: <i>{current_inst[:80]}</i>"
                                if current_inst else "")
                await context.bot.send_message(
                    chat_id=query.message.chat_id,
                    text=(
                        f"📝 <b>Instrucciones para nota #{job_id}</b>{current_text}\n\n"
                        f"Escribí el enfoque directamente:\n"
                        f"<i>Ej: enfocalo en pymes exportadoras, tono crítico</i>\n"
                        f"<i>Ej: versión corta, solo datos duros</i>"
                    ),
                    parse_mode="HTML",
                    reply_markup={"inline_keyboard": [[
                        {"text": "↩ Volver", "callback_data": "h_cur_volver"}
                    ]]}
                )

            # ── Formato (continua / desplegable) ──────────────────────────────
            elif action == "h_cur_fmt" and len(parts) >= 3:
                job_id = int(parts[1])
                fmt    = parts[2]  # "continua" | "desplegable"
                state  = _load_state(job_id)
                state["formato"] = fmt
                _save_state(job_id, state)
                keyboard = _cur.build_card_keyboard(job_id, state)
                await query.edit_message_reply_markup(reply_markup=keyboard)
                await query.answer(
                    "☑️ Formato: continua" if fmt == "continua" else "☑️ Formato: desplegable",
                    show_alert=False
                )

            # ── Keywords — ver lista con pesos y ajustar ──────────────────────
            elif action == "h_cur_kw" and arg:
                job_id = int(arg)
                state  = _load_state(job_id)
                matched_kw = state.get("matched_kw", [])
                with _sq.connect(_HDB) as _c:
                    _jrow = _c.execute(
                        "SELECT title, source_url FROM jobs WHERE id=?", (job_id,)
                    ).fetchone()
                _jtitle = (_jrow[0] if _jrow else "") or ""
                _jurl   = (_jrow[1] if _jrow else "") or ""
                _title_short = _jtitle[:55] + "…" if len(_jtitle) > 55 else _jtitle

                def _kw_keyboard(kws, jid):
                    _rows = []
                    for k in kws:
                        ww = _br.get_keyword_weight(k)
                        em = "🟢" if ww > 0.5 else ("🔴" if ww < -0.5 else "🟡")
                        _rows.append([
                            {"text": f"{em} {k}  {ww:+.1f}",
                             "callback_data": f"h_cur_kw:{jid}"},
                            {"text": "👍",  "callback_data": f"h_cur_kw_up:{jid}:{k}"},
                            {"text": "👎",  "callback_data": f"h_cur_kw_dn:{jid}:{k}"},
                            {"text": "✖️",  "callback_data": f"h_cur_kw_rm:{jid}:{k}"},
                            {"text": "🗑",  "callback_data": f"h_cur_kw_del:{jid}:{k}"},
                        ])
                    _rows.append([{"text": "✏️ Ajustar etiquetas",
                                   "callback_data": f"h_cur_kw_add:{jid}"}])
                    _rows.append([{"text": "↩ Volver",
                                   "callback_data": f"h_cur_volver:{jid}"}])
                    return {"inline_keyboard": _rows}

                _hdr = (
                    f"🔑 <b><a href='{_jurl}'>{_title_short}</a></b>\n"
                    f"<i>👍👎 ajustar peso  ·  ✖️ quitar de nota</i>"
                    if _jurl else
                    f"🔑 <b>{_title_short}</b>\n"
                    f"<i>👍👎 ajustar peso  ·  ✖️ quitar de nota</i>"
                )
                try:
                    await query.edit_message_text(
                        _hdr, parse_mode="HTML",
                        reply_markup=_kw_keyboard(matched_kw, job_id),
                        disable_web_page_preview=True,
                    )
                except Exception:
                    pass

            elif action in ("h_cur_kw_up", "h_cur_kw_dn",
                            "h_cur_kw_rm", "h_cur_kw_del") and len(parts) >= 3:
                job_id = int(parts[1])
                kw     = ":".join(parts[2:])
                state  = _load_state(job_id)
                matched_kw = list(state.get("matched_kw", []))

                if action == "h_cur_kw_up":
                    _br.update_keyword_weight(kw, +0.3)
                    w = _br.get_keyword_weight(kw)
                    await query.answer(f"👍 {kw}  {w:+.1f}", show_alert=False)

                elif action == "h_cur_kw_dn":
                    _br.update_keyword_weight(kw, -0.3)
                    w = _br.get_keyword_weight(kw)
                    await query.answer(f"👎 {kw}  {w:+.1f}", show_alert=False)

                elif action == "h_cur_kw_rm":
                    # Quitar solo de esta nota
                    if kw in matched_kw:
                        matched_kw.remove(kw)
                    state["matched_kw"] = matched_kw
                    _save_state(job_id, state)
                    await query.answer(f"✖️ {kw} quitada de esta nota", show_alert=False)

                elif action == "h_cur_kw_del":
                    # Descalificar para siempre + quitar de esta nota
                    _br.blacklist_keyword(kw)
                    if kw in matched_kw:
                        matched_kw.remove(kw)
                    state["matched_kw"] = matched_kw
                    _save_state(job_id, state)
                    await query.answer(f"🗑 {kw} descalificada para siempre", show_alert=False)

                # Refrescar teclado con la lista actualizada
                rows = []
                for k in matched_kw:
                    ww = _br.get_keyword_weight(k)
                    em = "🟢" if ww > 0.5 else ("🔴" if ww < -0.5 else "🟡")
                    rows.append([
                        {"text": f"{em} {k}  {ww:+.1f}",
                         "callback_data": f"h_cur_kw:{job_id}"},
                        {"text": "👍",  "callback_data": f"h_cur_kw_up:{job_id}:{k}"},
                        {"text": "👎",  "callback_data": f"h_cur_kw_dn:{job_id}:{k}"},
                        {"text": "✖️",  "callback_data": f"h_cur_kw_rm:{job_id}:{k}"},
                        {"text": "🗑",  "callback_data": f"h_cur_kw_del:{job_id}:{k}"},
                    ])
                rows.append([{"text": "✏️ Ajustar etiquetas",
                              "callback_data": f"h_cur_kw_add:{job_id}"}])
                rows.append([{"text": "↩ Volver",
                              "callback_data": f"h_cur_volver:{job_id}"}])
                try:
                    await query.edit_message_reply_markup(
                        reply_markup={"inline_keyboard": rows}
                    )
                except Exception:
                    pass

            elif action == "h_cur_kw_add" and arg:
                job_id = int(arg)
                context.user_data["awaiting_kw_for"]              = job_id
                context.user_data["awaiting_kw_panel_msg_id"]     = query.message.message_id
                context.user_data["awaiting_kw_panel_chat_id"]    = query.message.chat_id
                sent = await context.bot.send_message(
                    chat_id=query.message.chat_id,
                    text=(
                        f"✏️ <b>Agregar etiquetas — nota #{job_id}</b>\n\n"
                        f"Separalas con coma, punto o <code> - </code>:\n"
                        f"<code>exportacion, monotributo, arca</code>\n"
                        f"<i>Se suman a las actuales. Usá ✖️ en el panel para borrar.</i>"
                    ),
                    parse_mode="HTML",
                    reply_markup={"inline_keyboard": [[
                        {"text": "↩ Volver", "callback_data": f"h_cur_kw_back:{job_id}"}
                    ]]}
                )
                context.user_data["awaiting_kw_prompt_msg_id"] = sent.message_id
                await query.answer()

            elif action == "h_cur_kw_back" and arg:
                # Volver desde el prompt de agregar etiquetas → restaurar panel de keywords
                job_id        = int(arg)
                panel_msg_id  = context.user_data.pop("awaiting_kw_panel_msg_id",  None)
                panel_chat_id = context.user_data.pop("awaiting_kw_panel_chat_id", None)
                context.user_data.pop("awaiting_kw_for", None)
                context.user_data.pop("awaiting_kw_prompt_msg_id", None)
                # Borrar el prompt (el mensaje actual con este botón)
                try:
                    await query.message.delete()
                except Exception:
                    pass
                # Restaurar panel de keywords en el mensaje original
                if panel_msg_id and panel_chat_id:
                    state = _load_state(job_id)
                    matched_kw = state.get("matched_kw", [])
                    rows = []
                    for k in matched_kw:
                        ww = _br.get_keyword_weight(k)
                        em = "🟢" if ww > 0.5 else ("🔴" if ww < -0.5 else "🟡")
                        rows.append([
                            {"text": f"{em} {k}  {ww:+.1f}",
                             "callback_data": f"h_cur_kw:{job_id}"},
                            {"text": "👍", "callback_data": f"h_cur_kw_up:{job_id}:{k}"},
                            {"text": "👎", "callback_data": f"h_cur_kw_dn:{job_id}:{k}"},
                            {"text": "✖️", "callback_data": f"h_cur_kw_rm:{job_id}:{k}"},
                            {"text": "🗑", "callback_data": f"h_cur_kw_del:{job_id}:{k}"},
                        ])
                    rows.append([{"text": "✏️ Agregar etiquetas",
                                  "callback_data": f"h_cur_kw_add:{job_id}"}])
                    rows.append([{"text": "↩ Volver",
                                  "callback_data": f"h_cur_volver:{job_id}"}])
                    try:
                        await context.bot.edit_message_reply_markup(
                            chat_id=panel_chat_id,
                            message_id=panel_msg_id,
                            reply_markup={"inline_keyboard": rows},
                        )
                    except Exception:
                        pass
                await query.answer()

            # ── Volver — cierra el sub-menú o prompt activo ───────────────────
            elif action == "h_cur_volver":
                context.user_data.pop("awaiting_tags_for", None)
                context.user_data.pop("awaiting_inst_for", None)
                context.user_data.pop("awaiting_kw_for", None)
                if arg:
                    # Viene desde h_cur_cats: el mensaje actual ES la tarjeta
                    # (fue editada para mostrar categorías). Restaurarla.
                    job_id = int(arg)
                    state  = _load_state(job_id)
                    with _sq.connect(_HDB) as _c:
                        row = _c.execute(
                            "SELECT title, score, source_url FROM jobs WHERE id=?",
                            (job_id,)
                        ).fetchone()
                    if row:
                        _title, _score, _url = row
                        _domain = (_url.split("//", 1)[-1].split("/")[0]
                                   .replace("www.", "") if _url else "")
                        _msg = (
                            f"<b>{_title}</b>\n"
                            f"⭐ {float(_score or 0):.1f}  ·  {_domain}\n"
                            f"<a href='{_url}'>📎 ver fuente</a>"
                        )
                        _kb = _cur.build_card_keyboard(job_id, state)
                        await query.edit_message_text(
                            _msg, parse_mode="HTML", reply_markup=_kb
                        )
                    else:
                        await query.message.delete()
                else:
                    # Viene desde h_cur_tags o h_cur_instruct: borrar el prompt
                    await query.message.delete()
                await query.answer()

            # ── Cancelar briefing — borra tarjetas + resumen ──────────────────
            elif action == "h_cur_cancel":
                chat_id = query.message.chat_id
                # Borrar todas las tarjetas del briefing activo (jobs en curado con card_msg_id)
                with _sq.connect(_HDB) as _c:
                    _rows = _c.execute(
                        "SELECT content_json FROM jobs WHERE stage='curado'"
                    ).fetchall()
                for _row in _rows:
                    try:
                        _st = _js.loads(_row[0]) if _row[0] else {}
                        _mid = _st.get("card_msg_id")
                        if _mid:
                            await context.bot.delete_message(chat_id=chat_id, message_id=_mid)
                    except Exception:
                        pass
                # Borrar el mensaje resumen (el que tiene este botón)
                try:
                    await query.message.delete()
                except Exception:
                    pass
                await query.answer("Briefing cerrado", show_alert=False)

            # ── Paginación ────────────────────────────────────────────────────
            elif action == "h_cur_more" and arg:
                offset = int(arg)
                await asyncio.to_thread(_cur.run_briefing, offset)

            # ── Liberar la tanda mostrada y traer la próxima (vacía el registro) ──
            elif action == "h_cur_liberar":
                await query.answer("Liberando…", show_alert=False)
                _cleared = await asyncio.to_thread(_cur.liberar_batch)
                for _mid in (_cleared or []):
                    try:
                        await context.bot.delete_message(
                            chat_id=query.message.chat_id, message_id=_mid)
                    except Exception:
                        pass
                try:
                    await query.message.delete()
                except Exception:
                    pass
                await asyncio.to_thread(_cur.run_briefing, 0)

        except Exception as _he:
            logger.warning(f"h_cur handler error: {_he}")
            await context.bot.send_message(
                chat_id=query.message.chat_id,
                text=f"⚠️ Error en curador: {_he}"
            )
        return
    # ── 2FA Redacción web ────────────────────────────────────────────────────
    if query.data.startswith("redaccion_2fa_"):
        # formato: redaccion_2fa_<token>  o  redaccion_2fa_deny_<token>
        if query.data.startswith("redaccion_2fa_deny_"):
            token = query.data[len("redaccion_2fa_deny_"):]
            action = "deny"
            label = "❌ Acceso rechazado"
        else:
            token = query.data[len("redaccion_2fa_"):]
            action = "confirm"
            label = "✅ Acceso confirmado"
        try:
            requests.post(
                f"http://127.0.0.1:5050/api/tg-2fa/{token}/{action}",
                timeout=5
            )
        except Exception as e:
            logger.warning(f"2FA callback error: {e}")
        await query.edit_message_text(
            f"🔐 *Redacción ME.ar*\n\n{label}.",
            parse_mode="Markdown"
        )
        return

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

    if query.data == "manual_text_start":
        context.user_data["waiting_for_manual_text"] = True
        await query.edit_message_text(
            "✍️ *Ingresá el texto del artículo*\n\nPegá el contenido y lo proceso como una nota normal.",
            parse_mode="Markdown",
        )
        return

    if query.data == "cancel":
        context.user_data.pop("waiting_for_title", None)
        context.user_data.pop("waiting_for_manual_text", None)
        context.chat_data.pop("sug_queue", None)
        await query.edit_message_text("Cancelado.")
        return


    if query.data == "change_title":
        context.user_data["waiting_for_title"] = True
        await query.edit_message_text(
            "Escribi el nuevo titulo para la nota\n"
            "(solo escribilo como mensaje normal):"
        )
        return

    # ─── Frases flow ─────────────────────────────────────────────────────────────
    if query.data in (
        "frase_cancel",
        "frase_pub", "frase_schedule", "fs_back_to_preview",
        "fs_to_ht", "fs_confirm_ht", "fs_change_ht_pre",
        "fs_morning", "fs_noon", "fs_evening",
        "fs_custom", "fs_hour_write", "fs_confirm_custom",
        "frase_tweet", "frase_no_tweet", "frase_change_ht",
        "frase_set_kicker", "frase_set_tag", "frase_set_texto",
    ) or query.data.startswith(("frase_toggle_", "fs_day_", "fs_h_")):

        fp = context.user_data.get("frase_pending")

        if query.data.startswith("frase_toggle_"):
            if not fp:
                await query.edit_message_caption(caption="Error: no hay frase pendiente.")
                return
            # tw/tg/wp/li/igf/igs/wa → tw_on/tg_on/wp_on/li_on/igf_on/igs_on/wa_on
            key = query.data.replace("frase_toggle_", "") + "_on"
            fp[key] = not _frase_flag(fp, key)
            context.user_data["frase_pending"] = fp
            await query.edit_message_reply_markup(reply_markup=_build_frase_kb(fp))
            return

        if query.data in ("frase_set_kicker", "frase_set_tag", "frase_set_texto"):
            if not fp:
                await query.edit_message_caption(caption="Error: no hay frase pendiente.")
                return
            campo = {"frase_set_kicker": "kicker", "frase_set_tag": "tag",
                     "frase_set_texto": "texto"}[query.data]
            context.user_data["awaiting_frase_header"] = campo
            if campo == "texto":
                await query.message.reply_text(
                    f"✏️ Pasame el texto nuevo de la frase (regenero la placa).\n"
                    f"Actual: {fp.get('texto', '')[:200]}")
            else:
                ejemplo = ("Día de la Independencia Argentina" if campo == "kicker"
                           else "9 de julio del 2026")
                actual = ("FRASE DESTACADA" if campo == "kicker" else "INSPIRACIÓN") \
                    if not fp.get(campo) else fp[campo]
                await query.message.reply_text(
                    f"🏷 Pasame el texto para reemplazar «{actual}» "
                    f"({'arriba izquierda' if campo == 'kicker' else 'arriba derecha'}).\n"
                    f"Ej: {ejemplo}")
            return

        if query.data == "frase_cancel":
            context.user_data.pop("frase_pending", None)
            await query.edit_message_caption(caption="Cancelado.")
            return

        if query.data == "fs_back_to_preview":
            if not fp:
                await query.edit_message_caption(caption="Error: no hay frase pendiente.")
                return
            await query.edit_message_caption(
                caption=f"💬 *{md_escape(fp['texto'])}*\n\n_Elegí las redes y acción:_",
                parse_mode="Markdown",
                reply_markup=_build_frase_kb(fp),
            )
            return

        if query.data == "frase_pub":
            if not fp:
                await query.edit_message_caption(caption="Error: no hay frase pendiente.")
                return
            frase     = fp["texto"]
            img_bytes = fp.get("img_bytes")
            tw_on     = fp.get("tw_on", True)
            tg_on     = fp.get("tg_on", True)
            wp_on     = fp.get("wp_on", True)
            li_on     = fp.get("li_on", False)
            custom_ht = context.user_data.get("frase_custom_ht", "#Frases #MundoEmpresarial #Pymes")

            post_url = ""
            post_id  = None
            res_lines = []

            if wp_on:
                await query.edit_message_caption(caption="📤 Publicando…")
                try:
                    import sys as _sys_fr
                    _sys_fr.path.insert(0, "/opt/me-harness")
                    from agents.frases import publish as _frases_publish
                    _fr_result = await asyncio.to_thread(
                        _frases_publish, frase, img_bytes,
                        tw_on=tw_on, tg_on=tg_on, hashtags=custom_ht
                    )
                    post_url = _fr_result["wp_link"]
                    post_id  = _fr_result["wp_id"]
                    res_lines.append(f"✅ WP: {post_url}")
                    if _fr_result.get("tg_msg_id"):
                        res_lines.append("✅ Canal TG")
                    if _fr_result.get("tweet_id"):
                        res_lines.append("✅ Twitter")
                    # Marcar como ya publicado en redes para no repetir abajo
                    tg_on = False
                except Exception as e:
                    res_lines.append(f"❌ Error: {e}")

            if li_on and post_url:
                # Personal (perfil de Leo) — se deja como estaba
                li_url = await asyncio.to_thread(post_linkedin, {"title": frase, "excerpt": ""}, post_url)
                if li_url:
                    res_lines.append(f"✅ LinkedIn (personal): {li_url}")
                else:
                    res_lines.append(f"❌ LinkedIn (personal): {_LAST_LINKEDIN_ERROR[:80]}")
                # Página Mundo Empresarial AR (org) — NUEVO
                li_org_url = await asyncio.to_thread(post_linkedin_org, {"title": frase, "excerpt": ""}, post_url)
                if li_org_url:
                    res_lines.append(f"✅ LinkedIn (página): {li_org_url}")
                else:
                    res_lines.append("❌ LinkedIn (página): ver logs")

            res_lines.extend(await _frase_ig_wa(
                context, query.message.chat_id,
                _frase_flag(fp, "igf_on"), _frase_flag(fp, "igs_on"), _frase_flag(fp, "wa_on"),
                frase, img_bytes, post_url, custom_ht))

            res_text = "\n".join(res_lines)
            await query.edit_message_caption(caption=res_text)

            if tw_on:
                tweet_text = frase
                if post_url:
                    tweet_text += f"\n\n{utm_url(post_url, 'twitter')}"
                tweet_text += f"\n\n{custom_ht}"
                if len(tweet_text) > 280:
                    tweet_text = frase[:200] + "…\n\n" + custom_ht
                context.user_data["frase_tweeting"] = {
                    "frase":     frase,
                    "img_bytes": img_bytes,
                    "post_url":  post_url,
                    "post_id":   post_id,
                }
                kb_tweet = InlineKeyboardMarkup([
                    [
                        InlineKeyboardButton("Twittear", callback_data="frase_tweet"),
                        InlineKeyboardButton("No twittear", callback_data="frase_no_tweet"),
                    ],
                    [InlineKeyboardButton("Cambiar HT", callback_data="frase_change_ht")],
                ])
                await query.message.reply_text(
                    res_text + f"\n\n— Preview del tweet —\n`{md_escape(tweet_text)}`",
                    parse_mode="Markdown",
                    reply_markup=kb_tweet,
                )
            context.user_data.pop("frase_pending", None)
            return

        if query.data == "frase_schedule":
            if not fp:
                await query.edit_message_caption(caption="Error: no hay frase pendiente.")
                return
            ht_now = context.user_data.get("frase_sched_ht", "#Frases #MundoEmpresarial #Pymes")
            await query.edit_message_caption(
                caption=(
                    f"💬 *{md_escape(fp['texto'])}*\n\n"
                    f"*🐦 Twitter — Hashtags:*\n`{md_escape(ht_now)}`\n\n"
                    f"Confirmá los hashtags antes de elegir cuándo publicar:"
                ),
                parse_mode="Markdown",
                reply_markup=_build_frase_sched_pre_ht_kb(),
            )
            return

        if query.data == "fs_to_ht":
            if not fp:
                await query.edit_message_caption(caption="Error: no hay frase pendiente.")
                return
            ht_now = context.user_data.get("frase_sched_ht", "#Frases #MundoEmpresarial #Pymes")
            await query.edit_message_caption(
                caption=(
                    f"💬 *{md_escape(fp['texto'])}*\n\n"
                    f"*🐦 Twitter — Hashtags:*\n`{md_escape(ht_now)}`\n\n"
                    f"Confirmá los hashtags antes de elegir cuándo publicar:"
                ),
                parse_mode="Markdown",
                reply_markup=_build_frase_sched_pre_ht_kb(),
            )
            return

        if query.data == "fs_confirm_ht":
            if not fp:
                await query.edit_message_caption(caption="Error: no hay frase pendiente.")
                return
            await query.edit_message_caption(
                caption=f"💬 *{md_escape(fp['texto'])}*\n\n⏰ *Elegí cuándo publicar:*",
                parse_mode="Markdown",
                reply_markup=_build_frase_schedule_kb(),
            )
            return

        if query.data == "fs_change_ht_pre":
            current_ht = context.user_data.get("frase_sched_ht", "#Frases #MundoEmpresarial #Pymes")
            context.user_data["waiting_for_frase_sched_ht"] = True
            await query.edit_message_caption(
                caption=f"Hashtags actuales: `{md_escape(current_ht)}`\n\nEscribí los nuevos hashtags:",
                parse_mode="Markdown",
            )
            return

        if query.data in ("fs_morning", "fs_noon", "fs_evening"):
            slot = query.data.replace("fs_", "")
            target = _target_datetime_for_slot(slot)
            await _do_frase_schedule(query, context, target)
            return

        if query.data == "fs_custom":
            if not fp:
                await _frase_edit(query, caption="Error: no hay frase pendiente.")
                return
            await _frase_edit(
                query,
                caption="📅 *Elegí el día:*",
                parse_mode="Markdown",
                reply_markup=_build_frase_sched_day_kb(),
            )
            return

        if query.data.startswith("fs_day_"):
            from datetime import datetime, timezone, timedelta
            day_offset = int(query.data[-1])
            context.user_data["frase_sched_day"] = day_offset
            tz_arg    = timezone(timedelta(hours=-3))
            day_dt    = datetime.now(tz_arg) + timedelta(days=day_offset)
            day_label = day_dt.strftime("%A %d/%m")
            await query.edit_message_caption(
                caption=f"🕐 *Elegí la hora para el {day_label}:*",
                parse_mode="Markdown",
                reply_markup=_build_frase_sched_hour_kb(),
            )
            return

        if query.data.startswith("fs_h_"):
            from datetime import datetime, timezone, timedelta
            hour      = int(query.data.split("_")[-1])
            day_offset = context.user_data.get("frase_sched_day", 0)
            tz_arg    = timezone(timedelta(hours=-3))
            now_arg   = datetime.now(tz_arg)
            target = (now_arg + timedelta(days=day_offset)).replace(
                hour=hour, minute=0, second=0, microsecond=0
            )
            if target <= now_arg + timedelta(minutes=5):
                target += timedelta(days=1)
            await _do_frase_schedule(query, context, target)
            return

        if query.data == "fs_hour_write":
            from datetime import datetime, timezone, timedelta
            day_offset = context.user_data.get("frase_sched_day", 0)
            tz_arg    = timezone(timedelta(hours=-3))
            day_label = (datetime.now(tz_arg) + timedelta(days=day_offset)).strftime("%A %d/%m")
            context.user_data["waiting_for_frase_custom_hour"] = True
            await query.edit_message_caption(
                caption=f"Escribí la hora para el {day_label} (formato HH:MM, ej: 14:30):"
            )
            return

        if query.data == "fs_confirm_custom":
            from datetime import datetime, timezone, timedelta
            iso = context.user_data.get("frase_sched_target")
            if not iso:
                await query.edit_message_caption(caption="No hay hora guardada. Volvé a empezar.")
                return
            tz_arg = timezone(timedelta(hours=-3))
            target = datetime.fromisoformat(iso)
            if target.tzinfo is None:
                target = target.replace(tzinfo=tz_arg)
            await _do_frase_schedule(query, context, target)
            return

        if query.data == "frase_tweet":
            ft = context.user_data.get("frase_tweeting")
            if not ft:
                await query.edit_message_text("No hay frase pendiente de twittear.")
                return
            await query.edit_message_text("Publicando en Twitter/X…")
            try:
                auth      = OAuth1(TWITTER_API_KEY, TWITTER_API_SECRET, TWITTER_TOKEN, TWITTER_SECRET)
                img_bytes = ft.get("img_bytes") or b""
                media_id  = None
                if img_bytes:
                    _mime = _twitter_mime(img_bytes)
                    if _mime:
                        r_media = requests.post(
                            "https://upload.twitter.com/1.1/media/upload.json",
                            files={"media": ("image", img_bytes, _mime)}, auth=auth, timeout=30,
                        )
                        media_id = r_media.json().get("media_id_string") if r_media.status_code == 200 else None
                    else:
                        logger.warning("Frase: formato imagen no soportado por Twitter, tweet sin imagen")
                frase     = ft["frase"]
                post_url  = ft.get("post_url", "")
                custom_ht = context.user_data.get("frase_custom_ht", "#Frases #MundoEmpresarial #Pymes")
                tweet_text = frase
                if post_url:
                    tweet_text += f"\n\n{utm_url(post_url, 'twitter')}"
                tweet_text += f"\n\n{custom_ht}"
                if len(tweet_text) > 280:
                    tweet_text = frase[:200] + "…\n\n" + custom_ht
                payload = {"text": tweet_text}
                if media_id:
                    payload["media"] = {"media_ids": [media_id]}
                r_tw = requests.post(
                    "https://api.twitter.com/2/tweets",
                    json=payload, auth=auth, timeout=30,
                )
                if r_tw.status_code in (200, 201):
                    await query.edit_message_text(
                        f"✅ Twitteado!{chr(10) + 'WP: ' + post_url if post_url else ''}"
                    )
                else:
                    await query.edit_message_text(
                        f"❌ Twitter {r_tw.status_code}: {r_tw.text[:200]}"
                        + (f"\n\nWP: {post_url}" if post_url else "")
                    )
            except Exception as e:
                await query.edit_message_text(f"❌ Error en Twitter: {e}")
            context.user_data.pop("frase_tweeting", None)
            return

        if query.data == "frase_no_tweet":
            ft       = context.user_data.pop("frase_tweeting", {})
            post_url = ft.get("post_url", "")
            await query.edit_message_text(
                f"✅ Publicado!" + (f"\n\nWP: {post_url}" if post_url else "")
            )
            return

        if query.data == "frase_change_ht":
            current_ht = context.user_data.get("frase_custom_ht", "#Frases #MundoEmpresarial #Pymes")
            context.user_data["waiting_for_frase_ht"] = True
            await query.edit_message_text(
                f"Hashtags actuales: `{md_escape(current_ht)}`\n\nEscribí los nuevos hashtags:",
                parse_mode="Markdown",
            )
            return

        return  # catch-all frase/fs no manejado

    # ─── ECO flow ─────────────────────────────────────────────────────────────────
    if query.data.startswith("eco_") or query.data.startswith("ecosch_"):
        eco = context.user_data.get("eco")
        if not eco:
            await query.answer("No hay ECO activo.", show_alert=True)
            return

        if query.data == "eco_edit_title":
            context.user_data["waiting_eco_title"] = True
            await query.edit_message_text(
                f"Título actual: `{md_escape(get_title(_eco_data_with_alt(eco)))}`\n\n"
                "Escribí el título alternativo para el ECO:",
                parse_mode="Markdown",
            )
            return

        if query.data == "eco_edit_bajada":
            context.user_data["waiting_eco_bajada"] = True
            d = _eco_data_with_alt(eco)
            await query.edit_message_text(
                f"Bajada actual: _{md_escape(get_excerpt(d)[:120])}_\n\n"
                "Escribí la bajada alternativa para el ECO:",
                parse_mode="Markdown",
            )
            return

        if query.data == "eco_toggle_tw":
            eco["tw_on"] = not eco.get("tw_on", True)
            context.user_data["eco"] = eco
            await query.edit_message_text(_eco_preview_text(eco), parse_mode="Markdown", reply_markup=_build_eco_kb(eco))
            return

        if query.data == "eco_toggle_tg":
            eco["tg_on"] = not eco.get("tg_on", True)
            context.user_data["eco"] = eco
            await query.edit_message_text(_eco_preview_text(eco), parse_mode="Markdown", reply_markup=_build_eco_kb(eco))
            return

        if query.data == "eco_toggle_li":
            eco["li_on"] = not eco.get("li_on", False)
            context.user_data["eco"] = eco
            await query.edit_message_text(_eco_preview_text(eco), parse_mode="Markdown", reply_markup=_build_eco_kb(eco))
            return

        if query.data == "eco_restore":
            eco["alt_title"] = None
            eco["alt_bajada"] = None
            context.user_data["eco"] = eco
            await query.edit_message_text(_eco_preview_text(eco), parse_mode="Markdown", reply_markup=_build_eco_kb(eco))
            return

        if query.data == "eco_cancel":
            context.user_data.pop("eco", None)
            await query.edit_message_text("📣 ECO cancelado.")
            return

        if query.data == "eco_pub_now":
            await query.edit_message_text("📣 Publicando ECO en redes…")
            eco_d = _eco_data_with_alt(eco)
            results = [f"📣 *ECO publicado*\n🔗 {eco['wp_url']}"]
            if eco.get("tg_on", True):
                tg_id = await publish_to_channel(context.bot, eco_d, eco["wp_url"])
                results.append("✅ Canal TG" if tg_id else "❌ Canal TG falló")
            if eco.get("tw_on", True):
                tw_url = await asyncio.to_thread(post_tweet, eco_d, eco["wp_url"])
                if tw_url:
                    results.append(f"✅ Twitter: {tw_url}")
                else:
                    err = _LAST_TWITTER_ERROR or "error desconocido"
                    results.append(f"❌ Twitter: {err[:100]}")
            if eco.get("li_on", False):
                li_url = await asyncio.to_thread(post_linkedin, eco_d, eco["wp_url"])
                if li_url:
                    results.append(f"✅ LinkedIn: {li_url}")
                else:
                    err = _LAST_LINKEDIN_ERROR or "error desconocido"
                    results.append(f"❌ LinkedIn: {err[:100]}")
            context.user_data.pop("eco", None)
            await query.edit_message_text("\n".join(results), parse_mode="Markdown")
            return

        if query.data == "eco_schedule":
            await query.edit_message_text(
                _eco_preview_text(eco) + "\n\n⏰ *Elegí cuándo publicar el ECO:*",
                parse_mode="Markdown",
                reply_markup=_build_eco_schedule_kb(),
            )
            return

        if query.data == "eco_schedule_back":
            await query.edit_message_text(_eco_preview_text(eco), parse_mode="Markdown", reply_markup=_build_eco_kb(eco))
            return

        if query.data in ("ecosch_morning", "ecosch_noon", "ecosch_evening"):
            slot = query.data.replace("ecosch_", "")
            target = _target_datetime_for_slot(slot)
            await _do_eco_schedule(query, context, eco, target)
            return

        if query.data == "ecosch_custom":
            await query.edit_message_text("📅 *Elegí el día para el ECO:*", parse_mode="Markdown", reply_markup=_build_eco_sched_day_kb())
            return

        if query.data in ("ecosch_day_0", "ecosch_day_1", "ecosch_day_2"):
            from datetime import datetime, timezone, timedelta
            day_offset = int(query.data[-1])
            context.user_data["eco_sched_day"] = day_offset
            tz_arg = timezone(timedelta(hours=-3))
            day_dt = datetime.now(tz_arg) + timedelta(days=day_offset)
            day_label = day_dt.strftime("%A %d/%m")
            await query.edit_message_text(
                f"🕐 *Elegí la hora para el ECO el {day_label}:*",
                parse_mode="Markdown",
                reply_markup=_build_eco_sched_hour_kb(),
            )
            return

        if query.data.startswith("ecosch_h_"):
            from datetime import datetime, timezone, timedelta
            hour = int(query.data.split("_")[-1])
            day_offset = context.user_data.get("eco_sched_day", 0)
            tz_arg = timezone(timedelta(hours=-3))
            now_arg = datetime.now(tz_arg)
            target = (now_arg + timedelta(days=day_offset)).replace(
                hour=hour, minute=0, second=0, microsecond=0
            )
            if target <= now_arg + timedelta(minutes=5):
                target += timedelta(days=1)
            await _do_eco_schedule(query, context, eco, target)
            return

        return  # catch-all eco no manejado

    # ─── Article flow ─────────────────────────────────────────────────────────────
    data = context.user_data.get("article")
    if not data:
        await query.edit_message_text("Error: no hay nota pendiente.")
        return

    # ── Programar publicación ──
    if query.data == "pub_schedule":
        tw_on = context.user_data.get("tw_on", True)
        if tw_on:
            # Confirmar / cambiar HT antes de elegir hora
            auto_ht = _build_hashtags(data)
            ht_now = context.user_data.get("pre_sched_hashtags") or auto_ht
            context.user_data["pre_sched_hashtags"] = ht_now
            await query.edit_message_text(
                build_preview(data) + f"\n\n*🐦 Twitter — Hashtags:*\n`{md_escape(ht_now)}`\n\nConfirmá los hashtags o cambiálos antes de elegir cuándo publicar:",
                parse_mode="Markdown",
                reply_markup=_build_sched_pre_ht_kb(),
            )
        else:
            # Twitter apagado: ir directo al picker de hora
            await query.edit_message_text(
                build_preview(data) + "\n\n⏰ *Elegí cuándo publicar:*",
                parse_mode="Markdown",
                reply_markup=build_schedule_kb(),
            )
        return

    if query.data == "sched_back":
        await query.edit_message_text(
            build_preview(data), parse_mode="Markdown",
            reply_markup=_preview_kb_from_ctx(context),
        )
        return

    # ── Volver desde menú de slots al paso de HT ──
    if query.data == "sched_to_ht":
        auto_ht = _build_hashtags(data)
        ht_now = context.user_data.get("pre_sched_hashtags") or auto_ht
        context.user_data["pre_sched_hashtags"] = ht_now
        await query.edit_message_text(
            build_preview(data) + f"\n\n*🐦 Twitter — Hashtags:*\n`{md_escape(ht_now)}`\n\nConfirmá los hashtags o cambiálos antes de elegir cuándo publicar:",
            parse_mode="Markdown",
            reply_markup=_build_sched_pre_ht_kb(),
        )
        return

    # ── HT confirmados → mostrar menú de slots ──
    if query.data == "sched_confirm_ht":
        await query.edit_message_text(
            build_preview(data) + "\n\n⏰ *Elegí cuándo publicar:*",
            parse_mode="Markdown",
            reply_markup=build_schedule_kb(),
        )
        return

    # ── Cambiar HT antes de programar ──
    if query.data == "sched_change_ht_pre":
        current_ht = context.user_data.get("pre_sched_hashtags") or _build_hashtags(data)
        context.user_data["_ht_suggested_sched"] = current_ht
        context.user_data["waiting_for_pre_sched_ht"] = True
        await query.edit_message_text(
            f"Hashtags actuales: `{md_escape(current_ht)}`\n\n"
            "Escribí los nuevos hashtags (con o sin #, separados por espacios):",
            parse_mode="Markdown",
        )
        return

    # ── Turnos fijos ──
    if query.data in ("sched_morning", "sched_noon", "sched_evening"):
        slot = query.data.replace("sched_", "")
        target = _target_datetime_for_slot(slot)
        await _do_schedule(query, context, data, target)
        return

    # ── Fijar hora: mostrar picker de día ──
    if query.data == "sched_custom":
        await query.edit_message_text(
            "📅 *Elegí el día:*",
            parse_mode="Markdown",
            reply_markup=build_sched_day_kb(),
        )
        return

    # ── Día seleccionado → mostrar picker de hora ──
    if query.data in ("sched_day_0", "sched_day_1", "sched_day_2"):
        from datetime import datetime, timezone, timedelta
        day_offset = int(query.data[-1])
        context.user_data["sched_custom_day"] = day_offset
        tz_arg = timezone(timedelta(hours=-3))
        day_dt = datetime.now(tz_arg) + timedelta(days=day_offset)
        day_label = day_dt.strftime("%A %d/%m")
        await query.edit_message_text(
            f"🕐 *Elegí la hora para el {day_label}:*",
            parse_mode="Markdown",
            reply_markup=build_sched_hour_kb(),
        )
        return

    # ── Hora seleccionada (botón) → programar ──
    if query.data.startswith("sched_h_"):
        from datetime import datetime, timezone, timedelta
        hour = int(query.data.split("_")[-1])
        day_offset = context.user_data.get("sched_custom_day", 0)
        tz_arg = timezone(timedelta(hours=-3))
        now_arg = datetime.now(tz_arg)
        target = (now_arg + timedelta(days=day_offset)).replace(
            hour=hour, minute=0, second=0, microsecond=0
        )
        if target <= now_arg + timedelta(minutes=5):
            target += timedelta(days=1)
        await _do_schedule(query, context, data, target)
        return

    # ── Escribir hora personalizada ──
    if query.data == "sched_hour_write":
        from datetime import datetime, timezone, timedelta
        day_offset = context.user_data.get("sched_custom_day", 0)
        tz_arg = timezone(timedelta(hours=-3))
        day_label = (datetime.now(tz_arg) + timedelta(days=day_offset)).strftime("%A %d/%m")
        context.user_data["waiting_for_custom_hour"] = True
        await query.edit_message_text(
            f"Escribí la hora para el {day_label} (formato HH:MM, ej: 14:30):"
        )
        return

    # ── Confirmar hora personalizada escrita ──
    if query.data == "sched_confirm_custom":
        from datetime import datetime, timezone, timedelta
        iso = context.user_data.get("sched_custom_target")
        if not iso:
            await query.edit_message_text("No hay hora guardada. Volvé a empezar.")
            return
        tz_arg = timezone(timedelta(hours=-3))
        target = datetime.fromisoformat(iso)
        if target.tzinfo is None:
            target = target.replace(tzinfo=tz_arg)
        await _do_schedule(query, context, data, target)
        return

    if query.data == "change_ht":
        stored = context.user_data.get("published")
        current_ht = context.user_data.get("custom_hashtags")
        if not current_ht and stored:
            current_ht = _build_hashtags(stored["data"])
        context.user_data["_ht_suggested_direct"] = current_ht
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
            tw_id = tweet_id_from_url(tweet_url)
            if tw_id and stored.get("id"):
                tg_msg_id = stored.get("tg_msg_id", 0)
                await asyncio.to_thread(
                    append_social_meta, stored["id"], stored["content"],
                    tw_id, tg_msg_id
                )
            warn = get_last_twitter_error()  # advertencia de retry sin imagen, si aplica
            warn_msg = f"\n{warn}" if warn else ""
            await query.edit_message_text(
                f"Publicado en WordPress y en Twitter/X!\n\n"
                f"WP: {stored['url']}\nTweet: {tweet_url}{warn_msg}"
            )
        else:
            err = get_last_twitter_error() or "(sin detalle)"
            await query.edit_message_text(
                f"Publicado en WordPress pero falló Twitter.\n\n"
                f"WP: {stored['url']}\n\n"
                f"⚠️ Twitter dijo: `{md_escape(err[:300])}`",
                parse_mode="Markdown",
            )
        return

    if query.data == "no_tweet":
        stored = context.user_data.get("published")
        url = stored["url"] if stored else ""
        await query.edit_message_text(f"Publicado!\n\n{url}")
        return

    if query.data == "pub_auto_confirm":
        if not data:
            await query.edit_message_text("No hay nota activa.")
            return

        tw_on   = context.user_data.get("tw_on", True)
        tg_on   = context.user_data.get("tg_on", True)
        li_on   = context.user_data.get("li_on", False)
        dest_on = context.user_data.get("dest_on", False)

        await query.edit_message_text("📤 Enviando al harness…")

        image_id = data.pop("_featured_media_id", None)
        if not image_id and data.get("image_url"):
            kw  = focus_keyword(data["title"])
            alt = f"{kw} - {get_title(data)}"
            image_id = await asyncio.to_thread(upload_image, data["image_url"], alt)

        job_id = await asyncio.to_thread(_enqueue_to_harness, data, image_id, dest_on)
        await query.edit_message_text(
            f"✅ *En cola del harness* — job \\#{job_id}\n"
            f"El publicador lo publica en segundos con formato estándar\\.",
            parse_mode="Markdown",
        )

        _edit_idx = context.user_data.pop("auto_todo_edit_idx", None)
        if _edit_idx is not None:
            _proc = context.chat_data.get("auto_todo_processed", [])
            if _edit_idx < len(_proc):
                _proc[_edit_idx]["done"] = True
                context.chat_data["auto_todo_processed"] = _proc

        sug_queue = context.chat_data.get("sug_queue", [])
        if sug_queue:
            next_url = sug_queue.pop(0)
            context.chat_data["sug_queue"] = sug_queue
            context.user_data["curador_auto"] = True
            async def _next_reply(text, **kw):
                return await context.bot.send_message(chat_id=query.message.chat_id, text=text, **kw)
            fake_msg = type("FM", (), {"text": next_url, "chat_id": query.message.chat_id, "reply_text": _next_reply})()
            fake_upd = type("FU", (), {"message": fake_msg})()
            asyncio.create_task(handle_link(fake_upd, context))
        return

    # ── Publicar (fallback) → harness ──
    destacado = context.user_data.get("dest_on", False)
    await query.edit_message_text("📤 Enviando al harness…")

    image_id = None
    if data.get("image_url"):
        kw  = focus_keyword(data["title"])
        alt = f"{kw} - {get_title(data)}"
        image_id = await asyncio.to_thread(upload_image, data["image_url"], alt)

    job_id = await asyncio.to_thread(_enqueue_to_harness, data, image_id, destacado)
    await query.edit_message_text(
        f"✅ *En cola del harness* — job \\#{job_id}\n"
        f"El publicador lo publica en segundos con formato estándar\\.",
        parse_mode="Markdown",
    )


# ── Borrar nota ───────────────────────────────────────────────────────────────

def find_post(query: str) -> dict | None:
    """Busca un post en WP vía WARP (Ferozo bloquea IP del VPS con 415 sin proxy)."""
    h    = wp_auth()
    warp = {"http": "socks5://127.0.0.1:40000", "https": "socks5://127.0.0.1:40000"}

    def _extract(p):
        return {
            "id":             p["id"],
            "title":          p["title"].get("rendered", ""),
            "link":           p["link"],
            "categories":     p.get("categories", []),
            "featured_media": p.get("featured_media", 0),
            "content":        p.get("content", {}).get("raw", "") or p.get("content", {}).get("rendered", ""),
        }

    if query.strip().isdigit():
        r = requests.get(
            f"{WP_URL}/wp-json/wp/v2/posts/{query.strip()}?context=edit",
            headers=h, proxies=warp, timeout=10
        )
        return _extract(r.json()) if r.status_code == 200 else None

    clean = query.strip().rstrip("/")
    slug  = clean.split("/")[-1]
    r = requests.get(
        f"{WP_URL}/wp-json/wp/v2/posts?slug={slug}&per_page=1&context=edit&status=any",
        headers=h, proxies=warp, timeout=10
    )
    if r.status_code == 200 and r.json():
        return _extract(r.json()[0])
    return None


def trash_post(post_id: int) -> bool:
    h = {**wp_auth(), "Content-Type": "application/json"}
    r = requests.delete(f"{WP_URL}/wp-json/wp/v2/posts/{post_id}", headers=h,
                        proxies=_WP_PROXIES, timeout=15)
    return r.status_code in (200, 201)


def update_post(post_id: int, payload: dict) -> bool:
    """Actualiza un post existente en WordPress. Devuelve True si ok."""
    h = {**wp_auth(), "Content-Type": "application/json"}
    r = requests.post(
        f"{WP_URL}/wp-json/wp/v2/posts/{post_id}",
        headers=h, json=payload, proxies=_WP_PROXIES, timeout=30
    )
    if r.status_code in (200, 201):
        return True
    logger.error(f"update_post {post_id}: {r.status_code} {r.text[:300]}")
    return False


# ── Hilo de Twitter ───────────────────────────────────────────────────────────

async def cmd_hilo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Uso: /hilo <URL o ID>  —  genera un hilo de Twitter para una nota ya publicada."""
    args = " ".join(context.args).strip()
    if not args:
        await update.message.reply_text(
            "Uso: /hilo <URL o ID>\n"
            "Ejemplo: /hilo https://mundoempresarial.ar/mi-nota/\n"
            "O: /hilo 1234"
        )
        return

    if not OPENAI_API_KEY:
        await update.message.reply_text(
            "❌ Necesito OPENAI_API_KEY configurada en Railway para generar hilos."
        )
        return

    msg = await update.message.reply_text("Buscando la nota y generando el hilo...")

    post = await asyncio.to_thread(find_post, args)
    if not post:
        await msg.edit_text("No encontré la nota. Verificá la URL o el ID.")
        return

    # Limpiar el contenido HTML a texto plano para GPT
    content_html = post.get("content", "") or ""
    # Sacar tags HTML para que GPT tenga el texto limpio
    body_soup = BeautifulSoup(content_html, "html.parser")
    # Sacar el comentario de mebot para que no aparezca
    body_text = body_soup.get_text(separator="\n").strip()
    body_text = re.sub(r'<!--.*?-->', '', body_text, flags=re.DOTALL)
    # Limpiar líneas vacías múltiples
    body_text = re.sub(r'\n{3,}', '\n\n', body_text)

    title = BeautifulSoup(post["title"], "html.parser").get_text()
    wp_url = utm_url(post["link"], "twitter")

    hashtags = _build_hashtags({"title": title, "excerpt": body_text[:400]})

    try:
        tweets = await asyncio.to_thread(
            generate_thread_with_gpt, title, body_text, wp_url, hashtags
        )
    except RuntimeError as e:
        await msg.edit_text(f"❌ {e}")
        return
    except Exception as e:
        logger.error(f"cmd_hilo generate: {e}")
        await msg.edit_text(f"❌ Error generando hilo: {type(e).__name__}")
        return

    if not tweets or len(tweets) < 2:
        await msg.edit_text("❌ GPT devolvió un hilo muy corto, probá de nuevo.")
        return

    # Guardar para publicación
    context.user_data["thread_post"] = {
        "post": post,
        "tweets": tweets,
        "image_url": "",
    }
    # Intentar obtener la imagen destacada del post (para el primer tweet)
    if post.get("featured_media"):
        try:
            h = wp_auth()
            r = requests.get(
                f"{WP_URL}/wp-json/wp/v2/media/{post['featured_media']}",
                headers=h, timeout=10,
            )
            if r.status_code == 200:
                context.user_data["thread_post"]["image_url"] = r.json().get("source_url", "")
        except Exception:
            pass

    # Mostrar preview
    preview_text = _build_thread_preview(tweets, post["title"])
    kb = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("🚀 Publicar hilo", callback_data="thread_publish"),
            InlineKeyboardButton("🔄 Regenerar", callback_data="thread_regen"),
        ],
        [InlineKeyboardButton("Cancelar", callback_data="thread_cancel")],
    ])
    await msg.edit_text(preview_text, parse_mode="Markdown", reply_markup=kb)


def _build_thread_preview(tweets: list[str], title: str) -> str:
    """Preview del hilo para Telegram."""
    title_clean = BeautifulSoup(title, "html.parser").get_text()
    lines = [f"🧵 *Vista previa del hilo ({len(tweets)} tweets)*", ""]
    lines.append(f"_Nota:_ {md_escape(title_clean[:80])}")
    lines.append("")
    for i, tw in enumerate(tweets, 1):
        lines.append(f"*[{i}/{len(tweets)}]* ({len(tw)} chars)")
        lines.append(f"```\n{tw}\n```")
    return "\n".join(lines)


async def handle_thread_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    if query.data == "thread_cancel":
        context.user_data.pop("thread_post", None)
        await query.edit_message_text("Hilo cancelado.")
        return

    stored = context.user_data.get("thread_post")
    if not stored:
        await query.edit_message_text("No hay hilo pendiente. Usá /hilo <URL>.")
        return

    if query.data == "thread_regen":
        post = stored["post"]
        await query.edit_message_text("🔄 Regenerando hilo...")

        content_html = post.get("content", "") or ""
        body_soup = BeautifulSoup(content_html, "html.parser")
        body_text = body_soup.get_text(separator="\n").strip()
        body_text = re.sub(r'<!--.*?-->', '', body_text, flags=re.DOTALL)
        body_text = re.sub(r'\n{3,}', '\n\n', body_text)

        title = BeautifulSoup(post["title"], "html.parser").get_text()
        wp_url = utm_url(post["link"], "twitter")
        hashtags = _build_hashtags({"title": title, "excerpt": body_text[:400]})

        try:
            tweets = await asyncio.to_thread(
                generate_thread_with_gpt, title, body_text, wp_url, hashtags
            )
        except Exception as e:
            await query.edit_message_text(f"❌ Error regenerando: {type(e).__name__}")
            return

        stored["tweets"] = tweets
        context.user_data["thread_post"] = stored

        preview_text = _build_thread_preview(tweets, post["title"])
        kb = InlineKeyboardMarkup([
            [
                InlineKeyboardButton("🚀 Publicar hilo", callback_data="thread_publish"),
                InlineKeyboardButton("🔄 Regenerar", callback_data="thread_regen"),
            ],
            [InlineKeyboardButton("Cancelar", callback_data="thread_cancel")],
        ])
        await query.edit_message_text(preview_text, parse_mode="Markdown", reply_markup=kb)
        return

    if query.data == "thread_publish":
        await query.edit_message_text(f"🚀 Publicando hilo de {len(stored['tweets'])} tweets...")

        urls = await asyncio.to_thread(
            post_twitter_thread, stored["tweets"], stored["image_url"]
        )

        if not urls:
            await query.edit_message_text("❌ No se pudo publicar el primer tweet del hilo.")
            return

        n_expected = len(stored["tweets"])
        n_actual = len(urls)
        status = f"✅ Hilo publicado ({n_actual}/{n_expected} tweets)"
        if n_actual < n_expected:
            status = f"⚠️ Hilo publicado parcialmente ({n_actual}/{n_expected})"

        # Link al primer tweet (el hilo se ve desde ahí)
        await query.edit_message_text(
            f"{status}\n\n🔗 Primer tweet:\n{urls[0]}"
        )
        context.user_data.pop("thread_post", None)
        return


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
        await query.edit_message_text("Error al borrar. Revisa los logs del VPS.")


# ── Editar nota ───────────────────────────────────────────────────────────────

def _build_edit_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("✏️ Cambiar título", callback_data="edit_title"),
            InlineKeyboardButton("📂 Cambiar categoría", callback_data="edit_cat"),
        ],
        [
            InlineKeyboardButton("🖼️ Cambiar foto", callback_data="edit_photo"),
            InlineKeyboardButton("🏷 Watermark", callback_data="edit_wm"),
        ],
        [
            InlineKeyboardButton("📡 Publicar en redes", callback_data="edit_publish"),
        ],
        [
            InlineKeyboardButton("🗑️ Borrar", callback_data="edit_delete"),
            InlineKeyboardButton("Cancelar", callback_data="edit_cancel"),
        ],
    ])


# Redes de /editar → Publicar. Las 5 con API las difunde el AGENTE (redes.difundir_nota);
# WhatsApp no tiene API → texto para copiar y pegar.
_EDIT_PUB_NETS = [
    ("tw", "🐦 Twitter"),
    ("tg", "📣 Canal TG"),
    ("li", "💼 LinkedIn"),
    ("fb", "📘 Facebook"),
    ("ig", "📷 Instagram"),
    ("wa", "🟢 WhatsApp"),
]
# Mapa red del bot → canal del agente. WhatsApp queda afuera (sin API).
_EDIT_PUB_CANAL = {"tw": "x", "tg": "tg", "li": "li", "fb": "fb", "ig": "ig"}


def _build_publish_social_kb(st: dict) -> InlineKeyboardMarkup:
    """Teclado para difundir una nota existente en TODAS las redes (vía agente redes).
    `st` es un dict con claves pub_tw / pub_tg / pub_li / pub_fb / pub_ig / pub_wa."""
    rows, row = [], []
    for key, label in _EDIT_PUB_NETS:
        icon = "✅" if st.get(f"pub_{key}") else "❌"
        row.append(InlineKeyboardButton(f"{icon} {label}", callback_data=f"pubtoggle_{key}"))
        if len(row) == 2:
            rows.append(row); row = []
    if row:
        rows.append(row)
    rows.append([
        InlineKeyboardButton("📡 Publicar", callback_data="pub_execute"),
        InlineKeyboardButton("Cancelar", callback_data="edit_cancel"),
    ])
    return InlineKeyboardMarkup(rows)


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


def _build_delete_kb(del_tw: bool, del_wp: bool, del_tg: bool, has_tw: bool, has_tg: bool,
                     del_li: bool = False, has_li: bool = False) -> InlineKeyboardMarkup:
    """Teclado con toggles on/off para elegir qué borrar."""
    tw_label = ("✅" if del_tw else "❌") + " Borrar de Twitter" + ("" if has_tw else " (N/A)")
    wp_label = ("✅" if del_wp else "❌") + " Borrar de WordPress"
    tg_label = ("✅" if del_tg else "❌") + " Borrar del canal TG" + ("" if has_tg else " (N/A)")
    li_label = ("✅" if del_li else "❌") + " Borrar de LinkedIn" + ("" if has_li else " (N/A)")
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(wp_label, callback_data="deltoggle_wp")],
        [InlineKeyboardButton(tw_label, callback_data="deltoggle_tw")],
        [InlineKeyboardButton(tg_label, callback_data="deltoggle_tg")],
        [InlineKeyboardButton(li_label, callback_data="deltoggle_li")],
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

    if query.data == "edit_wm":
        post = context.user_data.get("edit_post")
        if not post:
            await query.answer("No hay nota en edición.", show_alert=True)
            return
        if not (post.get("featured_media") or 0):
            await query.answer("La nota no tiene foto destacada.", show_alert=True)
            return
        await query.answer("Aplicando watermark…")
        ok = await asyncio.to_thread(_apply_wm_to_featured, post)
        _txt = ("✅ Watermark aplicado a la foto. " if ok else "❌ No pude aplicar el watermark. ") + post.get("link", "")
        await query.edit_message_text(_txt)
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
        has_li = bool(meta.get("li_urn"))

        context.user_data["del_wp"] = True
        context.user_data["del_tw"] = has_tw
        context.user_data["del_tg"] = has_tg
        context.user_data["del_li"] = has_li
        context.user_data["del_has_tw"] = has_tw
        context.user_data["del_has_tg"] = has_tg
        context.user_data["del_has_li"] = has_li

        tw_info = f"Tweet ID: `{meta.get('tweet_id','-')}`" if has_tw else "Tweet: no registrado"
        tg_info = f"TG msg: `{meta.get('tg_msg','-')}`" if has_tg else "TG canal: no registrado"
        li_info = f"LinkedIn URN: `{meta.get('li_urn','-')}`" if has_li else "LinkedIn: no registrado"

        await query.edit_message_text(
            f"🗑️ *Borrar nota*\n\n*{post['title']}*\n\n"
            f"{tw_info}\n{tg_info}\n{li_info}\n\n"
            "Elegí qué borrar:",
            parse_mode="Markdown",
            reply_markup=_build_delete_kb(
                context.user_data["del_tw"],
                context.user_data["del_wp"],
                context.user_data["del_tg"],
                has_tw, has_tg,
                context.user_data["del_li"], has_li,
            ),
        )
        return

    # Toggles de delete
    if query.data == "deltoggle_wp":
        context.user_data["del_wp"] = not context.user_data.get("del_wp", True)
        await query.edit_message_reply_markup(reply_markup=_build_delete_kb(
            context.user_data.get("del_tw", False), context.user_data["del_wp"],
            context.user_data.get("del_tg", False), context.user_data.get("del_has_tw", False),
            context.user_data.get("del_has_tg", False), context.user_data.get("del_li", False),
            context.user_data.get("del_has_li", False),
        ))
        return

    if query.data == "deltoggle_tw":
        if not context.user_data.get("del_has_tw"):
            return
        context.user_data["del_tw"] = not context.user_data.get("del_tw", False)
        await query.edit_message_reply_markup(reply_markup=_build_delete_kb(
            context.user_data["del_tw"], context.user_data.get("del_wp", True),
            context.user_data.get("del_tg", False), context.user_data.get("del_has_tw", False),
            context.user_data.get("del_has_tg", False), context.user_data.get("del_li", False),
            context.user_data.get("del_has_li", False),
        ))
        return

    if query.data == "deltoggle_tg":
        if not context.user_data.get("del_has_tg"):
            return
        context.user_data["del_tg"] = not context.user_data.get("del_tg", False)
        await query.edit_message_reply_markup(reply_markup=_build_delete_kb(
            context.user_data.get("del_tw", False), context.user_data.get("del_wp", True),
            context.user_data["del_tg"], context.user_data.get("del_has_tw", False),
            context.user_data.get("del_has_tg", False), context.user_data.get("del_li", False),
            context.user_data.get("del_has_li", False),
        ))
        return

    if query.data == "deltoggle_li":
        if not context.user_data.get("del_has_li"):
            return
        context.user_data["del_li"] = not context.user_data.get("del_li", False)
        await query.edit_message_reply_markup(reply_markup=_build_delete_kb(
            context.user_data.get("del_tw", False), context.user_data.get("del_wp", True),
            context.user_data.get("del_tg", False), context.user_data.get("del_has_tw", False),
            context.user_data.get("del_has_tg", False), context.user_data["del_li"],
            context.user_data.get("del_has_li", False),
        ))
        return

    if query.data == "del_execute":
        del_wp = context.user_data.get("del_wp", False)
        del_tw = context.user_data.get("del_tw", False)
        del_tg = context.user_data.get("del_tg", False)
        del_li = context.user_data.get("del_li", False)

        if not any([del_wp, del_tw, del_tg, del_li]):
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

        # Borrar de LinkedIn
        if del_li and meta.get("li_urn"):
            ok = await asyncio.to_thread(delete_linkedin_post, meta["li_urn"])
            results.append("✅ Post de LinkedIn borrado" if ok else "❌ Error borrando de LinkedIn")

        # Borrar de WordPress (último, porque cambia la URL)
        if del_wp:
            ok = await asyncio.to_thread(trash_post, post["id"])
            results.append("✅ Nota enviada a la papelera de WordPress" if ok
                           else "❌ Error borrando de WordPress")

        # Limpiar estado
        for k in ("edit_post", "del_wp", "del_tw", "del_tg", "del_li",
                  "del_has_tw", "del_has_tg", "del_has_li"):
            context.user_data.pop(k, None)

        await query.edit_message_text("\n".join(results) or "Nada que borrar.")
        return

    # ── Publicar en redes: mostrar toggles (TODAS las redes vía agente) ──
    if query.data == "edit_publish":
        meta = parse_social_meta(post.get("content", ""))
        has_tw = bool(meta.get("tweet_id"))
        has_tg = bool(meta.get("tg_msg"))
        # Default ON en las redes con API que aún no se publicaron; WhatsApp OFF (manual).
        context.user_data["pub_tw"] = not has_tw
        context.user_data["pub_tg"] = not has_tg
        context.user_data["pub_li"] = True
        context.user_data["pub_fb"] = True
        context.user_data["pub_ig"] = True
        context.user_data["pub_wa"] = False

        status_lines = []
        if has_tw:
            status_lines.append(f"⚠️ Ya tuiteado (`{meta.get('tweet_id')}`) — marcarlo crea uno nuevo")
        if has_tg:
            status_lines.append(f"⚠️ Ya en canal TG (`{meta.get('tg_msg')}`) — se repite el mensaje")
        status_str = "\n".join(status_lines) if status_lines else "Sin publicaciones previas."

        await query.edit_message_text(
            f"📡 *Publicar en redes*\n\n*{post['title']}*\n\n"
            f"{status_str}\n\n"
            "_TG/X/LinkedIn salen al toque · Facebook/Instagram se encolan con pacing "
            "(≈15 min entre notas, sin ráfaga) · WhatsApp = texto para copiar._\n\n"
            "Elegí destinos:",
            parse_mode="Markdown",
            reply_markup=_build_publish_social_kb(context.user_data),
        )
        return

    # Toggle genérico de cualquier red (pubtoggle_tw / _tg / _li / _fb / _ig / _wa)
    if query.data.startswith("pubtoggle_"):
        net = query.data.split("_", 1)[1]
        context.user_data[f"pub_{net}"] = not context.user_data.get(f"pub_{net}", False)
        await query.edit_message_reply_markup(
            reply_markup=_build_publish_social_kb(context.user_data)
        )
        return

    if query.data == "pub_execute":
        sel = [k for k, _ in _EDIT_PUB_NETS if context.user_data.get(f"pub_{k}")]
        if not sel:
            await query.edit_message_text("Nada seleccionado. Cancelado.")
            return

        await query.edit_message_text("📡 Publicando en redes…")
        post_url = post["link"]
        import html as _html
        title_clean = _html.unescape(re.sub(r"<[^>]+>", "", post.get("title", ""))).strip()
        results = []

        # 1) Redes con API → agente redes.difundir_nota (placas + UTM + pacing Meta).
        canales = [_EDIT_PUB_CANAL[k] for k in sel if k in _EDIT_PUB_CANAL]
        if canales:
            cat_ids  = post.get("categories") or []
            cat_name = CAT_NAMES.get(cat_ids[0], "Noticias") if cat_ids else "Noticias"

            def _difundir():
                import sys as _sys
                if "/opt/me-harness" not in _sys.path:
                    _sys.path.insert(0, "/opt/me-harness")
                from agents import publicador as _pub
                return _pub.post_to_social(
                    wp_post_id=post["id"], wp_url=post_url, title=title_clean,
                    excerpt="", tags=[], categoria=cat_name, canales=tuple(canales),
                )
            try:
                social = await asyncio.wait_for(asyncio.to_thread(_difundir), timeout=150)
            except Exception as e:
                social = {}
                results.append(f"❌ Error del agente de redes: `{str(e)[:200]}`")

            errores = social.get("errores") or {}
            if "tg" in canales:
                results.append("✅ Canal @MundoEmpresarial_AR" if social.get("tg_msg_id")
                               else f"❌ Telegram: {errores.get('tg', '—')}")
            if "x" in canales:
                results.append(f"✅ Twitter: https://x.com/i/status/{social['tweet_id']}"
                               if social.get("tweet_id") else f"❌ Twitter: {errores.get('x', '—')}")
            if "li" in canales:
                results.append("✅ LinkedIn" if social.get("li_urn")
                               else f"❌ LinkedIn: {errores.get('li', '—')}")
            meta_enc = social.get("meta_encolados") or []
            if "fb" in canales:
                results.append("🕒 Facebook encolado (sale con pacing)" if "fb" in meta_enc
                               else "❌ Facebook no encolado (revisar claves)")
            if "ig" in canales:
                results.append("🕒 Instagram encolado (sale con pacing)" if "ig" in meta_enc
                               else "❌ Instagram no encolado (revisar claves)")

        # 2) WhatsApp: sin API → texto para copiar y pegar.
        if "wa" in sel:
            data = await asyncio.to_thread(_post_to_data, post)
            s_title = get_title(data)
            kw_wa = focus_keyword(data.get("original_title") or data.get("title", ""))
            s_excerpt_wa = get_excerpt(data, kw=kw_wa)
            wa_text = f"📰 {s_title}\n\n{s_excerpt_wa[:200]}\n\n🔗 {utm_url(post_url, 'whatsapp')}"
            await query.message.reply_text(f"— Copiá y pegá en WhatsApp —\n\n{wa_text}")
            results.append("✅ Texto para WhatsApp preparado")

        # Limpiar estado
        for k, _ in _EDIT_PUB_NETS:
            context.user_data.pop(f"pub_{k}", None)
        context.user_data.pop("edit_post", None)

        await query.edit_message_text("\n".join(results) or "Nada ejecutado.")
        return


# ── /publicador — Gestión de notas publicadas ─────────────────────────────────

_PUBX_NETS_LINKS = ["wp", "telegram", "twitter", "facebook", "instagram", "linkedin", "whatsapp"]
_PUBX_NETS_REP   = ["telegram", "twitter", "facebook", "instagram", "linkedin", "whatsapp"]
_PUBX_NETS_DEL   = ["wp", "telegram", "twitter", "linkedin"]

_PUBX_NET_LABEL = {
    "wp": "WordPress", "telegram": "Telegram", "twitter": "Twitter",
    "facebook": "Facebook", "instagram": "Instagram",
    "linkedin": "LinkedIn", "whatsapp": "WhatsApp",
}


def _pubx_copy(net: str, title: str, excerpt: str, url: str, post_id: int) -> str:
    """Copy + link UTM para compartir en cada red."""
    ex = excerpt[:280] if excerpt else ""
    if net == "wp":
        edit_url = f"{WP_URL}/wp-admin/post.php?post={post_id}&action=edit"
        return f"🌐 *WordPress*\n{url}\nAdmin: {edit_url}"
    if net == "telegram":
        return f"📰 *{md_escape(title)}*\n\n{md_escape(ex)}\n\n{utm_url(url, 'telegram')}"
    if net == "twitter":
        tw = utm_url(url, "twitter")
        head = title[:200] if len(title) > 200 else title
        return f"{head}\n\n{tw}"
    if net == "facebook":
        return f"📰 {title}\n\n{ex}\n\n{utm_url(url, 'facebook')}"
    if net == "instagram":
        return f"📷 {title}\n\n{ex}\n\n#MundoEmpresarial #Economia #Pymes #Argentina"
    if net == "linkedin":
        return f"{title}\n\n{ex}\n\n{utm_url(url, 'linkedin')}"
    if net == "whatsapp":
        return f"*{title}*\n\n{ex}\n\n{utm_url(url, 'whatsapp')}"
    return f"{title}\n{url}"


def _build_pubx_main_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📋 Traer links",  callback_data="pubx_links")],
        [InlineKeyboardButton("🔁 Republicar",   callback_data="pubx_rep")],
        [InlineKeyboardButton("🗑️ Borrar",       callback_data="pubx_del")],
        [InlineKeyboardButton("↩️ Cerrar",       callback_data="pubx_cancel")],
    ])


def _build_pubx_links_kb(sel: set) -> InlineKeyboardMarkup:
    rows = []
    row = []
    for net in _PUBX_NETS_LINKS:
        icon = "✅" if net in sel else "☐"
        row.append(InlineKeyboardButton(f"{icon} {_PUBX_NET_LABEL[net]}",
                                        callback_data=f"pubx_lt_{net}"))
        if len(row) == 2:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    rows.append([
        InlineKeyboardButton("📤 Traer",    callback_data="pubx_links_send"),
        InlineKeyboardButton("↩️ Volver",  callback_data="pubx_main"),
    ])
    return InlineKeyboardMarkup(rows)


def _build_pubx_rep_kb(sel: set) -> InlineKeyboardMarkup:
    rows = []
    row = []
    for net in _PUBX_NETS_REP:
        icon = "✅" if net in sel else "☐"
        row.append(InlineKeyboardButton(f"{icon} {_PUBX_NET_LABEL[net]}",
                                        callback_data=f"pubx_rt_{net}"))
        if len(row) == 2:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    rows.append([
        InlineKeyboardButton("📢 Publicar ahora",  callback_data="pubx_rep_now"),
        InlineKeyboardButton("⏰ Programar hora",  callback_data="pubx_rep_hora"),
    ])
    rows.append([InlineKeyboardButton("↩️ Volver", callback_data="pubx_main")])
    return InlineKeyboardMarkup(rows)


def _build_pubx_hora_kb() -> InlineKeyboardMarkup:
    hours = [8, 9, 10, 11, 12, 13, 14, 15, 16, 17, 18, 19, 20, 21]
    rows = []
    row = []
    for h in hours:
        row.append(InlineKeyboardButton(f"{h:02d}:00", callback_data=f"pubx_hora_{h:02d}"))
        if len(row) == 4:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    rows.append([InlineKeyboardButton("↩️ Volver", callback_data="pubx_rep")])
    return InlineKeyboardMarkup(rows)


def _build_pubx_del_kb(sel: set, meta: dict) -> InlineKeyboardMarkup:
    rows = []
    for net in _PUBX_NETS_DEL:
        avail = True
        if net == "telegram" and not meta.get("tg_msg"):
            avail = False
        elif net == "twitter" and not meta.get("tweet_id"):
            avail = False
        elif net == "linkedin" and not meta.get("li_urn"):
            avail = False
        icon   = "✅" if net in sel else "☐"
        suffix = "" if avail else " (N/A)"
        rows.append([InlineKeyboardButton(
            f"{icon} {_PUBX_NET_LABEL[net]}{suffix}",
            callback_data=f"pubx_dt_{net}",
        )])
    rows.append([
        InlineKeyboardButton("🗑️ Confirmar borrado", callback_data="pubx_del_confirm"),
        InlineKeyboardButton("↩️ Volver",            callback_data="pubx_main"),
    ])
    return InlineKeyboardMarkup(rows)


def _pubx_excerpt(post: dict) -> str:
    raw = post.get("content", "")
    clean = re.sub(r'<!--[^>]*-->', '', raw)
    clean = re.sub(r'<[^>]+>', ' ', clean)
    return re.sub(r'\s+', ' ', clean).strip()[:280]


async def _pubx_do_social(
    context: ContextTypes.DEFAULT_TYPE,
    post: dict,
    sel: set,
    chat_id: int | None = None,
) -> list:
    """Publica en las redes de `sel`. Devuelve lista de resultados."""
    data = await asyncio.to_thread(_post_to_data, post)
    results = []
    new_tg_msg   = 0
    new_tweet_id = ""

    if "telegram" in sel:
        msg_id = await publish_to_channel(context.bot, data, post["link"])
        if msg_id:
            new_tg_msg = msg_id
            results.append("✅ Publicado en canal Telegram")
        else:
            results.append("❌ Error publicando en Telegram")

    if "twitter" in sel:
        tw_url = await asyncio.to_thread(post_tweet, data, post["link"])
        if tw_url:
            new_tweet_id = tw_url.rsplit("/", 1)[-1]
            results.append(f"✅ Tweet: {tw_url}")
        else:
            err = get_last_twitter_error() or "(sin detalle)"
            results.append(f"❌ Error Twitter: {err[:120]}")

    if "linkedin" in sel:
        li_url = await asyncio.to_thread(post_linkedin, data, post["link"])
        results.append(f"✅ LinkedIn: {li_url}" if li_url else "❌ Error LinkedIn")

    for net in ("facebook", "instagram", "whatsapp"):
        if net not in sel:
            continue
        copy_text = _pubx_copy(net, post["title"], _pubx_excerpt(post),
                               post["link"].rstrip("/"), post["id"])
        target = chat_id or ADMIN_CHAT_ID
        if target:
            await context.bot.send_message(
                chat_id=target, text=copy_text,
                parse_mode="Markdown", disable_web_page_preview=True,
            )
        results.append(f"📋 Copy {_PUBX_NET_LABEL[net]} enviado al chat")

    if new_tg_msg or new_tweet_id:
        await asyncio.to_thread(
            append_social_meta,
            post["id"], post.get("content", ""), new_tweet_id, new_tg_msg,
        )

    return results


async def _pubx_fire_social(context: ContextTypes.DEFAULT_TYPE):
    """Job programado: dispara publicación en redes al horario indicado."""
    job_data  = context.job.data
    post      = job_data["post"]
    sel       = set(job_data["sel"])
    chat_id   = job_data.get("chat_id")

    results = await _pubx_do_social(context, post, sel, chat_id=chat_id)
    txt = "⏰ *Publicación programada ejecutada*\n\n" + "\n".join(results)
    if chat_id:
        await context.bot.send_message(chat_id=chat_id, text=txt, parse_mode="Markdown")


async def cmd_publicador(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Uso: /publicador <URL o ID de la nota>"""
    args = " ".join(context.args).strip()
    if not args:
        await update.message.reply_text(
            "Uso: /publicador <URL o ID>\n"
            "Ejemplo: /publicador https://mundoempresarial.ar/mi-nota/"
        )
        return

    msg = await update.message.reply_text("Buscando nota...")
    post = await asyncio.to_thread(find_post, args)
    if not post:
        await msg.edit_text("No encontré la nota. Verificá la URL o el ID.")
        return

    context.user_data["pubx_post"]      = post
    context.user_data["pubx_links_sel"] = set()
    context.user_data["pubx_rep_sel"]   = {"telegram"}
    context.user_data["pubx_del_sel"]   = set()

    await msg.edit_text(
        f"📄 *{md_escape(post['title'])}*\n{post['link']}",
        parse_mode="Markdown",
        reply_markup=_build_pubx_main_kb(),
    )


async def handle_pubx_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    d = query.data

    if d == "pubx_cancel":
        for k in ("pubx_post", "pubx_links_sel", "pubx_rep_sel", "pubx_del_sel"):
            context.user_data.pop(k, None)
        await query.edit_message_text("Menú cerrado.")
        return

    post = context.user_data.get("pubx_post")

    if d == "pubx_main":
        if not post:
            await query.edit_message_text("No hay nota activa. Usá /publicador <URL>")
            return
        await query.edit_message_text(
            f"📄 *{md_escape(post['title'])}*\n{post['link']}",
            parse_mode="Markdown",
            reply_markup=_build_pubx_main_kb(),
        )
        return

    if not post:
        await query.edit_message_text("No hay nota activa. Usá /publicador <URL>")
        return

    # ── Traer links ──────────────────────────────────────────────────────────

    if d == "pubx_links":
        sel = context.user_data.get("pubx_links_sel", set())
        await query.edit_message_text(
            "📋 *Traer links*\n\nSeleccioná las redes:",
            parse_mode="Markdown",
            reply_markup=_build_pubx_links_kb(sel),
        )
        return

    if d.startswith("pubx_lt_"):
        net = d[len("pubx_lt_"):]
        sel = context.user_data.get("pubx_links_sel", set()).copy()
        if net in sel:
            sel.discard(net)
        else:
            sel.add(net)
        context.user_data["pubx_links_sel"] = sel
        await query.edit_message_reply_markup(reply_markup=_build_pubx_links_kb(sel))
        return

    if d == "pubx_links_send":
        sel = context.user_data.get("pubx_links_sel", set())
        if not sel:
            await query.answer("Seleccioná al menos una red", show_alert=True)
            return
        title   = post["title"]
        url     = post["link"].rstrip("/")
        excerpt = _pubx_excerpt(post)
        errors  = []
        for net in _PUBX_NETS_LINKS:
            if net not in sel:
                continue
            copy_text = _pubx_copy(net, title, excerpt, url, post["id"])
            # telegram y wp tienen md_escape aplicado en _pubx_copy; el resto es copy-paste plano
            pm = "Markdown" if net in ("telegram", "wp") else None
            try:
                await query.message.reply_text(
                    copy_text, parse_mode=pm, disable_web_page_preview=True,
                )
            except Exception as e:
                errors.append(f"{_PUBX_NET_LABEL[net]}: {e}")
        nets_str = ", ".join(_PUBX_NET_LABEL[n] for n in _PUBX_NETS_LINKS if n in sel)
        status = f"✅ Links enviados: {nets_str}"
        if errors:
            status += "\n\n⚠️ " + " | ".join(errors)
        await query.edit_message_text(
            f"{status}\n\n_{md_escape(title)}_",
            parse_mode="Markdown",
        )
        return

    # ── Republicar ───────────────────────────────────────────────────────────

    if d == "pubx_rep":
        sel = context.user_data.get("pubx_rep_sel", {"telegram"})
        await query.edit_message_text(
            "🔁 *Republicar*\n\nSeleccioná redes:",
            parse_mode="Markdown",
            reply_markup=_build_pubx_rep_kb(sel),
        )
        return

    if d.startswith("pubx_rt_"):
        net = d[len("pubx_rt_"):]
        sel = context.user_data.get("pubx_rep_sel", set()).copy()
        if net in sel:
            sel.discard(net)
        else:
            sel.add(net)
        context.user_data["pubx_rep_sel"] = sel
        await query.edit_message_reply_markup(reply_markup=_build_pubx_rep_kb(sel))
        return

    if d == "pubx_rep_hora":
        await query.edit_message_text(
            "⏰ *Programar hora*\n\nSeleccioná la hora (ARG):",
            parse_mode="Markdown",
            reply_markup=_build_pubx_hora_kb(),
        )
        return

    if d.startswith("pubx_hora_"):
        sel = context.user_data.get("pubx_rep_sel", set())
        if not sel:
            await query.answer("Seleccioná al menos una red primero", show_alert=True)
            return
        hour = int(d.split("_")[-1])
        from datetime import timezone, timedelta
        tz_arg = timezone(timedelta(hours=-3))
        now_arg = datetime.now(tz_arg)
        target  = now_arg.replace(hour=hour, minute=0, second=0, microsecond=0)
        if target <= now_arg:
            from datetime import timedelta as td
            target = target + td(days=1)
        context.application.job_queue.run_once(
            _pubx_fire_social,
            when=target.astimezone(timezone.utc),
            data={"post": post, "sel": list(sel), "chat_id": query.message.chat_id},
            name=f"pubx_{post['id']}_{hour:02d}",
        )
        nets_str = ", ".join(_PUBX_NET_LABEL[n] for n in _PUBX_NETS_REP if n in sel)
        await query.edit_message_text(
            f"⏰ Programado para las *{hour:02d}:00 ARG*\n"
            f"Redes: {nets_str}\n\n_{md_escape(post['title'])}_",
            parse_mode="Markdown",
        )
        return

    if d == "pubx_rep_now":
        sel = context.user_data.get("pubx_rep_sel", set())
        if not sel:
            await query.answer("Seleccioná al menos una red", show_alert=True)
            return
        await query.edit_message_text("Publicando...")
        results = await _pubx_do_social(
            context, post, sel, chat_id=query.message.chat_id,
        )
        await query.edit_message_text("\n".join(results) or "Sin resultados.")
        return

    # ── Borrar ───────────────────────────────────────────────────────────────

    if d == "pubx_del":
        meta = parse_social_meta(post.get("content", ""))
        sel = context.user_data.get("pubx_del_sel") or set()
        if not sel:
            sel = {"wp"}
            if meta.get("tg_msg"):
                sel.add("telegram")
            if meta.get("tweet_id"):
                sel.add("twitter")
            if meta.get("li_urn"):
                sel.add("linkedin")
            context.user_data["pubx_del_sel"] = sel
        await query.edit_message_text(
            f"🗑️ *Borrar nota*\n\n_{md_escape(post['title'])}_\n\nSeleccioná de dónde borrar:",
            parse_mode="Markdown",
            reply_markup=_build_pubx_del_kb(sel, meta),
        )
        return

    if d.startswith("pubx_dt_"):
        net  = d[len("pubx_dt_"):]
        meta = parse_social_meta(post.get("content", ""))
        if net == "telegram" and not meta.get("tg_msg"):
            return
        if net == "twitter" and not meta.get("tweet_id"):
            return
        if net == "linkedin" and not meta.get("li_urn"):
            return
        sel = context.user_data.get("pubx_del_sel", set()).copy()
        if net in sel:
            sel.discard(net)
        else:
            sel.add(net)
        context.user_data["pubx_del_sel"] = sel
        await query.edit_message_reply_markup(reply_markup=_build_pubx_del_kb(sel, meta))
        return

    if d == "pubx_del_confirm":
        sel = context.user_data.get("pubx_del_sel", set())
        if not sel:
            await query.answer("Nada seleccionado", show_alert=True)
            return
        await query.edit_message_text("Borrando...")
        meta    = parse_social_meta(post.get("content", ""))
        results = []

        if "twitter" in sel and meta.get("tweet_id"):
            ok = await asyncio.to_thread(delete_tweet, meta["tweet_id"])
            results.append("✅ Tweet borrado" if ok else "❌ Error borrando tweet")

        if "telegram" in sel and meta.get("tg_msg"):
            try:
                ok = await delete_from_channel(context.bot, int(meta["tg_msg"]))
                results.append("✅ Mensaje TG borrado" if ok
                               else "❌ Error borrando del canal TG")
            except (ValueError, Exception) as e:
                results.append(f"❌ Error TG: {e}")

        if "linkedin" in sel and meta.get("li_urn"):
            ok = await asyncio.to_thread(delete_linkedin_post, meta["li_urn"])
            results.append("✅ LinkedIn borrado" if ok else "❌ Error borrando LinkedIn")

        if "wp" in sel:
            ok = await asyncio.to_thread(trash_post, post["id"])
            results.append("✅ WP → papelera" if ok else "❌ Error borrando de WP")

        for k in ("pubx_post", "pubx_links_sel", "pubx_rep_sel", "pubx_del_sel"):
            context.user_data.pop(k, None)

        await query.edit_message_text("\n".join(results) or "Nada que borrar.")
        return


def _apply_wm_to_featured(post: dict) -> bool:
    """Baja la featured actual del post, le aplica el watermark y la re-sube como destacada."""
    try:
        fm = post.get("featured_media") or 0
        if not fm:
            return False
        r = requests.get(f"{WP_URL}/wp-json/wp/v2/media/{fm}",
                         headers=wp_auth(), proxies=_WP_PROXIES, timeout=20)
        src = (r.json() or {}).get("source_url") if r.ok else None
        if not src:
            return False
        dl = requests.get(src, headers=HEADERS_BROWSER, timeout=20)
        if not dl.ok:
            return False
        import sys as _sw
        _sw.path.insert(0, "/opt/me-harness")
        from agents import marca_agua as _ma
        wm_bytes = _ma.aplicar_watermark(dl.content)
        kw = focus_keyword(post["title"])
        new_mid = upload_image_bytes(wm_bytes, "jpg", f"{kw} - {post['title']}")
        if not new_mid:
            return False
        return update_post(post["id"], {"featured_media": new_mid})
    except Exception as e:
        logger.error(f"_apply_wm_to_featured: {e}")
        return False


async def _handle_edit_photo_url(url: str, post: dict) -> bool:
    """Descarga imagen desde URL y la setea como destacada del post."""
    kw = focus_keyword(post["title"])
    alt = f"{kw} - {post['title']}"
    media_id = upload_image(url, alt, watermark=True)
    if not media_id:
        return False
    return update_post(post["id"], {"featured_media": media_id})


async def _handle_edit_photo_bytes(img_bytes: bytes, ctype: str, post: dict) -> bool:
    """Sube bytes de imagen a WordPress y la setea como destacada."""
    try:
        try:
            import sys as _sw
            _sw.path.insert(0, "/opt/me-harness")
            from agents import marca_agua as _ma
            img_bytes = _ma.aplicar_watermark(img_bytes)
            ctype = "image/jpeg"
        except Exception as _we:
            logger.warning(f"watermark edit_photo_bytes: {_we}")
        ext = ctype.split("/")[-1] if "/" in ctype else "jpg"
        h = {**wp_auth(), "Content-Disposition": f"attachment; filename=editada.{ext}",
             "Content-Type": ctype}
        r = requests.post(
            f"{WP_URL}/wp-json/wp/v2/media", headers=h, data=img_bytes,
            proxies=_WP_PROXIES, timeout=60
        )
        if not r.ok:
            logger.error(f"Upload photo: {r.status_code} {r.text[:200]}")
            return False
        media_id = r.json()["id"]

        kw = focus_keyword(post["title"])
        alt = f"{kw} - {post['title']}"
        requests.post(
            f"{WP_URL}/wp-json/wp/v2/media/{media_id}",
            headers={**wp_auth(), "Content-Type": "application/json"},
            json={"alt_text": alt, "caption": alt},
            proxies=_WP_PROXIES, timeout=10,
        )
        return update_post(post["id"], {"featured_media": media_id})
    except Exception as e:
        logger.error(f"edit_photo_bytes: {e}")
        return False


async def handle_photo_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Maneja foto enviada por Telegram durante edición o flujo sin_imagen."""

    # ── /vivo: foto de fondo para la placa de la cobertura ───────────────────
    if context.user_data.get("awaiting_vv_foto"):
        context.user_data.pop("awaiting_vv_foto", None)
        await _vv_capturar_foto(update, context)
        return

    # ── /encuesta: foto manual de la portada ─────────────────────────────────
    if context.user_data.get("awaiting_enc_foto"):
        context.user_data.pop("awaiting_enc_foto")
        fp = context.user_data.get("enc")
        if not fp:
            await update.message.reply_text("⚠️ Se perdió la encuesta. Reiniciá con /encuesta."); return
        msg = await update.message.reply_text("⏳ Subiendo la foto…")
        try:
            photo = update.message.photo[-1]
            file = await photo.get_file(read_timeout=30, connect_timeout=15)
            import requests as _rq_enc
            dl = await asyncio.to_thread(lambda: _rq_enc.get(file.file_path, timeout=30))
            mid = await asyncio.to_thread(upload_image_bytes, dl.content, "jpg")
        except Exception as e:
            await msg.edit_text(f"❌ No pude subir la foto: {e}"); return
        if not mid:
            await msg.edit_text("❌ No pude subir la foto. Reintentá o elegí automática."); return
        fp["image_id"] = mid
        await msg.edit_text(f"✅ Foto cargada (#{mid}).")
        await update.message.reply_text("Elegí redes y acción:", reply_markup=_build_enc_kb(fp))
        return

    # ── /input_evento: foto de una tanda EN VIVO (al inbox, con pie) ─────────
    if context.user_data.get("input_evento") is not None:
        await _iev_foto(update, context)
        return

    # ── /evento: foto del evento (va a la portada) ───────────────────────────
    _ev = context.user_data.get("evento")
    if _ev is not None:
        msg = await update.message.reply_text("⏳ Subiendo la foto del evento a WordPress…")
        try:
            photo = update.message.photo[-1]
            file = await photo.get_file(read_timeout=30, connect_timeout=15)
            import requests as _rq_ev
            dl = await asyncio.to_thread(lambda: _rq_ev.get(file.file_path, timeout=30))
            mid = await asyncio.to_thread(upload_image_bytes, dl.content, "jpg")
        except Exception as e:
            await msg.edit_text(f"❌ No pude subir la foto: {e}")
            return
        if not mid:
            await msg.edit_text("❌ No pude subir la foto a WP. Probá de nuevo.")
            return
        _ev["fotos"].append(mid)
        await msg.edit_text(f"✅ Foto del evento cargada (#{mid}).")
        await update.message.reply_text(
            _evento_resumen(_ev), parse_mode="HTML", reply_markup=_evento_kb())
        return

    # ── Flujo sin_imagen: Leo envía foto para una nota sin imagen ────────────
    _wmf_job = context.user_data.get("awaiting_wm_foto_for")
    if _wmf_job:
        context.user_data.pop("awaiting_wm_foto_for", None)
        msg = await update.message.reply_text("⏳ Subiendo la foto (con watermark)…")
        try:
            photo = update.message.photo[-1]
            file  = await photo.get_file(read_timeout=30, connect_timeout=15)
            import requests as _rq_wf
            dl = await asyncio.to_thread(lambda: _rq_wf.get(file.file_path, timeout=30))
            import sys as _sy_wf, json as _js_wf, sqlite3 as _sq_wf
            _sy_wf.path.insert(0, "/opt/me-harness")
            import broker as _br_wf
            _job = _br_wf.get_job(_wmf_job)
            _cj = {}
            try: _cj = _js_wf.loads(_job.get("content_json") or "{}")
            except Exception: pass
            _wmon = not _cj.get("sin_watermark", False)
            mid = await asyncio.to_thread(upload_image_bytes, dl.content, "jpg", "", _wmon)
        except Exception as e:
            await msg.edit_text(f"❌ No pude subir la foto: {e}")
            return
        if not mid:
            await msg.edit_text("❌ No pude subir la foto. Reintentá.")
            return
        _cj["image_id_override"] = mid
        with _sq_wf.connect("/opt/me-harness/harness.db") as _c_wf:
            _c_wf.execute("UPDATE jobs SET content_json=? WHERE id=?", (_js_wf.dumps(_cj), _wmf_job))
        await msg.edit_text(f"✅ Foto cargada (#{mid}) para la nota #{_wmf_job}" + (" con watermark." if _wmon else " sin watermark."))
        return

    job_id_photo = context.user_data.get("awaiting_img_url_for")
    if job_id_photo:
        context.user_data.pop("awaiting_img_url_for", None)
        context.user_data.pop("awaiting_img_msg_id", None)
        msg = await update.message.reply_text("⏳ Subiendo foto a WordPress...")
        try:
            photo = update.message.photo[-1]
            file  = await photo.get_file(read_timeout=30, connect_timeout=15)
            # Descargar via requests con timeout explícito (más confiable que download_as_bytearray)
            import requests as _req_ph
            _dl = await asyncio.to_thread(
                lambda: _req_ph.get(file.file_path, timeout=30)
            )
            img_bytes = _dl.content
            media_id  = await asyncio.to_thread(upload_image_bytes, img_bytes, "jpg")
            if media_id:
                import sys as _sys_ph, json as _js_ph, sqlite3 as _sq_ph
                _sys_ph.path.insert(0, "/opt/me-harness")
                import broker as _br_ph
                job_i = _br_ph.get_job(job_id_photo)
                cj_i = {}
                try: cj_i = _js_ph.loads(job_i.get("content_json") or "{}")
                except Exception: pass
                cj_i["image_id_override"] = media_id
                with _sq_ph.connect("/opt/me-harness/harness.db") as _c_ph:
                    _c_ph.execute(
                        "UPDATE jobs SET stage='publicacion', content_json=?, updated_at=datetime('now') WHERE id=?",
                        (_js_ph.dumps(cj_i), job_id_photo)
                    )
                await msg.edit_text(
                    f"✅ <b>Foto subida a WP (#{media_id})</b>\nNota #{job_id_photo} vuelve a publicación.",
                    parse_mode="HTML"
                )
            else:
                await msg.edit_text(
                    "❌ No pude subir la foto a WP.",
                    reply_markup={"inline_keyboard": [[
                        {"text": "🔗 Agregar URL",      "callback_data": f"h_img_url:{job_id_photo}"},
                        {"text": "➖ Publicar sin foto", "callback_data": f"h_img_skip:{job_id_photo}"},
                    ]]}
                )
        except Exception as e:
            logger.error(f"handle_photo sin_imagen: {e}")
            await msg.edit_text(f"❌ Error: {e}")
        return

    # ── Flujo edición de nota existente ─────────────────────────────────────
    if not context.user_data.get("waiting_for_edit_photo"):
        return
    post = context.user_data.get("edit_post")
    if not post:
        await update.message.reply_text("No hay nota en edición.")
        return

    context.user_data["waiting_for_edit_photo"] = False
    msg = await update.message.reply_text("Subiendo foto...")

    try:
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


# ── Curador diario (RSS + scoring + briefing) ────────────────────────────────

KW_PYME = [
    # Información útil (Hilo 1)
    "afip", "arca", "monotributo", "monotributista", "iva", "ganancias",
    "moratoria", "blanqueo", "régimen simplificado", "factura electrónica",
    "paritaria", "convenio colectivo", "sueldo", "jubilación", "anses",
    "vencimiento", "plazo", "declaración jurada", "tarifa",
    # Macro económica
    "dólar", "dolar", "inflación", "tasa de interés", "bcra", "cepo",
    "tipo de cambio", "reservas", "fmi", "deuda", "bonos", "riesgo país",
    # Pymes / empresariado
    "pyme", "pymes", "emprendedor", "empresario", "empresa", "industria",
    "industrial", "fábrica", "cámara empresaria", "came", "enac",
    "parque industrial", "empleo", "despidos",
    # Sectores
    "agro", "campo", "exportación", "importación", "comercio", "retail",
    "construcción", "automotriz", "textil", "minería", "energía",
    "vaca muerta", "litio", "vitivinicultura",
    # Digitalización pyme
    "fintech", "ecommerce", "startup argentina", "transformación digital",
    "ciberseguridad empresarial",
    # Política económica
    "milei", "caputo", "kicillof", "ministerio de economía", "producción",
    "desregulación", "rigi", "ley bases", "reforma laboral", "reforma tributaria",
]


# ── Sistema de feedback / aprendizaje del curador ─────────────────────────────
# Persiste en WordPress como post privado (sobrevive redeploys de Railway).

_FEEDBACK_CACHE: dict | None = None
_FEEDBACK_POST_ID: int | None = None
_FEEDBACK_POST_SLUG = "mebot-feedback-store"


def _find_or_create_feedback_post() -> int | None:
    """Busca el post privado donde vive el feedback store. Si no existe, lo crea."""
    global _FEEDBACK_POST_ID
    if _FEEDBACK_POST_ID:
        return _FEEDBACK_POST_ID

    h = wp_auth()
    # Buscar por slug
    try:
        r = requests.get(
            f"{WP_URL}/wp-json/wp/v2/posts?slug={_FEEDBACK_POST_SLUG}&status=private,draft",
            headers=h, proxies=_WP_PROXIES, timeout=15,
        )
        if r.status_code == 200 and r.json():
            _FEEDBACK_POST_ID = r.json()[0]["id"]
            logger.info(f"Feedback post encontrado: ID {_FEEDBACK_POST_ID}")
            return _FEEDBACK_POST_ID
    except Exception as e:
        logger.warning(f"Buscando feedback post: {e}")

    # Crear
    try:
        payload = {
            "title": "MEBot Feedback Store",
            "slug": _FEEDBACK_POST_SLUG,
            "status": "private",
            "content": "{}",
            "excerpt": "Storage interno del bot. No tocar manualmente.",
        }
        r = requests.post(
            f"{WP_URL}/wp-json/wp/v2/posts",
            headers={**h, "Content-Type": "application/json"},
            json=payload, proxies=_WP_PROXIES, timeout=20,
        )
        if r.status_code == 201:
            _FEEDBACK_POST_ID = r.json()["id"]
            logger.info(f"Feedback post creado: ID {_FEEDBACK_POST_ID}")
            return _FEEDBACK_POST_ID
        logger.error(f"Crear feedback post {r.status_code}: {r.text[:200]}")
    except Exception as e:
        logger.error(f"Crear feedback post: {e}")
    return None


def _default_feedback() -> dict:
    return {
        "version": 1,
        "updated_at": "",
        "domain_weights":  {},  # {domain: int}
        "keyword_weights": {},  # {kw: int}
        "hilo_hints":      {},  # {kw: 1|2|3} — keywords que sugieren un hilo específico
        "interactions":    [],  # últimas 100 acciones para debug
        "scheduled_jobs":  [],  # posts programados pendientes (para recovery post-redeploy)
    }


def _add_scheduled_job(post_id: int, post_url: str, run_at,
                       data: dict, user_data: dict, post_content: str):
    """Agrega un job programado al feedback store."""
    fb = _load_feedback()
    fb.setdefault("scheduled_jobs", []).append({
        "post_id":      post_id,
        "post_url":     post_url,
        "run_at":       run_at.isoformat(),
        "data": {
            "title":             data.get("title", ""),
            "original_title":    data.get("original_title", ""),
            "excerpt":           data.get("excerpt", ""),
            "original_excerpt":  data.get("original_excerpt", ""),
            "rewritten_excerpt": data.get("rewritten_excerpt", ""),
            "image_url":         data.get("image_url", ""),
            "source_url":        data.get("source_url", ""),
            "is_youtube":        data.get("is_youtube", False),
            "youtube_video_id":  data.get("youtube_video_id", ""),
            "title_edited":      data.get("title_edited", False),
            "excerpt_edited":    data.get("excerpt_edited", False),
            "orig_title_on":     user_data.get("orig_title_on", False),
            "orig_excerpt_on":   user_data.get("orig_excerpt_on", False),
        },
        "tw_on":           user_data.get("tw_on", True),
        "tg_on":           user_data.get("tg_on", True),
        "wa_on":           user_data.get("wa_on", False),
        "li_on":           user_data.get("li_on", False),
        "custom_hashtags": user_data.get("pre_sched_hashtags"),
        "post_content":    "",
    })
    _save_feedback(fb)


def _remove_scheduled_job(post_id: int):
    """Saca un job del store (cuando ya se ejecutó)."""
    fb = _load_feedback()
    fb["scheduled_jobs"] = [
        j for j in fb.get("scheduled_jobs", []) if j.get("post_id") != post_id
    ]
    _save_feedback(fb)


async def _fire_scheduled_social(context: ContextTypes.DEFAULT_TYPE):
    """Callback de job_queue.run_once: dispara canal TG + tweet automático."""
    if BOT_PAUSED:
        logger.info("_fire_scheduled_social: bot pausado, saltando job")
        return
    job_data = context.job.data
    data = job_data.get("data", {})
    post_url = job_data.get("post_url", "")
    post_id = job_data.get("post_id")
    chat_id = job_data.get("chat_id")
    tg_on = job_data.get("tg_on", True)
    tw_on = job_data.get("tw_on", True)
    li_on = job_data.get("li_on", False)

    results = [f"🔔 Nota programada publicada: {post_url}"]

    # Canal TG
    tg_msg_id = 0
    if tg_on:
        tg_msg_id = await publish_to_channel(context.bot, data, post_url)
        results.append("✅ Canal TG" if tg_msg_id else "❌ Canal TG falló")

    # Twitter
    tweet_id = ""
    if tw_on:
        custom_ht = job_data.get("custom_hashtags")
        tweet_url = await asyncio.to_thread(post_tweet, data, post_url, custom_ht)
        if tweet_url:
            tweet_id = tweet_url.split("/")[-1]
            results.append(f"✅ Twitter: {tweet_url}")
        else:
            err = _LAST_TWITTER_ERROR or "error desconocido"
            results.append(f"❌ Twitter falló: {err[:120]}")

    # LinkedIn
    li_urn_sched = ""
    if li_on:
        li_url = await asyncio.to_thread(post_linkedin, data, post_url)
        if li_url:
            results.append(f"✅ LinkedIn: {li_url}")
            li_urn_sched = _LAST_LINKEDIN_URN
        else:
            err = _LAST_LINKEDIN_ERROR or "error desconocido"
            results.append(f"❌ LinkedIn: {err[:120]}")

    # Persistir tweet_id, tg_msg_id y li_urn en el post
    if (tweet_id or tg_msg_id or li_urn_sched) and post_id:
        await asyncio.to_thread(
            append_social_meta, post_id, job_data.get("post_content", ""),
            tweet_id, tg_msg_id, li_urn_sched,
        )

    if chat_id:
        await context.bot.send_message(chat_id=int(chat_id), text="\n".join(results))

    # Remover del store
    if post_id:
        await asyncio.to_thread(_remove_scheduled_job, post_id)


async def _do_eco_schedule(query, context, eco: dict, target):
    """Programa el ECO social en job_queue."""
    eco_d = _eco_data_with_alt(eco)
    job_data = {
        "eco":     eco,
        "eco_d":   eco_d,
        "chat_id": query.message.chat_id,
    }
    try:
        context.application.job_queue.run_once(
            _fire_eco_social,
            when=target,
            data=job_data,
            name=f"eco_social_{eco.get('post_id', 'x')}_{int(target.timestamp())}",
        )
    except Exception as e:
        logger.error(f"eco run_once falló: {e}")
    context.user_data.pop("eco", None)
    context.user_data.pop("eco_sched_day", None)
    await query.edit_message_text(
        f"📣 *ECO programado* para {target.strftime('%A %d/%m a las %H:%M')} ARG\n\n"
        f"🔗 {eco['wp_url']}",
        parse_mode="Markdown",
    )


async def _fire_eco_social(context: ContextTypes.DEFAULT_TYPE):
    """Dispara el ECO: publica en redes con alt_title/alt_bajada."""
    job_data = context.job.data
    eco  = job_data.get("eco", {})
    eco_d = job_data.get("eco_d", {})
    chat_id = job_data.get("chat_id")
    results = [f"📣 *ECO publicado*\n🔗 {eco.get('wp_url', '')}"]
    if eco.get("tg_on", True):
        tg_id = await publish_to_channel(context.bot, eco_d, eco.get("wp_url", ""))
        results.append("✅ Canal TG" if tg_id else "❌ Canal TG falló")
    if eco.get("tw_on", True):
        tw_url = await asyncio.to_thread(post_tweet, eco_d, eco.get("wp_url", ""))
        if tw_url:
            results.append(f"✅ Twitter: {tw_url}")
        else:
            err = _LAST_TWITTER_ERROR or "error desconocido"
            results.append(f"❌ Twitter: {err[:100]}")
    if eco.get("li_on", False):
        li_url = await asyncio.to_thread(post_linkedin, eco_d, eco.get("wp_url", ""))
        if li_url:
            results.append(f"✅ LinkedIn: {li_url}")
        else:
            err = _LAST_LINKEDIN_ERROR or "error desconocido"
            results.append(f"❌ LinkedIn: {err[:100]}")
    if chat_id:
        await context.bot.send_message(chat_id=int(chat_id), text="\n".join(results), parse_mode="Markdown")


def _restore_scheduled_jobs(app):
    """Al iniciar el bot, re-registra jobs programados cuya hora aún no pasó."""
    from datetime import datetime, timezone, timedelta
    tz_arg = timezone(timedelta(hours=-3))

    try:
        fb = _load_feedback()
        pending = fb.get("scheduled_jobs", [])
    except Exception as e:
        logger.warning(f"Restore scheduled jobs: {e}")
        return

    if not pending:
        return

    restored = 0
    expired = 0
    for job in pending:
        try:
            run_at = datetime.fromisoformat(job["run_at"])
            if run_at.tzinfo is None:
                run_at = run_at.replace(tzinfo=tz_arg)
        except Exception:
            continue

        if run_at <= datetime.now(tz_arg):
            # Ya pasó — lo sacamos del store sin ejecutar (el WP post ya se publicó solo)
            expired += 1
            continue

        try:
            app.job_queue.run_once(
                _fire_scheduled_social,
                when=run_at,
                data={
                    "post_id":         job["post_id"],
                    "post_url":        job["post_url"],
                    "post_content":    job.get("post_content", ""),
                    "data":            job["data"],
                    "tw_on":           job.get("tw_on", True),
                    "tg_on":           job.get("tg_on", True),
                    "wa_on":           job.get("wa_on", False),
                    "li_on":           job.get("li_on", False),
                    "custom_hashtags": job.get("custom_hashtags"),
                    "chat_id":         int(ADMIN_CHAT_ID) if ADMIN_CHAT_ID else None,
                },
                name=f"sched_social_{job['post_id']}",
            )
            restored += 1
        except Exception as e:
            logger.warning(f"No pude re-registrar job {job.get('post_id')}: {e}")

    # Limpiar expirados
    if expired:
        fb["scheduled_jobs"] = [
            j for j in pending
            if datetime.fromisoformat(j["run_at"]).replace(
                tzinfo=tz_arg if datetime.fromisoformat(j["run_at"]).tzinfo is None else None
            ) > datetime.now(tz_arg)
        ]
        _save_feedback(fb)

    logger.info(f"Scheduled jobs restaurados: {restored} pendientes, {expired} expirados")


def _load_feedback() -> dict:
    """Lee el feedback desde WP. Cacheado en memoria."""
    global _FEEDBACK_CACHE
    if _FEEDBACK_CACHE is not None:
        return _FEEDBACK_CACHE

    post_id = _find_or_create_feedback_post()
    if not post_id:
        _FEEDBACK_CACHE = _default_feedback()
        return _FEEDBACK_CACHE

    try:
        r = requests.get(
            f"{WP_URL}/wp-json/wp/v2/posts/{post_id}?context=edit",
            headers=wp_auth(), proxies=_WP_PROXIES, timeout=15,
        )
        if r.status_code == 200:
            raw_content = r.json().get("content", {}).get("raw", "") or ""
            # El content puede estar envuelto en <p>...</p> por WP
            clean = re.sub(r'<[^>]+>', '', raw_content).strip()
            if clean:
                data = json.loads(clean)
                _FEEDBACK_CACHE = {**_default_feedback(), **data}
            else:
                _FEEDBACK_CACHE = _default_feedback()
        else:
            _FEEDBACK_CACHE = _default_feedback()
    except Exception as e:
        logger.warning(f"Load feedback: {e}")
        _FEEDBACK_CACHE = _default_feedback()
    return _FEEDBACK_CACHE


def _save_feedback(data: dict) -> bool:
    """Guarda el feedback store en WP."""
    global _FEEDBACK_CACHE
    from datetime import datetime
    data["updated_at"] = datetime.utcnow().isoformat() + "Z"
    # Truncar interactions a las últimas 200
    if len(data.get("interactions", [])) > 200:
        data["interactions"] = data["interactions"][-200:]
    _FEEDBACK_CACHE = data

    post_id = _find_or_create_feedback_post()
    if not post_id:
        return False
    try:
        payload = {"content": json.dumps(data, ensure_ascii=False)}
        r = requests.post(
            f"{WP_URL}/wp-json/wp/v2/posts/{post_id}",
            headers={**wp_auth(), "Content-Type": "application/json"},
            json=payload, proxies=_WP_PROXIES, timeout=20,
        )
        if r.status_code not in (200, 201):
            logger.error(f"Save feedback {r.status_code}: {r.text[:200]}")
        return r.status_code in (200, 201)
    except Exception as e:
        logger.error(f"Save feedback: {e}")
        return False


def _title_keywords(title: str) -> list[str]:
    """Extrae keywords significativas del título (sin stop-words, >3 chars, lowercase)."""
    clean = re.sub(r'[^\w\sáéíóúñ]', ' ', (title or "").lower())
    return [
        w for w in clean.split()
        if len(w) > 3 and w not in STOP_WORDS
    ]


async def cmd_ingesta(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Dispara la ingesta RSS manualmente y reporta cuántas notas encoló."""
    msg = await update.message.reply_text("📡 Corriendo ingesta RSS...")
    import sys as _sys
    _sys.path.insert(0, "/opt/me-harness")
    try:
        from agents import ingesta as _ing
        n = await asyncio.to_thread(_ing.run)
        await msg.edit_text(
            f"✅ <b>Ingesta completada</b>\n"
            f"{n} notas nuevas encoladas para el Curador.\n"
            f"Usá /briefing para ver el briefing.",
            parse_mode="HTML"
        )
    except Exception as e:
        await msg.edit_text(f"⚠️ Error en ingesta: {e}")


async def cmd_eventos(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Agenda del agente EVENTOS hacia adelante: boletín semanal (lunes) + efemérides del
    calendario. Antes el agente solo avisaba cuando algo entraba en ventana; ahora se le pregunta."""
    import sys as _syse
    _syse.path.insert(0, "/opt/me-harness"); _syse.path.insert(0, "/opt/me-harness/agents")

    def _get():
        import eventos as _ev
        return _ev.proximos(8)

    try:
        evs = await asyncio.wait_for(asyncio.to_thread(_get), timeout=30)
    except Exception as e:
        await update.message.reply_text(f"⚠️ No pude leer la agenda: {str(e)[:150]}")
        return
    if not evs:
        await update.message.reply_text("🗓 No hay nada en la agenda.")
        return
    _EMO = {"boletin": "📬", "efemeride": "📅"}
    lines = ["🗓 <b>Agenda del agente EVENTOS</b>\n"]
    for e in evs:
        d = e.get("dias", 0)
        cuando = "HOY" if d == 0 else ("mañana" if d == 1 else f"en {d} días")
        fecha = e.get("fecha", "")
        f = f"{fecha[8:10]}/{fecha[5:7]}" if len(fecha) >= 10 else fecha
        lines.append(f"{_EMO.get(e.get('tipo'), '•')} <b>{e.get('nombre')}</b> — {f} ({cuando})\n"
                     f"    <i>{e.get('segmento')}</i> · {e.get('estado')}")
    lines.append("\n<i>Notas con slot futuro → /programadas</i>")
    await update.message.reply_text(
        "\n".join(lines), parse_mode="HTML", disable_web_page_preview=True,
        reply_markup={"inline_keyboard": [[
            {"text": "➕ Nueva efeméride", "callback_data": "efem_new"},
            {"text": "✏️ Editar / borrar", "callback_data": "efem_list"}]]})


# ── Panel para cargar una efeméride al calendario del agente de eventos ────────
# "todo el set de botones para armar el evento con sus características" (Leo 30/7).
# Mismo patrón mutex que /notamanual: un flag awaiting a la vez, panel que se redibuja.
# El save escribe en eventos_calendario.json vía eventos.add_evento() (harness).
_EFEM_ESPERAS = ("awaiting_efem_nombre", "awaiting_efem_fecha",
                 "awaiting_efem_angulo", "awaiting_efem_segmento")

_EFEM_SEGMENTOS = ["empleadores y RRHH pyme", "contadores y estudios contables",
                   "industriales y fabricantes", "comerciantes y retail",
                   "empresarias y emprendedoras", "profesionales", "toda la base"]


def _efem_espera(context, flag: str = None):
    """El bot espera UNA sola cosa a la vez (evita que un flag viejo capture el próximo texto)."""
    for f in _EFEM_ESPERAS:
        context.user_data.pop(f, None)
    if flag:
        context.user_data[flag] = True


def _efem_panel_text(d: dict) -> str:
    import html as _h
    def e(s): return _h.escape(str(s or ""))
    _f = d.get("fecha") or ""
    _fd = f"{_f[3:5]}/{_f[0:2]}" if len(_f) >= 5 else "—"
    _tit = "Editar efeméride" if d.get("_edit_id") else "Nueva efeméride"
    return (
        f"🗓️ <b>{_tit}</b>\n\n"
        f"📛 <b>Nombre:</b> {e(d.get('nombre')) or '—'}\n"
        f"📅 <b>Fecha:</b> {_fd}\n"
        f"🎯 <b>Segmento:</b> {e(d.get('segmento')) or '—'}\n"
        f"💬 <b>Ángulo:</b> {e(d.get('angulo')) or '—'}\n"
        f"⏰ <b>Anticipación:</b> {d.get('anticipacion', 5)} días\n"
        f"💪 <b>Esfuerzo:</b> {e(d.get('esfuerzo') or 'media')}\n\n"
        "Cargá los campos y tocá ✅ Guardar. <i>Nombre y fecha son obligatorios; "
        "el ángulo es lo que más ayuda a los agentes a cubrirla bien.</i>")


def _efem_kb(d: dict) -> dict:
    rows = [
        [{"text": "✏️ Nombre",  "callback_data": "efem_nombre"},
         {"text": "📅 Fecha",   "callback_data": "efem_fecha"}],
        [{"text": "🎯 Segmento", "callback_data": "efem_segmento"},
         {"text": "💬 Ángulo",  "callback_data": "efem_angulo"}],
        [{"text": "⏰ Anticipación", "callback_data": "efem_anticip"},
         {"text": "💪 Esfuerzo",     "callback_data": "efem_esfuerzo"}],
        [{"text": "✅ Guardar",  "callback_data": "efem_save"},
         {"text": "✖️ Cancelar", "callback_data": "efem_cancel"}]]
    if d.get("_edit_id"):
        rows.append([{"text": "🗑 Borrar esta efeméride", "callback_data": "efem_del"}])
    return {"inline_keyboard": rows}


async def _efem_redraw(context, d: dict, query=None):
    """Redibuja el panel en su lugar tras cada cambio."""
    try:
        if query is not None:
            await query.edit_message_text(_efem_panel_text(d), parse_mode="HTML",
                                          reply_markup=_efem_kb(d))
            d["_chat"] = query.message.chat_id
            d["_msg"] = query.message.message_id
            return
        if d.get("_chat") and d.get("_msg"):
            await context.bot.edit_message_text(
                chat_id=d["_chat"], message_id=d["_msg"], text=_efem_panel_text(d),
                parse_mode="HTML", reply_markup=_efem_kb(d))
    except Exception:
        pass


async def handle_efem_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    data = q.data

    if data == "efem_new":
        d = {"nombre": "", "fecha": "", "segmento": "", "angulo": "",
             "anticipacion": 5, "esfuerzo": "media"}
        context.user_data["efem"] = d
        _efem_espera(context)
        await q.edit_message_text(_efem_panel_text(d), parse_mode="HTML",
                                  reply_markup=_efem_kb(d))
        d["_chat"] = q.message.chat_id
        d["_msg"] = q.message.message_id
        return

    if data == "efem_list":
        import sys as _syse
        _syse.path.insert(0, "/opt/me-harness"); _syse.path.insert(0, "/opt/me-harness/agents")

        def _list():
            import eventos as _ev
            return _ev.listar_efemerides()

        try:
            efs = await asyncio.wait_for(asyncio.to_thread(_list), timeout=30)
        except Exception as e:
            await q.edit_message_text(f"⚠️ No pude leer el calendario: {str(e)[:150]}")
            return
        if not efs:
            await q.edit_message_text("No hay efemérides cargadas todavía.")
            return
        rows = []
        for ef in efs:
            _f = ef.get("fecha") or ""
            _fd = f"{_f[3:5]}/{_f[0:2]}" if len(_f) >= 5 else _f
            rows.append([{"text": f"{(ef.get('nombre') or '?')[:38]} ({_fd})",
                          "callback_data": f"efem_edit:{ef['id']}"}])
        await q.edit_message_text("✏️ Elegí la efeméride a editar o borrar:",
                                  reply_markup={"inline_keyboard": rows})
        return

    if data.startswith("efem_edit:"):
        _eid = data.split(":", 1)[1]
        import sys as _syse
        _syse.path.insert(0, "/opt/me-harness"); _syse.path.insert(0, "/opt/me-harness/agents")

        def _load():
            import eventos as _ev
            return _ev.get_evento(_eid)

        try:
            ev = await asyncio.wait_for(asyncio.to_thread(_load), timeout=30)
        except Exception as e:
            await q.edit_message_text(f"⚠️ No pude cargarla: {str(e)[:150]}")
            return
        if not ev:
            await q.edit_message_text("Esa efeméride ya no existe.")
            return
        d = {"nombre": ev.get("nombre", ""), "fecha": ev.get("fecha", ""),
             "segmento": ev.get("segmento", ""), "angulo": ev.get("angulo", ""),
             "anticipacion": ev.get("anticipacion_dias", 5),
             "esfuerzo": ev.get("esfuerzo_sugerido", "media"), "_edit_id": _eid}
        context.user_data["efem"] = d
        _efem_espera(context)
        await q.edit_message_text(_efem_panel_text(d), parse_mode="HTML",
                                  reply_markup=_efem_kb(d))
        d["_chat"] = q.message.chat_id
        d["_msg"] = q.message.message_id
        return

    d = context.user_data.get("efem")
    if d is None:
        await q.edit_message_text("Esta tarjeta expiró. Abrí /eventos y tocá ➕ de nuevo.")
        return

    if data == "efem_cancel":
        context.user_data.pop("efem", None)
        _efem_espera(context)
        await q.edit_message_text("✖️ Cancelado.")
        return

    if data == "efem_del":
        _eid = d.get("_edit_id")
        if not _eid:
            await q.message.reply_text("Esta efeméride todavía no está guardada.")
            return
        import sys as _syse
        _syse.path.insert(0, "/opt/me-harness"); _syse.path.insert(0, "/opt/me-harness/agents")

        def _del():
            import eventos as _ev
            return _ev.del_evento(_eid)

        try:
            ok = await asyncio.wait_for(asyncio.to_thread(_del), timeout=30)
        except Exception as e:
            await q.edit_message_text(f"⚠️ No pude borrar: {str(e)[:150]}")
            return
        context.user_data.pop("efem", None)
        _efem_espera(context)
        await q.edit_message_text(
            "🗑 Efeméride borrada." if ok else "No la encontré (¿ya no estaba?).")
        return

    if data in ("efem_nombre", "efem_fecha", "efem_angulo"):
        campo = data.split("_")[1]
        _efem_espera(context, f"awaiting_efem_{campo}")
        _prompts = {"nombre": "Escribí el NOMBRE de la efeméride.",
                    "fecha": "Escribí la FECHA (DD/MM, ej: 29/07).",
                    "angulo": "Escribí el ÁNGULO editorial: con qué mirada cubrirla."}
        await q.message.reply_text(_prompts[campo])
        return

    if data == "efem_segmento":
        rows = [[{"text": s, "callback_data": f"efem_seg:{i}"}]
                for i, s in enumerate(_EFEM_SEGMENTOS)]
        rows.append([{"text": "✏️ Otro (escribir)", "callback_data": "efem_seg:otro"},
                     {"text": "⬅️ Volver", "callback_data": "efem_back"}])
        await q.edit_message_text("🎯 Elegí el segmento:",
                                  reply_markup={"inline_keyboard": rows})
        return
    if data.startswith("efem_seg:"):
        val = data.split(":", 1)[1]
        if val == "otro":
            _efem_espera(context, "awaiting_efem_segmento")
            await q.message.reply_text("Escribí el SEGMENTO (a quién apunta).")
            return
        d["segmento"] = _EFEM_SEGMENTOS[int(val)]
        await _efem_redraw(context, d, q)
        return

    if data == "efem_anticip":
        rows = [[{"text": f"{n} días", "callback_data": f"efem_ant:{n}"} for n in (3, 5, 7, 10)],
                [{"text": "⬅️ Volver", "callback_data": "efem_back"}]]
        await q.edit_message_text("⏰ ¿Con cuántos días de anticipación te aviso?",
                                  reply_markup={"inline_keyboard": rows})
        return
    if data.startswith("efem_ant:"):
        d["anticipacion"] = int(data.split(":")[1])
        await _efem_redraw(context, d, q)
        return

    if data == "efem_esfuerzo":
        rows = [[{"text": "Mínima", "callback_data": "efem_esf:minima"},
                 {"text": "Media", "callback_data": "efem_esf:media"},
                 {"text": "Máxima", "callback_data": "efem_esf:maxima"}],
                [{"text": "⬅️ Volver", "callback_data": "efem_back"}]]
        await q.edit_message_text(
            "💪 Nivel de esfuerzo de la campaña:\n"
            "• Mínima: 3 noticias → newsletter\n"
            "• Media: + opinión curada, intro y copy de redes\n"
            "• Máxima: + una columna propia (te pide insumos)",
            reply_markup={"inline_keyboard": rows})
        return
    if data.startswith("efem_esf:"):
        d["esfuerzo"] = data.split(":")[1]
        await _efem_redraw(context, d, q)
        return

    if data == "efem_back":
        await _efem_redraw(context, d, q)
        return

    if data == "efem_save":
        if not d.get("nombre") or not d.get("fecha"):
            await q.message.reply_text("Faltan el nombre o la fecha (obligatorios).")
            return
        import sys as _syse
        _syse.path.insert(0, "/opt/me-harness"); _syse.path.insert(0, "/opt/me-harness/agents")

        def _save():
            import eventos as _ev
            return (_ev.update_evento(d["_edit_id"], d) if d.get("_edit_id")
                    else _ev.add_evento(d))

        try:
            r = await asyncio.wait_for(asyncio.to_thread(_save), timeout=30)
        except Exception as e:
            await q.edit_message_text(f"⚠️ No pude guardar: {str(e)[:150]}")
            return
        if not r.get("ok"):
            await q.message.reply_text(f"⚠️ {r.get('error', 'no se pudo guardar')}")
            return
        ev = r["evento"]
        _accion = "actualizada" if d.get("_edit_id") else "guardada en el calendario"
        context.user_data.pop("efem", None)
        _efem_espera(context)
        _f = ev["fecha"]
        _fd = f"{_f[3:5]}/{_f[0:2]}"
        await q.edit_message_text(
            f"✅ <b>Efeméride {_accion}</b>\n\n"
            f"📛 {ev['nombre']}\n"
            f"📅 {_fd} · ⏰ avisa {ev['anticipacion_dias']}d antes · 💪 {ev['esfuerzo_sugerido']}\n"
            f"🎯 {ev['segmento'] or '—'}\n"
            f"💬 {ev['angulo'] or '—'}\n\n"
            "El agente te la va a proponer al entrar en su ventana. La ves en /eventos.",
            parse_mode="HTML")
        return


async def cmd_programadas(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Lista las notas programadas en WP con opción de reprogramar."""
    import requests as _req_pr, base64 as _b64_pr, sqlite3 as _sq_pr
    from config import WP_URL as _WP_PR, WP_USER as _WPU_PR, WP_PASS as _WPP_PR
    _tok_pr = _b64_pr.b64encode(f"{_WPU_PR}:{_WPP_PR}".encode()).decode()
    _hdrs_pr = {"Authorization": f"Basic {_tok_pr}"}

    with _sq_pr.connect(_HDB) as _c_pr:
        rows_pr = _c_pr.execute(
            "SELECT id, title, pub_date, stage FROM jobs "
            "WHERE pub_date IS NOT NULL AND pub_date != '' "
            "AND stage NOT IN ('done','rejected') ORDER BY pub_date ASC LIMIT 10"
        ).fetchall()

    wp_future_pr = []
    try:
        _rf_pr = _req_pr.get(
            f"{_WP_PR}/wp-json/wp/v2/posts?status=future&per_page=20&_fields=id,title,date,link",
            headers=_hdrs_pr, timeout=10
        )
        if _rf_pr.ok:
            wp_future_pr = _rf_pr.json()
    except Exception:
        pass

    if not rows_pr and not wp_future_pr:
        await update.message.reply_text("No hay notas programadas.")
        return

    lines_pr = ["🗓 <b>Notas programadas</b>\n"]
    kb_pr = []
    for r_pr in rows_pr:
        jid_pr, title_pr, pub_date_pr, stage_pr = r_pr
        lines_pr.append(f"⏳ <b>{(title_pr or '')[:55]}</b>\n  📅 {pub_date_pr[:16]}  ·  {stage_pr}")
        kb_pr.append([
            {"text": f"✏️ #{jid_pr} {(title_pr or '')[:25]}", "callback_data": f"h_cola_back:{jid_pr}"},
            {"text": "❌ Cancelar",                             "callback_data": f"h_cola_cancel_job:{jid_pr}"},
        ])
    for wp_pr in wp_future_pr:
        wp_id_pr    = wp_pr["id"]
        wp_title_pr = wp_pr.get("title", {}).get("rendered", "")[:55]
        wp_date_pr  = wp_pr.get("date", "")[:16].replace("T", " ")
        lines_pr.append(f"📅 <b>{wp_title_pr}</b>\n  🗓 {wp_date_pr}")
        kb_pr.append([
            {"text": "🕐 Reprogramar", "callback_data": f"h_wp_reschedule:{wp_id_pr}"},
            {"text": "⚡ Publicar ya",  "callback_data": f"h_wp_publish_now:{wp_id_pr}"},
            {"text": "📝 Borrador",     "callback_data": f"h_wp_to_draft:{wp_id_pr}"},
        ])
    kb_pr.append([
        {"text": "🔄 Actualizar", "callback_data": "h_cola_ver_programadas"},
        {"text": "↩ Volver",      "callback_data": "pip_close"},
    ])
    await update.message.reply_text(
        "\n".join(lines_pr), parse_mode="HTML",
        reply_markup={"inline_keyboard": kb_pr},
        disable_web_page_preview=True,
    )


async def _borrar_cards_briefing(context, chat_id, jids):
    """Borra las tarjetas del briefing (card_msg_id) + el encabezado del briefing auto."""
    import sqlite3 as _sq, json as _js
    for _jid in jids:
        try:
            with _sq.connect("/opt/me-harness/harness.db") as _c:
                _row = _c.execute("SELECT content_json FROM jobs WHERE id=?", (int(_jid),)).fetchone()
            _st = _js.loads(_row[0]) if _row and _row[0] else {}
            if _st.get("card_msg_id"):
                await context.bot.delete_message(chat_id=chat_id, message_id=_st["card_msg_id"])
        except Exception:
            pass
    try:  # encabezado "🤖 Briefing AUTO…" (lo guarda curador.run_briefing_auto)
        _h = _js.load(open("/opt/me-harness/auto_briefing_hdr.json"))
        if _h.get("header_msg_id"):
            await context.bot.delete_message(chat_id=chat_id, message_id=_h["header_msg_id"])
    except Exception:
        pass


async def cmd_nutricion(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Briefing NUTRICIÓN (13/8): qué noticias fueron a nutrir living notes — en cola + procesadas
    24h, con destino/resultado y botón ↩️ para devolver una al briefing (enseña al router)."""
    msg = await update.message.reply_text("🍃 Generando briefing de nutrición…")
    import sys as _sys
    _sys.path.insert(0, "/opt/me-harness")
    try:
        from agents import curador as _cur
        await asyncio.to_thread(_cur.run_briefing_nutricion)
        await msg.delete()
    except Exception as e:
        await msg.edit_text(f"⚠️ Error en briefing nutrición: {e}")


async def cmd_briefing(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Briefing del Curador. `/briefing` o `/briefing manual` → briefing manual (revisar notas).
    `/briefing auto` → menú del briefing AUTOMÁTICO (cantidad de notas + horarios + pausa)."""
    _a0 = (context.args[0].lower() if context.args else "")
    if _a0.startswith("auto"):
        import sys as _sys
        _sys.path.insert(0, "/opt/me-harness")
        try:
            from agents import curador as _cur
            await update.message.reply_text(_cur.ciclaje_texto(), parse_mode="HTML",
                                            reply_markup=_cur.ciclaje_kb())
        except Exception as e:
            await update.message.reply_text(f"⚠️ No pude abrir el ciclaje: {e}")
        return
    offset = 0
    if context.args and _a0 != "manual":
        try:
            offset = int(context.args[0])
        except ValueError:
            pass
    msg = await update.message.reply_text("📰 Generando briefing…")
    import sys as _sys
    _sys.path.insert(0, "/opt/me-harness")
    try:
        from agents import curador as _cur
        await asyncio.to_thread(_cur.run_briefing, offset)
        await msg.delete()
    except Exception as e:
        await msg.edit_text(f"⚠️ Error en briefing: {e}")


async def cmd_ciclaje(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Abre el menú de config del briefing AUTOMÁTICO: cantidad de notas por corrida + horarios
    (turnos) + pausar/reanudar + correr ahora. Antes solo se llegaba por el botón ⚙️ de la tarjeta."""
    import sys as _sys
    _sys.path.insert(0, "/opt/me-harness")
    try:
        from agents import curador as _cur
        await update.message.reply_text(_cur.ciclaje_texto(), parse_mode="HTML",
                                        reply_markup=_cur.ciclaje_kb())
    except Exception as e:
        await update.message.reply_text(f"⚠️ No pude abrir el ciclaje: {e}")


async def cmd_lector(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Evalúa una nota con el Lector QA a pedido.
    Uso: /lector <URL_de_la_nota_o_job_id>
    """
    import sys as _sys, sqlite3 as _sq, json as _jl
    _sys.path.insert(0, "/opt/me-harness")

    arg = (context.args[0] if context.args else "").strip()
    if not arg:
        await update.message.reply_text(
            "Uso: <code>/lector &lt;URL_de_la_nota&gt;</code> o <code>/lector &lt;job_id&gt;</code>",
            parse_mode="HTML"
        )
        return

    msg = await update.message.reply_text("🔍 Buscando nota…")

    try:
        import broker as _brl

        # Resolver: ¿es job_id o URL?
        job = None
        if arg.isdigit():
            job = _brl.get_job(int(arg))
        else:
            # Buscar por wp_url o source_url en la DB
            with _sq.connect(_brl.HARNESS_DB) as _conn:
                _conn.row_factory = _sq.Row
                _row = _conn.execute(
                    "SELECT * FROM jobs WHERE wp_url=? OR source_url=? ORDER BY id DESC LIMIT 1",
                    (arg, arg)
                ).fetchone()
                if _row:
                    job = dict(_row)

        if not job:
            # Intentar extraer slug del URL y buscar en content_json
            _slug = arg.rstrip("/").split("/")[-1]
            with _sq.connect(_brl.HARNESS_DB) as _conn:
                _conn.row_factory = _sq.Row
                _rows = _conn.execute(
                    "SELECT * FROM jobs WHERE stage='done' ORDER BY id DESC LIMIT 300"
                ).fetchall()
            for _r in _rows:
                _cj = _jl.loads(_r["content_json"] or "{}")
                _wp = (_cj.get("wp_result") or {}).get("link", "") or _r["wp_url"] or ""
                if _slug and _slug in _wp:
                    job = dict(_r)
                    break

        if not job:
            await msg.edit_text(f"❌ No encontré ningún job para: <code>{arg}</code>", parse_mode="HTML")
            return

        wp_url = job.get("wp_url") or ""
        if not wp_url:
            _cj2 = _jl.loads(job.get("content_json") or "{}")
            wp_url = (_cj2.get("wp_result") or {}).get("link", "") or ""
        if not wp_url:
            await msg.edit_text(f"❌ Job #{job['id']} no tiene wp_url.", parse_mode="HTML")
            return

        job_id = job["id"]
        title  = job.get("title", "")
        await msg.edit_text(
            f"🔍 Evaluando job <b>#{job_id}</b>…\n<a href='{wp_url}'>{title[:70]}</a>",
            parse_mode="HTML", disable_web_page_preview=True
        )

        from agents import lector as _lect
        await _lect.evaluate(wp_url, job_id, title=title)

        await msg.delete()

    except Exception as _lerr:
        await msg.edit_text(f"❌ Error en /lector: {_lerr}", parse_mode="HTML")


async def cmd_coladepublicacion(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Muestra la cola de publicación del harness con sugerencias del agente.
    Leo confirma/ajusta destino → el job pasa a redaccion.
    """
    import sys as _sys, json as _js
    _sys.path.insert(0, "/opt/me-harness")
    try:
        import broker as _br_cola
        from agents import cola as _cola_agent
    except Exception as e:
        await update.message.reply_text(f"⚠️ Error cargando harness: {e}")
        return

    msg = await update.message.reply_text("🗂 Cargando cola de publicación…")

    # Ejecutar el agente para asignar sugerencias pendientes
    try:
        await asyncio.to_thread(_cola_agent.run_once)
    except Exception:
        pass

    jobs = _br_cola.dequeue_cola(limit=20)

    import sqlite3 as _sq_extra
    with _sq_extra.connect("/opt/me-harness/harness.db") as _c_extra:
        n_pendientes = _c_extra.execute(
            "SELECT COUNT(*) FROM jobs WHERE stage IN ('redaccion','publicacion','sin_imagen')"
        ).fetchone()[0]
        n_programadas_db = _c_extra.execute(
            "SELECT COUNT(*) FROM jobs WHERE pub_date IS NOT NULL AND pub_date != '' "
            "AND stage NOT IN ('done','rejected')"
        ).fetchone()[0]

    await msg.delete()

    if not jobs and not n_pendientes and not n_programadas_db:
        await update.message.reply_text(
            "📭 <b>Cola vacía</b> — no hay notas esperando publicación.\n"
            "<i>Usá /briefing para curar notas primero.</i>",
            parse_mode="HTML"
        )
        return

    # ── Solo el panel resumen — las cards se muestran con "Notas a procesar" ──
    manualmente = sum(
        1 for j in jobs
        if j.get("agent_suggestion") and
        "cambiado manualmente" in (
            __import__("json").loads(j["agent_suggestion"]).get("reasoning", "")
            if j["agent_suggestion"] else ""
        )
    )

    resumen = f"🗂 <b>Cola de publicación</b>"
    if jobs:
        resumen += f"\n📋 A procesar: <b>{len(jobs)}</b>"
    if n_pendientes:
        resumen += f"\n⏳ Pendientes en pipeline: <b>{n_pendientes}</b>"
    if n_programadas_db:
        resumen += f"\n🗓 Programadas: <b>{n_programadas_db}</b>"

    kb_resumen = []
    if jobs:
        kb_resumen.append([{"text": f"📋 Notas a procesar ({len(jobs)})",
                            "callback_data": "h_cola_notas_a_procesar"}])
    _proc_row = []
    if jobs:
        _proc_row.append({"text": f"📤 Procesar listado ({manualmente})", "callback_data": "h_cola_process_list"})
    if n_pendientes:
        _proc_row.append({"text": f"🔄 Procesar pendientes ({n_pendientes})", "callback_data": "h_cola_process_pend"})
    if _proc_row:
        kb_resumen.append(_proc_row)
    if jobs or n_pendientes:
        kb_resumen.append([{"text": "🚀 Procesar todo", "callback_data": "h_cola_process_all"}])
    if n_pendientes:
        kb_resumen.append([{"text": f"⏳ Ver pendientes ({n_pendientes})",
                            "callback_data": "h_cola_ver_pendientes"}])
    kb_resumen.append([{"text": "🗓 Ver notas programadas",
                        "callback_data": "h_cola_ver_programadas"}])
    kb_resumen.append([{"text": "🛑 Cancelar publicador", "callback_data": "h_cola_stop_pub"}])

    resumen_msg = await update.message.reply_text(
        resumen, parse_mode="HTML",
        reply_markup={"inline_keyboard": kb_resumen}
    )
    context.user_data["cola_msg_ids"] = [resumen_msg.message_id]


async def cmd_inst(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Aprueba nota del harness con instrucciones: /inst {job_id} {texto}
    Ej: /inst 42 enfocalo en exportadoras, tono crítico, versión corta
    """
    if not context.args or len(context.args) < 2:
        await update.message.reply_text(
            "Uso: <code>/inst {job_id} instrucciones</code>\n"
            "Ej: <code>/inst 42 enfocalo en pymes exportadoras, tono crítico</code>",
            parse_mode="HTML"
        )
        return
    import sys as _sys
    _sys.path.insert(0, "/opt/me-harness")
    try:
        from agents import curador as _cur
        job_id       = int(context.args[0])
        instructions = " ".join(context.args[1:])
        ok = _cur.approve(job_id, instructions=instructions)
        if ok:
            await update.message.reply_text(
                f"✅ Nota #{job_id} aprobada con instrucciones:\n"
                f"<i>{instructions[:200]}</i>",
                parse_mode="HTML"
            )
        else:
            await update.message.reply_text(f"❌ No encontré nota #{job_id} en el harness.")
    except ValueError:
        await update.message.reply_text("⚠️ job_id debe ser un número.")
    except Exception as e:
        await update.message.reply_text(f"⚠️ Error: {e}")


async def _process_url_for_review(url: str) -> dict | None:
    """Scrape + GPT rewrite sin publicar. Retorna data dict o None en error."""
    try:
        data = await asyncio.to_thread(scrape, url)
    except Exception:
        return None
    hilo = detect_hilo(data)
    data["hilo"] = hilo
    kw = focus_keyword(data.get("original_title") or data.get("title", ""))
    try:
        data["rewritten_excerpt"] = await asyncio.to_thread(
            rewrite_excerpt_with_gpt, get_title(data), data.get("text", ""),
            data.get("original_excerpt") or data.get("excerpt", ""), kw,
        )
    except Exception:
        data["rewritten_excerpt"] = ""
    return data


async def _publish_processed(bot, context, data: dict, chat_id: int):
    """Sube imagen + publica post + redes para un data dict ya procesado."""
    kw = focus_keyword(data.get("original_title") or data.get("title", ""))
    dest_on = context.user_data.get("dest_on", False)
    image_id = None
    if data.get("image_url"):
        image_id = await asyncio.to_thread(upload_image, data["image_url"], f"{kw} - {get_title(data)}")
    published = await asyncio.to_thread(publish_post, data, image_id, dest_on)
    if not published:
        return None, _LAST_WP_ERROR or "sin detalle"
    post_url     = published["link"]
    post_id      = published["id"]
    post_content = published["content"]
    tg_msg_id = 0
    tweet_id  = ""
    li_urn    = ""
    if context.user_data.get("tg_on", True):
        tg_msg_id = await publish_to_channel(bot, data, post_url) or 0
    if context.user_data.get("tw_on", True):
        tw_url = await asyncio.to_thread(post_tweet, data, post_url, None)
        if tw_url:
            tweet_id = tw_url.rsplit("/", 1)[-1]
    if context.user_data.get("li_on", False):
        li_url = await asyncio.to_thread(post_linkedin, data, post_url)
        if li_url:
            li_urn = _LAST_LINKEDIN_URN
    await asyncio.to_thread(append_social_meta, post_id, post_content, tweet_id, tg_msg_id, li_urn)
    return get_title(data), post_url


async def _auto_publish_url(bot, context, url: str, chat_id: int):
    """Scrape + publicación completa de una URL. Retorna (title, wp_url) o (None, error_str)."""
    data = await _process_url_for_review(url)
    if data is None:
        return None, f"Error scrapeando {url[:60]}"
    return await _publish_processed(bot, context, data, chat_id)


# ── Reporte diario programado ────────────────────────────────────────────────

def _ga4_fetch_data() -> dict:
    """Consulta GA4 y devuelve métricas de la última semana. Requiere GA4_SA_PATH y GA4_PROPERTY_ID."""
    from google.analytics.data_v1beta import BetaAnalyticsDataClient
    from google.analytics.data_v1beta.types import (
        RunReportRequest, Dimension, Metric, DateRange, OrderBy,
    )
    import os as _os
    _os.environ["GOOGLE_APPLICATION_CREDENTIALS"] = GA4_SA_PATH
    client = BetaAnalyticsDataClient()
    prop = f"properties/{GA4_PROPERTY_ID}"

    # 1. Usuarios por día (últimos 7 días)
    r_daily = client.run_report(RunReportRequest(
        property=prop,
        dimensions=[Dimension(name="date")],
        metrics=[Metric(name="totalUsers"), Metric(name="sessions")],
        date_ranges=[DateRange(start_date="7daysAgo", end_date="yesterday")],
    ))
    daily = [
        {"date": row.dimension_values[0].value,
         "users": int(row.metric_values[0].value),
         "sessions": int(row.metric_values[1].value)}
        for row in r_daily.rows
    ]

    # 2. Tráfico por hora (últimos 7 días)
    r_hourly = client.run_report(RunReportRequest(
        property=prop,
        dimensions=[Dimension(name="hour")],
        metrics=[Metric(name="sessions")],
        date_ranges=[DateRange(start_date="7daysAgo", end_date="yesterday")],
        order_bys=[OrderBy(metric=OrderBy.MetricOrderBy(metric_name="sessions"), desc=True)],
    ))
    hourly = [
        {"hour": int(row.dimension_values[0].value),
         "sessions": int(row.metric_values[0].value)}
        for row in r_hourly.rows
    ]

    # 3. Fuentes de tráfico
    r_sources = client.run_report(RunReportRequest(
        property=prop,
        dimensions=[Dimension(name="sessionSource"), Dimension(name="sessionMedium")],
        metrics=[Metric(name="sessions")],
        date_ranges=[DateRange(start_date="7daysAgo", end_date="yesterday")],
        order_bys=[OrderBy(metric=OrderBy.MetricOrderBy(metric_name="sessions"), desc=True)],
        limit=8,
    ))
    sources = [
        {"source": row.dimension_values[0].value,
         "medium": row.dimension_values[1].value,
         "sessions": int(row.metric_values[0].value)}
        for row in r_sources.rows
    ]

    # 4. Top páginas
    r_pages = client.run_report(RunReportRequest(
        property=prop,
        dimensions=[Dimension(name="pageTitle")],
        metrics=[Metric(name="screenPageViews")],
        date_ranges=[DateRange(start_date="7daysAgo", end_date="yesterday")],
        order_bys=[OrderBy(metric=OrderBy.MetricOrderBy(metric_name="screenPageViews"), desc=True)],
        limit=5,
    ))
    pages = [
        {"title": row.dimension_values[0].value[:60],
         "views": int(row.metric_values[0].value)}
        for row in r_pages.rows
    ]

    # 5. Dispositivos
    r_dev = client.run_report(RunReportRequest(
        property=prop,
        dimensions=[Dimension(name="deviceCategory")],
        metrics=[Metric(name="sessions")],
        date_ranges=[DateRange(start_date="7daysAgo", end_date="yesterday")],
    ))
    devices = {row.dimension_values[0].value: int(row.metric_values[0].value) for row in r_dev.rows}

    return {"daily": daily, "hourly": hourly, "sources": sources, "pages": pages, "devices": devices}


def _ga4_best_slots(hourly: list) -> list[int]:
    """Devuelve las 3 horas con más tráfico, separadas al menos 3h entre sí."""
    sorted_hours = sorted(hourly, key=lambda x: x["sessions"], reverse=True)
    slots = []
    for item in sorted_hours:
        h = item["hour"]
        if all(abs(h - s) >= 3 for s in slots):
            slots.append(h)
        if len(slots) == 3:
            break
    return sorted(slots)


async def _ga4_weekly_report(context: ContextTypes.DEFAULT_TYPE):
    """Reporte GA4 los lunes a las 8:00 ARG. Actualiza slots del curador y envía análisis al admin."""
    if not GA4_SA_PATH or not GA4_PROPERTY_ID or not ADMIN_CHAT_ID:
        return
    try:
        data = await asyncio.to_thread(_ga4_fetch_data)
    except Exception as e:
        logger.error(f"_ga4_weekly_report fetch: {e}")
        try:
            await context.bot.send_message(
                chat_id=int(ADMIN_CHAT_ID),
                text=f"⚠️ GA4 weekly report falló al obtener datos: {e}",
            )
        except Exception:
            pass
        return

    daily = data["daily"]
    hourly = data["hourly"]
    sources = data["sources"]
    pages = data["pages"]
    devices = data.get("devices", {})

    # Métricas base
    avg_users = round(sum(d["users"] for d in daily) / len(daily)) if daily else 0
    total_sessions_week = sum(d["sessions"] for d in daily)
    goal = 1000
    gap = goal - avg_users
    pct = round(avg_users / goal * 100)

    # Mejores slots horarios
    best_slots = _ga4_best_slots(hourly)
    slots_str = " / ".join(f"{h:02d}:00" for h in best_slots)

    # Fuentes top
    sources_text = "\n".join(
        f"  • {s['source']}/{s['medium']}: {s['sessions']} sesiones"
        for s in sources[:5]
    )

    # Top páginas
    pages_text = "\n".join(f"  • {p['title']}: {p['views']} vistas" for p in pages[:5])

    # Dispositivos
    dev_text = " | ".join(f"{k}: {v}" for k, v in devices.items())

    # Pedirle 3 recomendaciones a GPT
    gpt_recs = ""
    if OPENAI_API_KEY:
        prompt = (
            f"Sos el analista digital de MundoEmpresarial.ar, medio de noticias económicas para pymes argentinas.\n"
            f"OBJETIVO MASTER: llegar a 1000 usuarios únicos por día.\n\n"
            f"Datos de la última semana:\n"
            f"- Promedio usuarios/día: {avg_users} ({pct}% del objetivo)\n"
            f"- Sesiones totales: {total_sessions_week}\n"
            f"- Dispositivos: {dev_text}\n"
            f"- Fuentes de tráfico:\n{sources_text}\n"
            f"- Top páginas:\n{pages_text}\n"
            f"- Horas pico de tráfico: {slots_str}\n\n"
            f"Dá exactamente 3 recomendaciones concretas y accionables para acercar el sitio al objetivo de 1000 usuarios/día. "
            f"Cada recomendación en una línea, comenzando con un emoji relevante. "
            f"Español rioplatense, directo al punto, sin intro ni cierre."
        )
        try:
            r = openai_post(
                "https://api.openai.com/v1/chat/completions",
                headers={"Authorization": f"Bearer {OPENAI_API_KEY}", "Content-Type": "application/json"},
                json={"model": "gpt-4o-mini", "messages": [{"role": "user", "content": prompt}], "temperature": 0.5},
                timeout=60,
            )
            if r.status_code == 200:
                gpt_recs = r.json()["choices"][0]["message"]["content"].strip()
        except Exception as e:
            logger.error(f"_ga4_weekly_report GPT: {e}")

    # Armar mensaje
    progress_bar = "█" * (pct // 10) + "░" * (10 - pct // 10)
    slots_notice = ""

    msg = (
        f"📊 *Reporte semanal GA4 — MundoEmpresarial.ar*\n\n"
        f"🎯 Objetivo: 1.000 usuarios/día\n"
        f"📈 Promedio último semana: *{avg_users} usuarios/día*\n"
        f"`{progress_bar}` {pct}%\n"
        f"{'✅ Objetivo superado!' if avg_users >= goal else f'Faltan *{gap} usuarios/día* para el objetivo'}\n"
        f"{slots_notice}\n\n"
        f"⏱ *Horas pico:* {slots_str}\n\n"
        f"🌐 *Fuentes de tráfico:*\n{sources_text}\n\n"
        f"📄 *Top notas:*\n{pages_text}\n\n"
    )
    if gpt_recs:
        msg += f"💡 *3 recomendaciones para llegar a 1.000:*\n{gpt_recs}"

    try:
        await context.bot.send_message(
            chat_id=int(ADMIN_CHAT_ID),
            text=msg,
            parse_mode="Markdown",
        )
    except Exception as e:
        logger.error(f"_ga4_weekly_report send: {e}")


async def _learn_hashtags_daily(context: ContextTypes.DEFAULT_TYPE):
    """Lee correcciones de HT del día y actualiza ht_mapping.json via GPT (23:15 ARG)."""
    if not OPENAI_API_KEY:
        return
    import datetime as _dt
    try:
        with open(_HT_FEEDBACK_FILE) as f:
            all_records = json.load(f)
    except Exception:
        return
    today = _dt.date.today().isoformat()
    today_records = [r for r in all_records if r.get("ts", "").startswith(today)]
    if not today_records:
        return
    try:
        with open(_HT_MAPPING_FILE) as f:
            current_mapping = json.load(f)
    except Exception:
        current_mapping = {}
    corrections_text = "\n".join(
        f"Tags: {r['tags']} | Título: {r['title']} | Sugerido: {r['suggested']} → Usado: {r['used']}"
        for r in today_records
    )
    prompt = (
        "Sos el sistema de aprendizaje de hashtags de MundoEmpresarial.ar "
        "(noticias económicas para pymes argentinas en Twitter/X).\n\n"
        f"Mapping actual (término → hashtag sin #):\n{json.dumps(current_mapping, ensure_ascii=False)}\n\n"
        f"Correcciones del editor hoy ({today}):\n{corrections_text}\n\n"
        "Actualizá el mapping incorporando los patrones que ves. "
        "Devolvé SOLO un JSON con dos campos: "
        "\"mapping\" (dict actualizado, misma estructura) y "
        "\"aprendizaje\" (string 1-3 líneas explicando qué aprendiste hoy)."
    )
    try:
        r = openai_post(
            "https://api.openai.com/v1/chat/completions",
            headers={"Authorization": f"Bearer {OPENAI_API_KEY}", "Content-Type": "application/json"},
            json={"model": "gpt-4o-mini", "messages": [{"role": "user", "content": prompt}], "temperature": 0.2},
            timeout=60,
        )
        if r.status_code != 200:
            logger.error(f"_learn_hashtags_daily GPT {r.status_code}")
            return
        raw = r.json()["choices"][0]["message"]["content"].strip()
        m = re.search(r'\{.*\}', raw, re.DOTALL)
        if not m:
            logger.error("_learn_hashtags_daily: no JSON en respuesta GPT")
            return
        result = json.loads(m.group())
        updated_mapping = result.get("mapping", {})
        aprendizaje = result.get("aprendizaje", "Sin cambios.")
        if updated_mapping:
            with open(_HT_MAPPING_FILE, "w") as f:
                json.dump(updated_mapping, f, ensure_ascii=False, indent=2)
        if ADMIN_CHAT_ID:
            await context.bot.send_message(
                chat_id=int(ADMIN_CHAT_ID),
                text=f"🧠 *HT Learning diario*\n_{md_escape(aprendizaje)}_\n\n_{len(today_records)} corrección(es) procesadas hoy._",
                parse_mode="Markdown",
            )
    except Exception as e:
        logger.error(f"_learn_hashtags_daily: {e}")


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


# ─── Frases: keyboards ────────────────────────────────────────────────────────

def _frase_flag(fp: dict, key: str) -> bool:
    """Flag de red del flujo /frases. Todos ON por defecto salvo LinkedIn."""
    return bool(fp.get(key, key != "li_on"))


def _build_frase_kb(fp: dict) -> InlineKeyboardMarkup:
    def _lbl(txt, key):
        return ("✅ " if _frase_flag(fp, key) else "☐ ") + txt
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton(_lbl("Twitter", "tw_on"), callback_data="frase_toggle_tw"),
            InlineKeyboardButton(_lbl("Canal TG", "tg_on"), callback_data="frase_toggle_tg"),
        ],
        [
            InlineKeyboardButton(_lbl("WordPress", "wp_on"), callback_data="frase_toggle_wp"),
            InlineKeyboardButton(_lbl("LinkedIn", "li_on"), callback_data="frase_toggle_li"),
        ],
        [
            InlineKeyboardButton(_lbl("IG feed", "igf_on"), callback_data="frase_toggle_igf"),
            InlineKeyboardButton(_lbl("IG story", "igs_on"), callback_data="frase_toggle_igs"),
            InlineKeyboardButton(_lbl("WhatsApp", "wa_on"), callback_data="frase_toggle_wa"),
        ],
        [
            InlineKeyboardButton("✏️ Texto", callback_data="frase_set_texto"),
            InlineKeyboardButton("🏷 Título sup.", callback_data="frase_set_kicker"),
            InlineKeyboardButton("📅 Etiqueta", callback_data="frase_set_tag"),
        ],
        [
            InlineKeyboardButton("🚀 Publicar ahora", callback_data="frase_pub"),
            InlineKeyboardButton("⏰ Programar", callback_data="frase_schedule"),
        ],
        [InlineKeyboardButton("❌ Cancelar", callback_data="frase_cancel")],
    ])


def _build_frase_sched_pre_ht_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("✅ Confirmar HT", callback_data="fs_confirm_ht"),
            InlineKeyboardButton("✏️ Cambiar HT", callback_data="fs_change_ht_pre"),
        ],
        [InlineKeyboardButton("↩️ Volver", callback_data="fs_back_to_preview")],
    ])


def _build_frase_schedule_kb() -> InlineKeyboardMarkup:
    from datetime import datetime, timezone, timedelta
    tz_arg = timezone(timedelta(hours=-3))
    now = datetime.now(tz_arg)
    noon_day = "hoy" if now.hour < 11 else "mañana"
    evening_day = "hoy" if now.hour < 17 else "mañana"
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🌅 Turno mañana — 08:00 (mañana)", callback_data="fs_morning")],
        [InlineKeyboardButton(f"☀️ Mediodía — 12:00 ({noon_day})", callback_data="fs_noon")],
        [InlineKeyboardButton(f"🌇 Tarde — 18:00 ({evening_day})", callback_data="fs_evening")],
        [InlineKeyboardButton("🕐 Fijar hora", callback_data="fs_custom")],
        [InlineKeyboardButton("↩️ Volver", callback_data="fs_to_ht")],
    ])


def _build_frase_sched_day_kb() -> InlineKeyboardMarkup:
    from datetime import datetime, timezone, timedelta
    tz_arg = timezone(timedelta(hours=-3))
    now = datetime.now(tz_arg)
    labels = [
        (f"Hoy {now.strftime('%d/%m')}", "fs_day_0"),
        (f"Mañana {(now + timedelta(days=1)).strftime('%d/%m')}", "fs_day_1"),
        (f"Pasado {(now + timedelta(days=2)).strftime('%d/%m')}", "fs_day_2"),
    ]
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(label, callback_data=cd) for label, cd in labels],
        [InlineKeyboardButton("↩️ Volver", callback_data="fs_confirm_ht")],
    ])


def _build_frase_sched_hour_kb() -> InlineKeyboardMarkup:
    hours = ["06", "08", "10", "12", "14", "16", "18", "20", "22"]
    rows = []
    for i in range(0, len(hours), 3):
        rows.append([
            InlineKeyboardButton(f"{h}:00", callback_data=f"fs_h_{h}")
            for h in hours[i:i+3]
        ])
    rows.append([
        InlineKeyboardButton("✏️ Escribir hora", callback_data="fs_hour_write"),
        InlineKeyboardButton("↩️ Volver", callback_data="fs_custom"),
    ])
    return InlineKeyboardMarkup(rows)


async def cmd_set_frases_base(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Guarda la foto recibida como plantilla base para /frases."""
    photo = update.message.photo[-1] if update.message.photo else None
    if not photo:
        await update.message.reply_text("Mandame la imagen base como foto junto con el comando /set_frases_base.")
        return
    os.makedirs("/opt/mundoempresarial-bot/assets", exist_ok=True)
    file = await context.bot.get_file(photo.file_id)
    await file.download_to_drive("/opt/mundoempresarial-bot/assets/frases_base.png")
    await update.message.reply_text("✅ Plantilla base guardada. Ya podés usar /frases.")


CAT_FRASES = 1838  # Categoría "Frases" en WordPress


def _gpt_frase_body(frase: str) -> str:
    """Genera 4 párrafos de reflexión empresarial para una frase inspiradora via GPT."""
    if not OPENAI_API_KEY:
        return ""
    prompt = (
        "Sos el editor de MundoEmpresarial.ar, medio de noticias económicas para pymes y "
        "empresarios argentinos. Esta nota pertenece a la sección de frases inspiradoras.\n\n"
        f'La frase a desarrollar es: "{frase}"\n\n'
        "Escribí exactamente 4 párrafos de 80-100 palabras cada uno:\n"
        "1. Significado de la frase en el contexto empresarial argentino actual.\n"
        "2. Cómo aplica en la práctica para pymes, con ejemplos concretos.\n"
        "3. Relación con los desafíos actuales de los empresarios (inflación, mercado, competencia).\n"
        "4. Reflexión final con llamado a la acción para el lector empresario.\n\n"
        "Reglas: español rioplatense (vos/ustedes). Sin citar la frase textualmente más de una vez. "
        "Sin encabezados ni HTML. Párrafos separados por línea en blanco. "
        "Máximo 120 palabras por párrafo. Solo el texto, sin explicaciones previas."
    )
    try:
        r = openai_post(
            "https://api.openai.com/v1/chat/completions",
            headers={"Authorization": f"Bearer {OPENAI_API_KEY}", "Content-Type": "application/json"},
            json={"model": "gpt-4o-mini", "messages": [{"role": "user", "content": prompt}], "temperature": 0.5},
            timeout=60,
        )
        if r.status_code == 200:
            return r.json()["choices"][0]["message"]["content"].strip()
    except Exception as e:
        logger.warning(f"_gpt_frase_body: {e}")
    return ""


def _format_frase_content(frase: str, kw: str, body_text: str = "") -> str:
    """Genera el HTML completo de una nota de frase respetando los criterios SEO del manual."""
    parts = []
    # Lead en negrita con keyword
    parts.append(f"<p><strong>{frase}</strong></p>")
    # H2 con keyword
    parts.append(f"<h2>{kw.capitalize()}: reflexión para empresarios y pymes</h2>")
    # Cuerpo: párrafos generados por GPT o fallback genérico
    if body_text:
        for para in body_text.split("\n\n"):
            para = para.strip()
            if para:
                parts.append(f"<p>{para}</p>")
    else:
        parts.append(
            f"<p>Esta frase invita a los empresarios y dueños de pymes a reflexionar sobre "
            f"el rol del {kw} en el crecimiento de sus negocios. En el contexto económico argentino, "
            f"donde la incertidumbre y la inflación marcan el día a día, las palabras que orientan "
            f"la acción tienen un valor especial.</p>"
        )
    # Segundo H2 (Rank Math: keyword en subheadings)
    parts.append(f"<h2>Por qué el {kw} importa en tu empresa</h2>")
    # Pyme box
    parts.append(pyme_box(frase, frase))
    return "\n".join(parts)


def _wp_publish_frase(frase: str, img_bytes: bytes, scheduled_for=None) -> dict:
    """Sube imagen y crea post en WordPress con SEO completo (Rank Math)."""
    h = wp_auth()
    img_resp = requests.post(
        f"{WP_URL}/wp-json/wp/v2/media",
        headers={**h, "Content-Disposition": 'attachment; filename="frase.png"',
                 "Content-Type": "image/png"},
        data=img_bytes, timeout=30,
    )
    img_id  = None
    img_url = ""
    if img_resp.status_code == 201:
        img_id  = img_resp.json().get("id")
        img_url = img_resp.json().get("source_url", "")

    kw        = focus_keyword(frase)
    wp_title  = seo_title(frase)   # max 60 chars, corte inteligente
    desc      = frase[:155] if len(frase) <= 155 else frase[:152] + "..."
    slug      = url_slug(frase)[:75]
    alt_text  = f"{kw} - {wp_title}"

    # Actualizar alt de la imagen con el keyword
    if img_id and kw:
        try:
            requests.post(
                f"{WP_URL}/wp-json/wp/v2/media/{img_id}",
                headers={**h, "Content-Type": "application/json"},
                json={"alt_text": alt_text, "title": wp_title},
                timeout=10,
            )
        except Exception:
            pass

    body_text = _gpt_frase_body(frase)
    content   = _format_frase_content(frase, kw, body_text)

    payload = {
        "title":          wp_title,
        "content":        content,
        "excerpt":        desc,
        "slug":           slug,
        "categories":     [CAT_FRASES],
        "featured_media": img_id or 0,
        "meta": {
            "rank_math_title":            _meta_safe(wp_title),
            "rank_math_description":      _meta_safe(desc, 320),
            "rank_math_focus_keyword":    kw,
            "rank_math_robots":           ["index", "follow"],
            "rank_math_og_content_image": img_url,
        },
    }
    if scheduled_for is not None:
        from datetime import timezone, timedelta
        tz_arg = timezone(timedelta(hours=-3))
        if scheduled_for.tzinfo is None:
            scheduled_for = scheduled_for.replace(tzinfo=tz_arg)
        payload["status"] = "future"
        payload["date"] = scheduled_for.strftime("%Y-%m-%dT%H:%M:%S")
    else:
        payload["status"] = "publish"

    r = requests.post(f"{WP_URL}/wp-json/wp/v2/posts", headers=h, json=payload, proxies=_WP_PROXIES, timeout=30)
    if r.status_code == 201:
        body = r.json()
        return {"link": body["link"], "id": body["id"]}
    raise RuntimeError(f"WP {r.status_code}: {r.text[:200]}")


# ─── /encuesta: nota-encuesta interactiva + notas embebidas + difusión ─────────

def _build_enc_kb(fp: dict) -> InlineKeyboardMarkup:
    c = fp.get("canales", {})
    def _l(txt, k):
        return ("✅ " if c.get(k) else "☐ ") + txt
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(_l("Telegram", "tg"), callback_data="enc_tg"),
         InlineKeyboardButton(_l("X", "x"), callback_data="enc_x"),
         InlineKeyboardButton(_l("LinkedIn", "li"), callback_data="enc_li")],
        [InlineKeyboardButton(_l("Facebook", "fb"), callback_data="enc_fb"),
         InlineKeyboardButton(_l("Instagram", "ig"), callback_data="enc_ig"),
         InlineKeyboardButton(_l("WhatsApp", "wa"), callback_data="enc_wa")],
        [InlineKeyboardButton(_l("📧 Newsletter", "nl"), callback_data="enc_tognl")],
        [InlineKeyboardButton("🚀 Publicar ahora", callback_data="enc_pub"),
         InlineKeyboardButton("⏰ Programar", callback_data="enc_sched")],
        [InlineKeyboardButton("❌ Cancelar", callback_data="enc_cancel")]])


async def _enc_wa_delivery(context, chat_id, wp_id, pregunta, wp_url):
    """WhatsApp no tiene API → el bot le manda a Leo la foto de la nota + el copy para
    subir a mano (mismo criterio que /frases)."""
    def _foto():
        import requests as _rq
        try:
            p = _rq.get(f"{WP_URL}/wp-json/wp/v2/posts/{wp_id}?_fields=featured_media", timeout=15).json()
            mid = p.get("featured_media")
            if not mid:
                return None
            m = _rq.get(f"{WP_URL}/wp-json/wp/v2/media/{mid}?_fields=source_url", timeout=15).json()
            src = m.get("source_url")
            return _rq.get(src, timeout=20).content if src else None
        except Exception:
            return None
    copy = f"🗳️ {pregunta}\n\nVotá acá 👉 {utm_url(wp_url, 'whatsapp')}"
    try:
        foto = await asyncio.to_thread(_foto)
        if foto:
            bio = io.BytesIO(foto); bio.name = "encuesta.jpg"
            await context.bot.send_photo(chat_id=int(chat_id), photo=bio,
                caption="🟢 Para WhatsApp (estado/grupo): descargá la foto y subila. Texto 👇")
        await context.bot.send_message(chat_id=int(chat_id), text=copy)
    except Exception as e:
        logger.warning(f"wa delivery encuesta: {e}")


async def _do_encuesta_publish(context, fp: dict, scheduled_for, chat_id=None) -> str:
    """Llama al agente encuesta.publish en thread (nada bloqueante en el loop)."""
    import sys as _se
    if "/opt/me-harness" not in _se.path:
        _se.path.insert(0, "/opt/me-harness")
    cdict = fp.get("canales", {})
    wa_on = bool(cdict.get("wa"))
    canales = tuple(k for k, v in cdict.items() if v and k != "wa")   # wa se entrega manual
    if "ig" in canales:
        canales = canales + ("ig_story",)   # la encuesta también va a IG story

    def _run():
        from agents import encuesta as _E
        return _E.publish(fp["pregunta"], fp["opciones"], fp.get("notas"), canales,
                          scheduled_for, image_id=fp.get("image_id"))
    try:
        r = await asyncio.wait_for(asyncio.to_thread(_run), timeout=120)
    except Exception as e:
        return f"❌ Error: {str(e)[:150]}"
    if not r.get("ok"):
        return f"❌ {r.get('error')}"
    dif = r.get("difusion") or {}
    okred = [n for n, k in (("TG", "tg_msg_id"), ("X", "tweet_id"), ("LI", "li_urn")) if dif.get(k)]
    if dif.get("tg_poll_msg_id"):
        okred.append("poll TG")
    if dif.get("meta_encolados"):
        okred.append("FB+IG+story en cola (pacing 5min)")
    # WhatsApp: entrega manual a Leo (solo publicación inmediata)
    if wa_on and scheduled_for is None and chat_id:
        await _enc_wa_delivery(context, chat_id, r.get("wp_id"), fp["pregunta"], r.get("wp_url"))
        okred.append("WhatsApp (manual)")
    when = f"programada {scheduled_for.strftime('%d/%m %H:%M')}" if scheduled_for else "publicada"
    red = (", ".join(okred) if okred else ("se difunde a la hora" if scheduled_for else "—"))
    return f"✅ Encuesta {when}\n{r.get('wp_url')}\nRedes: {red}", r


def _enc_nl_base_kb():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("👥 Base completa", callback_data="enc_nl_base:base")],
        [InlineKeyboardButton("👀 Lectores", callback_data="enc_nl_base:lectores")],
        [InlineKeyboardButton("⚡ Activos", callback_data="enc_nl_base:activos")],
        [InlineKeyboardButton("❌ Sin newsletter", callback_data="enc_nl_cancel")]])


async def _do_enc_newsletter(context, nl: dict, scheduled_at) -> str:
    """Arma y programa el newsletter de la encuesta (comparte el enc con la web)."""
    import sys as _se
    if "/opt/me-harness" not in _se.path:
        _se.path.insert(0, "/opt/me-harness")

    def _run():
        from agents import encuesta as _E
        return _E.enviar_newsletter(nl["enc"], nl["pregunta"], nl["opciones"], nl.get("notas"),
                                    base=nl.get("base", "base"), subject=nl.get("subject"),
                                    scheduled_at=scheduled_at, nota_url=nl.get("nota_url"))
    try:
        r = await asyncio.wait_for(asyncio.to_thread(_run), timeout=120)
    except Exception as e:
        return f"❌ Error newsletter: {str(e)[:150]}"
    if not r.get("ok"):
        return f"❌ Newsletter: {r.get('error')}"
    sch = r.get("sched") or {}
    when = "ahora" if not scheduled_at else scheduled_at[11:16]
    warn = " ⚠️ >1000 dest (escalonado)" if sch.get("warn") else ""
    return (f"✅ Newsletter #{r.get('campaign_id')} a <b>{nl.get('base')}</b> — {when}{warn}\n"
            f"Asunto: {r.get('subject','')}\nDest.: {sch.get('recipients','?')}\n"
            f"Preview: {r.get('preview','')}").replace("<b>", "").replace("</b>", "")


async def cmd_encuesta(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["enc"] = {"canales": {"tg": True, "x": True, "li": True, "fb": True, "ig": True, "wa": True}}
    context.user_data["awaiting_enc_pregunta"] = True
    await update.message.reply_text(
        "🗳️ <b>Nueva encuesta</b>\n\nPasame la <b>PREGUNTA</b>.\n"
        "Ej: Como pyme, ¿la suba de tarifas frena tu producción?", parse_mode="HTML")


async def cmd_frases(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Genera imagen con frase y muestra preview con opciones de publicación."""
    frase = " ".join(context.args).strip() if context.args else ""
    if not frase:
        await update.message.reply_text(
            "Usá: `/frases <texto>`\n\nEjemplo:\n`/frases La innovación distingue a los líderes.`",
            parse_mode="Markdown",
        )
        return

    msg = await update.message.reply_text("🎨 Generando imagen...")
    try:
        from frases_gen import generate_frase_image
        img_bytes = await asyncio.to_thread(generate_frase_image, frase)
    except Exception as e:
        await msg.edit_text(f"❌ Error generando imagen: {e}")
        return

    fp = {
        "texto":    frase,
        "img_bytes": img_bytes,
        "tw_on":    True,
        "tg_on":    True,
        "wp_on":    True,
        "igf_on":   True,
        "igs_on":   True,
        "wa_on":    True,
    }
    context.user_data["frase_pending"] = fp
    context.user_data.pop("frase_custom_ht", None)
    context.user_data.pop("frase_sched_ht", None)

    await msg.delete()
    bio = io.BytesIO(img_bytes)
    bio.name = "frase.png"
    await update.message.reply_photo(
        photo=bio,
        caption=f"💬 *{md_escape(frase)}*\n\n_Elegí las redes y acción:_",
        parse_mode="Markdown",
        reply_markup=_build_frase_kb(fp),
    )


async def _frase_edit(query, caption: str, parse_mode: str = None, reply_markup=None):
    """edit_message_caption si el mensaje es foto, edit_message_text si es texto puro."""
    kwargs = {"reply_markup": reply_markup} if reply_markup is not None else {}
    if parse_mode:
        kwargs["parse_mode"] = parse_mode
    if query.message.photo:
        await query.edit_message_caption(caption=caption, **kwargs)
    else:
        await query.edit_message_text(caption, **kwargs)


async def _frase_ig_wa(context, chat_id, igf_on, igs_on, wa_on,
                       frase, img_bytes, post_url, custom_ht):
    """IG feed/story (Graph API vía redes del harness) + entrega WhatsApp manual.
    La placa master (cuadrada) se adapta con pad_placa: feed 3:4, story/estado 9:16.
    Devuelve las líneas de resultado (✅/❌ por destino). Todo en to_thread+timeout
    — nada sync bloqueante en el loop del bot."""
    lines = []
    if not img_bytes:
        return lines
    from frases_gen import pad_placa
    if igf_on or igs_on:
        try:
            import sys as _sys_ig
            if "/opt/me-harness" not in _sys_ig.path:
                _sys_ig.path.insert(0, "/opt/me-harness")
            from agents import redes as _redes
            cap = f"{frase}\n\n{custom_ht}"
            if igf_on:
                png = await asyncio.to_thread(pad_placa, img_bytes, 1080, 1440)
                r = await asyncio.wait_for(
                    asyncio.to_thread(_redes.ig_publicar_bytes, png, cap, False,
                                      f"placa frase {frase[:50]}"),
                    timeout=120)
                lines.append("✅ IG feed" if r.get("ok")
                             else f"❌ IG feed: {str(r.get('error'))[:60]}")
            if igs_on:
                png = await asyncio.to_thread(pad_placa, img_bytes, 1080, 1920)
                r = await asyncio.wait_for(
                    asyncio.to_thread(_redes.ig_publicar_bytes, png, cap, True,
                                      f"placa frase story {frase[:50]}"),
                    timeout=120)
                lines.append("✅ IG story" if r.get("ok")
                             else f"❌ IG story: {str(r.get('error'))[:60]}")
        except asyncio.TimeoutError:
            lines.append("❌ Instagram: timeout")
        except Exception as e:
            lines.append(f"❌ Instagram: {str(e)[:60]}")
    if wa_on and chat_id:
        # WhatsApp no tiene API de publicación → se entrega la placa formato estado
        # (9:16) + el texto aparte, listos para subir a mano.
        try:
            png = await asyncio.to_thread(pad_placa, img_bytes, 1080, 1920)
            bio = io.BytesIO(png)
            bio.name = "frase_whatsapp.png"
            await context.bot.send_photo(
                chat_id=int(chat_id), photo=bio,
                caption="🟢 Para WhatsApp (estado/canal): descargá la placa y subila. Texto 👇")
            wa_copy = frase
            if post_url:
                wa_copy += f"\n\n{utm_url(post_url, 'whatsapp')}"
            await context.bot.send_message(chat_id=int(chat_id), text=wa_copy)
            lines.append("🟢 WhatsApp: placa + texto (subida manual)")
        except Exception as e:
            lines.append(f"❌ WhatsApp: {str(e)[:60]}")
    return lines


async def _do_frase_schedule(query, context, target):
    """Programa publicación de una frase: WP (future) + job para TG/Twitter."""
    fp = context.user_data.get("frase_pending")
    if not fp:
        await _frase_edit(query, caption="Error: no hay frase pendiente.")
        return

    frase     = fp["texto"]
    img_bytes = fp.get("img_bytes")
    tw_on     = fp.get("tw_on", True)
    tg_on     = fp.get("tg_on", True)
    li_on     = fp.get("li_on", False)
    custom_ht = context.user_data.get("frase_sched_ht", "#Frases #MundoEmpresarial #Pymes")

    await _frase_edit(query, caption="🔍 Verificando colisiones…")
    adjusted = await asyncio.to_thread(find_scheduled_collision, target)

    offset_msg = ""
    if adjusted != target:
        delta_min = int((adjusted - target).total_seconds() / 60)
        offset_msg = f" (ajustado +{delta_min} min)"

    await _frase_edit(query, caption=f"📤 Programando para {adjusted.strftime('%A %d/%m %H:%M')}{offset_msg}…")

    if not img_bytes:
        try:
            from frases_gen import generate_frase_image
            img_bytes = await asyncio.to_thread(
                generate_frase_image, frase, fp.get("kicker"), fp.get("tag"))
        except Exception as e:
            await _frase_edit(query, caption=f"❌ Error generando imagen: {e}")
            return

    try:
        import sys as _sys_frs
        _sys_frs.path.insert(0, "/opt/me-harness")
        from agents.frases import wp_publish as _frases_wp
        wp_data = await asyncio.to_thread(_frases_wp, frase, img_bytes, adjusted)
    except Exception as e:
        await _frase_edit(query, caption=f"❌ Error en WordPress: {e}")
        return

    post_url = wp_data["link"]
    post_id  = wp_data["id"]

    job_data = {
        "frase":     frase,
        "post_url":  post_url,
        "post_id":   post_id,
        "tw_on":     tw_on,
        "tg_on":     tg_on,
        "li_on":     li_on,
        "igf_on":    _frase_flag(fp, "igf_on"),
        "igs_on":    _frase_flag(fp, "igs_on"),
        "wa_on":     _frase_flag(fp, "wa_on"),
        "chat_id":   query.message.chat_id,
        "custom_ht": custom_ht,
        "kicker":    fp.get("kicker"),
        "tag":       fp.get("tag"),
    }
    try:
        context.application.job_queue.run_once(
            _fire_frase_social,
            when=adjusted,
            data=job_data,
            name=f"sched_frase_{post_id}",
        )
    except Exception as e:
        logger.error(f"run_once frase falló: {e}")

    context.user_data.pop("frase_pending", None)
    context.user_data.pop("frase_sched_ht", None)
    context.user_data.pop("frase_sched_day", None)
    context.user_data.pop("frase_sched_target", None)

    await _frase_edit(
        query,
        caption=(
            f"✅ Frase programada para {adjusted.strftime('%A %d/%m a las %H:%M')}{offset_msg}\n\n"
            f"📝 WP: {post_url}\n"
            f"🔔 A esa hora se postea en canal TG y preview de Twitter."
        ),
    )


async def _fire_frase_social(context: ContextTypes.DEFAULT_TYPE):
    """Job programado: publica frase en canal TG y envía preview de tweet al admin."""
    job_data  = context.job.data
    frase     = job_data.get("frase", "")
    post_url  = job_data.get("post_url", "")
    post_id   = job_data.get("post_id")
    chat_id   = job_data.get("chat_id")
    tg_on     = job_data.get("tg_on", True)
    tw_on     = job_data.get("tw_on", True)
    li_on     = job_data.get("li_on", False)
    custom_ht = job_data.get("custom_ht", "#Frases #MundoEmpresarial #Pymes")

    results = [f"🔔 Frase programada publicada: {post_url}"]

    try:
        from frases_gen import generate_frase_image
        img_bytes = await asyncio.to_thread(
            generate_frase_image, frase, job_data.get("kicker"), job_data.get("tag"))
    except Exception as e:
        img_bytes = None
        results.append(f"❌ Error generando imagen: {e}")

    if tg_on and img_bytes:
        try:
            cap = f"💬 *{md_escape(frase)}*"
            if post_url:
                cap += f"\n\n🔗 [Ver en web]({utm_url(post_url, 'telegram')})"
            cap += f"\n\n{custom_ht}"
            bio = io.BytesIO(img_bytes)
            bio.name = "frase.png"
            await context.bot.send_photo(
                chat_id=TELEGRAM_CHANNEL, photo=bio, caption=cap, parse_mode="Markdown",
            )
            results.append("✅ Canal TG")
        except Exception as e:
            results.append(f"❌ Canal TG: {e}")

    results.extend(await _frase_ig_wa(
        context, chat_id,
        job_data.get("igf_on", False), job_data.get("igs_on", False), job_data.get("wa_on", False),
        frase, img_bytes, post_url, custom_ht))

    if tw_on and chat_id and img_bytes:
        tweet_text = frase
        if post_url:
            tweet_text += f"\n\n{utm_url(post_url, 'twitter')}"
        tweet_text += f"\n\n{custom_ht}"
        if len(tweet_text) > 280:
            tweet_text = frase[:200] + "…\n\n" + custom_ht
        try:
            user_data = context.application.user_data[int(chat_id)]
            user_data["frase_tweeting"] = {
                "frase":     frase,
                "img_bytes": img_bytes,
                "post_url":  post_url,
                "post_id":   post_id,
            }
            user_data["frase_custom_ht"] = custom_ht
        except Exception:
            pass
        kb_tweet = InlineKeyboardMarkup([
            [
                InlineKeyboardButton("Twittear", callback_data="frase_tweet"),
                InlineKeyboardButton("No twittear", callback_data="frase_no_tweet"),
            ],
            [InlineKeyboardButton("Cambiar HT", callback_data="frase_change_ht")],
        ])
        await context.bot.send_message(
            chat_id=int(chat_id),
            text="\n".join(results) + f"\n\n— Preview del tweet —\n`{md_escape(tweet_text)}`",
            parse_mode="Markdown",
            reply_markup=kb_tweet,
        )
    if li_on and post_url:
        li_url = await asyncio.to_thread(post_linkedin, {"title": frase, "excerpt": ""}, post_url)
        if li_url:
            results.append(f"✅ LinkedIn: {li_url}")
        else:
            results.append(f"❌ LinkedIn: {_LAST_LINKEDIN_ERROR[:80]}")

    if chat_id:
        await context.bot.send_message(chat_id=int(chat_id), text="\n".join(results))


# ══════════════════════════════════════════════════════════════════════════════
# /edito — Editor editorial
# ══════════════════════════════════════════════════════════════════════════════

_EDITO_DB        = "/opt/me-harness/harness.db"
_EDITO_PAGE_SIZE = 6

_NIVEL_EMOJI = {"breaking": "⚡", "dia": "☀️", "semana": "📅", "mes": "📆", "anio": "🗓"}
_ESTADO_EMOJI = {"activo": "🟢", "saturado": "🟡", "cerrado": "⛔", "lista_negra": "🔴"}
_NIVEL_LABELS = ["breaking", "dia", "semana", "mes", "anio"]


# ── DB helpers ────────────────────────────────────────────────────────────────

def _edito_get_temas():
    """Retorna temas raíz (sin padre) activos."""
    import sqlite3 as _sq
    try:
        with _sq.connect(_EDITO_DB) as conn:
            conn.row_factory = _sq.Row
            rows = conn.execute(
                "SELECT * FROM temas WHERE estado NOT IN ('cerrado','lista_negra') "
                "AND (parent_id IS NULL OR parent_id = 0) "
                "ORDER BY n_notas DESC LIMIT 100"
            ).fetchall()
        return [dict(r) for r in rows]
    except Exception as e:
        logger.warning(f"_edito_get_temas: {e}")
        return []


def _edito_get_subtemas(parent_id: int):
    import sqlite3 as _sq
    try:
        with _sq.connect(_EDITO_DB) as conn:
            conn.row_factory = _sq.Row
            rows = conn.execute(
                "SELECT * FROM temas WHERE parent_id=? ORDER BY n_notas DESC",
                (parent_id,)
            ).fetchall()
        return [dict(r) for r in rows]
    except Exception as e:
        logger.warning(f"_edito_get_subtemas: {e}")
        return []


def _edito_count_subtemas(parent_id: int) -> int:
    import sqlite3 as _sq
    try:
        with _sq.connect(_EDITO_DB) as conn:
            row = conn.execute(
                "SELECT COUNT(*) FROM temas WHERE parent_id=?", (parent_id,)
            ).fetchone()
        return row[0] if row else 0
    except Exception:
        return 0


def _edito_set_parent(subtema_id: int, parent_id):
    import sqlite3 as _sq
    with _sq.connect(_EDITO_DB) as conn:
        conn.execute("UPDATE temas SET parent_id=? WHERE id=?", (parent_id, subtema_id))


def _edito_make_independent(subtema_id: int):
    _edito_set_parent(subtema_id, None)


def _edito_unificar(src_id: int, dst_id: int):
    """Fusiona src en dst: suma n_notas, mueve subtemas de src a dst, cierra src."""
    import sqlite3 as _sq
    with _sq.connect(_EDITO_DB) as conn:
        src = conn.execute("SELECT n_notas FROM temas WHERE id=?", (src_id,)).fetchone()
        if src:
            conn.execute("UPDATE temas SET n_notas = n_notas + ? WHERE id=?", (src[0], dst_id))
        conn.execute("UPDATE temas SET parent_id=? WHERE parent_id=?", (dst_id, src_id))
        conn.execute("UPDATE temas SET estado='cerrado' WHERE id=?", (src_id,))


def _edito_get_tema(tema_id: int):
    import sqlite3 as _sq
    try:
        with _sq.connect(_EDITO_DB) as conn:
            conn.row_factory = _sq.Row
            row = conn.execute("SELECT * FROM temas WHERE id=?", (tema_id,)).fetchone()
        return dict(row) if row else None
    except Exception:
        return None


def _edito_set_tema_estado(tema_id: int, estado: str):
    import sqlite3 as _sq
    with _sq.connect(_EDITO_DB) as conn:
        conn.execute("UPDATE temas SET estado=? WHERE id=?", (estado, tema_id))


def _edito_set_tema_nivel(tema_id: int, nivel: str):
    import sqlite3 as _sq
    with _sq.connect(_EDITO_DB) as conn:
        conn.execute("UPDATE temas SET nivel=? WHERE id=?", (nivel, tema_id))


def _edito_rename_tema(tema_id: int, nuevo_nombre: str):
    import sqlite3 as _sq
    with _sq.connect(_EDITO_DB) as conn:
        conn.execute("UPDATE temas SET nombre=? WHERE id=?", (nuevo_nombre.strip(), tema_id))


def _edito_upsert_tema(nombre: str, nivel: str, parent_id=None):
    import sqlite3 as _sq
    from datetime import datetime as _dt
    now = _dt.utcnow().isoformat()
    with _sq.connect(_EDITO_DB) as conn:
        row = conn.execute("SELECT id FROM temas WHERE nombre=?", (nombre,)).fetchone()
        if row:
            conn.execute(
                "UPDATE temas SET nivel=?, estado='activo', ultima_vez=?, parent_id=COALESCE(?,parent_id) WHERE id=?",
                (nivel, now, parent_id, row[0])
            )
            return row[0]
        cur = conn.execute(
            "INSERT INTO temas (nombre, nivel, estado, n_notas, primera_vez, ultima_vez, creado_por, parent_id) "
            "VALUES (?,?,?,?,?,?,?,?)",
            (nombre, nivel, "activo", 0, now, now, "leo", parent_id)
        )
        return cur.lastrowid


def _edito_get_descubrimientos(limit: int = 30):
    import sqlite3 as _sq
    try:
        with _sq.connect(_EDITO_DB) as conn:
            conn.row_factory = _sq.Row
            rows = conn.execute(
                "SELECT id, title, score, updated_at, content_json FROM jobs "
                "WHERE stage='descubrimiento' ORDER BY score DESC, id DESC LIMIT ?",
                (limit,)
            ).fetchall()
        return [dict(r) for r in rows]
    except Exception as e:
        logger.warning(f"_edito_get_descubrimientos: {e}")
        return []


def _edito_promote_disc(job_id: int):
    import sqlite3 as _sq
    with _sq.connect(_EDITO_DB) as conn:
        conn.execute(
            "UPDATE jobs SET stage='curado', updated_at=datetime('now') WHERE id=?",
            (job_id,)
        )


def _edito_reject_disc(job_id: int):
    import sqlite3 as _sq, json as _js
    with _sq.connect(_EDITO_DB) as conn:
        row = conn.execute("SELECT content_json FROM jobs WHERE id=?", (job_id,)).fetchone()
        cj = {}
        if row and row[0]:
            try: cj = _js.loads(row[0])
            except Exception: pass
        cj["rejected_reason"] = "descartado_leo"
        conn.execute(
            "UPDATE jobs SET stage='rejected', content_json=?, updated_at=datetime('now') WHERE id=?",
            (_js.dumps(cj), job_id)
        )


def _edito_get_notas_tema(tema_id: int, limit: int = 20) -> list:
    """
    Notas publicadas (stage=done) relacionadas al tema.
    Usa el embedding del tema vs content_json embeddings si están disponibles;
    sino, busca por palabras clave del nombre.
    """
    import sqlite3 as _sq, json as _js
    _STOP = {"hoy","para","como","pero","con","los","las","del","que","una",
             "uno","este","esta","por","sus","más","sin","sobre","entre",
             "desde","hasta","será","fueron","son","hay","ser","hace",
             "tras","ante","bajo","según","cuyo","cuya","este","esos"}
    try:
        with _sq.connect(_EDITO_DB) as conn:
            row = conn.execute(
                "SELECT nombre, wp_cat_id FROM temas WHERE id=?", (tema_id,)
            ).fetchone()
            if not row:
                return []
            nombre, wp_cat_id = row[0], row[1]

            # ── Ruta 1: por wp_cat_id (exacta, si el tema viene de WP) ───────
            if wp_cat_id:
                rows = conn.execute(
                    "SELECT title, wp_url, updated_at, content_json FROM jobs "
                    "WHERE stage='done' ORDER BY updated_at DESC LIMIT 500"
                ).fetchall()
                results = []
                for title, url, updated_at, cj_raw in rows:
                    if not cj_raw:
                        continue
                    try:
                        cj = _js.loads(cj_raw)
                        cats = cj.get("categories") or cj.get("category_ids") or []
                        if wp_cat_id in cats or str(wp_cat_id) in [str(x) for x in cats]:
                            results.append({
                                "title": title, "url": url,
                                "date": (updated_at or "")[:10]
                            })
                    except Exception:
                        pass
                if results:
                    return results[:limit]

            # ── Ruta 2: keyword sobre título ─────────────────────────────────
            words = [w.strip(".,;:()") for w in nombre.lower().split()
                     if len(w) > 3 and w.strip(".,;:()") not in _STOP][:4]
            if not words:
                words = nombre.lower().split()[:2]
            cond   = " OR ".join(f"LOWER(title) LIKE ?" for _ in words)
            params = [f"%{w}%" for w in words] + [limit]
            rows   = conn.execute(
                f"SELECT title, wp_url, updated_at FROM jobs "
                f"WHERE stage='done' AND ({cond}) ORDER BY updated_at DESC LIMIT ?",
                params
            ).fetchall()
            return [{"title": r[0], "url": r[1], "date": (r[2] or "")[:10]} for r in rows]

    except Exception as e:
        logger.warning(f"_edito_get_notas_tema: {e}")
        return []


def _edito_get_living_notes():
    import sqlite3 as _sq
    try:
        with _sq.connect(_EDITO_DB) as conn:
            conn.row_factory = _sq.Row
            rows = conn.execute("SELECT * FROM living_notes ORDER BY id").fetchall()
        return [dict(r) for r in rows]
    except Exception as e:
        logger.warning(f"_edito_get_living_notes: {e}")
        return []


# ── Keyboard builders ─────────────────────────────────────────────────────────

def _edito_main_kb(temas_count: int = 0, disc_count: int = 0) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton(f"📋 Temas ({temas_count})",          callback_data="edito_temas_0"),
            InlineKeyboardButton(f"🔭 Descubrimientos ({disc_count})", callback_data="edito_disc_0"),
        ],
        [
            InlineKeyboardButton("📌 Living notes", callback_data="edito_ln"),
            InlineKeyboardButton("➕ Agregar tema", callback_data="edito_add"),
        ],
        [
            InlineKeyboardButton("🗂 Seed desde WP",   callback_data="edito_seedwp"),
            InlineKeyboardButton("🔄 Scan publicados", callback_data="edito_rebuild"),
        ],
        [InlineKeyboardButton("❌ Cerrar", callback_data="edito_close")],
    ])


def _edito_temas_kb(temas: list, page: int) -> InlineKeyboardMarkup:
    start = page * _EDITO_PAGE_SIZE
    chunk = temas[start:start + _EDITO_PAGE_SIZE]
    rows = []
    for t in chunk:
        ne    = _NIVEL_EMOJI.get(t["nivel"], "📄")
        ee    = _ESTADO_EMOJI.get(t["estado"], "")
        nsub  = _edito_count_subtemas(t["id"])
        badge = f" [{nsub}]" if nsub else ""
        label = f"{ne}{ee} {t['nombre'][:28]}{badge}"
        rows.append([InlineKeyboardButton(label, callback_data=f"edito_tema_{t['id']}")])
    nav = []
    total_pages = max(1, (len(temas) + _EDITO_PAGE_SIZE - 1) // _EDITO_PAGE_SIZE)
    if page > 0:
        nav.append(InlineKeyboardButton("◀", callback_data=f"edito_temas_{page-1}"))
    if total_pages > 1:
        nav.append(InlineKeyboardButton(f"{page+1}/{total_pages}", callback_data="edito_noop"))
    if start + _EDITO_PAGE_SIZE < len(temas):
        nav.append(InlineKeyboardButton("▶", callback_data=f"edito_temas_{page+1}"))
    if nav:
        rows.append(nav)
    rows.append([InlineKeyboardButton("↩ Menú", callback_data="edito_main")])
    return InlineKeyboardMarkup(rows)


def _edito_tema_detail_kb(tema_id: int, nsub: int = 0, parent_id: int = None) -> InlineKeyboardMarkup:
    """KB unificado para temas raíz y subtemas. parent_id!=None indica que es subtema."""
    is_sub = parent_id is not None
    rows = [
        [
            InlineKeyboardButton("⏱ Temporalidad", callback_data=f"edito_nivel_{tema_id}"),
            InlineKeyboardButton("✏️ Renombrar",    callback_data=f"edito_rename_{tema_id}"),
        ]
    ]
    if nsub:
        rows.append([InlineKeyboardButton(
            f"📂 Subtemas ({nsub})", callback_data=f"edito_subtemas_{tema_id}"
        )])
    rows.append([InlineKeyboardButton(
        "📰 Ver notas publicadas", callback_data=f"edito_notas_{tema_id}"
    )])
    rows.append([
        InlineKeyboardButton("➕ Agregar subtema", callback_data=f"edito_addsub_{tema_id}"),
        InlineKeyboardButton("🔗 Unificar con...", callback_data=f"edito_unificar_{tema_id}"),
    ])
    if is_sub:
        rows.append([
            InlineKeyboardButton("⬆️ Hacer independiente", callback_data=f"edito_indep_{tema_id}"),
            InlineKeyboardButton("🔀 Cambiar padre",        callback_data=f"edito_chpadre_{tema_id}"),
        ])
    else:
        rows.append([InlineKeyboardButton(
            "🔀 Mover como subtema de...", callback_data=f"edito_chpadre_{tema_id}"
        )])
    rows.append([
        InlineKeyboardButton("❌ Cerrar",      callback_data=f"edito_cerrar_{tema_id}"),
        InlineKeyboardButton("🚫 Lista negra", callback_data=f"edito_negra_{tema_id}"),
    ])
    back_cb = f"edito_subtemas_{parent_id}" if is_sub else "edito_temas_0"
    rows.append([InlineKeyboardButton("↩ Volver", callback_data=back_cb)])
    return InlineKeyboardMarkup(rows)


def _edito_subtemas_kb(subtemas: list, parent_id: int) -> InlineKeyboardMarkup:
    rows = []
    for st in subtemas:
        ne    = _NIVEL_EMOJI.get(st["nivel"], "📄")
        ee    = _ESTADO_EMOJI.get(st["estado"], "")
        label = f"{ne}{ee} {st['nombre'][:32]}  · {st.get('n_notas',0)} notas"
        rows.append([InlineKeyboardButton(label, callback_data=f"edito_tema_{st['id']}")])
    rows.append([
        InlineKeyboardButton("➕ Agregar subtema", callback_data=f"edito_addsub_{parent_id}"),
        InlineKeyboardButton("↩ Volver",          callback_data=f"edito_tema_{parent_id}"),
    ])
    return InlineKeyboardMarkup(rows)


def _edito_chpadre_kb(subtema_id: int, temas_raiz: list) -> InlineKeyboardMarkup:
    rows = []
    for t in temas_raiz[:10]:
        ne    = _NIVEL_EMOJI.get(t["nivel"], "📄")
        label = f"{ne} {t['nombre'][:30]}"
        rows.append([InlineKeyboardButton(
            label, callback_data=f"edito_setpadre_{subtema_id}_{t['id']}"
        )])
    rows.append([InlineKeyboardButton("↩ Cancelar", callback_data=f"edito_tema_{subtema_id}")])
    return InlineKeyboardMarkup(rows)


def _edito_unificar_kb(src_id: int, temas: list) -> InlineKeyboardMarkup:
    rows = []
    for t in temas[:10]:
        ne    = _NIVEL_EMOJI.get(t["nivel"], "📄")
        label = f"{ne} {t['nombre'][:30]}"
        rows.append([InlineKeyboardButton(
            label, callback_data=f"edito_unif_{src_id}_{t['id']}"
        )])
    rows.append([InlineKeyboardButton("↩ Cancelar", callback_data=f"edito_tema_{src_id}")])
    return InlineKeyboardMarkup(rows)


def _edito_nivel_kb(tema_id: int) -> InlineKeyboardMarkup:
    rows, row = [], []
    for n in _NIVEL_LABELS:
        e = _NIVEL_EMOJI.get(n, "")
        row.append(InlineKeyboardButton(f"{e} {n}", callback_data=f"edito_setnivel_{tema_id}_{n}"))
        if len(row) == 3:
            rows.append(row); row = []
    if row:
        rows.append(row)
    rows.append([InlineKeyboardButton("↩ Volver", callback_data=f"edito_tema_{tema_id}")])
    return InlineKeyboardMarkup(rows)


def _edito_disc_kb(job_id: int, pos: int, total: int) -> InlineKeyboardMarkup:
    nav = []
    if pos > 0:
        nav.append(InlineKeyboardButton("◀", callback_data=f"edito_disc_{pos-1}"))
    nav.append(InlineKeyboardButton(f"{pos+1}/{total}", callback_data="edito_noop"))
    if pos < total - 1:
        nav.append(InlineKeyboardButton("▶", callback_data=f"edito_disc_{pos+1}"))
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("✅ Enviar a briefing", callback_data=f"edito_discok_{job_id}_{pos}"),
            InlineKeyboardButton("🗑 Descartar",         callback_data=f"edito_discdel_{job_id}_{pos}"),
        ],
        nav,
        [InlineKeyboardButton("↩ Menú", callback_data="edito_main")],
    ])


def _edito_add_nivel_kb() -> InlineKeyboardMarkup:
    rows, row = [], []
    for n in _NIVEL_LABELS:
        e = _NIVEL_EMOJI.get(n, "")
        row.append(InlineKeyboardButton(f"{e} {n}", callback_data=f"edito_addnivel_{n}"))
        if len(row) == 3:
            rows.append(row); row = []
    if row:
        rows.append(row)
    rows.append([InlineKeyboardButton("↩ Cancelar", callback_data="edito_main")])
    return InlineKeyboardMarkup(rows)


# ── Text helpers ──────────────────────────────────────────────────────────────

def _edito_main_text(temas_count: int, disc_count: int) -> str:
    return (
        f"🗞 <b>EDITOR EDITORIAL</b>\n\n"
        f"• Temas de agenda: <b>{temas_count}</b>\n"
        f"• Descubrimientos pendientes: <b>{disc_count}</b>"
    )


def _edito_temas_text(temas: list, page: int) -> str:
    total = len(temas)
    if not total:
        return "📋 <b>Temas activos</b>\n\nNo hay temas aún. Usá 🔄 Actualizar temas."
    start = page * _EDITO_PAGE_SIZE
    end   = min(start + _EDITO_PAGE_SIZE, total)
    lines = [f"📋 <b>Temas de agenda</b> ({start+1}–{end} de {total})\n"]
    for t in temas[start:end]:
        ne    = _NIVEL_EMOJI.get(t["nivel"], "📄")
        ee    = _ESTADO_EMOJI.get(t["estado"], "")
        nivel = t.get("nivel", "dia")
        lines.append(f"{ne}{ee} <b>{t['nombre']}</b>\n    ⏱ {nivel} · {t.get('n_notas',0)} notas")
    return "\n".join(lines)


def _edito_tema_detail_text(t: dict, nsub: int = 0, parent_nombre: str = "") -> str:
    ne      = _NIVEL_EMOJI.get(t["nivel"], "📄")
    ee      = _ESTADO_EMOJI.get(t["estado"], "")
    pv      = (t.get("primera_vez") or "")[:10]
    uv      = (t.get("ultima_vez") or "")[:10]
    sub_txt = f"\nSubtemas: <b>{nsub}</b>" if nsub else ""
    pad_txt = f"\nSubtema de: <i>{parent_nombre}</i>" if parent_nombre else ""
    icono   = "🔹" if parent_nombre else "📌"
    return (
        f"{icono} <b>{t['nombre']}</b>{pad_txt}\n\n"
        f"Temporalidad: {ne} <b>{t['nivel']}</b>  |  Estado: {ee} {t['estado']}\n"
        f"Notas cubierta: <b>{t.get('n_notas',0)}</b>{sub_txt}\n"
        f"Primera nota: {pv or '—'}  |  Última: {uv or '—'}"
    )


def _edito_notas_text(notas: list, tema_nombre: str) -> str:
    if not notas:
        return f"📰 <b>{tema_nombre}</b>\n\nNo se encontraron notas publicadas sobre este tema."
    lines = [f"📰 <b>{tema_nombre}</b> — {len(notas)} notas relacionadas\n"]
    for n in notas:
        title = (n["title"] or "sin título")[:80]
        url   = n.get("url") or ""
        date  = n.get("date") or "—"
        entry = f"<a href=\"{url}\">{title}</a>" if url else title
        lines.append(f"• {entry}  <i>{date}</i>")
    return "\n".join(lines)


def _edito_subtemas_text(subtemas: list, parent_nombre: str) -> str:
    if not subtemas:
        return (
            f"📂 <b>Subtemas de «{parent_nombre}»</b>\n\n"
            f"No hay subtemas todavía. Usá ➕ para crear uno."
        )
    lines = [f"📂 <b>Subtemas de «{parent_nombre}»</b> ({len(subtemas)})\n"]
    for st in subtemas:
        ne    = _NIVEL_EMOJI.get(st["nivel"], "📄")
        ee    = _ESTADO_EMOJI.get(st["estado"], "")
        nivel = st.get("nivel", "dia")
        lines.append(f"{ne}{ee} <b>{st['nombre']}</b>\n    ⏱ {nivel} · {st.get('n_notas',0)} notas")
    return "\n".join(lines)


def _edito_disc_text(job: dict, pos: int, total: int) -> str:
    import json as _js
    title   = job.get("title") or "sin título"
    score   = round(job.get("score") or 0, 1)
    cj = {}
    try: cj = _js.loads(job.get("content_json") or "{}")
    except Exception: pass
    source  = cj.get("source_name") or "desconocida"
    excerpt = (cj.get("excerpt") or "")[:200]
    txt = (
        f"🔭 <b>Descubrimiento {pos+1}/{total}</b>\n\n"
        f"<b>{title}</b>\n"
        f"Fuente: {source}  |  Score: {score}"
    )
    if excerpt:
        txt += f"\n\n{excerpt}"
    return txt


def _edito_ln_text(notes: list) -> str:
    if not notes:
        return "📌 <b>Living Notes</b>\n\nNo hay living notes configuradas."
    from datetime import datetime as _dt
    lines = ["📌 <b>Living Notes</b>\n"]
    for ln in notes:
        last = ln.get("ultimo_update")
        if last:
            try:
                diff = _dt.utcnow() - _dt.fromisoformat(last)
                h = int(diff.total_seconds() // 3600)
                last_str = f"hace {h}h" if h < 48 else f"hace {h//24}d"
            except Exception:
                last_str = last[:10]
        else:
            last_str = "nunca"
        icon = "🟢" if ln.get("wp_post_id") else "⚪"
        lines.append(f"{icon} <b>{ln['tema']}</b> — {last_str}")
    return "\n".join(lines)


# ── /pipeline ─────────────────────────────────────────────────────────────────

# Condición SQL y etiqueta para cada estado del pipeline
_PIP_COND = {
    "ingesta":       "stage='ingesta'",
    "descubrimiento":"stage='descubrimiento'",
    "curado":        "stage='curado'",
    "cola":          "stage='cola'",
    "redaccion":     "stage='redaccion'",
    "revision":      "stage='revision'",
    "publicacion":   "stage='publicacion'",
    "sin_imagen":    "stage='sin_imagen'",
    "publinota":     "stage='publinota'",
    "programadas":   "stage='done' AND tg_pending=1",
    "sin_evaluar":   "stage='done' AND (tg_pending IS NULL OR tg_pending=0) AND lector_evaluated=0",
    "evaluadas":     "stage='done' AND lector_evaluated=1 AND lector_score IS NOT NULL",
    "rechazadas":    "stage='rejected'",
}
_PIP_LABEL = {
    "ingesta":       "🔵 Ingesta",
    "descubrimiento":"🔎 Descubrimiento",
    "curado":        "📋 Briefing",
    "cola":          "📌 Cola",
    "redaccion":     "✍️ Redacción",
    "revision":      "🛑 Frenadas",
    "publicacion":   "📤 Publicación",
    "sin_imagen":    "🖼️ Sin imagen",
    "publinota":     "💼 Comercial",
    "programadas":   "📅 Programadas",
    "sin_evaluar":   "⏳ Sin evaluar",
    "evaluadas":     "🔍 QA+Lector",
    "rechazadas":    "🚫 Rechazadas",
}


def _pipeline_stats() -> tuple:
    """Retorna (text, counts_dict) con el estado actual del pipeline."""
    import sqlite3 as _sq
    from datetime import datetime as _dt
    today = _dt.now().strftime("%Y-%m-%d")
    db = "/opt/me-harness/harness.db"
    with _sq.connect(db) as c:
        def n(cond): return c.execute(f"SELECT COUNT(*) FROM jobs WHERE {cond}").fetchone()[0]
        counts = {k: n(v) for k, v in _PIP_COND.items()}
        # Para rechazadas y evaluadas: solo hoy en el resumen
        counts["rechazadas_hoy"] = n(f"stage='rejected' AND DATE(updated_at)='{today}'")
        counts["evaluadas_hoy"]  = n(f"stage='done' AND lector_evaluated=1 AND lector_score IS NOT NULL AND DATE(updated_at)='{today}'")
        avg_r = c.execute(
            f"SELECT AVG(lector_score) FROM jobs WHERE stage='done' AND lector_evaluated=1 "
            f"AND lector_score IS NOT NULL AND DATE(updated_at)='{today}'"
        ).fetchone()
        avg = round((avg_r[0] or 0), 1)

    w  = " ⚠️" if counts["sin_imagen"] else ""
    rv = " ⚠️" if counts.get("revision") else ""
    lns = [f"📊 <b>Pipeline</b> — línea de trabajo · {today}\n"]
    lns += [
        f"🔵 Ingesta           <b>{counts['ingesta']}</b>",
        f"🔎 Descubrimiento    <b>{counts['descubrimiento']}</b>",
        f"📋 Curado            <b>{counts['curado']}</b>",
        f"📌 Cola              <b>{counts['cola']}</b>",
        f"✍️ Redacción         <b>{counts['redaccion']}</b>",
        f"🛑 Frenadas (rev.)   <b>{counts['revision']}</b>{rv}",
        f"📤 Publicación       <b>{counts['publicacion']}</b>",
        f"🖼️ Sin imagen        <b>{counts['sin_imagen']}</b>{w}",
        "──────────────────────────",
        f"📅 Programadas       <b>{counts['programadas']}</b>",
        f"⏳ Sin evaluar        <b>{counts['sin_evaluar']}</b>",
    ]
    eval_line = f"🔍 QA+Lector (hoy)   <b>{counts['evaluadas_hoy']}</b>"
    if avg and counts["evaluadas_hoy"]:
        eval_line += f"  · avg {avg}/10"
    lns.append(eval_line)
    lns.append(f"🚫 Rechazadas (hoy)  <b>{counts['rechazadas_hoy']}</b>")
    if counts.get("publinota"):
        lns.append(f"💼 Comercial         <b>{counts['publinota']}</b>")
    lns.append("\n<i>Tocá una etapa para ver las notas.</i>")
    return "\n".join(lns), counts


def _pipeline_kb(counts: dict) -> dict:
    """Keyboard con un botón por etapa mostrando el conteo."""
    def btn(key):
        label = _PIP_LABEL[key]
        c = counts.get(key, 0)
        warn = "⚠️ " if key == "sin_imagen" and c else ""
        return {"text": f"{warn}{label} ({c})", "callback_data": f"pip_stage_{key}"}

    rows = [
        [btn("ingesta"),     btn("descubrimiento")],
        [btn("curado"),      btn("cola")],
        [btn("redaccion"),   btn("revision")],
        [btn("publicacion"), btn("sin_imagen")],
        [btn("programadas"), btn("sin_evaluar")],
        [btn("evaluadas"),   btn("rechazadas")],
    ]
    if counts.get("publinota"):
        rows.append([btn("publinota")])
    rows.append([{"text": "🔄 Refrescar", "callback_data": "pip_refresh"}])
    return {"inline_keyboard": rows}


def _pipeline_stage_detail(key: str) -> str:
    """Muestra las últimas 10 notas de una etapa/estado específico."""
    import sqlite3 as _sq
    cond  = _PIP_COND.get(key)
    label = _PIP_LABEL.get(key, key)
    if not cond:
        return "Etapa desconocida."
    db = "/opt/me-harness/harness.db"
    is_eval = key == "evaluadas"
    is_prog = key == "programadas"
    with _sq.connect(db) as c:
        if is_eval:
            rows = c.execute(
                f"SELECT id, title, lector_score, updated_at FROM jobs WHERE {cond} "
                "ORDER BY updated_at DESC LIMIT 10"
            ).fetchall()
        elif is_prog:
            rows = c.execute(
                f"SELECT id, title, tg_pending_at, updated_at FROM jobs WHERE {cond} "
                "ORDER BY tg_pending_at ASC LIMIT 10"
            ).fetchall()
        else:
            rows = c.execute(
                f"SELECT id, title, updated_at FROM jobs WHERE {cond} "
                "ORDER BY updated_at DESC LIMIT 10"
            ).fetchall()

    if not rows:
        return f"{label}\n\nNo hay notas en esta etapa."

    lines = [f"<b>{label}</b> — últimas {len(rows)}\n"]
    for row in rows:
        jid   = row[0]
        title = (row[1] or "")[:52]
        if is_eval:
            score = row[2]
            sc_str = f"  ⭐{score}/10" if score else ""
            lines.append(f"#{jid} {title}{sc_str}")
        elif is_prog:
            sched = (row[2] or "")[:16].replace("T", " ")
            lines.append(f"#{jid} {title}\n      📅 {sched}")
        else:
            lines.append(f"#{jid} {title}")
    return "\n".join(lines)


async def cmd_pipeline(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Estado del pipeline editorial: cuántas notas hay en cada etapa."""
    msg = await update.message.reply_text("📊 Calculando…")
    try:
        text, counts = await asyncio.to_thread(_pipeline_stats)
        kb = _pipeline_kb(counts)
    except Exception as e:
        await msg.edit_text(f"❌ Error: {e}")
        return
    await msg.edit_text(text, parse_mode="HTML", reply_markup=kb)


# ── /rutina — tu día con el sistema + chequeo de salud en un toque ────────────
_RUTINA_TXT = (
    "📅 <b>Tu rutina con el sistema</b>\n\n"
    "El diario se hace solo. Vos sos el <b>editor</b>: aprobás, corregís rumbo y mirás que no se trabe.\n\n"
    "🔵 <b>Lo único obligatorio: los briefings</b>\n"
    "<b>08:00 · 12:00 · 18:00</b> → te llegan 5 notas. Marcás las que van y <b>Aprobar</b>. ~2 min c/u. "
    "Es lo único que frena la máquina si no lo hacés.\n\n"
    "👉 <b>A demanda</b> (1 toque cuando el sistema te consulta):\n"
    "• 🩺 03:00 — Supervisor: si propone un parche → 🔧 Generar → ves el código → ✅ Aplicar\n"
    "• 🚫 Nota frenada por el gate → <b>Publicar igual</b> o la dejás\n"
    "• 📅 Efeméride (08:15) → elegís esfuerzo de campaña\n"
    "• 📈 Impacto / 🎯 Título SEO → 1 toque\n"
    "• 🧭 Lunes → recos editoriales de la semana → aprobás\n\n"
    "🟢 <b>Corre sin vos:</b> cotizaciones, living notes, difusión a redes, hitos, auto-cura.\n\n"
    "🔎 <b>¿Cómo sé si está todo bien?</b>\n"
    "Apretá el botón de abajo cuando quieras. Y si algo se rompe, el sistema te avisa solo "
    "(silencio = todo bien). El detalle fino lo ves con /pipeline."
)


def _rutina_kb():
    return InlineKeyboardMarkup([[InlineKeyboardButton("🔎 ¿Está todo bien?", callback_data="rutina_check")]])


def _chequeo_rapido() -> str:
    """Chequeo de salud en criollo para /rutina. Reusa los sensores del auditor + RAM + servicio.
    Devuelve un semáforo simple: qué está bien y qué mirar."""
    import sys as _srx
    _srx.path.insert(0, "/opt/me-harness"); _srx.path.insert(0, "/opt/me-harness/agents")
    import datetime as _dt
    import subprocess as _sp
    try:
        from agents import auditor as _aud
        st = _aud._pipeline_stats()
    except Exception as e:
        return f"⚠️ No pude leer el estado ahora ({str(e)[:70]}). Probá de nuevo o /pipeline."
    hora = (_dt.datetime.utcnow() - _dt.timedelta(hours=3)).hour  # ARG (UTC-3)
    lineas, alertas = [], 0

    # 1. ¿Está publicando?
    p6, p24 = st.get("published_6h", 0), st.get("published_24h", 0)
    if p6 == 0 and 9 <= hora <= 22:
        lineas.append("🔴 <b>No publicó nada en 6h</b> (y es horario diurno) — algo se trabó"); alertas += 1
    else:
        lineas.append(f"✅ Publicando: {p6} en las últimas 6h ({p24} en el día)")

    # 2. ¿Sale a redes?
    dif, meta = st.get("stuck_difusion", 0), st.get("meta_queue_stuck", 0)
    if dif or meta:
        lineas.append(f"⚠️ <b>Difusión trabada</b>: {dif} nota(s) sin salir / {meta} en cola de FB·IG"); alertas += 1
    else:
        lineas.append("✅ Difusión a redes: al día")

    # 3. Cola del gate: la FRENADA es accionable (te espera); las rechazadas son descartes
    # normales — solo alarman si se disparan (gate muy duro / entra basura).
    rev, rech = st.get("revision_queue", 0), st.get("rechazada_queue", 0)
    if rev:
        extra = f" · {rech} rechazadas" if rech else ""
        lineas.append(f"⚠️ Gate: <b>{rev} frenada(s)</b> esperándote{extra}"); alertas += 1
    elif rech >= 15:
        lineas.append(f"⚠️ Gate: {rech} rechazadas acumuladas — fijate si está muy duro"); alertas += 1
    else:
        lineas.append(f"✅ Gate de calidad: {rev} frenadas, {rech} rechazadas (normal)")

    # 4. Jobs trabados (la auto-cura los destraba sola — informativo, no alarma dura)
    stuck = st.get("stuck_auto_jobs", 0)
    if stuck:
        lineas.append(f"ℹ️ {stuck} nota(s) trabada(s) &gt;2h — la auto-cura las resuelve sola (mirá si sigue mañana)")

    # 5. Newsletter (riesgo de exceder el cupo de envío)
    if (st.get("email_health") or {}).get("riesgo"):
        lineas.append("⚠️ <b>Newsletter</b>: una campaña supera el cupo por hora — puede fallar el excedente"); alertas += 1

    # 6. OpenAI: créditos/cuota. Es el sensor que faltaba — el 20/8 la cuenta se quedó SIN
    # CRÉDITOS y el sistema seguía "verde" mientras el redactor descartaba notas, no había
    # dedup ni transcripciones. Una llamada mínima (1 token) dice la verdad en 3 segundos.
    try:
        import requests as _rq
        _r = _rq.post("https://api.openai.com/v1/chat/completions",
                      headers={"Authorization": f"Bearer {OPENAI_API_KEY}"},
                      json={"model": "gpt-4o-mini", "max_tokens": 1,
                            "messages": [{"role": "user", "content": "ok"}]}, timeout=12)
        if _r.status_code == 200:
            lineas.append("✅ OpenAI: responde (créditos OK)")
        else:
            _err = ""
            try:
                _err = (_r.json().get("error") or {}).get("code") or (_r.json().get("error") or {}).get("message", "")
            except Exception:
                _err = _r.text[:60]
            if "credit" in str(_err).lower() or "quota" in str(_err).lower():
                lineas.append("🔴 <b>OpenAI SIN CRÉDITOS</b> — no se redacta, no hay dedup ni "
                              "transcripciones. Cargá saldo en platform.openai.com → billing")
            else:
                lineas.append(f"🔴 <b>OpenAI no responde</b> (HTTP {_r.status_code}: {str(_err)[:50]})")
            alertas += 1
    except Exception as _e_oai:
        lineas.append(f"⚠️ OpenAI: no pude chequear ({str(_e_oai)[:45]})"); alertas += 1

    # 7. Servidor: RAM + que el harness esté corriendo
    ram = None
    try:
        m = {}
        for l in open("/proc/meminfo"):
            pp = l.split(":")
            if len(pp) == 2:
                m[pp[0]] = int(pp[1].split()[0])
        tot, av = m.get("MemTotal", 1), m.get("MemAvailable", 0)
        ram = round(100 * (tot - av) / tot)
    except Exception:
        pass
    try:
        act = _sp.run(["systemctl", "is-active", "me-harness"], capture_output=True, text=True, timeout=10).stdout.strip()
    except Exception:
        act = "?"
    if act != "active":
        lineas.append(f"🔴 <b>El harness no está corriendo</b> (estado: {act}) — avisame"); alertas += 1
    elif ram is not None and ram >= 90:
        lineas.append(f"⚠️ <b>Servidor</b>: RAM {ram}% (alta)"); alertas += 1
    else:
        lineas.append(f"✅ Servidor: RAM {ram if ram is not None else '?'}% , todo corriendo")

    hhmm = (_dt.datetime.utcnow() - _dt.timedelta(hours=3)).strftime("%H:%M")
    if alertas == 0:
        head = f"🟢 <b>Todo en orden</b>  <i>({hhmm})</i>\n\n"
        foot = "\n\nNo tenés que hacer nada 👍"
    else:
        head = f"🟠 <b>Hay {alertas} cosa(s) para mirar</b>  <i>({hhmm})</i>\n\n"
        foot = "\n\nSi una alerta sigue después de un rato, avisame y lo miro."
    return head + "\n".join(lineas) + foot


async def cmd_rutina(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Tu rutina diaria con el sistema + botón de chequeo de salud."""
    await update.message.reply_text(_RUTINA_TXT, parse_mode="HTML",
                                    reply_markup=_rutina_kb(), disable_web_page_preview=True)


# ── Command + callback ────────────────────────────────────────────────────────

async def cmd_editor(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Menú del Editor editorial — agenda temática, descubrimientos, living notes."""
    temas = await asyncio.to_thread(_edito_get_temas)
    discs = await asyncio.to_thread(_edito_get_descubrimientos)
    await update.message.reply_text(
        _edito_main_text(len(temas), len(discs)),
        parse_mode="HTML",
        reply_markup=_edito_main_kb(len(temas), len(discs)),
    )


async def handle_edito_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    d = query.data

    if d == "edito_noop":
        return

    if d == "edito_close":
        await query.edit_message_text("🗞 Editor cerrado.")
        return

    # ── Actualizar temas ahora ────────────────────────────────────────────────
    if d == "edito_seedwp":
        await query.edit_message_text("🗂 Importando categorías de WordPress... (puede tardar ~15s)")
        import sys as _sys
        _sys.path.insert(0, "/opt/me-harness")
        try:
            from agents import editor as _ed
            result = await asyncio.to_thread(_ed.seed_temas_from_wp_categories)
            temas  = await asyncio.to_thread(_edito_get_temas)
            discs  = await asyncio.to_thread(_edito_get_descubrimientos)
            await query.edit_message_text(
                _edito_main_text(len(temas), len(discs)) +
                f"\n\n✅ <i>{result['raiz']} temas raíz + {result['sub']} subtemas importados desde WP</i>",
                parse_mode="HTML",
                reply_markup=_edito_main_kb(len(temas), len(discs)),
            )
        except Exception as e:
            await query.edit_message_text(f"❌ Error al importar: {e}")
        return

    if d == "edito_rebuild":
        await query.edit_message_text("🔄 Escaneando últimos 30 días... (puede tardar ~60s)")
        import sys as _sys
        _sys.path.insert(0, "/opt/me-harness")
        try:
            from agents import editor as _ed
            n      = await asyncio.to_thread(_ed.build_temas_from_published, 30)
            temas  = await asyncio.to_thread(_edito_get_temas)
            discs  = await asyncio.to_thread(_edito_get_descubrimientos)
            await query.edit_message_text(
                _edito_main_text(len(temas), len(discs)) +
                f"\n\n✅ <i>{n} temas procesados desde los últimos 30 días</i>",
                parse_mode="HTML",
                reply_markup=_edito_main_kb(len(temas), len(discs)),
            )
        except Exception as e:
            await query.edit_message_text(f"❌ Error al actualizar: {e}")
        return

    # ── Menú principal ────────────────────────────────────────────────────────
    if d == "edito_main":
        temas = await asyncio.to_thread(_edito_get_temas)
        discs = await asyncio.to_thread(_edito_get_descubrimientos)
        await query.edit_message_text(
            _edito_main_text(len(temas), len(discs)),
            parse_mode="HTML",
            reply_markup=_edito_main_kb(len(temas), len(discs)),
        )
        return

    # ── Lista de temas paginada ───────────────────────────────────────────────
    if d.startswith("edito_temas_"):
        page  = int(d.split("_")[2])
        temas = await asyncio.to_thread(_edito_get_temas)
        await query.edit_message_text(
            _edito_temas_text(temas, page),
            parse_mode="HTML",
            reply_markup=_edito_temas_kb(temas, page),
        )
        return

    # ── Detalle de un tema (raíz o subtema) ──────────────────────────────────
    if d.startswith("edito_tema_"):
        tema_id   = int(d.split("_")[2])
        t         = await asyncio.to_thread(_edito_get_tema, tema_id)
        if not t:
            await query.answer("Tema no encontrado", show_alert=True)
            return
        nsub      = await asyncio.to_thread(_edito_count_subtemas, tema_id)
        parent_id = t.get("parent_id")
        if parent_id:
            parent  = await asyncio.to_thread(_edito_get_tema, parent_id)
            pnombre = parent["nombre"] if parent else ""
        else:
            pnombre = ""
        await query.edit_message_text(
            _edito_tema_detail_text(t, nsub, pnombre),
            parse_mode="HTML",
            reply_markup=_edito_tema_detail_kb(tema_id, nsub, parent_id),
        )
        return

    # ── Lista de subtemas de un tema ──────────────────────────────────────────
    if d.startswith("edito_subtemas_"):
        parent_id = int(d.split("_")[2])
        parent    = await asyncio.to_thread(_edito_get_tema, parent_id)
        subtemas  = await asyncio.to_thread(_edito_get_subtemas, parent_id)
        pnombre   = parent["nombre"] if parent else f"#{parent_id}"
        await query.edit_message_text(
            _edito_subtemas_text(subtemas, pnombre),
            parse_mode="HTML",
            reply_markup=_edito_subtemas_kb(subtemas, parent_id),
        )
        return

    # ── Hacer independiente ───────────────────────────────────────────────────
    if d.startswith("edito_indep_"):
        tema_id = int(d.split("_")[2])
        await asyncio.to_thread(_edito_make_independent, tema_id)
        await query.answer("⬆️ Ahora es tema independiente", show_alert=False)
        t    = await asyncio.to_thread(_edito_get_tema, tema_id)
        nsub = await asyncio.to_thread(_edito_count_subtemas, tema_id)
        await query.edit_message_text(
            _edito_tema_detail_text(t, nsub),
            parse_mode="HTML",
            reply_markup=_edito_tema_detail_kb(tema_id, nsub, None),
        )
        return

    # ── Selector de nuevo padre ───────────────────────────────────────────────
    if d.startswith("edito_chpadre_"):
        tema_id    = int(d.split("_")[2])
        t          = await asyncio.to_thread(_edito_get_tema, tema_id)
        nombre     = t["nombre"] if t else f"#{tema_id}"
        temas_raiz = await asyncio.to_thread(_edito_get_temas)
        temas_raiz = [x for x in temas_raiz if x["id"] != tema_id]
        await query.edit_message_text(
            f"🔀 <b>«{nombre}»</b> → elegí el tema padre:",
            parse_mode="HTML",
            reply_markup=_edito_chpadre_kb(tema_id, temas_raiz),
        )
        return

    if d.startswith("edito_setpadre_"):
        parts      = d.split("_")
        tema_id    = int(parts[2])
        parent_id  = int(parts[3])
        await asyncio.to_thread(_edito_set_parent, tema_id, parent_id)
        parent  = await asyncio.to_thread(_edito_get_tema, parent_id)
        pnombre = parent["nombre"] if parent else f"#{parent_id}"
        await query.answer(f"✅ Padre: «{pnombre}»", show_alert=False)
        t    = await asyncio.to_thread(_edito_get_tema, tema_id)
        nsub = await asyncio.to_thread(_edito_count_subtemas, tema_id)
        await query.edit_message_text(
            _edito_tema_detail_text(t, nsub, pnombre),
            parse_mode="HTML",
            reply_markup=_edito_tema_detail_kb(tema_id, nsub, parent_id),
        )
        return

    # ── Renombrar tema ────────────────────────────────────────────────────────
    if d.startswith("edito_rename_"):
        tema_id = int(d.split("_")[2])
        t = await asyncio.to_thread(_edito_get_tema, tema_id)
        nombre = t["nombre"] if t else f"#{tema_id}"
        context.user_data["edito_rename_id"] = tema_id
        await query.edit_message_text(
            f"✏️ <b>Renombrar tema</b>\n\nNombre actual: <i>{nombre}</i>\n\nEscribí el nuevo nombre:",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("↩ Cancelar", callback_data=f"edito_tema_{tema_id}")]
            ]),
        )
        return

    # ── Cambiar nivel / temporalidad ─────────────────────────────────────────
    if d.startswith("edito_nivel_"):
        tema_id = int(d.split("_")[2])
        t = await asyncio.to_thread(_edito_get_tema, tema_id)
        nombre = t["nombre"] if t else f"#{tema_id}"
        await query.edit_message_text(
            f"🔄 <b>{nombre}</b>\n\nElegí el nuevo nivel temporal:",
            parse_mode="HTML",
            reply_markup=_edito_nivel_kb(tema_id),
        )
        return

    if d.startswith("edito_setnivel_"):
        parts   = d.split("_")
        tema_id = int(parts[2])
        nivel   = parts[3]
        await asyncio.to_thread(_edito_set_tema_nivel, tema_id, nivel)
        t         = await asyncio.to_thread(_edito_get_tema, tema_id)
        nsub      = await asyncio.to_thread(_edito_count_subtemas, tema_id)
        parent_id = t.get("parent_id") if t else None
        pnombre   = ""
        if parent_id:
            p = await asyncio.to_thread(_edito_get_tema, parent_id)
            pnombre = p["nombre"] if p else ""
        if t:
            await query.edit_message_text(
                _edito_tema_detail_text(t, nsub, pnombre),
                parse_mode="HTML",
                reply_markup=_edito_tema_detail_kb(tema_id, nsub, parent_id),
            )
        await query.answer(f"✅ Temporalidad → {nivel}", show_alert=False)
        return

    # ── Cerrar tema ───────────────────────────────────────────────────────────
    if d.startswith("edito_cerrar_"):
        tema_id = int(d.split("_")[2])
        await asyncio.to_thread(_edito_set_tema_estado, tema_id, "cerrado")
        temas = await asyncio.to_thread(_edito_get_temas)
        discs = await asyncio.to_thread(_edito_get_descubrimientos)
        await query.edit_message_text(
            _edito_main_text(len(temas), len(discs)),
            parse_mode="HTML",
            reply_markup=_edito_main_kb(len(temas), len(discs)),
        )
        await query.answer("⛔ Tema cerrado", show_alert=False)
        return

    # ── Lista negra ───────────────────────────────────────────────────────────
    if d.startswith("edito_negra_"):
        tema_id = int(d.split("_")[2])
        await asyncio.to_thread(_edito_set_tema_estado, tema_id, "lista_negra")
        temas = await asyncio.to_thread(_edito_get_temas)
        discs = await asyncio.to_thread(_edito_get_descubrimientos)
        await query.edit_message_text(
            _edito_main_text(len(temas), len(discs)),
            parse_mode="HTML",
            reply_markup=_edito_main_kb(len(temas), len(discs)),
        )
        await query.answer("🚫 En lista negra — no atraerá más material", show_alert=False)
        return

    # ── Descubrimientos — acciones ok/del ────────────────────────────────────
    if d.startswith("edito_discok_") or d.startswith("edito_discdel_"):
        is_ok   = d.startswith("edito_discok_")
        parts   = d.split("_")
        job_id  = int(parts[2])
        pos     = int(parts[3])
        if is_ok:
            await asyncio.to_thread(_edito_promote_disc, job_id)
            await query.answer("✅ Enviado a briefing", show_alert=False)
        else:
            await asyncio.to_thread(_edito_reject_disc, job_id)
            await query.answer("🗑 Descartado", show_alert=False)
        discs = await asyncio.to_thread(_edito_get_descubrimientos)
        if not discs:
            temas = await asyncio.to_thread(_edito_get_temas)
            await query.edit_message_text(
                _edito_main_text(len(temas), 0),
                parse_mode="HTML",
                reply_markup=_edito_main_kb(len(temas), 0),
            )
        else:
            new_pos = min(pos, len(discs) - 1)
            job     = discs[new_pos]
            await query.edit_message_text(
                _edito_disc_text(job, new_pos, len(discs)),
                parse_mode="HTML",
                reply_markup=_edito_disc_kb(job["id"], new_pos, len(discs)),
            )
        return

    # ── Descubrimientos — navegar ─────────────────────────────────────────────
    if d.startswith("edito_disc_"):
        pos   = int(d.split("_")[2])
        discs = await asyncio.to_thread(_edito_get_descubrimientos)
        if not discs:
            await query.edit_message_text(
                "🔭 No hay descubrimientos pendientes.",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("↩ Menú", callback_data="edito_main")]
                ]),
            )
            return
        pos = max(0, min(pos, len(discs) - 1))
        job = discs[pos]
        await query.edit_message_text(
            _edito_disc_text(job, pos, len(discs)),
            parse_mode="HTML",
            reply_markup=_edito_disc_kb(job["id"], pos, len(discs)),
        )
        return

    # ── Living notes ──────────────────────────────────────────────────────────
    if d == "edito_ln":
        notes = await asyncio.to_thread(_edito_get_living_notes)
        await query.edit_message_text(
            _edito_ln_text(notes),
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("↩ Menú", callback_data="edito_main")]
            ]),
        )
        return

    # ── Ver notas publicadas de un tema ──────────────────────────────────────
    if d.startswith("edito_notas_"):
        tema_id = int(d.split("_")[2])
        t       = await asyncio.to_thread(_edito_get_tema, tema_id)
        notas   = await asyncio.to_thread(_edito_get_notas_tema, tema_id)
        nombre  = t["nombre"] if t else f"#{tema_id}"
        parent_id = t.get("parent_id") if t else None
        await query.edit_message_text(
            _edito_notas_text(notas, nombre),
            parse_mode="HTML",
            disable_web_page_preview=True,
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("↩ Volver", callback_data=f"edito_tema_{tema_id}")]
            ]),
        )
        return

    # ── Agregar subtema ───────────────────────────────────────────────────────
    if d.startswith("edito_addsub_"):
        parent_id = int(d.split("_")[2])
        parent    = await asyncio.to_thread(_edito_get_tema, parent_id)
        pnombre   = parent["nombre"] if parent else f"#{parent_id}"
        context.user_data["edito_add_tema"]      = True
        context.user_data["edito_add_parent_id"] = parent_id
        await query.edit_message_text(
            f"➕ <b>Nuevo subtema de «{pnombre}»</b>\n\nEscribí el nombre del subtema:",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("↩ Cancelar", callback_data=f"edito_tema_{parent_id}")]
            ]),
        )
        return

    # ── Unificar — selector ───────────────────────────────────────────────────
    if d.startswith("edito_unificar_"):
        src_id  = int(d.split("_")[2])
        src     = await asyncio.to_thread(_edito_get_tema, src_id)
        snombre = src["nombre"] if src else f"#{src_id}"
        temas   = await asyncio.to_thread(_edito_get_temas)
        temas   = [t for t in temas if t["id"] != src_id]
        await query.edit_message_text(
            f"🔗 <b>Unificar «{snombre}» dentro de...</b>\n\n"
            f"El tema seleccionado absorbe a «{snombre}» (sus notas y subtemas pasan al destino):",
            parse_mode="HTML",
            reply_markup=_edito_unificar_kb(src_id, temas),
        )
        return

    # ── Unificar — confirmar ──────────────────────────────────────────────────
    if d.startswith("edito_unif_"):
        parts  = d.split("_")
        src_id = int(parts[2])
        dst_id = int(parts[3])
        await asyncio.to_thread(_edito_unificar, src_id, dst_id)
        dst = await asyncio.to_thread(_edito_get_tema, dst_id)
        dname = dst["nombre"] if dst else f"#{dst_id}"
        await query.answer(f"✅ Unificado en «{dname}»", show_alert=False)
        nsub = await asyncio.to_thread(_edito_count_subtemas, dst_id)
        await query.edit_message_text(
            _edito_tema_detail_text(dst, nsub) if dst else "Tema destino no encontrado.",
            parse_mode="HTML",
            reply_markup=_edito_tema_detail_kb(dst_id, nsub) if dst else None,
        )
        return

    # ── Agregar tema ──────────────────────────────────────────────────────────
    if d == "edito_add":
        context.user_data["edito_add_tema"] = True
        await query.edit_message_text(
            "➕ <b>Agregar tema</b>\n\nEscribí el nombre del tema en el chat:",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("↩ Cancelar", callback_data="edito_main")]
            ]),
        )
        return

    if d.startswith("edito_addnivel_"):
        nivel     = d.split("_")[2]
        nombre    = context.user_data.pop("edito_add_nombre", None)
        parent_id = context.user_data.pop("edito_add_parent_id", None)
        if not nombre:
            await query.answer("No encontré el nombre del tema", show_alert=True)
            return
        tid = await asyncio.to_thread(_edito_upsert_tema, nombre, nivel, parent_id)
        await query.answer(f"✅ «{nombre}» creado como {nivel}", show_alert=False)
        if parent_id:
            # Volver al detalle del padre
            parent  = await asyncio.to_thread(_edito_get_tema, parent_id)
            nsub    = await asyncio.to_thread(_edito_count_subtemas, parent_id)
            pparent = parent.get("parent_id") if parent else None
            await query.edit_message_text(
                _edito_tema_detail_text(parent, nsub) if parent else "✅ Subtema creado.",
                parse_mode="HTML",
                reply_markup=_edito_tema_detail_kb(parent_id, nsub, pparent) if parent else None,
            )
        else:
            temas = await asyncio.to_thread(_edito_get_temas)
            discs = await asyncio.to_thread(_edito_get_descubrimientos)
            await query.edit_message_text(
                _edito_main_text(len(temas), len(discs)),
                parse_mode="HTML",
                reply_markup=_edito_main_kb(len(temas), len(discs)),
            )
        return


async def _post_init(application: Application) -> None:
    from telegram import BotCommand
    await application.bot.set_my_commands([
        # ── Pipeline editorial ────────────────────────────────────────────────
        BotCommand("rutina",            "Tu rutina diaria + chequeo de salud en un toque"),
        BotCommand("pipeline",          "Estado del pipeline editorial — todas las etapas"),
        BotCommand("programadas",       "Notas programadas — reprogramar o publicar ahora"),
        BotCommand("eventos",           "Agenda — boletín semanal + efemérides que vienen"),
        BotCommand("briefing",          "Briefing manual — /briefing (notas) · /briefing auto (config)"),
        BotCommand("ciclaje",           "Briefing auto — notas por corrida + horarios (= /briefing auto)"),
        BotCommand("coladepublicacion", "Cola de publicación — confirmar destinos"),
        BotCommand("editor",            "Editor — agenda temática, descubrimientos, living notes"),
        BotCommand("ingesta",           "Disparar ingesta manual de fuentes RSS"),
        BotCommand("nutricion",         "Briefing de nutrición — alimentar living notes"),
        # ── Contenido ─────────────────────────────────────────────────────────
        BotCommand("vivo",              "Cobertura EN VIVO — nota + redes, todo desde el bot"),
        BotCommand("input_evento",      "Tandas EN VIVO — texto, audios y fotos para la crónica"),
        BotCommand("evento",            "Cobertura de evento propio — audios, fotos y texto → nota"),
        BotCommand("campania",          "Campaña de evento en curso — revivir, armar o cancelar"),
        BotCommand("frases",            "Crear nota con frase inspiradora + imagen"),
        BotCommand("notamanual",        "Cargar columna de autor (PDF o texto) y publicarla"),
        BotCommand("publicador",        "Gestionar nota publicada — links, republicar, borrar"),
        BotCommand("editar",            "Editar una nota ya publicada"),
        # ── Información ───────────────────────────────────────────────────────
        BotCommand("stats",             "Estadísticas del día (publicadas, errores)"),
        # ── Configuración ─────────────────────────────────────────────────────
        BotCommand("fuentes",           "Gestionar fuentes RSS del EDITOR"),
    ])
    logger.info("BotCommands registrados en Telegram")


async def on_finde_approval_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Botones del resumen finde (8AM): aprobar / no publicar / corregir / reprogramar / cancelar.
    Escribe la decisión en harness.db (tabla finde_approval); el harness postea según eso."""
    import sqlite3 as _sqf
    from datetime import datetime as _dtf
    query = update.callback_query
    await query.answer()
    try:
        action, fdate = query.data.split(":", 1)
    except ValueError:
        return
    action = action[len("fap_"):]
    HDB = "/opt/me-harness/harness.db"

    def _set(**fields):
        fields["updated_at"] = _dtf.utcnow().isoformat()
        with _sqf.connect(HDB) as conn:
            ex = conn.execute("SELECT 1 FROM finde_approval WHERE date=?", (fdate,)).fetchone()
            if ex:
                cols = ", ".join(f"{k}=?" for k in fields)
                conn.execute(f"UPDATE finde_approval SET {cols} WHERE date=?", (*fields.values(), fdate))
            else:
                keys = ["date"] + list(fields)
                conn.execute(
                    f"INSERT INTO finde_approval ({','.join(keys)}) VALUES ({','.join('?' * len(keys))})",
                    (fdate, *fields.values()))

    def _hora():
        with _sqf.connect(HDB) as conn:
            r = conn.execute("SELECT publish_at FROM finde_approval WHERE date=?", (fdate,)).fetchone()
        return ((r[0] if r and r[0] else "") or "")[11:16] or "09:00"

    if action == "aprobar":
        _set(status="aprobado")
        try: await query.edit_message_reply_markup(reply_markup=None)
        except Exception: pass
        await query.message.reply_text(f"✅ Aprobado — el resumen sale al canal a las {_hora()}.")
    elif action == "nopub":
        _set(status="no_publicar")
        try: await query.edit_message_reply_markup(reply_markup=None)
        except Exception: pass
        await query.message.reply_text("🚫 Listo, el resumen NO se publica en el canal.")
    elif action == "cancel":
        _set(status="cancelado")
        try: await query.edit_message_reply_markup(reply_markup=None)
        except Exception: pass
        await query.message.reply_text(f"✖️ Cerrado. Sigue todo igual: sale a las {_hora()}.")
    elif action == "corregir":
        context.user_data["awaiting_finde_corr"] = fdate
        await query.message.reply_text("✏️ Escribí las recomendaciones para rehacer el texto del resumen:")
    elif action == "reprog":
        context.user_data["awaiting_finde_reprog"] = fdate
        await query.message.reply_text("🕘 Escribí la nueva hora en formato HH:MM (ej: 10:30):")


# ════════════════════════════════════════════════════════════════════════════
# /notamanual — Columna de autor (contenido 100% propio de ME): PDF o texto pegado.
# Detecta título, bajada, autor, cuerpo, categoría, etiquetas y hashtags, y la
# maqueta con el MISMO esquema de redacción (_gpt_format_article) y SEO/publicación
# (publicador del harness vía stage='publicacion'). No inventa ni altera el contenido.
# ════════════════════════════════════════════════════════════════════════════

def _pdf_to_text(path: str):
    """Devuelve (texto, n_imagenes). texto=None si no hay librería; '' si falla."""
    Reader = None
    try:
        from pypdf import PdfReader as Reader
    except ImportError:
        try:
            from PyPDF2 import PdfReader as Reader
        except ImportError:
            return None, 0
    try:
        reader = Reader(path)
        text = "\n".join((pg.extract_text() or "") for pg in reader.pages).strip()
        nimg = 0
        try:
            for pg in reader.pages:
                nimg += len(getattr(pg, "images", []) or [])
        except Exception:
            nimg = 0
        return text, nimg
    except Exception as e:
        logger.warning(f"_pdf_to_text error: {e}")
        return "", 0


def _gpt_nota_manual_extract(raw_text: str):
    """GPT separa la columna cruda en campos. Preserva la voz del autor; no inventa."""
    if not OPENAI_API_KEY:
        return None
    prompt = (
        "Sos el editor de MundoEmpresarial.ar. Te paso el texto COMPLETO de una columna de "
        "opinión escrita por un empresario o lector (contenido propio, NO de otra web). "
        "Devolvé SOLO un JSON con esta forma exacta:\n"
        '{\n'
        '  "title": "título de la nota; si el texto trae uno usalo, si no proponé uno fiel y sin clickbait",\n'
        '  "bajada": "1-2 oraciones que resuman el enfoque de la columna",\n'
        '  "autor": "nombre del autor y, si aparece, su cargo/empresa (ej: Juan Perez, presidente de CAME); si no se identifica, vacio",\n'
        '  "cuerpo": "el cuerpo de la columna TAL CUAL, palabra por palabra, SIN reescribir, SIN '
        'resumir y SIN cambiar el orden; solo sacá el título y la firma. Mantené los párrafos '
        '(dejá una línea en blanco entre párrafos) y los subtítulos que el autor haya puesto (uno por línea)",\n'
        '  "tags": ["3 a 6 etiquetas: entidades, personas y temas mencionados"],\n'
        '  "hashtags": ["3 a 5 hashtags para redes, sin espacios, ej #Pymes"]\n'
        '}\n'
        "REGLAS: No inventes datos. No cambies la postura ni la tesis del autor. "
        "Espanol rioplatense. Devolve SOLO el JSON.\n\n"
        "TEXTO:\n" + raw_text[:9000]
    )
    try:
        r = openai_post(
            "https://api.openai.com/v1/chat/completions",
            headers={"Authorization": f"Bearer {OPENAI_API_KEY}", "Content-Type": "application/json"},
            json={"model": "gpt-4o-mini",
                  "messages": [{"role": "user", "content": prompt}],
                  "temperature": 0.2,
                  "response_format": {"type": "json_object"}},
            timeout=60,
        )
        if r.status_code != 200:
            logger.warning(f"_gpt_nota_manual_extract {r.status_code}: {r.text[:200]}")
            return None
        raw = r.json()["choices"][0]["message"]["content"].strip()
        raw = re.sub(r'^```(?:json)?\s*', '', raw)
        raw = re.sub(r'\s*```$', '', raw).strip()
        d = json.loads(raw)
        if not d.get("title") or not d.get("cuerpo"):
            return None
        return d
    except Exception as e:
        logger.warning(f"_gpt_nota_manual_extract error: {e}")
        return None


_CAT_NAME_CACHE = {}
def _cat_names(ids: list) -> str:
    if not ids:
        return "—"
    missing = [i for i in ids if i not in _CAT_NAME_CACHE]
    if missing:
        try:
            r = requests.get(f"{WP_URL}/wp-json/wp/v2/categories",
                             params={"include": ",".join(map(str, missing)),
                                     "_fields": "id,name", "per_page": 100}, timeout=15)
            for c in r.json():
                _CAT_NAME_CACHE[c["id"]] = c["name"]
        except Exception:
            pass
    return ", ".join(_CAT_NAME_CACHE.get(i, str(i)) for i in ids)


def _cols_det_html(blocks: list) -> dict:
    """Formateo determinístico: cada bloque → <p>; líneas cortas sin puntuación de cierre → <h2>."""
    import html as _h
    parts, h2s = [], []
    for b in blocks:
        b = b.strip()
        if not b:
            continue
        es_titulo = ("\n" not in b and len(b) <= 80
                     and not b.rstrip().endswith((".", ":", "?", "!", "…", ",", ";", '"', "»", ")")))
        if es_titulo:
            h2s.append(b)
            parts.append(f'<h2 id="{url_slug(b) or "seccion"}">{_h.escape(b)}</h2>')
        else:
            parts.append("<p>" + "<br>".join(_h.escape(l) for l in b.split("\n")) + "</p>")
    return {"html": "".join(parts), "h2_headings": h2s, "bullets": []}


def _cols_estructura_verbatim(cuerpo: str) -> dict | None:
    """Un solo bloque grande → GPT inserta SOLO saltos de párrafo (sin cambiar palabras).
    Guard VERBATIM: si el texto de salida no tiene EXACTAMENTE las mismas palabras en el mismo
    orden, se descarta (no queremos reescritura). None si no se puede garantizar la fidelidad."""
    if not OPENAI_API_KEY:
        return None
    prompt = ("Te doy el cuerpo de una columna de opinión escrita como un solo bloque. Tu ÚNICA tarea "
              "es insertar SALTOS DE PÁRRAFO donde cambia la idea, para que se lea cómodo. "
              "REGLA ABSOLUTA: NO cambies, agregues, saques ni reordenes NINGUNA palabra. NO resumas. "
              "NO agregues títulos ni bullets. Devolvé EXACTAMENTE el mismo texto, palabra por palabra, "
              "solo con una línea en blanco entre párrafos.\n\nTEXTO:\n" + cuerpo[:9000])
    try:
        r = openai_post(
            "https://api.openai.com/v1/chat/completions",
            headers={"Authorization": f"Bearer {OPENAI_API_KEY}", "Content-Type": "application/json"},
            json={"model": "gpt-4o-mini", "messages": [{"role": "user", "content": prompt}],
                  "temperature": 0},
            timeout=60)
        if r.status_code != 200:
            return None
        out = r.json()["choices"][0]["message"]["content"].strip()
        w = lambda s: re.findall(r"\w+", (s or "").lower())
        if w(out) != w(cuerpo):           # guard verbatim: mismas palabras, mismo orden
            logger.warning("columna verbatim guard: GPT alteró palabras → formateo determinístico")
            return None
        blocks = [b.strip() for b in re.split(r"\n\s*\n", out) if b.strip()]
        return _cols_det_html(blocks)
    except Exception as e:
        logger.warning(f"_cols_estructura_verbatim: {e}")
        return None


def _format_columna_html(cuerpo: str) -> dict:
    """Formatea una columna de autor SIN reescribirla (preserva las palabras). Si ya viene en
    párrafos, los respeta; si es un solo bloque grande, inserta saltos de párrafo con GPT
    verbatim-guarded (no cambia ni una palabra). Fix 2026-07-15."""
    cuerpo = (cuerpo or "").strip()
    blocks = [b.strip() for b in re.split(r"\n\s*\n", cuerpo) if b.strip()]
    if len(blocks) >= 3:                   # el autor ya lo separó en párrafos → respetar
        return _cols_det_html(blocks)
    if len(cuerpo) > 700:                  # bloque grande sin párrafos → estructurar (verbatim)
        g = _cols_estructura_verbatim(cuerpo)
        if g:
            return g
    return _cols_det_html(blocks or [cuerpo])


def _build_nota_manual_data(ex: dict, modo: str = "tal_cual", enfoque: str = "") -> dict:
    """Arma el data listo para publicar. `modo`:
    - 'tal_cual' (recomendado): PRESERVA el texto del autor, solo lo formatea (sin reescribir).
    - 'estilo_me': lo reescribe al formato del diario (bullets + secciones + resumen pymes).
      `enfoque`: ángulo pedido por el editor (capa del briefing o escrito a mano + ajustes)."""
    title  = (ex.get("title") or "").strip()
    bajada = (ex.get("bajada") or "").strip()
    autor  = (ex.get("autor") or "").strip()
    cuerpo = (ex.get("cuerpo") or "").strip()
    kw = focus_keyword(title)
    _fallback = "<p>" + "</p><p>".join(p.strip() for p in cuerpo.split("\n\n") if p.strip()) + "</p>"
    if modo == "estilo_me":
        instr = (f"Es una COLUMNA DE OPINIÓN firmada por {autor or 'un lector'}. Respetá su tesis y "
                 "postura; estructurá en secciones, agregá bullets y resumen para pymes.")
        if enfoque:
            instr += ("\nENFOQUE PEDIDO POR EL EDITOR (obligatorio, manda sobre lo anterior): "
                      + enfoque)
        fmt = _gpt_format_article(title, cuerpo, source_url="columna-de-autor", kw=kw,
                                  redactor_instr=instr) or {}
        body_html = fmt.get("html") or _fallback
        bullets = fmt.get("bullets", []) or []
        h2      = fmt.get("h2_headings", []) or []
    else:
        fmt = _format_columna_html(cuerpo)
        body_html = fmt["html"] or _fallback
        bullets = fmt["bullets"]
        h2      = fmt["h2_headings"]
    if autor:
        byline = ('<p style="font-size:14px;color:#15487F;border-left:4px solid #E97C1E;'
                  'padding-left:12px;margin:0 0 20px;">Por <strong>' + autor + '</strong></p>')
        body_html = byline + body_html
    try:
        cat_ids = detect_categories(title, cuerpo, bajada) or []
    except Exception:
        cat_ids = []
    cat_ids = [91] + [c for c in cat_ids if c != 91]   # Opinión (91) primero
    gtags = [t.strip() for t in (ex.get("tags") or []) if t and str(t).strip()]
    tags  = list(dict.fromkeys(gtags + extract_tags(title)))[:6]
    hts = []
    for h in (ex.get("hashtags") or []):
        h = re.sub(r"\s+", "", str(h).strip())
        if h and not h.startswith("#"):
            h = "#" + h
        if len(h) > 1:
            hts.append(h)
    hts = list(dict.fromkeys(hts))[:5]
    if not hts:
        hts = ["#" + re.sub(r"\s+", "", t) for t in tags[:4]]
    return {"title": title, "excerpt": bajada or cuerpo[:160], "content_html": body_html,
            "bullets": bullets, "h2_headings": h2, "category_ids": cat_ids,
            "tag_names": tags, "hashtags": hts, "focus_keyword": kw, "autor": autor,
            "formato": "continua", "portada": False,
            "source_url": f"columna://{url_slug(title) or 'autor'}"}


def _crear_job_nota_manual(d: dict, modo: str, hilo: int = 3) -> int:
    """Crea el job de una nota manual en 'curado' (flag es_nota_manual) para mostrarla con el
    MISMO card del curador del briefing (build_card_keyboard). El contenido YA es final
    (tal_cual formatea / estilo_me reescribe en el bot), así que al aprobar va DIRECTO a
    'publicacion' sin pasar por el redactor (que lo reescribiría). La foto sigue la ruta del
    publicador (override → og → biblioteca → img_gen → logo) con watermark de marca."""
    import sys as _s; _s.path.insert(0, "/opt/me-harness")
    import broker as _br
    cj = {
        "title": d["title"], "excerpt": d.get("excerpt", ""), "content_html": d["content_html"],
        "bullets": d.get("bullets", []), "h2_headings": d.get("h2_headings", []),
        "image_url": None, "image_id_override": d.get("image_id_override"),
        "category_ids": d.get("category_ids", []), "tag_names": d.get("tag_names", []),
        "hashtags": d.get("hashtags", []), "matched_kw": [],
        "formato": d.get("formato") or "continua", "portada": bool(d.get("portada")),
        "meta_desc": (d.get("excerpt") or "")[:210], "focus_keyword": d.get("focus_keyword", ""),
        "source": d["source_url"], "source_url": d["source_url"],
        "fuente_propia": True, "pre_passed": True, "autor": d.get("autor", ""),
        # Una columna es una nota deliberada y autónoma: nunca se consolida en otra.
        "skip_consolidacion": True, "title_locked": True,
        "es_nota_manual": True, "manual_modo": modo, "manual_submission": True,
        "pdf_images": d.get("pdf_images", 0), "hilo": hilo,
    }
    return _br.enqueue("curado", source_url=d["source_url"], title=d["title"],
                       content=cj, score=8.0, hilo=hilo, force=True)


def _publicar_nota_manual_directo(job_id: int, state: dict) -> str:
    """Publica una nota manual DIRECTO a 'publicacion' (contenido final, sin redactor).
    Traduce el destino elegido en el card (pub_dest_override / pub_date_override) a la
    programación del publicador con _next_available_slot. Devuelve el texto de confirmación."""
    import sys as _s; _s.path.insert(0, "/opt/me-harness")
    import broker as _br, sqlite3 as _sq
    from agents import publicador as _pub
    HDB = "/opt/me-harness/harness.db"
    override = state.get("pub_dest_override")
    fecha_ov = state.get("pub_date_override")
    instr = None; cuando = None
    if fecha_ov:
        instr = f"programar:{fecha_ov}"; cuando = str(fecha_ov).replace("T", " ")[:16]
    elif override in ("mejor_horario", "finde"):
        try:
            iso = _pub._next_available_slot(override)
            instr = f"programar:{iso}"; cuando = iso.replace("T", " ")[:16]
        except Exception:
            pass
    if override == "hoy_portada":
        state["portada"] = True
    # Mover a publicación con el contenido ya editado en el card (formato, portada, foto, título)
    _br.update_stage(job_id, "publicacion", content=state)
    if instr:
        with _sq.connect(HDB) as conn:
            conn.execute("UPDATE jobs SET instructions=? WHERE id=?", (instr, job_id))
    _tit = (state.get("title") or "")[:60]
    if instr:
        return f"🗓️ «{_tit}» programada para {cuando}."
    return f"✅ «{_tit}» → publicando ahora (foto automática con watermark si no cargaste una)."


def _html_to_telegram_text(html: str) -> str:
    """Convierte el HTML del cuerpo a texto legible para mostrarlo en Telegram."""
    t = html or ""
    t = re.sub(r'(?i)<h2[^>]*>', '\n\n▸ ', t)
    t = re.sub(r'(?i)</h2>', '\n', t)
    t = re.sub(r'(?i)<li[^>]*>', '\n• ', t)
    t = re.sub(r'(?i)</p>|</div>', '\n\n', t)
    t = re.sub(r'(?i)<br\s*/?>', '\n', t)
    t = re.sub(r'<[^>]+>', '', t)
    t = t.replace('&hellip;', '…').replace('&amp;', '&').replace('&nbsp;', ' ')
    t = re.sub(r'&#?\w+;', ' ', t)
    t = re.sub(r'[ \t]+', ' ', t)
    t = re.sub(r'\n{3,}', '\n\n', t).strip()
    return t


def _chunks(s: str, n: int = 3900) -> list:
    """Parte un texto en pedazos <= n respetando párrafos (límite Telegram 4096)."""
    out, cur = [], ""
    for para in (s or "").split("\n\n"):
        while len(para) > n:
            if cur:
                out.append(cur); cur = ""
            out.append(para[:n]); para = para[n:]
        if len(cur) + len(para) + 2 > n:
            if cur:
                out.append(cur)
            cur = para
        else:
            cur = (cur + "\n\n" + para) if cur else para
    if cur:
        out.append(cur)
    return out or [""]


_NM_ESPERAS = ("awaiting_nota_manual_title", "awaiting_nota_manual_bajada",
               "awaiting_nota_manual_tags", "awaiting_nota_manual_sched",
               "awaiting_nm_enfoque", "awaiting_nm_ajuste")


def _nm_espera(context, flag: str = None):
    """El bot solo puede estar esperando UNA cosa a la vez.

    Sin esto, tocar 🗓️ Programar y después ✏️ Título dejaba el flag de la fecha vivo: el
    título que escribías lo agarraba el parser de fechas. Cada botón limpia los demás.
    """
    for f in _NM_ESPERAS:
        context.user_data.pop(f, None)
    if flag:
        context.user_data[flag] = True


async def cmd_notamanual(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if BOT_PAUSED:
        await update.message.reply_text("⏸ Bot en pausa. Usá /RESUME para reactivar.")
        return
    _nm_espera(context)          # arrancar limpio: nada de flags colgados de una nota anterior
    context.user_data["awaiting_nota_manual"] = True
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("📄 Adjuntar texto", callback_data="nm_texto"),
         InlineKeyboardButton("🎤 Adjuntar audio", callback_data="nm_audio"),
         InlineKeyboardButton("🎥 Video", callback_data="nm_video")],
        [InlineKeyboardButton("✖️ Cancelar", callback_data="nm_cancel")]])
    await update.message.reply_text(
        "📝 <b>Nota manual — columna de autor</b>\n\n"
        "Pegá el <b>texto completo</b> de la columna acá, o usá los botones: "
        "<b>📄 texto</b> (PDF o .txt plano) · <b>🎤 audio</b> (WhatsApp, Telegram o mp4 — lo "
        "transcribo) · <b>🎥 video</b> (mp4 de WhatsApp/Telegram o link de Instagram/YouTube).\n\n"
        "Detecto <b>título, bajada, autor, cuerpo, categoría, etiquetas y hashtags</b> y la "
        "maqueto con el formato y SEO de siempre. No invento ni cambio el contenido.",
        parse_mode="HTML", reply_markup=kb)


_NM_AUDIO_EXT = (".m4a", ".mp3", ".ogg", ".opus", ".wav", ".aac", ".flac", ".amr", ".mpga")
_NM_VIDEO_EXT = (".mp4", ".mov", ".webm", ".mkv", ".avi", ".m4v", ".3gp", ".mpeg")


async def _nm_from_audio(update, context, media):
    """Baja un audio o VIDEO (voz de Telegram, m4a/ogg/mp3, mp4/mov de WhatsApp, etc.), lo transcribe
    con Whisper (_whisper_from_file saca el audio del video con ffmpeg) y lo procesa como el texto de
    la nota manual (misma ruta que el PDF/texto)."""
    import os as _os, tempfile as _tmp
    m = update.message
    status = await m.reply_text("📥 Bajando el archivo…")
    path = None
    try:
        f = await media.get_file(read_timeout=120, connect_timeout=30)
        base = getattr(media, "file_name", None) or f"{media.file_unique_id}.ogg"
        ext = _os.path.splitext(base)[1].lower() or ".ogg"
        path = _os.path.join(_tmp.gettempdir(), f"nm_{media.file_unique_id}{ext}")
        await f.download_to_drive(path)
    except Exception as e:
        await status.edit_text(
            f"❌ No pude bajar el archivo ({e}). Ojo: Telegram limita los archivos del bot a ~20 MB "
            "(si el video pesa más, mandá uno más corto o solo el audio).")
        return
    await status.edit_text("🧠 Transcribiendo… (puede tardar un rato)")
    texto = await asyncio.to_thread(_whisper_from_file, path)
    try:
        if path: _os.remove(path)
    except Exception:
        pass
    if not texto:
        await status.edit_text("⚠️ No pude transcribir. Probá reenviarlo (audio m4a/ogg/mp3 o video mp4/mov).")
        return
    context.user_data.pop("awaiting_nota_manual", None)
    await status.edit_text(f"✅ Audio transcripto ({len(texto.split())} palabras). Procesando la columna…")
    await _process_nota_manual(update, context, texto, status_msg=status)


async def handle_document(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.user_data.get("awaiting_nota_manual"):
        return
    doc = update.message.document
    fn = (doc.file_name or "").lower()
    mime = (doc.mime_type or "").lower()
    # Audio o VIDEO enviado como ARCHIVO (WhatsApp exporta .opus/.m4a/.mp4 como documento) → transcribir
    if mime.startswith(("audio/", "video/")) or fn.endswith(_NM_AUDIO_EXT + _NM_VIDEO_EXT):
        await _nm_from_audio(update, context, doc)
        return
    # Archivo de TEXTO (.txt/.md) → misma ruta que el texto pegado / el PDF
    if fn.endswith((".txt", ".md")) or mime.startswith("text/"):
        context.user_data.pop("awaiting_nota_manual", None)
        msg = await update.message.reply_text("📄 Leyendo el archivo…")
        try:
            f = await doc.get_file()
            raw = bytes(await f.download_as_bytearray())
        except Exception as e:
            await msg.edit_text(f"❌ No pude descargar el archivo: {e}")
            return
        text = None
        for _enc in ("utf-8-sig", "utf-8", "cp1252", "latin-1"):  # Word/Windows exporta cp1252
            try:
                text = raw.decode(_enc)
                break
            except Exception:
                continue
        text = (text or "").strip()
        if len(text) < 400:
            await msg.edit_text("⚠️ El archivo tiene muy poco texto (mínimo ~400 caracteres). "
                                "Revisalo o pegá la columna directamente.")
            return
        if len(text) > 60000:
            await msg.edit_text("⚠️ Archivo muy largo — tomo los primeros 60.000 caracteres…")
            text = text[:60000]
        await msg.edit_text("🧩 Procesando la columna…")
        await _process_nota_manual(update, context, text, status_msg=msg)
        return
    if not (fn.endswith(".pdf") or (doc.mime_type or "") == "application/pdf"):
        await update.message.reply_text(
            "Subí un <b>PDF</b>, un <b>archivo de texto</b> (.txt/.md), un <b>audio/video</b> "
            "(nota de voz, m4a, mp4…), o pegá el texto de la columna.",
            parse_mode="HTML")
        return
    context.user_data.pop("awaiting_nota_manual", None)
    msg = await update.message.reply_text("📄 Leyendo el PDF…")
    import tempfile as _tmp, os as _os
    try:
        f = await doc.get_file()
        path = _os.path.join(_tmp.gettempdir(), f"nm_{doc.file_unique_id}.pdf")
        await f.download_to_drive(path)
        text, pdf_imgs = await asyncio.to_thread(_pdf_to_text, path)
        try: _os.remove(path)
        except Exception: pass
    except Exception as e:
        await msg.edit_text(f"❌ No pude descargar el PDF: {e}")
        return
    if text is None:
        await msg.edit_text("⚠️ No tengo lector de PDF instalado todavía. Pegá el texto de la columna directamente con /notamanual.")
        return
    if not text or len(text) < 400:
        await msg.edit_text("⚠️ No pude extraer texto suficiente del PDF (¿es escaneado/imagen?). Probá pegando el texto.")
        return
    await msg.edit_text("🧩 Procesando la columna…")
    await _process_nota_manual(update, context, text, status_msg=msg, pdf_images=pdf_imgs)


async def _process_nota_manual(update, context, raw_text: str, status_msg=None, pdf_images=0):
    raw_text = (raw_text or "").strip()
    if len(raw_text) < 400:
        await update.message.reply_text("⚠️ El texto es muy corto para una nota. Pegá la columna completa (mín. ~400 caracteres).")
        return
    ex = await asyncio.to_thread(_gpt_nota_manual_extract, raw_text)
    if not ex:
        m = "❌ No pude procesar el texto (GPT). Reintentá en un momento."
        if status_msg: await status_msg.edit_text(m)
        else: await update.message.reply_text(m)
        return
    # Elegir el TRATAMIENTO antes de armar (botones): tal cual (preserva) vs estilo ME (reescribe)
    import html as _hh
    context.user_data["nota_manual_ex"] = {"ex": ex, "pdf_images": pdf_images}
    context.user_data.pop("nota_manual", None)
    if status_msg:
        try: await status_msg.edit_text("📝 Columna recibida.")
        except Exception: pass
    txt = ("📝 <b>Columna recibida</b> — «" + _hh.escape((ex.get("title") or "")[:70]) + "»\n\n"
           "¿Cómo la publico?\n\n"
           "📝 <b>Tal cual</b> (recomendado): tu texto exacto, solo formateado en párrafos + la "
           "firma del autor. Respeta tu voz — no reescribe ni una palabra.\n\n"
           "✍️ <b>Estilo ME</b>: lo reescribo con bullets, secciones y «Resumen para pymes», "
           "como una nota del diario.")
    await update.message.reply_text(txt, parse_mode="HTML", reply_markup=_nota_manual_modo_kb())


def _nota_manual_modo_kb() -> dict:
    return {"inline_keyboard": [
        [{"text": "📝 Transcribir tal cual (recomendado)", "callback_data": "nm_modo_talcual"}],
        [{"text": "✍️ Procesar estilo ME", "callback_data": "nm_modo_estilome"}],
        [{"text": "✖️ Cancelar", "callback_data": "nm_cancel"}]]}


_NM_VIDLINK_RE = re.compile(
    r"^\s*(https?://(?:www\.|m\.)?(?:instagram\.com|youtube\.com|youtu\.be)/\S+)\s*$", re.I)

# Enfoques de redacción por capa del briefing (para «Procesar estilo ME»)
_NM_ENFOQUES = {
    1: ("Informarse es respetarse — SERVICIO PRÁCTICO: qué cambia para el dueño de pyme, "
        "requisitos, montos, fechas y pasos concretos. Tono claro y útil, cero relleno."),
    2: ("Mundo empresarial — NOTICIA del sector empresario: el hecho, los números, los "
        "actores y qué significa para el ecosistema pyme."),
    3: ("La Voz de las pymes — TESTIMONIAL/HUMANO: la historia y la voz del protagonista, "
        "sus decisiones, aprendizajes y citas en primera persona."),
    4: ("Comercial — PRESENTACIÓN atractiva y transparente de la empresa/producto: propuesta "
        "de valor, diferenciales y datos útiles para el lector."),
}
_NM_CAPA_LBL = {1: "1️⃣ Informarse", 2: "2️⃣ Mundo emp.", 3: "3️⃣ La Voz", 4: "4️⃣ Comercial"}


def _nm_enfoque_kb() -> dict:
    return {"inline_keyboard": [
        [{"text": _NM_CAPA_LBL[1], "callback_data": "nm_enf:1"},
         {"text": _NM_CAPA_LBL[2], "callback_data": "nm_enf:2"}],
        [{"text": _NM_CAPA_LBL[3], "callback_data": "nm_enf:3"},
         {"text": _NM_CAPA_LBL[4], "callback_data": "nm_enf:4"}],
        [{"text": "✍️ Escribir el enfoque a mano", "callback_data": "nm_enf_manual"}],
        [{"text": "⬅️ Volver", "callback_data": "nm_enf_back"},
         {"text": "✖️ Cancelar", "callback_data": "nm_cancel"}]]}


def _nm_preview_kb() -> dict:
    return {"inline_keyboard": [
        [{"text": "✅ Aceptar → briefing", "callback_data": "nm_ok"}],
        [{"text": "🔧 Ajustar el enfoque", "callback_data": "nm_adj"}],
        [{"text": "⬅️ Volver", "callback_data": "nm_back_enf"},
         {"text": "✖️ Cancelar", "callback_data": "nm_cancel"}]]}


async def _nm_from_video_link(update, context, url: str):
    """Link de Instagram o YouTube en /notamanual → baja el audio con yt-dlp, transcribe con
    Whisper y sigue la misma ruta que el texto pegado. YouTube va por WARP (ban de IP del VPS);
    Instagram va directo con cookies (IG bloquea IPs de datacenter/proxy)."""
    es_yt = ("youtube.com" in url.lower()) or ("youtu.be" in url.lower())
    status = await update.effective_message.reply_text(
        f"📥 Bajando el audio del video de {'YouTube' if es_yt else 'Instagram'}…")
    if es_yt:
        texto = await asyncio.to_thread(_whisper_from_url, url, YOUTUBE_PROXY, YOUTUBE_COOKIES)
    else:
        texto = await asyncio.to_thread(_whisper_from_url, url, None, INSTAGRAM_COOKIES)
    if not texto:
        await status.edit_text("⚠️ No pude bajar/transcribir ese video. Probá con el archivo "
                               "mp4 directamente, o revisá que el link sea público.")
        context.user_data["awaiting_nota_manual"] = True   # que pueda reintentar
        return
    await status.edit_text(f"✅ Video transcripto ({len(texto.split())} palabras). Procesando la columna…")
    await _process_nota_manual(update, context, texto, status_msg=status)


async def _nm_estilo_procesar(update, context):
    """Redacta la columna en estilo ME con el enfoque vigente y muestra el PREVIEW con los
    botones Aceptar / Ajustar enfoque / Volver / Cancelar. Reentrante: cada ajuste re-procesa."""
    stash = context.user_data.get("nota_manual_ex")
    if not stash:
        await context.bot.send_message(chat_id=update.effective_chat.id,
                                       text="⚠️ Se perdió la columna. Reiniciá con /notamanual.")
        return
    enfoque = context.user_data.get("nm_enfoque") or ""
    st = await context.bot.send_message(chat_id=update.effective_chat.id,
                                        text="🧩 Redactando estilo ME con ese enfoque…")
    d = await asyncio.to_thread(_build_nota_manual_data, stash["ex"], "estilo_me", enfoque)
    d["pdf_images"] = stash.get("pdf_images", 0)
    context.user_data["nm_d"] = d
    try:
        await st.delete()
    except Exception:
        pass
    parts = []
    if d.get("bullets"):
        parts.append("📌 Lo que tenés que saber:\n" + "\n".join("• " + b for b in d["bullets"]))
    parts.append(_html_to_telegram_text(d["content_html"]))
    await context.bot.send_message(
        chat_id=update.effective_chat.id, parse_mode="HTML",
        text=f"📄 <b>{d['title']}</b>\n<i>{(d.get('excerpt') or '')[:200]}</i>\n\n(así quedaría la nota)")
    for chunk in _chunks("\n\n".join(parts), 3900):
        await context.bot.send_message(chat_id=update.effective_chat.id, text=chunk,
                                       disable_web_page_preview=True)
    await context.bot.send_message(
        chat_id=update.effective_chat.id, parse_mode="HTML",
        text=("¿La mando al briefing así?\n<i>Enfoque actual: " +
              (enfoque[:300] or "sin enfoque específico") + "</i>"),
        reply_markup=_nm_preview_kb())


async def handle_notamanual_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    act = q.data[len("nm_"):]
    data = context.user_data.get("nota_manual")
    if act == "cancel":
        _nm_espera(context)
        context.user_data.pop("awaiting_nota_manual", None)
        for _k in ("nota_manual", "nota_manual_ex", "nm_enfoque", "nm_capa", "nm_d"):
            context.user_data.pop(_k, None)
        await q.edit_message_text("✖️ Nota manual cancelada.")
        return
    # ── Flujo de ENFOQUE (estilo ME) ──────────────────────────────────────────
    if act.startswith("enf:"):          # capa 1-4 del briefing → enfoque de esa categoría
        _n = int(act.split(":")[1])
        context.user_data["nm_capa"] = _n
        context.user_data["nm_enfoque"] = _NM_ENFOQUES.get(_n, "")
        await q.edit_message_text(f"🎯 Enfoque: {_NM_CAPA_LBL.get(_n)}")
        await _nm_estilo_procesar(update, context)
        return
    if act == "enf_manual":
        _nm_espera(context, "awaiting_nm_enfoque")
        await q.edit_message_text(
            "✍️ Escribí el <b>enfoque de la nota</b> (una o dos frases: ángulo, a quién le "
            "habla, qué destacar).", parse_mode="HTML")
        return
    if act == "enf_back":               # volver al picker Tal cual / Estilo ME
        _nm_espera(context)
        await q.edit_message_text("📝 ¿Cómo la proceso?", reply_markup=_nota_manual_modo_kb())
        return
    if act == "back_enf":               # volver del preview al menú de enfoque
        _nm_espera(context)
        context.user_data.pop("nm_d", None)
        await q.edit_message_text("🎯 ¿Con qué enfoque la redacto?",
                                  reply_markup=_nm_enfoque_kb())
        return
    if act == "adj":
        _nm_espera(context, "awaiting_nm_ajuste")
        await q.edit_message_text(
            "🔧 Contame el <b>ajuste</b> (ej: «más corto», «enfocalo en el costo laboral», "
            "«sacá la parte de X») y la vuelvo a redactar.", parse_mode="HTML")
        return
    if act == "ok":
        d = context.user_data.get("nm_d")
        if not d:
            await q.edit_message_text("⚠️ Se perdió la nota. Reiniciá con /notamanual.")
            return
        _capa = context.user_data.get("nm_capa") or 3
        await q.edit_message_text("🗂 Dale — la mando al briefing…")
        try:
            jid = await asyncio.to_thread(_crear_job_nota_manual, d, "estilo_me", _capa)
        except Exception as _e_ok:
            await q.message.reply_text(f"❌ No pude crear la nota: {_e_ok}")
            return
        _nm_espera(context)
        for _k in ("nota_manual", "nota_manual_ex", "nm_enfoque", "nm_capa", "nm_d"):
            context.user_data.pop(_k, None)
        import sys as _s_ok; _s_ok.path.insert(0, "/opt/me-harness")
        from agents import curador as _cur_ok
        await asyncio.to_thread(_cur_ok.run_briefing_single, jid)
        try:
            await q.message.delete()
        except Exception:
            pass
        return
    if act == "texto":
        # Adjuntar texto plano: PDF o archivo .txt (misma ruta de handle_document).
        context.user_data["awaiting_nota_manual"] = True
        try:
            await q.edit_message_text(
                "📄 Dale — mandame el archivo ahora: <b>PDF</b> o <b>.txt</b> con la columna "
                "(también podés pegar el texto directamente acá).", parse_mode="HTML")
        except Exception:
            await q.message.reply_text("📄 Mandame el PDF o el .txt ahora.")
        return
    if act == "video":
        # Video: archivo mp4/mov (TG/WA, ~20 MB máx) o LINK de Instagram/YouTube.
        context.user_data["awaiting_nota_manual"] = True
        try:
            await q.edit_message_text(
                "🎥 Dale — mandame el <b>video</b> ahora: archivo <b>mp4/mov</b> de WhatsApp o "
                "Telegram (hasta ~20 MB), o pegá el <b>link de Instagram o YouTube</b> y lo "
                "transcribo.", parse_mode="HTML")
        except Exception:
            await q.message.reply_text("🎥 Mandame el mp4 o el link de Instagram/YouTube.")
        return
    if act == "audio":
        # Adjuntar audio: dejamos el flag de espera; el usuario manda el archivo y lo transcribimos.
        context.user_data["awaiting_nota_manual"] = True
        try:
            await q.edit_message_text(
                "🎤 Dale — mandame el audio ahora: <b>nota de voz de WhatsApp o Telegram</b>, "
                "m4a, mp3, ogg… o un <b>mp4</b> (video corto, hasta ~20 MB) y lo paso a texto "
                "para la columna.", parse_mode="HTML")
        except Exception:
            await q.message.reply_text("🎤 Mandame el audio (o mp4) ahora y lo transcribo.")
        return
    # Estilo ME → primero el ENFOQUE (capa del briefing o escrito a mano), después redacta
    # y muestra preview iterativo (aceptar / ajustar / volver). Ver _nm_estilo_procesar.
    if act == "modo_estilome":
        if not context.user_data.get("nota_manual_ex"):
            await q.edit_message_text("⚠️ Se perdió la columna. Reiniciá con /notamanual.")
            return
        context.user_data.pop("nm_enfoque", None)
        context.user_data.pop("nm_capa", None)
        await q.edit_message_text("🎯 ¿Con qué enfoque la redacto?", reply_markup=_nm_enfoque_kb())
        return
    # Tal cual: arma la nota y la muestra con el MISMO card del curador del briefing
    # (menú unificado). El cuerpo va como preview arriba.
    if act == "modo_talcual":
        stash = context.user_data.get("nota_manual_ex")
        if not stash:
            await q.edit_message_text("⚠️ Se perdió la columna. Reiniciá con /notamanual.")
            return
        modo = "tal_cual"
        etq = "tal cual (tu texto)"
        await q.edit_message_text(f"🧩 Armando la nota — {etq}…")
        d = await asyncio.to_thread(_build_nota_manual_data, stash["ex"], modo)
        d["pdf_images"] = stash.get("pdf_images", 0)
        context.user_data.pop("nota_manual_ex", None)
        context.user_data.pop("nota_manual", None)
        try:
            jid = await asyncio.to_thread(_crear_job_nota_manual, d, modo)
        except Exception as _e_nm:
            await q.message.reply_text(f"❌ No pude crear la nota: {_e_nm}")
            return
        # Preview del cuerpo (así se va a publicar)
        try:
            parts = []
            if d.get("bullets"):
                parts.append("📌 Lo que tenés que saber:\n" + "\n".join("• " + b for b in d["bullets"]))
            parts.append(_html_to_telegram_text(d["content_html"]))
            await q.message.reply_text("📄 <b>Cuerpo de la nota</b> (así se va a publicar):", parse_mode="HTML")
            for chunk in _chunks("\n\n".join(parts), 3900):
                await q.message.reply_text(chunk, disable_web_page_preview=True)
        except Exception:
            pass
        # Card del curador (mismo menú que el briefing) + borrar el mensaje "Armando…"
        import sys as _s_nmj; _s_nmj.path.insert(0, "/opt/me-harness")
        from agents import curador as _cur_nmj
        await asyncio.to_thread(_cur_nmj.run_briefing_single, jid)
        try:
            await q.message.delete()
        except Exception:
            pass
        return
    # Cualquier otro callback nm_* (foto/bajada/cat/pub/prog…) pertenecía al panel viejo,
    # ya reemplazado por el card del curador. No debería llegar nunca; se ignora silencioso.


# ══════════════════════════════════════════════════════════════════════════════
# /evento — cobertura propia de eventos (audios + texto + links + fotos → harness)
# ══════════════════════════════════════════════════════════════════════════════

_WHISPER_OK_EXT = {".mp3", ".m4a", ".mp4", ".mpeg", ".mpga", ".wav", ".webm",
                   ".ogg", ".oga", ".flac"}


def _whisper_from_file(path: str) -> str:
    """Transcribe un archivo de audio/voz local con Whisper API (español).
    Convierte con ffmpeg a mp3 mono si el formato no es soportado o pesa demasiado."""
    if not OPENAI_API_KEY:
        return ""
    import os as _os, shutil as _sh, subprocess as _sp, tempfile as _tmp
    src = path
    tmp_mp3 = None
    try:
        ext = _os.path.splitext(path)[1].lower()
        size_mb = _os.path.getsize(path) / (1024 * 1024)
        if ext not in _WHISPER_OK_EXT or size_mb > 24.5:
            if not _sh.which("ffmpeg"):
                logger.error("_whisper_from_file: ffmpeg no disponible para convertir")
                return ""
            fd, tmp_mp3 = _tmp.mkstemp(suffix=".mp3"); _os.close(fd)
            _sp.run(["ffmpeg", "-y", "-i", path, "-vn", "-ac", "1", "-ar", "16000",
                     "-b:a", "48k", tmp_mp3], check=True, capture_output=True)
            src = tmp_mp3
        if _os.path.getsize(src) / (1024 * 1024) > 24.5:
            logger.error("_whisper_from_file: audio > 25 MB incluso comprimido")
            return ""
        with open(src, "rb") as f:
            r = openai_post(
                "https://api.openai.com/v1/audio/transcriptions",
                headers={"Authorization": f"Bearer {OPENAI_API_KEY}"},
                data={"model": "whisper-1", "language": "es", "response_format": "text"},
                files={"file": (_os.path.basename(src), f, "audio/mpeg")},
                timeout=300,
            )
        if r.status_code == 200:
            return r.text.strip()
        logger.error(f"_whisper_from_file API {r.status_code}: {r.text[:300]}")
    except Exception as e:
        logger.error(f"_whisper_from_file: {e}")
    finally:
        if tmp_mp3:
            try: _os.remove(tmp_mp3)
            except Exception: pass
    return ""


def _evento_kb() -> dict:
    return {"inline_keyboard": [
        [{"text": "✅ Procesar", "callback_data": "ev_process"}],
        [{"text": "📝 Instrucción", "callback_data": "ev_instr"},
         {"text": "✖️ Cancelar", "callback_data": "ev_cancel"}]]}


def _evento_resumen(ev: dict) -> str:
    import html as _html
    extra = f"\n📝 Instrucción: {_html.escape(ev['instr'][:120])}" if ev.get("instr") else ""
    return (
        f"🎪 <b>Evento:</b> {_html.escape(ev['nombre'])}\n"
        f"🎙️ Audios: {len(ev['transcripts'])}  ·  "
        f"📝 Notas: {len(ev['notas'])}  ·  "
        f"🔗 Links: {len(ev['links'])}  ·  "
        f"📷 Fotos: {len(ev['fotos'])}{extra}\n\n"
        "Seguí mandando material. Cuando termines, tocá <b>✅ Procesar</b>.")


async def cmd_campania(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Muestra la campaña de evento en curso + botones para revivir/armar/cancelar."""
    import sys as _sc
    _sc.path.insert(0, "/opt/me-harness"); _sc.path.insert(0, "/opt/me-harness/agents")
    try:
        import eventos as _ev
        p = await asyncio.to_thread(_ev._leer_campania_pend)
    except Exception as e:
        await update.message.reply_text(f"❌ Error leyendo el estado: {e}")
        return
    if not p or not p.get("evento_id"):
        await update.message.reply_text(
            "📭 No hay ninguna campaña de evento en curso.\n"
            "Se arranca desde el evento que te propone el agente (o el chequeo diario).")
        return
    from telegram import InlineKeyboardButton, InlineKeyboardMarkup
    estado = p.get("estado"); cur = p.get("curadas", {}); evid = p["evento_id"]
    txt = (f"🗳 <b>Campaña en curso: {p.get('nombre')}</b> ({p.get('esfuerzo')})\n"
           f"Ángulo: {p.get('angulo')}\n"
           f"Notas curadas: {len(cur.get('opinion', []))} opinión + "
           f"{len(cur.get('noticias', []))} noticias\n"
           f"Estado: <b>{estado}</b>")
    rows = []
    if estado == "esperando_insumos":
        rows.append([InlineKeyboardButton("📝 Aportar insumos y armar",
                                          callback_data=f"h_evt_insumos:{evid}")])
        rows.append([InlineKeyboardButton("⏭ Armar sin nota principal",
                                          callback_data=f"h_evt_finalizar:{evid}")])
    elif estado == "armada":
        txt += "\n\n✅ Ya está armada (newsletter en borrador — revisá y aprobá el envío)."
    else:
        rows.append([InlineKeyboardButton("✅ Armar ahora",
                                          callback_data=f"h_evt_finalizar:{evid}")])
    rows.append([InlineKeyboardButton("❌ Cancelar campaña",
                                      callback_data=f"h_evt_cancel:{evid}")])
    await update.message.reply_text(
        txt, parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(rows) if rows else None)


async def cmd_evento(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if BOT_PAUSED:
        await update.message.reply_text("⏸ Bot en pausa. Usá /RESUME para reactivar.")
        return
    nombre = " ".join(context.args).strip() if context.args else "Evento sin título"
    context.user_data["evento"] = {
        "nombre": nombre, "transcripts": [], "notas": [], "links": [], "fotos": [], "instr": ""}
    context.user_data.pop("awaiting_evento_instr", None)
    await update.message.reply_text(
        f"🎪 <b>Cobertura de evento:</b> {nombre}\n\n"
        "Mandame <b>audios/voz, fotos, texto o links</b> del evento (de a uno o varios).\n"
        "• Los audios los transcribo solo (poné el nombre del orador en el epígrafe del audio).\n"
        "• Las fotos van a la portada.\n"
        "• Los links de <b>YouTube</b> los transcribo (no subas el video, mandá el link).\n"
        "• Los links de otros medios se suman como fuentes.\n\n"
        "Cuando termines, tocá <b>✅ Procesar</b> y elegís una nota síntesis o varias.",
        parse_mode="HTML", reply_markup=_evento_kb())


async def _evento_add_text(update, context, text_in: str):
    ev = context.user_data.get("evento")
    if ev is None:
        return
    urls = re.findall(r'https?://\S+', text_in or "")
    if urls:
        for u in urls:
            kind = detect_url_kind(u)
            if kind in ("youtube", "instagram"):
                # Igual que hoy: NO se sube el video, se transcribe el link.
                status = await update.message.reply_text(f"🔍 Transcribiendo {kind}…")
                try:
                    import sys as _s_ev
                    _s_ev.path.insert(0, "/opt/me-harness")
                    from agents.social import analyze as _soc_analyze
                    data = await asyncio.to_thread(_soc_analyze, u)
                    txt = (data.get("text") or "").strip()
                    if txt:
                        label = data.get("title") or f"{kind.capitalize()} {len(ev['transcripts']) + 1}"
                        ev["transcripts"].append({"orador": label, "texto": txt})
                        if data.get("image_url"):
                            ev.setdefault("image_urls", []).append(data["image_url"])
                        await status.edit_text(f"✅ «{label}» transcripto ({len(txt.split())} palabras).")
                    else:
                        if u not in ev["links"]:
                            ev["links"].append(u)
                        await status.edit_text("⚠️ No saqué transcripción; lo dejo como fuente/link.")
                except Exception as e:
                    if u not in ev["links"]:
                        ev["links"].append(u)
                    await status.edit_text(f"⚠️ No pude transcribir ({e}); lo dejo como link.")
            elif u not in ev["links"]:
                ev["links"].append(u)
    else:
        ev["notas"].append(text_in)
    await update.message.reply_text(
        _evento_resumen(ev), parse_mode="HTML", reply_markup=_evento_kb())


async def handle_evento_media(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Recibe audio/voz durante una cobertura de evento y lo transcribe.
    Si estamos esperando una NOTA MANUAL, transcribe el audio como texto de la columna."""
    m = update.message
    media = m.voice or m.audio or m.video
    if context.user_data.get("awaiting_nota_manual") and media:
        await _nm_from_audio(update, context, media)
        return
    if context.user_data.get("input_evento") is not None and media:
        await _iev_media(update, context, media)
        return
    ev = context.user_data.get("evento")
    if ev is None:
        return
    if not media:
        return
    status = await m.reply_text("🎙️ Bajando audio…")
    import os as _os, tempfile as _tmp
    path = None
    try:
        f = await media.get_file(read_timeout=90, connect_timeout=30)
        base = getattr(media, "file_name", None) or f"{media.file_unique_id}.ogg"
        ext = _os.path.splitext(base)[1].lower() or ".ogg"
        path = _os.path.join(_tmp.gettempdir(), f"ev_{media.file_unique_id}{ext}")
        await f.download_to_drive(path)
    except Exception as e:
        await status.edit_text(
            f"❌ No pude bajar el audio ({e}). Ojo: Telegram limita los archivos del bot a ~20 MB.")
        return
    await status.edit_text("🧠 Transcribiendo… (puede tardar un rato)")
    texto = await asyncio.to_thread(_whisper_from_file, path)
    try:
        if path: _os.remove(path)
    except Exception:
        pass
    if not texto:
        await status.edit_text("⚠️ No pude transcribir el audio. Probá reenviarlo.")
        return
    label = (m.caption or "").strip() or getattr(media, "file_name", None) \
        or f"Audio {len(ev['transcripts']) + 1}"
    ev["transcripts"].append({"orador": label, "texto": texto})
    await status.edit_text(f"✅ «{label}» transcripto ({len(texto.split())} palabras).")
    await m.reply_text(_evento_resumen(ev), parse_mode="HTML", reply_markup=_evento_kb())


# ══════════════════════════════════════════════════════════════════════════════
# Nota a partir de un TWEET: pegás el link → leo el tweet + su imagen → tu directiva
# es el ángulo → el harness redacta → el publicador embebe el tweet. (10/8/2026)
# ══════════════════════════════════════════════════════════════════════════════

def _enqueue_tweet_nota(tw: dict, directiva: str, img_desc: str, media_id) -> int:
    """Encola en 'curado' una nota basada en un tweet. text = tweet + info de la imagen;
    instructions = ángulo; source_url = el tweet (para que el publicador lo embeba)."""
    import sqlite3 as _sq
    from datetime import datetime as _dt
    text = f"Tweet de {tw['author_name']} (@{tw['handle']}), {tw.get('date','')}:\n{tw['text']}"
    if img_desc:
        text += f"\n\n[Contenido de la imagen del tweet]:\n{img_desc}"
    base_instr = (f"Nota a partir de un tweet de {tw['author_name']} (@{tw['handle']}). Usá el tweet y la "
                  "información de su imagen como ÚNICA fuente; NO inventes datos fuera de eso. El tweet va a "
                  "quedar EMBEBIDO en la nota, así que no lo pegues textual entero: contextualizá y desarrollá.")
    if directiva:
        base_instr += " ÁNGULO EDITORIAL (lo que Leo quiere destacar): " + directiva
    cj = {"title": "", "excerpt": "", "source_name": tw["author_name"],
          "text": text, "image_id_override": media_id,
          "manual_submission": True, "fuente_propia": True}
    with _sq.connect("/opt/me-harness/harness.db") as conn:
        cur = conn.execute(
            "INSERT INTO jobs (stage, source_url, title, content_json, score, hilo, instructions, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            ("curado", tw["url"], "", json.dumps(cj, ensure_ascii=False), 8.0, 3,
             base_instr, _dt.utcnow().isoformat()))
        return cur.lastrowid


async def _start_tweet_nota(update, context, url: str):
    """Pegaste un link de tweet: lo leo y te pido la directiva (el ángulo)."""
    msg = await update.message.reply_text("🐦 Leyendo el tweet…")
    tw = await asyncio.to_thread(scrape_tweet, url)
    if not tw:
        await msg.edit_text("❌ No pude leer el tweet (¿protegido, borrado, o X caído?). Probá con otro.")
        return
    context.user_data["tweet_nota"] = tw
    context.user_data["awaiting_tweet_directiva"] = True
    import html as _h
    resumen = (f"🐦 <b>Tweet de {_h.escape(tw['author_name'])}</b> (@{_h.escape(tw['handle'])})\n"
               f"«{_h.escape((tw['text'] or '')[:600])}»")
    if tw.get("media_urls"):
        resumen += f"\n🖼️ +{len(tw['media_urls'])} imagen(es) — las leo para nutrir la nota"
    await msg.edit_text(
        resumen + "\n\n📝 <b>¿Cuál es tu ángulo / qué querés destacar?</b>\n"
        "Escribí la directiva (así el redactor sabe el foco). O mandá <b>-</b> para una nota neutra.",
        parse_mode="HTML", disable_web_page_preview=True)


async def _crear_nota_tweet(update, context, tw: dict, directiva: str):
    """Lee la imagen del tweet, sube la destacada, encola el job y muestra el card del curador."""
    status = await update.message.reply_text("🧠 Leyendo la imagen y armando la nota…")
    img_url = next((u for u in (tw.get("media_urls") or [])
                    if re.search(r'pbs\.twimg\.com/media|\.(jpg|jpeg|png|webp)', u, re.I)), None)
    img_desc = await asyncio.to_thread(_describe_tweet_image, img_url) if img_url else ""
    media_id = await asyncio.to_thread(upload_image, img_url, tw["author_name"], True) if img_url else None
    try:
        jid = await asyncio.to_thread(_enqueue_tweet_nota, tw, directiva, img_desc, media_id)
    except Exception as e:
        await status.edit_text(f"❌ No pude crear la nota: {e}")
        return
    try:
        await status.delete()
    except Exception:
        pass
    import sys as _s_tw
    _s_tw.path.insert(0, "/opt/me-harness")
    from agents import curador as _cur_tw
    await asyncio.to_thread(_cur_tw.run_briefing_single, jid)


def _enqueue_evento(ev: dict, modo: str) -> list:
    """Encola el/los job(s) del evento en stage='curado' (entra al briefing).
    modo='one' → una nota síntesis; modo='multi' → una por audio/orador."""
    import sqlite3 as _sq
    from datetime import datetime as _dt
    nombre = ev["nombre"]
    slug = url_slug(nombre) or "evento"
    fotos = ev.get("fotos") or []
    img_override = fotos[0] if fotos else None
    extra_imgs = fotos[1:] if len(fotos) > 1 else []
    # Si no hay foto propia, usar el thumbnail del video (YouTube/IG) como portada
    img_url_fallback = (ev.get("image_urls") or [None])[0] if not img_override else None
    links = (ev.get("links") or [])[:2]
    notas = ev.get("notas") or []
    transcripts = ev.get("transcripts") or []

    base_instr = (f"Cobertura propia del evento «{nombre}». No inventes datos: usá solo lo que está "
                  "en el material provisto. Atribuí cada cita textual a su orador. Formato estándar ME.")
    if ev.get("instr"):
        base_instr += " " + ev["instr"]

    def _mk_text(ns, ts):
        parts = []
        if ns:
            parts.append("NOTAS DE CAMPO:\n" + "\n\n".join(ns))
        for t in ts:
            parts.append(f"=== {t['orador']} ===\n{t['texto']}")
        return "\n\n".join(parts).strip()

    def _insert(title, text, src_slug):
        cj = {
            "title": title, "excerpt": "", "source_name": nombre,
            "text": text, "multi_source_urls": links,
            "image_id_override": img_override, "image_url": img_url_fallback,
            "images": extra_imgs,
            "matched_kw": [], "fuente_propia_evento": True, "manual_submission": True,
        }
        with _sq.connect("/opt/me-harness/harness.db") as conn:
            cur = conn.execute(
                "INSERT INTO jobs (stage, source_url, title, content_json, score, hilo, instructions, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                ("curado", f"evento://{src_slug}", title,
                 json.dumps(cj, ensure_ascii=False), 9.0, 3, base_instr, _dt.utcnow().isoformat()))
            return cur.lastrowid

    ids = []
    if modo == "multi" and len(transcripts) >= 2:
        for i, t in enumerate(transcripts):
            title = f"{nombre}: {t['orador']}" if t.get("orador") else nombre
            sslug = f"{slug}-{url_slug(t.get('orador') or '') or i}"
            ids.append(_insert(title, _mk_text(notas if i == 0 else [], [t]), sslug))
    else:
        ids.append(_insert(nombre, _mk_text(notas, transcripts), slug))
    return ids


async def handle_evento_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    act = q.data[len("ev_"):]
    ev = context.user_data.get("evento")
    if act == "cancel":
        context.user_data.pop("evento", None)
        context.user_data.pop("awaiting_evento_instr", None)
        await q.edit_message_text("✖️ Cobertura de evento cancelada.")
        return
    if not ev:
        await q.edit_message_text("⚠️ No hay un evento activo. Reiniciá con /evento &lt;nombre&gt;.")
        return
    if act == "instr":
        context.user_data["awaiting_evento_instr"] = True
        await q.edit_message_text(
            "📝 Mandame la instrucción editorial para este evento (tono, enfoque, qué destacar).")
        return
    if act == "process":
        if not ev["transcripts"] and not ev["notas"]:
            await q.edit_message_text(
                "⚠️ No cargaste material todavía (audios o texto). Mandá algo y tocá ✅ Procesar de nuevo.")
            return
        n = len(ev["transcripts"])
        rows = [[{"text": "📄 Una nota síntesis", "callback_data": "ev_one"}]]
        if n >= 2:
            rows.append([{"text": f"🧩 Varias ({n} — una por audio)", "callback_data": "ev_multi"}])
        rows.append([{"text": "✖️ Cancelar", "callback_data": "ev_cancel"}])
        await q.edit_message_text(
            f"¿Cómo armo la cobertura de «{ev['nombre']}»?\n"
            f"Material: {n} audio(s) · {len(ev['notas'])} nota(s) · "
            f"{len(ev['links'])} link(s) · {len(ev['fotos'])} foto(s).",
            reply_markup={"inline_keyboard": rows})
        return
    if act in ("one", "multi"):
        try:
            ids = await asyncio.to_thread(_enqueue_evento, ev, act)
        except Exception as e:
            await q.edit_message_text(f"❌ Error al encolar el evento: {e}")
            return
        context.user_data.pop("evento", None)
        context.user_data.pop("awaiting_evento_instr", None)
        if len(ids) == 1:
            await q.edit_message_text(
                f"✅ Nota del evento encolada (#{ids[0]}). Aparece en el próximo briefing para que la apruebes.")
        else:
            lst = ", #".join(str(i) for i in ids)
            await q.edit_message_text(
                f"✅ {len(ids)} notas del evento encoladas (#{lst}). Aparecen en el briefing.")
        return


# ══════════════════════════════════════════════════════════════════════════════
# /input_evento — tandas de material para la crónica EN VIVO que arma Claude
# (20/8/2026). A diferencia de /evento, acá NO se redacta nada: cada tanda
# (textos + audios transcriptos + fotos con pie) se guarda como JSON en el VPS
# y Claude la integra a la nota en vivo cuando Leo se lo pide por remote control.
# ══════════════════════════════════════════════════════════════════════════════

IEV_INBOX = "/tmp/envivo/inbox"


def _iev_kb() -> dict:
    return {"inline_keyboard": [
        [{"text": "✅ Procesar tanda", "callback_data": "iev_process"}],
        [{"text": "✖️ Cerrar ventana", "callback_data": "iev_cancel"}]]}


def _iev_resumen(s: dict) -> str:
    return (f"📦 <b>Tanda en curso:</b> {len(s['textos'])} texto(s) · "
            f"{len(s['audios'])} audio(s) · {len(s['fotos'])} foto(s)\n\n"
            "Seguí mandando material, o tocá <b>✅ Procesar tanda</b> para dejársela lista a Claude.")


async def cmd_input_evento(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if BOT_PAUSED:
        await update.message.reply_text("⏸ Bot en pausa. Usá /RESUME para reactivar.")
        return
    import os as _os
    _os.makedirs(IEV_INBOX + "/fotos", exist_ok=True)
    context.user_data["input_evento"] = {"textos": [], "audios": [], "fotos": []}
    await update.message.reply_text(
        "🔴 <b>Ventana de material EN VIVO abierta.</b>\n\n"
        "Mandame <b>texto</b>, <b>audios/voz</b> (el epígrafe = quién habla) y "
        "<b>fotos con pie</b>. Video corto también va (lo transcribo).\n"
        "Cuando la tanda esté completa, tocá <b>✅ Procesar tanda</b> y después "
        "pedile a Claude por remote control que la integre a la crónica.\n"
        "La ventana queda abierta para la tanda siguiente.",
        parse_mode="HTML", reply_markup=_iev_kb())


async def _iev_add_text(update, context, text_in: str):
    s = context.user_data.get("input_evento")
    if s is None:
        return
    s["textos"].append(text_in)
    await update.message.reply_text(_iev_resumen(s), parse_mode="HTML", reply_markup=_iev_kb())


async def _iev_media(update, context, media):
    """Audio/voz/video de una tanda EN VIVO → Whisper → transcript con orador."""
    s = context.user_data.get("input_evento")
    m = update.message
    status = await m.reply_text("🎙️ Bajando audio…")
    import os as _os, tempfile as _tmp
    path = None
    try:
        f = await media.get_file(read_timeout=90, connect_timeout=30)
        base = getattr(media, "file_name", None) or f"{media.file_unique_id}.ogg"
        ext = _os.path.splitext(base)[1].lower() or ".ogg"
        path = _os.path.join(_tmp.gettempdir(), f"iev_{media.file_unique_id}{ext}")
        await f.download_to_drive(path)
    except Exception as e:
        await status.edit_text(
            f"❌ No pude bajar el audio ({e}). Ojo: Telegram limita los archivos del bot a ~20 MB.")
        return
    await status.edit_text("🧠 Transcribiendo…")
    texto = await asyncio.to_thread(_whisper_from_file, path)
    try:
        if path:
            _os.remove(path)
    except Exception:
        pass
    if not texto:
        await status.edit_text("⚠️ No pude transcribir el audio. Probá reenviarlo.")
        return
    orador = (m.caption or "").strip()
    s["audios"].append({"orador": orador, "texto": texto})
    quien = f"«{orador}»" if orador else "sin orador (decime quién habla si querés cita atribuida)"
    await status.edit_text(f"✅ Audio transcripto ({len(texto.split())} palabras) — {quien}.")
    await m.reply_text(_iev_resumen(s), parse_mode="HTML", reply_markup=_iev_kb())


async def _iev_foto(update, context):
    """Foto de una tanda EN VIVO → se guarda en el inbox del VPS con su pie."""
    s = context.user_data.get("input_evento")
    m = update.message
    msg = await m.reply_text("📷 Guardando foto…")
    import os as _os
    try:
        photo = m.photo[-1]
        f = await photo.get_file(read_timeout=60, connect_timeout=20)
        _os.makedirs(IEV_INBOX + "/fotos", exist_ok=True)
        ruta = f"{IEV_INBOX}/fotos/{photo.file_unique_id}.jpg"
        await f.download_to_drive(ruta)
    except Exception as e:
        await msg.edit_text(f"❌ No pude guardar la foto: {e}")
        return
    pie = (m.caption or "").strip()
    s["fotos"].append({"ruta": ruta, "pie": pie})
    await msg.edit_text("✅ Foto guardada" + (f" — «{pie[:80]}»" if pie else " (sin pie)") + ".")
    await m.reply_text(_iev_resumen(s), parse_mode="HTML", reply_markup=_iev_kb())


async def handle_iev_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    act = q.data[len("iev_"):]
    s = context.user_data.get("input_evento")
    if act == "cancel":
        context.user_data.pop("input_evento", None)
        await q.edit_message_text("✖️ Ventana EN VIVO cerrada.")
        return
    if s is None:
        await q.edit_message_text("⚠️ No hay ventana abierta. Reabrí con /input_evento.")
        return
    if act == "process":
        if not (s["textos"] or s["audios"] or s["fotos"]):
            await q.edit_message_text(
                "⚠️ La tanda está vacía — mandá algo primero.", reply_markup=_iev_kb())
            return
        import glob as _gl, os as _os, re as _re
        from datetime import datetime as _dt
        _os.makedirs(IEV_INBOX, exist_ok=True)
        previas = [int(m.group(1)) for p in _gl.glob(f"{IEV_INBOX}/tanda_*.json")
                   if (m := _re.search(r"tanda_(\d+)\.json$", p))]
        n = max(previas or [0]) + 1
        tanda = {"n": n, "hora": _dt.now().strftime("%H:%M"), **s}
        with open(f"{IEV_INBOX}/tanda_{n}.json", "w", encoding="utf-8") as fh:
            json.dump(tanda, fh, ensure_ascii=False)
        # la ventana queda abierta con buffers limpios para la próxima tanda
        context.user_data["input_evento"] = {"textos": [], "audios": [], "fotos": []}

        # ── Hook /vivo: si hay una cobertura EN VIVO activa, Claude no interviene —
        # el bloque se redacta y se propone solo (20/8/2026). Sin estado (o harness caído,
        # o cobertura ya cerrada), comportamiento viejo intacto.
        try:
            import sys as _svh
            _svh.path.insert(0, "/opt/me-harness")
            from agents import vivo as _vivoh
            estado_h = await asyncio.to_thread(_vivoh.cargar_estado)
        except Exception:
            estado_h = None
        if estado_h is not None and estado_h.get("cerrada"):
            estado_h = None
        if estado_h is None:
            await q.edit_message_text(
                f"📦 <b>Tanda {n} lista</b> ({len(s['textos'])} texto(s) · {len(s['audios'])} audio(s) · "
                f"{len(s['fotos'])} foto(s)).\n\n"
                f"Decile a Claude por remote control: <b>«integrá la tanda {n}»</b>.\n"
                "La ventana sigue abierta para la próxima tanda.",
                parse_mode="HTML")
            return
        await q.edit_message_text(f"📦 Tanda {n} lista. 🖊 Redactando el bloque…")
        contexto_h = {
            "enfoque": estado_h.get("enfoque") or "",
            "resumen_contexto": estado_h.get("resumen_contexto") or "",
            "titulo_nota": estado_h.get("titulo_nota") or "",
            "bloques_previos": context.user_data.get("vv_bloques_previos") or [],
        }
        try:
            from agents import vivo_prompts as _vph
            bloque_h = await asyncio.to_thread(_vph.redactar_bloque, tanda, contexto_h)
        except Exception as e:
            await q.message.reply_text(
                f"⚠️ No pude redactar el bloque de la tanda {n} ({e}). Queda guardada para integrarla manual.")
            return
        context.user_data["vv_bloque"] = {
            "tanda": tanda, "html": bloque_h.get("html", ""), "tweet": bloque_h.get("tweet", ""),
            "resumen": bloque_h.get("resumen", ""),
            "al_final": False, "con_tweet": bool(bloque_h.get("tweet")),
        }
        await _vvb_mostrar_preview(update, context, q, bloque_h)
        return


# ══════════════════════════════════════════════════════════════════════════════
# /vivo — panel de cobertura EN VIVO: prepara la nota + placas, arranca la
# cobertura, integra los bloques que llegan por /input_evento (redactados solos
# por el harness), y cierra + difunde + newsletter al final (20/8/2026).
# Todo lo pesado (scrape, GPT, placas, WP, redes) vive en agents/vivo(.py|_prompts.py)
# en el harness; acá solo arma el panel y llama con asyncio.to_thread.
# ══════════════════════════════════════════════════════════════════════════════

_VV_CANAL_LABEL = {
    "twitter": "🐦 Twitter", "telegram": "📣 Telegram", "facebook": "📘 Facebook",
    "ig_feed": "📷 IG Feed", "ig_story": "📱 IG Story", "linkedin": "💼 LinkedIn",
}
_VV_CANAL_ORDEN = ["twitter", "telegram", "facebook", "ig_feed", "ig_story", "linkedin"]


def _vv_canales_default() -> dict:
    return {c: True for c in _VV_CANAL_ORDEN}


def _vv_canales_kb(canales: dict) -> dict:
    rows, row = [], []
    for c in _VV_CANAL_ORDEN:
        icon = "✅" if canales.get(c) else "❌"
        row.append({"text": f"{icon} {_VV_CANAL_LABEL[c]}", "callback_data": f"vv_ch:{c}"})
        if len(row) == 2:
            rows.append(row); row = []
    if row:
        rows.append(row)
    rows.append([{"text": "⬅️ Volver", "callback_data": "vv_ch_back"}])
    return {"inline_keyboard": rows}


def _vv_canales_activo(context) -> dict | None:
    """Cuál de los 3 flujos (arranque / cierre / difusión) tiene el panel de canales abierto ahora."""
    if context.user_data.get("vv_cierre") is not None:
        return context.user_data["vv_cierre"]["canales"]
    if context.user_data.get("vv_dif") is not None:
        return context.user_data["vv_dif"]["canales"]
    cfg = context.user_data.get("vivo_cfg")
    if cfg is not None:
        return cfg["canales"]
    return None


async def _vv_edit(q, text: str, parse_mode: str = None, reply_markup=None):
    """edit_message_caption si el mensaje es una foto (preview de placa/bloque), si no edit_message_text."""
    kwargs = {}
    if reply_markup is not None:
        kwargs["reply_markup"] = reply_markup
    if parse_mode:
        kwargs["parse_mode"] = parse_mode
    if q.message.photo:
        await q.edit_message_caption(caption=text, **kwargs)
    else:
        await q.edit_message_text(text, **kwargs)


# ── Panel principal de preparación (/vivo <nombre>) ─────────────────────────────
_VV_ESPERAS = ("awaiting_vv_titulo", "awaiting_vv_placa", "awaiting_vv_foto",
               "awaiting_vv_enfoque", "awaiting_vv_links")


def _vv_espera(context, flag: str = None):
    for f in _VV_ESPERAS:
        context.user_data.pop(f, None)
    if flag:
        context.user_data[flag] = True


def _vv_panel_text(cfg: dict) -> str:
    import html as _h
    def e(s): return _h.escape(str(s)) if s else "—"
    canales_on = [_VV_CANAL_LABEL[c] for c in _VV_CANAL_ORDEN if cfg["canales"].get(c)]
    return (
        f"🔴 <b>Preparación EN VIVO:</b> {e(cfg['nombre'])}\n\n"
        f"📰 <b>Título nota:</b> {e(cfg.get('titulo_nota'))}\n"
        f"🪧 <b>Placa:</b> {e(cfg.get('titulo_placa'))} / {e(cfg.get('bajada'))} / {e(cfg.get('linea_fecha'))}\n"
        f"🖼 <b>Foto de placa:</b> {'cargada ✅' if cfg.get('foto') else '—'}\n"
        f"🎯 <b>Enfoque:</b> {e(cfg.get('enfoque'))}\n"
        f"🔗 <b>Links de contexto:</b> {len(cfg.get('links') or [])}\n"
        f"📡 <b>Canales:</b> {', '.join(canales_on) or '—'}\n\n"
        "Completá lo que quieras y tocá <b>👁 Preview</b> antes de arrancar.")


def _vv_kb(cfg: dict) -> dict:
    return {"inline_keyboard": [
        [{"text": "📰 Título nota", "callback_data": "vv_titulo"},
         {"text": "🪧 Texto de placa", "callback_data": "vv_placa"}],
        [{"text": "🖼 Foto de placa", "callback_data": "vv_foto"},
         {"text": "🎯 Enfoque", "callback_data": "vv_enfoque"}],
        [{"text": "🔗 Links de contexto", "callback_data": "vv_links"},
         {"text": "📡 Canales", "callback_data": "vv_canales"}],
        [{"text": "👁 Preview", "callback_data": "vv_preview"}],
        [{"text": "✅ Arrancar", "callback_data": "vv_arrancar"},
         {"text": "✖️ Cancelar", "callback_data": "vv_cancel"}]]}


async def cmd_vivo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if BOT_PAUSED:
        await update.message.reply_text("⏸ Bot en pausa. Usá /RESUME para reactivar.")
        return
    args = context.args or []
    sub = args[0].lower() if args else ""
    if sub == "estado":
        await _vv_cmd_estado(update, context)
        return
    if sub == "cerrar":
        await _vv_cmd_cerrar(update, context)
        return
    if sub == "difundir":
        await _vv_cmd_difundir(update, context)
        return
    if sub == "mail":
        await _vv_cmd_mail(update, context)
        return
    nombre = " ".join(args).strip() or "Cobertura en vivo"
    cfg = {
        "nombre": nombre, "titulo_nota": None, "titulo_placa": None, "bajada": None,
        "linea_fecha": None, "foto": None, "enfoque": "", "links": [],
        "resumen_contexto": "", "canales": _vv_canales_default(),
    }
    context.user_data["vivo_cfg"] = cfg
    # paneles viejos afuera: si quedó un cierre/difusión colgado, se llevaría los toggles de canales
    context.user_data.pop("vv_cierre", None)
    context.user_data.pop("vv_dif", None)
    _vv_espera(context)
    await update.message.reply_text(
        _vv_panel_text(cfg), parse_mode="HTML", reply_markup=_vv_kb(cfg))


async def _vv_cmd_estado(update, context):
    import sys as _sve
    _sve.path.insert(0, "/opt/me-harness")
    from agents import vivo as _vivoe
    estado = await asyncio.to_thread(_vivoe.cargar_estado)
    if not estado:
        await update.message.reply_text(
            "No hay ninguna cobertura EN VIVO activa. Abrí una con /vivo <nombre>.")
        return
    n_fotos = len(estado.get("fotos_publicadas") or [])
    cerrada = estado.get("cerrada")
    estado_txt = f"🟢 cerrada ({cerrada})" if cerrada else "🔴 EN VIVO"
    await update.message.reply_text(
        f"{estado_txt} <b>{estado.get('nombre', '?')}</b>\n\n"
        f"📰 {estado.get('titulo_nota', '?')}\n"
        f"🔗 {estado.get('nota_url', '—')}\n"
        f"🎯 {estado.get('enfoque') or '—'}\n"
        f"🖼 {n_fotos} foto(s) publicada(s)\n"
        f"𝕏 último tweet: {estado.get('tweet_last') or estado.get('tweet_root') or '—'}",
        parse_mode="HTML", disable_web_page_preview=True)


async def _vv_capturar_texto(update, context, campo: str, text_in: str):
    """Captura el texto tipeado por Leo para un campo del panel /vivo y redibuja el resumen."""
    cfg = context.user_data.get("vivo_cfg")
    if cfg is None:
        await update.message.reply_text("⚠️ Se perdió el panel. Abrí de nuevo con /vivo <nombre>.")
        return
    if campo == "titulo":
        cfg["titulo_nota"] = text_in.strip()
    elif campo == "placa":
        lineas = [l.strip() for l in text_in.split("\n") if l.strip()]
        if lineas:
            cfg["titulo_placa"] = lineas[0]
        if len(lineas) >= 2:
            cfg["bajada"] = lineas[1]
        if len(lineas) >= 3:
            cfg["linea_fecha"] = lineas[2]
        if len(lineas) < 3:
            await update.message.reply_text(
                f"⚠️ Recibí {len(lineas)} línea(s) de 3 — cargué lo que mandaste, "
                "tocá 🪧 de nuevo para completar el resto.")
    elif campo == "enfoque":
        cfg["enfoque"] = text_in.strip()
    elif campo == "links":
        urls = re.findall(r'https?://\S+', text_in)
        if not urls:
            await update.message.reply_text("⚠️ No encontré ningún link ahí. Probá de nuevo.")
            return
        nuevos = [u for u in urls if u not in cfg["links"]]
        cfg["links"].extend(nuevos)
        await update.message.reply_text(f"🔗 Sumé {len(nuevos)} link(s) — total {len(cfg['links'])}.")
    await update.message.reply_text(_vv_panel_text(cfg), parse_mode="HTML", reply_markup=_vv_kb(cfg))


async def _vv_capturar_foto(update, context):
    cfg = context.user_data.get("vivo_cfg")
    if cfg is None:
        await update.message.reply_text("⚠️ Se perdió el panel. Abrí de nuevo con /vivo <nombre>.")
        return
    msg = await update.message.reply_text("📷 Guardando foto…")
    try:
        photo = update.message.photo[-1]
        file = await photo.get_file(read_timeout=30, connect_timeout=15)
        dl = await asyncio.to_thread(lambda: requests.get(file.file_path, timeout=30))
        cfg["foto"] = dl.content
    except Exception as e:
        await msg.edit_text(f"❌ No pude guardar la foto: {e}")
        return
    await msg.edit_text("✅ Foto cargada.")
    await update.message.reply_text(_vv_panel_text(cfg), parse_mode="HTML", reply_markup=_vv_kb(cfg))


async def _vv_preview(update, context, q):
    cfg = context.user_data.get("vivo_cfg")
    target = q.message if q else update.message
    if cfg is None:
        await target.reply_text("⚠️ No hay panel activo. Abrí con /vivo <nombre>.")
        return
    status = await target.reply_text("👁 Generando preview…")
    import sys as _svp
    _svp.path.insert(0, "/opt/me-harness")
    from agents import vivo as _vivop, vivo_prompts as _vpp

    def _work():
        resumen = ""
        if cfg.get("links"):
            try:
                paginas = _vivop.scrape_contexto(cfg["links"])
            except Exception:
                paginas = []
            if paginas:
                resumen = _vpp.resumir_links(paginas)
        placas = _vivop.generar_placas(
            cfg.get("titulo_placa") or cfg.get("titulo_nota") or cfg["nombre"],
            cfg.get("bajada") or "", cfg.get("linea_fecha") or "", cfg.get("foto"))
        return resumen, placas

    try:
        resumen, placas = await asyncio.to_thread(_work)
    except Exception as e:
        await status.edit_text(f"❌ Error generando el preview: {e}")
        return
    if resumen:
        cfg["resumen_contexto"] = resumen
    try:
        await status.delete()
    except Exception:
        pass
    canales_on = [_VV_CANAL_LABEL[c] for c in _VV_CANAL_ORDEN if cfg["canales"].get(c)]
    bio = io.BytesIO(placas["feed"])
    bio.name = "placa_preview.jpg"
    caption = (
        f"👁 <b>Preview — {cfg['nombre']}</b>\n\n"
        f"📰 {cfg.get('titulo_nota') or '(sin título de nota)'}\n"
        f"🎯 {cfg.get('enfoque') or '—'}\n"
        f"🔗 {len(cfg.get('links') or [])} link(s) de contexto\n"
        f"📡 {', '.join(canales_on) or '—'}\n\n"
        "Si está todo OK, tocá ✅ Arrancar. Si no, ✏️ seguí editando.")[:1000]
    await target.reply_photo(
        photo=bio, caption=caption, parse_mode="HTML",
        reply_markup={"inline_keyboard": [
            [{"text": "✅ Arrancar", "callback_data": "vv_arrancar"},
             {"text": "✏️ Seguir editando", "callback_data": "vv_back"}]]})


async def _vv_arrancar(update, context, q):
    cfg = context.user_data.get("vivo_cfg")
    target = q.message if q else update.message
    if cfg is None:
        await target.reply_text("⚠️ Se perdió el panel. Abrí de nuevo con /vivo <nombre>.")
        return
    await _vv_edit(q, "🔴 Arrancando la cobertura EN VIVO…")
    import sys as _sva
    _sva.path.insert(0, "/opt/me-harness")
    from agents import vivo as _vivoa, vivo_prompts as _vpa
    resumen = cfg.get("resumen_contexto") or ""
    if not resumen and cfg.get("links"):
        # Leo arrancó sin pasar por el preview → el resumen de contexto se arma acá
        def _ctx():
            try:
                paginas = _vivoa.scrape_contexto(cfg["links"])
                return _vpa.resumir_links(paginas) if paginas else ""
            except Exception:
                return ""
        resumen = await asyncio.to_thread(_ctx)
        cfg["resumen_contexto"] = resumen
    intro_html = f'<p style="color:#243;line-height:1.6;">{resumen}</p>' if resumen else ""
    arr = {
        "nombre": cfg["nombre"],
        "titulo_nota": cfg.get("titulo_nota") or cfg["nombre"],
        "titulo_placa": cfg.get("titulo_placa") or cfg.get("titulo_nota") or cfg["nombre"],
        "bajada": cfg.get("bajada") or "",
        "linea_fecha": cfg.get("linea_fecha") or "",
        "foto_bytes": cfg.get("foto"),
        "contexto_intro_html": intro_html,
        "enfoque": cfg.get("enfoque") or "",
        "resumen_contexto": resumen,
        "canales": cfg["canales"],
        "cta": None,
    }
    try:
        estado = await asyncio.to_thread(_vivoa.arrancar, arr)
    except Exception as e:
        estado = {"ok": False, "error": str(e)}
    if not (isinstance(estado, dict) and estado.get("nota_id")):
        # arrancar NO levanta: devuelve {"ok": False, "error"} — el panel se conserva para reintentar
        err = (estado or {}).get("error") if isinstance(estado, dict) else str(estado)
        await target.reply_text(
            f"❌ No arrancó la cobertura: {err}\nEl panel sigue activo — corregí y reintentá.",
            reply_markup=_vv_kb(cfg))
        return
    context.user_data.pop("vivo_cfg", None)
    context.user_data.pop("vv_bloques_previos", None)
    _vv_espera(context)
    await target.reply_text(
        f"🔴 <b>Cobertura EN VIVO arrancada.</b>\n\n"
        f"📰 {estado.get('titulo_nota', '?')}\n"
        f"🔗 {estado.get('nota_url', '')}\n\n"
        "Ahora abrí /input_evento para mandar tandas de material — se redactan e integran solas.",
        parse_mode="HTML", disable_web_page_preview=True)


# ── Bloques redactados solos durante /input_evento (cobertura activa) ──────────
def _vvb_kb(b: dict) -> dict:
    pos_label = "⬆️ Arriba" if not b["al_final"] else "⬇️ Al final"
    tw_label = "𝕏 hilo ✅" if b["con_tweet"] else "𝕏 hilo ❌"
    return {"inline_keyboard": [
        [{"text": "✅ Publicar", "callback_data": "vvb_pub"},
         {"text": "✏️ Ajustar", "callback_data": "vvb_adj"}],
        [{"text": pos_label, "callback_data": "vvb_pos"},
         {"text": tw_label, "callback_data": "vvb_tw"}],
        [{"text": "🗑 Descartar", "callback_data": "vvb_del"}]]}


async def _vvb_mostrar_preview(update, context, q, bloque: dict):
    """Manda el preview de un bloque recién redactado (o reescrito) con los botones de acción."""
    b = context.user_data.get("vv_bloque")
    if b is None:
        return
    import html as _h
    texto_plano = re.sub(r"<[^>]+>", " ", b["html"] or "").strip()
    texto_plano = _h.escape(re.sub(r"\s+", " ", texto_plano))
    partes = [f"🖊 <b>Bloque tanda {b['tanda']['n']} ({b['tanda']['hora']})</b>\n\n{texto_plano[:900]}"]
    if b.get("tweet"):
        partes.append(f"\n\n𝕏 <i>Tweet:</i> {_h.escape(b['tweet'])}")
    if bloque.get("descartes"):
        partes.append("\n\n🗑 <i>Descartado:</i> " + _h.escape("; ".join(bloque["descartes"])[:300]))
    if bloque.get("fallback"):
        partes.append("\n\n⚠️ <i>Redacción de emergencia (OpenAI no respondió) — revisá antes de publicar.</i>")
    txt = "".join(partes)[:4000]
    target = q.message if q else update.message
    await target.reply_text(txt, parse_mode="HTML", reply_markup=_vvb_kb(b))


def _vvb_archivar_tanda(n):
    import os as _osx
    _osx.makedirs(f"{IEV_INBOX}/procesadas", exist_ok=True)
    try:
        _osx.replace(f"{IEV_INBOX}/tanda_{n}.json", f"{IEV_INBOX}/procesadas/tanda_{n}.json")
    except Exception:
        pass


async def _vvb_ajustar(update, context, text_in: str):
    b = context.user_data.get("vv_bloque")
    if b is None:
        await update.message.reply_text("⚠️ Se perdió el bloque. Volvé a integrar la tanda desde /input_evento.")
        return
    import sys as _sva2
    _sva2.path.insert(0, "/opt/me-harness")
    from agents import vivo as _vivoa2, vivo_prompts as _vpa2
    estado = await asyncio.to_thread(_vivoa2.cargar_estado) or {}
    contexto = {
        "enfoque": (estado.get("enfoque") or "") + f"\nCorrección puntual de Leo para este bloque: {text_in.strip()}",
        "resumen_contexto": estado.get("resumen_contexto") or "",
        "titulo_nota": estado.get("titulo_nota") or "",
        "bloques_previos": context.user_data.get("vv_bloques_previos") or [],
    }
    msg = await update.message.reply_text("🖊 Reescribiendo el bloque…")
    try:
        bloque = await asyncio.to_thread(_vpa2.redactar_bloque, b["tanda"], contexto)
    except Exception as e:
        await msg.edit_text(f"❌ No pude reescribir: {e}")
        return
    b["html"] = bloque.get("html", "")
    b["tweet"] = bloque.get("tweet", "")
    b["resumen"] = bloque.get("resumen") or b.get("resumen", "")
    if not b["tweet"]:
        b["con_tweet"] = False
    try:
        await msg.delete()
    except Exception:
        pass
    await _vvb_mostrar_preview(update, context, None, bloque)


async def _vvb_publicar(update, context, q, b: dict):
    await _vv_edit(q, "📤 Publicando bloque…")
    import sys as _svp2
    _svp2.path.insert(0, "/opt/me-harness")
    from agents import vivo as _vivop2
    tweet = b["tweet"] if b.get("con_tweet") else None
    try:
        res = await asyncio.to_thread(
            _vivop2.integrar_bloque, b["html"], b["tanda"].get("fotos") or [], tweet, b["al_final"])
    except Exception as e:
        res = {"ok": False, "error": str(e)}
    if not (isinstance(res, dict) and res.get("ok")):
        err = (res or {}).get("error") if isinstance(res, dict) else str(res)
        await q.message.reply_text(
            f"❌ No pude integrar el bloque: {err}\nEl bloque sigue acá — reintentá o descartalo.",
            reply_markup=_vvb_kb(b))
        return
    _vvb_archivar_tanda(b["tanda"]["n"])
    # memoria para el redactor: el RESUMEN del bloque (no el HTML crudo, que truncado es puro CSS)
    memoria = b.get("resumen") or re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", b["html"] or "")).strip()[:300]
    context.user_data.setdefault("vv_bloques_previos", []).append(memoria)
    context.user_data.pop("vv_bloque", None)
    await q.message.reply_text(
        f"✅ Bloque de la tanda {b['tanda']['n']} publicado" + (" con hilo en 𝕏." if tweet else "."),
        parse_mode="HTML")


async def _handle_vvb_button(update, context, q, data: str):
    act = data[len("vvb_"):]
    if act == "adj":
        context.user_data["awaiting_vvb_adj"] = True
        await q.message.reply_text("✏️ Escribí el ajuste puntual para este bloque (qué cambiar).")
        return
    b = context.user_data.get("vv_bloque")
    if b is None:
        await _vv_edit(q, "⚠️ Se perdió el bloque. Volvé a integrar la tanda desde /input_evento.")
        return
    if act == "pos":
        b["al_final"] = not b["al_final"]
        await q.edit_message_reply_markup(reply_markup=_vvb_kb(b))
        return
    if act == "tw":
        b["con_tweet"] = not b["con_tweet"]
        await q.edit_message_reply_markup(reply_markup=_vvb_kb(b))
        return
    if act == "del":
        _vvb_archivar_tanda(b["tanda"]["n"])
        context.user_data.pop("vv_bloque", None)
        await _vv_edit(q, "🗑 Bloque descartado.")
        return
    if act == "pub":
        await _vvb_publicar(update, context, q, b)
        return


# ── /vivo cerrar — bloque de cierre + portada + difusión ────────────────────────
async def _vv_cmd_cerrar(update, context):
    import sys as _svc
    _svc.path.insert(0, "/opt/me-harness")
    from agents import vivo as _vivoc, vivo_prompts as _vpc
    estado = await asyncio.to_thread(_vivoc.cargar_estado)
    if not estado:
        await update.message.reply_text("No hay ninguna cobertura EN VIVO activa para cerrar.")
        return
    if estado.get("cerrada"):
        await update.message.reply_text(f"Esta cobertura ya está cerrada ({estado['cerrada']}).")
        return
    msg = await update.message.reply_text("🖊 Redactando el cierre…")
    contexto = {
        "enfoque": estado.get("enfoque") or "",
        "resumen_contexto": estado.get("resumen_contexto") or "",
        "titulo_nota": estado.get("titulo_nota") or "",
        "bloques_previos": context.user_data.get("vv_bloques_previos") or [],
    }
    try:
        prop = await asyncio.to_thread(_vpc.proponer_cierre, contexto)
    except Exception as e:
        await msg.edit_text(f"❌ No pude proponer el cierre: {e}")
        return
    fotos = (estado.get("fotos_publicadas") or [])[-6:]
    cierre = {
        "titulo_pasado": prop.get("titulo_pasado") or estado.get("titulo_nota") or "",
        "bloque_html": prop.get("bloque_html", ""), "tweet_cierre": prop.get("tweet_cierre", ""),
        "copys": prop.get("copys") or {},
        "portada_media_id": (fotos[-1]["id"] if fotos else None),
        "canales": _vv_canales_default(), "_fotos": fotos,
        "_fallback": bool(prop.get("fallback")),
    }
    context.user_data["vv_cierre"] = cierre
    try:
        await msg.delete()
    except Exception:
        pass
    await update.message.reply_text(
        _vv_cierre_texto(cierre), parse_mode="HTML", reply_markup=_vv_cierre_kb(cierre))


def _vv_cierre_texto(c: dict) -> str:
    import html as _h
    texto_plano = _h.escape(re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", c["bloque_html"] or "")).strip())
    canales_on = [_VV_CANAL_LABEL[k] for k in _VV_CANAL_ORDEN if c["canales"].get(k)]
    port = c.get("portada_media_id")
    aviso = ("\n⚠️ <i>Redacción de emergencia (OpenAI no respondió) — revisá antes de cerrar.</i>\n"
             if c.get("_fallback") else "")
    return (
        "🏁 <b>Cierre de cobertura</b>\n\n"
        f"📰 Título (pasado): {_h.escape(c['titulo_pasado'])}\n\n"
        f"{texto_plano[:700]}\n\n"
        f"𝕏 Tweet cierre: {_h.escape(c['tweet_cierre'])}\n{aviso}\n"
        f"🖼 Portada: {'#' + str(port) if port else 'elegí una abajo'}\n"
        f"📡 Canales: {', '.join(canales_on) or '—'}\n\n"
        "Elegí portada y canales, y tocá ✅ Cerrar y difundir.")


def _vv_cierre_kb(c: dict) -> dict:
    rows, row = [], []
    for f in c.get("_fotos") or []:
        icon = "🔘" if f["id"] == c.get("portada_media_id") else "⚪"
        row.append({"text": f"{icon} #{f['id']}", "callback_data": f"vv_port:{f['id']}"})
        if len(row) == 3:
            rows.append(row); row = []
    if row:
        rows.append(row)
    rows.append([{"text": "📡 Canales", "callback_data": "vv_canales"}])
    rows.append([{"text": "✅ Cerrar y difundir", "callback_data": "vv_cierre_go"},
                 {"text": "✖️ Cancelar", "callback_data": "vv_cierre_cancel"}])
    return {"inline_keyboard": rows}


async def _vv_cierre_go(update, context, q):
    c = context.user_data.get("vv_cierre")
    if c is None:
        await q.edit_message_text("⚠️ Se perdió el panel de cierre.")
        return
    await q.edit_message_text("🏁 Cerrando la cobertura y difundiendo…")
    import sys as _svcg
    _svcg.path.insert(0, "/opt/me-harness")
    from agents import vivo as _vivocg
    cierre_cfg = {
        "bloque_html": c["bloque_html"], "titulo_pasado": c["titulo_pasado"],
        "portada_media_id": c.get("portada_media_id"), "tweet_cierre": c["tweet_cierre"],
        "copys": c["copys"], "canales": c["canales"],
    }
    try:
        res = await asyncio.to_thread(_vivocg.cerrar, cierre_cfg)
    except Exception as e:
        res = {"ok": False, "error": str(e)}
    if not (isinstance(res, dict) and res.get("ok")):
        # cerrar NO levanta: devuelve {"ok": False, "error"} — el panel se conserva para reintentar
        err = (res or {}).get("error") if isinstance(res, dict) else str(res)
        await q.message.reply_text(
            f"❌ No pude cerrar la cobertura: {err}\nEl panel sigue activo — reintentá.",
            reply_markup=_vv_cierre_kb(c))
        return
    context.user_data.pop("vv_cierre", None)
    context.user_data.pop("vv_bloques_previos", None)
    await q.message.reply_text(
        f"✅ <b>Cobertura cerrada y difundida.</b>\n🔗 {res.get('nota_url', '')}",
        parse_mode="HTML", disable_web_page_preview=True)


# ── /vivo mail — newsletter del cierre ───────────────────────────────────────────
async def _vv_cmd_mail(update, context):
    import sys as _svm
    _svm.path.insert(0, "/opt/me-harness")
    from agents import vivo as _vivom, vivo_prompts as _vpm
    estado = await asyncio.to_thread(_vivom.cargar_estado)
    if not estado:
        await update.message.reply_text("No hay ninguna cobertura EN VIVO activa.")
        return
    msg = await update.message.reply_text("✉️ Armando la propuesta de newsletter…")
    contexto = {
        "enfoque": estado.get("enfoque") or "",
        "resumen_contexto": estado.get("resumen_contexto") or "",
        "titulo_nota": estado.get("titulo_nota") or "",
        "bloques_previos": context.user_data.get("vv_bloques_previos") or [],
    }
    try:
        prop = await asyncio.to_thread(_vpm.proponer_mail, contexto)
    except Exception as e:
        await msg.edit_text(f"❌ No pude armar la propuesta: {e}")
        return
    mail = {
        "estado": estado, "subject": prop.get("subject", ""), "pre_header": prop.get("pre_header", ""),
        "hero_title": prop.get("hero_title", ""), "hero_quote": prop.get("hero_quote", ""),
        "contexto_parrafos": prop.get("contexto_parrafos") or [],
        "segmento": None, "segmento_label": "Toda la base", "campaign_id": None,
        "fallback": bool(prop.get("fallback")),
    }
    context.user_data["vv_mail"] = mail
    try:
        await msg.delete()
    except Exception:
        pass
    await update.message.reply_text(_vv_mail_texto(mail), parse_mode="HTML", reply_markup=_vv_mail_kb())


def _vv_mail_texto(m: dict) -> str:
    import html as _h
    parrafos = "\n\n".join(m.get("contexto_parrafos") or [])  # trae <b> válido para Telegram
    aviso = ("\n⚠️ <i>Propuesta de emergencia (OpenAI no respondió) — revisá antes de enviar.</i>\n"
             if m.get("fallback") else "")
    return (
        f"✉️ <b>Newsletter — {_h.escape(m['estado'].get('nombre', '?'))}</b>\n\n"
        f"<b>Asunto:</b> {_h.escape(m['subject'])}\n"
        f"<b>Pre-header:</b> {_h.escape(m['pre_header'])}\n\n"
        f"<b>{_h.escape(m['hero_title'])}</b>\n<i>{_h.escape(m['hero_quote'])}</i>\n\n"
        f"{parrafos[:600]}\n{aviso}\n"
        f"👥 Destinatarios: {_h.escape(m['segmento_label'])}\n\n"
        "Elegí destinatarios y confirmá el envío (+3 min).")


def _vv_mail_kb() -> dict:
    return {"inline_keyboard": [
        [{"text": "👥 Toda la base", "callback_data": "vv_mail_all"},
         {"text": "🏷 Por tag", "callback_data": "vv_mail_tag"}],
        [{"text": "✏️ Ajustar", "callback_data": "vv_mail_adj"}],
        [{"text": "✅ Enviar ahora (+3 min)", "callback_data": "vv_mail_send"},
         {"text": "✖️ Cancelar", "callback_data": "vv_mail_cancel"}]]}


def _vv_mail_tags_kb(tags: list, pg: int = 0) -> dict:
    per = 8
    start = pg * per
    chunk = tags[start:start + per]
    rows = [[{"text": (t.get("title") or f"tag {t.get('id')}")[:40], "callback_data": f"vv_mail_tag:{start + i}"}]
            for i, t in enumerate(chunk)]
    nav = []
    if pg > 0:
        nav.append({"text": "⬅️", "callback_data": f"vv_mail_pg:{pg - 1}"})
    if start + per < len(tags):
        nav.append({"text": "➡️", "callback_data": f"vv_mail_pg:{pg + 1}"})
    if nav:
        rows.append(nav)
    rows.append([{"text": "⬅️ Volver", "callback_data": "vv_mail_back"}])
    return {"inline_keyboard": rows}


async def _vv_mail_tags_mostrar(update, context, q):
    m = context.user_data.get("vv_mail")
    if m is None:
        await q.edit_message_text("⚠️ Se perdió la propuesta.")
        return
    await q.edit_message_text("🏷 Cargando tags…")
    import sys as _svtg
    _svtg.path.insert(0, "/opt/me-harness")
    from agents import vivo as _vivotg
    try:
        tags = await asyncio.to_thread(_vivotg.listar_tags_fluentcrm)
    except Exception as e:
        await q.edit_message_text(f"❌ No pude leer los tags: {e}")
        return
    context.user_data["vv_mail_tags"] = tags
    if not tags:
        await q.edit_message_text("No hay tags en FluentCRM.", reply_markup=_vv_mail_kb())
        return
    await q.edit_message_text("🏷 Elegí un tag:", reply_markup=_vv_mail_tags_kb(tags, 0))


async def _vv_mail_ajustar(update, context, text_in: str):
    m = context.user_data.get("vv_mail")
    if m is None:
        await update.message.reply_text("⚠️ Se perdió la propuesta. Reabrí con «/vivo mail».")
        return
    import sys as _svma
    _svma.path.insert(0, "/opt/me-harness")
    from agents import vivo_prompts as _vpma
    contexto = {
        "enfoque": (m["estado"].get("enfoque") or "") + f"\nAjuste de Leo: {text_in.strip()}",
        "resumen_contexto": m["estado"].get("resumen_contexto") or "",
        "titulo_nota": m["estado"].get("titulo_nota") or "",
        "bloques_previos": context.user_data.get("vv_bloques_previos") or [],
    }
    msg = await update.message.reply_text("✉️ Reescribiendo…")
    try:
        prop = await asyncio.to_thread(_vpma.proponer_mail, contexto)
    except Exception as e:
        await msg.edit_text(f"❌ No pude reescribir: {e}")
        return
    m.update(subject=prop.get("subject", ""), pre_header=prop.get("pre_header", ""),
              hero_title=prop.get("hero_title", ""), hero_quote=prop.get("hero_quote", ""),
              contexto_parrafos=prop.get("contexto_parrafos") or [])
    try:
        await msg.delete()
    except Exception:
        pass
    await update.message.reply_text(_vv_mail_texto(m), parse_mode="HTML", reply_markup=_vv_mail_kb())


async def _vv_mail_send(update, context, q):
    m = context.user_data.get("vv_mail")
    if m is None:
        await q.edit_message_text("⚠️ Se perdió la propuesta.")
        return
    await q.edit_message_text("✉️ Programando el envío…")
    import sys as _svsn
    _svsn.path.insert(0, "/opt/me-harness")
    from agents import vivo as _vivosn
    from datetime import datetime as _dtsn, timedelta as _tdsn, timezone as _tzsn
    # hora AR = UTC-3 (patrón _boletin_enviar_ahora; datetime.now() naive del VPS va en UTC)
    cuando = (_dtsn.now(_tzsn.utc) - _tdsn(hours=3) + _tdsn(minutes=3)).strftime("%Y-%m-%d %H:%M:%S")
    prop = {"subject": m["subject"], "pre_header": m["pre_header"], "hero_title": m["hero_title"],
            "hero_quote": m["hero_quote"], "contexto_parrafos": m["contexto_parrafos"]}
    try:
        res = await asyncio.to_thread(_vivosn.mail_crear_y_programar, prop, m["segmento"], cuando)
    except Exception as e:
        res = {"ok": False, "error": str(e)}
    m["campaign_id"] = res.get("campaign_id")  # se guarda igual: ✖️ Cancelar borra el draft huérfano
    if not res.get("ok"):
        await q.message.reply_text(
            f"❌ No pude programar el envío: {res.get('error')}", reply_markup=_vv_mail_kb())
        return
    await q.message.reply_text(
        f"✅ Newsletter programado para las {cuando} (ARG) a {res.get('recipients', '?')} "
        f"destinatario(s) — {m['segmento_label']}.", parse_mode="HTML")


async def _vv_mail_cancel(update, context, q):
    m = context.user_data.pop("vv_mail", None)
    context.user_data.pop("vv_mail_tags", None)
    if m and m.get("campaign_id"):
        import sys as _svmc
        _svmc.path.insert(0, "/opt/me-harness")
        from agents import vivo as _vivomc
        try:
            await asyncio.to_thread(_vivomc.mail_cancelar, m["campaign_id"])
        except Exception:
            pass
    await q.edit_message_text("✖️ Newsletter cancelado.")


# ── /vivo difundir — re-difusión genérica (foto o placa + copy editable) ────────
async def _vv_cmd_difundir(update, context):
    import sys as _svd
    _svd.path.insert(0, "/opt/me-harness")
    from agents import vivo as _vivod
    estado = await asyncio.to_thread(_vivod.cargar_estado)
    if not estado:
        await update.message.reply_text("No hay ninguna cobertura EN VIVO activa.")
        return
    fotos = (estado.get("fotos_publicadas") or [])[-6:]
    dif = {
        "estado": estado, "imagen_media_id": (fotos[-1]["id"] if fotos else None),
        "usar_placa": not fotos,
        "texto": estado.get("resumen_contexto") or estado.get("titulo_nota") or "",
        "canales": _vv_canales_default(), "_fotos": fotos,
    }
    context.user_data["vv_dif"] = dif
    await update.message.reply_text(_vv_dif_texto(dif), parse_mode="HTML", reply_markup=_vv_dif_kb(dif))


def _vv_dif_texto(d: dict) -> str:
    import html as _h
    canales_on = [_VV_CANAL_LABEL[k] for k in _VV_CANAL_ORDEN if d["canales"].get(k)]
    img = "placa" if d.get("usar_placa") else f"foto #{d.get('imagen_media_id')}"
    return (
        f"🚀 <b>Re-difusión — {_h.escape(d['estado'].get('nombre', '?'))}</b>\n\n"
        f"🖼 Imagen: {img}\n\n"
        f"{_h.escape((d.get('texto') or '')[:500])}\n\n"
        f"📡 Canales: {', '.join(canales_on) or '—'}\n\n"
        "Elegí imagen, editá el texto si querés, y tocá 🚀 Difundir.")


def _vv_dif_kb(d: dict) -> dict:
    rows, row = [], []
    for f in d.get("_fotos") or []:
        icon = "🔘" if (not d.get("usar_placa") and f["id"] == d.get("imagen_media_id")) else "⚪"
        row.append({"text": f"{icon} #{f['id']}", "callback_data": f"vv_dif_pic:{f['id']}"})
        if len(row) == 3:
            rows.append(row); row = []
    if row:
        rows.append(row)
    icon_placa = "🔘" if d.get("usar_placa") else "⚪"
    rows.append([{"text": f"{icon_placa} Usar placa", "callback_data": "vv_dif_placa"}])
    rows.append([{"text": "✏️ Editar texto", "callback_data": "vv_dif_txt"},
                 {"text": "📡 Canales", "callback_data": "vv_canales"}])
    rows.append([{"text": "🚀 Difundir", "callback_data": "vv_dif_go"},
                 {"text": "✖️ Cancelar", "callback_data": "vv_dif_cancel"}])
    return {"inline_keyboard": rows}


async def _vv_dif_texto_capturar(update, context, text_in: str):
    d = context.user_data.get("vv_dif")
    if d is None:
        await update.message.reply_text("⚠️ Se perdió el panel. Reabrí con «/vivo difundir».")
        return
    d["texto"] = text_in.strip()
    await update.message.reply_text(_vv_dif_texto(d), parse_mode="HTML", reply_markup=_vv_dif_kb(d))


async def _vv_dif_go(update, context, q):
    d = context.user_data.get("vv_dif")
    if d is None:
        await q.edit_message_text("⚠️ Se perdió el panel.")
        return
    await q.edit_message_text("🚀 Difundiendo…")
    import sys as _svdg2
    _svdg2.path.insert(0, "/opt/me-harness")
    from agents import vivo as _vivodg2
    estado = d["estado"]

    def _work():
        img_bytes = None
        try:
            if d.get("usar_placa"):
                # medias = {"feed": {"id","url"}, "story": {...}, "wide": {...}}
                medias = estado.get("medias") or {}
                surl = (medias.get("feed") or {}).get("url") or (medias.get("wide") or {}).get("url")
                if isinstance(surl, str) and surl.startswith("http"):
                    r = requests.get(surl, timeout=20)
                    if r.ok:
                        img_bytes = r.content
            else:
                foto = next((f for f in (d.get("_fotos") or [])
                            if f.get("id") == d.get("imagen_media_id")), None)
                if foto and foto.get("url"):
                    r = requests.get(foto["url"], timeout=20)
                    if r.ok:
                        img_bytes = r.content
        except Exception:
            img_bytes = None
        texto = d.get("texto") or ""
        # twitter incluido para que el toggle 🐦 tenga efecto (sin URL en el texto → gratis)
        copys = {"twitter": texto, "telegram": texto, "facebook": texto,
                 "linkedin": texto, "ig_caption": texto}
        return _vivodg2.difundir(copys, img_bytes, d["canales"], estado.get("nota_url", ""))

    try:
        res = await asyncio.to_thread(_work)
    except Exception as e:
        res = {"error": str(e)}
    if isinstance(res, dict) and res.get("error"):
        await q.message.reply_text(
            f"⚠️ La difusión terminó con error: {res['error']}\nEl panel sigue activo — reintentá.",
            reply_markup=_vv_dif_kb(d))
        return
    context.user_data.pop("vv_dif", None)
    fallas = [k for k, v in (res or {}).items()
              if isinstance(v, dict) and not (v.get("ok") or v.get("id"))]
    extra = ("\n⚠️ Fallaron: " + ", ".join(fallas)) if fallas else ""
    await q.message.reply_text("✅ Re-difusión enviada." + extra)


# ── Dispatcher único de todos los botones del ecosistema /vivo ("^vv") ──────────
async def handle_vv_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    data = q.data

    if data.startswith("vvb_"):
        await _handle_vvb_button(update, context, q, data)
        return

    if data.startswith("vv_ch:"):
        canales = _vv_canales_activo(context)
        if canales is None:
            await q.edit_message_text("⚠️ Se perdió el panel.")
            return
        canal = data.split(":", 1)[1]
        if canal in canales:
            canales[canal] = not canales[canal]
        await q.edit_message_text("📡 Tocá para activar/desactivar cada canal:",
                                  reply_markup=_vv_canales_kb(canales))
        return

    if data == "vv_canales":
        canales = _vv_canales_activo(context)
        if canales is None:
            await q.edit_message_text("⚠️ No hay ningún panel activo.")
            return
        await q.edit_message_text("📡 Tocá para activar/desactivar cada canal:",
                                  reply_markup=_vv_canales_kb(canales))
        return

    if data == "vv_ch_back":
        if context.user_data.get("vv_cierre") is not None:
            c = context.user_data["vv_cierre"]
            await q.edit_message_text(_vv_cierre_texto(c), parse_mode="HTML", reply_markup=_vv_cierre_kb(c))
        elif context.user_data.get("vv_dif") is not None:
            d = context.user_data["vv_dif"]
            await q.edit_message_text(_vv_dif_texto(d), parse_mode="HTML", reply_markup=_vv_dif_kb(d))
        else:
            cfg = context.user_data.get("vivo_cfg")
            if cfg is not None:
                await q.edit_message_text(_vv_panel_text(cfg), parse_mode="HTML", reply_markup=_vv_kb(cfg))
        return

    # Panel principal de preparación
    if data == "vv_titulo":
        _vv_espera(context, "awaiting_vv_titulo")
        await q.message.reply_text("📰 Escribí el TÍTULO de la nota.")
        return
    if data == "vv_placa":
        _vv_espera(context, "awaiting_vv_placa")
        await q.message.reply_text(
            "🪧 Mandame 3 líneas (una por renglón):\n1) Título de placa\n2) Bajada\n3) Fecha y lugar")
        return
    if data == "vv_foto":
        _vv_espera(context, "awaiting_vv_foto")
        await q.message.reply_text("🖼 Mandame la FOTO de fondo para la placa.")
        return
    if data == "vv_enfoque":
        _vv_espera(context, "awaiting_vv_enfoque")
        await q.message.reply_text("🎯 Escribí el ENFOQUE editorial de la cobertura.")
        return
    if data == "vv_links":
        _vv_espera(context, "awaiting_vv_links")
        await q.message.reply_text(
            "🔗 Mandame uno o más links de contexto (podés poner varios en el mismo mensaje).")
        return
    if data == "vv_preview":
        await _vv_preview(update, context, q)
        return
    if data == "vv_arrancar":
        await _vv_arrancar(update, context, q)
        return
    if data == "vv_cancel":
        context.user_data.pop("vivo_cfg", None)
        _vv_espera(context)
        await _vv_edit(q, "✖️ Preparación EN VIVO cancelada.")
        return
    if data == "vv_back":
        cfg = context.user_data.get("vivo_cfg")
        if cfg is not None:
            await q.message.reply_text(_vv_panel_text(cfg), parse_mode="HTML", reply_markup=_vv_kb(cfg))
        else:
            await q.message.reply_text("⚠️ Se perdió el panel. Abrí de nuevo con /vivo <nombre>.")
        return

    # Cierre de cobertura
    if data.startswith("vv_port:"):
        c = context.user_data.get("vv_cierre")
        if c is None:
            await q.edit_message_text("⚠️ Se perdió el panel de cierre.")
            return
        mid = data.split(":", 1)[1]
        try:
            c["portada_media_id"] = int(mid)
        except ValueError:
            c["portada_media_id"] = mid
        await q.edit_message_text(_vv_cierre_texto(c), parse_mode="HTML", reply_markup=_vv_cierre_kb(c))
        return
    if data == "vv_cierre_cancel":
        context.user_data.pop("vv_cierre", None)
        await q.edit_message_text("✖️ Cierre cancelado (la cobertura sigue abierta).")
        return
    if data == "vv_cierre_go":
        await _vv_cierre_go(update, context, q)
        return

    # Newsletter de cierre
    if data == "vv_mail_all":
        m = context.user_data.get("vv_mail")
        if m is None:
            await q.edit_message_text("⚠️ Se perdió la propuesta.")
            return
        m["segmento"] = None
        m["segmento_label"] = "Toda la base"
        await q.edit_message_text(_vv_mail_texto(m), parse_mode="HTML", reply_markup=_vv_mail_kb())
        return
    if data == "vv_mail_tag":
        await _vv_mail_tags_mostrar(update, context, q)
        return
    if data.startswith("vv_mail_pg:"):
        pg = int(data.split(":", 1)[1])
        tags = context.user_data.get("vv_mail_tags") or []
        await q.edit_message_text("🏷 Elegí un tag:", reply_markup=_vv_mail_tags_kb(tags, pg))
        return
    if data.startswith("vv_mail_tag:"):
        idx = int(data.split(":", 1)[1])
        tags = context.user_data.get("vv_mail_tags") or []
        m = context.user_data.get("vv_mail")
        if m is None or idx >= len(tags):
            await q.edit_message_text("⚠️ Se perdió la propuesta.")
            return
        tag = tags[idx]
        # formato de segmento de FluentCRM (el mismo de newsletter._segment_for_publico)
        m["segmento"] = [{"list": "all", "tag": tag["id"]}]
        m["segmento_label"] = f"Tag: {tag.get('title') or tag['id']}"
        await q.edit_message_text(_vv_mail_texto(m), parse_mode="HTML", reply_markup=_vv_mail_kb())
        return
    if data == "vv_mail_back":
        m = context.user_data.get("vv_mail")
        if m is None:
            await q.edit_message_text("⚠️ Se perdió la propuesta.")
            return
        await q.edit_message_text(_vv_mail_texto(m), parse_mode="HTML", reply_markup=_vv_mail_kb())
        return
    if data == "vv_mail_adj":
        context.user_data["awaiting_vv_mail_adj"] = True
        await q.message.reply_text("✏️ Escribí el ajuste para el newsletter (qué cambiar).")
        return
    if data == "vv_mail_send":
        await _vv_mail_send(update, context, q)
        return
    if data == "vv_mail_cancel":
        await _vv_mail_cancel(update, context, q)
        return

    # Re-difusión
    if data.startswith("vv_dif_pic:"):
        d = context.user_data.get("vv_dif")
        if d is None:
            await q.edit_message_text("⚠️ Se perdió el panel.")
            return
        mid = data.split(":", 1)[1]
        try:
            d["imagen_media_id"] = int(mid)
        except ValueError:
            d["imagen_media_id"] = mid
        d["usar_placa"] = False
        await q.edit_message_text(_vv_dif_texto(d), parse_mode="HTML", reply_markup=_vv_dif_kb(d))
        return
    if data == "vv_dif_placa":
        d = context.user_data.get("vv_dif")
        if d is None:
            await q.edit_message_text("⚠️ Se perdió el panel.")
            return
        d["usar_placa"] = True
        await q.edit_message_text(_vv_dif_texto(d), parse_mode="HTML", reply_markup=_vv_dif_kb(d))
        return
    if data == "vv_dif_txt":
        context.user_data["awaiting_vv_dif_txt"] = True
        await q.message.reply_text("✏️ Mandame el texto nuevo para la difusión.")
        return
    if data == "vv_dif_cancel":
        context.user_data.pop("vv_dif", None)
        await q.edit_message_text("✖️ Difusión cancelada.")
        return
    if data == "vv_dif_go":
        await _vv_dif_go(update, context, q)
        return


REDACCION_BLOCKED_FILE = "/opt/redaccion-app/blocked.json"

async def cmd_desbloquear(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Lista y desbloquea IPs bloqueadas en redaccion.mundoempresarial.ar (2 intentos fallidos = bloqueo)."""
    import json as _jdb
    try:
        with open(REDACCION_BLOCKED_FILE) as _f:
            data = _jdb.load(_f)
    except Exception:
        data = {}
    blocked = {ip: r for ip, r in data.items() if isinstance(r, dict) and r.get("blocked")}
    if not blocked:
        await update.message.reply_text("✅ No hay IPs bloqueadas en redaccion.mundoempresarial.ar.")
        return
    lineas = [f"• <code>{ip}</code> — {r.get('count', 0)} fallos, último {r.get('last_fail', '?')}"
              for ip, r in blocked.items()]
    txt = "🔒 <b>IPs bloqueadas en redacción</b>" + chr(10) + chr(10) + chr(10).join(lineas)
    rows = [[InlineKeyboardButton(f"🔓 Desbloquear {ip}", callback_data=f"desbloq_{ip}")] for ip in blocked]
    rows.append([InlineKeyboardButton("🔓 Desbloquear TODAS", callback_data="desbloq___ALL__")])
    await update.message.reply_text(txt, parse_mode="HTML", reply_markup=InlineKeyboardMarkup(rows))


async def handle_desbloq_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Botones del /desbloquear: quita una IP o vacía todo el blocked.json de la app de redacción."""
    query = update.callback_query
    import json as _jdb
    target = query.data[len("desbloq_"):]
    try:
        with open(REDACCION_BLOCKED_FILE) as _f:
            data = _jdb.load(_f)
    except Exception:
        data = {}
    if target == "__ALL__":
        n = sum(1 for r in data.values() if isinstance(r, dict) and r.get("blocked"))
        data = {}
        msg = f"✅ Desbloqueadas todas las IPs ({n})."
    else:
        data.pop(target, None)
        msg = f"✅ Desbloqueada <code>{target}</code>."
    try:
        with open(REDACCION_BLOCKED_FILE, "w") as _f:
            _jdb.dump(data, _f, indent=2)
    except Exception as e:
        await query.answer(f"Error al guardar: {e}", show_alert=True)
        return
    await query.answer("Desbloqueado ✅")
    await query.edit_message_text(msg + chr(10) + chr(10) + "Ya podés entrar a redaccion.mundoempresarial.ar.",
                                  parse_mode="HTML")


def main():
    # Esperar a que la instancia anterior libere el lock (evita 409 Conflict)
    _wait_for_lock_release()

    app = Application.builder().token(TELEGRAM_TOKEN).post_init(_post_init).build()
    # Control de acceso GLOBAL: corre antes que todo (group=-1). Solo el operador pasa;
    # a cualquier otro le frena la cadena entera de handlers. Ver _solo_admin.
    if not _admin_ids():
        logger.warning("⚠️ ADMIN_CHAT_ID vacío — el guard bloqueará a TODOS (fail-closed).")
    app.add_handler(TypeHandler(Update, _solo_admin), group=-1)
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("desbloquear", cmd_desbloquear))
    app.add_handler(CommandHandler("STOP", cmd_stop))
    app.add_handler(CommandHandler("RESUME", cmd_resume))
    app.add_handler(CommandHandler("comandos", cmd_comandos))
    app.add_handler(CommandHandler("help", cmd_comandos))
    app.add_handler(CommandHandler("borrar", cmd_borrar))
    app.add_handler(CommandHandler("editar", cmd_editar))
    app.add_handler(CommandHandler("hilo", cmd_hilo))
    app.add_handler(CommandHandler("fuentes", cmd_fuentes))
    app.add_handler(CommandHandler("ingesta", cmd_ingesta))
    app.add_handler(CommandHandler("briefing", cmd_briefing))
    app.add_handler(CommandHandler("nutricion", cmd_nutricion))
    app.add_handler(CommandHandler("ciclaje", cmd_ciclaje))
    app.add_handler(CommandHandler("programadas", cmd_programadas))
    app.add_handler(CommandHandler("eventos", cmd_eventos))
    app.add_handler(CommandHandler("coladepublicacion", cmd_coladepublicacion))
    app.add_handler(CommandHandler("inst", cmd_inst))
    app.add_handler(CommandHandler("reglas", cmd_reglas))
    app.add_handler(CommandHandler("publinotas", cmd_publinotas))
    app.add_handler(CommandHandler("kwtemp", cmd_kwtemp))
    app.add_handler(CommandHandler("creditos", cmd_creditos))
    app.add_handler(CommandHandler("testtwitter", cmd_testtwitter))
    app.add_handler(CommandHandler("stats", cmd_stats))
    app.add_handler(CommandHandler("frases", cmd_frases))
    app.add_handler(CommandHandler("encuesta", cmd_encuesta))
    app.add_handler(CommandHandler("set_frases_base", cmd_set_frases_base))
    app.add_handler(CommandHandler("publicador", cmd_publicador))
    app.add_handler(CommandHandler("lector", cmd_lector))
    app.add_handler(CommandHandler("editor", cmd_editor))
    app.add_handler(CommandHandler("pipeline", cmd_pipeline))
    app.add_handler(CommandHandler("rutina", cmd_rutina))
    app.add_handler(CommandHandler("notamanual", cmd_notamanual))
    app.add_handler(CommandHandler("evento", cmd_evento))
    app.add_handler(CommandHandler("input_evento", cmd_input_evento))
    app.add_handler(CommandHandler("vivo", cmd_vivo))
    app.add_handler(CommandHandler("campania", cmd_campania))
    app.add_handler(CallbackQueryHandler(handle_edito_button, pattern="^edito_"))
    app.add_handler(CallbackQueryHandler(handle_pubx_button, pattern="^pubx_"))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_link))
    app.add_handler(MessageHandler(filters.PHOTO & filters.CaptionRegex(r"^/set_frases_base"), cmd_set_frases_base))
    app.add_handler(MessageHandler(filters.PHOTO, handle_photo_message))
    app.add_handler(MessageHandler(filters.VOICE | filters.AUDIO | filters.VIDEO, handle_evento_media))
    app.add_handler(MessageHandler(filters.Document.ALL, handle_document))
    # Patrón más específico para /borrar (confirmar/cancelar), antes del nuevo flow de /editar
    app.add_handler(CallbackQueryHandler(handle_delete_button, pattern="^del_(confirm|cancel)$"))
    app.add_handler(CallbackQueryHandler(handle_thread_button, pattern="^thread_"))
    app.add_handler(CallbackQueryHandler(handle_sources_button, pattern="^(src_|srcdel_)"))
    app.add_handler(CallbackQueryHandler(handle_edit_button, pattern="^(edit_|setcat_|deltoggle_|del_execute|pubtoggle_|pub_execute)"))
    app.add_handler(CallbackQueryHandler(on_finde_approval_cb, pattern="^fap_"))
    app.add_handler(CallbackQueryHandler(handle_notamanual_button, pattern="^nm_"))
    app.add_handler(CallbackQueryHandler(handle_evento_button, pattern="^ev_"))
    app.add_handler(CallbackQueryHandler(handle_iev_button, pattern="^iev_"))
    app.add_handler(CallbackQueryHandler(handle_vv_button, pattern="^vv"))
    app.add_handler(CallbackQueryHandler(handle_efem_button, pattern="^efem_"))
    app.add_handler(CallbackQueryHandler(handle_desbloq_button, pattern="^desbloq_"))
    app.add_handler(CallbackQueryHandler(handle_button))

    # Programar tareas automáticas en Argentina (UTC-3)
    from datetime import timezone, timedelta
    tz_arg = timezone(timedelta(hours=-3))
    job_queue = app.job_queue


    # Aprendizaje de hashtags a las 23:15 ARG
    job_queue.run_daily(
        _learn_hashtags_daily,
        time=dtime(hour=23, minute=15, tzinfo=tz_arg),
        name="ht_learning",
    )
    logger.info("HT learning programado para las 23:15 ARG")

    # Reporte GA4 semanal: lunes 8 AM ARG
    job_queue.run_daily(
        _ga4_weekly_report,
        time=dtime(hour=8, minute=0, tzinfo=tz_arg),
        days=(0,),
        name="ga4_weekly",
    )
    logger.info("GA4 weekly report programado para los lunes 08:00 ARG")

    # Health check semanal de servicios pagos: lunes 9 AM ARG
    job_queue.run_daily(
        send_weekly_credits_check,
        time=dtime(hour=9, minute=0, tzinfo=tz_arg),
        days=(0,),  # 0 = lunes en python-telegram-bot
        name="weekly_credits",
    )
    logger.info("Weekly credits check programado para los lunes 09:00 ARG")

    # Re-registrar scheduled jobs persistidos (sobreviven redeploys)
    try:
        _restore_scheduled_jobs(app)
    except Exception as e:
        logger.warning(f"restore_scheduled_jobs: {e}")

    logger.info("Bot iniciado y esperando links...")
    app.run_polling(
        drop_pending_updates=True,
        allowed_updates=["message", "callback_query", "my_chat_member", "edited_message"],
    )


if __name__ == "__main__":
    main()
