"""Text blocks as movable objects (core/textobjects.py): listing, hit-test,
move (glyph for glyph, nothing else changes), list items, headers,
captions, cells, rotated pages and text, undo, O17 (moved text is
standalone), delete, clipboard strip, width, stacking order."""

import time

import pymupdf
import pytest

from pdfstudio.core import PdfDocument, flow
from pdfstudio.core import text_edit as te
from pdfstudio.core import textobjects as to
from tests.conftest import make_sample_pdf
from tests.flowdocs import HEADER, report

EXTRA = " Dette er en ny sætning, som er tilføjet for at se teksten flyde videre."


# -- helpers ------------------------------------------------------------------
def _pdf(doc: pymupdf.Document) -> PdfDocument:
    return PdfDocument(pymupdf.open("pdf", doc.tobytes()))


def column(doc, page) -> flow.Story:
    return next(s for s in flow.page_stories(doc, page) if s.kind == "flow")


def body_objs(doc, page) -> list[to.TextObject]:
    return [o for o in to.list_text(doc, page) if o.story_kind == "flow"]


def gray(page, zoom=1.5) -> pymupdf.Pixmap:
    return page.get_pixmap(matrix=pymupdf.Matrix(zoom, zoom), colorspace=pymupdf.csGRAY, alpha=False,
                           annots=False)


def changed_pixels(a: pymupdf.Pixmap, b: pymupdf.Pixmap, tol: int = 24):
    """(x, y) pixmap pixels that differ by more than ``tol``."""
    assert (a.width, a.height) == (b.width, b.height)
    sa, sb, stride = a.samples, b.samples, a.stride
    out = []
    for y in range(a.height):
        ra, rb = sa[y * stride: y * stride + a.width], sb[y * stride: y * stride + a.width]
        if ra == rb:
            continue
        out.extend((x, y) for x in range(a.width) if abs(ra[x] - rb[x]) > tol)
    return out


def assert_only_changed_in(before: pymupdf.Pixmap, after: pymupdf.Pixmap, rects, zoom=1.5, grow=2.0):
    boxes = [pymupdf.Rect(r) + (-grow, -grow, grow, grow) for r in rects]
    bad = [(x, y) for x, y in changed_pixels(before, after)
           if not any(bx.contains(pymupdf.Point((x + 0.5) / zoom, (y + 0.5) / zoom)) for bx in boxes)]
    assert not bad, f"{len(bad)} pixels changed outside the old/new box, e.g. {bad[:5]}"


def words_in(page, rect) -> str:
    """The words inside a display rect (text extraction works unrotated)."""
    clip = (pymupdf.Rect(rect) + (-1, -1, 1, 1)) * page.derotation_matrix
    return " ".join(w[4] for w in page.get_text("words", clip=clip, sort=True))


def norm(t: str) -> str:
    return " ".join(t.split())


def state(doc, page):
    return doc[page].read_contents(), doc.xref_get_key(doc[page].xref, "PieceInfo")


def styles(obj) -> list[tuple]:
    return sorted({(ln.style.font, round(ln.style.size, 2), tuple(round(c, 3) for c in ln.style.color))
                   for ln in obj.lines})


def table_doc() -> PdfDocument:
    doc = pymupdf.open()
    pg = doc.new_page()
    pg.insert_text((72, 80), "Nøgletal for regnskabsåret", fontname="helv", fontsize=14)
    rows = [("Omsætning", "1.204,5", "1.112,0"), ("Resultat", "98,7", "120,4"), ("Egenkapital", "410,2", "399,9")]
    for k, (label, a, b) in enumerate(rows):
        y = 120 + 18 * k
        pg.insert_text((72, y), label, fontname="helv", fontsize=10)
        for text, right in ((a, 300.0), (b, 420.0)):
            pg.insert_text((right - pymupdf.get_text_length(text, "helv", 10), y), text, fontname="helv", fontsize=10)
    return PdfDocument.from_bytes(doc.tobytes())


@pytest.fixture(scope="module")
def rep():
    return report(chapters=2, figure=True)


@pytest.fixture(scope="module")
def short():
    """One chapter: the last page (1) has three paragraphs and room below."""
    return report(chapters=1)


