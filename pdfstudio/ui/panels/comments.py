"""The comment list in the right panel (Acrobat style).

One card per comment thread, grouped by page: author avatar, name and time,
the marked text as a quote, the comment itself (click to edit in place),
replies, review status and a "resolved" check mark. On top: a type filter,
search, a filter menu (status, author, current page) and sorting; F8 /
Shift+F8 (and the arrows next to the count) step through the comments.

Cards and the page stay in sync both ways: selecting a card shows (and, in
"Kommentér" mode, selects) its annotation; selecting annotations on the
page selects their cards. All changes go through
:mod:`pdfstudio.core.comments` (one undo step each). After a change only the
changed pages are re-read, and existing cards are updated in place so an
open editor keeps its focus.
"""

from __future__ import annotations

from datetime import datetime

from PySide6.QtCore import QEvent, QRectF, QSize, Qt, QTimer, QVariantAnimation, Signal
from PySide6.QtGui import QAction, QActionGroup, QColor, QFont, QGuiApplication, QKeyEvent, QPainter, QPen
from PySide6.QtWidgets import (
    QApplication,
    QFrame,
    QGraphicsRectItem,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMenu,
    QPlainTextEdit,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

from ...core import CONTENT, STRUCTURE
from ...core import annotations as core_annots
from ...core import comments as core_comments
from ...i18n import tr
from ..theme import tokens as T
from ..widgets.color_button import to_qcolor
from ..widgets.segmented import Segment, SegmentedControl
from ..widgets.util import IconButton, ThemedIconLabel, label, on_theme_change, set_prop

MARKUP = ("highlight", "underline", "strikeout", "squiggly", "replace", "insert")
QUOTE_MAX = 240

#: Type filter key -> comment kinds it shows (``None``: everything).
FILTERS: dict[str, frozenset[str] | None] = {
    "all": None,
    "markup": frozenset(MARKUP),
    "notes": frozenset({"note"}),
    "drawings": frozenset({"ink", "rect", "ellipse", "line", "arrow", "polygon", "polyline"}),
    "text": frozenset({"freetext"}),
}
FILTER_ICONS = {"markup": "highlighter-circle", "notes": "note", "drawings": "scribble-loop", "text": "text-aa"}
STATUS_FILTERS = ("all", "open", "resolved")
SORTS = ("page", "newest", "author")
STATUS_CHIP = {"accepted": "success", "rejected": "danger", "cancelled": "muted", "completed": "accent"}

Key = tuple[int, int]


def matches(kind: str, filter_key: str) -> bool:
    kinds = FILTERS.get(filter_key)
    return kinds is None or kind in kinds


# -- small helpers ---------------------------------------------------------------------
def format_when(dt: datetime | None, now: datetime | None = None) -> str:
    """Danish relative time: »Lige nu«, »for 5 min. siden«, »i dag 14.03«,
    »i går 09.12«, »12. okt. 14.03«, »12. okt. 2024«."""
    if dt is None:
        return ""
    now = (now or datetime.now()).astimezone()
    dt = dt.astimezone(now.tzinfo)
    secs = (now - dt).total_seconds()
    if -60 < secs < 60:
        return tr("comments.when.now")
    if 0 < secs < 3600:
        return tr("comments.when.minutes", n=int(secs // 60))
    hm = f"{dt.hour:02d}.{dt.minute:02d}"
    days = (now.date() - dt.date()).days
    if days == 0:
        return tr("comments.when.today", time=hm)
    if days == 1:
        return tr("comments.when.yesterday", time=hm)
    month = tr(f"comments.month.{dt.month}")
    if dt.year == now.year:
        return tr("comments.when.date", day=dt.day, month=month, time=hm)
    return tr("comments.when.date_year", day=dt.day, month=month, year=dt.year)


def initials(name: str) -> str:
    parts = [p for p in (name or "").replace(".", " ").replace("_", " ").split() if p]
    if not parts:
        return "?"
    if len(parts) == 1:
        return parts[0][:2].upper() if len(parts[0]) > 1 and not parts[0][1].isupper() else parts[0][:1].upper()
    return (parts[0][0] + parts[-1][0]).upper()


class Avatar(QWidget):
    """Initials in a circle coloured by the author's name."""

    def __init__(self, name: str, size: int = 26, parent=None) -> None:
        super().__init__(parent)
        self._name = name
        self.setFixedSize(size, size)
        on_theme_change(self, self.update)

    def set_name(self, name: str) -> None:
        if name != self._name:
            self._name = name
            self.update()

    def paintEvent(self, _ev) -> None:  # noqa: N802
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(T.avatar_color(self._name))
        r = QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5)
        p.drawEllipse(r)
        f = QFont(self.font())
        f.setPixelSize(max(8, int(self.height() * 0.4)))
        f.setWeight(QFont.Weight.DemiBold)
        p.setFont(f)
        p.setPen(T.qcolor("text_on_accent"))
        p.drawText(r, Qt.AlignmentFlag.AlignCenter, initials(self._name))
        p.end()


class _HfwMixin:
    """Wrapped labels: tell the parent layout when our height-for-width
    changed. Qt caches it per layout item and only refreshes the cache on
    updateGeometry(), so a card whose text, replies or editor changed would
    otherwise keep its old height and clip its content."""

    def event(self, ev) -> bool:  # noqa: D401
        result = super().event(ev)  # type: ignore[misc]
        if ev.type() == QEvent.Type.LayoutRequest:
            self.updateGeometry()  # type: ignore[attr-defined]
        return result


class _Box(_HfwMixin, QWidget):
    """Plain container that keeps its parent's layout up to date."""


class AutoTextEdit(QPlainTextEdit):
    """Plain text editor that grows with its text; Ctrl+Enter commits, Esc
    cancels."""

    commit = Signal()
    cancel = Signal()

    def __init__(self, placeholder: str = "", parent=None, *, min_lines: int = 2, max_lines: int = 12) -> None:
        super().__init__(parent)
        self.setPlaceholderText(placeholder)
        self.setTabChangesFocus(True)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self._min, self._max = min_lines, max_lines
        self.document().contentsChanged.connect(self._fit)
        self._fit()

    def _fit(self) -> None:
        fm = self.fontMetrics()
        lines = max(self._min, min(self._max, int(self.document().size().height()) or 1))
        h = int(lines * fm.lineSpacing() + 2 * self.document().documentMargin() + 6)
        self.setFixedHeight(h)
        self.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded if lines >= self._max
                                        else Qt.ScrollBarPolicy.ScrollBarAlwaysOff)

    def resizeEvent(self, ev) -> None:  # noqa: N802
        super().resizeEvent(ev)
        self._fit()

    def keyPressEvent(self, ev: QKeyEvent) -> None:  # noqa: N802
        if ev.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter) and ev.modifiers() & Qt.KeyboardModifier.ControlModifier:
            self.commit.emit()
            return
        if ev.key() == Qt.Key.Key_Escape:
            self.cancel.emit()
            return
        super().keyPressEvent(ev)


