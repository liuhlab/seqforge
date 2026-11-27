"""The bounding job's kind: Jev places each record of a study's linked group, and code writes bounds.

The kind table lists this module as ``study-bounds``, so ``seqforge pass start study-bounds`` runs
its kind on the run engine. One unit is one study, keyed by the GEO Series it starts from. Its
inputs are the text Jev is shown, so a changed record or paper changes only the units whose group
holds it.

Code keeps the starting Series in the study, or each sub-Series when it starts from a SuperSeries,
and leaves every SuperSeries out. Jev, a model that answers yes/no questions with a probability, is
asked about every other Series and every paper of the group twice, as a closed question of
:mod:`.closed`: as written, then negated. A record is in the study when Jev's answer to the
question as written is yes. The answer is the bounds file and Jev's two answers for each record
asked, saved as every job saves them, since the file has no place for a kept paper's probability.

A unit ends refused when its group holds more than :data:`MOST_SERIES` Series, when Jev gives no
answer about a record, or when its bounds fail the format check; unsettled when Jev's two answers do
not agree on a record; and settled otherwise. :func:`write_bounds` writes each answer as a bounds
file.
"""

from __future__ import annotations

import hashlib
import threading
from pathlib import Path
from typing import Any

from labllm import Held, Inputs, Outcome, Role, TaskReader, Unit, UnitKind

from ..passes.kinds import Built, Kind, name_of
from .bounds import FORMAT, check_bounds
from .closed import ASKINGS_SCHEMA, JEV, YES, Answer, Asking, Askings, YesNo, ask, disagreeing
from .folder import model_name, picked_studies, write_files
from .group import SERIES, group_text, linked_series, paper_reader, read_group, record_store

KIND_NAME = name_of(__name__)
#: Raise it when a question, the answer's shape or a rule of placing changes.
KIND_VERSION = "2026.11.2"
#: The most Series a linked group may hold. A larger group is refused before its records are read.
MOST_SERIES = 50
#: The role Jev fills. :data:`.closed.JEV` fills it unless a pass names another model.
ROLE = "jev"

ANSWER_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {"bounds": {"type": "object"}, "askings": ASKINGS_SCHEMA},
    "required": ["bounds", "askings"],
    "additionalProperties": False,
}


def _question(record: str, target: str) -> YesNo:
    """Jev's question about ``record``, as written and negated. ``target`` names the study."""
    if record.startswith("GSE"):
        return YesNo(
            record,
            f"Was GEO Series {record} deposited as part of the same piece of research as {target}: "
            "data made for it, rather than an older dataset that this research only re-uses or "
            "cites, a later dataset that re-uses its samples, or an unrelated study?",
            f"Is GEO Series {record} an older dataset that the research of {target} only re-uses "
            "or cites, a later dataset that re-uses its samples, or an unrelated study, rather "
            "than data deposited for the same research?",
        )
    return YesNo(
        record,
        f"Is paper {record} the paper that first reports the data of {target} (a preprint and its "
        "journal version both count), rather than a later paper that re-uses that data, a paper "
        "that reports another dataset of this group, or a paper linked by mistake?",
        f"Is paper {record} a later paper that re-uses the data of {target}, a paper that reports "
        "another dataset of this group, or a paper linked by mistake, rather than the paper that "
        "first reports that data (a preprint and its journal version both count as the paper)?",
    )


def _name(record: str) -> str:
    """The name a record's question goes under in a request to Jev."""
    return f"s_{record}" if record.startswith("GSE") else f"p_{record.removeprefix('pmid:')}"


def _ask(u: Unit, text: str, askings: dict[str, Asking]) -> dict[str, Answer]:
    """Jev's answer to each asking, in one call. A question left unanswered refuses."""
    answers = ask(u, ROLE, text, askings)
    if missing := [askings[n].closed_question.about for n, a in answers.items() if a is None]:
        u.refuse("jev_answered", f"Jev gave no answer about {', '.join(missing)}")
    return {n: a for n, a in answers.items() if a is not None}


