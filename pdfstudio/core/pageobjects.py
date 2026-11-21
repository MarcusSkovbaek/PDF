"""Everything "Markér objekt" can select on a page, as one kind of object –
and the object clipboard.

A page holds three kinds of objects, each with its own engine:

* **image** – an image placement (:mod:`pdfstudio.core.objects`),
* **text** – a paragraph / standalone text block (:mod:`pdfstudio.core.textobjects`),
* **graphic** – painted vector paths (:mod:`pdfstudio.core.graphics`).

This facade lists and hit-tests them together (:class:`PageObject`) and
applies one action to a mixed selection as ONE undo step: it opens one
``pdf.edit`` and calls the engines' public functions inside it (nested
edits join the outer step). Graphics and images are edited first (they only
insert or rewrite ``cm`` operators and blocks), text last (removing glyphs
rewrites the content stream). Afterwards every object is found again
(:func:`find_again`) and returned in input order.

Clipboard (O10): :func:`copy_objects` makes a one-page PDF from a throw-away
copy of the page with everything but the selection removed. :func:`paste_clip`
places that page on the target page with ``Page.show_pdf_page`` (which also
copies fonts, images etc. into another document) so the objects appear at
the clip's rect plus an offset in the target's display space – whatever the
page sizes or /Rotate – and then *flattens* the Form XObject this created
into the page's own content: its resources are merged into the page's
(colliding names renamed), the resource names in its content are rewritten
and the content is appended as ``q <matrix> cm … Q``. Pasted text, graphics
and images are therefore ordinary page content again – listable, movable,
editable. Should flattening fail, the (correctly placed) form is kept.

Coordinates are display space (see CLAUDE.md).
"""

from __future__ import annotations

import dataclasses
import math
import re
from collections import Counter
from dataclasses import dataclass

import pymupdf

from . import graphics as gr
from . import objects as ob
from . import textobjects as tob
from .document import CONTENT, write_bytes
from .graphics import KEEP
from .render import as_document, render_page

__all__ = [
    "KEEP",
    "PageObject",
    "Clip",
    "page_objects",
    "object_at",
    "objects_in",
    "contents_of",
    "find_again",
    "move_objects",
    "delete_objects",
    "scale_objects",
    "set_text_width",
    "set_style",
    "restack",
    "render_without",
    "render_only",
    "copy_objects",
    "paste_clip",
    "duplicate_objects",
]

KINDS = ("image", "text", "graphic")
#: hit tolerance (pt) for strokes, as :func:`graphics.graphic_at`
_TOL = 2.0
#: how far (pt) an object may stick out of a rect and still lie "inside" it
_INSIDE = 0.5


def _label(key: str, default: str) -> str:
    try:
        from ..i18n import tr

        text = tr(key)
        return default if text == key else text
    except Exception:  # pragma: no cover - i18n is optional for core
        return default


# ---------------------------------------------------------------------------
# data
# ---------------------------------------------------------------------------
@dataclass
class PageObject:
    """One selectable object of a page (display coordinates)."""

    kind: str                         # "image" | "text" | "graphic"
    page: int
    rect: pymupdf.Rect
    ref: object                       # ImageObject | TextObject | GraphicObject
    key: tuple                        # (kind,) + ref.key


@dataclass
class Clip:
    """Objects on the clipboard: a one-page PDF holding only them."""

    data: bytes                       # the PDF
    rect: pymupdf.Rect                # union of the objects' display rects on their page
    page_size: tuple[float, float]    # display size of that page


def _kind_of(ref) -> str:
    if isinstance(ref, ob.ImageObject):
        return "image"
    if isinstance(ref, tob.TextObject):
        return "text"
    if isinstance(ref, gr.GraphicObject):
        return "graphic"
    raise TypeError(f"not a page object: {type(ref).__name__}")


def _wrap(ref, kind: str | None = None) -> PageObject:
    kind = kind or _kind_of(ref)
    return PageObject(kind=kind, page=ref.page, rect=pymupdf.Rect(ref.rect), ref=ref, key=(kind,) + tuple(ref.key))


def _as_po(obj) -> PageObject:
    return obj if isinstance(obj, PageObject) else _wrap(obj)


def _reading(o: PageObject) -> tuple:
    r = o.rect
    return (round(r.y0, 1), round(r.x0, 1))


def _area(r) -> float:
    return abs(r.width * r.height)


def _inside(r, box, tol: float = _INSIDE) -> bool:
    return r.x0 >= box.x0 - tol and r.y0 >= box.y0 - tol and r.x1 <= box.x1 + tol and r.y1 <= box.y1 + tol


# ---------------------------------------------------------------------------
# listing and hit-testing
# ---------------------------------------------------------------------------
def _per_content(d: pymupdf.Document, page: int):
    """``(cache dict, key)`` for facts about the page's current content
    (``key`` None: not cacheable)."""
    cache = getattr(d, "_folio_pageobj", None)
    if cache is None:
        cache = {}
        try:
            d._folio_pageobj = cache
        except Exception:
            pass
    try:
        key = (page, gr._scan_key(d, page))
    except Exception:
        key = None
    if len(cache) > 64:
        cache.clear()
    return cache, key


def _facts(d: pymupdf.Document, page: int) -> tuple[list, set]:
    """The page's editable image placements (:func:`objects.list_images`
    scans the content stream every time) and the path numbers of its text
    decorations – cached per page content."""
    cache, key = _per_content(d, page)
    if key is not None and key in cache:
        return cache[key]
    facts = ([i for i in ob.list_images(d, page) if i.editable], _decor_paths(d, page, tob.list_text(d, page)))
    if key is not None:
        cache[key] = facts
    return facts


def _images(d: pymupdf.Document, page: int) -> list:
    return _facts(d, page)[0]


def _page_decor(d: pymupdf.Document, page: int) -> set[int]:
    return _facts(d, page)[1]


