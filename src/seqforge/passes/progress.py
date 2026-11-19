"""A task that writes its pass's progress to stderr while the pass runs.

A pass over every GEO Series runs for a day, and liulab-llms's ``run_pass`` blocks until it ends with
no progress hook. So :class:`ProgressTask` tallies each unit's ending and cost as the unit ends, and
writes one line at most every :data:`EVERY_S` seconds -- a day's pass is a few thousand lines, not
two hundred thousand. stdout is never touched: it carries only the command's JSON.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any

import typer
from labllm import Gateway, Model, PassResult, Task, UnitKind
from labllm.state import NOT_ENDINGS, PASS_ENDINGS

#: The fewest seconds between two progress lines.
EVERY_S = 30.0

#: Every outcome a pass gives a unit, in the order a line lists them: liulab-llms's own.
OUTCOMES = (*PASS_ENDINGS, *NOT_ENDINGS)


class ProgressTask(Task):
    """A liulab-llms task whose passes report their progress to stderr.

    The tally hooks the engine's per-unit step, ``_run_unit``: the one place a unit's ending and
    cost are both known. It is a private method, so if a later liulab-llms renames it this override
    is simply never called. The pass then runs and records exactly as before; only the lines
    between the first and the last go quiet.
    """

    def __init__(
        self,
        folder: str | Path,
        *,
        gateway: Gateway,
        reviews: str | Path,
    ) -> None:
        super().__init__(folder, gateway=gateway, reviews=reviews)
        self._lock = threading.Lock()
        self._name = ""
        self._total = 0
        self._counts = dict.fromkeys(OUTCOMES, 0)
        self._spent = 0.0
        self._began = self._last = time.monotonic()

    def run_pass(
        self,
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
        with self._lock:
            self._name, self._total = kind.name, len(keys)
            self._counts = dict.fromkeys(OUTCOMES, 0)
            self._spent = 0.0
            self._began = self._last = time.monotonic()
        _say(
            f"{kind.name}: a pass over {len(keys):,} units starts, ceiling ${ceiling_usd:,.2f}, "
            f"{units_at_once} at once, task {self.folder}"
        )
        result = super().run_pass(
            kind,
            keys,
            models=models,
            ceiling_usd=ceiling_usd,
            label=label,
            off_peak_only=off_peak_only,
            units_at_once=units_at_once,
            note=note,
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

    def _run_unit(self, *args: Any, **kwargs: Any) -> Any:
        ended = super()._run_unit(*args, **kwargs)
        ending = getattr(ended, "ending", None)
        spent = getattr(getattr(ended, "cost", None), "spent_usd", 0.0)
        with self._lock:
            if ending in self._counts:
                self._counts[ending] += 1
            self._spent += spent
            now = time.monotonic()
            if now - self._last >= EVERY_S:
                self._last = now
                _say(
                    f"{self._name}: {_tally(self._counts, self._total)}; "
                    f"${self._spent:,.4f} spent, {_clock(now - self._began)} in"
                )
        return ended


def _tally(counts: Mapping[str, int], total: int) -> str:
    done = sum(counts.get(o, 0) for o in OUTCOMES)
    each = ", ".join(f"{o} {counts.get(o, 0):,}" for o in OUTCOMES)
    return f"{done:,} of {total:,} units ended ({each})"


def _clock(seconds: float) -> str:
    whole = int(seconds)
    return f"{whole // 3600}:{whole // 60 % 60:02d}:{whole % 60:02d}"


def _say(line: str) -> None:
    typer.echo(line, err=True)
