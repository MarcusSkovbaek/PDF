# Real-world test corpus

`make_corpus.py` builds realistic office-made PDFs for manual tests, the object-move feature and
the stress test (`tests/stress_objects.py`). It writes LibreOffice flat-XML sources and converts
them with a headless LibreOffice, so the PDFs have real line breaking, subset fonts, tables,
images and lots of **vector graphics**. It is a manual tool: pytest does not collect it.

| File | Contents |
|------|----------|
| `aarsrapport.pdf` | ~58-page Danish annual report, A4 portrait (Writer). Cover with a full-page coloured background rectangle, circles and a rule; table of contents (page references are fields); 10 chapters, each starting on a new page (Heading 1), with sections (Heading 2); justified body text with bold, italic and coloured words; bullet and numbered lists; 14 bordered tables (filled header row, zebra rows, right-aligned numbers, total rows); 2 bar-chart images with captions "Figur N: ..."; header on every page except the cover ("Novo Grøn A/S · Årsrapport 2026" + logo + chapter name + rule under the header); footer "Side N af M" (fields); 30 fact boxes (text frames with fill and border: full width or floated right); drawn shapes (rectangles with text, ellipses, lines, arrows with arrow heads), a 3-box process diagram with arrows and a feedback loop, a 3x3 risk matrix, KPI circles, a two-column section with a column rule, a heading on a coloured full-width band. |
| `strategi.pdf` | 14-page landscape (16:9) strategy deck (Impress). Title bar with title on every slide, coloured boxes with text, circles, lines/arrows, connectors (org chart), chevron process diagram, timeline, SWOT, Gantt-style roadmap, images (chart, "photo", logo), footer "Side N af 14". |

Regenerate (LibreOffice `soffice` must be on PATH; takes a few seconds, no network):

```
python tests/corpus/make_corpus.py [OUTPUT_DIR] [--keep-sources] [--preview PNG_DIR]
```

The default output folder is the session scratchpad
(`/tmp/claude-0/-home-user-PDF/740062b8-3b33-5ee1-bf87-7d6301ab4318/scratchpad/corpus/`).
The script is deterministic (fixed seed and Danish vocabulary: the same sources every time),
checks the result with PyMuPDF and prints a summary (pages, drawings, images, words, text blocks).

The generated PDFs and the `.fodt`/`.fodp` sources are **not committed**: regenerate them.

LibreOffice 24.2 notes (worked around in the script): a full-width wrap-none text frame anchored
on a long paragraph can hang the layout, so those boxes get their own empty anchor paragraph;
text inside drawn shapes only honours *automatic* styles; in Impress only custom shapes wrap
their text and need `draw:auto-grow-height="false"` to keep their size.
