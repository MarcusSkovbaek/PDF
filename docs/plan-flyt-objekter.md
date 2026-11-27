# Implementation plan – move text blocks and graphics ("Markér objekt")

Binding developer plan for the approved spec `docs/kravspec-flyt-objekter.md` (O1–O17). Written by the
orchestrator; sub-agents implement one work package (WP) each. **Read `CLAUDE.md` first** – its rules are
binding (offline, `pdfstudio.core` Qt-free, every mutation inside `pdf.edit(...)`, display-space coordinates,
Danish strings via `tr()` in `pdfstudio/i18n/da_<area>.py`, no new dependencies).

## Decisions (orchestrator)

* Three object kinds on a page: **image** (exists: `core/objects.py`), **text** (new: `core/textobjects.py`),
  **graphic** = vector paths in the page content (new: `core/graphics.py`). A facade `core/pageobjects.py`
  combines them for the UI (WP-C). The tool `ui/viewer/tools/edit_object.py` is generalised (WP-D1/D2).
* Graphics are edited **in the content stream**, like images today: a move inserts/rewrites ONE `cm` at the
  start of the outermost `q … Q` block that draws only that path, or wraps the path's own operators
  (construction ops + paint op) in `q <cm> … Q` once; later moves rewrite that same `cm` (the stream does not
  grow). Nothing else in the stream is touched (O13). Scaling keeps the stroke width (O8) by **rewriting the
  path's coordinates**, not with a scaling `cm`.
* Text blocks are moved with the existing text engine: remove their lines (`text_edit._remove_lines`) and
  write them again glyph for glyph (`text_edit._moved_runs` + `write_runs`), translated (O13). A moved
  block becomes **standalone** (O17): its raw rect is stored in the page's Folio markers as `/Free`, and the
  flow analysis turns blocks whose first-line origin lies in a `/Free` rect into `kind="single"`,
  `role="free"` stories (and obstacles for the column) – the column never pulls them back in.
* O5 applies to **graphics**: page backgrounds, repeated header/footer graphics (template) and table grid
  lines are not picked by a plain click (Alt+click does). Text headers/footers ARE selectable (O1).
* Undo: every public mutating function opens ONE `pdf.edit(label, CONTENT, (page,), ...)`. The facade
  composes several of them inside an outer `pdf.edit` (nested edits join the outer step – see
  `PdfDocument.edit`) so a multi-object action is one undo step. `merge=True` (keyboard nudge bursts) folds
  into the previous step with the same label (pattern: `objects.transform_image`).
* Identity after an edit: objects carry a `key`; `find_again(doc, page, obj)` relocates them. Graphic path
  indices do not change when only `cm`s/state ops are inserted.
* Copy/paste (O10, WP-C): the clipboard is a **one-page PDF** made from a throw-away copy of the source page
  with everything except the selection removed; paste = `show_pdf_page` onto the target page, then the
  resulting Form XObject is **flattened** into the page content (resource names merged/renamed), so pasted
  text/graphics/images are first-class page content again. Works within and across documents.
* Strings: each WP owns its own file – WP-A `pdfstudio/i18n/da_graphics.py` (keys `graphics.*`), WP-B
  `da_textobj.py` (`textobj.*`), WP-C `da_pageobj.py` (`pageobj.*`); WP-D edits `da_objects.py`.
  Core labels use the `_label(key, default)` pattern of `core/objects.py`.
* Rotated pages (/Rotate 90/180/270) and rotated text must work and be tested (O15).
* Performance (O16): listing objects of a dense page < 50 ms after the first time (cache per page content,
  e.g. keyed by `fonts._page_key(page)` like `text_edit.text_blocks`); a move/release < 0.5 s.

## WP-A – `core/graphics.py` (+ additions in `core/objects.py`)

