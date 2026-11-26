"""Where a kind's task stands: each unit's ending, each unit not done, each pass's cost per unit.

Read through liulab-llms's public reads of a task only -- its passes, every key it has seen, each
unit's outcomes, the keys of each current ending -- never its state file's tables. Each read goes
by the kind's name, so nothing is built from the kind's code, and nothing here calls the gateway:
a task reads the same with the gateway down and with none of the stores a kind reads.

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

from labllm import ENDINGS, NOT_ENDINGS, Pass, TaskReader, UnitCost

#: Each per-unit figure of a pass, and how it is read from one unit's cost.
FIGURES: dict[str, Callable[[UnitCost], float]] = {
    "spent_usd": lambda c: c.spent_usd,
    "first_cost_usd": lambda c: c.first_cost_usd,
    "requests": lambda c: c.requests,
    "seconds": lambda c: c.seconds or 0.0,
}


def task_status(task: TaskReader, kind: str) -> dict[str, Any]:
    """The units of the kind named ``kind`` by current ending and by latest outcome, and its passes.

    ``counts`` gives every unit's current ending, a person's review applied, and, apart from that,
    the units whose latest outcome is unfinished or held: a unit settled before and then unfinished
    in a later pass counts under both. ``not_done`` names each of those last units, in key order,
    with that outcome's ending, its reason and its pass; it has no cap, so a caller filters it.
    Each pass's ``per_unit`` covers the units that called a model in it -- a cache replay included,
    a held unit or one the pass never started not.
    """
    counts = dict.fromkeys((*ENDINGS, *NOT_ENDINGS), 0)
    for ending in ENDINGS:
        counts[ending] = len(task.keys(kind, endings=(ending,)))
    costs: dict[int, list[UnitCost]] = defaultdict(list)
    not_done: list[dict[str, Any]] = []
    keys = task.keys(kind)
    for key in keys:
        outcomes = task.outcomes(kind, key)
        if outcomes and (latest := outcomes[-1]).ending in NOT_ENDINGS:
            counts[latest.ending] += 1
            not_done.append(
                {
                    "key": key,
                    "ending": latest.ending,
                    "reason": latest.reason,
                    "pass": latest.stamp.pass_id,
                }
            )
        for outcome in outcomes:
            if outcome.cost is not None and outcome.cost.seconds is not None:
                costs[outcome.stamp.pass_id].append(outcome.cost)
    return {
        "units": len(keys),
        "counts": counts,
        "not_done": not_done,
        "passes": [
            {
                **pass_json(p),
                "ceiling_usd": p.ceiling_usd,
                "ended": p.ended,
                "per_unit": _per_unit(costs[p.pass_id]),
            }
            for p in task.passes(kind)
        ],
    }


def pass_json(p: Pass) -> dict[str, Any]:
    """What ``pass start`` and ``pass status`` both say of one pass, from the task's record of it."""
    return {
        "pass_id": p.pass_id,
        "label": p.label,
        "kind_version": p.kind_version,
        "models": {role: dataclasses.asdict(m) for role, m in sorted(p.models.items())},
        "store_build": p.note,
        "counts": p.counts,
        "stopped": p.stopped,
        "spent_usd": p.spent_usd,
        "first_cost_usd": p.first_cost_usd,
        "requests": p.requests,
        "cache_hits": p.cache_hits,
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
