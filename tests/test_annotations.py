import pymupdf
import pytest

from pdfstudio.core import CONTENT
from pdfstudio.core.annotations import (
    MARKUP_KINDS,
    SHAPE_KINDS,
    AnnotInfo,
    add_ink,
    add_note,
    add_shape,
    add_text_markup,
    annotation_at,
    delete_annotation,
    list_annotations,
    markup_words,
    update_annotation,
)
from pdfstudio.core.render import page_to_display_rect, render_page
from pdfstudio.core.search import search

YELLOW = (1.0, 0.9, 0.0)
RED = (1.0, 0.0, 0.0)
BLUE = (0.0, 0.0, 1.0)


def close(a, b, tol=1.0) -> bool:
    return all(abs(x - y) <= tol for x, y in zip(a, b))


def word_rect(pdf, page: int, text: str) -> pymupdf.Rect:
    """Display rect of ``text`` (via search)."""
    return next(h for h in search(pdf, text) if h.page == page).rects[0]


def get_annot(pdf, page: int, xref: int):
    pg = pdf.page(page)
    return pg, pg.load_annot(xref)


# -- markup ------------------------------------------------------------------------------
@pytest.mark.parametrize("kind", MARKUP_KINDS)
def test_markup_kinds(sample_pdf, kind):
    rect = word_rect(sample_pdf, 0, "Kvartalsrapport")
    xref = add_text_markup(sample_pdf, 0, kind, rect, BLUE, 0.6)
    assert isinstance(xref, int)
    info = list_annotations(sample_pdf, 0)[0]
    assert info.kind == kind and info.xref == xref
    assert close(info.color, BLUE, 0.01)
    assert abs(info.opacity - 0.6) < 0.01
    assert sample_pdf.history.undo_label in ("Fremhæv tekst", "Understreg tekst", "Gennemstreg tekst", "Bølgeunderstreg tekst")


def test_highlight_marks_whole_words_in_rect(sample_pdf):
    events = []
    sample_pdf.listen(events.append)
    # drag across part of "Hello World": catches both words, not "Indlejret"
    hello = word_rect(sample_pdf, 0, "Hello")
    world = word_rect(sample_pdf, 0, "World")
    sel = pymupdf.Rect(hello.x0 + 5, hello.y0 + 3, world.x0 + 10, world.y1 - 3)
    xref = add_text_markup(sample_pdf, 0, "highlight", sel, YELLOW)
    assert sample_pdf.history.undo_label == "Fremhæv tekst"
    assert events[-1].kind == CONTENT and events[-1].pages == (0,)
    pg, annot = get_annot(sample_pdf, 0, xref)
    quads = annot.vertices
    assert len(quads) == 4  # one line -> one quad
    marked = pymupdf.Quad(*quads).rect
    assert marked.x0 <= hello.x0 + 0.5 and marked.x1 >= world.x1 - 0.5
    skrifttype = word_rect(sample_pdf, 0, "skrifttype:")
    assert marked.x0 > skrifttype.x1 - 1
    assert annot.info["title"] == ""  # no personal data


def test_highlight_multiple_lines_one_quad_per_line(sample_pdf):
    first = word_rect(sample_pdf, 0, "Dette")
    second = word_rect(sample_pdf, 0, "Anden")
    sel = pymupdf.Rect(first.x0, first.y0 + 2, second.x1, second.y1 - 2)
    xref = add_text_markup(sample_pdf, 0, "underline", sel, RED)
    _, annot = get_annot(sample_pdf, 0, xref)
    assert len(annot.vertices) == 8
    preview = markup_words(sample_pdf, 0, sel)
    assert len(preview) == 2


def test_markup_without_words_returns_none(sample_pdf):
    assert add_text_markup(sample_pdf, 0, "highlight", (300, 600, 400, 700), YELLOW) is None
    assert not sample_pdf.history.can_undo
    assert list_annotations(sample_pdf, 0) == []


