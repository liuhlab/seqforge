"""The metadata resolver: archive records + prose -> per-sample facts. A sibling of ``score``.

``score`` answers "what is this library?" from bytes. This answers "which sample is each file, and
what is that sample?" from records and prose. They are siblings rather than one stage because they
have the same ways of being wrong and therefore need the same discipline: both emit evidenced values,
both surface disagreements they refuse to arbitrate, and both can refuse outright. What they must
never do is talk to each other — see "the line", below.

**The join is code's, at every level.** run -> experiment -> sample -> project comes out of the
record, by accession; record-run -> file-on-disk comes out of the run accession in the filename or
the original filenames the record declares. A language model is never asked which sample a file is,
and never could be: it is not shown the files.

**The subject is the document.** A claim cannot name a sample — ``AssertionDraft`` has ``field``,
``value``, ``span``, and nothing else, and it stays that way. Instead each record level is rendered
as *its own document*, so the sample-level document contains that sample's fields and nothing else,
and "which sample" is answered by which file we handed the model. This is the trick ``instruct.py``
already ships for document role: code knows it because code chose it. The alternative — a ``subject``
field on the draft — would hand the model a new authority, and the two-jobs sentence would need
rewriting.

**Where basis comes from, and why it is not a vote.**

===========================================  ===============  ================================
source                                       basis            because
===========================================  ===============  ================================
a record's structured field (strain=CQ758)   ``asserted``     the submitter declared it, of
                                                              this sample, in a typed slot
a model reading THIS sample's own prose      ``asserted``     the document is about this
                                                              sample and nothing else
a model reading a DATASET-level document     ``inferred``     the paper says it of the study;
(a paper, a README)                                           that it holds of *this* sample
                                                              is our inference, not its claim
===========================================  ===============  ================================

A disagreement across bases keeps the stronger basis's value **and stays open** — the §958 pattern,
where the library takes the observed value and the conflict rides along. A disagreement *within* one
basis stores **no value at all**: two equal authorities contradicting each other is not something
code may break, and a wrong value here is permanent (``experiment`` is inside ``dataset_hash`` and
the manifest is never rewritten).

That asymmetry is the thing that catches the error R5 provably cannot. "We dissected neurons and body
wall muscle" entails ``tissue=neurons`` *and* ``tissue=muscle`` — both quotes are real, both are
contiguous, both pass span verification and entailment. What separates them is that the record says
``Neurons``, so the wrong one lands as an open Conflict a human reads instead of a fact a corpus
inherits.

**No archive is the normal case, not the degraded one.** Most sequencing data has never had an
accession and never will: a freshly sequenced plate on a lab filesystem has no BioProject, no
BioSample, and no submitter alias. With no record, sample identity falls back to the run grouping
(filenames group; they always did) and sample facts come from whatever prose there is, or from
nothing. Nothing here refuses for lack of a record. The refusal is narrower and it is real: a record
that *exists* and does not account for the files on disk is a broken join, and half-joining would
leave some files with no sample while the manifest still read as though it described them all.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from ..io.attributes import is_attribute
from ..models.assertion import Assertion
from ..models.base import Basis
from ..models.blocker import Blocker, BlockerCode, BlockerSubject
from ..models.conflict import Conflict, ConflictPosition
from ..models.evidenced import EvidencedStr, EvidencedTaxid
from ..models.observation import FileIdentity
from ..models.records import ArchiveRecord, ArchiveRecordSet
from ..models.resolve import MetadataResolution, ProjectFacts, ResolvedSample
from .group import run_key

#: Which authority wins when two sources disagree about one sample attribute. Never a vote and never
#: a confidence comparison: an LLM's self-reported confidence is advisory (R2) and would happily
#: outrank a database.
_BASIS_RANK: dict[Basis, int] = {
    "observed": 3,
    "user_confirmed": 2,
    "asserted": 1,
    "inferred": 0,
}

#: A record attribute that is real, useful, and NOT one of NCBI's 960 sample attributes. These are
#: facts about the record rather than the biology, so they are read by name here and never offered as
#: sample fields.
_RECORD_META = frozenset({"center_name", "biosample_package", "data_type", "submission_date"})

#: The prefix an assertion uses to name a sample attribute: ``experiment.samples.tissue``.
SAMPLE_FIELD_PREFIX = "experiment.samples."

#: What a document is ABOUT — set by code from which record produced it, never by the model and never
#: from the filename. ``dataset`` is a document handed to us for the whole pile of files (a paper, a
#: README); the others name one record.
DocScope = str


@dataclass(frozen=True)
class DocumentSubject:
    """Which record a document was rendered from. Code's answer to "which sample is this about?".

    Mirrors ``instruct.py``'s ``instruction_docs``: a set of ``doc_sha256`` that code assembled
    because code chose the documents. Nothing here is derivable from the document's contents, and
    that is the point — a spoofable subject would be worse than no subject.
    """

    doc_sha256: str
    scope: DocScope
    #: The record's accession, when the scope names one. ``None`` for a dataset-level document.
    subject: str | None = None


@dataclass(frozen=True)
class _Position:
    """One source's answer for one (sample, attribute), before anything is decided."""

    value: str
    basis: Basis
    evidence: list[str]
    confidence: float | None
    rung: int


