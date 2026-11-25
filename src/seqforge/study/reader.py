"""The reading job's kind: a writer lists a study's values, Jev picks each sample's, and code writes the reading.

Importing this module registers the kind, so ``seqforge pass start study-reading`` runs it on the
run engine. One unit is one study, keyed by the GEO Series it was picked by, and read from the
standard set's own bounds file: the records of the Series it keeps, their sequenced samples, and
its papers, each in full where the paper store holds the full text and by its abstract otherwise.

The chain, for one study:

1. **The writer** (DeepSeek Flash, thinking on) reads the records and the papers and writes the
   open lists: the labels the study varied on purpose with their levels and baseline, every value
   each label takes, each with a quote, and a short search name per value. A study whose records
   name a pull-down library strategy also gets that assay's guide, and its writer lists which
   control goes with which pull-down. An answer that cannot be used is asked for once more.
2. **Jev**, a model that answers closed questions with probabilities, answers every closed
   question: whether each listed factor was varied on purpose (asked plain and negated), its
   baseline (the options in order and reversed), whether its levels are ordered, what a label with
   no listed value is, and where the libraries' material came from. Then, for each sample, its
   level on each factor and its value on each label, asked twice, the options in order and
   reversed.
3. **Code** finds every quote in its record or paper; Jev judges whether each found passage backs
   its claim, or speaks against it, and checks each listed tie of a control to a pull-down.
4. **liulab-data's ontology search** gives candidates for each value's search name, and Jev picks
   one or none. Code maps the species from the record's taxonomy id and ``healthy`` to its PATO
   id, each named through the same store, so every id in the file comes from a snapshot it names.
5. **Code** writes the reading, drops a factor level no sample is at, and runs the format check.

A unit ends **refused** when the writer's answer cannot be used after one retry or the reading
fails the format check, **unsettled** when Jev's two askings disagree on whether a factor was
varied on purpose, on its baseline or on any sample's level, and **settled** otherwise.
:func:`write_readings` writes each answer as a reading file.
"""

from __future__ import annotations

import datetime
import hashlib
import threading
from collections import defaultdict
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml
from labllm import Held, Inputs, Model, Outcome, Role, Task, Unit, UnitKind

from ..passes import SetupRefused
from ..passes.kinds import Kind, register
from ..passes.review import ReviewForm
from ..standard_set import BOUNDS, default_set_dir, pick_entries
from . import sources as src
from .claims import load
from .group import SERIES, paper_reader
from .lists import (
    CODE_LABELS,
    LABELS,
    MEANING,
    NUMERIC,
    RETRY,
    FactorList,
    Label,
    Lists,
    Value,
    guide_text,
    guides_for,
    instructions,
    parse_lists,
    snake,
)
from .ontology_search import (
    Candidate,
    OntologySearch,
    open_store_search,
    snapshot_source,
    vocabulary,
)
from .publish import model_name, write_files
from .reading import FORMAT, check_reading

KIND_NAME = "study-reading"
#: Raise it when an instruction, a question, a guide or a rule of writing the reading changes.
KIND_VERSION = "2026.11.1"
#: The roles, and the models that fill them unless a pass names others.
WRITER_ROLE, JEV_ROLE = "writer", "jev"
WRITER = Model("deepseek", "deepseek-flash", thinking=True, max_output_tokens=40000)
JEV = Model("typesafe", "jev-1.14.0")
#: A yes/no answer is yes when Jev's p(yes) is at least this.
YES = 0.5
#: A searched term is written only when Jev's pick of it has at least this probability.
TERM_AT = 0.8
#: The most values of one label Jev chooses among. A study whose writer lists more for one label
#: is refused, naming the label: a value cut from the options would be a quiet loss, and asking in
#: batches would give picks whose probabilities do not compare.
MOST_VALUES = 25
#: The Jev requests one study sends at once.
AT_ONCE = 4
#: The most characters of a description Jev reads, for the study and for one sample.
_STUDY_CAP, _SAMPLE_CAP = 90_000, 20_000
#: The ids code maps on its own: healthy, as the reading format names it.
HEALTHY = "PATO:0000461"
#: The words ``sex`` takes as its term, which the format defines itself.
SEX_WORDS = ("female", "male", "hermaphrodite", "mixed")
#: The search names code looks the species' adult stage up by, the first whole-text match taken.
ADULT_STAGE = ("adult stage", "adult")
#: The labels whose gap Jev is told about, when a label has no listed value.
_NA_EXAMPLE = {
    "target": "an assay with no pull-down, or a control library",
    "control_type": "a library that is not a background control",
    "cell_line": "tissue taken from an animal",
    "cell_type": "whole tissue with no cell type isolated",
    "treatment": "no treatment applied in the study",
    "dose": "no treatment",
    "duration": "no treatment",
    "strain": "a species with no strains, such as human",
}

_P = {"type": "number", "minimum": 0, "maximum": 1}
ANSWER_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "reading": {"type": "object"},
        "askings": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "about": {"type": "string"},
                    "how": {"enum": ["negated", "reversed"]},
                    "first": {},
                    "second": {},
                },
                "required": ["about", "how", "first", "second"],
                "additionalProperties": False,
            },
        },
        "notes": {"type": "object"},
    },
    "required": ["reading", "askings", "notes"],
    "additionalProperties": False,
}


# ---------------------------------------------------------------- Jev's answers


def _p(answer: Any) -> float | None:
    """A yes/no answer's p(yes), or ``None`` when Jev gave none."""
    value = (answer or {}).get("noul") if isinstance(answer, Mapping) else None
    return float(value) if isinstance(value, int | float) else None


def _pick(answer: Any) -> tuple[str | None, float | None]:
    """A choice answer's pick and its probability, or ``(None, None)`` when Jev gave none."""
    if not isinstance(answer, Mapping):
        return None, None
    choice = answer.get("choice")
    p = (answer.get("probabilities") or {}).get(choice)
    return (str(choice) if choice is not None else None), (
        float(p) if isinstance(p, int | float) else None
    )