def test_markup_barely_touching_line_is_ignored(sample_pdf):
    line1 = word_rect(sample_pdf, 0, "Dette")
    # covers the first line, only grazes the top 1pt of the second line
    second = word_rect(sample_pdf, 0, "Anden")
    sel = pymupdf.Rect(line1.x0, line1.y0, 500, second.y0 + 1)
    xref = add_text_markup(sample_pdf, 0, "highlight", sel, YELLOW)
    _, annot = get_annot(sample_pdf, 0, xref)
    assert len(annot.vertices) == 4


def test_highlight_on_rotated_page_covers_displayed_words(sample_pdf):
    hit = word_rect(sample_pdf, 2, "Roteret")  # display coords, vertical text
    assert hit.height > hit.width
    sel = hit + (-2, -2, 2, 2)
    xref = add_text_markup(sample_pdf, 2, "highlight", sel, YELLOW)
    assert xref is not None
    pg, annot = get_annot(sample_pdf, 2, xref)
    # stored in unrotated page space covering the word "Roteret" only
    words = {w[4]: pymupdf.Rect(w[:4]) for w in pg.get_text("words")}
    quad = pymupdf.Quad(*annot.vertices).rect
    assert close(quad, words["Roteret"], 0.5)
    assert not quad.intersects(words["side"])
    # reported back in display space around the hit
    info = list_annotations(sample_pdf, 2)[0]
    assert info.rect.contains(hit)
    # and rendered there: yellow pixels inside the hit rect
    zoom = 2
    pix = render_page(sample_pdf.doc, 2, zoom)
    cx, cy = int((hit.x0 + 1) * zoom), int((hit.y0 + hit.y1) / 2 * zoom)
    r, g, b = pix.pixel(cx, cy)
    assert r > 200 and g > 180 and b < 120
    # far away from the text nothing is drawn
    assert pix.pixel(20, 20) == (255, 255, 255)


def test_markup_validation(sample_pdf):
    with pytest.raises(ValueError):
        add_text_markup(sample_pdf, 0, "glow", (0, 0, 10, 10), YELLOW)
    with pytest.raises(ValueError):
        add_text_markup(sample_pdf, 0, "highlight", (0, 0, 600, 900), (255, 0, 0))
    with pytest.raises(ValueError):
        add_text_markup(sample_pdf, 0, "highlight", (0, 0, 600, 900), YELLOW, opacity=2)


# -- notes ---------------------------------------------------------------------------------
@pytest.mark.parametrize("page", [0, 2])
def test_note_icon_at_display_point(sample_pdf, page):
    point = pymupdf.Point(400, 300)
    xref = add_note(sample_pdf, page, point, "Husk at tjekke tallene", RED)
    assert sample_pdf.history.undo_label == "Tilføj note"
    info = list_annotations(sample_pdf, page)[0]
    assert info.xref == xref and info.kind == "note"
    assert info.contents == "Husk at tjekke tallene"
    assert close(info.rect.top_left, point, 0.5)
    assert info.rect.width > 5 and info.rect.height > 5
    assert close(info.color, RED, 0.01)
    # rendered just below/right of the point
    pix = render_page(sample_pdf.doc, page, 1.0, clip=info.rect)
    colored = sum(1 for y in range(pix.height) for x in range(pix.width) if pix.pixel(x, y) != (255, 255, 255))
    assert colored > 20
    assert annotation_at(sample_pdf, page, point + (5, 5)).xref == xref


# -- ink -----------------------------------------------------------------------------------
def test_ink_on_rotated_page(sample_pdf):
    strokes = [[(100, 100), (150, 120), (200, 100)], [(120, 200)]]
    xref = add_ink(sample_pdf, 2, strokes, BLUE, width=3, opacity=0.5)
    assert sample_pdf.history.undo_label == "Frihåndstegning"
    pg, annot = get_annot(sample_pdf, 2, xref)
    first = [pymupdf.Point(v) * pg.rotation_matrix for v in annot.vertices[0]]
    assert all(close(a, b, 0.05) for a, b in zip(first, strokes[0]))
    info = list_annotations(sample_pdf, 2)[0]
    assert info.kind == "ink" and info.width == 3 and abs(info.opacity - 0.5) < 0.01
    assert info.rect.contains(pymupdf.Rect(100, 100, 200, 120))
    # hit only near the stroke
    assert annotation_at(sample_pdf, 2, (150, 121)).xref == xref
    assert annotation_at(sample_pdf, 2, (150, 160)) is None
    with pytest.raises(ValueError):
        add_ink(sample_pdf, 2, [[]], BLUE)


