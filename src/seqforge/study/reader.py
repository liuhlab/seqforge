"""The reading job's kind: a writer lists a study's values, Jev picks each sample's, and code writes the reading.

The kind table lists this module as ``study-reading``, so ``seqforge pass start study-reading`` runs
its kind on the run engine. One unit is one study, keyed by the GEO Series it was picked by, and
read from its bounds file in the folder of study folders the pass names: the records of the Series
it keeps, their sequenced samples, and its papers, each in full where the paper store holds the
full text and by its abstract otherwise.

The chain, for one study:

1. **The writer** (DeepSeek Flash, thinking on) reads the records and the papers and writes the
   open lists: the labels the study varied on purpose with their levels and baseline, every value
   each label takes, each with a quote, and a short search name per value. A study whose records
   name a pull-down library strategy also gets that assay's guide, and its writer lists which
   control goes with which pull-down. An answer that cannot be used is asked for once more.
2. **Jev**, a model that answers closed questions with probabilities, answers every closed
   question, each asked as :mod:`.closed` asks one: whether each listed factor was varied on
   purpose (asked plain and negated), its baseline (the options in order and reversed), whether
   its levels are ordered, what a label with no listed value is, and where the libraries' material
   came from. Then, for each sample, its level on each factor and its value on each label, asked
   twice, the options in order and reversed. A question Jev leaves unanswered makes its claim
   ``unknown``, with no probability.
3. **Code** finds every quote in its record or paper; Jev judges whether each found passage backs
   its claim, or speaks against it, and checks each listed tie of a control to a pull-down.
4. **liulab-data's ontology search** gives candidates for each value's search name, and Jev picks
   one or none. Code maps the species from the record's taxonomy id and ``healthy`` to its PATO
   id, each named through the same store, so every id in the file comes from a snapshot it names.
5. **Code** writes the reading, drops a factor level no sample is at, and runs the format check.

A unit ends **refused** when the writer's answer cannot be used after one retry or the reading
fails the format check, **unsettled** when Jev's two askings do not agree on whether a factor was
varied on purpose, on its baseline or on any sample's level (an asking left unanswered never
agrees), and **settled** otherwise.
:func:`write_readings` writes each answer as a reading file.
"""

from __future__ import annotations

import datetime
import hashlib
import threading
from collections import defaultdict
from collections.abc import Callable, Mapping
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import TYPE_CHECKING, Any, TypeGuard, TypeVar

from labllm import Held, Inputs, Model, Outcome, Role, Task, Unit, UnitKind

from ..passes import SetupRefused
from ..passes.kinds import Built, Kind, name_of
from . import sources as src
from .closed import (
    ASKINGS_SCHEMA,
    JEV,
    YES,
    Answer,
    Asking,
    Askings,
    Choice,
    Option,
    YesNo,
    ask,
    disagreeing,
)
from .folder import bounds_in, file_text, model_name, picked_studies, text_problems, write_files
from .group import SERIES, paper_reader, record_store
from .labels import LABELS, label_row, vocabulary
from .lists import (
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
from .reading import FORMAT, check_reading

if TYPE_CHECKING:
    from labdata.ontology import Candidate, OntologyStore, Term

KIND_NAME = name_of(__name__)
#: Raise it when an instruction, a question, a guide, the answer's shape or a rule of writing the
#: reading changes.
KIND_VERSION = "2026.11.7"
#: The roles, and the models that fill them unless a pass names others: the writer, and
#: :data:`.closed.JEV`.
WRITER_ROLE, JEV_ROLE = "writer", "jev"
WRITER = Model("deepseek", "deepseek-flash", thinking=True, max_output_tokens=40000)
#: A searched term is written only when Jev's pick of it has at least this probability.
TERM_AT = 0.8
#: The most values of one label Jev chooses among. A study whose writer lists more for one label
#: is refused, naming the label: a value cut from the options would be a quiet loss, and asking in
#: batches would give picks whose probabilities do not compare.
MOST_VALUES = 25
#: The Jev requests one study sends at once.
AT_ONCE = 4
#: The most candidates one ontology search returns.
MOST_CANDIDATES = 10
#: What ``source`` in a reading's ``ontology_snapshot`` starts with, before the snapshots' names.
STORE_SOURCE = "liulab-data ontology store"
#: The most characters of a description Jev reads, for the study and for one sample.
_STUDY_CAP, _SAMPLE_CAP = 90_000, 20_000
ANSWER_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "reading": {"type": "object"},
        "askings": ASKINGS_SCHEMA,
        "unanswered": {"type": "array", "items": {"type": "string"}},
        "notes": {"type": "object"},
    },
    "required": ["reading", "askings", "unanswered", "notes"],
    "additionalProperties": False,
}


