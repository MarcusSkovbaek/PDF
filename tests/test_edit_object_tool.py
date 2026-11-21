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
ALT = Qt.KeyboardModifier.AltModifier
CTRL = Qt.KeyboardModifier.ControlModifier
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
    # (Alt: no snapping – the right edge would snap to the page centre at 297.5)
    drag(tool, ws.view, 1, (CX, CY), (CX + 60, CY + 8), mods=SHIFT | ALT)
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
    # Tab walks through all objects in reading order: Shift+Tab from nothing = the last one, the image
    assert key(tool, Qt.Key.Key_Backtab)
    assert tool.selected is not None and tool.selection()[0].kind == "image"
    for _ in range(3):
        key(tool, Qt.Key.Key_Right)
    key(tool, Qt.Key.Key_Down, SHIFT)
    assert rect_close(images(ws)[0].rect, (75, 510, 235, 630))
    assert len(ws.pdf.history._undo) == 1                            # one burst = one undo step
    ws.pdf.undo()
    assert rect_close(images(ws)[0].rect, (72, 500, 232, 620))
    assert key(tool, Qt.Key.Key_Escape) and tool.selected is None
    key(tool, Qt.Key.Key_Backtab)
    assert key(tool, Qt.Key.Key_Delete)
    assert images(ws) == [] and tool.selected is None
    assert "Venstre kolonne" in ws.pdf.doc[1].get_text()            # nothing else removed
    ws.pdf.undo()
    assert len(images(ws)) == 1


def test_inspector_fields_with_aspect_lock(ws):
    tool = ws.tool
    click(tool, ws.view, 1, CX, CY)
    panel = ws.inspector.scroll.widget()
    panel.w.setValue(80 * MM)                                        # lock on: height follows
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
    assert panel.meta.text() == "På side 1: 4 tekstblokke."          # every kind of object is counted
    ws.view.go_to_page(1)
    qapp.processEvents()
    panel.refresh()
    assert panel.meta.text() == "På side 2: 2 tekstblokke, 1 grafisk element og 1 billede."
    # a low-resolution warning does not stick to the "nothing selected" text
    click(ws.tool, ws.view, 1, CX, CY)
    assert panel.meta.property("role") == "danger"                  # 40 px over 160 pt = 18 ppi
    ws.tool.select(None)
    assert panel.meta.property("role") == "caption"


# ===========================================================================
# text blocks and vector graphics (WP-D1)
# ===========================================================================
from pdfstudio.core import pageobjects as po  # noqa: E402

# page 0 of the shapes document:
A = (99, 99, 201, 161)            # filled red box, 2 pt stroke (display rect incl. stroke)
B = (318.5, 298.5, 441.5, 381.5)  # blue frame, 3 pt stroke, no fill
PARA = "Et afsnit med almindelig tekst."


def make_shapes_pdf(path, *, stamp: str = "") -> str:
    doc = pymupdf.open()
    p = doc.new_page(width=595, height=842)
    p.draw_rect(pymupdf.Rect(0, 0, 595, 842), color=None, fill=(0.97, 0.96, 0.92))      # page background
    p.draw_rect(pymupdf.Rect(100, 100, 200, 160), color=(0.8, 0.1, 0.1), fill=(1, 0.8, 0.8), width=2)
    p.draw_rect(pymupdf.Rect(320, 300, 440, 380), color=(0.1, 0.3, 0.8), fill=None, width=3)
    p.insert_text((100, 250), PARA, fontname="helv", fontsize=12)
    p.draw_rect(pymupdf.Rect(100, 450, 400, 560), color=(0.2, 0.5, 0.2), fill=(0.9, 1, 0.9), width=1)
    p.insert_text((120, 490), "Faktaboks", fontname="helv", fontsize=14)
    p.insert_text((120, 520), "Tekst inde i kassen.", fontname="helv", fontsize=11)
    if stamp:
        p.insert_text((100, 800), stamp, fontname="helv", fontsize=9)
    doc.new_page(width=595, height=842)
    doc.save(str(path))
    doc.close()
    return str(path)