# -- shapes --------------------------------------------------------------------------------
@pytest.mark.parametrize("page", [0, 2])
@pytest.mark.parametrize("kind", ["rect", "ellipse"])
def test_box_shapes(sample_pdf, page, kind):
    p1, p2 = (300, 250), (200, 150)  # any corner order
    xref = add_shape(sample_pdf, page, kind, p1, p2, RED, fill=BLUE, width=4, opacity=0.8)
    info = list_annotations(sample_pdf, page)[0]
    assert info.kind == kind and info.xref == xref
    assert close(info.rect, (200, 150, 300, 250), 0.5)
    assert close(info.fill, BLUE, 0.01) and close(info.color, RED, 0.01)
    assert info.width == 4 and abs(info.opacity - 0.8) < 0.01
    assert sample_pdf.history.undo_label in ("Tilføj rektangel", "Tilføj ellipse")
    pix = render_page(sample_pdf.doc, page, 1.0)
    r, g, b = pix.pixel(250, 200)
    assert b > 150 and r < 150  # filled blue inside, where it was drawn


@pytest.mark.parametrize("page", [0, 2])
def test_line_and_arrow(sample_pdf, page):
    line = add_shape(sample_pdf, page, "line", (100, 100), (300, 200), RED, width=2)
    arrow = add_shape(sample_pdf, page, "arrow", (100, 300), (300, 300), BLUE)
    filled = add_shape(sample_pdf, page, "arrow", (100, 400), (300, 400), BLUE, fill=BLUE)
    infos = {i.xref: i for i in list_annotations(sample_pdf, page)}
    assert infos[line].kind == "line"
    assert infos[arrow].kind == "arrow"
    pg, a = get_annot(sample_pdf, page, arrow)
    assert a.line_ends == (pymupdf.PDF_ANNOT_LE_NONE, pymupdf.PDF_ANNOT_LE_OPEN_ARROW)
    _, f = get_annot(sample_pdf, page, filled)
    assert f.line_ends[1] == pymupdf.PDF_ANNOT_LE_CLOSED_ARROW
    _, la = get_annot(sample_pdf, page, line)
    ends = [pymupdf.Point(v) * pg.rotation_matrix for v in la.vertices]
    assert close(ends[0], (100, 100), 0.05) and close(ends[1], (300, 200), 0.05)
    assert sample_pdf.history.undo_label == "Tilføj pil"
    # hit testing follows the stroke, not the bounding box
    assert annotation_at(sample_pdf, page, (200, 151)).xref == line
    assert annotation_at(sample_pdf, page, (120, 190)) is None


def test_shape_validation(sample_pdf):
    with pytest.raises(ValueError):
        add_shape(sample_pdf, 0, "star", (0, 0), (10, 10), RED)
    with pytest.raises(ValueError):
        add_shape(sample_pdf, 0, "rect", (10, 10), (10, 50), RED)
    with pytest.raises(ValueError):
        add_shape(sample_pdf, 0, "line", (10, 10), (10, 10), RED)
    assert not sample_pdf.history.can_undo
    assert set(SHAPE_KINDS) == {"rect", "ellipse", "line", "arrow"}


# -- queries -----------------------------------------------------------------------------
def test_annotation_at_returns_topmost(sample_pdf):
    below = add_shape(sample_pdf, 1, "rect", (100, 100), (300, 300), RED, fill=RED)
    above = add_shape(sample_pdf, 1, "ellipse", (150, 150), (250, 250), BLUE)
    assert annotation_at(sample_pdf, 1, (200, 200)).xref == above
    assert annotation_at(sample_pdf, 1, (110, 110)).xref == below
    assert annotation_at(sample_pdf.doc, 1, (500, 700)) is None