def page_objects(doc, page: int, *, alt: bool = False) -> list[PageObject]:
    """The objects of a page in reading order (top to bottom, then left to
    right): editable images, text blocks and graphics. ``alt`` also lists
    backgrounds, template graphics and table rules (each such path on its
    own). The engines cache per page content, so repeated calls are cheap."""
    d = as_document(doc)
    if not (0 <= page < d.page_count):
        return []
    texts = tob.list_text(d, page)
    decor = _page_decor(d, page)
    out = [_wrap(t, "text") for t in texts]
    out += [_wrap(g, "graphic") for g in gr.list_graphics(d, page, all_items=alt) if _own_graphic(g, decor)]
    out += [_wrap(i, "image") for i in _images(d, page)]
    out.sort(key=_reading)
    return out


def _own_graphic(g, decor: set) -> bool:
    """An editable graphic that is not just the underline/strike stroke of a
    text (those belong to – and move with – their text)."""
    return g.editable and not (decor and set(g.paths) <= decor)


def _graphics_at(d: pymupdf.Document, page: int, x: float, y: float, alt: bool) -> list:
    decor = _page_decor(d, page)
    if alt:
        g = gr.graphic_at(d, page, (x, y), alt=True, tolerance=_TOL)
        return [g] if g is not None and _own_graphic(g, decor) else []
    lst = gr._listing(d, page)
    paths = lst.scan.paths
    out = []
    for o in gr._objects(d, page, lst, False):
        r = o.rect
        if not _own_graphic(o, decor) or not (r.x0 - _TOL <= x <= r.x1 + _TOL and r.y0 - _TOL <= y <= r.y1 + _TOL):
            continue
        if any(gr._path_hit(paths[n], x, y, _TOL) for n in o.paths):
            out.append(o)
    return out


def object_at(doc, page: int, point, *, alt: bool = False) -> PageObject | None:
    """The object under the display point: a text block when the point is on
    one of its line boxes, otherwise the most specific (smallest) graphic or
    image there. ``alt`` (Alt+click) also reaches backgrounds, template
    graphics and table rules and picks single paths out of groups."""
    d = as_document(doc)
    if not (0 <= page < d.page_count):
        return None
    t = tob.text_at(d, page, point)
    if t is not None:
        return _wrap(t, "text")
    pt = pymupdf.Point(point)
    cands = [_wrap(g, "graphic") for g in _graphics_at(d, page, pt.x, pt.y, alt)]
    cands += [_wrap(i, "image") for i in _images(d, page) if ob._contains(i, pt, 0.0)]
    if not cands:
        return None
    # the smallest wins; between equal areas the one listed last (drawn later)
    return min(reversed(cands), key=lambda o: _area(o.rect))


def objects_in(doc, page: int, rect, *, alt: bool = False) -> list[PageObject]:
    """The objects lying completely inside the display rect (rubber band)."""
    d = as_document(doc)
    if not (0 <= page < d.page_count):
        return []
    r = pymupdf.Rect(rect)
    r.normalize()
    decor = _page_decor(d, page)
    out = [_wrap(t, "text") for t in tob.text_in(d, page, r)]
    out += [_wrap(g, "graphic") for g in gr.graphics_in(d, page, r, alt=alt) if _own_graphic(g, decor)]
    out += [_wrap(i, "image") for i in _images(d, page) if _inside(i.rect, r)]
    out.sort(key=_reading)
    return out


def contents_of(doc, page: int, box: PageObject) -> list[PageObject]:
    """What a box graphic encloses (O4 "Medtag indhold"): the objects of any
    kind whose rect lies inside the box's rect – the box itself excluded."""
    box = _as_po(box)
    if box.kind != "graphic":
        return []
    return [o for o in page_objects(doc, page) if o.key != box.key and _inside(o.rect, box.rect)]


def find_again(doc, page: int, obj) -> PageObject | None:
    """``obj`` on the current document (after edits / undo), or None."""
    d = as_document(doc)
    if obj is None or not (0 <= page < d.page_count):
        return None
    po = _as_po(obj)
    try:
        if po.kind == "text":
            ref = tob.find_text(d, page, po.ref)
        elif po.kind == "graphic":
            ref = gr.find_graphic(d, page, po.ref)
        else:
            ref = ob._locate(d, page, po.ref)[1]
    except (LookupError, ValueError):
        ref = None
    return _wrap(ref, po.kind) if ref is not None else None


# ---------------------------------------------------------------------------
# edit helpers
# ---------------------------------------------------------------------------
class _Sel:
    """A selection resolved on the current document, split by kind."""

    def __init__(self, items: list[PageObject]) -> None:
        self.items = items
        self.texts = [o.ref for o in items if o.kind == "text"]
        self.graphics = [o.ref for o in items if o.kind == "graphic"]
        self.images = [o.ref for o in items if o.kind == "image"]

    def __bool__(self) -> bool:
        return bool(self.items)


def _resolve(doc, page: int, objs) -> _Sel:
    if isinstance(objs, (PageObject, ob.ImageObject, tob.TextObject, gr.GraphicObject)):
        objs = [objs]
    out: list[PageObject] = []
    seen: set = set()
    for o in objs or ():
        cur = find_again(doc, page, o)
        if cur is not None and cur.key not in seen:
            seen.add(cur.key)
            out.append(cur)
    return _Sel(out)


def _again(doc, page: int, refs) -> list[PageObject]:
    out = []
    for ref in refs:
        if ref is None:
            continue
        po = find_again(doc, page, ref)
        if po is not None:
            out.append(po)
    return out


def _match_text(results: list, wanted: list, rx: float = 0.0, ry: float = 0.0) -> list:
    """The engine's results (found objects, maybe fewer) for ``wanted``, in
    the order of ``wanted`` (``None`` where one is missing)."""
    if len(results) == len(wanted):
        return list(results)
    pool = list(results)
    out = []
    for w in wanted:
        x, y = w.origin
        same = [r for r in pool if r.key[2] == w.key[2]]
        if not same:
            out.append(None)
            continue
        best = min(same, key=lambda r: math.hypot(r.origin[0] - x - rx, r.origin[1] - y - ry))
        pool.remove(best)
        out.append(best)
    return out


