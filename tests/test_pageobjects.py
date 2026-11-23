"""The page-object facade (core/pageobjects.py): images, text blocks and
graphics as one selection – listing, hit priority, mixed edits as one undo
step, drag rendering, and the object clipboard (copy/paste within a page,
to another page, into another document, across /Rotate)."""

from __future__ import annotations

import os
import time

import pymupdf
import pytest

from pdfstudio.core import PdfDocument
from pdfstudio.core import graphics as gr
from pdfstudio.core import objects as ob
from pdfstudio.core import pageobjects as po
from pdfstudio.core import textobjects as tob
from tests.conftest import SANS_TTF, make_png
from tests.flowdocs import report

BOX = pymupdf.Rect(80, 100, 320, 220)
BOX_TEXT = "Faktaboks: omsætningen steg"
BODY_TEXT = "Brødtekst nederst på siden"
IMG = pymupdf.Rect(360, 100, 520, 220)
BIG = pymupdf.Rect(360, 360, 560, 560)
SMALL = pymupdf.Rect(400, 400, 440, 440)

CORPUS = "/tmp/claude-0/-home-user-PDF/740062b8-3b33-5ee1-bf87-7d6301ab4318/scratchpad/corpus"


# -- fixtures and helpers -------------------------------------------------------
def scene_doc(rotation: int = 0) -> pymupdf.Document:
    """A callout box (fill + border) with text inside, a red rule, an image,
    a big grey square with a small green one on it, and body text."""
    doc = pymupdf.open()
    pg = doc.new_page(width=595, height=842)
    # fill and border as two paths (as office programs write them): one group
    pg.draw_rect(BOX, color=None, fill=(1, 0.9, 0.8))
    pg.draw_rect(BOX, color=(0, 0, 1), width=2)
    if SANS_TTF:
        pg.insert_font(fontname="LibSans", fontfile=SANS_TTF)
        pg.insert_text((95, 135), BOX_TEXT, fontname="LibSans", fontsize=12, color=(0.1, 0.1, 0.4))
    else:  # pragma: no cover - fonts are installed on CI
        pg.insert_text((95, 135), BOX_TEXT, fontname="helv", fontsize=12, color=(0.1, 0.1, 0.4))
    pg.draw_line((80, 300), (300, 300), color=(1, 0, 0), width=3)
    pg.insert_image(IMG, stream=make_png(80, 60, (40, 160, 60)), keep_proportion=False)
    pg.draw_rect(BIG, color=None, fill=(0.8, 0.8, 0.8))
    pg.insert_text((72, 760), BODY_TEXT, fontname="helv", fontsize=11)
    pg.draw_rect(SMALL, color=None, fill=(0, 0.7, 0))
    if rotation:
        pg.set_rotation(rotation)
    return pymupdf.open("pdf", doc.tobytes())


def scene(rotation: int = 0, pages: int = 1) -> PdfDocument:
    doc = scene_doc(rotation)
    for _ in range(pages - 1):
        doc.new_page(width=595, height=842)
    return PdfDocument.from_bytes(doc.tobytes())


def blank(width=595, height=842, rotation=0) -> PdfDocument:
    doc = pymupdf.open()
    pg = doc.new_page(width=width, height=height)
    if rotation:
        pg.set_rotation(rotation)
    return PdfDocument.from_bytes(doc.tobytes())


def by_kind(objs, kind):
    return [o for o in objs if o.kind == kind]


def find(objs, kind, rect, tol=3.0):
    """The object of ``kind`` whose rect is ``rect`` (± tol)."""
    hits = [o for o in objs if o.kind == kind and all(abs(a - b) <= tol for a, b in zip(o.rect, pymupdf.Rect(rect)))]
    assert len(hits) == 1, (kind, rect, [(o.kind, o.rect) for o in objs])
    return hits[0]


def norm(text: str) -> str:
    """Text with no-break spaces as spaces and whitespace collapsed (on
    Windows the fixtures' Arial has one glyph for both, so extracted text
    reads U+00A0 where a space was written)."""
    return " ".join(text.replace("\xa0", " ").split())


def tx(obj) -> str:
    """The normalised text of a text PageObject / TextObject."""
    return norm(getattr(obj, "ref", obj).text)


def page_text(page) -> str:
    return norm(page.get_text())


def text_obj(objs, text):
    hits = [o for o in objs if o.kind == "text" and norm(text) in tx(o)]
    assert len(hits) == 1, [tx(o) for o in objs if o.kind == "text"]
    return hits[0]


def test_text_helpers_ignore_no_break_spaces():
    assert norm("Faktaboks:\xa0omsætningen\xa0 steg\n") == BOX_TEXT
    fake = type("T", (), {"text": "Brødtekst\xa0nederst\xa0på\xa0siden"})()
    obj = po.PageObject(kind="text", page=0, rect=pymupdf.Rect(0, 0, 1, 1), ref=fake, key=("text",))
    assert tx(obj) == BODY_TEXT and text_obj([obj], BODY_TEXT) is obj


def assert_rect(a, b, tol=0.6):
    a, b = pymupdf.Rect(a), pymupdf.Rect(b)
    assert all(abs(x - y) <= tol for x, y in zip(a, b)), (a, b)


def pix(doc, page=0, clip=None, zoom=1.0, alpha=False) -> pymupdf.Pixmap:
    return po.render_page(doc, page, zoom, clip=clip, alpha=alpha, annots=False)


def mismatch(a: pymupdf.Pixmap, b: pymupdf.Pixmap, tol: int = 60) -> float:
    """Fraction of pixels that differ by more than ``tol`` in any channel."""
    assert (a.width, a.height) == (b.width, b.height), ((a.width, a.height), (b.width, b.height))
    sa, sb, n = a.samples, b.samples, a.n
    m = min(n, 3)
    bad = 0
    for i in range(0, len(sa), n):
        if sa[i:i + m] != sb[i:i + m] and max(abs(sa[i + k] - sb[i + k]) for k in range(m)) > tol:
            bad += 1
    return bad / max(1, a.width * a.height)


def at(p: pymupdf.Pixmap, x: float, y: float, zoom: float = 1.0) -> tuple:
    """Pixel at display point (x, y) of a page pixmap (honours its offset)."""
    return tuple(p.pixel(int(x * zoom) - p.x, int(y * zoom) - p.y)[:3])


def near(c, rgb, tol=40) -> bool:
    return all(abs(a - b) <= tol for a, b in zip(c, rgb))


# -- listing and hit-testing -------------------------------------------------------
def test_listing_kinds_order_and_keys():
    pdf = scene()
    objs = po.page_objects(pdf.doc, 0)
    assert {o.kind for o in objs} == {"text", "graphic", "image"}
    assert len(by_kind(objs, "text")) == 2 and len(by_kind(objs, "image")) == 1
    assert len(by_kind(objs, "graphic")) == 4                      # box, rule, big, small
    # reading order: top to bottom, then left to right
    ys = [(round(o.rect.y0, 1), round(o.rect.x0, 1)) for o in objs]
    assert ys == sorted(ys)
    for o in objs:
        assert o.key[0] == o.kind and o.key[1:] == tuple(o.ref.key)
        assert o.page == 0 and o.rect == o.ref.rect
        assert po.find_again(pdf.doc, 0, o).key == o.key
    box = find(objs, "graphic", BOX + (-1, -1, 1, 1))
    assert box.ref.paths == (0, 1) or len(box.ref.paths) >= 1
    # a raw engine object is accepted too
    assert po.find_again(pdf.doc, 0, box.ref).key == box.key
    assert po.page_objects(pdf.doc, 5) == [] and po.object_at(pdf.doc, -1, (1, 1)) is None


