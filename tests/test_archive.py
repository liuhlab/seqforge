"""The archive transcriber's network seam: the ``labdata`` accession hop, then ``efetch`` + parse.

``_experiments_for`` no longer routes through ENA/GEO-SOFT — it delegates the accession -> SRA
experiments hop to :func:`labdata.experiments_for`, whose Entrez ``elink`` route reaches a GEO
SuperSeries our own SOFT recursion could not. These tests mock that hop (seqforge never hits the
network in a test) and the ``efetch`` calls, then drive the *real* parse/merge path on the committed
pilot XML — the same fixtures :mod:`test_records` uses — so the composition is exercised end to end
without a byte of network.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Sequence
from pathlib import Path

import pytest

from seqforge.io import IO_VERSION, archive
from seqforge.io.remote import RemoteError
from seqforge.models.records import ArchiveRecord, ArchiveRecordSet

FIXTURES = Path(__file__).parent / "fixtures" / "archive"


class _FakeExperiment:
    """The shape ``_experiments_for`` reads off a ``labdata`` Experiment: just ``.accession``."""

    def __init__(self, accession: str) -> None:
        self.accession = accession


def _patch_labdata(
    monkeypatch: pytest.MonkeyPatch, resolver: Callable[[str], list[_FakeExperiment]]
) -> None:
    """Install ``resolver`` as ``labdata.experiments_for`` (absent in the pinned build, so no raise)."""
    import labdata

    monkeypatch.setattr(labdata, "experiments_for", resolver, raising=False)


# ------------------------------------------------------------ the labdata accession hop


def test_experiments_for_returns_labdatas_experiment_accessions_sorted_and_deduped(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: list[str] = []

    def resolver(accession: str) -> list[_FakeExperiment]:
        seen.append(accession)  # record it to prove the accession passes through unchanged
        return [_FakeExperiment(a) for a in ("SRX2", "SRX1", "SRX2")]

    _patch_labdata(monkeypatch, resolver)
    assert archive._experiments_for("GSE229022") == ["SRX1", "SRX2"]
    assert seen == [
        "GSE229022"
    ]  # reaches labdata untransformed (folded from the passthrough sibling)


# `test_experiments_for_translates_a_labdata_error_into_a_remote_error` was deleted (#110): it raised
# `AccessionError`, a subclass of `LabdataError`, which `_experiments_for` catches on one `except
# LabdataError`. `test_experiments_for_does_not_retry_a_terminal_labdata_error` below raises the base
# class onto that same except -- same RemoteError message -- and additionally pins `calls["n"] == 1`,
# so it reddens wherever this one would, and for the narrower catch too (base escapes, subclass would not).


def test_experiments_for_refuses_loudly_when_the_accession_resolves_to_nothing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_labdata(monkeypatch, lambda acc: [])
    # An accession that was GIVEN and yields no experiments is a refusal, not a silent omission from
    # a permanent, content-addressed manifest.
    with pytest.raises(RemoteError, match="no experiments found"):
        archive._experiments_for("GSE229022")


def test_experiments_for_retries_a_transient_labdata_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A momentary NCBI 5xx at the accession hop must not abort `records` (#9): GSE274290 hit a bare
    HTTP 500 live. A transient labdata error backs off and retries, exactly as `_get` does."""
    from labdata.exceptions import LabdataError

    calls = {"n": 0}

    def resolver(accession: str) -> list[_FakeExperiment]:
        calls["n"] += 1
        if calls["n"] == 1:
            raise LabdataError(
                "NCBI E-utilities request failed: HTTP Error 500: Internal Server Error"
            )
        return [_FakeExperiment("SRX1")]

    _patch_labdata(monkeypatch, resolver)
    monkeypatch.setattr(time, "sleep", lambda _s: None)  # `archive` calls `time.sleep` directly
    assert archive._experiments_for("GSE274290") == ["SRX1"]
    assert calls["n"] == 2  # retried through the 500


