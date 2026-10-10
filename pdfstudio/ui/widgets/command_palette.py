"""Command palette (Ctrl+K): fuzzy search over every registered action."""

from __future__ import annotations

from PySide6.QtCore import QAbstractListModel, QEvent, QModelIndex, QRectF, QSize, Qt, Signal
from PySide6.QtGui import QFont, QFontMetrics, QPainter, QPainterPath, QPen
from PySide6.QtWidgets import (
    QAbstractItemView,
    QFrame,
    QLineEdit,
    QListView,
    QStyledItemDelegate,
    QVBoxLayout,
    QWidget,
)

from ...i18n import tr
from ..theme import tokens as T
from ..theme.icons import icon
from .util import ThemedIconLabel, add_shadow, divider, hbox, label


def fuzzy_score(query: str, text: str) -> tuple[float, list[int]] | None:
    """Subsequence match with bonuses for word starts and consecutive runs.

    Returns ``(score, matched_positions)`` or ``None`` if not all characters
    of ``query`` occur in order.
    """
    q = query.casefold().replace(" ", "")
    t = text.casefold()
    if not q:
        return 0.0, []
    # exact substring gets a big boost
    sub = t.find(query.casefold().strip())
    positions: list[int] = []
    score = 0.0
    ti = 0
    prev = -2
    for ch in q:
        found = t.find(ch, ti)
        if found < 0:
            return None
        bonus = 1.0
        if found == 0 or not t[found - 1].isalnum():
            bonus += 2.0
        if found == prev + 1:
            bonus += 1.5
        bonus -= min(found - ti, 8) * 0.12
        score += bonus
        positions.append(found)
        prev = found
        ti = found + 1
    if sub >= 0:
        score += 6.0 + (3.0 if sub == 0 or not t[sub - 1].isalnum() else 0.0)
        positions = list(range(sub, sub + len(query.strip())))
    score -= len(t) * 0.01
    return score, positions


class PaletteModel(QAbstractListModel):
    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.rows: list[tuple] = []   # (entry, positions)

    def rowCount(self, parent=QModelIndex()) -> int:  # noqa: N802
        return 0 if parent.isValid() else len(self.rows)

    def data(self, index, role=Qt.ItemDataRole.DisplayRole):
        if not index.isValid():
            return None
        entry, pos = self.rows[index.row()]
        if role == Qt.ItemDataRole.DisplayRole:
            return entry.text
        if role == Qt.ItemDataRole.UserRole:
            return entry
        if role == Qt.ItemDataRole.UserRole + 1:
            return pos
        return None

    def set_rows(self, rows) -> None:
        self.beginResetModel()
        self.rows = rows
        self.endResetModel()


