""""Markér kommentar" – select, move, restyle and delete annotations.

* Click selects a comment (accent frame); Shift/Ctrl+click adds or removes
  one; dragging on empty page area draws a selection rectangle; Ctrl+A
  selects every comment on the current page.
* Dragging a selected comment moves the whole selection; a single comment
  can be resized with its corner handles. Arrow keys nudge the selection by
  1 pt (Shift: 10 pt) – a burst of nudges is one undo step.
* Delete / Backspace removes the selection, Esc clears it.
* Double-click (or Enter) edits a note's text or a text box in place.
* Dragging over text that is not a comment selects the text (characters,
  double click: word, triple click: paragraph) and shows the mini toolbar
  (Kommentér, Fremhæv, …) – like Acrobat's comment tool.
* The inspector shows the selection's common style; changes apply to all
  selected comments as one undo step, and hovering a colour previews it on
  the page before anything is written.

``selectionChanged`` lets the comments panel follow the selection.
"""

from __future__ import annotations

import math
from dataclasses import replace

import pymupdf
from PySide6.QtCore import QPointF, QRectF, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QKeyEvent, QPen
from PySide6.QtWidgets import QFrame, QGraphicsItem, QGraphicsRectItem, QVBoxLayout, QWidget

from ....core import RELOADED, STRUCTURE
from ....core import annotations as core_annots
from ....i18n import tr
from ...theme import tokens as T
from ...widgets.util import label
from ._annot_common import (
    FILL_KINDS,
    TEXT_KINDS,
    WIDTH_KINDS,
    AnnotStyle,
    AnnotStylePanel,
    PagePreview,
    ask_note,
    same_color,
)
from ..text_selection import TextSelection
from ._text_actions import SelectionUI
from .annotate_freetext import FreeTextSession, is_auto_width, style_of
from .base import Tool, ToolMouseEvent, ToolRegistry

FRAME_PAD = 3.0
HANDLE_PX = 9.0
MIN_SIZE = 4.0                   # pt
FIXED_SIZE_KINDS = ("note",)     # icons keep their size
DRAG_START = 2.0                 # pt before a press becomes a move / band
NUDGE = 1.0                      # pt per arrow key press (Shift: NUDGE_BIG)
NUDGE_BIG = 10.0
NUDGE_BURST_MS = 900             # nudges closer together than this are one undo step
_ARROWS = {Qt.Key.Key_Left: (-1, 0), Qt.Key.Key_Right: (1, 0), Qt.Key.Key_Up: (0, -1), Qt.Key.Key_Down: (0, 1)}

Key = tuple[int, int]  # (page, xref)


def _key(info: core_annots.AnnotInfo) -> Key:
    return (info.page, info.xref)


