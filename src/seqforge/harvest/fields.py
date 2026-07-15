"""The closed vocabulary of manifest fields an LLM may assert — enforced by code, not by the prompt.

``AssertionDraft.field`` is a plain ``str``, and it has to stay one: the wire schema must fit inside
every provider's strict-schema subset. That makes the vocabulary a **code** obligation rather than a
type one, and until this module existed there was nothing discharging it. ``DEFAULT_FIELDS`` was only
ever interpolated into the prompt; ``verify`` never compared a returned draft against it. So the
model could name any field it liked and both R5 checks would still pass:

    field: "processing.params.outFilterMismatchNmax"   value: "10"
    quote: "add --outFilterMismatchNmax 10 to the alignment"

That quote is real, it is contiguous, and it genuinely entails "10". ``span_verified`` and
``entailment_ok`` both hold. R5 is working exactly as designed and it does not help, because R5 asks
*"is this claim in the document?"* and the question here is *"is this a field you may set at all?"*.
Prose would have become aligner argv, which is R1's whole prohibition.

Nothing exploited it only because no path existed from an ``Assertion`` into ``manifest fill``. The
processing manifest builds that path, so the allowlist lands first.

**Asking and enforcing are different jobs.** The prompt asks for these fields; this module refuses
everything else. Conflating the two is how a prompt quietly becomes a security boundary — and a
prompt is the one component here we cannot make deterministic.
"""

from __future__ import annotations

from typing import Literal

#: Manifest paths worth asking of every document. ``library.*`` is byte-decidable and only ever a
#: HYPOTHESIS here (resolve owns the decision, §3.4); ``experiment.*`` is the part bytes genuinely
#: cannot see.
DEFAULT_FIELDS: tuple[str, ...] = (
    "library.chemistry",
    "experiment.organism",
    "experiment.accessions",
    "experiment.samples.tissue",
    "experiment.samples.condition",
)

#: Manifest paths asked ONLY of a document handed to us under ``--instruction``.
#:
#: A downloaded methods PDF may never set these: a GEO description is an untrusted input, and prose
#: reaching ``--soloStrand`` would be a prompt-injection path from a database field into an aligner.
#: With the default counting everything (R15), excluding reference docs costs nothing — a paper saying
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

#: Every field any draft may carry, from any document. A draft naming anything else is rejected by
#: ``verify_drafts`` with ``field_not_permitted`` — fail-closed, code-owned, no passthrough.
#:
#: Keep this frozen and explicit. The temptation is a prefix rule ("anything under ``experiment.``"),
#: which re-opens exactly the hole above: ``experiment.samples.condition`` is a field, and
#: ``experiment.anything.you.can.name`` must not be.
PERMITTED_FIELDS: frozenset[str] = frozenset(DEFAULT_FIELDS) | frozenset(INSTRUCTION_FIELDS)

DocRole = Literal["reference", "instruction"]
"""What a document IS to us — decided by the flag it arrived under, never by its filename.

A filename trigger (``alignment_instruction.md``) would be magic, unauditable, and spoofable by
renaming a downloaded PDF. ``alignment_instruction.md`` is merely the *conventional* name you pass to
``--instruction``; it is load-bearing nowhere.
"""


def fields_for_role(role: DocRole) -> tuple[str, ...]:
    """Which fields to ASK of a document in this role. Enforcement is :func:`permitted_for_role`."""
    return (*DEFAULT_FIELDS, *INSTRUCTION_FIELDS) if role == "instruction" else DEFAULT_FIELDS


def permitted_for_role(field: str, role: DocRole) -> bool:
    """May a draft from a document in this role set this field? Fail-closed."""
    return field in (frozenset(fields_for_role(role)))


def is_permitted(field: str) -> bool:
    """Is ``field`` a manifest path the LLM is allowed to assert at all, from any document?"""
    return field in PERMITTED_FIELDS


__all__ = [
    "DEFAULT_FIELDS",
    "INSTRUCTION_FIELDS",
    "PERMITTED_FIELDS",
    "DocRole",
    "fields_for_role",
    "permitted_for_role",
    "is_permitted",
]
