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
RESOLVE_VERSION = "2026.7.2"

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
