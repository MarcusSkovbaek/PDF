"""Search panel (Ctrl+F): incremental full-text search with a results list.

Uses ``pdfstudio.core.search.search`` lazily (time-sliced on the GUI thread so
big documents stay responsive); hits are highlighted in the PageView.
"""

from __future__ import annotations

import time

from PySide6.QtCore import QAbstractListModel, QModelIndex, QRectF, QSize, Qt, QTimer, Signal
from PySide6.QtGui import QFont, QFontMetrics, QPainter, QPainterPath
from PySide6.QtWidgets import (
    QAbstractItemView,
    QLabel,
    QLineEdit,
    QListView,
    QStackedWidget,
    QStyle,
    QStyledItemDelegate,
    QVBoxLayout,
    QWidget,
)

from ...i18n import tr
from ..theme import tokens as T
from ..theme.icons import icon
from ..widgets.util import IconButton, ThemedIconLabel, hbox, label, on_theme_change

try:
    from ...core.search import search as core_search
except Exception:  # pragma: no cover - core module not available yet
    core_search = None


class HitModel(QAbstractListModel):
    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.hits: list = []
        self.query = ""
        self.current = -1

    def rowCount(self, parent=QModelIndex()) -> int:  # noqa: N802
        return 0 if parent.isValid() else len(self.hits)

    def data(self, index, role=Qt.ItemDataRole.DisplayRole):
        if not index.isValid():
            return None
        hit = self.hits[index.row()]
        if role == Qt.ItemDataRole.DisplayRole:
            return hit.context
        if role == Qt.ItemDataRole.UserRole:
            return hit
        return None

    def clear(self) -> None:
        self.beginResetModel()
        self.hits = []
        self.current = -1
        self.endResetModel()

    def extend(self, hits) -> None:
        if not hits:
            return
        n = len(self.hits)
        self.beginInsertRows(QModelIndex(), n, n + len(hits) - 1)
        self.hits.extend(hits)
        self.endInsertRows()


class HitDelegate(QStyledItemDelegate):
    def sizeHint(self, option, index) -> QSize:  # noqa: N802
        return QSize(200, 56)

    def paint(self, p: QPainter, option, index) -> None:
        p.save()
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        model: HitModel = index.model()
        hit = index.data(Qt.ItemDataRole.UserRole)
        r = QRectF(option.rect).adjusted(6, 2, -6, -2)
        is_cur = index.row() == model.current
        hovered = bool(option.state & QStyle.StateFlag.State_MouseOver)
        if is_cur or hovered:
            path = QPainterPath()
            path.addRoundedRect(r, 8, 8)
            p.fillPath(path, T.qcolor("selected" if is_cur else "hover"))
        small = QFont(option.font)
        small.setPixelSize(T.FONT_SIZE_XS)
        small.setWeight(QFont.Weight.DemiBold)
        p.setFont(small)
        p.setPen(T.qcolor("accent_text" if is_cur else "text_subtle"))
        p.drawText(r.adjusted(10, 6, -10, 0), Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop,
                   tr("shell.search.page_n", n=hit.page + 1))
        # context with the match emphasised
        base = QFont(option.font)
        base.setPixelSize(T.FONT_SIZE_SMALL)
        bold = QFont(base)
        bold.setWeight(QFont.Weight.Bold)
        ctx = " ".join((hit.context or "").split())
        q = " ".join(model.query.split())
        pos = ctx.casefold().find(q.casefold()) if q else -1
        parts = [(ctx, False)] if pos < 0 else [(ctx[:pos], False), (ctx[pos:pos + len(q)], True), (ctx[pos + len(q):], False)]
        x = r.left() + 10
        y = r.top() + 24
        avail = r.right() - 10
        fm_b, fm_n = QFontMetrics(bold), QFontMetrics(base)
        # make sure the match is visible: trim leading text if too long
        if pos >= 0:
            lead = parts[0][0]
            match_w = fm_b.horizontalAdvance(parts[1][0])
            while lead and fm_n.horizontalAdvance("…" + lead) + match_w > (avail - x) * 0.7:
                lead = lead[1:]
            if lead != parts[0][0]:
                parts[0] = ("…" + lead.lstrip(), False)
        for text, strong in parts:
            if not text or x >= avail:
                continue
            f = bold if strong else base
            fm = fm_b if strong else fm_n
            p.setFont(f)
            p.setPen(T.qcolor("text") if strong else T.qcolor("text_muted"))
            el = fm.elidedText(text, Qt.TextElideMode.ElideRight, int(avail - x))
            p.drawText(QRectF(x, y, avail - x, 20), Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter, el)
            x += fm.horizontalAdvance(el)
        p.restore()


