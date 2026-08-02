"""Tests for ``resolve/records.py`` — which sample each file is, and what it was.

The metadata resolver and the provenance gate are one subject: the gate is what decides whether the
prose it reads describes THIS dataset at all, so it belongs beside the resolver it guards.
"""

from __future__ import annotations

from dataclasses import dataclass
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
from seqforge.models.resolve import MetadataResolution, ResolvedSample
from seqforge.resolve.records import SAMPLE_FIELD_PREFIX, DocumentSubject, resolve_metadata

# ================================================================================================
# records — the metadata resolver against the archive's real bytes
# ================================================================================================
#
# The metadata resolver, against the real archive's real bytes.
#
# `fixtures/archive/*.xml` are **not hand-written**. They are exactly what NCBI returned for the pilot
# dataset on 2026-07-16, committed unedited, so the parsers are tested against the format that exists
# rather than the format we remember. That distinction is the whole reason they are here: the SPLiT-seq
# lesson in this repo is that a test which builds its own input from the same assumptions as the code
# proves the two agree and nothing else. There is deliberately NO automated freshness guard: nothing
# re-fetches these XMLs and diffs them against what NCBI serves today — that needs the network, and the
# repo carries only the `external` and `repo` markers, no `network` one. So the committed fixtures can
# drift from the live archive silently; that is a known gap, not an oversight. The taxonomy seed table
# (`io/taxonomy.py`) shares it — pinned against committed literals, never re-resolved live.
#
# The pilot's own `expected.yaml` carries the same claims for the eval harness, but that case is
# `kind: local` and skips wherever the 220 GB of FASTQ is not mounted — which is everywhere except one
# laptop. So the ground truth also lives here, where CI can see it, with no FASTQ involved at all:
# sample facts come from records, and records are 60 kB of XML.


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

    Pre-registered in `evals/cases/real/PRJNA1027859/expected.yaml` ("tissue=Neurons") before any run.
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


# ------------------------------------------------- what a record declares that has no key at all
#
# A submitter may type a fact into a structured characteristic under a name nobody controls, and the
# key space is closed, so that fact can never become an `experiment.samples.*` value. Keeping it out
# is the decision. Being QUIET about it was not: the same sentence in a free-text protocol field
# reaches the prose path, so the submitter who used the structured slot ended up less legible to the
# compiler than the one who buried it in a paragraph. These two tests are the pair — the note fires
# on the invented tag, and stays silent on the bookkeeping every archive stamps on every sample,
# which is what keeps it worth reading (#165).

BENCHMARK = Path(__file__).resolve().parents[1] / "evals" / "benchmark"

#: The BD Rhapsody case whose GEO record declares its capture bead in a characteristic. Its
#: `records.json` is committed, so this needs no network and no FASTQ.
BD_CASE = BENCHMARK / "GSE282765-colon-crod-wta"
BD_BEAD_TAG = "bd rhapsody_capture_bead_version"


def _bd_case_files(records: ArchiveRecordSet) -> list[FileIdentity]:
    """The two filenames the run record itself declares. The join is by name, so no bytes are needed."""
    run = records.at("run")[0]
    return [_file(name, str(i).ljust(64, "d")) for i, name in enumerate(run.filenames)]


