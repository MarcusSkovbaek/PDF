"""Core: vector graphics in the page content (``pdfstudio.core.graphics``)."""

from __future__ import annotations

import re
import time

import pymupdf
import pytest

from pdfstudio.core import PdfDocument
from pdfstudio.core import graphics as gr
from pdfstudio.core import objects as ob
from tests import test_objects as to
from tests.conftest import make_png

build = to.build
RED = (255, 0, 0)
GREEN = (0, 255, 0)
BLUE = (0, 0, 255)
WHITE = (255, 255, 255)

TEXT = b"BT /F1 12 Tf 72 760 Td (Kvartalsrapport) Tj ET\n"
# a red box with a blue 2 pt border, drawn LibreOffice-style: bare fill, then
# the stroke in its own block with state set in between
BOX = (b"1 0 0 rg\n100 600 120 80 re f\n"
       b"0 0 1 RG\nq 2 w 0 J 1 j\n100 600 120 80 re S\nQ\n")
LINE = b"q 0 g 0 0 0 RG 3 w 300 300 m 450 300 l S Q\n"


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------
def build_pages(contents: list[bytes], *, width=595, height=842) -> PdfDocument:
    """A document with one page per content stream (font /F1 = Helvetica)."""
    doc = pymupdf.open()
    for content in contents:
        page = doc.new_page(width=width, height=height)
        font = page.insert_font(fontname="helv")
        doc.xref_set_key(page.xref, "Resources", f"<</Font<</F1 {font} 0 R>>>>")
        cx = doc.get_new_xref()
        doc.update_object(cx, "<<>>")
        doc.update_stream(cx, content)
        doc.xref_set_key(page.xref, "Contents", f"{cx} 0 R")
    data = doc.tobytes()
    doc.close()
    return PdfDocument.from_bytes(data)


def render(doc, page=0, zoom=1.0) -> pymupdf.Pixmap:
    return doc[page].get_pixmap(matrix=pymupdf.Matrix(zoom, zoom), annots=False)


def px(doc, x, y, page=0) -> tuple:
    pix = render(doc, page)
    return tuple(pix.pixel(int(x), int(y))[:3])


def near(c, rgb, tol=40) -> bool:
    return all(abs(a - b) <= tol for a, b in zip(c, rgb))


def diff_outside(a: pymupdf.Pixmap, b: pymupdf.Pixmap, boxes, pad=2) -> int:
    """Number of differing pixels outside the display rects ``boxes``."""
    assert (a.width, a.height) == (b.width, b.height)
    sa, sb, n = a.samples, b.samples, a.n
    bad = 0
    rects = [pymupdf.Rect(r) + (-pad, -pad, pad, pad) for r in boxes]
    for y in range(a.height):
        row = y * a.stride
        if sa[row:row + a.width * n] == sb[row:row + a.width * n]:
            continue
        for x in range(a.width):
            o = row + x * n
            if sa[o:o + n] != sb[o:o + n] and not any(r.x0 <= x <= r.x1 and r.y0 <= y <= r.y1 for r in rects):
                bad += 1
    return bad


def drawings(doc, page=0):
    pg = doc[page]
    return [(pymupdf.Rect(d["rect"]) * pg.rotation_matrix, d.get("width"), d.get("color"), d.get("fill"))
            for d in pg.get_drawings()]


def assert_rect(a, b, tol=0.6) -> None:
    a, b = pymupdf.Rect(a), pymupdf.Rect(b)
    assert all(abs(x - y) <= tol for x, y in zip(a, b)), (a, b)


def only(doc, page=0, **kw) -> gr.GraphicObject:
    objs = gr.list_graphics(doc, page, **kw)
    assert len(objs) == 1, objs
    return objs[0]


def contents(pdf, page=0) -> bytes:
    return pdf.doc[page].read_contents()


# ---------------------------------------------------------------------------
# listing
# ---------------------------------------------------------------------------
def test_list_basic_shapes_match_mupdf():
    doc = pymupdf.open()
    page = doc.new_page(width=595, height=842)
    sh = page.new_shape()
    sh.draw_rect(pymupdf.Rect(50, 50, 150, 120))
    sh.finish(color=(0, 0, 1), fill=(1, 0, 0), width=2)                 # fill + stroke (one B)
    sh.draw_oval(pymupdf.Rect(250, 50, 350, 120))
    sh.finish(color=None, fill=(0, 1, 0))                                # fill only
    sh.draw_line((50, 300), (200, 300))
    sh.finish(color=(0, 0, 0), width=1)                                  # stroke only
    sh.draw_bezier((300, 300), (330, 250), (370, 350), (400, 300))
    sh.finish(color=(1, 0, 1), width=1.5)
    sh.draw_polyline([(50, 500), (120, 450), (190, 500), (120, 560)])
    sh.finish(color=(0, 0, 0), fill=(1, 1, 0), closePath=True, width=1)
    sh.commit()
    pdf = PdfDocument.from_bytes(doc.tobytes())
    objs = gr.list_graphics(pdf.doc, 0)
    assert [o.kind for o in objs] == ["shape", "shape", "line", "shape", "shape"]
    ref = drawings(pdf.doc)
    assert len(ref) == len(objs)
    for o, (r, w, color, fill) in zip(objs, ref):
        hw = (w or 0) / 2 if color else 0
        if o is objs[3]:            # MuPDF uses the control points; we flatten the curve (tighter)
            assert (r + (-hw, -hw, hw, hw)).contains(o.rect) and o.rect.height < r.height * 0.7
        else:
            assert_rect(o.rect, r + (-hw, -hw, hw, hw), tol=0.8)
        assert (o.stroke is None) == (color is None)
        assert (o.fill is None) == (fill is None)
        if color:
            assert o.stroke == pytest.approx(tuple(color), abs=0.01)
            assert o.width == pytest.approx(w)
        if fill:
            assert o.fill == pytest.approx(tuple(fill), abs=0.01)
        assert o.editable and o.selectable and not o.in_form
    rect, oval, line, curve, poly = objs
    assert rect.items[0][0] == "re"
    assert sum(1 for i in oval.items if i[0] == "c") == 4
    assert line.items == [("l", pymupdf.Point(50, 300), pymupdf.Point(200, 300))]
    assert curve.items[0][0] == "c"
    assert [i[0] for i in poly.items] == ["l", "l", "l", "l"]
    assert rect.key == (0, rect.paths, tuple(round(v, 1) for v in rect.rect))


def test_paint_operators_and_no_paint():
    content = (b"q 0 0 1 RG 0.5 g 10 10 50 50 re B Q\n"        # fill+stroke
               b"q 0 0 1 RG 100 10 50 50 re s Q\n"              # closed stroke
               b"q 1 0 0 rg 200 10 m 250 10 l 225 60 l f* Q\n"  # even-odd fill
               b"q 300 10 50 50 re n Q\n"                       # nothing painted
               b"q 0.2 0.4 0.6 0.1 k 400 10 50 50 re F Q\n"     # cmyk fill, F
               b"q /DeviceRGB cs 0 1 0 sc 480 10 40 40 re f Q\n")
    pdf, _ = build(content)
    objs = gr.list_graphics(pdf.doc, 0)
    assert len(objs) == 5
    b, s, fs, k, sc = objs
    assert b.stroke == (0, 0, 1) and b.fill == (0.5, 0.5, 0.5)
    assert s.stroke == (0, 0, 1) and s.fill is None and s.width == 1
    assert fs.fill == (1, 0, 0) and fs.stroke is None and fs.width == 0
    assert k.fill == pytest.approx((0.7, 0.5, 0.3))
    assert sc.fill == (0, 1, 0)


