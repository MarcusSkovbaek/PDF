"""Font resolution for editing and inserting text.

The goal is that edited text looks like it was always there, so a font is
picked in this order (see :func:`resolve_font`):

1. **embedded** – the font program embedded in the PDF for the original span,
   if it can be loaded, its character map provably matches the glyphs the PDF
   actually uses (checked against ``page.get_texttrace()``) and it contains a
   glyph for every character of the new text (subset fonts often do not).
2. **builtin** for the standard-14 families (Helvetica/Times/Courier) when the
   PDF does not embed them – MuPDF renders those with exactly these fonts.
3. **system** – a locally installed font found by name (PostScript name, full
   name, family + style, plus metric-compatible aliases such as
   Arial <-> Liberation Sans, Calibri <-> Carlito).
4. **builtin** – PyMuPDF's built-in Nimbus fonts (Latin, Greek, Cyrillic)
   chosen by serif / mono / bold / italic.
5. broad-coverage fallbacks (system Unicode fonts, MuPDF's CJK font).

The system font index is built lazily on first use from the font files'
``name`` tables (parsed directly, a few hundred bytes per file) and cached
for the process lifetime. Nothing is ever written to disk.
"""

from __future__ import annotations

import hashlib
import math
import os
import re
import struct
import sys
import threading
from dataclasses import dataclass, field

import pymupdf

__all__ = [
    "SpanStyle",
    "FontChoice",
    "SystemFace",
    "resolve_font",
    "available_families",
    "qt_family_hint",
    "style_from_span",
    "clean_font_name",
    "font_covers",
    "text_width",
    "prepare_text",
    "missing_chars",
    "system_font_index",
    "warm_up",
]


# ---------------------------------------------------------------------------
# data classes
# ---------------------------------------------------------------------------
@dataclass
class SpanStyle:
    font: str
    size: float
    color: tuple = (0.0, 0.0, 0.0)
    bold: bool = False
    italic: bool = False
    serif: bool = False
    mono: bool = False


@dataclass
class FontChoice:
    font: pymupdf.Font
    name: str
    source: str  # "embedded" | "system" | "builtin"


@dataclass(frozen=True)
class SystemFace:
    path: str
    index: int  # face index inside a .ttc collection
    family: str
    subfamily: str
    full: str
    ps: str
    bold: bool
    italic: bool
    weight: int = 400
    width: int = 5
    serif: bool | None = None
    mono: bool = False
    legacy_family: str = ""


# ---------------------------------------------------------------------------
# name helpers
# ---------------------------------------------------------------------------
_SUBSET_RE = re.compile(r"^[A-Z]{6}\+")
_TOKEN_RE = re.compile(r"[A-Z]+(?=[A-Z][a-z])|[A-Z]?[a-z]+|[A-Z]+|\d+")

_STYLE_WORDS = {
    "regular", "normal", "book", "roman", "plain", "bold", "bd", "italic", "it",
    "oblique", "obl", "semibold", "demibold", "demi", "semi", "extrabold",
    "ultrabold", "black", "heavy", "light", "extralight", "ultralight", "thin",
    "medium", "md", "condensed", "cond", "narrow", "kursiv", "ital", "bolditalic",
    "boldoblique", "bi", "mt", "ps", "psmt", "lt", "std", "pro", "extra", "ultra",
}
_NOISE_WORDS = {"regular", "mt", "ps", "psmt", "normal", "book", "plain"}

_BOLD_RE = re.compile(r"bold|black|heavy|semibold|demibold|demi\b|extrabold|ultrabold|fett|\bbd\b", re.I)
_ITALIC_RE = re.compile(r"italic|oblique|kursiv|slanted|inclined|\bit\b|\bital\b", re.I)
_MONO_RE = re.compile(
    r"courier|mono|consolas|console|menlo|monaco|inconsolata|typewriter|cousine|fixed|\bcode\b|lettergothic|andale",
    re.I,
)
_SANS_RE = re.compile(
    r"sans|arial|helvetica|calibri|carlito|arimo|verdana|tahoma|segoe|gothic|grotesk|grotesque|inter\b|roboto|"
    r"frutiger|univers|futura|myriad|lato|trebuchet|candara|corbel|gill|avenir|ubuntu|cantarell|nunito|"
    r"montserrat|poppins|raleway|franklin|century ?gothic|lucida ?grande|selawik|aptos|dejavusans|nimbussans|"
    r"helv|loma|freesans",
    re.I,
)
_SERIF_RE = re.compile(
    r"times|roman|serif|georgia|garamond|cambria|caladea|palatino|antiqua|minion|baskerville|bodoni|didot|"
    r"century|charter|constantia|tinos|merriweather|cormorant|bookman|schoolbook|sabon|utopia|perpetua|"
    r"goudy|caslon|plantin|tiro|cochin|hoefler|charis|gelasio|joanna|rockwell|playfair|libre ?baskerville|"
    r"\bbook ?antiqua|freeserif",
    re.I,
)


