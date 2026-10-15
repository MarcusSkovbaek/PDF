"""Shared pieces of the "Kommentér" tools: style state, the inspector panel
(colour presets, opacity, stroke width, fill, text font/size), a live style
sample, the page preview overlay and the note dialog.

Styles are remembered per tool in QSettings (local), so a user's favourite
highlight colour survives restarts.

The inspector panel separates *previewing* from *committing*: hovering a
swatch or dragging a slider emits ``previewRequested`` (nothing is written to
the PDF), while a click, a released slider or a settled spin box emits
``propertyChanged`` / ``styleChanged`` once – so restyling a comment is one
undo step instead of one per slider tick.
"""

from __future__ import annotations

import math
from dataclasses import asdict, dataclass
from typing import Any

import pymupdf
from PySide6.QtCore import QPointF, QRectF, QSettings, QSize, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QFont, QPainter, QPainterPath, QPen, QPixmap
from PySide6.QtWidgets import (
    QCheckBox,
    QDoubleSpinBox,
    QFormLayout,
    QFrame,
    QGraphicsItem,
    QHBoxLayout,
    QPlainTextEdit,
    QSlider,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from ....core import annotations as core_annots
from ....i18n import tr
from ...dialogs.common import FolioDialog
from ...theme import tokens as T
from ...widgets.color_button import ColorButton, from_qcolor, to_qcolor
from ...widgets.segmented import Segment, SegmentedControl
from ...widgets.util import label
from ..render_util import to_qimage
from .base import Tool

# Annotation colours are document content, not UI chrome: fixed values.
PRESETS: tuple[tuple[float, float, float], ...] = (
    (1.0, 0.84, 0.0),     # yellow
    (0.47, 0.87, 0.35),   # green
    (0.33, 0.65, 1.0),    # blue
    (1.0, 0.45, 0.72),    # pink
    (0.93, 0.26, 0.21),   # red
    (0.13, 0.13, 0.15),   # ink
)

# Which inspector controls apply to which annotation kinds.
WIDTH_KINDS = ("ink", "line", "arrow", "rect", "ellipse")
FILL_KINDS = ("rect", "ellipse")
TEXT_KINDS = ("freetext",)
FONT_LABELS = {"sans": "Helvetica", "serif": "Times", "mono": "Courier"}  # PDF base-14 families
COMMIT_DELAY_MS = 350  # keyboard / wheel changes settle before they are written


@dataclass
class AnnotStyle:
    color: tuple = PRESETS[0]
    opacity: float = 1.0
    width: float = 2.0
    fill: tuple | None = None
    fontsize: float = 12.0          # free text
    font: str = "sans"              # free text: core.annotations.FREETEXT_FONTS

    # -- persistence (local QSettings only) ---------------------------------
    @classmethod
    def load(cls, key: str, default: "AnnotStyle") -> "AnnotStyle":
        s = QSettings()
        raw = s.value(f"annot/{key}")
        if not isinstance(raw, dict):
            return AnnotStyle(**asdict(default))
        try:
            fill = raw.get("fill")
            font = str(raw.get("font", default.font))
            return cls(
                color=tuple(float(c) for c in raw.get("color", default.color)),
                opacity=float(raw.get("opacity", default.opacity)),
                width=float(raw.get("width", default.width)),
                fill=tuple(float(c) for c in fill) if fill else None,
                fontsize=min(96.0, max(4.0, float(raw.get("fontsize", default.fontsize)))),
                font=font if font in core_annots.FREETEXT_FONTS else default.font,
            )
        except (TypeError, ValueError):
            return AnnotStyle(**asdict(default))

    def save(self, key: str) -> None:
        d = asdict(self)
        d["color"] = list(self.color)
        d["fill"] = list(self.fill) if self.fill else None
        QSettings().setValue(f"annot/{key}", d)


def same_color(a: tuple | None, b: tuple | None, tol: float = 0.01) -> bool:
    if a is None or b is None:
        return a is b
    return all(abs(x - y) < tol for x, y in zip(a, b))


# ---------------------------------------------------------------------------
# swatches and the live style sample
# ---------------------------------------------------------------------------
class Swatch(QToolButton):
    """Round preset swatch with a selection ring; reports hovering so the
    colour can be previewed before it is chosen."""

    hovered = Signal(bool)

    def __init__(self, color: tuple) -> None:
        super().__init__()
        self.color = color
        self.setCheckable(True)
        self.setFixedSize(26, 26)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setAutoRaise(True)

    def enterEvent(self, ev) -> None:  # noqa: N802
        super().enterEvent(ev)
        self.hovered.emit(True)

    def leaveEvent(self, ev) -> None:  # noqa: N802
        super().leaveEvent(ev)
        self.hovered.emit(False)

    def paintEvent(self, _ev) -> None:  # noqa: N802
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        r = self.rect().adjusted(4, 4, -4, -4)
        if self.isChecked():
            p.setPen(QPen(T.qcolor("accent"), 2))
            p.drawEllipse(self.rect().adjusted(1, 1, -1, -1))
        elif self.underMouse():
            p.setPen(QPen(T.qcolor("border_strong"), 1))
            p.drawEllipse(self.rect().adjusted(1, 1, -1, -1))
        path = QPainterPath()
        path.addEllipse(r)
        p.fillPath(path, to_qcolor(self.color))
        p.setPen(QPen(T.qcolor("border_strong"), 1))  # keeps dark swatches visible in dark mode
        p.drawEllipse(r)
        p.end()


class StyleSample(QWidget):
    """A small piece of "paper" showing what the tool will draw with the
    current style – and, while a swatch is hovered, with that colour."""

    def __init__(self, kind: str, style: AnnotStyle) -> None:
        super().__init__()
        self.kind = kind
        self.style_ = style
        self.preview: dict[str, Any] = {}
        self.setFixedHeight(64 if kind == "freetext" else 52)

    def sizeHint(self) -> QSize:  # noqa: N802
        return QSize(220, self.height())

    def set_preview(self, props: dict[str, Any] | None) -> None:
        self.preview = dict(props or {})
        self.update()

    def _value(self, name: str) -> Any:
        return self.preview.get(name, getattr(self.style_, name))

    def paintEvent(self, _ev) -> None:  # noqa: N802
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        r = QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5)
        paper = QPainterPath()
        paper.addRoundedRect(r, 10, 10)
        p.fillPath(paper, T.qcolor("page"))
        p.setPen(QPen(T.qcolor("border"), 1))
        p.drawPath(paper)
        p.setClipPath(paper)
        color = to_qcolor(self._value("color"))
        opacity = float(self._value("opacity"))
        ink = to_qcolor(PRESETS[5])  # the "document" text on the paper
        kind = self.kind
        if kind in ("highlight", "underline", "strikeout"):
            self._paint_markup(p, r, color, opacity, ink)
        elif kind == "freetext":
            self._paint_text(p, r, color, opacity)
        elif kind == "note":
            self._paint_note(p, r, color)
        else:
            self._paint_stroke(p, r, color, opacity)
        p.end()

    def _paint_markup(self, p: QPainter, r: QRectF, color: QColor, opacity: float, ink: QColor) -> None:
        f = QFont(self.font())
        f.setFamilies(["Times New Roman", "Liberation Serif", "Times", "serif"])
        f.setPixelSize(17)
        p.setFont(f)
        text = tr("annot.sample.markup")
        fm = p.fontMetrics()
        tw = fm.horizontalAdvance(text)
        x = r.center().x() - tw / 2
        base = r.center().y() + fm.ascent() / 2 - 2
        band = QRectF(x - 3, base - fm.ascent() - 1, tw + 6, fm.ascent() + fm.descent() + 2)
        c = QColor(color)
        if self.kind == "highlight":
            c.setAlphaF(opacity)
            p.save()
            p.setCompositionMode(QPainter.CompositionMode.CompositionMode_Multiply)
            p.fillRect(band, c)
            p.restore()
            p.setPen(ink)
            p.drawText(QPointF(x, base), text)
            return
        p.setPen(ink)
        p.drawText(QPointF(x, base), text)
        c.setAlphaF(opacity)
        y = base + 2.5 if self.kind == "underline" else base - fm.xHeight() / 2
        p.setPen(QPen(c, 1.6))
        p.drawLine(QPointF(x, y), QPointF(x + tw, y))

    def _paint_text(self, p: QPainter, r: QRectF, color: QColor, opacity: float) -> None:
        font_key = str(self._value("font"))
        size = float(self._value("fontsize"))
        f = QFont(self.font())
        f.setFamilies({"sans": ["Arial", "Liberation Sans", "Helvetica", "sans-serif"],
                       "serif": ["Times New Roman", "Liberation Serif", "Times", "serif"],
                       "mono": ["Courier New", "Liberation Mono", "Courier", "monospace"]}[font_key])
        f.setPixelSize(max(9, min(30, round(size * 1.15))))  # on-paper size, kept within the strip
        p.setFont(f)
        c = QColor(color)
        c.setAlphaF(opacity)
        p.setPen(c)
        p.drawText(r.adjusted(14, 0, -10, 0), Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft,
                   tr("annot.sample.text"))

    def _paint_note(self, p: QPainter, r: QRectF, color: QColor) -> None:
        box = QRectF(r.center().x() - 13, r.center().y() - 13, 26, 26)
        path = QPainterPath()
        path.addRoundedRect(box, 4, 4)
        p.fillPath(path, color)
        ink = to_qcolor(PRESETS[5])
        ink.setAlphaF(0.35)
        p.setPen(QPen(ink, 1))
        p.drawPath(path)
        ink.setAlphaF(0.6)
        p.setPen(QPen(ink, 1.6))
        for i in range(3):
            y = box.top() + 8 + i * 5
            p.drawLine(QPointF(box.left() + 6, y), QPointF(box.right() - (6 if i < 2 else 11), y))

    def _paint_stroke(self, p: QPainter, r: QRectF, color: QColor, opacity: float) -> None:
        width = max(0.5, float(self._value("width")))
        fill = self._value("fill")
        c = QColor(color)
        c.setAlphaF(opacity)
        pen = QPen(c, width)
        pen.setCapStyle(Qt.PenCapStyle.RoundCap)
        pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
        p.setPen(pen)
        inner = r.adjusted(26, 12, -26, -12)
        kind = self.kind
        if kind == "ink":
            path = QPainterPath(QPointF(inner.left(), inner.center().y()))
            steps = 48
            for i in range(1, steps + 1):
                t = i / steps
                x = inner.left() + inner.width() * t
                y = inner.center().y() + math.sin(t * math.pi * 3) * inner.height() * 0.42 * (1 - 0.35 * t)
                path.lineTo(x, y)
            p.drawPath(path)
            return
        if fill is not None and kind in ("rect", "ellipse"):
            fc = to_qcolor(fill)
            fc.setAlphaF(opacity)
            p.setBrush(fc)
        if kind == "ellipse":
            p.drawEllipse(inner.adjusted(inner.width() * 0.2, 0, -inner.width() * 0.2, 0))
        elif kind in ("line", "arrow"):
            a, b = QPointF(inner.left() + 20, inner.bottom()), QPointF(inner.right() - 20, inner.top())
            p.drawLine(a, b)
            if kind == "arrow":
                ang = math.atan2(a.y() - b.y(), a.x() - b.x())
                size = 7 + width * 1.5
                for d in (-0.45, 0.45):
                    p.drawLine(b, QPointF(b.x() + size * math.cos(ang + d), b.y() + size * math.sin(ang + d)))
        else:
            p.drawRect(inner.adjusted(inner.width() * 0.2, 0, -inner.width() * 0.2, 0))