def test_hit_priority_text_first_then_smallest():
    pdf = scene()
    # on the text inside the box: the text, not the box
    t = po.object_at(pdf.doc, 0, (120, 131))
    assert t.kind == "text" and BOX_TEXT in tx(t)
    # inside the box, away from the text: the box
    b = po.object_at(pdf.doc, 0, (200, 190))
    assert b.kind == "graphic" and b.ref.fill is not None
    # on the rule
    assert po.object_at(pdf.doc, 0, (150, 301)).ref.stroke == (1, 0, 0)
    # the small square on the big one wins; next to it the big one
    small = po.object_at(pdf.doc, 0, (420, 420))
    assert small.kind == "graphic"
    assert_rect(small.rect, SMALL)
    big = po.object_at(pdf.doc, 0, (500, 500))
    assert_rect(big.rect, BIG)
    # the image
    assert po.object_at(pdf.doc, 0, (440, 160)).kind == "image"
    # empty paper
    assert po.object_at(pdf.doc, 0, (300, 600)) is None


def test_smallest_area_also_between_images_and_graphics():
    doc = pymupdf.open()
    pg = doc.new_page()
    pg.insert_image(pymupdf.Rect(100, 100, 400, 400), stream=make_png(30, 30))
    pg.draw_rect(pymupdf.Rect(150, 150, 200, 200), color=None, fill=(1, 0, 0))
    pg.insert_text((50, 50), "x")                                   # (keeps the shapes apart)
    pg.draw_rect(pymupdf.Rect(50, 450, 500, 700), color=None, fill=(0, 0, 1))
    pg.insert_image(pymupdf.Rect(300, 500, 340, 540), stream=make_png(8, 8, (0, 255, 0)))
    pdf = PdfDocument.from_bytes(doc.tobytes())
    assert po.object_at(pdf.doc, 0, (175, 175)).kind == "graphic"     # small shape on a big image
    assert po.object_at(pdf.doc, 0, (300, 300)).kind == "image"
    assert po.object_at(pdf.doc, 0, (320, 520)).kind == "image"       # small image on a big shape
    assert po.object_at(pdf.doc, 0, (100, 600)).kind == "graphic"


def test_alt_reaches_background_and_selects_single_paths():
    doc = scene_doc()
    pg = doc[0]
    # a full-page background drawn first
    data = b"q 0.95 0.95 0.9 rg 0 0 595 842 re f Q\n" + pg.read_contents()
    ob._write_page_contents(doc, pg, data)
    pdf = PdfDocument.from_bytes(doc.tobytes())
    assert po.object_at(pdf.doc, 0, (300, 600)) is None                # never the background
    bg = po.object_at(pdf.doc, 0, (300, 600), alt=True)
    assert bg.kind == "graphic" and bg.ref.kind == "background"
    assert all(o.ref.kind != "background" for o in po.page_objects(pdf.doc, 0) if o.kind == "graphic")
    assert any(o.kind == "graphic" and o.ref.kind == "background" for o in po.page_objects(pdf.doc, 0, alt=True))
    # deep select: the border of the callout box alone
    box = po.object_at(pdf.doc, 0, (81, 160))
    border = po.object_at(pdf.doc, 0, (81, 160), alt=True)
    assert len(border.ref.paths) == 1 and len(box.ref.paths) == 2
    assert border.ref.stroke == (0, 0, 1)
    # text still comes first with Alt
    assert po.object_at(pdf.doc, 0, (120, 131), alt=True).kind == "text"
    # and the single path can be moved alone
    (moved,) = po.move_objects(pdf, 0, [border], 0, 30)
    assert_rect(moved.rect, border.rect + (0, 30, 0, 30))
    assert po.find_again(pdf.doc, 0, po.object_at(pdf.doc, 0, (200, 190), alt=True)).ref.fill is not None


def test_objects_in_rubber_band():
    pdf = scene()
    inside = po.objects_in(pdf.doc, 0, (70, 90, 330, 230))
    assert sorted(o.kind for o in inside) == ["graphic", "text"]
    every = po.objects_in(pdf.doc, 0, (0, 0, 595, 842))
    assert len(every) == len(po.page_objects(pdf.doc, 0))
    assert [o.kind for o in po.objects_in(pdf.doc, 0, (350, 90, 530, 230))] == ["image"]
    assert po.objects_in(pdf.doc, 0, (0, 600, 200, 700)) == []


def test_contents_of_a_callout_box():
    pdf = scene()
    objs = po.page_objects(pdf.doc, 0)
    box = find(objs, "graphic", BOX + (-1, -1, 1, 1))
    inner = po.contents_of(pdf.doc, 0, box)
    assert [o.kind for o in inner] == ["text"] and BOX_TEXT in tx(inner[0])
    big = find(objs, "graphic", BIG)
    assert [o.rect for o in po.contents_of(pdf.doc, 0, big)] == [find(objs, "graphic", SMALL).rect]
    assert po.contents_of(pdf.doc, 0, inner[0]) == []                  # only boxes have contents
    # box and contents move together in one step
    sel = [box] + inner
    moved = po.move_objects(pdf, 0, sel, 0, 300)
    assert [o.kind for o in moved] == ["graphic", "text"]
    assert_rect(moved[0].rect, box.rect + (0, 300, 0, 300))
    assert_rect(moved[1].rect, inner[0].rect + (0, 300, 0, 300), tol=1.0)
    assert len(pdf.history._undo) == 1


# -- mixed edits -------------------------------------------------------------------
def test_mixed_move_is_one_undo_step_and_undo_is_exact():
    pdf = scene()
    before_bytes = pdf.doc[0].read_contents()
    before = pix(pdf.doc)
    objs = po.page_objects(pdf.doc, 0)
    sel = [find(objs, "graphic", BOX + (-1, -1, 1, 1)), text_obj(objs, BOX_TEXT), find(objs, "image", IMG),
           find(objs, "graphic", SMALL)]
    t = time.perf_counter()
    moved = po.move_objects(pdf, 0, sel, 30, 400)
    assert time.perf_counter() - t < 0.5                                   # O16
    assert [o.kind for o in moved] == ["graphic", "text", "image", "graphic"]
    for old, new in zip(sel, moved):
        assert_rect(new.rect, old.rect + (30, 400, 30, 400), tol=1.0)
    assert len(pdf.history._undo) == 1 and pdf.history.undo_label == "Flyt"
    after = pix(pdf.doc)
    assert near(at(after, 440, 560), (40, 160, 60))                        # the image is at its new place
    assert near(at(after, 440, 160), (255, 255, 255))                      # and gone from the old one
    assert BOX_TEXT in page_text(pdf.doc[0]) and BODY_TEXT in page_text(pdf.doc[0])
    pdf.undo()
    assert pdf.doc[0].read_contents() == before_bytes
    assert mismatch(pix(pdf.doc), before, tol=0) == 0
    # the moved objects can be found again after the undo by their old identity
    assert all(po.find_again(pdf.doc, 0, o) is not None for o in sel)


def test_nudges_merge_into_one_step():
    pdf = scene()
    objs = po.page_objects(pdf.doc, 0)
    sel = [find(objs, "graphic", BOX + (-1, -1, 1, 1)), text_obj(objs, BOX_TEXT)]
    cur = sel
    for _ in range(4):
        cur = po.move_objects(pdf, 0, cur, 1, 0, merge=True)
    assert len(pdf.history._undo) == 1
    assert_rect(cur[0].rect, sel[0].rect + (4, 0, 4, 0))
    assert_rect(cur[1].rect, sel[1].rect + (4, 0, 4, 0), tol=1.0)
    po.move_objects(pdf, 0, cur, 0, 1, merge=False)                          # a new step
    assert len(pdf.history._undo) == 2
    pdf.undo()
    pdf.undo()
    assert_rect(po.find_again(pdf.doc, 0, sel[0]).rect, sel[0].rect)


def test_text_is_kept_on_the_page_and_everything_moves_alike():
    pdf = scene()
    objs = po.page_objects(pdf.doc, 0)
    sel = [find(objs, "graphic", BOX + (-1, -1, 1, 1)), text_obj(objs, BOX_TEXT)]
    moved = po.move_objects(pdf, 0, sel, -500, 0)
    dx = moved[1].rect.x0 - sel[1].rect.x0
    assert -100 < dx < -90 and moved[1].rect.x0 >= -0.5                    # stopped at the page edge
    assert_rect(moved[0].rect, sel[0].rect + (dx, 0, dx, 0), tol=1.0)


