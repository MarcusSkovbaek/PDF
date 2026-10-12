"""UI flow of "Tilføj tekst" and "Tilføj billede"."""

import pytest
from PySide6.QtCore import QEvent, Qt
from PySide6.QtGui import QColor, QImage, QKeyEvent

from pdfstudio.core import text_edit as te
from tests.conftest import make_png
from tests.test_annotate_tools import drag, ev
from tests.test_ui_shell import win  # noqa: F401  (fixture)


@pytest.fixture
def ws(win, qapp, sample_pdf_path):  # noqa: F811
    win.open_path(sample_pdf_path)
    w = win.current_workspace()
    w.set_mode("edit")
    qapp.processEvents()
    return w


def _ctrl_enter(editor):
    editor.keyPressEvent(QKeyEvent(QEvent.Type.KeyPress, Qt.Key.Key_Return, Qt.KeyboardModifier.ControlModifier))


def test_click_adds_text_in_document_style(ws):
    ws.set_tool("edit.add_text")
    tool = ws.tool
    tool.mouse_press(ev(ws.view, 0, 100, 500))
    tool.mouse_release(ev(ws.view, 0, 100, 500, buttons=Qt.MouseButton.NoButton))
    assert tool.editor is not None and tool.editor.toPlainText() == ""
    tool.editor.setPlainText("Ny tekst med æøå")
    _ctrl_enter(tool.editor)
    new = next(b for b in te.text_blocks(ws.pdf.doc, 0) if b.text.startswith("Ny tekst"))
    assert new.text == "Ny tekst med æøå"
    # defaults to the document's dominant body text style (headings ignored)
    from pdfstudio.core.insert import document_text_style

    body = document_text_style(ws.pdf.doc)
    assert abs(new.style.size - body.size) < 0.2 and new.style.serif == body.serif
    assert abs(new.lines[0].origin[0] - 100) < 1.0
    assert ws.pdf.history.undo_label


def test_drag_sets_wrap_width_and_empty_text_adds_nothing(ws):
    ws.set_tool("edit.add_text")
    tool = ws.tool
    drag(tool, ws.view, 0, (80, 600), (200, 640))
    assert abs(tool.block.bbox.width - 120) < 1
    tool.commit()                                    # nothing typed
    assert not ws.pdf.history.can_undo
    drag(tool, ws.view, 0, (80, 600), (200, 640))
    tool.editor.setPlainText("Et ret langt afsnit der helt sikkert skal brydes om på flere linjer")
    tool.commit()
    new = next(b for b in te.text_blocks(ws.pdf.doc, 0) if b.text.startswith("Et ret"))
    assert len(new.lines) >= 2 and new.bbox.x1 <= 201


def test_click_outside_commits(ws):
    ws.set_tool("edit.add_text")
    tool = ws.tool
    tool.mouse_press(ev(ws.view, 0, 100, 500))
    tool.mouse_release(ev(ws.view, 0, 100, 500, buttons=Qt.MouseButton.NoButton))
    tool.editor.setPlainText("Gemmes ved klik")
    tool.mouse_press(ev(ws.view, 0, 300, 700))
    assert tool.editor is None
    assert any(b.text == "Gemmes ved klik" for b in te.text_blocks(ws.pdf.doc, 0))


def test_add_image_click_and_drag(ws, tmp_path, monkeypatch):
    from pdfstudio.ui.viewer.tools import add_image

    path = tmp_path / "logo.png"
    path.write_bytes(make_png(400, 200))
    monkeypatch.setattr(add_image, "_AUTO_FILE", str(path))
    ws.set_tool("edit.add_image")
    tool = ws.tool
    before = len(ws.pdf.page(0).get_images())
    tool.mouse_press(ev(ws.view, 0, 300, 400))          # click: file dialog (auto), natural size
    tool.mouse_release(ev(ws.view, 0, 300, 400, buttons=Qt.MouseButton.NoButton))
    infos = ws.pdf.page(0).get_image_info()
    assert len(ws.pdf.page(0).get_images()) == before + 1
    bbox = infos[-1]["bbox"]
    assert abs((bbox[2] - bbox[0]) - 300) < 1 and abs((bbox[3] - bbox[1]) - 150) < 1   # 400x200 px @96 dpi
    drag(tool, ws.view, 0, (50, 600), (150, 700))      # drag: fitted into the frame, aspect kept
    bbox = ws.pdf.page(0).get_image_info()[-1]["bbox"]
    assert abs((bbox[2] - bbox[0]) - 100) < 1 and abs((bbox[3] - bbox[1]) - 50) < 1


def test_add_image_from_clipboard_image(ws):
    ws.set_tool("edit.add_image")
    tool = ws.tool
    img = QImage(160, 90, QImage.Format.Format_RGB32)
    img.fill(QColor("#22aa66"))
    tool.set_image(img)
    assert tool.pending.startswith(b"\x89PNG")
    panel = tool.properties_widget()
    assert panel is not None
    tool.mouse_press(ev(ws.view, 0, 300, 300))
    tool.mouse_release(ev(ws.view, 0, 300, 300, buttons=Qt.MouseButton.NoButton))
    bbox = ws.pdf.page(0).get_image_info()[-1]["bbox"]
    assert abs((bbox[2] - bbox[0]) - 120) < 1