def _step(pdf, label: str, page: int, merge: bool = False):
    """Context manager for one undo step (``merge``: fold it into the
    previous step when that has the same label – a burst of nudges)."""
    return _Step(pdf, label, page, merge)


class _Step:
    def __init__(self, pdf, label: str, page: int, merge: bool) -> None:
        self.pdf = pdf
        self.fold = merge and pdf.history.undo_label == label
        self.cm = pdf.edit(label, CONTENT, (page,))

    def __enter__(self):
        return self.cm.__enter__()

    def __exit__(self, *exc):
        res = self.cm.__exit__(*exc)
        if exc[0] is None and self.fold:
            self.pdf.history.discard_last()      # the state before the burst stays the undo target
        return res


# ---------------------------------------------------------------------------
# edits
# ---------------------------------------------------------------------------
def move_objects(pdf, page: int, objs, dx: float, dy: float, *, merge: bool = False) -> list[PageObject]:
    """Move objects by ``(dx, dy)`` display points – one undo step
    (``merge=True``: a burst of keyboard nudges is one step). When text is
    selected the displacement is shortened so the text stays on the page
    (text outside it would be lost); everything moves by the same amount.
    Moved text becomes standalone (O17). Returns the objects as they are now."""
    if not (math.isfinite(dx) and math.isfinite(dy)):
        raise ValueError("bad offset")
    sel = _resolve(pdf.doc, page, objs)
    if not sel or (abs(dx) < 1e-6 and abs(dy) < 1e-6):
        return sel.items
    pg = pdf.doc[page]
    if sel.texts:
        dx, dy = tob._keep_on_page(pg, sel.texts, dx, dy)
        if abs(dx) < 1e-6 and abs(dy) < 1e-6:
            return sel.items
    new: dict[int, object] = {}
    with _step(pdf, _label("pageobj.undo.move", "Flyt"), page, merge):
        if sel.graphics:
            for g, n in zip(sel.graphics, gr.move_graphics(pdf, page, sel.graphics, dx, dy)):
                new[id(g)] = n
        for img in sel.images:
            new[id(img)] = ob.move_image(pdf, page, img, dx, dy)
        if sel.texts:
            rx, ry = tob._raw_delta(pg, dx, dy)
            moved = tob.move_text(pdf, page, sel.texts, dx, dy)
            for t, n in zip(sel.texts, _match_text(moved, sel.texts, rx, ry)):
                new[id(t)] = n
    return _again(pdf.doc, page, [_po_or_none(new.get(id(o.ref)), o.kind) for o in sel.items])


def _po_or_none(ref, kind: str) -> PageObject | None:
    return _wrap(ref, kind) if ref is not None else None


def delete_objects(pdf, page: int, objs) -> None:
    """Delete objects (one undo step): graphics, images (topmost first, so
    the placement numbers of the others stay valid), then text."""
    sel = _resolve(pdf.doc, page, objs)
    if not sel:
        return
    with _step(pdf, _label("pageobj.undo.delete", "Slet"), page):
        if sel.graphics:
            gr.delete_graphics(pdf, page, sel.graphics)
        for img in sorted(sel.images, key=lambda i: -i.index):
            ob.delete_image(pdf, page, img)
        if sel.texts:
            tob.delete_text(pdf, page, sel.texts)


def _scale_map(graphics: list, old: pymupdf.Rect, new: pymupdf.Rect) -> pymupdf.Matrix:
    """The display transform :func:`graphics.scale_graphics` applies for
    ``old -> new`` (it keeps half the stroke width out of the scaling)."""
    hw = max((g.width / 2 for g in graphics if g.stroke is not None), default=0.0)
    go, gn = pymupdf.Rect(old), pymupdf.Rect(new)
    if old.width > 2 * hw + 1e-6 and old.height > 2 * hw + 1e-6 and new.width > 2 * hw and new.height > 2 * hw:
        go = old + (hw, hw, -hw, -hw)
        gn = new + (hw, hw, -hw, -hw)
    sx, sy = gn.width / go.width, gn.height / go.height
    return (pymupdf.Matrix(1, 0, 0, 1, -go.x0, -go.y0) * pymupdf.Matrix(sx, 0, 0, sy, 0, 0)
            * pymupdf.Matrix(1, 0, 0, 1, gn.x0, gn.y0))


def scale_objects(pdf, page: int, objs, old_rect, new_rect) -> list[PageObject]:
    """Scale graphics and images so the selection's box ``old_rect`` becomes
    ``new_rect`` (display space; one undo step). Graphics keep their stroke
    width (O8); every image's rect is mapped by the same transform. Text
    cannot be scaled here (``ValueError``) – its width is changed with
    :func:`set_text_width`, its size in *Redigér tekst*."""
    old = pymupdf.Rect(old_rect)
    new = pymupdf.Rect(new_rect)
    old.normalize()
    new.normalize()
    if old.is_empty or new.is_empty or not all(math.isfinite(v) for v in list(old) + list(new)):
        raise ValueError("empty rect")
    sel = _resolve(pdf.doc, page, objs)
    if sel.texts:
        raise ValueError("text cannot be scaled")
    if not sel:
        return []
    m = _scale_map(sel.graphics, old, new)
    new_refs: dict[int, object] = {}
    with _step(pdf, _label("pageobj.undo.scale", "Skalér"), page):
        if sel.graphics:
            for g, n in zip(sel.graphics, gr.scale_graphics(pdf, page, sel.graphics, old, new)):
                new_refs[id(g)] = n
        for img in sel.images:
            r = pymupdf.Rect(img.rect.tl * m, img.rect.br * m)
            r.normalize()
            new_refs[id(img)] = ob.set_image_rect(pdf, page, img, r)
    return _again(pdf.doc, page, [_po_or_none(new_refs.get(id(o.ref)), o.kind) for o in sel.items])


