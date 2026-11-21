"""Text blocks of the page content as movable objects ("Markér objekt").

A *text object* is what one click selects (O1): a paragraph of a column
(a :class:`pdfstudio.core.flow.Piece`, a list item together with its
bullet) or a standalone text – header, footer, table cell, caption, text in
a box or on a figure, rotated text, or text the user moved before (a
``free`` story, O17).

Moving one (O6/O13) uses the text engine of *Redigér tekst*: the block's
lines (and its bullet's) are removed glyph by glyph with a text-only
redaction (:func:`text_edit._remove_lines` – images, vector art and other
text stay) and written again with the document's own fonts, translated by
the move, every glyph at its own position (:func:`_exact_runs`: a new run
wherever the producer kerned or positioned a glyph; the new fonts get their
exact glyph widths, :func:`_exact_widths`, since MuPDF writes them cut off
to whole thousandths of an em). Font, size, colour, kerning and list
bullets move along; underline/strike strokes are drawn again as they were
(same colour, length, thickness). The text stays searchable and editable.
A move keeps the objects on the page (text outside it would be lost).

A moved block is no longer part of the text flow (O17): its raw rect is
stored in the page's Folio markers (``/Free``), and the flow analysis
(:func:`flow._analyze_page`) turns a block starting inside such a rect into
a ``kind="single"``, ``role="free"`` story – an obstacle for the column,
which neither moves it nor pulls it back. The text that was below it stays
where it is. A moved paragraph also loses its continuation markers.

All public coordinates are display space (page rotation applied); every
mutation is one undo step (``merge=True``: a burst of keyboard nudges with
the same label is folded into one step).
"""

from __future__ import annotations

import math
import re
import zlib
from dataclasses import dataclass, field, replace

import pymupdf

from . import flow
from . import text_edit as te
from .document import CONTENT

__all__ = [
    "TextObject",
    "list_text",
    "text_at",
    "text_in",
    "find_text",
    "move_text",
    "delete_text",
    "delete_text_doc",
    "strip_text_except",
    "set_text_width",
    "restack_text",
    "mark_free",
    "mark_free_lines",
]

#: padding (pt) around a moved block's raw rect stored as ``/Free``
FREE_PAD = 1.0
#: how far (pt) a marker / an origin may be off and still count as the same
_SAME = 0.6


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
class TextObject:
    page: int
    rect: pymupdf.Rect                # display bbox incl. a list item's bullet
    lines: list                       # text_edit.TextLine of the block (+ bullet lines, last)
    text: str                         # bullet + " " + the editor text (soft wraps as spaces)
    story_kind: str                   # "flow" | "single"
    role: str                         # flow.Story.role ("body", "header", "free", ...)
    free: bool                        # standalone after a move (O17)
    key: tuple                        # (page, rounded raw origin of the first line, text hash)
    # -- internal ----------------------------------------------------------------
    block: te.TextBlock | None = field(default=None, repr=False, compare=False)
    bullet: te.TextBlock | None = field(default=None, repr=False, compare=False)
    piece: flow.Piece | None = field(default=None, repr=False, compare=False)
    story: flow.Story | None = field(default=None, repr=False, compare=False)

    @property
    def origin(self) -> tuple[float, float]:
        """Raw (unrotated) start of the first baseline of the block."""
        return tuple(self.lines[0].raw_origin) if self.lines else (0.0, 0.0)


def _text_hash(text: str) -> int:
    return zlib.crc32(" ".join(text.split()).encode("utf-8"))


def _make(page: int, story: flow.Story, piece: flow.Piece) -> TextObject:
    block = piece.block
    bullet = piece.bullet
    lines = list(block.lines) + (list(bullet.lines) if bullet is not None else [])
    rect = pymupdf.Rect(block.bbox)
    text = piece.text
    if bullet is not None:
        rect |= bullet.bbox
        text = " ".join(ln.text.strip() for ln in bullet.lines) + " " + text
    o = block.lines[0].raw_origin
    key = (page, (round(o[0], 1), round(o[1], 1)), _text_hash(text))
    return TextObject(page=page, rect=rect, lines=lines, text=text, story_kind=story.kind, role=story.role,
                      free=story.role == "free", key=key, block=block, bullet=bullet, piece=piece, story=story)


# ---------------------------------------------------------------------------
# listing and hit-testing
# ---------------------------------------------------------------------------
def list_text(doc: pymupdf.Document, page: int) -> list[TextObject]:
    """The text objects of a page: one per paragraph of a column (with its
    list bullet) and one per standalone text (header, footer, table cell,
    caption, box, figure text, rotated text, moved text). Cached per page
    content and markers – treat the result as read-only."""
    cache = getattr(doc, "_folio_textobjs", None)
    if cache is None:
        cache = {}
        try:
            doc._folio_textobjs = cache
        except Exception:
            pass
    try:
        from .fonts import _page_key
        key = (page, _page_key(doc[page]), flow._marker_sig(doc, page))
    except Exception:
        key = None
    if key is not None and key in cache:
        return list(cache[key])
    out: list[TextObject] = []
    for story in flow.page_stories(doc, page):
        pieces = story.pieces if story.kind == "flow" else story.pieces[:1]
        for piece in pieces:
            if piece.block.lines:
                out.append(_make(page, story, piece))
    if key is not None:
        if len(cache) > 64:
            cache.clear()
        cache[key] = out
    return list(out)


