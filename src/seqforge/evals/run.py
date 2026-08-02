"""The eval runner — drive real cases through the real compiler and score the outcome.

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

**A stage that did not run says so.** Every harvest grade carries a ``status`` — ``complete``,
``partial`` or ``unmeasured`` — and the report carries the tier-wide totals behind it. A skip still
poisons no rate, which is exactly why it used to be invisible: the first graded ``--llm`` tier pass
issued 68 of 141 planned requests because five of the seven prose-carrying cases aborted, and the
summary reported a clean ``harvest.matched`` for the two that happened to survive. Silence about a
stage that ran and silence about a stage that never did are different sentences (#182).
"""

from __future__ import annotations

import os
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

from ..harvest import (
    EXTRACT_PROMPT_VERSION,
    CeilingExceeded,
    LLMProvider,
    TokenMeter,
    extract_planned,
    normalize_document,
    plan_extraction,
    resolve_provider,
    verify_drafts,
    write_transcript,
)
from ..harvest.normalize import NormalizedDoc
from ..kb.loader import load_all_specs
from ..models.assertion import Assertion, AssertionDraft, ExtractorProvenance
from ..models.blocker import Blocker
from ..models.resolve import EvalReport
from ..resolve import Hypothesis, chemistry_hypothesis, resolve_dataset
from ..resolve.records import DocumentSubject, resolve_metadata
from ..workspace import EVAL_TRANSCRIPTS_DIRNAME, eval_dir
from .case import (
    Case,
    CaseError,
    CaseSkipped,
    Materialized,
    SkipKind,
    discover_cases,
    materialize,
)
from .grade import CaseGrade, Grade, grade_case


