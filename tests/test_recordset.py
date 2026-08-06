"""Tests for ``recordset.py`` — the loader both dialects go through, and the draft it can read back.

The subject is a **gate**, so most of these are about what does NOT load: an attribute a human typed
would be granted the standing an archive's typed slot has — no quote, no span, nothing that greps
back — permanently inside the dataset hash, and nothing downstream notices. The archive path is
tested for the opposite property: a cache ``io records`` wrote must load unchanged.

The draft's no-op guarantee and its CLI refusals live in ``tests/test_cli.py``; what a loaded set
does to a resolution lives in ``tests/test_records.py``.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import pytest
import yaml

from seqforge.models.blocker import BlockerCode
from seqforge.models.records import (
    ArchiveRecord,
    ArchiveRecordSet,
    FreeText,
    RecordAttribute,
    SubmittedFile,
)
from seqforge.recordset import RecordSetError, draft_record_set, load_record_set

# ================================================================================================
# helpers — local rather than in conftest: nothing outside this file writes a record set by hand
# ================================================================================================


def _write(tmp_path: Path, payload: dict[str, Any], name: str = "records.yaml") -> Path:
    path = tmp_path / name
    path.write_text(yaml.safe_dump(payload, sort_keys=False))
    return path


#: A minimal legal user set: two runs, each its own sample, both accounted for. The tests that refuse
#: something start from this and add exactly the defect they are about, so a refusal cannot come from
#: an unrelated flaw in the fixture.
def _two_runs() -> dict[str, Any]:
    return {
        "source": "user",
        "query": "plateA",
        "records": [
            {
                "level": "run",
                "id": "plateA_S1",
                "filenames": ["plateA_S1_L001_R1_001.fastq.gz", "plateA_S1_L001_R2_001.fastq.gz"],
            },
            {
                "level": "run",
                "id": "plateA_S3",
                "filenames": ["plateA_S3_L001_R1_001.fastq.gz", "plateA_S3_L001_R2_001.fastq.gz"],
            },
        ],
    }


def _touch_fastqs(directory: Path, names: list[str]) -> list[str]:
    directory.mkdir(parents=True, exist_ok=True)
    for name in names:
        (directory / name).write_bytes(b"")
    return names


def _comment_lines(text: str) -> list[str]:
    return [line.strip() for line in text.splitlines() if line.strip().startswith("#")]


# ================================================================================================
# the user dialect: what it refuses
# ================================================================================================


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("attributes", [{"name": "strain", "value": "CQ758"}]),
        ("free_text", [{"label": "note", "text": "daf-2 replicate 3"}]),
    ],
)
def test_a_fact_typed_into_a_user_set_is_refused_and_the_same_set_without_it_loads(
    tmp_path: Path, key: str, value: list[dict[str, str]]
) -> None:
    """Prose is refused beside the typed slot: harvest's own gate is `has_prose`, so a set carrying
    free text is visible to it, and the quote would grep back against a file written to be grepped.

    Both halves in one test on purpose: an over-strict loader that refused the clean set too would
    pass a refusal-only test while making the feature unusable.
    """
    payload = _two_runs()
    clean = load_record_set(_write(tmp_path, payload, "clean.yaml"))
    assert [r.accession for r in clean.records] == ["plateA_S1", "plateA_S3"]

    payload["records"][0][key] = value
    with pytest.raises(RecordSetError) as caught:
        load_record_set(_write(tmp_path, payload, "dirty.yaml"))

    (blocker,) = caught.value.blockers
    assert blocker.code is BlockerCode.RECORD_SET_INVALID
    assert blocker.evidence == [key]
    assert "harvest" in blocker.remedy
    assert caught.value.report.ok is False
    assert caught.value.report.blockers == caught.value.blockers


@pytest.mark.parametrize("level", ["experiment", "project"])
def test_an_archive_level_is_refused(tmp_path: Path, level: str) -> None:
    """Two levels, `run` and `sample`, because those are the two the join reads."""
    payload = _two_runs()
    payload["records"].append({"level": level, "id": f"a_{level}"})
    with pytest.raises(RecordSetError) as caught:
        load_record_set(_write(tmp_path, payload))

    (blocker,) = caught.value.blockers
    assert blocker.id == "blk-record-set-level"
    assert level in blocker.message
    assert "`run` and `sample`" in blocker.message


def test_a_dangling_parent_is_refused_by_the_id_it_names(tmp_path: Path) -> None:
    """A typo'd parent is the failure mode with no downstream symptom: the walk up stops at the run,
    the run's own id becomes the sample id, and the library quietly splits in two at exit 0."""
    payload = _two_runs()
    payload["records"].append({"level": "sample", "id": "plateA"})
    payload["records"][0]["parent"] = "plateA"
    payload["records"][1]["parent"] = "platA"  # the typo
    with pytest.raises(RecordSetError) as caught:
        load_record_set(_write(tmp_path, payload))

    (blocker,) = caught.value.blockers
    assert blocker.id == "blk-record-set-parent"
    assert "platA" in blocker.message
    assert "plateA" in blocker.evidence


