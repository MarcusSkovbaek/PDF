"""Page thumbnails: sidebar strip and the large "Sider" organiser grid.

Lazy: a thumbnail is rendered only when its cell is painted (i.e. visible),
time-sliced on the GUI thread. Supports multi-selection, drag & drop
reordering (``core.pages.move_pages``), dropping PDF/image files to insert
them (``core.pages.insert_pdf``) and a context menu built from the page
actions registered by the workspace.
"""

from __future__ import annotations

import os
import time

from PySide6.QtCore import (
    QAbstractListModel,
    QByteArray,
    QItemSelectionModel,
    QMimeData,
    QModelIndex,
    QPoint,
    QRect,
    QRectF,
    QSize,
    Qt,
    QTimer,
    Signal,
)
from PySide6.QtGui import QColor, QFont, QPainter, QPainterPath, QPen
from PySide6.QtWidgets import (
    QAbstractItemView,
    QListView,
    QMenu,
    QStyle,
    QStyledItemDelegate,
    QStyleOptionViewItem,
    QVBoxLayout,
    QWidget,
)

from ...i18n import tr
from ..theme import tokens as T
from ..viewer.page_view import paint_soft_shadow
from ..viewer.render_util import page_size, render_qpixmap

MIME = "application/x-folio-pages"
PixmapRole = Qt.ItemDataRole.UserRole + 1
SizeRole = Qt.ItemDataRole.UserRole + 2
LABEL_H = 24
PAD = 10


class ThumbRenderer:
    """Per-document thumbnail cache with a time-sliced render queue."""

    def __init__(self, model: "ThumbModel") -> None:
        self.model = model
        self.width = 120
        self.dpr = 1.0
        self._cache: dict[int, tuple] = {}   # page -> (QPixmap, width, stale)
        self._queue: list[int] = []
        self._timer = QTimer()
        self._timer.setSingleShot(True)
        self._timer.setInterval(0)
        self._timer.timeout.connect(self._step)

    def get(self, page: int):
        hit = self._cache.get(page)
        if hit is None or hit[1] != self.width or hit[2]:
            self.request(page)
        return hit[0] if hit else None

    def request(self, page: int) -> None:
        if page not in self._queue:
            self._queue.append(page)
        if not self._timer.isActive():
            self._timer.start()

    def invalidate(self, pages=None) -> None:
        if pages is None:
            for k, v in list(self._cache.items()):
                self._cache[k] = (v[0], v[1], True)
        else:
            for p in pages:
                if p in self._cache:
                    v = self._cache[p]
                    self._cache[p] = (v[0], v[1], True)

    def clear(self) -> None:
        self._cache.clear()
        self._queue.clear()

    def _step(self) -> None:
        deadline = time.perf_counter() + 0.025
        pdf = self.model.pdf
        while self._queue and time.perf_counter() < deadline:
            page = self._queue.pop()   # LIFO: most recently painted first
            if page >= pdf.page_count:
                continue
            hit = self._cache.get(page)
            if hit is not None and hit[1] == self.width and not hit[2]:
                continue
            try:
                w, h = page_size(pdf.doc, page)
                box_h = self.width * 1.42
                zoom = min(self.width / w, box_h / h)
                pm = render_qpixmap(pdf.doc, page, zoom, self.dpr)
            except Exception:
                continue
            self._cache[page] = (pm, self.width, False)
            self.model.page_rendered(page)
        if self._queue:
            self._timer.start()


class ThumbModel(QAbstractListModel):
    def __init__(self, pdf, parent=None) -> None:
        super().__init__(parent)
        self.pdf = pdf
        self.renderer = ThumbRenderer(self)
        self.current_page = 0
        self._count = pdf.page_count

    def rowCount(self, parent=QModelIndex()) -> int:  # noqa: N802
        return 0 if parent.isValid() else self._count

    def data(self, index: QModelIndex, role=Qt.ItemDataRole.DisplayRole):
        if not index.isValid():
            return None
        row = index.row()
        if role == Qt.ItemDataRole.DisplayRole:
            return str(row + 1)
        if role == Qt.ItemDataRole.ToolTipRole:
            return tr("shell.thumbs.page_n", n=row + 1)
        if role == PixmapRole:
            return self.renderer.get(row)
        if role == SizeRole:
            try:
                return page_size(self.pdf.doc, row)
            except Exception:
                return (595.0, 842.0)
        return None

    def flags(self, index: QModelIndex):
        base = Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable | Qt.ItemFlag.ItemIsDragEnabled
        if not index.isValid():
            return Qt.ItemFlag.ItemIsDropEnabled
        return base

    def supportedDropActions(self):  # noqa: N802
        return Qt.DropAction.MoveAction | Qt.DropAction.CopyAction

    def supportedDragActions(self):  # noqa: N802
        return Qt.DropAction.MoveAction

    def mimeTypes(self):  # noqa: N802
        return [MIME, "text/uri-list"]

    def mimeData(self, indexes):  # noqa: N802
        rows = sorted({i.row() for i in indexes})
        md = QMimeData()
        md.setData(MIME, QByteArray(",".join(map(str, rows)).encode()))
        return md

    def reset(self) -> None:
        self.beginResetModel()
        self._count = self.pdf.page_count
        self.renderer.clear()
        self.endResetModel()

    def page_rendered(self, page: int) -> None:
        if page < self._count:
            idx = self.index(page)
            self.dataChanged.emit(idx, idx, [PixmapRole])

    def refresh_pages(self, pages=None) -> None:
        self.renderer.invalidate(pages)
        if self._count:
            if pages is None:
                self.dataChanged.emit(self.index(0), self.index(self._count - 1), [PixmapRole])
            else:
                for p in pages:
                    if 0 <= p < self._count:
                        self.dataChanged.emit(self.index(p), self.index(p), [PixmapRole])


