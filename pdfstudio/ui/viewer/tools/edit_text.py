""""Rediger tekst" – edit the PDF's text as in Word.

One click puts the caret where you click: the whole text surface opens – a
page's column of body text (all its paragraphs, headings and list items as
one text) or a standalone text (header, footer, table cell, caption, a
rotated label). The editor is transparent and lies exactly on the text, in
the document's own fonts, sizes, colours, alignment and line pitch; behind it
the page is shown without the text being edited (``core.flow.render_without``),
so images and colour stay and no old text shows through. Arrow keys, Enter
(new paragraph), Backspace (joins paragraphs), selection and formatting work
across paragraphs; an arrow past the end goes on in the next column / page,
Tab in the next text surface. Esc or a click elsewhere keeps the changes:
:func:`pdfstudio.core.flow.apply_story` writes them and lets the column flow
on over the following pages – one undo step per editing session.

Extension points: ``InlineTextEditor`` (the editor widget),
``EditTextTool.start_story`` / ``commit`` / ``_write_story`` (session flow),
``_look_for`` (how a paragraph appears) – "Tilføj tekst" builds on them.
"""

from __future__ import annotations

import math
from collections import Counter
from dataclasses import dataclass, replace

import pymupdf
from PySide6.QtCore import QEvent, QPointF, QRectF, QSize, Qt, QTimer, Signal
from PySide6.QtGui import (
    QColor,
    QFont,
    QFontMetricsF,
    QKeyEvent,
    QMouseEvent,
    QPainter,
    QPainterPath,
    QPen,
    QTextBlockFormat,
    QTextCharFormat,
    QTextCursor,
    QTextDocumentFragment,
    QTextFormat,
    QTransform,
)
from PySide6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QComboBox,
    QDoubleSpinBox,
    QFormLayout,
    QFrame,
    QGraphicsPathItem,
    QGraphicsPixmapItem,
    QGraphicsProxyWidget,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QPushButton,
    QStyle,
    QStyledItemDelegate,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from ....core import RELOADED, STRUCTURE
from ....core import flow
from ....core import fonts as core_fonts
from ....core import text_edit as te
from ....i18n import tr
from ...theme import tokens as T
from ...theme.icons import icon
from ...widgets.color_button import ColorButton, to_qcolor
from ...widgets.util import IconButton, ThemedIconLabel, add_shadow, label
from .base import Tool, ToolMouseEvent, ToolRegistry

HOVER_PAD = 2.0      # pt around a hovered paragraph
EDIT_PAD = 3.0       # pt around the paragraph being edited
KEYS_GAP = 6         # px between the edit frame and the key-hint chip
RECENT_FONTS_KEY = "edit/recentFonts"
RECENT_FONTS_MAX = 5
SHOW_BOXES_KEY = "edit/showAllTextBoxes"

# Character formatting the user applied in the editor is stored as custom
# properties on the characters (Qt keeps them through typing, copy and
# undo); commit turns them into core ``formats``. Unformatted characters keep
# the style they inherit from the original text.
_P = int(QTextFormat.Property.UserProperty)
FORMAT_PROPS = {"bold": _P + 1, "italic": _P + 2, "underline": _P + 3, "strike": _P + 4,
                "color": _P + 5, "size": _P + 6, "font": _P + 7}
#: Line spacing presets (× "single" = 1.2 × the text size).
LINE_SPACINGS = (1.0, 1.15, 1.5, 2.0)
SINGLE = 1.2


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------
def _settings():
    from ...app import settings

    return settings()


def _unit(block: te.TextBlock) -> tuple[tuple[float, float], tuple[float, float]]:
    """(u, v): the block's text direction on screen and the direction from
    one line to the next (display space, y down)."""
    a = math.radians(block.angle)
    u = (math.cos(a), math.sin(a))
    return u, (-u[1], u[0])


def _line_pitch(block: te.TextBlock) -> float:
    """Distance between consecutive baselines (pt) – default 1.2 × size."""
    _u, v = _unit(block)
    ys = [ln.origin[0] * v[0] + ln.origin[1] * v[1] for ln in block.lines]
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


def display_font_name(name: str) -> str:
    """Human name for a PDF font: "ABCDEF+NimbusSans-Regular" -> "Nimbus Sans",
    "Times-Roman" -> "Times", "ArialMT" -> "Arial", "TimesNewRomanPS-BoldMT" -> "Times New Roman Bold"."""
    import re

    n = core_fonts.clean_font_name(name) or name
    n = re.sub(r"(PSMT|PS|MT)$", "", n)
    fam, _, style = n.partition("-")
    fam = re.sub(r"(PSMT|PS|MT)$", "", fam)
    fam = re.sub(r"(?<=[a-z])(?=[A-Z])", " ", fam).strip()
    style = re.sub(r"(PSMT|PS|MT)$", "", style)
    if style.lower() in ("", "regular", "roman", "book", "normal"):
        return fam or name
    return f"{fam} {re.sub(r'(?<=[a-z])(?=[A-Z])', ' ', style)}"


def _style_key(style: core_fonts.SpanStyle) -> tuple:
    return (style.font, round(style.size, 2), tuple(round(c, 3) for c in style.color), style.bold, style.italic)


def recent_fonts() -> list[str]:
    """Font families the user picked lately (most recent first; local QSettings)."""
    try:
        val = _settings().value(RECENT_FONTS_KEY, [])
    except Exception:
        return []
    if isinstance(val, str):
        val = [val] if val else []
    return [str(v) for v in (val or []) if v][:RECENT_FONTS_MAX]


def remember_font(family: str) -> None:
    if not family:
        return
    fonts = [f for f in recent_fonts() if f.casefold() != family.casefold()]
    fonts.insert(0, family)
    try:
        _settings().setValue(RECENT_FONTS_KEY, fonts[:RECENT_FONTS_MAX])
    except Exception:
        pass


def _preview_font(family: str, px: int = 14) -> QFont:
    f = QFont(core_fonts.qt_family_hint(core_fonts.SpanStyle(family or "", 12)))
    f.setPixelSize(px)
    return f


# ---------------------------------------------------------------------------
# inline editor
# ---------------------------------------------------------------------------
class InlineTextEditor(QTextEdit):
    """Frameless editor laid over a paragraph; geometry in viewport pixels
    (or, for rotated text, in its own pixels inside a rotated scene proxy)."""

    commitRequested = Signal()
    cancelRequested = Signal()
    nextRequested = Signal()
    previousRequested = Signal()
    formatRequested = Signal(str)   # Ctrl+B / Ctrl+I / Ctrl+U: "bold" | "italic" | "underline"
    leaveRequested = Signal(int)    # an arrow key past the start (-1) / end (+1) of the text

    _clip: tuple[str, QTextDocumentFragment] | None = None  # last copy made in an editor (rich)

    def __init__(self, viewport: QWidget | None) -> None:
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
        self.escape_commits = False   # Esc finishes (keeps the text) instead of cancelling
        self.bullets: dict = {}       # paragraph tag -> (text, QFont, QColor, x px): list bullets to draw
        self.bullet_tag = None        # block property holding a paragraph's tag
        self.textChanged.connect(self._apply_line_height)

    def configure(self, font: QFont, color: QColor, background: QColor, line_px: float, wrap_px: int) -> None:
        sel = T.qcolor("accent")
        sel.setAlpha(70)
        # style sheet first: (re)polishing resolves the font again
        self.setStyleSheet(
            "QTextEdit#InlineTextEditor { border: none; padding: 0px;"
            f" background: rgba({background.red()},{background.green()},{background.blue()},{background.alpha()});"
            f" color: {color.name()};"
            f" selection-background-color: rgba({sel.red()},{sel.green()},{sel.blue()},{sel.alpha()});"
            f" selection-color: {color.name()}; }}")
        self.ensurePolished()
        self.setFont(font)
        self.document().setDefaultFont(font)
        self._line_px = line_px
        self.setLineWrapColumnOrWidth(max(8, wrap_px))
        self._apply_line_height()

    def _apply_line_height(self) -> None:
        if self._line_px <= 0:
            return
        doc = self.document()
        fixed = QTextBlockFormat.LineHeightTypes.FixedHeight.value
        block = doc.begin()
        while block.isValid():  # only touch the format when a paragraph lacks it:
            bf = block.blockFormat()  # every change is an undo step of its own
            if bf.lineHeightType() != fixed or abs(bf.lineHeight() - self._line_px) > 0.01:
                break
            block = block.next()
        else:
            return
        cur = QTextCursor(doc)
        cur.select(QTextCursor.SelectionType.Document)
        fmt = QTextBlockFormat()
        fmt.setLineHeight(self._line_px, fixed)
        self.blockSignals(True)
        cur.joinPreviousEditBlock()   # one Ctrl+Z undoes the typing and this together
        cur.mergeBlockFormat(fmt)
        cur.endEditBlock()
        self.blockSignals(False)

    def set_first_line_indent(self, px: float) -> None:
        """Indent of the first line only (a paragraph's first-line indent)."""
        block = self.document().begin()
        fmt = block.blockFormat()
        if abs(fmt.textIndent() - px) < 0.01:
            return
        fmt.setTextIndent(px)
        self.blockSignals(True)
        QTextCursor(block).setBlockFormat(fmt)
        self.blockSignals(False)

    def set_hanging_indent(self, px: float) -> None:
        """Lines after the first start ``px`` further right (a list item's
        text after "1.", a hanging indent)."""
        block = self.document().begin()
        fmt = block.blockFormat()
        if abs(fmt.leftMargin() - px) < 0.01 and abs(fmt.textIndent() + px) < 0.01:
            return
        fmt.setLeftMargin(px)
        fmt.setTextIndent(-px)
        self.blockSignals(True)
        QTextCursor(block).setBlockFormat(fmt)
        self.blockSignals(False)

    def set_alignment(self, align: Qt.AlignmentFlag) -> None:
        opt = self.document().defaultTextOption()
        opt.setAlignment(align)
        self.document().setDefaultTextOption(opt)

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

    # -- clipboard: rich inside Folio, plain text from elsewhere ------------
    def createMimeDataFromSelection(self):  # noqa: N802 - Qt override
        mime = super().createMimeDataFromSelection()
        cur = self.textCursor()
        if cur.hasSelection():
            InlineTextEditor._clip = (cur.selection().toPlainText(), cur.selection())
        return mime

    def canInsertFromMimeData(self, source) -> bool:  # noqa: N802 - Qt override
        return source.hasText() or super().canInsertFromMimeData(source)

    def insertFromMimeData(self, source) -> None:  # noqa: N802 - Qt override
        """Text from other programs comes in as plain text in the format at
        the cursor; text copied inside an editor keeps its formatting."""
        text = source.text() if source.hasText() else ""
        clip = InlineTextEditor._clip
        cur = self.textCursor()
        if clip is not None and text and text == clip[0]:
            cur.insertFragment(clip[1])
        elif text:
            cur.insertText(text.replace("\r\n", "\n").replace("\r", "\n").replace("\t", "    "),
                           self.currentCharFormat())
        self.setTextCursor(cur)

    def keyPressEvent(self, ev: QKeyEvent) -> None:  # noqa: N802
        key, mods = ev.key(), ev.modifiers()
        if mods & Qt.KeyboardModifier.ControlModifier and not mods & Qt.KeyboardModifier.ShiftModifier \
                and key in (Qt.Key.Key_B, Qt.Key.Key_I, Qt.Key.Key_U):
            ev.accept()
            self.formatRequested.emit({Qt.Key.Key_B: "bold", Qt.Key.Key_I: "italic", Qt.Key.Key_U: "underline"}[key])
            return
        if key == Qt.Key.Key_Escape:
            ev.accept()
            (self.commitRequested if self.escape_commits else self.cancelRequested).emit()
            return
        if key in (Qt.Key.Key_Return, Qt.Key.Key_Enter) and mods & Qt.KeyboardModifier.ControlModifier:
            ev.accept()
            self.commitRequested.emit()
            return
        if key == Qt.Key.Key_Tab and not mods & Qt.KeyboardModifier.ShiftModifier:
            ev.accept()
            self.nextRequested.emit()
            return
        if key == Qt.Key.Key_Backtab or (key == Qt.Key.Key_Tab and mods & Qt.KeyboardModifier.ShiftModifier):
            ev.accept()
            self.previousRequested.emit()
            return
        moves = {Qt.Key.Key_Down: 1, Qt.Key.Key_Right: 1, Qt.Key.Key_PageDown: 1,
                 Qt.Key.Key_Up: -1, Qt.Key.Key_Left: -1, Qt.Key.Key_PageUp: -1}
        step = moves.get(key) if not mods & (Qt.KeyboardModifier.ShiftModifier | Qt.KeyboardModifier.ControlModifier) \
            else None
        before = self.textCursor().position()
        had_selection = self.textCursor().hasSelection()
        super().keyPressEvent(ev)
        if step is not None and not had_selection and self.textCursor().position() == before:
            # the caret could not move: on into the next / previous text
            doc_end = self.document().characterCount() - 1
            if (step > 0 and before >= doc_end) or (step < 0 and before == 0) or key in (
                    Qt.Key.Key_Down, Qt.Key.Key_Up, Qt.Key.Key_PageDown, Qt.Key.Key_PageUp):
                self.leaveRequested.emit(step)

    def paintEvent(self, ev) -> None:  # noqa: N802 - Qt override
        super().paintEvent(ev)
        if not self.bullets or self.bullet_tag is None:
            return
        p = QPainter(self.viewport())
        try:
            layout = self.document().documentLayout()
            block = self.document().begin()
            while block.isValid():
                tag = block.blockFormat().property(self.bullet_tag)
                info = self.bullets.get(tag) if tag is not None else None
                if info is not None and block.layout() is not None and block.layout().lineCount():
                    text, font, color, x = info
                    line = block.layout().lineAt(0)
                    layout.blockBoundingRect(block)        # laid out
                    y = block.layout().position().y() + line.y() + line.ascent() - self.verticalScrollBar().value()
                    p.setFont(font)
                    p.setPen(color)
                    p.drawText(QPointF(x, y), text)
                block = block.next()
        finally:
            p.end()

    def event(self, ev) -> bool:
        # keep Tab for us instead of focus navigation
        if ev.type() == QEvent.Type.KeyPress and ev.key() in (Qt.Key.Key_Tab, Qt.Key.Key_Backtab):
            self.keyPressEvent(ev)
            return True
        if ev.type() == QEvent.Type.ShortcutOverride:
            # editing keys belong to the editor, not to window shortcuts
            # (Ctrl+Z undoes typing here; the document undo comes after commit)
            mods = ev.modifiers()
            if mods & Qt.KeyboardModifier.ControlModifier and ev.key() in (
                    Qt.Key.Key_Z, Qt.Key.Key_Y, Qt.Key.Key_B, Qt.Key.Key_I, Qt.Key.Key_U, Qt.Key.Key_A,
                    Qt.Key.Key_C, Qt.Key.Key_X, Qt.Key.Key_V):
                ev.accept()
                return True
        return super().event(ev)