# -- listing ------------------------------------------------------------------
def test_one_object_per_paragraph_and_per_standalone_text(rep):
    objs = to.list_text(rep, 0)
    stories = flow.page_stories(rep, 0)
    flows = sum(len(s.pieces) for s in stories if s.kind == "flow")
    singles = sum(1 for s in stories if s.kind == "single")
    assert len(objs) == flows + singles
    roles = [o.role for o in objs]
    assert {"body", "header", "footer", "caption"} <= set(roles)
    head = next(o for o in objs if o.role == "header")
    assert head.text == HEADER and head.story_kind == "single" and not head.free
    assert next(o for o in objs if o.role == "footer").text.startswith("Side 1 af")
    items = [o for o in objs if o.bullet is not None]
    assert len(items) == 3
    for o in items:
        assert o.text.startswith("• ")
        assert o.rect.contains(o.bullet.bbox) and o.rect.contains(o.block.bbox)
        assert all(ln in o.lines for ln in o.bullet.lines)
    assert next(o for o in objs if o.role == "body").text.startswith("1. Kapitel")
    # stable identity, cached
    again = to.list_text(rep, 0)
    assert [o.key for o in again] == [o.key for o in objs]
    assert all(to.find_text(rep, 0, o).key == o.key for o in objs)


def test_listing_the_sample_document():
    doc = pymupdf.open("pdf", make_sample_pdf())
    texts = [o.text for o in to.list_text(doc, 0)]
    assert "Kvartalsrapport 2026" in texts
    assert any("Times" in t and "Anden linje" in t for t in texts)
    rot = to.list_text(doc, 2)                       # /Rotate 90
    assert rot and all(o.rect.width > 0 and o.rect.height > 0 for o in rot)


def test_hit_test_and_rubber_band(rep):
    objs = body_objs(rep, 0)
    para = next(o for o in objs if o.bullet is None and len(o.block.lines) > 3)
    ln = para.lines[2]
    hit = to.text_at(rep, 0, ((ln.bbox.x0 + ln.bbox.x1) / 2, (ln.bbox.y0 + ln.bbox.y1) / 2))
    assert hit is not None and hit.key == para.key
    item = next(o for o in objs if o.bullet is not None)
    b = item.bullet.bbox
    assert to.text_at(rep, 0, ((b.x0 + b.x1) / 2, (b.y0 + b.y1) / 2)).key == item.key
    assert to.text_at(rep, 0, (20, 400)) is None          # the margin
    inside = to.text_in(rep, 0, para.rect + (-3, -3, 3, 3))
    assert [o.key for o in inside] == [para.key]
    assert to.text_in(rep, 0, (0, 0, 10, 10)) == []


# -- moving -------------------------------------------------------------------
def test_move_a_paragraph_glyph_for_glyph(short):
    pdf = _pdf(short)
    doc = pdf.doc
    objs = body_objs(doc, 1)
    para = objs[1]
    others = {o.key: pymupdf.Rect(o.rect) for o in to.list_text(doc, 1) if o.key != para.key}
    before = gray(doc[1])
    old = state(doc, 1)
    t = time.perf_counter()
    moved = to.move_text(pdf, 1, [para], 0, 400)
    elapsed = time.perf_counter() - t
    assert elapsed < 1.5
    assert len(moved) == 1
    new = moved[0]
    assert new.text == para.text
    assert new.free and new.role == "free" and new.story_kind == "single"
    assert abs(new.rect.y0 - (para.rect.y0 + 400)) < 0.6 and abs(new.rect.x0 - para.rect.x0) < 0.6
    assert styles(new) == styles(para)                      # same fonts, sizes, colours
    page = pdf.doc[1]
    assert norm(words_in(page, new.rect)) == norm(para.text)   # searchable / extractable where it is now
    hits = page.search_for(para.lines[1].text[:30])
    assert hits and all(h.y0 > para.rect.y1 for h in hits)
    assert words_in(page, para.rect).strip() == ""          # gone from its old place
    # nothing else on the page changed (pixels) – the text below did not move up
    assert_only_changed_in(before, gray(page), [para.rect, new.rect])
    for o in to.list_text(pdf.doc, 1):
        if o.key in others:
            assert o.rect == others.pop(o.key)
    assert not others
    # one undo step restores the page exactly
    assert len(pdf.history._undo) == 1
    assert pdf.undo() and state(pdf.doc, 1) == old


