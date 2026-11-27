"""Core: images in the page content (``pdfstudio.core.objects``)."""

from __future__ import annotations

import re

import pymupdf
import pytest

from pdfstudio.core import PdfDocument
from pdfstudio.core import objects as ob
from tests.conftest import filled_pixmap, make_png, make_sample_pdf

RED = (220, 30, 30)
GREEN = (20, 180, 60)
BLUE = (30, 60, 220)


# ---------------------------------------------------------------------------
# fixtures / helpers
# ---------------------------------------------------------------------------
def png(rgb=RED, w=40, h=30) -> bytes:
    return make_png(w, h, rgb)


def jpeg(rgb=GREEN, w=64, h=48) -> bytes:
    return filled_pixmap(w, h, rgb).tobytes("jpeg", jpg_quality=95)


def alpha_png(w=30, h=20) -> bytes:
    return filled_pixmap(w, h, (0, 0, 255, 140), alpha=True).tobytes("png")


def build(content: bytes, *, images: dict[str, bytes] | None = None, forms: dict[str, tuple[bytes, dict]] | None = None,
          rotation: int = 0, cropbox=None, pages: int = 1) -> tuple[PdfDocument, dict[str, int]]:
    """A document whose first page has exactly ``content`` as its content
    stream. ``images`` maps resource names to image data; ``forms`` maps
    names to (stream, {resource name: image name}). Font /F1 = Helvetica."""
    doc = pymupdf.open()
    for _ in range(pages):
        doc.new_page(width=595, height=842)
    page = doc[0]
    xrefs: dict[str, int] = {}
    for name, data in (images or {}).items():
        xrefs[name] = page.insert_image(pymupdf.Rect(0, 0, 10, 10), stream=data)
    font = page.insert_font(fontname="helv")
    xobj = {n: x for n, x in xrefs.items()}
    for name, (stream, res) in (forms or {}).items():
        fx = doc.get_new_xref()
        inner = " ".join(f"/{k} {xrefs[v]} 0 R" for k, v in res.items())
        doc.update_object(fx, f"<</Type/XObject/Subtype/Form/BBox[0 0 595 842]"
                              f"/Resources<</XObject<<{inner}>>/Font<</F1 {font} 0 R>>>>>>")
        doc.update_stream(fx, stream)
        xobj[name] = fx
        xrefs[name] = fx
    entries = " ".join(f"/{n} {x} 0 R" for n, x in xobj.items())
    doc.xref_set_key(page.xref, "Resources", f"<</XObject<<{entries}>>/Font<</F1 {font} 0 R>>>>")
    cx = doc.get_new_xref()
    doc.update_object(cx, "<<>>")
    doc.update_stream(cx, content)
    doc.xref_set_key(page.xref, "Contents", f"{cx} 0 R")
    if cropbox is not None:
        page.set_cropbox(pymupdf.Rect(cropbox))
    if rotation:
        page.set_rotation(rotation)
    data = doc.tobytes()
    doc.close()
    return PdfDocument.from_bytes(data), xrefs


def mupdf_rects(doc, page: int) -> list[pymupdf.Rect]:
    """MuPDF's own idea of where the images are (display space)."""
    pg = doc[page]
    return [pymupdf.Rect(i["bbox"]) * pg.rotation_matrix for i in pg.get_image_info()]


def assert_rect(a, b, tol=0.6) -> None:
    a, b = pymupdf.Rect(a), pymupdf.Rect(b)
    assert all(abs(x - y) <= tol for x, y in zip(a, b)), (a, b)


def assert_matches_mupdf(doc, page: int) -> list[ob.ImageObject]:
    objs = ob.list_images(doc, page)
    ref = mupdf_rects(doc, page)
    assert len(objs) == len(ref), (objs, ref)
    for o, r in zip(objs, ref):
        assert_rect(o.rect, r)
    return objs


def color_at(doc, page: int, x: float, y: float) -> tuple[int, int, int]:
    pix = doc[page].get_pixmap(matrix=pymupdf.Matrix(1, 1), annots=False)
    return tuple(pix.pixel(int(x), int(y))[:3])