# ---------------------------------------------------------------------------
# font picker
# ---------------------------------------------------------------------------
_KIND_ROLE = Qt.ItemDataRole.UserRole + 1   # "header" | "font"
_VALUE_ROLE = Qt.ItemDataRole.UserRole      # font name passed to the core
_CAPTION_ROLE = Qt.ItemDataRole.UserRole + 2


class _FontItemDelegate(QStyledItemDelegate):
    """Section captions and font rows; each family is drawn in itself."""

    def __init__(self, popup: "FontPopup") -> None:
        super().__init__(popup)
        self.popup = popup

    def sizeHint(self, option, index) -> QSize:  # noqa: N802
        return QSize(200, 28 if index.data(_KIND_ROLE) == "header" else 32)

    def paint(self, p: QPainter, option, index) -> None:
        p.save()
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        r = QRectF(option.rect)
        if index.data(_KIND_ROLE) == "header":
            f = QFont(option.font)
            f.setPixelSize(T.FONT_SIZE_XS)
            f.setWeight(QFont.Weight.DemiBold)
            p.setFont(f)
            p.setPen(T.qcolor("text_subtle"))
            p.drawText(r.adjusted(10, 6, -10, 0), Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
                       index.data(Qt.ItemDataRole.DisplayRole))
            p.restore()
            return
        pill = r.adjusted(4, 1, -4, -1)
        selected = bool(option.state & QStyle.StateFlag.State_Selected)
        hover = bool(option.state & QStyle.StateFlag.State_MouseOver)
        if selected or hover:
            path = QPainterPath()
            path.addRoundedRect(pill, 7, 7)
            p.fillPath(path, T.qcolor("selected" if selected else "hover"))
        value = index.data(_VALUE_ROLE)
        current = value == self.popup.current
        right = pill.right() - 10
        if current:
            ic = icon("check", "accent_text")
            ic.paint(p, int(right - 14), int(pill.center().y() - 7), 14, 14)
            right -= 22
        caption = index.data(_CAPTION_ROLE)
        if caption:
            small = QFont(option.font)
            small.setPixelSize(T.FONT_SIZE_XS)
            p.setFont(small)
            p.setPen(T.qcolor("text_subtle"))
            cw = QFontMetricsF(small).horizontalAdvance(caption)
            p.drawText(QRectF(right - cw, pill.top(), cw, pill.height()), Qt.AlignmentFlag.AlignVCenter, caption)
            right -= cw + 10
        f = index.data(Qt.ItemDataRole.FontRole) or QFont(option.font)
        p.setFont(f)
        p.setPen(T.qcolor("accent_text" if current else "text"))
        text_rect = QRectF(pill.left() + 10, pill.top(), max(10.0, right - pill.left() - 10), pill.height())
        text = QFontMetricsF(f).elidedText(index.data(Qt.ItemDataRole.DisplayRole), Qt.TextElideMode.ElideRight,
                                           text_rect.width())
        p.drawText(text_rect, Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter, text)
        p.restore()


class FontPopup(QFrame):
    """Searchable list of font families: the document's own font, recently
    used fonts and all fonts (base-14 + installed). Type to filter, arrow keys
    to move, Enter to pick, Esc to close."""

    chosen = Signal(str)

    def __init__(self, parent: QWidget, document: list[tuple[str, str]], recent: list[str],
                 families: list[str], current: str) -> None:
        super().__init__(parent, Qt.WindowType.Popup | Qt.WindowType.FramelessWindowHint
                         | Qt.WindowType.NoDropShadowWindowHint)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose, True)
        self.current = current
        outer = QVBoxLayout(self)
        outer.setContentsMargins(10, 6, 10, 14)   # room for the soft shadow
        card = QFrame()
        card.setObjectName("Popover")
        add_shadow(card, blur=24, y=6)
        outer.addWidget(card)
        v = QVBoxLayout(card)
        v.setContentsMargins(6, 6, 6, 6)
        v.setSpacing(4)
        search_row = QWidget()
        sr = QHBoxLayout(search_row)
        sr.setContentsMargins(8, 0, 4, 0)
        sr.setSpacing(4)
        sr.addWidget(ThemedIconLabel("magnifying-glass", 16, "text_subtle"))
        self.search = QLineEdit()
        self.search.setObjectName("FontSearch")
        self.search.setProperty("variant", "bare")
        self.search.setPlaceholderText(tr("edit.text.font.search"))
        self.search.setClearButtonEnabled(True)
        sr.addWidget(self.search, 1)
        v.addWidget(search_row)
        sep = QFrame()
        sep.setProperty("divider", "h")
        v.addWidget(sep)
        self.list = QListWidget()
        self.list.setObjectName("FontList")
        self.list.setItemDelegate(_FontItemDelegate(self))
        self.list.setMouseTracking(True)
        self.list.setUniformItemSizes(False)
        self.list.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.list.setVerticalScrollMode(QAbstractItemView.ScrollMode.ScrollPerPixel)
        self.list.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        v.addWidget(self.list, 1)
        self.empty = label("", "caption", wrap=True)
        self.empty.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.empty.setContentsMargins(8, 16, 8, 16)
        self.empty.hide()
        v.addWidget(self.empty)

        self._sections: list[tuple[QListWidgetItem, list[QListWidgetItem]]] = []
        self._add_section(tr("edit.text.font.document"),
                          [(text, value, tr("edit.text.font.original_tag")) for text, value in document])
        known = {f.casefold() for f in families}
        shown_recent = [f for f in recent if f.casefold() in known]
        if shown_recent:
            self._add_section(tr("edit.text.font.recent"), [(f, f, "") for f in shown_recent])
        self._add_section(tr("edit.text.font.all"), [(f, f, "") for f in families])

        self.search.textChanged.connect(self._filter)
        self.search.installEventFilter(self)
        self.list.itemClicked.connect(self._clicked)
        self._select_current()
        self.setFocusProxy(self.search)

    # -- building -----------------------------------------------------------
    def _add_section(self, title: str, rows: list[tuple[str, str, str]]) -> None:
        head = QListWidgetItem(title)
        head.setData(_KIND_ROLE, "header")
        head.setFlags(Qt.ItemFlag.ItemIsEnabled)
        self.list.addItem(head)
        items = []
        for text, value, caption in rows:
            it = QListWidgetItem(text)
            it.setData(_KIND_ROLE, "font")
            it.setData(_VALUE_ROLE, value)
            it.setData(_CAPTION_ROLE, caption)
            it.setData(Qt.ItemDataRole.FontRole, _preview_font(value if caption == "" else text))
            it.setToolTip(text)
            self.list.addItem(it)
            items.append(it)
        self._sections.append((head, items))

    def _font_rows(self) -> list[int]:
        return [i for i in range(self.list.count())
                if self.list.item(i).data(_KIND_ROLE) == "font" and not self.list.isRowHidden(i)]

    def _select_current(self) -> None:
        for i in self._font_rows():
            if self.list.item(i).data(_VALUE_ROLE) == self.current:
                self.list.setCurrentRow(i)
                self.list.scrollToItem(self.list.item(i), QAbstractItemView.ScrollHint.PositionAtCenter)
                return
        rows = self._font_rows()
        if rows:
            self.list.setCurrentRow(rows[0])

    # -- filtering / navigation ---------------------------------------------
    def _filter(self, text: str) -> None:
        q = text.strip().casefold()
        squash = q.replace(" ", "")
        any_visible = False
        for head, items in self._sections:
            visible = 0
            for it in items:
                name = it.text().casefold()
                hit = not q or q in name or squash in name.replace(" ", "")
                self.list.setRowHidden(self.list.row(it), not hit)
                visible += hit
            self.list.setRowHidden(self.list.row(head), visible == 0)
            any_visible |= visible > 0
        self.empty.setText(tr("edit.text.font.none", query=text.strip()))
        self.empty.setVisible(not any_visible)
        self.list.setVisible(any_visible)
        rows = self._font_rows()
        if rows:
            self.list.setCurrentRow(rows[0])
            self.list.scrollToItem(self.list.item(rows[0]))

    def _move(self, step: int) -> None:
        rows = self._font_rows()
        if not rows:
            return
        cur = self.list.currentRow()
        pos = rows.index(cur) if cur in rows else -1
        nxt = rows[max(0, min(len(rows) - 1, pos + step))]
        self.list.setCurrentRow(nxt)
        self.list.scrollToItem(self.list.item(nxt))

    def eventFilter(self, obj, ev) -> bool:  # noqa: N802
        if obj is self.search and ev.type() == QEvent.Type.KeyPress:
            key = ev.key()
            if key in (Qt.Key.Key_Down, Qt.Key.Key_Up):
                self._move(1 if key == Qt.Key.Key_Down else -1)
                return True
            if key in (Qt.Key.Key_PageDown, Qt.Key.Key_PageUp):
                self._move(8 if key == Qt.Key.Key_PageDown else -8)
                return True
            if key in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
                it = self.list.currentItem()
                if it is not None and it.data(_KIND_ROLE) == "font" and self.list.isVisible():
                    self._clicked(it)
                return True
            if key == Qt.Key.Key_Escape:
                self.close()
                return True
        return super().eventFilter(obj, ev)

    def _clicked(self, item: QListWidgetItem) -> None:
        if item.data(_KIND_ROLE) != "font":
            return
        value = item.data(_VALUE_ROLE)
        self.close()
        self.chosen.emit(value)

    def choose(self, value: str) -> None:
        """Pick ``value`` as if the user clicked it (tests, automation)."""
        for i in range(self.list.count()):
            it = self.list.item(i)
            if it.data(_KIND_ROLE) == "font" and it.data(_VALUE_ROLE) == value:
                self._clicked(it)
                return