class PaletteDelegate(QStyledItemDelegate):
    def sizeHint(self, option, index) -> QSize:  # noqa: N802
        return QSize(400, 40)

    def paint(self, p: QPainter, option, index) -> None:
        from PySide6.QtWidgets import QStyle

        p.save()
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        entry = index.data(Qt.ItemDataRole.UserRole)
        positions = set(index.data(Qt.ItemDataRole.UserRole + 1) or [])
        r = QRectF(option.rect).adjusted(6, 1, -6, -1)
        enabled = entry.action.isEnabled()
        if option.state & QStyle.StateFlag.State_Selected:
            path = QPainterPath()
            path.addRoundedRect(r, 8, 8)
            p.fillPath(path, T.qcolor("selected"))
        x = r.left() + 12
        ic = icon(entry.icon or "arrow-right", "accent_text" if option.state & QStyle.StateFlag.State_Selected else "text_muted")
        ic.paint(p, int(x), int(r.center().y() - 9), 18, 18,
                 mode=ic.Mode.Normal if enabled else ic.Mode.Disabled)
        x += 30
        # right side: shortcut chips and category
        base = QFont(option.font)
        base.setPixelSize(T.FONT_SIZE)
        small = QFont(option.font)
        small.setPixelSize(T.FONT_SIZE_XS)
        right = r.right() - 10
        sc = entry.shortcut_text
        if sc:
            p.setFont(small)
            fm = QFontMetrics(small)
            keys = sc.split("+") if sc != "+" else ["+"]
            for key in reversed(keys):
                kw = fm.horizontalAdvance(key) + 12
                kr = QRectF(right - kw, r.center().y() - 10, kw, 20)
                path = QPainterPath()
                path.addRoundedRect(kr, 5, 5)
                p.fillPath(path, T.qcolor("hover"))
                p.setPen(QPen(T.qcolor("border_strong"), 1))
                p.drawPath(path)
                p.setPen(T.qcolor("text_muted"))
                p.drawText(kr, Qt.AlignmentFlag.AlignCenter, key)
                right = kr.left() - 4
            right -= 8
        if entry.category:
            p.setFont(small)
            fm = QFontMetrics(small)
            cw = fm.horizontalAdvance(entry.category)
            p.setPen(T.qcolor("text_subtle"))
            p.drawText(QRectF(right - cw, r.top(), cw, r.height()), Qt.AlignmentFlag.AlignVCenter, entry.category)
            right -= cw + 12
        # title with highlighted matches
        bold = QFont(base)
        bold.setWeight(QFont.Weight.Bold)
        fm_n, fm_b = QFontMetrics(base), QFontMetrics(bold)
        text = entry.text
        normal_color = T.qcolor("text" if enabled else "text_subtle")
        for i, ch in enumerate(text):
            hit = i in positions
            f, fm = (bold, fm_b) if hit else (base, fm_n)
            w = fm.horizontalAdvance(ch)
            if x + w > right:
                p.setFont(base)
                p.setPen(normal_color)
                p.drawText(QRectF(x, r.top(), 20, r.height()), Qt.AlignmentFlag.AlignVCenter, "…")
                break
            p.setFont(f)
            p.setPen(T.qcolor("accent_text") if hit and enabled else normal_color)
            p.drawText(QRectF(x, r.top(), w + 2, r.height()), Qt.AlignmentFlag.AlignVCenter, ch)
            x += w
        p.restore()


