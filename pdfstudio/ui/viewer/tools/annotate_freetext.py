""""Tekstfelt" – typewriter text (standard ``/FreeText`` annotations).

Click on the page to start typing there (the box grows with the text), or
drag to set the box width (the text wraps inside it). The inline editor is
laid over the page with the box's own font, size, colour and line pitch
(mirroring MuPDF's free text layout), so what you type is what gets saved.
Esc, Ctrl+Enter or a click elsewhere finishes; the inspector sets font, size
and colour (also while typing). Existing text boxes are edited the same way
by double-clicking them with "Markér kommentar" (see :class:`FreeTextSession`).
"""

from __future__ import annotations

from dataclasses import replace

import pymupdf
from PySide6.QtCore import QEvent, QObject, QPointF, QRectF, Qt, Signal
from PySide6.QtGui import QColor, QFont, QFontMetricsF, QKeyEvent, QPainter, QPen, QTextBlockFormat, QTextCursor
from PySide6.QtWidgets import QFrame, QGraphicsRectItem, QLabel, QTextEdit, QWidget

from ....core import RELOADED, STRUCTURE
from ....core import annotations as core_annots
from ....core import fonts as core_fonts
from ....i18n import tr
from ...theme import tokens as T
from ...widgets.color_button import to_qcolor
from ._annot_common import PRESETS, AnnotStyle, PagePreview, StyledTool
from .base import Tool, ToolMouseEvent, ToolRegistry

MIN_BOX = 12.0       # pt; narrower drags count as a click
EDGE = 4.0           # pt kept free at the right page edge by growing boxes
FRAME_PAD = 3.0      # pt between the text box and the editing frame
_PDF_FAMILY = {"sans": ("Helvetica", False, False), "serif": ("Times-Roman", True, False),
               "mono": ("Courier", False, True)}


def screen_font(style: AnnotStyle, px: float) -> QFont:
    """On-screen font that matches a base-14 free text font (metric-close)."""
    name, serif, mono = _PDF_FAMILY.get(style.font, _PDF_FAMILY["sans"])
    font = QFont(core_fonts.qt_family_hint(core_fonts.SpanStyle(name, style.fontsize, serif=serif, mono=mono)))
    font.setPixelSize(max(1, round(px)))
    font.setKerning(True)
    font.setHintingPreference(QFont.HintingPreference.PreferNoHinting)
    return font


def style_of(info: core_annots.AnnotInfo) -> AnnotStyle:
    """Inspector/editor style of an existing free text annotation."""
    font = info.font if info.font in core_annots.FREETEXT_FONTS else "sans"
    return AnnotStyle(tuple(info.color or (0.0, 0.0, 0.0)), float(info.opacity or 1.0), 0.0,
                      tuple(info.fill) if info.fill else None, float(info.fontsize or 12.0), font)


def is_auto_width(info: core_annots.AnnotInfo) -> bool:
    """Was the box sized to its text (click-created)? Then editing lets it
    grow sideways again instead of wrapping at its current width."""
    style = style_of(info)
    try:
        w, h = core_annots.free_text_size(info.contents, style.fontsize, style.font)
    except ValueError:
        return False
    return abs(info.rect.width - w) < 1.0 and abs(info.rect.height - h) < 1.0


