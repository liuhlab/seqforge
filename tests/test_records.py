"""Tests for ``resolve/records.py`` — which sample each file is.

The metadata resolver and the provenance gate are one subject: the gate is what decides whether the
prose it reads describes THIS dataset at all, so it belongs beside the resolver it guards.
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
from seqforge.models.blocker import BlockerCode
from seqforge.models.observation import FileIdentity
from seqforge.models.records import ArchiveRecordSet
from seqforge.resolve.records import resolve_metadata

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
# the join comes from records, and records are 60 kB of XML.


FIXTURES = Path(__file__).parent / "fixtures" / "archive"

#: The pilot's six runs: three wild-type, three daf-2. Transcribed from the BioSample records, which
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


def _file(basename: str, sha: str, size_bytes: int = 1024) -> FileIdentity:
    """Identity only. The resolver is handed no probe output at all — see `resolve/records.py`.

    ``size_bytes`` is the one field here anything reads: it is what a record's declared size is
    compared against, and every other test wants it to be some number and does not care which.
    """
    return FileIdentity(basename=basename, sha256=sha, size_bytes=size_bytes)


def _pilot_files() -> list[FileIdentity]:
    """Two files per run, named the way `fasterq-dump --split-files` writes them."""
    out: list[FileIdentity] = []
    for i, run in enumerate(sorted(WT_RUNS + DAF2_RUNS)):
        for mate in (1, 2):
            out.append(_file(f"{run}_{mate}.fastq.gz", f"{i}{mate}".ljust(64, "a")))
    return out


# ---------------------------------------------------------------- the records themselves


def test_the_hierarchy_joins_run_to_experiment_to_sample_to_project(
    records: ArchiveRecordSet,
) -> None:
    """The join is the archive's own, followed by code. Every run reaches a sample and a project."""
    counts = {level: len(records.at(level)) for level in ("project", "sample", "experiment", "run")}
    assert counts == {"project": 1, "sample": 6, "experiment": 6, "run": 6}
    for run in records.at("run"):
        sample = records.ancestor(run, "sample")
        assert sample is not None, f"{run.accession} reaches no sample"
        assert sample.accession.startswith("SAMN")
        project = records.ancestor(run, "project")
        assert project is not None
        assert project.accession == "PRJNA1027859"


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


# ---------------------------------------------------------------- A1: the join


def test_each_pilot_file_lands_on_its_own_biosample(records: ArchiveRecordSet) -> None:
    """Twelve files, six BioSamples, two mates each: the join is the archive's, by run accession."""
    out = resolve_metadata(files=_pilot_files(), records=records)
    assert not out.blockers
    assert len(out.samples) == 6
    for sample in out.samples:
        assert len(sample.file_shas) == 2, f"{sample.sample_id} lost a mate"
        assert sample.accession is not None
        assert sample.accession.startswith("SAMN")
        assert sample.sample_id == sample.accession


def test_the_dataset_wide_facts_come_off_the_record_and_cite_it(records: ArchiveRecordSet) -> None:
    """`experiment.organism` used to be a CLI flag citing nothing. The record declares it, once per
    sample — and a study is structured facts only, never the abstract: a hashed manifest does not
    hold a paragraph."""
    out = resolve_metadata(files=_pilot_files(), records=records)
    assert out.organism is not None
    assert (out.organism.value, out.organism.basis) == (6239, "asserted")
    assert len(out.organism.evidence) == 6
    assert out.project is not None
    assert out.project.accession == "PRJNA1027859"
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


def test_a_record_that_does_not_account_for_the_files_refuses(records: ArchiveRecordSet) -> None:
    """Half-joining is the failure. Four placed samples and two silent ones reads as six."""
    obs = [*_pilot_files(), _file("mystery_1.fastq.gz", "c" * 64)]
    out = resolve_metadata(files=obs, records=records)
    assert [b.code for b in out.blockers] == [BlockerCode.RECORD_JOIN_INCOMPLETE]
    assert out.samples == []
    assert "mystery_1.fastq.gz" in out.blockers[0].evidence


