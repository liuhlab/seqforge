"""The bounds file of one study: which GEO Series and papers belong to it, and what was left out.

A study is whole Series: no field says which samples of a Series belong. One claim is one Series
kept or one linked record left out, written as :mod:`.claims` has every study file write a claim.
A left-out record's backing says why it does not belong, so it carries no other reason.

:func:`check_bounds` is the format check. It takes a file's content as loaded and returns what is
wrong with it, each problem naming its field; an empty list is a valid file. There is one format at
a time: a file with another ``format`` number is rejected, never read as an older form.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Literal

from pydantic import Field, PositiveInt, ValidationError

from .claims import (
    Claim,
    Passage,
    Strict,
    claim_problems,
    format_problems,
    passage_problems,
    validation_problems,
)

#: The one format the check accepts. A change to the file's shape raises it, and rewrites every file.
FORMAT = 1


class Placed(Claim):
    """One record placed by the bounds: a Series kept, or a Series or paper left out."""

    id: str = Field(min_length=1)


class Models(Strict):
    """Who wrote the file: the model that drew the bounds, and the one that gave probabilities."""

    bounds: str | None
    probabilities: str | None


class Bounds(Strict):
    """A whole bounds file."""

    format: Literal[1]
    study: str = Field(min_length=1)
    models: Models
    series: list[Placed] = Field(min_length=1)
    papers: list[str]
    left_out: list[Placed]
    reason: str = Field(min_length=1)
    passages: dict[PositiveInt, Passage]


def _between_fields(bounds: Bounds) -> Iterator[tuple[str, str]]:
    """What no single field shows: a record placed twice, a claim's mark, a pointer to nothing."""
    claims = [(f"series[{at}]", claim) for at, claim in enumerate(bounds.series)]
    claims += [(f"left_out[{at}]", claim) for at, claim in enumerate(bounds.left_out)]
    # A record is placed once: kept, or left out, never both and never twice.
    placed: dict[str, str] = {}
    places = [(f"papers[{at}]", paper) for at, paper in enumerate(bounds.papers)]
    places += [(f"{where}.id", claim.id) for where, claim in claims]
    for where, record in places:
        if record in placed:
            yield where, f"{record} is already placed at {placed[record]}"
        placed.setdefault(record, where)
    yield from passage_problems(bounds.passages)
    for where, claim in claims:
        yield from claim_problems(
            where, claim, bounds.passages, probabilities_by=bounds.models.probabilities
        )


def check_bounds(data: object) -> list[dict[str, str]]:
    """What is wrong with one bounds file's content, each as ``{"field", "reason"}``; none if valid."""
    if problems := format_problems(data, FORMAT):
        return problems
    try:
        bounds = Bounds.model_validate(data)
    except ValidationError as exc:
        return validation_problems(exc)
    return [{"field": field, "reason": reason} for field, reason in _between_fields(bounds)]
