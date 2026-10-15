"""Images that are part of the page content – list, hit-test, move, resize,
rotate, replace, delete and extract them (the "Markér objekt" tool).

How it works
------------
An image in a PDF page is not an object with an identity of its own: the
page's content stream *draws* an image XObject with ``/Name Do`` under the
current transformation matrix (CTM), which maps the image's unit square onto
the page. This module interprets the content stream(s) of a page – and the
Form XObjects they invoke – with a small tokenizer that tracks ``q``/``Q``/
``cm`` to find every image *placement* (and inline image) together with its
CTM. Edits are byte-precise splices into the content stream:

* **move / resize / rotate** – the placement's transform is changed by
  rewriting (or inserting) a single ``cm`` at the start of the outermost
  graphics-state block (``q … Q``) that draws nothing but that image, so a
  clipping path that crops the image (Word, LibreOffice) moves with it.
  Without such a block the ``Do`` is wrapped in ``q <m> cm … Q`` once; later
  edits rewrite that same ``cm``. The content stream therefore does not grow
  with repeated moves and the image data (XObject) is never copied or
  recompressed.
* **delete** – that block (or the bare ``Do``) is removed.
* **replace** – the new image is stored once as an XObject (JPEG kept as is,
  alpha kept as SMask) and drawn in place of the old one: same position in
  the stacking order, same orientation, fitted into the old frame.

Everything else in the content stream (text, vector art, other images,
marked content) stays byte-for-byte untouched; annotations are not page
content and are never affected. The same image XObject placed twice is two
independent placements (``ImageObject.index`` tells them apart).

Coordinates are *display space* (see CLAUDE.md). ``ImageObject.matrix`` maps
the image's unit square – (0, 0) is the image's top-left pixel corner,
(1, 1) the bottom-right one – onto the page, so rotated and mirrored
placements are described exactly; ``rect`` is its bounding box.

Limitations
-----------
* An image inside a Form XObject that draws nothing else is edited by
  transforming the form invocation (exact). An image inside a form that also
  draws other content (e.g. a letterhead template shared by every page) is
  edited in a private copy of that form for this one invocation, so other
  pages that use the template keep the original.
* Resizing a placement that is rotated by a non-multiple of 90° maps its
  bounding box onto the new rectangle: a non-uniform resize then skews the
  image (the UI keeps the aspect ratio, which keeps it a pure rotation).
* A transparency soft mask or clipping path set *outside* the image's own
  ``q … Q`` block stays where it is when the image moves. Replacing an image
  drops the old image's own crop/clip (the new image is shown whole).
* Images drawn by Type3 font glyphs or by annotation appearances (stamps)
  are not page content and are not listed.
"""

from __future__ import annotations

import math
import os
import re
from dataclasses import dataclass, field
from typing import Union

import pymupdf

from .document import CONTENT

__all__ = [
    "ImageObject",
    "ImageRef",
    "list_images",
    "image_at",
    "hit_test",
    "transform_image",
    "set_image_rect",
    "resize_image",
    "move_image",
    "rotate_image",
    "flip_image",
    "delete_image",
    "replace_image",
    "extract_image",
    "extract_image_file",
    "image_pixmap",
    "render_without_image",
]


def _label(key: str, default: str) -> str:
    try:
        from ..i18n import tr

        text = tr(key)
        return default if text == key else text
    except Exception:  # pragma: no cover - i18n is optional for core
        return default


# ---------------------------------------------------------------------------
# public data
# ---------------------------------------------------------------------------
@dataclass
class ImageObject:
    """One image placement on a page (display coordinates)."""

    page: int
    index: int                  # placement number on the page in drawing order (0 = bottom-most)
    xref: int                   # image XObject xref; 0 for an inline image
    name: str                   # resource name ("" for inline images)
    rect: pymupdf.Rect          # bounding box on the page
    quad: pymupdf.Quad          # exact corners (ul, ur, ll, lr = the image's own corners)
    matrix: pymupdf.Matrix      # unit square (0,0 = top-left pixel corner) -> display space
    width: int                  # pixels
    height: int
    has_alpha: bool = False     # SMask / Mask / SMaskInData
    bpc: int = 8
    colorspace: str = ""
    filter: str = ""            # e.g. "DCTDecode" (JPEG), "FlateDecode"
    inline: bool = False        # BI … ID … EI image inside the content stream
    in_form: bool = False       # drawn through a Form XObject
    smask: int = 0              # xref of the soft mask image, if any
    editable: bool = True

    # -- derived geometry -------------------------------------------------
    @property
    def rotation(self) -> float:
        """Clockwise angle (degrees, 0..360) of the image's top edge on screen."""
        m = self.matrix
        return math.degrees(math.atan2(m.b, m.a)) % 360.0

    @property
    def flipped(self) -> bool:
        """True when the image is shown mirrored."""
        m = self.matrix
        return m.a * m.d - m.b * m.c < 0

    @property
    def axis_aligned(self) -> bool:
        """Rotation is a multiple of 90° and the image is not skewed."""
        m = self.matrix
        eps = 1e-6 * max(1.0, abs(m.a), abs(m.b), abs(m.c), abs(m.d))
        return (abs(m.b) <= eps and abs(m.c) <= eps) or (abs(m.a) <= eps and abs(m.d) <= eps)

    @property
    def size_pt(self) -> tuple[float, float]:
        """Shown width/height along the image's own axes (pt)."""
        m = self.matrix
        return math.hypot(m.a, m.b), math.hypot(m.c, m.d)

    @property
    def dpi(self) -> tuple[float, float]:
        """Effective resolution on the page (pixels per inch)."""
        w, h = self.size_pt
        return (self.width * 72.0 / w if w > 0 else 0.0, self.height * 72.0 / h if h > 0 else 0.0)

    @property
    def key(self) -> tuple[int, int]:
        """(page, index) – stable across moves/resizes/replacements."""
        return self.page, self.index


ImageRef = Union[ImageObject, int]   # an ImageObject, or an image xref (topmost placement)


