""""Markér kommentar" – select, move, restyle and delete annotations.

Click an annotation to select it (accent frame), drag to move it, Delete /
Backspace removes it, double-click a note to edit its text. The inspector
shows the selected annotation's colour/opacity/width and applies changes
directly (one undo step each).
"""

from __future__ import annotations

from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import QColor, QKeyEvent, QPen
from PySide6.QtWidgets import QFrame, QGraphicsRectItem, QVBoxLayout, QWidget

from ....core import annotations as core_annots
from ....i18n import tr
from ...theme import tokens as T
from ...widgets.util import label
from ._annot_common import AnnotStyle, AnnotStylePanel, ask_note
from .base import Tool, ToolMouseEvent, ToolRegistry

FRAME_PAD = 3.0
WIDTH_KINDS = ("ink", "line", "arrow", "rect", "ellipse")
FILL_KINDS = ("rect", "ellipse")


@ToolRegistry.register
class AnnotSelectTool(Tool):
    id = "annotate.select"
    label = tr("annot.select")
    hint = tr("annot.select.hint")
    icon = "cursor"
    shortcut = "V"
    mode = "annotate"
    order = 0
    cursor = Qt.CursorShape.ArrowCursor

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.selected: core_annots.AnnotInfo | None = None
        self._frame: QGraphicsRectItem | None = None
        self._press: QPointF | None = None
        self._moved = False

    # -- selection ----------------------------------------------------------
    def select(self, info: core_annots.AnnotInfo | None, *, emit: bool = True) -> None:
        self._clear_frame()
        self.selected = info
        if info is not None:
            r = info.rect
            self._frame = QGraphicsRectItem(QRectF(r.x0 - FRAME_PAD, r.y0 - FRAME_PAD,
                                                   r.width + 2 * FRAME_PAD, r.height + 2 * FRAME_PAD))
            pen = QPen(T.qcolor("accent"), 1.5)
            pen.setCosmetic(True)
            self._frame.setPen(pen)
            fill = QColor(T.qcolor("accent"))
            fill.setAlpha(18)
            self._frame.setBrush(fill)
            self._frame.setZValue(45)
            self.ctx.view.add_overlay(info.page, self._frame)
        if emit:  # rebuilds the inspector – not wanted while restyling
            self.changed.emit()

    def _clear_frame(self) -> None:
        if self._frame is not None and self.ctx is not None:
            self.ctx.view.remove_overlay(self._frame)
        self._frame = None

    def _reselect(self) -> None:
        """Refresh the selection after an edit (xref is stable)."""
        if self.selected is None:
            return
        page, xref = self.selected.page, self.selected.xref
        info = next((a for a in core_annots.list_annotations(self.ctx.pdf.doc, page) if a.xref == xref), None)
        self.select(info, emit=info is None)

    def deactivate(self) -> None:
        self._clear_frame()
        self.selected = None
        super().deactivate()

    def document_changed(self, event) -> None:
        if self.selected is None or self.ctx is None:
            return
        try:
            self._reselect()
        except Exception:
            self.select(None)

    # -- mouse ----------------------------------------------------------------
    def mouse_move(self, ev: ToolMouseEvent) -> None:
        if self._press is not None and self.selected is not None and self._frame is not None:
            d = ev.pos_on(self.selected.page) - self._press
            if d.manhattanLength() >= 2 or self._moved:
                self._moved = True
                self._frame.setPos(d)
            return
        hit = ev.page >= 0 and core_annots.annotation_at(self.ctx.pdf.doc, ev.page, (ev.pos.x(), ev.pos.y()))
        self.ctx.view.set_tool_cursor(Qt.CursorShape.SizeAllCursor if hit else Qt.CursorShape.ArrowCursor)

    def mouse_press(self, ev: ToolMouseEvent) -> None:
        if ev.button != Qt.MouseButton.LeftButton:
            return
        info = core_annots.annotation_at(self.ctx.pdf.doc, ev.page, (ev.pos.x(), ev.pos.y())) if ev.page >= 0 else None
        if info is None or self.selected is None or info.xref != self.selected.xref or info.page != self.selected.page:
            self.select(info)
        self._press = QPointF(ev.pos) if info is not None else None
        self._moved = False

    def mouse_release(self, ev: ToolMouseEvent) -> None:
        press, moved = self._press, self._moved
        self._press, self._moved = None, False
        if press is None or not moved or self.selected is None:
            return
        d = ev.pos_on(self.selected.page) - press
        r = self.selected.rect
        new = (r.x0 + d.x(), r.y0 + d.y(), r.x1 + d.x(), r.y1 + d.y())
        if self._frame is not None:
            self._frame.setPos(0, 0)
        core_annots.update_annotation(self.ctx.pdf, self.selected.page, self.selected.xref, rect=new)

    def mouse_double_click(self, ev: ToolMouseEvent) -> None:
        if self.selected is None or self.selected.kind != "note":
            return
        text = ask_note(self.ctx.view.window(), self.selected.contents, editing=True)
        if text is not None and text != self.selected.contents:
            core_annots.update_annotation(self.ctx.pdf, self.selected.page, self.selected.xref, contents=text)

    def key_press(self, ev: QKeyEvent) -> bool:
        if self.selected is None:
            return False
        if ev.key() in (Qt.Key.Key_Delete, Qt.Key.Key_Backspace):
            self.delete_selected()
            return True
        if ev.key() == Qt.Key.Key_Escape:
            self.select(None)
            return True
        return False

    def delete_selected(self) -> None:
        if self.selected is None:
            return
        page, xref = self.selected.page, self.selected.xref
        self.select(None)
        core_annots.delete_annotation(self.ctx.pdf, page, xref)

    # -- inspector --------------------------------------------------------------
    def properties_widget(self) -> QWidget:
        a = self.selected
        if a is None:
            w = QWidget()
            v = QVBoxLayout(w)
            v.setContentsMargins(16, 4, 16, 16)
            card = QFrame()
            card.setObjectName("Card")
            cl = QVBoxLayout(card)
            cl.setContentsMargins(14, 12, 14, 12)
            cl.addWidget(label(tr("annot.select.none"), "heading"))
            cl.addWidget(label(tr("annot.select.none.hint"), "muted", wrap=True))
            v.addWidget(card)
            v.addStretch(1)
            return w
        style = AnnotStyle(tuple(a.color or (0, 0, 0)), float(a.opacity or 1.0), float(a.width or 1.0),
                           tuple(a.fill) if a.fill else None)
        panel = AnnotStylePanel(tr(f"annot.kind.{a.kind}"), tr("annot.select.selected.hint"), style,
                                width=a.kind in WIDTH_KINDS, fill=a.kind in FILL_KINDS)
        panel.styleChanged.connect(self._apply_style)
        return panel

    def _apply_style(self, style: AnnotStyle) -> None:
        a = self.selected
        if a is None:
            return
        props = {"color": style.color, "opacity": style.opacity}
        if a.kind in WIDTH_KINDS:
            props["width"] = style.width
        if a.kind in FILL_KINDS:
            props["fill"] = style.fill
        core_annots.update_annotation(self.ctx.pdf, a.page, a.xref, **props)