def test_moved_text_has_the_same_look_at_its_new_place(short):
    pdf = _pdf(short)
    para = body_objs(pdf.doc, 1)[1]
    zoom = 2
    old_pix = pdf.doc[1].get_pixmap(matrix=pymupdf.Matrix(zoom, zoom), clip=para.rect, colorspace=pymupdf.csGRAY)
    new = to.move_text(pdf, 1, [para], 0, 300)[0]
    r = pymupdf.Rect(para.rect) + (0, 300, 0, 300)
    new_pix = pdf.doc[1].get_pixmap(matrix=pymupdf.Matrix(zoom, zoom), clip=r, colorspace=pymupdf.csGRAY)
    assert new.rect.y0 == pytest.approx(r.y0, abs=0.5)
    # 300 pt is a whole number of pixels: the glyphs render the same
    assert len(changed_pixels(old_pix, new_pix, tol=60)) < 0.002 * old_pix.width * old_pix.height


def test_move_a_list_item_moves_its_bullet(rep):
    pdf = _pdf(rep)
    item = next(o for o in body_objs(pdf.doc, 0) if o.bullet is not None)
    before = gray(pdf.doc[0])
    new = to.move_text(pdf, 0, [item], 40, -6)[0]
    assert new.bullet is not None and new.free
    assert new.text == item.text
    assert abs(new.rect.x0 - (item.rect.x0 + 40)) < 0.6 and abs(new.rect.y0 - (item.rect.y0 - 6)) < 0.6
    assert abs(new.bullet.bbox.x0 - (item.bullet.bbox.x0 + 40)) < 0.6
    assert "•" in words_in(pdf.doc[0], new.bullet.bbox)
    assert_only_changed_in(before, gray(pdf.doc[0]), [item.rect, new.rect])


@pytest.mark.parametrize("role", ["header", "caption", "footer"])
def test_move_standalone_texts(rep, role):
    pdf = _pdf(rep)
    obj = next(o for o in to.list_text(pdf.doc, 0) if o.role == role)
    before = gray(pdf.doc[0])
    new = to.move_text(pdf, 0, [obj], -30, 8)[0]
    assert new.text == obj.text and new.free
    assert abs(new.rect.x0 - (obj.rect.x0 - 30)) < 0.6 and abs(new.rect.y0 - (obj.rect.y0 + 8)) < 0.6
    assert_only_changed_in(before, gray(pdf.doc[0]), [obj.rect, new.rect])
    # the column is not affected
    assert [p.text for p in column(pdf.doc, 0).pieces] == [p.text for p in column(rep, 0).pieces]


def test_move_a_table_cell():
    pdf = table_doc()
    cells = [o for o in to.list_text(pdf.doc, 0) if o.role == "cell"]
    cell = next(o for o in cells if o.text == "98,7")
    before = gray(pdf.doc[0])
    new = to.move_text(pdf, 0, [cell], 0, 60)[0]
    assert new.text == "98,7" and abs(new.rect.y0 - (cell.rect.y0 + 60)) < 0.6
    assert_only_changed_in(before, gray(pdf.doc[0]), [cell.rect, new.rect])
    assert any(o.text == "120,4" for o in to.list_text(pdf.doc, 0))     # its neighbour stays


@pytest.mark.parametrize("rotate", [90, 180, 270])
def test_move_on_rotated_pages(rotate):
    src = report(chapters=1, rotate=rotate)
    pdf = _pdf(src)
    para = body_objs(pdf.doc, 1)[1]
    old = state(pdf.doc, 1)
    before = gray(pdf.doc[1])
    v = pymupdf.Point(10, 300) * pdf.doc[1].rotation_matrix - pymupdf.Point(0, 0) * pdf.doc[1].rotation_matrix
    dx, dy = v.x, v.y                                   # 300 pt down the column (into the empty part)
    new = to.move_text(pdf, 1, [para], dx, dy)[0]
    assert new.text == para.text and new.free
    assert abs(new.rect.x0 - (para.rect.x0 + dx)) < 0.6 and abs(new.rect.y0 - (para.rect.y0 + dy)) < 0.6
    assert styles(new) == styles(para)
    assert norm(words_in(pdf.doc[1], new.rect)).startswith(norm(para.lines[0].text))
    assert_only_changed_in(before, gray(pdf.doc[1]), [para.rect, new.rect])
    assert pdf.undo() and state(pdf.doc, 1) == old