def _ask(u: Unit, description: str, questions: Mapping[str, Any]) -> dict[str, Any]:
    """Jev's answers to ``questions``, by name; none asked, no call."""
    if not questions:
        return {}
    answers = u.jev(JEV_ROLE, description=description, questions=questions).answers
    return dict(answers) if isinstance(answers, Mapping) else {}


def _choice(instructions: str, options: Mapping[str, str], *, reverse: bool = False) -> Any:
    items = list(options.items())
    return {
        "type": "choice",
        "instructions": instructions,
        "criteria": dict(reversed(items) if reverse else items),
    }


def _noul(instructions: str) -> dict[str, str]:
    return {"type": "noul", "instructions": instructions}


def _r(p: float | None) -> float | None:
    return None if p is None else round(p, 3)


# ---------------------------------------------------------------- the writer's lists


def _write_lists(u: Unit, user: str, system: str) -> tuple[Lists, int]:
    """The writer's lists, and how many answers it took. A second broken answer refuses the unit."""
    reply = u.chat(WRITER_ROLE, system=system, user=user, schema={"type": "object"})
    lists, broken = parse_lists(reply.text)
    if lists is not None:
        return lists, 1
    again = u.chat(
        WRITER_ROLE,
        system=system,
        user=user + RETRY.format(reason=broken),
        schema={"type": "object"},
    )
    lists, still = parse_lists(again.text)
    if lists is None:
        u.refuse("writer_answer", f"the writer's answer could not be used twice: {still}")
    return lists, 2


@dataclass
class _Factor:
    """A listed factor as the chain carries it: the writer's list and Jev's answers."""

    label: str
    listed: FactorList
    levels: list[Value]
    names: list[str]
    p_varied: float | None = None
    p_varied_negated: float | None = None
    baseline: str | None = None
    baseline_reversed: str | None = None
    p_ordered: float | None = None


@dataclass
class _Lists:
    """The writer's lists, made usable: factors by label, every label's values, its gap."""

    factors: list[_Factor]
    labels: dict[str, Label]
    ties: list[Any] = field(default_factory=list)


def _tidy(lists: Lists, samples: set[str]) -> _Lists:
    """Fold the writer's answer into the shape the chain asks about.

    A label code writes is never a factor, and neither is ``target``: its levels become values.
    A factor's label lists no values of its own, since its levels say them. A value that says
    nothing is dropped, and so is a tie that names a library outside the study's samples.
    """
    labels: dict[str, Label] = {label: lists.labels.get(label) or Label() for label in LABELS}
    for name, label in lists.extra_labels.items():
        own = snake(name)
        if own and own not in labels and own not in CODE_LABELS:
            labels[own] = label
    for label in labels.values():
        label.values = [_said(v) for v in label.values if _says(v)]
    factors: list[_Factor] = []
    for listed in lists.factors:
        name = snake(listed.label)
        levels = [_said(v) for v in listed.levels if _says(v)]
        if not name or name in CODE_LABELS or not levels:
            continue
        if name == "target":
            labels.setdefault(name, Label()).values += levels
            continue
        names = [str(v.name or v.text) for v in levels]
        factors.append(_Factor(name, listed, levels, names))
        labels.setdefault(name, Label())
    for factor in factors:
        labels[factor.label].values = []
    ties = [
        t
        for t in lists.ties
        if t.control in samples and all(p in samples for p in t.pulldowns) and t.quote
    ]
    return _Lists(factors, labels, ties)


def _says(value: Value) -> bool:
    return bool(value.text.strip()) or value.value is not None


def _said(value: Value) -> Value:
    """A value with its text filled from its number when the writer gave only the number."""
    if not value.text.strip():
        number = (
            value.value if not isinstance(value.value, list) else "-".join(map(str, value.value))
        )
        value.text = f"{number} {value.unit or ''}".strip()
    return value


def _number(value: float | list[float] | None) -> Any:
    """A number as the reading writes it: whole numbers without a decimal point."""
    if isinstance(value, list):
        return [_number(v) for v in value]
    if isinstance(value, float) and value.is_integer():
        return int(value)
    return value


def _glossary(lists: _Lists, kept: list[_Factor]) -> str:
    """The listed values, each with its quote, as Jev reads them beside a sample's record."""
    out = ["Values listed for this study, each with the words that say it:"]
    for factor in kept:
        out.append(f"Factor {factor.label}:")
        for level in factor.levels:
            said = f' ("{level.quote.text[:200]}")' if level.quote else ""
            out.append(f"  - {level.text}{said}")
    for label, listed in lists.labels.items():
        if listed.values:
            texts = [
                v.text
                + (f' ("{v.quote.text[:150]}")' if v.quote and v.quote.text != v.text else "")
                for v in listed.values
            ]
            out.append(f"{label}: " + " | ".join(texts))
    return "\n".join(out)


# ---------------------------------------------------------------- Jev, the study


