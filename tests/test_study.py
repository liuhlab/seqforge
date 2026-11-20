"""The format check of a study's bounds file, through ``seqforge study check-bounds``.

Every file here is written by hand in the test. The repo's own 50 bounds files are checked where the
standard set is listed, in ``test_standard_set.py``.
"""

from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any

import pytest
import yaml
from typer.testing import CliRunner

from seqforge.cli import app

runner = CliRunner()

#: One claim of each backing: a quoted Series, an inferred Series, and a left-out paper marked unknown.
VALID: dict[str, Any] = {
    "format": 1,
    "study": "GSE1",
    "models": {"bounds": "test/large", "probabilities": "test/small"},
    "series": [
        {"id": "GSE1", "quoted": [1, 2], "p": 0.9},
        {"id": "GSE2", "inferred": "Same samples, later run.", "see": [2], "p": 1},
    ],
    "papers": ["pmid:11"],
    "left_out": [{"id": "pmid:22", "unknown": "Nothing read says who made it", "p": None}],
    "reason": "One paper and the two Series it deposited.",
    "passages": {
        1: {"record": "GSE1", "field": "Series_summary", "quote": "a quote"},
        2: {"paper": "pmid:11", "section": "Methods", "quote": "another"},
    },
}


def _check(tmp_path: Path, bounds: dict[str, Any]) -> tuple[int, dict[str, Any]]:
    path = tmp_path / "bounds.yaml"
    path.write_text(yaml.safe_dump(bounds, sort_keys=False))
    result = runner.invoke(app, ["study", "check-bounds", str(path)])
    return result.exit_code, json.loads(result.stdout)


def _without(*path: str | int) -> Any:
    def change(bounds: dict[str, Any]) -> None:
        *way, last = path
        at: Any = bounds
        for step in way:
            at = at[step]
        del at[last]

    return change


def _with(value: Any, *path: str | int) -> Any:
    def change(bounds: dict[str, Any]) -> None:
        *way, last = path
        at: Any = bounds
        for step in way:
            at = at[step]
        at[last] = value

    return change


def test_a_bounds_file_in_the_current_format_is_accepted(tmp_path: Path) -> None:
    """A claim of each backing, a passage from a record and one from a paper, a null probability."""
    code, said = _check(tmp_path, VALID)

    assert (code, said["valid"], said["problems"]) == (0, True, [])


@pytest.mark.parametrize(
    ("change", "field", "reason"),
    [
        pytest.param(_with(2, "format"), "format", "expected 1", id="another format number"),
        pytest.param(_without("format"), "format", "expected 1", id="no format number"),
        pytest.param(
            _with({}, "split_samples"), "split_samples", "not a field", id="a sample split"
        ),
        pytest.param(
            _with("no label applies", "left_out", 0, "na"),
            "left_out[0].na",
            "not a field",
            id="a mark the bounds do not have",
        ),
        pytest.param(
            _without("series", 0, "quoted"), "series[0]", "this one has none", id="no backing mark"
        ),
        pytest.param(
            _with("Worked out.", "series", 0, "inferred"),
            "series[0]",
            "this one has quoted and inferred",
            id="two backing marks",
        ),
        pytest.param(
            _with([1, 7], "series", 0, "quoted"),
            "series[0].quoted",
            "passage 7 does not exist",
            id="a quoted claim points to a passage that is not there",
        ),
        pytest.param(
            _with([], "series", 0, "quoted"),
            "series[0].quoted",
            "at least one passage",
            id="a quoted claim points to no passage",
        ),
        pytest.param(
            _with([7], "series", 1, "see"),
            "series[1].see",
            "passage 7 does not exist",
            id="an inferred claim rests on a passage that is not there",
        ),
        pytest.param(
            _with(" ", "series", 1, "inferred"),
            "series[1].inferred",
            "says how",
            id="inferred without saying how",
        ),
        pytest.param(
            _with(1.5, "left_out", 0, "p"),
            "left_out[0].p",
            "less than or equal to 1",
            id="a probability above one",
        ),
        pytest.param(
            _with(None, "models", "probabilities"),
            "series[0].p",
            "models.probabilities",
            id="a probability no model is named for",
        ),
        pytest.param(
            _with("GSE2", "left_out", 0, "id"),
            "left_out[0].id",
            "already placed at series[1].id",
            id="a record both kept and left out",
        ),
        pytest.param(
            _without("passages", 1, "field"),
            "passages[1]",
            "a record and its field",
            id="a passage that does not say where it was read",
        ),
    ],
)
def test_a_broken_bounds_file_is_rejected_with_the_field_and_the_reason(
    tmp_path: Path, change: Any, field: str, reason: str
) -> None:
    """One format at a time, no sample split, and no claim without its backing: a file that breaks
    the written format exits as a refusal, and each problem names where it is and why."""
    bounds = copy.deepcopy(VALID)
    change(bounds)

    code, said = _check(tmp_path, bounds)

    assert (code, said["valid"]) == (3, False)
    assert any(p["field"] == field and reason in p["reason"] for p in said["problems"]), said
