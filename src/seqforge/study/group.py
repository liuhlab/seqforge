"""What a study's bounding reads from liulab-data's stores: its linked Series and their papers.

A study starts from one GEO Series. Its **linked group** is that Series, every Series one link away
from it, and every paper any of them links. :func:`linked_series` gathers the group's Series,
:func:`read_group` reads the group as plain data, and :func:`group_text` renders what the model is
shown: the records and each paper's abstract. Record and paper text is only ever passed on: the
repo keeps identifiers.
"""

from __future__ import annotations

import re
from collections import defaultdict
from collections.abc import Iterable, Mapping
from typing import Any

#: The Series fields the model sees, in this order, each with its label.
_SERIES_FIELDS = (
    ("Series_title", "Title"),
    ("Series_type", "Type"),
    ("Series_status", "Status"),
    ("Series_submission_date", "Submitted"),
    ("Series_summary", "Summary"),
    ("Series_overall_design", "Overall design"),
    ("Series_citation", "Citation"),
    ("Series_relation", "Relation"),
)
#: A GEO Series accession.
SERIES = re.compile(r"GSE\d+")
#: A record's link to a paper, by PubMed id or DOI.
_PAPER = ("PubMed", "DOI")


def paper_id(pmid: object) -> str:
    """A paper as the study files write it: ``pmid:<number>``."""
    return f"pmid:{str(pmid).split(':')[-1]}"


def _fields(record: Any, name: str) -> list[str]:
    return [str(value) for value in record.field(name) if str(value).strip()]


def _far_ends(record: Any, relation: str) -> list[str]:
    return sorted({link.other for link in record.links if link.relation == relation})


def _papers(links: Iterable[Any]) -> list[str]:
    """The papers ``links`` reach, as ``PMID:<n>`` or ``DOI:<doi>``, a placeholder id dropped."""
    from labdata.papers.root import is_placeholder, paper_key

    sources: dict[str, set[str]] = defaultdict(set)
    for link in links:
        if link.relation in _PAPER:
            sources[link.other].add(link.source)
    return sorted(key for key, said in sources.items() if not is_placeholder(paper_key(key), said))


def _ends(store: Any, accessions: Iterable[str], *relations: str) -> set[str]:
    """The far ends of ``relations`` from each of ``accessions``, in one read of the store."""
    wanted = sorted(set(accessions))
    found = store.links(wanted) if wanted else {}
    return {link.other for links in found.values() for link in links if link.relation in relations}


def linked_series(store: Any, start: str) -> list[str]:
    """``start`` and every Series one link away from it, in accession order.

    Four kinds of link tie two Series: a SuperSeries and its sub-Series, a shared BioProject, two
    BioProjects one umbrella step apart, and a shared paper. A paper that only a BioProject links
    ties nothing, and neither does a reanalysis. A Series the store does not hold is kept.
    """
    own = store.links([start])[start]
    supers = {link.other for link in own if link.relation == "SubSeries of"}
    projects = {link.other for link in own if link.relation == "BioProject"}
    umbrellas = _ends(store, projects, "under umbrella")
    projects |= umbrellas | _ends(store, projects | umbrellas, "umbrella of")
    found = {start} | supers | {link.other for link in own if link.relation == "SuperSeries of"}
    found |= _ends(store, supers, "SuperSeries of")
    found |= _ends(store, projects, "BioProject of")
    found |= _ends(store, _papers(own), "PubMed of", "DOI of")
    return sorted((s for s in found if SERIES.fullmatch(s)), key=lambda s: int(s[3:]))


