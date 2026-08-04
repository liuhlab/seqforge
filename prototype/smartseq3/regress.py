"""Measure 6 — THE REGRESSION CHECK. Score the whole corpus with the draft `smartseq3` loaded.

Constraint 3 of map #225: no existing case's verdict or score may move. This scores every case in
BOTH tiers (`evals/cases` hermetic + `evals/benchmark` HF) three ways —

  stock    the shipped KB, unchanged
  draft-R  stock + smartseq3 with the anchored tag test in `requires` (a hard gate)
  draft-S  stock + smartseq3 SUPPORTS-ONLY (`requires` carries only the implicit read/length gates)

— and diffs the FULL ranked candidate list per run, not just the winner. `draft-S` is the variant the
ticket asks about explicitly, and it is the one that can move a case: with no gate, `smartseq3` is a
valid candidate on every 2-file dataset in the corpus.

Throwaway prototype for wayfinder #234. NOT production code; nothing under src/ is touched.
"""

from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "src"))

import yaml  # noqa: E402

from seqforge.evals.run import load_cases, materialize  # noqa: E402
from seqforge.kb.loader import load_all_specs  # noqa: E402
from seqforge.kb.schema import MotifPresent, Signature, Spec  # noqa: E402
from seqforge.resolve import resolve_runs  # noqa: E402

DRAFT = Path(__file__).parent / "spec-draft" / "smartseq3" / "spec.yaml"


def draft_specs() -> tuple[Spec, Spec]:
    """(draft-R, draft-S): the same entry with the tag test as a gate, and with no gate at all."""
    r = Spec.model_validate(yaml.safe_load(DRAFT.read_text()))
    s = r.model_copy(
        update={
            "signature": Signature(
                requires=[],  # supports-only: the reads block's min_len/injectivity are the only gates
                supports=r.signature.supports,
                excludes=r.signature.excludes,
            )
        }
    )
    return r, s


def variants() -> dict[str, dict[str, Spec]]:
    stock = load_all_specs()
    r, s = draft_specs()
    return {
        "stock": stock,
        "draft-R": {**stock, "smartseq3": r},
        "draft-S": {**stock, "smartseq3": s},
    }


def snapshot(multi) -> list[dict]:
    """Everything a verdict is made of, per run: the full ranked candidate list, not just the top."""
    out = []
    for run in multi.runs:
        res = run.output.result
        out.append(
            {
                "run": run.run_id,
                "rung": res.rung_reached,
                "candidates": [
                    {
                        "tech": c.technology,
                        "status": c.score.status,
                        "value": c.score.value,
                        "roles": c.role_assignment.model_dump(mode="json"),
                    }
                    for c in res.candidates
                ],
                "conflicts": [c.model_dump(mode="json") for c in res.conflicts],
                "questions": [q.model_dump(mode="json") for q in res.questions],
                "blockers": [b.model_dump(mode="json") for b in res.blockers],
            }
        )
    return out


def main() -> None:
    roots = [REPO / "evals" / "cases", REPO / "evals" / "benchmark"]
    cases = []
    for root in roots:
        cases += load_cases(root)
    print(f"{len(cases)} cases across {[r.name for r in roots]}", file=sys.stderr)

    vs = variants()
    results: dict[str, dict[str, object]] = {}
    for case in cases:
        with tempfile.TemporaryDirectory(prefix="sf-regress-") as tmp:
            try:
                built = materialize(case, Path(tmp) / "inputs")
            except Exception as exc:  # noqa: BLE001 — a skipped case is data, not a failure
                results[case.id] = {"skipped": f"{type(exc).__name__}: {exc}"}
                print(f"  {case.id}: SKIP ({exc})", file=sys.stderr)
                continue
            per: dict[str, object] = {}
            for name, specs in vs.items():
                multi = resolve_runs(
                    built.paths,
                    registry=built.registry,
                    specs=specs,
                    workspace=Path(tmp),
                    use_cache=False,
                    _probed=built.probed,
                )
                per[name] = {"exit_code": multi.exit_code(), "runs": snapshot(multi)}
            results[case.id] = per
            same_r = json.dumps(per["stock"], sort_keys=True) == json.dumps(per["draft-R"], sort_keys=True)
            same_s = json.dumps(per["stock"], sort_keys=True) == json.dumps(per["draft-S"], sort_keys=True)
            print(
                f"  {case.id}: draft-R {'identical' if same_r else 'MOVED'} | "
                f"draft-S {'identical' if same_s else 'MOVED'}",
                file=sys.stderr,
            )

    out = Path(__file__).parent / "results" / "regression.json"
    out.parent.mkdir(exist_ok=True)
    out.write_text(json.dumps(results, indent=1, sort_keys=True))

    print("\n=== REGRESSION SUMMARY ===")
    moved_r, moved_s, skipped = [], [], []
    for cid, per in sorted(results.items()):
        if "skipped" in per:
            skipped.append(cid)
            continue
        a = json.dumps(per["stock"], sort_keys=True)
        if json.dumps(per["draft-R"], sort_keys=True) != a:
            moved_r.append(cid)
        if json.dumps(per["draft-S"], sort_keys=True) != a:
            moved_s.append(cid)
    n = len(results) - len(skipped)
    print(f"cases scored: {n}   skipped (unmaterializable): {len(skipped)} {skipped}")
    print(f"draft-R moved: {len(moved_r)} {moved_r}")
    print(f"draft-S moved: {len(moved_s)} {moved_s}")


if __name__ == "__main__":
    main()
