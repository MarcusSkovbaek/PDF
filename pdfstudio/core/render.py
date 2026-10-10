"""Rasterising pages and the display-space geometry helpers.

All coordinates handed to / returned from ``pdfstudio.core`` are in *display
space*: PDF points with the origin at the top-left of the page as it is shown,
i.e. with the page's ``/Rotate`` applied (the space of ``page.rect``).

PyMuPDF itself expects *unrotated* page coordinates for annotations, text
extraction, ``insert_text`` and similar APIs. The helpers below convert
between the two spaces; feature modules should use them instead of
re-implementing the matrix juggling.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Iterable, Union

import pymupdf

if TYPE_CHECKING:  # pragma: no cover
    from .document import PdfDocument

DocLike = Union[pymupdf.Document, "PdfDocument"]
PointLike = Union[pymupdf.Point, tuple[float, float]]
RectLike = Union[pymupdf.Rect, tuple[float, float, float, float]]


def as_document(doc: DocLike) -> pymupdf.Document:
    """Accept either a :class:`PdfDocument` or a raw :class:`pymupdf.Document`.

    Read-only core functions are documented as taking ``doc``; the UI may pass
    whichever it has at hand.
    """
    inner = getattr(doc, "doc", None)
    if isinstance(inner, pymupdf.Document):
        return inner
    return doc  # type: ignore[return-value]


# -- display <-> unrotated page space ----------------------------------------
def display_to_page_point(page: pymupdf.Page, point: PointLike) -> pymupdf.Point:
    """Display-space point -> unrotated page space."""
    return pymupdf.Point(point) * page.derotation_matrix


def page_to_display_point(page: pymupdf.Page, point: PointLike) -> pymupdf.Point:
    """Unrotated page-space point -> display space."""
    return pymupdf.Point(point) * page.rotation_matrix


def display_to_page_rect(page: pymupdf.Page, rect: RectLike) -> pymupdf.Rect:
    """Display-space rect -> unrotated page space (normalized)."""
    return (pymupdf.Rect(rect) * page.derotation_matrix).normalize()


def page_to_display_rect(page: pymupdf.Page, rect: RectLike) -> pymupdf.Rect:
    """Unrotated page-space rect -> display space (normalized)."""
    return (pymupdf.Rect(rect) * page.rotation_matrix).normalize()


def display_to_page_points(page: pymupdf.Page, points: Iterable[PointLike]) -> list[pymupdf.Point]:
    mat = page.derotation_matrix
    return [pymupdf.Point(p) * mat for p in points]


# -- rendering -----------------------------------------------------------------
def render_page(
    doc: DocLike,
    index: int,
    zoom: float,
    *,
    annots: bool = True,
    clip: RectLike | None = None,
    alpha: bool = False,
) -> pymupdf.Pixmap:
    """Render page ``index`` at ``zoom`` (1.0 == 72 dpi) to an RGB pixmap.

    ``clip`` is a display-space rectangle (points); only that part of the page
    is rendered and the pixmap's ``x``/``y`` hold its pixel offset within the
    full page rendering. ``annots=False`` hides annotations (useful for
    thumbnails of the "clean" document or for exporting).
    """
    if zoom <= 0:
        raise ValueError("zoom must be positive")
    page = as_document(doc)[index]
    matrix = pymupdf.Matrix(zoom, zoom)
    # PyMuPDF's ``clip`` is already interpreted in display space (it is
    # applied after the page's rotation), so it can be passed through as is.
    clip_rect = pymupdf.Rect(clip).normalize() & page.rect if clip is not None else None
    if clip_rect is not None and clip_rect.is_empty:
        # Nothing of the page is visible: return a 1x1 background pixmap
        # instead of letting MuPDF raise on an empty area.
        pix = pymupdf.Pixmap(pymupdf.csRGB, pymupdf.IRect(0, 0, 1, 1), alpha)
        pix.set_rect(pix.irect, (255, 255, 255, 0) if alpha else (255, 255, 255))
        return pix
    return page.get_pixmap(matrix=matrix, clip=clip_rect, alpha=alpha, annots=annots)


def page_display_size(doc: DocLike, index: int) -> tuple[float, float]:
    """Width and height of page ``index`` as displayed (rotation applied)."""
    rect = as_document(doc)[index].rect
    return (rect.width, rect.height)


__all__ = [
    "as_document",
    "display_to_page_point",
    "display_to_page_points",
    "display_to_page_rect",
    "page_display_size",
    "page_to_display_point",
    "page_to_display_rect",
    "render_page",
]
