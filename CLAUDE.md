# Folio PDF – architecture & contracts

Offline desktop PDF editor (Acrobat-Pro-like) for **Windows**, written in
Python 3.11+ with **PySide6** (UI) and **PyMuPDF** (`import pymupdf`, never
`fitz`) as engine. UI language: **Danish**.

## Non-negotiables
1. **100% offline.** Never import `QtNetwork`, `QtWebEngine*`, `urllib`,
   `http`, `requests`, `ssl` or anything that phones home. No update checks,
   no telemetry, no web fonts, no CDN. `pdfstudio.security.netguard.install()`
   is called first thing at startup and in tests; it must keep passing.
   Files are only read/written where the user chooses (plus Qt's local
   `QSettings` for preferences/recent files).
2. **Premium UI/UX.** Calm, modern, consistent (think Linear / Figma / Arc).
   Use only theme tokens from `pdfstudio/ui/theme/tokens.py` – no hard-coded
   colors in widgets. Icons only via `pdfstudio.ui.theme.icons.icon(name)`.
3. **`pdfstudio.core` is Qt-free** and fully unit-tested. UI never calls
   PyMuPDF for mutations directly – it calls a `core` function.
4. Every mutation runs inside `with pdf.edit("Dansk label", kind, pages):`
   (undoable, notifies observers). Never cache `pdf.doc` or page objects
   across edits (undo replaces the document object).
5. All user-visible strings through `pdfstudio.i18n.tr("area.key")`; put the
   Danish text in `pdfstudio/i18n/da_<area>.py` (`STRINGS = {...}`). Each
   area owns its own file → no edit conflicts.

## Coordinates (important)
Core APIs take/return coordinates in **display space**: PDF points, origin at
the top-left of the page *as rendered with its /Rotate applied*, i.e. the
space of `page.rect` and of a pixmap from `page.get_pixmap(matrix=Matrix(z, z))`
divided by `z`. Core functions convert internally with
`page.derotation_matrix` / `page.rotation_matrix` where PyMuPDF expects
unrotated coordinates (annotations, insert_text, redactions, get_text).
Rotated pages must be covered by tests.

Colors in core are `(r, g, b)` floats 0..1.

## Layout
```
main.py                       entry point (python main.py [file.pdf ...])
pdfstudio/
  security/netguard.py        network kill switch
  i18n/                       tr(), da.py + da_<area>.py string tables
  core/                       Qt-free engine
    document.py               PdfDocument (edit/undo/redo/save/listen)  [done]
    history.py                snapshot undo                            [done]
    render.py                 rasterising pages
    pages.py                  page organisation, split, extract
    merge.py                  combine files
    images.py                 images (screenshots) -> PDF
    search.py                 text search
    annotations.py            markup, notes, ink, shapes; list/update/delete
    fonts.py                  font resolution (embedded -> system -> base14)
    text_edit.py              edit existing text preserving its format
    insert.py                 new text boxes / images in document style
  ui/
    app.py                    QApplication setup, theme, run()
    actions.py                ActionRegistry (id -> QAction, palette)
    main_window.py            window, tabs, home screen, menus
    theme/ tokens.py qss.py icons.py
    viewer/ page_view.py      continuous page view (QGraphicsView)
            tools/base.py     Tool interface + ToolRegistry
            tools/*.py        one module per tool
    panels/                   thumbnails, properties inspector, search, outline
    dialogs/                  merge, images->pdf, split, password, ...
    widgets/                  toast, color button, segmented control, ...
tests/                        pytest; core tests + offscreen UI smoke tests
packaging/                    PyInstaller spec + Windows build scripts
```

