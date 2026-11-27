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

from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import TYPE_CHECKING

import typer

from ..liulab_data import LiulabDataMissing
from ..study import bounds, reading
from ..study.claims import load
from ..study.folder import BOUNDS, JOB_BOUNDS, JOB_READING, READING
from ..study.relations import relations
from ._common import _emit, _StageOut
from .root import study_app

if TYPE_CHECKING:
    from labllm import TaskHolder, TaskReader


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


@contextmanager
def _needs_liulab_data() -> Iterator[None]:
    """Refuse the run, exit 2, when the reading check needs liulab-data and it is not there.

    The check judges each ontology id by liulab-data's vocabularies: without them it cannot say
    whether a file is valid, so it says nothing of the file.
    """
    try:
        yield
    except LiulabDataMissing as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(2) from exc


def _verdict(file: Path, accepted: int, problems: list[dict[str, str]], **more: object) -> None:
    """Print the one JSON object every command here prints, and exit 0 if valid, 3 if rejected."""
    said = {"file": str(file), "format": accepted, "valid": not problems, "problems": problems}
    _emit(_StageOut({**said, **more}, 3 if problems else 0))


def _write(
    kind_name: str,
    write: Callable[[TaskReader, str, Path, str], dict[str, dict[str, object]]],
    out: Path,
    name: str,
    task: Path | None,
) -> None:
    """Write each study's file from a pass's answers, and print how each study ended.

    A text the format check rejects is a refusal, as check-bounds and check-reading make it: the
    other files are still written, and the command exits 3. The task is only read, with no hold.
    A task a pass holds is refused, exit 3 with ``task_in_use``: a file once written is never
    replaced, so a write during a pass would keep an earlier pass's answer for each study the pass
    has not reached. The check sees only its own moment, so a pass that starts after it is not
    caught.
    """
    from labllm import TaskInUse

    from ._kind_task import KindTask, for_kind

    def run(setup: KindTask) -> _StageOut:
        try:
            with setup.read() as opened:
                if (holder := opened.holder) is not None:
                    raise TaskInUse(_held(setup.folder, holder))
                done = write(opened, kind_name, out, name)
        except (ValueError, LiulabDataMissing) as e:
            return _StageOut({"error": str(e)}, 2)
        said: dict[str, object] = {"kind": kind_name, "task": str(setup.folder), "out": str(out)}
        rejected = any("problems" in study for study in done.values())
        return _StageOut({**said, "studies": done}, 3 if rejected else 0)

    _emit(for_kind(kind_name, task=task, body=run))


def _held(folder: Path, holder: TaskHolder) -> str:
    """Why a write refuses a task a pass holds: who holds it, since when, and when to run it."""
    if holder.pid is None:
        who = "a process that is still opening it"
    else:
        who = f"{holder.user}'s process {holder.pid} on {holder.host}"
    if holder.since is not None:
        who += f", since {holder.since.isoformat()}"
    return (
        f"{folder} is held by {who}. A pass is running on it, and a file once written is never "
        "replaced: run the command again once the pass ends"
    )


@study_app.command("write-bounds")
def study_write_bounds(
    out: Path = typer.Option(
        ..., "--out", help="The folder of study folders, such as evals/standard-set."
    ),
    name: str = typer.Option(
        BOUNDS,
        "--name",
        help=f"The file's name in each study's folder; {JOB_BOUNDS} for the standard set.",
    ),
    task: Path | None = typer.Option(
        None,
        "--task",
        help="The bounding pass's task folder. Default: seqforge/passes/<kind>/ here.",
    ),
) -> None:
    """Write ``<out>/<ID>/<name>`` from the bounding pass's answers, for each study that has none.

    A refused study gets no file; the JSON gives its reason. The format check runs on the exact
    text to be written, and a text it rejects is not written. A file already there is never
    overwritten. It reads the task only, with no hold, so it works with the gateway down. While a
    pass holds the task it refuses and writes nothing, since it would keep an earlier pass's answer
    for each study the pass has not reached; run it again once the pass ends. Exit 0 with one
    object per study; 2 for no task, a task a pass stopped in the middle of a write, or a
    ``--name`` that is a path; 3 while a pass holds the task (``task_in_use``, naming the holder),
    when a pass's write kept the read waiting, or when the format check rejects a text, whose study
    the JSON names with its problems.
    """
    from ..study.bounding import KIND_NAME, write_bounds

    _write(KIND_NAME, write_bounds, out, name, task)


@study_app.command("write-reading")
def study_write_reading(
    out: Path = typer.Option(
        ..., "--out", help="The folder of study folders, such as evals/standard-set."
    ),
    name: str = typer.Option(
        READING,
        "--name",
        help=f"The file's name in each study's folder; {JOB_READING} for the standard set.",
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
    readings stay as they are. While a pass holds the task it refuses and writes nothing, for the
    reason write-bounds gives. Exit 0 with one object per study; 2 as for write-bounds, or for no
    liulab-data for the format check; 3 as for write-bounds.
    """
    from ..study.reader import KIND_NAME, write_readings

    _write(KIND_NAME, write_readings, out, name, task)


@study_app.command("check-bounds")
def study_check_bounds(
    file: Path = typer.Argument(..., help=f"A study's bounds file, such as <ID>/{BOUNDS}."),
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
    file: Path = typer.Argument(..., help=f"A study's reading file, such as <ID>/{READING}."),
) -> None:
    """Check one reading file against the current format.

    Prints the same JSON object as check-bounds, with the same exit codes, and exits 2 too, printing
    no verdict, when liulab-data is not installed: its vocabularies judge each ontology id. The
    check reads the file alone, so it does not look for a quote in the record it names.
    """
    data, problems = _content(file)
    with _needs_liulab_data():
        problems = problems or reading.check_reading(data)
    _verdict(file, reading.FORMAT, problems)


@study_app.command("relations")
def study_relations(
    file: Path = typer.Argument(..., help=f"A study's reading file, such as <ID>/{READING}."),
) -> None:
    """Print the relations between the samples of one valid reading.

    Checks the file as check-reading does, and rejects it the same way. For a valid file the JSON
    object also holds `relations`: replicates, contrasts, series, shared_biosample and
    signal_to_control, recomputed on every run and written nowhere.
    """
    data, problems = _content(file)
    read = None
    if not problems:
        with _needs_liulab_data():
            read, problems = reading.read_reading(data)
    if read is None or problems:
        _verdict(file, reading.FORMAT, problems)
        return
    _verdict(file, reading.FORMAT, [], relations=relations(read))
