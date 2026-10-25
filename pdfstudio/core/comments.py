"""Comment threads – Acrobat-compatible replies, review states and quotes.

A *comment* is any top-level annotation (highlight, note, shape, …). Its
thread is stored the standard way (ISO 32000, 12.5.6.2), exactly like
Adobe Acrobat does it, so threads round-trip between Folio and Acrobat:

* **Replies** are ``/Text`` annotations with ``/IRT`` → parent (reply type
  ``/RT /R``, the default). A reply to a reply points at that reply; the
  thread is shown flat in date order.
* **Review state** (*Accepteret / Afvist / Annulleret / Fuldført / Ingen*)
  is a hidden ``/Text`` annotation with ``/IRT`` → parent,
  ``/StateModel (Review)`` and ``/State (Accepted)`` …; the newest one
  counts. **Løst** (Word's "resolve") is Acrobat's check mark: state model
  ``Marked`` with ``Marked`` / ``Unmarked``.
* **Replace text** is Acrobat's group of a strike-out (``/IT
  /StrikeOutTextEdit``, carrying the replacement text) and a caret with
  ``/IRT`` → strike-out and ``/RT /Group``; **insert text** is a caret.

Annotations with ``/IRT`` never show up as comments of their own
(:func:`pdfstudio.core.annotations.list_annotations` hides them).
Every mutation is one undo step. Coordinates are display space.
"""

from __future__ import annotations

import io
from dataclasses import dataclass, field
from datetime import datetime
from typing import Iterable, Mapping, Sequence

import pymupdf

from ..i18n import tr
from . import annotations as A
from . import textsel
from .document import CONTENT, PdfDocument
from .identity import current_author, parse_pdf_date, pdf_date
from .render import DocLike, PointLike, RectLike, as_document, display_to_page_rect, page_to_display_rect

REVIEW_STATES = ("none", "accepted", "rejected", "cancelled", "completed")
_REVIEW_PDF = {"none": "None", "accepted": "Accepted", "rejected": "Rejected",
               "cancelled": "Cancelled", "completed": "Completed"}
_REVIEW_KEY = {v.lower(): k for k, v in _REVIEW_PDF.items()}
_HIDDEN = pymupdf.PDF_ANNOT_IS_HIDDEN | pymupdf.PDF_ANNOT_IS_NO_ZOOM | pymupdf.PDF_ANNOT_IS_NO_ROTATE
_REPLY_FLAGS = _HIDDEN  # not drawn on the page (Acrobat shows them in the thread only)


@dataclass
class Reply:
    xref: int
    author: str
    text: str
    created: datetime | None
    modified: datetime | None


@dataclass
class Comment:
    """One comment thread (display space)."""

    page: int
    xref: int
    kind: str                       # AnnotInfo.kind, or "replace" / "insert" for text edits
    author: str
    text: str                       # /Contents (replacement text for "replace")
    quote: str                      # text under a markup (empty otherwise)
    color: tuple | None
    rect: pymupdf.Rect
    created: datetime | None
    modified: datetime | None
    status: str = "none"            # REVIEW_STATES
    resolved: bool = False          # Acrobat check mark / Word "resolved"
    replies: list[Reply] = field(default_factory=list)
    members: list[int] = field(default_factory=list)  # group members (e.g. the caret of "replace")

    @property
    def key(self) -> tuple[int, int]:
        return (self.page, self.xref)

    @property
    def last_activity(self) -> datetime | None:
        dates = [d for d in [self.modified, self.created] + [r.modified or r.created for r in self.replies] if d]
        return max(dates) if dates else None

    def matches(self, query: str) -> bool:
        q = query.casefold().strip()
        if not q:
            return True
        hay = [self.text, self.quote, self.author] + [r.text for r in self.replies] + [r.author for r in self.replies]
        return any(q in (h or "").casefold() for h in hay)


# -- reading -------------------------------------------------------------------------
def _key(doc: pymupdf.Document, xref: int, name: str) -> str:
    kind, value = doc.xref_get_key(xref, name)
    if kind == "null":
        return ""
    if kind == "name":
        return value.lstrip("/")
    return value


def _date(doc: pymupdf.Document, xref: int, name: str) -> datetime | None:
    return parse_pdf_date(_key(doc, xref, name))


def _sort_key(d: datetime | None, xref: int):
    return (d.timestamp() if d else 0.0, xref)