@dataclass
class HarvestGrade:
    """How the LLM stage did on one case: recall, hallucination, and what the tripwire caught.

    It carries the **claims**, not a summary of them. Both halves used to be flattened on the way in:
    an accepted claim became one ``field -> str(value)`` entry, dropping the quote it rests on and
    the span that makes the quote checkable, and a refused draft became one increment of an integer.
    So a report could say ``library.chemistry = "RNA-Seq"`` and never *from this quote, in this
    document, at these offsets* — which is the entire content of the hallucination tripwire — while
    one benchmark case's 84 refusals survived as the number 84.
    """

    matched: list[str] = field(default_factory=list)
    missing: list[str] = field(default_factory=list)
    #: Expected fields a document that never answered would have been asked. **Could not check**, as
    #: against ``missing``'s checked and found nothing — the same distinction the corpus already
    #: insists on between a package that is ``absent`` and one that is ``unavailable``, and for the
    #: same reason: folding them together hides a gap behind a word that reads as a finding.
    #: Excluded from every rate. A field that was nonetheless *found* elsewhere is ``matched``: a
    #: negative verdict needs the whole accepted set, a positive one needs one document.
    unchecked: list[str] = field(default_factory=list)
    #: Forbidden fields that survived verification — a claim the prose does not make. Corpus poison.
    hallucinated: list[str] = field(default_factory=list)
    #: Requests actually issued at the model seam, counted by the meter — **not** the document
    #: count, which is a floor: a document whose first attempt hit a 429 costs two requests and the
    #: old number reported one. An archive record with prose is its own document, so the caller
    #: cannot infer either number from the file count it passed in.
    n_calls: int = 0
    #: Documents sent, kept beside `n_calls` because "73 documents, 78 requests" is two facts and
    #: neither substitutes for the other.
    n_documents: int = 0
    #: Of those, the ones the provider never answered — retries exhausted, nothing to read. It is
    #: the plan-versus-issued gap at the grain a reader can act on, and the third fact beside the
    #: other two: 73 documents, 78 requests, 5 of the documents never answered.
    n_documents_failed: int = 0
    #: Fields extracted in SOME trials but not all. Not averaged away: a field the model finds two
    #: times in three is a field you cannot depend on, and that is a finding in its own right.
    unstable: list[str] = field(default_factory=list)
    #: The Assertions this grade was computed over, whole — each with the quote it rests on and the
    #: span that locates it. `matched`/`missing`/`hallucinated` are verdicts about these; this is the
    #: evidence they were reached from, and a verdict with the evidence thrown away is not checkable.
    assertions: list[Assertion] = field(default_factory=list)
    #: The drafts that did NOT survive — field, value, quote, reason, detail, and the document each
    #: came from. Both producers land here: a draft the model returned malformed (extract-time, where
    #: field/value/quote may be absent) and one whose field, quote or entailment failed (verify).
    rejected: list[dict[str, Any]] = field(default_factory=list)
    #: What each document sent was: sha, basename, scope, subject, size. Small by construction, and
    #: it is what turns the sha256 in a Span back into "GSM4318946's sample record" for a reader.
    documents: list[dict[str, Any]] = field(default_factory=list)
    #: How the calls were made — max_tokens, response_format, thinking. One dict, because every
    #: request in a run is made the same way; the eval path used to drop it while the CLI kept it.
    mode: dict[str, Any] = field(default_factory=dict)

    @property
    def n_rejected(self) -> int:
        """How many drafts the tripwire threw out. A summary of :attr:`rejected`, never a substitute
        for it: the count says a net caught something and nothing whatever about what."""
        return len(self.rejected)

    @property
    def status(self) -> str:
        """``complete`` | ``partial`` | ``unmeasured`` — how much of this stage actually ran.

        The one word a reader needs before believing anything else in this grade. A stage that never
        ran and a stage that ran and found nothing both report empty lists, and a green tier that
        cannot tell them apart says nothing at all about the cases whose model never answered.
        """
        if not self.n_documents or self.n_documents_failed >= self.n_documents:
            return "unmeasured"
        return "partial" if self.n_documents_failed else "complete"

    def to_json(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            # First, because it qualifies every list under it.
            "status": self.status,
            "matched": self.matched,
            "missing": self.missing,
            "hallucinated": self.hallucinated,
            "n_rejected": self.n_rejected,
            "n_calls": self.n_calls,
            "n_documents": self.n_documents,
            "n_documents_failed": self.n_documents_failed,
            "assertions": [a.model_dump(mode="json") for a in self.assertions],
            "rejected": self.rejected,
        }
        if self.documents:
            out["documents"] = self.documents
        if self.mode:
            out["mode"] = self.mode
        if self.unstable:
            out["unstable"] = self.unstable
        if self.unchecked:
            out["unchecked"] = self.unchecked
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
    #: WHICH skip, as a key rather than a sentence. ``absent`` means the corpus does not hold this
    #: case's package at all, so nobody anywhere can run it; ``unavailable`` is this machine's
    #: problem. Both are excluded from every rate, and only one is something to act on.
    skip_kind: SkipKind = "unavailable"
    #: Set when this case reached its token Ceiling. The case did not finish, so it is reported as a
    #: skip and excluded from every rate — but a skip is a shrug and this is a refusal, so it also
    #: carries the structured ``Blocker`` and `eval run` exits 3 on it.
    blocker: Blocker | None = None
    #: Where this case's exchanges landed, RELATIVE to the run directory — so the report survives the
    #: directory being moved or downloaded as a CI artifact. ``None`` when nothing was recorded: a
    #: byte-only run reaches no model, and a run with no workspace has nowhere to put one.
    transcript: str | None = None

    def to_json(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "case": self.case_id,
            "seconds": round(self.seconds, 3),
            "llm_calls": self.llm_calls,
        }
        # Before the skip short-circuit: a case stopped at its ceiling is the one whose exchanges are
        # most worth reading, and it reports as a skip.
        if self.transcript is not None:
            out["transcript"] = self.transcript
        if self.blocker is not None:
            out["blockers"] = [self.blocker.model_dump(mode="json")]
        if self.skipped:
            return {**out, "skipped": self.skipped, "skip_kind": self.skip_kind}
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
    ceiling: int | None = None,
    workspace: Path | None = None,
) -> CaseRun:
    """Run one case through the compiler ``trials`` times and grade every trial.

    Deterministic cases ignore ``trials`` (re-running identical bytes measures nothing).

    ``ceiling`` is **per case**, because a case is a dataset and the ceiling bounds a dataset. A
    corpus-wide one would stop the whole run at the first expensive case and never grade the
    thirteen behind it, which is the opposite of what a backstop is for.

    ``workspace`` is the run directory every case in this run shares — where this case's exchanges
    land, as ``transcripts/<case>.jsonl``. Without one the case still runs, in a temporary directory
    that is deleted, and records nothing: a transcript with no address is memory nobody reads.
    """
    started = time.monotonic()
    if case.needs_llm and not llm:
        # The case's expectation *depends* on a claim only the LLM can supply. Running it byte-only
        # would grade a different question and count the miss as a failure. Skip, loudly. Records
        # alone do not trip this: a fingerprint case resolves its bytes and grades samples from
        # records with no key, so it runs hermetically.
        return CaseRun(
            case.id,
            _empty_grade(case),
            skipped="needs the LLM stage (prose, no declared hypothesis); pass --llm",
            seconds=time.monotonic() - started,
        )

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
                case.id,
                _empty_grade(case),
                skipped=str(exc),
                skip_kind=exc.kind,
                seconds=time.monotonic() - started,
            )

        # A synthetic case keeps its prose in the case dir; a fingerprint case ships it inside the
        # package and materialize surfaces it. Harvest reads whichever is present.
        docs = built.metadata_docs or case.metadata_docs
        use_llm = llm and (bool(docs) or case.records is not None)
        n = trials if use_llm else 1

        # One meter per case, spanning every trial: it is the only thing that counts the requests,
        # and the ceiling it holds is this dataset's. It records only when there is a run directory
        # to write the transcript into — otherwise 14 cases x N exchanges of held text is memory
        # nothing will ever read.
        meter = (
            TokenMeter(
                provider if provider is not None else resolve_provider(),
                ceiling=ceiling,
                subject=case.id,
                record=workspace is not None,
            )
            if use_llm
            else None
        )

        ws = workspace or tmp_path
        for _ in range(n):
            hypothesis: Hypothesis | None = None
            if case.recipe.hypothesis:
                hypothesis = Hypothesis(value=case.recipe.hypothesis, id="recipe", confidence=0.9)
            verified: list[Assertion] = []
            subjects: list[DocumentSubject] = []
            if use_llm:
                assert meter is not None  # built above from the same `use_llm`
                try:
                    hg, u, verified, subjects = _run_harvest(case, docs, meter=meter, model=model)
                except CeilingExceeded as exc:
                    # A refusal, not an unavailability: the provider answered every request it was
                    # given. The case did not finish, so it scores nothing — but it carries the
                    # Blocker, and `eval run` exits 3 on it rather than shrugging.
                    return CaseRun(
                        case.id,
                        _empty_grade(case),
                        skipped=f"token ceiling: {exc}",
                        blocker=exc.blocker(),
                        usage=meter.usage(),
                        llm_calls=meter.n_exchanges,
                        seconds=time.monotonic() - started,
                        # The exchanges up to the breach were paid for, and "on what?" is exactly
                        # the question a breach produces.
                        transcript=_save_transcript(workspace, case.id, meter),
                    )
                harvests.append(hg)
                # `hg.n_calls` is the METER's count of requests, not `len(docs)`. Every archive
                # record with prose is extracted as its own document, so the carried-file count
                # under-reports the real cost by the record count — on GSE126954 that is 1 against
                # 983 — and the document count in turn under-reports it by every retry.
                calls += hg.n_calls
                for k, v in u.items():
                    usage[k] = usage.get(k, 0) + v
                # The SAME reduction `manifest fill` makes, over the same verified assertions — a
                # harness that reduced prose its own way would be measuring itself (#188). `None`
                # means harvest has no opinion, which is NOT the same as overriding: a case that
                # declared its hypothesis in `inputs/recipe.yaml` keeps it, and that channel is the
                # only one `GSE317744` is graded on chemistry through.
                hyp = chemistry_hypothesis(verified)
                if hyp is not None:
                    hypothesis = hyp

            out = resolve_dataset(
                built.paths,
                registry=built.registry,
                hypothesis=hypothesis,
                workspace=ws,
                use_cache=False,
                _probed=built.probed,
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
        transcript=_save_transcript(workspace, case.id, meter),
    )


