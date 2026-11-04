"""Flowing text: a page's column edited as one text, as in Word.

Model
-----
* A **story** is what one editor shows: a column of body text on one page
  (``kind == "flow"``), or a standalone text – header, footer, table cell,
  caption, text in a figure or a box, rotated text (``kind == "single"``).
* Its **pieces** are its blocks (:func:`text_edit.text_blocks`) in reading
  order. A piece's ``text`` is what the editor shows: soft wraps as spaces,
  hard line breaks as "\\n". A paragraph split by a page break or by an
  image is two pieces; the second one ``cont_prev`` (detected from the
  layout, or from a marker Folio leaves in the page when it splits one).
* :func:`apply_story` takes the edited paragraphs (:class:`Edit`, in order,
  new ones from Enter included), typesets the changed ones with
  :mod:`text_edit` (same fonts, sizes, colours, alignment, lists) and lays
  out every line from the story's page on – line by line into the column,
  around images, tables and figures (they stay where they are), on into
  the same column of the next page and onto new pages when the last one is
  full. Text that continues from the next page is pulled back when there is
  room. Everything is one undo step (``merge`` joins several).

All geometry inside is in raw (unrotated) page space: ``d`` is the text
direction, ``n`` the direction from one line to the next; positions along
them are ``_dot(point, d)`` / ``_dot(point, n)``.
"""

from __future__ import annotations

import re
import statistics
from collections import OrderedDict
from dataclasses import dataclass, field, replace

import pymupdf

from . import running
from . import text_edit as te
from .document import CONTENT, STRUCTURE, PdfDocument
from .fonts import SpanStyle
from .text_edit import TextBlock, TextLine, TextRun, _dot, _is_axis, _perp, _proj_range

# ---------------------------------------------------------------------------
# public data
# ---------------------------------------------------------------------------


@dataclass
class Piece:
    """One block of a story."""

    block: TextBlock
    text: str                       # editor text (soft wraps as " ", hard breaks "\n")
    soft: tuple = ()                # offsets of the soft-wrap spaces in ``text``
    layout: te.LayoutInfo | None = None
    cont_prev: bool = False         # continues the paragraph of the previous piece / page
    cont_next: bool = False         # its paragraph continues in the next piece / page
    bullet: TextBlock | None = None  # list bullet beside the first line


@dataclass
class Story:
    page: int
    kind: str                       # "flow" | "single"
    pieces: list[Piece]
    lo: float = 0.0                 # the column along the text direction (raw)
    hi: float = 0.0
    dir: tuple = (1.0, 0.0)
    rect: pymupdf.Rect = field(default_factory=pymupdf.Rect)  # display rect (column on this page)
    role: str = "body"              # body | header | footer | cell | caption | box | figure | rotated | wide

    @property
    def text(self) -> str:
        return "\n".join(p.text for p in self.pieces)


@dataclass
class Edit:
    """One paragraph of an edited story, in order."""

    piece: int | None               # index into ``story.pieces``; None: a new paragraph
    text: str
    like: int | None = None         # a new paragraph looks like this piece (Enter splits it)
    formats: list | None = None     # text_edit formats, offsets into ``text``
    align: str | None = None
    line_spacing: float | None = None
    style: SpanStyle | None = None


@dataclass
class _Placed:
    page: int                       # final page index
    base: float                     # baseline along n (raw)
    text: str                       # the line's text
    start: int                      # offset of the line in its paragraph text
    xs: list | None = None          # raw position along d of each character (and the end)
    rot: object = None              # the page's rotation matrix (raw -> display)
    axes: tuple = ((1.0, 0.0), (0.0, 1.0))   # (d, n)


@dataclass
class FlowResult:
    pages: tuple = ()               # pages whose text changed (final indices)
    inserted: tuple = ()            # pages Folio added
    removed: tuple = ()             # pages Folio removed again
    renumbered: tuple = ()          # pages whose page number was brought up to date
    overflow: float = 0.0
    substitutions: list = field(default_factory=list)
    lines: dict = field(default_factory=dict)   # unit's first edit index -> [_Placed]
    parts: dict = field(default_factory=dict)   # edit index -> (unit's first edit index, offset in its text)

    def locate(self, edit: int, offset: int):
        """Where character ``offset`` of edited paragraph ``edit`` ended up:
        ``(page, (x, y))`` in display space (y on the baseline), or None."""
        key, delta = self.parts.get(edit, (edit, 0))
        offset += delta
        placed = self.lines.get(key) or []
        best = None
        for pl in placed:
            if pl.start <= offset:
                best = pl
        if best is None:
            best = placed[0] if placed else None
        if best is None:
            return None
        k = max(0, min(offset - best.start, len(best.text)))
        return best.page, _point_in_line(best, k)


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------
_CAPTION = re.compile(r"^\s*(figur|fig\.|tabel|table|figure|billede|graf|diagram|kilde|source)\b", re.I)
_SENTENCE_END = tuple(".!?:;»\"”)")
_MARKER_KEY = "FolioPDF"


def _band(ln: TextLine, n) -> tuple[float, float]:
    b = _dot(ln.raw_origin, n)
    size = ln.style.size or 10.0
    return b - ln.ascender * size, b - ln.descender * size


def _base(ln: TextLine, n) -> float:
    return _dot(ln.raw_origin, n)


