""""Markér objekt" – select, move, resize, rotate, replace and delete images
that are part of the page content (placed with "Tilføj billede" or already in
the PDF), like Acrobat's "Rediger billede".

* Hovering outlines an image (dashed accent, the exact – possibly rotated –
  outline); a click selects it (accent frame with four corner handles, the
  same visual language as "Markér kommentar").
* Dragging moves the image; Shift keeps the move horizontal/vertical. While
  dragging, the image is *lifted* off the page: its old place shows what is
  underneath (rendered once without the image) and a translucent copy of the
  image follows the pointer, with a small badge showing the position. The
  change is written on release – one undo step.
* Dragging a corner handle resizes with the **aspect ratio kept** (what one
  nearly always wants for photos and logos); Shift resizes freely (only for
  upright images – a free resize of a rotated image would skew it), Alt
  resizes around the centre. The badge shows the size in mm.
* Arrow keys nudge by 1 pt (Shift: 10 pt; a burst of nudges is one undo
  step), Delete/Backspace removes the image (toast with "Fortryd"), Esc
  clears the selection, Tab / Shift+Tab select the next / previous image on
  the page, Ctrl+C copies the image to the clipboard.
* The inspector shows a thumbnail (as placed, checkerboard behind
  transparency), pixel size, format, effective resolution, X/Y/B/H fields in
  millimetres (with an aspect lock) and the actions "Erstat billede…",
  "Gem billede som…", "Rotér 90°", "Spejlvend" and "Slet billede".

All geometry is display space (page points, rotation applied), so every zoom
level and rotated pages work; the core (:mod:`pdfstudio.core.objects`) does
the PDF work.
"""

from __future__ import annotations

import math
import os
from dataclasses import dataclass

