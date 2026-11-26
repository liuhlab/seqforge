"""The standard set as the repo keeps it, through ``seqforge standard-set list``."""

from __future__ import annotations

import json

import pytest
from typer.testing import CliRunner

from seqforge.cli import app
from seqforge.study.bounds import check_bounds
from seqforge.study.claims import load
from seqforge.study.reading import check_reading

runner = CliRunner()


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