def _thread(doc: pymupdf.Document, page: pymupdf.Page, info: A.AnnotInfo,
            kids: dict[int, list[pymupdf.Annot]], pt: textsel.PageText | None) -> Comment:
    xref = info.xref
    replies: list[Reply] = []
    states: dict[str, tuple] = {}
    members: list[int] = []
    stack, seen = [xref], {xref}
    while stack:
        parent = stack.pop()
        for a in kids.get(parent, []):
            if a.xref in seen:
                continue
            seen.add(a.xref)
            rt = _key(doc, a.xref, "RT") or "R"
            if rt == "Group":
                members.append(a.xref)
                continue
            model = _key(doc, a.xref, "StateModel")
            created, modified = _date(doc, a.xref, "CreationDate"), _date(doc, a.xref, "M")
            if model:
                key = _sort_key(modified or created, a.xref)
                if model not in states or key > states[model][0]:
                    states[model] = (key, _key(doc, a.xref, "State"))
                continue
            ai = a.info or {}
            replies.append(Reply(a.xref, ai.get("title", "") or "", ai.get("content", "") or "", created, modified))
            stack.append(a.xref)
    replies.sort(key=lambda r: _sort_key(r.created or r.modified, r.xref))
    status = _REVIEW_KEY.get(states.get("Review", (None, "None"))[1].lower(), "none")
    resolved = states.get("Marked", (None, "Unmarked"))[1].lower() == "marked"

    kind = info.kind
    if kind == "strikeout" and (_key(doc, xref, "IT") == "StrikeOutTextEdit" or members):
        kind = "replace"
    elif kind == "caret":
        kind = "insert"
    quote = ""
    if info.kind in A.MARKUP_KINDS and pt is not None:
        quote = textsel.text_in_rects(pt, _quad_rects(page, xref))
    return Comment(
        page=info.page, xref=xref, kind=kind, author=info.author, text=info.contents, quote=quote,
        color=info.color, rect=info.rect,
        created=_date(doc, xref, "CreationDate"), modified=_date(doc, xref, "M"),
        status=status, resolved=resolved, replies=replies, members=members,
    )


def _quad_rects(page: pymupdf.Page, xref: int) -> list[pymupdf.Rect]:
    annot = page.load_annot(xref)
    verts = annot.vertices or []
    return [page_to_display_rect(page, pymupdf.Quad(verts[i:i + 4]).rect) for i in range(0, len(verts) - 3, 4)]


#: kinds that are not comments (links, form fields, popups are not annotations
#: of pymupdf's annots() anyway)
_SKIP_KINDS = {"popup", "link", "widget"}


def comments(doc: DocLike, pages: Iterable[int] | None = None) -> list[Comment]:
    """All comment threads, in page order and top-to-bottom on each page."""
    d = as_document(doc)
    out: list[Comment] = []
    for p in (range(d.page_count) if pages is None else pages):
        page = d[p]
        infos = [i for i in A.list_annotations(d, p) if i.kind not in _SKIP_KINDS]
        if not infos:
            continue
        kids = A.thread_children(page)
        pt = textsel.page_text(d, p) if any(i.kind in A.MARKUP_KINDS for i in infos) else None
        threads = [_thread(d, page, info, kids, pt) for info in infos]
        threads.sort(key=lambda c: (round(c.rect.y0, 1), c.rect.x0))
        out.extend(threads)
    return out


def comment(doc: DocLike, page: int, xref: int) -> Comment | None:
    return next((c for c in comments(doc, [page]) if c.xref == xref), None)


# -- writing -------------------------------------------------------------------------
def _child(page: pymupdf.Page, parent: pymupdf.Annot, text: str) -> pymupdf.Annot:
    """A /Text annotation in ``parent``'s thread, placed on the parent."""
    r = parent.rect
    annot = page.add_text_annot(pymupdf.Point(r.x0, r.y0), text, icon="Comment")
    annot.set_irt_xref(parent.xref)
    A.stamp_new(annot)
    annot.set_flags(_REPLY_FLAGS)
    annot.update()
    return annot


def _load_top(pdf: PdfDocument, page: int, xref: int) -> pymupdf.Annot:
    annot = A._load(pdf.page(page), xref)
    if A.is_thread_child(annot):
        raise ValueError(tr("core.err.comment_child"))
    return annot


def add_comment(
    pdf: PdfDocument,
    selection: A.Selection,
    text: str,
    color: A.Color,
    *,
    kind: str = "highlight",
    opacity: float = 1.0,
) -> list[A.AnnotRef]:
    """Comment on a text selection (``{page: [line rect, ...]}``): marks it
    (highlight by default) with ``text`` as the comment – one undo step.
    Returns the created ``(page, xref)`` pairs; the comment is on the first."""
    return A.add_markup(pdf, kind, selection, color, opacity, contents=text,
                        label=tr("core.undo.comment"))


