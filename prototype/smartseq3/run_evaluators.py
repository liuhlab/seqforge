"""Measure 3: RUN every existing evaluator in resolve/evaluators.py against SS3 R1, SS3 R2 and bulk.

Throwaway measurement code for wayfinder #234. Nothing here is production.
"""

from __future__ import annotations

import glob
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, "/Users/hanqing/src/seqforge/.claude/worktrees/agent-a6538a2d0e32ec644/src")

from seqforge.fingerprint.load import load_fingerprint, probed_from_fingerprint  # noqa: E402
from seqforge.io import OnlistRegistry  # noqa: E402
from seqforge.kb.schema import (  # noqa: E402
    BaseComposition,
    DistinctRatio,
    Element,
    HasSegment,
    HeaderIndex,
    MotifPresent,
    Read,
    SegmentLength,
)
from seqforge.resolve.evaluators import evaluate  # noqa: E402
from seqforge.resolve.window import WindowProbe  # noqa: E402

SP = "/private/tmp/claude-501/-Users-hanqing-src-seqforge/e6d5af24-455c-4dc0-aa28-ccb45f262d44/scratchpad"
TAG = "ATTGCGCAATG"

# A minimal SS3-shaped Read, so element-addressed tests resolve. R1 role only; the same object is
# handed to R2/bulk probes so the tests are literally identical across the three populations.
R1 = Read(
    id="R1",
    seqspec_read_id="R1",
    strand="pos",
    min_len=40,
    elements=[
        Element(type="fixed", name="tso_tag", start=0, end=11, sequence=TAG, seqspec_region_type="custom_primer"),
        Element(type="umi", name="umi", start=11, end=19, seqspec_region_type="umi"),
        Element(type="fixed", name="ggg", start=19, end=22, sequence="GGG", seqspec_region_type="custom_primer"),
        Element(type="cdna", name="cdna", start=22, end=None, seqspec_region_type="cdna"),
    ],
)

TESTS = {
    # -- the positional one: motif anchored at read start (the closest existing word to the ask)
    "motif@start mm2 min.50": MotifPresent(test="motif_present", read="R1", motif=TAG, where="read_start", max_mismatch=2, min_rate=0.50),
    "motif@start mm2 min.05": MotifPresent(test="motif_present", read="R1", motif=TAG, where="read_start", max_mismatch=2, min_rate=0.05),
    "motif@start mm2 min.01": MotifPresent(test="motif_present", read="R1", motif=TAG, where="read_start", max_mismatch=2, min_rate=0.01),
    "motif@start mm0 min.05": MotifPresent(test="motif_present", read="R1", motif=TAG, where="read_start", max_mismatch=0, min_rate=0.05),
    "motif anywhere mm2 .05": MotifPresent(test="motif_present", read="R1", motif=TAG, where="anywhere", max_mismatch=2, min_rate=0.05),
    "motif window[0,0] mm2 .05": MotifPresent(test="motif_present", read="R1", motif=TAG, where="window", search_start=0, search_end=0, max_mismatch=2, min_rate=0.05),
    # -- has_segment constant on the tag window (the majority-gated proportion)
    "has_segment const 0-11": HasSegment(test="has_segment", read="R1", start=0, end=11, kind="constant"),
    "has_segment const 0-22": HasSegment(test="has_segment", read="R1", start=0, end=22, kind="constant"),
    "has_segment const 19-22 (GGG)": HasSegment(test="has_segment", read="R1", start=19, end=22, kind="constant"),
    "has_segment random 11-19 (UMI)": HasSegment(test="has_segment", read="R1", start=11, end=19, kind="random"),
    # -- composition
    "base_comp G 19-22 >=0.8": BaseComposition(test="base_composition", read="R1", start=19, end=22, base="G", min_fraction=0.8),
    "base_comp G 19-22 >=0.5": BaseComposition(test="base_composition", read="R1", start=19, end=22, base="G", min_fraction=0.5),
    "base_comp A 0-1 >=0.5": BaseComposition(test="base_composition", read="R1", start=0, end=1, base="A", min_fraction=0.5),
    "base_comp T 1-3 >=0.5": BaseComposition(test="base_composition", read="R1", start=1, end=3, base="T", min_fraction=0.5),
    # -- distinct ratio (supports-only)
    "distinct 0-11 low": DistinctRatio(test="distinct_ratio", read="R1", start=0, end=11, expect="low"),
    "distinct 11-19 high": DistinctRatio(test="distinct_ratio", read="R1", start=11, end=19, expect="high"),
    "distinct 0-25 high (bulk's own)": DistinctRatio(test="distinct_ratio", read="R1", start=0, end=25, expect="high"),
    # -- geometry / header
    "segment_length 151": SegmentLength(test="segment_length", read="R1", length=151, tolerance=0),
    "header_index present": HeaderIndex(test="header_index", present=True),
}


def probes_for(root: str) -> dict[str, WindowProbe]:
    loaded = load_fingerprint(root)
    paths, probed = probed_from_fingerprint(loaded)
    out = {}
    for p in paths:
        mate = "R1" if str(p).endswith("_1.fastq.gz") else "R2"
        obs, seqs = probed[str(p)]
        out[mate] = WindowProbe(observation=obs, seqs=seqs)
    return out


def main() -> None:
    reg = OnlistRegistry(offline=True)
    pops: list[tuple[str, WindowProbe]] = []
    for cell in sorted(glob.glob(os.path.join(SP, "cells", "*")), key=lambda p: int(Path(p).name.split("-")[3])):
        idx = Path(cell).name.split("-")[3]
        wps = probes_for(cell)
        pops.append((f"ss3-{idx}-R1", wps["R1"]))
        pops.append((f"ss3-{idx}-R2", wps["R2"]))
    bulk = probes_for(os.path.join(SP, "bulk"))
    pops.append(("BULK-R1", bulk["R1"]))
    pops.append(("BULK-R2", bulk["R2"]))

    results: dict[str, dict[str, dict]] = {}
    for name, test in TESTS.items():
        results[name] = {}
        for label, wp in pops:
            ev = evaluate(test, R1, wp, spec=None, registry=reg)  # type: ignore[arg-type]
            results[name][label] = {
                "outcome": str(ev.outcome),
                "score": round(ev.score, 4),
                "detail": ev.detail,
            }

    with open(os.path.join(SP, "evaluators.json"), "w") as fh:
        json.dump(results, fh, indent=1)

    # printed table: does this test SEPARATE? (pass on every ss3 R1, fail on every ss3 R2 + bulk)
    ss3_r1 = [lab for lab, _ in pops if lab.startswith("ss3") and lab.endswith("R1")]
    negatives = [lab for lab, _ in pops if lab.endswith("R2") or lab.startswith("BULK")]
    print(f"{'test':<32} {'ss3-R1 outcomes':<26} {'neg outcomes':<26} SEPARATES?")
    for name, per in results.items():
        pos = {per[lab]["outcome"] for lab in ss3_r1}
        neg = {per[lab]["outcome"] for lab in negatives}
        sep = pos == {"pass"} and neg == {"fail"}
        print(f"{name:<32} {str(sorted(pos)):<26} {str(sorted(neg)):<26} {'YES' if sep else 'no'}")
    print()
    for name, per in results.items():
        print(f"--- {name}")
        for lab, r in per.items():
            print(f"    {lab:<14} {r['outcome']:<8} score={r['score']:<8} {r['detail']}")


if __name__ == "__main__":
    main()
