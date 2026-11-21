"""Vector graphics in the page content – list, hit-test, move, scale, restyle,
restack and delete paths (the "Markér objekt" tool, spec O2/O5/O8/O11/O12).

How it works
------------
A rectangle, line or curve on a PDF page is not an object: the content
stream *constructs* a path (``m l c v y re h``) and *paints* it (``S s f F
f* B B* b b*``) with the graphics state in effect – the current
transformation matrix (CTM) from ``cm`` and the colours, line width etc.
saved and restored by ``q``/``Q``. This module interprets the content
stream(s) of a page – and the Form XObjects they invoke – with a small
operator scanner (operands are sliced between two operators, so numbers are
never tokenised one by one in Python) and records every painted path:

* its byte span (first construction operand … paint operator), the
  construction operators with their operands, the paint operator,
* the CTM, the innermost ``q … Q`` block, the graphics state as raw operator
  bytes per category (re-emittable as is),
* its geometry in display space (bounding box incl. half the stroke width,
  flattened polylines for hit-testing, ``page.get_drawings()``-like items).

Paths are grouped into :class:`GraphicObject` s (a filled box and its border,
an arrow = line + head, a figure drawn of several strokes) and classified so
page backgrounds, header/footer graphics repeated from a template and table
rules are not picked by a plain click (O5).

Edits are byte-precise splices like those of :mod:`pdfstudio.core.objects`:

* **move** (and a transform with ``keep_stroke=False``) inserts or rewrites ONE
  ``cm`` at the start of the outermost ``q … Q`` block that draws nothing but
  the selection, or wraps the path's own operators in ``q <cm> … Q`` once;
  later moves rewrite that same ``cm`` – the stream does not grow.
* **scale** rewrites the path coordinates (``re`` becomes ``m/l/l/l/h`` if
  the transform in user space is not axis-aligned), so the line width stays
  exactly as it was (O8).
* **style** inserts/rewrites ``RG``/``rg``/``w`` right before the path inside
  a block that draws only the selection (or a new ``q … Q``) and changes the
  paint operator when a stroke or fill is added or removed.
* **restack** removes the selection's blocks and re-emits them, with the
  graphics state they need, at the end (front) or start (back) of the page.
* **delete** removes the blocks / bare path operators.

Everything else in the content stream stays byte-for-byte untouched (O13).
Paths inside a Form XObject that draws only paths are one object per form
invocation (moved by transforming the invocation; scaled/restyled in a
private copy of the form for this one invocation). Paths in a form that also
draws text or images are listed with ``editable=False``.

Coordinates are display space (see CLAUDE.md); colours are ``(r, g, b)``
floats 0..1.

Limitations
-----------
* A clipping path set *outside* the selection's own ``q … Q`` block stays
  where it is when the graphic moves, and is not re-emitted on restack.
* Colours in special colour spaces (Separation, DeviceN, Lab, Indexed,
  patterns) are reported approximately; they are kept exactly when moved.
* ``sh`` (shadings) are not objects.
"""

from __future__ import annotations

import bisect
import dataclasses
import hashlib
import math
import re
from dataclasses import dataclass

import pymupdf

from . import objects as ob
from .document import CONTENT
from .objects import (
    _DELIMS,
    _IDENTITY,
    _OP_END,
    _OP_START,
    _TOKEN,
    _decode_name,
    _det,
    _find_ei,
    _fmt,
    _inline_params,
    _inv,
    _key,
    _label,
    _num,
    _page_matrix,
    _ref,
    _register_xobject,
    _resources_owner,
    _set_stream,
    _skip_string,
    _write_page_contents,
)

__all__ = [
    "KEEP",
    "GraphicObject",
    "list_graphics",
    "graphic_at",
    "graphics_in",
    "find_graphic",
    "move_graphics",
    "transform_graphics",
    "scale_graphics",
    "delete_graphics",
    "delete_graphics_doc",
    "set_graphic_style",
    "restack_graphics",
    "render_without_graphics",
    "strip_page_except",
]


class _Keep:
    """Sentinel type of :data:`KEEP`."""

    __slots__ = ()

    def __repr__(self) -> str:
        return "KEEP"


#: "leave unchanged" for :func:`set_graphic_style`
KEEP = _Keep()


# ---------------------------------------------------------------------------
# public data
# ---------------------------------------------------------------------------
@dataclass
class GraphicObject:
    """One selectable graphic (one or more painted paths) on a page."""

    page: int
    index: int                  # drawing order of its first path (= min(paths))
    paths: tuple                # member path numbers (page drawing order, incl. non-selectable ones)
    rect: pymupdf.Rect          # display bbox incl. stroke
    kind: str                   # "shape" | "line" | "background" | "template" | "table"
    stroke: tuple | None        # (r, g, b) of the first stroked member, None = no stroke
    fill: tuple | None          # (r, g, b) of the first filled member, None = no fill
    width: float                # display stroke width (pt) of the first stroked member, 0 if none
    selectable: bool            # False for background/template/table (O5) – Alt picks them
    editable: bool              # False e.g. for clip paths or paths in mixed forms
    in_form: bool
    items: list                 # display-space items like page.get_drawings(): ("l", p1, p2),
                                # ("c", p1, p2, p3, p4), ("re", rect), ("qu", quad)
    key: tuple                  # (page, paths, rounded rect) – relocate with find_graphic()


# ---------------------------------------------------------------------------
# content-stream scanner
# ---------------------------------------------------------------------------
# Stops at strings (skipped in Python), comments, hex strings and operators;
# operands are whatever lies between two operators.
_GSCAN = re.compile(
    rb"\("
    rb"|%[^\r\n]*"
    rb"|<[0-9A-Fa-f\x00\t\n\x0c\r ]*>"
    rb"|" + _OP_START + rb"([A-Za-z'\"][^" + _DELIMS + rb"]*)" + _OP_END
)
_WSB = b"\x00\t\n\x0c\r "
_NAME_AT_END = re.compile(rb"/([^" + _DELIMS + rb"]*)[\x00\t\n\x0c\r ]*\Z")
_PATH_ARGS = {b"m": 2, b"l": 2, b"c": 6, b"v": 4, b"y": 4, b"re": 4, b"h": 0}
# paint operator -> (stroke, fill, close, even-odd); n = no paint
_PAINT = {b"S": (True, False, False, False), b"s": (True, False, True, False),
          b"f": (False, True, False, False), b"F": (False, True, False, False),
          b"f*": (False, True, False, True), b"B": (True, True, False, False),
          b"B*": (True, True, False, True), b"b": (True, True, True, False),
          b"b*": (True, True, True, True), b"n": None}
# state operator -> category ("S"/"F" = stroke/fill colour)
_STATE = {b"w": "w", b"J": "J", b"j": "j", b"M": "M", b"d": "d", b"ri": "ri", b"i": "i",
          b"CS": "CS", b"cs": "cs", b"SC": "S", b"SCN": "S", b"G": "S", b"RG": "S", b"K": "S",
          b"sc": "F", b"scn": "F", b"g": "F", b"rg": "F", b"k": "F", b"gs": "gs"}
# setting one category resets another (a colour space resets its colour; the
# device colour operators set their own colour space)
_RESETS = {b"G": "CS", b"RG": "CS", b"K": "CS", b"g": "cs", b"rg": "cs", b"k": "cs", b"CS": "S", b"cs": "F"}
_TEXT_SHOW = {b"Tj", b"TJ", b"'", b'"'}
_FLAT_STEPS = 8
_BAND = 0.2                 # header/footer band (fraction of the page height) for the template test
_MAX_FORM_DEPTH = 10
_EMPTY: dict = {}


class _Block:
    """A ``q … Q`` block inside one container. ``lo``/``hi``: the paint events
    (sequence numbers) inside the block are ``lo <= seq < hi``."""

    __slots__ = ("cont", "parent", "q_start", "q_end", "Q_start", "Q_end", "ctm", "first_cm", "mc", "mc_ok",
                 "lo", "hi", "state")

    def __init__(self, cont, parent, q_start, q_end, ctm, mc, lo, state) -> None:
        self.cont = cont
        self.parent: _Block | None = parent
        self.q_start = q_start
        self.q_end = q_end
        self.Q_start: int | None = None
        self.Q_end: int | None = None
        self.ctm: pymupdf.Matrix = ctm
        self.first_cm: tuple[int, int, pymupdf.Matrix] | None = None
        self.mc = mc
        self.mc_ok = True
        self.lo = lo
        self.hi = lo
        self.state: dict = state          # graphics state at q

    def exclusive_to(self, seqs: set, nested_mc: bool) -> bool:
        """Closed, draws something, and every paint event inside is in ``seqs``."""
        if self.Q_end is None or (nested_mc and not self.mc_ok):
            return False
        if self.hi <= self.lo or self.hi - self.lo > len(seqs):
            return False
        return all(i in seqs for i in range(self.lo, self.hi))


class _Cont:
    """The page's content, or one invocation of a Form XObject."""

    __slots__ = ("kind", "xref", "data", "xobjects", "parent_ev", "depth", "events", "mc_ok", "path_only",
                 "q_depth_end", "top_dirty", "owner")

    def __init__(self, kind, xref, data, xobjects, parent_ev, depth, owner) -> None:
        self.kind: str = kind              # "page" | "form"
        self.xref: int = xref
        self.data: bytes = data
        self.xobjects: dict[str, int] = xobjects
        self.parent_ev: _Ev | None = parent_ev
        self.depth = depth
        self.events: list[_Ev] = []
        self.mc_ok = True
        self.path_only = False             # a form drawing paths (or path-only forms) and nothing else
        self.q_depth_end = 0               # unclosed q at the end
        self.top_dirty = False             # state / cm / clip changed outside any q (page)
        self.owner = owner                 # xref whose /Resources apply (colour spaces)


