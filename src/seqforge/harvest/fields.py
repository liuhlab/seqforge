"""The closed vocabulary of manifest fields an LLM may assert — enforced by code, not by the prompt.

``AssertionDraft.field`` is a plain ``str``, and it has to stay one: the wire schema must fit inside
every provider's strict-schema subset. That makes the vocabulary a **code** obligation rather than a
type one, and until this module existed there was nothing discharging it. ``DEFAULT_FIELDS`` was only
ever interpolated into the prompt; ``verify`` never compared a returned draft against it. So the
model could name any field it liked and both span-verification checks would still pass:

    field: "processing.params.outFilterMismatchNmax"   value: "10"
    quote: "add --outFilterMismatchNmax 10 to the alignment"

That quote is real, it is contiguous, and it genuinely entails "10". ``span_verified`` and
``entailment_ok`` both hold. Span verification is working exactly as designed and it does not help,
because it asks *"is this claim in the document?"* and the question here is *"is this a field you may
set at all?"*. Prose would have become aligner argv, which is precisely what we forbid.

**Asking and enforcing are different jobs.** The prompt asks for these fields; this module refuses
everything else. Conflating the two is how a prompt quietly becomes a security boundary — and a
prompt is the one component here we cannot make deterministic.

**Two axes, not one: role and scope.**

``role`` is what a document IS to us — a reference we cite, or an instruction written for us. It
decides whether ``processing.*`` is askable at all.

``scope`` is what a document is ABOUT. Each level of an archive record is rendered as its own
document, and the level decides what that document may be asked: an experiment's protocol paragraph
may name a chemistry, and a project, sample or run record is asked nothing. A ``dataset``-scoped
document (a paper, a README) is about the whole pile of files.

Both are set from **how the document arrived**, never from its contents. A filename trigger would be
magic, unauditable, and spoofable by renaming a download.
"""

from __future__ import annotations

from typing import Literal

DocRole = Literal["reference", "instruction"]
"""What a document IS to us — decided by the flag it arrived under, never by its filename.

``alignment_instruction.md`` is merely the *conventional* name you pass to ``--instruction``; it is
load-bearing nowhere.
"""

DocScope = Literal["dataset", "project", "sample", "experiment", "run"]
"""What a document is ABOUT — decided by which record produced it, never by the model.

``dataset`` is the default and the only scope a human-supplied document ever has: you hand us a paper
about the whole thing. The other four are archive record levels, and a document at one of them was
rendered by code from a record code fetched.
"""

#: Manifest paths worth asking of a document about the whole dataset. ``library.*`` is byte-decidable
#: and only ever a HYPOTHESIS here (the byte resolver owns the decision); ``experiment.*`` is the part
#: bytes genuinely cannot see.
DEFAULT_FIELDS: tuple[str, ...] = (
    "library.chemistry",
    # A biology fact, NOT byte-decidable and NOT a hypothesis: whether the input was whole cells or
    # isolated nuclei. No byte in a FASTQ says which, and the model names only the biology ("single
    # nuclei") — never a feature. Code maps it to the primary soloFeature (GeneFull for nuclei), which
    # is why it is safe to source from prose: the blast radius is which matrix is primary, and all five
    # are computed regardless. See `manifest.policy.resolve_features`.
    "library.prep_type",
    "experiment.organism",
)

#: Manifest paths asked ONLY of a document handed to us under ``--instruction``.
#:
#: A downloaded methods PDF may never set these: a GEO description is an untrusted input, and prose
#: reaching ``--soloStrand`` would be a prompt-injection path from a database field into an aligner.
#: With the default counting everything, excluding reference docs costs nothing — a paper saying
#: "we used GeneFull" describes a subset of what we already compute.
#:
#: `processing.genome.annotation_name` is deliberately absent: it is a liulab-genome registry name
#: (`WS298`), a vocabulary no paper writes in, so asking for it would only invite a guess. It stays a
#: CLI flag. Each field added here costs prompt tokens on every extraction and needs eval coverage —
#: keep the surface to what earns it.
INSTRUCTION_FIELDS: tuple[str, ...] = (
    "processing.quantification",
    "processing.genome.assembly",
)

#: What each archive record level may say, and it is a strict narrowing rather than a convenience.
#: Only ``experiment`` is asked anything: the chemistry from the protocol paragraph ("Single Cell 3'
#: v3.1 Reagent Kits ... 28+94 nt pair-end reads"), which enters ``resolve`` as a hypothesis and never
#: as evidence. A ``project``, ``sample`` or ``run`` record has no opinion about the chemistry, so it
#: is asked nothing and :mod:`seqforge.harvest.plan` never renders it for a call. What a sample was
#: (its tissue, strain or treatment) is a study reading's to say, not the compiler's.
_SCOPE_FIELDS: dict[DocScope, tuple[str, ...]] = {
    "dataset": DEFAULT_FIELDS,
    "experiment": ("library.chemistry",),
}

#: Every field any draft may carry, from any document. A draft naming anything else is rejected by
#: ``verify_drafts`` with ``field_not_permitted`` — fail-closed, code-owned, no passthrough.
#:
#: Keep this derived and explicit. The temptation is a prefix rule ("anything under ``experiment.``"),
#: which re-opens exactly the hole above: ``experiment.organism`` is a field, and
#: ``experiment.anything.you.can.name`` must not be.
PERMITTED_FIELDS: frozenset[str] = frozenset(DEFAULT_FIELDS) | frozenset(INSTRUCTION_FIELDS)


def fields_for(scope: DocScope, role: DocRole) -> tuple[str, ...]:
    """Which fields to ASK of a document with this scope and role. Enforcement is :func:`permitted_for`."""
    base = _SCOPE_FIELDS.get(scope, ())
    if role == "instruction":
        return (*base, *INSTRUCTION_FIELDS)
    return base


def permitted_for(field: str, scope: DocScope, role: DocRole) -> bool:
    """May a draft from a document with this scope and role set this field? Fail-closed."""
    return field in frozenset(fields_for(scope, role))


def describe_asked(fields: tuple[str, ...]) -> str:
    """The ask as the prompt states it: one field path per line."""
    return "\n".join(f"- {f}" for f in fields)


__all__ = [
    "DEFAULT_FIELDS",
    "INSTRUCTION_FIELDS",
    "PERMITTED_FIELDS",
    "DocRole",
    "DocScope",
    "describe_asked",
    "fields_for",
    "permitted_for",
]
