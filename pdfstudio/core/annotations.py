"""Annotations: text markup, notes, free text, freehand ink and shapes.

All public coordinates are display space (see :mod:`pdfstudio.core.render`);
PyMuPDF's annotation API works in unrotated page space, so everything is
converted on the way in and out. Annotations are identified by their PDF
``xref``, which is stable across undo/redo (snapshots are byte-identical
serialisations without garbage collection).

New annotations carry the author (``/T``: the Windows user's name, see
:mod:`pdfstudio.core.identity`), creation and modification dates and a
unique ``/NM`` – like Acrobat, so comment lists show who wrote what when.
Replies, review states and group members (annotations with ``/IRT``) belong
to their parent's thread (:mod:`pdfstudio.core.comments`) and are left out
of :func:`list_annotations` / :func:`annotation_at`; deleting a parent
deletes them too.

Group operations (:func:`update_annotations`, :func:`move_annotations`,
:func:`delete_annotations`) change several annotations as one undo step;
:func:`render_preview` shows a change without making it (hover previews).
"""

from __future__ import annotations

import re
import uuid
from dataclasses import dataclass
from typing import Any, Iterable, Mapping, Sequence

import pymupdf

from ..i18n import tr
from .document import CONTENT, PdfDocument
from .identity import current_author, pdf_date
from .render import (
    DocLike,
    PointLike,
    RectLike,
    as_document,
    display_to_page_point,
    display_to_page_rect,
    page_to_display_point,
    page_to_display_rect,
    render_page,
)

Color = tuple[float, float, float]
AnnotRef = tuple[int, int]  # (page, xref)

MARKUP_KINDS = ("highlight", "underline", "strikeout", "squiggly")
SHAPE_KINDS = ("rect", "ellipse", "line", "arrow")
#: Font choices of free text (typewriter) annotations: the PDF base-14
#: families every reader has. Text outside their encoding (e.g. Greek, CJK)
#: is still shown – MuPDF then embeds a fallback font into the appearance.
FREETEXT_FONTS = ("sans", "serif", "mono")
#: MuPDF's free text layout (× font size): first baseline below the top of
#: the box and baseline-to-baseline distance. The inline editor mirrors it.
FREETEXT_BASELINE = 0.8
FREETEXT_LINE_HEIGHT = 1.2

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
    pymupdf.PDF_ANNOT_CARET: "caret",
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
_UPDATABLE = ("color", "fill", "opacity", "width", "contents", "rect", "fontsize", "font")
_FT_FONTNAMES = {"sans": "Helv", "serif": "TiRo", "mono": "Cour"}      # /DA resource names
_FT_METRICS = {"sans": "helv", "serif": "tiro", "mono": "cour"}        # get_text_length names
_FT_KEYS = {"helv": "sans", "helvetica": "sans", "tiro": "serif", "times-roman": "serif",
            "cour": "mono", "courier": "mono"}
_DA_COLOR = re.compile(r"((?:[-+]?(?:\d+\.?\d*|\.\d+)\s+){1,4})(rg|g|k)\b")
_DA_FONT = re.compile(r"/([^\s/]+)\s+([-+]?(?:\d+\.?\d*|\.\d+))\s+Tf")


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
    fontsize: float = 0.0       # free text only (0 otherwise)
    font: str = ""              # free text only: FREETEXT_FONTS key (or the PDF font name)


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


def _check_fontsize(size: float) -> float:
    value = float(size)
    if not 1.0 <= value <= 400.0:
        raise ValueError(tr("core.err.annot_fontsize", size=size))
    return value


def _check_font(font: str) -> str:
    if font not in FREETEXT_FONTS:
        raise ValueError(tr("core.err.annot_font", font=font))
    return font


