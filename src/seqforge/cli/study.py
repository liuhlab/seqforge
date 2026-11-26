"""`seqforge study`: a study's files, written from the bounding and reading passes, checked, and read.

``write-bounds`` writes each study's bounds file from the bounding pass's answers, as
``seqforge pass start study-bounds`` left them in its task, and ``write-reading`` each study's
reading file from the reading pass's, as ``seqforge pass start study-reading`` left them. Neither
writes over a file already there. ``check-bounds`` and ``check-reading``
each say whether one file is in the current format, and what is wrong with it when it is not.
``relations`` checks a reading the same way, then prints the relations between its samples. The
checks read the one file and nothing else: no store, no network, no model, and they write no file.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING

import typer

from ..study import bounds, reading
from ..study.claims import load
from ..study.relations import relations
from ._common import _emit, _StageOut
from .root import study_app

if TYPE_CHECKING:
    from labllm import Task, UnitKind


def _content(file: Path) -> tuple[object, list[dict[str, str]]]:
    """One file's content, or why it is not content: not YAML, or a key written twice.

    Exits 2 for a file that cannot be read, which a file that is not UTF-8 text is.
    """
    try:
        text = file.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        typer.echo(f"cannot read {file}: {exc}", err=True)
        raise typer.Exit(2) from exc
    return load(text)


def _verdict(file: Path, accepted: int, problems: list[dict[str, str]], **more: object) -> None:
    """Print the one JSON object every command here prints, and exit 0 if valid, 3 if rejected."""
    said = {"file": str(file), "format": accepted, "valid": not problems, "problems": problems}
    _emit(_StageOut({**said, **more}, 3 if problems else 0))


def _write(
    kind_name: str,
    write: Callable[[Task, UnitKind, Path, str], dict[str, dict[str, object]]],
    out: Path,
    name: str,
    task: Path | None,
) -> None:
    """Write each study's file from a pass's answers, and print how each study ended."""
    from ._kind_task import KindTask, for_kind

    def run(setup: KindTask) -> _StageOut:
        from ..passes.status import read_only_kind

        setup.require_task()
        try:
            with setup.read() as opened:
                done = write(opened, read_only_kind(kind_name), out, name)
        except ValueError as e:
            return _StageOut({"error": str(e)}, 2)
        said: dict[str, object] = {"kind": kind_name, "task": str(setup.folder), "out": str(out)}
        return _StageOut({**said, "studies": done}, 0)

    _emit(for_kind(kind_name, task=task, body=run))


@study_app.command("write-bounds")
def study_write_bounds(
    out: Path = typer.Option(
        ..., "--out", help="The folder of study folders, such as evals/standard-set."
    ),
    name: str = typer.Option(
        "bounds.yaml",
        "--name",
        help="The file's name in each study's folder; job-bounds.yaml for the standard set.",
    ),
    task: Path | None = typer.Option(
        None,
        "--task",
        help="The bounding pass's task folder. Default: seqforge/passes/<kind>/ here.",
    ),
) -> None:
    """Write ``<out>/<ID>/<name>`` from the bounding pass's answers, for each study that has none.

    A refused study gets no file; the JSON gives its reason. A file already there is never
    overwritten. It reads the task only, so it works with the gateway down. Exit 0 with one object
    per study; 2 for no task or a ``--name`` that is a path; 3 when a running pass holds the task.
    """
    from ..study.bounding import KIND_NAME, write_bounds

    _write(KIND_NAME, write_bounds, out, name, task)


@study_app.command("write-reading")
def study_write_reading(
    out: Path = typer.Option(
        ..., "--out", help="The folder of study folders, such as evals/standard-set."
    ),
    name: str = typer.Option(
        "reading.yaml",
        "--name",
        help="The file's name in each study's folder; job-reading.yaml for the standard set.",
    ),
    task: Path | None = typer.Option(
        None,
        "--task",
        help="The reading pass's task folder. Default: seqforge/passes/<kind>/ here.",
    ),
) -> None:
    """Write ``<out>/<ID>/<name>`` from the reading pass's answers, for each study that has none.

    The same as write-bounds, for the reading pass: a refused study gets no file and the JSON
    gives its reason, and a file already there is never overwritten, so the standard set's own
    readings stay as they are. Exit 0 with one object per study; 2 for no task or a ``--name`` that
    is a path; 3 when a running pass holds the task.
    """
    from ..study.reader import KIND_NAME, write_readings

    _write(KIND_NAME, write_readings, out, name, task)


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
