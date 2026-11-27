"""Saving documents from the UI: checks, background writing, autosave.

:class:`SaveController` (one per main window) owns everything around
:meth:`pdfstudio.core.document.PdfDocument.prepare_save`:

* before writing: was the file changed by another program since it was
  opened (Overskriv / Gem som… / Annullér)? Is it write protected or open in
  another program (then offer Gem som…)? Does it carry digital signatures
  that the change will invalidate (asked once per document)?
* the document is serialised on the UI thread (PyMuPDF keeps the GIL), then
  the file is written by a worker thread while a nested event loop keeps the
  window responsive; a "Gemmer…" indicator shows in the status bar when it
  takes noticeable time. Edits made meanwhile stay unsaved (dirty);
* autosave: every N minutes (setting, default 2) every document with unsaved
  changes gets a recovery copy in a local folder (never the original);
  saving or closing removes it; after a crash :meth:`check_recovery` offers
  to restore them. All recovery file operations run in order on one worker
  thread.
"""

from __future__ import annotations

import os
from concurrent.futures import ThreadPoolExecutor

from PySide6.QtCore import QEventLoop, QObject, QThread, QTimer, Signal

from ..core import DocumentError, InvalidPassword, PasswordRequired, PdfDocument
from ..core.document import _stat, check_writable, write_atomic
from ..core.recovery import RecoveryEntry, RecoveryStore
from ..i18n import tr

AUTOSAVE_KEY = "save/autosave_minutes"
WARN_EXTERNAL_KEY = "save/warn_external"
DEFAULT_AUTOSAVE_MIN = 2
#: Show the "Gemmer…" indicator right away for documents this big (pages).
BIG_DOCUMENT = 60


class _Writer(QThread):
    """Writes bytes to a file atomically (pure I/O, no document access)."""

    def __init__(self, path: str, data: bytes, parent=None) -> None:
        super().__init__(parent)
        self.path, self.data = path, data
        self.error: Exception | None = None

    def run(self) -> None:
        try:
            write_atomic(self.path, self.data)
        except Exception as exc:  # noqa: BLE001 - reported on the UI thread
            self.error = exc


