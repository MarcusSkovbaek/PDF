"""DocumentWorkspace – everything belonging to one open document (one tab).

Layout::

    ┌ Sidebar ─────┬ Center ───────────────────────────┬ Inspector ┐
    │ [Sider|Bog.. │        ╭ floating tool pill ╮      │ Egenskaber│
    │ thumbnails   │         PageView / Organizer       │ tool props│
    └──────────────┴────────────────────────────────────┴───────────┘

The workspace owns the :class:`PdfDocument`, the :class:`PageView`, the side
panels and the active tool, and subscribes to ``pdf.listen`` to keep every
view in sync. Page operations used by the "Sider" actions live here too.
"""

from __future__ import annotations

import os

from PySide6.QtCore import QEvent, QSize, Qt, QTimer, Signal
from PySide6.QtWidgets import (
    QFileDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QScrollArea,
    QSlider,
    QSplitter,
    QStackedWidget,
    QTabBar,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from ..core import CONTENT, RELOADED, SAVED, STRUCTURE
from ..i18n import tr
from .theme import tokens as T
from .theme.icons import icon
from .viewer.page_view import PageView
from .viewer.tools.base import MODES, ToolContext, ToolRegistry
from .widgets.util import IconButton, ThemedIconLabel, add_shadow, divider, label, on_theme_change, set_prop

try:
    from ..core import pages as core_pages
except Exception:  # pragma: no cover - core module not available yet
    core_pages = None

MODE_DEFAULT_TOOL = {"view": "view.select"}


class FloatingToolbar(QFrame):
    """Centered pill with the current mode's tools and actions."""

    toolClicked = Signal(str)

    def __init__(self, parent: QWidget) -> None:
        super().__init__(parent)
        self.setObjectName("FloatingBar")
        self._lay = QHBoxLayout(self)
        self._lay.setContentsMargins(6, 6, 6, 6)
        self._lay.setSpacing(4)
        self._tool_buttons: dict[str, QToolButton] = {}
        add_shadow(self, blur=26, y=6, role="shadow")

    def build(self, tools, actions, active_id: str | None, empty_text: str = "") -> None:
        while self._lay.count():
            item = self._lay.takeAt(0)
            w = item.widget()
            if w is not None:
                w.deleteLater()
        self._tool_buttons = {}
        for cls in tools:
            b = IconButton(cls.icon or "circle", "", variant="tool", checkable=True,
                           on_role="text_on_accent", icon_size=19)
            tip = cls.display_label()
            if cls.shortcut:
                tip += f"  ({cls.shortcut})"
            b.setToolTip(tip)
            b.setChecked(cls.id == active_id)
            b.clicked.connect(lambda _=False, tid=cls.id: self.toolClicked.emit(tid))
            b.setAccessibleName(cls.id)
            self._lay.addWidget(b)
            self._tool_buttons[cls.id] = b
        if tools and actions:
            sep = divider("v")
            sep.setFixedHeight(22)
            self._lay.addSpacing(2)
            self._lay.addWidget(sep, 0, Qt.AlignmentFlag.AlignVCenter)
            self._lay.addSpacing(2)
        for entry in actions:
            b = IconButton(entry.icon or "circle", "", variant="tool", icon_size=19)
            b.setDefaultAction(entry.action)
            b.setIcon(icon(entry.icon or "circle"))
            b.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonIconOnly)
            b.setAccessibleName(entry.id)
            b.setToolTip(entry.action.toolTip())
            self._lay.addWidget(b)
        if not tools and not actions:
            lbl = label(empty_text, "muted")
            lbl.setContentsMargins(10, 0, 10, 0)
            lbl.setMinimumHeight(36)
            self._lay.addWidget(lbl)
        self.adjustSize()

    def set_active(self, tool_id: str | None) -> None:
        for tid, b in self._tool_buttons.items():
            b.setChecked(tid == tool_id)

    def tool_ids(self) -> list[str]:
        return list(self._tool_buttons)


