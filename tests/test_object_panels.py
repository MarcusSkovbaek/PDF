"""Inspector panels of "Markér objekt" (WP-D2): text, graphic, multi and image."""

from __future__ import annotations

import os

import pytest
from PySide6.QtCore import QEvent, Qt
from PySide6.QtGui import QGuiApplication, QKeyEvent

from pdfstudio.core import graphics as gr
from pdfstudio.core import objects as ob
from pdfstudio.core import pageobjects as po
from pdfstudio.ui.viewer.tools._object_panels import (
    GraphicObjectPanel,
    ImageArrangePanel,
    MultiObjectPanel,
    TextObjectPanel,
)
from tests.test_edit_object_tool import (  # noqa: F401  (fixtures)
    ALT,
    CTRL,
    MM,
    NONE,
    PARA,
    SHIFT,
    click,
    graphic_at,
    objs,
    para,
    rect_close,
    sel_kinds,
    sws,
    ws,
)
from tests.test_ui_shell import win  # noqa: F401  (fixture)

SHOT_DIR = os.environ.get("FOLIO_SHOT_DIR", "")
RED = (99, 99, 201, 161)          # red box incl. its 2 pt stroke
BLUE = (318.5, 298.5, 441.5, 381.5)


def panel(w):
    return w.inspector.scroll.widget()


def undo_steps(w) -> int:
    return len(w.pdf.history._undo)


def ctrl_shift(tool, k) -> bool:
    return tool.key_press(QKeyEvent(QEvent.Type.KeyPress, k, CTRL | SHIFT))


def z_order(w, page=0):
    """Graphic rects in drawing order (bottom first)."""
    return [tuple(round(v) for v in g.rect) for g in sorted(gr.list_graphics(w.pdf.doc, page), key=lambda g: g.index)]


def commit(spin, value):
    spin.setValue(value)


# ---------------------------------------------------------------------------
def test_each_selection_kind_gets_its_panel(sws, ws):  # noqa: F811
    tool = sws.tool
    assert isinstance(panel(sws), ImageArrangePanel)                # nothing selected
    click(tool, sws.view, 0, 150, 245)
    assert isinstance(panel(sws), TextObjectPanel)
    p = panel(sws)
    assert p.title.text() == "Tekst" and PARA in p.detail.text() and "pt" in p.meta.text()
    click(tool, sws.view, 0, 150, 130)
    p = panel(sws)
    assert isinstance(p, GraphicObjectPanel) and p.title.text() == "Figur"
    assert "Streg 2 pt" in p.detail.text()
    click(tool, sws.view, 0, 150, 245, mods=SHIFT)
    p = panel(sws)
    assert isinstance(p, MultiObjectPanel) and p.title.text() == "2 objekter"
    assert "1 tekstblok" in p.detail.text() and "1 grafisk element" in p.detail.text()
    assert not p.w.isVisibleTo(p) and p.size_note.isVisibleTo(p)  # text inside: no B/H
    assert not p.stroke_btn.isVisibleTo(p)                         # not all graphics: no style


def test_image_panel_keeps_its_rows_and_gets_arrange(ws):  # noqa: F811
    tool = ws.tool
    click(tool, ws.view, 1, 152, 560)
    p = panel(ws)
    assert isinstance(p, ImageArrangePanel)
    assert p.replace_btn.isVisibleTo(p) and p.front_btn.isVisibleTo(p) and p.back_btn.isVisibleTo(p)
    lay = p.edit_box.layout()
    assert lay.indexOf(p.flip_btn) < lay.indexOf(p.delete_btn)
    assert "Ctrl+Shift+]" in p.front_btn.toolTip()
    # z-order: put a filled rect over the image, then bring the image to the front
    with ws.pdf.edit("test", "content", (1,)):
        ws.pdf.doc[1].draw_rect((60, 480, 260, 640), color=None, fill=(0, 0.5, 1))
    click(tool, ws.view, 1, 152, 560, mods=ALT)
    if tool.selection_kind() != "image":
        tool.select(ob.list_images(ws.pdf.doc, 1)[0])
    pix0 = ws.pdf.doc[1].get_pixmap(clip=(150, 555, 154, 559)).pixel(1, 1)
    p.front_btn.click()
    assert tool.selection_kind() == "image"
    pix1 = ws.pdf.doc[1].get_pixmap(clip=(150, 555, 154, 559)).pixel(1, 1)
    assert pix0[0] < 10 and pix0[2] > 240 and pix1 != pix0
    assert ctrl_shift(tool, Qt.Key.Key_BracketLeft)                 # back again, by keyboard
    assert ws.pdf.doc[1].get_pixmap(clip=(150, 555, 154, 559)).pixel(1, 1) == pix0


