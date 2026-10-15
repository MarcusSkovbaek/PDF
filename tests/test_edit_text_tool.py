"""UI flow of the "Rediger tekst" tool (core behaviour is in test_text_edit)."""

import pytest
from PySide6.QtCore import QEvent, QPointF, Qt
from PySide6.QtGui import QKeyEvent

from pdfstudio.core import text_edit as te
from tests.test_ui_shell import win  # noqa: F401  (fixture)


@pytest.fixture
def tool(win, qapp, sample_pdf_path):  # noqa: F811
    win.open_path(sample_pdf_path)
    ws = win.current_workspace()
    ws.set_mode("edit")
    qapp.processEvents()
    assert ws.tool is not None and ws.tool.id == "edit.text"
    return ws.tool


def _block(tool, startswith, page=0):
    return next(b for b in tool.blocks(page) if b.text.startswith(startswith))


def _key(editor, key, mods=Qt.KeyboardModifier.NoModifier):
    editor.keyPressEvent(QKeyEvent(QEvent.Type.KeyPress, key, mods))


def test_edit_and_commit_keeps_format(tool):
    b = _block(tool, "Dette")
    tool.start_edit(b)
    assert tool.editor is not None and tool.editor.toPlainText() == b.text
    tool.editor.setPlainText("Rettet afsnit med æøå.\nAnden linje.")
    _key(tool.editor, Qt.Key.Key_Return, Qt.KeyboardModifier.ControlModifier)
    assert tool.editor is None
    new = _block(tool, "Rettet")
    assert new.text == "Rettet afsnit med æøå.\nAnden linje."
    # Times is not embedded in the sample: its metric clone (Nimbus Roman) is used
    assert new.style.serif and ("Times" in new.style.font or "Nimbus" in new.style.font)
    assert abs(new.style.size - 12) < 0.1
    assert abs(new.lines[0].origin[1] - b.lines[0].origin[1]) < 0.5
    assert tool.ctx.pdf.history.undo_label == "Redigér tekst"


def test_escape_cancels_without_change(tool):
    b = _block(tool, "Dette")
    tool.start_edit(b)
    tool.editor.setPlainText("noget andet")
    _key(tool.editor, Qt.Key.Key_Escape)
    assert tool.editor is None
    assert not tool.ctx.pdf.history.can_undo
    assert _block(tool, "Dette").text == b.text


def test_unchanged_text_creates_no_undo_step(tool):
    tool.start_edit(_block(tool, "Dette"))
    tool.commit()
    assert not tool.ctx.pdf.history.can_undo


def test_tab_saves_and_opens_next_paragraph(tool):
    b = _block(tool, "Dette")
    tool.start_edit(b)
    tool.editor.setPlainText("Første afsnit rettet.")
    _key(tool.editor, Qt.Key.Key_Tab)
    assert tool.block is not None and tool.block.text.startswith("Indlejret")
    assert _block(tool, "Første").text == "Første afsnit rettet."
    tool.cancel()


def test_style_override_from_panel(tool, qapp):
    tool.start_edit(_block(tool, "Fed"))
    panel = tool.properties_widget()
    assert panel.format_box.isVisibleTo(panel)
    panel.size.setValue(20.0)
    panel.color.set_color((0.8, 0.1, 0.1), emit=True)
    assert tool.style_override is not None and tool.style_override.size == 20.0
    tool.commit()
    new = _block(tool, "Fed")
    assert abs(new.style.size - 20.0) < 0.1
    assert new.style.color[0] > 0.7 and new.style.color[1] < 0.2


def test_reset_style_clears_override(tool):
    tool.start_edit(_block(tool, "Fed"))
    panel = tool.properties_widget()
    panel.size.setValue(30.0)
    tool.reset_style()
    assert tool.style_override is None


def test_editor_aligns_with_pdf_baseline(tool, qapp):
    b = _block(tool, "Dette")
    tool.start_edit(b)
    qapp.processEvents()
    view = tool.ctx.view
    want = view.mapFromScene(view.page_to_scene(0, QPointF(*b.lines[0].origin)))
    bx, by = tool.editor.first_baseline()
    got = tool.editor.geometry().topLeft()
    assert abs(got.x() + bx - want.x()) <= 1.5 and abs(got.y() + by - want.y()) <= 1.5
    tool.cancel()