from PySide6.QtCore import QPointF, QRectF, QSize, Qt, QTimer
from PySide6.QtGui import (
    QColor,
    QFont,
    QFontMetricsF,
    QGuiApplication,
    QImage,
    QKeyEvent,
    QPainter,
    QPainterPath,
    QPen,
    QPixmap,
    QPolygonF,
    QTransform,
)
from PySide6.QtWidgets import (
    QDoubleSpinBox,
    QFileDialog,
    QFrame,
    QGraphicsItem,
    QGraphicsPixmapItem,
    QGraphicsPolygonItem,
    QGraphicsRectItem,
    QGridLayout,
    QHBoxLayout,
    QSizePolicy,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from ....core import RELOADED, STRUCTURE
from ....core import objects as core_objects
from ....core.images import SUPPORTED_IMAGE_EXTENSIONS
from ....i18n import tr
from ...theme import tokens as T
from ...theme.icons import icon
from ...widgets.util import IconButton, label, on_theme_change
from ..render_util import to_qimage
from .base import Tool, ToolMouseEvent, ToolRegistry

HANDLE_PX = 9.0                  # corner handles, on screen
MIN_SIZE = 4.0                   # pt
DRAG_START_PX = 3.0              # pointer travel before a press becomes a drag
NUDGE = 1.0                      # pt per arrow key (Shift: NUDGE_BIG)
NUDGE_BIG = 10.0
NUDGE_BURST_MS = 900             # nudges closer together than this are one undo step
GHOST_OPACITY = 0.72
PREVIEW_MAX_PX = 1600            # cap for the drag preview pixmap
LOW_PPI = 120                    # below this an image looks soft in print
MM_PER_PT = 25.4 / 72.0
THUMB_W, THUMB_H = 232, 128
Z_LIFT, Z_HOVER, Z_GHOST, Z_FRAME, Z_BADGE = 12, 20, 35, 45, 60
_ARROWS = {Qt.Key.Key_Left: (-1, 0), Qt.Key.Key_Right: (1, 0), Qt.Key.Key_Up: (0, -1), Qt.Key.Key_Down: (0, 1)}
# corner order of the frame: top-left, top-right, bottom-right, bottom-left
_CORNER_DIR = ((-1, -1), (1, -1), (1, 1), (-1, 1))

# Test hooks: paths returned instead of showing the file dialogs.
_AUTO_OPEN: str | None = None
_AUTO_SAVE: str | None = None


def _qrect(r) -> QRectF:
    return QRectF(r.x0, r.y0, r.x1 - r.x0, r.y1 - r.y0)


def _quad_polygon(obj: core_objects.ImageObject) -> QPolygonF:
    q = obj.quad
    return QPolygonF([QPointF(p.x, p.y) for p in (q.ul, q.ur, q.lr, q.ll)])


def _unit_transform(obj: core_objects.ImageObject, pw: int, ph: int) -> QTransform:
    """Pixmap pixels -> page display space, as the image is placed."""
    m = obj.matrix
    return QTransform.fromScale(1.0 / max(pw, 1), 1.0 / max(ph, 1)) * QTransform(m.a, m.b, m.c, m.d, m.e, m.f)


def _bbox_map(old: QRectF, new: QRectF) -> QTransform:
    """Maps the rectangle ``old`` onto ``new``."""
    sx = new.width() / old.width() if old.width() else 1.0
    sy = new.height() / old.height() if old.height() else 1.0
    return (QTransform.fromTranslate(-old.left(), -old.top()) * QTransform.fromScale(sx, sy)
            * QTransform.fromTranslate(new.left(), new.top()))


def format_name(obj: core_objects.ImageObject) -> str:
    f = obj.filter
    if f == "DCTDecode":
        return tr("objects.format.jpeg")
    if f == "JPXDecode":
        return tr("objects.format.jpx")
    if f == "JBIG2Decode":
        return tr("objects.format.jbig2")
    if f == "CCITTFaxDecode":
        return tr("objects.format.ccitt")
    return tr("objects.format.lossless")


def _mm(v: float) -> str:
    return f"{v * MM_PER_PT:.1f}".replace(".", ",")


# ---------------------------------------------------------------------------
# overlay items
# ---------------------------------------------------------------------------
class _Badge(QGraphicsItem):
    """A small tooltip-like pill under the dragged image (screen-sized)."""

    def __init__(self) -> None:
        super().__init__()
        self.setFlag(QGraphicsItem.GraphicsItemFlag.ItemIgnoresTransformations, True)
        self.setZValue(Z_BADGE)
        self._text = ""
        self._font = QFont()
        self._font.setPixelSize(11)
        self._font.setWeight(QFont.Weight.DemiBold)
        self._rect = QRectF()

    def set_text(self, text: str) -> None:
        if text == self._text:
            return
        self.prepareGeometryChange()
        self._text = text
        w = QFontMetricsF(self._font).horizontalAdvance(text) + 16
        self._rect = QRectF(-w / 2, 10, w, 22)

    def text(self) -> str:
        return self._text

    def boundingRect(self) -> QRectF:  # noqa: N802
        return self._rect.adjusted(-1, -1, 1, 1)

    def paint(self, p: QPainter, _opt, _widget=None) -> None:
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        path = QPainterPath()
        path.addRoundedRect(self._rect, 6, 6)
        p.fillPath(path, T.qcolor("tooltip_bg"))
        p.setPen(T.qcolor("tooltip_text"))
        p.setFont(self._font)
        p.drawText(self._rect, Qt.AlignmentFlag.AlignCenter, self._text)


@dataclass
class _Drag:
    kind: str                         # "move" | "resize"
    obj: core_objects.ImageObject
    press: QPointF                    # page point of the press
    corner: int = -1                  # resize: the dragged corner
    started: bool = False
    rect: QRectF | None = None        # preview bounding box (display space)


# ---------------------------------------------------------------------------
# the tool
# ---------------------------------------------------------------------------
@ToolRegistry.register
class EditObjectTool(Tool):
    id = "edit.object"
    label = tr("objects.tool.label")
    hint = tr("objects.tool.hint")
    icon = "bounding-box"
    shortcut = "O"
    mode = "edit"
    order = 15            # after "Rediger tekst" (10), before "Tilføj tekst" (20)
    cursor = Qt.CursorShape.ArrowCursor

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.selected: core_objects.ImageObject | None = None
        self._images: dict[int, list[core_objects.ImageObject]] = {}
        self._pixmaps: dict[tuple, QPixmap] = {}
        self._hover: QGraphicsPolygonItem | None = None
        self._hover_key: tuple[int, int] | None = None
        self._frame: QGraphicsRectItem | None = None
        self._outline: QGraphicsPolygonItem | None = None
        self._drag: _Drag | None = None
        self._ghost: QGraphicsPixmapItem | None = None
        self._lift: QGraphicsPixmapItem | None = None
        self._badge: _Badge | None = None
        self._stale_previews: list[QGraphicsItem] = []
        self._stale_page = -1
        self._panel: ImageObjectPanel | None = None
        self._nudge_burst = False
        self._nudge_timer = QTimer(self)
        self._nudge_timer.setSingleShot(True)
        self._nudge_timer.setInterval(NUDGE_BURST_MS)
        self._nudge_timer.timeout.connect(self._end_burst)
        self._busy = False                 # an own edit is being written
        try:
            on_theme_change(self, self._retheme)
        except Exception:  # pragma: no cover - no theme manager (bare core use)
            pass

    def _retheme(self) -> None:
        """Overlay colours come from the theme: rebuild them."""
        if self.ctx is not None and self.active and self._drag is None:
            self._set_selected(self.selected, refresh=False)

    # -- lifecycle -------------------------------------------------------------
    def activate(self, ctx) -> None:
        super().activate(ctx)
        ctx.view.pageRendered.connect(self._on_page_rendered)
        ctx.view.currentPageChanged.connect(self._on_current_page)

    def deactivate(self) -> None:
        self._cancel_drag()
        self._clear_stale_previews()
        self._clear_hover()
        self._set_selected(None, refresh=False)
        self._end_burst()
        if self.ctx is not None:
            view = self.ctx.view
            for sig, slot in ((view.pageRendered, self._on_page_rendered),
                              (view.currentPageChanged, self._on_current_page)):
                try:
                    sig.disconnect(slot)
                except (RuntimeError, TypeError):
                    pass
        super().deactivate()

    def document_changed(self, event) -> None:
        self._images.clear()
        self._pixmaps.clear()
        self._hover_key = None
        self._clear_hover()
        if self._drag is not None:
            self._cancel_drag()
        if not self._busy:
            self._end_burst()   # something else changed: the next nudge is a new step
        if event.kind in (RELOADED, STRUCTURE):
            self._stale_previews = []   # the view dropped every overlay
        if self.selected is not None and self.ctx is not None:
            self._set_selected(self._find_again(self.selected))
        elif self._panel is not None:
            self._panel.refresh()

    # -- image lookup ------------------------------------------------------------
    def images(self, page: int) -> list[core_objects.ImageObject]:
        """The images on ``page`` (cached until the document changes)."""
        if page < 0 or self.ctx is None:
            return []
        if page not in self._images:
            try:
                self._images[page] = core_objects.list_images(self.ctx.pdf.doc, page)
            except Exception:
                self._images[page] = []
        return self._images[page]

    def image_at(self, page: int, pos: QPointF) -> core_objects.ImageObject | None:
        if page < 0:
            return None
        tol = 2.0 / max(self.ctx.view.zoom, 0.05)      # 2 px of slack around thin images
        return core_objects.hit_test(self.images(page), (pos.x(), pos.y()), tolerance=tol)

    def _find_again(self, obj: core_objects.ImageObject) -> core_objects.ImageObject | None:
        """The placement ``obj`` after the document changed (same index; else
        the same image at the same place), or None."""
        objs = self.images(obj.page)
        same_place = [o for o in objs if o.xref == obj.xref and all(abs(a - b) < 0.05 for a, b in zip(o.matrix, obj.matrix))]
        if same_place:
            return same_place[0]
        if 0 <= obj.index < len(objs) and objs[obj.index].xref == obj.xref:
            return objs[obj.index]
        return None

    def _pixmap(self, obj: core_objects.ImageObject) -> QPixmap | None:
        """The image itself (preview resolution), cached."""
        key = (obj.page, obj.index, obj.xref, obj.width, obj.height)
        pm = self._pixmaps.get(key)
        if pm is None:
            try:
                pix = core_objects.image_pixmap(self.ctx.pdf.doc, obj, max_side=PREVIEW_MAX_PX)
                pm = QPixmap.fromImage(to_qimage(pix))
            except Exception:
                return None
            self._pixmaps[key] = pm
        return pm

    # -- selection ------------------------------------------------------------------
    def select(self, obj: core_objects.ImageObject | None) -> None:
        """Select an image placement (None clears the selection)."""
        self._end_burst()
        self._set_selected(obj)

    def _set_selected(self, obj: core_objects.ImageObject | None, *, refresh: bool = True) -> None:
        self._clear_frame()
        self.selected = obj
        if obj is not None and self.ctx is not None:
            if self._hover_key == obj.key:
                self._clear_hover()
            self._build_frame(obj)
        if refresh and self._panel is not None:
            self._panel.refresh()

    def _build_frame(self, obj: core_objects.ImageObject) -> None:
        view = self.ctx.view
        accent = T.qcolor("accent")
        frame = QGraphicsRectItem(_qrect(obj.rect))
        pen = QPen(accent, 1.5)
        pen.setCosmetic(True)
        frame.setPen(pen)
        frame.setZValue(Z_FRAME)
        hpen = QPen(accent, 1.5)
        hpen.setCosmetic(True)
        for c in self._corners(frame.rect()):
            h = QGraphicsRectItem(QRectF(-HANDLE_PX / 2, -HANDLE_PX / 2, HANDLE_PX, HANDLE_PX), frame)
            h.setFlag(QGraphicsItem.GraphicsItemFlag.ItemIgnoresTransformations, True)
            h.setPen(hpen)
            h.setBrush(T.qcolor("surface_raised"))
            h.setPos(c)
            h.setZValue(1)
        try:
            view.add_overlay(obj.page, frame)
        except IndexError:
            return
        self._frame = frame
        if not obj.axis_aligned:   # show the exact outline of a rotated image too
            outline = QGraphicsPolygonItem(_quad_polygon(obj))
            open_pen = QPen(accent, 1.0)
            open_pen.setCosmetic(True)
            outline.setPen(open_pen)
            outline.setZValue(Z_FRAME - 1)
            view.add_overlay(obj.page, outline)
            self._outline = outline

    @staticmethod
    def _corners(r: QRectF) -> list[QPointF]:
        return [r.topLeft(), r.topRight(), r.bottomRight(), r.bottomLeft()]

    def _sync_frame(self, rect: QRectF) -> None:
        if self._frame is None:
            return
        self._frame.setRect(rect)
        for h, c in zip(self._frame.childItems(), self._corners(rect)):
            h.setPos(c)
        if self._outline is not None:
            self._outline.setVisible(False)   # the exact outline is stale while dragging

    def _clear_frame(self) -> None:
        for item in (self._frame, self._outline):
            if item is not None and self.ctx is not None:
                self.ctx.view.remove_overlay(item)
        self._frame = None
        self._outline = None

    def handle_at(self, page: int, pos: QPointF) -> int:
        """Index of the corner handle under ``pos`` (page coords), or -1."""
        obj = self.selected
        if obj is None or self._frame is None or page != obj.page:
            return -1
        zoom = max(self.ctx.view.zoom, 0.05)
        r = obj.rect
        small = min(r.width, r.height) * zoom < 3 * HANDLE_PX
        if small and r.contains((pos.x(), pos.y())):
            return -1        # a tiny image: inside means "move"
        tol = (HANDLE_PX / 2 + 3) / zoom
        for i, c in enumerate(self._corners(self._frame.rect())):
            if abs(pos.x() - c.x()) <= tol and abs(pos.y() - c.y()) <= tol:
                return i
        return -1

    # -- hover --------------------------------------------------------------------------
    def _set_hover(self, obj: core_objects.ImageObject | None) -> None:
        key = obj.key if obj is not None else None
        if self.selected is not None and key == self.selected.key:
            key, obj = None, None     # the frame already shows it
        if key == self._hover_key:
            return
        self._clear_hover()
        self._hover_key = key
        if obj is None:
            return
        item = QGraphicsPolygonItem(_quad_polygon(obj))
        color = T.qcolor("accent")
        pen = QPen(color, 1.2, Qt.PenStyle.DashLine)
        pen.setCosmetic(True)
        item.setPen(pen)
        fill = QColor(color)
        fill.setAlpha(18)
        item.setBrush(fill)
        item.setZValue(Z_HOVER)
        try:
            self.ctx.view.add_overlay(obj.page, item)
        except IndexError:
            return
        self._hover = item

    def _clear_hover(self) -> None:
        if self._hover is not None and self.ctx is not None:
            self.ctx.view.remove_overlay(self._hover)
        self._hover = None
        self._hover_key = None

    # -- mouse ------------------------------------------------------------------------------
    def mouse_move(self, ev: ToolMouseEvent) -> None:
        if self._drag is not None:
            self._drag_to(ev)
            return
        corner = self.handle_at(ev.page, ev.pos)
        if corner >= 0:
            self._set_hover(None)
            diag = Qt.CursorShape.SizeFDiagCursor if corner in (0, 2) else Qt.CursorShape.SizeBDiagCursor
            self.ctx.view.set_tool_cursor(diag)
            return
        hit = self.image_at(ev.page, ev.pos)
        self._set_hover(hit)
        self.ctx.view.set_tool_cursor(Qt.CursorShape.SizeAllCursor if hit is not None else Qt.CursorShape.ArrowCursor)

    def mouse_press(self, ev: ToolMouseEvent) -> None:
        if ev.button != Qt.MouseButton.LeftButton:
            return
        self._clear_stale_previews()
        self._end_burst()
        corner = self.handle_at(ev.page, ev.pos)
        if corner >= 0:
            self._drag = _Drag("resize", self.selected, QPointF(ev.pos), corner)
            return
        hit = self.image_at(ev.page, ev.pos)
        if hit is None:
            if self.selected is not None:
                self.select(None)
            return
        if self.selected is None or hit.key != self.selected.key:
            self.select(hit)
        self._drag = _Drag("move", self.selected, QPointF(ev.pos))

    def mouse_release(self, ev: ToolMouseEvent) -> None:
        drag, self._drag = self._drag, None
        if drag is None:
            return
        if not drag.started or drag.rect is None:
            self._clear_previews()
            return
        obj, new = drag.obj, drag.rect
        old = _qrect(obj.rect)
        if drag.kind == "move":
            dx, dy = new.left() - old.left(), new.top() - old.top()
            if abs(dx) < 1e-6 and abs(dy) < 1e-6:
                self._clear_previews()
                return
            self._write(lambda: core_objects.move_image(self.ctx.pdf, obj.page, obj, dx, dy))
        else:
            self._write(lambda: core_objects.set_image_rect(
                self.ctx.pdf, obj.page, obj, (new.left(), new.top(), new.right(), new.bottom())))

    def mouse_double_click(self, ev: ToolMouseEvent) -> None:
        pass

    # -- dragging ---------------------------------------------------------------------------
    def _drag_to(self, ev: ToolMouseEvent) -> None:
        drag = self._drag
        obj = drag.obj
        p = ev.pos_on(obj.page)
        d = p - drag.press
        if not drag.started:
            if math.hypot(d.x(), d.y()) * self.ctx.view.zoom < DRAG_START_PX:
                return
            drag.started = True
            self._start_previews(obj)
        shift = bool(ev.modifiers & Qt.KeyboardModifier.ShiftModifier)
        alt = bool(ev.modifiers & Qt.KeyboardModifier.AltModifier)
        old = _qrect(obj.rect)
        if drag.kind == "move":
            if shift:        # constrain to the dominant axis
                d = QPointF(d.x(), 0) if abs(d.x()) >= abs(d.y()) else QPointF(0, d.y())
            d = self._clamp_move(obj, d.x(), d.y())
            rect = old.translated(d[0], d[1])
            self._badge_text(tr("objects.badge.pos", x=_mm(rect.left()), y=_mm(rect.top())))
        else:
            rect = self._resize_rect(obj, drag.corner, p, keep=not shift or not obj.axis_aligned, centre=alt)
            self._badge_text(tr("objects.badge.size", w=_mm(rect.width()), h=_mm(rect.height())))
        drag.rect = rect
        self._show_preview(obj, rect)

    def _clamp_move(self, obj, dx: float, dy: float) -> tuple[float, float]:
        """Keep the image's centre on the page."""
        pr = self.ctx.pdf.page(obj.page).rect
        cx, cy = (obj.rect.x0 + obj.rect.x1) / 2, (obj.rect.y0 + obj.rect.y1) / 2
        dx = min(max(dx, pr.x0 - cx), pr.x1 - cx)
        dy = min(max(dy, pr.y0 - cy), pr.y1 - cy)
        return dx, dy

    @staticmethod
    def _resize_rect(obj, corner: int, p: QPointF, *, keep: bool, centre: bool) -> QRectF:
        r = obj.rect
        ow, oh = max(r.width, 1e-6), max(r.height, 1e-6)
        dx, dy = _CORNER_DIR[corner]
        if centre:
            cx, cy = (r.x0 + r.x1) / 2, (r.y0 + r.y1) / 2
            w, h = 2 * (p.x() - cx) * dx, 2 * (p.y() - cy) * dy
        else:
            ax = r.x1 if dx < 0 else r.x0          # the opposite corner stays put
            ay = r.y1 if dy < 0 else r.y0
            w, h = (p.x() - ax) * dx, (p.y() - ay) * dy
        if keep:
            s = max(w / ow, h / oh, MIN_SIZE / min(ow, oh))
            w, h = ow * s, oh * s
        else:
            w, h = max(w, MIN_SIZE), max(h, MIN_SIZE)
        if centre:
            return QRectF(cx - w / 2, cy - h / 2, w, h)
        x0 = ax if dx > 0 else ax - w
        y0 = ay if dy > 0 else ay - h
        return QRectF(x0, y0, w, h)

    def _start_previews(self, obj: core_objects.ImageObject) -> None:
        """Lift the image: cover its old place with the page as it looks
        without it, and create the translucent copy that follows the pointer."""
        self._clear_hover()
        view = self.ctx.view
        scale = view.zoom * view.dpr()
        try:
            pix = core_objects.render_without_image(self.ctx.pdf.doc, obj.page, obj, scale, clip=obj.rect)
            lift = QGraphicsPixmapItem(QPixmap.fromImage(to_qimage(pix)))
            lift.setTransformationMode(Qt.TransformationMode.SmoothTransformation)
            lift.setTransform(QTransform.fromScale(1 / scale, 1 / scale))
            lift.setPos(pix.x / scale, pix.y / scale)
            lift.setZValue(Z_LIFT)
            view.add_overlay(obj.page, lift)
            self._lift = lift
        except Exception:
            self._lift = None
        pm = self._pixmap(obj)
        if pm is not None:
            ghost = QGraphicsPixmapItem(pm)
            ghost.setTransformationMode(Qt.TransformationMode.SmoothTransformation)
            ghost.setOpacity(GHOST_OPACITY)
            ghost.setZValue(Z_GHOST)
            view.add_overlay(obj.page, ghost)
            self._ghost = ghost
        self._badge = _Badge()
        view.add_overlay(obj.page, self._badge)

    def _show_preview(self, obj: core_objects.ImageObject, rect: QRectF) -> None:
        old = _qrect(obj.rect)
        if self._ghost is not None:
            pm = self._ghost.pixmap()
            self._ghost.setTransform(_unit_transform(obj, pm.width(), pm.height()) * _bbox_map(old, rect))
        self._sync_frame(rect)
        if self._badge is not None:
            self._badge.setPos(rect.center().x(), rect.bottom())

    def _badge_text(self, text: str) -> None:
        if self._badge is not None:
            self._badge.set_text(text)

    def _clear_previews(self) -> None:
        for item in (self._ghost, self._lift, self._badge):
            if item is not None and self.ctx is not None:
                self.ctx.view.remove_overlay(item)
        self._ghost = self._lift = self._badge = None
        if self.selected is not None and self._frame is not None:
            self._sync_frame(_qrect(self.selected.rect))
            if self._outline is not None:
                self._outline.setVisible(True)

    def _cancel_drag(self) -> None:
        self._drag = None
        self._clear_previews()

    def _write(self, fn) -> None:
        """Run a core edit. The drag previews stay until the page has been
        re-rendered with the change (no flash of the old image)."""
        stale = [it for it in (self._ghost, self._lift, self._badge) if it is not None]
        self._ghost = self._lift = self._badge = None
        page = self.selected.page if self.selected is not None else -1
        self._busy = True
        try:
            new = fn()
        except Exception as exc:  # noqa: BLE001
            self._busy = False
            for it in stale:
                self.ctx.view.remove_overlay(it)
            self._set_selected(self.selected)
            self.ctx.toast(tr("objects.error.edit", err=str(exc)), "error")
            return
        self._busy = False
        self._stale_previews.extend(stale)
        if stale:
            self._stale_page = page
            QTimer.singleShot(600, self._clear_stale_previews)   # fallback
        if isinstance(new, core_objects.ImageObject):
            self._set_selected(new)

    def _on_page_rendered(self, page: int) -> None:
        if self._stale_previews and (self._stale_page < 0 or page == self._stale_page):
            self._clear_stale_previews()

    def _clear_stale_previews(self) -> None:
        items, self._stale_previews = self._stale_previews, []
        try:
            for it in items:
                if self.ctx is not None:
                    self.ctx.view.remove_overlay(it)
        except RuntimeError:   # the view is gone (tab closed)
            pass

    def _on_current_page(self, _page: int) -> None:
        if self._panel is not None and self.selected is None:
            self._panel.refresh()

    # -- keyboard ---------------------------------------------------------------------------------
    def key_press(self, ev: QKeyEvent) -> bool:
        key = ev.key()
        shift = bool(ev.modifiers() & Qt.KeyboardModifier.ShiftModifier)
        if key in (Qt.Key.Key_Tab, Qt.Key.Key_Backtab):
            return self.select_next(-1 if key == Qt.Key.Key_Backtab or shift else 1)
        if self.selected is None:
            return False
        if key == Qt.Key.Key_Escape:
            if self._drag is not None:
                self._cancel_drag()
            else:
                self.select(None)
            return True
        if key in (Qt.Key.Key_Delete, Qt.Key.Key_Backspace):
            self.delete_selected()
            return True
        if key in _ARROWS:
            step = NUDGE_BIG if shift else NUDGE
            ux, uy = _ARROWS[key]
            self.nudge(ux * step, uy * step)
            return True
        return False

    def select_next(self, step: int = 1) -> bool:
        """Tab / Shift+Tab: the next / previous image on the page."""
        page = self.selected.page if self.selected is not None else self.ctx.view.current_page
        objs = self.images(page)
        if not objs:
            return False
        if self.selected is None:
            nxt = objs[0] if step > 0 else objs[-1]
        else:
            nxt = objs[(self.selected.index + step) % len(objs)]
        self.select(nxt)
        r = nxt.rect
        self.ctx.view.ensure_visible_rect(page, _qrect(r))
        return True

    def nudge(self, dx: float, dy: float) -> None:
        obj = self.selected
        if obj is None or self._drag is not None:
            return
        dx, dy = self._clamp_move(obj, dx, dy)
        if not dx and not dy:
            return
        merge = self._nudge_burst
        self._write(lambda: core_objects.move_image(self.ctx.pdf, obj.page, obj, dx, dy, merge=merge))
        self._nudge_burst = True
        self._nudge_timer.start()

    def _end_burst(self) -> None:
        self._nudge_burst = False
        self._nudge_timer.stop()

    # -- actions --------------------------------------------------------------------------------
    def delete_selected(self) -> None:
        obj = self.selected
        if obj is None:
            return
        self.select(None)
        try:
            core_objects.delete_image(self.ctx.pdf, obj.page, obj)
        except Exception as exc:  # noqa: BLE001
            self.ctx.toast(tr("objects.error.edit", err=str(exc)), "error")
            return
        self.ctx.toast(tr("objects.deleted"), "info", action_text=tr("objects.undo_action"),
                       on_action=self.ctx.pdf.undo)

    def set_rect(self, rect: QRectF) -> None:
        """Place the selected image's bounding box at ``rect`` (display pt)."""
        obj = self.selected
        if obj is None or rect.width() < 0.5 or rect.height() < 0.5:
            return
        old = _qrect(obj.rect)
        if all(abs(a - b) < 1e-3 for a, b in ((old.left(), rect.left()), (old.top(), rect.top()),
                                               (old.width(), rect.width()), (old.height(), rect.height()))):
            return
        self._write(lambda: core_objects.set_image_rect(
            self.ctx.pdf, obj.page, obj, (rect.left(), rect.top(), rect.right(), rect.bottom())))

    def rotate_selected(self, degrees: float = 90) -> None:
        obj = self.selected
        if obj is not None:
            self._write(lambda: core_objects.rotate_image(self.ctx.pdf, obj.page, obj, degrees))

    def flip_selected(self) -> None:
        obj = self.selected
        if obj is not None:
            self._write(lambda: core_objects.flip_image(self.ctx.pdf, obj.page, obj))

    def replace_selected(self, path: str | None = None) -> bool:
        obj = self.selected
        if obj is None:
            return False
        if path is None:
            path = _AUTO_OPEN
        if path is None:
            patterns = " ".join(f"*{e}" for e in SUPPORTED_IMAGE_EXTENSIONS)
            path, _ = QFileDialog.getOpenFileName(self.ctx.view.window(), tr("objects.dialog.replace"),
                                                  self._start_dir(), tr("objects.filter.images", patterns=patterns))
        if not path:
            return False
        try:
            new = core_objects.replace_image(self.ctx.pdf, obj.page, obj, path, keep_aspect=True)
        except OSError as exc:
            self.ctx.toast(tr("objects.error.replace", err=str(exc)), "error")
            return False
        except Exception:  # noqa: BLE001 - not an image MuPDF can read
            self.ctx.toast(tr("objects.error.unreadable", name=os.path.basename(path)), "error")
            return False
        self._set_selected(new)
        self.ctx.toast(tr("objects.replaced"), "success")
        return True

    def save_selected(self, path: str | None = None) -> str | None:
        obj = self.selected
        if obj is None:
            return None
        try:
            data, ext = core_objects.extract_image_file(self.ctx.pdf.doc, obj)
        except Exception as exc:  # noqa: BLE001
            self.ctx.toast(tr("objects.error.save", err=str(exc)), "error")
            return None
        ext = "jpg" if ext == "jpeg" else ext
        if path is None:
            path = _AUTO_SAVE
        if path is None:
            stem = os.path.splitext(self.ctx.pdf.display_name)[0]
            suggestion = os.path.join(self._start_dir(), f"{stem}_{tr('objects.default_name')}{obj.index + 1}.{ext}")
            kind = {"jpg": "JPEG", "jpx": "JPEG 2000"}.get(ext, ext.upper())
            path, _ = QFileDialog.getSaveFileName(self.ctx.view.window(), tr("objects.dialog.save"), suggestion,
                                                  tr("objects.filter.ext", kind=kind, ext=ext))
        if not path:
            return None
        if not os.path.splitext(path)[1]:
            path += "." + ext
        try:
            with open(path, "wb") as fh:
                fh.write(data)
        except OSError as exc:
            self.ctx.toast(tr("objects.error.save", err=str(exc)), "error")
            return None
        self.ctx.toast(tr("objects.saved", name=os.path.basename(path)), "success")
        return path

    def _start_dir(self) -> str:
        p = getattr(self.ctx.pdf, "path", None)
        return os.path.dirname(p) if p else os.path.expanduser("~")

    # -- clipboard ---------------------------------------------------------------------------------
    def has_selection(self) -> bool:
        return self.selected is not None

    def copy(self) -> bool:
        """Ctrl+C: the selected image (full resolution) to the clipboard."""
        if self.selected is None:
            return False
        try:
            pix = core_objects.image_pixmap(self.ctx.pdf.doc, self.selected)
        except Exception:
            return False
        QGuiApplication.clipboard().setImage(to_qimage(pix))
        return True

    # -- inspector -----------------------------------------------------------------------------------
    def properties_widget(self) -> QWidget:
        if self._panel is None:
            self._panel = ImageObjectPanel(self)
        self._panel.refresh()
        return self._panel


# ---------------------------------------------------------------------------
# inspector panel
# ---------------------------------------------------------------------------
class _Thumb(QWidget):
    """The selected image as placed (rotation/mirroring applied), centred on
    a checkerboard when it is transparent."""

    def __init__(self) -> None:
        super().__init__()
        self.setFixedHeight(THUMB_H)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self._img: QImage | None = None
        self._alpha = False

    def set_image(self, img: QImage | None, alpha: bool) -> None:
        self._img, self._alpha = img, alpha
        self.update()

    def paintEvent(self, _ev) -> None:  # noqa: N802
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
        r = QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5)
        path = QPainterPath()
        path.addRoundedRect(r, T.RADIUS_S + 2, T.RADIUS_S + 2)
        p.fillPath(path, T.qcolor("surface_sunken"))
        if self._img is not None and not self._img.isNull():
            avail = r.adjusted(8, 8, -8, -8)
            s = min(avail.width() / self._img.width(), avail.height() / self._img.height())
            w, h = self._img.width() * s, self._img.height() * s
            target = QRectF(avail.center().x() - w / 2, avail.center().y() - h / 2, w, h)
            if self._alpha:
                p.save()
                p.setClipRect(target)
                cell = 6
                light, dark = T.qcolor("surface_raised"), T.qcolor("border")
                y, row = target.top(), 0
                while y < target.bottom():
                    x, col = target.left(), row % 2
                    while x < target.right():
                        p.fillRect(QRectF(x, y, cell, cell), dark if col % 2 else light)
                        x += cell
                        col += 1
                    y += cell
                    row += 1
                p.restore()
            p.drawImage(target, self._img)
        p.setPen(QPen(T.qcolor("border"), 1))
        p.drawPath(path)
        p.end()