def _ask_study(u: Unit, text: str, lists: _Lists) -> dict[str, Any]:
    """Jev's answers about the study: each factor, each label with no value, the donors."""
    questions: dict[str, Any] = {}
    for i, f in enumerate(lists.factors):
        levels = "; ".join(f"{n} ({v.text})" for n, v in zip(f.names, f.levels, strict=True))
        levels = levels[:1500]
        questions[f"f{i}_varied"] = _noul(
            f"Did the study vary {f.label} on purpose between its sequenced samples, to compare "
            f"these levels: {levels}?"
        )
        questions[f"f{i}_varied_negated"] = _noul(
            f"Is {f.label} (values: {levels}) only a description, a replicate or batch label, or "
            "a restatement of another factor, rather than something the study varied on purpose "
            "to compare?"
        )
        options = {f"L{j}": name for j, name in enumerate(f.names)}
        options["no_reference"] = (
            "the study compares the levels with each other; none is a reference"
        )
        asked = (
            f"Which level of {f.label} is the reference the study compares the others with, such "
            "as untreated, wild type, control, young or time zero?"
        )
        questions[f"f{i}_baseline"] = _choice(asked, options)
        questions[f"f{i}_baseline_reversed"] = _choice(asked, options, reverse=True)
        questions[f"f{i}_ordered"] = _noul(
            f"Do the levels of {f.label} ({levels}) run along a scale, such as a time course, a "
            "dose series or a series of ages?"
        )
    for label, listed in lists.labels.items():
        if listed.values or label in {f.label for f in lists.factors}:
            continue
        example = _NA_EXAMPLE.get(label)
        meaning = MEANING.get(label, "a label of the study's own")
        questions[f"gap_{label}"] = _choice(
            f"For the samples of this study, what about their {label.replace('_', ' ')} "
            f"({meaning})?",
            {
                "na": "it does not apply to these samples" + (f": {example}" if example else ""),
                "unknown": "it applies, but nothing in the text says it",
            },
        )
    questions["donor"] = _choice(
        "Where did each sequenced library's material come from?",
        {
            "one_each": "each library from its own individual animal or person",
            "shared": "some libraries share one individual",
            "pool": "each library pools many individuals",
            "cell_line": "a cell line or a culture, no individual",
            "cannot_tell": "the text does not say",
        },
    )
    answers = _ask(u, text, questions)
    for i, f in enumerate(lists.factors):
        f.p_varied = _p(answers.get(f"f{i}_varied"))
        f.p_varied_negated = _p(answers.get(f"f{i}_varied_negated"))
        f.baseline = _level(f, _pick(answers.get(f"f{i}_baseline"))[0])
        f.baseline_reversed = _level(f, _pick(answers.get(f"f{i}_baseline_reversed"))[0])
        f.p_ordered = _p(answers.get(f"f{i}_ordered"))
    return answers


def _level(factor: _Factor, choice: str | None) -> str | None:
    """The level a choice names, or ``None`` for no reference or no answer."""
    if (
        choice
        and choice.startswith("L")
        and choice[1:].isdigit()
        and int(choice[1:]) < len(factor.names)
    ):
        return factor.names[int(choice[1:])]
    return None


def _kept(lists: _Lists) -> list[_Factor]:
    """The factors Jev says were varied on purpose; of two on one label, the surer one.

    A listed factor Jev does not keep stays a label: its levels become the label's values.
    """
    by_label: dict[str, _Factor] = {}
    for f in lists.factors:
        surer = f.label not in by_label or (f.p_varied or 0) > (by_label[f.label].p_varied or 0)
        if f.p_varied is not None and f.p_varied >= YES and surer:
            by_label[f.label] = f
    kept = list(by_label.values())
    for f in lists.factors:
        if f not in kept and f.label not in by_label:
            have = {v.text for v in lists.labels[f.label].values}
            lists.labels[f.label].values += [v for v in f.levels if v.text not in have]
    return kept


# ---------------------------------------------------------------- Jev, each sample


def _sample_questions(
    kept: list[_Factor], labels: Mapping[str, Label], *, reverse: bool
) -> dict[str, Any]:
    questions: dict[str, Any] = {}
    for i, f in enumerate(kept):
        options = {f"L{j}": f"`sample` has {f.label} = {v.text}" for j, v in enumerate(f.levels)}
        options["unknown"] = f"`sample`'s text does not say its {f.label}"
        options["na"] = f"{f.label} does not apply to `sample`"
        questions[f"lvl{i}"] = _choice(
            f"Which {f.label} does `sample` have?", options, reverse=reverse
        )
    for label, listed in labels.items():
        if not listed.values:
            continue
        words = label.replace("_", " ")
        options = {f"v{j}": f"{words} is {v.text}" for j, v in enumerate(listed.values)}
        options["unknown"] = f"`sample`'s text and the listed words do not say its {words}"
        example = _NA_EXAMPLE.get(label)
        options["na"] = f"{words} does not apply to `sample`" + (f" ({example})" if example else "")
        meaning = MEANING.get(label, "a label of the study's own")
        questions[f"lab_{label}"] = _choice(
            f"What is the {words} of `sample` ({meaning})?", options, reverse=reverse
        )
    return questions


# ---------------------------------------------------------------- passages


@dataclass
class _Passages:
    """The file's passages, numbered in order of first use, each written once."""

    by_number: dict[int, dict[str, Any]] = field(default_factory=dict)
    _numbers: dict[str, int] = field(default_factory=dict)

    def number(self, passage: Mapping[str, Any]) -> int:
        key = repr(sorted(passage.items()))
        if key not in self._numbers:
            self._numbers[key] = len(self._numbers) + 1
            self.by_number[self._numbers[key]] = dict(passage)
        return self._numbers[key]


@dataclass
class _Backing:
    """How a listed value or factor is backed, once code found its quotes and Jev judged them."""

    quoted: list[int] = field(default_factory=list)
    see: list[int] = field(default_factory=list)
    against: list[int] = field(default_factory=list)
    how: str = ""

    def marks(self) -> dict[str, Any]:
        if self.quoted:
            mark: dict[str, Any] = {"quoted": self.quoted}
        else:
            mark = {"inferred": self.how or "Jev picked it from the values the writer listed"}
            if self.see:
                mark["see"] = self.see
        if self.against:
            mark["against"] = self.against
        return mark


@dataclass(frozen=True)
class _Tie:
    """Jev's p that a control and a pull-down share their material, and the passage cited."""

    p: float | None
    passage: int

    @property
    def kept(self) -> bool:
        return self.p is not None and self.p >= YES


