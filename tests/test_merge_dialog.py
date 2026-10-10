import pymupdf

from tests.conftest import make_png, make_sample_pdf


def _pdf(tmp_path, name, pages, pw=None):
    doc = pymupdf.open()
    for i in range(pages):
        doc.new_page().insert_text((72, 72), f"{name} {i + 1}")
    path = str(tmp_path / name)
    if pw:
        doc.save(path, encryption=pymupdf.PDF_ENCRYPT_AES_256, user_pw=pw, owner_pw=pw)
    else:
        doc.save(path)
    return path


def _dialog(paths=None):
    from pdfstudio.ui.dialogs.merge_dialog import MergeDialog

    return MergeDialog(None, initial=paths)


def test_empty_dialog_cannot_run(qapp):
    dlg = _dialog()
    assert not dlg.run_btn.isEnabled()
    assert dlg.no_selection.isVisibleTo(dlg)


def test_merge_order_ranges_and_images(qapp, tmp_path):
    a = _pdf(tmp_path, "a.pdf", 3)
    b = _pdf(tmp_path, "b.pdf", 4)
    png = tmp_path / "shot.png"
    png.write_bytes(make_png())
    dlg = _dialog([a, b, str(png), str(tmp_path / "notes.txt")])
    assert [e.name for e in dlg.entries()] == ["a.pdf", "b.pdf", "shot.png"]
    assert dlg.total_label.text() == "3 filer · 8 sider i alt"

    dlg.list.setCurrentRow(1)                     # b.pdf: only pages 4 and 2
    dlg.pages.setText("4, 2")
    dlg.pages.textEdited.emit("4, 2")
    assert "2 af 4 sider" in dlg.list.item(1).text()
    dlg.list.setCurrentRow(2)
    dlg.move_selected(-1)                         # image before b.pdf
    assert [e.name for e in dlg.entries()] == ["a.pdf", "shot.png", "b.pdf"]
    assert dlg.total_label.text() == "3 filer · 6 sider i alt"

    dlg._run()
    doc = pymupdf.open("pdf", dlg.result_bytes)
    assert doc.page_count == 6
    assert "a.pdf 1" in doc[0].get_text()
    assert "b.pdf 4" in doc[4].get_text() and "b.pdf 2" in doc[5].get_text()
    assert [t[1] for t in doc.get_toc() if t[0] == 1] == ["a", "shot", "b"]


def test_invalid_range_blocks_run(qapp, tmp_path):
    dlg = _dialog([_pdf(tmp_path, "a.pdf", 2)])
    dlg.list.setCurrentRow(0)
    dlg.pages.setText("5")
    dlg.pages.textEdited.emit("5")
    assert not dlg.run_btn.isEnabled() and dlg.pages_error.text()
    dlg.pages.setText("")
    dlg.pages.textEdited.emit("")
    assert dlg.run_btn.isEnabled()


def test_locked_pdf_asks_password(qapp, tmp_path, monkeypatch):
    from pdfstudio.ui.dialogs import merge_dialog

    asked = []
    answers = iter(["forkert", "hemmelig"])
    monkeypatch.setattr(merge_dialog, "ask_password", lambda parent, name, wrong=False: asked.append(wrong) or next(answers))
    dlg = _dialog([_pdf(tmp_path, "låst.pdf", 2, pw="hemmelig")])
    assert asked == [False, True]                 # second prompt flags the wrong password
    assert dlg.entries()[0].password == "hemmelig"
    dlg._run()
    assert pymupdf.open("pdf", dlg.result_bytes).page_count == 2


def test_cancelled_password_skips_file(qapp, tmp_path, monkeypatch):
    from pdfstudio.ui.dialogs import merge_dialog

    monkeypatch.setattr(merge_dialog, "ask_password", lambda *a, **k: None)
    dlg = _dialog([_pdf(tmp_path, "låst.pdf", 1, pw="x"), make_sample_pdf(str(tmp_path / "s.pdf")) and str(tmp_path / "s.pdf")])
    assert [e.name for e in dlg.entries()] == ["s.pdf"]
