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


def test_a_run_alias_asserts_its_samples_genotype_over_the_papers_inference(
    records: ArchiveRecordSet,
) -> None:
    """The pilot's fix. The WT-vs-daf-2 contrast lives in the run alias ("N2_wild_type"), and a run

    belongs to exactly one sample, so its document's claim is a declaration ABOUT that sample —
    `asserted`, which beats the paper's dataset-level `inferred` daf-2. Before the run->sample join a
    run document mapped to no sample and its claim was silently dropped, leaving the paper's inference
    standing on the wild-type samples (exactly what sf-demo-4 produced: daf-2 on "WT replicate 2/3").
    """
    wt_run = next(
        r for r in records.at("run") if r.accession == "SRR28716558"
    )  # an N2 wild-type run
    ancestor = records.ancestor(wt_run, "sample")
    assert ancestor is not None
    wt_sample = ancestor.accession
    run_doc, paper = "r" * 64, "p" * 64
    out = resolve_metadata(
        files=_pilot_files(),
        records=records,
        assertions=[
            _assertion("experiment.samples.genotype", "WT", run_doc),
            _assertion("experiment.samples.genotype", "daf-2(e1370)", paper),
        ],
        subjects=[
            DocumentSubject(doc_sha256=run_doc, scope="run", subject="SRR28716558"),
            DocumentSubject(doc_sha256=paper, scope="dataset"),
        ],
    )
    by_sample = {s.accession: s for s in out.samples}
    # the WT run's sample takes genotype from its OWN run alias, asserted, not the paper's inference
    assert by_sample[wt_sample].attributes["genotype"].value == "WT"
    assert by_sample[wt_sample].attributes["genotype"].basis == "asserted"
    # a sample with NO per-sample claim is now left NULL, not stamped with the paper's blanket daf-2:
    # genotype is declared per-sample here (the WT run owns "WT"), so it varies by sample and a
    # study-wide value is an unsafe guess for a sample the archive left blank. Null beats a wrong,
    # permanent value — this is the second half of the PRJNA1027859 fix (#10). A warning surfaces it.
    other = next(s for acc, s in by_sample.items() if acc != wt_sample)
    assert "genotype" not in other.attributes
    assert any(
        w.code == "sample_attribute_inferred_only" and (w.subject.ref or "").endswith("genotype")
        for w in out.warnings
    )


def test_an_experiment_title_asserts_its_samples_diet(records: ArchiveRecordSet) -> None:
    """GSE229022's fix, at the resolve layer. The diet ("feed with E. coli OP50") lives only in the

    GEO GSM title, which the archive renders as the EXPERIMENT title. An experiment belongs to exactly
    one sample, so a treatment claim from its document is a declaration ABOUT that sample — `asserted`
    via `subject_to_sample`, the same join a run alias uses. This is why `fields.py` asks the
    experiment scope for `treatment`: without this mapping the claim would land on no sample and be
    dropped, which is how every GSE229022 sample said `treatment: null` under a title that named its
    diet.
    """
    exp = records.at("experiment")[0]
    sample = records.ancestor(exp, "sample")
    assert sample is not None
    exp_doc = "x" * 64
    out = resolve_metadata(
        files=_pilot_files(),
        records=records,
        assertions=[_assertion("experiment.samples.treatment", "E. coli OP50", exp_doc)],
        subjects=[DocumentSubject(doc_sha256=exp_doc, scope="experiment", subject=exp.accession)],
    )
    by_sample = {s.accession: s for s in out.samples}
    # the experiment's own sample takes the diet from its title, asserted — not null, not inferred
    assert by_sample[sample.accession].attributes["treatment"].value == "E. coli OP50"
    assert by_sample[sample.accession].attributes["treatment"].basis == "asserted"
    # a sample the experiment does not belong to gets no treatment from it at all
    other = next(s for acc, s in by_sample.items() if acc != sample.accession)
    assert "treatment" not in other.attributes


