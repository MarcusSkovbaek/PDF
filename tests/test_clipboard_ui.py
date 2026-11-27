"""UI for PDF pages from the clipboard (docs/kravspec-udklipsholder.md, U1–U17):
reading the system clipboard, "Ny PDF fra udklipsholder" (Ctrl+Shift+N),
"Indsæt fra udklipsholder" (Ctrl+V / context menu in the Sider grid and the
thumbnails), "Brug teksten i stedet" and Indstillinger → Udklipsholder.

UI-flow tests use small fakes for ``content_to_pdf`` / ``insert_content``;
the ``needs_core`` tests run the real core."""

from __future__ import annotations

import os
import re

import pymupdf
import pytest
from PySide6.QtCore import QMimeData, Qt, QUrl
from PySide6.QtGui import QColor, QGuiApplication, QImage, QKeySequence

from pdfstudio.core import STRUCTURE
from pdfstudio.core import clipboard_pdf as cp
from pdfstudio.i18n import tr
from tests.conftest import make_png
from tests.test_ui_shell import pump, win  # noqa: F401  (fixture)


def _core_ready() -> bool:
    try:
        cp.text_to_pdf("x")
    except NotImplementedError:
        return False
    except Exception:  # noqa: BLE001 - implemented (any other failure shows in the tests themselves)
        return True
    return True


needs_core = pytest.mark.skipif(not _core_ready(), reason="core/clipboard_pdf.py is not implemented yet")


# ------------------------------------------------------------------ helpers
@pytest.fixture
def clip(qapp):
    """The system clipboard, empty before and after the test."""
    from pdfstudio.ui.viewer.tools import _object_clipboard

    cb = QGuiApplication.clipboard()
    cb.clear()
    _object_clipboard.clear()
    yield cb
    cb.clear()
    _object_clipboard.clear()


def image(w=400, h=300, color=(80, 120, 200)) -> QImage:
    img = QImage(w, h, QImage.Format.Format_ARGB32)
    img.fill(QColor(*color))
    return img


def set_mime(cb, *, urls=(), html=None, text=None, data=None, img=None) -> None:
    md = QMimeData()
    if urls:
        md.setUrls([QUrl.fromLocalFile(u) for u in urls])
    if html is not None:
        md.setHtml(html)
    if text is not None:
        md.setText(text)
    for fmt, value in (data or {}).items():
        md.setData(fmt, value)
    if img is not None:
        md.setImageData(img)
    cb.setMimeData(md)


def one_page_pdf(w=300, h=200, text="") -> bytes:
    doc = pymupdf.open()
    page = doc.new_page(width=w, height=h)
    if text:
        page.insert_text((20, 40), text)
    data = doc.tobytes()
    doc.close()
    return data


class Toasts:
    """Records the window's toasts (text, kind, action_text, on_action)."""

    def __init__(self, win):  # noqa: F811
        self.items: list[tuple] = []
        orig = win.toast

        def toast(text, kind="info", **kw):
            self.items.append((text, kind, kw.get("action_text"), kw.get("on_action")))
            return orig(text, kind, **kw)

        win.toast = toast

    def last(self, kind: str | None = None):
        items = [t for t in self.items if kind is None or t[1] == kind]
        return items[-1] if items else None


@pytest.fixture
def toasts(win):  # noqa: F811
    return Toasts(win)


