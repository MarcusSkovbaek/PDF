"""PDF pages from the clipboard (core/clipboard_pdf.py, U1–U17): every kind,
priority and preference, page sizes, lossless images with alpha, Danish text
in an embedded font, HTML without any network access, files with passwords,
insertion as one undo step, Folio's object clip, and speed."""

from __future__ import annotations

import os
import random
import socket
import struct
import time
import zlib

import pymupdf
import pytest

from pdfstudio.core import PdfDocument
from pdfstudio.core import clipboard_pdf as cp
from pdfstudio.core import pageobjects as po
from pdfstudio.core.clipboard_pdf import ClipContent, ClipOptions
from pdfstudio.core.fonts import SpanStyle
from tests.conftest import SANS_TTF, make_png

MM = 72 / 25.4
A4W, A4H = 595.0, 842.0


# -- helpers ---------------------------------------------------------------------
def png_bytes(width: int, height: int, pixel, *, alpha: bool = False, filters=(0, 1, 2)) -> tuple[bytes, bytes]:
    """A PNG written by hand (no Pillow) – ``pixel(x, y)`` gives (r, g, b[, a]) –
    with rows using the given filter types in turn. Returns (png, raw pixels)."""
    n = 4 if alpha else 3
    rows = [bytes(v for x in range(width) for v in pixel(x, y)[:n]) for y in range(height)]
    out = bytearray()
    prev = bytes(width * n)
    for y, row in enumerate(rows):
        f = filters[y % len(filters)]
        if f == 0:
            line = row
        elif f == 1:      # Sub
            line = bytes((row[i] - (row[i - n] if i >= n else 0)) & 255 for i in range(len(row)))
        else:             # Up
            line = bytes((row[i] - prev[i]) & 255 for i in range(len(row)))
        out += bytes([f]) + line
        prev = row

    def chunk(tag: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)

    ihdr = struct.pack(">IIBBBBB", width, height, 8, 6 if alpha else 2, 0, 0, 0)
    png = b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr) + chunk(b"IDAT", zlib.compress(bytes(out), 9)) + chunk(b"IEND", b"")
    return png, b"".join(rows)


def open_pdf(data: bytes) -> pymupdf.Document:
    return pymupdf.open("pdf", data)


def embedded_image(doc: pymupdf.Document, page: int = 0) -> tuple[pymupdf.Pixmap, pymupdf.Pixmap | None]:
    imgs = doc.get_page_images(page, full=True)
    assert len(imgs) == 1, imgs
    xref, smask = imgs[0][0], imgs[0][1]
    return pymupdf.Pixmap(doc, xref), (pymupdf.Pixmap(doc, smask) if smask else None)


def image_rect(doc: pymupdf.Document, page: int = 0) -> pymupdf.Rect:
    infos = doc[page].get_image_info()
    assert len(infos) == 1, infos
    return pymupdf.Rect(infos[0]["bbox"])


def approx_rect(a, b, tol=0.6):
    a, b = pymupdf.Rect(a), pymupdf.Rect(b)
    assert all(abs(x - y) <= tol for x, y in zip(a, b)), (a, b)


def all_text(doc: pymupdf.Document) -> str:
    return "".join(p.get_text() for p in doc)


def write(tmp_path, name: str, data: bytes) -> str:
    path = str(tmp_path / name)
    with open(path, "wb") as fh:
        fh.write(data)
    return path


def small_pdf(pages: int, label: str, size=(400, 300), password: str | None = None) -> bytes:
    doc = pymupdf.open()
    for i in range(pages):
        pg = doc.new_page(width=size[0], height=size[1])
        pg.insert_text((40, 60), f"{label} side {i + 1}", fontname="helv", fontsize=14)
    if password:
        return doc.tobytes(encryption=pymupdf.PDF_ENCRYPT_AES_256, user_pw=password, owner_pw=password + "!")
    return doc.tobytes()


def doc_with(width=A4W, height=A4H, rotation=0, pages=3, font=None) -> PdfDocument:
    doc = pymupdf.open()
    for i in range(pages):
        pg = doc.new_page(width=width, height=height)
        if font:
            pg.insert_font(fontname="Body", fontfile=font)
            pg.insert_text((72, 100), f"Brødtekst på side {i + 1} i dokumentets skrift.", fontname="Body",
                           fontsize=10.5, color=(0.2, 0.2, 0.2))
        else:
            pg.insert_text((72, 100), f"Side {i + 1}", fontname="helv", fontsize=12)
        if rotation:
            pg.set_rotation(rotation)
    return PdfDocument.from_bytes(doc.tobytes())


