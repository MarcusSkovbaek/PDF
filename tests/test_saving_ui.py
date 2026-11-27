"""Saving in the app: conflicts, copies, autosave/recovery, password, size."""

import json
import os
import time

import pymupdf
import pytest

from pdfstudio.core import annotations as A
from pdfstudio.core.recovery import RecoveryStore
from pdfstudio.ui import saving
from pdfstudio.ui.dialogs import common as dialogs
from tests.test_ui_shell import win  # noqa: F401  (fixture)


@pytest.fixture(autouse=True)
def _clear_auto():
    yield
    for k in ("file_changed", "unwritable", "confirm", "recover", "security"):
        dialogs._AUTO.pop(k, None)


def _dirty(win, path):  # noqa: F811
    ws = win.open_path(path)
    A.add_note(ws.pdf, 0, (100, 100), "ændring", (1, 1, 0))
    return ws


def test_save_writes_in_background_and_clears_dirty(win, qapp, sample_pdf_path):  # noqa: F811
    ws = _dirty(win, sample_pdf_path)
    assert win.save(ws)
    assert not ws.pdf.is_dirty and not win.saver.busy and not win.busy.isVisible()
    assert len(A.list_annotations(pymupdf.open(sample_pdf_path), 0)) == 1


def test_file_changed_by_another_program(win, qapp, sample_pdf_path):  # noqa: F811
    ws = _dirty(win, sample_pdf_path)
    time.sleep(0.01)
    with open(sample_pdf_path, "ab") as fh:
        fh.write(b"\n% other program\n")
    dialogs._AUTO["file_changed"] = "cancel"
    assert not win.save(ws) and ws.pdf.is_dirty
    dialogs._AUTO["file_changed"] = "overwrite"
    assert win.save(ws) and not ws.pdf.is_dirty
    # the warning can be switched off
    win.saver.set_settings(2, False)
    A.add_note(ws.pdf, 0, (150, 100), "igen", (1, 1, 0))
    time.sleep(0.01)
    with open(sample_pdf_path, "ab") as fh:
        fh.write(b"\n% again\n")
    dialogs._AUTO["file_changed"] = "cancel"
    assert win.save(ws)


def test_locked_file_offers_save_as(win, qapp, sample_pdf_path, tmp_path, monkeypatch):  # noqa: F811
    ws = _dirty(win, sample_pdf_path)
    monkeypatch.setattr(saving, "check_writable", lambda p: "locked" if p == ws.pdf.path else None)
    dialogs._AUTO["unwritable"] = "cancel"
    assert not win.save(ws)
    dialogs._AUTO["unwritable"] = "save_as"
    other = str(tmp_path / "andet.pdf")
    monkeypatch.setattr(win.saver, "_ask_path", lambda *a, **k: other)
    assert win.save(ws) and ws.pdf.path == other and os.path.exists(other)


def test_signed_document_asks_once(win, qapp, sample_pdf_path):  # noqa: F811
    ws = _dirty(win, sample_pdf_path)
    doc = ws.pdf.doc
    form = doc.get_new_xref()
    doc.update_object(form, "<< /Fields [] /SigFlags 3 >>")
    doc.xref_set_key(doc.pdf_catalog(), "AcroForm", f"{form} 0 R")
    dialogs._AUTO["confirm"] = "cancel"
    assert not win.save(ws)
    dialogs._AUTO["confirm"] = "ok"
    assert win.save(ws)
    dialogs._AUTO["confirm"] = "cancel"          # not asked again for this document
    A.add_note(ws.pdf, 0, (150, 100), "igen", (1, 1, 0))
    assert win.save(ws)


def test_save_copy_keeps_working_file(win, qapp, sample_pdf_path, tmp_path):  # noqa: F811
    ws = _dirty(win, sample_pdf_path)
    copy = str(tmp_path / "kopi.pdf")
    assert win.save_copy(ws, copy)
    assert ws.pdf.is_dirty and ws.pdf.path == os.path.abspath(sample_pdf_path)
    assert len(A.list_annotations(pymupdf.open(copy), 0)) == 1
    assert "Kopien er gemt" in " ".join(win.toasts.visible_messages())


def test_autosave_writes_and_save_removes_copy(win, qapp, sample_pdf_path):  # noqa: F811
    saver = win.saver
    ws = win.open_path(sample_pdf_path)
    assert saver.autosave_now() == 0                     # nothing unsaved
    A.add_note(ws.pdf, 0, (100, 100), "x", (1, 1, 0))
    assert saver.autosave_now() == 1
    assert saver.autosave_now() == 0                     # no new changes since
    saver.flush()
    folder = saver.store.folder
    pdfs = [f for f in os.listdir(folder) if f.endswith(".pdf")]
    assert len(pdfs) == 1
    assert len(A.list_annotations(pymupdf.open(os.path.join(folder, pdfs[0])), 0)) == 1
    assert pymupdf.open(sample_pdf_path)[0].first_annot is None   # the original is untouched
    win.save(ws)
    saver.flush()
    assert not [f for f in os.listdir(folder) if f.endswith(".pdf")]


def test_autosave_interval_setting(win, qapp):  # noqa: F811
    from pdfstudio.ui.dialogs.settings_dialog import SettingsDialog

    assert win.saver.timer.isActive() and win.saver.timer.interval() == 2 * 60_000
    dlg = SettingsDialog(win)
    dlg.minutes.setValue(5)
    dlg.apply()
    assert win.saver.timer.interval() == 5 * 60_000
    dlg = SettingsDialog(win)
    dlg.autosave.setChecked(False)
    dlg.apply()
    assert not win.saver.timer.isActive() and win.saver.autosave_minutes() == 0


