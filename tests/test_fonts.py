import shutil

import pymupdf
import pytest

from pdfstudio.core import fonts as F
from pdfstudio.core.fonts import (
    FontChoice,
    SpanStyle,
    available_families,
    clean_font_name,
    family_key,
    font_covers,
    qt_family_hint,
    resolve_font,
    style_from_span,
)
from tests.conftest import SANS_TTF, SERIF_TTF, make_sample_pdf

DANISH = "Blåbærgrød ÆØÅ æøå"


@pytest.fixture
def doc():
    d = pymupdf.open("pdf", make_sample_pdf())
    yield d
    d.close()


@pytest.fixture
def subset_doc():
    d = pymupdf.open("pdf", make_sample_pdf())
    d.subset_fonts()  # MuPDF's own subsetter: drops unused glyphs + cmap
    out = pymupdf.open("pdf", d.tobytes(garbage=3, deflate=True))
    d.close()
    yield out
    out.close()


# -- names & styles ---------------------------------------------------------
def test_clean_and_family_key():
    assert clean_font_name("ABCDEF+ArialMT") == "ArialMT"
    assert family_key("Arial-BoldMT") == "arial"
    assert family_key("ABCDEF+TimesNewRomanPS-BoldItalicMT") == "timesnewroman"
    assert family_key("TimesNewRomanPSMT") == "timesnewroman"
    assert family_key("Times-Roman") == "times"
    assert family_key("Times New Roman") == "timesnewroman"
    assert family_key("Calibri,Bold") == "calibri"
    assert family_key("LiberationSans-Bold") == "liberationsans"


@pytest.mark.parametrize(
    "name, flags, bold, italic, serif, mono",
    [
        ("Helvetica", 0, False, False, False, False),
        ("Times-Roman", 4, False, False, True, False),
        ("Times-BoldItalic", 0, True, True, True, False),
        ("ABCDEF+Arial-BoldMT", 0, True, False, False, False),
        ("Calibri-Italic", 0, False, True, False, False),
        ("Helvetica-Oblique", 0, False, True, False, False),
        ("Montserrat-SemiBold", 0, True, False, False, False),
        ("CourierNewPSMT", 0, False, False, False, True),
        ("Georgia", 0, False, False, True, False),
        ("SomeFont", 4 | 16, True, False, True, False),
        ("SomeFont", 2 | 8, False, True, False, True),
    ],
)
def test_style_from_span(name, flags, bold, italic, serif, mono):
    st = style_from_span(name, 12, 0x3366CC, flags)
    assert st.font == clean_font_name(name)
    assert (st.bold, st.italic, st.serif, st.mono) == (bold, italic, serif, mono)
    assert st.size == 12
    assert st.color == pytest.approx((0x33 / 255, 0x66 / 255, 0xCC / 255), abs=1e-3)


# -- resolution ---------------------------------------------------------------
def test_base14_resolves_to_builtin_clone(doc):
    st = style_from_span("Times-Roman", 12, 0, 4)
    choice = resolve_font(doc, 0, st, "Hej " + DANISH)
    assert isinstance(choice, FontChoice)
    assert choice.source == "builtin"
    assert "Times" in choice.font.name
    assert font_covers(choice.font, DANISH)

    st = style_from_span("Helvetica-Bold", 12, 0, 16)
    choice = resolve_font(doc, 0, st, "x")
    assert choice.font.name == "Helvetica-Bold"


@pytest.mark.skipif(not SANS_TTF, reason="no sans TTF available")
def test_embedded_full_font_is_used(doc):
    st = style_from_span("LiberationSans", 14, 0x0033CC, 0)
    choice = resolve_font(doc, 0, st, "Helt ny tekst " + DANISH)
    assert choice.source == "embedded"
    assert font_covers(choice.font, DANISH)


@pytest.mark.skipif(not SANS_TTF, reason="no sans TTF available")
def test_subset_font_used_for_known_glyphs_only(subset_doc):
    page = subset_doc[0]
    span = next(s for b in page.get_text("dict")["blocks"] for ln in b.get("lines", ())
                for s in ln["spans"] if "Hello" in s["text"])
    st = style_from_span(span["font"], span["size"], span["color"], span["flags"])
    known = "World Hello Indlejret"
    choice = resolve_font(subset_doc, 0, st, known)
    assert choice.source == "embedded"
    # the subset has no glyphs for Danish letters -> must fall back, and the
    # fallback must really contain them
    choice = resolve_font(subset_doc, 0, st, "ÆØÅ æøå")
    assert choice.source in ("system", "builtin")
    assert font_covers(choice.font, "ÆØÅ æøå")
    assert not choice.font.is_serif