def test_experiments_for_does_not_retry_a_terminal_labdata_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A malformed accession is terminal — no backoff, one attempt, a loud RemoteError."""
    from labdata.exceptions import LabdataError

    calls = {"n": 0}

    def resolver(accession: str) -> list[_FakeExperiment]:
        calls["n"] += 1
        raise LabdataError("banana is not a resolvable accession")

    _patch_labdata(monkeypatch, resolver)
    monkeypatch.setattr(time, "sleep", lambda _s: None)
    with pytest.raises(RemoteError, match="could not resolve experiments"):
        archive._experiments_for("banana")
    assert calls["n"] == 1


# ------------------------------------------------------------ the whole fetch, composed


def test_fetch_records_composes_labdatas_hop_with_the_efetch_parse_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """labdata resolves the experiments; the committed pilot XML drives the rest, unchanged."""
    _patch_labdata(monkeypatch, lambda acc: [_FakeExperiment("SRX24283130")])

    fixtures = {
        "sra": (FIXTURES / "PRJNA1027859.sra.xml").read_text(),
        "biosample": (FIXTURES / "PRJNA1027859.biosample.xml").read_text(),
        "bioproject": (FIXTURES / "PRJNA1027859.bioproject.xml").read_text(),
    }

    def fake_efetch(db: str, ids: list[str], **params: str) -> str:
        return fixtures[db]

    monkeypatch.setattr(archive, "_efetch", fake_efetch)

    record_set = archive.fetch_records("PRJNA1027859")

    assert record_set.query == "PRJNA1027859"
    # All four levels come through the same parse the pilot exercised.
    assert record_set.at("project")
    assert record_set.at("experiment")
    runs = record_set.at("run")
    assert runs
    # And the BioSample harmonization still lands: the pilot's two strains reach the records under
    # NCBI's own harmonized `strain` key (this is exactly what the merge step exists to do).
    samples = record_set.at("sample")
    assert samples
    strains = {
        attr.value for sample in samples for attr in sample.attributes if attr.name == "strain"
    }
    assert {"CQ757", "CQ758"} <= strains


# ------------------------------------------------------------ a study bigger than one efetch batch

_STUDY = (
    '<STUDY center_name="BioProject" alias="PRJNA9999999" accession="SRP999999">'
    "<IDENTIFIERS><PRIMARY_ID>SRP999999</PRIMARY_ID>"
    '<EXTERNAL_ID namespace="BioProject">PRJNA9999999</EXTERNAL_ID></IDENTIFIERS>'
    "<DESCRIPTOR><STUDY_TITLE>A study of more runs than fit in one request</STUDY_TITLE>"
    "</DESCRIPTOR></STUDY>"
)


def _package_set(pairs: Sequence[tuple[str, str]]) -> str:
    """``efetch db=sra`` XML for (experiment, biosample) pairs, every package restating the STUDY.

    Restating it is the archive's own shape — a package is the whole chain above one experiment — and
    it is why a fetch that pages sees the same study once per page.
    """
    packages = "".join(
        f'<EXPERIMENT_PACKAGE><EXPERIMENT accession="{exp}"><TITLE>{exp}</TITLE></EXPERIMENT>'
        f"{_STUDY}"
        f'<SAMPLE accession="SRS{exp[3:]}" alias="{sample} nuclei"><IDENTIFIERS>'
        f'<EXTERNAL_ID namespace="BioSample">{sample}</EXTERNAL_ID></IDENTIFIERS>'
        "<SAMPLE_NAME><TAXON_ID>6239</TAXON_ID></SAMPLE_NAME></SAMPLE>"
        f'<RUN_SET><RUN accession="SRR{exp[3:]}"/></RUN_SET></EXPERIMENT_PACKAGE>'
        for exp, sample in pairs
    )
    return f"<EXPERIMENT_PACKAGE_SET>{packages}</EXPERIMENT_PACKAGE_SET>"


def _paged_archive(
    monkeypatch: pytest.MonkeyPatch, pairs: Sequence[tuple[str, str]]
) -> list[tuple[str, list[str]]]:
    """Serve ``pairs`` as an archive that answers exactly the ids each request asks for.

    Returns the call log, so a test can see how many pages the fetch actually took. The biosample and
    bioproject replies are empty on purpose: they only ever *add* attributes, and this is about how
    many records come back.
    """
    _patch_labdata(monkeypatch, lambda acc: [_FakeExperiment(exp) for exp, _ in pairs])
    calls: list[tuple[str, list[str]]] = []

    def fake_efetch(db: str, ids: list[str], **params: str) -> str:
        calls.append((db, list(ids)))
        if db != "sra":
            return "<RecordSet/>"
        asked = set(ids)
        return _package_set([p for p in pairs if p[0] in asked])

    monkeypatch.setattr(archive, "_efetch", fake_efetch)
    return calls


def test_a_study_too_large_for_one_efetch_holds_exactly_one_project_record(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Every page restates the study, so deduplicating within a page leaves one project record per
    page (#239). SRP383998's 1440 runs came back with the project 15 times, which is exactly its page
    count — a defect no study small enough for a single request can show.
    """
    pairs = [(f"SRX{i:07d}", f"SAMN{i:07d}") for i in range(2 * archive._BATCH + 50)]
    calls = _paged_archive(monkeypatch, pairs)

    record_set = archive.fetch_records("PRJNA9999999")

    assert [db for db, _ in calls].count("sra") == 3, "the fixture must span more than one page"
    assert [r.accession for r in record_set.at("project")] == ["PRJNA9999999"]
    seen = {(r.level, r.accession) for r in record_set.records}
    assert len(seen) == len(record_set.records)  # and no level duplicates across pages either
    assert len(record_set.at("experiment")) == len(record_set.at("run")) == len(pairs)