def text_at(doc: pymupdf.Document, page: int, point, *, tolerance: float = 1.0) -> TextObject | None:
    """The text object with a line box (or its bullet) under the display
    point ``point`` (grown by ``tolerance`` pt); the smallest one wins."""
    if page < 0:
        return None
    x, y = float(point[0]), float(point[1])
    best, best_area = None, None
    for obj in list_text(doc, page):
        r = obj.rect
        if not (r.x0 - tolerance <= x <= r.x1 + tolerance and r.y0 - tolerance <= y <= r.y1 + tolerance):
            continue
        for ln in obj.lines:
            b = ln.bbox
            if b.x0 - tolerance <= x <= b.x1 + tolerance and b.y0 - tolerance <= y <= b.y1 + tolerance:
                area = abs(r.width * r.height)
                if best is None or area < best_area:
                    best, best_area = obj, area
                break
    return best


def text_in(doc: pymupdf.Document, page: int, rect) -> list[TextObject]:
    """The text objects lying completely inside the display rect ``rect``."""
    r = pymupdf.Rect(rect)
    r.normalize()
    out = []
    for obj in list_text(doc, page):
        o = obj.rect
        if o.x0 >= r.x0 - 0.5 and o.y0 >= r.y0 - 0.5 and o.x1 <= r.x1 + 0.5 and o.y1 <= r.y1 + 0.5:
            out.append(obj)
    return out


def _dist(a, b) -> float:
    return math.hypot(float(a[0]) - float(b[0]), float(a[1]) - float(b[1]))


def _find(doc: pymupdf.Document, page: int, origin, text_hash: int | None, *, near: float | None = None):
    """The object of ``page`` starting at the raw ``origin`` (preferring one
    with this text), else the one with this text nearest to it (within
    ``near`` pt when given)."""
    objs = list_text(doc, page)
    at = [o for o in objs if _dist(o.origin, origin) <= _SAME]
    if at:
        same = [o for o in at if text_hash is None or o.key[2] == text_hash]
        return (same or at)[0]
    if text_hash is None:
        return None
    same = [o for o in objs if o.key[2] == text_hash]
    if not same:
        return None
    best = min(same, key=lambda o: _dist(o.origin, origin))
    if near is not None and _dist(best.origin, origin) > near:
        return None
    return best


def find_text(doc: pymupdf.Document, page: int, obj: TextObject) -> TextObject | None:
    """``obj`` on the current document (after edits / undo): the object with
    the same key, else the one starting at the same place, else the one with
    the same text nearest to where it was."""
    if obj is None or page < 0 or page >= doc.page_count:
        return None
    for o in list_text(doc, page):
        if o.key == obj.key:
            return o
    return _find(doc, page, obj.key[1], obj.key[2])


def _resolve(doc: pymupdf.Document, page: int, objs) -> list[TextObject]:
    out: list[TextObject] = []
    seen: set[tuple] = set()
    for obj in objs or ():
        o = find_text(doc, page, obj)
        if o is not None and o.key not in seen:
            seen.add(o.key)
            out.append(o)
    return out


# ---------------------------------------------------------------------------
# markers: /Free rects and continuation markers
# ---------------------------------------------------------------------------
def _raw_delta(pg: pymupdf.Page, dx: float, dy: float) -> tuple[float, float]:
    """A display-space displacement in raw (unrotated) page space."""
    m = pg.derotation_matrix
    return dx * m.a + dy * m.c, dx * m.b + dy * m.d


def _raw_rect(obj: TextObject, pg: pymupdf.Page) -> pymupdf.Rect:
    r = pymupdf.Rect()
    for ln in obj.lines:
        r |= pymupdf.Rect(ln.raw_bbox) if ln.raw_bbox is not None else pymupdf.Rect(ln.bbox) * pg.derotation_matrix
        for d in ln.decor_rects:
            r |= pymupdf.Rect(d)
    return r


def _inside(point, r: pymupdf.Rect) -> bool:
    return r.x0 <= point[0] <= r.x1 and r.y0 <= point[1] <= r.y1


def _new_free(obj: TextObject, pg: pymupdf.Page, rx: float, ry: float) -> flow.FreeBlock:
    """The free-block entry of ``obj`` moved by (rx, ry) raw: its rect and
    the origin and text hash of each of its lines there."""
    rect = _raw_rect(obj, pg) + (rx - FREE_PAD, ry - FREE_PAD, rx + FREE_PAD, ry + FREE_PAD)

    def entry(ln, text):
        return (round(ln.raw_origin[0] + rx, 2), round(ln.raw_origin[1] + ry, 2), flow._line_hash(text))

    lines = [entry(ln, ln.text) for ln in obj.lines]
    # a row the source split into pieces (the words of a loose justified
    # line) may read as one line once rewritten: the whole row counts too
    rows: dict[tuple, list] = {}
    for ln in obj.lines:
        n = te._perp(ln.dir)
        rows.setdefault((round(ln.dir[0], 3), round(ln.dir[1], 3), round(te._dot(ln.raw_origin, n) * 2)), []).append(ln)
    for row in rows.values():
        if len(row) > 1:
            row.sort(key=lambda ln: te._dot(ln.raw_origin, ln.dir))
            e = entry(row[0], "".join(ln.text for ln in row))
            if e not in lines:
                lines.append(e)
    return flow.FreeBlock(rect, tuple(lines))


