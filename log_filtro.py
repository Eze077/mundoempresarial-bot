# -*- coding: utf-8 -*-
"""Filtro de `logging` que tapa secretos (token de bot de Telegram, claves sk-/AIza/xai-/github_pat_)
en el mensaje, los argumentos y el traceback de cada registro. Las excepciones de `requests` traen la
URL entera (https://api.telegram.org/bot<TOKEN>/...) y terminaban en el journal.
Uso: `import log_filtro; log_filtro.instalar()` justo después de logging.basicConfig().
Nunca rompe un registro: ante cualquier error interno el registro pasa tal cual."""
import logging
import re

_RE = re.compile(
    r"bot\d{6,}:[A-Za-z0-9_-]{20,}"
    r"|(?<![A-Za-z0-9])sk-[A-Za-z0-9_-]{20,}"
    r"|(?<![A-Za-z0-9])AIza[0-9A-Za-z_-]{30,}"
    r"|(?<![A-Za-z0-9])xai-[A-Za-z0-9]{20,}"
    r"|(?<![A-Za-z0-9])github_pat_[A-Za-z0-9_]{20,}"
)


def _sub(m):
    return "bot<TOKEN>" if m.group(0).startswith("bot") else "<CLAVE>"


def tapar(texto: str) -> str:
    return _RE.sub(_sub, texto)


class FiltroSecretos(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        try:
            try:
                msg = record.getMessage()
            except Exception:
                msg = None
            if msg is not None:
                nuevo = tapar(msg)
                if nuevo != msg:
                    record.msg, record.args = nuevo, None
            if record.exc_info and record.exc_info[0] is not None:
                if not record.exc_text:
                    record.exc_text = logging.Formatter().formatException(record.exc_info)
                record.exc_text = tapar(record.exc_text)
            if record.exc_text:
                record.exc_text = tapar(record.exc_text)
            if record.stack_info:
                record.stack_info = tapar(record.stack_info)
        except Exception:
            pass
        return True


_F = FiltroSecretos()


def instalar() -> None:
    """Filtro en el logger raíz y en cada handler del raíz (los filtros del logger raíz no
    alcanzan a los registros de los loggers hijos; los de los handlers sí)."""
    try:
        raiz = logging.getLogger()
        if _F not in raiz.filters:
            raiz.addFilter(_F)
        for h in raiz.handlers:
            if _F not in h.filters:
                h.addFilter(_F)
    except Exception:
        pass
