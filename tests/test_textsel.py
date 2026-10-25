import pymupdf
import pytest

from pdfstudio.core import textsel as ts


def _doc(rotation=0):
    doc = pymupdf.open()
    p = doc.new_page(width=400, height=300)
    p.insert_text((50, 60), "Hello brave new world", fontname="helv", fontsize=12)
    p.insert_text((50, 76), "second line here", fontname="helv", fontsize=12)
    p.insert_text((50, 140), "Other para-", fontname="helv", fontsize=12)
    p.insert_text((50, 156), "graph text", fontname="helv", fontsize=12)
    if rotation:
        p.set_rotation(rotation)
    return doc


def _caret_at_char(pt, text):
    """Caret just before the first occurrence of ``text`` on the page."""
    full = "".join(g.c for g in pt.chars)
    return full.index(text)


def _mid(rect):
    return ((rect.x0 + rect.x1) / 2, (rect.y0 + rect.y1) / 2)


def test_chars_in_reading_order():
    pt = ts.page_text(_doc(), 0)
    assert "".join(g.c for g in pt.chars).startswith("Hello brave new worldsecond line here")
    assert len(pt.lines) == 4
    assert pt.text.startswith("Hello brave new world\nsecond line here")


def test_mid_word_selection_is_char_precise():
    pt = ts.page_text(_doc(), 0)
    i = _caret_at_char(pt, "rave")       # between "b" and "r"
    j = _caret_at_char(pt, "orld")       # between "w" and "o"
    # carets from points: left half of "r" and left half of "o"
    a = ts.hit(pt, (pt.chars[i].rect.x0 + 0.5, _mid(pt.chars[i].rect)[1]))
    b = ts.hit(pt, (pt.chars[j].rect.x0 + 0.5, _mid(pt.chars[j].rect)[1]))
    assert (a, b) == (i, j)
    assert pt.text_between(a, b) == "rave new w"
    rects = ts.selection_rects(pt, a, b)
    assert len(rects) == 1


def test_selection_across_lines_and_paragraphs():
    pt = ts.page_text(_doc(), 0)
    a = _caret_at_char(pt, "world")
    b = _caret_at_char(pt, "graph") + 5
    assert pt.text_between(a, b) == "world\nsecond line here\nOther para-\ngraph"
    assert pt.text_between(a, b, line_sep=" ").startswith("world second line here\nOther")
    assert len(ts.selection_rects(pt, b, a)) == 4  # order independent


def test_hit_in_margin_picks_the_line():
    pt = ts.page_text(_doc(), 0)
    y = _mid(pt.chars[0].rect)[1]
    assert ts.hit(pt, (5, y)) == 0                    # left margin: line start
    assert ts.hit(pt, (390, y)) == pt.lines[0].end    # right margin: line end
    assert ts.char_at(pt, (5, y)) is None
    assert ts.char_at(pt, _mid(pt.chars[0].rect)) == 0


def test_word_line_paragraph_bounds():
    pt = ts.page_text(_doc(), 0)
    i = _caret_at_char(pt, "brave") + 2
    a, b = ts.word_bounds(pt, i)
    assert pt.text_between(a, b) == "brave"
    a, b = ts.line_bounds(pt, i)
    assert pt.text_between(a, b) == "Hello brave new world"
    a, b = ts.paragraph_bounds(pt, _caret_at_char(pt, "Other"))
    assert pt.text_between(a, b, line_sep=" ") == "Other para- graph text"
    # hyphenated word continues on the next line
    a, b = ts.word_bounds(pt, _caret_at_char(pt, "para"))
    assert pt.text_between(a, b) == "para-\ngraph"


def test_quote_from_rects_rejoins_hyphenation():
    pt = ts.page_text(_doc(), 0)
    a = _caret_at_char(pt, "para")
    b = _caret_at_char(pt, "graph") + 5
    rects = ts.selection_rects(pt, a, b)
    assert ts.text_in_rects(pt, rects) == "paragraph"


@pytest.mark.parametrize("rotation", [90, 180, 270])
def test_rotated_pages(rotation):
    doc = _doc(rotation)
    pt = ts.page_text(doc, 0)
    i = _caret_at_char(pt, "brave")
    j = i + 5
    a = ts.hit(pt, _mid(pt.chars[i].rect))
    # hitting the middle of a char gives the caret before or after it
    assert a in (i, i + 1)
    b = ts.hit(pt, _mid(pt.chars[j - 1].rect))
    assert b in (j - 1, j)
    r = ts.selection_rects(pt, i, j)[0]
    # the rect lies inside the displayed page
    w, h = doc[0].rect.width, doc[0].rect.height
    assert 0 <= r.x0 < r.x1 <= w and 0 <= r.y0 < r.y1 <= h
    assert ts.text_in_rects(pt, [r]) == "brave"


def test_empty_page():
    doc = pymupdf.open()
    doc.new_page()
    pt = ts.page_text(doc, 0)
    assert len(pt) == 0 and ts.hit(pt, (10, 10)) is None
    assert ts.word_bounds(pt, 0) == (0, 0)