# ---------------------------------------------------------------- Jev's answers

#: The option a choice offers for a label that does not apply.
_NA = "na"
#: Where each library's material came from, asked once of every study.
_DONOR = Choice(
    "donor",
    "Where did each sequenced library's material come from?",
    (
        Option("one_each", "each library from its own individual animal or person"),
        Option("shared", "some libraries share one individual"),
        Option("pool", "each library pools many individuals"),
        Option("cell_line", "a cell line or a culture, no individual"),
        Option("cannot_tell", "the text does not say"),
    ),
)


def _yes(answer: Answer | None) -> TypeGuard[Answer]:
    """Whether Jev's answer to a yes/no question is yes."""
    return answer is not None and answer.option is YES


def _left(askings: Mapping[str, Asking], answers: Mapping[str, Answer | None]) -> list[str]:
    """What each asked question Jev left unanswered is about, in the order asked."""
    return [a.closed_question.about for n, a in askings.items() if answers[n] is None]


def _listed(answer: Answer | None) -> tuple[Option, float] | None:
    """The listed level, value or term Jev picked, and its probability.

    ``None`` when Jev picked an option that lists nothing (na, unknown, no reference, none of the
    terms) or gave no answer.
    """
    if answer is None or answer.option is None or answer.option.value is None:
        return None
    return answer.option, answer.p


def _r(p: float | None) -> float | None:
    return None if p is None else round(p, 3)


# ---------------------------------------------------------------- the writer's lists


def _write_lists(u: Unit, user: str, system: str) -> tuple[Lists, int]:
    """The writer's lists, and how many answers it took. A second broken answer refuses the unit.

    No schema is sent: the gateway sends a DeepSeek schema as a strict function call, which needs
    thinking off, and the writer thinks. :func:`.lists.parse_lists` takes the object out of the
    reply's text.
    """
    reply = u.chat(WRITER_ROLE, system=system, user=user)
    lists, broken = parse_lists(reply.text)
    if lists is not None:
        return lists, 1
    again = u.chat(WRITER_ROLE, system=system, user=user + RETRY.format(reason=broken))
    lists, still = parse_lists(again.text)
    if lists is None:
        u.refuse("writer_answer", f"the writer's answer could not be used twice: {still}")
    return lists, 2


@dataclass
class _Factor:
    """A listed factor as the chain carries it: the writer's list and Jev's answers.

    ``varied`` holds both askings of whether it was varied on purpose, ``reference`` both of its
    baseline, and ``ordered`` the one answer on whether its levels run along a scale, which
    ``ordered_about`` names.
    """

    label: str
    listed: FactorList
    levels: list[Value]
    names: list[str]
    varied: Askings = field(init=False)
    reference: Askings = field(init=False)
    ordered: Answer | None = None
    ordered_about: str = ""

    @property
    def baseline(self) -> str | None:
        """The level Jev's first asking names the reference, while it is still a level."""
        picked = _listed(self.reference.first)
        name = picked[0].name if picked else None
        return name if name in self.names else None


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
    labels: dict[str, Label] = {
        r.name: lists.labels.get(r.name) or Label() for r in LABELS if r.written_by == "writer"
    }
    for name, label in lists.extra_labels.items():
        own = snake(name)
        if own and own not in labels and label_row(own).written_by == "writer":
            labels[own] = label
    for label in labels.values():
        label.values = [_said(v) for v in label.values if _says(v)]
    factors: list[_Factor] = []
    for listed in lists.factors:
        name = snake(listed.label)
        levels = [_said(v) for v in listed.levels if _says(v)]
        if not name or label_row(name).written_by == "code" or not levels:
            continue
        if not label_row(name).factor:
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