@pytest.fixture
def fake_core(monkeypatch):
    """Small fakes for the core functions: a new PDF of one page per kind,
    pages inserted as blank pages inside one pdf.edit."""
    calls: list[dict] = []

    def content_to_pdf(content, options=None, *, passwords=None):
        options = options or cp.ClipOptions()
        kind = cp.chosen_kind(content, options.prefer)
        if kind is None:
            raise ValueError("Udklipsholderen indeholder intet, der kan bruges")
        calls.append({"fn": "new", "kind": kind, "options": options, "passwords": passwords})
        size = (612, 792) if kind == "text" else (300, 225)
        alt = "text" if kind == "image" and "text" in content.kinds() and options.prefer is None else None
        return cp.ClipResult(one_page_pdf(*size, text=kind), kind, 1,
                             skipped=cp.usable_files(content.files)[1], alternative=alt)

    def insert_content(pdf, content, at, options=None, *, passwords=None):
        options = options or cp.ClipOptions(page_size="match")
        kind = cp.chosen_kind(content, options.prefer)
        if kind is None:
            raise ValueError("Udklipsholderen indeholder intet, der kan bruges")
        n = 2 if kind == "text" else 1
        calls.append({"fn": "insert", "kind": kind, "at": at, "options": options, "passwords": passwords})
        with pdf.edit("Indsæt fra udklipsholder", STRUCTURE):
            for i in range(n):
                pdf.doc.new_page(at + i, width=200 if kind == "text" else 400, height=300)
        alt = "text" if kind == "image" and "text" in content.kinds() and options.prefer is None else None
        return cp.ClipInsert(list(range(at, at + n)), kind, skipped=cp.usable_files(content.files)[1],
                             alternative=alt)

    monkeypatch.setattr(cp, "content_to_pdf", content_to_pdf)
    monkeypatch.setattr(cp, "insert_content", insert_content)
    return calls


@pytest.fixture
def doc_ws(win, qapp, sample_pdf_path):  # noqa: F811
    ws = win.open_path(sample_pdf_path)
    pump(qapp, 100)
    return ws


def page_widths(pdf) -> list[int]:
    return [round(pdf.page(i).rect.width) for i in range(pdf.page_count)]


# ------------------------------------------------------------- read_clipboard
def test_read_image_is_lossless_png(clip):
    from pdfstudio.ui.clipboard_source import read_clipboard

    clip.setImage(image(64, 48, (10, 200, 30)))
    content = read_clipboard()
    assert content.kinds() == ["image"]
    (png,) = content.images
    assert png.startswith(b"\x89PNG")
    pix = pymupdf.Pixmap(png)
    assert (pix.width, pix.height) == (64, 48)
    assert pix.pixel(10, 10)[:3] == (10, 200, 30)


def test_read_raw_png_bytes_are_kept(clip):
    from pdfstudio.ui.clipboard_source import read_clipboard

    raw = make_png(30, 20, (1, 2, 3))
    set_mime(clip, data={"image/png": raw})
    assert read_clipboard().images == [raw]


def test_read_windows_png_and_pdf_formats(clip):
    from pdfstudio.ui.clipboard_source import read_clipboard

    raw = make_png(30, 20)
    pdf = one_page_pdf()
    set_mime(clip, data={'application/x-qt-windows-mime;value="PNG"': raw,
                         'application/x-qt-windows-mime;value="Portable Document Format"': pdf})
    content = read_clipboard()
    assert content.images == [raw] and content.pdf == pdf


def test_read_files_pdf_html_and_text(clip, tmp_path):
    from pdfstudio.ui.clipboard_source import read_clipboard

    a = tmp_path / "a.pdf"
    a.write_bytes(one_page_pdf())
    b = tmp_path / "b.png"
    b.write_bytes(make_png())
    set_mime(clip, urls=[str(a), str(b)], html="<h1>Overskrift</h1>", data={"application/pdf": one_page_pdf()})
    content = read_clipboard()
    assert [os.path.basename(f) for f in content.files] == ["a.pdf", "b.png"]
    assert content.pdf.startswith(b"%PDF")
    assert "Overskrift" in content.html
    assert content.text is None             # the file list is not text
    assert content.kinds()[:3] == ["files", "pdf", "html"]

    clip.setText("Æblegrød – 25 €")
    content = read_clipboard()
    assert content.text == "Æblegrød – 25 €" and content.kinds() == ["text"]
    assert not content.files and content.pdf is None and not content.images


def test_read_ignores_web_links_and_bogus_pdf(clip):
    from pdfstudio.ui.clipboard_source import read_clipboard

    md = QMimeData()
    md.setUrls([QUrl("https://example.com/a.pdf")])
    md.setData("application/pdf", b"not a pdf")
    clip.setMimeData(md)
    content = read_clipboard()
    assert content.files == [] and content.pdf is None
    assert content.is_empty()


def test_read_empty(clip):
    from pdfstudio.ui.clipboard_source import clipboard_available, read_clipboard

    assert read_clipboard().is_empty()
    assert not clipboard_available()


