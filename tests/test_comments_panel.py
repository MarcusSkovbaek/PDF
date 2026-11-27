"""The comment list (right panel) and commenting on text selections."""

from datetime import datetime, timedelta

import pytest
from PySide6.QtCore import QPointF, Qt

from pdfstudio.core import annotations as A
from pdfstudio.core import comments as C
from pdfstudio.core import identity, textsel
from pdfstudio.ui.dialogs import common as dialogs
from pdfstudio.ui.panels.comments import format_when, initials
from tests.test_ui_shell import win  # noqa: F401  (fixture)


@pytest.fixture(autouse=True)
def _author():
    identity.set_author("Anne Andersen")
    yield
    identity.set_author(None)


def _lines(pdf, page, text):
    pt = textsel.page_text(pdf.doc, page)
    a = "".join(g.c for g in pt.chars).index(text)
    return textsel.selection_rects(pt, a, a + len(text))


def _populated(win, qapp, path):  # noqa: F811
    ws = win.open_path(path)
    pdf = ws.pdf
    C.add_comment(pdf, {0: _lines(pdf, 0, "Dette er en broedtekst")}, "Ret stavningen", (1, .84, 0))
    A.add_note(pdf, 0, (330, 92), "Tjek tallene", (1, .84, 0))
    A.add_shape(pdf, 1, "rect", (72, 420), (300, 480), (.9, .2, .2))
    A.add_ink(pdf, 1, [[(80, 650), (120, 660)]], (.9, .2, .2))
    w, h = A.free_text_size("Husk Søren", 12)
    A.add_free_text(pdf, 1, (300, 600, 300 + w, 600 + h), "Husk Søren", (.1, .1, .1), 12)
    ws.show_comments()
    qapp.processEvents()
    return ws, ws.comments


def _kinds(panel):
    return [c.comment.kind for c in panel.cards()]


def test_cards_show_author_quote_text(win, qapp, sample_pdf_path):  # noqa: F811
    ws, panel = _populated(win, qapp, sample_pdf_path)
    assert ws.inspector.current_tab() == "comments" and panel.isVisible()
    assert _kinds(panel) == ["note", "highlight", "rect", "freetext", "ink"]   # top to bottom
    hl = panel.cards()[1]
    assert hl.name.text() == "Anne Andersen" and hl.avatar._name == "Anne Andersen"
    assert hl.quote.text() == "»Dette er en broedtekst«"
    assert hl.body.text() == "Ret stavningen"
    assert "Fremhævning" in hl.meta.text() and "Lige nu" in hl.meta.text()
    assert panel.count.text() == "5 kommentarer"
    assert ws.inspector.tabs._segments[1].text == "Kommentarer  5"


def test_filters_search_and_sort(win, qapp, sample_pdf_path):  # noqa: F811
    ws, panel = _populated(win, qapp, sample_pdf_path)
    panel.set_filter("notes")
    assert _kinds(panel) == ["note"] and panel.count.text() == "1 af 5 vist" and panel.reset_btn.isVisible()
    panel.set_filter("drawings")
    assert _kinds(panel) == ["rect", "ink"]
    panel.set_filter("all")
    panel.set_query("stavning")
    assert _kinds(panel) == ["highlight"]
    panel.set_query("BROED")               # the quote is searched too
    assert _kinds(panel) == ["highlight"]
    panel.set_query("zzz")
    assert panel.stack.currentIndex() == 2
    panel.reset_filters()
    assert len(panel.cards()) == 5 and not panel.reset_btn.isVisible()
    # status: resolved comments are hidden by "Åbne"
    hl = panel.cards()[1].comment
    C.set_resolved(ws.pdf, hl.page, hl.xref, True)
    qapp.processEvents()
    assert panel.card(hl.page, hl.xref).property("resolved") is True
    panel.set_status_filter("open")
    assert "highlight" not in _kinds(panel)
    panel.set_status_filter("resolved")
    assert _kinds(panel) == ["highlight"]
    panel.set_status_filter("all")
    # author filter
    identity.set_author("Bo")
    A.add_note(ws.pdf, 1, (500, 500), "Bos note", (1, .84, 0))
    qapp.processEvents()
    assert panel.authors() == ["Anne Andersen", "Bo"]
    panel.set_author_filter("Bo")
    assert [c.comment.text for c in panel.cards()] == ["Bos note"]
    panel.set_author_filter("")
    # sorting: by author, flat list with page tags
    panel.set_sort("author")
    assert panel.cards()[-1].comment.author == "Bo" and "side 2" in panel.cards()[-1].meta.text()
    panel.set_sort("page")


