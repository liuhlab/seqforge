"""The metadata resolver, against the real archive's real bytes.

`fixtures/archive/*.xml` are **not hand-written**. They are exactly what NCBI returned for the pilot
dataset on 2026-07-16, committed unedited, so the parsers are tested against the format that exists
rather than the format we remember. That distinction is the whole reason they are here: the SPLiT-seq
lesson in this repo is that a test which builds its own input from the same assumptions as the code
proves the two agree and nothing else. `test_the_fixtures_are_what_the_archive_still_serves` is the
anti-rot half — it re-fetches and diffs, and it is marked `network` so it runs on demand rather than
in CI.

The pilot's own `expected.yaml` carries the same claims for the eval harness, but that case is
`kind: local` and skips wherever the 220 GB of FASTQ is not mounted — which is everywhere except one
laptop. So the ground truth also lives here, where CI can see it, with no FASTQ involved at all:
sample facts come from records, and records are 60 kB of XML.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from seqforge.io.archive import (
    merge_biosample_attributes,
    parse_bioproject_set,
    parse_biosample_set,
    parse_sra_package_set,
)
from seqforge.models.assertion import Assertion, ExtractorProvenance, SourceSpan
from seqforge.models.blocker import BlockerCode
from seqforge.models.observation import FileIdentity
from seqforge.models.records import ArchiveRecordSet
from seqforge.resolve.records import DocumentSubject, resolve_metadata

FIXTURES = Path(__file__).parent / "fixtures" / "archive"

#: The pilot's six runs, and which strain each carries. Transcribed from the BioSample records, which
#: are committed beside this file — not from a run, and not from memory.
WT_RUNS = ("SRR28716556", "SRR28716557", "SRR28716558")
DAF2_RUNS = ("SRR28716553", "SRR28716554", "SRR28716555")


@pytest.fixture(scope="module")
def records() -> ArchiveRecordSet:
    """The pilot's archive records, parsed from what NCBI actually served. No network."""
    recs = parse_sra_package_set((FIXTURES / "PRJNA1027859.sra.xml").read_text())
    recs = merge_biosample_attributes(
        recs, parse_biosample_set((FIXTURES / "PRJNA1027859.biosample.xml").read_text())
    )
    return ArchiveRecordSet(source="fixture", query="PRJNA1027859", records=recs)


def _file(basename: str, sha: str) -> FileIdentity:
    """Identity only. The resolver is handed no probe output at all — see `resolve/records.py`."""
    return FileIdentity(basename=basename, sha256=sha, size_bytes=1024)


def _pilot_files() -> list[FileIdentity]:
    """Two files per run, named the way `fasterq-dump --split-files` writes them."""
    out: list[FileIdentity] = []
    for i, run in enumerate(sorted(WT_RUNS + DAF2_RUNS)):
        for mate in (1, 2):
            out.append(_file(f"{run}_{mate}.fastq.gz", f"{i}{mate}".ljust(64, "a")))
    return out


# ---------------------------------------------------------------- the records themselves


def test_the_archive_record_carries_all_four_levels(records: ArchiveRecordSet) -> None:
    assert len(records.at("project")) == 1
    assert len(records.at("sample")) == 6
    assert len(records.at("experiment")) == 6
    assert len(records.at("run")) == 6


def test_the_hierarchy_joins_run_to_experiment_to_sample_to_project(
    records: ArchiveRecordSet,
) -> None:
    """The join is the archive's own, followed by code. Every run reaches a sample and a project."""
    for run in records.at("run"):
        sample = records.ancestor(run, "sample")
        assert sample is not None, f"{run.accession} reaches no sample"
        assert sample.accession.startswith("SAMN")
        project = records.ancestor(run, "project")
        assert project is not None and project.accession == "PRJNA1027859"


def test_ncbi_harmonizes_the_attributes_and_we_do_not(records: ArchiveRecordSet) -> None:
    """`harmonized_name` comes out of the BioSample record. We never guess at NCBI's vocabulary."""
    sample = records.by_accession("SAMN40935621")
    assert sample is not None
    harmonized = {a.name for a in sample.attributes if a.harmonized}
    assert {"strain", "tissue", "sex", "dev_stage", "collection_date"} <= harmonized


def test_the_bioproject_record_declares_the_data_type() -> None:
    facts = parse_bioproject_set((FIXTURES / "PRJNA1027859.bioproject.xml").read_text())
    assert facts["PRJNA1027859"]
    assert any(
        a.name == "data_type" and "raw sequence reads" in a.value for a in facts["PRJNA1027859"]
    )


# ---------------------------------------------------------------- A1: the pre-registered facts


def test_every_pilot_sample_gets_the_tissue_the_record_declares(records: ArchiveRecordSet) -> None:
    """THE test. Six samples, `tissue: Neurons` on every one — which the pilot's manifest said null.

    Pre-registered in `evals/cases/PRJNA1027859/expected.yaml` ("tissue=Neurons") before any run.
    """
    out = resolve_metadata(files=_pilot_files(), records=records)
    assert not out.blockers
    assert len(out.samples) == 6
    for sample in out.samples:
        assert sample.attributes["tissue"].value == "Neurons"
        assert sample.attributes["tissue"].basis == "asserted"
        assert sample.attributes["tissue"].evidence == [sample.accession]


