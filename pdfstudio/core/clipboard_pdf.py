"""PDF pages from what the clipboard holds (Qt-free).

The UI reads the system clipboard into a :class:`ClipContent`
(``ui/clipboard_source.py``); this module turns it into PDF pages:

* :func:`content_to_pdf` – a new PDF ("Ny PDF fra udklipsholder", U7–U9),
* :func:`insert_content` – new pages in an open document, one undo step
  ("Indsæt fra udklipsholder", U10–U13).

What can be used (U1–U6), in priority order when the clipboard holds several
kinds: Folio's own object clip → files copied in Explorer (PDFs, images) →
PDF data → image → formatted text (HTML) → plain text. When an image is used
although text is there too (Excel cells, a piece of a web page), the result
names ``alternative="text"`` so the UI can offer "Brug teksten i stedet".

Everything is local: nothing is ever fetched (U16) – images that copied HTML
only references on the web are left out.

Text and HTML are set with MuPDF's HTML engine (``pymupdf.Story``) in a local
TrueType font (Arial on Windows, Liberation Sans / another local sans on
Linux) that is embedded (subset) in the PDF, so the text stays searchable and
editable (U3, U15). Only when no local font covers the Danish characters do
MuPDF's built-in fonts take over.

API contract (binding – the UI codes against it; see
``docs/kravspec-udklipsholder.md``)::

    ClipContent(images, files, pdf, html, text, folio)   .kinds() .is_empty()
    ClipOptions(page_size="image"|"A4"|"Letter"|"match", margin_mm, text_page, text_style, prefer)
    SUPPORTED_FILE_EXTENSIONS ; usable_files(paths) -> (usable, skipped)
    chosen_kind(content, prefer=None) -> str | None
    content_to_pdf(content, options=None, *, passwords=None) -> ClipResult(data, kind, pages, skipped, alternative)
    text_to_pdf(text, *, page_size=A4, margin_mm=20.0, style=None) -> bytes
    html_to_pdf(html, *, page_size=A4, margin_mm=20.0, style=None) -> bytes
    insert_content(pdf, content, at, options=None, *, passwords=None)
        -> ClipInsert(pages, kind, skipped, alternative)        # ONE pdf.edit (STRUCTURE)
"""

# Clipboard data is untrusted input: every step falls back instead of raising.
# ruff: noqa: BLE001, S110, S112
from __future__ import annotations

import base64
import binascii
import html as _html
import io
import os
import re
import struct
import threading
import zlib
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from html.parser import HTMLParser

import pymupdf

from ..i18n import tr
from . import fonts as _fonts
from .document import (
    STRUCTURE,
    PasswordRequired,
    write_bytes,
)
from .fonts import SpanStyle
from .images import (
    PAGE_SIZES,
    SCREEN_DPI,
    SUPPORTED_IMAGE_EXTENSIONS,
    _pixel_size,
    image_as_pdf,
)

#: kinds in priority order (U6)
KINDS = ("folio", "files", "pdf", "image", "html", "text")

#: A4 portrait in PDF points
A4 = (595.28, 841.89)
#: Letter portrait in PDF points
LETTER = (612.0, 792.0)

MM = 72.0 / 25.4

#: files that can be used when copied in Explorer (U2): PDFs + images.py's image types
SUPPORTED_FILE_EXTENSIONS: tuple[str, ...] = (".pdf",) + tuple(SUPPORTED_IMAGE_EXTENSIONS)

#: characters the body font must contain (U15)
_COVERAGE = "AZaz09æøåÆØÅ€“”–‘’„éü"
_DEFAULT_SIZE = 11.0
_LINE_HEIGHT = 1.35
_TEXT_MARGIN_MM = 20.0


@dataclass
class ClipContent:
    """What the system clipboard holds, read by the UI.

    ``images``  encoded images (PNG preferred, lossless), in clipboard order
    ``files``   local file paths copied in Explorer, in their order
    ``pdf``     PDF data another program put on the clipboard
    ``html``    formatted text (an HTML document or fragment)
    ``text``    plain text
    ``folio``   Folio's own object clip (``pageobjects.Clip``) when it is the
                newest thing on the clipboard
    """

    images: list[bytes] = field(default_factory=list)
    files: list[str] = field(default_factory=list)
    pdf: bytes | None = None
    html: str | None = None
    text: str | None = None
    folio: object | None = None

    def kinds(self) -> list[str]:
        """The usable kinds present, in priority order (:data:`KINDS`)."""
        have = {
            "folio": self.folio is not None,
            "files": bool(usable_files(self.files)[0]),
            "pdf": bool(self.pdf),
            "image": bool(self.images),
            "html": bool(self.html and self.html.strip()),
            "text": bool(self.text and self.text.strip()),
        }
        return [k for k in KINDS if have[k]]

    def is_empty(self) -> bool:
        """True when nothing in it can become a page."""
        return not self.kinds()


@dataclass
class ClipOptions:
    """How pages are made.

    ``page_size``  for images: "image" (the page is the image at 96 dpi, no
                   margin – default for a new PDF, U9), "A4" / "Letter" (the
                   image contained, orientation following the image), or
                   "match" (the neighbour page's paper size, orientation
                   following the image – default for inserted pages, U12;
                   :func:`content_to_pdf` treats it as "A4")
    ``margin_mm``  margin around an image on a paper size (default 10 mm)
    ``text_page``  page size (w, h) in points for text; None = A4 portrait
                   (insert: the neighbour page's size)
    ``text_style`` body style for text; None = a readable default (Arial /
                   Helvetica 11 pt; insert: ``insert.document_text_style``)
    ``prefer``     force one kind (e.g. "text" for "Brug teksten i stedet");
                   None = priority order
    """

    page_size: str = "image"
    margin_mm: float = 10.0
    text_page: tuple[float, float] | None = None
    text_style: SpanStyle | None = None
    prefer: str | None = None


