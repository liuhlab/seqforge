"""A folder of study folders: what each holds, which studies it lists, how a job's file is written.

A study folder is named by the study's id. The standard set, kept under ``evals/standard-set/``,
is one folder of them, and the default while no other is named. Its 50 studies' own files were
drawn once, by commands since retired; a study swapped into the set is drawn by the bounding job,
:mod:`seqforge.study.bounding`.

A job's pass keeps each study's answer in its task. :func:`write_files` turns each current answer
into ``<out>/<key>/<name>`` with the job's own ``render``, checks the exact text it would write,
and says how each study ended. The bounding job and the reading job both write through it.
"""

from __future__ import annotations

import os
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING, Any

import yaml

from .claims import load

if TYPE_CHECKING:
    from labllm import Outcome, Task, UnitKind

#: A study's own file, its bounds file and its reading file, in the study's folder.
STUDY, BOUNDS, READING = "study.yaml", "bounds.yaml", "reading.yaml"
#: The bounding job's and the reading job's file in a standard-set study folder, beside the set's
#: own ``bounds.yaml`` and ``reading.yaml``.
JOB_BOUNDS, JOB_READING = "job-bounds.yaml", "job-reading.yaml"
#: The list the studies were picked into, beside the study folders.
PICK = "pick.yaml"

#: A format check: the problems it finds in a file's content, each naming a field and the reason.
Check = Callable[[object], list[dict[str, str]]]


def default_set_dir() -> Path:
    """``<repo>/evals/standard-set``. The set ships with the repo, so this is a relative walk-up."""
    return Path(__file__).resolve().parents[3] / "evals" / "standard-set"


def pick_entries(folder: Path) -> dict[str, dict[str, Any]]:
    """The picked studies by id, in the pick's order. Spares are not studies until one is swapped in."""
    pick = yaml.safe_load((folder / PICK).read_text(encoding="utf-8"))
    return {str(entry["id"]): entry for entry in pick["studies"]}


def file_text(content: Any) -> str:
    """A study file's text, as every job writes one."""
    return yaml.safe_dump(content, sort_keys=False, width=100, allow_unicode=True)


def text_problems(text: str, check: Check) -> list[dict[str, str]]:
    """What ``check`` finds wrong with a file of ``text``, read back as the file would be read."""
    data, problems = load(text)
    return problems or check(data)


def model_name(outcome: Outcome, role: str | None) -> str | None:
    """The model that filled ``role`` in the pass that gave ``outcome``, as ``provider/name``."""
    if role is None:
        return None
    model = outcome.stamp.models[role]
    return f"{model.provider}/{model.name}"


def write_files(
    task: Task,
    kind: UnitKind,
    out: Path,
    name: str,
    render: Callable[[Outcome], dict[str, Any]],
    check: Check,
) -> dict[str, dict[str, Any]]:
    """Write ``<out>/<key>/<name>`` for each unit with an answer, and say how each study ended.

    A refused study gets no file, and its reason instead. ``render`` gives the file's content, each
    model already named as the pass's stamp records it, and ``check`` runs on the exact text that
    would be written: a text it rejects is not written, and its problems are said instead. A file
    already on disk is never overwritten: delete it by hand to take a newer answer. ``name`` is a
    file name, not a path, so no two studies can share a file, and each file appears whole or not
    at all.
    """
    if not name or name in {".", ".."} or Path(name).name != name:
        raise ValueError(f"--name must be a file name, not a path: {name!r}")
    done: dict[str, dict[str, Any]] = {}
    for key in task.keys(kind, endings=["settled", "unsettled", "refused"]):
        outcome = task.current(kind, key)
        if outcome is None:
            continue
        said: dict[str, Any] = {"ending": outcome.ending}
        if outcome.answer is None:
            said["reason"] = outcome.reason
        else:
            path = out / key / name
            if path.exists():
                said["file"] = "kept the file on disk"
            else:
                text = file_text(render(outcome))
                if problems := text_problems(text, check):
                    said["file"] = "not written: the format check rejects it"
                    said["problems"] = problems
                else:
                    said["file"] = "written"
                    _publish(path, text)
            if failed := [f"{c.name}: {c.message}" for c in outcome.checks if c.message]:
                said["checks_failed"] = failed
        done[key] = said
    return done


def _publish(path: Path, text: str) -> None:
    """Write ``text`` to ``path`` whole or not at all, and never over a file already there.

    The text goes to a hidden file beside ``path`` first, which a hard link then publishes: the link
    fails when ``path`` exists, so a file another run wrote first is kept.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    partial = path.with_name(f".{path.name}.partial")
    partial.write_text(text, encoding="utf-8")
    try:
        os.link(partial, path)
    except FileExistsError:
        pass
    finally:
        partial.unlink()
