"""A folder of study folders: what each holds, which studies it lists, how a job's file is written.

A study folder is named by the study's id. A person names the folder when a pass starts, and the
standard set, kept under ``evals/standard-set/``, is the default while no other is named. Its 50
studies' own files were drawn once, by commands since retired; a study swapped into the set is
drawn by the bounding job, :mod:`seqforge.study.bounding`. A folder's pick lists its studies; a
folder with no pick has its studies named by the pass.

A job's pass keeps each study's answer in its task. :func:`write_files` turns each current answer
into ``<out>/<key>/<name>`` with the job's own ``render``, checks the exact text it would write,
and says how each study ended. The bounding job and the reading job both write through it, and the
reading job reads each study's bounds file with :func:`bounds_in`.

This module is the only code that knows a study folder's file names. Any other package reads a
study folder through :func:`open_study_folder`, which hands back its files each through its format
check, or refuses the folder.
"""

from __future__ import annotations

import os
from collections.abc import Callable, Collection
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

import yaml

from ..passes import SetupRefused
from .bounds import Bounds, read_bounds
from .claims import load
from .reading import Reading, read_reading

if TYPE_CHECKING:
    from labllm import Outcome, TaskReader

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


def picked_studies(folder: Path) -> list[str]:
    """The studies a pass over ``folder`` covers when it names none: its pick's, in order.

    Refuses the setup for a folder with no pick, whose studies only the pass can name.
    """
    if not (folder / PICK).is_file():
        raise SetupRefused(f"{folder} has no {PICK}: name the studies with --keys FILE")
    return list(pick_entries(folder))


class StudyFolderRefused(Exception):
    """A study folder the reader will not open: what is wrong with it, by the file it is in."""

    def __init__(self, folder: Path, problems: dict[str, list[dict[str, str]]]) -> None:
        self.folder = folder
        #: Each file's problems by its name, each naming a field and the reason.
        self.problems = problems
        said = "; ".join(
            f"{name} {p['field']}: {p['reason']}" for name, found in problems.items() for p in found
        )
        super().__init__(f"{folder} is refused: {said}")


@dataclass(frozen=True)
class StudyFolder:
    """One study folder, opened and checked: what later stages read of a study.

    ``study`` is the standard set's own study file, which only a study of the set has. A folder
    always holds its bounds, and its reading once the reading job has written one.

    ``superseries`` is the SuperSeries the study file names, and ``subseries`` the Series under it.
    Only that file says which Series is the SuperSeries; a bounds file does not, so any other
    folder has none.
    """

    path: Path
    study: dict[str, Any] | None
    bounds: Bounds
    reading: Reading | None
    superseries: str | None = None
    subseries: frozenset[str] = frozenset()


def _superseries(study: dict[str, Any] | None) -> tuple[str | None, frozenset[str]]:
    """The SuperSeries a study file names, and the Series under it.

    The file has no format check, so each field is taken only in the shape it is written.
    """
    records = (study or {}).get("records")
    if not isinstance(records, dict):
        return None, frozenset()
    parent, series = records.get("superseries"), records.get("series")
    if not isinstance(parent, str) or not isinstance(series, list):
        return None, frozenset()
    return parent, frozenset(s for s in series if isinstance(s, str) and s != parent)


def _checked[T](
    path: Path, read: Callable[[object], tuple[T | None, list[dict[str, str]]]]
) -> tuple[T | None, list[dict[str, str]]]:
    """The file at ``path`` through its check ``read``, or ``None`` and what is wrong with it."""
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        return None, [{"field": "(file)", "reason": f"cannot read: {exc}"}]
    data, problems = load(text)
    if problems:
        return None, problems
    found, problems = read(data)
    return (None, problems) if found is None or problems else (found, [])


def _study_file(data: object) -> tuple[dict[str, Any] | None, list[dict[str, str]]]:
    """The standard set's study file. It has no format number, so its shape is all there is to check."""
    if not isinstance(data, dict):
        return None, [{"field": "(file)", "reason": "not a mapping of fields"}]
    return data, []


def open_study_folder(folder: Path) -> StudyFolder:
    """The study folder at ``folder``, each of its files read through its format check.

    A standard-set folder also holds the jobs' files, ``job-bounds.yaml`` and ``job-reading.yaml``,
    beside the set's own: the reader opens the set's own and never the jobs'. Refuses the folder,
    naming each file and its problems, when it has no bounds file or when a file it holds fails its
    check. The reading check judges ontology ids by liulab-data, so without liulab-data a folder
    with a reading raises :class:`~seqforge.liulab_data.LiulabDataMissing`.
    """
    problems: dict[str, list[dict[str, str]]] = {}
    study = bounds = reading = None
    if (folder / STUDY).is_file():
        study, problems[STUDY] = _checked(folder / STUDY, _study_file)
    if (folder / BOUNDS).is_file():
        bounds, problems[BOUNDS] = _checked(folder / BOUNDS, read_bounds)
    else:
        problems[BOUNDS] = [
            {"field": "(file)", "reason": "missing: a study folder holds its bounds"}
        ]
    if (folder / READING).is_file():
        reading, problems[READING] = _checked(folder / READING, read_reading)
    problems = {name: found for name, found in problems.items() if found}
    if problems or bounds is None:
        raise StudyFolderRefused(folder, problems)
    parent, subseries = _superseries(study)
    return StudyFolder(
        path=folder,
        study=study,
        bounds=bounds,
        reading=reading,
        superseries=parent,
        subseries=subseries,
    )


def bounds_in(folder: Path, key: str) -> Bounds:
    """Study ``key``'s bounds, read from its bounds file in ``folder`` through the format check.

    Holds the unit when the file is missing or cannot be read, and when the check finds a problem
    in it, naming each one.
    """
    from labllm import Held

    path = folder / key / BOUNDS
    if not path.is_file():
        raise Held(f"no bounds file for {key} at {path}")
    bounds, problems = _checked(path, read_bounds)
    if bounds is None:
        said = "; ".join(f"{p['field']}: {p['reason']}" for p in problems)
        raise Held(f"{path} fails the bounds format check: {said}")
    return bounds


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
    task: TaskReader,
    kind: str,
    out: Path,
    name: str,
    render: Callable[[Outcome], dict[str, Any]],
    check: Check,
    *,
    keys: Collection[str] | None = None,
) -> dict[str, dict[str, Any]]:
    """Write ``<out>/<key>/<name>`` for each unit with an answer, and say how each study ended.

    The units are those of the kind named ``kind``, only those ``keys`` names when it names any:
    a task is shared, so a write for one set of studies leaves out the others the task answered.
    A refused study gets no file, and its reason
    instead. ``render`` gives the file's content, each
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
        if keys is not None and key not in keys:
            continue
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
