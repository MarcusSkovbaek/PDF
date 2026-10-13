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
