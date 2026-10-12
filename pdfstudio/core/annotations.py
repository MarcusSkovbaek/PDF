"""Annotations: text markup, notes, freehand ink and shapes.

All public coordinates are display space (see :mod:`pdfstudio.core.render`);
PyMuPDF's annotation API works in unrotated page space, so everything is
converted on the way in and out. Annotations are identified by their PDF
``xref``, which is stable across undo/redo (snapshots are byte-identical
serialisations without garbage collection).

No personal data is written: the annotation author (``/T``) is left empty.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable, Sequence

import pymupdf

from ..i18n import tr
from .document import CONTENT, PdfDocument
from .render import (
    DocLike,
    PointLike,
    RectLike,
    as_document,
    display_to_page_point,
    display_to_page_rect,
    page_to_display_point,
    page_to_display_rect,
)

Color = tuple[float, float, float]

MARKUP_KINDS = ("highlight", "underline", "strikeout", "squiggly")
SHAPE_KINDS = ("rect", "ellipse", "line", "arrow")

#: Word selection for :func:`add_text_markup`: minimum covered share of a
#: word's length and of its line height (see ``_words_in_rect``).
WORD_ALONG = 0.2
WORD_ACROSS = 0.4
#: Hit-test tolerance in points for :func:`annotation_at`.
HIT_TOLERANCE = 3.0

_TYPE_KINDS = {
    pymupdf.PDF_ANNOT_TEXT: "note",
    pymupdf.PDF_ANNOT_HIGHLIGHT: "highlight",
    pymupdf.PDF_ANNOT_UNDERLINE: "underline",
    pymupdf.PDF_ANNOT_STRIKE_OUT: "strikeout",
    pymupdf.PDF_ANNOT_SQUIGGLY: "squiggly",
    pymupdf.PDF_ANNOT_INK: "ink",
    pymupdf.PDF_ANNOT_SQUARE: "rect",
    pymupdf.PDF_ANNOT_CIRCLE: "ellipse",
    pymupdf.PDF_ANNOT_LINE: "line",
    pymupdf.PDF_ANNOT_FREE_TEXT: "freetext",
    pymupdf.PDF_ANNOT_POLYGON: "polygon",
    pymupdf.PDF_ANNOT_POLY_LINE: "polyline",
    pymupdf.PDF_ANNOT_STAMP: "stamp",
}
_MARKUP_TYPES = {
    pymupdf.PDF_ANNOT_HIGHLIGHT,
    pymupdf.PDF_ANNOT_UNDERLINE,
    pymupdf.PDF_ANNOT_STRIKE_OUT,
    pymupdf.PDF_ANNOT_SQUIGGLY,
}
_FILL_TYPES = {
    pymupdf.PDF_ANNOT_SQUARE,
    pymupdf.PDF_ANNOT_CIRCLE,
    pymupdf.PDF_ANNOT_LINE,
    pymupdf.PDF_ANNOT_POLYGON,
    pymupdf.PDF_ANNOT_POLY_LINE,
}
_BORDER_TYPES = _FILL_TYPES | {pymupdf.PDF_ANNOT_INK, pymupdf.PDF_ANNOT_FREE_TEXT}
# Shapes drawn inside /Rect minus /RD: their geometry is the inner rect.
_BOX_TYPES = {pymupdf.PDF_ANNOT_SQUARE, pymupdf.PDF_ANNOT_CIRCLE}
# Types whose geometry is a list of points rather than just /Rect.
_VERTEX_KEYS = {
    pymupdf.PDF_ANNOT_LINE: "L",
    pymupdf.PDF_ANNOT_INK: "InkList",
    pymupdf.PDF_ANNOT_POLYGON: "Vertices",
    pymupdf.PDF_ANNOT_POLY_LINE: "Vertices",
    **{t: "QuadPoints" for t in _MARKUP_TYPES},
}
_UPDATABLE = ("color", "fill", "opacity", "width", "contents", "rect")


@dataclass
class AnnotInfo:
    """Snapshot of an annotation's user-editable properties (display space)."""

    xref: int
    kind: str                   # MARKUP_KINDS, SHAPE_KINDS, "note", "ink", "freetext", ...
    rect: pymupdf.Rect          # rect/ellipse: the outline path (stroke centred on it,
                                # == p1-p2 of add_shape); others: the annotation rect
    color: Color | None         # stroke colour
    fill: Color | None          # interior colour (shapes)
    opacity: float
    width: float                # border / line width (0 if not applicable)
    contents: str
    page: int = -1
    author: str = ""            # /T of annotations made by other tools