class _ClickLabel(QLabel):
    clicked = Signal()

    def mousePressEvent(self, ev) -> None:  # noqa: N802
        if ev.button() == Qt.MouseButton.LeftButton:
            self.clicked.emit()
            ev.accept()
            return
        super().mousePressEvent(ev)


class _Editor(_HfwMixin, QWidget):
    """Editor + Annullér / Gem buttons; commits when focus leaves it."""

    committed = Signal(str)
    cancelled = Signal()

    def __init__(self, placeholder: str, ok_text: str, parent=None, *, min_lines: int = 2) -> None:
        super().__init__(parent)
        v = QVBoxLayout(self)
        v.setContentsMargins(0, 0, 0, 0)
        v.setSpacing(6)
        self.edit = AutoTextEdit(placeholder, min_lines=min_lines)
        self.edit.setObjectName("CommentEditor")
        v.addWidget(self.edit)
        row = QHBoxLayout()
        row.setSpacing(6)
        row.addStretch(1)
        self.cancel_btn = QPushButton(tr("comments.cancel"))
        self.cancel_btn.setProperty("variant", "ghost")
        self.ok_btn = QPushButton(ok_text)
        self.ok_btn.setProperty("variant", "primary")
        for b in (self.cancel_btn, self.ok_btn):
            b.setCursor(Qt.CursorShape.PointingHandCursor)
            b.setProperty("size", "sm")
            row.addWidget(b)
        v.addLayout(row)
        self.ok_btn.clicked.connect(self._commit)
        self.cancel_btn.clicked.connect(self._cancel)
        self.edit.commit.connect(self._commit)
        self.edit.cancel.connect(self._cancel)
        self.edit.installEventFilter(self)
        self._original = ""
        self._done = True

    def start(self, text: str) -> None:
        self._original = text
        self._done = False
        self.edit.setPlainText(text)
        self.show()
        self.edit.setFocus(Qt.FocusReason.OtherFocusReason)
        c = self.edit.textCursor()
        c.movePosition(c.MoveOperation.End)
        self.edit.setTextCursor(c)

    @property
    def active(self) -> bool:
        return not self._done

    def text(self) -> str:
        return self.edit.toPlainText().strip()

    def _commit(self) -> None:
        if self._done:
            return
        self._done = True
        self.committed.emit(self.text())

    def _cancel(self) -> None:
        if self._done:
            return
        self._done = True
        self.cancelled.emit()

    def eventFilter(self, obj, ev) -> bool:  # noqa: N802
        if obj is self.edit and ev.type() == QEvent.Type.FocusOut and not self._done:
            QTimer.singleShot(0, self, self._focus_check)
        return False

    def _focus_check(self) -> None:
        fw = QApplication.focusWidget()
        if self._done or (fw is not None and self.isAncestorOf(fw)):
            return
        if not self.isVisible():
            return
        # focus went elsewhere: keep what was typed (like Acrobat / Word)
        if self.text() != self._original.strip():
            self._commit()
        else:
            self._cancel()


# -- cards ---------------------------------------------------------------------------
class ReplyRow(_HfwMixin, QWidget):
    edit_requested = Signal(int, str)   # xref, new text
    delete_requested = Signal(int)

    def __init__(self, reply: core_comments.Reply, parent=None) -> None:
        super().__init__(parent)
        self.xref = reply.xref
        h = QHBoxLayout(self)
        h.setContentsMargins(0, 2, 0, 2)
        h.setSpacing(7)
        self.avatar = Avatar(reply.author, 20)
        h.addWidget(self.avatar, 0, Qt.AlignmentFlag.AlignTop)
        v = QVBoxLayout()
        v.setSpacing(1)
        top = QHBoxLayout()
        top.setSpacing(6)
        self.name = label("", "heading")
        self.name.setObjectName("CommentAuthor")
        self.when = label("", "caption")
        self.name.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Preferred)
        self.name.setMinimumWidth(10)
        self.when.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        top.addWidget(self.name)
        top.addWidget(self.when, 1)
        self.more = IconButton("dots-three", tr("comments.more"), variant="icon-xs", icon_size=14)
        self.more.clicked.connect(self._menu)
        top.addWidget(self.more)
        v.addLayout(top)
        self.text = label("", None, wrap=True)
        self.text.setObjectName("CommentText")
        self.text.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        v.addWidget(self.text)
        self.editor = _Editor(tr("comments.reply.placeholder"), tr("comments.save"), min_lines=1)
        self.editor.hide()
        self.editor.committed.connect(self._edited)
        self.editor.cancelled.connect(self._end_edit)
        v.addWidget(self.editor)
        h.addLayout(v, 1)
        self.set_reply(reply)

    def set_reply(self, r: core_comments.Reply) -> None:
        self._reply = r
        self.avatar.set_name(r.author)
        self.name.setText(r.author or tr("comments.unknown_author"))
        self.refresh_time()
        if not self.editor.active:
            self.text.setText(r.text)

    def refresh_time(self) -> None:
        self.when.setText(format_when(self._reply.created or self._reply.modified))

    def _menu(self) -> None:
        m = QMenu(self)
        m.addAction(tr("comments.edit"), self._start_edit)
        m.addAction(tr("comments.copy_text"), lambda: QGuiApplication.clipboard().setText(self._reply.text))
        m.addSeparator()
        m.addAction(tr("comments.delete_reply"), lambda: self.delete_requested.emit(self.xref))
        m.exec(self.more.mapToGlobal(self.more.rect().bottomLeft()))

    def _start_edit(self) -> None:
        self.text.hide()
        self.editor.start(self._reply.text)

    def _edited(self, text: str) -> None:
        self._end_edit()
        if text and text != self._reply.text:
            self.edit_requested.emit(self.xref, text)

    def _end_edit(self) -> None:
        self.editor.hide()
        self.text.show()


