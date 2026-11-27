"""Multi-page test documents for the text flow (PyMuPDF's own typesetting,
so the tests need nothing but PyMuPDF): a report with running header,
"Side N af M" footer, headings, justified paragraphs, bullet lists and
optionally chapters on new pages, a figure with caption, two columns."""

from __future__ import annotations

import io
import random

import pymupdf

WORDS = ("omsætning resultat bestyrelsen udviklingen efterspørgsel grønne løsninger væksten medarbejdere kunder "
         "markedet investeringer regnskabsår strategi bæredygtighed afdelingen produktionen leverandører økonomi "
         "forventer betydelig samlede særligt selskabet kvartal projekter digitalisering effektivisering").split()

CSS = ("* {font-family: sans-serif; font-size: 11pt;} p {text-align: justify; margin: 0 0 6pt 0;}"
       " h1 {font-size: 18pt; margin: 12pt 0 6pt 0;} h2 {font-size: 13pt; margin: 10pt 0 4pt 0;}"
       " li {margin: 0 0 3pt 0;} p.caption {text-align: center; font-size: 9pt; margin: 2pt 0 8pt 0;}")

HEADER = "Firma A/S · Rapport 2026"


def sentences(r: random.Random, n: int) -> str:
    out = []
    for _ in range(n):
        ws = [r.choice(WORDS) for _ in range(r.randint(8, 16))]
        out.append(" ".join(ws).capitalize() + ".")
    return " ".join(out)


def chart_png(w: int = 300, h: int = 140) -> bytes:
    doc = pymupdf.open()
    p = doc.new_page(width=w, height=h)
    p.draw_rect(p.rect, color=None, fill=(0.95, 0.96, 0.99))
    for i in range(6):
        p.draw_rect(pymupdf.Rect(20 + i * 45, h - 20 - 15 * (i + 1), 50 + i * 45, h - 20), color=None,
                    fill=(0.2, 0.45, 0.75))
    return p.get_pixmap(matrix=pymupdf.Matrix(1.5, 1.5)).tobytes("png")


def chapter_html(r: random.Random, c: int, figure: bool) -> str:
    html = [f"<h1>{c + 1}. Kapitel om {r.choice(WORDS)}</h1>"]
    for _ in range(3):
        html.append(f"<p>{sentences(r, 5)}</p>")
    if figure and c == 0:
        html.append('<p style="text-align:center"><img src="chart.png" width="300" height="140"/></p>')
        html.append(f'<p class="caption">Figur {c + 1}: Udviklingen i {r.choice(WORDS)}</p>')
    html.append("<ul>" + "".join(f"<li>{sentences(r, 1)}</li>" for _ in range(3)) + "</ul>")
    html.append(f"<h2>{c + 1}.1 Afsnit om {r.choice(WORDS)}</h2>")
    for _ in range(4):
        html.append(f"<p>{sentences(r, 6)}</p>")
    return "".join(html)


def report(chapters: int = 2, seed: int = 3, *, chapter_breaks: bool = False, figure: bool = False,
           columns: int = 1, rotate: int = 0) -> pymupdf.Document:
    r = random.Random(seed)
    parts = [chapter_html(r, c, figure) for c in range(chapters)]
    if not chapter_breaks:
        parts = ["".join(parts)]
    archive = pymupdf.Archive()
    if figure:
        archive.add(chart_png(), "chart.png")
    buf = io.BytesIO()
    writer = pymupdf.DocumentWriter(buf)
    mediabox = pymupdf.paper_rect("a4")
    if columns == 1:
        frames = [pymupdf.Rect(72, 80, 523, 770)]
    else:
        frames = [pymupdf.Rect(72, 80, 290, 770), pymupdf.Rect(305, 80, 523, 770)]
    for html in parts:
        css = CSS if columns == 1 else CSS + " p {text-align: left;}"   # narrow justified columns get very loose
        story = pymupdf.Story(html=html, user_css=css, archive=archive)
        more = True
        while more:
            dev = writer.begin_page(mediabox)
            for where in frames:
                if not more:
                    break
                more, _filled = story.place(where)
                story.draw(dev)
            writer.end_page()
    writer.close()
    doc = pymupdf.open("pdf", buf.getvalue())
    n = doc.page_count
    for i, page in enumerate(doc):
        page.insert_text((400, 50), HEADER, fontname="helv", fontsize=8, color=(0.4, 0.4, 0.4))
        t = f"Side {i + 1} af {n}"
        page.insert_text(((595 - pymupdf.get_text_length(t, "helv", 8)) / 2, 805), t, fontname="helv", fontsize=8)
        if rotate:
            page.set_rotation(rotate)
    return pymupdf.open("pdf", doc.tobytes())