def test_a_document_code_did_not_place_names_no_sample(records: ArchiveRecordSet) -> None:
    """The subject is the document, and code chooses the documents. An unplaced doc writes nothing."""
    out = resolve_metadata(
        files=_pilot_files(),
        records=records,
        assertions=[_assertion("experiment.samples.tissue", "muscle", "e" * 64)],
        subjects=[],  # code never placed this document
    )
    assert not out.warnings
    assert all(s.attributes["tissue"].value == "Neurons" for s in out.samples)


def test_a_papers_wrong_reading_is_a_warning_the_record_wins_and_it_still_compiles(
    records: ArchiveRecordSet,
) -> None:
    """The error span verification provably cannot catch, caught by having two independent sources — and now resolved.

    "we dissected neurons and body wall muscle" entails tissue=neurons AND tissue=muscle: both quotes
    are real, both pass span verification and entailment. The record separates them: it is a
    declaration about THIS sample (asserted), the paper's claim is our inference (inferred), and
    asserted wins. The disagreement is a non-blocking WARNING — the record's value stands and the
    dataset still compiles, because a sample annotation is not a reason to refuse a whole manifest.
    """
    doc = "f" * 64
    out = resolve_metadata(
        files=_pilot_files(),
        records=records,
        assertions=[_assertion("experiment.samples.tissue", "muscle", doc)],
        subjects=[DocumentSubject(doc_sha256=doc, scope="dataset")],
    )
    assert len(out.warnings) == 6  # one per sample the paper's claim fanned onto
    warning = out.warnings[0]
    assert warning.subject.ref == "experiment.samples.tissue"
    assert "muscle" in warning.message and "Neurons" in warning.message
    # the record's value stands, and it is not an OPEN conflict — nothing here blocks a compile
    for sample in out.samples:
        assert sample.attributes["tissue"].value == "Neurons"


def test_two_equal_authorities_disagreeing_leave_null_as_a_warning(
    records: ArchiveRecordSet,
) -> None:
    """A wrong value here is permanent; a missing one is not. Code does not break a tie between equals,

    so the attribute is left null — a value, per the "null beats a wrong guess" rule — and the
    disagreement rides along as a non-blocking warning rather than a refusal.
    """
    doc = "9" * 64
    out = resolve_metadata(
        files=_pilot_files(),
        records=records,
        assertions=[_assertion("experiment.samples.tissue", "muscle", doc)],
        subjects=[DocumentSubject(doc_sha256=doc, scope="sample", subject="SAMN40935621")],
    )
    target = next(s for s in out.samples if s.accession == "SAMN40935621")
    assert "tissue" not in target.attributes  # left null: two asserted sources disagree
    assert any(
        "SAMN40935621" in w.message and w.subject.ref == "experiment.samples.tissue"
        for w in out.warnings
    )
    # every other sample is untouched: the document named one sample and only one
    for sample in out.samples:
        if sample.accession != "SAMN40935621":
            assert sample.attributes["tissue"].value == "Neurons"


def test_two_equal_authorities_agreeing_only_in_case_resolve_rather_than_null(
    records: ArchiveRecordSet,
) -> None:
    """'Male' and 'male' are the same value; a permanent manifest must not null an equal-authority
    attribute over capitalization alone (PRJNA1195922 lost `sex` exactly this way). Here the record
    says tissue "Neurons" and a sample-scoped assertion says "neurons" — equal authority, agreeing
    case-insensitively — so the attribute RESOLVES, with no disagreement warning.
    """
    doc = "7" * 64
    out = resolve_metadata(
        files=_pilot_files(),
        records=records,
        assertions=[_assertion("experiment.samples.tissue", "neurons", doc)],
        subjects=[DocumentSubject(doc_sha256=doc, scope="sample", subject="SAMN40935621")],
    )
    target = next(s for s in out.samples if s.accession == "SAMN40935621")
    assert "tissue" in target.attributes  # NOT null — the two agree case-insensitively
    assert target.attributes["tissue"].value.casefold() == "neurons"
    # and no ambiguity warning was raised for this sample's tissue
    assert not any(
        "SAMN40935621" in w.message and w.subject.ref == "experiment.samples.tissue"
        for w in out.warnings
    )


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