def read_group(store: Any, papers: Any | None, entry: Mapping[str, Any]) -> dict[str, Any]:
    """One study's linked group: its Series with their samples, and the papers they link.

    ``entry`` names the study's ``id`` and its ``linked_series``. ``store`` is liulab-data's record
    store and ``papers`` its paper reader, or ``None`` when no paper store is configured. A linked
    Series the store does not hold is kept, marked not held. A paper lists every Series it links,
    those outside the group included, since a paper shared with another Series is the main sign of
    a re-cite.
    """
    wanted = [str(gse) for gse in entry["linked_series"]]
    opened = store.open_many(wanted)
    held = {gse: rec for gse, rec in opened.items() if not isinstance(rec, Exception)}
    sample_ids = {gse: _far_ends(rec, "sample") for gse, rec in held.items()}
    samples = store.open_many(sorted({s for ids in sample_ids.values() for s in ids}))

    series: list[dict[str, Any]] = []
    for gse in wanted:
        record = held.get(gse)
        if record is None:
            series.append({"id": gse, "held": False})
            continue
        series.append(
            {
                "id": gse,
                "held": True,
                "fields": {label: _fields(record, name) for name, label in _SERIES_FIELDS},
                "superseries_of": _far_ends(record, "SuperSeries of"),
                "pmids": [p[5:] for p in _papers(record.links) if p.startswith("PMID:")],
                "samples": [
                    _sample(samples[s]) for s in sample_ids[gse] if not _is_error(samples[s])
                ],
            }
        )

    pmids = list(dict.fromkeys(p for s in series for p in s.get("pmids", [])))
    details = papers.papers(pmids) if papers is not None and pmids else {}
    links = store.links([f"PMID:{p}" for p in pmids], "PubMed of") if pmids else {}
    paper_rows = []
    for pmid in pmids:
        paper = details.get(pmid)
        info = getattr(paper, "info", None)
        paper_rows.append(
            {
                "id": paper_id(pmid),
                "pmid": pmid,
                "title": getattr(info, "title", None),
                "journal": getattr(info, "journal", None),
                "published": str(info.published) if info and info.published else None,
                "types": list(getattr(info, "publication_type", ()) or ()),
                "abstract": getattr(info, "abstract", None),
                "series": sorted(
                    link.other
                    for link in links.get(f"PMID:{pmid}", [])
                    if SERIES.fullmatch(link.other)
                ),
            }
        )
    return {"id": str(entry["id"]), "series": series, "papers": paper_rows}


def _is_error(record: object) -> bool:
    return isinstance(record, Exception)


def _sample(record: Any) -> dict[str, Any]:
    return {
        "id": record.accession,
        "title": " ".join(_fields(record, "Sample_title")),
        "source": " ".join(_fields(record, "Sample_source_name_ch1")),
        "characteristics": _fields(record, "Sample_characteristics_ch1"),
        "library": " / ".join(
            _fields(record, "Sample_library_strategy") + _fields(record, "Sample_library_source")
        ),
        "series": _fields(record, "Sample_series_id"),
    }


def group_text(group: Mapping[str, Any]) -> str:
    """The records and paper abstracts of one linked group, as the text the model reads.

    A paper with no abstract is shown by its title, journal and date only.
    """
    out = [f"STUDY PICKED BY: {group['id']}", ""]
    for s in group["series"]:
        out.append(f"=== GEO Series {s['id']} ===")
        if not s["held"]:
            out += ["(linked, but its record is not available)", ""]
            continue
        for label, values in s["fields"].items():
            out += [f"{label}: {value}" for value in values]
        out.append(
            f"Papers linked by this record: {', '.join(map(paper_id, s['pmids'])) or 'none'}"
        )
        out.append(f"Samples ({len(s['samples'])}):")
        for sample in s["samples"]:
            also = [g for g in sample["series"] if g != s["id"]]
            parts = [sample["title"], sample["source"], "; ".join(sample["characteristics"])]
            parts += [sample["library"], f"also in {', '.join(also)}" if also else ""]
            out.append(f"- {sample['id']}: " + " | ".join(p for p in parts if p))
        out.append("")
    for p in group["papers"]:
        out.append(f"=== Paper {p['id']} ===")
        out.append(f"Title: {p['title'] or '(not available)'}")
        out.append(f"Journal: {p['journal'] or '?'}; published {p['published'] or '?'}")
        if p["types"]:
            out.append(f"Publication type: {', '.join(p['types'])}")
        out.append(f"GEO Series that link this paper: {', '.join(p['series']) or 'none'}")
        if p["abstract"]:
            out.append(f"Abstract: {p['abstract']}")
        out.append("")
    if not group["papers"]:
        out.append("No paper is linked by any of these records.")
    return "\n".join(out).rstrip() + "\n"


def paper_reader() -> Any | None:
    """liulab-data's paper reader, from ``LABDATA_PAPERS``; ``None`` when no paper store is set up.

    So a study with no paper can still be read.
    """
    from labdata.exceptions import StoreNotConfiguredError
    from labdata.papers import open_paper_store

    try:
        return open_paper_store().reader
    except StoreNotConfiguredError:
        return None