def _marker_plan(doc: pymupdf.Document, page: int, objs: list[TextObject], new: list):
    """``(free blocks, continuation markers of this page, next page's markers
    or None)`` after ``objs`` left their place (moved: ``new`` holds their
    new free-block entries; deleted: none). Entries none of whose lines is on
    the page any more are dropped (stale)."""
    origins = [o.origin for o in objs]
    old = flow._free_blocks(doc, page)
    gone: set[int] = set()
    for o in objs:
        for ln in o.lines:
            k = flow._free_of(ln, old)
            if k >= 0:
                gone.add(k)                                # replaced by the new place / deleted
    present: set[int] = set()
    if old:
        for b in te.text_blocks(doc, page, merge=False):
            for ln in b.lines:
                k = flow._free_of(ln, old)
                if k >= 0:
                    present.add(k)
    free = [b for k, b in enumerate(old) if k not in gone and k in present] + list(new)
    # continuation markers: the moved piece's own, and the one of the piece
    # its paragraph ran on into (later on this page, or on the next page)
    drop_here = list(origins)
    drop_next = []
    for o in objs:
        if o.story is None or o.piece is None or o.story.kind != "flow":
            continue
        pieces = o.story.pieces
        k = next((i for i, p in enumerate(pieces) if p is o.piece), -1)
        if 0 <= k < len(pieces) - 1:
            if o.piece.cont_next:
                drop_here.append(pieces[k + 1].block.lines[0].raw_origin)
        elif k == len(pieces) - 1 and page + 1 < doc.page_count and flow._markers(doc, page + 1):
            # (only a page that HAS continuation markers needs its columns
            # analysed – most do not, and the analysis is the slow part)
            # the column's last paragraph: a marker at the start of the same
            # column on the next page says it ran on there
            try:
                others = flow.page_stories(doc, page)
                columns = sum(1 for s in others if s.kind == "flow" and s.dir == o.story.dir)
                m = flow._matching(o.story, flow.page_stories(doc, page + 1), columns)
            except Exception:
                m = None
            if m is not None and m.pieces:
                drop_next.append(m.pieces[0].block.lines[0].raw_origin)

    def keep(marks, drop):
        return [(x, y) for x, y in marks if not any(_dist((x, y), p) <= _SAME for p in drop)]

    conts = keep(flow._markers(doc, page), drop_here)
    nxt = None
    if drop_next:
        old = flow._markers(doc, page + 1)
        new = keep(old, drop_next)
        if len(new) != len(old):
            nxt = new
    return free, conts, nxt


def _write_markers(doc: pymupdf.Document, page: int, plan) -> None:
    free, conts, nxt = plan
    if free != flow._free_blocks(doc, page) or conts != flow._markers(doc, page):
        flow._set_free(doc, page, free, conts)
    if nxt is not None:
        flow._set_markers(doc, page + 1, nxt)


def _pages(page: int, plan) -> tuple[int, ...]:
    return (page, page + 1) if plan[2] is not None else (page,)


def _after(doc: pymupdf.Document, page: int, objs: list[TextObject], rx: float = 0.0, ry: float = 0.0):
    """The objects again after an edit (expected ``(rx, ry)`` raw away)."""
    out = []
    for o in objs:
        x, y = o.origin
        hit = _find(doc, page, (x + rx, y + ry), o.key[2], near=max(6.0, o.lines[0].style.size or 6.0))
        if hit is not None:
            out.append(hit)
    return out


# ---------------------------------------------------------------------------
# re-typesetting where the glyphs are
# ---------------------------------------------------------------------------
#: a glyph more than this far (pt) from where the previous glyph's advance
#: puts it starts a new run (kerning, letter spacing, per-glyph positioning)
_EXACT = 0.005


