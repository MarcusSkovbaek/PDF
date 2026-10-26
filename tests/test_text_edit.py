import pymupdf
import pytest

from pdfstudio.core import CONTENT, PdfDocument
from pdfstudio.core.fonts import SpanStyle, style_from_span
from pdfstudio.core.text_edit import TextBlock, block_at, replace_block_text, text_blocks
from tests.conftest import SANS_NAME, SANS_TTF, make_sample_pdf


# -- helpers ------------------------------------------------------------------
def spans(page):
    out = []
    for b in page.get_text("dict")["blocks"]:
        for ln in b.get("lines", ()):
            for s in ln["spans"]:
                if s["text"].strip():
                    out.append(dict(s, dir=ln["dir"]))
    return out


def find_span(page, needle):
    hits = [s for s in spans(page) if needle in s["text"]]
    assert hits, f"{needle!r} not found in {[s['text'] for s in spans(page)]}"
    return hits[0]


def block_with(pdf, page, needle) -> TextBlock:
    return next(b for b in text_blocks(pdf.doc, page) if needle in b.text)


def pix(page, clip=None, zoom=2):
    return page.get_pixmap(matrix=pymupdf.Matrix(zoom, zoom), clip=clip, alpha=False)


def ink_bbox(pm, threshold=200):
    """Bounding box (in pixmap pixels) of non-white pixels."""
    xs, ys = [], []
    s = pm.samples
    n = pm.n
    for y in range(pm.height):
        row = y * pm.stride
        for x in range(pm.width):
            i = row + x * n
            if min(s[i], s[i + 1], s[i + 2]) < threshold:
                xs.append(x)
                ys.append(y)
    if not xs:
        return None
    return min(xs), min(ys), max(xs), max(ys)


@pytest.fixture
def pdf():
    p = PdfDocument.from_bytes(make_sample_pdf())
    yield p
    p.close()


# -- extraction -----------------------------------------------------------------
def test_text_blocks_page0(pdf):
    blocks = text_blocks(pdf.doc, 0)
    texts = [b.text for b in blocks]
    assert "Kvartalsrapport 2026" in texts
    times = block_with(pdf, 0, "broedtekst")
    assert times.text == "Dette er en broedtekst skrevet med Times.\nAnden linje i samme afsnit."
    assert len(times.lines) == 2
    assert times.style.serif and not times.style.bold
    assert times.style.size == pytest.approx(12)
    assert times.lines[0].origin == pytest.approx((72, 130))
    assert times.lines[1].origin == pytest.approx((72, 146))
    head = block_with(pdf, 0, "Kvartalsrapport")
    assert head.style.color == pytest.approx((0.1, 0.1, 0.1), abs=0.01)
    assert head.style.size == pytest.approx(24)
    if SANS_TTF:
        bold = block_with(pdf, 0, "Fed overskrift")
        assert bold.style.bold and not bold.style.serif
        emb = block_with(pdf, 0, "Indlejret")
        assert emb.style.color == pytest.approx((0, 0.2, 0.8), abs=0.01)


def test_text_blocks_rotated_page_display_coords(pdf):
    page = pdf.doc[2]
    assert page.rotation == 90
    (blk,) = text_blocks(pdf.doc, 2)
    assert blk.text == "Roteret side"
    # raw (72, 100) on a page rotated by 90 degrees -> display (842-100, 72)
    assert blk.lines[0].origin == pytest.approx((742, 72))
    assert page.rect.contains(blk.bbox)
    # the rendered glyphs are inside the reported display bbox
    pm = pix(page, zoom=1)
    x0, y0, x1, y1 = ink_bbox(pm)
    assert blk.bbox.x0 - 2 <= x0 and x1 <= blk.bbox.x1 + 2
    assert blk.bbox.y0 - 2 <= y0 and y1 <= blk.bbox.y1 + 2


def test_block_at(pdf):
    blk = block_at(pdf.doc, 0, (100, 140))
    assert blk is not None and "broedtekst" in blk.text
    assert block_at(pdf.doc, 0, (500, 700)) is None
    rot = block_at(pdf.doc, 2, (748, 100))
    assert rot is not None and rot.text == "Roteret side"


# -- editing ------------------------------------------------------------------------
def test_edit_times_line_keeps_format(pdf):
    blk = block_with(pdf, 0, "broedtekst")
    events = []
    pdf.listen(events.append)
    replace_block_text(pdf, blk, "Dette er en ændret tekst i Times.\nAnden linje i samme afsnit.")
    assert events and events[-1].kind == CONTENT and events[-1].pages == (0,)
    page = pdf.doc[0]
    new = find_span(page, "ændret")
    assert new["text"] == "Dette er en ændret tekst i Times."
    assert "Times" in new["font"] or "Roman" in new["font"] or "Serif" in new["font"]
    assert new["flags"] & pymupdf.TEXT_FONT_SERIFED
    assert new["size"] == pytest.approx(12, abs=0.01)
    assert new["color"] == 0
    assert new["origin"][0] == pytest.approx(72, abs=0.5)
    assert new["origin"][1] == pytest.approx(130, abs=0.5)
    # unchanged second line is untouched (still the original base-14 font)
    second = find_span(page, "Anden linje")
    assert second["font"] == "Times-Roman" and second["origin"] == pytest.approx((72, 146))
    # the block now reads back as edited
    assert block_with(pdf, 0, "ændret").text == "Dette er en ændret tekst i Times.\nAnden linje i samme afsnit."
    # other blocks unchanged
    head = find_span(page, "Kvartalsrapport")
    assert head["font"] == "Helvetica" and head["origin"] == pytest.approx((72, 90))
    assert "broedtekst" not in page.get_text()