@dataclass
class ClipResult:
    """A new PDF made from the clipboard."""

    data: bytes
    kind: str                       # the kind used (one of KINDS)
    pages: int
    skipped: list[str] = field(default_factory=list)   # file names that could not be used
    alternative: str | None = None  # "text" when an image was used and text is there too


@dataclass
class ClipInsert:
    """Pages inserted from the clipboard (display indices after the insert)."""

    pages: list[int]
    kind: str
    skipped: list[str] = field(default_factory=list)
    alternative: str | None = None


def usable_files(paths: Sequence[str]) -> tuple[list[str], list[str]]:
    """Split copied file paths into usable ones (existing PDFs and images, in
    order) and skipped ones (file names, for the message)."""
    usable: list[str] = []
    skipped: list[str] = []
    for p in paths or ():
        if os.path.isfile(p) and os.path.splitext(p)[1].lower() in SUPPORTED_FILE_EXTENSIONS:
            usable.append(p)
        else:
            skipped.append(os.path.basename(p.rstrip("/\\")) or p)
    return usable, skipped


def chosen_kind(content: ClipContent, prefer: str | None = None) -> str | None:
    """The kind :func:`content_to_pdf` would use (``prefer`` when present).

    "text" and "html" stand in for each other as a preference: "Brug teksten
    i stedet" (``prefer="text"``) uses the formatted text when a web page put
    only an image and HTML on the clipboard."""
    kinds = content.kinds()
    if prefer in kinds:
        return prefer
    if prefer in ("text", "html"):
        other = "html" if prefer == "text" else "text"
        if other in kinds:
            return other
    return kinds[0] if kinds else None


# =============================================================================
# fonts for text / HTML
# =============================================================================
_SANS_FAMILIES = ("arial", "liberationsans", "arimo", "helvetica", "segoeui", "notosans", "dejavusans",
                  "freesans", "opensans")
_SERIF_FAMILIES = ("timesnewroman", "liberationserif", "tinos", "georgia", "notoserif", "dejavuserif",
                   "freeserif")
_MONO_FAMILIES = ("consolas", "couriernew", "liberationmono", "cousine", "dejavusansmono", "notosansmono",
                  "freemono")


@dataclass
class _Family:
    """A local font family usable in CSS: face bytes keyed by (bold, italic)."""

    name: str                                   # family name (for the report / tests)
    faces: dict[tuple[bool, bool], bytes]


_family_cache: dict[tuple, _Family | None] = {}
_family_lock = threading.Lock()


def _face_bytes(face) -> bytes | None:
    try:
        if face.path.lower().endswith((".ttc", ".otc")):
            return _fonts._extract_ttc_face(face.path, face.index)
        with open(face.path, "rb") as fh:
            return fh.read()
    except Exception:
        return None


def _family_keys(style: SpanStyle | None, fallback: Sequence[str]) -> list[str]:
    keys: list[str] = []
    if style is not None and style.font:
        name = _fonts.clean_font_name(style.font)
        fam = _fonts.family_key(name)
        keys += [fam, re.sub(r"[^a-z0-9]", "", name.lower())]
        stripped = re.sub(r"(psmt|ps|mt|std|pro|lt)$", "", fam)
        if stripped and stripped != fam:
            keys.append(stripped)
        aliases = getattr(_fonts, "_ALIASES", {})
        for key in list(keys):
            keys += aliases.get(key, [])
    keys += list(fallback)
    out: list[str] = []
    for k in keys:
        if k and k not in out:
            out.append(k)
    return out


def _local_family(keys: Sequence[str]) -> _Family | None:
    """The first local family among ``keys`` whose regular face covers the
    Danish characters and symbols (U15)."""
    cache_key = tuple(keys)
    with _family_lock:
        if cache_key in _family_cache:
            return _family_cache[cache_key]
    found: _Family | None = None
    try:
        idx = _fonts.system_font_index()
    except Exception:
        idx = None
    for key in keys if idx is not None else ():
        faces = idx.by_family.get(key)
        if not faces:
            continue
        chosen: dict[tuple[bool, bool], object] = {}
        for face in faces:
            slot = (bool(face.bold), bool(face.italic))
            best = chosen.get(slot)
            # prefer the normal width and the weight closest to 400 / 700
            want = 700 if slot[0] else 400
            if best is None or (abs(face.width - 5), abs(face.weight - want)) < \
                    (abs(best.width - 5), abs(best.weight - want)):
                chosen[slot] = face
        regular = chosen.get((False, False))
        if regular is None:
            continue
        data = _face_bytes(regular)
        if not data:
            continue
        try:
            if not _fonts.font_covers(pymupdf.Font(fontbuffer=data), _COVERAGE):
                continue
        except Exception:
            continue
        loaded = {(False, False): data}
        for slot, face in chosen.items():
            if slot != (False, False):
                b = _face_bytes(face)
                if b:
                    loaded[slot] = b
        found = _Family(regular.family or key, loaded)
        break
    with _family_lock:
        _family_cache[cache_key] = found
    return found


