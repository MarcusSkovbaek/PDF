"""Running headers, footers and page numbers (Qt-free).

When the text flow adds a page, the page gets the header and footer of the
page before it (text glyph for glyph, rules and logos), and page numbers in
headers and footers ("Side 13 af 58", "13", "- 13 -", "13/58", "Page 3 of
9") are brought up to date on every page after a page was added or removed.

Page numbers are found by their pattern in the top / bottom band of each
page and by agreeing with the page index (printed number − index is the
same on the pages) – a year or an amount in a footer is never touched."""

from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass, replace

import pymupdf

from . import text_edit as te
from .fonts import font_covers, resolve_font, text_width
from .text_edit import TextBlock, TextLine, TextRun, _dot

#: share of the page height at the top and bottom where headers / footers live
ZONE = 0.11

_NUMBER = re.compile(
    r"(?:^|(?<=[\s·|•–—-]))(?P<pre>(?:side|page|s\.|p\.)\s*)?(?P<n>\d{1,4})"
    r"(?:(?P<mid>\s*(?:af|of|/|\|)\s*)(?P<m>\d{1,4}))?(?P<post>\s*[-–—]?\s*)$", re.I)


@dataclass
class NumberLine:
    page: int
    line: TextLine
    n: int                       # the printed page number
    m: int | None                # the printed page count ("af 58"), if any
    n_span: tuple[int, int]      # where they are in ``line.text``
    m_span: tuple[int, int] | None


def _zones(page: pymupdf.Page) -> list[pymupdf.Rect]:
    """Top and bottom band of the page (raw coordinates) – of the page as
    shown and of the page as written (a rotated page's text may run either
    way), so headers and footers are found in both."""
    out = []
    raw = page.rect * page.derotation_matrix
    m = page.derotation_matrix
    for r, mat in ((page.rect, m), (raw, None)):
        h = ZONE * r.height
        for band in (pymupdf.Rect(r.x0, r.y0, r.x1, r.y0 + h), pymupdf.Rect(r.x0, r.y1 - h, r.x1, r.y1)):
            z = band * mat if mat is not None else band
            if not any(abs(z.x0 - o.x0) < 0.5 and abs(z.y0 - o.y0) < 0.5 and abs(z.x1 - o.x1) < 0.5
                       and abs(z.y1 - o.y1) < 0.5 for o in out):
                out.append(z)
    return out


def _band_lines(page: pymupdf.Page) -> list[TextLine]:
    rm = page.rotation_matrix
    out: list[TextLine] = []
    seen: set[tuple] = set()
    for zone in _zones(page):
        data = page.get_text("rawdict", flags=te._EXTRACT_FLAGS, clip=zone)
        for b in data.get("blocks", ()):
            if b.get("type", 0) != 0:
                continue
            for rl in b.get("lines", ()):
                for ln in te._build_lines(rl, rm):
                    key = (round(ln.raw_origin[0], 1), round(ln.raw_origin[1], 1), ln.text)
                    if ln.text.strip() and len(ln.char_pos) == len(ln.text) and key not in seen:
                        seen.add(key)
                        out.append(ln)
    return out


def number_lines(doc: pymupdf.Document, page: int) -> list[NumberLine]:
    """Lines in the page's header / footer band that end in a page number."""
    out = []
    pg = doc[page]
    for ln in _band_lines(pg):
        m = _NUMBER.search(ln.text.rstrip())
        if m is None:
            continue
        out.append(NumberLine(page=page, line=ln, n=int(m.group("n")), m=int(m.group("m")) if m.group("m") else None,
                              n_span=m.span("n"), m_span=m.span("m") if m.group("m") else None))
    return out


