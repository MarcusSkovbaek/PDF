"""Sidebar overview of all comments (annotations) in the document.

Grouped by page; each row shows the kind, its colour and – for text markup –
the marked text, otherwise the comment's own text. A compact filter (Alle /
Markeringer / Noter / Tegninger & figurer / Tekstfelter) with a count sits on
top. The list and "Markér kommentar" stay in sync both ways: selecting rows
(Ctrl/Shift for several) selects those comments on the page and scrolls to
them; selecting comments on the page highlights their rows. The list is
rebuilt lazily: only when visible, after the document changed.
"""

from __future__ import annotations

import pymupdf
from PySide6.QtCore import QItemSelectionModel, QRectF, QSize, Qt
from PySide6.QtGui import QIcon, QPainter, QPixmap
from PySide6.QtWidgets import (
    QAbstractItemView,
    QStackedWidget,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from ...core import annotations as core_annots
from ...i18n import tr
from ..theme import tokens as T
from ..widgets.color_button import to_qcolor
from ..widgets.segmented import Segment, SegmentedControl
from ..widgets.util import ThemedIconLabel, label, on_theme_change

InfoRole = Qt.ItemDataRole.UserRole + 1
MARKUP = ("highlight", "underline", "strikeout", "squiggly")
SNIPPET = 70
HIDDEN_KINDS = ("popup", "link", "widget")

#: Filter key -> annotation kinds it shows (``None``: everything).
FILTERS: dict[str, frozenset[str] | None] = {
    "all": None,
    "markup": frozenset(MARKUP),
    "notes": frozenset({"note"}),
    "drawings": frozenset({"ink", "rect", "ellipse", "line", "arrow", "polygon", "polyline"}),
    "text": frozenset({"freetext"}),
}
FILTER_ICONS = {"markup": "highlighter-circle", "notes": "note", "drawings": "scribble-loop", "text": "text-t"}


def _dot(color: tuple | None) -> QIcon:
    pm = QPixmap(14, 14)
    pm.fill(Qt.GlobalColor.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    p.setBrush(to_qcolor(color) if color else T.qcolor("text_subtle"))
    p.setPen(T.qcolor("border_strong"))  # keeps dark colours visible in dark mode
    p.drawEllipse(QRectF(2.5, 2.5, 9, 9))
    p.end()
    return QIcon(pm)


def _snippet(doc: pymupdf.Document, info: core_annots.AnnotInfo) -> str:
    text = ""
    if info.kind in MARKUP:
        try:
            text = _marked_text(doc[info.page], info.xref)
        except Exception:
            text = ""
    text = " ".join((text or info.contents or "").split())
    if len(text) > SNIPPET:
        text = text[:SNIPPET - 1].rstrip() + "…"
    return text


def _marked_text(page: pymupdf.Page, xref: int) -> str:
    """The words a text markup covers: words whose centre lies inside one of
    its quads (the annotation's /Rect is padded and would pick up neighbours)."""
    annot = page.load_annot(xref)
    verts = annot.vertices or []
    quads = [pymupdf.Quad(verts[i:i + 4]).rect for i in range(0, len(verts) - 3, 4)]
    words = []
    for x0, y0, x1, y1, w, *_ in page.get_text("words"):
        c = pymupdf.Point((x0 + x1) / 2, (y0 + y1) / 2)
        if any(c in q for q in quads):
            words.append(w)
    return " ".join(words)


def matches(kind: str, filter_key: str) -> bool:
    kinds = FILTERS.get(filter_key)
    return kinds is None or kind in kinds


class CommentsPanel(QWidget):
    def __init__(self, workspace, parent=None) -> None:
        super().__init__(parent)
        self.workspace = workspace
        self._dirty = True
        self._filter = "all"
        self._syncing = False
        self._linked_tool = None
        self._infos: list[core_annots.AnnotInfo] = []
        lay = QVBoxLayout(self)
        lay.setContentsMargins(8, 6, 8, 8)
        lay.setSpacing(6)

        # filter + count
        self.header = QWidget()
        hv = QVBoxLayout(self.header)
        hv.setContentsMargins(2, 0, 2, 0)
        hv.setSpacing(6)
        self.filter = SegmentedControl(
            [Segment("all", tr("comments.filter.all"), None, tr("comments.filter.all"))]
            + [Segment(k, "", FILTER_ICONS[k], tr(f"comments.filter.{k}")) for k in FILTERS if k != "all"],
            height=30, expand=True, min_seg=36)
        self.filter.currentChanged.connect(self.set_filter)
        self.count = label("", "caption")
        self.count.setContentsMargins(4, 0, 0, 0)
        hv.addWidget(self.filter)
        hv.addWidget(self.count)
        lay.addWidget(self.header)

        self.tree = QTreeWidget()
        self.tree.setObjectName("CommentsTree")
        self.tree.setHeaderHidden(True)
        self.tree.setIndentation(0)  # rows highlight as one rounded shape
        self.tree.setRootIsDecorated(False)
        self.tree.setIconSize(QSize(14, 14))
        self.tree.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.tree.setWordWrap(True)
        self.tree.setUniformRowHeights(False)
        self.tree.setTextElideMode(Qt.TextElideMode.ElideNone)
        self.tree.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.tree.itemClicked.connect(self._jump)
        self.tree.itemActivated.connect(self._jump)
        self.tree.currentItemChanged.connect(lambda cur, _prev: self._jump(cur) if not self._syncing else None)
        self.tree.itemSelectionChanged.connect(self._selection_from_tree)

        self.stack = QStackedWidget()
        self.stack.addWidget(self._state_page("chat-circle-text", tr("comments.empty.title"),
                                              tr("comments.empty.text")))
        self.stack.addWidget(self.tree)
        self.none_page = self._state_page("funnel-simple", "", tr("comments.filter.none.hint"))
        self.stack.addWidget(self.none_page)
        lay.addWidget(self.stack, 1)
        on_theme_change(self, lambda: self._populate() if self._infos else None)  # dots follow the theme

    @staticmethod
    def _state_page(icon_name: str, title: str, text: str) -> QWidget:
        page = QWidget()
        ev = QVBoxLayout(page)
        ev.setContentsMargins(16, 40, 16, 16)
        ev.setSpacing(6)
        ev.addWidget(ThemedIconLabel(icon_name, 28, "text_subtle"), 0, Qt.AlignmentFlag.AlignHCenter)
        ev.addSpacing(4)
        t = label(title, "heading")
        t.setAlignment(Qt.AlignmentFlag.AlignCenter)
        d = label(text, "caption", wrap=True)
        d.setAlignment(Qt.AlignmentFlag.AlignCenter)
        ev.addWidget(t)
        ev.addWidget(d)
        ev.addStretch(1)
        page.title_label = t  # type: ignore[attr-defined]
        return page

    # -- lifecycle ----------------------------------------------------------
    def document_changed(self) -> None:
        self._dirty = True
        if self.isVisible():
            self.reload()

    def showEvent(self, ev) -> None:  # noqa: N802
        super().showEvent(ev)
        self._link_tool()
        if self._dirty:
            self.reload()

    def set_filter(self, key: str) -> None:
        if key not in FILTERS:
            return
        self._filter = key
        if self.filter.current_key() != key:
            self.filter.set_current_key(key, animate=self.isVisible())
        self._populate()

    def reload(self) -> None:
        self._dirty = False
        self._link_tool()
        doc = self.workspace.pdf.doc
        infos: list[core_annots.AnnotInfo] = []
        for page in range(doc.page_count):
            try:
                infos.extend(a for a in core_annots.list_annotations(doc, page) if a.kind not in HIDDEN_KINDS)
            except Exception:
                continue
        self._infos = infos
        self._populate()

    def _populate(self) -> None:
        doc = self.workspace.pdf.doc
        self._syncing = True
        try:
            self.tree.clear()
            total = len(self._infos)
            shown = [a for a in self._infos if matches(a.kind, self._filter)]
            pages: dict[int, list[core_annots.AnnotInfo]] = {}
            for a in shown:
                pages.setdefault(a.page, []).append(a)
            for page, infos in pages.items():
                head = QTreeWidgetItem([tr("comments.page", n=page + 1, count=len(infos))])
                head.setFlags(head.flags() & ~Qt.ItemFlag.ItemIsSelectable)
                f = head.font(0)
                f.setBold(True)
                head.setFont(0, f)
                self.tree.addTopLevelItem(head)
                for a in infos:
                    kind = tr(f"annot.kind.{a.kind}")
                    snippet = _snippet(doc, a)
                    # two lines: the kind, then what it says (wraps instead of eliding)
                    item = QTreeWidgetItem([f"{kind}\n{snippet}" if snippet else kind])
                    item.setIcon(0, _dot(a.color))
                    item.setToolTip(0, (a.contents or snippet or kind))
                    item.setData(0, InfoRole, (a.page, a.xref))
                    head.addChild(item)
                head.setExpanded(True)
            self._update_header(total, len(shown))
            if not total:
                self.stack.setCurrentIndex(0)
            elif not shown:
                self.none_page.title_label.setText(tr(f"comments.filter.none.{self._filter}"))
                self.stack.setCurrentIndex(2)
            else:
                self.stack.setCurrentIndex(1)
        finally:
            self._syncing = False
        self._sync_from_tool()

    def _update_header(self, total: int, shown: int) -> None:
        self.header.setVisible(total > 0)
        if self._filter == "all":
            text = tr("comments.count.one") if total == 1 else tr("comments.count", n=total)
        else:
            text = tr("comments.count.filtered", shown=shown, total=total,
                      name=tr(f"comments.filter.{self._filter}"))
        self.count.setText(text)
        tips = {}
        for key in FILTERS:
            n = sum(1 for a in self._infos if matches(a.kind, key))
            tips[key] = tr("comments.filter.tip", name=tr(f"comments.filter.{key}"), n=n)
        for seg in getattr(self.filter, "_segments", []):  # counts in the tooltips
            seg.tooltip = tips.get(seg.key, seg.tooltip)

    # -- rows <-> "Markér kommentar" ----------------------------------------------
    def _items(self):
        for i in range(self.tree.topLevelItemCount()):
            head = self.tree.topLevelItem(i)
            for j in range(head.childCount()):
                yield head.child(j)

    def _link_tool(self) -> None:
        """Follow the selection of this document's "Markér kommentar" tool."""
        ws = self.workspace
        tool = ws.tool_instance("annotate.select") if hasattr(ws, "tool_instance") else None
        if tool is None or tool is self._linked_tool or not hasattr(tool, "selectionChanged"):
            return
        tool.selectionChanged.connect(self._sync_from_tool)
        self._linked_tool = tool

    def _sync_from_tool(self) -> None:
        tool = self._linked_tool
        if tool is None or self._syncing:
            return
        keys = {(a.page, a.xref) for a in getattr(tool, "selection", [])}
        self._syncing = True
        try:
            first = None
            for item in self._items():
                on = tuple(item.data(0, InfoRole) or ()) in keys
                item.setSelected(on)
                if on and first is None:
                    first = item
            if first is not None:
                self.tree.setCurrentItem(first, 0, QItemSelectionModel.SelectionFlag.NoUpdate)
                self.tree.scrollToItem(first)
        finally:
            self._syncing = False

    def _selection_from_tree(self) -> None:
        if self._syncing:
            return
        keys = [tuple(it.data(0, InfoRole)) for it in self.tree.selectedItems() if it.data(0, InfoRole)]
        ws = self.workspace
        tool = ws.tool
        if tool is None or tool.id != "annotate.select":
            if not keys:
                return
            ws.set_tool("annotate.select")
            tool = ws.tool
        if tool is None or not hasattr(tool, "set_selection"):
            return
        doc = ws.pdf.doc
        by_page: dict[int, dict[int, core_annots.AnnotInfo]] = {}
        infos = []
        for page, xref in keys:
            if page not in by_page:
                by_page[page] = {a.xref: a for a in core_annots.list_annotations(doc, page)}
            if xref in by_page[page]:
                infos.append(by_page[page][xref])
        self._syncing = True
        try:
            tool.set_selection(infos)
        finally:
            self._syncing = False

    # -- navigation ---------------------------------------------------------
    def _jump(self, item: QTreeWidgetItem | None) -> None:
        """Scroll the page view to a row's comment (if it is not in view)."""
        data = item.data(0, InfoRole) if item is not None else None
        if not data:
            return
        page, xref = data
        ws = self.workspace
        info = next((a for a in core_annots.list_annotations(ws.pdf.doc, page) if a.xref == xref), None)
        if info is None:
            return
        r = info.rect
        ws.view.ensure_visible_rect(page, QRectF(r.x0, r.y0, r.width, r.height).adjusted(-24, -24, 24, 24))

    def _activate(self, item: QTreeWidgetItem) -> None:
        """Select just this row's comment and show it."""
        if not item.data(0, InfoRole):
            return
        self.tree.clearSelection()
        item.setSelected(True)  # -> _selection_from_tree selects it on the page
        self.tree.setCurrentItem(item)
        self._jump(item)