def test_undo_while_editing_cancels(tool):
    tool.start_edit(_block(tool, "Dette"))
    tool.editor.setPlainText("A")
    tool.commit()
    tool.start_edit(_block(tool, "Fed"))
    tool.ctx.pdf.undo()
    assert tool.editor is None
    assert any(b.text.startswith("Dette") for b in te.text_blocks(tool.ctx.pdf.doc, 0))


def test_editor_follows_core_alignment(win, qapp, tmp_path):  # noqa: F811
    import pymupdf

    doc = pymupdf.open()
    pg = doc.new_page()
    pg.insert_textbox(pymupdf.Rect(100, 100, 500, 200), "Brødtekst der fylder hele tekstområdet ud. " * 4,
                      fontname="helv", fontsize=12)
    pg.insert_textbox(pymupdf.Rect(100, 260, 500, 360), "Højrestillet adresse\nPostnummer og by",
                      fontname="helv", fontsize=12, align=2)
    path = str(tmp_path / "align.pdf")
    doc.save(path)
    win.open_path(path)
    ws = win.current_workspace()
    ws.set_mode("edit")
    tool = ws.tool
    b = next(x for x in tool.blocks(0) if x.text.startswith("Højrestillet"))
    assert te.layout_info(ws.pdf.doc, b).align == "right"
    tool.start_edit(b)
    qapp.processEvents()
    assert tool.editor.document().defaultTextOption().alignment() & Qt.AlignmentFlag.AlignRight
    view = tool.ctx.view
    right = view.mapFromScene(view.page_to_scene(0, QPointF(b.bbox.x1, b.bbox.y0))).x()
    assert abs(tool.editor.geometry().right() - right) <= 3
    tool.cancel()


def test_shift_tab_goes_to_previous_paragraph(tool):
    b = _block(tool, "Indlejret")
    tool.start_edit(b)
    _key(tool.editor, Qt.Key.Key_Backtab, Qt.KeyboardModifier.ShiftModifier)
    assert tool.block is not None and tool.block.text.startswith("Dette")
    tool.cancel()


def test_font_list_previews_families(tool):
    tool.start_edit(_block(tool, "Dette"))
    panel = tool.properties_widget()
    assert panel.font.count() > 1
    assert panel.font.itemData(1, Qt.ItemDataRole.FontRole) is not None
    tool.cancel()


def _mixed_doc(tmp_path):
    import pymupdf

    doc = pymupdf.open()
    pg = doc.new_page()
    tw = pymupdf.TextWriter(pg.rect)
    x = 72
    for word, font in (("Dette er ", pymupdf.Font("helv")), ("vigtigt", pymupdf.Font("hebo")),
                       (" for alle.", pymupdf.Font("helv"))):
        tw.append((x, 100), word, font=font, fontsize=12)
        x += font.text_length(word, 12)
    tw.write_text(pg)
    path = str(tmp_path / "mixed.pdf")
    doc.save(path)
    return path


def test_editor_shows_mixed_styles_and_follows_zoom(win, qapp, tmp_path):  # noqa: F811
    win.open_path(_mixed_doc(tmp_path))
    ws = win.current_workspace()
    ws.set_mode("edit")
    tool = ws.tool
    tool.start_edit(tool.blocks(0)[0])
    cur = tool.editor.textCursor()
    i = tool.editor.toPlainText().index("vigtigt")
    cur.setPosition(i + 2)
    assert cur.charFormat().fontWeight() >= 700                   # the bold word is bold in the editor
    cur.setPosition(3)
    assert cur.charFormat().fontWeight() < 700
    px = tool.editor.font().pixelSize()
    ws.view.set_zoom(ws.view.zoom * 2)
    qapp.processEvents()
    assert tool.editor.font().pixelSize() >= px * 1.8              # zooming rescales the editor text
    tool.editor.setPlainText("Dette er meget vigtigt for alle.")
    tool.commit()
    spans = [s for b in ws.pdf.page(0).get_text("dict")["blocks"] for ln in b["lines"] for s in ln["spans"]]
    assert any(s["text"] == "vigtigt" and "Bold" in s["font"] for s in spans)


