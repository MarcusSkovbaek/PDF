"""Edit existing PDF text so the result looks like it was always there.

Pipeline of :func:`replace_block_text`:

1. Plan. Every character of the new text inherits the style (font, size,
   colour, baseline rise) of the original character it corresponds to
   (character diff), so bold words, superscripts etc. survive. New lines are
   mapped onto the block's original lines (same index -> same baseline);
   lines whose text and style are unchanged are **not touched at all**
   (pixel identical). Changed lines keep the block's alignment (left / right
   / centred / justified) and line spacing. In a wrapped paragraph a line that
   grows too wide reflows into the paragraph's following lines; single
   lines and blocks of independent lines grow sideways within the page's text
   area. A font per style comes from :func:`pdfstudio.core.fonts.resolve_font`
   (embedded -> system -> built-in) and covers every new glyph.
2. Remove the old glyphs of changed lines with redaction annotations that only
   cover a thin band through the middle of each span, applied with
   images/line-art untouched and no fill – backgrounds, images and vector art
   stay. Pending user redactions on the page are left pending.
3. Write the new lines with :class:`pymupdf.TextWriter` at the original
   baselines, in the line's original direction.
4. Deduplicate font programs: when the written font is byte-identical to a
   font file already in the PDF (e.g. the original embedded font), the new
   font descriptor is pointed at the existing stream.

Beyond plain text, an edit can carry character formatting for parts of the
text (``formats``: bold, italic, underline, strike-through, colour, size,
font – applied on top of the inherited styles), a new alignment
(left / centre / right / justified) and line spacing. Underlines and
strike-through lines are vector strokes in a PDF: existing ones are found
next to the text (:func:`_attach_decorations`), removed with the text they
belong to and redrawn under the new text. When the edited text becomes
taller or shorter, the text below it in the same column moves along
(:func:`_reflow`) as long as nothing else (images, drawings, other columns)
is in the way and it stays on the page; otherwise the edit reports the
overflow (:class:`EditResult`) so the UI can offer to shrink the text
(:func:`fit_scale`).

All public geometry is in display space (page rotation applied). Internally
PyMuPDF text extraction, redactions and TextWriter positions all use the
unrotated page space ("raw"), which is what each line keeps privately.
"""

from __future__ import annotations

import hashlib
import math
import re
from dataclasses import dataclass, field, fields, replace

import pymupdf

from .document import CONTENT
from .fonts import (
    SpanStyle,
    clean_font_name,
    family_key,
    styled_font_name,
    _is_mono_name,
    _serif_from_name,
    has_space,
    prepare_text,
    resolve_font,
    style_from_span,
    text_width,
)

__all__ = [
    "EditResult",
    "TextLine",
    "TextBlock",
    "text_blocks",
    "block_at",
    "warm_page",
    "fit_scale",
    "replace_block_text",
    "write_runs",
    "TextRun",
]

_EXTRACT_FLAGS = pymupdf.TEXT_PRESERVE_WHITESPACE | pymupdf.TEXT_MEDIABOX_CLIP


def _label(key: str, default: str) -> str:
    try:
        from ..i18n import tr

        text = tr(key)
        return default if text == key else text
    except Exception:  # pragma: no cover - i18n is optional for core
        return default


# ---------------------------------------------------------------------------
# data model
# ---------------------------------------------------------------------------
@dataclass
class TextLine:
    text: str
    bbox: pymupdf.Rect  # display space
    origin: tuple[float, float]  # display space (baseline start)
    style: SpanStyle
    # -- internal geometry in unrotated page space ---------------------------
    dir: tuple[float, float] = field(default=(1.0, 0.0), repr=False)
    raw_origin: tuple[float, float] = field(default=(0.0, 0.0), repr=False)
    raw_bbox: pymupdf.Rect | None = field(default=None, repr=False)
    extent: tuple[float, float] = field(default=(0.0, 0.0), repr=False)  # start/end along dir
    span_rects: list = field(default_factory=list, repr=False)  # raw span boxes
    char_rects: list = field(default_factory=list, repr=False)  # raw char boxes
    ascender: float = field(default=0.9, repr=False)
    descender: float = field(default=-0.25, repr=False)
    space_em: float | None = field(default=None, repr=False)  # measured space advance / size
    char_styles: list = field(default_factory=list, repr=False)  # SpanStyle per char of ``text``
    char_rises: list = field(default_factory=list, repr=False)  # baseline offset per char (pt)
    char_pos: list = field(default_factory=list, repr=False)  # (start, end) along dir per char (raw)
    display_dir: tuple[float, float] = field(default=(1.0, 0.0), repr=False)  # dir on screen
    decor: dict = field(default_factory=dict, repr=False)  # "underline"/"strike" -> (offset along n, thickness)
    decor_rects: list = field(default_factory=list, repr=False)  # raw rects of those strokes
    cell: bool = field(default=False, repr=False)  # part of a row of table cells


@dataclass
class TextBlock:
    page: int
    bbox: pymupdf.Rect  # display space
    lines: list[TextLine]
    style: SpanStyle
    rotation: int = field(default=0, repr=False)

    @property
    def text(self) -> str:
        return "\n".join(line.text for line in self.lines)

    @property
    def angle(self) -> float:
        """Direction of the text as displayed, in degrees (y axis down, so
        positive is clockwise): 0 = left to right, 90 = top to bottom,
        -90 = bottom to top, 180 = upside down."""
        if not self.lines:
            return 0.0
        dx, dy = self.lines[0].display_dir
        a = math.degrees(math.atan2(dy, dx))
        snapped = round(a / 90.0) * 90.0
        a = snapped if abs(a - snapped) < 0.05 else a
        return 180.0 if a <= -180.0 else a + 0.0


@dataclass
class TextRun:
    """One line of text to write (raw page coordinates)."""

    text: str
    font: pymupdf.Font
    size: float
    color: tuple
    origin: tuple[float, float]
    dir: tuple[float, float] = (1.0, 0.0)
    space: float | None = None  # space advance (pt) if the font lacks a space glyph
    width: float = 0.0          # advance width (pt) – for underline / strike-through
    underline: tuple | None = None   # (offset below the baseline, thickness) to draw
    strike: tuple | None = None      # (offset above the baseline, thickness)


# ---------------------------------------------------------------------------
# small geometry helpers
# ---------------------------------------------------------------------------
def _dot(p, d) -> float:
    return p[0] * d[0] + p[1] * d[1]


def _perp(d) -> tuple[float, float]:
    # next line direction for text running along d (y axis points down)
    return (-d[1], d[0])


def _proj_range(rect, d) -> tuple[float, float]:
    x0, y0, x1, y1 = rect
    a = x0 * d[0] if d[0] >= 0 else x1 * d[0]
    b = x1 * d[0] if d[0] >= 0 else x0 * d[0]
    c = y0 * d[1] if d[1] >= 0 else y1 * d[1]
    e = y1 * d[1] if d[1] >= 0 else y0 * d[1]
    return a + c, b + e


def _union(boxes) -> pymupdf.Rect:
    boxes = list(boxes)
    return pymupdf.Rect(min(b[0] for b in boxes), min(b[1] for b in boxes),
                        max(b[2] for b in boxes), max(b[3] for b in boxes))


def _is_axis(d) -> bool:
    return abs(d[0]) < 1e-3 or abs(d[1]) < 1e-3


def _style_key(style: SpanStyle) -> tuple:
    return (style.font, round(style.size, 2), tuple(round(c, 3) for c in style.color),
            style.bold, style.italic, style.serif, style.mono, style.underline, style.strike)


def _font_key(style: SpanStyle) -> tuple:
    """What picks the font program (decorations do not)."""
    return (style.font, style.bold, style.italic, style.serif, style.mono)


# ---------------------------------------------------------------------------
# extraction
# ---------------------------------------------------------------------------
def _segments(chars: list, d) -> list[list]:
    """Split a PyMuPDF line at gaps that are clearly not word spaces (table
    cells, tab stops, columns sharing a baseline)."""
    vis = [i for i, c in enumerate(chars) if not c[0].isspace()]
    if len(vis) < 2:
        return [chars]
    gaps = []  # (index of next visible char, gap, size, is word gap)
    for a, b in zip(vis, vis[1:]):
        end_a = _proj_range(chars[a][2], d)[1]
        start_b = _proj_range(chars[b][2], d)[0]
        size = float(chars[b][3].get("size", 0) or chars[a][3].get("size", 0) or 10)
        g = start_b - end_a
        word = b - a > 1 or g > 0.2 * size
        gaps.append((b, g, size, word))
    word_gaps = [g for _b, g, _s, w in gaps if w]
    cuts = []
    for b, g, size, word in gaps:
        if not word:
            continue
        others = list(word_gaps)
        others.remove(g)
        ref = min(others) if others else None
        if g > 1.2 * size or (ref is not None and g > 0.45 * size and g > 1.8 * max(ref, 0.05 * size)):
            cuts.append(b)
    if not cuts:
        return [chars]
    out, prev = [], 0
    for b in cuts:
        out.append(chars[prev:b])
        prev = b
    out.append(chars[prev:])
    return out


def _build_lines(raw_line: dict, rm: pymupdf.Matrix) -> list[TextLine]:
    d = tuple(raw_line.get("dir", (1.0, 0.0)))
    chars = []  # (c, origin, bbox, span)
    for span in raw_line.get("spans", ()):
        if span.get("alpha", 255) == 0:
            continue  # invisible text (e.g. an OCR layer) is not editable
        for ch in span.get("chars", ()):
            chars.append((ch["c"], ch["origin"], tuple(ch["bbox"]), span))
    if not chars:
        return []
    out = []
    for seg in _segments(chars, d):
        ln = _line_from_chars(seg, d, rm)
        if ln is not None:
            out.append(ln)
    return out


def _char_positions(chars: list, d) -> list[tuple[float, float]]:
    """(start, end) of every char along the writing direction ``d``. Char
    boxes are axis-aligned, so for slanted text (a 30° stamp) the advance is
    the distance to the next glyph's origin, and for the last glyph it is
    recovered from its box and the font's height."""
    if _is_axis(d):
        return [(_dot(c[1], d), _proj_range(c[2], d)[1]) for c in chars]
    cos, sin = abs(d[0]), abs(d[1])
    out = []
    for k, c in enumerate(chars):
        s0 = _dot(c[1], d)
        if k + 1 < len(chars):
            out.append((s0, max(s0, _dot(chars[k + 1][1], d))))
            continue
        x0, y0, x1, y1 = c[2]
        span = c[3]
        size = float(span.get("size", 10) or 10)
        h = size * (float(span.get("ascender", 0.9) or 0.9) - float(span.get("descender", -0.25) or -0.25))
        # an advance a and height h rotated: box = (a·cos + h·sin) × (a·sin + h·cos)
        adv = ((x1 - x0) - h * sin) / cos if cos >= sin else ((y1 - y0) - h * cos) / sin
        out.append((s0, s0 + max(adv, 0.0)))
    return out


