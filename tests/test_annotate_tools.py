"""UI flow of the "Kommentér" tools."""

import pytest
from PySide6.QtCore import QEvent, QPointF, Qt
from PySide6.QtGui import QKeyEvent

from pdfstudio.core.annotations import list_annotations
from pdfstudio.ui.viewer.tools.base import ToolMouseEvent
from tests.test_ui_shell import win  # noqa: F401  (fixture)

L = Qt.MouseButton.LeftButton
NONE = Qt.MouseButton.NoButton


@pytest.fixture
def ws(win, qapp, sample_pdf_path):  # noqa: F811
    win.open_path(sample_pdf_path)
    w = win.current_workspace()
    w.set_mode("annotate")
    qapp.processEvents()
    return w


def ev(view, page, x, y, button=L, buttons=None, mods=Qt.KeyboardModifier.NoModifier):
    pos = QPointF(x, y)
    scene = view.page_to_scene(page, pos)
    return ToolMouseEvent(page, pos, scene, QPointF(view.mapFromScene(scene)),
                          buttons if buttons is not None else button, button, mods, view)


def drag(tool, view, page, a, b, mods=Qt.KeyboardModifier.NoModifier):
    tool.mouse_press(ev(view, page, *a, mods=mods))
    mid = ((a[0] + b[0]) / 2, (a[1] + b[1]) / 2)
    tool.mouse_move(ev(view, page, *mid, button=NONE, buttons=L, mods=mods))
    tool.mouse_move(ev(view, page, *b, button=NONE, buttons=L, mods=mods))
    tool.mouse_release(ev(view, page, *b, buttons=NONE, mods=mods))


def kinds(ws, page=0):
    return [a.kind for a in list_annotations(ws.pdf.doc, page)]


def test_highlight_across_two_lines(ws):
    ws.set_tool("annotate.highlight")
    page = ws.pdf.page(0)
    a = page.search_for("broedtekst")[0]
    b = page.search_for("samme")[0]
    drag(ws.tool, ws.view, 0, (a.x0 + 1, (a.y0 + a.y1) / 2), (b.x1 - 1, (b.y0 + b.y1) / 2))
    annots = list_annotations(ws.pdf.doc, 0)
    assert [x.kind for x in annots] == ["highlight"]
    assert annots[0].rect.y1 > b.y0           # covers the second line too
    assert not ws.tool.has_selection()


def test_underline_and_strike(ws):
    page = ws.pdf.page(0)
    r = page.search_for("Hello")[0]
    for tid in ("annotate.underline", "annotate.strikeout"):
        ws.set_tool(tid)
        drag(ws.tool, ws.view, 0, (r.x0 + 1, r.y0 + r.height / 2), (r.x1 - 1, r.y0 + r.height / 2))
    assert kinds(ws) == ["underline", "strikeout"]


def test_ink_and_shapes(ws):
    ws.set_tool("annotate.ink")
    drag(ws.tool, ws.view, 0, (100, 400), (200, 450))
    ws.set_tool("annotate.shape")
    for kind in ("rect", "ellipse", "arrow"):
        ws.tool.set_kind(kind)
        drag(ws.tool, ws.view, 0, (100, 500), (220, 560))
    ws.tool.set_kind("rect")
    drag(ws.tool, ws.view, 0, (300, 300), (301, 301))      # a click: nothing
    assert kinds(ws) == ["ink", "rect", "ellipse", "arrow"]


def test_shift_makes_square(ws):
    ws.set_tool("annotate.shape")
    ws.tool.set_kind("rect")
    drag(ws.tool, ws.view, 0, (100, 500), (200, 530), mods=Qt.KeyboardModifier.ShiftModifier)
    r = list_annotations(ws.pdf.doc, 0)[-1].rect
    assert abs(r.width - r.height) < 1.0


def test_note_and_edit(ws, monkeypatch):
    from pdfstudio.ui.viewer.tools import _annot_common

    monkeypatch.setattr(_annot_common, "_AUTO_NOTE", "Husk at tjekke tallet")
    ws.set_tool("annotate.note")
    ws.tool.mouse_release(ev(ws.view, 0, 400, 120))
    note = list_annotations(ws.pdf.doc, 0)[-1]
    assert note.kind == "note" and note.contents == "Husk at tjekke tallet"
    ws.set_tool("annotate.select")
    c = note.rect
    ws.tool.mouse_press(ev(ws.view, 0, (c.x0 + c.x1) / 2, (c.y0 + c.y1) / 2))
    ws.tool.mouse_release(ev(ws.view, 0, (c.x0 + c.x1) / 2, (c.y0 + c.y1) / 2, buttons=NONE))
    assert ws.tool.selected is not None
    monkeypatch.setattr(_annot_common, "_AUTO_NOTE", "Rettet note")
    ws.tool.mouse_double_click(ev(ws.view, 0, (c.x0 + c.x1) / 2, (c.y0 + c.y1) / 2))
    assert list_annotations(ws.pdf.doc, 0)[-1].contents == "Rettet note"