def test_an_unharmonized_characteristic_is_surfaced_rather_than_silently_dropped() -> None:
    """`bd rhapsody_capture_bead_version: enhanced beads` is the whole chemistry of that library.

    It survives transcription — the record keeps it, unharmonized, with the submitter's own tag —
    and then stops here, because NCBI does not define that name and a key we coined would accept
    whatever an extraction wanted to put in it. Both halves are asserted below: the value does NOT
    become a sample attribute, AND the resolver says out loud what it declined to key.

    "It warns" is exactly the claim that rots, which is why the message is read rather than counted:
    a note that no longer names the tag or the value is a note nobody can act on.
    """
    from seqforge.evals.case import load_case

    case = load_case(BD_CASE)
    assert case.records is not None, "the case commits its archive records, so this runs offline"
    out = resolve_metadata(files=_bd_case_files(case.records), records=case.records)

    assert not out.blockers, "an attribute with no key is never a refusal"
    assert len(out.samples) == 1
    sample = out.samples[0]
    assert sample.accession == "SAMN52065473"
    # the vocabulary is NOT widened: the bead has no key, under its own name or any near neighbour
    assert not [k for k in sample.attributes if "bead" in k or k == BD_BEAD_TAG]
    # ...and the harmonized siblings on the same record still land, so nothing was thrown out with it
    assert sample.attributes["tissue"].value == "Colon"
    assert sample.attributes["strain"].value == "C57BL/6J Cre"

    notes = [w for w in out.warnings if w.code == "sample_attribute_unharmonized"]
    bead = [w for w in notes if BD_BEAD_TAG in w.message]
    assert len(bead) == 1, f"the dropped bead is invisible; notes were {[w.message for w in notes]}"
    assert "enhanced beads" in bead[0].message, "a note that omits the value cannot be acted on"
    assert sample.sample_id in bead[0].message, "and it must say WHICH sample declared it"
    assert bead[0].subject.ref == f"{SAMPLE_FIELD_PREFIX}{BD_BEAD_TAG}"

    # The arrangement this case was built with is unchanged: the declared bead is evidence FOR the
    # pre-registration and never something the run grades itself against. A claim keyed on it would
    # be the case marking its own homework.
    assert not [k for k in case.expected.fields if BD_BEAD_TAG in k]


def test_the_bookkeeping_every_archive_stamps_on_a_sample_is_not_surfaced(
    records: ArchiveRecordSet,
) -> None:
    """The note has to be rare to be worth reading, and the pilot is the proof that it is.

    `center_name`, `biosample_package` and `taxonomy_id` are unharmonized on every BioSample NCBI
    serves, and they are facts about the record rather than about the biology — the organism is read
    by name here and becomes `experiment.organism`, not a sample attribute. A note on each of them
    would be three lines per sample on every dataset that has a record at all, which is how a warning
    stops being read.
    """
    out = resolve_metadata(files=_pilot_files(), records=records)
    assert len(out.samples) == 6
    assert [w for w in out.warnings if w.code == "sample_attribute_unharmonized"] == []
    # the taxid is not dropped, it is read by name — which is why it owes no note
    assert out.organism is not None and out.organism.value == 6239


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


# The metadata resolver is a decision table, and these nine cells are it tested one at a time. Every
# row has the same call shape — `resolve_metadata(files=_pilot_files(), records, assertions, subjects)`
# — and differs only in the cell: (doc scope, doc subject, does the record already declare this
# attribute, do the values agree) -> (stored value + basis on the subject's sample, presence/absence on
# other samples, warning code). Three rows (run/experiment/sample) vary an axis `_basis_for` does NOT
# branch on: it reads `doc.scope` only for `== "dataset"` (`resolve/records.py`), so a run, an
# experiment and a sample document all become `asserted` the same way — via which record level
# `_subject_to_sample` mapped. The `*_asserts*` ids are that trio.


@dataclass(frozen=True)
class _Claim:
    """One assertion + (optionally) the document code placed it as. ``subject`` is the record accession
    the document was rendered from — ``@experiment0`` is resolved to the first experiment's accession at
    run time, since it is not known until the fixture is built. ``placed=False`` means the assertion
    exists but code registered no ``DocumentSubject`` for it (an unplaced document names no sample)."""

    field: str
    value: str
    scope: str
    subject: str | None
    placed: bool = True