def test_a_run_parented_to_another_run_is_refused(tmp_path: Path) -> None:
    """`parent` names a sample. Pointed at a run it resolves, and then resolves to no sample."""
    payload = _two_runs()
    payload["records"][1]["parent"] = "plateA_S1"
    with pytest.raises(RecordSetError) as caught:
        load_record_set(_write(tmp_path, payload))

    (blocker,) = caught.value.blockers
    assert blocker.id == "blk-record-set-parent"
    assert "a run and not a sample" in blocker.message


def test_a_duplicate_id_is_refused(tmp_path: Path) -> None:
    """Two records answering to one id: `parent` reaches whichever comes first, silently."""
    payload = _two_runs()
    payload["records"][1]["id"] = "plateA_S1"
    with pytest.raises(RecordSetError) as caught:
        load_record_set(_write(tmp_path, payload))

    assert [b.id for b in caught.value.blockers] == ["blk-record-set-id"]
    assert "declared twice" in caught.value.blockers[0].message


#: Each entry is a consumer of the id, not a taste: the units delimiters, then path components, then
#: argument boundaries where the workflow interpolates `{sample}` unquoted, then an option lead.
_UNTYPEABLE_IDS = ["plate\t7", "plate\n7", "..", "../etc", "plates/7", "plate 7", "a;b", "-plate"]


@pytest.mark.parametrize("ident", _UNTYPEABLE_IDS)
def test_an_id_that_could_not_be_a_sample_id_is_refused(tmp_path: Path, ident: str) -> None:
    """A run with no sample above it is its own sample, so any id here reaches `ResolvedSample`, a
    plain `str` — and from there a units cell, a results directory and an unquoted shell word.

    Refused where the human's input arrives, because by the time a tab has split a units row the
    manifest is written, content-addressed and permanent.
    """
    payload = _two_runs()
    payload["records"][0]["id"] = ident
    with pytest.raises(RecordSetError) as caught:
        load_record_set(_write(tmp_path, payload))

    (blocker,) = caught.value.blockers
    assert blocker.id == "blk-record-set-id"
    assert blocker.evidence == [ident], "the refusal names the string, not just the rule"
    assert "letters, digits" in blocker.remedy, "and says what IS allowed"

    # A rule the author has to apply themselves is a rule retyped wrong once, so the remedy carries
    # a spelling to paste — and that spelling has to be one this loader accepts. A remedy refused by
    # the thing that printed it is worse than no remedy at all, and nothing else would notice.
    suggested = re.search(r"`([^`]+)` is the nearest", blocker.remedy)
    assert suggested is not None, blocker.remedy
    payload["records"][0]["id"] = suggested.group(1)
    assert load_record_set(_write(tmp_path, payload, "fixed.yaml")).at("run")


@pytest.mark.parametrize("ident", ["plateA_S1", "plate.7", "7plate", "a", "lib-01_S3.rep2"])
def test_the_ids_a_human_would_actually_type_still_load(tmp_path: Path, ident: str) -> None:
    """The other half of the rule, and the half an over-tight allowlist would break silently.

    `plateA_S1` is what `run_key` itself produces, so a rule refusing it would refuse the draft
    `records new` writes over ordinary Illumina filenames — the one file this loader must take.
    """
    payload = _two_runs()
    payload["records"][0]["id"] = ident
    assert [r.accession for r in load_record_set(_write(tmp_path, payload)).records] == [
        ident,
        "plateA_S3",
    ]