# ------------------------------------------------- the size the archive declares, and what it may do
#
# The archive publishes a size beside every submitted filename, and the temptation it creates is to
# join on it. It may not: a size is a coincidence between two numbers, and a filename is a fact the
# submitter typed, so matching on the first would be a guess laid over the second. What it may do is
# *comment* on a join the name already made — and only there, because only a file matched by the name
# the record declares is claiming to BE that submitted file. A file matched by its run accession
# claims nothing of the sort: `fasterq-dump` regenerates a FASTQ that has no reason to weigh what the
# submitter uploaded, so a disagreement there is the normal case and a warning on it would be noise on
# every dataset fetched the ordinary way. Never a `Blocker` either way (ADR-0010): this stage decides.

#: The pilot's real SRA numbers, from `<SRAFile filename=... size=...>` — the shape ADR-0033 quotes.
SUBMITTED_NAME = "NasalProx1_270_2.fastq.gz"
SUBMITTED_SIZE = 28543057


def _one_run_records(
    *,
    size_bytes: int | None = None,
    io_version: str | None = "2026.7.0",
    submitted: bool = True,
) -> ArchiveRecordSet:
    """One run, reachable BOTH ways: by its accession, and by the name the submitter uploaded under.

    One record set serving both joins is what makes the rows below a comparison — the record, the
    declared size and the file on disk are held fixed and the only thing that varies is which of the
    two names the file carries. ``submitted=False`` is the deposit that published no originals.
    """
    from seqforge.models.records import ArchiveRecord, SubmittedFile

    uploaded = [SubmittedFile(filename=SUBMITTED_NAME, size_bytes=size_bytes)] if submitted else []
    return ArchiveRecordSet(
        source="fixture",
        query="PRJNA249",
        io_version=io_version,
        records=[
            ArchiveRecord(level="sample", accession="SAMN249"),
            ArchiveRecord(level="experiment", accession="SRX19886090", parent="SAMN249"),
            ArchiveRecord(
                level="run",
                accession="SRR19886090",
                parent="SRX19886090",
                submitted_files=uploaded,
            ),
        ],
    )


@pytest.mark.parametrize(
    ("basename", "declared", "warns"),
    [
        # The file wears the submitter's own name, so it claims to BE that upload — and weighs 4096.
        (SUBMITTED_NAME, SUBMITTED_SIZE, True),
        # A file `fasterq-dump` wrote out of SRA's normalized copy claimed nothing of the sort, so
        # its weight bears on nothing and a note here would land on every ordinary download.
        ("SRR19886090_2.fastq.gz", SUBMITTED_SIZE, False),
        # Every record set written before the size was transcribed: absent is not a disagreement,
        # and a note saying so would fire on every migrated transcript in the corpus.
        (SUBMITTED_NAME, None, False),
    ],
    ids=["the_name_made_the_join", "the_accession_made_the_join", "the_record_declared_no_size"],
)
def test_a_declared_size_may_comment_on_a_filename_join_and_may_never_unmake_one(
    basename: str, declared: int | None, warns: bool
) -> None:
    """Both numbers go in the message: a note saying only "the size differs" leaves the reader to
    fetch the record to learn by how much, which is the work it exists to save.

    Every row also asserts the join stands and the compile exits 0 (ADR-0010) — the alternative
    reading of a size disagreement is "this is the wrong file, drop the join", which would strand the
    file with no sample while the manifest still read as though it described it.
    """
    out = resolve_metadata(
        files=[_file(basename, "d" * 64, size_bytes=4096)],
        records=_one_run_records(size_bytes=declared),
    )
    notes = [w for w in out.warnings if w.code == "submitted_file_size_mismatch"]
    if warns:
        assert len(notes) == 1, f"warnings were {[w.model_dump() for w in out.warnings]}"
        assert notes[0].subject.ref == SUBMITTED_NAME
        assert SUBMITTED_NAME in notes[0].message, "a note not naming the file cannot be acted on"
        assert str(SUBMITTED_SIZE) in notes[0].message, "the size the archive declares"
        assert "4096" in notes[0].message, "and the size the file on disk actually is"
    else:
        assert notes == []
    assert out.blockers == [], "a declared size may never refuse a dataset"
    assert [s.accession for s in out.samples] == ["SAMN249"], "the join is the name's to make"
    assert out.samples[0].file_shas == ["d" * 64]


# --------------------------------- a stale record set is not a deposit that published no originals