def test_folio_object_clip_is_detected_while_newest(clip, sample_pdf):
    from pdfstudio.ui.clipboard_source import (
        clipboard_available,
        describe,
        read_clipboard,
    )
    from pdfstudio.ui.viewer.tools import _object_clipboard

    marker = object()
    _object_clipboard.put(marker, sample_pdf, 0, 1, image=image(20, 20), text="Faktaboks")
    content = read_clipboard()
    assert content.folio is marker
    assert cp.chosen_kind(content) == "folio"
    assert describe(content) == tr("clipboard.what.folio")
    assert clipboard_available()
    clip.setText("noget andet")             # copied something else since: the clip is stale
    assert read_clipboard().folio is None


# --------------------------------------------------------- clipboard_available
def test_clipboard_available_per_kind(clip, tmp_path):
    from pdfstudio.ui.clipboard_source import clipboard_available

    clip.setText("   \n  ")
    assert not clipboard_available()        # blank text
    clip.setText("hej")
    assert clipboard_available()
    clip.setImage(image())
    assert clipboard_available()
    doc = tmp_path / "notes.docx"
    doc.write_bytes(b"x")
    set_mime(clip, urls=[str(doc)])
    assert not clipboard_available()        # unsupported file type only
    set_mime(clip, urls=[str(tmp_path / "missing.pdf")])
    assert not clipboard_available()        # gone
    pdf = tmp_path / "a.pdf"
    pdf.write_bytes(one_page_pdf())
    set_mime(clip, urls=[str(doc), str(pdf)])
    assert clipboard_available()
    set_mime(clip, html="<p>formateret</p>")
    assert clipboard_available()
    set_mime(clip, data={"application/pdf": one_page_pdf()})
    assert clipboard_available()


def test_describe(clip, tmp_path):
    from pdfstudio.ui.clipboard_source import describe

    a, b = tmp_path / "a.pdf", tmp_path / "b.png"
    a.write_bytes(one_page_pdf())
    b.write_bytes(make_png())
    assert describe(cp.ClipContent(images=[b"x"])) == "et skærmbillede"
    assert describe(cp.ClipContent(images=[b"x", b"y"])) == "2 billeder"
    assert describe(cp.ClipContent(files=[str(a), str(b)])) == "2 filer"
    assert describe(cp.ClipContent(files=[str(a)])) == "1 fil"
    assert describe(cp.ClipContent(text="hej")) == "tekst"
    assert describe(cp.ClipContent(html="<b>x</b>")) == "formateret tekst"
    assert describe(cp.ClipContent(images=[b"x"], text="t"), "text") == "tekst"


def test_clipboard_title_and_options(qapp, tmp_path):
    from datetime import datetime

    from PySide6.QtCore import QSettings

    from pdfstudio.ui.clipboard_source import clip_options, clipboard_title

    assert clipboard_title(datetime(2026, 11, 26, 10, 30).astimezone()) == "Udklipsholder 2026-11-26 10.30.pdf"
    s = QSettings(str(tmp_path / "s.ini"), QSettings.Format.IniFormat)
    new, ins = clip_options(s, "new"), clip_options(s, "insert")
    assert (new.page_size, ins.page_size, new.margin_mm) == ("image", "match", 10.0)
    s.setValue("clipboard/new_page_size", "Letter")
    s.setValue("clipboard/insert_page_size", "bogus")
    s.setValue("clipboard/margin_mm", 99)
    opts = clip_options(s, "new", prefer="text")
    assert (opts.page_size, opts.margin_mm, opts.prefer) == ("Letter", 50.0, "text")
    assert clip_options(s, "insert").page_size == "match"


# ---------------------------------------------------------------- the actions
def test_actions_registered_and_placed(win):  # noqa: F811
    new = win.actions.get("file.new_from_clipboard")
    ins = win.actions.get("pages.insert_clipboard")
    assert new and ins and new.palette and ins.palette
    assert new.action.shortcut().toString() == "Ctrl+Shift+N"
    assert new.icon == "clipboard-text" and ins.icon == "clipboard-text"
    assert new.category == tr("shell.cat.file")
    file_menu = next(m for m in win.main_menu.findChildren(type(win.main_menu)) if m.title() == tr("shell.menu.file"))
    names = [a.objectName() for a in file_menu.actions() if a.objectName()]
    assert names[names.index("file.new") + 1] == "file.new_from_clipboard"
    pages_ids = [e.id for e in win.actions.for_menu("pages")]
    assert pages_ids[pages_ids.index("pages.insert_file") + 1] == "pages.insert_clipboard"
    # Ctrl+V stays free for the text editor / Markér objekt (no window shortcut takes it)
    paste = QKeySequence(QKeySequence.StandardKey.Paste)
    assert not any(paste in e.action.shortcuts() for e in win.actions.all())


