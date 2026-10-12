"""Drawing tools of "Kommentér": Note, Tegn (freehand ink) and Figur
(rectangle / ellipse / line / arrow). Live previews are scene overlays in
display coordinates; the annotation is created on release (one undo step)."""

from __future__ import annotations

import math

from PySide6.QtCore import QLineF, QPointF, QRectF, Qt
from PySide6.QtGui import QBrush, QPainterPath, QPen
from PySide6.QtWidgets import QGraphicsEllipseItem, QGraphicsLineItem, QGraphicsPathItem, QGraphicsRectItem, QWidget

from ....core import annotations as core_annots
from ....i18n import tr
from ...widgets.color_button import to_qcolor
from ...widgets.segmented import Segment, SegmentedControl
from ._annot_common import PRESETS, AnnotStyle, AnnotStylePanel, ask_note
from .base import Tool, ToolMouseEvent, ToolRegistry

MIN_DRAG = 3.0  # pt; smaller drags count as clicks


def _pen(style: AnnotStyle) -> QPen:
    c = to_qcolor(style.color)
    c.setAlphaF(style.opacity)
    pen = QPen(c, style.width)
    pen.setCapStyle(Qt.PenCapStyle.RoundCap)
    pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
    return pen


class _StyledTool(Tool):
    mode = "annotate"
    default_style = AnnotStyle(PRESETS[5], 1.0, 2.0)
    panel_width = True
    panel_fill = False

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.style = AnnotStyle.load(self.id, self.default_style)
        self._panel: AnnotStylePanel | None = None

    def extra_widget(self) -> QWidget | None:
        return None

    def properties_widget(self):
        if self._panel is None:
            self._panel = AnnotStylePanel(self.label, self.hint, self.style, width=self.panel_width,
                                          fill=self.panel_fill, extra=self.extra_widget())
            self._panel.styleChanged.connect(self._style_changed)
        return self._panel

    def _style_changed(self, style: AnnotStyle) -> None:
        self.style = style
        style.save(self.id)


# ---------------------------------------------------------------------------
@ToolRegistry.register
class NoteTool(_StyledTool):
    id = "annotate.note"
    label = tr("annot.note")
    hint = tr("annot.note.hint")
    icon = "note"
    shortcut = "N"
    order = 40
    cursor = Qt.CursorShape.CrossCursor
    default_style = AnnotStyle(PRESETS[0], 1.0)
    panel_width = False

    def mouse_release(self, ev: ToolMouseEvent) -> None:
        if ev.page < 0 or ev.button != Qt.MouseButton.LeftButton:
            return
        text = ask_note(self.ctx.view.window())
        if text is None:
            return
        core_annots.add_note(self.ctx.pdf, ev.page, (ev.pos.x(), ev.pos.y()), text, self.style.color)


# ---------------------------------------------------------------------------
@ToolRegistry.register
class InkTool(_StyledTool):
    id = "annotate.ink"
    label = tr("annot.ink")
    hint = tr("annot.ink.hint")
    icon = "scribble-loop"
    shortcut = "D"
    order = 50
    cursor = Qt.CursorShape.CrossCursor
    default_style = AnnotStyle(PRESETS[4], 1.0, 2.0)

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._page = -1
        self._points: list[QPointF] = []
        self._item: QGraphicsPathItem | None = None

    def mouse_press(self, ev: ToolMouseEvent) -> None:
        if ev.page < 0 or ev.button != Qt.MouseButton.LeftButton:
            return
        self._page, self._points = ev.page, [QPointF(ev.pos)]
        self._item = QGraphicsPathItem()
        self._item.setPen(_pen(self.style))
        self._item.setZValue(40)
        self.ctx.view.add_overlay(ev.page, self._item)

    def mouse_move(self, ev: ToolMouseEvent) -> None:
        if self._item is None:
            return
        p = ev.pos_on(self._page)
        if (p - self._points[-1]).manhattanLength() < 0.6:
            return
        self._points.append(p)
        path = QPainterPath(self._points[0])
        for q in self._points[1:]:
            path.lineTo(q)
        self._item.setPath(path)

    def mouse_release(self, ev: ToolMouseEvent) -> None:
        if self._item is None:
            return
        self.ctx.view.remove_overlay(self._item)
        self._item = None
        pts = [(p.x(), p.y()) for p in self._points]
        if len(pts) == 1:
            pts.append((pts[0][0] + 0.5, pts[0][1] + 0.5))  # a dot
        core_annots.add_ink(self.ctx.pdf, self._page, [pts], self.style.color, self.style.width,
                            self.style.opacity)
        self._points = []

    def deactivate(self) -> None:
        if self._item is not None and self.ctx is not None:
            self.ctx.view.remove_overlay(self._item)
        self._item = None
        super().deactivate()


