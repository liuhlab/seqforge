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

#: Every field any draft may carry, from any document. A draft naming anything else is rejected by
#: ``verify_drafts`` with ``field_not_permitted`` — fail-closed, code-owned, no passthrough.
#:
#: Keep this frozen and explicit. The temptation is a prefix rule ("anything under ``experiment.``"),
#: which re-opens exactly the hole above: ``experiment.samples.condition`` is a field, and
#: ``experiment.anything.you.can.name`` must not be.
PERMITTED_FIELDS: frozenset[str] = frozenset(DEFAULT_FIELDS)


def is_permitted(field: str) -> bool:
    """Is ``field`` a manifest path the LLM is allowed to assert at all?"""
    return field in PERMITTED_FIELDS


__all__ = ["DEFAULT_FIELDS", "PERMITTED_FIELDS", "is_permitted"]
