"""A reading joined to the samples a compile made, as labels on those samples and nothing more.

The manifest owns which samples there are, their organism and their assay; the reading owns what
each sample was. :func:`join_reading` meets the two on the BioSample accession: a manifest sample's
``accession``, and the ``biosample_accession`` a reading row copied from its GEO record. It hands
back one column per factor and per label that varies across the joined samples, each sample's
value in it, and the reading's hash. Labels the same for every sample tell no sample apart, so
they get no column; a label some samples lack varies (:func:`~.labels.varies`).

Where the two overlap, one checks the other, and a disagreement is a warning and never a
``Conflict``: neither side is an observation of the bytes, and a reading never blocks a compile.
The warnings are a manifest sample with no reading row, a manifest sample whose BioSample several
reading rows link, which takes none of them, a reading row with no compiled sample, a species that is not the manifest's organism, and a manifest assay term that is not the reading's
assay term or under it in the ontology.
"""

from __future__ import annotations

from collections.abc import Collection, Iterator, Sequence
from dataclasses import dataclass, field

from ..models.blocker import BlockerSubject, ValidationWarning
from .labels import LABELS, label_row, varies
from .reading import Reading, SampleClaim

#: The column that names the reading a row's labels came from.
READING_HASH = "reading_hash"
#: What a label's column is called when a column of the compile's own already has its name.
CLAIMED_PREFIX = "reading_"
#: The prefix of a species term's NCBI Taxonomy id.
TAXON = "NCBITaxon:"


@dataclass(frozen=True)
class CompiledSample:
    """One sample a compile made, as far as the join reads it."""

    sample_id: str
    #: The BioSample accession the manifest records, or ``None`` for a sample with no archive id.
    accession: str | None
    #: The manifest's organism, an NCBI Taxonomy id.
    taxid: str
    #: The ontology ids the manifest's assay labels name.
    assay_terms: tuple[str, ...]


@dataclass(frozen=True)
class Joined:
    """What a reading adds to a compile's sample table, and what the join found wrong."""

    reading_hash: str
    #: The label columns, then :data:`READING_HASH`.
    columns: list[str]
    #: Each joined sample's cells by column, by sample id. A sample with no reading row has none.
    cells: dict[str, dict[str, str]]
    warnings: list[ValidationWarning] = field(default_factory=list)
    #: Each check the join could not make, and why.
    unchecked: list[str] = field(default_factory=list)


def term_under(term: str, ancestor: str) -> bool | None:
    """Whether ontology id ``term`` is ``ancestor`` or under it, or ``None`` when it cannot be said.

    The answer belongs to liulab-data's ontology store. The store keeps each term's id, name and
    synonyms and no is-a relation between terms, so only the same id is answered here, and every
    other pair is left unchecked rather than judged.
    """
    return True if term == ancestor else None


def cell(label: str, claim: SampleClaim) -> str:
    """A claim as one cell: its level, its made-up id, its text, its number, or the gap it marks."""
    if claim.unknown is not None:
        return "unknown"
    if claim.na is not None:
        return "na"
    if claim.level is not None:
        return claim.level
    if label_row(label).id_alone and claim.id:
        return claim.id
    if claim.text:
        return claim.text
    if claim.value is not None:
        value = claim.value
        said = f"{value[0]}-{value[1]}" if isinstance(value, list) else str(value)
        return f"{said} {claim.unit}" if claim.unit else said
    term = claim.term
    return ";".join(term) if isinstance(term, list) else term or ""


def _subject(ref: str) -> BlockerSubject:
    return BlockerSubject(kind="field", ref=ref)


def _label_order(reading: Reading, rows: Sequence[dict[str, SampleClaim]]) -> list[str]:
    """The factors in the reading's order, then the label table's labels, then the study's own."""
    named = [*reading.factors, *(row.name for row in LABELS)]
    own = [label for row in rows for label in row if label not in named]
    return list(dict.fromkeys([*named, *own]))