def test_delete_mixed_objects():
    pdf = scene()
    objs = po.page_objects(pdf.doc, 0)
    sel = [find(objs, "graphic", BOX + (-1, -1, 1, 1)), text_obj(objs, BOX_TEXT), find(objs, "image", IMG)]
    po.delete_objects(pdf, 0, sel)
    left = po.page_objects(pdf.doc, 0)
    assert len(left) == len(objs) - 3
    assert BOX_TEXT not in page_text(pdf.doc[0]) and BODY_TEXT in page_text(pdf.doc[0])
    assert ob.list_images(pdf.doc, 0) == []
    assert len(pdf.history._undo) == 1 and pdf.history.undo_label == "Slet"
    pdf.undo()
    assert len(po.page_objects(pdf.doc, 0)) == len(objs)


def test_delete_several_images():
    doc = pymupdf.open()
    pg = doc.new_page()
    for k in range(3):
        pg.insert_image(pymupdf.Rect(50 + 150 * k, 100, 150 + 150 * k, 200), stream=make_png(10, 10, (k * 80, 0, 0)))
    pdf = PdfDocument.from_bytes(doc.tobytes())
    imgs = po.page_objects(pdf.doc, 0)
    po.delete_objects(pdf, 0, [imgs[0], imgs[2]])
    (left,) = ob.list_images(pdf.doc, 0)
    assert_rect(left.rect, imgs[1].rect)


def test_scale_graphics_and_image_keeps_stroke_width():
    pdf = scene()
    objs = po.page_objects(pdf.doc, 0)
    box = find(objs, "graphic", BOX + (-1, -1, 1, 1))
    img = find(objs, "image", IMG)
    old = box.rect | img.rect
    new = pymupdf.Rect(old.x0, old.y0, old.x0 + old.width * 1.5, old.y0 + old.height * 2)
    sbox, simg = po.scale_objects(pdf, 0, [box, img], old, new)
    assert len(pdf.history._undo) == 1 and pdf.history.undo_label == "Skalér"
    assert sbox.ref.width == pytest.approx(box.ref.width)                 # O8
    assert simg.rect.x1 == pytest.approx(new.x1, abs=1.5) and simg.rect.y1 == pytest.approx(new.y1, abs=2.5)
    assert simg.rect.width == pytest.approx(img.rect.width * 1.5, rel=0.02)
    # the box spans the union's height
    assert (sbox.rect.x0, sbox.rect.y0, sbox.rect.y1) == pytest.approx((new.x0, new.y0, new.y1), abs=0.6)
    # text cannot be scaled; nothing changes
    with pytest.raises(ValueError):
        po.scale_objects(pdf, 0, [sbox, text_obj(objs, BOX_TEXT)], sbox.rect, sbox.rect + (0, 0, 10, 10))
    assert len(pdf.history._undo) == 1
    with pytest.raises(ValueError):
        po.scale_objects(pdf, 0, [sbox], sbox.rect, (10, 10, 10, 20))


def test_set_text_width():
    pdf = PdfDocument.from_bytes(report(chapters=1).tobytes())
    para = next(o for o in po.page_objects(pdf.doc, 1) if o.kind == "text" and o.ref.story_kind == "flow"
                and len(o.ref.lines) >= 3)
    narrow = po.set_text_width(pdf, 1, para, para.rect.width * 0.6)
    assert narrow.kind == "text" and len(narrow.ref.lines) > len(para.ref.lines)
    assert len(pdf.history._undo) == 1 and pdf.history.undo_label == "Skift bredde"
    g = scene()
    with pytest.raises(ValueError):
        po.set_text_width(g, 0, by_kind(po.page_objects(g.doc, 0), "graphic")[0], 100)
    assert not g.history.can_undo


def test_set_style_changes_graphics_only():
    pdf = scene()
    objs = po.page_objects(pdf.doc, 0)
    rule = next(o for o in objs if o.kind == "graphic" and o.ref.stroke == (1, 0, 0))
    txt = text_obj(objs, BOX_TEXT)
    out = po.set_style(pdf, 0, [rule, txt], stroke=(0, 0, 1), width=2.0)
    assert [o.kind for o in out] == ["graphic", "text"]
    assert out[0].ref.stroke == (0, 0, 1) and out[0].ref.width == pytest.approx(2.0)
    assert out[1].key == txt.key
    assert len(pdf.history._undo) == 1 and pdf.history.undo_label == "Skift udseende"
    # nothing to change / no graphics: no step
    po.set_style(pdf, 0, [txt], stroke=(0, 1, 0))
    po.set_style(pdf, 0, out)
    assert len(pdf.history._undo) == 1


def test_restack_each_kind():
    doc = pymupdf.open()
    pg = doc.new_page()
    pg.insert_text((100, 130), "Tekst under kassen", fontname="helv", fontsize=20)
    pg.insert_image(pymupdf.Rect(300, 300, 400, 400), stream=make_png(10, 10, (255, 0, 0)))
    pg.draw_rect(pymupdf.Rect(90, 100, 420, 420), color=None, fill=(0, 0, 1))   # covers both
    pdf = PdfDocument.from_bytes(doc.tobytes())
    txt_rect = pymupdf.Rect(100, 110, 300, 135)

    def dark_text() -> int:
        p = pdf.doc[0].get_pixmap(clip=txt_rect, colorspace=pymupdf.csGRAY)
        return sum(1 for v in p.samples if v < 20)

    assert near(at(pix(pdf.doc), 350, 350), (0, 0, 255)) and dark_text() == 0
    objs = po.page_objects(pdf.doc, 0)
    img, txt, box = by_kind(objs, "image")[0], by_kind(objs, "text")[0], by_kind(objs, "graphic")[0]
    (i2,) = po.restack(pdf, 0, [img], front=True)
    assert near(at(pix(pdf.doc), 350, 350), (255, 0, 0)) and i2.kind == "image"
    (t2,) = po.restack(pdf, 0, [txt], front=True)
    assert dark_text() > 50 and t2.kind == "text"
    (b2,) = po.restack(pdf, 0, [box], front=True)
    assert near(at(pix(pdf.doc), 350, 350), (0, 0, 255)) and dark_text() == 0
    assert pdf.history.undo_label == "Læg forrest"
    po.restack(pdf, 0, [b2], front=False)
    assert near(at(pix(pdf.doc), 350, 350), (255, 0, 0)) and dark_text() > 50
    assert pdf.history.undo_label == "Læg bagerst" and len(pdf.history._undo) == 4
    # a mixed selection: one step; the box stays below its text
    objs = po.page_objects(pdf.doc, 0)
    every = po.restack(pdf, 0, objs, front=True)
    assert len(every) == 3 and len(pdf.history._undo) == 5
    assert dark_text() > 50 and near(at(pix(pdf.doc), 350, 350), (255, 0, 0))
    every = po.restack(pdf, 0, every, front=False)
    assert len(every) == 3 and dark_text() > 50 and near(at(pix(pdf.doc), 350, 350), (255, 0, 0))


# -- rendering ---------------------------------------------------------------------
def test_render_without_hides_only_the_objects():
    pdf = scene()
    objs = po.page_objects(pdf.doc, 0)
    sel = [find(objs, "graphic", SMALL), text_obj(objs, BOX_TEXT), find(objs, "image", IMG)]
    before = pix(pdf.doc)
    contents = pdf.doc[0].read_contents()
    p = po.render_without(pdf.doc, 0, sel, 1.0)
    assert near(at(p, 420, 420), (204, 204, 204))                         # the big square shows through
    assert near(at(p, 440, 160), (255, 255, 255))                         # no image
    assert near(at(p, 200, 190), (255, 230, 204))                         # the box is still there
    gray = p.pixel(0, 0)
    assert gray[:3] == (255, 255, 255)
    # nothing else changed: compare outside the removed objects
    boxes = [o.rect + (-2, -2, 2, 2) for o in sel]
    sa, sb = before.samples, p.samples
    for y in range(0, before.height, 3):
        for x in range(0, before.width, 3):
            if any(b.contains(pymupdf.Point(x + 0.5, y + 0.5)) for b in boxes):
                continue
            o = (y * before.width + x) * 3
            assert sa[o:o + 3] == sb[o:o + 3], (x, y)
    assert pdf.doc[0].read_contents() == contents and not pdf.history.can_undo
    clipped = po.render_without(pdf.doc, 0, sel, 2.0, clip=SMALL)
    assert (clipped.x, clipped.y) == (800, 800)