def set_text_width(pdf, page: int, obj, width: float) -> PageObject:
    """Wrap a text object anew at ``width`` display points (O9; one undo
    step) – see :func:`textobjects.set_text_width`."""
    po = find_again(pdf.doc, page, obj)
    if po is None:
        raise LookupError("object not found")
    if po.kind != "text":
        raise ValueError("only text has a wrap width")
    with _step(pdf, _label("pageobj.undo.width", "Skift bredde"), page):
        t = tob.set_text_width(pdf, page, po.ref, width)
    again = find_again(pdf.doc, page, _wrap(t, "text"))
    if again is None:                                          # pragma: no cover - defensive
        raise LookupError("text object vanished")
    return again


def set_style(pdf, page: int, objs, *, stroke=KEEP, fill=KEEP, width: float | None = None) -> list[PageObject]:
    """Change stroke colour, fill colour (``None`` removes it) and/or stroke
    width of the selected graphics (O11; one undo step). Images and text in
    the selection are left as they are. Returns the selection re-found."""
    sel = _resolve(pdf.doc, page, objs)
    if not sel.graphics or (stroke is KEEP and fill is KEEP and width is None):
        return sel.items
    new_refs: dict[int, object] = {}
    with _step(pdf, _label("pageobj.undo.style", "Skift udseende"), page):
        styled = gr.set_graphic_style(pdf, page, sel.graphics, stroke=stroke, fill=fill, width=width)
        for g, n in zip(sel.graphics, styled):
            new_refs[id(g)] = n
    return _again(pdf.doc, page, [_po_or_none(new_refs.get(id(o.ref), o.ref), o.kind) for o in sel.items])


def _z(scan, ev) -> int:
    """Drawing position of an event in the page content (its outermost form
    invocation for content inside forms)."""
    while ev.cont.kind == "form" and ev.cont.parent_ev is not None:
        ev = ev.cont.parent_ev
    return ev.seq


def restack(pdf, page: int, objs, *, front: bool = True) -> list[PageObject]:
    """Bring objects to the front or send them to the back (O12; one undo
    step). Within the selection graphics end up below images and images
    below text (a box below its text, either way); each kind keeps its own
    relative order."""
    sel = _resolve(pdf.doc, page, objs)
    if not sel:
        return []
    scan = gr._page_scan(pdf.doc, page)
    imgs = sorted(sel.images, key=lambda i: _z(scan, scan.images[i.index]) if i.index < len(scan.images) else 0)
    new_refs: dict[int, object] = {}
    label = _label("pageobj.undo.front", "Læg forrest") if front else _label("pageobj.undo.back", "Læg bagerst")

    def do_graphics():
        if sel.graphics:
            for g, n in zip(sel.graphics, gr.restack_graphics(pdf, page, sel.graphics, front=front)):
                new_refs[id(g)] = n

    def do_images():
        for img in (imgs if front else reversed(imgs)):
            cur = find_again(pdf.doc, page, _wrap(img, "image"))
            if cur is None:
                raise LookupError("image placement not found")
            new_refs[id(img)] = ob.restack_image(pdf, page, cur.ref, front=front)

    def do_text():
        if sel.texts:
            done = tob.restack_text(pdf, page, sel.texts, front=front)
            for t, n in zip(sel.texts, _match_text(done, sel.texts)):
                new_refs[id(t)] = n

    with _step(pdf, label, page):
        steps = (do_graphics, do_images, do_text) if front else (do_text, do_images, do_graphics)
        for fn in steps:
            fn()
    return _again(pdf.doc, page, [_po_or_none(new_refs.get(id(o.ref)), o.kind) for o in sel.items])


# ---------------------------------------------------------------------------
# rendering (drag preview)
# ---------------------------------------------------------------------------
def _copy_page(d: pymupdf.Document, page: int, *, annots: bool) -> pymupdf.Document:
    tmp = pymupdf.open()
    tmp.insert_pdf(d, from_page=page, to_page=page, annots=annots, links=False, widgets=annots)
    return tmp


def _remove(tmp: pymupdf.Document, sel: _Sel) -> None:
    """Remove the selection from page 0 of a throw-away copy of its page."""
    if sel.graphics:
        gr.delete_graphics_doc(tmp, 0, [dataclasses.replace(g, page=0) for g in sel.graphics])
    for img in sorted(sel.images, key=lambda i: -i.index):
        paint, _obj = ob._locate(tmp, 0, img)
        ob._delete_paint(tmp, tmp[0], paint)
    if sel.texts:
        tob.delete_text_doc(tmp, 0, sel.texts)


def render_without(doc, page: int, objs, zoom: float, *, clip=None) -> pymupdf.Pixmap:
    """Render (part of) the page as it looks without the given objects – the
    page behind a lifted selection while it is dragged. ``doc`` is not
    changed (a throw-away copy of the page is used)."""
    d = as_document(doc)
    sel = _resolve(d, page, objs)
    tmp = _copy_page(d, page, annots=True)
    try:
        if sel:
            _remove(tmp, sel)
        return render_page(tmp, 0, zoom, clip=clip)
    finally:
        tmp.close()