def test_an_unknown_key_on_a_record_is_refused(tmp_path: Path) -> None:
    payload = _two_runs()
    payload["records"][0]["sample_id"] = "plateA"
    with pytest.raises(RecordSetError) as caught:
        load_record_set(_write(tmp_path, payload))

    (blocker,) = caught.value.blockers
    assert blocker.id == "blk-record-set-unknown-key"
    assert blocker.evidence == ["sample_id"]


def test_an_unknown_key_on_the_set_is_refused(tmp_path: Path) -> None:
    """Including `io_version`: it is the transcriber's stamp, and a hand-written set was not fetched."""
    payload = _two_runs()
    payload["io_version"] = "2026.7.1"
    with pytest.raises(RecordSetError) as caught:
        load_record_set(_write(tmp_path, payload))

    (blocker,) = caught.value.blockers
    assert blocker.evidence == ["io_version"]
    assert "io_version" in blocker.remedy


def test_a_run_with_no_filenames_and_a_sample_with_them_are_both_refused(tmp_path: Path) -> None:
    """The files hang off the run; the sample is what several runs point at."""
    payload = _two_runs()
    del payload["records"][0]["filenames"]
    payload["records"].append(
        {"level": "sample", "id": "plateA", "filenames": ["plateA_S1_L001_R1_001.fastq.gz"]}
    )
    with pytest.raises(RecordSetError) as caught:
        load_record_set(_write(tmp_path, payload))

    assert [b.id for b in caught.value.blockers] == [
        "blk-record-set-filenames",
        "blk-record-set-filenames",
    ]
    assert "declares no filenames" in caught.value.blockers[0].message
    assert "reached THROUGH its runs" in caught.value.blockers[1].message


def test_one_file_declared_by_two_runs_is_refused(tmp_path: Path) -> None:
    """The join keeps whichever it reads last, so the second claim would move the file's sample."""
    payload = _two_runs()
    payload["records"][1]["filenames"].append("plateA_S1_L001_R1_001.fastq.gz")
    with pytest.raises(RecordSetError) as caught:
        load_record_set(_write(tmp_path, payload))

    (blocker,) = caught.value.blockers
    assert "plateA_S1_L001_R1_001.fastq.gz" in blocker.message


def test_every_refusal_names_something_to_type(tmp_path: Path) -> None:
    """A file wrong in four ways is refused four times in one pass, each with its own remedy —
    the difference between one edit and four round-trips."""
    payload = _two_runs()
    payload["records"][0]["attributes"] = [{"name": "strain", "value": "CQ758"}]
    payload["records"][0]["parent"] = "nobody"
    payload["records"][1]["level"] = "experiment"
    payload["nonsense"] = True
    with pytest.raises(RecordSetError) as caught:
        load_record_set(_write(tmp_path, payload))

    assert len(caught.value.blockers) == 4
    assert all(b.remedy.strip() for b in caught.value.blockers)
    assert {b.code for b in caught.value.blockers} == {BlockerCode.RECORD_SET_INVALID}


def test_a_file_that_is_not_a_mapping_refuses_rather_than_raising(tmp_path: Path) -> None:
    path = tmp_path / "records.yaml"
    path.write_text("- level: run\n  id: x\n")
    with pytest.raises(RecordSetError) as caught:
        load_record_set(path)

    assert caught.value.blockers[0].id == "blk-record-set-unparsable"


def test_a_missing_file_refuses_rather_than_raising(tmp_path: Path) -> None:
    with pytest.raises(RecordSetError) as caught:
        load_record_set(tmp_path / "nope.yaml")

    assert caught.value.blockers[0].id == "blk-record-set-unreadable"