class _Ev:
    """One paint event in a container: a painted path, an image, an inline
    image, a form invocation, a text show operator, a shading."""

    __slots__ = ("cont", "seq", "kind", "start", "end", "ctm", "block", "state", "clean", "brk",
                 # path
                 "num", "op", "op_span", "segs", "items", "polys", "clip", "broken", "pre", "M",
                 "stroke", "fill", "close", "evenodd", "bbox", "geom", "wdisp", "_rgb", "inner",
                 # xobjects
                 "xref", "name_span", "child", "index")

    def __init__(self, cont, seq, kind, start, end, ctm, block, state, clean, brk) -> None:
        self.cont: _Cont = cont
        self.seq = seq
        self.kind = kind
        self.start = start
        self.end = end
        self.ctm: pymupdf.Matrix = ctm
        self.block: _Block | None = block
        self.state: dict = state
        self.clean = clean                 # no operator between the previous paint event and this one
        self.brk = brk                     # grouping break counter (text/images/forms/shadings)
        self.num = -1
        self.child: _Cont | None = None
        self.index = None
        self.xref = 0
        self.name_span = None
        self._rgb = None
        self.inner: list[bytes] = []       # state operators inside the path's own span (PyMuPDF style)


class _Path:
    """A path under construction (display geometry is computed on the fly)."""

    __slots__ = ("start", "segs", "items", "polys", "sub", "cur", "first", "clip", "broken", "M", "inner")

    def __init__(self, start: int, M: tuple) -> None:
        self.start = start
        self.segs: list = []               # (op, operand start, op end, numbers)
        self.items: list = []              # display items ("l", p0, p1) | ("c", p0..p3) | ("q4", [4 pts])
        self.polys: list = []              # [[display points], closed]
        self.sub: list | None = None
        self.cur = (0.0, 0.0)
        self.first = (0.0, 0.0)
        self.clip = False
        self.broken = False
        self.M = M
        self.inner: list[bytes] = []

    def _pt(self, x: float, y: float) -> tuple[float, float]:
        a, b, c, d, e, f = self.M
        return (x * a + y * c + e, x * b + y * d + f)

    def move(self, x: float, y: float) -> None:
        self.cur = self.first = (x, y)
        self.sub = [[self._pt(x, y)], False]
        self.polys.append(self.sub)

    def _ensure(self) -> list:
        if self.sub is None or self.sub[1]:
            self.move(*self.cur)
        return self.sub[0]

    def line(self, x: float, y: float) -> None:
        pts = self._ensure()
        p = self._pt(x, y)
        self.items.append(("l", pts[-1], p))
        pts.append(p)
        self.cur = (x, y)

    def curve(self, x1, y1, x2, y2, x3, y3) -> None:
        pts = self._ensure()
        p0, p1, p2, p3 = pts[-1], self._pt(x1, y1), self._pt(x2, y2), self._pt(x3, y3)
        self.items.append(("c", p0, p1, p2, p3))
        for k in range(1, _FLAT_STEPS + 1):
            t = k / _FLAT_STEPS
            u = 1 - t
            a, b, c, d = u * u * u, 3 * u * u * t, 3 * u * t * t, t * t * t
            pts.append((a * p0[0] + b * p1[0] + c * p2[0] + d * p3[0], a * p0[1] + b * p1[1] + c * p2[1] + d * p3[1]))
        self.cur = (x3, y3)

    def rect(self, x, y, w, h) -> None:
        pts = [self._pt(x, y), self._pt(x + w, y), self._pt(x + w, y + h), self._pt(x, y + h)]
        self.sub = [pts, True]
        self.polys.append(self.sub)
        self.items.append(("q4", pts))
        self.cur = self.first = (x, y)

    def close(self) -> None:
        if self.sub is not None and not self.sub[1]:
            pts = self.sub[0]
            if len(pts) > 2 and pts[-1] != pts[0]:      # (closing a 2-point line draws nothing new)
                self.items.append(("l", pts[-1], pts[0]))
            self.sub[1] = True
        self.cur = self.first


def _floats(region: bytes, k: int) -> list[float] | None:
    toks = region.split()
    if len(toks) != k:
        return None
    try:
        return [float(t) for t in toks]
    except ValueError:
        return None


def _mtuple(m: pymupdf.Matrix) -> tuple:
    return (m.a, m.b, m.c, m.d, m.e, m.f)


class _Scanner:
    """Interprets a page's content (and its forms) to find painted paths."""

    def __init__(self, doc: pymupdf.Document, page: pymupdf.Page) -> None:
        self.doc = doc
        self.pm = _page_matrix(page)
        self.helper = ob._Scanner(doc)       # XObject info cache
        self.conts: list[_Cont] = []
        self.paths: list[_Ev] = []
        self.images: list[_Ev] = []          # same order/indices as objects.list_images

    def scan_page(self, page: pymupdf.Page) -> _Cont:
        doc = self.doc
        parts = []
        for x in page.get_contents():
            try:
                parts.append(doc.xref_stream(x) or b"")
            except Exception:
                parts.append(b"")
        owner = _resources_owner(doc, page.xref)
        cont = _Cont("page", page.xref, b"\n".join(parts), self.helper.page_xobjects(page.xref), None, 0, owner)
        self.conts.append(cont)
        self._run(cont, pymupdf.Matrix(_IDENTITY), (), 0)
        return cont

    # -- the interpreter ---------------------------------------------------
    def _run(self, cont: _Cont, ctm: pymupdf.Matrix, chain: tuple, depth: int) -> None:  # noqa: C901
        data = cont.data
        events = cont.events
        search = _GSCAN.search
        pm = self.pm
        M = _mtuple(ctm * pm)
        pos = prev = 0
        stack: list = []
        block: _Block | None = None
        pending: _Block | None = None
        st: dict = _EMPTY
        mc = 0
        path: _Path | None = None
        pre: list = []                       # state operators since the last other operator
        last_op_end = -1                     # end of the last operator that is not part of an event
        last_ev_end = -1
        brk = 0
        gs_n = 0
        in_text = False

        def event(kind, start, end):
            nonlocal last_ev_end, last_op_end
            clean = last_ev_end >= 0 and last_op_end <= last_ev_end
            ev = _Ev(cont, len(events), kind, start, end, ctm, block, st, clean, brk)
            events.append(ev)
            last_ev_end = last_op_end = end
            return ev

        while True:
            m = search(data, pos)
            if m is None:
                break
            s, e = m.span()
            op = m.group(1)
            if op is None:
                pos = _skip_string(data, e) if data[s] == 0x28 else e
                continue
            pos = e
            region = data[prev:s]
            prev = e
            k = _PATH_ARGS.get(op)
            if k is not None:                                    # path construction
                lead = len(region) - len(region.lstrip(_WSB))
                ostart = s - len(region) + lead if k else s
                if path is None:
                    path = _Path(ostart, M)
                vals = _floats(region, k) if k else []
                if vals is None:
                    path.broken = True
                    continue
                path.segs.append((op, ostart, e, vals))
                if op == b"re":
                    path.rect(*vals)
                elif op == b"l":
                    path.line(*vals)
                elif op == b"m":
                    path.move(*vals)
                elif op == b"c":
                    path.curve(*vals)
                elif op == b"v":
                    x, y = path.cur
                    path.curve(x, y, *vals)
                elif op == b"y":
                    path.curve(vals[0], vals[1], vals[2], vals[3], vals[2], vals[3])
                else:
                    path.close()
                continue
            if op in (b"W", b"W*"):
                if path is not None:
                    path.clip = True
                continue
            if op in _PAINT:
                flags = _PAINT[op]
                if path is not None and path.polys:
                    if flags is not None:
                        ev = event("path", path.start, e)
                        self._finish_path(ev, path, op, s, e, flags, pre)
                    if path.clip and not stack and cont.kind == "page":
                        cont.top_dirty = True        # a clip outside any q stays in force to the end
                path = None
                pre = []
                continue
            cat = _STATE.get(op)
            if cat is not None:
                lead = len(region) - len(region.lstrip(_WSB))
                ostart = s - len(region) + lead
                raw = data[ostart:e]
                if path is not None:         # PyMuPDF sets colours/width between construction and paint
                    path.inner.append(raw)
                st = dict(st)
                reset = _RESETS.get(op)
                if reset is not None:
                    st.pop(reset, None)
                if cat == "gs":
                    gs_n += 1
                    st[("gs", gs_n)] = raw
                else:
                    st.pop(cat, None)
                    st[cat] = raw
                pre.append((cat, ostart, e, op))
                if not stack and cont.kind == "page":
                    cont.top_dirty = True
                last_op_end = e
                continue
            if path is not None:             # any other operator inside a path: listed, not editable
                path.broken = True
            pre = []
            if op == b"q":
                stack.append((ctm, block, st, M))
                block = _Block(cont, block, s, e, ctm, mc, len(events), st)
                pending = block
                last_op_end = e
                continue
            if op == b"Q":
                if stack:
                    block.Q_start, block.Q_end = s, e
                    block.hi = len(events)
                    if mc != block.mc:
                        block.mc_ok = False
                    ctm, block, st, M = stack.pop()
                pending = None
                last_op_end = e
                continue
            if op == b"cm":
                toks = region.split()
                mat = None
                if len(toks) >= 6:
                    try:
                        mat = pymupdf.Matrix(*(float(t) for t in toks[-6:]))
                    except ValueError:
                        mat = None
                if mat is not None:
                    if pending is not None and len(toks) == 6:
                        lead = len(region) - len(region.lstrip(_WSB))
                        pending.first_cm = (s - len(region) + lead, e, mat)
                    ctm = mat * ctm
                    M = _mtuple(ctm * pm)
                    if not stack and cont.kind == "page":
                        cont.top_dirty = True
                pending = None
                last_op_end = e
                continue
            pending = None
            if op == b"Do":
                a = _NAME_AT_END.search(region)
                base = s - len(region)
                if a is None:
                    last_op_end = e
                    continue
                ns, ne = base + a.start(), base + a.end(1)
                brk += 1
                self._do(cont, data, ns, ne, e, ctm, chain, depth, event)
                brk += 1
                continue
            if op == b"BI":
                brk += 1
                e2 = self._inline(data, e)
                ev = event("inline", s, e2)
                if abs(_det(ctm)) >= 1e-9:
                    ev.index = len(self.images)
                    self.images.append(ev)
                pos = prev = e2
                brk += 1
                continue
            if op in _TEXT_SHOW:
                if in_text:
                    event("text", s, e)
                else:
                    last_op_end = e
                continue
            if op == b"BT":
                in_text = True
                brk += 1
            elif op == b"ET":
                in_text = False
                brk += 1
            elif op == b"sh":
                brk += 1
                lead = len(region) - len(region.lstrip(_WSB))
                event("sh", s - len(region) + lead, e)
                brk += 1
                continue
            elif op == b"EMC":
                mc -= 1
                b = block
                while b is not None and b.mc > mc:
                    b.mc_ok = False
                    b = b.parent
                if mc < 0:
                    cont.mc_ok = False
            elif op in (b"BMC", b"BDC"):
                mc += 1
            last_op_end = e
        if mc != 0:
            cont.mc_ok = False
        cont.q_depth_end = len(stack)
        if cont.kind == "form":
            cont.path_only = bool(events) and all(
                ev.kind == "path" or (ev.kind == "form" and ev.child is not None and ev.child.path_only)
                for ev in events)

    def _finish_path(self, ev: _Ev, path: _Path, op: bytes, s: int, e: int, flags, pre) -> None:
        ev.op = op
        ev.op_span = (s, e)
        ev.segs = path.segs
        ev.items = path.items
        ev.polys = path.polys
        ev.clip = path.clip
        ev.broken = path.broken
        ev.pre = pre
        ev.inner = path.inner
        ev.M = path.M
        ev.stroke, ev.fill, ev.close, ev.evenodd = flags
        xs = [p[0] for poly in path.polys for p in poly[0]]
        ys = [p[1] for poly in path.polys for p in poly[0]]
        ev.geom = (min(xs), min(ys), max(xs), max(ys))
        a, b, c, d = path.M[:4]
        scale = math.sqrt(abs(a * d - b * c))
        w = 1.0
        raw = ev.state.get("w")
        if raw is not None:
            try:
                w = abs(float(raw.split()[0]))
            except (ValueError, IndexError):
                w = 1.0
        ev.wdisp = w * scale if ev.stroke else 0.0
        hw = max(ev.wdisp, 0.5) / 2 if ev.stroke else 0.0
        g = ev.geom
        ev.bbox = (g[0] - hw, g[1] - hw, g[2] + hw, g[3] + hw)
        ev.num = len(self.paths)
        self.paths.append(ev)

    def _do(self, cont, data, ns, ne, end, ctm, chain, depth, event) -> None:
        name = _decode_name(data[ns + 1:ne].decode("latin-1"))
        xref = cont.xobjects.get(name, 0)
        kind, info = self.helper.xobject(xref)
        if kind == "image":
            ev = event("image", ns, end)
            ev.xref = xref
            ev.name_span = (ns, ne)
            if abs(_det(ctm)) >= 1e-9:
                ev.index = len(self.images)
                self.images.append(ev)
        elif kind == "form" and depth < _MAX_FORM_DEPTH and xref not in chain:
            ev = event("form", ns, end)
            ev.xref = xref
            ev.name_span = (ns, ne)
            try:
                stream = self.doc.xref_stream(xref) or b""
            except Exception:
                stream = b""
            has_res = info.get("has_resources")
            xobjects = ob._xobject_map(self.doc, xref) if has_res else cont.xobjects
            child = _Cont("form", xref, stream, xobjects, ev, cont.depth + 1, xref if has_res else cont.owner)
            ev.child = child
            self.conts.append(child)
            self._run(child, info["matrix"] * ctm, chain + (xref,), depth + 1)
        else:
            ev = event("other", ns, end)
            ev.name_span = (ns, ne)

    @staticmethod
    def _inline(data: bytes, pos: int) -> int:
        """Skip ``BI <dict> ID <data> EI`` from after ``BI``; returns the
        offset after ``EI``."""
        n = len(data)
        toks = []
        id_end = None
        while pos < n:
            m = _TOKEN.match(data, pos)
            kind, end = m.lastgroup, m.end()
            if kind in ("ws", "comment"):
                pos = end
                continue
            if kind == "str":
                end = _skip_string(data, end)
            if kind == "op" and data[pos:end] == b"ID":
                id_end = end
                break
            toks.append((kind, data[pos:end]))
            pos = end
        if id_end is None:
            return n
        return _find_ei(data, id_end + 1, _inline_params(toks))