def _parse_da(da: str) -> tuple[Color | None, str, float]:
    """(text colour, font key or name, size) of a ``/DA`` string such as
    ``"0.9 0.1 0.1 rg /Helv 12 Tf"``."""
    color: Color | None = None
    matches = list(_DA_COLOR.finditer(da or ""))
    if matches:
        values = [float(v) for v in matches[-1].group(1).split()]
        color = _color(values)
    font, size = "", 0.0
    fm = _DA_FONT.search(da or "")
    if fm:
        name = fm.group(1)
        font = _FT_KEYS.get(name.lower(), name)
        size = float(fm.group(2))
    return color, font, size


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
    color, fill = _color(colors.get("stroke")), _color(colors.get("fill"))
    font, fontsize = "", 0.0
    if annot.type[0] == pymupdf.PDF_ANNOT_FREE_TEXT:
        # text colour, font and size live in /DA; PyMuPDF reports the
        # background (/C) as "stroke"
        kind, da = page.parent.xref_get_key(annot.xref, "DA")
        color, font, fontsize = _parse_da(da if kind == "string" else "")
        fill = _color(colors.get("stroke"))
        if color is None:
            color = (0.0, 0.0, 0.0)
    return AnnotInfo(
        xref=annot.xref,
        kind=_kind(annot),
        rect=page_to_display_rect(page, _geometry_rect(page, annot)),
        color=color,
        fill=fill,
        opacity=1.0 if opacity is None or opacity < 0 else float(opacity),
        width=float(width) if width and width > 0 else 0.0,
        contents=info.get("content", "") or "",
        page=index,
        author=info.get("title", "") or "",
        fontsize=fontsize,
        font=font,
    )


def _load(page: pymupdf.Page, xref: int) -> pymupdf.Annot:
    try:
        annot = page.load_annot(xref)
    except Exception:
        annot = None
    if annot is None:
        raise ValueError(tr("core.err.annot_missing"))
    return annot


def stamp_new(annot: pymupdf.Annot) -> None:
    """Author, dates and a unique name of a newly created annotation."""
    now = pdf_date()
    annot.set_info(title=current_author(), creationDate=now, modDate=now)
    annot.parent.parent.xref_set_key(annot.xref, "NM", pymupdf.get_pdf_str(uuid.uuid4().hex))


def _finish(annot: pymupdf.Annot, opacity: float) -> int:
    if opacity < 1.0:
        annot.set_opacity(opacity)
    stamp_new(annot)
    annot.update()
    return annot.xref


def is_thread_child(annot: pymupdf.Annot) -> bool:
    """Replies, review states and group members (``/IRT``)."""
    try:
        return bool(annot.irt_xref)
    except Exception:  # pragma: no cover - defensive
        return False


def thread_children(page: pymupdf.Page) -> dict[int, list[pymupdf.Annot]]:
    """``{parent xref: [annotations with /IRT -> parent]}`` of a page."""
    out: dict[int, list[pymupdf.Annot]] = {}
    for a in page.annots():
        if is_thread_child(a):
            out.setdefault(a.irt_xref, []).append(a)
    return out


def thread_descendants(page: pymupdf.Page, xref: int) -> list[int]:
    """Every annotation whose /IRT chain leads to ``xref``, deepest first."""
    kids = thread_children(page)
    out: list[int] = []
    seen = {xref}

    def walk(x: int) -> None:
        for a in kids.get(x, []):
            if a.xref not in seen:
                seen.add(a.xref)
                walk(a.xref)
                out.append(a.xref)

    walk(xref)
    return out


def _delete_with_thread(page: pymupdf.Page, xref: int) -> None:
    """Delete an annotation with its replies, states and group members."""
    for child in thread_descendants(page, xref):
        try:
            page.delete_annot(page.load_annot(child))
        except Exception:
            pass  # already removed together with its own parent
    page.delete_annot(_load(page, xref))


def _top_annots(page: pymupdf.Page) -> list[pymupdf.Annot]:
    return [a for a in page.annots() if not is_thread_child(a)]


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


Selection = Mapping[int, Sequence[RectLike]]  # {page: [display rect per text line]}


