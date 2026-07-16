"""``resolve`` — the scoring engine: bytes + KB (+ optional hypothesis) -> a ranked, escalated verdict.

Deterministic and LLM-free (R2/R8/R11). Signature-test evaluators score a JSON-safe evidence matrix
``M[role][file]``; a cardinality-normalized joint role-assignment picks the best injective
files->roles map per technology; escalation turns the ranked candidates into exactly one of
``Decision`` / ``Conflict`` / ``Question`` / ``Blocker`` with rung provenance. Every artifact is
content-addressed under ``.seqforge/`` (R7). The only interpretive input is a span-verified
``hypothesis`` that steers control flow — it never enters the matrix (§3.4).
"""

from __future__ import annotations

#: CalVer YYYY.M.PATCH; bumped when scoring/assignment/escalation semantics change. Folded into the
#: dataset cache key so a resolver change invalidates stale candidates (R7).
#: 2026.7.1 — `resolve_runs`: files are grouped into runs and each run is assigned on its own
#: bytes. A dataset resolved as one library dropped every file but one pair per role.
RESOLVE_VERSION = "2026.7.1"

from .cache import Cache, dataset_id  # noqa: E402
from .engine import (  # noqa: E402
    Hypothesis,
    MultiRunOutput,
    ResolveOutput,
    RunResolution,
    exit_code_for,
    resolve_dataset,
    resolve_runs,
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
