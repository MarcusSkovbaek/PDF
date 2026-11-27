"""Combine several PDFs and images into one document.

Files are listed with a first-page thumbnail and page count, can be
reordered by drag & drop (or the up/down buttons) and each file can be
limited to a page selection ("1-3, 7"). Locked PDFs ask for their password
when added. The result is exposed as ``result_bytes`` / ``result_title``.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field

import pymupdf
from PySide6.QtCore import QRectF, QSize, Qt
from PySide6.QtGui import QFont, QGuiApplication, QIcon, QKeySequence, QPainter, QPainterPath, QPixmap, QShortcut
from PySide6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QFileDialog,
    QFrame,
    QHBoxLayout,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QPushButton,
    QStyle,
    QStyledItemDelegate,
    QVBoxLayout,
    QWidget,
)

from ...core import DocumentError, InvalidPassword, PasswordRequired
from ...core.images import SUPPORTED_IMAGE_EXTENSIONS
from ...core.merge import MergeSource, merge_files, open_source
from ...core.pages import parse_page_ranges
from ...i18n import tr
from ..theme import tokens as T
from ..theme.icons import icon
from ..viewer.render_util import to_qimage
from ..widgets.util import label
from .common import FolioDialog, show_error
from .password import ask_password

THUMB_W, THUMB_H = 36, 48
ENTRY_ROLE = Qt.ItemDataRole.UserRole
ACCEPTED = (".pdf",) + tuple(SUPPORTED_IMAGE_EXTENSIONS)


@dataclass
class MergeEntry:
    path: str
    page_count: int
    password: str | None = None
    ranges: str = ""                       # user text; "" = all pages
    page_indices: list[int] | None = field(default=None)  # parsed ranges

    @property
    def name(self) -> str:
        return os.path.basename(self.path)

    @property
    def selected_count(self) -> int:
        return self.page_count if self.page_indices is None else len(self.page_indices)

    def source(self) -> MergeSource:
        return MergeSource(self.path, self.page_indices, self.password)


def _human_size(path: str) -> str:
    try:
        size = os.path.getsize(path)
    except OSError:
        return ""
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            return (f"{size:.0f} {unit}" if unit == "B" else f"{size:.1f} {unit}").replace(".", ",")
        size /= 1024
    return ""


def _first_page_thumb(doc: pymupdf.Document, dpr: float) -> QPixmap:
    page = doc[0]
    zoom = min(THUMB_W / page.rect.width, THUMB_H / page.rect.height) * dpr
    img = to_qimage(page.get_pixmap(matrix=pymupdf.Matrix(zoom, zoom), annots=True))
    img.setDevicePixelRatio(dpr)
    canvas = QPixmap(int(THUMB_W * dpr), int(THUMB_H * dpr))
    canvas.setDevicePixelRatio(dpr)
    canvas.fill(Qt.GlobalColor.transparent)
    p = QPainter(canvas)
    w, h = img.width() / dpr, img.height() / dpr
    r = QRectF((THUMB_W - w) / 2, (THUMB_H - h) / 2, w, h)
    p.drawImage(r, img)
    p.setPen(T.qcolor("border_strong"))
    p.drawRect(r.adjusted(0, 0, -0.5, -0.5))
    p.end()
    return canvas


class FileRowDelegate(QStyledItemDelegate):
    """Row: thumbnail, file name and a muted meta line (pages · size). Draws
    the thumbnail untinted so selection never recolours page previews."""

    ROW_H = THUMB_H + 16

    def sizeHint(self, option, index) -> QSize:  # noqa: N802
        return QSize(option.rect.width(), self.ROW_H)

    def paint(self, p: QPainter, option, index) -> None:
        p.save()
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        r = QRectF(option.rect).adjusted(2, 1, -2, -1)
        selected = bool(option.state & QStyle.StateFlag.State_Selected)
        hovered = bool(option.state & QStyle.StateFlag.State_MouseOver)
        if selected or hovered:
            path = QPainterPath()
            path.addRoundedRect(r, 8, 8)
            p.fillPath(path, T.qcolor("selected" if selected else "hover"))
        pm = index.data(Qt.ItemDataRole.DecorationRole)
        if isinstance(pm, QIcon):
            pm = pm.pixmap(QSize(THUMB_W, THUMB_H))
        if isinstance(pm, QPixmap) and not pm.isNull():
            p.drawPixmap(int(r.left() + 10), int(r.center().y() - THUMB_H / 2), pm)
        name, _, meta = (index.data(Qt.ItemDataRole.DisplayRole) or "").partition("\n")
        x = r.left() + 10 + THUMB_W + 12
        tr_ = QRectF(x, r.top(), r.right() - x - 10, r.height())
        f = QFont(option.font)
        f.setPixelSize(13)
        f.setWeight(QFont.Weight.DemiBold)
        p.setFont(f)
        p.setPen(T.qcolor("text"))
        fm = p.fontMetrics()
        p.drawText(tr_.adjusted(0, 0, 0, -r.height() / 2 + 1), Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignBottom,
                   fm.elidedText(name, Qt.TextElideMode.ElideMiddle, int(tr_.width())))
        f.setPixelSize(12)
        f.setWeight(QFont.Weight.Normal)
        p.setFont(f)
        p.setPen(T.qcolor("text_muted"))
        p.drawText(tr_.adjusted(0, r.height() / 2 + 3, 0, 0), Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop, meta)
        p.restore()


class FileList(QListWidget):
    def __init__(self, dialog: "MergeDialog") -> None:
        super().__init__()
        self.dialog = dialog
        self.setObjectName("ImageGrid")  # shares the framed drop-area look
        self.setIconSize(QSize(THUMB_W, THUMB_H))
        self.setSpacing(2)
        self.setDragDropMode(QAbstractItemView.DragDropMode.InternalMove)
        self.setDefaultDropAction(Qt.DropAction.MoveAction)
        self.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.setAcceptDrops(True)
        self.setMinimumSize(440, 360)
        self.setMouseTracking(True)
        self.setItemDelegate(FileRowDelegate(self))

    def dragEnterEvent(self, ev) -> None:  # noqa: N802
        if ev.mimeData().hasUrls():
            ev.acceptProposedAction()
        else:
            super().dragEnterEvent(ev)

    def dragMoveEvent(self, ev) -> None:  # noqa: N802
        if ev.mimeData().hasUrls():
            ev.acceptProposedAction()
        else:
            super().dragMoveEvent(ev)

    def dropEvent(self, ev) -> None:  # noqa: N802
        if ev.mimeData().hasUrls():
            ev.acceptProposedAction()
            self.dialog.add_paths([u.toLocalFile() for u in ev.mimeData().urls() if u.isLocalFile()])
        else:
            super().dropEvent(ev)
            self.dialog.entries_changed()

    def paintEvent(self, ev) -> None:  # noqa: N802
        super().paintEvent(ev)
        if self.count():
            return
        p = QPainter(self.viewport())
        r = QRectF(self.viewport().rect())
        cy = r.center().y()
        p.drawPixmap(int(r.center().x() - 20), int(cy - 58), icon("files", "text_subtle").pixmap(40, 40))
        f = QFont(self.font())
        f.setPixelSize(15)
        f.setWeight(QFont.Weight.DemiBold)
        p.setFont(f)
        p.setPen(T.qcolor("text"))
        p.drawText(QRectF(r.left(), cy - 8, r.width(), 24), Qt.AlignmentFlag.AlignCenter, tr("merge.empty.title"))
        f.setPixelSize(13)
        f.setWeight(QFont.Weight.Normal)
        p.setFont(f)
        p.setPen(T.qcolor("text_muted"))
        p.drawText(QRectF(r.left(), cy + 18, r.width(), 20), Qt.AlignmentFlag.AlignCenter, tr("merge.empty.text"))
        p.end()


class MergeDialog(FolioDialog):
    def __init__(self, parent: QWidget | None, initial: list[str] | None = None) -> None:
        super().__init__(parent, tr("merge.title"), tr("merge.subtitle"), icon_name="stack", width=860)
        self.result_bytes: bytes | None = None
        self.result_title: str | None = None

        row = QHBoxLayout()
        row.setSpacing(20)
        self.body.addLayout(row, 1)

        left = QVBoxLayout()
        left.setSpacing(10)
        actions = QHBoxLayout()
        actions.setSpacing(8)
        self.add_btn = self._button(tr("merge.add"), "plus", self.choose_files)
        actions.addWidget(self.add_btn)
        actions.addStretch(1)
        self.up_btn = self._button("", "arrow-up", lambda: self.move_selected(-1), ghost=True, tip=tr("merge.up"))
        self.down_btn = self._button("", "arrow-down", lambda: self.move_selected(1), ghost=True,
                                     tip=tr("merge.down"))
        self.remove_btn = self._button(tr("merge.remove"), "trash", self.remove_selected, ghost=True)
        for b in (self.up_btn, self.down_btn, self.remove_btn):
            actions.addWidget(b)
        left.addLayout(actions)
        self.list = FileList(self)
        self.list.itemSelectionChanged.connect(self._selection_changed)
        left.addWidget(self.list, 1)
        row.addLayout(left, 1)

        side = QFrame()
        side.setObjectName("Card")
        side.setFixedWidth(280)
        sl = QVBoxLayout(side)
        sl.setContentsMargins(16, 14, 16, 16)
        sl.setSpacing(8)
        sl.addWidget(label(tr("merge.section.file"), "section"))
        self.file_name = label("", None, wrap=True)
        self.file_name.setTextFormat(Qt.TextFormat.PlainText)
        sl.addWidget(self.file_name)
        self.file_info = label("", "muted")
        sl.addWidget(self.file_info)
        self.pages_label = label(tr("merge.pages.label"), "caption")
        sl.addWidget(self.pages_label)
        self.pages = QLineEdit()
        self.pages.setPlaceholderText(tr("merge.pages.placeholder"))
        self.pages.textEdited.connect(self._ranges_edited)
        sl.addWidget(self.pages)
        self.pages_error = label("", "danger", wrap=True)
        sl.addWidget(self.pages_error)
        self.no_selection = label(tr("merge.no_selection"), "muted", wrap=True)
        sl.addWidget(self.no_selection)
        sl.addSpacing(10)
        sl.addWidget(label(tr("merge.section.options"), "section"))
        self.bookmarks = QCheckBox(tr("merge.bookmarks"))
        self.bookmarks.setChecked(True)
        sl.addWidget(self.bookmarks)
        sl.addStretch(1)
        row.addWidget(side)

        self.total_label = label("", "muted")
        self.buttons.insertWidget(0, self.total_label)
        self.add_button(tr("merge.cancel"), "secondary", "cancel")
        self.run_btn = self.add_button(tr("merge.run"), "primary", default=True)
        self.run_btn.clicked.connect(self._run)

        QShortcut(QKeySequence.StandardKey.Delete, self.list, activated=self.remove_selected)
        self.setAcceptDrops(True)
        if initial:
            self.add_paths(initial)
        self.entries_changed()

    # -- helpers ----------------------------------------------------------
    def _button(self, text: str, icon_name: str, slot, *, ghost: bool = False, tip: str = "") -> QPushButton:
        b = QPushButton(icon(icon_name, "text"), text)
        if ghost:
            b.setProperty("variant", "ghost")
        if tip:
            b.setToolTip(tip)
        b.setCursor(Qt.CursorShape.PointingHandCursor)
        b.setAutoDefault(False)
        b.clicked.connect(slot)
        return b

    def entries(self) -> list[MergeEntry]:
        return [self.list.item(i).data(ENTRY_ROLE) for i in range(self.list.count())]

    def _current_entry(self) -> MergeEntry | None:
        items = self.list.selectedItems()
        return items[0].data(ENTRY_ROLE) if len(items) == 1 else None

    def _toast(self, text: str, kind: str) -> None:
        host = self.parent()
        if host is not None and hasattr(host, "toast"):
            host.toast(text, kind)

    # -- adding / removing / ordering -----------------------------------------
    def _open(self, path: str) -> tuple[pymupdf.Document, str | None] | None:
        """Open a file, asking for a password when needed. None = skipped."""
        password = None
        wrong = False
        while True:
            try:
                return open_source(path, password), password
            except PasswordRequired:
                pass
            except InvalidPassword:
                wrong = True
            except DocumentError:
                return None
            password = ask_password(self, os.path.basename(path), wrong=wrong)
            if password is None:
                return None

    def add_paths(self, paths: list[str]) -> None:
        skipped = []
        dpr = self.devicePixelRatioF()
        for path in paths:
            if not os.path.isfile(path) or os.path.splitext(path)[1].lower() not in ACCEPTED:
                continue
            opened = self._open(path)
            if opened is None:
                skipped.append(os.path.basename(path))
                continue
            doc, password = opened
            try:
                entry = MergeEntry(os.path.abspath(path), doc.page_count, password)
                item = QListWidgetItem(QIcon(_first_page_thumb(doc, dpr)), "")
            finally:
                doc.close()
            item.setData(ENTRY_ROLE, entry)
            item.setToolTip(entry.path)
            item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsDropEnabled)
            self.list.addItem(item)
            self._refresh_item(item)
        if skipped:
            self._toast(tr("merge.skipped", names=", ".join(skipped)), "error")
        self.entries_changed()

    def choose_files(self) -> None:
        patterns = " ".join(f"*{e}" for e in ACCEPTED)
        paths, _ = QFileDialog.getOpenFileNames(self, tr("merge.file_dialog"), "",
                                                tr("merge.file_filter", patterns=patterns))
        if paths:
            self.add_paths(paths)

    def remove_selected(self) -> None:
        for item in self.list.selectedItems():
            self.list.takeItem(self.list.row(item))
        self.entries_changed()

    def move_selected(self, delta: int) -> None:
        rows = sorted(self.list.row(i) for i in self.list.selectedItems())
        if not rows or (delta < 0 and rows[0] == 0) or (delta > 0 and rows[-1] == self.list.count() - 1):
            return
        for r in (rows if delta < 0 else reversed(rows)):
            item = self.list.takeItem(r)
            self.list.insertItem(r + delta, item)
        self.list.clearSelection()
        for r in rows:
            self.list.item(r + delta).setSelected(True)
        self.entries_changed()

    # -- per-file page selection -----------------------------------------------
    def _refresh_item(self, item: QListWidgetItem) -> None:
        e: MergeEntry = item.data(ENTRY_ROLE)
        if e.page_indices is None:
            pages = tr("merge.row.pages_one") if e.page_count == 1 else tr("merge.row.pages", n=e.page_count)
        else:
            pages = tr("merge.row.selected", sel=e.selected_count, n=e.page_count)
        extra = [pages, _human_size(e.path)]
        if e.password:
            extra.append(tr("merge.row.locked"))
        item.setText(f"{e.name}\n{'  ·  '.join(x for x in extra if x)}")

    def _ranges_edited(self, text: str) -> None:
        items = self.list.selectedItems()
        if len(items) != 1:
            return
        e: MergeEntry = items[0].data(ENTRY_ROLE)
        e.ranges = text
        error = ""
        if not text.strip():
            e.page_indices = None
        else:
            try:
                e.page_indices = parse_page_ranges(text, e.page_count)
            except ValueError as exc:
                e.page_indices, error = None, str(exc)
        self.pages_error.setText(error)
        self.pages_error.setVisible(bool(error))
        self._refresh_item(items[0])
        self.entries_changed()

    # -- state -------------------------------------------------------------
    def _invalid_entries(self) -> list[MergeEntry]:
        bad = []
        for e in self.entries():
            if e.ranges.strip():
                try:
                    parse_page_ranges(e.ranges, e.page_count)
                except ValueError:
                    bad.append(e)
        return bad

    def entries_changed(self) -> None:
        entries = self.entries()
        files, pages = len(entries), sum(e.selected_count for e in entries)
        self.total_label.setText("" if not files else tr("merge.total.one", pages=pages) if files == 1
                                 else tr("merge.total", files=files, pages=pages))
        self.run_btn.setEnabled(files > 0 and pages > 0 and not self._invalid_entries())
        self.list.viewport().update()
        self._selection_changed()

    def _selection_changed(self) -> None:
        selected = self.list.selectedItems()
        rows = sorted(self.list.row(i) for i in selected)
        self.remove_btn.setEnabled(bool(selected))
        self.up_btn.setEnabled(bool(rows) and rows[0] > 0)
        self.down_btn.setEnabled(bool(rows) and rows[-1] < self.list.count() - 1)
        e = self._current_entry()
        for w in (self.file_name, self.file_info, self.pages_label, self.pages):
            w.setVisible(e is not None)
        self.no_selection.setVisible(e is None)
        if e is None:
            self.pages_error.setVisible(False)
            return
        self.file_name.setText(e.name)
        self.file_info.setText(tr("merge.row.pages_one") if e.page_count == 1
                               else tr("merge.row.pages", n=e.page_count))
        if self.pages.text() != e.ranges:
            self.pages.setText(e.ranges)
        invalid = e in self._invalid_entries()
        self.pages_error.setVisible(invalid and bool(self.pages_error.text()))

    # -- drops onto the dialog ----------------------------------------------
    def dragEnterEvent(self, ev) -> None:  # noqa: N802
        if ev.mimeData().hasUrls():
            ev.acceptProposedAction()

    def dropEvent(self, ev) -> None:  # noqa: N802
        self.add_paths([u.toLocalFile() for u in ev.mimeData().urls() if u.isLocalFile()])

    # -- result --------------------------------------------------------------
    def _run(self) -> None:
        entries = self.entries()
        if not entries:
            return
        QGuiApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
        try:
            self.result_bytes = merge_files([e.source() for e in entries], add_bookmarks=self.bookmarks.isChecked())
        except Exception as exc:  # noqa: BLE001
            QGuiApplication.restoreOverrideCursor()
            show_error(self, tr("merge.error.title"), str(exc))
            return
        QGuiApplication.restoreOverrideCursor()
        self.result_title = tr("merge.result_title")
        self.accept()