def clean_font_name(name: str) -> str:
    """Strip a subset prefix (``ABCDEF+``) and surrounding whitespace."""
    return _SUBSET_RE.sub("", (name or "").strip())


def _norm(name: str) -> str:
    return re.sub(r"[^a-z0-9]", "", clean_font_name(name).lower())


def _tokens(name: str) -> list[str]:
    out: list[str] = []
    for part in re.split(r"[-,_\s]+", clean_font_name(name)):
        if not part:
            continue
        toks = _TOKEN_RE.findall(part)
        out.extend(toks if toks else [part])
    return out


def _loose(name: str) -> str:
    """Name normalised for "is this the same font" comparisons."""
    toks = [t.lower() for t in _tokens(name)]
    toks = [t for t in toks if t not in _NOISE_WORDS] or toks
    return "".join(toks)


def family_key(name: str) -> str:
    """Normalised family name without style words: ``"Arial-BoldMT"`` ->
    ``"arial"``, ``"TimesNewRomanPS-ItalicMT"`` -> ``"timesnewroman"``,
    ``"Times-Roman"`` -> ``"times"``."""
    toks = [t.lower() for t in _tokens(name)]
    while len(toks) > 1:
        last = toks[-1]
        if last == "roman" and toks[-2] == "new":
            break
        if last in _STYLE_WORDS:
            toks.pop()
            continue
        break
    return "".join(toks)


def _is_bold_name(name: str) -> bool:
    return bool(_BOLD_RE.search(" ".join(_tokens(name)))) or bool(_BOLD_RE.search(name))


def _is_italic_name(name: str) -> bool:
    return bool(_ITALIC_RE.search(" ".join(_tokens(name)))) or bool(re.search(r"italic|oblique|kursiv", name, re.I))


def _serif_from_name(name: str) -> bool | None:
    spaced = " ".join(_tokens(name))
    for candidate in (name, spaced):
        if _SANS_RE.search(candidate):
            return False
    for candidate in (name, spaced):
        if _SERIF_RE.search(candidate):
            return True
    return None


def _is_mono_name(name: str) -> bool:
    return bool(_MONO_RE.search(name) or _MONO_RE.search(" ".join(_tokens(name))))


def color_from_int(value) -> tuple[float, float, float]:
    if isinstance(value, (tuple, list)):
        vals = [float(v) for v in value]
        if len(vals) == 1:
            return (vals[0],) * 3
        if len(vals) == 4:  # CMYK -> RGB (naive)
            c, m, y, k = vals
            return ((1 - c) * (1 - k), (1 - m) * (1 - k), (1 - y) * (1 - k))
        return tuple(vals[:3])  # type: ignore[return-value]
    v = int(value or 0)
    return (((v >> 16) & 255) / 255.0, ((v >> 8) & 255) / 255.0, (v & 255) / 255.0)


def style_from_span(font: str, size: float, color, flags: int = 0) -> SpanStyle:
    """Build a :class:`SpanStyle` from PyMuPDF span data (``font``, ``size``,
    ``color`` as sRGB int or tuple, ``flags``)."""
    name = clean_font_name(font) or "Helvetica"
    flags = int(flags or 0)
    bold = bool(flags & pymupdf.TEXT_FONT_BOLD) or _is_bold_name(name)
    italic = bool(flags & pymupdf.TEXT_FONT_ITALIC) or _is_italic_name(name)
    mono = bool(flags & pymupdf.TEXT_FONT_MONOSPACED) or _is_mono_name(name)
    serif_name = _serif_from_name(name)
    serif = serif_name if serif_name is not None else bool(flags & pymupdf.TEXT_FONT_SERIFED)
    if mono:
        serif = False if serif_name is None else serif
    return SpanStyle(
        font=name,
        size=round(float(size), 2),
        color=tuple(round(c, 4) for c in color_from_int(color)),
        bold=bold,
        italic=italic,
        serif=bool(serif),
        mono=mono,
    )


# ---------------------------------------------------------------------------
# glyph coverage
# ---------------------------------------------------------------------------
def _needed_chars(text: str) -> set[str]:
    out = set()
    for ch in set(text or ""):
        if ch in " \u00a0":
            continue  # spaces are measured by the writer even without a glyph
        if ord(ch) < 32 or ch.isspace() or ch in "\u00ad\u200b\u200c\u200d\ufeff":
            continue
        out.add(ch)
    return out


def missing_chars(font: pymupdf.Font, text: str) -> set[str]:
    missing = set()
    for ch in _needed_chars(text):
        try:
            if font.has_glyph(ord(ch)) <= 0:
                missing.add(ch)
        except Exception:
            missing.add(ch)
    return missing


def font_covers(font: pymupdf.Font, text: str) -> bool:
    return not missing_chars(font, text)


_INVISIBLE = dict.fromkeys(map(ord, "\u00ad\u200b\u200c\u200d\ufeff"), None)


