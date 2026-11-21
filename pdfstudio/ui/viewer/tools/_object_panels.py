"""Inspector panels of "Markér objekt" (WP-D2), one per selection kind:

* :class:`TextObjectPanel` – one text block: X/Y, the wrap width B (O9),
  "Redigér tekst", Arrangér, Slet;
* :class:`GraphicObjectPanel` – one graphic: X/Y/B/H with an aspect lock
  (the stroke width stays, O8), stroke/fill colour incl. "Ingen" and the
  stroke width (O11), Arrangér (O12), "Medtag indhold" for boxes (O4), Slet;
* :class:`MultiObjectPanel` – several objects: X/Y, B/H when the selection
  can be scaled (no text), the style when every member is a graphic,
  "Medtag indhold" when it is a box with its contents, Arrangér, Slet;
* :class:`ImageArrangePanel` – :class:`ImageObjectPanel` unchanged plus the
  Arrangér row (also shown, without its edit part, for "nothing selected").

Fields follow the selection and the document (undo/redo) live, commit on
Enter / focus out / arrow steps (never per keystroke), leave a field alone
while the user types in it and never write a value that did not change.
They are registered in :attr:`EditObjectTool.PANELS` at the end of this module.
"""

from __future__ import annotations

from PySide6.QtCore import QLocale, QPointF, QRectF, Qt
from PySide6.QtGui import QPainter, QPainterPath, QPen
from PySide6.QtWidgets import (
    QCheckBox,
    QDoubleSpinBox,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from ....core import pageobjects as po
from ....core.pageobjects import PageObject
from ....i18n import tr
from ...theme import tokens as T
from ...theme.icons import icon
from ...widgets.color_button import ColorButton
from ...widgets.util import IconButton, label, on_theme_change, tooltip_with_shortcut
from .edit_object import (
    MM_PER_PT,
    EditObjectTool,
    ImageObjectPanel,
    ObjectPanel,
    _row_button,
    _spin,
    _union,
    count_text,
)

SHORTCUT_FRONT = "Ctrl+Shift+]"
SHORTCUT_BACK = "Ctrl+Shift+["
MM_TOL = 0.051          # a field differs from the object when it is off by more than this (mm, 1 decimal shown)
PT_TOL = 0.051          # the same for the stroke width (pt)
DEFAULT_STROKE = (0.0, 0.0, 0.0)
DEFAULT_FILL = (1.0, 1.0, 1.0)


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------
def _num(v: float) -> str:
    """``2`` / ``0,5`` in the UI locale (no trailing zeros)."""
    loc = QLocale()
    return loc.toString(float(v), "f", 2).rstrip("0").rstrip(loc.decimalPoint())


def _typing(spin: QDoubleSpinBox) -> bool:
    """The user is typing in ``spin`` (focus and text not committed yet)."""
    return spin.hasFocus() and spin.cleanText() != spin.textFromValue(spin.value())


def _load(spin: QDoubleSpinBox, value: float, tol: float = MM_TOL) -> None:
    """Show ``value`` unless the field already shows it or the user types in it."""
    if _typing(spin):
        return
    if abs(spin.value() - value) > tol / 2 or spin.text() == "":
        spin.setValue(value)


def _from_field(spin: QDoubleSpinBox, actual_pt: float) -> float:
    """The field's value in pt – or the object's exact value when the field
    still shows it (the field is rounded to 0.1 mm: no drift on the other axis)."""
    if abs(spin.value() - actual_pt * MM_PER_PT) <= MM_TOL:
        return actual_pt
    return spin.value() / MM_PER_PT


def _graphics(sel: list[PageObject]) -> list[PageObject]:
    return [o for o in sel if o.kind == "graphic"]


def _box_contents(tool: EditObjectTool, obj: PageObject | None) -> list[PageObject]:
    """What the box graphic ``obj`` encloses (O4), [] for anything else."""
    if obj is None or obj.kind != "graphic" or tool.ctx is None:
        return []
    try:
        return list(po.contents_of(tool.ctx.pdf.doc, obj.page, obj))
    except Exception:
        return []


class StyleSwatch(ColorButton):
    """:class:`ColorButton` that can also show "no colour" (a slashed swatch)."""

    def __init__(self, title: str) -> None:
        super().__init__(DEFAULT_STROKE, title=title)
        self._none = False

    def is_none(self) -> bool:
        return self._none

    def set_none(self, on: bool) -> None:
        if on != self._none:
            self._none = on
            self.update()

    def paintEvent(self, ev) -> None:  # noqa: N802
        if not self._none:
            super().paintEvent(ev)
            return
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        r = QRectF(self.rect()).adjusted(1.5, 1.5, -1.5, -1.5)
        frame = QPainterPath()
        frame.addRoundedRect(r, 7, 7)
        p.fillPath(frame, T.qcolor("hover" if self.underMouse() else "surface_raised"))
        p.setPen(QPen(T.qcolor("border_strong"), 1))
        p.drawPath(frame)
        sw = r.adjusted(5, 5, -5, -5)
        path = QPainterPath()
        path.addRoundedRect(sw, 4, 4)
        p.fillPath(path, T.qcolor("page"))
        p.setPen(QPen(T.qcolor("border_strong"), 1))
        p.drawPath(path)
        p.setClipPath(path)
        p.setPen(QPen(T.qcolor("danger"), 1.6))
        p.drawLine(sw.bottomLeft() + QPointF(1, -1), sw.topRight() + QPointF(-1, 1))
        p.end()


# ---------------------------------------------------------------------------
# shared panel skeleton
# ---------------------------------------------------------------------------
class _PanelBase(QWidget):
    """Card (title / detail / meta) + an edit box with sections, as the image panel."""

    def __init__(self, tool: EditObjectTool) -> None:
        super().__init__()
        self.tool = tool
        self._loading = False
        self._buttons: list[tuple[QToolButton, str, str]] = []
        v = QVBoxLayout(self)
        v.setContentsMargins(16, 4, 16, 16)
        v.setSpacing(10)
        self._outer = v

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
        self.body = QVBoxLayout(self.edit_box)
        self.body.setContentsMargins(0, 4, 0, 0)
        self.body.setSpacing(8)
        v.addWidget(self.edit_box)

    def _finish(self, tips: str) -> None:
        self._outer.addSpacing(2)
        self._outer.addWidget(label(tips, "caption", wrap=True))
        self._outer.addStretch(1)
        on_theme_change(self, self._refresh_icons)

    def _refresh_icons(self) -> None:
        for b, name, role in self._buttons:
            b.setIcon(icon(name, role))
        self.refresh()

    def refresh(self) -> None:  # pragma: no cover - overridden
        pass

    # -- building blocks -----------------------------------------------------------
    def _section(self, key: str) -> QWidget:
        w = label(tr(key), "section")
        self.body.addWidget(w)
        return w

    def _position_grid(self, *, size: bool) -> QGridLayout:
        """X / Y (and B / H with the aspect lock) – the image panel's layout."""
        grid = QGridLayout()
        grid.setHorizontalSpacing(6)
        grid.setVerticalSpacing(8)
        self.x = _spin(tr("objects.field.x.tip"), -2000, 5000)
        self.y = _spin(tr("objects.field.y.tip"), -2000, 5000)
        for col, (key, spin) in enumerate((("x", self.x), ("y", self.y))):
            grid.addWidget(label(tr(f"objects.field.{key}"), "caption"), 0, col * 3)
            grid.addWidget(spin, 0, col * 3 + 1)
        self.x.valueChanged.connect(lambda _v: self._move_changed())
        self.y.valueChanged.connect(lambda _v: self._move_changed())
        if size:
            self.w = _spin(tr("objects.field.w.tip"), 0.5, 5000)
            self.h = _spin(tr("objects.field.h.tip"), 0.5, 5000)
            self.w_label = label(tr("objects.field.w"), "caption")
            self.h_label = label(tr("objects.field.h"), "caption")
            grid.addWidget(self.w_label, 1, 0)
            grid.addWidget(self.w, 1, 1)
            grid.addWidget(self.h_label, 1, 3)
            grid.addWidget(self.h, 1, 4)
        grid.setColumnStretch(1, 1)
        grid.setColumnStretch(4, 1)
        grid.setColumnMinimumWidth(2, 26)
        self.body.addLayout(grid)
        return grid

    def _add_lock(self, grid: QGridLayout) -> None:
        self.lock = IconButton("link-simple", tr("objects.lock.tip"), variant="icon-sm", checkable=True)
        self.lock.setChecked(True)
        self.lock.toggled.connect(lambda on: self.lock.set_icon_name("link-simple" if on else "link-simple-break"))
        grid.addWidget(self.lock, 1, 2)
        self.w.valueChanged.connect(lambda _v: self._size_changed(self.w))
        self.h.valueChanged.connect(lambda _v: self._size_changed(self.h))

    def _arrange_rows(self) -> None:
        self._section("objects.section.arrange")
        row = QHBoxLayout()
        row.setSpacing(4)
        self.front_btn = _row_button(self, row, "selection-foreground", tr("objects.action.front"),
                                     lambda: self.tool.restack(True),
                                     tooltip_with_shortcut(tr("objects.action.front.tip"), SHORTCUT_FRONT))
        self.back_btn = _row_button(self, row, "selection-background", tr("objects.action.back"),
                                    lambda: self.tool.restack(False),
                                    tooltip_with_shortcut(tr("objects.action.back.tip"), SHORTCUT_BACK))
        self.body.addLayout(row)

    def _delete_row(self) -> None:
        self.delete_btn = _row_button(self, self.body, "trash", tr("objects.action.delete_sel"),
                                      self.tool.delete_selected, tooltip_with_shortcut(
                                          tr("objects.action.delete_sel.tip"), "Del"), role="danger")

    def _include_box(self) -> None:
        """"Medtag indhold" (O4) – first in the edit box: it decides what is selected."""
        self.include = QCheckBox(tr("objects.include"))
        self.include.setToolTip(tr("objects.include.tip"))
        self.include.toggled.connect(self._include_toggled)
        self.include_row = QWidget()
        row = QVBoxLayout(self.include_row)
        row.setContentsMargins(0, 0, 0, 6)
        row.addWidget(self.include)
        self.body.insertWidget(0, self.include_row)

    # -- loading -----------------------------------------------------------------------------
    def _load_position(self, r) -> None:
        _load(self.x, r.x0 * MM_PER_PT)
        _load(self.y, r.y0 * MM_PER_PT)

    def _load_size(self, r) -> None:
        _load(self.w, r.width * MM_PER_PT)
        _load(self.h, r.height * MM_PER_PT)

    def _load_include(self) -> bool:
        """Show "Medtag indhold" when the primary object is a box enclosing
        something; checked = the tool's setting."""
        prim = self.tool.primary
        show = bool(_box_contents(self.tool, prim))
        self.include_row.setVisible(show)
        self.include.setChecked(self.tool.include_contents)
        return show

    # -- committing ----------------------------------------------------------------------------
    def _move_changed(self) -> None:
        sel = self.tool.selection()
        if self._loading or not sel:
            return
        r = _union(sel)
        x, y = _from_field(self.x, r.x0), _from_field(self.y, r.y0)
        if abs(x - r.x0) < 1e-3 and abs(y - r.y0) < 1e-3:
            return
        self.tool.move_selection_to(x, y)

    def _size_changed(self, spin: QDoubleSpinBox) -> None:
        sel = self.tool.selection()
        if self._loading or not sel:
            return
        r = _union(sel)
        x, y = _from_field(self.x, r.x0), _from_field(self.y, r.y0)
        w, h = _from_field(self.w, r.width), _from_field(self.h, r.height)
        if abs(w - r.width) < 1e-3 and abs(h - r.height) < 1e-3:
            return
        if self.lock.isChecked() and r.width > 0 and r.height > 0:
            aspect = r.width / r.height
            if spin is self.w:
                h = w / aspect
            else:
                w = h * aspect
        self.tool.set_selection_rect(QRectF(x, y, w, h))

    def _include_toggled(self, on: bool) -> None:
        if self._loading:
            return
        tool = self.tool
        prim = tool.primary
        tool.set_include_contents(on)
        if prim is None or prim.kind != "graphic":
            return
        # apply it to the box at hand: with or without what it encloses
        if on:
            tool.select([prim] + _box_contents(tool, prim))
        else:
            tool.select(prim)


# ---------------------------------------------------------------------------
# style controls (stroke / fill / width)
# ---------------------------------------------------------------------------
class _StyleMixin:
    """Streg / Fyld swatches with "Ingen" toggles and the stroke width."""

    def _style_section(self) -> None:
        self.style_title = self._section("objects.section.style")
        grid = QGridLayout()
        grid.setHorizontalSpacing(6)
        grid.setVerticalSpacing(8)
        self.stroke_btn = StyleSwatch(tr("objects.stroke.title"))
        self.stroke_btn.setToolTip(tr("objects.stroke.tip"))
        self.stroke_none = IconButton("prohibit", tr("objects.stroke.none.tip"), variant="icon-sm", checkable=True)
        self.fill_btn = StyleSwatch(tr("objects.fill.title"))
        self.fill_btn.setToolTip(tr("objects.fill.tip"))
        self.fill_none = IconButton("prohibit", tr("objects.fill.none.tip"), variant="icon-sm", checkable=True)
        self.width_spin = QDoubleSpinBox()
        self.width_spin.setRange(0.1, 72.0)
        self.width_spin.setDecimals(1)
        self.width_spin.setSingleStep(0.5)
        self.width_spin.setSuffix(tr("objects.unit.pt"))
        self.width_spin.setKeyboardTracking(False)
        self.width_spin.setButtonSymbols(QDoubleSpinBox.ButtonSymbols.NoButtons)
        self.width_spin.setAlignment(Qt.AlignmentFlag.AlignRight)
        self.width_spin.setToolTip(tr("objects.width.tip"))
        self.width_spin.setMinimumWidth(78)
        self.stroke_label = label(tr("objects.stroke"), "caption")
        self.fill_label = label(tr("objects.fill"), "caption")
        self.width_label = label(tr("objects.width"), "caption")
        grid.addWidget(self.stroke_label, 0, 0)
        grid.addWidget(self.stroke_btn, 0, 1)
        grid.addWidget(self.stroke_none, 0, 2)
        grid.addWidget(self.fill_label, 0, 4)
        grid.addWidget(self.fill_btn, 0, 5)
        grid.addWidget(self.fill_none, 0, 6)
        grid.addWidget(self.width_label, 1, 0, 1, 4)
        grid.addWidget(self.width_spin, 1, 4, 1, 4)
        grid.setColumnStretch(3, 1)
        grid.setColumnStretch(7, 1)
        self.body.addLayout(grid)
        self._style_grid = grid
        self._last_stroke = DEFAULT_STROKE
        self._last_fill = DEFAULT_FILL
        self.stroke_btn.colorChanged.connect(lambda c: self._color_picked("stroke", c))
        self.fill_btn.colorChanged.connect(lambda c: self._color_picked("fill", c))
        self.stroke_none.toggled.connect(lambda on: self._none_toggled("stroke", on))
        self.fill_none.toggled.connect(lambda on: self._none_toggled("fill", on))
        self.width_spin.valueChanged.connect(self._width_changed)

    def _set_style_visible(self, on: bool, *, fill: bool = True) -> None:
        for w in (self.style_title, self.stroke_label, self.stroke_btn, self.stroke_none,
                  self.width_label, self.width_spin):
            w.setVisible(on)
        for w in (self.fill_label, self.fill_btn, self.fill_none):
            w.setVisible(on and fill)

    def _load_style(self, g) -> None:
        """Show the style of the graphic ``g`` (a GraphicObject)."""
        stroke, fill = getattr(g, "stroke", None), getattr(g, "fill", None)
        if stroke is not None:
            self._last_stroke = tuple(stroke)
            self.stroke_btn.set_color(tuple(stroke))
        if fill is not None:
            self._last_fill = tuple(fill)
            self.fill_btn.set_color(tuple(fill))
        self.stroke_btn.set_none(stroke is None)
        self.fill_btn.set_none(fill is None)
        self.stroke_none.setChecked(stroke is None)
        self.fill_none.setChecked(fill is None)
        self.width_spin.setEnabled(stroke is not None)
        width = float(getattr(g, "width", 0.0) or 0.0)
        if stroke is not None and width > 0:
            _load(self.width_spin, width, PT_TOL)

    def _style_targets(self) -> list[PageObject]:
        return _graphics(self.tool.selection())

    def _would_be_empty(self, removing: str) -> bool:
        """Removing ``removing`` ("stroke"/"fill") leaves a graphic with neither."""
        other = "fill" if removing == "stroke" else "stroke"
        return any(getattr(o.ref, other, None) is None for o in self._style_targets())

    def _color_picked(self, which: str, color: tuple) -> None:
        if self._loading or not self._style_targets():
            return
        cur = [getattr(o.ref, which, None) for o in self._style_targets()]
        if all(c is not None and all(abs(a - b) < 0.002 for a, b in zip(c, color)) for c in cur):
            self._sync_none(which)
            return
        self.tool.set_style(**{which: tuple(color)})

    def _none_toggled(self, which: str, on: bool) -> None:
        if self._loading or not self._style_targets():
            return
        if on:
            if all(getattr(o.ref, which, None) is None for o in self._style_targets()):
                return
            if self._would_be_empty(which):
                self.tool.ctx.toast(tr("objects.style.need_one"), "warning")
                self._sync_none(which)
                return
            self.tool.set_style(**{which: None})
        else:
            if all(getattr(o.ref, which, None) is not None for o in self._style_targets()):
                return
            color = self._last_stroke if which == "stroke" else self._last_fill
            self.tool.set_style(**{which: tuple(color)})

    def _sync_none(self, which: str) -> None:
        """Put a "Ingen" toggle back to what the primary graphic has."""
        prim = next(iter(self._style_targets()), None)
        has = prim is not None and getattr(prim.ref, which, None) is not None
        btn, sw = (self.stroke_none, self.stroke_btn) if which == "stroke" else (self.fill_none, self.fill_btn)
        self._loading = True
        try:
            btn.setChecked(not has)
            sw.set_none(not has)
            if has:
                sw.set_color(tuple(getattr(prim.ref, which)))
        finally:
            self._loading = False

    def _width_changed(self, value: float) -> None:
        targets = [o for o in self._style_targets() if getattr(o.ref, "stroke", None) is not None]
        if self._loading or not targets:
            return
        if all(abs(float(o.ref.width) - value) <= PT_TOL for o in targets):
            return
        self.tool.set_style(width=float(value))


# ---------------------------------------------------------------------------
# the panels
# ---------------------------------------------------------------------------
class TextObjectPanel(_PanelBase):
    """One text block: where it is, how wide it wraps (O9), edit / arrange / delete."""

    def __init__(self, tool: EditObjectTool) -> None:
        super().__init__(tool)
        self._section("objects.section.position")
        self._position_grid(size=True)
        self.w.setToolTip(tr("objects.field.text_w.tip"))
        self.h.setEnabled(False)
        self.h.setToolTip(tr("objects.field.text_h.tip"))
        self.w.valueChanged.connect(lambda _v: self._width_changed())
        self.body.addSpacing(6)
        self._section("objects.section.text")
        self.edit_btn = _row_button(self, self.body, "pencil-simple", tr("objects.action.edit_text"),
                                    self._edit_text, tr("objects.action.edit_text.tip"))
        self.body.addSpacing(6)
        self._arrange_rows()
        self._delete_row()
        self._finish(tr("objects.tips.text"))
        self.refresh()

    def _text(self) -> PageObject | None:
        sel = self.tool.selection()
        return sel[0] if len(sel) == 1 and sel[0].kind == "text" else None

    def refresh(self) -> None:
        t = self._text()
        self.edit_box.setVisible(t is not None)
        self.title.setText(tr("objects.kind.text"))
        if t is None:
            self.detail.setText("")
            self.meta.setVisible(False)
            return
        self.detail.setText(ObjectPanel._detail([t]))
        meta = ObjectPanel._meta([t])
        self.meta.setText(meta)
        self.meta.setVisible(bool(meta))
        can_width = self.tool._width_text() is not None
        self.w.setEnabled(can_width)
        self._loading = True
        try:
            self._load_position(t.rect)
            self._load_size(t.rect)
        finally:
            self._loading = False

    def _width_changed(self) -> None:
        t = self._text()
        if self._loading or t is None:
            return
        w = _from_field(self.w, t.rect.width)
        if abs(w - t.rect.width) < 0.25:
            return
        self.tool.set_text_width(w)

    def _edit_text(self) -> None:
        t = self._text()
        if t is None:
            return
        lines = getattr(t.ref, "lines", None) or []
        if lines:
            b = lines[0].bbox
            pos = QPointF(b.x0 + min(1.0, (b.x1 - b.x0) / 2), (b.y0 + b.y1) / 2)
        else:
            pos = QPointF((t.rect.x0 + t.rect.x1) / 2, (t.rect.y0 + t.rect.y1) / 2)
        self.tool.edit_text_at(t.page, pos)


class GraphicObjectPanel(_StyleMixin, _PanelBase):
    """One graphic: position and size (stroke kept, O8), look (O11), arrange (O12), box contents (O4)."""

    def __init__(self, tool: EditObjectTool) -> None:
        super().__init__(tool)
        self._kind = None
        self._section("objects.section.position")
        grid = self._position_grid(size=True)
        self._add_lock(grid)
        self.body.addSpacing(6)
        self._style_section()
        self.body.addSpacing(6)
        self._arrange_rows()
        self._include_box()
        self._delete_row()
        self._finish(tr("objects.tips.graphic"))
        self.refresh()

    def _graphic(self) -> PageObject | None:
        sel = self.tool.selection()
        return sel[0] if len(sel) == 1 and sel[0].kind == "graphic" else None

    def refresh(self) -> None:
        o = self._graphic()
        self.edit_box.setVisible(o is not None)
        if o is None:
            self.title.setText(tr("objects.kind.graphic"))
            self.detail.setText("")
            self.meta.setVisible(False)
            return
        g = o.ref
        kind = getattr(g, "kind", "shape")
        self.title.setText(tr(f"objects.title.graphic.{kind}"))
        self.detail.setText(_style_text(g))
        note = tr("objects.in_form") if getattr(g, "in_form", False) else ""
        self.meta.setText(note)
        self.meta.setVisible(bool(note))
        self._loading = True
        try:
            if kind != self._kind:
                # a line has no meaningful aspect ratio (its height is the stroke)
                self.lock.setChecked(kind != "line")
                self._kind = kind
            self._load_position(o.rect)
            self._load_size(o.rect)
            self._set_style_visible(True, fill=kind != "line" or g.fill is not None)
            self._load_style(g)
            self._load_include()
        finally:
            self._loading = False


class MultiObjectPanel(_StyleMixin, _PanelBase):
    """Several objects: position, size when scalable, the style of all-graphic selections."""

    def __init__(self, tool: EditObjectTool) -> None:
        super().__init__(tool)
        self._section("objects.section.position")
        grid = self._position_grid(size=True)
        self._add_lock(grid)
        self.size_note = label("", "caption")
        self.body.addWidget(self.size_note)
        self.style_gap = QWidget()
        self.style_gap.setFixedHeight(6)
        self.body.addWidget(self.style_gap)
        self._style_section()
        self.body.addSpacing(6)
        self._arrange_rows()
        self._include_box()
        self._delete_row()
        self._finish(tr("objects.tips.multi"))
        self.refresh()

    def refresh(self) -> None:
        sel = self.tool.selection()
        self.edit_box.setVisible(len(sel) > 1)
        self.title.setText(tr("objects.kind.many", n=len(sel)) if sel else tr("objects.none.title"))
        if len(sel) < 2:
            self.detail.setText("")
            self.meta.setVisible(False)
            return
        self.detail.setText(count_text(sel))
        self.meta.setVisible(False)
        r = _union(sel)
        scalable = all(o.kind in ("graphic", "image") for o in sel)
        graphics_only = all(o.kind == "graphic" for o in sel)
        for w in (self.w, self.h, self.w_label, self.h_label, self.lock):
            w.setVisible(scalable)
        self.size_note.setVisible(not scalable)
        self.size_note.setText(tr("objects.size", w=_fmt_mm(r.width), h=_fmt_mm(r.height)))
        self._loading = True
        try:
            self._load_position(r)
            if scalable:
                self._load_size(r)
            self.style_gap.setVisible(graphics_only)
            self._set_style_visible(graphics_only)
            if graphics_only:
                self._load_style(self.tool.primary.ref)
            self._load_include()
        finally:
            self._loading = False


class ImageArrangePanel(ImageObjectPanel):
    """:class:`ImageObjectPanel` plus "Læg forrest / bagerst" before "Slet billede"."""

    def __init__(self, tool: EditObjectTool) -> None:
        super().__init__(tool)
        lay = self.edit_box.layout()
        at = lay.indexOf(self.delete_btn)
        lay.insertSpacing(at, 6)
        lay.insertWidget(at + 1, label(tr("objects.section.arrange"), "section"))
        row = QHBoxLayout()
        row.setSpacing(4)
        self.front_btn = _row_button(self, row, "selection-foreground", tr("objects.action.front"),
                                     lambda: tool.restack(True),
                                     tooltip_with_shortcut(tr("objects.action.front.tip"), SHORTCUT_FRONT))
        self.back_btn = _row_button(self, row, "selection-background", tr("objects.action.back"),
                                    lambda: tool.restack(False),
                                    tooltip_with_shortcut(tr("objects.action.back.tip"), SHORTCUT_BACK))
        lay.insertLayout(at + 2, row)


def _fmt_mm(v: float) -> str:
    return QLocale().toString(v * MM_PER_PT, "f", 1)


def _style_text(g) -> str:
    """"Streg 2 pt · fyld" / "Kun fyld" / "Streg 3 pt · uden fyld"."""
    stroke, fill = getattr(g, "stroke", None), getattr(g, "fill", None)
    if stroke is None:
        return tr("objects.style.fill_only") if fill is not None else ""
    w = tr("objects.graphic.stroke", w=_num(getattr(g, "width", 0.0) or 0.0))
    if getattr(g, "kind", "") == "line" and fill is None:
        return w
    return tr("objects.style.with_fill" if fill is not None else "objects.style.no_fill", stroke=w)


EditObjectTool.PANELS.update({
    "none": ImageArrangePanel,
    "image": ImageArrangePanel,
    "text": TextObjectPanel,
    "graphic": GraphicObjectPanel,
    "multi": MultiObjectPanel,
})

__all__ = ["GraphicObjectPanel", "ImageArrangePanel", "MultiObjectPanel", "StyleSwatch", "TextObjectPanel"]
