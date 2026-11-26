"""A closed question asked of Jev: yes or no, or one of a list, asked once or twice.

Jev, a model that answers closed questions with a probability, answers a yes/no question with its
p(yes), and a choice with its pick and that option's probability. A question asked twice is asked
the second time negated, when it is a yes/no question, or with its options reversed, when it is a
choice.

One threshold decides a yes/no answer: yes when p(yes) is above :data:`YES_ABOVE`, no when it is
below, and neither at exactly that value. Two askings agree only when both lean the same way: a
negated question's two answers are yes and no, and a reversed choice's two picks are one option.
No answer is its own outcome, ``None``, and it never agrees with anything. What a job does with no
answer is the job's own: the bounding job refuses the study.

A job names each question by what it is about, puts its askings into Jev requests under names of
its own with :func:`ask`, and keeps both answers of a question asked twice as :class:`Askings`.
Every job saves them in one form, :meth:`Askings.saved`, which :data:`ASKINGS_SCHEMA` states and
:func:`disagreeing` reads back.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any, ClassVar, Literal, TypeGuard, get_args

from labllm import Model, Unit

#: The model that fills a job's Jev role unless a pass names another.
JEV = Model("typesafe", "jev-1.14.0")
#: A yes/no answer is yes when Jev's p(yes) is above this, no when it is below, and neither at it.
YES_ABOVE = 0.5

#: How a question's second asking differs from its first.
How = Literal["negated", "reversed"]


@dataclass(frozen=True)
class Option:
    """One answer a closed question offers.

    ``key`` is the name Jev answers under, and ``text`` the words it reads. ``name`` is what the
    saved askings call the option, its key when empty, and ``value`` is what the job gets back.
    """

    key: str
    text: str
    name: str = ""
    value: Any = None

    @property
    def said(self) -> str:
        """The option's name in the saved askings."""
        return self.name or self.key


#: The two answers of a yes/no question.
YES, NO = Option("yes", "yes"), Option("no", "no")


@dataclass(frozen=True)
class Answer:
    """Jev's answer to one asking: the option it leans to, and its probability for that option.

    A yes/no answer leans to yes or to no by its p(yes). At exactly :data:`YES_ABOVE` it leans to
    neither: ``option`` is ``None`` and ``p`` is that value.
    """

    option: Option | None
    p: float

    @property
    def p_yes(self) -> float:
        """Jev's p(yes), for an answer to a yes/no question."""
        return 1 - self.p if self.option is NO else self.p

    def saved(self) -> dict[str, Any]:
        """The answer as the saved askings hold it: its option's name, and its probability."""
        return {"option": None if self.option is None else self.option.said, "p": self.p}


@dataclass(frozen=True)
class YesNo:
    """A yes/no question about ``about``.

    ``text`` is the question as written, and ``negated`` the question a second asking puts, whose
    yes means no. A question with no negated text is asked once.
    """

    about: str
    text: str
    negated: str | None = None
    how: ClassVar[How] = "negated"

    @property
    def first(self) -> Asking:
        """The question as written."""
        return Asking(self)

    @property
    def second(self) -> Asking:
        """The negated question."""
        if self.negated is None:
            raise ValueError(f"the question about {self.about} has no negated text")
        return Asking(self, second=True)

    def form(self, *, second: bool) -> dict[str, Any]:
        """The question as Jev's API takes it."""
        return {"type": "noul", "instructions": self.negated if second else self.text}

    def read(self, reply: Any) -> Answer | None:
        """Jev's answer from its reply to one asking, or ``None`` for no answer."""
        p = reply.get("noul") if isinstance(reply, Mapping) else None
        if not _probability(p):
            return None
        p = float(p)
        if p > YES_ABOVE:
            return Answer(YES, p)
        if p < YES_ABOVE:
            return Answer(NO, 1 - p)
        return Answer(None, p)


