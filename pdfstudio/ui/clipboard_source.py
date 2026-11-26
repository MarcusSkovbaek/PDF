"""The system clipboard as :class:`~pdfstudio.core.clipboard_pdf.ClipContent`.

Used by "Ny PDF fra udklipsholder" and "Indsæt fra udklipsholder"
(``docs/kravspec-udklipsholder.md``). Everything stays local (U16): only the
clipboard of this computer is read, local file paths are kept, web links
are ignored, and nothing an HTML fragment refers to is fetched.

* :func:`read_clipboard` – everything usable, encoded for the core (images as
  lossless PNG). Called when the user runs a command.
* :func:`clipboard_available` – a cheap check (formats only, no image
  encoding) for the enabled state of the commands.
* :func:`describe` – "et skærmbillede", "2 filer", "tekst" … for messages.
* helpers shared by the window and the workspace: :func:`clip_options`,
  :func:`password_prompt`, :func:`clipboard_title`, :func:`skipped_message`,
  :func:`busy`.
"""

from __future__ import annotations

import os
from contextlib import contextmanager
from datetime import datetime

from PySide6.QtCore import QBuffer, QIODevice, QMimeData, Qt
from PySide6.QtGui import QGuiApplication

from ..core.clipboard_pdf import ClipContent, ClipOptions, chosen_kind, usable_files
from ..i18n import tr, tr_n
from .viewer.tools import _object_clipboard

PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"

#: QSettings keys and defaults (decisions G1/G2 of the spec)
NEW_PAGE_SIZE_KEY = "clipboard/new_page_size"
INSERT_PAGE_SIZE_KEY = "clipboard/insert_page_size"
MARGIN_KEY = "clipboard/margin_mm"
NEW_PAGE_SIZES = ("image", "A4", "Letter")
INSERT_PAGE_SIZES = ("match", "image")
DEFAULT_MARGIN_MM = 10.0
MARGIN_RANGE = (0, 50)


# ------------------------------------------------------------------ reading
def _mime() -> QMimeData | None:
    try:
        return QGuiApplication.clipboard().mimeData()
    except RuntimeError:  # pragma: no cover - no clipboard on this platform
        return None


def _windows_format(fmt: str, name: str) -> bool:
    """Qt exposes registered Windows clipboard formats as
    ``application/x-qt-windows-mime;value="<name>"``."""
    return fmt.lower().endswith(f'value="{name.lower()}"')


def _png_formats(md: QMimeData) -> list[str]:
    return [f for f in md.formats() if f.lower() == "image/png" or _windows_format(f, "PNG")]


def _pdf_formats(md: QMimeData) -> list[str]:
    return [f for f in md.formats()
            if f.lower() in ("application/pdf", "application/x-pdf") or _windows_format(f, "Portable Document Format")]


def _local_files(md: QMimeData) -> list[str]:
    if not md.hasUrls():
        return []
    return [os.path.normpath(u.toLocalFile()) for u in md.urls() if u.isLocalFile() and u.toLocalFile()]


def _folio(md: QMimeData):
    """Folio's own object clip – only while the system clipboard still holds
    Folio's marker, i.e. it is the newest thing copied."""
    if not md.hasFormat(_object_clipboard.MIME):
        return None
    entry = _object_clipboard.current()
    return entry.clip if entry is not None else None


def _plain_text(md: QMimeData) -> str | None:
    """Real text only: ``QMimeData.text()`` also turns a copied file list into
    text, which must not count as text."""
    if not md.hasFormat("text/plain"):
        return None
    text = md.text()
    if not text or not text.strip():
        return None
    if md.hasUrls():
        links = {u.toString() for u in md.urls()} | {u.toLocalFile() for u in md.urls() if u.isLocalFile()}
        lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
        if lines and all(ln in links for ln in lines):
            return None
    return text


def _pdf_data(md: QMimeData) -> bytes | None:
    for fmt in _pdf_formats(md):
        data = bytes(md.data(fmt))
        if b"%PDF" in data[:1024]:
            return data
    return None


def _png_data(md: QMimeData) -> bytes | None:
    """The image losslessly as PNG: the program's own PNG bytes when it put
    some there (keeps transparency exactly), else Qt's image encoded as PNG."""
    for fmt in _png_formats(md):
        data = bytes(md.data(fmt))
        if data.startswith(PNG_SIGNATURE):
            return data
    if not md.hasImage():
        return None
    img = QGuiApplication.clipboard().image()
    if img.isNull():
        return None
    buf = QBuffer()
    buf.open(QIODevice.OpenModeFlag.WriteOnly)
    # PNG is lossless at every level; a low compression level keeps a 4K
    # screenshot well under the time budget (U17)
    img.save(buf, "PNG", 80)
    return bytes(buf.data())