def test_display_font_name():
    from pdfstudio.ui.viewer.tools.edit_text import display_font_name as d

    assert d("ABCDEF+NimbusSans-Regular") == "Nimbus Sans"
    assert d("Times-Roman") == "Times"
    assert d("ArialMT") == "Arial"
    assert d("TimesNewRomanPS-BoldMT") == "Times New Roman Bold"


def test_soft_breaks_restored_before_the_change():
    from pdfstudio.ui.viewer.tools.edit_text import EditTextTool

    shown = "aaa bbb ccc ddd"          # three soft breaks at 3, 7, 11
    soft = [3, 7, 11]
    edited = "aaa bbb cXc ddd"
    assert EditTextTool._restore_soft_breaks(shown, soft, edited) == "aaa\nbbb\ncXc ddd"
    assert EditTextTool._restore_soft_breaks(shown, soft, "Xaa bbb ccc ddd") == "Xaa bbb ccc ddd"


def test_justified_paragraph_shown_as_one_justified_paragraph(win, qapp, tmp_path):  # noqa: F811
    from tests.test_text_edit import _list_and_paragraph_doc

    pdf = _list_and_paragraph_doc()
    path = str(tmp_path / "j.pdf")
    pdf.save(path)
    win.open_path(path)
    ws = win.current_workspace()
    ws.set_mode("edit")
    tool = ws.tool
    par = next(b for b in tool.blocks(0) if b.text.startswith("Kvartalets"))
    tool.start_edit(par)
    assert "\n" not in tool.editor.toPlainText()
    assert tool.editor.document().defaultTextOption().alignment() & Qt.AlignmentFlag.AlignJustify
    tool.commit()                                    # unchanged -> no undo step
    assert not ws.pdf.history.can_undo


# -- rotated text is edited in place -------------------------------------------------
def _rotated_doc(tmp_path):
    import pymupdf

    doc = pymupdf.open()
    pg = doc.new_page()
    pg.insert_text((72, 90), "Vandret overskrift", fontname="helv", fontsize=16)
    pg.insert_text((80, 520), "Lodret etiket nedefra", fontname="helv", fontsize=12, rotate=90)
    path = str(tmp_path / "rot.pdf")
    doc.save(path)
    return path


def _open_edit(win, qapp, path):  # noqa: F811
    win.open_path(path)
    ws = win.current_workspace()
    ws.set_mode("edit")
    qapp.processEvents()
    return ws, ws.tool


def test_rotated_text_editor_is_rotated_with_the_text(win, qapp, tmp_path):  # noqa: F811
    ws, tool = _open_edit(win, qapp, _rotated_doc(tmp_path))
    b = next(x for x in tool.blocks(0) if x.text.startswith("Lodret"))
    assert b.angle == -90
    tool.start_edit(b)
    qapp.processEvents()
    assert tool._proxy is not None and tool.editor.parentWidget() is None   # inside the page scene
    assert tool.editor.font().pixelSize() == round(12 * tool._px_per_pt())   # the text's own size
    # the editor's first baseline sits on the PDF baseline, running upwards
    bx, by = tool.editor.first_baseline()
    page_item = ws.view.page_item(0)
    origin = tool._proxy.mapToItem(page_item, QPointF(bx, by))
    ahead = tool._proxy.mapToItem(page_item, QPointF(bx + 50, by))
    assert abs(origin.x() - b.lines[0].origin[0]) < 0.6 and abs(origin.y() - b.lines[0].origin[1]) < 0.6
    assert abs(ahead.x() - origin.x()) < 0.1 and ahead.y() < origin.y() - 30
    tool.editor.setPlainText("Lodret etiket ændret")
    _key(tool.editor, Qt.Key.Key_Return, Qt.KeyboardModifier.ControlModifier)
    assert tool.editor is None and tool._proxy is None
    new = next(x for x in te.text_blocks(ws.pdf.doc, 0) if x.text.startswith("Lodret"))
    assert new.text == "Lodret etiket ændret" and new.angle == -90
    assert new.lines[0].origin == pytest.approx(b.lines[0].origin, abs=0.5)