class CommandPalette(QWidget):
    """Overlay covering ``host``: scrim + floating card."""

    closed = Signal()

    def __init__(self, host: QWidget, registry) -> None:
        super().__init__(host)
        self.host = host
        self.registry = registry
        self._mru: list[str] = []
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, False)
        self.hide()

        self.card = QFrame(self)
        self.card.setObjectName("Palette")
        add_shadow(self.card, blur=48, y=16)
        v = QVBoxLayout(self.card)
        v.setContentsMargins(8, 4, 8, 8)
        v.setSpacing(4)
        self.field = QLineEdit()
        self.field.setProperty("variant", "palette")
        self.field.setPlaceholderText(tr("shell.palette.placeholder"))
        self.field.textChanged.connect(self._filter)
        self.field.installEventFilter(self)
        head = hbox(ThemedIconLabel("magnifying-glass", 18, "text_subtle"), self.field,
                    margins=(10, 0, 6, 0), spacing=6)
        v.addLayout(head)
        v.addWidget(divider("h"))
        self.model = PaletteModel(self)
        self.list = QListView()
        self.list.setModel(self.model)
        self.list.setItemDelegate(PaletteDelegate(self.list))
        self.list.setUniformItemSizes(True)
        self.list.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.list.setVerticalScrollMode(QAbstractItemView.ScrollMode.ScrollPerPixel)
        self.list.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.list.setStyleSheet("QListView::item, QListView::item:selected, QListView::item:hover"
                                " { background: transparent; padding: 0px; }")
        self.list.clicked.connect(self._run_index)
        v.addSpacing(2)
        v.addWidget(self.list)
        self.footer = label(tr("shell.palette.footer"), "caption")
        self.footer.setContentsMargins(12, 2, 12, 2)
        v.addWidget(self.footer)
        host.installEventFilter(self)

    # -- show / hide -----------------------------------------------------
    def open(self) -> None:
        self.setGeometry(self.host.rect())
        self.field.clear()
        self._filter("")
        self._layout_card()
        self.show()
        self.raise_()
        self.field.setFocus(Qt.FocusReason.ShortcutFocusReason)

    def close_palette(self) -> None:
        if self.isVisible():
            self.hide()
            self.closed.emit()

    def _layout_card(self) -> None:
        w = min(620, max(360, self.width() - 80))
        rows = min(9, max(1, self.model.rowCount()))
        list_h = rows * 40 + 8
        self.list.setFixedHeight(list_h)
        self.card.setFixedWidth(w)
        self.card.adjustSize()
        self.card.move((self.width() - w) // 2, max(40, int(self.height() * 0.14)))

    def paintEvent(self, _ev) -> None:  # noqa: N802
        p = QPainter(self)
        p.fillRect(self.rect(), T.qcolor("scrim"))
        p.end()

    def mousePressEvent(self, ev) -> None:  # noqa: N802
        if not self.card.geometry().contains(ev.position().toPoint()):
            self.close_palette()

    def eventFilter(self, obj, ev) -> bool:  # noqa: N802
        if obj is self.host and ev.type() == QEvent.Type.Resize and self.isVisible():
            self.setGeometry(self.host.rect())
            self._layout_card()
        if obj is self.field and ev.type() == QEvent.Type.KeyPress:
            key = ev.key()
            if key == Qt.Key.Key_Escape:
                self.close_palette()
                return True
            if key in (Qt.Key.Key_Down, Qt.Key.Key_Up, Qt.Key.Key_PageDown, Qt.Key.Key_PageUp):
                step = {Qt.Key.Key_Down: 1, Qt.Key.Key_Up: -1, Qt.Key.Key_PageDown: 8, Qt.Key.Key_PageUp: -8}[key]
                self._move(step)
                return True
            if key in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
                self._run_index(self.list.currentIndex())
                return True
        return False

    # -- filtering -------------------------------------------------------
    def entries(self):
        return [e for e in self.registry.all() if e.palette and e.text]

    def _filter(self, text: str) -> None:
        text = text.strip()
        scored = []
        for e in self.entries():
            hay = e.text
            res = fuzzy_score(text, hay)
            extra = None
            if res is None and text:
                extra = fuzzy_score(text, f"{e.category} {e.keywords} {e.id}")
                if extra is None:
                    continue
                res = (extra[0] - 4.0, [])
            score, pos = res
            if not text and e.id in self._mru:
                score += 100 - self._mru.index(e.id)
            if not e.action.isEnabled():
                score -= 50
            scored.append((score, e, pos))
        if text:
            scored.sort(key=lambda s: -s[0])
        else:
            scored.sort(key=lambda s: (-s[0] if s[0] > 0 else 0, s[1].category, s[1].order, s[1].text))
        self.model.set_rows([(e, pos) for _s, e, pos in scored])
        if self.model.rowCount():
            self.list.setCurrentIndex(self.model.index(0))
        if self.isVisible():
            self._layout_card()

    def _move(self, step: int) -> None:
        n = self.model.rowCount()
        if not n:
            return
        cur = self.list.currentIndex().row()
        nxt = max(0, min(n - 1, cur + step)) if abs(step) > 1 else (cur + step) % n
        self.list.setCurrentIndex(self.model.index(nxt))

    def _run_index(self, index: QModelIndex) -> None:
        if not index.isValid():
            return
        entry = index.data(Qt.ItemDataRole.UserRole)
        if not entry.action.isEnabled():
            return
        self.close_palette()
        if entry.id in self._mru:
            self._mru.remove(entry.id)
        self._mru.insert(0, entry.id)
        del self._mru[8:]
        self.registry.trigger(entry.id)
