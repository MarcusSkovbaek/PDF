"""Hand tool: drag to pan. (Holding Space pans with any tool.)"""

from __future__ import annotations

from PySide6.QtCore import QPointF, Qt

from ....i18n import tr
from .base import Tool, ToolMouseEvent, ToolRegistry


@ToolRegistry.register
class HandTool(Tool):
    id = "view.hand"
    label = tr("shell.tool.hand")
    icon = "hand"
    shortcut = "H"
    mode = ("view",)
    order = 20
    cursor = Qt.CursorShape.OpenHandCursor
    hint = tr("shell.tool.hand.hint")

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._last: QPointF | None = None

    def mouse_press(self, ev: ToolMouseEvent) -> None:
        if ev.button == Qt.MouseButton.LeftButton:
            self._last = ev.view_pos
            ev.view.set_tool_cursor(Qt.CursorShape.ClosedHandCursor)

    def mouse_move(self, ev: ToolMouseEvent) -> None:
        if self._last is None:
            return
        d = ev.view_pos - self._last
        self._last = ev.view_pos
        ev.view.pan_by(d.x(), d.y())

    def mouse_release(self, ev: ToolMouseEvent) -> None:
        self._last = None
        ev.view.set_tool_cursor(self.cursor)
