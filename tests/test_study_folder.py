"""A study folder through the one reader that opens it, and the standard set as the repo keeps it."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
import yaml
from typer.testing import CliRunner

from seqforge.cli import app
from seqforge.study.bounds import check_bounds
from seqforge.study.claims import load
from seqforge.study.folder import StudyFolderRefused, open_study_folder
from seqforge.study.reading import check_reading
from study_kit import READING, VALID

runner = CliRunner()


def _write(folder: Path, name: str, content: dict[str, Any]) -> None:
    folder.mkdir(parents=True, exist_ok=True)
    (folder / name).write_text(yaml.safe_dump(content, sort_keys=False))


@pytest.mark.parametrize(
    ("broken", "field"),
    [
        pytest.param(None, None, id="both files valid"),
        pytest.param("bounds.yaml", "series[0].p", id="the bounds fail their check"),
        pytest.param("reading.yaml", "samples.GSM1.biosample.p", id="the reading fails its check"),
    ],
)
def test_a_study_folder_opens_as_checked_bounds_and_reading(
    tmp_path: Path, broken: str | None, field: str | None
) -> None:
    """The reader hands back a plain study folder's bounds and reading as the checked objects later
    stages read, and refuses the folder, naming the file and the field, when either fails its format
    check: a compile or a report never reads a file the check rejects."""
    folder = tmp_path / "GSE1"
    bounds = yaml.safe_load(yaml.safe_dump(VALID))
    reading = yaml.safe_load(yaml.safe_dump(READING))
    if broken == "bounds.yaml":
        bounds["series"][0]["p"] = 2
    if broken == "reading.yaml":
        reading["samples"]["GSM1"]["biosample"]["p"] = 2
    _write(folder, "bounds.yaml", bounds)
    _write(folder, "reading.yaml", reading)

    if broken is None:
        opened = open_study_folder(folder)
        assert opened.bounds.reason == VALID["reason"]
        assert [claim.id for claim in opened.bounds.series] == ["GSE1", "GSE2"]
        assert opened.reading is not None
        assert set(opened.reading.rows()) == set(READING["samples"])
        assert opened.reading.factors["age"].baseline == "young"
        return
    with pytest.raises(StudyFolderRefused) as refused:
        open_study_folder(folder)
    assert list(refused.value.problems) == [broken]
    assert field in {p["field"] for p in refused.value.problems[broken]}


def test_a_standard_set_folder_opens_as_the_sets_own_files_never_the_jobs(tmp_path: Path) -> None:
    """A standard-set study holds the jobs' ``job-bounds.yaml`` and ``job-reading.yaml`` beside the
    set's own files. The reader shows the set's own: a study whose set has no reading yet has
    none, though the job wrote one, and a job's file that fails its check does not refuse it."""
    folder = tmp_path / "GSE1"
    _write(folder, "study.yaml", {"id": "GSE1", "status": "settled"})
    _write(folder, "bounds.yaml", VALID)
    _write(folder, "job-bounds.yaml", {**VALID, "reason": "The job's own line."})
    _write(folder, "job-reading.yaml", {**READING, "format": 1})

    opened = open_study_folder(folder)

    assert opened.study == {"id": "GSE1", "status": "settled"}
    assert opened.bounds.reason == VALID["reason"]
    assert opened.reading is None


@pytest.mark.repo
def test_the_standard_set_lists_fifty_studies_each_with_identifiers_reason_and_bounds() -> None:
    """The repo's own set, through the listing command: every study still in the pick is drawn, and
    nothing else is, in the pick's order. A study added or dropped without the pick, a direction split that drifted, a
    study file missing what later stages read, or a bounds file or a worked reading that is not in
    the current format goes red here."""
    from seqforge.study.folder import default_set_dir, pick_entries

    listed = {s["id"]: s for s in json.loads(runner.invoke(app, ["standard-set", "list"]).stdout)}
    picked = pick_entries(default_set_dir())

    assert list(listed) == list(picked), "the listing reads the pick, in the pick's order"
    by_direction = [s["direction"] for s in listed.values()]
    assert {d: by_direction.count(d) for d in set(by_direction)} == {
        "aging": 17,
        "neurodegeneration": 17,
        "c_elegans": 16,
    }
    for key, study in listed.items():
        assert study["reason_picked"], key
        assert study["records"]["series"], key
        bounds = study["bounds"]
        assert check_bounds(bounds) == [], key
        assert bounds["study"] == key
        assert {claim["id"] for claim in bounds["series"]} <= set(study["records"]["series"]), key

    readings = sorted(default_set_dir().glob("*/reading.yaml"))
    assert readings, "the worked readings are gone"
    for path in readings:
        reading, problems = load(path.read_text())
        assert problems == [], path.parent.name
        assert check_reading(reading) == [], path.parent.name
        assert reading["study"] == path.parent.name  # type: ignore[index]
        assert path.parent.name in listed
