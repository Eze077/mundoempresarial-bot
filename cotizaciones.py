"""
cotizaciones.py — Monitor y publicador de cotizaciones para MundoEmpresarial.ar

Assets: Dólar (Oficial/Blue/MEP/CCL), Euro, Real, Yuan, Oro (PAXG), BTC
Fuentes: dolarapi.com · ambito.com (yuan) · Binance (PAXGUSDT + BTCUSDT)
Jobs:    apertura (Lun-Vie 10:00) · cierre (Lun-Vie 15:00) · resumen x3 diarios
         · poll cada 5 min (variación ±X%) · baselines por asset

Integración: llamar init() desde main.py antes de registrar handlers/jobs.
"""

import os
import json
import logging
import asyncio
import requests
from datetime import datetime, timezone, timedelta, time as dtime
from requests_oauthlib import OAuth1
from telegram import InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes

logger = logging.getLogger(__name__)

# ── Paths ────────────────────────────────────────────────────────────────────
_BASE_DIR    = "/opt/mundoempresarial-bot"
_CONFIG_FILE = f"{_BASE_DIR}/cotizaciones_config.json"
_STATE_FILE  = f"{_BASE_DIR}/cotizaciones_state.json"

# ── Timezone ─────────────────────────────────────────────────────────────────
ARG_TZ = timezone(timedelta(hours=-3))

# ── Defaults ─────────────────────────────────────────────────────────────────
_DEFAULT_CONFIG = {
    "thresholds": {
        "oficial": 2.0,
        "blue":    2.0,
        "mep":     2.0,
        "ccl":     2.0,
        "euro":    3.0,
        "real":    3.0,
        "yuan":    3.0,
        "oro":     2.0,
        "btc":     5.0,
    },
    "apertura_time": "10:00",
    "cierre_time":   "15:00",
    "resumen_times": ["08:00", "13:00", "20:00"],
    "cooldown_min":  60,
    "active": {
        "oficial": True,
        "blue":    True,
        "mep":     True,
        "ccl":     True,
        "euro":    True,
        "real":    True,
        "yuan":    True,
        "oro":     True,
        "btc":     True,
    },
}

# ── Runtime deps (inyectados desde main.py via init()) ───────────────────────
_CHANNEL:    str = ""
_TW_KEY:        str = ""
_TW_SECRET:     str = ""
_TW_TOKEN:      str = ""
_TW_TSECRET:    str = ""
_ADMIN_CHAT_ID: str = ""
_paused_fn = lambda: False


def init(channel: str, tw_key: str, tw_secret: str, tw_token: str,
         tw_tsecret: str, admin_chat_id: str = "", paused_fn=None) -> None:
    global _CHANNEL, _TW_KEY, _TW_SECRET, _TW_TOKEN, _TW_TSECRET, _ADMIN_CHAT_ID, _paused_fn
    _CHANNEL        = channel
    _TW_KEY         = tw_key
    _TW_SECRET      = tw_secret
    _TW_TOKEN       = tw_token
    _TW_TSECRET     = tw_tsecret
    _ADMIN_CHAT_ID  = admin_chat_id
    if paused_fn:
        _paused_fn = paused_fn


# ── Config & State I/O ───────────────────────────────────────────────────────
_config_cache: dict | None = None
_state_cache:  dict | None = None


def load_config() -> dict:
    global _config_cache
    if _config_cache is not None:
        return _config_cache
    try:
        with open(_CONFIG_FILE) as f:
            _config_cache = json.load(f)
    except Exception:
        _config_cache = json.loads(json.dumps(_DEFAULT_CONFIG))
    return _config_cache


def save_config(data: dict) -> None:
    global _config_cache
    _config_cache = data
    os.makedirs(_BASE_DIR, exist_ok=True)
    with open(_CONFIG_FILE, "w") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def load_state() -> dict:
    global _state_cache
    if _state_cache is not None:
        return _state_cache
    try:
        with open(_STATE_FILE) as f:
            _state_cache = json.load(f)
    except Exception:
        _state_cache = {"baselines": {}, "last_alert_ts": {}}
    return _state_cache


def save_state(data: dict) -> None:
    global _state_cache
    _state_cache = data
    os.makedirs(_BASE_DIR, exist_ok=True)
    with open(_STATE_FILE, "w") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


