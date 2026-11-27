"""Text markup tools: Fremhæv / Understreg / Gennemstreg.

They reuse the select tool's character-precise selection (across lines
and pages) and preview it in the markup colour; releasing the mouse marks
it – one annotation per page with one quad per text line, one undo step.
A double click marks a word, a triple click a paragraph.
"""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor

from ....core import annotations as core_annots
from ....i18n import tr
from ...widgets.color_button import to_qcolor
from ._annot_common import PRESETS, AnnotStyle, AnnotStylePanel
from .base import ToolRegistry
from .select import SelectTool


class MarkupTool(SelectTool):
    kind = "highlight"
    mode = "annotate"
    cursor = Qt.CursorShape.IBeamCursor
    shows_bar = False
    default_style = AnnotStyle(PRESETS[0], 1.0)

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.style = AnnotStyle.load(self.id, self.default_style)
        self._panel: AnnotStylePanel | None = None

    def selection_brush(self) -> QColor:
        c = to_qcolor(self.style.color)
        c.setAlphaF(0.45 if self.kind == "highlight" else 0.25)
        return c

    def properties_widget(self):
        if self._panel is None:
            self._panel = AnnotStylePanel(self.label, self.hint, self.style, sample=self.kind)
            self._panel.styleChanged.connect(self._style_changed)
        return self._panel

    def _style_changed(self, style: AnnotStyle) -> None:
        self.style = style
        style.save(self.id)

    def selection_finished(self) -> None:
        rects = self.line_rects()
        self.clear_selection()
        if rects:
            core_annots.add_markup(self.ctx.pdf, self.kind, rects, self.style.color, self.style.opacity)


@ToolRegistry.register
class HighlightTool(MarkupTool):
    id = "annotate.highlight"
    kind = "highlight"
    label = tr("annot.highlight")
    hint = tr("annot.highlight.hint")
    icon = "highlighter-circle"
    shortcut = "H"
    order = 10
    default_style = AnnotStyle(PRESETS[0], 1.0)


@ToolRegistry.register
class UnderlineTool(MarkupTool):
    id = "annotate.underline"
    kind = "underline"
    label = tr("annot.underline")
    hint = tr("annot.underline.hint")
    icon = "text-underline"
    shortcut = "U"
    order = 20
    default_style = AnnotStyle(PRESETS[2], 1.0)


@ToolRegistry.register
class StrikeTool(MarkupTool):
    id = "annotate.strikeout"
    kind = "strikeout"
    label = tr("annot.strikeout")
    hint = tr("annot.strikeout.hint")
    icon = "text-strikethrough"
    shortcut = "K"
    order = 30
    default_style = AnnotStyle(PRESETS[4], 1.0)