def test_enabled_state_follows_the_clipboard(win, clip, qapp, sample_pdf_path):  # noqa: F811
    new = win.actions.action("file.new_from_clipboard")
    ins = win.actions.action("pages.insert_clipboard")
    assert not new.isEnabled() and not ins.isEnabled()
    assert new.toolTip() == tr("clipboard.empty_tip")
    clip.setText("hej")
    assert new.isEnabled() and not ins.isEnabled()      # no document open yet
    assert "Ctrl+Shift+N" in new.toolTip()
    win.open_path(sample_pdf_path)
    pump(qapp, 50)
    assert ins.isEnabled()
    clip.clear()
    assert not new.isEnabled() and not ins.isEnabled()
    assert ins.toolTip() == tr("clipboard.empty_tip")
    clip.setImage(image())
    assert new.isEnabled() and ins.isEnabled()


def test_ctrl_shift_n_opens_an_unsaved_tab(win, clip, qapp, fake_core, monkeypatch, tmp_path):  # noqa: F811
    from PySide6.QtTest import QTest
    from PySide6.QtWidgets import QFileDialog

    from pdfstudio.ui.app import set_last_dir

    clip.setImage(image())
    win.activateWindow()
    QTest.keyClick(win, Qt.Key.Key_N, Qt.KeyboardModifier.ControlModifier | Qt.KeyboardModifier.ShiftModifier)
    pump(qapp, 50)
    ws = win.current_workspace()
    assert ws is not None and fake_core[-1]["kind"] == "image"
    assert fake_core[-1]["options"].page_size == "image" and fake_core[-1]["options"].margin_mm == 10
    assert ws.pdf.path is None and ws.pdf.is_dirty           # nothing written anywhere
    assert re.fullmatch(r"Udklipsholder \d{4}-\d\d-\d\d \d\d\.\d\d\.pdf", ws.pdf.display_name)
    assert win.tabs.tabText(win.tabs.currentIndex()) == ws.pdf.display_name
    assert "Ny PDF lavet af et skærmbillede" in win.toasts.visible_messages()
    # Gem suggests that name in the last used folder
    set_last_dir(str(tmp_path))
    asked = []
    monkeypatch.setattr(QFileDialog, "getSaveFileName",
                        staticmethod(lambda *a, **k: (asked.append(a[2]) or "", "")))
    win.save()
    assert asked == [os.path.join(str(tmp_path), ws.pdf.display_name)]


def test_new_from_clipboard_uses_settings_and_reports(win, clip, toasts, fake_core, tmp_path):  # noqa: F811
    win.settings.setValue("clipboard/new_page_size", "A4")
    win.settings.setValue("clipboard/margin_mm", 5)
    good = tmp_path / "a.png"
    good.write_bytes(make_png())
    bad = tmp_path / "notes.docx"
    bad.write_bytes(b"x")
    set_mime(clip, urls=[str(good), str(bad)])
    ws = win.new_from_clipboard()
    assert ws is not None
    opts = fake_core[-1]["options"]
    assert (opts.page_size, opts.margin_mm) == ("A4", 5.0) and callable(fake_core[-1]["passwords"])
    assert toasts.items[0][0] == "Ny PDF lavet af 1 fil" and toasts.items[0][2] is None
    assert toasts.last("warning")[0] == "Sprunget over: notes.docx"


def test_new_from_clipboard_empty_or_unusable(win, clip, toasts, fake_core, monkeypatch):  # noqa: F811
    assert win.new_from_clipboard() is None
    assert toasts.last() == (tr("clipboard.empty"), "info", None, None)

    def refuse(*_a, **_k):
        raise ValueError("Intet at bruge")

    monkeypatch.setattr(cp, "content_to_pdf", refuse)
    clip.setText("hej")
    assert win.new_from_clipboard() is None
    assert toasts.last()[:2] == ("Intet at bruge", "info")
    assert win.current_workspace() is None


