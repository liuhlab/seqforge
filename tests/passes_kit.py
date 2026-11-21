"""The test kit of `seqforge pass` and `seqforge review`, imported by `test_passes.py` alone.

The pass and review commands are exercised on the run engine's test-only kind, apart from any
production kind: one writer role that writes a text in capitals, over
fixture texts, behind liulab-llms's fake gateway. The review repo is :class:`FakeGitHub`, in memory.
It lives here and not in ``conftest.py`` so that no other test session imports liulab-llms.
"""

from __future__ import annotations

import dataclasses
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from labllm import Held, Inputs, Model, QueueEntry, Role, Unit, UnitKind
from labllm.testing import Price

from seqforge.passes.github import GitHubError, Issue
from seqforge.passes.review import Candidate, Item, ReviewForm

#: The texts the echo kind's units read, by key, in the kind's default order.
ECHO_TEXTS = {"u1": "alpha", "u2": "beta", "u3": "gamma", "u4": "delta"}
ECHO_SCHEMA = {
    "type": "object",
    "properties": {"word": {"type": "string"}},
    "required": ["word"],
    "additionalProperties": False,
}


ECHO_WEAK = Model("test", "echo-weak", max_output_tokens=50)
ECHO_STRONG = Model("test", "echo-strong", max_output_tokens=50)
#: Input is free and output costs a tenth of a cent a character, so each call holds back five cents
#: against the ceiling: a ceiling of six cents lets exactly one call through.
ECHO_PRICES = {
    m.name: Price(input_usd_per_token=0.0, output_usd_per_token=0.001)
    for m in (ECHO_WEAK, ECHO_STRONG)
}


def echo_writer(form: dict[str, Any]) -> str:
    """Plays the writer. The strong model answers every text right; the weak one answers alpha right,
    beta and delta wrong (unsettled) and gamma with no word (refused)."""
    text = form["messages"][-1]["content"]
    if form["model"] == ECHO_STRONG.name:
        return json.dumps({"word": text.upper()})
    return json.dumps({"word": {"alpha": "ALPHA", "gamma": ""}.get(text, text[::-1])})


def echo_unit_kind(texts: dict[str, str], *, name: str = "echo") -> UnitKind:
    """The liulab-llms kind: write the unit's text in capitals, checked by code."""

    def load(key: str) -> Inputs:
        if key not in texts:
            raise Held(f"no text for {key}")
        return Inputs(texts[key], tag=f"text={texts[key]}")

    def run(u: Unit) -> dict[str, str]:
        word = json.loads(u.chat("writer", user=u.inputs.data, schema=ECHO_SCHEMA).text)["word"]
        if not word:
            u.refuse("answered", "the writer gave no word")
        return {"word": word}

    def in_capitals(inputs: Inputs, answer: dict[str, str]) -> str | None:
        wanted = inputs.data.upper()
        return None if answer["word"] == wanted else f"{answer['word']!r} is not {wanted!r}"

    return UnitKind(
        name=name,
        version="1",
        roles={"writer": Role("chat", "writes a text in capitals")},
        load=load,
        run=run,
        answer_schema=ECHO_SCHEMA,
        answer_checks=(in_capitals,),
    )


def echo_records(inputs: Inputs) -> str:
    """The echo unit's one record, its text, with a link."""
    return f"- The text [{inputs.data}](https://en.wiktionary.org/wiki/{inputs.data})"


def echo_show(answer: Any) -> str:
    return f"The word `{answer['word']}`"


def echo_candidates(entry: QueueEntry, inputs: Inputs) -> list[Candidate]:
    """The one starting answer the echo kind's code offers: the text, capitalised."""
    return [Candidate("The text, capitalised by the kind's code", {"word": inputs.data.upper()})]


def echo_items(entry: QueueEntry, inputs: Inputs) -> list[Item]:
    """The echo form's one per-item question: how the word is written."""
    options = (("capitals", "Write it in capitals"), ("lower", "Write it in lower case"))
    return [Item("case", "How is the word written?", options)]


def echo_place(start: Any, choices: Mapping[str, str]) -> dict[str, str]:
    """The starting answer, with the word written as the person ticked; unticked keeps it."""
    word = start["word"]
    written = {"capitals": word.upper(), "lower": word.lower()}
    return {"word": written.get(choices.get("case", ""), word)}


#: The echo kind's two-question review form: a starting answer, and how the word is written.
ECHO_FORM = ReviewForm(
    records=echo_records,
    show=echo_show,
    place=echo_place,
    drop_reasons=("not a word", "other"),
    candidates=echo_candidates,
    items=echo_items,
)


def echo_priority(store: Any, entry: QueueEntry) -> tuple[bool, str]:
    """The echo kind reviews refused units first, then the rest, each in key order."""
    return (entry.ending != "refused", entry.key)


@dataclass
class FakeGitHub:
    """The review repo in memory: what the review commands reach in place of GitHub.

    It starts as the real repo was made, with the ``hand-check`` label only. Like GitHub, it refuses
    a label it already has. It also refuses an issue naming a label it lacks, so a command that
    forgot to create its labels goes red here. ``filed`` holds every issue, in the order filed;
    ``closers`` who last closed each, and ``comments`` each issue's comments, oldest first.
    """

    repo: str = "lab/seqforge-review"
    label_table: dict[str, tuple[str, str]] = field(
        default_factory=lambda: {"hand-check": ("ededed", "The benchmark's hand-check")}
    )
    filed: list[Issue] = field(default_factory=list)
    closers: dict[int, str] = field(default_factory=dict)
    comments: dict[int, list[str]] = field(default_factory=dict)

    def labels(self) -> set[str]:
        return set(self.label_table)

    def create_label(self, name: str, *, color: str, description: str) -> None:
        if name in self.label_table:
            raise GitHubError(f"label {name!r} already exists")
        self.label_table[name] = (color, description)

    def issues(self, label: str) -> list[Issue]:
        return [issue for issue in self.filed if label in issue.labels]

    def create_issue(self, *, title: str, body: str, labels: Sequence[str]) -> Issue:
        if missing := sorted(set(labels) - set(self.label_table)):
            raise GitHubError(f"no label {', '.join(missing)} in {self.repo}")
        number = len(self.filed) + 1
        url = f"https://github.com/{self.repo}/issues/{number}"
        issue = Issue(number, url, title, body, tuple(labels), "open", None)
        self.filed.append(issue)
        return issue

    def closer(self, number: int) -> str:
        return self.closers[number]

    def comment(self, number: int, body: str) -> None:
        self.comments.setdefault(number, []).append(body)

    def reopen(self, number: int) -> None:
        issue = self.filed[number - 1]
        self.filed[number - 1] = dataclasses.replace(issue, state="open", state_reason="reopened")

    def add_labels(self, number: int, labels: Sequence[str]) -> None:
        if missing := sorted(set(labels) - set(self.label_table)):
            raise GitHubError(f"no label {', '.join(missing)} in {self.repo}")
        issue = self.filed[number - 1]
        kept = tuple(dict.fromkeys((*issue.labels, *labels)))
        self.filed[number - 1] = dataclasses.replace(issue, labels=kept)

    def close(self, number: int, reason: str, *, by: str = "a-reviewer") -> None:
        """``by`` closes issue ``number``, as ``completed`` or ``not_planned``."""
        closed = dataclasses.replace(self.filed[number - 1], state="closed", state_reason=reason)
        self.filed[number - 1] = closed
        self.closers[number] = by