def _ask_study(u: Unit, text: str, lists: _Lists) -> tuple[dict[str, Answer | None], list[str]]:
    """Jev's answers about the study: each factor, each label with no value, the donors.

    Each factor keeps its own answers; every answer also comes back under its asking's name. Also
    returns what each question asked once is about, where Jev left it unanswered: a label with no
    value, and the donors. A factor's ordered question counts only once the factor is kept.
    """
    askings: dict[str, Asking] = {}
    for i, f in enumerate(lists.factors):
        levels = "; ".join(f"{n} ({v.text})" for n, v in zip(f.names, f.levels, strict=True))
        levels = levels[:1500]
        varied = YesNo(
            f"factor {f.label}: varied on purpose",
            f"Did the study vary {f.label} on purpose between its sequenced samples, to compare "
            f"these levels: {levels}?",
            f"Is {f.label} (values: {levels}) only a description, a replicate or batch label, or "
            "a restatement of another factor, rather than something the study varied on purpose "
            "to compare?",
        )
        reference = Choice(
            f"factor {f.label}: baseline",
            f"Which level of {f.label} is the reference the study compares the others with, such "
            "as untreated, wild type, control, young or time zero?",
            (
                *(
                    Option(f"L{j}", name, name=name, value=level)
                    for j, (name, level) in enumerate(zip(f.names, f.levels, strict=True))
                ),
                Option(
                    "no_reference",
                    "the study compares the levels with each other; none is a reference",
                ),
            ),
        )
        ordered = YesNo(
            f"factor {f.label}: ordered",
            f"Do the levels of {f.label} ({levels}) run along a scale, such as a time course, a "
            "dose series or a series of ages?",
        )
        askings[f"f{i}_varied"] = varied.first
        askings[f"f{i}_varied_negated"] = varied.second
        askings[f"f{i}_baseline"] = reference.first
        askings[f"f{i}_baseline_reversed"] = reference.second
        askings[f"f{i}_ordered"] = ordered.first
    for label, listed in lists.labels.items():
        if listed.values or label in {f.label for f in lists.factors}:
            continue
        example = label_row(label).na
        meaning = label_row(label).holds
        askings[f"gap_{label}"] = Choice(
            f"{label}: no listed value",
            f"For the samples of this study, what about their {label.replace('_', ' ')} "
            f"({meaning})?",
            (
                Option(
                    _NA, "it does not apply to these samples" + (f": {example}" if example else "")
                ),
                Option("unknown", "it applies, but nothing in the text says it"),
            ),
        ).first
    askings["donor"] = _DONOR.first
    answers = ask(u, JEV_ROLE, text, askings)
    for i, f in enumerate(lists.factors):
        f.varied = Askings(
            askings[f"f{i}_varied"].closed_question,
            answers[f"f{i}_varied"],
            answers[f"f{i}_varied_negated"],
        )
        f.reference = Askings(
            askings[f"f{i}_baseline"].closed_question,
            answers[f"f{i}_baseline"],
            answers[f"f{i}_baseline_reversed"],
        )
        f.ordered = answers[f"f{i}_ordered"]
        f.ordered_about = askings[f"f{i}_ordered"].closed_question.about
    unanswered = [
        askings[n].closed_question.about
        for n in askings
        if (n.startswith("gap_") or n == "donor") and answers[n] is None
    ]
    return answers, unanswered


def _kept(lists: _Lists) -> list[_Factor]:
    """The factors Jev says were varied on purpose; of two on one label, the surer one.

    A factor is kept when Jev's answer to the question as written is yes. A listed factor Jev does
    not keep stays a label: its levels become the label's values.
    """
    by_label: dict[str, tuple[_Factor, float]] = {}
    for f in lists.factors:
        said = f.varied.first
        if _yes(said) and said.p > by_label.get(f.label, (f, 0.0))[1]:
            by_label[f.label] = (f, said.p)
    kept = [f for f, _ in by_label.values()]
    for f in lists.factors:
        if f not in kept and f.label not in by_label:
            have = {v.text for v in lists.labels[f.label].values}
            lists.labels[f.label].values += [v for v in f.levels if v.text not in have]
    return kept


# ---------------------------------------------------------------- Jev, each sample