# ── Data fetching ────────────────────────────────────────────────────────────
_DOLAR_CASA_MAP = {
    "oficial":          "oficial",
    "blue":             "blue",
    "bolsa":            "mep",
    "contadoconliqui":  "ccl",
}


def _fetch_dolarapi() -> dict:
    result = {}
    try:
        r = requests.get("https://dolarapi.com/v1/dolares", timeout=10)
        if r.status_code == 200:
            for item in r.json():
                casa = item.get("casa", "").lower().replace(" ", "")
                key  = _DOLAR_CASA_MAP.get(casa)
                if key:
                    result[key] = {
                        "compra":  item.get("compra"),
                        "venta":   item.get("venta"),
                        "nombre":  item.get("nombre", key),
                    }
    except Exception as e:
        logger.warning(f"dolarapi dolares: {e}")

    for code, key in (("eur", "euro"), ("brl", "real")):
        try:
            r = requests.get(f"https://dolarapi.com/v1/cotizaciones/{code}", timeout=10)
            if r.status_code == 200:
                d = r.json()
                result[key] = {"compra": d.get("compra"), "venta": d.get("venta")}
        except Exception as e:
            logger.warning(f"dolarapi {code}: {e}")

    return result


def _fetch_yuan() -> dict | None:
    """Yuan chino (CNY) desde Ambito mercados."""
    try:
        r = requests.get(
            "https://mercados.ambito.com/dolar/yuan/variacion",
            timeout=10,
            headers={"User-Agent": "Mozilla/5.0"},
        )
        if r.status_code == 200:
            d = r.json()
            compra = float(str(d.get("compra", "0")).replace(",", "."))
            venta  = float(str(d.get("venta",  "0")).replace(",", "."))
            if venta:
                return {"compra": compra, "venta": venta}
    except Exception as e:
        logger.warning(f"ambito yuan: {e}")
    return None


def _fetch_oro() -> dict | None:
    """Oro via PAXG/USDT en Binance (1 PAXG = 1 troy oz de oro)."""
    try:
        r = requests.get(
            "https://api.binance.com/api/v3/ticker/price?symbol=PAXGUSDT",
            timeout=10,
        )
        if r.status_code == 200:
            return {"precio": float(r.json()["price"]), "moneda": "USD/oz"}
    except Exception as e:
        logger.warning(f"binance paxg: {e}")
    return None


def _fetch_btc() -> dict | None:
    try:
        r = requests.get(
            "https://api.binance.com/api/v3/ticker/price?symbol=BTCUSDT",
            timeout=10,
        )
        if r.status_code == 200:
            return {"precio": float(r.json()["price"]), "moneda": "USD"}
    except Exception as e:
        logger.warning(f"binance btc: {e}")
    return None


def fetch_all() -> dict:
    data = _fetch_dolarapi()
    yuan = _fetch_yuan()
    if yuan:
        data["yuan"] = yuan
    oro = _fetch_oro()
    if oro:
        data["oro"] = oro
    btc = _fetch_btc()
    if btc:
        data["btc"] = btc
    return data


def _get_price(asset: str, d: dict) -> float | None:
    """Extrae el precio relevante (venta para fiat, precio para oro/btc)."""
    if asset in ("oro", "btc"):
        return d.get("precio")
    return d.get("venta")


# ── Baselines ────────────────────────────────────────────────────────────────
def capture_baseline(assets: list[str]) -> None:
    cotiz = fetch_all()
    state = load_state()
    today = datetime.now(ARG_TZ).strftime("%Y-%m-%d")
    for asset in assets:
        if asset not in cotiz:
            continue
        price = _get_price(asset, cotiz[asset])
        if price:
            state.setdefault("baselines", {})[asset] = {"price": price, "date": today}
    save_state(state)
    logger.info(f"Baselines capturados para: {assets}")