# -- helpers -----------------------------------------------------------------------
def _color(value: Sequence[float] | None) -> Color | None:
    if value is None or len(value) == 0:
        return None
    if len(value) == 1:  # gray
        return (float(value[0]),) * 3  # type: ignore[return-value]
    if len(value) == 4:  # CMYK -> RGB (naive, good enough for a swatch)
        c, m, y, k = (float(v) for v in value)
        return ((1 - c) * (1 - k), (1 - m) * (1 - k), (1 - y) * (1 - k))
    r, g, b = (float(v) for v in value[:3])
    return (r, g, b)


def _check_color(value: Sequence[float]) -> list[float]:
    rgb = [float(v) for v in value]
    if len(rgb) != 3 or not all(0.0 <= v <= 1.0 for v in rgb):
        raise ValueError(f"color must be (r, g, b) floats 0..1, got {value!r}")
    return rgb


def _check_opacity(opacity: float) -> float:
    value = float(opacity)
    if not 0.0 <= value <= 1.0:
        raise ValueError(f"opacity must be 0..1, got {opacity!r}")
    return value


def _kind(annot: pymupdf.Annot) -> str:
    type_id = annot.type[0]
    if type_id == pymupdf.PDF_ANNOT_LINE:
        ends = annot.line_ends or (0, 0)
        if any(e != pymupdf.PDF_ANNOT_LE_NONE for e in ends):
            return "arrow"
    return _TYPE_KINDS.get(type_id, annot.type[1].lower())


def _geometry_rect(page: pymupdf.Page, annot: pymupdf.Annot) -> pymupdf.Rect:
    """Unrotated rect of the annotation's geometry.

    For rectangles/ellipses MuPDF stores ``/Rect`` grown by the stroke and
    the difference in ``/RD``; we report (and accept in
    :func:`update_annotation`) the inner rect so values round-trip exactly.
    """
    rect = annot.rect
    if annot.type[0] not in _BOX_TYPES:
        return rect
    kind, value = page.parent.xref_get_key(annot.xref, "RD")
    if kind != "array":
        return rect
    try:
        left, bottom, right, top = (float(v) for v in value.strip("[] ").split())
    except ValueError:
        return rect
    inner = pymupdf.Rect(rect.x0 + left, rect.y0 + top, rect.x1 - right, rect.y1 - bottom)
    return inner if not inner.is_empty else rect


def _info(annot: pymupdf.Annot, page: pymupdf.Page, index: int) -> AnnotInfo:
    colors = annot.colors or {}
    opacity = annot.opacity
    border = annot.border or {}
    width = border.get("width", 0.0) if annot.type[0] in _BORDER_TYPES else 0.0
    info = annot.info or {}
    return AnnotInfo(
        xref=annot.xref,
        kind=_kind(annot),
        rect=page_to_display_rect(page, _geometry_rect(page, annot)),
        color=_color(colors.get("stroke")),
        fill=_color(colors.get("fill")),
        opacity=1.0 if opacity is None or opacity < 0 else float(opacity),
        width=float(width) if width and width > 0 else 0.0,
        contents=info.get("content", "") or "",
        page=index,
        author=info.get("title", "") or "",
    )


def _load(page: pymupdf.Page, xref: int) -> pymupdf.Annot:
    try:
        annot = page.load_annot(xref)
    except Exception:
        annot = None
    if annot is None:
        raise ValueError(tr("core.err.annot_missing"))
    return annot


def _finish(annot: pymupdf.Annot, opacity: float) -> int:
    if opacity < 1.0:
        annot.set_opacity(opacity)
    annot.update()
    return annot.xref


# -- text markup ---------------------------------------------------------------------
def _overlap(a0: float, a1: float, b0: float, b1: float) -> float:
    return max(0.0, min(a1, b1) - max(a0, b0))


