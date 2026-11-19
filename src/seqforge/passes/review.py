"""The review form: a queued unit as an issue a person answers by ticking, and the ticks read back.

A unit in a kind's review queue becomes an issue a person answers by ticking boxes, and the ticks
become an answer again. What is about a kind's own units is the kind's :class:`ReviewForm`: how its
records and answers show, which starting answers its code offers, which per-item questions it asks,
how a per-item choice changes an answer, and its drop reasons. The rest is here and the same for every kind: the
issue's title, sections and labels, the tick boxes, and the hidden marker.

The marker lets the answer be read back with no task. Opening a task takes its hold, which a
running pass keeps, so the issue itself carries what read-back needs: the kind, the key, the inputs
tag of the answer shown, and each starting answer with the pass that gave it.

The contract between filing and reading back is the body's text:

- the marker, one line: ``<!-- seqforge-review {JSON} -->``, the JSON holding ``kind``, ``key``,
  ``inputs_tag`` and ``starts`` (tick id -> ``{"pass_id", "answer"}``; ``pass_id`` is null for a
  candidate the kind's code offers);
- one tick box per line, ``- [ ] <label> <!-- seqforge <group> [<ids>] -->``, where the group is
  ``start <id>`` (the id a key of ``starts``), ``item <item id> <option id>``, or ``drop`` (the
  label is the reason). GitHub ticks a box by turning its ``[ ]`` into ``[x]``.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from labllm import Inputs, Model, QueueEntry


@dataclass(frozen=True)
class Candidate:
    """A starting answer the kind's code offers beside the passes' own, with its tick box's label."""

    label: str
    answer: Any


@dataclass(frozen=True)
class Item:
    """One per-item question: which one of ``options`` (``(id, label)`` pairs) an item takes.

    Ids are what reading back sees: letters, digits and ``_.:``, joined by single hyphens.
    """

    id: str
    question: str
    options: tuple[tuple[str, str], ...]


def _no_candidates(entry: QueueEntry, inputs: Inputs) -> list[Candidate]:
    return []


def _no_items(entry: QueueEntry, inputs: Inputs) -> list[Item]:
    return []


def _keep_start(start: Any, choices: Mapping[str, str]) -> Any:
    return start


@dataclass(frozen=True)
class ReviewForm:
    """A kind's own part of its review issues.

    ``records`` shows the unit's records, with links, as Markdown; ``show`` shows one answer.
    ``candidates`` are starting answers the kind's code offers beside each pass's answer, and
    ``items`` the per-item questions, both from the queue entry and the unit's inputs. ``place``
    makes the answer from the ticked starting answer and the option ticked for each item (an item
    left unticked is absent), or raises :class:`Unreadable` saying why it cannot. ``drop_reasons``
    are the reasons a person can tick to drop the unit.
    """

    records: Callable[[Inputs], str]
    show: Callable[[Any], str]
    drop_reasons: tuple[str, ...]
    candidates: Callable[[QueueEntry, Inputs], Sequence[Candidate]] = _no_candidates
    items: Callable[[QueueEntry, Inputs], Sequence[Item]] = _no_items
    place: Callable[[Any, Mapping[str, str]], Any] = _keep_start


#: The label every issue of a kind carries, and its colour.
KIND_LABEL, KIND_COLOR = "review:{}", "1d76db"
#: The label for each check the current answer failed, and its colour.
CHECK_LABEL, CHECK_COLOR = "check:{}", "d93f0b"
#: The label on a closed issue whose unit's inputs changed after filing, so nothing was stored from
#: it; reading back passes over it from then on.
INPUTS_CHANGED_LABEL, INPUTS_CHANGED_COLOR = "inputs-changed", "cfd3d7"


def labels_for(entry: QueueEntry) -> dict[str, tuple[str, str]]:
    """The issue's labels, in order, each with the colour and description it is created with."""
    labels = {KIND_LABEL.format(entry.kind): (KIND_COLOR, f"A unit of the {entry.kind} kind")}
    for check in entry.failed:
        labels[CHECK_LABEL.format(check.name)] = (CHECK_COLOR, f"The answer failed {check.name}")
    return labels