class SaveController(QObject):
    busyChanged = Signal(str)   # "" when idle

    def __init__(self, window) -> None:
        super().__init__(window)
        from .app import recovery_dir

        self.win = window
        self.store = RecoveryStore(recovery_dir())
        self._io = ThreadPoolExecutor(max_workers=1, thread_name_prefix="folio-recovery")
        self._saving: set[int] = set()
        self._ids: dict[int, str] = {}           # id(workspace) -> recovery entry id
        self._autosaved: dict[int, int] = {}     # id(workspace) -> revision written
        self._busy_timer = QTimer(self)
        self._busy_timer.setSingleShot(True)
        self._busy_timer.timeout.connect(self._show_busy)
        self._busy_text = ""
        self.timer = QTimer(self)
        self.timer.timeout.connect(self.autosave_now)
        self.apply_settings()

    # -- settings ----------------------------------------------------------------
    def autosave_minutes(self) -> int:
        try:
            return max(0, int(self.win.settings.value(AUTOSAVE_KEY, DEFAULT_AUTOSAVE_MIN)))
        except (TypeError, ValueError):
            return DEFAULT_AUTOSAVE_MIN

    def warn_external(self) -> bool:
        v = self.win.settings.value(WARN_EXTERNAL_KEY, True)
        return v not in (False, "false", "0", 0)

    def set_settings(self, minutes: int, warn_external: bool) -> None:
        self.win.settings.setValue(AUTOSAVE_KEY, max(0, int(minutes)))
        self.win.settings.setValue(WARN_EXTERNAL_KEY, bool(warn_external))
        self.apply_settings()

    def apply_settings(self) -> None:
        minutes = self.autosave_minutes()
        if minutes:
            self.timer.start(minutes * 60_000)
        else:
            self.timer.stop()

    # -- state -------------------------------------------------------------------------
    @property
    def busy(self) -> bool:
        return bool(self._saving)

    def is_saving(self, ws) -> bool:
        return id(ws) in self._saving

    # -- saving ------------------------------------------------------------------------
    def save(self, ws) -> bool:
        if not ws.pdf.path:
            return self.win.save_as(ws)  # the window's method: one entry point for "Gem som"
        return self._save_to(ws, ws.pdf.path)

    def save_as(self, ws, path: str | None = None) -> bool:
        path = path or self._ask_path(ws, tr("shell.file.save_as_title"), ws.pdf.path)
        return bool(path) and self._save_to(ws, path)

    def save_copy(self, ws, path: str | None = None) -> bool:
        if path is None:
            stem = os.path.splitext(ws.pdf.display_name)[0]
            folder = os.path.dirname(ws.pdf.path) if ws.pdf.path else None
            path = self._ask_path(ws, tr("save.copy.title"), None,
                                  name=tr("save.copy.name", name=stem) + ".pdf", folder=folder)
        return bool(path) and self._save_to(ws, path, copy=True)

    def _ask_path(self, ws, title: str, current: str | None, *, name: str | None = None,
                  folder: str | None = None) -> str | None:
        from PySide6.QtWidgets import QFileDialog

        from .app import last_dir

        base = current or os.path.join(folder or last_dir(), name or ws.pdf.display_name)
        path, _ = QFileDialog.getSaveFileName(self.win, title, base, tr("shell.filter.pdf"))
        if not path:
            return None
        if not path.lower().endswith(".pdf"):
            path += ".pdf"
        return path

    def _save_to(self, ws, path: str, *, copy: bool = False) -> bool:
        from .dialogs import common

        pdf = ws.pdf
        if id(ws) in self._saving:
            return False
        target = os.path.abspath(path)
        same = bool(pdf.path) and os.path.normcase(pdf.path) == os.path.normcase(target)
        if same and not copy and self.warn_external() and os.path.exists(target) and pdf.changed_on_disk():
            choice = common.ask_file_changed(self.win, pdf.display_name)
            if choice == "save_as":
                return self.win.save_as(ws)
            if choice != "overwrite":
                return False
        reason = check_writable(target)
        if reason:
            if common.ask_unwritable(self.win, os.path.basename(target), reason) == "save_as":
                return self.save_copy(ws) if copy else self.win.save_as(ws)
            return False
        if not copy and not getattr(pdf, "_signature_ok", False) and pdf.has_signatures():
            if not common.confirm(self.win, tr("save.signed.title"), tr("save.signed.text"), tr("save.signed.ok")):
                return False
            pdf._signature_ok = True  # asked once per document
        self._saving.add(id(ws))
        self._set_busy(tr("save.busy", name=os.path.basename(target)), now=pdf.page_count >= BIG_DOCUMENT)
        try:
            try:
                job = pdf.prepare_save()
            except DocumentError as exc:
                self._error(exc)
                return False
            writer = _Writer(target, job.data, self)
            loop = QEventLoop()
            writer.finished.connect(loop.quit)
            writer.start()
            if not writer.isFinished():
                loop.exec()       # the window stays responsive while the file is written
            writer.wait()
            writer.deleteLater()
            if writer.error is not None:
                self._error(writer.error)
                return False
        finally:
            self._saving.discard(id(ws))
            self._set_busy("")
        from .app import add_recent, set_last_dir

        set_last_dir(os.path.dirname(target))
        if copy:
            self.win.toast(tr("save.copy.done", name=os.path.basename(target)), "success",
                           action_text=tr("save.open"), on_action=lambda p=target: self.win.open_path(p))
            return True
        pdf.finish_save(target, job)
        if not pdf.is_dirty:
            self.forget(ws)
        add_recent(target)
        self.win.toast(tr("shell.saved", name=os.path.basename(target)), "success")
        self.win._update_tab(ws)
        return True

    def _error(self, exc: Exception) -> None:
        from .dialogs.common import show_error

        show_error(self.win, tr("shell.error.save_title"), tr("shell.error.save", err=str(exc)))

    def _set_busy(self, text: str, *, now: bool = False) -> None:
        self._busy_text = text
        if not text:
            self._busy_timer.stop()
            self.busyChanged.emit("")
            return
        if now:
            self._show_busy()
            from PySide6.QtWidgets import QApplication

            QApplication.processEvents()  # paint it before the UI thread serialises
        else:
            self._busy_timer.start(200)

    def _show_busy(self) -> None:
        if self._busy_text:
            self.busyChanged.emit(self._busy_text)

    # -- autosave / recovery --------------------------------------------------------------
    def autosave_now(self) -> int:
        """Write recovery copies of all documents with new unsaved changes.
        Returns how many were written (queued)."""
        n = 0
        for ws in self.win.workspaces():
            pdf = ws.pdf
            key = id(ws)
            if not pdf.is_dirty or key in self._saving or self._autosaved.get(key) == pdf.revision:
                continue
            try:
                data = pdf.recovery_bytes()
            except Exception:  # noqa: BLE001 - autosave must never disturb the user
                continue
            entry = self._ids.setdefault(key, self.store.new_id())
            self._autosaved[key] = pdf.revision
            protected = pdf.security is not None and pdf.security.needs_password
            self._io.submit(self._write_entry, entry, data, pdf.path, pdf.display_name, protected)
            n += 1
        return n

    def _write_entry(self, entry: str, data: bytes, original: str | None, title: str, protected: bool) -> None:
        try:
            self.store.write(entry, data, original_path=original, title=title, protected=protected)
        except Exception:  # noqa: BLE001 - e.g. disk full: the next tick tries again
            pass

    def forget(self, ws) -> None:
        """The document was saved or closed: drop its recovery copy."""
        key = id(ws)
        self._autosaved.pop(key, None)
        entry = self._ids.pop(key, None)
        if entry is not None:
            self._io.submit(self.store.remove, entry)

    def flush(self) -> None:
        """Wait until queued recovery writes are done."""
        self._io.submit(lambda: None).result()

    def shutdown(self) -> None:
        """Clean exit: no copies are kept."""
        self.timer.stop()
        self._io.shutdown(wait=True)
        self.store.close()

    def check_recovery(self) -> list:
        """Offer to restore copies left by a crash. Returns the restored workspaces."""
        try:
            orphans = self.store.orphans()
        except OSError:
            return []
        if not orphans:
            return []
        from .dialogs.common import ask_recover

        choice = ask_recover(self.win, orphans)
        if choice == "discard":
            for e in orphans:
                self.store.discard(e)
            return []
        if choice != "restore":
            return []          # "Senere": offered again next time
        restored = []
        for e in orphans:
            ws = self._restore(e)
            if ws is not None:
                restored.append(ws)
        return restored

    def _restore(self, entry: RecoveryEntry):
        from .dialogs.password import ask_password

        password = None
        wrong = False
        while True:
            try:
                pdf = PdfDocument.open(entry.pdf_file, password)
                break
            except PasswordRequired:
                pass
            except InvalidPassword:
                wrong = True
            except DocumentError as exc:
                self.win.toast(tr("save.recover.failed", name=entry.title, err=str(exc)), "error")
                return None
            password = ask_password(self.win, entry.title, wrong=wrong)
            if password is None:
                return None
        pdf.path = os.path.abspath(entry.original_path) if entry.original_path else None
        pdf._title = entry.title
        pdf._disk_stamp = _stat(pdf.path)
        pdf._dirty = True
        ws = self.win.add_document(pdf)
        self.store.adopt(entry)
        self._ids[id(ws)] = entry.id
        self._autosaved[id(ws)] = pdf.revision
        self.win._update_tab(ws)
        self.win.toast(tr("save.recover.done", name=entry.title), "success")
        return ws


__all__ = ["SaveController"]