def _words_in_rect(page: pymupdf.Page, rect: pymupdf.Rect) -> list[tuple]:
    """Words (unrotated ``extractWORDS`` tuples) selected by ``rect``.

    A word is selected when ``rect`` covers at least :data:`WORD_ALONG` of
    its length (or its centre) *and* at least :data:`WORD_ACROSS` of the line
    height (relative to the thinner of word and rect), measured along / across
    the writing direction of its line. So grazing the line above or below
    does not mark it, while a partly covered word is marked as a whole.
    """
    textpage = page.get_textpage()
    try:
        blocks = textpage.extractDICT()["blocks"]
    except Exception:  # pragma: no cover - defensive, dict extraction is robust
        blocks = []

    def horizontal(block_no: int, line_no: int) -> bool:
        try:
            dx, dy = blocks[block_no]["lines"][line_no]["dir"]
        except (IndexError, KeyError, TypeError):
            return True
        return abs(dx) >= abs(dy)

    chosen = []
    for word in textpage.extractWORDS():
        box = pymupdf.Rect(word[:4])
        if box.is_empty or not box.intersects(rect):
            continue
        x_ov = _overlap(box.x0, box.x1, rect.x0, rect.x1)
        y_ov = _overlap(box.y0, box.y1, rect.y0, rect.y1)
        if horizontal(word[5], word[6]):
            along, along_len = x_ov, box.width
            across, across_len = y_ov, min(box.height, rect.height)
        else:
            along, along_len = y_ov, box.height
            across, across_len = x_ov, min(box.width, rect.width)
        centre = pymupdf.Point((box.x0 + box.x1) / 2, (box.y0 + box.y1) / 2)
        if across >= WORD_ACROSS * across_len and (along >= WORD_ALONG * along_len or centre in rect):
            chosen.append(word)
    return chosen


def _line_quads(words: Iterable[tuple]) -> list[pymupdf.Quad]:
    """One quad per text line, spanning the chosen words of that line."""
    lines: dict[tuple[int, int], pymupdf.Rect] = {}
    for word in words:
        key = (word[5], word[6])
        box = pymupdf.Rect(word[:4])
        lines[key] = lines[key] | box if key in lines else box
    return [lines[k].quad for k in sorted(lines)]


def markup_words(doc: DocLike, page: int, rect: RectLike) -> list[pymupdf.Rect]:
    """Display-space rects of the text lines :func:`add_text_markup` would
    mark for ``rect`` – handy for a live selection preview."""
    pg = as_document(doc)[page]
    quads = _line_quads(_words_in_rect(pg, display_to_page_rect(pg, rect)))
    return [page_to_display_rect(pg, q.rect) for q in quads]


def add_text_markup(
    pdf: PdfDocument,
    page: int,
    kind: str,
    rect: RectLike,
    color: Color,
    opacity: float = 1.0,
) -> int | None:
    """Highlight / underline / strike out / squiggly-underline the words
    inside ``rect`` (display space). Returns the annotation xref, or
    ``None`` (and changes nothing) if ``rect`` contains no words."""
    if kind not in MARKUP_KINDS:
        raise ValueError(tr("core.err.annot_kind", kind=kind))
    stroke = _check_color(color)
    opacity = _check_opacity(opacity)
    pg = pdf.page(page)
    quads = _line_quads(_words_in_rect(pg, display_to_page_rect(pg, rect)))
    if not quads:
        return None
    with pdf.edit(tr(f"core.undo.{kind}"), CONTENT, (page,)):
        pg = pdf.page(page)
        adder = {
            "highlight": pg.add_highlight_annot,
            "underline": pg.add_underline_annot,
            "strikeout": pg.add_strikeout_annot,
            "squiggly": pg.add_squiggly_annot,
        }[kind]
        annot = adder(quads=quads)
        annot.set_colors(stroke=stroke)
        return _finish(annot, opacity)