def _exact_runs(pg: pymupdf.Page, page: int, lines: list) -> list:
    """The lines as text runs at exactly the positions their glyphs have
    (raw page space): like :func:`text_edit._moved_runs`, but every glyph
    that does not sit where the previous one's advance ends starts a run of
    its own – a moved block renders as it did, also where the producer
    kerned or positioned glyphs one by one (LibreOffice)."""
    exact = [ln for ln in lines if ln.text and len(ln.char_pos) == len(ln.text)]
    rest = [ln for ln in lines if not (ln.text and len(ln.char_pos) == len(ln.text))]
    runs = te._moved_runs(pg, page, rest, 0.0) if rest else []
    if not exact:
        return runs
    by_key: dict[tuple, object] = {}
    texts: dict[tuple, list[str]] = {}
    for ln in exact:
        for ch, st in zip(ln.text, ln.char_styles or [ln.style] * len(ln.text)):
            k = te._font_key(st)
            by_key.setdefault(k, st)
            texts.setdefault(k, []).append(ch)
    fonts = {k: te.resolve_font(pg.parent, page, by_key[k], "".join(v)).font for k, v in texts.items()}

    def font_of(st):
        return fonts[te._font_key(st)]

    for ln in exact:
        styles = ln.char_styles or [ln.style] * len(ln.text)
        rises = ln.char_rises or [0.0] * len(ln.text)
        rich = list(zip(ln.text, styles, rises))
        em = ln.space_em
        d = ln.dir
        b0 = te._dot(ln.raw_origin, d)
        segs: list[list] = []                 # [first char, end char, start along d]
        end = None
        for i, (ch, st, _r) in enumerate(rich):
            x = ln.char_pos[i][0]
            # a ligature ("ﬀ" read as "ff"): one char has the glyph's width, the
            # other none – the pair is written as two glyphs in one run
            joined = ln.char_pos[i][1] - ln.char_pos[i][0] < 1e-3 or (
                i > 0 and ln.char_pos[i - 1][1] - ln.char_pos[i - 1][0] < 1e-3)
            if end is None or (abs(x - end) > _EXACT and not joined):
                segs.append([i, i + 1, x])
                end = x
            else:
                segs[-1][1] = i + 1
            end += _adv(font_of(st), ch, st.size, em * st.size if em else None)
        line_runs = []
        for a, b, x in segs:
            off = x - b0
            o = (ln.raw_origin[0] + d[0] * off, ln.raw_origin[1] + d[1] * off)
            piece = rich[a:b]
            if not "".join(c[0] for c in piece).strip():
                # a run of spaces only (where words were spread): drawn as
                # space glyphs where they were, or the words would read as one
                st = piece[0][1]
                font = font_of(st)
                if te.has_space(font):
                    line_runs.append(te.TextRun(text=" " * len(piece), font=font, size=st.size,
                                                color=tuple(st.color), origin=o, dir=d,
                                                space=em * st.size if em else None))
                continue
            line_runs.extend(te._emit_line(piece, o, d, em, 0.0, font_of, ln.decor, width=_adv))
        runs.extend(te._bridge_line(line_runs) if len(segs) > 1 else line_runs)
    return runs


_HEX = re.compile(r"<([0-9a-fA-F]+)>")


def _to_unicode(cmap: str) -> dict[int, str]:
    """CID -> text of a ToUnicode CMap (bfchar and bfrange entries)."""
    out: dict[int, str] = {}

    def text(h: str) -> str:
        try:
            return bytes.fromhex(h).decode("utf-16-be")
        except Exception:
            return ""

    for block in re.findall(r"beginbfchar(.*?)endbfchar", cmap, re.S):
        hexes = _HEX.findall(block)
        for a, b in zip(hexes[0::2], hexes[1::2]):
            out[int(a, 16)] = text(b)
    for block in re.findall(r"beginbfrange(.*?)endbfrange", cmap, re.S):
        for line in block.strip().splitlines():
            hexes = _HEX.findall(line)
            if len(hexes) < 3:
                continue
            lo, hi = int(hexes[0], 16), int(hexes[1], 16)
            if "[" in line:
                for k, h in enumerate(hexes[2:]):
                    out[lo + k] = text(h)
            elif hi - lo < 65536:
                t = text(hexes[2])
                for k in range(hi - lo + 1):
                    out[lo + k] = t[:-1] + chr(ord(t[-1]) + k) if t else ""
    return out


def _parse_widths(arr: str) -> dict[int, float]:
    toks = re.findall(r"\[|\]|-?\d+(?:\.\d+)?", arr)
    out: dict[int, float] = {}
    i = 1 if toks and toks[0] == "[" else 0
    while i < len(toks) - 1:
        if toks[i] == "]":
            break
        c = int(float(toks[i]))
        if toks[i + 1] == "[":
            i += 2
            while i < len(toks) and toks[i] != "]":
                out[c] = float(toks[i])
                c += 1
                i += 1
            i += 1
        elif i + 2 < len(toks):
            c2, w = int(float(toks[i + 1])), float(toks[i + 2])
            for k in range(c, min(c2, c + 65535) + 1):
                out[k] = w
            i += 3
        else:
            break
    return out


def _format_widths(widths: dict[int, float]) -> str:
    parts, run, start = [], [], None
    for c in sorted(widths):
        if run and c == start + len(run):
            run.append(widths[c])
            continue
        if run:
            parts.append(f"{start} [" + " ".join(f"{w:g}" for w in run) + "]")
        start, run = c, [widths[c]]
    if run:
        parts.append(f"{start} [" + " ".join(f"{w:g}" for w in run) + "]")
    return "[" + " ".join(parts) + "]"


_UNITS: dict[tuple, int] = {}


def _units(font: pymupdf.Font, ch: str) -> int:
    """The advance of one character in whole thousandths of an em, rounded –
    the /W :func:`_exact_widths` gives the new fonts."""
    key = (id(font), font.name, ch)
    u = _UNITS.get(key)
    if u is None:
        if len(_UNITS) > 50000:
            _UNITS.clear()
        u = _UNITS[key] = int(round(font.text_length(ch, fontsize=1000.0)))
    return u


def _adv(font: pymupdf.Font, text: str, size: float, space: float | None = None) -> float:
    """Advance width of ``text`` as every viewer renders it once written by
    :func:`_write`: the sum of its glyphs' /W (whole thousandths); ``space``
    as in :func:`text_edit.text_width` for a font without a space glyph."""
    text = te.prepare_text(font, text)
    spaced = te.has_space(font)
    total = 0
    extra = 0.0
    for c in text:
        if c == " " and not spaced:
            extra += space if space else 0.25 * size
        else:
            total += _units(font, c)
    return total * size / 1000.0 + extra