def _css_color(color) -> str:
    try:
        r, g, b = (max(0, min(255, round(float(c) * 255))) for c in tuple(color)[:3])
    except Exception:
        return "#000"
    return f"#{r:02x}{g:02x}{b:02x}"


@dataclass
class _Setup:
    archive: pymupdf.Archive
    css: str
    family: str          # CSS family of the body text
    source: str          # "system" | "builtin"


def _font_setup(style: SpanStyle | None, *, mono_family: bool = False) -> _Setup:
    """Archive + CSS for the body font chosen from ``style``. ``mono_family``
    adds a local monospace family for <pre>/<code> (HTML)."""
    style = style or SpanStyle("Arial", _DEFAULT_SIZE)
    if style.mono:
        fallback, generic = _MONO_FAMILIES, "monospace"
    elif style.serif:
        fallback, generic = _SERIF_FAMILIES, "serif"
    else:
        fallback, generic = _SANS_FAMILIES, "sans-serif"
    archive = pymupdf.Archive()
    rules: list[str] = []

    def add_family(css_name: str, fam: _Family | None) -> bool:
        if fam is None:
            return False
        for (bold, italic), data in fam.faces.items():
            fname = f"{css_name}-{int(bold)}{int(italic)}.ttf"
            archive.add((data, fname))
            weight, slant = ("bold" if bold else "normal"), ("italic" if italic else "normal")
            rules.append(f"@font-face{{font-family:{css_name};src:url({fname});font-weight:{weight};"
                         f"font-style:{slant};}}")
        return True

    body = _local_family(_family_keys(style, fallback))
    source = "system" if add_family("folio-body", body) else "builtin"
    family = "folio-body" if body is not None else generic
    mono = "monospace"
    if mono_family and add_family("folio-mono", _local_family(list(_MONO_FAMILIES))):
        mono = "folio-mono"
    size = float(style.size) if style.size and style.size > 0 else _DEFAULT_SIZE
    size = max(4.0, min(72.0, size))
    weight, slant = ("bold" if style.bold else "normal"), ("italic" if style.italic else "normal")
    rules.append(
        f"body{{margin:0;padding:0;font-family:{family};font-size:{size:.2f}pt;line-height:{_LINE_HEIGHT:.2f};"
        f"color:{_css_color(style.color)};font-weight:{weight};font-style:{slant};}}")
    rules.append(f"pre,code,tt,kbd,samp{{font-family:{mono};}}")
    return _Setup(archive, "\n".join(rules), family, source)


def body_font(style: SpanStyle | None = None) -> tuple[str, str]:
    """``(family name, source)`` of the font :func:`text_to_pdf` uses for
    ``style`` – source "system" (a local font, embedded) or "builtin"."""
    fallback = _MONO_FAMILIES if style and style.mono else _SERIF_FAMILIES if style and style.serif \
        else _SANS_FAMILIES
    fam = _local_family(_family_keys(style or SpanStyle("Arial", _DEFAULT_SIZE), fallback))
    return (fam.name, "system") if fam is not None else ("", "builtin")


# =============================================================================
# laying out a Story on pages
# =============================================================================
def _page_box(page_size: tuple[float, float], margin_mm: float) -> tuple[pymupdf.Rect, pymupdf.Rect]:
    w, h = (float(v) for v in page_size)
    if not (w > 0 and h > 0):
        w, h = A4
    m = max(0.0, float(margin_mm or 0.0)) * MM
    m = min(m, min(w, h) / 4)
    mediabox = pymupdf.Rect(0, 0, w, h)
    return mediabox, pymupdf.Rect(m, m, w - m, h - m)


def _story_pdf(body: str, setup: _Setup, page_size, margin_mm: float, extra_css: str = "") -> bytes:
    story = pymupdf.Story(body, user_css=setup.css + "\n" + extra_css, archive=setup.archive)
    mediabox, where = _page_box(page_size, margin_mm)
    buf = io.BytesIO()
    writer = pymupdf.DocumentWriter(buf)
    more, pages, stuck = 1, 0, 0
    try:
        while more:
            dev = writer.begin_page(mediabox)
            more, filled = story.place(where)
            story.draw(dev)
            writer.end_page()
            pages += 1
            filled = pymupdf.Rect(filled)
            stuck = stuck + 1 if more and (filled.is_empty or filled.height <= 0) else 0
            if stuck > 3 or pages > 20000:
                raise RuntimeError("layout does not progress")
    finally:
        writer.close()
    doc = pymupdf.open("pdf", buf.getvalue())
    try:
        try:
            doc.subset_fonts()          # the whole TTF is embedded otherwise (~1 MB)
        except Exception:  # pragma: no cover - keeps the full fonts
            pass
        return write_bytes(doc, garbage=3, deflate=True)
    finally:
        doc.close()


# =============================================================================
# plain text
# =============================================================================
_CONTROL = re.compile(r"[\x00-\x08\x0b\x0e-\x1f\x7f]")


def _clean_text(text: str) -> str:
    text = (text or "").replace("\r\n", "\n").replace("\r", "\n").replace("\f", "\n")
    text = text.replace("\u2028", "\n").replace("\u2029", "\n\n")
    text = _CONTROL.sub("", text)
    lines = [ln.expandtabs(4).rstrip() for ln in text.split("\n")]
    while lines and not lines[-1]:
        lines.pop()
    return "\n".join(lines)


