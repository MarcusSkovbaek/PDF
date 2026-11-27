"""Document tab bar: closable tabs with a dirty dot, middle-click close, reorder."""

from __future__ import annotations

from PySide6.QtCore import QPointF, QRectF, QSize, Qt, Signal
from PySide6.QtGui import QPainter, QPainterPath, QPen
from PySide6.QtWidgets import QAbstractButton, QTabBar

from ..theme import tokens as T


class TabCloseButton(QAbstractButton):
    def __init__(self, bar: "DocTabBar") -> None:
        super().__init__(bar)
        self.bar = bar
        self.dirty = False
        self.setFixedSize(20, 20)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.setMouseTracking(True)

    def sizeHint(self) -> QSize:  # noqa: N802
        return QSize(20, 20)

    def enterEvent(self, ev) -> None:  # noqa: N802
        self.update()
        super().enterEvent(ev)

    def leaveEvent(self, ev) -> None:  # noqa: N802
        self.update()
        super().leaveEvent(ev)

    def _tab_index(self) -> int:
        for i in range(self.bar.count()):
            if self.bar.tabButton(i, QTabBar.ButtonPosition.RightSide) is self:
                return i
        return -1

    def paintEvent(self, _ev) -> None:  # noqa: N802
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        idx = self._tab_index()
        hovered = self.underMouse()
        tab_active = idx == self.bar.currentIndex() or idx == self.bar.hover_index
        r = QRectF(self.rect()).adjusted(1, 1, -1, -1)
        if self.dirty and not hovered:
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(T.qcolor("text_muted" if tab_active else "text_subtle"))
            p.drawEllipse(r.center(), 3.6, 3.6)
            p.end()
            return
        if not (hovered or tab_active):
            p.end()
            return
        if hovered:
            bg = QPainterPath()
            bg.addRoundedRect(r, 5, 5)
            p.fillPath(bg, T.qcolor("pressed" if self.isDown() else "hover"))
        pen = QPen(T.qcolor("text" if hovered else "text_subtle"), 1.4)
        pen.setCapStyle(Qt.PenCapStyle.RoundCap)
        p.setPen(pen)
        c = r.center()
        d = 3.6
        p.drawLine(QPointF(c.x() - d, c.y() - d), QPointF(c.x() + d, c.y() + d))
        p.drawLine(QPointF(c.x() - d, c.y() + d), QPointF(c.x() + d, c.y() - d))
        p.end()


class DocTabBar(QTabBar):
    closeRequested = Signal(int)

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setObjectName("DocTabs")
        self.setMovable(True)
        self.setDocumentMode(True)
        self.setDrawBase(False)
        self.setExpanding(False)
        self.setUsesScrollButtons(True)
        self.setElideMode(Qt.TextElideMode.ElideMiddle)
        self.setIconSize(QSize(16, 16))
        self.setMouseTracking(True)
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.hover_index = -1
        self.currentChanged.connect(lambda _i: self._update_buttons())

    def add_tab(self, icon, text: str, key: str, closable: bool = True) -> int:
        i = self.addTab(icon, text)
        self.setTabData(i, key)
        if closable:
            btn = TabCloseButton(self)
            btn.clicked.connect(lambda _=False, b=btn: self._close_clicked(b))
            self.setTabButton(i, QTabBar.ButtonPosition.RightSide, btn)
        return i

    def index_of(self, key: str) -> int:
        for i in range(self.count()):
            if self.tabData(i) == key:
                return i
        return -1

    def set_dirty(self, i: int, dirty: bool) -> None:
        btn = self.tabButton(i, QTabBar.ButtonPosition.RightSide)
        if isinstance(btn, TabCloseButton) and btn.dirty != dirty:
            btn.dirty = dirty
            btn.update()

    def _close_clicked(self, btn) -> None:
        for i in range(self.count()):
            if self.tabButton(i, QTabBar.ButtonPosition.RightSide) is btn:
                self.closeRequested.emit(i)
                return

    def _update_buttons(self) -> None:
        for i in range(self.count()):
            b = self.tabButton(i, QTabBar.ButtonPosition.RightSide)
            if b is not None:
                b.update()

    def mouseMoveEvent(self, ev) -> None:  # noqa: N802
        h = self.tabAt(ev.position().toPoint())
        if h != self.hover_index:
            self.hover_index = h
            self._update_buttons()
        super().mouseMoveEvent(ev)

    def leaveEvent(self, ev) -> None:  # noqa: N802
        self.hover_index = -1
        self._update_buttons()
        super().leaveEvent(ev)

    def mouseReleaseEvent(self, ev) -> None:  # noqa: N802
        if ev.button() == Qt.MouseButton.MiddleButton:
            i = self.tabAt(ev.position().toPoint())
            if i >= 0:
                self.closeRequested.emit(i)
                return
        super().mouseReleaseEvent(ev)

    def tabSizeHint(self, index: int) -> QSize:  # noqa: N802
        s = super().tabSizeHint(index)
        return QSize(min(max(s.width(), 120), 240), 34)