def near(c, rgb, tol=40) -> bool:
    return all(abs(a - b) <= tol for a, b in zip(c, rgb))


def text_and_drawings(doc, page: int):
    pg = doc[page]
    return pg.get_text("text"), [(d["rect"], d.get("fill")) for d in pg.get_drawings()]


IMG = b"q 160 0 0 120 72 600 cm /Im1 Do Q\n"
TEXT = b"BT /F1 12 Tf 72 760 Td (Kvartalsrapport) Tj ET\n"
RECT = b"q 0 0 1 rg 300 300 100 50 re f Q\n"


# ---------------------------------------------------------------------------
# listing
# ---------------------------------------------------------------------------
def test_list_sample_document():
    pdf = PdfDocument.from_bytes(make_sample_pdf())
    assert ob.list_images(pdf.doc, 0) == []
    (img,) = assert_matches_mupdf(pdf.doc, 1)
    assert (img.width, img.height) == (40, 30)
    assert img.xref > 0 and not img.inline and not img.in_form and img.editable
    assert_rect(img.rect, (72, 500, 232, 620))
    assert img.rotation == 0 and not img.flipped and img.axis_aligned
    assert img.size_pt == pytest.approx((160, 120))
    assert img.dpi == pytest.approx((18, 18))


@pytest.mark.parametrize("rotation", [0, 90, 180, 270])
def test_rotated_pages_and_cropbox(rotation):
    pdf, _ = build(TEXT + IMG, images={"Im1": png()}, rotation=rotation, cropbox=(30, 40, 560, 800))
    (img,) = assert_matches_mupdf(pdf.doc, 0)
    # the display matrix agrees with MuPDF's transform (unit square, top-left origin)
    info = pdf.doc[0].get_image_info()[0]
    want = pymupdf.Matrix(info["transform"]) * pdf.doc[0].rotation_matrix
    assert all(abs(a - b) < 0.01 for a, b in zip(img.matrix, want))
    assert img.rotation == pytest.approx(rotation)
    # moving by a display vector moves it by exactly that on screen
    moved = ob.move_image(pdf, 0, img, 25, -40)
    assert_rect(moved.rect, img.rect + (25, -40, 25, -40))
    assert_matches_mupdf(pdf.doc, 0)
    resized = ob.set_image_rect(pdf, 0, moved, (100, 120, 220, 300))
    assert_rect(resized.rect, (100, 120, 220, 300))
    assert_matches_mupdf(pdf.doc, 0)
    assert resized.rotation == pytest.approx(rotation)


def test_same_image_placed_twice_are_independent():
    content = TEXT + IMG + b"q 80 0 0 60 300 200 cm /Im1 Do Q\n"
    pdf, x = build(content, images={"Im1": png()})
    a, b = assert_matches_mupdf(pdf.doc, 0)
    assert a.xref == b.xref == x["Im1"] and (a.index, b.index) == (0, 1)
    ob.move_image(pdf, 0, b, -100, 50)
    a2, b2 = ob.list_images(pdf.doc, 0)
    assert_rect(a2.rect, a.rect)
    assert_rect(b2.rect, b.rect + (-100, 50, -100, 50))
    ob.delete_image(pdf, 0, a2)
    (left,) = assert_matches_mupdf(pdf.doc, 0)
    assert_rect(left.rect, b2.rect) and left.xref == x["Im1"]
    # the image data itself is still there (one XObject, used once)
    assert len(pdf.doc.get_page_images(0)) == 1


