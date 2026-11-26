"""PDF pages from what the clipboard holds (Qt-free).

The UI reads the system clipboard into a :class:`ClipContent`
(``ui/clipboard_source.py``); this module turns it into PDF pages:

* :func:`content_to_pdf` – a new PDF ("Ny PDF fra udklipsholder", U7–U9),
* :func:`insert_content` – new pages in an open document, one undo step
  ("Indsæt fra udklipsholder", U10–U13).

What can be used (U1–U6), in priority order when the clipboard holds several
kinds: Folio's own object clip → files copied in Explorer (PDFs, images) →
PDF data → image → formatted text (HTML) → plain text. When an image is used
although text is there too (Excel cells, a piece of a web page), the result
names ``alternative="text"`` so the UI can offer "Brug teksten i stedet".

Everything is local: nothing is ever fetched (U16) – images that copied HTML
only references on the web are left out.

API contract (binding – the UI codes against it; see
``docs/kravspec-udklipsholder.md``)::

    ClipContent(images, files, pdf, html, text, folio)   .kinds() .is_empty()
    ClipOptions(page_size="image"|"A4"|"Letter"|"match", margin_mm, text_page, text_style, prefer)
    SUPPORTED_FILE_EXTENSIONS ; usable_files(paths) -> (usable, skipped)
    chosen_kind(content, prefer=None) -> str | None
    content_to_pdf(content, options=None, *, passwords=None) -> ClipResult(data, kind, pages, skipped, alternative)
    text_to_pdf(text, *, page_size=A4, margin_mm=20.0, style=None) -> bytes
    html_to_pdf(html, *, page_size=A4, margin_mm=20.0, style=None) -> bytes
    insert_content(pdf, content, at, options=None, *, passwords=None)
        -> ClipInsert(pages, kind, skipped, alternative)        # ONE pdf.edit (STRUCTURE)
"""

from __future__ import annotations

import os
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field

from .fonts import SpanStyle
from .images import SUPPORTED_IMAGE_EXTENSIONS

#: kinds in priority order (U6)
KINDS = ("folio", "files", "pdf", "image", "html", "text")

#: A4 portrait in PDF points
A4 = (595.28, 841.89)

#: files that can be used when copied in Explorer (U2): PDFs + images.py's image types
SUPPORTED_FILE_EXTENSIONS: tuple[str, ...] = (".pdf",) + tuple(SUPPORTED_IMAGE_EXTENSIONS)


@dataclass
class ClipContent:
    """What the system clipboard holds, read by the UI.

    ``images``  encoded images (PNG preferred, lossless), in clipboard order
    ``files``   local file paths copied in Explorer, in their order
    ``pdf``     PDF data another program put on the clipboard
    ``html``    formatted text (an HTML document or fragment)
    ``text``    plain text
    ``folio``   Folio's own object clip (``pageobjects.Clip``) when it is the
                newest thing on the clipboard
    """

    images: list[bytes] = field(default_factory=list)
    files: list[str] = field(default_factory=list)
    pdf: bytes | None = None
    html: str | None = None
    text: str | None = None
    folio: object | None = None

    def kinds(self) -> list[str]:
        """The usable kinds present, in priority order (:data:`KINDS`)."""
        have = {
            "folio": self.folio is not None,
            "files": bool(usable_files(self.files)[0]),
            "pdf": bool(self.pdf),
            "image": bool(self.images),
            "html": bool(self.html and self.html.strip()),
            "text": bool(self.text and self.text.strip()),
        }
        return [k for k in KINDS if have[k]]

    def is_empty(self) -> bool:
        """True when nothing in it can become a page."""
        return not self.kinds()


