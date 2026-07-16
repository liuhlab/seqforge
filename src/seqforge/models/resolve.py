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
    """One ranked technology candidate with its role assignment and per-field deciding rungs."""

    technology: ChemistryId
    score: TechScore
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


class ComposeResult(BaseModel):
    """The output of ``compose``: selected modules, emitted config paths, and the gate verdicts.

    ``gate`` maps a gate name (``params`` / ``wiring`` / ``e2e``) to its verdict. ``skip`` is
    first-class and distinct from ``pass``: the wiring and e2e gates depend on a toolchain seqforge
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
    gate: dict[str, Literal["pass", "fail", "skip"]]
    params_preview: dict[str, object]


class RunResult(BaseModel):
    """The output of the headless ``run`` entrypoint."""

    dataset_id: str
    stages: dict[str, str]
    exit: int
    artifacts: dict[str, Uri]
    provenance_id: str


class EvalReport(BaseModel):
    """The output of ``eval run``: the metrics tracked on every prompt/KB/resolve change."""

    n_cases: int
    field_accuracy: float
    false_accept_rate: float
    false_refuse_rate: float
    questions_asked: dict[str, float]
    cost: dict[str, float]
    per_case: list[dict[str, object]]