def _pitch(lines: list[TextLine], n) -> float:
    if len(lines) >= 2:
        gaps = sorted(_base(b, n) - _base(a, n) for a, b in zip(lines, lines[1:]))
        g = gaps[len(gaps) // 2]
        if g > 0.5 * (lines[0].style.size or 10.0):
            return g
    return 1.2 * (lines[0].style.size or 10.0)


def _layout(doc, block: TextBlock) -> te.LayoutInfo | None:
    """text_edit's layout of a block, cached per document object."""
    cache = getattr(doc, "_folio_layouts", None)
    if cache is None:
        cache = {}
        try:
            doc._folio_layouts = cache
        except Exception:
            pass
    try:
        from .fonts import _page_key
        key = (_page_key(doc[block.page]), tuple(round(v, 2) for v in block.bbox), block.text)
    except Exception:
        key = None
    if key is not None and key in cache:
        return cache[key]
    try:
        lay = te.layout_info(doc, block)
    except Exception:
        lay = None
    if key is not None:
        if len(cache) > 600:
            cache.clear()
        cache[key] = lay
    return lay


def _piece(doc, block: TextBlock) -> Piece:
    lay = _layout(doc, block)
    text, soft = te.editor_text(block, lay)
    return Piece(block=block, text=text, soft=soft, layout=lay)


def _markers(doc, page: int) -> list[tuple[float, float]]:
    """Starts (raw x, y of the first baseline) of pieces Folio split off the
    previous frame – they continue a paragraph."""
    try:
        kind, val = doc.xref_get_key(doc[page].xref, f"PieceInfo/{_MARKER_KEY}/Private/Cont")
    except Exception:
        return []
    if kind != "array":
        return []
    nums = [float(x) for x in re.findall(r"-?\d+(?:\.\d+)?", val)]
    return list(zip(nums[0::2], nums[1::2]))


def _is_inserted(doc, page: int) -> bool:
    try:
        return doc.xref_get_key(doc[page].xref, f"PieceInfo/{_MARKER_KEY}/Private/Inserted")[1] == "true"
    except Exception:
        return False


def _running_rects(doc, page: int) -> list[pymupdf.Rect]:
    """Header / footer areas Folio copied onto a page it inserted."""
    try:
        kind, val = doc.xref_get_key(doc[page].xref, f"PieceInfo/{_MARKER_KEY}/Private/Running")
    except Exception:
        return []
    if kind != "array":
        return []
    nums = [float(x) for x in re.findall(r"-?\d+(?:\.\d+)?", val)]
    return [pymupdf.Rect(nums[i:i + 4]) for i in range(0, len(nums) - 3, 4)]


def _set_markers(doc, page: int, conts: list[tuple[float, float]], inserted: bool | None = None,
                 running: list | None = None) -> None:
    pg = doc[page]
    if inserted is None:
        inserted = _is_inserted(doc, page)
    if running is None:
        running = _running_rects(doc, page) if inserted else []
    if not conts and not inserted:
        if doc.xref_get_key(pg.xref, f"PieceInfo/{_MARKER_KEY}")[0] != "null":
            doc.xref_set_key(pg.xref, f"PieceInfo/{_MARKER_KEY}", "null")
        return
    arr = " ".join(f"{x:.2f} {y:.2f}" for x, y in conts)
    from .identity import pdf_date
    run = " ".join(f"{r.x0:.2f} {r.y0:.2f} {r.x1:.2f} {r.y1:.2f}" for r in running)
    priv = f"<</Cont[{arr}]" + ("/Inserted true" if inserted else "") + (f"/Running[{run}]" if run else "") + ">>"
    doc.xref_set_key(pg.xref, "PieceInfo", f"<</{_MARKER_KEY}<</LastModified({pdf_date()})/Private{priv}>>>>")


def _looks_continued(a: TextBlock, b: TextBlock, lo: float, hi: float) -> bool:
    """Does block ``b`` (next frame) continue the paragraph that ends ``a``?"""
    last, first = a.lines[-1], b.lines[0]
    size = last.style.size or 10.0
    if abs((first.style.size or size) - size) > 0.06 * size or last.dir != first.dir:
        return False
    if te.family_key(first.style.font) != te.family_key(last.style.font):
        return False
    if last.extent[1] < hi - max(2.0, 0.8 * size):     # the line did not run to the column's end
        return False
    if first.extent[0] > lo + max(1.5, 0.3 * size):    # an indented first line: a new paragraph
        return False
    text = first.text.lstrip()
    if text and text[0].islower():
        return True
    return not last.text.rstrip().endswith(_SENTENCE_END)


# ---------------------------------------------------------------------------
# page analysis: which blocks form which story
# ---------------------------------------------------------------------------
@dataclass
class _PageInfo:
    stories: list[Story]
    obstacles: list[pymupdf.Rect]   # raw rects the flowing text must keep clear of
    header_bottom: float | None     # along n (raw), body dir
    footer_top: float | None
    dir: tuple


def _quick_blocks(doc, page: int) -> list[tuple]:
    """(x0, y0, x1, y1, text) of a page's text blocks (display space), cheap."""
    cache = getattr(doc, "_folio_quick", None)
    if cache is None:
        cache = {}
        try:
            doc._folio_quick = cache
        except Exception:
            pass
    try:
        from .fonts import _page_key
        key = _page_key(doc[page])
    except Exception:
        key = None
    if key is not None and key in cache:
        return cache[key]
    out = [tuple(b[:5]) for b in doc[page].get_text("blocks") if b[6] == 0 and str(b[4]).strip()]
    if key is not None:
        if len(cache) > 400:
            cache.clear()
        cache[key] = out
    return out


def _norm(text: str) -> str:
    return re.sub(r"\d+", "#", " ".join(text.split()))


def _repeats(doc, page: int, bbox: pymupdf.Rect, text: str) -> bool:
    """Is a block like this on a neighbouring page at the same place
    (a running header or footer)?"""
    key = _norm(text)
    for q in (page - 1, page + 1, page - 2, page + 2):
        if 0 <= q < doc.page_count:
            for x0, y0, x1, y1, t in _quick_blocks(doc, q):
                if abs(y0 - bbox.y0) <= 2.5 and (abs(x0 - bbox.x0) <= 40 or abs(x1 - bbox.x1) <= 40) \
                        and _norm(str(t)) == key:
                    return True
    return False


def _analyze(doc, page: int) -> _PageInfo:
    cache = getattr(doc, "_folio_flow", None)
    if cache is None:
        cache = {}
        try:
            doc._folio_flow = cache
        except Exception:
            pass
    try:
        from .fonts import _page_key
        key = _page_key(doc[page])
    except Exception:
        key = None
    if key is not None and key in cache:
        return cache[key]
    info = _analyze_page(doc, page)
    if key is not None:
        if len(cache) > 120:
            cache.clear()
        cache[key] = info
    return info


def _analyze_page(doc, page: int) -> _PageInfo:
    pg = doc[page]
    blocks = [b for b in te.text_blocks(doc, page) if b.lines and b.text.strip()]
    prect = pg.rect
    area = prect.width * prect.height
    # the body's text direction: that of most multi-line text
    dirs: dict[tuple, int] = {}
    for b in blocks:
        if _is_axis(b.lines[0].dir) and all(ln.dir == b.lines[0].dir for ln in b.lines):
            dirs[b.lines[0].dir] = dirs.get(b.lines[0].dir, 0) + sum(len(ln.text) for ln in b.lines)
    d = max(dirs, key=dirs.get) if dirs else (1.0, 0.0)
    n = _perp(d)
    decor = [r for b in blocks for ln in b.lines for r in ln.decor_rects]
    obstacles = []
    for r in te._obstacles(pg, decor):
        r = pymupdf.Rect(r)
        if r.is_empty and r.width < 0.3 and r.height < 0.3:
            continue
        if abs(r.width * r.height) > 0.6 * area:   # a page background
            continue
        obstacles.append(r)

    def raw_rect(b: TextBlock) -> pymupdf.Rect:
        return pymupdf.Rect(b.bbox) * pg.derotation_matrix

    stories: list[Story] = []
    body: list[TextBlock] = []
    bullets: list[TextBlock] = []
    roles: dict[int, str] = {}
    sizes = sorted(ln.style.size for b in blocks for ln in b.lines if ln.style.size)
    body_size = statistics.median(sizes) if sizes else 10.0
    for b in blocks:
        first = b.lines[0]
        role = None
        if not (_is_axis(first.dir) and all(ln.dir == d for ln in b.lines)):
            role = "rotated"
        elif any(ln.cell for ln in b.lines):
            role = "cell"
        elif te.is_list_bullet(b):
            bullets.append(b)
            continue
        else:
            r = raw_rect(b)
            inside = [o for o in obstacles if (o & r).get_area() > 0.5 * max(r.get_area(), 1e-6)]
            if inside:
                # text in a frame of its own (a callout box) or on an image / chart
                boxed = any(o.x0 <= r.x0 + 1 and o.y0 <= r.y0 + 1 and o.x1 >= r.x1 - 1 and o.y1 >= r.y1 - 1
                            and o.get_area() > 1.05 * r.get_area() for o in inside)
                role = "box" if boxed else "figure"
            else:
                # running headers / footers: in the top or bottom 13 % of the
                # page along the line direction (rotated pages too)
                pmin, pmax = _proj_range(prect * pg.derotation_matrix, n)
                zone = 0.13 * (pmax - pmin)
                top, bot = _proj_range(r, n)
                small = (b.style.size or body_size) <= 0.92 * body_size
                if (bot <= pmin + zone or top >= pmax - zone) and len(b.lines) <= 2 \
                        and (_repeats(doc, page, b.bbox, b.text) or small):
                    role = "header" if bot <= pmin + zone else "footer"
        if role is None:
            body.append(b)
        else:
            roles[id(b)] = role
    # captions: short text right above / below a figure or table (only the
    # real figures count – not one caption's box, or every line below would be one)
    figures = list(obstacles) + [raw_rect(x) for x in blocks if roles.get(id(x)) in ("cell", "figure")]
    area_lo = min((ln.extent[0] for b in body for ln in b.lines), default=0.0)
    area_hi = max((ln.extent[1] for b in body for ln in b.lines), default=0.0)
    joined = []
    for b in list(body):
        if len(b.lines) > 2:
            continue
        r = raw_rect(b)
        a0, a1 = _proj_range(r, d)
        c0, c1 = _proj_range(r, n)
        size = b.style.size or body_size
        near = []
        for o in figures:
            o0, o1 = _proj_range(o, d)
            p0, p1 = _proj_range(o, n)
            if min(a1, o1) - max(a0, o0) <= 0:
                continue
            if -0.5 * size <= c0 - p1 <= 1.6 * size or -0.5 * size <= p0 - c1 <= 1.6 * size:
                near.append(o)
        if not near:
            continue
        centred = abs((a0 - area_lo) - (area_hi - a1)) <= 3.0 and (a1 - a0) <= 0.85 * (area_hi - area_lo)
        if _CAPTION.match(b.text) or centred:
            body.remove(b)
            roles[id(b)] = "caption"
            joined.extend(pymupdf.Rect(o) | r for o in near)   # the figure and its caption stay together
    obstacles.extend(joined)
    # columns: the extents of multi-line body text, merged where they overlap
    multi = [b for b in body if len(b.lines) >= 2] or body
    ranges: list[list[float]] = []
    for b in sorted(multi, key=lambda b: min(ln.extent[0] for ln in b.lines)):
        lo = min(ln.extent[0] for ln in b.lines)
        hi = max(ln.extent[1] for ln in b.lines)
        if ranges and lo < ranges[-1][1] - 1.0:
            ranges[-1][1] = max(ranges[-1][1], hi)
        else:
            ranges.append([lo, hi])
    columns: list[list[TextBlock]] = [[] for _ in ranges]
    for b in body:
        lo = min(ln.extent[0] for ln in b.lines)
        hi = max(ln.extent[1] for ln in b.lines)
        overlaps = [max(0.0, min(hi, c[1]) - max(lo, c[0])) for c in ranges]
        if not overlaps or max(overlaps) <= 0:
            roles[id(b)] = "wide"
            continue
        k = max(range(len(ranges)), key=lambda i: overlaps[i])
        others = [o for i, o in enumerate(overlaps) if i != k and o > 0.3 * (ranges[i][1] - ranges[i][0])]
        if others:
            roles[id(b)] = "wide"         # spans several columns (a heading over both)
            continue
        columns[k].append(b)
    # list bullets go with the item beside them
    owner: dict[int, TextBlock] = {}
    for bl in bullets:
        bb = _base(bl.lines[0], n)
        size = bl.style.size or body_size
        best = None
        for col in columns:
            for b in col:
                f = b.lines[0]
                if abs(_base(f, n) - bb) <= 0.45 * size and 0 <= f.extent[0] - bl.lines[0].extent[1] <= 4 * size:
                    if best is None or f.extent[0] < best.lines[0].extent[0]:
                        best = b
        if best is not None:
            owner[id(best)] = bl
        else:
            roles[id(bl)] = "figure"        # a lone symbol: stays where it is
    # stories
    for k, col in enumerate(columns):
        if not col:
            continue
        col.sort(key=lambda b: (_band(b.lines[0], n)[0], b.lines[0].extent[0]))
        pieces = []
        for b in col:
            pc = _piece(doc, b)
            pc.bullet = owner.get(id(b))
            pieces.append(pc)
        rect = pymupdf.Rect()
        for p in pieces:
            rect |= p.block.bbox
            if p.bullet is not None:
                rect |= p.bullet.bbox
        stories.append(Story(page=page, kind="flow", pieces=pieces, lo=ranges[k][0], hi=ranges[k][1], dir=d,
                             rect=rect, role="body"))
    for b in blocks:
        role = roles.get(id(b))
        if role is None:
            continue
        lines_d = b.lines[0].dir
        stories.append(Story(page=page, kind="single", pieces=[_piece(doc, b)],
                             lo=min(ln.extent[0] for ln in b.lines), hi=max(ln.extent[1] for ln in b.lines),
                             dir=lines_d, rect=pymupdf.Rect(b.bbox), role=role))
    # obstacles for the flow: images, drawings and text that does not flow
    for s in stories:
        if s.kind == "single" and s.role in ("cell", "caption", "figure", "box", "wide", "rotated"):
            obstacles.append(raw_rect(s.pieces[0].block))
    head = [s for s in stories if s.role == "header"]
    foot = [s for s in stories if s.role == "footer"]
    header_bottom = max((_proj_range(raw_rect(s.pieces[0].block), n)[1] for s in head), default=None)
    footer_top = min((_proj_range(raw_rect(s.pieces[0].block), n)[0] for s in foot), default=None)
    info = _PageInfo(stories=stories, obstacles=obstacles, header_bottom=header_bottom, footer_top=footer_top, dir=d)
    _mark_continuations(doc, page, info)
    return info


def _mark_continuations(doc, page: int, info: _PageInfo) -> None:
    """Pieces of a column that continue the one before them (a paragraph
    split around an image), from Folio's markers or the layout."""
    marks = _markers(doc, page)
    for s in info.stories:
        if s.kind != "flow":
            continue
        n = _perp(s.dir)
        for a, b in zip(s.pieces, s.pieces[1:]):
            f = b.block.lines[0]
            marked = any(abs(x - f.raw_origin[0]) <= 0.6 and abs(y - f.raw_origin[1]) <= 0.6 for x, y in marks)
            if not marked:
                lo_a, hi_a = _band(a.block.lines[-1], n)
                lo_b, _hi_b = _band(f, n)
                between = [o for o in info.obstacles
                           if _proj_range(o, n)[0] >= hi_a - 1.0 and _proj_range(o, n)[1] <= lo_b + 1.0
                           and min(_proj_range(o, s.dir)[1], s.hi) - max(_proj_range(o, s.dir)[0], s.lo) > 0]
                marked = bool(between) and _looks_continued(a.block, b.block, s.lo, s.hi)
            if marked:
                a.cont_next = True
                b.cont_prev = True


def page_stories(doc, page: int) -> list[Story]:
    """The stories of a page: its columns of body text, then the standalone
    texts (headers, footers, cells, captions, …)."""
    return _analyze(doc, page).stories


def story_at(doc, page: int, point) -> tuple[Story, int] | None:
    """The story and piece index at a display-space point (the nearest piece
    when the point is in a column's gap between paragraphs)."""
    if page < 0:
        return None
    x, y = float(point[0]), float(point[1])
    best = None
    stories = page_stories(doc, page)
    for s in stories:
        for i, p in enumerate(s.pieces):
            r = pymupdf.Rect(p.block.bbox)
            if p.bullet is not None:
                r |= p.bullet.bbox
            pad = 2.0
            if r.x0 - pad <= x <= r.x1 + pad and r.y0 - pad <= y <= r.y1 + pad:
                area = r.width * r.height
                if best is None or area < best[2]:
                    best = (s, i, area)
    if best is None:
        # between two paragraphs of a column: the nearest one (as in Word)
        for s in stories:
            if s.kind != "flow" or not (s.rect.x0 - 2 <= x <= s.rect.x1 + 2 and s.rect.y0 - 2 <= y <= s.rect.y1 + 2):
                continue
            for i, p in enumerate(s.pieces):
                r = p.block.bbox
                dist = max(r.y0 - y, y - r.y1, 0.0)
                if best is None or dist < best[2]:
                    best = (s, i, dist)
    return (best[0], best[1]) if best else None


# ---------------------------------------------------------------------------
# the column on the following pages
# ---------------------------------------------------------------------------
def _compatible(doc, a: int, b: int) -> bool:
    pa, pb = doc[a], doc[b]
    return pa.rotation == pb.rotation and pa.mediabox == pb.mediabox and pa.rect == pb.rect


def _matching(story: Story, other: list[Story], columns: int | None = None) -> Story | None:
    """The same column on another page (same width and place; on a page
    with as many columns, when ``columns`` is given)."""
    w = story.hi - story.lo
    if columns is not None and sum(1 for s in other if s.kind == "flow" and s.dir == story.dir) != columns:
        return None
    for s in other:
        if s.kind != "flow" or s.dir != story.dir:
            continue
        ov = min(s.hi, story.hi) - max(s.lo, story.lo)
        if ov >= 0.8 * min(w, s.hi - s.lo) and abs((s.hi - s.lo) - w) <= 0.12 * w + 6.0:
            return s
    return None


@dataclass
class _Area:
    top: float        # first line's top may start here (along n)
    bottom: float     # lines must end above this


def _type_area(doc, page: int, story: Story, info: _PageInfo) -> _Area:
    """Where the column's text may go on its pages: the highest first line
    and the lowest last line of this column on the pages around (the type
    area of a full page), within the header and footer."""
    n = _perp(story.dir)
    pg = doc[page]
    pmin, pmax = _proj_range(pg.rect * pg.derotation_matrix, n)
    size = story.pieces[0].block.style.size or 10.0
    tops, bottoms = [], []
    own = [_band(p.block.lines[0], n)[0] for p in story.pieces]
    own_bottom = max(_band(p.block.lines[-1], n)[1] for p in story.pieces)
    tops.append(min(own))
    last_page = doc.page_count - 1
    columns = sum(1 for s in info.stories if s.kind == "flow" and s.dir == story.dir)
    for q in range(max(0, page - 3), min(doc.page_count, page + 4)):
        if q == page or not _compatible(doc, page, q):
            continue
        try:
            other = _analyze(doc, q)
        except Exception:
            continue
        m = _matching(story, other.stories, columns)
        if m is None:
            continue
        tops.append(min(_band(p.block.lines[0], n)[0] for p in m.pieces))
        if q != last_page:
            bottoms.append(max(_band(p.block.lines[-1], n)[1] for p in m.pieces))
    top = statistics.median(tops)
    if bottoms:
        # pages the text ran over from are full: their lowest line is the
        # bottom of the type area
        bottom = max(bottoms + ([own_bottom] if page != last_page else []))
    else:
        # nothing to measure it on (one page, or none like it): margins as
        # at the top, as a word processor has them
        margin = max(36.0, top - pmin)
        bottom = pmax - margin
    bottom = max(bottom, own_bottom)
    if info.footer_top is not None:
        bottom = min(bottom, info.footer_top - 0.5 * size) if info.footer_top - 0.5 * size > own_bottom else bottom
    bottom = min(bottom, pmax - 6.0)
    return _Area(top=top, bottom=bottom)


def _starts_section(story: Story) -> bool:
    """Does the column start with a heading (a chapter on a new page)?"""
    if not story.pieces:
        return False
    first = story.pieces[0]
    sizes = [ln.style.size for p in story.pieces for ln in p.block.lines if ln.style.size]
    body = statistics.median(sizes) if sizes else 10.0
    return (first.block.style.size or body) >= 1.25 * body and len(first.block.lines) <= 3 and not first.cont_prev


# ---------------------------------------------------------------------------
# typesetting: paragraphs -> lines
# ---------------------------------------------------------------------------
@dataclass
class _Line:
    page: int                     # page whose raw coordinates ``base`` / ``runs`` use
    base: float                   # baseline along n
    asc: float
    desc: float
    lo: float                     # extent along d
    hi: float
    gap: float | None             # distance from the previous line's baseline (None: top of a frame)
    text: str = ""
    start: int = 0                # offset in the paragraph text
    runs: list | None = None      # changed text: the runs to write
    orig: TextLine | None = None  # unchanged text: the original line
    orig_page: int | None = None  # its page


@dataclass
class _Unit:
    """A paragraph as the layout sees it."""

    pieces: list[Piece]           # its original pieces (empty: new)
    pages: list[int]              # page of each piece
    edit: int | None = None       # index into the edits (None: not edited)
    text: str | None = None       # new text (None: unchanged)
    like: Piece | None = None
    like_page: int | None = None
    before: float | None = None   # baseline distance from the previous paragraph
    min_page: int = 0             # never moves to an earlier page than this
    lines: list[_Line] = field(default_factory=list)
    bullet: TextBlock | None = None
    bullet_page: int | None = None
    new_bullet: bool = False      # copy ``like``'s bullet (a new list item)
    parts: list = field(default_factory=list)   # (edit index, offset of its text in the unit text)
    fixed: bool = False           # formats / style / alignment changed: no line is kept as it was


def _shift_line(ln: TextLine, n, s: float) -> TextLine:
    """A copy of ``ln`` moved ``s`` pt along ``n`` (raw space)."""
    dx, dy = n[0] * s, n[1] * s

    def mv(r):
        return pymupdf.Rect(r) + (dx, dy, dx, dy) if r is not None else None

    return replace(ln, raw_origin=(ln.raw_origin[0] + dx, ln.raw_origin[1] + dy), raw_bbox=mv(ln.raw_bbox),
                   char_rects=[mv(r) for r in ln.char_rects], span_rects=[mv(r) for r in ln.span_rects],
                   decor_rects=[mv(r) for r in ln.decor_rects])


def _unit_text(unit: _Unit) -> str:
    return " ".join(p.text for p in unit.pieces)


def _line_metrics(runs: list[TextRun], n, d) -> tuple[float, float, float, float, float]:
    main = max(runs, key=lambda r: len(r.text or ""))   # not a raised or lowered run
    base = _dot(main.origin, n)
    asc = desc = 0.0
    lo, hi = float("inf"), float("-inf")
    for r in runs:
        try:
            a, de = float(r.font.ascender), float(r.font.descender)
        except Exception:
            a, de = 0.9, -0.25
        asc = max(asc, a * r.size)
        desc = max(desc, -de * r.size)
        s = _dot(r.origin, d)
        w = r.width or (r.font.text_length(r.text, fontsize=r.size) if r.text else 0.0)
        lo, hi = min(lo, s), max(hi, s + w)
    return base, asc, desc, lo, hi


def _typeset(doc, unit: _Unit, edit: Edit | None, n, d, substitutions: list) -> None:
    """Fill ``unit.lines``."""
    if unit.text is None:          # unchanged: the original lines as they are
        lines = []
        prev = None
        pos = 0
        for piece, pg in zip(unit.pieces, unit.pages):
            for i, ln in enumerate(piece.block.lines):
                size = ln.style.size or 10.0
                b = _base(ln, n)
                if prev is None:
                    gap = unit.before
                elif i == 0:
                    gap = _pitch(unit.pieces[0].block.lines, n)   # across a frame break
                else:
                    gap = b - prev
                lines.append(_Line(page=pg, base=b, asc=ln.ascender * size, desc=-ln.descender * size,
                                   lo=ln.extent[0], hi=ln.extent[1], gap=gap, text=ln.text, start=pos, orig=ln,
                                   orig_page=pg))
                pos += len(ln.text) + 1
                prev = b
        unit.lines = lines
        return
    # changed or new: one virtual block (the pieces one under the other on
    # the first piece's page), typeset by text_edit
    if unit.pieces:
        first_page = unit.pages[0]
        vlines = list(unit.pieces[0].block.lines)
        pitch = _pitch(vlines, n)
        for piece in unit.pieces[1:]:
            shift = _base(vlines[-1], n) + pitch - _base(piece.block.lines[0], n)
            vlines.extend(_shift_line(ln, n, shift) for ln in piece.block.lines)
        b0 = unit.pieces[0].block
        vblock = replace(b0, lines=vlines)
        shown = _unit_text(unit)
        soft = []
        off = 0
        for k, piece in enumerate(unit.pieces):
            soft.extend(off + p for p in piece.soft)
            off += len(piece.text)
            if k < len(unit.pieces) - 1:
                soft.append(off)        # the frame break was a wrap too
                off += 1
        text = te.restore_soft_breaks(shown, soft, unit.text)
    else:
        first_page = unit.like_page
        vblock = unit.like.block
        text = unit.text
    page = doc[first_page]
    if not text.strip():
        unit.lines = []
        return
    plan = te._plan(page, vblock, text, edit.style if edit else None, formats=edit.formats if edit else None,
                    align=edit.align if edit else None, line_spacing=edit.line_spacing if edit else None,
                    emit_all=True)
    for sub in plan.substitutions:
        if sub not in substitutions:
            substitutions.append(sub)
    size = vblock.style.size or 10.0
    rows: list[list[TextRun]] = []
    for r in sorted(plan.runs, key=lambda r: _dot(r.origin, n)):
        b = _dot(r.origin, n)
        if rows and b - _dot(rows[-1][0].origin, n) <= 0.6 * size:   # same line (a raised run, a space)
            rows[-1].append(r)
        else:
            rows.append([r])
    plain = unit.text
    lines = []
    prev = None
    pos = 0
    for rs in rows:
        rs.sort(key=lambda r: _dot(r.origin, d))
        base, asc, desc, lo, hi = _line_metrics(rs, n, d)
        txt = "".join(r.text for r in rs if r.text)
        # where this line's text starts in the paragraph text (for the caret)
        probe = txt.strip()[:12]
        k = plain.find(probe, pos) if probe else -1
        start = k if k >= 0 else pos
        pos = start + len(txt.strip())
        gap = unit.before if prev is None else base - prev
        lines.append(_Line(page=first_page, base=base, asc=asc, desc=desc, lo=lo, hi=hi, gap=gap, text=txt,
                           start=start, runs=rs))
        prev = base
    unit.lines = lines


# ---------------------------------------------------------------------------
# layout: lines -> frames
# ---------------------------------------------------------------------------
@dataclass
class _Frame:
    page: int                     # existing page index, or -(k + 1) for the k-th new page
    top: float
    bottom: float
    bands: list                   # (lo, hi along d, top, bottom along n) of what text must avoid
    after: int = -1               # new page: inserted after this existing page


def _frame(doc, page: int, story: Story, info: _PageInfo, area: _Area, n, d, exclude: set) -> _Frame:
    size = story.pieces[0].block.style.size if story.pieces else 10.0
    clear = 0.35 * (size or 10.0)
    bands = []
    for o in info.obstacles:
        a0, a1 = _proj_range(o, d)
        c0, c1 = _proj_range(o, n)
        if min(a1, story.hi) - max(a0, story.lo) <= 0.5:
            continue
        if any((o & r).get_area() > 0.6 * max(o.get_area(), 1e-6) for r in exclude):
            continue            # the bullet of one of our own items
        bands.append((a0, a1, c0 - clear, c1 + clear))
    return _Frame(page=page, top=area.top, bottom=area.bottom, bands=bands)


def _fits(line: _Line, base: float, frame: _Frame) -> tuple[bool, float | None]:
    """(fits, where to try next along n) for a line at ``base``."""
    top, bot = base - line.asc, base + line.desc
    if bot > frame.bottom + 0.05:
        return False, None
    for a0, a1, c0, c1 in frame.bands:
        if min(a1, line.hi) - max(a0, line.lo) <= 0.5:
            continue
        if bot > c0 and top < c1:
            return False, c1 + line.asc
    return True, base



# ---------------------------------------------------------------------------
# apply
# ---------------------------------------------------------------------------
def _befores(doc, story: Story) -> list[float | None]:
    """Baseline distance from the previous paragraph for each piece (None
    for the first one and for continued pieces). Across an image or table
    the paragraph spacing of the column is used instead (the text flows
    around it)."""
    n = _perp(story.dir)
    obstacles = _analyze(doc, story.page).obstacles
    pieces = story.pieces
    raw: list[float | None] = []
    blocked: list[bool] = []
    for i, p in enumerate(pieces):
        if i == 0 or p.cont_prev:
            raw.append(None)
            blocked.append(False)
            continue
        lo_a = _band(pieces[i - 1].block.lines[-1], n)[1]
        hi_b = _band(p.block.lines[0], n)[0]
        between = any(_proj_range(o, n)[0] >= lo_a - 1.0 and _proj_range(o, n)[1] <= hi_b + 1.0
                      and min(_proj_range(o, story.dir)[1], story.hi) - max(_proj_range(o, story.dir)[0], story.lo) > 0
                      for o in obstacles)
        raw.append(_base(p.block.lines[0], n) - _base(pieces[i - 1].block.lines[-1], n))
        blocked.append(between)
    gaps = [g for g, b in zip(raw, blocked) if g is not None and not b]
    typical = statistics.median(gaps) if gaps else None
    out = []
    for i, (g, b, p) in enumerate(zip(raw, blocked, pieces)):
        if (g is not None and b) or (g is None and not p.cont_prev):
            # across an image, or the first paragraph of the page (it only
            # needs a distance when text from the page before ends up above it)
            g = typical if typical is not None else 1.45 * _pitch(p.block.lines, n)
        out.append(g)
    return out


def _paragraph_gap(doc, story: Story, like: Piece) -> float:
    """Spacing for a new paragraph made from ``like`` (Enter): that of
    paragraphs like it in the column (a list keeps its item spacing)."""
    n = _perp(story.dir)
    befores = _befores(doc, story)
    k = story.pieces.index(like) if like in story.pieces else -1
    item = like.bullet is not None or bool(te.marker_end(like.text))
    if item:
        if k + 1 < len(story.pieces) and befores[k + 1] is not None:
            return befores[k + 1]
        if k >= 0 and befores[k] is not None:
            return befores[k]
    size = like.block.style.size or 10.0
    same = [g for g, p in zip(befores, story.pieces)
            if g is not None and abs((p.block.style.size or size) - size) <= 0.1 * size]
    if same:
        return statistics.median(same)
    return 1.45 * _pitch(like.block.lines, n)


def _edit_changed(e: Edit, p: Piece) -> bool:
    return e.text != p.text or bool(e.formats) or e.align is not None or e.line_spacing is not None \
        or e.style is not None


def _story_units(doc, story: Story, edits: list[Edit]) -> list[_Unit]:
    """The edited paragraphs of the story's page as units."""
    pieces = story.pieces
    befores = _befores(doc, story)
    units: list[_Unit] = []
    for k, e in enumerate(edits):
        fixed = bool(e.formats) or e.align is not None or e.line_spacing is not None or e.style is not None
        if e.piece is not None:
            p = pieces[e.piece]
            prev = pieces[e.piece - 1] if e.piece > 0 else None
            if p.cont_prev and prev is not None and units and units[-1].pieces and units[-1].pieces[-1] is prev:
                # the rest of a paragraph split around an image: one unit
                u = units[-1]
                head = u.text if u.text is not None else _unit_text(u)
                u.parts.append((k, len(head) + 1))
                if u.text is not None or _edit_changed(e, p):
                    u.text = head + " " + e.text
                u.fixed = u.fixed or fixed
                u.pieces.append(p)
                u.pages.append(story.page)
                continue
            u = _Unit(pieces=[p], pages=[story.page], edit=k, text=e.text if _edit_changed(e, p) else None,
                      before=befores[e.piece], min_page=story.page, bullet=p.bullet,
                      bullet_page=story.page if p.bullet else None, parts=[(k, 0)], fixed=fixed)
        else:
            like = pieces[e.like if e.like is not None else 0]
            u = _Unit(pieces=[], pages=[], edit=k, text=e.text, like=like, like_page=story.page,
                      before=_paragraph_gap(doc, story, like), min_page=story.page,
                      new_bullet=like.bullet is not None, bullet=like.bullet,
                      bullet_page=story.page if like.bullet else None, parts=[(k, 0)], fixed=True)
        units.append(u)
    return units


class _Chain:
    """The story's column on the following pages, joined as needed."""

    def __init__(self, doc, story: Story, units: list[_Unit]) -> None:
        self.doc = doc
        self.stories = [story]
        self.units = units
        self.infos = {story.page: _analyze(doc, story.page)}
        self.n = _perp(story.dir)
        self.closed = False       # the next page does not take this column's text

    def info(self, page: int) -> _PageInfo:
        if page not in self.infos:
            self.infos[page] = _analyze(self.doc, page)
        return self.infos[page]

    def next_story(self) -> Story | None:
        s = self.stories[-1]
        q = s.page + 1
        if q >= self.doc.page_count or not _compatible(self.doc, s.page, q):
            return None
        columns = sum(1 for x in self.info(s.page).stories if x.kind == "flow" and x.dir == s.dir)
        return _matching(s, self.info(q).stories, columns)

    def continues(self, m: Story) -> bool:
        """Does ``m`` (next page) begin with the rest of our last paragraph?"""
        s = self.stories[-1]
        if not m.pieces or not s.pieces:
            return False
        f = m.pieces[0].block.lines[0]
        if any(abs(x - f.raw_origin[0]) <= 0.6 and abs(y - f.raw_origin[1]) <= 0.6
               for x, y in _markers(self.doc, m.page)):
            return True
        area = _type_area(self.doc, m.page, m, self.info(m.page))
        if _band(f, self.n)[0] > area.top + 2.5 * (f.style.size or 10.0):
            return False
        return _looks_continued(s.pieces[-1].block, m.pieces[0].block, s.lo, s.hi)

    def last_continues(self) -> bool:
        m = self.next_story()
        return m is not None and self.continues(m)

    def extend(self) -> bool:
        """Join the next page's column (its paragraphs follow ours)."""
        if self.closed:
            return False
        m = self.next_story()
        if m is None:
            self.closed = True
            return False
        cont = self.continues(m)
        if not cont and _starts_section(m):
            self.closed = True      # a chapter starts there: new pages go in between
            return False
        befores = _befores(self.doc, m)
        units = self.units
        for idx, p in enumerate(m.pieces):
            joined = (idx == 0 and cont) or (idx > 0 and p.cont_prev)
            if joined and units:
                u = units[-1]
                u.pieces.append(p)
                u.pages.append(m.page)
                if u.text is not None:
                    u.text = u.text + " " + p.text
                continue
            units.append(_Unit(pieces=[p], pages=[m.page], before=befores[idx], min_page=m.page,
                               bullet=p.bullet, bullet_page=m.page if p.bullet else None))
        self.stories.append(m)
        return True


def _place(units: list[_Unit], frames: list[_Frame], seq_of: dict, grow) -> tuple[list, bool]:
    """Lay the units' lines into the frames. ``grow()`` adds a frame (or
    returns False: stop and report overflow). Returns ``(placements,
    overflowed)``; a placement is ``(unit, line, frame, base, jumped)``.
    A paragraph never leaves a single line at the bottom or the top of a
    page (widow / orphan control, as in Word)."""
    placed: list = []
    st = {"fi": 0, "prev": None, "first": True}

    def place_unit(u: _Unit, breaks: set) -> bool:
        for li, ln in enumerate(u.lines):
            fi = st["fi"]
            jumped = False
            if st["first"]:
                base = ln.base                      # the story's first line stays where it is
            elif li in breaks and placed and placed[-1][2] is frames[fi]:
                # kept together with the lines after it: on to the next frame
                if fi + 1 >= len(frames) and not grow():
                    return False
                fi += 1
                base = frames[fi].top + ln.asc
                jumped = True
            elif st["prev"] is None or ln.gap is None:
                base = frames[fi].top + ln.asc
            else:
                base = st["prev"] + ln.gap
            for _guard in range(400):
                ok, nxt = _fits(ln, base, frames[fi])
                if ok:
                    break
                jumped = True
                if nxt is None:                     # past the frame's end: on to the next one
                    if fi + 1 >= len(frames) and not grow():
                        return False
                    fi += 1
                    base = frames[fi].top + ln.asc
                else:
                    base = nxt
            placed.append((u, ln, frames[fi], base, jumped and li > 0))
            st["fi"], st["prev"], st["first"] = fi, base, False
        return True

    for u in units:
        want = seq_of.get(u.min_page, 0)
        while st["fi"] < want and st["fi"] + 1 < len(frames):
            st["fi"] += 1
            st["prev"] = None
        saved = (dict(st), len(placed))
        breaks: set = set()
        tried: list[set] = []
        while True:
            st.update(saved[0])
            del placed[saved[1]:]
            if not place_unit(u, breaks):
                if not tried:
                    return placed, True
                breaks = tried.pop()                # keeping lines together does not fit: as it was
                st.update(saved[0])
                del placed[saved[1]:]
                if not place_unit(u, breaks):
                    return placed, True
                break
            fix = _keep_together(u, placed[saved[1]:], breaks) if len(tried) < 3 else None
            if fix is None:
                break
            tried.append(breaks)
            breaks = fix
    return placed, False


def _keep_together(u: _Unit, rows: list, breaks: set) -> set | None:
    """Widow / orphan control for one placed paragraph: the line indices
    that must start a new frame, or None when it is fine. A paragraph that
    stands exactly where it stood is left as the document has it."""
    n = len(rows)
    if n < 2:
        return None
    if u.text is None and all(ln.orig is not None and fr.page == ln.orig_page and abs(base - ln.base) < 0.05
                              for _u, ln, fr, base, _j in rows):
        return None
    segs: list[list[int]] = [[0]]
    for i in range(1, n):
        if rows[i][2] is rows[i - 1][2]:
            segs[-1].append(i)
        else:
            segs.append([i])
    if len(segs) < 2:
        return None
    if len(segs[0]) == 1 and 0 not in breaks:
        return breaks | {0}                         # orphan: the first line alone at the bottom
    if len(segs[-1]) == 1:                          # widow: the last line alone at the top
        before = segs[-2]
        if len(before) >= 3:
            return breaks | {segs[-1][0] - 1}
        if before[0] == 0 and 0 not in breaks:
            return breaks | {0}                     # two lines + one: the paragraph moves on as a whole
    return None


def apply_story(pdf: PdfDocument, story: Story, edits: list[Edit], *, merge: object = None) -> FlowResult:
    """Write the edited paragraphs of ``story`` (from :func:`page_stories`
    on the current document) and let the column flow. One undo step; edits
    with the same ``merge`` token join one undo step."""
    doc = pdf.doc
    result = FlowResult()
    if story.kind == "single":
        return _apply_single(pdf, story, edits, merge, result)
    d = story.dir
    n = _perp(d)
    units = _story_units(doc, story, edits)
    chain = _Chain(doc, story, units)
    # a paragraph running over the page end is laid out as a whole
    while chain.last_continues() and chain.extend():
        pass
    substitutions: list = []

    def typeset_all() -> None:
        for u in units:
            if not u.lines and (u.pieces or u.text):
                _typeset(doc, u, edits[u.edit] if u.edit is not None else None, n, d, substitutions)

    typeset_all()
    while True:
        frames = []
        seq_of = {}
        for s in chain.stories:
            seq_of[s.page] = len(frames)
            info = chain.info(s.page)
            frames.append(_frame(doc, s.page, s, info, _type_area(doc, s.page, s, info), n, d, set()))
        placed, over = _place(units, frames, seq_of, lambda: False)
        if not over:
            break
        if chain.extend():
            while chain.last_continues() and chain.extend():
                pass
            typeset_all()
            continue
        # no page to flow into: new pages after the last one of the chain
        last = chain.stories[-1].page
        top = min(f.top for f in frames)
        bottom = frames[0].bottom
        made = []

        def grow() -> bool:
            if len(made) >= 50:
                return False
            f = _Frame(page=-(len(made) + 1), top=top, bottom=bottom, bands=[], after=last)
            made.append(f)
            frames.append(f)
            return True

        placed, over = _place(units, frames, seq_of, grow)
        result.overflow = 1.0 if over else 0.0
        break
    result.substitutions = substitutions
    return _write(pdf, story, chain, units, placed, merge, result, n, d)


def _write(pdf, story: Story, chain: _Chain, units: list[_Unit], placed, merge, result: FlowResult, n, d):
    doc = pdf.doc
    removals: dict[int, list[TextLine]] = {}
    writes: dict[int, list[TextRun]] = {}       # page (>= 0 existing, < 0 new) -> runs
    moves: dict[tuple, list[TextLine]] = {}     # (source page, target page, shift) -> unchanged lines
    conts: dict[int, list[tuple[float, float]]] = {}
    touched: set[int] = set()
    rows_of: dict[int, list] = {}
    for u, ln, fr, base, jumped in placed:
        rows_of.setdefault(id(u), []).append((ln, fr, base, jumped))
    infos: list = []                             # (unit, line, frame page, base, runs or None)
    for u in units:
        rows = rows_of.get(id(u), [])
        changed = u.text is not None
        originals = [(ln, pg) for piece, pg in zip(u.pieces, u.pages) for ln in piece.block.lines]
        keep: set[int] = set()
        if changed and not u.fixed:
            # lines that come out exactly as they were stay untouched
            for i, (ln, fr, base, _j) in enumerate(rows):
                if i < len(originals) and ln.runs is not None:
                    o, opg = originals[i]
                    if o.text == ln.text and fr.page == opg and abs(base - _base(o, n)) < 0.05 \
                            and abs(ln.base - _base(o, n)) < 0.05 and _same_runs(ln.runs, o, n, d):
                        keep.add(i)
        if changed:
            for i, (o, opg) in enumerate(originals):
                if i not in keep:
                    removals.setdefault(opg, []).append(o)
                    touched.add(opg)
        first_shift = None
        first_page = None
        for i, (ln, fr, base, jumped) in enumerate(rows):
            if i == 0:
                first_shift, first_page = base - ln.base, fr.page
            if jumped or (i > 0 and fr is not rows[i - 1][1]):
                o = ln.orig.raw_origin if ln.orig is not None else _main_origin(ln.runs)
                s = base - ln.base
                conts.setdefault(fr.page, []).append((o[0] + n[0] * s, o[1] + n[1] * s))
            in_place = fr.page == (ln.orig_page if ln.orig is not None else ln.page) and abs(base - ln.base) < 0.05
            if (not changed and in_place) or i in keep:
                infos.append((u, ln, fr.page, base, None))
                continue
            touched.add(fr.page)
            if not changed:
                removals.setdefault(ln.orig_page, []).append(ln.orig)
                touched.add(ln.orig_page)
                moves.setdefault((ln.orig_page, fr.page, round(base - ln.base, 3)), []).append(ln.orig)
                infos.append((u, ln, fr.page, base, None))       # same glyphs, only moved
                continue
            runs = [_moved(r, n, base - ln.base) for r in ln.runs]
            writes.setdefault(fr.page, []).extend(runs)
            infos.append((u, ln, fr.page, base, runs))
        # a list item's bullet follows its first line
        if u.bullet is not None:
            if u.new_bullet:
                if rows:
                    shift = rows[0][2] - _base(u.like.block.lines[0], n)
                    writes.setdefault(first_page, []).extend(
                        te._moved_runs(doc[u.bullet_page], u.bullet_page, u.bullet.lines, shift))
                    touched.add(first_page)
            elif not rows or abs(first_shift) >= 0.05 or first_page != u.bullet_page:
                removals.setdefault(u.bullet_page, []).extend(u.bullet.lines)
                touched.add(u.bullet_page)
                if rows:
                    moves.setdefault((u.bullet_page, first_page, round(first_shift, 3)), []).extend(u.bullet.lines)
    # unchanged lines that moved: re-typeset glyph for glyph, a group at a time
    for (src, dst, shift), lines in moves.items():
        writes.setdefault(dst, []).extend(te._moved_runs(doc[src], src, lines, shift))
    # paragraphs the edit removed (merged into the one before, or deleted)
    kept = {id(p) for u in units for p in u.pieces}
    for p in story.pieces:
        if id(p) not in kept:
            removals.setdefault(story.page, []).extend(p.block.lines)
            if p.bullet is not None:
                removals[story.page].extend(p.bullet.lines)
            touched.add(story.page)
    if not touched and not writes:
        _record_lines(doc, result, infos, n, d, {})
        return result
    new = sorted({fr.page for _u, _l, fr, _b, _j in placed if fr.page < 0}, reverse=True)
    after = chain.stories[-1].page
    final = {}                                    # frame page -> final page index
    for pg in touched | set(writes):
        if pg >= 0:
            final[pg] = pg + (len(new) if new and pg > after else 0)
    for k in new:
        final[k] = after + (-k)
    label = te._label("core.undo.edit_text", "Redigér tekst")
    pages = tuple(sorted(final.values()))
    structure = bool(new) or any(_is_inserted(doc, s.page) for s in chain.stories)
    with pdf.edit(label, STRUCTURE if structure else CONTENT, None if structure else pages, merge=merge):
        doc = pdf.doc
        for pg in sorted(p for p in final if p >= 0):
            page = doc[pg]
            if removals.get(pg):
                te._remove_lines(page, removals[pg])
            if writes.get(pg):
                te.write_runs(page, writes[pg])
        for s in chain.stories:
            if s.page in final:
                _update_markers(doc, s.page, s, conts.get(s.page, []))
        tmpl = doc[story.page]
        width, height, rotation = tmpl.mediabox.width, tmpl.mediabox.height, tmpl.rotation
        old_count = doc.page_count
        origin: list = list(range(old_count))     # new index -> old index (None: added) for page numbers
        template: dict = {}
        running_lines = [ln for s in chain.info(after).stories if s.role in ("header", "footer")
                         for p in s.pieces for ln in p.block.lines] if new else []
        for order, k in enumerate(sorted(new, reverse=True)):   # -1 first: right after ``after``
            at = after + 1 + order
            page = doc.new_page(pno=at, width=width, height=height)
            if rotation:
                page.set_rotation(rotation)
            # F13: the header and footer of the page before
            covered = running.copy_running(doc, after, at, running_lines)
            if writes.get(k):
                te.write_runs(doc[at], writes[k])
            _set_markers(doc, at, conts.get(k, []), inserted=True, running=covered)
            origin.insert(at, ("new", after))
        removed = _drop_empty_inserted(pdf, [final[s.page] for s in chain.stories if s.page in final])
        for gone in sorted(removed, reverse=True):
            del origin[gone]
        result.removed = tuple(removed)
        if new or removed:
            # F13: page numbers ("Side 13 af 58") on the pages after
            template = {p: o[1] for p, o in enumerate(origin) if isinstance(o, tuple)}
            renumbered = running.renumber(doc, [o if isinstance(o, int) else None for o in origin], old_count,
                                          template=template)
            result.renumbered = tuple(renumbered)
    result.inserted = tuple(final[k] for k in sorted(new, reverse=True))
    for gone in sorted(result.removed, reverse=True):
        final = {k: (v - 1 if v > gone else v) for k, v in final.items()}
    result.pages = tuple(sorted(set(final.values()) - set(result.removed)))
    _record_lines(pdf.doc, result, infos, n, d, final)
    return result


def _same_runs(runs: list[TextRun], o: TextLine, n, d) -> bool:
    """Do these runs draw line ``o`` where it is (same start)?"""
    if not runs:
        return False
    start = min(_dot(r.origin, d) for r in runs if r.text)
    return abs(start - o.extent[0]) < 0.05


def _main_origin(runs: list[TextRun]):
    main = max(runs, key=lambda r: len(r.text or ""))
    first = min(runs, key=lambda r: r.origin[0] + r.origin[1])
    return (first.origin[0], main.origin[1]) if abs(main.origin[1] - first.origin[1]) < 1e-6 else first.origin


def _update_markers(doc, page: int, story: Story, ours: list[tuple[float, float]]) -> None:
    """Replace this column's continuation markers, keep other columns'."""
    d = story.dir
    keep = [(x, y) for x, y in _markers(doc, page) if not story.lo - 1.0 <= _dot((x, y), d) <= story.hi + 1.0]
    _set_markers(doc, page, keep + ours)


def _drop_empty_inserted(pdf, pages: list[int]) -> list[int]:
    """Pages Folio inserted that no longer hold any text are removed."""
    doc = pdf.doc
    gone = []
    for pg in sorted(set(pages), reverse=True):
        if 0 <= pg < doc.page_count and _is_inserted(doc, pg) and doc.page_count > 1 and _only_running(doc, pg):
            doc.delete_page(pg)
            gone.append(pg)
    return sorted(gone)


def _only_running(doc, pg: int) -> bool:
    """Does a page Folio inserted hold nothing but the header / footer it
    copied (no body text, no images of its own)?"""
    page = doc[pg]
    keep = [r + (-1, -1, 1, 1) for r in _running_rects(doc, pg)]

    def ours(r) -> bool:
        r = pymupdf.Rect(r)
        return any(k.contains(r) or (k & r).get_area() >= 0.6 * max(r.get_area(), 1e-6) for k in keep)

    for w in page.get_text("words"):
        if w[4].strip() and not ours(w[:4]):
            return False
    for info in page.get_image_info():
        if not ours(info["bbox"]):
            return False
    return True


def _moved(r: TextRun, n, s: float) -> TextRun:
    return replace(r, origin=(r.origin[0] + n[0] * s, r.origin[1] + n[1] * s))


def _record_lines(doc, result: FlowResult, infos, n, d, final: dict) -> None:
    """Where each edited paragraph's lines are now (for the caret)."""
    for u, ln, pg, base, runs in infos:
        if u.edit is None:
            continue
        page = final.get(pg, pg)
        if ln.orig is not None and runs is None:
            xs = [c[0] for c in ln.orig.char_pos] + [ln.orig.extent[1]] if len(ln.orig.char_pos) == len(ln.text) \
                else None
            text = ln.text
        else:
            xs, _t = _run_positions(runs if runs is not None else ln.runs, d)
            text = ln.text
        rot = doc[page].rotation_matrix if 0 <= page < doc.page_count else pymupdf.Matrix()
        result.lines.setdefault(u.edit, []).append(
            _Placed(page=page, base=base, text=text, start=ln.start, xs=xs, rot=rot, axes=(d, n)))
    for u in {id(x[0]): x[0] for x in infos}.values():
        for edit, offset in u.parts:
            result.parts[edit] = (u.edit, offset)


def _run_positions(runs: list[TextRun], d) -> tuple[list[float] | None, str]:
    text, xs = "", []
    for r in sorted((r for r in runs if r.text), key=lambda r: _dot(r.origin, d)):
        s0 = _dot(r.origin, d)
        acc = 0.0
        for ch in r.text:
            xs.append(s0 + acc)
            try:
                acc += r.font.text_length(ch, fontsize=r.size)
            except Exception:
                acc += 0.5 * r.size
        text += r.text
        end = s0 + acc
    if xs:
        xs.append(end)
        return xs, text
    return None, text


def _point_in_line(pl: _Placed, k: int):
    d, n = pl.axes
    xs = pl.xs
    if xs:
        x = xs[min(k, len(xs) - 1)]
    else:
        x = 0.0
    raw = pymupdf.Point(d[0] * x + n[0] * pl.base, d[1] * x + n[1] * pl.base)
    p = raw * pl.rot
    return (p.x, p.y)


def _apply_single(pdf, story: Story, edits: list[Edit], merge, result: FlowResult) -> FlowResult:
    """A standalone text: edited where it is (it grows in its own place)."""
    piece = story.pieces[0]
    text = "\n".join(e.text for e in edits)
    e0 = edits[0] if edits else Edit(piece=0, text="")
    if text == piece.text and not e0.formats and e0.align is None and e0.line_spacing is None and e0.style is None:
        return result
    full = te.restore_soft_breaks(piece.text, piece.soft, text)
    with pdf.edit(te._label("core.undo.edit_text", "Redigér tekst"), CONTENT, (story.page,), merge=merge):
        # (text below it in the same place moves along; what cannot move is reported as overflow)
        res = te.replace_block_text(pdf, piece.block, full, e0.style, formats=e0.formats, align=e0.align,
                                    line_spacing=e0.line_spacing)
    result.substitutions = list(res.substitutions)
    result.overflow = res.overflow
    result.pages = (story.page,)
    return result


@dataclass
class Limits:
    """Where a flow story's text may go on its own page (display space)."""

    area: pymupdf.Rect                                   # the column within the type area
    obstacles: list = field(default_factory=list)        # rects the text must keep clear of


def column_limits(doc, story: Story) -> Limits:
    """The column of ``story`` on its page: the type area (between header
    and footer) and what text flows around (images, figures, tables,
    other text) – an editor uses it to see when the text must flow on."""
    pg = doc[story.page]
    rot = pg.rotation_matrix
    if story.kind != "flow" or not story.pieces:
        return Limits(area=pymupdf.Rect(story.rect))
    d = story.dir
    n = _perp(d)
    info = _analyze(doc, story.page)
    area = _type_area(doc, story.page, story, info)
    frame = _frame(doc, story.page, story, info, area, n, d, set())

    def display(a0: float, a1: float, c0: float, c1: float) -> pymupdf.Rect:
        pts = [pymupdf.Point(a * d[0] + c * n[0], a * d[1] + c * n[1]) * rot for a in (a0, a1) for c in (c0, c1)]
        return pymupdf.Rect(min(p.x for p in pts), min(p.y for p in pts), max(p.x for p in pts), max(p.y for p in pts))

    return Limits(area=display(story.lo, story.hi, frame.top, frame.bottom),
                  obstacles=[display(a0, a1, c0, c1) for a0, a1, c0, c1 in frame.bands])


def _story_sig(story: Story) -> tuple:
    return (story.page, story.kind, tuple(tuple(round(v, 1) for v in p.block.bbox) for p in story.pieces))


def _text_free_page(doc, story: Story) -> pymupdf.Document:
    """A one-page copy of the story's page without the story's text (and
    its list bullets) – cached for the last few stories, so the editor's
    backdrop is ready when the page was prepared while idle."""
    cache = getattr(doc, "_folio_without", None)
    if cache is None:
        cache = OrderedDict()
        try:
            doc._folio_without = cache
        except Exception:
            pass
    try:
        from .fonts import _page_key
        key = (_page_key(doc[story.page]), _story_sig(story))
    except Exception:
        key = None
    hit = cache.get(key) if key is not None else None
    if hit is not None and not hit.is_closed:
        cache.move_to_end(key)
        return hit
    tmp = pymupdf.open()
    tmp.insert_pdf(doc, from_page=story.page, to_page=story.page, annots=True)
    page = tmp[0]
    lines = [ln for p in story.pieces for ln in p.block.lines]
    lines += [ln for p in story.pieces if p.bullet is not None for ln in p.bullet.lines]
    if lines:
        te._remove_lines(page, lines)
    if key is not None:
        cache[key] = tmp
        while len(cache) > 4:
            _k, old = cache.popitem(last=False)
            try:
                old.close()
            except Exception:
                pass
    return tmp


def prepare_without(doc, story: Story) -> None:
    """Precompute :func:`render_without`'s text-free page (idle time)."""
    _text_free_page(doc, story)


def render_without(doc, story: Story, zoom: float, *, clip=None, alpha: bool = False) -> pymupdf.Pixmap:
    """The story's page rendered without the story's text (and its list
    bullets) – images, drawings, colour and other text stay: the backdrop
    an editor shows its live text on (``clip``: display rect, points)."""
    page = _text_free_page(doc, story)[0]
    kw = {"matrix": pymupdf.Matrix(zoom, zoom), "alpha": alpha, "annots": True}
    if clip is not None:
        kw["clip"] = pymupdf.Rect(clip)
    return page.get_pixmap(**kw)


__all__ = ["Edit", "FlowResult", "Limits", "Piece", "Story", "apply_story", "column_limits", "page_stories",
           "prepare_without", "render_without", "story_at"]