def test_the_strain_separates_the_pilots_two_conditions(records: ArchiveRecordSet) -> None:
    """3x CQ757 + 3x CQ758 — pre-registered, and the only structured field that tells them apart."""
    out = resolve_metadata(files=_pilot_files(), records=records)
    by_run = {s.sample_id: s for s in out.samples}
    strains = sorted(s.attributes["strain"].value for s in by_run.values())
    assert strains == ["CQ757", "CQ757", "CQ757", "CQ758", "CQ758", "CQ758"]


def test_the_other_pre_registered_sample_facts_land_too(records: ArchiveRecordSet) -> None:
    out = resolve_metadata(files=_pilot_files(), records=records)
    for sample in out.samples:
        assert sample.attributes["sex"].value == "hermaphrodite"
        assert sample.attributes["dev_stage"].value == "Adult Day 1"


def test_a_transcribed_fact_carries_no_confidence(records: ArchiveRecordSet) -> None:
    """Copying `strain = CQ758` out of a record is not a judgement, so there is nothing to report.

    The pilot's manifest wrote `confidence: 0.750672` onto four unrelated fields — one number about
    one decision, wearing four hats. A record transcription is the opposite case: no number at all.
    """
    out = resolve_metadata(files=_pilot_files(), records=records)
    for sample in out.samples:
        for attr in sample.attributes.values():
            assert attr.confidence is None


def test_the_files_reach_their_samples(records: ArchiveRecordSet) -> None:
    out = resolve_metadata(files=_pilot_files(), records=records)
    assert sum(len(s.file_shas) for s in out.samples) == 12
    assert all(len(s.file_shas) == 2 for s in out.samples)


def test_the_organism_comes_from_the_record_and_cites_it(records: ArchiveRecordSet) -> None:
    """`experiment.organism` used to be a CLI flag citing nothing. The record declares it."""
    out = resolve_metadata(files=_pilot_files(), records=records)
    assert out.organism is not None
    assert out.organism.value == 6239
    assert out.organism.basis == "asserted"
    assert len(out.organism.evidence) == 6


def test_the_project_facts_are_structured_only(records: ArchiveRecordSet) -> None:
    """Title, centre, data type — never the abstract. A hashed manifest does not hold a paragraph."""
    out = resolve_metadata(files=_pilot_files(), records=records)
    assert out.project is not None
    assert out.project.accession == "PRJNA1027859"
    assert out.project.title == (
        "A Single-Nucleus Atlas of Adult C. elegans Neurons Reveals GPCR and "
        "Insulin-signaling Profiles"
    )
    assert out.project.center == "Princeton University"
    assert "abstract" not in out.project.model_dump()


# ---------------------------------------------------------------- the join, and its refusal


def test_the_original_filenames_join_when_the_accession_is_gone(records: ArchiveRecordSet) -> None:
    """A downloaded dataset does not always keep the run accession in its filenames.

    These are the submitter's own names, straight out of the record's `<SRAFile supertype="Original">`
    entries — the pilot's files really are called this in SRA.
    """
    names = [
        "2562__daf-2_R3_library-read-1.fastq.gz",
        "2562__daf-2_R3_library-read-4.fastq.gz",
    ]
    obs = [_file(n, str(i).ljust(64, "b")) for i, n in enumerate(names)]
    out = resolve_metadata(files=obs, records=records)
    assert not out.blockers
    assert len(out.samples) == 1
    assert out.samples[0].accession == "SAMN40935621"
    assert out.samples[0].attributes["strain"].value == "CQ758"


def test_a_record_that_does_not_account_for_the_files_refuses(records: ArchiveRecordSet) -> None:
    """Half-joining is the failure. Four placed samples and two silent ones reads as six."""
    obs = [*_pilot_files(), _file("mystery_1.fastq.gz", "c" * 64)]
    out = resolve_metadata(files=obs, records=records)
    assert [b.code for b in out.blockers] == [BlockerCode.RECORD_JOIN_INCOMPLETE]
    assert out.samples == []
    assert "mystery_1.fastq.gz" in out.blockers[0].evidence


# ---------------------------------------------------------------- no record at all


def test_no_record_is_not_a_refusal() -> None:
    """Most sequencing data never had an accession. It still compiles; it just says less."""
    out = resolve_metadata(files=_pilot_files(), records=None)
    assert not out.blockers
    assert len(out.samples) == 6
    assert all(s.attributes == {} for s in out.samples)
    assert all(s.accession is None for s in out.samples)
    assert out.project is None
    assert out.organism is None


def test_without_a_record_the_run_grouping_is_the_sample_identity() -> None:
    out = resolve_metadata(files=_pilot_files(), records=None)
    assert sorted(s.sample_id for s in out.samples) == sorted(WT_RUNS + DAF2_RUNS)


# ---------------------------------------------------------------- prose, subjects, and conflict


