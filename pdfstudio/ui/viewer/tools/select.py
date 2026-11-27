"""Text selection tool (*Markér*): character-precise selection across lines,
columns and pages (see :class:`~pdfstudio.ui.viewer.text_selection.TextSelection`),
a floating mini toolbar with Kommentér / Fremhæv / … under the selection,
a right-click menu, Ctrl+A selects everything and Ctrl+C copies.

Subclasses (the markup tools) set ``shows_bar = False`` and act in
:meth:`SelectTool.selection_finished` instead.
"""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor

from ....i18n import tr
from ...theme import tokens as T
from ..text_selection import TextSelection
from ._text_actions import SelectionUI
from .base import Tool, ToolMouseEvent, ToolRegistry


@ToolRegistry.register
class SelectTool(Tool):
    id = "view.select"
    label = tr("shell.tool.select")
    icon = "cursor"
    shortcut = "V"
    mode = ("view",)
    order = 10
    cursor = Qt.CursorShape.ArrowCursor
    hint = tr("shell.tool.select.hint")
    shows_bar = True

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.sel = TextSelection(self, self.selection_brush)
        self.sel_ui = SelectionUI(self, self.sel)

    # -- lifecycle -------------------------------------------------------------
    def activate(self, ctx) -> None:
        super().activate(ctx)
        if self.shows_bar:
            self.sel_ui.attach()

    def deactivate(self) -> None:
        self.clear_selection()
        self.sel_ui.detach()
        super().deactivate()

    def document_changed(self, _event) -> None:
        self.sel.invalidate()
        self.clear_selection()

    # -- events ------------------------------------------------------------------
    def mouse_press(self, ev: ToolMouseEvent) -> None:
        if ev.button != Qt.MouseButton.LeftButton:
            return
        self.sel_ui.hide_bar()
        if not self.sel.press(ev):
            self.clear_selection()

    def mouse_move(self, ev: ToolMouseEvent) -> None:
        if self.sel.dragging:
            self.sel.drag(ev)
            return
        over = self.sel.over_text(ev.page, ev.pos)
        ev.view.set_tool_cursor(Qt.CursorShape.IBeamCursor if over else self.cursor)

    def mouse_release(self, ev: ToolMouseEvent) -> None:
        if not self.sel.dragging:
            return
        self.sel.release(ev)
        if self.sel.has_selection():
            self.selection_finished()

    def mouse_double_click(self, ev: ToolMouseEvent) -> None:
        if ev.button == Qt.MouseButton.LeftButton or ev.left:
            self.sel_ui.hide_bar()
            self.sel.press(ev)  # click_count 2: the word

    def selection_finished(self) -> None:
        """A selection was made with the mouse."""
        if self.shows_bar:
            self.sel_ui.show_bar()
        self.changed.emit()

    def key_press(self, ev) -> bool:
        if ev.key() == Qt.Key.Key_Escape and self.has_selection():
            self.clear_selection()
            return True
        return False

    def context_menu(self, ev: ToolMouseEvent, global_pos) -> bool:
        return self.shows_bar and self.sel_ui.context_menu(ev, global_pos)

    # -- selection ------------------------------------------------------------------
    def has_selection(self) -> bool:
        return self.sel.has_selection()

    def selected_text(self) -> str:
        return self.sel.text()

    def select_all(self) -> bool:
        ok = self.sel.select_all()
        if ok and self.shows_bar:
            self.sel_ui.show_bar()
        return ok

    def copy(self) -> bool:
        return self.sel_ui.copy()

    def run_selection_action(self, key: str) -> bool:
        """Kommentér / Fremhæv / … on the current selection (Ctrl+Alt+M)."""
        return self.sel_ui.run(key)

    def selection_brush(self) -> QColor:
        """Fill of the live selection (markup tools preview their colour)."""
        return T.qcolor("text_selection")

    def line_rects(self):
        """``{page: [display rect per line]}`` of the selection."""
        return self.sel.rects()

    def clear_selection(self) -> None:
        had = self.sel.has_selection()
        self.sel.clear()
        self.sel_ui.hide_bar()
        if had:
            self.changed.emit()  # e.g. "Kopiér" is no longer available
