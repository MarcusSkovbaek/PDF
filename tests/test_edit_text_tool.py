"""UI flow of the "Rediger tekst" tool: a whole text surface (a column, or a
standalone text) in one editor, as in Word (core behaviour: test_flow,
test_text_edit)."""

import pymupdf
import pytest
from PySide6.QtCore import QEvent, QPointF, Qt
from PySide6.QtGui import QKeyEvent, QTextCursor

from pdfstudio.core import flow
from pdfstudio.core import text_edit as te
from tests.flowdocs import HEADER, report
from tests.test_annotate_tools import ev
from tests.test_ui_shell import win  # noqa: F401  (fixture)

LS = " "


@pytest.fixture
def tool(win, qapp, sample_pdf_path):  # noqa: F811
    win.open_path(sample_pdf_path)
    ws = win.current_workspace()
    ws.set_mode("edit")
    qapp.processEvents()
    assert ws.tool is not None and ws.tool.id == "edit.text"
    return ws.tool


def _open_edit(win, qapp, path):  # noqa: F811
    win.open_path(path)
    ws = win.current_workspace()
    ws.set_mode("edit")
    qapp.processEvents()
    return ws, ws.tool


@pytest.fixture
def rep_tool(win, qapp, tmp_path):  # noqa: F811
    path = str(tmp_path / "rapport.pdf")
    report(chapters=2).save(path)
    ws, tool = _open_edit(win, qapp, path)
    return tool


def _block(tool, startswith, page=0):
    return next(b for b in tool.blocks(page) if b.text.startswith(startswith))


def _key(editor, key, mods=Qt.KeyboardModifier.NoModifier, text=""):
    editor.keyPressEvent(QKeyEvent(QEvent.Type.KeyPress, key, mods, text))


def _para(tool, startswith):
    """The editor paragraph whose text starts with ``startswith``."""
    q = tool.editor.document().begin()
    while q.isValid():
        if q.text().startswith(startswith):
            return q
        q = q.next()
    raise AssertionError(startswith)


def _set_para(tool, startswith, text):
    """Replace one paragraph's text in the editor, as typing would."""
    q = _para(tool, startswith)
    cur = QTextCursor(q)
    cur.setPosition(q.position())
    cur.setPosition(q.position() + q.length() - 1, QTextCursor.MoveMode.KeepAnchor)
    cur.insertText(text)
    return cur


def _baseline_errors(tool):
    """For every paragraph: how far (px) its first baseline is from the PDF's."""
    view, ed = tool.ctx.view, tool.editor
    doc = ed.document()
    out = []
    q = doc.begin()
    k = 0
    while q.isValid() and k < len(tool.story.pieces):
        doc.documentLayout().blockBoundingRect(q)
        ql = q.layout()
        line = ql.lineAt(0)
        x = line.cursorToX(0)
        x = x[0] if isinstance(x, tuple) else x
        got = QPointF(ed.geometry().x() + ql.position().x() + x, ed.geometry().y() + ql.position().y() + line.y()
                      + line.ascent())
        want = view.page_to_viewport(tool.story.page, QPointF(*tool.story.pieces[k].block.lines[0].origin))
        out.append(max(abs(got.x() - want.x()), abs(got.y() - want.y())))
        q = q.next()
        k += 1
    return out


def _char_point(block, line, word):
    """Display point a little inside ``word`` on ``line`` of ``block``."""
    ln = block.lines[line]
    i = ln.text.index(word)
    x = ln.origin[0] + (ln.char_pos[i][0] - ln.extent[0]) + 1.0
    return x, (ln.bbox.y0 + ln.bbox.y1) / 2, i


