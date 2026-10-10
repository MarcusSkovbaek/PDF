"""Continuous, lazily rendered page view.

Scene coordinates are PDF points; each page is a :class:`PageItem` whose local
coordinate system *is* the page display space (points, rotation applied), so
overlays added with :meth:`PageView.add_overlay` are simply child items in
display coordinates and scale with the zoom automatically (use cosmetic pens
for constant-width outlines).

Rendering: only pages intersecting the viewport (plus one page of look-ahead)
are rasterised, one per event-loop tick, nearest-to-centre first. After a
zoom change the previous pixmap is drawn scaled until the debounced re-render
lands. At very high zoom levels the full page is rendered at a capped
resolution and a sharp *detail* tile covering the visible part is rendered on
top. Pixmaps are kept in an LRU bounded by bytes.
"""

from __future__ import annotations

import bisect
import time
from collections import OrderedDict

from PySide6.QtCore import (
    QEasingCurve,
    QPoint,
    QPointF,
    QRectF,
    Qt,
    QTimer,
    QVariantAnimation,
    Signal,
)
from PySide6.QtGui import (
    QBrush,
    QColor,
    QLinearGradient,
    QPainter,
    QPainterPath,
    QPen,
    QRadialGradient,
    QTransform,
)
from PySide6.QtWidgets import (
    QFrame,
    QGraphicsItem,
    QGraphicsPathItem,
    QGraphicsScene,
    QGraphicsView,
)

from ..theme import tokens as T
from .render_util import page_size, render_qpixmap
from .tools.base import ToolMouseEvent

MIN_ZOOM = 0.1
MAX_ZOOM = 8.0
GAP_PX = 20            # vertical gap between pages (screen px)
SIDE_PX = 32           # horizontal margin (screen px)
FULL_PIXELS_CAP = 9_000_000   # full-page renders larger than this use detail tiles
CACHE_BYTES = 320 * 1024 * 1024


# ---------------------------------------------------------------------------
# soft shadow helper (gradient 9-slice; cheap and crisp at every zoom)
# ---------------------------------------------------------------------------
def paint_soft_shadow(p: QPainter, r: QRectF, blur: float, color: QColor, dy: float = 0.0) -> None:
    if blur <= 0 or color.alpha() == 0:
        return
    r = r.translated(0, dy)
    c0 = QColor(color)
    cm = QColor(color)
    cm.setAlpha(int(color.alpha() * 0.35))
    c1 = QColor(color)
    c1.setAlpha(0)

    def lin(x1, y1, x2, y2):
        g = QLinearGradient(x1, y1, x2, y2)
        g.setColorAt(0.0, c0)
        g.setColorAt(0.45, cm)
        g.setColorAt(1.0, c1)
        return QBrush(g)

    b = blur
    L, Tp, R, B = r.left(), r.top(), r.right(), r.bottom()
    p.fillRect(QRectF(L, Tp - b, r.width(), b), lin(0, Tp, 0, Tp - b))
    p.fillRect(QRectF(L, B, r.width(), b), lin(0, B, 0, B + b))
    p.fillRect(QRectF(L - b, Tp, b, r.height()), lin(L, 0, L - b, 0))
    p.fillRect(QRectF(R, Tp, b, r.height()), lin(R, 0, R + b, 0))
    for cx, cy, rx, ry in ((L, Tp, L - b, Tp - b), (R, Tp, R, Tp - b), (L, B, L - b, B), (R, B, R, B)):
        g = QRadialGradient(QPointF(cx, cy), b)
        g.setColorAt(0.0, c0)
        g.setColorAt(0.45, cm)
        g.setColorAt(1.0, c1)
        p.fillRect(QRectF(rx, ry, b, b), QBrush(g))