class FontComboBox(QComboBox):
    """A combo box that looks like every other field but opens a searchable
    :class:`FontPopup` with "I dokumentet", "Senest brugt" and all fonts.
    Item 0 is the paragraph's own (document) font; the rest are families."""

    popupShown = Signal(object)  # the FontPopup

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.popup: FontPopup | None = None
        self._families: list[str] = []

    def set_fonts(self, original_name: str, original_value: str, families: list[str]) -> None:
        """Item 0 = the document font; the family list is only rebuilt when it changed."""
        self.blockSignals(True)
        try:
            if families != self._families:
                self.clear()
                self.addItem(original_name, original_value)
                for fam in families:
                    self.addItem(fam, fam)
                    self.setItemData(self.count() - 1, _preview_font(fam, 13), Qt.ItemDataRole.FontRole)
                self._families = list(families)
            else:
                self.setItemText(0, original_name)
                self.setItemData(0, original_value)
            self.setItemData(0, _preview_font(original_value, 13), Qt.ItemDataRole.FontRole)
            self.setItemData(0, tr("edit.text.font.original", name=original_name), Qt.ItemDataRole.ToolTipRole)
        finally:
            self.blockSignals(False)

    def showPopup(self) -> None:  # noqa: N802 - Qt override
        if self.count() == 0:
            return
        # parented to the window, not the combo: combo descendants get combo styles
        popup = FontPopup(self.window(), [(self.itemText(0), self.itemData(0))], recent_fonts(),
                          self._families, self.currentData())
        popup.chosen.connect(self._choose)
        popup.destroyed.connect(self._popup_gone)
        self.popup = popup
        width = max(self.width() + 20, 300)
        popup.resize(width, 400)
        below = self.mapToGlobal(self.rect().bottomLeft())
        x, y = below.x() - 10, below.y() - 2
        screen = self.screen().availableGeometry() if self.screen() is not None else None
        if screen is not None:
            if y + popup.height() > screen.bottom():
                y = self.mapToGlobal(self.rect().topLeft()).y() - popup.height() + 8
            x = max(screen.left(), min(x, screen.right() - width))
        popup.move(x, y)
        popup.show()
        popup.search.setFocus(Qt.FocusReason.PopupFocusReason)
        self.popupShown.emit(popup)

    def wheelEvent(self, ev) -> None:  # noqa: N802 - Qt override
        ev.ignore()  # scrolling the inspector must not change the font

    def hidePopup(self) -> None:  # noqa: N802 - Qt override
        if self.popup is not None:
            self.popup.close()
        super().hidePopup()

    def _popup_gone(self, *_a) -> None:
        self.popup = None

    def _choose(self, value: str) -> None:
        idx = self.findData(value)
        if idx < 0:
            return
        self.setCurrentIndex(idx)
        self.activated.emit(idx)


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
        self.font = FontComboBox()
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
        self.bold = IconButton("text-bolder", tr("edit.text.bold"), variant="icon", checkable=True, shortcut="Ctrl+B")
        self.italic = IconButton("text-italic", tr("edit.text.italic"), variant="icon", checkable=True,
                                 shortcut="Ctrl+I")
        self.underline = IconButton("text-underline", tr("edit.text.underline"), variant="icon", checkable=True,
                                    shortcut="Ctrl+U")
        self.strike = IconButton("text-strikethrough", tr("edit.text.strike"), variant="icon", checkable=True)
        for b, key in ((self.bold, "bold"), (self.italic, "italic"), (self.underline, "underline"),
                       (self.strike, "strike")):
            b.setAccessibleName(f"format.{key}")
            b.setFocusPolicy(Qt.FocusPolicy.NoFocus)   # keep the editor's selection
            b.clicked.connect(lambda _c=False, k=key: self._toggle(k))
        row.addWidget(self.color)
        row.addSpacing(4)
        for b in (self.bold, self.italic, self.underline, self.strike):
            row.addWidget(b)
        row.addStretch(1)
        form.addRow(tr("edit.text.color"), row)
        fv.addLayout(form)
        self.scope = label("", "caption", wrap=True)
        fv.addWidget(self.scope)
        self.reset = QPushButton(tr("edit.text.reset"))
        self.reset.setProperty("variant", "ghost")
        self.reset.setCursor(Qt.CursorShape.PointingHandCursor)
        self.reset.clicked.connect(self.tool.reset_style)
        fv.addWidget(self.reset, 0, Qt.AlignmentFlag.AlignLeft)
        self.rotated_note = label(tr("edit.text.rotated"), "caption", wrap=True)
        fv.addWidget(self.rotated_note)
        v.addWidget(self.format_box)

        # alignment + line spacing of an existing paragraph ("Redigér tekst")
        self.para_box = QWidget()
        pv = QVBoxLayout(self.para_box)
        pv.setContentsMargins(0, 6, 0, 0)
        pv.setSpacing(8)
        pv.addWidget(label(tr("edit.text.section.paragraph"), "section"))
        pr = QHBoxLayout()
        pr.setSpacing(4)
        self.para_align: dict[str, IconButton] = {}
        for key, ic in (("left", "text-align-left"), ("center", "text-align-center"),
                        ("right", "text-align-right"), ("justify", "text-align-justify")):
            b = IconButton(ic, tr(f"edit.text.align.{key}"), variant="icon", checkable=True)
            b.setAccessibleName(f"paragraph.{key}")
            b.setFocusPolicy(Qt.FocusPolicy.NoFocus)
            b.clicked.connect(lambda _c=False, k=key: self.tool.set_paragraph(align=k))
            pr.addWidget(b)
            self.para_align[key] = b
        pr.addStretch(1)
        pv.addLayout(pr)
        sform = QFormLayout()
        sform.setSpacing(8)
        self.spacing = QComboBox()
        self.spacing.addItem(tr("edit.text.spacing.original"), None)
        for factor in LINE_SPACINGS:
            self.spacing.addItem(f"{factor:g}".replace(".", ","), factor)
        self.spacing.activated.connect(self._spacing_chosen)
        sform.addRow(tr("edit.text.spacing"), self.spacing)
        pv.addLayout(sform)
        self.para_reset = QPushButton(tr("edit.text.paragraph.reset"))
        self.para_reset.setProperty("variant", "ghost")
        self.para_reset.setCursor(Qt.CursorShape.PointingHandCursor)
        self.para_reset.clicked.connect(lambda: self.tool.set_paragraph(align=None, spacing=None))
        pv.addWidget(self.para_reset, 0, Qt.AlignmentFlag.AlignLeft)
        v.addWidget(self.para_box)

        # alignment of new text ("Tilføj tekst")
        self.align_box = QWidget()
        av = QVBoxLayout(self.align_box)
        av.setContentsMargins(0, 6, 0, 0)
        av.setSpacing(8)
        av.addWidget(label(tr("edit.add_text.section.align"), "section"))
        ar = QHBoxLayout()
        ar.setSpacing(4)
        self.align_buttons: dict[str, IconButton] = {}
        for key, ic in (("left", "text-align-left"), ("center", "text-align-center"), ("right", "text-align-right")):
            b = IconButton(ic, tr(f"edit.add_text.align.{key}"), variant="icon", checkable=True)
            b.setAutoExclusive(True)
            b.clicked.connect(lambda _c=False, k=key: self.tool.set_align(k))
            ar.addWidget(b)
            self.align_buttons[key] = b
        ar.addStretch(1)
        av.addLayout(ar)
        v.addWidget(self.align_box)
        self.align_box.setVisible(bool(getattr(tool, "supports_alignment", False)))

        # "Vis alle tekstbokse" ("Rediger tekst")
        self.view_box = QWidget()
        vv = QVBoxLayout(self.view_box)
        vv.setContentsMargins(0, 6, 0, 0)
        vv.setSpacing(6)
        vv.addWidget(label(tr("edit.text.section.view"), "section"))
        self.show_boxes = QCheckBox(tr("edit.text.show_boxes"))
        self.show_boxes.setCursor(Qt.CursorShape.PointingHandCursor)
        self.show_boxes.setToolTip(tr("edit.text.show_boxes.tip"))
        self.show_boxes.toggled.connect(self.tool.set_show_all_boxes)
        vv.addWidget(self.show_boxes)
        v.addWidget(self.view_box)
        self.view_box.setVisible(bool(getattr(tool, "supports_box_outlines", False)))
        v.addStretch(1)
        self._loading = False
        self.refresh()

    def refresh(self) -> None:
        block, style = self.tool.block, self.tool.current_style()
        editing = block is not None and style is not None
        self.format_box.setVisible(editing)
        self.para_box.setVisible(editing and bool(getattr(self.tool, "supports_paragraph", False))
                                 and not self.tool.rotated())
        self._loading = True
        try:
            self.show_boxes.setChecked(self.tool.show_all_boxes)
            for key, b in self.align_buttons.items():
                b.setChecked(getattr(self.tool, "align", "left") == key)
        finally:
            self._loading = False
        if not editing:
            self.title.setText(tr(self.tool.idle_title_key))
            self.text.setText(tr(self.tool.idle_text_key))
            return
        self.title.setText(display_font_name(block.style.font) or "–")
        self.text.setText(f"{block.style.size:g} pt".replace(".", ","))
        self._loading = True
        try:
            original = display_font_name(block.style.font) or core_fonts.clean_font_name(block.style.font)
            self.font.set_fonts(original, block.style.font, core_fonts.available_families())
            idx = self.font.findData(style.font)
            self.font.setCurrentIndex(max(0, idx))
            self.size.setValue(style.size)
            self.color.set_color(tuple(style.color))
            self.bold.setChecked(style.bold)
            self.italic.setChecked(style.italic)
            self.underline.setChecked(style.underline)
            self.strike.setChecked(style.strike)
            self.reset.setEnabled(self.tool.has_user_format())
            self.rotated_note.setVisible(self.tool.rotated())
            lay = self.tool._layout
            current = self.tool.align_override or (
                "justify" if lay is not None and lay.justified else (lay.align if lay is not None else "left"))
            for key, b in self.para_align.items():
                b.setChecked(key == current)
            idx = self.spacing.findData(self.tool.spacing_override) if self.tool.spacing_override else 0
            self.spacing.setCurrentIndex(max(0, idx))
            if lay is not None and lay.line_spacing and not self.tool.spacing_override:
                self.spacing.setItemText(0, tr("edit.text.spacing.original_n",
                                               n=f"{lay.line_spacing / SINGLE:.2f}".rstrip("0").rstrip(".").replace(".", ",")))
            else:
                self.spacing.setItemText(0, tr("edit.text.spacing.original"))
            self.para_reset.setEnabled(self.tool.align_override is not None or self.tool.spacing_override is not None)
        finally:
            self._loading = False
        self.refresh_selection()

    def refresh_selection(self) -> None:
        """Show the selection's formatting (or the paragraph's) and say
        which one the controls change."""
        if self.tool.block is None or self.tool.editor is None:
            return
        selected = self.tool.has_text_selection()
        self.scope.setText(tr("edit.text.scope.selection") if selected else tr("edit.text.scope.paragraph"))
        if not selected:
            return  # the toggles show the paragraph's style (refresh)
        state = self.tool.format_state()
        self._loading = True
        try:
            for b, key in ((self.bold, "bold"), (self.italic, "italic"), (self.underline, "underline"),
                           (self.strike, "strike")):
                b.setChecked(state[key])
        finally:
            self._loading = False

    def _toggle(self, key: str) -> None:
        """Bold / italic / underline / strike: the selected text, or – with
        nothing selected – the whole paragraph (Ctrl+B etc. in the editor
        format what is typed next instead, like Word)."""
        if self._loading or self.tool.block is None:
            return
        if self.tool.has_text_selection():
            self.tool.toggle_format(key)
        else:
            self._changed()

    def _spacing_chosen(self, index: int) -> None:
        if self._loading:
            return
        value = self.spacing.itemData(index)
        self.tool.set_paragraph(spacing=float(value) if value else None)

    def _changed(self, *_a) -> None:
        if self._loading or self.tool.block is None:
            return
        if self.tool.has_text_selection():
            # font / size / colour of the selected text only
            sender = self.sender()
            if sender is self.font:
                family = self.font.currentData() or self.tool.block.style.font
                if self.font.currentIndex() > 0:
                    remember_font(family)
                self.tool.apply_char_format({"font": family})
            elif sender is self.size:
                self.tool.apply_char_format({"size": float(self.size.value())})
            elif sender is self.color:
                self.tool.apply_char_format({"color": tuple(self.color.color())})
            return
        base = self.tool.block.style
        family = self.font.currentData() or base.font
        if self.sender() is self.font and self.font.currentIndex() > 0:
            remember_font(family)
        self.tool.set_style(replace(
            base,
            font=family,
            size=float(self.size.value()),
            color=tuple(self.color.color()),
            bold=self.bold.isChecked(),
            italic=self.italic.isChecked(),
            underline=self.underline.isChecked(),
            strike=self.strike.isChecked(),
        ))
        self.reset.setEnabled(self.tool.has_user_format())


