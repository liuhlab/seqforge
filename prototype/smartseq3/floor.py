"""Measure 4: the FLOOR. Dilute the tagged population down to (and past) the 6.9% known-good low case.

A low-tag-fraction SMART-seq3 library is, by #226's citable finding, tagged reads plus internal
fragments that are byte-identical to bulk. So the honest dilution keeps real tagged reads and pads with
real UNTAGGED reads from the same libraries — and, as a cross-check, with real bulk reads.

Reports, at each mixture: the anchored rate at offset 0 (what `motif_present where=read_start`
measures), and the prototype's positional-concentration statistics (purity, enrichment vs best other
offset). Throwaway measurement code for wayfinder #234.
"""

from __future__ import annotations

import glob
import gzip
import json
import os
import random
from collections import Counter

SP = "/private/tmp/claude-501/-Users-hanqing-src-seqforge/e6d5af24-455c-4dc0-aa28-ccb45f262d44/scratchpad"
TAG = "ATTGCGCAATG"
MM = 2
N_MIX = 2000
SCAN_END = 40  # background window, the one #226/#230 quoted


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


def hist(seqs: list[str], scan_end: int = SCAN_END) -> Counter:
    m = len(TAG)
    h: Counter = Counter()
    for s in seqs:
        for off in range(0, min(len(s) - m, scan_end) + 1):
            if ham_le(s[off : off + m], TAG, MM):
                h[off] += 1
    return h


def stats(seqs: list[str]) -> dict:
    n = len(seqs)
    h = hist(seqs)
    at0 = h.get(0, 0)
    others = {k: v for k, v in h.items() if k != 0}
    best = max(others.values()) if others else 0
    tot = sum(h.values())
    return {
        "n": n,
        "hits@0": at0,
        "rate@0": at0 / n if n else 0.0,
        "best_other": best,
        "total_hits": tot,
        "purity": (at0 / tot) if tot else None,
        "enrich": (at0 / best) if best else (float("inf") if at0 else None),
    }


def main() -> None:
    tagged: list[str] = []
    untagged: list[str] = []
    for cell in sorted(glob.glob(os.path.join(SP, "cells", "*"))):
        for fq in glob.glob(os.path.join(cell, "fastq", "*_1.fastq.gz")):
            for s in reads(fq):
                (tagged if ham_le(s[0:11], TAG, MM) else untagged).append(s)
    bulk = reads(sorted(glob.glob(os.path.join(SP, "bulk", "fastq", "*_1.fastq.gz")))[0])
    print(f"pool: tagged={len(tagged)} untagged={len(untagged)} bulk_r1={len(bulk)}")

    rng = random.Random(20260804)
    fracs = [0.705, 0.40, 0.20, 0.10, 0.069, 0.05, 0.02, 0.01, 0.005, 0.002, 0.001, 0.0]
    out = {"ss3_diluent": [], "bulk_diluent": []}
    for key, diluent in (("ss3_diluent", untagged), ("bulk_diluent", bulk)):
        for f in fracs:
            k = int(round(N_MIX * f))
            mix = rng.sample(tagged, k) + rng.sample(diluent, N_MIX - k)
            rng.shuffle(mix)
            s = stats(mix)
            s["target_frac"] = f
            s["n_tagged_in"] = k
            out[key].append(s)

    with open(os.path.join(SP, "floor.json"), "w") as fh:
        json.dump(out, fh, indent=1)

    for key in out:
        print(f"\n=== diluent: {key} (n={N_MIX} reads per mixture, mm<={MM}, scan 0..{SCAN_END}) ===")
        print(f"{'target':>7} {'tagged':>7} {'hits@0':>7} {'rate@0':>8} {'bestOth':>8} {'purity':>8} {'enrich':>8}")
        for s in out[key]:
            p = "n/a" if s["purity"] is None else f"{s['purity']:.4f}"
            e = "n/a" if s["enrich"] is None else ("inf" if s["enrich"] == float("inf") else f"{s['enrich']:.1f}")
            print(
                f"{s['target_frac']*100:>6.1f}% {s['n_tagged_in']:>7} {s['hits@0']:>7} "
                f"{s['rate@0']*100:>7.3f}% {s['best_other']:>8} {p:>8} {e:>8}"
            )


if __name__ == "__main__":
    main()
