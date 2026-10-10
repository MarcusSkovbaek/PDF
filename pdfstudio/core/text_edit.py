"""Edit existing PDF text so the result looks like it was always there.

Pipeline of :func:`replace_block_text`:

1. Plan: split the new text into lines, map them onto the block's original
   lines (same index -> same baseline), wrap lines that got too wide, keep the
   block's alignment and line spacing, and resolve a font per line style
   (:func:`pdfstudio.core.fonts.resolve_font`). Lines whose text and style are
   unchanged are **not touched at all** (pixel identical, original kerning,
   mixed styles inside the line survive).
2. Remove the old glyphs of changed lines with redaction annotations that only
   cover a thin band through the middle of each span, applied with
   images/line-art untouched and no fill – backgrounds, images and vector art
   stay.
3. Write the new lines with :class:`pymupdf.TextWriter` at the original
   baselines, size and colour, in the line's original direction.
4. Deduplicate font programs: when the written font is byte-identical to a
   font file already in the PDF (e.g. the original embedded font), the new
   font descriptor is pointed at the existing stream.

All public geometry is in display space (page rotation applied). Internally
PyMuPDF text extraction, redactions and TextWriter positions all use the
unrotated page space ("raw"), which is what each line keeps privately.
"""

from __future__ import annotations

import hashlib
import math
import re
from dataclasses import dataclass, field, fields

import pymupdf

from .document import CONTENT
from .fonts import SpanStyle, has_space, prepare_text, resolve_font, style_from_span, text_width

