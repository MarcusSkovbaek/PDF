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


# -- multi-select, group move / delete, nudging --------------------------------------------
SHIFT = Qt.KeyboardModifier.ShiftModifier


def key(k, mods=Qt.KeyboardModifier.NoModifier):
    return QKeyEvent(QEvent.Type.KeyPress, k, mods)


def two_shapes(ws):
    from pdfstudio.core.annotations import add_shape

    a = add_shape(ws.pdf, 0, "rect", (100, 500), (200, 560), (0.9, 0.2, 0.2))
    b = add_shape(ws.pdf, 0, "ellipse", (300, 500), (400, 560), (0.9, 0.2, 0.2))
    ws.set_tool("annotate.select")
    return a, b


def rects(ws, page=0):
    return {a.xref: a.rect for a in list_annotations(ws.pdf.doc, page)}


def test_shift_click_adds_and_drag_moves_the_group(ws):
    a, b = two_shapes(ws)
    tool, view = ws.tool, ws.view
    before, depth = rects(ws), len(ws.pdf.history._undo)
    tool.mouse_press(ev(view, 0, 100, 530))
    tool.mouse_release(ev(view, 0, 100, 530, buttons=NONE))
    tool.mouse_press(ev(view, 0, 350, 530, mods=SHIFT))
    tool.mouse_release(ev(view, 0, 350, 530, buttons=NONE, mods=SHIFT))
    assert {x.xref for x in tool.selection} == {a, b} and tool.selected is None
    assert len(tool._frames) == 2
    drag(tool, view, 0, (100, 530), (150, 580))                  # grab one, both move
    after = rects(ws)
    for x in (a, b):
        assert abs(after[x].x0 - before[x].x0 - 50) < 1.5 and abs(after[x].y0 - before[x].y0 - 50) < 1.5
    assert len(ws.pdf.history._undo) == depth + 1                # one undo step
    assert ws.pdf.history.undo_label == "Flyt kommentarer"
    assert len(tool.selection) == 2                              # still selected after the edit
    ws.pdf.undo()
    assert all(abs(rects(ws)[x].x0 - before[x].x0) < 0.5 for x in (a, b))
    # shift+click on a selected one removes it again
    tool.mouse_press(ev(view, 0, 100, 530))
    tool.mouse_release(ev(view, 0, 100, 530, buttons=NONE))
    assert [x.xref for x in tool.selection] == [a]
    tool.mouse_press(ev(view, 0, 350, 530, mods=SHIFT))
    assert len(tool.selection) == 2
    tool.mouse_press(ev(view, 0, 350, 530, mods=SHIFT))
    assert [x.xref for x in tool.selection] == [a]


def test_click_on_member_without_drag_selects_just_it(ws):
    a, b = two_shapes(ws)
    tool, view = ws.tool, ws.view
    tool.select_all()
    assert len(tool.selection) == 2
    tool.mouse_press(ev(view, 0, 350, 530))
    assert len(tool.selection) == 2                              # a drag would move both ...
    tool.mouse_release(ev(view, 0, 350, 530, buttons=NONE))
    assert [x.xref for x in tool.selection] == [b]               # ... a click picks one


def test_rubber_band_selects_and_inspector_shows_group(ws):
    from pdfstudio.ui.viewer.tools._annot_common import AnnotStylePanel

    a, b = two_shapes(ws)
    tool, view = ws.tool, ws.view
    drag(tool, view, 0, (60, 470), (420, 590))                   # on empty page area
    assert {x.xref for x in tool.selection} == {a, b}
    assert tool._band_item is None                               # band removed on release
    panel = ws.inspector.scroll.widget()
    assert isinstance(panel, AnnotStylePanel) and panel.title.text() == "2 kommentarer valgt"
    depth = len(ws.pdf.history._undo)
    panel._set_color((0.1, 0.5, 0.9))                            # applies to all, one step
    assert len(ws.pdf.history._undo) == depth + 1
    assert all(abs(x.color[2] - 0.9) < 0.01 for x in list_annotations(ws.pdf.doc, 0))
    # a plain click on empty space clears the selection
    tool.mouse_press(ev(view, 0, 500, 750))
    tool.mouse_release(ev(view, 0, 500, 750, buttons=NONE))
    assert tool.selection == []


def test_delete_removes_the_whole_selection_in_one_step(ws):
    two_shapes(ws)
    tool = ws.tool
    assert tool.select_all()                                    # Ctrl+A: all on the page
    tool.key_press(key(Qt.Key.Key_Delete))
    assert kinds(ws) == []
    assert ws.pdf.history.undo_label == "Slet kommentarer"
    ws.pdf.undo()
    assert kinds(ws) == ["rect", "ellipse"]


