"""UI flow of "Markér objekt" (select / move / resize / replace / delete images)."""

from __future__ import annotations

import pymupdf
import pytest
from PySide6.QtCore import QEvent, QPointF, Qt
from PySide6.QtGui import QGuiApplication, QKeyEvent

from pdfstudio.core import insert
from pdfstudio.core import objects as ob
from tests.conftest import make_png
from tests.test_annotate_tools import drag, ev
from tests.test_ui_shell import win  # noqa: F401  (fixture)

L = Qt.MouseButton.LeftButton
NONE = Qt.MouseButton.NoButton
NOMOD = Qt.KeyboardModifier.NoModifier
SHIFT = Qt.KeyboardModifier.ShiftModifier
MM = 25.4 / 72


@pytest.fixture
def ws(win, qapp, sample_pdf_path):  # noqa: F811
    win.open_path(sample_pdf_path)
    w = win.current_workspace()
    w.set_mode("edit")
    w.set_tool("edit.object")
    w.view.go_to_page(1)
    qapp.processEvents()
    return w


def images(ws, page=1):
    return ob.list_images(ws.pdf.doc, page)


def key(tool, k, mods=NOMOD) -> bool:
    return tool.key_press(QKeyEvent(QEvent.Type.KeyPress, k, mods))


def click(tool, view, page, x, y, mods=NOMOD):
    tool.mouse_press(ev(view, page, x, y, mods=mods))
    tool.mouse_release(ev(view, page, x, y, buttons=NONE, mods=mods))


def rect_close(a, b, tol=0.6) -> bool:
    return all(abs(x - y) <= tol for x, y in zip(pymupdf.Rect(a), pymupdf.Rect(b)))


# the sample's page 2 (index 1) has one 40x30 px image at (72, 500)-(232, 620)
CX, CY = 152, 560


def test_registered_between_the_edit_tools(win, ws):  # noqa: F811
    from pdfstudio.ui.viewer.tools.base import ToolRegistry

    ids = [t.id for t in ToolRegistry.for_mode("edit")]
    assert ids.index("edit.text") < ids.index("edit.object") < ids.index("edit.add_text")
    assert win.actions.action("tool.edit.object").shortcut().toString() == "O"
    assert ws.tool.id == "edit.object"


def test_hover_click_selects_and_inspector_shows_details(ws):
    from pdfstudio.ui.viewer.tools.edit_object import ImageObjectPanel

    tool = ws.tool
    panel = ws.inspector.scroll.widget()
    assert isinstance(panel, ImageObjectPanel)
    assert not panel.edit_box.isVisibleTo(panel)
    tool.mouse_move(ev(ws.view, 1, CX, CY, button=NONE, buttons=NONE))
    assert tool._hover is not None                                    # dashed outline
    tool.mouse_move(ev(ws.view, 1, 400, 700, button=NONE, buttons=NONE))
    assert tool._hover is None
    click(tool, ws.view, 1, CX, CY)
    assert tool.selected is not None and tool.selected.index == 0
    assert tool._frame is not None and len(tool._frame.childItems()) == 4    # corner handles
    assert panel.edit_box.isVisibleTo(panel)
    assert "40 × 30 px" in panel.detail.text()
    assert panel.x.value() == pytest.approx(72 * MM, abs=0.05)
    assert panel.w.value() == pytest.approx(160 * MM, abs=0.05)
    click(tool, ws.view, 1, 400, 700)                                 # empty page: deselect
    assert tool.selected is None and tool._frame is None