def _exact_widths(doc: pymupdf.Document, first_new: int, runs: list) -> None:
    """MuPDF writes the glyph widths (/W) of a font it embeds as whole
    thousandths of an em *cut off*, but places the glyphs with their exact
    advances: every glyph then renders up to 1/1000 em early, which adds up
    along a line. The new fonts get their widths *rounded* (:func:`_units`)
    – not exact decimals: MuPDF itself reads /W as whole numbers, so only
    whole widths render alike in every viewer; the runs are laid out and
    placed with these widths (:func:`_adv`, ``write_runs(advance=…)``)."""
    by_name: dict[str, pymupdf.Font] = {}
    for r in runs:
        try:
            by_name.setdefault(r.font.name.replace(" ", ""), r.font)
        except Exception:
            continue
    for x in range(first_new, doc.xref_length()):
        try:
            if doc.xref_get_key(x, "Subtype")[1] != "/Type0":
                continue
            name = doc.xref_get_key(x, "BaseFont")[1].lstrip("/").replace("#20", "").replace(" ", "")
            font = by_name.get(re.sub(r"^[A-Z]{6}\+", "", name))
            kind, val = doc.xref_get_key(x, "DescendantFonts")
            m = re.search(r"(\d+) 0 R", val or "")
            tu = doc.xref_get_key(x, "ToUnicode")
            if font is None or not m or tu[0] != "xref":
                continue
            cid_font = int(m.group(1))
            wkind, wval = doc.xref_get_key(cid_font, "W")
            if wkind == "xref":
                w_xref = int(wval.split()[0])
                arr = doc.xref_object(w_xref, compressed=True)
            elif wkind == "array":
                w_xref, arr = 0, wval
            else:
                continue
            widths = _parse_widths(arr)
            uni = _to_unicode(doc.xref_stream(int(tu[1].split()[0])).decode("latin-1"))
            changed = False
            for cid, t in uni.items():
                if len(t) != 1 or cid not in widths:
                    continue
                exact = float(_units(font, t))
                if abs(exact - widths[cid]) > 1e-3 and abs(exact - widths[cid]) < 1.0001:
                    widths[cid] = exact
                    changed = True
            if not changed:
                continue
            new = _format_widths(widths)
            if w_xref:
                doc.update_object(w_xref, new)
            else:
                doc.xref_set_key(cid_font, "W", new)
        except Exception:
            continue


def _write(page: pymupdf.Page, runs: list, *, overlay: bool = True) -> None:
    """``text_edit.write_runs`` with exact glyph widths for the new fonts."""
    doc = page.parent
    first_new = doc.xref_length()
    te.write_runs(page, runs, overlay=overlay, advance=_adv)
    _exact_widths(doc, first_new, runs)


# ---------------------------------------------------------------------------
# edits
# ---------------------------------------------------------------------------
def _sig(ln) -> tuple:
    return (round(ln.raw_origin[0], 2), round(ln.raw_origin[1], 2), ln.text)


def _strips(lines: list) -> list[pymupdf.Rect]:
    """What removing ``lines`` redacts (raw): the band through each span
    and the middle of each glyph box (the leftover pass)."""
    out = [pymupdf.Rect(r) for ln in lines for r in te._band_rects(ln)]
    out += [te._center_box(pymupdf.Rect(c), 0.4, 0.35) for ln in lines for c in ln.char_rects]
    return [r for r in out if not r.is_empty]


def _collateral(doc: pymupdf.Document, page: int, lines: list) -> list:
    """Other text lines whose glyphs removing ``lines`` would also hit
    (text overlapping them – a block dropped onto another): they are
    removed too and written again exactly where they are, so overlapping
    text is never damaged."""
    if not lines:
        return []
    own = {_sig(ln) for ln in lines}
    others = [ln for b in te.text_blocks(doc, page, merge=False) for ln in b.lines
              if _sig(ln) not in own and ln.raw_bbox is not None]
    strips = _strips(lines)
    hit: list = []
    while strips and others:
        area = pymupdf.Rect(strips[0])
        for r in strips[1:]:
            area |= r
        found = []
        for ln in others:
            box = pymupdf.Rect(ln.raw_bbox)
            if not box.intersects(area):
                continue
            near = [r for r in strips if r.intersects(box)]
            if near and any(pymupdf.Rect(c).intersects(r) for c in ln.char_rects for r in near):
                found.append(ln)
        if not found:
            break
        hit.extend(found)
        ids = {id(ln) for ln in found}
        others = [ln for ln in others if id(ln) not in ids]
        strips = _strips(found)          # what removing those hits in turn
    # written back in reading order, so text extraction reads them as before
    hit.sort(key=lambda ln: (round(ln.dir[0], 3), round(ln.dir[1], 3),
                             round(te._dot(ln.raw_origin, te._perp(ln.dir)), 1), te._dot(ln.raw_origin, ln.dir)))
    return hit


def _own_decor(lines: list) -> list:
    """The lines with only the strokes that really are their underline /
    strike-through: a stroke reaching more than 2 pt past the line's glyphs
    on either side (an accent rule under a heading) is not the line's and
    neither moves nor goes with it."""
    out = []
    for ln in lines:
        if not ln.decor_rects:
            out.append(ln)
            continue
        lo, hi = ln.extent
        own = [r for r in ln.decor_rects
               if te._proj_range(pymupdf.Rect(r), ln.dir)[0] >= lo - 2.0
               and te._proj_range(pymupdf.Rect(r), ln.dir)[1] <= hi + 2.0]
        if len(own) == len(ln.decor_rects):
            out.append(ln)
        elif own:
            out.append(replace(ln, decor_rects=own))
        else:
            out.append(replace(ln, decor_rects=[], decor={},
                               char_styles=[replace(st, underline=False, strike=False) if (st.underline or st.strike)
                                            else st for st in ln.char_styles]))
    return out


