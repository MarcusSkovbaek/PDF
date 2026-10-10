"""Full-text search.

PyMuPDF's ``search_for`` is always case-insensitive and returns a flat list
of rectangles (a hit spanning two lines cannot be told apart from two hits).
We therefore search our own per-page character model built from
``rawdict`` extraction:

* each text line contributes its characters (with their bounding boxes),
* lines of one block are joined by a space, blocks by a newline, so a phrase
  can match across a line break but never across unrelated blocks,
* runs of whitespace are collapsed, so ``"Anden  linje"`` and a query with a
  single space match.

Hit rectangles (one per line touched) are converted to display space.
Pages are processed lazily, so the UI can show the first hits immediately
and stop iterating at any time.
"""

from __future__ import annotations

import unicodedata
from dataclasses import dataclass
from typing import Iterator

import pymupdf

from .render import DocLike, as_document

#: Characters of context shown on each side of a hit.
CONTEXT_CHARS = 40

_TEXT_FLAGS = (
    pymupdf.TEXT_PRESERVE_WHITESPACE
    | pymupdf.TEXT_MEDIABOX_CLIP
    | pymupdf.TEXT_CID_FOR_UNKNOWN_UNICODE
)  # ligatures are deliberately *not* preserved: "ﬁ" is expanded to "fi"


@dataclass
class SearchHit:
    """One occurrence: ``rects`` (display coords, one per line) and a short
    single-line ``context`` snippet around the match."""

    page: int
    rects: list[pymupdf.Rect]
    context: str


@dataclass
class _PageText:
    text: str                                   # normalized page text
    boxes: list[tuple[int, pymupdf.Rect] | None]  # per char: (line id, unrotated bbox)


def _is_space(ch: str) -> bool:
    return ch.isspace() or unicodedata.category(ch) == "Zs"


def _normalize_query(query: str) -> str:
    return " ".join(query.split())


def _fold(text: str) -> str:
    """Lower-case char by char so indices stay aligned with ``text``."""
    out = []
    for ch in text:
        low = ch.lower()
        out.append(low if len(low) == 1 else ch)
    return "".join(out)


def _page_text(page: pymupdf.Page, textpage: pymupdf.TextPage) -> _PageText:
    chars: list[str] = []
    boxes: list[tuple[int, pymupdf.Rect] | None] = []

    def add_space(sep: str) -> None:
        if not chars:
            return
        if chars[-1] in " \n":
            if sep == "\n":
                chars[-1] = "\n"
            return
        chars.append(sep)
        boxes.append(None)

    line_id = 0
    for block in textpage.extractRAWDICT()["blocks"]:
        if block.get("type", 0) != 0:
            continue
        add_space("\n")
        for line in block["lines"]:
            add_space(" ")
            line_id += 1
            for span in line["spans"]:
                for char in span["chars"]:
                    c = char["c"]
                    if _is_space(c):
                        add_space(" ")
                    else:
                        chars.append(c)
                        boxes.append((line_id, pymupdf.Rect(char["bbox"])))
    return _PageText("".join(chars), boxes)


def _is_word_char(ch: str) -> bool:
    return ch.isalnum() or ch == "_"


def _context(text: str, start: int, end: int) -> str:
    """Up to ``CONTEXT_CHARS`` around the match, within its text block,
    without cutting words, with ellipses where text was left out."""
    block_start = text.rfind("\n", 0, start) + 1
    block_end = text.find("\n", end)
    if block_end == -1:
        block_end = len(text)
    left = max(block_start, start - CONTEXT_CHARS)
    right = min(block_end, end + CONTEXT_CHARS)
    if left > block_start:
        cut = text.find(" ", left, start)
        left = cut + 1 if cut != -1 else left
    if right < block_end:
        cut = text.rfind(" ", end, right)
        right = cut if cut != -1 else right
    snippet = text[left:right].strip()
    return ("…" if left > block_start else "") + snippet + ("…" if right < block_end else "")


def _hit_rects(page: pymupdf.Page, model: _PageText, start: int, end: int) -> list[pymupdf.Rect]:
    per_line: dict[int, pymupdf.Rect] = {}
    for entry in model.boxes[start:end]:
        if entry is None:
            continue
        line_id, bbox = entry
        if line_id in per_line:
            per_line[line_id] |= bbox
        else:
            per_line[line_id] = pymupdf.Rect(bbox)
    mat = page.rotation_matrix
    return [(r * mat).normalize() for r in per_line.values()]


def search_page(
    doc: DocLike,
    index: int,
    query: str,
    *,
    case_sensitive: bool = False,
    whole_words: bool = False,
) -> list[SearchHit]:
    """All hits on one page (see :func:`search`)."""
    needle = _normalize_query(query)
    if not needle:
        return []
    page = as_document(doc)[index]
    textpage = page.get_textpage(flags=_TEXT_FLAGS)
    # Cheap pre-check on plain text before building the per-char model.
    plain = " ".join(textpage.extractText().split())
    if not case_sensitive:
        plain, needle_cmp = _fold(plain), _fold(needle)
    else:
        needle_cmp = needle
    if needle_cmp not in plain:
        return []

    model = _page_text(page, textpage)
    haystack = model.text if case_sensitive else _fold(model.text)
    hits: list[SearchHit] = []
    pos = 0
    while True:
        start = haystack.find(needle_cmp, pos)
        if start < 0:
            break
        end = start + len(needle_cmp)
        if whole_words and (
            (start > 0 and _is_word_char(haystack[start - 1]) and _is_word_char(haystack[start]))
            or (end < len(haystack) and _is_word_char(haystack[end]) and _is_word_char(haystack[end - 1]))
        ):
            pos = start + 1
            continue
        rects = _hit_rects(page, model, start, end)
        if rects:
            hits.append(SearchHit(index, rects, _context(model.text, start, end)))
        pos = end
    return hits


def search(
    doc: DocLike,
    query: str,
    *,
    case_sensitive: bool = False,
    whole_words: bool = False,
    start_page: int = 0,
) -> Iterator[SearchHit]:
    """Yield hits page by page (lazily), starting at ``start_page``.

    Matching is case-insensitive unless ``case_sensitive``; ``whole_words``
    requires the match not to continue a word on either side. Whitespace in
    the query matches any whitespace / line break in the text.
    """
    index = max(0, start_page)
    # Re-resolve the document for every page: when ``doc`` is a PdfDocument
    # an undo between two ``next()`` calls replaces the underlying document.
    while index < as_document(doc).page_count:
        yield from search_page(doc, index, query, case_sensitive=case_sensitive, whole_words=whole_words)
        index += 1


__all__ = ["CONTEXT_CHARS", "SearchHit", "search", "search_page"]
