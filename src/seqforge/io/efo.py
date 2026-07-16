"""EFO assay labels — what ``EFO:0009922`` is called, in the words of the people who own the term.

``assay: EFO:0009922`` is good standardization and unreadable to a human, which was the first thing
the pilot's manifest got told off for. The fix is a name, and the name is not ours to write: it comes
from EFO via EBI's OLS4, is generated into ``efo/labels.json`` by ``seqforge io efo refresh``, and is
never typed by hand. The KB's ``spec.yaml`` files each carry a comment saying which term they mean —
comments are not checked, and a comment claiming a label is exactly the hand-maintained contract that
rots. ``kb lint`` now reads *this* file instead.

Shipped rather than fetched for the same reason the BioSample vocabulary is: a manifest must be
fillable on a compute node with no internet. Five terms, so the file is tiny; it grows by one entry
per KB technology, which is the rate at which anything here grows.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from functools import cache
from pathlib import Path

#: EBI's Ontology Lookup Service, v4. The term IRI is **double**-URL-encoded in the path — OLS4
#: returns 404 for a singly-encoded one, which is the kind of detail that belongs in code and a test
#: rather than in somebody's memory.
OLS4_TERMS = "https://www.ebi.ac.uk/ols4/api/ontologies/efo/terms/"

#: How a CURIE becomes an IRI. EFO's own namespace; the OBO-style terms EFO borrows (UBERON, CL) use
#: a different one, which is why this is not a general CURIE expander and does not pretend to be.
EFO_IRI_BASE = "http://www.ebi.ac.uk/efo/"

_DATA = Path(__file__).parent / "efo" / "labels.json"


@dataclass(frozen=True)
class EfoTerm:
    """One ontology term: its id, its label, and what else it is called."""

    curie: str
    name: str
    iri: str
    synonyms: tuple[str, ...] = ()


class TermUnknown(KeyError):
    """A CURIE with no shipped label. Never a silently blank name."""


@cache
def load_terms() -> dict[str, EfoTerm]:
    if not _DATA.is_file():
        return {}
    doc = json.loads(_DATA.read_text(encoding="utf-8"))
    return {
        curie: EfoTerm(
            curie=curie,
            name=str(meta["name"]),
            iri=str(meta.get("iri", "")),
            synonyms=tuple(meta.get("synonyms", ())),
        )
        for curie, meta in sorted(doc["terms"].items())
    }


def has_term(curie: str) -> bool:
    return curie in load_terms()


def term(curie: str) -> EfoTerm:
    """The term ``curie`` names. Raises rather than returning a blank: an unlabelled assay is a bug.

    It means a KB spec declares a CURIE nobody fetched a label for, which ``kb lint`` refuses — so
    reaching this raise means the lint was skipped, and a manifest with an empty assay name would be
    a worse outcome than a loud one.
    """
    try:
        return load_terms()[curie]
    except KeyError as exc:
        raise TermUnknown(
            f"no shipped EFO label for {curie!r}. Fetch it with `seqforge io efo refresh {curie}` "
            f"and commit `io/efo/labels.json` — the label comes from EFO, never from us."
        ) from exc


def iri_for(curie: str) -> str:
    """``EFO:0009922`` -> ``http://www.ebi.ac.uk/efo/EFO_0009922``."""
    return EFO_IRI_BASE + curie.replace(":", "_")


def parse_ols4_term(payload: dict[str, object]) -> EfoTerm:
    """OLS4's JSON -> an :class:`EfoTerm`. The only reader of that shape.

    An obsolete term is refused rather than shipped: EFO deprecates assay terms (it has replaced 10x
    ids before), and silently pinning a manifest's vocabulary to a dead one is how a corpus ends up
    self-consistent and wrong.
    """
    if payload.get("is_obsolete"):
        raise TermUnknown(
            f"EFO says {payload.get('short_form')!r} is obsolete. Refusing to ship it as an assay "
            f"label: find the term that replaced it."
        )
    label = payload.get("label")
    iri = payload.get("iri")
    if not isinstance(label, str) or not isinstance(iri, str):
        raise TermUnknown(f"OLS4 returned no label/iri for {payload.get('short_form')!r}")
    short = str(payload.get("short_form") or "").replace("_", ":")
    raw = payload.get("synonyms")
    synonyms = tuple(sorted(s for s in raw if isinstance(s, str))) if isinstance(raw, list) else ()
    return EfoTerm(curie=short, name=label, iri=iri, synonyms=synonyms)


def write_terms(terms: dict[str, EfoTerm], *, fetched: str) -> Path:
    """The ONLY writer of ``efo/labels.json``, so it cannot drift from EFO by hand."""
    _DATA.parent.mkdir(parents=True, exist_ok=True)
    doc = {
        "source": OLS4_TERMS,
        "fetched": fetched,
        "terms": {
            t.curie: {"name": t.name, "iri": t.iri, "synonyms": list(t.synonyms)}
            for t in sorted(terms.values(), key=lambda x: x.curie)
        },
    }
    _DATA.write_text(json.dumps(doc, indent=1, sort_keys=True, ensure_ascii=False) + "\n")
    load_terms.cache_clear()
    return _DATA


__all__ = [
    "OLS4_TERMS",
    "EFO_IRI_BASE",
    "EfoTerm",
    "TermUnknown",
    "load_terms",
    "has_term",
    "term",
    "iri_for",
    "parse_ols4_term",
    "write_terms",
]