@dataclass(frozen=True)
class Choice:
    """A choice about ``about``.

    ``text`` asks it, and Jev picks one of ``options``. A second asking puts the same text with the
    options in reverse order.
    """

    about: str
    text: str
    options: tuple[Option, ...]
    how: ClassVar[How] = "reversed"

    @property
    def first(self) -> Asking:
        """The choice with its options in order."""
        return Asking(self)

    @property
    def second(self) -> Asking:
        """The choice with its options reversed."""
        return Asking(self, second=True)

    def form(self, *, second: bool) -> dict[str, Any]:
        """The question as Jev's API takes it."""
        options = reversed(self.options) if second else self.options
        return {
            "type": "choice",
            "instructions": self.text,
            "criteria": {o.key: o.text for o in options},
        }

    def read(self, reply: Any) -> Answer | None:
        """Jev's pick from its reply to one asking, or ``None`` for no answer."""
        if not isinstance(reply, Mapping):
            return None
        pick = next((o for o in self.options if o.key == reply.get("choice")), None)
        probabilities = reply.get("probabilities")
        p = probabilities.get(pick.key) if pick and isinstance(probabilities, Mapping) else None
        return Answer(pick, float(p)) if pick and _probability(p) else None


#: A closed question.
ClosedQuestion = YesNo | Choice


@dataclass(frozen=True)
class Asking:
    """One asking of a closed question: its first, or its second."""

    closed_question: ClosedQuestion
    second: bool = False


def ask(
    u: Unit, role: str, description: str, askings: Mapping[str, Asking]
) -> dict[str, Answer | None]:
    """Ask Jev, filling ``role``, every asking in one request about ``description``.

    Each asking goes under the name that ``askings`` gives it, in that order, and its answer comes
    back under the same name: ``None`` when Jev left the question out or answered it in a form that
    the question does not take. Nothing asked, no request.
    """
    if not askings:
        return {}
    questions = {n: a.closed_question.form(second=a.second) for n, a in askings.items()}
    replies = u.jev(role, description=description, questions=questions).answers
    replies = replies if isinstance(replies, Mapping) else {}
    return {n: a.closed_question.read(replies.get(n)) for n, a in askings.items()}


@dataclass(frozen=True)
class Askings:
    """Both askings of a closed question asked twice, and Jev's answer to each."""

    closed_question: ClosedQuestion
    first: Answer | None
    second: Answer | None

    def saved(self) -> dict[str, Any]:
        """The two askings as every job saves them, in the form :data:`ASKINGS_SCHEMA` states."""
        return {
            "about": self.closed_question.about,
            "how": self.closed_question.how,
            "first": None if self.first is None else self.first.saved(),
            "second": None if self.second is None else self.second.saved(),
        }


def disagreeing(saved: Iterable[Mapping[str, Any]]) -> list[str]:
    """What each saved question is about whose two askings do not agree, in order."""
    return [a["about"] for a in saved if not _agree(a["how"], a["first"], a["second"])]


def _agree(how: How, first: Mapping[str, Any] | None, second: Mapping[str, Any] | None) -> bool:
    picks = [None if a is None else a["option"] for a in (first, second)]
    if None in picks:
        return False
    return picks[0] != picks[1] if how == "negated" else picks[0] == picks[1]


def _probability(p: Any) -> TypeGuard[float]:
    return isinstance(p, int | float) and not isinstance(p, bool) and 0 <= p <= 1


_SAVED_ANSWER: dict[str, Any] = {
    "anyOf": [
        {"type": "null"},
        {
            "type": "object",
            "properties": {
                "option": {"type": ["string", "null"]},
                "p": {"type": "number", "minimum": 0, "maximum": 1},
            },
            "required": ["option", "p"],
            "additionalProperties": False,
        },
    ]
}
#: The saved askings of a job's answer: for each question asked twice, what it is about, how its
#: second asking differs, and each answer as its option's name and probability, or ``null``.
ASKINGS_SCHEMA: dict[str, Any] = {
    "type": "array",
    "items": {
        "type": "object",
        "properties": {
            "about": {"type": "string"},
            "how": {"enum": list(get_args(How))},
            "first": _SAVED_ANSWER,
            "second": _SAVED_ANSWER,
        },
        "required": ["about", "how", "first", "second"],
        "additionalProperties": False,
    },
}
