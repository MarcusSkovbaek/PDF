""""Adgangskode…" – protect a document with a password (or remove it)."""

from __future__ import annotations

from PySide6.QtWidgets import QCheckBox, QFormLayout, QLineEdit, QWidget

from ...core.document import ALL_PERMISSIONS, Security
from ...i18n import tr
from ..widgets.util import label
from .common import _AUTO, FolioDialog


class SecurityDialog(FolioDialog):
    def __init__(self, parent: QWidget | None, current: Security | None) -> None:
        super().__init__(parent, tr("security.title"), tr("security.subtitle"), icon_name="lock-key", width=460)
        self.current = current
        self.require = QCheckBox(tr("security.require"))
        self.require.setChecked(current is not None and current.needs_password)
        self.body.addWidget(self.require)
        form = QFormLayout()
        form.setSpacing(8)
        self.pw1 = QLineEdit()
        self.pw2 = QLineEdit()
        for f in (self.pw1, self.pw2):
            f.setEchoMode(QLineEdit.EchoMode.Password)
        if current is not None and current.needs_password:
            self.pw1.setText(current.user_pw)
            self.pw2.setText(current.user_pw)
        form.addRow(label(tr("security.password"), "caption"), self.pw1)
        form.addRow(label(tr("security.repeat"), "caption"), self.pw2)
        self.fields = QWidget()
        self.fields.setLayout(form)
        self.body.addWidget(self.fields)
        self.error = label("", "danger")
        self.body.addWidget(self.error)
        restricted = current is not None and current.permissions != ALL_PERMISSIONS
        note = label(tr("security.restrictions"), "caption", wrap=True)
        note.setVisible(restricted)
        self.body.addWidget(note)
        self.choice = "cancel"
        if current is not None:
            rm = self.add_button(tr("security.remove"), "ghost", "remove")
            self.buttons.insertWidget(0, rm)
            self.buttons.insertStretch(1, 1)
        self.add_button(tr("shell.dirty.cancel"), "secondary", "cancel")
        self.ok = self.add_button(tr("security.apply"), "primary", None, default=True)
        self.ok.clicked.connect(self._apply)
        self.require.toggled.connect(self._validate)
        self.pw1.textChanged.connect(self._validate)
        self.pw2.textChanged.connect(self._validate)
        self._validate()

    def _validate(self) -> None:
        on = self.require.isChecked()
        self.fields.setEnabled(on)
        msg = ""
        if on and not self.pw1.text():
            msg = tr("security.empty")
        elif on and self.pw1.text() != self.pw2.text():
            msg = tr("security.mismatch")
        # a missing password just disables "Anvend"; a mismatch is explained
        self.error.setText(msg if msg == tr("security.mismatch") and self.pw2.text() else "")
        self.ok.setEnabled(not msg)

    def _apply(self) -> None:
        self._finish("apply")

    def result_security(self) -> tuple[str, Security | None] | None:
        if self.choice == "remove":
            return ("remove", None)
        if self.choice != "apply":
            return None
        cur = self.current
        perms = cur.permissions if cur is not None else ALL_PERMISSIONS
        owner = cur.owner_pw if cur is not None else ""
        if self.require.isChecked():
            return ("apply", Security(user_pw=self.pw1.text(), owner_pw=owner, permissions=perms))
        # no open password: keep only the restrictions there were
        return ("apply", Security(permissions=perms) if perms != ALL_PERMISSIONS else None)


def ask_security(parent: QWidget | None, current: Security | None) -> tuple[str, Security | None] | None:
    """``None`` if cancelled, else ``(choice, new security or None)``."""
    if "security" in _AUTO:
        pw = _AUTO["security"]
        return ("apply", Security(user_pw=pw)) if pw else ("remove", None)
    dlg = SecurityDialog(parent, current)
    dlg.exec()
    return dlg.result_security()
