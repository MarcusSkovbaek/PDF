"""Saving: password protection kept, conflicts, copies, recovery, size reduction."""

import json
import os
import time

import pymupdf
import pytest

from pdfstudio.core import CONTENT, PdfDocument
from pdfstudio.core import annotations as A
from pdfstudio.core.document import Security, check_writable, write_atomic
from pdfstudio.core.optimize import ReduceOptions, reduce_size
from pdfstudio.core.recovery import RecoveryStore, pid_alive
from tests.conftest import filled_pixmap


def _protected(tmp_path, user="hemmelig", owner="ejer", perms=None, name="enc.pdf"):
    doc = pymupdf.open()
    doc.new_page().insert_text((72, 72), "Fortroligt", fontsize=14)
    path = str(tmp_path / name)
    kw = {"permissions": perms} if perms is not None else {}
    doc.save(path, encryption=pymupdf.PDF_ENCRYPT_AES_256, user_pw=user, owner_pw=owner, **kw)
    return path


def test_password_survives_save(tmp_path):
    path = _protected(tmp_path)
    pdf = PdfDocument.open(path, "hemmelig")
    assert pdf.security is not None and pdf.security.user_pw == "hemmelig"
    A.add_note(pdf, 0, (100, 100), "note", (1, 1, 0))
    pdf.save()
    raw = pymupdf.open(path)
    assert raw.needs_pass
    assert raw.authenticate("hemmelig") in (2, 6)
    again = PdfDocument.open(path, "hemmelig")
    assert len(A.list_annotations(again.doc, 0)) == 1


def test_opened_with_owner_password_is_protected_with_it(tmp_path):
    path = _protected(tmp_path)
    pdf = PdfDocument.open(path, "ejer")
    pdf.save()
    raw = pymupdf.open(path)
    assert raw.needs_pass and raw.authenticate("ejer") == 6


def test_restrictions_without_open_password_are_kept(tmp_path):
    perms = pymupdf.PDF_PERM_ACCESSIBILITY | pymupdf.PDF_PERM_PRINT  # no copying / modifying
    path = _protected(tmp_path, user="", perms=perms)
    pdf = PdfDocument.open(path)
    assert pdf.security is not None and not pdf.security.needs_password
    pdf.save()
    raw = pymupdf.open(path)
    assert not raw.needs_pass and (raw.metadata or {}).get("encryption")
    assert not raw.permissions & pymupdf.PDF_PERM_COPY and raw.permissions & pymupdf.PDF_PERM_PRINT


def test_set_and_remove_password(sample_pdf, tmp_path):
    events = []
    sample_pdf.listen(events.append)
    sample_pdf.set_security(Security(user_pw="ny kode"))
    assert sample_pdf.is_dirty and events[-1].kind == CONTENT
    out = str(tmp_path / "beskyttet.pdf")
    sample_pdf.save(out)
    assert pymupdf.open(out).needs_pass
    sample_pdf.set_security(None)
    sample_pdf.save()
    assert not pymupdf.open(out).needs_pass
    # exports / copies used internally stay unprotected
    assert not pymupdf.open("pdf", sample_pdf.to_bytes()).needs_pass


def test_changed_on_disk_detection(sample_pdf, sample_pdf_path):
    assert not sample_pdf.changed_on_disk()
    time.sleep(0.01)
    with open(sample_pdf_path, "ab") as fh:
        fh.write(b"\n% changed by someone else\n")
    assert sample_pdf.changed_on_disk()
    sample_pdf.save()
    assert not sample_pdf.changed_on_disk()


def test_check_writable(tmp_path):
    f = tmp_path / "a.pdf"
    f.write_bytes(b"%PDF-1.4")
    assert check_writable(str(f)) is None
    assert check_writable(str(tmp_path / "new.pdf")) is None
    os.chmod(f, 0o444)
    try:
        if os.access(str(f), os.W_OK):  # running as root: permissions are not enforced
            pytest.skip("write protection not enforced for this user")
        assert check_writable(str(f)) == "readonly"
    finally:
        os.chmod(f, 0o644)
    assert check_writable(str(tmp_path / "missing" / "x.pdf")) == "folder"


def test_save_copy_keeps_state(sample_pdf, sample_pdf_path, tmp_path):
    A.add_note(sample_pdf, 0, (100, 100), "x", (1, 1, 0))
    copy = str(tmp_path / "kopi.pdf")
    sample_pdf.save_copy(copy)
    assert sample_pdf.is_dirty and sample_pdf.path == os.path.abspath(sample_pdf_path)
    assert len(A.list_annotations(pymupdf.open(copy), 0)) == 1


def test_background_save_keeps_later_edits_dirty(sample_pdf, tmp_path):
    A.add_note(sample_pdf, 0, (100, 100), "før", (1, 1, 0))
    job = sample_pdf.prepare_save()
    A.add_note(sample_pdf, 0, (150, 100), "under gem", (1, 1, 0))   # while the file is written
    out = write_atomic(str(tmp_path / "bg.pdf"), job.data)
    sample_pdf.finish_save(out, job)
    assert sample_pdf.is_dirty                                     # the second note is not in the file
    assert len(A.list_annotations(pymupdf.open(out), 0)) == 1
    sample_pdf.save()
    assert not sample_pdf.is_dirty


def test_save_does_not_reload_or_repair(sample_pdf, sample_pdf_path):
    xref = A.add_note(sample_pdf, 0, (100, 100), "x", (1, 1, 0))
    doc_before = sample_pdf.doc
    sample_pdf.save()
    assert sample_pdf.doc is doc_before                        # no reload: xrefs stay valid
    assert A.list_annotations(sample_pdf.doc, 0)[0].xref == xref
    d = pymupdf.open(sample_pdf_path)
    assert not d.is_repaired and d.page_count == 3