# ---------------------------------------------------------------------------
# the tool
# ---------------------------------------------------------------------------
_PIECE = _P + 20      # block property: index of the story piece a paragraph comes from
_ALIGN = _P + 21      # block property: alignment chosen in the inspector
_SPACING = _P + 22    # block property: line spacing chosen in the inspector (× SINGLE)
_STYLE = _P + 23      # block property: the paragraph's own style changes (inspector, no selection)
_GEOM = _P + 24       # block property: [left, right, indent, pitch, top] in pt
_PT = _P + 25         # char property: the text size in pt (pixel sizes follow the zoom)
_LS = " "        # Qt's line separator: a hard line break inside a paragraph


@dataclass
class _Look:
    """How one paragraph appears in the editor: pt, in the story's own frame
    (``s`` along the text from the anchor, ``t`` from line to line)."""

    style: core_fonts.SpanStyle
    pitch: float
    align: str                  # left | center | right | justify
    left: float                 # the paragraph's box along s
    right: float
    indent: float               # the first line starts this much further (negative: hanging)
    first: tuple                # (s, t) of the first baseline origin
    spacing: float = 100.0      # letter spacing (%) that matches the PDF's line widths
    bullet: tuple | None = None  # (text, style, s) of a list bullet


def _local(anchor, u, v, x: float, y: float) -> tuple[float, float]:
    dx, dy = x - anchor[0], y - anchor[1]
    return dx * u[0] + dy * u[1], dx * v[0] + dy * v[1]


