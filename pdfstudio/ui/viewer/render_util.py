"""Bridges ``pdfstudio.core.render`` to Qt images (with a local fallback)."""

from __future__ import annotations

import pymupdf
from PySide6.QtGui import QImage, QPixmap

try:  # prefer the shared core implementation
    from ...core.render import page_display_size as _core_size
    from ...core.render import render_page as _core_render
except Exception:  # pragma: no cover - module not available yet
    _core_render = None
    _core_size = None


def _fallback_render(doc, index, zoom, *, annots=True, clip=None, alpha=False):
    page = doc[index]
    if clip is not None:
        clip = pymupdf.Rect(clip) & page.rect
    return page.get_pixmap(matrix=pymupdf.Matrix(zoom, zoom), clip=clip, annots=annots, alpha=alpha)


def render_pixmap(doc, index: int, zoom: float, *, clip=None, annots: bool = True) -> pymupdf.Pixmap:
    if _core_render is not None:
        try:
            return _core_render(doc, index, zoom, annots=annots, clip=clip)
        except Exception:
            pass
    return _fallback_render(doc, index, zoom, annots=annots, clip=clip)


def page_size(doc, index: int) -> tuple[float, float]:
    if _core_size is not None:
        try:
            return _core_size(doc, index)
        except Exception:
            pass
    r = doc[index].rect
    return (r.width, r.height)


def to_qimage(pix: pymupdf.Pixmap) -> QImage:
    if pix.n == 1:
        fmt = QImage.Format.Format_Grayscale8
    elif pix.n == 3 and not pix.alpha:
        fmt = QImage.Format.Format_RGB888
    elif pix.n == 4 and pix.alpha:
        fmt = QImage.Format.Format_RGBA8888
    else:
        pix = pymupdf.Pixmap(pymupdf.csRGB, pix)
        fmt = QImage.Format.Format_RGB888
    img = QImage(pix.samples_mv, pix.width, pix.height, pix.stride, fmt)
    return img.copy()


def render_qimage(doc, index: int, zoom: float, *, clip=None, annots: bool = True) -> QImage:
    return to_qimage(render_pixmap(doc, index, zoom, clip=clip, annots=annots))


def render_qpixmap(doc, index: int, zoom: float, dpr: float = 1.0, *, clip=None, annots: bool = True) -> QPixmap:
    """Render at ``zoom * dpr`` device pixels; the pixmap's DPR is set."""
    img = render_qimage(doc, index, zoom * dpr, clip=clip, annots=annots)
    pm = QPixmap.fromImage(img)
    pm.setDevicePixelRatio(dpr)
    return pm
