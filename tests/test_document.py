import os
import socket

import pymupdf
import pytest

from pdfstudio.core import PdfDocument, RELOADED, STRUCTURE, PasswordRequired, InvalidPassword
from pdfstudio.security.netguard import NetworkBlockedError


def test_network_is_blocked():
    with pytest.raises(NetworkBlockedError):
        socket.create_connection(("example.com", 80), timeout=1)
    with pytest.raises(NetworkBlockedError):
        socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    with pytest.raises(NetworkBlockedError):
        socket.getaddrinfo("localhost", 80)


def test_edit_undo_redo(sample_pdf):
    events = []
    sample_pdf.listen(events.append)
    assert sample_pdf.page_count == 3
    with sample_pdf.edit("Slet side", STRUCTURE):
        sample_pdf.doc.delete_page(0)
    assert sample_pdf.page_count == 2
    assert sample_pdf.is_dirty
    assert events[-1].kind == STRUCTURE
    assert sample_pdf.history.undo_label == "Slet side"
    assert sample_pdf.undo()
    assert sample_pdf.page_count == 3
    assert events[-1].kind == RELOADED
    assert sample_pdf.redo()
    assert sample_pdf.page_count == 2


def test_failed_edit_rolls_back(sample_pdf):
    with pytest.raises(RuntimeError):
        with sample_pdf.edit("Bad"):
            sample_pdf.doc.delete_page(0)
            raise RuntimeError("boom")
    assert sample_pdf.page_count == 3
    assert not sample_pdf.history.can_undo


def test_nested_edits_are_one_step(sample_pdf):
    with sample_pdf.edit("Outer", STRUCTURE):
        sample_pdf.doc.delete_page(0)
        with sample_pdf.edit("Inner", STRUCTURE):
            sample_pdf.doc.delete_page(0)
    assert sample_pdf.page_count == 1
    sample_pdf.undo()
    assert sample_pdf.page_count == 3


def test_save_over_original(sample_pdf, sample_pdf_path):
    with sample_pdf.edit("Slet", STRUCTURE):
        sample_pdf.doc.delete_page(0)
    sample_pdf.save()
    assert not sample_pdf.is_dirty
    assert pymupdf.open(sample_pdf_path).page_count == 2
    assert not [f for f in os.listdir(os.path.dirname(sample_pdf_path)) if f.startswith(".folio-")]


def test_password(tmp_path):
    doc = pymupdf.open()
    doc.new_page()
    path = str(tmp_path / "enc.pdf")
    doc.save(path, encryption=pymupdf.PDF_ENCRYPT_AES_256, user_pw="hemmelig", owner_pw="x")
    with pytest.raises(PasswordRequired):
        PdfDocument.open(path)
    with pytest.raises(InvalidPassword):
        PdfDocument.open(path, "forkert")
    pdf = PdfDocument.open(path, "hemmelig")
    assert pdf.page_count == 1


def test_open_image_as_pdf(png_path):
    pdf = PdfDocument.open(png_path)
    assert pdf.page_count == 1
    assert pdf.path is None and pdf.display_name.endswith(".pdf")