def _sample_questions(kept: list[_Factor], labels: Mapping[str, Label]) -> dict[str, Choice]:
    """The choices Jev is asked about each sample, by name: its level and its label values."""
    questions: dict[str, Choice] = {}
    for i, f in enumerate(kept):
        questions[f"lvl{i}"] = Choice(
            f"level of {f.label}",
            f"Which {f.label} does `sample` have?",
            (
                *(
                    Option(f"L{j}", f"`sample` has {f.label} = {v.text}", name=name, value=v)
                    for j, (name, v) in enumerate(zip(f.names, f.levels, strict=True))
                ),
                Option("unknown", f"`sample`'s text does not say its {f.label}"),
                Option(_NA, f"{f.label} does not apply to `sample`"),
            ),
        )
    for label, listed in labels.items():
        if not listed.values:
            continue
        words = label.replace("_", " ")
        example = label_row(label).na
        meaning = label_row(label).holds
        questions[f"lab_{label}"] = Choice(
            f"value of {label}",
            f"What is the {words} of `sample` ({meaning})?",
            (
                *(
                    Option(f"v{j}", f"{words} is {v.text}", value=v)
                    for j, v in enumerate(listed.values)
                ),
                Option("unknown", f"`sample`'s text and the listed words do not say its {words}"),
                Option(
                    _NA,
                    f"{words} does not apply to `sample`" + (f" ({example})" if example else ""),
                ),
            ),
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
    """Jev's answer on whether a control and a pull-down share material, and the passage cited."""

    answer: Answer | None
    passage: int

    @property
    def p(self) -> float | None:
        """Jev's p(yes) that the two share their material, or ``None`` for no answer."""
        return None if self.answer is None else self.answer.p_yes

    @property
    def kept(self) -> bool:
        return _yes(self.answer)


def _check_passages(
    u: Unit,
    text: str,
    sources: Mapping[str, Any],
    claims: Mapping[str, tuple[str, Value | FactorList]],
    ties: list[Any],
    unanswered: list[str],
) -> tuple[dict[str, _Backing], dict[tuple[str, str], _Tie], _Passages, dict[str, int]]:
    """Find each claim's quotes, and ask Jev whether each found passage backs or contradicts it.

    ``claims`` maps a claim's key to what it says and the listed value or factor behind it. Returns
    each claim's backing, each listed tie of a control to a pull-down whose quote was found with
    Jev's p for it, the passages, and counts of what was not found. What each check Jev left
    unanswered is about goes on ``unanswered``.
    """
    passages = _Passages()
    found: dict[str, list[tuple[str, int]]] = defaultdict(list)
    counts = {"quotes": 0, "not_found": 0}
    questions: dict[str, Asking] = {}
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
            questions[name] = YesNo(
                f"passage {n} {role} {key}",
                f'Passage from {source}, {place}: "{shown}"\nClaim: {said}.\n'
                + (
                    "Does the passage say so? Yes if it states it or clearly implies it; no if it "
                    "is about something else."
                    if role == "backs"
                    else "Does the passage say something different from the claim, so that the "
                    "two disagree? No if it agrees or is about something else."
                ),
            ).first
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
            questions[name] = YesNo(
                f"tie of {tie.control} to {pulldown}",
                f'Were control library {tie.control} ("{titles[tie.control]}") and pull-down '
                f'library {pulldown} ("{titles[pulldown]}") made from the same material, such as '
                f'one chromatin preparation? The words cited for the tie, "{where.passage["quote"][:300]}", '
                f"are in the {place} of {held_by}. Answer no unless the text says or clearly "
                "implies it.",
            ).first
    answers = ask(
        u,
        JEV_ROLE,
        "Checking quoted passages against claims about a study's samples.\n\n" + text,
        questions,
    )
    unanswered += _left(questions, answers)
    backing: dict[str, _Backing] = {}
    for key, (_, listed) in claims.items():
        b = _Backing(how=listed.how)
        for name, n in found[key]:
            yes = _yes(answers[name])
            if roles.get(name) == "backs":
                (b.quoted if yes else b.see).append(n)
            elif yes:
                b.against.append(n)
        b.quoted, b.see, b.against = (list(dict.fromkeys(x)) for x in (b.quoted, b.see, b.against))
        b.see = [n for n in b.see if n not in b.quoted]
        backing[key] = b
    tied = {pair: _Tie(answers[name], n) for pair, (name, n) in tie_names.items()}
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
    ontology: Ontology,
    used: Mapping[tuple[str, str, str | None], Value],
    glossary: str,
    unanswered: list[str],
) -> _Terms:
    """Each used value's term: the search's candidates for its search names, Jev's pick of them.

    ``used`` maps ``(label, text, vocabulary)`` to the listed value, the vocabulary being the one
    of the species of a sample that has it, so one text is looked up once per vocabulary. A term
    is written only for a pick at least :data:`TERM_AT` sure; a treatment of several
    compounds gets a list, only when every compound got one. ``healthy`` maps to its PATO id when
    the store holds it, and ``adult`` to the species' adult stage when the search finds it by its
    whole name. A vocabulary the store does not hold finds nothing, and is noted. What each pick
    Jev left unanswered is about goes on ``unanswered``.
    """
    terms = _Terms()
    questions: dict[str, Asking] = {}
    asked: dict[str, tuple[tuple[str, str, str | None], int]] = {}
    wanted: dict[tuple[str, str, str | None], int] = {}
    for (label, text, vocab), value in used.items():
        rules, folded = label_row(label), text.strip().lower()
        if folded in rules.ids:
            if (named := _live(ontology, rules.ids[folded])) is not None:
                terms.by_text[(label, text, vocab)] = named.id
                terms.snapshots[named.id] = named.snapshot
            continue
        if vocab is None:
            continue
        if folded in rules.named_by:
            if (whole := _whole_name(ontology, vocab, rules.named_by[folded])) is not None:
                terms.by_text[(label, text, vocab)] = whole.id
                terms.snapshots[whole.id] = whole.snapshot
            continue
        names = value.search if isinstance(value.search, list) else [value.search or text]
        names = [n for n in names if n and n.strip()][: 10 if rules.many_terms else 1]
        wanted[(label, text, vocab)] = len(names)
        for n, name in enumerate(names):
            found = _search(ontology, name.strip(), vocab)
            terms.searched += 1
            if vocab not in ontology.snapshots:
                terms.not_held.add(vocab)
            if not found:
                continue
            key = f"o{len(questions)}"
            questions[key] = Choice(
                f"term for the {label} {name!r}",
                f'Which ontology term names the {label.replace("_", " ")} "{name}" (the text: "{text}")?',
                (
                    *(
                        Option(f"c{j}", f"{c.id} {c.name or c.text}", value=c)
                        for j, c in enumerate(found[:10])
                    ),
                    Option("none", "none of these terms names it"),
                ),
            ).first
            asked[key] = ((label, text, vocab), n)
    answers = ask(
        u, JEV_ROLE, f"Ontology terms for the samples of a study.\n{glossary[:6000]}", questions
    )
    unanswered += _left(questions, answers)
    picked: dict[tuple[str, str, str | None], dict[int, str]] = defaultdict(dict)
    for key, (which, n) in asked.items():
        if (pick := _listed(answers[key])) and pick[1] >= TERM_AT:
            candidate: Candidate = pick[0].value
            picked[which][n] = candidate.id
            terms.snapshots[candidate.id] = candidate.snapshot
    for which, ids in picked.items():
        if len(ids) == wanted[which]:
            ordered = [ids[n] for n in sorted(ids)]
            terms.by_text[which] = (
                ordered if label_row(which[0]).many_terms and len(ordered) > 1 else ordered[0]
            )
    return terms


