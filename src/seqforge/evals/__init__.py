"""``evals`` — the harness that stops the compiler rotting invisibly (brief §9).

Every other module in seqforge can be pinned by a unit test: same bytes in, same artifact out. Two
things here cannot.

1. **The LLM stage is nondeterministic.** The same document has produced different (both correct,
   both span-verified) quotes across runs. There is no output to snapshot — only a rate to measure.
2. **Prompt and KB edits are silent.** Adding a KB alias or rewording an instruction changes
   extraction behavior without changing a single test. The brief is explicit: treat prompt and KB
   changes as code changes.

So this module measures rates over real cases run through the real pipeline, and singles out
**false-accept** — a confident wrong manifest — as the metric that matters. A refusal costs a human's
attention; a false accept silently poisons a training corpus and is never questioned again. The
grading tables in :mod:`.grade` encode that asymmetry rather than treating every failure alike.
"""

from __future__ import annotations

#: CalVer YYYY.M.PATCH; bumped when case format or grading semantics change. A grading change is a
#: code change — two reports are only comparable at equal EVALS_VERSION.
EVALS_VERSION = "2026.7.0"

from .case import (  # noqa: E402
    Case,
    CaseError,
    CaseSkipped,
    Expected,
    FingerprintRecipe,
    LocalRecipe,
    Materialized,
    RandomRecipe,
    Recipe,
    SpecRecipe,
    default_cases_dir,
    discover_cases,
    load_case,
    materialize,
)
from .grade import CaseGrade, FieldCheck, Grade, grade_case, outcome_of  # noqa: E402
from .run import (  # noqa: E402
    CaseRun,
    HarvestGrade,
    build_report,
    load_cases,
    run_case,
    run_cases,
)

__all__ = [
    "EVALS_VERSION",
    # cases
    "Case",
    "CaseError",
    "CaseSkipped",
    "Expected",
    "Recipe",
    "SpecRecipe",
    "RandomRecipe",
    "LocalRecipe",
    "FingerprintRecipe",
    "Materialized",
    "load_case",
    "discover_cases",
    "default_cases_dir",
    "materialize",
    # grading
    "Grade",
    "CaseGrade",
    "FieldCheck",
    "grade_case",
    "outcome_of",
    # running
    "run_case",
    "run_cases",
    "build_report",
    "load_cases",
    "CaseRun",
    "HarvestGrade",
]