class SearchPanel(QWidget):
    hitsChanged = Signal()

    def __init__(self, workspace, parent=None) -> None:
        super().__init__(parent)
        self.workspace = workspace
        self._iter = None
        self._query = ""
        self._done = True
        self._pending_hits: list = []

        lay = QVBoxLayout(self)
        lay.setContentsMargins(12, 8, 12, 8)
        lay.setSpacing(8)

        self.field = QLineEdit()
        self.field.setPlaceholderText(tr("shell.search.placeholder"))
        self.field.setClearButtonEnabled(True)
        self._lead = self.field.addAction(icon("magnifying-glass", "text_subtle"), QLineEdit.ActionPosition.LeadingPosition)
        on_theme_change(self, lambda: self._lead.setIcon(icon("magnifying-glass", "text_subtle")))
        self.field.textChanged.connect(self._on_text)
        self.field.returnPressed.connect(self._on_return)
        self.field.installEventFilter(self)
        lay.addWidget(self.field)

        self.btn_case = IconButton("text-aa", tr("shell.search.case"), variant="icon-sm", checkable=True, icon_size=15)
        self.btn_words = IconButton("text-t", tr("shell.search.words"),
                                    variant="icon-sm", checkable=True, icon_size=15)
        self.btn_case.toggled.connect(lambda _: self.restart())
        self.btn_words.toggled.connect(lambda _: self.restart())
        self.count = label("", "caption")
        self.btn_prev = IconButton("caret-up", tr("shell.search.prev"), variant="icon-sm", icon_size=14, shortcut="Shift+Enter")
        self.btn_next = IconButton("caret-down", tr("shell.search.next"), variant="icon-sm", icon_size=14, shortcut="Enter")
        self.btn_prev.clicked.connect(self.prev_hit)
        self.btn_next.clicked.connect(self.next_hit)
        lay.addLayout(hbox(self.btn_case, self.btn_words, None, self.count, 4, self.btn_prev, self.btn_next, spacing=2))

        self.model = HitModel(self)
        self.list = QListView()
        self.list.setModel(self.model)
        self.list.setItemDelegate(HitDelegate(self.list))
        self.list.setMouseTracking(True)
        self.list.setUniformItemSizes(True)
        self.list.setVerticalScrollMode(QAbstractItemView.ScrollMode.ScrollPerPixel)
        self.list.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.list.setStyleSheet("QListView::item, QListView::item:hover, QListView::item:selected"
                                " { background: transparent; padding: 0px; }")
        self.list.clicked.connect(lambda idx: self.set_current(idx.row()))

        self.empty = QWidget()
        self.empty_icon = ThemedIconLabel("magnifying-glass", 28, "text_subtle")
        self.empty_title = label(tr("shell.search.empty_title"), "heading")
        self.empty_text = label(tr("shell.search.empty_text"), "caption", wrap=True)
        self.empty_title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.empty_text.setAlignment(Qt.AlignmentFlag.AlignCenter)
        ev = QVBoxLayout(self.empty)
        ev.setContentsMargins(16, 40, 16, 16)
        ev.setSpacing(6)
        ev.addWidget(self.empty_icon, 0, Qt.AlignmentFlag.AlignHCenter)
        ev.addSpacing(4)
        ev.addWidget(self.empty_title)
        ev.addWidget(self.empty_text)
        ev.addStretch(1)

        self.stack = QStackedWidget()
        self.stack.addWidget(self.empty)
        self.stack.addWidget(self.list)
        lay.addWidget(self.stack, 1)

        self._debounce = QTimer(self)
        self._debounce.setSingleShot(True)
        self._debounce.setInterval(220)
        self._debounce.timeout.connect(self.restart)
        self._pump = QTimer(self)
        self._pump.setSingleShot(True)
        self._pump.setInterval(0)
        self._pump.timeout.connect(self._step)
        self._update_state()

    # -- public ------------------------------------------------------------
    def focus_field(self) -> None:
        self.field.setFocus(Qt.FocusReason.ShortcutFocusReason)
        self.field.selectAll()

    def set_query(self, text: str) -> None:
        self.field.setText(text)
        self.restart()

    def hits(self) -> list:
        return list(self.model.hits)

    def is_running(self) -> bool:
        return not self._done

    def finish_now(self) -> None:
        """Run the pending search to completion synchronously (tests)."""
        while not self._done:
            self._step(budget=10.0)

    def document_changed(self) -> None:
        if self._query:
            self._debounce.start()

    # -- search driving ---------------------------------------------------------
    def _on_text(self, _t: str) -> None:
        self._debounce.start()

    def eventFilter(self, obj, ev) -> bool:  # noqa: N802
        from PySide6.QtCore import QEvent

        if obj is self.field and ev.type() == QEvent.Type.KeyPress:
            if ev.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter) and ev.modifiers() & Qt.KeyboardModifier.ShiftModifier:
                self.prev_hit()
                return True
            if ev.key() == Qt.Key.Key_Escape:
                if self.field.text():
                    self.field.clear()
                else:
                    self.workspace.view.setFocus()
                return True
        return False

    def _on_return(self) -> None:
        if self._debounce.isActive() or self.field.text().strip() != self._query:
            self._debounce.stop()
            self.restart()
            self._step(budget=0.15)
        self.next_hit()

    def restart(self) -> None:
        self._debounce.stop()
        self._query = self.field.text().strip()
        self.model.clear()
        self.model.query = self._query
        self.workspace.view.set_search_hits([])
        self._iter = None
        self._done = True
        if self._query and core_search is not None:
            self._iter = core_search(self.workspace.pdf, self._query,
                                     case_sensitive=self.btn_case.isChecked(),
                                     whole_words=self.btn_words.isChecked())
            self._done = False
            self._pump.start()
        self._update_state()

    def _step(self, budget: float = 0.025) -> None:
        if self._iter is None:
            self._done = True
            return
        deadline = time.perf_counter() + budget
        new = []
        try:
            while time.perf_counter() < deadline:
                new.append(next(self._iter))
        except StopIteration:
            self._done = True
            self._iter = None
        except Exception:
            self._done = True
            self._iter = None
        if new:
            self.model.extend(new)
            self.workspace.view.append_search_hits(new)
            if self.model.current < 0:
                self.set_current(0, scroll=True)
        if not self._done:
            self._pump.start()
        self._update_state()
        self.hitsChanged.emit()

    # -- navigation ---------------------------------------------------------------
    def set_current(self, row: int, scroll: bool = True) -> None:
        n = len(self.model.hits)
        if n == 0:
            return
        row %= n
        old = self.model.current
        self.model.current = row
        for r in (old, row):
            if 0 <= r < n:
                idx = self.model.index(r)
                self.model.dataChanged.emit(idx, idx)
        self.list.scrollTo(self.model.index(row))
        self.workspace.view.set_current_hit(row, scroll=scroll)
        self._update_state()

    def next_hit(self) -> None:
        if self.model.hits:
            self.set_current(self.model.current + 1)

    def prev_hit(self) -> None:
        if self.model.hits:
            self.set_current(self.model.current - 1)

    def clear(self) -> None:
        self.field.clear()
        self.restart()

    def _update_state(self) -> None:
        n = len(self.model.hits)
        if not self._query:
            self.count.setText("")
            self.empty_icon.set_icon("magnifying-glass")
            self.empty_title.setText(tr("shell.search.empty_title"))
            self.empty_text.setText(tr("shell.search.empty_text"))
            self.stack.setCurrentWidget(self.empty)
        elif n == 0 and self._done:
            self.count.setText(tr("shell.search.none_short"))
            self.empty_icon.set_icon("magnifying-glass-minus")
            self.empty_title.setText(tr("shell.search.none_title"))
            self.empty_text.setText(tr("shell.search.none_text", q=self._query))
            self.stack.setCurrentWidget(self.empty)
        else:
            cur = self.model.current + 1 if self.model.current >= 0 else 0
            more = "" if self._done else "+"
            self.count.setText(tr("shell.search.count", cur=cur, n=f"{n}{more}"))
            self.stack.setCurrentWidget(self.list)
        has = n > 0
        self.btn_prev.setEnabled(has)
        self.btn_next.setEnabled(has)
