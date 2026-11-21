"""Writing a study job's answers as files: one per study, beside the files already there, never over them.

A job's pass keeps each study's answer in its task. :func:`write_files` turns each current answer
into ``<out>/<key>/<name>`` with the job's own ``render``, and says how each study ended. The
bounding job and the reading job both write through it.
"""

from __future__ import annotations

import os
from collections.abc import Callable
from pathlib import Path
from typing import Any

import yaml
from labllm import Outcome, Task, UnitKind


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
) -> dict[str, dict[str, Any]]:
    """Write ``<out>/<key>/<name>`` for each unit with an answer, and say how each study ended.

    A refused study gets no file, and its reason instead. A file already on disk is never
    overwritten: delete it by hand to take a newer answer. ``name`` is a file name, not a path, so
    no two studies can share a file, and each file appears whole or not at all.
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
            said["file"] = "kept the file on disk" if path.exists() else "written"
            if not path.exists():
                text = yaml.safe_dump(
                    render(outcome), sort_keys=False, width=100, allow_unicode=True
                )
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