def test_an_unstamped_record_set_that_cannot_join_names_the_re_fetch() -> None:
    """A set with no writer stamp was transcribed before seqforge read submitted files at all.

    Its silence is a question we did not ask, not an answer the archive gave, and the two must not
    read alike — the old message blamed the archive for declaring no such filenames, which sends a
    reader to check their download when the fix is one command against the cache.
    """
    records = _one_run_records(size_bytes=None, io_version=None)
    out = resolve_metadata(files=[_file("renamed_R1.fastq.gz", "e" * 64)], records=records)

    assert [b.code for b in out.blockers] == [BlockerCode.RECORD_JOIN_INCOMPLETE]
    blocker = out.blockers[0]
    assert "seqforge io records PRJNA249" in blocker.remedy, (
        "the remedy has to name the command that rewrites the set, and the accession to run it on"
    )
    assert "the original filenames the record declares" not in blocker.message, (
        "that wording asserts the archive declared nothing, which an unstamped set cannot know"
    )


def test_a_stamped_set_that_declares_no_originals_still_blames_neither_side() -> None:
    """The contrast case, and the reason the stamp exists: most deposits publish no originals.

    Here the transcriber DID look and the archive published nothing, so the join really did run out of
    both halves and the message that says so is the honest one. Re-fetching would change nothing and
    telling someone to do it would waste the one remedy they read.
    """
    records = _one_run_records(submitted=False)
    out = resolve_metadata(files=[_file("renamed_R1.fastq.gz", "f" * 64)], records=records)

    assert [b.code for b in out.blockers] == [BlockerCode.RECORD_JOIN_INCOMPLETE]
    blocker = out.blockers[0]
    assert "the original filenames the record declares" in blocker.message
    assert "seqforge io records" not in blocker.remedy, (
        "a fresh set re-fetches to the same emptiness; the remedy must stay about the files on disk"
    )


# ---------------------------------------------------------------- no record at all


def test_no_record_is_not_a_refusal() -> None:
    """Most sequencing data never had an accession. It still compiles — and with nothing to group by,
    the run key IS the sample identity."""
    out = resolve_metadata(files=_pilot_files(), records=None)
    assert not out.blockers
    assert sorted(s.sample_id for s in out.samples) == sorted(WT_RUNS + DAF2_RUNS)
    assert all(s.accession is None for s in out.samples)
    assert out.project is None
    assert out.organism is None


# ---------------------------------------------------------------- A3: a record IS a document


def test_a_record_becomes_its_own_document_scoped_to_itself(records: ArchiveRecordSet) -> None:
    """The document holds one record's prose, so the subject is the document.

    And only its prose. `strain = CQ758` is already a key and a value, so showing a model the typed
    half would be asking it to transcribe something code can copy — a chance to be wrong.
    """
    from seqforge.harvest import normalize_record

    sample = records.by_accession("SAMN40935621")
    assert sample is not None
    doc = normalize_record(sample)
    assert (doc.scope, doc.subject) == ("sample", "SAMN40935621")
    assert "single nucleus sequencing daf2 replicate 3" in doc.text
    # ...and nothing about any OTHER sample is in it. That is what makes the subject unambiguous.
    assert "replicate 1" not in doc.text
    assert "CQ757" not in doc.text
    # ...nor the structured half of its own record.
    assert "CQ758" not in doc.text
    assert "hermaphrodite" not in doc.text


# ---------------------------------------------------------------- A5: the line, as an import graph


