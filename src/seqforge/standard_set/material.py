"""What one study's drawing reads from liulab-data's stores: its linked Series and their papers.

A study's **linked group** is every Series the pick lists for it and every paper any of them links.
:func:`read_group` reads that group as plain data, :func:`drafter_text` renders the part the
drafting model sees (the records and each paper's abstract), and :func:`stage` writes everything
the judging model may open, the full paper text included, into a folder outside the repo. Record
and paper text is only ever passed on or written there: the repo keeps identifiers.
"""

from __future__ import annotations

import re
import textwrap
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any

import yaml

from . import PICK

#: The Series fields the drafting model sees, in this order, each with its label.
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
#: Field-name fragments left out of the full records the judging model may open: people's contact
#: details and file listings, which say nothing about what belongs to a study.
_NOT_STAGED = ("contact", "supplementary_file", "data_row_count")
_GSE = re.compile(r"GSE\d+")


def pick_entries(set_dir: Path) -> dict[str, dict[str, Any]]:
    """The picked studies by id, in the pick's order. Spares are not studies until one is swapped in."""
    pick = yaml.safe_load((set_dir / PICK).read_text())
    return {str(entry["id"]): entry for entry in pick["studies"]}


def paper_id(pmid: object) -> str:
    """A paper as the study files write it: ``pmid:<number>``."""
    return f"pmid:{str(pmid).split(':')[-1]}"


def _fields(record: Any, name: str) -> list[str]:
    return [str(value) for value in record.field(name) if str(value).strip()]


def _far_ends(record: Any, relation: str) -> list[str]:
    return sorted({link.other for link in record.links if link.relation == relation})


def read_group(store: Any, papers: Any | None, entry: Mapping[str, Any]) -> dict[str, Any]:
    """One study's linked group: its Series with their samples, and the papers they link.

    ``store`` is liulab-data's record store and ``papers`` its paper reader, or ``None`` when no
    paper store is configured. A linked Series the store does not hold is kept, marked not held. A
    paper lists every Series it links, those outside the group included, since a paper shared with
    another Series is the main sign of a re-cite.
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
                "record": record,
                "fields": {label: _fields(record, name) for name, label in _SERIES_FIELDS},
                "superseries_of": _far_ends(record, "SuperSeries of"),
                "bioprojects": _far_ends(record, "BioProject"),
                "pmids": [p.split(":")[-1] for p in _far_ends(record, "PubMed")],
                "samples": [
                    _sample(samples[s]) for s in sample_ids[gse] if not _is_error(samples[s])
                ],
            }
        )

    pmids = list(
        dict.fromkeys(
            [str(p) for p in entry.get("pmids") or []]
            + [p for s in series for p in s.get("pmids", [])]
        )
    )
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
                "paper": paper,
                "title": getattr(info, "title", None),
                "journal": getattr(info, "journal", None),
                "published": str(info.published) if info and info.published else None,
                "types": list(getattr(info, "publication_type", ()) or ()),
                "abstract": getattr(info, "abstract", None),
                "series": sorted(
                    link.other
                    for link in links.get(f"PMID:{pmid}", [])
                    if _GSE.fullmatch(link.other)
                ),
            }
        )
    supers = [s["id"] for s in series if s.get("superseries_of")]
    return {
        "id": str(entry["id"]),
        "series": series,
        "papers": paper_rows,
        "superseries": supers[0] if supers else None,
    }


def _is_error(record: object) -> bool:
    return isinstance(record, Exception)


def _sample(record: Any) -> dict[str, Any]:
    return {
        "id": record.accession,
        "record": record,
        "title": " ".join(_fields(record, "Sample_title")),
        "source": " ".join(_fields(record, "Sample_source_name_ch1")),
        "characteristics": _fields(record, "Sample_characteristics_ch1"),
        "library": " / ".join(
            _fields(record, "Sample_library_strategy") + _fields(record, "Sample_library_source")
        ),
        "series": _fields(record, "Sample_series_id"),
    }


def record_ids(group: Mapping[str, Any]) -> dict[str, Any]:
    """The group's records as the study file keeps them: identifiers only."""
    series = group["series"]
    return {
        "series": [s["id"] for s in series],
        "superseries": group["superseries"],
        "bioprojects": list(dict.fromkeys(b for s in series for b in s.get("bioprojects", []))),
        "pmids": [p["pmid"] for p in group["papers"]],
    }