# ---------------------------------------------------------------------------
# colours
# ---------------------------------------------------------------------------
def _cmyk(c, m, y, k) -> tuple:
    return (max(0.0, 1 - min(1.0, c + k)), max(0.0, 1 - min(1.0, m + k)), max(0.0, 1 - min(1.0, y + k)))


def _clamp(t: tuple) -> tuple:
    return tuple(min(1.0, max(0.0, float(v))) for v in t)


def _cs_family(doc, owner: int, raw_cs: bytes | None) -> str:
    if raw_cs is None:
        return "DeviceGray"
    m = re.search(rb"/([^" + _DELIMS + rb"]*)", raw_cs)
    if m is None:
        return ""
    name = _decode_name(m.group(1).decode("latin-1"))
    if name in ("DeviceGray", "DeviceRGB", "DeviceCMYK", "Pattern", "G", "RGB", "CMYK"):
        return {"G": "DeviceGray", "RGB": "DeviceRGB", "CMYK": "DeviceCMYK"}.get(name, name)
    if not owner:
        return ""
    t, v = _key(doc, owner, f"Resources/ColorSpace/{name}")
    if t == "xref":
        try:
            v = doc.xref_object(_ref(v), compressed=True)
        except Exception:
            return ""
    fam = re.findall(r"/([A-Za-z]+)", v or "")
    return fam[0] if fam else ""


def _color(doc, owner: int, raw: bytes | None, raw_cs: bytes | None) -> tuple:
    """RGB of a colour operator (approximate for special colour spaces)."""
    if raw is None:                                    # initial colour: black (full tint)
        return (0.0, 0.0, 0.0)
    toks = raw.split()
    op = toks[-1]
    args = toks[:-1]
    if args and args[-1].startswith(b"/"):              # a pattern
        return (0.5, 0.5, 0.5)
    try:
        nums = [float(t) for t in args]
    except ValueError:
        return (0.0, 0.0, 0.0)
    if op in (b"G", b"g") and len(nums) == 1:
        return _clamp((nums[0],) * 3)
    if op in (b"RG", b"rg") and len(nums) == 3:
        return _clamp(tuple(nums))
    if op in (b"K", b"k") and len(nums) == 4:
        return _clamp(_cmyk(*nums))
    fam = _cs_family(doc, owner, raw_cs)
    if fam in ("Separation", "DeviceN"):
        t = sum(nums) / max(1, len(nums))
        return _clamp((1 - t,) * 3)
    if fam == "Lab" and len(nums) == 3:
        return _clamp((nums[0] / 100.0,) * 3)
    if fam == "Indexed":
        return (0.5, 0.5, 0.5)
    if len(nums) == 1:
        return _clamp((nums[0],) * 3)
    if len(nums) == 3:
        return _clamp(tuple(nums))
    if len(nums) == 4:
        return _clamp(_cmyk(*nums))
    return (0.0, 0.0, 0.0)


def _rgb(doc, ev: _Ev) -> tuple[tuple | None, tuple | None]:
    """(stroke rgb, fill rgb) of a path event (None when not stroked/filled)."""
    if ev._rgb is None:
        st = ev.state
        owner = ev.cont.owner
        s = _color(doc, owner, st.get("S"), st.get("CS")) if ev.stroke else None
        f = _color(doc, owner, st.get("F"), st.get("cs")) if ev.fill else None
        ev._rgb = (s, f)
    return ev._rgb


# ---------------------------------------------------------------------------
# page scan, units, classification, grouping (cached per page content)
# ---------------------------------------------------------------------------
class _Unit:
    """What a plain listing treats as one item: a painted path, or the
    invocation of a form that draws only paths."""

    __slots__ = ("paths", "evs", "ev", "bbox", "editable", "in_form", "form_xref", "lineish", "stroke", "fill",
                 "groupable", "ordinal")

    def __init__(self, ev: _Ev, evs: list[_Ev], in_form: bool, form_xref: int, editable: bool) -> None:
        self.ev = ev                      # the event an edit acts on (path, or form invocation)
        self.evs = evs                    # member path events
        self.paths = tuple(p.num for p in evs)
        x0 = min(p.bbox[0] for p in evs)
        y0 = min(p.bbox[1] for p in evs)
        x1 = max(p.bbox[2] for p in evs)
        y1 = max(p.bbox[3] for p in evs)
        self.bbox = (x0, y0, x1, y1)
        self.in_form = in_form
        self.form_xref = form_xref        # outermost form ancestor (0 = page content)
        self.editable = editable
        self.stroke = any(p.stroke for p in evs)
        self.fill = any(p.fill for p in evs)
        self.lineish = all(p.stroke and not p.fill and all(i[0] == "l" for i in p.items)
                           and not any((c or p.close) and len(pts) > 2 for pts, c in p.polys) for p in evs)
        self.groupable = ev.kind == "path"
        self.ordinal = 0


class _PageScan:
    __slots__ = ("page", "pm", "rect", "conts", "paths", "images", "units", "unit_of", "_band")

    def __init__(self) -> None:
        self.conts: list[_Cont] = []
        self.paths: list[_Ev] = []
        self.images: list[_Ev] = []
        self.units: list[_Unit] = []
        self.unit_of: list[_Unit | None] = []
        self._band = None


def _scan_key(doc: pymupdf.Document, index: int) -> bytes:
    pg = doc[index]
    h = hashlib.blake2b(digest_size=16)
    try:
        h.update(pg.read_contents())
    except Exception:
        pass
    owner = _resources_owner(doc, pg.xref)
    if owner:
        for k in ("Resources", "Resources/XObject", "Resources/ColorSpace"):
            t, v = _key(doc, owner, k)
            h.update(v.encode("utf-8", "replace"))
            if t == "xref":
                try:
                    h.update(doc.xref_object(_ref(v), compressed=True).encode("utf-8", "replace"))
                except Exception:
                    pass
    h.update(repr((index, pg.rotation, tuple(pg.mediabox), tuple(pg.cropbox))).encode())
    return h.digest()


