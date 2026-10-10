"""Images (screenshots, photos) -> one PDF.

Images are collected in a reorderable thumbnail grid: drop files, pick them
with a file dialog or paste a screenshot from the clipboard (Ctrl+V). Page
options are shown next to a live preview of the selected image's page.
The result is exposed as ``result_bytes`` / ``result_title`` – the main
window opens it as a new (unsaved) document.
"""

from __future__ import annotations

import os
from dataclasses import dataclass

import pymupdf
from PySide6.QtCore import QBuffer, QByteArray, QIODevice, QRectF, QSize, Qt, QTimer
from PySide6.QtGui import QFont, QGuiApplication, QImage, QImageReader, QKeySequence, QPainter, QPixmap, QShortcut
from PySide6.QtWidgets import (
    QAbstractItemView,
    QComboBox,
    QFileDialog,
    QFormLayout,
    QFrame,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QPushButton,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from ...core.images import SUPPORTED_IMAGE_EXTENSIONS, ImagePageOptions, images_to_pdf
from ...i18n import tr
from ..theme import tokens as T
from ..theme.icons import icon
from ..viewer.render_util import to_qimage
from ..widgets.segmented import Segment, SegmentedControl
from ..widgets.util import label, on_theme_change
from .common import FolioDialog, show_error

THUMB = 132
PREVIEW_W, PREVIEW_H = 220, 250
PAGE_SIZES = ("fit", "A4", "A3", "A5", "Letter", "Legal")
SOURCE_ROLE = Qt.ItemDataRole.UserRole


@dataclass
class ImageSource:
    """One image in the list: a file on disk or raw bytes (clipboard)."""

    name: str
    path: str | None = None
    data: bytes | None = None

    @property
    def payload(self) -> str | bytes:
        return self.path if self.path is not None else self.data  # type: ignore[return-value]


def _is_supported(path: str) -> bool:
    return os.path.splitext(path)[1].lower() in SUPPORTED_IMAGE_EXTENSIONS


def _thumbnail(src: ImageSource, size: int) -> QImage | None:
    """Fast, EXIF-aware, downscaled decode (never loads full-size photos)."""
    if src.path is not None:
        reader = QImageReader(src.path)
    else:
        buf = QBuffer()
        buf.setData(QByteArray(src.data or b""))
        buf.open(QIODevice.OpenModeFlag.ReadOnly)
        reader = QImageReader(buf)
        reader._buf = buf  # keep the buffer alive while reading
    reader.setAutoTransform(True)
    full = reader.size()
    if full.isValid() and (full.width() > size or full.height() > size):
        reader.setScaledSize(full.scaled(size, size, Qt.AspectRatioMode.KeepAspectRatio))
    img = reader.read()
    return None if img.isNull() else img


class ImageList(QListWidget):
    """Thumbnail grid with internal drag-reorder, external file drops and an
    empty-state hint."""

    def __init__(self, dialog: "ImagesToPdfDialog") -> None:
        super().__init__()
        self.dialog = dialog
        self.setViewMode(QListWidget.ViewMode.IconMode)
        self.setIconSize(QSize(THUMB, THUMB))
        self.setGridSize(QSize(THUMB + 28, THUMB + 44))
        self.setResizeMode(QListWidget.ResizeMode.Adjust)
        self.setMovement(QListWidget.Movement.Snap)
        self.setDragDropMode(QAbstractItemView.DragDropMode.InternalMove)
        self.setDefaultDropAction(Qt.DropAction.MoveAction)
        self.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.setWordWrap(True)
        self.setTextElideMode(Qt.TextElideMode.ElideMiddle)
        self.setUniformItemSizes(True)
        self.setAcceptDrops(True)
        self.setMinimumSize(420, 380)
        self.setObjectName("ImageGrid")
        self.setSpacing(6)

    # external drops (files from Explorer) + internal reordering
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
            paths = [u.toLocalFile() for u in ev.mimeData().urls() if u.isLocalFile()]
            ev.acceptProposedAction()
            self.dialog.add_paths(paths)
        else:
            super().dropEvent(ev)
            self.dialog.sources_changed()

    def paintEvent(self, ev) -> None:  # noqa: N802
        super().paintEvent(ev)
        if self.count():
            return
        p = QPainter(self.viewport())
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        r = QRectF(self.viewport().rect())
        ic = icon("image", "text_subtle").pixmap(40, 40)
        cy = r.center().y()
        p.drawPixmap(int(r.center().x() - 20), int(cy - 58), ic)
        f = QFont(self.font())  # app font may be pixel-sized: set sizes explicitly
        f.setPixelSize(15)
        f.setWeight(QFont.Weight.DemiBold)
        p.setFont(f)
        p.setPen(T.qcolor("text"))
        p.drawText(QRectF(r.left(), cy - 8, r.width(), 24), Qt.AlignmentFlag.AlignCenter, tr("images.empty.title"))
        f.setPixelSize(13)
        f.setWeight(QFont.Weight.Normal)
        p.setFont(f)
        p.setPen(T.qcolor("text_muted"))
        p.drawText(QRectF(r.left(), cy + 18, r.width(), 20), Qt.AlignmentFlag.AlignCenter, tr("images.empty.text"))
        p.end()


class ImagesToPdfDialog(FolioDialog):
    def __init__(self, parent: QWidget | None, initial: list[str] | None = None) -> None:
        super().__init__(parent, tr("images.title"), tr("images.subtitle"), icon_name="image", width=880)
        self.result_bytes: bytes | None = None
        self.result_title: str | None = None
        self._clip_counter = 0
        self._preview_timer = QTimer(self, singleShot=True, interval=120)
        self._preview_timer.timeout.connect(self._render_preview)

        row = QHBoxLayout()
        row.setSpacing(20)
        self.body.addLayout(row, 1)

        # -- left: list + list actions ----------------------------------
        left = QVBoxLayout()
        left.setSpacing(10)
        actions = QHBoxLayout()
        actions.setSpacing(8)
        self.add_btn = self._tool_button(tr("images.add"), "plus", self.choose_files)
        self.paste_btn = self._tool_button(tr("images.paste"), "clipboard-text", self.paste_clipboard)
        self.paste_btn.setToolTip(tr("images.paste.tip"))
        actions.addWidget(self.add_btn)
        actions.addWidget(self.paste_btn)
        actions.addStretch(1)
        self.sort_btn = self._tool_button(tr("images.sort"), "sort-ascending", self.sort_by_name, ghost=True)
        self.remove_btn = self._tool_button(tr("images.remove"), "trash", self.remove_selected, ghost=True)
        actions.addWidget(self.sort_btn)
        actions.addWidget(self.remove_btn)
        left.addLayout(actions)
        self.list = ImageList(self)
        self.list.itemSelectionChanged.connect(self._selection_changed)
        left.addWidget(self.list, 1)
        row.addLayout(left, 1)

        # -- right: options + preview -----------------------------------
        side = QFrame()
        side.setObjectName("Card")
        side.setFixedWidth(280)
        sl = QVBoxLayout(side)
        sl.setContentsMargins(16, 14, 16, 16)
        sl.setSpacing(10)
        sl.addWidget(label(tr("images.section.page"), "section"))
        form = QFormLayout()
        form.setSpacing(8)
        form.setLabelAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
        self.page_size = QComboBox()
        for key in PAGE_SIZES:
            self.page_size.addItem(tr("images.page_size.fit") if key == "fit" else key, key)
        self.page_size.currentIndexChanged.connect(self._options_changed)
        form.addRow(tr("images.page_size"), self.page_size)
        self.margin = QSpinBox()
        self.margin.setRange(0, 50)
        self.margin.setSuffix(" mm")
        self.margin.valueChanged.connect(self._options_changed)
        form.addRow(tr("images.margin"), self.margin)
        sl.addLayout(form)
        self.orientation_label = label(tr("images.orientation"), "caption")
        sl.addWidget(self.orientation_label)
        self.orientation = SegmentedControl([
            Segment("auto", tr("images.orientation.auto")),
            Segment("portrait", tr("images.orientation.portrait")),
            Segment("landscape", tr("images.orientation.landscape")),
        ], expand=True, height=30, min_seg=48)
        self.orientation.currentChanged.connect(self._options_changed)
        sl.addWidget(self.orientation)
        self.scale_label = label(tr("images.scale"), "caption")
        sl.addWidget(self.scale_label)
        self.scale = SegmentedControl([
            Segment("contain", tr("images.scale.contain")),
            Segment("cover", tr("images.scale.cover")),
            Segment("stretch", tr("images.scale.stretch")),
        ], expand=True, height=30, min_seg=48)
        self.scale.currentChanged.connect(self._options_changed)
        sl.addWidget(self.scale)
        sl.addSpacing(6)
        sl.addWidget(label(tr("images.preview"), "section"))
        self.preview = QLabel()
        self.preview.setFixedSize(PREVIEW_W + 24, PREVIEW_H)
        self.preview.setAlignment(Qt.AlignmentFlag.AlignCenter)
        sl.addWidget(self.preview, 0, Qt.AlignmentFlag.AlignHCenter)
        sl.addStretch(1)
        row.addWidget(side)

        # -- footer ------------------------------------------------------
        self.count_label = label("", "muted")
        self.buttons.insertWidget(0, self.count_label)
        self.add_button(tr("images.cancel"), "secondary", "cancel")
        self.create_btn = self.add_button(tr("images.create"), "primary", default=True)
        self.create_btn.clicked.connect(self._create)

        QShortcut(QKeySequence.StandardKey.Paste, self, activated=self.paste_clipboard)
        QShortcut(QKeySequence.StandardKey.Delete, self.list, activated=self.remove_selected)
        on_theme_change(self, self._render_preview)
        self.setAcceptDrops(True)
        if initial:
            self.add_paths(initial)
        self.sources_changed()
        self._options_changed()

    # -- helpers ----------------------------------------------------------
    def _tool_button(self, text: str, icon_name: str, slot, *, ghost: bool = False) -> QPushButton:
        b = QPushButton(icon(icon_name, "text"), text)
        if ghost:
            b.setProperty("variant", "ghost")
        b.setCursor(Qt.CursorShape.PointingHandCursor)
        b.setAutoDefault(False)
        b.clicked.connect(slot)
        return b

    def sources(self) -> list[ImageSource]:
        return [self.list.item(i).data(SOURCE_ROLE) for i in range(self.list.count())]

    def options(self) -> ImagePageOptions:
        return ImagePageOptions(
            page_size=self.page_size.currentData(),
            orientation=self.orientation.current_key(),
            margin_mm=float(self.margin.value()),
            scale=self.scale.current_key(),
        )

    # -- adding / removing -------------------------------------------------
    def _add_source(self, src: ImageSource) -> bool:
        thumb = _thumbnail(src, THUMB * 2)
        if thumb is None:
            return False
        item = QListWidgetItem(QPixmap.fromImage(thumb), src.name)
        item.setData(SOURCE_ROLE, src)
        item.setToolTip(src.path or src.name)
        item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsDropEnabled)
        self.list.addItem(item)
        return True

    def add_paths(self, paths: list[str]) -> None:
        bad = []
        added = False
        for path in paths:
            if not os.path.isfile(path) or not _is_supported(path):
                continue
            if self._add_source(ImageSource(os.path.basename(path), path=path)):
                added = True
            else:
                bad.append(os.path.basename(path))
        if bad:
            self._toast(tr("images.unreadable", name=", ".join(bad)), "error")
        if added:
            self.list.setCurrentRow(self.list.count() - 1)
        self.sources_changed()

    def choose_files(self) -> None:
        patterns = " ".join(f"*{ext}" for ext in SUPPORTED_IMAGE_EXTENSIONS)
        start = os.path.dirname(self.sources()[-1].path or "") if self.list.count() and self.sources()[-1].path else ""
        paths, _ = QFileDialog.getOpenFileNames(self, tr("images.file_dialog"), start,
                                                tr("images.file_filter", patterns=patterns))
        if paths:
            self.add_paths(paths)

    def paste_clipboard(self) -> None:
        mime = QGuiApplication.clipboard().mimeData()
        if mime is not None and mime.hasUrls():
            files = [u.toLocalFile() for u in mime.urls() if u.isLocalFile()]
            if files:
                self.add_paths(files)
                return
        img = QGuiApplication.clipboard().image()
        if img.isNull():
            self._toast(tr("images.clipboard_empty"), "info")
            return
        self.add_image(img)

    def add_image(self, img: QImage) -> None:
        """Add an in-memory image (e.g. a pasted screenshot) losslessly as PNG."""
        buf = QBuffer()
        buf.open(QIODevice.OpenModeFlag.WriteOnly)
        img.save(buf, "PNG")
        self._clip_counter += 1
        name = tr("images.clipboard_name", n=self._clip_counter) + ".png"
        if self._add_source(ImageSource(name, data=bytes(buf.data()))):
            self.list.setCurrentRow(self.list.count() - 1)
        self.sources_changed()

    def remove_selected(self) -> None:
        for item in self.list.selectedItems():
            self.list.takeItem(self.list.row(item))
        self.sources_changed()

    def sort_by_name(self) -> None:
        self.list.sortItems(Qt.SortOrder.AscendingOrder)
        self.sources_changed()

    def _toast(self, text: str, kind: str) -> None:
        host = self.parent()
        if host is not None and hasattr(host, "toast"):
            host.toast(text, kind)

    # -- state updates -----------------------------------------------------
    def sources_changed(self) -> None:
        n = self.list.count()
        self.count_label.setText("" if n == 0 else tr("images.count.one") if n == 1 else tr("images.count", n=n))
        self.create_btn.setEnabled(n > 0)
        self.remove_btn.setEnabled(bool(self.list.selectedItems()))
        self.sort_btn.setEnabled(n > 1)
        self.list.viewport().update()
        self._preview_timer.start()

    def _selection_changed(self) -> None:
        self.remove_btn.setEnabled(bool(self.list.selectedItems()))
        self._preview_timer.start()

    def _options_changed(self, *_args) -> None:
        paper = self.page_size.currentData() != "fit"
        for w in (self.orientation_label, self.orientation, self.scale_label, self.scale):
            w.setVisible(paper)
        self._preview_timer.start()

    def _preview_source(self) -> ImageSource | None:
        item = self.list.currentItem() or (self.list.item(0) if self.list.count() else None)
        return item.data(SOURCE_ROLE) if item is not None else None

    def _render_preview(self) -> None:
        src = self._preview_source()
        if src is None:
            self.preview.setPixmap(QPixmap())
            return
        try:
            doc = pymupdf.open("pdf", images_to_pdf([src.payload], self.options()))
            page = doc[0]
            zoom = min(PREVIEW_W / page.rect.width, (PREVIEW_H - 16) / page.rect.height)
            dpr = self.devicePixelRatioF()
            img = to_qimage(page.get_pixmap(matrix=pymupdf.Matrix(zoom * dpr, zoom * dpr)))
            doc.close()
        except Exception:  # noqa: BLE001 - preview must never break the dialog
            self.preview.setPixmap(QPixmap())
            return
        img.setDevicePixelRatio(dpr)
        w, h = img.width() / dpr, img.height() / dpr
        canvas = QPixmap(QSize(int((w + 16) * dpr), int((h + 16) * dpr)))
        canvas.setDevicePixelRatio(dpr)
        canvas.fill(Qt.GlobalColor.transparent)
        p = QPainter(canvas)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        shadow = T.qcolor("shadow")
        for i, a in ((4, 0.035), (2, 0.06)):  # soft, barely-there drop shadow
            shadow.setAlphaF(a)
            p.fillRect(QRectF(8 - i, 9 - i + 1, w + 2 * i, h + 2 * i), shadow)
        p.drawImage(QRectF(8, 8, w, h), img)
        p.end()
        self.preview.setPixmap(canvas)

    # -- drops onto the whole dialog ----------------------------------------
    def dragEnterEvent(self, ev) -> None:  # noqa: N802
        if ev.mimeData().hasUrls():
            ev.acceptProposedAction()

    def dropEvent(self, ev) -> None:  # noqa: N802
        self.add_paths([u.toLocalFile() for u in ev.mimeData().urls() if u.isLocalFile()])

    # -- result ----------------------------------------------------------
    def _create(self) -> None:
        sources = self.sources()
        if not sources:
            return
        QGuiApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
        try:
            self.result_bytes = images_to_pdf([s.payload for s in sources], self.options())
        except Exception as exc:  # noqa: BLE001
            QGuiApplication.restoreOverrideCursor()
            show_error(self, tr("images.error.title"), str(exc))
            return
        QGuiApplication.restoreOverrideCursor()
        first = sources[0]
        self.result_title = (os.path.splitext(first.name)[0] + ".pdf") if len(sources) == 1 and first.path \
            else tr("images.result_title")
        self.accept()