def add_markup(
    pdf: PdfDocument,
    kind: str,
    selection: Selection,
    color: Color,
    opacity: float = 1.0,
    *,
    contents: str = "",
    label: str | None = None,
) -> list[AnnotRef]:
    """Mark an explicit flow selection (``{page: [line rect, ...]}``,
    display space; one quad per rect – typically one per text line, see
    :func:`pdfstudio.core.textsel.selection_rects`) – one annotation per
    page, all in one undo step. ``contents`` (the comment) goes on the first
    page's annotation. Returns ``[(page, xref), ...]`` (empty: nothing
    changed)."""
    if kind not in MARKUP_KINDS:
        raise ValueError(tr("core.err.annot_kind", kind=kind))
    stroke = _check_color(color)
    opacity = _check_opacity(opacity)
    plan: dict[int, list[pymupdf.Quad]] = {}
    for page in sorted(selection):
        pg = pdf.page(page)
        quads = [display_to_page_rect(pg, r).quad for r in selection[page]]
        quads = [q for q in quads if not q.rect.is_empty]
        if quads:
            plan[page] = quads
    if not plan:
        return []
    out: list[AnnotRef] = []
    with pdf.edit(label or tr(f"core.undo.{kind}"), CONTENT, tuple(plan)):
        for page, quads in plan.items():
            pg = pdf.page(page)
            adder = {
                "highlight": pg.add_highlight_annot,
                "underline": pg.add_underline_annot,
                "strikeout": pg.add_strikeout_annot,
                "squiggly": pg.add_squiggly_annot,
            }[kind]
            annot = adder(quads=quads)
            annot.set_colors(stroke=stroke)
            if contents and not out:
                annot.set_info(content=str(contents))
            out.append((page, _finish(annot, opacity)))
    return out


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


# -- free text (typewriter) ----------------------------------------------------------
def _clean_text(text: str) -> str:
    """Line breaks normalised to ``\\n``; tabs become spaces (the base-14
    fonts have no tab glyph)."""
    return str(text or "").replace("\r\n", "\n").replace("\r", "\n").replace("\t", "    ")


_FONT_CACHE: dict[str, pymupdf.Font] = {}


def _text_width(text: str, fontsize: float, font: str) -> float:
    """Advance width of ``text`` in a base-14 free text font. (Not
    ``pymupdf.get_text_length``: it measures æ/ø/å wrongly.) Characters the
    font lacks are drawn by MuPDF with a fallback font – estimated here."""
    name = _FT_METRICS.get(font, "helv")
    f = _FONT_CACHE.get(name)
    if f is None:
        f = _FONT_CACHE[name] = pymupdf.Font(name)
    width = 0.0
    for ch in text:
        code = ord(ch)
        if f.has_glyph(code):
            width += f.text_length(ch, fontsize=fontsize)
        else:  # CJK and other wide scripts take a full em, the rest ~0.6 em
            width += fontsize * (1.0 if code >= 0x1100 else 0.6)
    return width


def _wrapped_line_count(paragraph: str, fontsize: float, font: str, width: float) -> int:
    """Lines a paragraph takes when wrapped greedily at spaces (like MuPDF);
    a word wider than the box is broken between characters."""
    lines, line = 1, ""
    for word in paragraph.split(" "):
        candidate = f"{line} {word}" if line else word
        if _text_width(candidate, fontsize, font) <= width:
            line = candidate
            continue
        if line:
            lines += 1
        line = word
        while len(line) > 1 and _text_width(line, fontsize, font) > width:
            cut = 1
            while cut < len(line) - 1 and _text_width(line[:cut + 1], fontsize, font) <= width:
                cut += 1
            lines += 1
            line = line[cut:]
    return lines