def _decor_paths(d: pymupdf.Document, page: int, texts: list) -> set[int]:
    """Path numbers of the underline / strike strokes of the text objects
    (they are graphics in the content stream but belong to the text)."""
    rects = []
    rot = d[page].rotation_matrix
    for t in texts:
        for ln in t.lines:
            for r in ln.decor_rects:
                rects.append((pymupdf.Rect(r) * rot, bool(getattr(t, "free", False))))
    if not rects:
        return set()
    out = set()
    for ev in gr._page_scan(d, page).paths:
        b = ev.bbox
        if min(b[2] - b[0], b[3] - b[1]) > 4.0:
            continue
        top = ev
        while top.cont.kind == "form" and top.cont.parent_ev is not None:
            top = top.cont.parent_ev
        pasted = top.seg != 0          # a pasted path decorates only pasted (= free) text
        for r, free in rects:
            if pasted and not free:
                continue
            if (abs(b[0] - r.x0) <= 1.0 and abs(b[2] - r.x1) <= 1.0
                    and abs((b[1] + b[3]) / 2 - (r.y0 + r.y1) / 2) <= 1.0):
                out.add(ev.num)
                break
    return out


def _isolate(d: pymupdf.Document, page: int, sel: _Sel) -> pymupdf.Document:
    """A throw-away copy of the page with nothing on it but the selection
    (no annotations, form fields or Folio markers)."""
    tmp = _copy_page(d, page, annots=False)
    try:
        keep_paths = {n for g in sel.graphics for n in g.paths} | _decor_paths(d, page, sel.texts)
        gr.strip_page_except(tmp, 0, keep_paths=keep_paths, keep_images={i.index for i in sel.images})
        tob.strip_text_except(tmp, 0, sel.texts)
        if tmp.xref_get_key(tmp[0].xref, "PieceInfo")[0] != "null":
            tmp.xref_set_key(tmp[0].xref, "PieceInfo", "null")
    except Exception:
        tmp.close()
        raise
    return tmp


def render_only(doc, page: int, objs, zoom: float, *, clip=None) -> pymupdf.Pixmap:
    """Render (part of) the page with nothing on it but the given objects,
    transparent everywhere else – the ghost of a dragged selection."""
    d = as_document(doc)
    sel = _resolve(d, page, objs)
    tmp = _isolate(d, page, sel)
    try:
        return render_page(tmp, 0, zoom, clip=clip, alpha=True, annots=False)
    finally:
        tmp.close()


# ---------------------------------------------------------------------------
# clipboard
# ---------------------------------------------------------------------------
def copy_objects(doc, page: int, objs) -> Clip:
    """The objects on the clipboard: a one-page PDF (the page with
    everything else removed), their union rect and the page's size."""
    d = as_document(doc)
    sel = _resolve(d, page, objs)
    if not sel:
        raise ValueError("nothing to copy")
    rect = pymupdf.Rect(sel.items[0].rect)
    for o in sel.items[1:]:
        rect |= o.rect
    tmp = _isolate(d, page, sel)
    try:
        size = (tmp[0].rect.width, tmp[0].rect.height)
        data = write_bytes(tmp, garbage=3, deflate=True)
    finally:
        tmp.close()
    return Clip(data=data, rect=rect, page_size=size)


def paste_clip(pdf, page: int, clip: Clip, *, offset=(0.0, 0.0)) -> list[PageObject]:
    """Paste the clipboard onto ``page`` so its objects appear at
    ``clip.rect`` moved by ``offset`` (display points of the target page) –
    within a page, onto another page or into another document (one undo
    step). Pasted text is standalone (like moved text, O17). Returns the
    objects that appeared, in reading order."""
    dx, dy = float(offset[0]), float(offset[1])
    if not (math.isfinite(dx) and math.isfinite(dy)):
        raise ValueError("bad offset")
    src = pymupdf.open("pdf", clip.data)
    try:
        if src.page_count < 1:
            raise ValueError("empty clip")
        before = Counter(o.key for o in page_objects(pdf.doc, page, alt=True))
        # where the pasted lines start (target display space)
        starts = [(ln.origin[0] + dx, ln.origin[1] + dy) for t in tob.list_text(src, 0) for ln in t.lines]
        with _step(pdf, _label("pageobj.undo.paste", "Indsæt"), page):
            d = pdf.doc
            had = Counter(o.key for o in tob.list_text(d, page))
            _place(d, page, src, clip, dx, dy)
            if starts:
                tob.mark_free(d, page, _pasted_text(d, page, had, starts))
    finally:
        src.close()
    out = []
    for o in page_objects(pdf.doc, page, alt=True):
        if before[o.key] > 0:
            before[o.key] -= 1
        else:
            out.append(o)
    return out


def _pasted_text(d: pymupdf.Document, page: int, had: Counter, starts: list) -> list:
    """The text objects a paste added: new ones made only of pasted lines (a
    block the page's own text was merged into is left alone, so existing
    text never becomes standalone by a paste)."""
    had = Counter(had)
    out = []
    for o in tob.list_text(d, page):
        if had[o.key] > 0:
            had[o.key] -= 1
            continue
        if o.lines and all(any(math.hypot(ln.origin[0] - x, ln.origin[1] - y) <= 0.6 for x, y in starts)
                           for ln in o.lines):
            out.append(o)
    return out


def duplicate_objects(pdf, page: int, objs, offset=(10.0, 10.0)) -> list[PageObject]:
    """Copy and paste objects on their own page, ``offset`` away (Ctrl+D;
    one undo step). Returns the copies."""
    sel = _resolve(pdf.doc, page, objs)
    if not sel:
        return []
    with _step(pdf, _label("pageobj.undo.duplicate", "Duplikér"), page):
        clip = copy_objects(pdf.doc, page, sel.items)
        return paste_clip(pdf, page, clip, offset=offset)


# ---------------------------------------------------------------------------
# pasting: place the clip page, then flatten the form
# ---------------------------------------------------------------------------
_CATEGORIES = ("Font", "XObject", "ExtGState", "ColorSpace", "Pattern", "Shading", "Properties")
_DEVICE_CS = {"DeviceGray", "DeviceRGB", "DeviceCMYK", "Pattern"}
_INLINE_DEVICE_CS = {"G", "RGB", "CMYK", "DeviceGray", "DeviceRGB", "DeviceCMYK", "I", "Indexed"}
_SAFE_NAME = re.compile(r"[A-Za-z0-9_.+\-]+")
_WSB = b"\x00\t\n\x0c\r "