def test_xy_fields_move_the_selection_as_one_undo_step(sws):  # noqa: F811
    tool = sws.tool
    click(tool, sws.view, 0, 150, 130)
    p = panel(sws)
    assert p.x.value() == pytest.approx(99 * MM, abs=0.06)
    commit(p.x, 50.0)
    g = tool.selection()[0]
    assert g.rect.x0 == pytest.approx(50 / MM, abs=0.2)
    assert g.rect.y0 == pytest.approx(99, abs=0.05)                # the other axis does not drift
    assert undo_steps(sws) == 1
    commit(p.x, 50.0)                                              # unchanged: nothing written
    p.refresh()
    assert undo_steps(sws) == 1
    commit(p.y, 100.0)
    assert tool.selection()[0].rect.y0 == pytest.approx(100 / MM, abs=0.2)
    assert undo_steps(sws) == 2


def test_graphic_size_fields_scale_and_keep_the_stroke(sws):  # noqa: F811
    tool = sws.tool
    click(tool, sws.view, 0, 150, 130)
    p = panel(sws)
    assert p.lock.isChecked()
    w0 = p.w.value()
    commit(p.w, w0 * 2)
    g = tool.selection()[0]
    assert g.rect.width == pytest.approx(204, abs=0.6)
    assert g.rect.height == pytest.approx(124, abs=0.6)           # ratio kept
    assert g.ref.width == pytest.approx(2.0, abs=0.01)            # O8
    assert undo_steps(sws) == 1
    assert p.h.value() == pytest.approx(124 * MM, abs=0.06)       # fields follow
    p.lock.setChecked(False)
    commit(p.h, 20.0)
    g = tool.selection()[0]
    assert g.rect.height == pytest.approx(20 / MM, abs=0.6) and g.rect.width == pytest.approx(204, abs=0.6)


def test_stroke_fill_and_width(sws):  # noqa: F811
    tool = sws.tool
    click(tool, sws.view, 0, 150, 130)
    p = panel(sws)
    assert not p.stroke_none.isChecked() and not p.fill_none.isChecked()
    assert p.width_spin.value() == pytest.approx(2.0)
    p.stroke_btn.set_color((0.0, 0.0, 1.0), emit=True)
    g = tool.selection()[0].ref
    assert g.stroke == pytest.approx((0, 0, 1), abs=0.01)
    commit(p.width_spin, 4.0)
    g = tool.selection()[0].ref
    assert g.width == pytest.approx(4.0, abs=0.05) and g.stroke == pytest.approx((0, 0, 1), abs=0.01)
    p.fill_btn.set_color((1.0, 1.0, 0.0), emit=True)
    assert tool.selection()[0].ref.fill == pytest.approx((1, 1, 0), abs=0.01)
    n = undo_steps(sws)
    assert n == 3
    p.fill_none.click()                                           # "Ingen" fill
    g = tool.selection()[0].ref
    assert g.fill is None and g.stroke is not None
    assert p.fill_btn.is_none() and p.fill_none.isChecked()
    assert "uden fyld" in p.detail.text()
    # removing the stroke too is refused: toast, no change
    toasts = []
    tool.ctx.toast = lambda text, kind="info", **kw: toasts.append((text, kind))
    p.stroke_none.click()
    assert toasts and "streg eller fyld" in toasts[0][0]
    assert not p.stroke_none.isChecked() and not p.stroke_btn.is_none()
    assert tool.selection()[0].ref.stroke is not None
    assert undo_steps(sws) == n + 1
    p.fill_none.click()                                           # fill back (last colour)
    assert tool.selection()[0].ref.fill == pytest.approx((1, 1, 0), abs=0.01)
    p.stroke_none.click()                                         # now the stroke may go
    g = tool.selection()[0].ref
    assert g.stroke is None and g.fill is not None
    assert not p.width_spin.isEnabled()