#: Under the run directory, one file per case. A directory rather than one file for the corpus: a
#: transcript is per dataset, and a reader opening a case's exchanges should not have to filter
#: thirteen other datasets out of them first.


def _save_transcript(workspace: Path | None, case_id: str, meter: TokenMeter | None) -> str | None:
    """Write one case's exchanges under the run directory; return the path RELATIVE to it.

    Relative because the run directory travels — it is downloaded as a CI artifact and read on
    another machine — and an absolute path recorded on the machine that produced it would name a
    directory that is not there. ``None`` when there is nothing to write: no run directory, no model
    reached, or a meter that recorded nothing.
    """
    if workspace is None or meter is None:
        return None
    transcript = meter.transcript()
    if not transcript.exchanges:
        return None
    relative = f"{EVAL_TRANSCRIPTS_DIRNAME}/{case_id}.jsonl"
    write_transcript(workspace / relative, transcript)
    return relative


#: Ceiling on the default fan-out. A run wide enough to saturate a shared node is not a faster run:
#: package pulls start contending, an ``--llm`` pass starts measuring the provider's rate limit
#: rather than the compiler, and on a login node it is other people's work that pays. 24 is a
#: maintainer's choice (2026-07-31), not a measurement — raise it per-run with ``--jobs``.
MAX_DEFAULT_JOBS = 24

