"""Vertical line positions from a PDF, for verifying rendered spacing.

Paragraph properties matching the template does NOT mean the rendered gaps are
right -- that assumption hid a doubled blank line (36pt against 18pt at the same
kind of boundary) through several rounds of review. This measures the output.

    python tools/measure_pdf_spacing.py resume.pdf

Known limitation: Google Docs exports (the guide's Example Resume) write one Tm
per GLYPH with only a handful of distinct Y values, so per-line positions are
not recoverable from them. LibreOffice output, which is what this pipeline
produces, reads correctly.

Handles both producers.

Google Docs (the guide's Example Resume) writes a flipped Tm per block and then
per-character Td offsets; depth from the top is Tm_f - Td_ty. LibreOffice (our
renders) writes one absolute Td per line with an unflipped page box, so depth is
page_height - y. Both reduce to a list of line depths, which is what spacing
comparison needs.
"""
import re, sys, zlib
from collections import defaultdict

def streams(path):
    data = open(path, "rb").read()
    out = ""
    for m in re.finditer(rb"stream\r?\n", data):
        s = m.end(); e = data.find(b"endstream", s)
        if e < 0: continue
        chunk = data[s:e]
        try:
            out += zlib.decompress(chunk).decode("latin-1") + "\n"
        except Exception:
            # /Filter none — the stream is stored raw, not deflated. Skipping
            # these lost the page content stream entirely on Google Docs exports.
            out += chunk.decode("latin-1", "ignore") + "\n"
    h = re.search(rb"/MediaBox\s*\[\s*[\d.]+\s+[\d.]+\s+([\d.]+)\s+([\d.]+)", data)
    return out, (float(h.group(2)) if h else 792.0)

def lines(path):
    blob, page_h = streams(path)
    rows = defaultdict(lambda: [0.0, ""])
    for chunk in blob.split("BT")[1:]:
        chunk = chunk.split("ET")[0]
        tf = re.search(r"/\w+\s+([\d.]+)\s+Tf", chunk)
        size = float(tf.group(1)) if tf else 0.0
        tm = re.search(r"([\d.\-]+)\s+([\d.\-]+)\s+([\d.\-]+)\s+([\d.\-]+)\s+([\d.\-]+)\s+([\d.\-]+)\s+Tm", chunk)
        tds = re.findall(r"([\d.\-]+)\s+([\d.\-]+)\s+Td", chunk)
        if tm and float(tm.group(4)) < 0:                 # flipped: Google Docs
            f = float(tm.group(6))
            for _, ty in tds:
                if float(ty) != 0.0:
                    depth = round(f - float(ty), 1)
                    rows[depth][0] = max(rows[depth][0], size)
        elif tds:                                          # absolute: LibreOffice
            depth = round(page_h - float(tds[0][1]), 1)
            rows[depth][0] = max(rows[depth][0], size)
    return sorted((d, v[0]) for d, v in rows.items())

if __name__ == "__main__":
    rs = lines(sys.argv[1])
    print(f"{'depth':>8} {'gap':>7} {'size':>6}")
    print("-" * 26)
    prev = None
    for d, s in rs:
        print(f"{d:>8} {('%.1f' % (d - prev)) if prev is not None else '-':>7} {s:>6}")
        prev = d