def read_clipboard() -> ClipContent:
    """Everything usable on the system clipboard right now."""
    md = _mime()
    content = ClipContent()
    if md is None or not md.formats():
        return content
    content.folio = _folio(md)
    content.files = _local_files(md)
    content.pdf = _pdf_data(md)
    png = _png_data(md)
    if png:
        content.images = [png]
    if md.hasHtml():
        html = md.html()
        content.html = html if html and html.strip() else None
    content.text = _plain_text(md)
    return content


def clipboard_available() -> bool:
    """Whether the clipboard holds something that can become a page – formats
    only (no image encoding, no file reading), cheap enough for the enabled
    state of the commands."""
    md = _mime()
    if md is None or not md.formats():
        return False
    if _folio(md) is not None:
        return True
    if usable_files(_local_files(md))[0]:
        return True
    if md.hasImage() or _png_formats(md) or _pdf_formats(md):
        return True
    if md.hasHtml() and md.html().strip():
        return True
    return _plain_text(md) is not None


def describe(content: ClipContent, kind: str | None = None) -> str:
    """A short Danish summary of what is (or was) used: "et skærmbillede",
    "2 filer", "tekst" …"""
    kind = kind or chosen_kind(content)
    if kind == "files":
        return tr_n("clipboard.what.files", len(usable_files(content.files)[0]))
    if kind == "image":
        return tr_n("clipboard.what.image", max(1, len(content.images)))
    if kind in ("folio", "pdf", "html", "text"):
        return tr(f"clipboard.what.{kind}")
    return tr("clipboard.what.nothing")


# ------------------------------------------------------------------ helpers
def clipboard_title(when: datetime | None = None) -> str:
    """The file name a new PDF suggests on save: "Udklipsholder 2026-11-26 10.30.pdf" (local time)."""
    when = when or datetime.now().astimezone()      # the user's local time
    return tr("clipboard.title", stamp=when.strftime("%Y-%m-%d %H.%M"))


def _setting(settings, key: str, default):
    return settings.value(key, default) if settings is not None else default


def page_size_setting(settings, which: str) -> str:
    """``which`` = "new" | "insert" – the stored page size, validated."""
    key, allowed = ((NEW_PAGE_SIZE_KEY, NEW_PAGE_SIZES) if which == "new"
                    else (INSERT_PAGE_SIZE_KEY, INSERT_PAGE_SIZES))
    value = str(_setting(settings, key, allowed[0]))
    return value if value in allowed else allowed[0]


def margin_setting(settings) -> float:
    try:
        mm = float(_setting(settings, MARGIN_KEY, DEFAULT_MARGIN_MM))
    except (TypeError, ValueError):
        mm = DEFAULT_MARGIN_MM
    return min(max(mm, MARGIN_RANGE[0]), MARGIN_RANGE[1])


def clip_options(settings, which: str, *, prefer: str | None = None) -> ClipOptions:
    """The options from *Indstillinger → Udklipsholder* for a new PDF
    (``which="new"``) or inserted pages (``"insert"``)."""
    return ClipOptions(page_size=page_size_setting(settings, which), margin_mm=margin_setting(settings),
                       prefer=prefer)


@contextmanager
def busy():
    """A wait cursor while the pages are made."""
    QGuiApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
    try:
        yield
    finally:
        QGuiApplication.restoreOverrideCursor()


def password_prompt(parent):
    """``passwords(file_name)`` for the core: asks with the normal password
    dialog (the wait cursor lifted meanwhile); asking again for the same
    file means the previous password was wrong."""
    asked: dict[str, int] = {}

    def ask(name: str) -> str | None:
        from .dialogs.password import ask_password

        n = asked.get(name, 0)
        asked[name] = n + 1
        overridden = QGuiApplication.overrideCursor() is not None
        if overridden:
            QGuiApplication.restoreOverrideCursor()
        try:
            return ask_password(parent, name, wrong=n > 0)
        finally:
            if overridden:
                QGuiApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)

    return ask


def skipped_message(names: list[str], limit: int = 3) -> str:
    shown = ", ".join(names[:limit])
    if len(names) > limit:
        return tr("clipboard.skipped.more", names=shown, n=len(names) - limit)
    return tr("clipboard.skipped", names=shown)


__all__ = ["INSERT_PAGE_SIZE_KEY", "MARGIN_KEY", "NEW_PAGE_SIZE_KEY", "busy", "clip_options", "clipboard_available",
           "clipboard_title", "describe", "margin_setting", "page_size_setting", "password_prompt", "read_clipboard",
           "skipped_message"]