def _check_passages(
    u: Unit,
    text: str,
    sources: Mapping[str, Any],
    claims: Mapping[str, tuple[str, Value | FactorList]],
    ties: list[Any],
) -> tuple[dict[str, _Backing], dict[tuple[str, str], _Tie], _Passages, dict[str, int]]:
    """Find each claim's quotes, and ask Jev whether each found passage backs or contradicts it.

    ``claims`` maps a claim's key to what it says and the listed value or factor behind it. Returns
    each claim's backing, each listed tie of a control to a pull-down whose quote was found with
    Jev's p for it, the passages, and counts of what was not found.
    """
    passages = _Passages()
    found: dict[str, list[tuple[str, int]]] = defaultdict(list)
    counts = {"quotes": 0, "not_found": 0}
    questions: dict[str, Any] = {}
    roles: dict[str, str] = {}
    for key, (said, listed) in claims.items():
        for role, quote in [("backs", listed.quote)] + [("against", q) for q in listed.against]:
            if quote is None:
                continue
            counts["quotes"] += 1
            where = src.find_quote(quote.model_dump(), sources)
            if where is None:
                counts["not_found"] += 1
                continue
            n = passages.number(where.passage)
            name = f"q{len(questions)}"
            found[key].append((name, n))
            shown = where.passage["quote"][:600]
            source = where.passage.get("paper") or where.passage.get("record")
            place = where.passage.get("section") or where.passage.get("field")
            questions[name] = _noul(
                f'Passage from {source}, {place}: "{shown}"\nClaim: {said}.\n'
                + (
                    "Does the passage say so? Yes if it states it or clearly implies it; no if it "
                    "is about something else."
                    if role == "backs"
                    else "Does the passage say something different from the claim, so that the "
                    "two disagree? No if it agrees or is about something else."
                )
            )
            roles[name] = role
    tie_names: dict[tuple[str, str], tuple[str, int]] = {}
    titles = {s["id"]: " ".join(s["fields"]["Sample_title"]) for s in sources["samples"]}
    for tie in ties:
        where = src.find_quote(tie.quote.model_dump(), sources)
        counts["quotes"] += 1
        if where is None:
            counts["not_found"] += 1
            continue
        held = where.passage.get("record")
        held_by = (
            "every sample record"
            if held == src.EACH_SAMPLE
            else ", ".join(held)
            if isinstance(held, list)
            else str(held or where.passage.get("paper"))
        )
        place = where.passage.get("field") or where.passage.get("section")
        for pulldown in tie.pulldowns:
            name = f"t{len(tie_names)}"
            tie_names[(tie.control, pulldown)] = (name, passages.number(where.passage))
            questions[name] = _noul(
                f'Were control library {tie.control} ("{titles[tie.control]}") and pull-down '
                f'library {pulldown} ("{titles[pulldown]}") made from the same material, such as '
                f'one chromatin preparation? The words cited for the tie, "{where.passage["quote"][:300]}", '
                f"are in the {place} of {held_by}. Answer no unless the text says or clearly "
                "implies it."
            )
    answers = _ask(
        u, "Checking quoted passages against claims about a study's samples.\n\n" + text, questions
    )
    backing: dict[str, _Backing] = {}
    for key, (_, listed) in claims.items():
        b = _Backing(how=listed.how)
        for name, n in found[key]:
            yes = (_p(answers.get(name)) or 0.0) >= YES
            if roles.get(name) == "backs":
                (b.quoted if yes else b.see).append(n)
            elif yes:
                b.against.append(n)
        b.quoted, b.see, b.against = (list(dict.fromkeys(x)) for x in (b.quoted, b.see, b.against))
        b.see = [n for n in b.see if n not in b.quoted]
        backing[key] = b
    tied = {pair: _Tie(_p(answers.get(name)), n) for pair, (name, n) in tie_names.items()}
    return backing, tied, passages, counts


# ---------------------------------------------------------------- ontology terms


@dataclass
class _Terms:
    """The ids code and Jev gave each value's text, and the snapshot each id came from."""

    by_text: dict[tuple[str, str, str | None], str | list[str]] = field(default_factory=dict)
    snapshots: dict[str, str] = field(default_factory=dict)
    searched: int = 0
    not_held: set[str] = field(default_factory=set)


def _terms(
    u: Unit,
    search: OntologySearch,
    used: Mapping[tuple[str, str, str | None], Value],
    glossary: str,
) -> _Terms:
    """Each used value's term: the search's candidates for its search names, Jev's pick of them.

    ``used`` maps ``(label, text, vocabulary)`` to the listed value, the vocabulary being the one
    of the species of a sample that has it, so one text is looked up once per vocabulary. A term
    is written only for a pick at least :data:`TERM_AT` sure; a treatment of several
    compounds gets a list, only when every compound got one. ``healthy`` maps to its PATO id when
    the store holds it, and ``adult`` to the species' adult stage when the search finds it by its
    whole name. A vocabulary the store does not hold finds nothing, and is noted.
    """
    terms = _Terms()
    questions: dict[str, Any] = {}
    asked: dict[str, tuple[tuple[str, str, str | None], int, str | None]] = {}
    candidates: dict[str, tuple[Candidate, ...]] = {}
    wanted: dict[tuple[str, str, str | None], int] = {}
    for (label, text, vocab), value in used.items():
        if label == "disease" and text.strip().lower() == "healthy":
            if (healthy := search.lookup(HEALTHY)) is not None:
                terms.by_text[(label, text, vocab)] = healthy.id
                terms.snapshots[healthy.id] = healthy.snapshot
            continue
        if vocab is None:
            continue
        if label == "development_stage" and text.strip().lower() == "adult":
            if (adult := _adult_stage(search, vocab)) is not None:
                terms.by_text[(label, text, vocab)] = adult[0].id
                terms.snapshots[adult[0].id] = adult[1]
            continue
        names = value.search if isinstance(value.search, list) else [value.search or text]
        names = [n for n in names if n and n.strip()][: 1 if label != "treatment" else 10]
        wanted[(label, text, vocab)] = len(names)
        for n, name in enumerate(names):
            found = search.search(name.strip(), vocab)
            terms.searched += 1
            if found.snapshot is None:
                terms.not_held.add(vocab)
            if not found.candidates:
                continue
            key = f"o{len(questions)}"
            options = {f"c{j}": f"{c.id} {c.name}" for j, c in enumerate(found.candidates[:10])}
            options["none"] = "none of these terms names it"
            questions[key] = _choice(
                f'Which ontology term names the {label.replace("_", " ")} "{name}" (the text: "{text}")?',
                options,
            )
            asked[key] = ((label, text, vocab), n, found.snapshot)
            candidates[key] = found.candidates
    answers = _ask(u, f"Ontology terms for the samples of a study.\n{glossary[:6000]}", questions)
    picked: dict[tuple[str, str, str | None], dict[int, str]] = defaultdict(dict)
    for key, (which, n, snapshot) in asked.items():
        choice, p = _pick(answers.get(key))
        if choice and choice.startswith("c") and p is not None and p >= TERM_AT and snapshot:
            picked[which][n] = candidates[key][int(choice[1:])].id
            terms.snapshots[picked[which][n]] = snapshot
    for which, ids in picked.items():
        if len(ids) == wanted[which]:
            ordered = [ids[n] for n in sorted(ids)]
            terms.by_text[which] = (
                ordered if which[0] == "treatment" and len(ordered) > 1 else ordered[0]
            )
    return terms


