"""`seqforge review`: a kind's review queue as issues in the lab's private review repo.

`review file` posts a small sample of the queue -- the kind's unsettled and refused units -- as
issues, one unit each, which a person answers by ticking boxes and closing. Each issue carries a
hidden marker naming its kind, key and inputs tag, so a unit is never filed twice for the same
inputs and reading back needs no local list of what was filed. It prints one JSON object and exits
0, 2 for a setup it cannot use, or 3 when another process holds the task or GitHub fails.

`review read` reads the closed issues back: each answer or drop is stored through liulab-llms into
the kind's reviews file in the clone, which it commits and pushes. It never opens the task, so it
runs while a pass holds it. It exits 4 when it reopened an issue for a person to answer again.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any

import typer

from ._common import _emit, _StageOut
from ._kind_task import KindTask, for_kind, inputs_now
from .root import review_app

if TYPE_CHECKING:
    from labllm import UnitKind

    from ..passes.github import GitHub, Issue
    from ..passes.kinds import Kind


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
    from ..passes import github
    from ..passes.github import GitHubError
    from ..passes.review import KIND_LABEL, issue_for, labels_for, read_marker
    from ..passes.task import store_for

    def run(setup: KindTask) -> _StageOut:
        kind = setup.kind
        repo = github.make_github(setup.reviews.parent)
        store, _ = store_for(kind)
        unit_kind = kind.unit_kind(store)
        # Reading the queue takes the task's hold, so a running pass makes this exit 3.
        with setup.read() as opened:
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
                # A unit whose inputs changed since its latest pass is not filed: liulab-llms would
                # refuse a review made for its old inputs, so it waits for a pass over the new ones.
                inputs = inputs_now(unit_kind, entry.key)
                if inputs is None or inputs.tag != entry.inputs_tag:
                    continue
                title, body = issue_for(kind.review_form, entry, inputs)
                wanted = labels_for(entry)
                for label, (color, description) in wanted.items():
                    if label not in labels:
                        repo.create_label(label, color=color, description=description)
                        labels.add(label)
                issue = repo.create_issue(title=title, body=body, labels=list(wanted))
                filed.append({"key": entry.key, "url": issue.url})
        except GitHubError as exc:
            payload = {"kind": kind.name, "task": str(setup.folder), "filed": filed}
            return _StageOut({**payload, "stopped": "github_failed", "message": str(exc)}, 3)
        return _StageOut(
            {
                "kind": kind.name,
                "task": str(setup.folder),
                "filed": filed,
                "unfiled": len(waiting) - len(filed),
            },
            0,
        )

    return for_kind(name, task=task, reviews=reviews, body=run)


@review_app.command("read")
def review_read(
    kind: str = typer.Argument(..., help="The registered seqforge kind whose issues to read back."),
    reviews: Path | None = typer.Option(
        None,
        "--reviews",
        envvar="SEQFORGE_REVIEWS",
        help="A local clone of the review repo: the reviews are stored in it, committed and pushed.",
    ),
) -> None:
    """Read people's answers back from a kind's closed issues, store them, and print its JSON.

    An issue closed as completed stores the answer its ticks make, and one closed as not planned
    a drop with the reasons ticked, each as the person who closed it. An issue already read back is
    passed over. Ticks that make no answer, or an answer that fails the kind's checks, store
    nothing: the issue gets a comment saying why and is reopened. The kind's reviews file is then
    committed alone and pushed. Exit 0; 4 when it reopened an issue; 2 for a setup it cannot use;
    3 when GitHub or git fails (stored reviews stay committed in the clone; rerun to push them).
    """
    _emit(_review_read(name=kind, reviews=reviews))


def _review_read(*, name: str, reviews: Path | None) -> _StageOut:
    """The body of ``review read``: every setup refusal, then each closed issue, then git."""
    from ..passes import github
    from ..passes.task import store_for

    def run(setup: KindTask) -> _StageOut:
        repo = github.make_github(setup.reviews.parent)
        # Storing a review loads the unit's inputs, so a kind that reads the store needs it now.
        store, _ = store_for(setup.kind)
        unit_kind = setup.kind.unit_kind(store)
        # No task is opened: liulab-llms's reviews need none, so a running pass never blocks this.
        return _read_back(setup.kind, unit_kind, repo, setup.reviews)

    return for_kind(name, task=None, reviews=reviews, body=run)


def _read_back(kind: Kind, unit_kind: UnitKind, repo: GitHub, review_files: Path) -> _StageOut:
    """Store what each closed issue of ``kind`` says, then commit and push its reviews file."""
    from labllm import ChecksFailed, Reviews

    from ..passes import clone
    from ..passes.github import GitHubError
    from ..passes.review import (
        INPUTS_CHANGED_LABEL,
        KIND_LABEL,
        LOST_MARKER,
        Unreadable,
        read_answer,
        read_drop,
        read_marker,
    )

    saved = Reviews(review_files)
    stored = {(review.key, review.inputs_tag) for review in saved.read(unit_kind)}
    out: dict[str, Any] = {
        "kind": kind.name,
        "answered": [],
        "dropped": [],
        "reopened": [],
        "inputs_changed": [],
    }
    stop: tuple[str, str] | None = None
    try:
        for issue in sorted(repo.issues(KIND_LABEL.format(kind.name)), key=lambda i: i.number):
            # An issue labelled as having changed inputs was read once and has nothing to store.
            if issue.state != "closed" or INPUTS_CHANGED_LABEL in issue.labels:
                continue
            marker = read_marker(issue.body)
            if marker is None or marker.kind != kind.name:
                reason = LOST_MARKER if marker is None else _other_kind(marker.kind, kind.name)
                _reopen(repo, issue, reason)
                key = None if marker is None else marker.key
                out["reopened"].append({"key": key, "url": issue.url, "reason": reason})
                continue
            if (marker.key, marker.inputs_tag) in stored:
                continue
            inputs = inputs_now(unit_kind, marker.key)
            now = None if inputs is None else inputs.tag
            if now != marker.inputs_tag:
                _set_inputs_changed(repo, issue, marker.inputs_tag, now)
                out["inputs_changed"].append({"key": marker.key, "url": issue.url})
                continue
            entry: dict[str, Any] = {"key": marker.key, "url": issue.url}
            try:
                if issue.state_reason == "not_planned":
                    note = read_drop(issue.body)
                    who = repo.closer(issue.number)
                    saved.save(
                        unit_kind,
                        marker.key,
                        who=who,
                        inputs_tag=marker.inputs_tag,
                        drop=True,
                        note=note,
                    )
                    out["dropped"].append({**entry, "who": who, "note": note})
                else:
                    answer, accepted = read_answer(kind.review_form, issue.body)
                    who = repo.closer(issue.number)
                    saved.save(
                        unit_kind,
                        marker.key,
                        who=who,
                        inputs_tag=marker.inputs_tag,
                        answer=answer,
                        accepted_pass=accepted,
                    )
                    out["answered"].append({**entry, "who": who, "accepted_pass": accepted})
            except ChecksFailed as exc:
                failed = "; ".join(f"{check.name}: {check.message}" for check in exc.failed)
                reason = f"The answer fails the kind's checks: {failed}."
            except Unreadable as exc:
                reason = str(exc)
            except ValueError as exc:
                reason = f"The answer cannot be stored: {exc}."
            else:
                stored.add((marker.key, marker.inputs_tag))
                continue
            _reopen(repo, issue, reason)
            out["reopened"].append({**entry, "reason": reason})
    except GitHubError as exc:
        stop = ("github_failed", str(exc))

    # Whatever was stored is shared, even after GitHub failed part of the way through.
    pushed: bool | None
    try:
        message = f"Read back reviews of {kind.name}"
        pushed = True if clone.share(review_files.parent, saved.path(unit_kind), message) else None
    except clone.GitFailed as exc:
        pushed = False
        stop = stop or ("git_failed", str(exc))
    out["pushed"] = pushed
    if stop is not None:
        out["stopped"], out["message"] = stop
        return _StageOut(out, 3)
    return _StageOut(out, 4 if out["reopened"] else 0)


def _other_kind(found: str, wanted: str) -> str:
    return f"The issue's seqforge marker names kind {found}, not {wanted}."


#: What a person reads on an issue reopened because nothing could be stored from it.
_REOPENED = (
    "`seqforge review read` stored nothing from this issue: {reason}\n\n"
    "Fix this in the issue, then close it again: as completed to keep your answer, or as not "
    "planned to drop the unit."
)


def _reopen(repo: GitHub, issue: Issue, reason: str) -> None:
    repo.comment(issue.number, _REOPENED.format(reason=reason))
    repo.reopen(issue.number)


def _set_inputs_changed(repo: GitHub, issue: Issue, filed: str, now: str | None) -> None:
    """Say on the issue that its unit's inputs changed, and label it so it is never read again.

    liulab-llms stores no review made for inputs a unit no longer has, and no person can change
    that from the issue, so it stays closed. The comment comes first: a label added without it
    would hide the issue from the next read with nothing said.
    """
    from ..passes.review import INPUTS_CHANGED_COLOR, INPUTS_CHANGED_LABEL

    changed = f"from `{filed}` to `{now}`" if now is not None else "and are not there now"
    repo.comment(
        issue.number,
        f"`seqforge review read` stored nothing from this issue: the unit's inputs changed after "
        f"it was filed, {changed}, so this answer is for inputs the unit no longer has. A pass "
        "answers the new inputs, and the unit is filed again if it still needs a person. This "
        "issue stays closed.",
    )
    if INPUTS_CHANGED_LABEL not in repo.labels():
        description = "The unit's inputs changed after filing, so nothing was stored"
        repo.create_label(INPUTS_CHANGED_LABEL, color=INPUTS_CHANGED_COLOR, description=description)
    repo.add_labels(issue.number, [INPUTS_CHANGED_LABEL])