def resolve_metadata(
    *,
    files: Sequence[FileIdentity],
    records: ArchiveRecordSet | None = None,
    assertions: Sequence[Assertion] = (),
    subjects: Sequence[DocumentSubject] = (),
) -> MetadataResolution:
    """Resolve the files into samples, and the samples into facts.

    Takes ``FileIdentity`` rather than ``Observation`` on purpose. This stage needs a basename and a
    sha256 and nothing else, and being handed the probe's output would mean *promising* not to read
    the signals in it. A signature that cannot see them keeps the promise structurally — see
    :func:`_the_line` for why it is worth keeping.
    """
    by_doc = {d.doc_sha256: d for d in subjects}
    samples, blockers = _join(files, records)

    verified = [a for a in assertions if a.span_verified and a.entailment_ok]
    resolved: list[ResolvedSample] = []
    conflicts: list[Conflict] = []
    for sample in samples:
        positions = _positions_for(sample, verified, by_doc)
        attrs, sample_conflicts = _decide(sample.sample_id, positions)
        resolved.append(
            ResolvedSample(
                sample_id=sample.sample_id,
                accession=sample.accession,
                attributes=attrs,
                file_shas=sample.file_shas,
            )
        )
        conflicts.extend(sample_conflicts)

    return MetadataResolution(
        samples=resolved,
        project=_project_facts(records),
        organism=_organism(records),
        conflicts=conflicts,
        blockers=blockers,
    )


@dataclass(frozen=True)
class _Sample:
    """A joined sample: who it is, which files carry it, and the record behind it (if any)."""

    sample_id: str
    accession: str | None
    file_shas: list[str]
    record: ArchiveRecord | None


def _join(
    files: Sequence[FileIdentity], records: ArchiveRecordSet | None
) -> tuple[list[_Sample], list[Blocker]]:
    """Files -> samples. The record when there is one, the filenames when there is not."""
    if records is None or not records.at("run"):
        return _join_by_filename(files), []

    runs = records.at("run")
    by_accession = {r.accession: r for r in runs}
    by_filename: dict[str, ArchiveRecord] = {}
    for declared in runs:
        for name in declared.filenames:
            by_filename[name] = declared

    grouped: dict[str, list[str]] = {}
    accession_of: dict[str, str | None] = {}
    record_of: dict[str, ArchiveRecord | None] = {}
    unclaimed: list[str] = []

    for f in files:
        basename = f.basename
        run = by_accession.get(run_key(basename)) or by_filename.get(basename)
        if run is None:
            unclaimed.append(basename)
            continue
        sample = records.ancestor(run, "sample")
        # A run whose sample record is missing still has an identity — its own accession. Degraded,
        # and honest about it: the files are grouped correctly, we just cannot say what they are.
        sample_id = sample.accession if sample is not None else run.accession
        grouped.setdefault(sample_id, []).append(f.sha256)
        accession_of[sample_id] = sample.accession if sample is not None else None
        record_of[sample_id] = sample

    if unclaimed:
        return [], [_join_blocker(unclaimed, records)]

    return (
        [
            _Sample(
                sample_id=sid,
                accession=accession_of[sid],
                file_shas=sorted(grouped[sid]),
                record=record_of[sid],
            )
            for sid in sorted(grouped)
        ],
        [],
    )


def _join_by_filename(files: Sequence[FileIdentity]) -> list[_Sample]:
    """No record: the run grouping IS the sample identity.

    This is the path for every dataset that never went near an archive, which is most of them. It is
    exactly what the pipeline already did — filenames group, bytes assign — and it produces samples
    with no facts, because there is nothing declaring any.
    """
    grouped: dict[str, list[str]] = {}
    for f in files:
        grouped.setdefault(run_key(f.basename), []).append(f.sha256)
    return [
        _Sample(sample_id=sid, accession=None, file_shas=sorted(grouped[sid]), record=None)
        for sid in sorted(grouped)
    ]