@pytest.fixture
def no_network(monkeypatch):
    """Fails the test on any attempt to resolve or connect (on top of netguard)."""
    calls = []

    def boom(*a, **k):
        calls.append(a)
        raise AssertionError("network access attempted")

    for name in ("create_connection", "getaddrinfo", "gethostbyname"):
        monkeypatch.setattr(socket, name, boom)
    yield calls
    assert not calls


# -- kinds, priority, preference ---------------------------------------------------
def test_kinds_and_priority(tmp_path):
    png = make_png(80, 40)
    pdf_file = write(tmp_path, "a.pdf", small_pdf(1, "A"))
    c = ClipContent(images=[png], files=[pdf_file], pdf=small_pdf(1, "P"), html="<b>x</b>", text="x")
    assert c.kinds() == ["files", "pdf", "image", "html", "text"]
    assert cp.chosen_kind(c) == "files"
    assert cp.content_to_pdf(c).kind == "files"
    c.files = []
    assert cp.content_to_pdf(c).kind == "pdf"
    c.pdf = None
    r = cp.content_to_pdf(c)
    assert r.kind == "image" and r.alternative == "text"
    assert cp.content_to_pdf(c, ClipOptions(prefer="text")).kind == "text"
    assert cp.content_to_pdf(c, ClipOptions(prefer="html")).kind == "html"
    c.images = []
    r = cp.content_to_pdf(c)
    assert r.kind == "html" and r.alternative is None
    c.html = None
    assert cp.content_to_pdf(c).kind == "text"


def test_prefer_text_falls_back_to_html_and_unknown_prefer_is_ignored():
    c = ClipContent(images=[make_png(50, 50)], html="<p>Kun <b>HTML</b> her</p>")
    r = cp.content_to_pdf(c)
    assert r.kind == "image" and r.alternative == "text"
    r = cp.content_to_pdf(c, ClipOptions(prefer="text"))       # "Brug teksten i stedet"
    assert r.kind == "html" and "Kun HTML her" in all_text(open_pdf(r.data)).replace("\n", " ")
    assert cp.chosen_kind(c, "pdf") == "image"
    assert cp.content_to_pdf(ClipContent(images=[make_png()])).alternative is None


def test_empty_content_raises_in_danish():
    for c in (ClipContent(), ClipContent(text="   \n "), ClipContent(files=["/findes/ikke.pdf"])):
        assert c.is_empty()
        with pytest.raises(ValueError, match="Udklipsholderen"):
            cp.content_to_pdf(c)
        with pytest.raises(ValueError):
            cp.insert_content(doc_with(), c, 0)


# -- images: page sizes and quality ---------------------------------------------------
def test_image_page_is_the_image_at_96_dpi():
    r = cp.content_to_pdf(ClipContent(images=[make_png(1920, 1080)]))
    d = open_pdf(r.data)
    assert r.kind == "image" and r.pages == d.page_count == 1
    assert d[0].rect == pymupdf.Rect(0, 0, 1440, 810)               # 1920×1080 px × 0.75, landscape
    approx_rect(image_rect(d), d[0].rect, 0.01)                     # no white edges


def test_several_images_one_page_each_in_order():
    imgs = [make_png(100, 50, (255, 0, 0)), make_png(40, 80, (0, 0, 255))]
    d = open_pdf(cp.content_to_pdf(ClipContent(images=imgs)).data)
    assert [p.rect for p in d] == [pymupdf.Rect(0, 0, 75, 37.5), pymupdf.Rect(0, 0, 30, 60)]
    assert embedded_image(d, 0)[0].pixel(5, 5) == (255, 0, 0)
    assert embedded_image(d, 1)[0].pixel(5, 5) == (0, 0, 255)


@pytest.mark.parametrize("paper,size", [("A4", (A4W, A4H)), ("Letter", (612, 792)), ("match", (A4W, A4H))])
def test_image_on_paper_orientation_follows_image(paper, size):
    w, h = size
    for px, page in (((1600, 900), (h, w)), ((600, 900), (w, h))):
        d = open_pdf(cp.content_to_pdf(ClipContent(images=[make_png(*px)]),
                                       ClipOptions(page_size=paper, margin_mm=10)).data)
        assert d[0].rect.width == pytest.approx(page[0], abs=0.5)
        assert d[0].rect.height == pytest.approx(page[1], abs=0.5)
        r = image_rect(d)
        m = 10 * MM
        assert r.x0 >= m - 0.5 and r.y0 >= m - 0.5
        assert r.x1 <= d[0].rect.width - m + 0.5 and r.y1 <= d[0].rect.height - m + 0.5
        assert r.width / r.height == pytest.approx(px[0] / px[1], rel=1e-3)      # contained, aspect kept
        assert (r.x0 + r.x1) / 2 == pytest.approx(d[0].rect.width / 2, abs=0.5)  # centred
        assert (r.y0 + r.y1) / 2 == pytest.approx(d[0].rect.height / 2, abs=0.5)
        assert min(r.x0, r.y0) == pytest.approx(m, abs=0.5)                       # touches a margin


