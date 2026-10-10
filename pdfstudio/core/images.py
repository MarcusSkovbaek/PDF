"""Images (typically screenshots) -> PDF.

Quality matters more than size here: images are embedded *losslessly*.
Every image is first turned into a one-page PDF by MuPDF's own image
document handler (``convert_to_pdf``), which

* passes JPEG data through untouched (no recompression),
* stores PNG/BMP/GIF/TIFF pixels Flate-compressed (lossless),
* keeps an alpha channel as a soft mask, so transparent areas show the
  white page underneath, and
* applies the JPEG EXIF orientation via the placement matrix.

That page is then placed on the target page with ``show_pdf_page``, which
also gives us clipping for the "cover" scale mode for free. Formats MuPDF
cannot decode (e.g. WebP) are converted to PNG with Pillow when it is
installed; Pillow is optional.
"""

from __future__ import annotations

import io
import os
from dataclasses import dataclass
from typing import Iterator, Sequence

import pymupdf

from ..i18n import tr
from .document import DocumentError

try:  # Pillow is optional – only used for formats MuPDF cannot read.
    from PIL import Image as _PILImage
    from PIL import ImageOps as _PILImageOps
except Exception:  # pragma: no cover - depends on the environment
    _PILImage = None
    _PILImageOps = None

#: Screen resolution used to size "fit" pages: 1 image pixel == 0.75 pt.
SCREEN_DPI = 96.0
MM = 72.0 / 25.4

PAGE_SIZES: dict[str, tuple[float, float]] = {
    "A3": (842.0, 1191.0),
    "A4": (595.0, 842.0),
    "A5": (420.0, 595.0),
    "Letter": (612.0, 792.0),
    "Legal": (612.0, 1008.0),
}
PAGE_SIZE_CHOICES = ("fit",) + tuple(PAGE_SIZES)
ORIENTATIONS = ("auto", "portrait", "landscape")
SCALE_MODES = ("contain", "cover", "stretch")

_MUPDF_EXTENSIONS = (
    ".png", ".jpg", ".jpeg", ".jpe", ".jfif", ".bmp", ".gif", ".tif", ".tiff",
    ".jxr", ".jp2", ".jpx", ".pnm", ".pbm", ".pgm", ".ppm", ".pam", ".psd",
)
_PILLOW_EXTENSIONS = (".webp",)


def _supported_extensions() -> tuple[str, ...]:
    exts = list(_MUPDF_EXTENSIONS)
    if _PILImage is not None:
        registered = _PILImage.registered_extensions()
        exts += [e for e in _PILLOW_EXTENSIONS if e in registered]
    return tuple(exts)


#: Lower-case file extensions (with dot) accepted by :func:`images_to_pdf`.
SUPPORTED_IMAGE_EXTENSIONS: tuple[str, ...] = _supported_extensions()


def is_image_file(path: str) -> bool:
    return os.path.splitext(path)[1].lower() in SUPPORTED_IMAGE_EXTENSIONS


@dataclass
class ImagePageOptions:
    """How images are laid out on pages.

    ``page_size``  "fit" (page == image at 96 dpi) or a paper name
                   ("A4", "A3", "A5", "Letter", "Legal"; case-insensitive).
    ``orientation`` for paper sizes: "auto" picks landscape for wide images.
    ``margin_mm``  white margin on every side.
    ``scale``      "contain" (whole image, centred), "cover" (fill the area,
                   cropping the overflow) or "stretch" (fill, ignore aspect).
    """

    page_size: str = "fit"
    orientation: str = "auto"
    margin_mm: float = 0.0
    scale: str = "contain"

    def validated(self) -> "ImagePageOptions":
        """Return a normalized copy; raises ``ValueError`` on bad values."""
        size = self.page_size
        if size.lower() == "fit":
            size = "fit"
        else:
            match = {k.lower(): k for k in PAGE_SIZES}.get(size.lower())
            if match is None:
                raise ValueError(tr("core.err.image_option", name="page_size", value=self.page_size))
            size = match
        orientation = self.orientation.lower()
        if orientation not in ORIENTATIONS:
            raise ValueError(tr("core.err.image_option", name="orientation", value=self.orientation))
        scale = self.scale.lower()
        if scale not in SCALE_MODES:
            raise ValueError(tr("core.err.image_option", name="scale", value=self.scale))
        margin = float(self.margin_mm)
        if margin < 0 or margin != margin:  # negative or NaN
            raise ValueError(tr("core.err.image_option", name="margin_mm", value=self.margin_mm))
        return ImagePageOptions(size, orientation, margin, scale)


# -- loading -------------------------------------------------------------------
def _read_source(image: str | bytes) -> tuple[bytes, str]:
    """Return ``(data, display_name)`` for a path or raw bytes."""
    if isinstance(image, (bytes, bytearray, memoryview)):
        return bytes(image), tr("core.unnamed_source")
    name = os.path.basename(image)
    try:
        with open(image, "rb") as fh:
            return fh.read(), name
    except OSError as exc:
        raise DocumentError(tr("core.err.open_failed", name=name, reason=exc.strerror or exc)) from exc


def _pillow_to_png(data: bytes) -> bytes | None:
    """Decode with Pillow (if available) and re-encode losslessly as PNG."""
    if _PILImage is None:
        return None
    try:
        with _PILImage.open(io.BytesIO(data)) as im:
            im = _PILImageOps.exif_transpose(im)
            if im.mode not in ("RGB", "RGBA", "L", "LA"):
                im = im.convert("RGBA" if "A" in im.getbands() or "transparency" in im.info else "RGB")
            out = io.BytesIO()
            im.save(out, "PNG")
            return out.getvalue()
    except Exception:
        return None