def test_a_file_that_forgot_source_user_is_read_as_an_archive_and_refuses_rather_than_raising(
    tmp_path: Path,
) -> None:
    """`source` and not the extension picks the dialect, so a file missing it is read as a transcript
    it is not — refused with the pydantic failures as evidence and the user spelling as the remedy."""
    payload = _two_runs()
    del payload["source"]
    with pytest.raises(RecordSetError) as caught:
        load_record_set(_write(tmp_path, payload))

    (blocker,) = caught.value.blockers
    assert blocker.code is BlockerCode.RECORD_SET_INVALID
    assert blocker.evidence, "one readable line per problem, never a traceback"
    assert "source: user" in blocker.remedy


# ================================================================================================
# the user dialect: what it accepts, and what comes back
# ================================================================================================


def test_two_runs_under_one_sample_load_with_their_filenames(tmp_path: Path) -> None:
    """The shape the whole feature exists for: one library sequenced twice, declared as one sample."""
    payload = _two_runs()
    payload["records"].append({"level": "sample", "id": "plateA"})
    payload["records"][0]["parent"] = "plateA"
    payload["records"][1]["parent"] = "plateA"

    loaded = load_record_set(_write(tmp_path, payload))

    assert loaded.source == "user"
    assert loaded.query == "plateA"
    assert loaded.io_version is None
    assert [r.accession for r in loaded.at("run")] == ["plateA_S1", "plateA_S3"]
    assert loaded.at("run")[0].filenames == [
        "plateA_S1_L001_R1_001.fastq.gz",
        "plateA_S1_L001_R2_001.fastq.gz",
    ]
    assert all(r.attributes == [] and r.free_text == [] for r in loaded.records)
    for run in loaded.at("run"):
        ancestor = loaded.ancestor(run, "sample")
        assert ancestor is not None and ancestor.accession == "plateA"


def test_the_query_defaults_to_the_files_own_stem(tmp_path: Path) -> None:
    """There is no accession a human typed, so the file names itself."""
    payload = _two_runs()
    del payload["query"]
    loaded = load_record_set(_write(tmp_path, payload, "worm-plate-7.yaml"))
    assert loaded.query == "worm-plate-7"


def test_json_and_yaml_are_one_code_path(tmp_path: Path) -> None:
    """YAML is a superset of JSON, so the same bytes in either spelling load identically."""
    payload = _two_runs()
    as_json = tmp_path / "records.json"
    as_json.write_text(json.dumps(payload, indent=2))
    assert load_record_set(as_json).records == load_record_set(_write(tmp_path, payload)).records


# ================================================================================================
# the archive dialect: unchanged, and it must stay that way
# ================================================================================================


def test_an_archive_cache_still_loads_unchanged(tmp_path: Path) -> None:
    """An `io records` cache carries exactly what the strict dialect refuses — attributes, prose,
    four levels, a writer stamp — and a round trip is the assertion: a dropped key is an inequality.
    """
    original = ArchiveRecordSet(
        source="ncbi-sra+biosample",
        query="PRJNA1027859",
        io_version="2026.7.1",
        records=[
            ArchiveRecord(level="project", accession="PRJNA1027859"),
            ArchiveRecord(
                level="sample",
                accession="SAMN37812345",
                parent="PRJNA1027859",
                attributes=[
                    RecordAttribute(name="strain", value="CQ758", harmonized=True),
                    RecordAttribute(name="odd_tag", value="enhanced beads", raw_name="Odd Tag"),
                ],
                free_text=[FreeText(label="sample_alias", text="daf-2 R3")],
            ),
            ArchiveRecord(
                level="run",
                accession="SRR28716558",
                parent="SAMN37812345",
                submitted_files=[
                    SubmittedFile(
                        filename="SRR28716558_1.fastq.gz",
                        md5="d41d8cd98f00b204e9800998ecf8427e",
                        size_bytes=90210,
                        uri="s3://sra-pub-src-1/SRR28716558/x_1.fastq.gz",
                    )
                ],
            ),
        ],
    )
    path = tmp_path / "PRJNA1027859.json"
    path.write_text(json.dumps(original.model_dump(mode="json"), indent=2))

    assert load_record_set(path) == original