# ── Formatters ───────────────────────────────────────────────────────────────
_EMOJI = {
    "oficial": "💵", "blue": "💵", "mep": "💵", "ccl": "💵",
    "euro": "💶", "real": "🇧🇷", "yuan": "🇨🇳", "oro": "🥇", "btc": "₿",
}
_NOMBRES = {
    "oficial": "Dólar Oficial",
    "blue":    "Dólar Blue",
    "mep":     "Dólar MEP",
    "ccl":     "Dólar CCL",
    "euro":    "Euro",
    "real":    "Real",
    "yuan":    "Yuan",
    "oro":     "Oro (PAXG)",
    "btc":     "Bitcoin",
}
_URLS = {
    "oficial": "https://mundoempresarial.ar/cotizaciones/dolar-oficial/",
    "blue":    "https://mundoempresarial.ar/cotizaciones/dolar-blue/",
    "mep":     "https://mundoempresarial.ar/cotizaciones/dolar-mep/",
    "ccl":     "https://mundoempresarial.ar/cotizaciones/dolar-ccl/",
    "euro":    "https://mundoempresarial.ar/cotizaciones/euro/",
    "real":    "https://mundoempresarial.ar/cotizaciones/real/",
    "yuan":    "https://mundoempresarial.ar/cotizaciones/",
    "oro":     "https://mundoempresarial.ar/cotizaciones/",
    "btc":     "https://mundoempresarial.ar/cotizaciones/",
}
_MAIN_URL = "https://mundoempresarial.ar/cotizaciones/"


def _fmt_price(asset: str, d: dict, variation_pct: float | None = None) -> str:
    em   = _EMOJI.get(asset, "💱")
    name = _NOMBRES.get(asset, asset.upper())
    if asset in ("oro", "btc"):
        p = d.get("precio", 0)
        base = f"{em} {name}: ${p:,.0f} {d.get('moneda', 'USD')}"
    else:
        compra = d.get("compra")
        venta  = d.get("venta")
        if compra and venta:
            base = f"{em} {name}: ${compra:,.0f} / ${venta:,.0f}"
        elif venta:
            base = f"{em} {name}: ${venta:,.0f}"
        else:
            return f"{em} {name}: —"

    if variation_pct is not None:
        arrow = "🔺" if variation_pct > 0 else "🔻"
        base += f" {arrow}{variation_pct:+.1f}%"
    return base


def _utm(base_url: str, campaign: str) -> str:
    sep = "&" if "?" in base_url else "?"
    return f"{base_url}{sep}utm_source=telegram&utm_medium=cotizaciones&utm_campaign={campaign}"


def build_apertura(cotiz: dict) -> str:
    now = datetime.now(ARG_TZ)
    lines = [f"💵 *Dólar — Apertura | {now.strftime('%a %d/%m').capitalize()}*\n"]
    for key in ("oficial", "blue", "mep", "ccl"):
        if key in cotiz:
            lines.append(_fmt_price(key, cotiz[key]))
    lines.append(f"\n🔗 {_utm(_MAIN_URL, 'apertura')}")
    return "\n".join(lines)


def build_cierre(cotiz: dict, baselines: dict) -> str:
    now = datetime.now(ARG_TZ)
    lines = [f"💵 *Dólar — Cierre | {now.strftime('%a %d/%m').capitalize()}*\n"]
    today = now.strftime("%Y-%m-%d")
    for key in ("oficial", "blue", "mep", "ccl"):
        if key not in cotiz:
            continue
        var = None
        bl  = baselines.get(key, {})
        if bl.get("date") == today and bl.get("price"):
            cur = _get_price(key, cotiz[key])
            if cur:
                var = (cur - bl["price"]) / bl["price"] * 100
        lines.append(_fmt_price(key, cotiz[key], var))
    lines.append(f"\n🔗 {_utm(_MAIN_URL, 'cierre')}")
    return "\n".join(lines)


def build_resumen(cotiz: dict, slot: str = "") -> str:
    now      = datetime.now(ARG_TZ)
    date_str = now.strftime("%a %d/%m — %H:%M").capitalize()
    label    = f"{slot} | {date_str}" if slot else date_str
    lines    = [f"📊 *Cotizaciones | {label}*\n"]

    for key in ("oficial", "blue", "mep", "ccl"):
        if key in cotiz:
            lines.append(_fmt_price(key, cotiz[key]))
    lines.append("")
    for key in ("euro", "real", "yuan", "oro", "btc"):
        if key in cotiz:
            lines.append(_fmt_price(key, cotiz[key]))

    lines.append(f"\n🔗 {_utm(_MAIN_URL, 'resumen')}")
    return "\n".join(lines)