class ThumbDelegate(QStyledItemDelegate):
    def __init__(self, view: "ThumbnailView") -> None:
        super().__init__(view)
        self.view = view

    def sizeHint(self, option, index) -> QSize:  # noqa: N802
        return self.view.cell_size()

    def paint(self, p: QPainter, option: QStyleOptionViewItem, index: QModelIndex) -> None:
        p.save()
        p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        p.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, True)
        cell = QRectF(option.rect)
        tw = self.view.thumb_width
        box_h = tw * 1.42
        w, h = index.data(SizeRole) or (595.0, 842.0)
        s = min(tw / w, box_h / h)
        pw, ph = round(w * s), round(h * s)
        x = cell.x() + (cell.width() - pw) / 2
        y = cell.y() + PAD + (box_h - ph) / 2
        page_r = QRectF(round(x), round(y), pw, ph)
        selected = bool(option.state & QStyle.StateFlag.State_Selected)
        hovered = bool(option.state & QStyle.StateFlag.State_MouseOver)
        model = index.model()
        is_current = index.row() == getattr(model, "current_page", -1)

        if selected or hovered:
            bg = QPainterPath()
            bg.addRoundedRect(cell.adjusted(3, 2, -3, -2), 10, 10)
            p.fillPath(bg, T.qcolor("selected" if selected else "hover"))
        paint_soft_shadow(p, page_r, 2, T.qcolor("shadow_strong"), 1)
        paint_soft_shadow(p, page_r, 8, T.qcolor("shadow"), 2)
        p.fillRect(page_r, T.qcolor("page"))
        pm = index.data(PixmapRole)
        if pm is not None:
            p.drawPixmap(page_r, pm, QRectF(pm.rect()))
        if selected:
            ring = QPainterPath()
            ring.addRoundedRect(page_r.adjusted(-3, -3, 3, 3), 4, 4)
            p.setPen(QPen(T.qcolor("accent"), 2))
            p.setBrush(Qt.BrushStyle.NoBrush)
            p.drawPath(ring)
        elif T.current().is_dark:
            p.setPen(QPen(QColor(255, 255, 255, 20), 1))
            p.drawRect(page_r.adjusted(-0.5, -0.5, 0.5, 0.5))

        # page number pill
        f = QFont(option.font)
        f.setPixelSize(T.FONT_SIZE_XS)
        f.setWeight(QFont.Weight.DemiBold if (is_current or selected) else QFont.Weight.Medium)
        p.setFont(f)
        label = index.data(Qt.ItemDataRole.DisplayRole)
        fm = p.fontMetrics()
        lw = max(22, fm.horizontalAdvance(label) + 12)
        lr = QRectF(cell.center().x() - lw / 2, cell.y() + PAD + box_h + 6, lw, 18)
        if is_current:
            pill = QPainterPath()
            pill.addRoundedRect(lr, 9, 9)
            p.fillPath(pill, T.qcolor("accent"))
            p.setPen(T.qcolor("text_on_accent"))
        else:
            p.setPen(T.qcolor("text" if selected else "text_muted"))
        p.drawText(lr, Qt.AlignmentFlag.AlignCenter, label)
        p.restore()


