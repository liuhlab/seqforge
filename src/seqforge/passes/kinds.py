"""The registry of seqforge's unit kinds, by name.

A kind is registered once, by the module that defines it, and every pass and review command finds
it here by the name a person types. A module that defines a kind imports liulab-llms, so it is
imported only when its kind is asked for: :data:`KIND_MODULES` says which module registers which
name. Tests register their own.
"""

from __future__ import annotations

import importlib
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from . import SetupRefused

if TYPE_CHECKING:
    # liulab-data is a pixi dependency and not a wheel one, so nothing here imports it at run time.
    from labdata import Store
    from labllm import Model, QueueEntry, UnitKind

    from .review import ReviewForm


@dataclass(frozen=True)
class Kind:
    """A seqforge unit kind: liulab-llms's kind plus what seqforge decides around it.

    ``build`` makes the liulab-llms ``UnitKind``, named ``name``. It is handed the store a pass
    opened -- one build, read once when the pass starts -- when the kind reads liulab-data's store,
    and ``None`` when it does not, so the kind's ``load`` closes over that one build and never opens
    the store itself. ``default_keys`` gives the units a pass covers when it names none, in the order
    they should start, from the same store. ``default_models`` fills every role a pass does not name.

    ``review_form`` is how the kind's units show in a review issue and how a person's ticks become
    its answer. ``review_priority`` orders its review queue for filing: a sort key over each entry,
    given the same store, lowest filed first. Without one, the queue is filed in key order.
    """

    name: str
    build: Callable[[Store | None], UnitKind]
    default_models: Mapping[str, Model]
    default_keys: Callable[[Store | None], Iterable[str]]
    review_form: ReviewForm
    review_priority: Callable[[Store | None, QueueEntry], Any] | None = None
    reads_store: bool = False

    def unit_kind(self, store: Store | None) -> UnitKind:
        """The liulab-llms kind over ``store``. Raises ``ValueError`` if its name is not this one's."""
        made = self.build(store)
        if made.name != self.name:
            raise ValueError(f"kind {self.name!r} builds a liulab-llms kind named {made.name!r}")
        return made


#: Every registered kind, by name.
KINDS: dict[str, Kind] = {}

#: The module that registers each of seqforge's own kinds when it is imported, by the kind's name.
KIND_MODULES: dict[str, str] = {"study-bounds": "seqforge.study.bounding"}


def register(kind: Kind) -> Kind:
    """Register ``kind`` under its name. Raises ``ValueError`` if that name is taken."""
    if kind.name in KINDS:
        raise ValueError(f"a kind named {kind.name!r} is already registered")
    KINDS[kind.name] = kind
    return kind


def kind_named(name: str) -> Kind:
    """The kind registered as ``name``. Raises :class:`SetupRefused` naming the ones there are."""
    if name not in KINDS and name in KIND_MODULES:
        importlib.import_module(KIND_MODULES[name])
    try:
        return KINDS[name]
    except KeyError:
        known = ", ".join(sorted({*KINDS, *KIND_MODULES})) or "none yet"
        raise SetupRefused(f"no kind named {name!r}; registered kinds: {known}") from None