# ---------------------------------------------------------------------------
class PageItem(QGraphicsItem):
    SHADOW_PX = 18

    def __init__(self, view: "PageView", index: int, w: float, h: float) -> None:
        super().__init__()
        self.view = view
        self.index = index
        self.w = w
        self.h = h
        self.pix = None            # QPixmap of the whole page
        self.pix_zoom = 0.0        # zoom*dpr it was rendered at
        self.stale = False         # content changed; re-render but keep showing
        self.detail = None         # (QPixmap, QRectF clip in points, zoom*dpr)
        self._margin = self.SHADOW_PX
        self.setAcceptedMouseButtons(Qt.MouseButton.NoButton)

    def set_zoom_margin(self, zoom: float) -> None:
        self.prepareGeometryChange()
        self._margin = self.SHADOW_PX / max(zoom, 0.01)

    def page_rect(self) -> QRectF:
        return QRectF(0, 0, self.w, self.h)

    def boundingRect(self) -> QRectF:  # noqa: N802
        m = self._margin
        return QRectF(-m, -m, self.w + 2 * m, self.h + 2 * m)

    def paint(self, p: QPainter, _opt, _widget=None) -> None:
        t = p.worldTransform()
        dev = t.mapRect(self.page_rect())
        r = QRectF(round(dev.x()), round(dev.y()), round(dev.width()), round(dev.height()))
        p.save()
        p.resetTransform()
        tok = T.current()
        # shadows: tight contact + wide ambient
        paint_soft_shadow(p, r, 3, T.qcolor("shadow_strong") if not tok.is_dark else QColor(0, 0, 0, 120), 1)
        paint_soft_shadow(p, r, 16, T.qcolor("shadow") if not tok.is_dark else QColor(0, 0, 0, 90), 5)
        p.fillRect(r, T.qcolor("page"))
        if self.pix is not None:
            p.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, True)
            p.drawPixmap(r, self.pix, QRectF(self.pix.rect()))
        if self.detail is not None:
            dpix, clip, _z = self.detail
            dr = t.mapRect(clip)
            p.drawPixmap(QRectF(round(dr.x()), round(dr.y()), round(dr.width()), round(dr.height())),
                         dpix, QRectF(dpix.rect()))
        if tok.is_dark:
            p.setPen(QPen(QColor(255, 255, 255, 18), 1))
            p.setBrush(Qt.BrushStyle.NoBrush)
            p.drawRect(r.adjusted(-0.5, -0.5, 0.5, 0.5))
        p.restore()

    def pixmap_bytes(self) -> int:
        n = 0
        if self.pix is not None:
            n += self.pix.width() * self.pix.height() * 4
        if self.detail is not None:
            n += self.detail[0].width() * self.detail[0].height() * 4
        return n