def _cache(doc) -> dict:
    cache = getattr(doc, "_folio_graphics", None)
    if cache is None:
        cache = {"scan": {}, "list": {}}
        try:
            doc._folio_graphics = cache
        except Exception:
            pass
    return cache


def _page_scan(doc: pymupdf.Document, index: int, key: bytes | None = None) -> _PageScan:
    cache = _cache(doc)["scan"]
    if key is None:
        try:
            key = _scan_key(doc, index)
        except Exception:
            key = None
    if key is not None and key in cache:
        return cache[key]
    scan = _build_scan(doc, index)
    if key is not None:
        if len(cache) > 160:
            cache.clear()
        cache[key] = scan
    return scan


def _build_scan(doc: pymupdf.Document, index: int) -> _PageScan:
    page = doc[index]
    out = _PageScan()
    out.page = index
    out.rect = pymupdf.Rect(page.rect)
    sc = _Scanner(doc, page)
    out.pm = sc.pm
    try:
        sc.scan_page(page)
    except Exception:            # pragma: no cover - a broken stream lists nothing rather than crash
        sc = _Scanner(doc, page)
    out.conts, out.paths, out.images = sc.conts, sc.paths, sc.images
    out.unit_of = [None] * len(out.paths)
    forms: dict[int, _Unit] = {}
    for p in out.paths:
        if abs(_det(p.ctm)) < 1e-9:
            continue
        # the outermost invocation of the chain of path-only forms the path
        # is in (if any), and the outermost form ancestor
        top = None
        c = p.cont
        outer_form = 0
        climbing = True
        while c.kind == "form":
            if climbing and c.path_only:
                top = c.parent_ev
            else:
                climbing = False
            outer_form = c.xref
            c = c.parent_ev.cont
        if top is not None:
            u = forms.get(id(top))
            if u is None:
                evs = [q for q in out.paths if _below(q, top)]
                ok = top.cont.kind == "page" and not any(q.broken for q in evs)
                u = _Unit(top, evs, True, outer_form, ok)
                forms[id(top)] = u
                out.units.append(u)
            out.unit_of[p.num] = u
            continue
        editable = p.cont.kind == "page" and not p.clip and not p.broken
        u = _Unit(p, [p], p.cont.kind == "form", outer_form, editable)
        out.units.append(u)
        out.unit_of[p.num] = u
    for i, u in enumerate(out.units):
        u.ordinal = i
    return out


def _below(p: _Ev, form_ev: _Ev) -> bool:
    c = p.cont
    while c.kind == "form":
        if c.parent_ev is form_ev:
            return True
        c = c.parent_ev.cont
    return False


def _area(r) -> float:
    return max(0.0, r[2] - r[0]) * max(0.0, r[3] - r[1])


def _inter(a, b) -> tuple:
    return (max(a[0], b[0]), max(a[1], b[1]), min(a[2], b[2]), min(a[3], b[3]))


def _band_units(scan: _PageScan, doc) -> dict[int, tuple]:
    """unit ordinal -> (bbox, stroke?, fill?, stroke rgb, fill rgb) of the
    units lying in the top / bottom band of the page (for the template
    test). The band is 20 % of the page height rather than the planned 11 %:
    a slide's title bar and its accent rule (0–69 pt of 446 pt) must count."""
    if scan._band is None:
        h = scan.rect.height
        out = {}
        for u in scan.units:
            y0, y1 = u.bbox[1], u.bbox[3]
            top, bottom = scan.rect.y0, scan.rect.y1
            if y1 <= top + _BAND * h or y0 >= bottom - _BAND * h:
                s, f = _rgb(doc, u.evs[0])
                out[u.ordinal] = (u.bbox, u.stroke, u.fill, s, f)
        scan._band = out
    return scan._band


def _same_sig(a, b) -> bool:
    if a[1] != b[1] or a[2] != b[2]:
        return False
    if any(abs(x - y) > 0.5 for x, y in zip(a[0], b[0])):
        return False
    for ca, cb in ((a[3], b[3]), (a[4], b[4])):
        if (ca is None) != (cb is None):
            return False
        if ca is not None and any(abs(x - y) > 0.02 for x, y in zip(ca, cb)):
            return False
    return True


def _form_pages(doc) -> dict[int, int]:
    """xref -> number of pages whose resources name that XObject."""
    counts: dict[int, int] = {}
    for i in range(doc.page_count):
        try:
            owner = _resources_owner(doc, doc.page_xref(i))
            refs = set(ob._xobject_map(doc, owner).values()) if owner else set()
        except Exception:
            refs = set()
        for x in refs:
            counts[x] = counts.get(x, 0) + 1
    return counts


def _as_rect(ev: _Ev):
    """Display rect if the path is one axis-aligned rectangle, else None."""
    if len(ev.polys) != 1:
        return None
    pts = list(ev.polys[0][0])
    if len(pts) == 5 and abs(pts[0][0] - pts[4][0]) < 0.01 and abs(pts[0][1] - pts[4][1]) < 0.01:
        pts = pts[:4]
    if len(pts) != 4 or any(i[0] == "c" for i in ev.items):
        return None
    for i in range(4):
        (x0, y0), (x1, y1) = pts[i], pts[(i + 1) % 4]
        if abs(x0 - x1) > 0.05 and abs(y0 - y1) > 0.05:
            return None
    return ev.geom


def _rules(ev: _Ev) -> list | None:
    """Axis-aligned table rules of a path: [("h", y, x0, x1) | ("v", x, y0, y1)]
    or None if the path is not made of thin straight rules."""
    out = []
    if ev.stroke and ev.wdisp <= 3.0:
        for it in ev.items:
            if it[0] == "c":
                return None
            if it[0] == "q4":
                pts = it[1]
                segs = [(pts[i], pts[(i + 1) % 4]) for i in range(4)]
            else:
                segs = [(it[1], it[2])]
            for (x0, y0), (x1, y1) in segs:
                if abs(y0 - y1) <= 0.5:
                    out.append(("h", (y0 + y1) / 2, min(x0, x1), max(x0, x1)))
                elif abs(x0 - x1) <= 0.5:
                    out.append(("v", (x0 + x1) / 2, min(y0, y1), max(y0, y1)))
                else:
                    return None
        return out or None
    if ev.fill and not ev.stroke:
        r = _as_rect(ev)
        if r is None:
            return None
        w, h = r[2] - r[0], r[3] - r[1]
        if min(w, h) > 2.0:
            return None
        if w >= h:
            return [("h", (r[1] + r[3]) / 2, r[0], r[2])]
        return [("v", (r[0] + r[2]) / 2, r[1], r[3])]
    return None


def _clusters(values: list[float], gap: float = 1.0) -> int:
    n = 0
    last = None
    for v in sorted(values):
        if last is None or v - last > gap:
            n += 1
        last = v
    return n


def _tables(units: list[_Unit]) -> set[int]:
    """Ordinals of units that are table rules / cell backgrounds."""
    rules = []          # (orientation, coordinate, lo, hi, unit ordinal)
    for u in units:
        if u.ev.kind != "path":
            continue
        rs = _rules(u.ev)
        if rs:
            rules.extend((o, c, a, b, u.ordinal) for o, c, a, b in rs)
    if len(rules) < 4 or len(rules) > 6000:
        return set()
    hs = [r for r in rules if r[0] == "h"]
    vs = sorted((r for r in rules if r[0] == "v"), key=lambda r: r[1])
    if len(hs) < 2 or len(vs) < 2:
        return set()
    parent = list(range(len(rules)))
    idx = {id(r): i for i, r in enumerate(rules)}

    def find(i):
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    vx = [r[1] for r in vs]
    tol = 1.5
    for h in hs:
        _o, y, x0, x1, _u = h
        lo = bisect.bisect_left(vx, x0 - tol)
        hi = bisect.bisect_right(vx, x1 + tol)
        for v in vs[lo:hi]:
            if v[2] - tol <= y <= v[3] + tol:
                a, b = find(idx[id(h)]), find(idx[id(v)])
                if a != b:
                    parent[a] = b
    comps: dict[int, list] = {}
    for i, r in enumerate(rules):
        comps.setdefault(find(i), []).append(r)
    out: set[int] = set()
    boxes = []
    for rs in comps.values():
        hy = [r[1] for r in rs if r[0] == "h"]
        vx2 = [r[1] for r in rs if r[0] == "v"]
        nh, nv = _clusters(hy), _clusters(vx2)
        if nh >= 2 and nv >= 2 and (nh >= 3 or nv >= 3):
            out.update(r[4] for r in rs)
            xs = [r[2] for r in rs if r[0] == "h"] + [r[3] for r in rs if r[0] == "h"] + vx2
            ys = [r[2] for r in rs if r[0] == "v"] + [r[3] for r in rs if r[0] == "v"] + hy
            boxes.append((min(xs) - tol, min(ys) - tol, max(xs) + tol, max(ys) + tol))
    if boxes:
        for u in units:
            if u.ordinal in out or u.ev.kind != "path" or u.ev.stroke or not u.ev.fill:
                continue
            r = _as_rect(u.ev)
            if r is None:
                continue
            for b in boxes:
                if b[0] <= r[0] and b[1] <= r[1] and r[2] <= b[2] and r[3] <= b[3]:
                    out.add(u.ordinal)
                    break
    return out


class _Listing:
    __slots__ = ("scan", "kinds", "groups", "objs", "objs_all")

    def __init__(self, scan) -> None:
        self.scan = scan
        self.kinds: list[str] = []         # per unit: "" (selectable) | "background" | "template" | "table"
        self.groups: list[list[_Unit]] = []
        self.objs = None
        self.objs_all = None


