import pymupdf
import pytest

from pdfstudio.core.render import (
    as_document,
    display_to_page_point,
    display_to_page_rect,
    page_display_size,
    page_to_display_point,
    page_to_display_rect,
    render_page,
)


def _dark_bbox(pix: pymupdf.Pixmap, threshold: int = 128):
    """Bounding box (pixels) of dark pixels, or None."""
    xs, ys = [], []
    samples, n, w = pix.samples, pix.n, pix.width
    for y in range(pix.height):
        row = y * w * n
        for x in range(w):
            if samples[row + x * n] < threshold:
                xs.append(x)
                ys.append(y)
    return (min(xs), min(ys), max(xs), max(ys)) if xs else None


def test_render_basic_size_and_alpha(sample_pdf):
    pix = render_page(sample_pdf.doc, 0, 2.0)
    assert isinstance(pix, pymupdf.Pixmap)
    assert (pix.width, pix.height) == (1190, 1684)
    assert pix.alpha == 0 and pix.n == 3
    pix = render_page(sample_pdf.doc, 0, 1.0, alpha=True)
    assert pix.alpha == 1


def test_render_accepts_pdfdocument(sample_pdf):
    assert as_document(sample_pdf) is sample_pdf.doc
    assert as_document(sample_pdf.doc) is sample_pdf.doc
    pix = render_page(sample_pdf, 1, 0.5)
    assert pix.width == round(595 * 0.5)


def test_rotated_page_renders_in_display_space(sample_pdf):
    assert page_display_size(sample_pdf.doc, 2) == (842.0, 595.0)
    assert page_display_size(sample_pdf, 0) == (595.0, 842.0)
    pix = render_page(sample_pdf.doc, 2, 1.0)
    assert (pix.width, pix.height) == (842, 595)


def test_clip_is_display_space_on_rotated_page(sample_pdf):
    page = sample_pdf.page(2)
    # where the text is in display space (it runs vertically on the rotated page)
    word = page_to_display_rect(page, page.search_for("Roteret")[0])
    full = render_page(sample_pdf.doc, 2, 1.0)
    bbox = _dark_bbox(full)
    assert bbox is not None
    assert word.x0 - 2 <= bbox[0] and bbox[2] <= word.x1 + 2  # text found where expected

    clip = word + (-5, -5, 5, 5)
    pix = render_page(sample_pdf.doc, 2, 2.0, clip=clip)
    assert abs(pix.width - clip.width * 2) <= 2
    assert abs(pix.height - clip.height * 2) <= 2
    assert (pix.x, pix.y) == (round(clip.x0 * 2), round(clip.y0 * 2))
    assert _dark_bbox(pix) is not None


def test_clip_outside_page_returns_blank(sample_pdf):
    pix = render_page(sample_pdf.doc, 0, 1.0, clip=(2000, 2000, 2100, 2100))
    assert pix.width >= 1 and _dark_bbox(pix) is None


def test_annots_flag(sample_pdf):
    with sample_pdf.edit("test"):
        page = sample_pdf.page(0)
        annot = page.add_rect_annot(pymupdf.Rect(300, 400, 400, 500))
        annot.set_colors(stroke=(0, 0, 0), fill=(0, 0, 0))
        annot.update()
    clip = (320, 420, 380, 480)
    with_annots = render_page(sample_pdf.doc, 0, 1.0, clip=clip)
    without = render_page(sample_pdf.doc, 0, 1.0, clip=clip, annots=False)
    assert _dark_bbox(with_annots) is not None
    assert _dark_bbox(without) is None


def test_bad_zoom(sample_pdf):
    with pytest.raises(ValueError):
        render_page(sample_pdf.doc, 0, 0)


def test_geometry_helpers_round_trip(sample_pdf):
    page = sample_pdf.page(2)
    p = pymupdf.Point(100, 50)
    assert page_to_display_point(page, display_to_page_point(page, p)) == p
    r = pymupdf.Rect(10, 20, 110, 70)
    back = page_to_display_rect(page, display_to_page_rect(page, r))
    assert back == r
    # on a 90 degree page a display-wide rect is tall in page space
    unrot = display_to_page_rect(page, r)
    assert unrot.width == r.height and unrot.height == r.width