def test_rotated_and_flipped_placement():
    # rotated 30°, and a mirrored copy
    c, s = 0.8660254, 0.5
    content = (f"q {100 * c:.4f} {100 * s:.4f} {-60 * s:.4f} {60 * c:.4f} 200 400 cm /Im1 Do Q\n"
               "q -120 0 0 80 450 600 cm /Im1 Do Q\n").encode()
    pdf, _ = build(content, images={"Im1": png()})
    rot, mir = assert_matches_mupdf(pdf.doc, 0)
    assert rot.rotation == pytest.approx(330, abs=0.01)     # 30° counter-clockwise on screen
    assert not rot.axis_aligned and not rot.flipped
    assert mir.flipped and mir.axis_aligned
    assert rot.size_pt == pytest.approx((100, 60), abs=0.01)
    # hit-test follows the rotated outline, not the bounding box
    corner = pymupdf.Point(rot.rect.x0 + 2, rot.rect.y0 + 2)
    assert ob.image_at(pdf.doc, 0, corner) is None
    centre = (rot.quad.ul + rot.quad.lr) * 0.5
    assert ob.image_at(pdf.doc, 0, centre).index == 0
    # moving keeps the orientation exactly
    moved = ob.move_image(pdf, 0, rot, 30, 30)
    assert moved.rotation == pytest.approx(rot.rotation, abs=1e-4)
    assert moved.size_pt == pytest.approx(rot.size_pt, abs=1e-3)
    m2 = ob.move_image(pdf, 0, mir, -10, 0)
    assert m2.flipped
    assert_matches_mupdf(pdf.doc, 0)


def test_image_at_topmost_and_tolerance():
    content = b"q 300 0 0 300 50 400 cm /Im1 Do Q\nq 100 0 0 100 100 500 cm /Im2 Do Q\n"
    pdf, x = build(content, images={"Im1": png(RED), "Im2": png(BLUE)})
    big, small = ob.list_images(pdf.doc, 0)
    assert ob.image_at(pdf.doc, 0, (150, 290)).xref == x["Im2"]      # overlap: topmost wins
    assert ob.image_at(pdf.doc, 0, (300, 200)).xref == x["Im1"]
    assert ob.image_at(pdf.doc, 0, (500, 100)) is None
    assert ob.image_at(pdf.doc, 0, (352, 200)) is None
    assert ob.image_at(pdf.doc, 0, (352, 200), tolerance=3).xref == x["Im1"]


# ---------------------------------------------------------------------------
# edits keep everything else intact
# ---------------------------------------------------------------------------
def test_move_leaves_other_content_untouched():
    content = TEXT + RECT + IMG + b"q 1 0 0 rg 50 50 40 40 re f Q\n"
    pdf, _ = build(content, images={"Im1": png()})
    pg = pdf.doc[0]
    pg.add_highlight_annot(pymupdf.Rect(72, 70, 180, 90))
    before = text_and_drawings(pdf.doc, 0)
    annots = [(a.type[0], a.rect) for a in pdf.doc[0].annots()]
    data_before = pdf.doc[0].read_contents()
    (img,) = ob.list_images(pdf.doc, 0)
    ob.move_image(pdf, 0, img, 40, 10)
    assert text_and_drawings(pdf.doc, 0) == before
    assert [(a.type[0], a.rect) for a in pdf.doc[0].annots()] == annots
    after = pdf.doc[0].read_contents()
    # byte-exact outside the one changed cm
    assert after.replace(b"160 0 0 120 112 590 cm", b"160 0 0 120 72 600 cm") == data_before
    # pixels: the image is at the new place, old place shows the page
    assert near(color_at(pdf.doc, 0, 112 + 80, 842 - 590 - 60), RED)
    assert near(color_at(pdf.doc, 0, 72 + 10, 842 - 600 - 110), (255, 255, 255))


def test_repeated_moves_do_not_grow_the_file():
    pdf, _ = build(TEXT + IMG + RECT, images={"Im1": jpeg()})
    img = ob.list_images(pdf.doc, 0)[0]
    img = ob.move_image(pdf, 0, img, 1, 1)
    size1 = len(pdf.to_bytes())
    stream1 = len(pdf.doc[0].read_contents())
    for k in range(30):
        img = ob.move_image(pdf, 0, img, 3 if k % 2 else -2, 1.5)
        img = ob.set_image_rect(pdf, 0, img, img.rect + (0, 0, 2, 1.5))
    assert len(pdf.doc[0].read_contents()) <= stream1 + 12
    assert len(pdf.to_bytes()) <= size1 + 64
    assert len(pdf.doc.get_page_images(0)) == 1
    # one undo step per edit
    rect = img.rect
    pdf.undo()
    assert ob.list_images(pdf.doc, 0)[0].rect != rect


