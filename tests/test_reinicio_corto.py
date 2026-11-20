# -*- coding: utf-8 -*-
"""Lo que llega durante un reinicio (20/11/2026): con un reinicio corto el bot procesa lo pendiente; con una caída larga o sin
marca lo descarta como antes."""
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


@pytest.fixture
def M(monkeypatch):
    for k in ("TELEGRAM_TOKEN", "WP_USER", "WP_PASS"):
        monkeypatch.setenv(k, "falso")
    import main as m
    return m


def test_reinicio_corto_procesa_lo_pendiente(M, tmp_path, monkeypatch):
    marca = tmp_path / "vivo"
    monkeypatch.setattr(M, "_ULTIMO_VIVO", str(marca))
    M._marcar_vivo()
    t = int(marca.read_text())
    assert M._soltar_pendientes(ahora=t + 20) is False                     # despliegue: 20 s caído → procesa
    assert M._soltar_pendientes(ahora=t + M.REINICIO_CORTO_S + 1) is True  # caída larga → descarta


def test_sin_marca_descarta_como_antes(M, tmp_path, monkeypatch):
    monkeypatch.setattr(M, "_ULTIMO_VIVO", str(tmp_path / "no-existe"))
    assert M._soltar_pendientes() is True


def test_lo_que_recibe_ptb_es_una_corutina(M):
    """PTB espera corutinas en la JobQueue y en post_shutdown: una función común daba TypeError cada 30 s (20/11)."""
    import inspect
    assert inspect.iscoroutinefunction(M._job_vivo)
    src = open(M.__file__, encoding="utf-8").read()
    assert "run_repeating(_job_vivo" in src and ".post_shutdown(_job_vivo)" in src
