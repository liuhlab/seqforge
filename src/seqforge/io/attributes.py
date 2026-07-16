"""NCBI's harmonized BioSample attribute vocabulary — the key space for a sample fact.

A sample fact needs a key, and the key cannot be ours. ``condition`` was — we invented it, no archive
uses it, and it is exactly the slot a language model filed worm husbandry into on the pilot, because
a field named "condition" will accept anything you can call a condition. NCBI already solved this:
960 curated attribute names, each with a definition, a display name, and the synonyms real submitters
actually type. This module is that list, and nothing else decides what a sample attribute may be
called.

**Ask a subset, enforce the whole list.** :mod:`seqforge.harvest.fields` picks the handful of
attributes worth spending prompt tokens on; ``SampleGroup`` validates every key against all 960. The
two are deliberately different sizes — asking for 960 fields would be absurd, and permitting only the
handful we ask for would throw away an attribute a record genuinely declares.

*A correction worth recording, because the obvious design is wrong.* Every BioSample record declares
a **package** (the pilot's is ``Model.organism.animal.1.0``), and each of NCBI's attributes lists the
packages it belongs to — so it looks as though a record's package narrows the key space from 960 to a
couple of dozen. It does not. The pilot's record declares ``Model.organism.animal.1.0`` and carries
``strain`` and ``dev_stage``; NCBI lists ``strain`` only under OneHealthEnteric/PHA4GE.wwsurv and
``dev_stage`` only under Human.1.0/Invertebrate.1.0. Narrowing by package would have dropped
``strain`` — the one attribute that separates this dataset's two conditions. Packages are therefore
recorded and never enforced.

The data ships (``biosample/attributes.json``, 239 kB of plain diffable JSON) rather than being
fetched, because enforcement must work offline: a validator that needs the network is a validator
that fails open on a compute node. It is **generated** by ``seqforge io attributes refresh``, records
its own source URL and fetch date, and is never hand-edited.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from functools import cache
from pathlib import Path

#: NCBI's own list, in its own format. The one place this vocabulary comes from.
ATTRIBUTES_URL = "https://www.ncbi.nlm.nih.gov/biosample/docs/attributes/?format=xml"

_DATA = Path(__file__).parent / "biosample" / "attributes.json"


@dataclass(frozen=True)
class BioSampleAttribute:
    """One of NCBI's harmonized attributes: the name, what it means, and what people call it."""

    #: The harmonized name — the key a ``SampleGroup`` attribute is stored under.
    name: str
    #: NCBI's human-facing label, e.g. ``dev_stage`` -> "development stage".
    display: str
    #: NCBI's definition. Handed to the model verbatim when we ask for this attribute, so the prompt
    #: never carries our paraphrase of someone else's vocabulary. Some are empty in NCBI's own list.
    description: str
    #: What submitters write instead, per NCBI. The only basis on which a raw tag is harmonized.
    synonyms: tuple[str, ...] = ()


class VocabularyMissing(RuntimeError):
    """The shipped vocabulary is absent or unreadable. Never a silently empty key space."""


def _key(raw: str) -> str:
    """Fold a submitter's tag the way a lookup needs: case, spaces and underscores are noise."""
    return raw.strip().lower().replace(" ", "_").replace("-", "_")


@cache
def load_attributes() -> dict[str, BioSampleAttribute]:
    """Every harmonized attribute, by name. Cached — it is a read-only 239 kB file."""
    if not _DATA.is_file():
        raise VocabularyMissing(
            f"the NCBI BioSample attribute vocabulary is missing from the package ({_DATA}). "
            f"Regenerate it with `seqforge io attributes refresh`."
        )
    doc = json.loads(_DATA.read_text(encoding="utf-8"))
    return {
        name: BioSampleAttribute(
            name=name,
            display=str(meta.get("display", "")),
            description=str(meta.get("description", "")),
            synonyms=tuple(meta.get("synonyms", ())),
        )
        for name, meta in sorted(doc["attributes"].items())
    }