def test_strings_comments_and_inline_images_are_skipped():
    raw = bytes([0x45, 0x49, 0x20, 255, 0, 0, 0, 255, 0, 0, 0, 255])   # "EI " inside the image data
    content = (b"BT /F1 9 Tf 10 10 Td (re f 1 0 0 rg 0 0 9 9 re f \\) S) Tj [(m) -20 (l)] TJ <0BDC0A> Tj ET"
               b" % 0 0 100 100 re f\n"
               b"/Span <</ActualText (q 0 0 5 5 re f)>> BDC EMC\n"
               b"q 100 0 0 100 300 300 cm BI /W 2 /H 2 /CS /RGB /BPC 8 ID " + raw + b"\nEI Q\n"
               + LINE)
    pdf, _ = build(content)
    (line,) = gr.list_graphics(pdf.doc, 0)
    assert line.kind == "line" and line.paths == (0,)
    assert_rect(line.rect, (298.5, 540.5, 451.5, 543.5))
    moved = gr.move_graphics(pdf, 0, [line], 0, 10)
    assert_rect(moved[0].rect, line.rect + (0, 10, 0, 10))
    assert contents(pdf).startswith(content[:-len(LINE)])
    assert len(ob.list_images(pdf.doc, 0)) == 1


# ---------------------------------------------------------------------------
# grouping
# ---------------------------------------------------------------------------
def test_fill_stroke_pair_is_one_group():
    pdf, _ = build(TEXT + BOX)
    (box,) = gr.list_graphics(pdf.doc, 0)
    assert box.paths == (0, 1) and box.kind == "shape"
    assert box.fill == (1, 0, 0) and box.stroke == (0, 0, 1) and box.width == 2
    assert_rect(box.rect, (99, 161, 221, 243))


def test_box_with_border_arrow_and_separate_shapes():
    content = (BOX
               # an arrow: shaft + filled head touching its end
               + b"q 0 0 0 RG 1 w 300 500 m 400 500 l S Q\n"
               b"q 0 g 400 495 m 412 500 l 400 505 l h f Q\n"
               # two shapes far apart
               b"q 0 1 0 rg 50 50 30 30 re f Q\nq 0 1 0 rg 500 50 30 30 re f Q\n")
    pdf, _ = build(content)
    objs = gr.list_graphics(pdf.doc, 0)
    assert [o.paths for o in objs] == [(0, 1), (2, 3), (4,), (5,)]
    assert objs[1].kind == "shape"          # line + head is not a plain line


def test_text_breaks_grouping_but_identical_fill_and_stroke_join():
    content = (b"q 1 0 0 rg 100 100 50 50 re f Q\n" + TEXT + b"q 0 0 1 rg 150 100 50 50 re f Q\n"
               # Word style: fill, a text, then the border of the same rect
               + b"q 0 1 0 rg 300 300 80 40 re f Q\n" + TEXT + b"q 0 G 300 300 80 40 re S Q\n")
    pdf, _ = build(content)
    objs = gr.list_graphics(pdf.doc, 0)
    assert [o.paths for o in objs] == [(0,), (1,), (2, 3)]


# ---------------------------------------------------------------------------
# classification (O5)
# ---------------------------------------------------------------------------
def test_page_background_is_not_selectable():
    content = b"q 0.95 0.95 0.9 rg 0 0 595 842 re f Q\n" + TEXT + BOX
    pdf, _ = build(content)
    (box,) = gr.list_graphics(pdf.doc, 0)
    assert box.paths == (1, 2)
    assert gr.graphic_at(pdf.doc, 0, (400, 400)) is None           # body area: never the background
    bg = gr.graphic_at(pdf.doc, 0, (400, 400), alt=True)
    assert bg.kind == "background" and not bg.selectable and bg.paths == (0,)
    every = gr.list_graphics(pdf.doc, 0, all_items=True)
    assert [o.kind for o in every] == ["background", "shape"]
    # a page frame (stroke only) is a background too
    pdf2, _ = build(b"q 0 G 10 10 575 822 re S Q\n" + BOX)
    assert [o.kind for o in gr.list_graphics(pdf2.doc, 0, all_items=True)] == ["background", "shape"]


def test_header_rule_repeated_on_pages_is_template():
    rule = b"q 0.2 0.4 0.3 RG 0.75 w 56.7 788.5 m 538.5 788.5 l S Q\n"
    other = b"q 0 0 1 RG 0.75 w 56.7 60 m 300 60 l S Q\n"
    pdf = build_pages([rule + TEXT + BOX, rule + TEXT, rule + other])
    for p in range(3):
        sel = gr.list_graphics(pdf.doc, p)
        assert all(o.kind != "template" for o in sel)
        every = gr.list_graphics(pdf.doc, p, all_items=True)
        assert every[0].kind == "template" and every[0].paths == (0,) and not every[0].selectable
    assert [o.kind for o in gr.list_graphics(pdf.doc, 0)] == ["shape"]
    assert [o.kind for o in gr.list_graphics(pdf.doc, 2)] == ["line"]     # a unique footer line stays
    assert gr.graphic_at(pdf.doc, 1, (300, 842 - 788.5)) is None
    assert gr.graphic_at(pdf.doc, 1, (300, 842 - 788.5), alt=True).kind == "template"


def test_template_from_shared_form():
    form = b"q 0 0 1 rg 0 812 595 30 re f Q"
    pdf, x = build(b"q /Fm1 Do Q\n" + TEXT, forms={"Fm1": (form, {})}, pages=2)
    doc = pdf.doc
    doc.xref_set_key(doc.page_xref(1), "Resources", doc.xref_get_key(doc.page_xref(0), "Resources")[1])
    (o,) = gr.list_graphics(doc, 0, all_items=True)
    assert o.kind == "template" and o.in_form
    assert gr.list_graphics(doc, 0) == []


def table(x0=60, y0=400, cols=3, rows=3, w=80, h=20, zebra=True) -> bytes:
    out = []
    if zebra:
        for r in range(rows):
            if r % 2 == 0:
                out.append(f"q 0.9 0.95 0.9 rg {x0} {y0 + r * h} {cols * w} {h} re f Q\n")
    for r in range(rows + 1):
        out.append(f"q 0 G 0.5 w {x0} {y0 + r * h} m {x0 + cols * w} {y0 + r * h} l S Q\n")
    for c in range(cols + 1):
        out.append(f"q 0 G 0.5 w {x0 + c * w} {y0} m {x0 + c * w} {y0 + rows * h} l S Q\n")
    return "".join(out).encode()


def test_table_grid_is_not_selectable():
    pdf, _ = build(TEXT + table() + BOX)
    sel = gr.list_graphics(pdf.doc, 0)
    assert [o.kind for o in sel] == ["shape"] and sel[0].fill == (1, 0, 0)
    every = gr.list_graphics(pdf.doc, 0, all_items=True)
    kinds = [o.kind for o in every]
    assert kinds.count("table") == 2 + 4 + 4 and kinds.count("shape") == 1
    # a click on a rule or a cell does not pick the table; Alt does, one rule at a time
    assert gr.graphic_at(pdf.doc, 0, (100, 842 - 420)) is None
    assert gr.graphic_at(pdf.doc, 0, (100, 842 - 420), alt=True).kind == "table"
    rule = gr.graphic_at(pdf.doc, 0, (140, 842 - 400), alt=True)
    assert rule.kind == "table" and len(rule.paths) == 1
    # graphics_in without Alt ignores the table, with Alt lists its parts
    assert gr.graphics_in(pdf.doc, 0, (50, 300, 400, 460)) == []
    assert len(gr.graphics_in(pdf.doc, 0, (50, 300, 400, 460), alt=True)) == 10


def test_box_made_of_four_lines_is_not_a_table():
    lines = (b"q 0 G 100 100 m 200 100 l S Q\nq 0 G 200 100 m 200 150 l S Q\n"
             b"q 0 G 200 150 m 100 150 l S Q\nq 0 G 100 150 m 100 100 l S Q\n")
    pdf, _ = build(lines)
    (frame,) = gr.list_graphics(pdf.doc, 0)
    assert frame.paths == (0, 1, 2, 3) and frame.kind == "line"