_TEXT_CSS = ".folio-text{white-space:pre-wrap;margin:0;padding:0;}"


def text_to_pdf(text: str, *, page_size: tuple[float, float] = A4, margin_mm: float = 20.0,
                style: SpanStyle | None = None) -> bytes:
    """Plain text set on pages (U3): paragraphs, empty lines and indentation
    kept, long text flowing onto new pages, an embedded font covering every
    character (U15)."""
    clean = _clean_text(text) or " "
    setup = _font_setup(style)
    body = f'<div class="folio-text">{_html.escape(clean, quote=False)}</div>'
    return _story_pdf(body, setup, page_size, margin_mm, _TEXT_CSS)


# =============================================================================
# HTML
# =============================================================================
def cf_html_fragment(payload: str) -> str:
    """The HTML in a Windows CF_HTML payload ("Version:…StartHTML:…" header,
    ``<!--StartFragment-->`` markers); other HTML is returned unchanged."""
    s = payload or ""
    if not s.lstrip().startswith("Version:"):
        return s
    a = s.find("<!--StartFragment")
    b = s.find("<!--EndFragment")
    if a >= 0 and b > a:
        start = s.find("-->", a)
        frag = s[start + 3 if start >= 0 else a:b]
    else:
        frag = None
        raw = s.encode("utf-8", "replace")
        for k0, k1 in (("StartFragment", "EndFragment"), ("StartHTML", "EndHTML")):
            m0 = re.search(k0 + r":\s*(-?\d+)", s)
            m1 = re.search(k1 + r":\s*(-?\d+)", s)
            if m0 and m1:
                i, j = int(m0.group(1)), int(m1.group(1))
                if 0 <= i < j <= len(raw):
                    frag = raw[i:j].decode("utf-8", "replace")
                    break
        if frag is None:
            lt = s.find("<")
            frag = s[lt:] if lt >= 0 else ""
    # Excel puts the markers inside the <table>: give rows their table back
    if re.search(r"<t[rd][\s>]", frag, re.IGNORECASE) and not re.search(r"<table[\s>]", frag, re.IGNORECASE):
        frag = "<table>" + frag + "</table>"
    return frag


_DROP_WITH_CONTENT = {"script", "style", "head", "title", "noscript", "iframe", "object", "embed", "template",
                      "svg", "math", "xml", "canvas", "audio", "video", "select", "textarea", "button",
                      "frameset", "frame", "applet"}
_KEEP_TAGS = {"p", "br", "div", "span", "b", "strong", "i", "em", "u", "ins", "s", "strike", "del", "sub", "sup",
              "small", "big", "font", "h1", "h2", "h3", "h4", "h5", "h6", "ul", "ol", "li", "dl", "dt", "dd",
              "table", "thead", "tbody", "tfoot", "tr", "td", "th", "caption", "col", "colgroup", "blockquote",
              "pre", "code", "tt", "kbd", "samp", "hr", "a", "img", "center", "mark", "q", "cite", "abbr",
              "address", "section", "article", "header", "footer", "main", "figure", "figcaption", "label",
              "var", "nav", "aside"}
_VOID = {"br", "hr", "img", "col"}
_MAX_DEPTH = 40           # MuPDF lays out nothing beyond ~100 nested boxes
_BLOCK = {"p", "div", "h1", "h2", "h3", "h4", "h5", "h6", "li", "tr", "table", "blockquote", "pre", "dt", "dd",
          "section", "article", "header", "footer", "figure", "figcaption", "address", "ul", "ol", "caption"}
_KEEP_ATTRS = {"style", "colspan", "rowspan", "align", "valign", "color", "width", "height", "start", "type",
               "value", "dir", "bgcolor", "size"}
_EXTERNAL = re.compile(r"url\s*\(|@import|expression\s*\(|javascript:|vbscript:|[a-z][a-z0-9+.-]*://", re.IGNORECASE)
_DROP_PROPS = re.compile(r"^(mso-|font-family$|font$|behavior$|-moz-binding$|content$|src$|background-image$)")


def _clean_style(value: str) -> str:
    keep = []
    for decl in value.split(";"):
        if ":" not in decl:
            continue
        prop, val = decl.split(":", 1)
        prop = prop.strip().lower()
        val = val.strip()
        if not prop or not val or _DROP_PROPS.match(prop) or _EXTERNAL.search(val):
            continue
        if "background" in prop and _EXTERNAL.search(val):
            continue
        keep.append(f"{prop}:{val}")
    return ";".join(keep)


_PERCENT = re.compile(r"(%[0-9A-Fa-f]{2})+")


def _unquote(s: str) -> str:
    """Percent-decoding (UTF-8) without urllib."""
    return _PERCENT.sub(lambda m: bytes.fromhex(m.group(0).replace("%", "")).decode("utf-8", "replace"), s)


def file_url_path(src: str) -> str | None:
    """The local path of a ``file:`` URL (``file:///C:/Users/…/clip_image001.png``)
    or None. Nothing is accessed."""
    if not src.lower().startswith("file:"):
        return None
    rest = src[5:]
    if rest.startswith("//"):
        rest = rest[2:]
        if rest.lower().startswith("localhost/"):
            rest = rest[9:]
        elif not rest.startswith("/") and not re.match(r"^[A-Za-z]:", rest):
            return None                       # a network share (file://server/…): never touched
    rest = _unquote(rest.split("?", 1)[0].split("#", 1)[0])
    if re.match(r"^/[A-Za-z]:[/\\]", rest):   # /C:/… -> C:/…
        rest = rest[1:]
    return rest or None


