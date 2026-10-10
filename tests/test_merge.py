import os

import pymupdf
import pytest

from pdfstudio.core import InvalidPassword, PasswordRequired
from pdfstudio.core.document import DocumentError
from pdfstudio.core.merge import MergeSource, merge_files, open_source
from tests.conftest import make_png, make_sample_pdf


@pytest.fixture
def files(tmp_path):
    """Two PDFs (one with an outline) and a PNG on disk."""
    doc = pymupdf.open("pdf", make_sample_pdf())
    doc.set_toc([[1, "Indledning", 1], [2, "Detaljer", 2], [1, "Bilag", 3]])
    rapport = str(tmp_path / "Rapport.pdf")
    doc.save(rapport)
    doc.close()

    doc = pymupdf.open()
    for i in range(2):
        doc.new_page(width=300, height=300).insert_text((50, 50), f"Faktura {i + 1}")
    faktura = str(tmp_path / "Faktura.pdf")
    doc.save(faktura)
    doc.close()

    png = str(tmp_path / "Skærmbillede.png")
    with open(png, "wb") as fh:
        fh.write(make_png(400, 300))
    return rapport, faktura, png


def test_merge_pdfs_and_image_with_bookmarks(files):
    rapport, faktura, png = files
    data = merge_files([MergeSource(rapport), MergeSource(faktura), MergeSource(png)])
    out = pymupdf.open("pdf", data)
    assert out.page_count == 3 + 2 + 1
    assert out[2].rotation == 90  # rotation of the source page kept
    assert "Faktura 1" in out[3].get_text()
    # image page at 96 dpi
    assert (out[5].rect.width, out[5].rect.height) == (300, 225)
    assert out.get_toc() == [
        [1, "Rapport", 1],
        [2, "Indledning", 1],
        [3, "Detaljer", 2],
        [2, "Bilag", 3],
        [1, "Faktura", 4],
        [1, "Skærmbillede", 6],
    ]


def test_merge_without_file_bookmarks_keeps_outlines(files):
    rapport, faktura, _ = files
    out = pymupdf.open("pdf", merge_files([MergeSource(faktura), MergeSource(rapport)], add_bookmarks=False))
    assert out.get_toc() == [[1, "Indledning", 3], [2, "Detaljer", 4], [1, "Bilag", 5]]


def test_merge_page_subsets_and_order(files):
    rapport, faktura, _ = files
    data = merge_files([MergeSource(faktura, page_indices=[1, 0]), MergeSource(rapport, page_indices=[2])])
    out = pymupdf.open("pdf", data)
    assert out.page_count == 3
    assert "Faktura 2" in out[0].get_text()
    assert "Faktura 1" in out[1].get_text()
    assert "Roteret" in out[2].get_text()
    toc = out.get_toc()
    # only outline items pointing to kept pages survive, re-numbered
    assert toc == [[1, "Faktura", 1], [1, "Rapport", 3], [2, "Bilag", 3]]


def test_merge_empty_subset_is_skipped(files):
    rapport, faktura, _ = files
    out = pymupdf.open("pdf", merge_files([MergeSource(faktura, page_indices=[]), MergeSource(rapport)]))
    assert out.page_count == 3
    assert out.get_toc()[0] == [1, "Rapport", 1]


def test_merge_bad_page_index(files):
    _, faktura, _ = files
    with pytest.raises(ValueError):
        merge_files([MergeSource(faktura, page_indices=[5])])


def test_merge_encrypted(files, tmp_path):
    rapport, _, _ = files
    locked = str(tmp_path / "Hemmelig.pdf")
    doc = pymupdf.open(rapport)
    doc.save(locked, encryption=pymupdf.PDF_ENCRYPT_AES_256, owner_pw="ejer", user_pw="kode")
    doc.close()
    with pytest.raises(PasswordRequired) as info:
        merge_files([MergeSource(locked)])
    assert "Hemmelig.pdf" in str(info.value)
    with pytest.raises(InvalidPassword):
        merge_files([MergeSource(locked, password="nej")])
    out = pymupdf.open("pdf", merge_files([MergeSource(locked, password="kode")]))
    assert out.page_count == 3
    assert not out.needs_pass
    assert out.get_toc()[1] == [2, "Indledning", 1]


def test_merge_corrupt_names_file(files, tmp_path):
    rapport, _, _ = files
    broken = tmp_path / "Ødelagt.pdf"
    broken.write_bytes(b"\x00\x01garbage" * 50)
    with pytest.raises(DocumentError, match="Ødelagt.pdf"):
        merge_files([MergeSource(rapport), MergeSource(str(broken))])
    broken_png = tmp_path / "foto.png"
    broken_png.write_bytes(b"\x89PNG\r\n\x1a\nnot really")
    with pytest.raises(DocumentError, match="foto.png"):
        merge_files([MergeSource(str(broken_png))])
    empty = tmp_path / "tom.pdf"
    empty.write_bytes(b"")
    with pytest.raises(DocumentError, match="tom.pdf"):
        merge_files([MergeSource(str(empty))])
    with pytest.raises(DocumentError, match="findes-ikke.pdf"):
        merge_files([MergeSource(str(tmp_path / "findes-ikke.pdf"))])


def test_merge_requires_sources():
    with pytest.raises(ValueError):
        merge_files([])


def test_merge_does_not_keep_files_open(files):
    rapport, faktura, png = files
    merge_files([MergeSource(rapport), MergeSource(faktura), MergeSource(png)])
    # Deleting/renaming would fail on Windows if a handle was still open; on
    # every OS we can at least check the files are freely replaceable.
    for path in files:
        os.replace(path, path + ".moved")
        assert os.path.exists(path + ".moved")
    fd_dir = "/proc/self/fd"
    if os.path.isdir(fd_dir):
        targets = []
        for fd in os.listdir(fd_dir):
            try:
                targets.append(os.readlink(os.path.join(fd_dir, fd)))
            except OSError:
                pass
        assert not any(t.startswith(os.path.dirname(rapport)) for t in targets)


def test_merge_keeps_links_and_annotations(tmp_path):
    doc = pymupdf.open()
    for _ in range(2):
        doc.new_page()
    doc[0].insert_link({"kind": pymupdf.LINK_GOTO, "page": 1, "from": pymupdf.Rect(10, 10, 100, 30), "to": pymupdf.Point(0, 0)})
    page = doc[1]
    page.add_text_annot((50, 50), "kommentar")
    path = str(tmp_path / "links.pdf")
    doc.save(path)
    doc.close()
    out = pymupdf.open("pdf", merge_files([MergeSource(path), MergeSource(path)]))
    assert out.page_count == 4
    assert out[2].get_links()[0]["page"] == 3
    assert [a.info["content"] for a in out[3].annots()] == ["kommentar"]


def test_open_source_bytes(files):
    with open(files[0], "rb") as fh:
        doc = open_source(fh.read())
    assert doc.page_count == 3
    doc.close()
    doc = open_source(make_png(8, 8))
    assert doc.page_count == 1
    doc.close()