def test_unchanged_regions_render_identically(pdf):
    page = pdf.doc[0]
    clips = [pymupdf.Rect(60, 50, 400, 105), pymupdf.Rect(60, 180, 400, 250)]
    before = [pix(page, c).samples for c in clips]
    blk = block_with(pdf, 0, "broedtekst")
    replace_block_text(pdf, blk, "Kort.\nAnden linje i samme afsnit.")
    page = pdf.doc[0]
    after = [pix(page, c).samples for c in clips]
    assert before == after
    assert "Kort." in page.get_text()
    assert pdf.history.undo_label == "Redigér tekst"


def test_undo_restores_original(pdf):
    original = pdf.doc[0].get_text()
    blk = block_with(pdf, 0, "broedtekst")
    replace_block_text(pdf, blk, "Noget helt andet")
    assert pdf.doc[0].get_text() != original
    assert pdf.undo()
    assert pdf.doc[0].get_text() == original
    assert pdf.redo()
    assert "Noget helt andet" in pdf.doc[0].get_text()


def test_no_change_creates_no_undo_step(pdf):
    blk = block_with(pdf, 0, "broedtekst")
    replace_block_text(pdf, blk, blk.text)
    assert not pdf.history.can_undo


@pytest.mark.skipif(not SANS_TTF, reason="no sans TTF")
def test_embedded_font_kept_without_duplicating_it(pdf):
    size0 = len(pdf.doc.tobytes(garbage=3, deflate=True))
    blk = block_with(pdf, 0, "Indlejret")
    replace_block_text(pdf, blk, "Indlejret skrifttype: Hej Verden – blåbær")
    page = pdf.doc[0]
    s = find_span(page, "Hej Verden")
    assert s["text"] == "Indlejret skrifttype: Hej Verden – blåbær"
    assert SANS_NAME.removesuffix("Sans") in s["font"].replace(" ", "")
    assert s["size"] == pytest.approx(14, abs=0.01)
    assert s["color"] == 0x0033CC
    assert s["origin"] == pytest.approx((72, 200), abs=0.5)
    # the (400 KB) font program was not embedded a second time
    size1 = len(pdf.doc.tobytes(garbage=3, deflate=True))
    assert size1 - size0 < 30_000


@pytest.mark.skipif(not SANS_TTF, reason="no sans TTF")
def test_subset_font_reused_then_fallback_for_new_glyphs():
    d = pymupdf.open("pdf", make_sample_pdf())
    d.subset_fonts()
    pdf = PdfDocument.from_bytes(d.tobytes(garbage=3, deflate=True))
    d.close()
    page = pdf.doc[0]
    size0 = len(pdf.doc.tobytes(garbage=3, deflate=True))
    fonts_before = {f[3] for f in page.get_fonts()}

    blk = block_with(pdf, 0, "Hello")
    replace_block_text(pdf, blk, "World Hello Indlejret")
    page = pdf.doc[0]
    s = find_span(page, "World Hello")
    assert s["text"] == "World Hello Indlejret"
    # still the embedded subset font program (small), not a 400 KB system font
    assert SANS_NAME.removesuffix("Sans") in s["font"].replace(" ", "")
    assert s["origin"] == pytest.approx((72, 200), abs=0.5)
    # no foreign font was embedded (e.g. a fallback just for the spaces)
    new_fonts = {f[3] for f in page.get_fonts()} - fonts_before
    assert all("Liberation" in name for name in new_fonts), new_fonts
    assert len(pdf.doc.tobytes(garbage=3, deflate=True)) - size0 < 60_000
    # glyphs really render (subset glyphs were not emptied)
    assert ink_bbox(pix(page, pymupdf.Rect(70, 185, 300, 205))) is not None

    blk = block_with(pdf, 0, "World Hello")
    replace_block_text(pdf, blk, "Hej ÆØÅ æøå")
    page = pdf.doc[0]
    s = find_span(page, "ÆØÅ")
    assert s["text"] == "Hej ÆØÅ æøå"
    assert not style_from_span(s["font"], 14, 0, 0).serif
    assert s["color"] == 0x0033CC and s["size"] == pytest.approx(14, abs=0.01)
    # each glyph renders: ink spans most of the expected width
    box = ink_bbox(pix(page, pymupdf.Rect(70, 185, 300, 205), zoom=2))
    assert box is not None and (box[2] - box[0]) / 2 > 60
    pdf.close()