def test_new_comment_from_selection_opens_editor(win, qapp, sample_pdf_path):  # noqa: F811
    ws = win.open_path(sample_pdf_path)
    tool = ws.tool
    assert tool.id == "view.select"
    pt = tool.sel.page_text(0)
    full = "".join(g.c for g in pt.chars)
    a = full.index("broedtekst")
    tool.sel.anchor, tool.sel.focus = (0, a), (0, a + len("broedtekst"))
    assert tool.has_selection()
    assert win.actions.trigger("comments.new")                 # Ctrl+Alt+M
    qapp.processEvents()
    qapp.processEvents()
    [c] = C.comments(ws.pdf.doc)
    assert c.quote == "broedtekst" and c.kind == "highlight"
    card = ws.comments.card(c.page, c.xref)
    assert card is not None and card.selected and card.editor.active
    card.editor.edit.setPlainText("Skal være »brødtekst«")
    card.editor._commit()
    qapp.processEvents()
    assert C.comment(ws.pdf.doc, c.page, c.xref).text == "Skal være »brødtekst«"
    assert ws.pdf.history.undo_label == "Redigér kommentar"
    # one undo removes the text, the next one the highlight
    ws.pdf.undo()
    ws.pdf.undo()
    qapp.processEvents()
    assert C.comments(ws.pdf.doc) == [] and ws.comments.cards() == []


def test_comment_without_selection_picks_note_tool(win, qapp, sample_pdf_path):  # noqa: F811
    ws = win.open_path(sample_pdf_path)
    win.actions.trigger("comments.new")
    assert ws.mode == "annotate" and ws.tool.id == "annotate.note"


def test_replies_status_and_delete_from_card(win, qapp, sample_pdf_path):  # noqa: F811
    ws, panel = _populated(win, qapp, sample_pdf_path)
    card = panel.cards()[1]
    key = card.key
    card.start_reply()
    assert card.reply_editor.active
    card.reply_editor.edit.setPlainText("Enig")
    card.reply_editor._commit()
    qapp.processEvents()
    card = panel.card(*key)
    assert [r.text for r in card.comment.replies] == ["Enig"] and len(card._reply_rows) == 1
    card.status_chosen.emit(key, "accepted")
    qapp.processEvents()
    assert panel.card(*key).chip.isVisible() and panel.card(*key).chip.text() == "Accepteret"
    card.resolve_toggled.emit(key, True)
    qapp.processEvents()
    assert C.comment(ws.pdf.doc, *key).resolved
    # the card menu offers the actions
    texts = [a.text() for a in card.build_menu().actions()]
    assert "Svar" in texts and "Genåbn" in texts and "Slet" in texts
    # deleting a thread with replies asks first
    dialogs._AUTO["confirm"] = "cancel"
    try:
        assert not panel.delete_keys([key])
        assert C.comment(ws.pdf.doc, *key) is not None
        dialogs._AUTO["confirm"] = "ok"
        assert panel.delete_keys([key])
    finally:
        dialogs._AUTO.pop("confirm", None)
    qapp.processEvents()
    assert panel.card(*key) is None and C.comment(ws.pdf.doc, *key) is None
    assert len(ws.pdf.page(0).annots().__iter__.__self__ and list(ws.pdf.page(0).annots())) == 1  # the note