def test_uncovered_do_is_wrapped_once():
    # no q/Q around the image: the cm before it stays in force afterwards
    content = b"q 2 0 0 2 0 0 cm 50 0 0 40 20 300 cm /Im1 Do 0 1 0 rg 0 0 10 10 re f Q\n"
    pdf, _ = build(content, images={"Im1": png()})
    before = text_and_drawings(pdf.doc, 0)[1]
    (img,) = assert_matches_mupdf(pdf.doc, 0)
    img = ob.move_image(pdf, 0, img, 10, 0)
    data = pdf.doc[0].read_contents()
    assert data.count(b" cm") == 3
    for _ in range(5):
        img = ob.move_image(pdf, 0, img, 10, 0)
    assert pdf.doc[0].read_contents().count(b" cm") == 3       # flat: rewrites its own cm
    assert text_and_drawings(pdf.doc, 0)[1] == before          # the green square did not move
    assert_matches_mupdf(pdf.doc, 0)


def test_clip_moves_with_image():
    # Word/LibreOffice style: the image is cropped by a clip in its own block
    content = TEXT + b"q 72 600 80 120 re W n q 160 0 0 120 72 600 cm /Im1 Do Q Q\n"
    pdf, _ = build(content, images={"Im1": png(RED)})
    (img,) = ob.list_images(pdf.doc, 0)
    assert near(color_at(pdf.doc, 0, 100, 842 - 660), RED)
    ob.move_image(pdf, 0, img, 200, 0)
    assert near(color_at(pdf.doc, 0, 300, 842 - 660), RED)          # visible at the new place
    assert near(color_at(pdf.doc, 0, 100, 842 - 660), (255, 255, 255))
    assert pdf.doc[0].read_contents().startswith(TEXT)


def test_libreoffice_clip_block_with_marked_content():
    # LibreOffice: the /Figure sequence ends inside the clip block of a cropped image
    content = (b"q\n/Figure <</MCID 0>> BDC\nQ q 72 600 80 120 re\nW* n\nq 160 0 0 120 72 600 cm\n/Im1 Do Q\n"
               b"EMC\n/P <</MCID 1>> BDC\nQ q BT /F1 12 Tf 72 760 Td (Kvartalsrapport) Tj ET\nEMC\nQ\n")
    pdf, _ = build(content, images={"Im1": png(RED)})
    (img,) = assert_matches_mupdf(pdf.doc, 0)
    moved = ob.move_image(pdf, 0, img, 200, 0)                     # the crop moves along
    assert near(color_at(pdf.doc, 0, 300, 842 - 660), RED)
    assert near(color_at(pdf.doc, 0, 100, 842 - 660), (255, 255, 255))
    pdf.undo()
    # a tall replacement is fitted into the full frame and not cropped by the old clip
    new = ob.replace_image(pdf, 0, img, png(GREEN, 100, 300))
    assert_rect(new.rect, (132, 122, 172, 242))
    assert near(color_at(pdf.doc, 0, 165, 842 - 660), GREEN)
    data = pdf.doc[0].read_contents()
    assert data.count(b"BDC") == 2 and data.count(b"EMC") == 2
    assert "Kvartalsrapport" in pdf.doc[0].get_text()
    ob.delete_image(pdf, 0, new)
    assert ob.list_images(pdf.doc, 0) == []
    assert moved is not None


