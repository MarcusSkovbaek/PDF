""""Rediger tekst" – edit existing PDF text in place, Acrobat style.

Hovering outlines the paragraph under the cursor. Clicking opens an inline
editor exactly on top of it, using the paragraph's own font, size, colour and
line pitch, painted on the page's own background colour so the original text
does not show through. Text that runs at an angle (rotated pages, vertical
labels) is edited in place too: the editor is embedded in the page scene and
rotated with the text. Committing calls
:func:`pdfstudio.core.text_edit.replace_block_text`, which rewrites the text
in the same font inside the PDF (one undo step).

Keys while editing: Ctrl+Enter or clicking elsewhere saves, Tab saves and
opens the next paragraph, Esc cancels. A small key-hint chip is placed next
to the frame where it covers no other text. The inspector shows the
paragraph's formatting (searchable font picker with recently used fonts);
changing it overrides the style for the whole paragraph. "Vis alle
tekstbokse" outlines every editable text block on the visible pages.

Extension points: ``InlineTextEditor`` (geometry/appearance),
``EditTextTool.start_edit``/``commit``/``cancel`` (session flow),
``_background_color`` (page background sampling) and the hooks used by
"Tilføj tekst" (``_layout_for``, ``_wrap_width``, ``_write`` ...).
"""

from __future__ import annotations