def test_rgb_png_is_embedded_pixel_for_pixel():
    png, raw = png_bytes(97, 61, lambda x, y: ((x * 7) % 256, (y * 13) % 256, (x * y) % 256))
    d = open_pdf(cp.content_to_pdf(ClipContent(images=[png])).data)
    base, mask = embedded_image(d)
    assert mask is None and base.n == 3 and (base.width, base.height) == (97, 61)
    assert bytes(base.samples) == raw


def test_rgba_png_is_embedded_losslessly_with_its_alpha():
    def px(x, y):
        a = (x * 9 + y * 5) % 256                                   # every alpha value, 0 and 255 included
        return ((x * 11) % 256, (y * 17) % 256, (x + y) % 256, a)

    png, raw = png_bytes(120, 80, px, alpha=True)
    d = open_pdf(cp.content_to_pdf(ClipContent(images=[png])).data)
    base, mask = embedded_image(d)
    assert mask is not None and base.n == 3 and mask.n == 1
    assert bytes(base.samples) == bytes(b for i in range(0, len(raw), 4) for b in raw[i:i + 3])
    assert bytes(mask.samples) == raw[3::4]
    # also on an inserted page ("match" layout) the pixels stay the same
    pdf = doc_with()
    cp.insert_content(pdf, ClipContent(images=[png]), 1)
    base2, mask2 = embedded_image(pdf.doc, 1)
    assert bytes(base2.samples) == bytes(base.samples) and bytes(mask2.samples) == bytes(mask.samples)
    # and after saving / reopening
    again = open_pdf(pdf.to_bytes())
    b3, m3 = embedded_image(again, 1)
    assert bytes(b3.samples) == bytes(base.samples) and bytes(m3.samples) == bytes(mask.samples)


def test_opaque_rgba_png_gets_no_soft_mask():
    png, raw = png_bytes(30, 20, lambda x, y: (x * 8, y * 12, 99, 255), alpha=True)
    base, mask = embedded_image(open_pdf(cp.content_to_pdf(ClipContent(images=[png])).data))
    assert mask is None and bytes(base.samples) == bytes(b for i in range(0, len(raw), 4) for b in raw[i:i + 3])


def test_transparent_area_shows_the_white_page():
    png, _ = png_bytes(40, 20, lambda x, y: (255, 0, 0, 0 if x < 20 else 255), alpha=True)
    d = open_pdf(cp.content_to_pdf(ClipContent(images=[png])).data)
    pix = d[0].get_pixmap(matrix=pymupdf.Matrix(4 / 3, 4 / 3))
    assert pix.pixel(5, 10) == (255, 255, 255) and pix.pixel(35, 10) == (255, 0, 0)


def test_jpeg_and_other_formats_still_work():
    jpg = pymupdf.Pixmap(make_png(64, 32)).tobytes("jpg")
    d = open_pdf(cp.content_to_pdf(ClipContent(images=[jpg])).data)
    assert d[0].rect == pymupdf.Rect(0, 0, 48, 24)
    assert d.extract_image(d.get_page_images(0, full=True)[0][0])["ext"] == "jpeg"   # passed through


def test_broken_clipboard_image_is_skipped():
    r = cp.content_to_pdf(ClipContent(images=[b"\x89PNG\r\n\x1a\nkaput", make_png(20, 20)]))
    assert r.pages == 1 and r.skipped == ["Billede 1"]
    with pytest.raises(ValueError, match="Billede 1"):
        cp.content_to_pdf(ClipContent(images=[b"ikke et billede"]))


# -- text (U3, U15) ----------------------------------------------------------------------
DANISH = "Æblegrød – 25 € “citat” æøå ÆØÅ"


def test_danish_text_is_searchable_and_the_font_embedded():
    r = cp.content_to_pdf(ClipContent(text=DANISH + "\nNæste linje"))
    d = open_pdf(r.data)
    assert r.kind == "text"
    approx_rect(d[0].rect, (0, 0, 595.28, 841.89), 0.01)
    pg = d[0]
    for needle in ("Æblegrød", "25 €", "“citat”", "æøå", "ÆØÅ", "–"):
        assert pg.search_for(needle), needle
    assert DANISH in pg.get_text()
    fonts = pg.get_fonts()
    assert fonts and all(f[1] != "n/a" for f in fonts)              # embedded
    name, source = cp.body_font()
    print(f"\nbody font: {name!r} ({source}); in PDF: {[f[3] for f in fonts]}")
    if source == "system":
        assert all("+" in f[3] for f in fonts)                      # subset of the local TTF
        assert len(r.data) < 200_000


