"""The drafting kind: a small model draws one study's bounds from its records and paper abstracts.

Importing this module registers the kind, so ``seqforge pass start standard-set-bounds`` runs it on
the run engine. One unit is one picked study, keyed by its id. The model's answer is the draft: a
one-line summary and the bounds. Code checks only that the bounds are well formed over the linked
group; whether they are right is the judging model's call, so a draft that fails a check is still
kept and judged. :func:`write_drafts` copies each unit's current answer beside its study as
``draft.yaml``, once.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import threading
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

import yaml
from labllm import Held, Inputs, Model, Outcome, Role, Task, Unit, UnitKind

from ..passes.kinds import Kind, register
from ..passes.review import ReviewForm
from ..study.bounds import FORMAT
from . import DRAFT, default_set_dir
from .material import drafter_text, open_sources, pick_entries, read_group

KIND_NAME = "standard-set-bounds"
#: Raise it when the prompt, the answer's shape or a check changes.
KIND_VERSION = "2026.11.4"

_LIST = {"type": "array", "items": {"type": "string"}}
#: The bounds as a model writes them. ``left_out`` is a list here, since a model fills a fixed
#: shape more reliably than a mapping keyed by identifier; the draft and the verdict key it.
BOUNDS_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "series": _LIST,
        "papers": _LIST,
        "reason": {"type": "string"},
        "left_out": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {"id": {"type": "string"}, "reason": {"type": "string"}},
                "required": ["id", "reason"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["series", "papers", "reason", "left_out"],
    "additionalProperties": False,
}
ANSWER_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {"summary": {"type": "string"}, "bounds": BOUNDS_SCHEMA},
    "required": ["summary", "bounds"],
    "additionalProperties": False,
}

#: What a study's bounds are, for either model. The judging prompt quotes it too, so both models
#: draw to the same rules.
RULES = """\
A study is one piece of research: the experiments one paper (or one unpublished project) reports.
Its bounds say which GEO Series and which papers belong to it. You are given a linked group: the
Series a study was picked by, every Series linked to it, and every paper any of them links. Not
everything linked belongs.

Rules:
- "series" lists the Series that belong to the study. A SuperSeries only repeats the samples of its
  sub-Series, so it is never in "series": list its sub-Series and put the SuperSeries in "left_out".
- "papers" lists the papers that report this study, written "pmid:<number>". A preprint and its
  journal version are the same paper twice: keep both. A later paper that only re-uses the data, or
  a paper that belongs to another Series of the group, is left out.
- A Series that belongs to a different study, such as an older dataset this one re-uses, or a newer
  dataset that re-uses this one's samples, is left out. The study is the one the group was picked by.
- Every Series and every paper of the linked group is either in the study or in "left_out", never
  both. Each "left_out" entry gives its id and a reason of about eight words at most.
