"""Styled dialog building blocks shared by every dialog.

* :class:`FolioDialog` – base dialog: icon well + title + subtitle header,
  a content area (``self.body`` layout) and a right-aligned button row.
* :func:`ask_save_changes` – "Gem / Gem ikke / Annullér" prompt.
* :func:`show_error`, :func:`confirm` – simple message dialogs.
"""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QDialog, QFrame, QHBoxLayout, QPushButton, QVBoxLayout, QWidget

from ...i18n import tr
from ..widgets.util import ThemedIconLabel, label


class FolioDialog(QDialog):
    def __init__(self, parent: QWidget | None, title: str, subtitle: str = "", *,
                 icon_name: str | None = None, icon_role: str = "accent_text", width: int = 440) -> None:
        super().__init__(parent)
        self.setWindowTitle(title)
        self.setModal(True)
        self.setMinimumWidth(width)
        outer = QVBoxLayout(self)
        outer.setContentsMargins(24, 22, 24, 20)
        outer.setSpacing(16)
        head = QHBoxLayout()
        head.setSpacing(14)
        if icon_name:
            well = QFrame()
            well.setObjectName("IconWell")
            well.setFixedSize(40, 40)
            wl = QVBoxLayout(well)
            wl.setContentsMargins(0, 0, 0, 0)
            wl.addWidget(ThemedIconLabel(icon_name, 22, icon_role), 0, Qt.AlignmentFlag.AlignCenter)
            head.addWidget(well, 0, Qt.AlignmentFlag.AlignTop)
        tv = QVBoxLayout()
        tv.setSpacing(4)
        self.title_label = label(title, "title")
        tv.addWidget(self.title_label)
        self.subtitle_label = label(subtitle, "muted", wrap=True)
        self.subtitle_label.setVisible(bool(subtitle))
        tv.addWidget(self.subtitle_label)
        head.addLayout(tv, 1)
        outer.addLayout(head)
        self.body = QVBoxLayout()
        self.body.setSpacing(10)
        outer.addLayout(self.body)
        self.buttons = QHBoxLayout()
        self.buttons.setSpacing(8)
        self.buttons.addStretch(1)
        outer.addSpacing(4)
        outer.addLayout(self.buttons)

    def add_button(self, text: str, variant: str = "secondary", role: str | None = None, *,
                   default: bool = False) -> QPushButton:
        b = QPushButton(text)
        if variant != "secondary":
            b.setProperty("variant", variant)
        b.setCursor(Qt.CursorShape.PointingHandCursor)
        b.setMinimumWidth(88)
        if default:
            b.setDefault(True)
            b.setAutoDefault(True)
        else:
            b.setAutoDefault(False)
        if role is not None:
            b.clicked.connect(lambda: self._finish(role))
        self.buttons.addWidget(b)
        return b

    def _finish(self, role: str) -> None:
        self.choice = role
        if role == "cancel":
            self.reject()
        else:
            self.accept()


def ask_save_changes(parent: QWidget | None, name: str) -> str:
    """Returns ``"save"``, ``"discard"`` or ``"cancel"``."""
    dlg = FolioDialog(parent, tr("shell.dirty.title", name=name), tr("shell.dirty.text"),
                      icon_name="floppy-disk", width=460)
    dlg.choice = "cancel"
    discard = dlg.add_button(tr("shell.dirty.discard"), "ghost", "discard")
    dlg.buttons.insertWidget(0, discard)  # left aligned, away from the safe choices
    dlg.buttons.insertStretch(1, 1)
    dlg.add_button(tr("shell.dirty.cancel"), "secondary", "cancel")
    dlg.add_button(tr("shell.dirty.save"), "primary", "save", default=True)
    if not _AUTO.get("save_changes"):
        dlg.exec()
    else:
        dlg.choice = _AUTO["save_changes"]
    return dlg.choice


def show_error(parent: QWidget | None, title: str, text: str) -> None:
    dlg = FolioDialog(parent, title, text, icon_name="warning-circle", icon_role="danger")
    dlg.choice = "ok"
    dlg.add_button(tr("shell.ok"), "primary", "ok", default=True)
    if not _AUTO.get("error"):
        dlg.exec()


def confirm(parent: QWidget | None, title: str, text: str, ok_text: str, *, danger: bool = False) -> bool:
    dlg = FolioDialog(parent, title, text, icon_name="warning" if danger else "question",
                      icon_role="danger" if danger else "accent_text")
    dlg.choice = "cancel"
    dlg.add_button(tr("shell.dirty.cancel"), "secondary", "cancel")
    dlg.add_button(ok_text, "danger" if danger else "primary", "ok", default=True)
    if _AUTO.get("confirm") is not None:
        return _AUTO["confirm"] == "ok"
    dlg.exec()
    return dlg.choice == "ok"


# Test hook: pre-answer dialogs (e.g. ``_AUTO["save_changes"] = "discard"``).
_AUTO: dict[str, str] = {}