def test_harvest_cannot_see_the_probe() -> None:
    """The model reads prose. It is never shown what the bytes said, and this is why.

    A probe-sighted extractor would settle ties the probe itself created and log the wrong reason:
    the manifest would say `asserted` for a fact a byte decided, and the rung provenance would be a
    lie. Structural because no runtime test can reach it — an import that crossed this line would be
    exercised only by a prompt, and what a prompt contains is not assertable. It caught nothing yet;
    what it holds is that `resolve_metadata` takes `FileIdentity` and not `Observation`, so probe
    signals are absent at this seam rather than merely unread.
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


# ================================================================================================
# A record set a human wrote: it groups, and the grouping is not observed
# ================================================================================================
#
# `source` used to be reported and never branched on. Two messages key on it now, and both would be
# wrong without it. A hand-written set's whole purpose is to fuse runs a filename cannot rejoin — a
# library resequenced for saturation is `_S3` where batch one was `_S1` — so the fuse is a note and
# never a refusal; fired on a deposit that same note would report every ordinary run-to-BioSample
# fusion, which is how a warning stops being read. And a set with no accession and no writer stamp
# would otherwise be told to re-fetch from an archive it was never near.
#
# The hand-written fixture goes through the loader and the ARCHIVE one below does not, which is the
# asymmetry rather than an oversight: the archive dialect IS the container model — the loader
# validates it and nothing more — so a set built directly is byte-for-byte what a cache would load
# as. Only the user dialect has rules living outside the model, and only there can a constructed
# fixture say something the product refuses.


def _user_record_set(
    tmp_path: Path, *, sample_of: dict[str, str], query: str = "records.yaml"
) -> ArchiveRecordSet:
    """A structure-only set: two runs of one library, and which sample each declares itself part of.

    `sample_of` maps a run id to the sample id it parents to, so one call builds both arrangements —
    the fuse (both runs to one sample) and the no-op draft `records new` writes (one sample per run).
    Deliberately carries NO attributes, because a hand-written line has no document to grep back into.

    **Written as YAML and read back through the real loader**, never built as a model: the container
    does not enforce the hand-written dialect, so a fixture constructing `ArchiveRecordSet` directly
    can express a set the product refuses, and every test below would then be proved against a file
    nobody could hand the CLI. Load-bearing twice over here, because both properties under test are
    keyed on `source` and the loader is what decides what a `source: user` set may say.

    The run ids are not the filenames' run keys, which is the realistic shape: a human names the runs
    whatever they call them and joins by `filenames`, so the fuse is visible only by asking what
    `run_key` would have said about the files that landed together.
    """
    import yaml

    from seqforge.recordset import load_record_set

    files_of = {
        "batch1": ["lib7_S1_L001_R1_001.fastq.gz", "lib7_S1_L001_R2_001.fastq.gz"],
        "batch2": ["lib7_S3_L001_R1_001.fastq.gz", "lib7_S3_L001_R2_001.fastq.gz"],
    }
    records: list[dict[str, object]] = [
        {"level": "sample", "id": sample} for sample in sorted(set(sample_of.values()))
    ]
    records += [
        {"level": "run", "id": run, "parent": sample_of[run], "filenames": files_of[run]}
        for run in sorted(sample_of)
    ]
    # `query` is written out rather than left to default: the loader falls back to the file's own
    # STEM, and the refusal below is asserted to print the name a human would recognise the file by.
    path = tmp_path / "records.yaml"
    path.write_text(yaml.safe_dump({"source": "user", "query": query, "records": records}))
    return load_record_set(path)


def _plate_files() -> list[FileIdentity]:
    """The four files, two per resequencing batch. `run_key` reads `lib7_S1` and `lib7_S3`."""
    names = [
        "lib7_S1_L001_R1_001.fastq.gz",
        "lib7_S1_L001_R2_001.fastq.gz",
        "lib7_S3_L001_R1_001.fastq.gz",
        "lib7_S3_L001_R2_001.fastq.gz",
    ]
    return [_file(name, str(i).ljust(64, "7")) for i, name in enumerate(names)]


def test_a_declared_fuse_compiles_as_one_sample_and_says_the_grouping_was_declared(
    tmp_path: Path,
) -> None:
    """The feature working, and the note that keeps it honest.

    `_S1` and `_S3` are one library sequenced twice and nothing in either name says so, which is why
    the record set exists. So the fuse must LAND and must be SAID: two libraries of the same
    chemistry declared as one sample is the shape no other gate catches — the chemistries agree,
    every file is placed, and the result is a plausible matrix nobody would look at twice. The
    message is read rather than counted, because "it warns" is the claim that rots.
    """
    out = resolve_metadata(
        files=_plate_files(),
        records=_user_record_set(tmp_path, sample_of={"batch1": "lib7", "batch2": "lib7"}),
    )

    assert out.blockers == [], "a declared fuse is never a refusal — it is the primary use case"
    assert [s.sample_id for s in out.samples] == ["lib7"]
    assert len(out.samples[0].file_shas) == 4, "all four files compile into the one sample"
    assert out.samples[0].accession is None, (
        "`lib7` is a grouping key a human chose, not a specimen an archive named — storing it as an "
        "accession is both false and unrepresentable, and it arrived here as an uncaught error"
    )

    fused = [w for w in out.warnings if w.code == "declared_sample_fuses_runs"]
    assert len(fused) == 1, f"warnings were {[w.model_dump() for w in out.warnings]}"
    note = fused[0]
    assert all(run in note.message for run in ("lib7_S1", "lib7_S3")), (
        "the note has to name BOTH runs the filenames would have kept apart"
    )
    assert "lib7" in note.message, "and which sample they were fused into"
    assert "declared rather than observed" in note.message, (
        "the point of the note is that no byte and no filename said these are one library"
    )
    assert note.subject.ref == "experiment.samples.lib7"


def test_the_safe_draft_grouping_says_nothing(tmp_path: Path) -> None:
    """`records new` drafts one sample per run, so applying it unedited changes no grouping — and
    must therefore be silent: a note here would fire on every set a human generated and did not
    edit, the same "warn on the normal case" defect the `source` gate prevents one level over."""
    out = resolve_metadata(
        files=_plate_files(),
        records=_user_record_set(tmp_path, sample_of={"batch1": "lib7_a", "batch2": "lib7_b"}),
    )

    assert out.blockers == []
    assert [s.sample_id for s in out.samples] == ["lib7_a", "lib7_b"]
    assert [w for w in out.warnings if w.code == "declared_sample_fuses_runs"] == []


def test_an_archive_set_fusing_its_runs_under_one_biosample_is_silent() -> None:
    """The regression the `source` gate exists to prevent. Joining several runs under one BioSample
    is what `ancestor(run, "sample")` is FOR — the pilot does it six times over — so a note keyed on
    the grouping rather than on who declared it lands on every dataset that has a record at all."""
    from seqforge.models.records import ArchiveRecord

    records = ArchiveRecordSet(
        source="ncbi-sra+biosample",
        query="PRJNA1027859",
        io_version="2026.7.0",
        records=[
            ArchiveRecord(level="sample", accession="SAMN40935621"),
            ArchiveRecord(level="experiment", accession="SRX24276460", parent="SAMN40935621"),
            ArchiveRecord(level="run", accession="SRR28716553", parent="SRX24276460"),
            ArchiveRecord(level="run", accession="SRR28716554", parent="SRX24276460"),
        ],
    )
    out = resolve_metadata(
        files=[
            _file("SRR28716553_1.fastq.gz", "a" * 64),
            _file("SRR28716554_1.fastq.gz", "b" * 64),
        ],
        records=records,
    )

    assert out.blockers == []
    assert [s.sample_id for s in out.samples] == ["SAMN40935621"], "the two runs are one sample"
    assert out.warnings == [], f"an archive fusion is the normal case: {out.warnings}"


def test_a_hand_written_set_that_leaves_a_file_unplaced_is_sent_to_its_own_file(
    tmp_path: Path,
) -> None:
    """Same refusal, correctly explained. A hand-written set has no accession and no writer stamp,
    so the stale-cache branch fires on every one of them and tells a human to re-fetch records from
    an archive that was never involved — a remedy that cannot be followed and does not describe the
    gap. What is missing is a line in a file they can open.
    """
    records = _user_record_set(tmp_path, sample_of={"batch1": "lib7"})
    out = resolve_metadata(
        files=[*_plate_files(), _file("orphan_S9_L001_R1_001.fastq.gz", "9" * 64)],
        records=records,
    )

    assert [b.code for b in out.blockers] == [BlockerCode.RECORD_JOIN_INCOMPLETE]
    blocker = out.blockers[0]
    assert blocker.id == "blk-record-join-incomplete", "the same refusal, not a new one"
    assert out.samples == [], "half-joining is the failure, whoever wrote the set"
    assert "seqforge records new" in blocker.remedy, "the draft-and-edit route"
    assert "filenames" in blocker.remedy, "and the one-line fix that does not need a re-draft"
    assert "seqforge io records" not in blocker.remedy, (
        "there is no archive to re-fetch from: the record set was written by hand"
    )
    assert "fasterq-dump" not in blocker.remedy
    assert "the record set records.yaml" in blocker.message, (
        "the set's `query` is a filename here, so it must not be dropped where an accession is read"
    )
    assert "orphan_S9_L001_R1_001.fastq.gz" in blocker.evidence