@dataclass(frozen=True)
class _Cell:
    """One row of the precedence table: the inputs, and what must land where."""

    id: str
    use_records: bool
    claims: tuple[_Claim, ...]
    attr: str
    #: The value expected on the subject's sample, or — when no claim names a sample — on EVERY sample.
    #: ``None`` means the attribute must be ABSENT there.
    value: str | None
    basis: str | None = None
    casefold: bool = False  # compare `value` case-insensitively (the "Male"/"male" rule)
    check_confidence: bool = False
    confidence: float | None = None
    #: Samples OTHER than the subject: "n/a" (unchecked), "absent" (attr not set), "keep_record" (still
    #: carries the record's Neurons).
    others: str = "n/a"
    #: "skip" | "none" (no warnings at all) | "ambiguous" | "inferred_only" | "none_for_subject".
    warning: str = "skip"
    warn_count: int | None = None
    warn_contains: tuple[str, ...] = ()


_PRECEDENCE_TABLE: tuple[_Cell, ...] = (
    # A dataset document (a paper, a README) claims a study-wide fact with no record to declare it: it
    # fans to every sample as OUR inference, not the paper's claim about each one.
    _Cell(
        id="dataset_document_fans_to_all_as_inferred",
        use_records=False,
        claims=(_Claim("experiment.samples.tissue", "neurons", "dataset", None),),
        attr="tissue",
        value="neurons",
        basis="inferred",
        warning="none",
    ),
    # The pilot's fix: the WT-vs-daf-2 contrast lives in a run alias, and a run belongs to exactly one
    # sample, so the run document's claim is `asserted` of THAT sample and beats the paper's dataset-
    # level `inferred` daf-2. A sample the run does not cover keeps NO per-sample value — the paper's
    # blanket value is an unsafe guess there — and that is a non-blocking `inferred_only` warning (#10).
    _Cell(
        id="run_alias_asserts_over_the_papers_inference",
        use_records=True,
        claims=(
            _Claim("experiment.samples.genotype", "WT", "run", "SRR28716558"),
            _Claim("experiment.samples.genotype", "daf-2(e1370)", "dataset", None),
        ),
        attr="genotype",
        value="WT",
        basis="asserted",
        others="absent",
        warning="inferred_only",
        warn_contains=("genotype",),
    ),
    # GSE229022's fix at the resolve layer: the diet lives only in the GSM title, which the archive
    # renders as the EXPERIMENT title. An experiment belongs to one sample, so its document's claim is
    # `asserted` of that sample via the same `subject_to_sample` join a run alias uses.
    _Cell(
        id="experiment_title_asserts_to_its_sample",
        use_records=True,
        claims=(
            _Claim("experiment.samples.treatment", "E. coli OP50", "experiment", "@experiment0"),
        ),
        attr="treatment",
        value="E. coli OP50",
        basis="asserted",
        others="absent",
    ),
    # A document code did not place has no subject, so it may name no sample: its claim is dropped and
    # the record's value stands untouched on every sample.
    _Cell(
        id="unplaced_document_names_no_sample",
        use_records=True,
        claims=(_Claim("experiment.samples.tissue", "muscle", "dataset", None, placed=False),),
        attr="tissue",
        value="Neurons",
        warning="none",
    ),
    # The error span verification provably cannot catch: "neurons and body wall muscle" entails BOTH
    # tissue=neurons and tissue=muscle, both quotes real. The record separates them — it is `asserted`
    # of this sample, the paper's reading is `inferred` — so the record wins and the paper rides along
    # as a non-blocking warning on each sample it fanned onto.
    _Cell(
        id="dataset_paper_wrong_reading_loses_to_the_record",
        use_records=True,
        claims=(_Claim("experiment.samples.tissue", "muscle", "dataset", None),),
        attr="tissue",
        value="Neurons",
        warning="ambiguous",
        warn_count=6,
        warn_contains=("muscle", "Neurons"),
    ),
    # Two equal authorities (the record and a sample-scoped assertion) disagree: code does not break the
    # tie, so the attribute is left null — a value, per "null beats a wrong guess" — and the
    # disagreement is a warning. Every OTHER sample keeps its record value.
    _Cell(
        id="equal_authorities_disagree_leave_null",
        use_records=True,
        claims=(_Claim("experiment.samples.tissue", "muscle", "sample", "SAMN40935621"),),
        attr="tissue",
        value=None,
        others="keep_record",
        warning="ambiguous",
        warn_contains=("SAMN40935621",),
    ),
    # 'Neurons' and 'neurons' are the same value; a permanent manifest must not null an equal-authority
    # attribute over capitalization alone (PRJNA1195922 lost `sex` exactly this way). Equal authorities
    # that agree case-insensitively RESOLVE, with no disagreement warning for this sample.
    _Cell(
        id="equal_authorities_agree_in_case_resolve",
        use_records=True,
        claims=(_Claim("experiment.samples.tissue", "neurons", "sample", "SAMN40935621"),),
        attr="tissue",
        value="neurons",
        casefold=True,
        warning="none_for_subject",
    ),
    # A sample document writes only its own sample, `asserted`, and a model's read IS a judgement — so
    # it carries a confidence, unlike a record transcription.
    _Cell(
        id="sample_document_asserts_only_its_own_sample",
        use_records=True,
        claims=(_Claim("experiment.samples.genotype", "daf-2(e1370)", "sample", "SAMN40935621"),),
        attr="genotype",
        value="daf-2(e1370)",
        basis="asserted",
        check_confidence=True,
        confidence=0.9,
        others="absent",
    ),
    # `condition` was ours, not one of NCBI's 960 — the slot the model filed worm husbandry into. A
    # field outside the vocabulary never reaches any sample.
    _Cell(
        id="field_outside_ncbi_vocabulary_never_lands",
        use_records=True,
        claims=(_Claim("experiment.samples.condition", "grown at 20C", "dataset", None),),
        attr="condition",
        value=None,
    ),
)