Content-stream scanner for vector paths (reuse tokenizer pieces and helpers of `core/objects.py`:
`_skip_string`, `_Block`/`_Container` ideas, `_paints`, `_splice`, `_commit`, `_write_page_contents`,
`_page_matrix`, `_inv`, `_det`, `_fmt`; extend `objects.py` only additively and keep every existing test
green). Track per path paint: container, start of its first construction operand, paint operator span, CTM,
innermost `q…Q` block, the paint operator (`S s f F f* B B* b b*`; `n` = no paint → not an object; a path
with `W`/`W*` is a clip → not editable), the graphics state in effect as raw operator bytes per category
(`w J j M d ri i gs CS cs SC SCN sc scn G g RG rg K k`, saved/restored with `q`/`Q`), parsed points
(user space) → display bbox (stroke width included) and flattened polylines for hit-testing. Text
(`BT…ET`), images and form invocations between two paths break grouping. Paths inside a Form XObject: one
object per form invocation if the form draws only paths (edited by transforming the invocation, like a
collapsible image form); paths in a form that also draws text/images are listed `editable=False`. Shading
`sh` is not an object.

```python
KEEP = object()                       # "leave unchanged" sentinel for set_graphic_style
@dataclass
class GraphicObject:
    page: int
    index: int                        # group number on the page (drawing order of its first path)
    paths: tuple[int, ...]            # member path numbers (page drawing order, incl. non-selectable ones)
    rect: pymupdf.Rect                # display bbox incl. stroke
    kind: str                         # "shape" | "line" | "background" | "template" | "table"
    stroke: tuple | None              # (r, g, b) 0..1 of the first stroked member, None = no stroke
    fill: tuple | None
    width: float                      # display stroke width (pt) of the first stroked member, 0 if none
    selectable: bool                  # False for background/template/table (O5) – Alt picks them
    editable: bool                    # False e.g. for clip paths or paths in mixed forms
    in_form: bool
    items: list                       # display-space drawing items like page.get_drawings(): ("l", p1, p2),
                                      # ("c", p1, p2, p3, p4), ("re", rect), ("qu", quad) – for UI outlines
    key: tuple                        # (page, paths, rounded rect) – relocate with find_graphic()
def list_graphics(doc, page, *, all_items=False) -> list[GraphicObject]   # all_items: incl. non-selectable;
                                      # with all_items each background/template/table path is its own object
def graphic_at(doc, page, point, *, alt=False, tolerance=2.0) -> GraphicObject | None   # topmost first;
                                      # filled: inside; stroke-only: distance to the polyline <= w/2 + tol
def graphics_in(doc, page, rect, *, alt=False) -> list[GraphicObject]     # fully inside rect
def find_graphic(doc, page, obj) -> GraphicObject | None
def move_graphics(pdf, page, objs, dx, dy, *, merge=False) -> list[GraphicObject]
def transform_graphics(pdf, page, objs, matrix, *, keep_stroke=True, label=None, merge=False) -> list[GraphicObject]
def scale_graphics(pdf, page, objs, old_rect, new_rect) -> list[GraphicObject]   # stroke width kept (O8)
def delete_graphics(pdf, page, objs) -> None
def delete_graphics_doc(doc, page, objs) -> None          # same without undo (throw-away docs)
def set_graphic_style(pdf, page, objs, *, stroke=KEEP, fill=KEEP, width=None) -> list[GraphicObject]  # O11
def restack_graphics(pdf, page, objs, *, front=True) -> list[GraphicObject]                         # O12
def render_without_graphics(doc, page, objs, zoom, *, clip=None) -> pymupdf.Pixmap
def strip_page_except(doc, page, *, keep_paths=(), keep_images=()) -> None   # for the clipboard (WP-C):
                                      # removes every other path (any kind), `sh`, image placement (by
                                      # ImageObject.index) and form invocation; text is not touched
# in core/objects.py (WP-A too):
def restack_image(pdf, page, target, *, front=True) -> ImageObject
```