def test_marked_content_is_respected():
    # a BDC that straddles the q…Q block must not be cut by delete
    content = (b"/Artifact BMC q 160 0 0 120 72 600 cm /Im1 Do Q EMC\n"
               b"/P <</MCID 0>> BDC q 50 0 0 50 300 300 cm /Im1 Do Q\n" + TEXT + b"EMC\n")
    pdf, _ = build(content, images={"Im1": png()})
    a, b = ob.list_images(pdf.doc, 0)
    ob.delete_image(pdf, 0, b)
    data = pdf.doc[0].read_contents()
    assert data.count(b"BDC") == 1 and data.count(b"EMC") == 2 and data.count(b"BMC") == 1
    ob.delete_image(pdf, 0, ob.list_images(pdf.doc, 0)[0])
    assert ob.list_images(pdf.doc, 0) == []
    assert "Kvartalsrapport" in pdf.doc[0].get_text()


def test_strings_and_dicts_with_operators_inside():
    tricky = (b"BT /F1 9 Tf 10 10 Td (q Q cm \\) (Do) /Im1 Do) Tj [(a) -20 (b)] TJ <0BDC0A> Tj ET"
              b" % q Q 1 0 0 1 9 9 cm /Im1 Do\n/Q BMC /Span <</ActualText (x>) Q) /Alt true>> BDC EMC EMC\n")
    content = tricky + IMG
    # the deliberately odd stream makes MuPDF print syntax errors while it
    # interprets it – expected here, so keep them out of the test output
    shown = pymupdf.TOOLS.mupdf_display_errors()
    pymupdf.TOOLS.mupdf_display_errors(False)
    try:
        pdf, _ = build(content, images={"Im1": png()})
        (img,) = assert_matches_mupdf(pdf.doc, 0)
        ob.move_image(pdf, 0, img, 5, 5)
        assert pdf.doc[0].read_contents().startswith(tricky)
        assert_matches_mupdf(pdf.doc, 0)
    finally:
        pymupdf.TOOLS.mupdf_display_errors(shown)
        pymupdf.TOOLS.reset_mupdf_warnings()


# ---------------------------------------------------------------------------
# forms, inline images, shared streams, inherited resources
# ---------------------------------------------------------------------------
def test_image_only_form_is_moved_as_a_whole():
    form = b"q 100 0 0 80 0 0 cm /Img Do Q"
    content = TEXT + b"q 1 0 0 1 200 300 cm /Fm1 Do Q\n"
    pdf, x = build(content, images={"Im1": png()}, forms={"Fm1": (form, {"Img": "Im1"})})
    (img,) = assert_matches_mupdf(pdf.doc, 0)
    assert img.in_form and img.xref == x["Im1"]
    ob.move_image(pdf, 0, img, -50, -20)
    assert pdf.doc.xref_stream(x["Fm1"]) == form                  # the form itself is untouched
    (moved,) = assert_matches_mupdf(pdf.doc, 0)
    assert_rect(moved.rect, img.rect + (-50, -20, -50, -20))
    ob.delete_image(pdf, 0, moved)
    assert ob.list_images(pdf.doc, 0) == [] and "Kvartalsrapport" in pdf.doc[0].get_text()


def test_image_in_shared_template_form_is_edited_privately():
    form = b"BT /F1 10 Tf 40 40 Td (Brevpapir) Tj ET q 60 0 0 40 450 760 cm /Logo Do Q"
    content = b"q /Fm1 Do Q\n" + TEXT
    pdf, x = build(content, images={"Im1": png()}, forms={"Fm1": (form, {"Logo": "Im1"})}, pages=2)
    # the second page uses the same template
    doc = pdf.doc
    doc.xref_set_key(doc.page_xref(1), "Resources", doc.xref_get_key(doc.page_xref(0), "Resources")[1])
    cx = doc.get_new_xref()
    doc.update_object(cx, "<<>>")
    doc.update_stream(cx, b"q /Fm1 Do Q\n")
    doc.xref_set_key(doc.page_xref(1), "Contents", f"{cx} 0 R")
    (logo,) = assert_matches_mupdf(doc, 0)
    assert logo.in_form
    ob.move_image(pdf, 0, logo, -400, 0)
    (moved,) = assert_matches_mupdf(pdf.doc, 0)
    assert_rect(moved.rect, logo.rect + (-400, 0, -400, 0))
    assert "Brevpapir" in pdf.doc[0].get_text()
    assert_rect(ob.list_images(pdf.doc, 1)[0].rect, logo.rect)     # page 2 keeps the original
    assert pdf.doc.xref_stream(x["Fm1"]) == form
    ob.move_image(pdf, 0, moved, 0, 30)                            # editing again works
    assert_rect(ob.list_images(pdf.doc, 0)[0].rect, logo.rect + (-400, 30, -400, 30))
    ob.delete_image(pdf, 0, ob.list_images(pdf.doc, 0)[0])
    assert ob.list_images(pdf.doc, 0) == [] and "Brevpapir" in pdf.doc[0].get_text()
    assert len(ob.list_images(pdf.doc, 1)) == 1