def _resolve_marker(marker: str | None, records: ArchiveRecordSet) -> str | None:
    return records.at("experiment")[0].accession if marker == "@experiment0" else marker


def _subject_sample(cell: _Cell, records: ArchiveRecordSet) -> str | None:
    """The sample a placed run/experiment/sample document is about, or ``None`` (dataset/unplaced)."""
    for claim in cell.claims:
        if claim.placed and claim.scope in ("sample", "experiment", "run"):
            acc = _resolve_marker(claim.subject, records)
            if claim.scope == "sample":
                return acc
            rec = records.by_accession(acc) if acc is not None else None
            ancestor = records.ancestor(rec, "sample") if rec is not None else None
            return ancestor.accession if ancestor is not None else None
    return None


@pytest.mark.parametrize("cell", _PRECEDENCE_TABLE, ids=[c.id for c in _PRECEDENCE_TABLE])
def test_the_sample_attribute_precedence_table(cell: _Cell, records: ArchiveRecordSet) -> None:
    """Every cell of the metadata resolver's precedence table, one row at a time. See the comment above
    the table for the axis; each `_Cell` is one combination of (scope, subject, record-declares?,
    agree?) and its expected (value+basis, others, warning). The line this guards lives in
    `resolve/records.py`."""
    recs = records if cell.use_records else None
    assertions: list[Assertion] = []
    subjects: list[DocumentSubject] = []
    for i, claim in enumerate(cell.claims):
        doc = str(i) * 64
        assertions.append(_assertion(claim.field, claim.value, doc))
        if claim.placed:
            subjects.append(
                DocumentSubject(
                    doc_sha256=doc,
                    scope=claim.scope,
                    subject=_resolve_marker(claim.subject, records),
                )
            )

    out = resolve_metadata(
        files=_pilot_files(), records=recs, assertions=assertions, subjects=subjects
    )
    assert not out.blockers

    subject_acc = _subject_sample(cell, records)
    by_acc = {s.accession: s for s in out.samples}

    def _expect(sample: ResolvedSample) -> None:
        if cell.value is None:
            assert cell.attr not in sample.attributes
            return
        ev = sample.attributes[cell.attr]
        if cell.casefold:
            assert ev.value.casefold() == cell.value.casefold()
        else:
            assert ev.value == cell.value
        if cell.basis is not None:
            assert ev.basis == cell.basis
        if cell.check_confidence:
            assert ev.confidence == cell.confidence

    if subject_acc is None:
        # no claim named a sample: the expectation holds for EVERY sample (fan-out, or absence)
        for sample in out.samples:
            _expect(sample)
    else:
        _expect(by_acc[subject_acc])
        for acc, sample in by_acc.items():
            if acc == subject_acc:
                continue
            if cell.others == "absent":
                assert cell.attr not in sample.attributes
            elif cell.others == "keep_record":
                assert sample.attributes[cell.attr].value == "Neurons"

    _expect_warnings(out, cell, subject_acc)