#: The bound on extraction calls in flight — case-level and document-level concurrency multiply and
#: the provider sees the product — lives with the fan-out that makes them (`harvest/plan.py`), not
#: here. It moved when that fan-out stopped being written twice; it was never a property of the eval
#: harness, and `harvest extract` needs the same protection.


def default_jobs() -> int:
    """How many cases to run at once when the caller does not say: usable cores, capped.

    **Usable**, not present. ``os.process_cpu_count()`` honours CPU affinity and cgroup limits;
    ``os.cpu_count()`` reports the machine. On the cluster node this was written on they read 48 and
    192 — spending the machine's count there oversubscribes every other job on the box, and makes
    each case slower rather than the run faster. In CI the same call yields the runner's core count,
    so "parallel according to the cores available" needs no special case.

    The same rule covers the ``--llm`` pass. It waits on a socket rather than on this machine, so it
    could in principle run wider — but a fan-out past a couple of dozen measures the provider's rate
    limit instead of the compiler, and a partly rate-limited run is a worse number than a slower one.
    An explicit ``jobs`` always wins; this is the default, not a cap.
    """
    # `process_cpu_count` is 3.13+; on 3.12 (which mypy targets) fall back to the machine count.
    usable = getattr(os, "process_cpu_count", None)
    cpus: int | None = usable() if usable is not None else os.cpu_count()
    return min(MAX_DEFAULT_JOBS, cpus or 1)