# ---------------------------------------------------------------------------
# inspector panel
# ---------------------------------------------------------------------------
class AnnotStylePanel(QWidget):
    """Inspector panel for annotation tools and selections.

    Signals:
    * ``styleChanged(AnnotStyle)`` – a committed change (creation tools keep it);
    * ``propertyChanged(name, value)`` – the same change as one property
      (``color``, ``opacity``, ``width``, ``fill``, ``fontsize``, ``font``);
    * ``previewRequested(dict | None)`` – show these properties without
      committing them (hovered swatch, dragged slider); ``None`` ends it.

    ``sample`` adds a :class:`StyleSample` of that kind; ``mixed`` lists
    properties whose values differ within a multi-selection.
    """

    styleChanged = Signal(object)
    propertyChanged = Signal(str, object)
    previewRequested = Signal(object)

    def __init__(self, title: str, hint: str, style: AnnotStyle, *, width: bool = False,
                 fill: bool = False, opacity: bool = True, text: bool = False, sample: str | None = None,
                 mixed: frozenset[str] | set[str] = frozenset(), extra: QWidget | None = None) -> None:
        super().__init__()
        self.style_ = style
        self.mixed = set(mixed)
        self._pending: tuple[str, Any] | None = None
        self._commit_timer = QTimer(self)
        self._commit_timer.setSingleShot(True)
        self._commit_timer.setInterval(COMMIT_DELAY_MS)
        self._commit_timer.timeout.connect(self._commit_pending)

        v = QVBoxLayout(self)
        v.setContentsMargins(16, 4, 16, 16)
        v.setSpacing(10)
        card = QFrame()
        card.setObjectName("Card")
        cl = QVBoxLayout(card)
        cl.setContentsMargins(14, 12, 14, 12)
        cl.setSpacing(3)
        self.title = label(title, "heading")
        cl.addWidget(self.title)
        cl.addWidget(label(hint, "muted", wrap=True))
        v.addWidget(card)
        if extra is not None:
            v.addWidget(extra)

        self.sample: StyleSample | None = None
        self.font_seg: SegmentedControl | None = None
        self.size: QDoubleSpinBox | None = None
        if text:
            v.addWidget(label(tr("annot.section.text"), "section"))
            self.font_seg = SegmentedControl(
                [Segment(k, FONT_LABELS[k], None, tr(f"annot.font.{k}")) for k in core_annots.FREETEXT_FONTS],
                expand=True, height=32, min_seg=60)
            self.font_seg.set_current_key(style.font, animate=False)
            self.font_seg.currentChanged.connect(self._set_font)
            v.addWidget(self.font_seg)
            tform = QFormLayout()
            tform.setSpacing(8)
            self.size = QDoubleSpinBox()
            self.size.setRange(4.0, 96.0)
            self.size.setDecimals(1)
            self.size.setSingleStep(1.0)
            self.size.setSuffix(" pt")
            self.size.setValue(style.fontsize)
            self.size.setKeyboardTracking(False)
            self.size.valueChanged.connect(self._size_changed)
            tform.addRow(tr("annot.fontsize"), self.size)
            v.addLayout(tform)

        v.addWidget(label(tr("annot.section.style"), "section"))
        if sample:
            self.sample = StyleSample(sample, style)
            v.addWidget(self.sample)
        row = QHBoxLayout()
        row.setSpacing(4)
        self.swatches: list[Swatch] = []
        for c in PRESETS:
            sw = Swatch(c)
            sw.clicked.connect(lambda _=False, c=c: self._set_color(c))
            sw.hovered.connect(lambda on, c=c: self._hover_color(c if on else None))
            row.addWidget(sw)
            self.swatches.append(sw)
        self.custom = ColorButton(style.color, title=tr("annot.color_title"))
        self.custom.colorChanged.connect(self._set_color)
        row.addSpacing(4)
        row.addWidget(self.custom)
        row.addStretch(1)
        v.addLayout(row)

        form = QFormLayout()
        form.setSpacing(8)
        self.opacity = QSlider(Qt.Orientation.Horizontal)
        self.opacity.setRange(10, 100)
        self.opacity.setValue(int(round(style.opacity * 100)))
        self.opacity.valueChanged.connect(lambda _v: self._slider_moved("opacity"))
        self.opacity.sliderReleased.connect(self._commit_pending)
        self.opacity_label = label("", "caption")
        self.opacity_label.setMinimumWidth(38)
        if opacity:
            orow = QHBoxLayout()
            orow.addWidget(self.opacity, 1)
            orow.addWidget(self.opacity_label)
            form.addRow(tr("annot.opacity"), orow)
        self.width = QSlider(Qt.Orientation.Horizontal)
        self.width.setRange(1, 20)
        self.width.setValue(int(round(style.width * 2)))
        self.width.valueChanged.connect(lambda _v: self._slider_moved("width"))
        self.width.sliderReleased.connect(self._commit_pending)
        self.width_label = label("", "caption")
        self.width_label.setMinimumWidth(38)
        if width:
            wrow = QHBoxLayout()
            wrow.addWidget(self.width, 1)
            wrow.addWidget(self.width_label)
            form.addRow(tr("annot.width"), wrow)
        self.fill = QCheckBox(tr("annot.fill"))
        if "fill" in self.mixed:
            self.fill.setTristate(True)
            self.fill.setCheckState(Qt.CheckState.PartiallyChecked)
        else:
            self.fill.setChecked(style.fill is not None)
        self.fill.clicked.connect(self._fill_clicked)
        if fill:
            form.addRow("", self.fill)
        v.addLayout(form)
        v.addStretch(1)
        self._sync()

    # -- committing -----------------------------------------------------------
    def _emit(self, name: str, value: Any) -> None:
        self.mixed.discard(name)
        self._sync()
        self.previewRequested.emit(None)
        self.propertyChanged.emit(name, value)
        self.styleChanged.emit(self.style_)

    def _set_color(self, color: tuple) -> None:
        self._flush()
        self.style_.color = tuple(color)
        if self.style_.fill is not None:
            self.style_.fill = tuple(color)
        self._emit("color", tuple(color))

    def _set_font(self, key: str) -> None:
        self._flush()
        self.style_.font = key
        self._emit("font", key)

    def _fill_clicked(self, *_a) -> None:
        self._flush()
        self.fill.setTristate(False)  # a mixed selection becomes all-or-nothing
        on = self.fill.checkState() != Qt.CheckState.Unchecked
        self.style_.fill = tuple(self.style_.color) if on else None
        self._emit("fill", self.style_.fill)

    def _slider_moved(self, name: str) -> None:
        value = self.opacity.value() / 100.0 if name == "opacity" else self.width.value() / 2.0
        setattr(self.style_, name, value)
        self.mixed.discard(name)
        self._sync()
        if self._pending is not None and self._pending[0] != name:
            self._commit_pending()
        self._pending = (name, value)
        self._preview({name: value})
        slider = self.opacity if name == "opacity" else self.width
        if not slider.isSliderDown():  # keyboard / wheel / click on the groove
            self._commit_timer.start()

    def _size_changed(self, value: float) -> None:
        self.style_.fontsize = float(value)
        self.mixed.discard("fontsize")
        if self._pending is not None and self._pending[0] != "fontsize":
            self._commit_pending()
        self._pending = ("fontsize", float(value))
        self._preview({"fontsize": float(value)})
        self._commit_timer.start()

    def _commit_pending(self) -> None:
        self._commit_timer.stop()
        pending, self._pending = self._pending, None
        if pending is not None:
            self._emit(*pending)

    def _flush(self) -> None:
        """Commit a pending slider/spin change before another property."""
        if self._pending is not None:
            self._commit_pending()

    # -- previewing ------------------------------------------------------------
    def _preview(self, props: dict[str, Any] | None) -> None:
        if self.sample is not None:
            self.sample.set_preview(props)
        self.previewRequested.emit(props)

    def _hover_color(self, color: tuple | None) -> None:
        if color is None:
            if self._pending is None:
                self._preview(None)
            return
        props: dict[str, Any] = {"color": tuple(color)}
        if self.style_.fill is not None and "fill" not in self.mixed:
            props["fill"] = tuple(color)
        self._preview(props)

    # -- state ----------------------------------------------------------------
    def _sync(self) -> None:
        mixed_color = "color" in self.mixed
        for sw in self.swatches:
            sw.setChecked(not mixed_color and same_color(sw.color, self.style_.color))
        self.custom.set_color(tuple(self.style_.color))
        self.opacity_label.setText("–" if "opacity" in self.mixed else f"{self.opacity.value()} %")
        self.width_label.setText("–" if "width" in self.mixed else f"{self.width.value() / 2:g} pt")
        if self.sample is not None:
            self.sample.style_ = self.style_
            self.sample.update()

    def set_title(self, text: str) -> None:
        self.title.setText(text)


