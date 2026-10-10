"""Insert new content that matches the document: text boxes in the
document's body style and images. Geometry is in display space."""

from __future__ import annotations

import pymupdf

from .document import CONTENT
from .fonts import SpanStyle, resolve_font, style_from_span, text_width
from .text_edit import TextRun, _label, _wrap, write_runs

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


def add_text_box(pdf, page: int, rect, text: str, style: SpanStyle, align: str = "left") -> None:
    """Write ``text`` (``\\n`` separates paragraphs) wrapped to the width of
    the display-space ``rect``, first baseline at the rect's top + ascender.
    Text that does not fit vertically continues below the rect (never
    dropped). Upright on rotated pages."""
    if align not in ALIGNMENTS:
        raise ValueError(f"align: {align!r}")
    text = (text or "").replace("\r\n", "\n").replace("\r", "\n").replace("\t", "    ")
    if not text.strip():
        return
    rect = pymupdf.Rect(rect)
    doc = pdf.doc
    choice = resolve_font(doc, page, style, text)
    font = choice.font
    size = float(style.size)
    asc = float(getattr(font, "ascender", 0.9) or 0.9)
    desc = float(getattr(font, "descender", -0.25) or -0.25)
    line_h = size * max(1.15, asc - desc)
    width = max(rect.width, size)

    lines: list[str] = []
    for para in text.split("\n"):
        para = para.rstrip()
        lines.extend(_wrap(para, font, size, width) if para else [""])
    while lines and not lines[-1].strip():
        lines.pop()

    with pdf.edit(_label("core.undo.add_text", "Tilføj tekst"), CONTENT, (page,)):
        pg = pdf.doc[page]
        m, d = _display_to_raw(pg)
        runs = []
        for i, line in enumerate(lines):
            if not line.strip():
                continue
            w = text_width(font, line, size)
            if align == "center":
                x = rect.x0 + (rect.width - w) / 2
            elif align == "right":
                x = rect.x1 - w
            else:
                x = rect.x0
            y = rect.y0 + asc * size + i * line_h
            p = pymupdf.Point(x, y) * m
            runs.append(TextRun(text=line, font=font, size=size, color=tuple(style.color),
                                origin=(p.x, p.y), dir=d))
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
