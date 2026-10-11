"""Inspector content for "Sider" mode: what is selected and what can be done
with it. Buttons are bound to the window's registered actions, so labels,
icons, shortcuts and enabled state stay in sync with menus and toolbars."""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QFrame, QSizePolicy, QToolButton, QVBoxLayout, QWidget

from ...i18n import tr
from ..theme.icons import icon
from ..widgets.util import label, on_theme_change

SELECTION_ACTIONS = ("pages.rotate_left", "pages.rotate_right", "pages.duplicate", "pages.extract", "pages.delete")
DOCUMENT_ACTIONS = ("pages.insert_blank", "pages.insert_file", "tools.split")


def format_pages(pages: list[int]) -> str:
    """0-based indices -> compact 1-based text: [0, 1, 2, 4] -> "1–3, 5"."""
    parts: list[str] = []
    nums = sorted(set(pages))
    i = 0
    while i < len(nums):
        j = i
        while j + 1 < len(nums) and nums[j + 1] == nums[j] + 1:
            j += 1
        a, b = nums[i] + 1, nums[j] + 1
        parts.append(str(a) if a == b else f"{a}–{b}")
        i = j + 1
    return ", ".join(parts)


class PagesPanel(QWidget):
    def __init__(self, workspace) -> None:
        super().__init__()
        self.workspace = workspace
        v = QVBoxLayout(self)
        v.setContentsMargins(16, 4, 16, 16)
        v.setSpacing(6)

        card = QFrame()
        card.setObjectName("Card")
        cl = QVBoxLayout(card)
        cl.setContentsMargins(14, 12, 14, 12)
        cl.setSpacing(2)
        self.title = label("", "heading")
        self.detail = label("", "muted", wrap=True)
        cl.addWidget(self.title)
        cl.addWidget(self.detail)
        v.addWidget(card)
        v.addSpacing(10)

        self._buttons: list[tuple[QToolButton, str]] = []
        v.addWidget(label(tr("pagespanel.section.selected"), "section"))
        for aid in SELECTION_ACTIONS:
            self._add_button(v, aid)
        v.addSpacing(10)
        v.addWidget(label(tr("pagespanel.section.document"), "section"))
        for aid in DOCUMENT_ACTIONS:
            self._add_button(v, aid)
        v.addSpacing(12)
        v.addWidget(label(tr("pagespanel.tip"), "caption", wrap=True))
        v.addStretch(1)
        on_theme_change(self, self._refresh_icons)
        self.refresh()

    def _add_button(self, layout: QVBoxLayout, aid: str) -> None:
        act = self.workspace.action(aid)
        if act is None:
            return
        b = QToolButton()
        b.setDefaultAction(act)
        b.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
        b.setAutoRaise(True)
        b.setCursor(Qt.CursorShape.PointingHandCursor)
        b.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        b.setProperty("variant", "row")
        b.setAccessibleName(aid)
        layout.addWidget(b)
        self._buttons.append((b, aid))
        self._refresh_icon(b, aid)

    def _refresh_icon(self, b: QToolButton, aid: str) -> None:
        reg = self.workspace.window_actions
        name = getattr(reg.get(aid), "icon", None) if reg is not None else None
        if name:
            b.setIcon(icon(name, "danger" if aid == "pages.delete" else "text_muted"))

    def _refresh_icons(self) -> None:
        for b, aid in self._buttons:
            self._refresh_icon(b, aid)

    def refresh(self) -> None:
        org = getattr(self.workspace, "organizer", None)
        # the grid's real selection (workspace.selected_pages() falls back to
        # the current page, which would misreport "nothing selected")
        sel = org.view.selected_pages() if org is not None and self.workspace.mode == "pages" else []
        if not sel:
            self.title.setText(tr("pagespanel.none"))
            self.detail.setText(tr("pagespanel.none.hint"))
        else:
            self.title.setText(tr("pagespanel.one") if len(sel) == 1 else tr("pagespanel.many", n=len(sel)))
            key = "pagespanel.which.one" if len(sel) == 1 else "pagespanel.which.many"
            self.detail.setText(tr(key, pages=format_pages(sel)))
        for b, aid in self._buttons:
            if aid in SELECTION_ACTIONS:
                b.setEnabled(bool(sel) and b.defaultAction().isEnabled())
