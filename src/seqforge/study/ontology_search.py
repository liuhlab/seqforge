"""The one door the reader asks for ontology terms through: liulab-data's ontology store.

The ontologies, their dated snapshots and the search over them are liulab-data's, so seqforge keeps
no ontology file and no ontology code. The reader hands a label's short text and a vocabulary to an
:class:`OntologySearch` and gets back up to :data:`MOST_CANDIDATES` candidates, whole-text matches
first, and the snapshot that answered; it names a code-mapped id, such as a species' taxonomy id,
through the same store. :class:`StoreSearch` opens the store once, which pins each
vocabulary's snapshot until it is closed, so one pass reads one set of snapshots.

:func:`vocabulary` says which vocabulary a label's terms come from, for a sample of one species.
"""

from __future__ import annotations

import datetime
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import Any, Protocol

#: The most candidates one search returns.
MOST_CANDIDATES = 10
#: What ``source`` in a reading's ``ontology_snapshot`` starts with, before the snapshots' names.
STORE_SOURCE = "liulab-data ontology store"

#: Each label whose terms a search finds, and its vocabulary. ``None`` means by species.
_BY_LABEL = {
    "assay": "efo",
    "tissue": "uberon",
    "cell_type": "cl",
    "cell_line": "cellosaurus",
    "disease": "mondo",
    "treatment": "chebi",
    "development_stage": None,
}
#: The stage list of each species that has one, by NCBI Taxonomy id. A rat stage has none.
_STAGES = {"9606": "hsapdv", "10090": "mmusdv", "6239": "wbls", "7227": "fbdv"}
#: *C. elegans* names its tissues in its own anatomy list.
_WORM, _WORM_TISSUE = "6239", "wbbt"


@dataclass(frozen=True)
class Candidate:
    """One term a search found: its id, the ontology's own name for it, and the text it matched."""

    id: str
    name: str
    matched: str


@dataclass(frozen=True)
class Found:
    """What one search returned: candidates, whole-text matches first, and the snapshot searched.

    ``snapshot`` is ``None`` when the store holds no such vocabulary, so nothing was searched.
    """

    candidates: tuple[Candidate, ...]
    snapshot: str | None


@dataclass(frozen=True)
class Named:
    """A term looked up by its id: the ontology's name for it and the snapshot that holds it."""

    id: str
    name: str
    snapshot: str


class OntologySearch(Protocol):
    """Looks up a label's short text in one vocabulary, and a term by its id.

    ``date`` is the day the snapshots it answers from were pinned, and ``snapshots`` their names,
    sorted: every vocabulary's, searched or not.
    """

    date: datetime.date
    snapshots: tuple[str, ...]

    def search(self, text: str, vocabulary: str) -> Found:
        """Up to :data:`MOST_CANDIDATES` candidates for ``text``, whole-text matches first."""
        ...

    def lookup(self, id: str) -> Named | None:
        """The live term ``id`` names, a bare number being a taxonomy id; ``None`` if none."""
        ...


class StoreSearch:
    """liulab-data's ontology store, opened once and pinned until :meth:`close`.

    The store is found through ``LABDATA_ONTOLOGIES`` or its per-user pointer; liulab-data's own
    error, which names the variable, is raised when none can be opened. ``date`` is the UTC day it
    was opened, because the store names its snapshots by UTC day.

    An open store answers only on the thread that opened it, and a pass reads several units at
    once on threads of its own, so one thread of this object's opens the store and asks it every
    question. Each search takes milliseconds, so the units queueing for it cost little.
    """

    def __init__(self) -> None:
        from labdata.ontology import open_ontology_store

        self.date = datetime.datetime.now(datetime.UTC).date()
        self._owner = ThreadPoolExecutor(max_workers=1, thread_name_prefix="ontology-store")
        try:
            self._store: Any = self._owner.submit(open_ontology_store).result()
        except BaseException:
            self._owner.shutdown()
            raise
        self.snapshots = tuple(sorted(self._store.snapshots.values()))

    def search(self, text: str, vocabulary: str) -> Found:
        """Up to :data:`MOST_CANDIDATES` candidates; none, and no snapshot, for an unheld vocabulary."""
        return self._owner.submit(self._search, text, vocabulary).result()

    def _search(self, text: str, vocabulary: str) -> Found:
        snapshot = self._store.snapshots.get(vocabulary)
        if snapshot is None:
            return Found((), None)
        found = self._store.search(text, vocabulary, limit=MOST_CANDIDATES)
        return Found(tuple(Candidate(c.id, c.name or c.text, c.text) for c in found), snapshot)

    def lookup(self, id: str) -> Named | None:
        """The term ``id`` names; ``None`` for an id no snapshot holds or one retired unnamed."""
        term = self._owner.submit(self._store.lookup, id).result()
        if term is None or term.name is None or term.obsolete:
            return None
        return Named(term.id, term.name, term.snapshot)

    def close(self) -> None:
        """Release the snapshots and the thread that read them."""
        self._owner.submit(self._store.close).result()
        self._owner.shutdown()


def snapshot_source(snapshots: set[str]) -> str:
    """A reading's ``ontology_snapshot.source``: the store, and each snapshot its ids came from."""
    return f"{STORE_SOURCE}: {', '.join(sorted(snapshots))}"


def vocabulary(label: str, taxid: str | None) -> str | None:
    """The vocabulary a label's terms come from for a sample of species ``taxid``.

    ``None`` when the label takes no searched term: one with no vocabulary, such as a genotype, a
    stage of a species with no stage list, such as the rat, or a species, whose id is the record's.
    """
    if label == "development_stage":
        return _STAGES.get(str(taxid))
    if label == "tissue" and str(taxid) == _WORM:
        return _WORM_TISSUE
    return _BY_LABEL.get(label)