def _mode(values: list[int]) -> int | None:
    if not values:
        return None
    (value, count), = Counter(values).most_common(1)
    return value if count >= max(2, len(values) // 3) or len(values) == 1 else None


def renumber(doc: pymupdf.Document, origin: list[int | None], old_count: int, *,
             template: dict[int, int] | None = None) -> list[int]:
    """Bring page numbers up to date after pages were added or removed –
    call inside ``pdf.edit``. ``origin[p]``: the old index of (new) page
    ``p``, None for a page that was added; ``template[p]``: the old page an
    added page copied its header / footer from. Returns changed pages."""
    template = template or {}
    found = {p: number_lines(doc, p) for p in range(doc.page_count)}
    offsets, totals = [], []
    for p, lines in found.items():
        old = origin[p] if p < len(origin) else None
        if old is None:
            continue
        for nl in lines:
            offsets.append(nl.n - old)
            if nl.m is not None:
                totals.append(nl.m - old_count)
    offset = _mode(offsets)
    total = _mode(totals)
    if offset is None and total is None:
        return []
    count = doc.page_count
    changed = []
    fonts: dict = {}
    for p, lines in found.items():
        old = origin[p] if p < len(origin) else None
        src = old if old is not None else template.get(p)
        edits = []
        for nl in lines:
            want_n = nl.n
            if offset is not None and src is not None and nl.n - src == offset:
                want_n = p + offset
            want_m = nl.m
            if total is not None and nl.m is not None and nl.m - old_count == total:
                want_m = count + total
            if want_n != nl.n or want_m != nl.m:
                edits.append((nl, want_n, want_m))
        for nl, want_n, want_m in edits:
            text = nl.line.text
            parts = [(nl.n_span, str(want_n))]
            if nl.m_span is not None:
                parts.append((nl.m_span, str(want_m)))
            for (a, b), s in sorted(parts, reverse=True):
                text = text[:a] + s + text[b:]
            _rewrite_line(doc[p], p, nl.line, text, fonts)
        if edits:
            changed.append(p)
    return changed


def _rewrite_line(page: pymupdf.Page, index: int, line: TextLine, text: str, fonts: dict) -> None:
    """One header / footer line with new text, same font and size, anchored
    like it is on the page (centred, right or left). ``fonts``: fonts
    resolved so far (a style's font serves every page)."""
    d = line.dir
    styles = {id(st) for st in (line.char_styles or [])}
    if len(styles) <= 1:
        st = line.style
        key = (st.font, round(st.size, 2), st.bold, st.italic)
        font = fonts.get(key)
        if font is None or not font_covers(font, text):
            font = resolve_font(page.parent, index, st, text).font
            fonts[key] = font
        width = text_width(font, text, st.size)
        runs = [TextRun(text=text, font=font, size=st.size, color=tuple(st.color), origin=line.raw_origin, dir=d,
                        width=width)]
    else:  # several styles in the line: the full typesetter
        block = TextBlock(page=index, bbox=pymupdf.Rect(line.bbox), lines=[line], style=line.style,
                          rotation=page.rotation)
        runs = [r for r in te._plan(page, block, text, None, emit_all=True).runs if r.text]
    if not runs:
        return
    old0, old1 = line.extent
    new0 = min(_dot(r.origin, d) for r in runs)
    new1 = max(_dot(r.origin, d) + r.width for r in runs)
    raw = page.rect * page.derotation_matrix
    lo, hi = sorted((_dot(raw.tl, d), _dot(raw.br, d)))
    mid = (lo + hi) / 2
    centre = (old0 + old1) / 2
    if abs(centre - mid) <= max(3.0, 0.02 * (hi - lo)):
        shift = centre - (new0 + new1) / 2                  # centred on the page
    elif centre > mid:
        shift = old1 - new1                                 # right-aligned
    else:
        shift = old0 - new0
    if abs(shift) > 1e-6:
        runs = [replace(r, origin=(r.origin[0] + d[0] * shift, r.origin[1] + d[1] * shift)) for r in runs]
    te._remove_lines(page, [line])
    te.write_runs(page, runs)


def copy_running(doc: pymupdf.Document, src: int, dst: int, lines: list[TextLine]) -> list[pymupdf.Rect]:
    """Put the header / footer ``lines`` of page ``src`` onto page ``dst``
    (same place, glyph for glyph), with the rules and logos in the same
    bands. Returns the raw rects they cover."""
    spage, dpage = doc[src], doc[dst]
    covered: list[pymupdf.Rect] = []
    if lines:
        te.write_runs(dpage, te._moved_runs(spage, src, lines, 0.0))
        covered.extend(pymupdf.Rect(ln.raw_bbox) for ln in lines)
    zones = _zones(spage)
    # rules / boxes of the header and footer
    shape = None
    try:
        drawings = spage.get_drawings()
    except Exception:
        drawings = []
    for dr in drawings:
        r = pymupdf.Rect(dr.get("rect"))
        if r.is_infinite or not any(z.contains(r) for z in zones) or not dr.get("items"):
            continue
        if shape is None:
            shape = dpage.new_shape()
        for item in dr["items"]:
            kind = item[0]
            if kind == "l":
                shape.draw_line(item[1], item[2])
            elif kind == "re":
                shape.draw_rect(item[1])
            elif kind == "qu":
                shape.draw_quad(item[1])
            elif kind == "c":
                shape.draw_bezier(item[1], item[2], item[3], item[4])
        shape.finish(color=dr.get("color"), fill=dr.get("fill"), width=dr.get("width") or 1.0,
                     closePath=bool(dr.get("closePath")), even_odd=bool(dr.get("even_odd")),
                     fill_opacity=dr.get("fill_opacity") or 1.0, stroke_opacity=dr.get("stroke_opacity") or 1.0,
                     lineCap=max(dr.get("lineCap") or (0,)) if isinstance(dr.get("lineCap"), (list, tuple))
                     else (dr.get("lineCap") or 0), lineJoin=dr.get("lineJoin") or 0,
                     dashes=dr.get("dashes") or None)
        covered.append(r)
    if shape is not None:
        shape.commit()
    # logos (the same image object – the file does not grow by a copy)
    try:
        infos = spage.get_image_info(xrefs=True)
    except Exception:
        infos = []
    for info in infos:
        xref = info.get("xref") or 0
        r = pymupdf.Rect(info.get("bbox"))
        if xref <= 0 or r.is_empty or not any(z.contains(r) for z in zones):
            continue
        try:
            dpage.insert_image(r, xref=xref, keep_proportion=False)
        except Exception:
            continue
        covered.append(r)
    return covered


__all__ = ["NumberLine", "ZONE", "copy_running", "number_lines", "renumber"]