def prepare_text(font: pymupdf.Font, text: str) -> str:
    """Normalise text for writing with ``font``: drop invisible format
    characters, map no-break spaces to spaces when the font lacks them."""
    text = (text or "").translate(_INVISIBLE)
    if "\u00a0" in text:
        try:
            if font.has_glyph(0xA0) <= 0:
                text = text.replace("\u00a0", " ")
        except Exception:
            text = text.replace("\u00a0", " ")
    return text


def has_space(font: pymupdf.Font) -> bool:
    try:
        return font.has_glyph(32) > 0
    except Exception:
        return False


def text_width(font: pymupdf.Font, text: str, size: float, space: float | None = None) -> float:
    """Advance width of ``text`` in points. ``space`` (points) is used for
    spaces when the font has no space glyph."""
    text = prepare_text(font, text)
    if has_space(font) or " " not in text:
        return font.text_length(text, fontsize=size)
    sp = space if space else 0.25 * size
    return sum(font.text_length(w, fontsize=size) for w in text.split(" ") if w) + sp * text.count(" ")


# ---------------------------------------------------------------------------
# font object cache (identity matters: MuPDF reuses an embedded font when the
# same font program is written again within a session)
# ---------------------------------------------------------------------------
_font_cache: dict[tuple, pymupdf.Font] = {}
_font_cache_lock = threading.Lock()
_FONT_CACHE_MAX = 48