def add_markup_lines(
    pdf: PdfDocument,
    page: int,
    kind: str,
    line_rects: Sequence[RectLike],
    color: Color,
    opacity: float = 1.0,
) -> int | None:
    """Like :func:`add_text_markup`, but for an explicit flow selection: one
    quad per given display-space rect (typically the union of the selected
    words of one text line). Returns the xref, or ``None`` if nothing given."""
    if kind not in MARKUP_KINDS:
        raise ValueError(tr("core.err.annot_kind", kind=kind))
    stroke = _check_color(color)
    opacity = _check_opacity(opacity)
    pg = pdf.page(page)
    quads = [display_to_page_rect(pg, r).quad for r in line_rects]
    quads = [q for q in quads if not q.rect.is_empty]
    if not quads:
        return None
    with pdf.edit(tr(f"core.undo.{kind}"), CONTENT, (page,)):
        pg = pdf.page(page)
        adder = {
            "highlight": pg.add_highlight_annot,
            "underline": pg.add_underline_annot,
            "strikeout": pg.add_strikeout_annot,
            "squiggly": pg.add_squiggly_annot,
        }[kind]
        annot = adder(quads=quads)
        annot.set_colors(stroke=stroke)
        return _finish(annot, opacity)


# -- notes, ink, shapes ----------------------------------------------------------------
def add_note(pdf: PdfDocument, page: int, point: PointLike, text: str, color: Color) -> int:
    """Sticky note whose icon's top-left corner is at ``point`` (display)."""
    stroke = _check_color(color)
    with pdf.edit(tr("core.undo.note"), CONTENT, (page,)):
        pg = pdf.page(page)
        # PyMuPDF places the (non-rotating) icon so that ``point`` is its
        # top-left corner as displayed, once converted to page space.
        annot = pg.add_text_annot(display_to_page_point(pg, point), text, icon="Note")
        annot.set_colors(stroke=stroke)
        return _finish(annot, 1.0)


def add_ink(
    pdf: PdfDocument,
    page: int,
    strokes: Sequence[Sequence[PointLike]],
    color: Color,
    width: float = 2.0,
    opacity: float = 1.0,
) -> int:
    """Freehand drawing; ``strokes`` is a list of polylines (display)."""
    stroke = _check_color(color)
    opacity = _check_opacity(opacity)
    paths = [list(s) for s in strokes if len(s)]
    if not paths:
        raise ValueError(tr("core.err.annot_ink_empty"))
    with pdf.edit(tr("core.undo.ink"), CONTENT, (page,)):
        pg = pdf.page(page)
        ink = []
        for path in paths:
            pts = [display_to_page_point(pg, p) for p in path]
            if len(pts) == 1:  # a single click: draw a dot
                pts.append(pymupdf.Point(pts[0].x + 0.01, pts[0].y))
            ink.append([(p.x, p.y) for p in pts])
        annot = pg.add_ink_annot(ink)
        annot.set_border(width=float(width))
        annot.set_colors(stroke=stroke)
        return _finish(annot, opacity)


def add_shape(
    pdf: PdfDocument,
    page: int,
    kind: str,
    p1: PointLike,
    p2: PointLike,
    stroke: Color,
    fill: Color | None = None,
    width: float = 2.0,
    opacity: float = 1.0,
) -> int:
    """Rectangle / ellipse (bounding box ``p1``-``p2``) or line / arrow from
    ``p1`` to ``p2`` (arrow head at ``p2``; closed and filled when ``fill``
    is given, open otherwise). Display coordinates."""
    if kind not in SHAPE_KINDS:
        raise ValueError(tr("core.err.annot_kind", kind=kind))
    stroke_rgb = _check_color(stroke)
    fill_rgb = _check_color(fill) if fill is not None else None
    opacity = _check_opacity(opacity)
    a, b = pymupdf.Point(p1), pymupdf.Point(p2)
    if kind in ("rect", "ellipse"):
        if pymupdf.Rect(a, b).normalize().is_empty:
            raise ValueError(tr("core.err.annot_empty"))
    elif abs(a - b) < 0.5:
        raise ValueError(tr("core.err.annot_empty"))
    with pdf.edit(tr(f"core.undo.{kind}"), CONTENT, (page,)):
        pg = pdf.page(page)
        if kind in ("rect", "ellipse"):
            box = display_to_page_rect(pg, pymupdf.Rect(a, b).normalize())
            annot = pg.add_rect_annot(box) if kind == "rect" else pg.add_circle_annot(box)
        else:
            annot = pg.add_line_annot(display_to_page_point(pg, a), display_to_page_point(pg, b))
            if kind == "arrow":
                head = pymupdf.PDF_ANNOT_LE_CLOSED_ARROW if fill_rgb else pymupdf.PDF_ANNOT_LE_OPEN_ARROW
                annot.set_line_ends(pymupdf.PDF_ANNOT_LE_NONE, head)
        annot.set_border(width=float(width))
        if fill_rgb is not None:
            annot.set_colors(stroke=stroke_rgb, fill=fill_rgb)
        else:
            annot.set_colors(stroke=stroke_rgb)
        xref = _finish(annot, opacity)
        if kind in ("rect", "ellipse"):
            # MuPDF grows /Rect by the stroke on update; put the outline
            # back exactly on p1-p2 now that the final width is known.
            annot.set_rect(box)
            annot.update()
        return xref


