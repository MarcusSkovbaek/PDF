"""Reduce the file size of a document ("Reducér filstørrelse").

Works on a copy – the open document is not changed – and returns the new
PDF bytes; the UI saves them as a new file. Images above the chosen
resolution are downsampled and recompressed (JPEG for photos, lossless
recompression otherwise), fonts are subset, unused and duplicate objects
removed and streams compressed (object streams included). Optionally
metadata and personal information (document info, XMP metadata, comment
authors) are removed. Password protection is kept.
"""

from __future__ import annotations

from dataclasses import dataclass

import pymupdf

from .document import PdfDocument, write_bytes

#: Image quality presets: name -> target resolution (dpi), JPEG quality.
QUALITY = {
    "high": (300, 85),
    "medium": (150, 75),
    "low": (96, 60),
}


@dataclass
class ReduceOptions:
    images: str | None = "medium"        # QUALITY key, or None to keep images as they are
    strip_metadata: bool = False         # document info + XMP metadata
    strip_authors: bool = False          # author (/T) of comments


@dataclass
class ReduceResult:
    data: bytes
    before: int
    after: int

    @property
    def saved_fraction(self) -> float:
        return 0.0 if not self.before else max(0.0, 1.0 - self.after / self.before)


def reduce_size(pdf: PdfDocument, options: ReduceOptions | None = None) -> ReduceResult:
    options = options or ReduceOptions()
    before = len(pdf.serialize(protect=False))
    work = pymupdf.open("pdf", write_bytes(pdf.doc))
    try:
        if options.images:
            dpi, quality = QUALITY[options.images]
            # only images clearly sharper than the target are touched
            work.rewrite_images(dpi_threshold=int(dpi * 1.25), dpi_target=dpi, quality=quality,
                                lossy=True, lossless=True, bitonal=False, color=True, gray=True)
        try:
            work.subset_fonts()
        except Exception:
            pass
        if options.strip_metadata:
            work.set_metadata({})
            try:
                work.del_xml_metadata()
            except Exception:
                pass
        if options.strip_authors:
            for page in work:
                for annot in page.annots():
                    work.xref_set_key(annot.xref, "T", "null")
        kw: dict = {}
        if pdf.security is not None:
            kw.update(pdf.security.save_kwargs())
        data = write_bytes(work, garbage=4, deflate=True, deflate_images=True, deflate_fonts=True,
                           use_objstms=True, **kw)
    finally:
        work.close()
    if pdf.security is None and len(data) > before:
        data = pdf.serialize(protect=False)  # never hand back a bigger file
    return ReduceResult(data, before, len(data))


__all__ = ["QUALITY", "ReduceOptions", "ReduceResult", "reduce_size"]
