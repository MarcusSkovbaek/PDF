"""Home screen: welcome, drop zone, quick actions and recent files."""

from __future__ import annotations

import os
import time

from PySide6.QtCore import QRectF, QSize, Qt, Signal
from PySide6.QtGui import QFontMetrics, QPainter, QPainterPath, QPen
from PySide6.QtWidgets import (
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from ..i18n import tr
from .theme import tokens as T
from .widgets.brand import BrandMark
from .widgets.util import ThemedIconLabel, label, on_theme_change, set_prop


class DropZone(QFrame):
    clicked = Signal()

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setObjectName("DropZoneCustom")
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setMinimumHeight(176)
        self._hot = False
        self._hover = False
        v = QVBoxLayout(self)
        v.setContentsMargins(24, 28, 24, 28)
        v.setSpacing(6)
        well = QFrame()
        well.setObjectName("IconWell")
        well.setFixedSize(52, 52)
        wl = QVBoxLayout(well)
        wl.setContentsMargins(0, 0, 0, 0)
        wl.addWidget(ThemedIconLabel("upload-simple", 26, "accent_text"), 0, Qt.AlignmentFlag.AlignCenter)
        v.addWidget(well, 0, Qt.AlignmentFlag.AlignHCenter)
        v.addSpacing(8)
        t = label(tr("shell.home.drop_title"), "title")
        t.setAlignment(Qt.AlignmentFlag.AlignCenter)
        s = label(tr("shell.home.drop_sub"), "muted")
        s.setAlignment(Qt.AlignmentFlag.AlignCenter)
        v.addWidget(t)
        v.addWidget(s)
        on_theme_change(self, self.update)

    def set_hot(self, on: bool) -> None:
        self._hot = on
        self.update()

    def enterEvent(self, ev) -> None:  # noqa: N802
        self._hover = True
        self.update()

    def leaveEvent(self, ev) -> None:  # noqa: N802
        self._hover = False
        self.update()

    def mouseReleaseEvent(self, ev) -> None:  # noqa: N802
        if ev.button() == Qt.MouseButton.LeftButton and self.rect().contains(ev.position().toPoint()):
            self.clicked.emit()

    def paintEvent(self, _ev) -> None:  # noqa: N802
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        r = QRectF(self.rect()).adjusted(1, 1, -1, -1)
        path = QPainterPath()
        path.addRoundedRect(r, 16, 16)
        if self._hot:
            p.fillPath(path, T.qcolor("accent_soft"))
        elif self._hover:
            p.fillPath(path, T.qcolor("surface_raised"))
            p.fillPath(path, T.qcolor("accent", alpha=0.03))
        else:
            p.fillPath(path, T.qcolor("surface_raised"))
        pen = QPen(T.qcolor("accent" if (self._hot or self._hover) else "border_strong"), 1.5)
        pen.setDashPattern([5, 4])
        p.setPen(pen)
        p.drawPath(path)
        p.end()


class ActionCard(QFrame):
    clicked = Signal()

    def __init__(self, icon_name: str, title: str, text: str, parent=None) -> None:
        super().__init__(parent)
        self.setObjectName("ActionCard")
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setAttribute(Qt.WidgetAttribute.WA_Hover, True)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.setMinimumHeight(132)
        v = QVBoxLayout(self)
        v.setContentsMargins(18, 18, 18, 16)
        v.setSpacing(4)
        well = QFrame()
        well.setObjectName("IconWell")
        well.setFixedSize(38, 38)
        wl = QVBoxLayout(well)
        wl.setContentsMargins(0, 0, 0, 0)
        wl.addWidget(ThemedIconLabel(icon_name, 20, "accent_text"), 0, Qt.AlignmentFlag.AlignCenter)
        v.addWidget(well)
        v.addSpacing(10)
        v.addWidget(label(title, "heading"))
        d = label(text, "caption", wrap=True)
        v.addWidget(d)
        v.addStretch(1)

    def mouseReleaseEvent(self, ev) -> None:  # noqa: N802
        if ev.button() == Qt.MouseButton.LeftButton and self.rect().contains(ev.position().toPoint()):
            self.clicked.emit()


class RecentRow(QFrame):
    clicked = Signal(str)

    def __init__(self, path: str, parent=None) -> None:
        super().__init__(parent)
        self.path = path
        self.setObjectName("RecentRow")
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setAttribute(Qt.WidgetAttribute.WA_Hover, True)
        self.setToolTip(path)
        h = QHBoxLayout(self)
        h.setContentsMargins(10, 8, 12, 8)
        h.setSpacing(12)
        h.addWidget(ThemedIconLabel("file-pdf", 20, "danger"))
        name = QLabel(os.path.basename(path))
        name.setProperty("role", "heading")
        folder = label(_short_folder(os.path.dirname(path)), "caption")
        folder.setMinimumWidth(40)
        folder.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        h.addWidget(name)
        h.addWidget(folder, 1)
        try:
            when = _relative_time(os.path.getmtime(path))
        except OSError:
            when = ""
        h.addWidget(label(when, "caption"))

    def mouseReleaseEvent(self, ev) -> None:  # noqa: N802
        if ev.button() == Qt.MouseButton.LeftButton:
            self.clicked.emit(self.path)


def _short_folder(folder: str) -> str:
    home = os.path.expanduser("~")
    if folder.startswith(home):
        folder = "~" + folder[len(home):]
    return folder


def _relative_time(ts: float) -> str:
    d = max(0, time.time() - ts)
    if d < 60:
        return tr("shell.time.now")
    if d < 3600:
        return tr("shell.time.minutes", n=int(d // 60))
    if d < 86400:
        return tr("shell.time.hours", n=int(d // 3600))
    if d < 2 * 86400:
        return tr("shell.time.yesterday")
    if d < 7 * 86400:
        return tr("shell.time.days", n=int(d // 86400))
    return time.strftime("%d.%m.%Y", time.localtime(ts))


class HomeView(QWidget):
    actionRequested = Signal(str)
    openRequested = Signal(str)
    clearRecentRequested = Signal()

    CARDS = (
        ("file.open", "folder-open", "shell.home.card.open", "shell.home.card.open_text"),
        ("tools.merge", "stack", "shell.home.card.merge", "shell.home.card.merge_text"),
        ("tools.images_to_pdf", "image", "shell.home.card.images", "shell.home.card.images_text"),
        ("file.new", "file-plus", "shell.home.card.new", "shell.home.card.new_text"),
    )

    def __init__(self, recent_provider, parent=None) -> None:
        super().__init__(parent)
        self.setObjectName("HomeView")
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self._recent_provider = recent_provider
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        outer.addWidget(scroll)
        page = QWidget()
        page.setObjectName("HomeView")
        page.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        scroll.setWidget(page)
        row = QHBoxLayout(page)
        row.setContentsMargins(32, 0, 32, 0)
        col_w = QWidget()
        col_w.setMaximumWidth(920)
        row.addStretch(1)
        row.addWidget(col_w, 100)
        row.addStretch(1)
        col = QVBoxLayout(col_w)
        col.setContentsMargins(0, 56, 0, 40)
        col.setSpacing(0)

        hero = QHBoxLayout()
        hero.setSpacing(16)
        hero.addWidget(BrandMark(48), 0, Qt.AlignmentFlag.AlignTop)
        hv = QVBoxLayout()
        hv.setSpacing(4)
        hv.addWidget(label(tr("shell.home.welcome"), "display"))
        hv.addWidget(label(tr("shell.home.tagline"), "muted"))
        hero.addLayout(hv, 1)
        col.addLayout(hero)
        col.addSpacing(32)

        self.drop = DropZone()
        self.drop.clicked.connect(lambda: self.actionRequested.emit("file.open"))
        col.addWidget(self.drop)
        col.addSpacing(16)

        grid = QGridLayout()
        grid.setSpacing(12)
        self.cards: dict[str, ActionCard] = {}
        for i, (aid, ic, t, d) in enumerate(self.CARDS):
            card = ActionCard(ic, tr(t), tr(d))
            card.clicked.connect(lambda aid=aid: self.actionRequested.emit(aid))
            card.setAccessibleName(aid)
            grid.addWidget(card, 0, i)
            self.cards[aid] = card
        col.addLayout(grid)
        col.addSpacing(36)

        rh = QHBoxLayout()
        rh.addWidget(label(tr("shell.home.recent"), "title"))
        rh.addStretch(1)
        self.clear_btn = QPushButton(tr("shell.home.recent_clear"))
        self.clear_btn.setProperty("variant", "ghost")
        self.clear_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.clear_btn.clicked.connect(self.clearRecentRequested)
        rh.addWidget(self.clear_btn)
        col.addLayout(rh)
        col.addSpacing(8)
        self.recent_box = QFrame()
        self.recent_box.setProperty("card", True)
        self.recent_lay = QVBoxLayout(self.recent_box)
        self.recent_lay.setContentsMargins(6, 6, 6, 6)
        self.recent_lay.setSpacing(0)
        col.addWidget(self.recent_box)
        col.addSpacing(28)

        foot = QHBoxLayout()
        foot.setSpacing(8)
        foot.addStretch(1)
        foot.addWidget(ThemedIconLabel("shield-check", 16, "success"))
        foot.addWidget(label(tr("shell.home.privacy"), "caption"))
        foot.addStretch(1)
        col.addLayout(foot)
        col.addStretch(1)
        self.refresh_recent()

    def refresh_recent(self) -> None:
        while self.recent_lay.count():
            it = self.recent_lay.takeAt(0)
            if it.widget():
                it.widget().deleteLater()
        files = [f for f in self._recent_provider() if os.path.isfile(f)][:8]
        self.clear_btn.setVisible(bool(files))
        if not files:
            empty = QWidget()
            ev = QHBoxLayout(empty)
            ev.setContentsMargins(14, 16, 14, 16)
            ev.setSpacing(10)
            ev.addWidget(ThemedIconLabel("clock-counter-clockwise", 18, "text_subtle"))
            ev.addWidget(label(tr("shell.home.recent_empty"), "muted"), 1)
            self.recent_lay.addWidget(empty)
            return
        for path in files:
            r = RecentRow(path)
            r.clicked.connect(self.openRequested)
            self.recent_lay.addWidget(r)

    def recent_count(self) -> int:
        return sum(1 for i in range(self.recent_lay.count())
                   if isinstance(self.recent_lay.itemAt(i).widget(), RecentRow))