# ---------------------------------------------------------------------------
# hit-testing
# ---------------------------------------------------------------------------
def test_hit_testing_fill_stroke_and_topmost():
    content = (b"q 1 0 0 rg 100 100 200 200 re f Q\n" + TEXT
               + b"q 0 0 1 rg 150 150 50 50 re f Q\n" + TEXT
               + b"q 0 G 2 w 400 100 100 100 re S Q\n" + TEXT + LINE)
    pdf, _ = build(content)
    big, small, frame, line = gr.list_graphics(pdf.doc, 0)
    assert gr.graphic_at(pdf.doc, 0, (120, 842 - 120)).paths == big.paths
    assert gr.graphic_at(pdf.doc, 0, (175, 842 - 175)).paths == small.paths          # topmost wins
    # stroke only: the interior is empty, the stroke is hit within w/2 + tolerance
    assert gr.graphic_at(pdf.doc, 0, (450, 842 - 150)) is None
    assert gr.graphic_at(pdf.doc, 0, (402.5, 842 - 150)).paths == frame.paths
    assert gr.graphic_at(pdf.doc, 0, (404.5, 842 - 150)) is None
    assert gr.graphic_at(pdf.doc, 0, (404.5, 842 - 150), tolerance=4).paths == frame.paths
    assert gr.graphic_at(pdf.doc, 0, (375, 842 - 303)).paths == line.paths
    assert gr.graphic_at(pdf.doc, 0, (375, 842 - 306)) is None
    assert gr.graphic_at(pdf.doc, 0, (460, 842 - 300)) is None                       # past the end


def test_hit_testing_even_odd_hole():
    ring = b"q 0 g 100 100 200 200 re 150 150 100 100 re f* Q\n"
    pdf, _ = build(ring)
    assert gr.graphic_at(pdf.doc, 0, (120, 842 - 120)) is not None
    assert gr.graphic_at(pdf.doc, 0, (200, 842 - 200), tolerance=0) is None          # the hole


def test_alt_deep_select_picks_one_path_of_a_group():
    # BOX = red fill (path 0) + blue 2 pt border (path 1) – one group; an arrow
    # (shaft 2 + head 3); then a lone square (4)
    content = (BOX + b"q 0 0 0 RG 1 w 300 500 m 400 500 l S Q\n"
               b"q 0 g 400 495 m 412 500 l 400 505 l h f Q\n"
               b"q 0 1 0 rg 50 50 30 30 re f Q\n")
    pdf, _ = build(content)
    box, arrow, square = gr.list_graphics(pdf.doc, 0)
    assert box.paths == (0, 1) and arrow.paths == (2, 3)
    # a plain click picks the group, Alt the topmost path under the point
    assert gr.graphic_at(pdf.doc, 0, (160, 842 - 640)).paths == (0, 1)
    fill = gr.graphic_at(pdf.doc, 0, (160, 842 - 640), alt=True)
    assert fill.paths == (0,) and fill.fill == (1, 0, 0) and fill.stroke is None and fill.selectable
    border = gr.graphic_at(pdf.doc, 0, (100, 842 - 640), alt=True)       # on the border: drawn on top
    assert border.paths == (1,) and border.stroke == (0, 0, 1) and border.fill is None
    assert_rect(border.rect, box.rect)
    head = gr.graphic_at(pdf.doc, 0, (404, 842 - 500), alt=True)
    assert head.paths == (3,)
    assert gr.graphic_at(pdf.doc, 0, (65, 842 - 65), alt=True).paths == square.paths
    assert gr.graphic_at(pdf.doc, 0, (500, 842 - 700), alt=True) is None
    # the single path is a full object: it can be found again and edited alone
    assert gr.find_graphic(pdf.doc, 0, border).paths == (1,)
    (moved,) = gr.move_graphics(pdf, 0, border, 0, 50)
    assert moved.paths == (1,)
    assert_rect(moved.rect, box.rect + (0, 50, 0, 50))
    fill_now = gr.find_graphic(pdf.doc, 0, fill)
    assert fill_now is not None
    assert_rect(fill_now.rect, fill.rect)                                 # the fill stayed
    # moved away from its fill the border is a shape of its own; the listing
    # of the untouched page still groups them (deep select changes no listing)
    assert [o.paths for o in gr.list_graphics(pdf.doc, 0)][:2] == [(0,), (1,)]
    pdf.undo()
    assert [o.paths for o in gr.list_graphics(pdf.doc, 0)] == [(0, 1), (2, 3), (4,)]


def test_alt_deep_select_background_and_path_only_form():
    form = b"q 1 0 0 rg 0 0 100 60 re f Q q 0 0 1 RG 2 w 0 0 100 60 re S Q"
    content = b"q 0.9 0.9 0.9 rg 0 0 595 842 re f Q\nq 1 0 0 1 200 300 cm /Fm1 Do Q\n"
    pdf, _ = build(content, forms={"Fm1": (form, {})})
    bg = gr.graphic_at(pdf.doc, 0, (50, 50), alt=True)
    assert bg.kind == "background" and bg.paths == (0,) and not bg.selectable
    # inside a path-only form the form invocation is the unit
    f = gr.graphic_at(pdf.doc, 0, (250, 842 - 330), alt=True)
    assert f.paths == (1, 2) and f.in_form and f.editable


# ---------------------------------------------------------------------------
# move
# ---------------------------------------------------------------------------
def test_move_inserts_one_cm_and_changes_nothing_else():
    content = TEXT + b"q 0 1 0 rg 50 50 40 40 re f Q\n" + BOX + LINE
    pdf, _ = build(content)
    before_pix = render(pdf.doc)
    before = contents(pdf)
    box = gr.list_graphics(pdf.doc, 0)[1]
    (moved,) = gr.move_graphics(pdf, 0, [box], 150, 40)
    assert_rect(moved.rect, box.rect + (150, 40, 150, 40))
    after = contents(pdf)
    # byte level: the bare fill got one q/cm/Q wrap, the stroke block one cm
    want = before.replace(b"100 600 120 80 re f\n", b"\nq 1 0 0 1 150 -40 cm\n100 600 120 80 re f\nQ\n\n")
    want = want.replace(b"q 2 w", b"q\n1 0 0 1 150 -40 cm\n 2 w")
    assert after == want
    # pixels: identical outside the old and the new box
    after_pix = render(pdf.doc)
    assert diff_outside(before_pix, after_pix, [box.rect, moved.rect]) == 0
    assert near(px(pdf.doc, 160 + 150, 842 - 640 + 40), RED)
    assert near(px(pdf.doc, 160, 842 - 640), WHITE)
    assert pdf.history.undo_label == "Flyt grafik"
    pdf.undo()
    assert contents(pdf) == before


def test_move_rewrites_existing_cm_in_exclusive_block():
    content = TEXT + b"q 1 0 0 1 0 0 cm 0 0 1 rg 100 100 50 50 re f Q\n"
    pdf, _ = build(content)
    o = only(pdf.doc)
    o = gr.move_graphics(pdf, 0, o, 10, -5)[0]
    assert contents(pdf) == TEXT + b"q 1 0 0 1 10 5 cm 0 0 1 rg 100 100 50 50 re f Q\n"


def test_repeated_moves_do_not_grow_the_stream():
    pdf, _ = build(TEXT + BOX + LINE)
    box = gr.list_graphics(pdf.doc, 0)[0]
    box = gr.move_graphics(pdf, 0, [box], 1, 1)[0]
    size = len(contents(pdf))
    start = box.rect
    for k in range(25):
        box = gr.move_graphics(pdf, 0, [box], 3 if k % 2 else -2, 1.5)[0]
    assert len(contents(pdf)) <= size + 12
    assert contents(pdf).count(b" cm") == 2
    assert_rect(box.rect, start + (10, 37.5, 10, 37.5))
    assert len(pdf.history._undo) == 26


