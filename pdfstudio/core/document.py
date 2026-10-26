"""The document model shared by every feature.

``PdfDocument`` wraps a :class:`pymupdf.Document` and adds:

* undo/redo (``edit()`` context manager + :class:`History`)
* dirty tracking and atomic saving – split into :meth:`PdfDocument.prepare_save`
  (serialise, main thread), :func:`write_atomic` (pure file I/O, safe in a
  worker thread) and :meth:`PdfDocument.finish_save`, so the UI can write in
  the background; :meth:`PdfDocument.save` does all three
* password protection that survives saving (:class:`Security`)
* detection of changes made to the file by other programs
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
import secrets
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

#: PDF permission bits meaning "everything allowed"
ALL_PERMISSIONS = -1
#: write PDFs into a native MuPDF buffer (see :func:`write_bytes`)
NATIVE_WRITE = True


def write_bytes(doc: pymupdf.Document, *, garbage: int = 0, clean: bool = False, deflate: bool = False,
                deflate_images: bool = False, deflate_fonts: bool = False,
                encryption: int = pymupdf.PDF_ENCRYPT_NONE, permissions: int = ALL_PERMISSIONS,
                owner_pw: str | None = None, user_pw: str | None = None, use_objstms: bool = False) -> bytes:
    """``doc.tobytes(...)`` with the same result, an order of magnitude
    faster on big files: PyMuPDF writes to bytes through a Python callback
    per chunk (450 000 calls for a 58-page report, ~110 ms); here MuPDF
    writes into a native buffer (~8 ms). Falls back to ``tobytes`` when the
    PyMuPDF internals it uses are not there."""
    kw = dict(garbage=garbage, clean=clean, deflate=deflate, deflate_images=deflate_images,
              deflate_fonts=deflate_fonts, encryption=encryption, permissions=permissions,
              owner_pw=owner_pw, user_pw=user_pw, use_objstms=int(bool(use_objstms)))
    if not NATIVE_WRITE:
        return doc.tobytes(**kw)
    try:
        mupdf = pymupdf.mupdf
        pdf = pymupdf._as_pdf_document(doc)
        clean_embedded, ensure_id = pymupdf.JM_embedded_clean, pymupdf.JM_ensure_identity
    except AttributeError:
        return doc.tobytes(**kw)
    if doc.is_closed or doc.is_encrypted:
        raise ValueError("document closed or encrypted")
    if doc.page_count < 1:
        raise ValueError("cannot save with zero pages")
    opts = mupdf.PdfWriteOptions()
    opts.do_garbage = garbage
    opts.do_clean = opts.do_sanitize = int(bool(clean))
    opts.do_compress = int(bool(deflate))
    opts.do_compress_images = int(bool(deflate_images))
    opts.do_compress_fonts = int(bool(deflate_fonts))
    opts.do_encrypt = encryption
    opts.permissions = permissions
    if owner_pw is not None:
        opts.opwd_utf8_set_value(owner_pw)
    elif user_pw is not None:
        opts.opwd_utf8_set_value(user_pw)
    if user_pw is not None:
        opts.upwd_utf8_set_value(user_pw)
    opts.do_preserve_metadata = 1
    opts.do_use_objstms = int(bool(use_objstms))
    pdf.m_internal.resynth_required = 0
    clean_embedded(pdf)
    ensure_id(pdf)
    buf = mupdf.FzBuffer(1 << 16)
    out = mupdf.FzOutput(buf)
    mupdf.pdf_write_document(pdf, out, opts)
    out.fz_close_output()
    return buf.fz_buffer_extract()


@dataclass(frozen=True)
class Security:
    """Password protection to write when saving (AES-256).

    ``user_pw`` is needed to open the file ("" = opens without a password,
    e.g. a file that only restricts printing/copying). ``owner_pw`` unlocks
    all rights; ``""`` means unknown – a random one is used so the
    restrictions in ``permissions`` stay in force."""

    user_pw: str = ""
    owner_pw: str = ""
    permissions: int = ALL_PERMISSIONS

    @property
    def needs_password(self) -> bool:
        return bool(self.user_pw)

    def save_kwargs(self) -> dict:
        return {
            "encryption": pymupdf.PDF_ENCRYPT_AES_256,
            "user_pw": self.user_pw,
            "owner_pw": self.owner_pw or secrets.token_hex(16),
            "permissions": self.permissions,
        }


@dataclass(frozen=True)
class SaveJob:
    """Serialised document, ready to be written by :func:`write_atomic`."""

    data: bytes
    revision: int


def _stat(path: str | None) -> tuple[int, int] | None:
    if not path:
        return None
    try:
        st = os.stat(path)
    except OSError:
        return None
    return (st.st_mtime_ns, st.st_size)


def write_atomic(path: str, data: bytes) -> str:
    """Write ``data`` to ``path`` via a temp file in the same folder and an
    atomic replace (the old file stays intact if anything fails). Touches
    no document – safe to call from a worker thread. Returns the path."""
    target = os.path.abspath(path)
    folder = os.path.dirname(target) or "."
    fd, tmp = tempfile.mkstemp(prefix=".folio-", suffix=".pdf", dir=folder)
    try:
        try:  # keep the file's permissions (mkstemp makes it private)
            os.chmod(tmp, os.stat(target).st_mode & 0o7777)
        except OSError:
            pass
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, target)
    except BaseException as exc:
        try:
            os.remove(tmp)
        except OSError:
            pass
        if isinstance(exc, Exception):
            raise DocumentError(str(exc)) from exc
        raise
    return target


def check_writable(path: str) -> str | None:
    """Why ``path`` cannot be saved to, or ``None``: ``"readonly"`` (file is
    write protected), ``"locked"`` (open in another program – Windows),
    ``"folder"`` (no write access to the folder)."""
    target = os.path.abspath(path)
    folder = os.path.dirname(target) or "."
    if not os.path.isdir(folder) or not os.access(folder, os.W_OK):
        return "folder"
    if os.path.exists(target):
        if not os.access(target, os.W_OK):
            return "readonly"
        try:
            with open(target, "r+b"):
                pass
        except PermissionError:
            return "locked"
        except OSError:
            return "locked"
    return None


class PdfDocument:
    def __init__(self, doc: pymupdf.Document, path: str | None = None, *, title: str | None = None):
        self._doc = doc
        self.path: str | None = path
        self._title = title
        self.history = History()
        self._dirty = False
        self._listeners: list[Listener] = []
        self._edit_depth = 0
        #: increases with every change event – a cheap cache key for views
        self.revision = 0
        #: password protection written on save (``None``: none)
        self.security: Security | None = None
        self._disk_stamp = _stat(path)

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
        security = None
        if doc.needs_pass:
            level = doc.authenticate(password)
            if not level:
                doc.close()
                raise InvalidPassword(path)
            # 2 = user password, 4 = owner password, 6 = both are this one.
            # Opened with the owner password only, the user password is
            # unknown: the one typed protects the saved file instead.
            security = Security(user_pw=password, owner_pw=password if level & 4 else "",
                                permissions=doc.permissions)
        elif (doc.metadata or {}).get("encryption"):
            security = Security(permissions=doc.permissions)  # restrictions without an open password
        if security is not None:
            # Re-open decrypted in memory so saving/undo works uniformly;
            # the protection is put back on save.
            data = write_bytes(doc)
            doc.close()
            doc = pymupdf.open("pdf", data)
        pdf = cls(doc, os.path.abspath(path))
        pdf.security = security
        return pdf

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
        self.revision += 1
        for cb in list(self._listeners):
            cb(event)

    # -- editing ---------------------------------------------------------
    def _serialize(self) -> bytes:
        return write_bytes(self._doc)

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
    def set_security(self, security: Security | None) -> None:
        """Change the password protection (applies when saving)."""
        if security == self.security:
            return
        self.security = security
        self._dirty = True
        self._emit(ChangeEvent(CONTENT, (), ""))

    def changed_on_disk(self) -> bool:
        """The file was changed (or removed) by something else since it was
        opened or last saved here."""
        return bool(self.path) and self._disk_stamp is not None and _stat(self.path) != self._disk_stamp

    def has_signatures(self) -> bool:
        """The document contains digital signatures (saving changes breaks them)."""
        try:
            if self._doc.get_sigflags() > 0:
                return True
        except Exception:
            pass
        for page in self._doc:
            for w in page.widgets() or []:
                if w.field_type == pymupdf.PDF_WIDGET_TYPE_SIGNATURE and w.field_value:
                    return True
        return False

    def serialize(self, *, compact: bool = True, protect: bool = True) -> bytes:
        """The PDF as saved: fonts subset, garbage collected and compressed
        (``compact``), encrypted when protected (``protect``)."""
        kw: dict = {}
        if protect and self.security is not None:
            kw.update(self.security.save_kwargs())
        if compact:
            self._subset_fonts()
            return write_bytes(self._doc, garbage=3, deflate=True, **kw)
        return write_bytes(self._doc, **kw)

    def prepare_save(self) -> SaveJob:
        """Serialise for saving (must run on the thread that owns the doc)."""
        try:
            return SaveJob(self.serialize(), self.revision)
        except Exception as exc:  # pymupdf raises various types
            raise DocumentError(str(exc)) from exc

    def finish_save(self, path: str, job: SaveJob) -> None:
        """Record a successful write of ``job`` to ``path``. Changes made
        while it was being written keep the document dirty."""
        self.path = os.path.abspath(path)
        self._disk_stamp = _stat(self.path)
        if self.revision == job.revision:
            self._dirty = False
        self._emit(ChangeEvent(SAVED, None, ""))

    def save(self, path: str | None = None) -> str:
        """Save atomically (temp file + replace). Returns the path written."""
        target = os.path.abspath(path or self.path or "")
        if not target or target == os.path.abspath(""):
            raise DocumentError("Ingen filsti angivet")
        job = self.prepare_save()
        write_atomic(target, job.data)
        self.finish_save(target, job)
        return target

    def save_copy(self, path: str) -> str:
        """Write a copy; the document keeps its path and unsaved state."""
        return write_atomic(path, self.prepare_save().data)

    def recovery_bytes(self) -> bytes:
        """Quick serialisation for crash recovery (no compaction, protected
        like the file would be)."""
        return self.serialize(compact=False)

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
        """Unprotected, compacted PDF bytes (for exports and copies)."""
        return self.serialize(protect=False)

    def close(self) -> None:
        self._listeners.clear()
        try:
            self._doc.close()
        except Exception:
            pass