def test_save_keeps_outline_links_metadata_and_labels(tmp_path):
    doc = pymupdf.open()
    for i in range(3):
        doc.new_page().insert_text((72, 72), f"Side {i + 1}")
    doc.set_toc([[1, "Kapitel 1", 1], [2, "Afsnit", 2], [1, "Kapitel 2", 3]])
    doc[0].insert_link({"kind": pymupdf.LINK_GOTO, "from": pymupdf.Rect(72, 60, 150, 80), "page": 2})
    doc.set_metadata({"title": "Rapport", "author": "Forfatter"})
    doc.set_page_labels([{"startpage": 0, "prefix": "", "style": "r", "firstpagenum": 1}])
    path = str(tmp_path / "rich.pdf")
    doc.save(path)
    pdf = PdfDocument.open(path)
    A.add_note(pdf, 1, (100, 100), "x", (1, 1, 0))
    pdf.save()
    out = pymupdf.open(path)
    assert [t[1] for t in out.get_toc()] == ["Kapitel 1", "Afsnit", "Kapitel 2"]
    assert out[0].get_links()[0]["page"] == 2
    assert out.metadata["title"] == "Rapport" and out[1].get_label() == "ii"


def test_signature_detection(tmp_path, sample_pdf):
    assert not sample_pdf.has_signatures()
    doc = sample_pdf.doc
    cat = doc.pdf_catalog()
    form = doc.get_new_xref()
    doc.update_object(form, "<< /Fields [] /SigFlags 3 >>")
    doc.xref_set_key(cat, "AcroForm", f"{form} 0 R")
    assert sample_pdf.has_signatures()


# -- recovery ---------------------------------------------------------------------------
def test_recovery_roundtrip_after_crash(tmp_path, sample_pdf):
    folder = str(tmp_path / "rec")
    crashed = RecoveryStore(folder)
    A.add_note(sample_pdf, 0, (100, 100), "ikke gemt", (1, 1, 0))
    eid = crashed.new_id()
    crashed.write(eid, sample_pdf.recovery_bytes(), original_path=sample_pdf.path, title="sample.pdf",
                  protected=False)
    # the crashed session's process is gone
    with open(crashed._session_file, "w", encoding="utf-8") as fh:
        json.dump({"pid": 999_999_999}, fh)
    store = RecoveryStore(folder)
    [entry] = store.orphans()
    assert entry.original_path == sample_pdf.path and entry.title == "sample.pdf" and not entry.protected
    restored = PdfDocument.open(entry.pdf_file)
    assert A.list_annotations(restored.doc, 0)[0].contents == "ikke gemt"
    store.adopt(entry)
    assert RecoveryStore(folder).orphans() == []       # owned by a live session now
    store.close()
    assert not os.path.exists(entry.pdf_file)


def test_recovery_of_live_session_is_not_offered(tmp_path, sample_pdf):
    folder = str(tmp_path / "rec")
    running = RecoveryStore(folder)
    running.write(running.new_id(), sample_pdf.recovery_bytes(), original_path=None, title="x", protected=False)
    assert RecoveryStore(folder).orphans() == []
    running.close()
    assert os.listdir(folder) == []


def test_recovery_copy_of_protected_document_is_encrypted(tmp_path):
    pdf = PdfDocument.open(_protected(tmp_path), "hemmelig")
    data = pdf.recovery_bytes()
    assert pymupdf.open("pdf", data).needs_pass


def test_pid_alive():
    assert pid_alive(os.getpid()) and not pid_alive(999_999_999) and not pid_alive(0)


# -- reduce size ----------------------------------------------------------------------------
def _image_heavy() -> PdfDocument:
    doc = pymupdf.open()
    for i in range(3):
        page = doc.new_page()
        pix = filled_pixmap(2400, 1800, (200, 80 + 40 * i, 60),
                            regions=[((x, 0, x + 40, 1800), (20, 20, 20)) for x in range(0, 2400, 120)])
        page.insert_image(pymupdf.Rect(50, 50, 545, 420), stream=pix.tobytes("jpg", jpg_quality=95))
        page.insert_text((50, 460), "Billedtekst æøå", fontsize=12)
        a = page.add_text_annot((60, 500), "Kommentar")
        a.set_info(title="Hemmelig Person")
        a.update()
    doc.set_metadata({"author": "Hemmelig Person", "title": "Fotos"})
    return PdfDocument(pymupdf.open("pdf", doc.tobytes()))


def test_reduce_size_downsamples_images():
    pdf = _image_heavy()
    res = reduce_size(pdf, ReduceOptions(images="low"))
    assert res.after < res.before * 0.7 and res.saved_fraction > 0.3
    out = pymupdf.open("pdf", res.data)
    assert out.page_count == 3 and "Billedtekst æøå" in out[0].get_text()
    info = out[0].get_image_info()[0]
    assert info["width"] < 2400
    assert pdf.doc[0].get_image_info()[0]["width"] == 2400   # the open document is untouched
    assert out.metadata["author"] == "Hemmelig Person"


def test_reduce_size_strips_personal_data():
    pdf = _image_heavy()
    res = reduce_size(pdf, ReduceOptions(images=None, strip_metadata=True, strip_authors=True))
    out = pymupdf.open("pdf", res.data)
    assert not out.metadata.get("author")
    assert all((a.info.get("title") or "") == "" for p in out for a in p.annots())
    assert res.after <= res.before


def test_reduce_size_keeps_protection(tmp_path):
    pdf = PdfDocument.open(_protected(tmp_path), "hemmelig")
    res = reduce_size(pdf)
    assert pymupdf.open("pdf", res.data).needs_pass
