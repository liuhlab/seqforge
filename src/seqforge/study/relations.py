"""What a valid reading implies between its samples: five relations, worked out and never stored.

A reading writes each sample's labels and the study's factors. Which samples are replicates, which
groups a contrast compares, which samples run along an ordered factor, which libraries came from
one piece of material, and which control goes with which pull-down all follow from those, so no
file holds them. :func:`relations` recomputes them. Samples, factors and levels come out in the
file's own order.

Two samples have the same assay, target or control type when their claims carry the same ``text``,
or the same gap (both ``na``, or both ``unknown``). A sample's level on a factor is the ``level``
of its claim on that label, and ``None`` when the factor does not cover the sample. A **control
library** is a sample whose ``control_type`` claim says a kind: it is ``quoted`` or ``inferred``. A
control type that is ``unknown`` makes no control. A **signal** is a sample whose ``target`` claim
says a target the same way, and that is not a control library.
"""

from __future__ import annotations

from itertools import combinations
from typing import Any

from .reading import Reading, SampleClaim

#: What a claim says, to compare two samples on: its text, or which gap it marks.
_Said = tuple[str, str | None]


def _said(claim: SampleClaim) -> _Said:
    if claim.na is not None:
        return ("na", None)
    if claim.unknown is not None:
        return ("unknown", None)
    return ("says", claim.text)


def _grouped(pairs: list[tuple[Any, Any]]) -> dict[Any, list[Any]]:
    """The second of each pair under the first, both in the order met."""
    out: dict[Any, list[Any]] = {}
    for key, member in pairs:
        out.setdefault(key, []).append(member)
    return out


def relations(reading: Reading) -> dict[str, list[dict[str, Any]]]:
    """The five relations of a valid reading. Ask only of a reading the check found no problem in."""
    rows = reading.rows()
    factors = list(reading.factors)
    assay = {s: _said(row["assay"]) for s, row in rows.items()}
    target = {s: _said(row["target"]) for s, row in rows.items()}
    control = {s: _said(row["control_type"]) for s, row in rows.items()}
    levels = {
        s: tuple(row[f].level if f in row else None for f in factors) for s, row in rows.items()
    }

    def but(sample: str, at: int) -> tuple[str | None, ...]:
        """A sample's levels on every factor but one."""
        return levels[sample][:at] + levels[sample][at + 1 :]

    def fixed(rest: tuple[str | None, ...], at: int) -> dict[str, str | None]:
        return dict(zip(factors[:at] + factors[at + 1 :], rest, strict=True))

    # A group is the samples equal on assay, target, control type and every factor level.
    groups = _grouped([((assay[s], target[s], control[s], levels[s]), s) for s in rows])
    replicates = [
        {
            "assay": a[1],
            "target": t[1],
            "control_type": c[1],
            "levels": dict(zip(factors, at, strict=True)),
            "samples": members,
        }
        for (a, t, c, at), members in groups.items()
        if len(members) > 1
    ]

    contrasts: list[dict[str, Any]] = []
    series: list[dict[str, Any]] = []
    for at, (label, factor) in enumerate(reading.factors.items()):
        covered = [s for s in rows if levels[s][at] is not None]
        # Samples that differ in this factor alone: equal on all else that makes a group.
        runs = _grouped([((assay[s], target[s], control[s], but(s, at)), s) for s in covered])
        for (a, t, c, rest), members in runs.items():
            if c[0] == "says":  # neither group that a contrast compares is a control library
                continue
            by_level = _grouped([(levels[s][at], s) for s in members])
            present = [level for level in factor.levels if level in by_level]
            if factor.baseline is None:
                pairs = list(combinations(present, 2))
            else:
                pairs = [(factor.baseline, level) for level in present if level != factor.baseline]
            contrasts += [
                {
                    "factor": label,
                    "baseline": factor.baseline,
                    "levels": [one, other],
                    "samples": {one: by_level[one], other: by_level[other]},
                    "assay": a[1],
                    "target": t[1],
                    "fixed": fixed(rest, at),
                }
                for one, other in pairs
                if one in by_level
            ]
        if not factor.ordered:
            continue
        for (a, t, c, rest), members in runs.items():
            by_level = _grouped([(levels[s][at], s) for s in members])
            if len(by_level) > 1:
                series.append(
                    {
                        "factor": label,
                        "assay": a[1],
                        "target": t[1],
                        "control_type": c[1],
                        "fixed": fixed(rest, at),
                        "steps": [
                            {"level": level, "samples": by_level[level]}
                            for level in factor.levels
                            if level in by_level
                        ],
                    }
                )

    material = _grouped([(row["biosample"].id, s) for s, row in rows.items()])
    shared = {biosample: members for biosample, members in material.items() if len(members) > 1}
    return {
        "replicates": replicates,
        "contrasts": contrasts,
        "series": series,
        "shared_biosample": [{"biosample": b, "samples": members} for b, members in shared.items()],
        "signal_to_control": [
            {"biosample": b, "signal": signal, "control": library}
            for b, members in shared.items()
            for signal in members
            if target[signal][0] == "says" and control[signal][0] != "says"
            for library in members
            if control[library][0] == "says"
        ],
    }
