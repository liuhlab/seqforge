"""The wrong-PDF guard (#51): does a staged document describe a DIFFERENT study than the bytes?

The gate is a pure function over span-verified assertions + the deterministic records + the observed
winner, so it is tested here directly, without a workspace. A dataset-level mismatch surfaces an open
Conflict (a human decides); the strongest case — nothing matches AND the assay contradicts the reads
— refuses with a Blocker.
"""

from __future__ import annotations

from pathlib import Path

from seqforge.cli.manifest import _dataset_document_texts
from seqforge.kb.loader import load_all_specs
from seqforge.models.assertion import Assertion, ExtractorProvenance, SourceSpan
from seqforge.models.records import ArchiveRecord, ArchiveRecordSet, FreeText, RecordAttribute
from seqforge.resolve.provenance import check_provenance
from seqforge.resolve.records import DocumentSubject

_SPECS = load_all_specs()
_EXTRACTOR = ExtractorProvenance(model_id="test", prompt_version="1")
_PAPER = "d" * 64  # the staged dataset-scoped document's sha256


def _assert(field: str, value: str, *, doc: str = _PAPER) -> Assertion:
    return Assertion(
        id=f"a-{field}-{value}",
        field=field,
        value=value,
        span=SourceSpan(doc_sha256=doc, quote=value),
        span_verified=True,
        entailment_ok=True,
        llm_confidence=0.9,
        extractor=_EXTRACTOR,
    )


def _records(
    *, taxid: str = "6239", strain: str = "N2", query: str = "PRJNA111111"
) -> ArchiveRecordSet:
    sample = ArchiveRecord(
        level="sample",
        accession="SRS1",
        parent=query,
        attributes=[
            RecordAttribute(name="taxonomy_id", value=taxid),
            RecordAttribute(name="strain", value=strain, harmonized=True),
        ],
        free_text=[FreeText(label="sample_alias", text="the-worm-atlas")],
    )
    project = ArchiveRecord(level="project", accession=query)
    return ArchiveRecordSet(source="test", query=query, records=[project, sample])


_DATASET_SUBJECT = [DocumentSubject(doc_sha256=_PAPER, scope="dataset", subject=None)]


def test_a_matching_paper_raises_nothing() -> None:
    # Right organism, an overlapping strain, and the paper names our own project accession.
    conflicts, blockers = check_provenance(
        assertions=[
            _assert("experiment.organism", "Caenorhabditis elegans"),
            _assert("experiment.samples.strain", "N2"),
        ],
        subjects=_DATASET_SUBJECT,
        records=_records(),
        winning_techs={"10x-3p-gex-v3"},
        specs=_SPECS,
        document_texts={_PAPER: "We deposited the data under PRJNA111111. Reads are 10x 3' v3."},
    )
    assert conflicts == []
    assert blockers == []


def test_the_wrong_pdf_same_organism_same_assay_surfaces_an_open_conflict() -> None:
    # D2: a daf-2 paper staged over a whole-body (N2) worm dataset. Same organism, same 10x assay, but
    # the paper names a foreign study accession and a non-overlapping genotype -> a human should look.
    conflicts, blockers = check_provenance(
        assertions=[
            _assert("experiment.organism", "Caenorhabditis elegans"),
            _assert("experiment.samples.genotype", "daf-2(e1370)"),
            _assert("library.chemistry", "10x-3p-gex-v3"),  # agrees with the bytes
        ],
        subjects=_DATASET_SUBJECT,
        records=_records(strain="N2"),
        winning_techs={"10x-3p-gex-v3"},
        specs=_SPECS,
        document_texts={_PAPER: "This snRNA-seq study of daf-2 mutants is archived as GSE999999."},
    )
    assert blockers == []
    assert len(conflicts) == 1
    c = conflicts[0]
    assert c.status == "open"
    assert c.field == "document.provenance"
    assert c.kind == "other"
    assert c.decidable_by == ["user"]


