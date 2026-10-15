"""Offscreen smoke tests for the application shell (window, viewer, panels)."""

from __future__ import annotations

import os
import time

import pymupdf
import pytest

from tests.conftest import make_png


@pytest.fixture
def win(qapp, tmp_path):
    from pdfstudio.ui import app as app_mod
    from pdfstudio.ui.dialogs import common
    from pdfstudio.ui.main_window import MainWindow

    app_mod.configure_settings(str(tmp_path / "settings"))
    app_mod.create_app(theme="light")
    w = MainWindow()
    w.resize(1280, 820)
    w.show()
    qapp.processEvents()
    yield w
    common._AUTO["save_changes"] = "discard"
    w.close()
    common._AUTO.clear()
    w.deleteLater()
    # processEvents() never runs DeferredDelete outside an event loop: without
    # this every test's window would leak and be restyled by later tests
    from PySide6.QtCore import QCoreApplication, QEvent

    QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete.value)
    qapp.processEvents()
    app_mod.configure_settings(None)


def pump(qapp, ms: int = 50) -> None:
    end = time.perf_counter() + ms / 1000
    while time.perf_counter() < end:
        qapp.processEvents()


def test_network_guard_active():
    from pdfstudio.security import netguard

    assert netguard.is_installed()


def test_window_starts_with_home(win):
    from pdfstudio.ui.home_view import HomeView

    assert win.tabs.count() == 1
    assert isinstance(win.current_widget(), HomeView)
    for aid in ("file.open", "file.save", "file.save_as", "edit.undo", "edit.redo", "edit.find",
                "view.palette", "view.zoom_in", "view.fit_page", "pages.rotate_right", "tools.merge"):
        assert win.actions.has(aid), aid
    # shortcuts per contract
    assert win.actions.action("file.open").shortcut().toString() == "Ctrl+O"
    assert win.actions.action("view.palette").shortcut().toString() == "Ctrl+K"
    assert not win.actions.action("file.save").isEnabled()
    assert not win.modebar.isVisible()


def test_open_navigate_zoom(win, qapp, sample_pdf_path):
    ws = win.open_path(sample_pdf_path)
    assert ws is not None
    pump(qapp, 150)
    assert win.current_workspace() is ws
    assert ws.view.page_count == 3
    assert win.tabs.count() == 1  # the home tab was replaced
    assert ws.thumbs.model().rowCount() == 3
    ws.view.go_to_page(2)
    pump(qapp)
    assert ws.view.current_page == 2
    assert win.page_nav.field.text() == "3"
    ws.view.set_zoom(2.0)
    assert ws.view.zoom == pytest.approx(2.0)
    assert ws.view.fit_mode is None
    z = ws.view.zoom
    win.actions.trigger("view.zoom_in")
    pump(qapp, 300)
    assert ws.view.zoom > z
    win.actions.trigger("view.fit_width")
    assert ws.view.fit_mode == "width"
    # visible pages get rendered lazily
    ws.view.render_visible_now()
    assert any(ws.view.page_item(i).pix is not None for i in ws.view.visible_pages())
    # opening the same file again re-uses the tab
    assert win.open_path(sample_pdf_path) is ws
    assert win.tabs.count() == 1


def test_rotate_undo_redo_via_actions(win, qapp, sample_pdf_path):
    ws = win.open_path(sample_pdf_path)
    ws.view.go_to_page(0)
    assert ws.pdf.page(0).rotation == 0
    assert win.actions.trigger("pages.rotate_right")
    pump(qapp)
    assert ws.pdf.page(0).rotation == 90
    assert ws.pdf.is_dirty
    undo_text = win.actions.action("edit.undo").text()
    assert undo_text.startswith("Fortryd:")
    assert win.actions.action("edit.undo").isEnabled()
    assert win.actions.trigger("edit.undo")
    pump(qapp)
    assert ws.pdf.page(0).rotation == 0
    assert win.actions.trigger("edit.redo")
    pump(qapp)
    assert ws.pdf.page(0).rotation == 90
    # page view relayouted with the rotated size
    item = ws.view.page_item(0)
    assert item.w > item.h


