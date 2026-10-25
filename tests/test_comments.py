import io
from datetime import timezone

import pymupdf
import pytest

from pdfstudio.core import PdfDocument
from pdfstudio.core import annotations as A
from pdfstudio.core import comments as C
from pdfstudio.core import identity, textsel


@pytest.fixture(autouse=True)
def _author():
    identity.set_author("Anne Andersen")
    yield
    identity.set_author(None)


def _lines(pdf, page, text):
    pt = textsel.page_text(pdf.doc, page)
    full = "".join(g.c for g in pt.chars)
    a = full.index(text)
    return textsel.selection_rects(pt, a, a + len(text))


def _reopen(pdf, tmp_path):
    path = str(tmp_path / "out.pdf")
    pdf.save(path)
    return PdfDocument.open(path)


def test_comment_on_selection_quotes_exact_text(sample_pdf):
    refs = C.add_comment(sample_pdf, {0: _lines(sample_pdf, 0, "en broedtekst skrevet")}, "Ret ordet", (1, .8, 0))
    [c] = C.comments(sample_pdf.doc)
    assert (c.page, c.xref) == refs[0]
    assert c.kind == "highlight" and c.text == "Ret ordet" and c.author == "Anne Andersen"
    assert c.quote == "en broedtekst skrevet"
    assert c.created is not None and c.created.tzinfo is not None
    assert sample_pdf.history.undo_label == "Kommentér tekst"


def test_replies_threads_and_roundtrip(sample_pdf, tmp_path):
    [(p, x)] = C.add_comment(sample_pdf, {0: _lines(sample_pdf, 0, "Anden linje")}, "Hvorfor?", (1, .8, 0))
    identity.set_author("Bo")
    r1 = C.add_reply(sample_pdf, p, x, "Fordi.")
    identity.set_author("Anne Andersen")
    C.add_reply(sample_pdf, p, r1, "Ok, tak")       # reply to a reply: same thread
    pdf = _reopen(sample_pdf, tmp_path)
    [c] = C.comments(pdf.doc)                         # replies are not comments of their own
    assert [(r.author, r.text) for r in c.replies] == [("Bo", "Fordi."), ("Anne Andersen", "Ok, tak")]
    # stored the standard way: /Text with /IRT -> parent, hidden on the page
    doc = pdf.doc
    reply = c.replies[0].xref
    assert doc.xref_get_key(reply, "Subtype")[1] == "/Text"
    assert doc.xref_get_key(reply, "IRT")[1] == f"{x} 0 R"
    assert int(doc.xref_get_key(reply, "F")[1]) & pymupdf.PDF_ANNOT_IS_HIDDEN
    assert not A.annotation_at(doc, p, (c.rect.x0 + 2, c.rect.y0 + 2)).xref == reply
    C.edit_reply(pdf, p, reply, "Fordi det er sådan.")
    assert C.comment(pdf.doc, p, x).replies[0].text == "Fordi det er sådan."
    C.delete_reply(pdf, p, reply)                     # deletes the reply to it as well
    assert C.comment(pdf.doc, p, x).replies == []


def test_status_and_resolved_use_acrobat_states(sample_pdf, tmp_path):
    [(p, x)] = C.add_comment(sample_pdf, {0: _lines(sample_pdf, 0, "Kvartalsrapport")}, "", (1, .8, 0))
    C.set_status(sample_pdf, p, x, "accepted")
    C.set_status(sample_pdf, p, x, "rejected")       # same author: updates, no second state annot
    C.set_resolved(sample_pdf, p, x, True)
    pdf = _reopen(sample_pdf, tmp_path)
    c = C.comment(pdf.doc, p, x)
    assert c.status == "rejected" and c.resolved and c.replies == []
    states = A.thread_children(pdf.page(p))[x]
    models = sorted(pdf.doc.xref_get_key(s.xref, "StateModel")[1] for s in states)
    assert models == ["Marked", "Review"]
    review = next(s for s in states if pdf.doc.xref_get_key(s.xref, "StateModel")[1] == "Review")
    assert pdf.doc.xref_get_key(review.xref, "State")[1] == "Rejected"
    C.set_resolved(pdf, p, x, False)
    assert not C.comment(pdf.doc, p, x).resolved
    with pytest.raises(ValueError):
        C.set_status(pdf, p, x, "maybe")


def test_newest_state_wins_across_authors(sample_pdf):
    [(p, x)] = C.add_comment(sample_pdf, {0: _lines(sample_pdf, 0, "Kvartalsrapport")}, "", (1, .8, 0))
    C.set_status(sample_pdf, p, x, "accepted")
    identity.set_author("Bo")
    C.set_status(sample_pdf, p, x, "completed")
    doc = sample_pdf.doc
    states = A.thread_children(sample_pdf.page(p))[x]
    # make Anne's state the newest
    anne = next(s for s in states if s.info["title"] == "Anne Andersen")
    doc.xref_set_key(anne.xref, "M", "(D:20990101000000Z)")
    assert C.comment(doc, p, x).status == "accepted"


