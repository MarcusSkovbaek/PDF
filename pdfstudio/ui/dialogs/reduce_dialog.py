""""Reducér filstørrelse…" – save a smaller copy (images downsampled, fonts
subset, unused objects removed, optionally metadata stripped)."""

from __future__ import annotations

import os

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication, QCheckBox, QWidget

from ...core.document import write_atomic
from ...core.optimize import ReduceOptions, reduce_size
from ...i18n import tr
from ..widgets.segmented import Segment, SegmentedControl
from ..widgets.util import label
from .common import FolioDialog, show_error


def human_size(n: int) -> str:
    from PySide6.QtCore import QLocale

    loc = QLocale(QLocale.Language.Danish, QLocale.Country.Denmark)
    if n < 1024:
        return f"{n} B"
    if n < 1024 * 1024:
        return f"{loc.toString(n / 1024, 'f', 0)} KB"
    return f"{loc.toString(n / 1024 / 1024, 'f', 1)} MB"


class ReduceDialog(FolioDialog):
    KEYS = ("high", "medium", "low", "keep")

    def __init__(self, parent: QWidget | None, ws) -> None:
        super().__init__(parent, tr("reduce.title"), tr("reduce.subtitle"), icon_name="arrows-in", width=480)
        self.ws = ws
        self.body.addWidget(label(tr("reduce.images"), "section"))
        self.quality = SegmentedControl([Segment(k, tr(f"reduce.images.{k}"), None, tr(f"reduce.images.{k}.tip"))
                                         for k in self.KEYS], height=32, expand=True)
        self.quality.set_current_key("medium", animate=False)
        self.quality.currentChanged.connect(self._tip)
        self.body.addWidget(self.quality)
        self.tip = label("", "caption")
        self.body.addWidget(self.tip)
        self.strip = QCheckBox(tr("reduce.strip"))
        self.body.addWidget(self.strip)
        size = os.path.getsize(ws.pdf.path) if ws.pdf.path and os.path.exists(ws.pdf.path) else 0
        self.current = label(tr("reduce.current", size=human_size(size)) if size else "", "caption")
        self.body.addWidget(self.current)
        self.choice = "cancel"
        self.add_button(tr("shell.dirty.cancel"), "secondary", "cancel")
        run = self.add_button(tr("reduce.run"), "primary", None, default=True)
        run.clicked.connect(lambda: self.run())  # not run(checked): "checked" is no path
        self._tip("medium")

    def _tip(self, key: str) -> None:
        self.tip.setText(tr(f"reduce.images.{key}.tip"))

    def options(self) -> ReduceOptions:
        key = self.quality.current_key()
        strip = self.strip.isChecked()
        return ReduceOptions(images=None if key == "keep" else key, strip_metadata=strip, strip_authors=strip)

    def run(self, path: str | None = None) -> str | None:
        pdf = self.ws.pdf
        win = self.parent()
        if path is None:
            from PySide6.QtWidgets import QFileDialog

            from ..app import last_dir

            stem = os.path.splitext(pdf.display_name)[0]
            folder = os.path.dirname(pdf.path) if pdf.path else last_dir()
            base = os.path.join(folder, tr("reduce.name", name=stem) + ".pdf")
            path, _ = QFileDialog.getSaveFileName(self, tr("reduce.title"), base, tr("shell.filter.pdf"))
            if not path:
                return None
            if not path.lower().endswith(".pdf"):
                path += ".pdf"
        QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
        try:
            res = reduce_size(pdf, self.options())
            write_atomic(path, res.data)
        except Exception as exc:  # noqa: BLE001 - shown to the user
            QApplication.restoreOverrideCursor()
            show_error(self, tr("reduce.title"), tr("reduce.error", err=str(exc)))
            return None
        QApplication.restoreOverrideCursor()
        name = os.path.basename(path)
        if res.after < res.before:
            msg = tr("reduce.done", name=name, before=human_size(res.before), after=human_size(res.after),
                     pct=round(res.saved_fraction * 100))
        else:
            msg = tr("reduce.none", size=human_size(res.after))
        if win is not None and hasattr(win, "toast"):
            win.toast(msg, "success", action_text=tr("save.open"), on_action=lambda p=path: win.open_path(p))
        self.result_path = path
        self._finish("ok")
        return path