@ToolRegistry.register
class EditTextTool(Tool):
    """Edits a whole text surface – a page's column of body text, or a
    standalone text such as a header, a table cell or a rotated label – in
    one editor, as in Word: one click puts the caret where you click, the
    paragraphs are one text, and the text flows (``core.flow``)."""

    id = "edit.text"
    label = tr("edit.text.label")
    hint = tr("edit.text.hint")
    icon = "text-t"
    shortcut = "E"
    mode = "edit"
    order = 10
    cursor = Qt.CursorShape.IBeamCursor
    idle_title_key = "edit.text.idle.title"
    idle_text_key = "edit.text.idle.text"
    supports_box_outlines = True    # "Vis tekstflader"
    supports_alignment = False      # alignment buttons (new text only)
    supports_paragraph = True       # alignment + line spacing of existing paragraphs

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.editor: InlineTextEditor | None = None
        self.story: flow.Story | None = None
        self._proxy: QGraphicsProxyWidget | None = None   # rotated text: the editor inside the scene
        self._proxy_drag = False
        self._bg: QGraphicsPixmapItem | None = None       # the page without the text being edited
        self._bg_timer = QTimer(self)
        self._bg_timer.setSingleShot(True)
        self._bg_timer.setInterval(120)
        self._bg_timer.timeout.connect(self._render_background)
        self._keys: QLabel | None = None
        self._keys_covers_text = False
        self._panel: EditTextPanel | None = None
        self._committing = False
        self._looks: list[_Look] = []
        self._anchor = (0.0, 0.0)
        self._axes = ((1.0, 0.0), (0.0, 1.0))
        self._box = (0.0, 0.0, 0.0)          # (s0, s1, t0): the editor's area in the story frame
        self._ppp = 1.0
        self._session: object | None = None   # undo merge token of the editing session
        self._drag_anchor: int | None = None  # caret where a click-drag started
        self._box_items: dict[int, QGraphicsPathItem] = {}
        self._box_timer = QTimer(self)
        self._box_timer.setSingleShot(True)
        self._box_timer.setInterval(40)
        self._box_timer.timeout.connect(self._refresh_box_outlines)
        self.show_all_boxes = False

    # -- lifecycle ---------------------------------------------------------
    def activate(self, ctx) -> None:
        super().activate(ctx)
        view = ctx.view
        view.zoomChanged.connect(self._on_zoom)
        view.horizontalScrollBar().valueChanged.connect(self._reposition)
        view.verticalScrollBar().valueChanged.connect(self._reposition)
        view.verticalScrollBar().valueChanged.connect(self._schedule_box_outlines)
        view.pagesReset.connect(self._on_pages_reset)
        if self.supports_box_outlines:
            try:
                self.show_all_boxes = str(_settings().value(SHOW_BOXES_KEY, "false")).lower() in ("true", "1")
            except Exception:
                self.show_all_boxes = False
            self._schedule_box_outlines()
        # resolving fonts the first time scans the system fonts – do it early
        QTimer.singleShot(0, core_fonts.available_families)

    def deactivate(self) -> None:
        self.commit()
        self._clear_box_outlines()
        self._box_timer.stop()
        if self.ctx is not None:
            view = self.ctx.view
            for sig, slot in ((view.zoomChanged, self._on_zoom),
                              (view.horizontalScrollBar().valueChanged, self._reposition),
                              (view.verticalScrollBar().valueChanged, self._reposition),
                              (view.verticalScrollBar().valueChanged, self._schedule_box_outlines),
                              (view.pagesReset, self._on_pages_reset)):
                try:
                    sig.disconnect(slot)
                except (RuntimeError, TypeError):
                    pass
        super().deactivate()

    def _on_zoom(self, *_a) -> None:
        """Zooming while editing: font pixel sizes and margins follow."""
        if self.editor is not None:
            self._ppp = self._px_per_pt()
            self._apply_px()
            self._align_first()
            self._reposition()
            self._bg_timer.start()
        self._schedule_box_outlines()

    def _on_pages_reset(self) -> None:
        # the view dropped every overlay
        self._box_items.clear()
        self._bg = None
        if self.editor is not None:
            self._bg_timer.start()
        self._schedule_box_outlines()

    def properties_widget(self) -> QWidget | None:
        if self._panel is None:
            self._panel = EditTextPanel(self)
        self._panel.refresh()
        return self._panel

    def document_changed(self, event) -> None:
        self._clear_box_outlines()
        self._schedule_box_outlines()
        if self.editor is not None and not self._committing and event.kind in (RELOADED, STRUCTURE):
            self.cancel()  # undo/redo or page changes invalidate the open text

    # -- what is where -----------------------------------------------------------
    def stories(self, page: int) -> list[flow.Story]:
        try:
            return flow.page_stories(self.ctx.pdf.doc, page)
        except Exception:
            return []

    def story_at(self, page: int, pos: QPointF) -> tuple[flow.Story, int] | None:
        if page < 0:
            return None
        try:
            return flow.story_at(self.ctx.pdf.doc, page, (pos.x(), pos.y()))
        except Exception:
            return None

    def blocks(self, page: int) -> list[te.TextBlock]:
        """Every editable paragraph of the page (bullets excluded)."""
        return [p.block for s in self.stories(page) for p in s.pieces]

    def block_at(self, page: int, pos: QPointF) -> te.TextBlock | None:
        hit = self.story_at(page, pos)
        return hit[0].pieces[hit[1]].block if hit else None

    def rotated(self) -> bool:
        """Does the open text run at an angle on screen?"""
        return self.story is not None and bool(self.story.pieces) and abs(self.story.pieces[0].block.angle) > 0.01

    # -- "Vis tekstflader" -------------------------------------------------------
    def set_show_all_boxes(self, on: bool) -> None:
        on = bool(on)
        if on == self.show_all_boxes:
            return
        self.show_all_boxes = on
        try:
            _settings().setValue(SHOW_BOXES_KEY, on)
        except Exception:
            pass
        if not on:
            self._clear_box_outlines()
        self._schedule_box_outlines()

    def _schedule_box_outlines(self, *_a) -> None:
        if self.active and self.show_all_boxes:
            self._box_timer.start()

    def _clear_box_outlines(self) -> None:
        if self.ctx is not None:
            for item in self._box_items.values():
                self.ctx.view.remove_overlay(item)
        self._box_items.clear()

    def _refresh_box_outlines(self) -> None:
        """One dashed path per visible page (plus a screen of margin): the
        text surfaces – a column as a whole, standalone texts on their own."""
        if self.ctx is None or not self.active or not self.show_all_boxes:
            self._clear_box_outlines()
            return
        view = self.ctx.view
        wanted = set(view.visible_pages(extra_px=view.viewport().height() // 2))
        for page in [p for p in self._box_items if p not in wanted]:
            view.remove_overlay(self._box_items.pop(page))
        editing = (self.story.page, tuple(self.story.rect)) if self.story is not None else None
        for page in sorted(wanted):
            if page in self._box_items:
                continue
            path = QPainterPath()
            for st in self.stories(page):
                if editing == (page, tuple(st.rect)):
                    continue
                r = st.rect
                path.addRoundedRect(QRectF(r.x0 - HOVER_PAD, r.y0 - HOVER_PAD,
                                           r.width + 2 * HOVER_PAD, r.height + 2 * HOVER_PAD), 1.5, 1.5)
            item = QGraphicsPathItem(path)
            color = T.qcolor("accent")
            color.setAlpha(150)
            pen = QPen(color, 1.0)
            pen.setCosmetic(True)
            pen.setDashPattern([3.0, 3.0])
            item.setPen(pen)
            item.setZValue(15)
            item.setAcceptedMouseButtons(Qt.MouseButton.NoButton)
            view.add_overlay(page, item)
            self._box_items[page] = item

    def _refresh_page_outline(self, page: int) -> None:
        if page in self._box_items and self.ctx is not None:
            self.ctx.view.remove_overlay(self._box_items.pop(page))
        self._schedule_box_outlines()

    # -- mouse -----------------------------------------------------------------
    def mouse_move(self, ev: ToolMouseEvent) -> None:
        if self._forward_mouse(QEvent.Type.MouseMove, ev):
            return
        if self._drag_anchor is not None and self.editor is not None and ev.buttons & Qt.MouseButton.LeftButton:
            cur = self.editor.textCursor()
            cur.setPosition(self._drag_anchor)
            cur.setPosition(self._caret_at(ev), QTextCursor.MoveMode.KeepAnchor)
            self.editor.setTextCursor(cur)
            return
        over = self.story_at(ev.page, ev.pos) is not None
        self.ctx.view.viewport().setCursor(Qt.CursorShape.IBeamCursor if over else Qt.CursorShape.ArrowCursor)

    def mouse_press(self, ev: ToolMouseEvent) -> None:
        if not ev.left:
            return
        if self._forward_mouse(QEvent.Type.MouseButtonPress, ev):
            return
        hit = self.story_at(ev.page, ev.pos)
        if self.editor is not None and hit is not None and self._same_story(hit[0]):
            # a click in the open text outside the editor's widget (beside a line)
            self._place_caret(ev)
            return
        if self.editor is not None:
            self.commit()
            hit = self.story_at(ev.page, ev.pos)      # the page may have changed
        if hit is not None:
            self.start_story(hit[0], click=(ev.page, QPointF(ev.pos)))
            if self.editor is not None and self._proxy is None:
                self._drag_anchor = self.editor.textCursor().position()

    def mouse_release(self, ev: ToolMouseEvent) -> None:
        self._drag_anchor = None
        self._forward_mouse(QEvent.Type.MouseButtonRelease, ev)

    def mouse_double_click(self, ev: ToolMouseEvent) -> None:
        if self._forward_mouse(QEvent.Type.MouseButtonDblClick, ev):
            return
        if self.editor is not None and self._proxy is None:
            cur = self.editor.textCursor()
            cur.setPosition(self._caret_at(ev))
            cur.select(QTextCursor.SelectionType.WordUnderCursor)
            self.editor.setTextCursor(cur)

    def _same_story(self, story: flow.Story) -> bool:
        return self.story is not None and story.page == self.story.page and tuple(story.rect) == tuple(self.story.rect)

    def _caret_at(self, ev: ToolMouseEvent) -> int:
        view = self.ctx.view
        pos = self.editor.viewport().mapFrom(view.viewport(), ev.view_pos.toPoint())
        return self.editor.cursorForPosition(pos).position()

    def _place_caret(self, ev: ToolMouseEvent) -> None:
        cur = self.editor.textCursor()
        cur.setPosition(self._caret_at(ev))
        self.editor.setTextCursor(cur)
        self.editor.setFocus(Qt.FocusReason.MouseFocusReason)
        self._drag_anchor = cur.position()

    def _forward_mouse(self, kind: QEvent.Type, ev: ToolMouseEvent) -> bool:
        """Mouse events on the rotated editor (a scene proxy) are passed on to
        it, so clicking, drag-selecting and double-clicking work as in any
        text field."""
        if self._proxy is None or self.editor is None:
            return False
        local = self._proxy.mapFromScene(ev.scene_pos)
        inside = QRectF(self.editor.rect()).contains(local)
        if kind == QEvent.Type.MouseMove:
            if not self._proxy_drag:
                if inside:
                    self.ctx.view.viewport().setCursor(Qt.CursorShape.IBeamCursor)
                return inside
        elif kind == QEvent.Type.MouseButtonRelease:
            if not self._proxy_drag:
                return False
            self._proxy_drag = False
        elif not inside:
            return False
        elif kind == QEvent.Type.MouseButtonPress:
            self._proxy_drag = True
        target = self.editor.viewport()
        pos = local - QPointF(target.pos())
        glob = QPointF(self.ctx.view.viewport().mapToGlobal(ev.view_pos.toPoint()))
        button = ev.button if kind != QEvent.Type.MouseMove else Qt.MouseButton.NoButton
        if button == Qt.MouseButton.NoButton and kind != QEvent.Type.MouseMove:
            button = Qt.MouseButton.LeftButton
        from PySide6.QtWidgets import QApplication

        QApplication.sendEvent(target, QMouseEvent(kind, pos, glob, button, ev.buttons, ev.modifiers))
        return True

    def key_press(self, ev: QKeyEvent) -> bool:
        if ev.key() == Qt.Key.Key_Escape and self.editor is not None:
            self.commit()
            return True
        return False

    # -- the paragraph at the caret ------------------------------------------------
    def _qblock(self):
        """The editor paragraph the caret is in."""
        if self.editor is None:
            return None
        return self.editor.textCursor().block()

    def _piece_index(self, qblock) -> int | None:
        if qblock is None:
            return None
        tag = qblock.blockFormat().property(_PIECE)
        return int(tag) if tag is not None else None

    def _active(self) -> int | None:
        k = self._piece_index(self._qblock())
        if k is None and self.story is not None and self.story.pieces:
            k = 0
        return k

    @property
    def block(self) -> te.TextBlock | None:
        """The paragraph the caret is in (its original block)."""
        k = self._active()
        if k is None or self.story is None or k >= len(self.story.pieces):
            return None
        return self.story.pieces[k].block

    def _changes_of(self, qblock) -> dict:
        val = qblock.blockFormat().property(_STYLE) if qblock is not None else None
        return dict(val) if val else {}

    def current_style(self) -> core_fonts.SpanStyle | None:
        b = self.block
        if b is None:
            return None
        changes = self._changes_of(self._qblock())
        return replace(b.style, **{k: (tuple(v) if k == "color" else v) for k, v in changes.items()}) \
            if changes else b.style

    @property
    def style_override(self) -> core_fonts.SpanStyle | None:
        return self.current_style() if self._changes_of(self._qblock()) else None

    @property
    def align_override(self) -> str | None:
        q = self._qblock()
        val = q.blockFormat().property(_ALIGN) if q is not None else None
        return str(val) if val else None

    @property
    def spacing_override(self) -> float | None:
        q = self._qblock()
        val = q.blockFormat().property(_SPACING) if q is not None else None
        return float(val) if val else None

    @property
    def _layout(self) -> te.LayoutInfo | None:
        k = self._active()
        if k is None or self.story is None:
            return None
        return self._layout_of(self.story.pieces[k], self.align_override, self.spacing_override)

    def _layout_of(self, piece: flow.Piece, align: str | None, spacing: float | None) -> te.LayoutInfo | None:
        if align is None and spacing is None and piece.layout is not None:
            return piece.layout
        try:
            return te.layout_info(self.ctx.pdf.doc, piece.block, align=align,
                                  line_spacing=spacing * SINGLE if spacing else None)
        except Exception:
            return piece.layout

    def set_style(self, style: core_fonts.SpanStyle) -> None:
        """The inspector without a selection: the whole paragraph at the
        caret gets this style (what differs from its original)."""
        q, b = self._qblock(), self.block
        if q is None or b is None:
            return
        changes = {}
        for key in ("font", "size", "color", "bold", "italic", "underline", "strike"):
            val = getattr(style, key)
            if (tuple(val) if key == "color" else val) != (tuple(getattr(b.style, key)) if key == "color"
                                                           else getattr(b.style, key)):
                changes[key] = list(val) if key == "color" else val
        fmt = q.blockFormat()
        fmt.setProperty(_STYLE, changes or None)
        self._set_block_format(q, fmt)
        self._restyle_block(q)
        if self._panel is not None:
            self._panel.refresh()

    def reset_style(self) -> None:
        """"Nulstil til original": the paragraph (or the selection) as it was."""
        if self.editor is None:
            return
        cur = self.editor.textCursor()
        blocks = self._selected_blocks() if cur.hasSelection() else [self._qblock()]
        for q in blocks:
            fmt = q.blockFormat()
            fmt.setProperty(_STYLE, None)
            self._set_block_format(q, fmt)
            c = QTextCursor(q)
            c.setPosition(q.position())
            c.setPosition(q.position() + q.length() - 1, QTextCursor.MoveMode.KeepAnchor)
            self._strip_user_formats(c)
            self._restyle_block(q)
        if self._panel is not None:
            self._panel.refresh()

    def has_user_format(self) -> bool:
        q = self._qblock()
        if q is None:
            return False
        if self._changes_of(q):
            return True
        it = q.begin()
        while not it.atEnd():
            f = it.fragment()
            if f.isValid() and any(f.charFormat().hasProperty(p) for p in FORMAT_PROPS.values()):
                return True
            it += 1
        return False

    # -- opening -------------------------------------------------------------------
    def start_edit(self, block: te.TextBlock) -> None:
        """Open the text surface containing ``block`` with the caret at the
        end of that paragraph."""
        for st in self.stories(block.page):
            for i, p in enumerate(st.pieces):
                if p.block.bbox == block.bbox and p.block.text == block.text:
                    self.start_story(st, piece=i)
                    return

    def start_story(self, story: flow.Story, *, click: tuple | None = None, piece: int | None = None,
                    at_end: bool = False, session: object | None = None) -> None:
        self.cancel()
        view = self.ctx.view
        self.story = story
        self._session = session if session is not None else object()
        if not story.pieces:
            return
        first = story.pieces[0].block
        self._anchor = first.lines[0].origin if first.lines else (first.bbox.x0, first.bbox.y1)
        self._axes = _unit(first) if abs(first.angle) > 0.01 else ((1.0, 0.0), (0.0, 1.0))
        self._ppp = self._px_per_pt()
        self._looks = [self._look_for(p) for p in story.pieces]
        s0 = min(lk.left for lk in self._looks)
        s1 = max(lk.right for lk in self._looks)
        # room above the first line for Qt's line box (a top margin cannot be negative)
        t0 = min(lk.first[1] - max(1.6 * lk.style.size, lk.pitch) for lk in self._looks) - 2.0
        self._box = (s0, s1, t0)
        if self.rotated():
            self.editor = InlineTextEditor(None)
            self.editor.setMinimumSize(8, 8)  # else the proxy keeps QTextEdit's 78 px minimum
            self.editor.ensurePolished()
            self._proxy = QGraphicsProxyWidget()
            self._proxy.setWidget(self.editor)
            self._proxy.setZValue(36)
            view.add_overlay(story.page, self._proxy)
        else:
            self.editor = InlineTextEditor(view.viewport())
        ed = self.editor
        ed.escape_commits = True
        ed.bullet_tag = _PIECE
        ed.commitRequested.connect(self.commit)
        ed.cancelRequested.connect(self.cancel)
        ed.nextRequested.connect(lambda: self.leave(1, tab=True))
        ed.previousRequested.connect(lambda: self.leave(-1, tab=True))
        ed.leaveRequested.connect(self.leave)
        ed.formatRequested.connect(self.toggle_format)
        ed.cursorPositionChanged.connect(self._selection_moved)
        self._build_document()
        ed.document().contentsChanged.connect(self._reposition)
        self._keys = QLabel(self._keys_html(), view.viewport())
        self._keys.setObjectName("EditKeysHint")
        self._keys.setTextFormat(Qt.TextFormat.RichText)
        self._keys.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        self._keys_covers_text = False
        ed.textChanged.connect(self._on_typed)
        self._reposition()
        self._render_background()
        ed.show()
        self._keys.show()
        if self._proxy is not None:
            view.setFocus(Qt.FocusReason.MouseFocusReason)
            self._proxy.setFocus(Qt.FocusReason.MouseFocusReason)
        ed.setFocus(Qt.FocusReason.MouseFocusReason)
        # the caret: where the click was, or in the given paragraph
        cur = ed.textCursor()
        if click is not None and self._proxy is None:
            page, pos = click
            vp = view.page_to_viewport(page, pos)
            cur = ed.cursorForPosition(ed.viewport().mapFrom(view.viewport(), vp.toPoint()))
        elif piece is not None:
            q = ed.document().findBlockByNumber(piece)
            cur.setPosition(q.position() + q.length() - 1)     # the end of that paragraph
        else:
            cur.movePosition(QTextCursor.MoveOperation.End if at_end else QTextCursor.MoveOperation.Start)
        ed.setTextCursor(cur)
        r = story.rect if click is None else pymupdf.Rect(click[1].x(), click[1].y(), click[1].x(), click[1].y())
        tl = view.page_to_scene(story.page, QPointF(r.x0, r.y0))
        br = view.page_to_scene(story.page, QPointF(r.x1, min(r.y1, r.y0 + 200)))
        view.ensureVisible(QRectF(tl, br).normalized(), 40, 60)
        if self.show_all_boxes:
            self._refresh_page_outline(story.page)
        self.changed.emit()
        if self._panel is not None:
            self._panel.refresh()

    def _keys_html(self) -> str:
        """The key hints with the keys emphasised: "<b>Esc</b> afslutter · ..."."""
        strong, sep = T.qcolor("text").name(), T.qcolor("text_subtle").name()
        parts = []
        for part in tr("edit.text.keys").split("·"):
            key, _, what = part.strip().partition(" ")
            parts.append(f"<span style='color:{strong}; font-weight:600'>{key}</span> {what}")
        return f" <span style='color:{sep}'>·</span> ".join(parts)

    def _on_typed(self) -> None:
        if self._keys is not None and self._keys_covers_text:
            self._keys.hide()

    def _px_per_pt(self) -> float:
        """Viewport pixels per pt, exactly (a column is hundreds of pt tall:
        a rounded scale would drift)."""
        view = self.ctx.view
        page = self.story.page if self.story is not None else 0
        m = view.viewportTransform()
        a = m.map(view.page_to_scene(page, QPointF(0, 0)))
        b = m.map(view.page_to_scene(page, QPointF(1000, 0)))
        c = m.map(view.page_to_scene(page, QPointF(0, 1000)))
        return max(abs(b.x() - a.x()), abs(b.y() - a.y()), abs(c.y() - a.y()), abs(c.x() - a.x())) / 1000.0

    # -- the paragraphs' looks -----------------------------------------------------
    def _look_for(self, piece: flow.Piece, align: str | None = None, spacing: float | None = None) -> _Look:
        b = piece.block
        lay = self._layout_of(piece, align, spacing)
        u, v = self._axes
        style = b.style
        size = style.size or 10.0
        if spacing:
            pitch = spacing * SINGLE * size
        elif lay is not None and getattr(lay, "pitch", 0.0):
            pitch = lay.pitch
        else:
            pitch = _line_pitch(b)
        starts, ends = [], []
        for ln in b.lines:
            s, _t = _local(self._anchor, u, v, *ln.origin)
            starts.append(s)
            ends.append(s + max(0.0, ln.extent[1] - ln.extent[0]))
        if not starts:
            starts, ends = [0.0], [b.bbox.width]
        first = _local(self._anchor, u, v, *(b.lines[0].origin if b.lines else (b.bbox.x0, b.bbox.y1)))
        lmin, rmax = min(starts), max(ends)
        x_first, x_rest = starts[0], starts[0]
        if len(starts) >= 2:
            rest = starts[1:]
            x_rest = min(rest) if max(rest) - min(rest) <= 1.0 else lmin
        elif b.lines:
            k = te.marker_end(b.lines[0].text)
            ln = b.lines[0]
            if k and len(ln.char_pos) == len(ln.text):
                x_rest = x_first + (ln.char_pos[k][0] - ln.extent[0])
        how = align or ("justify" if lay is not None and lay.justified else (lay.align if lay is not None else "left"))
        wrap = lay.wrap_width if lay is not None else (rmax - lmin)
        slack = 0.5 if how == "justify" and not align else max(2.0, 0.3 * size)
        if wrap <= 0:
            wrap = max(self._room_ahead(b, first[0]) - (lmin - first[0]), rmax - lmin)
        if align and lay is not None and lay.frame and not self.rotated():
            ax = self._anchor[0] * u[0] + self._anchor[1] * u[1]
            left, right = lay.frame[0] - ax, lay.frame[1] - ax + slack
        elif how == "center":
            c = (lmin + rmax) / 2
            w = max(wrap, rmax - lmin) + slack
            left, right = c - w / 2, c + w / 2
        elif how == "right":
            right = rmax + 0.75
            left = right - max(wrap, rmax - lmin) - slack
        else:
            left = min(x_first, x_rest)
            right = left + max(wrap, rmax - left) + slack
        bullet = None
        if piece.bullet is not None and piece.bullet.lines:
            bl = piece.bullet.lines[0]
            bs, _bt = _local(self._anchor, u, v, *bl.origin)
            bullet = (bl.text, bl.style, bs)
        look = _Look(style=style, pitch=pitch, align=how, left=left, right=right,
                     indent=x_first - x_rest if how in ("left", "justify") else 0.0, first=first, bullet=bullet)
        look.spacing = self._spacing_fix(b, look)
        return look

    def _room_ahead(self, b: te.TextBlock, s_first: float) -> float:
        """Distance (pt) from the paragraph's start to the page edge along
        the text – how far a never-wrapping line may run."""
        u, _v = self._axes
        rect = self.ctx.pdf.page(b.page).rect
        ox, oy = self._anchor[0] + u[0] * s_first, self._anchor[1] + u[1] * s_first
        room = []
        for o, d, lo, hi in ((ox, u[0], rect.x0, rect.x1), (oy, u[1], rect.y0, rect.y1)):
            if abs(d) > 1e-6:
                room.append(((hi if d > 0 else lo) - o) / d)
        return max(min(room) if room else 0.0, 0.0)

    def _qfont(self, style: core_fonts.SpanStyle, px: float) -> QFont:
        font = QFont(core_fonts.qt_family_hint(style))
        font.setPixelSize(max(1, round(style.size * px)))
        font.setBold(style.bold)
        font.setItalic(style.italic)
        font.setUnderline(style.underline)
        font.setStrikeOut(style.strike)
        font.setKerning(True)
        font.setHintingPreference(QFont.HintingPreference.PreferNoHinting)
        return font

    def _spacing_fix(self, block: te.TextBlock, look: _Look) -> float:
        """Letter spacing (%) that makes the on-screen font as wide as the
        PDF's: the screen family is often a metric-close substitute."""
        lines = [ln for ln in block.lines if len(ln.text.strip()) >= 4]
        if not lines:
            return 100.0
        ln = max(lines, key=lambda ln: len(ln.text))
        ppp = self._ppp
        font = self._qfont(look.style, ppp)
        qt_w = QFontMetricsF(font).horizontalAdvance(ln.text.rstrip())
        pdf_w = (ln.extent[1] - ln.extent[0]) * ppp
        if qt_w <= 0:
            return 100.0
        return max(85.0, min(115.0, 100.0 * pdf_w / qt_w))

    def _char_base(self, look: _Look, changes: dict | None = None) -> QTextCharFormat:
        style = look.style
        if changes:
            style = replace(style, **{k: (tuple(v) if k == "color" else v) for k, v in changes.items()})
        fmt = QTextCharFormat()
        fmt.setFont(self._qfont(style, self._ppp), QTextCharFormat.FontPropertiesInheritanceBehavior.FontPropertiesAll)
        fmt.setFontLetterSpacingType(QFont.SpacingType.PercentageSpacing)
        fmt.setFontLetterSpacing(look.spacing if not changes or "font" not in changes else 100.0)
        fmt.setForeground(to_qcolor(tuple(style.color)))
        fmt.setProperty(_PT, float(style.size))
        return fmt

    # -- the document --------------------------------------------------------------
    def _build_document(self) -> None:
        ed, story = self.editor, self.story
        style = story.pieces[0].block.style
        ed.configure(self._qfont(style, self._ppp), to_qcolor(tuple(style.color)), QColor(0, 0, 0, 0), 0.0,
                     max(8, int(round((self._box[1] - self._box[0]) * self._ppp))))
        ed.setLineWrapMode(QTextEdit.LineWrapMode.FixedPixelWidth)
        ed.blockSignals(True)
        try:
            ed.setPlainText("\n".join(p.text.replace("\n", _LS) for p in story.pieces))
            doc = ed.document()
            q = doc.begin()
            for k, (piece, look) in enumerate(zip(story.pieces, self._looks)):
                fmt = QTextBlockFormat()
                fmt.setProperty(_PIECE, k)
                fmt.setProperty(_GEOM, [look.left, look.right, look.indent, look.pitch, 0.0])
                self._set_block_format(q, fmt)
                c = QTextCursor(q)
                c.setPosition(q.position())
                c.setPosition(q.position() + q.length() - 1, QTextCursor.MoveMode.KeepAnchor)
                base = self._char_base(look)
                c.setCharFormat(base)
                c.setBlockCharFormat(base)
                self._merge_char_formats(q, piece, look)
                q = q.next()
            self._apply_px()
            self._align_baselines()
            doc.clearUndoRedoStacks()
            self._update_bullets()
        finally:
            ed.blockSignals(False)

    def _set_block_format(self, q, fmt: QTextBlockFormat) -> None:
        c = QTextCursor(q)
        self.editor.blockSignals(True)
        try:
            c.setBlockFormat(fmt)
        finally:
            self.editor.blockSignals(False)

    def _geom(self, q) -> list:
        g = q.blockFormat().property(_GEOM)
        return list(g) if g else [self._box[0], self._box[1], 0.0, 12.0, 0.0]

    def _apply_px(self) -> None:
        """Pixel sizes, margins and line heights from the pt values (zoom)."""
        ed = self.editor
        ppp = self._ppp
        s0, s1, _t0 = self._box
        ed.setLineWrapColumnOrWidth(max(8, int(round((s1 - s0) * ppp))))
        doc = ed.document()
        q = doc.begin()
        last = 0
        ed.blockSignals(True)
        try:
            while q.isValid():
                left, right, indent, pitch, top = self._geom(q)
                fmt = q.blockFormat()
                align = str(fmt.property(_ALIGN) or "")
                k = self._piece_index(q)
                if k is None or k >= len(self._looks):
                    k = last                 # an untagged paragraph looks like the one before
                last = k
                how = align or (self._looks[k].align if self._looks else "left")
                fmt.setLeftMargin(max(0.0, (left - s0) * ppp) + (max(0.0, -indent) * ppp if how in ("left", "justify")
                                                                  else 0.0))
                fmt.setTextIndent(indent * ppp if how in ("left", "justify") else 0.0)
                fmt.setRightMargin(max(0.0, (s1 - right) * ppp))
                fmt.setLineHeight(pitch * ppp, QTextBlockFormat.LineHeightTypes.FixedHeight.value)
                fmt.setTopMargin(max(0.0, top * ppp))
                fmt.setAlignment({"center": Qt.AlignmentFlag.AlignHCenter, "right": Qt.AlignmentFlag.AlignRight,
                                  "justify": Qt.AlignmentFlag.AlignJustify}.get(how, Qt.AlignmentFlag.AlignLeft))
                QTextCursor(q).setBlockFormat(fmt)
                # character pixel sizes from their pt sizes
                it = q.begin()
                while not it.atEnd():
                    frag = it.fragment()
                    if frag.isValid():
                        cf = frag.charFormat()
                        pt = cf.property(FORMAT_PROPS["size"]) if cf.hasProperty(FORMAT_PROPS["size"]) \
                            else cf.property(_PT)
                        if pt:
                            px = max(1, round(float(pt) * ppp))
                            if cf.font().pixelSize() != px:
                                f = QTextCharFormat()
                                font = QFont(cf.font())
                                font.setPixelSize(px)
                                f.setFont(font, QTextCharFormat.FontPropertiesInheritanceBehavior.FontPropertiesSpecifiedOnly)
                                c = QTextCursor(doc)
                                c.setPosition(frag.position())
                                c.setPosition(frag.position() + frag.length(), QTextCursor.MoveMode.KeepAnchor)
                                c.mergeCharFormat(f)
                    it += 1
                q = q.next()
        finally:
            ed.blockSignals(False)
        self._update_bullets()

    def _first_baseline_px(self) -> float | None:
        doc = self.editor.document()
        q = doc.begin()
        doc.documentLayout().blockBoundingRect(q)
        ql = q.layout()
        if ql is None or not ql.lineCount():
            return None
        return ql.position().y() + ql.lineAt(0).y() + ql.lineAt(0).ascent()

    def _align_first(self) -> None:
        """The editor's top so the first paragraph's baseline lands on the
        PDF's (Qt ignores the first paragraph's top margin)."""
        got = self._first_baseline_px()
        if got is None or not self._looks:
            return
        s0, s1, _t0 = self._box
        self._box = (s0, s1, self._looks[0].first[1] - got / self._ppp)

    def _align_baselines(self) -> None:
        """Top margins so every paragraph's first baseline lands on the PDF's
        (measured from Qt's own layout)."""
        ed = self.editor
        doc = ed.document()
        layout = doc.documentLayout()
        ppp = self._ppp
        self._align_first()
        t0 = self._box[2]
        q = doc.begin().next()
        k = 1
        while q.isValid() and k < len(self._looks):
            want = (self._looks[k].first[1] - t0) * ppp
            layout.blockBoundingRect(q)
            ql = q.layout()
            if ql is not None and ql.lineCount():
                got = ql.position().y() + ql.lineAt(0).y() + ql.lineAt(0).ascent()
                fmt = q.blockFormat()
                top = max(0.0, fmt.topMargin() + want - got)
                fmt.setTopMargin(top)
                g = self._geom(q)
                g[4] = top / ppp
                fmt.setProperty(_GEOM, g)
                QTextCursor(q).setBlockFormat(fmt)
            q = q.next()
            k += 1

    def _merge_char_formats(self, q, piece: flow.Piece, look: _Look) -> None:
        """The original paragraph's own character styles (a bold word, a
        coloured phrase): one format per run of equally styled characters."""
        base = piece.block.style
        base_key = _style_key(base)
        formats: dict = {}
        doc = self.editor.document()
        cur = QTextCursor(doc)

        def fmt_for(st: core_fonts.SpanStyle) -> QTextCharFormat:
            key = _style_key(st)
            fmt = formats.get(key)
            if fmt is None:
                fmt = QTextCharFormat()
                fmt.setFontWeight(QFont.Weight.Bold if st.bold else QFont.Weight.Normal)
                fmt.setFontItalic(st.italic)
                fmt.setForeground(to_qcolor(st.color))
                if abs(st.size - base.size) > 0.05:
                    f = QFont(self._qfont(base, self._ppp))
                    f.setPixelSize(max(1, round(st.size * self._ppp)))
                    fmt.setFont(f, QTextCharFormat.FontPropertiesInheritanceBehavior.FontPropertiesSpecifiedOnly)
                    fmt.setProperty(_PT, float(st.size))
                if st.font != base.font and not (st.bold != base.bold or st.italic != base.italic):
                    fmt.setFontFamilies([core_fonts.qt_family_hint(st)])
                formats[key] = fmt
            return fmt

        pos = q.position()
        for line in piece.block.lines:
            styles = (line.char_styles or [])[:len(line.text)]
            k = 0
            while k < len(styles):
                st = styles[k]
                key = _style_key(st) if st is not None else base_key
                end = k + 1
                while end < len(styles) and styles[end] is not None and _style_key(styles[end]) == key:
                    end += 1
                if key != base_key:
                    cur.setPosition(pos + k)
                    cur.setPosition(pos + end, QTextCursor.MoveMode.KeepAnchor)
                    cur.mergeCharFormat(fmt_for(st))
                k = end
            pos += len(line.text) + 1

    def _restyle_block(self, q) -> None:
        """A paragraph's style changed in the inspector: its look follows."""
        k = self._piece_index(q)
        if k is None or k >= len(self._looks):
            return
        changes = self._changes_of(q)
        look = self._looks[k]
        c = QTextCursor(q)
        c.setPosition(q.position())
        c.setPosition(q.position() + q.length() - 1, QTextCursor.MoveMode.KeepAnchor)
        fmt = self._char_base(look, changes)
        if not changes:
            c.setCharFormat(fmt)
            self._merge_char_formats(q, self.story.pieces[k], look)
        else:
            c.mergeCharFormat(fmt)
        c.setBlockCharFormat(fmt)
        g = self._geom(q)
        scale = float(changes.get("size", look.style.size)) / max(look.style.size, 0.1)
        spacing = self._spacing_value(q)
        g[3] = spacing * SINGLE * float(changes.get("size", look.style.size)) if spacing else look.pitch * scale
        bf = q.blockFormat()
        bf.setProperty(_GEOM, g)
        self._set_block_format(q, bf)
        self._apply_px()
        self._reposition()

    def _spacing_value(self, q) -> float | None:
        val = q.blockFormat().property(_SPACING)
        return float(val) if val else None

    def _update_bullets(self) -> None:
        """List bullets drawn by the editor next to their paragraph, so they
        follow it while you type (the page behind shows none)."""
        ed = self.editor
        if ed is None:
            return
        bullets = {}
        for k, look in enumerate(self._looks):
            if look.bullet is not None:
                text, st, s = look.bullet
                bullets[k] = (text, self._qfont(st, self._ppp), to_qcolor(tuple(st.color)),
                              (s - self._box[0]) * self._ppp)
        ed.bullets = bullets
        ed.viewport().update()

    # -- geometry -----------------------------------------------------------------
    def _reposition(self, *_a) -> None:
        if self.editor is None or self.story is None:
            return
        ed = self.editor
        ppp = self._ppp
        s0, s1, t0 = self._box
        w = int(round((s1 - s0) * ppp)) + 2
        h = int(round(ed.document().size().height())) + 4
        if self._proxy is not None:
            ed.resize(w, h)
            self._proxy.resize(ed.size())
            # editor pixel (X, Y) is the point (s0 + X / ppp, t0 + Y / ppp)
            # of the text's own frame, rotated with the text onto the page
            t = QTransform()
            t.translate(self._anchor[0], self._anchor[1])
            t.rotate(self.story.pieces[0].block.angle)
            t.translate(s0, t0)
            t.scale(1.0 / ppp, 1.0 / ppp)
            self._proxy.setTransform(t)
            frame = QRectF(self.ctx.view.mapFromScene(self._proxy.mapToScene(QRectF(ed.rect()))).boundingRect())
            self._place_keys(frame)
            return
        view = self.ctx.view
        u, v = self._axes
        x = self._anchor[0] + u[0] * s0 + v[0] * t0
        y = self._anchor[1] + u[1] * s0 + v[1] * t0
        tl = view.mapFromScene(view.page_to_scene(self.story.page, QPointF(x, y)))
        ed.setGeometry(int(round(tl.x())), int(round(tl.y())), w, h)
        self._place_keys(QRectF(ed.geometry()))

    def _place_keys(self, frame: QRectF | None) -> None:
        """Put the key-hint chip next to the editor where it covers no other
        text (above or below it, right or left); else where it covers least,
        and it gives way once the user types."""
        chip = self._keys
        if chip is None or frame is None:
            return
        chip.adjustSize()
        w, h = chip.width(), chip.height()
        view = self.ctx.view
        area = QRectF(view.viewport().rect()).adjusted(4, 4, -4, -4)
        vis = frame & area if frame.intersects(area) else frame
        candidates = [
            QRectF(vis.right() - w, frame.top() - KEYS_GAP - h, w, h),    # above, right
            QRectF(vis.right() - w, frame.bottom() + KEYS_GAP, w, h),     # below, right
            QRectF(vis.left(), frame.top() - KEYS_GAP - h, w, h),         # above, left
            QRectF(vis.left(), frame.bottom() + KEYS_GAP, w, h),          # below, left
            QRectF(frame.right() + KEYS_GAP, vis.top(), w, h),            # beside, right
            QRectF(frame.left() - KEYS_GAP - w, vis.top(), w, h),         # beside, left
            QRectF(area.right() - w, area.top(), w, h),                   # the view's corner
        ]
        others = []
        page = self.story.page
        for st in self.stories(page):
            if self._same_story(st):
                continue
            for p in st.pieces:
                o = p.block.bbox
                tl = view.page_to_viewport(page, QPointF(o.x0, o.y0))
                br = view.page_to_viewport(page, QPointF(o.x1, o.y1))
                others.append(QRectF(tl, br).normalized().adjusted(-2, -2, 2, 2))
        inner = frame.adjusted(1, 1, -1, -1)

        def cost(r: QRectF) -> float:
            covered = sum((r & o).width() * (r & o).height() for o in others if r.intersects(o))
            if r.intersects(inner):
                covered += 1e6 + (r & inner).width() * (r & inner).height()
            outside = 0.0 if area.contains(r) else 1e7
            return covered + outside

        costs = [cost(r) for r in candidates]
        best = min(range(len(candidates)), key=lambda i: (costs[i], i))
        r = candidates[best]
        r.moveLeft(max(area.left(), min(r.left(), area.right() - w)))
        chip.move(int(round(r.left())), int(round(r.top())))
        self._keys_covers_text = costs[best] > 0

    def _viewport_rect(self, item) -> QRectF:
        view = self.ctx.view
        poly = view.mapFromScene(item.mapToScene(item.rect()))
        return QRectF(poly.boundingRect())

    def _render_background(self) -> None:
        """The page without the text being edited, under the (transparent)
        editor – images, lines and colour stay, no old text shows through."""
        if self.editor is None or self.story is None or not self._wants_background():
            return
        view = self.ctx.view
        try:
            zoom = view.zoom * max(1.0, view.devicePixelRatioF())
            pix = flow.render_without(self.ctx.pdf.doc, self.story, zoom)
        except Exception:
            return
        from PySide6.QtGui import QImage, QPixmap

        img = QImage(pix.samples, pix.width, pix.height, pix.stride, QImage.Format.Format_RGB888).copy()
        if self._bg is not None:
            view.remove_overlay(self._bg)
        item = QGraphicsPixmapItem(QPixmap.fromImage(img))
        item.setTransform(QTransform.fromScale(1.0 / zoom, 1.0 / zoom))
        item.setZValue(5)
        item.setAcceptedMouseButtons(Qt.MouseButton.NoButton)
        item.setTransformationMode(Qt.TransformationMode.SmoothTransformation)
        view.add_overlay(self.story.page, item)
        self._bg = item

    def _wants_background(self) -> bool:
        return True

    # -- closing ------------------------------------------------------------------
    def _teardown(self) -> None:
        page = self.story.page if self.story is not None else None
        self._bg_timer.stop()
        if self._proxy is not None:
            if self.ctx is not None:
                self.ctx.view.remove_overlay(self._proxy)
            self._proxy.deleteLater()
        elif self.editor is not None:
            self.editor.hide()
            self.editor.deleteLater()
        if self._keys is not None:
            self._keys.hide()
            self._keys.deleteLater()
        if self._bg is not None and self.ctx is not None:
            self.ctx.view.remove_overlay(self._bg)
        self.editor = self._keys = self._proxy = self._bg = None
        self._proxy_drag = False
        self._drag_anchor = None
        self.story = None
        self._looks = []
        if page is not None and self.show_all_boxes:
            self._refresh_page_outline(page)
        if self._panel is not None:
            self._panel.refresh()
        self.changed.emit()

    def cancel(self) -> None:
        if self.editor is None:
            return
        self._teardown()
        self.ctx.view.setFocus()

    def collect_edits(self) -> list[flow.Edit]:
        """The editor's paragraphs as core edits (in order). A paragraph
        whose tag was already seen is new (Enter split it off)."""
        out = []
        seen: set[int] = set()
        last = 0
        q = self.editor.document().begin()
        while q.isValid():
            k = self._piece_index(q)
            text = q.text().replace(_LS, "\n")
            fmt = q.blockFormat()
            align = str(fmt.property(_ALIGN)) if fmt.property(_ALIGN) else None
            spacing = self._spacing_value(q)
            changes = self._changes_of(q)
            style = None
            if changes and k is not None:
                style = replace(self.story.pieces[k].block.style,
                                **{a: (tuple(b) if a == "color" else b) for a, b in changes.items()})
            formats = self._block_formats(q)
            if k is None or k in seen:
                out.append(flow.Edit(piece=None, like=k if k is not None else last, text=text, formats=formats or None,
                                     align=align, line_spacing=spacing * SINGLE if spacing else None, style=style))
            else:
                seen.add(k)
                last = k
                out.append(flow.Edit(piece=k, text=text, formats=formats or None, align=align,
                                     line_spacing=spacing * SINGLE if spacing else None, style=style))
            q = q.next()
        return out

    def _block_formats(self, q) -> list[tuple[int, int, dict]]:
        out: list[tuple[int, int, dict]] = []
        it = q.begin()
        start = q.position()
        while not it.atEnd():
            frag = it.fragment()
            if frag.isValid():
                fmt = frag.charFormat()
                changes = {}
                for key, prop in FORMAT_PROPS.items():
                    if fmt.hasProperty(prop):
                        v = fmt.property(prop)
                        changes[key] = tuple(v) if key == "color" else v
                if changes:
                    a, b = frag.position() - start, frag.position() - start + frag.length()
                    if out and out[-1][1] == a and out[-1][2] == changes:
                        out[-1] = (out[-1][0], b, changes)
                    else:
                        out.append((a, b, changes))
            it += 1
        return out

    def collect_formats(self) -> list[tuple[int, int, dict]]:
        """The user's formatting of the paragraph at the caret (offsets
        into its text)."""
        q = self._qblock()
        return self._block_formats(q) if q is not None else []

    def commit(self) -> bool:
        """Write the editor's text into the PDF. Returns True if it changed."""
        if self.editor is None or self.story is None or self._committing:
            return False
        story = self.story
        edits = self.collect_edits()
        session = self._session
        self._teardown()
        self._committing = True
        try:
            changed = self._write_story(story, edits, session)
        except Exception as exc:  # noqa: BLE001 – never lose the user's session silently
            self.ctx.toast(tr("edit.text.error", err=str(exc)), "error")
            return False
        finally:
            self._committing = False
        if changed:
            QTimer.singleShot(150, self, lambda p=story.page: self._warm(p))
        return changed

    def _write_story(self, story: flow.Story, edits: list[flow.Edit], session) -> bool:
        """Put the edited text into the PDF; tell the user when a font lacked
        characters, and – for a standalone text that ran into what is below –
        offer to make it smaller."""
        pdf = self.ctx.pdf
        res = flow.apply_story(pdf, story, edits, merge=session if story.kind == "flow" else None)
        for wanted, used in res.substitutions[:2]:
            self.ctx.toast(tr("edit.text.substituted", font=display_font_name(wanted), used=display_font_name(used)),
                           "info")
        if res.overflow > 0 and story.kind == "single" and edits:
            e = edits[0]
            piece = story.pieces[0]
            text = te.restore_soft_breaks(piece.text, piece.soft, "\n".join(x.text for x in edits))

            def shrink() -> None:
                self._shrink_to_fit(piece.block, text, e.style, e.formats, e.align, e.line_spacing)

            self.ctx.toast(tr("edit.text.overflow"), "warning", action_text=tr("edit.text.overflow.fit"),
                           on_action=shrink, duration_ms=12000)
        return bool(res.pages)

    def _shrink_to_fit(self, block, text, style, formats, align, line_spacing) -> None:
        """"Formindsk så den passer": redo the edit with a smaller text size."""
        pdf = self.ctx.pdf
        label = pdf.history.undo_label
        if label != tr("core.undo.edit_text") and label != "Redigér tekst":
            return
        pdf.undo()  # the document is now exactly as ``block`` was read from it
        try:
            scale = te.fit_scale(pdf.doc, block, text, style, formats=formats, align=align,
                                 line_spacing=line_spacing)
            base = style or block.style
            st = replace(base, size=round(base.size * scale, 2))
            sized = [(a, b, {**c, "size": round(c["size"] * scale, 2)} if "size" in c else c)
                     for a, b, c in (formats or [])]
            te.replace_block_text(pdf, block, text, st, formats=sized or None, align=align,
                                  line_spacing=line_spacing)
        except Exception as exc:  # noqa: BLE001
            pdf.redo()
            self.ctx.toast(tr("edit.text.error", err=str(exc)), "error")
            return
        self.ctx.toast(tr("edit.text.overflow.done", pct=round(scale * 100)), "success")

    def _warm(self, page: int) -> None:
        """Idle after a commit: prepare the page so the next text opens fast."""
        try:
            te.warm_page(self.ctx.pdf.doc, page)
            flow.page_stories(self.ctx.pdf.doc, page)
        except Exception:  # only a cache – never disturb the user
            pass

    # -- moving on to the next text --------------------------------------------------
    def _reading_order(self, page: int, tab: bool) -> list[flow.Story]:
        stories = self.stories(page)
        if not tab:
            body = [s for s in stories if s.kind == "flow"]
            return sorted(body, key=lambda s: (s.lo, s.rect.y0))
        return sorted(stories, key=lambda s: (round(s.rect.y0 / 6.0), s.rect.x0))

    def leave(self, step: int, tab: bool = False) -> None:
        """Arrow past the end / start (or Tab / Shift+Tab): save and go on
        in the next / previous text surface – the next column, then the
        next page."""
        if self.story is None:
            return
        page = self.story.page
        here = self._reading_order(page, tab)
        idx = next((i for i, s in enumerate(here) if self._same_story(s)), -1)
        key = (round(self.story.rect.x0, 1), round(self.story.rect.y0, 1))
        session = self._session
        self.commit()
        here = self._reading_order(page, tab)      # positions may have changed
        if idx < 0:
            idx = next((i for i, s in enumerate(here) if (round(s.rect.x0, 1), round(s.rect.y0, 1)) == key), -1)
        j = idx + step
        target = here[j] if idx >= 0 and 0 <= j < len(here) else None
        if target is None:
            pages = range(page + 1, self.ctx.pdf.page_count) if step > 0 else range(page - 1, -1, -1)
            for p in pages:
                order = self._reading_order(p, tab)
                if order:
                    target = order[0] if step > 0 else order[-1]
                    break
        if target is not None:
            if target.page != page:
                self.ctx.view.go_to_page(target.page)
            self.start_story(target, at_end=step < 0, session=session)
            if self.editor is not None:
                cur = self.editor.textCursor()
                cur.movePosition(QTextCursor.MoveOperation.End if step < 0 else QTextCursor.MoveOperation.Start)
                self.editor.setTextCursor(cur)

    def commit_and_next(self, step: int = 1) -> None:
        self.leave(step, tab=True)

    # -- character formatting (selection) -------------------------------------
    def _px_size(self, pt: float) -> int:
        return max(1, round(pt * self._ppp))

    def _char_format(self, changes: dict) -> QTextCharFormat:
        """Qt format for ``changes`` (what it looks like + the property the
        commit reads back)."""
        fmt = QTextCharFormat()
        for key, value in changes.items():
            fmt.setProperty(FORMAT_PROPS[key], value if key != "color" else list(value))
            if key == "bold":
                fmt.setFontWeight(QFont.Weight.Bold if value else QFont.Weight.Normal)
            elif key == "italic":
                fmt.setFontItalic(bool(value))
            elif key == "underline":
                fmt.setFontUnderline(bool(value))
            elif key == "strike":
                fmt.setFontStrikeOut(bool(value))
            elif key == "color":
                fmt.setForeground(to_qcolor(tuple(value)))
            elif key == "size":
                f = QFont(self.editor.font())
                f.setPixelSize(self._px_size(float(value)))
                fmt.setFont(f, QTextCharFormat.FontPropertiesInheritanceBehavior.FontPropertiesSpecifiedOnly)
            elif key == "font":
                fmt.setFontFamilies([core_fonts.qt_family_hint(core_fonts.SpanStyle(str(value), 12))])
        return fmt

    def _strip_user_formats(self, cur: QTextCursor) -> None:
        """Remove the user's formatting from the characters of ``cur``."""
        doc = self.editor.document()
        a, b = cur.selectionStart(), cur.selectionEnd()
        q = doc.findBlock(a)
        while q.isValid() and q.position() <= b:
            it = q.begin()
            while not it.atEnd():
                frag = it.fragment()
                if frag.isValid() and frag.position() < b and frag.position() + frag.length() > a:
                    fmt = frag.charFormat()
                    if any(fmt.hasProperty(p) for p in FORMAT_PROPS.values()):
                        for p in FORMAT_PROPS.values():
                            fmt.clearProperty(p)
                        c = QTextCursor(doc)
                        c.setPosition(max(a, frag.position()))
                        c.setPosition(min(b, frag.position() + frag.length()), QTextCursor.MoveMode.KeepAnchor)
                        c.setCharFormat(fmt)
                it += 1
            q = q.next()

    def has_text_selection(self) -> bool:
        return self.editor is not None and self.editor.textCursor().hasSelection()

    def apply_char_format(self, changes: dict) -> None:
        """Format the selected text – or what is typed next at the cursor."""
        if self.editor is None or not changes:
            return
        fmt = self._char_format(changes)
        cur = self.editor.textCursor()
        if cur.hasSelection():
            cur.mergeCharFormat(fmt)
            self.editor.setTextCursor(cur)
        else:
            self.editor.mergeCurrentCharFormat(fmt)
        self.editor.setFocus()
        self._reposition()

    def format_state(self) -> dict:
        """Effective bold / italic / underline / strike of the selection (all
        characters) or of the cursor position."""
        out = {k: False for k in ("bold", "italic", "underline", "strike")}
        if self.editor is None:
            return out
        cur = self.editor.textCursor()
        doc = self.editor.document()
        if cur.hasSelection():
            a, b = cur.selectionStart(), cur.selectionEnd()
            fmts = []
            c = QTextCursor(doc)
            for pos in range(a, b):
                c.setPosition(pos + 1)
                if doc.characterAt(pos) not in (" ", "\n", _LS):
                    fmts.append(c.charFormat())
        else:
            fmts = [self.editor.currentCharFormat()]
        if not fmts:
            return out
        out["bold"] = all(f.fontWeight() >= 600 for f in fmts)
        out["italic"] = all(f.fontItalic() for f in fmts)
        out["underline"] = all(f.fontUnderline() for f in fmts)
        out["strike"] = all(f.fontStrikeOut() for f in fmts)
        return out

    def toggle_format(self, kind: str) -> None:
        """Ctrl+B / Ctrl+I / Ctrl+U and the inspector toggles."""
        if self.editor is None:
            return
        self.apply_char_format({kind: not self.format_state()[kind]})
        if self._panel is not None:
            self._panel.refresh_selection()

    def _selection_moved(self) -> None:
        if self._panel is not None:
            self._panel.refresh()

    def _selected_blocks(self) -> list:
        if self.editor is None:
            return []
        cur = self.editor.textCursor()
        doc = self.editor.document()
        a, b = cur.selectionStart(), cur.selectionEnd()
        out = []
        q = doc.findBlock(a)
        while q.isValid() and q.position() <= b:
            out.append(q)
            if q.position() + q.length() > b:
                break
            q = q.next()
        return out or [cur.block()]

    def set_paragraph(self, align: str | None = "", spacing: float | None = -1.0) -> None:
        """Alignment ("left" | "center" | "right" | "justify", ``None`` =
        original) and line spacing (× single, ``None`` = original) of the
        paragraphs in the selection (or the one at the caret)."""
        if self.editor is None or self.story is None:
            return
        for q in self._selected_blocks():
            fmt = q.blockFormat()
            if align != "":
                fmt.setProperty(_ALIGN, align or None)
            if spacing is None or spacing >= 0:
                fmt.setProperty(_SPACING, spacing or None)
            k = self._piece_index(q)
            if k is not None and k < len(self.story.pieces):
                look = self._look_for(self.story.pieces[k], str(fmt.property(_ALIGN) or "") or None,
                                      float(fmt.property(_SPACING)) if fmt.property(_SPACING) else None)
                g = self._geom(q)
                g[0], g[1], g[2], g[3] = look.left, look.right, look.indent, look.pitch
                fmt.setProperty(_GEOM, g)
                s0, s1, t0 = self._box
                self._box = (min(s0, look.left), max(s1, look.right), t0)
            self._set_block_format(q, fmt)
        self._apply_px()
        self._reposition()
        self.editor.setFocus()
        if self._panel is not None:
            self._panel.refresh()

    # -- hooks for subclasses ---------------------------------------------------
    def set_align(self, align: str) -> None:
        """Alignment for new text (only "Tilføj tekst" supports it)."""