# ---------------------------------------------------------------------------
# inline editor
# ---------------------------------------------------------------------------
class FreeTextEditor(QTextEdit):
    """Frameless, transparent plain-text editor laid over the page."""

    finishRequested = Signal()

    def __init__(self, viewport: QWidget) -> None:
        super().__init__(viewport)
        self.setObjectName("FreeTextEditor")
        self.setAcceptRichText(False)
        self.setFrameShape(QFrame.Shape.NoFrame)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setTabChangesFocus(False)
        self.document().setDocumentMargin(0)
        self.setContentsMargins(0, 0, 0, 0)
        self.setViewportMargins(0, 0, 0, 0)
        self._line_px = 0.0
        self._hint = ""
        self._hint_color = T.qcolor("text_subtle")
        self.textChanged.connect(self._apply_line_height)

    def set_hint(self, text: str) -> None:
        """Placeholder shown while empty (Qt's own is not drawn in NoWrap mode)."""
        self._hint = text
        self.viewport().update()

    def configure(self, font: QFont, color: QColor, line_px: float, wrap_px: int | None) -> None:
        self.setFont(font)
        self.document().setDefaultFont(font)
        sel = T.qcolor("accent")
        sel.setAlpha(70)
        self.setStyleSheet(
            "QTextEdit#FreeTextEditor { border: none; padding: 0px; background: transparent;"
            f" color: rgba({color.red()},{color.green()},{color.blue()},{color.alpha()});"
            f" selection-background-color: rgba({sel.red()},{sel.green()},{sel.blue()},{sel.alpha()});"
            f" selection-color: {color.name()}; }}")
        self._hint_color = QColor(color)
        self._hint_color.setAlphaF(color.alphaF() * 0.4)  # the hint previews the text colour
        if wrap_px is None:
            self.setLineWrapMode(QTextEdit.LineWrapMode.NoWrap)
        else:
            self.setLineWrapMode(QTextEdit.LineWrapMode.FixedPixelWidth)
            self.setLineWrapColumnOrWidth(max(8, int(wrap_px)))
        self._line_px = line_px
        self._apply_line_height()

    def _apply_line_height(self) -> None:
        if self._line_px <= 0:
            return
        cur = QTextCursor(self.document())
        cur.select(QTextCursor.SelectionType.Document)
        fmt = QTextBlockFormat()
        fmt.setLineHeight(self._line_px, QTextBlockFormat.LineHeightTypes.FixedHeight.value)
        self.blockSignals(True)
        cur.mergeBlockFormat(fmt)
        self.blockSignals(False)

    def first_baseline(self) -> QPointF:
        """Position of the first line's baseline start inside the widget."""
        block = self.document().begin()
        layout = block.layout()
        if layout is None or layout.lineCount() == 0:
            self.document().documentLayout().blockBoundingRect(block)  # force a layout
            layout = block.layout()
        off = self.viewport().pos()
        if layout is None or layout.lineCount() == 0:
            return QPointF(off.x(), off.y() + QFontMetricsF(self.font()).ascent())
        line = layout.lineAt(0)
        pos = layout.position()
        return QPointF(off.x() + pos.x() + line.x(), off.y() + pos.y() + line.y() + line.ascent())

    def paintEvent(self, ev) -> None:  # noqa: N802
        super().paintEvent(ev)
        if self._hint and self.document().isEmpty():
            p = QPainter(self.viewport())
            p.setFont(self.font())
            p.setPen(self._hint_color)
            base = self.first_baseline() - QPointF(self.viewport().pos())
            p.drawText(base, self._hint)
            p.end()

    def keyPressEvent(self, ev: QKeyEvent) -> None:  # noqa: N802
        key, mods = ev.key(), ev.modifiers()
        if key == Qt.Key.Key_Escape or (key in (Qt.Key.Key_Return, Qt.Key.Key_Enter)
                                        and mods & Qt.KeyboardModifier.ControlModifier):
            ev.accept()
            self.finishRequested.emit()
            return
        super().keyPressEvent(ev)

    def event(self, ev) -> bool:
        if ev.type() == QEvent.Type.ShortcutOverride and isinstance(ev, QKeyEvent) \
                and ev.key() == Qt.Key.Key_Escape:
            ev.accept()  # Esc finishes the text – not a window shortcut
            return True
        return super().event(ev)