class ThumbnailView(QListView):
    pageActivated = Signal(int)
    selectionChangedPages = Signal(list)

    def __init__(self, workspace, *, grid: bool = False, thumb_width: int = 116, parent=None) -> None:
        super().__init__(parent)
        self.workspace = workspace
        self.grid = grid
        self.thumb_width = thumb_width
        self._drop_row = -1
        self._sync = False
        self.model_ = ThumbModel(workspace.pdf, self)
        self.setModel(self.model_)
        self.setItemDelegate(ThumbDelegate(self))
        self.setViewMode(QListView.ViewMode.IconMode)
        self.setFlow(QListView.Flow.LeftToRight)
        self.setWrapping(True)
        self.setResizeMode(QListView.ResizeMode.Adjust)
        self.setMovement(QListView.Movement.Static)
        self.setUniformItemSizes(True)
        self.setSpacing(4 if not grid else 10)
        self.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.setDragDropMode(QAbstractItemView.DragDropMode.DragDrop)
        self.setDragEnabled(True)
        self.setAcceptDrops(True)
        self.setDropIndicatorShown(False)
        self.setDefaultDropAction(Qt.DropAction.MoveAction)
        self.setMouseTracking(True)
        self.setVerticalScrollMode(QAbstractItemView.ScrollMode.ScrollPerPixel)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.customContextMenuRequested.connect(self._context_menu)
        self.clicked.connect(lambda idx: self.pageActivated.emit(idx.row()))
        self.activated.connect(lambda idx: self.pageActivated.emit(idx.row()))
        self.selectionModel().selectionChanged.connect(
            lambda *_: self.selectionChangedPages.emit(self.selected_pages()))
        self.setStyleSheet("QListView::item, QListView::item:hover, QListView::item:selected"
                           " { background: transparent; border: none; padding: 0px; }")
        self._update_renderer_width()

    # -- sizing ---------------------------------------------------------------
    def cell_size(self) -> QSize:
        tw = self.thumb_width
        return QSize(tw + 2 * PAD + (8 if self.grid else 0), int(tw * 1.42) + PAD + LABEL_H + 10)

    def set_thumb_width(self, w: int) -> None:
        self.thumb_width = int(w)
        self._update_renderer_width()
        self.model_.layoutChanged.emit()
        self.scheduleDelayedItemsLayout()

    def _update_renderer_width(self) -> None:
        r = self.model_.renderer
        r.width = self.thumb_width
        try:
            r.dpr = max(1.0, self.devicePixelRatioF())
        except Exception:
            r.dpr = 1.0

    def showEvent(self, ev) -> None:  # noqa: N802
        self._update_renderer_width()
        super().showEvent(ev)

    # -- state ----------------------------------------------------------------
    def reset_pages(self) -> None:
        self.model_.pdf = self.workspace.pdf
        self.model_.reset()

    def refresh_pages(self, pages=None) -> None:
        self.model_.refresh_pages(pages)

    def selected_pages(self) -> list[int]:
        return sorted({i.row() for i in self.selectionModel().selectedIndexes()})

    def select_pages(self, pages) -> None:
        sm = self.selectionModel()
        sm.clearSelection()
        for p in pages:
            if 0 <= p < self.model_.rowCount():
                sm.select(self.model_.index(p), QItemSelectionModel.SelectionFlag.Select)

    def set_current_page(self, page: int) -> None:
        old = self.model_.current_page
        self.model_.current_page = page
        for r in (old, page):
            if 0 <= r < self.model_.rowCount():
                self.update(self.model_.index(r))
        if self.grid or not (0 <= page < self.model_.rowCount()):
            return
        idx = self.model_.index(page)
        if len(self.selected_pages()) <= 1:
            self.selectionModel().setCurrentIndex(idx, QItemSelectionModel.SelectionFlag.ClearAndSelect)
        self.scrollTo(idx, QAbstractItemView.ScrollHint.EnsureVisible)

    # -- keyboard ---------------------------------------------------------------
    def keyPressEvent(self, ev) -> None:  # noqa: N802
        if ev.key() in (Qt.Key.Key_Delete, Qt.Key.Key_Backspace) and self.selected_pages():
            self.workspace.trigger_action("pages.delete")
            ev.accept()
            return
        super().keyPressEvent(ev)

    # -- context menu ---------------------------------------------------------
    def _context_menu(self, pos: QPoint) -> None:
        idx = self.indexAt(pos)
        if idx.isValid() and idx.row() not in self.selected_pages():
            self.selectionModel().setCurrentIndex(idx, QItemSelectionModel.SelectionFlag.ClearAndSelect)
        menu = QMenu(self)
        groups = [
            ("pages.rotate_left", "pages.rotate_right"),
            ("pages.duplicate", "pages.extract"),
            ("pages.insert_blank", "pages.insert_file"),
            ("pages.delete",),
        ]
        for gi, group in enumerate(groups):
            if gi:
                menu.addSeparator()
            for aid in group:
                act = self.workspace.action(aid)
                if act is not None:
                    menu.addAction(act)
        if menu.actions():
            menu.exec(self.viewport().mapToGlobal(pos))

    # -- drag & drop -----------------------------------------------------------
    def _accepts(self, md: QMimeData) -> bool:
        if md.hasFormat(MIME):
            return True
        if md.hasUrls():
            return any(u.isLocalFile() for u in md.urls())
        return False

    def _insert_row_at(self, pos: QPoint) -> int:
        n = self.model_.rowCount()
        if n == 0:
            return 0
        idx = self.indexAt(pos)
        multi_col = self.viewport().width() >= 2 * self.cell_size().width() + 8
        if not idx.isValid():
            # nearest item
            best, bd = n - 1, float("inf")
            for r in range(n):
                rect = self.visualRect(self.model_.index(r))
                if not rect.isValid():
                    continue
                d = (rect.center() - pos).manhattanLength()
                if d < bd:
                    best, bd = r, d
            idx = self.model_.index(best)
        rect = self.visualRect(idx)
        after = pos.x() > rect.center().x() if multi_col else pos.y() > rect.center().y()
        return idx.row() + (1 if after else 0)

    def dragEnterEvent(self, ev) -> None:  # noqa: N802
        if self._accepts(ev.mimeData()):
            ev.setDropAction(Qt.DropAction.MoveAction if ev.mimeData().hasFormat(MIME) else Qt.DropAction.CopyAction)
            ev.accept()
        else:
            ev.ignore()

    def dragMoveEvent(self, ev) -> None:  # noqa: N802
        if not self._accepts(ev.mimeData()):
            ev.ignore()
            return
        pos = ev.position().toPoint()
        self._drop_row = self._insert_row_at(pos)
        h = self.viewport().height()
        if pos.y() < 30:
            self.verticalScrollBar().setValue(self.verticalScrollBar().value() - 18)
        elif pos.y() > h - 30:
            self.verticalScrollBar().setValue(self.verticalScrollBar().value() + 18)
        ev.setDropAction(Qt.DropAction.MoveAction if ev.mimeData().hasFormat(MIME) else Qt.DropAction.CopyAction)
        ev.accept()
        self.viewport().update()

    def dragLeaveEvent(self, ev) -> None:  # noqa: N802
        self._drop_row = -1
        self.viewport().update()
        super().dragLeaveEvent(ev)

    def dropEvent(self, ev) -> None:  # noqa: N802
        md = ev.mimeData()
        row = self._insert_row_at(ev.position().toPoint())
        self._drop_row = -1
        self.viewport().update()
        if md.hasFormat(MIME):
            rows = [int(x) for x in bytes(md.data(MIME)).decode().split(",") if x]
            ev.setDropAction(Qt.DropAction.MoveAction)
            ev.accept()
            QTimer.singleShot(0, lambda: self.workspace.move_pages(rows, row))
            return
        if md.hasUrls():
            files = [u.toLocalFile() for u in md.urls() if u.isLocalFile()]
            ev.setDropAction(Qt.DropAction.CopyAction)
            ev.accept()
            QTimer.singleShot(0, lambda: self.workspace.insert_files(files, row))
            return
        ev.ignore()

    def paintEvent(self, ev) -> None:  # noqa: N802
        super().paintEvent(ev)
        if self._drop_row < 0:
            return
        n = self.model_.rowCount()
        if n == 0:
            return
        multi_col = self.viewport().width() >= 2 * self.cell_size().width() + 8
        if self._drop_row < n:
            rect = self.visualRect(self.model_.index(self._drop_row))
            before = True
        else:
            rect = self.visualRect(self.model_.index(n - 1))
            before = False
        p = QPainter(self.viewport())
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(T.qcolor("accent"))
        if multi_col:
            x = rect.left() - 2 if before else rect.right() + 2
            bar = QRectF(x - 1.5, rect.top() + 8, 3, rect.height() - 16)
            dot = QRectF(x - 4, rect.top() + 4, 8, 8)
        else:
            y = rect.top() if before else rect.bottom()
            bar = QRectF(rect.left() + 14, y - 1.5, rect.width() - 28, 3)
            dot = QRectF(rect.left() + 10, y - 4, 8, 8)
        p.drawRoundedRect(bar, 1.5, 1.5)
        p.drawEllipse(dot)
        p.end()


class ThumbnailsPanel(QWidget):
    """Sidebar wrapper (keeps the API small for the workspace)."""

    def __init__(self, workspace, parent=None) -> None:
        super().__init__(parent)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(6, 4, 2, 4)
        self.view = ThumbnailView(workspace, grid=False, thumb_width=112, parent=self)
        lay.addWidget(self.view)
        self.pageActivated = self.view.pageActivated
