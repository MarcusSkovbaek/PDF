"""Colour swatch button: shows the current colour, opens a local QColorDialog."""

from __future__ import annotations

from PySide6.QtCore import QRectF, QSize, Qt, Signal
from PySide6.QtGui import QColor, QPainter, QPainterPath, QPen
from PySide6.QtWidgets import QColorDialog, QToolButton, QWidget

from ..theme import tokens as T


def to_qcolor(rgb: tuple) -> QColor:
    """Core colour (floats 0..1) -> QColor."""
    r, g, b = (max(0.0, min(1.0, float(c))) for c in tuple(rgb)[:3])
    return QColor.fromRgbF(r, g, b)


def from_qcolor(color: QColor) -> tuple[float, float, float]:
    return (round(color.redF(), 4), round(color.greenF(), 4), round(color.blueF(), 4))


class ColorButton(QToolButton):
    colorChanged = Signal(tuple)  # (r, g, b) floats

    def __init__(self, color: tuple = (0.0, 0.0, 0.0), parent: QWidget | None = None, *, title: str = "") -> None:
        super().__init__(parent)
        self._color = tuple(color)
        self._title = title
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFixedSize(QSize(34, 30))
        self.setAutoRaise(True)
        self.clicked.connect(self._pick)

    def color(self) -> tuple:
        return self._color

    def set_color(self, color: tuple, *, emit: bool = False) -> None:
        color = tuple(color)
        if color == self._color:
            return
        self._color = color
        self.update()
        if emit:
            self.colorChanged.emit(color)

    def _pick(self) -> None:
        c = QColorDialog.getColor(to_qcolor(self._color), self.window(), self._title)
        if c.isValid():
            self.set_color(from_qcolor(c), emit=True)

    def paintEvent(self, _ev) -> None:  # noqa: N802
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        r = QRectF(self.rect()).adjusted(1.5, 1.5, -1.5, -1.5)
        frame = QPainterPath()
        frame.addRoundedRect(r, 7, 7)
        p.fillPath(frame, T.qcolor("hover" if self.underMouse() else "surface_raised"))
        p.setPen(QPen(T.qcolor("border_strong"), 1))
        p.drawPath(frame)
        sw = QPainterPath()
        sw.addRoundedRect(r.adjusted(5, 5, -5, -5), 4, 4)
        fill = to_qcolor(self._color)
        p.fillPath(sw, fill)
        p.setPen(QPen(T.swatch_outline(fill), 1))
        p.drawPath(sw)
        p.end()
