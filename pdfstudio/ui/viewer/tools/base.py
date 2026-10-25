"""Tool framework for the page view.

A *tool* receives mouse/key events from :class:`~pdfstudio.ui.viewer.page_view.PageView`
in page display coordinates. Tools are grouped by *mode* (the segmented
control "Vis / Rediger / Kommentér / Sider"); each mode's floating toolbar
lists its tools sorted by ``order``.

Writing a tool (put it in ``pdfstudio/ui/viewer/tools/<name>.py`` – every
module in this package is imported automatically at startup)::

    from .base import Tool, ToolRegistry

    @ToolRegistry.register
    class HighlightTool(Tool):
        id = "annotate.highlight"
        label = "Fremhæv"            # use tr(...) via a class property if you like
        icon = "highlighter-circle"  # Phosphor name
        shortcut = "U"
        mode = "annotate"            # or a tuple of modes
        order = 10
        cursor = Qt.CursorShape.IBeamCursor
        hint = "Træk hen over tekst for at fremhæve"

        def mouse_release(self, ev):
            if ev.page >= 0:
                annotations.add_text_markup(self.ctx.pdf, ev.page, ...)

One instance per tool and per document tab is created lazily. ``self.ctx``
(:class:`ToolContext`) gives access to the ``pdf`` (PdfDocument), ``view``
(PageView), ``workspace`` and ``toast(text, kind="info", **kw)``.
"""

from __future__ import annotations

import importlib
import pkgutil
from dataclasses import dataclass
from typing import Callable

from PySide6.QtCore import QObject, QPointF, Qt, Signal
from PySide6.QtGui import QKeyEvent
from PySide6.QtWidgets import QWidget

MODES = ("view", "edit", "annotate", "pages")


@dataclass
class ToolMouseEvent:
    """Mouse event in page display coordinates.

    ``page`` is the page under the cursor (``-1`` between pages); ``pos`` is
    the point in that page's display space (PDF points, rotation applied). For
    drags that leave the start page use :meth:`pos_on` to map to any page.
    """

    page: int
    pos: QPointF
    scene_pos: QPointF
    view_pos: QPointF
    buttons: Qt.MouseButton
    button: Qt.MouseButton
    modifiers: Qt.KeyboardModifier
    view: object = None
    click_count: int = 1

    def pos_on(self, page: int) -> QPointF:
        """This event's position expressed in ``page``'s display coordinates."""
        return self.view.scene_to_page(page, self.scene_pos)

    @property
    def left(self) -> bool:
        return bool(self.buttons & Qt.MouseButton.LeftButton) or self.button == Qt.MouseButton.LeftButton


@dataclass
class ToolContext:
    pdf: object                       # pdfstudio.core.PdfDocument
    view: object                      # PageView
    workspace: object                 # DocumentWorkspace
    toast: Callable[..., object]      # toast(text, kind="info", action_text=None, on_action=None)


class Tool(QObject):
    id: str = ""
    label: str = ""
    icon: str = ""
    shortcut: str | None = None
    cursor = Qt.CursorShape.ArrowCursor
    mode: str | tuple[str, ...] = "view"
    order: int = 100
    hint: str = ""          # status bar hint while active

    changed = Signal()      # emit when the tool wants its properties panel/hint refreshed

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self.ctx: ToolContext | None = None
        self.active = False

    # -- lifecycle -------------------------------------------------------
    def activate(self, ctx: ToolContext) -> None:
        self.ctx = ctx
        self.active = True

    def deactivate(self) -> None:
        self.active = False

    # -- events (override what you need) -------------------------------
    def mouse_press(self, ev: ToolMouseEvent) -> None:
        pass

    def mouse_move(self, ev: ToolMouseEvent) -> None:
        pass

    def mouse_release(self, ev: ToolMouseEvent) -> None:
        pass

    def mouse_double_click(self, ev: ToolMouseEvent) -> None:
        pass

    def key_press(self, ev: QKeyEvent) -> bool:
        return False

    def context_menu(self, ev: ToolMouseEvent, global_pos) -> bool:
        """Right click: show a menu at ``global_pos`` and return True, or
        return False for none."""
        return False

    def properties_widget(self) -> QWidget | None:
        return None

    # -- optional clipboard / selection hooks ----------------------------
    def has_selection(self) -> bool:
        return False

    def copy(self) -> bool:
        return False

    def select_all(self) -> bool:
        return False

    def document_changed(self, event) -> None:
        """Called after the PdfDocument changed (ChangeEvent)."""

    # -- helpers ---------------------------------------------------------
    @classmethod
    def modes(cls) -> tuple[str, ...]:
        return (cls.mode,) if isinstance(cls.mode, str) else tuple(cls.mode)

    @classmethod
    def display_label(cls) -> str:
        return cls.label


class ToolRegistry:
    """Global registry of tool classes keyed by ``Tool.id``."""

    _tools: dict[str, type[Tool]] = {}
    _discovered = False

    @classmethod
    def register(cls, tool_cls: type[Tool]) -> type[Tool]:
        if not tool_cls.id:
            raise ValueError(f"{tool_cls.__name__} has no id")
        cls._tools[tool_cls.id] = tool_cls
        return tool_cls

    @classmethod
    def get(cls, tool_id: str) -> type[Tool] | None:
        cls.discover()
        return cls._tools.get(tool_id)

    @classmethod
    def all(cls) -> list[type[Tool]]:
        cls.discover()
        return sorted(cls._tools.values(), key=lambda t: (t.order, t.id))

    @classmethod
    def for_mode(cls, mode: str) -> list[type[Tool]]:
        return [t for t in cls.all() if mode in t.modes()]

    @classmethod
    def discover(cls) -> None:
        """Import every module in this package so decorators register tools."""
        if cls._discovered:
            return
        cls._discovered = True
        pkg = importlib.import_module(__package__)
        for m in sorted(pkgutil.iter_modules(pkg.__path__), key=lambda m: m.name):
            if m.name == "base" or m.name.startswith("_"):
                continue
            try:
                importlib.import_module(f"{__package__}.{m.name}")
            except Exception as exc:  # a broken tool must not break the app
                import sys
                import traceback

                print(f"[folio] tool module {m.name!r} failed to load: {exc}", file=sys.stderr)
                traceback.print_exc()
