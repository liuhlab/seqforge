"""The eval runner — drive real cases through the real compiler and score the outcome (brief §9).

This runs the shipping code path, not a reimplementation of it: ``materialize -> [harvest] -> resolve``
via the same ``resolve_dataset`` / ``extract_drafts`` / ``verify_drafts`` the CLI calls. An eval that
tested a parallel copy of the pipeline would grade the wrong program.

Two design points carry most of the value:

**Trials are first-class.** The LLM stage is nondeterministic — the same document has been observed to
yield different (both valid) quotes across runs. A single trial therefore measures a sample, not the
system. ``--trials N`` re-runs each prose case and reports ``stability``; a case counts as correct only
if **every** trial was correct. A stage that is right 4 times in 5 is not right, and averaging that
away is how a harness lies to you.

**Harvest false-accepts roll up into the case grade.** A verified-but-wrong ``experiment.*`` assertion
is not a lesser failure than a wrong chemistry: bytes can never contradict it, so it reaches the
manifest unchallenged. It grades ``false_accept`` like any other silent wrong answer.
"""

from __future__ import annotations

import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..harvest import (
    ExtractUnavailable,
    LLMProvider,
    extract_drafts,
    normalize_document,
    resolve_provider,
    verify_drafts,
)
from ..harvest.normalize import NormalizedDoc
from ..kb.loader import load_all_specs
from ..models.assertion import Assertion, AssertionDraft, ExtractorProvenance
from ..models.resolve import EvalReport
from ..resolve import Hypothesis, resolve_dataset
from .case import Case, CaseError, CaseSkipped, Materialized, discover_cases, materialize
from .grade import CaseGrade, Grade, grade_case


@dataclass
class HarvestGrade:
    """How the LLM stage did on one case: recall, hallucination, and what the tripwire caught."""

    matched: list[str] = field(default_factory=list)
    missing: list[str] = field(default_factory=list)
    #: Forbidden fields that survived verification — a claim the prose does not make. Corpus poison.
    hallucinated: list[str] = field(default_factory=list)
    #: Drafts the R5 tripwire rejected. Not a failure: this is the safety net doing its job.
    n_rejected: int = 0
    extracted: dict[str, str] = field(default_factory=dict)

    def to_json(self) -> dict[str, Any]:
        return {
            "matched": self.matched,
            "missing": self.missing,
            "hallucinated": self.hallucinated,
            "n_rejected": self.n_rejected,
            "extracted": self.extracted,
        }


@dataclass
class CaseRun:
    """One case's full result across all trials."""

    case_id: str
    grade: CaseGrade
    trials: int = 1
    stability: float = 1.0
    harvest: HarvestGrade | None = None
    usage: dict[str, int] = field(default_factory=dict)
    seconds: float = 0.0
    llm_calls: int = 0
    skipped: str | None = None

    def to_json(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "case": self.case_id,
            "seconds": round(self.seconds, 3),
            "llm_calls": self.llm_calls,
        }
        if self.skipped:
            return {**out, "skipped": self.skipped}
        out.update(self.grade.to_json())
        out["trials"] = self.trials
        out["stability"] = round(self.stability, 3)
        if self.usage:
            out["usage"] = self.usage
        if self.harvest is not None:
            out["harvest"] = self.harvest.to_json()
        return out


