"""Shared pieces of the "Kommentér" tools: style state, the inspector panel
(colour presets, opacity, stroke width, fill) and the note dialog.

Styles are remembered per tool in QSettings (local), so a user's favourite
highlight colour survives restarts.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass

from PySide6.QtCore import QSettings, Qt, Signal
from PySide6.QtGui import QPainter, QPainterPath, QPen
from PySide6.QtWidgets import (
    QCheckBox,
    QFormLayout,
    QFrame,
    QHBoxLayout,
    QPlainTextEdit,
    QSlider,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from ....i18n import tr
from ...dialogs.common import FolioDialog
from ...theme import tokens as T
from ...widgets.color_button import ColorButton, from_qcolor, to_qcolor
from ...widgets.util import label

# Annotation colours are document content, not UI chrome: fixed values.
PRESETS: tuple[tuple[float, float, float], ...] = (
    (1.0, 0.84, 0.0),     # yellow
    (0.47, 0.87, 0.35),   # green
    (0.33, 0.65, 1.0),    # blue
    (1.0, 0.45, 0.72),    # pink
    (0.93, 0.26, 0.21),   # red
    (0.13, 0.13, 0.15),   # ink
)


@dataclass
class AnnotStyle:
    color: tuple = PRESETS[0]
    opacity: float = 1.0
    width: float = 2.0
    fill: tuple | None = None

    # -- persistence (local QSettings only) ---------------------------------
    @classmethod
    def load(cls, key: str, default: "AnnotStyle") -> "AnnotStyle":
        s = QSettings()
        raw = s.value(f"annot/{key}")
        if not isinstance(raw, dict):
            return AnnotStyle(**asdict(default))
        try:
            fill = raw.get("fill")
            return cls(
                color=tuple(float(c) for c in raw.get("color", default.color)),
                opacity=float(raw.get("opacity", default.opacity)),
                width=float(raw.get("width", default.width)),
                fill=tuple(float(c) for c in fill) if fill else None,
            )
        except (TypeError, ValueError):
            return AnnotStyle(**asdict(default))

    def save(self, key: str) -> None:
        d = asdict(self)
        d["color"] = list(self.color)
        d["fill"] = list(self.fill) if self.fill else None
        QSettings().setValue(f"annot/{key}", d)


class Swatch(QToolButton):
    """Round preset swatch with a selection ring."""

    def __init__(self, color: tuple) -> None:
        super().__init__()
        self.color = color
        self.setCheckable(True)
        self.setFixedSize(26, 26)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setAutoRaise(True)

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


class AnnotStylePanel(QWidget):
    """Inspector panel for annotation tools. Emits ``styleChanged`` with the
    new :class:`AnnotStyle`."""

    styleChanged = Signal(object)

    def __init__(self, title: str, hint: str, style: AnnotStyle, *, width: bool = False,
                 fill: bool = False, opacity: bool = True, extra: QWidget | None = None) -> None:
        super().__init__()
        self.style_ = style
        v = QVBoxLayout(self)
        v.setContentsMargins(16, 4, 16, 16)
        v.setSpacing(10)
        card = QFrame()
        card.setObjectName("Card")
        cl = QVBoxLayout(card)
        cl.setContentsMargins(14, 12, 14, 12)
        cl.setSpacing(3)
        cl.addWidget(label(title, "heading"))
        cl.addWidget(label(hint, "muted", wrap=True))
        v.addWidget(card)
        if extra is not None:
            v.addWidget(extra)

        v.addWidget(label(tr("annot.section.style"), "section"))
        row = QHBoxLayout()
        row.setSpacing(4)
        self.swatches = []
        for c in PRESETS:
            sw = Swatch(c)
            sw.clicked.connect(lambda _=False, c=c: self._set_color(c))
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
        self.opacity.valueChanged.connect(self._changed)
        self.opacity_label = label("", "caption")
        if opacity:
            orow = QHBoxLayout()
            orow.addWidget(self.opacity, 1)
            orow.addWidget(self.opacity_label)
            form.addRow(tr("annot.opacity"), orow)
        self.width = QSlider(Qt.Orientation.Horizontal)
        self.width.setRange(1, 20)
        self.width.setValue(int(round(style.width * 2)))
        self.width.valueChanged.connect(self._changed)
        self.width_label = label("", "caption")
        if width:
            wrow = QHBoxLayout()
            wrow.addWidget(self.width, 1)
            wrow.addWidget(self.width_label)
            form.addRow(tr("annot.width"), wrow)
        self.fill = QCheckBox(tr("annot.fill"))
        self.fill.setChecked(style.fill is not None)
        self.fill.toggled.connect(self._changed)
        if fill:
            form.addRow("", self.fill)
        v.addLayout(form)
        v.addStretch(1)
        self._sync()

    def _set_color(self, color: tuple) -> None:
        self.style_.color = tuple(color)
        if self.style_.fill is not None:
            self.style_.fill = tuple(color)
        self._sync()
        self.styleChanged.emit(self.style_)

    def _changed(self, *_a) -> None:
        self.style_.opacity = self.opacity.value() / 100.0
        self.style_.width = self.width.value() / 2.0
        self.style_.fill = tuple(self.style_.color) if self.fill.isChecked() else None
        self._sync()
        self.styleChanged.emit(self.style_)

    def _sync(self) -> None:
        for sw in self.swatches:
            sw.setChecked(all(abs(a - b) < 0.01 for a, b in zip(sw.color, self.style_.color)))
        self.custom.set_color(tuple(self.style_.color))
        self.opacity_label.setText(f"{self.opacity.value()} %")
        self.width_label.setText(f"{self.width.value() / 2:g} pt")


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

__all__ = ["AnnotStyle", "AnnotStylePanel", "PRESETS", "ask_note", "from_qcolor"]
