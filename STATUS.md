# Status – Folio PDF (paused 2026-10-10, usage limit)

All work is committed on `claude/happy-wozniak-yin6iu`. `python -m pytest` is green and
`main.py --screenshot` starts the app and opens a PDF.

## Done
- Foundation: `core/document.py` (edit/undo/redo/save), `history.py`, `security/netguard.py`, i18n, `CLAUDE.md` contract.
- Core engine (tested): `render`, `pages`, `merge`, `images`, `search`, `annotations`.
- Packaging: `packaging/folio.spec`, `build_windows.bat`, `block_network_windows.ps1`, icon in `assets/`.

## Interrupted mid-work (verify + finish first)
1. **Text editing engine** – `core/fonts.py`, `core/text_edit.py`, `core/insert.py` + tests.
   The agent was last adding advance-width verification for embedded fonts in `resolve_font`.
   Check against the CLAUDE.md contract and the requirements (same font/size/color/baseline,
   Danish glyph fallback, rotated pages, images/vector art preserved, undo).
2. **UI shell** – `main.py`, `ui/app.py`, `actions.py`, `main_window.py`, `workspace.py`,
   `home_view.py`, `theme/*`, `viewer/page_view.py`, `tools/{base,select,hand}.py`,
   `panels/{thumbnails,search_panel,outline}.py`, `widgets/*`, `tests/test_ui_shell.py`.
   The agent was in its screenshot self-check loop. Re-check light/dark screenshots.

## Next phases
3. Edit tools in UI: inline "Rediger tekst" overlay (uses `text_edit`), "Tilføj tekst", "Tilføj billede",
   properties inspector (font/size/color).
4. Comment tools (highlight/underline/strike, note, ink, shapes) + dialogs:
   `dialogs/merge_dialog.py` (`MergeDialog`, `.result_bytes`), `images_dialog.py` (`ImagesToPdfDialog`, incl. clipboard paste),
   `split_dialog.py` (`SplitDialog(parent, pdf)`), page organiser.
5. QA loop: tests + screenshots + fixes until polished; README (Danish) with Windows install/build guide.