def build_alert(asset: str, current: float, baseline: float) -> str:
    name = _NOMBRES.get(asset, asset.upper())
    em   = _EMOJI.get(asset, "💱")
    pct  = (current - baseline) / baseline * 100
    verb = "subió" if pct > 0 else "bajó"
    arrow = "🔺" if pct > 0 else "🔻"

    if asset in ("oro", "btc"):
        price_fmt = f"${current:,.0f} USD"
    else:
        price_fmt = f"${current:,.0f}"

    url = _utm(_URLS.get(asset, _MAIN_URL), f"variacion_{asset}")
    return (
        f"{arrow} *{em} {name} {verb} {abs(pct):.1f}%* → {price_fmt}\n"
        f"🔗 {url}"
    )


def _to_tweet(md_text: str) -> str:
    """Convierte markdown TG a texto plano apto para Twitter (≤280 chars)."""
    clean = (md_text
             .replace("*", "")
             .replace("_", "")
             .replace("`", ""))
    # Queda la URL sin markdown link
    lines = [l for l in clean.split("\n") if l.strip()]
    tweet = "\n".join(lines)
    if len(tweet) > 280:
        tweet = tweet[:277] + "…"
    return tweet


# ── Publisher ────────────────────────────────────────────────────────────────
async def _publish(bot, tg_text: str, tweet_text: str | None = None) -> None:
    # Canal público
    try:
        await bot.send_message(
            chat_id=_CHANNEL,
            text=tg_text,
            parse_mode="Markdown",
            disable_web_page_preview=True,
        )
    except Exception as e:
        logger.error(f"cotiz canal TG: {e}")

    # Chat privado del admin
    if _ADMIN_CHAT_ID:
        try:
            await bot.send_message(
                chat_id=int(_ADMIN_CHAT_ID),
                text=tg_text,
                parse_mode="Markdown",
                disable_web_page_preview=True,
            )
        except Exception as e:
            logger.error(f"cotiz admin TG: {e}")

    if _TW_KEY and tweet_text:
        try:
            auth = OAuth1(_TW_KEY, _TW_SECRET, _TW_TOKEN, _TW_TSECRET)
            r = requests.post(
                "https://api.twitter.com/2/tweets",
                json={"text": tweet_text[:280]},
                auth=auth,
                timeout=20,
            )
            if r.status_code == 201:
                logger.info(f"cotiz tweet OK: {r.json()['data']['id']}")
            else:
                logger.warning(f"cotiz tweet {r.status_code}: {r.text[:200]}")
        except Exception as e:
            logger.error(f"cotiz tweet: {e}")


# ── Job callbacks ────────────────────────────────────────────────────────────
async def job_baseline_fiat(context: ContextTypes.DEFAULT_TYPE) -> None:
    await asyncio.to_thread(capture_baseline, ["oficial", "blue", "euro", "real", "yuan", "oro"])


async def job_baseline_mep(context: ContextTypes.DEFAULT_TYPE) -> None:
    await asyncio.to_thread(capture_baseline, ["mep", "ccl"])


async def job_baseline_btc(context: ContextTypes.DEFAULT_TYPE) -> None:
    await asyncio.to_thread(capture_baseline, ["btc"])


async def job_apertura(context: ContextTypes.DEFAULT_TYPE) -> None:
    cotiz = await asyncio.to_thread(fetch_all)
    text  = build_apertura(cotiz)
    await _publish(context.bot, text, _to_tweet(text))


async def job_cierre(context: ContextTypes.DEFAULT_TYPE) -> None:
    cotiz = await asyncio.to_thread(fetch_all)
    state = load_state()
    text  = build_cierre(cotiz, state.get("baselines", {}))
    await _publish(context.bot, text, _to_tweet(text))


async def job_resumen(context: ContextTypes.DEFAULT_TYPE) -> None:
    slot  = (context.job.data or {}).get("slot", "")
    cotiz = await asyncio.to_thread(fetch_all)
    text  = build_resumen(cotiz, slot)
    await _publish(context.bot, text, _to_tweet(text))


