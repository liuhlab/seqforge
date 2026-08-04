"""Where does the off-offset tag background come from? Characterize it per cell and per offset.

Throwaway measurement code for wayfinder #234.
"""

from __future__ import annotations

import glob
import gzip
import os
from collections import Counter
from pathlib import Path

SP = "/private/tmp/claude-501/-Users-hanqing-src-seqforge/e6d5af24-455c-4dc0-aa28-ccb45f262d44/scratchpad"
TAG = "ATTGCGCAATG"
MM = 2


def reads(path: str) -> list[str]:
    out = []
    with gzip.open(path, "rt") as fh:
        for i, line in enumerate(fh):
            if i % 4 == 1:
                out.append(line.strip())
    return out


def ham_le(a: str, b: str, k: int) -> bool:
    n = 0
    for x, y in zip(a, b):
        if x != y:
            n += 1
            if n > k:
                return False
    return True


def offsets(seqs: list[str], scan_end: int = 40) -> Counter:
    h: Counter = Counter()
    for s in seqs:
        for off in range(0, min(len(s) - 11, scan_end) + 1):
            if ham_le(s[off : off + 11], TAG, MM):
                h[off] += 1
    return h


print("Per-cell R1: split into tagged (match at offset 0) and untagged; the UNTAGGED half's own")
print("off-offset tag hits are the background a diluted (low-tag-fraction) library would carry.\n")
print(f"{'cell':>6} {'n':>6} {'tagged':>7} {'untag':>6} {'untag_bg_hits':>14} {'bg_rate':>9} {'top offsets in untagged'}")
allunt: list[str] = []
for cell in sorted(glob.glob(os.path.join(SP, "cells", "*")), key=lambda p: int(Path(p).name.split("-")[3])):
    idx = Path(cell).name.split("-")[3]
    seqs = reads(glob.glob(os.path.join(cell, "fastq", "*_1.fastq.gz"))[0])
    tg = [s for s in seqs if ham_le(s[0:11], TAG, MM)]
    un = [s for s in seqs if not ham_le(s[0:11], TAG, MM)]
    allunt += un
    h = offsets(un)
    tot = sum(h.values())
    top = ", ".join(f"{k}:{v}" for k, v in h.most_common(5))
    print(f"{idx:>6} {len(seqs):>6} {len(tg):>7} {len(un):>6} {tot:>14} {tot/max(1,len(un))*100:>8.2f}% {top}")

h = offsets(allunt)
print(f"\npooled untagged n={len(allunt)}  total off-offset hits(0..40)={sum(h.values())}")
print("offset histogram (untagged pool):", dict(sorted(h.items())))

bulk = reads(sorted(glob.glob(os.path.join(SP, "bulk", "fastq", "*_1.fastq.gz")))[0])
hb = offsets(bulk)
print(f"\nbulk R1 n={len(bulk)} off-offset hits(0..40)={sum(hb.values())}", dict(sorted(hb.items())))

# is the background reverse-complement TSO (read-through into the other end)? check the revcomp motif
RC = "CATTGCGCAAT"
def rcoff(seqs):
    h = Counter()
    for s in seqs:
        for off in range(0, len(s) - 11 + 1):
            if ham_le(s[off:off+11], RC, MM):
                h[off] += 1
    return h
hr = rcoff(allunt)
print(f"\nrevcomp tag CATTGCGCAAT in untagged pool: total={sum(hr.values())} top={hr.most_common(5)}")
