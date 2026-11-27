"""Page organisation: rotate, delete, move, duplicate, insert, extract, split.

Page indices are 0-based everywhere; only :func:`parse_page_ranges` deals
with the 1-based numbers users type. Every mutation is one undoable
``pdf.edit(..., STRUCTURE)`` step; no-ops (e.g. moving pages onto their own
position) do not create an undo step.
"""

from __future__ import annotations

import os
import re
from typing import Iterable, Sequence

import pymupdf

from ..i18n import tr
from .document import STRUCTURE, PdfDocument, write_bytes
from .images import ImagePageOptions
from .merge import open_source, validate_indices

_DASHES = "-–—‒−"  # hyphen, en dash, em dash, figure dash, minus
_RANGE_RE = re.compile(rf"^(\d*)\s*[{_DASHES}]\s*(\d*)$")
_SEPARATORS = re.compile(r"[,;]")


def _label(key: str, count: int) -> str:
    """Singular/plural undo label: ``core.undo.<key>_page(s)``."""
    return tr(f"core.undo.{key}_page" if count == 1 else f"core.undo.{key}_pages")


def _unique_sorted(pdf: PdfDocument, indices: Iterable[int]) -> list[int]:
    return sorted(set(validate_indices(list(indices), pdf.page_count)))


# -- page range text -------------------------------------------------------------
def _page_number(token: str, page_count: int) -> int:
    number = int(token)
    if number < 1:
        raise ValueError(tr("core.err.range_zero"))
    if number > page_count:
        key = "core.err.range_out_of_bounds_one" if page_count == 1 else "core.err.range_out_of_bounds"
        raise ValueError(tr(key, page=number, count=page_count))
    return number - 1


def parse_page_ranges(text: str, page_count: int) -> list[int]:
    """Parse user input like ``"1-3, 5, 8-"`` into 0-based page indices.

    * items are separated by ``,`` or ``;``; whitespace is ignored
    * ``a-b`` is inclusive; ``a-`` runs to the last page, ``-b`` from the
      first; en/em dashes are accepted as well as ``-``
    * a descending range (``5-3``) yields the pages in descending order
    * the result keeps the typed order and drops repeated pages

    Raises ``ValueError`` with a Danish message on empty, malformed or
    out-of-range input.
    """
    parts = [p.strip() for p in _SEPARATORS.split(text or "")]
    parts = [p for p in parts if p]
    if not parts:
        raise ValueError(tr("core.err.range_empty"))
    result: list[int] = []
    seen: set[int] = set()
    for part in parts:
        if part.isdigit():
            pages: Iterable[int] = [_page_number(part, page_count)]
        else:
            match = _RANGE_RE.match(part)
            if not match or not (match.group(1) or match.group(2)):
                raise ValueError(tr("core.err.range_syntax", part=part))
            start = _page_number(match.group(1), page_count) if match.group(1) else 0
            end = _page_number(match.group(2), page_count) if match.group(2) else page_count - 1
            step = 1 if end >= start else -1
            pages = range(start, end + step, step)
        for page in pages:
            if page not in seen:
                seen.add(page)
                result.append(page)
    return result


# -- simple structure operations -----------------------------------------------------
def rotate_pages(pdf: PdfDocument, indices: Sequence[int], degrees: int = 90) -> None:
    """Rotate pages by ``degrees`` (relative, multiple of 90; negative = left)."""
    if degrees % 90:
        raise ValueError(tr("core.err.rotation"))
    pages = _unique_sorted(pdf, indices)
    if not pages or degrees % 360 == 0:
        return
    with pdf.edit(_label("rotate", len(pages)), STRUCTURE, pages):
        for i in pages:
            page = pdf.page(i)
            page.set_rotation((page.rotation + degrees) % 360)


def delete_pages(pdf: PdfDocument, indices: Sequence[int]) -> None:
    """Delete pages. Refuses (``ValueError``) to delete every page."""
    pages = _unique_sorted(pdf, indices)
    if not pages:
        return
    if len(pages) >= pdf.page_count:
        raise ValueError(tr("core.err.delete_all"))
    with pdf.edit(_label("delete", len(pages)), STRUCTURE, None):
        pdf.doc.delete_pages(pages)


