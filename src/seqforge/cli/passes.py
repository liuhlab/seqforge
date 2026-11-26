"""`seqforge pass`: start a pass of a seqforge unit kind through the lab gateway, and read its task.

The pass itself is liulab-llms's. This module turns the options into what it takes -- a kind, models
by role, keys in order, a dollar ceiling, a task -- and its result into one JSON object on stdout
and an exit code: 0 when the pass reached the end of its keys whatever the counts, 2 for a setup it
cannot use, 3 when it stopped or another process holds the task. Rerunning the same command
resumes: every unit already answered replays from the gateway's cache at no cost.
"""

from __future__ import annotations

import math
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
    kind: str = typer.Argument(..., help="The registered seqforge kind to run."),
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

    The units are the kind's default keys, or `--keys FILE`, or `--only ENDINGS`. Exit 0 when the
    pass reached the end of its keys, whatever its counts; 3 when it stopped -- at its ceiling, a
    provider's balance or cap, a provider or the gateway unavailable -- or another process holds the
    task; 2 for a setup it cannot use, a model name the gateway rejects included. Rerun the same
    command to resume: units already answered replay from the gateway's cache at no cost. Progress
    goes to stderr; stdout carries only JSON.
    """
    _emit(
        _pass_start(
            name=kind,
            ceiling_usd=ceiling_usd,
            model=model,
            keys_file=keys_file,
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
    only: str | None,
    units_at_once: int,
    off_peak_only: bool,
    label: str | None,
    task: Path | None,
) -> _StageOut:
    """The body of ``pass start``: every setup refusal first, then the hold, then the pass."""
    from labllm import GatewayUnreachable, Rejected, Task

    from ..passes import gateway
    from ..passes.model_text import parse_models
    from ..passes.progress import run_with_progress
    from ..passes.status import pass_json
    from ..passes.task import store_for

    def run(setup: KindTask) -> _StageOut:
        kind = setup.kind
        named = parse_models(model)
        if keys_file is not None and only is not None:
            raise SetupRefused("give --keys FILE or --only ENDINGS, not both")
        file_keys = None if keys_file is None else _read_keys(keys_file)
        endings = None if only is None else _parse_endings(only)
        if not math.isfinite(ceiling_usd) or ceiling_usd < 0:
            raise SetupRefused(f"--ceiling-usd must be a number of dollars, not {ceiling_usd}")
        try:
            client = gateway.make_gateway()
        except GatewayUnreachable as exc:
            raise SetupRefused(f"no gateway address: {exc}") from exc
        store, build = store_for(kind)
        unit_kind = kind.unit_kind(store)
        models = {**kind.default_models, **named}
        if extra := sorted(set(models) - set(unit_kind.roles)):
            raise SetupRefused(
                f"kind {kind.name!r} has no role {', '.join(extra)}; "
                f"its roles are {', '.join(sorted(unit_kind.roles))}"
            )
        if unfilled := sorted(set(unit_kind.roles) - set(models)):
            raise SetupRefused(f"no model for role {', '.join(unfilled)}: name one with --model")

        with setup.open(Task, gateway=client) as opened:
            if file_keys is not None:
                keys = file_keys
            elif endings is not None:
                keys = opened.keys(unit_kind, endings=endings)
            else:
                keys = list(kind.default_keys(store))
            result = run_with_progress(
                opened,
                unit_kind,
                keys,
                models=models,
                ceiling_usd=ceiling_usd,
                label=label or kind.name,
                off_peak_only=off_peak_only,
                units_at_once=units_at_once,
                note=build,
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
    kind: str = typer.Argument(..., help="The registered seqforge kind whose task to read."),
    task: Path | None = typer.Option(
        None, "--task", help="The task's folder. Default: seqforge/passes/<kind>/ here."
    ),
) -> None:
    """Report where a kind's task stands, and what each of its passes cost per unit, as JSON.

    It reads the task's state and its reviews only, so it works with the gateway down. Exit 0 with
    the report; 2 for an unknown kind or a folder that holds no task; 3 when a running pass holds
    the task, whose stderr is then the live view.
    """
    _emit(_pass_status(name=kind, task=task))


def _pass_status(*, name: str, task: Path | None) -> _StageOut:
    from ..passes.status import read_only_kind, task_status

    def run(setup: KindTask) -> _StageOut:
        setup.require_task()
        with setup.read() as opened:
            report = task_status(opened, read_only_kind(setup.kind.name))
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
    from labllm.state import ENDINGS

    endings = [e.strip() for e in text.split(",") if e.strip()]
    if not endings or set(endings) - set(ENDINGS):
        raise SetupRefused(
            f"--only {text!r}: a unit's current ending is one of {', '.join(ENDINGS)}"
        )
    return endings