def test_move_rotated_text():
    doc = pymupdf.open()
    pg = doc.new_page()
    for k in range(6):
        pg.insert_text((72, 100 + 14 * k), f"Brødtekst på siden, linje {k + 1} af et lille afsnit.",
                       fontname="helv", fontsize=11)
    pg.insert_text((520, 700), "Lodret tekst i margenen", fontname="helv", fontsize=12, rotate=90)
    pdf = PdfDocument.from_bytes(doc.tobytes())
    obj = next(o for o in to.list_text(pdf.doc, 0) if "Lodret" in o.text)
    assert obj.role == "rotated" and obj.rect.height > obj.rect.width
    before = gray(pdf.doc[0])
    new = to.move_text(pdf, 0, [obj], -40, -100)[0]
    assert new.text == obj.text
    assert abs(new.rect.x0 - (obj.rect.x0 - 40)) < 0.6 and abs(new.rect.y0 - (obj.rect.y0 - 100)) < 0.6
    assert new.lines[0].dir == obj.lines[0].dir
    assert_only_changed_in(before, gray(pdf.doc[0]), [obj.rect, new.rect])


def test_several_objects_and_nudges_are_one_undo_step(rep):
    pdf = _pdf(rep)
    old = state(pdf.doc, 0)
    objs = [o for o in to.list_text(pdf.doc, 0) if o.role in ("header", "caption")]
    moved = to.move_text(pdf, 0, objs, 5, 0)
    assert len(moved) == 2 and len(pdf.history._undo) == 1
    for _ in range(4):                                   # a burst of arrow-key nudges
        moved = to.move_text(pdf, 0, moved, 1, 0, merge=True)
    assert len(pdf.history._undo) == 1
    head = next(o for o in moved if o.text == HEADER)
    orig = next(o for o in objs if o.text == HEADER)
    assert head.rect.x0 == pytest.approx(orig.rect.x0 + 9, abs=0.6)
    assert pdf.undo() and state(pdf.doc, 0) == old
    # a stale object is found again; an unknown one is ignored
    assert to.move_text(pdf, 0, [], 3, 3) == []


# -- O17: moved text leaves the flow ---------------------------------------------
def test_moved_paragraph_is_standalone_and_the_column_flows_around_it(short):
    pdf = _pdf(short)
    objs = body_objs(pdf.doc, 1)
    para, below = objs[1], objs[2]
    new = to.move_text(pdf, 1, [para], 0, 400)[0]
    free = [s for s in flow.page_stories(pdf.doc, 1) if s.role == "free"]
    assert len(free) == 1 and free[0].kind == "single" and norm(free[0].pieces[0].text) == norm(para.text)
    assert flow._free_rects(pdf.doc, 1)
    col = column(pdf.doc, 1)
    assert norm(para.text) not in norm(" ".join(p.text for p in col.pieces))
    # the text that was below it stays where it was
    assert next(o for o in body_objs(pdf.doc, 1) if o.key == below.key).rect == below.rect
    # the free block is an obstacle for the column
    info = flow._analyze(pdf.doc, 1)
    assert any(o.intersects(pymupdf.Rect(new.rect) * pdf.doc[1].derotation_matrix) for o in info.obstacles)
    # a paragraph of the column grows a lot: the column flows around the moved
    # block, which neither moves nor joins the column again
    col = column(pdf.doc, 1)
    edits = [flow.Edit(piece=k, text=p.text) for k, p in enumerate(col.pieces)]
    edits[0] = flow.Edit(piece=0, text=col.pieces[0].text + EXTRA * 12)
    flow.apply_story(pdf, col, edits)
    again = [o for o in to.list_text(pdf.doc, 1) if o.free]
    assert len(again) == 1 and again[0].text == new.text
    assert again[0].rect == new.rect
    for p in range(pdf.doc.page_count):
        for s in flow.page_stories(pdf.doc, p):
            if s.kind == "flow":
                assert norm(para.text) not in norm(" ".join(pc.text for pc in s.pieces))
                if p == 1:
                    for pc in s.pieces:
                        for ln in pc.block.lines:
                            assert not ln.bbox.intersects(new.rect), ln.text
    # the column text did grow into the page (the extra sentences are there)
    assert "tilføjet" in " ".join(pc.text for pc in column(pdf.doc, 1).pieces)