def _checks(
    gsm: str, row: dict[str, SampleClaim], sample: CompiledSample, unchecked: list[str]
) -> Iterator[ValidationWarning]:
    """Where a reading row and the compiled sample it joins disagree on what both of them name."""
    species = row.get("species")
    if species is not None and isinstance(species.term, str) and species.term.startswith(TAXON):
        taxid = species.term.removeprefix(TAXON)
        if taxid != sample.taxid:
            yield ValidationWarning(
                code="SPECIES_NOT_ORGANISM",
                message=f"the reading's {gsm} is of taxid {taxid}, and the manifest's sample "
                f"{sample.sample_id} of organism {sample.taxid}",
                subject=_subject(f"reading.samples.{gsm}.species"),
            )
    assay = row.get("assay")
    if assay is None or not assay.says or not isinstance(assay.term, str) or not sample.assay_terms:
        return
    answers = [term_under(kit, assay.term) for kit in sample.assay_terms]
    if True in answers:
        return
    kits = ", ".join(sample.assay_terms)
    if None in answers:
        unchecked.append(
            f"assay of {sample.sample_id}: whether {kits} is under {assay.term}, which "
            "liulab-data's ontology store cannot answer"
        )
        return
    yield ValidationWarning(
        code="ASSAY_NOT_UNDER_READING",
        message=f"the manifest's assay {kits} for {sample.sample_id} is not {assay.term}, the "
        f"reading's assay for {gsm}, nor under it",
        subject=_subject(f"reading.samples.{gsm}.assay"),
    )


def join_reading(
    reading: Reading, samples: Sequence[CompiledSample], *, taken: Collection[str] = ()
) -> Joined:
    """The reading's labels on the compiled ``samples``, joined by BioSample accession.

    ``taken`` names the columns the compile's own table already has; a label of the same name
    takes :data:`CLAIMED_PREFIX` before it.
    """
    rows = reading.rows()
    # Several rows can link one BioSample, such as a pull-down and its input from one material.
    # Nothing else the join checks tells them apart, so a sample of that BioSample takes no row.
    by_accession: dict[str, list[str]] = {}
    for gsm, own in reading.samples.items():
        if own.biosample_accession is not None:
            by_accession.setdefault(own.biosample_accession, []).append(gsm)
    warnings: list[ValidationWarning] = []
    unchecked: list[str] = []
    joined: dict[str, dict[str, SampleClaim]] = {}
    compiled: set[str] = set()
    for sample in samples:
        linking = by_accession.get(sample.accession, []) if sample.accession else []
        if len(linking) > 1:
            compiled.update(linking)
            warnings.append(
                ValidationWarning(
                    code="BIOSAMPLE_IN_SEVERAL_ROWS",
                    message=f"the manifest's sample {sample.sample_id} ({sample.accession}) is "
                    f"linked by {len(linking)} rows of the reading of {reading.study}, "
                    f"{', '.join(linking)}, and nothing tells which one it is, so it carries no "
                    "labels",
                    subject=_subject(f"experiment.samples.{sample.sample_id}"),
                )
            )
            continue
        if not linking:
            warnings.append(
                ValidationWarning(
                    code="SAMPLE_NOT_IN_READING",
                    message=f"the manifest's sample {sample.sample_id} "
                    f"({sample.accession or 'no BioSample accession'}) has no row in the "
                    f"reading of {reading.study}, so it carries no labels",
                    subject=_subject(f"experiment.samples.{sample.sample_id}"),
                )
            )
            continue
        (gsm,) = linking
        compiled.add(gsm)
        joined[sample.sample_id] = rows[gsm]
        warnings.extend(_checks(gsm, rows[gsm], sample, unchecked))
    for gsm, own in reading.samples.items():
        if gsm not in compiled:
            linked = own.biosample_accession or "its record links no BioSample"
            warnings.append(
                ValidationWarning(
                    code="READING_SAMPLE_NOT_COMPILED",
                    message=f"the reading's {gsm} ({linked}) is no sample of this compile",
                    subject=_subject(f"reading.samples.{gsm}"),
                )
            )

    values = {
        label: {sid: cell(label, row[label]) for sid, row in joined.items() if label in row}
        for label in _label_order(reading, list(joined.values()))
    }
    kept = [
        label
        for label, by_sample in values.items()
        if label in reading.factors or varies([by_sample.get(sid) for sid in joined])
    ]
    names = {label: f"{CLAIMED_PREFIX}{label}" if label in taken else label for label in kept}
    digest = reading.content_hash()
    cells = {
        sid: {
            **{names[label]: values[label].get(sid, "") for label in kept},
            READING_HASH: digest,
        }
        for sid in joined
    }
    return Joined(
        reading_hash=digest,
        columns=[*names.values(), READING_HASH],
        cells=cells,
        warnings=warnings,
        unchecked=unchecked,
    )