def test_rect_and_image_survive_editing_nearby_text(pdf):
    page = pdf.doc[1]
    images_before = [(i[0], i[2], i[3]) for i in page.get_images()]
    drawings_before = [(d["rect"], d.get("fill")) for d in page.get_drawings()]
    image_rects_before = [i["bbox"] for i in page.get_image_info()]
    right_before = block_with(pdf, 1, "Hoejre").text

    blk = block_with(pdf, 1, "Venstre")
    replace_block_text(pdf, blk, "Ny venstre kolonne.\nMed to linjer.")
    page = pdf.doc[1]
    assert [(i[0], i[2], i[3]) for i in page.get_images()] == images_before
    assert [(d["rect"], d.get("fill")) for d in page.get_drawings()] == drawings_before
    assert [i["bbox"] for i in page.get_image_info()] == image_rects_before
    # rendered pixels of the filled rect and of the image
    pm = pix(page, zoom=1)
    assert pm.pixel(300, 450)[:3] == pytest.approx((229, 229, 255), abs=2)
    assert pm.pixel(150, 560)[:3] == pytest.approx((200, 60, 60), abs=3)
    assert block_with(pdf, 1, "Hoejre").text == right_before
    assert "Venstre kolonne" not in page.get_text()
    assert block_with(pdf, 1, "Ny venstre").text == "Ny venstre kolonne.\nMed to linjer."


def test_text_over_filled_rect_keeps_background():
    doc = pymupdf.open()
    p = doc.new_page()
    p.draw_rect(pymupdf.Rect(50, 50, 400, 120), color=None, fill=(1, 1, 0))
    p.insert_text((60, 90), "Tekst på gul baggrund", fontname="helv", fontsize=14)
    pdf = PdfDocument.from_bytes(doc.tobytes())
    replace_block_text(pdf, text_blocks(pdf.doc, 0)[0], "Ændret tekst")
    page = pdf.doc[0]
    assert page.get_text().strip() == "Ændret tekst"
    pm = pix(page, zoom=1)
    assert pm.pixel(300, 85)[:3] == (255, 255, 0)  # background where old text was
    assert len(page.get_drawings()) == 1


def test_rotated_page_edit_keeps_display_location(pdf):
    (blk,) = text_blocks(pdf.doc, 2)
    ink_before = ink_bbox(pix(pdf.doc[2], zoom=1))
    replace_block_text(pdf, blk, "Roteret side ændret")
    (new,) = text_blocks(pdf.doc, 2)
    assert new.text == "Roteret side ændret"
    assert new.lines[0].origin == pytest.approx(blk.lines[0].origin, abs=0.5)
    assert new.lines[0].dir == pytest.approx(blk.lines[0].dir)
    assert new.style.size == pytest.approx(18, abs=0.01)
    # displayed text runs top -> bottom from the same corner as before
    pm = pix(pdf.doc[2], zoom=1)
    x0, y0, x1, y1 = ink_bbox(pm)
    assert abs(y0 - ink_before[1]) <= 1 and y1 > ink_before[3] + 20
    assert abs(x0 - ink_before[0]) <= 1 and abs(x1 - ink_before[2]) <= 1


def test_add_and_remove_lines(pdf):
    blk = block_with(pdf, 0, "broedtekst")
    replace_block_text(pdf, blk, blk.text + "\nTredje linje tilføjet.")
    page = pdf.doc[0]
    third = find_span(page, "Tredje")
    assert third["origin"] == pytest.approx((72, 162), abs=0.5)  # same 16 pt spacing
    assert third["size"] == pytest.approx(12, abs=0.01)
    blk = block_with(pdf, 0, "broedtekst")
    assert len(blk.lines) == 3
    replace_block_text(pdf, blk, "Dette er en broedtekst skrevet med Times.")
    text = pdf.doc[0].get_text()
    assert "Anden linje" not in text and "Tredje" not in text and "broedtekst" in text


def test_empty_text_deletes_block(pdf):
    blk = block_with(pdf, 0, "broedtekst")
    replace_block_text(pdf, blk, "")
    text = pdf.doc[0].get_text()
    assert "broedtekst" not in text and "Anden linje" not in text
    assert "Kvartalsrapport" in text


def test_long_line_wraps_to_block_width(pdf):
    blk = block_with(pdf, 1, "Venstre")
    width = blk.bbox.width
    long = "Denne linje er alt for lang til kolonnen og skal derfor ombrydes pænt over flere linjer."
    replace_block_text(pdf, blk, long)
    new = block_with(pdf, 1, "Denne linje")
    assert len(new.lines) >= 2
    assert " ".join(ln.text for ln in new.lines) == long
    for ln in new.lines:
        assert ln.bbox.x0 >= blk.bbox.x0 - 0.5
        assert ln.bbox.width <= width + 3
    # right column untouched
    assert block_with(pdf, 1, "Hoejre").bbox.x0 == pytest.approx(305, abs=0.5)


def test_single_line_heading_grows_without_wrapping(pdf):
    blk = block_with(pdf, 0, "Kvartalsrapport")
    replace_block_text(pdf, blk, "Kvartalsrapport 2026 – tredje kvartal")
    s = find_span(pdf.doc[0], "tredje")
    assert s["text"] == "Kvartalsrapport 2026 – tredje kvartal"
    assert s["origin"] == pytest.approx((72, 90), abs=0.5)
    assert s["color"] == 0x1A1A1A


def test_style_override(pdf):
    blk = block_with(pdf, 0, "broedtekst")
    st = SpanStyle(**{**vars(blk.style), "color": (0.8, 0.0, 0.0), "size": 14.0, "bold": True})
    replace_block_text(pdf, blk, blk.text, style=st)
    page = pdf.doc[0]
    first, second = find_span(page, "broedtekst"), find_span(page, "Anden")
    for s in (first, second):
        assert s["size"] == pytest.approx(14, abs=0.01)
        assert s["color"] == 0xCC0000
        assert s["flags"] & pymupdf.TEXT_FONT_BOLD or "Bold" in s["font"]
    assert first["origin"] == pytest.approx((72, 130), abs=0.5)
    # line spacing scaled with the size (16 * 14/12)
    assert second["origin"][1] - first["origin"][1] == pytest.approx(16 * 14 / 12, abs=0.6)