@dataclass
class ClipOptions:
    """How pages are made.

    ``page_size``  for images: "image" (the page is the image at 96 dpi, no
                   margin – default for a new PDF, U9), "A4" / "Letter" (the
                   image contained, orientation following the image), or
                   "match" (the neighbour page's paper size, orientation
                   following the image – default for inserted pages, U12;
                   :func:`content_to_pdf` treats it as "A4")
    ``margin_mm``  margin around an image on a paper size (default 10 mm)
    ``text_page``  page size (w, h) in points for text; None = A4 portrait
                   (insert: the neighbour page's size)
    ``text_style`` body style for text; None = a readable default (Arial /
                   Helvetica 11 pt; insert: ``insert.document_text_style``)
    ``prefer``     force one kind (e.g. "text" for "Brug teksten i stedet");
                   None = priority order
    """

    page_size: str = "image"
    margin_mm: float = 10.0
    text_page: tuple[float, float] | None = None
    text_style: SpanStyle | None = None
    prefer: str | None = None


@dataclass
class ClipResult:
    """A new PDF made from the clipboard."""

    data: bytes
    kind: str                       # the kind used (one of KINDS)
    pages: int
    skipped: list[str] = field(default_factory=list)   # file names that could not be used
    alternative: str | None = None  # "text" when an image was used and text is there too


@dataclass
class ClipInsert:
    """Pages inserted from the clipboard (display indices after the insert)."""

    pages: list[int]
    kind: str
    skipped: list[str] = field(default_factory=list)
    alternative: str | None = None


def usable_files(paths: Sequence[str]) -> tuple[list[str], list[str]]:
    """Split copied file paths into usable ones (existing PDFs and images, in
    order) and skipped ones (file names, for the message)."""
    usable: list[str] = []
    skipped: list[str] = []
    for p in paths or ():
        if os.path.isfile(p) and os.path.splitext(p)[1].lower() in SUPPORTED_FILE_EXTENSIONS:
            usable.append(p)
        else:
            skipped.append(os.path.basename(p.rstrip("/\\")) or p)
    return usable, skipped


def chosen_kind(content: ClipContent, prefer: str | None = None) -> str | None:
    """The kind :func:`content_to_pdf` would use (``prefer`` when present)."""
    kinds = content.kinds()
    if prefer in kinds:
        return prefer
    return kinds[0] if kinds else None


def text_to_pdf(text: str, *, page_size: tuple[float, float] = A4, margin_mm: float = 20.0,
                style: SpanStyle | None = None) -> bytes:
    """Plain text set on pages (U3): paragraphs, empty lines and indentation
    kept, long text flowing onto new pages, an embedded font covering every
    character (U15)."""
    raise NotImplementedError


def html_to_pdf(html: str, *, page_size: tuple[float, float] = A4, margin_mm: float = 20.0,
                style: SpanStyle | None = None) -> bytes:
    """Formatted text (U4): headings, bold/italic/underline, colours, lists
    and simple tables; nothing is fetched (U16)."""
    raise NotImplementedError


def content_to_pdf(content: ClipContent, options: ClipOptions | None = None, *,
                   passwords: Callable[[str], str | None] | None = None) -> ClipResult:
    """A new PDF from the clipboard content. ``passwords(file_name)`` is asked
    for password-protected PDFs (None skips the file). Raises ``ValueError``
    when nothing usable is there."""
    raise NotImplementedError


def insert_content(pdf, content: ClipContent, at: int, options: ClipOptions | None = None, *,
                   passwords: Callable[[str], str | None] | None = None) -> ClipInsert:
    """Insert the clipboard content as new pages before index ``at`` (one
    ``pdf.edit`` – one undo step). Default ``page_size`` "match": images on
    the neighbour page's paper size; text in the neighbour page's size with
    the document's body style. Raises ``ValueError`` when nothing usable is
    there."""
    raise NotImplementedError


__all__ = ["A4", "KINDS", "SUPPORTED_FILE_EXTENSIONS", "ClipContent", "ClipInsert", "ClipOptions", "ClipResult",
           "chosen_kind", "content_to_pdf", "html_to_pdf", "insert_content", "text_to_pdf", "usable_files"]