def test_text_keeps_paragraphs_blank_lines_and_indentation():
    text = "Første afsnit\n\n\n    Indrykket med fire\n\tTab-indrykket\nSidste linje"
    d = open_pdf(cp.text_to_pdf(text))
    lines = []
    for b in d[0].get_text("dict")["blocks"]:
        for ln in b["lines"]:
            lines.append((ln["bbox"][1], ln["bbox"][0], "".join(s["text"] for s in ln["spans"])))
    lines.sort()
    texts = [t for _y, _x, t in lines]
    assert [t.strip() for t in texts] == ["Første afsnit", "Indrykket med fire", "Tab-indrykket", "Sidste linje"]
    ys = [y for y, _x, _t in lines]
    step = ys[3] - ys[2]
    assert step == pytest.approx(11 * 1.35, abs=0.6)                 # 11 pt, line height 1.35
    assert ys[1] - ys[0] == pytest.approx(3 * step, abs=1.0)         # two empty lines kept
    margin = 20 * MM
    words = {w[4]: w for w in d[0].get_text("words")}
    assert words["Første"][0] == pytest.approx(margin, abs=0.5)
    assert words["Indrykket"][0] > margin + 10                      # leading spaces kept
    assert words["Tab-indrykket"][0] == pytest.approx(words["Indrykket"][0], abs=0.5)   # tab = 4 spaces


def test_long_lines_wrap_and_long_text_flows_onto_pages():
    para = "Dette er en lang linje med mange ord, der skal ombrydes pænt inden for margenen. " * 6
    text = "\n\n".join(f"{i}. {para}" for i in range(60))
    d = open_pdf(cp.text_to_pdf(text))
    assert d.page_count >= 3
    margin = 20 * MM
    for pg in d:
        for w in pg.get_text("words"):
            assert margin - 0.5 <= w[0] and w[2] <= pg.rect.width - margin + 0.5
            assert margin - 0.5 <= w[1] and w[3] <= pg.rect.height - margin + 0.5
    assert all_text(d).count("ombrydes") == 360
    assert d[d.page_count - 1].search_for("59.")


def test_text_style_size_font_colour_and_page():
    style = SpanStyle("Times New Roman", 14, (0.8, 0.0, 0.0), bold=True, serif=True)
    d = open_pdf(cp.text_to_pdf("Stor fed tekst", page_size=(400, 300), margin_mm=10, style=style))
    assert d[0].rect == pymupdf.Rect(0, 0, 400, 300)
    span = d[0].get_text("dict")["blocks"][0]["lines"][0]["spans"][0]
    assert span["size"] == pytest.approx(14)
    assert span["color"] == 0xCC0000
    assert span["flags"] & 16 or "Bold" in span["font"]
    if cp.body_font(style)[1] == "system":
        assert "serif" in span["font"].lower() or "times" in span["font"].lower() or "roman" in span["font"].lower()
    mono = open_pdf(cp.text_to_pdf("kode", style=SpanStyle("Courier", 10, mono=True)))
    f = mono[0].get_text("dict")["blocks"][0]["lines"][0]["spans"][0]["font"].lower()
    assert "mono" in f or "courier" in f or "consolas" in f


def test_odd_characters_do_not_break_the_layout():
    text = "a\x00b\x07c\r\nd\re\fslut <tag> & \"x\" 'y'\u2028ny linje"
    d = open_pdf(cp.text_to_pdf(text))
    t = d[0].get_text()
    assert "abc" in t and "<tag> & \"x\" 'y'" in t and "ny linje" in t


# -- HTML (U4, U16) ------------------------------------------------------------------------
CF_HTML_BODY = (
    "<html><head><link rel=\"stylesheet\" href=\"http://example.com/s.css\">"
    "<style>@import url(https://example.com/x.css); p{color:blue}</style>"
    "<script src=\"https://example.com/a.js\"></script></head><body>"
    "<!--StartFragment--><h1>Overskrift</h1>"
    "<p>Almindelig <b>fed</b> og <i>kursiv</i> og <u>understreget</u> og "
    "<span style=\"color:#ff0000;font-family:Calibri;background-image:url(http://example.com/b.png)\">rød</span></p>"
    "<ul><li>Punkt et</li><li>Punkt to</li></ul><ol><li>Nummer et</li></ol>"
    "<table><tr><th>Navn</th><th>Beløb</th></tr><tr><td>Æbler</td><td>25 €</td></tr></table>"
    "<p><img src=\"http://example.com/billede.png\"><img src=\"https://example.com/b.gif\" width=10>"
    "<a href=\"https://example.com\">et link</a></p><script>alert(1)</script>"
    "<!--EndFragment--></body></html>"
)


