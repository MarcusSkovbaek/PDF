import os

import pymupdf
import pytest

from pdfstudio.core import STRUCTURE, InvalidPassword, PasswordRequired, PdfDocument
from pdfstudio.core.document import DocumentError
from pdfstudio.core.pages import (
    delete_pages,
    duplicate_pages,
    extract_pages,
    insert_blank_page,
    insert_pdf,
    move_pages,
    parse_page_ranges,
    rotate_pages,
    split_document,
)
from tests.conftest import make_png, make_sample_pdf

LETTERS = "ABCDEFGHIJ"


def make_lettered(n: int = 5, title: str = "Rapport.pdf") -> PdfDocument:
    """Document whose page i shows the letter LETTERS[i] (easy to track)."""
    doc = pymupdf.open()
    for i in range(n):
        page = doc.new_page(width=300 + i, height=400)
        page.insert_text((50, 50), f"Side {LETTERS[i]}", fontname="helv", fontsize=20)
    data = doc.tobytes()
    doc.close()
    return PdfDocument.from_bytes(data, title=title)


def letters(doc) -> str:
    if isinstance(doc, PdfDocument):
        doc = doc.doc
    elif isinstance(doc, bytes):
        doc = pymupdf.open("pdf", doc)
    out = ""
    for page in doc:
        text = page.get_text().strip()
        out += text[-1] if text.startswith("Side ") else "_"
    return out


# -- parse_page_ranges ---------------------------------------------------------------
@pytest.mark.parametrize(
    "text, expected",
    [
        ("1", [0]),
        ("1-3, 5, 8-", [0, 1, 2, 4, 7, 8, 9]),
        ("  2 -4 ,7 ", [1, 2, 3, 6]),
        ("1–3", [0, 1, 2]),          # en dash
        ("1 — 2", [0, 1]),           # em dash
        ("-3", [0, 1, 2]),           # open start
        ("9-", [8, 9]),
        ("5-3", [4, 3, 2]),          # descending range
        ("3, 1, 3, 2", [2, 0, 1]),   # typed order, duplicates dropped
        ("1;2", [0, 1]),
        ("1,,2,", [0, 1]),
        ("10", [9]),
    ],
)
def test_parse_page_ranges(text, expected):
    assert parse_page_ranges(text, 10) == expected


@pytest.mark.parametrize("text", ["", "  ", ",", "abc", "1-2-3", "-", "1.5", "2 3", "a-3"])
def test_parse_page_ranges_syntax_errors(text):
    with pytest.raises(ValueError):
        parse_page_ranges(text, 10)


def test_parse_page_ranges_out_of_range_messages():
    with pytest.raises(ValueError, match="Side 11 findes ikke – dokumentet har 10 sider"):
        parse_page_ranges("1-11", 10)
    with pytest.raises(ValueError, match="kun 1 side"):
        parse_page_ranges("2", 1)
    with pytest.raises(ValueError, match="starter ved 1"):
        parse_page_ranges("0-2", 10)
    with pytest.raises(ValueError, match="Ugyldigt sideinterval"):
        parse_page_ranges("x", 10)


# -- rotate ----------------------------------------------------------------------------
def test_rotate_pages_relative_and_undo(sample_pdf):
    events = []
    sample_pdf.listen(events.append)
    rotate_pages(sample_pdf, [0, 2])
    assert [sample_pdf.page(i).rotation for i in range(3)] == [90, 0, 180]
    assert sample_pdf.history.undo_label == "Rotér sider"
    assert events[-1].kind == STRUCTURE and events[-1].pages == (0, 2)
    rotate_pages(sample_pdf, [0], -90)
    assert sample_pdf.page(0).rotation == 0
    assert sample_pdf.history.undo_label == "Rotér side"
    rotate_pages(sample_pdf, [1], 270)
    assert sample_pdf.page(1).rotation == 270
    sample_pdf.undo()
    sample_pdf.undo()
    sample_pdf.undo()
    assert [sample_pdf.page(i).rotation for i in range(3)] == [0, 0, 90]


def test_rotate_validation_and_noops(sample_pdf):
    with pytest.raises(ValueError):
        rotate_pages(sample_pdf, [0], 45)
    with pytest.raises(ValueError):
        rotate_pages(sample_pdf, [7])
    rotate_pages(sample_pdf, [0], 360)
    rotate_pages(sample_pdf, [])
    assert not sample_pdf.history.can_undo


# -- delete ------------------------------------------------------------------------------
def test_delete_pages():
    pdf = make_lettered(5)
    delete_pages(pdf, [3, 1, 1])
    assert letters(pdf) == "ACE"
    assert pdf.history.undo_label == "Slet sider"
    pdf.undo()
    assert letters(pdf) == "ABCDE"
    delete_pages(pdf, [4])
    assert pdf.history.undo_label == "Slet side"


def test_delete_all_pages_refused():
    pdf = make_lettered(3)
    with pytest.raises(ValueError, match="mindst én side"):
        delete_pages(pdf, [0, 1, 2])
    assert pdf.page_count == 3
    assert not pdf.history.can_undo
    with pytest.raises(ValueError):
        delete_pages(pdf, [3])