def _classify(doc, index: int, scan: _PageScan, neighbours) -> list[str]:
    units = scan.units
    kinds = [""] * len(units)
    if not units:
        return kinds
    page_area = max(1e-6, _area(tuple(scan.rect)))
    prect = tuple(scan.rect)
    form_pages = None
    for u in units:
        a = _area(_inter(u.bbox, prect)) / page_area
        if (u.fill and a >= 0.5) or (u.stroke and not u.fill and a >= 0.7):
            kinds[u.ordinal] = "background"
            continue
        if u.form_xref:
            if form_pages is None:
                form_pages = _form_pages(doc)
            if form_pages.get(u.form_xref, 0) >= 2:
                kinds[u.ordinal] = "template"
                continue
    sigs = _band_units(scan, doc)
    if sigs:
        others = []
        for q in neighbours:
            others.extend(_band_units(_page_scan(doc, q), doc).values())
        for o, me in sigs.items():
            if not kinds[o] and any(_same_sig(me, other) for other in others):
                kinds[o] = "template"
    for o in _tables([u for u in units if not kinds[u.ordinal]]):
        kinds[o] = "table"
    return kinds


def _grow(r, d) -> tuple:
    return (r[0] - d, r[1] - d, r[2] + d, r[3] + d)


def _touch(a, b) -> bool:
    return a[0] <= b[2] and b[0] <= a[2] and a[1] <= b[3] and b[1] <= a[3]


def _geom_sig(u: _Unit) -> tuple:
    out = []
    for it in u.ev.items:
        pts = it[1] if it[0] == "q4" else it[1:]
        out.append((it[0],) + tuple((round(x, 1), round(y, 1)) for x, y in pts))
    return tuple(out)


_MAX_SEG_PAIRS = 40000


def _segments(ev: _Ev) -> list:
    """Flattened outline segments of a path in display space (closed
    subpaths closed – also when the paint operator closes or fills)."""
    segs = []
    for pts, closed in ev.polys:
        n = len(pts)
        if n == 1:
            segs.append((pts[0], pts[0]))
            continue
        last = n if (closed or ev.close or ev.fill) and n > 2 else n - 1
        for i in range(last):
            segs.append((pts[i], pts[(i + 1) % n]))
    return segs


def _orient(a, b, c) -> float:
    return (b[0] - a[0]) * (c[1] - a[1]) - (b[1] - a[1]) * (c[0] - a[0])


def _seg_seg_dist2(p0, p1, q0, q1) -> float:
    d1, d2 = _orient(q0, q1, p0), _orient(q0, q1, p1)
    d3, d4 = _orient(p0, p1, q0), _orient(p0, p1, q1)
    if ((d1 > 0 > d2) or (d1 < 0 < d2)) and ((d3 > 0 > d4) or (d3 < 0 < d4)):
        return 0.0                                   # proper crossing
    return min(_seg_dist2(p0[0], p0[1], q0[0], q0[1], q1[0], q1[1]),
               _seg_dist2(p1[0], p1[1], q0[0], q0[1], q1[0], q1[1]),
               _seg_dist2(q0[0], q0[1], p0[0], p0[1], p1[0], p1[1]),
               _seg_dist2(q1[0], q1[1], p0[0], p0[1], p1[0], p1[1]))


def _paths_touch(a: _Ev, b: _Ev) -> bool:
    """The outlines of two paths come within 1 pt + half their stroke widths
    (lying inside the other's fill without outline contact is no contact)."""
    reach = 1.0 + (a.wdisp + b.wdisp) / 2
    if not _touch(_grow(a.bbox, reach), b.bbox):
        return False
    sa, sb = _segments(a), _segments(b)
    if len(sa) * len(sb) > _MAX_SEG_PAIRS:
        return True                                  # too complex: the bbox test decides
    r2 = reach * reach
    for p0, p1 in sa:
        bx0, bx1 = min(p0[0], p1[0]) - reach, max(p0[0], p1[0]) + reach
        by0, by1 = min(p0[1], p1[1]) - reach, max(p0[1], p1[1]) + reach
        for q0, q1 in sb:
            if (max(q0[0], q1[0]) < bx0 or min(q0[0], q1[0]) > bx1
                    or max(q0[1], q1[1]) < by0 or min(q0[1], q1[1]) > by1):
                continue
            if _seg_seg_dist2(p0, p1, q0, q1) <= r2:
                return True
    return False


def _contact(u: _Unit, v: _Unit) -> bool:
    return any(_paths_touch(a, b) for a in u.evs for b in v.evs)


def _group(scan: _PageScan, kinds: list[str]) -> list[list[_Unit]]:
    groups: list[list[_Unit]] = []
    cur: list[_Unit] | None = None
    cur_box = None
    cur_boxes: list = []
    last = None
    for u in scan.units:
        if kinds[u.ordinal]:
            if last is not None and u.ev.cont is last.ev.cont:
                cur = None
            continue
        g = _grow(u.bbox, 1.0)
        join = (cur is not None and u.groupable and last is not None and last.groupable
                and u.ev.cont is last.ev.cont and u.ev.brk == last.ev.brk and _touch(g, cur_box)
                and any(_touch(g, b) and _contact(u, m) for b, m in zip(cur_boxes, cur)))
        if join:
            cur.append(u)
            cur_boxes.append(g)
            cur_box = (min(cur_box[0], g[0]), min(cur_box[1], g[1]), max(cur_box[2], g[2]), max(cur_box[3], g[3]))
        else:
            cur = [u]
            cur_boxes = [g]
            cur_box = g
            groups.append(cur)
        last = u
    # fill/stroke pairs of identical geometry belong together even when
    # something was drawn in between
    seen: dict = {}
    merged: dict[int, int] = {}
    for gi, g in enumerate(groups):
        if len(g) != 1 or not g[0].groupable:
            continue
        u = g[0]
        if u.ev.stroke == u.ev.fill:
            continue
        sig = (id(u.ev.cont), _geom_sig(u))
        other = seen.get(sig)
        if other is not None and groups[other][0].ev.stroke != u.ev.stroke:
            merged[gi] = other
            del seen[sig]
        else:
            seen[sig] = gi
    if merged:
        for gi, target in merged.items():
            groups[target] = groups[target] + groups[gi]
            groups[gi] = []
        groups = [g for g in groups if g]
    # a border drawn as separate straight lines around a filled rectangle
    # (LibreOffice callout boxes: fill, the text, then four border lines)
    fills = {}
    for gi, g in enumerate(groups):
        if len(g) == 1 and g[0].groupable and g[0].ev.fill and not g[0].ev.stroke:
            r = _as_rect(g[0].ev)
            if r is not None:
                fills.setdefault(id(g[0].ev.cont), []).append((gi, r))
    if fills:
        merged = {}
        for gi, g in enumerate(groups):
            if not all(u.groupable and u.lineish and _rules(u.ev) for u in g):
                continue
            box = (min(u.bbox[0] for u in g), min(u.bbox[1] for u in g),
                   max(u.bbox[2] for u in g), max(u.bbox[3] for u in g))
            for fi, r in fills.get(id(g[0].ev.cont), ()):
                if fi not in merged.values() and all(abs(a - b) <= 2.0 for a, b in zip(box, r)):
                    merged[gi] = fi
                    break
        for gi, target in merged.items():
            groups[target] = groups[target] + groups[gi]
            groups[gi] = []
        groups = [g for g in groups if g]
    return groups


def _listing(doc: pymupdf.Document, index: int) -> _Listing:
    cache = _cache(doc)["list"]
    try:
        keys = tuple(_scan_key(doc, q) if 0 <= q < doc.page_count else None for q in (index - 1, index, index + 1))
    except Exception:
        keys = None
    if keys is not None and keys in cache:
        return cache[keys]
    scan = _page_scan(doc, index, keys[1] if keys else None)
    lst = _Listing(scan)
    neighbours = [q for q in (index - 1, index + 1) if 0 <= q < doc.page_count]
    lst.kinds = _classify(doc, index, scan, neighbours)
    lst.groups = _group(scan, lst.kinds)
    if keys is not None:
        if len(cache) > 160:
            cache.clear()
        cache[keys] = lst
    return lst


# ---------------------------------------------------------------------------
# building GraphicObjects
# ---------------------------------------------------------------------------
def _items(evs: list[_Ev]) -> list:
    out = []
    P = pymupdf.Point
    for ev in evs:
        for it in ev.items:
            if it[0] == "l":
                out.append(("l", P(it[1]), P(it[2])))
            elif it[0] == "c":
                out.append(("c", P(it[1]), P(it[2]), P(it[3]), P(it[4])))
            else:
                pts = it[1]
                xs = [p[0] for p in pts]
                ys = [p[1] for p in pts]
                aligned = all(abs(pts[i][0] - pts[(i + 1) % 4][0]) < 1e-3 or abs(pts[i][1] - pts[(i + 1) % 4][1]) < 1e-3
                              for i in range(4))
                if aligned:
                    out.append(("re", pymupdf.Rect(min(xs), min(ys), max(xs), max(ys))))
                else:
                    ul, ur, lr, ll = (P(p) for p in pts)
                    out.append(("qu", pymupdf.Quad(ul, ur, ll, lr)))
    return out


def _make(doc, page: int, lst: _Listing, units: list[_Unit], kind: str | None = None,
          selectable: bool | None = None) -> GraphicObject:
    units = sorted(set(units), key=lambda u: u.ordinal)
    evs = sorted((p for u in units for p in u.evs), key=lambda p: p.num)
    paths = tuple(p.num for p in evs)
    x0 = min(u.bbox[0] for u in units)
    y0 = min(u.bbox[1] for u in units)
    x1 = max(u.bbox[2] for u in units)
    y1 = max(u.bbox[3] for u in units)
    rect = pymupdf.Rect(x0, y0, x1, y1)
    stroke = fill = None
    width = 0.0
    for p in evs:
        s, f = _rgb(doc, p)
        if stroke is None and s is not None:
            stroke, width = s, p.wdisp
        if fill is None and f is not None:
            fill = f
    if kind is None:
        classes = {lst.kinds[u.ordinal] for u in units}
        if len(units) == 1 and lst.kinds[units[0].ordinal]:
            kind = lst.kinds[units[0].ordinal]
        else:
            kind = "line" if all(u.lineish for u in units) else "shape"
        if selectable is None:
            selectable = classes == {""}
    if selectable is None:
        selectable = kind in ("shape", "line")
    return GraphicObject(
        page=page, index=paths[0] if paths else 0, paths=paths, rect=rect, kind=kind,
        stroke=stroke, fill=fill, width=width, selectable=selectable,
        editable=all(u.editable for u in units), in_form=any(u.in_form for u in units),
        items=_items(evs), key=(page, paths, tuple(round(v, 1) for v in rect)),
    )