class Organizer(QWidget):
    """Large page grid shown in "Sider" mode (drag to reorder)."""

    def __init__(self, workspace) -> None:
        super().__init__()
        from .panels.thumbnails import ThumbnailView

        self.setObjectName("Organizer")
        lay = QVBoxLayout(self)
        lay.setContentsMargins(24, 76, 24, 0)
        lay.setSpacing(10)
        self.title = label(tr("shell.pages.title"), "title")
        self.info = label("", "caption")
        self.slider = QSlider(Qt.Orientation.Horizontal)
        self.slider.setRange(90, 260)
        self.slider.setValue(150)
        self.slider.setFixedWidth(140)
        self.slider.setToolTip(tr("shell.pages.size"))
        small = ThemedIconLabel("squares-four", 16, "text_subtle")
        big = ThemedIconLabel("square", 16, "text_subtle")
        head = QHBoxLayout()
        head.setSpacing(10)
        head.addWidget(self.title)
        head.addWidget(self.info, 0, Qt.AlignmentFlag.AlignVCenter)
        head.addStretch(1)
        head.addWidget(small)
        head.addWidget(self.slider)
        head.addWidget(big)
        lay.addLayout(head)
        self.view = ThumbnailView(workspace, grid=True, thumb_width=150)
        lay.addWidget(self.view, 1)
        self.slider.valueChanged.connect(self.view.set_thumb_width)
        self.update_info(workspace.pdf.page_count)

    def update_info(self, n: int) -> None:
        self.info.setText(tr("shell.pages.info", n=n))

    def paintEvent(self, ev) -> None:  # noqa: N802
        from PySide6.QtGui import QPainter

        p = QPainter(self)
        p.fillRect(self.rect(), T.qcolor("canvas"))
        p.end()


class Inspector(QWidget):
    """Right panel: active tool's properties, else document information."""

    def __init__(self, workspace) -> None:
        super().__init__()
        self.workspace = workspace
        self.setObjectName("Inspector")
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(0)
        head = QWidget()
        head.setObjectName("PanelHeader")
        hl = QHBoxLayout(head)
        hl.setContentsMargins(16, 12, 10, 8)
        self.head_icon = ThemedIconLabel("sliders-horizontal", 16, "text_muted")
        self.head_title = label(tr("shell.inspector.title"), "heading")
        hl.addWidget(self.head_icon)
        hl.addSpacing(4)
        hl.addWidget(self.head_title, 1)
        lay.addWidget(head)
        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(True)
        self.scroll.setFrameShape(QFrame.Shape.NoFrame)
        self.scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        lay.addWidget(self.scroll, 1)
        self._tool_widget: QWidget | None = None
        self.doc_info = self._build_doc_info()
        self.scroll.setWidget(self.doc_info)

    def _build_doc_info(self) -> QWidget:
        w = QWidget()
        v = QVBoxLayout(w)
        v.setContentsMargins(16, 4, 16, 16)
        v.setSpacing(14)
        self.tool_card = QFrame()
        self.tool_card.setProperty("card", True)
        tc = QHBoxLayout(self.tool_card)
        tc.setContentsMargins(12, 10, 12, 10)
        tc.setSpacing(10)
        well = QFrame()
        well.setObjectName("IconWell")
        well.setFixedSize(32, 32)
        wl = QVBoxLayout(well)
        wl.setContentsMargins(0, 0, 0, 0)
        self.tool_icon = ThemedIconLabel("cursor", 18, "accent_text")
        wl.addWidget(self.tool_icon, 0, Qt.AlignmentFlag.AlignCenter)
        tc.addWidget(well, 0, Qt.AlignmentFlag.AlignTop)
        tv = QVBoxLayout()
        tv.setSpacing(2)
        self.tool_name = label("", "heading")
        self.tool_hint = label("", "caption", wrap=True)
        tv.addWidget(self.tool_name)
        tv.addWidget(self.tool_hint)
        tc.addLayout(tv, 1)
        v.addWidget(self.tool_card)

        v.addWidget(label(tr("shell.inspector.document"), "section"))
        self.rows: dict[str, QLabel] = {}
        grid = QVBoxLayout()
        grid.setSpacing(9)
        for key in ("name", "folder", "pages", "size", "filesize", "status"):
            row = QHBoxLayout()
            k = label(tr(f"shell.inspector.{key}"), "caption")
            k.setFixedWidth(78)
            val = QLabel("")
            val.setWordWrap(True)
            val.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
            row.addWidget(k, 0, Qt.AlignmentFlag.AlignTop)
            row.addWidget(val, 1)
            grid.addLayout(row)
            self.rows[key] = val
        v.addLayout(grid)
        v.addStretch(1)
        return w

    def set_tool(self, tool) -> None:
        if self._tool_widget is not None:
            self.scroll.takeWidget()
            self._tool_widget.setParent(None)
            self._tool_widget = None
        pw = tool.properties_widget() if tool is not None else None
        if pw is not None:
            self._tool_widget = pw
            self.doc_info.setParent(None)
            self.scroll.setWidget(pw)
            self.head_title.setText(tool.display_label())
        else:
            if self.scroll.widget() is not self.doc_info:
                self.scroll.takeWidget()
                self.scroll.setWidget(self.doc_info)
            self.head_title.setText(tr("shell.inspector.title"))
        if tool is not None:
            self.tool_icon.set_icon(tool.icon or "cursor")
            self.tool_name.setText(tool.display_label())
            self.tool_hint.setText(tool.hint or "")
        self.refresh_doc()

    def refresh_doc(self) -> None:
        ws = self.workspace
        pdf = ws.pdf
        self.rows["name"].setText(pdf.display_name)
        self.rows["folder"].setText(os.path.dirname(pdf.path) if pdf.path else tr("shell.inspector.not_saved"))
        self.rows["pages"].setText(str(pdf.page_count))
        try:
            w, h = ws.view.page_item(ws.view.current_page).w, ws.view.page_item(ws.view.current_page).h
            self.rows["size"].setText(_paper_name(w, h))
        except Exception:
            self.rows["size"].setText("–")
        size = ""
        if pdf.path and os.path.exists(pdf.path):
            size = _human_size(os.path.getsize(pdf.path))
        self.rows["filesize"].setText(size or "–")
        self.rows["status"].setText(tr("shell.inspector.dirty") if pdf.is_dirty else tr("shell.inspector.clean"))


