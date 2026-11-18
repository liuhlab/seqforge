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
from labllm import (
    CeilingReached,
    Gateway,
    GatewayUnreachable,
    Model,
    PassResult,
    ProviderExhausted,
    ProviderUnavailable,
    Task,
    UnitKind,
)

#: The fewest seconds between two progress lines.
EVERY_S = 30.0

#: Every outcome a pass gives a unit, in the order a line lists them.
OUTCOMES = ("settled", "unsettled", "refused", "unfinished", "held")

#: The reasons of the stops, each of which the unit that met it records as ``"<reason>: <message>"``.
_STOPS = tuple(
    s.reason for s in (CeilingReached, ProviderExhausted, ProviderUnavailable, GatewayUnreachable)
)


class ProgressTask(Task):
    """A liulab-llms task whose passes report their progress to stderr, and their stop's message.

    The tally hooks the engine's per-unit step, ``_run_unit``: the one place a unit's ending, cost
    and reason are all known. It is a private method, so if a later liulab-llms renames it this
    override is simply never called. The pass then runs and records exactly as before; only the
    lines between the first and the last go quiet, and :meth:`stop_message` falls back to the reason.
    """

    def __init__(
        self,
        folder: str | Path,
        *,
        gateway: Gateway,
        reviews: str | Path,
        every_s: float = EVERY_S,
    ) -> None:
        super().__init__(folder, gateway=gateway, reviews=reviews)
        self._every_s = every_s
        self._lock = threading.Lock()
        self._name = ""
        self._total = 0
        self._counts = dict.fromkeys(OUTCOMES, 0)
        self._spent = 0.0
        self._began = self._last = time.monotonic()
        self._met: str | None = None

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
    ) -> PassResult:
        """Run the pass as liulab-llms does, writing a line as it starts, as it goes and as it ends."""
        keys = list(keys)
        with self._lock:
            self._name, self._total = kind.name, len(keys)
            self._counts = dict.fromkeys(OUTCOMES, 0)
            self._spent, self._met = 0.0, None
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
        )
        ending = (
            "reached the end of its keys"
            if result.stopped is None
            else f"stopped, {result.stopped}: {self.stop_message(result)}"
        )
        _say(
            f"{kind.name}: pass {result.pass_id} {ending}. "
            f"{_tally(result.counts, len(keys))}; ${result.spent_usd:,.4f} spent, "
            f"{result.requests:,} requests sent, {result.cache_hits:,} answers from the cache"
        )
        return result

    def stop_message(self, result: PassResult) -> str | None:
        """The message of the stop that ended ``result``'s pass, as the gateway gave it; None if none.

        The unit that met the stop records the gateway's own message, such as the ceiling crossed or
        the provider's balance; every unit after it records only that the pass stopped.
        """
        if result.stopped is None:
            return None
        with self._lock:
            return self._met or f"the pass stopped: {result.stopped}"

    def _run_unit(self, *args: Any, **kwargs: Any) -> Any:
        ended = super()._run_unit(*args, **kwargs)
        ending = getattr(ended, "ending", None)
        spent = getattr(getattr(ended, "cost", None), "spent_usd", 0.0)
        reason = getattr(ended, "reason", None)
        with self._lock:
            if ending in self._counts:
                self._counts[ending] += 1
            self._spent += spent
            if self._met is None and ending == "unfinished" and isinstance(reason, str):
                stop, sep, message = reason.partition(": ")
                if sep and stop in _STOPS:
                    self._met = message
            now = time.monotonic()
            if now - self._last >= self._every_s:
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
