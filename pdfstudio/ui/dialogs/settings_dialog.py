""""Indstillinger…" – preferences (stored locally with QSettings)."""

from __future__ import annotations

from PySide6.QtWidgets import QCheckBox, QHBoxLayout, QSpinBox, QWidget

from ...i18n import tr
from ..widgets.util import label
from .common import FolioDialog


class SettingsDialog(FolioDialog):
    def __init__(self, window) -> None:
        super().__init__(window, tr("settings.title"), tr("settings.subtitle"), icon_name="gear-six", width=500)
        self.win = window
        saver = window.saver
        self.body.addWidget(label(tr("settings.save"), "section"))
        minutes = saver.autosave_minutes()
        self.autosave = QCheckBox(tr("settings.autosave"))
        self.autosave.setChecked(minutes > 0)
        self.body.addWidget(self.autosave)
        row = QHBoxLayout()
        row.setContentsMargins(26, 0, 0, 0)
        row.setSpacing(8)
        row.addWidget(label(tr("settings.autosave.every"), None))
        self.minutes = QSpinBox()
        self.minutes.setRange(1, 60)
        self.minutes.setValue(minutes or 2)
        self.minutes.setFixedWidth(72)
        row.addWidget(self.minutes)
        self.unit = label("", None)
        row.addWidget(self.unit)
        row.addStretch(1)
        self.every = QWidget()
        self.every.setLayout(row)
        self.body.addWidget(self.every)
        hint = label(tr("settings.autosave.hint"), "caption", wrap=True)
        hint.setContentsMargins(26, 0, 0, 0)
        self.body.addWidget(hint)
        self.warn = QCheckBox(tr("settings.warn_external"))
        self.warn.setChecked(saver.warn_external())
        self.body.addWidget(self.warn)
        self.minutes.valueChanged.connect(self._unit)
        self.autosave.toggled.connect(self.every.setEnabled)
        self.every.setEnabled(self.autosave.isChecked())
        self._unit(self.minutes.value())
        self.choice = "cancel"
        self.add_button(tr("shell.dirty.cancel"), "secondary", "cancel")
        ok = self.add_button(tr("settings.ok"), "primary", None, default=True)
        ok.clicked.connect(self.apply)

    def _unit(self, n: int) -> None:
        self.unit.setText(tr("settings.autosave.minutes") if n == 1 else tr("settings.autosave.minutes_n"))

    def apply(self) -> None:
        self.win.saver.set_settings(self.minutes.value() if self.autosave.isChecked() else 0, self.warn.isChecked())
        self._finish("ok")