def cf_html(body: str) -> str:
    header = "Version:0.9\r\nStartHTML:{:010d}\r\nEndHTML:{:010d}\r\nStartFragment:{:010d}\r\nEndFragment:{:010d}\r\n"
    probe = header.format(0, 0, 0, 0)
    start = len(probe.encode())
    raw = body.encode()
    frag0 = start + raw.index(b"<!--StartFragment-->") + len("<!--StartFragment-->")
    frag1 = start + raw.index(b"<!--EndFragment-->")
    return header.format(start, start + len(raw), frag0, frag1) + body


def spans(doc):
    out = []
    for pg in doc:
        for b in pg.get_text("dict")["blocks"]:
            for ln in b.get("lines", ()):
                for s in ln["spans"]:
                    if s["text"].strip():
                        out.append(s)
    return out


def test_html_keeps_headings_formatting_lists_and_tables(no_network):
    d = open_pdf(cp.html_to_pdf(cf_html(CF_HTML_BODY)))
    ss = spans(d)
    by_text = {s["text"].strip(): s for s in ss}
    body = by_text["Almindelig"]
    head = by_text["Overskrift"]
    assert head["size"] > body["size"] * 1.4 and head["flags"] & 16
    assert by_text["fed"]["flags"] & 16 and not body["flags"] & 16
    assert by_text["kursiv"]["flags"] & 2
    assert by_text["rød"]["color"] == 0xFF0000
    text = all_text(d)
    for t in ("Punkt et", "Punkt to", "Nummer et", "Navn", "Beløb", "Æbler", "25 €", "et link", "understreget"):
        assert t in text, t
    assert "•" in text and "1." in text
    assert text.index("Navn") < text.index("Æbler")
    assert d[0].get_images() == []                                  # web images are never fetched
    assert "alert" not in text and "color:blue" not in text
    assert {s["font"].split("+")[-1] for s in ss} >= {by_text["Almindelig"]["font"].split("+")[-1]}
    assert all(f[1] != "n/a" for f in d[0].get_fonts())
    # the underline is drawn
    assert any(abs(dr["rect"].height) < 2 or dr["rect"].height < 2 for dr in d[0].get_drawings())


def test_sanitizer_drops_every_external_reference():
    safe, plain = cp.sanitize_html(cf_html(CF_HTML_BODY))
    low = safe.lower()
    for bad in ("http", "script", "<link", "@import", "url(", "<style", "href", "calibri", "<img"):
        assert bad not in low, bad
    assert "<h1>" in low and "<table>" in low and 'style="color:#ff0000"' in low
    assert "Overskrift" in plain and "Æbler" in plain and "alert" not in plain


def test_cf_html_variants():
    frag = cp.cf_html_fragment(cf_html(CF_HTML_BODY))
    assert frag.startswith("<h1>") and frag.endswith("</script>")
    # offsets only (no markers)
    body = "<html><body><p>Kun offsets</p></body></html>"
    head = "Version:1.0\r\nStartHTML:{:010d}\r\nEndHTML:{:010d}\r\n"
    n = len(head.format(0, 0))
    assert "Kun offsets" in cp.cf_html_fragment(head.format(n, n + len(body)) + body)
    # plain fragments / documents are kept
    assert cp.cf_html_fragment("<b>x</b>") == "<b>x</b>"
    # Excel puts the markers inside the table
    excel = cp.cf_html_fragment(cf_html("<table><!--StartFragment--><tr><td>A1</td></tr><!--EndFragment--></table>"))
    assert excel.startswith("<table>") and "A1" in excel


def test_html_images_from_data_uri_and_existing_local_files(tmp_path, no_network):
    import base64

    png = make_png(30, 20, (0, 200, 0))
    local = write(tmp_path, "clip_image001.png", make_png(16, 16, (200, 0, 0)))
    url = "file:///" + local.replace("\\", "/").lstrip("/").replace(" ", "%20")
    html = (f'<p>Data: <img src="data:image/png;base64,{base64.b64encode(png).decode()}"></p>'
            f'<p>Lokal: <img src="{url}"></p><p>Mangler: <img src="file:///C:/findes/ikke.png"></p>'
            '<p>Share: <img src="file://server/share/x.png"></p>')
    d = open_pdf(cp.html_to_pdf(html))
    assert len(d[0].get_images()) == 2
    assert cp.file_url_path("file:///C:/Users/x/AppData/Local/Temp/clip%20image.png") == \
        "C:/Users/x/AppData/Local/Temp/clip image.png"
    assert cp.file_url_path("file:///tmp/a.png") == "/tmp/a.png"
    assert cp.file_url_path("file://server/share/a.png") is None
    assert cp.file_url_path("https://example.com/a.png") is None


def test_odd_html_never_crashes(no_network):
    for markup in ("<table><tr><td>uden slut", "<<<>>>&&&", "</p></div></li>tekst", "<p style='width:-99999pt'>x</p>",
                   "<img>", "<div>" * 500 + "dybt" + "</div>" * 10, "Version:0.9\r\nStartHTML:-1\r\nlige tekst"):
        d = open_pdf(cp.html_to_pdf(markup))
        assert d.page_count >= 1
    assert "dybt" in all_text(open_pdf(cp.html_to_pdf("<div>" * 500 + "dybt")))