def test_render_only_is_transparent_elsewhere():
    pdf = scene()
    objs = po.page_objects(pdf.doc, 0)
    sel = [find(objs, "graphic", BOX + (-1, -1, 1, 1)), text_obj(objs, BOX_TEXT)]
    p = po.render_only(pdf.doc, 0, sel, 1.0)
    assert p.alpha and p.n == 4
    assert p.pixel(500, 700)[3] == 0                                      # paper: transparent
    assert p.pixel(420, 420)[3] == 0                                      # unselected square: not drawn
    assert p.pixel(440, 160)[3] == 0                                      # unselected image: not drawn
    inside = p.pixel(200, 190)
    assert inside[3] == 255 and near(inside[:3], (255, 230, 204))         # the box
    # the text is drawn
    tp = po.render_only(pdf.doc, 0, sel, 1.0, clip=sel[1].rect)
    assert any(tp.samples[i + 3] > 200 and tp.samples[i] < 100 for i in range(0, len(tp.samples), 4))
    assert not pdf.history.can_undo


# -- clipboard ---------------------------------------------------------------------
def test_copy_builds_a_one_page_pdf_of_the_selection():
    pdf = scene()
    objs = po.page_objects(pdf.doc, 0)
    sel = [find(objs, "graphic", BOX + (-1, -1, 1, 1)), text_obj(objs, BOX_TEXT)]
    clip = po.copy_objects(pdf.doc, 0, sel)
    assert_rect(clip.rect, sel[0].rect | sel[1].rect)
    assert clip.page_size == (595, 842)
    d = pymupdf.open("pdf", clip.data)
    assert d.page_count == 1
    text = page_text(d[0])
    assert BOX_TEXT in text and BODY_TEXT not in text
    assert len(gr.list_graphics(d, 0)) == 1 and ob.list_images(d, 0) == []
    assert d[0].first_annot is None
    with pytest.raises(ValueError):
        po.copy_objects(pdf.doc, 0, [])


def test_underlined_text_carries_its_stroke():
    doc = pymupdf.open()
    pg = doc.new_page()
    pg.insert_text((72, 200), "Understreget tekst", fontname="helv", fontsize=12)
    w = pymupdf.get_text_length("Understreget tekst", "helv", 12)
    pg.draw_line((72, 202), (72 + w, 202), color=(0, 0, 0), width=0.8)
    pg.draw_line((72, 400), (300, 400), color=(1, 0, 0), width=0.8)          # a rule of its own
    pdf = PdfDocument.from_bytes(doc.tobytes())
    objs = po.page_objects(pdf.doc, 0)
    assert sorted(o.kind for o in objs) == ["graphic", "text"]                # the underline is part of the text
    assert by_kind(objs, "graphic")[0].ref.stroke == (1, 0, 0)
    assert len(po.objects_in(pdf.doc, 0, (60, 180, 200, 210))) == 1
    txt = by_kind(objs, "text")[0]
    clip = po.copy_objects(pdf.doc, 0, [txt])
    lines = pymupdf.open("pdf", clip.data)[0].get_drawings()
    assert len(lines) == 1 and lines[0]["color"] == (0, 0, 0)
    dst = blank()
    (n,) = po.paste_clip(dst, 0, clip, offset=(0, 100))
    assert n.kind == "text" and len(dst.doc[0].get_drawings()) == 1
    assert dst.doc[0].get_drawings()[0]["rect"].y0 == pytest.approx(302, abs=0.6)
    p = po.render_without(pdf.doc, 0, [txt], 1.0, clip=(60, 180, 200, 210))
    assert all(v > 200 for v in p.samples)                                  # text and stroke gone


def test_paste_on_the_same_page_with_offset():
    pdf = scene()
    objs = po.page_objects(pdf.doc, 0)
    sel = [find(objs, "graphic", BOX + (-1, -1, 1, 1)), text_obj(objs, BOX_TEXT), find(objs, "image", IMG)]
    clip = po.copy_objects(pdf.doc, 0, sel)
    original = pix(pdf.doc, clip=clip.rect)
    new = po.paste_clip(pdf, 0, clip, offset=(0, 420))
    assert sorted(o.kind for o in new) == ["graphic", "image", "text"]
    assert len(pdf.history._undo) == 1 and pdf.history.undo_label == "Indsæt"
    for o in sel:
        twin = next(n for n in new if n.kind == o.kind)
        assert_rect(twin.rect, o.rect + (0, 420, 0, 420), tol=1.0)
    assert len(po.page_objects(pdf.doc, 0)) == len(objs) + 3
    # the pasted area looks like the original (the grey square is not under it)
    pasted = pix(pdf.doc, clip=clip.rect + (0, 420, 0, 420))
    assert mismatch(original, pasted) < 0.002
    # flattened: no form left, the content is the page's own
    assert b"fzFrm" not in pdf.doc[0].read_contents()
    assert all(not o.ref.in_form for o in new if o.kind in ("graphic", "image"))
    # pasted text is standalone and editable/movable
    t = next(n for n in new if n.kind == "text")
    assert t.ref.free and tx(t) == tx(text_obj(objs, BOX_TEXT))
    (mt,) = po.move_objects(pdf, 0, [t], 10, 0)
    assert_rect(mt.rect, t.rect + (10, 0, 10, 0), tol=1.0)
    assert page_text(pdf.doc[0]).count(BOX_TEXT) == 2
    pdf.undo()
    pdf.undo()
    assert len(po.page_objects(pdf.doc, 0)) == len(objs)


def test_paste_on_top_of_text_never_frees_the_page_text():
    pdf = scene()
    objs = po.page_objects(pdf.doc, 0)
    src, body = text_obj(objs, BOX_TEXT), text_obj(objs, BODY_TEXT)
    clip = po.copy_objects(pdf.doc, 0, [src])
    # half a line below the body text: the text engine reads both lines as one
    # paragraph, so nothing is marked standalone - the page's own text never is
    po.paste_clip(pdf, 0, clip, offset=(body.rect.x0 - src.rect.x0 + 20, body.rect.y0 - src.rect.y0 + 6))
    assert page_text(pdf.doc[0]).count(BOX_TEXT) == 2
    assert not any(o.ref.free for o in po.page_objects(pdf.doc, 0) if o.kind == "text" and BODY_TEXT in tx(o))


def test_paste_reuses_identical_resources_on_the_same_page():
    pdf = scene()
    fonts_before = {f[4] for f in pdf.doc[0].get_fonts()}
    objs = po.page_objects(pdf.doc, 0)
    clip = po.copy_objects(pdf.doc, 0, [text_obj(objs, BOX_TEXT)])
    po.paste_clip(pdf, 0, clip, offset=(0, 30))
    po.paste_clip(pdf, 0, clip, offset=(0, 60))
    xobjs = pdf.doc[0].get_xobjects()
    assert not any(x[1].startswith("fzFrm") for x in xobjs)              # forms dissolved
    # the copied font is recognised as the page's own: same name, no second font
    assert {f[4] for f in pdf.doc[0].get_fonts()} == fonts_before
    assert b"_1 " not in pdf.doc[0].read_contents()
    assert page_text(pdf.doc[0]).count(BOX_TEXT) == 3