def run_case(
    case: Case,
    *,
    llm: bool = False,
    provider: LLMProvider | None = None,
    model: str | None = None,
    trials: int = 1,
    workspace: Path | None = None,
) -> CaseRun:
    """Run one case through the compiler ``trials`` times and grade every trial.

    Deterministic cases ignore ``trials`` (re-running identical bytes measures nothing).
    """
    started = time.monotonic()
    needs_llm = case.has_prose and case.recipe.hypothesis is None
    if needs_llm and not llm:
        # The case's expectation *depends* on a claim only the LLM can supply. Running it byte-only
        # would grade a different question and count the miss as a failure. Skip, loudly.
        return CaseRun(
            case.id,
            _empty_grade(case),
            skipped="needs the LLM stage (prose, no declared hypothesis); pass --llm",
            seconds=time.monotonic() - started,
        )

    use_llm = llm and case.has_prose
    n = trials if use_llm else 1

    grades: list[CaseGrade] = []
    harvest: HarvestGrade | None = None
    usage: dict[str, int] = {}
    calls = 0

    with tempfile.TemporaryDirectory(prefix="seqforge-eval-") as tmp:
        tmp_path = Path(tmp)
        try:
            built = materialize(case, tmp_path / "inputs")
        except CaseSkipped as exc:
            return CaseRun(
                case.id, _empty_grade(case), skipped=str(exc), seconds=time.monotonic() - started
            )

        ws = workspace or tmp_path
        for _ in range(n):
            hypothesis: Hypothesis | None = None
            if case.recipe.hypothesis:
                hypothesis = Hypothesis(value=case.recipe.hypothesis, id="recipe", confidence=0.9)
            if use_llm:
                try:
                    harvest, hyp, u = _run_harvest(case, provider=provider, model=model)
                except ExtractUnavailable as exc:
                    return CaseRun(
                        case.id,
                        _empty_grade(case),
                        skipped=f"LLM unavailable: {exc}",
                        seconds=time.monotonic() - started,
                    )
                calls += len(case.metadata_docs)
                for k, v in u.items():
                    usage[k] = usage.get(k, 0) + v
                if hyp is not None:
                    hypothesis = hyp

            out = resolve_dataset(
                built.paths,
                registry=built.registry,
                hypothesis=hypothesis,
                workspace=ws,
                use_cache=False,
            )
            grades.append(
                grade_case(
                    case.id,
                    case.expected,
                    out.result,
                    out.exit_code(),
                    _labels(out, built),
                )
            )

    worst = _worst(grades)
    if harvest is not None:
        worst = _fold_harvest(worst, harvest)
    n_ok = sum(1 for g in grades if g.ok)
    return CaseRun(
        case_id=case.id,
        grade=worst,
        trials=n,
        stability=n_ok / len(grades) if grades else 0.0,
        harvest=harvest,
        usage=usage,
        seconds=time.monotonic() - started,
        llm_calls=calls,
    )


def run_cases(
    cases: list[Case],
    *,
    llm: bool = False,
    provider: LLMProvider | None = None,
    model: str | None = None,
    trials: int = 1,
) -> tuple[EvalReport, list[CaseRun]]:
    """Run every case and aggregate into the brief §9 metric set."""
    runs = [run_case(c, llm=llm, provider=provider, model=model, trials=trials) for c in cases]
    return build_report(runs), runs


def build_report(runs: list[CaseRun]) -> EvalReport:
    """Aggregate. Skipped cases are excluded from every rate — a skip is not a pass."""
    scored = [r for r in runs if r.skipped is None]
    n = len(scored)

    checks = [c for r in scored for c in r.grade.fields]
    n_field = len(checks) + sum(
        len(r.harvest.matched) + len(r.harvest.missing) + len(r.harvest.hallucinated)
        for r in scored
        if r.harvest is not None
    )
    n_field_ok = sum(1 for c in checks if c.ok) + sum(
        len(r.harvest.matched) for r in scored if r.harvest is not None
    )

    false_accept = sum(1 for r in scored if r.grade.grade is Grade.FALSE_ACCEPT)
    false_refuse = sum(1 for r in scored if r.grade.grade is Grade.FALSE_REFUSE)
    asked = sum(1 for r in scored if r.grade.actual_outcome == "ask")
    missed = sum(1 for r in scored if r.grade.missed_question)

    cost: dict[str, float] = {
        "seconds": round(sum(r.seconds for r in runs), 3),
        "llm_calls": float(sum(r.llm_calls for r in runs)),
    }
    for key in ("input_tokens", "output_tokens", "cache_read_tokens"):
        total = sum(r.usage.get(key, 0) for r in runs)
        if total:
            cost[key] = float(total)

    return EvalReport(
        n_cases=n,
        field_accuracy=(n_field_ok / n_field) if n_field else 1.0,
        false_accept_rate=(false_accept / n) if n else 0.0,
        false_refuse_rate=(false_refuse / n) if n else 0.0,
        questions_asked={
            "total": float(asked),
            "per_case": (asked / n) if n else 0.0,
            "missed": float(missed),
        },
        cost=cost,
        per_case=[r.to_json() for r in runs],
    )