# -- files (U2) ------------------------------------------------------------------------------
def test_files_in_their_order_with_images_and_skipped_names(tmp_path):
    a = write(tmp_path, "a.pdf", small_pdf(2, "A", size=(300, 400)))
    img = write(tmp_path, "skærm.png", make_png(200, 100))
    b = write(tmp_path, "b.pdf", small_pdf(1, "B", size=(500, 500)))
    txt = write(tmp_path, "noter.txt", b"hej")
    broken = write(tmp_path, "i stykker.pdf", b"%PDF-1.7 kaput")
    r = cp.content_to_pdf(ClipContent(files=[a, txt, img, broken, b, str(tmp_path / "væk.pdf")]))
    d = open_pdf(r.data)
    assert r.kind == "files" and r.pages == d.page_count == 4
    assert [p.get_text().strip() for p in d] == ["A side 1", "A side 2", "", "B side 1"]
    assert d[0].rect == pymupdf.Rect(0, 0, 300, 400) and d[3].rect == pymupdf.Rect(0, 0, 500, 500)
    assert d[2].rect == pymupdf.Rect(0, 0, 150, 75)                  # image as the image
    assert sorted(r.skipped) == sorted(["noter.txt", "i stykker.pdf", "væk.pdf"])


@pytest.mark.parametrize("answer,pages,skipped", [("hemmelig", 3, []), ("forkert", 1, ["låst.pdf"]),
                                                  (None, 1, ["låst.pdf"])])
def test_password_callback(tmp_path, answer, pages, skipped):
    locked = write(tmp_path, "låst.pdf", small_pdf(2, "L", password="hemmelig"))
    other = write(tmp_path, "åben.pdf", small_pdf(1, "O"))
    asked = []

    def passwords(name):
        asked.append(name)
        return answer

    r = cp.content_to_pdf(ClipContent(files=[locked, other]), passwords=passwords)
    assert asked == ["låst.pdf"]
    assert r.pages == pages and r.skipped == skipped
    if answer == "hemmelig":
        d = open_pdf(r.data)
        assert not d.needs_pass and "L side 2" in d[1].get_text()


def test_locked_pdf_without_callback_is_skipped(tmp_path):
    locked = write(tmp_path, "låst.pdf", small_pdf(1, "L", password="x"))
    with pytest.raises(ValueError, match="låst.pdf"):
        cp.content_to_pdf(ClipContent(files=[locked]))


def test_multi_frame_tiff_gives_several_pages(tmp_path):
    Image = pytest.importorskip("PIL.Image")
    frames = [Image.new("RGB", (80, 40), c) for c in ("red", "green", "blue")]
    path = str(tmp_path / "scan.tif")
    frames[0].save(path, save_all=True, append_images=frames[1:])
    r = cp.content_to_pdf(ClipContent(files=[path]))
    assert r.pages == 3


def test_pdf_data_and_its_password():
    r = cp.content_to_pdf(ClipContent(pdf=small_pdf(2, "P")))
    assert r.kind == "pdf" and r.pages == 2
    locked = small_pdf(1, "P", password="pw")
    asked = []
    r = cp.content_to_pdf(ClipContent(pdf=locked), passwords=lambda n: asked.append(n) or "pw")
    assert asked == ["Udklipsholder"] and r.pages == 1
    with pytest.raises(ValueError, match="PDF"):
        cp.content_to_pdf(ClipContent(pdf=locked), passwords=lambda n: None)
    with pytest.raises(ValueError, match="PDF"):
        cp.content_to_pdf(ClipContent(pdf=b"%PDF-1.4 rod"))


# -- inserting pages (U10–U13) -----------------------------------------------------------------
@pytest.mark.parametrize("at", [0, 1, 3])
def test_insert_positions_and_one_undo_step(at):
    pdf = doc_with(pages=3)
    names = [pdf.doc[i].get_text().strip() for i in range(3)]
    undo_before = len(pdf.history._undo)
    res = cp.insert_content(pdf, ClipContent(images=[make_png(100, 60), make_png(60, 100)]), at)
    assert res.pages == [at, at + 1] and res.kind == "image"
    assert pdf.page_count == 5
    assert [pdf.doc[i].get_text().strip() for i in range(5) if i not in res.pages] == names
    assert all(pdf.doc[i].get_images(full=True) for i in res.pages)
    assert len(pdf.history._undo) == undo_before + 1 and pdf.history.undo_label == "Indsæt fra udklipsholder"
    pdf.undo()
    assert pdf.page_count == 3 and [pdf.doc[i].get_text().strip() for i in range(3)] == names


