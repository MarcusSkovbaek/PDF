"""Text selection tool: drag across words (multi-page), double-click a word,
Ctrl+A selects everything, Ctrl+C copies."""

from __future__ import annotations

from dataclasses import dataclass

import pymupdf
from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import QColor, QGuiApplication, QPainterPath, QPen
from PySide6.QtWidgets import QGraphicsPathItem

from ....i18n import tr
from ...theme import tokens as T
from .base import Tool, ToolMouseEvent, ToolRegistry


@dataclass
class Word:
    rect: QRectF       # display coordinates
    text: str
    line: tuple[int, int]   # (block, line)


def page_words(doc, index: int) -> list[Word]:
    page = doc[index]
    mat = page.rotation_matrix
    out: list[Word] = []
    for x0, y0, x1, y1, text, block, line, _wn in page.get_text("words", sort=False):
        r = (pymupdf.Rect(x0, y0, x1, y1) * mat).normalize()
        out.append(Word(QRectF(r.x0, r.y0, r.width, r.height), text, (block, line)))
    return out


@ToolRegistry.register
class SelectTool(Tool):
    id = "view.select"
    label = tr("shell.tool.select")
    icon = "cursor"
    shortcut = "V"
    mode = ("view",)
    order = 10
    cursor = Qt.CursorShape.ArrowCursor
    hint = tr("shell.tool.select.hint")

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._words: dict[int, list[Word]] = {}
        self._anchor: tuple[int, int] | None = None
        self._focus: tuple[int, int] | None = None
        self._dragging = False
        self._press_pos: QPointF | None = None
        self._items: list[QGraphicsPathItem] = []

    # -- data ------------------------------------------------------------
    def words(self, page: int) -> list[Word]:
        w = self._words.get(page)
        if w is None:
            try:
                w = page_words(self.ctx.pdf.doc, page)
            except Exception:
                w = []
            self._words[page] = w
        return w

    def document_changed(self, _event) -> None:
        self._words.clear()
        self.clear_selection()

    def deactivate(self) -> None:
        self.clear_selection()
        super().deactivate()

    # -- hit testing -------------------------------------------------------
    def _word_at(self, page: int, pos: QPointF, exact: bool = False) -> int:
        words = self.words(page)
        if not words:
            return -1
        for i, w in enumerate(words):
            if w.rect.adjusted(-1, -1, 1, 1).contains(pos):
                return i
        if exact:
            return -1
        # same line band: closest horizontally
        best, bd = -1, float("inf")
        for i, w in enumerate(words):
            if w.rect.top() - 2 <= pos.y() <= w.rect.bottom() + 2:
                d = 0 if w.rect.left() <= pos.x() <= w.rect.right() else min(
                    abs(pos.x() - w.rect.left()), abs(pos.x() - w.rect.right()))
                if d < bd:
                    best, bd = i, d
        if best >= 0:
            return best
        for i, w in enumerate(words):
            c = w.rect.center()
            d = (c.x() - pos.x()) ** 2 * 0.25 + (c.y() - pos.y()) ** 2
            if d < bd:
                best, bd = i, d
        return best

    # -- events ------------------------------------------------------------
    def mouse_press(self, ev: ToolMouseEvent) -> None:
        if ev.button != Qt.MouseButton.LeftButton:
            return
        self.clear_selection()
        if ev.page < 0:
            return
        idx = self._word_at(ev.page, ev.pos)
        if idx < 0:
            return
        self._anchor = (ev.page, idx)
        self._focus = None
        self._dragging = True
        self._press_pos = ev.view_pos

    def mouse_move(self, ev: ToolMouseEvent) -> None:
        view = ev.view
        if self._dragging and self._anchor is not None:
            if self._press_pos is not None and (ev.view_pos - self._press_pos).manhattanLength() < 3:
                return
            page, local = view.page_at(ev.scene_pos, nearest=True)
            idx = self._word_at(page, local)
            if idx >= 0:
                self._focus = (page, idx)
                self._paint()
            return
        if ev.page >= 0 and self._word_at(ev.page, ev.pos, exact=True) >= 0:
            view.set_tool_cursor(Qt.CursorShape.IBeamCursor)
        else:
            view.set_tool_cursor(Qt.CursorShape.ArrowCursor)

    def mouse_release(self, ev: ToolMouseEvent) -> None:
        self._dragging = False
        if self._focus is None:
            self._anchor = None

    def mouse_double_click(self, ev: ToolMouseEvent) -> None:
        if ev.page < 0:
            return
        idx = self._word_at(ev.page, ev.pos, exact=True)
        if idx >= 0:
            self._anchor = (ev.page, idx)
            self._focus = (ev.page, idx)
            self._dragging = False
            self._paint()

    def key_press(self, ev) -> bool:
        if ev.key() == Qt.Key.Key_Escape and self.has_selection():
            self.clear_selection()
            return True
        return False

    # -- selection -----------------------------------------------------------
    def has_selection(self) -> bool:
        return self._anchor is not None and self._focus is not None

    def selection_range(self) -> tuple[tuple[int, int], tuple[int, int]] | None:
        if not self.has_selection():
            return None
        a, b = sorted([self._anchor, self._focus])
        return a, b

    def selected_words(self) -> dict[int, list[Word]]:
        rng = self.selection_range()
        if rng is None:
            return {}
        (p0, i0), (p1, i1) = rng
        out: dict[int, list[Word]] = {}
        for p in range(p0, p1 + 1):
            words = self.words(p)
            if not words:
                continue
            lo = i0 if p == p0 else 0
            hi = i1 if p == p1 else len(words) - 1
            if lo <= hi:
                out[p] = words[lo:hi + 1]
        return out

    def selected_text(self) -> str:
        parts: list[str] = []
        for _p, words in sorted(self.selected_words().items()):
            line = None
            buf: list[str] = []
            for w in words:
                if line is not None and w.line != line:
                    parts.append(" ".join(buf))
                    buf = []
                buf.append(w.text)
                line = w.line
            if buf:
                parts.append(" ".join(buf))
        return "\n".join(parts)

    def select_all(self) -> bool:
        n = self.ctx.pdf.page_count
        first = next((p for p in range(n) if self.words(p)), None)
        if first is None:
            return False
        last = next(p for p in range(n - 1, -1, -1) if self.words(p))
        self._anchor = (first, 0)
        self._focus = (last, len(self.words(last)) - 1)
        self._paint()
        return True

    def copy(self) -> bool:
        text = self.selected_text()
        if not text:
            return False
        QGuiApplication.clipboard().setText(text)
        if self.ctx and self.ctx.toast:
            self.ctx.toast(tr("shell.select.copied"), "success")
        return True

    def selection_brush(self) -> QColor:
        """Fill of the live selection (markup tools preview their colour)."""
        return T.qcolor("text_selection")

    def line_rects(self) -> dict[int, list[QRectF]]:
        """Selected words merged per text line: {page: [display rect, ...]}."""
        out: dict[int, list[QRectF]] = {}
        for p, words in self.selected_words().items():
            bands: dict[tuple[int, int], QRectF] = {}
            for w in words:
                bands[w.line] = bands[w.line].united(w.rect) if w.line in bands else QRectF(w.rect)
            out[p] = list(bands.values())
        return out

    def clear_selection(self) -> None:
        had = self.has_selection()
        self._anchor = None
        self._focus = None
        self._remove_items()
        if had:
            self.changed.emit()

    def _remove_items(self) -> None:
        if self.ctx is None:
            self._items = []
            return
        for it in self._items:
            try:
                self.ctx.view.remove_overlay(it)
            except Exception:
                pass
        self._items = []

    def _paint(self) -> None:
        self._remove_items()
        view = self.ctx.view
        brush = self.selection_brush()
        for p, words in self.selected_words().items():
            path = QPainterPath()
            # merge words on one line into a single band
            bands: dict[tuple[int, int], QRectF] = {}
            order: list[tuple[int, int]] = []
            for w in words:
                if w.line in bands:
                    bands[w.line] = bands[w.line].united(w.rect)
                else:
                    bands[w.line] = QRectF(w.rect)
                    order.append(w.line)
            for key in order:
                path.addRect(bands[key].adjusted(-0.5, -0.5, 0.5, 0.5))
            item = QGraphicsPathItem(path)
            item.setBrush(brush)
            item.setPen(QPen(Qt.PenStyle.NoPen))
            item.setZValue(8)
            try:
                view.add_overlay(p, item)
                self._items.append(item)
            except IndexError:
                pass
        self.changed.emit()