def _draw(u: Unit) -> dict[str, Any]:
    """The unit's answer: the bounds code and Jev draw, and Jev's two answers for each record."""
    group, text = u.inputs.data["group"], u.inputs.data["text"]
    start = group["id"]
    supers = [s["id"] for s in group["series"] if s["held"] and s["superseries_of"]]
    if start in supers:
        subs = next(s for s in group["series"] if s["id"] == start)["superseries_of"]
        target, how = f"the Series under SuperSeries {start}", f"a sub-Series of {start}"
        kept = [s["id"] for s in group["series"] if s["id"] in subs]
    else:
        target, how, kept = f"GEO Series {start}", f"the study starts from {start}", [start]
    asked = [s["id"] for s in group["series"] if s["id"] not in {*kept, *supers}]
    asked += [p["id"] for p in group["papers"]]
    questions = {_name(r): _question(r, target) for r in asked}
    first = _ask(u, text, {n: q.first for n, q in questions.items()})
    second = _ask(u, text, {n: q.second for n, q in questions.items()})

    series = [{"id": r, "inferred": f"code: {how}", "p": None} for r in kept]
    left_out = [
        {"id": r, "inferred": "code: a SuperSeries only repeats its sub-Series' samples", "p": None}
        for r in supers
    ]
    papers = []
    for n, q in questions.items():
        r, is_series = q.about, q.about.startswith("GSE")
        said = (
            "was deposited for the same research as" if is_series else "first reports the data of"
        )
        inside = first[n].option is YES
        claim = {
            "id": r,
            "inferred": f"Jev's p(yes) that {r} {said} {target}: {first[n].p_yes:.2f}; "
            f"its p(yes) to the reversed question: {second[n].p_yes:.2f}",
            "p": round(first[n].p, 4),
        }
        if not inside:
            left_out.append(claim)
        elif is_series:
            series.append(claim)
        else:
            papers.append(r)
    # The answer names a model by its role; the file names it as the pass's stamp records it.
    by = ROLE if asked else None
    reason = (
        f"Code kept {target}; Jev judged each other record kept here to be from the same research."
        if asked
        else f"Code kept {target}; the linked group holds no other record to ask about."
    )
    bounds = {
        "format": FORMAT,
        "study": start,
        "models": {"bounds": by, "probabilities": by},
        "series": series,
        "papers": papers,
        "left_out": left_out,
        "reason": reason,
        "passages": {},
    }
    if problems := check_bounds(bounds):
        u.refuse("bounds_format", "; ".join(f"{p['field']}: {p['reason']}" for p in problems))
    return {
        "bounds": bounds,
        "askings": [Askings(q, first[n], second[n]).saved() for n, q in questions.items()],
    }


def answers_agree(inputs: Inputs, answer: Any) -> str | None:
    """Fail a study where Jev's two answers to a record's question do not agree."""
    split = disagreeing(answer["askings"])
    return f"Jev's two answers disagree on {', '.join(split)}" if split else None


def bounds_kind(store: Any, papers: Any | None) -> UnitKind:
    """The liulab-llms kind, over liulab-data's record store and its paper reader, or ``None``."""
    # A pass loads several units at once, and the stores' readers are not made for two threads.
    one_at_a_time = threading.Lock()

    def load(key: str) -> Inputs:
        if not SERIES.fullmatch(key):
            raise Held(f"{key} is not a GEO Series")
        with one_at_a_time:
            series = linked_series(store, key)
            too_large = len(series) > MOST_SERIES
            group = (
                None
                if too_large
                else read_group(store, papers, {"id": key, "linked_series": series})
            )
        if group is None:
            return Inputs({"series": series, "group": None}, tag=_tag(" ".join(series)))
        if not group["series"][series.index(key)]["held"]:
            raise Held(f"the store does not hold {key}")
        if papers is None and group["papers"]:
            raise Held("no paper store is configured, so the abstracts cannot be read")
        text = group_text(group)
        return Inputs({"series": series, "group": group, "text": text}, tag=_tag(text))

    def run(u: Unit) -> Any:
        if u.inputs.data["group"] is None:
            u.refuse("group_size", f"group too large: {len(u.inputs.data['series'])} Series")
        return _draw(u)

    return UnitKind(
        name=KIND_NAME,
        version=KIND_VERSION,
        roles={ROLE: Role("jev", "places each linked Series and paper in the study or out")},
        load=load,
        run=run,
        answer_schema=ANSWER_SCHEMA,
        answer_checks=(answers_agree,),
    )


def _tag(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()[:16]


def _build(folder: Path) -> Built:
    """The kind over liulab-data's record store and paper store, each opened once for the pass.

    A study is drawn from the stores alone, so the kind reads nothing in ``folder``.
    """
    store, build = record_store()
    return Built(bounds_kind(store, paper_reader()), store_build=build)


def bounds_file(outcome: Outcome) -> dict[str, Any]:
    """One unit's answer as its bounds file, each model named as the pass's stamp records it."""
    bounds = dict(outcome.answer["bounds"])
    bounds["models"] = {said: model_name(outcome, role) for said, role in bounds["models"].items()}
    return bounds


def write_bounds(task: TaskReader, kind: str, out: Path, name: str) -> dict[str, dict[str, Any]]:
    """Write ``<out>/<key>/<name>`` for each study with an answer, and say how each study ended.

    A refused study gets no file, and its reason instead; a file already on disk is never
    overwritten, and a file the format check rejects is not written. See
    :func:`.folder.write_files`.
    """
    return write_files(task, kind, out, name, bounds_file, check_bounds)


KIND = Kind(
    name=KIND_NAME,
    build=_build,
    default_models={ROLE: JEV},
    default_keys=picked_studies,
    write=write_bounds,
)