async def job_poll(context: ContextTypes.DEFAULT_TYPE) -> None:
    if _paused_fn():
        return

    cfg        = load_config()
    state      = load_state()
    cotiz      = await asyncio.to_thread(fetch_all)
    now        = datetime.now(ARG_TZ)
    today      = now.strftime("%Y-%m-%d")
    is_weekday = now.weekday() < 5
    thresholds = cfg.get("thresholds", _DEFAULT_CONFIG["thresholds"])
    active     = cfg.get("active",     _DEFAULT_CONFIG["active"])
    cooldown_s = cfg.get("cooldown_min", 60) * 60
    baselines  = state.get("baselines", {})
    last_ts    = state.get("last_alert_ts", {})
    changed    = False

    for asset, d in cotiz.items():
        if not active.get(asset, True):
            continue
        if asset != "btc" and not is_weekday:
            continue

        bl = baselines.get(asset, {})
        if bl.get("date") != today or not bl.get("price"):
            continue

        current = _get_price(asset, d)
        if not current:
            continue

        pct = abs((current - bl["price"]) / bl["price"] * 100)
        if pct < thresholds.get(asset, 3.0):
            continue

        last_str = last_ts.get(asset)
        if last_str:
            try:
                last_dt = datetime.fromisoformat(last_str)
                if (now - last_dt).total_seconds() < cooldown_s:
                    continue
            except Exception:
                pass

        text = build_alert(asset, current, bl["price"])
        await _publish(context.bot, text, _to_tweet(text))
        state.setdefault("last_alert_ts", {})[asset] = now.isoformat()
        changed = True

    if changed:
        save_state(state)


# ── Comando /cotizaciones ────────────────────────────────────────────────────
def _panel_text(cotiz: dict) -> str:
    now = datetime.now(ARG_TZ)
    lines = [f"💱 *Cotizaciones — {now.strftime('%d/%m %H:%M')}*\n"]
    for key in ("oficial", "blue", "mep", "ccl"):
        if key in cotiz:
            lines.append(_fmt_price(key, cotiz[key]))
    lines.append("")
    for key in ("euro", "real", "yuan", "oro", "btc"):
        if key in cotiz:
            lines.append(_fmt_price(key, cotiz[key]))
    return "\n".join(lines)


def _panel_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("🔄 Actualizar", callback_data="cotiz_refresh"),
        InlineKeyboardButton("⚙️ Config",     callback_data="cotiz_config"),
    ]])


async def cmd_cotizaciones(update, context: ContextTypes.DEFAULT_TYPE) -> None:
    cotiz = await asyncio.to_thread(fetch_all)
    await update.message.reply_text(
        _panel_text(cotiz),
        parse_mode="Markdown",
        reply_markup=_panel_kb(),
    )


# ── Panel config ─────────────────────────────────────────────────────────────
def _config_text() -> str:
    cfg = load_config()
    th  = cfg.get("thresholds", _DEFAULT_CONFIG["thresholds"])
    rt  = cfg.get("resumen_times", ["08:00", "13:00", "20:00"])
    return (
        "⚙️ *Config Cotizaciones*\n\n"
        "*Umbrales de variación:*\n"
        f"  • Dólar:    {th.get('blue', 2.0)}%\n"
        f"  • Euro/Real: {th.get('euro', 3.0)}%\n"
        f"  • Oro:      {th.get('oro', 2.0)}%\n"
        f"  • BTC:      {th.get('btc', 5.0)}%\n\n"
        f"*Apertura:* {cfg.get('apertura_time', '10:00')} \\| "
        f"*Cierre:* {cfg.get('cierre_time', '15:00')}\n"
        f"*Resúmenes:* {' · '.join(rt)}\n"
        f"*Cooldown alertas:* {cfg.get('cooldown_min', 60)} min\n\n"
        "Seleccioná qué cambiar:"
    )


def _config_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("💵 Umbral Dólar",  callback_data="cotiz_cfg_dolar"),
            InlineKeyboardButton("💶 Umbral Fiat",   callback_data="cotiz_cfg_fiat"),
        ],
        [
            InlineKeyboardButton("🥇 Umbral Oro",    callback_data="cotiz_cfg_oro"),
            InlineKeyboardButton("₿  Umbral BTC",    callback_data="cotiz_cfg_btc"),
        ],
        [
            InlineKeyboardButton("⏰ Horarios",      callback_data="cotiz_cfg_horarios"),
            InlineKeyboardButton("↩️ Volver",        callback_data="cotiz_refresh"),
        ],
    ])