def test_home_card(win, clip, toasts, fake_core, qapp):  # noqa: F811
    home = win.home_view()
    assert list(home.cards)[-2:] == ["file.new_from_clipboard", "file.new"]
    widths = {c.width() for c in home.cards.values()}
    heights = {c.height() for c in home.cards.values()}
    assert max(widths) - min(widths) <= 1 and len(heights) == 1      # a calm, even row
    home.actionRequested.emit("file.new_from_clipboard")
    assert toasts.last() == (tr("clipboard.empty"), "info", None, None)
    assert win.current_workspace() is None
    clip.setText("hej")
    home.actionRequested.emit("file.new_from_clipboard")
    assert win.current_workspace() is not None and fake_core[-1]["kind"] == "text"


def test_use_text_instead_replaces_the_new_tab(win, clip, toasts, fake_core, qapp):  # noqa: F811
    set_mime(clip, img=image(), text="A1\tB1")
    ws = win.new_from_clipboard()
    _text, _kind, action_text, on_action = toasts.items[0]
    assert action_text == tr("clipboard.use_text") and callable(on_action)
    name = ws.pdf.display_name
    tabs = win.tabs.count()
    on_action()
    pump(qapp, 20)
    new = win.current_workspace()
    assert new is not ws and ws not in win.workspaces()
    assert fake_core[-1]["options"].prefer == "text"
    assert round(new.pdf.page(0).rect.width) == 612            # the text version
    assert new.pdf.display_name == name and new.pdf.is_dirty and new.pdf.path is None
    assert win.tabs.count() == tabs


def test_use_text_instead_only_while_unchanged(win, clip, toasts, fake_core, qapp):  # noqa: F811
    from pdfstudio.core import pages

    set_mime(clip, img=image(), text="A1\tB1")
    ws = win.new_from_clipboard()
    on_action = toasts.items[0][3]
    pages.rotate_pages(ws.pdf, [0], 90)                       # the user edited the new PDF
    n = len(fake_core)
    on_action()
    assert len(fake_core) == n and win.current_workspace() is ws
    assert toasts.last()[0] == tr("clipboard.stale")


def test_new_from_clipboard_without_text_offers_no_alternative(win, clip, toasts, fake_core):  # noqa: F811
    clip.setImage(image())
    win.new_from_clipboard()
    assert toasts.items[0][2] is None


# ------------------------------------------------------------ inserting pages
def test_ctrl_v_in_sider_grid_inserts_after_selection(win, doc_ws, clip, toasts, fake_core, qapp):  # noqa: F811
    from PySide6.QtTest import QTest

    win.settings.setValue("clipboard/insert_page_size", "image")
    doc_ws.set_mode("pages")
    pump(qapp, 50)
    view = doc_ws.organizer.view
    view.select_pages([1])
    view.setFocus()
    clip.setImage(image())
    QTest.keyClick(view, Qt.Key.Key_V, Qt.KeyboardModifier.ControlModifier)
    pump(qapp, 50)
    assert fake_core[-1]["fn"] == "insert" and fake_core[-1]["at"] == 2
    assert fake_core[-1]["options"].page_size == "image"
    assert doc_ws.pdf.page_count == 4 and page_widths(doc_ws.pdf)[2] == 400
    assert view.selected_pages() == [2]                       # the new page is selected
    text, kind, action_text, on_action = toasts.last()
    assert (text, kind, action_text) == ("1 side indsat fra udklipsholderen", "success", tr("shell.undo_short"))
    on_action()                                               # Fortryd: exactly the inserted page goes
    assert doc_ws.pdf.page_count == 3 and 400 not in page_widths(doc_ws.pdf)


def test_ctrl_v_in_the_thumbnail_strip(win, doc_ws, clip, fake_core, qapp):  # noqa: F811
    from PySide6.QtTest import QTest

    thumbs = doc_ws.thumbs
    thumbs.select_pages([0])
    thumbs.setFocus()
    clip.setText("Hej")
    QTest.keyClick(thumbs, Qt.Key.Key_V, Qt.KeyboardModifier.ControlModifier)
    pump(qapp, 50)
    assert fake_core[-1]["at"] == 1 and fake_core[-1]["options"].page_size == "match"
    assert doc_ws.pdf.page_count == 5                         # the fake sets text on 2 pages


