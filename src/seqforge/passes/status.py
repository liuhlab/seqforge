"""Where a kind's task stands: each unit's current ending, and each pass's cost and time per unit.

Read through liulab-llms's public reads of a task only -- its passes, every key it has seen, each
unit's outcomes, the keys of each current ending -- never its state file's tables. Nothing here
calls the gateway, so a task reads the same with the gateway down.

A task can hold about 200,000 units. So the units are read in one walk over their keys, each unit's
outcomes once; the endings come from one read per ending, and each figure is sorted once.
"""

from __future__ import annotations

import dataclasses
import math
import statistics
from collections import defaultdict
from collections.abc import Callable
from typing import Any

from labllm import Inputs, Task, Unit, UnitCost, UnitKind
from labllm.state import ENDINGS, NOT_ENDINGS

#: Each per-unit figure of a pass, and how it is read from one unit's cost.
FIGURES: dict[str, Callable[[UnitCost], float]] = {
    "spent_usd": lambda c: c.spent_usd,
    "first_cost_usd": lambda c: c.first_cost_usd,
    "requests": lambda c: c.requests,
    "seconds": lambda c: c.seconds or 0.0,
}


def read_only_kind(name: str) -> UnitKind:
    """A unit kind that is only its name, for reading a task of the kind named ``name``.

    A task's reads go by the kind's name. Reading with this instead of the kind's own unit kind means
    status builds nothing from the kind's code, so it needs neither liulab-data's store nor any input
    the kind loads: it reads a task anywhere its folder is.
    """

    def load(key: str) -> Inputs:
        raise RuntimeError(f"pass status reads {name!r}'s task; it loads no unit ({key!r})")

    def run(u: Unit) -> Any:
        raise RuntimeError(f"pass status reads {name!r}'s task; it runs no unit ({u.key!r})")

    return UnitKind(name=name, version="", roles={}, load=load, run=run, answer_schema={})


def task_status(task: Task, kind: UnitKind) -> dict[str, Any]:
    """The kind's units by current ending and by latest outcome, and each of its passes.

    ``counts`` gives every unit's current ending, a person's review applied, and, apart from that,
    the units whose latest outcome is unfinished or held: a unit settled before and then unfinished
    in a later pass counts under both. Each pass's ``per_unit`` covers the units that called a model
    in it -- a cache replay included, a held unit or one the pass never started not.
    """
    counts = dict.fromkeys((*ENDINGS, *NOT_ENDINGS), 0)
    for ending in ENDINGS:
        counts[ending] = len(task.keys(kind, endings=(ending,)))
    costs: dict[int, list[UnitCost]] = defaultdict(list)
    keys = task.keys(kind)
    for key in keys:
        outcomes = task.outcomes(kind, key)
        if outcomes and outcomes[-1].ending in NOT_ENDINGS:
            counts[outcomes[-1].ending] += 1
        for outcome in outcomes:
            if outcome.cost is not None and outcome.cost.seconds is not None:
                costs[outcome.stamp.pass_id].append(outcome.cost)
    return {
        "units": len(keys),
        "counts": counts,
        "passes": [
            {
                "pass_id": p.pass_id,
                "label": p.label,
                "kind_version": p.kind_version,
                "models": {role: dataclasses.asdict(m) for role, m in sorted(p.models.items())},
                "store_build": p.note,
                "ceiling_usd": p.ceiling_usd,
                "ended": p.ended,
                "stopped": p.stopped,
                "counts": p.counts,
                "spent_usd": p.spent_usd,
                "first_cost_usd": p.first_cost_usd,
                "requests": p.requests,
                "cache_hits": p.cache_hits,
                "per_unit": _per_unit(costs[p.pass_id]),
            }
            for p in task.passes(kind)
        ],
    }


def _per_unit(costs: list[UnitCost]) -> dict[str, Any]:
    figures: dict[str, Any] = {"units": len(costs)}
    for name, read in FIGURES.items():
        figures[name] = _spread(sorted(read(c) for c in costs))
    return figures


def _spread(values: list[float]) -> dict[str, float | None]:
    """The total, median and 90th percentile of ``values``, sorted; the last two None when empty.

    The 90th percentile is the nearest rank: the smallest value at least 90% of the values reach.
    """
    if not values:
        return {"total": 0, "median": None, "p90": None}
    return {
        "total": sum(values) if all(isinstance(v, int) for v in values) else math.fsum(values),
        "median": statistics.median(values),
        "p90": values[math.ceil(0.9 * len(values)) - 1],
    }
