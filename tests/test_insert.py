import pymupdf
import pytest

from pdfstudio.core import CONTENT, PdfDocument
from pdfstudio.core.fonts import SpanStyle
from pdfstudio.core.insert import add_image, add_text_box, document_text_style
from pdfstudio.core.text_edit import text_blocks
from tests.conftest import filled_pixmap, make_png, make_sample_pdf


@pytest.fixture
def pdf():
    p = PdfDocument.from_bytes(make_sample_pdf())
    yield p
    p.close()


def two_tone_png(w=60, h=40) -> bytes:
    """Top half red, bottom half blue."""
    pix = filled_pixmap(w, h, (255, 0, 0), regions=[((0, h // 2, w, h), (0, 0, 255))])
    return pix.tobytes("png")


def render(page, zoom=1):
    return page.get_pixmap(matrix=pymupdf.Matrix(zoom, zoom), alpha=False)


# -- document style ----------------------------------------------------------
def test_document_text_style_sample(pdf):
    st = document_text_style(pdf.doc)
    # page 2's 10 pt Helvetica column is the most common body text
    assert st.font == "Helvetica"
    assert st.size == pytest.approx(10)
    assert st.color == (0, 0, 0)
    assert not st.bold


def test_document_text_style_ignores_headings():
    doc = pymupdf.open()
    p = doc.new_page()
    p.insert_text((72, 80), "STOR OVERSKRIFT MED MANGE TEGN " * 3, fontname="hebo", fontsize=30)
    p.insert_textbox(pymupdf.Rect(72, 120, 520, 700), "Brødtekst i Times. " * 60, fontname="tiro",
                     fontsize=11, color=(0.2, 0.2, 0.2))
    st = document_text_style(doc)
    assert st.font == "Times-Roman" and st.serif
    assert st.size == pytest.approx(11)
    assert st.color == pytest.approx((0.2, 0.2, 0.2), abs=0.01)


def test_document_text_style_empty_document():
    doc = pymupdf.open()
    doc.new_page()
    st = document_text_style(doc)
    assert st.font == "Helvetica" and st.size == 11


# -- text boxes -------------------------------------------------------------------
def test_add_text_box_wraps_and_styles(pdf):
    style = SpanStyle(font="Times-Roman", size=12, color=(0.0, 0.4, 0.0), serif=True)
    rect = pymupdf.Rect(300, 600, 500, 700)
    events = []
    pdf.listen(events.append)
    text = "En ny tekstboks med dansk æøå ÆØÅ som skal ombrydes inden for rammen."
    add_text_box(pdf, 0, rect, text, style)
    assert events[-1].kind == CONTENT and events[-1].pages == (0,)
    blk = next(b for b in text_blocks(pdf.doc, 0) if "tekstboks" in b.text)
    assert " ".join(blk.text.split("\n")) == text
    assert len(blk.lines) >= 2
    for ln in blk.lines:
        assert rect.x0 - 0.5 <= ln.bbox.x0 and ln.bbox.x1 <= rect.x1 + 3
        assert ln.style.serif
        assert ln.style.size == pytest.approx(12)
        assert ln.style.color == pytest.approx((0, 0.4, 0), abs=0.01)
    assert blk.bbox.y0 >= rect.y0 - 1
    assert pdf.undo()
    assert "tekstboks" not in pdf.doc[0].get_text()


@pytest.mark.parametrize("align", ["left", "center", "right"])
def test_add_text_box_alignment(pdf, align):
    rect = pymupdf.Rect(100, 650, 400, 700)
    add_text_box(pdf, 0, rect, "Kort linje", SpanStyle(font="Helvetica", size=14), align=align)
    (ln,) = [ln for b in text_blocks(pdf.doc, 0) for ln in b.lines if ln.text == "Kort linje"]
    if align == "left":
        assert ln.bbox.x0 == pytest.approx(rect.x0, abs=0.5)
    elif align == "right":
        assert ln.bbox.x1 == pytest.approx(rect.x1, abs=0.5)
    else:
        assert (ln.bbox.x0 + ln.bbox.x1) / 2 == pytest.approx((rect.x0 + rect.x1) / 2, abs=0.5)


def test_add_text_box_rejects_bad_alignment(pdf):
    with pytest.raises(ValueError):
        add_text_box(pdf, 0, pymupdf.Rect(0, 0, 100, 100), "x", SpanStyle(font="Helvetica", size=10),
                     align="justify")


def test_add_text_box_blank_text_is_noop(pdf):
    add_text_box(pdf, 0, pymupdf.Rect(0, 0, 100, 100), "  \n ", SpanStyle(font="Helvetica", size=10))
    assert not pdf.history.can_undo


def test_add_text_box_on_rotated_page_is_upright(pdf):
    rect = pymupdf.Rect(100, 300, 400, 360)
    add_text_box(pdf, 2, rect, "Opret tekst", SpanStyle(font="Helvetica", size=20))
    blk = next(b for b in text_blocks(pdf.doc, 2) if "Opret" in b.text)
    assert rect.x0 - 1 <= blk.bbox.x0 and blk.bbox.x1 <= rect.x1 + 1
    assert rect.y0 - 1 <= blk.bbox.y0 and blk.bbox.y1 <= rect.y1 + 1
    # upright in display space: wide and short
    assert blk.bbox.width > 3 * blk.bbox.height
    # ink is where the text box is
    pm = render(pdf.doc[2])
    dark = [(x, y) for y in range(300, 330, 2) for x in range(100, 230, 2) if min(pm.pixel(x, y)) < 128]
    assert dark


def _font_report(doc, page) -> list:
    """Fonts of the page and their ToUnicode lines for U+00A0 (diagnostics)."""
    out = []
    for f in doc[page].get_fonts(full=True):
        kind, val = doc.xref_get_key(f[0], "ToUnicode")
        lines = []
        if kind == "xref":
            cmap = doc.xref_stream(int(val.split()[0])).decode("latin-1")
            lines = [ln for ln in cmap.splitlines() if "00a0" in ln.lower()][:3]
        out.append((f[0], f[3], f[2], lines))
    return out


def test_add_text_box_non_latin_falls_back(pdf):
    add_text_box(pdf, 0, pymupdf.Rect(72, 700, 500, 760), "Привет – 中文", SpanStyle(font="Helvetica", size=12))
    text = pdf.doc[0].get_text()
    spans = [(s["font"], s["text"]) for b in pdf.doc[0].get_text("dict")["blocks"] for ln in b.get("lines", ())
             for s in ln["spans"] if "Привет" in s["text"] or "中" in s["text"] or s["text"].strip() == "–"]
    assert "Привет – 中文" in text, (spans, _font_report(pdf.doc, 0))


# -- images ---------------------------------------------------------------------------
def test_add_image_from_path_and_bytes(pdf, png_path):
    add_image(pdf, 0, pymupdf.Rect(300, 300, 460, 400), png_path)
    add_image(pdf, 0, pymupdf.Rect(300, 420, 460, 520), make_png(rgb=(10, 200, 10)))
    infos = pdf.doc[0].get_image_info()
    assert len(infos) == 2
    pm = render(pdf.doc[0])
    assert pm.pixel(380, 350) == (40, 120, 220)
    assert pm.pixel(380, 470) == (10, 200, 10)
    assert pdf.undo()
    assert len(pdf.doc[0].get_image_info()) == 1


def test_add_image_keep_aspect(pdf):
    rect = pymupdf.Rect(100, 300, 500, 400)  # 4:1 box, 8:5 image
    add_image(pdf, 0, rect, make_png(320, 200), keep_aspect=True)
    (info,) = pdf.doc[0].get_image_info()
    bbox = pymupdf.Rect(info["bbox"])
    assert bbox.width / bbox.height == pytest.approx(1.6, abs=0.02)
    assert bbox.height == pytest.approx(100, abs=0.5)
    assert rect.contains(bbox)


def test_add_image_stretch(pdf):
    rect = pymupdf.Rect(100, 300, 500, 400)
    add_image(pdf, 0, rect, make_png(320, 200), keep_aspect=False)
    (info,) = pdf.doc[0].get_image_info()
    assert tuple(info["bbox"]) == pytest.approx(tuple(rect), abs=0.5)


@pytest.mark.parametrize("rotation", [0, 90, 180, 270])
def test_add_image_upright_on_rotated_pages(rotation):
    doc = pymupdf.open()
    doc.new_page(width=595, height=842)
    doc[0].set_rotation(rotation)
    pdf = PdfDocument.from_bytes(doc.tobytes())
    rect = pymupdf.Rect(100, 100, 220, 180)  # display coordinates
    add_image(pdf, 0, rect, two_tone_png(), keep_aspect=True)
    pm = render(pdf.doc[0])
    assert pm.pixel(160, 115) == (255, 0, 0)  # top half red
    assert pm.pixel(160, 165) == (0, 0, 255)  # bottom half blue
    assert pm.pixel(160, 195) == (255, 255, 255)  # nothing outside


def test_add_image_rejects_empty_rect(pdf):
    with pytest.raises(ValueError):
        add_image(pdf, 0, pymupdf.Rect(10, 10, 10, 50), make_png())