def _adult_stage(search: OntologySearch, vocab: str) -> tuple[Candidate, str] | None:
    for name in ADULT_STAGE:
        found = search.search(name, vocab)
        for candidate in found.candidates:
            if candidate.matched.strip().lower() == name and found.snapshot:
                return candidate, found.snapshot
    return None


# ---------------------------------------------------------------- the reading


@dataclass(frozen=True)
class _Context:
    """What the kind's build fixes for every unit: the one open ontology search."""

    search: OntologySearch


def _read(u: Unit, context: _Context) -> dict[str, Any]:
    """The unit's answer: the reading, both answers of every question asked twice, and notes."""
    data = u.inputs.data
    sources, guides = data["sources"], data["guides"]
    samples = src.sample_ids(sources)
    if not samples:
        u.refuse("no_samples", "the study has no sequenced sample in the store")
    notes_text = "".join(f"\n\nNotes for this study's assay:\n{guide_text(g)}" for g in guides)
    listed, attempts = _write_lists(u, data["writer_text"], instructions(guides))
    lists = _tidy(listed, set(samples))
    study_text = (src.records_text(sources) + "\n\n" + src.papers_text(sources, full=False))[
        :_STUDY_CAP
    ] + notes_text
    study = _ask_study(u, study_text, lists)
    kept = _kept(lists)
    if crowded := {
        k: len(v.values) for k, v in lists.labels.items() if len(v.values) > MOST_VALUES
    }:
        said = ", ".join(f"{label} ({n})" for label, n in crowded.items())
        u.refuse(
            "too_many_values",
            f"more values listed than Jev chooses among ({MOST_VALUES}) for: {said}",
        )
    glossary = _glossary(lists, kept)
    first = _sample_questions(kept, lists.labels, reverse=False)
    second = _sample_questions(kept, lists.labels, reverse=True)
    by_id = {s["id"]: s for s in sources["samples"]}

    def ask_sample(job: tuple[str, bool]) -> dict[str, Any]:
        gsm, reverse = job
        text = (src.sample_text(sources, by_id[gsm]) + "\n\n" + glossary)[:_SAMPLE_CAP] + notes_text
        return _ask(u, text, second if reverse else first)

    jobs = [(g, reverse) for g in samples for reverse in (False, True)]
    answered = dict(zip(jobs, u.each(ask_sample, jobs, at_once=AT_ONCE), strict=True))
    picks = {g: {q: _pick(answered[(g, False)].get(q)) for q in first} for g in samples}
    again = {g: {q: _pick(answered[(g, True)].get(q)) for q in first} for g in samples}
    askings = _askings(lists, kept, samples, picks, again)

    # A level no sample is at is dropped, and a factor with no level left is not a factor.
    for i, f in enumerate(kept):
        at = {int(c[1:]) for g in samples if (c := picks[g][f"lvl{i}"][0]) and c.startswith("L")}
        f.levels = [v for j, v in enumerate(f.levels) if j in at]
        old = f.names
        f.names = [n for j, n in enumerate(old) if j in at]
        for g in samples:
            c, p = picks[g][f"lvl{i}"]
            if c and c.startswith("L"):
                picks[g][f"lvl{i}"] = (f"L{f.names.index(old[int(c[1:])])}", p)
        f.baseline = f.baseline if f.baseline in f.names else None
        f.baseline_reversed = f.baseline_reversed if f.baseline_reversed in f.names else None
    covered = [f for f in kept if f.names]

    claims: dict[str, tuple[str, Value | FactorList]] = {}
    for f in covered:
        claims[f"factor:{f.label}"] = (
            f"the study varied {f.label} on purpose between its samples",
            f.listed,
        )
        for name, level in zip(f.names, f.levels, strict=True):
            claims[f"level:{f.label}:{name}"] = (
                f"{f.label} is {level.text} for the samples this passage describes",
                level,
            )
    used: dict[tuple[str, str, str | None], Value] = {}
    for g in samples:
        taxid = src.taxonomy(by_id[g])[1]
        for i, f in enumerate(kept):
            c = picks[g][f"lvl{i}"][0]
            if c and c.startswith("L") and f.names:
                level = f.levels[int(c[1:])]
                used.setdefault((f.label, level.text, vocabulary(f.label, taxid)), level)
        for label, listed_label in lists.labels.items():
            c = picks[g].get(f"lab_{label}", (None, None))[0]
            if c and c.startswith("v"):
                value = listed_label.values[int(c[1:])]
                claims.setdefault(
                    f"value:{label}:{c}",
                    (
                        f"{label.replace('_', ' ')} is {value.text} for the samples this passage describes",
                        value,
                    ),
                )
                used.setdefault((label, value.text, vocabulary(label, taxid)), value)
    check_text = src.records_text(sources)[:40_000] + notes_text
    backing, tied, passages, counts = _check_passages(u, check_text, sources, claims, lists.ties)
    terms = _terms(u, context.search, used, glossary)

    rows: dict[str, dict[str, Any]] = {g: {} for g in samples}
    donor_kind, p_donor = _pick(study.get("donor"))
    groups = _biosamples(samples, tied)
    for g in samples:
        taxid = src.taxonomy(by_id[g])[1]
        rows[g].update(_material(g, groups, donor_kind, p_donor))
        rows[g]["species"] = _species(sources, by_id[g], passages, context.search, terms)
        for i, f in enumerate(kept):
            c, p = picks[g][f"lvl{i}"]
            if c and c.startswith("L") and f.names:
                j = int(c[1:])
                key = f"level:{f.label}:{f.names[j]}"
                rows[g][f.label] = _value_claim(
                    f.label, f.levels[j], backing[key], terms, taxid, p, level=f.names[j]
                )
            else:
                rows[g][f.label] = _gap(f.label, "na" if c == "na" else "unknown", None, p)
        for label, listed_label in lists.labels.items():
            if label in {f.label for f in kept}:
                continue
            if listed_label.values:
                c, p = picks[g].get(f"lab_{label}", (None, None))
                if c and c.startswith("v"):
                    value = listed_label.values[int(c[1:])]
                    rows[g][label] = _value_claim(
                        label, value, backing[f"value:{label}:{c}"], terms, taxid, p
                    )
                else:
                    gap = listed_label.gap.why if listed_label.gap else None
                    rows[g][label] = _gap(label, "na" if c == "na" else "unknown", gap, p)
            else:
                c, p = _pick(study.get(f"gap_{label}"))
                gap = listed_label.gap.why if listed_label.gap else None
                rows[g][label] = _gap(label, "na" if c == "na" else "unknown", gap, p)

    factors = {}
    for f in covered:
        factor: dict[str, Any] = {
            "levels": f.names,
            "baseline": f.baseline,
            "ordered": f.p_ordered is not None and f.p_ordered >= YES,
        }
        factor.update(backing[f"factor:{f.label}"].marks())
        factor["p"] = _r(f.p_varied)
        factors[f.label] = factor
    shared = _share(rows)
    reading: dict[str, Any] = {
        "format": FORMAT,
        "study": sources["id"],
        "models": {"reading": WRITER_ROLE, "probabilities": JEV_ROLE},
        "read": _read_what(sources),
        "read_detail": _read_detail(sources),
    }
    if snapshot := _snapshot(rows, shared, terms, context.search.date):
        reading["ontology_snapshot"] = snapshot
    reading |= {
        "passages": passages.by_number,
        "factors": factors,
        "shared": shared,
        "samples": rows,
    }
    if problems := reading_problems(reading):
        u.refuse("reading_format", "; ".join(f"{p['field']}: {p['reason']}" for p in problems[:10]))
    return {
        "reading": reading,
        "askings": askings,
        "notes": {
            "guides": guides,
            "writer_answers": attempts,
            "quotes": counts["quotes"],
            "quotes_not_found": counts["not_found"],
            "ties": [{"control": c, "pulldown": d, "p": _r(t.p)} for (c, d), t in tied.items()],
            "searches": terms.searched,
            "vocabularies_not_held": sorted(terms.not_held),
            "factors_listed": len(lists.factors),
            "factors_kept": len(covered),
            "samples_left_out": len(sources["left_out"]),
        },
    }


