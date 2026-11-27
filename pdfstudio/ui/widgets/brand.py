"""The Folio app mark (painted, so it is crisp at any size and needs no files)."""

from __future__ import annotations

from PySide6.QtCore import QPointF, QRectF, QSize, Qt
from PySide6.QtGui import QColor, QIcon, QLinearGradient, QPainter, QPainterPath, QPixmap
from PySide6.QtWidgets import QWidget

from ..theme import tokens as T


def paint_mark(p: QPainter, r: QRectF) -> None:
    p.save()
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    s = r.width()
    bg = QPainterPath()
    bg.addRoundedRect(r, s * 0.28, s * 0.28)
    g = QLinearGradient(r.topLeft(), r.bottomRight())
    a = T.qcolor("accent")
    g.setColorAt(0.0, a.lighter(118))
    g.setColorAt(1.0, a.darker(118))
    p.fillPath(bg, g)
    # folded page glyph
    pw, ph = s * 0.42, s * 0.54
    x0 = r.center().x() - pw / 2
    y0 = r.center().y() - ph / 2
    fold = pw * 0.36
    page = QPainterPath()
    page.moveTo(x0, y0)
    page.lineTo(x0 + pw - fold, y0)
    page.lineTo(x0 + pw, y0 + fold)
    page.lineTo(x0 + pw, y0 + ph)
    page.lineTo(x0, y0 + ph)
    page.closeSubpath()
    p.fillPath(page, QColor(255, 255, 255, 245))
    corner = QPainterPath()
    corner.moveTo(x0 + pw - fold, y0)
    corner.lineTo(x0 + pw - fold, y0 + fold)
    corner.lineTo(x0 + pw, y0 + fold)
    corner.closeSubpath()
    p.fillPath(corner, QColor(255, 255, 255, 140))
    # two text lines
    p.setPen(Qt.PenStyle.NoPen)
    p.setBrush(a)
    lh = max(1.0, s * 0.05)
    for k, frac in enumerate((0.62, 0.42)):
        y = y0 + ph * (0.52 + k * 0.2)
        p.drawRoundedRect(QRectF(x0 + pw * 0.18, y, pw * frac, lh), lh / 2, lh / 2)
    p.restore()


def app_icon() -> QIcon:
    ic = QIcon()
    for size in (16, 24, 32, 48, 64, 128, 256):
        pm = QPixmap(size, size)
        pm.fill(Qt.GlobalColor.transparent)
        p = QPainter(pm)
        paint_mark(p, QRectF(0, 0, size, size))
        p.end()
        ic.addPixmap(pm)
    return ic


class BrandMark(QWidget):
    def __init__(self, size: int = 24, parent=None) -> None:
        super().__init__(parent)
        self._size = size
        self.setFixedSize(QSize(size, size))

    def paintEvent(self, _ev) -> None:  # noqa: N802
        p = QPainter(self)
        paint_mark(p, QRectF(0, 0, self._size, self._size))
        p.end()
