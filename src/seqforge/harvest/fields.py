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

``scope`` is what a document is ABOUT, and it is the newer of the two. Each level of an archive
record is rendered as its own document, so a sample-level document holds one sample's fields and
nothing else. That is what lets a claim name a sample without ``AssertionDraft`` growing a
``subject`` field: the subject is the document, and code chose the document. A ``dataset``-scoped
document (a paper, a README) is about the whole pile of files, so it may make dataset-wide claims,
and ``resolve`` records its sample claims as *inferred* rather than asserted — see
:mod:`seqforge.resolve.records`.

Both are set from **how the document arrived**, never from its contents. A filename trigger would be
magic, unauditable, and spoofable by renaming a download.
"""

from __future__ import annotations

from typing import Literal

from ..io.attributes import get_attribute, is_attribute

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

#: The sample attributes worth spending prompt tokens on. Hand-picked from NCBI's 960 — **asking**
#: for 960 fields would be absurd, and the ones here are the ones that describe a sample well enough
#: to analyse it: what it was, what was done to it, and who it was.
#:
#: Every name is NCBI's, and `test_every_asked_attribute_is_one_ncbi_defines` checks that against the
#: shipped vocabulary rather than against a second list here. `condition` used to be in this position
#: and was **ours** — no archive defines it, and a field named "condition" accepts anything you can
#: call a condition, which is how the pilot's extraction filed routine worm husbandry ("maintained on
#: NGM plates seeded with E. coli OP50 at 20 C") into it. NCBI's `treatment`, `genotype` and
#: `disease` are what it should have been all along, and each arrives with a definition somebody else
#: maintains.
ASKED_SAMPLE_ATTRIBUTES: tuple[str, ...] = (
    "tissue",
    "cell_type",
    "strain",
    "genotype",
    "treatment",
    "disease",
    "sex",
    "dev_stage",
    "age",
)

#: Manifest paths worth asking of a document about the whole dataset. ``library.*`` is byte-decidable
#: and only ever a HYPOTHESIS here (resolve owns the decision, §3.4); ``experiment.*`` is the part
#: bytes genuinely cannot see.
DEFAULT_FIELDS: tuple[str, ...] = (
    "library.chemistry",
    "experiment.organism",
    "experiment.accessions",
    *(f"experiment.samples.{a}" for a in ASKED_SAMPLE_ATTRIBUTES),
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
#:
#: - ``sample``: this record's own attributes, and nothing else. A BioSample record has no opinion
#:   about the chemistry, so asking it for one would only invite a guess from an alias.
#: - ``experiment``: the chemistry from the protocol paragraph ("Single Cell 3' v3.1 Reagent Kits ...
#:   28+94 nt pair-end reads"), which enters ``resolve`` as a hypothesis and never as evidence — plus
#:   ``treatment``, and *only* ``treatment``. An experiment's title is the GEO GSM title ("Day1
#:   Wild-type(N2) feed with E. coli OP50"), and an experiment belongs to exactly one sample, so a
#:   treatment claim from it is a declaration ABOUT that sample — ``asserted`` via the same
#:   ``subject_to_sample`` join that maps a run alias home, one level up. Treatment alone because the
#:   diet is the one variable that lives ONLY in that title: a BioSample owns ``strain``, ``age`` and
#:   ``tissue`` as typed fields, so asking the title for those too would only let a formatting
#:   difference ("Day6" vs the record's "day6") null a value the record had already resolved. GSE229022
#:   is 28 samples whose OP50/HT115/BW25113/delta-lon contrast is written nowhere else.
#: - ``project``: nothing, deliberately. The study abstract is normalized into a document so a fact
#:   *could* cite it, and no model reads it today: "wild-type and daf-2 mutants" is true of the study
#:   and false of any single sample, and project-level facts (title, centre, data type) are
#:   structured fields we transcribe rather than prose we interpret.
#: - ``run``: the same sample attributes as a sample document. A run alias ("N2_wild_type",
#:   "daf-2_R3", "Rep3 daf2 reads") is often the ONLY place the WT-vs-mutant contrast is written in
#:   plain words, and it is a per-run declaration of its sample's condition. A run belongs to exactly
#:   one sample (run -> experiment -> sample, joined by code), so a claim from its document is a
#:   declaration ABOUT that sample and ``_basis_for`` maps it home as ``asserted``. This was ``()`` on
#:   the theory that the sample's own alias said the same thing; the pilot falsified it — harvest read
#:   the "WT" alias from one of six sample documents and the paper's "daf-2" then fanned onto the two
#:   wild-type samples it missed. The run aliases said "N2_wild_type" plainly, and went unread.
_SCOPE_FIELDS: dict[DocScope, tuple[str, ...]] = {
    "dataset": DEFAULT_FIELDS,
    "sample": tuple(f"experiment.samples.{a}" for a in ASKED_SAMPLE_ATTRIBUTES),
    "experiment": ("library.chemistry", "experiment.samples.treatment"),
    "project": (),
    "run": tuple(f"experiment.samples.{a}" for a in ASKED_SAMPLE_ATTRIBUTES),
}

#: Every field any draft may carry, from any document. A draft naming anything else is rejected by
#: ``verify_drafts`` with ``field_not_permitted`` — fail-closed, code-owned, no passthrough.
#:
#: Keep this derived and explicit. The temptation is a prefix rule ("anything under ``experiment.``"),
#: which re-opens exactly the hole above: ``experiment.samples.tissue`` is a field, and
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


def fields_for_role(role: DocRole) -> tuple[str, ...]:
    """Back-compat shim: the dataset-scoped ask. A document with no record behind it is dataset-wide."""
    return fields_for("dataset", role)


def is_permitted(field: str) -> bool:
    """Is ``field`` a manifest path the LLM is allowed to assert at all, from any document?"""
    return field in PERMITTED_FIELDS


def describe_asked(fields: tuple[str, ...]) -> str:
    """The ask, with NCBI's definition beside each sample attribute — in NCBI's words, not ours.

    A definition we paraphrase is a definition that drifts from the vocabulary it claims to quote, and
    a prompt is the worst possible place to keep one: nothing checks it, and it is exactly where the
    pilot's misfiling happened. So the text handed to the model comes out of the shipped file NCBI's
    own list was generated into.
    """
    lines: list[str] = []
    for f in fields:
        name = f.removeprefix("experiment.samples.")
        if f.startswith("experiment.samples.") and is_attribute(name):
            attr = get_attribute(name)
            gloss = attr.description or attr.display or name
            lines.append(f"- {f}: {gloss} (NCBI BioSample attribute {name!r})")
        else:
            lines.append(f"- {f}")
    return "\n".join(lines)


__all__ = [
    "DEFAULT_FIELDS",
    "INSTRUCTION_FIELDS",
    "PERMITTED_FIELDS",
    "ASKED_SAMPLE_ATTRIBUTES",
    "DocRole",
    "DocScope",
    "fields_for",
    "fields_for_role",
    "permitted_for",
    "is_permitted",
    "describe_asked",
]