def _whole_name(ontology: Ontology, vocab: str, names: tuple[str, ...]) -> Candidate | None:
    """The first candidate whose whole text is one of ``names``, tried in order."""
    for name in names:
        for candidate in _search(ontology, name, vocab):
            if candidate.text.strip().lower() == name:
                return candidate
    return None


# ---------------------------------------------------------------- the ontology store

_T = TypeVar("_T")


class Ontology:
    """liulab-data's ontology store, opened once and pinned until :meth:`close`.

    The store is found through ``LABDATA_ONTOLOGIES`` or its per-user pointer; liulab-data's own
    error, which names the variable, is raised when none can be opened. ``date`` is the UTC day it
    was opened, because the store names its snapshots by UTC day, and ``snapshots`` maps each
    vocabulary it holds to its pinned snapshot.

    An open store answers only on the thread that opened it, and a pass reads several units at
    once on threads of its own, so one thread of this object's opens the store and asks it every
    question. Each search takes milliseconds, so the units queueing for it cost little.
    """

    def __init__(self) -> None:
        from labdata.ontology import open_ontology_store

        self.date = datetime.datetime.now(datetime.UTC).date()
        self._owner = ThreadPoolExecutor(max_workers=1, thread_name_prefix="ontology-store")
        try:
            self._store: OntologyStore = self._owner.submit(open_ontology_store).result()
        except BaseException:
            self._owner.shutdown()
            raise
        self.snapshots = self._store.snapshots

    def on_its_thread(self, ask: Callable[[OntologyStore], _T]) -> _T:
        """What ``ask`` returns when it asks the store, on the thread that opened it."""
        return self._owner.submit(ask, self._store).result()

    def close(self) -> None:
        """Release the snapshots and the thread that read them."""
        self.on_its_thread(lambda store: store.close())
        self._owner.shutdown()


