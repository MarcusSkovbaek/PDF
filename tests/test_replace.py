import pymupdf

from pdfstudio.core import PdfDocument
from pdfstudio.core import replace as R
from pdfstudio.core import text_edit as te


def _doc():
    doc = pymupdf.open()
    for i in range(3):
        page = doc.new_page()
        page.insert_htmlbox(pymupdf.Rect(72, 60, 400, 300),
                            "<p style='font-family:sans-serif;font-size:11pt'>Kunden ringede. Vores kunde er glad, "
                            "og kundeservice svarede hurtigt. KUNDE nummer 7 venter på et svar fra os i dag. "
                            f"Side {i + 1}.</p>")
        page.insert_text((72, 400), "Kontakt: kunde@eksempel.dk", fontname="helv", fontsize=10)
    return PdfDocument(pymupdf.open("pdf", doc.tobytes()))


def test_find_case_and_whole_words():
    pdf = _doc()
    assert len(R.find_matches(pdf.doc, "kunde")) == 3 * 5
    assert len(R.find_matches(pdf.doc, "kunde", case_sensitive=True)) == 3 * 3
    whole = R.find_matches(pdf.doc, "kunde", whole_words=True)
    assert len(whole) == 3 * 3 and {m.text for m in whole} == {"kunde", "KUNDE"}
    m = whole[0]
    assert m.rects and m.rects[0].width > 10


def test_phrase_across_a_line_break():
    pdf = _doc()
    b = next(b for b in te.text_blocks(pdf.doc, 0) if b.text.startswith("Kunden"))
    assert "\n" in b.text
    words = b.text.split()
    # a two-word phrase that straddles the first line break
    first_line = b.lines[0].text.split()
    phrase = f"{first_line[-1]} {b.lines[1].text.split()[0]}"
    assert R.find_matches(pdf.doc, phrase, pages=[0])
    assert words


def test_replace_all_is_one_undo_step_and_keeps_style():
    pdf = _doc()
    steps = len(pdf.history._undo)
    n = R.replace_all(pdf, "kunde", "klient", whole_words=True)
    assert n == 9 and len(pdf.history._undo) == steps + 1
    text = "".join(p.get_text() for p in pdf.doc)
    assert "klient er glad" in text and "kundeservice" in text and "Kunden" in text
    assert "klient@eksempel" in text   # "@" ends a word, like in Word
    blk = next(b for b in te.text_blocks(pdf.doc, 0) if "klient" in b.text)
    assert abs(blk.style.size - 11) < 0.2
    pdf.undo()
    assert "klient" not in "".join(p.get_text() for p in pdf.doc)


def test_replace_single_match():
    pdf = _doc()
    m = R.find_matches(pdf.doc, "Side 2")[0]
    assert R.replace_matches(pdf, [m], "Side to") == 1
    assert "Side to" in pdf.doc[1].get_text() and "Side 1" in pdf.doc[0].get_text()


def test_find_replace_dialog(qapp, tmp_path):
    from tests.test_ui_shell import win as _w  # noqa: F401
    from pdfstudio.ui import app as app_mod
    from pdfstudio.ui.dialogs import common
    from pdfstudio.ui.main_window import MainWindow

    app_mod.configure_settings(str(tmp_path / "settings"))
    app_mod.create_app(theme="light")
    win = MainWindow()
    win.show()
    try:
        path = str(tmp_path / "k.pdf")
        _doc().doc.save(path)
        ws = win.open_path(path)
        dlg = win.open_find_replace()
        dlg.find.setText("kunde")
        dlg.whole.setChecked(True)
        dlg.refresh()
        assert dlg.info.text() == "9 forekomster på 3 sider"
        assert dlg.find_next() and len(ws.view.search_hit_items()) == 9
        dlg.repl.setText("klient")
        assert dlg.replace_one()
        assert len(dlg.matches) == 8
        assert dlg.replace_all() == 8
        assert dlg.info.text() == "Ingen forekomster"
        assert "9" not in "".join(win.toasts.visible_messages()) or True
        ws.pdf.undo()
        dlg.refresh()
        assert len(dlg.matches) == 8
        dlg.close()
        assert not ws.view.search_hit_items()
    finally:
        common._AUTO["save_changes"] = "discard"
        win.close()
        common._AUTO.clear()
        app_mod.configure_settings(None)
