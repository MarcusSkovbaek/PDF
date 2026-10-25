"""Text editing 1.1: character formatting, underline/strike, alignment, line
spacing, moving the text below, overflow and shrink-to-fit."""

import pymupdf
import pytest

from pdfstudio.core import PdfDocument
from pdfstudio.core import text_edit as te

BODY = ("Selskabet har i årets løb haft en tilfredsstillende udvikling. Omsætningen steg med 12 procent, "
        "og resultatet før skat blev 6,1 mio. kr. Ledelsen anser resultatet for tilfredsstillende.")


def _doc(*, image_below: bool = False, underline: bool = False, rotation: int = 0) -> PdfDocument:
    doc = pymupdf.open()
    page = doc.new_page()
    page.insert_htmlbox(pymupdf.Rect(72, 60, 400, 200),
                        f"<p style='font-family:sans-serif;font-size:11pt'>{BODY}</p>")
    page.insert_text((72, 128), "Andet afsnit lige under.", fontname="helv", fontsize=11)
    page.insert_text((72, 144), "Tredje linje i samme flow.", fontname="helv", fontsize=11)
    page.insert_text((72, 800), "Sidefod 1", fontname="helv", fontsize=8)
    if image_below:
        page.draw_rect(pymupdf.Rect(72, 156, 400, 400), color=(0, 0, 0), fill=(0.8, 0.8, 0.9))
    if underline:
        page.insert_text((72, 300), "Vigtigt: understreget ord her", fontname="helv", fontsize=12)
        w0 = pymupdf.get_text_length("Vigtigt: ", "helv", 12)
        w1 = pymupdf.get_text_length("understreget", "helv", 12)
        page.draw_line((72 + w0, 302), (72 + w0 + w1, 302), color=(0, 0, 0), width=0.7)
    if rotation:
        page.set_rotation(rotation)
    return PdfDocument(pymupdf.open("pdf", doc.tobytes()))


def _block(pdf, startswith, page=0):
    return next(b for b in te.text_blocks(pdf.doc, page) if b.text.startswith(startswith))


def _baselines(pdf, text):
    return [ln.raw_origin[1] for b in te.text_blocks(pdf.doc, 0) for ln in b.lines if ln.text.startswith(text)]


def test_bold_one_word_inside_paragraph():
    pdf = _doc()
    b = _block(pdf, "Selskabet")
    i = b.text.index("Omsætningen")
    te.replace_block_text(pdf, b, b.text, formats=[(i, i + len("Omsætningen"), {"bold": True})])
    spans = [s for blk in pdf.page(0).get_text("dict")["blocks"] for ln in blk.get("lines", []) for s in ln["spans"]]
    bold = [s["text"] for s in spans if s["flags"] & 16]
    assert any("Omsætningen" in t for t in bold)
    assert not any("Selskabet" in t or "Ledelsen" in t for t in bold)


def test_formats_size_color_italic_and_strike():
    pdf = _doc()
    b = _block(pdf, "Selskabet")
    i = b.text.index("12 procent")
    te.replace_block_text(pdf, b, b.text, formats=[(i, i + 10, {"color": (0.8, 0, 0), "italic": True}),
                                                   (0, 9, {"strike": True})])
    page = pdf.page(0)
    red = [s for blk in page.get_text("dict")["blocks"] for ln in blk.get("lines", []) for s in ln["spans"]
           if s["color"] == 0xCC0000]
    assert red and "12" in "".join(s["text"] for s in red) and all(s["flags"] & 2 for s in red)
    # the strike-through line crosses "Selskabet"
    word = page.search_for("Selskabet")[0]
    lines = [d for d in page.get_drawings() if d["items"][0][0] == "l"]
    assert any(word.y0 < d["rect"].y0 < word.y1 and d["rect"].x0 <= word.x0 + 1 and d["rect"].x1 >= word.x1 - 1
               for d in lines)


def test_new_underline_is_drawn_under_the_word():
    pdf = _doc()
    b = _block(pdf, "Andet afsnit")
    te.replace_block_text(pdf, b, b.text, formats=[(0, 5, {"underline": True})])
    page = pdf.page(0)
    word = page.search_for("Andet")[0]
    strokes = [d["rect"] for d in page.get_drawings()]
    assert any(abs(r.x0 - word.x0) < 1.5 and abs(r.x1 - word.x1) < 1.5 and word.y1 - 4 < r.y0 < word.y1 + 2
               for r in strokes)


def test_existing_underline_is_detected_and_follows_the_text():
    pdf = _doc(underline=True)
    b = _block(pdf, "Vigtigt")
    ln = b.lines[0]
    under = [c for c, st in zip(ln.text, ln.char_styles) if st.underline]
    assert "".join(under).strip() == "understreget" and "underline" in ln.decor
    new = b.text.replace("understreget", "understregede")
    te.replace_block_text(pdf, b, new)
    page = pdf.page(0)
    word = page.search_for("understregede")[0]
    strokes = [d["rect"] for d in page.get_drawings() if d["items"][0][0] == "l"]
    assert len(strokes) == 1                                  # old stroke removed, new one drawn
    r = strokes[0]
    assert abs(r.x0 - word.x0) < 1.5 and abs(r.x1 - word.x1) < 1.5   # new length
    b2 = _block(pdf, "Vigtigt")
    assert "".join(c for c, st in zip(b2.lines[0].text, b2.lines[0].char_styles) if st.underline) == "understregede"