# ---------------------------------------------------------------------------
# small helpers
# ---------------------------------------------------------------------------
_IDENTITY = pymupdf.Matrix(1, 0, 0, 1, 0, 0)
_FLIP = pymupdf.Matrix(1, 0, 0, -1, 0, 1)        # fz image space (y down) <-> PDF image space (y up)
_DELIMS = rb"\x00\t\n\x0c\r ()<>\[\]{}/%"
_TOKEN = re.compile(
    rb"(?P<ws>[\x00\t\n\x0c\r ]+)"
    rb"|(?P<comment>%[^\r\n]*)"
    rb"|(?P<num>[+-]?(?:\d+\.?\d*|\.\d+))(?=[" + _DELIMS + rb"]|\Z)"
    rb"|(?P<name>/[^" + _DELIMS + rb"]*)"
    rb"|(?P<dopen><<)"
    rb"|(?P<dclose>>>)"
    rb"|(?P<hex><[^<>]*>)"
    rb"|(?P<aopen>\[)"
    rb"|(?P<aclose>\])"
    rb"|(?P<str>\()"
    rb"|(?P<op>[^" + _DELIMS + rb"]+)"
    rb"|(?P<other>[\s\S])"
)
_STR_SCAN = re.compile(rb"[()\\]")
_EI_AT = re.compile(rb"[\x00\t\n\x0c\r ]*EI(?=[" + _DELIMS + rb"]|\Z)")
_EI_SEARCH = re.compile(rb"[\x00\t\n\x0c\r ]EI(?=[" + _DELIMS + rb"]|\Z)")
_REFS = re.compile(r"/([^\s/<>\[\]()%{}]+)\s*(\d+)\s+(\d+)\s+R")
# An operator starts after whitespace or a bracket (never right after "/":
# that would be a name such as /Q) or at the very start.
_OP_START = rb"(?:(?<=[\x00\t\n\x0c\r ()<>\[\]{}])|(?<![\s\S]))"
_OP_END = rb"(?=[" + _DELIMS + rb"]|\Z)"
# The scanner only stops at what matters; the regex engine skips the rest.
# Strings are skipped in Python (nested parentheses), hex strings and
# comments here, so operators inside them are never seen.
_SCAN = re.compile(
    rb"\("
    rb"|%[^\r\n]*"
    rb"|<[0-9A-Fa-f\x00\t\n\x0c\r ]*>"
    rb"|" + _OP_START + rb"(q|Q|cm|Do|BI|BMC|BDC|EMC)" + _OP_END
)
# Everything that paints (an image, a form, a path, text, a shading).
# Counted only inside blocks around images; strings are not excluded, which
# can only over-count (= the safe, conservative direction).
_PAINT = re.compile(_OP_START + rb"""(?:S|s|f\*?|F|B\*?|b\*?|sh|Tj|TJ|'|"|Do|EI)""" + _OP_END)
_NUMBER = rb"([+-]?(?:\d+\.?\d*|\.\d+))"
_WS = rb"[\x00\t\n\x0c\r ]"
_CM_ARGS = re.compile(_OP_START + _NUMBER + (_WS + rb"+" + _NUMBER) * 5 + _WS + rb"*\Z")
_NAME_ARG = re.compile(rb"/([^" + _DELIMS + rb"]*)" + _WS + rb"*\Z")
_ARG_WINDOW = 256
_MAX_FORM_DEPTH = 10


def _skip_string(data: bytes, pos: int) -> int:
    """End of a literal string whose opening ``(`` ends at ``pos``."""
    depth = 1
    n = len(data)
    while pos < n:
        m = _STR_SCAN.search(data, pos)
        if m is None:
            return n
        c = data[m.start()]
        pos = m.end()
        if c == 0x5C:      # backslash escapes the next byte
            pos += 1
        elif c == 0x28:    # (
            depth += 1
        else:              # )
            depth -= 1
            if depth == 0:
                return pos
    return n


def _decode_name(raw: str) -> str:
    """PDF name without the slash, ``#xx`` escapes decoded."""
    if "#" not in raw:
        return raw
    return re.sub(r"#([0-9A-Fa-f]{2})", lambda m: chr(int(m.group(1), 16)), raw)


def _num(v: float) -> bytes:
    """A PDF real (never exponent notation) with enough precision."""
    if not math.isfinite(v):
        raise ValueError("non-finite number")
    a = abs(v)
    if a < 1e-10:
        return b"0"
    digits = max(4, min(10, 6 - int(math.floor(math.log10(a)))))
    s = f"{v:.{digits}f}".rstrip("0").rstrip(".")
    return (s if s not in ("", "-0") else "0").encode()


def _fmt(m: pymupdf.Matrix) -> bytes:
    return b" ".join(_num(x) for x in (m.a, m.b, m.c, m.d, m.e, m.f))


def _inv(m: pymupdf.Matrix) -> pymupdf.Matrix:
    r = pymupdf.Matrix()
    if r.invert(m):
        raise ValueError("degenerate transformation")
    return r


def _det(m: pymupdf.Matrix) -> float:
    return m.a * m.d - m.b * m.c


def _ref(value: str) -> int:
    try:
        return int(value.split()[0])
    except (ValueError, IndexError):
        return 0


def _key(doc: pymupdf.Document, xref: int, key: str) -> tuple[str, str]:
    try:
        return doc.xref_get_key(xref, key)
    except Exception:
        return "null", "null"


def _parse_refs(text: str) -> dict[str, int]:
    return {_decode_name(m.group(1)): int(m.group(2)) for m in _REFS.finditer(text or "")}


def _numbers(text: str) -> list[float]:
    try:
        return [float(x) for x in re.findall(r"[+-]?(?:\d+\.?\d*|\.\d+)", text or "")]
    except ValueError:
        return []


def _page_matrix(page: pymupdf.Page) -> pymupdf.Matrix:
    """PDF user space -> display space (cropbox origin, /Rotate applied).

    ``page.transformation_matrix`` ignores the cropbox offset on rotated
    pages, so the unrotated part is derived from the boxes here."""
    mb = page.mediabox
    cb = page.cropbox
    unrot = pymupdf.Matrix(1, 0, 0, -1, -cb.x0, mb.y1 - cb.y0)
    return unrot * page.rotation_matrix


# ---------------------------------------------------------------------------
# content stream interpretation
# ---------------------------------------------------------------------------
def _paints(data: bytes, start: int, end: int, limit: int = 2) -> int:
    """Number of painting operators in ``data[start:end]`` (stops at ``limit``)."""
    n = 0
    for _m in _PAINT.finditer(data, start, end):
        n += 1
        if n >= limit:
            break
    return n