def _search(ontology: Ontology, text: str, vocab: str) -> list[Candidate]:
    """The store's candidates for ``text``, whole-text matches first; none for an unheld vocabulary."""
    if vocab not in ontology.snapshots:
        return []
    return ontology.on_its_thread(lambda store: store.search(text, vocab, limit=MOST_CANDIDATES))


def _live(ontology: Ontology, id: str) -> Term | None:
    """The live term ``id`` names; ``None`` for an id no snapshot holds or one retired."""
    term = ontology.on_its_thread(lambda store: store.lookup(id))
    return None if term is None or term.name is None or term.obsolete else term


# ---------------------------------------------------------------- the reading


@dataclass(frozen=True)
class _Context:
    """What the kind's build fixes for every unit: the one open ontology store."""

    ontology: Ontology


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
    study, unanswered = _ask_study(u, study_text, lists)
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
    questions = _sample_questions(kept, lists.labels)
    by_id = {s["id"]: s for s in sources["samples"]}

    def ask_sample(job: tuple[str, bool]) -> dict[str, Answer | None]:
        gsm, second = job
        text = (src.sample_text(sources, by_id[gsm]) + "\n\n" + glossary)[:_SAMPLE_CAP] + notes_text
        askings = {n: q.second if second else q.first for n, q in questions.items()}
        return ask(u, JEV_ROLE, text, askings)

    jobs = [(g, second) for g in samples for second in (False, True)]
    answered = dict(zip(jobs, u.each(ask_sample, jobs, at_once=AT_ONCE), strict=True))
    picks = {g: answered[(g, False)] for g in samples}
    askings = _askings(lists, kept, samples, questions, answered)

    # A level no sample is at is dropped, and a factor with no level left is not a factor.
    for i, f in enumerate(kept):
        at = {pick[0].name for g in samples if (pick := _listed(picks[g][f"lvl{i}"]))}
        f.levels = [v for v, n in zip(f.levels, f.names, strict=True) if n in at]
        f.names = [n for n in f.names if n in at]
    covered = [f for f in kept if f.names]
    unanswered += [f.ordered_about for f in covered if f.ordered is None]

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
            if pick := _listed(picks[g][f"lvl{i}"]):
                at_level: Value = pick[0].value
                used.setdefault((f.label, at_level.text, vocabulary(f.label, taxid)), at_level)
        for label in lists.labels:
            if pick := _listed(picks[g].get(f"lab_{label}")):
                option, value = pick[0], pick[0].value
                claims.setdefault(
                    f"value:{label}:{option.key}",
                    (
                        f"{label.replace('_', ' ')} is {value.text} for the samples this passage describes",
                        value,
                    ),
                )
                used.setdefault((label, value.text, vocabulary(label, taxid)), value)
    check_text = src.records_text(sources)[:40_000] + notes_text
    backing, tied, passages, counts = _check_passages(
        u, check_text, sources, claims, lists.ties, unanswered
    )
    terms = _terms(u, context.ontology, used, glossary, unanswered)

    rows: dict[str, dict[str, Any]] = {g: {} for g in samples}
    groups = _biosamples(samples, tied)
    for g in samples:
        taxid = src.taxonomy(by_id[g])[1]
        rows[g].update(_material(g, groups, study["donor"]))
        rows[g]["species"] = _species(sources, by_id[g], passages, context.ontology, terms)
        for i, f in enumerate(kept):
            answer = picks[g][f"lvl{i}"]
            if pick := _listed(answer):
                option, p = pick
                key = f"level:{f.label}:{option.name}"
                rows[g][f.label] = _value_claim(
                    f.label, option.value, backing[key], terms, taxid, p, level=option.name
                )
            else:
                rows[g][f.label] = _gap(f.label, answer)
        for label, listed_label in lists.labels.items():
            if label in {f.label for f in kept}:
                continue
            gap = listed_label.gap.why if listed_label.gap else None
            if not listed_label.values:
                rows[g][label] = _gap(label, study.get(f"gap_{label}"), gap)
            elif pick := _listed(picks[g].get(f"lab_{label}")):
                option, p = pick
                key = f"value:{label}:{option.key}"
                rows[g][label] = _value_claim(label, option.value, backing[key], terms, taxid, p)
            else:
                rows[g][label] = _gap(label, picks[g].get(f"lab_{label}"), gap)
                if f"lab_{label}" in picks[g] and picks[g][f"lab_{label}"] is None:
                    unanswered.append(f"{g}: {questions[f'lab_{label}'].about}")

    factors = {}
    for f in covered:
        factor: dict[str, Any] = {
            "levels": f.names,
            "baseline": f.baseline,
            "ordered": _yes(f.ordered),
        }
        factor.update(backing[f"factor:{f.label}"].marks())
        factor["p"] = _r(f.varied.first.p_yes if f.varied.first else None)
        factors[f.label] = factor
    shared = _share(rows)
    reading: dict[str, Any] = {
        "format": FORMAT,
        "study": sources["id"],
        "models": {"reading": WRITER_ROLE, "probabilities": JEV_ROLE},
        "read": _read_what(sources),
        "read_detail": _read_detail(sources),
    }
    if snapshot := _snapshot(rows, shared, terms, context.ontology.date):
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
        "unanswered": unanswered,
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
    questions: Mapping[str, Choice],
    answered: Mapping[tuple[str, bool], Mapping[str, Answer | None]],
) -> list[dict[str, Any]]:
    """Both askings of every question Jev was asked twice that can make a reading unsettled.

    ``answered`` holds each sample's answers by its key and whether they answer the second asking.
    """
    out = [f.varied.saved() for f in lists.factors]
    for i, f in enumerate(kept):
        out.append(f.reference.saved())
        level = questions[f"lvl{i}"]
        for g in samples:
            asked = replace(level, about=f"{g}: {level.about}")
            first, second = answered[(g, False)][f"lvl{i}"], answered[(g, True)][f"lvl{i}"]
            out.append(Askings(asked, first, second).saved())
    return out


