"""Real-world test corpus for Folio PDF (manual tool, not collected by pytest).

Writes LibreOffice flat-XML sources and converts them to PDF with a headless
LibreOffice (``soffice``), so the PDFs look like documents made by a real
office suite: embedded subset fonts, text broken into lines by a real layout
engine, tables, images and - above all - vector graphics (filled boxes, frame
borders, header rules, ellipses, lines, arrows with marker heads).

Documents
---------
``aarsrapport.pdf``  ~55-page Danish annual report, A4 portrait (Writer, .fodt)
``strategi.pdf``     14-page landscape strategy deck (Impress, .fodp)

Everything is deterministic (fixed seed, fixed Danish vocabulary); the only
dependencies are the standard library, PyMuPDF (to draw the PNG images that are
base64-embedded in the flat XML) and LibreOffice itself.  No network access.

Usage::

    python tests/corpus/make_corpus.py [OUTPUT_DIR] [--keep-sources] [--preview DIR]

After converting, every PDF is opened with PyMuPDF, a summary is printed and a
few sanity assertions are checked (page counts, drawings, images, header and
footer text).
"""

from __future__ import annotations

import argparse
import base64
import random
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from xml.sax.saxutils import escape

import pymupdf

DEFAULT_OUT = (
    "/tmp/claude-0/-home-user-PDF/740062b8-3b33-5ee1-bf87-7d6301ab4318/scratchpad/corpus/"
)
SEED = 20261120
COMPANY = "Novo Grøn A/S"
HEADER_TEXT = f"{COMPANY} · Årsrapport 2026"

# Palette (hex for ODF, floats for the PyMuPDF-drawn images)
GREEN = "#1F5C4A"
GREEN_2 = "#2E7D5B"
GREEN_LIGHT = "#E8F3EE"
ZEBRA = "#F1F6F3"
BORDER = "#8FB3A2"
ACCENT = "#C8553D"
AMBER = "#E9B44C"
SLATE = "#34495E"
SKY = "#3D7EAA"
SKY_LIGHT = "#E3EEF6"


def esc(s: str) -> str:
    return escape(s, {'"': "&quot;"})


def _rgb(h: str) -> tuple[float, float, float]:
    h = h.lstrip("#")
    return tuple(int(h[i:i + 2], 16) / 255 for i in (0, 2, 4))  # type: ignore[return-value]


# --------------------------------------------------------------------------- images

def png_logo() -> bytes:
    """Small round leaf logo, 160x160 px."""
    doc = pymupdf.open()
    page = doc.new_page(width=80, height=80)
    page.draw_circle((40, 40), 38, color=None, fill=_rgb(GREEN))
    leaf = page.new_shape()
    leaf.draw_bezier((22, 58), (20, 25), (45, 16), (60, 20))
    leaf.draw_bezier((60, 20), (62, 45), (45, 62), (22, 58))
    leaf.finish(color=None, fill=(0.85, 0.95, 0.88), closePath=True)
    leaf.draw_line((24, 56), (52, 28))
    leaf.finish(color=_rgb(GREEN), width=2)
    leaf.commit()
    png = page.get_pixmap(dpi=144, alpha=True).tobytes("png")
    doc.close()
    return png


def png_bar_chart(title: str, labels: list[str], series: list[list[float]],
                  legend: list[str]) -> bytes:
    """Grouped bar chart as PNG (~960x540 px)."""
    W, H = 480, 270
    doc = pymupdf.open()
    page = doc.new_page(width=W, height=H)
    page.draw_rect(page.rect, color=None, fill=(1, 1, 1))
    page.insert_text((20, 24), title, fontname="hebo", fontsize=12, color=_rgb(SLATE))
    left, right, top, bottom = 50, W - 20, 45, H - 40
    vmax = max(max(s) for s in series) * 1.15
    steps = 5
    for i in range(steps + 1):
        y = bottom - (bottom - top) * i / steps
        page.draw_line((left, y), (right, y), color=(0.85, 0.87, 0.88), width=0.5)
        page.insert_text((14, y + 3), f"{vmax * i / steps:,.0f}".replace(",", "."),
                         fontname="helv", fontsize=7, color=(0.4, 0.4, 0.4))
    colors = [_rgb(GREEN), _rgb(SKY), _rgb(AMBER)]
    group_w = (right - left) / len(labels)
    bar_w = group_w * 0.7 / len(series)
    for g, lab in enumerate(labels):
        x0 = left + g * group_w + group_w * 0.15
        for s, vals in enumerate(series):
            h = (bottom - top) * vals[g] / vmax
            r = pymupdf.Rect(x0 + s * bar_w, bottom - h, x0 + (s + 1) * bar_w - 2, bottom)
            page.draw_rect(r, color=None, fill=colors[s % 3])
        page.insert_text((x0, bottom + 14), lab, fontname="helv", fontsize=8,
                         color=(0.25, 0.25, 0.25))
    page.draw_line((left, bottom), (right, bottom), color=(0.3, 0.3, 0.3), width=0.8)
    lx = right - 70 * len(legend)
    for s, name in enumerate(legend):
        page.draw_rect(pymupdf.Rect(lx, 16, lx + 9, 25), color=None, fill=colors[s % 3])
        page.insert_text((lx + 13, 24), name, fontname="helv", fontsize=8)
        lx += 70
    png = page.get_pixmap(dpi=144).tobytes("png")
    doc.close()
    return png


def png_photo(seed: int) -> bytes:
    """Abstract 'landscape photo' (gradient sky, hills, sun) ~ 800x500 px."""
    rnd = random.Random(seed)
    W, H = 400, 250
    doc = pymupdf.open()
    page = doc.new_page(width=W, height=H)
    for i in range(25):
        t = i / 24
        page.draw_rect(pymupdf.Rect(0, i * H / 25, W, (i + 1) * H / 25 + 1), color=None,
                       fill=(0.55 + 0.35 * t, 0.75 + 0.15 * t, 0.95))
    page.draw_circle((rnd.uniform(250, 340), 70), 28, color=None, fill=(1, 0.86, 0.45))
    for k, col in enumerate([(0.42, 0.66, 0.45), (0.27, 0.52, 0.36), (0.16, 0.38, 0.28)]):
        sh = page.new_shape()
        base = 150 + k * 30
        pts = [(0, H), (0, base)]
        for x in range(0, W + 1, 40):
            pts.append((x, base - rnd.uniform(0, 45)))
        pts += [(W, H)]
        sh.draw_polyline(pts)
        sh.finish(color=None, fill=col, closePath=True)
        sh.commit()
    for x in (60, 120, 300):  # wind turbines
        page.draw_line((x, 200), (x, 120), color=(1, 1, 1), width=2)
        for a in ((x, 120, x - 4, 92), (x, 120, x + 24, 132), (x, 120, x - 20, 136)):
            page.draw_line(a[:2], a[2:], color=(1, 1, 1), width=1.5)
    png = page.get_pixmap(dpi=144).tobytes("png")
    doc.close()
    return png


