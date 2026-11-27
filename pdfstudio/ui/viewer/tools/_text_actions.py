"""What can be done with a text selection – shared by the tools that select
text (*Markér* in Vis, *Markér kommentar* in Kommentér).

:class:`SelectionUI` shows the floating :class:`SelectionBar` under a
finished selection, builds the right-click menu and runs the actions:
Kommentér (highlight + comment, editor opens in the comment list), Fremhæv /
Understreg / Gennemstreg (in the colours of those tools), Erstat tekst and
Indsæt tekst (Acrobat's text edit proposals), Kopiér and Markér alt.
"""

from __future__ import annotations

from PySide6.QtCore import QObject, QPointF
from PySide6.QtGui import QGuiApplication
from PySide6.QtWidgets import QMenu

from ....core import annotations as core_annots
from ....core import comments as core_comments
from ....i18n import tr
from ..text_selection import SelectionBar, TextSelection
from ._annot_common import PRESETS, AnnotStyle
from .base import ToolRegistry

#: Text edit proposals (replace / insert) are blue, like in Acrobat.
TEXT_EDIT_COLOR = PRESETS[2]


def markup_style(kind: str) -> AnnotStyle:
    """The current style of the Fremhæv / Understreg / Gennemstreg tool."""
    cls = ToolRegistry.get(f"annotate.{kind}")
    default = getattr(cls, "default_style", AnnotStyle(PRESETS[0], 1.0))
    return AnnotStyle.load(f"annotate.{kind}", default)


class SelectionUI(QObject):
    def __init__(self, tool, sel: TextSelection) -> None:
        super().__init__(tool)
        self.tool = tool
        self.sel = sel
        self.bar: SelectionBar | None = None
        self._view = None

    # -- bar ----------------------------------------------------------------------
    def attach(self) -> None:
        view = self.tool.ctx.view
        if self._view is view and self.bar is not None:
            return
        self.detach()
        self._view = view
        self.bar = SelectionBar(view.viewport())
        self.bar.triggered.connect(self.run)
        view.verticalScrollBar().valueChanged.connect(self._reposition)
        view.horizontalScrollBar().valueChanged.connect(self._reposition)
        view.zoomChanged.connect(self._reposition)

    def detach(self) -> None:
        view = self._view
        if view is not None:
            for sig in (view.verticalScrollBar().valueChanged, view.horizontalScrollBar().valueChanged,
                        view.zoomChanged):
                try:
                    sig.disconnect(self._reposition)
                except (RuntimeError, TypeError):
                    pass
        if self.bar is not None:
            self.bar.hide()
            self.bar.setParent(None)
            self.bar.deleteLater()
        self.bar = None
        self._view = None

    def show_bar(self) -> None:
        if self.bar is None:
            return
        end = self.sel.end_rect()
        if end is None:
            self.bar.hide()
            return
        self.bar.show_at(self._view, *end)

    def hide_bar(self) -> None:
        if self.bar is not None:
            self.bar.hide()

    def _reposition(self, *_a) -> None:
        if self.bar is not None and self.bar.isVisible():
            self.show_bar()

    # -- actions ----------------------------------------------------------------------
    def run(self, key: str, caret: tuple[int, int] | None = None) -> bool:
        ctx = self.tool.ctx
        if ctx is None:
            return False
        if key == "copy":
            return self.copy()
        if key == "select_all":
            ok = self.sel.select_all()
            self.show_bar()
            return ok
        ws, pdf = ctx.workspace, ctx.pdf
        try:
            if key == "insert":
                if caret is None:
                    return False
                page, index = caret
                rect = core_comments.caret_rect(self.sel.page_text(page), index)
                if rect is None:
                    return False
                xref = core_comments.add_insert_text(pdf, page, rect, "", TEXT_EDIT_COLOR)
                self._done()
                ws.show_comments(page, xref, edit=True)
                return True
            rects = self.sel.rects()
            if not rects:
                ctx.toast(tr("sel.no_text"), "info")
                return False
            if key in core_annots.MARKUP_KINDS:
                st = markup_style(key)
                core_annots.add_markup(pdf, key, rects, st.color, st.opacity)
                self._done()
                return True
            if key == "comment":
                st = markup_style("highlight")
                refs = core_comments.add_comment(pdf, rects, "", st.color, opacity=st.opacity)
                self._done()
                if refs:
                    ws.show_comments(*refs[0], edit=True)
                return bool(refs)
            if key == "replace":
                page = min(rects)
                xref = core_comments.add_replace_text(pdf, page, rects[page], "", TEXT_EDIT_COLOR)
                self._done()
                ws.show_comments(page, xref, edit=True)
                return True
        except Exception as exc:  # noqa: BLE001 - shown to the user
            ctx.toast(tr("comments.error", err=str(exc)), "error")
        return False

    def _done(self) -> None:
        self.sel.clear()
        self.hide_bar()

    def copy(self) -> bool:
        text = self.sel.text()
        if not text:
            return False
        QGuiApplication.clipboard().setText(text)
        ctx = self.tool.ctx
        if ctx is not None and ctx.toast:
            ctx.toast(tr("sel.copied"), "success")
        return True

    # -- right-click menu ---------------------------------------------------------------
    def context_menu(self, ev, global_pos) -> bool:
        caret = self.sel.caret_at(ev.page, ev.pos) if self.sel.over_text(ev.page, ev.pos) else None
        has = self.sel.has_selection()
        if not has and caret is None:
            return False
        self.hide_bar()
        m = QMenu(self.tool.ctx.view)
        if has:
            m.addAction(tr("sel.copy"), lambda: self.run("copy")).setShortcut("Ctrl+C")
            m.addSeparator()
            m.addAction(tr("sel.comment"), lambda: self.run("comment")).setShortcut("Ctrl+Alt+M")
            m.addAction(tr("sel.highlight"), lambda: self.run("highlight"))
            m.addAction(tr("sel.underline"), lambda: self.run("underline"))
            m.addAction(tr("sel.strikeout"), lambda: self.run("strikeout"))
            m.addAction(tr("sel.replace"), lambda: self.run("replace"))
        if caret is not None:
            if has:
                m.addSeparator()
            m.addAction(tr("sel.insert"), lambda c=caret: self.run("insert", c))
        m.addSeparator()
        m.addAction(tr("sel.select_all"), lambda: self.run("select_all")).setShortcut("Ctrl+A")
        m.exec(global_pos)
        if self.sel.has_selection():
            self.show_bar()
        return True


__all__ = ["SelectionUI", "TEXT_EDIT_COLOR", "markup_style"]