def test_the_ask_is_scoped_so_a_biosample_is_never_asked_for_a_chemistry() -> None:
    """A sample record has no opinion about the chemistry, so asking invites a guess from an alias.

    "single nucleus sequencing daf2 replicate 3" contains no chemistry, but it does contain words a
    model could pattern-match on. The cheapest defence is not asking.
    """
    from seqforge.harvest.fields import fields_for

    assert "library.chemistry" not in fields_for("sample", "reference")
    assert "experiment.samples.tissue" in fields_for("sample", "reference")
    # ...and the experiment's protocol paragraph is asked for the chemistry, plus `treatment` (and only
    # treatment): the GSM title carries the diet, which lives nowhere in the typed BioSample fields.
    assert fields_for("experiment", "reference") == (
        "library.chemistry",
        "experiment.samples.treatment",
    )
    # ...but NOT strain/age/tissue: those are the BioSample's own typed fields, and asking the title
    # for them would let "Day6" vs "day6" null a value the record already resolved.
    assert "experiment.samples.strain" not in fields_for("experiment", "reference")
    assert "experiment.samples.age" not in fields_for("experiment", "reference")
    # ...and the project level is asked nothing at all: "wild-type and daf-2 mutants" is true of the
    # study and false of every single sample in it.
    assert fields_for("project", "reference") == ()


def test_a_record_document_may_never_set_processing(records: ArchiveRecordSet) -> None:
    """An archive field is an untrusted input. Prose reaching --soloStrand is precisely what we forbid."""
    from seqforge.harvest.fields import fields_for, permitted_for

    for scope in ("project", "sample", "experiment", "run"):
        assert not any(f.startswith("processing.") for f in fields_for(scope, "reference"))
        assert not permitted_for("processing.genome.assembly", scope, "reference")


def test_every_asked_attribute_is_one_ncbi_defines() -> None:
    """Derived, not typed twice. A name we invent here would sail past the manifest validator's
    key check only by being invented in both places -- which is exactly how `condition` survived."""
    from seqforge.harvest.fields import ASKED_SAMPLE_ATTRIBUTES
    from seqforge.io.attributes import is_attribute

    for name in ASKED_SAMPLE_ATTRIBUTES:
        assert is_attribute(name), f"{name!r} is not an NCBI harmonized BioSample attribute"
    assert "condition" not in ASKED_SAMPLE_ATTRIBUTES


def test_the_ask_carries_ncbis_own_definition_not_our_paraphrase() -> None:
    """The prompt is the worst place to keep a definition: nothing checks it, and it is exactly where
    the pilot's misfiling happened. So the text comes out of NCBI's list."""
    from seqforge.harvest.fields import describe_asked
    from seqforge.io.attributes import get_attribute

    text = describe_asked(("experiment.samples.dev_stage",))
    assert get_attribute("dev_stage").description in text
    assert "dev_stage" in text


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
    for path, want in sorted(claims.items()):
        got = _extract_experiment_field(path, out)
        assert _equal(want, got), f"{path}: expected {want!r}, got {got!r}"


def test_a_named_sample_pins_the_join_not_just_the_multiset() -> None:
    """`experiment.samples.*.strain` would pass even if the six facts were on the wrong six samples.

    Which is why the pre-registration names two of them. The `*` form asserts what the dataset
    contains; the named form asserts that the join put it in the right place.
    """
    from seqforge.evals import discover_cases

    case = next(c for c in discover_cases() if c.id == "PRJNA1027859")
    named = {k for k in case.expected.fields if k.startswith("experiment.samples.SAMN")}
    assert named, "no named-sample claim: a shuffled join would grade clean"