def _tokens(data: bytes, pos: int = 0):
    """``(kind, start, end)`` of the tokens of PDF syntax (content streams and
    object text); literal strings are one token."""
    n = len(data)
    while pos < n:
        m = ob._TOKEN.match(data, pos)
        if m is None:                                   # pragma: no cover - the regex matches any byte
            return
        kind, s, e = m.lastgroup, m.start(), m.end()
        if kind == "str":
            e = ob._skip_string(data, e)
        pos = e
        if kind in ("ws", "comment"):
            continue
        yield kind, s, e


def _parse_dict(text: str) -> list[tuple[str, str]]:
    """``[(key, value text)]`` of the top level of a PDF dictionary's text
    (keys as written, without the slash)."""
    data = text.encode("latin-1")
    toks = list(_tokens(data))
    out: list[tuple[str, str]] = []
    if not toks or toks[0][0] != "dopen":
        return out
    i, n = 1, len(toks)
    while i < n:
        kind, s, e = toks[i]
        if kind == "dclose":
            break
        if kind != "name":
            raise ValueError("malformed dictionary")
        key = data[s + 1:e].decode("latin-1")
        i += 1
        if i >= n:
            break
        vk, vs, ve = toks[i]
        if vk in ("dopen", "aopen"):
            close = "dclose" if vk == "dopen" else "aclose"
            depth = 0
            while i < n:
                k2 = toks[i][0]
                if k2 in ("dopen", "aopen"):
                    depth += 1
                elif k2 in ("dclose", "aclose"):
                    depth -= 1
                    if depth == 0:
                        if k2 != close:
                            raise ValueError("malformed dictionary")
                        break
                i += 1
            ve = toks[i][2] if i < n else len(data)
            i += 1
        elif (vk == "num" and i + 2 < n and toks[i + 1][0] == "num" and toks[i + 2][0] == "op"
              and data[toks[i + 2][1]:toks[i + 2][2]] == b"R"):
            ve = toks[i + 2][2]
            i += 3
        else:
            i += 1
        out.append((key, data[vs:ve].decode("latin-1")))
    return out


def _entries(doc: pymupdf.Document, xref: int, path: str) -> dict[str, str]:
    t, v = doc.xref_get_key(xref, path)
    if t == "xref":
        v = doc.xref_object(ob._ref(v), compressed=True)
    elif t != "dict":
        return {}
    return dict(_parse_dict(v))


def _own_resources(doc: pymupdf.Document, page_xref: int) -> None:
    """Give a page that inherits its /Resources the same resources itself."""
    if doc.xref_get_key(page_xref, "Resources")[0] != "null":
        return
    anc = ob._resources_owner(doc, page_xref)
    if anc and anc != page_xref:
        doc.xref_set_key(page_xref, "Resources", doc.xref_get_key(anc, "Resources")[1])
    else:
        doc.xref_set_key(page_xref, "Resources", "<<>>")


def _holder(doc: pymupdf.Document, page_xref: int, cat: str, *, create: bool = True) -> tuple[int, str] | None:
    """``(xref, key prefix)`` of the page's resource dictionary for ``cat``
    (``/Resources`` and the category may each be indirect objects – a key
    path through an indirect object cannot be written in one go)."""
    t, v = doc.xref_get_key(page_xref, "Resources")
    if t == "xref":
        res, prefix = ob._ref(v), ""
    elif t == "dict":
        res, prefix = page_xref, "Resources/"
    else:
        return None
    t2, v2 = doc.xref_get_key(res, prefix + cat)
    if t2 == "xref":
        return ob._ref(v2), ""
    if t2 != "dict":
        if not create:
            return None
        doc.xref_set_key(res, prefix + cat, "<<>>")
    return res, prefix + cat + "/"


def _put_resource(doc: pymupdf.Document, page_xref: int, cat: str, name: str, value: str) -> None:
    holder = _holder(doc, page_xref, cat)
    if holder is None:                                   # pragma: no cover - _own_resources ran before
        raise LookupError("page has no resources")
    x, prefix = holder
    doc.xref_set_key(x, prefix + name, value)


def _drop_resource(doc: pymupdf.Document, page_xref: int, cat: str, name: str) -> None:
    holder = _holder(doc, page_xref, cat, create=False)
    if holder is None:
        return
    x, prefix = holder
    text = doc.xref_object(x, compressed=True) if not prefix else doc.xref_get_key(x, prefix[:-1])[1]
    entries = _parse_dict(text)
    new = "<<" + "".join(f"/{k} {val}" for k, val in entries if k != name) + ">>"
    if not prefix:
        doc.update_object(x, new)
    else:
        doc.xref_set_key(x, prefix[:-1], new)


def _matrix_of(doc: pymupdf.Document, xref: int) -> pymupdf.Matrix:
    t, v = doc.xref_get_key(xref, "Matrix")
    vals = ob._numbers(v) if t == "array" else []
    return pymupdf.Matrix(*vals[:6]) if len(vals) >= 6 else pymupdf.Matrix(1, 0, 0, 1, 0, 0)


def _wrap_if_dirty(doc: pymupdf.Document, page: int) -> None:
    """Wrap the page content in ``q … Q`` when it leaves a changed CTM,
    clip or graphics state behind (pasted content must start from the
    defaults); unbalanced ``q`` are closed too."""
    scan = gr._page_scan(doc, page)
    if not scan.conts:
        return
    cont = scan.conts[0]
    if cont.top_dirty:
        pg = doc[page]
        data = pg.read_contents()
        ob._write_page_contents(doc, pg, b"q\n" + data + b"\n" + b"Q\n" * (cont.q_depth_end + 1))