class CommentCard(_HfwMixin, QFrame):
    """One comment thread."""

    def event(self, ev) -> bool:
        result = super().event(ev)
        if ev.type() in (QEvent.Type.LayoutRequest, QEvent.Type.Resize):
            self._fit_height()
        return result

    # The list's box layout caches each card's height-for-width and does not
    # refresh it reliably when nested wrapped labels change (replies were
    # clipped). Cards therefore report a plain size: their minimum height is
    # pinned to what the content needs at the current width.
    def hasHeightForWidth(self) -> bool:  # noqa: N802
        return False

    def sizeHint(self) -> QSize:  # noqa: N802
        hint = super().sizeHint()
        if self.minimumHeight() > 0:
            hint.setHeight(self.minimumHeight())
        return hint

    def _fit_height(self) -> None:
        """Minimum height = height of the wrapped content at this width."""
        lay = self.layout()
        if lay is None or self.width() <= 0:
            return
        h = lay.totalHeightForWidth(self.width())
        if h > 0 and h != self.minimumHeight():
            self.setMinimumHeight(h)
            # this may run inside the list's own layout pass, which then
            # ignores the new minimum: lay the list out again afterwards
            QTimer.singleShot(0, self, self._relayout_list)

    def _relayout_list(self) -> None:
        parent = self.parentWidget()
        lay = parent.layout() if parent is not None else None
        if lay is not None:
            lay.invalidate()
            lay.activate()

    activated = Signal(object, bool)        # card, additive (Ctrl/Shift)
    text_edited = Signal(object, str)       # key, text
    reply_added = Signal(object, str)       # key, text
    reply_edited = Signal(object, int, str)  # key, reply xref, text
    reply_deleted = Signal(object, int)
    resolve_toggled = Signal(object, bool)
    status_chosen = Signal(object, str)
    delete_requested = Signal(object)

    def __init__(self, comment: core_comments.Comment, parent=None) -> None:
        super().__init__(parent)
        self.setObjectName("CommentCard")
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFocusPolicy(Qt.FocusPolicy.ClickFocus)
        self.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Minimum)
        self.comment = comment
        self._selected = False
        lay = QVBoxLayout(self)
        lay.setContentsMargins(12, 10, 10, 10)
        lay.setSpacing(6)

        head = QHBoxLayout()
        head.setSpacing(8)
        self.avatar = Avatar(comment.author, 26)
        head.addWidget(self.avatar, 0, Qt.AlignmentFlag.AlignTop)
        names = QVBoxLayout()
        names.setSpacing(0)
        self.name = label("", "heading")
        self.name.setObjectName("CommentAuthor")
        self.meta = label("", "caption")
        for lbl in (self.name, self.meta):  # never wider than the card: long names are cut
            lbl.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
            lbl.setMinimumWidth(20)
        names.addWidget(self.name)
        names.addWidget(self.meta)
        head.addLayout(names, 1)
        self.resolve = IconButton("check-circle", tr("comments.resolve"), variant="icon-sm", checkable=True,
                                  icon_size=17, on_role="success")
        self.resolve.setAccessibleName("comments.resolve")
        self.resolve.clicked.connect(lambda on: self.resolve_toggled.emit(self.key, bool(on)))
        head.addWidget(self.resolve, 0, Qt.AlignmentFlag.AlignTop)
        self.more = IconButton("dots-three", tr("comments.more"), variant="icon-sm", icon_size=17)
        self.more.setAccessibleName("comments.more")
        self.more.clicked.connect(self._menu)
        head.addWidget(self.more, 0, Qt.AlignmentFlag.AlignTop)
        lay.addLayout(head)

        self.quote_box = _Box()
        qh = QHBoxLayout(self.quote_box)
        qh.setContentsMargins(0, 0, 0, 0)
        qh.setSpacing(8)
        self.quote_bar = QFrame()
        self.quote_bar.setFixedWidth(3)
        qh.addWidget(self.quote_bar)
        self.quote = label("", "muted", wrap=True)
        self.quote.setObjectName("CommentQuote")
        qh.addWidget(self.quote, 1)
        lay.addWidget(self.quote_box)

        self.body = _ClickLabel()
        self.body.setObjectName("CommentText")
        self.body.setWordWrap(True)
        self.body.setCursor(Qt.CursorShape.IBeamCursor)
        self.body.clicked.connect(self.start_edit)
        lay.addWidget(self.body)
        self.editor = _Editor(tr("comments.placeholder"), tr("comments.save"))
        self.editor.hide()
        self.editor.committed.connect(self._text_committed)
        self.editor.cancelled.connect(self._end_edit)
        lay.addWidget(self.editor)

        self.replies_box = _Box()
        self.replies_lay = QVBoxLayout(self.replies_box)
        self.replies_lay.setContentsMargins(12, 2, 0, 0)
        self.replies_lay.setSpacing(4)
        lay.addWidget(self.replies_box)
        self._reply_rows: dict[int, ReplyRow] = {}

        self.reply_link = QPushButton(tr("comments.reply"))
        self.reply_link.setProperty("variant", "link")
        self.reply_link.setCursor(Qt.CursorShape.PointingHandCursor)
        self.reply_link.clicked.connect(self.start_reply)
        self.reply_editor = _Editor(tr("comments.reply.placeholder"), tr("comments.reply.send"), min_lines=1)
        self.reply_editor.hide()
        self.reply_editor.committed.connect(self._reply_committed)
        self.reply_editor.cancelled.connect(self._end_reply)
        self.chip = QLabel()
        self.chip.setObjectName("StatusChip")
        self.chip.hide()
        self.footer = _Box()
        rl = QHBoxLayout(self.footer)
        rl.setContentsMargins(0, 0, 0, 0)
        rl.addWidget(self.reply_link)
        rl.addStretch(1)
        rl.addWidget(self.chip)
        lay.addWidget(self.footer)
        lay.addWidget(self.reply_editor)
        self.set_comment(comment)

    # -- data ------------------------------------------------------------------------
    @property
    def key(self) -> Key:
        return (self.comment.page, self.comment.xref)

    def set_comment(self, c: core_comments.Comment, *, show_page: bool = False) -> None:
        self.comment = c
        self.avatar.set_name(c.author)
        self.name.setText(c.author or tr("comments.unknown_author"))
        self._show_page = show_page
        self.refresh_time()
        chip = STATUS_CHIP.get(c.status)
        if chip:
            self.chip.setText(tr(f"core.comment.status.{c.status}"))
            set_prop(self.chip, "chip", chip)
            self.chip.show()
        else:
            self.chip.hide()
        self.resolve.setChecked(c.resolved)
        self.resolve.setToolTip(tr("comments.reopen") if c.resolved else tr("comments.resolve"))
        if self.property("resolved") != c.resolved:
            set_prop(self, "resolved", c.resolved)
            for child in self.findChildren(QLabel):  # rules keyed on the card's state
                child.style().unpolish(child)
                child.style().polish(child)
        quote = c.quote
        if len(quote) > QUOTE_MAX:
            quote = quote[:QUOTE_MAX - 1].rstrip() + "…"
        self.quote.setText(f"»{quote}«" if quote else "")
        self.quote_box.setVisible(bool(quote))
        col = to_qcolor(c.color) if c.color else T.qcolor("text_subtle")
        self.quote_bar.setStyleSheet(f"background: rgba({col.red()}, {col.green()}, {col.blue()}, 255);"
                                     " border-radius: 1px;")
        if not self.editor.active:
            self._show_body()
        # replies: update rows in place (keeps an open reply editor)
        keep = {r.xref for r in c.replies}
        for x in [x for x in self._reply_rows if x not in keep]:
            row = self._reply_rows.pop(x)
            row.hide()
            row.deleteLater()
        for i, r in enumerate(c.replies):
            row = self._reply_rows.get(r.xref)
            if row is None:
                row = ReplyRow(r)
                row.edit_requested.connect(lambda x, t: self.reply_edited.emit(self.key, x, t))
                row.delete_requested.connect(lambda x: self.reply_deleted.emit(self.key, x))
                self._reply_rows[r.xref] = row
            else:
                row.set_reply(r)
            if self.replies_lay.indexOf(row) != i:
                if self.replies_lay.indexOf(row) >= 0:
                    self.replies_lay.removeWidget(row)
                self.replies_lay.insertWidget(i, row)
        self.replies_box.setVisible(bool(c.replies))
        self.reply_link.setText(tr("comments.reply") if not c.replies else tr("comments.reply.more"))
        self._update_reply_visibility()

    def refresh_time(self) -> None:
        c = self.comment
        parts = [format_when(c.created or c.modified), tr(f"annot.kind.{c.kind}")]
        if getattr(self, "_show_page", False):
            parts.append(tr("comments.page_short", n=c.page + 1))
        self.meta.setText(" · ".join(p for p in parts if p))
        for row in self._reply_rows.values():
            row.refresh_time()

    def _show_body(self) -> None:
        text = self.comment.text
        editable = self.comment.kind not in ("freetext",)
        if text:
            self.body.setText(text)
            set_prop(self.body, "placeholder", False)
        else:
            key = "comments.placeholder.replace" if self.comment.kind == "replace" else \
                "comments.placeholder.insert" if self.comment.kind == "insert" else "comments.placeholder"
            self.body.setText(tr(key))
            set_prop(self.body, "placeholder", True)
        self.body.setVisible(bool(text) or editable)
        self.body.setCursor(Qt.CursorShape.IBeamCursor if editable else Qt.CursorShape.PointingHandCursor)

    # -- selection ---------------------------------------------------------------------
    @property
    def selected(self) -> bool:
        return self._selected

    def set_selected(self, on: bool) -> None:
        if on == self._selected:
            return
        self._selected = on
        set_prop(self, "selected", on)
        self._update_reply_visibility()

    def _update_reply_visibility(self) -> None:
        link = (self._selected or bool(self.comment.replies)) and not self.reply_editor.active
        self.reply_link.setVisible(link)
        self.footer.setVisible(link or self.comment.status in STATUS_CHIP)

    def mousePressEvent(self, ev) -> None:  # noqa: N802
        if ev.button() == Qt.MouseButton.LeftButton:
            additive = bool(ev.modifiers() & (Qt.KeyboardModifier.ControlModifier | Qt.KeyboardModifier.ShiftModifier))
            self.activated.emit(self, additive)
            ev.accept()
            return
        super().mousePressEvent(ev)

    def contextMenuEvent(self, ev) -> None:  # noqa: N802
        self.activated.emit(self, False)
        self._menu(ev.globalPos())

    # -- editing -----------------------------------------------------------------------
    @property
    def editing(self) -> bool:
        return self.editor.active or self.reply_editor.active or any(r.editor.active for r in self._reply_rows.values())

    def start_edit(self) -> None:
        if self.comment.kind == "freetext":   # its text is on the page: edit it there
            self.activated.emit(self, False)
            return
        if not self._selected:
            self.activated.emit(self, False)
        self.body.hide()
        self.editor.start(self.comment.text)

    def _text_committed(self, text: str) -> None:
        self._end_edit()
        if text != self.comment.text:
            self.text_edited.emit(self.key, text)

    def _end_edit(self) -> None:
        self.editor.hide()
        self._show_body()

    def start_reply(self) -> None:
        if not self._selected:
            self.activated.emit(self, False)
        self.reply_link.hide()
        self.reply_editor.start("")

    def _reply_committed(self, text: str) -> None:
        self._end_reply()
        if text:
            self.reply_added.emit(self.key, text)

    def _end_reply(self) -> None:
        self.reply_editor.hide()
        self._update_reply_visibility()

    # -- menu ----------------------------------------------------------------------
    def build_menu(self) -> QMenu:
        c = self.comment
        m = QMenu(self)
        if c.kind != "freetext":
            m.addAction(tr("comments.edit"), self.start_edit)
        m.addAction(tr("comments.reply"), self.start_reply)
        m.addAction(tr("comments.reopen") if c.resolved else tr("comments.resolve"),
                    lambda: self.resolve_toggled.emit(self.key, not c.resolved))
        sm = m.addMenu(tr("comments.status"))
        group = QActionGroup(sm)
        for st in core_comments.REVIEW_STATES:
            a = QAction(tr(f"core.comment.status.{st}"), sm)
            a.setCheckable(True)
            a.setChecked(c.status == st)
            a.triggered.connect(lambda _c=False, s=st: self.status_chosen.emit(self.key, s))
            group.addAction(a)
            sm.addAction(a)
        m.addSeparator()
        text = "\n".join(x for x in (c.quote, c.text) if x)
        copy = m.addAction(tr("comments.copy_text"), lambda: QGuiApplication.clipboard().setText(text))
        copy.setEnabled(bool(text))
        m.addSeparator()
        m.addAction(tr("comments.delete"), lambda: self.delete_requested.emit(self.key))
        return m

    def _menu(self, pos=None) -> None:
        m = self.build_menu()
        m.exec(pos if pos is not None else self.more.mapToGlobal(self.more.rect().bottomLeft()))


