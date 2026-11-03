# Manual stress tool (not collected by pytest): python tests/stress_flow.py file.pdf [step]
"""Grow one body paragraph on every ``step``-th page of a real document and
check the text flow: body text order, no new overlaps, headers/footers kept,
undo restores the original."""
import sys, time
sys.path.insert(0, '/home/user/PDF')
from pdfstudio.security import netguard; netguard.install()
import pymupdf
from pdfstudio.core import PdfDocument, flow
from tests.test_flow import body, overlaps

EXTRA = " Dette er en ny sætning, som er tilføjet for at se teksten flyde videre."
path = sys.argv[1]
step = int(sys.argv[2]) if len(sys.argv) > 2 else 4
orig = pymupdf.open(path)
base_body = body(orig)
base_over = overlaps(orig)
singles = lambda d, p: sorted(s.pieces[0].text for s in flow.page_stories(d, p) if s.role in ("header", "footer"))
problems, times = [], []
for page in range(0, orig.page_count, step):
    pdf = PdfDocument(pymupdf.open("pdf", orig.tobytes()))
    stories = [s for s in flow.page_stories(pdf.doc, page) if s.kind == "flow"]
    if not stories:
        continue
    s = max(stories, key=lambda x: sum(len(p.text) for p in x.pieces))
    paras = [i for i, p in enumerate(s.pieces) if len(p.block.lines) >= 3]
    if not paras:
        continue
    k = paras[len(paras) // 2]
    before = body(pdf.doc)
    heads = {p: singles(pdf.doc, p) for p in range(max(0, page - 1), min(pdf.doc.page_count, page + 3))}
    edits = [flow.Edit(piece=i, text=pc.text + (EXTRA * 2 if i == k else "")) for i, pc in enumerate(s.pieces)]
    t = time.perf_counter()
    try:
        res = flow.apply_story(pdf, s, edits)
    except Exception as exc:
        problems.append((page, "EXC " + repr(exc)[:150])); continue
    times.append((time.perf_counter() - t) * 1000)
    p = " ".join(s.pieces[k].text.split())
    i = before.find(p)
    exp = " ".join((before[:i + len(p)] + EXTRA * 2 + before[i + len(p):]).split())
    got = body(pdf.doc)
    if got != exp:
        import difflib
        ops = [op for op in difflib.SequenceMatcher(None, exp, got, autojunk=False).get_opcodes() if op[0] != "equal"]
        problems.append((page, f"text: {len(ops)} diffs, first {ops[0][0]} {exp[ops[0][1]:ops[0][2]][:60]!r} -> {got[ops[0][3]:ops[0][4]][:60]!r}"))
    over = overlaps(pdf.doc)
    if over > base_over:
        problems.append((page, f"overlaps {base_over} -> {over}"))
    shift = len(res.inserted)
    for q, h in heads.items():
        qq = q + (shift if res.inserted and q >= res.inserted[0] else 0)
        if qq < pdf.doc.page_count and singles(pdf.doc, qq) != h:
            problems.append((page, f"header/footer on page {q}: {h} -> {singles(pdf.doc, qq)}"))
    pdf.undo()
    if body(pdf.doc) != base_body or pdf.doc.page_count != orig.page_count:
        problems.append((page, "undo does not restore"))
    print(f"page {page:3d}: {times[-1]:6.0f} ms, pages changed {res.pages}, inserted {res.inserted}", flush=True)
print(f"{len(times)} edits, avg {sum(times) / max(len(times), 1):.0f} ms, max {max(times, default=0):.0f} ms")
print("problems:", len(problems))
for p in problems: print("  ", p)