class _Rewrite:
    """Remove some lines and write them again (moved, or in place), with
    the text they overlap kept intact (:func:`_collateral`)."""

    def __init__(self, doc: pymupdf.Document, page: int, lines: list, *, write: bool = True,
                 rx: float = 0.0, ry: float = 0.0):
        pg = doc[page]
        self.lines = _own_decor(lines)
        self.extra = _own_decor(_collateral(doc, page, self.lines))
        self.runs: list = []
        self.paths: list = []
        self.extra_runs: list = []
        self.extra_paths: list = []
        self.rx, self.ry = rx, ry
        if write:
            self.runs, self.paths = self._runs(pg, page, self.lines, rx, ry)
        if self.extra:
            self.extra_runs, self.extra_paths = self._runs(pg, page, self.extra, 0.0, 0.0)

    @staticmethod
    def _runs(pg, page, lines, rx, ry):
        runs = _exact_runs(pg, page, lines)
        if rx or ry:
            runs = [replace(r, origin=(r.origin[0] + rx, r.origin[1] + ry)) for r in runs]
        paths = _decor_paths(pg, lines)
        if paths is not None:
            # the underline / strike strokes move as they are drawn (colour,
            # length, thickness) instead of being drawn anew under each run
            runs = [replace(r, underline=None, strike=None) for r in runs if r.text]
        return runs, paths or []

    def remove(self, p: pymupdf.Page) -> None:
        te._remove_lines(p, self.lines + self.extra)

    def write(self, p: pymupdf.Page, *, overlay: bool = True) -> None:
        if self.runs:
            _write(p, self.runs, overlay=overlay)
        if self.paths:
            _draw_paths(p, self.paths, self.rx, self.ry, overlay=overlay)
        self.write_extra(p)

    def write_extra(self, p: pymupdf.Page) -> None:
        if self.extra_runs:
            _write(p, self.extra_runs)
        if self.extra_paths:
            _draw_paths(p, self.extra_paths, 0.0, 0.0)


def move_text(pdf, page: int, objs, dx: float, dy: float, *, merge: bool = False) -> list[TextObject]:
    """Move text objects by ``(dx, dy)`` display points – one undo step
    (``merge=True`` folds it into the previous step when that is a move of
    text too: a burst of keyboard nudges). The moved blocks become
    standalone (O17); text they overlap stays intact. Returns the moved
    objects as they are now."""
    if not (math.isfinite(dx) and math.isfinite(dy)):
        raise ValueError("bad offset")
    doc = pdf.doc
    targets = _resolve(doc, page, objs)
    if not targets or (abs(dx) < 1e-6 and abs(dy) < 1e-6):
        return targets
    pg = doc[page]
    dx, dy = _keep_on_page(pg, targets, dx, dy)
    if abs(dx) < 1e-6 and abs(dy) < 1e-6:
        return targets
    rx, ry = _raw_delta(pg, dx, dy)
    job = _Rewrite(doc, page, [ln for o in targets for ln in o.lines], rx=rx, ry=ry)
    plan = _marker_plan(doc, page, targets, [_new_free(o, pg, rx, ry) for o in targets])
    label = _label("textobj.undo.move", "Flyt tekst")
    fold = merge and pdf.history.undo_label == label
    with pdf.edit(label, CONTENT, _pages(page, plan)):
        d = pdf.doc
        p = d[page]
        job.remove(p)
        job.write(p)
        _write_markers(d, page, plan)
    if fold:
        pdf.history.discard_last()      # the state before the burst stays the undo target
    return _after(pdf.doc, page, targets, rx, ry)


def _keep_on_page(pg: pymupdf.Page, objs: list[TextObject], dx: float, dy: float) -> tuple[float, float]:
    """The displacement, shortened so the objects stay on the page (text
    outside it is neither shown nor extracted – it would be lost)."""
    box = pymupdf.Rect(objs[0].rect)
    for o in objs[1:]:
        box |= o.rect
    page = pg.rect
    if box.width <= page.width:
        dx = min(max(dx, page.x0 - box.x0), page.x1 - box.x1)
    if box.height <= page.height:
        dy = min(max(dy, page.y0 - box.y0), page.y1 - box.y1)
    return dx, dy


def _decor_paths(pg: pymupdf.Page, lines: list) -> list | None:
    """The drawings of the lines' underline / strike strokes (raw space), or
    None when one of them cannot be found (then they are drawn anew)."""
    rects = [pymupdf.Rect(r) for ln in lines for r in ln.decor_rects]
    if not rects:
        return []
    try:
        drawings = pg.get_drawings()
    except Exception:
        return None
    out = []
    for r in rects:
        hit = None
        for p in drawings:
            items = p.get("items") or ()
            if len(items) != 1 or items[0][0] not in ("l", "re", "qu"):
                continue
            k = items[0][0]
            if k == "l":
                a, b = items[0][1], items[0][2]
                w = float(p.get("width") or 1.0)
                box = pymupdf.Rect(min(a.x, b.x), a.y - w / 2, max(a.x, b.x), a.y + w / 2)
            else:
                box = pymupdf.Rect(items[0][1]) if k == "re" else pymupdf.Quad(items[0][1]).rect
            if max(abs(box.x0 - r.x0), abs(box.y0 - r.y0), abs(box.x1 - r.x1), abs(box.y1 - r.y1)) <= 0.05:
                hit = p
                break
        if hit is None:
            return None
        if not any(x is hit for x in out):
            out.append(hit)
    return out


