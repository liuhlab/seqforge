"""Score / compile output models — the wire formats between ``score`` and ``compile``.

Every stage output is a first-class model so ``schema export`` references only types that exist and
every stdout object round-trips through JSON Schema. ``TechScore`` is JSON-safe: no ``+/-inf`` ever
crosses the JSON boundary.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

from .base import Accession, Basis, ChemistryId, Rung, Sha256, Uri
from .blocker import Blocker, ValidationWarning
from .conflict import Conflict, ConflictPosition, Decidable
from .evidenced import EvidencedStr, EvidencedTaxid
from .processing import RuntimeEnv


class TechScore(BaseModel):
    """JSON-safe technology score. ``forbidden`` == a requires/excludes gate failed."""

    technology: ChemistryId
    status: Literal["forbidden", "scored"]
    value: float | None = None
    reason: str | None = None


class RoleAssignment(BaseModel):
    """The bipartite files->roles solution. ``assignment`` maps role_id -> file sha256."""

    assignment: dict[str, str]
    unassigned: list[str] = Field(default_factory=list)


class Candidate(BaseModel):
    """One ranked technology candidate with its read set, role assignment and per-field deciding rungs.

    One per technology — a chemistry never competes with itself, even when it publishes more than one
    sequencing configuration. The read set that won is recorded here, in the resolve artifacts, because
    this is where "how this was decided" lives; the **manifest gains no field for it**, since its read
    layout already lists exactly this set's reads and the composer reads the reads and never the name.
    """

    technology: ChemistryId
    score: TechScore
    #: Which of the chemistry's read sets the bytes selected: ``full`` (the maximal set every spec has)
    #: or a declared subset such as ``se``. ``full`` for every spec that declares no alternatives.
    read_set: str = "full"
    role_assignment: RoleAssignment
    rung_resolved: dict[str, int]
    equivalence_members: list[ChemistryId] = Field(default_factory=list)
    evidence: list[str] = Field(default_factory=list)


class Question(BaseModel):
    """A human-facing question. The code decides the option set; a human/agent picks among it."""

    id: str
    field: str
    prompt: str
    options: list[str]
    decidable_by: list[Decidable]
    rung: Rung


class Decision(BaseModel):
    """A persisted answer to an already-posed question (agents propose, code decides)."""

    question_id: str
    chosen: str
    basis: Basis
    actor: Literal["user", "agent", "code"]
    evidence: list[str] = Field(default_factory=list)


class ResolveResult(BaseModel):
    """The output of ``resolve score``: ranked candidates, surfaced conflicts, and open questions."""

    dataset_id: str
    kb_version: str
    rung_reached: Rung
    candidates: list[Candidate]
    conflicts: list[Conflict]
    questions: list[Question]
    blockers: list[Blocker] = Field(default_factory=list)


class ResolvedSample(BaseModel):
    """One biological sample, the files that carry it, and what we know about it.

    ``sample_id`` always exists and is always code's: it is the archive's sample accession when a
    record was joined, and the run grouping otherwise. There is no path on which a language model
    names a sample — that is the whole reason a per-record document works.

    ``attributes`` is keyed by an NCBI harmonized attribute name (``strain``, ``tissue``,
    ``dev_stage``). Open-keyed rather than a fixed set of typed fields, because the key space is
    NCBI's 960 and mirroring 960 names into pydantic fields is the hand-maintained contract this repo
    keeps getting bitten by. Enforcement lives in the validator, against the shipped vocabulary.
    """

    sample_id: str
    accession: Accession | None = None
    attributes: dict[str, EvidencedStr] = Field(default_factory=dict)
    #: The files carrying this sample, by content hash. ``fill`` turns these into manifest URIs; the
    #: resolver does not know what a URI is and should not.
    file_shas: list[Sha256] = Field(default_factory=list)


class ProjectFacts(BaseModel):
    """The study, as the archive declares it. Structured facts only (design decision, 2026-07-16).

    Not ``Evidenced``: none of this is an interpretation. The record says the title is X and we copied
    X, exactly as we copy a file's ``sha256`` — a basis and a confidence would be theatre. The study
    *abstract* is deliberately absent: it is prose, it belongs in a document a quote can grep back
    into, and pasting it into a content-addressed manifest would make the dataset's identity depend on
    a paragraph of English.
    """

    accession: Accession | None = None
    title: str | None = None
    center: str | None = None
    data_type: str | None = None
    released: str | None = None


class MetadataResolution(BaseModel):
    """The output of the metadata resolver — the sibling of ``resolve score``, over records not bytes.

    Same discipline and the same shape of answer, because it has the same ways of being wrong: it
    emits evidenced values, and can refuse with a ``Blocker`` (a record whose runs do not match the
    files on disk). A sample-attribute *disagreement* is different from a refusal: the resolver decides
    it — a stronger authority wins, equal authorities leave the field null — so it is a non-blocking
    ``warning``, not something that stops the dataset compiling. A dataset with no archive record and
    no prose resolves to samples-with-no-facts, which is a real answer and the honest one — most
    sequencing data has never had an accession.
    """

    samples: list[ResolvedSample] = Field(default_factory=list)
    project: ProjectFacts | None = None
    #: The organism, when a record or a document declares it. ``None`` means nobody said, and the
    #: caller must supply it — never a default, because a wrong taxid aligns cleanly against the wrong
    #: genome and nothing downstream ever asks again.
    organism: EvidencedTaxid | None = None
    #: Non-blocking notes on sample attributes the resolver decided under disagreement (precedence, or
    #: null when it could not pick). Surfaced so the resolution is never silent; they never block.
    warnings: list[ValidationWarning] = Field(default_factory=list)
    blockers: list[Blocker] = Field(default_factory=list)


class ArbitrationRequest(BaseModel):
    """LLM job (b) INPUT schema (opt-in ``resolve adjudicate``)."""

    conflict_id: str
    positions: list[ConflictPosition]


class ArbitrationResponse(BaseModel):
    """LLM job (b) OUTPUT schema — references a position by index, re-derives no values."""

    conflict_id: str
    chosen_index: int
    rationale: str


class ValidationReport(BaseModel):
    """The output of ``manifest validate``."""

    ok: bool
    blockers: list[Blocker]
    conflicts: list[Conflict]
    warnings: list[ValidationWarning] = Field(default_factory=list)


class ModuleSelection(BaseModel):
    """One selected, versioned workflow module and the runtime env it runs in."""

    name: str
    version: str
    env: RuntimeEnv


class SampleAdmission(BaseModel):
    """The read floor the LIVE knowledge base applied, and the samples it kept out of this compile.

    Present whenever the chemistry's spec declares ``min_input_reads`` — with ``excluded`` empty when
    every sample cleared it, so a reader can tell a gate that ran from one that never existed. Absent
    for the sixteen non-plate entries, none of which declares a floor.

    **The manifest records none of this.** The measurement is in the dataset (per file, in
    ``provenance``); the *verdict* is recomputed at every compile against whatever knowledge base is
    loaded, and lands here and in the pipeline directory. Freezing it into the write-once manifest
    would make a dataset's identity a function of a threshold — raise the floor and the same bytes
    become a different dataset.
    """

    #: The floor, in reads, as the loaded spec declares it.
    threshold: int
    #: Every sample the manifest carries — the denominator of ``summary``. The manifest keeps all of
    #: them; only this pipeline is shorter.
    declared: int
    #: sample id -> its **exact** read count, for every sample below ``threshold``. Exact rather than
    #: extrapolated: a file shallow enough to fail a floor of this size was read to EOF inside the
    #: probe's budget.
    excluded: dict[str, int] = Field(default_factory=dict)
    #: The exclusion record written beside the config, or ``None`` when nothing was excluded and there
    #: is therefore nothing to explain.
    record_path: Uri | None = None
    #: The totals line — *"240 of 1440 cells dropped"*. Rendered once and read twice, as the record's
    #: headline and as the compose verb's one line on the human stream, so the two cannot disagree
    #: about how much was lost. A sentence in a machine surface for the same reason ``Blocker`` carries
    #: one: the count alone does not say what it is a count of.
    summary: str


class GateVerdict(BaseModel):
    """One gate's judgement and the reason it reached it, in one envelope.

    ADR-0006's shape, applied to a gate: an envelope tracks a **decision**, and the test for whether
    two things belong in one is whether they can disagree. A status beside a parallel
    ``dict[str, list[str]]`` of reasons can — a ``fail`` with no entry, or an entry against a gate that
    passed, are both representable and neither is detectable. Riding together, they cannot.

    Three gates are three independent judgements, so there are three of these and not one.

    ``reason`` is empty on a clean ``pass`` and carries at least one line otherwise — **``skip``
    included**. A gate that reports ``skip`` is the whole reason this result distinguishes ``skip``
    from ``pass``, and until now it said only that it had not run, never what it was waiting for.
    """

    status: Literal["pass", "fail", "skip"]
    #: Why, in the words of whoever decided. For ``wiring`` that is snakemake's own stderr — the
    #: module's `InputFunctionException` message is written to be read by the person who hit it, and
    #: discarding it turned a wiring failure into an unexplained refusal that a silent pass was hard to
    #: tell from. Bounded (:data:`~seqforge.compose.gates.REASON_TAIL_LINES`), because a result object
    #: is a machine surface and a subprocess's stderr has no ceiling.
    reason: list[str] = Field(default_factory=list)


class ComposeResult(BaseModel):
    """The output of ``compose``: selected modules, emitted config paths, and the gate verdicts.

    ``gate`` maps a gate name (``params`` / ``wiring`` / ``e2e``) to its :class:`GateVerdict`. ``skip``
    is first-class and distinct from ``pass``: the wiring and e2e gates depend on a toolchain seqforge
    does not own (snakemake; STAR + liulab-genome + network), and a gate that reports ``pass``
    because it never ran would let green CI be mistaken for coverage.
    """

    modules: list[ModuleSelection]
    #: The run wrapper — **the thing a user submits**, and the reason `compose` exists. It is named
    #: here beside the config rather than left to be discovered on disk: it was previously written as
    #: a side effect of a gate that could not run, so `compose` reported success while emitting no
    #: runnable artifact at all. A deliverable absent from the result object is a deliverable nobody
    #: notices is missing.
    snakefile_path: Uri
    config_path: Uri
    units_path: Uri
    gate: dict[str, GateVerdict]
    #: The emitted config, as compose resolved it. It carried ``params_problems`` until the params
    #: gate's reason moved onto its own verdict — a preview of the config is what this field is named
    #: for, and a gate's findings were never that.
    params_preview: dict[str, object]
    #: What the live knowledge base's read floor admitted. ``None`` when the chemistry declares no
    #: floor, which is every dataset seqforge compiles today.
    admission: SampleAdmission | None = None
    #: The knowledge base **loaded at compile time**: what ``plan`` read for every ``backend.params``
    #: key, for the derived keys, and for the admission floor above, and what ``run_id`` folds
    #: (ADR-0037). On the result rather than compared inside a CLI, because ``compose`` is not the
    #: only verb that compiles — ``run`` chains the same call, and a disclosure only the human stream
    #: carried was one the headless path could not make.
    kb_version: str
    #: The knowledge base the manifest recorded at fill — **the KB that decided this chemistry**
    #: (ADR-0037), never rewritten by a compile. Beside the live one because naming either alone says
    #: a version without saying which of the two produced the params.
    manifest_kb_version: str

    @property
    def kb_moved(self) -> bool:
        """Did the knowledge base that decided this chemistry and the one that compiled it differ?

        One definition of the question, so no verb spells the comparison itself. **Derived, never
        stored**: a third value kept in step by hand is the copy that goes stale, and this one would
        go stale in the direction that reports agreement where there is none.

        A plain property and deliberately **not** a ``computed_field``. A computed field serialises
        into ``model_dump`` but appears only in pydantic's *serialization* schema, so ``schema
        export`` — which is the schema (R1) — would not have described a key the headless summary
        emits. The JSON surface is therefore the two versions themselves, both exported and both
        required; a consumer compares them, which is the same one line this property is.
        """
        return self.kb_version != self.manifest_kb_version


class RunResult(BaseModel):
    """The output of the headless ``run`` entrypoint."""

    dataset_id: str
    stages: dict[str, str]
    exit: int
    artifacts: dict[str, Uri]
    provenance_id: str


class RecordSetSummary(BaseModel):
    """What a record set turned out to SAY, once it was established that it parses.

    Every field answers a question an author asks about a file they just typed. ``n`` and
    ``n_filenames`` answer "did it see everything I wrote"; ``fused`` answers the only question that
    made the file worth writing.
    """

    #: Where the set came from. Semantic, not decorative: it selects the dialect the loader enforced,
    #: and it decides whether fusing runs is remarkable or the archive's ordinary shape.
    source: str
    #: What the set was asked for -- an accession for a fetched one, the file's own name for a typed
    #: one, which is what refusals downstream print when they name the set.
    query: str
    #: Records per level, all four, always -- a set that reports ``experiment: 0`` and one that never
    #: had the level at all look identical to a caller unless every level is always reported.
    n: dict[str, int]
    #: Files claimed across every record. The number to compare against `ls *.fastq.gz | wc -l`: a
    #: set that leaves a file unclaimed is refused at the join, and this is where that is visible
    #: before anything is compiled.
    n_filenames: int
    #: sample id -> the runs pointing at it, for every sample claimed by MORE THAN ONE run. This is
    #: the declaration a filename could not have made, so it is echoed back rather than counted.
    #:
    #: Empty for anything but a hand-written set, and that is not a limitation. In an archive
    #: transcript several runs under one sample is the ordinary deposit shape, so reporting it would
    #: print every run of every dataset and mean nothing -- the same reason the fuse warning is keyed
    #: on the source rather than fired on any fusion at all.
    fused: dict[str, list[str]] = Field(default_factory=dict)


class RecordSetResult(BaseModel):
    """One record set, the verdict on it, and what it says. The stdout object for both record verbs.

    One type rather than two because ``records new`` and ``records validate`` answer the same
    question about the same artifact: one about a file somebody wrote, one about the file it just
    wrote for them. Splitting them would let the two drift into disagreeing about what a record set
    is worth reporting, and a draft whose envelope says less than a validate of the identical file is
    a needless second shape for a caller to learn.
    """

    #: The file, as the caller named it.
    records: str
    #: The refusal channel. Read by ``exit_code_for_report``, so the exit code and this object can
    #: never disagree about whether the set was usable.
    report: ValidationReport
    #: ``None`` exactly when the set did not load -- there is nothing truthful to say about the
    #: contents of a file that was refused, and an empty summary would read as an empty set.
    summary: RecordSetSummary | None = None


class EvalReport(BaseModel):
    """The output of ``eval run``: the metrics tracked on every prompt/KB/resolve change."""

    n_cases: int
    #: Who produced the LLM-dependent numbers — ``{provider, model, prompt_version}``. ``None`` on a
    #: ``--no-llm`` run, which has no extractor at all.
    #:
    #: **A baseline is model-scoped**, and now demonstrably so: the DeepSeek preset serves two V4
    #: models and defaults to the cheap one, so two reports can carry the same prompt, the same
    #: corpus and different numbers. The same prompt on a different model is a different extractor
    #: (ADR-0009), and a report that does not name its own is a number nobody can reproduce or
    #: compare against the last one.
    extractor: dict[str, str] | None = None
    field_accuracy: float
    false_accept_rate: float
    false_refuse_rate: float
    questions_asked: dict[str, float]
    cost: dict[str, float]
    #: How much of the LLM stage this run actually measured: documents planned against documents
    #: extracted, and how many cases were ``complete`` / ``partial`` / ``unmeasured``. ``None`` on a
    #: run that harvested nothing, because zeros would read as a stage that ran and found nothing.
    #:
    #: It is here because a skip poisons no rate — correctly — and that is exactly what made a
    #: half-measured pass invisible: `harvest.matched` was reported for whichever cases survived,
    #: and the summary said nothing about the ones that never ran. This is the number that says so.
    harvest: dict[str, float] | None = None
    #: The run directory this report was written into — the one holding ``report.json`` and
    #: ``transcripts/<case>.jsonl``. ``None`` when the run wrote nothing (no ``-C``), which is the
    #: shape every report had before evals honoured "disk is state". Each case's own
    #: ``transcript`` key in ``per_case`` is relative to this, so the directory survives being moved
    #: or downloaded as a CI artifact while this records where it was produced.
    run_dir: str | None = None
    per_case: list[dict[str, object]]


class CasePlanRow(BaseModel):
    """One case's share of an ``--llm`` bill, before any of it is paid.

    A case that could not be planned carries ``skipped`` and zeros for everything else: a skip is not
    a free case, it is a case whose price is unknown, and reporting 0 tokens for it without saying so
    would understate the tier.
    """

    case: str
    #: Documents this case would read. NOT the exchange count — same-ask documents share a request.
    n_documents: int = 0
    #: Extraction requests this case would issue, before retries. It is the exchange count, which is
    #: also the floor on ``llm_calls``: a request whose first attempt is refused costs two.
    n_requests: int = 0
    n_records_read: int = 0
    n_records_collapsed: int = 0
    #: Records sent as their DISTINCTIVE BYTES only, the invariant they share having been read once.
    #: Beside ``n_records_collapsed`` rather than inside it because a record that cost nothing and a
    #: record that cost what it is worth are two facts (ADR-0031) — and without this column the
    #: mechanism reaches ``eval plan`` as nothing but an unexplained drop in ``n_chars``.
    n_records_reduced: int = 0
    n_chars: int = 0
    #: Input only, and the system prefix charged once **per request** — charging it per document is
    #: what made a fan-out over one-line archive records expensive. Output is not estimable: the
    #: model decides how many claims a document supports.
    estimated_input_tokens: int = 0
    skipped: str | None = None
    #: ``unavailable`` (this machine) or ``absent`` (the corpus never held the package). A plain
    #: string rather than the harness's ``SkipKind``, because a wire model may not import the harness.
    skip_kind: str | None = None


class EvalPlanReport(BaseModel):
    """The output of ``eval plan``: what an ``--llm`` run over a whole TIER would ask, and cost.

    ``harvest extract --dry-run`` answers the same question for one dataset. This is the tier-wide
    half, and it exists because the decision it informs — *is this run worth its money* — is taken
    over a corpus and was answerable only by spending one.

    Every token here is an **input** token, and the estimate is a lower bound against a Ceiling:
    ``--ceiling`` counts output and cache writes too, and neither is knowable before the model
    answers. So a case in :attr:`estimated_over_ceiling` will certainly breach, and a case absent
    from it may still.
    """

    #: Cases planned — skips excluded, exactly as ``EvalReport.n_cases`` excludes them.
    n_cases: int
    #: Of those, the ones that would reach a model at all: a case with neither prose nor records
    #: costs nothing under ``--llm`` and is not a saving worth hiding inside an average.
    n_reaching_a_model: int = 0
    n_skipped: int = 0
    #: What ``eval run --trials N`` would multiply the whole bill by. Every total below already
    #: carries it; the per-case rows are per trial, because a trial is the repeated unit.
    trials: int = 1
    n_documents: int = 0
    #: Requests the tier would issue, before retries. Below ``n_documents`` by however much batching
    #: same-ask documents bought; equal to it on a tier where no two documents share an ask.
    n_requests: int = 0
    n_records_read: int = 0
    n_records_collapsed: int = 0
    #: Records the tier sends as their distinctive bytes alone. Read, and costing a document each —
    #: the fold that pays for nothing is ``n_records_collapsed`` and these are not it (ADR-0031).
    n_records_reduced: int = 0
    n_chars: int = 0
    #: The stable prefix, byte-identical on every request — which is what makes prefix caching work,
    #: and why it is charged per request rather than per run.
    system_prompt_chars: int = 0
    estimated_input_tokens: int = 0
    #: The per-case token Ceiling this plan was read against, or ``None`` if none was given.
    ceiling: int | None = None
    #: Cases whose estimated INPUT alone already clears the ceiling. A lower bound, never a list of
    #: the only cases that can breach.
    estimated_over_ceiling: list[str] = Field(default_factory=list)
    per_case: list[CasePlanRow] = Field(default_factory=list)