_THRESHOLD_OPTIONS = [1.0, 2.0, 3.0, 5.0, 10.0]
_GROUP_META = {
    "dolar": ("Dólar (oficial/blue/mep/ccl)", ["oficial", "blue", "mep", "ccl"]),
    "fiat":  ("Euro / Real / Yuan",            ["euro", "real", "yuan"]),
    "oro":   ("Oro",                          ["oro"]),
    "btc":   ("Bitcoin",                      ["btc"]),
}


def _threshold_text(group: str) -> str:
    label, keys = _GROUP_META[group]
    th  = load_config().get("thresholds", _DEFAULT_CONFIG["thresholds"])
    cur = th.get(keys[0], 2.0)
    return f"⚙️ Umbral variación — *{label}*\n\nActual: *{cur}%*\nElegí el nuevo umbral:"


def _threshold_kb(group: str) -> InlineKeyboardMarkup:
    _, keys = _GROUP_META[group]
    th  = load_config().get("thresholds", _DEFAULT_CONFIG["thresholds"])
    cur = th.get(keys[0], 2.0)
    row = []
    rows = []
    for opt in _THRESHOLD_OPTIONS:
        mark = "✅ " if cur == opt else ""
        row.append(InlineKeyboardButton(
            f"{mark}{opt:.0f}%",
            callback_data=f"cotiz_th_{group}_{opt:.1f}",
        ))
        if len(row) == 3:
            rows.append(row); row = []
    if row:
        rows.append(row)
    rows.append([InlineKeyboardButton("↩️ Volver", callback_data="cotiz_config")])
    return InlineKeyboardMarkup(rows)


_HORA_OPTIONS = {
    "apertura": ["09:00", "09:30", "10:00", "10:30", "11:00"],
    "cierre":   ["14:00", "14:30", "15:00", "15:30", "16:00"],
    "res_man":  ["07:00", "07:30", "08:00", "08:30", "09:00"],
    "res_med":  ["12:00", "12:30", "13:00", "13:30", "14:00"],
    "res_noc":  ["19:00", "19:30", "20:00", "20:30", "21:00"],
}

_HORA_LABEL = {
    "apertura": "Apertura Dólar (Lun-Vie)",
    "cierre":   "Cierre Dólar (Lun-Vie)",
    "res_man":  "Resumen Mañana",
    "res_med":  "Resumen Mediodía",
    "res_noc":  "Resumen Noche",
}


def _horarios_menu_text() -> str:
    cfg = load_config()
    rt  = cfg.get("resumen_times", ["08:00", "13:00", "20:00"])
    return (
        "⏰ *Horarios de Cotizaciones*\n\n"
        f"  • Apertura dólar: *{cfg.get('apertura_time', '10:00')}* (Lun-Vie)\n"
        f"  • Cierre dólar:   *{cfg.get('cierre_time', '15:00')}* (Lun-Vie)\n"
        f"  • Resumen mañana: *{rt[0] if rt else '08:00'}* (todos los días)\n"
        f"  • Resumen mediodía: *{rt[1] if len(rt)>1 else '13:00'}* (todos los días)\n"
        f"  • Resumen noche:  *{rt[2] if len(rt)>2 else '20:00'}* (todos los días)\n\n"
        "_Los cambios de apertura/cierre reinician los jobs automáticamente._"
    )


def _horarios_menu_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🔓 Apertura Dólar",   callback_data="cotiz_cfg_hora_apertura")],
        [InlineKeyboardButton("🔒 Cierre Dólar",     callback_data="cotiz_cfg_hora_cierre")],
        [InlineKeyboardButton("🌅 Resumen Mañana",   callback_data="cotiz_cfg_hora_res_man")],
        [InlineKeyboardButton("☀️ Resumen Mediodía", callback_data="cotiz_cfg_hora_res_med")],
        [InlineKeyboardButton("🌙 Resumen Noche",    callback_data="cotiz_cfg_hora_res_noc")],
        [InlineKeyboardButton("↩️ Volver",           callback_data="cotiz_config")],
    ])


def _hora_picker_text(tipo: str) -> str:
    cfg   = load_config()
    rt    = cfg.get("resumen_times", ["08:00", "13:00", "20:00"])
    cur_map = {
        "apertura": cfg.get("apertura_time", "10:00"),
        "cierre":   cfg.get("cierre_time",   "15:00"),
        "res_man":  rt[0] if rt else "08:00",
        "res_med":  rt[1] if len(rt) > 1 else "13:00",
        "res_noc":  rt[2] if len(rt) > 2 else "20:00",
    }
    label  = _HORA_LABEL.get(tipo, tipo)
    actual = cur_map.get(tipo, "—")
    return f"⏰ *{label}*\n\nActual: *{actual}*\nElegí el nuevo horario:"