def test_paste_to_another_page():
    pdf = scene(pages=2)
    objs = po.page_objects(pdf.doc, 0)
    sel = [find(objs, "graphic", BOX + (-1, -1, 1, 1)), text_obj(objs, BOX_TEXT)]
    clip = po.copy_objects(pdf.doc, 0, sel)
    new = po.paste_clip(pdf, 1, clip)
    assert sorted(o.kind for o in new) == ["graphic", "text"]
    for o in sel:
        twin = next(n for n in new if n.kind == o.kind)
        assert_rect(twin.rect, o.rect, tol=1.0)                           # the same place on page 2
    assert mismatch(pix(pdf.doc, 0, clip=clip.rect), pix(pdf.doc, 1, clip=clip.rect)) < 0.002
    assert BOX_TEXT in page_text(pdf.doc[1])
    assert len(po.page_objects(pdf.doc, 0)) == len(objs)                   # the source page is unchanged


def test_paste_into_another_document_fonts_travel(tmp_path):
    src = scene()
    objs = po.page_objects(src.doc, 0)
    sel = [find(objs, "graphic", BOX + (-1, -1, 1, 1)), text_obj(objs, BOX_TEXT), find(objs, "image", IMG)]
    clip = po.copy_objects(src.doc, 0, sel)
    src.close()
    dst = blank()
    new = po.paste_clip(dst, 0, clip, offset=(20, 300))
    assert sorted(o.kind for o in new) == ["graphic", "image", "text"]
    t = next(n for n in new if n.kind == "text")
    if SANS_TTF:
        assert any("LiberationSans" in f[3].replace(" ", "") or "Arial" in f[3] for f in dst.doc[0].get_fonts())
    # save, reopen: the text is real text in the travelled font, and can be moved
    path = str(tmp_path / "dst.pdf")
    dst.save(path)
    again = PdfDocument.open(path)
    assert BOX_TEXT in page_text(again.doc[0])
    tt = po.find_again(again.doc, 0, t)
    assert tt is not None and tt.kind == "text"
    moved = tob.move_text(again, 0, [tt.ref], 0, 100)
    assert moved and tx(moved[0]) == tx(tt)
    assert_rect(moved[0].rect, tt.rect + (0, 100, 0, 100), tol=1.0)
    assert BOX_TEXT in page_text(again.doc[0])
    # the pasted graphic and image are first-class objects
    g = next(n for n in new if n.kind == "graphic")
    assert gr.find_graphic(again.doc, 0, g.ref) is not None
    (im,) = ob.list_images(again.doc, 0)
    assert_rect(im.rect, IMG + (20, 300, 20, 300), tol=1.0)
    again.close()


@pytest.mark.parametrize("src_rot,dst_rot", [(0, 90), (0, 180), (0, 270), (90, 0), (270, 90)])
def test_paste_between_pages_with_different_rotation(src_rot, dst_rot):
    src = scene(rotation=src_rot)
    objs = po.page_objects(src.doc, 0)
    box = next(o for o in objs if o.kind == "graphic" and o.ref.fill == (1.0, 0.9, 0.8))
    txt = next(o for o in objs if o.kind == "text" and BOX_TEXT in tx(o))
    img = by_kind(objs, "image")[0]
    sel = [box, txt, img]
    clip = po.copy_objects(src.doc, 0, sel)
    w, h = src.doc[0].rect.width, src.doc[0].rect.height
    # a target page with the same display size, whatever its /Rotate
    dw, dh = (w, h) if dst_rot in (0, 180) else (h, w)
    dst = blank(dw, dh, dst_rot)
    assert dst.doc[0].rect.width == pytest.approx(w)
    new = po.paste_clip(dst, 0, clip, offset=(5, 7))
    assert sorted(o.kind for o in new) == ["graphic", "image", "text"]
    for o in sel:
        twin = next(n for n in new if n.kind == o.kind)
        assert_rect(twin.rect, o.rect + (5, 7, 5, 7), tol=1.5)
    a = pix(src.doc, clip=clip.rect)
    b = pix(dst.doc, clip=clip.rect + (5, 7, 5, 7))
    assert mismatch(a, b) < 0.01
    tt = next(n for n in new if n.kind == "text")
    assert tx(tt) == tx(txt)


def test_paste_onto_a_smaller_page_and_off_the_page_clip():
    src = scene()
    objs = po.page_objects(src.doc, 0)
    small = find(objs, "graphic", SMALL)
    clip = po.copy_objects(src.doc, 0, [small])
    dst = blank(300, 300)
    (n,) = po.paste_clip(dst, 0, clip, offset=(-300, -300))
    assert_rect(n.rect, SMALL + (-300, -300, -300, -300))
    assert near(at(pix(dst.doc), 120, 120), (0, 178, 0))


def test_paste_renames_colliding_resource_names():
    # source: /F1 = Helvetica, /Im1 = red; target: /F1 = Times, /Im1 = blue
    def one(font, rgb, text, y):
        d = pymupdf.open()
        pg = d.new_page()
        fx = pg.insert_font(fontname=font)
        ix = pg.insert_image(pymupdf.Rect(0, 0, 1, 1), stream=make_png(10, 10, rgb))
        d.xref_set_key(pg.xref, "Resources", f"<</Font<</F1 {fx} 0 R>>/XObject<</Im1 {ix} 0 R>>>>")
        content = (f"BT /F1 14 Tf 72 {y} Td ({text}) Tj ET\nq 100 0 0 100 300 {y - 100} cm /Im1 Do Q\n").encode()
        cx = d.get_new_xref()
        d.update_object(cx, "<<>>")
        d.update_stream(cx, content)
        d.xref_set_key(pg.xref, "Contents", f"{cx} 0 R")
        return PdfDocument.from_bytes(d.tobytes())

    src = one("helv", (255, 0, 0), "Kilde Helvetica", 700)
    dst = one("tiro", (0, 0, 255), "Maal Times", 300)
    clip = po.copy_objects(src.doc, 0, po.page_objects(src.doc, 0))
    new = po.paste_clip(dst, 0, clip)
    assert sorted(o.kind for o in new) == ["image", "text"]
    p = pix(dst.doc)
    assert near(at(p, 350, 842 - 650), (255, 0, 0))                     # pasted image: red
    assert near(at(p, 350, 842 - 250), (0, 0, 255))                     # own image: still blue
    fonts = {f[3] for f in dst.doc[0].get_fonts()}
    assert {"Helvetica", "Times-Roman"} <= fonts
    spans = {s["text"]: s["font"] for b in dst.doc[0].get_text("dict")["blocks"] for ln in b.get("lines", [])
             for s in ln["spans"]}
    assert spans["Kilde Helvetica"].startswith("Helvetica") and spans["Maal Times"].startswith("Times")
    names = [x[1] for x in dst.doc[0].get_xobjects()] + [i[7] for i in dst.doc[0].get_images()]
    assert "Im1" in names and not any(n.startswith("fzFrm") for n in names)


def test_paste_onto_a_page_that_leaves_its_state_changed():
    # a producer that scales the page and sets a colour outside any q … Q
    d = pymupdf.open()
    pg = d.new_page()
    cx = d.get_new_xref()
    d.update_object(cx, "<<>>")
    d.update_stream(cx, b"0.5 0 0 0.5 0 0 cm 0 0 1 rg 100 100 200 200 re f 1 0 0 RG 20 w\n")
    d.xref_set_key(pg.xref, "Contents", f"{cx} 0 R")
    dst = PdfDocument.from_bytes(d.tobytes())
    src = scene()
    small = find(po.page_objects(src.doc, 0), "graphic", SMALL)
    clip = po.copy_objects(src.doc, 0, [small])
    (n,) = po.paste_clip(dst, 0, clip)
    assert_rect(n.rect, SMALL)
    p = pix(dst.doc)
    assert near(at(p, 420, 420), (0, 178, 0))
    assert near(at(p, 100, 742), (0, 0, 255))                            # the page's own drawing is unchanged