def _join_blocker(unclaimed: list[str], records: ArchiveRecordSet) -> Blocker:
    declared = sorted({r.accession for r in records.at("run")})
    return Blocker(
        id="blk-record-join-incomplete",
        code=BlockerCode.RECORD_JOIN_INCOMPLETE,
        message=(
            f"{records.query} declares {len(declared)} run(s) ({', '.join(declared[:6])}"
            f"{', ...' if len(declared) > 6 else ''}), and {len(unclaimed)} file(s) on disk match "
            f"none of them by run accession or by the original filenames the record declares: "
            f"{', '.join(sorted(unclaimed)[:6])}{', ...' if len(unclaimed) > 6 else ''}. Refusing to "
            f"half-join: the files it cannot place would silently get no sample facts, and a manifest "
            f"that is confident about some samples and quiet about others reads as one about all."
        ),
        remedy=(
            "Either the files are not from this accession, or they were renamed after download. "
            "Check the accession, or re-fetch with a tool that keeps the run accession in the "
            "filename (`fasterq-dump --split-files` names them <RUN>_1.fastq.gz). To compile with no "
            "sample facts at all, omit the accession — a dataset with no record is not an error."
        ),
        subject=BlockerSubject(kind="dataset", ref="experiment.samples"),
        evidence=sorted(unclaimed),
    )


def _positions_for(
    sample: _Sample,
    assertions: Sequence[Assertion],
    by_doc: dict[str, DocumentSubject],
) -> dict[str, list[_Position]]:
    """Every source's answer for every attribute of one sample. Decides nothing."""
    out: dict[str, list[_Position]] = {}

    if sample.record is not None:
        for attr in sample.record.attributes:
            if not attr.harmonized or attr.name in _RECORD_META or not is_attribute(attr.name):
                continue
            out.setdefault(attr.name, []).append(
                _Position(
                    value=attr.value,
                    basis="asserted",
                    evidence=[sample.record.accession],
                    # A copy is not a judgement: no confidence, because none was formed. See
                    # `Evidenced.confidence`.
                    confidence=None,
                    rung=0,
                )
            )

    for a in assertions:
        if not a.field.startswith(SAMPLE_FIELD_PREFIX):
            continue
        name = a.field[len(SAMPLE_FIELD_PREFIX) :]
        if not is_attribute(name):
            continue  # `fields.py` already refused it; belt and braces
        doc = by_doc.get(a.span.doc_sha256)
        if doc is None:
            continue  # a document code did not place has no subject, so it may not name one
        basis = _basis_for(doc, sample)
        if basis is None:
            continue  # this document is about a different sample
        out.setdefault(name, []).append(
            _Position(
                value=a.value,
                basis=basis,
                evidence=[a.id],
                confidence=a.llm_confidence,
                rung=0,
            )
        )
    return out


def _basis_for(doc: DocumentSubject, sample: _Sample) -> Basis | None:
    """What a claim from this document is, *about this sample*. ``None`` = it is not about it at all.

    A sample-scoped document names its sample, so a claim from it is a declaration about that sample.
    A dataset-scoped document (a paper) makes a claim about the study; that the claim holds of any one
    of six samples is **our** inference, so it is recorded as one. The distinction is not pedantry: it
    is what makes the precedence in :func:`_decide` principled rather than a tiebreak we invented.
    """
    if doc.scope == "dataset":
        return "inferred"
    if doc.scope == "sample" and doc.subject is not None and doc.subject == sample.accession:
        return "asserted"
    return None