def test_markup_hit_only_on_marked_lines(sample_pdf):
    first = word_rect(sample_pdf, 0, "Times.")
    second = word_rect(sample_pdf, 0, "Anden")
    xref = add_text_markup(sample_pdf, 0, "highlight", first | second, YELLOW)
    assert annotation_at(sample_pdf, 0, first.top_left + (1, 3)).xref == xref
    _, annot = get_annot(sample_pdf, 0, xref)
    line1 = pymupdf.Quad(*annot.vertices[:4]).rect
    line2 = pymupdf.Quad(*annot.vertices[4:]).rect
    assert line1.x1 > line2.x1 + 20  # second line is shorter
    # inside the annotation's bounding box but right of the shorter line
    gap = pymupdf.Point((line1.x1 + line2.x1) / 2, (line2.y0 + line2.y1) / 2 + 2)
    assert annotation_at(sample_pdf, 0, gap) is None


def test_list_annotations_types(sample_pdf):
    add_note(sample_pdf, 0, (50, 50), "a", RED)
    add_shape(sample_pdf, 0, "rect", (100, 300), (200, 400), RED)
    infos = list_annotations(sample_pdf.doc, 0)
    assert [i.kind for i in infos] == ["note", "rect"]
    assert all(isinstance(i, AnnotInfo) and i.page == 0 for i in infos)
    assert infos[1].fill is None


# -- update / delete --------------------------------------------------------------------------
def test_update_properties(sample_pdf):
    xref = add_shape(sample_pdf, 2, "rect", (100, 100), (200, 150), RED, fill=BLUE)
    update_annotation(sample_pdf, 2, xref, color=(0.0, 1.0, 0.0), opacity=0.3, width=5, contents="Tjek")
    assert sample_pdf.history.undo_label == "Redigér kommentar"
    info = list_annotations(sample_pdf, 2)[0]
    assert close(info.color, (0, 1, 0), 0.01)
    assert abs(info.opacity - 0.3) < 0.01 and info.width == 5 and info.contents == "Tjek"
    assert close(info.fill, BLUE, 0.01)
    update_annotation(sample_pdf, 2, xref, fill=None)
    assert list_annotations(sample_pdf, 2)[0].fill is None
    update_annotation(sample_pdf, 2, xref, fill=RED)
    assert close(list_annotations(sample_pdf, 2)[0].fill, RED, 0.01)
    # undo walks back one property change at a time
    sample_pdf.undo()
    assert list_annotations(sample_pdf, 2)[0].fill is None


@pytest.mark.parametrize("page", [0, 2])
def test_update_rect_of_box_shape_in_display_space(sample_pdf, page):
    xref = add_shape(sample_pdf, page, "rect", (100, 100), (200, 150), RED)
    update_annotation(sample_pdf, page, xref, rect=(300, 200, 450, 260))
    info = list_annotations(sample_pdf, page)[0]
    assert info.xref == xref
    assert close(info.rect, (300, 200, 450, 260), 0.5)


@pytest.mark.parametrize("page", [0, 2])
def test_update_rect_moves_line_and_ink(sample_pdf, page):
    line = add_shape(sample_pdf, page, "arrow", (100, 100), (200, 150), RED, width=2)
    ink = add_ink(sample_pdf, page, [[(300, 300), (350, 320)]], BLUE)
    for xref in (line, ink):
        old = next(i for i in list_annotations(sample_pdf, page) if i.xref == xref).rect
        update_annotation(sample_pdf, page, xref, rect=old + (40, 30, 40, 30))
        new = next(i for i in list_annotations(sample_pdf, page) if i.xref == xref).rect
        assert close(new, old + (40, 30, 40, 30), 0.5)
    pg, a = get_annot(sample_pdf, page, line)
    ends = [pymupdf.Point(v) * pg.rotation_matrix for v in a.vertices]
    assert close(ends[0], (140, 130), 0.1) and close(ends[1], (240, 180), 0.1)
    assert a.line_ends[1] == pymupdf.PDF_ANNOT_LE_OPEN_ARROW


