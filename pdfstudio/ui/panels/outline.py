"""Bookmarks (document outline) tree."""

from __future__ import annotations

from PySide6.QtCore import QPointF, Qt
from PySide6.QtWidgets import QStackedWidget, QTreeWidget, QTreeWidgetItem, QVBoxLayout, QWidget

from ...i18n import tr
from ..widgets.util import ThemedIconLabel, label

DestRole = Qt.ItemDataRole.UserRole + 1


class OutlinePanel(QWidget):
    def __init__(self, workspace, parent=None) -> None:
        super().__init__(parent)
        self.workspace = workspace
        lay = QVBoxLayout(self)
        lay.setContentsMargins(8, 6, 8, 8)
        self.tree = QTreeWidget()
        self.tree.setHeaderHidden(True)
        self.tree.setIndentation(14)
        self.tree.setUniformRowHeights(True)
        self.tree.setAnimated(True)
        self.tree.setExpandsOnDoubleClick(False)
        self.tree.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.tree.setTextElideMode(Qt.TextElideMode.ElideRight)
        self.tree.itemClicked.connect(self._activate)
        self.tree.itemActivated.connect(self._activate)

        empty = QWidget()
        ev = QVBoxLayout(empty)
        ev.setContentsMargins(16, 40, 16, 16)
        ev.setSpacing(6)
        ev.addWidget(ThemedIconLabel("bookmarks-simple", 28, "text_subtle"), 0, Qt.AlignmentFlag.AlignHCenter)
        ev.addSpacing(4)
        t = label(tr("shell.outline.empty_title"), "heading")
        t.setAlignment(Qt.AlignmentFlag.AlignCenter)
        d = label(tr("shell.outline.empty_text"), "caption", wrap=True)
        d.setAlignment(Qt.AlignmentFlag.AlignCenter)
        ev.addWidget(t)
        ev.addWidget(d)
        ev.addStretch(1)

        self.stack = QStackedWidget()
        self.stack.addWidget(empty)
        self.stack.addWidget(self.tree)
        lay.addWidget(self.stack)
        self.reload()

    def reload(self) -> None:
        self.tree.clear()
        try:
            toc = self.workspace.pdf.doc.get_toc(simple=False)
        except Exception:
            toc = []
        parents: list[QTreeWidgetItem | None] = [None]
        for entry in toc:
            lvl, title, page = entry[0], entry[1], entry[2]
            dest = entry[3] if len(entry) > 3 else {}
            item = QTreeWidgetItem([title.strip() or "—"])
            item.setToolTip(0, f"{title}  ·  {tr('shell.search.page_n', n=page)}" if page > 0 else title)
            point = None
            if isinstance(dest, dict) and dest.get("to") is not None:
                try:
                    to = dest["to"]
                    point = (float(to.x), float(to.y))
                except Exception:
                    point = None
            item.setData(0, DestRole, (page - 1, point))
            while len(parents) > lvl:
                parents.pop()
            while len(parents) < lvl:
                parents.append(parents[-1])
            parent = parents[-1]
            if parent is None:
                self.tree.addTopLevelItem(item)
            else:
                parent.addChild(item)
            parents.append(item)
        if toc:
            for i in range(self.tree.topLevelItemCount()):
                self.tree.topLevelItem(i).setExpanded(self.tree.topLevelItemCount() < 12)
        self.stack.setCurrentIndex(1 if toc else 0)

    def item_count(self) -> int:
        n = 0
        stack = [self.tree.topLevelItem(i) for i in range(self.tree.topLevelItemCount())]
        while stack:
            it = stack.pop()
            n += 1
            stack.extend(it.child(i) for i in range(it.childCount()))
        return n

    def _activate(self, item: QTreeWidgetItem, _col: int = 0) -> None:
        data = item.data(0, DestRole)
        if not data:
            return
        page, point = data
        if page < 0:
            return
        view = self.workspace.view
        if point is not None:
            # outline 'to' points are in unrotated PDF space; convert via page.rotation_matrix
            try:
                pg = self.workspace.pdf.page(page)
                import pymupdf

                p = pymupdf.Point(point) * pg.rotation_matrix
                view.go_to_page(page, QPointF(p.x, p.y))
                return
            except Exception:
                pass
        view.go_to_page(page)
        if item.childCount():
            item.setExpanded(True)