def _decide(
    sample_id: str, positions: dict[str, list[_Position]]
) -> tuple[dict[str, EvidencedStr], list[Conflict]]:
    """Turn each attribute's positions into at most one value, plus any conflict. Never a vote."""
    attrs: dict[str, EvidencedStr] = {}
    conflicts: list[Conflict] = []

    for name, found in sorted(positions.items()):
        distinct = {p.value for p in found}
        if len(distinct) == 1:
            best = max(found, key=lambda p: _BASIS_RANK[p.basis])
            attrs[name] = _evidenced(best)
            continue

        conflicts.append(
            Conflict(
                id=f"conflict-sample-{sample_id}-{name}".replace(".", "-"),
                field=f"{SAMPLE_FIELD_PREFIX}{name}",
                positions=[
                    ConflictPosition(
                        value=p.value,
                        basis=p.basis,
                        evidence=list(p.evidence),
                        confidence=p.confidence if p.confidence is not None else 1.0,
                    )
                    for p in sorted(found, key=lambda p: (-_BASIS_RANK[p.basis], p.value))
                ],
                kind="asserted_vs_asserted",
                # No byte says what tissue a nucleus came from, so reads/onlist/alignment cannot
                # settle this. Only the record, or the person who wrote both sentences.
                decidable_by=["metadata", "user"],
                status="open",
            )
        )

        ranked = sorted(found, key=lambda p: -_BASIS_RANK[p.basis])
        top = _BASIS_RANK[ranked[0].basis]
        winners = {p.value for p in ranked if _BASIS_RANK[p.basis] == top}
        if len(winners) == 1:
            # a stronger authority exists: keep its value, keep the conflict open (§958's pattern)
            attrs[name] = _evidenced(ranked[0])
        # else: two equal authorities disagree. Store nothing. A wrong value here is permanent, a
        # missing one is not, and code does not get to break a tie between equals.

    return attrs, conflicts


def _evidenced(p: _Position) -> EvidencedStr:
    return EvidencedStr(
        value=p.value,
        basis=p.basis,
        evidence=list(p.evidence),
        confidence=p.confidence,
        rung=p.rung,
    )


def _project_facts(records: ArchiveRecordSet | None) -> ProjectFacts | None:
    """The study's declared, structured facts. The abstract is deliberately not among them."""
    if records is None:
        return None
    projects = records.at("project")
    if not projects:
        return None
    p = projects[0]
    accession = p.accession if _looks_like_accession(p.accession) else None
    return ProjectFacts(
        accession=accession,
        title=p.text("study_title"),
        center=_meta(p, "center_name"),
        data_type=_meta(p, "data_type"),
        released=_meta(p, "submission_date"),
    )


def _meta(record: ArchiveRecord, name: str) -> str | None:
    for attr in record.attributes:
        if attr.name == name:
            return attr.value
    return None


def _organism(records: ArchiveRecordSet | None) -> EvidencedTaxid | None:
    """The taxid every sample record agrees on. Disagreement yields ``None`` rather than a majority.

    A dataset whose samples are two organisms is a real thing (a xenograft, a co-culture), and it is
    not something this function may flatten. ``None`` sends the caller to ask.
    """
    if records is None:
        return None
    seen: dict[str, list[str]] = {}
    for sample in records.at("sample"):
        taxid = _meta(sample, "taxonomy_id")
        if taxid:
            seen.setdefault(taxid, []).append(sample.accession)
    if len(seen) != 1:
        return None
    taxid, evidence = next(iter(seen.items()))
    if not taxid.isdigit() or int(taxid) <= 0:
        return None
    return EvidencedTaxid(
        value=int(taxid), basis="asserted", evidence=sorted(evidence), confidence=None, rung=0
    )


def _looks_like_accession(value: str) -> bool:
    import re

    from ..models.base import Accession  # noqa: F401  (documents where the pattern is owned)

    return bool(re.match(r"^([SED]R[RXPS]\d+|GS[EM]\d+|PRJ[A-Z]{2}\d+|SAM[NED][A-Z]?\d+)$", value))


def _the_line() -> str:
    """Why this resolver is not shown the probe. Kept as prose because it is a design commitment.

    The tempting version of this module reads the probe's output too — "the reads are 28+94, so the
    protocol paragraph saying 28+94 is corroborated". Two reasons not to, and the second is the cheap
    one:

    1. A probe-sighted reader would settle ties the probe itself created, and log the wrong reason.
       Nothing records corroboration, so the manifest would say "asserted" for a fact that a byte
       actually decided, and the rung provenance would be a lie.
    2. Read lengths say nothing about neurons. There is no byte in a FASTQ that bears on ``tissue``,
       ``strain``, ``sex`` or ``dev_stage``. The probe has zero bits to contribute to every field this
       module resolves, so the whole question is moot for the fields it actually decides.

    The chemistry hypothesis is the *legitimate* half of the same idea, and it goes the other way:
    prose steers which whitelist ``score`` checks first, and never enters the evidence matrix.
    """
    return __doc__ or ""


__all__ = [
    "DocScope",
    "DocumentSubject",
    "SAMPLE_FIELD_PREFIX",
    "resolve_metadata",
]