def move_pages(pdf: PdfDocument, indices: Sequence[int], to_index: int) -> list[int]:
    """Move the selected pages as one block to the gap before original page
    ``to_index`` (``page_count`` = the end).

    The selection may be non-contiguous; the moved pages keep their relative
    (ascending) order and close up. ``to_index`` refers to the numbering
    *before* the move, which is what a drop indicator between thumbnails
    shows. If ``to_index`` is itself selected, the block lands where that
    page's gap ends up. Returns the new indices of the moved pages.

    Example (pages A B C D E): ``move_pages([0, 2], 4)`` -> B D A C E,
    returns ``[2, 3]``; ``move_pages([3, 4], 0)`` -> D E A B C.
    """
    count = pdf.page_count
    if not 0 <= to_index <= count:
        raise ValueError(tr("core.err.insert_position", pos=to_index))
    selected = _unique_sorted(pdf, indices)
    if not selected:
        return []
    chosen = set(selected)
    rest = [i for i in range(count) if i not in chosen]
    insert_at = sum(1 for i in rest if i < to_index)
    order = rest[:insert_at] + selected + rest[insert_at:]
    new_indices = list(range(insert_at, insert_at + len(selected)))
    if order == list(range(count)):
        return new_indices
    with pdf.edit(_label("move", len(selected)), STRUCTURE, None):
        pdf.doc.select(order)
    return new_indices


def duplicate_pages(pdf: PdfDocument, indices: Sequence[int]) -> list[int]:
    """Insert an independent copy of every selected page right after its
    original. Returns the indices of the copies (ascending)."""
    pages = _unique_sorted(pdf, indices)
    if not pages:
        return []
    with pdf.edit(_label("duplicate", len(pages)), STRUCTURE, None):
        doc = pdf.doc
        for i in reversed(pages):  # back to front keeps earlier indices valid
            # fullcopy_page (not copy_page) so editing one copy does not
            # change the other; annotations are copied too.
            doc.fullcopy_page(i, i + 1 if i + 1 < doc.page_count else -1)
    return [i + k + 1 for k, i in enumerate(pages)]


def insert_blank_page(pdf: PdfDocument, at: int, width: float | None = None, height: float | None = None) -> int:
    """Insert an empty page before ``at`` (``page_count`` appends).

    Without an explicit size the page gets the displayed size of its
    neighbour (the page before, or the one after when inserting first).
    Returns the new page's index.
    """
    count = pdf.page_count
    if not 0 <= at <= count:
        raise ValueError(tr("core.err.insert_position", pos=at))
    if width is None or height is None:
        if count:
            ref = pdf.page(at - 1 if at > 0 else 0).rect
            width = ref.width if width is None else width
            height = ref.height if height is None else height
        else:
            width, height = width or 595.0, height or 842.0
    if width <= 0 or height <= 0:
        raise ValueError(tr("core.err.page_size"))
    with pdf.edit(tr("core.undo.insert_blank"), STRUCTURE, None):
        pdf.doc.new_page(pno=at if at < count else -1, width=width, height=height)
    return at


def insert_pdf(
    pdf: PdfDocument,
    source: str | bytes,
    at: int,
    page_indices: Sequence[int] | None = None,
    password: str | None = None,
    *,
    image_options: ImagePageOptions | None = None,
) -> list[int]:
    """Insert pages of another PDF (or an image) before ``at``.

    ``source`` is a path or bytes; images become pages via
    :func:`pdfstudio.core.images.images_to_pdf` (``image_options``, default
    "fit"). ``page_indices`` selects (and orders) the source pages. Raises
    ``PasswordRequired`` / ``InvalidPassword`` / ``DocumentError`` like
    :func:`pdfstudio.core.merge.open_source`. Returns the new page indices.
    """
    count = pdf.page_count
    if not 0 <= at <= count:
        raise ValueError(tr("core.err.insert_position", pos=at))
    src = open_source(source, password, image_options=image_options)
    try:
        if page_indices is not None:
            selected = validate_indices(page_indices, src.page_count)
            if not selected:
                return []
            if selected != list(range(src.page_count)):
                src.select(selected)
        n = src.page_count
        with pdf.edit(tr("core.undo.insert_pages"), STRUCTURE, None):
            pdf.doc.insert_pdf(src, start_at=at if at < count else -1, links=True, annots=True)
    finally:
        src.close()
    return list(range(at, at + n))


# -- producing new files -------------------------------------------------------------
def _runs(indices: Sequence[int]) -> list[tuple[int, int]]:
    """Group indices into ascending contiguous runs ``(first, last)``."""
    runs: list[tuple[int, int]] = []
    for i in indices:
        if runs and i == runs[-1][1] + 1:
            runs[-1] = (runs[-1][0], i)
        else:
            runs.append((i, i))
    return runs