class ImageObjectPanel(QWidget):
    """Inspector for "Markér objekt": what is selected and what can be done."""

    def __init__(self, tool: EditObjectTool) -> None:
        super().__init__()
        self.tool = tool
        self._loading = False
        v = QVBoxLayout(self)
        v.setContentsMargins(16, 4, 16, 16)
        v.setSpacing(10)

        card = QFrame()
        card.setObjectName("Card")
        cl = QVBoxLayout(card)
        cl.setContentsMargins(14, 12, 14, 12)
        cl.setSpacing(4)
        self.thumb = _Thumb()
        cl.addWidget(self.thumb)
        cl.addSpacing(4)
        self.title = label("", "heading")
        self.detail = label("", "muted", wrap=True)
        self.meta = label("", "caption", wrap=True)
        self.note = label("", "caption", wrap=True)
        for w in (self.title, self.detail, self.meta, self.note):
            cl.addWidget(w)
        v.addWidget(card)

        # -- position & size ----------------------------------------------------------
        self.edit_box = QWidget()
        ev = QVBoxLayout(self.edit_box)
        ev.setContentsMargins(0, 4, 0, 0)
        ev.setSpacing(8)
        ev.addWidget(label(tr("objects.section.position"), "section"))
        grid = QGridLayout()
        grid.setHorizontalSpacing(6)
        grid.setVerticalSpacing(8)
        self.x = self._spin(tr("objects.field.x.tip"), -2000, 5000)
        self.y = self._spin(tr("objects.field.y.tip"), -2000, 5000)
        self.w = self._spin(tr("objects.field.w.tip"), 1, 5000)
        self.h = self._spin(tr("objects.field.h.tip"), 1, 5000)
        self.lock = IconButton("link-simple", tr("objects.lock.tip"), variant="icon-sm", checkable=True)
        self.lock.setChecked(True)
        self.lock.toggled.connect(self._lock_toggled)
        for col, (key, spin) in enumerate((("x", self.x), ("y", self.y))):
            grid.addWidget(label(tr(f"objects.field.{key}"), "caption"), 0, col * 3)
            grid.addWidget(spin, 0, col * 3 + 1)
        grid.addWidget(label(tr("objects.field.w"), "caption"), 1, 0)
        grid.addWidget(self.w, 1, 1)
        grid.addWidget(self.lock, 1, 2)
        grid.addWidget(label(tr("objects.field.h"), "caption"), 1, 3)
        grid.addWidget(self.h, 1, 4)
        grid.setColumnStretch(1, 1)
        grid.setColumnStretch(4, 1)
        grid.setColumnMinimumWidth(2, 26)
        ev.addLayout(grid)
        for spin in (self.x, self.y, self.w, self.h):
            spin.valueChanged.connect(lambda _v, s=spin: self._field_changed(s))

        # -- actions -----------------------------------------------------------------------
        ev.addSpacing(6)
        ev.addWidget(label(tr("objects.section.image"), "section"))
        self._buttons: list[tuple[QToolButton, str, str]] = []
        self.replace_btn = self._row(ev, "swap", tr("objects.action.replace"), tool.replace_selected,
                                     tr("objects.action.replace.tip"))
        self.save_btn = self._row(ev, "download-simple", tr("objects.action.save"), tool.save_selected,
                                  tr("objects.action.save.tip"))
        self.rotate_btn = self._row(ev, "arrow-clockwise", tr("objects.action.rotate"), tool.rotate_selected)
        self.flip_btn = self._row(ev, "arrows-left-right", tr("objects.action.flip"), tool.flip_selected)
        self.delete_btn = self._row(ev, "trash", tr("objects.action.delete"), tool.delete_selected, role="danger")
        v.addWidget(self.edit_box)
        v.addSpacing(2)
        v.addWidget(label(tr("objects.tips"), "caption", wrap=True))
        v.addStretch(1)
        on_theme_change(self, self._refresh_icons)
        self.refresh()

    def _spin(self, tip: str, lo: float, hi: float) -> QDoubleSpinBox:
        s = QDoubleSpinBox()
        s.setRange(lo, hi)
        s.setDecimals(1)
        s.setSingleStep(1.0)
        s.setSuffix(tr("objects.unit.mm"))
        s.setKeyboardTracking(False)        # commit on Enter / focus out / arrows – not per keystroke
        s.setToolTip(tip)
        s.setMinimumWidth(78)
        s.setAlignment(Qt.AlignmentFlag.AlignRight)
        return s

    def _row(self, layout, icon_name: str, text: str, slot, tip: str = "", role: str = "text_muted") -> QToolButton:
        b = QToolButton()
        b.setText(text)
        b.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
        b.setAutoRaise(True)
        b.setCursor(Qt.CursorShape.PointingHandCursor)
        b.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        b.setProperty("variant", "row")
        b.setIconSize(QSize(18, 18))
        if tip:
            b.setToolTip(tip)
        b.clicked.connect(lambda _=False: slot())
        layout.addWidget(b)
        self._buttons.append((b, icon_name, role))
        b.setIcon(icon(icon_name, role))
        return b

    def _refresh_icons(self) -> None:
        for b, name, role in self._buttons:
            b.setIcon(icon(name, role))
        self.refresh()

    # -- state -----------------------------------------------------------------------------------
    def refresh(self) -> None:
        tool = self.tool
        obj = tool.selected
        self.edit_box.setVisible(obj is not None)
        self.thumb.setVisible(obj is not None)
        if obj is None:
            self.title.setText(tr("objects.none.title"))
            self.detail.setText(tr("objects.none.hint"))
            page = tool.ctx.view.current_page if tool.ctx is not None else -1
            n = len(tool.images(page)) if page >= 0 else 0
            key = "objects.count.zero" if n == 0 else "objects.count.one" if n == 1 else "objects.count.many"
            self.meta.setText(tr(key, n=n, page=page + 1) if page >= 0 else "")
            self.meta.setVisible(page >= 0)
            self.note.setVisible(False)
            self.thumb.set_image(None, False)
            return
        n = len(tool.images(obj.page))
        self.title.setText(tr("objects.title.n", i=obj.index + 1, n=n) if n > 1 else tr("objects.title"))
        parts = [tr("objects.pixels", w=obj.width, h=obj.height), format_name(obj)]
        if obj.has_alpha:
            parts.append(tr("objects.alpha"))
        self.detail.setText(" · ".join(parts))
        ppi = round(min(obj.dpi)) if min(obj.dpi) > 0 else 0
        meta = tr("objects.lowres" if 0 < ppi < LOW_PPI else "objects.resolution", ppi=ppi)
        orient = []
        rot = round(obj.rotation, 1) % 360
        if rot:
            orient.append(tr("objects.rotated", deg=f"{rot:g}"))
        if obj.flipped:
            orient.append(tr("objects.flipped"))
        if orient:
            meta += "\n" + ", ".join(orient).capitalize()
        self.meta.setText(meta)
        self.meta.setVisible(True)
        self.meta.setProperty("role", "danger" if 0 < ppi < LOW_PPI else "caption")
        self.meta.style().unpolish(self.meta)
        self.meta.style().polish(self.meta)
        note = tr("objects.in_form") if obj.in_form else tr("objects.inline") if obj.inline else ""
        self.note.setText(note)
        self.note.setVisible(bool(note))
        self.thumb.set_image(self._thumb_image(obj), obj.has_alpha)
        self._load_fields(obj)

    def _thumb_image(self, obj: core_objects.ImageObject) -> QImage | None:
        """The image as it appears on the page, sized for the thumbnail."""
        pm = self.tool._pixmap(obj)
        if pm is None:
            return None
        m, r = obj.matrix, obj.rect
        wpt, hpt = obj.size_pt
        if wpt <= 0 or hpt <= 0:
            return None
        dpr = self.devicePixelRatioF() or 1.0
        s = min((THUMB_W - 16) * dpr / max(r.width, 1e-6), (THUMB_H - 16) * dpr / max(r.height, 1e-6))
        # downsample in the image's own frame first (area averaging), then
        # apply the placement's rotation/mirroring
        small = pm.toImage().scaled(max(1, round(wpt * s)), max(1, round(hpt * s)),
                                    Qt.AspectRatioMode.IgnoreAspectRatio, Qt.TransformationMode.SmoothTransformation)
        if obj.axis_aligned and not obj.flipped and round(obj.rotation) % 360 == 0:
            return small
        rot = QTransform(m.a / wpt, m.b / wpt, m.c / hpt, m.d / hpt, 0, 0)
        return small.transformed(rot, Qt.TransformationMode.SmoothTransformation)

    def _load_fields(self, obj: core_objects.ImageObject) -> None:
        self._loading = True
        try:
            r = obj.rect
            for spin, val in ((self.x, r.x0), (self.y, r.y0), (self.w, r.width), (self.h, r.height)):
                if not spin.hasFocus() or abs(spin.value() - val * MM_PER_PT) > 0.05:
                    spin.setValue(val * MM_PER_PT)
            # a free aspect would skew a rotated image: keep it locked
            self.lock.setEnabled(obj.axis_aligned)
            if not obj.axis_aligned:
                self.lock.setChecked(True)
        finally:
            self._loading = False

    def _lock_toggled(self, on: bool) -> None:
        self.lock.set_icon_name("link-simple" if on else "link-simple-break")

    def _field_changed(self, spin: QDoubleSpinBox) -> None:
        obj = self.tool.selected
        if self._loading or obj is None:
            return
        r = obj.rect
        x, y = self.x.value() / MM_PER_PT, self.y.value() / MM_PER_PT
        w, h = self.w.value() / MM_PER_PT, self.h.value() / MM_PER_PT
        if self.lock.isChecked() and r.width > 0 and r.height > 0:
            aspect = r.width / r.height
            self._loading = True
            try:
                if spin is self.w:
                    h = w / aspect
                    self.h.setValue(h * MM_PER_PT)
                elif spin is self.h:
                    w = h * aspect
                    self.w.setValue(w * MM_PER_PT)
            finally:
                self._loading = False
        self.tool.set_rect(QRectF(x, y, w, h))


__all__ = ["EditObjectTool", "ImageObjectPanel"]