_IMAGE_TYPES = {"png": "png", "jpeg": "jpg", "jpg": "jpg", "gif": "gif", "bmp": "bmp", "tiff": "tif",
                "webp": "webp"}


def _image_ok(data: bytes) -> bool:
    try:
        with pymupdf.open(stream=data) as d:
            return (not d.is_pdf) and d.page_count > 0
    except Exception:
        return False


class _Sanitizer(HTMLParser):
    """Rebuilds HTML from an allow-list of tags and attributes: no scripts,
    styles sheets, links or external references; images only from data:
    URIs or existing local files, put into the Story's archive."""

    def __init__(self, archive: pymupdf.Archive):
        super().__init__(convert_charrefs=True)
        self.archive = archive
        self.out: list[str] = []
        self.text: list[str] = []
        self.skip = 0
        self.open: list[str] = []
        self.images = 0

    # -- images --------------------------------------------------------
    def _image_src(self, src: str) -> str | None:
        src = (src or "").strip()
        data: bytes | None = None
        ext = "png"
        if src.lower().startswith("data:"):
            m = re.match(r"data:image/([a-z0-9.+-]+)?[^,]*?(;base64)?,(.*)$", src, re.IGNORECASE | re.DOTALL)
            if not m or not m.group(2):
                return None
            ext = _IMAGE_TYPES.get((m.group(1) or "png").lower(), "png")
            try:
                data = base64.b64decode(re.sub(r"\s+", "", m.group(3)), validate=False)
            except (binascii.Error, ValueError):
                return None
        else:
            path = file_url_path(src)
            if path is None and re.match(r"^[A-Za-z]:[\\/]", src):
                path = src                    # a bare Windows path
            if path is None:
                return None                   # web or relative reference: never fetched (U16)
            ext = os.path.splitext(path)[1].lower().lstrip(".")
            if ("." + ext) not in SUPPORTED_IMAGE_EXTENSIONS or not os.path.isfile(path):
                return None
            try:
                with open(path, "rb") as fh:
                    data = fh.read()
            except OSError:
                return None
        if not data or not _image_ok(data):
            return None
        self.images += 1
        name = f"folio-clip-{self.images}.{ext or 'png'}"
        self.archive.add((data, name))
        return name

    # -- parser callbacks ----------------------------------------------
    def handle_starttag(self, tag, attrs):
        self._start(tag, attrs, closed=False)

    def handle_startendtag(self, tag, attrs):
        self._start(tag, attrs, closed=True)

    def _start(self, tag, attrs, closed):
        tag = tag.lower()
        if tag in _DROP_WITH_CONTENT:
            if not closed:
                self.skip += 1
            return
        if self.skip:
            return
        if tag in _BLOCK or tag == "br":
            self.text.append("\n")
        if tag not in _KEEP_TAGS or (len(self.open) >= _MAX_DEPTH and not closed and tag not in _VOID):
            return
        parts = [tag]
        src = None
        for name, value in attrs:
            name = (name or "").lower()
            value = value or ""
            if tag == "img" and name == "src":
                src = self._image_src(value)
                continue
            if name not in _KEEP_ATTRS:
                continue
            if name == "style":
                value = _clean_style(value)
                if not value:
                    continue
            elif _EXTERNAL.search(value):
                continue
            parts.append(f'{name}="{_html.escape(value, quote=True)}"')
        if tag == "img":
            if src is None:
                return
            parts.append(f'src="{src}"')
        if tag in _VOID:
            self.out.append(f"<{' '.join(parts)}/>")
            return
        self.out.append(f"<{' '.join(parts)}>")
        if not closed:
            self.open.append(tag)
        else:
            self.out.append(f"</{tag}>")

    def handle_endtag(self, tag):
        tag = tag.lower()
        if tag in _DROP_WITH_CONTENT:
            if self.skip:
                self.skip -= 1
            return
        if self.skip or tag not in _KEEP_TAGS or tag in _VOID:
            return
        if tag in _BLOCK:
            self.text.append("\n")
        elif tag in ("td", "th"):
            self.text.append("    ")
        if tag in self.open:
            while self.open:
                t = self.open.pop()
                self.out.append(f"</{t}>")
                if t == tag:
                    break

    def handle_data(self, data):
        if self.skip:
            return
        self.out.append(_html.escape(data, quote=False))
        self.text.append(data)

    def result(self) -> tuple[str, str]:
        while self.open:
            self.out.append(f"</{self.open.pop()}>")
        plain = re.sub(r"[ \t\u00a0]*\n[ \t\u00a0\n]*\n", "\n\n", "".join(self.text))
        plain = re.sub(r"[ \t]+", " ", plain)
        return "".join(self.out), plain.strip("\n")


def sanitize_html(markup: str, archive: pymupdf.Archive | None = None) -> tuple[str, str]:
    """``(safe HTML, plain text)`` of clipboard HTML (CF_HTML or not): only
    formatting tags, no scripts/links/external references (U16)."""
    p = _Sanitizer(archive if archive is not None else pymupdf.Archive())
    try:
        p.feed(cf_html_fragment(markup))
        p.close()
    except Exception:  # pragma: no cover - HTMLParser is lenient
        pass
    return p.result()


