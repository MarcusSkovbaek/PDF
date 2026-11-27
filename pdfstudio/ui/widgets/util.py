"""Small shared helpers for building themed widgets."""

from __future__ import annotations

from PySide6.QtCore import QEvent, QSize, Qt
from PySide6.QtGui import QColor, QKeySequence
from PySide6.QtWidgets import (
    QFrame,
    QGraphicsDropShadowEffect,
    QHBoxLayout,
    QLabel,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from ..theme import tokens as T
from ..theme.icons import icon


def on_theme_change(owner, callback) -> None:
    """Call ``callback(tokens)``-compatible slot whenever the theme changes.

    ``owner`` (a QObject) bounds the connection's lifetime.
    """
    from ..theme.manager import theme_manager

    tm = theme_manager()

    def _cb(*_a):
        try:
            callback()
        except RuntimeError:  # owner already deleted
            pass

    tm.changed.connect(_cb)
    try:
        owner.destroyed.connect(lambda *_: _safe_disconnect(tm.changed, _cb))
    except Exception:
        pass


def _safe_disconnect(signal, cb) -> None:
    try:
        signal.disconnect(cb)
    except Exception:
        pass


def set_prop(widget: QWidget, name: str, value) -> None:
    """Set a dynamic property used by the stylesheet and re-polish."""
    if widget.property(name) == value:
        return
    widget.setProperty(name, value)
    st = widget.style()
    st.unpolish(widget)
    st.polish(widget)
    widget.update()


def label(text: str = "", role: str | None = None, parent: QWidget | None = None, *, wrap: bool = False) -> QLabel:
    lbl = QLabel(text, parent)
    if role:
        lbl.setProperty("role", role)
    if wrap:
        lbl.setWordWrap(True)
    return lbl


def divider(orientation: str = "h", parent: QWidget | None = None) -> QFrame:
    f = QFrame(parent)
    f.setProperty("divider", orientation)
    return f


def hbox(*widgets, margins=(0, 0, 0, 0), spacing: int = 8, parent: QWidget | None = None) -> QHBoxLayout:
    lay = QHBoxLayout(parent) if parent is not None else QHBoxLayout()
    lay.setContentsMargins(*margins)
    lay.setSpacing(spacing)
    for w in widgets:
        if w is None:
            lay.addStretch(1)
        elif isinstance(w, int):
            lay.addSpacing(w)
        elif isinstance(w, QWidget):
            lay.addWidget(w)
        else:
            lay.addLayout(w)
    return lay


def vbox(*widgets, margins=(0, 0, 0, 0), spacing: int = 8, parent: QWidget | None = None) -> QVBoxLayout:
    lay = QVBoxLayout(parent) if parent is not None else QVBoxLayout()
    lay.setContentsMargins(*margins)
    lay.setSpacing(spacing)
    for w in widgets:
        if w is None:
            lay.addStretch(1)
        elif isinstance(w, int):
            lay.addSpacing(w)
        elif isinstance(w, QWidget):
            lay.addWidget(w)
        else:
            lay.addLayout(w)
    return lay


def shortcut_text(seq) -> str:
    """Human readable shortcut (first sequence), e.g. ``Ctrl+Shift+S``."""
    if seq is None:
        return ""
    if isinstance(seq, (list, tuple)):
        seq = seq[0] if seq else None
        if seq is None:
            return ""
    ks = seq if isinstance(seq, QKeySequence) else QKeySequence(seq)
    return ks.toString(QKeySequence.SequenceFormat.NativeText)


def tooltip_with_shortcut(text: str, seq=None) -> str:
    sc = shortcut_text(seq)
    return f"{text}  ({sc})" if sc else text


class ThemedShadow(QGraphicsDropShadowEffect):
    """Drop shadow whose colour follows the theme."""

    def __init__(self, parent=None, blur: float = 28, y: float = 8, role: str = "shadow_strong") -> None:
        super().__init__(parent)
        self._role = role
        self.setBlurRadius(blur)
        self.setOffset(0, y)
        self.refresh()
        try:
            on_theme_change(self, self.refresh)
        except Exception:
            pass

    def refresh(self, *_args) -> None:
        try:
            self.setColor(T.qcolor(self._role))
        except RuntimeError:
            pass


def add_shadow(widget: QWidget, blur: float = 28, y: float = 8, role: str = "shadow_strong") -> ThemedShadow:
    eff = ThemedShadow(widget, blur, y, role)
    widget.setGraphicsEffect(eff)
    return eff


class IconButton(QToolButton):
    """QToolButton showing a themed Phosphor icon that refreshes on theme change."""

    def __init__(
        self,
        icon_name: str,
        tooltip: str = "",
        parent: QWidget | None = None,
        *,
        variant: str = "icon",
        checkable: bool = False,
        role: str = "text_muted",
        on_role: str | None = "accent_text",
        icon_size: int = 18,
        shortcut=None,
    ) -> None:
        super().__init__(parent)
        self._icon_name = icon_name
        self._role = role
        self._on_role = on_role
        self.setProperty("variant", variant)
        self.setCheckable(checkable)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setIconSize(QSize(icon_size, icon_size))
        self.setFocusPolicy(Qt.FocusPolicy.TabFocus)
        if tooltip:
            self.setToolTip(tooltip_with_shortcut(tooltip, shortcut))
        self._apply_icon()
        on_theme_change(self, self._apply_icon)

    def set_icon_name(self, name: str, role: str | None = None) -> None:
        self._icon_name = name
        if role:
            self._role = role
        self._apply_icon()

    def _apply_icon(self) -> None:
        self.setIcon(icon(self._icon_name, self._role, on_role=self._on_role if self.isCheckable() else None))

    def setCheckable(self, on: bool) -> None:  # noqa: N802 - Qt naming
        super().setCheckable(on)
        if getattr(self, "_icon_name", None):
            self._apply_icon()



class ThemedIconLabel(QLabel):
    """A QLabel showing a themed icon pixmap."""

    def __init__(self, icon_name: str, size: int = 18, role: str = "text_muted", parent=None) -> None:
        super().__init__(parent)
        self._name, self._size, self._role = icon_name, size, role
        self.setFixedSize(size, size)
        self._apply()
        on_theme_change(self, self._apply)

    def set_icon(self, name: str, role: str | None = None) -> None:
        self._name = name
        if role:
            self._role = role
        self._apply()

    def _apply(self) -> None:
        dpr = self.devicePixelRatioF() if self.isVisible() else 2.0
        self.setPixmap(icon(self._name, self._role).pixmap(QSize(self._size, self._size), max(dpr, 1.0)))



def mix(a: QColor, b: QColor, t: float) -> QColor:
    return QColor(
        round(a.red() + (b.red() - a.red()) * t),
        round(a.green() + (b.green() - a.green()) * t),
        round(a.blue() + (b.blue() - a.blue()) * t),
        round(a.alpha() + (b.alpha() - a.alpha()) * t),
    )