def run_cases(
    cases: list[Case],
    *,
    llm: bool = False,
    provider: LLMProvider | None = None,
    model: str | None = None,
    trials: int = 1,
    jobs: int | None = None,
    ceiling: int | None = None,
    workspace: Path | None = None,
) -> tuple[EvalReport, list[CaseRun]]:
    """Run every case and aggregate into the harness's metric set.

    Cases are independent by construction — ``run_case`` materializes into its own temporary
    directory and shares no mutable state — so they run concurrently, ``jobs`` at a time
    (:func:`default_jobs` when unset, 1 to force the sequential path).

    **Threads, not processes.** Every expensive part of a case already releases the GIL: the socket
    reads for a package pull and an LLM call, and zlib's decompression of the slices. Processes would
    buy little and cost the pickling of a ``Case`` plus a fresh import of the KB in each worker.

    **Order is preserved**, and that is load-bearing rather than tidy: ``per_case`` is committed to
    reports and read in diffs, so it must not depend on which case happened to finish first. The
    aggregate metrics are order-independent already, but a report that shuffles its own rows every
    run cannot be compared to the last one.

    ``workspace`` turns the run into something on disk: the returned report names the directory it
    wrote, and each case's exchanges land under it. The harness was the one part of seqforge that
    kept nothing — every case ran in a temporary directory that was deleted, so a transcript had
    nowhere to go and stdout is the wrong place for a thousand exchanges.
    """
    n = jobs if jobs is not None else default_jobs()
    started = time.monotonic()
    run_dir = eval_dir(workspace) if workspace is not None else None

    def one(case: Case) -> CaseRun:
        # `ceiling` is handed down per case, not shared: it bounds a dataset, and each case is one.
        return run_case(
            case,
            llm=llm,
            provider=provider,
            model=model,
            trials=trials,
            ceiling=ceiling,
            workspace=run_dir,
        )

    if n <= 1 or len(cases) <= 1:
        runs = [one(c) for c in cases]
    else:
        with ThreadPoolExecutor(max_workers=min(n, len(cases))) as pool:
            # `map` yields in submission order, so `runs` matches `cases` however they interleave.
            runs = list(pool.map(one, cases))

    # Resolved once, here, rather than read off a run: `model` is usually None and the *effective*
    # model is the provider's default — `deepseek-v4-pro` on the DeepSeek preset. A report that
    # printed `null` for the common case would say nothing about the run that actually happened, and
    # this number does not transfer across models (ADR-0009). `eval run`'s coverage warning reads
    # this back, which is how a remedy line can name the model that ran instead of prescribing one.
    extractor = None
    if llm and provider is not None:
        extractor = {
            "provider": provider.name,
            "model": model or provider.default_model(),
            "prompt_version": EXTRACT_PROMPT_VERSION,
        }

    report = build_report(runs, wall_seconds=time.monotonic() - started, extractor=extractor)
    if run_dir is not None:
        report = report.model_copy(update={"run_dir": str(run_dir)})
    return report, runs


def build_report(
    runs: list[CaseRun],
    *,
    wall_seconds: float | None = None,
    extractor: dict[str, str] | None = None,
) -> EvalReport:
    """Aggregate. Skipped cases are excluded from every rate — a skip is not a pass.

    ``extractor`` names who produced the LLM-dependent numbers and is carried through untouched;
    :func:`run_cases` fills it, a ``--no-llm`` run leaves it ``None``. It is *not* derived from the
    runs, because a case that skipped for a missing key would silently drop it.

    ``cost.seconds`` is the sum of the cases' own durations; under ``jobs > 1`` that is work done,
    not time taken. ``wall_seconds`` is the elapsed time and is reported *beside* it rather than
    replacing it — the sum is what tells you the corpus got more expensive, and the elapsed time is
    what tells you the run got faster. Collapsing them into one number loses whichever question you
    were asking.

    ``harvest`` is the tier-wide coverage of the LLM stage — planned, extracted, unanswered, and how
    many cases sat at each of the three statuses. It is ``None`` on a run where nothing harvested,
    because zeros there would read as a stage that ran and found nothing, which is the one thing
    this key exists to distinguish.
    """
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
    if wall_seconds is not None:
        cost["wall_seconds"] = round(wall_seconds, 3)
    # `cache_write_tokens` is in this tuple because the Anthropic normalizer produces it and every
    # consumer but the on-disk ledger used to drop it — a cost the report simply did not show.
    for key in ("input_tokens", "output_tokens", "cache_read_tokens", "cache_write_tokens"):
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
        harvest=_harvest_coverage(runs),
        extractor=extractor,
        per_case=[r.to_json() for r in runs],
    )