def _askings(
    lists: _Lists,
    kept: list[_Factor],
    samples: list[str],
    picks: Mapping[str, Mapping[str, tuple[str | None, float | None]]],
    again: Mapping[str, Mapping[str, tuple[str | None, float | None]]],
) -> list[dict[str, Any]]:
    """Both answers to every question Jev was asked twice that can make a reading unsettled."""
    out: list[dict[str, Any]] = []
    for f in lists.factors:
        out.append(
            {
                "about": f"factor {f.label}: varied on purpose",
                "how": "negated",
                "first": f.p_varied,
                "second": f.p_varied_negated,
            }
        )
    for i, f in enumerate(kept):
        out.append(
            {
                "about": f"factor {f.label}: baseline",
                "how": "reversed",
                "first": f.baseline,
                "second": f.baseline_reversed,
            }
        )
        for g in samples:
            first, second = picks[g][f"lvl{i}"][0], again[g][f"lvl{i}"][0]
            out.append(
                {
                    "about": f"{g}: level of {f.label}",
                    "how": "reversed",
                    "first": _name(f, first),
                    "second": _name(f, second),
                }
            )
    return out


def _name(f: _Factor, choice: str | None) -> str | None:
    if choice and choice.startswith("L"):
        return f.names[int(choice[1:])] if f.names else None
    return choice


def askings_agree(inputs: Inputs, answer: Any) -> str | None:
    """Fail a reading where Jev's two askings of one question disagree.

    A question asked plain and negated disagrees when both answers say yes, or both say no; one
    asked with its options in order and reversed disagrees when the two picks differ.
    """
    split = []
    for a in answer["askings"]:
        if a["how"] == "negated":
            if (
                a["first"] is not None
                and a["second"] is not None
                and (a["first"] >= YES) == (a["second"] >= YES)
            ):
                split.append(a["about"])
        elif a["first"] != a["second"]:
            split.append(a["about"])
    if not split:
        return None
    shown = "; ".join(split[:5]) + (f"; and {len(split) - 5} more" if len(split) > 5 else "")
    return f"Jev's two askings disagree on {shown}"


def _biosamples(
    samples: list[str], tied: Mapping[tuple[str, str], _Tie]
) -> dict[str, tuple[str, list[str], float | None, list[int]]]:
    """Each sample's biosample id, the samples that share it, the surest tie, and the ties' passages."""
    parent = {g: g for g in samples}

    def root(g: str) -> str:
        while parent[g] != g:
            parent[g] = parent[parent[g]]
            g = parent[g]
        return g

    for (a, b), tie in tied.items():
        if tie.kept:
            parent[root(a)] = root(b)
    groups: dict[str, list[str]] = defaultdict(list)
    for g in samples:
        groups[root(g)].append(g)
    out = {}
    for n, members in enumerate(sorted(groups.values(), key=lambda m: samples.index(m[0])), 1):
        ties = [t for (a, _), t in tied.items() if t.kept and a in members]
        ps = [t.p for t in ties if t.p is not None]
        see = sorted({t.passage for t in ties})
        for g in members:
            out[g] = (f"B{n}", members, max(ps) if ps else None, see)
    return out