def test_context_menu_before_and_after(win, doc_ws, clip, fake_core, qapp):  # noqa: F811
    doc_ws.set_mode("pages")
    pump(qapp, 50)
    view = doc_ws.organizer.view

    def menu_at(row):
        rect = view.visualRect(view.model().index(row))
        menu = view.build_context_menu(rect.center())
        acts = {a.objectName(): a for a in menu.actions() if a.objectName().startswith("clipboard.")}
        return menu, acts

    menu, acts = menu_at(1)
    assert set(acts) == {"clipboard.before", "clipboard.after"}
    assert [acts["clipboard.before"].text(), acts["clipboard.after"].text()] == [
        "Indsæt fra udklipsholder før", "Indsæt fra udklipsholder efter"]
    assert not any(a.isEnabled() for a in acts.values())     # empty clipboard
    menu.deleteLater()
    clip.setImage(image())
    menu, acts = menu_at(1)
    assert view.selected_pages() == [1]
    acts["clipboard.before"].trigger()
    assert fake_core[-1]["at"] == 1 and page_widths(doc_ws.pdf)[1] == 400
    menu.deleteLater()
    pump(qapp, 20)
    menu, acts = menu_at(3)
    acts["clipboard.after"].trigger()
    assert fake_core[-1]["at"] == 4 and doc_ws.pdf.page_count == 5
    menu.deleteLater()


def test_insert_action_uses_current_page_without_selection(win, doc_ws, clip, fake_core, qapp):  # noqa: F811
    doc_ws.view.go_to_page(0)
    pump(qapp, 30)
    clip.setImage(image())
    assert win.actions.trigger("pages.insert_clipboard")
    assert fake_core[-1]["at"] == 1
    assert doc_ws.pdf.history.can_undo and len(doc_ws.pdf.history._undo) == 1   # one undo step


def test_insert_use_text_instead(win, doc_ws, clip, toasts, fake_core, qapp):  # noqa: F811
    set_mime(clip, img=image(), text="A1\tB1")
    doc_ws.insert_from_clipboard(at=1)
    assert doc_ws.pdf.page_count == 4
    _text, _kind, action_text, on_action = toasts.last("success")
    assert action_text == tr("clipboard.use_text")
    on_action()
    assert fake_core[-1]["options"].prefer == "text" and fake_core[-1]["at"] == 1
    assert doc_ws.pdf.page_count == 5 and page_widths(doc_ws.pdf)[1:3] == [200, 200]   # image page replaced
    assert len(doc_ws.pdf.history._undo) == 1
    assert toasts.last("success")[2] == tr("shell.undo_short")
    toasts.last("success")[3]()
    assert doc_ws.pdf.page_count == 3


def test_insert_toast_actions_are_stale_after_a_change(win, doc_ws, clip, toasts, fake_core, qapp):  # noqa: F811
    from pdfstudio.core import pages

    set_mime(clip, img=image(), text="A1\tB1")
    doc_ws.insert_from_clipboard(at=0)
    use_text = toasts.last("success")[3]
    pages.rotate_pages(doc_ws.pdf, [2], 90)
    use_text()
    assert toasts.last()[0] == tr("clipboard.stale") and doc_ws.pdf.page_count == 4

    clip.setImage(image())
    doc_ws.insert_from_clipboard(at=0)
    undo = toasts.last("success")[3]
    pages.rotate_pages(doc_ws.pdf, [2], 90)
    undo()                                                    # would undo the rotation: ignored
    assert doc_ws.pdf.page_count == 5


def test_insert_errors_are_reported(win, doc_ws, clip, toasts, fake_core, monkeypatch):  # noqa: F811
    assert doc_ws.insert_from_clipboard() == []
    assert toasts.last()[:2] == (tr("clipboard.empty"), "info")

    def broken(*_a, **_k):
        raise RuntimeError("defekt")

    monkeypatch.setattr(cp, "insert_content", broken)
    clip.setText("x")
    assert doc_ws.insert_from_clipboard() == []
    assert toasts.last()[1] == "error" and "defekt" in toasts.last()[0]
    assert doc_ws.pdf.page_count == 3