def issue_for(form: ReviewForm, entry: QueueEntry, inputs: Inputs) -> tuple[str, str]:
    """The title and body of the issue that asks a person to review ``entry``.

    ``inputs`` are the unit's, loaded for the inputs tag ``entry`` holds.
    """
    answered = [o for o in entry.outcomes if o.answer is not None]
    starts = {f"pass-{o.stamp.pass_id}": (f"Pass {o.stamp.pass_id}'s answer", o) for o in answered}
    candidates = {f"candidate-{n}": c for n, c in enumerate(form.candidates(entry, inputs), 1)}
    marker = {
        "kind": entry.kind,
        "key": entry.key,
        "inputs_tag": entry.inputs_tag,
        "starts": {
            **{i: {"pass_id": o.stamp.pass_id, "answer": o.answer} for i, (_, o) in starts.items()},
            **{i: {"pass_id": None, "answer": c.answer} for i, c in candidates.items()},
        },
    }
    # Every character that could close the comment early is escaped; JSON reads it back the same.
    text = json.dumps(marker, sort_keys=True).replace("<", "\\u003c").replace(">", "\\u003e")
    lines = [
        f"<!-- seqforge-review {text} -->",
        "",
        f"Filed by `seqforge review file`: unit `{entry.key}` of kind {entry.kind}.",
        "",
        "## Records",
        "",
        form.records(inputs),
        "",
        "## What the models answered",
    ]
    for outcome in entry.outcomes:
        stamp = outcome.stamp
        models = ", ".join(f"`{_model_text(r, m)}`" for r, m in sorted(stamp.models.items()))
        lines += [
            "",
            f"### Pass {stamp.pass_id}: {outcome.ending}",
            "",
            f"Models: {models}. Stamp: kind version {stamp.kind_version}, inputs "
            f"`{stamp.inputs_tag}`, seqforge {stamp.repo_version}, "
            f"liulab-llms {stamp.labllm_version}.",
        ]
        if outcome.answer is not None:
            lines += ["", form.show(outcome.answer)]
        if failed := [c for c in outcome.checks if not c.passed]:
            lines += ["", *(f"- ✗ {c.name}: {c.message}" for c in failed)]
    lines += [
        "",
        "## Your answer: tick, then close the issue",
        "",
        "**Start from** one answer:",
        "",
        *(_tick(label, "start", i) for i, (label, _) in starts.items()),
        *(_tick(c.label, "start", i) for i, c in candidates.items()),
    ]
    for item in form.items(entry, inputs):
        lines += [
            "",
            f"**{item.question}** Tick one, or none to keep the starting answer's.",
            "",
            *(_tick(label, "item", item.id, option) for option, label in item.options),
        ]
    lines += [
        "",
        "**Or drop it**, because:",
        "",
        *(_tick(reason, "drop") for reason in form.drop_reasons),
        "",
        "Close the issue as completed to store your answer, or as not planned to drop the unit.",
    ]
    current = next(o for o in reversed(entry.outcomes) if o.ending == entry.ending)
    title = f"Review {entry.kind}: {entry.key}, {entry.ending} after pass {current.stamp.pass_id}"
    return title, "\n".join(lines) + "\n"


#: What a tick box's ids may be: no spaces, and nothing that could end the comment they sit in.
_ID = re.compile(r"[A-Za-z0-9_.:]+(?:-[A-Za-z0-9_.:]+)*")


def _tick(label: str, group: str, *ids: str) -> str:
    for one in ids:
        if not _ID.fullmatch(one):
            raise ValueError(f"a review form's id {one!r} is not letters, digits and _.:-")
    if "\n" in label or "<!--" in label:
        raise ValueError(f"a review form's tick label {label!r} is not one plain line")
    return f"- [ ] {label} <!-- seqforge {' '.join((group, *ids))} -->"