- A Series belongs to the study whole or not at all: never name some of its samples.
- "reason" is one line saying why these Series and papers are one study.
"""
_SYSTEM = (
    "You draw the bounds of one study from GEO records and paper abstracts.\n\n"
    + RULES
    + '\nAlso write "summary": one plain line of at most 20 words saying what the study is, for a '
    "biologist who has not seen it: what was sampled, what is compared, and by which assay, such as "
    '"Bulk RNA-seq of hippocampus from young and aged mice, with and without exercise". Describe '
    "the data, not the paper's finding. No accession numbers.\n\n"
    "Answer with one JSON object of this shape:\n"
    '{"summary": "...", "bounds": {"series": ["GSE..."], "papers": ["pmid:..."], "reason": "...", '
    '"left_out": [{"id": "GSE... or pmid:...", "reason": "..."}]}}'
)


def bounds_problems(bounds: Mapping[str, Any], group: Mapping[str, Any]) -> list[str]:
    """What is malformed in ``bounds`` over the linked group ``group``; empty when nothing is.

    The same rules hold for the draft and for the judging model's own bounds.
    """
    series = {s["id"]: s for s in group["series"]}
    papers = {p["id"] for p in group["papers"]}
    inside = [*bounds["series"], *bounds["papers"]]
    out = [entry["id"] for entry in bounds["left_out"]]
    problems = []
    if not bounds["series"]:
        problems.append("no Series is in the study")
    # Each list is held against its own record type: a paper is no Series, and a Series no paper.
    if strangers := sorted(
        (set(bounds["series"]) - set(series))
        | (set(bounds["papers"]) - papers)
        | (set(out) - set(series) - papers)
    ):
        problems.append(f"not in the linked group, or in the wrong list: {', '.join(strangers)}")
    for name, ids in (
        ("series", bounds["series"]),
        ("papers", bounds["papers"]),
        ("left_out", out),
    ):
        if repeated := sorted({i for i in ids if ids.count(i) > 1}):
            problems.append(f"listed more than once in {name}: {', '.join(repeated)}")
    if both := sorted(set(inside) & set(out)):
        problems.append(f"both in the study and left out: {', '.join(both)}")
    if unplaced := sorted((set(series) | papers) - set(inside) - set(out)):
        problems.append(f"neither in the study nor left out: {', '.join(unplaced)}")
    superseries = group.get("superseries")
    if superseries in bounds["series"]:
        problems.append(f"{superseries} is a SuperSeries, so its sub-Series are listed, not it")
    return problems


def well_formed(inputs: Inputs, answer: Any) -> str | None:
    """Fail a draft whose bounds do not place every linked record exactly once."""
    return "; ".join(bounds_problems(answer["bounds"], inputs.data)) or None


def summary_is_one_line(inputs: Inputs, answer: Any) -> str | None:
    """Fail a summary the reading page cannot show as a heading."""
    summary = answer["summary"].strip()
    return None if summary and "\n" not in summary and len(summary) <= 200 else "not one short line"


class NotPicked(Exception):
    """The id names no study of the pick."""


def bounds_kind(read: Callable[[str], Mapping[str, Any]]) -> UnitKind:
    """The liulab-llms kind, over ``read``: a study's id to its linked group.

    ``read`` raises :class:`NotPicked` for an id that is not a picked study, which holds the unit.
    Any other error it raises is a failure of the read, and is left to end the unit unfinished.
    """

    def load(key: str) -> Inputs:
        try:
            group = read(key)
        except NotPicked:
            raise Held(f"{key} is not a picked study") from None
        text = drafter_text(group)
        return Inputs(group, tag=hashlib.sha256(text.encode()).hexdigest()[:16])

    def run(u: Unit) -> Any:
        reply = u.chat(
            "drafter", system=_SYSTEM, user=drafter_text(u.inputs.data), schema=ANSWER_SCHEMA
        )
        return json.loads(reply.text)

    return UnitKind(
        name=KIND_NAME,
        version=KIND_VERSION,
        roles={"drafter": Role("chat", "drafts a study's bounds from its records and abstracts")},
        load=load,
        run=run,
        answer_schema=ANSWER_SCHEMA,
        answer_checks=(well_formed, summary_is_one_line),
    )


def _build(store: Any) -> UnitKind:
    """The kind over liulab-data's stores: the record store the pass opened, and the paper store."""
    entries = pick_entries(default_set_dir())
    _, papers = open_sources()

    # A pass loads several units at once, and the stores' readers are not made for two threads.
    one_at_a_time = threading.Lock()

    def read(key: str) -> Mapping[str, Any]:
        if key not in entries:
            raise NotPicked(key)
        with one_at_a_time:
            group = read_group(store, papers, entries[key])
        if papers is None and group["papers"]:
            raise Held("no paper store is configured, so the abstracts cannot be read")
        return group

    return bounds_kind(read)


