# -*- coding: utf-8 -*-
"""log_filtro: tapa tokens/claves en mensaje, argumentos y traceback; un registro normal sale igual."""
import io
import logging
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import log_filtro  # noqa: E402

TOK = "bot" + "123456789" + ":" + "AAH" + "x" * 32          # forma de token, inventado
URL = f"https://api.telegram.org/{TOK}/sendMessage"


def _logger(nombre):
    buf = io.StringIO()
    h = logging.StreamHandler(buf)
    h.setFormatter(logging.Formatter("%(levelname)s %(message)s"))
    h.addFilter(log_filtro.FiltroSecretos())
    lg = logging.getLogger(nombre)
    lg.handlers[:] = [h]
    lg.propagate = False
    lg.setLevel(logging.DEBUG)
    return lg, buf


def test_excepcion_de_requests_con_url_sale_tapada():
    import requests
    lg, buf = _logger("t_exc")
    try:
        raise requests.exceptions.ConnectionError(f"HTTPSConnectionPool: Max retries exceeded with url: {URL}")
    except Exception as e:
        lg.error("falló %s", e, exc_info=True)
    out = buf.getvalue()
    assert "AAH" not in out and "123456789:" not in out
    assert "bot<TOKEN>" in out and "Traceback" in out


def test_mensaje_y_args_tapados():
    lg, buf = _logger("t_args")
    lg.warning("url %s y %s", URL, {"k": "sk-" + "a" * 30})
    lg.warning("directo " + URL)
    out = buf.getvalue()
    assert "AAH" not in out and "sk-aaaa" not in out and "<CLAVE>" in out


def test_otras_claves():
    assert log_filtro.tapar("x AIza" + "B" * 35) == "x <CLAVE>"
    assert log_filtro.tapar("xai-" + "c" * 25) == "<CLAVE>"
    assert log_filtro.tapar("github_pat_" + "d" * 30) == "<CLAVE>"


def test_registro_normal_sale_igual():
    lg, buf = _logger("t_norm")
    lg.info("nota %d publicada en %s, task-force-de-la-tarea ok", 5, "wp")
    assert buf.getvalue() == "INFO nota 5 publicada en wp, task-force-de-la-tarea ok\n"


def test_no_rompe_con_args_malos():
    lg, buf = _logger("t_mal")
    lg.info("sin placeholder", 1, 2)      # logging lo reporta por su cuenta; el filtro no debe lanzar
    assert log_filtro.FiltroSecretos().filter(logging.LogRecord("x", 20, "f", 1, "%d", ("a",), None)) is True


def test_instalar_cubre_loggers_hijos():
    buf = io.StringIO()
    h = logging.StreamHandler(buf)
    raiz = logging.getLogger()
    antes = raiz.handlers[:]
    raiz.addHandler(h)
    try:
        log_filtro.instalar()
        logging.getLogger("hijo.x").error("boom " + URL)
        assert "AAH" not in buf.getvalue() and "bot<TOKEN>" in buf.getvalue()
    finally:
        raiz.handlers[:] = antes