def test_restore_after_crash(win, qapp, sample_pdf_path):  # noqa: F811
    from pdfstudio.core import PdfDocument

    folder = win.saver.store.folder
    crashed = RecoveryStore(folder)
    pdf = PdfDocument.open(sample_pdf_path)
    A.add_note(pdf, 0, (100, 100), "fra nedbruddet", (1, 1, 0))
    crashed.write("abc", pdf.recovery_bytes(), original_path=sample_pdf_path, title="sample.pdf", protected=False)
    with open(crashed._session_file, "w", encoding="utf-8") as fh:
        json.dump({"pid": 999_999_999}, fh)
    dialogs._AUTO["recover"] = "later"
    assert win.saver.check_recovery() == []
    dialogs._AUTO["recover"] = "restore"
    [ws] = win.saver.check_recovery()
    assert ws.pdf.is_dirty and ws.pdf.path == os.path.abspath(sample_pdf_path)
    assert A.list_annotations(ws.pdf.doc, 0)[0].contents == "fra nedbruddet"
    assert win.saver.check_recovery() == []              # adopted, not offered again
    assert win.save(ws)
    win.saver.flush()
    assert not os.path.exists(os.path.join(folder, "abc.pdf"))


def test_discard_recovered_copies(win, qapp, sample_pdf_path):  # noqa: F811
    from pdfstudio.core import PdfDocument

    crashed = RecoveryStore(win.saver.store.folder)
    crashed.write("old", PdfDocument.open(sample_pdf_path).recovery_bytes(), original_path=None, title="x.pdf",
                  protected=False)
    with open(crashed._session_file, "w", encoding="utf-8") as fh:
        json.dump({"pid": 999_999_999}, fh)
    dialogs._AUTO["recover"] = "discard"
    assert win.saver.check_recovery() == []
    assert win.saver.store.orphans() == []


def test_password_action(win, qapp, sample_pdf_path):  # noqa: F811
    ws = win.open_path(sample_pdf_path)
    dialogs._AUTO["security"] = "hemmelig"
    win.actions.trigger("file.security")
    assert ws.pdf.security is not None and ws.pdf.is_dirty
    assert win.save(ws)
    assert pymupdf.open(sample_pdf_path).needs_pass
    dialogs._AUTO["security"] = ""
    win.actions.trigger("file.security")
    assert win.save(ws)
    assert not pymupdf.open(sample_pdf_path).needs_pass


def test_security_dialog_validation(win, qapp):  # noqa: F811
    from pdfstudio.ui.dialogs.security_dialog import SecurityDialog

    dlg = SecurityDialog(win, None)
    dlg.require.setChecked(True)
    assert not dlg.ok.isEnabled()
    dlg.pw1.setText("abc")
    dlg.pw2.setText("abd")
    assert not dlg.ok.isEnabled() and dlg.error.text() == "De to adgangskoder er ikke ens."
    dlg.pw2.setText("abc")
    assert dlg.ok.isEnabled()
    dlg._apply()
    assert dlg.result_security()[1].user_pw == "abc"
    # an already protected document opens the dialog filled in
    from pdfstudio.core.document import Security

    dlg = SecurityDialog(win, Security(user_pw="gammel", permissions=-3388))
    assert dlg.require.isChecked() and dlg.pw1.text() == "gammel" and dlg.ok.isEnabled()


def test_reduce_dialog_saves_smaller_copy(win, qapp, tmp_path):  # noqa: F811
    from tests.conftest import filled_pixmap
    from pdfstudio.ui.dialogs.reduce_dialog import ReduceDialog

    doc = pymupdf.open()
    page = doc.new_page()
    pix = filled_pixmap(2400, 1800, (30, 140, 200), regions=[((0, 0, 1200, 900), (240, 200, 20))])
    page.insert_image(pymupdf.Rect(50, 50, 545, 420), stream=pix.tobytes("jpg", jpg_quality=95))
    src = str(tmp_path / "foto.pdf")
    doc.save(src)
    ws = win.open_path(src)
    dlg = ReduceDialog(win, ws)
    dlg.quality.set_current_key("low", animate=False)
    out = str(tmp_path / "foto (reduceret).pdf")
    assert dlg.run(out) == out
    assert os.path.getsize(out) < os.path.getsize(src)
    assert not ws.pdf.is_dirty
    assert "foto (reduceret).pdf" in " ".join(win.toasts.visible_messages())


def test_reduce_button_asks_where_to_save(win, qapp, sample_pdf_path, monkeypatch):  # noqa: F811
    from PySide6.QtWidgets import QFileDialog, QPushButton
    from pdfstudio.i18n import tr
    from pdfstudio.ui.dialogs.reduce_dialog import ReduceDialog

    ws = win.open_path(sample_pdf_path)
    asked = []
    monkeypatch.setattr(QFileDialog, "getSaveFileName", staticmethod(lambda *a, **k: (asked.append(a) or "", "")))
    dlg = ReduceDialog(win, ws)
    btn = next(b for b in dlg.findChildren(QPushButton) if b.text() == tr("reduce.run"))
    btn.click()                       # cancelled in the file dialog: nothing is written
    assert len(asked) == 1