def test_page_operations(win, qapp, sample_pdf_path):
    ws = win.open_path(sample_pdf_path)
    first_text = ws.pdf.page(0).get_text()
    ws.move_pages([0], 3)
    pump(qapp)
    assert ws.pdf.page(2).get_text() == first_text
    assert ws.thumbs.model().rowCount() == 3
    ws.view.go_to_page(0)
    ws.duplicate_selected()
    pump(qapp)
    assert ws.pdf.page_count == 4
    ws.thumbs.select_pages([0, 1])
    ws.thumbs.setFocus()
    assert ws.selected_pages() == [0, 1]
    ws.delete_selected()
    pump(qapp)
    assert ws.pdf.page_count == 2
    ws.insert_blank()
    assert ws.pdf.page_count == 3
    win.actions.trigger("mode.pages")
    pump(qapp)
    assert ws.mode == "pages"
    assert ws.organizer is not None and ws.organizer.view.model().rowCount() == 3


def test_search_highlights(win, qapp, sample_pdf_path):
    ws = win.open_path(sample_pdf_path)
    win.actions.trigger("edit.find")
    assert ws.sidebar.current_tab() == "search"
    ws.search.set_query("kolonne")
    ws.search.finish_now()
    hits = ws.search.hits()
    assert len(hits) > 5
    assert all(h.page == 1 for h in hits)
    items = ws.view.search_hit_items()
    assert len(items) == len(hits)
    assert items[0].parentItem() is ws.view.page_item(1)
    ws.search.next_hit()
    assert ws.search.model.current == 1
    ws.search.set_query("findesikke")
    ws.search.finish_now()
    assert ws.search.hits() == []
    assert ws.view.search_hit_items() == []


def test_select_tool_copies_text(win, qapp, sample_pdf_path):
    from PySide6.QtGui import QGuiApplication

    ws = win.open_path(sample_pdf_path)
    assert ws.tool is not None and ws.tool.id == "view.select"
    assert win.actions.trigger("edit.select_all")
    assert ws.can_copy()
    assert win.actions.trigger("edit.copy")
    text = QGuiApplication.clipboard().text()
    assert "Kvartalsrapport" in text
    assert "Roteret side" in text
    # rotated page words are mapped to display coordinates (inside the page)
    words = ws.tool.words(2)
    item = ws.view.page_item(2)
    assert words and all(0 <= w.rect.center().x() <= item.w and 0 <= w.rect.center().y() <= item.h for w in words)


def test_save_as_and_recent(win, qapp, sample_pdf_path, tmp_path):
    from pdfstudio.ui.app import recent_files

    ws = win.open_path(sample_pdf_path)
    win.actions.trigger("pages.rotate_left")
    assert ws.pdf.is_dirty
    out = str(tmp_path / "gemt.pdf")
    assert win.save_as(ws, path=out)
    assert os.path.isfile(out)
    assert not ws.pdf.is_dirty
    assert recent_files()[0] == os.path.abspath(out)
    with pymupdf.open(out) as doc:
        assert doc[0].rotation == 270
    assert "Gemt" in " ".join(win.toasts.visible_messages())


def test_close_dirty_tab_prompts(win, qapp, sample_pdf_path):
    from pdfstudio.ui.dialogs import common

    ws = win.open_path(sample_pdf_path)
    win.actions.trigger("pages.rotate_right")
    common._AUTO["save_changes"] = "cancel"
    assert not win.close_tab(win.tabs.currentIndex())
    assert win.current_workspace() is ws
    common._AUTO["save_changes"] = "discard"
    assert win.close_tab(win.tabs.currentIndex())
    assert win.current_workspace() is None
    assert win.tabs.count() == 1  # home is back