@ToolRegistry.register
class AnnotSelectTool(Tool):
    id = "annotate.select"
    label = tr("annot.select")
    hint = tr("annot.select.hint")
    icon = "cursor"
    shortcut = "V"
    mode = "annotate"
    order = 0
    cursor = Qt.CursorShape.ArrowCursor

    selectionChanged = Signal()

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.selection: list[core_annots.AnnotInfo] = []
        self._frames: list[QGraphicsRectItem] = []
        self._press: tuple[int, QPointF] | None = None   # move: page + press point
        self._moved = False
        self._collapse_to: core_annots.AnnotInfo | None = None
        self._resize: QPointF | None = None               # fixed corner while resizing
        self._band: tuple[int, QPointF] | None = None     # rubber band: page + start
        self._band_item: QGraphicsRectItem | None = None
        self._band_base: list[core_annots.AnnotInfo] = []
        self._band_infos: list[core_annots.AnnotInfo] = []
        self._preview: PagePreview | None = None
        self._panel: AnnotStylePanel | None = None
        self.session: FreeTextSession | None = None
        self._nudge_burst = False
        self._own_change = False
        self._nudge_timer = QTimer(self)
        self._nudge_timer.setSingleShot(True)
        self._nudge_timer.setInterval(NUDGE_BURST_MS)
        self._nudge_timer.timeout.connect(self._end_burst)
        self.sel = TextSelection(self)
        self.sel_ui = SelectionUI(self, self.sel)

    # -- selection API ----------------------------------------------------------
    @property
    def selected(self) -> core_annots.AnnotInfo | None:
        """The selected comment when exactly one is selected."""
        return self.selection[0] if len(self.selection) == 1 else None

    @property
    def _frame(self) -> QGraphicsRectItem | None:
        return self._frames[0] if len(self._frames) == 1 else None

    def is_selected(self, info: core_annots.AnnotInfo) -> bool:
        return _key(info) in {_key(a) for a in self.selection}

    def select(self, info: core_annots.AnnotInfo | None, *, emit: bool = True) -> None:
        """Select just ``info`` (``None`` clears the selection)."""
        self.set_selection([info] if info is not None else [], emit=emit)

    def set_selection(self, infos, *, emit: bool = True) -> None:
        """Replace the selection. ``emit`` rebuilds the inspector – not wanted
        while restyling or while a rubber band is being dragged."""
        self._flush_panel()  # a slider/size change still pending belongs to the old selection
        unique: dict[Key, core_annots.AnnotInfo] = {}
        for a in infos:
            if a is not None:
                unique.setdefault(_key(a), a)
        old = [_key(a) for a in self.selection]
        self.selection = list(unique.values())
        self._clear_preview()
        self._build_frames()
        if emit:
            self.changed.emit()
        if emit or old != [_key(a) for a in self.selection]:
            self.selectionChanged.emit()

    def _flush_panel(self) -> None:
        panel = self._panel
        if panel is None:
            return
        try:
            panel._flush()
        except RuntimeError:  # the inspector already deleted it
            self._panel = None

    def toggle(self, info: core_annots.AnnotInfo) -> None:
        if self.is_selected(info):
            self.set_selection([a for a in self.selection if _key(a) != _key(info)])
        else:
            self.set_selection(self.selection + [info])

    def select_all(self) -> bool:
        if self.ctx is None:
            return False
        page = self.ctx.view.current_page
        infos = core_annots.list_annotations(self.ctx.pdf.doc, page)
        if not infos:
            return False
        self.set_selection(infos)
        return True

    # -- frames and handles -----------------------------------------------------
    def _build_frames(self) -> None:
        self._clear_frames()
        if self.ctx is None:
            return
        multi = len(self.selection) > 1
        for info in self.selection:
            r = info.rect
            frame = QGraphicsRectItem(QRectF(r.x0 - FRAME_PAD, r.y0 - FRAME_PAD,
                                             r.width + 2 * FRAME_PAD, r.height + 2 * FRAME_PAD))
            pen = QPen(T.qcolor("accent"), 1.5)
            pen.setCosmetic(True)
            frame.setPen(pen)
            fill = QColor(T.qcolor("accent"))
            fill.setAlpha(14 if multi else 18)
            frame.setBrush(fill)
            frame.setZValue(45)
            try:
                self.ctx.view.add_overlay(info.page, frame)
            except IndexError:
                continue
            self._frames.append(frame)
        if not multi and self._frames and self.selection[0].kind not in FIXED_SIZE_KINDS:
            self._add_handles(self._frames[0])

    def _corners(self) -> list[QPointF]:
        r = self._frame.rect()
        return [r.topLeft(), r.topRight(), r.bottomRight(), r.bottomLeft()]

    def _add_handles(self, frame: QGraphicsRectItem) -> None:
        """Four corner handles, children of the frame (they move with it) and
        transformation-independent (always 9 px on screen)."""
        pen = QPen(T.qcolor("accent"), 1.5)
        pen.setCosmetic(True)
        for c in self._corners():
            h = QGraphicsRectItem(QRectF(-HANDLE_PX / 2, -HANDLE_PX / 2, HANDLE_PX, HANDLE_PX), frame)
            h.setFlag(QGraphicsItem.GraphicsItemFlag.ItemIgnoresTransformations, True)
            h.setPen(pen)
            h.setBrush(T.qcolor("surface_raised"))
            h.setPos(c)
            h.setZValue(1)

    def _sync_handles(self) -> None:
        if self._frame is None:
            return
        for h, c in zip(self._frame.childItems(), self._corners()):
            h.setPos(c)

    def handle_at(self, page: int, pos: QPointF) -> int:
        """Index of the corner handle under ``pos`` (page coords), or -1."""
        frame, sel = self._frame, self.selected
        if frame is None or sel is None or page != sel.page or not frame.childItems():
            return -1
        tol = (HANDLE_PX / 2 + 3) / max(self.ctx.view.zoom, 0.05)
        for i, c in enumerate(self._corners()):
            c = c + frame.pos()
            if abs(pos.x() - c.x()) <= tol and abs(pos.y() - c.y()) <= tol:
                return i
        return -1

    def _clear_frames(self) -> None:
        if self.ctx is not None:
            for f in self._frames:
                self.ctx.view.remove_overlay(f)
        self._frames = []

    def _refresh(self, *, emit: bool = False) -> None:
        """Re-read the selected comments after an edit (xrefs are stable)."""
        if not self.selection or self.ctx is None:
            return
        doc = self.ctx.pdf.doc
        fresh: list[core_annots.AnnotInfo] = []
        by_page: dict[int, dict[int, core_annots.AnnotInfo]] = {}
        for page, xref in [_key(a) for a in self.selection]:
            if page >= doc.page_count:
                continue
            if page not in by_page:
                by_page[page] = {a.xref: a for a in core_annots.list_annotations(doc, page)}
            if xref in by_page[page]:
                fresh.append(by_page[page][xref])
        lost = len(fresh) != len(self.selection)
        self.set_selection(fresh, emit=emit or lost)

    # -- lifecycle -----------------------------------------------------------------
    def activate(self, ctx) -> None:
        super().activate(ctx)
        self.sel_ui.attach()

    def deactivate(self) -> None:
        self.sel.clear()
        self.sel_ui.detach()
        self._flush_panel()
        self.end_session()
        self._clear_preview()
        self._clear_band()
        self._clear_frames()
        had = bool(self.selection)
        self.selection = []
        if had:
            self.selectionChanged.emit()
        super().deactivate()

    def document_changed(self, event) -> None:
        self.sel.invalidate()
        self.sel.clear()
        self.sel_ui.hide_bar()
        if not self._own_change:
            self._end_burst()  # something else changed: the next nudge is a new step
        if self.session is not None:
            if not self.session.committing and event.kind in (RELOADED, STRUCTURE):
                self.session.cancel()
            return
        if not self.selection or self.ctx is None:
            return
        if event.kind == STRUCTURE:  # pages moved / deleted: indices are stale
            self.set_selection([])
            return
        try:
            self._refresh(emit=event.kind == RELOADED)  # undo/redo: values may differ
        except Exception:
            self.set_selection([])

    # -- mouse ---------------------------------------------------------------------
    def mouse_move(self, ev: ToolMouseEvent) -> None:
        if self.sel.dragging:
            self.sel.drag(ev)
            return
        if self._resize is not None and self.selected is not None and self._frame is not None:
            p = ev.pos_on(self.selected.page)
            r = QRectF(self._resize, p).normalized()
            self._frame.setRect(r.adjusted(-FRAME_PAD, -FRAME_PAD, FRAME_PAD, FRAME_PAD))
            self._sync_handles()
            return
        if self._band is not None:
            self._update_band(ev)
            return
        if self._press is not None and self.selection:
            page, start = self._press
            d = ev.pos_on(page) - start
            if d.manhattanLength() >= DRAG_START or self._moved:
                self._moved = True
                d = self._clamp_delta(d.x(), d.y())
                for f in self._frames:
                    f.setPos(QPointF(*d))
            return
        corner = self.handle_at(ev.page, ev.pos)
        if corner >= 0:
            diag = Qt.CursorShape.SizeFDiagCursor if corner in (0, 2) else Qt.CursorShape.SizeBDiagCursor
            self.ctx.view.set_tool_cursor(diag)
            return
        hit = ev.page >= 0 and core_annots.annotation_at(self.ctx.pdf.doc, ev.page, (ev.pos.x(), ev.pos.y()))
        if hit:
            cursor = Qt.CursorShape.SizeAllCursor
        elif self.sel.over_text(ev.page, ev.pos):
            cursor = Qt.CursorShape.IBeamCursor
        else:
            cursor = Qt.CursorShape.ArrowCursor
        self.ctx.view.set_tool_cursor(cursor)

    def mouse_press(self, ev: ToolMouseEvent) -> None:
        if ev.button != Qt.MouseButton.LeftButton:
            return
        if self.session is not None:  # a click outside the text box finishes it
            self.end_session()
        self.sel_ui.hide_bar()
        self._collapse_to = None
        corner = self.handle_at(ev.page, ev.pos)
        if corner >= 0:  # resize: the opposite corner of the annotation stays put
            r = self.selected.rect
            pts = [QPointF(r.x0, r.y0), QPointF(r.x1, r.y0), QPointF(r.x1, r.y1), QPointF(r.x0, r.y1)]
            self._resize = pts[(corner + 2) % 4]
            return
        info = core_annots.annotation_at(self.ctx.pdf.doc, ev.page, (ev.pos.x(), ev.pos.y())) \
            if ev.page >= 0 else None
        additive = bool(ev.modifiers & (Qt.KeyboardModifier.ShiftModifier | Qt.KeyboardModifier.ControlModifier))
        self._moved = False
        if info is None:
            if not additive and self.selection:
                self.select(None)
            self._press = None
            if (not additive or self.sel.has_selection()) and self.sel.press(ev, on_text=True):
                return  # selecting text (Shift extends a text selection)
            self.sel.clear()
            if ev.page >= 0:
                self._start_band(ev, additive)
            return
        self.sel.clear()
        if additive:
            self.toggle(info)
        elif not self.is_selected(info):
            self.select(info)
        elif len(self.selection) > 1:
            self._collapse_to = info  # a click without dragging selects just this one
        self._press = (ev.page, QPointF(ev.pos)) if self.is_selected(info) else None

    def mouse_release(self, ev: ToolMouseEvent) -> None:
        if self.sel.dragging:
            self.sel.release(ev)
            if self.sel.has_selection():
                self.sel_ui.show_bar()
            return
        if self._resize is not None and self.selected is not None:
            anchor, self._resize = self._resize, None
            r = QRectF(anchor, ev.pos_on(self.selected.page)).normalized()
            if r.width() < MIN_SIZE or r.height() < MIN_SIZE:
                self._refresh()
                return
            core_annots.update_annotation(self.ctx.pdf, self.selected.page, self.selected.xref,
                                          rect=(r.left(), r.top(), r.right(), r.bottom()))
            return
        if self._band is not None:
            self._finish_band()
            return
        press, moved, collapse = self._press, self._moved, self._collapse_to
        self._press, self._moved, self._collapse_to = None, False, None
        if press is None or not self.selection:
            return
        if not moved:
            if collapse is not None:
                self.select(collapse)
            return
        page, start = press
        d = ev.pos_on(page) - start
        dx, dy = self._clamp_delta(d.x(), d.y())
        for f in self._frames:
            f.setPos(0, 0)
        self.move_selection(dx, dy)

    def mouse_double_click(self, ev: ToolMouseEvent) -> None:
        info = core_annots.annotation_at(self.ctx.pdf.doc, ev.page, (ev.pos.x(), ev.pos.y())) \
            if ev.page >= 0 else None
        if info is not None:
            self.edit(info)
        elif ev.page >= 0:
            self.sel.press(ev, on_text=True)  # a word

    # -- rubber band -------------------------------------------------------------------
    def _start_band(self, ev: ToolMouseEvent, additive: bool) -> None:
        self._band = (ev.page, QPointF(ev.pos))
        self._band_base = list(self.selection) if additive else []
        self._band_infos = core_annots.list_annotations(self.ctx.pdf.doc, ev.page)

    def _update_band(self, ev: ToolMouseEvent) -> None:
        page, start = self._band
        end = ev.pos_on(page)
        if self._band_item is None:
            if (end - start).manhattanLength() < DRAG_START:
                return
            self._band_item = QGraphicsRectItem()
            pen = QPen(T.qcolor("accent"), 1.0, Qt.PenStyle.DashLine)
            pen.setCosmetic(True)
            self._band_item.setPen(pen)
            fill = QColor(T.qcolor("accent"))
            fill.setAlpha(22)
            self._band_item.setBrush(fill)
            self._band_item.setZValue(50)
            self.ctx.view.add_overlay(page, self._band_item)
        r = QRectF(start, end).normalized()
        self._band_item.setRect(r)
        band = pymupdf.Rect(r.left(), r.top(), r.right(), r.bottom())
        hits = [a for a in self._band_infos
                if a.rect.x0 <= band.x1 and a.rect.x1 >= band.x0 and a.rect.y0 <= band.y1 and a.rect.y1 >= band.y0]
        self.set_selection(self._band_base + hits, emit=False)

    def _finish_band(self) -> None:
        dragged = self._band_item is not None
        self._clear_band()
        if dragged:
            self.changed.emit()  # one inspector rebuild for the final selection
            self.selectionChanged.emit()

    def _clear_band(self) -> None:
        if self._band_item is not None and self.ctx is not None:
            self.ctx.view.remove_overlay(self._band_item)
        self._band_item = None
        self._band = None
        self._band_infos = []

    # -- moving --------------------------------------------------------------------------
    def _clamp_delta(self, dx: float, dy: float) -> tuple[float, float]:
        """Limit a move so the selection stays on its page(s); a selection
        already sticking out may only move back in."""
        doc = self.ctx.pdf.doc
        lo_x = lo_y = -math.inf
        hi_x = hi_y = math.inf
        for page in {a.page for a in self.selection}:
            pr = doc[page].rect
            rects = [a.rect for a in self.selection if a.page == page]
            x0, y0 = min(r.x0 for r in rects), min(r.y0 for r in rects)
            x1, y1 = max(r.x1 for r in rects), max(r.y1 for r in rects)
            lo_x, hi_x = max(lo_x, min(0.0, pr.x0 - x0)), min(hi_x, max(0.0, pr.x1 - x1))
            lo_y, hi_y = max(lo_y, min(0.0, pr.y0 - y0)), min(hi_y, max(0.0, pr.y1 - y1))
        return min(max(dx, lo_x), hi_x), min(max(dy, lo_y), hi_y)

    def move_selection(self, dx: float, dy: float, *, merge: bool = False) -> None:
        if not self.selection or (not dx and not dy):
            return
        self._clear_preview()
        self._own_change = True
        try:
            core_annots.move_annotations(self.ctx.pdf, [_key(a) for a in self.selection], dx, dy, merge=merge)
        finally:
            self._own_change = False

    def nudge(self, dx: float, dy: float) -> None:
        """Arrow keys: move by (dx, dy) pt; a burst is one undo step."""
        dx, dy = self._clamp_delta(dx, dy)
        if not dx and not dy:
            return
        self.move_selection(dx, dy, merge=self._nudge_burst)
        self._nudge_burst = True
        self._nudge_timer.start()

    def _end_burst(self) -> None:
        self._nudge_burst = False

    # -- text selection ----------------------------------------------------------------
    def has_selection(self) -> bool:
        return self.sel.has_selection()

    def copy(self) -> bool:
        return self.sel_ui.copy()

    def run_selection_action(self, key: str) -> bool:
        return self.sel_ui.run(key)

    def context_menu(self, ev: ToolMouseEvent, global_pos) -> bool:
        return self.sel_ui.context_menu(ev, global_pos)

    # -- keys ------------------------------------------------------------------------
    def key_press(self, ev: QKeyEvent) -> bool:
        if ev.key() == Qt.Key.Key_Escape and self.sel.has_selection():
            self.sel.clear()
            self.sel_ui.hide_bar()
            return True
        if not self.selection:
            return False
        key = ev.key()
        if key in (Qt.Key.Key_Delete, Qt.Key.Key_Backspace):
            self.delete_selected()
            return True
        if key == Qt.Key.Key_Escape:
            self.select(None)
            return True
        if key in _ARROWS:
            step = NUDGE_BIG if ev.modifiers() & Qt.KeyboardModifier.ShiftModifier else NUDGE
            ux, uy = _ARROWS[key]
            self.nudge(ux * step, uy * step)
            return True
        if key in (Qt.Key.Key_Return, Qt.Key.Key_Enter) and self.selected is not None:
            self.edit(self.selected)
            return True
        return False

    def delete_selected(self) -> None:
        if not self.selection:
            return
        refs = [_key(a) for a in self.selection]
        from ...panels.comments import confirm_delete
        from ....core import comments as core_comments

        threads = [c for p, x in refs if (c := core_comments.comment(self.ctx.pdf.doc, p, x)) is not None]
        if not confirm_delete(self.ctx.view.window(), threads):
            return
        self.select(None)
        core_annots.delete_annotations(self.ctx.pdf, refs)

    # -- editing text ------------------------------------------------------------------------
    def edit(self, info: core_annots.AnnotInfo) -> None:
        """Edit a note's text (dialog) or a text box (in place)."""
        if info.kind == "note":
            text = ask_note(self.ctx.view.window(), info.contents, editing=True)
            if text is not None and text != info.contents:
                core_annots.update_annotation(self.ctx.pdf, info.page, info.xref, contents=text)
        elif info.kind in TEXT_KINDS:
            self.edit_free_text(info)

    def edit_free_text(self, info: core_annots.AnnotInfo) -> None:
        self.end_session()
        self.select(info)
        self._clear_frames()  # the editing frame replaces the selection frame
        auto = is_auto_width(info)
        r = info.rect
        self.session = FreeTextSession(self, info.page, QPointF(r.x0, r.y0), style_of(info), text=info.contents,
                                       width=None if auto else r.width, min_height=0.0 if auto else r.height,
                                       xref=info.xref)
        self.session.finished.connect(self._session_finished)

    def _session_finished(self) -> None:
        self.session = None
        try:
            self._refresh(emit=True)
        except Exception:
            self.set_selection([])

    def end_session(self) -> None:
        if self.session is not None:
            self.session.commit()
        self.session = None

    # -- inspector --------------------------------------------------------------------------
    def properties_widget(self) -> QWidget:
        sel = self.selection
        if not sel:
            self._panel = None
            w = QWidget()
            v = QVBoxLayout(w)
            v.setContentsMargins(16, 4, 16, 16)
            card = QFrame()
            card.setObjectName("Card")
            cl = QVBoxLayout(card)
            cl.setContentsMargins(14, 12, 14, 12)
            cl.addWidget(label(tr("annot.select.none"), "heading"))
            cl.addWidget(label(tr("annot.select.none.hint"), "muted", wrap=True))
            v.addWidget(card)
            v.addStretch(1)
            w.setProperty("transientPanel", True)  # rebuilt per selection: the inspector deletes it
            return w
        first = sel[0]
        width_kinds = [a for a in sel if a.kind in WIDTH_KINDS]
        text_kinds = [a for a in sel if a.kind in TEXT_KINDS]
        fill_kinds = [a for a in sel if a.kind in FILL_KINDS]
        style = AnnotStyle(tuple(first.color or (0, 0, 0)), float(first.opacity or 1.0),
                           float((width_kinds[0].width if width_kinds else first.width) or 1.0),
                           tuple(first.fill) if first.fill else None)
        if text_kinds:
            ts = style_of(text_kinds[0])
            style.fontsize, style.font = ts.fontsize, ts.font
        mixed: set[str] = set()
        if any(not same_color(a.color, first.color) for a in sel):
            mixed.add("color")
        if any(abs(a.opacity - first.opacity) > 0.005 for a in sel):
            mixed.add("opacity")
        if width_kinds and any(abs(a.width - width_kinds[0].width) > 0.01 for a in width_kinds):
            mixed.add("width")
        if fill_kinds:
            style.fill = tuple(fill_kinds[0].fill) if fill_kinds[0].fill else None
            if len({a.fill is None for a in fill_kinds}) > 1:
                mixed.add("fill")
        if len(text_kinds) > 1 and len({(style_of(a).fontsize, style_of(a).font) for a in text_kinds}) > 1:
            mixed.add("fontsize")
        if len(sel) == 1:
            title, hint = tr(f"annot.kind.{first.kind}"), tr(
                "annot.select.text.hint" if first.kind in TEXT_KINDS else "annot.select.selected.hint")
        else:
            title = tr("annot.select.many", n=len(sel))
            hint = tr("annot.select.many.hint")
        panel = AnnotStylePanel(title, hint, style, width=bool(width_kinds), fill=bool(fill_kinds),
                                text=bool(text_kinds), mixed=mixed)
        panel.propertyChanged.connect(self._apply_property)
        panel.previewRequested.connect(self._show_preview)
        panel.setProperty("transientPanel", True)
        self._panel = panel
        return panel

    def _props_for(self, a: core_annots.AnnotInfo, name: str, value) -> dict:
        """The update one selected comment gets for an inspector change."""
        if name == "color":
            props = {"color": value}
            if a.kind in FILL_KINDS and a.fill is not None:
                props["fill"] = value  # filled shapes keep a matching fill
            return props
        if name == "fill":
            return {"fill": (a.color or value) if value is not None else None} if a.kind in FILL_KINDS else {}
        if name == "width":
            return {"width": value} if a.kind in WIDTH_KINDS else {}
        if name in ("fontsize", "font"):
            return {name: value} if a.kind in TEXT_KINDS else {}
        if name == "opacity":
            return {"opacity": value}
        return {}

    def _apply_property(self, name: str, value) -> None:
        self._clear_preview()
        if self.session is not None:  # editing a text box: style the editor, saved on commit
            if hasattr(self.session.style, name) and name != "fill":
                self.session.set_style(replace(self.session.style, **{name: value}))
            return
        changes = [(a.page, a.xref, p) for a in self.selection if (p := self._props_for(a, name, value))]
        if changes:
            core_annots.update_annotations(self.ctx.pdf, changes)

    # -- previews -------------------------------------------------------------------------
    def _show_preview(self, props: dict | None) -> None:
        """Show ``props`` on the selection without writing them (hovered
        swatch, dragged slider); ``None`` restores the real rendering."""
        if self.ctx is None:
            return
        if self.session is not None:
            self.session.preview(props)
            return
        if self._preview is None:
            self._preview = PagePreview(self.ctx.view)
        self._preview.clear()
        if not props:
            return
        pages: dict[int, tuple[dict, pymupdf.Rect]] = {}
        for a in self.selection:
            p: dict = {}
            for name, value in props.items():
                p.update(self._props_for(a, name, value))
            if not p:
                continue
            pad = max(a.width, float(p.get("width", 0.0))) + 4
            changes, clip = pages.get(a.page, ({}, None))
            changes[a.xref] = p
            r = pymupdf.Rect(a.rect) + (-pad, -pad, pad, pad)
            pages[a.page] = (changes, r if clip is None else clip | r)
        for page, (changes, clip) in pages.items():
            self._preview.add(self.ctx.pdf.doc, page, changes, clip)

    def _clear_preview(self) -> None:
        if self._preview is not None:
            self._preview.clear()
