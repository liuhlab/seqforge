"""What a study's reader reads from liulab-data's stores, and where a quote is found in it.

A study's **sources** are the records of the Series its bounds file keeps, the sequenced samples of
those Series, and the bounds' papers: each paper's abstract, and its full text where the paper
store holds one. A sample that is not sequenced, such as a microarray sample, is left out, and no
supplementary file of GEO or of a paper is read. :func:`read_sources` reads them as plain data,
:func:`records_text` and :func:`papers_text` render what a model reads, and :func:`find_quote`
says where a quote is: in its record's field character for character, or in its paper once each
run of white space is one space. The text of a record or a paper is only ever passed on: the repo
keeps identifiers and short quotes.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any
from xml.etree import ElementTree

#: The Series fields a model reads, in this order.
SERIES_FIELDS = (
    "Series_title",
    "Series_type",
    "Series_summary",
    "Series_overall_design",
    "Series_relation",
)
#: The sample fields a model reads, in this order.
SAMPLE_FIELDS = (
    "Sample_title",
    "Sample_source_name_ch1",
    "Sample_organism_ch1",
    "Sample_characteristics_ch1",
    "Sample_molecule_ch1",
    "Sample_description",
    "Sample_library_strategy",
    "Sample_library_source",
    "Sample_library_selection",
    "Sample_instrument_model",
    "Sample_treatment_protocol_ch1",
    "Sample_growth_protocol_ch1",
    "Sample_extract_protocol_ch1",
)
#: Fields that are read but never shown: code reads them itself.
_CODE_FIELDS = ("Sample_type", "Sample_taxid_ch1")
#: Fields that are shown once for the whole study when every sample holds the same lines.
_ONCE_FIELDS = (
    "Sample_organism_ch1",
    "Sample_molecule_ch1",
    "Sample_library_source",
    "Sample_library_selection",
    "Sample_instrument_model",
    "Sample_treatment_protocol_ch1",
    "Sample_growth_protocol_ch1",
    "Sample_extract_protocol_ch1",
)
#: The word a passage's ``record`` takes when every sample record holds the quote.
EACH_SAMPLE = "each_sample"
#: A GEO record's value is cut here in what a model reads; a protocol can run for pages.
_VALUE_CAP = 1500
#: The most characters of one paper's full text a model reads.
FULL_TEXT_CAP = 150_000
#: The one ``Sample_type`` that marks a sequenced sample.
SEQUENCED = "SRA"


def _values(record: Any, name: str) -> list[str]:
    return [str(value) for value in record.field(name) if str(value).strip()]


def read_sources(store: Any, papers: Any | None, bounds: Mapping[str, Any]) -> dict[str, Any]:
    """The sources of the study ``bounds`` draws, as plain data.

    ``store`` is liulab-data's record store and ``papers`` its paper reader, or ``None`` when no
    paper store is set up. A Series the store does not hold is kept, marked not held; a sample the
    store does not hold, or one that is not sequenced, is counted under ``left_out``.
    """
    wanted = [str(claim["id"]) for claim in bounds["series"]]
    opened = store.open_many(wanted)
    series: list[dict[str, Any]] = []
    sample_ids: list[str] = []
    for gse in wanted:
        record = opened.get(gse)
        if record is None or isinstance(record, Exception):
            series.append({"id": gse, "held": False})
            continue
        series.append(
            {"id": gse, "held": True, "fields": {f: _values(record, f) for f in SERIES_FIELDS}}
        )
        for link in record.links:
            if link.relation == "sample" and link.other not in sample_ids:
                sample_ids.append(link.other)
    sample_ids.sort(key=lambda s: int(s[3:]) if s[3:].isdigit() else 0)
    records = store.open_many(sample_ids) if sample_ids else {}
    samples, left_out = [], []
    for gsm in sample_ids:
        record = records.get(gsm)
        if record is None or isinstance(record, Exception):
            left_out.append({"id": gsm, "why": "the store does not hold it"})
            continue
        fields = {f: _values(record, f) for f in (*SAMPLE_FIELDS, *_CODE_FIELDS)}
        kind = (fields["Sample_type"] or ["?"])[0]
        if kind != SEQUENCED:
            left_out.append({"id": gsm, "why": f"Sample_type {kind}: not sequenced"})
            continue
        samples.append({"id": gsm, "fields": fields})
    pmids = [str(p).split(":")[-1] for p in bounds.get("papers") or []]
    details = papers.papers(pmids) if papers is not None and pmids else {}
    return {
        "id": str(bounds["study"]),
        "series": series,
        "samples": samples,
        "left_out": left_out,
        "papers": [_paper(pmid, details.get(pmid)) for pmid in pmids],
    }


def _paper(pmid: str, paper: Any | None) -> dict[str, Any]:
    """One paper's title, abstract and full text: the article XML where held, else the PDF."""
    info = getattr(paper, "info", None)
    row: dict[str, Any] = {
        "id": f"pmid:{pmid}",
        "title": getattr(info, "title", None),
        "abstract": getattr(info, "abstract", None),
        "full_text": None,
    }
    held = {f.kind: f for f in getattr(paper, "files", ()) if f.path is not None}
    if "xml" in held:
        with held["xml"].open() as fh:
            row["full_text"] = xml_sections(fh.read()) or None
    if row["full_text"] is None and "pdf" in held:
        with held["pdf"].open() as fh:
            text = _pdf_text(fh.read())
        row["full_text"] = [("Full text", text)] if text.strip() else None
    return row