# -- the panel -------------------------------------------------------------------------
class CommentsPanel(QWidget):
    countChanged = Signal(int)

    def __init__(self, workspace, parent=None) -> None:
        super().__init__(parent)
        self.workspace = workspace
        self._threads: dict[int, list[core_comments.Comment]] = {}
        self._dirty: set[int] | None = None       # None: everything
        self._filter = "all"
        self._status = "all"
        self._author = ""
        self._only_page = False
        self._query = ""
        self._sort = "page"
        self._cards: dict[Key, CommentCard] = {}
        self._headers: dict[int, QLabel] = {}
        self._selected: list[Key] = []
        self._order: list[Key] = []
        self._linked_tool = None
        self._syncing = False
        self._flash: QGraphicsRectItem | None = None
        self.setFocusPolicy(Qt.FocusPolicy.ClickFocus)

        lay = QVBoxLayout(self)
        lay.setContentsMargins(10, 8, 10, 8)
        lay.setSpacing(8)
        self.header = QWidget()
        hv = QVBoxLayout(self.header)
        hv.setContentsMargins(0, 0, 0, 0)
        hv.setSpacing(8)
        self.filter = SegmentedControl(
            [Segment("all", tr("comments.filter.all"), None, tr("comments.filter.all"))]
            + [Segment(k, "", FILTER_ICONS[k], tr(f"comments.filter.{k}")) for k in FILTERS if k != "all"],
            height=30, expand=True, min_seg=36)
        self.filter.currentChanged.connect(self.set_filter)
        hv.addWidget(self.filter)
        row = QHBoxLayout()
        row.setSpacing(4)
        self.search = QLineEdit()
        self.search.setProperty("variant", "search")
        self.search.setPlaceholderText(tr("comments.search"))
        self.search.setClearButtonEnabled(True)
        self.search.textChanged.connect(self.set_query)
        self._search_icon = ThemedIconLabel("magnifying-glass", 15, "text_subtle", self.search)
        self._search_icon.move(10, 8)
        row.addWidget(self.search, 1)
        self.filter_btn = IconButton("funnel-simple", tr("comments.filter.more"), variant="icon", checkable=True)
        self.filter_btn.setAccessibleName("comments.filter_menu")
        self.filter_btn.clicked.connect(self._filter_menu)
        self.sort_btn = IconButton("sort-ascending", tr("comments.sort"), variant="icon")
        self.sort_btn.setAccessibleName("comments.sort_menu")
        self.sort_btn.clicked.connect(self._sort_menu)
        row.addWidget(self.filter_btn)
        row.addWidget(self.sort_btn)
        hv.addLayout(row)
        crow = QHBoxLayout()
        crow.setSpacing(2)
        self.count = label("", "caption")
        crow.addWidget(self.count, 1)
        self.reset_btn = QPushButton(tr("comments.filter.reset"))
        self.reset_btn.setProperty("variant", "link")
        self.reset_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.reset_btn.clicked.connect(self.reset_filters)
        crow.addWidget(self.reset_btn)
        self.prev_btn = IconButton("caret-up", tr("comments.prev"), variant="icon-sm", icon_size=15, shortcut="Shift+F8")
        self.next_btn = IconButton("caret-down", tr("comments.next"), variant="icon-sm", icon_size=15, shortcut="F8")
        self.prev_btn.clicked.connect(lambda: self.step(-1))
        self.next_btn.clicked.connect(lambda: self.step(1))
        crow.addWidget(self.prev_btn)
        crow.addWidget(self.next_btn)
        hv.addLayout(crow)
        lay.addWidget(self.header)

        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(True)
        self.scroll.setFrameShape(QFrame.Shape.NoFrame)
        self.scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.list = QWidget()
        self.list.setObjectName("CommentList")
        self.list_lay = QVBoxLayout(self.list)
        self.list_lay.setContentsMargins(0, 0, 4, 8)
        self.list_lay.setSpacing(8)
        self.list_lay.addStretch(1)
        self.scroll.setWidget(self.list)

        self.stack = QStackedWidget()
        self.stack.addWidget(self._state_page("chat-circle-text", tr("comments.empty.title"), tr("comments.empty.text")))
        self.stack.addWidget(self.scroll)
        self.none_page = self._state_page("funnel-simple", tr("comments.filter.none"), tr("comments.filter.none.hint"))
        self.stack.addWidget(self.none_page)
        lay.addWidget(self.stack, 1)

        self._clock = QTimer(self)
        self._clock.setInterval(60_000)
        self._clock.timeout.connect(self._refresh_times)

    @staticmethod
    def _state_page(icon_name: str, title: str, text: str) -> QWidget:
        page = QWidget()
        ev = QVBoxLayout(page)
        ev.setContentsMargins(16, 40, 16, 16)
        ev.setSpacing(6)
        ev.addWidget(ThemedIconLabel(icon_name, 28, "text_subtle"), 0, Qt.AlignmentFlag.AlignHCenter)
        ev.addSpacing(4)
        t = label(title, "heading")
        t.setAlignment(Qt.AlignmentFlag.AlignCenter)
        d = label(text, "caption", wrap=True)
        d.setAlignment(Qt.AlignmentFlag.AlignCenter)
        ev.addWidget(t)
        ev.addWidget(d)
        ev.addStretch(1)
        page.title_label = t  # type: ignore[attr-defined]
        return page

    # -- lifecycle -------------------------------------------------------------------
    def document_changed(self, event=None) -> None:
        pages = getattr(event, "pages", None)
        if event is not None and event.kind == CONTENT and pages is not None and self._dirty is not None:
            self._dirty |= set(pages)
        elif event is None or event.kind != CONTENT or pages is None:
            self._dirty = None
        if event is not None and event.kind == STRUCTURE:
            self._clear_cards()
        if self.isVisible():
            self.reload()

    def showEvent(self, ev) -> None:  # noqa: N802
        super().showEvent(ev)
        self._link_tool()
        self._clock.start()
        if self._dirty is None or self._dirty:
            self.reload()

    def hideEvent(self, ev) -> None:  # noqa: N802
        super().hideEvent(ev)
        self._clock.stop()

    def ensure_loaded(self) -> None:
        if self._dirty is None or self._dirty:
            self.reload()

    def reload(self) -> None:
        self._link_tool()
        doc = self.workspace.pdf.doc
        n = doc.page_count
        if self._dirty is None:
            pages = list(range(n))
            self._threads = {}
        else:
            pages = sorted(p for p in self._dirty if 0 <= p < n)
        self._dirty = set()
        for p in pages:
            try:
                threads = core_comments.comments(doc, [p])
            except Exception:
                threads = []
            if threads:
                self._threads[p] = threads
            else:
                self._threads.pop(p, None)
        for p in [p for p in self._threads if p >= n]:
            del self._threads[p]
        self._populate()

    def all_comments(self) -> list[core_comments.Comment]:
        return [c for p in sorted(self._threads) for c in self._threads[p]]

    # -- filters ---------------------------------------------------------------------
    def _visible(self, c: core_comments.Comment) -> bool:
        if not matches(c.kind, self._filter):
            return False
        if self._status == "open" and c.resolved or self._status == "resolved" and not c.resolved:
            return False
        if self._author and c.author != self._author:
            return False
        if self._only_page and c.page != self.workspace.view.current_page:
            return False
        return c.matches(self._query)

    @property
    def filtered(self) -> bool:
        return (self._filter != "all" or self._status != "all" or bool(self._author) or self._only_page
                or bool(self._query.strip()))

    def set_filter(self, key: str) -> None:
        if key not in FILTERS:
            return
        self._filter = key
        if self.filter.current_key() != key:
            self.filter.set_current_key(key, animate=self.isVisible())
        self._populate()

    def set_status_filter(self, key: str) -> None:
        self._status = key if key in STATUS_FILTERS else "all"
        self._populate()

    def set_author_filter(self, name: str) -> None:
        self._author = name or ""
        self._populate()

    def set_only_current_page(self, on: bool) -> None:
        self._only_page = bool(on)
        self._populate()

    def set_query(self, text: str) -> None:
        self._query = text or ""
        if self.search.text() != self._query:
            self.search.setText(self._query)
        self._populate()

    def set_sort(self, key: str) -> None:
        self._sort = key if key in SORTS else "page"
        self._populate()

    def reset_filters(self) -> None:
        self._status, self._author, self._only_page = "all", "", False
        self._query = ""
        self.search.blockSignals(True)
        self.search.clear()
        self.search.blockSignals(False)
        self.set_filter("all")

    def authors(self) -> list[str]:
        return sorted({c.author for c in self.all_comments() if c.author}, key=str.casefold)

    def _filter_menu(self) -> None:
        m = QMenu(self)
        m.addSection(tr("comments.filter.status"))
        g = QActionGroup(m)
        for key in STATUS_FILTERS:
            a = m.addAction(tr(f"comments.filter.status.{key}"))
            a.setCheckable(True)
            a.setChecked(self._status == key)
            a.triggered.connect(lambda _c=False, k=key: self.set_status_filter(k))
            g.addAction(a)
        m.addSection(tr("comments.filter.author"))
        g2 = QActionGroup(m)
        for name in [""] + self.authors():
            a = m.addAction(name or tr("comments.filter.author.all"))
            a.setCheckable(True)
            a.setChecked(self._author == name)
            a.triggered.connect(lambda _c=False, n=name: self.set_author_filter(n))
            g2.addAction(a)
        m.addSeparator()
        a = m.addAction(tr("comments.filter.page"))
        a.setCheckable(True)
        a.setChecked(self._only_page)
        a.triggered.connect(self.set_only_current_page)
        m.exec(self.filter_btn.mapToGlobal(self.filter_btn.rect().bottomLeft()))
        self.filter_btn.setChecked(self._status != "all" or bool(self._author) or self._only_page)

    def _sort_menu(self) -> None:
        m = QMenu(self)
        g = QActionGroup(m)
        for key in SORTS:
            a = m.addAction(tr(f"comments.sort.{key}"))
            a.setCheckable(True)
            a.setChecked(self._sort == key)
            a.triggered.connect(lambda _c=False, k=key: self.set_sort(k))
            g.addAction(a)
        m.exec(self.sort_btn.mapToGlobal(self.sort_btn.rect().bottomLeft()))

    # -- building the list -----------------------------------------------------------
    def _clear_cards(self) -> None:
        for card in self._cards.values():
            card.hide()
            card.deleteLater()
        self._cards.clear()
        self._selected = []

    def _card(self, c: core_comments.Comment, show_page: bool) -> CommentCard:
        card = self._cards.get(c.key)
        if card is None:
            card = CommentCard(c)
            card.activated.connect(self._card_activated)
            card.text_edited.connect(self._edit_text)
            card.reply_added.connect(self._add_reply)
            card.reply_edited.connect(self._edit_reply)
            card.reply_deleted.connect(self._delete_reply)
            card.resolve_toggled.connect(self._resolve)
            card.status_chosen.connect(self._status_chosen)
            card.delete_requested.connect(self._delete)
            self._cards[c.key] = card
            card.set_comment(c, show_page=show_page)
        elif card.comment is not c or card._show_page != show_page:
            card.set_comment(c, show_page=show_page)
        return card

    def _header(self, page: int) -> QLabel:
        h = self._headers.get(page)
        if h is None:
            h = label("", "section")
            h.setContentsMargins(2, 4, 0, 0)
            self._headers[page] = h
        return h

    def _populate(self) -> None:
        all_ = self.all_comments()
        shown = [c for c in all_ if self._visible(c)]
        if self._sort == "newest":
            shown.sort(key=lambda c: (c.last_activity.timestamp() if c.last_activity else 0.0), reverse=True)
        elif self._sort == "author":
            shown.sort(key=lambda c: ((c.author or "").casefold(), c.page, c.rect.y0))
        grouped = self._sort == "page"
        live = {c.key for c in all_}
        for key in [k for k in self._cards if k not in live]:
            card = self._cards.pop(key)
            card.hide()
            card.deleteLater()
        per_page: dict[int, int] = {}
        for c in shown:
            per_page[c.page] = per_page.get(c.page, 0) + 1
        wanted: list[QWidget] = []
        page = None
        for c in shown:
            if grouped and c.page != page:
                page = c.page
                h = self._header(page)
                h.setText(tr("comments.page", n=page + 1, count=per_page[page]))
                wanted.append(h)
            wanted.append(self._card(c, show_page=not grouped))
        # re-lay out without hiding what stays: hiding a card would take the
        # keyboard focus away from an editor that is open in it
        lay = self.list_lay
        keep = {id(w) for w in wanted}
        self.list.setUpdatesEnabled(False)
        try:
            while lay.count():
                item = lay.takeAt(0)
                w = item.widget()
                if w is not None and id(w) not in keep:
                    w.hide()
            for w in wanted:
                lay.addWidget(w)
                w.show()
            lay.addStretch(1)
        finally:
            self.list.setUpdatesEnabled(True)
        self._order = [c.key for c in shown]
        self._selected = [k for k in self._selected if k in self._cards]
        for key, card in self._cards.items():
            card.set_selected(key in self._selected)
        self._update_header(len(all_), len(shown))
        if not all_:
            self.stack.setCurrentIndex(0)
        elif not shown:
            name = tr(f"comments.filter.none.{self._filter}") if self._filter != "all" else tr("comments.filter.none")
            self.none_page.title_label.setText(name)
            self.stack.setCurrentIndex(2)
        else:
            self.stack.setCurrentIndex(1)
        self.countChanged.emit(len(all_))
        self._sync_from_tool()

    def _update_header(self, total: int, shown: int) -> None:
        self.header.setVisible(total > 0)
        if not self.filtered:
            text = tr("comments.count.one") if total == 1 else tr("comments.count", n=total)
        else:
            text = tr("comments.count.filtered", shown=shown, total=total)
        self.count.setText(text)
        self.reset_btn.setVisible(self.filtered)
        self.prev_btn.setEnabled(shown > 0)
        self.next_btn.setEnabled(shown > 0)
        self.filter_btn.setChecked(self._status != "all" or bool(self._author) or self._only_page)

    def _refresh_times(self) -> None:
        for card in self._cards.values():
            if card.isVisible():
                card.refresh_time()

    def cards(self) -> list[CommentCard]:
        """Visible cards in list order."""
        return [self._cards[k] for k in self._order if k in self._cards]

    def card(self, page: int, xref: int) -> CommentCard | None:
        return self._cards.get((page, xref))

    # -- selection & navigation -------------------------------------------------------
    @property
    def selected_keys(self) -> list[Key]:
        return list(self._selected)

    def _set_selected(self, keys: list[Key], *, scroll: bool = True) -> None:
        self._selected = [k for k in keys if k in self._cards]
        for key, card in self._cards.items():
            card.set_selected(key in self._selected)
        if scroll and self._selected:
            card = self._cards[self._selected[0]]
            QTimer.singleShot(0, self, lambda: self._scroll_to(card))

    def _scroll_to(self, card: CommentCard) -> None:
        try:
            self.scroll.ensureWidgetVisible(card, 0, 12)
        except RuntimeError:
            pass

    def _card_activated(self, card: CommentCard, additive: bool) -> None:
        key = card.key
        if additive:
            keys = [k for k in self._selected if k != key] if key in self._selected else self._selected + [key]
        else:
            keys = [key]
        self._set_selected(keys, scroll=False)
        if not card.editing:
            self.setFocus(Qt.FocusReason.OtherFocusReason)  # Delete / arrow keys act on the list
        self._select_on_page(keys)
        if not additive:
            self.reveal(card.comment)

    def reveal(self, c: core_comments.Comment) -> None:
        """Scroll the page view to a comment and flash it."""
        view = self.workspace.view
        r = c.rect
        rect = QRectF(r.x0, r.y0, r.width, r.height)
        view.ensure_visible_rect(c.page, rect.adjusted(-36, -36, 36, 36))
        self._flash_rect(c.page, rect)

    def _flash_rect(self, page: int, rect: QRectF) -> None:
        view = self.workspace.view
        if self._flash is not None:
            try:
                view.remove_overlay(self._flash)
            except Exception:
                pass
        item = QGraphicsRectItem(rect.adjusted(-3, -3, 3, 3))
        pen = QPen(T.qcolor("accent"), 2.0)
        pen.setCosmetic(True)
        item.setPen(pen)
        item.setZValue(60)
        try:
            view.add_overlay(page, item)
        except IndexError:
            return
        self._flash = item
        anim = QVariantAnimation(self)
        anim.setDuration(1100)
        anim.setStartValue(1.0)
        anim.setEndValue(0.0)

        def step(v, it=item):
            try:
                it.setOpacity(float(v))
            except RuntimeError:
                anim.stop()

        def done(it=item):
            try:
                view.remove_overlay(it)
            except Exception:
                pass
            if self._flash is it:
                self._flash = None

        anim.valueChanged.connect(step)
        anim.finished.connect(done)
        anim.start(QVariantAnimation.DeletionPolicy.DeleteWhenStopped)

    def _select_on_page(self, keys: list[Key]) -> None:
        """Select the cards' annotations on the page with "Markér kommentar"
        (switching to it), so they can be moved and restyled right away."""
        ws = self.workspace
        tool = ws.tool
        if (tool is None or tool.id != "annotate.select") and keys:
            win = ws.window_
            if win is not None and hasattr(win, "activate_tool"):
                win.activate_tool("annotate.select")
            else:
                ws.set_tool("annotate.select")
            tool = ws.tool
            self._link_tool()
        if tool is None or tool.id != "annotate.select" or not hasattr(tool, "set_selection"):
            return
        doc = ws.pdf.doc
        infos = []
        by_page: dict[int, dict[int, core_annots.AnnotInfo]] = {}
        for page, xref in keys:
            if page not in by_page:
                by_page[page] = {a.xref: a for a in core_annots.list_annotations(doc, page)}
            if xref in by_page[page]:
                infos.append(by_page[page][xref])
        self._syncing = True
        try:
            tool.set_selection(infos)
        finally:
            self._syncing = False

    def focus_comment(self, page: int, xref: int, *, edit: bool = False) -> bool:
        """Select (and optionally start editing) a comment's card."""
        self.ensure_loaded()
        key = (page, xref)
        if key not in self._order and key in {c.key for c in self.all_comments()}:
            self.reset_filters()
        card = self._cards.get(key)
        if card is None:
            return False
        self._set_selected([key])
        self._scroll_to(card)
        if edit:
            QTimer.singleShot(0, self, lambda: self._edit_after_layout(card))
        return True

    def _edit_after_layout(self, card: CommentCard) -> None:
        try:
            self._scroll_to(card)
            card.start_edit()
        except RuntimeError:
            pass

    def step(self, delta: int) -> bool:
        """Select the next / previous comment (F8 / Shift+F8) and show it."""
        self.ensure_loaded()
        order = self._order
        if not order:
            return False
        if self._selected and self._selected[0] in order:
            i = order.index(self._selected[0]) + delta
        else:
            cur = self.workspace.view.current_page
            after = [i for i, k in enumerate(order) if k[0] >= cur]
            i = (after[0] if after else 0) if delta > 0 else ((after[0] - 1) if after else len(order) - 1)
        i %= len(order)
        key = order[i]
        self._set_selected([key])
        self._select_on_page([key])
        self.reveal(self._cards[key].comment)
        return True

    # -- tool <-> cards ---------------------------------------------------------------
    def _link_tool(self) -> None:
        ws = self.workspace
        tool = ws.tool_instance("annotate.select") if hasattr(ws, "tool_instance") else None
        if tool is None or tool is self._linked_tool or not hasattr(tool, "selectionChanged"):
            return
        tool.selectionChanged.connect(self._sync_from_tool)
        self._linked_tool = tool

    def _sync_from_tool(self) -> None:
        tool = self._linked_tool
        if tool is None or self._syncing or not getattr(tool, "active", False):
            return
        keys = [(a.page, a.xref) for a in getattr(tool, "selection", [])]
        if keys or self._selected:
            self._set_selected(keys)

    # -- actions -----------------------------------------------------------------------
    def _run(self, fn, *args) -> bool:
        try:
            fn(self.workspace.pdf, *args)
            return True
        except Exception as exc:  # noqa: BLE001 - shown to the user
            self.workspace.toast(tr("comments.error", err=str(exc)), "error")
            return False

    def _edit_text(self, key: Key, text: str) -> None:
        self._run(core_comments.edit_reply, key[0], key[1], text)

    def _add_reply(self, key: Key, text: str) -> None:
        self._run(core_comments.add_reply, key[0], key[1], text)

    def _edit_reply(self, key: Key, xref: int, text: str) -> None:
        self._run(core_comments.edit_reply, key[0], xref, text)

    def _delete_reply(self, key: Key, xref: int) -> None:
        self._run(core_comments.delete_reply, key[0], xref)

    def _resolve(self, key: Key, on: bool) -> None:
        self._run(core_comments.set_resolved, key[0], key[1], on)

    def _status_chosen(self, key: Key, status: str) -> None:
        self._run(core_comments.set_status, key[0], key[1], status)

    def _delete(self, key: Key) -> None:
        keys = self._selected if key in self._selected and len(self._selected) > 1 else [key]
        self.delete_keys(keys)

    def delete_keys(self, keys: list[Key]) -> bool:
        if not keys:
            return False
        if not confirm_delete(self.window(), [self._cards[k].comment for k in keys if k in self._cards]):
            return False
        self._selected = [k for k in self._selected if k not in keys]
        try:
            core_annots.delete_annotations(self.workspace.pdf, keys)
        except Exception as exc:  # noqa: BLE001
            self.workspace.toast(tr("comments.error", err=str(exc)), "error")
            return False
        return True

    def keyPressEvent(self, ev: QKeyEvent) -> None:  # noqa: N802
        key = ev.key()
        if key in (Qt.Key.Key_Delete, Qt.Key.Key_Backspace) and self._selected:
            self.delete_keys(list(self._selected))
            return
        if key in (Qt.Key.Key_Down, Qt.Key.Key_Up):
            self.step(1 if key == Qt.Key.Key_Down else -1)
            return
        if key in (Qt.Key.Key_Return, Qt.Key.Key_Enter) and len(self._selected) == 1:
            self._cards[self._selected[0]].start_edit()
            return
        super().keyPressEvent(ev)


def confirm_delete(parent, threads: list[core_comments.Comment]) -> bool:
    """Ask before deleting comments that have replies (they go too)."""
    replies = sum(len(c.replies) for c in threads)
    if not replies:
        return True
    from ..dialogs.common import confirm

    return confirm(parent, tr("comments.delete.title"), tr("comments.delete.text", n=replies),
                   tr("comments.delete"), danger=True)


__all__ = ["CommentCard", "CommentsPanel", "FILTERS", "confirm_delete", "format_when", "initials", "matches"]