def test_restack_graphic_and_text_by_button_and_keyboard(sws):  # noqa: F811
    tool = sws.tool
    click(tool, sws.view, 0, 150, 130)                            # red box (drawn second)
    p = panel(sws)
    red = (99, 99, 201, 161)
    order = z_order(sws)
    assert order.index(red) < len(order) - 1
    p.front_btn.click()
    assert z_order(sws)[-1] == red
    assert sws.pdf.history.undo_label == "Læg forrest"
    assert ctrl_shift(tool, Qt.Key.Key_BracketLeft)
    assert z_order(sws)[0] == red
    assert ctrl_shift(tool, Qt.Key.Key_BraceRight)               # Shift+] on a US layout
    assert z_order(sws)[-1] == red
    # text: a grey box drawn over the paragraph hides it; "Læg forrest" shows it again
    with sws.pdf.edit("test", "content", (0,)):
        sws.pdf.doc[0].draw_rect((90, 230, 300, 260), color=None, fill=(0.8, 0.8, 0.8))
    hit = sws.pdf.doc[0].search_for("afsnit")[0]
    probe = (hit.x0, hit.y0, hit.x1, hit.y1)

    def ink():
        pix = sws.pdf.doc[0].get_pixmap(clip=probe, dpi=144)
        return sum(1 for i in range(0, len(pix.samples), pix.n) if pix.samples[i] < 90)

    assert ink() == 0
    tool.select(para(sws))
    assert isinstance(panel(sws), TextObjectPanel)
    panel(sws).front_btn.click()
    assert sel_kinds(tool) == ["text"] and ink() > 20
    assert ctrl_shift(tool, Qt.Key.Key_BracketLeft)
    assert sel_kinds(tool) == ["text"] and ink() == 0


def test_restack_shortcut_is_claimed_from_the_window(sws, win):  # noqa: F811
    view = sws.view

    def override(k):
        e = QKeyEvent(QEvent.Type.ShortcutOverride, k, CTRL | SHIFT)
        e.ignore()
        QGuiApplication.sendEvent(view, e)
        return e.isAccepted()

    assert not override(Qt.Key.Key_BracketRight)                  # nothing selected
    click(sws.tool, view, 0, 150, 130)
    assert override(Qt.Key.Key_BracketRight) and override(Qt.Key.Key_BracketLeft)
    assert override(Qt.Key.Key_BraceRight)
    e = QKeyEvent(QEvent.Type.ShortcutOverride, Qt.Key.Key_BracketRight, CTRL)
    e.ignore()
    QGuiApplication.sendEvent(view, e)
    assert not e.isAccepted()                                     # Ctrl+] stays "Rotér sider"


def test_text_width_field_rewraps(sws):  # noqa: F811
    tool = sws.tool
    click(tool, sws.view, 0, 150, 245)
    p = panel(sws)
    t0 = tool.selection()[0]
    assert p.w.isEnabled() and not p.h.isEnabled()
    assert p.w.value() == pytest.approx(t0.rect.width * MM, abs=0.06)
    commit(p.w, 30.0)
    t = tool.selection()[0]
    assert t.kind == "text" and t.rect.width < 30 / MM + 1 and t.rect.height > t0.rect.height * 1.5
    assert " ".join(t.ref.text.split()) == PARA
    assert undo_steps(sws) == 1
    assert p.h.value() == pytest.approx(t.rect.height * MM, abs=0.06)
    assert abs(t.rect.x0 - t0.rect.x0) < 0.5                      # start edge kept


def test_edit_text_button_switches_tools(sws, qapp):  # noqa: F811
    click(sws.tool, sws.view, 0, 150, 245)
    panel(sws).edit_btn.click()
    qapp.processEvents()
    assert sws.tool.id == "edit.text"
    ed = sws.tool.editor
    assert ed is not None and ed.textCursor().block().text().startswith("Et afsnit")
    sws.tool.cancel()
    assert not sws.pdf.history.can_undo


def test_include_contents_checkbox_for_a_callout_box(sws):  # noqa: F811
    tool = sws.tool
    assert tool.include_contents
    click(tool, sws.view, 0, 105, 540)                            # the callout box's margin
    p = panel(sws)
    assert isinstance(p, MultiObjectPanel)
    assert p.include_row.isVisibleTo(p) and p.include.isChecked()
    assert sel_kinds(tool) == ["graphic", "text", "text"]
    assert not p.stroke_btn.isVisibleTo(p)
    p.include.setChecked(False)                                   # only the box now
    assert not tool.include_contents and sel_kinds(tool) == ["graphic"]
    g = panel(sws)
    assert isinstance(g, GraphicObjectPanel)
    assert g.include_row.isVisibleTo(g) and not g.include.isChecked()
    tool.select(None)
    click(tool, sws.view, 0, 105, 540)                            # a click selects just the box
    assert sel_kinds(tool) == ["graphic"]
    g.include.setChecked(True)
    assert tool.include_contents and sel_kinds(tool) == ["graphic", "text", "text"]
    assert isinstance(panel(sws), MultiObjectPanel)
    # a box without contents: no checkbox
    click(tool, sws.view, 0, 150, 130)
    g = panel(sws)
    assert isinstance(g, GraphicObjectPanel) and not g.include_row.isVisibleTo(g)
    assert not sws.pdf.history.can_undo