def test_right_aligned_block_stays_right_aligned():
    doc = pymupdf.open()
    p = doc.new_page()
    right = 500
    for i, t in enumerate(["Kunde A/S", "Hovedgaden 12", "1234 Byen"]):
        w = pymupdf.get_text_length(t, "helv", 11)
        p.insert_text((right - w, 100 + 14 * i), t, fontname="helv", fontsize=11)
    pdf = PdfDocument.from_bytes(doc.tobytes())
    blk = text_blocks(pdf.doc, 0)[0]
    assert len(blk.lines) == 3
    replace_block_text(pdf, blk, "Kunde A/S\nLange Hovedgade 120\n1234 Byen")
    s = find_span(pdf.doc[0], "Lange")
    assert s["bbox"][2] == pytest.approx(right, abs=0.6)
    assert s["origin"][1] == pytest.approx(114, abs=0.5)


def test_rotated_text_line_keeps_direction():
    doc = pymupdf.open()
    p = doc.new_page()
    p.insert_text((100, 500), "Lodret tekst", fontname="helv", fontsize=12, rotate=90)
    p.insert_text((200, 100), "Vandret", fontname="helv", fontsize=12)
    pdf = PdfDocument.from_bytes(doc.tobytes())
    blk = next(b for b in text_blocks(pdf.doc, 0) if "Lodret" in b.text)
    replace_block_text(pdf, blk, "Lodret tekst ændret")
    s = find_span(pdf.doc[0], "Lodret")
    assert s["text"] == "Lodret tekst ændret"
    assert s["dir"] == pytest.approx(blk.lines[0].dir, abs=1e-3)
    assert s["origin"] == pytest.approx(blk.lines[0].origin, abs=0.5)
    assert find_span(pdf.doc[0], "Vandret")["origin"] == pytest.approx((200, 100))


def test_tight_leading_neighbours_survive():
    doc = pymupdf.open()
    p = doc.new_page()
    for i in range(3):
        p.insert_text((72, 100 + 11 * i), f"Linje nummer {i} tæt sat", fontname="tiro", fontsize=12)
    pdf = PdfDocument.from_bytes(doc.tobytes())
    blk = text_blocks(pdf.doc, 0)[0]
    lines = blk.text.split("\n")
    assert len(lines) == 3
    lines[1] = "Midterste linje"
    replace_block_text(pdf, blk, "\n".join(lines))
    text = pdf.doc[0].get_text()
    assert "Linje nummer 0 tæt sat" in text and "Linje nummer 2 tæt sat" in text
    assert "Linje nummer 1" not in text and "Midterste linje" in text


def test_pending_user_redaction_is_not_applied(pdf):
    page = pdf.doc[0]
    page.add_redact_annot(pymupdf.Rect(60, 60, 300, 100))  # over the heading
    blk = block_with(pdf, 0, "broedtekst")
    replace_block_text(pdf, blk, "Ny tekst")
    page = pdf.doc[0]
    assert "Kvartalsrapport" in page.get_text()
    redacts = list(page.annots(types=(pymupdf.PDF_ANNOT_REDACT,)))
    assert len(redacts) == 1
    assert tuple(redacts[0].rect) == pytest.approx((60, 60, 300, 100), abs=1)


def test_file_growth_after_repeated_edits(pdf):
    size0 = len(pdf.doc.tobytes(garbage=3, deflate=True))
    for i in range(5):
        blk = block_with(pdf, 0, "Anden linje")
        replace_block_text(pdf, blk, blk.text + f" {i}")
    size1 = len(pdf.doc.tobytes(garbage=3, deflate=True))
    assert "Anden linje" in pdf.doc[0].get_text()
    # one copy of the Times clone (~50 KB) at most, not one per edit
    assert size1 - size0 < 90_000


def test_paragraph_reflows_into_following_lines(pdf):
    blk = block_with(pdf, 1, "Venstre")
    lines = blk.text.split("\n")
    n = len(lines)
    lines[1] = lines[1] + " Tilføjet tekst her."
    replace_block_text(pdf, blk, "\n".join(lines))
    new = block_with(pdf, 1, "Tilføjet")
    # words flow on into the next lines instead of leaving a ragged stub
    assert " ".join(new.text.split()) == " ".join(" ".join(lines).split())
    assert len(new.lines) <= n + 1
    for ln in new.lines:
        assert ln.bbox.x1 <= blk.bbox.x1 + 3
    # the line before the edit is untouched (original font)
    assert find_span(pdf.doc[1], lines[0])["font"] == "Helvetica"