def test_inline_image():
    # 2x2 RGB inline image, binary data containing "EI" bytes
    raw = bytes([0x45, 0x49, 0x20, 255, 0, 0, 0, 255, 0, 0, 0, 255])
    content = TEXT + b"q 100 0 0 100 300 300 cm BI /W 2 /H 2 /CS /RGB /BPC 8 ID " + raw + b"\nEI Q\n" + RECT
    pdf, _ = build(content)
    before = text_and_drawings(pdf.doc, 0)
    (img,) = assert_matches_mupdf(pdf.doc, 0)
    assert img.inline and img.xref == 0 and (img.width, img.height) == (2, 2)
    moved = ob.move_image(pdf, 0, img, -100, 0)
    assert_rect(moved.rect, img.rect + (-100, 0, -100, 0))
    assert text_and_drawings(pdf.doc, 0) == before
    data, ext = ob.extract_image_file(pdf.doc, moved)
    assert ext and len(data) > 20
    assert ob.image_pixmap(pdf.doc, moved).width == 2
    ob.delete_image(pdf, 0, moved)
    assert ob.list_images(pdf.doc, 0) == []
    assert text_and_drawings(pdf.doc, 0) == before


def test_shared_content_stream_is_not_changed_on_other_page():
    pdf, _ = build(TEXT + IMG, images={"Im1": png()}, pages=2)
    doc = pdf.doc
    doc.xref_set_key(doc.page_xref(1), "Resources", doc.xref_get_key(doc.page_xref(0), "Resources")[1])
    doc.xref_set_key(doc.page_xref(1), "Contents", doc.xref_get_key(doc.page_xref(0), "Contents")[1])
    (img,) = ob.list_images(doc, 0)
    ob.move_image(pdf, 0, img, 50, 0)
    assert_rect(ob.list_images(pdf.doc, 1)[0].rect, img.rect)
    assert_rect(ob.list_images(pdf.doc, 0)[0].rect, img.rect + (50, 0, 50, 0))


def test_inherited_resources_survive_replace():
    pdf, x = build(TEXT + IMG, images={"Im1": png()})
    doc = pdf.doc
    pages_root = int(doc.xref_get_key(doc.page_xref(0), "Parent")[1].split()[0])
    doc.xref_set_key(pages_root, "Resources", doc.xref_get_key(doc.page_xref(0), "Resources")[1])
    doc.xref_set_key(doc.page_xref(0), "Resources", "null")
    pdf2 = PdfDocument.from_bytes(doc.tobytes())
    assert "Kvartalsrapport" in pdf2.doc[0].get_text()
    (img,) = ob.list_images(pdf2.doc, 0)
    new = ob.replace_image(pdf2, 0, img, png(BLUE, 40, 30))
    assert new.xref != x["Im1"]
    assert "Kvartalsrapport" in pdf2.doc[0].get_text()             # fonts still resolve
    assert near(color_at(pdf2.doc, 0, 150, 842 - 660), BLUE)


