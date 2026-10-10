"""Theme switching: light / dark / follow the OS (persisted in QSettings)."""

from __future__ import annotations

import sys

from PySide6.QtCore import QObject, QSettings, Qt, Signal
from PySide6.QtGui import QGuiApplication
from PySide6.QtWidgets import QApplication, QProxyStyle, QStyle, QStyleFactory, QStyleOption

from . import icons
from . import tokens as T
from .qss import build_palette, build_qss

MODES = ("system", "light", "dark")
_SETTINGS_KEY = "appearance/theme"


class FolioStyle(QProxyStyle):
    """Fusion with a few refinements (rounded translucent menus, metrics)."""

    def __init__(self) -> None:
        super().__init__(QStyleFactory.create("Fusion"))

    def polish(self, arg):  # noqa: D401 - Qt override with overloads
        from PySide6.QtWidgets import QMenu, QWidget

        if isinstance(arg, QMenu):
            arg.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
            arg.setWindowFlag(Qt.WindowType.FramelessWindowHint, True)
            arg.setWindowFlag(Qt.WindowType.NoDropShadowWindowHint, True)
        if isinstance(arg, QWidget):
            return super().polish(arg)
        return super().polish(arg)

    # -- token-painted primitives (no image files needed) ------------------
    def drawPrimitive(self, element, option, painter, widget=None):  # noqa: N802
        PE = QStyle.PrimitiveElement
        if element in (PE.PE_IndicatorCheckBox, PE.PE_IndicatorItemViewItemCheck):
            self._draw_check(option, painter, widget)
            return
        if element == PE.PE_IndicatorRadioButton:
            self._draw_radio(option, painter)
            return
        arrows = {PE.PE_IndicatorArrowDown: "down", PE.PE_IndicatorArrowUp: "up",
                  PE.PE_IndicatorArrowLeft: "left", PE.PE_IndicatorArrowRight: "right",
                  PE.PE_IndicatorSpinDown: "down", PE.PE_IndicatorSpinUp: "up",
                  PE.PE_IndicatorSpinMinus: "down", PE.PE_IndicatorSpinPlus: "up"}
        if element in arrows:
            self._draw_chevron(option, painter, arrows[element])
            return
        super().drawPrimitive(element, option, painter, widget)

    # -- input fields: QComboBox / QSpinBox painted from tokens ------------
    FIELD_H = 30

    def drawComplexControl(self, control, option, painter, widget=None):  # noqa: N802
        CC = QStyle.ComplexControl
        SC = QStyle.SubControl
        S = QStyle.StateFlag
        if control == CC.CC_ComboBox:
            if option.subControls & SC.SC_ComboBoxFrame:
                self._draw_field(option, painter)
            r = self.subControlRect(control, option, SC.SC_ComboBoxArrow, widget)
            o = QStyleOption()
            o.rect = r
            o.state = option.state
            self._draw_chevron(o, painter, "down")
            return
        if control == CC.CC_SpinBox:
            if option.subControls & SC.SC_SpinBoxFrame and option.frame:
                self._draw_field(option, painter)
            from PySide6.QtCore import QRectF
            from PySide6.QtGui import QPainter, QPainterPath

            for sc, direction in ((SC.SC_SpinBoxUp, "up"), (SC.SC_SpinBoxDown, "down")):
                if not option.subControls & sc:
                    continue
                r = self.subControlRect(control, option, sc, widget)
                if option.activeSubControls & sc and option.state & S.State_MouseOver:
                    painter.save()
                    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
                    path = QPainterPath()
                    path.addRoundedRect(QRectF(r).adjusted(1, 1, -1, -1), 4, 4)
                    painter.fillPath(path, T.qcolor("hover"))
                    painter.restore()
                o = QStyleOption()
                o.rect = r
                o.state = option.state
                self._draw_chevron(o, painter, direction, small=True)
            return
        super().drawComplexControl(control, option, painter, widget)

    def _draw_field(self, option, p) -> None:
        from PySide6.QtCore import QRectF
        from PySide6.QtGui import QPainter, QPainterPath, QPen

        S = QStyle.StateFlag
        enabled = bool(option.state & S.State_Enabled)
        focus = bool(option.state & S.State_HasFocus)
        hover = bool(option.state & S.State_MouseOver)
        r = QRectF(option.rect).adjusted(0.5, 0.5, -0.5, -0.5)
        path = QPainterPath()
        path.addRoundedRect(r, 7, 7)
        p.save()
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.fillPath(path, T.qcolor("surface_sunken" if enabled else "bg_app"))
        if not enabled:
            color = T.qcolor("border")
        elif focus:
            color = T.qcolor("accent")
        elif hover:
            color = T.qcolor("text_subtle")
        else:
            color = T.qcolor("border_strong")
        p.setPen(QPen(color, 1))
        p.drawPath(path)
        p.restore()

    def subControlRect(self, control, option, sc, widget=None):  # noqa: N802
        from PySide6.QtCore import QRect

        CC = QStyle.ComplexControl
        SC = QStyle.SubControl
        r = option.rect
        if control == CC.CC_ComboBox:
            if sc == SC.SC_ComboBoxArrow:
                return QRect(r.right() - 26, r.top(), 24, r.height())
            if sc == SC.SC_ComboBoxEditField:
                return QRect(r.left() + 10, r.top() + 1, r.width() - 38, r.height() - 2)
            if sc == SC.SC_ComboBoxFrame:
                return r
        if control == CC.CC_SpinBox:
            bw = 20
            if sc == SC.SC_SpinBoxUp:
                return QRect(r.right() - bw - 2, r.top() + 2, bw, r.height() // 2 - 2)
            if sc == SC.SC_SpinBoxDown:
                return QRect(r.right() - bw - 2, r.top() + r.height() // 2, bw, r.height() - r.height() // 2 - 2)
            if sc == SC.SC_SpinBoxEditField:
                return QRect(r.left() + 9, r.top() + 1, r.width() - bw - 13, r.height() - 2)
            if sc == SC.SC_SpinBoxFrame:
                return r
        return super().subControlRect(control, option, sc, widget)

    def sizeFromContents(self, ct, option, size, widget=None):  # noqa: N802
        CT = QStyle.ContentsType
        out = super().sizeFromContents(ct, option, size, widget)
        if ct in (CT.CT_ComboBox, CT.CT_SpinBox):
            out.setHeight(max(out.height(), self.FIELD_H))
            out.setWidth(out.width() + 12)
        return out

    @staticmethod
    def _state(option, flag) -> bool:
        return bool(option.state & flag)

    def _draw_check(self, option, p, widget=None) -> None:
        from PySide6.QtCore import QPointF, QRectF
        from PySide6.QtGui import QPainter, QPainterPath, QPen

        S = QStyle.StateFlag
        enabled = self._state(option, S.State_Enabled)
        on = self._state(option, S.State_On)
        partial = self._state(option, S.State_NoChange)
        hover = self._state(option, S.State_MouseOver)
        r = QRectF(option.rect)
        side = min(r.width(), r.height(), 16.0)
        box = QRectF(0, 0, side, side)
        box.moveCenter(r.center())
        box = box.adjusted(0.5, 0.5, -0.5, -0.5)
        p.save()
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        path = QPainterPath()
        path.addRoundedRect(box, 4, 4)
        if on or partial:
            p.fillPath(path, T.qcolor("accent" if enabled else "text_subtle"))
            pen = QPen(T.qcolor("text_on_accent"), 1.8)
            pen.setCapStyle(Qt.PenCapStyle.RoundCap)
            pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
            p.setPen(pen)
            c = box.center()
            k = box.width() / 16.0
            if partial:
                p.drawLine(QPointF(c.x() - 4 * k, c.y()), QPointF(c.x() + 4 * k, c.y()))
            else:
                tick = QPainterPath()
                tick.moveTo(c.x() - 4 * k, c.y() + 0.2 * k)
                tick.lineTo(c.x() - 1.2 * k, c.y() + 3 * k)
                tick.lineTo(c.x() + 4.2 * k, c.y() - 3.2 * k)
                p.drawPath(tick)
        else:
            p.fillPath(path, T.qcolor("surface_sunken"))
            p.setPen(QPen(T.qcolor("accent" if hover and enabled else "border_strong"), 1.2))
            p.drawPath(path)
        p.restore()

    def _draw_radio(self, option, p) -> None:
        from PySide6.QtCore import QRectF
        from PySide6.QtGui import QPainter, QPen

        S = QStyle.StateFlag
        enabled = self._state(option, S.State_Enabled)
        on = self._state(option, S.State_On)
        hover = self._state(option, S.State_MouseOver)
        r = QRectF(option.rect)
        side = min(r.width(), r.height(), 16.0)
        box = QRectF(0, 0, side, side)
        box.moveCenter(r.center())
        box = box.adjusted(0.5, 0.5, -0.5, -0.5)
        p.save()
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        if on:
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(T.qcolor("accent" if enabled else "text_subtle"))
            p.drawEllipse(box)
            p.setBrush(T.qcolor("text_on_accent"))
            d = box.width() * 0.38
            inner = QRectF(0, 0, d, d)
            inner.moveCenter(box.center())
            p.drawEllipse(inner)
        else:
            p.setBrush(T.qcolor("surface_sunken"))
            p.setPen(QPen(T.qcolor("accent" if hover and enabled else "border_strong"), 1.2))
            p.drawEllipse(box)
        p.restore()

    def _draw_chevron(self, option, p, direction: str, small: bool = False) -> None:
        from PySide6.QtCore import QPointF
        from PySide6.QtGui import QPainter, QPainterPath, QPen

        enabled = self._state(option, QStyle.StateFlag.State_Enabled)
        r = option.rect
        c = QPointF(r.center()) + QPointF(0.5, 0.5)
        s = max(2.5, min(r.width(), r.height()) / 4.0)
        s = min(s, 2.6 if small else 4.0)
        pts = {
            "down": [(-s, -s / 2), (0, s / 2), (s, -s / 2)],
            "up": [(-s, s / 2), (0, -s / 2), (s, s / 2)],
            "left": [(s / 2, -s), (-s / 2, 0), (s / 2, s)],
            "right": [(-s / 2, -s), (s / 2, 0), (-s / 2, s)],
        }[direction]
        path = QPainterPath()
        path.moveTo(c + QPointF(*pts[0]))
        for pt in pts[1:]:
            path.lineTo(c + QPointF(*pt))
        p.save()
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        pen = QPen(T.qcolor("text_muted" if enabled else "text_subtle"), 1.5)
        pen.setCapStyle(Qt.PenCapStyle.RoundCap)
        pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
        p.setPen(pen)
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.drawPath(path)
        p.restore()

    def pixelMetric(self, metric, option=None, widget=None):
        PM = QStyle.PixelMetric
        if metric == PM.PM_SmallIconSize:
            return 16
        if metric == PM.PM_ToolBarIconSize:
            return 18
        if metric in (PM.PM_MenuHMargin, PM.PM_MenuVMargin):
            return 0
        if metric in (PM.PM_IndicatorWidth, PM.PM_IndicatorHeight,
                      PM.PM_ExclusiveIndicatorWidth, PM.PM_ExclusiveIndicatorHeight):
            return 16
        return super().pixelMetric(metric, option, widget)

    def styleHint(self, hint, option=None, widget=None, returnData=None):
        SH = QStyle.StyleHint
        if hint == SH.SH_UnderlineShortcut:
            return 0
        if hint == SH.SH_ToolTip_WakeUpDelay:
            return 450
        if hint == SH.SH_Menu_SubMenuPopupDelay:
            return 120
        return super().styleHint(hint, option, widget, returnData)


class ThemeManager(QObject):
    changed = Signal(object)  # Tokens

    def __init__(self, app: QApplication, settings: QSettings | None = None) -> None:
        super().__init__(app)
        self._app = app
        self._settings = settings
        mode = settings.value(_SETTINGS_KEY, "system") if settings is not None else "system"
        self._mode = mode if mode in MODES else "system"
        hints = QGuiApplication.styleHints()
        try:
            hints.colorSchemeChanged.connect(self._on_system_changed)
        except Exception:  # pragma: no cover - older Qt
            pass

    # -- queries ---------------------------------------------------------
    def mode(self) -> str:
        return self._mode

    def system_scheme(self) -> str:
        try:
            scheme = QGuiApplication.styleHints().colorScheme()
            return "dark" if scheme == Qt.ColorScheme.Dark else "light"
        except Exception:  # pragma: no cover
            return "light"

    def effective(self) -> str:
        return self.system_scheme() if self._mode == "system" else self._mode

    @property
    def tokens(self) -> T.Tokens:
        return T.current()

    # -- changes ---------------------------------------------------------
    def set_mode(self, mode: str, persist: bool = True) -> None:
        if mode not in MODES:
            raise ValueError(mode)
        self._mode = mode
        if persist and self._settings is not None:
            self._settings.setValue(_SETTINGS_KEY, mode)
        self.apply()

    def toggle(self) -> None:
        self.set_mode("light" if self.effective() == "dark" else "dark")

    def _on_system_changed(self, *_args) -> None:
        if self._mode == "system":
            self.apply()

    def apply(self) -> None:
        tokens = T.set_current(self.effective())
        icons.clear_cache()
        if not isinstance(self._app.style(), FolioStyle):
            self._app.setStyle(FolioStyle())
        self._app.setPalette(build_palette(tokens))
        self._app.setStyleSheet(build_qss(tokens))
        self.changed.emit(tokens)
        for w in self._app.topLevelWidgets():
            apply_title_bar(w, tokens.is_dark)


_instance: ThemeManager | None = None


def install(app: QApplication, settings: QSettings | None = None) -> ThemeManager:
    global _instance
    if _instance is None or _instance.parent() is not app:
        _instance = ThemeManager(app, settings)
    _instance.apply()
    return _instance


def theme_manager() -> ThemeManager:
    global _instance
    if _instance is None:
        app = QApplication.instance()
        _instance = ThemeManager(app)
        _instance.apply()
    return _instance


def apply_title_bar(widget, dark: bool) -> None:
    """Dark/light native title bar on Windows 10/11 (no-op elsewhere)."""
    if sys.platform != "win32":
        return
    try:
        if not widget.isWindow() or not widget.testAttribute(Qt.WidgetAttribute.WA_WState_Created):
            return
        import ctypes
        from ctypes import wintypes

        hwnd = wintypes.HWND(int(widget.winId()))
        value = ctypes.c_int(1 if dark else 0)
        dwm = ctypes.windll.dwmapi
        for attr in (20, 19):  # DWMWA_USE_IMMERSIVE_DARK_MODE (new / pre-20H1)
            if dwm.DwmSetWindowAttribute(hwnd, attr, ctypes.byref(value), ctypes.sizeof(value)) == 0:
                break
    except Exception:
        pass
