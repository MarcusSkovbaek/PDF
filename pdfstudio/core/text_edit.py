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
from .fonts import (
    SpanStyle,
    _is_mono_name,
    _serif_from_name,
    has_space,
    prepare_text,
    resolve_font,
    style_from_span,
    text_width,
)

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
    char_styles: list = field(default_factory=list, repr=False)  # SpanStyle per char of ``text``
    char_rises: list = field(default_factory=list, repr=False)  # baseline offset per char (pt)


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
            style.bold, style.italic, style.serif, style.mono)


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
    end = max(_proj_range(c[2], d)[1] for c in used if not c[0].isspace())

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
        lines = [ln for rl in b.get("lines", ()) for ln in _build_lines(rl, rm)]
        if lines:
            groups.extend(_split_columns(lines))
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
                        if j not in used and _continues(merged, gs[j]):
                            merged = merged + gs[j]
                            used.add(j)
                            changed = True
                            break
                    else:
                        break
                result.append(merged)
            gs = result
        out.extend(gs)
    for g in out:
        n = _perp(g[0].dir)
        g.sort(key=lambda ln: (round(_dot(ln.raw_origin, n), 1), _dot(ln.raw_origin, ln.dir)))
    return _reading_order(out)


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
    if first_new_xref >= total or total > 150_000:
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


def _plan(page: pymupdf.Page, block: TextBlock, new_text: str, style: SpanStyle | None) -> _Plan:
    old = block.lines
    n_old = len(old)
    override = style is not None and _style_key(style) != _style_key(block.style)
    new_style = style if override else None

    text = (new_text or "").replace("\r\n", "\n").replace("\r", "\n").replace("\t", "    ")
    rich_all = _styled_text(block, text)
    if new_style is not None:
        cache: dict[int, SpanStyle] = {}

        def merged(st: SpanStyle) -> SpanStyle:
            if id(st) not in cache:
                cache[id(st)] = _merge_style(st, block.style, new_style)
            return cache[id(st)]

        rich_all = [(ch, merged(st), rise) for ch, st, rise in rich_all]
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
        if override or i >= n_old or _rich_text(rich) != old[i].text:
            return False
        styles = old[i].char_styles or [old[i].style] * len(old[i].text)
        rises = old[i].char_rises or [0.0] * len(old[i].text)
        return all(c[1] is s and c[2] == r for c, s, r in zip(rich, styles, rises))

    # -- fonts, one per distinct style (covering all text in that style) ---
    by_key: dict[tuple, SpanStyle] = {}
    texts: dict[tuple, list[str]] = {}
    for rich in paragraphs:
        for ch, st, _r in rich:
            k = _style_key(st)
            by_key.setdefault(k, st)
            texts.setdefault(k, []).append(ch)
    fonts = {k: resolve_font(page.parent, block.page, by_key[k], "".join(v).replace("\ufffd", "")).font
             for k, v in texts.items()}

    def font_of(st: SpanStyle) -> pymupdf.Font:
        return fonts[_style_key(st)]

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
    lmin = min(ln.extent[0] for ln in old)
    rmax = max(ln.extent[1] for ln in old)
    at_right = [abs(ln.extent[1] - rmax) <= 1.0 for ln in old]

    def stretched(ln: TextLine) -> bool:
        """Is this line wider than its natural width (justified text)?"""
        styles = ln.char_styles or [ln.style] * len(ln.text)
        if " " not in ln.text or any(_style_key(st) not in fonts for st in styles):
            return False
        natural = 0.0
        for txt, st, _rise in _runs(list(zip(ln.text, styles, [0.0] * len(styles)))):
            natural += text_width(fonts[_style_key(st)], txt, st.size)
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
    max_width = _available_width(ctx, block, d, align, lmin, rmax, paragraph)

    def soft_break_after(i: int) -> bool:
        """Did old line ``i`` wrap into line ``i + 1`` (same paragraph)?"""
        if i >= n_old - 1 or not paragraph:
            return False
        return full[i] and abs(old[i + 1].extent[0] - lmin) <= 1.0

    # -- lay out lines (wrap + reflow into the paragraph's next lines) -------
    layout: list[tuple[list, int, bool]] = []  # (rich, paragraph index, paragraph continues)
    carry: list = []
    for i, rich in enumerate(paragraphs):
        if carry:
            body = rich
            while body and body[0][0] == " ":
                body = body[1:]
            rich = carry + ([(" ", carry[-1][1], carry[-1][2])] + body if body else [])
            carry = []
        if unchanged(i, rich) or not rich:
            pieces = [rich]
        else:
            pieces = _wrap_rich(rich, max_width, lambda r, i=i: measure(r, space_em(i)))
        if len(pieces) > 1 and soft_break_after(i) and i + 1 < len(paragraphs) \
                and _rich_text(paragraphs[i + 1]).strip():
            carry = [c for p in pieces[1:] for c in (p + [(" ", p[-1][1], p[-1][2])])][:-1]
            pieces = pieces[:1]
            layout.append((pieces[0], i, True))
            continue
        para_goes_on = (soft_break_after(i) and i + 1 < len(paragraphs)
                        and bool(_rich_text(paragraphs[i + 1]).strip()))
        for k, piece in enumerate(pieces):
            layout.append((piece, i, k < len(pieces) - 1 or para_goes_on))

    o0 = first.raw_origin
    remove: list[TextLine] = []
    runs: list[TextRun] = []
    for j, (rich, pi, continues) in enumerate(layout):
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

        if j < n_old and pi == j and unchanged(j, rich):
            continue  # unchanged line: leave the original glyphs alone
        if j < n_old:
            remove.append(old[j])
        if not _rich_text(rich).strip():
            continue
        em = space_em(pi)
        width = measure(rich, em)
        if align == "right":
            target = end_j - width
        elif align == "center":
            target = (start_j + end_j) / 2 - width / 2
        else:
            target = _dot(base, ld) if j < n_old else _dot(old[-1].raw_origin, ld)
        extra = 0.0
        n_spaces = sum(1 for c in rich if c[0] == " ")
        if (justified_block and continues and j < n_old - 1 and at_right[j] and n_spaces
                and 0.6 * (rmax - target) <= width < rmax - target):
            extra = (rmax - target - width) / n_spaces
        shift = target - _dot(base, ld)
        origin = (base[0] + ld[0] * shift, base[1] + ld[1] * shift)
        runs.extend(_emit_line(rich, origin, ld, em, extra, font_of))
    for j in range(len(layout), n_old):
        remove.append(old[j])
    return _Plan(remove=remove, runs=runs, align=align, wrap_width=max_width)


