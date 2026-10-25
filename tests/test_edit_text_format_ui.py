"""Formatting while editing text: selection formats, paragraph alignment and
spacing, plain-text paste, editor undo, overflow and substituted fonts."""

import pymupdf
import pytest
from PySide6.QtCore import QEvent, QMimeData, Qt
from PySide6.QtGui import QGuiApplication, QKeyEvent, QTextCursor

from pdfstudio.core import text_edit as te
from tests.test_ui_shell import win  # noqa: F401  (fixture)


@pytest.fixture
def tool(win, qapp, sample_pdf_path):  # noqa: F811
    win.open_path(sample_pdf_path)
    ws = win.current_workspace()
    ws.set_mode("edit")
    qapp.processEvents()
    return ws.tool


def _block(tool, startswith, page=0):
    return next(b for b in tool.blocks(page) if b.text.startswith(startswith))


def _key(editor, key, mods=Qt.KeyboardModifier.NoModifier):
    editor.keyPressEvent(QKeyEvent(QEvent.Type.KeyPress, key, mods))


def _select(editor, word):
    text = editor.toPlainText()
    a = text.index(word)
    cur = editor.textCursor()
    cur.setPosition(a)
    cur.setPosition(a + len(word), QTextCursor.MoveMode.KeepAnchor)
    editor.setTextCursor(cur)


def _spans(tool, page=0):
    return [s for b in tool.ctx.pdf.page(page).get_text("dict")["blocks"] for ln in b.get("lines", [])
            for s in ln["spans"]]


def test_ctrl_b_bolds_only_the_selection(tool):
    tool.start_edit(_block(tool, "Dette"))
    _select(tool.editor, "broedtekst")
    _key(tool.editor, Qt.Key.Key_B, Qt.KeyboardModifier.ControlModifier)
    assert tool.format_state()["bold"]
    assert tool.collect_formats() == [(12, 22, {"bold": True})]
    assert tool.commit()
    bold = [s["text"] for s in _spans(tool) if s["flags"] & 16 and "broedtekst" in s["text"]]
    assert bold and not any("Dette" in s["text"] for s in _spans(tool) if s["flags"] & 16)


def test_inspector_underline_and_color_apply_to_selection(tool):
    tool.start_edit(_block(tool, "Dette"))
    panel = tool.properties_widget()
    _select(tool.editor, "Times")
    panel.refresh_selection()
    assert panel.scope.text().startswith("Formateringen gælder den markerede")
    panel.underline.click()
    panel.color.set_color((0.8, 0.0, 0.0))
    panel.color.colorChanged.emit((0.8, 0.0, 0.0))
    formats = tool.collect_formats()
    assert formats and formats[0][2].get("underline") is True and formats[-1][2].get("color") == (0.8, 0.0, 0.0)
    tool.commit()
    page = tool.ctx.pdf.page(0)
    word = page.search_for("Times")[0]
    assert any(abs(d["rect"].x0 - word.x0) < 1.5 and d["rect"].y0 > word.y0 for d in page.get_drawings())
    red = [s["text"] for s in _spans(tool) if s["color"] == 0xCC0000]
    assert "Times" in "".join(red)


def test_inspector_without_selection_formats_the_paragraph(tool):
    tool.start_edit(_block(tool, "Indlejret"))
    panel = tool.properties_widget()
    panel.refresh_selection()
    assert "hele afsnittet" in panel.scope.text()
    panel.bold.click()
    assert tool.style_override is not None and tool.style_override.bold


def test_typing_after_ctrl_b_is_bold(tool):
    tool.start_edit(_block(tool, "Indlejret"))
    tool.editor.moveCursor(QTextCursor.MoveOperation.End)
    _key(tool.editor, Qt.Key.Key_B, Qt.KeyboardModifier.ControlModifier)
    tool.editor.insertPlainText(" NY")
    [(a, b, changes)] = tool.collect_formats()
    assert tool.editor.toPlainText()[a:b] == " NY" and changes == {"bold": True}


def test_paragraph_alignment_and_spacing(tool):
    b = _block(tool, "Dette")
    tool.start_edit(b)
    panel = tool.properties_widget()
    assert panel.para_box.isVisibleTo(panel)
    tool.set_paragraph(align="center")
    assert panel.para_align["center"].isChecked()
    tool.set_paragraph(spacing=2.0)
    assert tool.editor._line_px == pytest.approx(2.0 * 1.2 * 12 * tool._px_per_pt(), rel=0.01)
    tool.commit()
    # (two lines this far apart are two blocks now: read them as lines)
    lines = [ln for blk in te.text_blocks(tool.ctx.pdf.doc, 0) for ln in blk.lines
             if ln.text.startswith(("Dette", "Anden"))]
    assert lines[1].origin[1] - lines[0].origin[1] == pytest.approx(28.8, abs=0.5)
    centers = [(ln.bbox.x0 + ln.bbox.x1) / 2 for ln in lines]
    assert abs(centers[0] - centers[1]) < 1.0