# -- queries -------------------------------------------------------------------------
def list_annotations(doc: DocLike, page: int) -> list[AnnotInfo]:
    """All annotations of a page (no links / form fields / popups), in
    drawing order (last = topmost)."""
    pg = as_document(doc)[page]
    return [_info(annot, pg, page) for annot in pg.annots()]


def _segment_distance(p: pymupdf.Point, a: pymupdf.Point, b: pymupdf.Point) -> float:
    d = b - a
    length2 = d.x * d.x + d.y * d.y
    if length2 == 0:
        return abs(p - a)
    t = max(0.0, min(1.0, ((p.x - a.x) * d.x + (p.y - a.y) * d.y) / length2))
    return abs(p - (a + d * t))


def _hits(annot: pymupdf.Annot, point: pymupdf.Point, tolerance: float) -> bool:
    """Precise hit test in *unrotated* page space."""
    type_id = annot.type[0]
    rect = annot.rect
    if not (rect + (-tolerance, -tolerance, tolerance, tolerance)).contains(point):
        return False
    vertices = annot.vertices
    if type_id in _MARKUP_TYPES and vertices:
        for i in range(0, len(vertices) - 3, 4):
            quad_rect = pymupdf.Quad(*vertices[i:i + 4]).rect
            if (quad_rect + (-tolerance, -tolerance, tolerance, tolerance)).contains(point):
                return True
        return False
    if type_id in (pymupdf.PDF_ANNOT_LINE, pymupdf.PDF_ANNOT_INK, pymupdf.PDF_ANNOT_POLY_LINE) and vertices:
        width = (annot.border or {}).get("width", 1.0) or 1.0
        reach = max(width, 1.0) / 2 + tolerance
        paths = vertices if type_id == pymupdf.PDF_ANNOT_INK else [vertices]
        for path in paths:
            pts = [pymupdf.Point(v) for v in path]
            if len(pts) == 1 and abs(point - pts[0]) <= reach:
                return True
            for a, b in zip(pts, pts[1:]):
                if _segment_distance(point, a, b) <= reach:
                    return True
        return False
    return True


def annotation_at(doc: DocLike, page: int, point: PointLike, tolerance: float = HIT_TOLERANCE) -> AnnotInfo | None:
    """Topmost annotation under ``point`` (display), or ``None``.

    Markup is hit only on its marked lines, lines/ink only near the stroke;
    other annotations anywhere inside their rectangle.
    """
    pg = as_document(doc)[page]
    target = display_to_page_point(pg, point)
    for annot in reversed(list(pg.annots())):
        if _hits(annot, target, tolerance):
            return _info(annot, pg, page)
    return None


# -- updates -------------------------------------------------------------------------
def _fmt(values: Iterable[float]) -> str:
    return " ".join(f"{v:.4f}".rstrip("0").rstrip(".") for v in values)


