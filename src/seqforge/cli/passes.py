"""`seqforge pass`: start a pass of a seqforge unit kind through the lab gateway, and read its task.

The pass itself is liulab-llms's. This module turns the options into what it takes -- a kind, models
by role, keys in order, a dollar ceiling, a task -- and its result into one JSON object on stdout
and an exit code: 0 when the pass reached the end of its keys whatever the counts, 2 for a setup it
cannot use, 3 when it stopped or another process holds the task. Rerunning the same command
resumes: every unit already answered replays from the gateway's cache at no cost.
"""

from __future__ import annotations

from contextlib import closing
from pathlib import Path

import typer

from ..passes import SetupRefused
from ._common import _emit, _StageOut
from ._kind_task import KindTask, for_kind
from .root import pass_app

# liulab-llms is imported where it is used: it costs a few tenths of a second, which every other
# verb's start would otherwise pay.


@pass_app.command("start")
def pass_start(
    kind: str = typer.Argument(..., help="The seqforge kind to run."),
    ceiling_usd: float = typer.Option(
        ...,
        "--ceiling-usd",
        help="Required, no default: the most this pass may spend, in dollars.",
    ),
    model: list[str] = typer.Option(
        [],
        "--model",
        metavar="ROLE=PROVIDER/NAME[,KEY=VALUE...]",
        help=(
            "The model for one role, repeatable, e.g. writer=deepseek/deepseek-v4-flash,thinking=off. "
            "Keys: thinking (on/off), effort, temperature, max-output-tokens. A role not named takes "
            "the kind's default; a named one takes exactly what is written."
        ),
    ),
    keys_file: Path | None = typer.Option(
        None, "--keys", help="Run exactly these units, one key per line, in this order."
    ),
    studies: Path | None = typer.Option(
        None,
        "--studies",
        help=(
            "The folder of study folders the pass works on; with no --keys, it runs the studies "
            "the folder's pick.yaml lists. Default: the standard set, evals/standard-set."
        ),
    ),
    only: str | None = typer.Option(
        None,
        "--only",
        help=(
            "Run only the units whose current ending is one of these, comma-separated, e.g. "
            "unsettled,refused. A unit whose review still holds counts with the review's ending."
        ),
    ),
    units_at_once: int = typer.Option(32, "--units-at-once", min=1, help="Units run at once."),
    off_peak_only: bool = typer.Option(
        False, "--off-peak-only", help="Send calls only outside the model's peak hours."
    ),
    label: str | None = typer.Option(
        None, "--label", help="The pass's label in the gateway's record. Default: the kind's name."
    ),
    task: Path | None = typer.Option(
        None, "--task", help="The task's folder. Default: seqforge/passes/<kind>/ here."
    ),
) -> None:
    """Run one pass of a seqforge kind over its units, and print its JSON.

    The units are the kind's default keys, the studies the pick of the `--studies` folder lists, or
    `--keys FILE`, or `--only ENDINGS`. Exit 0 when the pass reached the end of its keys, whatever
    its counts; 3 when it stopped -- at its ceiling, a provider's balance or cap, a provider or the
    gateway unavailable -- or another process holds the task; 2 for a setup it cannot use, a model
    name the gateway rejects or a folder with no pick and no `--keys` included. Rerun the same
    command to resume: units already answered replay from the gateway's cache at no cost. Progress
    goes to stderr; stdout carries only JSON.
    """
    _emit(
        _pass_start(
            name=kind,
            ceiling_usd=ceiling_usd,
            model=model,
            keys_file=keys_file,
            studies=studies,
            only=only,
            units_at_once=units_at_once,
            off_peak_only=off_peak_only,
            label=label,
            task=task,
        )
    )