def _line_from_chars(chars: list, d, rm: pymupdf.Matrix) -> TextLine | None:
    first = next((i for i, c in enumerate(chars) if not c[0].isspace()), None)
    if first is None:
        return None
    last = max(i for i, c in enumerate(chars) if not c[0].isspace())
    used = chars[first: last + 1]
    text = "".join(c[0] for c in used)

    # dominant span by visible char count -> style
    counts: dict[int, int] = {}
    span_by_id: dict[int, dict] = {}
    for c in used:
        if not c[0].isspace():
            sid = id(c[3])
            counts[sid] = counts.get(sid, 0) + 1
            span_by_id[sid] = c[3]
    dom = span_by_id[max(counts, key=counts.get)]
    span_styles: dict[int, SpanStyle] = {}
    for c in used:
        sid = id(c[3])
        if sid not in span_styles:
            sp = c[3]
            span_styles[sid] = style_from_span(sp.get("font", ""), sp.get("size", 11), sp.get("color", 0),
                                               sp.get("flags", 0))
    style = span_styles[id(dom)]

    raw_bbox = _union(c[2] for c in used)
    raw_origin = (float(used[0][1][0]), float(used[0][1][1]))
    start = _dot(raw_origin, d)
    char_pos = _char_positions(used, d)
    end = max(char_pos[i][1] for i, c in enumerate(used) if not c[0].isspace())

    span_rects = []
    for sid in span_by_id:
        span_rects.append(_union(c[2] for c in used if id(c[3]) == sid and not c[0].isspace()))
    char_rects = [c[2] for c in used if not c[0].isspace()]
    space_widths = [abs(_proj_range(c[2], d)[1] - _proj_range(c[2], d)[0]) / max(float(c[3].get("size", 1) or 1), 1e-3)
                    for c in used if c[0] == " "]
    space_widths = [w for w in space_widths if 0.05 < w < 1.0]
    space_em = sorted(space_widths)[len(space_widths) // 2] if space_widths else None

    nrm = _perp(d)
    base_n = _dot(raw_origin, nrm)
    char_styles = [span_styles[id(c[3])] for c in used]
    char_rises = [round(_dot(c[1], nrm) - base_n, 2) for c in used]
    if all(abs(r) < 0.05 for r in char_rises):
        char_rises = [0.0] * len(used)

    disp_bbox = pymupdf.Rect(raw_bbox) * rm
    disp_origin = pymupdf.Point(raw_origin) * rm
    disp_dir = pymupdf.Point(d) * rm - pymupdf.Point(0, 0) * rm
    return TextLine(
        text=text,
        bbox=disp_bbox,
        origin=(disp_origin.x, disp_origin.y),
        style=style,
        dir=(float(d[0]), float(d[1])),
        raw_origin=raw_origin,
        raw_bbox=raw_bbox,
        extent=(start, end),
        span_rects=span_rects,
        char_rects=char_rects,
        ascender=float(dom.get("ascender", 0.9) or 0.9),
        descender=float(dom.get("descender", -0.25) or -0.25),
        space_em=space_em,
        char_styles=char_styles,
        char_rises=char_rises,
        char_pos=char_pos,
        display_dir=(float(disp_dir.x), float(disp_dir.y)),
    )


def _join_fragments(lines: list[TextLine]) -> set[int]:
    """Pieces of one text line that MuPDF reports as separate lines (text
    written out of order – e.g. words in another font of an edited line are
    written after the rest) are joined: same direction and baseline, and the
    gap between them no wider than a word space. Returns the ids of the
    lines that were merged into others."""
    gone: set[int] = set()
    cand = [ln for ln in lines if _is_axis(ln.dir) and len(ln.char_pos) == len(ln.text)]
    if len(cand) < 2:
        return gone
    rows: dict[tuple, list[TextLine]] = {}
    for ln in cand:
        n = _perp(ln.dir)
        rows.setdefault((round(ln.dir[0], 3), round(ln.dir[1], 3), round(_dot(ln.raw_origin, n) * 2)), []).append(ln)
    for row in rows.values():
        if len(row) < 2:
            continue
        row.sort(key=lambda ln: ln.extent[0])
        cur = row[0]
        for ln in row[1:]:
            size = max(cur.style.size, ln.style.size, 1.0)
            n = _perp(cur.dir)
            gap = ln.extent[0] - cur.extent[1]
            if (-0.5 <= gap <= 0.5 * size and abs(_dot(ln.raw_origin, n) - _dot(cur.raw_origin, n)) <= 0.35
                    and not _is_bullet(cur) and not _is_bullet(ln)):
                _absorb(cur, ln, gap)
                gone.add(id(ln))
            else:
                cur = ln
    return gone


def _absorb(a: TextLine, b: TextLine, gap: float) -> None:
    """Append line ``b`` (to the right of ``a`` on its baseline) to ``a``."""
    size = max(a.style.size, 1.0)
    space = gap > 0.12 * size
    sa = a.char_styles or [a.style] * len(a.text)
    sb = b.char_styles or [b.style] * len(b.text)
    ra = a.char_rises or [0.0] * len(a.text)
    rb = b.char_rises or [0.0] * len(b.text)
    a.text = a.text + (" " if space else "") + b.text
    a.char_styles = sa + ([sa[-1]] if space else []) + sb
    a.char_rises = ra + ([0.0] if space else []) + rb
    a.char_pos = a.char_pos + ([(a.extent[1], b.extent[0])] if space else []) + b.char_pos
    a.char_rects = a.char_rects + b.char_rects
    a.span_rects = a.span_rects + b.span_rects
    a.raw_bbox = pymupdf.Rect(a.raw_bbox) | b.raw_bbox
    a.bbox = pymupdf.Rect(a.bbox) | b.bbox
    a.extent = (a.extent[0], max(a.extent[1], b.extent[1]))
    for k, v in b.decor.items():
        a.decor.setdefault(k, v)
    a.decor_rects = a.decor_rects + b.decor_rects
    counts: dict[int, int] = {}
    by_id: dict[int, SpanStyle] = {}
    for ch, st in zip(a.text, a.char_styles):
        if not ch.isspace():
            counts[id(st)] = counts.get(id(st), 0) + 1
            by_id[id(st)] = st
    if counts:
        a.style = by_id[max(counts, key=counts.get)]


def _dominant_style(lines: list[TextLine]) -> SpanStyle:
    weights: dict[tuple, int] = {}
    styles: dict[tuple, SpanStyle] = {}
    for ln in lines:
        k = _style_key(ln.style)
        weights[k] = weights.get(k, 0) + len(ln.text.strip())
        styles.setdefault(k, ln.style)
    return styles[max(weights, key=weights.get)]


_BLOCK_CACHE: "OrderedDict[tuple, list[TextBlock]]" = None  # type: ignore[assignment]
_BLOCK_CACHE_MAX = 24


def text_blocks(doc: pymupdf.Document, page: int, *, merge: bool = True) -> list[TextBlock]:
    """The editable text blocks of a page (display coordinates). Blocks that
    continue each other (same direction, size and line pitch, overlapping
    horizontally) are merged into one paragraph.

    Results are cached per page *content* (so repeated calls – the editor,
    hover, an edit's planning – cost nothing until the page changes, also
    across undo). Treat the returned blocks as read-only."""
    global _BLOCK_CACHE
    from collections import OrderedDict

    from .fonts import _page_key

    pg = doc[page]
    if _BLOCK_CACHE is None:
        _BLOCK_CACHE = OrderedDict()
    page_key = (_page_key(pg), tuple(pg.rect))
    key = page_key + (merge,)
    hit = _BLOCK_CACHE.get(key)
    if hit is not None:
        _BLOCK_CACHE.move_to_end(key)
        return list(hit)
    # the merged and the unmerged view share one extraction
    base = _BLOCK_CACHE.get(page_key + ("groups",))
    if base is None:
        base = _extract_groups(pg)
        _BLOCK_CACHE[page_key + ("groups",)] = base
    blocks = _extract_blocks(pg, page, merge, base)
    _BLOCK_CACHE[key] = blocks
    while len(_BLOCK_CACHE) > _BLOCK_CACHE_MAX:
        _BLOCK_CACHE.popitem(last=False)
    return list(blocks)


def _extract_groups(pg: pymupdf.Page) -> tuple[list[list[TextLine]], dict[int, float]]:
    """Lines of the page grouped into PyMuPDF's blocks (split at columns,
    size changes and list items), before paragraphs are merged."""
    rm = pg.rotation_matrix
    data = pg.get_text("rawdict", flags=_EXTRACT_FLAGS)
    groups: list[list[TextLine]] = []
    raw_lines: list[TextLine] = []
    per_block = []
    for b in data.get("blocks", ()):
        if b.get("type", 0) != 0:
            continue
        lines = [ln for rl in b.get("lines", ()) for ln in _build_lines(rl, rm)]
        per_block.append(lines)
        raw_lines.extend(lines)
    gone = _join_fragments(raw_lines)
    if gone:
        raw_lines = [ln for ln in raw_lines if id(ln) not in gone]
        per_block = [[ln for ln in lines if id(ln) not in gone] for lines in per_block]
    _mark_table_rows(raw_lines, pg.rect * pg.derotation_matrix)
    for lines in per_block:
        if lines:
            for col in _split_columns(lines):
                for part in (p for sc in _split_size_changes(col) for p in _split_cell_rows(sc)):
                    # a wide line above (a heading, a paragraph) can bind side-by-side
                    # pieces (bullet + list item, table cells) into one column: re-split
                    groups.extend(_split_columns(part) if len(part) < len(col) else [part])
    groups = _split_interleaved(groups)
    _attach_decorations(pg, raw_lines)
    items = _list_item_starts(raw_lines)
    if items:
        groups = [g for grp in groups for g in _split_list_items(grp, items)]
    return groups, items


def _extract_blocks(pg: pymupdf.Page, page: int, merge: bool, base=None) -> list[TextBlock]:
    groups, items = base if base is not None else _extract_groups(pg)
    groups = [list(g) for g in groups]
    if merge:
        groups = _merge_groups(groups, items)
    blocks: list[TextBlock] = []
    for lines in groups:
        bbox = pymupdf.Rect(lines[0].bbox)
        for ln in lines[1:]:
            bbox |= ln.bbox
        blocks.append(TextBlock(page=page, bbox=bbox, lines=lines, style=_dominant_style(lines),
                                rotation=pg.rotation))
    return blocks


#: Underline / strike-through strokes: max thickness relative to the text size,
#: and where they sit relative to the baseline (× size; positive = below).
_DECOR_MAX = 0.16
_UNDERLINE_ZONE = (0.02, 0.36)
_STRIKE_ZONE = (-0.5, -0.12)


def _strokes(page: pymupdf.Page) -> list[tuple[float, float, float, float, pymupdf.Rect]]:
    """Thin horizontal strokes / filled bars that may underline or strike
    through text: ``(x0, x1, y centre, thickness, raw rect)``."""
    try:
        paths = page.get_drawings()
    except Exception:
        return []
    out = []
    for p in paths:
        items = p.get("items") or ()
        if len(items) != 1:
            continue
        kind = items[0][0]
        if kind == "l":
            a, b = items[0][1], items[0][2]
            if abs(a.y - b.y) > 0.25 or p.get("color") is None:
                continue
            w = float(p.get("width") or 1.0)
            x0, x1 = min(a.x, b.x), max(a.x, b.x)
            out.append((x0, x1, (a.y + b.y) / 2, w, pymupdf.Rect(x0, a.y - w / 2, x1, a.y + w / 2)))
        elif kind in ("re", "qu") and p.get("fill") is not None:
            r = pymupdf.Rect(items[0][1]) if kind == "re" else pymupdf.Quad(items[0][1]).rect
            if r.height <= 0 or r.width < 1.0:
                continue
            out.append((r.x0, r.x1, (r.y0 + r.y1) / 2, r.height, r))
    return out


def _attach_decorations(page: pymupdf.Page, lines: list[TextLine]) -> None:
    """Mark characters that have an underline / strike-through stroke (left to
    right text): their style gets ``underline`` / ``strike``, and the line
    remembers the stroke geometry so an edit can remove and redraw it."""
    horizontal = [ln for ln in lines if abs(ln.dir[1]) < 1e-3 and ln.dir[0] > 0
                  and len(ln.char_pos) == len(ln.text) == len(ln.char_styles)]
    if not horizontal:
        return
    strokes = _strokes(page)
    if not strokes:
        return
    cache: dict[tuple[int, str], SpanStyle] = {}
    for ln in horizontal:
        size = ln.style.size or 10.0
        base = ln.raw_origin[1]
        for x0, x1, y, w, rect in strokes:
            if w > _DECOR_MAX * size or x1 < ln.extent[0] - 1.0 or x0 > ln.extent[1] + 1.0:
                continue
            off = (y - base) / size
            if _UNDERLINE_ZONE[0] <= off <= _UNDERLINE_ZONE[1]:
                kind = "underline"
            elif _STRIKE_ZONE[0] <= off <= _STRIKE_ZONE[1]:
                kind = "strike"
            else:
                continue
            styles = list(ln.char_styles)
            hit = False
            for i, (a, b) in enumerate(ln.char_pos):
                if x0 - 0.5 <= (a + b) / 2 <= x1 + 0.5:
                    st = styles[i]
                    key = (id(st), kind)
                    if key not in cache:
                        cache[key] = replace(st, **{kind: True})
                    styles[i] = cache[key]
                    hit = True
            if hit:
                ln.char_styles = styles
                ln.decor[kind] = (abs(y - base), w)
                ln.decor_rects.append(rect)


def _split_columns(lines: list[TextLine]) -> list[list[TextLine]]:
    """Split one PyMuPDF block into side-by-side columns (lines that do not
    overlap along the writing direction), e.g. table cells."""
    if len(lines) < 2 or not all(_is_axis(ln.dir) and ln.dir == lines[0].dir for ln in lines):
        return [lines]
    order = sorted(range(len(lines)), key=lambda i: lines[i].extent[0])
    clusters: list[list[int]] = []
    hi = None
    for i in order:
        a, b = lines[i].extent
        if clusters and a < hi - 0.5:
            clusters[-1].append(i)
            hi = max(hi, b)
        else:
            clusters.append([i])
            hi = b
    if len(clusters) == 1:
        return [lines]
    return [[lines[i] for i in sorted(c)] for c in clusters]


_BULLETS = set("•◦▪▫‣⁃●○■□◆◇►▸▹➢➤✓✔–—-*·")


_MARKER_ONLY = re.compile(r"^\(?(?:\d{1,3}|[a-zA-Z]|[ivxlcIVXLC]{1,6})[.)]$")


def _is_bullet(ln: TextLine) -> bool:
    """A bullet glyph, or a list number standing apart from its item ("a." set
    with a tab before the text)."""
    t = ln.text.strip()
    if len(t) == 1 and (t in _BULLETS or 0xE000 <= ord(t) <= 0xF8FF):  # PUA: Symbol/OpenSymbol bullets
        return True
    return bool(_MARKER_ONLY.match(t))


_BULLET_SYMBOLS = set("•·◦▪▫‣⁃●○■□◆◇►▸▹➢➤✓✔")


def is_list_bullet(block: TextBlock) -> bool:
    """A block that is nothing but a list bullet glyph (often a private-use
    Symbol/OpenSymbol character no other font has): editors skip it – its
    list item next to it is the editable text."""
    t = block.text.strip()
    return len(t) == 1 and (t in _BULLET_SYMBOLS or 0xE000 <= ord(t) <= 0xF8FF)


#: A list marker at the start of a line, followed by a space: "1." "2)" "a."
#: "b)" "iv." "(3)" "•" "–" … (numbered lists keep the number in the line).
_MARKER = re.compile(r"^(?:\(?(?:\d{1,3}|[a-zA-Z]|[ivxlcIVXLC]{1,6})[.)]|[•◦▪▫‣⁃●○■□◆◇►▸▹➢➤✓✔–—*·-])\s+\S")


def marker_end(text: str) -> int:
    """Index of the first character after a leading list marker and its
    spaces (0 when the line does not start with one)."""
    m = _MARKER.match(text)
    return m.end() - 1 if m else 0


def _list_item_starts(lines: list[TextLine]) -> dict[int, float]:
    """``id(line) -> bullet start`` for every text line that starts a list
    item: a bullet glyph on the same row just left of it (Word/LibreOffice
    lists, slides), or a marker at the start of the line itself ("1. …",
    "a) …", "• …"). Bullets themselves map to their own start too."""
    out: dict[int, float] = {}
    for ln in lines:
        if _is_axis(ln.dir) and not _is_bullet(ln) and marker_end(ln.text):
            out[id(ln)] = ln.extent[0]
    bullets = [ln for ln in lines if _is_axis(ln.dir) and _is_bullet(ln)]
    if not bullets:
        return out
    for bl in bullets:
        n = _perp(bl.dir)
        row = _dot(bl.raw_origin, n)
        best = None
        for ln in lines:
            if ln is bl or ln.dir != bl.dir or _is_bullet(ln):
                continue
            gap = ln.extent[0] - bl.extent[1]
            if abs(_dot(ln.raw_origin, n) - row) <= 0.3 * max(ln.style.size, 1.0) and \
                    0 <= gap <= 4 * max(ln.style.size, 1.0) and (best is None or gap < best[0]):
                best = (gap, ln)
        if best is not None:
            out[id(best[1])] = bl.extent[0]
            out[id(bl)] = bl.extent[0]
    return out


def _split_list_items(lines: list[TextLine], items: dict[int, float]) -> list[list[TextLine]]:
    """Split a block at list items: every bullet is its own block, every
    item starts a new block, and text after the list (starting left of the
    item text, at or before the bullet) is a paragraph of its own."""
    if not any(id(ln) in items for ln in lines):
        return [lines]
    out: list[list[TextLine]] = []
    item_x: float | None = None   # text start of the current item
    for ln in lines:
        if id(ln) in items and _is_bullet(ln):
            out.append([ln])
            item_x = None
            continue
        if id(ln) in items:
            out.append([ln])
            item_x = ln.extent[0]
        elif item_x is not None and ln.extent[0] < item_x - 0.5 * max(ln.style.size, 1.0):
            out.append([ln])          # back at the margin: the list ended
            item_x = None
        elif out and not (len(out[-1]) == 1 and _is_bullet(out[-1][0]) and id(out[-1][0]) in items):
            out[-1].append(ln)
        else:
            out.append([ln])
    return out


def _split_size_changes(lines: list[TextLine]) -> list[list[TextLine]]:
    """Split one PyMuPDF block where the text size jumps from one row to the
    next: a caption or heading set tight above a paragraph is a paragraph of
    its own (MuPDF happily puts both in one block). Uses the same size
    tolerance as :func:`_continues`, so the parts are never merged back."""
    out = [[lines[0]]]
    for prev, ln in zip(lines, lines[1:]):
        sa, sb = prev.style.size, ln.style.size
        if (sa and sb and not (0.85 <= sb / sa <= 1.18)
                and abs(prev.dir[0] - ln.dir[0]) < 1e-3 and abs(prev.dir[1] - ln.dir[1]) < 1e-3):
            n = _perp(prev.dir)
            if _dot(ln.raw_origin, n) - _dot(prev.raw_origin, n) > 0.5 * min(sa, sb):  # the next row
                out.append([ln])
                continue
        out[-1].append(ln)
    return out


def _split_cell_rows(lines: list[TextLine]) -> list[list[TextLine]]:
    """Split one PyMuPDF block between a row of table cells (several pieces
    on one baseline, far apart) and ordinary lines above or below it – MuPDF
    puts a table's last row and the paragraph set tight under it in one
    block. Keeps the lines' order within each part."""
    if len(lines) < 3 or not all(_is_axis(ln.dir) and ln.dir == lines[0].dir for ln in lines):
        return [lines]
    n = _perp(lines[0].dir)
    order = sorted(lines, key=lambda ln: (_dot(ln.raw_origin, n), ln.extent[0]))
    rows: list[list[TextLine]] = [[order[0]]]
    for ln in order[1:]:
        ref = rows[-1][0]
        if abs(_dot(ln.raw_origin, n) - _dot(ref.raw_origin, n)) <= 0.35 * max(ref.style.size, 1.0):
            rows[-1].append(ln)
        else:
            rows.append([ln])
    if len(rows) < 2:
        return [lines]

    def cells(row: list[TextLine]) -> bool:
        row = sorted(row, key=lambda ln: ln.extent[0])
        return len(row) >= 2 and all(b.extent[0] - a.extent[1] >= 2.0 * max(a.style.size, 1.0)
                                     for a, b in zip(row, row[1:]))

    kinds = [cells(r) or all(ln.cell for ln in r) for r in rows]
    for row, is_cells in zip(rows, kinds):
        for ln in row:
            ln.cell = is_cells
    if all(kinds) or not any(kinds):
        return [lines]
    part_of: dict[int, int] = {}
    part = 0
    for k, row in enumerate(rows):
        if k and kinds[k] != kinds[k - 1]:
            part += 1
        for ln in row:
            part_of[id(ln)] = part
    out: list[list[TextLine]] = [[] for _ in range(part + 1)]
    for ln in lines:
        out[part_of[id(ln)]].append(ln)
    return out


def _mark_table_rows(lines: list[TextLine], raw_rect: pymupdf.Rect) -> None:
    """Flag the lines of rows of table cells across the whole page: at least
    three narrow pieces on one baseline, far apart (MuPDF may put the cells
    of one row – an edited one above all – in different blocks). Two pieces
    are not enough: side-by-side text columns look like that too."""
    by_dir: dict[tuple, list[TextLine]] = {}
    for ln in lines:
        if _is_axis(ln.dir) and ln.text.strip():
            by_dir.setdefault(ln.dir, []).append(ln)
    for d, group in by_dir.items():
        n = _perp(d)
        width = raw_rect.width if abs(d[0]) > 0.5 else raw_rect.height
        group.sort(key=lambda ln: _dot(ln.raw_origin, n))
        rows: list[list[TextLine]] = []
        for ln in group:
            if rows and _dot(ln.raw_origin, n) - _dot(rows[-1][0].raw_origin, n) <= 0.35 * max(ln.style.size, 1.0):
                rows[-1].append(ln)
            else:
                rows.append([ln])
        for row in rows:
            if len(row) < 3:
                continue
            row.sort(key=lambda ln: ln.extent[0])
            if all(ln.extent[1] - ln.extent[0] <= 0.25 * width for ln in row) and \
                    all(y.extent[0] - x.extent[1] >= 2.0 * max(x.style.size, 1.0) for x, y in zip(row, row[1:])):
                for ln in row:
                    ln.cell = True


def _split_interleaved(groups: list[list[TextLine]]) -> list[list[TextLine]]:
    """Split a group between two of its lines when a line of another group
    lies between them (same column, between the baselines): after an edit
    rewrote the first line of a list item, MuPDF reports the rest of that
    item in one block with the lines of the item above."""
    if len(groups) < 2:
        return groups
    all_lines = [(gi, ln) for gi, g in enumerate(groups) for ln in g if _is_axis(ln.dir)]
    out: list[list[TextLine]] = []
    for gi, g in enumerate(groups):
        if len(g) < 2 or not all(_is_axis(ln.dir) and ln.dir == g[0].dir for ln in g):
            out.append(g)
            continue
        n = _perp(g[0].dir)
        order = sorted(g, key=lambda ln: _dot(ln.raw_origin, n))
        part = [order[0]]
        for a, b in zip(order, order[1:]):
            ya, yb = _dot(a.raw_origin, n), _dot(b.raw_origin, n)
            pad = 0.4 * max(a.style.size, 1.0)
            lo = max(a.extent[0], b.extent[0])
            hi = min(a.extent[1], b.extent[1])
            between = yb - ya > 1.6 * max(a.style.size, 1.0) and any(
                hj != gi and ln.dir == a.dir and ya + pad < _dot(ln.raw_origin, n) < yb - pad
                and min(hi, ln.extent[1]) - max(lo, ln.extent[0]) > 0
                for hj, ln in all_lines)
            if between:
                ids = {id(x) for x in part}
                out.append([ln for ln in g if id(ln) in ids])
                part = [b]
            else:
                part.append(b)
        ids = {id(x) for x in part}
        out.append([ln for ln in g if id(ln) in ids] if len(part) < len(g) else g)
    return out


def _pitch(lines: list[TextLine]) -> float | None:
    if len(lines) < 2:
        return None
    n = _perp(lines[0].dir)
    gaps = sorted(_dot(lines[i + 1].raw_origin, n) - _dot(lines[i].raw_origin, n) for i in range(len(lines) - 1))
    g = gaps[len(gaps) // 2]
    return g if g > 0 else None


def _continues(upper: list[TextLine], lower: list[TextLine]) -> bool:
    """Is ``lower`` the next line(s) of the paragraph ``upper``?"""
    a, b = upper[-1], lower[0]
    if abs(a.dir[0] - b.dir[0]) > 1e-3 or abs(a.dir[1] - b.dir[1]) > 1e-3:
        return False
    if a.cell != b.cell:  # a row of table cells never runs on into ordinary text
        return False
    sa, sb = a.style.size, b.style.size
    if not sa or not sb or not (0.85 <= sb / sa <= 1.18):
        return False
    n = _perp(a.dir)
    pitch = _dot(b.raw_origin, n) - _dot(a.raw_origin, n)
    size = max(sa, sb)
    if not (0.9 * size <= pitch <= 1.6 * size):
        return False
    for p in (_pitch(upper), _pitch(lower)):
        if p is not None and abs(pitch - p) > 0.2 * p:
            return False
    if _pitch(upper) is None and _pitch(lower) is None and pitch > 1.35 * size:
        return False
    lo = max(min(ln.extent[0] for ln in upper), min(ln.extent[0] for ln in lower))
    hi = min(max(ln.extent[1] for ln in upper), max(ln.extent[1] for ln in lower))
    shorter = min(a.extent[1] - a.extent[0], b.extent[1] - b.extent[0])
    return hi - lo >= 0.5 * max(shorter, 1.0)


def _merge_groups(groups: list[list[TextLine]], items: dict[int, float] | None = None) -> list[list[TextLine]]:
    """Join blocks that are consecutive lines of one paragraph but were split
    by content-stream order (typically after an edit appended new text).
    ``items`` (list item starts): a new item never continues the text above
    it, and text back at the margin does not continue a list item."""
    items = items or {}
    if len(groups) < 2:
        return groups
    import bisect

    by_dir: dict[tuple, list[list[TextLine]]] = {}
    for g in groups:
        by_dir.setdefault((round(g[0].dir[0], 3), round(g[0].dir[1], 3)), []).append(g)
    out: list[list[TextLine]] = []
    for gs in by_dir.values():
        n = _perp(gs[0][0].dir)
        changed = True
        while changed and len(gs) > 1:
            changed = False
            gs.sort(key=lambda g: _dot(g[0].raw_origin, n))
            keys = [_dot(g[0].raw_origin, n) for g in gs]
            used: set[int] = set()
            result = []
            for i, g in enumerate(gs):
                if i in used:
                    continue
                used.add(i)
                merged = g
                while True:
                    last = merged[-1]
                    p = _dot(last.raw_origin, n)
                    lo = bisect.bisect_left(keys, p + 0.85 * last.style.size)
                    hi = bisect.bisect_right(keys, p + 1.65 * last.style.size)
                    for j in range(lo, hi):
                        if j in used or id(gs[j][0]) in items or _is_bullet(gs[j][0]) or _is_bullet(merged[0]):
                            continue
                        if id(merged[0]) in items and gs[j][0].extent[0] < \
                                merged[0].extent[0] - 0.5 * max(gs[j][0].style.size, 1.0):
                            continue
                        if _continues(merged, gs[j]):
                            merged = merged + gs[j]
                            used.add(j)
                            changed = True
                            break
                    else:
                        break
                result.append(merged)
            gs = result
        out.extend(gs)
    out = _merge_spaced_runs(out, items)
    for g in out:
        n = _perp(g[0].dir)
        g.sort(key=lambda ln: (round(_dot(ln.raw_origin, n), 1), _dot(ln.raw_origin, ln.dir)))
    return _reading_order(out)


def _merge_spaced_runs(groups: list[list[TextLine]], items: dict[int, float]) -> list[list[TextLine]]:
    """Single lines with wide but perfectly even spacing (a paragraph set
    with 1.5 or double line spacing) are one paragraph: three or more lines
    of one style, starting at the same edge, equally far apart."""
    singles = [g for g in groups if len(g) == 1 and _is_axis(g[0].dir) and id(g[0]) not in items
               and not _is_bullet(g[0])]
    if len(singles) < 3:
        return groups
    by_key: dict[tuple, list[list[TextLine]]] = {}
    for g in singles:
        ln = g[0]
        by_key.setdefault((ln.dir, _style_key(ln.style), round(ln.extent[0])), []).append(g)
    merged_ids: dict[int, list[TextLine]] = {}
    for gs in by_key.values():
        if len(gs) < 3:
            continue
        n = _perp(gs[0][0].dir)
        gs.sort(key=lambda g: _dot(g[0].raw_origin, n))
        run = [gs[0]]

        def flush(run):
            if len(run) >= 3:
                lines = [g[0] for g in run]
                for g in run:
                    merged_ids[id(g)] = lines

        for g in gs[1:]:
            size = g[0].style.size or 1.0
            gap = _dot(g[0].raw_origin, n) - _dot(run[-1][0].raw_origin, n)
            ok = 1.35 * size < gap <= 2.6 * size and abs(g[0].extent[0] - run[-1][0].extent[0]) <= 1.0
            if ok and len(run) >= 2:
                first_gap = _dot(run[1][0].raw_origin, n) - _dot(run[0][0].raw_origin, n)
                ok = abs(gap - first_gap) <= 0.5
            if ok:
                run.append(g)
            else:
                flush(run)
                run = [g]
        flush(run)
    if not merged_ids:
        return groups
    out, done = [], set()
    for g in groups:
        lines = merged_ids.get(id(g))
        if lines is None:
            out.append(g)
        elif id(lines) not in done:
            done.add(id(lines))
            out.append(list(lines))
    return out


ROW_TOLERANCE = 3.0  # pt; first baselines closer than this count as one row


def _reading_order(groups: list) -> list:
    """Top-to-bottom, and left-to-right for blocks starting on the same row.

    Side-by-side columns in different fonts have first baselines that differ
    by a fraction of a point; a plain (y, x) sort would then put the right
    column first. Rows are anchored on their first block, so the grouping is
    deterministic."""
    by_y = sorted(groups, key=lambda g: (g[0].raw_origin[1], g[0].raw_origin[0]))
    rows: list[list] = []
    for g in by_y:
        if rows and g[0].raw_origin[1] - rows[-1][0][0].raw_origin[1] <= ROW_TOLERANCE:
            rows[-1].append(g)
        else:
            rows.append([g])
    return [g for row in rows for g in sorted(row, key=lambda g: g[0].raw_origin[0])]


def warm_page(doc: pymupdf.Document, page: int) -> None:
    """Precompute what opening an editor on ``page`` needs – its text
    blocks and the proof of its embedded fonts – so the next edit opens at
    once. Meant for idle time right after an edit changed the page."""
    from .fonts import _embedded_candidates

    seen: set[str] = set()
    for blk in text_blocks(doc, page):
        for ln in blk.lines:
            for st in [ln.style] + list(ln.char_styles):
                if st.font not in seen:
                    seen.add(st.font)
                    _embedded_candidates(doc, page, st)
    text_blocks(doc, page, merge=False)


def block_at(doc: pymupdf.Document, page: int, point) -> TextBlock | None:
    """The text block under a display-space point (smallest match wins)."""
    p = pymupdf.Point(point)
    best, best_area = None, None
    for blk in text_blocks(doc, page):
        r = pymupdf.Rect(blk.bbox)
        r.x0 -= 2
        r.y0 -= 2
        r.x1 += 2
        r.y1 += 2
        if p in r:
            area = abs(blk.bbox)
            if best is None or area < best_area:
                best, best_area = blk, area
    return best


# ---------------------------------------------------------------------------
# writing
# ---------------------------------------------------------------------------
def _font_file_index(doc: pymupdf.Document, upto: int) -> dict:
    """``(key, length, sha1) -> font file xref`` of the font programs among
    xrefs ``< upto``; kept on the document object and extended incrementally
    (a full scan of a big PDF costs ~50 ms; undo starts a fresh one)."""
    idx = getattr(doc, "_folio_fontfiles", None)
    if idx is None:
        idx = {"upto": 1, "files": {}}
        try:
            doc._folio_fontfiles = idx
        except Exception:
            pass
    if upto > idx["upto"]:
        for desc, key, ff in _font_files(doc, idx["upto"], upto):
            try:
                data = doc.xref_stream(ff)
            except Exception:
                continue
            if data:
                idx["files"].setdefault((key, len(data), hashlib.sha1(data).hexdigest()), ff)
        idx["upto"] = upto
    return idx["files"]


def _font_files(doc: pymupdf.Document, lo: int, hi: int):
    for x in range(lo, hi):
        try:
            t = doc.xref_get_key(x, "Type")
        except Exception:
            continue
        if t[1] != "/FontDescriptor":
            continue
        for key in ("FontFile2", "FontFile3", "FontFile"):
            kt, kv = doc.xref_get_key(x, key)
            if kt == "xref":
                yield x, key, int(kv.split()[0])


def _dedupe_font_files(doc: pymupdf.Document, first_new_xref: int) -> None:
    """Point freshly written font descriptors at byte-identical font file
    streams that already exist in the PDF (avoids embedding the same font
    program twice, e.g. when writing with the original embedded font)."""
    try:
        total = doc.xref_length()
    except Exception:
        return
    if first_new_xref >= total or total > 150_000:
        return
    new = list(_font_files(doc, first_new_xref, total))
    if not new:
        return
    existing = _font_file_index(doc, first_new_xref)
    for desc, key, ff in new:
        try:
            data = doc.xref_stream(ff) or b""
        except Exception:
            continue
        if not data:
            continue
        sig = (key, len(data), hashlib.sha1(data).hexdigest())
        old = existing.get(sig)
        if old is None or old == ff:
            existing.setdefault(sig, ff)   # later edits reuse this one
            continue
        try:
            doc.xref_set_key(desc, key, f"{old} 0 R")
            # drop the now unreferenced duplicate stream
            doc.update_stream(ff, b"")
            doc.update_object(ff, "null")
        except Exception:
            pass
    idx = getattr(doc, "_folio_fontfiles", None)
    if idx is not None:
        idx["upto"] = max(idx["upto"], total)


def _append(tw: pymupdf.TextWriter, r: TextRun) -> None:
    text = prepare_text(r.font, r.text)
    if has_space(r.font) or " " not in text:
        tw.append(pymupdf.Point(r.origin), text, font=r.font, fontsize=r.size)
        return
    # font without a space glyph (subset): place words individually so no
    # fallback font gets embedded for the spaces
    sp = r.space if r.space else 0.25 * r.size
    x = 0.0
    for piece in re.split(r"( +)", text):
        if not piece:
            continue
        if piece[0] == " ":
            x += sp * len(piece)
            continue
        tw.append(pymupdf.Point(r.origin[0] + x, r.origin[1]), piece, font=r.font, fontsize=r.size)
        x += r.font.text_length(piece, fontsize=r.size)


def _morph_degrees(d) -> float:
    return math.degrees(math.atan2(-d[1], d[0]))


def write_runs(page: pymupdf.Page, runs: list[TextRun]) -> None:
    """Write text runs (raw page coordinates) with TextWriter – in their
    order (so text extraction sees each line in one piece), one writer per
    stretch of horizontal runs of one colour and one per rotated run; then
    their underline / strike-through strokes."""
    all_runs = runs
    runs = [r for r in runs if r.text]
    if not runs:
        _draw_decorations(page, all_runs)
        return
    doc = page.parent
    first_new = doc.xref_length()
    groups: list[tuple[tuple, list[TextRun]]] = []
    for r in runs:
        horizontal = abs(r.dir[0] - 1.0) < 1e-4 and abs(r.dir[1]) < 1e-4
        color = tuple(round(float(c), 4) for c in r.color)
        key = ("h", color) if horizontal else ("r", id(r))
        if groups and groups[-1][0] == key:
            groups[-1][1].append(r)
        else:
            groups.append((key, [r]))
    for key, group in groups:
        color = key[1] if key[0] == "h" else tuple(group[0].color)
        tw = pymupdf.TextWriter(page.rect, color=color)
        for r in group:
            _append(tw, r)
        if key[0] == "h":
            tw.write_text(page, color=color)
        else:
            r = group[0]
            tw.write_text(page, color=color,
                          morph=(pymupdf.Point(r.origin), pymupdf.Matrix(_morph_degrees(r.dir))))
    _fix_space_unicode(doc, first_new, runs)
    _dedupe_font_files(doc, first_new)
    _draw_decorations(page, all_runs)


_TOUNICODE_ENTRY = r"(<{gid}>\s*(?:<{gid}>\s*)?)<00a0>"


def _fix_space_unicode(doc: pymupdf.Document, first_new: int, runs: list[TextRun]) -> None:
    """Fonts whose space and no-break space share one glyph (Arial, Times
    New Roman, Calibri …) get a ToUnicode map from MuPDF that reads that
    glyph back as U+00A0: every space of the new text would copy and search
    as a no-break space, in Folio and in other readers. Map it to U+0020,
    unless the text really contains no-break spaces."""
    if any("\u00a0" in r.text for r in runs):
        return
    gids = set()
    for r in runs:
        try:
            g = r.font.has_glyph(0x20)
            if g and g == r.font.has_glyph(0xA0):
                gids.add(g)
        except Exception:
            continue
    map_space_glyphs(doc, gids, first_new)


def map_space_glyphs(doc: pymupdf.Document, gids, first_xref: int = 1) -> None:
    """Make glyphs ``gids`` read back as U+0020 instead of U+00A0 in the
    ToUnicode maps of the fonts from xref ``first_xref`` on (see
    :func:`_fix_space_unicode`)."""
    if not gids:
        return
    for xref in range(first_xref, doc.xref_length()):
        try:
            kind, val = doc.xref_get_key(xref, "ToUnicode")
            if kind != "xref" or doc.xref_get_key(xref, "Type")[1] != "/Font":
                continue
            tu = int(val.split()[0])
            cmap = doc.xref_stream(tu).decode("latin-1")
        except Exception:
            continue
        fixed = cmap
        for g in gids:
            fixed = re.sub(_TOUNICODE_ENTRY.format(gid=f"{g:04x}"), r"\1<0020>", fixed, flags=re.IGNORECASE)
        if fixed != cmap:
            doc.update_stream(tu, fixed.encode("latin-1"))


def _draw_decorations(page: pymupdf.Page, runs: list[TextRun]) -> None:
    """Underline / strike-through strokes of the runs (raw page space).
    Touching strokes of one line and colour are drawn as one."""
    segs: dict[tuple, list[list[float]]] = {}
    for r in runs:
        if not r.width:
            continue
        n = _perp(r.dir)
        for kind, deco in (("u", r.underline), ("s", r.strike)):
            if not deco:
                continue
            off, w = deco
            sign = 1.0 if kind == "u" else -1.0
            base = (r.origin[0] + n[0] * sign * off, r.origin[1] + n[1] * sign * off)
            key = (kind, round(_dot(base, n), 2), round(w, 3), tuple(round(float(c), 4) for c in r.color),
                   round(r.dir[0], 4), round(r.dir[1], 4))
            a = _dot(base, r.dir)
            segs.setdefault(key, []).append([a, a + r.width, base])
    if not segs:
        return
    shape = page.new_shape()
    for (kind, _b, w, color, dx, dy), parts in segs.items():
        d = (dx, dy)
        parts.sort(key=lambda p: p[0])
        merged = [parts[0]]
        for p in parts[1:]:
            if p[0] <= merged[-1][1] + 0.6 * max(w * 8, 2.0):
                merged[-1][1] = max(merged[-1][1], p[1])
            else:
                merged.append(p)
        for a, b, base in merged:
            t0 = a - _dot(base, d)
            p0 = pymupdf.Point(base[0] + d[0] * t0, base[1] + d[1] * t0)
            p1 = pymupdf.Point(p0.x + d[0] * (b - a), p0.y + d[1] * (b - a))
            shape.draw_line(p0, p1)
            shape.finish(color=color, width=w, closePath=False)
    shape.commit()


# ---------------------------------------------------------------------------
# removing old text
# ---------------------------------------------------------------------------
def _band_rects(line: TextLine) -> list[pymupdf.Rect]:
    rects = []
    d = line.dir
    if abs(d[1]) < 1e-3:  # horizontal (either way)
        for r in line.span_rects:
            cy, h = (r.y0 + r.y1) / 2, r.height
            rects.append(pymupdf.Rect(r.x0, cy - 0.18 * h, r.x1, cy + 0.18 * h))
    elif abs(d[0]) < 1e-3:  # vertical
        for r in line.span_rects:
            cx, w = (r.x0 + r.x1) / 2, r.width
            rects.append(pymupdf.Rect(cx - 0.18 * w, r.y0, cx + 0.18 * w, r.y1))
    else:
        rects = [_center_box(pymupdf.Rect(r)) for r in line.char_rects]
    return [r for r in rects if not r.is_empty]


def _center_box(r: pymupdf.Rect, fx: float = 0.3, fy: float = 0.3) -> pymupdf.Rect:
    c = (r.tl + r.br) / 2
    return pymupdf.Rect(c.x - fx * r.width, c.y - fy * r.height, c.x + fx * r.width, c.y + fy * r.height)


def _stash_redactions(page: pymupdf.Page) -> list[dict]:
    """Remove (and remember) pending user redaction annotations so that
    applying ours does not apply theirs."""
    saved = []
    for annot in list(page.annots(types=(pymupdf.PDF_ANNOT_REDACT,)) or []):
        try:
            saved.append({
                "rect": pymupdf.Rect(annot.rect),
                "vertices": annot.vertices,
                "info": dict(annot.info),
                "fill": (annot.colors or {}).get("fill"),
            })
        except Exception:
            continue
        page.delete_annot(annot)
    return saved


def _restore_redactions(page: pymupdf.Page, saved: list[dict]) -> None:
    for s in saved:
        try:
            quads = None
            v = s.get("vertices")
            if v and len(v) >= 4 and len(v) % 4 == 0:
                quads = [pymupdf.Quad(v[i: i + 4]) for i in range(0, len(v), 4)]
            target = quads[0] if quads and len(quads) == 1 else s["rect"]
            a = page.add_redact_annot(target, fill=s.get("fill") or None, cross_out=False)
            info = {k: val for k, val in s.get("info", {}).items() if k in ("content", "title", "subject")}
            if info:
                a.set_info(**info)
            a.update()
        except Exception:
            continue


def _apply_text_redactions(page: pymupdf.Page, rects: list[pymupdf.Rect]) -> None:
    if not rects:
        return
    for r in rects:
        page.add_redact_annot(r, fill=None, cross_out=False)
    page.apply_redactions(
        images=pymupdf.PDF_REDACT_IMAGE_NONE,
        graphics=pymupdf.PDF_REDACT_LINE_ART_NONE,
        text=pymupdf.PDF_REDACT_TEXT_REMOVE,
    )


def _leftover_chars(page: pymupdf.Page, lines: list[TextLine]) -> list[pymupdf.Rect]:
    """Char boxes of glyphs that survived the band redaction of ``lines``."""
    if not lines:
        return []
    out = []
    boxes = [pymupdf.Rect(ln.raw_bbox) + (-1, -1, 1, 1) for ln in lines]
    clip = pymupdf.Rect(boxes[0])
    for b in boxes[1:]:
        clip |= b
    data = page.get_text("rawdict", flags=_EXTRACT_FLAGS, clip=clip)
    for b in data.get("blocks", ()):
        if b.get("type", 0) != 0:
            continue
        for rl in b.get("lines", ()):
            lb = pymupdf.Rect(rl["bbox"])
            if not any(lb.intersects(bx) for bx in boxes):
                continue
            d = tuple(rl.get("dir", (1.0, 0.0)))
            for span in rl.get("spans", ()):
                for ch in span.get("chars", ()):
                    if ch["c"].isspace():
                        continue
                    o = ch["origin"]
                    for ln in lines:
                        if abs(d[0] - ln.dir[0]) > 1e-2 or abs(d[1] - ln.dir[1]) > 1e-2:
                            continue
                        n = _perp(ln.dir)
                        if abs(_dot(o, n) - _dot(ln.raw_origin, n)) > 0.6:
                            continue
                        s = _dot(o, ln.dir)
                        if ln.extent[0] - 0.5 <= s <= ln.extent[1] + 0.5:
                            out.append(pymupdf.Rect(ch["bbox"]))
                            break
    return out


def _remove_lines(page: pymupdf.Page, lines: list[TextLine]) -> None:
    rects = [r for ln in lines for r in _band_rects(ln)]
    decor = [pymupdf.Rect(r) + (-0.6, -0.6, 0.6, 0.6) for ln in lines for r in ln.decor_rects]
    if not rects and not decor:
        return
    saved = _stash_redactions(page)
    try:
        if rects:
            _apply_text_redactions(page, rects)
            leftovers = _leftover_chars(page, lines)
            if leftovers:
                _apply_text_redactions(page, [_center_box(r, 0.4, 0.35) for r in leftovers])
        if decor:
            # the underline / strike strokes of these lines (only strokes lying
            # completely inside their own small box are removed)
            for r in decor:
                page.add_redact_annot(r, fill=None, cross_out=False)
            page.apply_redactions(images=pymupdf.PDF_REDACT_IMAGE_NONE,
                                  graphics=pymupdf.PDF_REDACT_LINE_ART_REMOVE_IF_COVERED,
                                  text=pymupdf.PDF_REDACT_TEXT_NONE)
    finally:
        _restore_redactions(page, saved)


# ---------------------------------------------------------------------------
# layout planning
# ---------------------------------------------------------------------------
def _merge_style(line_style: SpanStyle, block_style: SpanStyle, new: SpanStyle | None) -> SpanStyle:
    """Apply what the user changed (``new`` vs. ``block_style``) to a line's
    own style, keeping per-line differences otherwise."""
    if new is None:
        return line_style
    values = {}
    for f in fields(SpanStyle):
        nv, bv, lv = getattr(new, f.name), getattr(block_style, f.name), getattr(line_style, f.name)
        if f.name == "size":
            if abs(float(nv) - float(bv)) > 1e-6 and bv:
                values["size"] = round(float(lv) * float(nv) / float(bv), 2)
            else:
                values["size"] = lv
        elif f.name == "color":
            same = tuple(round(float(c), 3) for c in nv) == tuple(round(float(c), 3) for c in bv)
            values["color"] = lv if same else tuple(float(c) for c in nv)
        else:
            values[f.name] = lv if nv == bv else nv
    if new.font != block_style.font:
        # a different family was picked: derive serif/mono from its name
        # unless the caller changed those flags explicitly
        if new.serif == block_style.serif:
            guess = _serif_from_name(new.font)
            if guess is not None:
                values["serif"] = guess
        if new.mono == block_style.mono:
            values["mono"] = _is_mono_name(new.font)
    return SpanStyle(**values)


def _wrap(text: str, font: pymupdf.Font, size: float, width: float, space: float | None = None) -> list[str]:
    tol = 1.5 + 0.01 * width

    def w(s: str) -> float:
        return text_width(font, s, size, space)

    if width <= 0 or w(text) <= width + tol:
        return [text]
    out: list[str] = []
    cur = ""
    for word in text.split(" "):
        cand = word if not cur else cur + " " + word
        if w(cand) <= width + tol:
            cur = cand
            continue
        if cur:
            out.append(cur)
            cur = ""
        # word alone too long -> hard break by characters
        while word and w(word) > width + tol:
            k = len(word)
            while k > 1 and w(word[:k]) > width + tol:
                k -= 1
            out.append(word[:k])
            word = word[k:]
        cur = word
    out.append(cur)
    return out


def _line_height(line: TextLine, size: float) -> float:
    return size * max(1.15, (line.ascender - line.descender))


@dataclass
class _Plan:
    remove: list[TextLine]
    runs: list[TextRun]
    align: str = "left"          # left | center | right (justified counts as left)
    wrap_width: float = 0.0      # max line width before wrapping (pt); 0 = never wraps
    justified: bool = False      # lines that continue the paragraph are stretched
    soft_breaks: tuple = ()      # old line i wraps into line i+1 (same paragraph)
    pitch: float = 0.0           # baseline to baseline distance (pt)
    # -- for moving the text below (reflow) -----------------------------------
    grow: float = 0.0            # how much further down the last line now ends (pt, may be < 0)
    frame: tuple = (0.0, 0.0)    # the block's column along the writing direction (raw)
    bottom: float = 0.0          # old end of the block across the lines (raw, along n)
    substitutions: list = field(default_factory=list)  # (original font, font used instead)
    others: list = field(default_factory=list)  # the page's other text lines
    dir: tuple = (1.0, 0.0)


@dataclass
class _PageContext:
    others: list  # TextLines of the page that are not part of the block
    pmin: float
    pmax: float
    area_lo: float  # text area along the writing direction
    area_hi: float
    text_hi: float  # right-most end of any other text


def _page_context(page: pymupdf.Page, block: TextBlock, d) -> _PageContext:
    pmin, pmax = _proj_range(page.rect * page.derotation_matrix, d)
    own = {(round(ln.raw_origin[0], 2), round(ln.raw_origin[1], 2), ln.text) for ln in block.lines}
    others = []
    try:
        for b in text_blocks(page.parent, block.page, merge=False):
            for ln in b.lines:
                if (round(ln.raw_origin[0], 2), round(ln.raw_origin[1], 2), ln.text) not in own:
                    others.append(ln)
    except Exception:
        pass
    same_axis = [ln for ln in others if abs(ln.dir[0] - d[0]) < 1e-3 and abs(ln.dir[1] - d[1]) < 1e-3]
    lo = min([ln.extent[0] for ln in same_axis + block.lines])
    hi = max([ln.extent[1] for ln in same_axis + block.lines])
    text_hi = max([ln.extent[1] for ln in same_axis], default=-1e9)
    area_lo = max(min(lo, pmax), pmin)
    area_hi = min(max(hi, pmax - (area_lo - pmin)), pmax - 9.0)
    return _PageContext(others, pmin, pmax, area_lo, max(area_hi, hi), text_hi)


def _alignment(ctx: _PageContext, lines: list[TextLine]) -> str:
    if len(lines) >= 2:
        starts = [ln.extent[0] for ln in lines]
        ends = [ln.extent[1] for ln in lines]
        centers = [(a + b) / 2 for a, b in zip(starts, ends)]
        if max(starts) - min(starts) <= 1.0:
            return "left"
        if max(ends) - min(ends) <= 1.0:
            return "right"
        if max(centers) - min(centers) <= 1.0:
            return "center"
        return "left"
    start, end = lines[0].extent
    if start - ctx.area_lo > 36:
        if abs((start + end) / 2 - (ctx.area_lo + ctx.area_hi) / 2) <= 3:
            return "center"
        if abs(end - ctx.text_hi) <= 1.0 or end >= ctx.area_hi - 1.0:
            return "right"  # flush with the text area's right edge
    return "left"


def _available_width(ctx: _PageContext, block: TextBlock, d, align: str, lmin: float, rmax: float,
                     paragraph: bool = False) -> float:
    """How wide a line of ``block`` may become before it is wrapped: a
    wrapped paragraph keeps its column width; single lines and blocks of
    independent lines (addresses, right-aligned blocks) may grow inside the
    page's text area."""
    own = rmax - lmin
    if paragraph:
        return own  # a wrapped paragraph keeps its column width (and reflows)
    if not _is_axis(d):
        return 0.0  # 0 = never wrap
    # a single line (label, heading, table cell) grows sideways like point
    # text, within the page's text area
    limit_lo, limit_hi = min(ctx.area_lo, lmin), max(ctx.area_hi, rmax)
    if align == "right":
        avail = rmax - limit_lo
    elif align == "center":
        c = (lmin + rmax) / 2
        avail = 2 * min(c - limit_lo, limit_hi - c)
    else:
        avail = limit_hi - lmin
    return max(own, avail)


# A "rich" character: (char, SpanStyle, rise). Lines are lists of them.
def _styled_text(block: TextBlock, new_text: str) -> list[tuple[str, SpanStyle, float]]:
    """Give every character of ``new_text`` the style (and baseline rise) of
    the original character it corresponds to (character diff against the
    block's text). Inserted characters continue the style before them."""
    import difflib

    old_chars: list[tuple[str, SpanStyle | None, float]] = []
    for k, ln in enumerate(block.lines):
        if k:
            old_chars.append(("\n", None, 0.0))
        styles = ln.char_styles or [ln.style] * len(ln.text)
        rises = ln.char_rises or [0.0] * len(ln.text)
        old_chars.extend(zip(ln.text, styles, rises))
    old_text = "".join(c[0] for c in old_chars)
    mapped: list[tuple[SpanStyle, float] | None] = [None] * len(new_text)
    # common prefix / suffix are copied directly; only the middle is diffed
    pre = 0
    lim = min(len(old_text), len(new_text))
    while pre < lim and old_text[pre] == new_text[pre]:
        pre += 1
    suf = 0
    while suf < lim - pre and old_text[-1 - suf] == new_text[-1 - suf]:
        suf += 1
    for k in range(pre):
        if old_chars[k][1] is not None:
            mapped[k] = (old_chars[k][1], old_chars[k][2])
    for k in range(1, suf + 1):
        if old_chars[-k][1] is not None:
            mapped[-k] = (old_chars[-k][1], old_chars[-k][2])
    old_mid = old_text[pre: len(old_text) - suf]
    new_mid = new_text[pre: len(new_text) - suf]
    sm = difflib.SequenceMatcher(None, old_mid, new_mid, autojunk=False)
    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        i1, i2, j1, j2 = i1 + pre, i2 + pre, j1 + pre, j2 + pre
        if tag == "equal":
            for k in range(j2 - j1):
                src = old_chars[i1 + k]
                if src[1] is not None:
                    mapped[j1 + k] = (src[1], src[2])
        elif tag == "replace":
            for k in range(j2 - j1):
                src = old_chars[min(i1 + k, i2 - 1)]
                if src[1] is not None:
                    mapped[j1 + k] = (src[1], src[2])
    # fill gaps: previous mapped char, else next, else the block style
    prev = None
    for k in range(len(mapped)):
        if mapped[k] is None:
            mapped[k] = prev
        else:
            prev = mapped[k]
    nxt = None
    for k in range(len(mapped) - 1, -1, -1):
        if mapped[k] is None:
            mapped[k] = nxt
        else:
            nxt = mapped[k]
    out = []
    for ch, m in zip(new_text, mapped):
        st, rise = m if m is not None else (block.style, 0.0)
        out.append((ch, st, rise))
    return out


def _rich_text(rich) -> str:
    return "".join(c[0] for c in rich)


def _rich_rstrip(rich):
    k = len(rich)
    while k and rich[k - 1][0].isspace():
        k -= 1
    return rich[:k]


def _runs(rich):
    """Split rich text into maximal runs of equal (style, rise)."""
    out = []
    for ch, st, rise in rich:
        if out and out[-1][1] is st and out[-1][2] == rise:
            out[-1][0].append(ch)
        else:
            out.append([[ch], st, rise])
    return [("".join(chs), st, rise) for chs, st, rise in out]


FORMAT_KEYS = ("bold", "italic", "underline", "strike", "color", "size", "font")


def _changed_style(st: SpanStyle, changes: dict) -> SpanStyle:
    values = {}
    for k, v in changes.items():
        if k == "color":
            values["color"] = tuple(float(c) for c in v)
        elif k == "size":
            values["size"] = round(float(v), 2)
        elif k in ("bold", "italic", "underline", "strike"):
            values[k] = bool(v)
        elif k == "font" and v and v != st.font:
            values["font"] = str(v)
            guess = _serif_from_name(str(v))
            if guess is not None:
                values["serif"] = guess
            values["mono"] = _is_mono_name(str(v))
    bold, italic = values.get("bold", st.bold), values.get("italic", st.italic)
    if (bold, italic) != (st.bold, st.italic):
        # name the face so the regular embedded font is not taken for it
        values["font"] = styled_font_name(values.get("font", st.font), bold, italic)
    return replace(st, **values) if values else st


def _apply_formats(rich: list, formats) -> list:
    """Apply character formatting ``[(start, end, {attr: value}), ...]``
    (offsets into the new text, ``FORMAT_KEYS``) on top of the inherited
    styles. Equal results share one style object (runs stay together)."""
    if not formats:
        return rich
    rich = list(rich)
    cache: dict[tuple, SpanStyle] = {}
    for start, end, changes in formats:
        changes = {k: v for k, v in dict(changes).items() if k in FORMAT_KEYS}
        if not changes:
            continue
        sig = tuple(sorted((k, tuple(v) if isinstance(v, (list, tuple)) else v) for k, v in changes.items()))
        for k in range(max(0, int(start)), min(int(end), len(rich))):
            ch, st, rise = rich[k]
            if ch == "\n":
                continue
            key = (id(st), sig)
            if key not in cache:
                cache[key] = _changed_style(st, changes)
            rich[k] = (ch, cache[key], rise)
    return rich


def _frame(ctx: _PageContext, block: TextBlock, d, lmin: float, rmax: float) -> tuple[float, float]:
    """The column a block sits in (along ``d``): its own extent widened by
    the text around it that overlaps it (body text sets the margins a centred
    heading is centred between)."""
    n = _perp(d)
    first = block.lines[0]
    here = _dot(first.raw_origin, n)
    reach = 40.0 * max(first.style.size, 1.0)
    lo, hi = lmin, rmax
    for ln in ctx.others:
        if abs(ln.dir[0] - d[0]) > 1e-3 or abs(ln.dir[1] - d[1]) > 1e-3:
            continue
        if abs(_dot(ln.raw_origin, n) - here) > reach:
            continue
        if ln.extent[1] > lmin - 1.0 and ln.extent[0] < rmax + 1.0:
            lo, hi = min(lo, ln.extent[0]), max(hi, ln.extent[1])
    return lo, hi


def _plan(page: pymupdf.Page, block: TextBlock, new_text: str, style: SpanStyle | None, *,
          formats=None, align: str | None = None, line_spacing: float | None = None) -> _Plan:
    """``align`` ("left" | "center" | "right" | "justify") and
    ``line_spacing`` (baseline distance in multiples of the text size)
    re-typeset the whole block."""
    old = block.lines
    n_old = len(old)
    override = style is not None and _style_key(style) != _style_key(block.style)
    new_style = style if override else None
    align_override = align if align in ("left", "center", "right", "justify") else None
    relayout = align_override is not None or (line_spacing is not None and line_spacing > 0)

    text = (new_text or "").replace("\r\n", "\n").replace("\r", "\n").replace("\t", "    ")
    rich_all = _styled_text(block, text)
    if new_style is not None:
        cache: dict[int, SpanStyle] = {}

        def merged(st: SpanStyle) -> SpanStyle:
            if id(st) not in cache:
                cache[id(st)] = _merge_style(st, block.style, new_style)
            return cache[id(st)]

        rich_all = [(ch, merged(st), rise) for ch, st, rise in rich_all]
    rich_all = _apply_formats(rich_all, formats)
    paragraphs = []
    cur = []
    for c in rich_all:
        if c[0] == "\n":
            paragraphs.append(_rich_rstrip(cur))
            cur = []
        else:
            cur.append(c)
    paragraphs.append(_rich_rstrip(cur))
    while paragraphs and not _rich_text(paragraphs[-1]).strip():
        paragraphs.pop()
    if not paragraphs:
        return _Plan(remove=list(old), runs=[])

    def unchanged(i: int, rich) -> bool:
        if override or relayout or i >= n_old or _rich_text(rich) != old[i].text:
            return False
        styles = old[i].char_styles or [old[i].style] * len(old[i].text)
        rises = old[i].char_rises or [0.0] * len(old[i].text)
        return all(c[1] is s and c[2] == r for c, s, r in zip(rich, styles, rises))

    # -- fonts, one per distinct face (covering all text in that face) ------
    by_key: dict[tuple, SpanStyle] = {}
    texts: dict[tuple, list[str]] = {}
    for rich in paragraphs:
        for ch, st, _r in rich:
            k = _font_key(st)
            by_key.setdefault(k, st)
            texts.setdefault(k, []).append(ch)
    fonts = {}
    substitutions = []
    for k, v in texts.items():
        choice = resolve_font(page.parent, block.page, by_key[k], "".join(v).replace("\ufffd", ""))
        fonts[k] = choice.font
        wanted = clean_font_name(by_key[k].font)
        if choice.source != "embedded" and wanted and family_key(wanted) != family_key(choice.name) \
                and (wanted, choice.name) not in substitutions:
            substitutions.append((wanted, choice.name))

    def font_of(st: SpanStyle) -> pymupdf.Font:
        return fonts[_font_key(st)]

    # -- geometry ----------------------------------------------------------
    d = old[0].dir
    n = _perp(d)
    first = old[0]
    if n_old >= 2:
        gaps = sorted(_dot(old[i + 1].raw_origin, n) - _dot(old[i].raw_origin, n) for i in range(n_old - 1))
        spacing = gaps[len(gaps) // 2]
        if spacing <= 0.5 * first.style.size:
            spacing = _line_height(old[-1], old[-1].style.size)
    else:
        spacing = _line_height(first, first.style.size)
    scale = 1.0
    if override and block.style.size and abs(style.size - block.style.size) > 1e-6:
        scale = float(style.size) / float(block.style.size)

    ctx = _page_context(page, block, d)
    align = _alignment(ctx, old) if all(_is_axis(ln.dir) for ln in old) else "left"
    old_align = align
    if line_spacing is not None and line_spacing > 0:
        spacing = float(line_spacing) * float(first.style.size) * scale
        scale_spacing = False
    else:
        scale_spacing = True
    if align == "left" and n_old == 1 and _is_axis(d) and not override:
        # a lone table cell follows its column (right-aligned amounts grow
        # to the left, centred headers both ways)
        align = _Columns(first, ctx.others).column_side(*first.extent) or align
    lmin = min(ln.extent[0] for ln in old)
    rmax = max(ln.extent[1] for ln in old)
    at_right = [abs(ln.extent[1] - rmax) <= 1.0 for ln in old]
    # where continuation lines start: the column edge, the text after a list
    # marker ("1. Første …" wraps under "Første"), or a hanging indent
    cont_x = lmin
    if n_old >= 2:
        rest = sorted(ln.extent[0] for ln in old[1:])
        if rest[-1] - rest[0] <= 1.0:
            cont_x = rest[0]
    elif marker_end(first.text) and len(first.char_pos) == len(first.text):
        cont_x = first.char_pos[marker_end(first.text)][0]

    def stretched(ln: TextLine) -> bool:
        """Is this line wider than its natural width (justified text)?"""
        styles = ln.char_styles or [ln.style] * len(ln.text)
        if " " not in ln.text or any(_font_key(st) not in fonts for st in styles):
            return False
        natural = 0.0
        for txt, st, _rise in _runs(list(zip(ln.text, styles, [0.0] * len(styles)))):
            natural += text_width(fonts[_font_key(st)], txt, st.size)
        width = ln.extent[1] - ln.extent[0]
        return width - natural > max(1.0, 0.01 * width)

    def space_em(i: int) -> float | None:
        return old[min(i, n_old - 1)].space_em

    def measure(rich, em) -> float:
        total = 0.0
        for txt, st, _rise in _runs(rich):
            total += text_width(font_of(st), txt, st.size, em * st.size if em else None)
        return total

    justified_block = (align == "left" and n_old >= 3 and sum(at_right[:-1]) >= 2
                       and any(stretched(ln) for ln in old[:-1]))
    full = [ln.extent[1] >= rmax - max(2.5 * ln.style.size, 0.15 * (rmax - lmin)) for ln in old]
    paragraph = align == "left" and (
        justified_block
        or (n_old >= 3 and sum(full[:-1]) >= 2)
        or (n_old == 2 and stretched(old[0]))
    )
    # a ragged two-line block (typically a list item that wrapped): only a
    # paragraph if real body text of its size fixes the column's right edge
    # (else the widest line would always "fill" its own column)
    two_line_ragged = align == "left" and n_old == 2 and not paragraph and full[0]
    paragraph = paragraph or two_line_ragged
    max_width = _available_width(ctx, block, d, align, lmin, rmax, paragraph)

    def first_word_width(ln: TextLine) -> float:
        """Approximate width of the first word of ``ln`` (pt)."""
        text = ln.text.lstrip()
        word = text.split(" ", 1)[0] if text else ""
        if not word or not ln.text:
            return 0.0
        return (ln.extent[1] - ln.extent[0]) * len(word) / max(len(ln.text), 1)

    def soft_break_after(i: int) -> bool:
        """Did old line ``i`` wrap into line ``i + 1`` (same paragraph)?
        Classic reflow test: only if the next line's first word would *not*
        have fitted on line ``i`` – list items and short lines are hard breaks."""
        if i >= n_old - 1 or not paragraph:
            return False
        if not (full[i] and abs(old[i + 1].extent[0] - cont_x) <= 1.0):
            return False
        space = 0.25 * old[i].style.size
        return old[i].extent[1] + space + first_word_width(old[i + 1]) > column_hi - 0.5

    # the column the block sits in: other lines on the same axis that span
    # the block's left edge end at the column's right edge (one-column page:
    # the margin; two columns: only lines of the same column qualify)
    column_hi = max([ln.extent[1] for ln in ctx.others
                     if abs(ln.dir[0] - d[0]) < 1e-3 and abs(ln.dir[1] - d[1]) < 1e-3
                     and ln.extent[0] - 2.0 <= lmin <= ln.extent[1]] + [rmax])
    if two_line_ragged:
        size = first.style.size or 1.0
        evidence = [ln for ln in ctx.others
                    if abs(ln.dir[0] - d[0]) < 1e-3 and abs(ln.dir[1] - d[1]) < 1e-3
                    and abs(ln.extent[1] - column_hi) <= 2.0 and 0.85 <= (ln.style.size or 0) / size <= 1.18]
        if len(evidence) < 2:
            paragraph = False
            max_width = _available_width(ctx, block, d, align, lmin, rmax, False)
    if paragraph and not any(soft_break_after(i) for i in range(n_old - 1)):
        # "full" lines but no real wrap (a list, an address): independent lines
        # that may grow sideways like point text instead of reflowing
        paragraph = False
        max_width = _available_width(ctx, block, d, align, lmin, rmax, False)
    if not paragraph and n_old == 1 and _is_axis(d) and max_width > 0:
        # a single line (a table cell, a label) grows sideways only until the
        # next text on its row: beyond that it would print over its neighbour
        row = _dot(first.raw_origin, n)
        size = first.style.size or 10.0
        same_row = [ln for ln in ctx.others if abs(ln.dir[0] - d[0]) < 1e-3 and abs(ln.dir[1] - d[1]) < 1e-3
                    and abs(_dot(ln.raw_origin, n) - row) <= 0.5 * size]
        right = min([ln.extent[0] for ln in same_row if ln.extent[0] >= rmax - 0.5] + [math.inf])
        left = max([ln.extent[1] for ln in same_row if ln.extent[1] <= lmin + 0.5] + [-math.inf])
        gap = 0.3 * size
        if align == "right":
            limit = rmax - (left + gap)
        elif align == "center":
            c = (lmin + rmax) / 2
            limit = 2 * min(c - (left + gap), (right - gap) - c)
        else:
            limit = (right - gap) - lmin
        if limit < max_width:
            max_width = max(limit, rmax - lmin)
    frame = (lmin, max(rmax, column_hi)) if paragraph else (lmin, rmax)
    if align_override is not None:
        # an explicit alignment re-typesets the block inside its column
        frame = (lmin, max(rmax, column_hi)) if paragraph else _frame(ctx, block, d, lmin, rmax)
        align = "left" if align_override == "justify" else align_override
        justified_block = align_override == "justify"
        if align_override != old_align or not paragraph:
            max_width = frame[1] - frame[0] if (paragraph or align != "left") else max_width

    # -- lay out lines (wrap + reflow into the paragraph's next lines) -------
    # (rich, paragraph index, paragraph continues, intact = the whole
    # paragraph on one line without text carried over from the line above)
    layout: list[tuple[list, int, bool, bool]] = []
    carry: list = []
    for i, rich in enumerate(paragraphs):
        carried = bool(carry)
        if carry:
            body = rich
            while body and body[0][0] == " ":
                body = body[1:]
            rich = carry + ([(" ", carry[-1][1], carry[-1][2])] + body if body else [])
            carry = []
        if unchanged(i, rich) or not rich:
            pieces = [rich]
        elif align == "left" and max_width > 0 and _is_axis(d):
            # each line may run to the column's right edge from where it starts
            # (an indented first line, a hanging indent, the text after "1.")
            j0 = len(layout)
            right = lmin + max_width

            def width_of(k: int, j0=j0, carried=carried) -> float:
                j = j0 + k
                start = old[j].extent[0] if j < n_old and not (relayout and align_override) else cont_x
                if j < n_old and k == 0 and not carried:
                    start = old[j].extent[0]
                return max(right - start, 4.0 * (first.style.size or 10.0))

            pieces = _wrap_rich(rich, width_of, lambda r, i=i: measure(r, space_em(i)))
        else:
            pieces = _wrap_rich(rich, max_width, lambda r, i=i: measure(r, space_em(i)))
        if len(pieces) > 1 and soft_break_after(i) and i + 1 < len(paragraphs) \
                and _rich_text(paragraphs[i + 1]).strip():
            carry = [c for p in pieces[1:] for c in (p + [(" ", p[-1][1], p[-1][2])])][:-1]
            pieces = pieces[:1]
            layout.append((pieces[0], i, True, False))
            continue
        para_goes_on = (soft_break_after(i) and i + 1 < len(paragraphs)
                        and bool(_rich_text(paragraphs[i + 1]).strip()))
        for k, piece in enumerate(pieces):
            layout.append((piece, i, k < len(pieces) - 1 or para_goes_on, not carried and len(pieces) == 1))

    o0 = first.raw_origin
    remove: list[TextLine] = []
    runs: list[TextRun] = []
    old_last = _dot(old[-1].raw_origin, n)
    last_base = None
    for j, (rich, pi, continues, intact) in enumerate(layout):
        if j < n_old:
            base = old[j].raw_origin
            ld = old[j].dir
            start_j, end_j = old[j].extent
        else:
            last = old[-1].raw_origin
            k = j - n_old + 1
            base = (last[0] + n[0] * spacing * k, last[1] + n[1] * spacing * k)
            if align == "left":  # continuation lines start at the continuation edge
                shift = cont_x - _dot(base, d)
                base = (base[0] + d[0] * shift, base[1] + d[1] * shift)
            ld = old[-1].dir
            start_j, end_j = (_dot(base, d), _dot(base, d) + (rmax - lmin))
            if align == "right":
                start_j, end_j = rmax - (end_j - start_j), rmax
            elif align == "center":
                start_j, end_j = lmin, rmax
        if not scale_spacing:
            # explicit line spacing: every line sits ``spacing`` below the first
            cur = _dot((base[0] - o0[0], base[1] - o0[1]), n)
            off = spacing * j - cur
            base = (base[0] + n[0] * off, base[1] + n[1] * off)
        elif scale != 1.0:
            off = _dot((base[0] - o0[0], base[1] - o0[1]), n) * (scale - 1.0)
            base = (base[0] + n[0] * off, base[1] + n[1] * off)
        if _rich_text(rich).strip():
            last_base = _dot(base, n)

        if j < n_old and pi == j and unchanged(j, rich):
            continue  # unchanged line: leave the original glyphs alone
        if j < n_old:
            remove.append(old[j])
        if not _rich_text(rich).strip():
            continue
        em = space_em(pi)
        width = measure(rich, em)
        if align_override is not None and align == "right":
            target = frame[1] - width
        elif align_override is not None and align == "center":
            target = (frame[0] + frame[1]) / 2 - width / 2
        elif align_override is not None and old_align != "left":
            target = frame[0]
        elif align == "right":
            target = end_j - width
        elif align == "center":
            target = (start_j + end_j) / 2 - width / 2
        else:
            target = _dot(base, ld) if j < n_old else cont_x
        extra = 0.0
        n_spaces = sum(1 for c in rich if c[0] == " ")
        if align_override == "justify":
            if continues and n_spaces and width < frame[1] - target:
                extra = (frame[1] - target - width) / n_spaces
        elif (justified_block and continues and j < n_old - 1 and at_right[j] and n_spaces
                and 0.6 * (rmax - target) <= width < rmax - target):
            extra = (rmax - target - width) / n_spaces
        decor = old[min(j, n_old - 1)].decor
        if (j < n_old and pi == j and intact and not override and not relayout and not extra and _is_axis(ld)
                and len(old[j].char_pos) == len(old[j].text)
                and not (justified_block and at_right[j] and j < n_old - 1)):
            # a line edited in place: keep the old geometry (unchanged words,
            # gap widths, table columns) instead of re-typesetting it
            pieces = _place_line(rich, old[j], em, font_of, align=align,
                                 columns=_Columns(old[j], ctx.others + old))
            ends = [x + measure(piece, em) for piece, x in pieces]
            if not ends or max(ends) <= max(rmax, old[j].extent[1]) + 0.5:
                b0 = _dot(base, ld)
                for piece, x in pieces:
                    off = x - b0
                    runs.extend(_emit_line(piece, (base[0] + ld[0] * off, base[1] + ld[1] * off), ld, em, 0.0,
                                           font_of, decor))
                continue
            # the old (stretched) gaps push the text past the column: re-typeset
            # it; a full-width line of a justified paragraph stays justified
            if justified_block and at_right[j] and n_spaces and width < rmax - target:
                extra = (rmax - target - width) / n_spaces
        shift = target - _dot(base, ld)
        origin = (base[0] + ld[0] * shift, base[1] + ld[1] * shift)
        runs.extend(_emit_line(rich, origin, ld, em, extra, font_of, decor))
    for j in range(len(layout), n_old):
        remove.append(old[j])
    soft = tuple(i for i in range(n_old) if soft_break_after(i))
    if last_base is None:  # everything deleted: the block is gone
        last_base = _dot(old[0].raw_origin, n) - spacing
    size_last = old[-1].style.size or first.style.size or 10.0
    bottom = old_last + max(-old[-1].descender, 0.25) * size_last
    return _Plan(remove=remove, runs=runs, align=align, wrap_width=max_width,
                 justified=bool(justified_block), soft_breaks=soft, pitch=float(spacing),
                 grow=float(last_base - old_last), frame=frame, bottom=bottom,
                 substitutions=substitutions, others=ctx.others, dir=d)


def _wrap_rich(rich, width, measure) -> list[list]:
    """Greedy word wrap of rich text to ``width`` (0 = never wrap) – a
    number, or a function giving the width of the n-th line."""
    width_of = width if callable(width) else (lambda _k, w=width: w)
    first = width_of(0)
    if first <= 0 or measure(rich) <= first + 1.5 + 0.01 * first:
        return [rich]
    tokens: list[tuple[list, list]] = []  # (separator, word)
    sep: list = []
    word: list = []
    for c in rich:
        if c[0] == " ":
            if word:
                tokens.append((sep, word))
                sep, word = [], []
            sep.append(c)
        else:
            word.append(c)
    if word or sep:
        tokens.append((sep, word))
    lines: list[list] = []
    cur: list = []

    def limit() -> float:
        w = width_of(len(lines))
        return w + 1.5 + 0.01 * w

    for sep, word in tokens:
        cand = cur + sep + word if (cur or not lines) else word
        if measure(cand) <= limit():
            cur = cand
            continue
        if cur:
            lines.append(cur)
            cur = []
        while word and measure(word) > limit():
            k = len(word)
            while k > 1 and measure(word[:k]) > limit():
                k -= 1
            lines.append(word[:k])
            word = word[k:]
        cur = word
    if cur:
        lines.append(cur)
    return lines or [rich]


def _cell_edges(line: TextLine) -> list[tuple[float, float]]:
    """(start, end) along the writing direction of the "cells" of a line:
    runs of glyphs separated by gaps wider than a usual word space."""
    if len(line.char_pos) != len(line.text):
        return [line.extent]
    vis = [i for i, c in enumerate(line.text) if not c.isspace()]
    if not vis:
        return []
    out = []
    start, prev = line.char_pos[vis[0]][0], vis[0]
    for i in vis[1:]:
        if line.char_pos[i][0] - line.char_pos[prev][1] > 0.3 * line.style.size:
            out.append((start, line.char_pos[prev][1]))
            start = line.char_pos[i][0]
        prev = i
    out.append((start, line.char_pos[prev][1]))
    return out


class _Columns:
    """Table columns around a line, seen through the cells of nearby lines
    (rows): which edge of a cell lines up with the cells above and below."""

    TOL = 0.75  # pt

    def __init__(self, line: TextLine, neighbours: list[TextLine]) -> None:
        d, n = line.dir, _perp(line.dir)
        here = _dot(line.raw_origin, n)
        reach = 30.0 * max(line.style.size, 1.0)
        self.edges = [e for ln in neighbours
                      if ln is not line and abs(ln.dir[0] - d[0]) < 1e-3 and abs(ln.dir[1] - d[1]) < 1e-3
                      and abs(_dot(ln.raw_origin, n) - here) <= reach
                      for e in _cell_edges(ln)]

    def votes(self, start: float, end: float) -> dict[str, int]:
        tol = self.TOL
        votes = {"left": 0, "right": 0, "center": 0}
        for s0, e0 in self.edges:
            same_l, same_r = abs(s0 - start) <= tol, abs(e0 - end) <= tol
            if same_l and same_r:
                continue  # an identical cell says nothing about alignment
            if same_l:
                votes["left"] += 1
            elif same_r:
                votes["right"] += 1
            elif abs((s0 + e0) / 2 - (start + end) / 2) <= tol:
                votes["center"] += 1
        return votes

    def evidence(self, start: float, end: float) -> int:
        """How many cells of other rows share an edge or the centre."""
        tol = self.TOL
        return sum(1 for s0, e0 in self.edges
                   if abs(s0 - start) <= tol or abs(e0 - end) <= tol
                   or abs((s0 + e0) / 2 - (start + end) / 2) <= tol)

    def column_side(self, start: float, end: float) -> str | None:
        """"right" / "center" when at least two other rows say the cell's
        column is aligned that way, else None."""
        votes = self.votes(start, end)
        best = max(("right", "center"), key=votes.get)
        return best if votes[best] >= 2 and votes[best] > votes["left"] else None

    def side(self, start: float, end: float) -> str | None:
        """"left" | "right" | "center": how the cell sits in its column
        (None: no other row shares an edge with it)."""
        votes = self.votes(start, end)
        if not any(votes.values()):
            return None
        best = max(votes, key=votes.get)
        return best if votes[best] > votes["left"] else "left"


def _place_line(rich, old: TextLine, em, font_of, *, align: str = "left",
                columns: _Columns | None = None) -> list[tuple[list, float]]:
    """Lay out a changed line that replaces ``old`` in place, keeping as much
    of the old geometry as possible. Returns pieces of rich text with their
    start along the line direction (raw page space).

    * a left-aligned line starts where it did and words starting in its
      unchanged beginning stay exactly where they were – no drift from
      kerning or character spacing; right-aligned / centred lines keep their
      end / centre;
    * gaps that survive from the old line keep their old width;
    * a gap clearly wider than a word space (table cells, tab stops), or a
      slightly wide one followed by text that lines up with cells of nearby
      rows (``columns``), starts a new *cell* that returns to its old column
      – keeping its left edge, or its right edge / centre when the column is
      aligned that way – unless the text before it now reaches that far
      (not in centred lines, which are re-centred as a whole);
    * the unchanged end of a cell keeps its internal geometry.

    New words and gaps get their natural width. Old positions are only
    trusted when the font we write with has the old glyph widths (otherwise
    pinned words could overlap)."""
    import difflib

    text = _rich_text(rich)
    old_text, pos = old.text, old.char_pos
    n_new, n_old = len(text), len(old_text)
    to_old: list[int | None] = [None] * n_new
    for tag, i1, _i2, j1, j2 in difflib.SequenceMatcher(None, old_text, text, autojunk=False).get_opcodes():
        if tag == "equal":
            for k in range(j2 - j1):
                to_old[j1 + k] = i1 + k
    lim = min(n_new, n_old)
    prefix = 0
    while prefix < lim and text[prefix] == old_text[prefix]:
        prefix += 1
    suffix = 0
    while suffix < lim - prefix and text[-1 - suffix] == old_text[-1 - suffix]:
        suffix += 1
    suffix_from, to_old_suffix = n_new - suffix, n_old - n_new

    def natural(a: int, b: int) -> float:
        return sum(text_width(font_of(st), t, st.size, em * st.size if em else None)
                   for t, st, _r in _runs(rich[a:b]))

    def space_width(st: SpanStyle) -> float:
        try:
            return text_width(font_of(st), " ", st.size, em * st.size if em else None)
        except KeyError:  # a style that no longer occurs in the text
            return (old.space_em or 0.28) * st.size

    tokens = [(m.start(), m.end()) for m in re.finditer(r"[^ ]+| +", text)]

    # do the old glyph widths match the fonts we write with?
    nat_sum = old_sum = 0.0
    for a, b in tokens:
        ia = to_old[a]
        if text[a] == " " or ia is None or any(to_old[k] != ia + k - a for k in range(a, b)):
            continue
        nat_sum += natural(a, b)
        old_sum += pos[ia + b - a - 1][1] - pos[ia][0]
    faithful = old_sum > 0 and abs(nat_sum - old_sum) <= max(0.6, 0.02 * old_sum)

    # the old line's gaps: (first, last char index, width, starts a cell)
    old_gaps: list[tuple[int, int, float, bool]] = []
    i = 0
    while i < n_old:
        if not old_text[i].isspace():
            i += 1
            continue
        lo = i
        while i < n_old and old_text[i].isspace():
            i += 1
        hi = i - 1
        if lo == 0 or hi == n_old - 1:
            continue
        st = old.char_styles[lo] if len(old.char_styles) == n_old else old.style
        width = pos[hi + 1][0] - pos[lo - 1][1]
        normal = space_width(st) * (hi - lo + 1)
        old_gaps.append([lo, hi, width, width > normal + max(0.25 * st.size, 0.6 * normal)])
    if columns is not None:
        # a slightly wide gap is a cell boundary too when the text after it
        # lines up with cells of other rows (compact tables)
        for g in old_gaps:
            if not g[3] and g[2] > space_width(old.style) * (g[1] - g[0] + 1) + 0.6:
                start = pos[g[1] + 1][0]
                stop = next((h[0] for h in old_gaps if h[0] > g[1] and h[3]), n_old)
                last = max(k for k in range(g[1] + 1, stop) if not old_text[k].isspace())
                g[3] = columns.evidence(start, pos[last][1]) >= 2

    def old_gap(ga: int, gb: int) -> list | None:
        """The old gap that the new gap ``text[ga:gb]`` continues."""
        for k in range(ga, gb):
            i = to_old[k]
            if i is not None and old_text[i].isspace():
                return next((g for g in old_gaps if g[0] <= i <= g[1]), None)
        return None

    def old_cell_end(first: int) -> float:
        """End of the old cell that starts at old char ``first``."""
        stop = next((g[0] for g in old_gaps if g[3] and g[0] > first), n_old)
        last = max(k for k in range(first, stop) if not old_text[k].isspace())
        return pos[last][1]

    # split the new line into cells at gaps that continue a wide old gap
    cells: list[tuple[tuple | None, list[tuple[int, int]]]] = [(None, [])]
    for a, b in tokens:
        if text[a] == " " and align != "center" and any(text[k] != " " for k in range(a)):
            g = old_gap(a, b)
            if g is not None and g[3] and b < n_new:
                cells.append((((a, b), g), []))
                continue
        cells[-1][1].append((a, b))
    if not cells[0][1]:
        cells.pop(0)

    def lay(cell: list[tuple[int, int]], x0: float, cursor: float | None, first_cell: bool,
            pin_prefix: bool) -> tuple[list[list], float | None]:
        """Place the words of one cell, the first one at ``x0``."""
        pieces: list[list] = []
        gap: tuple[int, int] | None = None
        delta: float | None = None  # shift of the unchanged end of the cell
        for a, b in cell:
            if text[a] == " ":
                gap = (a, b)
                continue
            nat_gap = natural(*gap) if gap else 0.0
            if not pieces:
                flow = x0 + (nat_gap if first_cell else 0.0)
            else:
                flow = cursor + nat_gap
            x = flow
            prev = old_gap(*gap) if gap is not None and pieces else None
            if pin_prefix and faithful and a < prefix:
                x = pos[a][0]                  # unchanged beginning: exact old position
            elif faithful and a >= suffix_from and delta is not None:
                x = pos[a + to_old_suffix][0] + delta
            elif prev is not None:
                x = cursor + prev[2]           # keep the old gap width
            if cursor is not None:
                x = max(x, cursor + 0.5 * (nat_gap or space_width(rich[a][1])))  # never touch
            if faithful and a >= suffix_from:
                delta = x - pos[a + to_old_suffix][0]
            if pieces and gap is not None and abs(x - flow) < 0.01:
                pieces[-1][1] = b              # natural flow: same piece
            else:
                if pieces and gap is not None:
                    pieces[-1][1] = gap[1]     # write the space too: the words stay separate
                pieces.append([a, b, x])
            cursor = x + natural(a, b)
            ia = to_old[a]
            if (faithful and ia is not None and all(to_old[k] == ia + k - a for k in range(a + 1, b))
                    and not any(pos[i][1] - pos[i][0] < 0.05 * rich[a][1].size for i in range(ia, ia + b - a))):
                # a word of the old line (not drawn with ligatures, whose parts
                # have no width of their own): keep its glyph positions (kerning)
                start, sx = a, x
                for k in range(a + 1, b):
                    old_x = x + pos[ia + k - a][0] - pos[ia][0]
                    diff = abs(old_x - (sx + natural(start, k)))
                    if 0.02 < diff <= 0.15 * rich[k][1].size:
                        pieces[-1][1] = k
                        pieces.append([k, b, old_x])
                        start, sx = k, old_x
                cursor = sx + natural(start, b)
            gap = None
        return pieces, cursor

    out: list[list] = []
    cursor: float | None = None
    multi = len(cells) > 1
    for k, (lead, cell) in enumerate(cells):
        if lead is None:
            first_old, start_x = 0, old.extent[0]
        else:
            (_ga, _gb), g = lead
            first_old, start_x = g[1] + 1, pos[g[1] + 1][0]
        end_x = old_cell_end(first_old) if multi else old.extent[1]
        if multi:
            side = columns.side(start_x, end_x) if columns is not None else None
            if side is None:  # no rows to compare with: follow the line
                side = "right" if align == "right" and k == len(cells) - 1 else "left"
        else:
            side = align
        if side == "left":
            x0 = start_x
        else:
            _dry, end = lay(cell, 0.0, None, lead is None, False)
            width = end or 0.0  # from x0 (incl. leading spaces) to the last glyph
            x0 = end_x - width if side == "right" else (start_x + end_x) / 2 - width / 2
        if lead is not None and cursor is not None:
            x0 = max(x0, cursor + natural(*lead[0]))  # the text before reaches into the column
        pieces, end = lay(cell, x0, cursor, lead is None, side == "left")
        out.extend(pieces)
        if end is not None:
            cursor = end
    return [(rich[a:b], x) for a, b, x in out]


def _decor_geometry(st: SpanStyle, decor: dict | None) -> tuple[tuple | None, tuple | None]:
    """(underline, strike) as (offset from the baseline, thickness): the
    line's own measured strokes when it had some, else typographic defaults."""
    decor = decor or {}
    size = st.size or 10.0
    u = s = None
    if st.underline:
        off, w = decor.get("underline", (0.12 * size, max(0.4, 0.055 * size)))
        u = (off, w)
    if st.strike:
        off, w = decor.get("strike", (0.28 * size, max(0.4, 0.055 * size)))
        s = (off, w)
    return u, s


def _emit_line(rich, origin, d, em, extra: float, font_of, decor: dict | None = None) -> list[TextRun]:
    """Turn one laid-out rich line into positioned TextRuns. Runs of one
    style are written in one piece unless spaces must be stretched
    (justification) or the font has no space glyph. Underlined / struck
    runs carry the stroke to draw (``decor``: the old line's strokes)."""
    n = _perp(d)
    out: list[TextRun] = []
    x = 0.0
    for txt, st, rise in _runs(rich):
        font = font_of(st)
        space = em * st.size if em else None
        pieces = re.split(r"( +)", txt) if (extra or not has_space(font)) else [txt]
        under, strike = _decor_geometry(st, decor)
        for piece in pieces:
            if not piece:
                continue
            w = text_width(font, piece, st.size, space)
            if piece[0] == " " and not piece.strip():
                gap = w + extra * len(piece)
                if extra and has_space(font):
                    # the space glyph itself, centred in the stretched gap:
                    # text extraction keeps the line in one piece
                    mid = x + extra * len(piece) / 2
                    out.append(TextRun(text=piece, font=font, size=st.size, color=tuple(st.color),
                                       origin=(origin[0] + d[0] * mid, origin[1] + d[1] * mid), dir=d,
                                       space=space))
                if (under or strike) and out:
                    # underline the space between two underlined words too
                    out.append(TextRun(text="", font=font, size=st.size, color=tuple(st.color),
                                       origin=(origin[0] + d[0] * x, origin[1] + d[1] * x), dir=d,
                                       width=gap, underline=under, strike=strike))
                x += gap
                continue
            if piece.strip():
                pos = (origin[0] + d[0] * x + n[0] * rise, origin[1] + d[1] * x + n[1] * rise)
                adv = w + (extra * piece.count(" ") if extra else 0.0)
                out.append(TextRun(text=piece, font=font, size=st.size, color=tuple(st.color),
                                   origin=pos, dir=d, space=space, width=adv,
                                   underline=under, strike=strike))
            x += w + (extra * piece.count(" ") if extra else 0.0)
    return _trim_decor(out)


def _trim_decor(runs: list[TextRun]) -> list[TextRun]:
    """Space runs only carry strokes between two decorated words: drop the
    ones at the end of a line or next to undecorated text."""
    out = []
    for i, r in enumerate(runs):
        if r.text:
            out.append(r)
            continue
        prev = next((x for x in reversed(runs[:i]) if x.text.strip()), None)
        nxt = next((x for x in runs[i + 1:] if x.text.strip()), None)
        u = r.underline if prev is not None and nxt is not None and prev.underline and nxt.underline else None
        st = r.strike if prev is not None and nxt is not None and prev.strike and nxt.strike else None
        if u or st:
            out.append(replace(r, underline=u, strike=st))
    return out


# ---------------------------------------------------------------------------
# public API
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class LayoutInfo:
    """How :func:`replace_block_text` will lay out new text for a block:
    UIs use it so their editor wraps and aligns exactly like the result."""

    align: str          # "left" | "center" | "right"
    wrap_width: float   # pt along the text direction; 0 = lines never wrap
    justified: bool = False          # paragraph lines are stretched to the column
    soft_breaks: tuple = ()          # indices i: line i flows on into line i+1
    line_spacing: float = 0.0        # baseline distance / text size
    frame: tuple = ()                # (start, end) of the column in display space along the text


def layout_info(doc: pymupdf.Document, block: TextBlock, *, align: str | None = None,
                line_spacing: float | None = None) -> LayoutInfo:
    """Alignment, wrap width, justification, soft line breaks, line spacing
    and column that editing ``block`` will use (with an explicit ``align`` /
    ``line_spacing``, the ones that setting gives)."""
    page = doc[block.page]
    try:
        plan = _plan(page, block, block.text, None, align=align, line_spacing=line_spacing)
    except Exception:  # never let a UI hint break editing
        return LayoutInfo("left", float(block.bbox.width))
    size = block.lines[0].style.size or 1.0
    first = block.lines[0]
    d = first.dir
    o = first.raw_origin
    s0 = _dot(o, d)
    ends = []
    for t in plan.frame:
        p = pymupdf.Point(o[0] + d[0] * (t - s0), o[1] + d[1] * (t - s0)) * page.rotation_matrix
        dd = first.display_dir
        ends.append(p.x * dd[0] + p.y * dd[1])
    frame = (min(ends), max(ends)) if len(ends) == 2 else ()
    return LayoutInfo(plan.align, float(plan.wrap_width), plan.justified, plan.soft_breaks,
                      round(plan.pitch / size, 3) if len(block.lines) > 1 or line_spacing else 0.0, frame)


@dataclass
class EditResult:
    """What an edit did besides changing the text."""

    moved: int = 0              # text lines below that moved along
    overflow: float = 0.0       # pt the edited text overlaps what is below (0: none)
    substitutions: list = field(default_factory=list)  # (font, font used instead): glyphs missing


# ---------------------------------------------------------------------------
# moving the text below (reflow)
# ---------------------------------------------------------------------------
def _same_dir(a, b) -> bool:
    return abs(a[0] - b[0]) < 1e-3 and abs(a[1] - b[1]) < 1e-3


def _obstacles(page: pymupdf.Page, decor: list) -> list[pymupdf.Rect]:
    """Raw boxes of images and vector graphics (not underline strokes)."""
    out = []
    try:
        for info in page.get_image_info():
            out.append(pymupdf.Rect(info["bbox"]) * page.derotation_matrix)
    except Exception:
        pass
    known = [pymupdf.Rect(r) for r in decor]
    try:
        for p in page.get_drawings():
            r = pymupdf.Rect(p["rect"])
            if r.is_infinite or (r.width < 0.01 and r.height < 0.01):
                continue
            if any(abs(r.x0 - k.x0) < 0.7 and abs(r.x1 - k.x1) < 0.7 and abs(r.y0 - k.y0) < 1.5 for k in known):
                continue
            out.append(r)
    except Exception:
        pass
    return out


def _reflow(page: pymupdf.Page, block: TextBlock, plan: _Plan) -> tuple[list[TextLine], float, float]:
    """Text below the block in its column that should move by ``plan.grow``:
    ``(lines to move, overflow, room)``. ``overflow`` > 0 means the edited
    text runs into something that cannot move (an image or drawing in the
    way, text beside it, the bottom of the page); then nothing moves.
    ``room`` is how far the text may grow before that happens."""
    d = plan.dir
    if not _is_axis(d):
        return [], 0.0, 0.0
    n = _perp(d)
    grow = plan.grow
    lo, hi = plan.frame
    size = block.lines[-1].style.size or 10.0
    pitch = plan.pitch or 1.2 * size
    old_last = _dot(block.lines[-1].raw_origin, n)
    below = [ln for ln in plan.others if _same_dir(ln.dir, d) and _dot(ln.raw_origin, n) > old_last + 0.3 * size]
    below.sort(key=lambda ln: _dot(ln.raw_origin, n))
    flow: list[TextLine] = []
    prev = old_last
    stop_at = None      # baseline of the first text that does not flow
    for ln in below:
        b = _dot(ln.raw_origin, n)
        inside = ln.extent[1] > lo + 1.0 and ln.extent[0] < hi - 1.0
        if not inside:
            continue
        if b - prev > max(3.0 * pitch, 4.0 * (ln.style.size or size)):
            stop_at = b - (ln.style.size or size)
            break       # a large gap: a footer or a separate area
        flow.append(ln)
        prev = max(prev, b)
    # page limits along n (raw space)
    pmin, pmax = _proj_range(page.rect * page.derotation_matrix, n)
    margin = max(18.0, min(54.0, 0.06 * (pmax - pmin)))
    limit = pmax - margin
    if stop_at is not None:
        limit = min(limit, stop_at - 0.5 * pitch)
    lowest_text = max([_dot(ln.raw_origin, n) for ln in plan.others if _same_dir(ln.dir, d)] + [old_last])
    limit = max(limit, min(pmax - 6.0, lowest_text))  # never stricter than where text already is
    end = max([_dot(ln.raw_origin, n) - ln.descender * (ln.style.size or size) for ln in flow]
              + [plan.bottom])
    room = limit - end
    # what sits beside the flowing text (another column, a table row) or in
    # the way (images, drawings) cannot follow
    band_top = plan.bottom - 0.2 * size
    band_bot = end + max(grow, 0.0)
    flow_ids = {id(ln) for ln in flow}
    for ln in below:
        if id(ln) in flow_ids:
            continue
        b = _dot(ln.raw_origin, n)
        if band_top <= b <= band_bot and any(abs(_dot(f.raw_origin, n) - b) < 0.5 * size for f in flow):
            return [], max(grow, 0.0), 0.0    # side by side with flowing text: a table / columns
    for r in _obstacles(page, [x for ln in flow + block.lines for x in ln.decor_rects]):
        a0, a1 = _proj_range(r, d)
        c0, c1 = _proj_range(r, n)
        if a1 <= lo + 1.0 or a0 >= hi - 1.0:
            continue
        if c1 > band_top and c0 < band_bot:
            room = min(room, c0 - (end if flow else plan.bottom))
            if grow > 0 or flow:
                return [], max(grow - max(room, 0.0), 0.0) if grow > 0 else 0.0, max(room, 0.0)
    if grow > room + 0.5:
        return [], grow - max(room, 0.0), max(room, 0.0)
    return flow, 0.0, room


def _moved_runs(page: pymupdf.Page, block_page: int, lines: list[TextLine], shift: float) -> list[TextRun]:
    """The lines re-typeset ``shift`` pt further along their line normal,
    glyph for glyph where they were (same fonts, kerning and gaps)."""
    runs: list[TextRun] = []
    by_key: dict[tuple, SpanStyle] = {}
    texts: dict[tuple, list[str]] = {}
    for ln in lines:
        for ch, st in zip(ln.text, ln.char_styles or [ln.style] * len(ln.text)):
            k = _font_key(st)
            by_key.setdefault(k, st)
            texts.setdefault(k, []).append(ch)
    fonts = {k: resolve_font(page.parent, block_page, by_key[k], "".join(v)).font for k, v in texts.items()}

    def font_of(st: SpanStyle) -> pymupdf.Font:
        return fonts[_font_key(st)]

    for ln in lines:
        styles = ln.char_styles or [ln.style] * len(ln.text)
        rises = ln.char_rises or [0.0] * len(ln.text)
        rich = list(zip(ln.text, styles, rises))
        n = _perp(ln.dir)
        base = (ln.raw_origin[0] + n[0] * shift, ln.raw_origin[1] + n[1] * shift)
        b0 = _dot(ln.raw_origin, ln.dir)
        if len(ln.char_pos) == len(ln.text):
            pieces = _place_line(rich, ln, ln.space_em, font_of)
        else:
            pieces = [(rich, b0)]
        for piece, x in pieces:
            off = x - b0
            runs.extend(_emit_line(piece, (base[0] + ln.dir[0] * off, base[1] + ln.dir[1] * off), ln.dir,
                                   ln.space_em, 0.0, font_of, ln.decor))
    return runs


def fit_scale(doc: pymupdf.Document, block: TextBlock, new_text: str, style: SpanStyle | None = None, *,
              formats=None, align: str | None = None, line_spacing: float | None = None) -> float:
    """Text size factor (0.5 … 1) at which the new text fits the room the
    block has (``1.0`` when it already fits) – "formindsk så den passer"."""
    page = doc[block.page]
    base_style = style or block.style

    def overflow(scale: float) -> float:
        st = replace(base_style, size=round(base_style.size * scale, 2))
        plan = _plan(page, block, new_text, st, formats=formats, align=align, line_spacing=line_spacing)
        _flow, over, room = _reflow(page, block, plan)
        return over if plan.grow > 0 else 0.0

    if overflow(1.0) <= 0.0:
        return 1.0
    lo, hi = 0.5, 1.0
    if overflow(lo) > 0.0:
        return lo
    for _ in range(7):
        mid = (lo + hi) / 2
        if overflow(mid) > 0.0:
            hi = mid
        else:
            lo = mid
    return round(lo, 3)


def replace_block_text(pdf, block: TextBlock, new_text: str, style: SpanStyle | None = None, *,
                       formats=None, align: str | None = None, line_spacing: float | None = None,
                       reflow: bool = True) -> EditResult:
    """Replace the text of ``block`` with ``new_text`` (lines separated by
    ``\\n``) keeping font, size, colour, position and alignment. Only text is
    removed – images and vector graphics underneath survive. Empty
    ``new_text`` deletes the block's text.

    * ``style`` applies the changed attributes (vs. ``block.style``) to every line;
    * ``formats`` – ``[(start, end, {"bold": True, "underline": True, "color":
      (r, g, b), "size": 12, "font": "Arial", ...}), ...]`` – formats parts of
      the new text (offsets into ``new_text``);
    * ``align`` ("left" | "center" | "right" | "justify") and ``line_spacing``
      (baseline distance in multiples of the text size) re-typeset the block;
    * ``reflow``: text below in the same column moves when the block grows or
      shrinks (see :class:`EditResult` for what happened)."""
    page = pdf.doc[block.page]
    plan = _plan(page, block, new_text, style, formats=formats, align=align, line_spacing=line_spacing)
    result = EditResult(substitutions=list(plan.substitutions))
    if not plan.remove and not plan.runs:
        return result  # nothing changed -> no undo step
    moved: list[TextLine] = []
    if reflow and abs(plan.grow) > 0.5:
        moved, over, _room = _reflow(page, block, plan)
        result.overflow = over
    with pdf.edit(_label("core.undo.edit_text", "Redigér tekst"), CONTENT, (block.page,)):
        page = pdf.doc[block.page]
        runs = list(plan.runs)
        if moved:
            runs.extend(_moved_runs(page, block.page, moved, plan.grow))
        _remove_lines(page, list(plan.remove) + moved)
        write_runs(page, runs)
    result.moved = len(moved)
    return result