def _material(
    g: str,
    groups: Mapping[str, tuple[str, list[str], float | None, list[int]]],
    donor_kind: str | None,
    p_donor: float | None,
) -> dict[str, Any]:
    """A sample's biosample and donor claims."""
    bid, members, p_tie, see = groups[g]
    if len(members) > 1:
        biosample: dict[str, Any] = {
            "id": bid,
            "inferred": "Jev judged the tie the writer listed between this library and another",
            "see": see,
            "p": _r(p_tie),
        }
    else:
        biosample = {
            "id": bid,
            "inferred": "no text read ties this library's material to another's",
            "p": None,
        }
    if donor_kind == "one_each":
        donor: dict[str, Any] = {
            "id": f"D{bid[1:]}",
            "inferred": "Jev judged each library to come from its own individual",
            "p": _r(p_donor),
        }
    elif donor_kind == "pool":
        donor = {"na": "the library pools many individuals", "p": _r(p_donor)}
    elif donor_kind == "cell_line":
        donor = {"na": "a cell line or a culture, no single individual", "p": _r(p_donor)}
    else:
        donor = {
            "unknown": "nothing read says which individual a library came from",
            "p": _r(p_donor),
        }
    return {"biosample": biosample, "donor": donor}


def _species(
    sources: Mapping[str, Any],
    sample: Mapping[str, Any],
    passages: _Passages,
    search: OntologySearch,
    terms: _Terms,
) -> dict[str, Any]:
    """A sample's species, from its own record: the organism as written, the taxonomy id as term.

    The id is written only when NCBI Taxonomy's snapshot holds it, and that snapshot is noted; a
    record that writes no organism takes the taxonomy's name for it.
    """
    name, taxid = src.taxonomy(sample)
    named = search.lookup(taxid) if taxid else None
    if named is not None:
        terms.snapshots[named.id] = named.snapshot
    if not name:
        claim: dict[str, Any] = {"text": named.name if named else f"NCBI Taxonomy {taxid}"}
        if named:
            claim["term"] = named.id
        claim["inferred"] = "the record's taxonomy id" + (
            ", named by NCBI Taxonomy" if named else ""
        )
        claim["p"] = None
        return claim
    where = src.find_quote(
        {"source": sample["id"], "field": "Sample_organism_ch1", "text": name}, sources
    )
    claim = {"text": name}
    if named:
        claim["term"] = named.id
    if where is not None:
        claim["quoted"] = [passages.number(where.passage)]
    else:
        claim["inferred"] = "the sample's record names this organism"
    claim["p"] = None
    return claim


def _value_claim(
    label: str,
    value: Value,
    backing: _Backing,
    terms: _Terms,
    taxid: str | None,
    p: float | None,
    *,
    level: str | None = None,
) -> dict[str, Any]:
    """A sample's claim on one listed value, its term looked up in the vocabulary of its species."""
    claim: dict[str, Any] = {"text": value.text}
    term = terms.by_text.get((label, value.text, vocabulary(label, taxid)))
    if label == "sex" and value.text.strip().lower() in SEX_WORDS:
        term = value.text.strip().lower()
    if term:
        claim["term"] = term
    if label in NUMERIC and value.value is not None:
        claim["value"] = _number(value.value)
        if value.unit and value.unit.strip():
            claim["unit"] = value.unit.strip()
    if level is not None:
        claim["level"] = level
    claim.update(backing.marks())
    claim["p"] = _r(p)
    return claim


def _gap(label: str, kind: str, why: str | None, p: float | None) -> dict[str, Any]:
    words = label.replace("_", " ")
    if kind == "na":
        return {
            "na": why or _NA_EXAMPLE.get(label) or f"{words} does not apply to this sample",
            "p": _r(p),
        }
    return {"unknown": why or f"what was read does not say its {words}", "p": _r(p)}


def _share(rows: dict[str, dict[str, Any]]) -> dict[str, Any]:
    """Move a label whose claim is the same on every sample, its ``p`` aside, under ``shared``.

    The shared claim takes the smallest of the samples' ``p``.
    """
    if len(rows) < 2:
        return {}
    shared = {}
    first = next(iter(rows.values()))
    for label in list(first):
        if label == "biosample" or not all(label in row for row in rows.values()):
            continue
        claims = [row[label] for row in rows.values()]
        bare = [{k: v for k, v in c.items() if k != "p"} for c in claims]
        if all(b == bare[0] for b in bare):
            ps = [c["p"] for c in claims if c["p"] is not None]
            shared[label] = {**bare[0], "p": min(ps) if ps else None}
            for row in rows.values():
                del row[label]
    return shared


def _snapshot(
    rows: Mapping[str, Mapping[str, Any]],
    shared: Mapping[str, Any],
    terms: _Terms,
    opened: datetime.date,
) -> dict[str, Any] | None:
    """The file's ``ontology_snapshot``: the snapshots its ids came from, and their pinning day.

    The date is the day the store was opened, which pinned its snapshots for the pass. ``None``
    when no claim writes an ontology id.
    """
    claims = [c for row in rows.values() for label, c in row.items() if label != "sex"]
    claims += [c for label, c in shared.items() if label != "sex"]
    ids = [i for c in claims if "term" in c for i in _as_list(c["term"])]
    if not ids:
        return None
    return {
        "source": snapshot_source({terms.snapshots[i] for i in ids}),
        "date": opened.isoformat(),
    }


def _as_list(term: str | list[str]) -> list[str]:
    return term if isinstance(term, list) else [term]


def _read_what(sources: Mapping[str, Any]) -> list[str]:
    read = ["records"]
    if any(p["abstract"] or p["full_text"] for p in sources["papers"]):
        read.append("abstract")
    if src.read_full_text(sources):
        read.append("full_text")
    return read