# ---------------------------------------------------------------------------
# replace / delete / extract
# ---------------------------------------------------------------------------
def test_replace_keeps_stacking_order_and_fits():
    content = IMG + b"q 0 0 1 rg 100 640 40 40 re f Q\n" + b"q 80 0 0 60 300 200 cm /Im1 Do Q\n"
    pdf, x = build(content, images={"Im1": png(RED, 40, 30)})
    first, second = ob.list_images(pdf.doc, 0)
    new = ob.replace_image(pdf, 0, first, png(GREEN, 100, 300), keep_aspect=True)
    assert new.index == 0 and (new.width, new.height) == (100, 300)
    # fitted into 160x120: 40x120, centred
    assert_rect(new.rect, (72 + 60, 842 - 720, 72 + 100, 842 - 600))
    # the blue square drawn after it still covers it
    assert near(color_at(pdf.doc, 0, 135, 842 - 645), (0, 0, 255))
    assert near(color_at(pdf.doc, 0, 140, 842 - 610), GREEN)
    # the other placement still shows the old image
    a, b = ob.list_images(pdf.doc, 0)
    assert b.xref == x["Im1"] and a.xref != x["Im1"]
    stretched = ob.replace_image(pdf, 0, b, png(GREEN, 100, 300), keep_aspect=False)
    assert_rect(stretched.rect, b.rect)
    assert stretched.xref == a.xref                                # same image data is reused
    pdf.undo()
    pdf.undo()
    assert [o.xref for o in ob.list_images(pdf.doc, 0)] == [x["Im1"], x["Im1"]]


def test_replace_rotated_placement_keeps_orientation_and_alpha():
    content = b"q 0 -160 120 0 72 600 cm /Im1 Do Q\n"    # rotated 90°
    pdf, _ = build(content, images={"Im1": png()}, rotation=90)
    (img,) = ob.list_images(pdf.doc, 0)
    new = ob.replace_image(pdf, 0, img, alpha_png(), keep_aspect=False)
    assert new.has_alpha and new.smask
    assert new.rotation == pytest.approx(img.rotation)
    assert_rect(new.rect, img.rect)
    data, ext = ob.extract_image_file(pdf.doc, new)
    assert ext == "png"
    pix = pymupdf.Pixmap(data)
    assert pix.alpha and (pix.width, pix.height) == (30, 20)


def test_replace_from_path_and_errors(tmp_path):
    pdf, _ = build(IMG, images={"Im1": png()})
    path = tmp_path / "ny.jpg"
    path.write_bytes(jpeg())
    (img,) = ob.list_images(pdf.doc, 0)
    new = ob.replace_image(pdf, 0, img, str(path))
    assert new.filter == "DCTDecode"
    assert ob.extract_image_file(pdf.doc, new) == (path.read_bytes(), "jpeg") or \
        ob.extract_image_file(pdf.doc, new) == (path.read_bytes(), "jpg")
    with pytest.raises(Exception):
        ob.replace_image(pdf, 0, new, b"not an image")
    assert ob.list_images(pdf.doc, 0)[0].xref == new.xref          # failed edit rolled back
    with pytest.raises(ValueError):
        ob.set_image_rect(pdf, 0, new, (10, 10, 10, 50))
    with pytest.raises(LookupError):
        ob.delete_image(pdf, 0, 99999)


def test_extract_jpeg_is_byte_identical_and_by_xref():
    data = jpeg()
    pdf, x = build(IMG, images={"Im1": data})
    assert ob.extract_image(pdf.doc, x["Im1"]) == data
    (img,) = ob.list_images(pdf.doc, 0)
    raw, ext = ob.extract_image_file(pdf.doc, img)
    assert raw == data and ext in ("jpg", "jpeg")
    pix = ob.image_pixmap(pdf.doc, img, max_side=32)
    assert max(pix.width, pix.height) == 32


def test_delete_and_undo():
    pdf, x = build(TEXT + IMG + RECT, images={"Im1": png()})
    before = text_and_drawings(pdf.doc, 0)
    ob.delete_image(pdf, 0, x["Im1"])
    assert ob.list_images(pdf.doc, 0) == []
    assert text_and_drawings(pdf.doc, 0) == before
    assert pdf.history.undo_label == "Slet billede"
    pdf.undo()
    assert len(ob.list_images(pdf.doc, 0)) == 1


