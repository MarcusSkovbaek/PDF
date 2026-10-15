# Status / handover – Folio PDF (paused 2026-10-12 to protect the weekly usage limit)

## >>> START HERE (max-effort review & optimisation) <<<
State 2026-10-15: all planned features built (four modes, all tools, dialogs), whole-app QA pass done
(see "QA status" below for verified items, measurements and the ranked list of remaining issues).
Full suite: 408 tests green in ~60 s on Linux AND on Windows (GitHub Actions).
Windows installer: `packaging/installer.iss` (Inno Setup, Danish wizard) + `packaging/folio.spec`; the
workflow `.github/workflows/windows-installer.yml` builds on windows-latest, runs the tests, smoke-tests the
frozen app and a silent install, and publishes `FolioPDF-Setup-<version>.exe` (~42 MB) as an artifact.
Suggested order for the review: (1) the ranked QA issues (undo memory per step, reflow of text below a
grown paragraph, per-page text cache for the editor, block grouping on Word/InDesign PDFs, async search,
thumbnail cache limit); (2) a code-level review of core/text_edit.py, core/objects.py and the tools for
simplification; (3) a real-Windows UX pass (HiDPI, font picker popup placement, file associations).

Branch `claude/happy-wozniak-yin6iu`, everything committed. `python -m pytest` green (~60 s).
`QT_QPA_PLATFORM=offscreen python main.py --screenshot out.png --size 1440x900 file.pdf` works.
Read `CLAUDE.md` first – it is the binding architecture/API contract.

## QA status (agent C, 2026-10-15)
**Verified**: full suite green – 408 tests in ~61 s (was 398 tests in 282 s), no Qt warnings/tracebacks printed,
clean exit. Offscreen screenshots of home, every mode/tool/inspector (idle + active), font picker, comments panel +
filter, command palette, toast and all dialogs (merge, images→PDF, split, password, unsaved, error) in light and dark.
Scenario script: typing T/V/H/… in every inline editor never triggers tool shortcuts; switching tool/mode mid-edit
commits (text edit, add text, free text), mid-drag cancels (shape, ink, image move) without stale overlays;
undo/redo of every edit kind, save, reopen (valid, not repaired). Real-world PDFs from LibreOffice (58-page
Danish report with tables/images/bullets/footers, 14-page landscape slides, 348-page copy): every block edited
(`tests/stress_edit_all_blocks.py`-style, 98 + 153 blocks, avg 165/36 ms), page ops, comments, images,
merge/split, save/reopen all OK. Offline: no network-capable module is imported anywhere (now a test), netguard
is installed first in `main.py`. (PyInstaller part skipped – coordinator owns `packaging/`.)

**Measured** (58 pages): open 180 ms, first render 70 ms, scroll ~30 ms/step; 348 pages: open 280 ms,
scroll ~70 ms/step; open editor 170–310 ms, commit 110–350 ms, undo 70–180 ms.

**Fixed**
- Suite slowness (282 s → 61 s): inspector panels of inactive tools were orphaned as hidden top-level
  widgets (`setParent(None)`) – one per tool and one per comment selection, never freed, restyled on every
  theme change. Now parked under the inspector / deleted when transient (`workspace.Inspector`,
  `annotate_select` marks its panels `transientPanel`, `_annot_common` parents unused controls). Real app
  leak too (each comment click leaked a panel). Regression test `test_closing_leaks_no_windows`.
  Plus `Pixmap.set_rect` in test helpers (Python per-pixel in PyMuPDF 1.28: 4 s for 1600×900) →
  `conftest.filled_pixmap`; conftest deletes leftover top-level widgets after each Qt test and filters the
  offscreen plugin's "does not support" noise; expected MuPDF syntax errors of the tricky-stream test silenced.
- Deferred `QTimer.singleShot` callbacks now bound to their owner (tracebacks after a tab closed).
- Text editing on real documents (core `text_edit`): a caption set tight above a paragraph was one block
  (editor shifted sideways by the centred caption) → split at row size changes; LibreOffice bullets
  (`\uf095`, •, ·) glued to their item or to the paragraph after the list → list-item-aware grouping (bullet,
  item, text after the list are separate blocks; bullets not offered for editing); two-line list items now
  reflow inside the item instead of overflowing/overlapping the next item; first-line-indented paragraphs:
  editor box starts at the column with only the first line indented.
- Page pixmap cache: one 320 MB budget for all tabs (was per tab), background tabs evicted first.
- UI polish: Danish number format everywhere (`QLocale` da_DK: "12,5 mm", "11,0 pt"); compact X/Y/B/H fields in
  "Markér objekt" no longer clip ("120,0 mm"); dark-theme colour dots/swatches for black ink get a visible ring;
  split dialog's empty "Hver side" area explains the result; Tekstfelt uses the Aa icon like Tilføj tekst (T);
  pages "Slet sider" tooltip shows Delete.