def test_an_archive_cache_carrying_an_unknown_key_still_loads(tmp_path: Path) -> None:
    """Tolerance, deliberately kept: these files are ours, already on disk, and cannot be re-typed."""
    payload = {
        "source": "ncbi-sra+biosample",
        "query": "PRJNA1027859",
        "records": [{"level": "run", "accession": "SRR28716558", "invented_later": 3}],
    }
    loaded = load_record_set(_write(tmp_path, payload, "cache.json"))
    assert [r.accession for r in loaded.records] == ["SRR28716558"]


# ================================================================================================
# the draft
# ================================================================================================


def test_the_draft_declares_the_fastq_files_and_nothing_else(tmp_path: Path) -> None:
    """A non-FASTQ beside the reads is not a file to declare, and the run keeps every name it wrote."""
    names = _touch_fastqs(
        tmp_path / "fastq",
        [
            "plateA_S1_L001_R1_001.fastq.gz",
            "plateA_S1_L002_R1_001.fastq.gz",
            "plateA_S3_L001_R1_001.fastq.gz",
            "notes.txt",
        ],
    )
    text = draft_record_set(tmp_path / "fastq")
    (tmp_path / "records.yaml").write_text(text)
    loaded = load_record_set(tmp_path / "records.yaml")

    assert "notes.txt" not in text
    assert [r.accession for r in loaded.records] == ["plateA_S1", "plateA_S3"]
    assert loaded.records[0].filenames == sorted(n for n in names if n.startswith("plateA_S1"))


@pytest.mark.parametrize(
    ("names", "expected"),
    [
        pytest.param(
            [
                "plateA_S1_L001_R1_001.fastq.gz",
                "plateA_S1_L001_R2_001.fastq.gz",
                "plateA_S3_L001_R1_001.fastq.gz",
                "plateA_S3_L001_R2_001.fastq.gz",
            ],
            ["plateA_S1", "plateA_S3", "`S1`", "`S3`", "sample-sheet"],
            id="sample-sheet",
        ),
        pytest.param(
            [
                "lib7_HJ7L2BGXX_S1_L001_R1_001.fastq.gz",
                "lib7_HJ7L2BGXX_S1_L001_R2_001.fastq.gz",
                "lib7_HVFNLDSX2_S1_L001_R1_001.fastq.gz",
                "lib7_HVFNLDSX2_S1_L001_R2_001.fastq.gz",
            ],
            ["flowcell", "`HJ7L2BGXX`", "`HVFNLDSX2`"],
            id="flowcell",
        ),
        pytest.param(
            ["wt_rep1_R1.fastq.gz", "wt_rep1_R2.fastq.gz", "daf2_rep1_R1.fastq.gz"],
            ["No two runs"],
            id="neither",
        ),
    ],
)
def test_the_draft_names_the_decision_it_will_not_take(
    tmp_path: Path, names: list[str], expected: list[str]
) -> None:
    """Two shapes compile as two samples at partial depth at exit 0 and no filename tells them apart;
    a scan that ran and found neither says so, because that looks identical to no scan at all."""
    _touch_fastqs(tmp_path / "fastq", names)
    # Joined, because the note is wrapped prose and which word lands on which line is not the point.
    # The header names no run key, no `S<n>` and no flowcell, so every hit below comes from a note.
    comments = " ".join(_comment_lines(draft_record_set(tmp_path / "fastq")))

    assert all(want in comments for want in expected), comments


def test_a_directory_whose_run_keys_could_not_be_sample_ids_refuses_too(tmp_path: Path) -> None:
    """The id rule held at the other end: what this verb writes must be what the loader takes.

    `records new -o` reads its own draft back, so an unloadable draft would be reported as a bug in
    seqforge. Refused here instead, naming the runs, so "a draft always loads" holds by construction.
    """
    directory = tmp_path / "odd"
    _touch_fastqs(directory, ["-weird_S1_R1_001.fastq.gz", "-weird_S1_R2_001.fastq.gz"])

    with pytest.raises(RecordSetError) as caught:
        draft_record_set(directory)

    (blocker,) = caught.value.blockers
    assert blocker.id == "blk-record-set-id"
    assert blocker.evidence == ["-weird_S1"], "the refusal names the run key, not the whole listing"
    assert "Rename" in blocker.remedy
