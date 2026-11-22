# -*- coding: utf-8 -*-
"""WebP propio (22/11/2026): cuando el sitio sirva «imagen.jpg.webp» en la misma URL a quien mande «image/webp» en el
Accept, el bot NO tiene que pedirlo al bajar imágenes de mundoempresarial.ar (la marca de agua recodificaría el WebP a JPEG
con pérdida y upload_image subiría nuestras fotos de nuevo como «.webp»). Las imágenes de otros sitios, como siempre.
Sin red: requests es de mentira."""
import os
import sys

import pytest

RAIZ_BOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, RAIZ_BOT)


@pytest.fixture
def main(monkeypatch):
    for k in ("TELEGRAM_TOKEN", "WP_USER", "WP_PASS"):
        monkeypatch.setenv(k, "falso")
    import main as m
    return m


@pytest.mark.parametrize("url", ["https://mundoempresarial.ar/wp-content/uploads/2026/11/nota-1.jpg",
                                 "https://www.mundoempresarial.ar/wp-content/uploads/2026/11/nota-1-300x200.jpg",
                                 "http://MundoEmpresarial.ar/wp-content/uploads/2026/07/ficha.png",
                                 "https://mundoempresarial.com.ar/wp-content/uploads/2026/11/nota-1.jpg",
                                 "https://www.mundoempresarial.com.ar/wp-content/uploads/2026/11/nota-1.jpg"])
def test_a_nuestro_sitio_no_le_pide_webp(main, url):
    h = main.headers_imagen(url)
    assert "image/webp" not in h["Accept"] and "image/avif" not in h["Accept"]
    assert h["Accept"].startswith("image/jpeg") and h["User-Agent"] == main.HEADERS_BROWSER["User-Agent"]
    assert "image/webp" in main.HEADERS_BROWSER["Accept"]          # el diccionario compartido no se toca


@pytest.mark.parametrize("url", ["https://www.infobae.com/resizer/foto.jpg", "https://mundoempresarial.ar.evil.example/x.jpg",
                                 "https://mundoempresarial.com.ar.evil.example/x.jpg", "https://falsomundoempresarial.com.ar/x.jpg",
                                 "", "no es una url"])
def test_a_otros_sitios_los_headers_de_siempre(main, url):
    assert main.headers_imagen(url) is main.HEADERS_BROWSER


class _Resp:
    def __init__(self, ok=True, contenido=b"\xff\xd8\xff\xe0jpeg", ctype="image/jpeg", js=None):
        self.ok, self.status_code, self.content, self.text = ok, 200 if ok else 500, contenido, ""
        self.headers = {"Content-Type": ctype}
        self._js = js or {}

    def raise_for_status(self):
        pass

    def json(self):
        return self._js


def test_upload_image_de_nuestro_sitio_baja_sin_webp(main, monkeypatch):
    pedidos = []

    def get(url, headers=None, **k):
        pedidos.append((url, headers["Accept"]))
        return _Resp()
    monkeypatch.setattr(main.requests, "get", get)
    monkeypatch.setattr(main.requests, "post", lambda *a, **k: _Resp(js={"id": 77}))
    assert main.upload_image("https://mundoempresarial.ar/wp-content/uploads/2026/11/nota-1.jpg") == 77
    assert pedidos and "image/webp" not in pedidos[0][1]
    pedidos.clear()
    main.upload_image("https://www.otro-diario.example/foto.jpg")
    assert "image/webp" in pedidos[0][1]                              # de otro sitio: sin cambios


def test_twitter_y_marca_de_agua_tambien_bajan_sin_webp(main, monkeypatch):
    pedidos = []

    def get(url, headers=None, **k):
        pedidos.append((url, (headers or {}).get("Accept", "")))
        if "/wp-json/wp/v2/media/" in url:
            return _Resp(js={"source_url": "https://mundoempresarial.ar/wp-content/uploads/2026/11/nota-1.jpg"})
        return _Resp(ok=False)                                         # corta ahí: solo importa el pedido
    monkeypatch.setattr(main.requests, "get", get)
    monkeypatch.setattr(main.requests, "post", lambda *a, **k: _Resp(ok=False))
    main.upload_twitter_media("https://mundoempresarial.ar/wp-content/uploads/2026/11/nota-1.jpg", auth=None)
    main._apply_wm_to_featured({"featured_media": 5, "id": 9, "title": "t"})
    imagenes = [a for u, a in pedidos if "/uploads/" in u]
    assert len(imagenes) == 2 and all("image/webp" not in a for a in imagenes)


def test_ninguna_descarga_de_imagen_usa_el_accept_del_navegador():
    """Las tres descargas de imágenes del bot pasan por headers_imagen (si aparece una nueva con HEADERS_BROWSER, falla)."""
    import re
    src = open(os.path.join(RAIZ_BOT, "main.py"), encoding="utf-8").read()
    for fn in ("upload_image", "upload_twitter_media", "_apply_wm_to_featured"):
        cuerpo = re.search(r"\ndef %s\(.*?(?=\n(?:async )?def )" % fn, src, re.S).group(0)
        assert "headers_imagen(" in cuerpo and "headers=HEADERS_BROWSER" not in cuerpo, fn