# ---------------------------------------------------------------------------
class PageView(QGraphicsView):
    currentPageChanged = Signal(int)
    zoomChanged = Signal(float)
    pagesReset = Signal()          # items were rebuilt (overlays dropped)
    pageRendered = Signal(int)
    fitModeChanged = Signal(str)

    def __init__(self, pdf, parent=None) -> None:
        super().__init__(parent)
        self.pdf = pdf
        self._scene = QGraphicsScene(self)
        self._scene.setItemIndexMethod(QGraphicsScene.ItemIndexMethod.NoIndex)
        self.setScene(self._scene)
        self.setFrameShape(QFrame.Shape.NoFrame)
        self.setAlignment(Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignTop)
        self.setRenderHints(QPainter.RenderHint.Antialiasing | QPainter.RenderHint.SmoothPixmapTransform)
        self.setViewportUpdateMode(QGraphicsView.ViewportUpdateMode.SmartViewportUpdate)
        self.setOptimizationFlag(QGraphicsView.OptimizationFlag.DontAdjustForAntialiasing, True)
        self.setTransformationAnchor(QGraphicsView.ViewportAnchor.NoAnchor)
        self.setResizeAnchor(QGraphicsView.ViewportAnchor.NoAnchor)
        self.setDragMode(QGraphicsView.DragMode.NoDrag)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setMouseTracking(True)
        self.viewport().setMouseTracking(True)

        self._zoom = 1.0
        self._fit_mode: str | None = "width"
        self._top_px = 72     # room for the floating toolbar
        self._items: list[PageItem] = []
        self._tops: list[float] = []
        self._current = 0
        self._lru: OrderedDict[int, int] = OrderedDict()   # page index -> bytes
        self._overlays: list[QGraphicsItem] = []
        self._search_hits = []
        self._search_items: list[QGraphicsPathItem] = []
        self._current_hit = -1
        self._tool = None
        self._tool_sink = None   # object with handle_tool_mouse(kind, ev)
        self._pan_last: QPointF | None = None
        self._space_down = False
        self._press_button = Qt.MouseButton.NoButton
        self._tool_cursor = Qt.CursorShape.ArrowCursor

        self._render_timer = QTimer(self)
        self._render_timer.setSingleShot(True)
        self._render_timer.setInterval(0)
        self._render_timer.timeout.connect(self._render_step)
        self._zoom_settle = QTimer(self)
        self._zoom_settle.setSingleShot(True)
        self._zoom_settle.setInterval(140)
        self._zoom_settle.timeout.connect(self._schedule_render)
        self._zoom_anim = QVariantAnimation(self)
        self._zoom_anim.setDuration(T.ANIM_MS + 30)
        self._zoom_anim.setEasingCurve(QEasingCurve.Type.OutCubic)
        self._zoom_anim.valueChanged.connect(lambda v: self._apply_zoom(float(v), self._anim_anchor))
        self._anim_anchor: QPoint | None = None

        self.verticalScrollBar().valueChanged.connect(self._on_scrolled)
        self.horizontalScrollBar().valueChanged.connect(self._on_scrolled)
        self._apply_theme()
        try:
            from ..widgets.util import on_theme_change

            on_theme_change(self, self._apply_theme)
        except Exception:
            pass
        self.rebuild()

    # ------------------------------------------------------------------ setup
    def _apply_theme(self) -> None:
        self.setBackgroundBrush(T.qcolor("canvas"))
        self._restyle_search()
        self.viewport().update()

    def set_top_margin(self, px: int) -> None:
        self._top_px = px
        self._layout()

    def dpr(self) -> float:
        try:
            return max(1.0, float(self.viewport().devicePixelRatioF()))
        except Exception:
            return 1.0

    # ----------------------------------------------------------------- layout
    def rebuild(self, keep_pixmaps: bool = False) -> None:
        """(Re)create page items from the document, preserving position."""
        old_page, old_frac = self._anchor_position()
        old_items = self._items
        doc = self.pdf.doc
        sizes = [page_size(doc, i) for i in range(doc.page_count)]
        reuse = keep_pixmaps and len(old_items) == len(sizes) and all(
            abs(it.w - w) < 0.01 and abs(it.h - h) < 0.01 for it, (w, h) in zip(old_items, sizes))
        self.clear_overlays()
        for it in self._search_items:
            if it.scene() is not None:
                self._scene.removeItem(it)
        self._search_items = []
        self._search_hits = []
        if reuse:
            for it in old_items:
                it.stale = True
                it.detail = None
        else:
            for it in old_items:
                self._scene.removeItem(it)
            self._items = [PageItem(self, i, w, h) for i, (w, h) in enumerate(sizes)]
            for it in self._items:
                self._scene.addItem(it)
            self._lru.clear()
        self._layout()
        if self._fit_mode:
            self._apply_fit(self._fit_mode, emit=False)
        if old_items and self._items:
            self._restore_position(min(old_page, len(self._items) - 1), old_frac)
        self.pagesReset.emit()
        self._schedule_render()
        self._update_current()

    def _layout(self) -> None:
        z = self._zoom
        gap = GAP_PX / z
        side = SIDE_PX / z
        top = self._top_px / z
        maxw = max((it.w for it in self._items), default=595.0)
        y = top
        self._tops = []
        for it in self._items:
            it.set_zoom_margin(z)
            it.setPos((maxw - it.w) / 2 + side, y)
            self._tops.append(y)
            y += it.h + gap
        bottom = y - gap + 48 / z
        self._scene.setSceneRect(QRectF(0, 0, maxw + 2 * side, max(bottom, top)))
        self.setTransform(QTransform.fromScale(z, z))

    def _anchor_position(self) -> tuple[int, float]:
        if not self._items:
            return 0, 0.0
        sp = self.mapToScene(QPoint(0, 0))
        i = self._index_for_y(sp.y())
        it = self._items[i]
        frac = (sp.y() - it.y()) / it.h if it.h else 0.0
        return i, frac

    def _restore_position(self, index: int, frac: float) -> None:
        it = self._items[index]
        y = it.y() + frac * it.h
        self._scroll_scene_y_to_top(y)

    def _scroll_scene_y_to_top(self, y: float) -> None:
        vb = self.verticalScrollBar()
        cur_top = self.mapToScene(QPoint(0, 0)).y()
        vb.setValue(vb.value() + round((y - cur_top) * self._zoom))

    def _index_for_y(self, y: float) -> int:
        if not self._tops:
            return 0
        i = bisect.bisect_right(self._tops, y) - 1
        return max(0, min(i, len(self._tops) - 1))

    # --------------------------------------------------------------- geometry
    @property
    def page_count(self) -> int:
        return len(self._items)

    def page_item(self, index: int) -> PageItem | None:
        if 0 <= index < len(self._items):
            return self._items[index]
        return None

    def page_at(self, scene_pos: QPointF, nearest: bool = False) -> tuple[int, QPointF]:
        if not self._items:
            return -1, QPointF()
        i = self._index_for_y(scene_pos.y())
        candidates = [i, i + 1] if i + 1 < len(self._items) else [i]
        for j in candidates:
            it = self._items[j]
            local = it.mapFromScene(scene_pos)
            if it.page_rect().contains(local):
                return j, local
        if nearest:
            best, bd = 0, float("inf")
            for j in {max(0, i - 1), i, min(len(self._items) - 1, i + 1)}:
                it = self._items[j]
                local = it.mapFromScene(scene_pos)
                cx = min(max(local.x(), 0), it.w)
                cy = min(max(local.y(), 0), it.h)
                d = (local.x() - cx) ** 2 + (local.y() - cy) ** 2
                if d < bd:
                    best, bd = j, d
            return best, self._items[best].mapFromScene(scene_pos)
        return -1, QPointF()

    def scene_to_page(self, index: int, scene_pos: QPointF) -> QPointF:
        return self._items[index].mapFromScene(scene_pos)

    def page_to_scene(self, index: int, point: QPointF) -> QPointF:
        return self._items[index].mapToScene(QPointF(point))

    def page_scene_rect(self, index: int) -> QRectF:
        it = self._items[index]
        return it.mapRectToScene(it.page_rect())

    def viewport_to_scene(self, vp: QPointF) -> QPointF:
        """Sub-pixel accurate viewport -> scene mapping."""
        origin = QPointF(self.mapToScene(QPoint(0, 0)))
        return origin + QPointF(vp.x() / self._zoom, vp.y() / self._zoom)

    def page_to_viewport(self, index: int, point: QPointF) -> QPointF:
        return QPointF(self.mapFromScene(self.page_to_scene(index, point)))

    def visible_pages(self, extra_px: int = 0) -> list[int]:
        if not self._items:
            return []
        vr = self.mapToScene(self.viewport().rect().adjusted(0, -extra_px, 0, extra_px)).boundingRect()
        first = self._index_for_y(vr.top())
        out = []
        for i in range(first, len(self._items)):
            r = self.page_scene_rect(i)
            if r.top() > vr.bottom():
                break
            if r.intersects(vr):
                out.append(i)
        return out

    # -------------------------------------------------------------------- zoom
    @property
    def zoom(self) -> float:
        return self._zoom

    @property
    def fit_mode(self) -> str | None:
        return self._fit_mode

    def set_zoom(self, zoom: float, anchor: QPoint | None = None, *, animate: bool = False, keep_fit: bool = False) -> None:
        zoom = max(MIN_ZOOM, min(MAX_ZOOM, float(zoom)))
        if not keep_fit and self._fit_mode is not None:
            self._fit_mode = None
            self.fitModeChanged.emit("")
        if animate and self.isVisible() and abs(zoom - self._zoom) > 1e-3:
            self._anim_anchor = anchor
            self._zoom_anim.stop()
            self._zoom_anim.setStartValue(self._zoom)
            self._zoom_anim.setEndValue(zoom)
            self._zoom_anim.start()
            return
        self._apply_zoom(zoom, anchor)

    def _apply_zoom(self, zoom: float, anchor: QPoint | None) -> None:
        if not self._items:
            self._zoom = zoom
            return
        if anchor is None:
            anchor = self.viewport().rect().center()
        sp = self.mapToScene(anchor)
        idx, dp = self.page_at(sp, nearest=True)
        if abs(zoom - self._zoom) < 1e-6:
            return
        self._zoom = zoom
        self._layout()
        new_sp = self.page_to_scene(idx, dp)
        vp = self.mapFromScene(new_sp)
        self.horizontalScrollBar().setValue(self.horizontalScrollBar().value() + vp.x() - anchor.x())
        self.verticalScrollBar().setValue(self.verticalScrollBar().value() + vp.y() - anchor.y())
        self.zoomChanged.emit(zoom)
        self._zoom_settle.start()
        self._update_current()

    def zoom_in(self, anchor: QPoint | None = None) -> None:
        self.set_zoom(_step_zoom(self._zoom, +1), anchor, animate=True)

    def zoom_out(self, anchor: QPoint | None = None) -> None:
        self.set_zoom(_step_zoom(self._zoom, -1), anchor, animate=True)

    def _fit_zoom(self, mode: str) -> float:
        if not self._items:
            return 1.0
        vw = max(50, self.viewport().width() - 2 * SIDE_PX)
        vh = max(50, self.viewport().height() - self._top_px - GAP_PX)
        if mode == "width":
            maxw = max(it.w for it in self._items)
            z = vw / maxw
            return min(z, 4.0)
        it = self._items[self._current] if self._current < len(self._items) else self._items[0]
        return min(vw / it.w, vh / it.h)

    def fit_width(self) -> None:
        self._apply_fit("width")

    def fit_page(self) -> None:
        self._apply_fit("page")

    def _apply_fit(self, mode: str, emit: bool = True) -> None:
        cur = self._current
        self._fit_mode = mode
        z = max(MIN_ZOOM, min(MAX_ZOOM, self._fit_zoom(mode)))
        if abs(z - self._zoom) > 1e-6:
            self._zoom = z
            self._layout()
            self.zoomChanged.emit(z)
            self._zoom_settle.start()
        if mode == "page" and self._items:
            self.go_to_page(cur)
        if emit:
            self.fitModeChanged.emit(mode)

    # -------------------------------------------------------------- navigation
    @property
    def current_page(self) -> int:
        return self._current

    def go_to_page(self, index: int, point: QPointF | None = None) -> None:
        if not self._items:
            return
        index = max(0, min(index, len(self._items) - 1))
        it = self._items[index]
        if point is None:
            y = it.y() - min(self._top_px, 56) / self._zoom
            if index == 0:
                y = 0
            self._scroll_scene_y_to_top(y)
        else:
            sp = self.page_to_scene(index, point)
            self._scroll_scene_y_to_top(sp.y() - self.viewport().height() / 3 / self._zoom)
        self._set_current(index)
        self._schedule_render()

    def next_page(self) -> None:
        self.go_to_page(self._current + 1)

    def prev_page(self) -> None:
        self.go_to_page(self._current - 1)

    def ensure_visible_rect(self, index: int, rect: QRectF) -> None:
        sr = self._items[index].mapRectToScene(rect)
        vr = self.mapToScene(self.viewport().rect()).boundingRect()
        if not vr.adjusted(0, self._top_px / self._zoom, 0, 0).contains(sr):
            self.centerOn(sr.center())

    def _on_scrolled(self, *_a) -> None:
        self._update_current()
        self._schedule_render()

    def _update_current(self) -> None:
        if not self._items:
            return
        vr = self.mapToScene(self.viewport().rect()).boundingRect()
        best, best_h = self._current, -1.0
        for i in self.visible_pages():
            r = self.page_scene_rect(i).intersected(vr)
            h = r.height() if r.isValid() else 0
            if h > best_h + 1e-3:
                best, best_h = i, h
        # at the very end of the document prefer the last page
        vb = self.verticalScrollBar()
        if vb.maximum() > 0 and vb.value() >= vb.maximum() and self.visible_pages():
            best = self.visible_pages()[-1]
        self._set_current(best)

    def _set_current(self, i: int) -> None:
        if i != self._current:
            self._current = i
            self.currentPageChanged.emit(i)

    # --------------------------------------------------------------- rendering
    def refresh(self, pages=None) -> None:
        """Re-render ``pages`` (or all) – keeps the old pixmap until ready."""
        if pages is None:
            self.rebuild(keep_pixmaps=True)
            return
        for i in pages:
            it = self.page_item(i)
            if it is not None:
                w, h = page_size(self.pdf.doc, i)
                if abs(w - it.w) > 0.01 or abs(h - it.h) > 0.01:
                    self.rebuild(keep_pixmaps=True)
                    return
                it.stale = True
                it.detail = None
        self._schedule_render()

    def _schedule_render(self) -> None:
        if not self._render_timer.isActive():
            self._render_timer.start()

    def _target_zoom(self, it: PageItem) -> tuple[float, bool]:
        """Device zoom to render the full page at, and whether detail is needed."""
        z = self._zoom * self.dpr()
        px = it.w * it.h * z * z
        if px <= FULL_PIXELS_CAP:
            return z, False
        capped = (FULL_PIXELS_CAP / (it.w * it.h)) ** 0.5
        return capped, True

    def _needs(self, i: int) -> str | None:
        it = self._items[i]
        tz, detail = self._target_zoom(it)
        if it.pix is None or it.stale or abs(it.pix_zoom - tz) > 1e-3:
            return "full"
        if detail:
            want = self._visible_clip(it)
            if want is None:
                return None
            z = self._zoom * self.dpr()
            if it.detail is None or abs(it.detail[2] - z) > 1e-3 or not it.detail[1].contains(want):
                return "detail"
        return None

    def _visible_clip(self, it: PageItem) -> QRectF | None:
        vr = self.mapToScene(self.viewport().rect()).boundingRect()
        local = it.mapRectFromScene(vr).intersected(it.page_rect())
        if local.isEmpty():
            return None
        return local

    def _render_step(self) -> None:
        if self._zoom_settle.isActive() or self._zoom_anim.state() == QVariantAnimation.State.Running:
            return  # wait until zooming settles
        visible = self.visible_pages()
        if not visible:
            return
        center = self.mapToScene(self.viewport().rect().center()).y()
        order = sorted(visible, key=lambda i: abs(self.page_scene_rect(i).center().y() - center))
        ahead = [i for i in (visible[-1] + 1, visible[0] - 1) if 0 <= i < len(self._items)]
        deadline = time.perf_counter() + 0.030
        did_any = False
        for i in order + ahead:
            need = self._needs(i)
            if need is None:
                continue
            self._render_page(i, need)
            did_any = True
            if time.perf_counter() > deadline:
                break
        if did_any:
            self._render_timer.start()

    def render_visible_now(self) -> None:
        """Synchronously render what is visible (used after reloads/tests)."""
        for i in self.visible_pages():
            need = self._needs(i)
            if need:
                self._render_page(i, need)

    def _render_page(self, i: int, what: str) -> None:
        it = self._items[i]
        dpr = self.dpr()
        try:
            if what == "full":
                tz, detail = self._target_zoom(it)
                pm = render_qpixmap(self.pdf.doc, i, tz / dpr, dpr)
                it.pix = pm
                it.pix_zoom = tz
                it.stale = False
                if not detail:
                    it.detail = None
            else:
                want = self._visible_clip(it)
                if want is None:
                    return
                m = 0.35
                clip = want.adjusted(-want.width() * m, -want.height() * m, want.width() * m, want.height() * m)
                clip = clip.intersected(it.page_rect())
                clip = QRectF(int(clip.x()), int(clip.y()), int(clip.width()) + 1, int(clip.height()) + 1).intersected(it.page_rect())
                z = self._zoom * dpr
                pm = render_qpixmap(self.pdf.doc, i, self._zoom, dpr,
                                    clip=(clip.left(), clip.top(), clip.right(), clip.bottom()))
                it.detail = (pm, clip, z)
        except Exception:
            it.stale = False
            if it.pix is None:
                from PySide6.QtGui import QPixmap

                it.pix = QPixmap(1, 1)
                it.pix.fill(T.qcolor("page"))
                it.pix_zoom = self._target_zoom(it)[0]
            return
        it.update()
        self._touch(i, it.pixmap_bytes())
        self.pageRendered.emit(i)

    def _touch(self, i: int, nbytes: int) -> None:
        self._lru.pop(i, None)
        self._lru[i] = nbytes
        total = sum(self._lru.values())
        if total <= CACHE_BYTES:
            return
        visible = set(self.visible_pages(extra_px=self.viewport().height()))
        for j in list(self._lru.keys()):
            if total <= CACHE_BYTES:
                break
            if j in visible or j >= len(self._items):
                continue
            it = self._items[j]
            total -= self._lru.pop(j)
            it.pix = None
            it.detail = None
            it.update()

    def cache_bytes(self) -> int:
        return sum(self._lru.values())

    # ---------------------------------------------------------------- overlays
    def add_overlay(self, index: int, item: QGraphicsItem) -> QGraphicsItem:
        it = self.page_item(index)
        if it is None:
            raise IndexError(index)
        item.setParentItem(it)
        if item.zValue() == 0:
            item.setZValue(10)
        self._overlays.append(item)
        return item

    def remove_overlay(self, item: QGraphicsItem) -> None:
        if item in self._overlays:
            self._overlays.remove(item)
        if item.scene() is not None:
            item.scene().removeItem(item)
        item.setParentItem(None)

    def clear_overlays(self, index: int | None = None) -> None:
        for item in list(self._overlays):
            parent = item.parentItem()
            if index is None or (isinstance(parent, PageItem) and parent.index == index):
                self.remove_overlay(item)

    # ------------------------------------------------------------------ search
    def set_search_hits(self, hits, current: int = -1) -> None:
        """Highlight ``hits`` (core.search.SearchHit); ``current`` is emphasised."""
        for item in self._search_items:
            if item.scene() is not None:
                self._scene.removeItem(item)
        self._search_items = []
        self._search_hits = []
        self._current_hit = current
        self.append_search_hits(hits or [])

    def append_search_hits(self, hits) -> None:
        """Add more hits (incremental search) without rebuilding existing ones."""
        hits = list(hits)
        first = len(self._search_items)
        self._search_hits.extend(hits)
        for hit in hits:
            it = self.page_item(hit.page)
            path = QPainterPath()
            for r in hit.rects:
                rr = QRectF(r.x0, r.y0, r.x1 - r.x0, r.y1 - r.y0).adjusted(-1, -1, 1, 1)
                path.addRoundedRect(rr, 1.5, 1.5)
            gi = QGraphicsPathItem(path)
            gi.setZValue(5)
            if it is not None:
                gi.setParentItem(it)
            self._search_items.append(gi)
        self._restyle_search(first)

    def set_current_hit(self, index: int, scroll: bool = True) -> None:
        old = self._current_hit
        self._current_hit = index
        for n in (old, index):
            if 0 <= n < len(self._search_items):
                self._restyle_search_one(n)
        if scroll and 0 <= index < len(self._search_hits):
            hit = self._search_hits[index]
            if hit.rects:
                r = hit.rects[0]
                rr = QRectF(r.x0, r.y0, r.x1 - r.x0, r.y1 - r.y0)
                for rx in hit.rects[1:]:
                    rr = rr.united(QRectF(rx.x0, rx.y0, rx.x1 - rx.x0, rx.y1 - rx.y0))
                self.ensure_visible_rect(hit.page, rr)
            self._schedule_render()

    def _restyle_search_one(self, n: int) -> None:
        gi = self._search_items[n]
        cur = n == self._current_hit
        gi.setBrush(T.qcolor("search_hit_current" if cur else "search_hit"))
        if cur:
            pen = QPen(T.qcolor("accent"), 1.5)
            pen.setCosmetic(True)
            gi.setPen(pen)
        else:
            gi.setPen(QPen(Qt.PenStyle.NoPen))
        gi.setZValue(6 if cur else 5)

    def search_hit_items(self) -> list[QGraphicsPathItem]:
        return list(self._search_items)

    def _restyle_search(self, first: int = 0) -> None:
        items = getattr(self, "_search_items", [])
        for n in range(first, len(items)):
            self._restyle_search_one(n)

    # ------------------------------------------------------------------- tools
    def set_tool(self, tool, sink=None) -> None:
        self._tool = tool
        if sink is not None:
            self._tool_sink = sink
        self._tool_cursor = getattr(tool, "cursor", Qt.CursorShape.ArrowCursor) if tool else Qt.CursorShape.ArrowCursor
        self._update_cursor()

    def set_tool_cursor(self, cursor) -> None:
        self._tool_cursor = cursor
        self._update_cursor()

    def _update_cursor(self) -> None:
        if self._pan_last is not None:
            self.viewport().setCursor(Qt.CursorShape.ClosedHandCursor)
        elif self._space_down:
            self.viewport().setCursor(Qt.CursorShape.OpenHandCursor)
        else:
            self.viewport().setCursor(self._tool_cursor)

    def pan_by(self, dx: float, dy: float) -> None:
        self.horizontalScrollBar().setValue(int(self.horizontalScrollBar().value() - dx))
        self.verticalScrollBar().setValue(int(self.verticalScrollBar().value() - dy))

    def _make_event(self, ev, button=None) -> ToolMouseEvent:
        vp = ev.position()
        sp = self.viewport_to_scene(vp)
        idx, local = self.page_at(sp)
        return ToolMouseEvent(
            page=idx, pos=local, scene_pos=sp, view_pos=QPointF(vp),
            buttons=ev.buttons(), button=button if button is not None else ev.button(),
            modifiers=ev.modifiers(), view=self,
        )

    def _dispatch(self, kind: str, ev) -> bool:
        tool = self._tool
        if tool is None:
            return False
        tev = self._make_event(ev)
        if kind == "double":
            tev.click_count = 2
        fn = {"press": tool.mouse_press, "move": tool.mouse_move,
              "release": tool.mouse_release, "double": tool.mouse_double_click}[kind]
        fn(tev)
        return True

    def _wants_pan(self, ev) -> bool:
        return ev.button() == Qt.MouseButton.MiddleButton or (
            self._space_down and ev.button() == Qt.MouseButton.LeftButton)

    def mousePressEvent(self, ev) -> None:  # noqa: N802
        self.setFocus(Qt.FocusReason.MouseFocusReason)
        if self._wants_pan(ev):
            self._pan_last = ev.position()
            self._update_cursor()
            ev.accept()
            return
        self._press_button = ev.button()
        if not self._dispatch("press", ev):
            super().mousePressEvent(ev)

    def mouseMoveEvent(self, ev) -> None:  # noqa: N802
        if self._pan_last is not None:
            d = ev.position() - self._pan_last
            self._pan_last = ev.position()
            self.pan_by(d.x(), d.y())
            ev.accept()
            return
        if not self._dispatch("move", ev):
            super().mouseMoveEvent(ev)

    def mouseReleaseEvent(self, ev) -> None:  # noqa: N802
        if self._pan_last is not None and ev.button() in (Qt.MouseButton.MiddleButton, Qt.MouseButton.LeftButton):
            self._pan_last = None
            self._update_cursor()
            ev.accept()
            return
        self._press_button = Qt.MouseButton.NoButton
        if not self._dispatch("release", ev):
            super().mouseReleaseEvent(ev)

    def mouseDoubleClickEvent(self, ev) -> None:  # noqa: N802
        if not self._dispatch("double", ev):
            super().mouseDoubleClickEvent(ev)

    def wheelEvent(self, ev) -> None:  # noqa: N802
        if ev.modifiers() & Qt.KeyboardModifier.ControlModifier:
            delta = ev.angleDelta().y() or ev.pixelDelta().y()
            if delta:
                factor = 1.0018 ** delta
                self.set_zoom(self._zoom * factor, ev.position().toPoint())
            ev.accept()
            return
        super().wheelEvent(ev)

    def keyPressEvent(self, ev) -> None:  # noqa: N802
        if ev.key() == Qt.Key.Key_Space and not ev.isAutoRepeat():
            self._space_down = True
            self._update_cursor()
            ev.accept()
            return
        if ev.key() == Qt.Key.Key_Space:
            ev.accept()
            return
        if self._tool is not None and self._tool.key_press(ev):
            ev.accept()
            return
        super().keyPressEvent(ev)

    def keyReleaseEvent(self, ev) -> None:  # noqa: N802
        if ev.key() == Qt.Key.Key_Space and not ev.isAutoRepeat():
            self._space_down = False
            self._update_cursor()
            ev.accept()
            return
        super().keyReleaseEvent(ev)

    def focusOutEvent(self, ev) -> None:  # noqa: N802
        if self._space_down:
            self._space_down = False
            self._update_cursor()
        super().focusOutEvent(ev)

    def resizeEvent(self, ev) -> None:  # noqa: N802
        super().resizeEvent(ev)
        if self._fit_mode and self._items:
            anchor_page, frac = self._anchor_position()
            z = max(MIN_ZOOM, min(MAX_ZOOM, self._fit_zoom(self._fit_mode)))
            if abs(z - self._zoom) > 1e-4:
                self._zoom = z
                self._layout()
                self._restore_position(anchor_page, frac)
                self.zoomChanged.emit(z)
                self._zoom_settle.start()
        self._schedule_render()

    def showEvent(self, ev) -> None:  # noqa: N802
        super().showEvent(ev)
        self._schedule_render()


_STEPS = (0.1, 0.25, 0.33, 0.5, 0.67, 0.75, 0.9, 1.0, 1.1, 1.25, 1.5, 1.75, 2.0, 2.5, 3.0, 4.0, 5.0, 6.0, 8.0)


def _step_zoom(z: float, direction: int) -> float:
    if direction > 0:
        for s in _STEPS:
            if s > z + 1e-3:
                return s
        return _STEPS[-1]
    for s in reversed(_STEPS):
        if s < z - 1e-3:
            return s
    return _STEPS[0]
