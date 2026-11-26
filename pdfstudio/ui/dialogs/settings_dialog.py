""""Indstillinger…" – preferences (stored locally with QSettings)."""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QCheckBox, QFormLayout, QHBoxLayout, QSpinBox, QWidget

from ...i18n import tr
from .. import clipboard_source as clip
from ..widgets.segmented import Segment, SegmentedControl
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
        self._build_clipboard()
        self.choice = "cancel"
        self.add_button(tr("shell.dirty.cancel"), "secondary", "cancel")
        ok = self.add_button(tr("settings.ok"), "primary", None, default=True)
        ok.clicked.connect(self.apply)

    def _unit(self, n: int) -> None:
        self.unit.setText(tr("settings.autosave.minutes") if n == 1 else tr("settings.autosave.minutes_n"))

    # -- Udklipsholder ---------------------------------------------------------------
    def _build_clipboard(self) -> None:
        settings = self.win.settings
        self.body.addSpacing(6)
        self.body.addWidget(label(tr("clipboard.settings.section"), "section"))
        form = QFormLayout()
        form.setContentsMargins(0, 0, 0, 0)
        form.setHorizontalSpacing(16)
        form.setVerticalSpacing(10)
        form.setLabelAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
        form.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow)

        def sizes(keys) -> SegmentedControl:
            return SegmentedControl([Segment(k, tr(f"clipboard.settings.size.{k}") if k in ("image", "match") else k,
                                             None, tr(f"clipboard.settings.size.{k}.tip")) for k in keys],
                                    height=30, expand=True, min_seg=56)

        self.clip_new = sizes(clip.NEW_PAGE_SIZES)
        self.clip_new.setAccessibleName("clipboard.new_page_size")
        self.clip_new.set_current_key(clip.page_size_setting(settings, "new"), animate=False)
        form.addRow(tr("clipboard.settings.new_size"), self.clip_new)
        self.clip_insert = sizes(clip.INSERT_PAGE_SIZES)
        self.clip_insert.setAccessibleName("clipboard.insert_page_size")
        self.clip_insert.set_current_key(clip.page_size_setting(settings, "insert"), animate=False)
        form.addRow(tr("clipboard.settings.insert_size"), self.clip_insert)
        self.clip_margin = QSpinBox()
        self.clip_margin.setRange(*clip.MARGIN_RANGE)
        self.clip_margin.setSuffix(" mm")
        self.clip_margin.setFixedWidth(96)
        self.clip_margin.setValue(round(clip.margin_setting(settings)))
        form.addRow(tr("clipboard.settings.margin"), self.clip_margin)
        self.body.addLayout(form)
        self.body.addWidget(label(tr("clipboard.settings.hint"), "caption", wrap=True))
        self.clip_new.currentChanged.connect(self._clip_margin_state)
        self.clip_insert.currentChanged.connect(self._clip_margin_state)
        self._clip_margin_state()

    def _clip_margin_state(self, *_a) -> None:
        # the margin only matters when an image is put on a paper size
        self.clip_margin.setEnabled(self.clip_new.current_key() != "image"
                                    or self.clip_insert.current_key() != "image")

    def apply(self) -> None:
        self.win.saver.set_settings(self.minutes.value() if self.autosave.isChecked() else 0, self.warn.isChecked())
        s = self.win.settings
        s.setValue(clip.NEW_PAGE_SIZE_KEY, self.clip_new.current_key())
        s.setValue(clip.INSERT_PAGE_SIZE_KEY, self.clip_insert.current_key())
        s.setValue(clip.MARGIN_KEY, self.clip_margin.value())
        s.sync()
        self._finish("ok")