def _place(doc: pymupdf.Document, page: int, src: pymupdf.Document, clip: Clip, dx: float, dy: float) -> None:
    pg = doc[page]
    _own_resources(doc, pg.xref)
    _wrap_if_dirty(doc, page)
    pg = doc[page]
    inner = pg.show_pdf_page(pg.rect, src, 0)
    # forget the graft bookkeeping for this throw-away source
    try:
        doc.ShownPages.pop((src._graft_id, 0), None)
        doc.Graftmaps.pop(src._graft_id, None)
    except Exception:                                    # pragma: no cover - PyMuPDF internals
        pass
    name = None
    for x in reversed(pg.get_contents()):
        found = re.findall(rb"/(fzFrm\d+)[\x00\t\n\x0c\r ]+Do", doc.xref_stream(x) or b"")
        if found:
            name = found[-1].decode("latin-1")
            break
    if name is None:                                     # pragma: no cover - show_pdf_page always draws it
        raise RuntimeError("pasted form not found")
    outer = ob._ref(_entries(doc, pg.xref, "Resources/XObject")[name])
    # the placement: clip page user space -> its display space -> + offset
    # -> target display space -> target user space (any sizes, /Rotate, cropbox)
    sp = src[0]
    pm_src = ob._page_matrix(sp)
    M = pm_src * pymupdf.Matrix(1, 0, 0, 1, dx, dy) * ob._inv(ob._page_matrix(pg))
    inner_m = _matrix_of(doc, inner)
    visible = (pymupdf.Rect(sp.rect) * ob._inv(pm_src)).normalize()          # source page area, user space
    form_box = (visible * inner_m).normalize()
    doc.xref_set_key(outer, "Matrix", "[" + ob._fmt(ob._inv(inner_m) * M).decode("latin-1") + "]")
    doc.xref_set_key(outer, "BBox", "[" + " ".join(ob._num(v).decode("latin-1") for v in form_box) + "]")
    # objects that stick out of their page were cut off there: clip them
    need_clip = not _inside(clip.rect, pymupdf.Rect(sp.rect), 0.5)
    try:
        _flatten(doc, page, name, inner, M, visible if need_clip else None)
    except Exception:
        pass                    # keep the form: it is placed correctly and renders the same


def _scan_names(data: bytes) -> tuple[list[tuple[str, str, int, int]], int, int]:
    """Resource names used by a content stream: ``[(category, name, start,
    end)]`` (span of the name token incl. its slash), and the number of
    ``Q`` without ``q`` and of ``q`` left open."""
    occ: list[tuple[str, str, int, int]] = []
    operands: list[tuple[str, int, int]] = []
    depth = under = 0

    def name(tok) -> str:
        return data[tok[1] + 1:tok[2]].decode("latin-1")

    n = len(data)
    toks = _tokens(data)
    while True:
        tok = next(toks, None)
        if tok is None:
            break
        kind, s, e = tok
        if kind != "op":
            operands.append((kind, s, e))
            continue
        op = data[s:e]
        last = operands[-1] if operands else None
        if op == b"BI":
            params: list[tuple[str, int, int]] = []
            id_end = None
            for k2, s2, e2 in toks:
                if k2 == "op" and data[s2:e2] == b"ID":
                    id_end = e2
                    break
                params.append((k2, s2, e2))
            if id_end is None:
                break
            for i, (k2, s2, e2) in enumerate(params[:-1]):
                if k2 == "name" and data[s2:e2] in (b"/CS", b"/ColorSpace") and params[i + 1][0] == "name":
                    v = params[i + 1]
                    if name(v) not in _INLINE_DEVICE_CS:
                        occ.append(("ColorSpace", name(v), v[1], v[2]))
            start = id_end + 1 if id_end < n and data[id_end] in _WSB else id_end
            end = ob._find_ei(data, start, ob._inline_params([(k2, data[s2:e2]) for k2, s2, e2 in params]))
            toks = _tokens(data, end)
            operands = []
            continue
        if op == b"Tf":
            if len(operands) >= 2 and operands[-2][0] == "name":
                occ.append(("Font", name(operands[-2]), operands[-2][1], operands[-2][2]))
        elif op in (b"Do", b"gs", b"sh", b"cs", b"CS", b"scn", b"SCN") and last is not None and last[0] == "name":
            cat = {b"Do": "XObject", b"gs": "ExtGState", b"sh": "Shading", b"scn": "Pattern",
                   b"SCN": "Pattern"}.get(op, "ColorSpace")
            if not (cat == "ColorSpace" and name(last) in _DEVICE_CS):
                occ.append((cat, name(last), last[1], last[2]))
        elif op in (b"BDC", b"DP"):
            if len(operands) >= 2 and last[0] == "name" and operands[-2][0] == "name":
                occ.append(("Properties", name(last), last[1], last[2]))
        elif op == b"q":
            depth += 1
        elif op == b"Q":
            depth -= 1
            if depth < 0:
                under += 1
                depth = 0
        operands = []
    return occ, under, depth


_REF = re.compile(r"(\d+)\s+(\d+)\s+R(?![A-Za-z0-9_.])")
#: most object pairs one resource comparison may look at
_COMPARE_BUDGET = 400


def _same_value(doc: pymupdf.Document, a: str, b: str, memo: dict) -> bool:
    """Whether two resource values (object text) describe the same thing –
    also when they refer to different but identical objects (``show_pdf_page``
    copies a font a pasted text uses, even into the document it came from)."""
    na, nb = " ".join(a.split()), " ".join(b.split())
    if na == nb:
        return True
    ra = [int(m.group(1)) for m in _REF.finditer(na)]
    rb = [int(m.group(1)) for m in _REF.finditer(nb)]
    if len(ra) != len(rb) or _REF.sub("R", na) != _REF.sub("R", nb):
        return False
    return all(_same_obj(doc, x, y, memo) for x, y in zip(ra, rb))


_STREAM_KEYS = ("Length", "Filter", "DecodeParms")


def _stream_dict(text: str) -> str:
    """A stream's dictionary without the keys that only describe its encoding."""
    try:
        items = _parse_dict(text)
    except Exception:
        return text
    return "<<" + "".join(f"/{k} {v}" for k, v in items if k not in _STREAM_KEYS) + ">>"