def _draw_paths(page: pymupdf.Page, paths: list, rx: float, ry: float, *, overlay: bool = True) -> None:
    """Draw stroke / bar drawings again, translated by (rx, ry) raw."""
    shape = page.new_shape()
    v = pymupdf.Point(rx, ry)
    for p in paths:
        kind, *geo = p["items"][0]
        if kind == "l":
            shape.draw_line(geo[0] + v, geo[1] + v)
        elif kind == "re":
            shape.draw_rect(pymupdf.Rect(geo[0]) + (rx, ry, rx, ry))
        else:
            q = pymupdf.Quad(geo[0])
            shape.draw_quad(pymupdf.Quad(q.ul + v, q.ur + v, q.ll + v, q.lr + v))
        cap = p.get("lineCap")
        shape.finish(color=p.get("color"), fill=p.get("fill"), width=float(p.get("width") or 0) or 1.0,
                     closePath=bool(p.get("closePath")) and kind != "l",
                     lineCap=max(cap) if isinstance(cap, (tuple, list)) and cap else int(cap or 0),
                     stroke_opacity=p.get("stroke_opacity") if p.get("stroke_opacity") is not None else 1,
                     fill_opacity=p.get("fill_opacity") if p.get("fill_opacity") is not None else 1)
    shape.commit(overlay=overlay)


def _delete(doc: pymupdf.Document, page: int, targets: list[TextObject]):
    job = _Rewrite(doc, page, [ln for o in targets for ln in o.lines], write=False)
    return job, _marker_plan(doc, page, targets, [])


def delete_text_doc(doc: pymupdf.Document, page: int, objs) -> None:
    """Remove the text objects' glyphs (and their underline/strike strokes)
    without an undo step – for throw-away documents."""
    targets = _resolve(doc, page, objs)
    if not targets:
        return
    job, plan = _delete(doc, page, targets)
    job.remove(doc[page])
    job.write(doc[page])
    _write_markers(doc, page, plan)


def delete_text(pdf, page: int, objs) -> None:
    """Delete text objects (one undo step). Only their glyphs go: images,
    vector art and other text (also text they overlap) stay; the text below
    does not move up."""
    doc = pdf.doc
    targets = _resolve(doc, page, objs)
    if not targets:
        return
    job, plan = _delete(doc, page, targets)
    with pdf.edit(_label("textobj.undo.delete", "Slet tekst"), CONTENT, _pages(page, plan)):
        d = pdf.doc
        job.remove(d[page])
        job.write(d[page])
        _write_markers(d, page, plan)


def strip_text_except(doc: pymupdf.Document, page: int, keep) -> None:
    """Remove every text line of the page that does not belong to one of
    the ``keep`` objects (matched by raw position and text, so objects
    listed on another copy of the same page work) – for the clipboard."""
    sig = set()
    for o in keep or ():
        for ln in o.lines:
            sig.add((round(ln.raw_origin[0], 1), round(ln.raw_origin[1], 1), ln.text))
    lines = [ln for b in te.text_blocks(doc, page, merge=False) for ln in b.lines]
    gone = [ln for ln in lines if (round(ln.raw_origin[0], 1), round(ln.raw_origin[1], 1), ln.text) not in sig]
    if gone:
        te._remove_lines(doc[page], gone)
        # free entries keep only blocks that are still there
        free = flow._free_blocks(doc, page)
        if free:
            removed = {id(ln) for ln in gone}
            kept = {flow._free_of(ln, free) for ln in lines if id(ln) not in removed}
            flow._set_free(doc, page, [b for k, b in enumerate(free) if k in kept])


def mark_free(doc: pymupdf.Document, page: int, objs) -> None:
    """Record text objects as standalone (O17) where they are – e.g. text
    just pasted onto the page: each gets its free entry (rect + its lines'
    origins and text), replacing any entry that held one of its lines.
    Document level, no undo step of its own (call it inside ``pdf.edit``)."""
    targets = _resolve(doc, page, objs)
    if targets:
        mark_free_lines(doc, page, [o.lines for o in targets])


def mark_free_lines(doc: pymupdf.Document, page: int, groups) -> None:
    """Like :func:`mark_free` for groups of text lines (``TextLine`` of
    :func:`list_text` objects): each group becomes ONE standalone object –
    also lines that extraction merged into a block with other text (a copy
    pasted right under its original): the free entry takes them out of it."""
    groups = [list(g) for g in groups if g]
    if not groups:
        return
    pg = doc[page]
    free = flow._free_blocks(doc, page)
    gone = {flow._free_of(ln, free) for g in groups for ln in g} - {-1}
    entries = [_new_free(_LinesOnly(g), pg, 0.0, 0.0) for g in groups]
    flow._set_free(doc, page, [b for k, b in enumerate(free) if k not in gone] + entries)