# -- move ----------------------------------------------------------------------------------
@pytest.mark.parametrize(
    "indices, to, expected, new",
    [
        ([0], 2, "BACDE", [1]),          # forward: lands before original page C
        ([0], 5, "BCDEA", [4]),          # to the end
        ([4], 0, "EABCD", [0]),          # backward to the start
        ([3], 1, "ADBCE", [1]),
        ([0, 2], 4, "BDACE", [2, 3]),    # non-contiguous forward
        ([3, 4], 0, "DEABC", [0, 1]),    # contiguous backward
        ([1, 4], 3, "ACBED", [2, 3]),    # one page before, one after the target
    ],
)
def test_move_pages(indices, to, expected, new):
    pdf = make_lettered(5)
    assert move_pages(pdf, indices, to) == new
    assert letters(pdf) == expected
    assert pdf.history.undo_label in ("Flyt side", "Flyt sider")
    pdf.undo()
    assert letters(pdf) == "ABCDE"


def test_move_non_contiguous_both_directions():
    pdf = make_lettered(6)
    # B and E to the gap before D: A C | B E | D F
    assert move_pages(pdf, [4, 1], 3) == [2, 3]
    assert letters(pdf) == "ACBEDF"
    pdf.undo()
    # A, C and F to the gap before E (mix of before/after the target)
    assert move_pages(pdf, [0, 2, 5], 4) == [2, 3, 4]
    assert letters(pdf) == "BDACFE"
    pdf.undo()
    # A and E to the very end
    assert move_pages(pdf, [0, 4], 6) == [4, 5]
    assert letters(pdf) == "BCDFAE"


def test_move_onto_itself_is_noop():
    pdf = make_lettered(5)
    # target inside / adjacent to the selection: block stays where it is
    assert move_pages(pdf, [1, 2], 2) == [1, 2]
    assert move_pages(pdf, [1, 2], 1) == [1, 2]
    assert move_pages(pdf, [1, 2], 3) == [1, 2]
    assert letters(pdf) == "ABCDE"
    assert not pdf.history.can_undo
    assert move_pages(pdf, [], 2) == []


def test_move_target_inside_selection_gathers_pages():
    pdf = make_lettered(5)
    # A, C, E with target C: rest before C is just B -> B A C E D
    assert move_pages(pdf, [0, 2, 4], 2) == [1, 2, 3]
    assert letters(pdf) == "BACED"


def test_move_validation():
    pdf = make_lettered(3)
    with pytest.raises(ValueError):
        move_pages(pdf, [0], 4)
    with pytest.raises(ValueError):
        move_pages(pdf, [5], 0)


def test_move_keeps_annotations_and_rotation(sample_pdf):
    with sample_pdf.edit("note"):
        page = sample_pdf.page(0)
        page.add_text_annot((100, 100), "hej")
    move_pages(sample_pdf, [0], 3)
    assert [sample_pdf.page(i).rotation for i in range(3)] == [0, 90, 0]
    assert [a.info["content"] for a in sample_pdf.page(2).annots()] == ["hej"]


# -- duplicate ------------------------------------------------------------------------------
def test_duplicate_pages():
    pdf = make_lettered(4)
    assert duplicate_pages(pdf, [2, 0]) == [1, 4]
    assert letters(pdf) == "AABCCD"
    assert pdf.history.undo_label == "Duplikér sider"
    pdf.undo()
    assert duplicate_pages(pdf, [3]) == [4]  # last page
    assert letters(pdf) == "ABCDD"


def test_duplicate_is_independent(sample_pdf):
    duplicate_pages(sample_pdf, [2])
    assert sample_pdf.page(3).rotation == 90
    with sample_pdf.edit("note"):
        page = sample_pdf.page(3)
        page.add_text_annot((50, 50), "kun kopien")
    assert len(list(sample_pdf.page(2).annots())) == 0
    assert len(list(sample_pdf.page(3).annots())) == 1


# -- insert blank -------------------------------------------------------------------------------
def test_insert_blank_page_neighbour_size(sample_pdf):
    # after the rotated (landscape as displayed) page -> landscape blank page
    assert insert_blank_page(sample_pdf, 3) == 3
    assert sample_pdf.page(3).rect.width == 842 and sample_pdf.page(3).rect.height == 595
    assert sample_pdf.history.undo_label == "Indsæt tom side"
    # at the start: size of the following page
    assert insert_blank_page(sample_pdf, 0) == 0
    assert (sample_pdf.page(0).rect.width, sample_pdf.page(0).rect.height) == (595, 842)
    assert sample_pdf.page(0).get_text() == ""
    assert insert_blank_page(sample_pdf, 1, 200, 100) == 1
    assert sample_pdf.page(1).rect == pymupdf.Rect(0, 0, 200, 100)
    assert sample_pdf.page_count == 6
    with pytest.raises(ValueError):
        insert_blank_page(sample_pdf, 99)
    with pytest.raises(ValueError):
        insert_blank_page(sample_pdf, 0, 0, 100)