def xml_sections(xml: bytes) -> list[tuple[str, str]]:
    """An article XML's body as ``(section, paragraph)`` pairs, markup removed.

    A section is its headings joined by ``" > "``. Back matter and the reference list are left out:
    only the body is read.
    """
    root = ElementTree.fromstring(xml)
    for element in root.iter():
        if isinstance(element.tag, str) and "}" in element.tag:
            element.tag = element.tag.split("}", 1)[1]
    body = root.find(".//body")
    out: list[tuple[str, str]] = []

    def walk(node: ElementTree.Element, trail: list[str]) -> None:
        title = node.find("title")
        here = trail + ([_flat(title)] if title is not None else [])
        for child in node:
            if child.tag == "sec":
                walk(child, here)
            elif child.tag in ("p", "fig", "table-wrap", "list", "disp-quote") and (
                text := _flat(child)
            ):
                out.append((" > ".join(here) or "Body", text))

    if body is not None:
        walk(body, [])
    return out


def _flat(element: ElementTree.Element) -> str:
    return " ".join("".join(element.itertext()).split())


def _pdf_text(data: bytes) -> str:
    """A PDF's text, the reference list cut off where its heading is in the second half."""
    import pymupdf

    with pymupdf.open(stream=data, filetype="pdf") as doc:
        text = "\n".join(page.get_text() for page in doc)
    heads = list(re.finditer(r"\n\s*(References|REFERENCES|Literature Cited)\s*\n", text))
    if heads and heads[-1].start() > len(text) / 2:
        text = text[: heads[-1].start()]
    return text


def read_full_text(sources: Mapping[str, Any]) -> bool:
    """Whether any of the study's papers is read in full."""
    return any(p["full_text"] for p in sources["papers"])


def library_strategies(sources: Mapping[str, Any]) -> set[str]:
    """The library strategies the study's sequenced sample records name."""
    return {v for s in sources["samples"] for v in s["fields"]["Sample_library_strategy"]}


def _once(samples: list[dict[str, Any]]) -> dict[str, list[str]]:
    """The fields every sample holds with the same lines, when there are two samples or more."""
    if len(samples) < 2:
        return {}
    out = {}
    for name in _ONCE_FIELDS:
        lines = {tuple(s["fields"][name]) for s in samples}
        if len(lines) == 1 and (only := next(iter(lines))):
            out[name] = list(only)
    return out


def records_text(sources: Mapping[str, Any]) -> str:
    """The records as a model reads them: each line ``FIELD: value``, so a quote names its field.

    Lines that every sample holds are shown once, under ``EACH_SAMPLE``.
    """
    out: list[str] = []
    for series in sources["series"]:
        out.append(f"=== GEO Series {series['id']} ===")
        if not series["held"]:
            out.append("(its record is not available)")
            continue
        out += [f"{name}: {v}" for name, values in series["fields"].items() for v in values]
    samples = sources["samples"]
    once = _once(samples)
    out.append(f"\n=== Sequenced samples: {len(samples)} ===")
    if once:
        out.append(f"{EACH_SAMPLE} (every sample record holds these lines):")
        out += [f"  {name}: {v[:_VALUE_CAP]}" for name, values in once.items() for v in values]
    for sample in samples:
        out.append(f"--- {sample['id']} ---")
        out += [
            f"{name}: {v[:_VALUE_CAP]}"
            for name in SAMPLE_FIELDS
            if name not in once
            for v in sample["fields"][name]
        ]
    if sources["left_out"]:
        out.append(f"(left out, not sequenced or not held: {len(sources['left_out'])} samples)")
    return "\n".join(out)