# -- F1 / F4: one click, the whole column ----------------------------------------------
def test_one_click_opens_the_column_with_the_caret_at_the_click(tool, qapp):
    b = _block(tool, "Dette")
    x, y, i = _char_point(b, 0, "broedtekst")
    tool.mouse_press(ev(tool.ctx.view, 0, x, y))
    tool.mouse_release(ev(tool.ctx.view, 0, x, y, buttons=Qt.MouseButton.NoButton))
    qapp.processEvents()
    ed = tool.editor
    assert ed is not None
    assert ed.document().blockCount() == len(tool.story.pieces) == 4    # the whole column is one text
    cur = ed.textCursor()
    assert cur.block().text().startswith("Dette")
    assert abs((cur.position() - cur.block().position()) - i) <= 1     # exactly where clicked
    assert max(_baseline_errors(tool)) <= 1.5                           # every paragraph lies on the PDF's
    tool.cancel()


def test_no_frames_while_hovering_or_editing(tool, qapp):
    from PySide6.QtWidgets import QGraphicsRectItem

    view = tool.ctx.view

    def rects():
        return [i for i in view.scene().items() if isinstance(i, QGraphicsRectItem) and i.zValue() >= 20]

    b = _block(tool, "Dette")
    x, y, _i = _char_point(b, 0, "Dette")
    before = len(rects())
    tool.mouse_move(ev(view, 0, x, y, button=Qt.MouseButton.NoButton, buttons=Qt.MouseButton.NoButton))
    assert len(rects()) == before
    assert view.viewport().cursor().shape() == Qt.CursorShape.IBeamCursor
    tool.mouse_press(ev(view, 0, x, y))
    qapp.processEvents()
    assert len(rects()) == before
    tool.cancel()


def test_the_page_behind_shows_no_old_text(tool, qapp):
    tool.start_edit(_block(tool, "Dette"))
    qapp.processEvents()
    assert tool._bg is not None
    img = tool._bg.pixmap().toImage()
    zoom = 1.0 / tool._bg.transform().m11()
    b = _block(tool, "Dette")
    dark = 0
    for x in range(int(b.bbox.x0 * zoom), int(b.bbox.x1 * zoom), 3):
        for y in range(int(b.bbox.y0 * zoom), int(b.bbox.y1 * zoom), 2):
            c = img.pixelColor(x, y)
            dark += c.lightness() < 160
    assert dark == 0
    tool.cancel()


def test_edit_one_paragraph_and_commit_keeps_format(tool):
    b = _block(tool, "Dette")
    tool.start_edit(b)
    _set_para(tool, "Dette", "Rettet afsnit med æøå." + LS + "Anden linje.")
    _key(tool.editor, Qt.Key.Key_Return, Qt.KeyboardModifier.ControlModifier)
    assert tool.editor is None
    new = _block(tool, "Rettet")
    assert new.text == "Rettet afsnit med æøå.\nAnden linje."
    # Times is not embedded in the sample: its metric clone (Nimbus Roman) is used
    assert new.style.serif and ("Times" in new.style.font or "Nimbus" in new.style.font)
    assert abs(new.style.size - 12) < 0.1
    assert abs(new.lines[0].origin[1] - b.lines[0].origin[1]) < 0.5
    assert tool.ctx.pdf.history.undo_label == "Redigér tekst"
    assert _block(tool, "Kvartalsrapport").text == "Kvartalsrapport 2026"   # the rest is as it was


def test_escape_keeps_the_changes(tool):
    tool.start_edit(_block(tool, "Dette"))
    _set_para(tool, "Dette", "Noget andet.")
    _key(tool.editor, Qt.Key.Key_Escape)
    assert tool.editor is None
    assert tool.ctx.pdf.history.can_undo
    assert _block(tool, "Noget").text == "Noget andet."


def test_unchanged_text_creates_no_undo_step(tool):
    tool.start_edit(_block(tool, "Dette"))
    tool.commit()
    assert not tool.ctx.pdf.history.can_undo


