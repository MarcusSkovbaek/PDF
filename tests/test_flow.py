"""Flowing text (core/flow.py): stories, flow over pages, new pages,
pulling text back, Enter/Backspace, list items, figures, chapters, undo."""

import pymupdf
import pytest

from pdfstudio.core import PdfDocument, flow
from pdfstudio.core import text_edit as te
from tests.flowdocs import HEADER, report

EXTRA = " Dette er en ny sætning, som er tilføjet for at se teksten flyde videre."


def _pdf(doc: pymupdf.Document) -> PdfDocument:
    return PdfDocument(pymupdf.open("pdf", doc.tobytes()))


def body(doc, pages=None) -> str:
    """The body text of the document in reading order (columns per page)."""
    out = []
    for p in (range(doc.page_count) if pages is None else pages):
        for s in flow.page_stories(doc, p):
            if s.kind == "flow":
                out.extend(pc.text for pc in s.pieces)
    return " ".join(" ".join(out).split())


def overlaps(doc) -> int:
    bad = 0
    for p in range(doc.page_count):
        lines = [pymupdf.Rect(ln["bbox"]) for b in doc[p].get_text("dict")["blocks"] for ln in b.get("lines", [])
                 if "".join(s["text"] for s in ln["spans"]).strip()]
        for i, a in enumerate(lines):
            for b in lines[i + 1:]:
                r = a & b
                if not r.is_empty and r.height > 0.35 * min(a.height, b.height) and r.width > 3:
                    bad += 1
    return bad


def column(doc, page) -> flow.Story:
    return next(s for s in flow.page_stories(doc, page) if s.kind == "flow")


def edits_for(story, changes: dict | None = None) -> list:
    changes = changes or {}
    return [flow.Edit(piece=k, text=changes.get(k, pc.text)) for k, pc in enumerate(story.pieces)]


def with_extra(text: str, piece_text: str, extra: str) -> str:
    """``text`` (normalised body) with ``extra`` after the piece's text."""
    p = " ".join(piece_text.split())
    k = text.find(p)
    assert k >= 0
    return " ".join((text[:k + len(p)] + extra + text[k + len(p):]).split())


@pytest.fixture(scope="module")
def rep():
    return report(chapters=2)


def test_stories_of_a_page(rep):
    stories = flow.page_stories(rep, 0)
    roles = [s.role for s in stories]
    assert roles.count("body") == 1 and "header" in roles and "footer" in roles
    s = column(rep, 0)
    assert s.pieces[0].text.startswith("1. Kapitel")
    items = [p for p in s.pieces if p.bullet is not None]
    assert len(items) == 3 and all(te.is_list_bullet(p.bullet) for p in items)
    head = next(x for x in stories if x.role == "header")
    assert head.pieces[0].text == HEADER
    hit = flow.story_at(rep, 0, (s.pieces[2].block.bbox.x0 + 5, s.pieces[2].block.bbox.y0 + 5))
    assert hit is not None and hit[0].kind == "flow" and hit[1] == 2


def test_growing_paragraph_flows_over_the_following_pages(rep):
    pdf = _pdf(rep)
    before = body(pdf.doc)
    s = column(pdf.doc, 0)
    res = flow.apply_story(pdf, s, edits_for(s, {2: s.pieces[2].text + EXTRA * 3}))
    assert res.pages == (0, 1, 2) and not res.inserted
    assert body(pdf.doc) == with_extra(before, s.pieces[2].text, EXTRA * 3)
    assert overlaps(pdf.doc) == 0
    # headers and footers stay where they are
    for p in range(3):
        texts = [st.pieces[0].text for st in flow.page_stories(pdf.doc, p) if st.kind == "single"]
        assert HEADER in texts and f"Side {p + 1} af 3" in texts
    # the column never runs into the footer
    for p in range(3):
        c = column(pdf.doc, p)
        assert max(pc.block.bbox.y1 for pc in c.pieces) < 790
    assert len(pdf.history._undo) == 1
    assert pdf.undo() and body(pdf.doc) == before


def test_overflow_on_the_last_page_adds_a_page(rep):
    pdf = _pdf(rep)
    before = body(pdf.doc)
    s = column(pdf.doc, 2)
    last = len(s.pieces) - 1
    res = flow.apply_story(pdf, s, edits_for(s, {last: s.pieces[last].text + EXTRA * 25}))
    assert pdf.doc.page_count == 4 and res.inserted == (3,)
    assert body(pdf.doc) == " ".join((before + EXTRA * 25).split())
    new = column(pdf.doc, 3)
    assert abs(new.pieces[0].block.bbox.y0 - column(pdf.doc, 1).pieces[0].block.bbox.y0) < 2   # at the column top
    assert overlaps(pdf.doc) == 0
    assert pdf.undo() and pdf.doc.page_count == 3 and body(pdf.doc) == before