def test_builtin_classification_when_unknown(doc):
    st = SpanStyle(font="TotallyUnknownFont-BoldItalic", size=10, color=(0, 0, 0), bold=True, italic=True,
                   serif=True)
    choice = resolve_font(doc, 0, st, "abc")
    assert choice.source in ("builtin", "system")
    if choice.source == "builtin":
        assert choice.font.name == "Times-BoldItalic"


def test_fallback_covers_cjk_and_cyrillic(doc):
    st = SpanStyle(font="Helvetica", size=10)
    for text in ("Привет мир", "中文测试"):
        choice = resolve_font(doc, 0, st, text)
        assert font_covers(choice.font, text), text


def test_resolve_without_document():
    choice = resolve_font(None, None, SpanStyle(font="Courier", size=10, mono=True), "code()")
    assert choice.font.is_monospaced or "Courier" in choice.font.name


# -- system index ---------------------------------------------------------------
@pytest.mark.skipif(not (SANS_TTF and SERIF_TTF), reason="no TTFs available")
def test_index_reads_names(tmp_path):
    shutil.copy(SANS_TTF, tmp_path / "a.ttf")
    shutil.copy(SERIF_TTF, tmp_path / "b.ttf")
    (tmp_path / "junk.ttf").write_bytes(b"not a font at all")
    idx = F._build_index([str(tmp_path)])
    assert len(idx.faces) == 2
    for face in idx.faces:
        assert face.ps and not face.bold and not face.italic
        assert pymupdf.Font(fontfile=face.path).name.startswith(face.family)
        assert idx.by_ps[F._norm(face.ps)] is face
    serif_face = next(f for f in idx.faces if f.path.endswith("b.ttf"))
    assert serif_face.serif is True


def test_ttc_face_extraction():
    idx = F.system_font_index()
    ttc = [f for f in idx.faces if f.path.lower().endswith(".ttc") and f.index > 0]
    if not ttc:
        pytest.skip("no font collections installed")
    face = ttc[0]
    font = F._load_face(face)
    assert font is not None
    assert font.glyph_count > 0


def test_index_is_cached_and_fast():
    a = F.system_font_index()
    b = F.system_font_index()
    assert a is b


def test_available_families():
    fams = available_families()
    for name in ("Helvetica", "Times", "Courier"):
        assert name in fams
    assert len(fams) == len(set(fams))
    assert fams == sorted(fams, key=str.casefold)


@pytest.mark.parametrize(
    "font, expected",
    [
        ("Helvetica", "Arial"),
        ("ABCDEF+ArialMT", "Arial"),
        ("Times-Roman", "Times New Roman"),
        ("TimesNewRomanPS-BoldMT", "Times New Roman"),
        ("LiberationSerif", "Times New Roman"),
        ("Courier", "Courier New"),
        ("Calibri-Bold", "Calibri"),
        ("Carlito", "Calibri"),
        ("SegoeUI", "Segoe UI"),
    ],
)
def test_qt_family_hint(font, expected):
    assert qt_family_hint(SpanStyle(font=font, size=10)) == expected


def test_qt_family_hint_fallbacks():
    assert qt_family_hint(SpanStyle(font="Xyzzy", size=10, serif=True)) == "Times New Roman"
    assert qt_family_hint(SpanStyle(font="Xyzzy", size=10, mono=True)) == "Courier New"
    assert qt_family_hint(SpanStyle(font="Xyzzy", size=10)) == "Arial"


def test_sfnt_cmap_rebuild_roundtrip():
    if not SANS_TTF:
        pytest.skip("no TTF")
    with open(SANS_TTF, "rb") as fh:
        buf = fh.read()
    orig = pymupdf.Font(fontbuffer=buf)
    mapping = {ord(c): orig.has_glyph(ord(c)) for c in "Hello æ"}
    patched = F._sfnt_with_cmap(buf, mapping)
    font = pymupdf.Font(fontbuffer=patched)
    for u, gid in mapping.items():
        assert font.has_glyph(u) == gid
    assert font.has_glyph(ord("Z")) == 0