@cache
def _synonym_index() -> dict[str, str]:
    """Folded tag -> harmonized name, built from NCBI's own synonym lists.

    A synonym claimed by two attributes is dropped rather than arbitrated: harmonizing it either way
    would be a guess, and an unharmonized attribute is recorded honestly as itself.
    """
    seen: dict[str, set[str]] = {}
    for attr in load_attributes().values():
        for raw in (attr.name, attr.display, *attr.synonyms):
            if raw:
                seen.setdefault(_key(raw), set()).add(attr.name)
    # a name always wins over another attribute's synonym for the same folded key
    for attr in load_attributes().values():
        seen[_key(attr.name)] = {attr.name}
    return {k: next(iter(v)) for k, v in seen.items() if len(v) == 1}


def source_provenance() -> dict[str, str]:
    """Where the shipped vocabulary came from and when. Recorded in the file, not in a comment."""
    doc = json.loads(_DATA.read_text(encoding="utf-8"))
    return {"source": str(doc["source"]), "fetched": str(doc["fetched"]), "n": str(doc["n"])}


def is_attribute(name: str) -> bool:
    """Is ``name`` one of NCBI's harmonized attribute names, exactly? Fail-closed."""
    return name in load_attributes()


def get_attribute(name: str) -> BioSampleAttribute:
    """The attribute called ``name``. Raises ``KeyError`` — callers should have asked first."""
    return load_attributes()[name]


def harmonize(raw: str) -> str | None:
    """A submitter's raw tag -> NCBI's harmonized name, or ``None`` if NCBI does not know it.

    ``None`` is a real answer and the common one for a submitter-invented tag. It means "record this
    as an unharmonized attribute", never "drop it" and never "guess the closest name".
    """
    return _synonym_index().get(_key(raw))


def parse_ncbi_attributes_xml(xml: str) -> dict[str, BioSampleAttribute]:
    """Parse NCBI's attribute-list XML. The only reader of that format; used by ``refresh``."""
    from xml.etree import ElementTree

    root = ElementTree.fromstring(xml)
    out: dict[str, BioSampleAttribute] = {}
    for node in root.findall(".//Attribute"):
        name = (node.findtext("HarmonizedName") or "").strip()
        if not name:
            continue
        out[name] = BioSampleAttribute(
            name=name,
            display=" ".join((node.findtext("Name") or "").split()),
            description=" ".join((node.findtext("Description") or "").split()),
            synonyms=tuple(
                sorted(
                    {" ".join((s.text or "").split()) for s in node.findall("Synonym") if s.text}
                )
            ),
        )
    if not out:
        raise VocabularyMissing(
            "NCBI's attribute list parsed to zero attributes — refusing to ship"
        )
    return out


def write_attributes(attrs: dict[str, BioSampleAttribute], *, fetched: str) -> Path:
    """Write the shipped vocabulary. The ONLY writer, so the file cannot drift from NCBI by hand."""
    _DATA.parent.mkdir(parents=True, exist_ok=True)
    doc = {
        "source": ATTRIBUTES_URL,
        "fetched": fetched,
        "n": len(attrs),
        "attributes": {
            a.name: {
                "display": a.display,
                "description": a.description,
                "synonyms": list(a.synonyms),
            }
            for a in sorted(attrs.values(), key=lambda x: x.name)
        },
    }
    _DATA.write_text(json.dumps(doc, indent=1, sort_keys=True, ensure_ascii=False) + "\n")
    load_attributes.cache_clear()
    _synonym_index.cache_clear()
    return _DATA


__all__ = [
    "ATTRIBUTES_URL",
    "BioSampleAttribute",
    "VocabularyMissing",
    "load_attributes",
    "source_provenance",
    "is_attribute",
    "get_attribute",
    "harmonize",
    "parse_ncbi_attributes_xml",
    "write_attributes",
]
