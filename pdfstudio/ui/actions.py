"""Central command registry.

Every command of the application is an *action id* (``"file.save"``) mapped
to a :class:`QAction`. Menus, the floating mode toolbars, the status bar and
the command palette (Ctrl+K) are all built from this registry, so features
only need to register once::

    window.actions.register(
        "pages.split", tr("pages.split"), icon="scissors",
        shortcut="Ctrl+Alt+S", handler=open_split_dialog,
        enabled=lambda: window.current_pdf() is not None,
        menu="pages", mode="pages", category=tr("shell.cat.pages"),
    )

``handler`` is called without arguments (``handler(checked)`` for checkable
actions). ``enabled`` is re-evaluated by :meth:`refresh_enabled` (called by
the main window whenever the document, tab or selection changes).

Extra keyword arguments (all optional):

* ``menu`` – ``"file" | "edit" | "view" | "pages" | "tools" | "help"``: also
  list the action at the end of that menu.
* ``mode`` – ``"view" | "edit" | "annotate" | "pages"`` (or a tuple): show the
  action in that mode's floating toolbar (after the mode's tools).
* ``category`` – group label shown in the command palette.
* ``palette`` – ``False`` hides it from the command palette.
* ``order`` – sort key inside menus / toolbars.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable, Iterable

from PySide6.QtCore import QObject, Qt, Signal
from PySide6.QtGui import QAction, QKeySequence
from PySide6.QtWidgets import QWidget

from .theme.icons import icon as themed_icon


@dataclass
class ActionEntry:
    id: str
    action: QAction
    handler: Callable
    enabled: Callable[[], bool]
    icon: str | None = None
    category: str = ""
    menu: str | None = None
    modes: tuple[str, ...] = ()
    palette: bool = True
    order: int = 100
    keywords: str = ""
    base_text: str = ""
    extra: dict = field(default_factory=dict)

    @property
    def text(self) -> str:
        return self.action.text().replace("&", "")

    @property
    def shortcut_text(self) -> str:
        sc = self.action.shortcut()
        return sc.toString(QKeySequence.SequenceFormat.NativeText) if not sc.isEmpty() else ""


def _seqs(shortcut) -> list[QKeySequence]:
    if shortcut is None:
        return []
    if isinstance(shortcut, (list, tuple)):
        out = []
        for s in shortcut:
            out.extend(_seqs(s))
        return out
    if isinstance(shortcut, QKeySequence.StandardKey):
        return list(QKeySequence.keyBindings(shortcut))
    return [shortcut if isinstance(shortcut, QKeySequence) else QKeySequence(shortcut)]


class ActionRegistry(QObject):
    registered = Signal(str)
    changed = Signal()

    def __init__(self, host: QWidget) -> None:
        super().__init__(host)
        self._host = host
        self._entries: dict[str, ActionEntry] = {}
        try:
            from .theme.manager import theme_manager

            theme_manager().changed.connect(self._refresh_icons)
        except Exception:  # pragma: no cover
            pass

    # -- registration ----------------------------------------------------
    def register(
        self,
        id: str,  # noqa: A002 - contract name
        text: str,
        *,
        icon: str | None = None,
        shortcut=None,
        handler: Callable,
        enabled: Callable[[], bool] = lambda: True,
        checkable: bool = False,
        checked: Callable[[], bool] | None = None,
        menu: str | None = None,
        mode: str | Iterable[str] | None = None,
        category: str = "",
        palette: bool = True,
        order: int = 100,
        keywords: str = "",
        tooltip: str | None = None,
    ) -> QAction:
        if id in self._entries:
            old = self._entries.pop(id)
            self._host.removeAction(old.action)
            old.action.deleteLater()
        act = QAction(text, self._host)
        act.setObjectName(id)
        act.setCheckable(checkable)
        seqs = _seqs(shortcut)
        if seqs:
            act.setShortcuts(seqs)
            act.setShortcutContext(Qt.ShortcutContext.WindowShortcut)
        if icon:
            act.setIcon(themed_icon(icon))
        tip = tooltip or text
        if seqs:
            tip = f"{tip}  ({seqs[0].toString(QKeySequence.SequenceFormat.NativeText)})"
        act.setToolTip(tip)
        if checkable:
            act.triggered.connect(lambda c=False, h=handler: h(c))
        else:
            act.triggered.connect(lambda _c=False, h=handler: h())
        self._host.addAction(act)
        modes = (mode,) if isinstance(mode, str) else tuple(mode or ())
        entry = ActionEntry(id, act, handler, enabled, icon, category, menu, modes, palette, order, keywords, text)
        if checked is not None:
            entry.extra["checked"] = checked
        self._entries[id] = entry
        self._apply_enabled(entry)
        self.registered.emit(id)
        self.changed.emit()
        return act

    def unregister(self, id: str) -> None:  # noqa: A002
        e = self._entries.pop(id, None)
        if e:
            self._host.removeAction(e.action)
            e.action.deleteLater()
            self.changed.emit()

    # -- queries ---------------------------------------------------------
    def action(self, id: str) -> QAction:  # noqa: A002
        return self._entries[id].action

    def get(self, id: str) -> ActionEntry | None:  # noqa: A002
        return self._entries.get(id)

    def has(self, id: str) -> bool:  # noqa: A002
        return id in self._entries

    def all(self) -> list[ActionEntry]:
        return list(self._entries.values())

    def for_menu(self, menu: str) -> list[ActionEntry]:
        return sorted((e for e in self._entries.values() if e.menu == menu), key=lambda e: e.order)

    def for_mode(self, mode: str) -> list[ActionEntry]:
        return sorted((e for e in self._entries.values() if mode in e.modes), key=lambda e: e.order)

    # -- behaviour -------------------------------------------------------
    def trigger(self, id: str) -> bool:  # noqa: A002
        e = self._entries.get(id)
        if e is None:
            return False
        self._apply_enabled(e)
        if not e.action.isEnabled():
            return False
        e.action.trigger()
        return True

    def set_text(self, id: str, text: str) -> None:  # noqa: A002
        e = self._entries.get(id)
        if e:
            e.action.setText(text)

    def _apply_enabled(self, e: ActionEntry) -> None:
        try:
            e.action.setEnabled(bool(e.enabled()))
        except Exception:
            e.action.setEnabled(False)
        chk = e.extra.get("checked")
        if chk is not None:
            try:
                e.action.blockSignals(True)
                e.action.setChecked(bool(chk()))
            except Exception:
                pass
            finally:
                e.action.blockSignals(False)

    def refresh_enabled(self) -> None:
        for e in self._entries.values():
            self._apply_enabled(e)

    def _refresh_icons(self, *_a) -> None:
        for e in self._entries.values():
            if e.icon:
                e.action.setIcon(themed_icon(e.icon))