def _expect_warnings(out: MetadataResolution, cell: _Cell, subject_acc: str | None) -> None:
    warnings = out.warnings
    ref = f"{SAMPLE_FIELD_PREFIX}{cell.attr}"
    if cell.warning == "none":
        assert not warnings
    elif cell.warning == "ambiguous" and cell.warn_count is not None:
        assert len(warnings) == cell.warn_count
        first = warnings[0]
        assert first.subject.ref == ref
        assert all(tok in first.message for tok in cell.warn_contains)
    elif cell.warning == "ambiguous":
        assert any(
            w.subject.ref == ref and all(tok in w.message for tok in cell.warn_contains)
            for w in warnings
        )
    elif cell.warning == "inferred_only":
        assert any(
            w.code == "sample_attribute_inferred_only"
            and (w.subject.ref or "").endswith(cell.warn_contains[0])
            for w in warnings
        )
    elif cell.warning == "none_for_subject":
        assert subject_acc is not None
        assert not any(subject_acc in w.message and w.subject.ref == ref for w in warnings)


# ---------------------------------------------------------------- A3: a record IS a document


def test_a_record_becomes_its_own_document_scoped_to_itself(records: ArchiveRecordSet) -> None:
    """The whole mechanism: the document holds one sample's prose, so the subject is the document."""
    from seqforge.harvest import normalize_record

    sample = records.by_accession("SAMN40935621")
    assert sample is not None
    doc = normalize_record(sample)
    assert doc.scope == "sample"
    assert doc.subject == "SAMN40935621"
    assert "single nucleus sequencing daf2 replicate 3" in doc.text
    # ...and nothing about any OTHER sample is in it. That is what makes the subject unambiguous.
    assert "replicate 1" not in doc.text
    assert "CQ757" not in doc.text


def test_rendering_a_record_is_deterministic(records: ArchiveRecordSet) -> None:
    """The rendering IS the document, so its sha256 is what a citation cites.

    A human handed the record must be able to regenerate the exact bytes a quote was checked against,
    or the span check is unfalsifiable for every record-derived claim.
    """
    from seqforge.harvest import normalize_record

    sample = records.by_accession("SAMN40935621")
    assert sample is not None
    assert normalize_record(sample).doc_sha256 == normalize_record(sample).doc_sha256


def test_only_free_text_is_rendered_never_the_structured_half(records: ArchiveRecordSet) -> None:
    """`strain = CQ758` is already a key and a value. Showing it to a model is a chance to be wrong.

    Code copies it. The model reads the sentence code cannot parse.
    """
    from seqforge.harvest import render_record

    sample = records.by_accession("SAMN40935621")
    assert sample is not None
    text = render_record(sample)
    assert "CQ758" not in text
    assert "hermaphrodite" not in text