def _read_detail(sources: Mapping[str, Any]) -> str:
    series = ", ".join(s["id"] for s in sources["series"])
    papers = [
        f"the full text of {p['id']}" if p["full_text"] else f"the abstract of {p['id']}"
        for p in sources["papers"]
        if p["full_text"] or p["abstract"]
    ]
    said = f"The records of {series} and their {len(sources['samples'])} sequenced samples"
    return said + (f"; {', '.join(papers)}." if papers else "; no paper.")


def file_form(reading: Mapping[str, Any]) -> dict[str, Any]:
    """A reading as its file holds it: the snapshot's date a date, as YAML writes one."""
    out = dict(reading)
    # The answer is kept as JSON, whose keys are text: a passage is numbered in the file.
    out["passages"] = {int(n): passage for n, passage in reading["passages"].items()}
    if isinstance(snapshot := out.get("ontology_snapshot"), Mapping):
        date = snapshot["date"]
        out["ontology_snapshot"] = {
            **snapshot,
            "date": datetime.date.fromisoformat(date) if isinstance(date, str) else date,
        }
    return out


def reading_problems(reading: Mapping[str, Any]) -> list[dict[str, str]]:
    """What the format check finds wrong with ``reading``, read back from the file it would be."""
    text = yaml.safe_dump(file_form(reading), sort_keys=False, width=100, allow_unicode=True)
    data, problems = load(text)
    return problems or check_reading(data)


# ---------------------------------------------------------------- the kind


def reading_kind(
    store: Any,
    papers: Any | None,
    *,
    set_dir: Path,
    search: OntologySearch,
) -> UnitKind:
    """The liulab-llms kind over liulab-data's record store, paper reader and ontology search.

    The paper reader may be ``None``. One ontology search serves the whole pass. Each study is read
    from ``<set_dir>/<key>/bounds.yaml``.
    """
    # A pass loads several units at once, and the stores' readers are not made for two threads.
    one_at_a_time = threading.Lock()
    context = _Context(search)

    def load_unit(key: str) -> Inputs:
        if not SERIES.fullmatch(key):
            raise Held(f"{key} is not a GEO Series")
        path = set_dir / key / BOUNDS
        if not path.is_file():
            raise Held(f"no bounds file for {key} at {path}")
        bounds = yaml.safe_load(path.read_text(encoding="utf-8"))
        if papers is None and bounds.get("papers"):
            raise Held("no paper store is configured, so the papers cannot be read")
        with one_at_a_time:
            sources = src.read_sources(store, papers, bounds)
        if missing := [s["id"] for s in sources["series"] if not s["held"]]:
            raise Held(f"the store does not hold {', '.join(missing)}")
        guides = guides_for(src.library_strategies(sources))
        text = src.records_text(sources) + "\n\n" + src.papers_text(sources, full=True)
        tag = hashlib.sha256("\n".join([text, *guides]).encode()).hexdigest()[:16]
        return Inputs({"sources": sources, "guides": guides, "writer_text": text}, tag=tag)

    return UnitKind(
        name=KIND_NAME,
        version=KIND_VERSION,
        roles={
            WRITER_ROLE: Role(
                "chat", "lists the study's factors, levels and label values, each with a quote"
            ),
            JEV_ROLE: Role("jev", "answers every closed question, and judges each passage"),
        },
        load=load_unit,
        run=lambda u: _read(u, context),
        answer_schema=ANSWER_SCHEMA,
        answer_checks=(askings_agree,),
    )


def _build(store: Any) -> UnitKind:
    """The kind over the pass's record store, liulab-data's paper store and its ontology store.

    The ontology store is opened once for the pass. Refuses the setup when it cannot be opened,
    rather than read with no search.
    """
    from labdata.exceptions import NotAStoreError, OntologyError, StoreNotConfiguredError

    try:
        search = open_store_search(datetime.date.today())
    except (StoreNotConfiguredError, NotAStoreError, OntologyError) as exc:
        raise SetupRefused(
            f"kind {KIND_NAME!r} searches liulab-data's ontology store: {exc}"
        ) from exc
    return reading_kind(store, paper_reader(), set_dir=default_set_dir(), search=search)


def reading_file(outcome: Outcome) -> dict[str, Any]:
    """One unit's answer as its reading file, each model named as the pass's stamp records it."""
    reading = file_form(outcome.answer["reading"])
    reading["models"] = {
        said: model_name(outcome, role) for said, role in reading["models"].items()
    }
    return reading


def write_readings(task: Task, kind: UnitKind, out: Path, name: str) -> dict[str, dict[str, Any]]:
    """Write ``<out>/<key>/<name>`` for each study with an answer, and say how each study ended.

    A refused study gets no file, and its reason instead; a file already on disk is never
    overwritten. See :func:`.publish.write_files`.
    """
    return write_files(task, kind, out, name, reading_file)


def _records(inputs: Inputs) -> str:
    """A unit's records for a review issue: links only, no record text."""
    geo = "https://www.ncbi.nlm.nih.gov/geo/query/acc.cgi?acc="
    sources = inputs.data["sources"]
    lines = [f"- [{s['id']}]({geo}{s['id']})" for s in sources["series"]]
    lines += [
        f"- [{p['id']}](https://pubmed.ncbi.nlm.nih.gov/{p['id'].removeprefix('pmid:')}/)"
        for p in sources["papers"]
    ]
    return "\n".join(lines)


def _show(answer: Any) -> str:
    reading = answer["reading"]
    factors = ", ".join(
        f"{label} ({', '.join(f['levels'])})" for label, f in reading["factors"].items()
    )
    return f"{len(reading['samples'])} samples. Factors: {factors or 'none'}."


def _keys(store: Any) -> Iterable[str]:
    return list(pick_entries(default_set_dir()))


KIND = register(
    Kind(
        name=KIND_NAME,
        build=_build,
        default_models={WRITER_ROLE: WRITER, JEV_ROLE: JEV},
        default_keys=_keys,
        review_form=ReviewForm(
            records=_records, show=_show, drop_reasons=("no reading can be written", "other")
        ),
        reads_store=True,
    )
)
