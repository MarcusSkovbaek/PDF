""""Tilføj tekst" – type new text anywhere, in the document's own style.

Click to start typing at that point or drag to set the box width. The
alignment buttons in the inspector (venstre / centreret / højre) decide how
the text sits in its box – for a click they work like point text: the text
starts at, is centred on, or ends at the click. The inline editor previews
the alignment exactly. The editor and inspector are the ones of "Rediger
tekst"; defaults come from :func:`pdfstudio.core.insert.document_text_style`
(the document's body text), and the last used style and alignment are
remembered while the app runs (the alignment also in local QSettings).
Saving calls :func:`pdfstudio.core.insert.add_text_box`.
"""

from __future__ import annotations

import pymupdf
from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import QPen
from PySide6.QtWidgets import QGraphicsRectItem

from ....core import fonts as core_fonts
from ....core import insert as core_insert
from ....core import text_edit as te
from ....i18n import tr
from ...theme import tokens as T
from .base import ToolMouseEvent, ToolRegistry
from .edit_text import EditTextTool, _settings

MIN_BOX = 12.0       # pt; smaller drags count as a click
CLICK_MARGIN = 36.0  # pt kept free at the page edges for click-placed boxes
ALIGN_KEY = "edit/addTextAlign"


def _metrics(doc: pymupdf.Document, page: int, style: core_fonts.SpanStyle) -> tuple[float, float]:
    """(ascender, line height) in pt – exactly what add_text_box uses."""
    try:
        font = core_fonts.resolve_font(doc, page, style, "Ag").font
        asc = float(getattr(font, "ascender", 0.9) or 0.9)
        desc = float(getattr(font, "descender", -0.25) or -0.25)
    except Exception:
        asc, desc = 0.9, -0.25
    return asc * style.size, style.size * max(1.15, asc - desc)


