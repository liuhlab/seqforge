"""Score / compile output models — the wire formats between ``score`` and ``compile``.

Every stage output is a first-class model so ``schema export`` references only types that exist and
every stdout object round-trips through JSON Schema. ``TechScore`` is JSON-safe: no ``+/-inf`` ever
crosses the JSON boundary.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

from .base import Basis, ChemistryId, Rung, Uri
from .blocker import Blocker, ValidationWarning
from .conflict import Conflict, ConflictPosition, Decidable
from .manifest import RuntimeEnv


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
    """The output of ``compose``: selected modules, emitted config paths, and the gate verdicts."""

    modules: list[ModuleSelection]
    config_path: Uri
    units_path: Uri
    gate: dict[str, Literal["pass", "fail"]]
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
