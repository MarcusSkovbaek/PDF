"""Combining several files (PDFs and images) into one PDF.

Sources are always read into memory and opened from bytes, so no file handle
stays open (Windows would otherwise lock the files).
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Sequence

import pymupdf

from ..i18n import tr
from .document import DocumentError, InvalidPassword, PasswordRequired
from .images import ImagePageOptions, images_to_pdf, is_image_file


@dataclass
class MergeSource:
    """One input file. ``page_indices`` (0-based, in the given order) selects
    a subset of its pages; ``password`` unlocks encrypted PDFs."""

    path: str
    page_indices: list[int] | None = None
    password: str | None = None


# -- opening sources -----------------------------------------------------------
def _looks_like_pdf(data: bytes) -> bool:
    return b"%PDF" in data[:1024]


def open_source(
    source: str | bytes,
    password: str | None = None,
    *,
    name: str | None = None,
    image_options: ImagePageOptions | None = None,
) -> pymupdf.Document:
    """Open a PDF or image (path or bytes) as an unencrypted in-memory PDF.

    Images become one page each (see :mod:`pdfstudio.core.images`). Raises
    :class:`PasswordRequired` / :class:`InvalidPassword` (with the file name
    as argument) for locked PDFs and :class:`DocumentError` naming the file
    for unreadable input. The caller owns (and must close) the result.
    """
    if isinstance(source, (bytes, bytearray, memoryview)):
        data = bytes(source)
        name = name or tr("core.unnamed_source")
        is_image = not _looks_like_pdf(data)
    else:
        name = name or os.path.basename(source)
        try:
            with open(source, "rb") as fh:
                data = fh.read()
        except OSError as exc:
            raise DocumentError(tr("core.err.open_failed", name=name, reason=exc.strerror or exc)) from exc
        is_image = is_image_file(source) or not _looks_like_pdf(data)

    if not data:
        raise DocumentError(tr("core.err.open_failed", name=name, reason=tr("core.err.empty_file")))

    if is_image:
        try:
            return pymupdf.open("pdf", images_to_pdf([data], image_options))
        except DocumentError as exc:
            raise DocumentError(tr("core.err.open_failed", name=name, reason=tr("core.err.unsupported"))) from exc

    try:
        doc = pymupdf.open("pdf", data)
    except Exception as exc:
        raise DocumentError(tr("core.err.open_failed", name=name, reason=tr("core.err.unsupported"))) from exc
    if doc.needs_pass:
        if password is None:
            doc.close()
            raise PasswordRequired(name)
        if not doc.authenticate(password):
            doc.close()
            raise InvalidPassword(name)
        # Decrypt so pages can be copied / the result saved without the key.
        plain = doc.tobytes(encryption=pymupdf.PDF_ENCRYPT_NONE)
        doc.close()
        doc = pymupdf.open("pdf", plain)
    if doc.page_count == 0:
        doc.close()
        raise DocumentError(tr("core.err.open_failed", name=name, reason=tr("core.err.unsupported")))
    return doc


def validate_indices(indices: Sequence[int], page_count: int) -> list[int]:
    """Return ``indices`` as a list of ints, raising ``ValueError`` (Danish
    message, 1-based page numbers) if any is out of range."""
    result = [int(i) for i in indices]
    for i in result:
        if not 0 <= i < page_count:
            raise ValueError(tr("core.err.page_index", page=i + 1))
    return result


# -- merging -------------------------------------------------------------------
def _shifted_toc(src: pymupdf.Document, page_offset: int, level_offset: int) -> list[list]:
    """``src``'s outline with page numbers shifted into the merged document."""
    toc = []
    last_level = level_offset
    for item in src.get_toc(simple=False):
        level, title, page = item[0], item[1], item[2]
        # The outline of a file can be sloppy; set_toc requires that a level
        # never increases by more than one step.
        level = min(level + level_offset, last_level + 1)
        last_level = level
        dest = dict(item[3]) if len(item) > 3 and isinstance(item[3], dict) else {}
        if page < 1:  # outline item without a destination in this file
            page = 1
            dest = {}
        entry: list = [level, title, page + page_offset]
        if dest.get("kind") == pymupdf.LINK_GOTO:
            kept = {k: dest[k] for k in ("kind", "to", "zoom", "collapse", "color", "bold", "italic") if k in dest}
            entry.append(kept)
        toc.append(entry)
    return toc


def merge_files(
    sources: Sequence[MergeSource],
    *,
    add_bookmarks: bool = True,
    image_options: ImagePageOptions | None = None,
) -> bytes:
    """Concatenate PDFs and images into a new PDF and return its bytes.

    Each PDF's own outline is preserved. With ``add_bookmarks`` every file
    additionally gets a top-level bookmark (its name without extension) and
    its outline is nested below it.
    """
    if not sources:
        raise ValueError(tr("core.err.no_sources"))
    out = pymupdf.open()
    toc: list[list] = []
    try:
        for source in sources:
            name = os.path.basename(source.path)
            src = open_source(source.path, source.password, name=name, image_options=image_options)
            try:
                if source.page_indices is not None:
                    indices = validate_indices(source.page_indices, src.page_count)
                    if not indices:
                        continue
                    if indices != list(range(src.page_count)):
                        src.select(indices)
                start = out.page_count
                out.insert_pdf(src, links=True, annots=True)
                if add_bookmarks:
                    toc.append([1, os.path.splitext(name)[0], start + 1])
                toc.extend(_shifted_toc(src, start, 1 if add_bookmarks else 0))
            finally:
                src.close()
        if out.page_count == 0:
            raise ValueError(tr("core.err.no_pages"))
        if toc:
            out.set_toc(toc)
        return out.tobytes(garbage=3, deflate=True)
    finally:
        out.close()


__all__ = ["MergeSource", "merge_files", "open_source", "validate_indices"]