Classification (O5): *background* = filled path whose bbox covers >= 50 % of the page area; *template* =
path in the top/bottom 11 % band whose geometry (bbox within 0.5 pt, same paint) also exists on the
previous or next page, or a path inside a Form XObject that is invoked on >= 2 pages; *table* = thin rules
(lines, or filled rects with min(w, h) <= 2 pt) forming a grid (>= 2 horizontal and >= 2 vertical rules that
meet/cross within 1.5 pt) plus filled cell backgrounds inside that grid. Grouping (O2): consecutive path
paints (no text/image/form paint in between, same container) of selectable kinds whose display bboxes,
grown by 1 pt, intersect – plus fill/stroke pairs of identical geometry – form one group.
Style (O11): insert `RG`/`rg`/`w` right before the path's construction ops inside its exclusive block (or a
new `q … Q` wrap); change the paint operator when a fill/stroke is added or removed (`S`→`B`, `s`→`b`,
`f`→`B`, `B`→`S`/`f`, …); width in user space = display width / sqrt(|det(CTM·page matrix)|). Restack
(O12): only paths/images of the page container; remove and re-emit as `q <state ops> <ctm> cm <ops> Q` at
the end (front) or start (back) of the page content (`page.wrap_contents()` first if q/Q are unbalanced).

Tests `tests/test_graphics.py`: hand-written content streams (like `tests/test_objects.py.build`) and
PyMuPDF `Shape` drawings: listing, grouping, classification, hit-testing (fill vs stroke), move (byte-level:
only a `cm` added; pixel-identical elsewhere: render before/after and compare everything outside the moved
area), repeated moves do not grow the stream, scale keeps stroke width, delete, style, restack, rotated pages
(90/180/270), cropbox offset, forms, clip paths not editable, undo = exact restore, `strip_page_except`.

## WP-B – `core/textobjects.py` (+ `/Free` markers in `core/flow.py`, small hooks in `core/text_edit.py`)

```python
@dataclass
class TextObject:
    page: int
    rect: pymupdf.Rect                # display bbox incl. a list item's bullet
    lines: list                       # text_edit.TextLine of the block (+ bullet lines)
    text: str
    story_kind: str                   # "flow" | "single"
    role: str                         # flow.Story.role ("body", "header", "free", ...)
    free: bool                        # standalone after a move (O17)
    key: tuple                        # (page, rounded raw origin of the first line, text hash)
def list_text(doc, page) -> list[TextObject]          # one per flow piece (paragraph, with its bullet) and
                                                      # one per single story (header, cell, caption, free…)
def text_at(doc, page, point, *, tolerance=1.0) -> TextObject | None   # point on a line box of the block
def text_in(doc, page, rect) -> list[TextObject]      # fully inside rect
def find_text(doc, page, obj) -> TextObject | None
def move_text(pdf, page, objs, dx, dy, *, merge=False) -> list[TextObject]   # O6/O13/O17
def delete_text(pdf, page, objs) -> None
def delete_text_doc(doc, page, objs) -> None          # no undo (throw-away docs)
def strip_text_except(doc, page, keep) -> None        # remove every text line not in `keep` (clipboard)
def set_text_width(pdf, page, obj, width) -> TextObject                       # O9: start edge kept, re-wrapped
def restack_text(pdf, page, objs, *, front=True) -> list[TextObject]          # O12
```

