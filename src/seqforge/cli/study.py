"""`seqforge study`: a study's files, written from the bounding and reading passes, checked, and read.

``write-bounds`` writes each study's bounds file from the bounding pass's answers, as
``seqforge pass start study-bounds`` left them in its task, and ``write-reading`` each study's
reading file from the reading pass's, as ``seqforge pass start study-reading`` left them. Neither
writes over a file already there. ``run`` chains the four: both passes under one ceiling, each
followed by its write. ``check-bounds`` and ``check-reading``
each say whether one file is in the current format, and what is wrong with it when it is not.
``relations`` checks a reading the same way, then prints the relations between its samples. The
checks read the one file and nothing else: no store, no network, no model, and they write no file.
"""

from __future__ import annotations

from collections.abc import Collection, Iterator
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
    from labllm import TaskHolder


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
    kind_name: str, out: Path, name: str, task: Path | None, keys: Collection[str] | None = None
) -> _StageOut:
    """Write each study's file from a pass's answers, with the kind's own writer; say how each ended.

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
                done = setup.kind.write(opened, kind_name, out, name, keys)
        except (ValueError, LiulabDataMissing) as e:
            return _StageOut({"error": str(e)}, 2)
        said: dict[str, object] = {"kind": kind_name, "task": str(setup.folder), "out": str(out)}
        rejected = any("problems" in study for study in done.values())
        return _StageOut({**said, "studies": done}, 3 if rejected else 0)

    return for_kind(kind_name, task=task, body=run)


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
    from ..study.bounding import KIND_NAME

    _emit(_write(KIND_NAME, out, name, task))


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
    from ..study.reader import KIND_NAME

    _emit(_write(KIND_NAME, out, name, task))


@study_app.command("run")
def study_run(
    studies: Path = typer.Option(
        ...,
        "--studies",
        help="The folder of study folders: it runs the studies its pick.yaml lists, or --keys.",
    ),
    ceiling_usd: float = typer.Option(
        ...,
        "--ceiling-usd",
        help="Required, no default: the most both passes together may spend, in dollars.",
    ),
    model: list[str] = typer.Option(
        [],
        "--model",
        metavar="ROLE=PROVIDER/NAME[,KEY=VALUE...]",
        help=(
            "The model for one role, repeatable, as pass start takes it. Each pass whose kind has "
            "the role takes it; a role neither kind has is refused."
        ),
    ),
    keys_file: Path | None = typer.Option(
        None, "--keys", help="Run exactly these studies, one key per line, in this order."
    ),
    units_at_once: int = typer.Option(32, "--units-at-once", min=1, help="Units run at once."),
    off_peak_only: bool = typer.Option(
        False, "--off-peak-only", help="Send calls only outside the model's peak hours."
    ),
) -> None:
    """Bound and read the studies of a folder: four existing verbs, in order, under one ceiling.

    It runs ``pass start study-bounds``, ``study write-bounds``, ``pass start study-reading`` and
    ``study write-reading``, each with its default task here and the folder's own ``bounds.yaml``
    and ``reading.yaml``, so the reading pass reads the bounds just written. Bounding spends first,
    and reading gets what is left of ``--ceiling-usd``. It adds no check of its own. Prints one
    JSON object, each step's own output in ``steps``. Exit 0 when every step did; otherwise the
    first step that did not stops the chain, and its exit is the verb's: 2 for a setup it cannot
    use, 3 at the ceiling, a stopped pass, a held task or a file the format check rejects. Rerun
    the same command to resume: answered units replay from the gateway's cache at no cost.
    """
    _emit(
        _study_run(
            studies=studies,
            ceiling_usd=ceiling_usd,
            model=model,
            keys_file=keys_file,
            units_at_once=units_at_once,
            off_peak_only=off_peak_only,
        )
    )


def _study_run(
    *,
    studies: Path,
    ceiling_usd: float,
    model: list[str],
    keys_file: Path | None,
    units_at_once: int,
    off_peak_only: bool,
) -> _StageOut:
    """The body of ``study run``: every setup refusal first, then each pass and its write."""
    from ..gateway import check_ceiling
    from ..passes import SetupRefused
    from ..passes.kinds import kind_named
    from ..passes.model_text import parse_model, parse_models
    from ..study import bounding, reader
    from .passes import _pass_start, _read_keys

    chain = (
        (bounding.KIND_NAME, "write-bounds", BOUNDS),
        (reader.KIND_NAME, "write-reading", READING),
    )
    said: dict[str, object] = {"studies": str(studies), "ceiling_usd": ceiling_usd}
    try:
        check_ceiling(ceiling_usd)
        kinds = [kind_named(name) for name, _, _ in chain]
        # Every text is read now, so a text that each pass would refuse is refused before any spend.
        parse_models(model)
        roles = {text: parse_model(text)[0] for text in model}
        known = {role for kind in kinds for role in kind.default_models}
        if extra := sorted(set(roles.values()) - known):
            raise SetupRefused(
                f"no study job has role {', '.join(extra)}; the roles are {', '.join(sorted(known))}"
            )
    except SetupRefused as exc:
        return _StageOut({**said, "error": str(exc), "spent_usd": 0.0, "steps": []}, 2)

    steps: list[dict[str, object]] = []
    spent = 0.0
    code = 0
    for kind, (_, write_verb, file_name) in zip(kinds, chain, strict=True):
        left = max(0.0, ceiling_usd - spent)
        ran = _pass_start(
            name=kind.name,
            ceiling_usd=left,
            model=[text for text, role in roles.items() if role in kind.default_models],
            keys_file=keys_file,
            studies=studies,
            only=None,
            units_at_once=units_at_once,
            off_peak_only=off_peak_only,
            label=None,
            task=None,
        )
        steps.append(_step(f"pass start {kind.name}", ran, ceiling_usd=left))
        # A pass that stopped spent too; a setup it refused, which prints a line, spent nothing.
        cost = ran.payload.get("spent_usd") if isinstance(ran.payload, dict) else None
        spent += cost if isinstance(cost, int | float) else 0.0
        if code := ran.code:
            break
        # The task is shared with every earlier chain run here, so the write takes this chain's
        # studies only: those the pass just ran, which its start has already checked can be read.
        chosen = _read_keys(keys_file) if keys_file is not None else kind.default_keys(studies)
        wrote = _write(kind.name, studies, file_name, None, list(chosen))
        steps.append(_step(f"study {write_verb}", wrote))
        if code := wrote.code:
            break
    return _StageOut({**said, "spent_usd": spent, "steps": steps}, code)


def _step(verb: str, out: _StageOut, **more: object) -> dict[str, object]:
    """One step of ``study run``: the verb it ran, its exit, and what the verb printed."""
    body = {"error": out.payload} if isinstance(out.payload, str) else out.payload
    return {"verb": verb, "exit": out.code, **more, **body}


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