def test_shorter_text_pulls_the_continuation_back(rep):
    pdf = _pdf(rep)
    before = body(pdf.doc)
    s = column(pdf.doc, 0)
    last = len(s.pieces) - 1
    words = s.pieces[last].text.split()
    cut = " ".join(words[:len(words) - 25])
    p1_first = column(pdf.doc, 1).pieces[0].text
    flow.apply_story(pdf, s, edits_for(s, {last: cut}))
    assert body(pdf.doc) == " ".join(before.replace(s.pieces[last].text, cut, 1).split())
    # the start of the continued text is on page 1 now
    assert p1_first.split()[0] in column(pdf.doc, 0).pieces[-1].text
    assert overlaps(pdf.doc) == 0


def test_enter_splits_and_backspace_merges(rep):
    pdf = _pdf(rep)
    before = body(pdf.doc)
    s = column(pdf.doc, 0)
    t = s.pieces[2].text
    mid = t.index(". ", len(t) // 2) + 1
    edits = edits_for(s, {2: t[:mid]})
    edits.insert(3, flow.Edit(piece=None, like=2, text=t[mid + 1:]))
    flow.apply_story(pdf, s, edits)
    assert body(pdf.doc) == before
    c = column(pdf.doc, 0)
    a, b = c.pieces[2], c.pieces[3]
    assert a.text == t[:mid] and b.text.startswith(t[mid + 1:][:30])
    gap = b.block.lines[0].bbox.y0 - a.block.lines[-1].bbox.y1
    normal = c.pieces[2].block.lines[0].bbox.y0 - c.pieces[1].block.lines[-1].bbox.y1
    assert abs(gap - normal) < 1.0                       # the paragraph spacing of the column
    # Backspace at the start of the new paragraph joins them again
    edits = edits_for(c, {2: a.text + " " + b.text})
    del edits[3]
    flow.apply_story(pdf, c, edits)
    assert body(pdf.doc) == before
    assert column(pdf.doc, 0).pieces[2].text.startswith(t[:40])
    assert overlaps(pdf.doc) == 0


def test_enter_in_a_list_makes_a_new_item_with_a_bullet(rep):
    pdf = _pdf(rep)
    s = column(pdf.doc, 0)
    k = next(i for i, p in enumerate(s.pieces) if p.bullet is not None)
    edits = edits_for(s)
    edits.insert(k + 1, flow.Edit(piece=None, like=k, text="Et helt nyt punkt i listen."))
    flow.apply_story(pdf, s, edits)
    c = column(pdf.doc, 0)
    new = next(p for p in c.pieces if p.text == "Et helt nyt punkt i listen.")
    assert new.bullet is not None
    assert abs(new.bullet.lines[0].bbox.x0 - s.pieces[k].bullet.lines[0].bbox.x0) < 0.5
    assert abs(new.block.lines[0].bbox.x0 - s.pieces[k].block.lines[0].bbox.x0) < 0.5
    assert sum(1 for p in c.pieces if p.bullet is not None) >= 3
    assert overlaps(pdf.doc) == 0


def test_a_chapter_start_keeps_its_page():
    doc = report(chapters=2, chapter_breaks=True)
    pdf = _pdf(doc)
    before = body(pdf.doc)
    chapter_page = next(p for p in range(pdf.doc.page_count) if column(pdf.doc, p).pieces[0].text.startswith("2."))
    snapshot = [(pc.text, tuple(round(v, 1) for v in pc.block.bbox)) for pc in column(pdf.doc, chapter_page).pieces]
    s = column(pdf.doc, chapter_page - 1)
    last = len(s.pieces) - 1
    res = flow.apply_story(pdf, s, edits_for(s, {last: s.pieces[last].text + EXTRA * 40}))
    assert res.inserted == (chapter_page,)
    after = [(pc.text, tuple(round(v, 1) for v in pc.block.bbox)) for pc in column(pdf.doc, chapter_page + 1).pieces]
    assert after == snapshot                             # the chapter page is untouched
    assert body(pdf.doc) == with_extra(before, s.pieces[last].text, EXTRA * 40)


def test_text_flows_around_a_figure():
    doc = report(chapters=1, figure=True)
    pdf = _pdf(doc)
    img = pymupdf.Rect(next(b["bbox"] for b in pdf.doc[0].get_text("dict")["blocks"] if b["type"] == 1))
    before = body(pdf.doc)
    s = column(pdf.doc, 0)
    k = max(i for i, p in enumerate(s.pieces) if p.block.bbox.y1 < img.y0)
    flow.apply_story(pdf, s, edits_for(s, {k: s.pieces[k].text + EXTRA * 4}))
    assert body(pdf.doc) == with_extra(before, s.pieces[k].text, EXTRA * 4)
    assert pymupdf.Rect(next(b["bbox"] for b in pdf.doc[0].get_text("dict")["blocks"] if b["type"] == 1)) == img
    caption = next(st for st in flow.page_stories(pdf.doc, 0) if st.role == "caption")
    band = img | caption.rect
    for pc in column(pdf.doc, 0).pieces:
        for ln in pc.block.lines:
            assert not ln.bbox.intersects(band) or (ln.bbox & band).height < 0.5
    assert overlaps(pdf.doc) == 0


def test_flow_on_rotated_pages():
    doc = report(chapters=2, rotate=90)
    pdf = _pdf(doc)
    before = body(pdf.doc)
    s = column(pdf.doc, 0)
    flow.apply_story(pdf, s, edits_for(s, {2: s.pieces[2].text + EXTRA * 3}))
    assert pdf.doc.page_count == 3
    assert body(pdf.doc) == with_extra(before, s.pieces[2].text, EXTRA * 3)
    assert overlaps(pdf.doc) == 0


def test_each_column_flows_on_its_own():
    doc = report(chapters=2, columns=2)
    pdf = _pdf(doc)
    left, right = [s for s in flow.page_stories(pdf.doc, 0) if s.kind == "flow"]
    right_before = [(pc.text, tuple(round(v, 1) for v in pc.block.bbox)) for pc in right.pieces]
    flow.apply_story(pdf, left, edits_for(left, {1: left.pieces[1].text + EXTRA * 3}))
    left2, right2 = [s for s in flow.page_stories(pdf.doc, 0) if s.kind == "flow"]
    assert [(pc.text, tuple(round(v, 1) for v in pc.block.bbox)) for pc in right2.pieces] == right_before
    assert EXTRA.split()[1] in body(pdf.doc, [0])
    assert overlaps(pdf.doc) == 0


def test_edits_of_one_session_are_one_undo_step(rep):
    pdf = _pdf(rep)
    before = body(pdf.doc)
    session = object()
    for _ in range(3):
        s = column(pdf.doc, 0)
        flow.apply_story(pdf, s, edits_for(s, {1: s.pieces[1].text + " Mere."}), merge=session)
    assert len(pdf.history._undo) == 1
    assert pdf.undo() and body(pdf.doc) == before


def test_locate_finds_the_caret_after_flowing(rep):
    pdf = _pdf(rep)
    s = column(pdf.doc, 0)
    last = len(s.pieces) - 1
    text = s.pieces[last].text + EXTRA * 3
    res = flow.apply_story(pdf, s, edits_for(s, {last: text}))
    page, (x, y) = res.locate(last, len(text))          # the end of the paragraph: on page 2 now
    assert page == 1
    hit = flow.story_at(pdf.doc, page, (x - 2, y - 3))
    assert hit is not None and "videre." in hit[0].pieces[hit[1]].text


def test_unchanged_story_writes_nothing(rep):
    pdf = _pdf(rep)
    s = column(pdf.doc, 0)
    res = flow.apply_story(pdf, s, edits_for(s))
    assert not res.pages and not pdf.history.can_undo


def test_a_header_is_edited_where_it_is(rep):
    pdf = _pdf(rep)
    head = next(s for s in flow.page_stories(pdf.doc, 0) if s.role == "header")
    flow.apply_story(pdf, head, [flow.Edit(piece=0, text=HEADER.replace("2026", "2027"))])
    head2 = next(s for s in flow.page_stories(pdf.doc, 0) if s.role == "header")
    assert head2.pieces[0].text == HEADER.replace("2026", "2027")
    assert abs(head2.rect.y0 - head.rect.y0) < 0.5
    assert body(pdf.doc) == body(rep)


def test_backdrop_without_the_story_is_cached_and_follows_edits():
    pdf = _pdf(report(1))
    st = next(s for s in flow.page_stories(pdf.doc, 0) if s.kind == "flow")
    flow.prepare_without(pdf.doc, st)
    text_free = flow._text_free_page(pdf.doc, st)
    assert flow._text_free_page(pdf.doc, st) is text_free             # cached
    words = text_free[0].get_text()
    assert st.pieces[0].text.split()[0] not in words                 # the story's text is gone
    assert HEADER.split()[0] in words                                # other text stays
    a = flow.render_without(pdf.doc, st, 1.0)
    assert a.width == round(pdf.doc[0].rect.width)
    edits = [flow.Edit(piece=k, text=p.text + (EXTRA if k == 0 else "")) for k, p in enumerate(st.pieces)]
    flow.apply_story(pdf, st, edits)
    st2 = next(s for s in flow.page_stories(pdf.doc, 0) if s.kind == "flow")
    assert flow._text_free_page(pdf.doc, st2) is not text_free        # the page changed: built anew