# -- insert pdf / image -------------------------------------------------------------------------
def test_insert_pdf_from_bytes_with_subset():
    pdf = make_lettered(3)
    other = make_lettered(5)
    new = insert_pdf(pdf, other.to_bytes(), 1, page_indices=[4, 3])
    assert new == [1, 2]
    assert letters(pdf) == "AEDBC"
    assert pdf.history.undo_label == "Indsæt sider"
    pdf.undo()
    assert insert_pdf(pdf, other.to_bytes(), 3) == [3, 4, 5, 6, 7]
    assert letters(pdf) == "ABCABCDE"


def test_insert_pdf_from_path(sample_pdf, sample_pdf_path):
    new = insert_pdf(sample_pdf, sample_pdf_path, 0)
    assert new == [0, 1, 2]
    assert sample_pdf.page_count == 6
    assert sample_pdf.page(2).rotation == 90


def test_insert_image(sample_pdf, png_path):
    new = insert_pdf(sample_pdf, png_path, 1)
    assert new == [1]
    page = sample_pdf.page(1)
    # 320x200 px at 96 dpi
    assert (page.rect.width, page.rect.height) == (240, 150)
    assert len(page.get_images()) == 1
    assert insert_pdf(sample_pdf, make_png(10, 10), 0) == [0]


def test_insert_encrypted_pdf(tmp_path):
    doc = pymupdf.open("pdf", make_sample_pdf())
    path = str(tmp_path / "locked.pdf")
    doc.save(path, encryption=pymupdf.PDF_ENCRYPT_AES_256, owner_pw="o", user_pw="hemmelig")
    pdf = make_lettered(2)
    with pytest.raises(PasswordRequired):
        insert_pdf(pdf, path, 0)
    with pytest.raises(InvalidPassword):
        insert_pdf(pdf, path, 0, password="forkert")
    assert insert_pdf(pdf, path, 2, password="hemmelig") == [2, 3, 4]
    assert pdf.page_count == 5
    assert not pdf.doc.is_encrypted


def test_insert_corrupt_source(tmp_path):
    path = tmp_path / "broken.pdf"
    path.write_bytes(b"%PDF-1.7 this is not really a pdf")
    pdf = make_lettered(2)
    with pytest.raises(DocumentError, match="broken.pdf"):
        insert_pdf(pdf, str(path), 0)
    assert pdf.page_count == 2 and not pdf.history.can_undo


# -- extract / split -----------------------------------------------------------------------------
def test_extract_pages_leaves_document_untouched():
    pdf = make_lettered(5)
    data = extract_pages(pdf, [3, 0, 1])
    assert letters(data) == "DAB"
    assert letters(pdf) == "ABCDE"
    assert not pdf.history.can_undo
    with pytest.raises(ValueError):
        extract_pages(pdf, [])


def test_extract_keeps_rotation_and_annots(sample_pdf):
    with sample_pdf.edit("note"):
        page = sample_pdf.page(2)
        page.add_text_annot((50, 50), "note")
    out = pymupdf.open("pdf", extract_pages(sample_pdf, [2]))
    assert out.page_count == 1
    assert out[0].rotation == 90
    assert len(list(out[0].annots())) == 1


def test_split_every_n():
    pdf = make_lettered(5, title="Rapport.pdf")
    parts = split_document(pdf, every_n=2)
    assert [name for name, _ in parts] == ["Rapport_del1.pdf", "Rapport_del2.pdf", "Rapport_del3.pdf"]
    assert [letters(data) for _, data in parts] == ["AB", "CD", "E"]
    assert pdf.page_count == 5 and not pdf.history.can_undo


def test_split_default_is_one_page_per_file_and_padded_names():
    pdf = make_lettered(10, title="x.pdf")
    parts = split_document(pdf)
    assert len(parts) == 10
    assert parts[0][0] == "x_del01.pdf" and parts[-1][0] == "x_del10.pdf"
    assert letters(parts[9][1]) == "J"


def test_split_ranges_string_and_lists():
    pdf = make_lettered(6, title="Bilag.PDF")
    parts = split_document(pdf, ranges="1-2, 5-, 3")
    assert [letters(d) for _, d in parts] == ["AB", "EF", "C"]
    assert parts[0][0] == "Bilag_del1.pdf"
    parts = split_document(pdf, ranges=[[5, 0], "2-3"])
    assert [letters(d) for _, d in parts] == ["FA", "BC"]


def test_split_validation():
    pdf = make_lettered(3)
    with pytest.raises(ValueError):
        split_document(pdf, every_n=0)
    with pytest.raises(ValueError):
        split_document(pdf, every_n=1, ranges="1")
    with pytest.raises(ValueError):
        split_document(pdf, ranges="4")
    with pytest.raises(ValueError):
        split_document(pdf, ranges=" , ")


def test_split_name_from_path(sample_pdf, tmp_path):
    path = str(tmp_path / "Årsregnskab 2026.pdf")
    sample_pdf.save(path)
    parts = split_document(sample_pdf, every_n=3)
    assert parts[0][0] == "Årsregnskab 2026_del1.pdf"
    assert os.path.splitext(parts[0][0])[1] == ".pdf"
