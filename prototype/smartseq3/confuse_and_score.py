"""Measure 5 (confusability + round-trip) and the positive half of 6 (does the draft actually WIN?).

  A. `rung02_separable(smartseq3, bulk-rnaseq-pe)` BOTH directions, plus every other KB leaf.
  B. `backend_identical(smartseq3, bulk-rnaseq-pe)` — the CI biconditional's input.
  C. `kb roundtrip` on the draft (a Spec-taking copy of `run_roundtrip`).
  D. resolve the ten GSE207085 cells and the bulk control with stock / draft-R / draft-S.

Throwaway prototype for wayfinder #234.
"""

from __future__ import annotations

import glob
import json
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
SP = "/private/tmp/claude-501/-Users-hanqing-src-seqforge/e6d5af24-455c-4dc0-aa28-ccb45f262d44/scratchpad"
sys.path.insert(0, str(REPO / "src"))

import yaml  # noqa: E402

from seqforge import kb  # noqa: E402
from seqforge.fingerprint.load import load_fingerprint, probed_from_fingerprint  # noqa: E402
from seqforge.io import DEFAULT_REGISTRY  # noqa: E402
from seqforge.kb.generate import generate_reads, write_fastq_gz  # noqa: E402
from seqforge.kb.schema import Signature, Spec  # noqa: E402
from seqforge.probe import probe_file  # noqa: E402
from seqforge.probe.signals import distinct_ratio, window_bases  # noqa: E402
from seqforge.resolve import resolve_dataset  # noqa: E402
from seqforge.resolve.confuse import (  # noqa: E402
    accepts_at_rungs_0_2,
    backend_identical,
    rung02_separable,
)
from seqforge.resolve.window import WindowProbe  # noqa: E402

DRAFT_PATH = Path(__file__).parent / "spec-draft" / "smartseq3" / "spec.yaml"


def load_drafts() -> tuple[Spec, Spec]:
    r = Spec.model_validate(yaml.safe_load(DRAFT_PATH.read_text()))
    s = r.model_copy(
        update={
            "signature": Signature(
                requires=[], supports=r.signature.supports, excludes=r.signature.excludes
            )
        }
    )
    return r, s


def probes_of(spec: Spec, workdir: Path, n: int = 400) -> list[WindowProbe]:
    reads = generate_reads(spec, n=n, seed=0)
    out = []
    for read_id, seqs in reads.items():
        p = workdir / f"{spec.identity.id}_{read_id}.fastq.gz"
        write_fastq_gz(p, seqs)
        out.append(WindowProbe(observation=probe_file(p), seqs=seqs[:200]))
    return out


def roundtrip_spec(spec: Spec, n: int = 2000) -> dict:
    """`run_roundtrip`'s checks, against a Spec object instead of a kb/specs directory id."""
    reads = generate_reads(spec, n=n, seed=0)
    checks: list[dict] = []
    with tempfile.TemporaryDirectory() as td:
        for read in spec.reads:
            seqs = reads[read.id]
            path = Path(td) / f"{read.id}.fastq.gz"
            write_fastq_gz(path, seqs)
            obs = probe_file(path)
            if read.min_len is not None and read.min_len == read.max_len:
                checks.append({"read": read.id, "check": "length",
                               "ok": obs.read_length.mode == read.min_len})
            has_open = any(el.type in ("cdna", "gdna") and el.end is None for el in read.elements)
            if has_open and read.min_len != read.max_len:
                checks.append({"read": read.id, "check": "cdna_variable",
                               "ok": obs.read_length.n_distinct > 1,
                               "n_distinct": obs.read_length.n_distinct})
            for el in read.elements:
                if el.start is None or el.end is None:
                    continue
                ratio = distinct_ratio(window_bases(seqs, el.start, el.end))
                if el.type == "barcode" and el.onlist:
                    checks.append({"read": read.id, "check": f"barcode_recurs:{el.name}",
                                   "ok": ratio is not None and ratio < 0.5, "ratio": ratio})
                elif el.type == "umi":
                    checks.append({"read": read.id, "check": f"umi_unique:{el.name}",
                                   "ok": ratio is not None and ratio > 0.7, "ratio": ratio})
    return {"tech": spec.identity.id, "passed": all(c["ok"] for c in checks), "checks": checks}


