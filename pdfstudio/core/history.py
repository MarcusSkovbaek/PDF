"""Snapshot based undo/redo.

Every mutating operation on a :class:`~pdfstudio.core.document.PdfDocument`
runs inside ``document.edit(...)`` which stores a full serialized copy of the
PDF *before* the change. This is simple, robust (works for every kind of
change PyMuPDF can make) and keeps features independent of each other.
Memory is bounded by ``max_bytes`` / ``max_steps``.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class Snapshot:
    label: str
    data: bytes


@dataclass
class History:
    max_steps: int = 60
    max_bytes: int = 400 * 1024 * 1024
    _undo: list[Snapshot] = field(default_factory=list)
    _redo: list[Snapshot] = field(default_factory=list)

    # -- queries ---------------------------------------------------------
    @property
    def can_undo(self) -> bool:
        return bool(self._undo)

    @property
    def can_redo(self) -> bool:
        return bool(self._redo)

    @property
    def undo_label(self) -> str | None:
        return self._undo[-1].label if self._undo else None

    @property
    def redo_label(self) -> str | None:
        return self._redo[-1].label if self._redo else None

    # -- mutation --------------------------------------------------------
    def push(self, label: str, before: bytes) -> None:
        """Record the state *before* a new change. Clears the redo stack."""
        self._undo.append(Snapshot(label, before))
        self._redo.clear()
        self._trim()

    def undo(self, current: bytes) -> Snapshot | None:
        """Return the snapshot to restore; ``current`` goes to the redo stack."""
        if not self._undo:
            return None
        snap = self._undo.pop()
        self._redo.append(Snapshot(snap.label, current))
        return snap

    def redo(self, current: bytes) -> Snapshot | None:
        if not self._redo:
            return None
        snap = self._redo.pop()
        self._undo.append(Snapshot(snap.label, current))
        return snap

    def discard_last(self) -> None:
        """Drop the most recent undo entry (used when an edit is rolled back)."""
        if self._undo:
            self._undo.pop()

    def clear(self) -> None:
        self._undo.clear()
        self._redo.clear()

    def _trim(self) -> None:
        while len(self._undo) > self.max_steps:
            self._undo.pop(0)
        while len(self._undo) > 1 and sum(len(s.data) for s in self._undo) > self.max_bytes:
            self._undo.pop(0)