def _harvest_coverage(runs: list[CaseRun]) -> dict[str, float] | None:
    """How much of the LLM stage a run actually measured, tier-wide. ``None`` when it ran none.

    **This is the plan-versus-issued gap, carried by the run that has it.** ``eval plan`` prices a
    tier's documents before anything is spent, and the first graded pass then issued 68 of 141
    planned requests without the summary saying so anywhere. ``documents_planned`` here is what this
    run's own plans asked for, so the gap needs no second command and cannot be read against a plan
    of a different tier on a different day. A large one means the report's ``harvest`` numbers
    describe a fraction of the corpus, and names which fraction:

    - ``documents_planned`` against ``documents_extracted`` — how much of the prose was read;
    - ``documents_extracted`` against ``cost.llm_calls`` — what retries cost on top of it;
    - ``cases_unmeasured`` — the cases a green ``harvest.matched`` says nothing whatever about.

    A case that skipped before harvest (an unreachable package) contributes nothing here and is
    counted where every other skip is. Comparing this ``documents_planned`` against ``eval plan``'s
    ``n_documents`` is what surfaces those.
    """
    harvests = [r.harvest for r in runs if r.harvest is not None]
    if not harvests:
        return None
    planned = sum(h.n_documents for h in harvests)
    failed = sum(h.n_documents_failed for h in harvests)
    statuses = [h.status for h in harvests]
    return {
        "cases": float(len(harvests)),
        "cases_complete": float(statuses.count("complete")),
        "cases_partial": float(statuses.count("partial")),
        "cases_unmeasured": float(statuses.count("unmeasured")),
        "documents_planned": float(planned),
        "documents_extracted": float(planned - failed),
        "documents_failed": float(failed),
        # Assertions this pass could not reach a verdict on. Excluded from `field_accuracy`, exactly
        # as a skipped case is excluded from every rate — which is why it has to be reported here.
        "assertions_unchecked": float(sum(len(h.unchecked) for h in harvests)),
    }


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
    case: Case,
    doc_paths: list[Path],
    *,
    meter: TokenMeter,
    model: str | None,
) -> tuple[HarvestGrade, dict[str, int], list[Assertion], list[DocumentSubject]]:
    """normalize -> extract -> verify over the case's prose. Only verified claims are graded.

    "The case's prose" now means three things: the documents a human put beside the case, the prose a
    fingerprint package carried inside itself (``doc_paths`` is whichever of the two materialize
    surfaced), and each archive record rendered as its own document. The record path is what lets a
    claim name a sample, so a harness that read only the human's documents could never grade one.

    Every request goes through ``meter``, which is the case's and spans its trials — so the call
    count this returns is a **delta** over that meter, not a fresh count. It proxies the provider's
    identity, so ``ExtractorProvenance`` still records who answered.

    **The fan-out is asked for a partial result**, which is the opposite of what the compiler asks
    it for and is right for opposite reasons. A harness exists to measure the stage; a case that
    reports nothing because one of its documents was never answered has measured nothing, and five
    of seven prose-carrying benchmark cases did exactly that on the first graded tier pass. What
    survives instead is every document that did answer, plus a per-document record of the ones that
    did not — and the fields those documents alone could have carried are reported as **unchecked**
    rather than as claims the model failed to make.
    """
    specs = load_all_specs()
    called_before = meter.n_exchanges

    # Which documents exist, and the fan-out that pays for them, is `harvest/plan.py`'s job — the
    # same call `harvest extract` makes. This loop, not the case loop, is where a real dataset's cost
    # lives: an archive record with prose used to be its own document, so GSE126954's runs made one
    # call each inside ONE case, and running cases in parallel cannot touch that because the case is
    # the unit above it. The plan collapses a sample's runs into one document, and its results come
    # back positionally — required rather than tidy, because `verify_drafts` and the last-wins
    # `by_field` below (the HARVEST grade: did the model say this at all) both read it in order.
    plan = plan_extraction(
        documents=[normalize_document(p) for p in doc_paths], records=case.records
    )
    docs: list[NormalizedDoc] = list(plan.documents)
    drafts: list[AssertionDraft] = []
    usage: dict[str, int] = {}
    extractor: ExtractorProvenance | None = None
    #: An outcome has FOUR halves and this loop used to read two. The drafts a document produced and
    #: what it cost were kept; the drafts it produced *malformed* and how the call was made were
    #: dropped on the floor — so a model returning nothing but broken JSON graded identically to one
    #: that read the document and found nothing in it.
    rejected: list[dict[str, Any]] = []
    mode: dict[str, Any] = {}

    outcomes = extract_planned(plan, specs, provider=meter, model=model, partial=True)

    unanswered: dict[str, str] = {}
    for doc, outcome in zip(docs, outcomes, strict=True):
        drafts.extend(outcome.drafts)
        extractor = outcome.extractor
        rejected.extend(outcome.rejected)
        mode = dict(outcome.mode) or mode
        for k, v in outcome.usage.items():
            usage[k] = usage.get(k, 0) + v
        if outcome.failure is not None:
            unanswered[doc.doc_sha256] = outcome.failure

    assert extractor is not None  # docs is non-empty (checked by the caller via has_prose)
    report = verify_drafts(drafts, docs, extractor=extractor)
    accepted: list[Assertion] = report.assertions
    by_field = {a.field: a for a in accepted}
    rejected.extend(report.rejected)

    # Fields a document that never answered would have been asked. A NEGATIVE verdict is a claim
    # about the whole accepted set — "the model read everything and did not say this" — so one hole
    # in that set unsettles it; a POSITIVE one needs a single document and is unaffected. Keying on
    # "no answering document was asked" instead was measurably too weak: on `GSE234962` the paper
    # aborted while the supplementary table answered, both are dataset-scoped, and the organism the
    # paper writes fifteen times was reported as a claim the model failed to make.
    blinded: set[str] = {f for d in docs if d.doc_sha256 in unanswered for f in plan.asked(d)}

    grade = HarvestGrade(
        n_calls=meter.n_exchanges - called_before,
        n_documents=len(docs),
        n_documents_failed=len(unanswered),
        assertions=list(accepted),
        rejected=rejected,
        documents=[_document_row(d, unanswered.get(d.doc_sha256)) for d in docs],
        mode=mode,
    )
    for want in case.expected.assertions:
        got = by_field.get(want.field)
        if got is not None and str(got.value) == want.value:
            grade.matched.append(want.field)
        elif want.field in blinded:
            # A document that would have been asked this was never answered. Reporting it `missing`
            # charges the model for prose it never got to read, and puts a silent zero into
            # `field_accuracy` — the shape a skipped case is excluded from every rate to avoid.
            grade.unchecked.append(want.field)
        else:
            grade.missing.append(want.field)
    grade.hallucinated = [f for f in case.expected.forbidden_fields if f in by_field]

    # No hypothesis is built here. `accepted` is returned whole and the CALLER reduces it with the
    # compiler's own `chemistry_hypothesis` — one reduction, both callers. This function used to
    # take `by_field["library.chemistry"]`, i.e. the LAST document to claim one, which is a
    # different answer from the compiler's on exactly the datasets where it matters.
    subjects = [
        DocumentSubject(doc_sha256=d.doc_sha256, scope=d.scope, subject=d.subject) for d in docs
    ]
    return grade, usage, accepted, subjects


