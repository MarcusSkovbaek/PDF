"""Status-bar controls: zoom (− / % / + / fit) and page navigation (x / n)."""

from __future__ import annotations

from PySide6.QtCore import QPoint, QSize, Qt, Signal
from PySide6.QtGui import QIntValidator
from PySide6.QtWidgets import QHBoxLayout, QLabel, QLineEdit, QMenu, QWidget

from ...i18n import tr
from ..theme.icons import icon
from .util import IconButton, divider

ZOOM_PRESETS = (0.5, 0.75, 1.0, 1.25, 1.5, 2.0, 3.0, 4.0)


class ZoomControl(QWidget):
    zoomRequested = Signal(float)
    zoomInRequested = Signal()
    zoomOutRequested = Signal()
    fitWidthRequested = Signal()
    fitPageRequested = Signal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        lay = QHBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(2)
        self.btn_out = IconButton("minus", tr("shell.zoom.out"), variant="icon-sm", icon_size=15, shortcut="Ctrl+-")
        self.btn_in = IconButton("plus", tr("shell.zoom.in"), variant="icon-sm", icon_size=15, shortcut="Ctrl+=")
        self.field = QLineEdit("100 %")
        self.field.setProperty("variant", "bare")
        self.field.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.field.setFixedWidth(62)
        self.field.setFixedHeight(26)
        self.field.setToolTip(tr("shell.zoom.field_tip"))
        self.field.returnPressed.connect(self._commit)
        self.btn_menu = IconButton("caret-down", tr("shell.zoom.presets"), variant="icon-sm", icon_size=12)
        self.btn_menu.clicked.connect(self._show_menu)
        self.btn_fit_w = IconButton("arrows-out-line-horizontal", tr("shell.zoom.fit_width"), variant="icon-sm",
                                    icon_size=15, checkable=True)
        self.btn_fit_p = IconButton("frame-corners", tr("shell.zoom.fit_page"), variant="icon-sm", icon_size=15,
                                    checkable=True, shortcut="Ctrl+0")
        self.btn_out.clicked.connect(self.zoomOutRequested)
        self.btn_in.clicked.connect(self.zoomInRequested)
        self.btn_fit_w.clicked.connect(lambda: self.fitWidthRequested.emit())
        self.btn_fit_p.clicked.connect(lambda: self.fitPageRequested.emit())
        for w in (self.btn_out, self.field, self.btn_menu, self.btn_in):
            lay.addWidget(w)
        lay.addSpacing(6)
        lay.addWidget(divider("v"))
        lay.addSpacing(6)
        lay.addWidget(self.btn_fit_w)
        lay.addWidget(self.btn_fit_p)

    def set_zoom(self, zoom: float, fit_mode: str | None = None) -> None:
        if not self.field.hasFocus():
            self.field.setText(f"{round(zoom * 100)} %")
        self.btn_fit_w.setChecked(fit_mode == "width")
        self.btn_fit_p.setChecked(fit_mode == "page")

    def _commit(self) -> None:
        txt = self.field.text().replace("%", "").replace(",", ".").strip()
        try:
            val = float(txt) / 100.0
        except ValueError:
            return
        self.field.clearFocus()
        self.zoomRequested.emit(val)

    def _show_menu(self) -> None:
        m = QMenu(self)
        for z in ZOOM_PRESETS:
            a = m.addAction(f"{round(z * 100)} %")
            a.triggered.connect(lambda _=False, z=z: self.zoomRequested.emit(z))
        m.addSeparator()
        m.addAction(icon("arrows-out-line-horizontal"), tr("shell.zoom.fit_width"), self.fitWidthRequested.emit)
        m.addAction(icon("frame-corners"), tr("shell.zoom.fit_page"), self.fitPageRequested.emit)
        m.popup(self.btn_menu.mapToGlobal(QPoint(0, -m.sizeHint().height() - 6)))


class PageNavigator(QWidget):
    pageRequested = Signal(int)  # 0-based
    prevRequested = Signal()
    nextRequested = Signal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        lay = QHBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(2)
        self.btn_prev = IconButton("caret-up", tr("shell.nav.prev"), variant="icon-sm", icon_size=14, shortcut="PgUp")
        self.btn_next = IconButton("caret-down", tr("shell.nav.next"), variant="icon-sm", icon_size=14, shortcut="PgDown")
        self.field = QLineEdit("1")
        self.field.setProperty("variant", "bare")
        self.field.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        self.field.setFixedHeight(26)
        self.field.setFixedWidth(40)
        self.field.setToolTip(tr("shell.nav.field_tip"))
        self._validator = QIntValidator(1, 1, self)
        self.field.setValidator(self._validator)
        self.field.returnPressed.connect(self._commit)
        self.total = QLabel("/ 1")
        self.total.setProperty("role", "muted")
        self.btn_prev.clicked.connect(self.prevRequested)
        self.btn_next.clicked.connect(self.nextRequested)
        lay.addWidget(self.btn_prev)
        lay.addWidget(self.btn_next)
        lay.addSpacing(4)
        lay.addWidget(self.field)
        lay.addSpacing(2)
        lay.addWidget(self.total)

    def set_state(self, current: int, count: int) -> None:
        self._validator.setTop(max(1, count))
        if not self.field.hasFocus():
            self.field.setText(str(current + 1))
        self.total.setText(f"/ {count}")
        self.btn_prev.setEnabled(current > 0)
        self.btn_next.setEnabled(current < count - 1)

    def _commit(self) -> None:
        try:
            n = int(self.field.text())
        except ValueError:
            return
        self.field.clearFocus()
        self.pageRequested.emit(n - 1)

    def sizeHint(self) -> QSize:  # noqa: N802
        return super().sizeHint()
