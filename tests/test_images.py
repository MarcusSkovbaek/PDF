import io

import pymupdf
import pytest

from pdfstudio.core.document import DocumentError
from pdfstudio.core.images import (
    SUPPORTED_IMAGE_EXTENSIONS,
    ImagePageOptions,
    images_to_pdf,
    is_image_file,
)
from tests.conftest import make_png

MM = 72 / 25.4


def open_pdf(data: bytes) -> pymupdf.Document:
    return pymupdf.open("pdf", data)


def two_tone_png(width: int = 200, height: int = 100) -> bytes:
    """Left half blue, right half red."""
    pix = pymupdf.Pixmap(pymupdf.csRGB, pymupdf.IRect(0, 0, width, height), False)
    pix.set_rect(pymupdf.IRect(0, 0, width // 2, height), (0, 0, 255))
    pix.set_rect(pymupdf.IRect(width // 2, 0, width, height), (255, 0, 0))
    return pix.tobytes("png")


def transparent_png() -> bytes:
    """100x50: left half opaque green, right half fully transparent."""
    pix = pymupdf.Pixmap(pymupdf.csRGB, pymupdf.IRect(0, 0, 100, 50), True)
    pix.set_rect(pix.irect, (0, 0, 0, 0))
    pix.set_rect(pymupdf.IRect(0, 0, 50, 50), (0, 160, 0, 255))
    return pix.tobytes("png")


def image_bbox(page: pymupdf.Page) -> pymupdf.Rect:
    infos = page.get_image_info()
    assert len(infos) == 1
    return pymupdf.Rect(infos[0]["bbox"])


def close(a: pymupdf.Rect, b: pymupdf.Rect, tol: float = 0.6) -> bool:
    return all(abs(x - y) <= tol for x, y in zip(a, b))


def test_supported_extensions():
    for ext in (".png", ".jpg", ".jpeg", ".bmp", ".gif", ".tif", ".tiff"):
        assert ext in SUPPORTED_IMAGE_EXTENSIONS
    assert all(e == e.lower() and e.startswith(".") for e in SUPPORTED_IMAGE_EXTENSIONS)
    assert is_image_file("C:/x/Skærmbillede.PNG")
    assert not is_image_file("doc.pdf")


def test_fit_page_is_image_size_at_96_dpi(png_path):
    doc = open_pdf(images_to_pdf([png_path, make_png(96, 192)]))
    assert doc.page_count == 2
    assert doc[0].rect == pymupdf.Rect(0, 0, 240, 150)  # 320x200 px
    assert doc[1].rect == pymupdf.Rect(0, 0, 72, 144)
    assert close(image_bbox(doc[0]), doc[0].rect)


def test_fit_with_margin():
    doc = open_pdf(images_to_pdf([make_png(96, 96)], ImagePageOptions(margin_mm=10)))
    page = doc[0]
    assert abs(page.rect.width - (72 + 20 * MM)) < 0.01
    assert close(image_bbox(page), pymupdf.Rect(10 * MM, 10 * MM, 10 * MM + 72, 10 * MM + 72))


def test_png_is_lossless_and_crisp():
    data = two_tone_png(64, 32)
    doc = open_pdf(images_to_pdf([data]))
    # rendering at 96 dpi reproduces the screenshot pixel for pixel
    pix = doc[0].get_pixmap(matrix=pymupdf.Matrix(96 / 72, 96 / 72))
    original = pymupdf.Pixmap(data)
    assert (pix.width, pix.height) == (64, 32)
    assert pix.samples == original.samples
    # stored losslessly (no DCT/JPEG recompression)
    xref = doc.get_page_images(0, full=True)[0][0]
    assert doc.xref_get_key(xref, "Filter")[1] != "/DCTDecode"


def test_jpeg_passthrough_without_recompression():
    jpg = pymupdf.Pixmap(two_tone_png()).tobytes("jpg", jpg_quality=90)
    doc = open_pdf(images_to_pdf([jpg], ImagePageOptions(page_size="A4")))
    xref = doc.get_page_images(0, full=True)[0][0]
    assert doc.xref_get_key(xref, "Filter")[1] == "/DCTDecode"
    assert doc.xref_stream_raw(xref) == jpg


@pytest.mark.parametrize(
    "size, expected",
    [("A4", (595, 842)), ("a3", (842, 1191)), ("A5", (420, 595)), ("Letter", (612, 792)), ("legal", (612, 1008))],
)
def test_paper_sizes_portrait_image(size, expected):
    doc = open_pdf(images_to_pdf([make_png(100, 200)], ImagePageOptions(page_size=size)))
    assert (doc[0].rect.width, doc[0].rect.height) == expected


def test_orientation_auto_and_forced():
    wide, tall = make_png(400, 200), make_png(200, 400)
    doc = open_pdf(images_to_pdf([wide, tall], ImagePageOptions(page_size="A4")))
    assert (doc[0].rect.width, doc[0].rect.height) == (842, 595)  # auto -> landscape
    assert (doc[1].rect.width, doc[1].rect.height) == (595, 842)
    doc = open_pdf(images_to_pdf([wide], ImagePageOptions(page_size="A4", orientation="portrait")))
    assert (doc[0].rect.width, doc[0].rect.height) == (595, 842)
    doc = open_pdf(images_to_pdf([tall], ImagePageOptions(page_size="A4", orientation="landscape")))
    assert (doc[0].rect.width, doc[0].rect.height) == (842, 595)


def test_contain_centres_and_keeps_aspect():
    doc = open_pdf(images_to_pdf([make_png(400, 200)], ImagePageOptions(page_size="A4", orientation="portrait", margin_mm=10)))
    page = doc[0]
    m = 10 * MM
    box_w = 595 - 2 * m
    h = box_w / 2
    y0 = (842 - h) / 2
    assert close(image_bbox(page), pymupdf.Rect(m, y0, 595 - m, y0 + h))


def test_stretch_fills_box():
    doc = open_pdf(images_to_pdf([make_png(400, 200)], ImagePageOptions(page_size="A5", orientation="portrait", scale="stretch", margin_mm=5)))
    m = 5 * MM
    assert close(image_bbox(doc[0]), pymupdf.Rect(m, m, 420 - m, 595 - m))


def test_cover_fills_box_and_crops():
    # wide two-tone image on a portrait page: only the centre is visible
    opts = ImagePageOptions(page_size="A4", orientation="portrait", scale="cover", margin_mm=10)
    doc = open_pdf(images_to_pdf([two_tone_png(200, 100)], opts))
    page = doc[0]
    m = 10 * MM
    pix = page.get_pixmap()
    # margin stays white (overflow is clipped to the content box)
    assert pix.pixel(int(m / 2), 421) == (255, 255, 255)
    assert pix.pixel(int(595 - m / 2), 421) == (255, 255, 255)
    assert pix.pixel(297, int(m / 2)) == (255, 255, 255)
    # content box fully covered: blue left of centre, red right of centre
    assert pix.pixel(int(m) + 3, int(m) + 3)[2] > 200
    assert pix.pixel(int(595 - m) - 3, int(842 - m) - 3)[0] > 200
    assert pix.pixel(int(m) + 3, int(842 - m) - 3) != (255, 255, 255)


def test_transparent_png_shows_white_page_and_keeps_alpha():
    doc = open_pdf(images_to_pdf([transparent_png()]))
    page = doc[0]
    pix = page.get_pixmap()
    assert pix.pixel(10, 10) == (0, 160, 0)
    assert pix.pixel(65, 20) == (255, 255, 255)
    # alpha kept as soft mask, not flattened onto a colour
    img = doc.get_page_images(0, full=True)[0]
    assert img[1] != 0  # smask xref


def test_bytes_and_paths_mixed(png_path):
    with open(png_path, "rb") as fh:
        raw = fh.read()
    doc = open_pdf(images_to_pdf([raw, png_path, bytearray(raw)]))
    assert doc.page_count == 3


def test_exif_orientation_is_respected():
    PIL = pytest.importorskip("PIL.Image")
    im = PIL.new("RGB", (200, 100), (255, 0, 0))
    im.paste((0, 0, 255), (0, 0, 100, 100))  # left half blue
    exif = PIL.Exif()
    exif[0x0112] = 6  # rotate 90 CW for display
    buf = io.BytesIO()
    im.save(buf, "JPEG", exif=exif.tobytes(), quality=95)
    doc = open_pdf(images_to_pdf([buf.getvalue()]))
    page = doc[0]
    assert (page.rect.width, page.rect.height) == (75, 150)  # 100x200 px displayed
    pix = page.get_pixmap()
    top, bottom = pix.pixel(37, 10), pix.pixel(37, 140)
    assert top[2] > 200 and top[0] < 60      # blue on top after rotating CW
    assert bottom[0] > 200 and bottom[2] < 60
    # JPEG data still passed through
    xref = doc.get_page_images(0, full=True)[0][0]
    assert doc.xref_get_key(xref, "Filter")[1] == "/DCTDecode"


def test_webp_via_pillow_when_available():
    PIL = pytest.importorskip("PIL.Image")
    if ".webp" not in SUPPORTED_IMAGE_EXTENSIONS:
        pytest.skip("Pillow without WebP support")
    buf = io.BytesIO()
    PIL.new("RGB", (40, 20), (0, 200, 0)).save(buf, "WEBP", lossless=True)
    doc = open_pdf(images_to_pdf([buf.getvalue()]))
    assert doc[0].rect == pymupdf.Rect(0, 0, 30, 15)


def test_other_formats_bmp_gif_tiff():
    pix = pymupdf.Pixmap(two_tone_png(30, 30))
    sources = [pix.tobytes("pnm"), pix.tobytes("pam")]
    pil = None
    try:
        from PIL import Image as pil
    except ImportError:
        pass
    if pil:
        for fmt in ("BMP", "GIF", "TIFF"):
            buf = io.BytesIO()
            pil.open(io.BytesIO(two_tone_png(30, 30))).save(buf, fmt)
            sources.append(buf.getvalue())
    doc = open_pdf(images_to_pdf(sources))
    assert doc.page_count == len(sources)
    assert all(p.rect == pymupdf.Rect(0, 0, 22.5, 22.5) for p in doc)


@pytest.mark.parametrize(
    "opts",
    [
        ImagePageOptions(page_size="B5"),
        ImagePageOptions(orientation="sideways"),
        ImagePageOptions(scale="zoom"),
        ImagePageOptions(margin_mm=-1),
        ImagePageOptions(page_size="A5", margin_mm=200),  # margins eat the page
    ],
)
def test_bad_options(opts):
    with pytest.raises(ValueError):
        images_to_pdf([make_png()], opts)


def test_errors_name_the_file(tmp_path):
    bad = tmp_path / "ødelagt.png"
    bad.write_bytes(b"\x89PNG\r\n\x1a\ngarbage")
    with pytest.raises(DocumentError, match="ødelagt.png"):
        images_to_pdf([make_png(), str(bad)])
    with pytest.raises(DocumentError, match="mangler.jpg"):
        images_to_pdf([str(tmp_path / "mangler.jpg")])
    with pytest.raises(DocumentError):
        images_to_pdf([b""])
    with pytest.raises(ValueError):
        images_to_pdf([])


def test_works_without_pillow(monkeypatch):
    from pdfstudio.core import images

    monkeypatch.setattr(images, "_PILImage", None)
    monkeypatch.setattr(images, "_PILImageOps", None)
    assert ".webp" not in images._supported_extensions()
    doc = open_pdf(images.images_to_pdf([transparent_png(), make_png()]))
    assert doc.page_count == 2
    with pytest.raises(DocumentError):
        images.images_to_pdf([b"RIFF\x00\x00\x00\x00WEBPVP8 garbage"])