def _human_size(n: int) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}".replace(".", ",")
        n /= 1024
    return str(n)


_PAPERS = {"A3": (842, 1191), "A4": (595, 842), "A5": (420, 595), "Letter": (612, 792), "Legal": (612, 1008)}


def _paper_name(w: float, h: float) -> str:
    mm = lambda v: round(v / 72 * 25.4)  # noqa: E731
    for name, (pw, ph) in _PAPERS.items():
        if (abs(w - pw) < 3 and abs(h - ph) < 3) or (abs(w - ph) < 3 and abs(h - pw) < 3):
            orient = tr("shell.inspector.portrait") if h >= w else tr("shell.inspector.landscape")
            return f"{name} · {orient}"
    return f"{mm(w)} × {mm(h)} mm"


class Sidebar(QWidget):
    def __init__(self, workspace) -> None:
        super().__init__()
        from .panels.outline import OutlinePanel
        from .panels.search_panel import SearchPanel
        from .panels.thumbnails import ThumbnailsPanel

        self.setObjectName("Sidebar")
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(0)
        head = QWidget()
        head.setObjectName("PanelHeader")
        hl = QHBoxLayout(head)
        hl.setContentsMargins(10, 10, 10, 6)
        self.tabs = QTabBar()
        self.tabs.setObjectName("SideTabs")
        self.tabs.setDrawBase(False)
        self.tabs.setExpanding(False)
        self.tabs.setIconSize(QSize(15, 15))
        self._tab_icons = ["squares-four", "bookmarks-simple", "magnifying-glass"]
        for name, key in zip(self._tab_icons, ("shell.side.pages", "shell.side.outline", "shell.side.search")):
            self.tabs.addTab(icon(name, "text_muted", selected_role="accent_text"), tr(key))
        hl.addWidget(self.tabs)
        hl.addStretch(1)
        lay.addWidget(head)
        self.stack = QStackedWidget()
        self.thumbs = ThumbnailsPanel(workspace)
        self.outline = OutlinePanel(workspace)
        self.search = SearchPanel(workspace)
        for w in (self.thumbs, self.outline, self.search):
            self.stack.addWidget(w)
        lay.addWidget(self.stack, 1)
        self.tabs.currentChanged.connect(self._on_tab)
        on_theme_change(self, self._refresh_icons)

    def _refresh_icons(self) -> None:
        for i, name in enumerate(self._tab_icons):
            self.tabs.setTabIcon(i, icon(name, "accent_text" if i == self.tabs.currentIndex() else "text_muted"))

    def _on_tab(self, i: int) -> None:
        self.stack.setCurrentIndex(i)
        self._refresh_icons()

    def show_tab(self, key: str) -> None:
        i = {"pages": 0, "outline": 1, "search": 2}[key]
        self.tabs.setCurrentIndex(i)


