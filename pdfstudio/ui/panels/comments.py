"""Sidebar overview of all comments (annotations) in the document.

Grouped by page; each row shows the kind, its colour and – for text markup –
the marked text, otherwise the comment's own text. Clicking a row jumps to
the comment and selects it with "Markér kommentar". The list is rebuilt
lazily: only when visible, after the document changed.
"""

from __future__ import annotations

import pymupdf
from PySide6.QtCore import QPointF, QSize, Qt
from PySide6.QtGui import QColor, QIcon, QPainter, QPixmap
from PySide6.QtWidgets import QStackedWidget, QTreeWidget, QTreeWidgetItem, QVBoxLayout, QWidget

from ...core import annotations as core_annots
from ...i18n import tr
from ..widgets.color_button import to_qcolor
from ..widgets.util import ThemedIconLabel, label

InfoRole = Qt.ItemDataRole.UserRole + 1
MARKUP = ("highlight", "underline", "strikeout", "squiggly")
SNIPPET = 70


def _dot(color: tuple | None) -> QIcon:
    pm = QPixmap(14, 14)
    pm.fill(Qt.GlobalColor.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    p.setBrush(to_qcolor(color) if color else QColor("#888888"))
    p.setPen(QColor(0, 0, 0, 50))
    p.drawEllipse(2, 2, 10, 10)
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


class CommentsPanel(QWidget):
    def __init__(self, workspace, parent=None) -> None:
        super().__init__(parent)
        self.workspace = workspace
        self._dirty = True
        lay = QVBoxLayout(self)
        lay.setContentsMargins(8, 6, 8, 8)
        self.tree = QTreeWidget()
        self.tree.setHeaderHidden(True)
        self.tree.setIndentation(10)
        self.tree.setIconSize(QSize(14, 14))
        self.tree.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.tree.setWordWrap(True)
        self.tree.setUniformRowHeights(False)
        self.tree.setTextElideMode(Qt.TextElideMode.ElideNone)
        self.tree.itemClicked.connect(self._activate)
        self.tree.itemActivated.connect(self._activate)

        empty = QWidget()
        ev = QVBoxLayout(empty)
        ev.setContentsMargins(16, 40, 16, 16)
        ev.setSpacing(6)
        ev.addWidget(ThemedIconLabel("chat-circle-text", 28, "text_subtle"), 0, Qt.AlignmentFlag.AlignHCenter)
        ev.addSpacing(4)
        t = label(tr("comments.empty.title"), "heading")
        t.setAlignment(Qt.AlignmentFlag.AlignCenter)
        d = label(tr("comments.empty.text"), "caption", wrap=True)
        d.setAlignment(Qt.AlignmentFlag.AlignCenter)
        ev.addWidget(t)
        ev.addWidget(d)
        ev.addStretch(1)

        self.stack = QStackedWidget()
        self.stack.addWidget(empty)
        self.stack.addWidget(self.tree)
        lay.addWidget(self.stack)

    # -- lifecycle ----------------------------------------------------------
    def document_changed(self) -> None:
        self._dirty = True
        if self.isVisible():
            self.reload()

    def showEvent(self, ev) -> None:  # noqa: N802
        super().showEvent(ev)
        if self._dirty:
            self.reload()

    def reload(self) -> None:
        self._dirty = False
        self.tree.clear()
        doc = self.workspace.pdf.doc
        total = 0
        for page in range(doc.page_count):
            try:
                infos = core_annots.list_annotations(doc, page)
            except Exception:
                continue
            infos = [a for a in infos if a.kind not in ("popup", "link", "widget")]
            if not infos:
                continue
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
                total += 1
            head.setExpanded(True)
        self.stack.setCurrentIndex(1 if total else 0)

    # -- navigation ---------------------------------------------------------
    def _activate(self, item: QTreeWidgetItem) -> None:
        data = item.data(0, InfoRole)
        if not data:
            return
        page, xref = data
        ws = self.workspace
        info = next((a for a in core_annots.list_annotations(ws.pdf.doc, page) if a.xref == xref), None)
        if info is None:
            return
        ws.view.go_to_page(page, QPointF(info.rect.x0, max(0.0, info.rect.y0 - 40)))
        ws.set_tool("annotate.select")
        tool = ws.tool
        if tool is not None and hasattr(tool, "select"):
            tool.select(info)