_META_KEYS = ("title", "author", "subject", "keywords", "creator", "producer")


def _subset_bytes(src: pymupdf.Document, indices: Sequence[int]) -> bytes:
    """A new PDF holding ``src``'s pages ``indices`` (in that order)."""
    out = pymupdf.open()
    try:
        runs = _runs(indices)
        for first, last in runs:
            out.insert_pdf(src, from_page=first, to_page=last, links=True, annots=True)
        meta = {k: v for k, v in (src.metadata or {}).items() if k in _META_KEYS and v}
        if meta:
            out.set_metadata(meta)
        # Several runs may each have copied the same fonts/images: garbage=4
        # merges those duplicates.
        return write_bytes(out, garbage=4 if len(runs) > 1 else 3, deflate=True)
    finally:
        out.close()


def extract_pages(pdf: PdfDocument, indices: Sequence[int]) -> bytes:
    """Return a new PDF with the given pages (in the given order); the
    document itself is not changed."""
    selected = validate_indices(indices, pdf.page_count)
    if not selected:
        raise ValueError(tr("core.err.no_pages"))
    return _subset_bytes(pdf.doc, selected)


def _stem(pdf: PdfDocument) -> str:
    stem = pdf.display_name
    if stem.lower().endswith(".pdf"):
        stem = stem[:-4]
    stem = re.sub(r'[\\/:*?"<>|\x00-\x1f]', "_", stem).strip(" .")
    return stem or os.path.splitext(tr("app.untitled"))[0]


def _split_groups(pdf: PdfDocument, every_n: int | None, ranges) -> list[list[int]]:
    count = pdf.page_count
    if every_n is not None and ranges is not None:
        raise ValueError(tr("core.err.split_args"))
    if ranges is None:
        n = 1 if every_n is None else int(every_n)
        if n < 1:
            raise ValueError(tr("core.err.split_every_n"))
        return [list(range(i, min(i + n, count))) for i in range(0, count, n)]
    if isinstance(ranges, str):
        items: list = [p for p in _SEPARATORS.split(ranges) if p.strip()]
        if not items:
            raise ValueError(tr("core.err.range_empty"))
    else:
        items = list(ranges)
    groups = []
    for item in items:
        group = parse_page_ranges(item, count) if isinstance(item, str) else validate_indices(item, count)
        if group:
            groups.append(group)
    if not groups:
        raise ValueError(tr("core.err.no_pages"))
    return groups


def split_document(
    pdf: PdfDocument,
    *,
    every_n: int | None = None,
    ranges: str | Sequence[str | Sequence[int]] | None = None,
) -> list[tuple[str, bytes]]:
    """Split into several PDFs; returns ``(suggested_filename, pdf_bytes)``.

    * ``every_n=3``  -> parts of 3 pages (the last may be shorter)
    * ``ranges="1-3, 4-6, 7-"`` -> one part per comma-separated item
    * ``ranges=[[0, 1], "5-"]`` -> one part per item (0-based index lists
      or range strings)
    * neither -> one file per page

    File names are ``<stem>_del1.pdf``, ``<stem>_del2.pdf`` ... (zero-padded
    when there are 10+ parts so they sort correctly). The document is not
    modified.
    """
    doc = pdf.doc
    return [(name, _subset_bytes(doc, group)) for name, group in split_plan(pdf, every_n=every_n, ranges=ranges)]


def split_plan(
    pdf: PdfDocument,
    *,
    every_n: int | None = None,
    ranges: str | Sequence[str | Sequence[int]] | None = None,
) -> list[tuple[str, list[int]]]:
    """What :func:`split_document` would produce, without building any PDF:
    ``(suggested_filename, 0-based page indices)`` per part. Cheap enough for
    live previews; raises ``ValueError`` on invalid input like split_document."""
    groups = _split_groups(pdf, every_n, ranges)
    stem = _stem(pdf)
    suffix = tr("core.split.part_suffix")
    width = len(str(len(groups)))
    return [(f"{stem}_{suffix}{n:0{width}d}.pdf", list(group)) for n, group in enumerate(groups, start=1)]


__all__ = [
    "delete_pages",
    "duplicate_pages",
    "extract_pages",
    "insert_blank_page",
    "insert_pdf",
    "move_pages",
    "parse_page_ranges",
    "rotate_pages",
    "split_document",
    "split_plan",
]