def askings_agree(inputs: Inputs, answer: Any) -> str | None:
    """Fail a reading where Jev's two askings of one question do not agree.

    :func:`.closed.disagreeing` decides: an asking Jev left unanswered never agrees.
    """
    split = disagreeing(answer["askings"])
    if not split:
        return None
    shown = "; ".join(split[:5]) + (f"; and {len(split) - 5} more" if len(split) > 5 else "")
    return f"Jev's two askings do not agree on {shown}"


def all_answered(inputs: Inputs, answer: Any) -> str | None:
    """Fail a reading where Jev left unanswered a question it reads one asking of.

    Those are a label's value, a label with no listed value, the donors, whether a kept factor's
    levels are ordered, each passage check, each tie and each term pick. Each writes what it writes
    for no answer, such as an ``unknown`` claim with no probability, and the study is unsettled, as
    it is when the two askings of a question asked twice do not agree.
    """
    left = answer["unanswered"]
    if not left:
        return None
    shown = "; ".join(left[:5]) + (f"; and {len(left) - 5} more" if len(left) > 5 else "")
    return f"Jev left unanswered {shown}"


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
    said: Answer | None,
) -> dict[str, Any]:
    """A sample's biosample and donor claims.

    ``said`` is Jev's answer on where the material of the study's libraries came from.
    """
    donor_kind = said.option.key if said and said.option else None
    p_donor = said.p if said else None
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
    ontology: Ontology,
    terms: _Terms,
) -> dict[str, Any]:
    """A sample's species, from its own record: the organism as written, the taxonomy id as term.

    The id is written only when NCBI Taxonomy's snapshot holds it, and that snapshot is noted; a
    record that writes no organism takes the taxonomy's name for it.
    """
    name, taxid = src.taxonomy(sample)
    named = _live(ontology, taxid) if taxid else None
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
    rules = label_row(label)
    claim: dict[str, Any] = {"text": value.text}
    term = terms.by_text.get((label, value.text, vocabulary(label, taxid)))
    if value.text.strip().lower() in rules.words:
        term = value.text.strip().lower()
    if term:
        claim["term"] = term
    if rules.number and value.value is not None:
        claim["value"] = _number(value.value)
        if value.unit and value.unit.strip():
            claim["unit"] = value.unit.strip()
    if level is not None:
        claim["level"] = level
    claim.update(backing.marks())
    claim["p"] = _r(p)
    return claim