def test_card_editor_keeps_focus_across_other_changes(win, qapp, sample_pdf_path):  # noqa: F811
    ws, panel = _populated(win, qapp, sample_pdf_path)
    card = panel.cards()[0]
    card.start_edit()
    assert card.editor.active
    card.editor.edit.setPlainText("Halvfærdig")
    A.add_note(ws.pdf, 1, (450, 450), "Andet sted", (1, .84, 0))   # change on another page
    qapp.processEvents()
    assert panel.card(*card.key) is card and card.editor.active
    assert card.editor.edit.toPlainText() == "Halvfærdig"


def test_cards_and_page_selection_stay_in_sync(win, qapp, sample_pdf_path):  # noqa: F811
    ws, panel = _populated(win, qapp, sample_pdf_path)
    ws.set_tool("annotate.select")
    tool = ws.tool
    p2 = A.list_annotations(ws.pdf.doc, 1)
    tool.set_selection([p2[0], p2[2]])
    qapp.processEvents()
    assert sorted(c.comment.kind for c in panel.cards() if c.selected) == ["freetext", "rect"]
    A.update_annotations(ws.pdf, [(1, p2[0].xref, {"color": (0, 0, 1)})])   # survives an edit
    qapp.processEvents()
    assert len([c for c in panel.cards() if c.selected]) == 2
    tool.select(None)
    qapp.processEvents()
    assert not any(c.selected for c in panel.cards())
    # card -> page (Ctrl+click adds)
    cards = panel.cards()
    panel._card_activated(cards[0], False)
    panel._card_activated(cards[1], True)
    assert sorted(a.kind for a in tool.selection) == ["highlight", "note"]


def test_card_click_from_view_mode_selects_on_page(win, qapp, sample_pdf_path):  # noqa: F811
    ws, panel = _populated(win, qapp, sample_pdf_path)
    win.set_mode("view")
    panel._card_activated(panel.cards()[0], False)
    assert ws.mode == "annotate" and ws.tool.id == "annotate.select"
    assert [a.kind for a in ws.tool.selection] == ["note"]


def test_f8_steps_through_comments(win, qapp, sample_pdf_path):  # noqa: F811
    ws, panel = _populated(win, qapp, sample_pdf_path)
    win.set_mode("view")
    ws.view.go_to_page(0)
    assert win.actions.trigger("comments.next")
    first = panel.selected_keys
    assert win.actions.trigger("comments.next")
    second = panel.selected_keys
    assert first != second and len(second) == 1
    assert win.actions.trigger("comments.prev")
    assert panel.selected_keys == first
    for _ in range(4):
        win.actions.trigger("comments.next")
    assert panel.selected_keys[0][0] == 1    # moved on to page 2


def test_empty_state_and_tab_switching(win, qapp, sample_pdf_path):  # noqa: F811
    ws = win.open_path(sample_pdf_path)
    ws.show_comments()
    qapp.processEvents()
    assert ws.comments.stack.currentIndex() == 0 and not ws.comments.header.isVisible()
    ws.inspector.show_tab("props")
    assert ws.inspector.current_tab() == "props"


def test_selection_bar_and_context_menu_actions(win, qapp, sample_pdf_path):  # noqa: F811
    ws = win.open_path(sample_pdf_path)
    tool = ws.tool
    pt = tool.sel.page_text(0)
    full = "".join(g.c for g in pt.chars)
    a = full.index("Anden linje")
    tool.sel.anchor, tool.sel.focus = (0, a), (0, a + 11)
    tool.selection_finished()
    bar = tool.sel_ui.bar
    assert bar.isVisible() and set(bar.buttons) == {"comment", "highlight", "underline", "strikeout", "replace", "copy"}
    bar.buttons["underline"].click()
    [c] = C.comments(ws.pdf.doc)
    assert c.kind == "underline" and c.quote == "Anden linje" and not bar.isVisible()
    # replace text: strike-out + caret, editor opens for the new text
    tool.sel.anchor, tool.sel.focus = (0, a), (0, a + 5)
    assert tool.run_selection_action("replace")
    qapp.processEvents()
    rep = next(c for c in C.comments(ws.pdf.doc) if c.kind == "replace")
    assert rep.quote == "Anden" and ws.comments.card(rep.page, rep.xref).editor.active
    # insert text at a caret (right-click menu entry)
    caret = full.index("Dette")
    assert tool.sel_ui.run("insert", (0, caret))
    ins = next(c for c in C.comments(ws.pdf.doc) if c.kind == "insert")
    assert ins.rect.width > 0


