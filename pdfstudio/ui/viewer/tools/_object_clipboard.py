"""The in-app object clipboard of "Markér objekt" (Ctrl+C / Ctrl+X / Ctrl+V).

Page objects are copied as a :class:`pdfstudio.core.pageobjects.Clip` (a
one-page PDF holding only them). The clip lives here, at module level, so it
works across tabs and documents of the running program. The system clipboard
gets a marker (plus the image or the text, so the copy is also useful in
other programs); when something else is copied afterwards – in Folio or in
another program – the marker is gone and the stale clip is not pasted.
Nothing ever leaves the machine.
"""

from __future__ import annotations

import uuid
import weakref
from dataclasses import dataclass, field

from PySide6.QtCore import QMimeData
from PySide6.QtGui import QGuiApplication, QImage

MIME = "application/x-folio-pdf-objects"


@dataclass
class Entry:
    clip: object                          # pageobjects.Clip
    source: weakref.ref | None            # the PdfDocument copied from
    page: int                             # its page
    count: int                            # number of objects
    token: str = field(default_factory=lambda: uuid.uuid4().hex)
    pastes: int = 0                       # pastes onto the source page so far (cascading offset)


_entry: Entry | None = None


def _ref(pdf) -> weakref.ref | None:
    try:
        return weakref.ref(pdf)
    except TypeError:  # pragma: no cover - PdfDocument supports weak references
        return None


def put(clip, pdf, page: int, count: int, *, image: QImage | None = None, text: str = "") -> Entry:
    """Store ``clip`` (copied from ``pdf``'s ``page``) and mark the system
    clipboard; ``image`` / ``text`` are offered to other programs too."""
    global _entry
    _entry = Entry(clip, _ref(pdf), page, count)
    md = QMimeData()
    md.setData(MIME, _entry.token.encode("ascii"))
    if image is not None and not image.isNull():
        md.setImageData(image)
    if text:
        md.setText(text)
    try:
        QGuiApplication.clipboard().setMimeData(md)
    except Exception:  # pragma: no cover - no clipboard on this platform
        pass
    return _entry


def current() -> Entry | None:
    """The clip to paste, or None (nothing copied, or the system clipboard
    has been given something else since)."""
    e = _entry
    if e is None:
        return None
    try:
        md = QGuiApplication.clipboard().mimeData()
    except Exception:  # pragma: no cover
        md = None
    if md is None or not md.formats():
        return e                        # no (readable) system clipboard: trust our own copy
    if md.hasFormat(MIME):
        return e if bytes(md.data(MIME)).decode("ascii", "replace") == e.token else None
    return None                         # someone copied something else since


def is_source(e: Entry, pdf, page: int) -> bool:
    return e.source is not None and e.source() is pdf and e.page == page


def clear() -> None:
    global _entry
    _entry = None


__all__ = ["Entry", "MIME", "put", "current", "is_source", "clear"]
