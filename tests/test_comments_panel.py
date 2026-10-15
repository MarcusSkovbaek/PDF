from pdfstudio.core import annotations as A
from tests.test_ui_shell import win  # noqa: F401  (fixture)


def test_comments_panel_lists_and_navigates(win, qapp, sample_pdf_path):  # noqa: F811
    ws = win.open_path(sample_pdf_path)
    pdf = ws.pdf
    A.add_markup_lines(pdf, 0, "highlight", pdf.page(0).search_for("Dette er en broedtekst"), (1, .84, 0))
    A.add_note(pdf, 0, (330, 92), "Tjek tallene", (1, .84, 0))
    A.add_shape(pdf, 1, "rect", (72, 420), (300, 480), (.9, .2, .2))
    panel = ws.sidebar.comments
    ws.sidebar.show_tab("comments")
    qapp.processEvents()
    tree = panel.tree
    assert tree.topLevelItemCount() == 2                       # pages 1 and 2
    p1 = tree.topLevelItem(0)
    texts = [p1.child(i).text(0) for i in range(p1.childCount())]
    assert texts[0] == "Fremhævning\nDette er en broedtekst"   # exactly the marked words
    assert texts[1] == "Note\nTjek tallene"
    rect_item = tree.topLevelItem(1).child(0)
    panel._activate(rect_item)
    assert ws.mode == "annotate" and ws.tool.id == "annotate.select"
    assert ws.tool.selected is not None and ws.tool.selected.kind == "rect"
    # stays in sync with edits
    A.delete_annotation(pdf, 1, ws.tool.selected.xref)
    qapp.processEvents()
    assert tree.topLevelItemCount() == 1


def test_comments_panel_empty_state(win, qapp, sample_pdf_path):  # noqa: F811
    ws = win.open_path(sample_pdf_path)
    ws.sidebar.show_tab("comments")
    qapp.processEvents()
    assert ws.sidebar.comments.stack.currentIndex() == 0


def _populated(win, qapp, path):  # noqa: F811
    ws = win.open_path(path)
    pdf = ws.pdf
    A.add_markup_lines(pdf, 0, "highlight", pdf.page(0).search_for("Dette er en broedtekst"), (1, .84, 0))
    A.add_note(pdf, 0, (330, 92), "Tjek tallene", (1, .84, 0))
    A.add_shape(pdf, 1, "rect", (72, 420), (300, 480), (.9, .2, .2))
    A.add_ink(pdf, 1, [[(80, 650), (120, 660)]], (.9, .2, .2))
    w, h = A.free_text_size("Husk Søren", 12)
    A.add_free_text(pdf, 1, (300, 600, 300 + w, 600 + h), "Husk Søren", (.1, .1, .1), 12)
    ws.sidebar.show_tab("comments")
    qapp.processEvents()
    return ws, ws.sidebar.comments


def _rows(panel):
    return [it.text(0) for it in panel._items()]


def test_comments_filter_and_count(win, qapp, sample_pdf_path):  # noqa: F811
    ws, panel = _populated(win, qapp, sample_pdf_path)
    assert panel.count.text() == "5 kommentarer" and len(_rows(panel)) == 5
    panel.set_filter("notes")
    assert _rows(panel) == ["Note\nTjek tallene"]
    assert panel.count.text() == "1 af 5 · Noter"
    assert panel.filter.current_key() == "notes"
    panel.set_filter("drawings")
    assert [r.split("\n")[0] for r in _rows(panel)] == ["Rektangel", "Tegning"]
    panel.set_filter("text")
    assert _rows(panel) == ["Tekstfelt\nHusk Søren"]
    assert panel.tree.topLevelItem(0).text(0).startswith("Side 2")
    panel.set_filter("markup")
    assert _rows(panel) == ["Fremhævning\nDette er en broedtekst"]
    # a filter without matches shows its own empty state
    A.delete_annotations(ws.pdf, [(0, a.xref) for a in A.list_annotations(ws.pdf.doc, 0)])
    qapp.processEvents()
    assert panel.stack.currentIndex() == 2 and panel.none_page.title_label.text() == "Ingen markeringer"
    panel.set_filter("all")
    assert panel.stack.currentIndex() == 1 and len(_rows(panel)) == 3


def test_comments_selection_syncs_both_ways(win, qapp, sample_pdf_path):  # noqa: F811
    ws, panel = _populated(win, qapp, sample_pdf_path)
    # page -> list: selecting comments with the tool highlights their rows
    ws.set_tool("annotate.select")
    tool = ws.tool
    p2 = A.list_annotations(ws.pdf.doc, 1)
    tool.set_selection([p2[0], p2[2]])
    qapp.processEvents()
    selected = sorted(it.text(0).split("\n")[0] for it in panel.tree.selectedItems())
    assert selected == ["Rektangel", "Tekstfelt"]
    # ... and keeps them highlighted after an edit rebuilt the list
    A.update_annotations(ws.pdf, [(1, p2[0].xref, {"color": (0, 0, 1)})])
    qapp.processEvents()
    assert len(panel.tree.selectedItems()) == 2
    tool.select(None)
    assert panel.tree.selectedItems() == []
    # list -> page: selecting rows (Ctrl/Shift for several) selects the comments
    rows = list(panel._items())
    rows[0].setSelected(True)
    rows[1].setSelected(True)
    assert sorted(a.kind for a in tool.selection) == ["highlight", "note"]
    panel._activate(rows[3])
    assert [a.kind for a in tool.selection] == ["ink"]
    assert [it.text(0) for it in panel.tree.selectedItems()] == ["Tegning"]


def test_comments_rows_select_tool_from_another_mode(win, qapp, sample_pdf_path):  # noqa: F811
    ws, panel = _populated(win, qapp, sample_pdf_path)
    ws.set_mode("view")
    list(panel._items())[1].setSelected(True)
    assert ws.mode == "annotate" and ws.tool.id == "annotate.select"
    assert [a.kind for a in ws.tool.selection] == ["note"]