def test_new_blank_and_save_without_path_uses_save_as(win, qapp, tmp_path, monkeypatch):
    win.actions.trigger("file.new")
    ws = win.current_workspace()
    assert ws.pdf.path is None and ws.pdf.page_count == 1
    called = {}
    monkeypatch.setattr(win, "save_as", lambda w=None, path=None: called.setdefault("ok", True))
    win.actions.trigger("file.save")
    assert called.get("ok")


def test_open_images_and_drop_overlay(win, qapp, tmp_path, monkeypatch):
    from pdfstudio.ui.dialogs.images_dialog import ImagesToPdfDialog

    # several images open the images->PDF dialog; simulate pressing "Opret PDF"
    monkeypatch.setattr(ImagesToPdfDialog, "exec", lambda self: (self._create(), self.result())[1])
    p1 = tmp_path / "a.png"
    p2 = tmp_path / "b.png"
    p1.write_bytes(make_png())
    p2.write_bytes(make_png(200, 300))
    win.open_files([str(p1)])
    assert win.current_workspace().pdf.page_count == 1
    win.open_files([str(p1), str(p2)])
    ws = win.current_workspace()
    assert ws.pdf.page_count == 2
    assert ws.pdf.path is None


def test_command_palette(win, qapp, sample_pdf_path):
    from pdfstudio.ui.widgets.command_palette import fuzzy_score

    assert fuzzy_score("gem", "Gem som…") is not None
    assert fuzzy_score("xyz", "Gem") is None
    assert fuzzy_score("rot", "Rotér mod højre")[0] > fuzzy_score("rot", "Indsæt fra fil…" + "rot")[0] - 100
    win.open_path(sample_pdf_path)
    win.actions.trigger("view.palette")
    assert win.palette.isVisible()
    win.palette.field.setText("højre")
    first = win.palette.model.rows[0][0]
    assert first.id == "pages.rotate_right"
    win.palette._run_index(win.palette.list.currentIndex())
    assert not win.palette.isVisible()
    assert win.current_pdf().page(0).rotation == 90


def test_theme_toggle(win, qapp):
    from pdfstudio.ui.theme import tokens as T
    from pdfstudio.ui.theme.manager import theme_manager

    tm = theme_manager()
    tm.set_mode("dark", persist=False)
    assert T.current().is_dark
    assert "rgba" in qapp.styleSheet()
    tm.set_mode("light", persist=False)
    assert not T.current().is_dark


def test_tools_registry_and_modes(win, qapp, sample_pdf_path):
    from pdfstudio.ui.viewer.tools.base import ToolRegistry

    ids = [t.id for t in ToolRegistry.for_mode("view")]
    assert ids[:2] == ["view.select", "view.hand"]
    ws = win.open_path(sample_pdf_path)
    win.actions.trigger("tool.view.hand")
    assert ws.tool.id == "view.hand"
    for mode in ("edit", "annotate", "pages", "view"):
        win.set_mode(mode)
        assert ws.mode == mode
    assert ws.tool.id == "view.hand"  # remembered per mode


def test_large_document_opens_fast(win, qapp, tmp_path):
    doc = pymupdf.open()
    for i in range(300):
        doc.new_page().insert_text((72, 72), f"Side {i + 1}")
    path = str(tmp_path / "big.pdf")
    doc.save(path)
    doc.close()
    t0 = time.perf_counter()
    ws = win.open_path(path)
    qapp.processEvents()
    elapsed = time.perf_counter() - t0
    assert ws.pdf.page_count == 300
    assert elapsed < 2.0
    pump(qapp, 300)
    rendered = sum(1 for i in range(300) if ws.view.page_item(i).pix is not None)
    assert 0 < rendered < 20
    thumbs = ws.thumbs.model().renderer._cache
    assert len(thumbs) < 40
    ws.view.go_to_page(299)
    pump(qapp, 100)
    assert ws.view.current_page == 299