- Shortcuts: shared keys fall back to the neutral *Vis* tool (H in Redigér = Hånd, not Fremhæv); test that no two
  actions share a key and every pill tooltip shows its shortcut; flushing a pending inspector change before the
  comment selection changes (it used to land on the new selection).
- Danish wording: accented imperatives (Redigér, Duplikér), »…« quotes, "fx", "…" without space in
  placeholders, real plurals instead of "side(r)" (`i18n.tr_n`), sentence-case section labels.
- README: features per mode/tool, shortcut tables, offline guarantee.

**Remaining (prioritised for the max-effort review)**
1. Undo snapshots store the whole document; after text edits the un-subset fonts make each step 3–5 MB
   (60 steps ≈ 200–300 MB). Store font streams once / compress snapshots / diff-based history.
2. Edited text that grows by a line overlaps the content below (no reflow of following blocks) – at least
   warn or offer to shrink; same for list items.
3. Editor open/commit latency 0.2–0.35 s on dense pages (`_page_context` re-extracts the page via
   `text_blocks(merge=False)` each time) – cache per page/version.
4. Block grouping heuristics (`_split_size_changes`, list items, `_continues`) need a corpus of Word/LO/InDesign
   PDFs; hanging indents and numbered lists ("1.", "a)") are not yet treated like bullets.
5. Search on 76 pages ~0.85 s (synchronous core call) – check the UI keeps it incremental on 500+ pages.
6. Thumbnail cache is unbounded (≈45 MB at 350 pages).
7. Font popup is clamped to the (800×600) offscreen screen in screenshots only – verify placement on a real
   multi-monitor Windows setup.
8. Split dialog folder field shows the end of long paths without middle elision.

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
2. DONE: mixed styles (bold/italic/colour/size per character) are shown in the editor
   (`_apply_rich_formats`) and kept on commit by the core. Fixed: zooming while editing now rescales the
   editor text (was only repositioned); the edited paragraph is scrolled into view (Tab below the fold);
   panel shows human font names (`display_font_name`).
3. DONE: rotated text (vertical, diagonal, rotated pages) is edited in place in a rotated editor.
4. DONE: font picker with search, "I dokumentet" / "Senest brugt" / "Alle skrifttyper", previews.
5. DONE: "Vis alle tekstbokse" toggle (visible pages only); Shift+Tab = previous paragraph.
6. DONE: key-hint chip placed where it covers no text.
   DONE: wide gaps / table cells in changed lines keep their columns; cells keep their alignment.
   DONE: Tilføj tekst alignment buttons (left/centre/right) with live preview.
   OPEN: never-wrapping text: editor frame reaches the page edge (long frame on diagonal stamps).
7. Real-world stress test (LibreOffice/Word-style PDF with subset CID fonts, justified text, tables, bullets;
   `python tests/stress_edit_all_blocks.py file.pdf` edits every block): no exceptions, alignment/justification,
   bold/italic runs and colours preserved, undo exact. Open: when a changed line contains a wide gap
   (table cells close enough to be one block) – FIXED, see 6.
   FIXED: list items / addresses were treated as one reflowing paragraph (a lengthened item flowed into
   the next one) – soft breaks now use the classic reflow test against the real column width; blocks
   without soft breaks grow sideways. Justified paragraphs are shown in the editor as one Qt-justified
   paragraph (line breaks match the PDF); on commit the soft breaks before the first change are restored
   so earlier lines stay pixel-identical (`layout_info().justified/soft_breaks`, `_restore_soft_breaks`).
   FIXED: saving now subsets fonts (`PdfDocument._subset_fonts`) – editing all 18 blocks grew the file
   62 KB → 1.6 MB before, 217 KB now.

## Next phases – ready-to-launch agents (run 1+2 in parallel, then 3)
- **Add text / image – optimisation backlog**: select/move/resize of placed images and text boxes
  (images are page content, so this needs a core "object" layer or placing them as stamp annotations);
  alignment buttons (left/centre/right) for new text.
- **Comment tools – optimisation backlog** (agent B no longer needed): DONE resize handles (corners, opposite
  corner fixed, notes excluded); TODO multi-select + group delete; free-text (typewriter) annotation; filter by type in the comments panel;
  highlight colour preview while hovering the swatches. DONE: comments panel (`ui/panels/comments.py`,
  sidebar tab; grouped by page, exact marked text for markups, click = jump + select; sidebar tabs are
  icon tabs with tooltips now).
- **Agent C – QA loop** after A+B: full test run, light/dark screenshots of every mode and dialog,
  fix visual/UX issues and bugs, update the Danish `README.md` (exists) (install, `build_windows.bat`, offline guarantee).
Each agent prompt must say: read CLAUDE.md + STATUS.md, stay in own files, do not git commit
(coordinator commits), run pytest + screenshot self-check loop before reporting.