@pytest.fixture
def sws(win, qapp, tmp_path):  # noqa: F811
    win.open_path(make_shapes_pdf(tmp_path / "shapes.pdf"))
    w = win.current_workspace()
    w.set_mode("edit")
    w.set_tool("edit.object")
    w.view.go_to_page(0)
    qapp.processEvents()
    return w


def objs(ws, page=0, kind=None, alt=False):
    return [o for o in po.page_objects(ws.pdf.doc, page, alt=alt) if kind is None or o.kind == kind]


def graphic_at(ws, rect, page=0):
    # alt: a copy overlapping its original is classified as a "table" by the
    # graphics engine (facade issue, see the WP-D1 report) – look everywhere
    for o in objs(ws, page, "graphic", alt=True):
        if rect_close(o.rect, rect):
            return o
    return None


def para(ws, page=0):
    return next(o for o in objs(ws, page, "text") if PARA in o.ref.text)


def sel_kinds(tool):
    return sorted(o.kind for o in tool.selection())


def moves(tool, view, page, a, b, mods=NOMOD):
    """Press at ``a`` and drag to ``b`` without releasing."""
    tool.mouse_press(ev(view, page, *a, mods=mods))
    mid = ((a[0] + b[0]) / 2, (a[1] + b[1]) / 2)
    tool.mouse_move(ev(view, page, *mid, button=NONE, buttons=L, mods=mods))
    tool.mouse_move(ev(view, page, *b, button=NONE, buttons=L, mods=mods))


def test_hover_outlines_text_and_graphics(sws):
    tool = sws.tool
    tool.mouse_move(ev(sws.view, 0, 150, 245, button=NONE, buttons=NONE))       # on the paragraph
    assert tool._hover is not None
    r = tool._hover.boundingRect()
    assert r.left() < 100 and r.right() > 259                                   # its whole block
    tool.mouse_move(ev(sws.view, 0, 150, 130, button=NONE, buttons=NONE))       # inside the filled box
    assert tool._hover is not None and abs(tool._hover.boundingRect().width() - 102) < 2
    tool.mouse_move(ev(sws.view, 0, 370, 340, button=NONE, buttons=NONE))       # inside an unfilled frame
    assert tool._hover is None
    tool.mouse_move(ev(sws.view, 0, 320, 340, button=NONE, buttons=NONE))       # on its stroke
    assert tool._hover is not None
    tool.mouse_move(ev(sws.view, 0, 50, 50, button=NONE, buttons=NONE))         # the page background
    assert tool._hover is None
    tool.mouse_move(ev(sws.view, 0, 50, 50, button=NONE, buttons=NONE, mods=ALT))
    assert tool._hover is not None                                              # Alt reaches it


def test_click_selects_text_graphic_and_shift_click_toggles(sws):
    from pdfstudio.ui.viewer.tools.edit_object import ObjectPanel

    tool = sws.tool
    click(tool, sws.view, 0, 150, 245)
    assert sel_kinds(tool) == ["text"] and PARA in tool.selected.text
    assert len(tool._frame.childItems()) == 2                                   # side handles (width)
    panel = sws.inspector.scroll.widget()
    assert isinstance(panel, ObjectPanel) and panel.title.text() == "Tekst"
    assert "almindelig" in panel.detail.text()
    click(tool, sws.view, 0, 150, 130)
    assert sel_kinds(tool) == ["graphic"] and rect_close(tool.selection()[0].rect, A)
    assert len(tool._frame.childItems()) == 4                                   # corner handles
    assert sws.inspector.scroll.widget().title.text() == "Grafik"
    click(tool, sws.view, 0, 150, 245, mods=SHIFT)                              # add the text
    assert sel_kinds(tool) == ["graphic", "text"]
    assert len(tool._frame.childItems()) == 0                                   # text in it: no scaling
    assert len(tool._members) == 2                                              # thin outline per member
    fr = tool._frame.rect()
    assert fr.top() < 100 and fr.bottom() > 253 and fr.right() > 259            # the union
    assert sws.inspector.scroll.widget().title.text() == "2 objekter"
    click(tool, sws.view, 0, 150, 130, mods=CTRL)                               # Ctrl+click removes again
    assert sel_kinds(tool) == ["text"]
    click(tool, sws.view, 0, 500, 700)                                          # empty space clears
    assert tool.selection() == [] and tool._frame is None
    assert not sws.pdf.history.can_undo


