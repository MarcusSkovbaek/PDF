""""Markér objekt" – select, move, scale, copy and delete everything that is
part of the page content: **text blocks**, **vector graphics** and **images**
(like Acrobat's "Rediger tekst og billeder" object mode, with the feel of a
design tool).

* Hovering outlines the object under the pointer (dashed accent, translucent
  fill; images show their exact – possibly rotated – outline). A click
  selects it (accent frame around the selection); Shift/Ctrl+click adds or
  removes objects; dragging on empty space draws a selection rectangle; a
  plain click on empty space clears the selection. Alt+click also reaches
  page backgrounds, template graphics and table rules and picks single
  paths out of a group (O5). A box graphic brings what it encloses along
  when "Medtag indhold" is on (O4, default; remembered locally).
* Dragging moves the selection; Shift keeps the move horizontal/vertical.
  While dragging, the selection is *lifted* off the page: its old place
  shows what is underneath (rendered once without it), a translucent copy
  follows the pointer and a badge shows the position in mm. The selection
  snaps to other objects' edges and centres and to the page's edges and
  centre (thin accent guides; Alt disables snapping). The change is written
  on release – one undo step.
* Corner handles scale graphics and images (graphics: ratio kept, Shift
  frees it; images keep their aspect ratio, Shift frees an upright image;
  Alt scales around the centre). A single text block gets side handles that
  change its wrap width (O9).
* Keys: arrows nudge 1 pt (Shift 10 pt; a burst is one undo step),
  Delete/Backspace deletes (toast with "Fortryd"), Esc cancels a drag or
  clears the selection, Tab / Shift+Tab walk through the page's objects,
  Ctrl+A selects every object of the page, Ctrl+C / Ctrl+X / Ctrl+V copy,
  cut and paste (in-app clipboard: across pages, tabs and documents),
  Ctrl+D duplicates. A double-click on text opens *Redigér tekst* there.
* The inspector shows a panel per selection kind (:attr:`EditObjectTool.PANELS`):
  the image panel for one image, a general panel otherwise.

All geometry is display space (page points, rotation applied), so every zoom
level and rotated pages work; :mod:`pdfstudio.core.pageobjects` (and, for a
single image, :mod:`pdfstudio.core.objects`) does the PDF work.
"""

from __future__ import annotations

import math
import os
from dataclasses import dataclass, field