# ---------------------------------------------------------------------------
@ToolRegistry.register
class ShapeTool(_StyledTool):
    id = "annotate.shape"
    label = tr("annot.shape")
    hint = tr("annot.shape.hint")
    icon = "square"
    shortcut = "R"
    order = 60
    cursor = Qt.CursorShape.CrossCursor
    default_style = AnnotStyle(PRESETS[4], 1.0, 2.0)
    panel_fill = True

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        from PySide6.QtCore import QSettings

        self.kind = str(QSettings().value("annot/shape_kind", "rect"))
        if self.kind not in core_annots.SHAPE_KINDS:
            self.kind = "rect"
        self._page = -1
        self._start = QPointF()
        self._item = None

    def extra_widget(self) -> QWidget:
        seg = SegmentedControl([
            Segment("rect", "", "square", tr("annot.shape.rect")),
            Segment("ellipse", "", "circle", tr("annot.shape.ellipse")),
            Segment("line", "", "line-segment", tr("annot.shape.line")),
            Segment("arrow", "", "arrow-up-right", tr("annot.shape.arrow")),
        ], expand=True, height=34, min_seg=40)
        seg.set_current_key(self.kind, animate=False)
        seg.currentChanged.connect(self.set_kind)
        return seg

    def set_kind(self, kind: str) -> None:
        from PySide6.QtCore import QSettings

        self.kind = kind
        QSettings().setValue("annot/shape_kind", kind)

    def _end(self, ev: ToolMouseEvent) -> QPointF:
        p = ev.pos_on(self._page)
        if not ev.modifiers & Qt.KeyboardModifier.ShiftModifier:
            return p
        d = p - self._start
        if self.kind in ("line", "arrow"):  # snap to 45°
            ang = round(math.atan2(d.y(), d.x()) / (math.pi / 4)) * (math.pi / 4)
            ln = math.hypot(d.x(), d.y())
            return self._start + QPointF(math.cos(ang) * ln, math.sin(ang) * ln)
        s = max(abs(d.x()), abs(d.y()))  # square / circle
        return self._start + QPointF(math.copysign(s, d.x() or 1), math.copysign(s, d.y() or 1))

    def mouse_press(self, ev: ToolMouseEvent) -> None:
        if ev.page < 0 or ev.button != Qt.MouseButton.LeftButton:
            return
        self._page, self._start = ev.page, QPointF(ev.pos)
        if self.kind in ("line", "arrow"):
            self._item = QGraphicsLineItem()
        elif self.kind == "ellipse":
            self._item = QGraphicsEllipseItem()
        else:
            self._item = QGraphicsRectItem()
        self._item.setPen(_pen(self.style))
        if self.style.fill is not None and self.kind in ("rect", "ellipse"):
            c = to_qcolor(self.style.fill)
            c.setAlphaF(self.style.opacity * 0.35)
            self._item.setBrush(QBrush(c))
        self._item.setZValue(40)
        self.ctx.view.add_overlay(ev.page, self._item)

    def mouse_move(self, ev: ToolMouseEvent) -> None:
        if self._item is None:
            return
        end = self._end(ev)
        if isinstance(self._item, QGraphicsLineItem):
            self._item.setLine(QLineF(self._start, end))
        else:
            self._item.setRect(QRectF(self._start, end).normalized())

    def mouse_release(self, ev: ToolMouseEvent) -> None:
        if self._item is None:
            return
        self.ctx.view.remove_overlay(self._item)
        self._item = None
        end = self._end(ev)
        if (end - self._start).manhattanLength() < MIN_DRAG:
            return
        core_annots.add_shape(self.ctx.pdf, self._page, self.kind, (self._start.x(), self._start.y()),
                              (end.x(), end.y()), self.style.color,
                              self.style.fill if self.kind in ("rect", "ellipse") else None,
                              self.style.width, self.style.opacity)

    def deactivate(self) -> None:
        if self._item is not None and self.ctx is not None:
            self.ctx.view.remove_overlay(self._item)
        self._item = None
        super().deactivate()