def test_outline_panel(win, qapp, tmp_path):
    doc = pymupdf.open()
    for i in range(3):
        doc.new_page().insert_text((72, 72), f"Kapitel {i + 1}")
    doc.set_toc([[1, "Kapitel 1", 1], [2, "Afsnit 1.1", 1], [1, "Kapitel 2", 2], [1, "Kapitel 3", 3]])
    path = str(tmp_path / "toc.pdf")
    doc.save(path)
    doc.close()
    ws = win.open_path(path)
    assert ws.outline.item_count() == 4
    item = ws.outline.tree.topLevelItem(2)
    ws.outline._activate(item)
    pump(qapp)
    assert ws.view.current_page == 2


def test_export_images(win, qapp, sample_pdf_path, tmp_path):
    win.open_path(sample_pdf_path)
    out = tmp_path / "png"
    out.mkdir()
    assert win.export_images(str(out), dpi=36) == 3
    assert len(list(out.glob("*.png"))) == 3


def test_password_protected(win, qapp, tmp_path, monkeypatch):
    doc = pymupdf.open()
    doc.new_page().insert_text((72, 72), "Hemmelig")
    path = str(tmp_path / "locked.pdf")
    doc.save(path, encryption=pymupdf.PDF_ENCRYPT_AES_256, user_pw="kode", owner_pw="kode")
    doc.close()
    answers = iter(["forkert", "kode"])
    seen = []

    def fake_ask(parent, name, wrong=False):
        seen.append(wrong)
        return next(answers)

    import pdfstudio.ui.dialogs.password as pw

    monkeypatch.setattr(pw, "ask_password", fake_ask)
    ws = win.open_path(path)
    assert ws is not None and ws.pdf.page_count == 1
    assert seen == [False, True]