@pytest.mark.parametrize("rotation", [0, 90])
def test_cropped_page_coordinates(rotation):
    doc = pymupdf.open()
    p = doc.new_page(width=595, height=842)
    p.insert_text((100, 200), "Beskaaret side tekst", fontname="helv", fontsize=12)
    p.set_cropbox(pymupdf.Rect(50, 100, 545, 742))
    p.set_rotation(rotation)
    pdf = PdfDocument.from_bytes(doc.tobytes())
    (blk,) = text_blocks(pdf.doc, 0)
    assert pdf.doc[0].rect.contains(blk.bbox)
    replace_block_text(pdf, blk, "Beskaaret side ændret")
    (new,) = text_blocks(pdf.doc, 0)
    assert new.text == "Beskaaret side ændret"
    assert new.lines[0].origin == pytest.approx(blk.lines[0].origin, abs=0.5)


def test_invisible_ocr_text_is_not_offered_for_editing():
    doc = pymupdf.open()
    p = doc.new_page()
    p.insert_text((50, 50), "Usynlig OCR", fontname="helv", fontsize=12, render_mode=3)
    p.insert_text((50, 80), "Synlig", fontname="helv", fontsize=12)
    assert [b.text for b in text_blocks(doc, 0)] == ["Synlig"]


def test_inline_bold_and_superscript_survive_edit():
    doc = pymupdf.open()
    p = doc.new_page()
    x = 72
    for txt, font, size, rise in [("Normal ", "helv", 12, 0), ("fed", "hebo", 12, 0), (" tekst", "helv", 12, 0),
                                  ("2", "helv", 7, -5)]:
        p.insert_text((x, 100 + rise), txt, fontname=font, fontsize=size)
        x += pymupdf.get_text_length(txt, font, size)
    pdf = PdfDocument.from_bytes(doc.tobytes())
    blk = text_blocks(pdf.doc, 0)[0]
    assert blk.text == "Normal fed tekst2"
    replace_block_text(pdf, blk, "Normal fed tekst ændret2")
    page = pdf.doc[0]
    bold = find_span(page, "fed")
    assert bold["flags"] & pymupdf.TEXT_FONT_BOLD or "Bold" in bold["font"]
    assert bold["text"].strip() == "fed"
    sup = [s for s in spans(page) if s["text"].strip() == "2"]
    assert sup and sup[0]["size"] == pytest.approx(7, abs=0.01)
    assert sup[0]["origin"][1] == pytest.approx(95, abs=0.5)
    assert "ændret" in page.get_text()


def test_justified_paragraph_stays_justified():
    doc = pymupdf.open()
    p = doc.new_page()
    text = ("Dette er et justeret afsnit med en del tekst, som fylder flere linjer, så vi kan se at "
            "linjerne stadig flugter i højre side efter en redigering af et ord midt i afsnittet. ") * 2
    p.insert_textbox(pymupdf.Rect(72, 72, 300, 300), text, fontname="tiro", fontsize=11,
                     align=pymupdf.TEXT_ALIGN_JUSTIFY)
    pdf = PdfDocument.from_bytes(doc.tobytes())
    blk = text_blocks(pdf.doc, 0)[0]
    right = max(ln.bbox.x1 for ln in blk.lines)
    lines = blk.text.split("\n")
    assert "flere" in lines[1]
    lines[1] = lines[1].replace("flere", "mange")
    replace_block_text(pdf, blk, "\n".join(lines))
    new = text_blocks(pdf.doc, 0)[0]
    edited = new.lines[1]
    assert "mange" in edited.text
    assert edited.bbox.x1 == pytest.approx(right, abs=1.0)


def test_style_override_with_new_family(pdf):
    blk = block_with(pdf, 0, "broedtekst")
    st = SpanStyle(**{**vars(blk.style), "font": "Helvetica"})
    replace_block_text(pdf, blk, blk.text, style=st)
    s = find_span(pdf.doc[0], "broedtekst")
    assert "Sans" in s["font"] or "Helvetica" in s["font"] or "Arial" in s["font"]
    assert text_blocks(pdf.doc, 0)[1].style.serif is False
    assert s["origin"] == pytest.approx((72, 130), abs=0.5)


def test_columns_are_in_reading_order(sample_pdf):
    # page 1 has two columns in different fonts whose first baselines differ slightly
    from pdfstudio.core import text_edit as te

    blocks = te.text_blocks(sample_pdf.doc, 1)
    assert blocks[0].text.startswith("Venstre") and blocks[1].text.startswith("Hoejre")


def _aligned_doc():
    import pymupdf

    from pdfstudio.core import PdfDocument

    doc = pymupdf.open()
    pg = doc.new_page()
    pg.insert_textbox(pymupdf.Rect(100, 100, 500, 200), "Centreret linje\nAnden centreret", fontname="helv",
                      fontsize=12, align=1)
    pg.insert_textbox(pymupdf.Rect(100, 260, 500, 360), "Højrestillet tekst", fontname="helv", fontsize=12, align=2)
    return PdfDocument.from_bytes(doc.tobytes())


def test_layout_info_reports_alignment_and_wrap(sample_pdf):
    from pdfstudio.core import text_edit as te

    pdf = _aligned_doc()
    infos = {b.text.split("\n")[0]: te.layout_info(pdf.doc, b) for b in te.text_blocks(pdf.doc, 0)}
    assert infos["Centreret linje"].align == "center"
    assert infos["Højrestillet tekst"].align == "right"
    col = next(b for b in te.text_blocks(sample_pdf.doc, 1) if b.text.startswith("Venstre"))
    info = te.layout_info(sample_pdf.doc, col)
    assert info.align == "left" and abs(info.wrap_width - col.bbox.width) < 2   # a column keeps its width