def drafter_text(group: Mapping[str, Any]) -> str:
    """The records and paper abstracts of one linked group, as the text the drafting model reads."""
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
        out += [f"Abstract: {p['abstract'] or '(not available)'}", ""]
    if not group["papers"]:
        out.append("No paper is linked by any of these records.")
    return "\n".join(out).rstrip() + "\n"


def _record_text(record: Any) -> str:
    lines = [f"{record.type} {record.accession}"]
    lines += [
        f"{name}: {value}"
        for name, value in record.fields
        if not any(part in name for part in _NOT_STAGED)
    ]
    return "\n".join(lines) + "\n"


def _article_text(xml: bytes) -> str:
    """A paper's XML as plain text: tags dropped, one block after another, in short lines.

    The lines are short so that a search of the file shows the sentence it found: a paragraph on
    one line is cut off in a search result, and the judging model then learns nothing from the hit.
    """
    text = xml.decode("utf-8", errors="replace")
    text = re.sub(r"</(p|title|sec|caption|td|th|tr|abstract|ref)>", "\n", text)
    text = re.sub(r"<[^>]+>", " ", text)
    for entity, char in (("&lt;", "<"), ("&gt;", ">"), ("&amp;", "&"), ("&#x2013;", "-")):
        text = text.replace(entity, char)
    blocks = (re.sub(r"[ \t]+", " ", line).strip() for line in text.splitlines())
    return "\n".join(textwrap.fill(block, 110) for block in blocks if block) + "\n"


def stage(group: Mapping[str, Any], folder: Path) -> list[str]:
    """Write what the judging model may open into ``folder``, and return the files' relative paths.

    ``records/<GSE>.txt`` is a Series record and ``records/<GSE>-samples.txt`` its sample records,
    in full but for contact details and file listings. ``papers/pmid-<n>.txt`` is a paper's details
    and abstract, and ``papers/pmid-<n>-full.txt`` its full text where the paper store holds it.
    """
    written: dict[str, str] = {}
    for s in group["series"]:
        if not s["held"]:
            continue
        written[f"records/{s['id']}.txt"] = _record_text(s["record"])
        written[f"records/{s['id']}-samples.txt"] = "\n".join(
            _record_text(sample["record"]) for sample in s["samples"]
        )
    for p in group["papers"]:
        head = [f"Paper {p['id']}", f"Title: {p['title']}", f"Journal: {p['journal']}"]
        head += [f"Published: {p['published']}", f"Publication type: {', '.join(p['types'])}"]
        head += [f"GEO Series that link this paper: {', '.join(p['series'])}", ""]
        written[f"papers/pmid-{p['pmid']}.txt"] = "\n".join(head) + f"Abstract: {p['abstract']}\n"
        for file in _held(getattr(p["paper"], "files", ()), "xml"):
            with file.open() as handle:
                written[f"papers/pmid-{p['pmid']}-full.txt"] = _article_text(handle.read())
            break
    for name, text in written.items():
        path = folder / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
    return sorted(written)


def _held(files: Iterable[Any], kind: str) -> list[Any]:
    return [f for f in files if f.kind == kind and f.path is not None]


def open_sources() -> tuple[Any, Any | None]:
    """liulab-data's record store and paper reader, from ``LABDATA_STORE`` and ``LABDATA_PAPERS``.

    The paper reader is ``None`` when no paper store is configured, so a study with no paper can
    still be read. A record store that cannot be opened raises liulab-data's own error.
    """
    import labdata
    from labdata.exceptions import StoreNotConfiguredError
    from labdata.papers import open_paper_store

    store = labdata.open_store()
    try:
        return store, open_paper_store().reader
    except StoreNotConfiguredError:
        return store, None