def test_arrow_keys_nudge_with_one_undo_step_per_burst(ws):
    a, b = two_shapes(ws)
    tool = ws.tool
    tool.select_all()
    before, depth = rects(ws), len(ws.pdf.history._undo)
    for _ in range(3):
        assert tool.key_press(key(Qt.Key.Key_Right))
    assert tool.key_press(key(Qt.Key.Key_Down, SHIFT))          # Shift: 10 pt
    after = rects(ws)
    for x in (a, b):
        assert abs(after[x].x0 - before[x].x0 - 3) < 0.05 and abs(after[x].y0 - before[x].y0 - 10) < 0.05
    assert len(ws.pdf.history._undo) == depth + 1               # the burst is one step
    assert len(tool.selection) == 2
    ws.pdf.undo()
    assert all(abs(rects(ws)[x].x0 - before[x].x0) < 0.05 for x in (a, b))
    tool.select_all()
    tool._end_burst()                                           # the burst timed out
    tool.key_press(key(Qt.Key.Key_Left))
    tool._end_burst()
    tool.key_press(key(Qt.Key.Key_Left))
    assert len(ws.pdf.history._undo) == depth + 2               # separate bursts, separate steps


def test_moves_stay_on_the_page(ws):
    from pdfstudio.core.annotations import add_shape

    add_shape(ws.pdf, 0, "rect", (5, 5), (60, 40), (0.9, 0.2, 0.2))
    ws.set_tool("annotate.select")
    tool = ws.tool
    tool.select_all()
    tool.key_press(key(Qt.Key.Key_Left, SHIFT))
    tool.key_press(key(Qt.Key.Key_Up, SHIFT))
    r = list_annotations(ws.pdf.doc, 0)[0].rect
    assert r.x0 >= -0.01 and r.y0 >= -0.01


# -- hover / drag previews ---------------------------------------------------------------------
def test_hovering_a_swatch_previews_without_committing(ws):
    a, _b = two_shapes(ws)
    tool = ws.tool
    tool.select(next(x for x in list_annotations(ws.pdf.doc, 0) if x.xref == a))
    panel = ws.inspector.scroll.widget()
    depth = len(ws.pdf.history._undo)
    panel._hover_color((0.47, 0.87, 0.35))
    assert tool._preview is not None and tool._preview.active
    assert len(ws.pdf.history._undo) == depth                   # nothing written
    assert abs(list_annotations(ws.pdf.doc, 0)[0].color[0] - 0.9) < 0.01
    panel._hover_color(None)                                     # leaving restores
    assert not tool._preview.active
    # dragging the opacity slider previews, releasing commits once
    panel.opacity.setSliderDown(True)
    for v in (80, 60, 40):
        panel.opacity.setValue(v)
    assert tool._preview.active and len(ws.pdf.history._undo) == depth
    panel.opacity.setSliderDown(False)
    assert len(ws.pdf.history._undo) == depth + 1
    assert abs(list_annotations(ws.pdf.doc, 0)[0].opacity - 0.4) < 0.01
    assert not tool._preview.active


def test_creation_tool_sample_follows_hover(ws):
    ws.set_tool("annotate.highlight")
    panel = ws.inspector.scroll.widget()
    assert panel.sample is not None and panel.sample.kind == "highlight"
    panel._hover_color((1.0, 0.45, 0.72))
    assert panel.sample.preview == {"color": (1.0, 0.45, 0.72)}
    panel._hover_color(None)
    assert panel.sample.preview == {}
    assert ws.tool.style.color != (1.0, 0.45, 0.72)             # hovering does not choose


# -- free text ("Tekstfelt") ----------------------------------------------------------------------
def type_text(widget, text):
    """Real key presses (QTest only handles Latin-1 characters – enough for æøå)."""
    from PySide6.QtTest import QTest

    QTest.keyClicks(widget, text)


def press_key(widget, k, mods=Qt.KeyboardModifier.NoModifier):
    from PySide6.QtWidgets import QApplication

    QApplication.sendEvent(widget, QKeyEvent(QEvent.Type.KeyPress, k, mods))