def test_click_on_a_member_of_a_multi_selection_keeps_it_for_dragging(sws):
    tool = sws.tool
    click(tool, sws.view, 0, 150, 130)
    click(tool, sws.view, 0, 150, 245, mods=SHIFT)
    tool.mouse_press(ev(sws.view, 0, 150, 130))
    assert len(tool.selection()) == 2                                           # still both (a drag may follow)
    tool.mouse_release(ev(sws.view, 0, 150, 130, buttons=NONE))
    assert sel_kinds(tool) == ["graphic"]                                       # a click: only that one


def test_rubber_band_selects_what_lies_inside(sws):
    tool = sws.tool
    moves(tool, sws.view, 0, (80, 80), (280, 280))
    assert tool._band is not None
    tool.mouse_release(ev(sws.view, 0, 280, 280, buttons=NONE))
    assert tool._band is None
    assert sel_kinds(tool) == ["graphic", "text"]
    # Shift + band adds
    drag(tool, sws.view, 0, (300, 280), (450, 400), mods=SHIFT)
    assert sel_kinds(tool) == ["graphic", "graphic", "text"]
    # a band over nothing clears
    drag(tool, sws.view, 0, (450, 600), (560, 700))
    assert tool.selection() == []


def test_drag_text_moves_it_unchanged_as_one_undo_step(sws):
    tool = sws.tool
    p = para(sws)
    old = pymupdf.Rect(p.rect)
    moves(tool, sws.view, 0, (150, 245), (200, 345), mods=ALT)
    assert tool._lift is not None and tool._ghost is not None and tool._ghost.opacity() < 1
    g = tool._ghost.mapRectToParent(tool._ghost.boundingRect())
    assert abs(g.left() - (old.x0 + 50 - 2)) < 1.5 and abs(g.top() - (old.y0 + 100 - 2)) < 1.5
    assert "mm" in tool._badge.text()
    assert not sws.pdf.history.can_undo
    tool.mouse_release(ev(sws.view, 0, 200, 345, buttons=NONE, mods=ALT))
    q = para(sws)
    assert rect_close(q.rect, old + (50, 100, 50, 100), tol=1.0)
    assert q.ref.text == p.ref.text
    assert PARA in sws.pdf.doc[0].get_text()
    assert len(sws.pdf.history._undo) == 1
    assert sel_kinds(tool) == ["text"] and rect_close(tool.selection()[0].rect, q.rect)
    sws.pdf.undo()
    assert rect_close(para(sws).rect, old, tol=0.5)
    assert sel_kinds(tool) == ["text"] and rect_close(tool.selection()[0].rect, old, tol=0.5)


def test_drag_graphic_and_multi_selection(sws):
    tool = sws.tool
    drag(tool, sws.view, 0, (150, 130), (180, 170), mods=ALT)
    assert graphic_at(sws, (129, 139, 231, 201)) is not None
    assert len(sws.pdf.history._undo) == 1
    # graphic + text together
    click(tool, sws.view, 0, 150, 245, mods=SHIFT)
    t0 = pymupdf.Rect(para(sws).rect)
    drag(tool, sws.view, 0, (180, 170), (180, 200), mods=ALT)
    assert graphic_at(sws, (129, 169, 231, 231)) is not None
    assert rect_close(para(sws).rect, t0 + (0, 30, 0, 30), tol=1.0)
    assert len(sws.pdf.history._undo) == 2 and len(tool.selection()) == 2


def test_snapping_to_another_shapes_left_edge_shows_a_guide(sws):
    tool = sws.tool
    off = -0.6     # inside the 6 px snapping distance and nearer to A (99) than to the box (99.5)
    grab = (320, 340)                                # on the frame's stroke
    target_dx = A[0] - B[0] + off                    # the left edge ends up `off` right of A's left edge
    moves(tool, sws.view, 0, grab, (grab[0] + target_dx, grab[1]))
    assert tool._guides, "a guide line is shown while snapped"
    lines = [g.line() for g in tool._guides]
    assert any(abs(ln.x1() - A[0]) < 1e-6 and abs(ln.x2() - A[0]) < 1e-6 for ln in lines)
    tool.mouse_release(ev(sws.view, 0, grab[0] + target_dx, grab[1], buttons=NONE))
    assert not tool._guides
    moved = tool.selection()[0]
    assert abs(moved.rect.x0 - A[0]) < 0.05                       # exactly on the edge
    sws.pdf.undo()
    tool.select(None)
    drag(tool, sws.view, 0, grab, (grab[0] + target_dx, grab[1]), mods=ALT)   # Alt: no snapping
    assert abs(tool.selection()[0].rect.x0 - (A[0] + off)) < 0.3


