# Manual stress tool (not collected by pytest):
#   python tests/stress_objects.py file.pdf [more.pdf ...] [--pages 0-15] [--max-per-page N]
#          [--rotate 90] [--dup-every N] [--log path]
"""Move every selectable object of every page by (+30, +20) and back and check
"only the selection changes" (O13), undo is exact (O14), rotated pages work
(O15, ``--rotate``) and timings (O16). Every ``--dup-every``-th object is also
duplicated (Ctrl+D) and copied + pasted into a new document.

Per object, on the page's own document (undo restores it between objects):

1. move (+30, +20): the object is found again at its rect + the delta (text is
   clamped to the page like the UI does), the page outside the old and new
   rect (grown by 3 pt) renders identically, the page's words are unchanged;
2. move back: the page renders like the original and the words are identical
   (text and positions);
3. undo twice: the page's content stream bytes are exactly the original;
4. duplicate (0, +40): original unchanged, more objects, the copy's words are
   added, undo exact; copy + paste into a blank page of a new document: the
   pasted words equal the source object's words, the pasted area renders like
   the object alone.

Problems are printed as a summary table and written (with everything needed
to reproduce them) to the log file.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
import traceback
from collections import Counter, defaultdict

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from pdfstudio.security import netguard  # noqa: E402

netguard.install()

import numpy as np  # noqa: E402
import pymupdf  # noqa: E402

from pdfstudio.core import PdfDocument  # noqa: E402
from pdfstudio.core import pageobjects as po  # noqa: E402
from pdfstudio.core import textobjects as tob  # noqa: E402
from pdfstudio.core.document import write_bytes  # noqa: E402
from pdfstudio.core.pages import parse_page_ranges  # noqa: E402
from pdfstudio.core.render import render_page  # noqa: E402

DX, DY = 30.0, 20.0
DUP = (0.0, 40.0)
POS_TOL = 0.6          # found-again rect tolerance (pt)
GROW = 3.0             # mask around old/new rect (pt)
OUT_TOL = 10           # max channel diff outside the masks after the move
BACK_TOL = 12          # max channel diff of the whole page after moving back
BAD_PX = 40            # a pixel "really differs" above this channel diff
SUBPIXEL = 0.01        # glyph drift (pt) after move and back that is tolerated (< one TJ unit): a note
WORD_TOL = 0.5         # word position tolerance after moving back (pt)


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------
def content_bytes(doc, page: int) -> bytes:
    pg = doc[page]
    return b"\n".join(doc.xref_stream(x) or b"" for x in pg.get_contents())


def pixels(pix: pymupdf.Pixmap) -> np.ndarray:
    a = np.frombuffer(pix.samples, dtype=np.uint8).reshape(pix.height, pix.width, pix.n)
    return a[:, :, :3].astype(np.int16)


def render(doc, page: int, clip=None) -> np.ndarray:
    return pixels(render_page(doc, page, 1.0, clip=clip))


def words(doc, page: int) -> list[tuple]:
    """(text, x0, y0, x1, y1) in display space (get_text works unrotated)."""
    pg = doc[page]
    m = pg.rotation_matrix
    out = []
    for w in pg.get_text("words"):
        r = pymupdf.Rect(w[:4]) * m
        out.append((w[4], r.x0, r.y0, r.x1, r.y1))
    return out


def glyphs(doc, page: int) -> list[tuple]:
    """(char, x, y) of every visible glyph origin (raw extraction order)."""
    out = []
    for b in doc[page].get_text("rawdict")["blocks"]:
        for ln in b.get("lines", ()):
            for sp in ln["spans"]:
                for c in sp["chars"]:
                    if c["c"].strip():
                        out.append((c["c"], c["origin"][0], c["origin"][1]))
    return out


def glyph_drift(a: list, b: list) -> float | None:
    """Largest distance between the same glyphs before and after (greedy per
    character); None when the glyphs differ."""
    if Counter(g[0] for g in a) != Counter(g[0] for g in b):
        return None
    pool = defaultdict(list)
    for g in b:
        pool[g[0]].append(g)
    worst = 0.0
    for g in a:
        cands = pool[g[0]]
        best = min(cands, key=lambda c: abs(c[1] - g[1]) + abs(c[2] - g[2]))
        cands.remove(best)
        worst = max(worst, abs(best[1] - g[1]), abs(best[2] - g[2]))
    return worst


def wcount(ws) -> Counter:
    return Counter(w[0] for w in ws)


def words_in_lines(ws, obj) -> Counter:
    """The words of ``ws`` whose centre lies on one of the text object's lines."""
    rects = [pymupdf.Rect(ln.bbox) for ln in obj.ref.lines] if obj.kind == "text" else []
    out = Counter()
    for w in ws:
        c = pymupdf.Point((w[1] + w[3]) / 2, (w[2] + w[4]) / 2)
        if any(r.contains(c) for r in rects):
            out[w[0]] += 1
    return out


