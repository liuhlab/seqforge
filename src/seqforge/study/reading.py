"""The reading file of one study: what each sequenced sample is, with the words that say so.

One claim is one label of one sample, written as :mod:`.claims` has every study file write a claim,
with one more backing mark of the reading's own: ``na``, for a label that does not apply. A claim
under ``shared`` belongs to every sample whose own row does not write that label, and a row's claim
replaces the shared one whole. A **factor** names a label the study varied on purpose and the
levels it took; a sample's claim on that label names its level.

:func:`check_reading` is the format check. It takes a file's content as loaded and returns what is
wrong with it, each problem naming its field; an empty list is a valid file. The check is
structural and reads the file alone: it never opens a record, so it does not look for a quote in
its source. :func:`read_reading` hands back the reading itself as well, for what is worked out of a
valid one.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any, Literal

from pydantic import Field, PositiveInt, StrictBool, ValidationError

from .claims import (
    MARKS,
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

#: The labels every sample has once the shared claims are applied. A row may add labels of its own.
LABELS = (
    "biosample",
    "donor",
    "assay",
    "target",
    "control_type",
    "species",
    "sex",
    "age",
    "development_stage",
    "tissue",
    "cell_type",
    "cell_line",
    "disease",
    "genotype",
    "strain",
    "treatment",
    "dose",
    "duration",
)

#: A sample's claim has one more backing mark than a bounds claim: the label does not apply.
SAMPLE_MARKS = (*MARKS, "na")

#: The labels whose claim may say only which one it is, by a name made up in the file.
_NAMED = ("biosample", "donor")

#: What says a value. A claim that marks a gap carries none of them.
_VALUE_FIELDS = ("text", "term", "value", "unit", "level")


class SampleClaim(Claim):
    """One label of one sample: what the source says, and how that is backed."""

    na: str | None = None
    text: str | None = None
    term: str | None = None
    #: One number, or the two ends of a range. Typed loosely so that the check says which in the
    #: format's own words: see :func:`_number_or_range`.
    value: Any = None
    unit: str | None = None
    level: str | None = None
    id: str | None = None

    @property
    def says(self) -> bool:
        """Whether the claim gives the label a value, and is not a marked gap."""
        return self.unknown is None and self.na is None


class Factor(Claim):
    """A label the study varied on purpose, and the levels it took. A factor is a claim too."""

    levels: list[str] = Field(min_length=1)
    #: Required. ``None`` when the study compares the levels with each other and none is a reference.
    baseline: str | None
    ordered: StrictBool


class Models(Strict):
    """Who wrote the file: the model that read the study, and the one that gave probabilities."""

    reading: str | None
    probabilities: str | None


class Reading(Strict):
    """A whole reading file."""

    format: Literal[1]
    study: str = Field(min_length=1)
    models: Models
    read: list[Literal["records", "abstract", "full_text"]] = Field(min_length=1)
    read_detail: str | None = None
    passages: dict[PositiveInt, Passage]
    factors: dict[str, Factor]
    shared: dict[str, SampleClaim]
    samples: dict[str, dict[str, SampleClaim]] = Field(min_length=1)

    def rows(self) -> dict[str, dict[str, SampleClaim]]:
        """Every sample's claims by label, once the shared claims are applied."""
        return {sample: {**self.shared, **own} for sample, own in self.samples.items()}


def _number_or_range(value: object) -> bool:
    def number(one: object) -> bool:
        return isinstance(one, int | float) and not isinstance(one, bool)

    return number(value) or (
        isinstance(value, list) and len(value) == 2 and all(number(end) for end in value)
    )


