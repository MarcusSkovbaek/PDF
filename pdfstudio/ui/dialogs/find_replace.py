""""Søg og erstat" (Ctrl+H): replace text in the whole document.

A small non-modal panel: search text, replacement, "Forskel på store og
små bogstaver" / "Hele ord", a live count of the occurrences (and on how
many pages), Find næste (highlights it on the page), Erstat (this one, then
the next) and Erstat alle (one undo step). Replaced text keeps the font,
size and colour of the text it replaces.
"""

from __future__ import annotations

from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import QCheckBox, QFormLayout, QLineEdit, QWidget

from ...core import replace as core_replace
from ...core.search import SearchHit
from ...i18n import tr, tr_n
from ..widgets.util import label
from .common import FolioDialog


class FindReplaceDialog(FolioDialog):
    def __init__(self, window, ws) -> None:
        super().__init__(window, tr("replace.title"), tr("replace.subtitle"), icon_name="swap", width=460)
        self.setModal(False)
        self.win = window
        self.ws = ws
        self.matches: list[core_replace.Match] = []
        self.current = -1
        self._stale = True
        form = QFormLayout()
        form.setSpacing(8)
        self.find = QLineEdit()
        self.find.setPlaceholderText(tr("replace.find.placeholder"))
        self.repl = QLineEdit()
        self.repl.setPlaceholderText(tr("replace.with.placeholder"))
        form.addRow(label(tr("replace.find"), "caption"), self.find)
        form.addRow(label(tr("replace.with"), "caption"), self.repl)
        box = QWidget()
        box.setLayout(form)
        self.body.addWidget(box)
        self.case = QCheckBox(tr("replace.case"))
        self.whole = QCheckBox(tr("replace.whole"))
        self.body.addWidget(self.case)
        self.body.addWidget(self.whole)
        self.info = label("", "caption")
        self.body.addWidget(self.info)
        self.choice = "close"
        close = self.add_button(tr("replace.close"), "ghost", None)
        close.clicked.connect(self.close)
        self.buttons.insertWidget(0, close)
        self.buttons.insertStretch(1, 1)
        self.next_btn = self.add_button(tr("replace.next"), "secondary", None)
        self.one_btn = self.add_button(tr("replace.one"), "secondary", None)
        self.all_btn = self.add_button(tr("replace.all"), "primary", None, default=True)
        self.next_btn.clicked.connect(self.find_next)
        self.one_btn.clicked.connect(self.replace_one)
        self.all_btn.clicked.connect(self.replace_all)
        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.setInterval(180)
        self._timer.timeout.connect(self.refresh)
        for w in (self.find,):
            w.textChanged.connect(self._schedule)
        self.case.toggled.connect(self._schedule)
        self.whole.toggled.connect(self._schedule)
        self._unlisten = ws.pdf.listen(self._doc_changed)
        self.finished.connect(self._cleanup)
        self.refresh()

    # -- state -----------------------------------------------------------------------
    def _schedule(self, *_a) -> None:
        self._stale = True
        self._timer.start()

    def _doc_changed(self, _ev) -> None:
        self._stale = True
        self._timer.start()

    def refresh(self) -> None:
        self._stale = False
        try:
            self.matches = core_replace.find_matches(self.ws.pdf.doc, self.find.text(),
                                                     case_sensitive=self.case.isChecked(),
                                                     whole_words=self.whole.isChecked())
        except Exception:
            self.matches = []
        self.current = min(self.current, len(self.matches) - 1)
        n = len(self.matches)
        pages = len({m.page for m in self.matches})
        if not self.find.text().strip():
            self.info.setText("")
        elif not n:
            self.info.setText(tr("replace.none"))
        else:
            self.info.setText(tr("replace.count", hits=tr_n("replace.hits", n), pages=tr_n("replace.pages", pages)))
        for b in (self.next_btn, self.one_btn, self.all_btn):
            b.setEnabled(n > 0)
        self._show()

    def _show(self) -> None:
        view = self.ws.view
        hits = [SearchHit(m.page, m.rects, m.text) for m in self.matches]
        view.set_search_hits(hits, self.current)
        if 0 <= self.current < len(hits):
            view.set_current_hit(self.current, scroll=True)

    # -- actions ---------------------------------------------------------------------
    def find_next(self) -> bool:
        if self._stale:
            self.refresh()
        if not self.matches:
            return False
        self.current = (self.current + 1) % len(self.matches)
        self._show()
        return True

    def replace_one(self) -> bool:
        if self._stale:
            self.refresh()
        if not self.matches:
            return False
        if self.current < 0:
            return self.find_next()
        m = self.matches[self.current]
        try:
            core_replace.replace_matches(self.ws.pdf, [m], self.repl.text())
        except Exception as exc:  # noqa: BLE001 - shown to the user
            self.win.toast(tr("replace.error", err=str(exc)), "error")
            return False
        self.current -= 1  # the next match moves into this index
        self.refresh()
        self.find_next()
        return True

    def replace_all(self) -> int:
        if self._stale:
            self.refresh()
        if not self.matches:
            return 0
        try:
            n = core_replace.replace_matches(self.ws.pdf, list(self.matches), self.repl.text())
        except Exception as exc:  # noqa: BLE001
            self.win.toast(tr("replace.error", err=str(exc)), "error")
            return 0
        self.current = -1
        self.refresh()
        self.win.toast(tr_n("replace.done", n), "success")
        return n

    def _cleanup(self, *_a) -> None:
        try:
            self._unlisten()
        except Exception:
            pass
        try:
            self.ws.view.set_search_hits([])
        except RuntimeError:
            pass

    def closeEvent(self, ev) -> None:  # noqa: N802
        self._cleanup()
        super().closeEvent(ev)


def open_find_replace(window, ws) -> FindReplaceDialog:
    dlg = getattr(ws, "_find_replace", None)
    if dlg is None:
        dlg = FindReplaceDialog(window, ws)
        ws._find_replace = dlg

        def gone(*_a):
            ws._find_replace = None

        dlg.finished.connect(gone)
        dlg.destroyed.connect(gone)
    view = ws.view
    tool = ws.tool
    if tool is not None and tool.has_selection() and hasattr(tool, "sel"):
        text = tool.sel.text(" ").strip()
        if text and "\n" not in text and len(text) < 80:
            dlg.find.setText(text)
    dlg.show()
    dlg.raise_()
    dlg.activateWindow()
    dlg.find.setFocus(Qt.FocusReason.OtherFocusReason)
    dlg.find.selectAll()
    view.setFocusProxy(None)
    return dlg
