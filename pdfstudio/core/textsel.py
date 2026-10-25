"""Character-precise text selection (Acrobat / Word style).

:func:`page_text` extracts a page's characters in reading order (MuPDF's
order: the order the text was written, which is the logical order for
Word, LibreOffice and most generators) with their display-space boxes.
A *caret* is an index ``0..len(chars)`` between characters, so selecting
from the middle of one word to the middle of another selects exactly those
characters. Lines of any direction (rotated pages, vertical labels) are
handled by projecting onto the line's own direction.

Everything is Qt-free; the UI keeps ``(page, caret)`` pairs for anchor and
focus and asks this module for the hit caret, the highlighted rects and the
text. Used by the text selection tool, the markup tools, comments (quotes
of marked text) and search & replace.
"""

from __future__ import annotations

import unicodedata
from dataclasses import dataclass, field
from typing import Iterable, Sequence

import pymupdf

from .render import DocLike, PointLike, RectLike, as_document

_FLAGS = (pymupdf.TEXT_PRESERVE_WHITESPACE | pymupdf.TEXT_PRESERVE_LIGATURES
          | pymupdf.TEXT_MEDIABOX_CLIP)
#: extra reach (pt) around a line's band that still counts as "on the line"
LINE_SLACK = 2.0
_WORD_JOINERS = "-'’_"


@dataclass(frozen=True)
class Glyph:
    """One character: its text and display-space box."""

    c: str
    rect: pymupdf.Rect
    line: int    # index into PageText.lines
    block: int   # MuPDF text block number (paragraph-ish)


@dataclass
class Line:
    start: int                  # first char index
    end: int                    # one past the last char
    dir: tuple[float, float]    # unit writing direction (display space)
    block: int
    lo: float = 0.0             # band across the writing direction
    hi: float = 0.0
    a0: float = 0.0             # extent along the writing direction
    a1: float = 0.0


@dataclass
class PageText:
    page: int
    chars: list[Glyph] = field(default_factory=list)
    lines: list[Line] = field(default_factory=list)

    def __len__(self) -> int:
        return len(self.chars)

    @property
    def text(self) -> str:
        return self.text_between(0, len(self.chars))

    def text_between(self, a: int, b: int, *, line_sep: str = "\n") -> str:
        """Text of chars ``[a, b)``. Lines are joined by ``line_sep``
        (paragraphs always by a newline); a line-end hyphen is kept."""
        a, b = _order(a, b)
        a, b = max(0, a), min(len(self.chars), b)
        out: list[str] = []
        prev: Glyph | None = None
        for g in self.chars[a:b]:
            if prev is not None and g.line != prev.line:
                sep = "\n" if g.block != prev.block else line_sep
                if out and out[-1].endswith(" ") and sep == " ":
                    sep = ""
                out.append(sep)
            out.append(g.c)
            prev = g
        return "".join(out)


def _order(a: int, b: int) -> tuple[int, int]:
    return (a, b) if a <= b else (b, a)


def _dot(p, d) -> float:
    return p[0] * d[0] + p[1] * d[1]


def _perp(d) -> tuple[float, float]:
    return (-d[1], d[0])


def _proj(rect: pymupdf.Rect, d) -> tuple[float, float]:
    vals = [_dot(c, d) for c in (rect.tl, rect.tr, rect.bl, rect.br)]
    return min(vals), max(vals)