def test_pages_panel_has_the_button(win, doc_ws, clip, qapp):  # noqa: F811
    doc_ws.set_mode("pages")
    pump(qapp, 50)
    panel = doc_ws.inspector._pages_panel
    btn = next(b for b, aid in panel._buttons if aid == "pages.insert_clipboard")
    ids = [aid for _b, aid in panel._buttons]
    assert ids.index("pages.insert_clipboard") == ids.index("pages.insert_file") + 1
    assert not btn.isEnabled()
    clip.setText("hej")
    assert btn.isEnabled()


def test_password_prompt_marks_repeat_as_wrong(qapp, monkeypatch):
    from pdfstudio.ui import clipboard_source
    from pdfstudio.ui.dialogs import password

    seen = []
    monkeypatch.setattr(password, "ask_password", lambda parent, name, wrong=False: seen.append((name, wrong)) or "pw")
    ask = clipboard_source.password_prompt(None)
    assert ask("a.pdf") == "pw" and ask("a.pdf") == "pw" and ask("b.pdf") == "pw"
    assert seen == [("a.pdf", False), ("a.pdf", True), ("b.pdf", False)]


# ------------------------------------------------------------------- settings
def test_settings_section_persists(win, qapp):  # noqa: F811
    from pdfstudio.ui.dialogs.settings_dialog import SettingsDialog

    dlg = SettingsDialog(win)
    assert dlg.clip_new.current_key() == "image" and dlg.clip_insert.current_key() == "match"
    assert dlg.clip_margin.value() == 10 and dlg.clip_margin.isEnabled()
    assert dlg.clip_new.keys() == ["image", "A4", "Letter"] and dlg.clip_insert.keys() == ["match", "image"]
    dlg.clip_insert.set_current_key("image", animate=False, emit=True)
    assert not dlg.clip_margin.isEnabled()                    # no paper size anywhere: no margin
    dlg.clip_new.set_current_key("Letter", animate=False, emit=True)
    assert dlg.clip_margin.isEnabled()
    dlg.clip_margin.setValue(25)
    dlg.apply()
    from pdfstudio.ui.app import settings

    fresh = settings()
    assert fresh.value("clipboard/new_page_size") == "Letter"
    assert fresh.value("clipboard/insert_page_size") == "image"
    assert int(fresh.value("clipboard/margin_mm")) == 25
    again = SettingsDialog(win)
    assert (again.clip_new.current_key(), again.clip_insert.current_key(), again.clip_margin.value()) == (
        "Letter", "image", 25)


# ------------------------------------------------------- with the real core
@needs_core
def test_real_core_new_pdf_from_screenshot(win, clip, qapp):  # noqa: F811
    clip.setImage(image(960, 540, (200, 40, 40)))
    ws = win.new_from_clipboard()
    assert ws is not None and ws.pdf.page_count == 1
    w, h = ws.pdf.page(0).rect.width, ws.pdf.page(0).rect.height
    assert w > h                                              # a landscape page as the image (96 dpi)
    assert w == pytest.approx(720, abs=2) and h == pytest.approx(405, abs=2)


@needs_core
def test_real_core_new_pdf_from_text_is_searchable(win, clip, qapp):  # noqa: F811
    from pdfstudio.core.search import search

    clip.setText("Æblegrød – 25 €\n\nAndet afsnit med æøå.")
    ws = win.new_from_clipboard()
    assert ws is not None
    assert list(search(ws.pdf.doc, "æøå")) and list(search(ws.pdf.doc, "Æblegrød"))


@needs_core
def test_real_core_insert_screenshot_matches_neighbour(win, doc_ws, clip, toasts, qapp):  # noqa: F811
    clip.setImage(image(1200, 600))
    before = doc_ws.pdf.page_count
    pages = doc_ws.insert_from_clipboard(at=1)
    assert pages == [1] and doc_ws.pdf.page_count == before + 1
    r = doc_ws.pdf.page(1).rect
    assert r.width > r.height                                 # landscape, the neighbour's paper size
    doc_ws.pdf.undo()
    assert doc_ws.pdf.page_count == before