def test_rotate_and_flip():
    pdf, _ = build(IMG, images={"Im1": png()})
    (img,) = ob.list_images(pdf.doc, 0)
    r = ob.rotate_image(pdf, 0, img, 90)
    assert r.rotation == pytest.approx(90)
    assert r.rect.width == pytest.approx(img.rect.height) and r.rect.height == pytest.approx(img.rect.width)
    c0 = ((img.rect.x0 + img.rect.x1) / 2, (img.rect.y0 + img.rect.y1) / 2)
    c1 = ((r.rect.x0 + r.rect.x1) / 2, (r.rect.y0 + r.rect.y1) / 2)
    assert c0 == pytest.approx(c1)
    f = ob.flip_image(pdf, 0, r)
    assert f.flipped
    assert_rect(f.rect, r.rect)
    assert_matches_mupdf(pdf.doc, 0)


def test_add_image_then_edit_round_trip(tmp_path):
    """Images placed by "Tilføj billede" (core.insert.add_image) on a rotated page."""
    from pdfstudio.core import insert

    pdf = PdfDocument.from_bytes(make_sample_pdf())
    insert.add_image(pdf, 2, (100, 100, 300, 250), png(BLUE, 200, 150))
    (img,) = assert_matches_mupdf(pdf.doc, 2)
    assert_rect(img.rect, (100, 100, 300, 250))
    assert img.rotation == pytest.approx(0)            # upright on screen although the page is rotated
    moved = ob.set_image_rect(pdf, 2, img, (400, 300, 600, 450))
    assert_rect(moved.rect, (400, 300, 600, 450))
    path = tmp_path / "out.pdf"
    pdf.save(str(path))
    again = PdfDocument.open(str(path))
    (saved,) = assert_matches_mupdf(again.doc, 2)
    assert_rect(saved.rect, (400, 300, 600, 450))
    assert "Roteret side" in again.doc[2].get_text()
    again.close()


def test_numbers_never_use_exponents():
    assert ob._num(1e-7) == b"0.0000001"
    assert ob._num(-0.0) == b"0"
    assert ob._num(123456.5) == b"123456.5"
    assert not re.search(rb"e", ob._fmt(pymupdf.Matrix(1e-6, 2e8, 0, 1, 3.25, -7)))


def test_nudge_burst_is_one_undo_step():
    pdf, _ = build(TEXT + IMG, images={"Im1": png()})
    (img,) = ob.list_images(pdf.doc, 0)
    start = img.rect
    img = ob.move_image(pdf, 0, img, 1, 0)
    for _ in range(4):
        img = ob.move_image(pdf, 0, img, 1, 0, merge=True)
    assert_rect(img.rect, start + (5, 0, 5, 0))
    assert len(pdf.history._undo) == 1
    pdf.undo()
    assert_rect(ob.list_images(pdf.doc, 0)[0].rect, start)
    # merge never folds into a different kind of step
    ob.delete_image(pdf, 0, ob.list_images(pdf.doc, 0)[0])
    pdf.undo()
    ob.move_image(pdf, 0, ob.list_images(pdf.doc, 0)[0], 0, 1, merge=True)
    assert pdf.history.undo_label == "Flyt billede" and len(pdf.history._undo) == 1


def test_hit_test_on_cached_listing_and_lifted_render():
    content = TEXT + b"q 0 0 1 rg 60 560 300 200 re f Q\n" + IMG
    pdf, _ = build(content, images={"Im1": png(RED)})
    objs = ob.list_images(pdf.doc, 0)
    assert ob.hit_test(objs, (150, 200)).index == 0
    assert ob.hit_test(objs, (500, 500)) is None
    before = pdf.doc[0].read_contents()
    pix = ob.render_without_image(pdf.doc, 0, objs[0], 1.0, clip=objs[0].rect)
    # what lies beneath the image: the blue rectangle
    assert near(pix.pixel(pix.width // 2, pix.height // 2)[:3], (0, 0, 255))
    assert (pix.x, pix.y) == (72, 122)
    assert pdf.doc[0].read_contents() == before and not pdf.history.can_undo