def _model_text(role: str, model: Model) -> str:
    """``ROLE=PROVIDER/NAME[,KEY=VALUE...]``, as ``pass start --model`` takes it.

    It names each setting that differs from liulab-llms's default.
    """
    from labllm import Model

    plain = Model(model.provider, model.name)
    text = f"{role}={model.provider}/{model.name}"
    for field, key in (
        ("thinking", "thinking"),
        ("effort", "effort"),
        ("temperature", "temperature"),
        ("max_output_tokens", "max-output-tokens"),
    ):
        value = getattr(model, field)
        if value != getattr(plain, field):
            shown = ("on" if value else "off") if isinstance(value, bool) else value
            text += f",{key}={shown}"
    return text


class Unreadable(ValueError):
    """The ticks make no answer. The message says why, in words for the person who ticked."""


#: Why an issue whose marker is gone or unreadable cannot be read back.
LOST_MARKER = "The issue has lost its seqforge marker, so its ticks cannot be read."


@dataclass(frozen=True)
class Start:
    """A starting answer the issue offered, and the pass that gave it (None: the kind's code)."""

    pass_id: int | None
    answer: Any


@dataclass(frozen=True)
class Marker:
    """What an issue's hidden marker holds: whose answer the issue asks for, and its starts."""

    kind: str
    key: str
    inputs_tag: str
    starts: dict[str, Start]


_MARKER = re.compile(r"^<!-- seqforge-review (?P<json>\{.*\}) -->[ \t]*$", re.MULTILINE)
_TICKED = re.compile(
    r"^[ \t]*[-*] \[(?P<mark>[ xX])\] (?P<label>.*?)[ \t]*<!-- seqforge (?P<ids>[^>]*?) -->[ \t]*$",
    re.MULTILINE,
)


def read_marker(body: str) -> Marker | None:
    """The marker in an issue's body, or None if it has none it can read."""
    found = _MARKER.search(body)
    if found is None:
        return None
    try:
        raw = json.loads(found["json"])
        starts = {i: Start(s["pass_id"], s["answer"]) for i, s in raw["starts"].items()}
        return Marker(raw["kind"], raw["key"], raw["inputs_tag"], starts)
    except (ValueError, KeyError, TypeError, AttributeError):
        return None


def _ticked(body: str) -> list[tuple[list[str], str]]:
    """Each ticked box: its group and ids, and its label."""
    return [
        (found["ids"].split(), found["label"])
        for found in _TICKED.finditer(body)
        if found["mark"] in "xX"
    ]


def read_answer(form: ReviewForm, body: str) -> tuple[Any, int | None]:
    """The answer an issue's ticks make, and the pass whose answer the person started from.

    The pass is None when they started from an answer the kind's code offered. Raises
    :class:`Unreadable` when the body has lost its marker, no starting answer or more than one is
    ticked, an item has more than one option ticked, or the kind's ``place`` cannot make
    an answer. The answer is not checked here: storing it runs the kind's answer checks.
    """
    marker = read_marker(body)
    if marker is None:
        raise Unreadable(LOST_MARKER)
    ticked = _ticked(body)
    starts = [ids[1] for ids, _ in ticked if ids[0] == "start" and len(ids) == 2]
    if not starts:
        raise Unreadable("Tick a starting answer.")
    if len(starts) > 1:
        raise Unreadable(f"Tick only one starting answer, not {len(starts)}.")
    start = marker.starts.get(starts[0])
    if start is None:
        raise Unreadable(f"The starting answer {starts[0]} is not one this issue offered.")
    choices: dict[str, list[str]] = {}
    for ids, _ in ticked:
        if ids[0] == "item" and len(ids) == 3:
            choices.setdefault(ids[1], []).append(ids[2])
    if twice := sorted(item for item, options in choices.items() if len(options) > 1):
        raise Unreadable(f"Tick at most one option for {', '.join(twice)}.")
    return form.place(start.answer, {item: o for item, (o,) in choices.items()}), start.pass_id


def read_drop(body: str) -> str:
    """The note a drop is stored with: each ticked reason, as its box reads, joined by ``; ``.

    Raises :class:`Unreadable` when no reason is ticked.
    """
    reasons = [label for ids, label in _ticked(body) if ids == ["drop"]]
    if not reasons:
        raise Unreadable("Tick a reason to drop it.")
    return "; ".join(reasons)