def test_scan_names_finds_every_resource_operand():
    content = (b"/GS1 gs /CS0 cs 0.5 scn /DeviceRGB CS 1 0 0 SCN /P1 cs /Pat1 scn\n"
               b"BT /F1 12 Tf (a /F2 Tf in a string \\) still) Tj ET\n"
               b"/Span <</MCID 1>> BDC EMC /OC /MC0 BDC EMC /Sh1 sh\n"
               b"q 10 0 0 10 0 0 cm BI /W 2 /H 1 /CS /CS9 /BPC 8 ID \x00\xff EI Q\n"
               b"q /Im1 Do Q Q")
    occ, under, depth = po._scan_names(content)
    got = [(c, n) for c, n, _s, _e in occ]
    assert got == [("ExtGState", "GS1"), ("ColorSpace", "CS0"), ("ColorSpace", "P1"), ("Pattern", "Pat1"),
                   ("Font", "F1"), ("Properties", "MC0"), ("Shading", "Sh1"), ("ColorSpace", "CS9"),
                   ("XObject", "Im1")]
    for _c, n, s, e in occ:
        assert content[s:e] == b"/" + n.encode()
    assert (under, depth) == (1, 0)
    assert po._scan_names(b"q q 1 0 0 rg")[1:] == (0, 2)


def test_parse_dict():
    items = po._parse_dict(r"<</A 1 0 R/B[/ICCBased 5 0 R]/C<</X<</Y 1>>>>/D /Foo/E (a\)b>>) /F 3/G true>>")
    assert items == [("A", "1 0 R"), ("B", "[/ICCBased 5 0 R]"), ("C", "<</X<</Y 1>>>>"), ("D", "/Foo"),
                     ("E", r"(a\)b>>)"), ("F", "3"), ("G", "true")]
    assert po._parse_dict("<<>>") == [] and po._parse_dict("/Foo") == []
    with pytest.raises(ValueError):
        po._parse_dict("<</A [1 2>> 3]>>")


def test_pasted_transparency_and_inline_images_keep_their_resources():
    content = (b"q /GS1 gs 1 0 0 rg 100 600 200 100 re f Q\n"
               b"q 50 0 0 50 400 600 cm BI /W 1 /H 1 /CS /CSx /BPC 8 ID \x00 EI Q\n")
    d = pymupdf.open()
    pg = d.new_page()
    d.xref_set_key(pg.xref, "Resources", "<</ExtGState<</GS1<</ca 0.5>>>>/ColorSpace<</CSx[/Indexed/DeviceRGB 0 <00ff00>]>>>>")
    cx = d.get_new_xref()
    d.update_object(cx, "<<>>")
    d.update_stream(cx, content)
    d.xref_set_key(pg.xref, "Contents", f"{cx} 0 R")
    src = PdfDocument.from_bytes(d.tobytes())
    objs = po.page_objects(src.doc, 0)
    assert sorted(o.kind for o in objs) == ["graphic", "image"]
    clip = po.copy_objects(src.doc, 0, objs)
    # target: its own, different GS1 and CSx
    t = pymupdf.open()
    tp = t.new_page()
    t.xref_set_key(tp.xref, "Resources", "<</ExtGState<</GS1<</ca 1>>>>/ColorSpace<</CSx/DeviceGray>>>>")
    dst = PdfDocument.from_bytes(t.tobytes())
    new = po.paste_clip(dst, 0, clip)
    assert sorted(o.kind for o in new) == ["graphic", "image"]
    a, b = pix(src.doc), pix(dst.doc)
    assert near(at(b, 200, 192), (255, 127, 127), tol=8)                   # still half transparent
    assert near(at(b, 425, 217), (0, 255, 0), tol=8)                       # the indexed colour
    assert mismatch(a, b) == 0
    gs = po._entries(dst.doc, dst.doc[0].xref, "Resources/ExtGState")
    assert len(gs) == 2 and gs["GS1"].replace(" ", "") == "<</ca1>>"


def test_flatten_failure_keeps_the_form(monkeypatch):
    src = scene()
    objs = po.page_objects(src.doc, 0)
    sel = [find(objs, "graphic", BOX + (-1, -1, 1, 1)), text_obj(objs, BOX_TEXT)]
    clip = po.copy_objects(src.doc, 0, sel)

    def boom(*a, **k):
        raise RuntimeError("no")

    monkeypatch.setattr(po, "_flatten", boom)
    dst = blank(842, 595, 90)
    po.paste_clip(dst, 0, clip, offset=(0, 400))
    assert b"fzFrm" in dst.doc[0].read_contents()
    a = pix(src.doc, clip=clip.rect)
    b = pix(dst.doc, clip=clip.rect + (0, 400, 0, 400))
    assert mismatch(a, b) < 0.01                                          # the form is placed exactly
    assert len(dst.history._undo) == 1


def test_duplicate_is_one_step():
    pdf = scene()
    objs = po.page_objects(pdf.doc, 0)
    # (overlapping copies are covered by test_duplicate_callout_box_is_a_new_object_of_its_own)
    sel = [find(objs, "graphic", SMALL), text_obj(objs, BOX_TEXT)]
    copies = po.duplicate_objects(pdf, 0, sel)
    assert sorted(o.kind for o in copies) == ["graphic", "text"]
    for o in sel:
        twin = next(n for n in copies if n.kind == o.kind)
        assert_rect(twin.rect, o.rect + (10, 10, 10, 10), tol=1.0)
    assert len(pdf.history._undo) == 1 and pdf.history.undo_label == "Duplikér"
    # the copy is an object of its own, not grouped with the original or the big square
    g = next(n for n in copies if n.kind == "graphic")
    assert not set(g.ref.paths) & set(sel[0].ref.paths)
    assert len(by_kind(po.page_objects(pdf.doc, 0), "graphic")) == len(by_kind(objs, "graphic")) + 1
    assert po.find_again(pdf.doc, 0, sel[0]).key == sel[0].key
    box = find(objs, "graphic", BOX + (-1, -1, 1, 1))
    (bc,) = po.duplicate_objects(pdf, 0, [box], offset=(0, 400))
    assert_rect(bc.rect, box.rect + (0, 400, 0, 400)) and bc.ref.paths != box.ref.paths
    assert len(bc.ref.paths) == 2                                          # fill + border, as the original
    pdf.undo()
    pdf.undo()
    assert len(po.page_objects(pdf.doc, 0)) == len(objs)
    assert po.duplicate_objects(pdf, 0, []) == []


CALLOUT = pymupdf.Rect(60, 240, 540, 310)


BODY_LINES = ["Selskabet fastholdt arbejdsmiljøet på fabrikkerne trods et",
              "udfordrende år, og sygefraværet faldt for tredje år i træk,",
              "mens antallet af arbejdsulykker blev halveret siden sidste år."]


def callout_doc(rotation: int = 0, body: bool = False) -> PdfDocument:
    """A LibreOffice-style callout box: the fill, the box text, then the
    border as four separate lines (one graphic object of five paths)."""
    doc = pymupdf.open()
    pg = doc.new_page(width=595, height=842)
    r = CALLOUT
    pg.draw_rect(r, color=None, fill=(0.89, 0.93, 0.96))
    pg.insert_text((r.x0 + 12, r.y0 + 30), BOX_TEXT, fontname="helv", fontsize=12)
    for a, b in ((r.tl, r.tr), (r.tr, r.br), (r.br, r.bl), (r.bl, r.tl)):
        pg.draw_line(a, b, color=(0.24, 0.49, 0.67), width=0.75)
    if body:
        for i, line in enumerate(BODY_LINES):
            pg.insert_text((60, 420 + i * 15), line, fontname="helv", fontsize=11)
        pg.draw_line((60, 520), (540, 520), color=(0, 0, 0), width=0.8)    # a full-width rule
    if rotation:
        pg.set_rotation(rotation)
    return PdfDocument.from_bytes(doc.tobytes())


def graphics_of(pdf, page=0):
    return by_kind(po.page_objects(pdf.doc, page), "graphic")