def test_mouse_text_selection_and_wheel_zoom(win, qapp, sample_pdf_path):
    from PySide6.QtCore import QPoint, QPointF, Qt
    from PySide6.QtGui import QWheelEvent
    from PySide6.QtTest import QTest

    ws = win.open_path(sample_pdf_path)
    view = ws.view
    view.go_to_page(0)
    pump(qapp)
    words = ws.tool.words(0)
    first = next(w for w in words if w.text == "Kvartalsrapport")
    last = next(w for w in words if w.text == "2026")
    a = view.page_to_viewport(0, first.rect.center()).toPoint()
    b = view.page_to_viewport(0, last.rect.center()).toPoint()
    vp = view.viewport()
    QTest.mousePress(vp, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, a)
    QTest.mouseMove(vp, QPoint((a.x() + b.x()) // 2, a.y()))
    QTest.mouseMove(vp, b)
    QTest.mouseRelease(vp, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, b)
    assert ws.tool.selected_text() == "Kvartalsrapport 2026"
    assert ws.can_copy()
    # Ctrl+wheel zoom keeps the point under the cursor fixed
    anchor = QPointF(b)
    before_idx, before_pt = view.page_at(view.viewport_to_scene(anchor))
    z0 = view.zoom
    ev = QWheelEvent(anchor, QPointF(vp.mapToGlobal(b)), QPoint(0, 0), QPoint(0, 240),
                     Qt.MouseButton.NoButton, Qt.KeyboardModifier.ControlModifier, Qt.ScrollPhase.NoScrollPhase, False)
    qapp.sendEvent(vp, ev)
    assert view.zoom > z0
    after_idx, after_pt = view.page_at(view.viewport_to_scene(anchor))
    assert after_idx == before_idx
    assert abs(after_pt.x() - before_pt.x()) < 2 and abs(after_pt.y() - before_pt.y()) < 2


def test_custom_tool_properties_and_toolbar(win, qapp, sample_pdf_path):
    from PySide6.QtWidgets import QLabel

    from pdfstudio.ui.viewer.tools.base import Tool, ToolRegistry

    class DummyTool(Tool):
        id = "annotate.test_dummy"
        label = "Testværktøj"
        icon = "pen"
        mode = "annotate"
        order = -1  # first in its mode (real annotate tools start at 0)

        def __init__(self, parent=None):
            super().__init__(parent)
            self.widget = QLabel("Egenskaber for test")

        def properties_widget(self):
            return self.widget

    ToolRegistry.register(DummyTool)
    try:
        ws = win.open_path(sample_pdf_path)
        win.set_mode("annotate")
        assert ws.tool is not None and ws.tool.id == "annotate.test_dummy"
        assert "annotate.test_dummy" in ws.toolbar.tool_ids()
        assert ws.inspector.scroll.widget() is ws.tool.widget
        win.set_mode("view")
        assert ws.inspector.scroll.widget() is ws.inspector.doc_info
        win.set_mode("annotate")
        assert ws.inspector.scroll.widget() is ws.tool.widget  # widget survived the round trip
    finally:
        ToolRegistry._tools.pop("annotate.test_dummy", None)


def test_thumbnail_drop_reorders(win, qapp, sample_pdf_path):
    from PySide6.QtCore import QByteArray, QMimeData, QPointF, Qt
    from PySide6.QtGui import QDropEvent

    from pdfstudio.ui.panels.thumbnails import MIME

    ws = win.open_path(sample_pdf_path)
    pump(qapp, 100)
    view = ws.thumbs
    texts = [ws.pdf.page(i).get_text() for i in range(3)]
    last = view.visualRect(view.model().index(2))
    md = QMimeData()
    md.setData(MIME, QByteArray(b"0"))
    pos = QPointF(last.center().x(), last.bottom() - 2)
    ev = QDropEvent(pos, Qt.DropAction.MoveAction, md, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier)
    view.dropEvent(ev)
    pump(qapp, 50)
    assert [ws.pdf.page(i).get_text() for i in range(3)] == [texts[1], texts[2], texts[0]]


def test_closing_leaks_no_windows(qapp, tmp_path, sample_pdf_path):
    """Inspector panels of every tool, rebuilt selection panels and the
    document panels all die with the window – nothing is left behind as a
    hidden top-level widget (those were restyled on every theme change and
    made the test-suite crawl)."""
    from PySide6.QtCore import QCoreApplication, QEvent

    from pdfstudio.core import annotations as A
    from pdfstudio.ui import app as app_mod
    from pdfstudio.ui.dialogs import common
    from pdfstudio.ui.main_window import MainWindow
    from pdfstudio.ui.viewer.tools.base import MODES, ToolRegistry

    def flush_deletes():
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete.value)
        qapp.processEvents()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete.value)

    flush_deletes()
    before = len(qapp.topLevelWidgets())
    app_mod.configure_settings(str(tmp_path / "settings"))
    try:
        w = MainWindow()
        w.resize(1280, 820)
        w.show()
        ws = w.open_path(sample_pdf_path)
        A.add_note(ws.pdf, 0, (330, 92), "Tjek tallene", (1, .84, 0))
        A.add_shape(ws.pdf, 0, "rect", (72, 420), (300, 480), (.9, .2, .2))
        for mode in MODES:
            ws.set_mode(mode)
            for cls in ToolRegistry.for_mode(mode):
                ws.set_tool(cls.id)
                qapp.processEvents()
        ws.set_tool("annotate.select")
        infos = A.list_annotations(ws.pdf.doc, 0)
        for _ in range(4):
            for info in infos:
                ws.tool.select(info)
            ws.tool.set_selection(infos)
            ws.tool.select(None)
        flush_deletes()
        transient = [p for p in ws.inspector.findChildren(type(ws.inspector.doc_info))
                     if p.property("transientPanel")]
        assert len(transient) <= 1   # rebuilt selection panels are deleted, not parked
        ws.set_mode("view")
        common._AUTO["save_changes"] = "discard"
        assert w.close()
        w.deleteLater()
        flush_deletes()
        left = [type(x).__name__ for x in qapp.topLevelWidgets()]
        assert len(left) == before, left
    finally:
        common._AUTO.clear()
        app_mod.configure_settings(None)