def test_multi_panel_with_graphics_scales_and_styles(sws):  # noqa: F811
    tool = sws.tool
    click(tool, sws.view, 0, 150, 130)
    click(tool, sws.view, 0, 322, 340, mods=SHIFT)                # the blue frame's edge
    assert sel_kinds(tool) == ["graphic", "graphic"]
    p = panel(sws)
    assert isinstance(p, MultiObjectPanel)
    assert p.w.isVisibleTo(p) and p.stroke_btn.isVisibleTo(p)
    commit(p.x, p.x.value() + 10)
    r = po.page_objects(sws.pdf.doc, 0)
    assert graphic_at(sws, (99 + 10 / MM, 99, 201 + 10 / MM, 161)) is not None
    assert undo_steps(sws) == 1
    p.stroke_btn.set_color((0.0, 0.6, 0.0), emit=True)
    assert all(o.ref.stroke == pytest.approx((0, 0.6, 0), abs=0.01) for o in tool.selection())
    toasts = []
    tool.ctx.toast = lambda text, kind="info", **kw: toasts.append(text)
    p.stroke_none.click()                                         # the blue frame has no fill: refused
    assert toasts and all(o.ref.stroke is not None for o in tool.selection())
    p.lock.setChecked(True)
    w0 = p.w.value()
    commit(p.w, w0 * 0.5)
    assert all(o.ref.width in (pytest.approx(2.0, abs=0.01), pytest.approx(3.0, abs=0.01))
               for o in tool.selection())
    assert len(r) == len(po.page_objects(sws.pdf.doc, 0))


def test_fields_refresh_after_undo_and_redo(sws):  # noqa: F811
    tool = sws.tool
    click(tool, sws.view, 0, 150, 130)
    p = panel(sws)
    x0 = p.x.value()
    commit(p.x, 80.0)
    commit(p.width_spin, 5.0)
    assert p.width_spin.value() == pytest.approx(5.0)
    sws.pdf.undo()
    assert p.width_spin.value() == pytest.approx(2.0)
    assert p.x.value() == pytest.approx(80.0, abs=0.06)
    sws.pdf.undo()
    assert p.x.value() == pytest.approx(x0, abs=0.06)
    sws.pdf.redo()
    assert p.x.value() == pytest.approx(80.0, abs=0.06)
    assert undo_steps(sws) == 1


def test_typing_is_not_overwritten_by_a_refresh(sws):  # noqa: F811
    tool = sws.tool
    click(tool, sws.view, 0, 150, 130)
    p = panel(sws)
    p.x.setFocus()
    le = p.x.lineEdit()
    le.selectAll()
    le.insert("12")
    p.refresh()
    if p.x.hasFocus():                                            # offscreen focus may be unavailable
        assert p.x.cleanText() == "12"
    assert undo_steps(sws) == 0


@pytest.mark.parametrize("theme", ["light", "dark"])
def test_screenshots(sws, win, qapp, tmp_path, theme):  # noqa: F811
    from pdfstudio.ui.theme.manager import theme_manager

    tm = theme_manager()
    tm.set_mode(theme, persist=False)
    try:
        out = SHOT_DIR or str(tmp_path)
        sws.inspector.show_tab("props")
        tool = sws.tool
        shots = {
            "text": lambda: click(tool, sws.view, 0, 150, 245),
            "graphic": lambda: click(tool, sws.view, 0, 150, 130),
            "multi": lambda: (tool.select(None), click(tool, sws.view, 0, 105, 540)),
        }
        for name, act in shots.items():
            act()
            sws.view.render_visible_now()
            qapp.processEvents()
            img = win.grab().toImage()
            path = os.path.join(out, f"panel_{name}_{theme}.png")
            assert img.save(path) and os.path.getsize(path) > 10_000
            insp = sws.inspector.grab().toImage()
            insp.save(os.path.join(out, f"inspector_{name}_{theme}.png"))
    finally:
        tm.set_mode("light", persist=False)
