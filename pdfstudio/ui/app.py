"""QApplication setup, settings, recent files and ``run()``."""

from __future__ import annotations

import argparse
import os
import sys

from PySide6.QtCore import QSettings, Qt, QTimer
from PySide6.QtGui import QFont, QGuiApplication
from PySide6.QtWidgets import QApplication

from .. import APP_NAME, APP_ORG
from .theme import tokens as T

RECENT_KEY = "recent/files"
RECENT_MAX = 15
_configured_dir: str | None = None


# ------------------------------------------------------------------ settings
def configure_settings(directory: str | None) -> None:
    """Store preferences as an INI file in ``directory`` (tests, portable mode)."""
    global _configured_dir
    _configured_dir = directory
    if directory:
        os.makedirs(directory, exist_ok=True)


def settings() -> QSettings:
    d = _configured_dir or os.environ.get("FOLIO_SETTINGS_DIR")
    if d:
        os.makedirs(d, exist_ok=True)
        return QSettings(os.path.join(d, "folio.ini"), QSettings.Format.IniFormat)
    return QSettings(APP_ORG, APP_NAME)


def recent_files() -> list[str]:
    val = settings().value(RECENT_KEY, [])
    if isinstance(val, str):
        val = [val] if val else []
    return [str(v) for v in (val or [])]


def add_recent(path: str) -> None:
    path = os.path.abspath(path)
    files = [f for f in recent_files() if os.path.normcase(f) != os.path.normcase(path)]
    files.insert(0, path)
    settings().setValue(RECENT_KEY, files[:RECENT_MAX])


def remove_recent(path: str) -> None:
    files = [f for f in recent_files() if os.path.normcase(f) != os.path.normcase(os.path.abspath(path))]
    settings().setValue(RECENT_KEY, files)


def clear_recent() -> None:
    settings().setValue(RECENT_KEY, [])


def last_dir(path: str | None = None) -> str:
    if path:
        return os.path.dirname(path)
    d = settings().value("paths/last_dir", "")
    if d and os.path.isdir(d):
        return d
    docs = os.path.join(os.path.expanduser("~"), "Documents")
    return docs if os.path.isdir(docs) else os.path.expanduser("~")


def set_last_dir(directory: str) -> None:
    settings().setValue("paths/last_dir", directory)


# ------------------------------------------------------------------- app
def app_font() -> QFont:
    f = QFont()
    f.setFamilies(T.FONT_FAMILIES)
    f.setPixelSize(T.FONT_SIZE)
    f.setHintingPreference(QFont.HintingPreference.PreferNoHinting if sys.platform != "win32"
                           else QFont.HintingPreference.PreferDefaultHinting)
    return f


def create_app(argv: list[str] | None = None, *, theme: str | None = None) -> QApplication:
    """Create (or reuse) the QApplication with Folio's style and theme."""
    app = QApplication.instance()
    if app is None:
        try:
            QGuiApplication.setHighDpiScaleFactorRoundingPolicy(
                Qt.HighDpiScaleFactorRoundingPolicy.PassThrough)
        except Exception:  # pragma: no cover
            pass
        app = QApplication(argv if argv is not None else sys.argv[:1])
    app.setApplicationName(APP_NAME)
    app.setOrganizationName(APP_ORG)
    app.setApplicationDisplayName(APP_NAME)
    app.setFont(app_font())
    if sys.platform == "win32":
        try:
            import ctypes

            ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("Folio.PDF")
        except Exception:
            pass
    from .theme import manager

    tm = manager.install(app, settings())
    if theme in ("light", "dark", "system"):
        tm.set_mode(theme, persist=False)
    return app


def parse_args(argv: list[str]) -> argparse.Namespace:
    p = argparse.ArgumentParser(prog="folio", description="Folio PDF – offline PDF editor")
    p.add_argument("files", nargs="*", help="PDF- eller billedfiler der skal åbnes")
    p.add_argument("--screenshot", metavar="PATH", help="render the window offscreen to PATH and quit")
    p.add_argument("--size", metavar="WxH", help="window size, e.g. 1440x900")
    p.add_argument("--theme", choices=("light", "dark", "system"), help="override the theme")
    p.add_argument("--mode", choices=("view", "edit", "annotate", "pages"), help=argparse.SUPPRESS)
    p.add_argument("--palette", action="store_true", help=argparse.SUPPRESS)
    p.add_argument("--search", metavar="TEXT", help=argparse.SUPPRESS)
    p.add_argument("--zoom", type=float, help=argparse.SUPPRESS)
    p.add_argument("--page", type=int, help=argparse.SUPPRESS)
    p.add_argument("--toast", metavar="TEXT", help=argparse.SUPPRESS)
    return p.parse_args(argv)


def run(argv: list[str] | None = None) -> int:
    args = parse_args(sys.argv[1:] if argv is None else argv)
    app = create_app(sys.argv[:1], theme=args.theme)
    from .main_window import MainWindow

    win = MainWindow()
    if args.size:
        try:
            w, h = (int(v) for v in args.size.lower().split("x"))
            win.resize(w, h)
        except ValueError:
            win.resize(1360, 860)
    else:
        geo = settings().value("window/geometry")
        if geo is not None and not args.screenshot:
            win.restoreGeometry(geo)
        else:
            win.resize(1360, 860)
    win.show()
    if args.files:
        win.open_files([os.path.abspath(f) for f in args.files])
    _apply_debug_state(win, args)
    if args.screenshot:
        def shoot() -> None:
            app.processEvents()
            ws = win.current_workspace()
            if ws is not None:
                ws.view.render_visible_now()
            app.processEvents()
            win.grab().save(args.screenshot)
            app.quit()

        QTimer.singleShot(1100, shoot)
    return app.exec()


def _apply_debug_state(win, args) -> None:
    """Hidden switches used by the visual self-check screenshots."""
    ws = win.current_workspace()
    if ws is not None:
        if args.mode:
            win.set_mode(args.mode)
        if args.zoom:
            ws.view.set_zoom(args.zoom)
        if args.page:
            QTimer.singleShot(50, lambda: ws.view.go_to_page(args.page - 1))
        if args.search:
            ws.show_search()
            ws.search.set_query(args.search)
    if args.palette:
        QTimer.singleShot(200, win.palette.open)
    if args.toast:
        QTimer.singleShot(300, lambda: win.toast(args.toast, "success", action_text="Fortryd", on_action=lambda: None))
