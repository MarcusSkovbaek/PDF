# Status / handover – Folio PDF (paused 2026-10-12 to protect the weekly usage limit)

## >>> START HERE (next session, max/ultra) <<<
State at pause (2026-10-13): **full suite green in ~50 s, no hangs, no crashes**; an end-to-end run
(edit text, add text, add image, highlight, note, rotate, 6× undo + redo, save, reopen) verified the saved
PDF content and validity. Visual review done for every mode, tool and dialog in light + dark.
Resolved at pause: the "hang" was a test fixture leaking every MainWindow (deleteLater never ran outside an
event loop → each test restyled all previous windows, 78 s for test_ui_shell); the exit segfault was Qt
objects left to interpreter finalisation – fixed in tests (`conftest.qapp` teardown) AND in the app
(`ui.app.shutdown()` after `app.exec()`, clean exit verified).
Next: work through the optimisation backlogs below, then a QA loop (agent C).

Branch `claude/happy-wozniak-yin6iu`, everything committed. `python -m pytest` green (~50 s).
`QT_QPA_PLATFORM=offscreen python main.py --screenshot out.png --size 1440x900 file.pdf` works.
Read `CLAUDE.md` first – it is the binding architecture/API contract.

## Done
- Foundation: `core/document.py` (edit/undo/redo/atomic save), `history.py`, `security/netguard.py`, i18n.
- Core engine (tested): `render`, `pages`, `merge`, `images`, `search`, `annotations`.
- Split dialog: `ui/dialogs/split_dialog.py` (each page / every N / ranges, live preview, folder picker,
  overwrite confirm, success toast) + `core.pages.split_plan()` + `tests/test_split_dialog.py`.
- Images -> PDF dialog: `ui/dialogs/images_dialog.py` (thumbnail grid with drag-reorder, file/Explorer drops,
  Ctrl+V clipboard screenshots, sort/remove, page size/margin/orientation/placement, live page preview);
  dropping several images on the app opens it pre-filled. Tests: `tests/test_images_dialog.py`.
- Merge dialog: `ui/dialogs/merge_dialog.py` (file list with first-page thumbnails, drag/up/down reorder,
  Explorer drops, per-file page ranges, password prompt for locked PDFs, bookmark option). Tests:
  `tests/test_merge_dialog.py`. ALL DIALOGS (split, images, merge) ARE DONE.
  The dialogs are the reference implementation for the remaining ones (uses `FolioDialog`, `SegmentedControl`, `da_split.py`).
- Pages mode ("Sider"): centred organiser grid, pill with rotate/duplicate/extract/insert/delete/split,
  selection-aware inspector panel `ui/panels/pages_panel.py` (+ `tests/test_pages_mode.py`).
  Fixed: stale tool buttons stayed on top of the mode pill after switching modes; selection
  not reported after model resets; closed windows still reacting to theme changes.
- **"Rediger tekst" tool** (`ui/viewer/tools/edit_text.py`, mode "Rediger"): hover outlines paragraphs; click
  opens an inline editor exactly over the text (same font/size/colour/line pitch, baseline-aligned by measuring
  Qt's layout, letter-spacing compensated to the PDF's line width, page background sampled so the old text
  never shows through); Ctrl+Enter/click elsewhere saves, Tab saves + next paragraph, Esc cancels; inspector
  panel overrides font/size/colour/bold/italic with "Nulstil til original"; undo/redo cancels an open edit.
  `ui/widgets/color_button.py`, `i18n/da_edit.py`, `tests/test_edit_text_tool.py` (8 tests).
  Core verified: no file growth over repeated edits, ~20 ms/edit, images/vector art/other blocks untouched;
  fixed reading order of side-by-side columns (`_reading_order`).