def test_a_sample_whose_experiments_straddle_a_page_boundary_is_one_record(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A sample repeats in every page holding one of its experiments, and pages split on experiment
    count, so a sample with several experiments lands in two of them. The duplicate reaches the
    biosample request too, where it spends an id slot on an attribute set already asked for.
    """
    per_sample = 60  # so SAMN0000001's experiments (60-119) fall either side of the 100 boundary
    pairs = [(f"SRX{i:07d}", f"SAMN{i // per_sample:07d}") for i in range(2 * archive._BATCH + 50)]
    calls = _paged_archive(monkeypatch, pairs)

    record_set = archive.fetch_records("PRJNA9999999")

    assert [r.accession for r in record_set.at("sample")] == [f"SAMN{i:07d}" for i in range(5)]
    asked = [ids for db, ids in calls if db == "biosample"]
    assert asked and all(len(ids) == len(set(ids)) for ids in asked)


# ------------------------------------------------------------ what the package states in prose


@pytest.fixture(scope="module")
def nasal_package() -> list[ArchiveRecord]:
    """SRP383998, exactly as NCBI served it. Its `DESIGN_DESCRIPTION` is empty and the chemistry is
    stated only in `LIBRARY_CONSTRUCTION_PROTOCOL` — the deposit shape that measured #237."""
    return archive.parse_sra_package_set((FIXTURES / "SRP383998.sra.xml").read_text())


def _experiment(records: list[ArchiveRecord]) -> ArchiveRecord:
    experiments = [r for r in records if r.level == "experiment"]
    assert len(experiments) == 1
    return experiments[0]


def test_the_construction_protocol_reaches_the_experiment_record(
    nasal_package: list[ArchiveRecord],
) -> None:
    """The chemistry is in the protocol paragraph and nowhere else, so a parser that reads only
    `DESIGN_DESCRIPTION` throws the version away: the record's own prose says "Smart-Seq3" and
    everything we kept said "SmartSeq", which resolves to no KB node at all."""
    experiment = _experiment(nasal_package)
    assert experiment.text("design_description") is None  # the deposit left it empty
    protocol = experiment.text("library_construction_protocol")
    assert protocol is not None, "the one field stating the chemistry never reached the record"
    assert "Smart-Seq3" in protocol


def test_the_construction_protocol_arrives_as_prose_and_not_as_a_typed_slot(
    nasal_package: list[ArchiveRecord],
) -> None:
    """It is free text a submitter wrote, so it belongs in the half a model reads and a span check
    verifies. A typed attribute would hand the value straight to the resolver with nothing to grep
    back against, and the filing columns beside it stay typed precisely because they are not prose."""
    experiment = _experiment(nasal_package)
    assert "library_construction_protocol" not in {a.name for a in experiment.attributes}
    assert not any("Smart-Seq3" in a.value for a in experiment.attributes)
    assert {"library_strategy", "library_source", "library_selection"} <= {
        a.name for a in experiment.attributes
    }


def test_the_samples_own_title_reaches_the_record(nasal_package: list[ArchiveRecord]) -> None:
    """`SAMPLE/TITLE` is what the submitter called the material; the alias beside it is the archive's
    id for it. Keeping only the alias leaves a sample document that names no subject a human would
    recognise."""
    samples = [r for r in nasal_package if r.level == "sample"]
    assert [s.text("sample_title") for s in samples] == ["nasal_prox1_270"]
    assert [s.text("sample_alias") for s in samples] == ["GSM6277169"]


# ------------------------------------------------------------ what the submitter uploaded

#: Both committed packages, because one carries six runs of a 10x deposit and the other a single
#: Smart-Seq3 run whose two Original entries are served in reverse order — the case a parser that
#: kept the archive's order would pass and a sorted one would not.
_SRA_PACKAGES = ("PRJNA1027859.sra.xml", "SRP383998.sra.xml")

#: Every run's filenames as `_original_filenames` returned them before `submitted_files` became the
#: stored field: sorted, deduplicated, Originals only. Pinned as literals rather than recomputed from
#: the parser, because a derived property checked against the code that fills it agrees with any code.
_DECLARED_FILENAMES: dict[str, list[str]] = {
    "SRR28716553": [
        "2562__daf-2_R3_library-read-1.fastq.gz",
        "2562__daf-2_R3_library-read-4.fastq.gz",
    ],
    "SRR28716554": [
        "2495__daf-2_R2_library-read-1.fastq.gz",
        "2495__daf-2_R2_library-read-4.fastq.gz",
    ],
    "SRR28716555": [
        "2235__daf-2_neuron_nuclei_library-read-1.fastq.gz",
        "2235__daf-2_neuron_nuclei_library-read-4.fastq.gz",
    ],
    "SRR28716556": [
        "2562__Wild-type_R3_library-read-1.fastq.gz",
        "2562__Wild-type_R3_library-read-4.fastq.gz",
    ],
    "SRR28716557": [
        "2495__wild-type_R2_library-read-1.fastq.gz",
        "2495__wild-type_R2_library-read-4.fastq.gz",
    ],
    "SRR28716558": [
        "2235__N2_wild_type_neuron_nuclei_library-read-1.fastq.gz",
        "2235__N2_wild_type_neuron_nuclei_library-read-4.fastq.gz",
    ],
    "SRR19886090": ["NasalProx1_270_1.fastq.gz", "NasalProx1_270_2.fastq.gz"],
}


@pytest.fixture(scope="module")
def submitted_runs() -> list[ArchiveRecord]:
    """Every run record in both committed packages, off the real parse of the real bytes."""
    return [
        record
        for package in _SRA_PACKAGES
        for record in archive.parse_sra_package_set((FIXTURES / package).read_text())
        if record.level == "run"
    ]


def test_every_submitted_file_carries_the_md5_size_and_uri_beside_its_name(
    submitted_runs: list[ArchiveRecord],
) -> None:
    """The four facts arrive on one `<SRAFile>` element, so they are one value (ADR-0033).

    A filename with no hash and no URI is what we had; an md5 with no URI names bytes nobody can
    reach. The URI is the `<Alternatives>` child of the *same* element, which is why each one must
    name its own file rather than a sibling's — reading the run's first `<Alternatives>` for every
    file would give every entry the same, wrong, bucket path.
    """
    files = [f for run in submitted_runs for f in run.submitted_files]
    assert len(files) == sum(len(names) for names in _DECLARED_FILENAMES.values())
    for f in files:
        assert f.md5 is not None and len(f.md5) == 32, f"{f.filename}: no provider md5"
        assert f.size_bytes is not None and f.size_bytes > 0, f"{f.filename}: no declared size"
        assert f.uri is not None and f.uri.startswith("s3://sra-pub-src-"), (
            f"{f.filename}: the submitter's own upload lives in a `sra-pub-src-*` bucket, and "
            f"{f.uri} is not one"
        )
        assert f.filename in f.uri, f"{f.filename}: the URI came off another element ({f.uri})"


def test_the_submitted_files_are_the_uploads_and_not_sras_own_normalized_products(
    submitted_runs: list[ArchiveRecord],
) -> None:
    """`supertype="Original"` still selects, and it now has to do more work than it did.

    The `.sra` and `.lite` entries beside them carry an `@md5`, an `@size` and their own
    `<Alternatives>` too, so a parser that lost the supertype filter would fill all four fields for
    all of them and every assertion about shape would still pass. What it would have transcribed is
    a file the submitter never uploaded and nobody has on disk, addressed in a bucket
    (`sra-pub-run-odp`, `sra-pub-zq-*`) holding SRA's regenerated copy rather than the original.
    """
    files = [f for run in submitted_runs for f in run.submitted_files]
    assert not [f for f in files if f.filename.endswith((".lite", ".sra"))]
    assert not [f for f in files if f.uri and "sra-pub-zq" in f.uri]
    assert all(f.filename.endswith(".fastq.gz") for f in files)


def test_the_filenames_property_returns_what_the_stored_field_no_longer_duplicates(
    submitted_runs: list[ArchiveRecord],
) -> None:
    """`filenames` is derived, so the join it feeds keeps its shape and the names are stored once.

    Sorted and deduplicated exactly as `sorted(set(...))` was: a record set is content-addressed and
    cached, so the order the archive happened to serve two entries in must not reach it — SRP383998
    serves its mate 2 before its mate 1.
    """
    assert {run.accession: run.filenames for run in submitted_runs} == _DECLARED_FILENAMES
    dumped = submitted_runs[0].model_dump()
    assert "filenames" not in dumped, "the names would then be stored twice, and could disagree"
    assert [f["filename"] for f in dumped["submitted_files"]] == submitted_runs[0].filenames


def test_a_freshly_fetched_record_set_carries_the_version_that_wrote_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The stamp is what separates "this deposit publishes no originals" from "this cache is older
    than the question". Most deposits publish none, so absence of files is the normal case and can
    never be the signal — which is why the second half of this test matters as much as the first.
    """
    _patch_labdata(monkeypatch, lambda acc: [_FakeExperiment("SRX15982970")])
    monkeypatch.setattr(
        archive,
        "_efetch",
        lambda db, ids, **params: (
            (FIXTURES / "SRP383998.sra.xml").read_text() if db == "sra" else "<RecordSet/>"
        ),
    )
    assert archive.fetch_records("SRP383998").io_version == IO_VERSION

    # The same fetch over a package set declaring no `<SRAFile>` at all: stamped all the same, and
    # its runs' emptiness is then a fact about the deposit rather than about our parser's age.
    _paged_archive(monkeypatch, [("SRX0000001", "SAMN0000001")])
    no_originals = archive.fetch_records("PRJNA9999999")
    assert [run.submitted_files for run in no_originals.at("run")] == [[]]
    assert no_originals.io_version == IO_VERSION


def test_a_record_set_written_before_submitted_files_loads_and_reads_as_unstamped() -> None:
    """Every committed benchmark transcript predates the writer stamp, and still reads as unstamped.

    They were migrated to `submitted_files` in the same change that introduced it — the legacy key is
    deliberately not accepted on input — so what they prove is the *stamp* half and not the rename:
    the absence of `io_version` survives a round trip, which is what a resolver refusing a stale cache
    has to be able to see. Their names arriving is the migration's own guarantee, held one test up.
    """
    benchmark = Path(__file__).resolve().parents[1] / "evals" / "benchmark"
    committed = sorted(benchmark.glob("*/records.json"))
    assert committed, "the committed transcripts are the fixture here"

    declared: list[str] = []
    for path in committed:
        record_set = ArchiveRecordSet.model_validate_json(path.read_text())
        assert record_set.io_version is None, f"{path.parent.name} predates the stamp"
        declared.extend(name for run in record_set.at("run") for name in run.filenames)
    assert declared, "no committed transcript declares a filename, so nothing was proven"


def test_efetch_adds_the_ncbi_api_key_only_when_the_environment_sets_one(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """eutils raises its 3->10 req/sec cap for a keyed caller (#9). The key is read from the
    environment and added to the request params, and it never appears when unset."""
    captured: dict[str, dict[str, str] | None] = {}

    def fake_get(url: str, params: dict[str, str] | None = None, timeout: int = 30) -> str:
        captured["params"] = params
        return "<EXPERIMENT_PACKAGE_SET/>"

    monkeypatch.setattr(archive, "_get", fake_get)

    monkeypatch.setenv("NCBI_API_KEY", "SECRET-KEY-123")
    archive._efetch("sra", ["SRX1"])
    keyed = captured["params"]
    assert keyed is not None, "_efetch reached _get with no params at all"
    assert keyed["api_key"] == "SECRET-KEY-123"

    monkeypatch.delenv("NCBI_API_KEY", raising=False)
    archive._efetch("sra", ["SRX1"])
    unkeyed = captured["params"]
    assert unkeyed is not None, "_efetch reached _get with no params at all"
    assert "api_key" not in unkeyed
