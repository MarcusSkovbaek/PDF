"""Non-blocking, animated toast notifications.

    host = ToastHost(main_window_central_widget)
    host.show("Gemt", "success")
    host.show("3 sider slettet", action_text="Fortryd", on_action=pdf.undo)
"""

from __future__ import annotations

from typing import Callable

from PySide6.QtCore import (
    QEasingCurve,
    QEvent,
    QObject,
    QParallelAnimationGroup,
    QPoint,
    QPropertyAnimation,
    Qt,
    QTimer,
)
from PySide6.QtWidgets import QFrame, QGraphicsOpacityEffect, QHBoxLayout, QLabel, QPushButton, QWidget

from ..theme import tokens as T
from .util import IconButton, ThemedIconLabel, add_shadow

KIND_ICON = {
    "info": ("info", "accent_text"),
    "success": ("check-circle", "success"),
    "warning": ("warning", "warning"),
    "error": ("warning-circle", "danger"),
}


class Toast(QWidget):
    def __init__(self, host: "ToastHost", text: str, kind: str, action_text: str | None,
                 on_action: Callable[[], None] | None, duration_ms: int) -> None:
        super().__init__(host.widget)
        self._host = host
        self._closing = False
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, False)
        outer = QHBoxLayout(self)
        outer.setContentsMargins(16, 10, 16, 20)  # room for the shadow
        frame = QFrame(self)
        frame.setObjectName("Toast")
        outer.addWidget(frame)
        lay = QHBoxLayout(frame)
        lay.setContentsMargins(12, 8, 8, 8)
        lay.setSpacing(10)
        name, role = KIND_ICON.get(kind, KIND_ICON["info"])
        lay.addWidget(ThemedIconLabel(name, 18, role))
        msg = QLabel(text)
        msg.setMaximumWidth(460)
        msg.setWordWrap(len(text) > 60)
        lay.addWidget(msg, 1)
        if action_text and on_action:
            btn = QPushButton(action_text)
            btn.setProperty("variant", "link")
            btn.setCursor(Qt.CursorShape.PointingHandCursor)

            def run():
                self.dismiss()
                on_action()

            btn.clicked.connect(run)
            lay.addSpacing(4)
            lay.addWidget(btn)
        close = IconButton("x", "", variant="icon-sm", icon_size=14, role="text_subtle")
        close.clicked.connect(self.dismiss)
        lay.addWidget(close)
        add_shadow(frame, blur=30, y=8)

        self._opacity = QGraphicsOpacityEffect(self)
        self._opacity.setOpacity(0.0)
        self.setGraphicsEffect(self._opacity)

        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.setInterval(duration_ms)
        self._timer.timeout.connect(self.dismiss)
        self.adjustSize()

    # pause while hovered
    def enterEvent(self, ev) -> None:  # noqa: N802
        self._timer.stop()
        super().enterEvent(ev)

    def leaveEvent(self, ev) -> None:  # noqa: N802
        if not self._closing:
            self._timer.start(1500)
        super().leaveEvent(ev)

    def appear(self, target: QPoint) -> None:
        self.move(target + QPoint(0, 14))
        self.show()
        self.raise_()
        grp = QParallelAnimationGroup(self)
        a = QPropertyAnimation(self, b"pos", self)
        a.setDuration(220)
        a.setStartValue(target + QPoint(0, 14))
        a.setEndValue(target)
        a.setEasingCurve(QEasingCurve.Type.OutCubic)
        b = QPropertyAnimation(self._opacity, b"opacity", self)
        b.setDuration(180)
        b.setStartValue(0.0)
        b.setEndValue(1.0)
        grp.addAnimation(a)
        grp.addAnimation(b)
        grp.start()
        self._anim = grp
        self._timer.start()

    def slide_to(self, target: QPoint) -> None:
        if self._closing:
            return
        a = QPropertyAnimation(self, b"pos", self)
        a.setDuration(T.ANIM_MS)
        a.setEndValue(target)
        a.setEasingCurve(QEasingCurve.Type.OutCubic)
        a.start()
        self._slide = a

    def dismiss(self) -> None:
        if self._closing:
            return
        self._closing = True
        self._timer.stop()
        b = QPropertyAnimation(self._opacity, b"opacity", self)
        b.setDuration(T.ANIM_MS)
        b.setStartValue(self._opacity.opacity())
        b.setEndValue(0.0)
        b.finished.connect(self._finish)
        b.start()
        self._fade = b

    def _finish(self) -> None:
        self._host._remove(self)
        self.deleteLater()


class ToastHost(QObject):
    """Owns the toasts shown over ``widget`` (bottom centre, stacked)."""

    MAX = 4

    def __init__(self, widget: QWidget, bottom_margin: int = 24) -> None:
        super().__init__(widget)
        self.widget = widget
        self.bottom_margin = bottom_margin
        self._toasts: list[Toast] = []
        widget.installEventFilter(self)

    def show(self, text: str, kind: str = "info", *, action_text: str | None = None,
             on_action: Callable[[], None] | None = None, duration_ms: int | None = None) -> Toast:
        if duration_ms is None:
            duration_ms = 6000 if action_text else (5000 if kind == "error" else 3200)
        while len(self._toasts) >= self.MAX:
            old = self._toasts.pop(0)
            old.hide()
            old.deleteLater()
        t = Toast(self, text, kind, action_text, on_action, duration_ms)
        self._toasts.append(t)
        positions = self._positions()
        for other, pos in zip(self._toasts[:-1], positions[:-1]):
            other.slide_to(pos)
        t.appear(positions[-1])
        return t

    __call__ = show

    def _positions(self) -> list[QPoint]:
        w = self.widget
        y = w.height() - self.bottom_margin + 20
        out: list[QPoint] = []
        for t in reversed(self._toasts):
            sz = t.sizeHint()
            t.resize(sz)
            y -= sz.height() - 12
            out.append(QPoint((w.width() - sz.width()) // 2, y))
        return list(reversed(out))

    def _remove(self, toast: Toast) -> None:
        if toast in self._toasts:
            self._toasts.remove(toast)
            for other, pos in zip(self._toasts, self._positions()):
                other.slide_to(pos)

    def visible_messages(self) -> list[str]:
        out = []
        for t in self._toasts:
            for lbl in t.findChildren(QLabel):
                if lbl.text():
                    out.append(lbl.text())
        return out

    def eventFilter(self, obj, ev) -> bool:  # noqa: N802
        if obj is self.widget and ev.type() == QEvent.Type.Resize and self._toasts:
            for t, pos in zip(self._toasts, self._positions()):
                t.move(pos)
        return False