- **Kommentér tools** (`tools/annotate_markup.py`, `annotate_draw.py`, `annotate_select.py`, `_annot_common.py`):
  Markér kommentar (V: select/move/restyle/delete, double-click edits notes), Fremhæv (H) / Understreg (U) /
  Gennemstreg (K) with flowing multi-line/multi-page word selection previewed in the markup colour
  (`core.annotations.add_markup_lines`), Note (N), Tegn (D, freehand), Figur (R: rect/ellipse/line/arrow,
  Shift = square/circle/45°). Inspector: colour presets + custom, opacity, width, fill; per-tool styles
  persisted in local QSettings. `i18n/da_comment.py`, `tests/test_annotate_tools.py`.
  Fixed: tools sharing a shortcut across modes (H, V) made both shortcuts dead – now one action per key
  dispatches by mode; `Tool.changed` now really refreshes the inspector.
- **Tilføj tekst** (`tools/add_text.py`, T): click = type at that point (box to the right margin), drag = set
  wrap width; reuses the Rediger-tekst inline editor + inspector; default = document body style
  (`insert.document_text_style`), last used style remembered for the session; saved via
  `insert.add_text_box` (editor baseline/line height use the same font metrics as the core).
- **Tilføj billede** (`tools/add_image.py`, I): choose file or clipboard in the inspector (preview), click =
  natural size (96 dpi, max 60 % of the page, centred on the click), drag = fit into frame, aspect kept.
  Tests: `tests/test_add_tools.py` (5, green individually). `edit_text.py` gained hooks `_block_pitch` / `_write`.
- Packaging: `packaging/folio.spec`, `build_windows.bat`, `block_network_windows.ps1`, `assets/folio.*`.

## Interrupted by usage limit – state verified by coordinator
1. **Text editing engine** – DONE and verified (subset-font cmap rebuild with advance-width proof is complete).
2. **UI shell**: looks premium already (header with tabs, offline badge, mode bar Vis/Rediger/Kommentér/Sider,
   floating tool pill, thumbnails/bookmarks/search sidebar, properties panel, status bar with zoom).
   Open items seen in screenshot review:
   - thumbnail list now sizes cells per page aspect (rotated/landscape pages fixed) – DONE;
   - dark theme + home screen screenshots reviewed – look good, no issues;
   - all four modes have their tools; Tilføj tekst/billede reviewed visually (new-text editor is transparent).

## Optimisation backlog for "Rediger tekst" (for a max/ultra session)
1. DONE: `text_edit.layout_info(doc, block)` exposes the core's alignment + wrap width; the editor wraps
   and aligns (left/centre/right) exactly like the written result (verified overlay-exact).
2. Mixed styles inside a paragraph (a bold word): the editor shows one font; show per-character styles
   (rich QTextEdit fed from `TextLine.char_styles`) and keep them on commit.
3. Rotated text: the editor is horizontal; consider a rotated overlay (QGraphicsProxyWidget) or a popover.
4. Font picker: render family names in their own font, search field, recently used.
5. Optional "show all text boxes" outline mode (Acrobat style) and Shift+Tab to the previous paragraph.
6. Key-hint chip placement (now hidden on first keystroke).

## Next phases – ready-to-launch agents (run 1+2 in parallel, then 3)
- **Add text / image – optimisation backlog**: select/move/resize of placed images and text boxes
  (images are page content, so this needs a core "object" layer or placing them as stamp annotations);
  alignment buttons (left/centre/right) for new text.
- **Comment tools – optimisation backlog** (agent B no longer needed): resize handles in "Markér kommentar";
  multi-select + group delete; free-text (typewriter) annotation; comments list panel in the sidebar
  (jump to / filter by type); highlight colour preview while hovering the swatches.
- **Agent C – QA loop** after A+B: full test run, light/dark screenshots of every mode and dialog,
  fix visual/UX issues and bugs, update the Danish `README.md` (exists) (install, `build_windows.bat`, offline guarantee).
Each agent prompt must say: read CLAUDE.md + STATUS.md, stay in own files, do not git commit
(coordinator commits), run pytest + screenshot self-check loop before reporting.