@pytest.mark.parametrize("rotation", [0, 90, 180, 270])
def test_duplicate_callout_box_is_a_new_object_of_its_own(rotation):
    pdf = callout_doc(rotation)
    (box,) = graphics_of(pdf)
    assert len(box.ref.paths) == 5
    for offset in ((10, 10), (0, 40), (40, 0)):
        (copy,) = po.duplicate_objects(pdf, 0, [box], offset=offset)
        assert copy.kind == "graphic" and len(copy.ref.paths) == 5
        assert not set(copy.ref.paths) & set(box.ref.paths)
        assert_rect(copy.rect, box.rect + (offset * 2), tol=1.0)
        now = graphics_of(pdf)
        assert len(now) == 2 and po.find_again(pdf.doc, 0, box).key == box.key
        # no table: the overlapping frames stay two selectable shapes
        assert "table" not in {g.kind for g in gr.list_graphics(pdf.doc, 0, all_items=True)}
        # the copy moves on its own
        (moved,) = po.move_objects(pdf, 0, [copy], 20, 0)
        assert_rect(moved.rect, copy.rect + (20, 0, 20, 0), tol=1.0)
        assert po.find_again(pdf.doc, 0, box).key == box.key
        assert len(graphics_of(pdf)) == 2
        pdf.undo()
        pdf.undo()
        assert graphics_of(pdf)[0].key == box.key and len(graphics_of(pdf)) == 1


@pytest.mark.parametrize("rotation", [0, 90])
def test_paste_exactly_on_top_of_the_original(rotation):
    pdf = scene(rotation)
    before = graphics_of(pdf)
    for g in before:
        new = po.paste_clip(pdf, 0, po.copy_objects(pdf.doc, 0, [g]), offset=(0, 0))
        assert len(new) == 1 and new[0].kind == "graphic", (g.rect, [(o.kind, o.rect) for o in new])
        assert_rect(new[0].rect, g.rect) and not set(new[0].ref.paths) & set(g.ref.paths)
        assert len(graphics_of(pdf)) == len(before) + 1
        assert po.find_again(pdf.doc, 0, g).key == g.key
        (moved,) = po.move_objects(pdf, 0, new, 0, 30)
        assert_rect(moved.rect, g.rect + (0, 30, 0, 30), tol=1.0)
        assert po.find_again(pdf.doc, 0, g).key == g.key
        pdf.undo()
        pdf.undo()
    pdf = callout_doc(rotation)
    (box,) = graphics_of(pdf)
    (copy,) = po.duplicate_objects(pdf, 0, [box], offset=(0, 0))
    assert len(copy.ref.paths) == 5 and len(graphics_of(pdf)) == 2


def test_restacking_next_to_a_pasted_copy_keeps_both_objects():
    for offset in ((0, 0), (0, 40)):
        for front in (True, False):
            pdf = callout_doc()
            (box,) = graphics_of(pdf)
            (copy,) = po.duplicate_objects(pdf, 0, [box], offset=offset)
            # the original to the front (after the pasted block) / the copy to the back
            target = po.find_again(pdf.doc, 0, box) if front else copy
            (res,) = po.restack(pdf, 0, [target], front=front)
            assert_rect(res.rect, target.rect) and len(res.ref.paths) == 5
            assert len(graphics_of(pdf)) == 2, (offset, front)


def assert_two_boxes(pdf, box, step):
    """The original callout and its copy: two separate selectable shapes
    (no table), and the copy moves on its own."""
    boxes = [g for g in graphics_of(pdf) if len(g.ref.paths) == 5]
    assert len(boxes) == 2 and len(graphics_of(pdf)) == 3, step                # + the rule
    assert "table" not in {g.kind for g in gr.list_graphics(pdf.doc, 0, all_items=True)}, step
    orig = min(boxes, key=lambda g: abs(g.rect.y0 - box.rect.y0) + abs(g.rect.x0 - box.rect.x0))
    copy = next(g for g in boxes if g is not orig)
    assert_rect(orig.rect, box.rect, tol=1.0)
    (m,) = po.move_objects(pdf, 0, [copy], 7, 0)
    assert_rect(m.rect, copy.rect + (7, 0, 7, 0), tol=1.0), step
    assert len([g for g in graphics_of(pdf) if len(g.ref.paths) == 5]) == 2, step
    assert_rect(po.find_again(pdf.doc, 0, orig).rect, box.rect, tol=1.0)
    pdf.undo()


@pytest.mark.parametrize("rotation", [0, 90])
def test_pasted_copy_survives_later_edits_of_the_page(rotation):
    from pdfstudio.core import flow
    from pdfstudio.core.document import write_bytes
    pdf = callout_doc(rotation, body=True)
    box = next(g for g in graphics_of(pdf) if len(g.ref.paths) == 5)
    (copy,) = po.duplicate_objects(pdf, 0, [box], offset=(0, 40))
    assert_two_boxes(pdf, box, "duplicate")
    # (a) move an unrelated paragraph (the text removal rewrites the content)
    para = text_obj(po.page_objects(pdf.doc, 0), BODY_LINES[0])
    po.move_objects(pdf, 0, [para], 0, 5)
    assert_two_boxes(pdf, box, "move paragraph")
    # (b) move the box's own text
    t = next(o for o in by_kind(po.page_objects(pdf.doc, 0), "text")
             if norm(BOX_TEXT) in tx(o) and not o.ref.free)
    po.move_objects(pdf, 0, [t], 3, 0)
    assert_two_boxes(pdf, box, "move box text")
    # (c) the flow editor on a paragraph of the page
    para = text_obj(po.page_objects(pdf.doc, 0), BODY_LINES[0])
    hit = flow.story_at(pdf.doc, 0, (para.rect.x0 + 5, para.rect.y0 + 5))
    assert hit is not None
    story, piece = hit
    flow.apply_story(pdf, story, [flow.Edit(piece=piece, text=story.pieces[piece].text + " Endnu en sætning.")])
    assert_two_boxes(pdf, box, "flow")
    # (e) undo / redo
    n = len(pdf.history._undo)
    for _ in range(n):
        pdf.undo()
    assert len(graphics_of(pdf)) == 2                                      # the box and the rule
    for _ in range(n):
        pdf.redo()
    assert_two_boxes(pdf, box, "redo")
    # (d) save and reopen with a cleaned, garbage-collected file
    again = PdfDocument.from_bytes(write_bytes(pdf.doc, garbage=3, clean=True, deflate=True))
    assert_two_boxes(again, box, "reopen")


@pytest.mark.parametrize("rotation", [0, 180])
def test_duplicated_rule_on_a_paragraph_is_no_underline(rotation):
    pdf = callout_doc(rotation, body=True)
    rule = next(g for g in graphics_of(pdf) if g.ref.kind == "line")
    n = len(graphics_of(pdf))
    # onto the paragraph: the copy lies exactly where an underline of its
    # second line would be
    para = text_obj(po.page_objects(pdf.doc, 0), BODY_LINES[0])
    x, y = para.ref.lines[1].raw_origin                                    # unrotated baseline
    target = pymupdf.Point(x, y + 2) * pdf.doc[0].rotation_matrix
    dy = target.y - (rule.rect.y0 + rule.rect.y1) / 2
    (copy,) = po.duplicate_objects(pdf, 0, [rule], offset=(0, dy))
    assert copy.kind == "graphic" and copy.ref.selectable
    assert len(graphics_of(pdf)) == n + 1
    (m,) = po.move_objects(pdf, 0, [copy], 0, 3)
    assert_rect(m.rect, copy.rect + (0, 3, 0, 3), tol=1.0)
    assert len(graphics_of(pdf)) == n + 1


# -- performance (O16) ------------------------------------------------------------------
def test_listing_and_hit_testing_are_fast_after_the_first_time():
    pdf = PdfDocument.from_bytes(report(chapters=2, figure=True).tobytes())
    po.page_objects(pdf.doc, 0)
    t = time.perf_counter()
    for _ in range(10):
        po.page_objects(pdf.doc, 0)
        po.object_at(pdf.doc, 0, (300, 400))
    assert (time.perf_counter() - t) / 10 < 0.05


