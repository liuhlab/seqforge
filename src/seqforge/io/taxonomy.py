"""Organism name -> NCBI taxid, with a round-trip verifier. **No model is involved, deliberately.**

This is the one gap in "the organism must come from the paper" that survived every audit, and the
shape of it is worth stating because it looks like a job for a language model and is not:

    harvest extracts   experiment.organism = "Caenorhabditis elegans"   (a str, with a verified span)
    fill_manifest wants experiment.organism_taxid = 6239                (an int)
    and nothing joined them, so the taxid had to be retyped by hand.

The model already does its job here — it *finds* the organism in the prose, span-verified. What was
missing is a converter, and a converter is a lookup table. A model asked for the taxid directly would
be asked to *recall* one, and a model confusing 6239 (C. elegans) with 6238 (C. briggsae) produces a
number that is well-formed, plausible, and wrong in a way nothing downstream can see: a worm dataset
aligned to a different worm maps at a rate that looks merely mediocre. That is the failure class this
project exists to prevent, so: **the LLM finds, code resolves.**

**The verifier is a round trip, and it is what makes this safe.** Resolve the name to a taxid, then
fetch that taxid back and confirm it answers to the name we started from (scientific name, synonym, or
common name). A wrong lookup does not survive it. Note this would make even a *model-proposed* taxid
safe — the criterion is "cheaply verifiable", not "hard to hardcode" — but a table is simpler and free,
so there is no reason to reach for one.

Two things this module refuses to do:

- **Guess a rank.** `Saccharomyces cerevisiae` resolves to 4932 (the species). The sacCer3 fixtures use
  559292 (strain S288C). Both are correct answers to different questions, and NCBI's search returns the
  species — so when a caller wants the strain, they say so. Silently promoting or demoting a rank is
  the kind of helpfulness that ends up in a corpus.
- **Reach the network without saying so.** The seed table below covers what we ship fixtures for. A
  name outside it needs one E-utilities call, and offline that is a refusal with an actionable remedy,
  never a guess.
"""

from __future__ import annotations

import json
import re
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path

from ..workspace import state_dir

_EUTILS = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils"

#: Names we resolve without a network call. **Every entry was verified against NCBI E-utilities on
#: 2026-07-15 by the round trip this module implements** — none is remembered.
#:
#: This is a cache, not a curation: it exists so the common path is offline and instant, not to decide
#: which organisms are worth supporting (that would be a policy hiding in a dict). Anything else does
#: one lookup. Keys are lowercased.
_SEED: dict[str, int] = {
    "caenorhabditis elegans": 6239,
    "c. elegans": 6239,
    "homo sapiens": 9606,
    "human": 9606,
    "mus musculus": 10090,
    "mouse": 10090,
    "saccharomyces cerevisiae": 4932,
    "saccharomyces cerevisiae s288c": 559292,
    "danio rerio": 7955,
    "drosophila melanogaster": 7227,
}


class TaxonomyUnavailable(RuntimeError):
    """A name could not be resolved (unknown, offline, or the round trip failed)."""


@dataclass(frozen=True)
class Taxon:
    """A resolved organism: the taxid, plus what NCBI says it is."""

    taxid: int
    scientific_name: str
    rank: str = ""
    names: tuple[str, ...] = ()

    def answers_to(self, name: str) -> bool:
        """Does this taxon go by ``name``? The round-trip check's actual question."""
        wanted = _normalize(name)
        return wanted in {_normalize(n) for n in (self.scientific_name, *self.names)}


def _normalize(name: str) -> str:
    """Casefold and collapse whitespace. Nothing cleverer: this is a key, not a parser."""
    return re.sub(r"\s+", " ", name).strip().lower()


def _get(url: str, *, timeout: float) -> str:
    with urllib.request.urlopen(url, timeout=timeout) as fh:  # noqa: S310 - a pinned NCBI host
        return str(fh.read().decode())


def fetch_taxon(taxid: int, *, timeout: float = 20.0) -> Taxon:
    """What NCBI says taxid ``taxid`` is. The second half of the round trip."""
    xml = _get(f"{_EUTILS}/efetch.fcgi?db=taxonomy&id={taxid}&retmode=xml", timeout=timeout)
    sci = re.search(r"<ScientificName>(.*?)</ScientificName>", xml)
    if not sci:
        raise TaxonomyUnavailable(f"NCBI returned no scientific name for taxid {taxid}")
    rank = re.search(r"<Rank>(.*?)</Rank>", xml)
    names = tuple(
        re.findall(r"<Synonym>(.*?)</Synonym>", xml)
        + re.findall(r"<GenbankCommonName>(.*?)</GenbankCommonName>", xml)
        + re.findall(r"<CommonName>(.*?)</CommonName>", xml)
    )
    return Taxon(
        taxid=int(taxid),
        scientific_name=sci.group(1),
        rank=rank.group(1) if rank else "",
        names=names,
    )


def resolve(name: str, *, offline: bool = False, timeout: float = 20.0, verify: bool = True) -> int:
    """``"Caenorhabditis elegans"`` -> ``6239``. Raises rather than guessing.

    ``verify`` runs the round trip: the taxid is fetched back and must answer to ``name``. It is on by
    default and should stay on — a lookup that returns the wrong taxid is otherwise undetectable, and
    the whole reason this is code instead of a model is that we can check it.
    """
    key = _normalize(name)
    if key in _SEED:
        return _SEED[key]
    if offline:
        raise TaxonomyUnavailable(
            f"cannot resolve organism {name!r} offline. Pass the NCBI taxid directly "
            f"(`--organism <taxid>`), or run with network access."
        )
    term = urllib.parse.quote(name)
    try:
        payload = json.loads(
            _get(f"{_EUTILS}/esearch.fcgi?db=taxonomy&term={term}&retmode=json", timeout=timeout)
        )
        ids = payload["esearchresult"]["idlist"]
    except Exception as exc:  # network, JSON, or shape - all mean "we do not know"
        raise TaxonomyUnavailable(f"NCBI taxonomy lookup for {name!r} failed: {exc}") from exc
    if not ids:
        raise TaxonomyUnavailable(
            f"NCBI taxonomy has no match for organism {name!r}. Check the spelling, or pass the "
            f"taxid directly with `--organism <taxid>`."
        )
    if len(ids) > 1:
        raise TaxonomyUnavailable(
            f"organism {name!r} is ambiguous: NCBI returns taxids {ids}. Pass the one you mean with "
            f"`--organism <taxid>` — picking for you is exactly the guess this refuses to make."
        )
    taxid = int(ids[0])
    if verify:
        taxon = fetch_taxon(taxid, timeout=timeout)
        if not taxon.answers_to(name):
            raise TaxonomyUnavailable(
                f"round trip failed: {name!r} resolved to taxid {taxid}, but NCBI says that is "
                f"{taxon.scientific_name!r} (rank {taxon.rank}). Refusing — a wrong organism aligns "
                f"at a rate that merely looks mediocre, and nothing downstream would object."
            )
    return taxid


def seed_names() -> dict[str, int]:
    """The offline table, for tests and `io taxon list`."""
    return dict(_SEED)


#: Where a resolved lookup is cached under a workspace, so a second run is offline and instant.
def cache_path(workspace: str | Path) -> Path:
    return state_dir(workspace, "taxonomy.json")