def page_text(doc: DocLike, page: int) -> PageText:
    """All characters of ``page`` in reading order (display space)."""
    pg = as_document(doc)[page]
    mat = pg.rotation_matrix
    out = PageText(page)
    try:
        raw = pg.get_text("rawdict", flags=_FLAGS)
    except Exception:  # pragma: no cover - damaged content streams
        return out
    for bno, block in enumerate(raw.get("blocks", [])):
        if block.get("type", 0) != 0:
            continue
        for line in block.get("lines", []):
            dx, dy = line.get("dir", (1.0, 0.0))
            d = pymupdf.Point(dx, dy) * mat - pymupdf.Point(0, 0) * mat
            n = abs(d) or 1.0
            ddir = (d.x / n, d.y / n)
            chars = [ch for span in line.get("spans", []) for ch in span.get("chars", [])]
            if not chars:
                continue
            ln = Line(len(out.chars), len(out.chars), ddir, bno)
            idx = len(out.lines)
            for ch in chars:
                c = ch.get("c", "")
                if not c or c in "\r\n":
                    continue
                r = (pymupdf.Rect(ch["bbox"]) * mat).normalize()
                if r.is_empty:  # zero-width glyphs (spaces in some fonts): keep a sliver
                    lb = (pymupdf.Rect(line["bbox"]) * mat).normalize()
                    o = pymupdf.Point(ch["origin"]) * mat
                    r = pymupdf.Rect(o.x, lb.y0, o.x + 0.01, lb.y1) if abs(ddir[0]) >= abs(ddir[1]) \
                        else pymupdf.Rect(lb.x0, o.y, lb.x1, o.y + 0.01)
                out.chars.append(Glyph(c, r, idx, bno))
            ln.end = len(out.chars)
            if ln.end == ln.start:
                continue
            # drop trailing whitespace from the line's measured extent only
            p = _perp(ddir)
            boxes = [g.rect for g in out.chars[ln.start:ln.end]]
            across = [_proj(r, p) for r in boxes]
            along = [_proj(r, ddir) for r in boxes]
            ln.lo, ln.hi = min(a for a, _ in across), max(b for _, b in across)
            ln.a0, ln.a1 = min(a for a, _ in along), max(b for _, b in along)
            out.lines.append(ln)
    return out


# -- hit testing -------------------------------------------------------------------
def line_at(pt: PageText, point: PointLike) -> int:
    """Index of the line closest to ``point`` (-1 if the page has no text).
    A point inside a line's band wins; otherwise the nearest band, with the
    distance along the line counting less (so the margin next to a line
    still picks that line rather than a neighbouring column)."""
    best, best_d = -1, float("inf")
    for i, ln in enumerate(pt.lines):
        p = _perp(ln.dir)
        across = _dot(point, p)
        along = _dot(point, ln.dir)
        da = 0.0 if ln.lo - LINE_SLACK <= across <= ln.hi + LINE_SLACK else min(abs(across - ln.lo), abs(across - ln.hi))
        dl = 0.0 if ln.a0 <= along <= ln.a1 else min(abs(along - ln.a0), abs(along - ln.a1))
        d = da * 4.0 + dl
        if d < best_d:
            best, best_d = i, d
    return best


def caret_in_line(pt: PageText, line: int, point: PointLike) -> int:
    """Caret index within ``line`` nearest to ``point``."""
    ln = pt.lines[line]
    along = _dot(point, ln.dir)
    for i in range(ln.start, ln.end):
        a, b = _proj(pt.chars[i].rect, ln.dir)
        if along < (a + b) / 2:
            return i
    return ln.end


def hit(pt: PageText, point: PointLike) -> int | None:
    """Caret index nearest to ``point`` (``None`` for a page without text)."""
    line = line_at(pt, point)
    if line < 0:
        return None
    return caret_in_line(pt, line, point)


def char_at(pt: PageText, point: PointLike, pad: float = 1.0) -> int | None:
    """Index of the character whose box contains ``point`` (for the I-beam
    cursor); ``None`` when not over text."""
    x, y = point[0], point[1]
    for i, g in enumerate(pt.chars):
        r = g.rect
        if r.x0 - pad <= x <= r.x1 + pad and r.y0 - pad <= y <= r.y1 + pad:
            return i
    return None


# -- units ---------------------------------------------------------------------------
def _is_word_char(c: str) -> bool:
    if c in _WORD_JOINERS:
        return True
    cat = unicodedata.category(c)
    return cat[0] in "LN" or cat == "Mn"


