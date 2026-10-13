"""Shared fixtures: realistic sample PDFs generated on the fly (no network,
no binary files in the repo)."""

from __future__ import annotations

import glob
import io
import os

import pymupdf
import pytest

from pdfstudio.security import netguard

# The whole test-suite runs with the network kill switch active, exactly like
# the application does.
netguard.install()

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")


def _find_font(pattern: str) -> str | None:
    for root in ("/usr/share/fonts", "C:/Windows/Fonts"):
        hits = sorted(glob.glob(os.path.join(root, "**", pattern), recursive=True))
        if hits:
            return hits[0]
    return None


SANS_TTF = _find_font("LiberationSans-Regular.ttf") or _find_font("arial.ttf")
SERIF_TTF = _find_font("LiberationSerif-Regular.ttf") or _find_font("times.ttf")
SANS_BOLD_TTF = _find_font("LiberationSans-Bold.ttf") or _find_font("arialbd.ttf")


def make_sample_pdf(path: str | None = None) -> bytes:
    """3 pages:

    0: Helvetica heading + Times body + embedded (subset) TTF paragraph, blue text
    1: two-column text, a filled rectangle and an image
    2: rotated 90 degrees with text
    """
    doc = pymupdf.open()

    p = doc.new_page(width=595, height=842)
    p.insert_text((72, 90), "Kvartalsrapport 2026", fontname="helv", fontsize=24, color=(0.1, 0.1, 0.1))
    p.insert_text((72, 130), "Dette er en broedtekst skrevet med Times.", fontname="tiro", fontsize=12)
    p.insert_text((72, 146), "Anden linje i samme afsnit.", fontname="tiro", fontsize=12)
    if SANS_TTF:
        p.insert_font(fontname="LibSans", fontfile=SANS_TTF)
        p.insert_text((72, 200), "Indlejret skrifttype: Hello World", fontname="LibSans", fontsize=14,
                      color=(0.0, 0.2, 0.8))
    if SANS_BOLD_TTF:
        p.insert_font(fontname="LibSansBold", fontfile=SANS_BOLD_TTF)
        p.insert_text((72, 240), "Fed overskrift", fontname="LibSansBold", fontsize=16)

    p = doc.new_page(width=595, height=842)
    p.insert_textbox(pymupdf.Rect(72, 72, 290, 400), "Venstre kolonne. " * 20, fontname="helv", fontsize=10)
    p.insert_textbox(pymupdf.Rect(305, 72, 523, 400), "Hoejre kolonne. " * 20, fontname="tiro", fontsize=10)
    p.draw_rect(pymupdf.Rect(72, 420, 523, 480), color=(0, 0, 0), fill=(0.9, 0.9, 1.0))
    pix = pymupdf.Pixmap(pymupdf.csRGB, pymupdf.IRect(0, 0, 40, 30), False)
    pix.set_rect(pix.irect, (200, 60, 60))
    p.insert_image(pymupdf.Rect(72, 500, 232, 620), pixmap=pix)

    p = doc.new_page(width=595, height=842)
    p.insert_text((72, 100), "Roteret side", fontname="helv", fontsize=18)
    p.set_rotation(90)

    data = doc.tobytes(garbage=3, deflate=True)
    doc.close()
    if path:
        with open(path, "wb") as fh:
            fh.write(data)
    return data


def make_png(width: int = 320, height: int = 200, rgb=(40, 120, 220)) -> bytes:
    pix = pymupdf.Pixmap(pymupdf.csRGB, pymupdf.IRect(0, 0, width, height), False)
    pix.set_rect(pix.irect, rgb)
    return pix.tobytes("png")


@pytest.fixture
def sample_pdf_path(tmp_path) -> str:
    path = str(tmp_path / "sample.pdf")
    make_sample_pdf(path)
    return path


@pytest.fixture
def sample_pdf(sample_pdf_path):
    from pdfstudio.core import PdfDocument

    pdf = PdfDocument.open(sample_pdf_path)
    yield pdf
    pdf.close()


@pytest.fixture
def png_path(tmp_path) -> str:
    path = str(tmp_path / "screenshot.png")
    with open(path, "wb") as fh:
        fh.write(make_png())
    return path


@pytest.fixture(scope="session")
def qapp():
    from PySide6.QtWidgets import QApplication

    app = QApplication.instance() or QApplication([])
    yield app
    # Tear Qt down deterministically while Python is still alive; leaving
    # widgets/scene items to interpreter finalisation crashes in C++.
    import gc

    from PySide6.QtCore import QCoreApplication, QEvent

    for w in app.topLevelWidgets():
        w.close()
        w.deleteLater()
    QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete.value)
    app.processEvents()
    gc.collect()
    QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete.value)


__all__ = ["make_sample_pdf", "make_png", "SANS_TTF", "SERIF_TTF", "io"]