class _Block:
    """A ``q … Q`` block inside one container."""

    __slots__ = ("container", "parent", "q_start", "q_end", "Q_start", "Q_end", "ctm", "first_cm", "mc",
                 "mc_ok", "_single")

    def __init__(self, container, parent, q_start, q_end, ctm, mc) -> None:
        self.container: _Container = container
        self.parent: _Block | None = parent
        self.q_start = q_start
        self.q_end = q_end
        self.Q_start: int | None = None
        self.Q_end: int | None = None
        self.ctm: pymupdf.Matrix = ctm
        self.first_cm: tuple[int, int, pymupdf.Matrix] | None = None   # a cm that is the block's first operator
        self.mc = mc                      # marked-content depth at q
        self.mc_ok = True
        self._single: bool | None = None

    def exclusive(self, nested_mc: bool) -> bool:
        """Closed and draws exactly one thing. ``nested_mc``: marked content
        must also be properly nested (needed to remove/replace the whole
        block; LibreOffice closes /Figure sequences inside the clip block)."""
        if self.Q_end is None or not (self.mc_ok or not nested_mc):
            return False
        if self._single is None:
            self._single = _paints(self.container.data, self.q_end, self.Q_start) == 1
        return self._single


class _Container:
    """The page's content, or one invocation of a Form XObject."""

    __slots__ = ("kind", "xref", "data", "xobjects", "parent_paint", "mc_ok", "_single")

    def __init__(self, kind, xref, data, xobjects, parent_paint) -> None:
        self.kind: str = kind              # "page" | "form"
        self.xref: int = xref              # page or form xref
        self.data: bytes = data
        self.xobjects: dict[str, int] = xobjects
        self.parent_paint: _Paint | None = parent_paint
        self.mc_ok = True
        self._single: bool | None = None

    @property
    def collapsible(self) -> bool:
        """A form that draws exactly one thing: transform its invocation instead."""
        if self.kind != "form" or not self.mc_ok:
            return False
        if self._single is None:
            self._single = _paints(self.data, 0, len(self.data)) == 1
        return self._single


class _Paint:
    """An operation that draws an image or invokes a form."""

    __slots__ = ("container", "start", "end", "ctm", "block", "kind", "xref", "name", "name_span", "info", "child")

    def __init__(self, container, start, end, ctm, block, kind, xref=0, name="", name_span=None) -> None:
        self.container: _Container = container
        self.start = start                  # first operand
        self.end = end                      # after the operator (after EI for inline images)
        self.ctm: pymupdf.Matrix = ctm      # CTM at the operator (before a form's /Matrix)
        self.block: _Block | None = block   # innermost enclosing q … Q
        self.kind: str = kind               # "image" | "inline" | "form"
        self.xref = xref
        self.name = name
        self.name_span: tuple[int, int] | None = name_span
        self.info: dict = {}
        self.child: _Container | None = None


class _Scanner:
    """Interprets a page's content (and its forms) to find image placements."""

    def __init__(self, doc: pymupdf.Document) -> None:
        self.doc = doc
        self.placements: list[_Paint] = []
        self._xinfo: dict[int, tuple[str, dict]] = {}

    # -- resources ----------------------------------------------------------
    def page_xobjects(self, page_xref: int) -> dict[str, int]:
        owner = _resources_owner(self.doc, page_xref)
        return _xobject_map(self.doc, owner) if owner else {}

    def xobject(self, xref: int) -> tuple[str, dict]:
        hit = self._xinfo.get(xref)
        if hit is not None:
            return hit
        doc = self.doc
        kind, info = "other", {}
        if xref > 0:
            sub = _key(doc, xref, "Subtype")[1]
            if sub == "/Image":
                kind, info = "image", _image_dict_info(doc, xref)
            elif sub == "/Form":
                kind = "form"
                nums = _numbers(_key(doc, xref, "Matrix")[1])
                info["matrix"] = pymupdf.Matrix(*nums) if len(nums) == 6 else pymupdf.Matrix(_IDENTITY)
                info["has_resources"] = _key(doc, xref, "Resources")[0] != "null"
        self._xinfo[xref] = (kind, info)
        return kind, info

    # -- running --------------------------------------------------------------
    def scan_page(self, page: pymupdf.Page) -> _Container:
        doc = self.doc
        parts = []
        for x in page.get_contents():
            try:
                parts.append(doc.xref_stream(x) or b"")
            except Exception:
                parts.append(b"")
        cont = _Container("page", page.xref, b"\n".join(parts), self.page_xobjects(page.xref), None)
        self._run(cont, pymupdf.Matrix(_IDENTITY), (), 0)
        return cont

    def _run(self, cont: _Container, ctm: pymupdf.Matrix, chain: tuple[int, ...], depth: int) -> None:
        data = cont.data
        search = _SCAN.search
        pos = 0
        stack: list[tuple[pymupdf.Matrix, _Block | None]] = []
        block: _Block | None = None
        pending: _Block | None = None     # the latest q, whose first operator may be a cm
        mc = 0
        while True:
            m = search(data, pos)
            if m is None:
                break
            s, e = m.span()
            op = m.group(1)
            if op is None:
                pos = _skip_string(data, e) if data[s] == 0x28 else e
                continue
            if op == b"q":
                stack.append((ctm, block))
                block = _Block(cont, block, s, e, ctm, mc)
                pending = block
            elif op == b"Q":
                if stack:
                    block.Q_start, block.Q_end = s, e
                    if mc != block.mc:
                        block.mc_ok = False
                    ctm, block = stack.pop()
                pending = None
            elif op == b"cm":
                w0 = max(0, s - _ARG_WINDOW)
                a = _CM_ARGS.search(data[w0:s])
                if a is not None:
                    mat = pymupdf.Matrix(*(float(x) for x in a.groups()))
                    start = w0 + a.start()
                    if pending is not None and not data[pending.q_end:start].strip(b"\x00\t\n\x0c\r "):
                        pending.first_cm = (start, e, mat)
                    ctm = mat * ctm
                pending = None
            elif op == b"Do":
                w0 = max(0, s - _ARG_WINDOW)
                a = _NAME_ARG.search(data[w0:s])
                if a is not None:
                    ns, ne = w0 + a.start(), w0 + a.end(1)
                    self._do(cont, data, ns, ne, e, ctm, block, chain, depth)
            elif op == b"BI":
                e = self._inline(cont, data, s, e, ctm, block)
            elif op == b"EMC":
                mc -= 1
                b = block
                while b is not None and b.mc > mc:   # closes a sequence opened outside the block
                    b.mc_ok = False
                    b = b.parent
                if mc < 0:
                    cont.mc_ok = False
            else:                                     # BMC / BDC
                mc += 1
            pos = e
        if mc != 0:
            cont.mc_ok = False

    def _do(self, cont, data, ns, ne, end, ctm, block, chain, depth) -> None:
        name = _decode_name(data[ns + 1:ne].decode("latin-1"))
        xref = cont.xobjects.get(name, 0)
        kind, info = self.xobject(xref)
        if kind == "image":
            p = _Paint(cont, ns, end, ctm, block, "image", xref, name, (ns, ne))
            p.info = info
            self.placements.append(p)
        elif kind == "form" and depth < _MAX_FORM_DEPTH and xref not in chain:
            p = _Paint(cont, ns, end, ctm, block, "form", xref, name, (ns, ne))
            try:
                stream = self.doc.xref_stream(xref) or b""
            except Exception:
                stream = b""
            xobjects = _xobject_map(self.doc, xref) if info.get("has_resources") else cont.xobjects
            child = _Container("form", xref, stream, xobjects, p)
            p.child = child
            self._run(child, info["matrix"] * ctm, chain + (xref,), depth + 1)

    def _inline(self, cont, data, bi_start, bi_end, ctm, block) -> int:
        """Parse ``BI <dict> ID <data> EI``; returns the offset after EI."""
        n = len(data)
        pos = bi_end
        toks: list[tuple[str, bytes]] = []
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
        params = _inline_params(toks)
        ei_end = _find_ei(data, id_end + 1, params)
        p = _Paint(cont, bi_start, ei_end, ctm, block, "inline")
        p.info = params
        self.placements.append(p)
        return ei_end