def _document_row(doc: NormalizedDoc, failure: str | None = None) -> dict[str, Any]:
    """One sent document, small enough to carry per case: what a Span's sha256 resolves back to.

    Deliberately not the plan's own report. That one carries the fields asked of each document and
    every archive record folded into it — hundreds of accessions for a collapsed run document, none
    of which a reader of a graded claim wants. ``scope`` is the load-bearing entry: it is what lets
    one exchange stand for its level when a run has more of them than anyone will read.

    ``failure`` is the provider's own message where this document was never answered, and it lives
    here rather than in a list of its own because *which* document went unanswered is the question a
    failure produces — and this row is already where a sha resolves into something a human reads.
    """
    row: dict[str, Any] = {
        "doc_sha256": doc.doc_sha256,
        "source": doc.source_basename,
        "scope": doc.scope,
        "subject": doc.subject,
        "n_chars": len(doc.text),
    }
    if failure is not None:
        row["failure"] = failure
    return row


def _merge_harvest(grades: list[HarvestGrade]) -> HarvestGrade:
    """Across trials, keep the WORST — never the last.

    Extraction is nondeterministic, so a field the model invented in 1 of 3 trials is a field it *can*
    invent, and a field it extracted in only 2 of 3 is not one you can rely on. Reporting the final
    trial (the bug this replaces) let a real hallucination vanish on a re-run — exactly the illusion
    trials exist to dispel.

    So: ``hallucinated`` and ``missing`` union (any trial failing is a failure), ``matched`` is what
    some trial got and none missed — which is the intersection whenever every trial could check
    every field, and is not when one of them could not — and fields that come and go are surfaced as
    ``unstable`` rather than silently averaged into a rate. ``unchecked`` intersects, because one
    trial reaching the document is enough to make the verdict a real one.

    The claims follow the same rule, which is why they are not merged the way the flattened dict was
    (``update``, last trial wins): a claim only one trial made is exactly what ``unstable`` names, so
    it has to survive the merge for the page to be able to show it. Distinct claims union; the same
    claim made three times is one claim. Refusals concatenate instead, because a draft refused in
    every trial cost three refusals — which is what the count has always said.
    """
    if len(grades) == 1:
        return grades[0]
    merged = HarvestGrade()
    merged.hallucinated = sorted({f for g in grades for f in g.hallucinated})
    merged.missing = sorted({f for g in grades for f in g.missing})
    seen = {f for g in grades for f in g.matched}
    merged.matched = sorted(seen - set(merged.missing))
    merged.unchecked = sorted(set.intersection(*(set(g.unchecked) for g in grades)))
    merged.rejected = [r for g in grades for r in g.rejected]
    merged.assertions = _distinct_assertions(grades)
    merged.documents = _distinct_documents(grades)
    merged.mode = next((g.mode for g in grades if g.mode), {})
    # cost is spent per trial, not merged away — the same for the requests and the documents behind
    merged.n_calls = sum(g.n_calls for g in grades)
    merged.n_documents = sum(g.n_documents for g in grades)
    merged.n_documents_failed = sum(g.n_documents_failed for g in grades)
    merged.unstable = sorted(seen - set(merged.matched))
    return merged