def add_reply(pdf: PdfDocument, page: int, xref: int, text: str) -> int:
    """Reply in the thread of comment ``xref`` (or of a reply). Returns the
    reply's xref."""
    text = str(text or "").strip()
    if not text:
        raise ValueError(tr("core.err.reply_empty"))
    A._load(pdf.page(page), xref)
    with pdf.edit(tr("core.undo.reply"), CONTENT, (page,)):
        pg = pdf.page(page)
        return _child(pg, A._load(pg, xref), text).xref


def edit_reply(pdf: PdfDocument, page: int, xref: int, text: str) -> None:
    """Change the text of a reply (or of a comment)."""
    A.update_annotation(pdf, page, xref, contents=str(text or ""))


def delete_reply(pdf: PdfDocument, page: int, xref: int) -> None:
    """Remove one reply (and replies to it)."""
    A.delete_annotation(pdf, page, xref)


def _set_state(pdf: PdfDocument, page: int, xref: int, model: str, state: str, label: str) -> None:
    _load_top(pdf, page, xref)
    author = current_author()
    with pdf.edit(label, CONTENT, (page,)):
        pg = pdf.page(page)
        parent = A._load(pg, xref)
        doc = pg.parent
        mine = None
        for a in A.thread_children(pg).get(xref, []):
            if _key(doc, a.xref, "StateModel") == model and (a.info or {}).get("title", "") == author:
                mine = a
        text = tr("core.comment.state_set", state=state, author=author or "?")
        if mine is None:
            mine = _child(pg, parent, text)
            doc.xref_set_key(mine.xref, "StateModel", pymupdf.get_pdf_str(model))
        else:
            mine.set_info(content=text, modDate=pdf_date())
            mine.update()
        doc.xref_set_key(mine.xref, "State", pymupdf.get_pdf_str(state))


def set_status(pdf: PdfDocument, page: int, xref: int, status: str) -> None:
    """Acrobat review state of a comment (:data:`REVIEW_STATES`)."""
    if status not in REVIEW_STATES:
        raise ValueError(tr("core.err.comment_status", status=status))
    _set_state(pdf, page, xref, "Review", _REVIEW_PDF[status], tr("core.undo.comment_status"))


def set_resolved(pdf: PdfDocument, page: int, xref: int, resolved: bool) -> None:
    """Mark a comment resolved (Acrobat's check mark) or reopen it."""
    _set_state(pdf, page, xref, "Marked", "Marked" if resolved else "Unmarked",
               tr("core.undo.comment_resolve" if resolved else "core.undo.comment_reopen"))


def caret_rect(pt: textsel.PageText, caret: int) -> pymupdf.Rect | None:
    """Display rect of an Acrobat-style insertion caret at ``caret``: centred
    on the caret position, sitting on the line's bottom."""
    if not pt.chars:
        return None
    caret = max(0, min(len(pt.chars), caret))
    if caret < len(pt.chars):
        g = pt.chars[caret]
        before = caret > 0 and pt.chars[caret - 1].line == g.line
        if not before and caret > 0 and caret == pt.lines[pt.chars[caret - 1].line].end:
            g = pt.chars[caret - 1]
            x = g.rect.x1
        else:
            x = g.rect.x0
    else:
        g = pt.chars[-1]
        x = g.rect.x1
    h = max(4.0, g.rect.height * 0.55)
    w = h * 0.9
    return pymupdf.Rect(x - w / 2, g.rect.y1 - h * 0.6, x + w / 2, g.rect.y1 + h * 0.4)


def add_insert_text(pdf: PdfDocument, page: int, rect: RectLike, text: str, color: A.Color) -> int:
    """Acrobat's "Indsæt tekst" – a caret (display ``rect``, see
    :func:`caret_rect`) proposing ``text`` to be inserted."""
    stroke = A._check_color(color)
    with pdf.edit(tr("core.undo.insert_text"), CONTENT, (page,)):
        pg = pdf.page(page)
        r = display_to_page_rect(pg, rect)
        annot = pg.add_caret_annot(pymupdf.Point(r.x0, r.y0))
        annot.set_rect(r)
        annot.set_colors(stroke=stroke)
        annot.set_info(content=str(text or ""))
        return A._finish(annot, 1.0)


