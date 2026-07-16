"""``Conflict`` — a surfaced disagreement between truths, never auto-picked.

``positions`` generalizes the common observed/asserted pair; ``status="benign"`` is the §12 escape
hatch for two confusable KB entries that emit identical ``backend.params``.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

from .base import Basis, Confidence, Rung

Decidable = Literal["reads", "onlist", "metadata", "alignment", "user"]
"""How a divergent pair could be separated. Includes ``onlist`` — the rung-3 mechanism §12 uses."""


class ConflictPosition(BaseModel):
    """One side of a disagreement. ``value`` is the canonical string form (fields are heterogeneous)."""

    value: str
    basis: Basis
    evidence: list[str] = Field(default_factory=list)
    confidence: Confidence


class Resolution(BaseModel):
    """How an open conflict was ultimately settled."""

    chosen_value: str
    basis: Basis
    rung: Rung
    decided_by: Literal["code", "user", "benign_equivalence"]
    note: str | None = None


class Conflict(BaseModel):
    """A first-class, surfaced disagreement between truths."""

    id: str
    field: str
    positions: list[ConflictPosition] = Field(min_length=2)
    kind: Literal["observed_vs_asserted", "asserted_vs_asserted", "other"]
    decidable_by: list[Decidable]
    status: Literal["open", "resolved", "benign"] = "open"
    resolution: Resolution | None = None