class DocumentWorkspace(QWidget):
    """One open document."""

    titleChanged = Signal()
    stateChanged = Signal()          # dirty/undo/redo/selection -> refresh actions
    toolChanged = Signal(str)
    modeChanged = Signal(str)

    def __init__(self, pdf, window=None, parent=None) -> None:
        super().__init__(parent)
        self.pdf = pdf
        self.window_ = window
        self.mode = "view"
        self._tools: dict[str, object] = {}
        self._mode_tool: dict[str, str] = {}
        self.tool = None

        self.view = PageView(pdf)
        self.view.set_tool(None, sink=self)

        lay = QHBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(0)
        self.splitter = QSplitter(Qt.Orientation.Horizontal)
        self.splitter.setHandleWidth(1)
        self.splitter.setChildrenCollapsible(False)
        lay.addWidget(self.splitter)

        self.sidebar = Sidebar(self)
        self.sidebar.setMinimumWidth(200)
        self.thumbs = self.sidebar.thumbs.view
        self.outline = self.sidebar.outline
        self.search = self.sidebar.search

        self.center = QWidget()
        self.center.setObjectName("Center")
        cl = QVBoxLayout(self.center)
        cl.setContentsMargins(0, 0, 0, 0)
        self.center_stack = QStackedWidget()
        cl.addWidget(self.center_stack)
        self.center_stack.addWidget(self.view)
        self.organizer: Organizer | None = None
        self.toolbar = FloatingToolbar(self.center)
        self.toolbar.toolClicked.connect(self.set_tool)
        self.center.installEventFilter(self)

        self.inspector = Inspector(self)
        self.inspector.setMinimumWidth(240)

        self.splitter.addWidget(self.sidebar)
        self.splitter.addWidget(self.center)
        self.splitter.addWidget(self.inspector)
        self.splitter.setStretchFactor(0, 0)
        self.splitter.setStretchFactor(1, 1)
        self.splitter.setStretchFactor(2, 0)
        self.splitter.setSizes([T.SIDEBAR_W, 900, T.INSPECTOR_W])

        # wiring
        self.thumbs.pageActivated.connect(self._thumb_activated)
        self.thumbs.selectionChangedPages.connect(lambda _p: self.stateChanged.emit())
        self.view.currentPageChanged.connect(self._on_current_page)
        self.view.pagesReset.connect(self._on_pages_reset)
        self._unlisten = pdf.listen(self._on_doc_event)

        self.set_mode("view")

    # ------------------------------------------------------------- helpers
    @property
    def window_actions(self):
        return getattr(self.window_, "actions", None)

    def action(self, aid: str):
        reg = self.window_actions
        if reg is not None and reg.has(aid):
            return reg.action(aid)
        return None

    def trigger_action(self, aid: str) -> None:
        reg = self.window_actions
        if reg is not None:
            reg.trigger(aid)

    def toast(self, text: str, kind: str = "info", **kw):
        fn = getattr(self.window_, "toast", None)
        if fn is not None:
            return fn(text, kind, **kw)
        return None

    @property
    def title(self) -> str:
        return self.pdf.display_name

    def close_document(self) -> None:
        try:
            self._unlisten()
        except Exception:
            pass
        if self.tool is not None:
            self.tool.deactivate()
        self.pdf.close()

    # ---------------------------------------------------------------- tools
    def tool_instance(self, tool_id: str):
        tool = self._tools.get(tool_id)
        if tool is None:
            cls = ToolRegistry.get(tool_id)
            if cls is None:
                return None
            tool = cls(self)
            tool.changed.connect(self._on_tool_changed)
            self._tools[tool_id] = tool
        return tool

    def set_tool(self, tool_id: str) -> None:
        cls = ToolRegistry.get(tool_id)
        if cls is None:
            return
        if self.mode not in cls.modes():
            self.set_mode(cls.modes()[0], tool_id=tool_id)
            return
        new = self.tool_instance(tool_id)
        if new is self.tool:
            return
        if self.tool is not None:
            self.tool.deactivate()
        self.tool = new
        ctx = ToolContext(pdf=self.pdf, view=self.view, workspace=self, toast=self.toast)
        new.activate(ctx)
        self.view.set_tool(new, sink=self)
        self._mode_tool[self.mode] = tool_id
        self.toolbar.set_active(tool_id)
        self.inspector.set_tool(new)
        self.toolChanged.emit(tool_id)
        self.stateChanged.emit()

    def _on_tool_changed(self) -> None:
        self.stateChanged.emit()

    def set_mode(self, mode: str, tool_id: str | None = None) -> None:
        if mode not in MODES:
            return
        self.mode = mode
        tools = ToolRegistry.for_mode(mode)
        if mode == "pages":
            if self.organizer is None:
                self.organizer = Organizer(self)
                self.organizer.view.pageActivated.connect(self._organizer_activated)
                self.organizer.view.selectionChangedPages.connect(lambda _p: self.stateChanged.emit())
                self.center_stack.addWidget(self.organizer)
            self.center_stack.setCurrentWidget(self.organizer)
            self.organizer.view.set_current_page(self.view.current_page)
        else:
            self.center_stack.setCurrentWidget(self.view)
        want = tool_id or self._mode_tool.get(mode) or MODE_DEFAULT_TOOL.get(mode)
        ids = [t.id for t in tools]
        if want not in ids:
            want = ids[0] if ids else None
        actions = self.window_actions.for_mode(mode) if self.window_actions is not None else []
        empty = tr(f"shell.mode.{mode}.empty")
        self.toolbar.build(tools, actions, want, empty)
        if want:
            if self.tool is not None and self.tool.id != want:
                self.tool.deactivate()
                self.tool = None
            self.set_tool(want)
        else:
            if self.tool is not None:
                self.tool.deactivate()
            self.tool = None
            # keep basic text selection available in modes without tools
            self.view.set_tool(None, sink=self)
            self.inspector.set_tool(None)
            self.toolChanged.emit("")
        self._position_toolbar()
        self.modeChanged.emit(mode)
        self.stateChanged.emit()

    def rebuild_toolbar(self) -> None:
        self.set_mode(self.mode, self.tool.id if self.tool else None)

    def eventFilter(self, obj, ev) -> bool:  # noqa: N802
        if obj is self.center and ev.type() in (QEvent.Type.Resize, QEvent.Type.Show, QEvent.Type.LayoutRequest):
            self._position_toolbar()
        return False

    def _position_toolbar(self) -> None:
        tb = self.toolbar
        tb.adjustSize()
        sh = tb.sizeHint()
        tb.resize(sh)
        x = max(8, (self.center.width() - sh.width()) // 2)
        tb.move(x, 14)
        tb.raise_()
        QTimer.singleShot(0, self._recenter_toolbar)

    def _recenter_toolbar(self) -> None:
        tb = self.toolbar
        sh = tb.sizeHint()
        tb.resize(sh)
        tb.move(max(8, (self.center.width() - sh.width()) // 2), 14)

    # --------------------------------------------------------------- panels
    def set_sidebar_visible(self, on: bool) -> None:
        self.sidebar.setVisible(on)

    def set_inspector_visible(self, on: bool) -> None:
        self.inspector.setVisible(on)

    def show_search(self) -> None:
        self.sidebar.setVisible(True)
        self.sidebar.show_tab("search")
        sel = self.tool.selected_text() if self.tool is not None and hasattr(self.tool, "selected_text") else ""
        if sel and "\n" not in sel and len(sel) < 80:
            self.search.field.setText(sel)
        self.search.focus_field()

    # -------------------------------------------------------------- events
    def _thumb_activated(self, page: int) -> None:
        self.view.go_to_page(page)

    def _organizer_activated(self, page: int) -> None:
        self.thumbs.set_current_page(page)
        self.view.go_to_page(page)

    def _on_current_page(self, page: int) -> None:
        self.thumbs.set_current_page(page)
        if self.organizer is not None:
            self.organizer.view.set_current_page(page)
        self.inspector.refresh_doc()
        self.stateChanged.emit()

    def _on_pages_reset(self) -> None:
        pass

    def _on_doc_event(self, ev) -> None:
        if ev.kind == CONTENT:
            self.view.refresh(ev.pages)
            self.thumbs.refresh_pages(ev.pages)
            if self.organizer is not None:
                self.organizer.view.refresh_pages(ev.pages)
        elif ev.kind in (STRUCTURE, RELOADED):
            self.view.rebuild(keep_pixmaps=ev.kind == RELOADED)
            self.view.render_visible_now()
            self.thumbs.reset_pages()
            self.thumbs.set_current_page(self.view.current_page)
            if self.organizer is not None:
                self.organizer.view.reset_pages()
                self.organizer.update_info(self.pdf.page_count)
            self.outline.reload()
        if ev.kind != SAVED:
            for tool in self._tools.values():
                try:
                    tool.document_changed(ev)
                except Exception:
                    pass
            self.search.document_changed()
        self.inspector.refresh_doc()
        self.titleChanged.emit()
        self.stateChanged.emit()

    # ------------------------------------------------------- clipboard etc.
    def can_copy(self) -> bool:
        return bool(self.tool is not None and self.tool.has_selection())

    def copy(self) -> bool:
        return bool(self.tool is not None and self.tool.copy())

    def select_all(self) -> bool:
        return bool(self.tool is not None and self.tool.select_all())

    # ------------------------------------------------------- page operations
    def selected_pages(self) -> list[int]:
        if self.mode == "pages" and self.organizer is not None:
            sel = self.organizer.view.selected_pages()
            if sel:
                return sel
        if self.sidebar.isVisible() and self.sidebar.stack.currentIndex() == 0:
            sel = self.thumbs.selected_pages()
            if len(sel) > 1 or (sel and self.thumbs.hasFocus()):
                return sel
        return [self.view.current_page] if self.pdf.page_count else []

    def _select_after(self, pages: list[int]) -> None:
        def apply():
            self.thumbs.select_pages(pages)
            if self.organizer is not None:
                self.organizer.view.select_pages(pages)
            if pages and self.mode != "pages":
                self.view.go_to_page(pages[0])
        QTimer.singleShot(0, apply)

    def _need_core(self) -> bool:
        if core_pages is None:
            self.toast(tr("shell.soon"), "info")
            return False
        return True

    def _run(self, fn, *args, success: str | None = None, undo: bool = False):
        try:
            result = fn(self.pdf, *args)
        except ValueError as exc:
            self.toast(str(exc) or tr("shell.error.generic"), "error")
            return None
        except Exception as exc:  # noqa: BLE001
            self.toast(tr("shell.error.op", err=str(exc)), "error")
            return None
        if success:
            if undo:
                self.toast(success, "success", action_text=tr("shell.undo_short"), on_action=self.pdf.undo)
            else:
                self.toast(success, "success")
        return result

    def rotate_selected(self, degrees: int) -> None:
        if not self._need_core():
            return
        pages = self.selected_pages()
        self._run(core_pages.rotate_pages, pages, degrees)
        self._select_after(pages)

    def delete_selected(self) -> None:
        if not self._need_core():
            return
        pages = self.selected_pages()
        if len(pages) >= self.pdf.page_count:
            self.toast(tr("shell.pages.cant_delete_all"), "warning")
            return
        self._run(core_pages.delete_pages, pages,
                  success=tr("shell.pages.deleted", n=len(pages)), undo=True)

    def duplicate_selected(self) -> None:
        if not self._need_core():
            return
        new = self._run(core_pages.duplicate_pages, self.selected_pages())
        if new:
            self._select_after(list(new))

    def move_pages(self, rows: list[int], to_index: int) -> None:
        if not self._need_core() or not rows:
            return
        # dropping a block onto itself is a no-op
        contiguous = rows == list(range(rows[0], rows[0] + len(rows)))
        if contiguous and rows[0] <= to_index <= rows[-1] + 1:
            return
        new = self._run(core_pages.move_pages, rows, to_index)
        if new:
            self._select_after(list(new))

    def insert_blank(self) -> None:
        if not self._need_core():
            return
        pages = self.selected_pages()
        at = (max(pages) + 1) if pages else self.pdf.page_count
        idx = self._run(core_pages.insert_blank_page, at)
        if idx is not None:
            self._select_after([idx])

    def insert_files(self, files: list[str], at: int | None = None) -> None:
        if not self._need_core() or not files:
            return
        if at is None:
            pages = self.selected_pages()
            at = (max(pages) + 1) if pages else self.pdf.page_count
        inserted: list[int] = []
        for f in files:
            try:
                new = core_pages.insert_pdf(self.pdf, f, at)
            except Exception as exc:  # noqa: BLE001 - includes password protected files
                pwd = None
                if "password" in type(exc).__name__.lower() or "Password" in str(type(exc)):
                    from .dialogs.password import ask_password

                    pwd = ask_password(self, os.path.basename(f))
                if pwd is None:
                    self.toast(tr("shell.error.insert", name=os.path.basename(f)), "error")
                    continue
                try:
                    new = core_pages.insert_pdf(self.pdf, f, at, password=pwd)
                except Exception:
                    self.toast(tr("shell.error.insert", name=os.path.basename(f)), "error")
                    continue
            inserted.extend(new)
            at += len(new)
        if inserted:
            self.toast(tr("shell.pages.inserted", n=len(inserted)), "success",
                       action_text=tr("shell.undo_short"), on_action=self.pdf.undo)
            self._select_after(inserted)

    def insert_from_file_dialog(self) -> None:
        files, _ = QFileDialog.getOpenFileNames(self, tr("shell.pages.insert_file_title"), _last_dir(),
                                                tr("shell.filter.open"))
        if files:
            self.insert_files(files)

    def extract_selected(self) -> None:
        if not self._need_core():
            return
        pages = self.selected_pages()
        stem = os.path.splitext(self.pdf.display_name)[0]
        rng = f"{pages[0] + 1}" if len(pages) == 1 else f"{pages[0] + 1}-{pages[-1] + 1}"
        suggestion = os.path.join(_last_dir(self.pdf.path), f"{stem} ({tr('shell.pages.pages_word')} {rng}).pdf")
        path, _ = QFileDialog.getSaveFileName(self, tr("shell.pages.extract_title"), suggestion, tr("shell.filter.pdf"))
        if not path:
            return
        if not path.lower().endswith(".pdf"):
            path += ".pdf"
        data = self._run(core_pages.extract_pages, pages)
        if data is None:
            return
        try:
            with open(path, "wb") as fh:
                fh.write(data)
        except OSError as exc:
            self.toast(tr("shell.error.save", err=str(exc)), "error")
            return
        self.toast(tr("shell.pages.extracted", n=len(pages), name=os.path.basename(path)), "success")


def _last_dir(path: str | None = None) -> str:
    if path:
        return os.path.dirname(path)
    try:
        from .app import settings

        d = settings().value("paths/last_dir", "")
        if d and os.path.isdir(d):
            return d
    except Exception:
        pass
    return os.path.expanduser("~")
