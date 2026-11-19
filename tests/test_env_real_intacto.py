# -*- coding: utf-8 -*-
"""Las pruebas nunca escriben /etc/mundoempresarial-bot.env (incidente del 19/11/2026)."""
import os

import pytest


@pytest.fixture
def main(monkeypatch):
    for k in ("TELEGRAM_TOKEN", "WP_USER", "WP_PASS"):
        monkeypatch.setenv(k, "falso")      # main.py los exige al importar; valores de prueba
    import main as m
    return m


def test_las_pruebas_apuntan_a_un_archivo_temporal(main):
    assert main._ENV_FILE == os.environ["ME_BOT_ENV_FILE"] and not main._ENV_FILE.startswith("/etc/")


def test_persistir_admin_reemplaza_solo_su_linea(main, tmp_path, monkeypatch):
    p = tmp_path / "bot.env"
    p.write_text("OTRA=1\nADMIN_CHAT_ID=123456789\nULTIMA=2\n", encoding="utf-8")
    monkeypatch.setattr(main, "_ENV_FILE", str(p))
    monkeypatch.setattr(main, "ADMIN_CHAT_ID", main.ADMIN_CHAT_ID)      # se restaura al terminar
    main._persist_admin_chat_id("7")
    t = p.read_text(encoding="utf-8")
    assert "OTRA=1\n" in t and "ULTIMA=2\n" in t and t.count("ADMIN_CHAT_ID=") == 1 and t.endswith("ADMIN_CHAT_ID=7\n")
