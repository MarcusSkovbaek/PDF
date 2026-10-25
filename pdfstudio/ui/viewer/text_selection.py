"""Character-precise text selection shared by the tools.

:class:`TextSelection` keeps an anchor and a focus ``(page, caret)`` (see
:mod:`pdfstudio.core.textsel`) and paints the selected lines on the pages.
Click-drag selects characters, a double click a word, a triple click a
paragraph (dragging after a double/triple click extends by words /
paragraphs, like Word), Shift+click extends. Selections may span pages.

Tools own one instance and forward their mouse events
(:meth:`press` / :meth:`drag` / :meth:`release`); :class:`SelectionBar`
is the floating mini toolbar shown next to a finished selection.
"""

from __future__ import annotations

import pymupdf
from PySide6.QtCore import QObject, QPointF, QRectF, Qt, Signal
from PySide6.QtGui import QColor, QPainterPath, QPen
from PySide6.QtWidgets import QFrame, QGraphicsPathItem, QHBoxLayout, QWidget

from ...core import textsel
from ...i18n import tr
from ..theme import tokens as T
from ..widgets.util import IconButton, add_shadow, divider

Pos = tuple[int, int]  # (page, caret)

#: A press closer than this (pt) to text starts a text selection.
TEXT_REACH = 2.5