def main() -> None:
    draft_r, draft_s = load_drafts()
    stock = kb.load_all_specs()

    with tempfile.TemporaryDirectory(prefix="sf-confuse-") as td:
        wd = Path(td)
        kb_probes = {i: probes_of(kb.load_spec(i), wd) for i in kb.list_spec_ids()}
        ss3_probes = probes_of(draft_r, wd)

        print("=== A. rung02_separable(smartseq3, X), both directions, over every KB spec ===")
        print(f"{'other':<28} {'ss3 accepts other':<19} {'other accepts ss3':<19} separable?")
        for other_id, oprobes in sorted(kb_probes.items()):
            other = kb.load_spec(other_id)
            a = accepts_at_rungs_0_2(draft_r, oprobes)
            b = accepts_at_rungs_0_2(other, ss3_probes)
            sep = rung02_separable(draft_r, ss3_probes, other, oprobes)
            print(f"{other_id:<28} {str(a):<19} {str(b):<19} {sep}")

        print("\n--- the same, for the SUPPORTS-ONLY draft (no requires gate at all) ---")
        for other_id in ("bulk-rnaseq-pe", "splitseq", "10x-3p-gex-v3"):
            other = kb.load_spec(other_id)
            a = accepts_at_rungs_0_2(draft_s, kb_probes[other_id])
            b = accepts_at_rungs_0_2(other, ss3_probes)
            print(f"{other_id:<28} {str(a):<19} {str(b):<19} "
                  f"{rung02_separable(draft_s, ss3_probes, other, kb_probes[other_id])}")

        print("\n=== B. backend_identical (the CI biconditional's input) ===")
        for other_id in sorted(stock):
            if stock[other_id].backend is None:
                continue
            same = backend_identical(draft_r, stock[other_id])
            if same:
                print(f"  backend_identical(smartseq3, {other_id}) = TRUE  <-- forces "
                      f"`processing_equivalent`, which is WRONG for this pair")
        print("  (only TRUEs printed; a TRUE here is a defect in the draft's placeholder backend)")

    print("\n=== C. kb roundtrip on the draft ===")
    rt = roundtrip_spec(draft_r)
    print(json.dumps(rt, indent=1))

    print("\n=== D. resolve the ten cells + the bulk control ===")
    variants = {"stock": stock, "draft-R": {**stock, "smartseq3": draft_r},
                "draft-S": {**stock, "smartseq3": draft_s}}
    roots = sorted(glob.glob(f"{SP}/cells/*"), key=lambda p: int(Path(p).name.split("-")[3]))
    roots.append(f"{SP}/bulk")
    print(f"{'dataset':<10} {'variant':<9} {'winner':<20} {'score':<9} {'runner-up':<20} {'score':<9} rung")
    table = {}
    for root in roots:
        name = Path(root).name.split("-")[3] if "cells" in root else "BULK"
        loaded = load_fingerprint(root)
        paths, probed = probed_from_fingerprint(loaded)
        table[name] = {}
        for vname, specs in variants.items():
            with tempfile.TemporaryDirectory(prefix="sf-score-") as td:
                out = resolve_dataset(paths, registry=DEFAULT_REGISTRY, specs=specs,
                                      workspace=Path(td), use_cache=False, _probed=probed)
            c = out.result.candidates
            top = (c[0].technology, c[0].score.value) if c else (None, None)
            run2 = (c[1].technology, c[1].score.value) if len(c) > 1 else (None, None)
            table[name][vname] = {"top": top, "second": run2, "rung": out.result.rung_reached,
                                  "questions": len(out.result.questions),
                                  "conflicts": len(out.result.conflicts),
                                  "blockers": len(out.result.blockers)}
            print(f"{name:<10} {vname:<9} {str(top[0]):<20} {str(round(top[1],4) if top[1] else top[1]):<9} "
                  f"{str(run2[0]):<20} {str(round(run2[1],4) if run2[1] else run2[1]):<9} "
                  f"{out.result.rung_reached}  q={len(out.result.questions)} "
                  f"c={len(out.result.conflicts)} b={len(out.result.blockers)}")
    (Path(__file__).parent / "results").mkdir(exist_ok=True)
    (Path(__file__).parent / "results" / "cells.json").write_text(json.dumps(table, indent=1))


if __name__ == "__main__":
    main()