# ---------------------------------------------------------------------------
# editing session
# ---------------------------------------------------------------------------
class FreeTextSession(QObject):
    """Typing a new text box, or editing an existing one (``xref``), in place.

    The box's top-left corner stays at ``origin`` (display pt). With
    ``width=None`` the box grows with the text (up to the page edge, then it
    wraps); otherwise the text wraps at ``width`` and the box is at least
    ``min_height`` tall. ``commit`` writes it in one undo step.
    """

    finished = Signal()

    def __init__(self, tool: Tool, page: int, origin: QPointF, style: AnnotStyle, *, text: str = "",
                 width: float | None = None, min_height: float = 0.0, xref: int | None = None) -> None:
        super().__init__(tool)
        self.tool = tool
        self.ctx = tool.ctx
        self.page = page
        self.origin = QPointF(origin)
        self.style = replace(style)       # what commit writes
        self._shown = self.style          # what the editor shows (may be a preview)
        self.width = width
        self.min_height = min_height
        self.xref = xref
        self.original_text = text
        self.original_style = replace(style)
        self.closed = False
        self.committing = False
        view = self.ctx.view
        self.editor = FreeTextEditor(view.viewport())
        self.editor.set_hint(tr("annot.freetext.placeholder"))
        self.editor.setPlainText(text)
        self.editor.finishRequested.connect(self.commit)
        self.editor.textChanged.connect(self._relayout)
        self.frame = QGraphicsRectItem()
        pen = QPen(T.qcolor("accent"), 1.2, Qt.PenStyle.DashLine)
        pen.setCosmetic(True)
        self.frame.setPen(pen)
        self.frame.setZValue(46)
        view.add_overlay(page, self.frame)
        self.keys = QLabel(tr("annot.freetext.keys"), view.viewport())
        self.keys.setObjectName("EditKeysHint")
        self.editor.textChanged.connect(self.keys.hide)  # it may cover what follows
        # an existing box: hide its rendering while the editor shows the text
        self.hider = PagePreview(view)
        if xref is not None:
            old = next((a.rect for a in core_annots.list_annotations(self.ctx.pdf.doc, page) if a.xref == xref),
                       None)
            if old is not None:
                self.hider.show(self.ctx.pdf.doc, page, {xref: None}, pymupdf.Rect(old) + (-2, -2, 2, 2))
        view.zoomChanged.connect(self._apply_style)
        view.horizontalScrollBar().valueChanged.connect(self._reposition)
        view.verticalScrollBar().valueChanged.connect(self._reposition)
        self._apply_style()
        self.editor.show()
        self.keys.show()
        self.editor.setFocus(Qt.FocusReason.MouseFocusReason)
        self.editor.moveCursor(QTextCursor.MoveOperation.End)

    # -- geometry (display pt) -------------------------------------------------
    def text(self) -> str:
        return self.editor.toPlainText() if not self.closed else ""

    def _page_width(self) -> float:
        return self.ctx.pdf.page(self.page).rect.width

    def _wrap_width(self, text: str) -> float | None:
        """Width the text wraps at: the fixed width, or the room left on the
        page once a growing box would cross the page edge."""
        if self.width is not None:
            return self.width
        room = max(self._shown.fontsize * 2, self._page_width() - self.origin.x() - EDGE)
        natural, _h = core_annots.free_text_size(text or " ", self._shown.fontsize, self._shown.font)
        return room if natural > room else None

    def _box(self, text: str | None = None) -> pymupdf.Rect:
        """The annotation rect the current text needs."""
        text = self.editor.toPlainText() if text is None else text
        shown = text if text.strip() else tr("annot.freetext.placeholder")
        wrap = self._wrap_width(shown)
        w, h = core_annots.free_text_size(shown, self._shown.fontsize, self._shown.font, wrap)
        h = max(h, self.min_height)
        x, y = self.origin.x(), self.origin.y()
        return pymupdf.Rect(x, y, x + w, y + h)

    def _px_per_pt(self) -> float:
        view = self.ctx.view
        a = view.mapFromScene(view.page_to_scene(self.page, QPointF(0, 0)))
        b = view.mapFromScene(view.page_to_scene(self.page, QPointF(100, 100)))
        return max(abs(b.x() - a.x()), abs(b.y() - a.y())) / 100.0

    # -- appearance -----------------------------------------------------------
    def set_style(self, style: AnnotStyle) -> None:
        """The style the text will be saved with."""
        self.style = replace(style)
        self._shown = self.style
        self._apply_style()

    def preview(self, props: dict | None) -> None:
        """Show the text with ``props`` (hovered colour, dragged size)
        without choosing them; ``None`` shows the chosen style again."""
        props = {k: v for k, v in (props or {}).items() if hasattr(self.style, k) and k != "fill"}
        self._shown = replace(self.style, **props) if props else self.style
        self._apply_style()

    def _apply_style(self, *_a) -> None:
        if self.closed:
            return
        ppp = self._px_per_pt()
        shown = self._shown
        color = to_qcolor(shown.color)
        color.setAlphaF(max(0.15, shown.opacity))
        wrap = self._wrap_width(self.editor.toPlainText())
        self.editor.configure(screen_font(shown, shown.fontsize * ppp), color,
                              core_annots.FREETEXT_LINE_HEIGHT * shown.fontsize * ppp,
                              None if wrap is None else round(wrap * ppp))
        self._reposition()

    def _relayout(self) -> None:
        # growing past the page edge switches to wrapping (and back)
        wrap = self._wrap_width(self.editor.toPlainText())
        wants_wrap = wrap is not None
        if wants_wrap != (self.editor.lineWrapMode() == QTextEdit.LineWrapMode.FixedPixelWidth):
            self._apply_style()
        else:
            self._reposition()

    def _reposition(self, *_a) -> None:
        if self.closed:
            return
        view = self.ctx.view
        ppp = self._px_per_pt()
        box = self._box()
        top_left = view.mapFromScene(view.page_to_scene(self.page, QPointF(box.x0, box.y0)))
        base = self.editor.first_baseline()
        want_baseline = core_annots.FREETEXT_BASELINE * self._shown.fontsize * ppp
        x = top_left.x() - base.x()
        y = top_left.y() + want_baseline - base.y()
        ideal = self.editor.document().idealWidth() if self.editor.toPlainText() else 0.0
        w = max(box.width * ppp, ideal) + 3
        h = max(box.height * ppp, self.editor.document().size().height()) + 3
        self.editor.setGeometry(round(x), round(y), round(w), round(h))
        self.frame.setRect(QRectF(box.x0 - FRAME_PAD, box.y0 - FRAME_PAD,
                                  box.width + 2 * FRAME_PAD, box.height + 2 * FRAME_PAD))
        self.keys.adjustSize()
        below = view.mapFromScene(view.page_to_scene(self.page, QPointF(box.x0 - FRAME_PAD,
                                                                        box.y1 + FRAME_PAD)))
        self.keys.move(round(below.x()), round(below.y() + 6))

    # -- ending -----------------------------------------------------------------
    def _teardown(self) -> None:
        if self.closed:
            return
        self.closed = True
        view = self.ctx.view
        for sig, slot in ((view.zoomChanged, self._apply_style),
                          (view.horizontalScrollBar().valueChanged, self._reposition),
                          (view.verticalScrollBar().valueChanged, self._reposition)):
            try:
                sig.disconnect(slot)
            except (RuntimeError, TypeError):
                pass
        self.hider.clear()
        try:
            view.remove_overlay(self.frame)
        except RuntimeError:
            pass
        for w in (self.editor, self.keys):
            w.hide()
            w.deleteLater()
        if view.isVisible():
            view.setFocus()

    def cancel(self) -> None:
        if self.closed:
            return
        self._teardown()
        self.finished.emit()

    def commit(self) -> int | None:
        """Write the text (new box, or changes to the existing one). Returns
        the annotation's xref (``None`` if nothing was written / it was emptied)."""
        if self.closed or self.committing:
            return None
        text = core_annots._clean_text(self.editor.toPlainText()).rstrip()
        self._shown = self.style  # a preview is never saved
        box = self._box(text)
        self._teardown()
        pdf, xref = self.ctx.pdf, self.xref
        self.committing = True
        try:
            if xref is None:
                if text.strip():
                    xref = core_annots.add_free_text(pdf, self.page, box, text, self.style.color,
                                                     self.style.fontsize, font=self.style.font,
                                                     opacity=self.style.opacity)
            elif not text.strip():
                core_annots.delete_annotation(pdf, self.page, xref)
                xref = None
            else:
                props = self._changed_props(text, box)
                if props:
                    core_annots.update_annotation(pdf, self.page, xref, **props)
        except Exception as exc:  # noqa: BLE001 – never lose the user's text silently
            self.ctx.toast(tr("annot.freetext.error", err=str(exc)), "error")
            xref = None
        finally:
            self.committing = False
        self.finished.emit()
        return xref

    def _changed_props(self, text: str, box: pymupdf.Rect) -> dict:
        old, new = self.original_style, self.style
        props: dict = {}
        if text != self.original_text:
            props["contents"] = text
        if new.fontsize != old.fontsize:
            props["fontsize"] = new.fontsize
        if new.font != old.font:
            props["font"] = new.font
        if tuple(new.color) != tuple(old.color):
            props["color"] = tuple(new.color)
        if abs(new.opacity - old.opacity) > 1e-3:
            props["opacity"] = new.opacity
        if props:
            props["rect"] = (box.x0, box.y0, box.x1, box.y1)
        return props