def test_saving_subsets_fonts_added_by_edits(sample_pdf, tmp_path):
    """Edits may embed a whole system font; saving keeps only the used glyphs."""
    import os

    import pymupdf

    from pdfstudio.core import text_edit as te

    b = next(x for x in te.text_blocks(sample_pdf.doc, 0) if x.text.startswith("Indlejret"))
    te.replace_block_text(sample_pdf, b, "Indlejret skrifttype med ÆØÅ æøå og flere tegn: §½€")
    unsubset = len(sample_pdf.doc.tobytes(garbage=3, deflate=True))
    out = str(tmp_path / "saved.pdf")
    sample_pdf.save(out)
    assert os.path.getsize(out) < unsubset
    d = pymupdf.open(out)
    assert "ÆØÅ æøå" in d[0].get_text() and not d.is_repaired


def _list_and_paragraph_doc():
    import pymupdf

    from pdfstudio.core import PdfDocument

    doc = pymupdf.open()
    pg = doc.new_page()
    body = ("Kvartalets resultat viser en markant fremgang i omsætningen, og bestyrelsen forventer, at "
            "udviklingen fortsætter i næste regnskabsår. ") * 3
    pg.insert_textbox(pymupdf.Rect(72, 72, 523, 200), body, fontname="helv", fontsize=11, align=3)
    y = 230
    for item in ("Omsætning steg med 12 %", "Overskud før skat: 48,2 mio. kr.", "Medarbejdere: 312"):
        pg.insert_text((100, y), item, fontname="helv", fontsize=11)
        y += 15
    return PdfDocument.from_bytes(doc.tobytes())


def test_list_items_are_hard_breaks_and_do_not_merge():
    from pdfstudio.core import text_edit as te

    pdf = _list_and_paragraph_doc()
    lst = next(b for b in te.text_blocks(pdf.doc, 0) if b.text.startswith("Omsætning"))
    info = te.layout_info(pdf.doc, lst)
    assert info.soft_breaks == () and not info.justified
    te.replace_block_text(pdf, lst, lst.text.replace("12 %", "12 % i forhold til sidste år, over forventning"))
    new = next(b for b in te.text_blocks(pdf.doc, 0) if b.text.startswith("Omsætning"))
    lines = new.text.split("\n")
    assert lines[0].endswith("over forventning")          # grew sideways in the column
    assert lines[1].startswith("Overskud") and lines[2].startswith("Medarbejdere")


def test_justified_paragraph_reports_soft_breaks():
    from pdfstudio.core import text_edit as te

    pdf = _list_and_paragraph_doc()
    par = next(b for b in te.text_blocks(pdf.doc, 0) if b.text.startswith("Kvartalets"))
    info = te.layout_info(pdf.doc, par)
    assert info.justified and info.soft_breaks == tuple(range(len(par.lines) - 1))


# -- changed lines keep their old geometry (table cells, unchanged words) -------------
def _words(page, clip=None):
    """[(word, x0 of its first glyph origin, x1)] in raw page space, reading order."""
    out = []
    for b in page.get_text("rawdict", clip=clip)["blocks"]:
        for ln in b.get("lines", ()):
            cur = []
            for s in ln["spans"]:
                for ch in s["chars"]:
                    if ch["c"].isspace():
                        if cur:
                            out.append(cur)
                        cur = []
                    else:
                        cur.append(ch)
            if cur:
                out.append(cur)
    return [("".join(c["c"] for c in w), w[0]["origin"][0], w[-1]["bbox"][2]) for w in out]


def _raw_text_doc(content: bytes, rotation: int = 0) -> PdfDocument:
    """A page whose content stream is ``content`` (Helvetica as /helv)."""
    doc = pymupdf.open()
    pg = doc.new_page()
    pg.insert_text((72, 100), "x", fontname="helv", fontsize=12)   # registers /helv
    doc.update_stream(pg.get_contents()[0], content)
    if rotation:
        pg.set_rotation(rotation)
    return PdfDocument.from_bytes(doc.tobytes())


def _table_line_doc(rotation=0):
    """One line with two table cells 7 pt apart, positioned like office
    suites do it (TJ offsets, no space character)."""
    pdf = _raw_text_doc(b"BT /helv 10 Tf 1 0 0 1 100 742 Tm [(2025) -700 (2026)] TJ ET", rotation)
    return pdf, 100 + pymupdf.get_text_length("2025", "helv", 10) + 7


@pytest.mark.parametrize("rotation", [0, 90])
def test_wide_gap_in_changed_line_keeps_the_column(rotation):
    pdf, col = _table_line_doc(rotation)
    (blk,) = text_blocks(pdf.doc, 0)
    assert blk.text == "2025 2026" and len(blk.lines) == 1
    replace_block_text(pdf, blk, "2025 2027")
    words = dict((w, x) for w, x, _x1 in _words(pdf.doc[0]))
    assert words["2025"] == pytest.approx(100, abs=0.05)
    assert words["2027"] == pytest.approx(col, abs=0.05)          # not collapsed to a word space
    (blk,) = text_blocks(pdf.doc, 0)
    replace_block_text(pdf, blk, "2025, 2027")                     # the first cell grows into the gap
    words = dict((w, x) for w, x, _x1 in _words(pdf.doc[0]))
    assert words["2027"] == pytest.approx(col, abs=0.05)
    (blk,) = text_blocks(pdf.doc, 0)
    replace_block_text(pdf, blk, "1202520252025 2027")             # ... and pushes it when it must
    (a, xa, ea), (b, xb, _eb) = _words(pdf.doc[0])
    assert (a, b) == ("1202520252025", "2027") and xb > col and xb - ea > 2.0