def test_mouse_reaches_the_rotated_editor(win, qapp, tmp_path):  # noqa: F811
    from tests.test_annotate_tools import ev

    ws, tool = _open_edit(win, qapp, _rotated_doc(tmp_path))
    b = next(x for x in tool.blocks(0) if x.text.startswith("Lodret"))
    tool.start_edit(b)
    qapp.processEvents()
    assert tool.editor.textCursor().position() == len(b.text)
    x, y = b.lines[0].origin
    tool.mouse_press(ev(ws.view, 0, x - 4, y - 1))                # just after the first glyph's start
    tool.mouse_release(ev(ws.view, 0, x - 4, y - 1, buttons=Qt.MouseButton.NoButton))
    assert tool.editor is not None                                 # still editing (no commit)
    assert tool.editor.textCursor().position() <= 1                # caret moved to the click
    tool.mouse_double_click(ev(ws.view, 0, x - 4, y - 12))         # inside "Lodret"
    assert tool.editor.textCursor().selectedText() == "Lodret"
    tool.mouse_press(ev(ws.view, 0, 400, 700))                     # outside: commit (unchanged)
    assert tool.editor is None and not ws.pdf.history.can_undo


def test_upright_text_on_rotated_page_uses_the_flat_editor(tool, qapp):
    b = tool.blocks(2)[0]                                          # sample page 3 is rotated by 90
    tool.ctx.view.go_to_page(2)
    tool.start_edit(b)
    qapp.processEvents()
    assert b.angle == 90 and tool._proxy is not None
    tool.cancel()
    assert tool._proxy is None


# -- font picker ---------------------------------------------------------------------
def test_font_picker_search_and_recent_fonts(tool, qapp):
    from pdfstudio.ui.viewer.tools.edit_text import recent_fonts

    tool.start_edit(_block(tool, "Dette"))
    panel = tool.properties_widget()
    panel.font.showPopup()
    popup = panel.font.popup
    assert popup is not None
    names = [popup.list.item(i).text() for i in popup._font_rows()]
    assert names[0] == panel.font.itemText(0)                      # the document's own font first
    popup.search.setText("cour")
    rows = [popup.list.item(i).text() for i in popup._font_rows()]
    assert rows and all("cour" in r.casefold() for r in rows)
    from PySide6.QtWidgets import QApplication

    QApplication.sendEvent(popup.search, QKeyEvent(QEvent.Type.KeyPress, Qt.Key.Key_Return,
                                                   Qt.KeyboardModifier.NoModifier))  # Enter picks the first match
    assert panel.font.popup is None or not panel.font.popup.isVisible()
    assert tool.style_override is not None and tool.style_override.font == rows[0]
    assert recent_fonts()[0] == rows[0]
    panel.font.showPopup()                                         # ... and it is "Senest brugt" now
    popup = panel.font.popup
    heads = [popup.list.item(i).text() for i in range(popup.list.count())
             if popup.list.item(i).data(Qt.ItemDataRole.UserRole + 1) == "header"]
    assert heads[:2] == ["I dokumentet", "Senest brugt"]
    popup.search.setText("zzzz-ingen")
    assert not popup._font_rows() and popup.empty.isVisibleTo(popup)
    popup.close()
    tool.cancel()


