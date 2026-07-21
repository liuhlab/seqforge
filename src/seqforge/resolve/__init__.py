"""``resolve`` — the scoring engine: bytes + KB (+ optional hypothesis) -> a ranked, escalated verdict.

Deterministic and LLM-free. Signature-test evaluators score a JSON-safe evidence matrix
``M[role][file]``; a cardinality-normalized joint role-assignment picks the best injective
files->roles map per technology; escalation turns the ranked candidates into exactly one of
``Decision`` / ``Conflict`` / ``Question`` / ``Blocker`` with rung provenance. Every artifact is
content-addressed under ``.seqforge/``. The only interpretive input is a span-verified
``hypothesis`` that steers control flow — it never enters the matrix (§3.4).
"""

from __future__ import annotations

#: CalVer YYYY.M.PATCH; bumped when scoring/assignment/escalation semantics change. Folded into the
#: dataset cache key so a resolver change invalidates stale candidates.
#: 2026.7.1 — `resolve_runs`: files are grouped into runs and each run is assigned on its own
#: bytes. A dataset resolved as one library dropped every file but one pair per role.
#: 2026.7.2 — over-length onlist admission: a barcode read over-sequenced into the length dead zone
#: (canonical < mode < over_length_min) is admitted when its barcode prefix hits the whitelist, so a
#: previously-forbidden over-sequenced read now resolves to its chemistry (#7).
#: 2026.7.3 — over-length admission uses a FLOOR-ANCHORED bar, not the support `min`: admission asks
#: "barcode vs cDNA" (chance ≈ whitelist floor), not "confident barcode" (0.6). A real over-sequenced
#: barcode read with ordinary sequencing error hit below 0.6 on exact match and fell to bulk (#7,
#: GSE126954 SRX5411291); the floor-anchored bar admits it while still rejecting a same-length cDNA.
#: 2026.7.4 — multi-lane surplus absorption: a run sequenced across N lanes holds N files per role, but
#: the injective assignment fills each role once; the surplus same-length lane files are now absorbed
#: into their role (was NO_VALID_ROLE_ASSIGNMENT), so a multi-lane 10x dataset resolves (GSE208154).
#: 2026.7.5 — surplus absorption matches by READ DESIGNATION (R1/R2/…) + length, not de-laned filename.
#: One accession sequenced across several flowcells carries a different flowcell id per file, so the
#: lanes of one read de-laned to different names and the cross-flowcell surplus stayed unassigned;
#: matching on the designation the sequencer wrote fuses them (GSE208154 is 2 flowcells x 8 lanes x
#: {R1,R2,I1} per run, which 2026.7.4's de-lane equality could not absorb across the flowcell boundary).
#: 2026.7.6 — hierarchical descent: resolve_dataset scores a length-FEASIBLE pool (drawn from runnable
#: specs via the scorer's own read-length gate) instead of a flat loop over the whole KB; escalate still
#: receives the full KB. Provably winner-invariant — a length-infeasible spec would have scored
#: forbidden — so the winner equals a flat full scan; this only narrows which specs are scored as the KB
#: grows, and reads sibling confusability off the tree instead of hand-declared cliques.
RESOLVE_VERSION = "2026.7.6"

from .cache import Cache, dataset_id  # noqa: E402
from .engine import (  # noqa: E402
    Hypothesis,
    MultiRunOutput,
    ResolveOutput,
    RunResolution,
    exit_code_for,
    resolve_dataset,
    resolve_runs,
    role_of_sha_for,
)
from .group import group_runs, run_key  # noqa: E402
from .scoring import Cell, TechEvaluation, build_tech_evaluation  # noqa: E402
from .window import WindowProbe  # noqa: E402

__all__ = [
    "RESOLVE_VERSION",
    "resolve_dataset",
    "resolve_runs",
    "ResolveOutput",
    "MultiRunOutput",
    "RunResolution",
    "role_of_sha_for",
    "group_runs",
    "run_key",
    "Hypothesis",
    "exit_code_for",
    "build_tech_evaluation",
    "TechEvaluation",
    "Cell",
    "WindowProbe",
    "Cache",
    "dataset_id",
]