# -- F5 / F4 / F6: Enter, Backspace, selection and formatting across paragraphs --------
def test_enter_splits_a_paragraph_and_backspace_joins_them(rep_tool, qapp):
    tool = rep_tool
    s = next(x for x in tool.stories(0) if x.kind == "flow")
    p = s.pieces[2]
    tool.start_edit(p.block)
    q = _para(tool, p.text[:20])
    cut = p.text.index(". ", len(p.text) // 2) + 2
    cur = tool.editor.textCursor()
    cur.setPosition(q.position() + cut)
    tool.editor.setTextCursor(cur)
    _key(tool.editor, Qt.Key.Key_Backspace)                       # the space after the full stop
    _key(tool.editor, Qt.Key.Key_Return, text="\r")              # Enter: a new paragraph
    assert tool.editor.document().blockCount() == len(s.pieces) + 1
    tool.commit()
    c = next(x for x in tool.stories(0) if x.kind == "flow")
    assert c.pieces[2].text == p.text[:cut - 1] and c.pieces[3].text.startswith(p.text[cut:][:20])
    # Backspace at the start of the new paragraph joins them again
    tool.start_edit(c.pieces[3].block)
    q = _para(tool, c.pieces[3].text[:20])
    cur = tool.editor.textCursor()
    cur.setPosition(q.position())
    tool.editor.setTextCursor(cur)
    _key(tool.editor, Qt.Key.Key_Backspace)
    cur = tool.editor.textCursor()
    cur.insertText(" ")
    tool.commit()
    c = next(x for x in tool.stories(0) if x.kind == "flow")
    assert c.pieces[2].text.replace("\n", " ").startswith(p.text[:60])
    assert len(c.pieces) == len(s.pieces)


def test_selection_across_paragraphs_deletes_and_formats(rep_tool, qapp):
    tool = rep_tool
    s = next(x for x in tool.stories(0) if x.kind == "flow")
    a, b = s.pieces[1], s.pieces[2]
    tool.start_edit(a.block)
    qa, qb = _para(tool, a.text[:20]), _para(tool, b.text[:20])
    cur = tool.editor.textCursor()
    cur.setPosition(qa.position() + len(a.text) - 20)
    cur.setPosition(qb.position() + 30, QTextCursor.MoveMode.KeepAnchor)
    tool.editor.setTextCursor(cur)
    tool.toggle_format("italic")                                   # Ctrl+I over two paragraphs
    tool.commit()
    spans = [sp for bl in tool.ctx.pdf.page(0).get_text("dict")["blocks"] for ln in bl.get("lines", [])
             for sp in ln["spans"] if sp["flags"] & 2]
    text = " ".join(sp["text"] for sp in spans)
    assert a.text[-15:].split()[-1] in text and b.text[:30].split()[1] in text
    # deleting across the two paragraphs joins what is left of them
    s = next(x for x in tool.stories(0) if x.kind == "flow")
    a, b = s.pieces[1], s.pieces[2]
    tool.start_edit(a.block)
    qa, qb = _para(tool, a.text[:20]), _para(tool, b.text[:20])
    cur = tool.editor.textCursor()
    cur.setPosition(qa.position() + 40)
    cur.setPosition(qb.position() + 40, QTextCursor.MoveMode.KeepAnchor)
    cur.removeSelectedText()
    tool.commit()
    c = next(x for x in tool.stories(0) if x.kind == "flow")
    assert len(c.pieces) == len(s.pieces) - 1
    assert c.pieces[1].text.replace("\n", " ").startswith((a.text[:40] + b.text[40:])[:60])


# -- F7 / F8: on into the next text, standalone texts -----------------------------------
def test_arrow_past_the_end_goes_on_on_the_next_page(rep_tool, qapp):
    tool = rep_tool
    s = next(x for x in tool.stories(0) if x.kind == "flow")
    tool.start_edit(s.pieces[-1].block)
    cur = tool.editor.textCursor()
    cur.movePosition(QTextCursor.MoveOperation.End)
    tool.editor.setTextCursor(cur)
    _key(tool.editor, Qt.Key.Key_Right)
    assert tool.editor is not None and tool.story.page == 1 and tool.story.kind == "flow"
    assert tool.editor.textCursor().position() == 0
    _key(tool.editor, Qt.Key.Key_Left)                             # and back to the end on page 1
    assert tool.story.page == 0
    assert tool.editor.textCursor().atEnd()
    tool.cancel()


def test_tab_goes_to_the_next_text_surface(tool):
    tool.start_edit(_block(tool, "Dette"))
    _key(tool.editor, Qt.Key.Key_Tab)
    assert tool.story.page == 1 and tool.editor.toPlainText().startswith("Venstre")
    _key(tool.editor, Qt.Key.Key_Tab)
    assert tool.editor.toPlainText().startswith("Hoejre")
    _key(tool.editor, Qt.Key.Key_Backtab, Qt.KeyboardModifier.ShiftModifier)
    assert tool.editor.toPlainText().startswith("Venstre")
    tool.cancel()


def test_a_header_is_a_text_of_its_own(rep_tool, qapp):
    tool = rep_tool
    head = next(s for s in tool.stories(0) if s.role == "header")
    x, y = head.rect.x0 + 5, (head.rect.y0 + head.rect.y1) / 2
    tool.mouse_press(ev(tool.ctx.view, 0, x, y))
    qapp.processEvents()
    assert tool.editor.toPlainText() == HEADER
    tool.cancel()


def test_typing_that_overflows_the_page_flows_on(rep_tool, qapp):
    tool = rep_tool
    before = len(tool.stories(1)[0].pieces[0].text)
    s = next(x for x in tool.stories(0) if x.kind == "flow")
    tool.start_edit(s.pieces[-1].block)
    tool.editor.textCursor().insertText(" Dette er en ny sætning, som skal flyde over på næste side." * 4)
    tool.commit()
    assert "flyde over på næste side." in tool.ctx.pdf.page(1).get_text()
    assert len(tool.ctx.pdf.history._undo) == 1 and before > 0


def test_text_flows_on_while_typing_and_the_caret_follows(rep_tool, qapp):
    tool = rep_tool
    s = next(x for x in tool.stories(0) if x.kind == "flow")
    tool.start_edit(s.pieces[-1].block)
    ed = tool.editor
    cur = ed.textCursor()
    cur.movePosition(QTextCursor.MoveOperation.End)
    ed.setTextCursor(cur)
    assert not tool.flow_now()                       # nothing changed yet: nothing to flow
    extra = " Dette er en ny sætning, som skal flyde over på næste side."
    ed.textCursor().insertText(extra * 4 + " SLUTORD")
    assert tool._flow_timer.isActive()               # flows after a pause
    assert tool.flow_now()
    pdf = tool.ctx.pdf
    assert "SLUTORD" in pdf.page(1).get_text() and "SLUTORD" not in pdf.page(0).get_text()
    # the editor is open again – on the next page, with the caret right after what was typed
    assert tool.editor is not None and tool.story.page == 1
    cur = tool.editor.textCursor()
    assert cur.block().text()[:cur.positionInBlock()].endswith("SLUTORD")
    tool.editor.textCursor().insertText(" og mere")
    tool.commit()
    assert "SLUTORD og mere" in pdf.page(1).get_text()
    assert len(pdf.history._undo) == 1             # one undo step for the whole session
    pdf.undo()
    assert "SLUTORD" not in pdf.doc[1].get_text()


def test_typing_without_overflow_does_not_flow(rep_tool, qapp):
    tool = rep_tool
    s = next(x for x in tool.stories(0) if x.kind == "flow")
    tool.start_edit(s.pieces[0].block)
    tool.editor.textCursor().insertText("Ny ")
    assert not tool.flow_now()
    assert tool.editor is not None and not tool.ctx.pdf.history._undo
    tool.cancel()


# -- appearance -------------------------------------------------------------------------
def test_editor_follows_core_alignment(win, qapp, tmp_path):  # noqa: F811
    doc = pymupdf.open()
    pg = doc.new_page()
    pg.insert_textbox(pymupdf.Rect(100, 100, 500, 200), "Brødtekst der fylder hele tekstområdet ud. " * 4,
                      fontname="helv", fontsize=12)
    pg.insert_textbox(pymupdf.Rect(100, 260, 500, 360), "Højrestillet adresse\nPostnummer og by",
                      fontname="helv", fontsize=12, align=2)
    path = str(tmp_path / "align.pdf")
    doc.save(path)
    ws, tool = _open_edit(win, qapp, path)
    b = next(x for x in tool.blocks(0) if x.text.startswith("Højrestillet"))
    assert te.layout_info(ws.pdf.doc, b).align == "right"
    tool.start_edit(b)
    qapp.processEvents()
    q = _para(tool, "Højrestillet")
    assert q.blockFormat().alignment() & Qt.AlignmentFlag.AlignRight
    assert max(_baseline_errors(tool)) <= 2.0          # (Qt keeps a pixel for the caret at the right)
    tool.cancel()


def test_editor_lines_up_with_a_screen_font_unlike_the_pdfs(win, qapp, tmp_path, monkeypatch):  # noqa: F811
    """The screen font may be far from the PDF's (a family that is not
    installed – Windows' offscreen platform): the click still lands on the
    clicked character and the lines still start where the PDF's do."""
    from pdfstudio.core import fonts

    monkeypatch.setattr(fonts, "qt_family_hint", lambda style: "Courier New")
    doc = pymupdf.open()
    pg = doc.new_page()
    pg.insert_textbox(pymupdf.Rect(100, 100, 500, 200), "Brødtekst der fylder hele tekstområdet ud. " * 4,
                      fontname="helv", fontsize=12)
    pg.insert_textbox(pymupdf.Rect(100, 260, 500, 360), "Højrestillet adresse\nPostnummer og by",
                      fontname="helv", fontsize=12, align=2)
    path = str(tmp_path / "mono.pdf")
    doc.save(path)
    ws, tool = _open_edit(win, qapp, path)
    b = next(x for x in tool.blocks(0) if x.text.startswith("Brødtekst"))
    x, y, i = _char_point(b, 1, "tekstområdet")
    tool.mouse_press(ev(tool.ctx.view, 0, x, y))
    tool.mouse_release(ev(tool.ctx.view, 0, x, y, buttons=Qt.MouseButton.NoButton))
    qapp.processEvents()
    cur = tool.editor.textCursor()
    line0 = len(b.lines[0].text) + 1
    assert abs(cur.positionInBlock() - (line0 + i)) <= 1
    assert max(_baseline_errors(tool)) <= 2.5
    tool.cancel()


def test_font_list_previews_families(tool):
    tool.start_edit(_block(tool, "Dette"))
    panel = tool.properties_widget()
    assert panel.font.count() > 1
    assert panel.font.itemData(1, Qt.ItemDataRole.FontRole) is not None
    tool.cancel()


def test_style_of_the_paragraph_from_the_panel(tool, qapp):
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
    assert abs(_block(tool, "Dette").style.size - 12) < 0.1            # only that paragraph


def test_reset_style_clears_override(tool):
    tool.start_edit(_block(tool, "Fed"))
    panel = tool.properties_widget()
    panel.size.setValue(30.0)
    tool.reset_style()
    assert tool.style_override is None
    tool.cancel()


def _mixed_doc(tmp_path):
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
    ws, tool = _open_edit(win, qapp, _mixed_doc(tmp_path))
    tool.start_edit(tool.blocks(0)[0])
    cur = tool.editor.textCursor()
    i = tool.editor.toPlainText().index("vigtigt")
    cur.setPosition(i + 2)
    assert cur.charFormat().fontWeight() >= 700                   # the bold word is bold in the editor
    cur.setPosition(3)
    assert cur.charFormat().fontWeight() < 700
    px = cur.charFormat().font().pixelSize()
    ws.view.set_zoom(ws.view.zoom * 2)
    qapp.processEvents()
    cur = tool.editor.textCursor()
    cur.setPosition(3)
    assert cur.charFormat().font().pixelSize() >= px * 1.8         # zooming rescales the editor text
    assert max(_baseline_errors(tool)) <= 2.0                     # (twice the pixels per pt now)
    cur.setPosition(i)
    cur.insertText("meget ")
    tool.commit()
    spans = [s for b in ws.pdf.page(0).get_text("dict")["blocks"] for ln in b["lines"] for s in ln["spans"]]
    assert any(s["text"] == "vigtigt" and "Bold" in s["font"] for s in spans)
    assert "meget vigtigt" in ws.pdf.page(0).get_text()


def test_display_font_name():
    from pdfstudio.ui.viewer.tools.edit_text import display_font_name as d

    assert d("ABCDEF+NimbusSans-Regular") == "Nimbus Sans"
    assert d("Times-Roman") == "Times"
    assert d("ArialMT") == "Arial"
    assert d("TimesNewRomanPS-BoldMT") == "Times New Roman Bold"


def test_soft_breaks_restored_before_the_change():
    shown = "aaa bbb ccc ddd"          # three soft breaks at 3, 7, 11
    soft = [3, 7, 11]
    edited = "aaa bbb cXc ddd"
    assert te.restore_soft_breaks(shown, soft, edited) == "aaa\nbbb\ncXc ddd"
    assert te.restore_soft_breaks(shown, soft, "Xaa bbb ccc ddd") == "Xaa bbb ccc ddd"


def test_justified_paragraph_shown_as_one_justified_paragraph(win, qapp, tmp_path):  # noqa: F811
    from tests.test_text_edit import _list_and_paragraph_doc

    pdf = _list_and_paragraph_doc()
    path = str(tmp_path / "j.pdf")
    pdf.save(path)
    ws, tool = _open_edit(win, qapp, path)
    par = next(b for b in tool.blocks(0) if b.text.startswith("Kvartalets"))
    tool.start_edit(par)
    q = _para(tool, "Kvartalets")
    assert LS not in q.text()
    assert q.blockFormat().alignment() & Qt.AlignmentFlag.AlignJustify
    tool.commit()                                    # unchanged -> no undo step
    assert not ws.pdf.history.can_undo


def test_first_line_indent_paragraph_starts_at_the_column(win, qapp, tmp_path):  # noqa: F811
    """A paragraph with a first-line indent: only its first line is indented;
    every later line starts at the column edge, where the core puts them."""
    doc = pymupdf.open()
    pg = doc.new_page()
    lines = ["Dette er et afsnit med indrykket første linje som",
             "fortsætter her på anden linje uden indrykning og",
             "slutter på den tredje linje."]
    for i, t in enumerate(lines):
        pg.insert_text((90 if i == 0 else 72, 100 + 14 * i), t, fontname="helv", fontsize=11)
    path = str(tmp_path / "indent.pdf")
    doc.save(path)
    ws, tool = _open_edit(win, qapp, path)
    b = tool.blocks(0)[0]
    tool.start_edit(b)
    qapp.processEvents()
    view = tool.ctx.view
    assert max(_baseline_errors(tool)) <= 1.5          # the first line, indented
    column = view.page_to_viewport(0, QPointF(72, b.lines[0].origin[1]))
    q = tool.editor.document().begin()
    second = q.layout().lineAt(1)
    assert abs(tool.editor.geometry().x() + q.layout().position().x() + second.x() - column.x()) <= 1.5
    tool.cancel()


def test_list_bullets_are_drawn_with_their_items(win, qapp, tmp_path):  # noqa: F811
    from tests.test_text_edit import _bulleted_list_doc

    path = str(tmp_path / "list.pdf")
    with open(path, "wb") as fh:
        fh.write(_bulleted_list_doc().to_bytes())
    ws, tool = _open_edit(win, qapp, path)
    texts = [b.text for b in tool.blocks(0)]
    assert texts and not any(len(t.strip()) == 1 for t in texts), texts   # bullets are no paragraphs
    item = next(p for s in tool.stories(0) for p in s.pieces if p.bullet is not None)
    tool.start_edit(item.block)
    assert tool.editor.bullets                                      # the editor draws them
    tool.cancel()


# -- rotated text is edited in place -------------------------------------------------
def _rotated_doc(tmp_path):
    doc = pymupdf.open()
    pg = doc.new_page()
    pg.insert_text((72, 90), "Vandret overskrift", fontname="helv", fontsize=16)
    pg.insert_text((80, 520), "Lodret etiket nedefra", fontname="helv", fontsize=12, rotate=90)
    path = str(tmp_path / "rot.pdf")
    doc.save(path)
    return path


def test_rotated_text_editor_is_rotated_with_the_text(win, qapp, tmp_path):  # noqa: F811
    ws, tool = _open_edit(win, qapp, _rotated_doc(tmp_path))
    b = next(x for x in tool.blocks(0) if x.text.startswith("Lodret"))
    assert b.angle == -90
    tool.start_edit(b)
    qapp.processEvents()
    assert tool._proxy is not None and tool.editor.parentWidget() is None   # inside the page scene
    cur = tool.editor.textCursor()
    cur.setPosition(1)
    assert cur.charFormat().font().pixelSize() == round(12 * tool._px_per_pt())   # the text's own size
    # the editor's first baseline sits on the PDF baseline, running upwards
    bx, by = tool.editor.first_baseline()
    page_item = ws.view.page_item(0)
    origin = tool._proxy.mapToItem(page_item, QPointF(bx, by))
    ahead = tool._proxy.mapToItem(page_item, QPointF(bx + 50, by))
    assert abs(origin.x() - b.lines[0].origin[0]) < 0.8 and abs(origin.y() - b.lines[0].origin[1]) < 0.8
    assert abs(ahead.x() - origin.x()) < 0.1 and ahead.y() < origin.y() - 30
    _set_para(tool, "Lodret", "Lodret etiket ændret")
    _key(tool.editor, Qt.Key.Key_Return, Qt.KeyboardModifier.ControlModifier)
    assert tool.editor is None and tool._proxy is None
    new = next(x for x in te.text_blocks(ws.pdf.doc, 0) if x.text.startswith("Lodret"))
    assert new.text == "Lodret etiket ændret" and new.angle == -90
    assert new.lines[0].origin == pytest.approx(b.lines[0].origin, abs=0.5)


def test_mouse_reaches_the_rotated_editor(win, qapp, tmp_path):  # noqa: F811
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


def test_upright_text_on_rotated_page_uses_the_rotated_editor(tool, qapp):
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


# -- "Vis tekstflader" -------------------------------------------------------------------
def test_show_text_surfaces_outlines_visible_pages(tool, qapp):
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
    tool.start_edit(_block(tool, "Dette"))                          # the open surface has no outline
    tool._box_timer.timeout.emit()
    n_edit = tool._box_items[0].path().elementCount()
    tool.cancel()
    tool._box_timer.timeout.emit()
    assert tool._box_items[0].path().elementCount() > n_edit
    panel.show_boxes.setChecked(False)
    assert not tool._box_items


def test_key_hint_does_not_cover_text(rep_tool, qapp):
    """The chip sits beside the editor; it never covers the text being edited."""
    tool = rep_tool
    view = tool.ctx.view
    view.set_zoom(1.0)
    qapp.processEvents()
    s = next(x for x in tool.stories(0) if x.kind == "flow")
    tool.start_edit(s.pieces[1].block)
    qapp.processEvents()
    chip = tool._keys.geometry()
    assert view.viewport().rect().contains(chip)
    assert not chip.intersects(tool.editor.geometry().adjusted(1, 1, -1, -1))
    tool.cancel()
