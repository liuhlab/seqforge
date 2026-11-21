"""The one door the reader asks for ontology terms through, and the stand-in behind it for now.

The ontologies, their dated snapshots and the search over them are liulab-data's, so seqforge keeps
no ontology file and no ontology code. The reader hands a label's short text and a vocabulary to an
:class:`OntologySearch` and gets back up to :data:`MOST_CANDIDATES` candidates, whole-text matches
first, and the snapshot that answered. Until liulab-data's search is plugged in here,
:data:`STAND_IN` answers every search with no candidate, so a reading writes no searched term.

:func:`vocabulary` says which vocabulary a label's terms come from, for a sample of one species.
"""

from __future__ import annotations

import datetime
from dataclasses import dataclass
from typing import Protocol

#: The most candidates one search returns.
MOST_CANDIDATES = 10

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
class Snapshot:
    """The dated snapshot of the ontologies a search answered from."""

    source: str
    date: datetime.date


@dataclass(frozen=True)
class Found:
    """What one search returned: candidates, whole-text matches first, and who answered."""

    candidates: tuple[Candidate, ...]
    snapshot: Snapshot | None


class OntologySearch(Protocol):
    """Looks up a label's short text in one vocabulary."""

    def search(self, text: str, vocabulary: str) -> Found:
        """Up to :data:`MOST_CANDIDATES` candidates for ``text``, whole-text matches first."""
        ...


class NoSearch:
    """The stand-in: no ontology is held yet, so every search finds nothing."""

    def search(self, text: str, vocabulary: str) -> Found:
        """No candidate, and no snapshot, since nothing answered."""
        return Found((), None)


#: What the reader searches with until liulab-data's search is plugged in.
STAND_IN: OntologySearch = NoSearch()


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
