"""A pass's progress, written to stderr while the pass runs.

A pass over every GEO Series runs for a day, and liulab-llms's ``run_pass`` blocks until it ends.
So :func:`run_with_progress` hands ``run_pass`` an ``on_end`` hook that tallies each unit's ending
and cost as the unit ends, and writes one line at most every :data:`EVERY_S` seconds -- a day's pass
is a few thousand lines, not two hundred thousand. stdout is never touched: it carries only the
command's JSON.
"""

from __future__ import annotations

import time
from collections.abc import Iterable, Mapping

import typer
from labllm import Model, PassResult, Task, UnitEnd, UnitKind
from labllm.state import NOT_ENDINGS, PASS_ENDINGS

#: The fewest seconds between two progress lines.
EVERY_S = 30.0

#: Every outcome a pass gives a unit, in the order a line lists them: liulab-llms's own.
OUTCOMES = (*PASS_ENDINGS, *NOT_ENDINGS)


class _Progress:
    """The running tally of one pass; :meth:`ended` is the ``on_end`` hook.

    liulab-llms never runs two ``on_end`` calls at once, so the tally needs no lock.
    """

    def __init__(self, name: str, total: int) -> None:
        self._name = name
        self._total = total
        self._counts = dict.fromkeys(OUTCOMES, 0)
        self._spent = 0.0
        self._began = self._last = time.monotonic()

    def ended(self, end: UnitEnd) -> None:
        """Count one unit's ending and cost, and write a line if :data:`EVERY_S` have passed."""
        if end.ending in self._counts:
            self._counts[end.ending] += 1
        self._spent += end.cost.spent_usd
        now = time.monotonic()
        if now - self._last >= EVERY_S:
            self._last = now
            _say(
                f"{self._name}: {_tally(self._counts, self._total)}; "
                f"${self._spent:,.4f} spent, {_clock(now - self._began)} in"
            )


def run_with_progress(
    task: Task,
    kind: UnitKind,
    keys: Iterable[str],
    *,
    models: Mapping[str, Model],
    ceiling_usd: float,
    label: str,
    off_peak_only: bool = False,
    units_at_once: int = 32,
    note: str | None = None,
) -> PassResult:
    """Run the pass as liulab-llms does, writing a line as it starts, as it goes and as it ends."""
    keys = list(keys)
    _say(
        f"{kind.name}: a pass over {len(keys):,} units starts, ceiling ${ceiling_usd:,.2f}, "
        f"{units_at_once} at once, task {task.folder}"
    )
    result = task.run_pass(
        kind,
        keys,
        models=models,
        ceiling_usd=ceiling_usd,
        label=label,
        off_peak_only=off_peak_only,
        units_at_once=units_at_once,
        note=note,
        on_end=_Progress(kind.name, len(keys)).ended,
    )
    ending = (
        "reached the end of its keys"
        if result.stopped is None
        else f"stopped, {result.stopped}: {result.stop_message}"
    )
    _say(
        f"{kind.name}: pass {result.pass_id} {ending}. "
        f"{_tally(result.counts, len(keys))}; ${result.spent_usd:,.4f} spent, "
        f"{result.requests:,} requests sent, {result.cache_hits:,} answers from the cache"
    )
    return result


def _tally(counts: Mapping[str, int], total: int) -> str:
    done = sum(counts.get(o, 0) for o in OUTCOMES)
    each = ", ".join(f"{o} {counts.get(o, 0):,}" for o in OUTCOMES)
    return f"{done:,} of {total:,} units ended ({each})"


def _clock(seconds: float) -> str:
    whole = int(seconds)
    return f"{whole // 3600}:{whole // 60 % 60:02d}:{whole % 60:02d}"


def _say(line: str) -> None:
    typer.echo(line, err=True)