def test_reads_acrobat_made_threads():
    """A thread as Acrobat writes it: reply without hidden flag, state as text strings."""
    doc = pymupdf.open()
    pg = doc.new_page()
    pg.insert_text((72, 100), "Acrobat text", fontsize=12)
    h = pg.add_highlight_annot(pg.search_for("Acrobat")[0])
    h.set_info(content="Parent", title="Adobe User", creationDate="D:20250101120000+01'00'")
    h.update()
    r = pg.add_text_annot(h.rect.tl, "Reply from Acrobat")
    r.set_info(title="Other")
    r.update()
    doc.xref_set_key(r.xref, "IRT", f"{h.xref} 0 R")
    doc.xref_set_key(r.xref, "RT", "/R")
    s = pg.add_text_annot(h.rect.tl, "Completed set by Other")
    s.update()
    doc.xref_set_key(s.xref, "IRT", f"{h.xref} 0 R")
    doc.xref_set_key(s.xref, "State", "(Completed)")
    doc.xref_set_key(s.xref, "StateModel", "(Review)")
    [c] = C.comments(doc)
    assert c.text == "Parent" and c.author == "Adobe User" and c.quote == "Acrobat"
    assert c.created.astimezone(timezone.utc).hour == 11
    assert [x.text for x in c.replies] == ["Reply from Acrobat"] and c.status == "completed"


def test_delete_comment_deletes_whole_thread(sample_pdf):
    [(p, x)] = C.add_comment(sample_pdf, {0: _lines(sample_pdf, 0, "Kvartalsrapport")}, "a", (1, .8, 0))
    r = C.add_reply(sample_pdf, p, x, "b")
    C.add_reply(sample_pdf, p, r, "c")
    C.set_resolved(sample_pdf, p, x, True)
    A.delete_annotation(sample_pdf, p, x)
    assert list(sample_pdf.page(p).annots()) == []
    sample_pdf.undo()
    assert len(C.comment(sample_pdf.doc, p, x).replies) == 2


def test_replace_and_insert_text(sample_pdf, tmp_path):
    pt = textsel.page_text(sample_pdf.doc, 0)
    full = "".join(g.c for g in pt.chars)
    a = full.index("broedtekst")
    rects = textsel.selection_rects(pt, a, a + len("broedtekst"))
    x = C.add_replace_text(sample_pdf, 0, rects, "brødtekst", (0, 0, 1))
    caret = full.index("Anden")
    cr = C.caret_rect(textsel.page_text(sample_pdf.doc, 0), caret)
    y = C.add_insert_text(sample_pdf, 0, cr, "Den ", (0, 0, 1))
    pdf = _reopen(sample_pdf, tmp_path)
    cs = {c.xref: c for c in C.comments(pdf.doc)}
    assert set(cs) == {x, y}                        # the grouped caret is part of "replace"
    assert cs[x].kind == "replace" and cs[x].text == "brødtekst" and cs[x].quote == "broedtekst"
    assert len(cs[x].members) == 1 and pdf.doc.xref_get_key(cs[x].members[0], "RT")[1] == "/Group"
    assert pdf.doc.xref_get_key(x, "IT")[1] == "/StrikeOutTextEdit"
    assert cs[y].kind == "insert" and cs[y].text == "Den "
    A.delete_annotation(pdf, 0, x)
    assert [c.xref for c in C.comments(pdf.doc)] == [y]


def test_comments_sorted_and_searchable(sample_pdf):
    C.add_comment(sample_pdf, {0: _lines(sample_pdf, 0, "Anden linje")}, "nederst", (1, .8, 0))
    A.add_note(sample_pdf, 0, (400, 60), "øverst", (1, .8, 0))
    A.add_shape(sample_pdf, 1, "rect", (72, 420), (300, 480), (.9, .2, .2))
    cs = C.comments(sample_pdf.doc)
    assert [c.text for c in cs] == ["øverst", "nederst", ""]
    assert cs[1].matches("ANDEN") and cs[1].matches("anne") and not cs[1].matches("xyz")


def test_rotated_page_comment(sample_pdf):
    C.add_comment(sample_pdf, {2: _lines(sample_pdf, 2, "Roteret")}, "Roteret!", (1, .8, 0))
    [c] = C.comments(sample_pdf.doc)
    assert c.quote == "Roteret" and c.page == 2


def test_summary_pdf(sample_pdf):
    [(p, x)] = C.add_comment(sample_pdf, {0: _lines(sample_pdf, 0, "Anden linje")}, "Tjek æøå", (1, .8, 0))
    C.add_reply(sample_pdf, p, x, "Svar her")
    C.set_status(sample_pdf, p, x, "accepted")
    data = C.summary_pdf(sample_pdf.doc, "rapport.pdf")
    out = pymupdf.open("pdf", data)
    text = "".join(pg.get_text() for pg in out)
    for s in ("Kommentaroversigt", "Side 1", "»Anden linje«", "Tjek æøå", "Svar her", "Accepteret", "Anne Andersen"):
        assert s in text
    empty = pymupdf.open("pdf", C.summary_pdf(pymupdf.open("pdf", io.BytesIO(PdfDocument.new_blank().to_bytes()))))
    assert "ingen kommentarer" in empty[0].get_text()


def test_identity_dates_roundtrip():
    from datetime import datetime

    s = identity.pdf_date(datetime(2026, 3, 4, 5, 6, 7))
    d = identity.parse_pdf_date(s)
    assert (d.year, d.month, d.day, d.hour, d.minute, d.second) == (2026, 3, 4, 5, 6, 7)
    assert identity.parse_pdf_date("D:2024") .year == 2024
    assert identity.parse_pdf_date("D:20240102030405Z").tzinfo == timezone.utc
    assert identity.parse_pdf_date("D:20240102030405-05'30'").utcoffset().total_seconds() == -5.5 * 3600
    assert identity.parse_pdf_date("garbage") is None and identity.parse_pdf_date(None) is None
    identity.set_author(None)
    assert isinstance(identity.current_author(), str)