def free_text_size(text: str, fontsize: float, font: str = "sans", width: float | None = None) -> tuple[float, float]:
    """Size (pt) of the box a free text annotation needs for ``text``.

    ``width=None`` gives the natural size (one line per paragraph, no
    wrapping); otherwise the text wraps at ``width`` and only the height
    grows. Mirrors MuPDF's layout (:data:`FREETEXT_LINE_HEIGHT`).
    """
    fontsize = _check_fontsize(fontsize)
    font = _check_font(font)
    paragraphs = _clean_text(text).split("\n")
    slack = 1.5 + 0.05 * fontsize  # keeps MuPDF from wrapping on rounding
    if width is None:
        natural = max((_text_width(p, fontsize, font) for p in paragraphs), default=0.0)
        box_w, lines = natural + slack, len(paragraphs)
    else:
        box_w = max(float(width), fontsize)
        lines = sum(_wrapped_line_count(p, fontsize, font, box_w - slack) for p in paragraphs)
    height = (lines * FREETEXT_LINE_HEIGHT + 0.1) * fontsize
    return box_w, height


def add_free_text(
    pdf: PdfDocument,
    page: int,
    rect: RectLike,
    text: str,
    color: Color = (0.0, 0.0, 0.0),
    fontsize: float = 12.0,
    *,
    font: str = "sans",
    fill: Color | None = None,
    opacity: float = 1.0,
) -> int:
    """Typewriter text box (a standard ``/FreeText`` annotation) in ``rect``
    (display space). The text wraps inside the box and reads upright on
    rotated pages; Danish (and any Unicode) text is supported. ``fill`` is an
    optional background colour. Use :func:`free_text_size` to size ``rect``."""
    rgb = _check_color(color)
    fill_rgb = _check_color(fill) if fill is not None else None
    opacity = _check_opacity(opacity)
    fontsize = _check_fontsize(fontsize)
    font = _check_font(font)
    text = _clean_text(text)
    if not text.strip():
        raise ValueError(tr("core.err.annot_text_empty"))
    box = pymupdf.Rect(rect).normalize()
    if box.is_empty:
        raise ValueError(tr("core.err.annot_empty"))
    with pdf.edit(tr("core.undo.freetext"), CONTENT, (page,)):
        pg = pdf.page(page)
        kwargs: dict[str, Any] = {"fontsize": fontsize, "fontname": _FT_FONTNAMES[font],
                                  "text_color": rgb, "rotate": pg.rotation}
        if fill_rgb is not None:
            kwargs["fill_color"] = fill_rgb
        annot = pg.add_freetext_annot(display_to_page_rect(pg, box), text, **kwargs)
        return _finish(annot, opacity)


# -- queries -------------------------------------------------------------------------
def list_annotations(doc: DocLike, page: int) -> list[AnnotInfo]:
    """All annotations of a page (no links / form fields / popups), in
    drawing order (last = topmost)."""
    pg = as_document(doc)[page]
    return [_info(annot, pg, page) for annot in _top_annots(pg)]


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
    for annot in reversed(_top_annots(pg)):
        if _hits(annot, target, tolerance):
            return _info(annot, pg, page)
    return None


def annotations_in_rect(doc: DocLike, page: int, rect: RectLike, *, contained: bool = False) -> list[AnnotInfo]:
    """Annotations whose rect touches ``rect`` (display; rubber-band
    selection) – or lies completely inside it with ``contained=True``."""
    sel = pymupdf.Rect(rect).normalize()
    out = []
    for info in list_annotations(doc, page):
        r = info.rect
        if contained:
            hit = sel.x0 <= r.x0 and sel.y0 <= r.y0 and r.x1 <= sel.x1 and r.y1 <= sel.y1
        else:  # inclusive overlap: thin lines (zero height) count too
            hit = r.x0 <= sel.x1 and r.x1 >= sel.x0 and r.y0 <= sel.y1 and r.y1 >= sel.y0
        if hit:
            out.append(info)
    return out


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


def _validate_props(props: Mapping[str, Any]) -> None:
    """Raise before any undo step is opened if ``props`` cannot be applied."""
    unknown = [k for k in props if k not in _UPDATABLE]
    if unknown:
        raise TypeError(tr("core.err.annot_prop", name=", ".join(unknown)))
    if props.get("color") is not None:
        _check_color(props["color"])
    if props.get("fill") is not None:
        _check_color(props["fill"])
    if "opacity" in props:
        _check_opacity(props["opacity"])
    if "fontsize" in props:
        _check_fontsize(props["fontsize"])
    if "font" in props:
        _check_font(props["font"])