def test_nudge_burst_is_one_undo_step():
    pdf, _ = build(TEXT + BOX)
    before = contents(pdf)
    box = only(pdf.doc)
    box = gr.move_graphics(pdf, 0, box, 1, 0)[0]
    for _ in range(4):
        box = gr.move_graphics(pdf, 0, [box], 1, 0, merge=True)[0]
    assert len(pdf.history._undo) == 1
    pdf.undo()
    assert contents(pdf) == before
    gr.delete_graphics(pdf, 0, only(pdf.doc))
    assert pdf.history.undo_label == "Slet grafik"


def test_move_several_objects_one_step():
    pdf, _ = build(TEXT + BOX + TEXT + LINE)
    objs = gr.list_graphics(pdf.doc, 0)
    moved = gr.move_graphics(pdf, 0, objs, -20, 30)
    assert [o.paths for o in moved] == [o.paths for o in objs]
    for a, b in zip(objs, moved):
        assert_rect(b.rect, a.rect + (-20, 30, -20, 30))
    assert len(pdf.history._undo) == 1


def test_uncovered_path_with_state_after_is_wrapped():
    # the cm/colour set after the path stay in force for the green square
    content = b"q 2 0 0 2 0 0 cm 1 0 0 rg 20 300 30 20 re f 0 1 0 rg 0 0 10 10 re f Q\n"
    pdf, _ = build(content)
    red, green = gr.list_graphics(pdf.doc, 0)
    gr.move_graphics(pdf, 0, red, 50, 0)
    r2, g2 = gr.list_graphics(pdf.doc, 0)
    assert_rect(r2.rect, red.rect + (50, 0, 50, 0))
    assert_rect(g2.rect, green.rect) and g2.fill == (0, 1, 0)


# ---------------------------------------------------------------------------
# rotated pages, cropbox
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("rotation", [0, 90, 180, 270])
def test_rotated_pages_and_cropbox(rotation):
    pdf, _ = build(TEXT + BOX + LINE, rotation=rotation, cropbox=(30, 40, 560, 800))
    objs = gr.list_graphics(pdf.doc, 0)
    ref = drawings(pdf.doc)
    assert len(objs) == 2
    assert_rect(objs[0].rect, ref[0][0] + (-1, -1, 1, 1))         # fill + 2 pt stroke
    assert_rect(objs[1].rect, ref[1][0] + (-1.5, -1.5, 1.5, 1.5))
    hit = gr.graphic_at(pdf.doc, 0, (objs[0].rect.x0 + 10, objs[0].rect.y0 + 10))
    assert hit.paths == objs[0].paths
    before_pix = render(pdf.doc)
    moved = gr.move_graphics(pdf, 0, objs[0], 25, -40)[0]
    assert_rect(moved.rect, objs[0].rect + (25, -40, 25, -40))
    assert_rect(drawings(pdf.doc)[0][0] + (-1, -1, 1, 1), moved.rect)
    assert diff_outside(before_pix, render(pdf.doc), [objs[0].rect, moved.rect]) == 0
    big = pymupdf.Rect(moved.rect.x0, moved.rect.y0, moved.rect.x1 + 60, moved.rect.y1 + 30)
    scaled = gr.scale_graphics(pdf, 0, [moved], moved.rect, big)[0]
    assert_rect(scaled.rect, big, tol=0.05)
    assert all(w == pytest.approx(2) for _r, w, c, _f in drawings(pdf.doc)[:1] if c)
    line = gr.list_graphics(pdf.doc, 0)[1]
    styled = gr.set_graphic_style(pdf, 0, [line], width=6)[0]
    assert styled.width == pytest.approx(6)
    assert_rect(styled.rect, line.rect + (-1.5, -1.5, 1.5, 1.5))


# ---------------------------------------------------------------------------
# scale
# ---------------------------------------------------------------------------
def test_scale_keeps_stroke_width():
    pdf, _ = build(TEXT + BOX + b"q 0 0 1 RG 4 w 300 300 m 350 380 400 380 450 300 c S Q\n")
    box, curve = gr.list_graphics(pdf.doc, 0)
    widths = [w for _r, w, c, _f in drawings(pdf.doc) if c]
    target = pymupdf.Rect(box.rect.x0, box.rect.y0, box.rect.x1 + 120, box.rect.y1 + 40)
    (big,) = gr.scale_graphics(pdf, 0, [box], box.rect, target)
    assert_rect(big.rect, target, tol=0.01)
    assert [w for _r, w, c, _f in drawings(pdf.doc) if c] == widths
    assert b" cm" not in contents(pdf)                         # coordinates rewritten, no scaling cm
    assert pdf.history.undo_label == "Skalér grafik"
    t2 = pymupdf.Rect(curve.rect.x0 - 50, curve.rect.y0, curve.rect.x1, curve.rect.y1 + 100)
    (c2,) = gr.scale_graphics(pdf, 0, curve, curve.rect, t2)
    assert_rect(c2.rect, t2, tol=0.6)
    assert [w for _r, w, c, _f in drawings(pdf.doc) if c] == widths
    # keep_stroke=False scales the stroke with a cm
    (c3,) = gr.transform_graphics(pdf, 0, [c2], pymupdf.Matrix(2, 0, 0, 2, -300, -300), keep_stroke=False)
    assert drawings(pdf.doc)[-1][1] == pytest.approx(8)


def test_scale_rotated_user_space_rewrites_rect():
    # the path's own CTM rotates by 30°: an axis-aligned display scale is a
    # skew in user space, so "re" becomes m/l/l/l/h
    c, s = 0.8660254, 0.5
    content = f"q {c} {s} {-s} {c} 200 300 cm 0 0 1 RG 2 w 0 0 100 50 re S Q\n".encode()
    pdf, _ = build(content)
    o = only(pdf.doc)
    target = pymupdf.Rect(o.rect.x0, o.rect.y0, o.rect.x1 + 100, o.rect.y1)
    (n,) = gr.scale_graphics(pdf, 0, o, o.rect, target)
    assert_rect(n.rect, target, tol=0.01)
    data = contents(pdf)
    assert b" re" not in data and data.count(b" l") == 3 and b" h" in data
    assert drawings(pdf.doc)[0][1] == pytest.approx(2)


# ---------------------------------------------------------------------------
# delete
# ---------------------------------------------------------------------------
def test_delete_and_undo():
    content = TEXT + BOX + LINE
    pdf, _ = build(content)
    box, line = gr.list_graphics(pdf.doc, 0)
    gr.delete_graphics(pdf, 0, [box])
    (left,) = gr.list_graphics(pdf.doc, 0)
    assert_rect(left.rect, line.rect) and left.paths == (0,)
    assert "Kvartalsrapport" in pdf.doc[0].get_text()
    assert near(px(pdf.doc, 160, 842 - 640), WHITE)
    data = contents(pdf)
    assert data.startswith(TEXT) and data.endswith(LINE)
    assert pdf.history.undo_label == "Slet grafik"
    pdf.undo()
    assert contents(pdf) == content


def test_delete_respects_marked_content():
    content = (b"/Artifact BMC q 0 0 1 rg 100 100 50 50 re f Q EMC\n"
               b"/Div <</MCID 0>> BDC q 1 0 0 rg 300 300 50 50 re f\nEMC Q\n" + TEXT)
    pdf, _ = build(content)
    a, b = gr.list_graphics(pdf.doc, 0)
    gr.delete_graphics(pdf, 0, [a, b])
    data = contents(pdf)
    assert data.count(b"BMC") == 1 and data.count(b"BDC") == 1 and data.count(b"EMC") == 2
    assert gr.list_graphics(pdf.doc, 0) == [] and "Kvartalsrapport" in pdf.doc[0].get_text()