def _objects(doc, page: int, lst: _Listing, all_items: bool) -> list[GraphicObject]:
    if all_items:
        if lst.objs_all is None:
            out = []
            for u in lst.scan.units:
                k = lst.kinds[u.ordinal]
                if k:
                    out.append(_make(doc, page, lst, [u], k, False))
            out.extend(_objects(doc, page, lst, False))
            out.sort(key=lambda o: o.index)
            lst.objs_all = out
        return lst.objs_all
    if lst.objs is None:
        lst.objs = [_make(doc, page, lst, g) for g in lst.groups]
    return lst.objs


def list_graphics(doc, page: int, *, all_items: bool = False) -> list[GraphicObject]:
    """Every selectable graphic on ``page`` in drawing order (bottom-most
    first). ``all_items`` also lists backgrounds, template graphics and table
    rules – each such path as its own object (Alt). The listing is cached
    per page content; never mutate the returned objects."""
    from .render import as_document

    d = as_document(doc)
    return list(_objects(d, page, _listing(d, page), all_items))


# ---------------------------------------------------------------------------
# hit-testing
# ---------------------------------------------------------------------------
def _seg_dist2(px, py, x0, y0, x1, y1) -> float:
    dx, dy = x1 - x0, y1 - y0
    L = dx * dx + dy * dy
    if L <= 1e-12:
        return (px - x0) ** 2 + (py - y0) ** 2
    t = max(0.0, min(1.0, ((px - x0) * dx + (py - y0) * dy) / L))
    qx, qy = x0 + t * dx, y0 + t * dy
    return (px - qx) ** 2 + (py - qy) ** 2


def _inside(polys, x, y, evenodd) -> bool:
    wn = 0
    cross = 0
    for pts, _closed in polys:
        n = len(pts)
        if n < 3:
            continue
        for i in range(n):
            x0, y0 = pts[i]
            x1, y1 = pts[(i + 1) % n]
            if (y0 <= y) != (y1 <= y):
                xi = x0 + (y - y0) * (x1 - x0) / (y1 - y0)
                if xi > x:
                    cross += 1
                    wn += 1 if y1 > y0 else -1
    return (cross % 2 == 1) if evenodd else wn != 0


def _path_hit(ev: _Ev, x: float, y: float, tol: float) -> bool:
    b = ev.bbox
    if not (b[0] - tol <= x <= b[2] + tol and b[1] - tol <= y <= b[3] + tol):
        return False
    if ev.fill and _inside(ev.polys, x, y, ev.evenodd):
        return True
    reach = (max(ev.wdisp, 0.5) / 2 if ev.stroke else 0.0) + tol
    r2 = reach * reach
    for pts, closed in ev.polys:
        n = len(pts)
        if n == 1:
            if (x - pts[0][0]) ** 2 + (y - pts[0][1]) ** 2 <= r2:
                return True
            continue
        last = n if (closed or ev.close or ev.fill) else n - 1
        for i in range(last):
            x0, y0 = pts[i]
            x1, y1 = pts[(i + 1) % n]
            if _seg_dist2(x, y, x0, y0, x1, y1) <= r2:
                return True
    return False


def graphic_at(doc, page: int, point, *, alt: bool = False, tolerance: float = 2.0) -> GraphicObject | None:
    """The topmost graphic at ``point`` (display space): inside a filled
    path, or within half the stroke width + ``tolerance`` of a stroke.
    ``alt`` also considers backgrounds, template graphics and table rules."""
    from .render import as_document

    d = as_document(doc)
    lst = _listing(d, page)
    objs = _objects(d, page, lst, alt)
    pt = pymupdf.Point(point)
    x, y = pt.x, pt.y
    paths = lst.scan.paths
    best, best_z = None, -1
    for o in objs:
        r = o.rect
        if not (r.x0 - tolerance <= x <= r.x1 + tolerance and r.y0 - tolerance <= y <= r.y1 + tolerance):
            continue
        for n in reversed(o.paths):
            if n > best_z and _path_hit(paths[n], x, y, tolerance):
                best, best_z = o, n
                break
    return best


def graphics_in(doc, page: int, rect, *, alt: bool = False) -> list[GraphicObject]:
    """Graphics lying completely inside ``rect`` (display space)."""
    r = pymupdf.Rect(rect)
    r.normalize()
    out = []
    for o in list_graphics(doc, page, all_items=alt):
        q = o.rect
        if r.x0 - 0.01 <= q.x0 and r.y0 - 0.01 <= q.y0 and q.x1 <= r.x1 + 0.01 and q.y1 <= r.y1 + 0.01:
            out.append(o)
    return out


def _close_rect(a, b, tol: float = 1.0) -> bool:
    return all(abs(x - y) <= tol for x, y in zip(a, b))


def _units_for(lst: _Listing, paths) -> list[_Unit] | None:
    scan = lst.scan
    if not paths or any(not (0 <= n < len(scan.paths)) for n in paths):
        return None
    units = []
    for n in paths:
        u = scan.unit_of[n]
        if u is None:
            return None
        if u not in units:
            units.append(u)
    return units


def find_graphic(doc, page: int, obj: GraphicObject) -> GraphicObject | None:
    """Relocate ``obj`` on the (possibly edited) page: same paths at the same
    place, else an object at the same place, else the same paths moved."""
    from .render import as_document

    d = as_document(doc)
    lst = _listing(d, page)
    units = _units_for(lst, obj.paths)
    cand = _make(d, page, lst, units, *(() if obj.selectable else (obj.kind, False))) if units else None
    if cand is not None and _close_rect(cand.rect, obj.rect):
        return cand
    for o in _objects(d, page, lst, True):
        if len(o.paths) == len(obj.paths) and _close_rect(o.rect, obj.rect):
            return o
    if cand is not None and abs(cand.rect.width - obj.rect.width) <= 1 and abs(cand.rect.height - obj.rect.height) <= 1:
        return cand
    return None


# ---------------------------------------------------------------------------
# edit helpers
# ---------------------------------------------------------------------------
def _resolve(doc, page: int, objs, *, need_editable: bool = True) -> tuple[_Listing, list[_Unit]]:
    if isinstance(objs, GraphicObject):
        objs = [objs]
    lst = _listing(doc, page)
    units: list[_Unit] = []
    for o in objs:
        us = _units_for(lst, o.paths)
        if us is not None:
            got = _make(doc, page, lst, us, o.kind, o.selectable)
            if not _close_rect(got.rect, o.rect):
                us = None
        if us is None:
            again = find_graphic(doc, page, o)
            us = _units_for(lst, again.paths) if again is not None else None
        if us is None:
            raise LookupError("graphic not found on the page")
        for u in us:
            if need_editable and not u.editable:
                raise ValueError("graphic cannot be edited")
            if u not in units:
                units.append(u)
    if not units:
        raise ValueError("nothing selected")
    return lst, units


def _outer(ev: _Ev, seqs: set, nested_mc: bool) -> _Block | None:
    best = None
    b = ev.block
    while b is not None and b.exclusive_to(seqs, nested_mc):
        best = b
        b = b.parent
    return best


def _cover(evs: list[_Ev], nested_mc: bool) -> list:
    """Disjoint spans covering ``evs`` (same container): ("block", block) for
    the outermost blocks drawing nothing else, ("run", [evs]) for bare,
    directly consecutive events (no operator in between, same block)."""
    seqs = {ev.seq for ev in evs}
    items = []
    bare = []
    for ev in sorted(evs, key=lambda e: e.seq):
        b = _outer(ev, seqs, nested_mc)
        if b is None:
            bare.append(ev)
        elif not any(x[1] is b for x in items):
            items.append(("block", b, b.q_start, b.Q_end))
    run: list[_Ev] = []
    for ev in bare:
        if run and ev.seq == run[-1].seq + 1 and ev.block is run[-1].block and ev.clean:
            run.append(ev)
        else:
            if run:
                items.append(("run", run, run[0].start, run[-1].end))
            run = [ev]
    if run:
        items.append(("run", run, run[0].start, run[-1].end))
    items.sort(key=lambda x: (x[2], -x[3]))
    out = []
    end = -1
    for it in items:
        if it[2] >= end:
            out.append(it)
            end = it[3]
        # else: nested inside the previous span (already covered)
    return out


def _tail(evs) -> bytes:
    """State operators inside bare path spans: re-emitted after a wrap or a
    removal, so the state seen by later content stays the same."""
    raw = [r for ev in evs for r in ev.inner]
    return b"\n".join(raw) + b"\n" if raw else b""


def _cm_edits(evs: list[_Ev], t: pymupdf.Matrix) -> list:
    data = evs[0].cont.data
    edits = []
    for kind, what, s, e in _cover(evs, nested_mc=False):
        if kind == "block":
            base = what.ctm
            if what.first_cm is not None:
                fs, fe, em = what.first_cm
                edits.append((fs, fe, _fmt(em * base * t * _inv(base)) + b" cm"))
            else:
                edits.append((what.q_end, what.q_end, b"\n" + _fmt(base * t * _inv(base)) + b" cm\n"))
        else:
            c = what[0].ctm
            f = c * t * _inv(c)
            edits.append((s, e, b"\nq " + _fmt(f) + b" cm\n" + data[s:e] + b"\nQ\n" + _tail(what)))
    return edits


def _removal(data: bytes, s: int, e: int) -> tuple[int, int, bytes]:
    """The edit that removes ``data[s:e]`` without leaving whitespace behind
    (one separating byte is swallowed) and without merging two tokens."""
    before = s == 0 or data[s - 1] in _WSB
    if before and e < len(data) and data[e] in _WSB:
        return s, e + 1, b""
    if before or e >= len(data) or data[e] in _WSB:
        return s, e, b""
    return s, e, b"\n"