def _hora_picker_kb(tipo: str) -> InlineKeyboardMarkup:
    cfg   = load_config()
    rt    = cfg.get("resumen_times", ["08:00", "13:00", "20:00"])
    cur_map = {
        "apertura": cfg.get("apertura_time", "10:00"),
        "cierre":   cfg.get("cierre_time",   "15:00"),
        "res_man":  rt[0] if rt else "08:00",
        "res_med":  rt[1] if len(rt) > 1 else "13:00",
        "res_noc":  rt[2] if len(rt) > 2 else "20:00",
    }
    current = cur_map.get(tipo, "")
    options = _HORA_OPTIONS.get(tipo, [])
    rows = []
    row  = []
    for opt in options:
        mark = "✅ " if opt == current else ""
        # Usar HH_MM para que no rompa el callback_data (no se permite ":")
        opt_key = opt.replace(":", "_")
        row.append(InlineKeyboardButton(
            f"{mark}{opt}",
            callback_data=f"cotiz_hora_{tipo}_{opt_key}",
        ))
        if len(row) == 3:
            rows.append(row); row = []
    if row:
        rows.append(row)
    rows.append([InlineKeyboardButton("↩️ Volver", callback_data="cotiz_cfg_horarios")])
    return InlineKeyboardMarkup(rows)


