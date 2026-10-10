"""Password prompt for encrypted PDFs."""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QLineEdit, QWidget

from ...i18n import tr
from ..theme.icons import icon
from ..widgets.util import label, on_theme_change
from .common import FolioDialog


class PasswordDialog(FolioDialog):
    def __init__(self, parent: QWidget | None, filename: str, *, wrong: bool = False) -> None:
        super().__init__(parent, tr("shell.password.title"), tr("shell.password.text", name=filename),
                         icon_name="lock-key", width=440)
        self.field = QLineEdit()
        self.field.setEchoMode(QLineEdit.EchoMode.Password)
        self.field.setPlaceholderText(tr("shell.password.placeholder"))
        self._toggle = self.field.addAction(icon("eye"), QLineEdit.ActionPosition.TrailingPosition)
        self._toggle.setToolTip(tr("shell.password.show"))
        self._toggle.triggered.connect(self._toggle_echo)
        on_theme_change(self, self._refresh_icon)
        self.body.addWidget(self.field)
        self.error = label(tr("shell.password.wrong"), "danger")
        self.error.setVisible(wrong)
        self.body.addWidget(self.error)
        self.add_button(tr("shell.dirty.cancel"), "secondary", "cancel")
        self.ok = self.add_button(tr("shell.password.open"), "primary", "ok", default=True)
        self.field.setFocus(Qt.FocusReason.OtherFocusReason)

    def _refresh_icon(self) -> None:
        shown = self.field.echoMode() == QLineEdit.EchoMode.Normal
        self._toggle.setIcon(icon("eye-slash" if shown else "eye"))

    def _toggle_echo(self) -> None:
        shown = self.field.echoMode() == QLineEdit.EchoMode.Normal
        self.field.setEchoMode(QLineEdit.EchoMode.Password if shown else QLineEdit.EchoMode.Normal)
        self._refresh_icon()

    def password(self) -> str:
        return self.field.text()


def ask_password(parent: QWidget | None, filename: str, *, wrong: bool = False) -> str | None:
    """Show the dialog; returns the password or ``None`` if cancelled."""
    dlg = PasswordDialog(parent, filename, wrong=wrong)
    if dlg.exec() == FolioDialog.DialogCode.Accepted:
        return dlg.password()
    return None
