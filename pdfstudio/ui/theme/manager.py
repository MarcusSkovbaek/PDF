"""Theme switching: light / dark / follow the OS (persisted in QSettings)."""

from __future__ import annotations

import sys

from PySide6.QtCore import QObject, QSettings, Qt, Signal
from PySide6.QtGui import QGuiApplication
from PySide6.QtWidgets import QApplication, QProxyStyle, QStyle, QStyleFactory

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

    def pixelMetric(self, metric, option=None, widget=None):
        PM = QStyle.PixelMetric
        if metric == PM.PM_SmallIconSize:
            return 16
        if metric == PM.PM_ToolBarIconSize:
            return 18
        if metric in (PM.PM_MenuHMargin, PM.PM_MenuVMargin):
            return 0
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