# ── Callback handler ─────────────────────────────────────────────────────────
async def handle_cotiz_button(update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    data  = query.data

    if data == "cotiz_refresh":
        cotiz = await asyncio.to_thread(fetch_all)
        await query.edit_message_text(
            _panel_text(cotiz), parse_mode="Markdown", reply_markup=_panel_kb()
        )

    elif data == "cotiz_config":
        await query.edit_message_text(
            _config_text(), parse_mode="Markdown", reply_markup=_config_kb()
        )

    elif data == "cotiz_cfg_horarios":
        await query.edit_message_text(
            _horarios_menu_text(), parse_mode="Markdown", reply_markup=_horarios_menu_kb()
        )

    elif data.startswith("cotiz_cfg_hora_"):
        tipo = data[len("cotiz_cfg_hora_"):]
        await query.edit_message_text(
            _hora_picker_text(tipo), parse_mode="Markdown", reply_markup=_hora_picker_kb(tipo)
        )

    elif data.startswith("cotiz_hora_"):
        # cotiz_hora_{tipo}_{HH_MM}  e.g. cotiz_hora_apertura_10_00
        rest  = data[len("cotiz_hora_"):]
        # tipo puede ser: apertura, cierre, res_man, res_med, res_noc
        # la hora tiene formato HH_MM (2 segmentos al final)
        parts = rest.rsplit("_", 2)   # ["apertura", "10", "00"]
        tipo  = parts[0]
        hhmm  = f"{parts[1]}:{parts[2]}"
        cfg   = load_config()
        rt    = cfg.get("resumen_times", ["08:00", "13:00", "20:00"])
        if tipo == "apertura":
            cfg["apertura_time"] = hhmm
        elif tipo == "cierre":
            cfg["cierre_time"] = hhmm
        elif tipo == "res_man":
            rt[0] = hhmm; cfg["resumen_times"] = rt
        elif tipo == "res_med":
            while len(rt) < 2: rt.append("13:00")
            rt[1] = hhmm; cfg["resumen_times"] = rt
        elif tipo == "res_noc":
            while len(rt) < 3: rt.append("20:00")
            rt[2] = hhmm; cfg["resumen_times"] = rt
        save_config(cfg)
        await query.edit_message_text(
            _hora_picker_text(tipo), parse_mode="Markdown", reply_markup=_hora_picker_kb(tipo)
        )

    elif data.startswith("cotiz_cfg_"):
        group = data[len("cotiz_cfg_"):]
        if group in _GROUP_META:
            await query.edit_message_text(
                _threshold_text(group),
                parse_mode="Markdown",
                reply_markup=_threshold_kb(group),
            )

    elif data.startswith("cotiz_th_"):
        # cotiz_th_{group}_{value}  e.g. cotiz_th_dolar_2.0
        parts = data.split("_")
        group = parts[2]
        value = float(parts[3])
        if group in _GROUP_META:
            cfg = load_config()
            th  = cfg.setdefault("thresholds", {})
            _, keys = _GROUP_META[group]
            for k in keys:
                th[k] = value
            save_config(cfg)
            await query.edit_message_text(
                _threshold_text(group),
                parse_mode="Markdown",
                reply_markup=_threshold_kb(group),
            )


# ── Comando /cotiz_horario ───────────────────────────────────────────────────
async def cmd_cotiz_horario(update, context: ContextTypes.DEFAULT_TYPE) -> None:
    args = context.args or []
    if len(args) < 2:
        await update.message.reply_text(
            "Uso:\n"
            "`/cotiz_horario apertura 10:00`\n"
            "`/cotiz_horario cierre 15:00`\n"
            "`/cotiz_horario resumen 08:00 13:00 20:00`",
            parse_mode="Markdown",
        )
        return

    tipo = args[0].lower()
    cfg  = load_config()

    if tipo == "apertura":
        cfg["apertura_time"] = args[1]
        save_config(cfg)
        await update.message.reply_text(f"✅ Apertura → {args[1]}\n_Reiniciá el bot para que tome efecto._", parse_mode="Markdown")
    elif tipo == "cierre":
        cfg["cierre_time"] = args[1]
        save_config(cfg)
        await update.message.reply_text(f"✅ Cierre → {args[1]}\n_Reiniciá el bot para que tome efecto._", parse_mode="Markdown")
    elif tipo == "resumen" and len(args) >= 4:
        cfg["resumen_times"] = [args[1], args[2], args[3]]
        save_config(cfg)
        await update.message.reply_text(
            f"✅ Resúmenes → {args[1]}, {args[2]}, {args[3]}\n_Reiniciá el bot para que tome efecto._",
            parse_mode="Markdown",
        )
    else:
        await update.message.reply_text("❌ Argumentos inválidos. Usá `/cotiz_horario` sin parámetros para ver la ayuda.", parse_mode="Markdown")


# ── Registro de jobs (llamar desde main.py) ──────────────────────────────────
def register_jobs(job_queue, tz_arg) -> None:
    cfg = load_config()

    def _t(hhmm: str) -> dtime:
        h, m = map(int, hhmm.split(":"))
        return dtime(hour=h, minute=m, tzinfo=tz_arg)

    apertura_t = _t(cfg.get("apertura_time", "10:00"))
    cierre_t   = _t(cfg.get("cierre_time",   "15:00"))
    resumen_ts = [_t(t) for t in cfg.get("resumen_times", ["08:00", "13:00", "20:00"])]
    slots      = ["Mañana", "Mediodía", "Noche"]

    # Baselines: fiat + oro a las 10:00 Lun-Vie, MEP/CCL a las 11:00, BTC a las 00:00
    job_queue.run_daily(job_baseline_fiat, time=apertura_t,  days=(0,1,2,3,4), name="cotiz_bl_fiat")
    job_queue.run_daily(job_baseline_mep,  time=_t("11:00"), days=(0,1,2,3,4), name="cotiz_bl_mep")
    job_queue.run_daily(job_baseline_btc,  time=_t("00:00"),                   name="cotiz_bl_btc")

    # Apertura y cierre: Lun-Vie (apertura comparte hora con baseline_fiat — se registra después)
    job_queue.run_daily(job_apertura, time=apertura_t, days=(0,1,2,3,4), name="cotiz_apertura")
    job_queue.run_daily(job_cierre,   time=cierre_t,   days=(0,1,2,3,4), name="cotiz_cierre")

    # Resúmenes diarios (todos los días)
    for t, slot, name in zip(resumen_ts, slots, ["cotiz_res_man", "cotiz_res_med", "cotiz_res_noc"]):
        job_queue.run_daily(job_resumen, time=t, name=name, data={"slot": slot})

    # Poll cada 5 minutos
    job_queue.run_repeating(job_poll, interval=300, first=30, name="cotiz_poll")

    logger.info(f"Cotizaciones jobs registrados — apertura {cfg.get('apertura_time')} / cierre {cfg.get('cierre_time')}")