def _apply_props(doc: pymupdf.Document, pg: pymupdf.Page, xref: int, props: Mapping[str, Any]) -> None:
    """Apply validated ``props`` to one annotation (no undo step of its own).
    Properties that do not apply to the annotation's type are ignored."""
    annot = _load(pg, xref)
    type_id = annot.type[0]
    free_text = type_id == pymupdf.PDF_ANNOT_FREE_TEXT
    appearance: dict[str, Any] = {}  # free text: regenerated from these
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
    if "fill" in props and (type_id in _FILL_TYPES or free_text):
        if props["fill"] is None:
            doc.xref_set_key(xref, "C" if free_text else "IC", "null")
            annot = _load(pg, xref)
        elif free_text:
            appearance["fill_color"] = _check_color(props["fill"])
        else:
            annot.set_colors(fill=_check_color(props["fill"]))
    if props.get("color") is not None:
        if free_text:
            appearance["text_color"] = _check_color(props["color"])
        else:
            annot.set_colors(stroke=_check_color(props["color"]))
    if free_text and "fontsize" in props:
        appearance["fontsize"] = _check_fontsize(props["fontsize"])
    if free_text and "font" in props:
        appearance["fontname"] = _FT_FONTNAMES[_check_font(props["font"])]
    if "opacity" in props:
        annot.set_opacity(_check_opacity(props["opacity"]))
    if "contents" in props:
        text = str(props["contents"] or "")
        annot.set_info(content=_clean_text(text) if free_text else text)
    annot.set_info(modDate=pdf_date())
    annot.update(**appearance)


def update_annotation(pdf: PdfDocument, page: int, xref: int, **props: Any) -> None:
    """Change properties of an annotation.

    Supported: ``color`` (stroke; text colour of free text), ``fill``
    (``None`` removes it; shapes and free text background), ``opacity``,
    ``width`` (border/line width), ``contents`` (note, comment or free text),
    ``rect`` (display space; line, ink and markup geometry is moved/scaled
    with it) and, for free text, ``fontsize`` and ``font``
    (:data:`FREETEXT_FONTS`).
    """
    _validate_props(props)
    _load(pdf.page(page), xref)  # fail early, outside the undo step
    if not props:
        return
    with pdf.edit(tr("core.undo.update_annot"), CONTENT, (page,)):
        _apply_props(pdf.doc, pdf.page(page), xref, props)


def delete_annotation(pdf: PdfDocument, page: int, xref: int) -> None:
    """Remove an annotation with its popup and its whole thread (replies,
    review states, group members)."""
    _load(pdf.page(page), xref)
    with pdf.edit(tr("core.undo.delete_annot"), CONTENT, (page,)):
        _delete_with_thread(pdf.page(page), xref)


# -- group operations (one undo step each) ---------------------------------------------


def _refs(items: Iterable[Sequence[int]]) -> list[AnnotRef]:
    return list(dict.fromkeys((int(p), int(x)) for p, x in items))


def update_annotations(pdf: PdfDocument, changes: Iterable[tuple[int, int, Mapping[str, Any]]]) -> None:
    """Change several annotations at once – one undo step. ``changes`` holds
    ``(page, xref, props)`` with the props of :func:`update_annotation`."""
    items = [(int(p), int(x), dict(props)) for p, x, props in changes if props]
    if not items:
        return
    for p, x, props in items:  # fail before the undo step
        _validate_props(props)
        _load(pdf.page(p), x)
    label = tr("core.undo.update_annots") if len(items) > 1 else tr("core.undo.update_annot")
    with pdf.edit(label, CONTENT, tuple(sorted({p for p, _, _ in items}))):
        for p, x, props in items:
            _apply_props(pdf.doc, pdf.page(p), x, props)


