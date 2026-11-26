"""``resolve`` — the scoring engine: bytes + KB (+ optional hypothesis) -> a ranked, escalated verdict.

Deterministic and LLM-free. Signature-test evaluators score a JSON-safe evidence matrix
``M[role][file]``; a cardinality-normalized joint role-assignment picks the best injective
files->roles map per technology; escalation turns the ranked candidates into exactly one of
``Decision`` / ``Conflict`` / ``Question`` / ``Blocker`` with rung provenance. Every artifact is
content-addressed under ``.seqforge/``. The only interpretive input is a span-verified
``hypothesis`` that steers control flow — it never enters the matrix.
"""

from __future__ import annotations

#: CalVer YYYY.M.PATCH; bump when scoring/assignment/escalation semantics change (folded into the dataset cache key). History: `docs/research/resolve-version-history-2026-11-25.md`.
RESOLVE_VERSION = "2026.11.0"

from .cache import Cache, dataset_id  # noqa: E402
from .engine import (  # noqa: E402
    CellOutcome,
    DatasetResolution,
    Hypothesis,
    MultiRunOutput,
    ResolveOutput,
    RunResolution,
    chemistry_hypothesis,
    exit_code_for,
    plate_chemistries,
    reduce_dataset,
    resolve_dataset,
    resolve_runs,
    role_of_sha_for,
)
from .group import group_runs, lane_of, run_key  # noqa: E402
from .scoring import Cell, TechEvaluation, build_tech_evaluation  # noqa: E402
from .window import WindowProbe  # noqa: E402

__all__ = [
    "RESOLVE_VERSION",
    "Cache",
    "Cell",
    "CellOutcome",
    "DatasetResolution",
    "Hypothesis",
    "MultiRunOutput",
    "ResolveOutput",
    "RunResolution",
    "TechEvaluation",
    "WindowProbe",
    "build_tech_evaluation",
    "chemistry_hypothesis",
    "dataset_id",
    "exit_code_for",
    "group_runs",
    "lane_of",
    "plate_chemistries",
    "reduce_dataset",
    "resolve_dataset",
    "resolve_runs",
    "role_of_sha_for",
    "run_key",
]