# ---------------------------------------------------------------------------
# the tool
# ---------------------------------------------------------------------------
@ToolRegistry.register
class FreeTextTool(StyledTool):
    id = "annotate.freetext"
    label = tr("annot.freetext")
    hint = tr("annot.freetext.hint")
    icon = "text-aa"   # like "Tilføj tekst" (also T): adds text; T-icon = edit existing text
    shortcut = "T"
    order = 45
    cursor = Qt.CursorShape.IBeamCursor
    default_style = AnnotStyle(PRESETS[5], 1.0, 0.0, None, 14.0, "sans")
    panel_width = False
    panel_text = True
    sample_kind = "freetext"

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.session: FreeTextSession | None = None
        self._press: tuple[int, QPointF] | None = None
        self._drag_item: QGraphicsRectItem | None = None

    # -- style --------------------------------------------------------------------
    def _style_changed(self, style: AnnotStyle) -> None:
        super()._style_changed(style)
        if self.session is not None:
            self.session.set_style(style)

    def preview_style(self, props: dict | None) -> None:
        """Hovered swatch / dragged slider: shown on the text being typed."""
        if self.session is not None:
            self.session.preview(props)

    # -- placing ------------------------------------------------------------------
    def mouse_move(self, ev: ToolMouseEvent) -> None:
        if self._press is not None and self._drag_item is not None:
            page, start = self._press
            self._drag_item.setRect(QRectF(start, ev.pos_on(page)).normalized())
            return
        self.ctx.view.set_tool_cursor(Qt.CursorShape.IBeamCursor if ev.page >= 0 else Qt.CursorShape.ArrowCursor)

    def mouse_press(self, ev: ToolMouseEvent) -> None:
        if ev.button != Qt.MouseButton.LeftButton:
            return
        if self.session is not None:
            self.session.commit()  # a click outside finishes the current text
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
        r = QRectF(start, ev.pos_on(page)).normalized()
        size = self.style.fontsize
        pw, ph = (self.ctx.pdf.page(page).rect.width, self.ctx.pdf.page(page).rect.height)
        if r.width() >= MIN_BOX:  # drag: a box of that width, the text wraps
            left, top = max(0.0, r.left()), max(0.0, r.top())
            self.start(page, QPointF(left, top), width=min(r.width(), pw - left),
                       min_height=r.height() if r.height() >= size else 0.0)
        else:  # click: the first line is centred on the click point
            line = core_annots.FREETEXT_LINE_HEIGHT * size
            x = min(max(0.0, start.x()), max(0.0, pw - 4 * size))
            y = min(max(0.0, start.y() - line / 2), max(0.0, ph - line))
            self.start(page, QPointF(x, y))

    def start(self, page: int, origin: QPointF, *, width: float | None = None, min_height: float = 0.0) -> None:
        self.end_session()
        self.session = FreeTextSession(self, page, origin, self.style, width=width, min_height=min_height)
        self.session.finished.connect(self._session_finished)

    def _session_finished(self) -> None:
        self.session = None

    def end_session(self) -> None:
        if self.session is not None:
            self.session.commit()
        self.session = None

    # -- lifecycle ----------------------------------------------------------------
    def key_press(self, ev: QKeyEvent) -> bool:
        if ev.key() == Qt.Key.Key_Escape and self.session is not None:
            self.session.commit()
            return True
        return False

    def document_changed(self, event) -> None:
        # undo/redo or page changes while typing: the page may be gone
        if self.session is not None and not self.session.committing and event.kind in (RELOADED, STRUCTURE):
            self.session.cancel()

    def deactivate(self) -> None:
        self.end_session()
        if self._drag_item is not None and self.ctx is not None:
            self.ctx.view.remove_overlay(self._drag_item)
        self._drag_item = None
        self._press = None
        super().deactivate()


__all__ = ["FreeTextEditor", "FreeTextSession", "FreeTextTool", "is_auto_width", "screen_font", "style_of"]