def move_annotations(pdf: PdfDocument, items: Iterable[Sequence[int]], dx: float, dy: float,
                     *, merge: bool = False) -> None:
    """Move annotations ``(page, xref)`` by ``(dx, dy)`` display points as one
    undo step. ``merge=True`` folds the move into the previous undo step when
    that one was a move too (keyboard nudges: one step per burst)."""
    refs = _refs(items)
    if not refs or (not dx and not dy):
        return
    for p, x in refs:
        _load(pdf.page(p), x)
    label = tr("core.undo.move_annots") if len(refs) > 1 else tr("core.undo.move_annot")
    fold = merge and pdf.history.undo_label == label
    with pdf.edit(label, CONTENT, tuple(sorted({p for p, _ in refs}))):
        for p, x in refs:
            pg = pdf.page(p)
            rect = page_to_display_rect(pg, _geometry_rect(pg, _load(pg, x)))
            _apply_props(pdf.doc, pg, x, {"rect": rect + (dx, dy, dx, dy)})
    if fold:
        pdf.history.discard_last()  # the step before the burst stays the undo target


def delete_annotations(pdf: PdfDocument, items: Iterable[Sequence[int]]) -> None:
    """Remove several annotations ``(page, xref)`` – one undo step."""
    refs = _refs(items)
    if not refs:
        return
    for p, x in refs:
        _load(pdf.page(p), x)
    label = tr("core.undo.delete_annots") if len(refs) > 1 else tr("core.undo.delete_annot")
    with pdf.edit(label, CONTENT, tuple(sorted({p for p, _ in refs}))):
        for p, x in refs:
            pg = pdf.page(p)
            if any(a.xref == x for a in pg.annots()):  # may have gone with a deleted parent
                _delete_with_thread(pg, x)


# -- previews ----------------------------------------------------------------------------
def render_preview(
    doc: DocLike,
    page: int,
    changes: Mapping[int, Mapping[str, Any] | None],
    zoom: float,
    clip: RectLike | None = None,
) -> pymupdf.Pixmap | None:
    """Render ``page`` as it would look with ``changes`` (``{xref: props}``;
    ``None`` hides that annotation) – without touching ``doc``: the changes
    are made on a throw-away copy of the page. For hover / drag previews and
    for hiding an annotation while it is edited in place. ``clip`` is display
    space. Returns ``None`` if the preview cannot be made."""
    src = as_document(doc)
    for props in changes.values():
        if props is not None:
            _validate_props(props)
    order = [a.xref for a in src[page].annots()]
    tmp = pymupdf.open()
    try:
        tmp.insert_pdf(src, from_page=page, to_page=page)
        pg = tmp[0]
        copies = [a.xref for a in pg.annots()]
        if len(copies) != len(order):
            return None
        mapping = dict(zip(order, copies))
        for xref, props in changes.items():
            target = mapping.get(xref)
            if target is None:
                continue
            if props is None:
                pg.delete_annot(_load(pg, target))
            else:
                _apply_props(tmp, pg, target, props)
        return render_page(tmp, 0, zoom, clip=clip)
    except Exception:  # a preview must never break the UI
        return None
    finally:
        tmp.close()


__all__ = [
    "AnnotInfo",
    "AnnotRef",
    "FREETEXT_BASELINE",
    "FREETEXT_FONTS",
    "FREETEXT_LINE_HEIGHT",
    "HIT_TOLERANCE",
    "MARKUP_KINDS",
    "SHAPE_KINDS",
    "Selection",
    "add_free_text",
    "add_ink",
    "add_markup",
    "add_note",
    "add_shape",
    "add_text_markup",
    "annotation_at",
    "annotations_in_rect",
    "is_thread_child",
    "thread_children",
    "thread_descendants",
    "delete_annotation",
    "delete_annotations",
    "free_text_size",
    "list_annotations",
    "markup_words",
    "move_annotations",
    "render_preview",
    "update_annotation",
    "update_annotations",
]
