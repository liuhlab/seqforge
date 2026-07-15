"""Project verified :class:`Assertion`s onto the instructable surface. Deterministic; no LLM (R2).

A user may tell seqforge what to do, in prose, in a document they hand us:

    seqforge harvest extract PAPER.pdf --instruction notes.md
    #                        ^ reference            ^ instruction

**The document's ROLE decides the basis, not the model's reading of the sentence.** An instruction is
imperative ("align this in GeneFull mode") and a claim is declarative ("we used GeneFull"), and it is
tempting to ask the LLM to tell them apart. Don't: that classification has no quote to check it
against, so it lands in exactly the class :func:`~seqforge.harvest.verify.entails` is provably blind
to — and this model's one known failure mode is *field misassignment*, a real quote correctly copied
onto the wrong field. Role is decided by **which flag the document arrived under**, which code owns
and a shell history records.

Role also subsumes mood by fiat, and fiat is the right tool here precisely because it is checkable: if
you wrote a descriptive sentence inside a file you handed us *for this purpose*, we honor it. You
authored the file for us; every sentence in it is addressed to us.

Note what cannot reach this module: a ``processing.*`` draft from a **reference** document.
:mod:`seqforge.harvest.fields` refuses it at verify time, so a downloaded methods PDF can never steer
the pipeline. That is a deliberate narrowing of "instructions may live among the unstructured
metadata", and it costs nothing: with the default counting everything (R15), a paper saying "we used
GeneFull" describes a subset of what we already compute.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from ..models.assertion import Assertion
from ..models.base import Basis
from ..models.conflict import Conflict, ConflictPosition

#: The fields an *instruction document* may set. A strict subset of the instructable surface: a CLI
#: flag may set more (threads, annotation_name), because a flag is typed by a human at run time while
#: this is a model's reading of prose.
#:
#: `annotation_name` is deliberately absent — it is a liulab-genome registry name (`WS298`), a
#: vocabulary no paper writes in, so asking for it would only invite a guess.
INSTRUCTABLE_FIELDS: frozenset[str] = frozenset(
    {
        "processing.quantification",
        "processing.genome.assembly",
    }
)


@dataclass(frozen=True)
class Instruction:
    """One processing directive: span-verified, carrying the basis its document's ROLE assigned."""

    field: str
    value: str
    basis: Basis
    evidence: list[str]


def instructions_from_assertions(
    assertions: Sequence[Assertion], *, instruction_docs: frozenset[str] = frozenset()
) -> tuple[list[Instruction], list[Conflict]]:
    """Project verified assertions onto the instructable surface.

    ``instruction_docs`` is the set of ``doc_sha256`` handed to us under ``--instruction``; code knows
    it because code chose it. Anything sourced elsewhere is dropped rather than downgraded — a
    reference document has no business setting intent at all.

    Two instructions disagreeing **at the same precedence** on one field is a :class:`Conflict`
    (exit 4): there is no tiebreak, and R6 applies to intent exactly as it applies to truth. Note this
    is a *same-basis* disagreement; an instruction differing from a policy default is not a conflict at
    all — that is what an instruction IS.
    """
    out: list[Instruction] = []
    for a in assertions:
        if a.field not in INSTRUCTABLE_FIELDS:
            continue  # experiment.*/library.* travel a different path; off-surface fields are dropped
        if not (a.span_verified and a.entailment_ok):
            continue  # belt and braces: verify already refuses to emit these
        if a.span.doc_sha256 not in instruction_docs:
            continue  # a reference doc may not steer the pipeline (see the module docstring)
        out.append(
            Instruction(field=a.field, value=a.value, basis="user_confirmed", evidence=[a.id])
        )

    conflicts: list[Conflict] = []
    by_field: dict[str, list[Instruction]] = {}
    for ins in out:
        by_field.setdefault(ins.field, []).append(ins)
    for field, group in sorted(by_field.items()):
        values = {i.value for i in group}
        if len(values) > 1:
            conflicts.append(
                Conflict(
                    id=f"conflict-instruction-{field.replace('.', '-')}",
                    field=field,
                    positions=[
                        ConflictPosition(
                            value=i.value, basis=i.basis, evidence=list(i.evidence), confidence=1.0
                        )
                        for i in group
                    ],
                    kind="asserted_vs_asserted",
                    # the first real consumer of Decidable's long-unused "user" member: only the
                    # person who wrote both sentences can say which one they meant.
                    decidable_by=["user"],
                    status="open",
                )
            )
    return out, conflicts


__all__ = ["Instruction", "INSTRUCTABLE_FIELDS", "instructions_from_assertions"]
