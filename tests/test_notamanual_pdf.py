# -*- coding: utf-8 -*-
"""/notamanual con PDF (19/11/2026, pedido de Leo: «subí un pdf y no lo pudo leer»):
  · un PDF escaneado (sin texto seleccionable) se lee con OCR (pdftoppm + tesseract) y se avisa que fue por OCR;
  · un archivo que llega sin /notamanual ya no se ignora en silencio: en el chat privado se ofrece usarlo como nota manual.
Sin red, sin Telegram de verdad, sin tesseract instalado (todo de mentira)."""
import asyncio
import os
import sys
import types

import pytest

RAIZ_BOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, RAIZ_BOT)

LARGO = "La columna de autor tiene párrafos completos con texto suficiente para una nota. " * 10


@pytest.fixture
def main(monkeypatch):
    for k in ("TELEGRAM_TOKEN", "WP_USER", "WP_PASS"):
        monkeypatch.setenv(k, "falso")
    import main as m
    return m


def _pypdf_falso(monkeypatch, texto_por_pagina):
    class Pag:
        def __init__(self, t):
            self.t, self.images = t, []

        def extract_text(self):
            return self.t

    class Reader:
        def __init__(self, path):
            self.pages = [Pag(t) for t in texto_por_pagina]
    monkeypatch.setitem(sys.modules, "pypdf", types.SimpleNamespace(PdfReader=Reader))


# ── OCR ─────────────────────────────────────────────────────────────────────────────────────────────────────

def test_pdf_con_texto_no_usa_ocr(main, monkeypatch):
    _pypdf_falso(monkeypatch, [LARGO])
    monkeypatch.setattr(main, "_pdf_ocr", lambda p: (_ for _ in ()).throw(AssertionError("no hace falta OCR")))
    texto, nimg, via = main._pdf_to_text("x.pdf")
    assert via == "texto" and texto.startswith("La columna")


def test_pdf_escaneado_se_lee_con_ocr(main, monkeypatch):
    _pypdf_falso(monkeypatch, ["", ""])                       # escaneo: pypdf no saca nada
    monkeypatch.setattr(main, "_pdf_ocr", lambda p: LARGO)
    texto, nimg, via = main._pdf_to_text("x.pdf")
    assert via == "ocr" and len(texto) >= main._PDF_MIN_TEXTO


def test_sin_herramientas_de_ocr_devuelve_vacio(main, monkeypatch):
    import shutil
    monkeypatch.setattr(shutil, "which", lambda b: None)
    assert main._pdf_ocr("x.pdf") == ""


def test_ocr_une_las_paginas_en_orden(main, monkeypatch, tmp_path):
    import shutil
    import subprocess
    monkeypatch.setattr(shutil, "which", lambda b: "/usr/bin/" + b)
    llamadas = []

    def run(cmd, **kw):
        llamadas.append(cmd[0])
        if cmd[0] == "pdftoppm":
            base = cmd[-1]
            for n in ("2", "1"):                                  # pdftoppm escribe p-1.png, p-2.png
                open(base + "-" + n + ".png", "w").close()
            return types.SimpleNamespace(stdout="", returncode=0)
        assert cmd[0] == "tesseract" and cmd[-1] == "spa"
        return types.SimpleNamespace(stdout="página " + cmd[1][-5], returncode=0)
    monkeypatch.setattr(subprocess, "run", run)
    assert main._pdf_ocr(str(tmp_path / "x.pdf")) == "página 1\n\npágina 2"
    assert llamadas == ["pdftoppm", "tesseract", "tesseract"]


# ── archivo sin /notamanual ─────────────────────────────────────────────────────────────────────────────────

class Msg:
    def __init__(self, tipo_chat="private", doc=None):
        self.chat = types.SimpleNamespace(type=tipo_chat, id=7)
        self.document = doc
        self.message_id = 1
        self.enviados = []

    async def reply_text(self, texto, **kw):
        self.enviados.append((texto, kw))
        return self


def _doc(nombre="columna.pdf", mime="application/pdf"):
    return types.SimpleNamespace(file_name=nombre, mime_type=mime, file_unique_id="u1")


def _upd(m):
    return types.SimpleNamespace(message=m, effective_chat=m.chat, effective_user=types.SimpleNamespace(id=7))


def test_un_pdf_sin_notamanual_ofrece_usarlo(main):
    m = Msg(doc=_doc())
    ctx = types.SimpleNamespace(user_data={})
    asyncio.run(main.handle_document(_upd(m), ctx))
    texto, kw = m.enviados[0]
    assert "¿Lo uso como nota manual" in texto and "columna.pdf" in texto
    assert "nm_usardoc" in str(kw["reply_markup"]) and ctx.user_data["nm_doc_pendiente"] is m.document


def test_en_un_grupo_no_contesta(main):
    m = Msg(tipo_chat="supergroup", doc=_doc())
    ctx = types.SimpleNamespace(user_data={})
    asyncio.run(main.handle_document(_upd(m), ctx))
    assert m.enviados == [] and "nm_doc_pendiente" not in ctx.user_data


def test_un_archivo_que_no_es_columna_explica(main):
    m = Msg(doc=_doc("foto.zip", "application/zip"))
    asyncio.run(main.handle_document(_upd(m), types.SimpleNamespace(user_data={})))
    assert "solo sé usar PDF o .txt" in m.enviados[0][0]


def test_el_boton_sin_archivo_pendiente_avisa(main):
    editados = []

    async def edit(texto, **kw):
        editados.append(texto)

    async def answer(*a, **k):
        return None
    q = types.SimpleNamespace(data="nm_usardoc", answer=answer, edit_message_text=edit,
                              message=Msg(), from_user=types.SimpleNamespace(id=7))
    upd = types.SimpleNamespace(callback_query=q, message=None, effective_user=q.from_user)
    asyncio.run(main.handle_notamanual_button(upd, types.SimpleNamespace(user_data={})))
    assert "Se perdió el archivo" in editados[0]


def test_el_boton_procesa_el_archivo_pendiente(main, monkeypatch):
    procesados = []

    async def fake_doc(update, context, doc):
        procesados.append((update.message, doc, context.user_data.get("awaiting_nota_manual")))
    monkeypatch.setattr(main, "_nm_documento", fake_doc)

    async def edit(texto, **kw):
        return None

    async def answer(*a, **k):
        return None
    msg_bot = Msg()
    q = types.SimpleNamespace(data="nm_usardoc", answer=answer, edit_message_text=edit, message=msg_bot,
                              from_user=types.SimpleNamespace(id=7))
    d = _doc()
    ctx = types.SimpleNamespace(user_data={"nm_doc_pendiente": d})
    asyncio.run(main.handle_notamanual_button(types.SimpleNamespace(callback_query=q, message=None, effective_user=q.from_user), ctx))
    assert procesados == [(msg_bot, d, True)] and "nm_doc_pendiente" not in ctx.user_data