def word_bounds(pt: PageText, index: int) -> tuple[int, int]:
    """``[a, b)`` of the word around char ``index`` (punctuation and spaces
    are words of one char, as in Word)."""
    n = len(pt.chars)
    if n == 0:
        return 0, 0
    index = max(0, min(n - 1, index))
    g = pt.chars[index]
    if not _is_word_char(g.c):
        return index, index + 1
    ln = pt.lines[g.line]
    a = index
    while a > ln.start and _is_word_char(pt.chars[a - 1].c):
        a -= 1
    b = index + 1
    while b < ln.end and _is_word_char(pt.chars[b].c):
        b += 1
    # a word hyphenated across lines continues on the next line
    if b == ln.end and pt.chars[b - 1].c == "-" and g.line + 1 < len(pt.lines):
        nxt = pt.lines[g.line + 1]
        if nxt.block == ln.block:
            while b < nxt.end and _is_word_char(pt.chars[b].c) and pt.chars[b].c != "-":
                b += 1
    return a, b


def line_bounds(pt: PageText, index: int) -> tuple[int, int]:
    if not pt.chars:
        return 0, 0
    ln = pt.lines[pt.chars[max(0, min(len(pt.chars) - 1, index))].line]
    return ln.start, ln.end


def paragraph_bounds(pt: PageText, index: int) -> tuple[int, int]:
    """``[a, b)`` of the text block (paragraph) around char ``index``."""
    if not pt.chars:
        return 0, 0
    block = pt.chars[max(0, min(len(pt.chars) - 1, index))].block
    idx = [i for i, g in enumerate(pt.chars) if g.block == block]
    return idx[0], idx[-1] + 1


# -- results -------------------------------------------------------------------------
def selection_rects(pt: PageText, a: int, b: int) -> list[pymupdf.Rect]:
    """One display rect per line for chars ``[a, b)`` – leading/trailing
    whitespace of each line piece is left out (like Acrobat)."""
    a, b = _order(a, b)
    rects: list[pymupdf.Rect] = []
    for ln in pt.lines:
        lo, hi = max(a, ln.start), min(b, ln.end)
        if lo >= hi:
            continue
        while lo < hi and pt.chars[lo].c.isspace():
            lo += 1
        while hi > lo and pt.chars[hi - 1].c.isspace():
            hi -= 1
        if lo >= hi:
            continue
        r = pymupdf.Rect(pt.chars[lo].rect)
        for g in pt.chars[lo + 1:hi]:
            r |= g.rect
        rects.append(r)
    return rects


def chars_in_rects(pt: PageText, rects: Iterable[RectLike]) -> list[int]:
    """Indices of chars whose centre lies inside one of ``rects`` (display)."""
    boxes = [pymupdf.Rect(r) for r in rects]
    out = []
    for i, g in enumerate(pt.chars):
        c = pymupdf.Point((g.rect.x0 + g.rect.x1) / 2, (g.rect.y0 + g.rect.y1) / 2)
        if any(c in r for r in boxes):
            out.append(i)
    return out


def text_in_rects(pt: PageText, rects: Sequence[RectLike]) -> str:
    """The text covered by ``rects`` (e.g. a highlight's quads), lines joined
    by spaces – the quote shown with a comment."""
    idx = chars_in_rects(pt, rects)
    if not idx:
        return ""
    parts: list[str] = []
    prev = None
    for i in idx:
        g = pt.chars[i]
        if prev is not None and (i != prev + 1 or g.line != pt.chars[prev].line):
            if parts and not parts[-1].endswith((" ", "-")):
                parts.append(" ")
            elif parts and parts[-1].endswith("-") and g.line != pt.chars[prev].line:
                parts[-1] = parts[-1][:-1]  # re-join a word hyphenated at line end
        parts.append(g.c)
        prev = i
    return " ".join("".join(parts).split())


__all__ = [
    "Glyph",
    "Line",
    "PageText",
    "caret_in_line",
    "char_at",
    "chars_in_rects",
    "hit",
    "line_at",
    "line_bounds",
    "page_text",
    "paragraph_bounds",
    "selection_rects",
    "text_in_rects",
    "word_bounds",
]
