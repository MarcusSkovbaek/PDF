# Status / handover – Folio PDF (paused 2026-10-10 to protect the weekly usage limit)

Branch `claude/happy-wozniak-yin6iu`, everything committed. `python -m pytest` green (≈250 tests).
`QT_QPA_PLATFORM=offscreen python main.py --screenshot out.png --size 1440x900 file.pdf` works.
Read `CLAUDE.md` first – it is the binding architecture/API contract.

## Done
- Foundation: `core/document.py` (edit/undo/redo/atomic save), `history.py`, `security/netguard.py`, i18n.
- Core engine (tested): `render`, `pages`, `merge`, `images`, `search`, `annotations`.
- Split dialog: `ui/dialogs/split_dialog.py` (each page / every N / ranges, live preview, folder picker,
  overwrite confirm, success toast) + `core.pages.split_plan()` + `tests/test_split_dialog.py`.
  Reference implementation for the other dialogs (uses `FolioDialog`, `SegmentedControl`, `da_split.py`).
- Packaging: `packaging/folio.spec`, `build_windows.bat`, `block_network_windows.ps1`, `assets/folio.*`.

## Interrupted by usage limit – state verified by coordinator
1. **Text editing engine** (`core/fonts.py`, `text_edit.py`, `insert.py` + tests): WORKS in a smoke test –
   editing keeps font/size/baseline, Danish glyphs (ÆØÅ æøå) fall back correctly, rotated page blocks
   are in display coords. The agent was last adding *advance-width verification for embedded subset
   fonts* in `resolve_font` – finish/verify that, then review against the CLAUDE.md requirements
   (images/vector art preserved, other blocks untouched, undo, file growth after repeated edits).
2. **UI shell**: looks premium already (header with tabs, offline badge, mode bar Vis/Rediger/Kommentér/Sider,
   floating tool pill, thumbnails/bookmarks/search sidebar, properties panel, status bar with zoom).
   Open items seen in screenshot review:
   - thumbnail list now sizes cells per page aspect (rotated/landscape pages fixed) – DONE;
   - dark theme + home screen screenshots reviewed – look good, no issues;
   - "Rediger", "Kommentér", "Sider" modes have no tools yet (phase 3/4).

## Next phases – ready-to-launch agents (run 1+2 in parallel, then 3)
- **Agent A – Edit tools UI** (`ui/viewer/tools/edit_text.py`, `add_text.py`, `add_image.py`,
  `ui/panels/properties` widgets, `i18n/da_edit.py`, `tests/test_ui_edit.py`): mode "Rediger".
  Edit text = hover outlines text blocks; click opens an inline editor overlay (QPlainTextEdit with
  `fonts.qt_family_hint`, scaled to zoom, same color) exactly over the block; Enter/blur commits via
  `text_edit.replace_block_text`, Esc cancels; inspector shows font/size/color/bold/italic to override.
  Add text = drag a box, defaults from `insert.document_text_style`. Add image = file picker or
  clipboard paste, drag to place, keep aspect. Select/move/resize/delete existing annotations.
- **Agent B – Comment tools + dialogs + pages mode** (`tools/markup.py`, `note.py`, `ink.py`, `shape.py`,
  `dialogs/merge_dialog.py` `MergeDialog` (+`.result_bytes`), `dialogs/images_dialog.py` `ImagesToPdfDialog`
  (drag&drop, Ctrl+V clipboard screenshots, reorder, page size/orientation/margin options, live preview),
  pages-mode grid organiser (split dialog already DONE – follow its style); `i18n/da_comment.py`,
  `da_dialogs.py`; tests). Home cards already call actions `tools.merge`, `tools.images_to_pdf`.
- **Agent C – QA loop** after A+B: full test run, light/dark screenshots of every mode and dialog,
  fix visual/UX issues and bugs, update the Danish `README.md` (exists) (install, `build_windows.bat`, offline guarantee).
Each agent prompt must say: read CLAUDE.md + STATUS.md, stay in own files, do not git commit
(coordinator commits), run pytest + screenshot self-check loop before reporting.