def sample_text(sources: Mapping[str, Any], sample: Mapping[str, Any]) -> str:
    """One sample's record, and the title and design of the study's Series."""
    out = [f"`sample` is {sample['id']}."]
    out += [f"{name}: {v[:700]}" for name in SAMPLE_FIELDS for v in sample["fields"][name]]
    for series in sources["series"]:
        if series["held"]:
            for name in ("Series_title", "Series_overall_design"):
                out += [f"{name}: {v[:_VALUE_CAP]}" for v in series["fields"][name]]
    return "\n".join(out)


def papers_text(sources: Mapping[str, Any], *, full: bool) -> str:
    """Each paper's title and abstract, and its full text when ``full`` and the store holds it."""
    out: list[str] = []
    for paper in sources["papers"]:
        out.append(f"=== Paper {paper['id']} ===")
        out.append(f"title: {paper['title'] or '(not available)'}")
        if paper["abstract"]:
            out.append(f"abstract: {paper['abstract']}")
        if full and paper["full_text"]:
            out.append("Full text, each paragraph headed by its section in square brackets:")
            size = 0
            for section, text in paper["full_text"]:
                piece = f"[{section}] {text}"
                if size + len(piece) > FULL_TEXT_CAP:
                    out.append("[... the full text is cut here ...]")
                    break
                out.append(piece)
                size += len(piece)
    if not sources["papers"]:
        out.append("No paper belongs to this study.")
    return "\n".join(out)


@dataclass(frozen=True)
class Found:
    """Where a quote was found: a passage as the reading file writes it."""

    passage: dict[str, Any]


def _spaced(text: str) -> str:
    return " ".join(text.split())


def find_quote(quote: Mapping[str, Any] | None, sources: Mapping[str, Any]) -> Found | None:
    """Where ``quote`` is in its source, or ``None`` when it is not there.

    ``quote`` names its ``source`` (a record id, ``each_sample`` or ``pmid:<n>``), the ``field`` of
    a record and the ``text``. A record quote is found when the named field holds it character for
    character: in the named record, or in every sample for ``each_sample``. The passage then lists
    every sample record that holds it in that field. A paper quote is found anywhere in the named
    paper once each run of white space is one space, and its section is where it was found.
    """
    if not quote:
        return None
    text, source = str(quote.get("text") or "").strip(), str(quote.get("source") or "").strip()
    field = str(quote.get("field") or "").strip()
    if not text:
        return None
    if source.startswith("pmid:"):
        return _in_paper(text, source, sources)
    samples = {s["id"]: s["fields"] for s in sources["samples"]}
    holders = [g for g, fields in samples.items() if any(text in v for v in fields.get(field, []))]
    if source == EACH_SAMPLE:
        if samples and len(holders) == len(samples):
            return Found({"record": EACH_SAMPLE, "field": field, "quote": text})
        return None
    if source in samples:
        if source not in holders:
            return None
        if len(holders) == len(samples) and len(samples) > 1:
            return Found({"record": EACH_SAMPLE, "field": field, "quote": text})
        record: str | list[str] = holders if len(holders) > 1 else source
        return Found({"record": record, "field": field, "quote": text})
    for series in sources["series"]:
        holds = series["held"] and any(text in v for v in series["fields"].get(field, []))
        if series["id"] == source and holds:
            return Found({"record": source, "field": field, "quote": text})
    return None


def _in_paper(text: str, source: str, sources: Mapping[str, Any]) -> Found | None:
    paper = next((p for p in sources["papers"] if p["id"] == source), None)
    if paper is None:
        return None
    sections: list[tuple[str, str]] = [
        (name, paper[name]) for name in ("title", "abstract") if paper[name]
    ]
    sections += paper["full_text"] or []
    wanted = _spaced(text)
    for section, body in sections:
        if wanted in _spaced(body):
            return Found({"paper": source, "section": section, "quote": wanted})
    return None


def taxonomy(sample: Mapping[str, Any]) -> tuple[str | None, str | None]:
    """A sample's species as its record writes it, and the record's NCBI Taxonomy id."""
    fields = sample["fields"]
    name = (fields["Sample_organism_ch1"] or [None])[0]
    taxid = (fields["Sample_taxid_ch1"] or [None])[0]
    return name, taxid


def holders_of(sources: Mapping[str, Any], field: str, text: str) -> list[str]:
    """The sample records whose ``field`` holds ``text`` whole, as one of its values."""
    return [s["id"] for s in sources["samples"] if text in s["fields"].get(field, [])]


def sample_ids(sources: Mapping[str, Any]) -> list[str]:
    """The study's sequenced samples, in accession order."""
    return [s["id"] for s in sources["samples"]]


def joined(values: Iterable[str]) -> str:
    """Values as one line of text, for a model's question."""
    return "; ".join(values)
