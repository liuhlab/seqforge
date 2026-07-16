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
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

from ..harvest import (
    ExtractUnavailable,
    LLMProvider,
    extract_drafts,
    has_prose,
    normalize_document,
    normalize_record,
    resolve_provider,
    verify_drafts,
)
from ..harvest.fields import fields_for
from ..harvest.normalize import NormalizedDoc
from ..kb.loader import load_all_specs
from ..models.assertion import Assertion, AssertionDraft, ExtractorProvenance
from ..models.resolve import EvalReport
from ..resolve import Hypothesis, resolve_dataset
from ..resolve.records import DocumentSubject, resolve_metadata
from .case import Case, CaseError, CaseSkipped, Materialized, discover_cases, materialize
from .grade import CaseGrade, Grade, grade_case


@dataclass
class HarvestGrade:
    """How the LLM stage did on one case: recall, hallucination, and what the tripwire caught."""

    matched: list[str] = field(default_factory=list)
    missing: list[str] = field(default_factory=list)
    #: Forbidden fields that survived verification — a claim the prose does not make. Corpus poison.
    hallucinated: list[str] = field(default_factory=list)
    #: Drafts the span-verification tripwire rejected. Not a failure: this is the safety net doing its job.
    n_rejected: int = 0
    #: Fields extracted in SOME trials but not all. Not averaged away: a field the model finds two
    #: times in three is a field you cannot depend on, and that is a finding in its own right.
    unstable: list[str] = field(default_factory=list)
    extracted: dict[str, str] = field(default_factory=dict)

    def to_json(self) -> dict[str, Any]:
        out = {
            "matched": self.matched,
            "missing": self.missing,
            "hallucinated": self.hallucinated,
            "n_rejected": self.n_rejected,
            "extracted": self.extracted,
        }
        if self.unstable:
            out["unstable"] = self.unstable
        return out


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
    harvests: list[HarvestGrade] = []
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
            verified: list[Assertion] = []
            subjects: list[DocumentSubject] = []
            if use_llm:
                try:
                    hg, hyp, u, verified, subjects = _run_harvest(
                        case, provider=provider, model=model
                    )
                except ExtractUnavailable as exc:
                    return CaseRun(
                        case.id,
                        _empty_grade(case),
                        skipped=f"LLM unavailable: {exc}",
                        seconds=time.monotonic() - started,
                    )
                harvests.append(hg)
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
            # The SECOND resolver, over the same files. It reads records and prose; it is handed no
            # probe signal (`FileIdentity`, not `Observation`). Running it here is what lets a
            # pre-registration's sample claims be graded at all -- before this the harness could not
            # see a sample, so "tissue=Neurons" was prose in a description field.
            metadata = resolve_metadata(
                files=[o.file for o in out.observations],
                records=built.records,
                assertions=verified,
                subjects=subjects,
            )
            trial_grade = grade_case(
                case.id,
                case.expected,
                out.result,
                out.exit_code(),
                _labels(out, built),
                metadata,
            )
            # Fold THIS trial's harvest into THIS trial's grade. Folding once at the end against the
            # merged harvest would charge every trial for one trial's hallucination, and `stability`
            # would stop meaning "how often was the whole case right".
            if harvests:
                trial_grade = _fold_harvest(trial_grade, harvests[-1])
            grades.append(trial_grade)

    harvest = _merge_harvest(harvests) if harvests else None
    worst = _worst(grades)
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
) -> tuple[HarvestGrade, Hypothesis | None, dict[str, int], list[Assertion], list[DocumentSubject]]:
    """normalize -> extract -> verify over the case's prose. Only verified claims are graded.

    "The case's prose" now means two things: the documents a human put beside it, and each archive
    record rendered as its own document. The second is what lets a claim name a sample, so a harness
    that ran only the first could never grade one.
    """
    specs = load_all_specs()
    llm = provider if provider is not None else resolve_provider()

    docs: list[NormalizedDoc] = [normalize_document(p) for p in case.metadata_docs]
    if case.records is not None:
        docs += [
            normalize_record(r)
            for r in case.records.records
            if has_prose(r) and fields_for(r.level, "reference")
        ]
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
    subjects = [
        DocumentSubject(doc_sha256=d.doc_sha256, scope=d.scope, subject=d.subject) for d in docs
    ]
    return grade, hypothesis, usage, accepted, subjects


def _merge_harvest(grades: list[HarvestGrade]) -> HarvestGrade:
    """Across trials, keep the WORST — never the last.

    Extraction is nondeterministic, so a field the model invented in 1 of 3 trials is a field it *can*
    invent, and a field it extracted in only 2 of 3 is not one you can rely on. Reporting the final
    trial (the bug this replaces) let a real hallucination vanish on a re-run — exactly the illusion
    trials exist to dispel.

    So: ``hallucinated`` and ``missing`` union (any trial failing is a failure), ``matched``
    intersects (a field counts only if EVERY trial got it), and fields that come and go are surfaced
    as ``unstable`` rather than silently averaged into a rate.
    """
    if len(grades) == 1:
        return grades[0]
    merged = HarvestGrade()
    merged.hallucinated = sorted({f for g in grades for f in g.hallucinated})
    merged.missing = sorted({f for g in grades for f in g.missing})
    merged.matched = sorted(set.intersection(*(set(g.matched) for g in grades)))
    merged.n_rejected = sum(g.n_rejected for g in grades)
    seen = {f for g in grades for f in g.matched}
    merged.unstable = sorted(seen - set(merged.matched))
    for g in grades:
        merged.extracted.update(g.extracted)
    return merged


def _fold_harvest(grade: CaseGrade, harvest: HarvestGrade) -> CaseGrade:
    """A verified-but-wrong assertion is a false-accept: bytes can never contradict ``experiment.*``.

    Returns a NEW grade. It used to mutate in place, and because ``_worst`` hands back a reference
    into the trials list rather than a copy, folding the worst grade rewrote a list element that
    ``stability`` was then counted over — reporting 0.667 for three identical, perfectly stable
    trials. A metric that is quietly wrong is worse than no metric, so this no longer mutates at all.
    """
    if harvest.hallucinated:
        return replace(
            grade,
            grade=Grade.FALSE_ACCEPT,
            notes=[
                *grade.notes,
                f"extracted claims the prose does not make: {harvest.hallucinated} "
                "(verified, so nothing downstream would catch it)",
            ],
        )
    if harvest.missing and grade.grade is Grade.CORRECT:
        return replace(
            grade,
            grade=Grade.WRONG_REASON,
            notes=[*grade.notes, f"failed to extract stated field(s): {harvest.missing}"],
        )
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