# -- real-world corpus ------------------------------------------------------------------
@pytest.mark.skipif(not os.path.exists(os.path.join(CORPUS, "strategi.pdf")), reason="corpus not generated")
def test_corpus_paste_a_text_box_onto_chevron_labels():
    pdf = PdfDocument.open(os.path.join(CORPUS, "strategi.pdf"))
    texts = by_kind(po.page_objects(pdf.doc, 3), "text")
    labels = [o for o in texts if o.ref.role == "cell" and len(o.ref.lines) == 1 and len(o.ref.text) < 20]
    assert len(labels) >= 4
    before = {tx(o): pymupdf.Rect(o.rect) for o in labels}
    box = max(texts, key=lambda o: len(o.ref.text))
    clip = po.copy_objects(pdf.doc, 3, [box])
    lab = labels[0]
    new = po.paste_clip(pdf, 3, clip, offset=(lab.rect.x0 - clip.rect.x0, lab.rect.y0 - clip.rect.y0))
    (t,) = by_kind(new, "text")
    assert t.ref.free
    (m,) = po.move_objects(pdf, 3, [t], 0, 60)
    assert_rect(m.rect, t.rect + (0, 60, 0, 60), tol=1.0)
    after = {tx(o): o for o in by_kind(po.page_objects(pdf.doc, 3), "text")}
    for text, r in before.items():
        assert text in after and not after[text].ref.free, text
        assert_rect(after[text].rect, r)
    pdf.close()


def words(page) -> list[str]:
    return sorted(norm(" ".join(w[4] for w in page.get_text("words"))).split())


@pytest.mark.skipif(not os.path.exists(os.path.join(CORPUS, "aarsrapport.pdf")), reason="corpus not generated")
@pytest.mark.parametrize("name,pages", [("aarsrapport.pdf", (0, 2, 20)), ("strategi.pdf", (1, 3))])
def test_corpus_copy_everything_into_a_new_document(name, pages):
    src = PdfDocument.open(os.path.join(CORPUS, name))
    for p in pages:
        objs = po.page_objects(src.doc, p)
        assert objs
        clip = po.copy_objects(src.doc, p, objs)
        w, h = src.doc[p].rect.width, src.doc[p].rect.height
        dst = blank(w, h)
        new = po.paste_clip(dst, 0, clip)
        assert len(new) >= len(objs) * 0.9, (p, len(new), len(objs))
        assert b"fzFrm" not in dst.doc[0].read_contents()
        # every object looks as it did on its page (the clip page holds only them)
        ref = pymupdf.open("pdf", clip.data)
        for o in objs:
            r = (o.rect & src.doc[p].rect)
            if r.is_empty or r.width < 2 or r.height < 2:
                continue
            a = ref[0].get_pixmap(clip=r)
            b = dst.doc[0].get_pixmap(clip=r)
            assert mismatch(a, b) < 0.01, (p, o.kind, o.rect)
        # and the text is the same
        assert words(dst.doc[0]) == words(ref[0])
        # pasted text can be moved again
        t = next((n for n in new if n.kind == "text"), None)
        if t is not None:
            moved = po.move_objects(dst, 0, [t], 10, 10)
            assert moved and moved[0].kind == "text"
        dst.close()
    src.close()


def _box_paragraph_doc(rotation: int = 0) -> PdfDocument:
    doc = pymupdf.open()
    pg = doc.new_page(width=595, height=842)
    pg.draw_rect(pymupdf.Rect(60, 80, 420, 400), color=(0.2, 0.4, 0.6), fill=(0.9, 0.95, 1.0), width=1)
    for k, line in enumerate(("Sådan beregnes nøgletallene", "Nøgletallene er beregnet efter",
                              "anbefalingerne fra Finansforeningen.")):
        pg.insert_text((72, 110 + 14 * k), line, fontname="helv", fontsize=11)
    if rotation:
        pg.set_rotation(rotation)
    return PdfDocument.from_bytes(doc.tobytes())


@pytest.mark.parametrize("rotation", [0, 90])
def test_a_duplicate_right_under_its_original_stays_its_own_object(rotation):
    # the copy continues the paragraph at its line pitch (offset = 3 lines): text
    # extraction alone would read original + copy as one paragraph
    pdf = _box_paragraph_doc(rotation)
    objs = po.page_objects(pdf.doc, 0)
    para = next(o for o in objs if o.kind == "text" and o.ref.text.startswith("Sådan"))
    n = len(objs)
    # 3 lines further along the paragraph, in display space
    off = (0, 42) if rotation == 0 else (-42, 0)
    (copy,) = po.duplicate_objects(pdf, 0, [para], offset=off)
    assert copy.kind == "text" and copy.ref.free
    assert_rect(copy.rect, para.rect + (*off, *off), tol=0.6)
    now = po.page_objects(pdf.doc, 0)
    assert len(now) == n + 1
    orig = po.find_again(pdf.doc, 0, para)
    assert orig is not None and not orig.ref.free
    assert_rect(orig.rect, para.rect, tol=0.6)                     # the original did not absorb the copy
    # the copy moves on its own; the original stays
    (moved,) = po.move_objects(pdf, 0, [copy], 0, 100)
    assert_rect(moved.rect, copy.rect + (0, 100, 0, 100), tol=0.6)
    assert_rect(po.find_again(pdf.doc, 0, para).rect, para.rect, tol=0.6)


@pytest.mark.parametrize("rotation", [90, 180, 270])
def test_copy_a_paragraph_on_a_rotated_page_pastes_exactly_its_words(rotation):
    src = scene(rotation=rotation)
    objs = po.page_objects(src.doc, 0)
    txt = text_obj(objs, BOX_TEXT)
    clip = po.copy_objects(src.doc, 0, [txt])
    w, h = src.doc[0].rect.width, src.doc[0].rect.height
    dst = blank(w, h)
    po.paste_clip(dst, 0, clip)
    got = sorted(wd[4] for wd in dst.doc[0].get_text("words"))
    assert got == sorted(txt.ref.text.split())


def test_paste_balances_the_clips_marked_content():
    assert po._mc_balance(b"EMC /A BMC (EMC) Tj /B <</MCID 1>> BDC EMC BI /W 1 /H 1 /BPC 8 /CS /G ID \x00 EI") == (1, 1)
    src = pymupdf.open()
    sp = src.new_page(width=595, height=842)
    cx = src.get_new_xref()
    src.update_object(cx, "<<>>")
    src.update_stream(cx, b"EMC\nq 1 0 0 rg 100 600 120 80 re f Q\n/Open BMC\n")
    src.xref_set_key(sp.xref, "Contents", f"{cx} 0 R")
    clip = po.Clip(data=src.tobytes(), rect=pymupdf.Rect(100, 162, 220, 242), page_size=(595, 842))
    pdf = callout_doc()
    (box,) = graphics_of(pdf)
    (new,) = po.paste_clip(pdf, 0, clip, offset=(0, 0))
    scan = gr._page_scan(pdf.doc, 0)
    assert scan.conts[0].mc_ok                                             # balanced again
    assert [p.seg for p in scan.paths] == [0] * 5 + [1]
    assert po.find_again(pdf.doc, 0, box).key == box.key and len(graphics_of(pdf)) == 2


@pytest.mark.skipif(not os.path.exists(os.path.join(CORPUS, "aarsrapport.pdf")), reason="corpus not generated")
def test_a_duplicated_list_item_dropped_on_another_leaves_that_item_alone():
    # the copy's bullet landed beside the item two rows down and was taken as its
    # bullet: the page's own item grew by the pasted bullet
    pdf = PdfDocument.open(os.path.join(CORPUS, "aarsrapport.pdf"))

    def seg():
        return sorted((o.kind, tuple(round(v, 1) for v in o.rect), " ".join((getattr(o.ref, "text", "") or "").split()))
                      for o in po.page_objects(pdf.doc, 16))

    objs = po.page_objects(pdf.doc, 16)
    item = next(o for o in objs if o.kind == "text" and o.ref.text.startswith("• Vækst"))
    before = seg()
    copies = po.duplicate_objects(pdf, 16, [item], offset=(0, 40))
    assert len(copies) == 1 and copies[0].ref.free and copies[0].ref.text.startswith("•")
    after = seg()
    for s in [(c.kind, tuple(round(v, 1) for v in c.rect), " ".join(c.ref.text.split())) for c in copies]:
        after.remove(s)
    assert after == before