# ---------------------------------------------------------------------------
# base class of the creation tools
# ---------------------------------------------------------------------------
class StyledTool(Tool):
    """A "Kommentér" tool with a remembered style and the style inspector."""

    mode = "annotate"
    default_style = AnnotStyle(PRESETS[5], 1.0, 2.0)
    panel_width = True
    panel_fill = False
    panel_text = False
    sample_kind: str | None = None

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.style = AnnotStyle.load(self.id, self.default_style)
        self._panel: AnnotStylePanel | None = None

    def extra_widget(self) -> QWidget | None:
        return None

    def properties_widget(self):
        if self._panel is None:
            self._panel = AnnotStylePanel(self.label, self.hint, self.style, width=self.panel_width,
                                          fill=self.panel_fill, text=self.panel_text, sample=self.sample_kind,
                                          extra=self.extra_widget())
            self._panel.styleChanged.connect(self._style_changed)
            self._panel.previewRequested.connect(self.preview_style)
        return self._panel

    def _style_changed(self, style: AnnotStyle) -> None:
        self.style = style
        style.save(self.id)

    def preview_style(self, props: dict | None) -> None:
        """A not yet chosen style is being previewed (``None``: back to the
        current one). The inspector's sample shows it; tools with something
        live on the page (an open text box) show it there too."""