class TextSelection(QObject):
    changed = Signal()

    def __init__(self, tool, brush=None) -> None:
        super().__init__(tool)
        self.tool = tool
        self._brush = brush
        self._texts: dict[int, textsel.PageText] = {}
        self._revision = None
        self.anchor: Pos | None = None
        self.focus: Pos | None = None
        self._unit = "char"
        self._unit_anchor: tuple[Pos, Pos] | None = None  # the word/paragraph first clicked
        self.dragging = False
        self._press_view: QPointF | None = None
        self._items: list[QGraphicsPathItem] = []

    # -- data -------------------------------------------------------------------
    @property
    def ctx(self):
        return self.tool.ctx

    def page_text(self, page: int) -> textsel.PageText:
        pdf = self.ctx.pdf
        rev = getattr(pdf, "revision", None)
        if rev != self._revision:
            self._texts.clear()
            self._revision = rev
        pt = self._texts.get(page)
        if pt is None:
            try:
                pt = textsel.page_text(pdf.doc, page)
            except Exception:
                pt = textsel.PageText(page)
            self._texts[page] = pt
        return pt

    def invalidate(self) -> None:
        self._texts.clear()
        self._revision = None

    def over_text(self, page: int, pos: QPointF) -> bool:
        return page >= 0 and textsel.char_at(self.page_text(page), (pos.x(), pos.y()), TEXT_REACH) is not None

    def _caret(self, page: int, pos: QPointF) -> int | None:
        return textsel.hit(self.page_text(page), (pos.x(), pos.y()))

    # -- mouse ------------------------------------------------------------------
    def press(self, ev, *, on_text: bool = False) -> bool:
        """Start (or extend, with Shift) a selection. Returns False when the
        page has no text there to select (with ``on_text``: when the press is
        not on a character – tools that drag other things elsewhere)."""
        if ev.page < 0:
            return False
        if on_text and not self.over_text(ev.page, ev.pos) and not (
                ev.modifiers & Qt.KeyboardModifier.ShiftModifier and self.has_selection()):
            return False
        caret = self._caret(ev.page, ev.pos)
        if caret is None:
            return False
        pos = (ev.page, caret)
        pt = self.page_text(ev.page)
        if ev.modifiers & Qt.KeyboardModifier.ShiftModifier and self.anchor is not None:
            self.focus = pos
            self._unit = "char"
        elif ev.click_count >= 3:
            a, b = textsel.paragraph_bounds(pt, min(caret, len(pt) - 1))
            self._set_unit("para", (ev.page, a), (ev.page, b))
        elif ev.click_count == 2:
            idx = textsel.char_at(pt, (ev.pos.x(), ev.pos.y()), TEXT_REACH)
            idx = idx if idx is not None else min(caret, len(pt) - 1)
            a, b = textsel.word_bounds(pt, idx)
            self._set_unit("word", (ev.page, a), (ev.page, b))
        else:
            self.anchor, self.focus = pos, None
            self._unit, self._unit_anchor = "char", None
        self.dragging = True
        self._press_view = QPointF(ev.view_pos)
        self.paint()
        return True

    def _set_unit(self, unit: str, a: Pos, b: Pos) -> None:
        self._unit, self._unit_anchor = unit, (a, b)
        self.anchor, self.focus = a, b

    def drag(self, ev) -> None:
        if not self.dragging or self.anchor is None:
            return
        if self._unit == "char" and self._press_view is not None and \
                (ev.view_pos - self._press_view).manhattanLength() < 3:
            return
        page, local = ev.view.page_at(ev.scene_pos, nearest=True)
        if page < 0:
            return
        caret = self._caret(page, local)
        if caret is None:
            return
        pos = (page, caret)
        if self._unit == "char" or self._unit_anchor is None:
            self.focus = pos
        else:
            a, b = self._unit_anchor
            pt = self.page_text(page)
            bounds = textsel.word_bounds if self._unit == "word" else textsel.paragraph_bounds
            if pos >= b:
                end = bounds(pt, max(0, caret - 1)) if caret > 0 else (caret, caret)
                self.anchor, self.focus = a, (page, max(end[1], caret))
            elif pos <= a:
                start = bounds(pt, min(caret, len(pt) - 1)) if len(pt) else (caret, caret)
                self.anchor, self.focus = b, (page, start[0])
            else:
                self.anchor, self.focus = a, b
        self.paint()

    def release(self, ev=None) -> None:
        self.dragging = False
        if self.focus is None or self.focus == self.anchor:
            if self._unit == "char":
                self.focus = None

    # -- queries ----------------------------------------------------------------
    def has_selection(self) -> bool:
        return self.anchor is not None and self.focus is not None and self.anchor != self.focus

    def ordered(self) -> tuple[Pos, Pos] | None:
        if not self.has_selection():
            return None
        a, b = sorted([self.anchor, self.focus])
        return a, b

    def ranges(self) -> dict[int, tuple[int, int]]:
        """``{page: (a, b)}`` caret ranges of the selection."""
        rng = self.ordered()
        if rng is None:
            return {}
        (p0, i0), (p1, i1) = rng
        out = {}
        for p in range(p0, p1 + 1):
            n = len(self.page_text(p))
            a = i0 if p == p0 else 0
            b = i1 if p == p1 else n
            if a < b:
                out[p] = (a, b)
        return out

    def rects(self) -> dict[int, list[pymupdf.Rect]]:
        """``{page: [display rect per line]}`` – what markup tools mark."""
        out = {}
        for p, (a, b) in self.ranges().items():
            rs = textsel.selection_rects(self.page_text(p), a, b)
            if rs:
                out[p] = rs
        return out

    def text(self, line_sep: str = "\n") -> str:
        parts = [self.page_text(p).text_between(a, b, line_sep=line_sep) for p, (a, b) in self.ranges().items()]
        return "\n".join(x for x in parts if x)

    def caret_at(self, page: int, pos: QPointF) -> Pos | None:
        c = self._caret(page, pos) if page >= 0 else None
        return None if c is None else (page, c)

    def select_all(self) -> bool:
        n = self.ctx.pdf.page_count
        first = next((p for p in range(n) if len(self.page_text(p))), None)
        if first is None:
            return False
        last = next(p for p in range(n - 1, -1, -1) if len(self.page_text(p)))
        self.anchor, self.focus = (first, 0), (last, len(self.page_text(last)))
        self._unit = "char"
        self.paint()
        return True

    def end_rect(self) -> tuple[int, pymupdf.Rect] | None:
        """Page and display rect of the selection's last line (bar placement)."""
        rects = self.rects()
        if not rects:
            return None
        page = max(rects)
        return page, rects[page][-1]

    # -- painting -----------------------------------------------------------------
    def brush(self) -> QColor:
        return self._brush() if callable(self._brush) else T.qcolor("text_selection")

    def clear(self) -> None:
        had = self.has_selection()
        self.anchor = self.focus = None
        self._unit, self._unit_anchor = "char", None
        self.dragging = False
        self._remove_items()
        if had:
            self.changed.emit()

    def _remove_items(self) -> None:
        if self.ctx is not None:
            for it in self._items:
                try:
                    self.ctx.view.remove_overlay(it)
                except Exception:
                    pass
        self._items = []

    def paint(self) -> None:
        self._remove_items()
        if self.ctx is None:
            return
        brush = self.brush()
        for p, rects in self.rects().items():
            path = QPainterPath()
            for r in rects:
                path.addRect(QRectF(r.x0, r.y0, r.width, r.height).adjusted(-0.4, -0.4, 0.4, 0.4))
            item = QGraphicsPathItem(path)
            item.setBrush(brush)
            item.setPen(QPen(Qt.PenStyle.NoPen))
            item.setZValue(8)
            try:
                self.ctx.view.add_overlay(p, item)
                self._items.append(item)
            except IndexError:
                pass
        self.changed.emit()


