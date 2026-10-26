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


# -- lists and indents (A10) ------------------------------------------------------------
ITEM2 = ("2. Andet punkt som er langt nok til at blive ombrudt over flere linjer, sådan som et tekstbehandlings"
         "program ville gøre det med en hængende indrykning")


def _wrap_item(text: str, x_text: float, right: float, size: float = 11) -> list[str]:
    """Break ``text`` like a word processor: a word goes on the next line
    only when it does not fit (continuation lines start at ``x_text``)."""
    lines, cur, x0 = [], "", 72.0
    for word in text.split():
        cand = f"{cur} {word}" if cur else word
        if cur and x0 + pymupdf.get_text_length(cand, "helv", size) > right:
            lines.append(cur)
            cur, x0 = word, x_text
        else:
            cur = cand
    return lines + [cur]


def _list_doc():
    doc = pymupdf.open()
    page = doc.new_page()
    # a body paragraph sets the column (72 … 520) like in a real document
    page.insert_textbox(pymupdf.Rect(72, 30, 520, 80), "Brødtekst der fylder hele spalten ud. " * 6,
                        fontname="helv", fontsize=11)
    y = 80
    page.insert_text((72, y), "Indledning til listen herunder.", fontname="helv", fontsize=11)
    y += 18
    page.insert_text((72, y), "1. Første punkt i listen", fontname="helv", fontsize=11)
    y += 14
    x_text = 72 + pymupdf.get_text_length("2. ", "helv", 11)
    for k, line in enumerate(_wrap_item(ITEM2, x_text, 520)):
        page.insert_text((72 if k == 0 else x_text, y), line, fontname="helv", fontsize=11)
        y += 14
    for it in ("3. Tredje punkt", "a) Bogstavpunkt"):
        page.insert_text((72, y), it, fontname="helv", fontsize=11)
        y += 14
    page.insert_text((72, y + 10), "Afsnit efter listen.", fontname="helv", fontsize=11)
    return PdfDocument(pymupdf.open("pdf", doc.tobytes()))


def test_numbered_items_are_separate_blocks():
    pdf = _list_doc()
    texts = [b.text for b in te.text_blocks(pdf.doc, 0)]
    assert "1. Første punkt i listen" in texts
    assert any(t.startswith("2. Andet") and t.replace("\n", " ") == ITEM2 for t in texts)
    assert "3. Tredje punkt" in texts and "a) Bogstavpunkt" in texts
    assert te.marker_end("12. Punkt") == 4 and te.marker_end("iv) x") == 4 and te.marker_end("Normal") == 0


def test_wrapped_list_item_keeps_its_hanging_indent():
    pdf = _list_doc()
    b = _block(pdf, "2.")
    longer = b.text.replace("\n", " ") + " Og så kommer der endnu mere tekst, som skal have sin egen linje i punktet."
    te.replace_block_text(pdf, b, longer)
    nb = _block(pdf, "2.")
    assert len(nb.lines) >= 3
    x_text = 72 + pymupdf.get_text_length("2. ", "helv", 11)
    assert all(abs(ln.extent[0] - x_text) < 0.6 for ln in nb.lines[1:])   # under the text, not the number
    below = _block(pdf, "3.")
    assert below.lines[0].raw_origin[1] > nb.lines[-1].raw_origin[1] + 10  # the next item moved down


def test_single_line_item_wraps_under_its_text():
    pdf = _list_doc()
    b = _block(pdf, "1.")
    te.replace_block_text(pdf, b, b.text + " med en tilføjelse som gør punktet så langt at det må ombrydes til en ny linje")
    nb = _block(pdf, "1.")
    assert len(nb.lines) == 2
    start_text = nb.lines[0].char_pos[3][0]   # "F" of "Første"
    assert abs(nb.lines[1].extent[0] - start_text) < 0.6


def _justified_doc(n_lines=4, lead=""):
    """A justified paragraph (word spacing via Tw) whose last line is full
    width too – it continues on the next page."""
    words = (lead + "Selskabet forventer en stigende indtjening i de kommende kvartaler takket mange nye kunder og "
             "markedet for gode losninger vokser fortsat i hele regionen hvor afdelingen har investeret "
             "betydeligt i digitalisering og effektivisering af produktionen samt leverandorer og projekter "
             "som skaber vaeksten i de naeste par aar for selskabet og dets medarbejdere i hele landet nu").split()
    doc = pymupdf.open()
    page = doc.new_page()
    page.insert_text((72, 60), "Rapport", fontname="helv", fontsize=11)   # registers /helv
    lines, line = [], []
    for w in words:
        if pymupdf.get_text_length(" ".join(line + [w]), "helv", 11) > 448 and line:
            lines.append(line)
            line = [w]
        else:
            line.append(w)
    ops = []
    for k, ln in enumerate(lines[:n_lines]):
        t = " ".join(ln)
        tw = (448 - pymupdf.get_text_length(t, "helv", 11)) / (len(ln) - 1)
        ops.append(f"BT /helv 11 Tf {tw:.3f} Tw 1 0 0 1 72 {page.rect.height - 100 - 15 * k} Tm ({t}) Tj ET")
    xref = page.get_contents()[0]
    doc.update_stream(xref, doc.xref_stream(xref) + ("\n" + "\n".join(ops) + "\n").encode())
    return PdfDocument(pymupdf.open("pdf", doc.tobytes()))