# ---------------------------------------------------------------------------
# page preview overlay
# ---------------------------------------------------------------------------
class _PixmapPatch(QGraphicsItem):
    """A rendered piece of a page, drawn pixel-aligned with the page pixmap."""

    def __init__(self, pixmap: QPixmap, offset: tuple[int, int], scale: float) -> None:
        super().__init__()
        self.pixmap = pixmap
        self.offset = offset          # device pixels inside the full-page rendering
        self.scale_ = scale           # device pixels per pt it was rendered at
        self.setAcceptedMouseButtons(Qt.MouseButton.NoButton)

    def boundingRect(self) -> QRectF:  # noqa: N802
        s = self.scale_
        return QRectF(self.offset[0] / s, self.offset[1] / s, self.pixmap.width() / s, self.pixmap.height() / s)

    def paint(self, p: QPainter, _opt, _widget=None) -> None:
        t = p.worldTransform()
        target = t.mapRect(self.boundingRect())
        if abs(t.m11() - self.scale_) < 1e-3:
            # same scale as rendered: snap to the page pixmap's pixel grid
            origin = t.map(QPointF(0, 0))
            target = QRectF(round(origin.x()) + self.offset[0], round(origin.y()) + self.offset[1],
                            self.pixmap.width(), self.pixmap.height())
        p.save()
        p.resetTransform()
        p.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, True)
        p.drawPixmap(target, self.pixmap, QRectF(self.pixmap.rect()))
        p.restore()