# -- floating mini toolbar ------------------------------------------------------------
BAR_ACTIONS = (
    ("comment", "chat-circle-text", "sel.comment"),
    None,
    ("highlight", "highlighter-circle", "sel.highlight"),
    ("underline", "text-underline", "sel.underline"),
    ("strikeout", "text-strikethrough", "sel.strikeout"),
    ("replace", "swap", "sel.replace"),
    None,
    ("copy", "copy", "sel.copy"),
)


class SelectionBar(QFrame):
    """Acrobat-style pill next to a text selection: Kommentér, Fremhæv,
    Understreg, Gennemstreg, Erstat tekst, Kopiér."""

    triggered = Signal(str)

    def __init__(self, viewport: QWidget) -> None:
        super().__init__(viewport)
        self.setObjectName("FloatingBar")
        lay = QHBoxLayout(self)
        lay.setContentsMargins(4, 4, 4, 4)
        lay.setSpacing(2)
        self.buttons: dict[str, IconButton] = {}
        for entry in BAR_ACTIONS:
            if entry is None:
                sep = divider("v")
                sep.setFixedHeight(18)
                lay.addSpacing(2)
                lay.addWidget(sep, 0, Qt.AlignmentFlag.AlignVCenter)
                lay.addSpacing(2)
                continue
            key, ic, text = entry
            shortcut = "Ctrl+Alt+M" if key == "comment" else ("Ctrl+C" if key == "copy" else None)
            b = IconButton(ic, tr(text), variant="icon", icon_size=17, shortcut=shortcut)
            b.setFocusPolicy(Qt.FocusPolicy.NoFocus)
            b.setAccessibleName(f"sel.{key}")
            b.clicked.connect(lambda _c=False, k=key: self.triggered.emit(k))
            lay.addWidget(b)
            self.buttons[key] = b
        add_shadow(self, blur=22, y=5, role="shadow")
        self.hide()

    def show_at(self, view, page: int, rect: pymupdf.Rect) -> None:
        """Place the bar under the selection's last line (above it when there
        is no room), kept inside the viewport."""
        self.adjustSize()
        vp = view.viewport()
        bottom = view.page_to_viewport(page, QPointF(rect.x1, rect.y1))
        top = view.page_to_viewport(page, QPointF(rect.x1, rect.y0))
        left = view.page_to_viewport(page, QPointF(rect.x0, rect.y1))
        w, h = self.width(), self.height()
        x = min(bottom.x(), max(left.x(), bottom.x() - w / 2)) - w / 2 + 12
        y = bottom.y() + 8
        if y + h > vp.height() - 8:
            y = top.y() - h - 8
        x = max(8, min(vp.width() - w - 8, x))
        y = max(8, min(vp.height() - h - 8, y))
        self.move(int(x), int(y))
        self.show()
        self.raise_()


__all__ = ["SelectionBar", "TextSelection"]
