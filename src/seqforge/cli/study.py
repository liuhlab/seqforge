"""`seqforge study`: the format checks of a study's files, and what a valid reading implies.

``check-bounds`` and ``check-reading`` each say whether one file is in the current format, and what
is wrong with it when it is not. ``relations`` checks a reading the same way, then prints the
relations between its samples. Each reads the one file and nothing else: no store, no network, no
model. None writes a file.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import typer
import yaml

from ..study import bounds, reading
from ..study.relations import relations
from ._common import _emit, _StageOut
from .root import study_app


def _content(file: Path) -> tuple[Any, list[dict[str, str]]]:
    """One file's content as loaded, or the problem that it is not YAML. Exits 2 if unreadable."""
    try:
        text = file.read_text()
    except OSError as exc:
        typer.echo(f"cannot read {file}: {exc}", err=True)
        raise typer.Exit(2) from exc
    try:
        return yaml.safe_load(text), []
    except yaml.YAMLError as exc:
        return None, [{"field": "(file)", "reason": f"not YAML: {' '.join(str(exc).split())}"}]


def _verdict(file: Path, accepted: int, problems: list[dict[str, str]], **more: object) -> None:
    """Print the one JSON object every command here prints, and exit 0 if valid, 3 if rejected."""
    said = {"file": str(file), "format": accepted, "valid": not problems, "problems": problems}
    _emit(_StageOut({**said, **more}, 3 if problems else 0))


@study_app.command("check-bounds")
def study_check_bounds(
    file: Path = typer.Argument(..., help="A study's bounds file, such as <ID>/bounds.yaml."),
) -> None:
    """Check one bounds file against the current format.

    Prints one JSON object: the file, the format number the check accepts, whether the file is
    valid, and its problems, each naming a field and the reason. Exit 0 for a valid file; 3 for a
    rejected one; 2 for a file that cannot be read.
    """
    data, problems = _content(file)
    _verdict(file, bounds.FORMAT, problems or bounds.check_bounds(data))


@study_app.command("check-reading")
def study_check_reading(
    file: Path = typer.Argument(..., help="A study's reading file, such as <ID>/reading.yaml."),
) -> None:
    """Check one reading file against the current format.

    Prints the same JSON object as check-bounds, with the same exit codes. The check reads the
    file alone, so it does not look for a quote in the record it names.
    """
    data, problems = _content(file)
    _verdict(file, reading.FORMAT, problems or reading.check_reading(data))


@study_app.command("relations")
def study_relations(
    file: Path = typer.Argument(..., help="A study's reading file, such as <ID>/reading.yaml."),
) -> None:
    """Print the relations between the samples of one valid reading.

    Checks the file as check-reading does, and rejects it the same way. For a valid file the JSON
    object also holds `relations`: replicates, contrasts, series, shared_biosample and
    signal_to_control, recomputed on every run and written nowhere.
    """
    data, problems = _content(file)
    read = None
    if not problems:
        read, problems = reading.read_reading(data)
    if read is None or problems:
        _verdict(file, reading.FORMAT, problems)
        return
    _verdict(file, reading.FORMAT, [], relations=relations(read))