import math
from collections import Counter
from dataclasses import replace

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
    QGraphicsProxyWidget,
    QGraphicsRectItem,
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
        cur = QTextCursor(doc)
        cur.select(QTextCursor.SelectionType.Document)
        fmt = QTextBlockFormat()
        fmt.setLineHeight(self._line_px, QTextBlockFormat.LineHeightTypes.FixedHeight.value)
        self.blockSignals(True)
        cur.mergeBlockFormat(fmt)
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
        if key == Qt.Key.Key_Backtab or (key == Qt.Key.Key_Tab and mods & Qt.KeyboardModifier.ShiftModifier):
            ev.accept()
            self.previousRequested.emit()
            return
        super().keyPressEvent(ev)

    def event(self, ev) -> bool:
        # keep Tab for us instead of focus navigation
        if ev.type() == QEvent.Type.KeyPress and ev.key() in (Qt.Key.Key_Tab, Qt.Key.Key_Backtab):
            self.keyPressEvent(ev)
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
        self.text.setText(f"{block.style.size:g} pt")
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
            self.reset.setEnabled(self.tool.style_override is not None)
            self.rotated_note.setVisible(self.tool.rotated())
        finally:
            self._loading = False

    def _changed(self, *_a) -> None:
        if self._loading or self.tool.block is None:
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
    idle_title_key = "edit.text.idle.title"
    idle_text_key = "edit.text.idle.text"
    supports_box_outlines = True    # "Vis alle tekstbokse"
    supports_alignment = False      # alignment buttons (new text only)

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._blocks: dict[int, list[te.TextBlock]] = {}
        self._hover: QGraphicsRectItem | None = None
        self._frame: QGraphicsRectItem | None = None
        self.editor: InlineTextEditor | None = None
        self._proxy: QGraphicsProxyWidget | None = None   # rotated text: editor inside the scene
        self._proxy_drag = False
        self._keys: QLabel | None = None
        self._keys_covers_text = False
        self.block: te.TextBlock | None = None
        self.style_override: core_fonts.SpanStyle | None = None
        self._panel: EditTextPanel | None = None
        self._committing = False
        self._layout: te.LayoutInfo | None = None
        self._shown_text = ""
        self._soft_positions: list[int] = []
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
        self._clear_hover()
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
        """Zooming while editing: re-derive font pixel sizes, not just the box."""
        if self.editor is not None:
            self._apply_editor_style()
            self._apply_rich_formats()
        self._schedule_box_outlines()

    def _on_pages_reset(self) -> None:
        # the view dropped every overlay (frame, hover, outlines)
        self._box_items.clear()
        self._hover = None
        self._schedule_box_outlines()

    def _apply_rich_formats(self) -> None:
        """Show per-character styles (a bold word, a coloured phrase) of the
        original paragraph. Only characters that differ from the paragraph's
        dominant style get an explicit format; the core keeps those styles on
        commit by aligning the new text with the old one."""
        if self.editor is None or self.block is None or self.style_override is not None:
            return
        base, ppp = self.block.style, self._px_per_pt()
        doc = self.editor.document()
        cur = QTextCursor(doc)
        if doc.toPlainText() != self._shown_text:
            return  # the user already typed: offsets no longer match
        self.editor.blockSignals(True)  # formatting is not typing (keeps the key hint)
        try:
            self._merge_char_formats(cur, base, ppp)
        finally:
            self.editor.blockSignals(False)

    def _merge_char_formats(self, cur: QTextCursor, base: core_fonts.SpanStyle, ppp: float) -> None:
        pos = 0
        for line in self.block.lines:
            styles = line.char_styles or []
            for k, st in enumerate(styles[:len(line.text)]):
                if st is None or _style_key(st) == _style_key(base):
                    continue
                fmt = QTextCharFormat()
                fmt.setFontWeight(QFont.Weight.Bold if st.bold else QFont.Weight.Normal)
                fmt.setFontItalic(st.italic)
                fmt.setForeground(to_qcolor(st.color))
                if abs(st.size - base.size) > 0.05:
                    f = QFont(self.editor.font())
                    f.setPixelSize(max(1, round(st.size * ppp)))
                    fmt.setFont(f, QTextCharFormat.FontPropertiesInheritanceBehavior.FontPropertiesSpecifiedOnly)
                if st.font != base.font and not (st.bold != base.bold or st.italic != base.italic):
                    fmt.setFontFamilies([core_fonts.qt_family_hint(st)])
                cur.setPosition(pos + k)
                cur.setPosition(pos + k + 1, QTextCursor.MoveMode.KeepAnchor)
                cur.mergeCharFormat(fmt)
            pos += len(line.text) + 1  # "\n" between lines

    def properties_widget(self) -> QWidget | None:
        if self._panel is None:
            self._panel = EditTextPanel(self)
        self._panel.refresh()
        return self._panel

    def document_changed(self, event) -> None:
        self._blocks.clear()
        self._clear_box_outlines()
        self._schedule_box_outlines()
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

    def rotated(self) -> bool:
        """Does the open paragraph run at an angle on screen?"""
        return self.block is not None and abs(self.block.angle) > 0.01

    # -- "Vis alle tekstbokse" -------------------------------------------------
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
        """One dashed path per visible page (plus a screen of margin), built
        from the cached blocks; pages scrolled far away are dropped."""
        if self.ctx is None or not self.active or not self.show_all_boxes:
            self._clear_box_outlines()
            return
        view = self.ctx.view
        wanted = set(view.visible_pages(extra_px=view.viewport().height() // 2))
        for page in [p for p in self._box_items if p not in wanted]:
            view.remove_overlay(self._box_items.pop(page))
        editing = (self.block.page, tuple(self.block.bbox)) if self.block is not None else None
        for page in sorted(wanted):
            if page in self._box_items:
                continue
            path = QPainterPath()
            for b in self.blocks(page):
                if editing == (page, tuple(b.bbox)):
                    continue
                r = b.bbox
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
        if self._forward_mouse(QEvent.Type.MouseMove, ev):
            return
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
        if self._forward_mouse(QEvent.Type.MouseButtonPress, ev):
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

    def mouse_release(self, ev: ToolMouseEvent) -> None:
        self._forward_mouse(QEvent.Type.MouseButtonRelease, ev)

    def mouse_double_click(self, ev: ToolMouseEvent) -> None:
        self._forward_mouse(QEvent.Type.MouseButtonDblClick, ev)

    def _forward_mouse(self, kind: QEvent.Type, ev: ToolMouseEvent) -> bool:
        """The page view hands every mouse event to the tool; events on the
        rotated editor (a scene proxy) are passed on to it here, so clicking,
        drag-selecting and double-clicking work as in any text field."""
        if self._proxy is None or self.editor is None:
            return False
        local = self._proxy.mapFromScene(ev.scene_pos)
        inside = QRectF(self.editor.rect()).contains(local)
        if kind == QEvent.Type.MouseMove:
            if not self._proxy_drag:
                if inside:
                    self.ctx.view.viewport().setCursor(Qt.CursorShape.IBeamCursor)
                    self._clear_hover()
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
        self._layout = self._layout_for(block)
        # make sure the paragraph is on screen (Tab may jump below the fold)
        r = block.bbox
        tl = view.page_to_scene(block.page, QPointF(r.x0, r.y0))
        br = view.page_to_scene(block.page, QPointF(r.x1, r.y1))
        view.ensureVisible(QRectF(tl, br).normalized(), 40, 60)
        if self.rotated():
            # rotated text: edit it in place, in a proxy rotated with the text
            self.editor = InlineTextEditor(None)
            self.editor.setMinimumSize(8, 8)  # else the proxy keeps QTextEdit's 78 px minimum
            self.editor.ensurePolished()
            self._proxy = QGraphicsProxyWidget()
            self._proxy.setWidget(self.editor)
            self._proxy.setZValue(36)
            view.add_overlay(block.page, self._proxy)
        else:
            self.editor = InlineTextEditor(view.viewport())
        self.editor.commitRequested.connect(self.commit)
        self.editor.cancelRequested.connect(self.cancel)
        self.editor.nextRequested.connect(self.commit_and_next)
        self.editor.previousRequested.connect(lambda: self.commit_and_next(-1))
        self.editor.textChanged.connect(self._reposition)
        self._shown_text, self._soft_positions = self._display_text(block)
        self.editor.setPlainText(self._shown_text)
        self._keys = QLabel(self._keys_html(), view.viewport())
        self._keys.setObjectName("EditKeysHint")
        self._keys.setTextFormat(Qt.TextFormat.RichText)
        self._keys.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        self._keys_covers_text = False
        # where no free spot exists the chip gives way once typing starts
        self.editor.textChanged.connect(self._on_typed)
        self._frame = self._outline(block.page, block.bbox, EDIT_PAD, strong=True)
        self._apply_editor_style()
        self._apply_rich_formats()
        self.editor.show()
        self._keys.show()
        if self._proxy is not None:
            view.setFocus(Qt.FocusReason.MouseFocusReason)
            self._proxy.setFocus(Qt.FocusReason.MouseFocusReason)
        self.editor.setFocus(Qt.FocusReason.MouseFocusReason)
        self.editor.selectAll() if not block.text.strip() else self.editor.moveCursor(QTextCursor.MoveOperation.End)
        if self.show_all_boxes:
            self._refresh_page_outline(block.page)
        self.changed.emit()
        if self._panel is not None:
            self._panel.refresh()

    def _keys_html(self) -> str:
        """The key hints with the keys emphasised: "<b>Ctrl+Enter</b> gemmer · ..."."""
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
        bg = self._editor_background()
        width_px = int(round(self._wrap_width() * ppp))
        if self._proxy is not None:
            self._proxy.setFont(font)  # a proxy propagates its own font to the widget
        self.editor.configure(font, to_qcolor(style.color), bg, pitch * ppp, width_px)
        align = self._layout.align if self._layout is not None else "left"
        if align == "left" and self._layout is not None and self._layout.justified:
            align = "justify"
        self.editor.set_alignment({"center": Qt.AlignmentFlag.AlignHCenter,
                                   "right": Qt.AlignmentFlag.AlignRight,
                                   "justify": Qt.AlignmentFlag.AlignJustify}.get(align, Qt.AlignmentFlag.AlignLeft))
        self._reposition()

    def _editor_background(self) -> QColor:
        """Opaque page colour: hides the original text while it is edited."""
        return _background_color(self.ctx.pdf.doc, self.block.page, self.block.bbox)

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
        if not lines:
            return 100.0
        ln = max(lines, key=lambda ln: len(ln.text))
        qt_w = QFontMetricsF(font).horizontalAdvance(ln.text.rstrip())
        length = ln.extent[1] - ln.extent[0] if self.rotated() else ln.bbox.width
        pdf_w = length * ppp
        if qt_w <= 0:
            return 100.0
        return max(85.0, min(115.0, 100.0 * pdf_w / qt_w))

    def _display_text(self, block: te.TextBlock) -> tuple[str, list[int]]:
        """Editor text: lines joined with a space where the original line
        merely wrapped (soft break), so Qt reflows/justifies the paragraph like
        the PDF; hard breaks stay newlines. Returns (text, soft break offsets)."""
        soft = set(self._layout.soft_breaks) if self._layout is not None else set()
        parts, positions, pos = [], [], 0
        for i, line in enumerate(block.lines):
            parts.append(line.text)
            pos += len(line.text)
            if i < len(block.lines) - 1:
                if i in soft:
                    parts.append(" ")
                    positions.append(pos)
                else:
                    parts.append("\n")
                pos += 1
        return "".join(parts), positions

    @staticmethod
    def _restore_soft_breaks(shown: str, soft: list[int], text: str) -> str:
        """Put the original line breaks back where the text before them is
        unchanged: those lines stay pixel-identical; the rest reflows."""
        common = 0
        for a, c in zip(shown, text):
            if a != c:
                break
            common += 1
        chars = list(text)
        for p in soft:
            if p < common and p < len(chars) and chars[p] == " ":
                chars[p] = "\n"
        return "".join(chars)

    def _layout_for(self, block: te.TextBlock) -> te.LayoutInfo:
        """How the core will lay out this block (alignment, wrap width)."""
        return te.layout_info(self.ctx.pdf.doc, block)

    def _text_extent(self) -> tuple[float, float]:
        """(start, end) of the paragraph along its own direction, relative to
        the first line's origin (pt, display space)."""
        b = self.block
        u, _v = _unit(b)
        ox, oy = b.lines[0].origin if b.lines else (b.bbox.x0, b.bbox.y0)
        starts, ends = [], []
        for ln in b.lines:
            s = (ln.origin[0] - ox) * u[0] + (ln.origin[1] - oy) * u[1]
            starts.append(s)
            ends.append(s + max(0.0, ln.extent[1] - ln.extent[0]))
        return (min(starts), max(ends)) if starts else (0.0, b.bbox.width)

    def _room_ahead(self) -> float:
        """Distance (pt) from the paragraph's start to the page edge along
        the text direction – how far a never-wrapping line may run."""
        b = self.block
        u, _v = _unit(b)
        rect = self.ctx.pdf.page(b.page).rect
        ox, oy = b.lines[0].origin if b.lines else (b.bbox.x0, b.bbox.y0)
        ox += u[0] * self._text_extent()[0]
        oy += u[1] * self._text_extent()[0]
        room = []
        for o, d, lo, hi in ((ox, u[0], rect.x0, rect.x1), (oy, u[1], rect.y0, rect.y1)):
            if abs(d) > 1e-6:
                room.append(((hi if d > 0 else lo) - o) / d)
        return max(min(room) if room else 0.0, 0.0)

    def _wrap_width(self) -> float:
        """The core's own wrap width (``text_edit.layout_info``), so the editor
        breaks lines exactly where the PDF will; a little slack keeps the
        caret visible. 0 from the core means "never wraps"."""
        b = self.block
        slack = max(2.0, 0.3 * b.style.size)
        if self._layout is not None and self._layout.justified:
            slack = 0.5  # justified lines are stretched to the box: it must be the column
        wrap = self._layout.wrap_width if self._layout is not None else b.bbox.width
        if self.rotated():
            start, end = self._text_extent()
            if wrap <= 0:
                wrap = max(self._room_ahead(), end - start + 4 * b.style.size)
            return max(wrap, end - start) + slack
        if wrap <= 0:
            wrap = self.ctx.pdf.page(b.page).rect.width - b.bbox.x0
        return max(wrap, b.bbox.width) + slack

    def _box_left(self, width_pt: float) -> float | None:
        """Left edge (pt) of the editor for centred/right-aligned text, or
        None for left-aligned text (positioned by its first glyph)."""
        b, lay = self.block, self._layout
        if lay is None or lay.align == "left":
            return None
        if self.rotated():  # along the text, relative to the first line's origin
            start, end = self._text_extent()
            if lay.align == "center":
                return (start + end) / 2 - width_pt / 2
            return end - width_pt + 0.75
        if lay.align == "center":
            return (b.bbox.x0 + b.bbox.x1) / 2 - width_pt / 2
        # right-aligned: the box's right edge is the text's right edge (Qt
        # keeps ~1 px for the caret there)
        return b.bbox.x1 - width_pt + 0.75

    def _reposition(self, *_a) -> None:
        if self.editor is None or self.block is None:
            return
        if self._proxy is not None:
            self._reposition_rotated()
            return
        view, b = self.ctx.view, self.block
        ppp = self._px_per_pt()
        style = self.current_style()
        first = b.lines[0] if b.lines else None
        fm = QFontMetricsF(self.editor.font())
        if first is not None:
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
        left_pt = self._box_left(self.editor.lineWrapColumnOrWidth() / ppp)
        if left_pt is not None:  # centred / right-aligned: Qt aligns inside the box
            x = view.mapFromScene(view.page_to_scene(b.page, QPointF(left_pt, origin.y()))).x()
        w = self.editor.lineWrapColumnOrWidth() + 2
        h = max(line_px, self.editor.content_height()) + 2
        self.editor.setGeometry(int(round(x)), int(round(y)), int(w), int(round(h)))
        frame_vp = None
        if self._frame is not None:
            # grow the frame with the text (pt)
            hh = max(b.bbox.height, h / ppp)
            ww = max(b.bbox.width, (w - 2) / ppp)
            rect = QRectF(b.bbox.x0 - EDIT_PAD, b.bbox.y0 - EDIT_PAD, ww + 2 * EDIT_PAD, hh + 2 * EDIT_PAD)
            self._frame.setRect(rect)
            frame_vp = self._viewport_rect(self._frame)
        self._place_keys(frame_vp)

    def _reposition_rotated(self) -> None:
        """Rotated text: size the editor in screen pixels and map it onto the
        page with a transform (rotation + 1/zoom scale) whose anchor is the
        first line's baseline origin."""
        b, ed = self.block, self.editor
        ppp = self._px_per_pt()
        fm = QFontMetricsF(ed.font())
        w = ed.lineWrapColumnOrWidth() + 2
        h = max(ed._line_px or fm.height(), ed.content_height()) + 2
        ed.resize(int(w), int(round(h)))
        self._proxy.resize(ed.size())
        bx, by = ed.first_baseline()
        left = self._box_left(ed.lineWrapColumnOrWidth() / ppp)
        ax = bx if left is None else -left * ppp     # widget x that lies on the first origin
        ox, oy = b.lines[0].origin if b.lines else (b.bbox.x0, b.bbox.y0)
        t = QTransform()
        t.translate(ox, oy)
        t.rotate(b.angle)
        t.scale(1.0 / ppp, 1.0 / ppp)
        t.translate(-ax, -by)
        self._proxy.setTransform(t)
        if self._frame is not None:
            # the frame lives in the editor's own (rotated) pixel space too;
            # it encloses the editor and the original lines (their extent
            # along the text, ascender to descender across it)
            u, v = _unit(b)
            inv_x, inv_y = [], []
            for ln in b.lines:
                du = (ln.origin[0] - ox) * u[0] + (ln.origin[1] - oy) * u[1]
                dv = (ln.origin[0] - ox) * v[0] + (ln.origin[1] - oy) * v[1]
                size = ln.style.size
                inv_x += [ax + du * ppp, ax + (du + ln.extent[1] - ln.extent[0]) * ppp]
                inv_y += [by + (dv - ln.ascender * size) * ppp, by + (dv - ln.descender * size) * ppp]
            pad = EDIT_PAD * ppp
            rect = QRectF(min([0.0] + inv_x) - pad, min([0.0] + inv_y) - pad, 0, 0)
            rect.setRight(max([w - 2] + inv_x) + pad)
            rect.setBottom(max([h - 2] + inv_y) + pad)
            self._frame.setRect(rect)
            self._frame.setTransform(t)
        self._place_keys(self._viewport_rect(self._frame) if self._frame is not None else None)

    def _viewport_rect(self, item) -> QRectF:
        view = self.ctx.view
        poly = view.mapFromScene(item.mapToScene(item.rect()))
        return QRectF(poly.boundingRect())

    def _place_keys(self, frame: QRectF | None) -> None:
        """Put the key-hint chip next to the edit frame where it covers no
        other text: above or below the frame, right- or left-aligned (the
        corners next to a paragraph are usually free – short last lines,
        paragraph spacing). If every spot covers text, the least covered one
        wins and the chip gives way once the user types."""
        chip = self._keys
        if chip is None or frame is None:
            return
        chip.adjustSize()
        w, h = chip.width(), chip.height()
        view, b = self.ctx.view, self.block
        area = QRectF(view.viewport().rect()).adjusted(4, 4, -4, -4)
        candidates = [
            QRectF(frame.right() - w, frame.top() - KEYS_GAP - h, w, h),    # above, right
            QRectF(frame.right() - w, frame.bottom() + KEYS_GAP, w, h),     # below, right
            QRectF(frame.left(), frame.top() - KEYS_GAP - h, w, h),         # above, left
            QRectF(frame.left(), frame.bottom() + KEYS_GAP, w, h),          # below, left
            QRectF(frame.right() + KEYS_GAP, frame.top(), w, h),            # beside, right
            QRectF(frame.left() - KEYS_GAP - w, frame.top(), w, h),         # beside, left
        ]
        others = []
        for o in self.blocks(b.page):
            if tuple(o.bbox) == tuple(b.bbox):
                continue
            tl = view.page_to_viewport(b.page, QPointF(o.bbox.x0, o.bbox.y0))
            br = view.page_to_viewport(b.page, QPointF(o.bbox.x1, o.bbox.y1))
            others.append(QRectF(tl, br).normalized().adjusted(-2, -2, 2, 2))

        def cost(r: QRectF) -> float:
            covered = sum((r & o).width() * (r & o).height() for o in others if r.intersects(o))
            outside = 0.0 if area.contains(r) else 1e7
            return covered + outside

        costs = [cost(r) for r in candidates]
        best = min(range(len(candidates)), key=lambda i: (costs[i], i))
        r = candidates[best]
        r.moveLeft(max(area.left(), min(r.left(), area.right() - w)))
        chip.move(int(round(r.left())), int(round(r.top())))
        self._keys_covers_text = costs[best] > 0

    def _teardown(self) -> None:
        page = self.block.page if self.block is not None else None
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
        if self._frame is not None and self.ctx is not None:
            self.ctx.view.remove_overlay(self._frame)
        self.editor = self._keys = self._frame = self._proxy = None
        self._proxy_drag = False
        self.block = None
        self.style_override = None
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

    def commit(self) -> bool:
        """Write the editor's text into the PDF. Returns True if it changed."""
        if self.editor is None or self.block is None or self._committing:
            return False
        block, text, style = self.block, self.editor.toPlainText(), self.style_override
        shown, soft = self._shown_text, self._soft_positions
        self._teardown()
        if text == shown and style is None:
            return False
        text = self._restore_soft_breaks(shown, soft, text)
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

    def commit_and_next(self, step: int = 1) -> None:
        """Save, then open the next (Tab) or previous (Shift+Tab) paragraph
        in reading order."""
        if self.block is None:
            return
        page, old = self.block.page, self.blocks(self.block.page)
        idx = next((i for i, b in enumerate(old) if b.bbox == self.block.bbox), -1)
        j = idx + step
        nxt = old[j] if idx >= 0 and 0 <= j < len(old) else None
        anchor = QPointF((nxt.bbox.x0 + nxt.bbox.x1) / 2, (nxt.bbox.y0 + nxt.bbox.y1) / 2) if nxt else None
        self.commit()
        target = None
        if anchor is not None:
            target = self.block_at(page, anchor)
        else:  # continue on the next / previous page that has text
            pages = range(page + 1, self.ctx.pdf.page_count) if step > 0 else range(page - 1, -1, -1)
            for p in pages:
                if self.blocks(p):
                    target = self.blocks(p)[0] if step > 0 else self.blocks(p)[-1]
                    break
        if target is not None:
            self.ctx.view.go_to_page(target.page) if target.page != page else None
            self.start_edit(target)

    # -- hooks for subclasses ---------------------------------------------------
    def set_align(self, align: str) -> None:
        """Alignment for new text (only "Tilføj tekst" supports it)."""
