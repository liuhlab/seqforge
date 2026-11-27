"""seqforge's unit kinds: the table of them by name, and what a kind is.

The kind table, :data:`KIND_MODULES`, is the one place a kind's name is written. It names the module
that defines each kind as its ``KIND``, and that module reads its own name back with
:func:`name_of`. Every pass command finds a kind here by the name a person types. A module that
defines a kind imports liulab-llms, so it is imported only when its kind is asked for. A test lists
its own kind in the table.
"""

from __future__ import annotations

import importlib
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from . import SetupRefused

if TYPE_CHECKING:
    from labllm import Model, TaskReader, UnitKind


def _nothing() -> None:
    """Release nothing: what a kind that keeps nothing open closes."""


@dataclass(frozen=True)
class Built:
    """A kind built for one pass, and what the pass needs of the stores it opened.

    ``store_build`` is the build of liulab-data's record store the kind reads, ``None`` for a kind
    that reads no record store. ``close`` releases what the build opened; the pass calls it once it
    is over.
    """

    unit_kind: UnitKind
    store_build: str | None = None
    close: Callable[[], None] = _nothing


@dataclass(frozen=True)
class Kind:
    """A seqforge unit kind: liulab-llms's kind plus what seqforge decides around it.

    ``build`` makes the liulab-llms ``UnitKind``, named ``name``, once when a pass starts. It is
    where the kind receives its setup: the folder of study folders the pass names, and every store
    the kind reads, which it opens then, so each unit of the pass reads what was open then. It
    raises :class:`SetupRefused`, naming the store, for one it cannot open. ``default_keys`` gives
    the units a pass over that folder covers when it names none, in the order they should start.
    ``default_models`` fills every role a pass does not name. ``write`` turns the answers in a task
    into one file per study, ``write(task, name, out, file_name)``, and says how each study ended:
    what ``study write-bounds`` and ``study write-reading`` run, and ``study run`` between passes.
    """

    name: str
    build: Callable[[Path], Built]
    default_models: Mapping[str, Model]
    default_keys: Callable[[Path], Iterable[str]]
    write: Callable[[TaskReader, str, Path, str], dict[str, dict[str, Any]]]


#: Every seqforge kind, by the name a person types: the module that defines it as its ``KIND``.
KIND_MODULES: dict[str, str] = {
    "study-bounds": "seqforge.study.bounding",
    "study-reading": "seqforge.study.reader",
}


def name_of(module: str) -> str:
    """The name the kind table gives the kind that ``module`` defines.

    Raises ``ValueError`` for a module the table does not list, so a kind's module that moved
    without its row fails on import.
    """
    for name, listed in KIND_MODULES.items():
        if listed == module:
            return name
    raise ValueError(f"the kind table lists no kind defined by {module}")


def kind_named(name: str) -> Kind:
    """The kind the table lists as ``name``. Raises :class:`SetupRefused` naming the ones there are."""
    try:
        module = KIND_MODULES[name]
    except KeyError:
        known = ", ".join(sorted(KIND_MODULES))
        raise SetupRefused(f"no kind named {name!r}; the kinds are {known}") from None
    kind: Kind = importlib.import_module(module).KIND
    return kind