# -- "Vis alle tekstbokse" -------------------------------------------------------------
def test_show_all_text_boxes_outlines_visible_pages(tool, qapp):
    from pdfstudio.ui import app as app_mod

    panel = tool.properties_widget()
    assert not panel.show_boxes.isChecked() and not tool._box_items
    panel.show_boxes.setChecked(True)
    tool._box_timer.timeout.emit()                                  # (debounced)
    assert 0 in tool._box_items
    item = tool._box_items[0]
    assert item.path().boundingRect().contains(QPointF(*tool.blocks(0)[0].lines[0].origin))
    assert str(app_mod.settings().value("edit/showAllTextBoxes")).lower() == "true"
    pages = set(tool._box_items)
    assert pages <= set(tool.ctx.view.visible_pages(extra_px=tool.ctx.view.viewport().height()))
    tool.start_edit(_block(tool, "Dette"))                          # the open paragraph has its own frame
    tool._box_timer.timeout.emit()
    n_edit = tool._box_items[0].path().elementCount()
    tool.cancel()
    tool._box_timer.timeout.emit()
    assert tool._box_items[0].path().elementCount() > n_edit
    panel.show_boxes.setChecked(False)
    assert not tool._box_items


@pytest.mark.parametrize("zoom", [1.4, 0.6])
def test_key_hint_does_not_cover_other_text(tool, qapp, zoom):
    """The chip sits beside the frame on free paper; where none is left (tiny
    zoom) it gives way as soon as the user types."""
    from PySide6.QtCore import QRect

    view = tool.ctx.view
    view.set_zoom(zoom)
    qapp.processEvents()
    for start in ("Dette", "Indlejret", "Fed"):
        b = _block(tool, start)
        tool.start_edit(b)
        qapp.processEvents()
        chip = tool._keys.geometry()
        frame = tool._viewport_rect(tool._frame).toRect()
        assert not chip.intersects(frame.adjusted(1, 1, -1, -1))
        assert view.viewport().rect().contains(chip)
        covered = []
        for o in tool.blocks(0):
            if o.bbox == b.bbox:
                continue
            tl = view.page_to_viewport(0, QPointF(o.bbox.x0, o.bbox.y0)).toPoint()
            br = view.page_to_viewport(0, QPointF(o.bbox.x1, o.bbox.y1)).toPoint()
            covered += [o.text] if chip.intersects(QRect(tl, br)) else []
        if zoom >= 1.4:
            assert not covered, (start, covered)
        if covered:
            assert tool._keys_covers_text
            tool.editor.insertPlainText("x")
            assert not tool._keys.isVisible()
        else:
            tool.editor.insertPlainText("x")
            assert tool._keys.isVisible()                          # harmless: it stays
        tool.cancel()


def test_first_line_indent_paragraph_editor_starts_at_the_column(win, qapp, tmp_path):  # noqa: F811
    """A paragraph with a first-line indent: only the editor's first line is
    indented; the box (and every later line) starts at the column edge, where
    the core puts the reflowed lines."""
    import pymupdf

    doc = pymupdf.open()
    pg = doc.new_page()
    lines = ["Dette er et afsnit med indrykket første linje som",
             "fortsætter her på anden linje uden indrykning og",
             "slutter på den tredje linje."]
    for i, t in enumerate(lines):
        pg.insert_text((90 if i == 0 else 72, 100 + 14 * i), t, fontname="helv", fontsize=11)
    path = str(tmp_path / "indent.pdf")
    doc.save(path)
    win.open_path(path)
    ws = win.current_workspace()
    ws.set_mode("edit")
    qapp.processEvents()
    tool = ws.tool
    b = tool.blocks(0)[0]
    tool.start_edit(b)
    qapp.processEvents()
    view = tool.ctx.view
    first = view.mapFromScene(view.page_to_scene(0, QPointF(*b.lines[0].origin)))
    column = view.mapFromScene(view.page_to_scene(0, QPointF(72, b.lines[0].origin[1])))
    bx, by = tool.editor.first_baseline()
    geo = tool.editor.geometry()
    assert abs(geo.x() + bx - first.x()) <= 1.5 and abs(geo.y() + by - first.y()) <= 1.5
    assert abs(geo.x() - column.x()) <= 1.5          # the box starts at the column, not at the indent
    second = tool.editor.document().begin().layout().lineAt(1)
    assert abs(geo.x() + second.x() - column.x()) <= 1.5
    tool.cancel()
