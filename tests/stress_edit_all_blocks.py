# Manual stress tool (not collected by pytest): python tests/stress_edit_all_blocks.py file.pdf
"""Edit every text block of a real-world PDF and check the result."""
import sys, time, traceback
sys.path.insert(0, '/home/user/PDF')
from pdfstudio.security import netguard; netguard.install()
import pymupdf
from pdfstudio.core import PdfDocument, text_edit as te
path = sys.argv[1]
pdf = PdfDocument.open(path)
pdf.history.max_steps = 100000   # keep every step so the undo check below is exact
problems, n, t_total, sources = [], 0, 0.0, {}


def overlaps(page) -> int:
    """Pairs of text lines whose boxes overlap noticeably (edited text running
    into the text below)."""
    lines = [pymupdf.Rect(l["bbox"]) for b in page.get_text("dict")["blocks"] for l in b.get("lines", [])
             if "".join(s["text"] for s in l["spans"]).strip()]
    count = 0
    for i, a in enumerate(lines):
        for b in lines[i + 1:]:
            r = a & b
            if not r.is_empty and r.height > 0.35 * min(a.height, b.height) and r.width > 3:
                count += 1
    return count


moved_total, overflows = 0, 0
for page in range(pdf.page_count):
    count = len(te.text_blocks(pdf.doc, page))
    for i in range(count):
        blocks = te.text_blocks(pdf.doc, page)
        if i >= len(blocks): break
        b = blocks[i]
        if not b.text.strip() or te.is_list_bullet(b): continue   # the UI does not edit bullets either
        new = b.text.replace("e", "æ", 1) + " ØÅ"   # changed glyphs incl. ones missing from subsets
        t = time.perf_counter()
        before = overlaps(pdf.page(page))
        try:
            res = te.replace_block_text(pdf, b, new)
            moved_total += res.moved
            overflows += res.overflow > 0
            after = overlaps(pdf.page(page))
            if after > before and not res.overflow:
                problems.append((page, i, repr(b.text[:30]), f"new overlap ({before} -> {after})"))
        except Exception as exc:
            problems.append((page, i, repr(b.text[:30]), "EXC " + repr(exc)[:120])); continue
        t_total += time.perf_counter() - t; n += 1
        # compare with the editable blocks (page.get_text() lists rewritten
        # lines after the untouched ones, in content-stream order)
        got = "\n".join(blk.text for blk in te.text_blocks(pdf.doc, page))
        norm = lambda s: " ".join(s.split())
        if norm(new) not in norm(got):
            # report which words went missing
            miss = [w for w in new.split() if w not in got][:5]
            kind = "text mismatch" if miss or not res.overflow else "wrapped in full cell (overflow reported)"
            problems.append((page, i, repr(b.text[:30]), f"{kind}, missing {miss}"))
data = pdf.to_bytes(); d = pymupdf.open("pdf", data)
print(f"{n} blocks edited, avg {1000 * t_total / max(n, 1):.0f} ms, file {len(data) // 1024} KB, repaired={d.is_repaired}")
print(f"lines moved along: {moved_total}, edits reporting overflow: {overflows}")
print("problems:", len(problems))
kinds = {}
for p in problems:
    kinds[p[3].split(" (")[0].split(",")[0]] = kinds.get(p[3].split(" (")[0].split(",")[0], 0) + 1
print("by kind:", kinds)
for p in problems[:25]: print("  ", p)
steps = len(pdf.history._undo)
while pdf.undo(): pass
print(f"(undo history keeps {steps} steps)")
orig = pymupdf.open(path)
same = all(" ".join(pdf.page(i).get_text().split()) == " ".join(orig[i].get_text().split()) for i in range(pdf.page_count))
print("undo restores original text:", same)
