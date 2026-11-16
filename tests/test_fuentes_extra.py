# -*- coding: utf-8 -*-
"""/fuentes escribe en el archivo que lee la ingesta del harness (sin red, sin token real)."""
import json
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import fuentes_extra as fx  # noqa: E402


@pytest.fixture
def archivo(tmp_path, monkeypatch):
    p = tmp_path / "fuentes_extra.json"
    monkeypatch.setenv("FUENTES_EXTRA_PATH", str(p))
    return p


def test_ruta_respeta_la_variable_de_entorno(archivo):
    assert fx.ruta() == str(archivo)


def test_guardar_agrega_y_actualiza_sin_duplicar(archivo):
    assert fx.guardar_fuente("a.com", {"name": "A", "rss_url": "https://a.com/rss"})
    assert fx.guardar_fuente("b.com", {"name": "B", "rss_url": "https://b.com/rss"})
    assert fx.guardar_fuente("a.com", {"name": "A2", "rss_url": "https://a.com/rss"})
    d = json.loads(archivo.read_text(encoding="utf-8"))
    assert sorted(d) == ["a.com", "b.com"] and d["a.com"]["name"] == "A2"
    assert not os.path.exists(str(archivo) + ".tmp")


def test_borrar_deja_marca(archivo):
    fx.guardar_fuente("a.com", {"rss_url": "https://a.com/rss"})
    fx.guardar_fuente("a.com", {"_deleted": True})
    assert fx.leer()["a.com"] == {"_deleted": True}


def test_sincronizar_migra_el_overlay_y_solo_escribe_si_cambia(archivo):
    overlay = {"a.com": {"rss_url": "https://a.com/rss"}, "b.com": {"_deleted": True}}
    assert fx.sincronizar(overlay) is True
    assert fx.leer() == overlay
    assert fx.sincronizar(overlay) is False
    assert fx.sincronizar({}) is False      # overlay vacío (WP caído) nunca pisa el archivo


def test_archivo_roto_no_levanta(archivo):
    archivo.write_text("{no es json", encoding="utf-8")
    assert fx.leer() == {}
    assert fx.guardar_fuente("a.com", {"rss_url": "x"})   # lo reescribe sano
    assert "a.com" in fx.leer()


def test_add_y_delete_source_del_bot_escriben_donde_lee_la_ingesta(archivo, monkeypatch):
    for k in ("TELEGRAM_TOKEN", "WP_USER", "WP_PASS"):
        monkeypatch.setenv(k, "falso")      # main.py los exige al importar; valores de prueba
    import main
    fb = {}
    monkeypatch.setattr(main, "_load_feedback", lambda: fb)
    monkeypatch.setattr(main, "_save_feedback", lambda d: True)
    assert main._add_source("nueva.com", {"name": "N", "rss_url": "https://nueva.com/feed"})
    assert json.loads(archivo.read_text(encoding="utf-8"))["nueva.com"]["rss_url"] == "https://nueva.com/feed"
    assert main._delete_source("nueva.com")
    assert json.loads(archivo.read_text(encoding="utf-8"))["nueva.com"] == {"_deleted": True}