def test_drag_moves_with_live_preview_as_one_undo_step(ws):
    tool = ws.tool
    tool.mouse_press(ev(ws.view, 1, CX, CY))
    tool.mouse_move(ev(ws.view, 1, CX + 20, CY + 15, button=NONE, buttons=L))
    tool.mouse_move(ev(ws.view, 1, CX + 50, CY + 40, button=NONE, buttons=L))
    # lifted: the old place is covered, a translucent copy follows the pointer
    assert tool._lift is not None and tool._ghost is not None
    assert tool._ghost.opacity() < 1
    ghost = tool._ghost.mapRectToParent(tool._ghost.boundingRect())
    assert abs(ghost.left() - 122) < 0.6 and abs(ghost.top() - 540) < 0.6
    assert abs(tool._frame.rect().left() - 122) < 0.6
    assert "mm" in tool._badge.text()
    assert not ws.pdf.history.can_undo                               # nothing written yet
    tool.mouse_release(ev(ws.view, 1, CX + 50, CY + 40, buttons=NONE))
    assert rect_close(images(ws)[0].rect, (122, 540, 282, 660))
    assert ws.pdf.history.undo_label == "Flyt billede" and len(ws.pdf.history._undo) == 1
    assert tool.selected is not None and rect_close(tool.selected.rect, (122, 540, 282, 660))
    assert tool._ghost is None and tool._drag is None
    ws.view.render_visible_now()
    tool._clear_stale_previews()
    ws.pdf.undo()
    assert rect_close(images(ws)[0].rect, (72, 500, 232, 620))
    assert tool.selected is not None and rect_close(tool.selected.rect, (72, 500, 232, 620))


def test_shift_drag_moves_along_one_axis(ws):
    tool = ws.tool
    drag(tool, ws.view, 1, (CX, CY), (CX + 60, CY + 8), mods=SHIFT)
    assert rect_close(images(ws)[0].rect, (132, 500, 292, 620))


def test_click_without_drag_writes_nothing(ws):
    click(ws.tool, ws.view, 1, CX, CY)
    drag(ws.tool, ws.view, 1, (CX, CY), (CX + 0.5, CY + 0.5))
    assert not ws.pdf.history.can_undo


def test_corner_resize_keeps_aspect_and_shift_frees_it(ws):
    tool = ws.tool
    click(tool, ws.view, 1, CX, CY)
    assert tool.handle_at(1, QPointF(232, 620)) == 2
    drag(tool, ws.view, 1, (232, 620), (312, 640))                   # aspect kept: 1.5x
    assert rect_close(images(ws)[0].rect, (72, 500, 312, 680))
    assert ws.pdf.history.undo_label == "Skalér billede"
    drag(tool, ws.view, 1, (312, 680), (300, 600), mods=SHIFT)       # free
    assert rect_close(images(ws)[0].rect, (72, 500, 300, 600))
    # the top-left corner: the bottom-right one stays put
    drag(tool, ws.view, 1, (72, 500), (100, 520), mods=SHIFT)
    assert rect_close(images(ws)[0].rect, (100, 520, 300, 600))


def test_keyboard_nudge_delete_escape_tab(ws):
    tool = ws.tool
    assert key(tool, Qt.Key.Key_Tab)                                 # Tab selects the page's first image
    assert tool.selected is not None
    for _ in range(3):
        key(tool, Qt.Key.Key_Right)
    key(tool, Qt.Key.Key_Down, SHIFT)
    assert rect_close(images(ws)[0].rect, (75, 510, 235, 630))
    assert len(ws.pdf.history._undo) == 1                            # one burst = one undo step
    ws.pdf.undo()
    assert rect_close(images(ws)[0].rect, (72, 500, 232, 620))
    assert key(tool, Qt.Key.Key_Escape) and tool.selected is None
    key(tool, Qt.Key.Key_Tab)
    assert key(tool, Qt.Key.Key_Delete)
    assert images(ws) == [] and tool.selected is None
    assert "Venstre kolonne" in ws.pdf.doc[1].get_text()            # nothing else removed
    ws.pdf.undo()
    assert len(images(ws)) == 1


