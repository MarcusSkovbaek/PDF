"""Generates assets/folio.png and assets/folio.ico (run once; output committed)."""
import os
import sys

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import QColor, QGuiApplication, QImage, QLinearGradient, QPainter, QPainterPath, QPen

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))


def draw(size: int) -> QImage:
    img = QImage(size, size, QImage.Format_ARGB32_Premultiplied)
    img.fill(Qt.transparent)
    p = QPainter(img)
    p.setRenderHint(QPainter.Antialiasing)
    s = size / 256.0
    bg = QPainterPath()
    bg.addRoundedRect(QRectF(8 * s, 8 * s, 240 * s, 240 * s), 56 * s, 56 * s)
    g = QLinearGradient(QPointF(0, 0), QPointF(size, size))
    g.setColorAt(0, QColor("#7B7BF0"))
    g.setColorAt(1, QColor("#4343B8"))
    p.fillPath(bg, g)
    # page with folded corner
    page = QPainterPath()
    l, t, r, b, f = 72 * s, 52 * s, 184 * s, 204 * s, 36 * s
    page.moveTo(l + 12 * s, t)
    page.lineTo(r - f, t)
    page.lineTo(r, t + f)
    page.lineTo(r, b - 12 * s)
    page.quadTo(r, b, r - 12 * s, b)
    page.lineTo(l + 12 * s, b)
    page.quadTo(l, b, l, b - 12 * s)
    page.lineTo(l, t + 12 * s)
    page.quadTo(l, t, l + 12 * s, t)
    p.fillPath(page, QColor(255, 255, 255, 245))
    fold = QPainterPath()
    fold.moveTo(r - f, t)
    fold.lineTo(r - f, t + f - 8 * s)
    fold.quadTo(r - f, t + f, r - f + 8 * s, t + f)
    fold.lineTo(r, t + f)
    fold.closeSubpath()
    p.fillPath(fold, QColor("#C9C9F7"))
    pen = QPen(QColor("#5B5BD6"), 12 * s, Qt.SolidLine, Qt.RoundCap)
    p.setPen(pen)
    for i, w in enumerate((76, 60, 76, 44)):
        y = (112 + i * 24) * s
        p.drawLine(QPointF(l + 22 * s, y), QPointF(l + (22 + w) * s, y))
    p.end()
    return img


if __name__ == "__main__":
    app = QGuiApplication(sys.argv)
    os.makedirs(os.path.join(ROOT, "assets"), exist_ok=True)
    draw(512).save(os.path.join(ROOT, "assets", "folio.png"))
    draw(256).save(os.path.join(ROOT, "assets", "folio.ico"))
    print("ok")