## Core API contract (signatures are binding – UI codes against them)
```python
# render.py
def render_page(doc, index, zoom, *, annots=True, clip=None, alpha=False) -> pymupdf.Pixmap
def page_display_size(doc, index) -> tuple[float, float]          # page.rect w,h

# pages.py  (pdf = PdfDocument; all mutations inside pdf.edit(..., STRUCTURE))
def parse_page_ranges(text: str, page_count: int) -> list[int]     # "1-3, 5, 8-" -> 0-based; ValueError on bad input
def rotate_pages(pdf, indices, degrees=90) -> None                 # relative, multiples of 90
def delete_pages(pdf, indices) -> None                             # refuses to delete all pages (ValueError)
def move_pages(pdf, indices, to_index) -> list[int]                # to_index = insert position in *original* numbering; returns new indices
def duplicate_pages(pdf, indices) -> list[int]
def insert_blank_page(pdf, at, width=None, height=None) -> int     # default size: neighbour page
def insert_pdf(pdf, source: str | bytes, at, page_indices=None, password=None) -> list[int]  # images allowed as source
def extract_pages(pdf, indices) -> bytes                           # new PDF bytes, doc unchanged
def split_document(pdf, *, every_n=None, ranges=None) -> list[tuple[str, bytes]]  # (suggested filename, bytes)

# merge.py
@dataclass class MergeSource: path: str; page_indices: list[int] | None = None; password: str | None = None
def merge_files(sources: list[MergeSource], *, add_bookmarks=True) -> bytes   # PDFs and images

# images.py
@dataclass class ImagePageOptions: page_size: str = "fit"  # "fit"|"A4"|"A3"|"A5"|"Letter"|"Legal"
                                   orientation: str = "auto"  # "auto"|"portrait"|"landscape"
                                   margin_mm: float = 0.0
                                   scale: str = "contain"     # "contain"|"cover"|"stretch"
def images_to_pdf(images: list[str | bytes], options=ImagePageOptions()) -> bytes
SUPPORTED_IMAGE_EXTENSIONS: tuple[str, ...]

# search.py
@dataclass class SearchHit: page: int; rects: list[pymupdf.Rect]; context: str   # display coords
def search(doc, query, *, case_sensitive=False, whole_words=False) -> Iterator[SearchHit]

# annotations.py  (pages kind CONTENT)
MARKUP_KINDS = ("highlight", "underline", "strikeout", "squiggly")
SHAPE_KINDS = ("rect", "ellipse", "line", "arrow")
@dataclass class AnnotInfo: xref: int; kind: str; rect: pymupdf.Rect; color; fill; opacity: float; width: float; contents: str
def add_text_markup(pdf, page, kind, rect, color, opacity=1.0) -> int | None   # marks words inside rect; xref or None if no words
def add_note(pdf, page, point, text, color) -> int
def add_ink(pdf, page, strokes, color, width=2.0, opacity=1.0) -> int          # strokes: list[list[(x, y)]]
def add_shape(pdf, page, kind, p1, p2, stroke, fill=None, width=2.0, opacity=1.0) -> int
def list_annotations(doc, page) -> list[AnnotInfo]
def annotation_at(doc, page, point) -> AnnotInfo | None
def update_annotation(pdf, page, xref, **props) -> None   # color, fill, opacity, width, contents, rect
def delete_annotation(pdf, page, xref) -> None

# fonts.py
@dataclass class SpanStyle: font: str; size: float; color: tuple; bold: bool; italic: bool; serif: bool; mono: bool
@dataclass class FontChoice: font: pymupdf.Font; name: str; source: str   # "embedded"|"system"|"builtin"
def resolve_font(doc, page_index, style: SpanStyle, text: str) -> FontChoice   # must cover all glyphs of text
def available_families() -> list[str]                    # for the font picker (base14 + local system fonts)
def qt_family_hint(style: SpanStyle) -> str              # good on-screen family for the inline editor

# text_edit.py
@dataclass class TextLine: text: str; bbox: pymupdf.Rect; origin: tuple[float, float]; style: SpanStyle
@dataclass class TextBlock: page: int; bbox: pymupdf.Rect; lines: list[TextLine]; style: SpanStyle  # display coords
    text -> str (lines joined by "\n")
def text_blocks(doc, page) -> list[TextBlock]
def block_at(doc, page, point) -> TextBlock | None
def replace_block_text(pdf, block, new_text, style: SpanStyle | None = None) -> None   # removes old text only (keeps images/vector art), writes new text in the same font/size/color at the same positions

# insert.py
def document_text_style(doc) -> SpanStyle                # dominant body style – default for new text
def add_text_box(pdf, page, rect, text, style: SpanStyle, align="left") -> None
def add_image(pdf, page, rect, image: str | bytes, keep_aspect=True) -> None
```

## UI contract
* `ui/actions.py` – `ActionRegistry.register(id, text, *, icon=None, shortcut=None, handler, enabled=lambda: True, checkable=False)`,
  `.action(id) -> QAction`, `.all()`, `.refresh_enabled()`. Menus, toolbars and
  the command palette (Ctrl+K) are built from it.
* `ui/viewer/tools/base.py` – `class Tool(QObject)`: `id, label, icon, shortcut, cursor`,
  `activate(ctx)`, `deactivate()`, `mouse_press/move/release(ev: ToolMouseEvent)`,
  `key_press(QKeyEvent) -> bool`, `properties_widget() -> QWidget | None`.
  `ToolMouseEvent` carries `page` (index or -1), `pos` (display-space point),
  `scene_pos`, `buttons`, `modifiers`. `ctx` = `ToolContext` (pdf, view,
  workspace, toast). `ToolRegistry.register(cls)` – tools are discovered by id.
* `ui/viewer/page_view.py` – `PageView(QGraphicsView)` for one `PdfDocument`:
  `page_at(scene_pos) -> (index, display_point)`, `page_to_scene(index, point)`,
  `page_scene_rect(index)`, `zoom`/`set_zoom`, `fit_width()`, `fit_page()`,
  `go_to_page(i)`, `current_page`, `refresh(pages=None)`,
  `add_overlay(index, item)` (item in display coords, auto-scaled),
  `set_search_hits(hits)`, signals `currentPageChanged(int)`, `zoomChanged(float)`.
  Renders lazily (only visible pages) with an LRU pixmap cache, crisp on HiDPI.
* `DocumentWorkspace` (one per tab) owns the `PdfDocument`, `PageView`, side
  panels, current tool, and subscribes to `pdf.listen` to refresh.

## Design language
Neutral graphite/slate surfaces, one accent (indigo `#5B5BD6`), 8 px grid,
radii 6/10/14, subtle 1 px borders, soft shadows on floating elements, 150 ms
ease-out transitions, generous whitespace. Typography: "Segoe UI Variable",
"Segoe UI", "Inter", sans-serif; 13 px base. Light and dark theme, following
the OS by default. Icons: Phosphor via qtawesome (`ph.` names).

## Commands
```
pip install -r requirements.txt -r requirements-dev.txt
python -m pytest                       # all tests (QT_QPA_PLATFORM=offscreen is set in conftest)
QT_QPA_PLATFORM=offscreen python main.py --screenshot out.png [file.pdf]   # UI self-check
```