def mask_box(rect, grow: float, shape) -> tuple[int, int, int, int]:
    r = pymupdf.Rect(rect) + (-grow, -grow, grow, grow)
    h, w = shape[:2]
    return (max(0, int(np.floor(r.x0))), max(0, int(np.floor(r.y0))),
            min(w, int(np.ceil(r.x1))), min(h, int(np.ceil(r.y1))))


def diff_outside(a: np.ndarray, b: np.ndarray, rects) -> tuple[int, int, tuple | None]:
    """(max channel diff, pixels above BAD_PX, bbox of those) outside ``rects``."""
    if a.shape != b.shape:
        return 255, -1, None
    d = np.abs(a - b).max(axis=2)
    for r in rects:
        x0, y0, x1, y1 = mask_box(r, GROW, d.shape)
        d[y0:y1, x0:x1] = 0
    bad = np.argwhere(d > BAD_PX)
    box = None
    if len(bad):
        box = (int(bad[:, 1].min()), int(bad[:, 0].min()), int(bad[:, 1].max()) + 1, int(bad[:, 0].max()) + 1)
    return int(d.max()), int(len(bad)), box


def words_same(a: list, b: list) -> tuple[bool, str]:
    """Same words at the same places (greedy per text, WORD_TOL)."""
    if wcount(a) != wcount(b):
        miss = wcount(a) - wcount(b)
        extra = wcount(b) - wcount(a)
        return False, f"text differs: missing {dict(list(miss.items())[:6])} extra {dict(list(extra.items())[:6])}"
    pool = defaultdict(list)
    for w in b:
        pool[w[0]].append(w)
    worst = 0.0
    worst_w = None
    for w in a:
        cands = pool[w[0]]
        best = min(cands, key=lambda c: abs(c[1] - w[1]) + abs(c[2] - w[2]))
        cands.remove(best)
        dist = max(abs(best[1] - w[1]), abs(best[2] - w[2]))
        if dist > worst:
            worst, worst_w = dist, (w, best)
    if worst > WORD_TOL:
        return False, f"word moved {worst:.2f} pt: {worst_w[0][0]!r} {worst_w[0][1:3]} -> {worst_w[1][1:3]}"
    return True, ""


def rect_close(a, b, tol: float = POS_TOL) -> float:
    return max(abs(a.x0 - b.x0), abs(a.y0 - b.y0), abs(a.x1 - b.x1), abs(a.y1 - b.y1))


def seg_sig(o) -> tuple:
    """An object's segmentation: kind, rect (0.1 pt) and text."""
    text = " ".join((getattr(o.ref, "text", "") or "").split()) if o.kind == "text" else ""
    return (o.kind, tuple(round(v, 1) for v in o.rect), text)


def segmentation(doc, page: int) -> Counter:
    return Counter(seg_sig(o) for o in po.page_objects(doc, page))


def fmt_rect(r) -> list:
    return [round(r.x0, 1), round(r.y0, 1), round(r.x1, 1), round(r.y1, 1)]