_HTML_CSS = """
h1{font-size:1.8em;font-weight:bold;margin:0.4em 0 0.3em 0;}
h2{font-size:1.45em;font-weight:bold;margin:0.4em 0 0.3em 0;}
h3{font-size:1.2em;font-weight:bold;margin:0.4em 0 0.25em 0;}
h4,h5,h6{font-size:1em;font-weight:bold;margin:0.3em 0 0.2em 0;}
p{margin:0 0 0.5em 0;}
table{border-collapse:collapse;margin:0.3em 0 0.6em 0;}
td,th{border:0.5pt solid #b4b4b4;padding:1.5pt 4pt;vertical-align:top;}
img{max-width:100%;}
"""


def _has_text(data: bytes) -> bool:
    with pymupdf.open("pdf", data) as doc:
        return any(page.get_text("text").strip() for page in doc)


def html_to_pdf(html: str, *, page_size: tuple[float, float] = A4, margin_mm: float = 20.0,
                style: SpanStyle | None = None) -> bytes:
    """Formatted text (U4): headings, bold/italic/underline, colours, lists
    and simple tables; nothing is fetched (U16)."""
    setup = _font_setup(style, mono_family=True)
    try:
        safe, plain = sanitize_html(html or "", setup.archive)
    except Exception:
        safe, plain = "", re.sub(r"<[^>]*>", " ", html or "")
    if safe.strip() and plain.strip() or "<img" in safe:
        try:
            data = _story_pdf(safe, setup, page_size, margin_mm, _HTML_CSS)
            if not plain.strip() or _has_text(data):
                return data
        except Exception:
            pass
    return text_to_pdf(plain, page_size=page_size, margin_mm=margin_mm, style=style)


# =============================================================================
# images
# =============================================================================
_PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"


@dataclass
class _Png:
    width: int
    height: int
    depth: int
    color_type: int
    idat: bytes
    palette: bytes


def _parse_png(data: bytes) -> _Png | None:
    """The parts of a PNG that can go into a PDF image without decoding –
    None for what MuPDF's own image path handles (interlaced, transparency
    keys, broken files)."""
    if not data.startswith(_PNG_SIGNATURE):
        return None
    pos, ihdr, idat, palette = 8, None, [], b""
    try:
        while pos + 8 <= len(data):
            (length,) = struct.unpack(">I", data[pos:pos + 4])
            ctype = data[pos + 4:pos + 8]
            chunk = data[pos + 8:pos + 8 + length]
            if len(chunk) != length:
                return None
            if ctype == b"IHDR":
                ihdr = struct.unpack(">IIBBBBB", chunk)
            elif ctype == b"PLTE":
                palette = chunk
            elif ctype == b"IDAT":
                idat.append(chunk)
            elif ctype == b"tRNS":
                return None                     # colour-key transparency: MuPDF path
            elif ctype == b"IEND":
                break
            pos += 12 + length
    except struct.error:
        return None
    if ihdr is None or not idat:
        return None
    w, h, depth, ctype, comp, filt, interlace = ihdr
    if comp or filt or interlace or w <= 0 or h <= 0:
        return None
    ok = {0: (1, 2, 4, 8, 16), 2: (8, 16), 3: (1, 2, 4, 8), 4: (8,), 6: (8,)}
    if depth not in ok.get(ctype, ()) or (ctype == 3 and not palette):
        return None
    return _Png(w, h, depth, ctype, b"".join(idat), palette)


def _new_stream(doc: pymupdf.Document, head: str, data: bytes, decode_parms: str | None = None) -> int:
    """A new object ``<<head>>`` with ``data`` as its (already Flate
    compressed) stream."""
    xref = doc.get_new_xref()
    doc.update_object(xref, f"<<{head}>>")
    doc.update_stream(xref, data, compress=False)
    doc.xref_set_key(xref, "Filter", "/FlateDecode")
    if decode_parms:
        doc.xref_set_key(xref, "DecodeParms", decode_parms)
    return xref


def _png_xref(doc: pymupdf.Document, png: _Png) -> int:
    """The PNG as an image XObject in ``doc``, pixel for pixel (U14).

    Without alpha the PNG's compressed data goes in as it is (PDF knows the
    PNG row filters: /Predictor 15). With alpha the rows are unfiltered by
    MuPDF, split into colour and an /SMask, and compressed again – never
    premultiplied, so semi-transparent pixels keep their exact colour."""
    w, h, depth = png.width, png.height, png.depth
    if png.color_type in (0, 2, 3):
        colors = 3 if png.color_type == 2 else 1
        if png.color_type == 3:
            n = len(png.palette) // 3
            cs = f"[/Indexed/DeviceRGB {n - 1} <{png.palette[:3 * n].hex()}>]"
        else:
            cs = "/DeviceRGB" if colors == 3 else "/DeviceGray"
        parms = f"<</Predictor 15/Colors {colors}/BitsPerComponent {depth}/Columns {w}>>"
        return _new_stream(doc, f"/Type/XObject/Subtype/Image/Width {w}/Height {h}/ColorSpace {cs}"
                           f"/BitsPerComponent {depth}", png.idat, parms)
    channels = 4 if png.color_type == 6 else 2
    tmp = pymupdf.open()
    try:
        x = _new_stream(tmp, "", png.idat, f"<</Predictor 15/Colors {channels}/BitsPerComponent 8/Columns {w}>>")
        raw = tmp.xref_stream(x)
    finally:
        tmp.close()
    n = w * h
    if not raw or len(raw) < n * channels:
        raise ValueError("bad PNG data")
    if channels == 4:
        color = bytearray(3 * n)
        color[0::3] = raw[0:4 * n:4]
        color[1::3] = raw[1:4 * n:4]
        color[2::3] = raw[2:4 * n:4]
        cs = "/DeviceRGB"
    else:
        color = raw[0:2 * n:2]
        cs = "/DeviceGray"
    alpha = raw[channels - 1:channels * n:channels]
    head = f"/Type/XObject/Subtype/Image/Width {w}/Height {h}/ColorSpace {cs}/BitsPerComponent 8"
    if alpha.count(255) != n:             # really transparent somewhere
        smask = _new_stream(doc, f"/Type/XObject/Subtype/Image/Width {w}/Height {h}/ColorSpace/DeviceGray"
                            "/BitsPerComponent 8", zlib.compress(alpha, 6))
        head += f"/SMask {smask} 0 R"
    return _new_stream(doc, head, zlib.compress(bytes(color), 6))


