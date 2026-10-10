"""Main window: header with document tabs, mode bar, workspaces, status bar.

Extension points for other features:

* **Tools** – drop a module in ``pdfstudio/ui/viewer/tools/`` (auto-imported).
* **Commands** – drop a module in ``pdfstudio/ui/commands/`` exposing
  ``register(window)``; register actions via ``window.actions.register(...)``
  (``menu=`` / ``mode=`` place them in menus / floating mode toolbars).
* Useful window API: ``current_workspace()``, ``current_pdf()``,
  ``add_document(pdf)``, ``open_path(path)``, ``toast(text, kind)``,
  ``refresh_actions()``.
"""

from __future__ import annotations

import os

from PySide6.QtCore import QEvent, QRectF, QSize, Qt, QTimer, QUrl
from PySide6.QtGui import QGuiApplication, QKeySequence, QPainter, QPainterPath, QPen
from PySide6.QtWidgets import (
    QApplication,
    QFileDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QMenu,
    QProgressDialog,
    QStackedWidget,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from .. import APP_NAME, __version__
from ..core import DocumentError, InvalidPassword, PasswordRequired, PdfDocument
from ..i18n import tr
from .actions import ActionRegistry
from .home_view import HomeView
from .theme import tokens as T
from .theme.icons import icon
from .theme.manager import apply_title_bar, theme_manager
from .viewer.tools.base import ToolRegistry
from .widgets.brand import BrandMark, app_icon
from .widgets.command_palette import CommandPalette
from .widgets.doc_tabs import DocTabBar
from .widgets.segmented import Segment, SegmentedControl
from .widgets.toast import ToastHost
from .widgets.util import IconButton, ThemedIconLabel, divider, label, on_theme_change, set_prop
from .widgets.zoom_control import PageNavigator, ZoomControl
from .workspace import DocumentWorkspace

PDF_EXT = (".pdf",)
MODES = (("view", "eye"), ("edit", "pencil-simple"), ("annotate", "chat-circle-text"), ("pages", "squares-four"))


def _is_image(path: str) -> bool:
    try:
        from ..core.images import is_image_file

        return is_image_file(path)
    except Exception:
        return os.path.splitext(path)[1].lower() in (".png", ".jpg", ".jpeg", ".bmp", ".gif", ".tif", ".tiff")


class DropOverlay(QWidget):
    def __init__(self, parent: QWidget) -> None:
        super().__init__(parent)
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        self.hide()

    def paintEvent(self, _ev) -> None:  # noqa: N802
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.fillRect(self.rect(), T.qcolor("scrim"))
        r = QRectF(self.rect()).adjusted(16, 16, -16, -16)
        path = QPainterPath()
        path.addRoundedRect(r, 18, 18)
        p.fillPath(path, T.qcolor("accent_soft"))
        pen = QPen(T.qcolor("accent"), 2)
        pen.setDashPattern([6, 4])
        p.setPen(pen)
        p.drawPath(path)
        card = QRectF(0, 0, 380, 132)
        card.moveCenter(r.center())
        cp = QPainterPath()
        cp.addRoundedRect(card, 16, 16)
        p.fillPath(cp, T.qcolor("surface_raised"))
        p.setPen(QPen(T.qcolor("border"), 1))
        p.drawPath(cp)
        ic = icon("upload-simple", "accent_text")
        ic.paint(p, int(card.center().x() - 16), int(card.top() + 20), 32, 32)
        f = p.font()
        f.setPixelSize(16)
        f.setWeight(f.Weight.DemiBold)
        p.setFont(f)
        p.setPen(T.qcolor("text"))
        p.drawText(QRectF(card.left(), card.top() + 60, card.width(), 24), Qt.AlignmentFlag.AlignCenter,
                   tr("shell.drop.title"))
        f.setPixelSize(12)
        f.setWeight(f.Weight.Normal)
        p.setFont(f)
        p.setPen(T.qcolor("text_muted"))
        p.drawText(QRectF(card.left(), card.top() + 88, card.width(), 20), Qt.AlignmentFlag.AlignCenter,
                   tr("shell.drop.text"))
        p.end()


class MainWindow(QMainWindow):
    def __init__(self, settings=None) -> None:
        super().__init__()
        from .app import settings as app_settings

        self.settings = settings or app_settings()
        self.setWindowTitle(APP_NAME)
        self.setWindowIcon(app_icon())
        self.setAcceptDrops(True)
        self.setMinimumSize(880, 560)
        self._ws_counter = 0
        self._widgets: dict[str, QWidget] = {}
        self._sidebar_on = self.settings.value("view/sidebar", True, type=bool)
        self._inspector_on = self.settings.value("view/inspector", True, type=bool)

        self.actions = ActionRegistry(self)
        self.theme = theme_manager()

        root = QWidget()
        root.setObjectName("AppRoot")
        root.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setCentralWidget(root)
        v = QVBoxLayout(root)
        v.setContentsMargins(0, 0, 0, 0)
        v.setSpacing(0)
        self.header = self._build_header()
        v.addWidget(self.header)
        self.modebar = self._build_modebar()
        v.addWidget(self.modebar)
        self.stack = QStackedWidget()
        self.stack.setObjectName("AppStack")
        v.addWidget(self.stack, 1)
        self.statusbar = self._build_statusbar()
        v.addWidget(self.statusbar)

        self.toasts = ToastHost(root, bottom_margin=T.STATUSBAR_H + 20)
        self.palette = CommandPalette(root, self.actions)
        self.drop_overlay = DropOverlay(root)
        root.installEventFilter(self)

        self._register_actions()
        self._register_tool_actions()
        self._discover_commands()
        self._build_menu()
        self.actions.changed.connect(self._schedule_menu_rebuild)
        self._menu_timer = QTimer(self)
        self._menu_timer.setSingleShot(True)
        self._menu_timer.setInterval(0)
        self._menu_timer.timeout.connect(self._build_menu)

        self.theme.changed.connect(self._on_theme)
        self.show_home()
        self._update_chrome()

    # ================================================================ build
    def _build_header(self) -> QWidget:
        w = QWidget()
        w.setObjectName("Header")
        w.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        w.setFixedHeight(T.HEADER_H)
        h = QHBoxLayout(w)
        h.setContentsMargins(10, 0, 10, 0)
        h.setSpacing(4)
        self.menu_btn = QToolButton()
        self.menu_btn.setProperty("variant", "pill")
        self.menu_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.menu_btn.setToolTip(tr("shell.menu.tip"))
        self.menu_btn.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        mb = QHBoxLayout(self.menu_btn)
        mb.setContentsMargins(8, 0, 8, 0)
        mb.setSpacing(8)
        mark = BrandMark(22)
        mark.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        name = QLabel(APP_NAME.split()[0])
        name.setProperty("role", "heading")
        name.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        caret = ThemedIconLabel("caret-down", 12, "text_subtle")
        caret.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        mb.addWidget(mark)
        mb.addWidget(name)
        mb.addWidget(caret)
        self.menu_btn.setFixedSize(QSize(104, 34))
        h.addWidget(self.menu_btn)
        h.addSpacing(6)
        h.addWidget(divider("v"), 0, Qt.AlignmentFlag.AlignVCenter)
        h.addSpacing(6)

        self.tabs = DocTabBar()
        self.tabs.currentChanged.connect(self._on_tab_changed)
        self.tabs.closeRequested.connect(self.close_tab)
        h.addWidget(self.tabs, 0, Qt.AlignmentFlag.AlignVCenter)
        self.new_tab_btn = IconButton("plus", tr("shell.tab.new"), variant="icon", icon_size=16)
        self.new_tab_btn.clicked.connect(self.show_home)
        h.addWidget(self.new_tab_btn)
        h.addStretch(1)

        self.badge = QFrame()
        self.badge.setObjectName("Badge")
        bl = QHBoxLayout(self.badge)
        bl.setContentsMargins(8, 3, 10, 3)
        bl.setSpacing(6)
        bl.addWidget(ThemedIconLabel("shield-check", 14, "success"))
        bt = QLabel(tr("app.offline_badge"))
        bt.setObjectName("BadgeText")
        bl.addWidget(bt)
        self.badge.setToolTip(tr("shell.offline.tip"))
        h.addWidget(self.badge, 0, Qt.AlignmentFlag.AlignVCenter)
        h.addSpacing(10)

        self.quick: dict[str, IconButton] = {}
        groups = [("file.open", "file.save"), ("edit.undo", "edit.redo"), ("view.palette", "view.toggle_theme")]
        icons = {"file.open": "folder-open", "file.save": "floppy-disk", "edit.undo": "arrow-counter-clockwise",
                 "edit.redo": "arrow-clockwise", "view.palette": "command", "view.toggle_theme": "moon"}
        for gi, group in enumerate(groups):
            if gi:
                h.addSpacing(4)
                h.addWidget(divider("v"), 0, Qt.AlignmentFlag.AlignVCenter)
                h.addSpacing(4)
            for aid in group:
                b = IconButton(icons[aid], "", variant="icon", icon_size=18)
                b.setAccessibleName(aid)
                b.clicked.connect(lambda _=False, a=aid: self.actions.trigger(a))
                h.addWidget(b)
                self.quick[aid] = b
        for d in w.findChildren(QFrame):
            if d.property("divider") == "v":
                d.setFixedHeight(20)
        return w

    def _build_modebar(self) -> QWidget:
        w = QWidget()
        w.setObjectName("ModeBar")
        w.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        w.setFixedHeight(T.MODEBAR_H)
        h = QHBoxLayout(w)
        h.setContentsMargins(10, 0, 10, 0)
        self.btn_sidebar = IconButton("layout", tr("shell.view.sidebar"),
                                      variant="icon", checkable=True, icon_size=18)
        self.btn_sidebar.clicked.connect(lambda: self.actions.trigger("view.toggle_sidebar"))
        h.addWidget(self.btn_sidebar)
        self.doc_title = label("", "heading")
        self.doc_sub = label("", "caption")
        h.addSpacing(6)
        h.addWidget(self.doc_title)
        h.addSpacing(2)
        h.addWidget(self.doc_sub)
        h.addStretch(1)
        self.modes = SegmentedControl([Segment(k, tr(f"shell.mode.{k}"), ic, tr(f"shell.mode.{k}.tip"))
                                       for k, ic in MODES])
        self.modes.currentChanged.connect(self._on_mode)
        h.addWidget(self.modes)
        h.addStretch(1)
        self.btn_inspector = IconButton("sliders-horizontal", tr("shell.view.inspector"), variant="icon",
                                        checkable=True, icon_size=18)
        self.btn_inspector.clicked.connect(lambda: self.actions.trigger("view.toggle_inspector"))
        h.addWidget(self.btn_inspector)
        return w

    def _build_statusbar(self) -> QWidget:
        w = QWidget()
        w.setObjectName("StatusBar")
        w.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        w.setFixedHeight(T.STATUSBAR_H)
        h = QHBoxLayout(w)
        h.setContentsMargins(10, 0, 10, 0)
        h.setSpacing(8)
        self.page_nav = PageNavigator()
        self.page_nav.pageRequested.connect(lambda i: self._with_ws(lambda ws: ws.view.go_to_page(i)))
        self.page_nav.prevRequested.connect(lambda: self.actions.trigger("nav.prev_page"))
        self.page_nav.nextRequested.connect(lambda: self.actions.trigger("nav.next_page"))
        h.addWidget(self.page_nav)
        h.addSpacing(4)
        h.addWidget(divider("v"))
        h.addSpacing(4)
        self.hint_icon = ThemedIconLabel("cursor", 14, "text_subtle")
        self.hint = label("", "caption")
        h.addWidget(self.hint_icon)
        h.addWidget(self.hint, 1)
        self.zoom = ZoomControl()
        self.zoom.zoomRequested.connect(lambda z: self._with_ws(lambda ws: ws.view.set_zoom(z, animate=True)))
        self.zoom.zoomInRequested.connect(lambda: self.actions.trigger("view.zoom_in"))
        self.zoom.zoomOutRequested.connect(lambda: self.actions.trigger("view.zoom_out"))
        self.zoom.fitWidthRequested.connect(lambda: self.actions.trigger("view.fit_width"))
        self.zoom.fitPageRequested.connect(lambda: self.actions.trigger("view.fit_page"))
        h.addWidget(self.zoom)
        for d in w.findChildren(QFrame):
            if d.property("divider") == "v":
                d.setFixedHeight(16)
        return w

    # ============================================================ actions
    def _register_actions(self) -> None:
        r = self.actions.register
        has_ws = lambda: self.current_workspace() is not None  # noqa: E731
        cat_file, cat_edit, cat_view, cat_pages, cat_nav, cat_tools = (
            tr("shell.cat.file"), tr("shell.cat.edit"), tr("shell.cat.view"), tr("shell.cat.pages"),
            tr("shell.cat.nav"), tr("shell.cat.tools"))

        # -- file
        r("file.new", tr("shell.file.new"), icon="file-plus", shortcut="Ctrl+N", handler=self.new_document,
          category=cat_file, order=10)
        r("file.open", tr("shell.file.open"), icon="folder-open", shortcut=QKeySequence.StandardKey.Open,
          handler=self.open_dialog, category=cat_file, order=20)
        r("file.save", tr("shell.file.save"), icon="floppy-disk", shortcut="Ctrl+S",
          handler=lambda: self.save(), enabled=has_ws, category=cat_file, order=30)
        r("file.save_as", tr("shell.file.save_as"), icon="floppy-disk-back", shortcut="Ctrl+Shift+S",
          handler=lambda: self.save_as(), enabled=has_ws, category=cat_file, order=40)
        r("file.print", tr("shell.file.print"), icon="printer", shortcut="Ctrl+P", handler=self.print_document,
          enabled=has_ws, category=cat_file, order=50)
        r("file.export_images", tr("shell.file.export_images"), icon="image", handler=self.export_images,
          enabled=has_ws, category=cat_file, order=60)
        r("file.close_tab", tr("shell.file.close_tab"), icon="x", shortcut="Ctrl+W",
          handler=lambda: self.close_tab(self.tabs.currentIndex()),
          enabled=lambda: self.tabs.count() > 1 or self.current_workspace() is not None, category=cat_file, order=70)
        r("file.quit", tr("shell.file.quit"), icon="sign-out", shortcut="Ctrl+Q", handler=self.close,
          category=cat_file, order=90)

        # -- edit
        r("edit.undo", tr("shell.edit.undo"), icon="arrow-counter-clockwise", shortcut="Ctrl+Z",
          handler=self.undo, enabled=lambda: bool(self.current_pdf() and self.current_pdf().history.can_undo),
          category=cat_edit, order=10)
        r("edit.redo", tr("shell.edit.redo"), icon="arrow-clockwise", shortcut=["Ctrl+Y", "Ctrl+Shift+Z"],
          handler=self.redo, enabled=lambda: bool(self.current_pdf() and self.current_pdf().history.can_redo),
          category=cat_edit, order=20)
        r("edit.copy", tr("shell.edit.copy"), icon="copy", shortcut="Ctrl+C",
          handler=lambda: self._with_ws(lambda ws: ws.copy()),
          enabled=lambda: bool(self.current_workspace() and self.current_workspace().can_copy()),
          category=cat_edit, order=30)
        r("edit.select_all", tr("shell.edit.select_all"), icon="selection-all", shortcut="Ctrl+A",
          handler=lambda: self._with_ws(lambda ws: ws.select_all()), enabled=has_ws, category=cat_edit, order=40)
        r("edit.find", tr("shell.edit.find"), icon="magnifying-glass", shortcut="Ctrl+F",
          handler=lambda: self._with_ws(lambda ws: ws.show_search()), enabled=has_ws, category=cat_edit, order=50)

        # -- view
        r("view.palette", tr("shell.view.palette"), icon="command", shortcut="Ctrl+K", handler=self.palette.open,
          category=cat_view, order=5, palette=False)
        r("view.zoom_in", tr("shell.zoom.in"), icon="magnifying-glass-plus", shortcut=["Ctrl+=", "Ctrl++"],
          handler=lambda: self._with_ws(lambda ws: ws.view.zoom_in()), enabled=has_ws, category=cat_view, order=10)
        r("view.zoom_out", tr("shell.zoom.out"), icon="magnifying-glass-minus", shortcut="Ctrl+-",
          handler=lambda: self._with_ws(lambda ws: ws.view.zoom_out()), enabled=has_ws, category=cat_view, order=20)
        r("view.zoom_100", tr("shell.zoom.actual"), icon="magnifying-glass", shortcut="Ctrl+1",
          handler=lambda: self._with_ws(lambda ws: ws.view.set_zoom(1.0, animate=True)), enabled=has_ws,
          category=cat_view, order=30)
        r("view.fit_width", tr("shell.zoom.fit_width"), icon="arrows-out-line-horizontal", shortcut="Ctrl+2",
          handler=lambda: self._with_ws(lambda ws: ws.view.fit_width()), enabled=has_ws, category=cat_view, order=40)
        r("view.fit_page", tr("shell.zoom.fit_page"), icon="frame-corners", shortcut="Ctrl+0",
          handler=lambda: self._with_ws(lambda ws: ws.view.fit_page()), enabled=has_ws, category=cat_view, order=50)
        r("view.toggle_sidebar", tr("shell.view.sidebar"), icon="layout", shortcut="Ctrl+\\",
          handler=self.toggle_sidebar, checkable=True, checked=lambda: self._sidebar_on, category=cat_view, order=60)
        r("view.toggle_inspector", tr("shell.view.inspector"), icon="sliders-horizontal", shortcut="Ctrl+Alt+\\",
          handler=self.toggle_inspector, checkable=True, checked=lambda: self._inspector_on, category=cat_view,
          order=70)
        r("view.toggle_theme", tr("shell.view.theme"), icon="circle-half", shortcut="Ctrl+Shift+L",
          handler=self.theme.toggle, category=cat_view, order=80)
        r("view.theme_system", tr("shell.view.theme_system"), icon="monitor", checkable=True,
          checked=lambda: self.theme.mode() == "system",
          handler=lambda on: self.theme.set_mode("system" if on else self.theme.effective()),
          category=cat_view, order=85)
        r("view.fullscreen", tr("shell.view.fullscreen"), icon="corners-out", shortcut="F11",
          handler=self.toggle_fullscreen, category=cat_view, order=90)
        r("view.next_tab", tr("shell.view.next_tab"), icon="arrow-right", shortcut="Ctrl+Tab",
          handler=lambda: self._cycle_tab(1), category=cat_view, order=95)
        r("view.prev_tab", tr("shell.view.prev_tab"), icon="arrow-left", shortcut="Ctrl+Shift+Tab",
          handler=lambda: self._cycle_tab(-1), category=cat_view, order=96)
        for n, (key, ic) in enumerate(MODES, start=1):
            r(f"mode.{key}", tr("shell.mode.switch", mode=tr(f"shell.mode.{key}")), icon=ic, shortcut=f"Alt+{n}",
              handler=lambda k=key: self.set_mode(k), enabled=has_ws, category=cat_view, order=100 + n)

        # -- navigation
        r("nav.next_page", tr("shell.nav.next"), icon="caret-down", shortcut="PgDown",
          handler=lambda: self._with_ws(lambda ws: ws.view.next_page()), enabled=has_ws, category=cat_nav, order=10)
        r("nav.prev_page", tr("shell.nav.prev"), icon="caret-up", shortcut="PgUp",
          handler=lambda: self._with_ws(lambda ws: ws.view.prev_page()), enabled=has_ws, category=cat_nav, order=20)
        r("nav.first_page", tr("shell.nav.first"), icon="caret-double-up", shortcut="Home",
          handler=lambda: self._with_ws(lambda ws: ws.view.go_to_page(0)), enabled=has_ws, category=cat_nav,
          order=30)
        r("nav.last_page", tr("shell.nav.last"), icon="caret-double-down", shortcut="End",
          handler=lambda: self._with_ws(lambda ws: ws.view.go_to_page(ws.pdf.page_count - 1)), enabled=has_ws,
          category=cat_nav, order=40)
        r("nav.goto", tr("shell.nav.goto"), icon="hash", shortcut="Ctrl+G", handler=self._focus_page_field,
          enabled=has_ws, category=cat_nav, order=50)

        # -- pages (also shown in the "Sider" mode toolbar)
        pw = lambda fn: (lambda: self._with_ws(fn))  # noqa: E731
        r("pages.rotate_left", tr("shell.pages.rotate_left"), icon="arrow-counter-clockwise", shortcut="Ctrl+[",
          handler=pw(lambda ws: ws.rotate_selected(-90)), enabled=has_ws, menu="pages", mode="pages",
          category=cat_pages, order=10)
        r("pages.rotate_right", tr("shell.pages.rotate_right"), icon="arrow-clockwise", shortcut="Ctrl+]",
          handler=pw(lambda ws: ws.rotate_selected(90)), enabled=has_ws, menu="pages", mode="pages",
          category=cat_pages, order=20)
        r("pages.duplicate", tr("shell.pages.duplicate"), icon="copy", shortcut="Ctrl+D",
          handler=pw(lambda ws: ws.duplicate_selected()), enabled=has_ws, menu="pages", mode="pages",
          category=cat_pages, order=30)
        r("pages.extract", tr("shell.pages.extract"), icon="export", shortcut="Ctrl+Shift+E",
          handler=pw(lambda ws: ws.extract_selected()), enabled=has_ws, menu="pages", mode="pages",
          category=cat_pages, order=40)
        r("pages.insert_blank", tr("shell.pages.insert_blank"), icon="file-plus",
          handler=pw(lambda ws: ws.insert_blank()), enabled=has_ws, menu="pages", mode="pages",
          category=cat_pages, order=50)
        r("pages.insert_file", tr("shell.pages.insert_file"), icon="file-arrow-down", shortcut="Ctrl+Shift+I",
          handler=pw(lambda ws: ws.insert_from_file_dialog()), enabled=has_ws, menu="pages", mode="pages",
          category=cat_pages, order=60)
        r("pages.delete", tr("shell.pages.delete"), icon="trash",
          handler=pw(lambda ws: ws.delete_selected()),
          enabled=lambda: bool(self.current_pdf() and self.current_pdf().page_count > 1),
          menu="pages", mode="pages", category=cat_pages, order=70)

        # -- tools (dialogs from other modules, imported lazily)
        r("tools.merge", tr("shell.tools.merge"), icon="stack", handler=self.open_merge_dialog,
          menu="tools", category=cat_tools, order=10)
        r("tools.images_to_pdf", tr("shell.tools.images"), icon="image", handler=self.open_images_dialog,
          menu="tools", category=cat_tools, order=20)
        r("tools.split", tr("shell.tools.split"), icon="scissors", handler=self.open_split_dialog,
          enabled=has_ws, menu="tools", mode="pages", category=cat_tools, order=80)
        r("help.about", tr("shell.help.about"), icon="info", handler=self.show_about, category=tr("shell.cat.help"),
          order=10)

    def _register_tool_actions(self) -> None:
        for cls in ToolRegistry.all():
            self.actions.register(
                f"tool.{cls.id}", tr("shell.tool.use", tool=cls.display_label()), icon=cls.icon or None,
                shortcut=cls.shortcut, handler=lambda tid=cls.id: self.activate_tool(tid),
                enabled=lambda: self.current_workspace() is not None, category=tr("shell.cat.tool"),
                order=200 + cls.order)

    def _discover_commands(self) -> None:
        try:
            from . import commands

            commands.discover(self)
        except Exception as exc:  # pragma: no cover
            import sys

            print(f"[folio] command discovery failed: {exc}", file=sys.stderr)

    def _schedule_menu_rebuild(self) -> None:
        if hasattr(self, "_menu_timer"):
            self._menu_timer.start()

    def _build_menu(self) -> None:
        a = self.actions.action
        menu = QMenu(self)
        menu.setObjectName("MainMenu")

        def add(m: QMenu, ids):
            for aid in ids:
                if aid is None:
                    m.addSeparator()
                elif self.actions.has(aid):
                    m.addAction(a(aid))

        def extras(m: QMenu, key: str, known: set[str]):
            more = [e for e in self.actions.for_menu(key) if e.id not in known]
            if more:
                m.addSeparator()
                for e in more:
                    m.addAction(e.action)

        palette_act = menu.addAction(icon("command"), tr("shell.view.palette"))
        palette_act.setShortcut(QKeySequence("Ctrl+K"))
        palette_act.setShortcutContext(Qt.ShortcutContext.WidgetShortcut)
        palette_act.triggered.connect(self.palette.open)
        menu.addSeparator()

        m_file = menu.addMenu(tr("shell.menu.file"))
        add(m_file, ["file.new", "file.open"])
        self.recent_menu = m_file.addMenu(icon("clock-counter-clockwise"), tr("shell.file.recent"))
        self.recent_menu.aboutToShow.connect(self._fill_recent_menu)
        add(m_file, [None, "file.save", "file.save_as", None, "file.export_images", "file.print", None,
                     "file.close_tab"])
        extras(m_file, "file", set())
        add(m_file, [None, "file.quit"])

        m_edit = menu.addMenu(tr("shell.menu.edit"))
        add(m_edit, ["edit.undo", "edit.redo", None, "edit.copy", "edit.select_all", None, "edit.find"])
        extras(m_edit, "edit", set())

        m_view = menu.addMenu(tr("shell.menu.view"))
        add(m_view, ["view.zoom_in", "view.zoom_out", "view.zoom_100", "view.fit_width", "view.fit_page", None,
                     "mode.view", "mode.edit", "mode.annotate", "mode.pages", None,
                     "view.toggle_sidebar", "view.toggle_inspector", None,
                     "view.toggle_theme", "view.theme_system", None, "view.fullscreen"])
        extras(m_view, "view", set())

        m_nav = m_view.addMenu(tr("shell.menu.nav"))
        add(m_nav, ["nav.first_page", "nav.prev_page", "nav.next_page", "nav.last_page", None, "nav.goto"])

        m_pages = menu.addMenu(tr("shell.menu.pages"))
        for e in self.actions.for_menu("pages"):
            if e.id == "pages.delete":
                m_pages.addSeparator()
            m_pages.addAction(e.action)

        m_tools = menu.addMenu(tr("shell.menu.tools"))
        for e in self.actions.for_menu("tools"):
            m_tools.addAction(e.action)

        m_help = menu.addMenu(tr("shell.menu.help"))
        add(m_help, ["help.about"])
        extras(m_help, "help", {"help.about"})
        menu.addSeparator()
        add(menu, ["file.quit"])
        old = self.menu_btn.menu()
        self.menu_btn.setMenu(menu)
        self.main_menu = menu
        if old is not None:
            old.deleteLater()

    def _fill_recent_menu(self) -> None:
        from .app import recent_files

        m = self.recent_menu
        m.clear()
        files = [f for f in recent_files() if os.path.isfile(f)]
        if not files:
            act = m.addAction(tr("shell.home.recent_empty"))
            act.setEnabled(False)
            return
        for f in files[:12]:
            act = m.addAction(icon("file-pdf"), os.path.basename(f))
            act.setToolTip(f)
            act.triggered.connect(lambda _=False, p=f: self.open_path(p))
        m.addSeparator()
        m.addAction(tr("shell.home.recent_clear"), self._clear_recent)

    # ======================================================== workspaces
    def current_widget(self) -> QWidget | None:
        i = self.tabs.currentIndex()
        if i < 0:
            return None
        return self._widgets.get(self.tabs.tabData(i))

    def current_workspace(self) -> DocumentWorkspace | None:
        w = self.current_widget()
        return w if isinstance(w, DocumentWorkspace) else None

    def current_pdf(self):
        ws = self.current_workspace()
        return ws.pdf if ws else None

    def workspaces(self) -> list[DocumentWorkspace]:
        out = []
        for i in range(self.tabs.count()):
            w = self._widgets.get(self.tabs.tabData(i))
            if isinstance(w, DocumentWorkspace):
                out.append(w)
        return out

    def _with_ws(self, fn):
        ws = self.current_workspace()
        if ws is not None:
            return fn(ws)
        return None

    def _home_key(self) -> str | None:
        for k, w in self._widgets.items():
            if isinstance(w, HomeView):
                return k
        return None

    def show_home(self) -> None:
        key = self._home_key()
        if key is None:
            from .app import recent_files

            key = "home"
            home = HomeView(recent_files)
            home.actionRequested.connect(lambda aid: self.actions.trigger(aid))
            home.openRequested.connect(self.open_path)
            home.clearRecentRequested.connect(self._clear_recent)
            self._widgets[key] = home
            self.stack.addWidget(home)
            self.tabs.add_tab(icon("house", "text_muted"), tr("shell.tab.home"), key, closable=True)
        else:
            self._widgets[key].refresh_recent()
        self.tabs.setCurrentIndex(self.tabs.index_of(key))
        self._on_tab_changed(self.tabs.currentIndex())

    def home_view(self) -> HomeView | None:
        k = self._home_key()
        return self._widgets.get(k) if k else None

    def add_document(self, pdf) -> DocumentWorkspace:
        self._ws_counter += 1
        key = f"doc:{self._ws_counter}"
        ws = DocumentWorkspace(pdf, window=self)
        ws.view.setAcceptDrops(False)
        ws.view.viewport().setAcceptDrops(False)
        ws.set_sidebar_visible(self._sidebar_on)
        ws.set_inspector_visible(self._inspector_on)
        self._widgets[key] = ws
        self.stack.addWidget(ws)
        ws.titleChanged.connect(lambda w=ws: self._update_tab(w))
        ws.stateChanged.connect(lambda w=ws: self._ws_state_changed(w))
        ws.toolChanged.connect(lambda _t, w=ws: self._ws_state_changed(w))
        ws.view.currentPageChanged.connect(lambda _p, w=ws: self._ws_state_changed(w))
        ws.view.zoomChanged.connect(lambda _z, w=ws: self._ws_state_changed(w))
        ws.view.fitModeChanged.connect(lambda _m, w=ws: self._ws_state_changed(w))
        home_key = self._home_key()
        replace_home = home_key is not None and self.tabs.tabData(self.tabs.currentIndex()) == home_key
        insert_at = self.tabs.currentIndex() + 1
        i = self.tabs.add_tab(icon("file-pdf", "text_muted"), pdf.display_name, key)
        if i != insert_at and insert_at <= self.tabs.count() - 1:
            self.tabs.moveTab(i, insert_at)
            i = insert_at
        self.tabs.setCurrentIndex(i)
        if replace_home:
            self._remove_tab(self.tabs.index_of(home_key))
        self._update_tab(ws)
        self._on_tab_changed(self.tabs.currentIndex())
        ws.view.setFocus()
        return ws

    def _update_tab(self, ws: DocumentWorkspace) -> None:
        for i in range(self.tabs.count()):
            if self._widgets.get(self.tabs.tabData(i)) is ws:
                self.tabs.setTabText(i, ws.pdf.display_name)
                self.tabs.setTabToolTip(i, ws.pdf.path or ws.pdf.display_name)
                self.tabs.set_dirty(i, ws.pdf.is_dirty)
        if ws is self.current_workspace():
            self._update_chrome()

    def _ws_state_changed(self, ws: DocumentWorkspace) -> None:
        if ws is self.current_workspace():
            self._update_chrome()

    def _on_tab_changed(self, i: int) -> None:
        w = self.current_widget()
        if w is not None:
            self.stack.setCurrentWidget(w)
        if isinstance(w, HomeView):
            w.refresh_recent()
        self._update_chrome()

    def _remove_tab(self, i: int) -> None:
        if i < 0:
            return
        key = self.tabs.tabData(i)
        w = self._widgets.pop(key, None)
        self.tabs.removeTab(i)
        if w is not None:
            self.stack.removeWidget(w)
            if isinstance(w, DocumentWorkspace):
                w.close_document()
            w.deleteLater()

    def close_tab(self, i: int) -> bool:
        if i < 0 or i >= self.tabs.count():
            return False
        w = self._widgets.get(self.tabs.tabData(i))
        if isinstance(w, DocumentWorkspace) and w.pdf.is_dirty:
            self.tabs.setCurrentIndex(i)
            if not self._resolve_dirty(w):
                return False
        if isinstance(w, HomeView) and self.tabs.count() == 1:
            return False
        self._remove_tab(i)
        if self.tabs.count() == 0:
            self.show_home()
        self._update_chrome()
        return True

    def _resolve_dirty(self, ws: DocumentWorkspace) -> bool:
        from .dialogs.common import ask_save_changes

        choice = ask_save_changes(self, ws.pdf.display_name)
        if choice == "save":
            return self.save(ws)
        return choice == "discard"

    def _cycle_tab(self, step: int) -> None:
        n = self.tabs.count()
        if n > 1:
            self.tabs.setCurrentIndex((self.tabs.currentIndex() + step) % n)

    # ============================================================ opening
    def open_dialog(self) -> None:
        from .app import last_dir

        files, _ = QFileDialog.getOpenFileNames(self, tr("shell.file.open_title"), last_dir(), tr("shell.filter.open"))
        if files:
            self.open_files(files)

    def open_files(self, files: list[str]) -> None:
        pdfs = [f for f in files if not _is_image(f)]
        images = [f for f in files if _is_image(f)]
        for f in pdfs:
            self.open_path(f)
        if len(images) == 1:
            self.open_path(images[0])
        elif images:
            self._images_to_document(images)

    def _images_to_document(self, images: list[str]) -> None:
        try:
            from ..core.images import images_to_pdf
        except Exception:
            for f in images:
                self.open_path(f)
            return
        try:
            data = images_to_pdf(images)
        except Exception as exc:  # noqa: BLE001
            self.toast(tr("shell.error.open", name=os.path.basename(images[0]), err=str(exc)), "error")
            return
        pdf = PdfDocument.from_bytes(data, title=tr("shell.images_title"))
        self.add_document(pdf)
        self.toast(tr("shell.images_done", n=len(images)), "success")

    def open_path(self, path: str, password: str | None = None) -> DocumentWorkspace | None:
        path = os.path.abspath(path)
        for ws in self.workspaces():
            if ws.pdf.path and os.path.normcase(ws.pdf.path) == os.path.normcase(path):
                self.tabs.setCurrentIndex(self.tabs.index_of(self._key_of(ws)))
                return ws
        if not os.path.isfile(path):
            self.toast(tr("shell.error.missing", name=os.path.basename(path)), "error")
            from .app import remove_recent

            remove_recent(path)
            hv = self.home_view()
            if hv:
                hv.refresh_recent()
            return None
        wrong = False
        while True:
            try:
                pdf = PdfDocument.open(path, password)
                break
            except PasswordRequired:
                pass
            except InvalidPassword:
                wrong = True
            except DocumentError as exc:
                self.toast(tr("shell.error.open", name=os.path.basename(path), err=str(exc)), "error")
                return None
            from .dialogs.password import ask_password

            password = ask_password(self, os.path.basename(path), wrong=wrong)
            if password is None:
                return None
        ws = self.add_document(pdf)
        if pdf.path:
            from .app import add_recent, set_last_dir

            add_recent(pdf.path)
            set_last_dir(os.path.dirname(pdf.path))
        return ws

    def _key_of(self, w: QWidget) -> str | None:
        for k, v in self._widgets.items():
            if v is w:
                return k
        return None

    def new_document(self) -> None:
        pdf = PdfDocument.new_blank(title=tr("app.untitled"))
        self.add_document(pdf)

    def _clear_recent(self) -> None:
        from .app import clear_recent

        clear_recent()
        hv = self.home_view()
        if hv:
            hv.refresh_recent()

    # ============================================================= saving
    def save(self, ws: DocumentWorkspace | None = None) -> bool:
        ws = ws or self.current_workspace()
        if ws is None:
            return False
        if not ws.pdf.path:
            return self.save_as(ws)
        return self._save_to(ws, ws.pdf.path)

    def save_as(self, ws: DocumentWorkspace | None = None, path: str | None = None) -> bool:
        ws = ws or self.current_workspace()
        if ws is None:
            return False
        if path is None:
            from .app import last_dir

            base = ws.pdf.path or os.path.join(last_dir(), ws.pdf.display_name)
            path, _ = QFileDialog.getSaveFileName(self, tr("shell.file.save_as_title"), base, tr("shell.filter.pdf"))
            if not path:
                return False
            if not path.lower().endswith(".pdf"):
                path += ".pdf"
        return self._save_to(ws, path)

    def _save_to(self, ws: DocumentWorkspace, path: str) -> bool:
        try:
            written = ws.pdf.save(path)
        except DocumentError as exc:
            from .dialogs.common import show_error

            show_error(self, tr("shell.error.save_title"), tr("shell.error.save", err=str(exc)))
            return False
        except Exception as exc:  # noqa: BLE001
            from .dialogs.common import show_error

            show_error(self, tr("shell.error.save_title"), tr("shell.error.save", err=str(exc)))
            return False
        from .app import add_recent, set_last_dir

        add_recent(written)
        set_last_dir(os.path.dirname(written))
        self.toast(tr("shell.saved", name=os.path.basename(written)), "success")
        self._update_tab(ws)
        return True

    # ============================================================ editing
    def undo(self) -> None:
        pdf = self.current_pdf()
        if pdf is not None and pdf.history.can_undo:
            pdf.undo()

    def redo(self) -> None:
        pdf = self.current_pdf()
        if pdf is not None and pdf.history.can_redo:
            pdf.redo()

    def activate_tool(self, tool_id: str) -> None:
        ws = self.current_workspace()
        if ws is None:
            return
        ws.set_tool(tool_id)
        self.modes.set_current_key(ws.mode, animate=True)

    def set_mode(self, mode: str) -> None:
        self.modes.set_current_key(mode, animate=True)
        self._on_mode(mode)

    def _on_mode(self, mode: str) -> None:
        ws = self.current_workspace()
        if ws is not None and ws.mode != mode:
            ws.set_mode(mode)
        self._update_chrome()

    # ============================================================== panels
    def toggle_sidebar(self, on: bool | None = None) -> None:
        self._sidebar_on = (not self._sidebar_on) if on is None else bool(on)
        self.settings.setValue("view/sidebar", self._sidebar_on)
        for ws in self.workspaces():
            ws.set_sidebar_visible(self._sidebar_on)
        self._update_chrome()

    def toggle_inspector(self, on: bool | None = None) -> None:
        self._inspector_on = (not self._inspector_on) if on is None else bool(on)
        self.settings.setValue("view/inspector", self._inspector_on)
        for ws in self.workspaces():
            ws.set_inspector_visible(self._inspector_on)
        self._update_chrome()

    def toggle_fullscreen(self) -> None:
        if self.isFullScreen():
            self.showNormal()
        else:
            self.showFullScreen()

    def _focus_page_field(self) -> None:
        self.page_nav.field.setFocus(Qt.FocusReason.ShortcutFocusReason)
        self.page_nav.field.selectAll()

    # ============================================================== chrome
    def refresh_actions(self) -> None:
        self.actions.refresh_enabled()

    def _update_chrome(self) -> None:
        ws = self.current_workspace()
        is_doc = ws is not None
        self.modebar.setVisible(is_doc)
        self.statusbar.setVisible(is_doc)
        pdf = ws.pdf if ws else None
        # undo/redo labels include the history label
        if pdf is not None and pdf.history.undo_label:
            self.actions.set_text("edit.undo", tr("shell.edit.undo_label", label=pdf.history.undo_label))
        else:
            self.actions.set_text("edit.undo", tr("shell.edit.undo"))
        if pdf is not None and pdf.history.redo_label:
            self.actions.set_text("edit.redo", tr("shell.edit.redo_label", label=pdf.history.redo_label))
        else:
            self.actions.set_text("edit.redo", tr("shell.edit.redo"))
        self.actions.refresh_enabled()
        for aid, b in self.quick.items():
            act = self.actions.action(aid)
            b.setEnabled(act.isEnabled())
            b.setToolTip(act.toolTip() if aid not in ("edit.undo", "edit.redo") else
                         f"{act.text()}  ({act.shortcut().toString(QKeySequence.SequenceFormat.NativeText)})")
        self.quick["view.toggle_theme"].set_icon_name("sun" if T.current().is_dark else "moon")
        self.btn_sidebar.setChecked(self._sidebar_on)
        self.btn_inspector.setChecked(self._inspector_on)
        if ws is None:
            self.setWindowTitle(APP_NAME)
            return
        self.modes.set_current_key(ws.mode, animate=self.isVisible())
        self.setWindowTitle(f"{'• ' if pdf.is_dirty else ''}{pdf.display_name} — {APP_NAME}")
        self.doc_title.setText("")
        self.doc_sub.setText("")
        self.page_nav.set_state(ws.view.current_page, pdf.page_count)
        self.zoom.set_zoom(ws.view.zoom, ws.view.fit_mode)
        tool = ws.tool
        if tool is not None:
            self.hint_icon.set_icon(tool.icon or "cursor")
            self.hint.setText(tool.hint or tool.display_label())
        else:
            self.hint_icon.set_icon("info")
            self.hint.setText(tr(f"shell.mode.{ws.mode}.empty"))

    def _on_theme(self, _tokens) -> None:
        apply_title_bar(self, T.current().is_dark)
        for i in range(self.tabs.count()):
            w = self._widgets.get(self.tabs.tabData(i))
            self.tabs.setTabIcon(i, icon("house" if isinstance(w, HomeView) else "file-pdf", "text_muted"))
        self._update_chrome()

    def toast(self, text: str, kind: str = "info", **kw):
        return self.toasts.show(text, kind, **kw)

    def showEvent(self, ev) -> None:  # noqa: N802
        super().showEvent(ev)
        apply_title_bar(self, T.current().is_dark)

    def eventFilter(self, obj, ev) -> bool:  # noqa: N802
        if obj is self.centralWidget() and ev.type() == QEvent.Type.Resize:
            self.drop_overlay.setGeometry(obj.rect())
        return False

    # ======================================================= drag & drop
    def _drop_files(self, md) -> list[str]:
        if not md.hasUrls():
            return []
        out = []
        for u in md.urls():
            if u.isLocalFile():
                p = u.toLocalFile()
                if os.path.isfile(p) and (p.lower().endswith(PDF_EXT) or _is_image(p)):
                    out.append(p)
        return out

    def dragEnterEvent(self, ev) -> None:  # noqa: N802
        if self._drop_files(ev.mimeData()):
            ev.acceptProposedAction()
            self.drop_overlay.setGeometry(self.centralWidget().rect())
            self.drop_overlay.show()
            self.drop_overlay.raise_()
        else:
            ev.ignore()

    def dragMoveEvent(self, ev) -> None:  # noqa: N802
        if self._drop_files(ev.mimeData()):
            ev.acceptProposedAction()

    def dragLeaveEvent(self, ev) -> None:  # noqa: N802
        self.drop_overlay.hide()

    def dropEvent(self, ev) -> None:  # noqa: N802
        self.drop_overlay.hide()
        files = self._drop_files(ev.mimeData())
        if files:
            ev.acceptProposedAction()
            QTimer.singleShot(0, lambda: self.open_files(files))

    # =========================================================== dialogs
    def open_merge_dialog(self) -> None:
        try:
            from .dialogs.merge_dialog import MergeDialog
        except ImportError:
            self.toast(tr("shell.soon"), "info")
            return
        dlg = MergeDialog(self)
        if dlg.exec() and getattr(dlg, "result_bytes", None):
            title = getattr(dlg, "result_title", None) or tr("shell.merged_title")
            self.add_document(PdfDocument.from_bytes(dlg.result_bytes, title=title))

    def open_images_dialog(self) -> None:
        try:
            from .dialogs.images_dialog import ImagesToPdfDialog
        except ImportError:
            self.toast(tr("shell.soon"), "info")
            return
        dlg = ImagesToPdfDialog(self)
        if dlg.exec() and getattr(dlg, "result_bytes", None):
            title = getattr(dlg, "result_title", None) or tr("shell.images_title")
            self.add_document(PdfDocument.from_bytes(dlg.result_bytes, title=title))

    def open_split_dialog(self) -> None:
        ws = self.current_workspace()
        if ws is None:
            return
        try:
            from .dialogs.split_dialog import SplitDialog
        except ImportError:
            self.toast(tr("shell.soon"), "info")
            return
        SplitDialog(self, ws.pdf).exec()

    def show_about(self) -> None:
        from .dialogs.common import FolioDialog

        dlg = FolioDialog(self, f"{APP_NAME} {__version__}", tr("shell.about.text"), icon_name="shield-check",
                          icon_role="success")
        dlg.add_button(tr("shell.ok"), "primary", "ok", default=True)
        dlg.exec()

    # ============================================================ output
    def print_document(self) -> None:
        ws = self.current_workspace()
        if ws is None:
            return
        try:
            from PySide6.QtPrintSupport import QPrintDialog, QPrinter
        except Exception:
            self.toast(tr("shell.print.unavailable"), "error")
            return
        printer = QPrinter(QPrinter.PrinterMode.HighResolution)
        printer.setDocName(ws.pdf.display_name)
        n = ws.pdf.page_count
        printer.setFromTo(1, n)
        dlg = QPrintDialog(printer, self)
        dlg.setWindowTitle(tr("shell.file.print"))
        dlg.setMinMax(1, n)
        if dlg.exec() != QPrintDialog.DialogCode.Accepted:
            return
        self._print_to(printer, ws.pdf)

    def _print_to(self, printer, pdf) -> None:
        from PySide6.QtCore import QRect

        from .viewer.render_util import render_qimage

        n = pdf.page_count
        first, last = printer.fromPage(), printer.toPage()
        if first <= 0 or last <= 0:
            first, last = 1, n
        pages = list(range(first - 1, min(last, n)))
        dpi = min(printer.resolution(), 300)
        painter = QPainter()
        if not painter.begin(printer):
            self.toast(tr("shell.print.failed"), "error")
            return
        progress = QProgressDialog(tr("shell.print.progress"), tr("shell.dirty.cancel"), 0, len(pages), self)
        progress.setWindowModality(Qt.WindowModality.WindowModal)
        progress.setMinimumDuration(400)
        try:
            for k, i in enumerate(pages):
                if progress.wasCanceled():
                    break
                if k:
                    printer.newPage()
                img = render_qimage(pdf.doc, i, dpi / 72.0)
                area = painter.viewport()
                landscape_page = img.width() > img.height()
                landscape_paper = area.width() > area.height()
                painter.save()
                if landscape_page != landscape_paper:
                    painter.translate(area.width(), 0)
                    painter.rotate(90)
                    area = QRect(0, 0, area.height(), area.width())
                s = min(area.width() / img.width(), area.height() / img.height())
                w, h = int(img.width() * s), int(img.height() * s)
                target = QRect((area.width() - w) // 2, (area.height() - h) // 2, w, h)
                painter.drawImage(target, img)
                painter.restore()
                progress.setValue(k + 1)
                QApplication.processEvents()
        finally:
            painter.end()
            progress.close()
        self.toast(tr("shell.print.sent", n=len(pages)), "success")

    def export_images(self, folder: str | None = None, dpi: int = 150) -> int:
        ws = self.current_workspace()
        if ws is None:
            return 0
        if folder is None:
            from .app import last_dir

            folder = QFileDialog.getExistingDirectory(self, tr("shell.export.title"), last_dir(ws.pdf.path))
            if not folder:
                return 0
        from .viewer.render_util import render_pixmap

        pdf = ws.pdf
        stem = os.path.splitext(pdf.display_name)[0]
        n = pdf.page_count
        digits = max(3, len(str(n)))
        progress = QProgressDialog(tr("shell.export.progress"), tr("shell.dirty.cancel"), 0, n, self)
        progress.setWindowModality(Qt.WindowModality.WindowModal)
        progress.setMinimumDuration(500)
        done = 0
        try:
            for i in range(n):
                if progress.wasCanceled():
                    break
                pix = render_pixmap(pdf.doc, i, dpi / 72.0)
                pix.save(os.path.join(folder, f"{stem}-{tr('shell.export.page_word')}-{i + 1:0{digits}d}.png"))
                done += 1
                progress.setValue(done)
                QApplication.processEvents()
        except Exception as exc:  # noqa: BLE001
            from .dialogs.common import show_error

            show_error(self, tr("shell.export.error_title"), str(exc))
        finally:
            progress.close()
        if done:
            self.toast(tr("shell.export.done", n=done, folder=os.path.basename(folder) or folder), "success")
        return done

    # ============================================================= close
    def closeEvent(self, ev) -> None:  # noqa: N802
        for ws in self.workspaces():
            if ws.pdf.is_dirty:
                self.tabs.setCurrentIndex(self.tabs.index_of(self._key_of(ws)))
                if not self._resolve_dirty(ws):
                    ev.ignore()
                    return
        self.settings.setValue("window/geometry", self.saveGeometry())
        for ws in self.workspaces():
            ws.close_document()
        ev.accept()
