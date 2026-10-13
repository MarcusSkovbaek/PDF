"""The document model shared by every feature.

``PdfDocument`` wraps a :class:`pymupdf.Document` and adds:

* undo/redo (``edit()`` context manager + :class:`History`)
* dirty tracking and atomic saving
* a tiny observer mechanism so the UI can react to changes without the core
  depending on Qt.

RULES FOR FEATURE CODE
----------------------
* Never cache ``document.doc`` or page objects across edits – undo/redo
  replaces the underlying ``pymupdf.Document``. Always go through
  ``document.doc`` / ``document.page(i)``.
* Every mutation must run inside ``with document.edit("Label", ...):`` so it
  is undoable and observers are notified.
"""

from __future__ import annotations

import os
import tempfile
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Callable, Iterator

import pymupdf

from .history import History


class PasswordRequired(Exception):
    """The PDF is encrypted and needs a password."""


class InvalidPassword(Exception):
    """The supplied password was wrong."""


class DocumentError(Exception):
    """A file could not be opened / saved as PDF."""


# Change kinds ---------------------------------------------------------------
CONTENT = "content"      # page content/annotations changed; ``pages`` lists them
STRUCTURE = "structure"  # pages added/removed/reordered/rotated
RELOADED = "reloaded"    # the whole document was replaced (undo/redo/revert)
SAVED = "saved"          # saved to disk (dirty flag cleared, path may change)


@dataclass(frozen=True)
class ChangeEvent:
    kind: str
    pages: tuple[int, ...] | None = None  # None == "possibly all pages"
    label: str = ""


Listener = Callable[[ChangeEvent], None]