def test_update_note_text(sample_pdf):
    xref = add_note(sample_pdf, 0, (50, 50), "før", RED)
    update_annotation(sample_pdf, 0, xref, contents="efter")
    assert list_annotations(sample_pdf, 0)[0].contents == "efter"


def test_update_errors(sample_pdf):
    xref = add_note(sample_pdf, 0, (50, 50), "x", RED)
    with pytest.raises(TypeError):
        update_annotation(sample_pdf, 0, xref, colour=RED)
    with pytest.raises(ValueError):
        update_annotation(sample_pdf, 0, 99999, color=RED)
    with pytest.raises(ValueError):
        update_annotation(sample_pdf, 0, xref, opacity=3)
    assert sample_pdf.history.undo_label == "Tilføj note"


def test_delete_and_undo_keeps_xref(sample_pdf):
    xref = add_shape(sample_pdf, 2, "ellipse", (100, 100), (200, 200), RED)
    note = add_note(sample_pdf, 2, (300, 300), "note", RED)
    delete_annotation(sample_pdf, 2, xref)
    assert sample_pdf.history.undo_label == "Slet kommentar"
    assert [i.xref for i in list_annotations(sample_pdf, 2)] == [note]
    sample_pdf.undo()
    assert [i.xref for i in list_annotations(sample_pdf, 2)] == [xref, note]
    delete_annotation(sample_pdf, 2, note)
    assert [i.xref for i in list_annotations(sample_pdf, 2)] == [xref]
    with pytest.raises(ValueError):
        delete_annotation(sample_pdf, 2, note)


def test_annotations_survive_save(sample_pdf, tmp_path):
    hit = word_rect(sample_pdf, 2, "Roteret")
    add_text_markup(sample_pdf, 2, "strikeout", hit, RED)
    add_shape(sample_pdf, 2, "rect", (10, 10), (60, 60), RED)
    sample_pdf.save(str(tmp_path / "out.pdf"))
    kinds = [i.kind for i in list_annotations(sample_pdf, 2)]
    assert kinds == ["strikeout", "rect"]
    rect = list_annotations(sample_pdf, 2)[1].rect
    assert close(rect, (10, 10, 60, 60), 0.5)
    assert page_to_display_rect(sample_pdf.page(2), sample_pdf.page(2).rect) is not None


def test_markup_vertical_text_lines():
    """Text written vertically in the content stream (not via /Rotate)."""
    from pdfstudio.core import PdfDocument

    doc = pymupdf.open()
    page = doc.new_page(width=400, height=400)
    page.insert_text((100, 300), "Lodret tekst her", fontname="helv", fontsize=14, rotate=90)
    pdf = PdfDocument.from_bytes(doc.tobytes())
    words = {w[4]: pymupdf.Rect(w[:4]) for w in pdf.page(0).get_text("words")}
    tekst = words["tekst"]
    assert tekst.height > tekst.width  # really vertical
    # a rect over the lower half of "tekst" and grazing nothing else
    sel = pymupdf.Rect(tekst.x0 + 2, (tekst.y0 + tekst.y1) / 2, tekst.x1 - 2, tekst.y1)
    xref = add_text_markup(pdf, 0, "highlight", sel, YELLOW)
    _, annot = get_annot(pdf, 0, xref)
    marked = pymupdf.Quad(*annot.vertices).rect
    assert close(marked, tekst, 0.5)


def test_thin_drag_along_a_line_selects_its_words(sample_pdf):
    hello = word_rect(sample_pdf, 0, "Hello")
    world = word_rect(sample_pdf, 0, "World")
    mid = (hello.y0 + hello.y1) / 2
    xref = add_text_markup(sample_pdf, 0, "highlight", (hello.x0 + 2, mid - 1, world.x1 - 2, mid + 1), YELLOW)
    _, annot = get_annot(sample_pdf, 0, xref)
    marked = pymupdf.Quad(*annot.vertices).rect
    assert marked.x0 <= hello.x0 + 0.5 and marked.x1 >= world.x1 - 0.5