def _compact_table_doc():
    """Rows "label  num  num" with right-aligned number columns 4 pt apart."""
    doc = pymupdf.open()
    pg = doc.new_page()
    rows = [("Q1", "101,5", "112,0"), ("Q2", "98,7", "120,4"), ("Q3", "110,2", "99,9")]
    for k, (label, a, b) in enumerate(rows):
        y = 100 + 18 * k
        pg.insert_text((60, y), label, fontname="helv", fontsize=10)
        for text, right in ((a, 120.0), (b, 150.0)):
            pg.insert_text((right - pymupdf.get_text_length(text, "helv", 10), y), text, fontname="helv", fontsize=10)
    return PdfDocument.from_bytes(doc.tobytes())


def test_compact_table_cells_keep_their_right_edge():
    pdf = _compact_table_doc()
    row = next(b for b in text_blocks(pdf.doc, 0) if b.text.startswith("98,7"))
    assert row.text == "98,7 120,4"
    replace_block_text(pdf, row, "98,7 7,5")
    words = {w: (x0, x1) for w, x0, x1 in _words(pdf.doc[0], clip=row.bbox + (-20, -2, 20, 2))}
    assert words["98,7"][1] == pytest.approx(120, abs=0.1)
    assert words["7,5"][1] == pytest.approx(150, abs=0.1)          # right-aligned like its column
    row = next(b for b in text_blocks(pdf.doc, 0) if b.text.startswith("98,7"))
    assert row.text == "98,7"                                      # the cells are blocks of their own now
    from pdfstudio.core.text_edit import layout_info

    assert layout_info(pdf.doc, row).align == "right"              # ... in a right-aligned column
    replace_block_text(pdf, row, "1.098,7")                        # so it grows to the left
    words = {w: (x0, x1) for w, x0, x1 in _words(pdf.doc[0], clip=row.bbox + (-20, -2, 40, 2))}
    assert words["1.098,7"][1] == pytest.approx(120, abs=0.1)
    assert words["7,5"][1] == pytest.approx(150, abs=0.1)
    row = next(b for b in text_blocks(pdf.doc, 0) if b.text.startswith("1.098,7"))
    replace_block_text(pdf, row, "1.098,7 7,5")                    # (typing a cell into another)


def test_unchanged_beginning_of_a_changed_line_is_pixel_identical():
    # kerned glyph positions (TJ) that the font's own advances would not reproduce
    pdf = _raw_text_doc(b"BT /helv 12 Tf 1 0 0 1 72 700 Tm [(Om) 30 (s) -20 (\\346tning i alt) -400 (410,5 mio.)] TJ ET")
    (blk,) = text_blocks(pdf.doc, 0)
    assert blk.text.startswith("Omsætning i alt")
    page = pdf.doc[0]
    clip = pymupdf.Rect(blk.bbox.x0 - 2, blk.bbox.y0 - 2, blk.bbox.x0 + 100, blk.bbox.y1 + 2)
    before = pix(page, clip).samples
    words_before = _words(page)
    replace_block_text(pdf, blk, blk.text.replace("410,5", "411,0"))
    page = pdf.doc[0]
    assert pix(page, clip).samples == before                     # "Omsætning i alt" did not move
    after = dict((w, x) for w, x, _x1 in _words(page))
    assert after["411,0"] == pytest.approx(dict((w, x) for w, x, _ in words_before)["410,5"], abs=0.05)


def test_centred_and_justified_lines_still_relayout():
    pdf = _aligned_doc()
    blk = next(b for b in text_blocks(pdf.doc, 0) if b.text.startswith("Centreret"))
    c = (blk.lines[0].bbox.x0 + blk.lines[0].bbox.x1) / 2
    replace_block_text(pdf, blk, blk.text.replace("Centreret linje", "Centreret linje der er længere"))
    new = next(b for b in text_blocks(pdf.doc, 0) if b.text.startswith("Centreret"))
    assert (new.lines[0].bbox.x0 + new.lines[0].bbox.x1) / 2 == pytest.approx(c, abs=0.6)


def test_block_angle_in_display_space(pdf):
    assert text_blocks(pdf.doc, 0)[0].angle == 0
    (rot,) = text_blocks(pdf.doc, 2)               # horizontal text on a page rotated by 90
    assert rot.angle == 90
    doc = pymupdf.open()
    p = doc.new_page()
    p.insert_text((100, 500), "Lodret tekst", fontname="helv", fontsize=12, rotate=90)
    up = PdfDocument.from_bytes(doc.tobytes())
    assert text_blocks(up.doc, 0)[0].angle == -90   # reads bottom to top