class PagePreview:
    """Shows parts of pages as :func:`core.annotations.render_preview` renders
    them – a change to annotations that is not made yet."""

    Z = 12  # above the page, below selection frames and editors

    def __init__(self, view) -> None:
        self.view = view
        self.items: list[_PixmapPatch] = []

    def show(self, doc, page: int, changes: dict, clip: pymupdf.Rect) -> bool:
        self.clear()
        view = self.view
        scale = view.zoom * view.dpr()
        pix = core_annots.render_preview(doc, page, changes, scale, clip=clip)
        if pix is None:
            return False
        img = to_qimage(pix)
        item = _PixmapPatch(QPixmap.fromImage(img), (pix.x, pix.y), scale)
        item.setZValue(self.Z)
        try:
            view.add_overlay(page, item)
        except IndexError:
            return False
        self.items.append(item)
        return True

    def add(self, doc, page: int, changes: dict, clip: pymupdf.Rect) -> None:
        """Like :meth:`show` but keeps the patches already shown (several pages)."""
        keep, self.items = self.items, []
        self.show(doc, page, changes, clip)
        self.items = keep + self.items

    def clear(self) -> None:
        for it in self.items:
            try:
                self.view.remove_overlay(it)
            except RuntimeError:
                pass
        self.items = []

    @property
    def active(self) -> bool:
        return bool(self.items)