def test_insert_bad_position():
    with pytest.raises(ValueError):
        cp.insert_content(doc_with(pages=2), ClipContent(text="x"), 3)
    with pytest.raises(ValueError):
        cp.insert_content(doc_with(pages=2), ClipContent(text="x"), -1)


@pytest.mark.parametrize("width,height,rotation", [(A4W, A4H, 0), (A4H, A4W, 0), (A4W, A4H, 90)])
def test_match_uses_the_neighbour_paper(width, height, rotation):
    pdf = doc_with(width, height, rotation, pages=2)
    disp = pdf.doc[0].rect                                          # display size, /Rotate applied
    short, long_ = min(disp.width, disp.height), max(disp.width, disp.height)
    res = cp.insert_content(pdf, ClipContent(images=[make_png(1920, 1080), make_png(600, 900)]), 1)
    wide, tall = (pdf.doc[i] for i in res.pages)
    assert (wide.rect.width, wide.rect.height) == pytest.approx((long_, short), abs=0.01)    # landscape
    assert (tall.rect.width, tall.rect.height) == pytest.approx((short, long_), abs=0.01)    # portrait
    assert wide.rotation == tall.rotation == 0
    m = 10 * MM
    r = image_rect(pdf.doc, res.pages[0])
    assert r.width == pytest.approx(long_ - 2 * m, abs=0.5)          # whole image, fills the width
    assert (r.y0 + r.y1) / 2 == pytest.approx(short / 2, abs=0.5)
    # text gets the neighbour's display size
    res = cp.insert_content(pdf, ClipContent(text="Tekst på nabosidens papir"), 0)
    tp = pdf.doc[res.pages[0]].rect
    assert (tp.width, tp.height) == pytest.approx((disp.width, disp.height), abs=0.01)


def test_insert_image_as_the_image_and_on_a4():
    pdf = doc_with(400, 400)
    res = cp.insert_content(pdf, ClipContent(images=[make_png(800, 600)]), 1, ClipOptions(page_size="image"))
    assert pdf.doc[res.pages[0]].rect == pymupdf.Rect(0, 0, 600, 450)
    res = cp.insert_content(pdf, ClipContent(images=[make_png(800, 600)]), 0, ClipOptions(page_size="A4"))
    assert pdf.doc[res.pages[0]].rect == pymupdf.Rect(0, 0, A4H, A4W)


@pytest.mark.skipif(not SANS_TTF, reason="needs a local TTF")
def test_inserted_text_uses_the_documents_body_style():
    pdf = doc_with(font=SANS_TTF, pages=2)
    res = cp.insert_content(pdf, ClipContent(text=DANISH), 2, ClipOptions(page_size="match"))
    assert res.pages == [2] and res.kind == "text"
    pg = pdf.doc[2]
    assert pg.search_for("Æblegrød") and pg.search_for("25 €")
    span = spans(pymupdf.open("pdf", pdf.to_bytes()))[-1]
    assert span["size"] == pytest.approx(10.5, abs=0.01)
    assert span["color"] == 0x333333
    family = pymupdf.Font(fontfile=SANS_TTF).name.split()[0].lower()
    assert family in span["font"].lower().replace(" ", "")


def test_insert_files_keep_their_page_sizes_and_html(tmp_path):
    pdf = doc_with(pages=1)
    f = write(tmp_path, "x.pdf", small_pdf(2, "X", size=(300, 200)))
    res = cp.insert_content(pdf, ClipContent(files=[f]), 1)
    assert res.pages == [1, 2] and pdf.doc[1].rect == pymupdf.Rect(0, 0, 300, 200)
    res = cp.insert_content(pdf, ClipContent(html="<h2>Fra Word</h2><ul><li>punkt</li></ul>"), 0)
    assert res.kind == "html" and "Fra Word" in pdf.doc[0].get_text()
    assert pdf.page_count == 4


def test_insert_alternative_and_prefer_text():
    pdf = doc_with(pages=1)
    c = ClipContent(images=[make_png(200, 100)], text="A1\tB1\nA2\tB2")
    res = cp.insert_content(pdf, c, 1)
    assert res.kind == "image" and res.alternative == "text"
    pdf.undo()
    res = cp.insert_content(pdf, c, 1, ClipOptions(page_size="match", prefer="text"))
    assert res.kind == "text" and pdf.doc[1].search_for("B2")


# -- Folio's object clip (U5) ----------------------------------------------------------------
def folio_scene() -> PdfDocument:
    doc = pymupdf.open()
    pg = doc.new_page(width=500, height=700)
    pg.draw_rect(pymupdf.Rect(80, 100, 300, 200), color=(0, 0, 1), fill=(1, 0.9, 0.8), width=2)
    pg.insert_text((95, 140), "Faktaboks", fontname="helv", fontsize=12)
    pg.insert_text((72, 600), "Brødtekst forneden", fontname="helv", fontsize=11)
    return PdfDocument.from_bytes(doc.tobytes())