def _same_obj(doc: pymupdf.Document, x: int, y: int, memo: dict) -> bool:
    if x == y:
        return True
    if (x, y) in memo:
        return memo[(x, y)]
    memo["n"] = memo.get("n", 0) + 1
    if memo["n"] > _COMPARE_BUDGET:
        return False
    memo[(x, y)] = True                                  # (a cycle back here: assume equal)
    try:
        sx, sy = doc.xref_is_stream(x), doc.xref_is_stream(y)
        tx, ty = doc.xref_object(x, compressed=True), doc.xref_object(y, compressed=True)
        if sx != sy:
            res = False
        elif sx:
            # the same data, maybe compressed differently (the clipboard PDF is deflated)
            res = (doc.xref_stream(x) == doc.xref_stream(y)
                   and _same_value(doc, _stream_dict(tx), _stream_dict(ty), memo))
        else:
            res = _same_value(doc, tx, ty, memo)
    except Exception:
        res = False
    memo[(x, y)] = res
    return res


def _fresh(name: str, taken) -> str:
    base = re.sub(r"[^A-Za-z0-9]", "", name)[:24] or "R"
    k = 1
    while f"{base}_{k}" in taken:
        k += 1
    return f"{base}_{k}"


def _mc_balance(data: bytes) -> tuple[int, int]:
    """Marked content of a content stream: the number of ``EMC`` without an
    opening ``BMC``/``BDC`` and of sequences left open."""
    depth = under = 0
    n = len(data)
    toks = _tokens(data)
    while True:
        tok = next(toks, None)
        if tok is None:
            break
        kind, s, e = tok
        if kind != "op":
            continue
        op = data[s:e]
        if op in (b"BMC", b"BDC"):
            depth += 1
        elif op == b"EMC":
            if depth:
                depth -= 1
            else:
                under += 1
        elif op == b"BI":                               # skip the inline image's data
            params = []
            id_end = None
            for k2, s2, e2 in toks:
                if k2 == "op" and data[s2:e2] == b"ID":
                    id_end = e2
                    break
                params.append((k2, data[s2:e2]))
            if id_end is None:
                break
            start = id_end + 1 if id_end < n and data[id_end] in _WSB else id_end
            toks = _tokens(data, ob._find_ei(data, start, ob._inline_params(params)))
    return under, depth


def _flatten(doc: pymupdf.Document, page: int, name: str, inner: int, M: pymupdf.Matrix,
             clip_rect: pymupdf.Rect | None) -> None:
    """Replace the invocation ``/name Do`` of the pasted form by the content
    of the clip page itself (``inner``, drawn through ``M``), with its
    resources merged into the page's."""
    pg = doc[page]
    data = doc.xref_stream(inner) or b""
    occ, under, depth = _scan_names(data)
    src_res = {cat: _entries(doc, inner, f"Resources/{cat}") for cat in _CATEGORIES}
    tgt_res = {cat: _entries(doc, pg.xref, f"Resources/{cat}") for cat in _CATEGORIES}
    renames: dict[tuple[str, str], str] = {}
    adds: list[tuple[str, str, str]] = []
    memo: dict = {}
    for cat, nm, _s, _e in occ:
        if (cat, nm) in renames:
            continue
        val = src_res[cat].get(nm)
        if val is None:
            renames[(cat, nm)] = nm                     # broken reference: left as it was
            continue
        have = tgt_res[cat]
        cur = have.get(nm)
        if cur is not None and _same_value(doc, cur, val, memo):
            renames[(cat, nm)] = nm                     # the same object under the same name
            continue
        other = next((k for k, v in have.items() if k != nm and v != "null" and _same_value(doc, v, val, memo)), None)
        if other is not None:
            renames[(cat, nm)] = other                  # the same object under another name
            continue
        new = nm if (cur is None or cur == "null") and _SAFE_NAME.fullmatch(nm) else _fresh(nm, have)
        have[new] = val
        adds.append((cat, new, val))
        renames[(cat, nm)] = new
    edits = [(s, e, b"/" + renames[(cat, nm)].encode("latin-1")) for cat, nm, s, e in occ
             if renames[(cat, nm)] != nm]
    body = ob._splice(data, edits)
    # /FolioPaste BMC … EMC marks a paste segment for the graphics engine: the
    # pasted paths never group, merge or form a table with what was there (a
    # marked-content sequence survives MuPDF's content filter, unlike an
    # empty BT ET); the body's own marked content is balanced inside it
    mc_under, mc_open = _mc_balance(body)
    head = b"/" + gr.PASTE_TAG + b" BMC\n" + b"/Folio BMC\n" * mc_under + b"q\n" + ob._fmt(M) + b" cm\n"
    if clip_rect is not None:
        r = clip_rect
        head += b" ".join(ob._num(v) for v in (r.x0, r.y0, r.width, r.height)) + b" re W n\n"
    block = (head + b"q\n" * under + body + b"\n" + b"Q\n" * depth + b"Q\n"
             + b"EMC\n" * mc_open + b"EMC\n")
    # find the invocation (show_pdf_page appends it as a content stream of its own)
    pat = re.compile(rb"/" + re.escape(name.encode("latin-1")) + rb"[\x00\t\n\x0c\r ]+Do")
    for x in reversed(pg.get_contents()):
        stream = doc.xref_stream(x) or b""
        hits = list(pat.finditer(stream))
        if hits:
            break
    else:                                                # pragma: no cover - checked by the caller
        raise LookupError("form invocation not found")
    for cat, new, val in adds:
        _put_resource(doc, pg.xref, cat, new, val)
    m = hits[-1]
    ob._set_stream(doc, x, stream[:m.start()] + b"\n" + block + stream[m.end():])
    _drop_resource(doc, pg.xref, "XObject", name)
