import types

import pymupdf
import pytest

from pdfstudio.core import PdfDocument
from pdfstudio.core.pages import rotate_pages
from pdfstudio.core.render import render_page
from pdfstudio.core.search import SearchHit, search, search_page


def make_doc(*pages: str, rotation: int = 0) -> PdfDocument:
    doc = pymupdf.open()
    for text in pages:
        page = doc.new_page(width=400, height=300)
        page.insert_textbox(pymupdf.Rect(40, 40, 360, 260), text, fontname="helv", fontsize=12)
        if rotation:
            page.set_rotation(rotation)
    data = doc.tobytes()
    doc.close()
    return PdfDocument.from_bytes(data)


def dark_pixels_in(pix: pymupdf.Pixmap, rect: pymupdf.Rect, zoom: float) -> int:
    count = 0
    for y in range(int(rect.y0 * zoom), int(rect.y1 * zoom)):
        for x in range(int(rect.x0 * zoom), int(rect.x1 * zoom)):
            if 0 <= x < pix.width and 0 <= y < pix.height and pix.pixel(x, y)[0] < 128:
                count += 1
    return count


def test_basic_search_returns_display_rects_and_context(sample_pdf):
    hits = list(search(sample_pdf.doc, "hello world"))
    assert len(hits) == 1
    hit = hits[0]
    assert isinstance(hit, SearchHit)
    assert hit.page == 0
    assert len(hit.rects) == 1
    expected = sample_pdf.page(0).search_for("Hello World")[0]
    assert all(abs(a - b) < 1.5 for a, b in zip(hit.rects[0], expected))
    assert "Hello World" in hit.context


def test_accepts_pdfdocument_and_is_lazy(sample_pdf):
    it = search(sample_pdf, "kolonne")
    assert isinstance(it, types.GeneratorType)
    first = next(it)
    assert first.page == 1


def test_case_sensitivity():
    pdf = make_doc("Kat kat KAT katte")
    assert len(list(search(pdf, "kat"))) == 4
    sensitive = list(search(pdf, "kat", case_sensitive=True))
    assert len(sensitive) == 2  # "kat" and "katte"
    assert len(list(search(pdf, "KAT", case_sensitive=True))) == 1
    assert len(list(search(pdf, "Kat", case_sensitive=True, whole_words=True))) == 1


def test_whole_words():
    pdf = make_doc("kat katte skat kat. (kat) kat_x")
    assert len(list(search(pdf, "kat"))) == 6
    hits = list(search(pdf, "kat", whole_words=True))
    assert len(hits) == 3  # "kat", "kat.", "(kat)"
    # a query starting/ending with punctuation needs no word boundary there
    assert len(list(search(pdf, "kat.", whole_words=True))) == 1


def test_danish_letters_case_insensitive():
    pdf = make_doc("Ærø og Østersøen ÅEN")
    assert len(list(search(pdf, "ærø"))) == 1
    assert len(list(search(pdf, "østersøen"))) == 1
    assert len(list(search(pdf, "åen"))) == 1
    assert len(list(search(pdf, "åen", case_sensitive=True))) == 0


def test_phrase_across_line_break_has_rect_per_line(sample_pdf):
    hits = list(search(sample_pdf.doc, "Times. Anden linje"))
    assert len(hits) == 1
    rects = hits[0].rects
    assert len(rects) == 2
    assert rects[0].y1 <= rects[1].y0 + 1  # first line above the second
    assert hits[0].context.startswith("Dette er en")


def test_whitespace_in_query_is_flexible():
    pdf = make_doc("to   mellemrum")
    assert len(list(search(pdf, "to mellemrum"))) == 1
    assert len(list(search(pdf, "  to\tmellemrum "))) == 1


def test_empty_query_and_no_match(sample_pdf):
    assert list(search(sample_pdf.doc, "")) == []
    assert list(search(sample_pdf.doc, "   ")) == []
    assert list(search(sample_pdf.doc, "findes slet ikke")) == []


def test_many_hits_on_page_and_order(sample_pdf):
    hits = list(search(sample_pdf.doc, "kolonne"))
    assert len(hits) == 40
    assert all(h.page == 1 for h in hits)


def test_context_is_short_with_ellipsis():
    text = " ".join(f"ord{i}" for i in range(25)) + " NØGLE " + " ".join(f"slut{i}" for i in range(25))
    pdf = make_doc(text)
    hit = next(search(pdf, "nøgle"))
    assert "NØGLE" in hit.context
    assert hit.context.startswith("…") and hit.context.endswith("…")
    assert len(hit.context) < 110
    assert "\n" not in hit.context


def test_rotated_page_hit_lands_on_rendered_text(sample_pdf):
    hits = list(search(sample_pdf.doc, "Roteret"))
    assert len(hits) == 1 and hits[0].page == 2
    rect = hits[0].rects[0]
    page = sample_pdf.page(2)
    assert rect in page.rect  # display coords: page is 842 x 595
    # vertical text on the rotated page: rect is tall and narrow
    assert rect.height > rect.width
    zoom = 2.0
    pix = render_page(sample_pdf.doc, 2, zoom)
    inside = dark_pixels_in(pix, rect, zoom)
    assert inside > 50
    # everything dark that belongs to the word is inside the hit rect
    word_only = dark_pixels_in(pix, rect + (-3, -3, 3, 3), zoom)
    assert word_only - inside < inside * 0.05


@pytest.mark.parametrize("rotation", [90, 180, 270])
def test_all_rotations(rotation):
    pdf = make_doc("Find mig her", rotation=rotation)
    page = pdf.page(0)
    hit = next(search(pdf, "mig"))
    unrot = page.search_for("mig")[0]
    expected = unrot * page.rotation_matrix
    assert all(abs(a - b) < 1.5 for a, b in zip(hit.rects[0], expected.normalize()))
    zoom = 2.0
    pix = render_page(pdf.doc, 0, zoom)
    assert dark_pixels_in(pix, hit.rects[0], zoom) > 20


def test_search_after_rotation_edit(sample_pdf):
    before = next(search(sample_pdf, "Kvartalsrapport")).rects[0]
    rotate_pages(sample_pdf, [0], 90)
    after = next(search(sample_pdf, "Kvartalsrapport")).rects[0]
    # page 0 is 595 wide unrotated: rotating 90 maps (x, y) -> (842 - y, x)
    assert abs(after.x0 - (842 - before.y1)) < 0.5
    assert abs(after.y0 - before.x0) < 0.5


def test_search_page_and_start_page(sample_pdf):
    assert search_page(sample_pdf.doc, 0, "kolonne") == []
    assert len(search_page(sample_pdf.doc, 1, "kolonne")) == 40
    assert list(search(sample_pdf.doc, "Kvartalsrapport", start_page=1)) == []


def test_generator_survives_undo_between_pages(sample_pdf):
    rotate_pages(sample_pdf, [0])
    it = search(sample_pdf, "e")
    next(it)
    sample_pdf.undo()  # replaces the underlying pymupdf.Document
    rest = list(it)
    assert rest