def _layout(px_w: int, px_h: int, page_size: str, margin_mm: float,
            paper: tuple[float, float] | None) -> tuple[float, float, pymupdf.Rect]:
    """Page size and image rect for an image of ``px_w`` × ``px_h`` pixels."""
    size = (page_size or "image").lower()
    if paper is None and size in ("image", "fit"):
        w, h = px_w * 72.0 / SCREEN_DPI, px_h * 72.0 / SCREEN_DPI
        return w, h, pymupdf.Rect(0, 0, w, h)
    if paper is None:
        paper = PAGE_SIZES["Letter"] if size == "letter" else PAGE_SIZES["A4"]
    short, long_ = min(paper), max(paper)
    w, h = (long_, short) if px_w > px_h else (short, long_)
    m = min(max(0.0, float(margin_mm or 0.0)) * MM, min(w, h) / 4)
    box = pymupdf.Rect(m, m, w - m, h - m)
    f = min(box.width / px_w, box.height / px_h)
    iw, ih = px_w * f, px_h * f
    x0, y0 = box.x0 + (box.width - iw) / 2, box.y0 + (box.height - ih) / 2
    return w, h, pymupdf.Rect(x0, y0, x0 + iw, y0 + ih)


def _image_doc(images: Sequence[bytes | str], page_size: str, margin_mm: float,
               paper: tuple[float, float] | None = None) -> pymupdf.Document:
    """Pages for images (one per frame). ``page_size`` "image" = the image
    at 96 dpi without margin; "A4"/"Letter"/"match" = contained on that paper
    (``paper`` – the neighbour page – for "match"; A4 without one),
    orientation following the image, centred, ``margin_mm`` around.

    PNGs are embedded by :func:`_png_xref` (bit-exact, alpha kept); other
    formats go through :func:`images.image_as_pdf` (JPEG passed through,
    others Flate-compressed, EXIF orientation applied)."""
    if (page_size or "").lower() != "match":
        paper = None
    out = pymupdf.open()
    try:
        for image in images:
            if isinstance(image, str):
                with open(image, "rb") as fh:
                    data, name = fh.read(), os.path.basename(image)
            else:
                data, name = bytes(image), ""
            png = _parse_png(data)
            if png is not None:
                try:
                    xref = _png_xref(out, png)
                except Exception:
                    png = None
                else:
                    w, h, target = _layout(png.width, png.height, page_size, margin_mm, paper)
                    page = out.new_page(width=w, height=h)
                    page.insert_image(target, xref=xref, keep_proportion=False)
                    continue
            src = image_as_pdf(data, name)
            try:
                for i in range(src.page_count):
                    px_w, px_h = _pixel_size(src, i)
                    w, h, target = _layout(px_w, px_h, page_size, margin_mm, paper)
                    page = out.new_page(width=w, height=h)
                    page.show_pdf_page(target, src, i, keep_proportion=False)
            finally:
                src.close()
        return out
    except BaseException:
        out.close()
        raise


# =============================================================================
# building the pages
# =============================================================================
@dataclass
class _Built:
    doc: pymupdf.Document
    kind: str
    skipped: list[str]
    alternative: str | None


def _open_pdf(source: str | bytes, name: str, passwords) -> pymupdf.Document | None:
    """An unencrypted PDF or None (unreadable, no / wrong password)."""
    from .merge import open_source

    try:
        return open_source(source, None, name=name)
    except PasswordRequired:
        pass
    except Exception:
        return None
    pw = passwords(name) if passwords is not None else None
    if pw is None:
        return None
    try:
        return open_source(source, pw, name=name)
    except Exception:
        return None


def _looks_like_pdf(data: bytes) -> bool:
    return b"%PDF" in data[:1024]


def _folio_doc(clip) -> pymupdf.Document:
    from .document import PdfDocument
    from .pageobjects import paste_clip

    w, h = clip.page_size
    tmp = PdfDocument.new_blank(float(w), float(h))
    try:
        paste_clip(tmp, 0, clip, offset=(0.0, 0.0))
        return pymupdf.open("pdf", write_bytes(tmp.doc, garbage=3, deflate=True))
    finally:
        tmp.close()