class PdfDocument:
    def __init__(self, doc: pymupdf.Document, path: str | None = None, *, title: str | None = None):
        self._doc = doc
        self.path: str | None = path
        self._title = title
        self.history = History()
        self._dirty = False
        self._listeners: list[Listener] = []
        self._edit_depth = 0

    # -- construction ----------------------------------------------------
    @classmethod
    def open(cls, path: str, password: str | None = None) -> "PdfDocument":
        # Read into memory: keeps no handle on the file (Windows locks open
        # files, which would break "save over the original").
        try:
            with open(path, "rb") as fh:
                data = fh.read()
            ext = os.path.splitext(path)[1].lstrip(".").lower() or "pdf"
            doc = pymupdf.open(stream=data, filetype=ext)
        except Exception as exc:  # pymupdf raises various types
            raise DocumentError(str(exc)) from exc
        if not doc.is_pdf:
            # images / xps / epub etc. -> convert to a PDF document in memory
            from .images import SUPPORTED_IMAGE_EXTENSIONS, images_to_pdf

            try:
                if ("." + ext) in SUPPORTED_IMAGE_EXTENSIONS:
                    pdf_data = images_to_pdf([data])  # same sizing as "Billeder til PDF"
                else:
                    pdf_data = doc.convert_to_pdf()
            finally:
                doc.close()
            doc = pymupdf.open("pdf", pdf_data)
            title = os.path.splitext(os.path.basename(path))[0] + ".pdf"
            pdf = cls(doc, None, title=title)
            pdf._dirty = True
            return pdf
        if doc.needs_pass:
            if password is None:
                doc.close()
                raise PasswordRequired(path)
            if not doc.authenticate(password):
                doc.close()
                raise InvalidPassword(path)
            # Re-open decrypted in memory so saving/undo works uniformly.
            data = doc.tobytes(encryption=pymupdf.PDF_ENCRYPT_NONE)
            doc.close()
            doc = pymupdf.open("pdf", data)
        return cls(doc, os.path.abspath(path))

    @classmethod
    def from_bytes(cls, data: bytes, title: str = "Unavngivet.pdf") -> "PdfDocument":
        pdf = cls(pymupdf.open("pdf", data), None, title=title)
        pdf._dirty = True
        return pdf

    @classmethod
    def new_blank(cls, width: float = 595.0, height: float = 842.0, title: str = "Unavngivet.pdf") -> "PdfDocument":
        doc = pymupdf.open()
        doc.new_page(width=width, height=height)
        pdf = cls(doc, None, title=title)
        pdf._dirty = True
        return pdf

    # -- basic properties -----------------------------------------------
    @property
    def doc(self) -> pymupdf.Document:
        return self._doc

    def page(self, index: int) -> pymupdf.Page:
        return self._doc[index]

    @property
    def page_count(self) -> int:
        return self._doc.page_count

    @property
    def is_dirty(self) -> bool:
        return self._dirty

    @property
    def display_name(self) -> str:
        if self.path:
            return os.path.basename(self.path)
        return self._title or "Unavngivet.pdf"

    # -- observers -------------------------------------------------------
    def listen(self, callback: Listener) -> Callable[[], None]:
        self._listeners.append(callback)

        def unsubscribe() -> None:
            if callback in self._listeners:
                self._listeners.remove(callback)

        return unsubscribe

    def _emit(self, event: ChangeEvent) -> None:
        for cb in list(self._listeners):
            cb(event)

    # -- editing ---------------------------------------------------------
    def _serialize(self) -> bytes:
        return self._doc.tobytes(garbage=0, deflate=False)

    def _replace(self, data: bytes) -> None:
        old = self._doc
        self._doc = pymupdf.open("pdf", data)
        old.close()

    @contextmanager
    def edit(self, label: str, kind: str = CONTENT, pages: Iterator[int] | tuple[int, ...] | None = None):
        """Run a mutation as one undoable step.

        Nested ``edit`` blocks are merged into the outermost one. If the body
        raises, the document is restored to its previous state and the
        exception propagates.
        """
        if self._edit_depth:
            self._edit_depth += 1
            try:
                yield self._doc
            finally:
                self._edit_depth -= 1
            return
        before = self._serialize()
        self._edit_depth = 1
        try:
            yield self._doc
        except BaseException:
            self._edit_depth = 0
            self._replace(before)
            self._emit(ChangeEvent(RELOADED, None, label))
            raise
        self._edit_depth = 0
        self.history.push(label, before)
        self._dirty = True
        self._emit(ChangeEvent(kind, tuple(pages) if pages is not None else None, label))

    def undo(self) -> bool:
        snap = self.history.undo(self._serialize())
        if snap is None:
            return False
        self._replace(snap.data)
        self._dirty = True
        self._emit(ChangeEvent(RELOADED, None, snap.label))
        return True

    def redo(self) -> bool:
        snap = self.history.redo(self._serialize())
        if snap is None:
            return False
        self._replace(snap.data)
        self._dirty = True
        self._emit(ChangeEvent(RELOADED, None, snap.label))
        return True

    # -- persistence -----------------------------------------------------
    def save(self, path: str | None = None) -> str:
        """Save atomically (temp file + replace). Returns the path written."""
        target = os.path.abspath(path or self.path or "")
        if not target or target == os.path.abspath(""):
            raise DocumentError("Ingen filsti angivet")
        folder = os.path.dirname(target) or "."
        fd, tmp = tempfile.mkstemp(prefix=".folio-", suffix=".pdf", dir=folder)
        os.close(fd)
        self._subset_fonts()
        try:
            self._doc.save(tmp, garbage=3, deflate=True, clean=False)
            os.replace(tmp, target)
        except Exception as exc:
            try:
                os.remove(tmp)
            except OSError:
                pass
            raise DocumentError(str(exc)) from exc
        # Re-open from the saved bytes so the file on disk is not kept locked
        # (important on Windows) and in-memory state matches the file.
        with open(target, "rb") as fh:
            self._replace(fh.read())
        self.path = target
        self._dirty = False
        self._emit(ChangeEvent(SAVED, None, ""))
        return target

    def _subset_fonts(self) -> None:
        """Keep only the glyphs actually used of every embedded font. Edited
        text may embed a whole system font (hundreds of KB); Word/Acrobat
        output is subset too. Best effort – a font that cannot be subset is
        simply kept as is."""
        try:
            self._doc.subset_fonts()
        except Exception:
            pass

    def to_bytes(self) -> bytes:
        self._subset_fonts()
        return self._doc.tobytes(garbage=3, deflate=True)

    def close(self) -> None:
        self._listeners.clear()
        try:
            self._doc.close()
        except Exception:
            pass