def _distinct_assertions(grades: list[HarvestGrade]) -> list[Assertion]:
    """Every distinct claim any trial made, in the order it was first made.

    Identity is the claim itself — field, value, and the quote it rests on in the document it cites —
    never the ``id``, which carries the draft's position in a batch and so differs across trials for
    what is plainly the same claim.
    """
    out: list[Assertion] = []
    seen: set[tuple[str, str, str, str]] = set()
    for grade in grades:
        for a in grade.assertions:
            key = (a.field, a.value, a.span.doc_sha256, a.span.quote)
            if key in seen:
                continue
            seen.add(key)
            out.append(a)
    return out


def _distinct_documents(grades: list[HarvestGrade]) -> list[dict[str, Any]]:
    """The documents the trials sent, deduplicated. Every trial sends the same list — the plan is a
    pure function of the case — so this is one list, not N stacked.

    Answering beats not answering. A document read in one trial and unanswered in another *was*
    read, and the row a reader looks a sha up in should say so; the fact that it also failed
    somewhere survives in ``n_documents_failed``, which counts per trial because each failure was
    its own paid attempt.
    """
    out: list[dict[str, Any]] = []
    at: dict[str, int] = {}
    for grade in grades:
        for row in grade.documents:
            sha = str(row.get("doc_sha256", ""))
            if sha not in at:
                at[sha] = len(out)
                out.append(row)
            elif "failure" not in row:
                out[at[sha]] = row
    return out


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
