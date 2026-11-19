"""`seqforge review`: a kind's review queue as issues in the lab's private review repo.

`review file` posts a small sample of the queue -- the kind's unsettled and refused units -- as
issues, one unit each, which a person answers by ticking boxes and closing. Each issue carries a
hidden marker naming its kind, key and inputs tag, so a unit is never filed twice for the same
inputs and reading back needs no local list of what was filed. It prints one JSON object and exits
0, 2 for a setup it cannot use, or 3 when another process holds the task or GitHub fails.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

import typer

from ..passes import SetupRefused
from ..workspace import passes_dir
from ._common import _emit, _StageOut
from .passes import _held
from .root import review_app

if TYPE_CHECKING:
    from labllm import Inputs, QueueEntry, UnitKind


@review_app.command("file")
def review_file(
    kind: str = typer.Argument(..., help="The registered seqforge kind whose queue to file."),
    count: int = typer.Option(
        5, "--count", min=1, help="How many units to file. Only a person raises it."
    ),
    task: Path | None = typer.Option(
        None, "--task", help="The task's folder. Default: seqforge/passes/<kind>/ here."
    ),
    reviews: Path | None = typer.Option(
        None,
        "--reviews",
        envvar="SEQFORGE_REVIEWS",
        help="A local clone of the review repo: its origin is where the issues go.",
    ),
) -> None:
    """File a sample of a kind's review queue as issues in the review repo, and print its JSON.

    Takes `--count` units (5 by default) in the kind's review priority, skipping every unit already
    filed for its current inputs, whether its issue is open or closed. Each issue is labelled with
    the kind and each check the unit's answer failed; labels the repo lacks are made. Exit 0; 2 for
    a setup it cannot use, or a folder with no task; 3 when another process holds the task, or a
    GitHub call fails (rerun to file the rest).
    """
    _emit(_review_file(name=kind, count=count, task=task, reviews=reviews))


def _review_file(*, name: str, count: int, task: Path | None, reviews: Path | None) -> _StageOut:
    """The body of ``review file``: every setup refusal, then the queue, then the filing."""
    from labllm import Task, TaskInUse

    from ..passes import gateway, github
    from ..passes.github import GitHubError
    from ..passes.kinds import kind_named
    from ..passes.review import KIND_LABEL, issue_for, labels_for, read_marker
    from ..passes.task import open_task, require_task, reviews_folder, store_for

    try:
        kind = kind_named(name)
        review_files = reviews_folder(reviews)
        folder = task if task is not None else passes_dir(Path(), kind.name)
        require_task(folder, kind.name)
        repo = github.make_github(review_files.parent)
        store, build = store_for(kind)
        unit_kind = kind.unit_kind(store)
        # Reading the queue takes the task's hold, so a running pass makes this exit 3.
        opened = open_task(Task, folder, gateway=gateway.offline_gateway(), reviews=review_files)
    except SetupRefused as exc:
        return _StageOut(str(exc), 2, err=True)
    except TaskInUse as exc:
        return _held(kind.name, folder, exc)
    with opened:
        queue = opened.review_queue(unit_kind)

    filed: list[dict[str, str]] = []
    try:
        issues = repo.issues(KIND_LABEL.format(kind.name))
        markers = [m for issue in issues if (m := read_marker(issue.body)) is not None]
        done = {(m.key, m.inputs_tag) for m in markers if m.kind == kind.name}
        waiting = [entry for entry in queue if (entry.key, entry.inputs_tag) not in done]
        priority = kind.review_priority
        if priority is not None:
            waiting.sort(key=lambda entry: priority(store, entry))
        labels = repo.labels()
        for entry in waiting:
            if len(filed) == count:
                break
            inputs = _current_inputs(unit_kind, entry)
            if inputs is None:
                continue
            title, body = issue_for(kind.review_form, entry, inputs, store_build=build)
            wanted = labels_for(entry)
            for label, (color, description) in wanted.items():
                if label not in labels:
                    repo.create_label(label, color=color, description=description)
                    labels.add(label)
            issue = repo.create_issue(title=title, body=body, labels=list(wanted))
            filed.append({"key": entry.key, "url": issue.url})
    except GitHubError as exc:
        payload = {"kind": kind.name, "task": str(folder), "filed": filed}
        return _StageOut({**payload, "stopped": "github_failed", "message": str(exc)}, 3)
    return _StageOut(
        {
            "kind": kind.name,
            "task": str(folder),
            "filed": filed,
            "unfiled": len(waiting) - len(filed),
        },
        0,
    )


def _current_inputs(unit_kind: UnitKind, entry: QueueEntry) -> Inputs | None:
    """The unit's inputs, if they are still the ones its current answer was made for.

    A unit whose inputs changed since its latest pass is not filed: liulab-llms would refuse a
    review made for its old inputs, so it waits for a pass over the new ones.
    """
    from labllm import Held

    try:
        inputs = unit_kind.load(entry.key)
    except Held:
        return None
    return inputs if inputs.tag == entry.inputs_tag else None