def b64(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii")


# --------------------------------------------------------------------------- Danish text

SUBJ = ["Koncernen", "Selskabet", "Ledelsen", "Bestyrelsen", "Novo Grøn",
        "Forretningsområdet Energi", "Vores medarbejdere", "Den nordiske organisation",
        "Produktionen i Esbjerg", "Salgsafdelingen", "Serviceforretningen",
        "Afdelingen for forskning og udvikling", "Indkøbsfunktionen", "Datterselskabet i Sverige"]
VERB = ["har styrket", "forbedrede", "øgede", "fastholdt", "investerede i", "udviklede",
        "reducerede", "fortsatte arbejdet med", "lancerede", "evaluerede", "prioriterede",
        "konsoliderede", "moderniserede", "fremskyndede"]
OBJ = ["den samlede omsætning", "driftsmarginen", "indsatsen for bæredygtighed",
       "den digitale platform", "samarbejdet med leverandørerne",
       "energieffektiviteten i produktionen", "kundetilfredsheden", "de langsigtede mål",
       "pengestrømmene fra driften", "porteføljen af vedvarende energi",
       "kompetenceudviklingen", "risikostyringen", "arbejdsmiljøet på fabrikkerne",
       "genanvendelsen af materialer", "leveringssikkerheden"]
ADV = ["i løbet af året", "i overensstemmelse med strategien", "trods et udfordrende marked",
       "på tværs af alle regioner", "med solide resultater", "som planlagt", "i andet halvår",
       "sammenlignet med sidste år", "inden for de fastsatte rammer", "med fokus på lønsomhed",
       "i tæt dialog med kunderne", "i første kvartal"]
CLAUSE = ["hvilket bidrog positivt til resultatet", "og udviklingen forventes at fortsætte i 2027",
          "samtidig med at omkostningerne blev holdt i ro", "så vi står stærkt i de kommende år",
          "og det understøtter vores ambition om CO2-neutralitet i 2030",
          "selv om råvarepriserne steg markant", "hvor især de nordiske markeder bidrog",
          "og effekten ses allerede i nøgletallene"]
SHORT = ["Det er vi stolte af.", "Udviklingen følges tæt.", "Målet er nået.",
         "Arbejdet fortsætter i 2027.", "Resultatet lever op til forventningerne."]


class Text:
    def __init__(self, seed: int):
        self.r = random.Random(seed)

    def sentence(self) -> str:
        r = self.r
        if r.random() < 0.12:
            return r.choice(SHORT)
        s = f"{r.choice(SUBJ)} {r.choice(VERB)} {r.choice(OBJ)} {r.choice(ADV)}"
        if r.random() < 0.55:
            s += ", " + r.choice(CLAUSE)
        return s + "."

    def para(self, lo=4, hi=7) -> str:
        return " ".join(self.sentence() for _ in range(self.r.randint(lo, hi)))

    def item(self) -> str:
        r = self.r
        return f"{r.choice(VERB).capitalize()} {r.choice(OBJ)} {r.choice(ADV)}"


# --------------------------------------------------------------------------- ODF common

NS = (
    'xmlns:office="urn:oasis:names:tc:opendocument:xmlns:office:1.0" '
    'xmlns:style="urn:oasis:names:tc:opendocument:xmlns:style:1.0" '
    'xmlns:text="urn:oasis:names:tc:opendocument:xmlns:text:1.0" '
    'xmlns:table="urn:oasis:names:tc:opendocument:xmlns:table:1.0" '
    'xmlns:draw="urn:oasis:names:tc:opendocument:xmlns:drawing:1.0" '
    'xmlns:fo="urn:oasis:names:tc:opendocument:xmlns:xsl-fo-compatible:1.0" '
    'xmlns:xlink="http://www.w3.org/1999/xlink" '
    'xmlns:dc="http://purl.org/dc/elements/1.1/" '
    'xmlns:meta="urn:oasis:names:tc:opendocument:xmlns:meta:1.0" '
    'xmlns:number="urn:oasis:names:tc:opendocument:xmlns:datastyle:1.0" '
    'xmlns:svg="urn:oasis:names:tc:opendocument:xmlns:svg-compatible:1.0" '
    'xmlns:presentation="urn:oasis:names:tc:opendocument:xmlns:presentation:1.0" '
    'xmlns:loext="urn:org:documentfoundation:names:experimental:office:xmlns:loext:1.0"'
)

FONTS = """<office:font-face-decls>
  <style:font-face style:name="Liberation Serif" svg:font-family="'Liberation Serif'" style:font-family-generic="roman" style:font-pitch="variable"/>
  <style:font-face style:name="Liberation Sans" svg:font-family="'Liberation Sans'" style:font-family-generic="swiss" style:font-pitch="variable"/>
  <style:font-face style:name="Carlito" svg:font-family="Carlito" style:font-family-generic="swiss" style:font-pitch="variable"/>
 </office:font-face-decls>"""

MARKERS = """<draw:marker draw:name="Arrow" svg:viewBox="0 0 20 30" svg:d="M10 0l-10 30h20z"/>"""


def meta(title: str) -> str:
    return (f"<office:meta><dc:title>{esc(title)}</dc:title>"
            f"<meta:initial-creator>{esc(COMPANY)}</meta:initial-creator>"
            "<dc:language>da-DK</dc:language></office:meta>")


def gstyle(name: str, parent: str = "", **props) -> str:
    """Automatic graphic style; keyword names use '__' for ':' and '_' for '-'."""
    attrs = " ".join(f'{k.replace("__", ":").replace("_", "-")}="{v}"' for k, v in props.items())
    par = f' style:parent-style-name="{parent}"' if parent else ""
    return (f'<style:style style:name="{name}" style:family="graphic"{par}>'
            f"<style:graphic-properties {attrs}/></style:style>")


# --------------------------------------------------------------------------- Writer report

class Report:
    """Builds the annual report as a flat ODT (.fodt)."""

    def __init__(self, seed: int = SEED):
        self.t = Text(seed)
        self.r = self.t.r
        self.body: list[str] = []
        self.auto: list[str] = []
        self.n = 0  # unique object counter
        self.chapters: list[str] = []
        self.figures = 0
        self.tables = 0
        self.boxes = 0
        self.logo = b64(png_logo())

    def uid(self, prefix: str) -> str:
        self.n += 1
        return f"{prefix}{self.n}"

    # ----- inline text
    def rich(self, text: str) -> str:
        """Escape text and sprinkle bold / italic / coloured words."""
        words = text.split(" ")
        out: list[str] = []
        i = 0
        while i < len(words):
            x = self.r.random()
            if x < 0.035 and i + 2 < len(words):
                n = self.r.randint(1, 3)
                style = self.r.choice(["Tb", "Ti", "Tc", "Tb", "Tbc"])
                out.append(f'<text:span text:style-name="{style}">'
                           f'{esc(" ".join(words[i:i + n]))}</text:span>')
                i += n
            else:
                out.append(esc(words[i]))
                i += 1
        return " ".join(out)

    def p(self, inner: str, style: str = "Text_20_body") -> None:
        self.body.append(f'<text:p text:style-name="{style}">{inner}</text:p>')

    def para(self, lo=4, hi=7, prefix: str = "") -> None:
        self.p(prefix + self.rich(self.t.para(lo, hi)))

    def h1(self, title: str, style: str = "Heading_20_1", prefix: str = "") -> None:
        self.chapters.append(title)
        k = len(self.chapters)
        self.body.append(
            f'<text:h text:style-name="{style}" text:outline-level="1">{prefix}'
            f'<text:bookmark text:name="kap{k}"/>{k}  {esc(title)}</text:h>')

    def h2(self, title: str) -> None:
        self.body.append(
            f'<text:h text:style-name="Heading_20_2" text:outline-level="2">{esc(title)}</text:h>')

    def bullets(self, items: list[str], numbered: bool = False) -> None:
        st = "Numbering_20_123" if numbered else "List_20_Bullet"
        lis = "".join(f'<text:list-item><text:p text:style-name="List_20_Para">{self.rich(i)}'
                      f"</text:p></text:list-item>" for i in items)
        self.body.append(f'<text:list text:style-name="{st}">{lis}</text:list>')

    # ----- tables
    def table(self, caption: str, head: list[str], rows: list[list[str]],
              widths: list[float]) -> None:
        self.tables += 1
        name = f"Tabel{self.tables}"
        self.p(esc(f"Tabel {self.tables}: {caption}"), "Table_20_Caption")
        cols = "".join(f'<table:table-column table:style-name="TC{int(w * 100)}"/>' for w in widths)
        for w in set(widths):
            st = (f'<style:style style:name="TC{int(w * 100)}" style:family="table-column">'
                  f'<style:table-column-properties style:column-width="{w}cm"/></style:style>')
            if st not in self.auto:
                self.auto.append(st)
        hdr = "".join(f'<table:table-cell table:style-name="CellHead" office:value-type="string">'
                      f'<text:p text:style-name="{"TableHeadNum" if j else "TableHead"}">{esc(c)}'
                      f"</text:p></table:table-cell>" for j, c in enumerate(head))
        body = []
        for i, row in enumerate(rows):
            total = row[0].lower().startswith(("i alt", "total", "resultat"))
            cs = "CellTotal" if total else ("CellZebra" if i % 2 else "CellPlain")
            cells = "".join(
                f'<table:table-cell table:style-name="{cs}" office:value-type="string">'
                f'<text:p text:style-name="{("TableNumB" if total else "TableNum") if j else ("TableTextB" if total else "TableText")}">'
                f"{esc(c)}</text:p></table:table-cell>" for j, c in enumerate(row))
            body.append(f"<table:table-row>{cells}</table:table-row>")
        self.body.append(
            f'<table:table table:name="{name}" table:style-name="Tab">{cols}'
            f"<table:table-header-rows><table:table-row>{hdr}</table:table-row>"
            f'</table:table-header-rows>{"".join(body)}</table:table>')
        self.p("", "Text_20_body")

    def num(self, v: float, dec: int = 0) -> str:
        s = f"{v:,.{dec}f}"
        return s.replace(",", "X").replace(".", ",").replace("X", ".")

    def fin_rows(self, labels: list[str], base: float, total: str | None = None,
                 years: int = 2) -> list[list[str]]:
        rows = []
        sums = [0.0] * years
        for lab in labels:
            v = base * self.r.uniform(0.15, 1.0)
            vals = [v * (1 + self.r.uniform(-0.12, 0.18)) ** k for k in range(years)][::-1]
            sums = [a + b for a, b in zip(sums, vals)]
            rows.append([lab] + [self.num(x, 1) for x in vals])
        if total:
            rows.append([total] + [self.num(x, 1) for x in sums])
        return rows

    # ----- images
    def figure(self, png: bytes, caption: str, w: float = 15.0, h: float = 8.44) -> None:
        self.figures += 1
        name = self.uid("Figur")
        self.p(f'<draw:frame draw:style-name="frImg" draw:name="{name}" text:anchor-type="as-char" '
               f'svg:width="{w}cm" svg:height="{h}cm" draw:z-index="0">'
               f"<draw:image><office:binary-data>{b64(png)}</office:binary-data></draw:image>"
               f"</draw:frame>", "Figure")
        self.p(esc(f"Figur {self.figures}: {caption}"), "Caption")

    # ----- callout boxes (text frames with fill and border)
    def callout(self, heading: str, lines: list[str], side: str = "full", tone: str = "green") -> None:
        """A fact box anchored at the start of a new body paragraph."""
        self.boxes += 1
        style = {"green": "frBox", "sky": "frBoxSky", "amber": "frBoxAmber"}[tone]
        if side != "full":
            style += "R"
        w = "17cm" if side == "full" else "6.4cm"
        inner = (f'<text:p text:style-name="BoxHeading">{esc(heading)}</text:p>'
                 + "".join(f'<text:p text:style-name="BoxText">{esc(x)}</text:p>' for x in lines))
        frame = (f'<draw:frame draw:style-name="{style}" draw:name="{self.uid("Faktaboks")}" '
                 f'text:anchor-type="paragraph" svg:width="{w}" draw:z-index="3">'
                 f'<draw:text-box fo:min-height="1.5cm">{inner}</draw:text-box></draw:frame>')
        if side == "full":
            # own (empty) anchor paragraph: a wrap-none frame on top of a long paragraph that
            # splits over a page break can send LibreOffice's layout into an endless loop
            self.p(frame, "BoxAnchor")
            self.para(4, 6)
        else:
            self.para(5, 8, prefix=frame)

    # ----- drawn shapes
    def diagram_para(self, height_cm: float, shapes: list[str]) -> None:
        """An empty paragraph of fixed height that holds paragraph-anchored shapes."""
        st = f"PDiag{int(height_cm * 10)}"
        decl = (f'<style:style style:name="{st}" style:family="paragraph" style:parent-style-name="Diagram">'
                f'<style:paragraph-properties fo:line-height="{height_cm}cm"/></style:style>')
        if decl not in self.auto:
            self.auto.append(decl)
        self.p("".join(shapes), st)

    def rect(self, x, y, w, h, style="grBox", text="", tstyle="ShapeText", kind="rect") -> str:
        tag = "draw:rect" if kind == "rect" else "draw:ellipse"
        inner = (f'<text:p text:style-name="{tstyle}"><text:span text:style-name="T{tstyle}">'
                 f"{esc(text)}</text:span></text:p>" if text else "")
        return (f'<{tag} draw:style-name="{style}" draw:name="{self.uid("Figur")}" '
                f'text:anchor-type="paragraph" draw:z-index="2" svg:x="{x}cm" svg:y="{y}cm" '
                f'svg:width="{w}cm" svg:height="{h}cm">{inner}</{tag}>')

    def line(self, x1, y1, x2, y2, style="grArrow") -> str:
        return (f'<draw:line draw:style-name="{style}" draw:name="{self.uid("Linje")}" '
                f'text:anchor-type="paragraph" draw:z-index="2" svg:x1="{x1}cm" svg:y1="{y1}cm" '
                f'svg:x2="{x2}cm" svg:y2="{y2}cm"><text:p/></draw:line>')

    # ----- document parts
    def cover(self) -> None:
        bg = (f'<draw:rect draw:style-name="grCoverBg" draw:name="Forsidebaggrund" '
              f'text:anchor-type="paragraph" draw:z-index="0" svg:x="0cm" svg:y="0cm" '
              f'svg:width="21cm" svg:height="29.7cm"><text:p/></draw:rect>')
        circ = (f'<draw:ellipse draw:style-name="grCoverCircle" draw:name="Forsidecirkel" '
                f'text:anchor-type="paragraph" draw:z-index="1" svg:x="11cm" svg:y="17.5cm" '
                f'svg:width="14cm" svg:height="14cm"><text:p/></draw:ellipse>')
        circ2 = (f'<draw:ellipse draw:style-name="grCoverCircle2" draw:name="Forsidecirkel2" '
                 f'text:anchor-type="paragraph" draw:z-index="1" svg:x="15.5cm" svg:y="-3cm" '
                 f'svg:width="8cm" svg:height="8cm"><text:p/></draw:ellipse>')
        rule = (f'<draw:line draw:style-name="grCoverRule" draw:name="Forsidelinje" '
                f'text:anchor-type="paragraph" draw:z-index="1" svg:x1="2cm" svg:y1="16.2cm" '
                f'svg:x2="9cm" svg:y2="16.2cm"><text:p/></draw:line>')
        logo = (f'<draw:frame draw:style-name="frLogo" draw:name="ForsideLogo" text:anchor-type="as-char" '
                f'svg:width="2.2cm" svg:height="2.2cm" draw:z-index="2"><draw:image>'
                f"<office:binary-data>{self.logo}</office:binary-data></draw:image></draw:frame>")
        self.p(bg + circ + circ2 + rule + logo, "PCoverFirst")
        self.p(esc("Årsrapport 2026"), "CoverTitle")
        self.p(esc("Grøn vækst med ansvar"), "CoverSub")
        self.p(esc(COMPANY), "CoverCompany")
        self.p(esc("CVR-nr. 12 34 56 78 · Industrivej 12 · 6700 Esbjerg"), "CoverSmall")

    def toc(self, titles: list[str]) -> None:
        self.body.append('<text:p text:style-name="PTocTitle">Indhold</text:p>')
        for k, t in enumerate(titles, 1):
            self.p(f"{k}  {esc(t)}<text:tab/><text:bookmark-ref text:reference-format=\"page\" "
                   f'text:ref-name="kap{k}">{k + 2}</text:bookmark-ref>', "TocEntry")
        self.p("", "Text_20_body")
        self.callout("Om denne rapport",
                     ["Årsrapporten dækker perioden 1. januar – 31. december 2026.",
                      "Alle beløb er i mio. kr., medmindre andet er angivet.",
                      "Rapporten er udarbejdet efter IFRS som godkendt af EU."], tone="sky")

    def chapter_sections(self, n_sections: int, paras: tuple[int, int] = (4, 6)) -> None:
        for i in range(n_sections):
            self.h2(self.r.choice(SECTION_TITLES))
            for j in range(self.r.randint(*paras)):
                if j == 1 and i % 3 == 1:  # fact box between the paragraphs
                    self.callout(self.r.choice(FACT_TITLES),
                                 [self.t.item() + "." for _ in range(2)], side="full",
                                 tone=self.r.choice(["green", "sky", "amber"]))
                else:
                    self.para()
            if i % 4 == 3:  # mini diagram: two labelled boxes, arrow, target ellipse
                a, b = self.r.sample(STEP_WORDS, 2)
                self.diagram_para(2.0, [
                    self.rect(0.5, 0.2, 4.5, 1.4, "grBox", a), self.line(5.1, 0.9, 6.9, 0.9),
                    self.rect(7.0, 0.2, 4.5, 1.4, "grBox2", b), self.line(11.6, 0.9, 13.4, 0.9),
                    self.rect(13.5, 0.1, 3.0, 1.6, "grKpi", "Mål", kind="ellipse")])

    def build(self) -> str:
        r = self.r
        titles = ["Ledelsesberetning", "Hoved- og nøgletal", "Strategi og forretningsmodel",
                  "Markeder og kunder", "Bæredygtighed", "Medarbejdere", "Risikostyring",
                  "Selskabsledelse", "Koncernregnskab", "Noter"]
        self.cover()
        self.toc(titles)

        # 1 Ledelsesberetning
        self.h1(titles[0])
        self.para(5, 7)
        self.callout("Året i korte træk",
                     ["Omsætningen steg 14 % til 8.412 mio. kr.",
                      "EBIT-marginen blev 11,8 % mod 10,2 % året før.",
                      "Udbytte på 6,50 kr. pr. aktie foreslås."])
        self.para()
        self.h2("Hovedbegivenheder i 2026")
        self.bullets([self.t.item() for _ in range(5)])
        for _ in range(4):
            self.para()
        self.h2("Forventninger til 2027")
        self.para()
        self.callout("Forventninger", ["Omsætningsvækst på 8–12 %.",
                                       "EBIT-margin på 12–13 %."], side="right", tone="amber")
        for _ in range(3):
            self.para()
        self.chapter_sections(7)

        # 2 Hoved- og nøgletal
        self.h1(titles[1])
        self.para()
        years = ["2022", "2023", "2024", "2025", "2026"]
        self.table("Hoved- og nøgletal for koncernen (mio. kr.)", ["Mio. kr."] + years,
                   [[lab] + [self.num(b * (1.09 ** k) * r.uniform(0.95, 1.05), 0) for k in range(5)]
                    for lab, b in [("Omsætning", 5600), ("Bruttoresultat", 2100),
                                   ("EBITDA", 1010), ("EBIT", 640), ("Finansielle poster", -45),
                                   ("Resultat før skat", 595), ("Årets resultat", 462),
                                   ("Balancesum", 7900), ("Egenkapital", 3800),
                                   ("Investeringer", 520), ("Frie pengestrømme", 380)]],
                   [5.0, 2.4, 2.4, 2.4, 2.4, 2.4])
        self.para()
        self.figure(png_bar_chart("Omsætning pr. forretningsområde (mio. kr.)",
                                  ["2022", "2023", "2024", "2025", "2026"],
                                  [[2100, 2350, 2600, 2950, 3400], [1800, 1900, 2150, 2400, 2700],
                                   [1200, 1350, 1600, 1980, 2312]],
                                  ["Energi", "Service", "Materialer"]),
                    "Omsætning fordelt på forretningsområder, 2022–2026")
        self.para()
        self.h2("Nøgletal")
        self.table("Udvalgte nøgletal", ["Nøgletal", "2025", "2026"],
                   [["Bruttomargin (%)", "37,4", "38,9"], ["EBIT-margin (%)", "10,2", "11,8"],
                    ["Egenkapitalforrentning (%)", "12,1", "13,4"], ["Soliditetsgrad (%)", "47,0", "48,2"],
                    ["Antal medarbejdere", "3.912", "4.207"], ["Resultat pr. aktie (kr.)", "17,40", "19,85"]],
                   [9.0, 4.0, 4.0])
        self.callout("Sådan beregnes nøgletallene",
                     ["Nøgletallene er beregnet i overensstemmelse med Finansforeningens anbefalinger.",
                      "EBIT-margin = EBIT / omsætning × 100."], tone="sky")
        self.chapter_sections(5)

        # 3 Strategi
        self.h1(titles[2])
        self.para()
        self.h2("Vores forretningsmodel")
        self.para()
        # three boxes connected by arrows + feedback line
        shapes = [self.rect(0.5, 0.5, 4.2, 1.8, "grBox", "Råvarer og energi"),
                  self.rect(6.4, 0.5, 4.2, 1.8, "grBox2", "Produktion og service"),
                  self.rect(12.3, 0.5, 4.2, 1.8, "grBox", "Kunder og markeder"),
                  self.line(4.8, 1.4, 6.3, 1.4), self.line(10.7, 1.4, 12.2, 1.4),
                  self.line(14.4, 2.4, 14.4, 3.4, "grLine"), self.line(14.4, 3.4, 2.6, 3.4, "grLine"),
                  self.line(2.6, 3.4, 2.6, 2.45, "grArrow"),
                  self.rect(5.5, 3.0, 6.0, 0.8, "grLabel", "Genanvendelse og feedback", "ShapeSmall")]
        self.diagram_para(4.2, shapes)
        self.p(esc("Figur: Værdikæden fra råvare til kunde – materialer føres tilbage i kredsløbet."),
               "Caption")
        for _ in range(3):
            self.para()
        self.h2("Strategiske pejlemærker")
        self.bullets(["Vækst i kerneforretningen", "Digitalisering af service",
                      "Cirkulære materialer", "Attraktiv arbejdsplads"])
        self.body.append('<text:section text:style-name="Sect2" text:name="ToSpalter">')
        self.h2("Strategien i to spalter")
        for _ in range(7):
            self.para(4, 6)
        self.body.append("</text:section>")
        self.callout("Strategi 2030", ["Fire pejlemærker styrer vores prioriteringer.",
                                       "Målene følges op hvert kvartal i bestyrelsen."], side="right")
        self.chapter_sections(5)

        # 4 Markeder og kunder
        self.h1(titles[3])
        self.para()
        self.callout("Markedsandel", ["Norden: 23 %", "Tyskland: 9 %", "Storbritannien: 6 %"],
                     side="right", tone="sky")
        self.para()
        self.para()
        self.table("Omsætning fordelt på geografi (mio. kr.)", ["Region", "2025", "2026"],
                   self.fin_rows(["Danmark", "Sverige", "Norge", "Finland", "Tyskland",
                                  "Storbritannien", "Øvrige"], 1500, "I alt"), [9.0, 4.0, 4.0])
        self.para()
        self.figure(png_bar_chart("Kundetilfredshed (indeks)", ["Norden", "Tyskland", "UK", "Øvrige"],
                                  [[72, 66, 61, 58], [78, 70, 66, 63]], ["2025", "2026"]),
                    "Kundetilfredshed pr. region, 2025 og 2026", 13.0, 7.31)
        self.chapter_sections(5)
        # decorative separator line + small shapes
        self.diagram_para(1.2, [self.line(0, 0.6, 17, 0.6, "grRule"),
                                self.rect(7.9, 0.2, 0.8, 0.8, "grDot", kind="ellipse"),
                                self.rect(8.9, 0.35, 0.5, 0.5, "grDot2", kind="ellipse")])
        self.para()

        # 5 Bæredygtighed (heading on a coloured full-width band)
        band = (f'<draw:rect draw:style-name="grBand" draw:name="Overskriftsbånd" '
                f'text:anchor-type="paragraph" draw:z-index="0" svg:x="0cm" svg:y="0.2cm" '
                f'svg:width="21cm" svg:height="1.95cm"><text:p/></draw:rect>')
        self.h1(titles[4], "PHeadingBand", prefix=band)
        self.para()
        kpis = [("-38 %", "CO2-udledning"), ("72 %", "Grøn strøm"), ("91 %", "Genanvendt"),
                ("0,8", "Ulykker pr. mio. timer")]
        shapes = []
        for i, (v, lab) in enumerate(kpis):
            x = 0.4 + i * 4.3
            shapes.append(self.rect(x + 0.6, 0.2, 2.8, 2.8, "grKpi" if i % 2 == 0 else "grKpi2",
                                    v, "ShapeBig", kind="ellipse"))
            shapes.append(self.rect(x, 3.15, 4.0, 0.7, "grLabel", lab, "ShapeSmall"))
        self.diagram_para(4.1, shapes)
        self.para()
        self.callout("Klimamål", ["Scope 1 og 2: -70 % i 2030 i forhold til 2020.",
                                  "Scope 3: -30 % i 2030.", "Net-zero senest i 2045."])
        self.h2("Miljødata")
        self.table("Miljødata", ["Indikator", "Enhed", "2025", "2026"],
                   [["CO2 scope 1", "ton", "41.200", "36.800"], ["CO2 scope 2", "ton", "22.900", "15.100"],
                    ["Energiforbrug", "MWh", "310.400", "296.700"], ["Vandforbrug", "m³", "1.240.000", "1.108.000"],
                    ["Affald i alt", "ton", "18.300", "17.050"], ["Heraf genanvendt", "%", "86", "91"]],
                   [6.5, 2.5, 4.0, 4.0])
        self.chapter_sections(5)
        self.callout("Certificeringer", ["ISO 14001 og ISO 50001 på alle fabrikker.",
                                         "SBTi-godkendte klimamål siden 2024."], side="right", tone="sky")
        self.para()

        # 6 Medarbejdere
        self.h1(titles[5])
        self.para()
        self.h2("Vores prioriteter")
        self.bullets([self.t.item() for _ in range(4)], numbered=True)
        self.para()
        self.table("Medarbejdere fordelt på funktion", ["Funktion", "Antal", "Andel kvinder (%)"],
                   [["Produktion", "2.140", "24"], ["Service", "980", "18"], ["Salg og marketing", "430", "47"],
                    ["Forskning og udvikling", "310", "35"], ["Administration", "347", "58"],
                    ["I alt", "4.207", "30"]], [8.0, 4.5, 4.5])
        self.callout("Medarbejdertilfredshed", ["Svarprocent: 88 %.", "Engagement: 79 point (+3)."],
                     tone="amber")
        self.chapter_sections(5)

        # 7 Risikostyring
        self.h1(titles[6])
        self.para()
        self.h2("Risikomatrix")
        self.para(3, 4)
        cols = [["grRiskG", "grRiskY", "grRiskR"], ["grRiskG", "grRiskY", "grRiskY"],
                ["grRiskG", "grRiskG", "grRiskY"]]
        names = [["Valuta", "Cyber", "Råvarer"], ["Regulering", "Leverandører", "Energipriser"],
                 ["Rente", "Omdømme", "Kompetencer"]]
        shapes = [self.line(1.5, 7.2, 1.5, 0.1), self.line(1.5, 7.2, 15.5, 7.2)]
        for row in range(3):
            for col in range(3):
                shapes.append(self.rect(2.0 + col * 4.4, 0.4 + row * 2.2, 4.2, 2.0,
                                        cols[row][col], names[row][col]))
        shapes.append(self.rect(15.6, 6.8, 1.6, 0.7, "grLabel", "Effekt", "ShapeSmall"))
        shapes.append(self.rect(0.0, 0.0, 1.4, 0.7, "grLabel", "Sandsyn.", "ShapeSmall"))
        self.diagram_para(7.8, shapes)
        self.p(esc("Figur: Væsentlige risici efter sandsynlighed og effekt."), "Caption")
        for _ in range(2):
            self.para()
        self.callout("Cybersikkerhed", ["Alle medarbejdere gennemfører årlig træning.",
                                        "Beredskabsplanen blev testet to gange i 2026."], tone="sky")
        self.bullets([self.t.item() for _ in range(5)])
        self.chapter_sections(5)

        # 8 Selskabsledelse
        self.h1(titles[7])
        self.para()
        self.h2("Bestyrelse og direktion")
        self.table("Bestyrelsens sammensætning", ["Navn", "Rolle", "Indtrådt", "Møder"],
                   [["Anne Kjær Holm", "Formand", "2018", "9/9"], ["Søren Bæk", "Næstformand", "2020", "9/9"],
                    ["Mette Østergaard", "Medlem", "2021", "8/9"], ["Lars Møller Due", "Medlem", "2022", "9/9"],
                    ["Ida Frølund", "Medarbejdervalgt", "2023", "7/9"]], [5.5, 4.5, 3.5, 3.5])
        self.para()
        self.bullets(["Revisionsudvalget", "Vederlagsudvalget", "Nominerings­udvalget"])
        self.chapter_sections(5)
        self.callout("God selskabsledelse", ["Novo Grøn følger 45 af 46 anbefalinger.",
                                             "Afvigelsen er forklaret på hjemmesiden."], side="right")
        self.para()

        # 9 Koncernregnskab
        self.h1(titles[8])
        self.para(3, 4)
        self.h2("Resultatopgørelse")
        self.table("Resultatopgørelse 1. januar – 31. december (mio. kr.)", ["Mio. kr.", "2025", "2026"],
                   self.fin_rows(["Omsætning", "Produktionsomkostninger", "Bruttoresultat",
                                  "Salgsomkostninger", "Administrationsomkostninger",
                                  "Andre driftsindtægter", "Resultat af primær drift",
                                  "Finansielle indtægter", "Finansielle omkostninger",
                                  "Resultat før skat", "Skat af årets resultat"], 2000,
                                 "Resultat efter skat"), [9.0, 4.0, 4.0])
        self.h2("Balance")
        self.table("Balance pr. 31. december – aktiver (mio. kr.)", ["Mio. kr.", "2025", "2026"],
                   self.fin_rows(["Goodwill", "Immaterielle aktiver", "Grunde og bygninger",
                                  "Produktionsanlæg", "Driftsmateriel", "Varebeholdninger",
                                  "Tilgodehavender", "Likvide beholdninger"], 1400, "I alt aktiver"),
                   [9.0, 4.0, 4.0])
        self.para()
        self.table("Balance pr. 31. december – passiver (mio. kr.)", ["Mio. kr.", "2025", "2026"],
                   self.fin_rows(["Aktiekapital", "Overført resultat", "Foreslået udbytte",
                                  "Hensatte forpligtelser", "Gæld til kreditinstitutter",
                                  "Leverandørgæld", "Anden gæld"], 1300, "I alt passiver"),
                   [9.0, 4.0, 4.0])
        self.h2("Pengestrømsopgørelse")
        self.para(3, 4)
        self.table("Pengestrømme (mio. kr.)", ["Mio. kr.", "2025", "2026"],
                   self.fin_rows(["Pengestrømme fra drift", "Pengestrømme fra investering",
                                  "Pengestrømme fra finansiering", "Kursregulering"], 700,
                                 "Ændring i likvider"), [9.0, 4.0, 4.0])
        self.chapter_sections(5)

        # 10 Noter
        self.h1(titles[9])
        self.para(3, 4)
        for k in range(1, 9):
            self.h2(f"Note {k} – {NOTE_TITLES[k - 1]}")
            self.para(3, 5)
            if k % 2:
                self.table(NOTE_TITLES[k - 1] + " (mio. kr.)", ["Mio. kr.", "2025", "2026"],
                           self.fin_rows([self.r.choice(OBJ).capitalize() for _ in range(4)], 400, "I alt"),
                           [9.0, 4.0, 4.0])
            else:
                self.para(3, 5)
        self.callout("Revisionspåtegning", ["Den uafhængige revisors erklæring findes på side 2 i "
                                            "den fulde rapport.", "Revisor: Grøn & Partnere, "
                                            "statsautoriseret revisionspartnerselskab."], tone="sky")
        self.para()
        return self.xml()

    # ----- XML
    def xml(self) -> str:
        auto = "\n  ".join(AUTO_STYLES_WRITER + self.auto)
        header = (
            '<text:p text:style-name="Header">'
            f'<draw:frame draw:style-name="frHeadLogo" draw:name="Sidehoved-logo" text:anchor-type="as-char" '
            f'svg:width="0.55cm" svg:height="0.55cm" draw:z-index="5"><draw:image>'
            f"<office:binary-data>{self.logo}</office:binary-data></draw:image></draw:frame>"
            f"<text:s/>{esc(HEADER_TEXT)}<text:tab/>"
            '<text:chapter text:display="name" text:outline-level="1">Kapitel</text:chapter></text:p>')
        footer = ('<text:p text:style-name="{st}">Side <text:page-number text:select-page="current">1'
                  '</text:page-number> af <text:page-count>1</text:page-count></text:p>')
        return f"""<?xml version="1.0" encoding="UTF-8"?>
<office:document {NS} office:version="1.3" office:mimetype="application/vnd.oasis.opendocument.text">
 {meta("Årsrapport 2026 – " + COMPANY)}
 {FONTS}
 <office:styles>
  {MARKERS}
  {STYLES_WRITER}
 </office:styles>
 <office:automatic-styles>
  {auto}
 </office:automatic-styles>
 <office:master-styles>
  <style:master-page style:name="Standard" style:page-layout-name="pm1">
   <style:header>{header}</style:header>
   <style:footer>{footer.format(st="Footer")}</style:footer>
  </style:master-page>
  <style:master-page style:name="Cover" style:page-layout-name="pm2" style:next-style-name="Standard">
   <style:footer>{footer.format(st="FooterCover")}</style:footer>
  </style:master-page>
 </office:master-styles>
 <office:body><office:text>
{chr(10).join(self.body)}
 </office:text></office:body>
</office:document>
"""


SECTION_TITLES = ["Udvikling i året", "Resultat og økonomi", "Investeringer", "Organisation",
                  "Digitalisering", "Samarbejde med partnere", "Opfølgning på mål", "Fokusområder",
                  "Initiativer i 2026", "Status og næste skridt", "Effektivisering", "Innovation"]
FACT_TITLES = ["Fakta", "Nøgletal", "Vidste du?", "Status", "Målsætning", "Eksempel"]
STEP_WORDS = ["Analyse", "Planlægning", "Indkøb", "Produktion", "Levering", "Service",
              "Opfølgning", "Genanvendelse"]
NOTE_TITLES = ["Anvendt regnskabspraksis", "Segmentoplysninger", "Personaleomkostninger",
               "Immaterielle aktiver", "Materielle aktiver", "Varebeholdninger",
               "Hensatte forpligtelser", "Nærtstående parter"]

STYLES_WRITER = f"""
  <style:default-style style:family="graphic">
   <style:graphic-properties svg:stroke-color="{GREEN}" draw:fill-color="{GREEN_LIGHT}" style:flow-with-text="false"/>
   <style:text-properties fo:language="da" fo:country="DK"/>
  </style:default-style>
  <style:default-style style:family="paragraph">
   <style:paragraph-properties style:tab-stop-distance="1.25cm" style:writing-mode="lr-tb"/>
   <style:text-properties style:font-name="Liberation Serif" fo:font-size="10.5pt" fo:language="da" fo:country="DK" fo:hyphenate="false"/>
  </style:default-style>
  <style:default-style style:family="table"><style:table-properties table:border-model="collapsing"/></style:default-style>
  <style:style style:name="Frame" style:family="graphic">
   <style:graphic-properties text:anchor-type="paragraph" svg:x="0cm" svg:y="0cm" style:wrap="parallel" style:number-wrapped-paragraphs="no-limit" style:wrap-contour="false" style:vertical-pos="top" style:vertical-rel="paragraph-content" style:horizontal-pos="center" style:horizontal-rel="paragraph-content"/>
  </style:style>
  <style:style style:name="Graphics" style:family="graphic">
   <style:graphic-properties text:anchor-type="paragraph" svg:x="0cm" svg:y="0cm" style:wrap="none" style:vertical-pos="top" style:vertical-rel="paragraph" style:horizontal-pos="center" style:horizontal-rel="paragraph"/>
  </style:style>
  <style:style style:name="Standard" style:family="paragraph" style:class="text"/>
  <style:style style:name="Text_20_body" style:display-name="Text body" style:family="paragraph" style:parent-style-name="Standard" style:class="text">
   <style:paragraph-properties fo:margin-top="0cm" fo:margin-bottom="0.22cm" fo:line-height="125%" fo:text-align="justify" fo:orphans="2" fo:widows="2"/>
  </style:style>
  <style:style style:name="Heading" style:family="paragraph" style:parent-style-name="Standard" style:next-style-name="Text_20_body" style:class="text">
   <style:paragraph-properties fo:keep-with-next="always"/>
   <style:text-properties style:font-name="Liberation Sans" fo:font-weight="bold" fo:color="{GREEN}"/>
  </style:style>
  <style:style style:name="Heading_20_1" style:display-name="Heading 1" style:family="paragraph" style:parent-style-name="Heading" style:default-outline-level="1" style:class="text">
   <style:paragraph-properties fo:break-before="page" fo:margin-top="0.4cm" fo:margin-bottom="0.5cm"/>
   <style:text-properties fo:font-size="22pt"/>
  </style:style>
  <style:style style:name="Heading_20_2" style:display-name="Heading 2" style:family="paragraph" style:parent-style-name="Heading" style:default-outline-level="2" style:class="text">
   <style:paragraph-properties fo:margin-top="0.45cm" fo:margin-bottom="0.2cm"/>
   <style:text-properties fo:font-size="13pt" fo:color="{GREEN_2}"/>
  </style:style>
  <style:style style:name="List_20_Para" style:display-name="List Paragraph" style:family="paragraph" style:parent-style-name="Text_20_body">
   <style:paragraph-properties fo:margin-bottom="0.1cm" fo:text-align="start"/>
  </style:style>
  <style:style style:name="Header" style:family="paragraph" style:parent-style-name="Standard" style:class="extra">
   <style:paragraph-properties fo:padding-bottom="0.12cm" fo:border-bottom="0.75pt solid {GREEN_2}" fo:border-top="none" fo:border-left="none" fo:border-right="none">
    <style:tab-stops><style:tab-stop style:position="17cm" style:type="right"/></style:tab-stops>
   </style:paragraph-properties>
   <style:text-properties style:font-name="Liberation Sans" fo:font-size="8.5pt" fo:color="{SLATE}"/>
  </style:style>
  <style:style style:name="Footer" style:family="paragraph" style:parent-style-name="Standard" style:class="extra">
   <style:paragraph-properties fo:text-align="end"/>
   <style:text-properties style:font-name="Liberation Sans" fo:font-size="8.5pt" fo:color="{SLATE}"/>
  </style:style>
  <style:style style:name="FooterCover" style:family="paragraph" style:parent-style-name="Footer">
   <style:text-properties fo:color="#FFFFFF"/>
  </style:style>
  <style:style style:name="Caption" style:family="paragraph" style:parent-style-name="Standard" style:class="extra">
   <style:paragraph-properties fo:margin-top="0.1cm" fo:margin-bottom="0.35cm" fo:text-align="center"/>
   <style:text-properties style:font-name="Liberation Sans" fo:font-size="9pt" fo:font-style="italic" fo:color="{SLATE}"/>
  </style:style>
  <style:style style:name="Table_20_Caption" style:display-name="Table Caption" style:family="paragraph" style:parent-style-name="Caption">
   <style:paragraph-properties fo:text-align="start" fo:margin-top="0.25cm" fo:margin-bottom="0.12cm" fo:keep-with-next="always"/>
   <style:text-properties fo:font-style="normal" fo:font-weight="bold"/>
  </style:style>
  <style:style style:name="Figure" style:family="paragraph" style:parent-style-name="Standard">
   <style:paragraph-properties fo:text-align="center" fo:margin-top="0.2cm" fo:keep-with-next="always"/>
  </style:style>
  <style:style style:name="Diagram" style:family="paragraph" style:parent-style-name="Standard">
   <style:paragraph-properties fo:margin-top="0.2cm" fo:margin-bottom="0.1cm" fo:keep-together="always"/>
  </style:style>
  <style:style style:name="BoxAnchor" style:family="paragraph" style:parent-style-name="Standard">
   <style:paragraph-properties fo:margin-top="0cm" fo:margin-bottom="0cm" fo:line-height="0.1cm"/>
  </style:style>
  <style:style style:name="TableText" style:family="paragraph" style:parent-style-name="Standard">
   <style:text-properties style:font-name="Liberation Sans" fo:font-size="9pt"/>
  </style:style>
  <style:style style:name="TableTextB" style:family="paragraph" style:parent-style-name="TableText">
   <style:text-properties fo:font-weight="bold"/>
  </style:style>
  <style:style style:name="TableNum" style:family="paragraph" style:parent-style-name="TableText">
   <style:paragraph-properties fo:text-align="end"/>
  </style:style>
  <style:style style:name="TableNumB" style:family="paragraph" style:parent-style-name="TableNum">
   <style:text-properties fo:font-weight="bold"/>
  </style:style>
  <style:style style:name="TableHead" style:family="paragraph" style:parent-style-name="TableText">
   <style:text-properties fo:font-weight="bold" fo:color="#FFFFFF"/>
  </style:style>
  <style:style style:name="TableHeadNum" style:family="paragraph" style:parent-style-name="TableHead">
   <style:paragraph-properties fo:text-align="end"/>
  </style:style>
  <style:style style:name="BoxHeading" style:family="paragraph" style:parent-style-name="Standard">
   <style:paragraph-properties fo:margin-bottom="0.12cm"/>
   <style:text-properties style:font-name="Liberation Sans" fo:font-size="11pt" fo:font-weight="bold" fo:color="{GREEN}"/>
  </style:style>
  <style:style style:name="BoxText" style:family="paragraph" style:parent-style-name="Standard">
   <style:paragraph-properties fo:margin-bottom="0.05cm"/>
   <style:text-properties style:font-name="Liberation Sans" fo:font-size="9.5pt" fo:color="#222222"/>
  </style:style>
  <style:style style:name="ShapeText" style:family="paragraph" style:parent-style-name="Standard">
   <style:paragraph-properties fo:text-align="center"/>
   <style:text-properties style:font-name="Liberation Sans" fo:font-size="10pt" fo:font-weight="bold" fo:color="#FFFFFF"/>
  </style:style>
  <style:style style:name="ShapeSmall" style:family="paragraph" style:parent-style-name="ShapeText">
   <style:text-properties fo:font-size="8.5pt" fo:font-weight="normal" fo:color="{SLATE}"/>
  </style:style>
  <style:style style:name="ShapeBig" style:family="paragraph" style:parent-style-name="ShapeText">
   <style:text-properties fo:font-size="17pt"/>
  </style:style>
  <style:style style:name="CoverTitle" style:family="paragraph" style:parent-style-name="Standard">
   <style:paragraph-properties fo:margin-top="0.4cm" fo:margin-bottom="0.3cm"/>
   <style:text-properties style:font-name="Liberation Sans" fo:font-size="40pt" fo:font-weight="bold" fo:color="#FFFFFF"/>
  </style:style>
  <style:style style:name="CoverSub" style:family="paragraph" style:parent-style-name="Standard">
   <style:paragraph-properties fo:margin-bottom="5.5cm"/>
   <style:text-properties style:font-name="Liberation Sans" fo:font-size="18pt" fo:color="#D8EFE4"/>
  </style:style>
  <style:style style:name="CoverCompany" style:family="paragraph" style:parent-style-name="Standard">
   <style:text-properties style:font-name="Liberation Sans" fo:font-size="16pt" fo:font-weight="bold" fo:color="#FFFFFF"/>
  </style:style>
  <style:style style:name="CoverSmall" style:family="paragraph" style:parent-style-name="Standard">
   <style:text-properties style:font-name="Liberation Sans" fo:font-size="9pt" fo:color="#D8EFE4"/>
  </style:style>
  <style:style style:name="TocEntry" style:family="paragraph" style:parent-style-name="Standard">
   <style:paragraph-properties fo:margin-bottom="0.25cm">
    <style:tab-stops><style:tab-stop style:position="17cm" style:type="right" style:leader-style="dotted" style:leader-text="."/></style:tab-stops>
   </style:paragraph-properties>
   <style:text-properties style:font-name="Liberation Sans" fo:font-size="12pt"/>
  </style:style>
  <style:style style:name="Tb" style:family="text"><style:text-properties fo:font-weight="bold"/></style:style>
  <style:style style:name="Ti" style:family="text"><style:text-properties fo:font-style="italic"/></style:style>
  <style:style style:name="Tc" style:family="text"><style:text-properties fo:color="{ACCENT}"/></style:style>
  <style:style style:name="Tbc" style:family="text"><style:text-properties fo:font-weight="bold" fo:color="{GREEN_2}"/></style:style>
  <text:outline-style style:name="Outline">
   <text:outline-level-style text:level="1" style:num-format=""/>
   <text:outline-level-style text:level="2" style:num-format=""/>
  </text:outline-style>
  <text:list-style style:name="List_20_Bullet" style:display-name="List Bullet">
   <text:list-level-style-bullet text:level="1" text:bullet-char="•">
    <style:list-level-properties text:list-level-position-and-space-mode="label-alignment">
     <style:list-level-label-alignment text:label-followed-by="listtab" text:list-tab-stop-position="1.0cm" fo:text-indent="-0.5cm" fo:margin-left="1.0cm"/>
    </style:list-level-properties>
    <style:text-properties fo:color="{GREEN_2}"/>
   </text:list-level-style-bullet>
  </text:list-style>
  <text:list-style style:name="Numbering_20_123" style:display-name="Numbering 123">
   <text:list-level-style-number text:level="1" style:num-suffix="." style:num-format="1">
    <style:list-level-properties text:list-level-position-and-space-mode="label-alignment">
     <style:list-level-label-alignment text:label-followed-by="listtab" text:list-tab-stop-position="1.0cm" fo:text-indent="-0.6cm" fo:margin-left="1.0cm"/>
    </style:list-level-properties>
   </text:list-level-style-number>
  </text:list-style>"""

_POS_PARA = dict(style__wrap="run-through", style__run_through="foreground",
                 style__vertical_pos="from-top", style__vertical_rel="paragraph",
                 style__horizontal_pos="from-left", style__horizontal_rel="paragraph")
_TEXT_MID = dict(draw__textarea_horizontal_align="center", draw__textarea_vertical_align="middle",
                 fo__padding="0.1cm")


def _box(name, fill, stroke, side="full"):
    pos = (dict(style__wrap="none", style__horizontal_pos="center")
           if side == "full" else
           dict(style__wrap="parallel", style__horizontal_pos="right", fo__margin_left="0.4cm",
                style__number_wrapped_paragraphs="no-limit"))
    return gstyle(name, "Frame", fo__background_color=fill,
                  fo__border=f"0.75pt solid {stroke}", fo__padding="0.3cm",
                  fo__margin_top="0.1cm", fo__margin_bottom="0.3cm",
                  style__vertical_pos="top", style__vertical_rel="paragraph",
                  style__horizontal_rel="paragraph", **pos)


AUTO_STYLES_WRITER = [
    # text inside drawn shapes: Writer only honours automatic styles there
    f'''<style:style style:name="TShapeText" style:family="text"><style:text-properties style:font-name="Liberation Sans" fo:font-family="'Liberation Sans'" fo:font-size="10pt" fo:font-weight="bold" fo:color="#FFFFFF"/></style:style>''',
    f'''<style:style style:name="TShapeSmall" style:family="text"><style:text-properties style:font-name="Liberation Sans" fo:font-family="'Liberation Sans'" fo:font-size="8.5pt" fo:color="{SLATE}"/></style:style>''',
    f'''<style:style style:name="TShapeBig" style:family="text"><style:text-properties style:font-name="Liberation Sans" fo:font-family="'Liberation Sans'" fo:font-size="17pt" fo:font-weight="bold" fo:color="#FFFFFF"/></style:style>''',
    f"""<style:page-layout style:name="pm1">
   <style:page-layout-properties fo:page-width="21cm" fo:page-height="29.7cm" style:print-orientation="portrait" fo:margin-top="1.2cm" fo:margin-bottom="1.2cm" fo:margin-left="2cm" fo:margin-right="2cm"/>
   <style:header-style><style:header-footer-properties fo:min-height="0cm" fo:margin-bottom="0.7cm"/></style:header-style>
   <style:footer-style><style:header-footer-properties fo:min-height="0cm" fo:margin-top="0.6cm"/></style:footer-style>
  </style:page-layout>""",
    """<style:page-layout style:name="pm2">
   <style:page-layout-properties fo:page-width="21cm" fo:page-height="29.7cm" style:print-orientation="portrait" fo:margin-top="2.5cm" fo:margin-bottom="1.2cm" fo:margin-left="2cm" fo:margin-right="2cm"/>
   <style:footer-style><style:header-footer-properties fo:min-height="0cm" fo:margin-top="0.6cm"/></style:footer-style>
  </style:page-layout>""",
    """<style:style style:name="PCoverFirst" style:family="paragraph" style:parent-style-name="Standard" style:master-page-name="Cover">
   <style:paragraph-properties fo:margin-top="5.5cm" fo:margin-bottom="1.6cm"/></style:style>""",
    f"""<style:style style:name="PTocTitle" style:family="paragraph" style:parent-style-name="Heading" style:master-page-name="Standard">
   <style:paragraph-properties fo:break-before="page" fo:margin-top="0.4cm" fo:margin-bottom="0.8cm"/>
   <style:text-properties fo:font-size="22pt"/></style:style>""",
    """<style:style style:name="PHeadingBand" style:family="paragraph" style:parent-style-name="Heading_20_1">
   <style:paragraph-properties fo:margin-top="0.75cm" fo:margin-bottom="0.9cm"/>
   <style:text-properties fo:color="#FFFFFF"/></style:style>""",
    """<style:style style:name="Sect2" style:family="section">
   <style:section-properties text:dont-balance-text-columns="false">
    <style:columns fo:column-count="2" fo:column-gap="0.8cm">
     <style:column-sep style:width="0.03cm" style:color="#8FB3A2" style:height="100%" style:vertical-align="top"/>
     <style:column style:rel-width="5000*" fo:start-indent="0cm" fo:end-indent="0.4cm"/>
     <style:column style:rel-width="5000*" fo:start-indent="0.4cm" fo:end-indent="0cm"/>
    </style:columns>
   </style:section-properties></style:style>""",
    f"""<style:style style:name="Tab" style:family="table">
   <style:table-properties style:width="17cm" table:align="margins" fo:margin-top="0cm" fo:margin-bottom="0.1cm"/></style:style>""",
    f"""<style:style style:name="CellHead" style:family="table-cell">
   <style:table-cell-properties fo:background-color="{GREEN}" fo:padding="0.09cm" fo:border="0.5pt solid {GREEN}"/></style:style>""",
    f"""<style:style style:name="CellPlain" style:family="table-cell">
   <style:table-cell-properties fo:background-color="#FFFFFF" fo:padding="0.09cm" fo:border="0.5pt solid {BORDER}"/></style:style>""",
    f"""<style:style style:name="CellZebra" style:family="table-cell">
   <style:table-cell-properties fo:background-color="{ZEBRA}" fo:padding="0.09cm" fo:border="0.5pt solid {BORDER}"/></style:style>""",
    f"""<style:style style:name="CellTotal" style:family="table-cell">
   <style:table-cell-properties fo:background-color="{GREEN_LIGHT}" fo:padding="0.09cm" fo:border-top="1.5pt solid {GREEN}" fo:border-bottom="0.5pt solid {BORDER}" fo:border-left="0.5pt solid {BORDER}" fo:border-right="0.5pt solid {BORDER}"/></style:style>""",
    gstyle("frImg", "Graphics", style__vertical_pos="top", style__vertical_rel="baseline", fo__border="none"),
    gstyle("frHeadLogo", "Graphics", style__vertical_pos="middle", style__vertical_rel="text"),
    gstyle("frLogo", "Graphics", style__vertical_pos="top", style__vertical_rel="baseline"),
    _box("frBox", GREEN_LIGHT, GREEN_2), _box("frBoxR", GREEN_LIGHT, GREEN_2, "right"),
    _box("frBoxSky", SKY_LIGHT, SKY), _box("frBoxSkyR", SKY_LIGHT, SKY, "right"),
    _box("frBoxAmber", "#FBF1DC", AMBER), _box("frBoxAmberR", "#FBF1DC", AMBER, "right"),
    gstyle("grCoverBg", draw__stroke="none", draw__fill="solid", draw__fill_color=GREEN,
           style__wrap="run-through", style__run_through="background",
           style__vertical_pos="from-top", style__vertical_rel="page",
           style__horizontal_pos="from-left", style__horizontal_rel="page"),
    gstyle("grCoverCircle", draw__stroke="none", draw__fill="solid", draw__fill_color=GREEN_2,
           style__wrap="run-through", style__run_through="background",
           style__vertical_pos="from-top", style__vertical_rel="page",
           style__horizontal_pos="from-left", style__horizontal_rel="page"),
    gstyle("grCoverCircle2", draw__stroke="solid", svg__stroke_color="#7FC2A2", svg__stroke_width="0.1cm",
           draw__fill="none", style__wrap="run-through", style__run_through="background",
           style__vertical_pos="from-top", style__vertical_rel="page",
           style__horizontal_pos="from-left", style__horizontal_rel="page"),
    gstyle("grCoverRule", draw__stroke="solid", svg__stroke_color="#FFFFFF", svg__stroke_width="0.08cm",
           style__wrap="run-through", style__run_through="background",
           style__vertical_pos="from-top", style__vertical_rel="page",
           style__horizontal_pos="from-left", style__horizontal_rel="page"),
    gstyle("grBand", draw__stroke="none", draw__fill="solid", draw__fill_color=GREEN_2,
           style__wrap="run-through", style__run_through="background",
           style__vertical_pos="from-top", style__vertical_rel="paragraph",
           style__horizontal_pos="from-left", style__horizontal_rel="page"),
    gstyle("grBox", draw__stroke="solid", svg__stroke_color=GREEN, svg__stroke_width="0.03cm",
           draw__fill="solid", draw__fill_color=GREEN_2, **_POS_PARA, **_TEXT_MID),
    gstyle("grBox2", draw__stroke="solid", svg__stroke_color="#2A5F85", svg__stroke_width="0.03cm",
           draw__fill="solid", draw__fill_color=SKY, **_POS_PARA, **_TEXT_MID),
    gstyle("grLabel", draw__stroke="none", draw__fill="solid", draw__fill_color="#FFFFFF",
           **_POS_PARA, **_TEXT_MID),
    gstyle("grKpi", draw__stroke="solid", svg__stroke_color=GREEN, svg__stroke_width="0.05cm",
           draw__fill="solid", draw__fill_color=GREEN_2, **_POS_PARA, **_TEXT_MID),
    gstyle("grKpi2", draw__stroke="solid", svg__stroke_color="#2A5F85", svg__stroke_width="0.05cm",
           draw__fill="solid", draw__fill_color=SKY, **_POS_PARA, **_TEXT_MID),
    gstyle("grRiskG", draw__stroke="solid", svg__stroke_color="#FFFFFF", svg__stroke_width="0.05cm",
           draw__fill="solid", draw__fill_color="#5FA77F", **_POS_PARA, **_TEXT_MID),
    gstyle("grRiskY", draw__stroke="solid", svg__stroke_color="#FFFFFF", svg__stroke_width="0.05cm",
           draw__fill="solid", draw__fill_color=AMBER, **_POS_PARA, **_TEXT_MID),
    gstyle("grRiskR", draw__stroke="solid", svg__stroke_color="#FFFFFF", svg__stroke_width="0.05cm",
           draw__fill="solid", draw__fill_color=ACCENT, **_POS_PARA, **_TEXT_MID),
    gstyle("grDot", draw__stroke="none", draw__fill="solid", draw__fill_color=GREEN_2, **_POS_PARA),
    gstyle("grDot2", draw__stroke="solid", svg__stroke_color=SKY, svg__stroke_width="0.04cm",
           draw__fill="none", **_POS_PARA),
    gstyle("grArrow", draw__stroke="solid", svg__stroke_color=SLATE, svg__stroke_width="0.05cm",
           draw__marker_end="Arrow", draw__marker_end_width="0.3cm", **_POS_PARA),
    gstyle("grLine", draw__stroke="solid", svg__stroke_color=SLATE, svg__stroke_width="0.05cm",
           **_POS_PARA),
    gstyle("grRule", draw__stroke="solid", svg__stroke_color=BORDER, svg__stroke_width="0.04cm",
           **_POS_PARA),
]


# --------------------------------------------------------------------------- Impress deck

class Deck:
    """Builds the landscape strategy deck as a flat ODP (.fodp).  Page 28 x 15.75 cm."""

    W, H = 28.0, 15.75

    def __init__(self, seed: int = SEED + 1):
        self.t = Text(seed)
        self.r = self.t.r
        self.n = 0
        self.pages: list[str] = []
        self.styles: dict[str, str] = {}
        self._names: dict[str, str] = {}
        self.logo = b64(png_logo())

    def uid(self, p="s") -> str:
        self.n += 1
        return f"{p}{self.n}"

    # ----- style helpers (deduplicated automatic styles)
    def g(self, **props) -> str:
        key = "g" + repr(sorted(props.items()))
        if key not in self._names:
            name = f"gr{len(self.styles) + 1}"
            self.styles[key] = gstyle(name, **props)
            self._names[key] = name
        return self._names[key]

    def ptxt(self, size=14, color="#222222", bold=False, align="start", italic=False) -> tuple[str, str]:
        pkey = f"p{align}"
        if pkey not in self.styles:
            name = f"P{len(self.styles) + 1}"
            self.styles[pkey] = (f'<style:style style:name="{name}" style:family="paragraph">'
                                 f'<style:paragraph-properties fo:text-align="{align}" fo:margin-bottom="0.15cm"/>'
                                 f"</style:style>")
            self._names[pkey] = name
        tkey = f"t{size}{color}{bold}{italic}"
        if tkey not in self.styles:
            name = f"T{len(self.styles) + 1}"
            self.styles[tkey] = (
                f'<style:style style:name="{name}" style:family="text"><style:text-properties '
                f'fo:font-size="{size}pt" fo:color="{color}" style:font-name="Liberation Sans" '
                f'fo:font-weight="{"bold" if bold else "normal"}" '
                f'fo:font-style="{"italic" if italic else "normal"}"/></style:style>')
            self._names[tkey] = name
        return self._names[pkey], self._names[tkey]

    def paras(self, lines: list[str], **fmt) -> str:
        ps, ts = self.ptxt(**fmt)
        return "".join(f'<text:p text:style-name="{ps}"><text:span text:style-name="{ts}">'
                       f"{esc(x)}</text:span></text:p>" for x in lines)

    # ----- shapes
    def rect(self, x, y, w, h, fill, text=(), stroke=None, kind="rect", fmt=None, valign="middle",
             halign="center", pad=0.2, sid=None) -> str:
        props = dict(draw__fill="solid" if fill else "none", draw__textarea_vertical_align=valign,
                     draw__textarea_horizontal_align="justify", draw__auto_grow_height="false", fo__wrap_option="wrap",
                     fo__padding_left=f"{pad}cm", fo__padding_right=f"{pad}cm",
                     fo__padding_top="0.1cm", fo__padding_bottom="0.1cm")
        if fill:
            props["draw__fill_color"] = fill
        if stroke:
            props.update(draw__stroke="solid", svg__stroke_color=stroke, svg__stroke_width="0.05cm")
        else:
            props["draw__stroke"] = "none"
        fmt = dict(fmt or {})
        fmt.setdefault("align", halign)
        idattr = f' draw:id="{sid}" xml:id="{sid}"' if sid else ""
        head = (f'draw:style-name="{self.g(**props)}" draw:name="{self.uid("Figur")}"{idattr} '
                f'svg:x="{x}cm" svg:y="{y}cm" svg:width="{w}cm" svg:height="{h}cm"')
        if not text:  # plain ODF rect / ellipse
            tag = {"rect": "draw:rect", "ellipse": "draw:ellipse"}[kind]
            return f"<{tag} {head}/>"
        # shapes with text are custom shapes: only those wrap their text in Impress
        geo = ('draw:type="rectangle" draw:enhanced-path="M 0 0 L 21600 0 21600 21600 0 21600 0 0 Z N"'
               if kind == "rect" else
               'draw:type="ellipse" draw:text-areas="3163 3163 18437 18437" '
               'draw:enhanced-path="U 10800 10800 10800 10800 0 360 Z N"')
        return (f"<draw:custom-shape {head}>{self.paras(list(text), **fmt)}"
                f'<draw:enhanced-geometry svg:viewBox="0 0 21600 21600" {geo}/></draw:custom-shape>')

    def textbox(self, x, y, w, h, lines, **fmt) -> str:
        st = self.g(draw__stroke="none", draw__fill="none", draw__auto_grow_height="false",
                    draw__textarea_vertical_align="top", fo__padding="0.1cm")
        return (f'<draw:frame draw:style-name="{st}" draw:name="{self.uid("Tekst")}" '
                f'svg:x="{x}cm" svg:y="{y}cm" svg:width="{w}cm" svg:height="{h}cm">'
                f"<draw:text-box>{self.paras(lines, **fmt)}</draw:text-box></draw:frame>")

    def line(self, x1, y1, x2, y2, color=SLATE, arrow=True, width="0.06cm") -> str:
        props = dict(draw__stroke="solid", svg__stroke_color=color, svg__stroke_width=width)
        if arrow:
            props.update(draw__marker_end="Arrow", draw__marker_end_width="0.35cm")
        return (f'<draw:line draw:style-name="{self.g(**props)}" draw:name="{self.uid("Linje")}" '
                f'svg:x1="{x1}cm" svg:y1="{y1}cm" svg:x2="{x2}cm" svg:y2="{y2}cm"><text:p/></draw:line>')

    def connector(self, a, ga, b, gb, pts, color=SLATE) -> str:
        st = self.g(draw__stroke="solid", svg__stroke_color=color, svg__stroke_width="0.05cm",
                    draw__marker_end="Arrow", draw__marker_end_width="0.3cm")
        (x1, y1), (x2, y2) = pts
        return (f'<draw:connector draw:style-name="{st}" draw:name="{self.uid("Forbindelse")}" '
                f'draw:type="standard" svg:x1="{x1}cm" svg:y1="{y1}cm" svg:x2="{x2}cm" svg:y2="{y2}cm" '
                f'draw:start-shape="{a}" draw:start-glue-point="{ga}" draw:end-shape="{b}" '
                f'draw:end-glue-point="{gb}"><text:p/></draw:connector>')

    def chevron(self, x, y, w, h, fill, text) -> str:
        st = self.g(draw__stroke="none", draw__fill="solid", draw__fill_color=fill,
                    draw__textarea_vertical_align="middle", draw__auto_grow_height="false")
        return (f'<draw:custom-shape draw:style-name="{st}" draw:name="{self.uid("Pil")}" '
                f'svg:x="{x}cm" svg:y="{y}cm" svg:width="{w}cm" svg:height="{h}cm">'
                f"{self.paras([text], size=13, color='#FFFFFF', bold=True, align='center')}"
                '<draw:enhanced-geometry svg:viewBox="0 0 21600 21600" draw:type="chevron" '
                'draw:modifiers="16200" draw:text-areas="?f0 0 ?f2 21600" '
                'draw:enhanced-path="M ?f0 0 L 21600 10800 ?f0 21600 0 21600 ?f1 10800 0 0 Z N">'
                '<draw:equation draw:name="f0" draw:formula="$0 "/>'
                '<draw:equation draw:name="f1" draw:formula="21600-?f0 "/>'
                '<draw:equation draw:name="f2" draw:formula="?f1 *1/2"/>'
                '<draw:handle draw:handle-position="$0 top" draw:handle-range-x-minimum="0" '
                'draw:handle-range-x-maximum="21600"/></draw:enhanced-geometry></draw:custom-shape>')

    def image(self, x, y, w, h, png_b64) -> str:
        st = self.g(draw__stroke="none", draw__fill="none")
        return (f'<draw:frame draw:style-name="{st}" draw:name="{self.uid("Billede")}" '
                f'svg:x="{x}cm" svg:y="{y}cm" svg:width="{w}cm" svg:height="{h}cm">'
                f"<draw:image><office:binary-data>{png_b64}</office:binary-data></draw:image></draw:frame>")

    # ----- slide chrome
    def slide(self, title: str, content: list[str], *, chrome=True) -> None:
        k = len(self.pages) + 1
        parts = []
        if chrome:
            parts.append(self.rect(0, 0, self.W, 2.3, GREEN, [title], valign="middle", halign="start",
                                   pad=1.2, fmt=dict(size=26, color="#FFFFFF", bold=True)))
            parts.append(self.rect(0, 2.3, self.W, 0.12, AMBER))
            parts.append(self.image(self.W - 2.0, 0.55, 1.2, 1.2, self.logo))
        parts += content
        parts.append(self.line(1.2, self.H - 1.1, self.W - 1.2, self.H - 1.1, BORDER, arrow=False,
                               width="0.03cm"))
        parts.append(self.textbox(1.1, self.H - 1.0, 14, 0.8,
                                  [f"{COMPANY} · Strategi 2027–2030"], size=10, color=SLATE))
        parts.append(self.textbox(self.W - 7.1, self.H - 1.0, 6, 0.8, [f"Side {k} af {{N}}"],
                                  size=10, color=SLATE, align="end"))
        self.pages.append(f'<draw:page draw:name="Dias{k}" draw:style-name="dp1" '
                          f'draw:master-page-name="Default">{"".join(parts)}</draw:page>')

    def build(self) -> str:
        t, r = self.t, self.r
        W, H = self.W, self.H
        white = dict(color="#FFFFFF")
        # 1 title
        self.slide("", [
            self.rect(0, 0, W, H, GREEN),
            self.rect(17.5, -4, 14, 14, GREEN_2, kind="ellipse"),
            self.rect(21, 9.5, 5, 5, None, stroke="#7FC2A2", kind="ellipse"),
            self.textbox(2, 4.2, 18, 2.4, ["Strategi 2027–2030"], size=44, bold=True, **white),
            self.textbox(2, 6.8, 18, 1.4, ["Grøn vækst med ansvar"], size=22, color="#D8EFE4"),
            self.line(2.1, 8.6, 9, 8.6, "#FFFFFF", arrow=False, width="0.08cm"),
            self.textbox(2, 9.0, 16, 1.2, [f"{COMPANY} · Bestyrelsesseminar, november 2026"],
                         size=14, **white),
            self.image(2.1, 1.4, 2.2, 2.2, self.logo),
        ], chrome=False)
        # 2 agenda
        items = ["Udgangspunkt", "Vision og ambition", "Strategiske pejlemærker", "Markeder",
                 "Organisation", "Køreplan", "Økonomi og risici"]
        c = []
        for i, it in enumerate(items):
            y = 3.1 + i * 1.5
            c.append(self.rect(2, y, 1.1, 1.1, GREEN_2 if i % 2 == 0 else SKY, [str(i + 1)],
                               kind="ellipse", fmt=dict(size=14, bold=True, **white), pad=0))
            c.append(self.textbox(3.6, y + 0.1, 11, 1.0, [it], size=17))
        c.append(self.image(16, 3.2, 10, 6.25, b64(png_photo(3))))
        self.slide("Dagsorden", c)
        # 3 vision: three boxes
        c = [self.textbox(1.2, 2.9, 25, 1.2, ["Vi vil være Nordens foretrukne partner i den grønne omstilling."],
                          size=18, italic=True, color=SLATE)]
        for i, (head, col) in enumerate([("Vækst", GREEN_2), ("Ansvar", SKY), ("Innovation", ACCENT)]):
            x = 1.2 + i * 8.7
            c.append(self.rect(x, 4.5, 8.0, 1.2, col, [head], fmt=dict(size=18, bold=True, **white)))
            c.append(self.rect(x, 5.7, 8.0, 7.6, "#F4F7F6", [t.sentence() for _ in range(3)],
                               stroke=col, valign="top", halign="start", pad=0.4, fmt=dict(size=13)))
        self.slide("Vision og ambition", c)
        # 4 process (chevrons + arrows)
        steps = ["Analyse", "Design", "Pilot", "Udrulning", "Opfølgning"]
        c = []
        for i, s in enumerate(steps):
            c.append(self.chevron(1.2 + i * 5.1, 3.6, 5.3, 2.0, [GREEN, GREEN_2, SKY, "#2A5F85", SLATE][i], s))
            c.append(self.textbox(1.4 + i * 5.1, 6.2, 4.6, 4.0, [t.item()], size=12))
        c.append(self.line(2.0, 11.2, 25.5, 11.2, ACCENT))
        c.append(self.textbox(2.0, 11.5, 20, 1.0, ["Tid: 2027 → 2030"], size=12, color=ACCENT, bold=True))
        self.slide("Sådan gennemfører vi strategien", c)
        # 5 market + chart image
        chart = png_bar_chart("Markedsvækst (mia. kr.)", ["DK", "SE", "NO", "FI", "DE"],
                              [[12, 18, 9, 7, 41], [15, 22, 11, 9, 52]], ["2026", "2030"])
        c = [self.image(1.2, 3.0, 14.5, 8.16, b64(chart)),
             self.rect(16.5, 3.0, 10.3, 8.2, SKY_LIGHT, ["Nøglepunkter"] , stroke=SKY, valign="top",
                       halign="start", pad=0.4, fmt=dict(size=16, bold=True, color="#2A5F85")),
             self.textbox(16.8, 4.2, 9.8, 6.8, ["• " + t.item() for _ in range(4)], size=13)]
        self.slide("Markeder i vækst", c)
        # 6 org chart with connectors
        c = [self.rect(11, 3.0, 6, 1.6, GREEN, ["Koncernledelse"], fmt=dict(size=14, bold=True, **white),
                       sid="org0")]
        units = ["Energi", "Service", "Materialer", "Digital"]
        for i, u in enumerate(units):
            x = 1.5 + i * 6.5
            c.append(self.rect(x, 6.4, 5.5, 1.5, GREEN_2 if i % 2 == 0 else SKY, [u],
                               fmt=dict(size=14, bold=True, **white), sid=f"org{i + 1}"))
            c.append(self.connector("org0", 2, f"org{i + 1}", 0, ((14, 4.6), (x + 2.75, 6.4))))
            c.append(self.rect(x, 8.9, 5.5, 2.8, "#FFFFFF", [t.item()], stroke=BORDER,
                               halign="start", fmt=dict(size=11)))
            c.append(self.line(x + 2.75, 7.9, x + 2.75, 8.85, BORDER))
        self.slide("Organisation", c)
        # 7 timeline
        c = [self.line(1.5, 7.5, 26.5, 7.5, SLATE, width="0.1cm")]
        for i, yr in enumerate(["2027", "2028", "2029", "2030"]):
            x = 3 + i * 6.5
            c.append(self.rect(x, 6.8, 1.4, 1.4, AMBER if i == 3 else GREEN_2, kind="ellipse",
                               stroke="#FFFFFF"))
            c.append(self.textbox(x - 1.3, 4.6, 4, 1.0, [yr], size=20, bold=True, color=GREEN, align="center"))
            c.append(self.textbox(x - 2.0, 8.8, 5.4, 3.5, [t.item()], size=12, align="center"))
        self.slide("Tidslinje", c)
        # 8 SWOT
        c = []
        for i, (head, col) in enumerate([("Styrker", GREEN_2), ("Svagheder", AMBER),
                                         ("Muligheder", SKY), ("Trusler", ACCENT)]):
            x = 1.2 + (i % 2) * 13.0
            y = 2.9 + (i // 2) * 5.6
            c.append(self.rect(x, y, 12.6, 5.3, col, [head] + ["• " + t.item() for _ in range(2)],
                               valign="top", halign="start", pad=0.5, fmt=dict(size=13, **white)))
        self.slide("SWOT-analyse", c)
        # 9 KPI circles
        c = []
        for i, (v, lab) in enumerate([("+10 %", "Årlig vækst"), ("13 %", "EBIT-margin"),
                                      ("-70 %", "CO2 scope 1+2"), ("85", "Kundetilfredshed")]):
            x = 1.8 + i * 6.4
            c.append(self.rect(x, 3.4, 5.0, 5.0, [GREEN_2, SKY, GREEN, AMBER][i], [v], kind="ellipse",
                               fmt=dict(size=30, bold=True, **white), pad=0))
            c.append(self.textbox(x - 0.5, 8.8, 6, 1.0, [lab], size=15, bold=True, align="center",
                                  color=SLATE))
            c.append(self.textbox(x - 0.5, 9.8, 6, 2.6, [t.sentence()], size=11, align="center"))
        self.slide("Mål for 2030", c)
        # 10 roadmap (gantt bars)
        c = []
        for j, yr in enumerate(["2027", "2028", "2029", "2030"]):
            c.append(self.textbox(7.5 + j * 4.9, 2.8, 4.5, 0.9, [yr], size=13, bold=True, align="center"))
            c.append(self.line(7.5 + j * 4.9, 3.7, 7.5 + j * 4.9, 13.8, BORDER, arrow=False, width="0.02cm"))
        for i, name in enumerate(["Ny fabrik", "Serviceplatform", "Cirkulære materialer",
                                  "Talentprogram", "Opkøb i Tyskland", "ERP-system"]):
            y = 4.0 + i * 1.6
            c.append(self.textbox(1.2, y, 6.2, 1.0, [name], size=13))
            start = r.randint(0, 4)
            length = r.randint(2, 8 - start)
            c.append(self.rect(7.5 + start * 2.45, y + 0.1, length * 2.45, 0.9,
                               [GREEN_2, SKY, AMBER][i % 3], ["Fase " + str(i + 1)],
                               fmt=dict(size=10, bold=True, **white)))
        self.slide("Køreplan", c)
        # 11 pyramid (stacked trapezium-like rects)
        c = []
        for i, (lab, col) in enumerate([("Purpose", GREEN), ("Vision", GREEN_2), ("Mål", SKY),
                                        ("Initiativer", "#2A5F85"), ("Drift", SLATE)]):
            w = 4 + i * 3.4
            c.append(self.rect(14 - w / 2, 3.0 + i * 2.1, w, 1.9, col, [lab],
                               fmt=dict(size=15, bold=True, **white)))
        c.append(self.textbox(1.2, 3.2, 6.5, 8, [t.sentence() for _ in range(3)], size=12))
        self.slide("Strategihuset", c)
        # 12 risks: boxes + arrows
        c = []
        for i, (risk, act) in enumerate([("Råvarepriser", "Langsigtede kontrakter"),
                                         ("Cyberangreb", "Beredskab og træning"),
                                         ("Mangel på kompetencer", "Talentprogram"),
                                         ("Regulering", "Tæt dialog med myndigheder")]):
            y = 3.0 + i * 2.6
            c.append(self.rect(1.5, y, 9, 2.0, "#FBE3DD", [risk], stroke=ACCENT,
                               fmt=dict(size=15, bold=True, color=ACCENT)))
            c.append(self.line(10.8, y + 1.0, 15.2, y + 1.0, SLATE))
            c.append(self.rect(15.5, y, 11, 2.0, GREEN_LIGHT, [act], stroke=GREEN_2,
                               fmt=dict(size=15, color=GREEN)))
        self.slide("Risici og tiltag", c)
        # 13 photo + quote
        c = [self.image(1.2, 3.0, 13.5, 8.44, b64(png_photo(7))),
             self.rect(15.5, 3.0, 11.3, 8.44, GREEN_LIGHT,
                       ["“Den grønne omstilling er vores største forretningsmulighed.”",
                        "– Anne Kjær Holm, bestyrelsesformand"], stroke=GREEN_2, halign="start",
                       pad=0.6, fmt=dict(size=17, italic=True, color=GREEN)),
             self.rect(25.6, 10.3, 1.6, 1.6, AMBER, kind="ellipse")]
        self.slide("Hvorfor nu?", c)
        # 14 closing
        c = [self.rect(0, 2.42, W, H - 2.42 - 1.4, GREEN_LIGHT),
             self.textbox(2, 4.5, 24, 2, ["Tak for opmærksomheden"], size=36, bold=True, color=GREEN,
                          align="center"),
             self.textbox(2, 7.2, 24, 3, ["Spørgsmål og drøftelse", "strategi@novogroen.example"],
                          size=18, color=SLATE, align="center"),
             self.rect(12.9, 10.6, 2.2, 2.2, GREEN_2, kind="ellipse")]
        self.slide("Afslutning", c)
        n = len(self.pages)
        pages = "\n".join(p.replace("{N}", str(n)) for p in self.pages)
        auto = "\n  ".join(self.styles.values())
        return f"""<?xml version="1.0" encoding="UTF-8"?>
<office:document {NS} office:version="1.3" office:mimetype="application/vnd.oasis.opendocument.presentation">
 {meta("Strategi 2027–2030 – " + COMPANY)}
 {FONTS}
 <office:styles>
  {MARKERS}
  <style:default-style style:family="graphic">
   <style:graphic-properties svg:stroke-color="{GREEN}" draw:fill-color="{GREEN_LIGHT}"/>
   <style:paragraph-properties style:writing-mode="lr-tb"/>
   <style:text-properties style:font-name="Liberation Sans" fo:font-size="14pt" fo:language="da" fo:country="DK"/>
  </style:default-style>
 </office:styles>
 <office:automatic-styles>
  <style:page-layout style:name="PM1"><style:page-layout-properties fo:margin-top="0cm" fo:margin-bottom="0cm" fo:margin-left="0cm" fo:margin-right="0cm" fo:page-width="{W}cm" fo:page-height="{H}cm" style:print-orientation="landscape"/></style:page-layout>
  <style:style style:name="dp1" style:family="drawing-page"><style:drawing-page-properties draw:fill="solid" draw:fill-color="#FFFFFF" presentation:background-visible="true" presentation:background-objects-visible="true"/></style:style>
  {auto}
 </office:automatic-styles>
 <office:master-styles>
  <style:master-page style:name="Default" style:page-layout-name="PM1" draw:style-name="dp1"/>
 </office:master-styles>
 <office:body><office:presentation>
{pages}
 </office:presentation></office:body>
</office:document>
"""


# --------------------------------------------------------------------------- convert + validate

def convert(sources: list[Path], outdir: Path) -> None:
    soffice = shutil.which("soffice") or shutil.which("libreoffice")
    if not soffice:
        sys.exit("LibreOffice (soffice) blev ikke fundet i PATH")
    for src in sources:
        target = outdir / (src.stem + ".pdf")
        for stale in (target, outdir / f".~lock.{target.name}#"):
            stale.unlink(missing_ok=True)  # never validate a PDF from an earlier run
        with tempfile.TemporaryDirectory(prefix="folio-lo-") as prof:
            cmd = [soffice, "--headless", "--norestore", f"-env:UserInstallation=file://{prof}",
                   "--convert-to", "pdf", "--outdir", str(outdir), str(src)]
            try:
                res = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
            except subprocess.TimeoutExpired:
                sys.exit(f"soffice hang ved konvertering af {src.name} (layout-løkke?)")
            if res.returncode != 0 or not target.exists():
                print(res.stdout, res.stderr, file=sys.stderr)
                sys.exit(f"soffice fejlede for {src.name} ({res.returncode})")


def summarize(path: Path) -> dict:
    doc = pymupdf.open(path)
    s = dict(pages=doc.page_count, drawings=0, pages_with_drawings=0, images=0, words=0, blocks=0)
    texts = []
    for page in doc:
        d = len(page.get_drawings())
        s["drawings"] += d
        s["pages_with_drawings"] += d > 0
        s["images"] += len(page.get_images(full=True))
        s["words"] += len(page.get_text("words"))
        s["blocks"] += sum(1 for b in page.get_text("blocks") if b[6] == 0)
        texts.append(page.get_text())
    s["texts"] = texts
    doc.close()
    return s


def validate(outdir: Path) -> None:
    specs = {"aarsrapport.pdf": (45, 70), "strategi.pdf": (12, 16)}
    for name, (lo, hi) in specs.items():
        p = outdir / name
        assert p.exists(), f"{p} mangler"
        s = summarize(p)
        texts = s.pop("texts")
        print(f"{name}: " + ", ".join(f"{k}={v}" for k, v in s.items()))
        assert lo <= s["pages"] <= hi, f"{name}: {s['pages']} sider (forventet {lo}-{hi})"
        assert s["pages_with_drawings"] >= 0.8 * s["pages"], f"{name}: for få sider med grafik"
        assert s["images"] > 0, f"{name}: ingen billeder"
        assert any("Side 1 af" in t for t in texts), f"{name}: 'Side 1 af' mangler"
        if name == "aarsrapport.pdf":
            assert HEADER_TEXT in texts[1] and HEADER_TEXT in texts[-1], "sidehoved mangler"
            assert HEADER_TEXT not in texts[0], "sidehoved på forsiden"
            assert f"Side 2 af {s['pages']}" in texts[1], "sidefod 'Side N af M' forkert"
            assert all(t.count("Side ") >= 1 for t in texts), "sidefod mangler på en side"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("outdir", nargs="?", default=DEFAULT_OUT, help="mappe til PDF-filerne")
    ap.add_argument("--keep-sources", action="store_true",
                    help="behold .fodt/.fodp-kilderne i output-mappen")
    ap.add_argument("--preview", metavar="DIR", help="gem PNG-forhåndsvisninger (60 dpi) af alle sider")
    args = ap.parse_args(argv)
    out = Path(args.outdir)
    out.mkdir(parents=True, exist_ok=True)
    t0 = time.time()
    with tempfile.TemporaryDirectory(prefix="folio-corpus-") as tmp:
        src_dir = out if args.keep_sources else Path(tmp)
        rep = src_dir / "aarsrapport.fodt"
        rep.write_text(Report().build(), encoding="utf-8")
        deck = src_dir / "strategi.fodp"
        deck.write_text(Deck().build(), encoding="utf-8")
        convert([rep, deck], out)
    print(f"Konverteret på {time.time() - t0:.1f} s -> {out}")
    validate(out)
    if args.preview:
        pv = Path(args.preview)
        pv.mkdir(parents=True, exist_ok=True)
        for name in ("aarsrapport", "strategi"):
            with pymupdf.open(out / f"{name}.pdf") as doc:
                for page in doc:
                    page.get_pixmap(dpi=60).save(pv / f"{name}-{page.number + 1:02d}.png")
        print(f"Forhåndsvisninger: {pv}")
    print(f"Færdig på {time.time() - t0:.1f} s")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