def test_delete_graphics_doc_without_undo():
    pdf, _ = build(TEXT + BOX)
    doc = pymupdf.open("pdf", pdf.to_bytes())
    gr.delete_graphics_doc(doc, 0, gr.list_graphics(doc, 0))
    assert gr.list_graphics(doc, 0) == [] and "Kvartalsrapport" in doc[0].get_text()
    # also on non-editable graphics: a painted clip keeps clipping
    doc2 = pymupdf.open("pdf", build(b"q 0 0 1 rg 100 100 50 50 re W f " + TEXT + b"Q\n")[0].to_bytes())
    clip = only(doc2)
    assert not clip.editable
    gr.delete_graphics_doc(doc2, 0, [clip])
    assert gr.list_graphics(doc2, 0) == []
    assert b"re W n" in doc2[0].read_contents()


# ---------------------------------------------------------------------------
# style (O11)
# ---------------------------------------------------------------------------
def test_style_colours_and_width():
    pdf, _ = build(TEXT + BOX + LINE)
    box, line = gr.list_graphics(pdf.doc, 0)
    (box2,) = gr.set_graphic_style(pdf, 0, [box], stroke=(0, 1, 0), fill=(1, 1, 0), width=4)
    assert box2.stroke == (0, 1, 0) and box2.fill == (1, 1, 0) and box2.width == pytest.approx(4)
    assert_rect(box2.rect, (98, 160, 222, 244))
    d = drawings(pdf.doc)
    assert d[0][3] == pytest.approx((1, 1, 0)) and d[0][1] == pytest.approx(4) and d[0][2] == pytest.approx((0, 1, 0))
    assert pdf.history.undo_label == "Skift udseende"
    size = len(contents(pdf))
    for colour in ((1, 0, 1), (0, 0, 0), (0, 1, 1)):                 # repeated changes rewrite, not grow
        (box2,) = gr.set_graphic_style(pdf, 0, [box2], stroke=colour, fill=colour, width=3)
    assert len(contents(pdf)) <= size
    assert box2.stroke == (0, 1, 1) and box2.width == pytest.approx(3)
    # the line keeps its own look
    assert gr.list_graphics(pdf.doc, 0)[1].stroke == (0, 0, 0)
    assert gr.list_graphics(pdf.doc, 0)[1].width == 3


def test_style_add_and_remove_fill_and_stroke():
    pdf, _ = build(TEXT + b"q 0 G 2 w 100 100 m 200 100 l 150 180 l h S Q\n"
                   + TEXT + b"q 1 0 0 rg 300 300 50 50 re f Q\n")
    tri, sq = gr.list_graphics(pdf.doc, 0)
    (tri2,) = gr.set_graphic_style(pdf, 0, tri, fill=(0, 0, 1))
    assert tri2.fill == (0, 0, 1) and tri2.stroke == (0, 0, 0)
    assert near(px(pdf.doc, 150, 842 - 130), BLUE)
    (tri3,) = gr.set_graphic_style(pdf, 0, tri2, stroke=None)
    assert tri3.stroke is None and tri3.fill == (0, 0, 1) and tri3.width == 0
    (sq2,) = gr.set_graphic_style(pdf, 0, sq, stroke=(0, 1, 0), width=5)
    assert sq2.stroke == (0, 1, 0) and sq2.fill == (1, 0, 0) and sq2.width == pytest.approx(5)
    with pytest.raises(ValueError):
        gr.set_graphic_style(pdf, 0, sq2, stroke=None, fill=None)
    assert gr.set_graphic_style(pdf, 0, [sq2]) == [sq2]                 # nothing to change
    with pytest.raises(ValueError):
        gr.set_graphic_style(pdf, 0, sq2, width=-1)


# ---------------------------------------------------------------------------
# restack (O12)
# ---------------------------------------------------------------------------
def test_restack_front_and_back():
    content = (b"0.5 w\n"                                     # state at top level that must not leak
               b"q 1 0 0 rg 100 100 100 100 re f Q\n" + TEXT
               + b"q 0 0 1 rg 150 150 100 100 re f Q\n" + TEXT
               + b"q 0 G 4 w 300 300 m 400 300 l S Q\n")
    pdf, _ = build(content)
    red, blue, line = gr.list_graphics(pdf.doc, 0)
    overlap = (175, 842 - 175)
    assert near(px(pdf.doc, *overlap), BLUE)
    (red2,) = gr.restack_graphics(pdf, 0, [red], front=True)
    assert near(px(pdf.doc, *overlap), RED)
    assert red2.paths == (2,) and red2.fill == (1, 0, 0)
    assert_rect(red2.rect, red.rect)
    assert pdf.history.undo_label == "Læg forrest"
    # the line is still 4 pt, the page state did not change
    assert [w for _r, w, c, _f in drawings(pdf.doc) if c] == [4]
    size = len(contents(pdf))
    (red3,) = gr.restack_graphics(pdf, 0, [red2], front=False)
    assert near(px(pdf.doc, *overlap), BLUE)
    assert red3.paths == (0,)
    for _ in range(5):
        (red3,) = gr.restack_graphics(pdf, 0, [red3], front=True)
        (red3,) = gr.restack_graphics(pdf, 0, [red3], front=False)
    (red4,) = gr.restack_graphics(pdf, 0, [red3], front=True)
    assert len(contents(pdf)) <= size + 2                      # no growth on repeated restacks
    assert pdf.history.undo_label == "Læg forrest"
    for _ in range(13):
        pdf.undo()
    assert contents(pdf) == content


def test_restack_keeps_inner_clip_and_state():
    content = (b"0 0 1 RG\nq 100 100 50 50 re W n 1 0 0 rg 80 80 100 100 re f Q\n" + TEXT
               + b"q 0 1 0 rg 120 120 100 100 re f Q\n")
    pdf, _ = build(content)
    clipped, green = gr.list_graphics(pdf.doc, 0)
    gr.restack_graphics(pdf, 0, clipped, front=True)
    assert near(px(pdf.doc, 140, 842 - 140), RED)               # on top now
    assert near(px(pdf.doc, 160, 842 - 160), GREEN)             # still clipped to 100..150
    assert near(px(pdf.doc, 90, 842 - 90), WHITE)


# ---------------------------------------------------------------------------
# forms
# ---------------------------------------------------------------------------
def test_path_only_form_is_one_editable_object():
    form = b"q 1 0 0 rg 0 0 100 60 re f Q q 0 0 1 RG 2 w 0 0 100 60 re S Q"
    content = TEXT + b"q 1 0 0 1 200 300 cm /Fm1 Do Q\n"
    pdf, x = build(content, forms={"Fm1": (form, {})})
    o = only(pdf.doc)
    assert o.in_form and o.editable and o.paths == (0, 1)
    assert_rect(o.rect, (199, 842 - 361, 301, 842 - 299))
    (m,) = gr.move_graphics(pdf, 0, o, -50, 20)
    assert_rect(m.rect, o.rect + (-50, 20, -50, 20))
    assert pdf.doc.xref_stream(x["Fm1"]) == form                 # the form itself is untouched
    target = pymupdf.Rect(m.rect.x0, m.rect.y0, m.rect.x1 + 100, m.rect.y1 + 20)
    (s,) = gr.scale_graphics(pdf, 0, m, m.rect, target)
    assert_rect(s.rect, target, tol=0.01)
    assert pdf.doc.xref_stream(x["Fm1"]) == form                 # scaled in a private copy
    assert [w for _r, w, c, _f in drawings(pdf.doc) if c] == [2]
    (st,) = gr.set_graphic_style(pdf, 0, s, fill=(0, 1, 0))
    assert st.fill == (0, 1, 0) and st.in_form
    pr = gr.restack_graphics(pdf, 0, st, front=False)
    assert pr[0].paths == (0, 1)
    gr.delete_graphics(pdf, 0, pr)
    assert gr.list_graphics(pdf.doc, 0) == [] and "Kvartalsrapport" in pdf.doc[0].get_text()