def test_editing_moved_text_keeps_it_standalone(short):
    """Redigér tekst on a moved block that grows: its new lines stay with
    it (the free rect grows along) and nothing of the column moves."""
    pdf = _pdf(short)
    para = body_objs(pdf.doc, 1)[1]
    new = to.move_text(pdf, 1, [para], 0, 400)[0]
    col_before = [(p.text, tuple(p.block.bbox)) for p in column(pdf.doc, 1).pieces]
    story = next(s for s in flow.page_stories(pdf.doc, 1) if s.role == "free")
    flow.apply_story(pdf, story, [flow.Edit(piece=0, text=story.pieces[0].text + EXTRA * 2)])
    free = [o for o in to.list_text(pdf.doc, 1) if o.free]
    assert len(free) == 1 and norm(free[0].text) == norm(para.text + EXTRA * 2)
    assert len(free[0].block.lines) > len(new.block.lines)
    assert [(p.text, tuple(p.block.bbox)) for p in column(pdf.doc, 1).pieces] == col_before


def test_underline_and_strike_move_with_the_text():
    doc = pymupdf.open()
    pg = doc.new_page()
    pg.insert_text((72, 100), "Almindelig brødtekst på siden, som står stille.", fontname="helv", fontsize=11)
    pg.insert_text((72, 200), "Understreget tekst", fontname="helv", fontsize=12)
    w = pymupdf.get_text_length("Understreget tekst", "helv", 12)
    pg.draw_line((72, 202), (72 + w, 202), color=(0, 0, 0), width=0.8)
    pdf = PdfDocument.from_bytes(doc.tobytes())
    obj = next(o for o in to.list_text(pdf.doc, 0) if o.text == "Understreget tekst")
    assert obj.lines[0].decor_rects                       # the stroke belongs to the text
    before = gray(pdf.doc[0])
    to.move_text(pdf, 0, [obj], 30, 100)
    lines = [d["rect"] for d in pdf.doc[0].get_drawings()]
    assert len(lines) == 1
    assert lines[0].y0 == pytest.approx(302, abs=0.6) and lines[0].x0 == pytest.approx(102, abs=0.6)
    assert_only_changed_in(before, gray(pdf.doc[0]), [obj.rect, pymupdf.Rect(obj.rect) + (30, 100, 30, 100)])


def test_moving_again_replaces_the_free_rect_and_delete_removes_it(short):
    pdf = _pdf(short)
    para = body_objs(pdf.doc, 1)[1]
    new = to.move_text(pdf, 1, [para], 0, 400)[0]
    assert len(flow._free_rects(pdf.doc, 1)) == 1
    new2 = to.move_text(pdf, 1, [new], 10, 20)[0]
    rects = flow._free_rects(pdf.doc, 1)
    assert len(rects) == 1
    raw = pymupdf.Rect(new2.rect) * pdf.doc[1].derotation_matrix
    assert rects[0].contains(raw)
    to.delete_text(pdf, 1, [new2])
    assert flow._free_rects(pdf.doc, 1) == []
    assert pdf.doc.xref_get_key(pdf.doc[1].xref, "PieceInfo/FolioPDF")[0] == "null"


def test_a_free_block_is_not_merged_into_the_paragraph_above(short):
    """Dropped right under another paragraph at its line pitch, the moved
    block would read as that paragraph's next lines – it stays apart."""
    pdf = _pdf(short)
    objs = body_objs(pdf.doc, 1)
    para, last = objs[1], objs[2]
    n_last = len(last.block.lines)
    pitch = last.lines[1].raw_origin[1] - last.lines[0].raw_origin[1]
    dy = (last.lines[-1].origin[1] + pitch) - para.lines[0].origin[1]
    new = to.move_text(pdf, 1, [para], 0, dy)[0]
    assert new.text == para.text and new.free
    assert len(next(o for o in to.list_text(pdf.doc, 1) if o.key == last.key).block.lines) == n_last
    # without the marker it would be one paragraph with the one above
    tmp = pymupdf.open("pdf", pdf.doc.tobytes())
    tmp.xref_set_key(tmp[1].xref, "PieceInfo", "null")
    merged = [b for b in te.text_blocks(tmp, 1) if b.text.startswith(last.block.lines[0].text)]
    assert merged and len(merged[0].lines) > n_last