def _pass_start(
    *,
    name: str,
    ceiling_usd: float,
    model: list[str],
    keys_file: Path | None,
    studies: Path | None,
    only: str | None,
    units_at_once: int,
    off_peak_only: bool,
    label: str | None,
    task: Path | None,
) -> _StageOut:
    """The body of ``pass start``: every setup refusal first, then the hold, then the pass."""
    from labllm import Rejected, Task

    from ..gateway import check_ceiling, make_gateway
    from ..passes.model_text import parse_models
    from ..passes.progress import run_with_progress
    from ..passes.status import pass_json
    from ..study.folder import default_set_dir

    def run(setup: KindTask) -> _StageOut:
        kind = setup.kind
        named = parse_models(model)
        if keys_file is not None and only is not None:
            raise SetupRefused("give --keys FILE or --only ENDINGS, not both")
        folder = default_set_dir() if studies is None else studies
        endings = None if only is None else _parse_endings(only)
        # The units --only names are read from the task, once it is open; the others are known now.
        keys: list[str] | None = None
        if keys_file is not None:
            keys = _read_keys(keys_file)
        elif endings is None:
            keys = list(kind.default_keys(folder))
        check_ceiling(ceiling_usd)
        client = make_gateway()
        built = kind.build(folder)
        # Whatever ends the pass from here, a refused setup included, the build releases what it
        # opened, once the task is closed.
        with closing(built):
            unit_kind = built.unit_kind
            models = {**kind.default_models, **named}
            # The roles are the built kind's, so they are checked after the build opened its stores.
            if extra := sorted(set(models) - set(unit_kind.roles)):
                raise SetupRefused(
                    f"kind {kind.name!r} has no role {', '.join(extra)}; "
                    f"its roles are {', '.join(sorted(unit_kind.roles))}"
                )
            if unfilled := sorted(set(unit_kind.roles) - set(models)):
                raise SetupRefused(
                    f"no model for role {', '.join(unfilled)}: name one with --model"
                )
            with setup.open(Task, gateway=client) as opened:
                if keys is None:
                    keys = opened.keys(unit_kind, endings=endings)
                result = run_with_progress(
                    opened,
                    unit_kind,
                    keys,
                    models=models,
                    ceiling_usd=ceiling_usd,
                    label=label or kind.name,
                    off_peak_only=off_peak_only,
                    units_at_once=units_at_once,
                    note=built.store_build,
                )
                (record,) = [p for p in opened.passes(unit_kind) if p.pass_id == result.pass_id]
        if result.stopped == Rejected.reason:
            # The gateway refused a request outright, such as one for a model it has no price for:
            # the setup is wrong, and rerunning the same command would only meet it again.
            return _StageOut(f"the gateway rejected a request: {result.stop_message}", 2, err=True)
        payload = {
            "kind": kind.name,
            "task": str(setup.folder),
            "keys": len(keys),
            **pass_json(record),
            "message": result.stop_message,
        }
        return _StageOut(payload, 0 if result.stopped is None else 3)

    return for_kind(name, task=task, body=run)


@pass_app.command("status")
def pass_status(
    kind: str = typer.Argument(..., help="The seqforge kind whose task to read."),
    task: Path | None = typer.Option(
        None, "--task", help="The task's folder. Default: seqforge/passes/<kind>/ here."
    ),
) -> None:
    """Report where a kind's task stands, what each pass cost per unit, and each unit not done.

    It prints one JSON object. It reads the task's state and its reviews only, and takes no hold,
    so it works with the gateway down and while a pass runs. `not_done` names each unit whose
    latest outcome is unfinished or held, with that outcome's reason and its pass. Exit 0 with the
    report; 2 for an unknown kind, a folder that holds no task, or a task a pass stopped in the
    middle of a write; 3 when a pass's write kept the read waiting, so a rerun reads.
    """
    _emit(_pass_status(name=kind, task=task))


def _pass_status(*, name: str, task: Path | None) -> _StageOut:
    from ..passes.status import task_status

    def run(setup: KindTask) -> _StageOut:
        with setup.read() as opened:
            report = task_status(opened, setup.kind.name)
        return _StageOut({"kind": setup.kind.name, "task": str(setup.folder), **report}, 0)

    return for_kind(name, task=task, body=run)


def _read_keys(path: Path) -> list[str]:
    """One key per line, in order; blank lines skipped. A repeated key is refused, naming it."""
    try:
        lines = path.read_text().splitlines()
    except (OSError, UnicodeDecodeError) as exc:
        raise SetupRefused(f"cannot read the keys file {path}: {exc}") from exc
    keys = [line.strip() for line in lines if line.strip()]
    seen: set[str] = set()
    for key in keys:
        if key in seen:
            raise SetupRefused(f"the keys file {path} lists {key!r} more than once")
        seen.add(key)
    return keys


def _parse_endings(text: str) -> list[str]:
    """``unsettled,refused`` -> the endings; each must be one liulab-llms keeps a current one of."""
    from labllm import ENDINGS

    endings = [e.strip() for e in text.split(",") if e.strip()]
    if not endings or set(endings) - set(ENDINGS):
        raise SetupRefused(
            f"--only {text!r}: a unit's current ending is one of {', '.join(ENDINGS)}"
        )
    return endings