# ---------------------------------------------------------------------------
# note dialog
# ---------------------------------------------------------------------------
class NoteDialog(FolioDialog):
    """Write / edit the text of a sticky note."""

    def __init__(self, parent: QWidget | None, text: str = "", *, editing: bool = False) -> None:
        super().__init__(parent, tr("annot.note.title_edit" if editing else "annot.note.title"),
                         tr("annot.note.subtitle"), icon_name="note-pencil", width=420)
        self.edit = QPlainTextEdit(text)
        self.edit.setPlaceholderText(tr("annot.note.placeholder"))
        self.edit.setMinimumHeight(120)
        self.body.addWidget(self.edit)
        self.add_button(tr("annot.cancel"), "secondary", "cancel")
        self.add_button(tr("annot.note.save"), "primary", "ok", default=True)
        self.edit.setFocus()

    def text(self) -> str:
        return self.edit.toPlainText().strip()


def ask_note(parent: QWidget | None, text: str = "", *, editing: bool = False) -> str | None:
    dlg = NoteDialog(parent, text, editing=editing)
    if _AUTO_NOTE is not None:
        return _AUTO_NOTE
    return dlg.text() if dlg.exec() == FolioDialog.DialogCode.Accepted else None


# Test hook: pre-answer the note dialog.
_AUTO_NOTE: str | None = None

__all__ = [
    "AnnotStyle",
    "AnnotStylePanel",
    "FILL_KINDS",
    "PRESETS",
    "PagePreview",
    "StyleSample",
    "StyledTool",
    "TEXT_KINDS",
    "WIDTH_KINDS",
    "ask_note",
    "from_qcolor",
    "same_color",
]