def test_no_two_actions_share_a_shortcut(win, qapp, sample_pdf_path):
    """Every key sequence of the window belongs to exactly one action (Qt
    disables ambiguous shortcuts), tools sharing a key across modes included,
    and every tool button of every mode pill names its shortcut."""
    from PySide6.QtCore import Qt
    from PySide6.QtGui import QAction

    from pdfstudio.ui.viewer.tools.base import MODES, ToolRegistry

    ws = win.open_path(sample_pdf_path)
    owners: dict[str, list[str]] = {}
    for act in win.findChildren(QAction):
        if act.shortcutContext() == Qt.ShortcutContext.WidgetShortcut:
            continue  # display-only hints (menus)
        for seq in act.shortcuts():
            if not seq.isEmpty():
                owners.setdefault(seq.toString(), []).append(act.objectName() or act.text())
    dups = {k: v for k, v in owners.items() if len(v) > 1}
    assert not dups, dups
    for cls in ToolRegistry.all():   # every tool shortcut reaches a tool
        if cls.shortcut:
            assert cls.shortcut.upper() in owners, cls.id
    for mode in MODES:
        ws.set_mode(mode)
        qapp.processEvents()
        tools = ToolRegistry.for_mode(mode)
        assert ws.toolbar.tool_ids() == [t.id for t in tools]          # pill order = registry order
        for cls in tools:
            tip = ws.toolbar._tool_buttons[cls.id].toolTip()
            assert tip.startswith(cls.display_label())
            if cls.shortcut:
                assert tip.endswith(f"({cls.shortcut})"), tip


def test_pixmap_cache_budget_is_shared_by_all_tabs(win, qapp, tmp_path, monkeypatch):
    """One memory budget for every open document: rendering in the visible
    tab takes the pixmaps of background tabs first."""
    from pdfstudio.ui.viewer import page_view

    paths = []
    for name in ("a.pdf", "b.pdf"):
        doc = pymupdf.open()
        for i in range(6):
            doc.new_page().insert_text((72, 72), f"{name} side {i + 1}")
        paths.append(str(tmp_path / name))
        doc.save(paths[-1])
    ws_a = win.open_path(paths[0])
    for i in range(6):
        ws_a.view.go_to_page(i)
        ws_a.view.render_visible_now()
    a_bytes = ws_a.view.cache_bytes()
    assert a_bytes > 0
    ws_b = win.open_path(paths[1])          # a is a background tab now
    qapp.processEvents()
    assert not ws_a.view.isVisible()
    monkeypatch.setattr(page_view, "CACHE_BYTES", a_bytes)
    ws_b.view.render_visible_now()
    assert ws_a.view.cache_bytes() < a_bytes                      # background tab gave memory back
    assert ws_b.view.cache_bytes() > 0
    assert ws_a.view.cache_bytes() + ws_b.view.cache_bytes() <= a_bytes


def test_no_network_capable_module_is_loaded():
    """The whole app (every tool, panel and dialog) imports nothing that can
    talk to the network – on top of the netguard kill switch. Checked in a
    fresh interpreter: pytest's own plugins import ssl."""
    import subprocess
    import sys

    code = (
        "import os, sys; os.environ['QT_QPA_PLATFORM'] = 'offscreen'\n"
        "import main\n"
        "from pdfstudio.ui import app, main_window\n"
        "from pdfstudio.ui.dialogs import images_dialog, merge_dialog, password, split_dialog\n"
        "from pdfstudio.ui.viewer.tools.base import ToolRegistry; ToolRegistry.discover()\n"
        "banned = ('PySide6.QtNetwork', 'PySide6.QtWebEngineCore', 'PySide6.QtWebEngineWidgets',"
        " 'PySide6.QtWebSockets', 'urllib.request', 'http.client', 'requests', 'urllib3', 'ssl', 'ftplib',"
        " 'smtplib', 'xmlrpc.client')\n"
        "print([m for m in sys.modules if any(m == b or m.startswith(b + '.') for b in banned)])\n"
    )
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    out = subprocess.run([sys.executable, "-c", code], cwd=root, capture_output=True, text=True, timeout=120)
    assert out.returncode == 0, out.stderr
    assert out.stdout.strip().splitlines()[-1] == "[]"
