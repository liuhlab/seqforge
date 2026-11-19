"""`seqforge pass`: start a pass of a seqforge unit kind through the lab gateway.

The pass itself is liulab-llms's. This module turns the options into what it takes -- a kind, models
by role, keys in order, a dollar ceiling, a task -- and its result into one JSON object on stdout
and an exit code: 0 when the pass reached the end of its keys whatever the counts, 2 for a setup it
cannot use, 3 when it stopped or another process holds the task. Rerunning the same command
resumes: every unit already answered replays from the gateway's cache at no cost.
"""

from __future__ import annotations

import dataclasses
import math
from pathlib import Path
from typing import TYPE_CHECKING, Any

import typer

from ..passes import SetupRefused
from ..workspace import passes_dir
from ._common import _emit, _StageOut
from .root import pass_app

if TYPE_CHECKING:
    # liulab-llms is imported where it is used: it costs a few tenths of a second, which every other
    # verb's start would otherwise pay.
    from labllm import Model

#: The stop reason of a pass the gateway's rejection of a request ended: a setup refusal, not a stop.
REJECTED = "rejected"

#: The settings a ``--model`` value may carry after its name, and the ``Model`` field each sets.
_SETTINGS = {
    "thinking": "thinking",
    "effort": "effort",
    "temperature": "temperature",
    "max-output-tokens": "max_output_tokens",
}


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
            "The model for one role, repeatable, e.g. writer=deepseek/deepseek-flash,thinking=off. "
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
    reviews: Path | None = typer.Option(
        None,
        "--reviews",
        envvar="SEQFORGE_REVIEWS",
        help="A local clone of the review repo, whose reviews/ say which units a review holds.",
    ),
) -> None:
    """Run one pass of a seqforge kind over its units, and print its JSON.

    The units are the kind's default keys, or `--keys FILE`, or `--only ENDINGS`. Exit 0 when the
    pass reached the end of its keys, whatever its counts; 3 when it stopped -- at its ceiling, a
    provider's balance or cap, a provider or the gateway unavailable -- or another process holds the
    task; 2 for a setup it cannot use, a model name the gateway rejects included. Rerun the same command to resume: units already answered
    replay from the gateway's cache at no cost. Progress goes to stderr; stdout carries only JSON.
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
            reviews=reviews,
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
    reviews: Path | None,
) -> _StageOut:
    """The body of ``pass start``: every setup refusal first, then the hold, then the pass."""
    from labllm import GatewayUnreachable, TaskInUse

    from ..passes import gateway
    from ..passes.kinds import kind_named
    from ..passes.progress import ProgressTask
    from ..passes.task import open_task, reviews_folder, store_for

    try:
        kind = kind_named(name)
        named = dict(_parse_model(text) for text in _no_role_twice(model))
        if keys_file is not None and only is not None:
            raise SetupRefused("give --keys FILE or --only ENDINGS, not both")
        file_keys = None if keys_file is None else _read_keys(keys_file)
        endings = None if only is None else _parse_endings(only)
        review_files = reviews_folder(reviews)
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
        folder = task if task is not None else passes_dir(Path(), kind.name)
        label = label or kind.name
        opened = open_task(ProgressTask, folder, gateway=client, reviews=review_files)
    except SetupRefused as exc:
        return _StageOut(str(exc), 2, err=True)
    except TaskInUse as exc:
        return _held(kind.name, folder, exc)

    try:
        with opened:
            if file_keys is not None:
                keys = file_keys
            elif endings is not None:
                keys = opened.keys(unit_kind, endings=endings)
            else:
                keys = list(kind.default_keys(store))
            result = opened.run_pass(
                unit_kind,
                keys,
                models=models,
                ceiling_usd=ceiling_usd,
                label=label,
                off_peak_only=off_peak_only,
                units_at_once=units_at_once,
                note=build,
            )
    except TaskInUse as exc:
        # Another process took the task over mid-pass, as it may once this one's hold goes stale.
        return _held(kind.name, folder, exc)
    if result.stopped == REJECTED:
        # The gateway refused a request outright, such as one for a model it has no price for: the
        # setup is wrong, and rerunning the same command would only meet it again.
        return _StageOut(f"the gateway rejected a request: {result.stop_message}", 2, err=True)
    return _StageOut(
        {
            "kind": kind.name,
            "kind_version": unit_kind.version,
            "task": str(folder),
            "pass_id": result.pass_id,
            "label": label,
            "store_build": build,
            "models": {role: dataclasses.asdict(m) for role, m in sorted(models.items())},
            "keys": len(keys),
            "counts": result.counts,
            "stopped": result.stopped,
            "message": result.stop_message,
            "spent_usd": result.spent_usd,
            "first_cost_usd": result.first_cost_usd,
            "requests": result.requests,
            "cache_hits": result.cache_hits,
        },
        0 if result.stopped is None else 3,
    )


def _held(kind: str, folder: Path, exc: Exception) -> _StageOut:
    """Exit 3 for a task another process holds; liulab-llms's message names the holder."""
    return _StageOut(
        {"kind": kind, "task": str(folder), "stopped": "task_in_use", "message": str(exc)}, 3
    )


def _no_role_twice(values: list[str]) -> list[str]:
    roles = [v.partition("=")[0].strip() for v in values]
    if twice := sorted({r for r in roles if roles.count(r) > 1}):
        raise SetupRefused(f"--model names role {', '.join(twice)} more than once")
    return values


def _parse_model(text: str) -> tuple[str, Model]:
    """``ROLE=PROVIDER/NAME[,KEY=VALUE...]`` -> ``(role, Model)``.

    The name is split from the provider at the first ``/`` only: a provider's model name may hold
    one. Settings not written take liulab-llms's ``Model`` defaults, not the kind's: the string is
    the whole model value, because the settings are part of the gateway's cache key.
    """
    from labllm import Model

    shape = f"--model {text!r}: write ROLE=PROVIDER/NAME[,KEY=VALUE...]"
    role, eq, rest = text.partition("=")
    head, *pairs = rest.split(",")
    provider, slash, model_name = head.partition("/")
    if not (eq and role.strip() and slash and provider.strip() and model_name.strip()):
        raise SetupRefused(shape)
    fields: dict[str, Any] = {}
    for pair in pairs:
        key, eq, value = (s.strip() for s in pair.partition("="))
        if not eq or key not in _SETTINGS:
            raise SetupRefused(f"{shape}, where KEY is one of {', '.join(_SETTINGS)}")
        if _SETTINGS[key] in fields:
            raise SetupRefused(f"--model {text!r} sets {key} twice")
        try:
            fields[_SETTINGS[key]] = _setting(key, value)
        except ValueError as exc:
            raise SetupRefused(f"--model {text!r}: {exc}") from None
    return role.strip(), Model(provider.strip(), model_name.strip(), **fields)


def _setting(key: str, value: str) -> bool | float | int | str:
    if key == "thinking":
        if value not in ("on", "off"):
            raise ValueError(f"thinking is on or off, not {value!r}")
        return value == "on"
    if key == "temperature":
        return float(value)
    if key == "max-output-tokens":
        tokens = int(value)
        if tokens < 1:
            raise ValueError(f"max-output-tokens is one or more, not {tokens}")
        return tokens
    if not value:
        raise ValueError(f"{key} needs a value")
    return value


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