def test_a_collapsed_run_document_is_asserted_of_the_sample_its_runs_belong_to() -> None:
    """The runs of one sample become ONE document, and its claims must still land on that sample.

    That is the half of the collapse that fails silently when it is wrong: `_basis_for` keeps a claim
    only when the document's subject maps to the sample being resolved, and otherwise drops it with
    nothing said — so a collapsed document pointed at the wrong subject is cheap and empty rather
    than cheap and correct. `_subject_to_sample` maps a sample accession to itself, which is why the
    document carries the SAMPLE's accession while its scope stays `run`.

    Which documents exist is `harvest/plan.py`'s decision, so the plan builds them here rather than
    the test hand-rolling a document the shipping path would never produce.
    """
    from seqforge.harvest import plan_extraction
    from seqforge.models.records import ArchiveRecord, FreeText

    def _run(accession: str, experiment: str, alias: str) -> ArchiveRecord:
        return ArchiveRecord(
            level="run",
            accession=accession,
            parent=experiment,
            free_text=[FreeText(label="run_alias", text=alias)],
        )

    records = ArchiveRecordSet(
        source="fixture",
        query="PRJNA9",
        records=[
            ArchiveRecord(level="sample", accession="SAMN1"),
            ArchiveRecord(level="experiment", accession="SRX1", parent="SAMN1"),
            _run("SRR11", "SRX1", "N2_wild_type_r1"),
            _run("SRR12", "SRX1", "N2_wild_type_r2"),
            ArchiveRecord(level="sample", accession="SAMN2"),
            ArchiveRecord(level="experiment", accession="SRX2", parent="SAMN2"),
            _run("SRR21", "SRX2", "daf-2_mutant_r1"),
            _run("SRR22", "SRX2", "daf-2_mutant_r2"),
        ],
    )
    plan = plan_extraction(records=records)
    collapsed = {d.subject: d for d in plan.documents if d.scope == "run"}
    assert set(collapsed) == {"SAMN1", "SAMN2"}, "two samples, two documents, not four"

    files = [
        _file(f"{run}_1.fastq.gz", f"{i}".ljust(64, "a"))
        for i, run in enumerate(("SRR11", "SRR12", "SRR21", "SRR22"))
    ]
    out = resolve_metadata(
        files=files,
        records=records,
        assertions=[
            _assertion("experiment.samples.genotype", "daf-2", collapsed["SAMN2"].doc_sha256)
        ],
        subjects=[
            DocumentSubject(doc_sha256=d.doc_sha256, scope=d.scope, subject=d.subject)
            for d in plan.documents
        ],
    )

    by_sample = {s.accession: s for s in out.samples}
    landed = by_sample["SAMN2"].attributes["genotype"]
    assert landed.value == "daf-2"
    assert landed.basis == "asserted", (
        "a run belongs to one sample, so its alias declares that sample"
    )
    assert "genotype" not in by_sample["SAMN1"].attributes, "and it declares no OTHER sample"


# What a record document may be ASKED -- the scope/role vocabulary, `fields_for`, `permitted_for`,
# `ASKED_SAMPLE_ATTRIBUTES`, `describe_asked` -- is `harvest/fields.py`'s table, and its tests live in
# `tests/test_fields.py`. The claim they used to carry here, "a record document may never set
# processing", is a property of that table and goes red there.


# ---------------------------------------------------------------- A5: the line, as an import graph


def test_harvest_cannot_see_the_probe() -> None:
    """The model reads prose. It is never shown what the bytes said, and this is why.

    A probe-sighted extractor would settle ties the probe itself created and log the wrong reason:
    nothing records corroboration, so the manifest would say `asserted` for a fact a byte decided,
    and the rung provenance would be a lie. The cheaper argument is that there is no byte in a FASTQ
    that bears on `tissue`, `strain`, `sex` or `dev_stage`, so the probe has nothing to contribute to
    any field harvest fills.

    **This test is partial and saying so is the point.** It checks the import graph, which is a real
    boundary a refactor cannot cross by accident. It cannot check what a *prompt* contains — nothing
    can, and no test in this repo should imply otherwise. That asymmetry is the reason the design
    refuses the context structurally instead of policing its use: `resolve_metadata` takes
    `FileIdentity`, not `Observation`, so probe signals are not merely unread there, they are absent.
    """
    import ast
    from pathlib import Path

    import seqforge

    root = Path(seqforge.__file__).parent
    offenders: list[str] = []
    for py in sorted((root / "harvest").rglob("*.py")):
        for node in ast.walk(ast.parse(py.read_text())):
            if isinstance(node, ast.ImportFrom) and (node.module or "").split(".")[-1] in {
                "probe",
                "observation",
            }:
                offenders.append(f"{py.name}:{node.lineno} imports {node.module}")
            if isinstance(node, ast.Import):
                offenders += [
                    f"{py.name}:{node.lineno} imports {a.name}"
                    for a in node.names
                    if "probe" in a.name.split(".")
                ]
    assert not offenders, (
        "harvest imports the probe. The one LLM touchpoint must not be able to read what the bytes "
        "said:\n" + "\n".join(offenders)
    )


