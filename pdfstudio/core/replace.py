"""Search and replace text in the document ("Søg og erstat", Ctrl+H).

Matches are found in the editable text blocks of
:mod:`pdfstudio.core.text_edit` (a phrase may continue on the next line of
a paragraph: spaces in the query match any white space, line breaks
included). Replacing rewrites the block with
:func:`~pdfstudio.core.text_edit.replace_block_text`, so the new text gets
the font, size and colour of the text it replaces and the paragraph
re-wraps. "Erstat alle" is one undo step.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

import pymupdf

from ..i18n import tr
from . import text_edit as te
from .document import CONTENT, PdfDocument
from .render import DocLike, as_document


@dataclass
class Match:
    page: int
    block: int            # index into text_edit.text_blocks(doc, page)
    start: int            # offsets into that block's text
    end: int
    text: str
    rects: list[pymupdf.Rect] = field(default_factory=list)   # display space


def pattern(query: str, *, case_sensitive: bool = False, whole_words: bool = False) -> re.Pattern | None:
    words = (query or "").split()
    if not words:
        return None
    body = r"\s+".join(re.escape(w) for w in words)
    if whole_words:
        body = rf"(?<!\w){body}(?!\w)"
    return re.compile(body, 0 if case_sensitive else re.IGNORECASE)


def _offset_rects(page: pymupdf.Page, block: te.TextBlock, a: int, b: int) -> list[pymupdf.Rect]:
    """Display rects of the characters ``[a, b)`` of the block's text."""
    out = []
    pos = 0
    rm = page.rotation_matrix
    for ln in block.lines:
        lo, hi = max(a, pos), min(b, pos + len(ln.text))
        if lo < hi:
            k0, k1 = lo - pos, hi - pos
            raw = pymupdf.Rect(ln.raw_bbox) if ln.raw_bbox is not None else None
            if raw is not None and len(ln.char_pos) == len(ln.text) and abs(ln.dir[1]) < 1e-3:
                s0, s1 = ln.char_pos[k0][0], ln.char_pos[k1 - 1][1]
                if ln.dir[0] < 0:
                    s0, s1 = -s1, -s0
                raw = pymupdf.Rect(s0, raw.y0, s1, raw.y1)
            rect = (raw * rm) if raw is not None else pymupdf.Rect(ln.bbox)
            out.append(rect.normalize())
        pos += len(ln.text) + 1
    return out


def find_matches(doc: DocLike, query: str, *, case_sensitive: bool = False, whole_words: bool = False,
                 pages=None) -> list[Match]:
    """Every occurrence of ``query`` in reading order."""
    pat = pattern(query, case_sensitive=case_sensitive, whole_words=whole_words)
    if pat is None:
        return []
    d = as_document(doc)
    out: list[Match] = []
    for p in (range(d.page_count) if pages is None else pages):
        try:
            blocks = te.text_blocks(d, p)
        except Exception:
            continue
        page = d[p]
        for i, blk in enumerate(blocks):
            if te.is_list_bullet(blk):
                continue
            for m in pat.finditer(blk.text):
                out.append(Match(p, i, m.start(), m.end(), m.group(0), _offset_rects(page, blk, m.start(), m.end())))
    return out


def _replaced(text: str, spans: list[tuple[int, int]], replacement: str) -> str:
    for a, b in sorted(spans, reverse=True):
        text = text[:a] + replacement + text[b:]
    return text


def replace_matches(pdf: PdfDocument, matches: list[Match], replacement: str) -> int:
    """Replace the given matches (all from :func:`find_matches` on the
    current document) – one undo step. Returns how many were replaced."""
    if not matches:
        return 0
    by_block: dict[tuple[int, int], list[tuple[int, int]]] = {}
    for m in matches:
        by_block.setdefault((m.page, m.block), []).append((m.start, m.end))
    pages = tuple(sorted({p for p, _b in by_block}))
    count = 0
    label = tr("core.undo.replace_one") if len(matches) == 1 else tr("core.undo.replace_all")
    with pdf.edit(label, CONTENT, pages):
        for p in pages:
            blocks = te.text_blocks(pdf.doc, p)
            # bottom to top: an edit only moves the text below it
            todo = sorted((i for (pp, i) in by_block if pp == p),
                          key=lambda i: (blocks[i].bbox.y0, blocks[i].bbox.x0), reverse=True)
            for i in todo:
                blk = blocks[i]
                spans = by_block[(p, i)]
                te.replace_block_text(pdf, blk, _replaced(blk.text, spans, replacement))
                count += len(spans)
    return count


def replace_all(pdf: PdfDocument, query: str, replacement: str, *, case_sensitive: bool = False,
                whole_words: bool = False, pages=None) -> int:
    """Replace every occurrence – one undo step. Returns the count."""
    return replace_matches(pdf, find_matches(pdf.doc, query, case_sensitive=case_sensitive,
                                             whole_words=whole_words, pages=pages), replacement)


__all__ = ["Match", "find_matches", "pattern", "replace_all", "replace_matches"]
