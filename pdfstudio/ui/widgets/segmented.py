"""Segmented control with an animated sliding selection pill."""

from __future__ import annotations

from dataclasses import dataclass

from PySide6.QtCore import QEasingCurve, QRectF, QSize, Qt, QVariantAnimation, Signal
from PySide6.QtGui import QFontMetrics, QPainter, QPainterPath, QPen
from PySide6.QtWidgets import QSizePolicy, QWidget

from ..theme import tokens as T
from ..theme.icons import icon


@dataclass
class Segment:
    key: str
    text: str
    icon: str | None = None
    tooltip: str = ""


class SegmentedControl(QWidget):
    currentChanged = Signal(str)

    PAD = 3
    ICON = 16

    def __init__(self, segments: list[Segment], parent: QWidget | None = None, *, height: int = 34,
                 expand: bool = False, min_seg: int = 64) -> None:
        super().__init__(parent)
        self._expand = expand
        self._min_seg = min_seg
        self._segments = list(segments)
        self._index = 0
        self._hover = -1
        self._pos = 0.0  # animated index position
        self._height = height
        self.setMouseTracking(True)
        self.setFocusPolicy(Qt.FocusPolicy.TabFocus)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setSizePolicy(QSizePolicy.Policy.Expanding if expand else QSizePolicy.Policy.Fixed,
                           QSizePolicy.Policy.Fixed)
        self._anim = QVariantAnimation(self)
        self._anim.setDuration(200)
        self._anim.setEasingCurve(QEasingCurve.Type.OutCubic)
        self._anim.valueChanged.connect(self._on_anim)
        self.setAccessibleName("segmented")

    # -- API -------------------------------------------------------------
    def keys(self) -> list[str]:
        return [s.key for s in self._segments]

    def current_key(self) -> str:
        return self._segments[self._index].key if self._segments else ""

    def set_current_key(self, key: str, animate: bool = True, emit: bool = False) -> None:
        for i, s in enumerate(self._segments):
            if s.key == key:
                self.set_current_index(i, animate=animate, emit=emit)
                return

    def set_current_index(self, i: int, *, animate: bool = True, emit: bool = True) -> None:
        if not (0 <= i < len(self._segments)):
            return
        changed = i != self._index
        self._index = i
        if animate and self.isVisible():
            self._anim.stop()
            self._anim.setStartValue(float(self._pos))
            self._anim.setEndValue(float(i))
            self._anim.start()
        else:
            self._pos = float(i)
            self.update()
        if changed and emit:
            self.currentChanged.emit(self._segments[i].key)

    # -- geometry --------------------------------------------------------
    def _seg_widths(self) -> list[int]:
        fm = QFontMetrics(self._bold_font())
        out = []
        for s in self._segments:
            w = fm.horizontalAdvance(s.text) + (20 if self._expand else 28)
            if s.icon:
                w += self.ICON + 7
            out.append(max(w, self._min_seg))
        if self._expand and out:
            avail = self.width() - 2 * self.PAD
            total = sum(out)
            if avail > total:
                extra = (avail - total) / len(out)
                out = [w + extra for w in out]
        return out

    def _bold_font(self):
        f = self.font()
        f.setWeight(f.Weight.DemiBold)
        return f

    def sizeHint(self) -> QSize:  # noqa: N802
        exp, self._expand = self._expand, False
        try:
            return QSize(int(sum(self._seg_widths())) + 2 * self.PAD, self._height)
        finally:
            self._expand = exp

    def minimumSizeHint(self) -> QSize:  # noqa: N802
        if self._expand:
            exp, self._expand = self._expand, False
            try:
                return QSize(sum(self._seg_widths()) + 2 * self.PAD, self._height)
            finally:
                self._expand = exp
        return self.sizeHint()

    def _rects(self) -> list[QRectF]:
        x = float(self.PAD)
        out = []
        for w in self._seg_widths():
            out.append(QRectF(x, self.PAD, w, self.height() - 2 * self.PAD))
            x += w
        return out

    def _on_anim(self, v) -> None:
        self._pos = float(v)
        self.update()

    # -- painting --------------------------------------------------------
    def paintEvent(self, _ev) -> None:  # noqa: N802
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        t = T.current()
        r = QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5)
        track = QPainterPath()
        track.addRoundedRect(r, 9, 9)
        p.fillPath(track, T.qcolor("pressed" if t.is_dark else "hover"))
        rects = self._rects()
        if rects:
            lo = int(self._pos)
            hi = min(lo + 1, len(rects) - 1)
            f = self._pos - lo
            a, b = rects[lo], rects[hi]
            pill = QRectF(a.x() + (b.x() - a.x()) * f, a.y(), a.width() + (b.width() - a.width()) * f, a.height())
            # hover background
            if 0 <= self._hover < len(rects) and self._hover != self._index:
                hp = QPainterPath()
                hp.addRoundedRect(rects[self._hover], 7, 7)
                p.fillPath(hp, T.qcolor("hover"))
            # soft shadow + pill
            sh = QPainterPath()
            sh.addRoundedRect(pill.translated(0, 1), 7, 7)
            p.fillPath(sh, T.qcolor("shadow"))
            pp = QPainterPath()
            pp.addRoundedRect(pill, 7, 7)
            p.fillPath(pp, T.qcolor("surface_raised") if not t.is_dark else T.qcolor("border_strong"))
            if not t.is_dark:
                p.setPen(QPen(T.qcolor("border"), 1))
                p.drawPath(pp)
        bold = self._bold_font()
        for i, (s, rr) in enumerate(zip(self._segments, rects)):
            active = i == self._index
            p.setFont(bold)
            color_role = "text" if active else ("text" if i == self._hover else "text_muted")
            fm = QFontMetrics(bold)
            tw = fm.horizontalAdvance(s.text)
            total = tw + ((self.ICON + 7) if s.icon else 0)
            x = rr.x() + (rr.width() - total) / 2
            if s.icon:
                ic = icon(s.icon, "accent_text" if active else color_role)
                ic.paint(p, int(x), int(rr.center().y() - self.ICON / 2), self.ICON, self.ICON)
                x += self.ICON + 7
            p.setPen(T.qcolor(color_role))
            p.drawText(QRectF(x, rr.y(), tw + 2, rr.height()), Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft, s.text)
        if self.hasFocus():
            p.setPen(QPen(T.qcolor("focus_ring"), 2))
            p.setBrush(Qt.BrushStyle.NoBrush)
            p.drawRoundedRect(r.adjusted(1, 1, -1, -1), 9, 9)
        p.end()

    # -- interaction -----------------------------------------------------
    def _index_at(self, pos) -> int:
        for i, r in enumerate(self._rects()):
            if r.contains(pos):
                return i
        return -1

    def mouseMoveEvent(self, ev) -> None:  # noqa: N802
        h = self._index_at(ev.position())
        if h != self._hover:
            self._hover = h
            seg = self._segments[h] if h >= 0 else None
            self.setToolTip(seg.tooltip if seg else "")
            self.update()

    def leaveEvent(self, _ev) -> None:  # noqa: N802
        self._hover = -1
        self.update()

    def mousePressEvent(self, ev) -> None:  # noqa: N802
        if ev.button() == Qt.MouseButton.LeftButton:
            i = self._index_at(ev.position())
            if i >= 0:
                self.set_current_index(i)

    def keyPressEvent(self, ev) -> None:  # noqa: N802
        if ev.key() == Qt.Key.Key_Left:
            self.set_current_index(max(0, self._index - 1))
        elif ev.key() == Qt.Key.Key_Right:
            self.set_current_index(min(len(self._segments) - 1, self._index + 1))
        else:
            super().keyPressEvent(ev)