__all__ = [
    "TextLine",
    "TextBlock",
    "text_blocks",
    "block_at",
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


# ---------------------------------------------------------------------------
# small geometry helpers
# ---------------------------------------------------------------------------
def _dot(p, d) -> float:
    return p[0] * d[0] + p[1] * d[1]


def _perp(d) -> tuple[float, float]:
    # next line direction for text running along d (y axis points down)
    return (-d[1], d[0])


def _proj_range(rect: pymupdf.Rect, d) -> tuple[float, float]:
    vals = [_dot((x, y), d) for x in (rect.x0, rect.x1) for y in (rect.y0, rect.y1)]
    return min(vals), max(vals)


def _is_axis(d) -> bool:
    return abs(d[0]) < 1e-3 or abs(d[1]) < 1e-3


def _style_key(style: SpanStyle) -> tuple:
    return (style.font, round(style.size, 2), tuple(round(c, 3) for c in style.color),
            style.bold, style.italic, style.serif, style.mono)


# ---------------------------------------------------------------------------
# extraction
# ---------------------------------------------------------------------------
def _build_line(raw_line: dict, rm: pymupdf.Matrix) -> TextLine | None:
    d = tuple(raw_line.get("dir", (1.0, 0.0)))
    chars = []  # (c, origin, bbox, span)
    for span in raw_line.get("spans", ()):
        for ch in span.get("chars", ()):
            chars.append((ch["c"], ch["origin"], pymupdf.Rect(ch["bbox"]), span))
    if not chars:
        return None
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
    style = style_from_span(dom.get("font", ""), dom.get("size", 11), dom.get("color", 0), dom.get("flags", 0))

    raw_bbox = pymupdf.Rect(used[0][2])
    for c in used[1:]:
        raw_bbox |= c[2]
    raw_origin = (float(used[0][1][0]), float(used[0][1][1]))
    start = _dot(raw_origin, d)
    end = max(_proj_range(c[2], d)[1] for c in used if not c[0].isspace())

    span_rects = []
    for sid in span_by_id:
        boxes = [c[2] for c in used if id(c[3]) == sid and not c[0].isspace()]
        r = pymupdf.Rect(boxes[0])
        for b in boxes[1:]:
            r |= b
        span_rects.append(r)
    char_rects = [c[2] for c in used if not c[0].isspace()]
    space_widths = [abs(_proj_range(c[2], d)[1] - _proj_range(c[2], d)[0]) / max(float(c[3].get("size", 1) or 1), 1e-3)
                    for c in used if c[0] == " "]
    space_widths = [w for w in space_widths if 0.05 < w < 1.0]
    space_em = sorted(space_widths)[len(space_widths) // 2] if space_widths else None

    disp_bbox = pymupdf.Rect(raw_bbox) * rm
    disp_origin = pymupdf.Point(raw_origin) * rm
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
    )


def _dominant_style(lines: list[TextLine]) -> SpanStyle:
    weights: dict[tuple, int] = {}
    styles: dict[tuple, SpanStyle] = {}
    for ln in lines:
        k = _style_key(ln.style)
        weights[k] = weights.get(k, 0) + len(ln.text.strip())
        styles.setdefault(k, ln.style)
    return styles[max(weights, key=weights.get)]


def text_blocks(doc: pymupdf.Document, page: int, *, merge: bool = True) -> list[TextBlock]:
    """The editable text blocks of a page (display coordinates). Blocks that
    continue each other (same direction, size and line pitch, overlapping
    horizontally) are merged into one paragraph."""
    pg = doc[page]
    rm = pg.rotation_matrix
    data = pg.get_text("rawdict", flags=_EXTRACT_FLAGS)
    groups: list[list[TextLine]] = []
    for b in data.get("blocks", ()):
        if b.get("type", 0) != 0:
            continue
        lines = [ln for ln in (_build_line(rl, rm) for rl in b.get("lines", ())) if ln is not None]
        if lines:
            groups.append(lines)
    if merge:
        groups = _merge_groups(groups)
    blocks: list[TextBlock] = []
    for lines in groups:
        bbox = pymupdf.Rect(lines[0].bbox)
        for ln in lines[1:]:
            bbox |= ln.bbox
        blocks.append(TextBlock(page=page, bbox=bbox, lines=lines, style=_dominant_style(lines),
                                rotation=pg.rotation))
    return blocks


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


def _merge_groups(groups: list[list[TextLine]]) -> list[list[TextLine]]:
    """Join blocks that are consecutive lines of one paragraph but were split
    by content-stream order (typically after an edit appended new text)."""
    if len(groups) < 2:
        return groups
    changed = True
    while changed:
        changed = False
        for i, g in enumerate(groups):
            for k, h in enumerate(groups):
                if i == k:
                    continue
                if _continues(g, h):
                    groups[i] = g + h
                    del groups[k]
                    changed = True
                    break
            if changed:
                break
    for g in groups:
        n = _perp(g[0].dir)
        g.sort(key=lambda ln: (round(_dot(ln.raw_origin, n), 1), _dot(ln.raw_origin, ln.dir)))
    return groups


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
def _dedupe_font_files(doc: pymupdf.Document, first_new_xref: int) -> None:
    """Point freshly written font descriptors at byte-identical font file
    streams that already exist in the PDF (avoids embedding the same font
    program twice, e.g. when writing with the original embedded font)."""
    try:
        total = doc.xref_length()
    except Exception:
        return
    if first_new_xref >= total or total > 300_000:
        return

    def font_files(lo: int, hi: int):
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

    new = list(font_files(first_new_xref, total))
    if not new:
        return
    new_data: dict[int, bytes] = {}
    for _desc, _key, ff in new:
        try:
            new_data[ff] = doc.xref_stream(ff) or b""
        except Exception:
            new_data[ff] = b""
    lengths = {len(v) for v in new_data.values() if v}
    if not lengths:
        return
    existing: dict[tuple[str, int, str], int] = {}
    for _desc, key, ff in font_files(1, first_new_xref):
        if ff >= first_new_xref:
            continue
        try:
            data = doc.xref_stream(ff)
        except Exception:
            continue
        if data and len(data) in lengths:
            existing.setdefault((key, len(data), hashlib.sha1(data).hexdigest()), ff)
    for desc, key, ff in new:
        data = new_data.get(ff)
        if not data:
            continue
        old = existing.get((key, len(data), hashlib.sha1(data).hexdigest()))
        if old is None or old == ff:
            continue
        try:
            doc.xref_set_key(desc, key, f"{old} 0 R")
            # drop the now unreferenced duplicate stream
            doc.update_stream(ff, b"")
            doc.update_object(ff, "null")
        except Exception:
            pass


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
    """Write text runs (raw page coordinates) with TextWriter, one writer per
    colour for horizontal text and one per run for rotated text."""
    runs = [r for r in runs if r.text and r.text.strip()]
    if not runs:
        return
    doc = page.parent
    first_new = doc.xref_length()
    groups: dict[tuple, list[TextRun]] = {}
    for r in runs:
        horizontal = abs(r.dir[0] - 1.0) < 1e-4 and abs(r.dir[1]) < 1e-4
        color = tuple(round(float(c), 4) for c in r.color)
        key = ("h", color) if horizontal else ("r", id(r))
        groups.setdefault(key, []).append(r)
    for key, group in groups.items():
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
    _dedupe_font_files(doc, first_new)


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
        rects = [_center_box(r) for r in line.char_rects]
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
    data = page.get_text("rawdict", flags=_EXTRACT_FLAGS)
    for b in data.get("blocks", ()):
        if b.get("type", 0) != 0:
            continue
        for rl in b.get("lines", ()):
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
    if not rects:
        return
    saved = _stash_redactions(page)
    try:
        _apply_text_redactions(page, rects)
        leftovers = _leftover_chars(page, lines)
        if leftovers:
            _apply_text_redactions(page, [_center_box(r, 0.4, 0.35) for r in leftovers])
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


def _alignment(page: pymupdf.Page, lines: list[TextLine], d) -> str:
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
    pmin, pmax = _proj_range(page.rect * page.derotation_matrix, d)
    start, end = lines[0].extent
    left_gap, right_gap = start - pmin, pmax - end
    if left_gap > 36 and right_gap > 36 and abs(left_gap - right_gap) <= 3:
        return "center"
    return "left"


def _available_width(page: pymupdf.Page, block: TextBlock, d, align: str, lmin: float, rmax: float) -> float:
    """How wide a line of ``block`` may become before it is wrapped: the
    block's own width, or more when there is free space up to the mirrored
    page margin and no other text is in the way."""
    own = rmax - lmin
    if not _is_axis(d):
        return 0.0 if len(block.lines) == 1 else own  # 0 = never wrap
    n = _perp(d)
    pmin, pmax = _proj_range(page.rect * page.derotation_matrix, d)
    size = max(ln.style.size for ln in block.lines)
    band = (min(_proj_range(ln.raw_bbox, n)[0] for ln in block.lines),
            max(_proj_range(ln.raw_bbox, n)[1] for ln in block.lines))
    left_margin = max(lmin - pmin, 18.0)
    right_margin = max(pmax - rmax, 18.0)
    limit_lo = pmin + (right_margin if align == "right" else left_margin)
    limit_hi = pmax - (left_margin if align == "left" else right_margin)
    if align == "center":
        m = max(min(lmin - pmin, pmax - rmax), 18.0)
        limit_lo, limit_hi = pmin + m, pmax - m
    try:
        others = page.get_text("blocks", flags=_EXTRACT_FLAGS)
    except Exception:
        others = []
    for ob in others:
        if len(ob) > 6 and ob[6] != 0:
            continue
        r = pymupdf.Rect(ob[:4])
        o_lo, o_hi = _proj_range(r, n)
        if o_hi <= band[0] + 0.5 or o_lo >= band[1] - 0.5:
            continue  # not beside the block
        a, b = _proj_range(r, d)
        # a paragraph next to other text is a column: keep its width;
        # a single line may grow up to about one em from its neighbour
        gap = size if len(block.lines) == 1 else max(a - rmax, lmin - b, 0.0)
        if a >= rmax - 0.5:
            limit_hi = min(limit_hi, a - gap)
        elif b <= lmin + 0.5:
            limit_lo = max(limit_lo, b + gap)
    if align == "right":
        avail = rmax - limit_lo
    elif align == "center":
        c = (lmin + rmax) / 2
        avail = 2 * min(c - limit_lo, limit_hi - c)
    else:
        avail = limit_hi - lmin
    return max(own, avail)


def _plan(page: pymupdf.Page, block: TextBlock, new_text: str, style: SpanStyle | None) -> _Plan:
    old = block.lines
    n_old = len(old)
    override = style is not None and _style_key(style) != _style_key(block.style)
    new_style = style if override else None

    text = (new_text or "").replace("\r\n", "\n").replace("\r", "\n").replace("\t", "    ")
    paragraphs = [p.rstrip() for p in text.split("\n")]
    while paragraphs and not paragraphs[-1].strip():
        paragraphs.pop()

    if not paragraphs:
        return _Plan(remove=list(old), runs=[])

    def para_style(i: int) -> SpanStyle:
        src = old[min(i, n_old - 1)].style
        return _merge_style(src, block.style, new_style)

    # -- fonts, one per distinct style (covering all text in that style) ---
    texts_by_style: dict[tuple, list[str]] = {}
    style_by_key: dict[tuple, SpanStyle] = {}
    for i, p in enumerate(paragraphs):
        st = para_style(i)
        k = _style_key(st)
        style_by_key[k] = st
        texts_by_style.setdefault(k, []).append(p)
    fonts = {k: resolve_font(page.parent, block.page, style_by_key[k], "".join(v)).font
             for k, v in texts_by_style.items()}

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

    align = _alignment(page, old, d) if all(_is_axis(ln.dir) for ln in old) else "left"
    lmin = min(ln.extent[0] for ln in old)
    rmax = max(ln.extent[1] for ln in old)
    max_width = _available_width(page, block, d, align, lmin, rmax)

    # -- lay out lines -------------------------------------------------------
    def para_space(i: int, st: SpanStyle) -> float | None:
        em = old[min(i, n_old - 1)].space_em
        return em * st.size if em else None

    def soft_break_after(i: int) -> bool:
        """Did old line ``i`` wrap into line ``i + 1`` (same paragraph)?"""
        if i >= n_old - 1 or align != "left":
            return False
        a, b = old[i], old[i + 1]
        width = rmax - lmin
        full = a.extent[1] >= rmax - max(2.5 * a.style.size, 0.15 * width)
        return full and abs(b.extent[0] - lmin) <= 1.0

    layout: list[tuple[str, SpanStyle, int]] = []  # (text, style, paragraph index)
    carry = ""
    for i, p in enumerate(paragraphs):
        st = para_style(i)
        font = fonts[_style_key(st)]
        if carry:
            p = f"{carry} {p.lstrip()}" if p.strip() else carry
            carry = ""
        unchanged = (i < n_old and not override and p == old[i].text)
        pieces = [p] if (unchanged or not p) else _wrap(p, font, st.size, max_width, para_space(i, st))
        if len(pieces) > 1 and soft_break_after(i) and i + 1 < len(paragraphs) and paragraphs[i + 1].strip():
            # reflow: overflowing words continue on the paragraph's next line
            carry = " ".join(pieces[1:])
            pieces = pieces[:1]
        for piece in pieces:
            layout.append((piece, st, i))

    o0 = first.raw_origin
    remove: list[TextLine] = []
    runs: list[TextRun] = []
    for j, (txt, st, pi) in enumerate(layout):
        if j < n_old:
            base = old[j].raw_origin
            ld = old[j].dir
            start_j, end_j = old[j].extent
        else:
            last = old[-1].raw_origin
            k = j - n_old + 1
            base = (last[0] + n[0] * spacing * k, last[1] + n[1] * spacing * k)
            ld = old[-1].dir
            start_j, end_j = (_dot(base, d), _dot(base, d) + (rmax - lmin))
            if align == "right":
                start_j, end_j = rmax - (end_j - start_j), rmax
            elif align == "center":
                start_j, end_j = lmin, rmax
        if scale != 1.0:
            off = _dot((base[0] - o0[0], base[1] - o0[1]), n) * (scale - 1.0)
            base = (base[0] + n[0] * off, base[1] + n[1] * off)

        if j < n_old and not override and txt == old[j].text:
            continue  # unchanged line: leave the original glyphs alone
        if j < n_old:
            remove.append(old[j])
        if not txt.strip():
            continue
        font = fonts[_style_key(st)]
        space = para_space(pi, st)
        width = text_width(font, txt, st.size, space)
        if align == "right":
            target = end_j - width
        elif align == "center":
            target = (start_j + end_j) / 2 - width / 2
        else:
            target = _dot(base, ld) if j < n_old else _dot(old[-1].raw_origin, ld)
        shift = target - _dot(base, ld)
        origin = (base[0] + ld[0] * shift, base[1] + ld[1] * shift)
        runs.append(TextRun(text=txt, font=font, size=st.size, color=tuple(st.color), origin=origin, dir=ld,
                            space=space))
    for j in range(len(layout), n_old):
        remove.append(old[j])
    return _Plan(remove=remove, runs=runs)


# ---------------------------------------------------------------------------
# public API
# ---------------------------------------------------------------------------
def replace_block_text(pdf, block: TextBlock, new_text: str, style: SpanStyle | None = None) -> None:
    """Replace the text of ``block`` with ``new_text`` (lines separated by
    ``\\n``) keeping font, size, colour, position and alignment. Only text is
    removed – images and vector graphics underneath survive. Empty
    ``new_text`` deletes the block's text. ``style`` (optional) applies the
    changed attributes (vs. ``block.style``) to every line."""
    page = pdf.doc[block.page]
    plan = _plan(page, block, new_text, style)
    if not plan.remove and not plan.runs:
        return  # nothing changed -> no undo step
    with pdf.edit(_label("core.undo.edit_text", "Redigér tekst"), CONTENT, (block.page,)):
        page = pdf.doc[block.page]
        _remove_lines(page, plan.remove)
        write_runs(page, plan.runs)