def test_snap_delta_unit():
    from PySide6.QtCore import QRectF

    from pdfstudio.ui.viewer.tools.edit_object import snap_delta

    r = QRectF(0, 0, 20, 20)
    other = QRectF(100, 50, 20, 20)
    xs = [(100, other), (110, other), (120, other)]
    ys = [(50, other), (60, other), (70, other)]
    dx, dy, guides = snap_delta(r, 97, 200, xs, ys, 4)
    assert (dx, dy) == (100, 200) and len(guides) == 3             # left/centre/right line up (x only)
    assert all(g.x1() == g.x2() for g in guides)
    dx, dy, guides = snap_delta(r, 104, 58, xs, ys, 4)
    assert (dx, dy) == (100, 60)                                    # x: -4 to 100; y: top -> 60
    dx, dy, _ = snap_delta(r, 150, 58, xs, ys, 4)
    assert dx == 150                                                # nothing near: unchanged
    dx, dy, _ = snap_delta(r, 97, 49, xs, ys, 4, lock_x=True)
    assert (dx, dy) == (97, 50)


def test_nudge_burst_is_one_undo_step(sws):
    tool = sws.tool
    click(tool, sws.view, 0, 150, 130)
    for _ in range(3):
        assert key(tool, Qt.Key.Key_Right)
    key(tool, Qt.Key.Key_Down, SHIFT)
    assert graphic_at(sws, (102, 109, 204, 171)) is not None
    assert len(sws.pdf.history._undo) == 1
    assert rect_close(tool.selection()[0].rect, (102, 109, 204, 171))
    sws.pdf.undo()
    assert graphic_at(sws, A) is not None


def test_delete_and_undo(sws):
    tool = sws.tool
    shown = []
    tool.ctx = tool.ctx.__class__(pdf=tool.ctx.pdf, view=tool.ctx.view, workspace=tool.ctx.workspace,
                                  toast=lambda text, kind="info", **kw: shown.append((text, kw)))
    click(tool, sws.view, 0, 150, 245)
    assert key(tool, Qt.Key.Key_Delete)
    assert PARA not in sws.pdf.doc[0].get_text() and tool.selection() == []
    assert shown and shown[-1][0] == "Objektet er slettet" and shown[-1][1]["action_text"] == "Fortryd"
    assert graphic_at(sws, A) is not None                          # nothing else removed
    shown[-1][1]["on_action"]()                                    # "Fortryd"
    assert PARA in sws.pdf.doc[0].get_text()


def test_copy_paste_on_the_page_and_into_another_document(sws, win, qapp, tmp_path):  # noqa: F811
    tool = sws.tool
    click(tool, sws.view, 0, 150, 130)
    assert key(tool, Qt.Key.Key_C, CTRL)
    assert key(tool, Qt.Key.Key_V, CTRL)                           # same page: +10/+10
    assert graphic_at(sws, (109, 109, 211, 171)) is not None
    assert rect_close(tool.selection()[0].rect, (109, 109, 211, 171))
    assert graphic_at(sws, A) is not None
    assert key(tool, Qt.Key.Key_V, CTRL)                           # again: a further 10 pt
    assert graphic_at(sws, (119, 119, 221, 181)) is not None
    assert len(sws.pdf.history._undo) == 2
    # onto another page: the same place
    sws.view.go_to_page(1)
    qapp.processEvents()
    assert key(tool, Qt.Key.Key_V, CTRL)
    assert graphic_at(sws, A, page=1) is not None
    # into a second document (tab): the same place
    win.open_path(make_shapes_pdf(tmp_path / "other.pdf", stamp="Andet dokument"))
    ws2 = win.current_workspace()
    assert ws2 is not sws
    ws2.set_mode("edit")
    ws2.set_tool("edit.object")
    ws2.view.go_to_page(1)
    qapp.processEvents()
    assert ws2.tool.can_paste()
    assert key(ws2.tool, Qt.Key.Key_V, CTRL)
    assert graphic_at(ws2, A, page=1) is not None and len(objs(ws2, 1, alt=True)) == 1
    assert ws2.tool.selection()[0].page == 1


