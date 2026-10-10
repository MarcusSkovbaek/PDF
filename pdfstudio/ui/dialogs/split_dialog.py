"""Split the current document into several PDF files.

Three modes – one file per page, a fixed number of pages per file, or custom
ranges (one file per comma separated item). A live preview lists the files
that will be written; nothing is generated until "Opdel" is pressed.
"""

from __future__ import annotations

import os

from PySide6.QtCore import QStandardPaths, Qt
from PySide6.QtWidgets import QFileDialog, QHBoxLayout, QLineEdit, QPushButton, QSpinBox, QStackedWidget, QVBoxLayout, QWidget

from ...core import PdfDocument
from ...core.pages import split_document, split_plan
from ...i18n import tr
from ..widgets.segmented import Segment, SegmentedControl
from ..widgets.util import label
from .common import FolioDialog, confirm, show_error

PREVIEW_ROWS = 5


def _pages_text(group: list[int]) -> str:
    if len(group) == 1:
        return tr("split.preview.pages_one", first=group[0] + 1)
    if group == list(range(group[0], group[-1] + 1)):
        return tr("split.preview.pages_range", first=group[0] + 1, last=group[-1] + 1)
    return tr("split.preview.pages_list", count=len(group))


class SplitDialog(FolioDialog):
    def __init__(self, parent: QWidget | None, pdf: PdfDocument) -> None:
        super().__init__(parent, tr("split.title"),
                         tr("split.subtitle", name=pdf.display_name, pages=pdf.page_count),
                         icon_name="scissors", width=500)
        self.pdf = pdf
        self.written: list[str] = []
        self._plan: list[tuple[str, list[int]]] = []

        self.mode = SegmentedControl([
            Segment("each", tr("split.mode.each")),
            Segment("every_n", tr("split.mode.every_n")),
            Segment("ranges", tr("split.mode.ranges")),
        ], expand=True)
        self.mode.currentChanged.connect(self._mode_changed)
        self.body.addWidget(self.mode)

        # mode specific options (the "each" page is intentionally empty)
        self.options = QStackedWidget()
        self.options.addWidget(QWidget())
        n_page = QWidget()
        n_row = QHBoxLayout(n_page)
        n_row.setContentsMargins(0, 4, 0, 0)
        n_row.addWidget(label(tr("split.every_n.label")))
        n_row.addStretch(1)
        self.every_n = QSpinBox()
        self.every_n.setRange(1, max(1, pdf.page_count))
        self.every_n.setValue(min(2, max(1, pdf.page_count)))
        self.every_n.setMinimumWidth(90)
        self.every_n.valueChanged.connect(self._update_preview)
        n_row.addWidget(self.every_n)
        self.options.addWidget(n_page)
        r_page = QWidget()
        r_col = QVBoxLayout(r_page)
        r_col.setContentsMargins(0, 4, 0, 0)
        r_col.setSpacing(6)
        self.ranges = QLineEdit()
        self.ranges.setPlaceholderText(tr("split.ranges.placeholder"))
        self.ranges.textChanged.connect(self._update_preview)
        r_col.addWidget(self.ranges)
        r_col.addWidget(label(tr("split.ranges.label"), "caption"))
        self.options.addWidget(r_page)
        self.body.addWidget(self.options)

        self.body.addSpacing(2)
        self.body.addWidget(label(tr("split.folder.label"), "section"))
        folder_row = QHBoxLayout()
        folder_row.setSpacing(8)
        self.folder = QLineEdit(self._default_folder())
        self.folder.textChanged.connect(self._update_preview)
        folder_row.addWidget(self.folder, 1)
        choose = QPushButton(tr("split.folder.choose"))
        choose.setCursor(Qt.CursorShape.PointingHandCursor)
        choose.clicked.connect(self._choose_folder)
        folder_row.addWidget(choose)
        self.body.addLayout(folder_row)

        self.body.addSpacing(2)
        self.preview_title = label("", "section")
        self.body.addWidget(self.preview_title)
        self.preview = label("", "muted", wrap=True)
        self.preview.setTextFormat(Qt.TextFormat.PlainText)
        self.body.addWidget(self.preview)
        self.error = label("", "danger", wrap=True)
        self.body.addWidget(self.error)

        self.add_button(tr("split.cancel"), "secondary", "cancel")
        self.run_button = self.add_button(tr("split.run"), "primary", default=True)
        self.run_button.clicked.connect(self._run)
        self._update_preview()

    # -- state ------------------------------------------------------------
    def _default_folder(self) -> str:
        if self.pdf.path:
            return os.path.dirname(self.pdf.path)
        return QStandardPaths.writableLocation(QStandardPaths.StandardLocation.DocumentsLocation) or os.getcwd()

    def _choose_folder(self) -> None:
        folder = QFileDialog.getExistingDirectory(self, tr("split.folder.dialog"), self.folder.text())
        if folder:
            self.folder.setText(folder)

    def _mode_changed(self, key: str) -> None:
        self.options.setCurrentIndex(self.mode.keys().index(key))
        if key == "ranges":
            self.ranges.setFocus()
        self._update_preview()

    def _split_kwargs(self) -> dict:
        key = self.mode.current_key()
        if key == "every_n":
            return {"every_n": self.every_n.value()}
        if key == "ranges":
            return {"ranges": self.ranges.text()}
        return {}

    def _update_preview(self) -> None:
        error = ""
        try:
            if self.mode.current_key() == "ranges" and not self.ranges.text().strip():
                self._plan = []
            else:
                self._plan = split_plan(self.pdf, **self._split_kwargs())
        except ValueError as exc:
            self._plan, error = [], str(exc)
        if not error and not os.path.isdir(self.folder.text().strip()):
            error = tr("split.error.folder")
        plan = self._plan
        if plan:
            self.preview_title.setText(tr("split.preview.one") if len(plan) == 1
                                       else tr("split.preview.count", count=len(plan)))
            rows = [f"{name}  ·  {_pages_text(group)}" for name, group in plan[:PREVIEW_ROWS]]
            if len(plan) > PREVIEW_ROWS:
                rows.append(tr("split.preview.more", count=len(plan) - PREVIEW_ROWS))
            self.preview.setText("\n".join(rows))
        else:
            self.preview_title.setText("")
            self.preview.setText("")
        self.preview_title.setVisible(bool(plan))
        self.preview.setVisible(bool(plan))
        self.error.setText(error)
        self.error.setVisible(bool(error))
        self.run_button.setEnabled(bool(plan) and not error)

    # -- run --------------------------------------------------------------
    def _run(self) -> None:
        folder = self.folder.text().strip()
        targets = [os.path.join(folder, name) for name, _ in self._plan]
        existing = [t for t in targets if os.path.exists(t)]
        if existing and not confirm(self, tr("split.overwrite.title"),
                                    tr("split.overwrite.text", count=len(existing)),
                                    tr("split.overwrite.ok"), danger=True):
            return
        try:
            parts = split_document(self.pdf, **self._split_kwargs())
            written = []
            for name, data in parts:
                path = os.path.join(folder, name)
                with open(path, "wb") as fh:
                    fh.write(data)
                written.append(path)
        except (OSError, ValueError) as exc:
            show_error(self, tr("split.error.title"), str(exc))
            return
        self.written = written
        self.accept()