def test_inspector_fields_with_aspect_lock(ws):
    tool = ws.tool
    click(tool, ws.view, 1, CX, CY)
    panel = ws.inspector.scroll.widget()
    panel.w.setValue(80 / MM)                                        # lock on: height follows
    r = images(ws)[0].rect
    assert abs(r.width - 80) < 0.3 and abs(r.height - 60) < 0.3 and abs(r.x0 - 72) < 0.3
    assert panel.h.value() == pytest.approx(60 * MM, abs=0.1)
    panel.lock.setChecked(False)
    panel.h.setValue(100 * MM)
    r = images(ws)[0].rect
    assert abs(r.width - 80) < 0.3 and abs(r.height - 100) < 0.3
    panel.x.setValue(20)                                             # mm from the left edge
    panel.y.setValue(30)
    r = images(ws)[0].rect
    assert abs(r.x0 - 20 / MM) < 0.3 and abs(r.y0 - 30 / MM) < 0.3
    assert len(ws.pdf.history._undo) == 4


def test_replace_save_rotate_flip_from_the_inspector(ws, tmp_path, monkeypatch):
    from pdfstudio.ui.viewer.tools import edit_object

    tool = ws.tool
    click(tool, ws.view, 1, CX, CY)
    panel = ws.inspector.scroll.widget()
    new = tmp_path / "logo.png"
    new.write_bytes(make_png(400, 200, (20, 160, 90)))
    monkeypatch.setattr(edit_object, "_AUTO_OPEN", str(new))
    panel.replace_btn.click()
    (img,) = images(ws)
    assert (img.width, img.height) == (400, 200)
    assert rect_close(img.rect, (72, 520, 232, 600))                 # fitted into the old frame
    assert tool.selected.width == 400 and "400 × 200 px" in panel.detail.text()
    out = tmp_path / "gemt.png"
    monkeypatch.setattr(edit_object, "_AUTO_SAVE", str(out))
    panel.save_btn.click()
    pix = pymupdf.Pixmap(out.read_bytes())
    assert (pix.width, pix.height) == (400, 200)
    panel.rotate_btn.click()
    assert images(ws)[0].rotation == pytest.approx(90)
    panel.flip_btn.click()
    assert images(ws)[0].flipped
    panel.delete_btn.click()
    assert images(ws) == []


def test_rotated_page_at_high_zoom(ws, qapp):
    insert.add_image(ws.pdf, 2, (100, 100, 300, 250), make_png(200, 150, (200, 40, 40)))
    ws.view.set_zoom(2.0)
    ws.view.go_to_page(2)
    qapp.processEvents()
    tool = ws.tool
    click(tool, ws.view, 2, 200, 175)
    assert tool.selected is not None and tool.selected.page == 2
    drag(tool, ws.view, 2, (200, 175), (250, 205))
    assert rect_close(images(ws, 2)[0].rect, (150, 130, 350, 280))
    assert tool.handle_at(2, QPointF(350, 280)) == 2
    assert tool.handle_at(2, QPointF(350 + 6 / 2.0, 280)) == 2       # tolerance scales with the zoom
    drag(tool, ws.view, 2, (350, 280), (450, 300))
    assert rect_close(images(ws, 2)[0].rect, (150, 130, 450, 355))
    assert "Roteret side" in ws.pdf.doc[2].get_text()


def test_copy_to_clipboard_and_deactivate_cleans_up(ws):
    tool = ws.tool
    click(tool, ws.view, 1, CX, CY)
    assert ws.can_copy() and ws.copy()
    img = QGuiApplication.clipboard().image()
    assert (img.width(), img.height()) == (40, 30)
    tool.mouse_move(ev(ws.view, 1, CX, CY, button=NONE, buttons=NONE))
    ws.set_tool("edit.text")
    assert tool.selected is None and tool._frame is None and tool._hover is None
    assert not any(it.zValue() in (12, 20, 35, 45, 60) for it in ws.view._overlays)


def test_empty_page_panel_counts_images(ws, qapp):
    panel = ws.inspector.scroll.widget()
    ws.view.go_to_page(0)
    qapp.processEvents()
    panel.refresh()
    assert "ingen billeder" in panel.meta.text()
    ws.view.go_to_page(1)
    qapp.processEvents()
    panel.refresh()
    assert "1 billede" in panel.meta.text()
