"""Insert new content that matches the document: text boxes in the
document's body style and images. Geometry is in display space."""

from __future__ import annotations

import pymupdf

from .document import CONTENT
from .fonts import SpanStyle, resolve_font, style_from_span, text_width
from .text_edit import _apply_formats, _emit_line, _font_key, _label, _rich_text, _runs, _wrap_rich, write_runs

__all__ = ["document_text_style", "add_text_box", "add_image", "ALIGNMENTS"]

ALIGNMENTS = ("left", "center", "right")
_DEFAULT_STYLE = SpanStyle(font="Helvetica", size=11.0, color=(0.0, 0.0, 0.0))


def document_text_style(doc: pymupdf.Document, max_pages: int = 10) -> SpanStyle:
    """The dominant body text style (by character count) of the first
    ``max_pages`` pages; headings (clearly larger sizes) are ignored."""
    stats: dict[tuple, list] = {}  # key -> [chars, span]
    sizes: list[tuple[float, int]] = []
    for pno in range(min(max_pages, doc.page_count)):
        try:
            data = doc[pno].get_text("dict", flags=pymupdf.TEXT_MEDIABOX_CLIP)
        except Exception:
            continue
        for b in data.get("blocks", ()):
            if b.get("type", 0) != 0:
                continue
            for ln in b.get("lines", ()):
                for s in ln.get("spans", ()):
                    n = len(s.get("text", "").strip())
                    if not n or s.get("size", 0) <= 0:
                        continue
                    key = (s.get("font", ""), round(float(s["size"]) * 2) / 2, s.get("color", 0),
                           int(s.get("flags", 0)) & 0x1E)
                    entry = stats.setdefault(key, [0, s])
                    entry[0] += n
                    sizes.append((float(s["size"]), n))
    if not stats:
        return SpanStyle(**vars(_DEFAULT_STYLE))
    # char-weighted median size; anything clearly above it is a heading
    sizes.sort()
    total = sum(n for _s, n in sizes)
    acc, median = 0, sizes[0][0]
    for size, n in sizes:
        acc += n
        if acc * 2 >= total:
            median = size
            break
    body = {k: v for k, v in stats.items() if k[1] <= median * 1.25 + 0.01} or stats
    key = max(body, key=lambda k: body[k][0])
    span = body[key][1]
    return style_from_span(span.get("font", ""), span["size"], span.get("color", 0), span.get("flags", 0))


def _display_to_raw(page: pymupdf.Page):
    m = page.derotation_matrix
    o = pymupdf.Point(0, 0) * m
    ex = pymupdf.Point(1, 0) * m - o
    return m, (ex.x, ex.y)


def add_text_box(pdf, page: int, rect, text: str, style: SpanStyle, align: str = "left", *,
                 formats=None) -> None:
    """Write ``text`` (``\\n`` separates paragraphs) wrapped to the width of
    the display-space ``rect``, first baseline at the rect's top + ascender.
    Text that does not fit vertically continues below the rect (never
    dropped). Upright on rotated pages. ``formats`` formats parts of the text
    like :func:`pdfstudio.core.text_edit.replace_block_text` does (bold,
    italic, underline, strike-through, colour, size, font)."""
    if align not in ALIGNMENTS:
        raise ValueError(f"align: {align!r}")
    text = (text or "").replace("\r\n", "\n").replace("\r", "\n").replace("\t", "    ")
    if not text.strip():
        return
    rect = pymupdf.Rect(rect)
    doc = pdf.doc
    rich = _apply_formats([(c, style, 0.0) for c in text], formats)
    by_key: dict[tuple, SpanStyle] = {}
    texts: dict[tuple, list[str]] = {}
    for ch, st, _r in rich:
        if ch != "\n":
            by_key.setdefault(_font_key(st), st)
            texts.setdefault(_font_key(st), []).append(ch)
    fonts = {k: resolve_font(doc, page, by_key[k], "".join(v)).font for k, v in texts.items()}

    def font_of(st: SpanStyle) -> pymupdf.Font:
        return fonts[_font_key(st)]

    def measure(r) -> float:
        return sum(text_width(font_of(st), t, st.size) for t, st, _rise in _runs(r))

    base_font = font_of(style) if _font_key(style) in fonts else next(iter(fonts.values()))
    size = float(style.size)
    asc = float(getattr(base_font, "ascender", 0.9) or 0.9)
    desc = float(getattr(base_font, "descender", -0.25) or -0.25)
    line_h = size * max(1.15, asc - desc)
    width = max(rect.width, size)

    lines: list[list] = []
    para: list = []
    for c in rich + [("\n", style, 0.0)]:
        if c[0] != "\n":
            para.append(c)
            continue
        while para and para[-1][0].isspace():
            para.pop()
        lines.extend(_wrap_rich(para, width, measure) if para else [[]])
        para = []
    while lines and not _rich_text(lines[-1]).strip():
        lines.pop()

    with pdf.edit(_label("core.undo.add_text", "Tilføj tekst"), CONTENT, (page,)):
        pg = pdf.doc[page]
        m, d = _display_to_raw(pg)
        runs = []
        y = rect.y0 + asc * size
        for line in lines:
            tallest = max([st.size for _c, st, _r in line] + [size])
            if line is not lines[0]:
                y += line_h * tallest / size
            if not _rich_text(line).strip():
                continue
            w = measure(line)
            if align == "center":
                x = rect.x0 + (rect.width - w) / 2
            elif align == "right":
                x = rect.x1 - w
            else:
                x = rect.x0
            p = pymupdf.Point(x, y) * m
            runs.extend(_emit_line(line, (p.x, p.y), d, None, 0.0, font_of))
        write_runs(pg, runs)


def add_image(pdf, page: int, rect, image: str | bytes, keep_aspect: bool = True) -> None:
    """Insert an image file/bytes into the display-space ``rect`` (upright on
    rotated pages). With ``keep_aspect`` the image is fitted and centred."""
    rect = pymupdf.Rect(rect)
    if rect.is_empty or rect.is_infinite:
        raise ValueError("empty rect")
    kwargs = {"filename": image} if isinstance(image, str) else {"stream": bytes(image)}
    with pdf.edit(_label("core.undo.add_image", "Indsæt billede"), CONTENT, (page,)):
        pg = pdf.doc[page]
        raw = rect * pg.derotation_matrix
        raw.normalize()
        rotate = pg.rotation % 360
        pg.insert_image(raw, keep_proportion=keep_aspect, rotate=rotate, **kwargs)