def _delete_edits(evs: list[_Ev], *, force: bool = False) -> list:
    data = evs[0].cont.data
    edits = []
    for kind, what, s, e in _cover(evs, nested_mc=True):
        if kind == "block":
            edits.append(_removal(data, s, e))
            continue
        for ev in what:
            if ev.kind == "path" and ev.clip:
                if not force:
                    raise ValueError("clip paths cannot be deleted")
                ps, pe = ev.op_span
                edits.append((ps, pe, b"n"))       # keep the clip, paint nothing
            else:
                tail = _tail([ev])
                edits.append((ev.start, ev.end, tail) if tail else _removal(data, ev.start, ev.end))
    return edits


def _splice(data: bytes, edits: list) -> bytes:
    out, last = [], 0
    for s, e, rep in sorted(edits, key=lambda x: (x[0], x[1])):
        if s < last:
            raise RuntimeError("overlapping content edits")
        out.append(data[last:s])
        out.append(rep)
        last = e
    out.append(data[last:])
    return b"".join(out)


def _commit_all(doc: pymupdf.Document, pg: pymupdf.Page, edits: dict) -> None:
    """Apply edits per container: forms are edited in a private copy for this
    one invocation (deepest first), the page's content is rewritten once."""
    pending = {c: list(e) for c, e in edits.items() if e}
    while pending:
        cont = max(pending, key=lambda c: c.depth)
        ed = pending.pop(cont)
        data = _splice(cont.data, ed)
        if cont.kind == "page":
            _write_page_contents(doc, pg, data)
            continue
        new = doc.get_new_xref()
        doc.update_object(new, doc.xref_object(cont.xref, compressed=True))
        _set_stream(doc, new, data)
        parent = cont.parent_ev
        name = _register_xobject(doc, parent.cont, pg.xref, new)
        s, e = parent.name_span
        pending.setdefault(parent.cont, []).append((s, e, b"/" + name.encode("latin-1")))


def _by_cont(evs) -> dict:
    out: dict = {}
    for ev in evs:
        out.setdefault(ev.cont, []).append(ev)
    return out


def _is_translation(m: pymupdf.Matrix) -> bool:
    return abs(m.a - 1) < 1e-9 and abs(m.b) < 1e-9 and abs(m.c) < 1e-9 and abs(m.d - 1) < 1e-9


def _rebuild(doc, page: int, path_sets: list[tuple]) -> list[GraphicObject]:
    lst = _listing(doc, page)
    out = []
    for paths in path_sets:
        units = _units_for(lst, paths)
        if units:
            out.append(_make(doc, page, lst, units))
    return out


def _coord_edits(ev: _Ev, u: pymupdf.Matrix) -> list:
    """Rewrite the construction operands of a path by the user-space
    transform ``u`` (the line width is not touched)."""
    a, b, c, d, e, f = u.a, u.b, u.c, u.d, u.e, u.f
    aligned = abs(b) < 1e-9 and abs(c) < 1e-9

    def tp(x, y):
        return x * a + y * c + e, x * b + y * d + f

    def nums(*vs):
        return b" ".join(_num(v) for v in vs)

    edits = []
    for op, s, end, vals in ev.segs:
        if op == b"h":
            continue
        if op == b"re":
            x, y, w, h = vals
            if aligned:
                nx, ny = tp(x, y)
                rep = nums(nx, ny, w * a, h * d) + b" re"
            else:
                p = [tp(x, y), tp(x + w, y), tp(x + w, y + h), tp(x, y + h)]
                rep = (nums(*p[0]) + b" m " + nums(*p[1]) + b" l " + nums(*p[2]) + b" l "
                       + nums(*p[3]) + b" l h")
        else:
            pts = []
            for i in range(0, len(vals), 2):
                pts.extend(tp(vals[i], vals[i + 1]))
            rep = nums(*pts) + b" " + op
        edits.append((s, end, rep))
    return edits


# ---------------------------------------------------------------------------
# public edits
# ---------------------------------------------------------------------------
def _olist(objs) -> list:
    return [objs] if isinstance(objs, GraphicObject) else list(objs)


def _commit(pdf, label: str, page: int, edits: dict, *, merge: bool = False) -> None:
    """One undo step that applies planned ``edits`` (``merge``: fold into the
    previous step when that has the same label – a burst of nudges)."""
    fold = merge and pdf.history.undo_label == label
    with pdf.edit(label, CONTENT, (page,)):
        doc = pdf.doc
        _commit_all(doc, doc[page], edits)
    if fold:
        pdf.history.discard_last()      # the state before the burst stays the undo target


# Edits are planned on the current document (validation errors are raised
# before anything changes) and applied inside one pdf.edit (same document
# object, so the planned byte offsets are valid).
def _plan_transform(doc, page: int, objs, T: pymupdf.Matrix, keep_stroke: bool) -> dict:
    lst, units = _resolve(doc, page, objs)
    pm = lst.scan.pm
    t = pm * T * _inv(pm)
    edits: dict = {}
    if _is_translation(T) or not keep_stroke:
        for cont, evs in _by_cont([u.ev for u in units]).items():
            edits[cont] = _cm_edits(evs, t)
    else:
        for u in units:
            for ev in u.evs:
                c = ev.ctm
                edits.setdefault(ev.cont, []).extend(_coord_edits(ev, c * t * _inv(c)))
    return edits


def transform_graphics(pdf, page: int, objs, matrix, *, keep_stroke: bool = True, label: str | None = None,
                       merge: bool = False) -> list[GraphicObject]:
    """Apply the display-space ``matrix`` to graphics (one undo step). A pure
    translation inserts/rewrites one ``cm``; other transforms rewrite the
    path coordinates so the line width stays (``keep_stroke``), or use a
    ``cm`` that scales the stroke too (``keep_stroke=False``). ``merge=True``
    folds the step into the previous one when that has the same label.
    Returns the updated objects (same paths)."""
    T = pymupdf.Matrix(matrix)
    if abs(_det(T)) < 1e-12 or not all(math.isfinite(v) for v in T):
        raise ValueError("degenerate transformation")
    if label is None:
        label = (_label("graphics.undo.move", "Flyt grafik") if _is_translation(T)
                 else _label("graphics.undo.transform", "Transformér grafik"))
    olist = _olist(objs)
    edits = _plan_transform(pdf.doc, page, olist, T, keep_stroke)
    _commit(pdf, label, page, edits, merge=merge)
    return _rebuild(pdf.doc, page, [o.paths for o in olist])


def move_graphics(pdf, page: int, objs, dx: float, dy: float, *, merge: bool = False) -> list[GraphicObject]:
    """Move graphics by (dx, dy) display points (``merge``: see
    :func:`transform_graphics`)."""
    if not (math.isfinite(dx) and math.isfinite(dy)):
        raise ValueError("bad offset")
    return transform_graphics(pdf, page, objs, pymupdf.Matrix(1, 0, 0, 1, dx, dy),
                              label=_label("graphics.undo.move", "Flyt grafik"), merge=merge)


def scale_graphics(pdf, page: int, objs, old_rect, new_rect) -> list[GraphicObject]:
    """Scale graphics so their bounding box ``old_rect`` becomes ``new_rect``
    (display space); line widths are kept (O8)."""
    old = pymupdf.Rect(old_rect)
    new = pymupdf.Rect(new_rect)
    old.normalize()
    new.normalize()
    if old.is_empty or new.is_empty or not all(math.isfinite(v) for v in list(old) + list(new)):
        raise ValueError("empty rect")
    olist = _olist(objs)
    # the bboxes include half the stroke width, which does not scale
    hw = max((o.width / 2 for o in olist if o.stroke is not None), default=0.0)
    go, gn = pymupdf.Rect(old), pymupdf.Rect(new)
    if old.width > 2 * hw + 1e-6 and old.height > 2 * hw + 1e-6 and new.width > 2 * hw and new.height > 2 * hw:
        go = old + (hw, hw, -hw, -hw)
        gn = new + (hw, hw, -hw, -hw)
    sx, sy = gn.width / go.width, gn.height / go.height
    m = (pymupdf.Matrix(1, 0, 0, 1, -go.x0, -go.y0) * pymupdf.Matrix(sx, 0, 0, sy, 0, 0)
         * pymupdf.Matrix(1, 0, 0, 1, gn.x0, gn.y0))
    moved = abs(sx - 1) < 1e-9 and abs(sy - 1) < 1e-9
    label = (_label("graphics.undo.move", "Flyt grafik") if moved
             else _label("graphics.undo.scale", "Skalér grafik"))
    return transform_graphics(pdf, page, olist, m, keep_stroke=True, label=label)


def _plan_delete(doc, page: int, objs, *, force: bool) -> dict:
    _lst, units = _resolve(doc, page, objs, need_editable=not force)
    return {cont: _delete_edits(evs, force=force) for cont, evs in _by_cont([u.ev for u in units]).items()}


def delete_graphics(pdf, page: int, objs) -> None:
    """Remove graphics from the page content (one undo step)."""
    edits = _plan_delete(pdf.doc, page, _olist(objs), force=False)
    _commit(pdf, _label("graphics.undo.delete", "Slet grafik"), page, edits)


def delete_graphics_doc(doc, page: int, objs) -> None:
    """:func:`delete_graphics` on a raw document without undo (throw-away
    copies: clipboard, lifted rendering). Also removes non-editable graphics
    (a clip path stays a clip, a path in a mixed form goes from a private
    copy of the form)."""
    from .render import as_document

    d = as_document(doc)
    _commit_all(d, d[page], _plan_delete(d, page, _olist(objs), force=True))


def _paint_op(op: bytes, stroke: bool, fill: bool) -> bytes:
    """The paint operator with the given stroke/fill; closing and the fill
    rule are kept (a stroke added to a fill closes the outline like the fill)."""
    has_stroke, _f, close, evenodd = _PAINT[op]
    if stroke and fill:
        if close or not has_stroke:
            return b"b*" if evenodd else b"b"
        return b"B*" if evenodd else b"B"
    if stroke:
        return b"s" if close else b"S"
    return b"f*" if evenodd else b"f"