def test_caption_set_tight_above_a_paragraph_is_its_own_block():
    """LibreOffice (and MuPDF's block detection) puts a 9 pt figure caption
    and the 11 pt paragraph right below it into one block; editing the
    paragraph must not drag the caption along (its centred first line used to
    shift the whole editor sideways)."""
    doc = pymupdf.open()
    p = doc.new_page()
    p.insert_text((200, 100), "Figur 1: Omsætning fordelt på regioner", fontname="helv", fontsize=9,
                  color=(.4, .4, .4))
    y = 113
    for t in ("Forventer omsætning bestyrelsen udviklingen medarbejdere bestyrelsen leverandører væksten",
              "grønne regnskabsår resultat effektivisering medarbejdere leverandører og meget mere tekst",
              "samlede leverandører produktionen."):
        p.insert_text((72, y), t, fontname="helv", fontsize=11)
        y += 14
    assert len(p.get_text("dict")["blocks"]) == 1          # MuPDF: one block
    pdf = PdfDocument.from_bytes(doc.tobytes())
    caption, body = text_blocks(pdf.doc, 0)
    assert caption.text.startswith("Figur 1") and len(caption.lines) == 1
    assert body.text.startswith("Forventer") and len(body.lines) == 3
    assert abs(body.bbox.x0 - 72) < 1
    replace_block_text(pdf, body, body.text.replace("Forventer", "Vi forventer"))
    caption2, body2 = text_blocks(pdf.doc, 0)
    assert caption2.text == caption.text and abs(caption2.bbox.x0 - caption.bbox.x0) < 0.01
    assert body2.text.startswith("Vi forventer")


def test_superscript_on_the_same_row_stays_in_its_paragraph():
    doc = pymupdf.open()
    p = doc.new_page()
    p.insert_text((72, 100), "Omsætningen steg med 12 procent i Danmark", fontname="helv", fontsize=11)
    p.insert_text((300, 96), "1", fontname="helv", fontsize=6)
    p.insert_text((72, 114), "og fortsatte i resten af Norden.", fontname="helv", fontsize=11)
    pdf = PdfDocument.from_bytes(doc.tobytes())
    texts = [b.text for b in text_blocks(pdf.doc, 0)]
    assert any("Omsætningen" in t and "Norden" in t for t in texts), texts


def test_bullet_and_item_below_a_heading_are_separate_blocks():
    """LibreOffice slides: heading, then "bullet + list item" on one row, all
    in one MuPDF block. The heading must not glue bullet and item together
    (the editor showed the private-use bullet glyph as part of the text)."""
    doc = pymupdf.open()
    p = doc.new_page(width=960, height=540)
    p.insert_text((62, 93), "1. Udviklingen i grønne løsninger", fontname="hebo", fontsize=24)
    p.insert_text((80, 116), "•", fontname="helv", fontsize=18)
    p.insert_text((98, 116), "Forventer produktionen strategi bestyrelsen", fontname="helv", fontsize=18)
    pdf = PdfDocument.from_bytes(doc.tobytes())
    texts = [b.text for b in text_blocks(pdf.doc, 0)]
    assert "Forventer produktionen strategi bestyrelsen" in texts, texts
    assert not any("•" in t and "Forventer" in t for t in texts), texts


def _bulleted_list_doc():
    """Body text, a bulleted list (a two-line item that wrapped) and body
    text again, set tight like Word/LibreOffice export it."""
    doc = pymupdf.open()
    p = doc.new_page()
    body = "Omsætningen steg markant i alle regioner, og bestyrelsen forventer fortsat vækst i det kommende"
    y = 100
    for t in (body, body, "regnskabsår."):
        p.insert_text((62, y), t, fontname="helv", fontsize=11)
        y += 14
    items = [("Projekter i Århus og Aalborg fortsætter med fuld kraft og nye kunder kommer til hver", "måned."),
             ("Overskud før skat: 48,2 mio. kr.", None)]
    for first, second in items:
        p.insert_text((80, y), "•", fontname="helv", fontsize=11)
        p.insert_text((98, y), first, fontname="helv", fontsize=11)
        y += 14
        if second:
            p.insert_text((98, y), second, fontname="helv", fontsize=11)
            y += 14
    for t in (body, "slut."):
        p.insert_text((62, y), t, fontname="helv", fontsize=11)
        y += 14
    return PdfDocument.from_bytes(doc.tobytes())


def test_bulleted_list_items_are_blocks_of_their_own():
    from pdfstudio.core import text_edit as te

    pdf = _bulleted_list_doc()
    texts = [b.text for b in text_blocks(pdf.doc, 0)]
    item = next(b for b in text_blocks(pdf.doc, 0) if b.text.startswith("Projekter"))
    assert item.text.endswith("måned.") and len(item.lines) == 2, texts
    assert "Overskud før skat: 48,2 mio. kr." in texts            # next item: separate
    assert sum(len(t) == 1 for t in texts) == 2                    # bullets stay apart
    assert any(t.startswith("Omsætningen") and t.endswith("slut.") for t in texts)  # text after the list
    info = te.layout_info(pdf.doc, item)
    assert info.soft_breaks == (0,) and info.wrap_width < 540 - 98
    replace_block_text(pdf, item, item.text.replace("Projekter", "Nye projekter"))
    new = next(b for b in text_blocks(pdf.doc, 0) if b.text.startswith("Nye projekter"))
    assert len(new.lines) == 2 and new.bbox.x1 <= item.bbox.x1 + 1.0   # reflowed inside the item
    assert all(abs(ln.origin[0] - 98) < 0.5 for ln in new.lines)
