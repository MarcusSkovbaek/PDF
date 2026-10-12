""""Tilføj billede" – place an image (file or clipboard screenshot) on a page.

Pick the image in the inspector (file or clipboard), then click on the page
to place it at a natural size, or drag a frame to fit it into. Without a
picked image, clicking opens the file dialog first. Saving calls
:func:`pdfstudio.core.insert.add_image` (aspect ratio kept by default).
"""

from __future__ import annotations

import os

import pymupdf
from PySide6.QtCore import QBuffer, QIODevice, QPointF, QRectF, QSize, Qt
from PySide6.QtGui import QGuiApplication, QImage, QImageReader, QPen, QPixmap
from PySide6.QtWidgets import (
    QCheckBox,
    QFileDialog,
    QFrame,
    QGraphicsRectItem,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from ....core import insert as core_insert
from ....core.images import SUPPORTED_IMAGE_EXTENSIONS
from ....i18n import tr
from ...theme import tokens as T
from ...theme.icons import icon
from ...widgets.util import label
from .base import Tool, ToolMouseEvent, ToolRegistry

MIN_DRAG = 8.0        # pt
MAX_PAGE_SHARE = 0.6  # click-placed images are at most 60 % of the page width

# Test hook: path returned instead of showing the file dialog.
_AUTO_FILE: str | None = None


def _natural_size(data: str | bytes) -> tuple[float, float] | None:
    """Image size in pt at 96 dpi (EXIF orientation applied)."""
    if isinstance(data, str):
        reader = QImageReader(data)
    else:
        buf = QBuffer()
        buf.setData(bytes(data))
        buf.open(QIODevice.OpenModeFlag.ReadOnly)
        reader = QImageReader(buf)
        reader._buf = buf
    reader.setAutoTransform(True)
    size = reader.size()
    if not size.isValid():
        img = reader.read()
        if img.isNull():
            return None
        size = img.size()
    return size.width() * 72.0 / 96.0, size.height() * 72.0 / 96.0


@ToolRegistry.register
class AddImageTool(Tool):
    id = "edit.add_image"
    label = tr("edit.add_image.label")
    hint = tr("edit.add_image.hint")
    icon = "image"
    shortcut = "I"
    mode = "edit"
    order = 30
    cursor = Qt.CursorShape.CrossCursor

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.pending: str | bytes | None = None
        self.pending_name = ""
        self.keep_aspect = True
        self._press: tuple[int, QPointF] | None = None
        self._item: QGraphicsRectItem | None = None
        self._panel: QWidget | None = None

    # -- choosing the image ---------------------------------------------------
    def choose_file(self) -> bool:
        if _AUTO_FILE is not None:
            path = _AUTO_FILE
        else:
            patterns = " ".join(f"*{e}" for e in SUPPORTED_IMAGE_EXTENSIONS)
            path, _ = QFileDialog.getOpenFileName(self.ctx.view.window(), tr("edit.add_image.dialog"), "",
                                                  tr("edit.add_image.filter", patterns=patterns))
        if not path:
            return False
        if _natural_size(path) is None:
            self.ctx.toast(tr("edit.add_image.unreadable", name=os.path.basename(path)), "error")
            return False
        self.pending, self.pending_name = path, os.path.basename(path)
        self._refresh_panel()
        return True

    def from_clipboard(self) -> bool:
        img = QGuiApplication.clipboard().image()
        if img.isNull():
            self.ctx.toast(tr("edit.add_image.clipboard_empty"), "info")
            return False
        self.set_image(img)
        return True

    def set_image(self, img: QImage) -> None:
        """Use an in-memory image (stored losslessly as PNG)."""
        buf = QBuffer()
        buf.open(QIODevice.OpenModeFlag.WriteOnly)
        img.save(buf, "PNG")
        self.pending, self.pending_name = bytes(buf.data()), tr("edit.add_image.clipboard_name")
        self._refresh_panel()

    # -- placing ------------------------------------------------------------------
    def mouse_press(self, ev: ToolMouseEvent) -> None:
        if not ev.left or ev.page < 0:
            return
        self._press = (ev.page, QPointF(ev.pos))
        self._item = QGraphicsRectItem()
        pen = QPen(T.qcolor("accent"), 1.2, Qt.PenStyle.DashLine)
        pen.setCosmetic(True)
        self._item.setPen(pen)
        self._item.setZValue(40)
        self.ctx.view.add_overlay(ev.page, self._item)

    def mouse_move(self, ev: ToolMouseEvent) -> None:
        if self._press is None or self._item is None:
            return
        page, start = self._press
        self._item.setRect(QRectF(start, ev.pos_on(page)).normalized())

    def mouse_release(self, ev: ToolMouseEvent) -> None:
        if self._press is None:
            return
        page, start = self._press
        self._press = None
        if self._item is not None:
            self.ctx.view.remove_overlay(self._item)
            self._item = None
        r = QRectF(start, ev.pos_on(page)).normalized()
        if self.pending is None and not self.choose_file():
            return
        if r.width() >= MIN_DRAG and r.height() >= MIN_DRAG:
            rect = (r.left(), r.top(), r.right(), r.bottom())
        else:
            rect = self._natural_rect(page, start)
            if rect is None:
                return
        try:
            core_insert.add_image(self.ctx.pdf, page, rect, self.pending, keep_aspect=self.keep_aspect)
        except Exception as exc:  # noqa: BLE001
            self.ctx.toast(tr("edit.add_image.error", err=str(exc)), "error")

    def _natural_rect(self, page: int, at: QPointF):
        size = _natural_size(self.pending)
        if size is None:
            return None
        w, h = size
        pr = self.ctx.pdf.page(page).rect
        s = min(1.0, MAX_PAGE_SHARE * pr.width / w, MAX_PAGE_SHARE * pr.height / h)
        w, h = w * s, h * s
        x = min(max(pr.x0, at.x() - w / 2), pr.x1 - w)   # centred on the click, kept on the page
        y = min(max(pr.y0, at.y() - h / 2), pr.y1 - h)
        return (x, y, x + w, y + h)

    def deactivate(self) -> None:
        if self._item is not None and self.ctx is not None:
            self.ctx.view.remove_overlay(self._item)
        self._item = None
        self._press = None
        super().deactivate()

    # -- inspector ------------------------------------------------------------------
    def properties_widget(self) -> QWidget:
        if self._panel is None:
            w = QWidget()
            v = QVBoxLayout(w)
            v.setContentsMargins(16, 4, 16, 16)
            v.setSpacing(10)
            card = QFrame()
            card.setObjectName("Card")
            cl = QVBoxLayout(card)
            cl.setContentsMargins(14, 12, 14, 12)
            cl.setSpacing(6)
            self._title = label("", "heading")
            self._detail = label("", "muted", wrap=True)
            self._preview = QLabel()
            self._preview.setAlignment(Qt.AlignmentFlag.AlignCenter)
            cl.addWidget(self._title)
            cl.addWidget(self._detail)
            cl.addWidget(self._preview)
            v.addWidget(card)
            row = QHBoxLayout()
            row.setSpacing(8)
            pick = QPushButton(icon("folder-open", "text"), tr("edit.add_image.choose"))
            pick.clicked.connect(self.choose_file)
            paste = QPushButton(icon("clipboard-text", "text"), tr("edit.add_image.paste"))
            paste.clicked.connect(self.from_clipboard)
            for b in (pick, paste):
                b.setCursor(Qt.CursorShape.PointingHandCursor)
                row.addWidget(b)
            v.addLayout(row)
            keep = QCheckBox(tr("edit.add_image.keep_aspect"))
            keep.setChecked(self.keep_aspect)
            keep.toggled.connect(lambda on: setattr(self, "keep_aspect", on))
            v.addWidget(keep)
            v.addStretch(1)
            self._panel = w
        self._refresh_panel()
        return self._panel

    def _refresh_panel(self) -> None:
        if self._panel is None:
            return
        if self.pending is None:
            self._title.setText(tr("edit.add_image.none"))
            self._detail.setText(tr("edit.add_image.none.hint"))
            self._preview.clear()
            self._preview.hide()
            return
        self._title.setText(self.pending_name)
        self._detail.setText(tr("edit.add_image.ready"))
        img = QImage()
        if isinstance(self.pending, str):
            img.load(self.pending)
        else:
            img.loadFromData(self.pending)
        if not img.isNull():
            pm = QPixmap.fromImage(img.scaled(QSize(220, 140), Qt.AspectRatioMode.KeepAspectRatio,
                                              Qt.TransformationMode.SmoothTransformation))
            self._preview.setPixmap(pm)
            self._preview.show()
