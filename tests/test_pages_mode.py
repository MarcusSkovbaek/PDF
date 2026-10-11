import pytest

from pdfstudio.ui.panels.pages_panel import format_pages
from tests.test_ui_shell import win  # noqa: F401  (fixture)


def test_format_pages():
    assert format_pages([]) == ""
    assert format_pages([0]) == "1"
    assert format_pages([4, 0, 1, 2]) == "1–3, 5"
    assert format_pages([1, 3, 5, 6]) == "2, 4, 6–7"


@pytest.fixture
def pages_ws(win, qapp, sample_pdf_path):  # noqa: F811
    win.open_path(sample_pdf_path)
    ws = win.current_workspace()
    ws.set_mode("pages")
    qapp.processEvents()
    return ws


def test_toolbar_has_no_stale_buttons(pages_ws):
    from PySide6.QtWidgets import QAbstractButton

    pages_ws.set_mode("view")      # build the view-mode pill first, then switch back
    pages_ws.set_mode("pages")
    names = [b.accessibleName() for b in pages_ws.toolbar.findChildren(QAbstractButton)]
    assert "view.select" not in names and "view.hand" not in names
    assert names[:2] == ["pages.rotate_left", "pages.rotate_right"]


def test_panel_follows_selection_and_rotates(pages_ws, qapp):
    panel = pages_ws.inspector._pages_panel
    assert panel is not None and pages_ws.inspector.scroll.widget() is panel
    view = pages_ws.organizer.view
    view.select_pages([0, 1])
    qapp.processEvents()
    assert panel.title.text() == "2 sider markeret"
    assert panel.detail.text() == "Sider 1–2"
    rotate = next(b for b, aid in panel._buttons if aid == "pages.rotate_right")
    assert rotate.isEnabled()
    before = [pages_ws.pdf.page(i).rotation for i in range(2)]
    rotate.click()
    qapp.processEvents()                       # selection is restored after the edit
    assert [pages_ws.pdf.page(i).rotation for i in range(2)] == [(r + 90) % 360 for r in before]
    view.clearSelection()
    qapp.processEvents()
    assert panel.title.text() == "Ingen sider markeret"


def test_view_mode_restores_doc_info(pages_ws):
    pages_ws.set_mode("view")
    assert pages_ws.inspector.scroll.widget() is not pages_ws.inspector._pages_panel


def test_grid_is_centered(pages_ws, qapp):
    view = pages_ws.organizer.view
    pages_ws.window().resize(1440, 900)
    qapp.processEvents()
    view._center_grid()
    left = view.viewportMargins().left()
    assert left == view.viewportMargins().right() and left >= 0