def test_continuation_markers_of_a_moved_piece_go():
    """A paragraph that Folio split over a page break: moving its first
    part removes the next page's continuation marker; moving the second
    part removes its own."""
    src = report(chapters=2)
    pdf = _pdf(src)
    col = column(pdf.doc, 0)
    edits = [flow.Edit(piece=k, text=p.text) for k, p in enumerate(col.pieces)]
    edits[2] = flow.Edit(piece=2, text=col.pieces[2].text + EXTRA * 3)
    flow.apply_story(pdf, col, edits)
    head = body_objs(pdf.doc, 1)[0]

    def marked(page, origin) -> bool:
        return any(abs(x - origin[0]) <= 0.6 and abs(y - origin[1]) <= 0.6 for x, y in flow._markers(pdf.doc, page))

    assert marked(1, head.origin)                         # page 2 starts with the rest of a paragraph
    tail = body_objs(pdf.doc, 0)[-1]
    to.move_text(pdf, 0, [tail], 0, 2)
    assert not marked(1, head.origin)
    assert pdf.undo() and marked(1, head.origin)
    head = body_objs(pdf.doc, 1)[0]
    to.move_text(pdf, 1, [head], 0, 1)
    assert not marked(1, head.origin)


# -- delete / clipboard ---------------------------------------------------------
def test_delete_text(rep):
    pdf = _pdf(rep)
    old = state(pdf.doc, 0)
    objs = body_objs(pdf.doc, 0)
    para = next(o for o in objs if o.bullet is None and len(o.block.lines) > 3)
    item = next(o for o in objs if o.bullet is not None)
    before = gray(pdf.doc[0])
    to.delete_text(pdf, 0, [para, item])
    page = pdf.doc[0]
    assert not page.search_for(para.lines[1].text[:30])
    assert words_in(page, item.rect).strip() == ""
    assert_only_changed_in(before, gray(page), [para.rect, item.rect])
    assert len(pdf.history._undo) == 1
    keys = {o.key for o in to.list_text(pdf.doc, 0)}
    assert para.key not in keys and item.key not in keys
    assert pdf.undo() and state(pdf.doc, 0) == old


def test_delete_text_doc_and_strip_text_except(rep):
    head = next(o for o in to.list_text(rep, 0) if o.role == "header")
    cap = next(o for o in to.list_text(rep, 0) if o.role == "caption")
    tmp = pymupdf.open()
    tmp.insert_pdf(rep, from_page=0, to_page=0)
    to.strip_text_except(tmp, 0, [head, cap])
    words = sorted(tmp[0].get_text().split())
    assert words == sorted((HEADER + " " + cap.text).split())
    assert tmp[0].get_images()                           # images and drawings stay
    tmp2 = pymupdf.open()
    tmp2.insert_pdf(rep, from_page=0, to_page=0)
    to.delete_text_doc(tmp2, 0, [head])
    assert HEADER not in tmp2[0].get_text() and cap.text in tmp2[0].get_text()


# -- width ---------------------------------------------------------------------
def test_narrower_and_wider(short):
    pdf = _pdf(short)
    para = body_objs(pdf.doc, 1)[1]
    n = len(para.block.lines)
    narrow = to.set_text_width(pdf, 1, para, para.rect.width * 0.6)
    assert len(narrow.block.lines) > n
    assert norm(narrow.text) == norm(para.text)
    assert styles(narrow) == styles(para)
    assert narrow.rect.x0 == pytest.approx(para.rect.x0, abs=0.6)
    assert narrow.rect.x1 <= para.rect.x0 + para.rect.width * 0.6 + 1.0
    assert narrow.lines[0].origin[1] == pytest.approx(para.lines[0].origin[1], abs=0.1)
    # the paragraph below moved down along (as with Redigér tekst)
    below = body_objs(pdf.doc, 1)[2]
    assert below.rect.y0 >= narrow.rect.y1
    assert len(pdf.history._undo) == 1
    wide = to.set_text_width(pdf, 1, narrow, para.rect.width)
    assert len(wide.block.lines) == n and norm(wide.text) == norm(para.text)
    assert pdf.undo() and pdf.undo()
    assert [o.rect for o in body_objs(pdf.doc, 1)] == [o.rect for o in body_objs(short, 1)]


def test_width_of_a_moved_block_and_a_heading(short):
    pdf = _pdf(short)
    para = body_objs(pdf.doc, 1)[1]
    new = to.move_text(pdf, 1, [para], 0, 400)[0]
    narrow = to.set_text_width(pdf, 1, new, 200)
    assert narrow.free and len(narrow.block.lines) > len(para.block.lines)
    assert narrow.rect.x1 <= new.rect.x0 + 201
    assert to.set_text_width(pdf, 1, narrow, narrow.rect.width).key == narrow.key   # same width: nothing
    with pytest.raises(ValueError):
        to.set_text_width(pdf, 1, narrow, 0)


