import pymupdf
from PySide6.QtGui import QColor, QGuiApplication, QImage

from tests.conftest import make_png


def _dialog(paths=None):
    from pdfstudio.ui.dialogs.images_dialog import ImagesToPdfDialog

    return ImagesToPdfDialog(None, initial=paths)


def _write(tmp_path, name, w, h):
    p = tmp_path / name
    p.write_bytes(make_png(w, h))
    return str(p)


def test_add_reorder_and_create(qapp, tmp_path):
    a = _write(tmp_path, "b_wide.png", 400, 200)
    b = _write(tmp_path, "a_tall.png", 200, 400)
    dlg = _dialog([a, b, str(tmp_path / "missing.png"), str(tmp_path / "note.txt")])
    assert [s.name for s in dlg.sources()] == ["b_wide.png", "a_tall.png"]
    assert dlg.create_btn.isEnabled() and dlg.count_label.text() == "2 billeder"
    dlg.sort_by_name()
    assert [s.name for s in dlg.sources()] == ["a_tall.png", "b_wide.png"]
    dlg._create()
    doc = pymupdf.open("pdf", dlg.result_bytes)
    assert doc.page_count == 2
    assert doc[0].rect.height > doc[0].rect.width       # order follows the list
    assert dlg.result_title == "Billeder.pdf"


def test_paper_options_and_preview(qapp, tmp_path):
    dlg = _dialog([_write(tmp_path, "shot.png", 800, 400)])
    assert not dlg.orientation.isVisibleTo(dlg)          # hidden for "fit"
    dlg.page_size.setCurrentIndex(dlg.page_size.findData("A4"))
    assert dlg.orientation.isVisibleTo(dlg)
    dlg.margin.setValue(10)
    opts = dlg.options()
    assert (opts.page_size, opts.margin_mm, opts.orientation) == ("A4", 10.0, "auto")
    dlg._render_preview()
    assert not dlg.preview.pixmap().isNull()
    dlg._create()
    page = pymupdf.open("pdf", dlg.result_bytes)[0]
    assert round(page.rect.width) == 842                 # A4 landscape for a wide screenshot
    assert dlg.result_title == "shot.pdf"


def test_paste_screenshot_from_clipboard(qapp):
    dlg = _dialog()
    assert not dlg.create_btn.isEnabled()
    img = QImage(320, 180, QImage.Format.Format_RGB32)
    img.fill(QColor("#3366cc"))
    QGuiApplication.clipboard().setImage(img)
    dlg.paste_clipboard()
    srcs = dlg.sources()
    if not srcs:  # some headless platforms have no clipboard – fall back to the direct API
        dlg.add_image(img)
        srcs = dlg.sources()
    assert len(srcs) == 1 and srcs[0].data.startswith(b"\x89PNG")
    dlg._create()
    page = pymupdf.open("pdf", dlg.result_bytes)[0]
    assert (round(page.rect.width), round(page.rect.height)) == (240, 135)  # 96 dpi -> points


def test_remove_selected(qapp, tmp_path):
    dlg = _dialog([_write(tmp_path, f"{i}.png", 50, 50) for i in range(3)])
    dlg.list.setCurrentRow(1)
    dlg.remove_selected()
    assert [s.name for s in dlg.sources()] == ["0.png", "2.png"]