def _rgb_op(rgb, op: bytes) -> bytes:
    vals = tuple(float(v) for v in rgb)
    if len(vals) != 3 or not all(math.isfinite(v) for v in vals):
        raise ValueError("colour must be (r, g, b)")
    vals = tuple(min(1.0, max(0.0, v)) for v in vals)
    return b" ".join(_num(v) for v in vals) + b" " + op


def _plan_style(doc, page: int, objs, stroke, fill, width) -> dict:
    s_op = _rgb_op(stroke, b"RG") if stroke is not KEEP and stroke is not None else None
    f_op = _rgb_op(fill, b"rg") if fill is not KEEP and fill is not None else None
    _lst, units = _resolve(doc, page, objs)
    edits: dict = {}
    for cont, cevs in _by_cont([ev for u in units for ev in u.evs]).items():
        seqs = {ev.seq for ev in cevs}
        ed = edits.setdefault(cont, [])
        for ev in cevs:
            new_stroke = ev.stroke if stroke is KEEP else stroke is not None
            new_fill = ev.fill if fill is KEEP else fill is not None
            if not (new_stroke or new_fill):
                raise ValueError("a graphic needs a stroke or a fill")
            want: dict[str, bytes] = {}
            if s_op is not None:
                want["S"] = s_op
            if f_op is not None:
                want["F"] = f_op
            if width is not None:
                a, b, c, d = ev.M[:4]
                scale = math.sqrt(abs(a * d - b * c)) or 1.0
                want["w"] = _num(width / scale) + b" w"
            # inside a block that draws only the selection the operators are
            # set in place (rewriting the ones a previous change inserted);
            # otherwise the path is wrapped in q … Q once
            in_place = ev.block is not None and ev.block.exclusive_to(seqs, False)
            inside, before, ins = [], [], []
            for cat, raw in want.items():
                hit = None
                for pcat, s0, e0, op in reversed(ev.pre):
                    if pcat == cat and op not in (b"CS", b"cs"):
                        hit = (s0, e0)
                        break
                if hit is not None and hit[0] >= ev.start:
                    inside.append((hit[0], hit[1], raw))     # set inside the path's own span
                elif hit is not None and in_place:
                    before.append((hit[0], hit[1], raw))
                else:
                    ins.append(raw)
            new_op = _paint_op(ev.op, new_stroke, new_fill)
            ps, pe = ev.op_span
            if new_op != ev.op:
                inside.append((ps, pe, new_op))
            if in_place:
                ed.extend(before)
                ed.extend(inside)
                if ins:
                    ed.append((ev.start, ev.start, b" ".join(ins) + b"\n"))
            else:
                body = _splice(cont.data[ev.start:ev.end], [(a - ev.start, b - ev.start, r) for a, b, r in inside])
                head = b" ".join(ins)
                ed.append((ev.start, ev.end, b"\nq " + (head + b"\n" if head else b"") + body + b"\nQ\n"
                           + _tail([ev])))
    return edits


def set_graphic_style(pdf, page: int, objs, *, stroke=KEEP, fill=KEEP,
                      width: float | None = None) -> list[GraphicObject]:
    """Change stroke colour, fill colour (``None`` removes it, a colour adds
    it when missing) and/or display stroke width of graphics (O11; one undo
    step). Returns the updated objects."""
    olist = _olist(objs)
    if stroke is KEEP and fill is KEEP and width is None:
        return olist
    if width is not None and (not math.isfinite(width) or width < 0):
        raise ValueError("bad width")
    edits = _plan_style(pdf.doc, page, olist, stroke, fill, width)
    _commit(pdf, _label("graphics.undo.style", "Skift udseende"), page, edits)
    return _rebuild(pdf.doc, page, [o.paths for o in olist])


def _state_bytes(st: dict) -> bytes:
    return b"\n".join(st.values())


def _restack_edits(cont: _Cont, evs: list[_Ev], front: bool) -> list:
    """Remove the spans drawing ``evs`` and re-emit them, with the graphics
    state and CTM they need, at the end (front) or start (back) of the page."""
    data = cont.data
    edits = []
    chunks = []
    for kind, what, s, e in _cover(evs, nested_mc=True):
        if kind == "block":
            st, ctm = what.state, what.ctm
        else:
            st, ctm = what[0].state, what[0].ctm
        sb = _state_bytes(st)
        plain = _close_m(ctm, _IDENTITY)
        if kind == "block" and not sb and plain:
            chunks.append(data[s:e] + b"\n")       # already a self-contained q … Q
        else:
            head = b"q\n" + (sb + b"\n" if sb else b"") + (b"" if plain else _fmt(ctm) + b" cm\n")
            chunks.append(head + data[s:e] + b"\nQ\n")
        tail = _tail(what) if kind == "run" else b""
        edits.append((s, e, tail) if tail else _removal(data, s, e))
    blob = b"".join(chunks)
    n = len(data)
    sep = b"" if not data or data[-1] in _WSB else b"\n"
    if front:
        if cont.top_dirty or cont.q_depth_end:
            # the page leaves state/CTM/clip changed (or q open) at its end:
            # wrap it once so the re-emitted graphics start from the defaults
            edits.append((0, 0, b"q\n"))
            edits.append((n, n, sep + b"Q\n" * (cont.q_depth_end + 1) + blob))
        else:
            edits.append((n, n, sep + blob))
    else:
        edits.append((0, 0, blob))
    return edits


def _close_m(a, b, tol: float = 1e-9) -> bool:
    return all(abs(x - y) <= tol for x, y in zip(a, b))


def _restack_label(front: bool) -> str:
    return _label("graphics.undo.front", "Læg forrest") if front else _label("graphics.undo.back", "Læg bagerst")


def restack_graphics(pdf, page: int, objs, *, front: bool = True) -> list[GraphicObject]:
    """Bring graphics to the front (drawn last) or send them to the back
    (drawn first) of the page content (O12; one undo step). Only graphics of
    the page's own content can be restacked (``ValueError`` otherwise).
    Returns the updated objects (their path numbers change)."""
    olist = _olist(objs)
    lst, units = _resolve(pdf.doc, page, olist)
    evs = [u.ev for u in units]
    if any(ev.cont.kind != "page" for ev in evs):
        raise ValueError("only graphics in the page content can be restacked")
    cont = evs[0].cont
    moved = sorted(n for u in units for n in u.paths)
    total = len(lst.scan.paths)
    _commit(pdf, _restack_label(front), page, {cont: _restack_edits(cont, evs, front)})
    base = total - len(moved) if front else 0
    new_of = {n: base + i for i, n in enumerate(moved)}
    return _rebuild(pdf.doc, page, [tuple(sorted(new_of[n] for n in o.paths if n in new_of)) for o in olist])


def _restack_image(pdf, page: int, target, front: bool):
    """Implementation of :func:`pdfstudio.core.objects.restack_image`."""
    doc = pdf.doc
    _paint, obj = ob._locate(doc, page, target)
    scan = _page_scan(doc, page)
    if not (0 <= obj.index < len(scan.images)):
        raise LookupError("image placement not found")       # pragma: no cover - defensive
    ev = scan.images[obj.index]
    while ev.cont.kind == "form" and len(ev.cont.events) == 1 and ev.cont.mc_ok:
        ev = ev.cont.parent_ev                              # a form drawing only this image
    if ev.cont.kind != "page":
        raise ValueError("only images in the page content can be restacked")
    n_images = len(scan.images)
    _commit(pdf, _restack_label(front), page, {ev.cont: _restack_edits(ev.cont, [ev], front)})
    objs = ob.list_images(pdf.doc, page)
    idx = n_images - 1 if front else 0
    if 0 <= idx < len(objs):
        return objs[idx]
    raise LookupError("image placement vanished")                # pragma: no cover - defensive


def render_without_graphics(doc, page: int, objs, zoom: float, *, clip=None) -> pymupdf.Pixmap:
    """Render (part of) ``page`` as it looks without the given graphics –
    the page behind a lifted selection while it is dragged. Works on a
    throw-away copy; ``doc`` is not changed."""
    from .render import as_document, render_page

    src = as_document(doc)
    olist = [objs] if isinstance(objs, GraphicObject) else list(objs)
    tmp = pymupdf.open()
    try:
        tmp.insert_pdf(src, from_page=page, to_page=page, annots=True, links=False, widgets=True)
        if olist:
            # the copy has the same content stream, so the same path numbers
            delete_graphics_doc(tmp, 0, [dataclasses.replace(o, page=0) for o in olist])
        return render_page(tmp, 0, zoom, clip=clip)
    finally:
        tmp.close()


def strip_page_except(doc, page: int, *, keep_paths=(), keep_images=()) -> None:
    """Remove every painted path (any kind), shading, image placement (by
    ``ImageObject.index``) and form invocation from ``page`` except the kept
    ones – for the clipboard (throw-away documents, no undo). Text is not
    touched; a form invocation stays if it draws something kept (its other
    paths/images are then removed from a private copy of the form)."""
    from .render import as_document

    d = as_document(doc)
    scan = _page_scan(d, page)
    keep_p = set(keep_paths)
    keep_i = set(keep_images)
    kept: dict = {}

    def keeps(cont: _Cont) -> bool:
        hit = cont in kept
        if hit:
            return kept[cont]
        res = False
        for ev in cont.events:
            if ev.kind == "path" and ev.num in keep_p:
                res = True
            elif ev.kind in ("image", "inline") and ev.index is not None and ev.index in keep_i:
                res = True
            elif ev.kind == "form" and ev.child is not None and keeps(ev.child):
                res = True
        kept[cont] = res
        return res

    edits: dict = {}

    def visit(cont: _Cont) -> None:
        dels = []
        for ev in cont.events:
            if ev.kind == "path":
                if ev.num not in keep_p:
                    dels.append(ev)
            elif ev.kind in ("image", "inline"):
                if ev.index is None or ev.index not in keep_i:
                    dels.append(ev)
            elif ev.kind in ("sh", "other"):
                dels.append(ev)
            elif ev.kind == "form":
                if ev.child is not None and keeps(ev.child):
                    visit(ev.child)
                else:
                    dels.append(ev)
        if dels:
            edits[cont] = _delete_edits(dels, force=True)

    if scan.conts:
        visit(scan.conts[0])
        _commit_all(d, d[page], edits)