def test_copied_text_and_stale_clip(sws):
    tool = sws.tool
    click(tool, sws.view, 0, 150, 245)
    assert sws.can_copy() and sws.copy()                           # Ctrl+C through the window action
    assert QGuiApplication.clipboard().text() == PARA              # plain text for other programs
    assert tool.can_paste()
    QGuiApplication.clipboard().setText("kopieret i et andet program")
    assert not tool.can_paste()                                    # never paste a stale clip
    assert not key(tool, Qt.Key.Key_V, CTRL)


def test_duplicate_with_ctrl_d(sws):
    tool = sws.tool
    click(tool, sws.view, 0, 150, 130)
    click(tool, sws.view, 0, 150, 245, mods=SHIFT)
    assert key(tool, Qt.Key.Key_D, CTRL)
    assert graphic_at(sws, (109, 109, 211, 171)) is not None
    texts = [o for o in objs(sws, 0, "text") if PARA in o.ref.text]
    assert len(texts) == 2
    assert len(tool.selection()) == 2 and all(o.rect.x0 > 105 for o in tool.selection())
    assert len(sws.pdf.history._undo) == 1


def test_window_shortcuts_reach_the_tool(sws, win, qapp):  # noqa: F811
    view = sws.view
    pages = sws.pdf.page_count

    def override(k, mods=CTRL):
        e = QKeyEvent(QEvent.Type.ShortcutOverride, k, mods)
        e.ignore()
        QGuiApplication.sendEvent(view, e)
        return e.isAccepted()

    assert not override(Qt.Key.Key_D)                              # nothing selected: the window's Ctrl+D
    click(sws.tool, view, 0, 150, 130)
    assert override(Qt.Key.Key_D) and override(Qt.Key.Key_C) and override(Qt.Key.Key_X)
    assert not override(Qt.Key.Key_Z)                              # undo stays a window shortcut
    win.activateWindow()
    view.setFocus()
    qapp.processEvents()
    from PySide6.QtTest import QTest

    QTest.keyClick(view, Qt.Key.Key_D, CTRL)
    qapp.processEvents()
    assert sws.pdf.page_count == pages                             # no page was duplicated …
    assert graphic_at(sws, (109, 109, 211, 171)) is not None       # … the object was


def test_alt_click_selects_the_page_background(sws):
    tool = sws.tool
    click(tool, sws.view, 0, 50, 50)
    assert tool.selection() == []
    click(tool, sws.view, 0, 50, 50, mods=ALT)
    (bg,) = tool.selection()
    assert bg.kind == "graphic" and bg.ref.kind == "background"


def test_corner_handle_scales_a_graphic_and_keeps_its_stroke(sws):
    tool = sws.tool
    click(tool, sws.view, 0, 150, 130)
    assert tool.handle_at(0, QPointF(201, 161)) == 2
    drag(tool, sws.view, 0, (201, 161), (252, 192))                 # ratio kept
    g = tool.selection()[0]
    assert abs(g.rect.width / g.rect.height - 102 / 62) < 0.02
    assert abs(g.rect.x0 - 99) < 0.3 and abs(g.rect.y0 - 99) < 0.3
    assert g.ref.width == pytest.approx(2.0, abs=0.01)
    assert len(sws.pdf.history._undo) == 1
    drag(tool, sws.view, 0, (g.rect.x1, g.rect.y1), (300, 200), mods=SHIFT)   # free
    g = tool.selection()[0]
    assert rect_close(g.rect, (99, 99, 300, 200), tol=0.6)
    assert g.ref.width == pytest.approx(2.0, abs=0.01)