def test_full_last_line_of_justified_paragraph_stays_in_the_column():
    pdf = _justified_doc()
    b = te.text_blocks(pdf.doc, 0)[-1]
    assert all(abs(ln.bbox.x1 - 520) < 0.5 for ln in b.lines)
    te.replace_block_text(pdf, b, b.text + " ja")   # fits with normal spaces, not with the stretched ones
    blocks = te.text_blocks(pdf.doc, 0)
    assert len(blocks) == 2 and blocks[-1].text.endswith("i ja")
    assert all(ln.bbox.x1 <= 520.5 for ln in blocks[-1].lines)
    assert abs(blocks[-1].lines[-1].bbox.x1 - 520) < 0.5   # still justified to the margin


def test_rewritten_justified_line_stays_one_line_with_real_spaces():
    # long words: the first line's word gaps are stretched to ~10 pt
    pdf = _justified_doc(lead="effektiviseringen digitaliseringen regnskabsaaret produktionsanlaeggene kunderne og "
                              "leverandoerkontrakterne ")
    b = te.text_blocks(pdf.doc, 0)[-1]
    assert b.text.split("\n")[0].endswith("kunderne og")
    new = b.text.replace("digitaliseringen", "digitaliseringer", 1)   # the first line is re-justified
    te.replace_block_text(pdf, b, new)
    blocks = te.text_blocks(pdf.doc, 0)
    assert len(blocks) == 2 and blocks[-1].text == new
    first = blocks[-1].lines[0]
    assert abs(first.bbox.x1 - 520) < 0.5            # still justified
    raw = pdf.page(0).get_text()
    assert "effektiviseringen digitaliseringer regnskabsaaret" in raw   # one real space per gap


def test_table_row_set_tight_above_a_paragraph_is_not_part_of_it():
    doc = pymupdf.open()
    page = doc.new_page()
    for x, t in ((72, "Post"), (250, "2025"), (480, "2026")):
        page.insert_text((x, 100), t, fontname="helv", fontsize=11)
    for x, t in ((72, "Omsætning"), (250, "183,8"), (480, "619,1")):
        page.insert_text((x, 114), t, fontname="helv", fontsize=11)
    page.insert_textbox(pymupdf.Rect(72, 117, 520, 200), "Brødtekst lige under tabellen, som fylder spalten. " * 4,
                        fontname="helv", fontsize=11)
    pdf = PdfDocument(pymupdf.open("pdf", doc.tobytes()))
    texts = [b.text for b in te.text_blocks(pdf.doc, 0)]
    para = next(t for t in texts if "Brødtekst" in t)
    assert para.startswith("Brødtekst") and "Omsætning" not in para and "619,1" not in para


def test_editing_first_line_of_item_keeps_items_apart():
    pdf = _list_doc()
    one = _block(pdf, "1.")
    te.replace_block_text(pdf, one, one.text + " med en tilføjelse som gør punktet så langt at det må ombrydes til en ny linje")
    two = _block(pdf, "2.")
    lines = two.text.split("\n")
    lines[0] = lines[0].replace("Andet", "Ændret")
    te.replace_block_text(pdf, two, "\n".join(lines))
    texts = [b.text.replace("\n", " ") for b in te.text_blocks(pdf.doc, 0)]
    assert any(t.startswith("1. Første punkt") and t.endswith("til en ny linje") for t in texts)
    assert ITEM2.replace("Andet", "Ændret") in texts


def test_group_with_a_foreign_line_between_its_lines_is_split():
    # MuPDF's block after an edit rewrote the first line of the second item:
    # [item 1 line 2, item 2 line 2, item 2 line 3] with item 2 line 1 elsewhere
    doc = pymupdf.open()
    page = doc.new_page()
    for y, t in ((100, "slutningen af det første punkt"), (114, "Andet punkt begynder her"),
                 (128, "og fortsætter her"), (142, "og slutter her.")):
        page.insert_text((98, y), t, fontname="helv", fontsize=11)
    lines = [ln for b in te.text_blocks(pymupdf.open("pdf", doc.tobytes()), 0, merge=False) for ln in b.lines]
    by_text = {ln.text: ln for ln in lines}
    a, first, b, c = (by_text[t] for t in ("slutningen af det første punkt", "Andet punkt begynder her",
                                          "og fortsætter her", "og slutter her."))
    out = te._split_interleaved([[a, b, c], [first]])
    assert [[ln.text for ln in g] for g in out] == [[a.text], [b.text, c.text], [first.text]]


def test_warm_page_prepares_blocks_and_fonts():
    pdf = _justified_doc()
    te.warm_page(pdf.doc, 0)
    assert getattr(pdf.doc, "_folio_embedded", None) is not None   # fonts proven ahead of time
    assert te.text_blocks(pdf.doc, 0)[0] is te.text_blocks(pdf.doc, 0)[0]   # served from the cache