class _LinesOnly:
    """Just the ``lines`` of a would-be text object (for :func:`_new_free`)."""

    def __init__(self, lines: list) -> None:
        self.lines = lines


def set_text_width(pdf, page: int, obj: TextObject, width: float) -> TextObject:
    """Wrap a text object anew at ``width`` – display points along its text
    direction, its bullet included (O9): the start edge stays, the text gets
    more or fewer lines in the same fonts, sizes and colours and keeps its
    alignment. Text of the column below moves along (as with *Redigér
    tekst*); a standalone (moved) block grows in place."""
    if not math.isfinite(float(width)) or width <= 0:
        raise ValueError("bad width")
    doc = pdf.doc
    found = find_text(doc, page, obj)
    if found is None:
        raise LookupError("text object not found")
    obj = found
    block = obj.block
    first = block.lines[0]
    start = min(ln.extent[0] for ln in obj.lines if te._same_dir(ln.dir, first.dir))
    lmin = min(ln.extent[0] for ln in block.lines)
    size = first.style.size or 10.0
    wrap = max(float(width) - (lmin - start), 2.0 * size)
    current = max(ln.extent[1] for ln in obj.lines) - start
    if abs(float(width) - current) < 0.25:
        return obj
    text = _wrap_text(obj)
    label = _label("textobj.undo.width", "Skift tekstbredde")
    # text overlapping the block is taken off first and written back after
    job = _Rewrite(doc, page, list(block.lines), write=False)
    refresh = None
    if obj.free:
        # a moved block grows in place; its free entry follows its new lines
        pg = doc[page]
        plan = te._plan(pg, block, text, None, width=wrap)
        rect = flow._runs_rect(plan.runs)
        for ln in (obj.bullet.lines if obj.bullet is not None else ()):
            rect = pymupdf.Rect(ln.raw_bbox) if rect.is_empty else rect | ln.raw_bbox
        blocks = flow._free_blocks(doc, page)
        k = flow._free_of(first, blocks)
        old = blocks[k] if k >= 0 else None
        if not rect.is_empty:
            refresh = (old, plan.runs + _exact_runs(pg, page, obj.bullet.lines if obj.bullet is not None else []),
                       rect + (-FREE_PAD, -FREE_PAD, FREE_PAD, FREE_PAD))
    with pdf.edit(label, CONTENT, (page,)):
        if job.extra:
            te._remove_lines(pdf.doc[page], job.extra)
        te.replace_block_text(pdf, block, text, width=wrap, reflow=obj.story_kind == "flow" and not obj.free,
                              label=label)
        job.write_extra(pdf.doc[page])
        if refresh is not None:
            flow._refresh_free(pdf.doc, page, *refresh)
    return _find(pdf.doc, page, obj.origin, None) or obj


def _first_word(ln) -> float:
    """Advance width (pt) of the first word of a line."""
    body = ln.text.lstrip()
    word = body.split(" ", 1)[0] if body else ""
    if not word:
        return 0.0
    if len(ln.char_pos) == len(ln.text):
        i0 = len(ln.text) - len(body)
        return max(0.0, ln.char_pos[i0 + len(word) - 1][1] - ln.char_pos[i0][0])
    return (ln.extent[1] - ln.extent[0]) * len(word) / max(len(ln.text), 1)


def _wrap_text(obj: TextObject) -> str:
    """The block's text for re-wrapping: lines that merely wrapped are
    joined with a space, real line breaks stay "\n". A wrap is what the
    text engine reads as one, or a line the next line's first word would
    not have fitted on within the block's own width (a block narrowed
    before is not judged by the column around it)."""
    lines = obj.block.lines
    soft = set(obj.piece.layout.soft_breaks) if obj.piece is not None and obj.piece.layout is not None else set()
    if len(lines) >= 2:
        rmax = max(ln.extent[1] for ln in lines)
        starts = sorted(ln.extent[0] for ln in lines[1:])
        cont = starts[0] if starts[-1] - starts[0] <= 1.0 else None
        for i, (a, b) in enumerate(zip(lines, lines[1:])):
            if i in soft or cont is None or not te._same_dir(a.dir, b.dir) or abs(b.extent[0] - cont) > 1.0:
                continue
            size = a.style.size or 10.0
            if a.text.strip() and a.extent[1] + 0.25 * size + _first_word(b) > rmax - 0.5:
                soft.add(i)
    parts = []
    for i, ln in enumerate(lines):
        parts.append(ln.text)
        if i < len(lines) - 1:
            parts.append(" " if i in soft else "\n")
    return "".join(parts)


def restack_text(pdf, page: int, objs, *, front: bool = True) -> list[TextObject]:
    """Draw text objects on top of everything else on the page (``front``)
    or behind it (O12) – same place, same glyphs; one undo step."""
    doc = pdf.doc
    targets = _resolve(doc, page, objs)
    if not targets:
        return targets
    job = _Rewrite(doc, page, [ln for o in targets for ln in o.lines])
    label = _label("textobj.undo.front", "Læg tekst forrest") if front else \
        _label("textobj.undo.back", "Læg tekst bagerst")
    with pdf.edit(label, CONTENT, (page,)):
        p = pdf.doc[page]
        job.remove(p)
        job.write(p, overlay=front)
    return _after(pdf.doc, page, targets)
