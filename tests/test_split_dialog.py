import os

import pymupdf

from pdfstudio.core import PdfDocument
from pdfstudio.core.pages import split_document, split_plan


def _doc(n=7):
    pdf = PdfDocument.new_blank(title="rapport.pdf")
    with pdf.edit("x"):
        for _ in range(n - 1):
            pdf.doc.new_page()
    return pdf


def test_split_plan_matches_split_document():
    pdf = _doc()
    for kwargs in ({}, {"every_n": 3}, {"ranges": "1-2, 5, 6-"}):
        plan = split_plan(pdf, **kwargs)
        parts = split_document(pdf, **kwargs)
        assert [n for n, _ in plan] == [n for n, _ in parts]
        assert [len(g) for _, g in plan] == [pymupdf.open("pdf", b).page_count for _, b in parts]


def test_split_dialog_preview_and_run(qapp, tmp_path):
    from pdfstudio.ui.dialogs.split_dialog import SplitDialog

    pdf = _doc()
    dlg = SplitDialog(None, pdf)
    dlg.folder.setText(str(tmp_path))
    dlg.mode.set_current_key("every_n", animate=False, emit=True)
    dlg.every_n.setValue(3)
    assert dlg.run_button.isEnabled()
    assert "3 filer" in dlg.preview_title.text()

    dlg.mode.set_current_key("ranges", animate=False, emit=True)
    assert not dlg.run_button.isEnabled()          # empty ranges
    dlg.ranges.setText("1-2, 99")
    assert not dlg.run_button.isEnabled() and dlg.error.text()
    dlg.ranges.setText("1-2, 3-")
    assert dlg.run_button.isEnabled() and not dlg.error.isVisibleTo(dlg)

    dlg._run()
    assert len(dlg.written) == 2
    assert all(os.path.exists(p) for p in dlg.written)
    assert pymupdf.open(dlg.written[1]).page_count == 5
    assert pdf.page_count == 7  # document untouched


def test_split_dialog_rejects_missing_folder(qapp, tmp_path):
    from pdfstudio.ui.dialogs.split_dialog import SplitDialog

    dlg = SplitDialog(None, _doc(3))
    dlg.folder.setText(str(tmp_path / "findes-ikke"))
    assert not dlg.run_button.isEnabled()
    assert dlg.error.text()