# ---------------------------------------------------------------------------
# the test
# ---------------------------------------------------------------------------
class Run:
    def __init__(self, args) -> None:
        self.args = args
        self.problems: list[dict] = []
        self.notes: list[dict] = []
        self.objects = Counter()
        self.move_ms: list[float] = []
        self.paste_ms: list[float] = []
        self.dup_ms: list[float] = []
        self.list_ms: list[float] = []
        self.pages = 0

    def problem(self, ctx: dict, what: str, **info) -> None:
        rec = dict(ctx, what=what, **info)
        self.problems.append(rec)
        if self.args.verbose:
            print("   PROBLEM", json.dumps(rec, ensure_ascii=False, default=str), flush=True)

    def note(self, ctx: dict, what: str, **info) -> None:
        self.notes.append(dict(ctx, what=what, **info))

    # -- one document ------------------------------------------------------
    def run_file(self, path: str) -> None:
        src = pymupdf.open(path)
        if self.args.rotate:
            for pg in src:
                pg.set_rotation((pg.rotation + self.args.rotate) % 360)
        data = write_bytes(src, garbage=0)
        n = src.page_count
        src.close()
        pages = parse_page_ranges(self.args.pages, n) if self.args.pages else list(range(n))
        for page in pages:
            pdf = PdfDocument.from_bytes(data, title=os.path.basename(path))
            try:
                self.run_page(path, pdf, page, data)
            except Exception as exc:  # keep going – record it
                self.problem({"pdf": os.path.basename(path), "page": page, "rotate": self.args.rotate},
                             "page crashed", exc=repr(exc), tb=traceback.format_exc()[-1500:])
            finally:
                pdf.doc.close()

    def run_page(self, path: str, pdf: PdfDocument, page: int, data: bytes) -> None:
        name = os.path.basename(path)
        t = time.perf_counter()
        objs = po.page_objects(pdf.doc, page)
        self.list_ms.append((time.perf_counter() - t) * 1000)
        if self.args.max_per_page:
            objs = objs[: self.args.max_per_page]
        self.pages += 1
        base_png = render(pdf.doc, page)
        base_words = words(pdf.doc, page)
        base_content = content_bytes(pdf.doc, page)
        self.base_glyphs = glyphs(pdf.doc, page)
        self.base_seg = segmentation(pdf.doc, page)
        t_page = time.perf_counter()
        for k, obj in enumerate(objs):
            self.objects[obj.kind] += 1
            ctx = {"pdf": name, "page": page, "rotate": self.args.rotate, "obj": k, "kind": obj.kind,
                   "rect": fmt_rect(obj.rect)}
            if obj.kind == "text":
                ctx["text"] = obj.ref.text[:50]
            elif obj.kind == "graphic":
                ctx["gkind"] = obj.ref.kind
            try:
                self.run_object(pdf, page, obj, ctx, base_png, base_words, base_content, k)
            except Exception as exc:
                self.problem(ctx, "crash", exc=repr(exc), tb=traceback.format_exc()[-1500:])
            # whatever happened: back to the original
            while pdf.history.can_undo:
                pdf.undo()
            if content_bytes(pdf.doc, page) != base_content:
                self.problem(ctx, "not restored after object (reloading)")
                pdf.doc.close()
                pdf.__init__(pymupdf.open("pdf", data))
        print(f"{name} p{page:3d} rot{self.args.rotate}: {len(objs):3d} objects "
              f"({dict(Counter(o.kind for o in objs))}) {time.perf_counter() - t_page:6.1f} s, "
              f"problems so far {len(self.problems)}", flush=True)

    def neighbours(self, pdf, page, ctx, what: str, involved_before, involved_now) -> None:
        """Every object not involved keeps its kind, rect and text."""
        want = self.base_seg - Counter(seg_sig(o) for o in involved_before)
        got = segmentation(pdf.doc, page) - Counter(seg_sig(o) for o in involved_now)
        if got != want:
            kinds = sorted({x[0] for x in (want - got) + (got - want)})
            self.problem(ctx, f"neighbour segmentation changed ({what}, {'+'.join(kinds)})",
                         lost=[list(x) for x in (want - got)][:8], new=[list(x) for x in (got - want)][:8])

    def run_object(self, pdf, page, obj, ctx, base_png, base_words, base_content, k) -> None:
        pg = pdf.doc[page]
        base_glyphs = self.base_glyphs
        ex_dx, ex_dy = (tob._keep_on_page(pg, [obj.ref], DX, DY) if obj.kind == "text" else (DX, DY))
        # 1. move – the page is listed first, as the view has done when the
        # object was clicked (after the previous object's undo it is a new document)
        po.page_objects(pdf.doc, page)
        t = time.perf_counter()
        res = po.move_objects(pdf, page, [obj], DX, DY)
        ms = (time.perf_counter() - t) * 1000
        self.move_ms.append(ms)
        ctx_m = dict(ctx, move_ms=round(ms))
        if ms > 500:
            self.problem(ctx_m, "slow move (> 500 ms)")
        if len(res) != 1 or res[0] is None:
            self.problem(ctx_m, "not found after move", got=len(res))
            moved = None
        else:
            moved = res[0]
            want = obj.rect + (ex_dx, ex_dy, ex_dx, ex_dy)
            off = rect_close(moved.rect, want)
            if off > POS_TOL:
                self.problem(ctx_m, "found at wrong rect", want=fmt_rect(want), got=fmt_rect(moved.rect),
                             off=round(off, 2))
        new_rect = obj.rect + (ex_dx, ex_dy, ex_dx, ex_dy)
        self.neighbours(pdf, page, ctx, "move", [obj], [moved] if moved is not None else [])
        png = render(pdf.doc, page)
        mx, nbad, box = diff_outside(base_png, png, [obj.rect, new_rect])
        if mx > OUT_TOL:
            self.problem(ctx_m, "page changed outside the object", max_diff=mx, pixels=nbad, bbox=box)
        ws = words(pdf.doc, page)
        if wcount(ws) != wcount(base_words):
            miss = wcount(base_words) - wcount(ws)
            extra = wcount(ws) - wcount(base_words)
            self.problem(ctx_m, "words changed by move", missing=dict(list(miss.items())[:8]),
                         extra=dict(list(extra.items())[:8]))
        # 2. move back
        if moved is not None:
            res2 = po.move_objects(pdf, page, [moved], -ex_dx, -ex_dy)
            if len(res2) != 1:
                self.problem(ctx, "not found after moving back", got=len(res2))
            else:
                off = rect_close(res2[0].rect, obj.rect)
                if off > POS_TOL:
                    self.problem(ctx, "back at wrong rect", got=fmt_rect(res2[0].rect), off=round(off, 2))
            self.neighbours(pdf, page, ctx, "move back", [obj], res2[:1])
            png2 = render(pdf.doc, page)
            mx, nbad, box = diff_outside(base_png, png2, [])
            if mx > BACK_TOL:
                drift = glyph_drift(base_glyphs, glyphs(pdf.doc, page)) if obj.kind == "text" else None
                if drift is not None and drift < SUBPIXEL and words_same(base_words, words(pdf.doc, page))[0]:
                    # every glyph is where it was within 0.01 pt (below one TJ unit): the
                    # rasteriser's sub-pixel grid flips some edge pixels – "looks as before"
                    self.note(ctx, "moved back: sub-pixel glyph drift < 0.01 pt", max_diff=mx, pixels=nbad,
                              drift=round(drift, 4), bbox=box)
                elif drift is not None:
                    self.problem(ctx, "moved back: glyphs moved >= 0.01 pt", max_diff=mx, pixels=nbad,
                                 bbox=box, drift=round(drift, 4))
                else:
                    self.problem(ctx, "moved back: page differs", max_diff=mx, pixels=nbad, bbox=box)
            ok, why = words_same(base_words, words(pdf.doc, page))
            if not ok:
                self.problem(ctx, "moved back: words differ", detail=why)
            # 3. undo twice
            pdf.undo()
        pdf.undo()
        if content_bytes(pdf.doc, page) != base_content:
            self.problem(ctx, "undo not exact (content stream)")
            return
        # 4. duplicate / copy + paste
        every = self.args.dup_every
        if every and k % every == 0:
            self.dup_and_paste(pdf, page, obj, ctx, base_png, base_words, base_content)

    def dup_and_paste(self, pdf, page, obj, ctx, base_png, base_words, base_content) -> None:
        pg = pdf.doc[page]
        n0 = len(po.page_objects(pdf.doc, page))
        fits = obj.rect.y1 + DUP[1] <= pg.rect.height
        t = time.perf_counter()
        dup = po.duplicate_objects(pdf, page, [obj], offset=DUP)
        self.dup_ms.append((time.perf_counter() - t) * 1000)
        if not dup and fits:
            self.problem(ctx, "duplicate: no new objects returned")
        n1 = len(po.page_objects(pdf.doc, page))
        self.neighbours(pdf, page, ctx, "duplicate", [obj], list(dup) + [o for o in [po.find_again(pdf.doc, page, obj)] if o])
        if fits and n1 <= n0:
            self.problem(ctx, "duplicate: object count did not grow", before=n0, after=n1)
        again = po.find_again(pdf.doc, page, obj)
        if again is None:
            self.problem(ctx, "duplicate: original not found")
        elif rect_close(again.rect, obj.rect) > POS_TOL:
            self.problem(ctx, "duplicate: original moved", got=fmt_rect(again.rect))
        copy_rect = obj.rect + (DUP[0], DUP[1], DUP[0], DUP[1])
        png = render(pdf.doc, page)
        mx, nbad, box = diff_outside(base_png, png, [copy_rect])
        if mx > OUT_TOL:
            self.problem(ctx, "duplicate: page changed outside the copy", max_diff=mx, pixels=nbad, bbox=box)
        if fits and obj.kind == "text":
            want = wcount(base_words) + words_in_lines(base_words, obj)
            got = wcount(words(pdf.doc, page))
            if got != want:
                self.problem(ctx, "duplicate: words", missing=dict(list((want - got).items())[:8]),
                             extra=dict(list((got - want).items())[:8]))
        pdf.undo()
        if content_bytes(pdf.doc, page) != base_content:
            self.problem(ctx, "duplicate: undo not exact")
        # delete: the others stay as they were
        po.delete_objects(pdf, page, [obj])
        self.neighbours(pdf, page, ctx, "delete", [obj], [])
        pdf.undo()
        if content_bytes(pdf.doc, page) != base_content:
            self.problem(ctx, "delete: undo not exact")
        # copy + paste into a new document (undo replaced the document: no stale page)
        clip = po.copy_objects(pdf.doc, page, [obj])
        w, h = pdf.doc[page].rect.width, pdf.doc[page].rect.height
        tgt = PdfDocument.new_blank(w, h)
        try:
            t = time.perf_counter()
            pasted = po.paste_clip(tgt, 0, clip)
            self.paste_ms.append((time.perf_counter() - t) * 1000)
            if not pasted:
                self.problem(ctx, "paste: nothing appeared")
            else:
                u = pymupdf.Rect(pasted[0].rect)
                for p in pasted[1:]:
                    u |= p.rect
                if rect_close(u, obj.rect) > 1.0:
                    self.problem(ctx, "paste: at wrong rect", got=fmt_rect(u))
            want = words_in_lines(base_words, obj)
            got = wcount(words(tgt.doc, 0))
            if got != want:
                self.problem(ctx, "paste: words differ", missing=dict(list((want - got).items())[:8]),
                             extra=dict(list((got - want).items())[:8]))
            r = pymupdf.Rect(obj.rect) & pymupdf.Rect(0, 0, w, h)
            if not r.is_empty and r.width >= 1 and r.height >= 1:
                ghost = po.render_only(pdf.doc, page, [obj], 1.0, clip=r)
                # composite the ghost on white like the pasted page
                a = np.frombuffer(ghost.samples, dtype=np.uint8).reshape(ghost.height, ghost.width, ghost.n)
                al = a[:, :, 3:4].astype(np.float32) / 255.0
                # pymupdf alpha pixmaps are premultiplied
                ref = np.clip(a[:, :, :3].astype(np.float32) + 255.0 * (1 - al), 0, 255).astype(np.int16)
                got_px = render(tgt.doc, 0, clip=r)
                if got_px.shape != ref.shape:
                    hh, ww = min(got_px.shape[0], ref.shape[0]), min(got_px.shape[1], ref.shape[1])
                    got_px, ref = got_px[:hh, :ww], ref[:hh, :ww]
                d = np.abs(got_px - ref).max(axis=2)
                frac = float((d > BAD_PX).mean()) if d.size else 0.0
                if frac > 0.02:
                    self.problem(ctx, "paste: rendering differs", frac_bad=round(frac, 3), max_diff=int(d.max()))
        finally:
            tgt.doc.close()

    # -- report --------------------------------------------------------------
    def report(self, log_path: str, title: str) -> None:
        by = Counter(p["what"] for p in self.problems)
        by_kind = Counter((p["what"], p.get("kind", "-")) for p in self.problems)

        def stat(xs):
            return (f"{sum(xs) / len(xs):7.0f} / {max(xs):6.0f}" if xs else "      - /      -")

        lines = [f"== {title}", f"pages {self.pages}, objects {sum(self.objects.values())} {dict(self.objects)}",
                 f"problems {len(self.problems)}"]
        for what, n in by.most_common():
            kinds = ", ".join(f"{k}:{c}" for (w, k), c in by_kind.items() if w == what)
            lines.append(f"  {n:5d}  {what}  ({kinds})")
        lines.append(f"tolerated notes {len(self.notes)}")
        for what, n in Counter(p["what"] for p in self.notes).most_common():
            lines.append(f"  {n:5d}  {what}")
        lines += [f"page_objects ms avg/max {stat(self.list_ms)}",
                  f"move ms        avg/max {stat(self.move_ms)}  (n={len(self.move_ms)})",
                  f"duplicate ms   avg/max {stat(self.dup_ms)}  (n={len(self.dup_ms)})",
                  f"paste ms       avg/max {stat(self.paste_ms)}  (n={len(self.paste_ms)})"]
        text = "\n".join(lines)
        print(text)
        with open(log_path, "w", encoding="utf-8") as fh:
            fh.write(text + "\n\n")
            for p in self.problems:
                fh.write(json.dumps(p, ensure_ascii=False, default=str) + "\n")
            fh.write("\n# tolerated notes\n")
            for p in self.notes:
                fh.write(json.dumps(dict(p, note=True), ensure_ascii=False, default=str) + "\n")
        print("details:", log_path)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("pdfs", nargs="+")
    ap.add_argument("--pages", default="", help='page ranges, 1-based like "1-16" (default all)')
    ap.add_argument("--max-per-page", type=int, default=0)
    ap.add_argument("--rotate", type=int, default=0, help="run on a copy with every page rotated by N")
    ap.add_argument("--dup-every", type=int, default=5, help="duplicate + copy/paste every N-th object (0 = never)")
    ap.add_argument("--log", default="stress_objects.log")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args(argv)
    if args.rotate % 90:
        ap.error("--rotate must be a multiple of 90")
    run = Run(args)
    for path in args.pdfs:
        run.run_file(path)
    run.report(args.log, " ".join(os.path.basename(p) for p in args.pdfs)
               + (f" rotate {args.rotate}" if args.rotate else ""))
    return 1 if run.problems else 0


if __name__ == "__main__":
    sys.exit(main())