_INLINE_KEYS = {"W": "Width", "H": "Height", "BPC": "BitsPerComponent", "CS": "ColorSpace", "F": "Filter",
                "IM": "ImageMask", "D": "Decode", "DP": "DecodeParms", "I": "Interpolate", "L": "Length"}
_INLINE_CS = {"G": 1, "DeviceGray": 1, "RGB": 3, "DeviceRGB": 3, "CMYK": 4, "DeviceCMYK": 4, "I": 1, "Indexed": 1}


def _inline_params(toks: list[tuple[str, bytes]]) -> dict:
    """The dictionary of an inline image (abbreviations expanded)."""
    out: dict = {}
    i = 0
    while i < len(toks):
        kind, raw = toks[i]
        i += 1
        if kind != "name" or i >= len(toks):
            continue
        key = _decode_name(raw[1:].decode("latin-1"))
        key = _INLINE_KEYS.get(key, key)
        kind, raw = toks[i]
        i += 1
        if kind == "aopen":                     # an array value: keep its first name (filter/colour space)
            vals = []
            while i < len(toks) and toks[i][0] != "aclose":
                vals.append(toks[i][1])
                i += 1
            i += 1
            raw = next((v for v in vals if v.startswith(b"/")), b"")
        val = raw.decode("latin-1")
        if val.startswith("/"):
            val = _decode_name(val[1:])
        out[key] = val
    return out


