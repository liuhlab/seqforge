"""Measure 4 — the PROPOSED new evaluator, written to the shape `resolve/evaluators.py` would need,
and then measured against the populations that decide whether it is worth a word in the DSL.

The ticket's ask: *"a fixed motif whose hits pile up at one offset far above background, at any
frequency above a floor"*. That is three numbers, not one:

    rate  = share of reads carrying the motif AT the declared offset
    bg    = the best rival offset's share, over a bounded scan window
    conc  = hits at the declared offset / hits at ANY offset      ("purity", in [0,1])

`conc` is the threshold-free part — it is a SHAPE, so in principle it does not move with the tag
fraction. Whether that principle survives contact with a low-tag-fraction library is the measurement
this file exists for, and the answer is in the `--floor` table: it does not.

THROWAWAY. Deliberately not wired into `kb/schema.py` or `resolve/evaluators.py`; map #225 forbids
production code, and a DSL word is exactly the kind of thing you do not add before the numbers.
"""

from __future__ import annotations

import glob
import json
import random
import sys
from dataclasses import dataclass
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
SP = "/private/tmp/claude-501/-Users-hanqing-src-seqforge/e6d5af24-455c-4dc0-aa28-ccb45f262d44/scratchpad"
sys.path.insert(0, str(REPO / "src"))

from seqforge.fingerprint.load import load_fingerprint, probed_from_fingerprint  # noqa: E402
from seqforge.resolve.window import WindowProbe  # noqa: E402

TAG = "ATTGCGCAATG"


@dataclass(frozen=True)
class Result:
    outcome: str  # pass / fail / abstain
    score: float
    n: int
    hits_at_offset: int
    rate: float
    best_rival_offset: int | None
    bg_rate: float
    concentration: float | None
    detail: str


def _matches(window: str, motif: str, max_mismatch: int) -> bool:
    mism = 0
    for a, b in zip(window, motif, strict=True):
        if a != b:
            mism += 1
            if mism > max_mismatch:
                return False
    return True


def motif_offset_concentration(
    seqs: list[str],
    motif: str,
    *,
    offset: int = 0,
    max_mismatch: int = 2,
    scan_end: int = 40,
    min_rate: float = 0.02,
    min_concentration: float = 0.5,
) -> Result:
    """PASS iff the motif sits at ``offset`` above ``min_rate`` AND concentrates there above background.

    ABSTAIN is reserved for "no read reaches this column" — the probe could not look. A read population
    that reaches the column and does not carry the motif is a FAIL: absent is not unobservable, and
    conflating the two is what `ABSTAIN never gates` exists to prevent.
    """
    m = len(motif)
    reach = [s for s in seqs if len(s) >= offset + m]
    if not reach:
        return Result("abstain", 0.0, 0, 0, 0.0, None, 0.0, None, "no read reaches this column")
    hits: dict[int, int] = {}
    for s in reach:
        for k in range(0, min(len(s) - m, scan_end) + 1):
            if _matches(s[k : k + m], motif, max_mismatch):
                hits[k] = hits.get(k, 0) + 1
    n = len(reach)
    at = hits.get(offset, 0)
    rivals = {k: v for k, v in hits.items() if k != offset}
    best_k = max(rivals, key=lambda k: rivals[k]) if rivals else None
    best_v = rivals[best_k] if best_k is not None else 0
    total = sum(hits.values())
    rate = at / n
    conc = (at / total) if total else None
    ok = rate >= min_rate and conc is not None and conc >= min_concentration
    # score: the SHAPE, floored by the frequency. Zero below the floor so a chance hit at offset 0 in a
    # library with no motif anywhere cannot score 1.0 off a denominator of one.
    score = (conc if conc is not None else 0.0) if rate >= min_rate else 0.0
    return Result(
        "pass" if ok else "fail",
        round(score, 4),
        n,
        at,
        round(rate, 5),
        best_k,
        round(best_v / n, 5),
        None if conc is None else round(conc, 4),
        f"rate@{offset}={rate:.4f} bg={best_v}@{best_k} conc={conc}",
    )


# ---------------------------------------------------------------- measurement
def _reads_of(root: str) -> dict[str, list[str]]:
    loaded = load_fingerprint(root)
    paths, probed = probed_from_fingerprint(loaded)
    out = {}
    for p in paths:
        mate = "R1" if str(p).endswith("_1.fastq.gz") else "R2"
        obs, seqs = probed[str(p)]
        out[mate] = WindowProbe(observation=obs, seqs=seqs).seqs
    return out


def main() -> None:
    print("=== the prototype evaluator on every population (min_rate=0.02, min_conc=0.5) ===")
    print(f"{'population':<16} {'outcome':<8} {'score':<8} {'n':>6} {'hits@0':>7} {'rate@0':>9} {'bg':>9} {'conc':>7}")
    rows = {}
    roots = sorted(glob.glob(f"{SP}/cells/*"), key=lambda p: int(Path(p).name.split("-")[3]))
    for root in roots + [f"{SP}/bulk"]:
        name = Path(root).name.split("-")[3] if "cells" in root else "BULK"
        for mate, seqs in sorted(_reads_of(root).items()):
            r = motif_offset_concentration(seqs, TAG)
            rows[f"{name}-{mate}"] = r.__dict__
            print(f"{name+'-'+mate:<16} {r.outcome:<8} {r.score:<8} {r.n:>6} {r.hits_at_offset:>7} "
                  f"{r.rate:>9} {r.bg_rate:>9} {str(r.concentration):>7}")

    print("\n=== the FLOOR: dilute real tagged reads into real UNTAGGED Smart-seq3 reads ===")
    print("(the honest diluent — #226 proves an untagged Smart-seq3 R1 is byte-identical to bulk cDNA)")
    tagged, untagged = [], []
    for root in roots:
        for mate, seqs in _reads_of(root).items():
            if mate != "R1":
                continue
            for s in seqs:
                (tagged if _matches(s[0:11], TAG, 2) else untagged).append(s)
    bulk_r1 = _reads_of(f"{SP}/bulk")["R1"]
    rng = random.Random(20260804)
    print(f"{'target':>8} {'diluent':<10} {'outcome':<8} {'score':<8} {'rate@0':>9} {'conc':>7}")
    for diluent_name, diluent in (("ss3-untag", untagged), ("bulk", bulk_r1)):
        for f in (0.705, 0.40, 0.20, 0.10, 0.069, 0.05, 0.02, 0.01, 0.005, 0.0):
            k = int(round(2000 * f))
            mix = rng.sample(tagged, k) + rng.sample(diluent, 2000 - k)
            r = motif_offset_concentration(mix, TAG)
            print(f"{f*100:>7.1f}% {diluent_name:<10} {r.outcome:<8} {r.score:<8} {r.rate:>9} {str(r.concentration):>7}")

    (Path(__file__).parent / "results").mkdir(exist_ok=True)
    (Path(__file__).parent / "results" / "prototype_evaluator.json").write_text(json.dumps(rows, indent=1))


if __name__ == "__main__":
    main()
