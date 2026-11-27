"""The bounds file of one study: which GEO Series and papers belong to it, and what was left out.

A study is whole Series: no field says which samples of a Series belong. One claim is one Series
kept or one linked record left out, written as :mod:`.claims` has every study file write a claim.
A left-out record's backing says why it does not belong, so it carries no other reason.

:func:`check_bounds` is the format check. It takes a file's content as loaded and returns what is
wrong with it, each problem naming its field; an empty list is a valid file. There is one format at
a time: a file with another ``format`` number is rejected, never read as an older form.
"""

from __future__ import annotations

import re
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

#: How a kept record is written. Every paper of the standard set is a PubMed id; no other form occurs.
_SERIES = re.compile(r"GSE[0-9]+")
_PAPER = re.compile(r"pmid:[0-9]+")


class Placed(Claim):
    """One record placed by the bounds: a Series kept, or a Series or paper left out."""

    id: str


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
    reason: str
    passages: dict[PositiveInt, Passage]
    #: Each placed record's title, copied from its archive record or paper, so a reader of the file
    #: needs no store. The one optional field: a file drawn before titles were copied has none.
    titles: dict[str, str] = Field(default_factory=dict)


def _between_fields(bounds: Bounds) -> Iterator[tuple[str, str]]:
    """What no single field shows: a record placed twice, a claim's mark, a pointer to nothing."""
    claims = [(f"series[{at}]", claim) for at, claim in enumerate(bounds.series)]
    claims += [(f"left_out[{at}]", claim) for at, claim in enumerate(bounds.left_out)]
    # A kept record is written in the one form of its kind, so a sample is never kept as a Series.
    for at, claim in enumerate(bounds.series):
        if not _SERIES.fullmatch(claim.id):
            yield f"series[{at}].id", "a Series is written GSE<number>"
    for at, paper in enumerate(bounds.papers):
        if not _PAPER.fullmatch(paper):
            yield f"papers[{at}]", "a paper is written pmid:<number>"
    for at, claim in enumerate(bounds.left_out):
        if not claim.id.strip():
            yield f"left_out[{at}].id", "blank: a left-out record is named"
    if not bounds.reason.strip():
        yield "reason", "blank: one line says why these Series and papers are one study"
    # A record is placed once: kept, or left out, never both and never twice.
    placed: dict[str, str] = {}
    places = [(f"papers[{at}]", paper) for at, paper in enumerate(bounds.papers)]
    places += [(f"{where}.id", claim.id) for where, claim in claims]
    for where, record in places:
        if record in placed:
            yield where, f"{record} is already placed at {placed[record]}"
        placed.setdefault(record, where)
    for record, title in bounds.titles.items():
        if record not in placed:
            yield f"titles.{record}", "not a record these bounds place"
        elif not title.strip():
            yield f"titles.{record}", "blank: a record with no title has no entry"
    yield from passage_problems(bounds.passages)
    for where, claim in claims:
        yield from claim_problems(
            where, claim, bounds.passages, probabilities_by=bounds.models.probabilities
        )


def read_bounds(data: object) -> tuple[Bounds | None, list[dict[str, str]]]:
    """One bounds file's content as bounds, and what is wrong with it.

    The bounds are ``None`` when the content is not a bounds file in this format at all. Use them
    only when the problems are empty.
    """
    if problems := format_problems(data, FORMAT):
        return None, problems
    try:
        bounds = Bounds.model_validate(data)
    except ValidationError as exc:
        return None, validation_problems(exc)
    return bounds, [{"field": field, "reason": reason} for field, reason in _between_fields(bounds)]


def check_bounds(data: object) -> list[dict[str, str]]:
    """What is wrong with one bounds file's content, each as ``{"field", "reason"}``; none if valid."""
    return read_bounds(data)[1]