def file_bounds(bounds: Mapping[str, Any]) -> dict[str, Any]:
    """Bounds as the draft and the verdict hold them: ``left_out`` keyed by identifier."""
    return {
        "series": list(bounds["series"]),
        "papers": list(bounds["papers"]),
        "reason": " ".join(bounds["reason"].split()),
        "left_out": {entry["id"]: entry["reason"] for entry in bounds["left_out"]},
    }


def study_bounds(study: str, bounds: Mapping[str, Any], model: str) -> dict[str, Any]:
    """A study's bounds file, from bounds as the draft and the verdict hold them, drawn by ``model``.

    Neither model here quotes a passage or gives a probability. So every claim is marked inferred,
    with the reason the model wrote as how, and its probability is empty: no model has given one
    yet. A sample split in older bounds is dropped, since a study is whole Series.
    """
    return {
        "format": FORMAT,
        "study": study,
        "models": {"bounds": model, "probabilities": None},
        "series": [
            {"id": gse, "inferred": bounds["reason"], "p": None} for gse in bounds["series"]
        ],
        "papers": list(bounds["papers"]),
        "left_out": [
            {"id": record, "inferred": why, "p": None} for record, why in bounds["left_out"].items()
        ],
        "reason": bounds["reason"],
        "passages": {},
    }


def draft_file(outcome: Outcome, when: dt.datetime) -> dict[str, Any]:
    """One unit's answer as its ``draft.yaml``: who drafted it, in which pass, and what it said."""
    model = outcome.stamp.models["drafter"]
    stamp = outcome.stamp
    return {
        "id": outcome.key,
        "model": f"{model.provider}/{model.name}",
        "pass": f"{stamp.kind}/{stamp.kind_version}/pass-{stamp.pass_id}",
        "when": when.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "summary": " ".join(outcome.answer["summary"].split()),
        "bounds": file_bounds(outcome.answer["bounds"]),
        # What the code's own checks found wrong with the draft's shape; empty for a settled unit.
        "checks_failed": [f"{c.name}: {c.message}" for c in outcome.checks if c.message],
    }


def write_drafts(task: Task, kind: UnitKind, set_dir: Path, when: dt.datetime) -> dict[str, str]:
    """Write ``<id>/draft.yaml`` for every unit with an answer, and say what happened to each.

    A draft already on disk is never overwritten: delete it by hand to take a newer answer.
    """
    done = {}
    for key in task.keys(kind, endings=["settled", "unsettled", "refused"]):
        outcome = task.current(kind, key)
        path = set_dir / key / DRAFT
        if outcome is None or outcome.answer is None:
            done[key] = "no answer"
        elif path.exists():
            done[key] = "kept the draft on disk"
        else:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(yaml.safe_dump(draft_file(outcome, when), sort_keys=False, width=100))
            done[key] = "written"
    return done


def _records(inputs: Inputs) -> str:
    """A unit's records for a review issue: links only, no record text."""
    geo = "https://www.ncbi.nlm.nih.gov/geo/query/acc.cgi?acc="
    lines = [f"- [{s['id']}]({geo}{s['id']})" for s in inputs.data["series"]]
    lines += [
        f"- [{p['id']}](https://pubmed.ncbi.nlm.nih.gov/{p['pmid']}/)"
        for p in inputs.data["papers"]
    ]
    return "\n".join(lines)


def _show(answer: Any) -> str:
    bounds = answer["bounds"]
    out = ", ".join(entry["id"] for entry in bounds["left_out"]) or "nothing"
    return (
        f"In: {', '.join(bounds['series'] + bounds['papers'])}. Left out: {out}. {bounds['reason']}"
    )


KIND = register(
    Kind(
        name=KIND_NAME,
        build=_build,
        default_models={
            "drafter": Model(
                "deepseek", "deepseek-v4-flash", thinking=False, max_output_tokens=2000
            )
        },
        default_keys=lambda store: list(pick_entries(default_set_dir())),
        review_form=ReviewForm(
            records=_records, show=_show, drop_reasons=("no bounds can be drawn", "other")
        ),
        reads_store=True,
    )
)