def _cache_put(key: tuple, font: pymupdf.Font) -> pymupdf.Font:
    with _font_cache_lock:
        if len(_font_cache) >= _FONT_CACHE_MAX:
            # drop the oldest buffer-backed entries first
            for k in list(_font_cache)[: _FONT_CACHE_MAX // 4]:
                _font_cache.pop(k, None)
        _font_cache[key] = font
    return font


def _font_from_buffer(buf: bytes) -> pymupdf.Font | None:
    key = ("buf", hashlib.sha1(buf).hexdigest(), len(buf))
    f = _font_cache.get(key)
    if f is not None:
        return f
    try:
        f = pymupdf.Font(fontbuffer=buf)
    except Exception:
        return None
    return _cache_put(key, f)


_BUILTIN_CODES = {
    # (serif, mono, bold, italic) -> code
    (False, False, False, False): "helv", (False, False, True, False): "hebo",
    (False, False, False, True): "heit", (False, False, True, True): "hebi",
    (True, False, False, False): "tiro", (True, False, True, False): "tibo",
    (True, False, False, True): "tiit", (True, False, True, True): "tibi",
}
_MONO_CODES = {(False, False): "cour", (True, False): "cobo", (False, True): "coit", (True, True): "cobi"}


def _builtin_code(serif: bool, mono: bool, bold: bool, italic: bool) -> str:
    if mono:
        return _MONO_CODES[(bool(bold), bool(italic))]
    return _BUILTIN_CODES[(bool(serif), False, bool(bold), bool(italic))]


def _builtin_font(code: str) -> pymupdf.Font | None:
    key = ("builtin", code)
    f = _font_cache.get(key)
    if f is not None:
        return f
    try:
        f = pymupdf.Font(code)
    except Exception:
        return None
    return _cache_put(key, f)


# Standard-14 families -> are rendered by MuPDF with its Nimbus clones.
_BASE14_FAMILIES = {"helvetica": (False, False), "times": (True, False), "timesroman": (True, False),
                    "courier": (False, True), "helv": (False, False), "tiro": (True, False),
                    "cour": (False, True)}


# ---------------------------------------------------------------------------
# system font index
# ---------------------------------------------------------------------------
_FONT_EXTS = (".ttf", ".otf", ".ttc", ".otc")


def font_directories() -> list[str]:
    dirs: list[str] = []
    home = os.path.expanduser("~")
    if sys.platform.startswith("win"):
        windir = os.environ.get("WINDIR") or os.environ.get("SystemRoot") or r"C:\Windows"
        dirs.append(os.path.join(windir, "Fonts"))
        local = os.environ.get("LOCALAPPDATA")
        if local:
            dirs.append(os.path.join(local, "Microsoft", "Windows", "Fonts"))
    elif sys.platform == "darwin":
        dirs += ["/System/Library/Fonts", "/System/Library/Fonts/Supplemental", "/Library/Fonts",
                 os.path.join(home, "Library", "Fonts")]
    else:
        dirs += ["/usr/share/fonts", "/usr/local/share/fonts", os.path.join(home, ".fonts"),
                 os.path.join(home, ".local", "share", "fonts")]
        xdg = os.environ.get("XDG_DATA_HOME")
        if xdg:
            dirs.append(os.path.join(xdg, "fonts"))
    seen, out = set(), []
    for d in dirs:
        key = os.path.normcase(os.path.abspath(d))
        if key not in seen and os.path.isdir(d):
            seen.add(key)
            out.append(d)
    return out


def _scan_font_files(dirs: list[str]) -> list[str]:
    files: list[str] = []
    for d in dirs:
        for root, _subdirs, names in os.walk(d):
            for n in names:
                if n.lower().endswith(_FONT_EXTS):
                    files.append(os.path.join(root, n))
    files.sort()
    return files


def _decode_name(platform: int, encoding: int, raw: bytes) -> str:
    try:
        if platform in (0, 3):
            return raw.decode("utf-16-be", "ignore")
        if platform == 1:
            return raw.decode("mac_roman", "ignore")
    except Exception:
        pass
    return raw.decode("latin-1", "ignore")


def _parse_name_table(data: bytes) -> dict[int, str]:
    if len(data) < 6:
        return {}
    _fmt, count, str_off = struct.unpack(">HHH", data[:6])
    best: dict[int, tuple[int, str]] = {}
    for k in range(count):
        rec = data[6 + 12 * k: 18 + 12 * k]
        if len(rec) < 12:
            break
        pid, eid, lid, nid, length, off = struct.unpack(">HHHHHH", rec)
        if nid not in (1, 2, 4, 6, 16, 17):
            continue
        if pid == 3 and eid in (0, 1, 10):
            prio = 0 if lid == 0x409 else (1 if (lid & 0xFF) == 0x09 else 2)
        elif pid == 0:
            prio = 3
        elif pid == 1 and eid == 0:
            prio = 4 if lid == 0 else 6
        else:
            continue
        raw = data[str_off + off: str_off + off + length]
        text = _decode_name(pid, eid, raw).strip("\x00 ").strip()
        if not text:
            continue
        if nid not in best or prio < best[nid][0]:
            best[nid] = (prio, text)
    return {k: v for k, (_p, v) in best.items()}


def _read_faces(path: str) -> list[SystemFace]:
    faces: list[SystemFace] = []
    with open(path, "rb") as fh:
        head = fh.read(12)
        if len(head) < 12:
            return faces
        tag = head[:4]
        if tag == b"ttcf":
            num = struct.unpack(">I", head[8:12])[0]
            if num <= 0 or num > 256:
                return faces
            offsets = struct.unpack(f">{num}I", fh.read(4 * num))
        elif tag in (b"\x00\x01\x00\x00", b"OTTO", b"true"):
            offsets = (0,)
        else:
            return faces
        for index, off in enumerate(offsets):
            fh.seek(off)
            hdr = fh.read(12)
            if len(hdr) < 12:
                continue
            num_tables = struct.unpack(">H", hdr[4:6])[0]
            if num_tables > 200:
                continue
            dirdata = fh.read(16 * num_tables)
            tables: dict[bytes, tuple[int, int]] = {}
            for t in range(num_tables):
                entry = dirdata[16 * t: 16 * t + 16]
                if len(entry) < 16:
                    break
                ttag, _chk, toff, tlen = struct.unpack(">4sIII", entry)
                tables[ttag] = (toff, tlen)
            if b"name" not in tables:
                continue
            toff, tlen = tables[b"name"]
            fh.seek(toff)
            names = _parse_name_table(fh.read(min(tlen, 1 << 20)))
            family = names.get(16) or names.get(1) or ""
            subfamily = names.get(17) or names.get(2) or ""
            if not family or family.startswith("."):
                continue
            weight, width, fs_type, fs_sel, fam_class, panose_serif = 400, 5, 0, 0, 0, 0
            if b"OS/2" in tables:
                ooff, olen = tables[b"OS/2"]
                fh.seek(ooff)
                os2 = fh.read(min(olen, 96))
                if len(os2) >= 64:
                    weight, width, fs_type = struct.unpack(">HHH", os2[4:10])
                    fam_class = struct.unpack(">h", os2[30:32])[0] >> 8
                    panose_serif = os2[33]
                    fs_sel = struct.unpack(">H", os2[62:64])[0]
            mono = False
            if b"post" in tables:
                poff, plen = tables[b"post"]
                fh.seek(poff)
                post = fh.read(16)
                if len(post) >= 16:
                    mono = struct.unpack(">I", post[12:16])[0] != 0
            if (fs_type & 0x000F) == 0x0002:
                continue  # restricted license: must not be embedded
            style_text = f"{subfamily} {names.get(4, '')}"
            bold = bool(fs_sel & 0x20) or weight >= 600 or _is_bold_name(subfamily)
            italic = bool(fs_sel & 0x01) or bool(fs_sel & 0x200) or _is_italic_name(style_text)
            serif: bool | None = _serif_from_name(family)
            if serif is None:
                if fam_class in (1, 2, 3, 4, 5, 7):
                    serif = True
                elif fam_class == 8 or panose_serif in (11, 12, 13):
                    serif = False
                elif 2 <= panose_serif <= 10:
                    serif = True
            faces.append(SystemFace(
                path=path, index=index, family=family, subfamily=subfamily,
                full=names.get(4, ""), ps=names.get(6, ""), bold=bold, italic=italic,
                weight=int(weight or 400), width=int(width or 5), serif=serif,
                mono=mono or _is_mono_name(family), legacy_family=names.get(1, ""),
            ))
    return faces


@dataclass
class _FontIndex:
    faces: list[SystemFace] = field(default_factory=list)
    by_ps: dict[str, SystemFace] = field(default_factory=dict)
    by_full: dict[str, SystemFace] = field(default_factory=dict)
    by_family: dict[str, list[SystemFace]] = field(default_factory=dict)


_index: _FontIndex | None = None
_index_lock = threading.Lock()


def _build_index(dirs: list[str] | None = None) -> _FontIndex:
    idx = _FontIndex()
    for path in _scan_font_files(dirs if dirs is not None else font_directories()):
        try:
            faces = _read_faces(path)
        except Exception:
            continue
        for face in faces:
            idx.faces.append(face)
            if face.ps:
                idx.by_ps.setdefault(_norm(face.ps), face)
            if face.full:
                idx.by_full.setdefault(_norm(face.full), face)
            keys = {_norm(face.family), family_key(face.family)}
            if face.legacy_family:
                keys.add(_norm(face.legacy_family))
            for key in keys:
                if key:
                    lst = idx.by_family.setdefault(key, [])
                    if face not in lst:
                        lst.append(face)
    return idx


def system_font_index(rebuild: bool = False) -> _FontIndex:
    """The (lazily built, cached) index of locally installed fonts."""
    global _index
    if _index is not None and not rebuild:
        return _index
    with _index_lock:
        if _index is None or rebuild:
            _index = _build_index()
    return _index


def warm_up() -> None:
    """Build the system font index now (e.g. from a background thread)."""
    system_font_index()


def _extract_ttc_face(path: str, index: int) -> bytes:
    """Return a standalone sfnt font for face ``index`` of a collection."""
    with open(path, "rb") as fh:
        data = fh.read()
    num = struct.unpack(">I", data[8:12])[0]
    off = struct.unpack(">I", data[12 + 4 * index: 16 + 4 * index])[0]
    if index >= num:
        raise ValueError("bad face index")
    sfnt_version = data[off: off + 4]
    num_tables = struct.unpack(">H", data[off + 4: off + 6])[0]
    entries = []
    for t in range(num_tables):
        e = off + 12 + 16 * t
        entries.append(struct.unpack(">4sIII", data[e: e + 16]))
    header = bytearray(sfnt_version + data[off + 4: off + 12])
    dir_size = 12 + 16 * num_tables
    body = bytearray()
    table_dir = bytearray()
    pos = dir_size
    for ttag, chk, toff, tlen in entries:
        chunk = data[toff: toff + tlen]
        table_dir += struct.pack(">4sIII", ttag, chk, pos, tlen)
        body += chunk
        pad = (-len(chunk)) % 4
        body += b"\0" * pad
        pos += tlen + pad
    return bytes(header + table_dir + body)


def _load_face(face: SystemFace) -> pymupdf.Font | None:
    key = ("file", face.path, face.index)
    f = _font_cache.get(key)
    if f is not None:
        return f
    try:
        if face.path.lower().endswith((".ttc", ".otc")):
            f = pymupdf.Font(fontbuffer=_extract_ttc_face(face.path, face.index))
        else:
            f = pymupdf.Font(fontfile=face.path)
    except Exception:
        return None
    return _cache_put(key, f)


# family aliases: metric-compatible or visually closest substitutes
_ALIASES: dict[str, list[str]] = {
    "helvetica": ["arial", "liberationsans", "arimo", "nimbussans", "helveticaneue", "freesans"],
    "helveticaneue": ["helvetica", "arial", "liberationsans", "arimo"],
    "arial": ["liberationsans", "arimo", "helvetica", "nimbussans", "freesans"],
    "arialnarrow": ["liberationsansnarrow", "nimbussansnarrow"],
    "liberationsans": ["arial", "arimo", "helvetica"],
    "arimo": ["arial", "liberationsans"],
    "times": ["timesnewroman", "liberationserif", "tinos", "nimbusroman", "freeserif"],
    "timesroman": ["timesnewroman", "liberationserif", "tinos", "nimbusroman"],
    "timesnewroman": ["times", "liberationserif", "tinos", "nimbusroman", "freeserif"],
    "liberationserif": ["timesnewroman", "tinos", "times"],
    "tinos": ["timesnewroman", "liberationserif"],
    "courier": ["couriernew", "liberationmono", "cousine", "nimbusmono", "nimbusmonops", "freemono"],
    "couriernew": ["courier", "liberationmono", "cousine", "nimbusmonops", "freemono"],
    "liberationmono": ["couriernew", "cousine"],
    "calibri": ["carlito"],
    "carlito": ["calibri"],
    "cambria": ["caladea"],
    "caladea": ["cambria"],
    "georgia": ["gelasio"],
    "segoeui": ["selawik", "opensans", "notosans"],
    "verdana": ["dejavusans"],
    "tahoma": ["dejavusans"],
    "symbol": ["standardsymbolsps"],
}

# broad-coverage fonts tried when nothing else contains all characters
_BROAD_SANS = ["arial", "segoeui", "notosans", "dejavusans", "freesans", "arialunicodems", "tahoma",
               "liberationsans"]
_BROAD_SERIF = ["timesnewroman", "notoserif", "dejavuserif", "freeserif", "liberationserif"]
_BROAD_CJK = ["microsoftyahei", "msyahei", "simsun", "yugothic", "msgothic", "meiryo", "malgungothic",
              "notosanscjk", "notosanscjksc", "notosanscjkjp", "sourcehansans", "wenquanyizenhei",
              "ipagothic", "nirmalaui", "leelawadeeui", "ebrima", "segoeuisymbol", "segoeuiemoji"]


def _pick_face(faces: list[SystemFace], bold: bool, italic: bool) -> list[SystemFace]:
    want_weight = 700 if bold else 400

    def score(f: SystemFace) -> float:
        s = 0.0
        s += 4 if f.bold != bool(bold) else 0
        s += 3 if f.italic != bool(italic) else 0
        s += abs(f.weight - want_weight) / 400.0
        s += abs(f.width - 5) * 0.4
        return s

    return sorted(faces, key=score)


def _system_candidates(style: SpanStyle) -> list[SystemFace]:
    """System faces matching the style's font name, best first."""
    idx = system_font_index()
    out: list[SystemFace] = []
    name = clean_font_name(style.font)
    exact = idx.by_ps.get(_norm(name)) or idx.by_full.get(_norm(name))
    if exact is not None:
        out.append(exact)
    fam = family_key(name)
    keys = [fam, _norm(name)]
    # e.g. "TimesNewRomanPS" -> "timesnewroman"
    stripped = re.sub(r"(psmt|ps|mt|std|pro|lt)$", "", fam)
    if stripped and stripped != fam:
        keys.append(stripped)
    for key in list(keys):
        keys.extend(_ALIASES.get(key, []))
    seen = set()
    for key in keys:
        if not key or key in seen:
            continue
        seen.add(key)
        faces = idx.by_family.get(key)
        if faces:
            for face in _pick_face(faces, style.bold, style.italic):
                # never trade the requested weight/slant for a looser alias
                if face.bold == bool(style.bold) and face.italic == bool(style.italic):
                    out.append(face)
            if out:
                break
    if not out:
        # same family exists, but not in the requested style: accept the
        # closest face of the first family that exists
        for key in keys:
            faces = idx.by_family.get(key)
            if faces:
                out.extend(_pick_face(faces, style.bold, style.italic))
                break
    # dedupe preserving order
    res, seen_faces = [], set()
    for f in out:
        k = (f.path, f.index)
        if k not in seen_faces:
            seen_faces.add(k)
            res.append(f)
    return res


def _family_faces(keys: list[str], bold: bool, italic: bool) -> list[SystemFace]:
    idx = system_font_index()
    out = []
    for key in keys:
        faces = idx.by_family.get(key)
        if faces:
            out.extend(_pick_face(faces, bold, italic)[:2])
    return out


# ---------------------------------------------------------------------------
# embedded fonts
# ---------------------------------------------------------------------------
def _trace_glyphs(page: pymupdf.Page, want: str) -> dict[int, int]:
    """unicode -> glyph id actually used on the page for spans of font ``want``
    (a :func:`_loose` name)."""
    pairs: dict[int, int] = {}
    try:
        trace = page.get_texttrace()
    except Exception:
        return pairs
    for span in trace:
        if _loose(span.get("font", "")) != want:
            continue
        for ch in span.get("chars", ()):
            u, gid = ch[0], ch[1]
            if u is None or gid is None or gid <= 0 or u == 0xFFFD or u < 32:
                continue
            pairs.setdefault(int(u), int(gid))
    return pairs


def _checksum(data: bytes) -> int:
    padded = data + b"\0" * ((-len(data)) % 4)
    return sum(struct.unpack(f">{len(padded) // 4}I", padded)) & 0xFFFFFFFF


def _build_cmap(mapping: dict[int, int]) -> bytes:
    items = sorted((u, g) for u, g in mapping.items() if 0 < g < 65536 and 0 <= u <= 0x10FFFF)
    # format 12 (full Unicode)
    groups: list[list[int]] = []
    for u, g in items:
        if groups and u == groups[-1][1] + 1 and g == groups[-1][2] + (u - groups[-1][0]):
            groups[-1][1] = u
        else:
            groups.append([u, u, g])
    f12 = struct.pack(">HHIII", 12, 0, 16 + 12 * len(groups), 0, len(groups))
    f12 += b"".join(struct.pack(">III", a, b, g) for a, b, g in groups)
    # format 4 (BMP), one segment per code point
    segs = [(u, u, (g - u) % 65536) for u, g in items if u < 0xFFFF][:8000]
    segs.append((0xFFFF, 0xFFFF, 1))
    n = len(segs)
    es = int(math.floor(math.log2(n)))
    sr = 2 * (2 ** es)
    f4_body = (struct.pack(f">{n}H", *[e for _s, e, _d in segs]) + struct.pack(">H", 0)
               + struct.pack(f">{n}H", *[s for s, _e, _d in segs])
               + struct.pack(f">{n}H", *[d for _s, _e, d in segs])
               + struct.pack(f">{n}H", *([0] * n)))
    f4 = struct.pack(">HHHHHHH", 4, 14 + len(f4_body), 0, 2 * n, sr, es, 2 * n - sr) + f4_body
    head = struct.pack(">HH", 0, 2)
    head += struct.pack(">HHI", 3, 1, 4 + 16)
    head += struct.pack(">HHI", 3, 10, 4 + 16 + len(f4))
    return head + f4 + f12


def _sfnt_with_cmap(buf: bytes, mapping: dict[int, int]) -> bytes | None:
    """Return the sfnt font ``buf`` with its cmap replaced by ``mapping``
    (unicode -> glyph id). Used for subset fonts whose own cmap is missing
    or does not match the glyphs the PDF uses."""
    if len(buf) < 12 or buf[:4] not in (b"\x00\x01\x00\x00", b"OTTO", b"true") or not mapping:
        return None
    try:
        num = struct.unpack(">H", buf[4:6])[0]
        tables: dict[bytes, bytes] = {}
        for i in range(num):
            tag, _chk, off, ln = struct.unpack(">4sIII", buf[12 + 16 * i: 28 + 16 * i])
            tables[tag] = buf[off: off + ln]
        tables[b"cmap"] = _build_cmap(mapping)
        tags = sorted(tables)
        n = len(tags)
        es = int(math.floor(math.log2(n)))
        sr = (2 ** es) * 16
        out_dir = bytearray(buf[:4] + struct.pack(">HHHH", n, sr, es, n * 16 - sr))
        body = bytearray()
        base = 12 + 16 * n
        for tag in tags:
            data = tables[tag]
            out_dir += struct.pack(">4sIII", tag, _checksum(data), base + len(body), len(data))
            body += data + b"\0" * ((-len(data)) % 4)
        return bytes(out_dir + body)
    except Exception:
        return None


def _glyph_visible(font: pymupdf.Font, u: int) -> bool:
    try:
        r = font.glyph_bbox(u)
        return r.width > 1e-4 or r.height > 1e-4
    except Exception:
        return True


def _embedded_covers(font: pymupdf.Font, text: str, pairs: dict[int, int]) -> bool:
    """Coverage check for embedded (possibly subset) fonts: every character
    must map to a glyph, and glyphs the page never shows must have an outline
    (subsetters often keep glyph slots but empty them)."""
    for ch in _needed_chars(text):
        u = ord(ch)
        try:
            gid = font.has_glyph(u)
        except Exception:
            return False
        if gid <= 0:
            return False
        if pairs.get(u) == gid:
            continue
        if not _glyph_visible(font, u):
            return False
    return True


def _embedded_candidates(doc: pymupdf.Document, page_index: int, style: SpanStyle):
    """Yield ``(font, pairs)`` for embedded fonts of the page that are proven
    to map unicode -> glyph exactly like the PDF does."""
    try:
        page = doc[page_index]
        fonts = page.get_fonts(full=True)
    except Exception:
        return
    want = _loose(style.font)
    if not want:
        return
    pairs = None
    seen = set()
    for entry in fonts:
        xref, ext, _ftype, basefont = entry[0], entry[1], entry[2], entry[3]
        if xref in seen or ext in ("n/a", "", None):
            continue
        seen.add(xref)
        loose_base = _loose(basefont)
        name_match = loose_base == want
        if not name_match and not (loose_base.startswith(want) or want.startswith(loose_base)):
            continue
        try:
            _n, _e, _t, buf = doc.extract_font(xref)
        except Exception:
            continue
        if not buf:
            continue
        font = _font_from_buffer(buf)
        if font is None:
            continue
        if not name_match and _loose(font.name) != want:
            continue
        if pairs is None:
            pairs = _trace_glyphs(page, want)
        if not pairs:
            continue  # cannot prove the cmap matches what the PDF shows
        if all(font.has_glyph(u) == gid for u, gid in pairs.items()):
            yield font, pairs
            continue
        # cmap missing / wrong (typical for subset fonts): rebuild it from
        # the glyphs the page is known to use
        patched = _sfnt_with_cmap(buf, pairs)
        if patched is None:
            continue
        pfont = _font_from_buffer(patched)
        if pfont is not None and all(pfont.has_glyph(u) == gid for u, gid in pairs.items()):
            yield pfont, pairs


# ---------------------------------------------------------------------------
# public API
# ---------------------------------------------------------------------------
def resolve_font(doc: pymupdf.Document, page_index: int, style: SpanStyle, text: str,
                 *, allow_embedded: bool = True) -> FontChoice:
    """Pick the best font for writing ``text`` in ``style`` on the page.

    The returned font covers every (non-space) character of ``text`` whenever
    any available font does.
    """
    name = clean_font_name(style.font) or "Helvetica"
    # 1. embedded font of the original span
    if allow_embedded and doc is not None and page_index is not None and page_index >= 0:
        for font, pairs in _embedded_candidates(doc, page_index, style):
            if _embedded_covers(font, text, pairs):
                return FontChoice(font, name, "embedded")

    fam = family_key(name)
    # 2. standard-14 family that the PDF does not embed: MuPDF's Nimbus clone
    if fam in _BASE14_FAMILIES:
        serif, mono = _BASE14_FAMILIES[fam]
        code = _builtin_code(serif, mono, style.bold, style.italic)
        font = _builtin_font(code)
        if font is not None and font_covers(font, text):
            return FontChoice(font, font.name, "builtin")

    # 3. same / metric-compatible family installed locally
    try:
        candidates = _system_candidates(style)
    except Exception:
        candidates = []
    for face in candidates:
        font = _load_face(face)
        if font is not None and font_covers(font, text):
            return FontChoice(font, face.family, "system")

    # 4. built-in by classification
    code = _builtin_code(style.serif, style.mono, style.bold, style.italic)
    font = _builtin_font(code)
    if font is not None and font_covers(font, text):
        return FontChoice(font, font.name, "builtin")

    # 5. broad-coverage fallbacks
    keys = (_BROAD_SERIF if style.serif else []) + _BROAD_SANS + _BROAD_CJK
    try:
        broad = _family_faces(keys, style.bold, style.italic)
    except Exception:
        broad = []
    for face in broad:
        font = _load_face(face)
        if font is not None and font_covers(font, text):
            return FontChoice(font, face.family, "system")
    for cjk in ("china-s", "japan", "korea", "china-t"):
        font = _builtin_font(cjk)
        if font is not None and font_covers(font, text):
            return FontChoice(font, font.name, "builtin")
    try:
        last_resort = _family_faces(["unifont", "unifontupper"], False, False)
    except Exception:
        last_resort = []
    for face in last_resort:
        font = _load_face(face)
        if font is not None and font_covers(font, text):
            return FontChoice(font, face.family, "system")

    # 6. nothing covers everything: the classified built-in, best effort
    font = _builtin_font(code if code else "helv") or pymupdf.Font("helv")
    return FontChoice(font, font.name, "builtin")


def available_families() -> list[str]:
    """Sorted unique family names for a font picker (base-14 + system)."""
    names = {"Helvetica", "Times", "Courier"}
    try:
        for face in system_font_index().faces:
            if face.family and not face.family.startswith((".", "#")):
                names.add(face.family)
    except Exception:
        pass
    return sorted(names, key=lambda s: s.casefold())


_QT_HINTS: dict[str, str] = {
    "helvetica": "Arial", "helv": "Arial", "arial": "Arial", "liberationsans": "Arial", "arimo": "Arial",
    "nimbussans": "Arial", "freesans": "Arial", "helveticaneue": "Arial",
    "arialnarrow": "Arial Narrow", "liberationsansnarrow": "Arial Narrow",
    "times": "Times New Roman", "timesroman": "Times New Roman", "timesnewroman": "Times New Roman",
    "tiro": "Times New Roman", "liberationserif": "Times New Roman", "tinos": "Times New Roman",
    "nimbusroman": "Times New Roman", "freeserif": "Times New Roman",
    "courier": "Courier New", "cour": "Courier New", "couriernew": "Courier New",
    "liberationmono": "Courier New", "cousine": "Courier New", "nimbusmono": "Courier New",
    "nimbusmonops": "Courier New", "freemono": "Courier New",
    "calibri": "Calibri", "carlito": "Calibri", "cambria": "Cambria", "caladea": "Cambria",
    "segoeui": "Segoe UI", "verdana": "Verdana", "tahoma": "Tahoma", "georgia": "Georgia",
    "trebuchetms": "Trebuchet MS", "garamond": "Garamond", "consolas": "Consolas",
    "centurygothic": "Century Gothic", "palatinolinotype": "Palatino Linotype",
    "palatino": "Palatino Linotype", "bookantiqua": "Book Antiqua", "candara": "Candara",
    "corbel": "Corbel", "constantia": "Constantia", "franklingothic": "Franklin Gothic",
    "lucidaconsole": "Lucida Console", "lucidasans": "Lucida Sans", "aptos": "Aptos",
    "symbol": "Symbol", "wingdings": "Wingdings", "impact": "Impact", "comicsansms": "Comic Sans MS",
    "bahnschrift": "Bahnschrift", "sitka": "Sitka Text", "ebrima": "Ebrima",
}


def qt_family_hint(style: SpanStyle) -> str:
    """An on-screen font family (available on Windows) that resembles the
    style – for the inline text editor."""
    name = clean_font_name(style.font)
    fam = family_key(name)
    for key in (fam, _norm(name), re.sub(r"(psmt|ps|mt)$", "", fam)):
        if key in _QT_HINTS:
            return _QT_HINTS[key]
    if _index is not None:
        faces = _index.by_family.get(fam)
        if faces:
            return faces[0].family
    if style.mono:
        return "Courier New"
    if style.serif:
        return "Times New Roman"
    return "Arial"
