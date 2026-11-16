# -*- coding: utf-8 -*-
"""Fuentes que el operador agrega/borra con /fuentes, en un archivo que la ingesta del harness lee.

Antes /fuentes guardaba solo en el feedback store de WordPress y la ingesta leia unicamente
sources.json del disco: las fuentes agregadas por el bot nunca se ingestaban. sources.json esta
en git (el bot no puede escribirlo sin romper el `git pull --ff-only` del despliegue), asi que
se usa un archivo aparte, fuera de git: {dominio: {...datos...}} o {dominio: {"_deleted": true}}.
agents/ingesta.py del harness (_FUENTES_EXTRA_PATH) lee el mismo default.
"""
import json
import logging
import os

logger = logging.getLogger(__name__)

_DEFAULT_VPS = "/opt/me-harness/fuentes_extra.json"


def ruta() -> str:
    p = os.environ.get("FUENTES_EXTRA_PATH")
    if p:
        return p
    if os.path.isdir(os.path.dirname(_DEFAULT_VPS)):
        return _DEFAULT_VPS
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), "fuentes_extra.json")  # dev


def leer() -> dict:
    try:
        with open(ruta(), "r", encoding="utf-8") as f:
            d = json.load(f)
        return d if isinstance(d, dict) else {}
    except FileNotFoundError:
        return {}
    except Exception as e:
        logger.warning(f"fuentes_extra ilegible: {e}")
        return {}


def _guardar(d: dict) -> None:
    p = ruta()
    tmp = p + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(d, f, ensure_ascii=False, indent=1, sort_keys=True)
    os.replace(tmp, p)   # atomico: la ingesta nunca lee un archivo a medio escribir


def guardar_fuente(domain: str, data: dict) -> bool:
    """Agrega/actualiza una fuente (o la marca borrada con {"_deleted": True}). No levanta excepciones."""
    try:
        d = leer()
        d[domain] = data
        _guardar(d)
        return True
    except Exception as e:
        logger.error(f"fuentes_extra no se pudo escribir: {e}")
        return False


def sincronizar(overlay: dict) -> bool:
    """Vuelca el overlay de WP al archivo (migra lo ya cargado con /fuentes). Solo escribe si cambia."""
    try:
        if not overlay:
            return False
        d = leer()
        nuevo = dict(d)
        for dom, datos in overlay.items():
            if isinstance(datos, dict):
                nuevo[dom] = datos
        if nuevo == d:
            return False
        _guardar(nuevo)
        return True
    except Exception as e:
        logger.error(f"fuentes_extra sincronizar: {e}")
        return False