def _gap(label: str, said: Answer | None, why: str | None = None) -> dict[str, Any]:
    """A claim with no listed value: ``na`` when Jev's answer is na, and ``unknown`` otherwise.

    With no answer from Jev, the claim has no probability.
    """
    words = label.replace("_", " ")
    p = None if said is None else _r(said.p)
    if said is not None and said.option is not None and said.option.key == _NA:
        return {
            "na": why or label_row(label).na or f"{words} does not apply to this sample",
            "p": p,
        }
    return {"unknown": why or f"what was read does not say its {words}", "p": p}


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
    # A term from a label's own words is no ontology id.
    claims = [c for row in rows.values() for label, c in row.items() if not label_row(label).words]
    claims += [c for label, c in shared.items() if not label_row(label).words]
    ids = [i for c in claims if "term" in c for i in _as_list(c["term"])]
    if not ids:
        return None
    return {
        "source": f"{STORE_SOURCE}: {', '.join(sorted({terms.snapshots[i] for i in ids}))}",
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
    return text_problems(file_text(file_form(reading)), check_reading)


# ---------------------------------------------------------------- the kind


def reading_kind(
    store: Any,
    papers: Any | None,
    *,
    folder: Path,
    ontology: Ontology,
) -> UnitKind:
    """The liulab-llms kind over liulab-data's record store, paper reader and ontology store.

    The paper reader may be ``None``. One open ontology store serves the whole pass. Each study is read
    from its bounds file in ``folder``, and held when the file is missing or fails its check.
    """
    # A pass loads several units at once, and the stores' readers are not made for two threads.
    one_at_a_time = threading.Lock()
    context = _Context(ontology)

    def load_unit(key: str) -> Inputs:
        if not SERIES.fullmatch(key):
            raise Held(f"{key} is not a GEO Series")
        bounds = bounds_in(folder, key)
        if papers is None and bounds.papers:
            raise Held("no paper store is configured, so the papers cannot be read")
        with one_at_a_time:
            sources = src.read_sources(store, papers, bounds)
        if missing := [s["id"] for s in sources["series"] if not s["held"]]:
            raise Held(f"the store does not hold {', '.join(missing)}")
        guides = guides_for(src.library_strategies(sources))
        text = src.records_text(sources) + "\n\n" + src.papers_text(sources, full=True)
        # A store update can change a reading's terms with no record changed, so the tag names
        # every snapshot pinned: which ones a study will search is not known until it is read.
        identity = "\n".join([text, *guides, *sorted(ontology.snapshots.values())])
        tag = hashlib.sha256(identity.encode()).hexdigest()[:16]
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
        answer_checks=(askings_agree, all_answered),
    )


def _build(folder: Path) -> Built:
    """The kind over liulab-data's record store, paper store and ontology store, reading ``folder``.

    Each store is opened once for the pass, and the ontology store is closed when the pass is over.
    Refuses the setup when one cannot be opened, the ontology store included, rather than read
    with no search.
    """
    from labdata.exceptions import NotAStoreError, OntologyError, StoreNotConfiguredError

    store, build = record_store()
    papers = paper_reader()
    try:
        ontology = Ontology()
    except (StoreNotConfiguredError, NotAStoreError, OntologyError) as exc:
        raise SetupRefused(
            f"kind {KIND_NAME!r} searches liulab-data's ontology store: {exc}"
        ) from exc
    kind = reading_kind(store, papers, folder=folder, ontology=ontology)
    # The ontology store stays open for the life of the pass, so every unit reads the snapshots
    # it pinned; the pass closes it when it ends.
    return Built(kind, store_build=build, close=ontology.close)


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
    overwritten, and a file the format check rejects is not written. See
    :func:`.folder.write_files`.
    """
    return write_files(task, kind, out, name, reading_file, check_reading)


KIND = Kind(
    name=KIND_NAME,
    build=_build,
    default_models={WRITER_ROLE: WRITER, JEV_ROLE: JEV},
    default_keys=picked_studies,
)