def _assertion(field: str, value: str, doc: str, *, conf: float = 0.9) -> Assertion:
    return Assertion(
        id=f"a-{field}-{value}-{doc[:4]}",
        field=field,
        value=value,
        span=SourceSpan(doc_sha256=doc, quote=value),
        span_verified=True,
        entailment_ok=True,
        llm_confidence=conf,
        extractor=ExtractorProvenance(model_id="test", prompt_version="test"),
    )


def test_a_dataset_document_fans_to_every_sample_as_an_inference(records: ArchiveRecordSet) -> None:
    """A paper says it of the study. That it holds of one of six samples is OUR inference, not its.

    Recording that as `inferred` rather than `asserted` is what makes the precedence below principled
    instead of a tiebreak we invented.
    """
    doc = "d" * 64
    out = resolve_metadata(
        files=_pilot_files(),
        records=None,
        assertions=[_assertion("experiment.samples.tissue", "neurons", doc)],
        subjects=[DocumentSubject(doc_sha256=doc, scope="dataset")],
    )
    assert len(out.samples) == 6
    for sample in out.samples:
        assert sample.attributes["tissue"].value == "neurons"
        assert sample.attributes["tissue"].basis == "inferred"


def test_a_document_code_did_not_place_names_no_sample(records: ArchiveRecordSet) -> None:
    """The subject is the document, and code chooses the documents. An unplaced doc writes nothing."""
    out = resolve_metadata(
        files=_pilot_files(),
        records=records,
        assertions=[_assertion("experiment.samples.tissue", "muscle", "e" * 64)],
        subjects=[],  # code never placed this document
    )
    assert not out.conflicts
    assert all(s.attributes["tissue"].value == "Neurons" for s in out.samples)


def test_a_papers_wrong_reading_becomes_a_conflict_the_record_wins(
    records: ArchiveRecordSet,
) -> None:
    """The error R5 provably cannot catch, caught by having two independent sources.

    "we dissected neurons and body wall muscle" entails tissue=neurons AND tissue=muscle: both quotes
    are real, both contiguous, both pass span verification and entailment. What separates them is the
    record.
    """
    doc = "f" * 64
    out = resolve_metadata(
        files=_pilot_files(),
        records=records,
        assertions=[_assertion("experiment.samples.tissue", "muscle", doc)],
        subjects=[DocumentSubject(doc_sha256=doc, scope="dataset")],
    )
    assert len(out.conflicts) == 6
    conflict = out.conflicts[0]
    assert conflict.field == "experiment.samples.tissue"
    assert conflict.status == "open"
    assert {p.value for p in conflict.positions} == {"Neurons", "muscle"}
    # the record is a declaration about THIS sample; the paper's claim about it is our inference
    for sample in out.samples:
        assert sample.attributes["tissue"].value == "Neurons"


def test_two_equal_authorities_disagreeing_store_nothing(records: ArchiveRecordSet) -> None:
    """A wrong value here is permanent; a missing one is not. Code does not break a tie between equals."""
    doc = "9" * 64
    out = resolve_metadata(
        files=_pilot_files(),
        records=records,
        assertions=[_assertion("experiment.samples.tissue", "muscle", doc)],
        subjects=[DocumentSubject(doc_sha256=doc, scope="sample", subject="SAMN40935621")],
    )
    target = next(s for s in out.samples if s.accession == "SAMN40935621")
    assert "tissue" not in target.attributes
    assert any(c.id.endswith("tissue") and "SAMN40935621" in c.id for c in out.conflicts)
    # every other sample is untouched: the document named one sample and only one
    for sample in out.samples:
        if sample.accession != "SAMN40935621":
            assert sample.attributes["tissue"].value == "Neurons"


def test_a_sample_document_writes_only_its_own_sample(records: ArchiveRecordSet) -> None:
    doc = "8" * 64
    out = resolve_metadata(
        files=_pilot_files(),
        records=records,
        assertions=[_assertion("experiment.samples.genotype", "daf-2(e1370)", doc)],
        subjects=[DocumentSubject(doc_sha256=doc, scope="sample", subject="SAMN40935621")],
    )
    written = [s for s in out.samples if "genotype" in s.attributes]
    assert [s.accession for s in written] == ["SAMN40935621"]
    assert written[0].attributes["genotype"].value == "daf-2(e1370)"
    assert written[0].attributes["genotype"].basis == "asserted"
    assert written[0].attributes["genotype"].confidence == 0.9  # a model's read IS a judgement


def test_a_field_outside_ncbis_vocabulary_never_reaches_a_sample(records: ArchiveRecordSet) -> None:
    """`condition` was ours, and it is the slot the model filed worm husbandry into. It is gone."""
    doc = "7" * 64
    out = resolve_metadata(
        files=_pilot_files(),
        records=records,
        assertions=[_assertion("experiment.samples.condition", "grown at 20C", doc)],
        subjects=[DocumentSubject(doc_sha256=doc, scope="dataset")],
    )
    assert all("condition" not in s.attributes for s in out.samples)