@ToolRegistry.register
class AddTextTool(EditTextTool):
    id = "edit.add_text"
    label = tr("edit.add_text.label")
    hint = tr("edit.add_text.hint")
    icon = "text-aa"
    shortcut = "T"
    mode = "edit"
    order = 20
    cursor = Qt.CursorShape.CrossCursor
    idle_title_key = "edit.add_text.idle.title"
    idle_text_key = "edit.add_text.idle.text"
    supports_box_outlines = False
    supports_alignment = True

    _session_style: core_fonts.SpanStyle | None = None  # shared by all tabs

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._press: tuple[int, QPointF] | None = None
        self._drag_item: QGraphicsRectItem | None = None
        self._anchor: tuple[int, float, float] | None = None  # click point of a click-placed box
        try:
            align = str(_settings().value(ALIGN_KEY, "left"))
        except Exception:
            align = "left"
        self.align = align if align in core_insert.ALIGNMENTS else "left"

    # -- style --------------------------------------------------------------
    def default_style(self) -> core_fonts.SpanStyle:
        if AddTextTool._session_style is not None:
            return AddTextTool._session_style
        try:
            return core_insert.document_text_style(self.ctx.pdf.doc)
        except Exception:
            return core_fonts.SpanStyle("Helvetica", 11.0)

    def _block_pitch(self) -> float:
        return _metrics(self.ctx.pdf.doc, self.block.page, self.block.style)[1]

    def _editor_background(self):
        from PySide6.QtGui import QColor

        return QColor(0, 0, 0, 0)  # nothing to hide: keep the page visible under new text

    def _layout_for(self, block):
        return te.LayoutInfo(self.align, float(block.bbox.width))

    def _wrap_width(self) -> float:
        return self.block.bbox.width

    # -- alignment ------------------------------------------------------------
    def set_align(self, align: str) -> None:
        """Alignment of the text in its box ("left" | "center" | "right");
        updates the open editor live."""
        if align not in core_insert.ALIGNMENTS:
            raise ValueError(f"align: {align!r}")
        self.align = align
        try:
            _settings().setValue(ALIGN_KEY, align)
        except Exception:
            pass
        if self.block is not None and self.editor is not None:
            if self._anchor is not None and self._anchor[0] == self.block.page:
                box = self._click_box(*self._anchor, self.block.style)
                self.block.bbox = box
                for ln in self.block.lines:
                    ln.bbox = pymupdf.Rect(box)
                    ln.origin = (box.x0, ln.origin[1])
                if self._frame is not None:
                    self._frame.setRect(QRectF(box.x0, box.y0, box.width, box.height))
            self._layout = self._layout_for(self.block)
            self._apply_editor_style()
            self.editor.setFocus()
        if self._panel is not None:
            self._panel.refresh()

    def _click_box(self, page: int, x: float, y: float, style: core_fonts.SpanStyle) -> pymupdf.Rect:
        """Box for text typed at a click: the baseline goes through the click;
        left-aligned text starts there and may run to the right margin,
        centred text is centred on it, right-aligned text ends there."""
        page_rect = self.ctx.pdf.page(page).rect
        asc, _lh = _metrics(self.ctx.pdf.doc, page, style)
        lo, hi = page_rect.x0 + CLICK_MARGIN, page_rect.x1 - CLICK_MARGIN
        least = 4 * style.size
        if self.align == "center":
            half = max(min(x - lo, hi - x), least / 2)
            left, right = x - half, x + half
        elif self.align == "right":
            left, right = min(lo, x - least), x
        else:
            left, right = x, max(x + least, hi)
        top = y - asc * 0.7
        return pymupdf.Rect(left, top, right, top + style.size * 1.3)

    # -- placing ----------------------------------------------------------------
    def block_at(self, page, pos):  # never pick up existing text
        return None

    def mouse_move(self, ev: ToolMouseEvent) -> None:
        if self._press is not None and self._drag_item is not None:
            page, start = self._press
            self._drag_item.setRect(QRectF(start, ev.pos_on(page)).normalized())
            return
        self.ctx.view.set_tool_cursor(Qt.CursorShape.CrossCursor if ev.page >= 0 else Qt.CursorShape.ArrowCursor)

    def mouse_press(self, ev: ToolMouseEvent) -> None:
        if not ev.left:
            return
        if self.editor is not None:
            self.commit()  # a click outside finishes the current text (Acrobat)
            return
        if ev.page < 0:
            return
        self._press = (ev.page, QPointF(ev.pos))
        self._drag_item = QGraphicsRectItem()
        pen = QPen(T.qcolor("accent"), 1.2, Qt.PenStyle.DashLine)
        pen.setCosmetic(True)
        self._drag_item.setPen(pen)
        self._drag_item.setZValue(40)
        self.ctx.view.add_overlay(ev.page, self._drag_item)

    def mouse_release(self, ev: ToolMouseEvent) -> None:
        if self._press is None:
            return
        page, start = self._press
        self._press = None
        if self._drag_item is not None:
            self.ctx.view.remove_overlay(self._drag_item)
            self._drag_item = None
        end = ev.pos_on(page)
        style = self.default_style()
        r = QRectF(start, end).normalized()
        if r.width() >= MIN_BOX:
            box = pymupdf.Rect(r.left(), r.top(), r.right(), r.bottom())
            anchor = None
        else:  # click: point text anchored at the click
            box = self._click_box(page, start.x(), start.y(), style)
            anchor = (page, start.x(), start.y())
        self.start_new(page, box, style)
        self._anchor = anchor

    def start_new(self, page: int, box: pymupdf.Rect, style: core_fonts.SpanStyle) -> None:
        asc, _lh = _metrics(self.ctx.pdf.doc, page, style)
        line = te.TextLine(text="", bbox=pymupdf.Rect(box), origin=(box.x0, box.y0 + asc), style=style)
        self._anchor = None
        self.start_edit(te.TextBlock(page=page, bbox=pymupdf.Rect(box), lines=[line], style=style))

    def _write(self, block: te.TextBlock, text: str, style: core_fonts.SpanStyle | None) -> None:
        final = style or block.style
        AddTextTool._session_style = final
        core_insert.add_text_box(self.ctx.pdf, block.page, block.bbox, text, final, align=self.align)

    def _teardown(self) -> None:
        self._anchor = None
        super()._teardown()

    def deactivate(self) -> None:
        if self._drag_item is not None and self.ctx is not None:
            self.ctx.view.remove_overlay(self._drag_item)
        self._drag_item = None
        self._press = None
        super().deactivate()