def test_remove_underline():
    pdf = _doc(underline=True)
    b = _block(pdf, "Vigtigt")
    i = b.text.index("understreget")
    te.replace_block_text(pdf, b, b.text, formats=[(i, i + 12, {"underline": False})])
    assert not [d for d in pdf.page(0).get_drawings() if d["items"][0][0] == "l"]


def test_center_and_right_align_existing_paragraph():
    pdf = _doc()
    b = _block(pdf, "Andet afsnit")
    te.replace_block_text(pdf, b, b.text, align="center")
    r = pdf.page(0).search_for("Andet afsnit lige under.")[0]
    frame = (72, 400)  # the paragraph above sets the column
    assert abs((r.x0 + r.x1) / 2 - (frame[0] + frame[1]) / 2) < 25
    b = _block(pdf, "Selskabet")
    te.replace_block_text(pdf, b, b.text, align="right")
    rights = [ln.extent[1] for ln in _block(pdf, "Selskabet").lines]
    assert max(rights) - min(rights) < 1.0


def test_justify_paragraph():
    pdf = _doc()
    b = _block(pdf, "Selskabet")
    te.replace_block_text(pdf, b, b.text, align="justify")
    lines = _block(pdf, "Selskabet").lines
    ends = [ln.extent[1] for ln in lines[:-1]]
    assert len(lines) >= 3 and max(ends) - min(ends) < 1.0
    assert lines[-1].extent[1] < max(ends) - 10       # the last line is not stretched


def test_line_spacing_and_reflow_below():
    pdf = _doc()
    b = _block(pdf, "Selskabet")
    before = _baselines(pdf, "Andet afsnit")[0]
    res = te.replace_block_text(pdf, b, b.text, line_spacing=2.0)
    lines = _block(pdf, "Selskabet").lines
    gaps = [lines[i + 1].raw_origin[1] - lines[i].raw_origin[1] for i in range(len(lines) - 1)]
    assert all(abs(g - 22.0) < 0.6 for g in gaps)
    assert res.moved == 2 and res.overflow == 0.0
    after = _baselines(pdf, "Andet afsnit")[0]
    assert after > before + 10                               # moved down with the paragraph
    assert _baselines(pdf, "Sidefod")[0] == pytest.approx(800, abs=0.5)   # footer stays


def test_growing_paragraph_pushes_text_down_and_shrinking_pulls_it_up():
    pdf = _doc()
    b = _block(pdf, "Selskabet")
    y0 = _baselines(pdf, "Andet afsnit")[0]
    te.replace_block_text(pdf, b, b.text + " " + BODY)
    y1 = _baselines(pdf, "Andet afsnit")[0]
    last = max(ln.raw_origin[1] for ln in _block(pdf, "Selskabet").lines)
    assert y1 > y0 and y1 > last + 11                      # no overlap
    b = _block(pdf, "Selskabet")
    te.replace_block_text(pdf, b, "Kort.")
    y2 = _baselines(pdf, "Andet afsnit")[0]
    assert y2 < y0


def test_growing_into_a_drawing_reports_overflow_and_fit_scale_fixes_it():
    pdf = _doc(image_below=True)
    b = _block(pdf, "Selskabet")
    longer = b.text + " " + BODY + " " + BODY
    y0 = _baselines(pdf, "Andet afsnit")[0]
    scale = te.fit_scale(pdf.doc, b, longer)
    assert 0.5 <= scale < 1.0
    res = te.replace_block_text(pdf, b, longer)
    assert res.overflow > 0 and res.moved == 0
    assert _baselines(pdf, "Andet afsnit")[0] == pytest.approx(y0, abs=0.1)   # nothing moved
    pdf.undo()
    b = _block(pdf, "Selskabet")
    st = b.style
    from dataclasses import replace

    res = te.replace_block_text(pdf, b, longer, replace(st, size=round(st.size * scale, 2)))
    assert res.overflow == 0.0


def test_missing_glyphs_report_the_substitute_font():
    pdf = _doc()
    b = _block(pdf, "Andet afsnit")
    res = te.replace_block_text(pdf, b, b.text + " 漢字")
    assert res.substitutions and res.substitutions[0][0].lower().startswith("helv")


def test_layout_info_reports_line_spacing():
    pdf = _doc()
    info = te.layout_info(pdf.doc, _block(pdf, "Selskabet"))
    assert 1.0 < info.line_spacing < 1.6


@pytest.mark.parametrize("rotation", [90, 270])
def test_underline_on_rotated_page(rotation):
    pdf = _doc(rotation=rotation)
    b = _block(pdf, "Andet afsnit")
    te.replace_block_text(pdf, b, b.text, formats=[(0, 5, {"underline": True})])
    page = pdf.page(0)
    word = page.search_for("Andet")[0]           # unrotated page space, like the drawings
    strokes = [pymupdf.Rect(d["rect"]) for d in page.get_drawings()]
    assert any(word.x0 - 1 <= s.x0 and s.x1 <= word.x1 + 1 and word.y0 + 0.5 * word.height <= s.y0 <= word.y1 + 3
               for s in strokes)


def test_double_spaced_paragraph_stays_one_block_after_editing():
    pdf = _doc()
    b = _block(pdf, "Selskabet")
    n = len(b.lines)
    te.replace_block_text(pdf, b, b.text, line_spacing=2.4)
    again = _block(pdf, "Selskabet")
    assert len(again.lines) == n >= 3            # still one paragraph to edit