def test_paths_in_mixed_form_are_not_editable():
    form = b"BT /F1 10 Tf 40 40 Td (Brevpapir) Tj ET q 0 0 1 rg 300 300 80 40 re f Q"
    pdf, x = build(b"q /Fm1 Do Q\n", forms={"Fm1": (form, {})})
    o = only(pdf.doc)
    assert o.in_form and not o.editable
    with pytest.raises(ValueError):
        gr.move_graphics(pdf, 0, o, 10, 10)
    with pytest.raises(ValueError):
        gr.delete_graphics(pdf, 0, o)
    assert not pdf.history.can_undo
    # rendering without it still works (private copy of the form)
    pix = gr.render_without_graphics(pdf.doc, 0, [o], 1.0)
    assert tuple(pix.pixel(340, 842 - 320)[:3]) == WHITE
    assert pdf.doc.xref_stream(x["Fm1"]) == form


def test_clip_paths():
    content = (b"q 50 50 200 200 re W n 1 0 0 rg 0 0 400 400 re f Q\n"     # clip only: not an object
               + TEXT + b"q 0 0 1 rg 300 300 50 50 re W f Q\n")          # painted clip: listed, not editable
    pdf, _ = build(content)
    objs = gr.list_graphics(pdf.doc, 0)
    assert len(objs) == 2
    clipped_fill, painted_clip = objs
    assert clipped_fill.editable and not painted_clip.editable
    with pytest.raises(ValueError):
        gr.move_graphics(pdf, 0, painted_clip, 5, 5)
    # the clipped fill moves together with its clip (it is in the same block)
    gr.move_graphics(pdf, 0, clipped_fill, 100, 0)
    assert near(px(pdf.doc, 260, 842 - 100), RED)
    assert near(px(pdf.doc, 60, 842 - 100), WHITE)


# ---------------------------------------------------------------------------
# images: restack
# ---------------------------------------------------------------------------
def test_restack_image_is_available_from_objects():
    content = b"q 200 0 0 200 100 400 cm /Im1 Do Q\nq 0 0 1 rg 150 450 100 100 re f Q\n"
    pdf, _ = build(content, images={"Im1": make_png(10, 10, (255, 0, 0))})
    assert near(px(pdf.doc, 200, 842 - 500), BLUE)
    (img,) = ob.list_images(pdf.doc, 0)
    up = ob.restack_image(pdf, 0, img, front=True)
    assert near(px(pdf.doc, 200, 842 - 500), RED)
    assert_rect(up.rect, img.rect)


# ---------------------------------------------------------------------------
# clipboard helpers, lifted rendering
# ---------------------------------------------------------------------------
def test_strip_page_except():
    content = (TEXT + BOX + LINE + b"q 200 0 0 100 50 50 cm /Im1 Do Q\nq 50 0 0 50 300 50 cm /Im1 Do Q\n"
               b"/Sh1 sh\n")
    pdf, _ = build(content, images={"Im1": make_png(10, 10, (255, 0, 0))})
    doc = pymupdf.open("pdf", pdf.to_bytes())
    box, line = gr.list_graphics(doc, 0)
    gr.strip_page_except(doc, 0, keep_paths=box.paths, keep_images=(1,))
    (left,) = gr.list_graphics(doc, 0)
    assert left.paths == (0, 1)
    assert_rect(left.rect, box.rect)
    imgs = ob.list_images(doc, 0)
    assert len(imgs) == 1
    assert_rect(imgs[0].rect, (300, 842 - 100, 350, 842 - 50))
    assert "Kvartalsrapport" in doc[0].get_text()
    assert b"sh" not in doc[0].read_contents().replace(b"Kvartalsrapport", b"")


def test_strip_page_except_forms():
    form = b"q 0 1 0 rg 0 0 50 50 re f Q"
    mixed = b"BT /F1 10 Tf 40 40 Td (Brevpapir) Tj ET q 0 0 1 rg 300 300 80 40 re f Q q 1 0 0 rg 0 0 9 9 re f Q"
    pdf, x = build(TEXT + b"q /Fm1 Do Q q /Fm2 Do Q\n", forms={"Fm1": (form, {}), "Fm2": (mixed, {})})
    doc = pymupdf.open("pdf", pdf.to_bytes())
    every = gr.list_graphics(doc, 0, all_items=True)
    assert [o.paths for o in every] == [(0,), (1,), (2,)]
    gr.strip_page_except(doc, 0, keep_paths=(1,))
    left = gr.list_graphics(doc, 0, all_items=True)
    assert len(left) == 1 and left[0].fill == (0, 0, 1)
    text = doc[0].get_text()
    assert "Brevpapir" in text and "Kvartalsrapport" in text