import pymupdf
from PySide6.QtCore import QEvent, QLineF, QLocale, QPointF, QRectF, QSize, Qt, QTimer, Signal
from PySide6.QtGui import (
    QColor,
    QFont,
    QFontMetricsF,
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
    QGraphicsLineItem,
    QGraphicsPixmapItem,
    QGraphicsPolygonItem,
    QGraphicsRectItem,
    QGridLayout,
    QSizePolicy,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from ....core import CONTENT, RELOADED, STRUCTURE, flow
from ....core import objects as core_objects
from ....core import pageobjects as po
from ....core.images import SUPPORTED_IMAGE_EXTENSIONS
from ....core.pageobjects import KEEP, PageObject
from ....i18n import tr
from ...theme import tokens as T
from ...theme.icons import icon
from ...widgets.util import IconButton, label, on_theme_change, set_prop
from ..render_util import to_qimage
from . import _object_clipboard as clipboard
from .base import Tool, ToolMouseEvent, ToolRegistry

HANDLE_PX = 9.0                  # corner handles, on screen
SIDE_W_PX, SIDE_H_PX = 6.0, 16.0  # side handles (text width), on screen
MIN_SIZE = 4.0                   # pt
MIN_TEXT_WIDTH = 12.0            # pt
DRAG_START_PX = 3.0              # pointer travel before a press becomes a drag
SNAP_PX = 6.0                    # snapping distance, on screen
NUDGE = 1.0                      # pt per arrow key (Shift: NUDGE_BIG)
NUDGE_BIG = 10.0
NUDGE_BURST_MS = 900             # nudges closer together than this are one undo step
WARM_MS = 150                    # idle delay before the current page's objects are listed
PASTE_STEP = 10.0                # pt: a paste onto the source page lands this far down/right
GHOST_OPACITY = 0.72
PREVIEW_MAX_PX = 1600            # cap for the drag preview pixmap
PREVIEW_PAD = 2.0                # pt around the selection in the drag previews (ink beyond the boxes)
TEXT_PAD = 4.5                   # pt left/right of text to its outline / frame (room for the side handles)
TEXT_PAD_Y = 2.5                 # pt above/below
LOW_PPI = 120                    # below this an image looks soft in print
MM_PER_PT = 25.4 / 72.0
THUMB_W, THUMB_H = 232, 128
SETTINGS_INCLUDE = "objects/include_contents"
Z_LIFT, Z_HOVER, Z_GHOST, Z_BAND, Z_MEMBER, Z_FRAME, Z_GUIDE, Z_BADGE = 12, 20, 35, 40, 44, 45, 50, 60
_ARROWS = {Qt.Key.Key_Left: (-1, 0), Qt.Key.Key_Right: (1, 0), Qt.Key.Key_Up: (0, -1), Qt.Key.Key_Down: (0, 1)}
# corner order of the frame: top-left, top-right, bottom-right, bottom-left
_CORNER_DIR = ((-1, -1), (1, -1), (1, 1), (-1, 1))
SIDE_LEFT, SIDE_RIGHT = 4, 5     # handle numbers of the text width handles

# Test hooks: paths returned instead of showing the file dialogs.
_AUTO_OPEN: str | None = None
_AUTO_SAVE: str | None = None


def _settings():
    from ...app import settings

    return settings()


def _qrect(r) -> QRectF:
    return QRectF(r.x0, r.y0, r.x1 - r.x0, r.y1 - r.y0)


def _prect(r: QRectF) -> pymupdf.Rect:
    return pymupdf.Rect(r.left(), r.top(), r.right(), r.bottom())


def _union(objs) -> pymupdf.Rect:
    r = pymupdf.Rect(objs[0].rect)
    for o in objs[1:]:
        r |= o.rect
    return r


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
    """Points as millimetres with one decimal, in the UI locale (as the spin boxes)."""
    return QLocale().toString(v * MM_PER_PT, "f", 1)


def kind_title(objs: list[PageObject]) -> str:
    """"Tekst", "Grafik", "Billede" or "3 objekter"."""
    if not objs:
        return tr("objects.none.title")
    if len(objs) == 1:
        return tr(f"objects.kind.{objs[0].kind}")
    return tr("objects.kind.many", n=len(objs))


def count_text(objs: list[PageObject]) -> str:
    """"2 tekstblokke, 1 grafisk element og 1 billede"."""
    parts = []
    for kind in ("text", "graphic", "image"):
        n = sum(1 for o in objs if o.kind == kind)
        if n:
            parts.append(tr(f"objects.count.{kind}.{'one' if n == 1 else 'many'}", n=n))
    if len(parts) > 1:
        return tr("objects.count.join", first=", ".join(parts[:-1]), last=parts[-1])
    return parts[0] if parts else ""


def snap_delta(rect: QRectF, dx: float, dy: float, xs, ys, tol: float, *, lock_x=False, lock_y=False):
    """Snap ``rect`` moved by ``(dx, dy)``: its left/centre/right to the
    nearest x in ``xs`` and its top/middle/bottom to the nearest y in ``ys``
    (``[(value, extent QRectF), ...]``) within ``tol``. Returns
    ``(dx, dy, guides)`` – guides as ``QLineF`` in display space."""
    moved = rect.translated(dx, dy)
    guides: list[QLineF] = []

    def best(lines, cands):
        hit = None
        for ln in lines:
            for val, _ext in cands:
                d = val - ln
                if abs(d) <= tol and (hit is None or abs(d) < abs(hit)):
                    hit = d
        return hit

    sx = None if lock_x else best((moved.left(), moved.center().x(), moved.right()), xs)
    sy = None if lock_y else best((moved.top(), moved.center().y(), moved.bottom()), ys)
    if sx is not None:
        dx += sx
    if sy is not None:
        dy += sy
    moved = rect.translated(dx, dy)
    if sx is not None:
        seen = set()
        for ln in (moved.left(), moved.center().x(), moved.right()):
            for val, ext in xs:
                if abs(val - ln) < 0.01 and round(val, 2) not in seen:
                    seen.add(round(val, 2))
                    y0, y1 = min(moved.top(), ext.top()), max(moved.bottom(), ext.bottom())
                    guides.append(QLineF(val, y0, val, y1))
    if sy is not None:
        seen = set()
        for ln in (moved.top(), moved.center().y(), moved.bottom()):
            for val, ext in ys:
                if abs(val - ln) < 0.01 and round(val, 2) not in seen:
                    seen.add(round(val, 2))
                    x0, x1 = min(moved.left(), ext.left()), max(moved.right(), ext.right())
                    guides.append(QLineF(x0, val, x1, val))
    return dx, dy, guides


# ---------------------------------------------------------------------------
# overlay items
# ---------------------------------------------------------------------------
class _Badge(QGraphicsItem):
    """A small tooltip-like pill under the dragged selection (screen-sized)."""

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
    kind: str                         # "move" | "resize" | "width" | "band"
    page: int
    objs: list                        # the selection at the press (PageObjects)
    press: QPointF                    # page point of the press
    base: QRectF = field(default_factory=QRectF)   # union of the selection at the press
    handle: int = -1                  # resize / width: the dragged handle
    started: bool = False
    rect: QRectF | None = None        # preview box (display space)
    reduce_to: PageObject | None = None   # click without drag on a member: select only it
    additive: bool = False            # band: add to the selection (Shift/Ctrl)
    xs: list = field(default_factory=list)   # snapping candidates
    ys: list = field(default_factory=list)


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

    #: inspector panel class per selection kind ("none", "image", "text",
    #: "graphic", "multi") – WP-D2 plugs its panels in here. A panel is built
    #: once per tool as ``cls(tool)`` and must have ``refresh()``.
    PANELS: dict[str, type] = {}

    selectionChanged = Signal()

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._sel: list[PageObject] = []
        self._primary: PageObject | None = None
        self._objects: dict[tuple[int, bool], list[PageObject]] = {}
        self._images: dict[int, list[core_objects.ImageObject]] = {}
        self._pixmaps: dict[tuple, QPixmap] = {}
        self._lineage: dict[tuple, PageObject] = {}
        self._hover: QGraphicsPolygonItem | None = None
        self._hover_key: tuple | None = None
        self._frame: QGraphicsRectItem | None = None
        self._outline: QGraphicsPolygonItem | None = None
        self._members: list[QGraphicsItem] = []
        self._guides: list[QGraphicsLineItem] = []
        self._band: QGraphicsRectItem | None = None
        self._drag: _Drag | None = None
        self._ghost: QGraphicsPixmapItem | None = None
        self._ghost_base = QTransform()
        self._lift: QGraphicsPixmapItem | None = None
        self._badge: _Badge | None = None
        self._stale_previews: list[QGraphicsItem] = []
        self._stale_page = -1
        self._panels: dict[type, QWidget] = {}
        self._panel: QWidget | None = None
        self._nudge_burst = False
        self._nudge_timer = QTimer(self)
        self._nudge_timer.setSingleShot(True)
        self._nudge_timer.setInterval(NUDGE_BURST_MS)
        self._nudge_timer.timeout.connect(self._end_burst)
        self._busy = False                 # an own edit is being written
        # list the current page's objects while idle, so the first hover is instant (O16)
        self._warm_timer = QTimer(self)
        self._warm_timer.setSingleShot(True)
        self._warm_timer.setInterval(WARM_MS)
        self._warm_timer.timeout.connect(self._warm)
        self.include_contents = self._load_include()
        try:
            on_theme_change(self, self._retheme)
        except Exception:  # pragma: no cover - no theme manager (bare core use)
            pass

    def _retheme(self) -> None:
        """Overlay colours come from the theme: rebuild them."""
        if self.ctx is not None and self.active and self._drag is None:
            self._rebuild_frame()

    # -- lifecycle -------------------------------------------------------------
    def activate(self, ctx) -> None:
        super().activate(ctx)
        ctx.view.pageRendered.connect(self._on_page_rendered)
        ctx.view.currentPageChanged.connect(self._on_current_page)
        ctx.view.installEventFilter(self)
        self._warm_timer.start()

    def deactivate(self) -> None:
        self._cancel_drag()
        self._clear_stale_previews()
        self._clear_hover()
        self._set_selection([], refresh=False)
        self._end_burst()
        self._warm_timer.stop()
        if self.ctx is not None:
            view = self.ctx.view
            view.removeEventFilter(self)
            for sig, slot in ((view.pageRendered, self._on_page_rendered),
                              (view.currentPageChanged, self._on_current_page)):
                try:
                    sig.disconnect(slot)
                except (RuntimeError, TypeError):
                    pass
        super().deactivate()

    def document_changed(self, event) -> None:
        self._objects.clear()
        if self.active:
            self._warm_timer.start()
        self._images.clear()
        self._pixmaps.clear()
        self._clear_hover()
        if event.kind in (RELOADED, STRUCTURE):
            self._stale_previews = []   # the view dropped every overlay
        if self._busy:
            return                      # the edit's result is selected by _write
        if self._drag is not None:
            self._cancel_drag()
        self._end_burst()               # something else changed: the next nudge is a new step
        if self._sel and self.ctx is not None:
            self._set_selection(self._refind(self._sel), primary=self._primary)
        else:
            self._refresh_panel()

    def _refind(self, objs: list[PageObject]) -> list[PageObject]:
        """``objs`` on the current document (after undo/redo: through the
        objects they were made from / turned into)."""
        out, seen = [], set()
        doc = self.ctx.pdf.doc
        for o in objs:
            cur = None
            try:
                cur = po.find_again(doc, o.page, o)
                if cur is None and o.key in self._lineage:
                    prev = self._lineage[o.key]
                    cur = po.find_again(doc, prev.page, prev)
            except Exception:
                cur = None
            if cur is not None and cur.key not in seen:
                seen.add(cur.key)
                out.append(cur)
        return out

    def _remember(self, old: list[PageObject], new: list[PageObject]) -> None:
        if len(self._lineage) > 400:
            self._lineage.clear()
        if len(old) == len(new):
            for a, b in zip(old, new):
                if a.key != b.key:
                    self._lineage[b.key] = a
                    self._lineage[a.key] = b

    # -- object lookup -----------------------------------------------------------
    def page_objects(self, page: int, *, alt: bool = False) -> list[PageObject]:
        """The objects on ``page`` in reading order (cached until the document changes)."""
        if page < 0 or self.ctx is None:
            return []
        k = (page, alt)
        if k not in self._objects:
            try:
                self._objects[k] = po.page_objects(self.ctx.pdf.doc, page, alt=alt)
            except Exception:
                self._objects[k] = []
        return self._objects[k]

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

    def object_at(self, page: int, pos: QPointF, *, alt: bool = False) -> PageObject | None:
        """The object under ``pos`` (text first, then the most specific
        graphic or image; thin images get 2 px of slack)."""
        if page < 0 or self.ctx is None:
            return None
        doc = self.ctx.pdf.doc
        try:
            hit = po.object_at(doc, page, (pos.x(), pos.y()), alt=alt)
        except Exception:
            hit = None
        if hit is None:
            tol = 2.0 / max(self.ctx.view.zoom, 0.05)
            img = core_objects.hit_test(self.images(page), (pos.x(), pos.y()), tolerance=tol)
            if img is not None and img.editable:
                try:
                    hit = po.find_again(doc, page, img)
                except Exception:
                    hit = None
        return hit

    def image_at(self, page: int, pos: QPointF) -> core_objects.ImageObject | None:
        hit = self.object_at(page, pos)
        return hit.ref if hit is not None and hit.kind == "image" else None

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
    def selection(self) -> list[PageObject]:
        """The selected objects (all on one page), the primary one first."""
        if self._primary is None:
            return list(self._sel)
        return [self._primary] + [o for o in self._sel if o.key != self._primary.key]

    @property
    def primary(self) -> PageObject | None:
        return self._primary

    @property
    def selected(self):
        """The engine object of the primary selection (e.g. the
        :class:`~pdfstudio.core.objects.ImageObject`), or None."""
        return self._primary.ref if self._primary is not None else None

    def selection_page(self) -> int:
        return self._sel[0].page if self._sel else -1

    def selected_image(self) -> core_objects.ImageObject | None:
        """The image when exactly one image is selected."""
        if len(self._sel) == 1 and self._sel[0].kind == "image":
            return self._sel[0].ref
        return None

    def selection_kind(self) -> str:
        """"none", "image", "text", "graphic" or "multi"."""
        if not self._sel:
            return "none"
        return self._sel[0].kind if len(self._sel) == 1 else "multi"

    def has_selection(self) -> bool:
        return bool(self._sel)

    def select(self, objs) -> None:
        """Select objects: a PageObject, an engine object (ImageObject,
        TextObject, GraphicObject), a list of them, or None (clear)."""
        self._end_burst()
        if objs is None:
            items = []
        elif isinstance(objs, (list, tuple)):
            items = list(objs)
        else:
            items = [objs]
        out = []
        for o in items:
            if not isinstance(o, PageObject):
                try:
                    o = po.find_again(self.ctx.pdf.doc, o.page, o)
                except Exception:
                    o = None
            if o is not None:
                out.append(o)
        self._set_selection(out)

    def select_all(self) -> bool:
        """Ctrl+A: every object on the current page."""
        if self.ctx is None:
            return False
        page = self.selection_page() if self._sel else self.ctx.view.current_page
        objs = self.page_objects(page)
        self._end_burst()
        self._set_selection(list(objs))
        return bool(objs)

    def _set_selection(self, objs: list[PageObject], *, primary: PageObject | None = None,
                       refresh: bool = True) -> None:
        if objs:
            page = objs[0].page
            seen, clean = set(), []
            for o in objs:
                if o.page == page and o.key not in seen:
                    seen.add(o.key)
                    clean.append(o)
            objs = clean
        old_keys = [o.key for o in self._sel]
        old_kind = self.selection_kind()
        self._sel = objs
        keys = {o.key for o in objs}
        if primary is not None and primary.key in keys:
            self._primary = next(o for o in objs if o.key == primary.key)
        elif self._primary is not None and self._primary.key in keys:
            self._primary = next(o for o in objs if o.key == self._primary.key)
        else:
            self._primary = objs[0] if objs else None
        if self._hover_key is not None and self._hover_key in keys:
            self._clear_hover()
        self._rebuild_frame()
        if not refresh:
            return
        if self.selection_kind() != old_kind and self._panel_class() is not type(self._panel):
            self.changed.emit()           # the inspector swaps the panel
        self._refresh_panel()
        if [o.key for o in objs] != old_keys or objs:
            self.selectionChanged.emit()

    # -- frame and handles ------------------------------------------------------------
    def _scalable(self) -> bool:
        """Corner handles: graphics and/or images, no text."""
        return bool(self._sel) and all(o.kind in ("graphic", "image") for o in self._sel)

    def _width_text(self) -> PageObject | None:
        """The single selected text block when its width can be changed
        (it runs left to right on screen)."""
        if len(self._sel) != 1 or self._sel[0].kind != "text":
            return None
        t = self._sel[0]
        lines = getattr(t.ref, "lines", None)
        if not lines:
            return None
        try:
            rot = self.ctx.pdf.doc[t.page].rotation_matrix
            d = pymupdf.Point(lines[0].dir) * pymupdf.Matrix(rot.a, rot.b, rot.c, rot.d, 0, 0)
        except Exception:
            return None
        return t if d.x > 0.99 and abs(d.y) < 0.02 else None

    def _frame_margins(self) -> tuple[float, float, float, float]:
        """How far the frame sits outside the selection's union on each
        side: text keeps a little air around its glyphs, a box around its
        text keeps its own edges."""
        u = _union(self._sel)
        p = pymupdf.Rect(u)
        for o in self._sel:
            if o.kind == "text":
                p |= o.rect + (-TEXT_PAD, -TEXT_PAD_Y, TEXT_PAD, TEXT_PAD_Y)
        return (u.x0 - p.x0, u.y0 - p.y0, p.x1 - u.x1, p.y1 - u.y1)

    def _frame_rect(self, union: QRectF | None = None) -> QRectF:
        """The frame for the selection's union ``union`` (default: as it is)."""
        r = union if union is not None else _qrect(_union(self._sel))
        ml, mt, mr, mb = self._frame_margins()
        return r.adjusted(-ml, -mt, mr, mb)

    def _rebuild_frame(self) -> None:
        self._clear_frame()
        if self._sel and self.ctx is not None:
            self._build_frame()

    def _build_frame(self) -> None:
        view = self.ctx.view
        page = self._sel[0].page
        accent = T.qcolor("accent")
        frame = QGraphicsRectItem(self._frame_rect())
        pen = QPen(accent, 1.5)
        pen.setCosmetic(True)
        frame.setPen(pen)
        frame.setZValue(Z_FRAME)
        hpen = QPen(accent, 1.5)
        hpen.setCosmetic(True)
        handles: list[tuple[int, QRectF]] = []
        if self._scalable():
            handles = [(i, QRectF(-HANDLE_PX / 2, -HANDLE_PX / 2, HANDLE_PX, HANDLE_PX)) for i in range(4)]
        elif self._width_text() is not None:
            side = QRectF(-SIDE_W_PX / 2, -SIDE_H_PX / 2, SIDE_W_PX, SIDE_H_PX)
            handles = [(SIDE_LEFT, side), (SIDE_RIGHT, side)]
        points = self._handle_points(frame.rect())
        for idx, shape in handles:
            h = QGraphicsRectItem(shape, frame)
            h.setFlag(QGraphicsItem.GraphicsItemFlag.ItemIgnoresTransformations, True)
            h.setPen(hpen)
            h.setBrush(T.qcolor("surface_raised"))
            h.setData(0, idx)
            h.setPos(points[idx])
            h.setZValue(1)
        try:
            view.add_overlay(page, frame)
        except IndexError:
            return
        self._frame = frame
        img = self.selected_image()
        if img is not None and not img.axis_aligned:   # the exact outline of a rotated image too
            outline = QGraphicsPolygonItem(_quad_polygon(img))
            open_pen = QPen(accent, 1.0)
            open_pen.setCosmetic(True)
            outline.setPen(open_pen)
            outline.setZValue(Z_FRAME - 1)
            view.add_overlay(page, outline)
            self._outline = outline
        if len(self._sel) > 1:                          # what belongs to the selection
            c = QColor(accent)
            c.setAlpha(130)
            mpen = QPen(c, 1.0)
            mpen.setCosmetic(True)
            fr = frame.rect()
            for o in self._sel:
                poly = self._outline_polygon(o)
                b = poly.boundingRect()
                if all(abs(a - z) < 0.3 for a, z in ((b.left(), fr.left()), (b.top(), fr.top()),
                                                      (b.right(), fr.right()), (b.bottom(), fr.bottom()))):
                    continue                            # the frame itself shows it
                item = QGraphicsPolygonItem(poly)
                item.setPen(mpen)
                item.setZValue(Z_MEMBER)
                view.add_overlay(page, item)
                self._members.append(item)

    @staticmethod
    def _outline_polygon(o: PageObject) -> QPolygonF:
        if o.kind == "image":
            return _quad_polygon(o.ref)
        r = _qrect(o.rect)
        if o.kind == "text":
            r = r.adjusted(-TEXT_PAD, -TEXT_PAD_Y, TEXT_PAD, TEXT_PAD_Y)
        return QPolygonF(r)

    @staticmethod
    def _handle_points(r: QRectF) -> dict[int, QPointF]:
        cy = r.center().y()
        return {0: r.topLeft(), 1: r.topRight(), 2: r.bottomRight(), 3: r.bottomLeft(),
                SIDE_LEFT: QPointF(r.left(), cy), SIDE_RIGHT: QPointF(r.right(), cy)}

    def _sync_frame(self, rect: QRectF) -> None:
        """Show the frame at ``rect`` (the selection's union, display pt)."""
        if self._frame is None:
            return
        fr = self._frame_rect(rect)
        self._frame.setRect(fr)
        pts = self._handle_points(fr)
        for h in self._frame.childItems():
            h.setPos(pts[int(h.data(0))])
        for item in [self._outline] + self._members:
            if item is not None:
                item.setVisible(False)   # stale while dragging

    def _clear_frame(self) -> None:
        for item in [self._frame, self._outline] + self._members:
            if item is not None and self.ctx is not None:
                self.ctx.view.remove_overlay(item)
        self._frame = None
        self._outline = None
        self._members = []

    def handle_at(self, page: int, pos: QPointF) -> int:
        """Number of the handle under ``pos`` (page coords): 0-3 the corners
        (top-left, clockwise), 4/5 the left/right side handles; -1 none."""
        if not self._sel or self._frame is None or page != self._sel[0].page:
            return -1
        handles = [int(h.data(0)) for h in self._frame.childItems()]
        if not handles:
            return -1
        zoom = max(self.ctx.view.zoom, 0.05)
        r = self._frame.rect()
        small = min(r.width(), r.height()) * zoom < 3 * HANDLE_PX
        if small and handles[0] < 4 and r.contains(pos):
            return -1        # a tiny object: inside means "move"
        pts = self._handle_points(r)
        for i in handles:
            c = pts[i]
            tx = ((SIDE_W_PX if i >= 4 else HANDLE_PX) / 2 + 3) / zoom
            ty = ((SIDE_H_PX if i >= 4 else HANDLE_PX) / 2 + 3) / zoom
            if abs(pos.x() - c.x()) <= tx and abs(pos.y() - c.y()) <= ty:
                return i
        return -1

    def _in_selection(self, page: int, pos: QPointF) -> bool:
        """Is ``pos`` inside the frame of a multi-selection?"""
        return (len(self._sel) > 1 and self._frame is not None and page == self._sel[0].page
                and self._frame.rect().contains(pos))

    # -- hover --------------------------------------------------------------------------
    def _set_hover(self, obj: PageObject | None) -> None:
        key = obj.key if obj is not None else None
        if key is not None and any(o.key == key for o in self._sel):
            key, obj = None, None     # the frame already shows it
        if key == self._hover_key:
            return
        self._clear_hover()
        self._hover_key = key
        if obj is None:
            return
        item = QGraphicsPolygonItem(self._outline_polygon(obj))
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
    @staticmethod
    def _mods(ev) -> tuple[bool, bool, bool]:
        m = ev.modifiers
        return (bool(m & Qt.KeyboardModifier.ShiftModifier),
                bool(m & (Qt.KeyboardModifier.ControlModifier | Qt.KeyboardModifier.MetaModifier)),
                bool(m & Qt.KeyboardModifier.AltModifier))

    def mouse_move(self, ev: ToolMouseEvent) -> None:
        if self._drag is not None:
            self._drag_to(ev)
            return
        view = self.ctx.view
        h = self.handle_at(ev.page, ev.pos)
        if h >= 0:
            self._set_hover(None)
            if h >= 4:
                view.set_tool_cursor(Qt.CursorShape.SizeHorCursor)
            else:
                view.set_tool_cursor(Qt.CursorShape.SizeFDiagCursor if h in (0, 2) else Qt.CursorShape.SizeBDiagCursor)
            return
        _shift, _ctrl, alt = self._mods(ev)
        hit = self.object_at(ev.page, ev.pos, alt=alt)
        self._set_hover(hit)
        over = hit is not None or self._in_selection(ev.page, ev.pos)
        view.set_tool_cursor(Qt.CursorShape.SizeAllCursor if over else Qt.CursorShape.ArrowCursor)

    def mouse_press(self, ev: ToolMouseEvent) -> None:
        if ev.button != Qt.MouseButton.LeftButton:
            return
        self._clear_stale_previews()
        self._end_burst()
        shift, ctrl, alt = self._mods(ev)
        toggle = shift or ctrl
        h = self.handle_at(ev.page, ev.pos)
        if h >= 0:
            sel = self.selection()
            self._drag = _Drag("width" if h >= 4 else "resize", sel[0].page, sel, QPointF(ev.pos),
                               _qrect(_union(sel)), h)
            return
        if ev.page < 0:
            if not toggle:
                self._set_selection([])
            return
        hit = self.object_at(ev.page, ev.pos, alt=alt)
        if hit is None:
            if not toggle and self._in_selection(ev.page, ev.pos):
                self._start_move(ev)
                return
            # empty space: a rubber band (a plain click clears on release)
            self._drag = _Drag("band", ev.page, self.selection(), QPointF(ev.pos), additive=toggle)
            return
        members = {o.key for o in self._sel}
        group = self._with_contents(hit, alt)
        if toggle:
            if hit.key in members and self._sel and self._sel[0].page == hit.page:
                drop = {o.key for o in group}
                self._set_selection([o for o in self._sel if o.key not in drop])
                return
            keep = self._sel if self._sel and self._sel[0].page == hit.page else []
            self._set_selection(keep + group, primary=hit)
        elif hit.key in members:
            self._start_move(ev, reduce_to=hit if len(self._sel) > 1 else None)
            return
        else:
            self._set_selection(group, primary=hit)
        self._start_move(ev)

    def _with_contents(self, hit: PageObject, alt: bool) -> list[PageObject]:
        """``hit`` plus – for a box graphic with "Medtag indhold" on – what it encloses (O4)."""
        if hit.kind != "graphic" or not self.include_contents or alt:
            return [hit]
        try:
            inside = po.contents_of(self.ctx.pdf.doc, hit.page, hit)
        except Exception:
            inside = []
        return [hit] + list(inside)

    def _start_move(self, ev: ToolMouseEvent, reduce_to: PageObject | None = None) -> None:
        sel = self.selection()
        if not sel:
            return
        self._drag = _Drag("move", sel[0].page, sel, QPointF(ev.pos), _qrect(_union(sel)), reduce_to=reduce_to)

    def mouse_release(self, ev: ToolMouseEvent) -> None:
        drag, self._drag = self._drag, None
        if drag is None:
            return
        self._clear_guides()
        if drag.kind == "band":
            self._finish_band(drag, ev)
            return
        if not drag.started or drag.rect is None:
            self._clear_previews()
            if drag.reduce_to is not None:
                self._set_selection([drag.reduce_to])
            return
        objs, new, old = drag.objs, drag.rect, drag.base
        page = drag.page
        pdf = self.ctx.pdf
        img = drag.objs[0].ref if len(objs) == 1 and objs[0].kind == "image" else None
        if drag.kind == "move":
            dx, dy = new.left() - old.left(), new.top() - old.top()
            if abs(dx) < 1e-6 and abs(dy) < 1e-6:
                self._clear_previews()
                return
            if img is not None:
                self._write(lambda: core_objects.move_image(pdf, page, img, dx, dy))
            else:
                self._write(lambda: po.move_objects(pdf, page, objs, dx, dy))
        elif drag.kind == "resize":
            if img is not None:
                self._write(lambda: core_objects.set_image_rect(
                    pdf, page, img, (new.left(), new.top(), new.right(), new.bottom())))
            else:
                self._write(lambda: po.scale_objects(pdf, page, objs, _prect(old), _prect(new)))
        else:   # width
            self._write_width(objs[0], old, new, drag.handle)

    def mouse_double_click(self, ev: ToolMouseEvent) -> None:
        """A double-click on text opens *Redigér tekst* with the caret there."""
        if ev.page < 0:
            return
        self._drag = None
        self._clear_previews()
        hit = self.object_at(ev.page, ev.pos)
        if hit is not None and hit.kind == "text":
            self.edit_text_at(ev.page, ev.pos)

    def edit_text_at(self, page: int, pos: QPointF) -> bool:
        """Switch to *Redigér tekst* and open the text surface at ``pos``
        with the caret at that point."""
        try:
            hit = flow.story_at(self.ctx.pdf.doc, page, (pos.x(), pos.y()))
        except Exception:
            hit = None
        ws = self.ctx.workspace
        if hit is None or ws is None:
            return False
        ws.set_tool("edit.text")
        tool = getattr(ws, "tool", None)
        if tool is None or tool.id != "edit.text" or not hasattr(tool, "start_story"):
            return False
        tool.start_story(hit[0], click=(page, QPointF(pos)))
        return True

    # -- rubber band ----------------------------------------------------------------------
    def _show_band(self, drag: _Drag, p: QPointF) -> None:
        r = QRectF(drag.press, p).normalized()
        drag.rect = r
        if self._band is None:
            item = QGraphicsRectItem(r)
            color = T.qcolor("accent")
            pen = QPen(color, 1.0)
            pen.setCosmetic(True)
            item.setPen(pen)
            fill = QColor(color)
            fill.setAlpha(22)
            item.setBrush(fill)
            item.setZValue(Z_BAND)
            try:
                self.ctx.view.add_overlay(drag.page, item)
            except IndexError:
                return
            self._band = item
            self._clear_hover()
        else:
            self._band.setRect(r)

    def _clear_band(self) -> None:
        if self._band is not None and self.ctx is not None:
            self.ctx.view.remove_overlay(self._band)
        self._band = None

    def _finish_band(self, drag: _Drag, ev: ToolMouseEvent) -> None:
        self._clear_band()
        if not drag.started or drag.rect is None:
            if not drag.additive:
                self._set_selection([])
            return
        _shift, _ctrl, alt = self._mods(ev)
        try:
            found = po.objects_in(self.ctx.pdf.doc, drag.page, _prect(drag.rect), alt=alt)
        except Exception:
            found = []
        if drag.additive and drag.objs and drag.objs[0].page == drag.page:
            self._set_selection(list(drag.objs) + found)
        else:
            self._set_selection(found)

    # -- dragging ---------------------------------------------------------------------------
    def _drag_to(self, ev: ToolMouseEvent) -> None:
        drag = self._drag
        p = ev.pos_on(drag.page)
        d = p - drag.press
        if not drag.started:
            if math.hypot(d.x(), d.y()) * self.ctx.view.zoom < DRAG_START_PX:
                return
            drag.started = True
            if drag.kind == "band":
                pass
            elif drag.kind == "width":
                self._clear_hover()
                self._badge = _Badge()
                self.ctx.view.add_overlay(drag.page, self._badge)
            else:
                self._start_previews(drag)
        if drag.kind == "band":
            self._show_band(drag, p)
            return
        shift, _ctrl, alt = self._mods(ev)
        old = drag.base
        if drag.kind == "move":
            lock_x = lock_y = False
            if shift:        # constrain to the dominant axis
                if abs(d.x()) >= abs(d.y()):
                    d, lock_y = QPointF(d.x(), 0), True
                else:
                    d, lock_x = QPointF(0, d.y()), True
            dx, dy = d.x(), d.y()
            guides: list[QLineF] = []
            if not alt:
                tol = SNAP_PX / max(self.ctx.view.zoom, 0.05)
                dx, dy, guides = snap_delta(old, dx, dy, drag.xs, drag.ys, tol, lock_x=lock_x, lock_y=lock_y)
            cx, cy = self._clamp_move(drag.page, old, dx, dy)
            if (cx, cy) != (dx, dy):
                guides = []
            rect = old.translated(cx, cy)
            self._show_guides(drag.page, guides)
            self._badge_text(tr("objects.badge.pos", x=_mm(rect.left()), y=_mm(rect.top())))
        elif drag.kind == "resize":
            # Shift frees the ratio – not for a rotated image (it would skew)
            keep = not shift or any(not o.ref.axis_aligned for o in drag.objs if o.kind == "image")
            rect = self._resize_rect(_prect(old), drag.handle, p, keep=keep, centre=alt)
            self._badge_text(tr("objects.badge.size", w=_mm(rect.width()), h=_mm(rect.height())))
        else:   # width of a text block
            if drag.handle == SIDE_RIGHT:
                w = max(old.width() + d.x(), MIN_TEXT_WIDTH)
                rect = QRectF(old.left(), old.top(), w, old.height())
            else:
                w = max(old.width() - d.x(), MIN_TEXT_WIDTH)
                rect = QRectF(old.right() - w, old.top(), w, old.height())
            self._badge_text(tr("objects.badge.width", w=_mm(rect.width())))
        drag.rect = rect
        self._show_preview(drag, rect)

    def _clamp_move(self, page: int, base: QRectF, dx: float, dy: float) -> tuple[float, float]:
        """Keep the selection's centre on the page."""
        pr = self.ctx.pdf.page(page).rect
        c = base.center()
        dx = min(max(dx, pr.x0 - c.x()), pr.x1 - c.x())
        dy = min(max(dy, pr.y0 - c.y()), pr.y1 - c.y())
        return dx, dy

    def _snap_targets(self, page: int, sel: list[PageObject]) -> tuple[list, list]:
        """Edges and centres of the page's other objects and of the page."""
        keys = {o.key for o in sel}
        xs, ys = [], []
        for o in self.page_objects(page):
            if o.key in keys:
                continue
            r = _qrect(o.rect)
            for v in (r.left(), r.center().x(), r.right()):
                xs.append((v, r))
            for v in (r.top(), r.center().y(), r.bottom()):
                ys.append((v, r))
        pr = _qrect(self.ctx.pdf.page(page).rect)
        for v in (pr.left(), pr.center().x(), pr.right()):
            xs.append((v, pr))
        for v in (pr.top(), pr.center().y(), pr.bottom()):
            ys.append((v, pr))
        return xs, ys

    def _show_guides(self, page: int, lines: list[QLineF]) -> None:
        self._clear_guides()
        if not lines:
            return
        pen = QPen(T.qcolor("accent"), 1.0)
        pen.setCosmetic(True)
        for ln in lines:
            item = QGraphicsLineItem(ln)
            item.setPen(pen)
            item.setZValue(Z_GUIDE)
            try:
                self.ctx.view.add_overlay(page, item)
            except IndexError:
                return
            self._guides.append(item)

    def _clear_guides(self) -> None:
        for item in self._guides:
            if self.ctx is not None:
                self.ctx.view.remove_overlay(item)
        self._guides = []

    @staticmethod
    def _resize_rect(r, corner: int, p: QPointF, *, keep: bool, centre: bool) -> QRectF:
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

    def _start_previews(self, drag: _Drag) -> None:
        """Lift the selection: cover its old place with the page as it looks
        without it, and create the translucent copy that follows the pointer."""
        self._clear_hover()
        view = self.ctx.view
        doc = self.ctx.pdf.doc
        objs, page = drag.objs, drag.page
        scale = view.zoom * view.dpr()
        area = _union(objs) + (-PREVIEW_PAD, -PREVIEW_PAD, PREVIEW_PAD, PREVIEW_PAD)
        area &= doc[page].rect
        if drag.kind == "move":
            drag.xs, drag.ys = self._snap_targets(page, objs)
        try:
            pix = po.render_without(doc, page, objs, scale, clip=area)
            lift = QGraphicsPixmapItem(QPixmap.fromImage(to_qimage(pix)))
            lift.setTransformationMode(Qt.TransformationMode.SmoothTransformation)
            lift.setTransform(QTransform.fromScale(1 / scale, 1 / scale))
            lift.setPos(pix.x / scale, pix.y / scale)
            lift.setZValue(Z_LIFT)
            view.add_overlay(page, lift)
            self._lift = lift
        except Exception:
            self._lift = None
        img = objs[0].ref if len(objs) == 1 and objs[0].kind == "image" else None
        pm, base = None, QTransform()
        if img is not None:
            pm = self._pixmap(img)
            if pm is not None:
                base = _unit_transform(img, pm.width(), pm.height())
        else:
            try:
                pix = po.render_only(doc, page, objs, scale, clip=area)
                pm = QPixmap.fromImage(to_qimage(pix))
                base = QTransform.fromScale(1 / scale, 1 / scale) * QTransform.fromTranslate(pix.x / scale,
                                                                                            pix.y / scale)
            except Exception:
                pm = None
        if pm is not None:
            ghost = QGraphicsPixmapItem(pm)
            ghost.setTransformationMode(Qt.TransformationMode.SmoothTransformation)
            ghost.setOpacity(GHOST_OPACITY)
            ghost.setZValue(Z_GHOST)
            ghost.setTransform(base)
            view.add_overlay(page, ghost)
            self._ghost = ghost
            self._ghost_base = base
        self._badge = _Badge()
        view.add_overlay(page, self._badge)

    def _show_preview(self, drag: _Drag, rect: QRectF) -> None:
        if self._ghost is not None:
            self._ghost.setTransform(self._ghost_base * _bbox_map(drag.base, rect))
        self._sync_frame(rect)
        if self._badge is not None:
            fr = self._frame_rect(rect)
            self._badge.setPos(fr.center().x(), fr.bottom())

    def _badge_text(self, text: str) -> None:
        if self._badge is not None:
            self._badge.set_text(text)

    def _clear_previews(self) -> None:
        for item in (self._ghost, self._lift, self._badge):
            if item is not None and self.ctx is not None:
                self.ctx.view.remove_overlay(item)
        self._ghost = self._lift = self._badge = None
        self._clear_guides()
        self._clear_band()
        if self._sel and self._frame is not None:
            self._rebuild_frame()

    def _cancel_drag(self) -> None:
        self._drag = None
        self._clear_previews()

    # -- writing ------------------------------------------------------------------------------
    def _as_objects(self, page: int, result) -> list[PageObject]:
        if result is None:
            return []
        if isinstance(result, PageObject):
            return [result]
        if isinstance(result, (list, tuple)):
            out = []
            for r in result:
                out += self._as_objects(page, r)
            return out
        try:
            found = po.find_again(self.ctx.pdf.doc, page, result)
        except Exception:
            found = None
        return [found] if found is not None else []

    def _write(self, fn, *, select: bool = True) -> list[PageObject] | None:
        """Run a core edit and select its result. The drag previews stay
        until the page has been re-rendered with the change (no flash of the
        old state). Returns the resulting objects (None on failure)."""
        stale = [it for it in (self._ghost, self._lift, self._badge) if it is not None]
        self._ghost = self._lift = self._badge = None
        self._clear_guides()
        before = list(self._sel)
        page = self.selection_page()
        self._busy = True
        try:
            result = fn()
        except Exception as exc:  # noqa: BLE001
            self._busy = False
            for it in stale:
                self.ctx.view.remove_overlay(it)
            self._set_selection(self._refind(before) if before else [])
            self.ctx.toast(tr("objects.error.edit", err=str(exc)), "error")
            return None
        self._busy = False
        self._stale_previews.extend(stale)
        if stale:
            self._stale_page = page
            QTimer.singleShot(600, self, self._clear_stale_previews)   # fallback
        new = self._as_objects(page, result)
        if select:
            self._remember(before, new)
            if new:
                prim = None
                if self._primary is not None:
                    for a, b in zip(before, new):
                        if a.key == self._primary.key:
                            prim = b
                self._set_selection(new, primary=prim)
            else:
                self._set_selection(self._refind(before))
        return new

    def _write_width(self, obj: PageObject, old: QRectF, new: QRectF, handle: int) -> None:
        width = new.width()
        if abs(width - old.width()) < 0.25:
            self._clear_previews()
            return
        pdf, page = self.ctx.pdf, obj.page
        dx = new.left() - old.left()

        def run():
            with pdf.edit(tr("objects.undo.width"), CONTENT, (page,)):
                t = po.set_text_width(pdf, page, obj, width)
                if handle == SIDE_LEFT and abs(dx) > 1e-3:
                    moved = po.move_objects(pdf, page, [t], dx, 0.0)
                    t = moved[0] if moved else t
            return t

        self._write(run)

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

    def _warm(self) -> None:
        if self.active and self.ctx is not None and self._drag is None:
            self.page_objects(self.ctx.view.current_page)

    def _on_current_page(self, _page: int) -> None:
        self._warm_timer.start()
        if self._panel is not None and not self._sel:
            self._panel.refresh()

    # -- keyboard ---------------------------------------------------------------------------------
    _CTRL_KEYS = (Qt.Key.Key_C, Qt.Key.Key_X, Qt.Key.Key_V, Qt.Key.Key_D)

    def eventFilter(self, obj, ev) -> bool:  # noqa: N802
        """Window shortcuts (Ctrl+D "Duplikér sider", Ctrl+C …) must not fire
        while this tool handles the key itself: claim it (ShortcutOverride),
        then the view passes the key press on to :meth:`key_press`."""
        if ev.type() == QEvent.Type.ShortcutOverride and self.active and self._wants_key(ev):
            ev.accept()
            return True
        return False

    def _wants_key(self, ev: QKeyEvent) -> bool:
        mods = ev.modifiers() & ~Qt.KeyboardModifier.KeypadModifier
        ctrl = Qt.KeyboardModifier.ControlModifier
        if mods != ctrl or ev.key() not in self._CTRL_KEYS:
            return False
        if self.ctx is not None and self.ctx.view.scene().focusItem() is not None:
            return False
        if ev.key() == Qt.Key.Key_V:
            return clipboard.current() is not None
        return bool(self._sel)

    def key_press(self, ev: QKeyEvent) -> bool:
        key = ev.key()
        mods = ev.modifiers() & ~Qt.KeyboardModifier.KeypadModifier
        shift = bool(mods & Qt.KeyboardModifier.ShiftModifier)
        ctrl = bool(mods & Qt.KeyboardModifier.ControlModifier)
        if ctrl and not shift and key in self._CTRL_KEYS:
            if key == Qt.Key.Key_V:
                return self.paste()
            if not self._sel:
                return False
            if key == Qt.Key.Key_C:
                return self.copy()
            if key == Qt.Key.Key_X:
                return self.cut()
            return self.duplicate()
        if key in (Qt.Key.Key_Tab, Qt.Key.Key_Backtab):
            return self.select_next(-1 if key == Qt.Key.Key_Backtab or shift else 1)
        if key == Qt.Key.Key_Escape:
            if self._drag is not None:
                self._cancel_drag()
                return True
            if self._sel:
                self.select(None)
                return True
            return False
        if not self._sel:
            return False
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
        """Tab / Shift+Tab: the next / previous object on the page."""
        page = self.selection_page() if self._sel else self.ctx.view.current_page
        objs = self.page_objects(page)
        if not objs:
            return False
        keys = [o.key for o in objs]
        cur = self._primary.key if self._primary is not None else None
        if cur in keys:
            nxt = objs[(keys.index(cur) + step) % len(objs)]
        else:
            nxt = objs[0] if step > 0 else objs[-1]
        self.select(nxt)
        self.ctx.view.ensure_visible_rect(page, _qrect(nxt.rect))
        return True

    def nudge(self, dx: float, dy: float) -> None:
        sel = self.selection()
        if not sel or self._drag is not None:
            return
        page = sel[0].page
        dx, dy = self._clamp_move(page, _qrect(_union(sel)), dx, dy)
        if not dx and not dy:
            return
        merge = self._nudge_burst
        pdf = self.ctx.pdf
        img = self.selected_image()
        if img is not None:
            self._write(lambda: core_objects.move_image(pdf, page, img, dx, dy, merge=merge))
        else:
            self._write(lambda: po.move_objects(pdf, page, sel, dx, dy, merge=merge))
        self._nudge_burst = True
        self._nudge_timer.start()

    def _end_burst(self) -> None:
        self._nudge_burst = False
        self._nudge_timer.stop()

    # -- public actions (also used by the inspector) ---------------------------------------------
    def move_selection_to(self, x: float, y: float) -> None:
        """Move the selection so its top-left corner is at ``(x, y)`` (display pt)."""
        sel = self.selection()
        if not sel:
            return
        r = _union(sel)
        dx, dy = x - r.x0, y - r.y0
        if abs(dx) < 1e-3 and abs(dy) < 1e-3:
            return
        pdf, page = self.ctx.pdf, sel[0].page
        img = self.selected_image()
        if img is not None:
            self._write(lambda: core_objects.move_image(pdf, page, img, dx, dy))
        else:
            self._write(lambda: po.move_objects(pdf, page, sel, dx, dy))

    def set_selection_rect(self, rect) -> None:
        """Scale the selection (graphics and images) so its box becomes
        ``rect`` (QRectF or (x0, y0, x1, y1), display pt)."""
        sel = self.selection()
        if not sel:
            return
        r = _prect(rect) if isinstance(rect, QRectF) else pymupdf.Rect(rect)
        r.normalize()
        if r.width < 0.5 or r.height < 0.5:
            return
        old = _union(sel)
        if all(abs(a - b) < 1e-3 for a, b in zip(old, r)):
            return
        pdf, page = self.ctx.pdf, sel[0].page
        img = self.selected_image()
        if img is not None:
            self._write(lambda: core_objects.set_image_rect(pdf, page, img, tuple(r)))
        elif self._scalable():
            self._write(lambda: po.scale_objects(pdf, page, sel, old, r))

    def set_rect(self, rect: QRectF) -> None:
        """Place the selected image's (or graphics') bounding box at ``rect``."""
        self.set_selection_rect(rect)

    def set_text_width(self, width: float) -> None:
        """Wrap the single selected text block at ``width`` pt (start edge kept)."""
        t = self._sel[0] if len(self._sel) == 1 and self._sel[0].kind == "text" else None
        if t is None or width <= 0:
            return
        r = _qrect(t.rect)
        self._write_width(t, r, QRectF(r.left(), r.top(), float(width), r.height()), SIDE_RIGHT)

    def set_style(self, stroke=KEEP, fill=KEEP, width: float | None = None) -> None:
        """Stroke colour, fill colour (None removes it) and/or stroke width of
        the selected graphics."""
        sel = self.selection()
        if not any(o.kind == "graphic" for o in sel):
            return
        if stroke is KEEP and fill is KEEP and width is None:
            return
        pdf, page = self.ctx.pdf, sel[0].page
        self._write(lambda: po.set_style(pdf, page, sel, stroke=stroke, fill=fill, width=width))

    def restack(self, front: bool = True) -> None:
        """"Læg forrest" / "Læg bagerst"."""
        sel = self.selection()
        if sel:
            pdf, page = self.ctx.pdf, sel[0].page
            self._write(lambda: po.restack(pdf, page, sel, front=front))

    @staticmethod
    def _load_include() -> bool:
        try:
            v = _settings().value(SETTINGS_INCLUDE, True)
        except Exception:
            return True
        return str(v).lower() not in ("false", "0", "no")

    def set_include_contents(self, on: bool) -> None:
        """"Medtag indhold" (O4): a click on a box selects what it encloses too."""
        self.include_contents = bool(on)
        try:
            _settings().setValue(SETTINGS_INCLUDE, self.include_contents)
        except Exception:
            pass
        self._refresh_panel()

    def delete_selected(self) -> None:
        sel = self.selection()
        if not sel:
            return
        pdf, page = self.ctx.pdf, sel[0].page
        img = self.selected_image()
        self._end_burst()
        self._set_selection([])
        try:
            if img is not None:
                core_objects.delete_image(pdf, page, img)
            else:
                po.delete_objects(pdf, page, sel)
        except Exception as exc:  # noqa: BLE001
            self.ctx.toast(tr("objects.error.edit", err=str(exc)), "error")
            return
        if img is not None:
            text = tr("objects.deleted")
        elif len(sel) == 1:
            text = tr("objects.deleted.one")
        else:
            text = tr("objects.deleted.many", n=len(sel))
        self.ctx.toast(text, "info", action_text=tr("objects.undo_action"), on_action=pdf.undo)

    def rotate_selected(self, degrees: float = 90) -> None:
        obj = self.selected_image()
        if obj is not None:
            self._write(lambda: core_objects.rotate_image(self.ctx.pdf, obj.page, obj, degrees))

    def flip_selected(self) -> None:
        obj = self.selected_image()
        if obj is not None:
            self._write(lambda: core_objects.flip_image(self.ctx.pdf, obj.page, obj))

    def replace_selected(self, path: str | None = None) -> bool:
        obj = self.selected_image()
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
        self.select(new)
        self.ctx.toast(tr("objects.replaced"), "success")
        return True

    def save_selected(self, path: str | None = None) -> str | None:
        obj = self.selected_image()
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
    def copy(self) -> bool:
        """Ctrl+C: the selection to the in-app clipboard (works across tabs
        and documents); a single image also goes to the system clipboard
        (full resolution), text as plain text."""
        sel = self.selection()
        if not sel or self.ctx is None:
            return False
        doc, page = self.ctx.pdf.doc, sel[0].page
        try:
            clip = po.copy_objects(doc, page, sel)
        except Exception as exc:  # noqa: BLE001
            self.ctx.toast(tr("objects.error.copy", err=str(exc)), "error")
            return False
        image = None
        img = self.selected_image()
        if img is not None:
            try:
                image = to_qimage(core_objects.image_pixmap(doc, img))
            except Exception:
                image = None
        texts = [o for o in sorted(sel, key=lambda o: (round(o.rect.y0, 1), o.rect.x0)) if o.kind == "text"]
        text = "\n".join(getattr(o.ref, "text", "") for o in texts)
        clipboard.put(clip, self.ctx.pdf, page, len(sel), image=image, text=text)
        return True

    def cut(self) -> bool:
        """Ctrl+X: copy, then delete."""
        if not self.copy():
            return False
        self.delete_selected()
        return True

    def can_paste(self) -> bool:
        return clipboard.current() is not None

    def paste(self, page: int | None = None) -> bool:
        """Ctrl+V: paste the in-app clipboard onto ``page`` (default: the
        current page) – where it was on its page; on the source page again
        10 pt further down/right with every paste. Selects what was pasted."""
        entry = clipboard.current()
        if entry is None or self.ctx is None:
            return False
        pdf = self.ctx.pdf
        if page is None:
            page = self.ctx.view.current_page
        if not (0 <= page < pdf.page_count):
            return False
        dx = dy = 0.0
        if clipboard.is_source(entry, pdf, page):
            entry.pastes += 1
            dx = dy = PASTE_STEP * entry.pastes
        dx, dy = self._fit_offset(page, entry.clip.rect, dx, dy)
        self._end_burst()
        self._set_selection([])
        new = self._write(lambda: po.paste_clip(pdf, page, entry.clip, offset=(dx, dy)))
        if new:
            self._set_selection(new)
            self.ctx.view.ensure_visible_rect(page, _qrect(_union(new)))
        return new is not None

    def _fit_offset(self, page: int, rect, dx: float, dy: float) -> tuple[float, float]:
        """Shift a paste so it lands on the page (a smaller page, many pastes)."""
        pr = self.ctx.pdf.page(page).rect
        r = pymupdf.Rect(rect) + (dx, dy, dx, dy)
        if r.x1 > pr.x1:
            dx -= min(r.x1 - pr.x1, r.x0 - pr.x0)
        if r.y1 > pr.y1:
            dy -= min(r.y1 - pr.y1, r.y0 - pr.y0)
        r = pymupdf.Rect(rect) + (dx, dy, dx, dy)
        if r.x0 < pr.x0:
            dx += pr.x0 - r.x0
        if r.y0 < pr.y0:
            dy += pr.y0 - r.y0
        return dx, dy

    def duplicate(self) -> bool:
        """Ctrl+D: a copy of the selection 10 pt down/right, selected."""
        sel = self.selection()
        if not sel:
            return False
        pdf, page = self.ctx.pdf, sel[0].page
        dx, dy = self._fit_offset(page, _union(sel), PASTE_STEP, PASTE_STEP)
        self._end_burst()
        new = self._write(lambda: po.duplicate_objects(pdf, page, sel, offset=(dx, dy)), select=False)
        if new:
            self._set_selection(new)
        return bool(new)

    # -- inspector -----------------------------------------------------------------------------------
    def _panel_class(self) -> type:
        panels = {"none": ImageObjectPanel, "image": ImageObjectPanel, "text": ObjectPanel,
                  "graphic": ObjectPanel, "multi": ObjectPanel}
        panels.update(type(self).PANELS)
        return panels[self.selection_kind()]

    def properties_widget(self) -> QWidget:
        cls = self._panel_class()
        panel = self._panels.get(cls)
        if panel is None:
            panel = cls(self)
            self._panels[cls] = panel
        self._panel = panel
        panel.refresh()
        return panel

    def _refresh_panel(self) -> None:
        if self._panel is not None and type(self._panel) is self._panel_class():
            self._panel.refresh()


# ---------------------------------------------------------------------------
# inspector panels
# ---------------------------------------------------------------------------
def _spin(tip: str, lo: float, hi: float) -> QDoubleSpinBox:
    s = QDoubleSpinBox()
    s.setRange(lo, hi)
    s.setDecimals(1)
    s.setSingleStep(1.0)
    s.setSuffix(tr("objects.unit.mm"))
    s.setKeyboardTracking(False)        # commit on Enter / focus out / arrows – not per keystroke
    s.setToolTip(tip)
    s.setMinimumWidth(78)
    # compact inspector fields (Figma-style): arrow keys / wheel still step
    # the value; the step buttons only ate the room "120,0 mm" needs
    s.setButtonSymbols(QDoubleSpinBox.ButtonSymbols.NoButtons)
    s.setAlignment(Qt.AlignmentFlag.AlignRight)
    return s


def _row_button(owner, layout, icon_name: str, text: str, slot, tip: str = "", role: str = "text_muted") -> QToolButton:
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
    owner._buttons.append((b, icon_name, role))
    b.setIcon(icon(icon_name, role))
    return b


class ObjectPanel(QWidget):
    """Inspector for any selection that is not a single image: what is
    selected and where (X/Y in mm). WP-D2 replaces it per kind through
    :attr:`EditObjectTool.PANELS`."""

    def __init__(self, tool: EditObjectTool) -> None:
        super().__init__()
        self.tool = tool
        self._loading = False
        self._buttons: list[tuple[QToolButton, str, str]] = []
        v = QVBoxLayout(self)
        v.setContentsMargins(16, 4, 16, 16)
        v.setSpacing(10)

        card = QFrame()
        card.setObjectName("Card")
        cl = QVBoxLayout(card)
        cl.setContentsMargins(14, 12, 14, 12)
        cl.setSpacing(4)
        self.title = label("", "heading")
        self.detail = label("", "muted", wrap=True)
        self.meta = label("", "caption", wrap=True)
        for w in (self.title, self.detail, self.meta):
            cl.addWidget(w)
        v.addWidget(card)

        self.edit_box = QWidget()
        ev = QVBoxLayout(self.edit_box)
        ev.setContentsMargins(0, 4, 0, 0)
        ev.setSpacing(8)
        ev.addWidget(label(tr("objects.section.position"), "section"))
        grid = QGridLayout()
        grid.setHorizontalSpacing(6)
        grid.setVerticalSpacing(8)
        self.x = _spin(tr("objects.field.x.tip"), -2000, 5000)
        self.y = _spin(tr("objects.field.y.tip"), -2000, 5000)
        for col, (key, spin) in enumerate((("x", self.x), ("y", self.y))):
            grid.addWidget(label(tr(f"objects.field.{key}"), "caption"), 0, col * 3)
            grid.addWidget(spin, 0, col * 3 + 1)
        grid.setColumnStretch(1, 1)
        grid.setColumnStretch(4, 1)
        grid.setColumnMinimumWidth(2, 26)
        ev.addLayout(grid)
        self.size = label("", "caption")
        ev.addWidget(self.size)
        for spin in (self.x, self.y):
            spin.valueChanged.connect(lambda _v: self._field_changed())
        ev.addSpacing(6)
        ev.addWidget(label(tr("objects.section.arrange"), "section"))
        self.delete_btn = _row_button(self, ev, "trash", tr("objects.action.delete_sel"), tool.delete_selected,
                                      role="danger")
        v.addWidget(self.edit_box)
        v.addSpacing(2)
        v.addWidget(label(tr("objects.tips.general"), "caption", wrap=True))
        v.addStretch(1)
        on_theme_change(self, self._refresh_icons)
        self.refresh()

    def _refresh_icons(self) -> None:
        for b, name, role in self._buttons:
            b.setIcon(icon(name, role))
        self.refresh()

    def refresh(self) -> None:
        sel = self.tool.selection()
        self.edit_box.setVisible(bool(sel))
        self.title.setText(kind_title(sel))
        if not sel:
            self.detail.setText(tr("objects.none.hint"))
            self.meta.setText("")
            self.meta.setVisible(False)
            return
        r = _union(sel)
        self.detail.setText(self._detail(sel))
        meta = self._meta(sel)
        self.meta.setText(meta)
        self.meta.setVisible(bool(meta))
        self.size.setText(tr("objects.size", w=_mm(r.width), h=_mm(r.height)))
        self._loading = True
        try:
            for spin, val in ((self.x, r.x0), (self.y, r.y0)):
                if not spin.hasFocus() or abs(spin.value() - val * MM_PER_PT) > 0.05:
                    spin.setValue(val * MM_PER_PT)
        finally:
            self._loading = False

    @staticmethod
    def _detail(sel: list[PageObject]) -> str:
        if len(sel) > 1:
            return count_text(sel)
        o = sel[0]
        if o.kind == "text":
            text = " ".join(str(getattr(o.ref, "text", "")).split())
            return tr("objects.quote", text=text if len(text) <= 80 else text[:78].rstrip() + "…")
        if o.kind == "graphic":
            return tr(f"objects.graphic.{getattr(o.ref, 'kind', 'shape')}")
        return tr("objects.kind.image")

    @staticmethod
    def _meta(sel: list[PageObject]) -> str:
        if len(sel) != 1:
            return ""
        o = sel[0]
        if o.kind == "text":
            lines = getattr(o.ref, "lines", None) or []
            if lines:
                st = lines[0].style
                size = QLocale().toString(float(st.size), "f", 1).rstrip("0").rstrip(QLocale().decimalPoint())
                return tr("objects.text.style", font=st.font, size=size)
        if o.kind == "graphic":
            g = o.ref
            if getattr(g, "stroke", None) is not None and getattr(g, "width", 0) > 0:
                w = QLocale().toString(float(g.width), "f", 1).rstrip("0").rstrip(QLocale().decimalPoint())
                return tr("objects.graphic.stroke", w=w)
        return ""

    def _field_changed(self) -> None:
        if self._loading or not self.tool.selection():
            return
        self.tool.move_selection_to(self.x.value() / MM_PER_PT, self.y.value() / MM_PER_PT)


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
                # paper tones in both themes: the image is shown as it sits on the page
                light = T.qcolor("page")
                dark = QColor(T.blend("page", "text_subtle", 0.22))
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
    """Inspector for one selected image (and for "nothing selected"): what it
    is and what can be done with it."""

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
        return _spin(tip, lo, hi)

    def _row(self, layout, icon_name: str, text: str, slot, tip: str = "", role: str = "text_muted") -> QToolButton:
        return _row_button(self, layout, icon_name, text, slot, tip, role)

    def _refresh_icons(self) -> None:
        for b, name, role in self._buttons:
            b.setIcon(icon(name, role))
        self.refresh()

    # -- state -----------------------------------------------------------------------------------
    def refresh(self) -> None:
        tool = self.tool
        obj = tool.selected_image()
        self.edit_box.setVisible(obj is not None)
        self.thumb.setVisible(obj is not None)
        if obj is None:
            # nothing selected: what there is to select on the current page
            self.title.setText(tr("objects.none.title"))
            self.detail.setText(tr("objects.none.hint"))
            page = tool.ctx.view.current_page if tool.ctx is not None else -1
            objs = tool.page_objects(page) if page >= 0 else []
            if objs:
                self.meta.setText(tr("objects.count.page", page=page + 1, what=count_text(objs)))
            else:
                self.meta.setText(tr("objects.count.zero", page=page + 1) if page >= 0 else "")
            self.meta.setVisible(page >= 0)
            set_prop(self.meta, "role", "caption")
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
        set_prop(self.meta, "role", "danger" if 0 < ppi < LOW_PPI else "caption")
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
        obj = self.tool.selected_image()
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


__all__ = ["EditObjectTool", "ImageObjectPanel", "ObjectPanel", "snap_delta"]