def _move_vertices(doc: pymupdf.Document, page: pymupdf.Page, annot: pymupdf.Annot, new_rect: pymupdf.Rect) -> None:
    """Map the point geometry of line/ink/markup annotations from their
    current display rect onto ``new_rect`` (display), keeping the xref."""
    old = page_to_display_rect(page, annot.rect)
    sx = new_rect.width / old.width if old.width > 0 else 1.0
    sy = new_rect.height / old.height if old.height > 0 else 1.0
    to_pdf = ~page.transformation_matrix

    def convert(v: Sequence[float]) -> tuple[float, float]:
        d = page_to_display_point(page, v)
        moved = pymupdf.Point(new_rect.x0 + (d.x - old.x0) * sx, new_rect.y0 + (d.y - old.y0) * sy)
        p = display_to_page_point(page, moved) * to_pdf
        return (p.x, p.y)

    type_id = annot.type[0]
    key = _VERTEX_KEYS[type_id]
    vertices = annot.vertices or []
    if type_id == pymupdf.PDF_ANNOT_INK:
        value = "[" + " ".join("[" + _fmt(c for v in path for c in convert(v)) + "]" for path in vertices) + "]"
    else:
        value = "[" + _fmt(c for v in vertices for c in convert(v)) + "]"
    doc.xref_set_key(annot.xref, key, value)


def update_annotation(pdf: PdfDocument, page: int, xref: int, **props: Any) -> None:
    """Change properties of an annotation.

    Supported: ``color`` (stroke), ``fill`` (``None`` removes it; shapes
    only), ``opacity``, ``width`` (border/line width), ``contents`` (note or
    comment text) and ``rect`` (display space; line, ink and markup
    geometry is moved/scaled with it).
    """
    unknown = [k for k in props if k not in _UPDATABLE]
    if unknown:
        raise TypeError(tr("core.err.annot_prop", name=", ".join(unknown)))
    _load(pdf.page(page), xref)  # fail early, outside the undo step
    if not props:
        return
    if "color" in props and props["color"] is not None:
        _check_color(props["color"])
    if props.get("fill") is not None:
        _check_color(props["fill"])
    if "opacity" in props:
        _check_opacity(props["opacity"])
    with pdf.edit(tr("core.undo.update_annot"), CONTENT, (page,)):
        doc = pdf.doc
        pg = pdf.page(page)
        annot = _load(pg, xref)
        type_id = annot.type[0]
        if "width" in props and type_id in _BORDER_TYPES:
            box = _geometry_rect(pg, annot)
            annot.set_border(width=float(props["width"]))
            annot.update()  # applies the new stroke-dependent /RD
            if type_id in _BOX_TYPES and "rect" not in props:
                annot.set_rect(box)  # keep the outline where it was
        if "rect" in props:
            new_rect = pymupdf.Rect(props["rect"]).normalize()
            if new_rect.is_empty and type_id not in _VERTEX_KEYS:
                raise ValueError(tr("core.err.annot_empty"))
            if type_id in _VERTEX_KEYS:
                _move_vertices(doc, pg, annot, new_rect)
                annot = _load(pg, xref)
            else:
                annot.set_rect(display_to_page_rect(pg, new_rect))
        if "fill" in props and type_id in _FILL_TYPES:
            if props["fill"] is None:
                doc.xref_set_key(xref, "IC", "null")
                annot = _load(pg, xref)
            else:
                annot.set_colors(fill=_check_color(props["fill"]))
        if props.get("color") is not None:
            annot.set_colors(stroke=_check_color(props["color"]))
        if "opacity" in props:
            annot.set_opacity(_check_opacity(props["opacity"]))
        if "contents" in props:
            annot.set_info(content=str(props["contents"] or ""))
        annot.update()


def delete_annotation(pdf: PdfDocument, page: int, xref: int) -> None:
    """Remove an annotation (and its popup)."""
    _load(pdf.page(page), xref)
    with pdf.edit(tr("core.undo.delete_annot"), CONTENT, (page,)):
        pg = pdf.page(page)
        pg.delete_annot(_load(pg, xref))


__all__ = [
    "AnnotInfo",
    "HIT_TOLERANCE",
    "MARKUP_KINDS",
    "SHAPE_KINDS",
    "add_ink",
    "add_note",
    "add_shape",
    "add_text_markup",
    "annotation_at",
    "delete_annotation",
    "list_annotations",
    "markup_words",
    "update_annotation",
]
