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