def test_side_handle_changes_the_width_of_a_text_block(sws):
    from pdfstudio.ui.viewer.tools import edit_object

    tool = sws.tool
    click(tool, sws.view, 0, 150, 245)
    p = tool.selection()[0]
    r = p.rect
    pad = edit_object.TEXT_PAD                                     # the frame sits a little outside
    hx, hy = r.x1 + pad, (r.y0 + r.y1) / 2
    assert tool.handle_at(0, QPointF(hx, hy)) == 5
    moves(tool, sws.view, 0, (hx, hy), (r.x0 + 90 + pad, hy))
    assert abs(tool._frame.rect().width() - (90 + 2 * pad)) < 1.5   # preview as a frame
    assert "B " in tool._badge.text()
    tool.mouse_release(ev(sws.view, 0, r.x0 + 90 + pad, hy, buttons=NONE))
    q = tool.selection()[0]
    assert q.kind == "text" and q.rect.width < 92 and q.rect.height > r.height * 1.5   # more lines
    assert " ".join(q.ref.text.split()) == PARA
    assert len(sws.pdf.history._undo) == 1


def test_double_click_on_text_opens_the_text_editor_at_the_click(sws, qapp):
    tool = sws.tool
    hit = sws.pdf.doc[0].search_for("almindelig")[0]
    x, y = hit.x0 + 1, (hit.y0 + hit.y1) / 2
    tool.mouse_press(ev(sws.view, 0, x, y))
    tool.mouse_release(ev(sws.view, 0, x, y, buttons=NONE))
    tool.mouse_double_click(ev(sws.view, 0, x, y))
    qapp.processEvents()
    assert sws.tool.id == "edit.text"
    ed = sws.tool.editor
    assert ed is not None
    cur = ed.textCursor()
    assert cur.block().text().startswith("Et afsnit")
    assert abs((cur.position() - cur.block().position()) - PARA.index("almindelig")) <= 1
    assert not sws.pdf.history.can_undo
    sws.tool.cancel()


def test_include_contents_selects_the_text_in_a_box(sws):
    tool = sws.tool
    assert tool.include_contents
    click(tool, sws.view, 0, 105, 540)                              # the callout box's margin
    assert sel_kinds(tool) == ["graphic", "text", "text"]
    assert tool.selection()[0].kind == "graphic"                    # the box is the primary object
    drag(tool, sws.view, 0, (105, 540), (105, 600), mods=ALT)
    assert graphic_at(sws, (99.5, 509.5, 400.5, 620.5)) is not None
    assert any("Faktaboks" in o.ref.text and o.rect.y0 > 530 for o in objs(sws, 0, "text"))
    tool.set_include_contents(False)
    from pdfstudio.ui.viewer.tools.edit_object import SETTINGS_INCLUDE, _settings

    assert str(_settings().value(SETTINGS_INCLUDE)).lower() == "false"
    tool.select(None)
    click(tool, sws.view, 0, 105, 600)
    assert sel_kinds(tool) == ["graphic"]
    tool.set_include_contents(True)


def test_escape_cancels_a_drag_then_clears(sws):
    tool = sws.tool
    moves(tool, sws.view, 0, (150, 130), (220, 220))
    assert tool._ghost is not None
    assert key(tool, Qt.Key.Key_Escape)
    assert tool._drag is None and tool._ghost is None and tool._lift is None and not tool._guides
    tool.mouse_release(ev(sws.view, 0, 220, 220, buttons=NONE))
    assert not sws.pdf.history.can_undo
    assert sel_kinds(tool) == ["graphic"]
    assert key(tool, Qt.Key.Key_Escape) and tool.selection() == []
    assert not key(tool, Qt.Key.Key_Escape)                         # nothing left to do


def test_tab_cycles_through_the_page_objects(sws):
    tool = sws.tool
    order = [o.key for o in objs(sws)]
    assert key(tool, Qt.Key.Key_Tab)
    assert tool.primary.key == order[0]
    key(tool, Qt.Key.Key_Tab)
    assert tool.primary.key == order[1]
    key(tool, Qt.Key.Key_Tab, SHIFT)
    assert tool.primary.key == order[0]
    key(tool, Qt.Key.Key_Backtab)
    assert tool.primary.key == order[-1]
    assert tool.select_all() and len(tool.selection()) == len(order)