def _label_problems(
    where: str, label: str, claim: SampleClaim, factors: dict[str, Factor]
) -> Iterator[tuple[str, str]]:
    """What is wrong with one written claim that depends on the label it is written under."""
    if claim.value is not None and not _number_or_range(claim.value):
        yield f"{where}.value", "a value is a number, or a range as [low, high]"
    if not claim.says:
        for name in _VALUE_FIELDS:
            if getattr(claim, name) is not None:
                yield f"{where}.{name}", f"an unknown or na claim gives no value, so no {name}"
    elif not (
        (claim.text or "").strip() or claim.value is not None or (label in _NAMED and claim.id)
    ):
        what = "text, value or id" if label in _NAMED else "text or value"
        yield where, f"a quoted or inferred claim says something: {what}"
    if label == "species" and not claim.says:
        yield where, "species is quoted or inferred, never unknown or na"
    if label == "biosample" and not claim.id:
        yield f"{where}.id", "missing: a biosample claim carries the id of the material"
    if claim.level is not None:
        if label not in factors:
            yield f"{where}.level", f"{label} is not a factor, so its claims name no level"
        elif claim.level not in factors[label].levels:
            yield f"{where}.level", f"{claim.level} is not a level of the factor {label}"


def _between_fields(reading: Reading) -> Iterator[tuple[str, str]]:
    """What no single field shows: a claim's mark, a pointer to nothing, a label a sample lacks."""
    by = reading.models.probabilities
    yield from passage_problems(reading.passages)
    rows = reading.rows()
    for label, factor in reading.factors.items():
        where = f"factors.{label}"
        yield from claim_problems(where, factor, reading.passages, probabilities_by=by)
        if len(set(factor.levels)) != len(factor.levels):
            yield f"{where}.levels", "a level is named twice"
        if factor.baseline is not None and factor.baseline not in factor.levels:
            yield f"{where}.baseline", f"{factor.baseline} is not one of the factor's levels"
        if not any(label in row for row in rows.values()):
            yield where, "no sample has this label"
            continue
        carried = {row[label].level for row in rows.values() if label in row}
        for level in dict.fromkeys(factor.levels):
            if level not in carried:
                yield f"{where}.levels", f"no sample is at the level {level}"
    written = [(f"shared.{label}", label, claim) for label, claim in reading.shared.items()]
    written += [
        (f"samples.{sample}.{label}", label, claim)
        for sample, own in reading.samples.items()
        for label, claim in own.items()
    ]
    for where, label, claim in written:
        yield from claim_problems(
            where, claim, reading.passages, marks=SAMPLE_MARKS, probabilities_by=by
        )
        yield from _label_problems(where, label, claim, reading.factors)
    # A row's claim replaces the shared one whole, its level too: a row that forgets the level
    # would leave its sample outside the factor without saying so.
    for sample, own in reading.samples.items():
        for label, claim in own.items():
            default = reading.shared.get(label)
            names_level = label in reading.factors and default is not None and default.level
            if names_level and claim.says and claim.level is None:
                yield (
                    f"samples.{sample}.{label}.level",
                    "missing: the shared claim this one replaces names a level",
                )
    # A gap is written as unknown or na, so a label that is absent was forgotten.
    varied = [label for label in reading.factors if any(label in row for row in rows.values())]
    for sample, row in rows.items():
        for label in dict.fromkeys((*LABELS, *varied)):
            if label not in row:
                yield (
                    f"samples.{sample}.{label}",
                    "missing: every sample has this label, in its own row or under shared",
                )


def read_reading(data: object) -> tuple[Reading | None, list[dict[str, str]]]:
    """One reading file's content as a reading, and what is wrong with it.

    The reading is ``None`` when the content is not one in this format at all. Use the reading only
    when the problems are empty.
    """
    if problems := format_problems(data, FORMAT):
        return None, problems
    try:
        reading = Reading.model_validate(data)
    except ValidationError as exc:
        return None, validation_problems(exc)
    return reading, [{"field": f, "reason": reason} for f, reason in _between_fields(reading)]


def check_reading(data: object) -> list[dict[str, str]]:
    """What is wrong with one reading file's content, each as ``{"field", "reason"}``; none if valid."""
    return read_reading(data)[1]