def test_the_metadata_resolver_is_handed_identity_not_signal() -> None:
    """The structural half of the same line, and the half that actually holds.

    `resolve_metadata(files=...)` takes `FileIdentity` — a basename, a sha256, a size. Taking
    `Observation` would mean *promising* not to read the per-cycle composition sitting in it, and a
    promise in a docstring is not a boundary.
    """
    import inspect

    from seqforge.models.observation import FileIdentity, Observation

    sig = inspect.signature(resolve_metadata)
    annotation = str(sig.parameters["files"].annotation)
    assert "FileIdentity" in annotation
    assert "Observation" not in annotation
    # ...and the two are genuinely different: Observation carries the signals, FileIdentity does not.
    assert "per_cycle_composition" in Observation.model_fields
    assert set(FileIdentity.model_fields) == {"sha256", "size_bytes", "basename", "local_uri"}


# ---------------------------------------------------------------- A1: the pre-registration grades


def test_the_pilots_pre_registered_sample_facts_are_checkable_and_hold() -> None:
    """The pre-registration's central claims, graded — which for a year they could not be.

    "3 WT (strain CQ757) + 3 daf-2 (CQ758); tissue=Neurons; dev_stage=Adult Day 1;
    sex=hermaphrodite" was written from public metadata before any run, and lived in a `description:`
    string where nothing read it. A pre-registration whose claims cannot be checked cannot be wrong,
    and one that cannot be wrong is not one.

    This runs on the committed records with no FASTQ at all: the pilot case itself is `kind: local`
    and skips wherever the data is not mounted, which is everywhere but one laptop. Sample facts come
    from records, and records are 27 kB of JSON.
    """
    from seqforge.evals import discover_cases
    from seqforge.evals.grade import _equal, _extract_experiment_field

    case = next(c for c in discover_cases() if c.id == "PRJNA1027859")
    assert case.records is not None, "the pilot's archive records ship with the case"

    files = [
        FileIdentity(
            basename=f"{acc}_{mate}.fastq.gz", sha256=f"{i}{mate}".ljust(64, "a"), size_bytes=99
        )
        for i, acc in enumerate(sorted(WT_RUNS + DAF2_RUNS))
        for mate in (1, 2)
    ]
    out = resolve_metadata(files=files, records=case.records)

    claims = {k: v for k, v in case.expected.fields.items() if k.startswith("experiment.")}
    assert claims, "the pre-registration's sample facts are in `fields:`, not in prose"
    # At least one claim names a specific sample (`experiment.samples.SAMN...`), not just the `*`
    # multiset: the `*` form asserts what the dataset CONTAINS, the named form asserts the join put a
    # fact on the RIGHT sample, so a shuffled join would fail this rather than grade clean (folded from
    # `test_a_named_sample_pins_the_join_not_just_the_multiset`).
    assert any(k.startswith("experiment.samples.SAMN") for k in claims), "no named-sample claim"
    for path, want in sorted(claims.items()):
        got = _extract_experiment_field(path, out)
        assert _equal(want, got), f"{path}: expected {want!r}, got {got!r}"
