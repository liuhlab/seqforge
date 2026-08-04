"""Measure 1+2: per-cell tag fraction and OFFSET HISTOGRAM of tag hits, on R1, R2 and a bulk control.

Throwaway measurement code for wayfinder #234. Nothing here is production.
"""

from __future__ import annotations

import glob
import gzip
import json
import os
import random
import sys
from collections import Counter

SP = "/private/tmp/claude-501/-Users-hanqing-src-seqforge/e6d5af24-455c-4dc0-aa28-ccb45f262d44/scratchpad"
TAG = "ATTGCGCAATG"          # SMART-seq3 TSO tag, 11 bp, expected at offset 0
ME = "CTGTCTCTTATACACATCT"   # Tn5 mosaic end read-through, 19 bp, expected NOWHERE in particular


def reads(path: str, limit: int = 100000) -> list[str]:
    out = []
    with gzip.open(path, "rt") as fh:
        for i, line in enumerate(fh):
            if i % 4 == 1:
                out.append(line.strip())
                if len(out) >= limit:
                    break
    return out


def hamming_le(a: str, b: str, k: int) -> bool:
    n = 0
    for x, y in zip(a, b):
        if x != y:
            n += 1
            if n > k:
                return False
    return True


def offset_hist(seqs: list[str], motif: str, mm: int, scan_to: int | None = None) -> Counter:
    """Count, per offset, the number of READS carrying `motif` at that offset (<= mm mismatches).

    A read may contribute to several offsets; in practice it contributes to at most one.
    """
    m = len(motif)
    hist: Counter = Counter()
    for s in seqs:
        limit = len(s) - m if scan_to is None else min(len(s) - m, scan_to)
        for off in range(0, limit + 1):
            if hamming_le(s[off : off + m], motif, mm):
                hist[off] += 1
    return hist


def summarize(seqs: list[str], motif: str, mm: int, label: str) -> dict:
    n = len(seqs)
    hist = offset_hist(seqs, motif, mm)
    at0 = hist.get(0, 0)
    others = {k: v for k, v in hist.items() if k != 0}
    best_other = max(others.values()) if others else 0
    best_other_off = max(others, key=lambda k: others[k]) if others else None
    # background restricted to the first 40 bp, the window #226/#230 quoted
    others40 = {k: v for k, v in others.items() if k <= 40}
    best_other40 = max(others40.values()) if others40 else 0
    total = sum(hist.values())
    return {
        "label": label,
        "n_reads": n,
        "motif": motif,
        "max_mismatch": mm,
        "hits_at_0": at0,
        "rate_at_0": at0 / n if n else 0.0,
        "best_nonzero_hits": best_other,
        "best_nonzero_offset": best_other_off,
        "best_nonzero_hits_first40": best_other40,
        "total_hits_all_offsets": total,
        # the three candidate concentration statistics
        "purity": at0 / total if total else None,                       # share of all hits at offset 0
        "enrich_vs_best_other": at0 / best_other if best_other else None,
        "enrich_vs_best_other_first40": at0 / best_other40 if best_other40 else None,
        "mean_bg_per_offset": (total - at0) / max(1, len(set(range(1, 141)))),
    }


def main() -> None:
    out = {"cells": [], "bulk": [], "controls": []}
    cells = sorted(glob.glob(os.path.join(SP, "cells", "*")))
    for cell in cells:
        idx = os.path.basename(cell).split("-")[3]
        fqs = sorted(glob.glob(os.path.join(cell, "fastq", "*.fastq.gz")))
        rec: dict = {"cell": idx, "dir": os.path.basename(cell)}
        for fq in fqs:
            mate = "R1" if fq.endswith("_1.fastq.gz") else "R2"
            seqs = reads(fq)
            rec[f"{mate}_exact"] = summarize(seqs, TAG, 0, f"{idx}/{mate}/exact")
            rec[f"{mate}_mm2"] = summarize(seqs, TAG, 2, f"{idx}/{mate}/mm2")
            rec[f"{mate}_ME"] = summarize(seqs, ME, 1, f"{idx}/{mate}/mosaic-end")
            rec[f"{mate}_len"] = Counter(len(s) for s in seqs).most_common(1)[0]
        out["cells"].append(rec)

    # bulk control: GSE283483-bulk, the benchmark case that resolves to bulk-rnaseq-pe
    for fq in sorted(glob.glob(os.path.join(SP, "bulk", "fastq", "*.fastq.gz"))):
        mate = "R1" if fq.endswith("_1.fastq.gz") else "R2"
        seqs = reads(fq)
        out["bulk"].append(
            {
                "mate": mate,
                "file": os.path.basename(fq),
                "exact": summarize(seqs, TAG, 0, f"bulk/{mate}/exact"),
                "mm2": summarize(seqs, TAG, 2, f"bulk/{mate}/mm2"),
                "ME": summarize(seqs, ME, 1, f"bulk/{mate}/mosaic-end"),
                "mode_len": Counter(len(s) for s in seqs).most_common(1)[0],
            }
        )

    # negative control: random 11-mers against the same reads, so "background" is measured not assumed
    rng = random.Random(20260804)
    randmers = ["".join(rng.choice("ACGT") for _ in range(11)) for _ in range(5)]
    deep = os.path.join(SP, "cells", "GSE207085-nasal-prox1-804-ee3352c6ea64", "fastq")
    r1 = reads(sorted(glob.glob(os.path.join(deep, "*_1.fastq.gz")))[0])
    bulk_r1 = reads(sorted(glob.glob(os.path.join(SP, "bulk", "fastq", "*_1.fastq.gz")))[0])
    for mer in randmers:
        out["controls"].append(summarize(r1, mer, 2, f"ss3-cell804-R1/random-{mer}"))
        out["controls"].append(summarize(bulk_r1, mer, 2, f"bulk-R1/random-{mer}"))

    with open(os.path.join(SP, "offsets.json"), "w") as fh:
        json.dump(out, fh, indent=1)
    print("wrote offsets.json", file=sys.stderr)


if __name__ == "__main__":
    main()
