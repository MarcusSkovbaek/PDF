# Manual stress tool (not collected by pytest): python tests/stress_edit_all_blocks.py file.pdf
"""Edit every text block of a real-world PDF and check the result."""
import sys, time, traceback
sys.path.insert(0, '/home/user/PDF')
from pdfstudio.security import netguard; netguard.install()
import pymupdf
from pdfstudio.core import PdfDocument, text_edit as te
path = sys.argv[1]
pdf = PdfDocument.open(path)
problems, n, t_total, sources = [], 0, 0.0, {}
for page in range(pdf.page_count):
    count = len(te.text_blocks(pdf.doc, page))
    for i in range(count):
        blocks = te.text_blocks(pdf.doc, page)
        if i >= len(blocks): break
        b = blocks[i]
        if not b.text.strip(): continue
        new = b.text.replace("e", "æ", 1) + " ØÅ"   # changed glyphs incl. ones missing from subsets
        t = time.perf_counter()
        try:
            te.replace_block_text(pdf, b, new)
        except Exception as exc:
            problems.append((page, i, repr(b.text[:30]), "EXC " + repr(exc)[:120])); continue
        t_total += time.perf_counter() - t; n += 1
        got = pdf.page(page).get_text()
        norm = lambda s: " ".join(s.split())
        if norm(new) not in norm(got):
            # report which words went missing
            miss = [w for w in new.split() if w not in got][:5]
            problems.append((page, i, repr(b.text[:30]), f"text mismatch, missing {miss}"))
data = pdf.to_bytes(); d = pymupdf.open("pdf", data)
print(f"{n} blocks edited, avg {1000 * t_total / max(n, 1):.0f} ms, file {len(data) // 1024} KB, repaired={d.is_repaired}")
print("problems:", len(problems))
for p in problems[:15]: print("  ", p)
while pdf.undo(): pass
orig = pymupdf.open(path)
same = all(" ".join(pdf.page(i).get_text().split()) == " ".join(orig[i].get_text().split()) for i in range(pdf.page_count))
print("undo restores original text:", same)