def image_as_pdf(data: bytes, name: str = "") -> pymupdf.Document:
    """Open image bytes as an in-memory PDF (one page per frame).

    The returned document's pages hold the image losslessly with EXIF
    orientation applied. Raises :class:`DocumentError` mentioning ``name``.
    """
    name = name or tr("core.unnamed_source")
    if not data:
        raise DocumentError(tr("core.err.open_failed", name=name, reason=tr("core.err.empty_file")))
    for candidate in (data, None):
        if candidate is None:
            candidate = _pillow_to_png(data)
            if candidate is None:
                break
        try:
            with pymupdf.open(stream=candidate) as img_doc:
                if img_doc.is_pdf or not img_doc.page_count:
                    raise ValueError("not an image")
                pdf_bytes = img_doc.convert_to_pdf()
            return pymupdf.open("pdf", pdf_bytes)
        except Exception:  # MuPDF raises several types; try Pillow next
            continue
    raise DocumentError(tr("core.err.open_failed", name=name, reason=tr("core.err.unsupported")))


def _pixel_size(src: pymupdf.Document, index: int) -> tuple[int, int]:
    """Pixel size of the image on ``src`` page ``index``, orientation applied."""
    page = src[index]
    images = src.get_page_images(index)
    if not images:  # pragma: no cover - convert_to_pdf always embeds one image
        r = page.rect
        return round(r.width * SCREEN_DPI / 72), round(r.height * SCREEN_DPI / 72)
    w, h = images[0][2], images[0][3]
    r = page.rect
    if w != h and (r.width > r.height) != (w > h):  # EXIF rotated by 90/270
        w, h = h, w
    return w, h


# -- layout --------------------------------------------------------------------
def _page_size(opts: ImagePageOptions, px_w: int, px_h: int) -> tuple[float, float]:
    margin = opts.margin_mm * MM
    if opts.page_size == "fit":
        return px_w * 72.0 / SCREEN_DPI + 2 * margin, px_h * 72.0 / SCREEN_DPI + 2 * margin
    w, h = PAGE_SIZES[opts.page_size]  # portrait
    landscape = opts.orientation == "landscape" or (opts.orientation == "auto" and px_w > px_h)
    return (h, w) if landscape else (w, h)


def _placement(box: pymupdf.Rect, img_w: float, img_h: float, scale: str) -> tuple[pymupdf.Rect, pymupdf.Rect | None]:
    """Where to draw an image of aspect ``img_w:img_h`` inside ``box``.

    Returns ``(target_rect, clip)`` where ``clip`` is the part of the source
    page (in its own coordinates, size ``img_w`` x ``img_h``) to show, or
    ``None`` for the whole image.
    """
    if scale == "stretch":
        return pymupdf.Rect(box), None
    fx, fy = box.width / img_w, box.height / img_h
    if scale == "contain":
        f = min(fx, fy)
        w, h = img_w * f, img_h * f
        x0 = box.x0 + (box.width - w) / 2
        y0 = box.y0 + (box.height - h) / 2
        return pymupdf.Rect(x0, y0, x0 + w, y0 + h), None
    # cover: scale to fill, show the centred part of the image that fits
    f = max(fx, fy)
    vis_w, vis_h = box.width / f, box.height / f
    cx0 = (img_w - vis_w) / 2
    cy0 = (img_h - vis_h) / 2
    return pymupdf.Rect(box), pymupdf.Rect(cx0, cy0, cx0 + vis_w, cy0 + vis_h)


def _iter_frames(images: Sequence[str | bytes]) -> Iterator[tuple[pymupdf.Document, int]]:
    for image in images:
        data, name = _read_source(image)
        src = image_as_pdf(data, name)
        try:
            for i in range(src.page_count):
                yield src, i
        finally:
            src.close()


def images_to_pdf(images: Sequence[str | bytes], options: ImagePageOptions | None = None) -> bytes:
    """Create a PDF with one page per image (per frame for multi-page TIFFs).

    ``images`` are file paths or raw image bytes. Raises ``ValueError`` for
    bad options or an empty list and :class:`DocumentError` (naming the file)
    for unreadable images.
    """
    if not images:
        raise ValueError(tr("core.err.no_images"))
    opts = (options or ImagePageOptions()).validated()
    margin = opts.margin_mm * MM
    out = pymupdf.open()
    try:
        for src, index in _iter_frames(images):
            px_w, px_h = _pixel_size(src, index)
            width, height = _page_size(opts, px_w, px_h)
            page = out.new_page(width=width, height=height)
            box = pymupdf.Rect(margin, margin, width - margin, height - margin)
            if box.is_empty:
                raise ValueError(tr("core.err.image_option", name="margin_mm", value=opts.margin_mm))
            src_rect = src[index].rect
            target, clip = _placement(box, src_rect.width, src_rect.height, opts.scale)
            page.show_pdf_page(target, src, index, keep_proportion=False, clip=clip)
        return out.tobytes(garbage=3, deflate=True)
    finally:
        out.close()


__all__ = [
    "ImagePageOptions",
    "PAGE_SIZES",
    "PAGE_SIZE_CHOICES",
    "ORIENTATIONS",
    "SCALE_MODES",
    "SCREEN_DPI",
    "SUPPORTED_IMAGE_EXTENSIONS",
    "image_as_pdf",
    "images_to_pdf",
    "is_image_file",
]