def test_paste_from_other_programs_is_plain_text(tool, qapp):
    tool.start_edit(_block(tool, "Indlejret"))
    tool.editor.moveCursor(QTextCursor.MoveOperation.End)
    mime = QMimeData()
    mime.setHtml("<b style='color:red;font-size:40px'>fed tekst</b>")
    mime.setText("fed tekst")
    tool.editor.insertFromMimeData(mime)
    assert tool.editor.toPlainText().endswith("fed tekst")
    assert tool.collect_formats() == []          # no formatting came along
    # copied inside an editor: formatting is kept
    _select(tool.editor, "Hello")
    _key(tool.editor, Qt.Key.Key_B, Qt.KeyboardModifier.ControlModifier)
    _select(tool.editor, "Hello")
    tool.editor.copy()
    tool.editor.moveCursor(QTextCursor.MoveOperation.End)
    tool.editor.insertFromMimeData(QGuiApplication.clipboard().mimeData())
    assert sum(1 for a, b, c in tool.collect_formats() if c.get("bold")) == 2


def test_ctrl_z_in_editor_undoes_typing_not_the_document(tool, win):
    pdf = tool.ctx.pdf
    steps = len(pdf.history._undo)
    tool.start_edit(_block(tool, "Indlejret"))
    tool.editor.moveCursor(QTextCursor.MoveOperation.End)
    tool.editor.insertPlainText("xyz")
    ev = QKeyEvent(QEvent.Type.ShortcutOverride, Qt.Key.Key_Z, Qt.KeyboardModifier.ControlModifier)
    assert tool.editor.event(ev) and ev.isAccepted()   # the editor claims Ctrl+Z
    tool.editor.undo()
    assert not tool.editor.toPlainText().endswith("xyz")
    assert len(pdf.history._undo) == steps


def test_overflow_offers_shrink_to_fit(win, qapp, tmp_path):  # noqa: F811
    doc = pymupdf.open()
    page = doc.new_page()
    page.insert_htmlbox(pymupdf.Rect(72, 60, 400, 200),
                        "<p style='font-family:sans-serif;font-size:11pt'>" + "Tekst der fylder godt. " * 12 + "</p>")
    page.draw_rect(pymupdf.Rect(72, 150, 400, 400), color=(0, 0, 0), fill=(0.8, 0.8, 0.9))
    path = str(tmp_path / "fuld.pdf")
    doc.save(path)
    ws = win.open_path(path)
    ws.set_mode("edit")
    tool = ws.tool
    b = _block(tool, "Tekst")
    tool.start_edit(b)
    tool.editor.setPlainText(b.text.replace("\n", " ") + " Ekstra tekst. " * 25)
    tool.commit()
    toasts = win.toasts.visible_messages()
    assert any("overlapper" in t for t in toasts)
    size_before = _block(tool, "Tekst").style.size
    toast = win.toasts._toasts[-1] if hasattr(win.toasts, "_toasts") else None
    tool._shrink_to_fit(b, b.text.replace("\n", " ") + " Ekstra tekst. " * 25, None, None, None, None)
    nb = _block(tool, "Tekst")
    assert nb.style.size < size_before
    assert max(ln.bbox.y1 for ln in nb.lines) <= 152
    assert any("formindsket" in t for t in win.toasts.visible_messages())


def test_editor_shows_list_item_hanging_indent(win, qapp, tmp_path):  # noqa: F811
    from tests.test_text_format import _list_doc
    path = str(tmp_path / "liste.pdf")
    _list_doc().doc.save(path)
    ws = win.open_path(path)
    ws.set_mode("edit")
    tool = ws.tool
    for start in ("2.", "1."):   # wrapped item, single-line item
        tool.start_edit(_block(tool, start))
        qapp.processEvents()
        fmt = tool.editor.document().begin().blockFormat()
        x_text = pymupdf.get_text_length(start + " ", "helv", 11)
        assert abs(tool._hanging_indent() - x_text) < 0.8
        assert fmt.leftMargin() > 0 and abs(fmt.textIndent() + fmt.leftMargin()) < 0.01
        tool.cancel()