def load_cases(cases_dir: Path | None = None, *, only: list[str] | None = None) -> list[Case]:
    cases = discover_cases(cases_dir)
    if only:
        wanted = set(only)
        cases = [c for c in cases if c.id in wanted]
        missing = wanted - {c.id for c in cases}
        if missing:
            raise CaseError(f"no such case(s): {sorted(missing)}")
    return cases


def _run_harvest(
    case: Case, *, provider: LLMProvider | None, model: str | None
) -> tuple[HarvestGrade, Hypothesis | None, dict[str, int]]:
    """normalize -> extract -> verify over the case's prose. Only verified claims are graded."""
    specs = load_all_specs()
    llm = provider if provider is not None else resolve_provider()

    docs: list[NormalizedDoc] = [normalize_document(p) for p in case.metadata_docs]
    drafts: list[AssertionDraft] = []
    usage: dict[str, int] = {}
    extractor: ExtractorProvenance | None = None
    for doc in docs:
        outcome = extract_drafts(doc, specs, provider=llm, model=model)
        drafts.extend(outcome.drafts)
        extractor = outcome.extractor
        for k, v in outcome.usage.items():
            usage[k] = usage.get(k, 0) + v

    assert extractor is not None  # docs is non-empty (checked by the caller via has_prose)
    report = verify_drafts(drafts, docs, extractor=extractor)
    accepted: list[Assertion] = report.assertions
    by_field = {a.field: a for a in accepted}

    grade = HarvestGrade(n_rejected=len(report.rejected))
    grade.extracted = {a.field: str(a.value) for a in accepted}
    for want in case.expected.assertions:
        got = by_field.get(want.field)
        if got is not None and str(got.value) == want.value:
            grade.matched.append(want.field)
        else:
            grade.missing.append(want.field)
    grade.hallucinated = [f for f in case.expected.forbidden_fields if f in by_field]

    hypothesis: Hypothesis | None = None
    chem = by_field.get("library.chemistry")
    if chem is not None:
        hypothesis = Hypothesis(value=str(chem.value), id="harvest", confidence=0.9)
    return grade, hypothesis, usage


def _fold_harvest(grade: CaseGrade, harvest: HarvestGrade) -> CaseGrade:
    """A verified-but-wrong assertion is a false-accept: bytes can never contradict ``experiment.*``."""
    if harvest.hallucinated:
        grade.grade = Grade.FALSE_ACCEPT
        grade.notes.append(
            f"extracted claims the prose does not make: {harvest.hallucinated} "
            "(verified, so nothing downstream would catch it)"
        )
    elif harvest.missing and grade.grade is Grade.CORRECT:
        grade.grade = Grade.WRONG_REASON
        grade.notes.append(f"failed to extract stated field(s): {harvest.missing}")
    return grade


def _worst(grades: list[CaseGrade]) -> CaseGrade:
    """Across trials, report the worst outcome — a stage that fails sometimes fails."""
    order = {
        Grade.FALSE_ACCEPT: 0,
        Grade.FALSE_REFUSE: 1,
        Grade.MIS_TRIAGE: 2,
        Grade.WRONG_REASON: 3,
        Grade.OVER_ASK: 4,
        Grade.CORRECT: 5,
    }
    return min(grades, key=lambda g: order[g.grade])


def _labels(out: Any, built: Materialized) -> dict[str, str]:
    """sha256 -> recipe read id, so role assertions are written against ``R1``/``R2``, not hashes."""
    return {
        o.file.sha256: built.labels.get(o.file.basename, o.file.basename) for o in out.observations
    }


def _empty_grade(case: Case) -> CaseGrade:
    return CaseGrade(case.id, Grade.CORRECT, case.expected.outcome, "skipped")
