"""Autosave for crash recovery – local files only.

While a document has unsaved changes the UI periodically writes a copy into
a private folder on this computer (``%LOCALAPPDATA%\\Folio PDF\\Gendannelse``
on Windows). The original file is never touched. Saving or closing the
document normally deletes its copy, so copies only survive a crash (or a
killed process); the next start offers to restore them.

Each running Folio writes a *session* file with its process id; entries
of a session whose process is gone are orphans that can be restored.
Copies of password-protected documents are written encrypted with the same
password, so restoring them asks for it again.

Thread safety: :meth:`RecoveryStore.write` only does file I/O and may run in
a worker thread; serialise the document first on its own thread
(:meth:`pdfstudio.core.document.PdfDocument.recovery_bytes`).
"""

from __future__ import annotations

import json
import os
import sys
import uuid
from dataclasses import dataclass
from datetime import datetime

from .document import write_atomic

APP_FOLDER = "Folio PDF"
RECOVERY_FOLDER = "Gendannelse"


def default_folder() -> str:
    if sys.platform == "win32":
        base = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~\\AppData\\Local")
    else:
        base = os.environ.get("XDG_DATA_HOME") or os.path.expanduser("~/.local/share")
    return os.path.join(base, APP_FOLDER, RECOVERY_FOLDER)


def pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    if pid == os.getpid():
        return True
    if sys.platform == "win32":
        try:
            import ctypes

            kernel32 = ctypes.windll.kernel32
            handle = kernel32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
            if not handle:
                return False
            try:
                code = ctypes.c_ulong()
                if not kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
                    return False
                return code.value == 259  # STILL_ACTIVE
            finally:
                kernel32.CloseHandle(handle)
        except Exception:
            return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


@dataclass
class RecoveryEntry:
    id: str
    pdf_file: str                 # the copy in the recovery folder
    original_path: str | None     # where the document came from (None: never saved)
    title: str
    saved_at: datetime
    protected: bool               # the copy needs the document's password
    session: str


class RecoveryStore:
    def __init__(self, folder: str | None = None) -> None:
        self.folder = folder or default_folder()
        self.session = uuid.uuid4().hex
        self._session_file = os.path.join(self.folder, f"session-{self.session}.json")
        self._started = False

    # -- session -----------------------------------------------------------------
    def _start(self) -> None:
        if self._started:
            return
        os.makedirs(self.folder, exist_ok=True)
        with open(self._session_file, "w", encoding="utf-8") as fh:
            json.dump({"pid": os.getpid(), "started": datetime.now().isoformat()}, fh)
        self._started = True

    def close(self) -> None:
        """Clean shutdown: remove this session's entries and session file."""
        for e in self._entries():
            if e.session == self.session:
                self.discard(e)
        try:
            os.remove(self._session_file)
        except OSError:
            pass
        self._started = False

    def _session_alive(self, session: str) -> bool:
        if session == self.session:
            return True
        path = os.path.join(self.folder, f"session-{session}.json")
        try:
            with open(path, encoding="utf-8") as fh:
                pid = int(json.load(fh).get("pid", 0))
        except (OSError, ValueError, TypeError):
            return False
        return pid_alive(pid)

    # -- entries -------------------------------------------------------------------
    @staticmethod
    def new_id() -> str:
        return uuid.uuid4().hex

    def _paths(self, entry_id: str) -> tuple[str, str]:
        return (os.path.join(self.folder, f"{entry_id}.pdf"), os.path.join(self.folder, f"{entry_id}.json"))

    def write(self, entry_id: str, data: bytes, *, original_path: str | None, title: str,
              protected: bool) -> None:
        """Store (or replace) the recovery copy ``entry_id``."""
        self._start()
        pdf_file, meta_file = self._paths(entry_id)
        write_atomic(pdf_file, data)
        meta = {"original_path": original_path, "title": title, "saved_at": datetime.now().isoformat(),
                "protected": bool(protected), "session": self.session}
        tmp = meta_file + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(meta, fh, ensure_ascii=False)
        os.replace(tmp, meta_file)

    def remove(self, entry_id: str) -> None:
        for path in self._paths(entry_id):
            try:
                os.remove(path)
            except OSError:
                pass

    def discard(self, entry: RecoveryEntry) -> None:
        self.remove(entry.id)

    def _entries(self) -> list[RecoveryEntry]:
        if not os.path.isdir(self.folder):
            return []
        out = []
        for name in os.listdir(self.folder):
            if not name.endswith(".json") or name.startswith("session-"):
                continue
            entry_id = name[:-5]
            pdf_file, meta_file = self._paths(entry_id)
            try:
                with open(meta_file, encoding="utf-8") as fh:
                    meta = json.load(fh)
                saved = datetime.fromisoformat(meta["saved_at"])
            except (OSError, ValueError, KeyError, TypeError):
                continue
            if not os.path.isfile(pdf_file):
                continue
            out.append(RecoveryEntry(entry_id, pdf_file, meta.get("original_path"), meta.get("title") or "",
                                     saved, bool(meta.get("protected")), meta.get("session", "")))
        return out

    def orphans(self) -> list[RecoveryEntry]:
        """Copies left behind by sessions that ended without cleaning up
        (crash), newest first. Stale session files are removed."""
        entries = [e for e in self._entries() if not self._session_alive(e.session)]
        if os.path.isdir(self.folder):
            for name in os.listdir(self.folder):
                if name.startswith("session-") and name.endswith(".json"):
                    sid = name[8:-5]
                    if sid != self.session and not self._session_alive(sid):
                        try:
                            os.remove(os.path.join(self.folder, name))
                        except OSError:
                            pass
        return sorted(entries, key=lambda e: e.saved_at, reverse=True)

    def adopt(self, entry: RecoveryEntry) -> None:
        """Make a restored entry part of this session (it is kept until the
        restored document is saved or closed)."""
        self._start()
        _pdf, meta_file = self._paths(entry.id)
        try:
            with open(meta_file, encoding="utf-8") as fh:
                meta = json.load(fh)
            meta["session"] = self.session
            with open(meta_file, "w", encoding="utf-8") as fh:
                json.dump(meta, fh, ensure_ascii=False)
            entry.session = self.session
        except (OSError, ValueError):
            pass


__all__ = ["RecoveryEntry", "RecoveryStore", "default_folder", "pid_alive"]