def _wrap_rich(rich, width: float, measure) -> list[list]:
    """Greedy word wrap of rich text to ``width`` (0 = never wrap)."""
    tol = 1.5 + 0.01 * width
    if width <= 0 or measure(rich) <= width + tol:
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
    for sep, word in tokens:
        cand = cur + sep + word if (cur or not lines) else word
        if measure(cand) <= width + tol:
            cur = cand
            continue
        if cur:
            lines.append(cur)
            cur = []
        while word and measure(word) > width + tol:
            k = len(word)
            while k > 1 and measure(word[:k]) > width + tol:
                k -= 1
            lines.append(word[:k])
            word = word[k:]
        cur = word
    if cur:
        lines.append(cur)
    return lines or [rich]


def _emit_line(rich, origin, d, em, extra: float, font_of) -> list[TextRun]:
    """Turn one laid-out rich line into positioned TextRuns. Runs of one
    style are written in one piece unless spaces must be stretched
    (justification) or the font has no space glyph."""
    n = _perp(d)
    out: list[TextRun] = []
    x = 0.0
    for txt, st, rise in _runs(rich):
        font = font_of(st)
        space = em * st.size if em else None
        pieces = re.split(r"( +)", txt) if (extra or not has_space(font)) else [txt]
        for piece in pieces:
            if not piece:
                continue
            w = text_width(font, piece, st.size, space)
            if piece[0] == " " and not piece.strip():
                x += w + extra * len(piece)
                continue
            if piece.strip():
                pos = (origin[0] + d[0] * x + n[0] * rise, origin[1] + d[1] * x + n[1] * rise)
                out.append(TextRun(text=piece, font=font, size=st.size, color=tuple(st.color),
                                   origin=pos, dir=d, space=space))
            x += w + (extra * piece.count(" ") if extra else 0.0)
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


def layout_info(doc: pymupdf.Document, block: TextBlock) -> LayoutInfo:
    """Alignment and wrap width that editing ``block`` will use."""
    try:
        plan = _plan(doc[block.page], block, block.text, None)
    except Exception:  # never let a UI hint break editing
        return LayoutInfo("left", float(block.bbox.width))
    return LayoutInfo(plan.align, float(plan.wrap_width))


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