def test_free_text_click_type_finish(ws, qapp):
    ws.set_tool("annotate.freetext")
    tool, view = ws.tool, ws.view
    tool.mouse_press(ev(view, 0, 300, 600))
    tool.mouse_release(ev(view, 0, 300, 600, buttons=NONE))
    assert tool.session is not None and tool.session.editor.isVisible()
    type_text(tool.session.editor, "Blåbær på Ærø")
    press_key(tool.session.editor, Qt.Key.Key_Return)
    type_text(tool.session.editor, "ØRESUND")
    press_key(tool.session.editor, Qt.Key.Key_Escape)            # Esc finishes (keeps the text)
    assert tool.session is None
    info = list_annotations(ws.pdf.doc, 0)[-1]
    assert info.kind == "freetext" and info.contents == "Blåbær på Ærø\nØRESUND"
    assert info.fontsize == tool.style.fontsize
    size = tool.style.fontsize
    assert abs(info.rect.x0 - 300) < 0.5 and abs((info.rect.y0 + 0.6 * size) - 600) < 0.5   # centred on the click
    assert ws.pdf.history.undo_label == "Tilføj tekstfelt"


def test_free_text_drag_sets_width_and_inspector_styles_live(ws, qapp):
    ws.set_tool("annotate.freetext")
    tool, view = ws.tool, ws.view
    drag(tool, view, 0, (300, 600), (420, 640))
    session = tool.session
    assert session is not None and session.width == pytest.approx(120, abs=0.5)
    type_text(session.editor, "En længere tekst der ombrydes inden for feltet")
    panel = ws.inspector.scroll.widget()
    panel.size.setValue(18)
    assert session._shown.fontsize == 18                        # previewed at once ...
    panel._commit_pending()                                     # ... chosen once it settles
    assert session.style.fontsize == 18
    panel._set_color((0.33, 0.65, 1.0))
    press_key(session.editor, Qt.Key.Key_Return, Qt.KeyboardModifier.ControlModifier)
    info = list_annotations(ws.pdf.doc, 0)[-1]
    assert info.fontsize == 18 and abs(info.color[2] - 1.0) < 0.01
    assert abs(info.rect.width - 120) < 0.5 and info.rect.height > 3 * 18   # wrapped
    words = " ".join(w[4] for w in ws.pdf.page(0).get_text("words", clip=info.rect + (-1, -1, 1, 1)))
    assert words == "En længere tekst der ombrydes inden for feltet"


def test_free_text_empty_click_creates_nothing(ws):
    ws.set_tool("annotate.freetext")
    tool, view = ws.tool, ws.view
    tool.mouse_press(ev(view, 0, 300, 600))
    tool.mouse_release(ev(view, 0, 300, 600, buttons=NONE))
    tool.mouse_press(ev(view, 0, 100, 700))                     # click elsewhere: finishes
    tool.mouse_release(ev(view, 0, 100, 700, buttons=NONE))
    assert tool.session is None and kinds(ws) == []
    assert not ws.pdf.history.can_undo


def test_double_click_edits_free_text_in_place(ws, qapp):
    from pdfstudio.core.annotations import add_free_text, free_text_size

    w, h = free_text_size("Første udkast", 12)
    xref = add_free_text(ws.pdf, 0, (100, 600, 100 + w, 600 + h), "Første udkast", (0.1, 0.1, 0.1), 12)
    ws.set_tool("annotate.select")
    tool, view = ws.tool, ws.view
    depth = len(ws.pdf.history._undo)
    tool.mouse_press(ev(view, 0, 110, 605))
    tool.mouse_release(ev(view, 0, 110, 605, buttons=NONE))
    tool.mouse_double_click(ev(view, 0, 110, 605))
    session = tool.session
    assert session is not None and session.xref == xref and session.width is None   # click-sized: grows
    assert session.editor.toPlainText() == "Første udkast" and session.hider.active
    type_text(session.editor, " er rettet på Ærø")
    press_key(session.editor, Qt.Key.Key_Escape)
    info = next(a for a in list_annotations(ws.pdf.doc, 0) if a.xref == xref)
    assert info.contents == "Første udkast er rettet på Ærø"
    assert info.rect.width > w + 20                             # the box grew with the text
    assert len(ws.pdf.history._undo) == depth + 1 and ws.pdf.history.undo_label == "Redigér kommentar"
    assert tool.session is None and [a.xref for a in tool.selection] == [xref]
    # emptying the text removes the box
    tool.key_press(key(Qt.Key.Key_Return))                      # Enter edits the selected box
    tool.session.editor.clear()
    tool.session.commit()
    assert xref not in [a.xref for a in list_annotations(ws.pdf.doc, 0)]