def test_public_methods_for_the_inspector(sws):
    tool = sws.tool
    click(tool, sws.view, 0, 150, 130)
    tool.move_selection_to(300, 600)
    assert rect_close(tool.selection()[0].rect, (300, 600, 402, 662))
    tool.set_selection_rect((300, 600, 504, 724))
    g = tool.selection()[0]
    assert rect_close(g.rect, (300, 600, 504, 724), tol=0.6) and g.ref.width == pytest.approx(2.0, abs=0.01)
    tool.set_style(stroke=(0, 0, 1), width=4)
    g = tool.selection()[0]
    assert g.ref.stroke == pytest.approx((0, 0, 1), abs=0.01) and g.ref.width == pytest.approx(4, abs=0.05)
    tool.restack(front=False)
    assert tool.selection()[0].kind == "graphic"
    n = len(objs(sws, 0, "graphic", alt=True))
    assert tool.duplicate() and len(objs(sws, 0, "graphic", alt=True)) == n + 1
    assert tool.copy() and tool.paste() and len(objs(sws, 0, "graphic", alt=True)) == n + 2
    tool.delete_selected()
    assert len(objs(sws, 0, "graphic", alt=True)) == n + 1
    click(tool, sws.view, 0, 150, 245)
    w0 = tool.selection()[0].rect.width
    tool.set_text_width(w0 * 0.5)
    t = tool.selection()[0]
    assert t.kind == "text" and t.rect.width < w0 * 0.55
    # the general panel moves the selection in mm
    panel = sws.inspector.scroll.widget()
    panel.x.setValue(20)
    panel.y.setValue(40)
    t = tool.selection()[0]
    assert abs(t.rect.x0 - 20 / MM) < 0.5 and abs(t.rect.y0 - 40 / MM) < 0.5


def test_screenshot_with_a_selection(sws, win, qapp, tmp_path):  # noqa: F811
    import os

    tool = sws.tool
    click(tool, sws.view, 0, 105, 540)                              # box + its text
    tool.mouse_move(ev(sws.view, 0, 150, 130, button=NONE, buttons=NONE))   # hover the red box
    sws.view.render_visible_now()
    qapp.processEvents()
    out = os.environ.get("FOLIO_SHOT_DIR") or str(tmp_path)
    path = os.path.join(out, "edit_object_selection.png")
    img = win.grab().toImage()
    assert img.save(path) and not img.isNull()
    assert os.path.getsize(path) > 10_000


def test_text_on_a_rotated_page(ws, qapp):
    ws.view.go_to_page(2)
    qapp.processEvents()
    tool = ws.tool
    (t,) = [o for o in po.page_objects(ws.pdf.doc, 2) if o.kind == "text"]
    c = ((t.rect.x0 + t.rect.x1) / 2, (t.rect.y0 + t.rect.y1) / 2)
    click(tool, ws.view, 2, *c)
    assert sel_kinds(tool) == ["text"]
    assert len(tool._frame.childItems()) == 0          # vertical on screen: no width handles
    drag(tool, ws.view, 2, c, (c[0] - 200, c[1] + 150), mods=ALT)
    (u,) = [o for o in po.page_objects(ws.pdf.doc, 2) if o.kind == "text"]
    assert rect_close(u.rect, t.rect + (-200, 150, -200, 150), tol=1.0)
    assert "Roteret side" in ws.pdf.doc[2].get_text()
    assert len(ws.pdf.history._undo) == 1


def test_deactivate_removes_every_overlay(sws):
    from pdfstudio.ui.viewer.tools import edit_object as eo

    tool = sws.tool
    click(tool, sws.view, 0, 150, 130)
    click(tool, sws.view, 0, 150, 245, mods=SHIFT)
    moves(tool, sws.view, 0, (150, 130), (151, 300))
    assert tool._members and tool._ghost is not None
    sws.set_tool("edit.text")
    zs = {eo.Z_LIFT, eo.Z_HOVER, eo.Z_GHOST, eo.Z_BAND, eo.Z_MEMBER, eo.Z_FRAME, eo.Z_GUIDE, eo.Z_BADGE}
    assert not any(it.zValue() in zs for it in sws.view._overlays)
    assert tool.selection() == [] and not sws.pdf.history.can_undo