# -- stacking order --------------------------------------------------------------
def test_send_text_to_the_back_and_front():
    doc = pymupdf.open()
    pg = doc.new_page()
    pg.insert_text((72, 100), "Tekst under en kasse", fontname="helv", fontsize=20)
    pg.insert_text((72, 300), "Anden tekst", fontname="helv", fontsize=20)
    pg.draw_rect(pymupdf.Rect(60, 70, 400, 115), color=None, fill=(0.2, 0.4, 0.8))
    pdf = PdfDocument.from_bytes(doc.tobytes())
    obj = next(o for o in to.list_text(pdf.doc, 0) if o.text.startswith("Tekst"))
    box = pymupdf.Rect(obj.rect)

    def dark(page) -> int:
        pm = page.get_pixmap(clip=box, colorspace=pymupdf.csGRAY)
        return sum(1 for v in pm.samples if v < 40)

    assert dark(pdf.doc[0]) == 0                        # hidden under the box
    front = to.restack_text(pdf, 0, [obj], front=True)
    assert front and front[0].text == obj.text and dark(pdf.doc[0]) > 50
    back = to.restack_text(pdf, 0, front, front=False)
    assert back and back[0].text == obj.text and dark(pdf.doc[0]) == 0
    assert "Tekst under en kasse" in pdf.doc[0].get_text()
    assert len(pdf.history._undo) == 2
    assert to.text_at(pdf.doc, 0, ((box.x0 + box.x1) / 2, (box.y0 + box.y1) / 2)).text == obj.text


# -- performance (O16) -------------------------------------------------------------
def test_listing_is_fast_after_the_first_time(rep):
    pdf = _pdf(rep)
    to.list_text(pdf.doc, 0)
    t = time.perf_counter()
    for _ in range(10):
        to.list_text(pdf.doc, 0)
        to.text_at(pdf.doc, 0, (300, 300))
    assert (time.perf_counter() - t) / 10 < 0.05


# -- overlapping text is never damaged (nudges in a dense column) ------------------
CORPUS = "/tmp/claude-0/-home-user-PDF/740062b8-3b33-5ee1-bf87-7d6301ab4318/scratchpad/corpus/aarsrapport.pdf"


def _same(a: str, b: str) -> bool:
    return norm(a) == norm(b)


def _intact(doc, page, obj, role="body"):
    """``obj`` is on the page as one object with all its lines and text."""
    hits = [o for o in to.list_text(doc, page) if _same(o.text, obj.text)]
    assert len(hits) == 1, f"{obj.text[:40]!r} is not one object any more"
    o = hits[0]
    assert len(o.block.lines) == len(obj.block.lines) and o.role == role
    return o


def _nudges(pdf, page, a, b, step=10.0, times=3):
    words = len(pdf.doc[page].get_text("words"))
    old = state(pdf.doc, page)
    cur = [a]
    for k in range(times):
        cur = to.move_text(pdf, page, cur, 0, step, merge=k > 0)
        assert len(cur) == 1 and _same(cur[0].text, a.text) and cur[0].free
        _intact(pdf.doc, page, a, role="free")
        o = _intact(pdf.doc, page, b)
        assert not o.free                                            # the neighbour stays where it was
        assert max(abs(u - v) for u, v in zip(o.rect, b.rect)) < 0.05
        assert len(pdf.doc[page].get_text("words")) == words         # no glyph lost
    assert len(pdf.history._undo) == 1
    assert pdf.undo() and state(pdf.doc, page) == old


def test_nudging_into_the_next_paragraph_keeps_it_intact(short):
    pdf = _pdf(short)
    objs = body_objs(pdf.doc, 1)
    _nudges(pdf, 1, objs[1], objs[2])


def test_nudging_in_the_annual_report_keeps_the_next_paragraph_intact():
    import os

    if not os.path.exists(CORPUS):
        pytest.skip("real-world corpus not generated")
    pdf = PdfDocument(pymupdf.open(CORPUS))
    objs = [o for o in to.list_text(pdf.doc, 5) if o.role == "body"]
    _nudges(pdf, 5, objs[0], objs[1])