def _build(content: ClipContent, opts: ClipOptions, passwords, *, paper: tuple[float, float] | None,
           text_page: tuple[float, float], text_style: SpanStyle | None) -> _Built:
    kind = chosen_kind(content, opts.prefer)
    if kind is None:
        raise ValueError(tr("clipcore.err.empty"))
    kinds = content.kinds()
    alternative = "text" if kind == "image" and ("html" in kinds or "text" in kinds) else None
    skipped: list[str] = []
    out = pymupdf.open()

    def append(src: pymupdf.Document) -> None:
        if src.page_count:
            out.insert_pdf(src, links=True, annots=True)

    def images(sources: Sequence[bytes | str], names: Sequence[str]) -> None:
        for src, name in zip(sources, names):
            try:
                d = _image_doc([src], opts.page_size, opts.margin_mm, paper)
            except Exception:
                skipped.append(name)
                continue
            try:
                append(d)
            finally:
                d.close()

    try:
        if kind == "folio":
            d = _folio_doc(content.folio)
            try:
                append(d)
            finally:
                d.close()
        elif kind == "files":
            usable, bad = usable_files(content.files)
            skipped += bad
            for path in usable:
                name = os.path.basename(path)
                is_pdf = path.lower().endswith(".pdf")
                if not is_pdf:
                    try:
                        with open(path, "rb") as fh:
                            is_pdf = _looks_like_pdf(fh.read(1024))
                    except OSError:
                        skipped.append(name)
                        continue
                if is_pdf:
                    d = _open_pdf(path, name, passwords)
                    if d is None:
                        skipped.append(name)
                        continue
                    try:
                        append(d)
                    finally:
                        d.close()
                else:
                    images([path], [name])
        elif kind == "pdf":
            d = _open_pdf(bytes(content.pdf), tr("clipcore.clipboard_name"), passwords)
            if d is None:
                raise ValueError(tr("clipcore.err.pdf"))
            try:
                append(d)
            finally:
                d.close()
        elif kind == "image":
            images(content.images, [tr("clipcore.image_n", n=i + 1) for i in range(len(content.images))])
        elif kind == "html":
            d = pymupdf.open("pdf", html_to_pdf(content.html, page_size=text_page, margin_mm=_TEXT_MARGIN_MM,
                                                style=text_style))
            try:
                append(d)
            finally:
                d.close()
        else:
            d = pymupdf.open("pdf", text_to_pdf(content.text, page_size=text_page, margin_mm=_TEXT_MARGIN_MM,
                                                style=text_style))
            try:
                append(d)
            finally:
                d.close()
        if out.page_count == 0:
            raise ValueError(tr("clipcore.err.nothing_usable", names=", ".join(skipped)) if skipped
                             else tr("clipcore.err.empty"))
    except BaseException:
        out.close()
        raise
    return _Built(out, kind, skipped, alternative)


def content_to_pdf(content: ClipContent, options: ClipOptions | None = None, *,
                   passwords: Callable[[str], str | None] | None = None) -> ClipResult:
    """A new PDF from the clipboard content. ``passwords(file_name)`` is asked
    for password-protected PDFs (None skips the file). Raises ``ValueError``
    when nothing usable is there."""
    opts = options or ClipOptions()
    built = _build(content, opts, passwords, paper=None, text_page=opts.text_page or A4,
                   text_style=opts.text_style)
    try:
        data = write_bytes(built.doc, garbage=3, deflate=True)
        pages = built.doc.page_count
    finally:
        built.doc.close()
    return ClipResult(data, built.kind, pages, built.skipped, built.alternative)


def insert_content(pdf, content: ClipContent, at: int, options: ClipOptions | None = None, *,
                   passwords: Callable[[str], str | None] | None = None) -> ClipInsert:
    """Insert the clipboard content as new pages before index ``at`` (one
    ``pdf.edit`` – one undo step). Default ``page_size`` "match": images on
    the neighbour page's paper size; text in the neighbour page's size with
    the document's body style. Raises ``ValueError`` when nothing usable is
    there."""
    opts = options or ClipOptions(page_size="match")
    count = pdf.page_count
    if not 0 <= at <= count:
        raise ValueError(tr("clipcore.err.position", pos=at))
    if count:
        r = pdf.doc[at - 1 if at > 0 else 0].rect          # display size (/Rotate applied)
        neighbour = (float(r.width), float(r.height))
    else:  # pragma: no cover - a PdfDocument always has a page
        neighbour = A4
    paper = neighbour if (opts.page_size or "").lower() == "match" else None
    style = opts.text_style
    if style is None and chosen_kind(content, opts.prefer) in ("text", "html"):
        from .insert import document_text_style

        style = document_text_style(pdf.doc)
    built = _build(content, opts, passwords, paper=paper, text_page=opts.text_page or neighbour,
                   text_style=style)
    try:
        n = built.doc.page_count
        with pdf.edit(tr("clipcore.undo.insert"), STRUCTURE, None):
            pdf.doc.insert_pdf(built.doc, start_at=at if at < pdf.page_count else -1, links=True, annots=True)
    finally:
        built.doc.close()
    return ClipInsert(list(range(at, at + n)), built.kind, built.skipped, built.alternative)


__all__ = ["A4", "KINDS", "SUPPORTED_FILE_EXTENSIONS", "ClipContent", "ClipInsert", "ClipOptions", "ClipResult",
           "body_font", "chosen_kind", "content_to_pdf", "html_to_pdf", "insert_content", "text_to_pdf",
           "usable_files"]