def test_select_move_restyle_delete(ws):
    ws.set_tool("annotate.shape")
    ws.tool.set_kind("rect")
    drag(ws.tool, ws.view, 0, (100, 500), (200, 560))
    ws.set_tool("annotate.select")
    tool = ws.tool
    drag(tool, ws.view, 0, (100, 530), (150, 580))       # grab the border, move by (50, 50)
    r = list_annotations(ws.pdf.doc, 0)[-1].rect
    assert abs(r.x0 - 150) < 1.5 and abs(r.y0 - 550) < 1.5
    assert tool.selected is not None
    panel = tool.properties_widget()
    panel._set_color((0.1, 0.5, 0.9))
    assert abs(list_annotations(ws.pdf.doc, 0)[-1].color[2] - 0.9) < 0.01
    tool.key_press(QKeyEvent(QEvent.Type.KeyPress, Qt.Key.Key_Delete, Qt.KeyboardModifier.NoModifier))
    assert kinds(ws) == []
    ws.pdf.undo()
    assert kinds(ws) == ["rect"]


def test_shared_shortcut_follows_mode(win, ws):  # noqa: F811
    hand, hl = win.actions.action("tool.view.hand"), win.actions.action("tool.annotate.highlight")
    owners = [a for a in (hand, hl) if a.shortcut().toString() == "H"]
    assert len(owners) == 1                             # never ambiguous
    act = owners[0]
    act.trigger()                                       # in annotate mode -> highlight
    assert ws.tool.id == "annotate.highlight"
    ws.set_mode("view")
    act.trigger()
    assert ws.tool.id == "view.hand"


def test_inspector_follows_annotation_selection(ws):
    ws.set_tool("annotate.shape")
    ws.tool.set_kind("rect")
    drag(ws.tool, ws.view, 0, (100, 500), (200, 560))
    ws.set_tool("annotate.select")
    ws.tool.select(list_annotations(ws.pdf.doc, 0)[-1])
    from pdfstudio.ui.viewer.tools._annot_common import AnnotStylePanel

    assert isinstance(ws.inspector.scroll.widget(), AnnotStylePanel)
    ws.tool.select(None)
    assert not isinstance(ws.inspector.scroll.widget(), AnnotStylePanel)


def test_resize_with_corner_handle(ws):
    ws.set_tool("annotate.shape")
    ws.tool.set_kind("rect")
    drag(ws.tool, ws.view, 0, (100, 500), (200, 560))
    ws.set_tool("annotate.select")
    tool = ws.tool
    tool.select(list_annotations(ws.pdf.doc, 0)[-1])
    assert len(tool._frame.childItems()) == 4                     # corner handles
    corner = tool._corners()[2]                                   # bottom-right (frame is padded)
    assert tool.handle_at(0, corner) == 2
    drag(tool, ws.view, 0, (corner.x(), corner.y()), (260, 620))
    r = list_annotations(ws.pdf.doc, 0)[-1].rect
    assert abs(r.x0 - 100) < 1.5 and abs(r.y0 - 500) < 1.5      # opposite corner fixed
    assert abs(r.x1 - 260) < 1.5 and abs(r.y1 - 620) < 1.5
    ws.pdf.undo()
    r = list_annotations(ws.pdf.doc, 0)[-1].rect
    assert abs(r.x1 - 200) < 1.5


def test_note_has_no_resize_handles(ws, monkeypatch):
    from pdfstudio.ui.viewer.tools import _annot_common

    monkeypatch.setattr(_annot_common, "_AUTO_NOTE", "x")
    ws.set_tool("annotate.note")
    ws.tool.mouse_release(ev(ws.view, 0, 400, 120))
    ws.set_tool("annotate.select")
    ws.tool.select(list_annotations(ws.pdf.doc, 0)[-1])
    assert ws.tool._frame.childItems() == []
