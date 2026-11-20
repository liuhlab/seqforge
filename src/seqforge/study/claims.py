"""How a claim is backed, in every study file: the claim and passage models and their checks.

A **claim** is one mapping that says one thing and how it is backed. It holds exactly one backing
mark, ``quoted: [passage numbers]``, ``inferred: "how"`` or ``unknown: "note"``, and ``p``, the
chance it is right, which is ``None`` until a model has given one. A claim that is not quoted may
add ``see: [passage numbers]`` for the passages it rests on. A **passage** is one quote with where
it was read, written once per file under its number.

A file kind builds on these: it subclasses :class:`Claim` with what its own claims say, and asks
:func:`claim_problems` about each one, naming the marks it allows. Every check here returns
problems as ``(field, reason)`` pairs and reads the file alone.
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from typing import Any

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError

#: The backing marks every file kind allows. A kind may allow more of its own.
MARKS = ("quoted", "inferred", "unknown")

#: Why a backing mark's text cannot be blank, for the marks every file kind allows.
_BLANK = {
    "inferred": "blank: an inferred claim says how it was worked out",
    "unknown": "blank: an unknown claim has a short note",
}


class Strict(BaseModel):
    """A part of a study file: a field the format does not name is rejected.

    So is a value of another type than the format writes, such as a number in quotes.
    """

    model_config = ConfigDict(extra="forbid", strict=True)


class Passage(Strict):
    """One quote and where it was read: a record and its field, or a paper and its section."""

    record: str | list[str] | None = None
    field: str | None = None
    paper: str | None = None
    section: str | None = None
    quote: str = Field(min_length=1)


class Claim(Strict):
    """What every claim holds: one backing mark, and its probability."""

    quoted: list[int] | None = None
    inferred: str | None = None
    unknown: str | None = None
    see: list[int] | None = None
    #: Required, and ``None`` when no model has given one yet.
    p: float | None = Field(ge=0, le=1)


def field_path(loc: tuple[int | str, ...]) -> str:
    """A pydantic location as the file's reader would write it: ``series[0].p``."""
    out = ""
    for part in loc:
        out += f"[{part}]" if isinstance(part, int) else f".{part}" if out else str(part)
    return out


def _keys_written_twice(
    loader: yaml.SafeLoader, node: yaml.Node, path: tuple[int | str, ...]
) -> Iterator[dict[str, str]]:
    """Each key that one mapping of the file writes a second time, by where it is."""
    if isinstance(node, yaml.SequenceNode):
        for at, item in enumerate(node.value):
            yield from _keys_written_twice(loader, item, (*path, at))
    if not isinstance(node, yaml.MappingNode):
        return
    seen: list[object] = []
    for key_node, value in node.value:
        key = loader.construct_object(key_node, deep=True)
        here = (*path, key if isinstance(key, int | str) else str(key))
        if key in seen:
            line = key_node.start_mark.line + 1
            yield {"field": field_path(here), "reason": f"written twice: again on line {line}"}
        seen.append(key)
        yield from _keys_written_twice(loader, value, here)


def load(text: str) -> tuple[object, list[dict[str, str]]]:
    """A study file's text as content, and why it cannot be taken as content at all.

    YAML keeps the last of two equal keys and says nothing, which would drop a sample's row or a
    passage unseen. So a key written twice in one mapping is a problem here, named by its field.
    """
    loader = yaml.SafeLoader(text)
    try:
        node = loader.get_single_node()
        if node is None:
            return None, []
        if twice := list(_keys_written_twice(loader, node, ())):
            return None, twice
        return loader.construct_document(node), []  # type: ignore[no-untyped-call]
    except yaml.YAMLError as exc:
        return None, [{"field": "(file)", "reason": f"not YAML: {' '.join(str(exc).split())}"}]
    finally:
        loader.dispose()  # type: ignore[no-untyped-call]


def format_problems(data: object, expected: int) -> list[dict[str, str]]:
    """Why ``data`` is not a file of format ``expected`` at all; empty when it is one.

    Asked first and alone, since a file of another format is not this format with mistakes in it.
    """
    if not isinstance(data, Mapping):
        return [{"field": "(file)", "reason": "not a mapping of fields"}]
    found = data.get("format")
    if type(found) is not int or found != expected:
        said = "missing" if "format" not in data else f"found {found!r}"
        return [{"field": "format", "reason": f"expected {expected}, {said}"}]
    return []


def validation_problems(exc: ValidationError) -> list[dict[str, str]]:
    """A model's validation errors as problems, each naming its field."""

    def reason(error: Mapping[str, Any]) -> str:
        if error["type"] == "missing":
            return "missing"
        if error["type"] == "extra_forbidden":
            return "not a field of the format"
        if error["type"] == "dict_type":
            return "not a mapping"
        return str(error["msg"])

    return [{"field": field_path(e["loc"]), "reason": reason(e)} for e in exc.errors()]


def passage_problems(passages: Mapping[int, Passage]) -> Iterator[tuple[str, str]]:
    """A passage that does not say where it was read, in one of the two ways a source is written."""
    for number, passage in passages.items():
        from_record = passage.record and passage.field and not (passage.paper or passage.section)
        from_paper = passage.paper and passage.section and not (passage.record or passage.field)
        if not (from_record or from_paper):
            yield (
                f"passages[{number}]",
                "a passage is from a record and its field, or from a paper and its section",
            )


def claim_problems(
    where: str,
    claim: Claim,
    passages: Mapping[int, Passage],
    *,
    marks: tuple[str, ...] = MARKS,
    probabilities_by: str | None,
) -> Iterator[tuple[str, str]]:
    """What is wrong with one claim, written at ``where``, over the file's ``passages``.

    ``marks`` are the backing marks its file kind allows, and ``probabilities_by`` the model the
    file names for its probabilities, or ``None`` when it names none.
    """
    given = [mark for mark in marks if getattr(claim, mark) is not None]
    if len(given) != 1:
        said = "none" if not given else " and ".join(given)
        yield where, f"a claim has exactly one of {', '.join(marks)}; this one has {said}"
    if claim.quoted is not None and not claim.quoted:
        yield f"{where}.quoted", "a quoted claim points to at least one passage"
    for mark in given:
        said = getattr(claim, mark)
        if isinstance(said, str) and not said.strip():
            yield f"{where}.{mark}", _BLANK.get(mark, "blank: a short text says why")
    if claim.see is not None and claim.quoted is not None:
        yield (
            f"{where}.see",
            "a quoted claim lists its passages under quoted, with no see beside it",
        )
    for name, numbers in (("quoted", claim.quoted), ("see", claim.see)):
        for number in numbers or []:
            if number not in passages:
                yield f"{where}.{name}", f"passage {number} does not exist"
    if claim.p is not None and probabilities_by is None:
        yield f"{where}.p", "a probability is given, so models.probabilities names who gave it"
