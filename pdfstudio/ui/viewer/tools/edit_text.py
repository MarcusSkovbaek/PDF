""""Rediger tekst" – edit existing PDF text in place, Acrobat style.

Hovering outlines the paragraph under the cursor. Clicking opens an inline
editor exactly on top of it, using the paragraph's own font, size, colour and
line pitch, painted on the page's own background colour so the original text
does not show through. Committing calls
:func:`pdfstudio.core.text_edit.replace_block_text`, which rewrites the text
in the same font inside the PDF (one undo step).

Keys while editing: Ctrl+Enter or clicking elsewhere saves, Tab saves and
opens the next paragraph, Esc cancels. The inspector shows the paragraph's
formatting; changing it overrides the style for the whole paragraph.

Extension points: ``InlineTextEditor`` (geometry/appearance),
``EditTextTool.start_edit``/``commit``/``cancel`` (session flow) and
``_background_color`` (page background sampling).
"""

from __future__ import annotations

from collections import Counter
from dataclasses import replace

import pymupdf
from PySide6.QtCore import QEvent, QPointF, QRectF, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QFont, QFontMetricsF, QKeyEvent, QPen, QTextBlockFormat, QTextCursor
from PySide6.QtWidgets import (
    QComboBox,
    QDoubleSpinBox,
    QFormLayout,
    QFrame,
    QGraphicsRectItem,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from ....core import RELOADED, STRUCTURE
from ....core import fonts as core_fonts
from ....core import text_edit as te
from ....i18n import tr
from ...theme import tokens as T
from ...widgets.color_button import ColorButton, to_qcolor
from ...widgets.util import IconButton, label
from .base import Tool, ToolMouseEvent, ToolRegistry

HOVER_PAD = 2.0      # pt around a hovered paragraph
EDIT_PAD = 3.0       # pt around the paragraph being edited


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------
def _line_pitch(block: te.TextBlock) -> float:
    """Distance between consecutive baselines (pt) – default 1.2 × size."""
    ys = [ln.origin[1] for ln in block.lines]
    gaps = sorted(b - a for a, b in zip(ys, ys[1:]) if b - a > 0.3 * block.style.size)
    return gaps[len(gaps) // 2] if gaps else block.style.size * 1.2


def _background_color(doc: pymupdf.Document, page: int, rect: pymupdf.Rect) -> QColor:
    """Most common colour on a thin ring just outside ``rect`` – the paper the
    paragraph sits on (white, tinted boxes, dark slides ...)."""
    try:
        ring = pymupdf.Rect(rect.x0 - 3, rect.y0 - 3, rect.x1 + 3, rect.y1 + 3) & doc[page].rect
        pix = doc[page].get_pixmap(matrix=pymupdf.Matrix(1, 1), clip=ring, alpha=False)
    except Exception:
        return QColor("white")
    w, h = pix.width, pix.height
    if w < 2 or h < 2:
        return QColor("white")
    samples = Counter()
    step = max(1, (w + h) // 120)
    for x in range(0, w, step):
        samples[pix.pixel(x, 0)] += 1
        samples[pix.pixel(x, h - 1)] += 1
    for y in range(0, h, step):
        samples[pix.pixel(0, y)] += 1
        samples[pix.pixel(w - 1, y)] += 1
    (r, g, b, *_), _n = samples.most_common(1)[0]
    return QColor(r, g, b)


def _style_key(style: core_fonts.SpanStyle) -> tuple:
    return (style.font, round(style.size, 2), tuple(round(c, 3) for c in style.color), style.bold, style.italic)


# ---------------------------------------------------------------------------
# inline editor
# ---------------------------------------------------------------------------
class InlineTextEditor(QTextEdit):
    """Frameless editor laid over a paragraph; geometry in viewport pixels."""

    commitRequested = Signal()
    cancelRequested = Signal()
    nextRequested = Signal()

    def __init__(self, viewport: QWidget) -> None:
        super().__init__(viewport)
        self.setObjectName("InlineTextEditor")
        self.setAcceptRichText(False)
        self.setFrameShape(QFrame.Shape.NoFrame)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setLineWrapMode(QTextEdit.LineWrapMode.FixedPixelWidth)
        self.setTabChangesFocus(False)
        self.document().setDocumentMargin(0)
        self.setContentsMargins(0, 0, 0, 0)
        self.setViewportMargins(0, 0, 0, 0)
        self._line_px = 0.0
        self.textChanged.connect(self._apply_line_height)

    def configure(self, font: QFont, color: QColor, background: QColor, line_px: float, wrap_px: int) -> None:
        self.setFont(font)
        self.document().setDefaultFont(font)
        self._line_px = line_px
        self.setLineWrapColumnOrWidth(max(8, wrap_px))
        sel = T.qcolor("accent")
        sel.setAlpha(70)
        self.setStyleSheet(
            "QTextEdit#InlineTextEditor { border: none; padding: 0px;"
            f" background: {background.name()}; color: {color.name()};"
            f" selection-background-color: rgba({sel.red()},{sel.green()},{sel.blue()},{sel.alpha()});"
            f" selection-color: {color.name()}; }}")
        self._apply_line_height()

    def _apply_line_height(self) -> None:
        if self._line_px <= 0:
            return
        doc = self.document()
        cur = QTextCursor(doc)
        cur.select(QTextCursor.SelectionType.Document)
        fmt = QTextBlockFormat()
        fmt.setLineHeight(self._line_px, QTextBlockFormat.LineHeightTypes.FixedHeight.value)
        self.blockSignals(True)
        cur.mergeBlockFormat(fmt)
        self.blockSignals(False)

    def first_baseline(self) -> tuple[float, float]:
        """(x, y) of the first character's baseline inside the editor."""
        block = self.document().begin()
        layout = block.layout()
        if layout is None or layout.lineCount() == 0:
            self.document().documentLayout().blockBoundingRect(block)  # force layout
            layout = block.layout()
        if layout is None or layout.lineCount() == 0:
            return 0.0, QFontMetricsF(self.font()).ascent()
        line = layout.lineAt(0)
        pos = layout.position()
        off = self.viewport().pos()
        return (off.x() + pos.x() + line.cursorToX(0)[0] if isinstance(line.cursorToX(0), tuple)
                else off.x() + pos.x() + line.cursorToX(0),
                off.y() + pos.y() + line.y() + line.ascent())

    def content_height(self) -> float:
        lines = max(1, self.document().lineCount() if self.lineWrapMode() == QTextEdit.LineWrapMode.NoWrap
                    else self._visual_lines())
        return lines * self._line_px

    def _visual_lines(self) -> int:
        n = 0
        block = self.document().begin()
        while block.isValid():
            layout = block.layout()
            n += max(1, layout.lineCount() if layout is not None else 1)
            block = block.next()
        return n

    def keyPressEvent(self, ev: QKeyEvent) -> None:  # noqa: N802
        key, mods = ev.key(), ev.modifiers()
        if key == Qt.Key.Key_Escape:
            ev.accept()
            self.cancelRequested.emit()
            return
        if key in (Qt.Key.Key_Return, Qt.Key.Key_Enter) and mods & Qt.KeyboardModifier.ControlModifier:
            ev.accept()
            self.commitRequested.emit()
            return
        if key == Qt.Key.Key_Tab and not mods & Qt.KeyboardModifier.ShiftModifier:
            ev.accept()
            self.nextRequested.emit()
            return
        super().keyPressEvent(ev)

    def event(self, ev) -> bool:
        # keep Tab for us instead of focus navigation
        if ev.type() == QEvent.Type.KeyPress and ev.key() == Qt.Key.Key_Tab:
            self.keyPressEvent(ev)
            return True
        return super().event(ev)


# ---------------------------------------------------------------------------
# properties panel
# ---------------------------------------------------------------------------
class EditTextPanel(QWidget):
    def __init__(self, tool: "EditTextTool") -> None:
        super().__init__()
        self.tool = tool
        v = QVBoxLayout(self)
        v.setContentsMargins(16, 4, 16, 16)
        v.setSpacing(10)
        card = QFrame()
        card.setObjectName("Card")
        cl = QVBoxLayout(card)
        cl.setContentsMargins(14, 12, 14, 12)
        cl.setSpacing(3)
        self.title = label("", "heading")
        self.text = label("", "muted", wrap=True)
        cl.addWidget(self.title)
        cl.addWidget(self.text)
        v.addWidget(card)

        self.format_box = QWidget()
        fv = QVBoxLayout(self.format_box)
        fv.setContentsMargins(0, 6, 0, 0)
        fv.setSpacing(8)
        fv.addWidget(label(tr("edit.text.section.format"), "section"))
        form = QFormLayout()
        form.setSpacing(8)
        self.font = QComboBox()
        self.font.setMinimumWidth(150)
        self.font.activated.connect(self._changed)
        form.addRow(tr("edit.text.font"), self.font)
        self.size = QDoubleSpinBox()
        self.size.setRange(2.0, 400.0)
        self.size.setDecimals(1)
        self.size.setSingleStep(0.5)
        self.size.setSuffix(" pt")
        self.size.valueChanged.connect(self._changed)
        form.addRow(tr("edit.text.size"), self.size)
        row = QHBoxLayout()
        row.setSpacing(6)
        self.color = ColorButton(title=tr("edit.text.color_title"))
        self.color.colorChanged.connect(self._changed)
        self.bold = IconButton("text-bolder", tr("edit.text.bold"), variant="icon", checkable=True)
        self.italic = IconButton("text-italic", tr("edit.text.italic"), variant="icon", checkable=True)
        self.bold.toggled.connect(self._changed)
        self.italic.toggled.connect(self._changed)
        row.addWidget(self.color)
        row.addWidget(self.bold)
        row.addWidget(self.italic)
        row.addStretch(1)
        form.addRow(tr("edit.text.color"), row)
        fv.addLayout(form)
        self.reset = QPushButton(tr("edit.text.reset"))
        self.reset.setProperty("variant", "ghost")
        self.reset.setCursor(Qt.CursorShape.PointingHandCursor)
        self.reset.clicked.connect(self.tool.reset_style)
        fv.addWidget(self.reset, 0, Qt.AlignmentFlag.AlignLeft)
        self.rotated_note = label(tr("edit.text.rotated"), "caption", wrap=True)
        fv.addWidget(self.rotated_note)
        v.addWidget(self.format_box)
        v.addStretch(1)
        self._loading = False
        self.refresh()

    def refresh(self) -> None:
        block, style = self.tool.block, self.tool.current_style()
        editing = block is not None and style is not None
        self.format_box.setVisible(editing)
        if not editing:
            self.title.setText(tr("edit.text.idle.title"))
            self.text.setText(tr("edit.text.idle.text"))
            return
        self.title.setText(core_fonts.clean_font_name(block.style.font) or "–")
        self.text.setText(f"{block.style.size:g} pt")
        self._loading = True
        try:
            original = core_fonts.clean_font_name(block.style.font)
            self.font.clear()
            self.font.addItem(original, block.style.font)
            self.font.setItemData(0, tr("edit.text.font.original", name=original), Qt.ItemDataRole.ToolTipRole)
            for fam in core_fonts.available_families():
                if fam != original:
                    self.font.addItem(fam, fam)
            idx = self.font.findData(style.font)
            self.font.setCurrentIndex(max(0, idx))
            self.size.setValue(style.size)
            self.color.set_color(tuple(style.color))
            self.bold.setChecked(style.bold)
            self.italic.setChecked(style.italic)
            self.reset.setEnabled(self.tool.style_override is not None)
            self.rotated_note.setVisible(bool(block.rotation))
        finally:
            self._loading = False

    def _changed(self, *_a) -> None:
        if self._loading or self.tool.block is None:
            return
        base = self.tool.block.style
        self.tool.set_style(replace(
            base,
            font=self.font.currentData() or base.font,
            size=float(self.size.value()),
            color=tuple(self.color.color()),
            bold=self.bold.isChecked(),
            italic=self.italic.isChecked(),
        ))
        self.reset.setEnabled(self.tool.style_override is not None)


# ---------------------------------------------------------------------------
# the tool
# ---------------------------------------------------------------------------
@ToolRegistry.register
class EditTextTool(Tool):
    id = "edit.text"
    label = tr("edit.text.label")
    hint = tr("edit.text.hint")
    icon = "text-t"
    shortcut = "E"
    mode = "edit"
    order = 10
    cursor = Qt.CursorShape.IBeamCursor

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._blocks: dict[int, list[te.TextBlock]] = {}
        self._hover: QGraphicsRectItem | None = None
        self._frame: QGraphicsRectItem | None = None
        self.editor: InlineTextEditor | None = None
        self._keys: QLabel | None = None
        self.block: te.TextBlock | None = None
        self.style_override: core_fonts.SpanStyle | None = None
        self._panel: EditTextPanel | None = None
        self._committing = False

    # -- lifecycle ---------------------------------------------------------
    def activate(self, ctx) -> None:
        super().activate(ctx)
        view = ctx.view
        view.zoomChanged.connect(self._reposition)
        view.horizontalScrollBar().valueChanged.connect(self._reposition)
        view.verticalScrollBar().valueChanged.connect(self._reposition)
        # resolving fonts the first time scans the system fonts – do it early
        QTimer.singleShot(0, core_fonts.available_families)

    def deactivate(self) -> None:
        self.commit()
        self._clear_hover()
        if self.ctx is not None:
            view = self.ctx.view
            for sig in (view.zoomChanged, view.horizontalScrollBar().valueChanged,
                        view.verticalScrollBar().valueChanged):
                try:
                    sig.disconnect(self._reposition)
                except (RuntimeError, TypeError):
                    pass
        super().deactivate()

    def properties_widget(self) -> QWidget | None:
        if self._panel is None:
            self._panel = EditTextPanel(self)
        self._panel.refresh()
        return self._panel

    def document_changed(self, event) -> None:
        self._blocks.clear()
        if self.editor is not None and not self._committing and event.kind in (RELOADED, STRUCTURE):
            self.cancel()  # undo/redo or page changes invalidate the open paragraph

    # -- block lookup --------------------------------------------------------
    def blocks(self, page: int) -> list[te.TextBlock]:
        if page not in self._blocks:
            try:
                self._blocks[page] = te.text_blocks(self.ctx.pdf.doc, page)
            except Exception:
                self._blocks[page] = []
        return self._blocks[page]

    def block_at(self, page: int, pos: QPointF) -> te.TextBlock | None:
        if page < 0:
            return None
        x, y = pos.x(), pos.y()
        hits = [b for b in self.blocks(page)
                if b.bbox.x0 - HOVER_PAD <= x <= b.bbox.x1 + HOVER_PAD
                and b.bbox.y0 - HOVER_PAD <= y <= b.bbox.y1 + HOVER_PAD]
        return min(hits, key=lambda b: b.bbox.width * b.bbox.height) if hits else None

    # -- hover ---------------------------------------------------------------
    def _clear_hover(self) -> None:
        if self._hover is not None and self.ctx is not None:
            self.ctx.view.remove_overlay(self._hover)
        self._hover = None

    def _outline(self, page: int, rect: pymupdf.Rect, pad: float, *, strong: bool) -> QGraphicsRectItem:
        item = QGraphicsRectItem(QRectF(rect.x0 - pad, rect.y0 - pad, rect.width + 2 * pad, rect.height + 2 * pad))
        color = T.qcolor("accent")
        pen = QPen(color, 1.6 if strong else 1.0)
        pen.setCosmetic(True)
        if not strong:
            pen.setStyle(Qt.PenStyle.DashLine)
            fill = QColor(color)
            fill.setAlpha(14)
            item.setBrush(fill)
        item.setPen(pen)
        item.setZValue(30 if strong else 20)
        self.ctx.view.add_overlay(page, item)
        return item

    def mouse_move(self, ev: ToolMouseEvent) -> None:
        block = self.block_at(ev.page, ev.pos)
        if block is not None and self.block is not None and block.page == self.block.page \
                and block.bbox == self.block.bbox:
            block = None  # already editing it
        self._clear_hover()
        if block is not None:
            self._hover = self._outline(block.page, block.bbox, HOVER_PAD, strong=False)
            self.ctx.view.viewport().setCursor(Qt.CursorShape.IBeamCursor)
        else:
            self.ctx.view.viewport().setCursor(Qt.CursorShape.ArrowCursor)

    def mouse_press(self, ev: ToolMouseEvent) -> None:
        if not ev.left:
            return
        block = self.block_at(ev.page, ev.pos)
        # clicking elsewhere saves the open paragraph first
        if self.editor is not None:
            same = block is not None and self.block is not None and block.page == self.block.page \
                and block.bbox == self.block.bbox
            if same:
                return
            target = (block.page, QPointF(ev.pos)) if block is not None else None
            self.commit()
            if target is not None:
                block = self.block_at(*target)
        if block is not None:
            self._clear_hover()
            self.start_edit(block)

    def key_press(self, ev: QKeyEvent) -> bool:
        if ev.key() == Qt.Key.Key_Escape and self.editor is not None:
            self.cancel()
            return True
        return False

    # -- editing session -----------------------------------------------------
    def current_style(self) -> core_fonts.SpanStyle | None:
        if self.block is None:
            return None
        return self.style_override or self.block.style

    def set_style(self, style: core_fonts.SpanStyle) -> None:
        if self.block is None:
            return
        self.style_override = None if _style_key(style) == _style_key(self.block.style) else style
        self._apply_editor_style()

    def reset_style(self) -> None:
        self.style_override = None
        self._apply_editor_style()
        if self._panel is not None:
            self._panel.refresh()

    def start_edit(self, block: te.TextBlock) -> None:
        self.cancel()
        view = self.ctx.view
        self.block = block
        self.style_override = None
        self.editor = InlineTextEditor(view.viewport())
        self.editor.commitRequested.connect(self.commit)
        self.editor.cancelRequested.connect(self.cancel)
        self.editor.nextRequested.connect(self.commit_and_next)
        self.editor.textChanged.connect(self._reposition)
        self.editor.setPlainText(block.text)
        self._keys = QLabel(tr("edit.text.keys"), view.viewport())
        # the key hint may cover the next paragraph: drop it once typing starts
        self.editor.textChanged.connect(self._keys.hide)
        self._keys.setObjectName("EditKeysHint")
        self._frame = self._outline(block.page, block.bbox, EDIT_PAD, strong=True)
        self._apply_editor_style()
        self.editor.show()
        self._keys.show()
        self.editor.setFocus(Qt.FocusReason.MouseFocusReason)
        self.editor.selectAll() if not block.text.strip() else self.editor.moveCursor(QTextCursor.MoveOperation.End)
        self.changed.emit()
        if self._panel is not None:
            self._panel.refresh()

    def _px_per_pt(self) -> float:
        view, page = self.ctx.view, self.block.page
        a = view.mapFromScene(view.page_to_scene(page, QPointF(0, 0)))
        b = view.mapFromScene(view.page_to_scene(page, QPointF(100, 0)))
        c = view.mapFromScene(view.page_to_scene(page, QPointF(0, 100)))
        return max(abs(b.x() - a.x()), abs(b.y() - a.y()), abs(c.y() - a.y()), abs(c.x() - a.x())) / 100.0

    def _apply_editor_style(self) -> None:
        if self.editor is None or self.block is None:
            return
        style = self.current_style()
        ppp = self._px_per_pt()
        font = QFont(core_fonts.qt_family_hint(style) if self.style_override is None or
                     self.style_override.font == self.block.style.font else style.font)
        font.setPixelSize(max(1, round(style.size * ppp)))
        font.setBold(style.bold)
        font.setItalic(style.italic)
        font.setKerning(True)
        font.setHintingPreference(QFont.HintingPreference.PreferNoHinting)
        if self.style_override is None or self.style_override.font == self.block.style.font:
            font.setLetterSpacing(QFont.SpacingType.PercentageSpacing, self._spacing_fix(font, ppp))
        pitch = self._block_pitch() * (style.size / max(self.block.style.size, 0.1))
        bg = _background_color(self.ctx.pdf.doc, self.block.page, self.block.bbox)
        width_px = int(round(self._wrap_width() * ppp))
        self.editor.configure(font, to_qcolor(style.color), bg, pitch * ppp, width_px)
        self._reposition()

    def _block_pitch(self) -> float:
        """Baseline-to-baseline distance (pt) the editor should use."""
        return _line_pitch(self.block)

    def _write(self, block: te.TextBlock, text: str, style: core_fonts.SpanStyle | None) -> None:
        """Put the edited text into the PDF (one undo step)."""
        te.replace_block_text(self.ctx.pdf, block, text, style)

    def _spacing_fix(self, font: QFont, ppp: float) -> float:
        """Letter spacing (%) that makes the on-screen font as wide as the
        PDF's: the screen family is often a metric-close substitute."""
        lines = [ln for ln in self.block.lines if len(ln.text.strip()) >= 4]
        if not lines or self.block.rotation:
            return 100.0
        ln = max(lines, key=lambda ln: len(ln.text))
        qt_w = QFontMetricsF(font).horizontalAdvance(ln.text.rstrip())
        pdf_w = ln.bbox.width * ppp
        if qt_w <= 0:
            return 100.0
        return max(85.0, min(115.0, 100.0 * pdf_w / qt_w))

    def _wrap_width(self) -> float:
        """Mirror core.text_edit's rule (see ``_available_width``): a single
        line grows sideways within the page's text area, a paragraph keeps
        its column width. A little slack keeps the caret visible."""
        b = self.block
        if b.rotation:
            return max(b.bbox.width, b.bbox.height) + 2
        slack = max(2.0, 0.3 * b.style.size)
        if len(b.lines) == 1:
            page_w = self.ctx.pdf.page(b.page).rect.width
            margin = max(36.0, min(b.bbox.x0, 72.0))
            return max(b.bbox.width, page_w - margin - b.bbox.x0) + slack
        return b.bbox.width + slack

    def _reposition(self, *_a) -> None:
        if self.editor is None or self.block is None:
            return
        view, b = self.ctx.view, self.block
        ppp = self._px_per_pt()
        style = self.current_style()
        first = b.lines[0] if b.lines else None
        fm = QFontMetricsF(self.editor.font())
        if first is not None and not b.rotation:
            origin = QPointF(first.origin[0], first.origin[1])
        else:
            origin = QPointF(b.bbox.x0, b.bbox.y0 + style.size * 0.8)
        top_left = view.mapFromScene(view.page_to_scene(b.page, origin))
        line_px = self.editor._line_px or fm.height()
        # place the editor so its first baseline lands exactly on the PDF's
        # (measured from Qt's own layout: margins/leading differ per platform)
        bx, by = self.editor.first_baseline()
        x = top_left.x() - bx
        y = top_left.y() - by
        w = self.editor.lineWrapColumnOrWidth() + 2
        h = max(line_px, self.editor.content_height()) + 2
        self.editor.setGeometry(int(round(x)), int(round(y)), int(w), int(round(h)))
        if self._frame is not None:
            # grow the frame with the text (pt)
            hh = max(b.bbox.height, h / ppp)
            ww = max(b.bbox.width, (w - 2) / ppp)
            self._frame.setRect(QRectF(b.bbox.x0 - EDIT_PAD, b.bbox.y0 - EDIT_PAD,
                                       ww + 2 * EDIT_PAD, hh + 2 * EDIT_PAD))
        if self._keys is not None:
            self._keys.adjustSize()
            frame_bottom = view.mapFromScene(view.page_to_scene(
                b.page, QPointF(b.bbox.x0 - EDIT_PAD, b.bbox.y0 + max(b.bbox.height, h / ppp) + EDIT_PAD)))
            self._keys.move(int(frame_bottom.x()), int(frame_bottom.y() + 6))

    def _teardown(self) -> None:
        if self.editor is not None:
            self.editor.hide()
            self.editor.deleteLater()
        if self._keys is not None:
            self._keys.hide()
            self._keys.deleteLater()
        if self._frame is not None and self.ctx is not None:
            self.ctx.view.remove_overlay(self._frame)
        self.editor = self._keys = self._frame = None
        self.block = None
        self.style_override = None
        if self._panel is not None:
            self._panel.refresh()
        self.changed.emit()

    def cancel(self) -> None:
        if self.editor is None:
            return
        self._teardown()
        self.ctx.view.setFocus()

    def commit(self) -> bool:
        """Write the editor's text into the PDF. Returns True if it changed."""
        if self.editor is None or self.block is None or self._committing:
            return False
        block, text, style = self.block, self.editor.toPlainText(), self.style_override
        self._teardown()
        if text == block.text and style is None:
            return False
        self._committing = True
        try:
            self._write(block, text, style)
        except Exception as exc:  # noqa: BLE001 – never lose the user's session silently
            self.ctx.toast(tr("edit.text.error", err=str(exc)), "error")
            return False
        finally:
            self._committing = False
        self._blocks.pop(block.page, None)
        return True

    def commit_and_next(self) -> None:
        """Save, then open the next paragraph in reading order (Tab)."""
        if self.block is None:
            return
        page, old = self.block.page, self.blocks(self.block.page)
        idx = next((i for i, b in enumerate(old) if b.bbox == self.block.bbox), -1)
        nxt = old[idx + 1] if 0 <= idx < len(old) - 1 else None
        anchor = QPointF((nxt.bbox.x0 + nxt.bbox.x1) / 2, (nxt.bbox.y0 + nxt.bbox.y1) / 2) if nxt else None
        self.commit()
        target = None
        if anchor is not None:
            target = self.block_at(page, anchor)
        else:
            for p in range(page + 1, self.ctx.pdf.page_count):
                if self.blocks(p):
                    target = self.blocks(p)[0]
                    break
        if target is not None:
            self.ctx.view.go_to_page(target.page) if target.page != page else None
            self.start_edit(target)