def test_markup_tool_marks_exact_characters(win, qapp, sample_pdf_path):  # noqa: F811
    ws = win.open_path(sample_pdf_path)
    ws.set_tool("annotate.highlight")
    tool = ws.tool
    pt = tool.sel.page_text(0)
    full = "".join(g.c for g in pt.chars)
    a = full.index("rapport")
    tool.sel.anchor, tool.sel.focus = (0, a), (0, a + 7)
    tool.selection_finished()
    [c] = C.comments(ws.pdf.doc)
    assert c.kind == "highlight" and c.quote == "rapport"


def test_triple_click_selects_paragraph(win, qapp, sample_pdf_path):  # noqa: F811
    from PySide6.QtTest import QTest

    ws = win.open_path(sample_pdf_path)
    view = ws.view
    view.go_to_page(0)
    qapp.processEvents()
    tool = ws.tool
    pt = tool.sel.page_text(0)
    full = "".join(g.c for g in pt.chars)
    r = pt.chars[full.index("broedtekst") + 2].rect
    p = view.page_to_viewport(0, QPointF((r.x0 + r.x1) / 2, (r.y0 + r.y1) / 2)).toPoint()
    vp = view.viewport()
    QTest.mouseDClick(vp, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, p)
    assert tool.selected_text() == "broedtekst"
    QTest.mouseClick(vp, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, p)
    assert tool.selected_text().startswith("Dette er en broedtekst skrevet med Times.")


def test_time_and_initials_helpers():
    now = datetime(2026, 10, 25, 14, 30).astimezone()
    assert format_when(now - timedelta(seconds=20), now) == "Lige nu"
    assert format_when(now - timedelta(minutes=5), now) == "For 5 min. siden"
    assert format_when(now - timedelta(hours=3), now) == "I dag 11.30"
    assert format_when(now - timedelta(days=1), now) == "I går 14.30"
    assert format_when(now - timedelta(days=20), now) == "5. okt. 14.30"
    assert format_when(now - timedelta(days=400), now).endswith("2025")
    assert format_when(None) == ""
    assert initials("Anne Marie Andersen") == "AA" and initials("bo") == "BO" and initials("") == "?"


def test_summarize_comments_action(win, qapp, sample_pdf_path, tmp_path):  # noqa: F811
    ws, panel = _populated(win, qapp, sample_pdf_path)
    out = str(tmp_path / "oversigt.pdf")
    assert win.summarize_comments(out) == out
    import pymupdf

    with pymupdf.open(out) as d:
        text = "".join(p.get_text() for p in d)
    assert "Kommentaroversigt" in text and "Ret stavningen" in text and "Husk Søren" in text
    assert win.current_workspace().pdf.path == out          # opened for reading


def test_left_click_on_more_opens_the_menu(win, qapp, sample_pdf_path):  # noqa: F811
    from PySide6.QtCore import QTimer
    from PySide6.QtTest import QTest
    from PySide6.QtWidgets import QApplication
    ws, panel = _populated(win, qapp, sample_pdf_path)
    shown = []

    def click(button):
        def grab():
            menu = QApplication.activePopupWidget()
            shown.append([a.text() for a in menu.actions()] if menu else None)
            if menu:
                menu.close()
        QTimer.singleShot(50, grab)
        QTest.mouseClick(button, Qt.MouseButton.LeftButton)
        qapp.processEvents()

    card = panel.cards()[1]
    key = card.key
    click(card.more)                       # the comment's "…"
    assert shown[-1] and "Svar" in shown[-1]
    card.start_reply()
    card.reply_editor.edit.setPlainText("Enig")
    card.reply_editor._commit()
    qapp.processEvents()
    row = next(iter(panel.card(*key)._reply_rows.values()))
    click(row.more)                        # a reply's "…"
    assert shown[-1] and "Slet svar" in shown[-1]