def test_render_without_graphics():
    content = b"q 0 1 0 rg 50 500 300 300 re f Q\n" + TEXT + BOX
    pdf, _ = build(content)
    green, box = gr.list_graphics(pdf.doc, 0)
    before = contents(pdf)
    pix = gr.render_without_graphics(pdf.doc, 0, [box], 1.0, clip=box.rect)
    assert (pix.x, pix.y) == (99, 161)
    assert near(pix.pixel(pix.width // 2, pix.height // 2)[:3], GREEN)        # what lies beneath
    assert contents(pdf) == before and not pdf.history.can_undo


# ---------------------------------------------------------------------------
# identity, undo, performance
# ---------------------------------------------------------------------------
def test_find_graphic_after_edits():
    pdf, _ = build(TEXT + b"q 0 1 0 rg 50 50 40 40 re f Q\n" + TEXT + BOX)
    green, box = gr.list_graphics(pdf.doc, 0)
    assert gr.find_graphic(pdf.doc, 0, box) == box
    gr.delete_graphics(pdf, 0, green)                            # path numbers shift
    again = gr.find_graphic(pdf.doc, 0, box)
    assert again.paths == (0, 1)
    assert_rect(again.rect, box.rect)
    moved = gr.move_graphics(pdf, 0, again, 30, 0)[0]
    assert gr.find_graphic(pdf.doc, 0, again).paths == moved.paths
    gr.delete_graphics(pdf, 0, moved)
    assert gr.find_graphic(pdf.doc, 0, moved) is None
    with pytest.raises(LookupError):
        gr.move_graphics(pdf, 0, moved, 1, 1)


def test_undo_restores_exactly_with_forms():
    form = b"q 1 0 0 rg 0 0 100 60 re f Q"
    content = TEXT + b"q 1 0 0 1 200 300 cm /Fm1 Do Q\n" + BOX
    pdf, _ = build(content, forms={"Fm1": (form, {})})
    snap = pdf.to_bytes()
    f, box = gr.list_graphics(pdf.doc, 0)
    f = gr.scale_graphics(pdf, 0, f, f.rect, f.rect + (0, 0, 40, 40))[0]
    gr.set_graphic_style(pdf, 0, box, fill=(0, 1, 0))
    gr.restack_graphics(pdf, 0, f, front=True)
    gr.delete_graphics(pdf, 0, gr.list_graphics(pdf.doc, 0))
    for _ in range(4):
        pdf.undo()
    no_id = re.compile(rb"/ID\s*\[[^\]]*\]")
    assert no_id.sub(b"", pdf.to_bytes()) == no_id.sub(b"", snap)


def dense_page(n=500) -> bytes:
    out = [TEXT.decode()]
    for i in range(n):
        x, y = 30 + (i % 25) * 21, 60 + (i // 25) * 36
        out.append(f"q {i % 7 / 7:.3f} 0.4 0.6 rg {x} {y} 15 12 re f Q\n")
        if i % 3 == 0:
            out.append(f"q 0 G 0.5 w {x} {y + 14} m {x + 15} {y + 20} l S Q\n")
        if i % 25 == 24:
            out.append("BT /F1 8 Tf 30 %d Td (Række) Tj ET\n" % (y + 28))
    return "".join(out).encode()


def test_dense_page_performance():
    pdf, _ = build(dense_page())
    t0 = time.perf_counter()
    objs = gr.list_graphics(pdf.doc, 0)
    cold = time.perf_counter() - t0
    t0 = time.perf_counter()
    for _ in range(5):
        gr.list_graphics(pdf.doc, 0)
        gr.graphic_at(pdf.doc, 0, (300, 400))
    hot = (time.perf_counter() - t0) / 5
    assert len(gr._listing(pdf.doc, 0).scan.paths) >= 660
    assert len(objs) > 400
    t0 = time.perf_counter()
    gr.move_graphics(pdf, 0, objs[200], 5, 5)
    edit = time.perf_counter() - t0
    # generous bounds for slow CI machines (measured: see the WP-A report)
    assert cold < 1.0 and hot < 0.05 and edit < 1.0, (cold, hot, edit)


def test_state_set_inside_the_path_keeps_later_content():
    # PyMuPDF style: colours are set between construction and paint, and stay
    # in force for what follows – a wrap or removal must keep that
    content = b"1 0 0 rg 100 100 50 50 re 0 0 1 rg f\n" + TEXT + b"300 300 50 50 re f\n"
    pdf, _ = build(content)
    first, second = gr.list_graphics(pdf.doc, 0)
    assert first.editable and first.fill == (0, 0, 1) and second.fill == (0, 0, 1)
    gr.move_graphics(pdf, 0, first, 0, -100)
    assert [o.fill for o in gr.list_graphics(pdf.doc, 0)] == [(0, 0, 1), (0, 0, 1)]
    assert near(px(pdf.doc, 325, 842 - 325), BLUE)
    pdf.undo()
    gr.set_graphic_style(pdf, 0, first, fill=(0, 1, 0))
    assert [o.fill for o in gr.list_graphics(pdf.doc, 0)] == [(0, 1, 0), (0, 0, 1)]
    assert near(px(pdf.doc, 125, 842 - 125), GREEN) and near(px(pdf.doc, 325, 842 - 325), BLUE)
    pdf.undo()
    gr.restack_graphics(pdf, 0, first, front=True)
    assert near(px(pdf.doc, 325, 842 - 325), BLUE) and near(px(pdf.doc, 125, 842 - 125), BLUE)
    pdf.undo()
    gr.delete_graphics(pdf, 0, first)
    (left,) = gr.list_graphics(pdf.doc, 0)
    assert left.fill == (0, 0, 1) and near(px(pdf.doc, 325, 842 - 325), BLUE)


def test_slide_title_bar_repeated_is_template():
    bar = b"q 0.12 0.36 0.29 rg 0 381.5 793.7 65 re f Q q 0.9 0.7 0.3 rg 0 377.5 793.7 4 re f Q\n"
    box = b"q 0.2 0.5 0.4 rg 100 100 200 120 re f Q\n"
    pdf = build_pages([box, bar + box, bar + TEXT, bar], width=793.7, height=446.5)
    assert [o.kind for o in gr.list_graphics(pdf.doc, 0)] == ["shape"]
    for p in (1, 2, 3):
        kinds = [o.kind for o in gr.list_graphics(pdf.doc, p, all_items=True)]
        assert kinds[:2] == ["template", "template"], (p, kinds)
    assert [o.kind for o in gr.list_graphics(pdf.doc, 1)] == ["shape"]


def test_libreoffice_callout_box_border_lines_join_the_fill():
    # fill, the box text, then four border lines (LibreOffice callout boxes)
    content = (b"0.89 0.93 0.96 rg\n56.65 475.7 m 538.55 475.7 l 538.55 405.7 l 56.65 405.7 l 56.65 475.7 l h f*\n"
               + TEXT + b"0.24 0.49 0.67 RG\n"
               b"q 0.75 w 56.7 475.3 m 538.55 475.3 l S Q\nq 0.75 w 538.2 475.7 m 538.2 405.7 l S Q\n"
               b"q 0.75 w 538.55 406.1 m 56.7 406.1 l S Q\nq 0.75 w 57.1 405.7 m 57.1 475.7 l S Q\n")
    pdf, _ = build(content)
    (box,) = gr.list_graphics(pdf.doc, 0)
    assert box.paths == (0, 1, 2, 3, 4) and box.kind == "shape"
    assert box.fill is not None and box.stroke is not None
    (moved,) = gr.move_graphics(pdf, 0, box, 0, 50)
    assert_rect(moved.rect, box.rect + (0, 50, 0, 50))
    assert "Kvartalsrapport" in pdf.doc[0].get_text()


# ---------------------------------------------------------------------------
# grouping by geometric contact, not bounding boxes
# ---------------------------------------------------------------------------
def circle(cx, cy, r, op=b"f", colour=b"0 0 1 rg") -> bytes:
    k = 0.5523 * r
    return (f"q {colour.decode()} {cx + r} {cy} m {cx + r} {cy + k} {cx + k} {cy + r} {cx} {cy + r} c "
            f"{cx - k} {cy + r} {cx - r} {cy + k} {cx - r} {cy} c {cx - r} {cy - k} {cx - k} {cy - r} {cx} {cy - r} c "
            f"{cx + k} {cy - r} {cx + r} {cy - k} {cx + r} {cy} c h {op.decode()} Q\n").encode()


def test_line_inside_a_circles_bbox_but_not_touching_it_is_separate():
    # the cover case: a rule within the circle's bbox, far from its outline
    content = circle(300, 400, 150) + b"q 1 1 1 RG 1 w 160 270 m 200 270 l S Q\n"
    pdf, _ = build(content)
    circ, line = gr.list_graphics(pdf.doc, 0)
    assert circ.paths == (0,) and line.paths == (1,) and line.kind == "line"
    assert gr.graphic_at(pdf.doc, 0, (180, 842 - 270)).paths == (1,)
    assert gr.graphic_at(pdf.doc, 0, (300, 842 - 400)).paths == (0,)


def test_small_circle_inside_a_filled_circle_is_separate():
    content = circle(300, 400, 150) + circle(300, 400, 20, colour=b"1 0 0 rg")
    pdf, _ = build(content)
    assert [o.paths for o in gr.list_graphics(pdf.doc, 0)] == [(0,), (1,)]
    assert gr.graphic_at(pdf.doc, 0, (300, 842 - 400)).paths == (1,)


def test_contact_groups_still_form():
    arrow = b"q 0 0 0 RG 1 w 100 500 m 200 500 l S Q\nq 0 g 200 495 m 212 500 l 200 505 l h f Q\n"
    zigzag = (b"q 0 G 1 w 300 100 m 340 140 l S Q\nq 0 G 1 w 340 140 m 380 100 l S Q\n"
              b"q 0 G 1 w 380 100 m 420 140 l S Q\n")
    # a stroked circle and a line starting on its outline
    ring = circle(450, 600, 40, op=b"S", colour=b"0 G 2 w") + b"q 0 G 2 w 490 600 m 540 600 l S Q\n"
    pdf, _ = build(arrow + TEXT + zigzag + TEXT + ring)
    assert [o.paths for o in gr.list_graphics(pdf.doc, 0)] == [(0, 1), (2, 3, 4), (5, 6)]


# ---------------------------------------------------------------------------
# tables need a real grid (aligned rules / tiling cells)
# ---------------------------------------------------------------------------
def test_overlapping_frames_are_not_a_table():
    pdf, _ = build(b"q 0 0 1 RG 2 w 100 600 120 80 re S Q\nq 0 0 1 RG 2 w 110 590 120 80 re S Q\n")
    objs = gr.list_graphics(pdf.doc, 0, all_items=True)
    assert [o.kind for o in objs] == ["shape", "shape"] and all(o.selectable for o in objs)
    pdf3, _ = build(b"q 0 G 1 w 100 600 120 80 re S Q\n" + TEXT + b"q 0 G 1 w 110 590 120 80 re S Q\n"
                    + TEXT + b"q 0 G 1 w 120 580 120 80 re S Q\n")
    assert [o.kind for o in gr.list_graphics(pdf3.doc, 0, all_items=True)] == ["shape"] * 3


def test_duplicated_callout_box_stays_two_shapes():
    def callout(dx, dy):
        x0, x1, y0, y1 = 56.65 + dx, 538.55 + dx, 405.7 - dy, 475.7 - dy
        return (f"0.89 0.93 0.96 rg\n{x0} {y1} m {x1} {y1} l {x1} {y0} l {x0} {y0} l {x0} {y1} l h f*\n".encode()
                + TEXT + (f"0.24 0.49 0.67 RG\nq 0.75 w {x0} {y1} m {x1} {y1} l S Q\n"
                          f"q 0.75 w {x1} {y1} m {x1} {y0} l S Q\n"
                          f"q 0.75 w {x1} {y0} m {x0} {y0} l S Q\nq 0.75 w {x0} {y0} m {x0} {y1} l S Q\n").encode())
    pdf, _ = build(callout(0, 0) + callout(10, 10))
    objs = gr.list_graphics(pdf.doc, 0)
    assert [o.paths for o in objs] == [(0, 1, 2, 3, 4), (5, 6, 7, 8, 9)]
    assert all(o.kind == "shape" and o.selectable for o in objs)


def test_adjoining_stroked_cells_are_a_table():
    cells = b"".join(f"q 0 G 0.5 w {100 + c * 60} {400 + r * 20} 60 20 re S Q\n".encode()
                     for r in range(3) for c in range(3))
    pdf, _ = build(cells + BOX)
    assert [o.kind for o in gr.list_graphics(pdf.doc, 0)] == ["shape"]
    assert [o.kind for o in gr.list_graphics(pdf.doc, 0, all_items=True)].count("table") == 9


# ---------------------------------------------------------------------------
# pasted blocks (/FolioPaste BMC … EMC, pageobjects._flatten) are
# segments of their own: they never merge or form a table with the page
# ---------------------------------------------------------------------------
def paste(content: bytes) -> bytes:
    return b"/FolioPaste BMC\n" + content + b"EMC\n"


def test_pasted_segment_never_joins_the_original():
    fill = b"q 1 0 0 rg 100 600 120 80 re f Q\n"
    # identical geometry: the fill of the original and of a copy pasted on top
    pdf, _ = build(fill + paste(fill))
    assert [o.paths for o in gr.list_graphics(pdf.doc, 0)] == [(0,), (1,)]
    pdf, _ = build(fill + fill)                       # without the boundary: one fill + "border" pair
    assert [o.paths for o in gr.list_graphics(pdf.doc, 0)] == [(0, 1)]
    # border lines of a pasted callout never join the original's fill
    lines = (b"0 0 1 RG q 1 w 100 680 m 220 680 l S Q\nq 1 w 220 680 m 220 600 l S Q\n"
             b"q 1 w 220 600 m 100 600 l S Q\nq 1 w 100 600 m 100 680 l S Q\n")
    pdf, _ = build(fill + TEXT + paste(lines))
    assert [o.paths for o in gr.list_graphics(pdf.doc, 0)] == [(0,), (1, 2, 3, 4)]
    pdf, _ = build(fill + TEXT + lines)
    assert [o.paths for o in gr.list_graphics(pdf.doc, 0)] == [(0, 1, 2, 3, 4)]
    # two framed boxes overlapping like a grid: a table on one page, two shapes
    # when the second one is pasted
    frame = b"q 0 G 1 w 60 500 480 70 re S Q\n"
    moved = b"q 0 G 1 w 60 460 480 70 re S Q\n"
    pdf, _ = build(frame + moved)
    assert [o.kind for o in gr.list_graphics(pdf.doc, 0, all_items=True)] == ["table", "table"]
    pdf, _ = build(frame + paste(moved))
    objs = gr.list_graphics(pdf.doc, 0, all_items=True)
    assert [o.kind for o in objs] == ["shape", "shape"] and all(o.selectable for o in objs)
    # text (also an empty BT ET) and other marked content are no boundary
    pdf, _ = build(frame + b"BT ET\n" + moved)
    assert [o.kind for o in gr.list_graphics(pdf.doc, 0, all_items=True)] == ["table", "table"]
    pdf, _ = build(b"/Artifact BMC\n" + frame + b"EMC\n/P <</MCID 0>> BDC\n" + moved + b"EMC\n")
    assert [o.kind for o in gr.list_graphics(pdf.doc, 0, all_items=True)] == ["table", "table"]
    pdf, _ = build(frame + TEXT + moved)
    assert [o.kind for o in gr.list_graphics(pdf.doc, 0, all_items=True)] == ["table", "table"]


def test_paste_segments_follow_the_marked_content_stack():
    fill = b"q 1 0 0 rg 100 600 120 80 re f Q\n"
    # other sequences inside and around a paste close the right one; after
    # the paste's EMC the page's own segment is back
    content = (b"/P <</MCID 0>> BDC\n" + fill + b"/FolioPaste BMC\n/Span BMC\n" + fill + b"EMC\n"
               + b"/Artifact BMC\n" + fill + b"EMC\nEMC\n" + fill + b"EMC\n")
    pdf, _ = build(content)
    scan = gr._page_scan(pdf.doc, 0)
    assert [p.seg for p in scan.paths] == [0, 1, 1, 0]
    # a pasted copy of a pasted object (nested) gets its own segment
    pdf, _ = build(paste(fill + paste(fill)) + paste(fill))
    assert [p.seg for p in gr._page_scan(pdf.doc, 0).paths] == [1, 2, 3]
    assert [o.paths for o in gr.list_graphics(pdf.doc, 0)] == [(0,), (1,), (2,)]


def test_edits_keep_pasted_paths_in_their_segment():
    frame = b"q 0 G 1 w 60 500 480 70 re S Q\n"
    moved = b"q 0 G 1 w 60 460 480 70 re S Q\n"
    pdf, _ = build(frame + TEXT + paste(moved))
    a, b = gr.list_graphics(pdf.doc, 0)
    (b2,) = gr.move_graphics(pdf, 0, b, 5, 0)
    (b3,) = gr.scale_graphics(pdf, 0, b2, b2.rect, b2.rect + (0, 0, 10, 0))
    (b4,) = gr.set_graphic_style(pdf, 0, b3, stroke=(1, 0, 0))
    objs = gr.list_graphics(pdf.doc, 0, all_items=True)
    assert [o.kind for o in objs] == ["shape", "shape"] and objs[1].key == b4.key
    # restack: the moved content keeps its segment, front and back
    for target, front in ((objs[0], True), (objs[1], False), (objs[1], True)):
        target = gr.find_graphic(pdf.doc, 0, target)
        gr.restack_graphics(pdf, 0, [target], front=front)
        assert [o.kind for o in gr.list_graphics(pdf.doc, 0, all_items=True)] == ["shape", "shape"]
    assert b"BT ET" not in contents(pdf)
    gr.delete_graphics(pdf, 0, [gr.list_graphics(pdf.doc, 0)[0]])
    assert len(gr.list_graphics(pdf.doc, 0)) == 1