* Move: `te._moved_runs(doc[page], page, lines, 0.0)` and translate every run origin by the raw delta
  (display delta through the page's derotation, rotation part only); `te._remove_lines` + `te.write_runs`.
  Underline/strike strokes move with the text. Rotated text and rotated pages must work.
* `/Free` markers: `flow._set_markers` must keep a `/Free [x0 y0 x1 y1 …]` array (raw rects, like
  `/Running`); add helpers to read/write it. Moving a block replaces its old free rect with the new one;
  deleting removes it. `flow._analyze_page`: a block whose first-line raw origin lies inside a free rect gets
  `role="free"` (single story, obstacle for the column). A moved piece loses its own `/Cont` marker, and if it
  continued on the next page (`cont_next`) that page's continuation marker for it is removed. BØR: a free
  block is never merged into a neighbouring paragraph by `text_edit.text_blocks` (check; fix only if safe).
* `te.write_runs(page, runs, *, overlay=True)` – pass `overlay` to `TextWriter.write_text` (send to back).
* O9 width: add an optional wrap-width override to the text engine (e.g. `te._plan(..., width=None)` and
  `replace_block_text(..., width=None)`) that re-wraps the paragraph at the new width, start edge kept; all
  existing text tests must stay green.

Tests `tests/test_textobjects.py`: listing per piece/single, hit-test, move (text identical and searchable,
fonts kept, nothing else on the page changes – pixel compare outside the old and new box), move a list item
(bullet follows), header/caption/cell, rotated page and rotated text, undo exact, free marker → the moved
block is a `free` single story and a later `flow.apply_story` on the column neither moves it nor pulls it in,
delete, width change (more lines, same style), restack back/front.

## WP-C – facade `core/pageobjects.py` + clipboard (after A and B)

```python
@dataclass
class PageObject:
    kind: str                         # "image" | "text" | "graphic"
    page: int
    rect: pymupdf.Rect
    ref: object                       # ImageObject | TextObject | GraphicObject
    key: tuple
def page_objects(doc, page, *, alt=False) -> list[PageObject]
def object_at(doc, page, point, *, alt=False) -> PageObject | None   # text glyph boxes first, then topmost
def objects_in(doc, page, rect, *, alt=False) -> list[PageObject]
def contents_of(doc, page, box: PageObject) -> list[PageObject]      # O4: objects inside a box graphic
def find_again(doc, page, obj) -> PageObject | None
def move_objects(pdf, page, objs, dx, dy, *, merge=False) -> list[PageObject]   # one undo step
def delete_objects(pdf, page, objs) -> None
def scale_objects(pdf, page, objs, old_rect, new_rect) -> list[PageObject]      # graphics + images only
def set_text_width(pdf, page, obj, width) -> PageObject
def set_style(pdf, page, objs, *, stroke=KEEP, fill=KEEP, width=None) -> list[PageObject]
def restack(pdf, page, objs, *, front=True) -> list[PageObject]
def render_without(doc, page, objs, zoom, *, clip=None) -> pymupdf.Pixmap     # the page behind (drag)
def render_only(doc, page, objs, zoom) -> pymupdf.Pixmap                      # the selection alone (alpha)
@dataclass
class Clip: data: bytes; rect: pymupdf.Rect; page_size: tuple[float, float]
def copy_objects(doc, page, objs) -> Clip
def paste_clip(pdf, page, clip, *, offset=(0.0, 0.0)) -> list[PageObject]      # same place (+ offset)
def duplicate_objects(pdf, page, objs, offset=(10.0, 10.0)) -> list[PageObject]
```

## WP-D1 / WP-D2 – UI (after C)

D1: `EditObjectTool` works on `PageObject`s: hover outline for text/graphics (images as today), click,
Shift/Ctrl+click, rubber band on empty space, Alt+click (O5), drag with lifted preview (`render_without` +
translucent `render_only`), snapping guides to other objects' edges/centres and the page centre/margins
(Alt disables) (O7), arrow nudges (burst = one undo step), Delete, Ctrl+C/Ctrl+V/Ctrl+D (in-app clipboard,
also across tabs), Esc, Tab cycling, double-click on text opens *Redigér tekst* there.
D2: inspector panels for text/graphic/mixed selections (X/Y mm, B/H for graphics, stroke/fill colour, width,
"Læg forrest/bagerst", "Medtag indhold" for boxes – default on (O4), "Redigér tekst"), corner handles that
scale graphics (O8; Shift free), side handles for a single text block's width (O9).

## WP-Z – real-world corpus (parallel to A/B)

`tests/corpus/make_corpus.py` (manual tool, not collected by pytest) writes LibreOffice flat-ODT sources and
converts them with `soffice --headless --convert-to pdf` into a folder given on the command line: a ~55-page
Danish annual report (headings, justified paragraphs with bold/italic/coloured words, bullet and numbered
lists, bordered tables with zebra fills, a chart image, header text + rule + logo, footer "Side N af M",
callout boxes with text, drawn rectangles/ellipses/lines/arrows, a coloured cover page, chapters on new
pages, a two-column section) and a ~14-page landscape strategy deck (title bars, coloured boxes with text,
shapes, arrows, images).

## WP-E – stress, docs, release (orchestrator)

`tests/stress_objects.py`: on every page of the corpus, move every selectable object by (+30, +20) pt and back,
check the page renders identically afterwards (tolerance for anti-aliasing), text still searchable, undo
exact; timings. Then README/STATUS/CLAUDE.md, Windows CI.
