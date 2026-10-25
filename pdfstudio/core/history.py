"""Snapshot based undo/redo.

Every mutating operation on a :class:`~pdfstudio.core.document.PdfDocument`
runs inside ``document.edit(...)`` which stores a full serialized copy of the
PDF *before* the change. This is simple, robust (works for every kind of
change PyMuPDF can make) and keeps features independent of each other.

Snapshots are stored *deduplicated*: a serialized PDF is a sequence of
``N 0 obj … endobj`` objects, and one edit changes only a few of them (a
page's content stream, a new font). Each snapshot is kept as a list of
object chunks in a shared, reference-counted pool, so unchanged objects –
fonts, images, other pages – exist once no matter how many steps there
are; big chunks are compressed. Memory is bounded by ``max_bytes`` (of
unique, stored data) / ``max_steps``.
"""

from __future__ import annotations

import hashlib
import zlib
from dataclasses import dataclass, field

#: chunks at least this big are stored compressed
COMPRESS_FROM = 2048
_SEP = b"endobj"


class _Pool:
    """Reference-counted store of byte chunks keyed by their hash."""

    def __init__(self) -> None:
        self._chunks: dict[bytes, list] = {}   # key -> [stored bytes, compressed?, refcount]
        self.size = 0                           # bytes actually held

    def put(self, data: bytes) -> tuple[bytes, ...]:
        parts = data.split(_SEP)
        keys = []
        last = len(parts) - 1
        for i, part in enumerate(parts):
            chunk = part + _SEP if i < last else part
            key = hashlib.blake2b(chunk, digest_size=16).digest()
            entry = self._chunks.get(key)
            if entry is None:
                if len(chunk) >= COMPRESS_FROM:
                    packed = zlib.compress(chunk, 1)
                    entry = [packed, True, 0] if len(packed) < len(chunk) else [chunk, False, 0]
                else:
                    entry = [chunk, False, 0]
                self._chunks[key] = entry
                self.size += len(entry[0])
            entry[2] += 1
            keys.append(key)
        return tuple(keys)

    def get(self, keys: tuple[bytes, ...]) -> bytes:
        out = []
        for k in keys:
            stored, packed, _n = self._chunks[k]
            out.append(zlib.decompress(stored) if packed else stored)
        return b"".join(out)

    def release(self, keys: tuple[bytes, ...]) -> None:
        for k in keys:
            entry = self._chunks.get(k)
            if entry is None:
                continue
            entry[2] -= 1
            if entry[2] <= 0:
                self.size -= len(entry[0])
                del self._chunks[k]


@dataclass
class Snapshot:
    label: str
    keys: tuple
    pool: _Pool = field(repr=False)

    @property
    def data(self) -> bytes:
        return self.pool.get(self.keys)


@dataclass
class History:
    max_steps: int = 60
    max_bytes: int = 400 * 1024 * 1024
    _undo: list[Snapshot] = field(default_factory=list)
    _redo: list[Snapshot] = field(default_factory=list)
    _pool: _Pool = field(default_factory=_Pool, repr=False)

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

    @property
    def memory(self) -> int:
        """Bytes held by all snapshots together."""
        return self._pool.size

    # -- mutation --------------------------------------------------------
    def _snap(self, label: str, data: bytes) -> Snapshot:
        return Snapshot(label, self._pool.put(data), self._pool)

    def push(self, label: str, before: bytes) -> None:
        """Record the state *before* a new change. Clears the redo stack."""
        self._undo.append(self._snap(label, before))
        for s in self._redo:
            self._pool.release(s.keys)
        self._redo.clear()
        self._trim()

    def undo(self, current: bytes) -> Snapshot | None:
        """Return the snapshot to restore; ``current`` goes to the redo stack."""
        if not self._undo:
            return None
        snap = self._undo.pop()
        self._redo.append(self._snap(snap.label, current))
        return _Detached(snap.label, snap.data, snap, self._pool)

    def redo(self, current: bytes) -> Snapshot | None:
        if not self._redo:
            return None
        snap = self._redo.pop()
        self._undo.append(self._snap(snap.label, current))
        return _Detached(snap.label, snap.data, snap, self._pool)

    def discard_last(self) -> None:
        """Drop the most recent undo entry (used when an edit is rolled back)."""
        if self._undo:
            self._pool.release(self._undo.pop().keys)

    def clear(self) -> None:
        for s in self._undo + self._redo:
            self._pool.release(s.keys)
        self._undo.clear()
        self._redo.clear()

    def _trim(self) -> None:
        while len(self._undo) > self.max_steps:
            self._pool.release(self._undo.pop(0).keys)
        while len(self._undo) > 1 and self._pool.size > self.max_bytes:
            self._pool.release(self._undo.pop(0).keys)


class _Detached:
    """A snapshot taken off a stack: its data is read once, then its chunks
    are released (the caller restores the document from ``data``)."""

    def __init__(self, label: str, data: bytes, snap: Snapshot, pool: _Pool) -> None:
        self.label = label
        self.data = data
        pool.release(snap.keys)