def test_folio_clip_becomes_a_page_of_the_source_size_with_objects_in_place():
    src = folio_scene()
    objs = po.page_objects(src.doc, 0)
    sel = [o for o in objs if o.kind == "graphic" or (o.kind == "text" and "Faktaboks" in o.ref.text)]
    assert len(sel) == 2
    clip = po.copy_objects(src.doc, 0, sel)
    r = cp.content_to_pdf(ClipContent(folio=clip, images=[make_png()], text="x"))
    assert r.kind == "folio" and r.pages == 1 and r.alternative is None
    d = open_pdf(r.data)
    assert (d[0].rect.width, d[0].rect.height) == pytest.approx(clip.page_size)
    new = po.page_objects(d, 0)
    assert sorted(o.kind for o in new) == ["graphic", "text"]
    for o in sel:
        twin = next(n for n in new if n.kind == o.kind)
        approx_rect(twin.rect, o.rect, 1.0)
    assert "Brødtekst" not in d[0].get_text()
    # inserted into a document: a page of clip.page_size
    pdf = doc_with(pages=1)
    res = cp.insert_content(pdf, ClipContent(folio=clip), 1)
    assert res.kind == "folio" and pdf.doc[1].rect == pymupdf.Rect(0, 0, 500, 700)


# -- U16: nothing is fetched -------------------------------------------------------------------
def test_everything_works_with_the_network_blocked(no_network, tmp_path):
    from pdfstudio.security import netguard

    assert netguard._installed                                      # conftest: like the application
    for c in (ClipContent(html=CF_HTML_BODY), ClipContent(text=DANISH), ClipContent(images=[make_png()])):
        assert cp.content_to_pdf(c).pages >= 1


# -- U17: speed ----------------------------------------------------------------------------
def screenshot_png(width=3840, height=2160) -> bytes:
    """A 4K screenshot-like RGBA PNG (opaque, as Qt writes clipboard images):
    UI bars, text-like stripes and a noisy photo area."""
    rnd = random.Random(7)
    rows = []
    for k in range(48):
        row = bytearray(b"\xf3\xf3\xf3\xff" * width)
        for _ in range(30):
            x0 = rnd.randrange(0, width - 200)
            seg = rnd.randrange(20, 200)
            row[x0 * 4:(x0 + seg) * 4] = bytes((rnd.randrange(256), 60, 120, 255)) * seg
        if k % 3 == 0:                                              # photo / video area: noise
            noise = bytes(rnd.getrandbits(8) for _ in range(800 * 3))
            px = bytearray(800 * 4)
            px[0::4], px[1::4], px[2::4], px[3::4] = noise[0::3], noise[1::3], noise[2::3], b"\xff" * 800
            row[1200 * 4:2000 * 4] = px
        rows.append(b"\x00" + bytes(row))
    raw = b"".join(rows[(y * 7 + y // 5) % len(rows)] for y in range(height))

    def chunk(tag, data):
        return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)

    ihdr = struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0)
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr) + chunk(b"IDAT", zlib.compress(raw, 6)) + chunk(b"IEND", b"")


def test_speed_4k_screenshot_and_50_pages_of_text():
    png = screenshot_png()
    cp.content_to_pdf(ClipContent(text="opvarmning"))               # font index + first Story
    t = time.perf_counter()
    r = cp.content_to_pdf(ClipContent(images=[png]))
    t_new = time.perf_counter() - t
    assert r.pages == 1 and open_pdf(r.data)[0].rect == pymupdf.Rect(0, 0, 2880, 1620)
    pdf = doc_with(pages=2)
    t = time.perf_counter()
    cp.insert_content(pdf, ClipContent(images=[png]), 1)
    t_insert = time.perf_counter() - t

    line = "Lorem ipsum dolor sit amet, æøå ÆØÅ – 25 € “citat”, consectetur adipiscing elit sed do eiusmod."
    text = "\n".join(line if i % 8 else "" for i in range(50 * 52))
    t = time.perf_counter()
    r = cp.content_to_pdf(ClipContent(text=text))
    t_text = time.perf_counter() - t
    pages = r.pages
    print(f"\n4K PNG ({len(png) // 1024} KB) -> new PDF {t_new * 1000:.0f} ms, inserted {t_insert * 1000:.0f} ms; "
          f"text {pages} pages {t_text * 1000:.0f} ms")
    assert pages >= 50
    factor = 2.0 if os.environ.get("CI") else 1.0
    assert t_new < 1.0 * factor and t_insert < 1.0 * factor
    assert t_text < 2.0 * factor