def _find_ei(data: bytes, start: int, params: dict) -> int:
    """Offset after the ``EI`` that ends an inline image whose data starts at
    ``start`` (uses /Length or the unfiltered size first, then a search)."""
    n = len(data)
    candidates = []
    try:
        if "Length" in params:
            candidates.append(start + int(float(params["Length"])))
        if "Filter" not in params:
            w, h = int(float(params.get("Width", 0))), int(float(params.get("Height", 0)))
            if params.get("ImageMask") == "true":
                comps, bpc = 1, 1
            else:
                comps = _INLINE_CS.get(str(params.get("ColorSpace", "")), 0)
                bpc = int(float(params.get("BitsPerComponent", 8)))
            if w > 0 and h > 0 and comps:
                candidates.append(start + (w * comps * bpc + 7) // 8 * h)
    except (TypeError, ValueError):
        pass
    for c in candidates:
        if c <= n:
            m = _EI_AT.match(data, c)
            if m is not None:
                return m.end()
    for m in _EI_SEARCH.finditer(data, start):
        tail = data[m.end():m.end() + 48]
        if all(b in b"\t\n\r " or 0x20 <= b < 0x7F for b in tail):
            return m.end()
    return n


def _image_dict_info(doc: pymupdf.Document, xref: int) -> dict:
    def num(key: str, default: int = 0) -> int:
        vals = _numbers(_key(doc, xref, key)[1])
        return int(vals[0]) if vals else default

    cs_t, cs = _key(doc, xref, "ColorSpace")
    if cs_t == "xref":
        try:
            cs = doc.xref_object(_ref(cs), compressed=True)
        except Exception:
            cs = ""
    csname = (re.findall(r"/([A-Za-z0-9]+)", cs or "") or [""])[0]
    filt = re.findall(r"/([A-Za-z0-9]+)", _key(doc, xref, "Filter")[1] or "")
    smask_t, smask = _key(doc, xref, "SMask")
    mask_t, _mask = _key(doc, xref, "Mask")
    sid = num("SMaskInData")
    image_mask = _key(doc, xref, "ImageMask")[1] == "true"
    return {
        "width": num("Width"),
        "height": num("Height"),
        "bpc": num("BitsPerComponent", 1 if image_mask else 8),
        "colorspace": "ImageMask" if image_mask else csname,
        "filter": filt[-1] if filt else "",
        "smask": _ref(smask) if smask_t == "xref" else 0,
        "has_alpha": smask_t == "xref" or mask_t not in ("null",) or sid > 0,
    }


def _resources_owner(doc: pymupdf.Document, page_xref: int) -> int:
    """The page or ancestor (/Pages node) that holds the page's /Resources."""
    x = page_xref
    for _ in range(64):
        if _key(doc, x, "Resources")[0] != "null":
            return x
        t, v = _key(doc, x, "Parent")
        if t != "xref":
            return 0
        x = _ref(v)
    return 0


def _xobject_map(doc: pymupdf.Document, owner: int) -> dict[str, int]:
    t, v = _key(doc, owner, "Resources/XObject")
    if t == "xref":
        try:
            v = doc.xref_object(_ref(v), compressed=True)
        except Exception:
            return {}
    elif t != "dict":
        return {}
    return _parse_refs(v)


def _xobject_holder(doc: pymupdf.Document, owner: int, page_xref: int) -> tuple[int, str]:
    """(xref, key prefix) of the dictionary to add an XObject name to, for
    content owned by ``owner`` (a page or a form). Creates /Resources and
    /XObject dictionaries as needed without breaking inheritance."""
    if _key(doc, owner, "Resources")[0] == "null":
        if owner != page_xref:              # a form without resources uses the page's
            return _xobject_holder(doc, page_xref, page_xref)
        anc = _resources_owner(doc, owner)
        if anc and anc != owner:            # inherited: give the page the same resources first
            t, v = _key(doc, anc, "Resources")
            doc.xref_set_key(owner, "Resources", v)
        else:
            doc.xref_set_key(owner, "Resources", "<<>>")
    t, v = _key(doc, owner, "Resources")
    if t == "xref":
        res = _ref(v)
        t2, v2 = _key(doc, res, "XObject")
        if t2 == "xref":
            return _ref(v2), ""
        if t2 != "dict":
            doc.xref_set_key(res, "XObject", "<<>>")
        return res, "XObject/"
    t2, v2 = _key(doc, owner, "Resources/XObject")
    if t2 == "xref":
        return _ref(v2), ""
    if t2 != "dict":
        doc.xref_set_key(owner, "Resources/XObject", "<<>>")
    return owner, "Resources/XObject/"


def _register_xobject(doc: pymupdf.Document, cont: _Container, page_xref: int, xref: int) -> str:
    """A resource name under which ``cont``'s content can draw ``xref``."""
    owner = cont.xref
    holder, prefix = _xobject_holder(doc, owner, page_xref)
    if prefix:
        t, v = _key(doc, holder, prefix[:-1])
        names = _parse_refs(v if t == "dict" else "")
    else:
        names = _parse_refs(doc.xref_object(holder, compressed=True))
    for name, x in names.items():
        if x == xref and re.fullmatch(r"[A-Za-z0-9_.\-]+", name):
            return name
    k = 0
    while f"FolioIm{k}" in names or f"FolioIm{k}" in cont.xobjects:
        k += 1
    name = f"FolioIm{k}"
    doc.xref_set_key(holder, prefix + name, f"{xref} 0 R")
    return name


# ---------------------------------------------------------------------------
# listing
# ---------------------------------------------------------------------------
def _scan(doc: pymupdf.Document, page_index: int) -> tuple[list[_Paint], list[ImageObject]]:
    page = doc[page_index]
    try:
        has_xobject_images = bool(doc.get_page_images(page_index, full=True))
    except Exception:
        has_xobject_images = True
    scanner = _Scanner(doc)
    if not has_xobject_images:
        quick = b"".join((doc.xref_stream(x) or b"") for x in page.get_contents())
        if b"BI" not in quick:
            return [], []
    scanner.scan_page(page)
    pm = _page_matrix(page)
    paints: list[_Paint] = []
    objs: list[ImageObject] = []
    for p in scanner.placements:
        ctm = p.ctm
        if abs(_det(ctm)) < 1e-9:
            continue          # degenerate: draws nothing visible
        disp = _FLIP * ctm * pm
        unit = pymupdf.Rect(0, 0, 1, 1)
        info = p.info
        if p.kind == "inline":
            cs = str(info.get("ColorSpace", ""))
            mask = info.get("ImageMask") == "true"
            filt = str(info.get("Filter", ""))
            vals = {
                "width": int(float(info.get("Width", 0) or 0)),
                "height": int(float(info.get("Height", 0) or 0)),
                "bpc": 1 if mask else int(float(info.get("BitsPerComponent", 8) or 8)),
                "colorspace": "ImageMask" if mask else cs,
                "filter": {"DCT": "DCTDecode", "Fl": "FlateDecode"}.get(filt, filt),
                "smask": 0,
                "has_alpha": False,
            }
        else:
            vals = info
        obj = ImageObject(
            page=page_index, index=len(objs), xref=p.xref, name=p.name,
            rect=unit * disp, quad=unit.quad * disp, matrix=disp,
            width=int(vals.get("width", 0)), height=int(vals.get("height", 0)),
            has_alpha=bool(vals.get("has_alpha")), bpc=int(vals.get("bpc", 8)),
            colorspace=str(vals.get("colorspace", "")), filter=str(vals.get("filter", "")),
            inline=p.kind == "inline", in_form=p.container.kind == "form",
            smask=int(vals.get("smask", 0)),
        )
        paints.append(p)
        objs.append(obj)
    return paints, objs


def list_images(doc: pymupdf.Document, page: int) -> list[ImageObject]:
    """Every image placement on ``page`` in drawing order (bottom-most first)."""
    return _scan(doc, page)[1]


def image_at(doc: pymupdf.Document, page: int, point, *, tolerance: float = 0.0) -> ImageObject | None:
    """The topmost image whose (possibly rotated) outline contains ``point``.
    ``tolerance`` (pt) grows every outline."""
    return hit_test(list_images(doc, page), point, tolerance=tolerance)


def hit_test(images: list[ImageObject], point, *, tolerance: float = 0.0) -> ImageObject | None:
    """:func:`image_at` on an already listed page (e.g. a cached listing)."""
    pt = pymupdf.Point(point)
    for obj in reversed(images):
        if _contains(obj, pt, tolerance):
            return obj
    return None


def _contains(obj: ImageObject, pt: pymupdf.Point, tol: float) -> bool:
    if not obj.rect.is_empty and not (obj.rect + (-tol, -tol, tol, tol)).contains(pt):
        return False
    try:
        u = pt * _inv(obj.matrix)
    except ValueError:
        return False
    w, h = obj.size_pt
    tu = tol / w if w > 0 else 0.0
    tv = tol / h if h > 0 else 0.0
    return -tu <= u.x <= 1 + tu and -tv <= u.y <= 1 + tv


# ---------------------------------------------------------------------------
# locating a placement for an edit
# ---------------------------------------------------------------------------
def _close(a: pymupdf.Matrix, b: pymupdf.Matrix, tol: float = 0.05) -> bool:
    return all(abs(x - y) <= tol for x, y in zip(a, b))


def _locate(doc: pymupdf.Document, page: int, target: ImageRef) -> tuple[_Paint, ImageObject]:
    paints, objs = _scan(doc, page)
    if isinstance(target, ImageObject):
        # xrefs may be renumbered (saving collects garbage): the placement's
        # index and transform identify it first
        i = target.index
        if 0 <= i < len(objs) and _close(objs[i].matrix, target.matrix):
            return paints[i], objs[i]
        for p, o in zip(paints, objs):
            if o.xref == target.xref and _close(o.matrix, target.matrix):
                return p, o
        same = [k for k, o in enumerate(objs) if _close(o.matrix, target.matrix)]
        if len(same) == 1:
            return paints[same[0]], objs[same[0]]
        if 0 <= i < len(objs) and objs[i].xref == target.xref and objs[i].xref:
            return paints[i], objs[i]
        raise LookupError("image placement not found")
    xref = int(target)
    hits = [k for k, o in enumerate(objs) if o.xref == xref and xref > 0]
    if not hits:
        raise LookupError(f"image {xref} is not placed on page {page}")
    return paints[hits[-1]], objs[hits[-1]]


def _edit_level(p: _Paint) -> _Paint:
    """The paint to edit: a form that draws nothing but this image is
    handled as the image itself (its invocation is transformed)."""
    while p.container.collapsible and p.container.parent_paint is not None:
        p = p.container.parent_paint
    return p


def _exclusive_block(p: _Paint, *, nested_mc: bool) -> _Block | None:
    """The outermost ``q … Q`` block around ``p`` that draws only ``p``
    (see :meth:`_Block.exclusive`)."""
    best = None
    b = p.block
    while b is not None and b.exclusive(nested_mc):
        best = b
        b = b.parent
    return best


# ---------------------------------------------------------------------------
# writing
# ---------------------------------------------------------------------------
def _splice(data: bytes, edits: list[tuple[int, int, bytes]]) -> bytes:
    out, last = [], 0
    for s, e, rep in sorted(edits, key=lambda x: (x[0], x[1])):
        out.append(data[last:s])
        out.append(rep)
        last = e
    out.append(data[last:])
    return b"".join(out)


def _set_stream(doc: pymupdf.Document, xref: int, data: bytes) -> None:
    for key in ("Filter", "DecodeParms"):
        if _key(doc, xref, key)[0] != "null":
            doc.xref_set_key(xref, key, "null")
    doc.update_stream(xref, data, compress=True)


def _contents_shared(doc: pymupdf.Document, page_xref: int, xref: int) -> bool:
    pat = re.compile(rf"(?<!\d){xref} 0 R")
    for i in range(doc.page_count):
        px = doc.page_xref(i)
        if px == page_xref:
            continue
        t, v = _key(doc, px, "Contents")
        if t == "xref" and _ref(v) != xref:
            try:
                v = doc.xref_object(_ref(v), compressed=True)   # an indirect array
            except Exception:
                continue
        if pat.search(v or ""):
            return True
    return False


def _write_page_contents(doc: pymupdf.Document, page: pymupdf.Page, data: bytes) -> None:
    """Store ``data`` as the page's single content stream (reusing the first
    stream object unless another page shares it)."""
    xrefs = page.get_contents()
    target = xrefs[0] if xrefs and not _contents_shared(doc, page.xref, xrefs[0]) else 0
    if not target:
        target = doc.get_new_xref()
        doc.update_object(target, "<<>>")
    _set_stream(doc, target, data)
    doc.xref_set_key(page.xref, "Contents", f"{target} 0 R")


def _commit(doc: pymupdf.Document, page: pymupdf.Page, cont: _Container, edits: list[tuple[int, int, bytes]]) -> None:
    data = _splice(cont.data, edits)
    if cont.kind == "page":
        _write_page_contents(doc, page, data)
        return
    # a form that also draws other things: edit a private copy for this invocation
    new = doc.get_new_xref()
    doc.update_object(new, doc.xref_object(cont.xref, compressed=True))
    _set_stream(doc, new, data)
    parent = cont.parent_paint
    name = _register_xobject(doc, parent.container, page.xref, new)
    s, e = parent.name_span
    _commit(doc, page, parent.container, [(s, e, b"/" + name.encode("latin-1"))])


def _transform_edit(p: _Paint, t: pymupdf.Matrix) -> list[tuple[int, int, bytes]]:
    """Edits that transform what ``p`` draws by ``t`` (PDF user space). Only
    a ``cm`` is inserted/rewritten, so marked content need not nest."""
    blk = _exclusive_block(p, nested_mc=False)
    if blk is not None:
        b = blk.ctm
        if blk.first_cm is not None:
            s, e, em = blk.first_cm
            f = em * b * t * _inv(b)
            return [(s, e, _fmt(f) + b" cm")]
        f = b * t * _inv(b)
        return [(blk.q_end, blk.q_end, b"\n" + _fmt(f) + b" cm\n")]
    c = p.ctm
    f = c * t * _inv(c)
    body = p.container.data[p.start:p.end]
    return [(p.start, p.end, b"\nq " + _fmt(f) + b" cm\n" + body + b"\nQ\n")]


def _check_rect(rect) -> pymupdf.Rect:
    r = pymupdf.Rect(rect)
    r.normalize()
    if r.is_empty or r.is_infinite or not all(math.isfinite(v) for v in r):
        raise ValueError("empty rect")
    return r


# ---------------------------------------------------------------------------
# public edits
# ---------------------------------------------------------------------------
def transform_image(pdf, page: int, target: ImageRef, matrix, *, label: str | None = None,
                    merge: bool = False) -> ImageObject:
    """Apply the display-space ``matrix`` to an image placement (one undo
    step). ``merge=True`` folds the step into the previous one when that has
    the same label (a burst of keyboard nudges = one undo step). Returns the
    updated :class:`ImageObject`."""
    t_disp = pymupdf.Matrix(matrix)
    if abs(_det(t_disp)) < 1e-12:
        raise ValueError("degenerate transformation")
    label = label or _label("objects.undo.move", "Flyt billede")
    fold = merge and pdf.history.undo_label == label
    with pdf.edit(label, CONTENT, (page,)):
        doc = pdf.doc
        pg = doc[page]
        paint, obj = _locate(doc, page, target)
        pm = _page_matrix(pg)
        t = pm * t_disp * _inv(pm)
        p = _edit_level(paint)
        _commit(doc, pg, p.container, _transform_edit(p, t))
    if fold:
        pdf.history.discard_last()      # the state before the burst stays the undo target
    return _after(pdf.doc, page, obj)


def _after(doc: pymupdf.Document, page: int, obj: ImageObject) -> ImageObject:
    objs = list_images(doc, page)
    if 0 <= obj.index < len(objs):
        return objs[obj.index]
    raise LookupError("image placement vanished")  # pragma: no cover - defensive


def set_image_rect(pdf, page: int, target: ImageRef, rect) -> ImageObject:
    """Move/scale the placement so its bounding box becomes ``rect``
    (display space). Orientation (rotation, mirroring) is kept."""
    new = _check_rect(rect)
    paint, obj = _locate(pdf.doc, page, target)
    old = obj.rect
    if old.width <= 0 or old.height <= 0:
        raise ValueError("image has no area")
    sx, sy = new.width / old.width, new.height / old.height
    m = (pymupdf.Matrix(1, 0, 0, 1, -old.x0, -old.y0) * pymupdf.Matrix(sx, 0, 0, sy, 0, 0)
         * pymupdf.Matrix(1, 0, 0, 1, new.x0, new.y0))
    moved = abs(sx - 1) < 1e-9 and abs(sy - 1) < 1e-9
    label = _label("objects.undo.move" if moved else "objects.undo.resize",
                   "Flyt billede" if moved else "Skalér billede")
    return transform_image(pdf, page, obj, m, label=label)


resize_image = set_image_rect


def move_image(pdf, page: int, target: ImageRef, dx: float, dy: float, *, merge: bool = False) -> ImageObject:
    """Move the placement by (dx, dy) display points (``merge``: see
    :func:`transform_image`)."""
    if not (math.isfinite(dx) and math.isfinite(dy)):
        raise ValueError("bad offset")
    return transform_image(pdf, page, target, pymupdf.Matrix(1, 0, 0, 1, dx, dy),
                           label=_label("objects.undo.move", "Flyt billede"), merge=merge)


def rotate_image(pdf, page: int, target: ImageRef, degrees: float = 90) -> ImageObject:
    """Rotate the placement clockwise (on screen) about its centre."""
    _paint, obj = _locate(pdf.doc, page, target)
    r = obj.rect
    c = pymupdf.Point((r.x0 + r.x1) / 2, (r.y0 + r.y1) / 2)
    m = pymupdf.Matrix(1, 0, 0, 1, -c.x, -c.y) * pymupdf.Matrix(degrees) * pymupdf.Matrix(1, 0, 0, 1, c.x, c.y)
    return transform_image(pdf, page, obj, m, label=_label("objects.undo.rotate", "Rotér billede"))


def flip_image(pdf, page: int, target: ImageRef, *, horizontal: bool = True) -> ImageObject:
    """Mirror the placement left/right (or top/bottom) in place."""
    _paint, obj = _locate(pdf.doc, page, target)
    r = obj.rect
    c = pymupdf.Point((r.x0 + r.x1) / 2, (r.y0 + r.y1) / 2)
    s = pymupdf.Matrix(-1, 0, 0, 1, 0, 0) if horizontal else pymupdf.Matrix(1, 0, 0, -1, 0, 0)
    m = pymupdf.Matrix(1, 0, 0, 1, -c.x, -c.y) * s * pymupdf.Matrix(1, 0, 0, 1, c.x, c.y)
    return transform_image(pdf, page, obj, m, label=_label("objects.undo.flip", "Spejlvend billede"))


def delete_image(pdf, page: int, target: ImageRef) -> None:
    """Remove one image placement from the page content (other placements
    of the same image and all other content are kept)."""
    with pdf.edit(_label("objects.undo.delete", "Slet billede"), CONTENT, (page,)):
        doc = pdf.doc
        paint, _obj = _locate(doc, page, target)
        _delete_paint(doc, doc[page], paint)


def _delete_paint(doc: pymupdf.Document, pg: pymupdf.Page, paint: _Paint) -> None:
    p = _edit_level(paint)
    blk = _exclusive_block(p, nested_mc=True)
    span = (blk.q_start, blk.Q_end) if blk is not None else (p.start, p.end)
    _commit(doc, pg, p.container, [(span[0], span[1], b"\n")])


def render_without_image(doc, page: int, target: ImageRef, zoom: float, *, clip=None,
                         annots: bool = True) -> pymupdf.Pixmap:
    """Render (part of) ``page`` as it looks without one image placement –
    e.g. to "lift" an image off the page while it is dragged. Works on a
    throw-away copy of the page; ``doc`` is not changed. ``clip`` is a
    display-space rectangle (see :func:`pdfstudio.core.render.render_page`)."""
    from .render import as_document, render_page

    src = as_document(doc)
    tmp = pymupdf.open()
    try:
        tmp.insert_pdf(src, from_page=page, to_page=page, annots=annots, links=False, widgets=annots)
        paint, _obj = _locate(tmp, 0, target)
        _delete_paint(tmp, tmp[0], paint)
        return render_page(tmp, 0, zoom, clip=clip, annots=annots)
    finally:
        tmp.close()


def _read_image(image: str | bytes | os.PathLike) -> bytes:
    if isinstance(image, (bytes, bytearray, memoryview)):
        data = bytes(image)
    else:
        with open(os.fspath(image), "rb") as fh:
            data = fh.read()
    if not data:
        raise ValueError("empty image")
    return data


def _add_image_xobject(doc: pymupdf.Document, page: pymupdf.Page, data: bytes) -> int:
    """Store ``data`` as an image XObject (JPEG kept as is, alpha as SMask)
    without drawing it: PyMuPDF's insert_image prepends a drawing stream,
    which is taken out again."""
    before = _key(doc, page.xref, "Contents")
    xref = page.insert_image(page.rect, stream=data, overlay=False, keep_proportion=False)
    doc.xref_set_key(page.xref, "Contents", before[1] if before[0] != "null" else "null")
    return xref


def replace_image(pdf, page: int, target: ImageRef, image: str | bytes, keep_aspect: bool = True) -> ImageObject:
    """Draw ``image`` (path or bytes) instead of the placement: same place in
    the stacking order, same orientation, fitted (``keep_aspect``) or
    stretched into the old frame. Other placements of the old image keep it."""
    data = _read_image(image)
    with pdf.edit(_label("objects.undo.replace", "Erstat billede"), CONTENT, (page,)):
        doc = pdf.doc
        pg = doc[page]
        paint, obj = _locate(doc, page, target)
        new_xref = _add_image_xobject(doc, pg, data)
        info = _image_dict_info(doc, new_xref)
        w, h = max(1, info["width"]), max(1, info["height"])
        s = pymupdf.Matrix(_IDENTITY)
        if keep_aspect:
            lx, ly = math.hypot(paint.ctm.a, paint.ctm.b), math.hypot(paint.ctm.c, paint.ctm.d)
            old_aspect, new_aspect = lx / max(ly, 1e-9), w / h
            if new_aspect > old_aspect:
                sv = old_aspect / new_aspect
                s = pymupdf.Matrix(1, 0, 0, sv, 0, (1 - sv) / 2)
            else:
                su = new_aspect / old_aspect
                s = pymupdf.Matrix(su, 0, 0, 1, (1 - su) / 2, 0)
        c_new = s * paint.ctm
        p = _edit_level(paint)
        name = _register_xobject(doc, p.container, pg.xref, new_xref)

        def draw(base: pymupdf.Matrix) -> bytes:
            f = c_new * _inv(base)
            return b"\nq " + _fmt(f) + b" cm /" + name.encode("latin-1") + b" Do Q\n"

        whole = _exclusive_block(p, nested_mc=False)     # incl. the old image's own clip
        removable = _exclusive_block(p, nested_mc=True)
        if whole is not None and whole is removable:
            edits = [(whole.q_start, whole.Q_end, draw(whole.ctm))]
        elif whole is not None:
            # the clip block cannot be removed (marked content ends inside it):
            # draw the new image just before it, unclipped, and drop the old one
            old = (removable.q_start, removable.Q_end) if removable is not None else (p.start, p.end)
            edits = [(whole.q_start, whole.q_start, draw(whole.ctm)), (old[0], old[1], b"\n")]
        else:
            edits = [(p.start, p.end, draw(p.ctm))]
        _commit(doc, pg, p.container, edits)
    return _after(pdf.doc, page, obj)


# ---------------------------------------------------------------------------
# reading image data
# ---------------------------------------------------------------------------
_PASSTHROUGH_EXT = {"png", "jpg", "jpeg", "jpx", "jp2", "bmp", "gif", "tif", "tiff"}


def _resolve(doc: pymupdf.Document, target: ImageRef) -> tuple[int, ImageObject | None]:
    if isinstance(target, ImageObject):
        return target.xref, target
    return int(target), None


def _rgb(pix: pymupdf.Pixmap) -> pymupdf.Pixmap:
    if pix.colorspace is not None and pix.colorspace.n not in (1, 3):
        pix = pymupdf.Pixmap(pymupdf.csRGB, pix)
    return pix


def _inline_block(doc: pymupdf.Document, obj: ImageObject) -> dict | None:
    """The text-extraction image block of an inline image (closest bbox)."""
    page = doc[obj.page]
    want = obj.rect * page.derotation_matrix
    best, dist = None, 1e9
    try:
        blocks = page.get_text("dict", flags=pymupdf.TEXT_PRESERVE_IMAGES)["blocks"]
    except Exception:
        return None
    for b in blocks:
        if b.get("type") != 1:
            continue
        r = pymupdf.Rect(b["bbox"])
        d = sum(abs(a - c) for a, c in zip(r, want))
        if d < dist:
            best, dist = b, d
    return best if dist < 4.0 else None


def extract_image_file(doc: pymupdf.Document, target: ImageRef) -> tuple[bytes, str]:
    """(file bytes, extension) for saving an image: the original encoded
    data where possible (JPEG stays JPEG, no recompression); images with a
    soft mask become PNG with alpha."""
    xref, obj = _resolve(doc, target)
    if xref <= 0:
        if obj is None:
            raise ValueError("inline images need the ImageObject")
        block = _inline_block(doc, obj)
        if block is None or not block.get("image"):
            raise LookupError("inline image data not found")
        return bytes(block["image"]), str(block.get("ext") or "png")
    info = doc.extract_image(xref) or {}
    smask = int(info.get("smask") or 0)
    ext = str(info.get("ext") or "").lower()
    if not smask and ext in _PASSTHROUGH_EXT and info.get("image"):
        return bytes(info["image"]), "jpg" if ext == "jpeg" else ext
    pix = image_pixmap(doc, target)
    return pix.tobytes("png"), "png"


def extract_image(doc: pymupdf.Document, target: ImageRef) -> bytes:
    """The image as file bytes (see :func:`extract_image_file`)."""
    return extract_image_file(doc, target)[0]


def image_pixmap(doc: pymupdf.Document, target: ImageRef, max_side: int | None = None) -> pymupdf.Pixmap:
    """The image itself (unrotated, RGB/gray, with alpha when it has a soft
    mask) – e.g. for previews. ``max_side`` downsamples large images."""
    xref, obj = _resolve(doc, target)
    if xref <= 0:
        if obj is None:
            raise ValueError("inline images need the ImageObject")
        block = _inline_block(doc, obj)
        if block is None or not block.get("image"):
            raise LookupError("inline image data not found")
        pix = _rgb(pymupdf.Pixmap(block["image"]))
    else:
        pix = _rgb(pymupdf.Pixmap(doc, xref))
        smask = obj.smask if obj is not None else _image_dict_info(doc, xref)["smask"]
        if smask and not pix.alpha:
            try:
                mask = pymupdf.Pixmap(doc, smask)
                if (mask.width, mask.height) != (pix.width, pix.height):
                    mask = pymupdf.Pixmap(mask, pix.width, pix.height, None)
                if mask.n != 1:
                    mask = pymupdf.Pixmap(pymupdf.csGRAY, mask)
                pix = pymupdf.Pixmap(pix, mask)
            except Exception:
                pass
    if max_side and max(pix.width, pix.height) > max_side:
        s = max_side / max(pix.width, pix.height)
        pix = pymupdf.Pixmap(pix, max(1, round(pix.width * s)), max(1, round(pix.height * s)), None)
    return pix