def test_the_strongest_case_no_signal_matches_and_assay_contradicts_blocks() -> None:
    # A wholly different study: wrong organism, foreign accession, non-overlapping strain, AND the
    # paper describes bulk RNA-seq while the reads are 10x single-cell. That is not a nuance; refuse.
    conflicts, blockers = check_provenance(
        assertions=[
            _assert("experiment.organism", "Homo sapiens"),
            _assert("experiment.samples.strain", "HeLa"),
            _assert("library.chemistry", "bulk-rnaseq-pe"),
        ],
        subjects=_DATASET_SUBJECT,
        records=_records(taxid="6239", strain="N2"),
        winning_techs={"10x-3p-gex-v3"},
        specs=_SPECS,
        document_texts={
            _PAPER: "A human bulk RNA-seq study, deposited as GSE999999 / PRJNA999999."
        },
    )
    assert conflicts == []
    assert len(blockers) == 1
    b = blockers[0]
    assert b.code.value == "PROVENANCE_MISMATCH"
    assert b.subject.kind == "dataset"


def test_a_paper_that_cites_related_work_is_not_a_mismatch() -> None:
    # The paper names our accession AND a foreign one it cites; organism + strain match. No mismatch.
    conflicts, blockers = check_provenance(
        assertions=[
            _assert("experiment.organism", "Caenorhabditis elegans"),
            _assert("experiment.samples.strain", "N2"),
        ],
        subjects=_DATASET_SUBJECT,
        records=_records(),
        winning_techs={"10x-3p-gex-v3"},
        specs=_SPECS,
        document_texts={
            _PAPER: "Our data (PRJNA111111) extends the earlier atlas GSE888888 of Smith et al."
        },
    )
    assert conflicts == []
    assert blockers == []


def test_no_records_means_nothing_to_contradict() -> None:
    conflicts, blockers = check_provenance(
        assertions=[_assert("experiment.organism", "Homo sapiens")],
        subjects=_DATASET_SUBJECT,
        records=None,
        winning_techs={"10x-3p-gex-v3"},
        specs=_SPECS,
        document_texts={_PAPER: "A human study, GSE999999."},
    )
    assert conflicts == []
    assert blockers == []


def test_a_sample_scoped_document_is_never_a_wrong_pdf_risk() -> None:
    # A record-rendered (sample-scoped) document is self-consistent with the records by construction,
    # so even a "foreign" accession in its text is not checked.
    conflicts, blockers = check_provenance(
        assertions=[_assert("experiment.samples.strain", "daf-2", doc="e" * 64)],
        subjects=[DocumentSubject(doc_sha256="e" * 64, scope="sample", subject="SRS1")],
        records=_records(),
        winning_techs={"10x-3p-gex-v3"},
        specs=_SPECS,
        document_texts={"e" * 64: "GSE999999"},
    )
    assert conflicts == []
    assert blockers == []


def test_dataset_document_texts_reads_only_dataset_scoped_docs_by_hash_suffix(
    tmp_path: Path,
) -> None:
    from seqforge.workspace import documents_dir

    docdir = documents_dir(tmp_path)
    docdir.mkdir(parents=True)
    (docdir / f"ghaddar-paper-{_PAPER[:12]}.txt").write_text("the paper body")
    other = "f" * 64
    (docdir / f"sample-SRS1-{other[:12]}.txt").write_text("a record-rendered doc")

    subjects = [
        DocumentSubject(doc_sha256=_PAPER, scope="dataset", subject=None),
        DocumentSubject(doc_sha256=other, scope="sample", subject="SRS1"),
    ]
    texts = _dataset_document_texts(tmp_path, subjects)
    assert texts == {_PAPER: "the paper body"}  # the sample-scoped doc is not returned


def test_a_quiet_paper_naming_no_accession_and_no_conflicting_claim_is_fine() -> None:
    # Organism matches, no strain claim, no accession printed -> every signal abstains -> nothing.
    conflicts, blockers = check_provenance(
        assertions=[_assert("experiment.organism", "Caenorhabditis elegans")],
        subjects=_DATASET_SUBJECT,
        records=_records(),
        winning_techs={"10x-3p-gex-v3"},
        specs=_SPECS,
        document_texts={_PAPER: "A study of the worm nervous system. No accessions here."},
    )
    assert conflicts == []
    assert blockers == []