def test_a_block_dropped_on_top_of_another_and_back(short):
    pdf = _pdf(short)
    objs = body_objs(pdf.doc, 1)
    a, b = objs[1], objs[2]
    words = len(pdf.doc[1].get_text("words"))
    dx, dy = b.rect.x0 - a.rect.x0, b.lines[0].origin[1] - a.lines[0].origin[1]
    moved = to.move_text(pdf, 1, [a], dx, dy)
    assert len(moved) == 1 and _same(moved[0].text, a.text)
    _intact(pdf.doc, 1, b)
    back = to.move_text(pdf, 1, moved, -dx, -dy)
    assert len(back) == 1 and _same(back[0].text, a.text)
    _intact(pdf.doc, 1, b)
    _intact(pdf.doc, 1, a, role="free")
    assert len(pdf.doc[1].get_text("words")) == words
    old = state(short, 1)
    assert pdf.undo() and pdf.undo() and state(pdf.doc, 1) == old


def test_deleting_text_lying_on_other_text_keeps_that_text(short):
    pdf = _pdf(short)
    objs = body_objs(pdf.doc, 1)
    a, b = objs[1], objs[2]
    moved = to.move_text(pdf, 1, [a], 0, b.lines[1].origin[1] - a.lines[0].origin[1] + 4)
    to.delete_text(pdf, 1, moved)
    _intact(pdf.doc, 1, b)
    assert not pdf.doc[1].search_for(a.lines[2].text[:25])


def test_a_space_after_a_symbol_in_another_font_survives_a_move():
    """A glyph from another font, then a space placed a hair off, then
    text: the space must still extract as a space after the text moved."""
    from tests.conftest import SANS_TTF

    if not SANS_TTF:
        pytest.skip("no TrueType font")
    doc = pymupdf.open()
    pg = doc.new_page()
    helv, sans = pymupdf.Font("helv"), pymupdf.Font(fontfile=SANS_TTF)
    tw = pymupdf.TextWriter(pg.rect)
    x = 72.0
    tw.append((x, 100), "Tid: 2027", font=helv, fontsize=12)
    x += helv.text_length("Tid: 2027", 12)
    tw.append((x, 100), " ", font=helv, fontsize=12)
    x += helv.text_length(" ", 12) + 0.04
    tw.append((x, 100), "→", font=sans, fontsize=12)
    x += sans.text_length("→", 12) - 0.03
    tw.append((x, 100), " ", font=sans, fontsize=12)
    x += sans.text_length(" ", 12) + 0.05
    tw.append((x, 100), "2030", font=helv, fontsize=12)
    tw.write_text(pg)
    pdf = PdfDocument.from_bytes(doc.tobytes())
    obj = next(o for o in to.list_text(pdf.doc, 0) if "2030" in o.text)
    assert norm(obj.text) == "Tid: 2027 → 2030"
    new = to.move_text(pdf, 0, [obj], 20, 40)[0]
    assert norm(new.text) == "Tid: 2027 → 2030"
    assert "2027 → 2030" in norm(pdf.doc[0].get_text())


def test_a_rule_under_a_heading_is_not_its_underline():
    """A heading dropped exactly onto a longer horizontal rule (an accent
    line) and moved back: the rule stays where it was."""
    doc = pymupdf.open()
    pg = doc.new_page()
    pg.insert_text((72, 100), "Overskrift", fontname="helv", fontsize=16)
    pg.insert_text((72, 300), "Brødtekst længere nede på siden.", fontname="helv", fontsize=11)
    pg.draw_line((60, 203), (400, 203), color=(0.2, 0.5, 0.3), width=1.0)
    pdf = PdfDocument.from_bytes(doc.tobytes())
    head = next(o for o in to.list_text(pdf.doc, 0) if o.text == "Overskrift")
    moved = to.move_text(pdf, 0, [head], 0, 100)          # the rule now sits right under it
    assert moved[0].lines[0].decor_rects                  # (the engine takes it for an underline)
    back = to.move_text(pdf, 0, moved, 0, -100)
    assert back[0].text == "Overskrift"
    rules = [d["rect"] for d in pdf.doc[0].get_drawings()]
    assert len(rules) == 1
    assert rules[0].x0 == pytest.approx(60, abs=0.6) and rules[0].x1 == pytest.approx(400, abs=0.6)
    assert rules[0].y0 == pytest.approx(203, abs=0.6)