def add_replace_text(pdf: PdfDocument, page: int, line_rects: Sequence[RectLike], replacement: str,
                     color: A.Color, caret: RectLike | None = None) -> int:
    """Acrobat's "Erstat tekst": strike out the selection (one rect per line)
    and propose ``replacement`` (on the strike-out), with a grouped caret at
    the end of the selection. Returns the strike-out's xref."""
    stroke = A._check_color(color)
    rects = [pymupdf.Rect(r) for r in line_rects if not pymupdf.Rect(r).is_empty]
    if not rects:
        raise ValueError(tr("core.err.annot_empty"))
    with pdf.edit(tr("core.undo.replace_text"), CONTENT, (page,)):
        pg = pdf.page(page)
        doc = pg.parent
        strike = pg.add_strikeout_annot(quads=[display_to_page_rect(pg, r).quad for r in rects])
        strike.set_colors(stroke=stroke)
        strike.set_info(content=str(replacement or ""))
        x = A._finish(strike, 1.0)
        doc.xref_set_key(x, "IT", "/StrikeOutTextEdit")
        last = rects[-1]
        cr = pymupdf.Rect(caret) if caret is not None else pymupdf.Rect(
            last.x1 - last.height * 0.25, last.y1 - last.height * 0.35, last.x1 + last.height * 0.25, last.y1 + 0.2 * last.height)
        r = display_to_page_rect(pg, cr)
        car = pg.add_caret_annot(pymupdf.Point(r.x0, r.y0))
        car.set_rect(r)
        car.set_colors(stroke=stroke)
        car.set_irt_xref(x)
        A.stamp_new(car)
        car.update()
        doc.xref_set_key(car.xref, "RT", "/Group")
        return x


# -- summary -------------------------------------------------------------------------
def _esc(text: str) -> str:
    return (str(text or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
            .replace("\n", "<br/>"))


def _when(d: datetime | None) -> str:
    return d.astimezone().strftime("%d-%m-%Y %H:%M") if d else ""


def summary_pdf(doc: DocLike, title: str = "") -> bytes:
    """A PDF summarising every comment: page, kind, author, date, quote,
    text, status and replies (Acrobat's "Opsummér kommentarer")."""
    d = as_document(doc)
    threads = comments(d)
    css = ("body{font-family:sans-serif;font-size:10pt;color:#1d1d24}"
           "h1{font-size:16pt;margin:0 0 4pt 0} .sub{color:#666;font-size:9pt;margin-bottom:12pt}"
           "h2{font-size:11pt;margin:12pt 0 4pt 0;border-bottom:1px solid #ccc}"
           ".c{margin:0 0 8pt 0} .meta{color:#555;font-size:8.5pt} .q{font-style:italic;color:#444}"
           ".r{margin-left:14pt} .st{color:#5B5BD6;font-size:8.5pt}")
    parts = [f"<h1>{_esc(tr('core.summary.title'))}</h1>",
             f"<div class='sub'>{_esc(title)} · {_esc(tr('core.summary.count', n=len(threads)))}</div>"]
    page = -1
    for c in threads:
        if c.page != page:
            page = c.page
            parts.append(f"<h2>{_esc(tr('core.summary.page', n=page + 1))}</h2>")
        meta = " · ".join(x for x in (tr(f"annot.kind.{c.kind}"), c.author, _when(c.created)) if x)
        status = []
        if c.status != "none":
            status.append(tr(f"core.comment.status.{c.status}"))
        if c.resolved:
            status.append(tr("core.comment.resolved"))
        block = [f"<div class='c'><div class='meta'>{_esc(meta)}</div>"]
        if c.quote:
            block.append(f"<div class='q'>»{_esc(c.quote)}«</div>")
        if c.text:
            block.append(f"<div>{_esc(c.text)}</div>")
        if status:
            block.append(f"<div class='st'>{_esc(' · '.join(status))}</div>")
        for r in c.replies:
            rm = " · ".join(x for x in (r.author, _when(r.created)) if x)
            block.append(f"<div class='r'><span class='meta'>{_esc(rm)}</span><br/>{_esc(r.text)}</div>")
        block.append("</div>")
        parts.append("".join(block))
    if not threads:
        parts.append(f"<p>{_esc(tr('core.summary.empty'))}</p>")
    story = pymupdf.Story(html="".join(parts), user_css=css)
    writer_buf = io.BytesIO()
    writer = pymupdf.DocumentWriter(writer_buf)
    box = pymupdf.paper_rect("a4")
    where = box + (54, 54, -54, -54)
    more = True
    while more:
        dev = writer.begin_page(box)
        more, _ = story.place(where)
        story.draw(dev)
        writer.end_page()
    writer.close()
    return writer_buf.getvalue()


__all__ = [
    "REVIEW_STATES",
    "Comment",
    "Reply",
    "add_comment",
    "add_insert_text",
    "add_replace_text",
    "add_reply",
    "caret_rect",
    "comment",
    "comments",
    "delete_reply",
    "edit_reply",
    "set_resolved",
    "set_status",
    "summary_pdf",
]
