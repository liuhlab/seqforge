"""``io records`` — fetch what a public archive declares about a dataset, at all four levels.

This is a **transcriber**, not a resolver. It turns an accession into
:class:`~seqforge.models.records.ArchiveRecordSet` — project, sample, experiment, run — and stops.
It decides nothing, harmonizes only where NCBI itself harmonized, and never touches a byte of FASTQ.
What the records *mean* is :mod:`seqforge.resolve.records`'s job.

**Why this had to exist at all.** ``io/remote.py`` already asks ENA for 24 fields, and none of them
told us anything per-sample: ``experiment_title`` and ``sample_title`` are byte-identical across all
six runs of the pilot ("Model organism or animal sample from Caenorhabditis elegans"), the fields
that *do* discriminate (``sample_alias``, ``library_name``, ``run_alias``) were never requested, and
the BioSample attributes that carry ``strain``/``tissue``/``sex``/``dev_stage`` were fetched by zero
lines of code anywhere in the repo. "We already have this and throw it away" was the comfortable
assumption, and it was false.

**Three calls, and each earns its place:**

1. ``labdata.experiments_for`` expands *any* accession — GEO series, BioProject, study, run,
   BioSample — into the experiments underneath it, over NCBI Entrez. It replaced our own ENA/SOFT
   routing because its ``elink`` route traverses a GEO *SuperSeries* (which owns no runs of its own)
   and a BioProject umbrella transitively — the recursion our SOFT parser missed, which lost a whole
   SuperSeries dataset while reporting success.
2. NCBI's ``efetch db=sra`` returns one ``EXPERIMENT_PACKAGE`` per experiment, and a package is the
   whole hierarchy in one object: STUDY (title, abstract, centre), EXPERIMENT (the protocol prose),
   SAMPLE (alias + attributes), RUN (accession, alias, original filenames). Everything but one thing.
3. That one thing is ``harmonized_name``. The SRA package gives a sample attribute as the submitter
   typed it (``<TAG>dev_stage</TAG>``); ``efetch db=biosample`` gives NCBI's own harmonization of the
   same attribute (``harmonized_name="dev_stage"``). We want NCBI's, because the alternative is us
   guessing at someone else's vocabulary.

``efetch db=sra`` does **not** accept a study accession (``id=SRP502277`` -> "ID list is empty"),
which is why step 1 is not optional. It does accept a list of experiment accessions, so the six
packages arrive in one request.

Every fetch degrades rather than aborts: an archive that is down, rate-limited, or simply does not
have a level yields fewer records, and a dataset with no accession at all yields none. That is not a
fallback — a great deal of sequencing data has never seen an accession and never will.
"""

from __future__ import annotations

import os
import re
import time
from xml.etree import ElementTree

from ..models.records import ArchiveRecord, ArchiveRecordSet, FreeText, RecordAttribute
from .attributes import harmonize
from .remote import _MAX_RETRIES, RemoteError, _get, retry_delay

#: A ``LabdataError`` message that names a TRANSIENT NCBI failure (a 5xx, a 429/rate-limit, a timeout)
#: — worth a backoff-and-retry. Everything else (a malformed accession, a record with no SRA data) is
#: terminal and raised at once. eutils returns intermittent HTTP 500s under load, and the accession
#: hop runs through ``labdata``'s own Entrez client, which `_get`'s retry does not wrap (#9).
_TRANSIENT_LABDATA = re.compile(
    r"\b(?:429|50[0234]|rate.?limit|timed?.?out|temporarily|connection)\b", re.I
)

#: NCBI E-utilities. ``efetch db=sra`` takes experiment accessions; ``db=biosample`` takes SAMN ids.
EUTILS_EFETCH = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/efetch.fcgi"

#: How many ids to put in one efetch. NCBI asks for POST above ~200; we stay well under and page.
_BATCH = 100

SOURCE = "ncbi-sra+biosample"


def _efetch(db: str, ids: list[str], **params: str) -> str:
    query = {"db": db, "id": ",".join(ids), "retmode": "xml", **params}
    # NCBI raises the eutils rate limit from 3 to 10 req/sec for a keyed caller. Consume NCBI_API_KEY
    # if the operator's environment sets one (it commonly does); without it we simply stay keyless and
    # lean on `_get`'s 429 backoff (#9). The key stays in the request params only — never logged.
    key = os.environ.get("NCBI_API_KEY")
    if key:
        query["api_key"] = key
    return _get(EUTILS_EFETCH, query)


def _text(node: ElementTree.Element | None, path: str) -> str:
    if node is None:
        return ""
    return " ".join((node.findtext(path) or "").split())


def _external_id(node: ElementTree.Element | None, namespace: str) -> str | None:
    """An ``<EXTERNAL_ID namespace="...">`` under ``node``. How the archive links its own namespaces."""
    if node is None:
        return None
    for ident in node.findall(".//EXTERNAL_ID"):
        if ident.get("namespace") == namespace and (ident.text or "").strip():
            return ident.text.strip()
    return None


def _free(label: str, text: str | None) -> list[FreeText]:
    """One FreeText, or none. Empty prose is absence, not an empty string."""
    cleaned = " ".join((text or "").split())
    return [FreeText(label=label, text=cleaned)] if cleaned else []


def parse_sra_package_set(xml: str) -> list[ArchiveRecord]:
    """``efetch db=sra`` XML -> project/sample/experiment/run records.

    One ``EXPERIMENT_PACKAGE`` carries the whole chain for one experiment, so the same STUDY and the
    same SAMPLE appear in several packages. They are de-duplicated by accession here rather than
    merged later: two packages describing one study describe it identically, and a "merge" would be
    inventing a reconciliation problem that the archive does not have.

    A sample record is keyed by its **BioSample** accession when the record declares one, because
    that is the id that survives leaving SRA. The experiment's ``parent`` is rewritten to match,
    using the mapping the record itself provides — code following the record, not inferring it.
    """
    try:
        root = ElementTree.fromstring(xml)
    except ElementTree.ParseError as exc:
        raise RemoteError(f"efetch db=sra returned unparsable XML: {exc}") from exc

    projects: dict[str, ArchiveRecord] = {}
    samples: dict[str, ArchiveRecord] = {}
    experiments: dict[str, ArchiveRecord] = {}
    runs: dict[str, ArchiveRecord] = {}

    for pkg in root.findall(".//EXPERIMENT_PACKAGE"):
        study = pkg.find("STUDY")
        study_id = None
        if study is not None:
            study_id = _external_id(study.find("IDENTIFIERS"), "BioProject") or study.get(
                "accession"
            )
        centre = ""
        submission = pkg.find("SUBMISSION")
        if submission is not None:
            centre = submission.get("center_name") or ""

        if study_id and study_id not in projects:
            projects[study_id] = ArchiveRecord(
                level="project",
                accession=study_id,
                attributes=([RecordAttribute(name="center_name", value=centre)] if centre else []),
                free_text=[
                    *_free("study_title", _text(study, "DESCRIPTOR/STUDY_TITLE")),
                    *_free("study_abstract", _text(study, "DESCRIPTOR/STUDY_ABSTRACT")),
                ],
            )

        sample = pkg.find("SAMPLE")
        sample_id = None
        if sample is not None:
            sample_id = _external_id(sample.find("IDENTIFIERS"), "BioSample") or sample.get(
                "accession"
            )
            if sample_id and sample_id not in samples:
                samples[sample_id] = ArchiveRecord(
                    level="sample",
                    accession=sample_id,
                    parent=study_id,
                    attributes=[
                        *_taxonomy(_text(sample, "SAMPLE_NAME/TAXON_ID")),
                        *_sample_attributes(sample),
                    ],
                    free_text=_free("sample_alias", sample.get("alias")),
                )

        experiment = pkg.find("EXPERIMENT")
        exp_id = experiment.get("accession") if experiment is not None else None
        if experiment is not None and exp_id and exp_id not in experiments:
            design = experiment.find("DESIGN")
            lib = experiment.find("DESIGN/LIBRARY_DESCRIPTOR")
            attrs = [
                RecordAttribute(name=name, value=value)
                for name, value in (
                    ("library_strategy", _text(lib, "LIBRARY_STRATEGY")),
                    ("library_source", _text(lib, "LIBRARY_SOURCE")),
                    ("library_selection", _text(lib, "LIBRARY_SELECTION")),
                    ("instrument_model", _text(experiment, ".//INSTRUMENT_MODEL")),
                )
                if value
            ]
            experiments[exp_id] = ArchiveRecord(
                level="experiment",
                accession=exp_id,
                parent=sample_id,
                attributes=attrs,
                free_text=[
                    *_free("experiment_title", _text(experiment, "TITLE")),
                    *_free("experiment_alias", experiment.get("alias")),
                    *_free("library_name", _text(lib, "LIBRARY_NAME")),
                    # the protocol paragraph: where "Single Cell 3 v3.1 Reagent Kits ... 28+94 nt
                    # pair-end reads" lives. The one piece of prose that describes the chemistry.
                    *_free("design_description", _text(design, "DESIGN_DESCRIPTION")),
                ],
            )

        for run in pkg.findall(".//RUN"):
            run_id = run.get("accession")
            if not run_id or run_id in runs:
                continue
            runs[run_id] = ArchiveRecord(
                level="run",
                accession=run_id,
                parent=exp_id,
                free_text=_free("run_alias", run.get("alias")),
                filenames=_original_filenames(run),
            )

    return [
        *sorted(projects.values(), key=lambda r: r.accession),
        *sorted(samples.values(), key=lambda r: r.accession),
        *sorted(experiments.values(), key=lambda r: r.accession),
        *sorted(runs.values(), key=lambda r: r.accession),
    ]


def _original_filenames(run: ElementTree.Element) -> list[str]:
    """What the submitter's files were called, per the archive.

    ``supertype="Original"`` only: the other entries are SRA's own normalized ``.sra``/``.lite``
    products, which are not files anyone has on disk under that name. These matter because a
    downloaded dataset does not always carry the run accession in its filenames, and then the
    original name is the only thing left that can join a file to its sample.
    """
    out: list[str] = []
    for f in run.findall(".//SRAFile"):
        if f.get("supertype") == "Original" and f.get("filename"):
            out.append(str(f.get("filename")))
    return sorted(set(out))


def _taxonomy(taxid: str) -> list[RecordAttribute]:
    """The organism the record declares, as a taxid.

    ``harmonized=False``: NCBI has no harmonized *attribute* called ``taxonomy_id`` — the organism
    lives in the record's structure, not in its attribute list. Recording it here keeps every declared
    fact about a sample in one place while keeping it out of the sample-attribute key space, which the
    960-name vocabulary owns. ``resolve`` reads it by name to fill ``experiment.organism``, which
    until now had to be typed on the command line and cited nothing.
    """
    return [RecordAttribute(name="taxonomy_id", value=taxid)] if taxid.isdigit() else []


def _sample_attributes(sample: ElementTree.Element) -> list[RecordAttribute]:
    """SRA's ``<SAMPLE_ATTRIBUTE><TAG>`` pairs, harmonized against NCBI's vocabulary where possible.

    This is the fallback path. When a BioSample record is reachable, :func:`merge_biosample_attributes`
    overwrites these with NCBI's *own* harmonization, which is authoritative in a way our synonym
    lookup is not. Applying the fallback first keeps a sample whose BioSample is unreachable (or which
    never had one) from silently having no attributes at all.
    """
    out: list[RecordAttribute] = []
    for attr in sample.findall(".//SAMPLE_ATTRIBUTE"):
        tag = " ".join((attr.findtext("TAG") or "").split())
        value = " ".join((attr.findtext("VALUE") or "").split())
        if not tag or not value:
            continue
        name = harmonize(tag)
        out.append(
            RecordAttribute(
                name=name or tag,
                value=value,
                harmonized=name is not None,
                raw_name=tag if name and name != tag else None,
            )
        )
    return sorted(out, key=lambda a: a.name)


def parse_biosample_set(xml: str) -> dict[str, list[RecordAttribute]]:
    """``efetch db=biosample`` XML -> BioSample accession -> attributes, using NCBI's harmonization.

    ``harmonized_name`` is NCBI's answer to "what is this attribute really called", computed by the
    people who own the vocabulary. Where it is absent the submitter invented the tag, and it is kept
    unharmonized rather than guessed at.
    """
    try:
        root = ElementTree.fromstring(xml)
    except ElementTree.ParseError as exc:
        raise RemoteError(f"efetch db=biosample returned unparsable XML: {exc}") from exc

    out: dict[str, list[RecordAttribute]] = {}
    for bs in root.findall(".//BioSample"):
        acc = bs.get("accession")
        if not acc:
            continue
        attrs: list[RecordAttribute] = []
        for attr in bs.findall(".//Attribute"):
            value = " ".join((attr.text or "").split())
            if not value:
                continue
            raw = attr.get("attribute_name") or ""
            harmonized = attr.get("harmonized_name")
            attrs.append(
                RecordAttribute(
                    name=harmonized or raw,
                    value=value,
                    harmonized=bool(harmonized),
                    raw_name=raw if harmonized and raw != harmonized else None,
                )
            )
        organism = bs.find(".//Description/Organism")
        if organism is not None and (organism.get("taxonomy_id") or "").isdigit():
            attrs.extend(_taxonomy(str(organism.get("taxonomy_id"))))
        owner = _text(bs.find("Owner"), "Name")
        if owner:
            attrs.append(RecordAttribute(name="center_name", value=owner))
        package = _text(bs, "Package")
        if package:
            # recorded, never enforced — see io/attributes.py for why narrowing by package is wrong
            attrs.append(RecordAttribute(name="biosample_package", value=package))
        out[acc] = sorted(attrs, key=lambda a: a.name)
    return out


def merge_biosample_attributes(
    records: list[ArchiveRecord], by_accession: dict[str, list[RecordAttribute]]
) -> list[ArchiveRecord]:
    """Replace a sample record's SRA-derived attributes with NCBI's harmonized ones where we have them."""
    out: list[ArchiveRecord] = []
    for rec in records:
        attrs = by_accession.get(rec.accession) if rec.level == "sample" else None
        out.append(rec.model_copy(update={"attributes": attrs}) if attrs else rec)
    return out


def parse_bioproject_set(xml: str) -> dict[str, list[RecordAttribute]]:
    """``efetch db=bioproject`` XML -> project accession -> the structured study facts (decision 5).

    Only the declared facts: data type, and the release/submission date. Title, abstract and centre
    already come from the SRA package, and asking two services for the same string invents a
    disagreement to arbitrate.
    """
    try:
        root = ElementTree.fromstring(xml)
    except ElementTree.ParseError as exc:
        raise RemoteError(f"efetch db=bioproject returned unparsable XML: {exc}") from exc

    out: dict[str, list[RecordAttribute]] = {}
    for summary in root.findall(".//DocumentSummary"):
        archive = summary.find(".//ArchiveID")
        acc = archive.get("accession") if archive is not None else None
        if not acc:
            continue
        attrs: list[RecordAttribute] = []
        types = sorted(
            {
                " ".join((d.text or "").split())
                for d in summary.findall(".//ProjectDataTypeSet/DataType")
                if d.text
            }
        )
        if types:
            attrs.append(RecordAttribute(name="data_type", value="; ".join(types)))
        sub = summary.find(".//Submission")
        if sub is not None and sub.get("submitted"):
            attrs.append(RecordAttribute(name="submission_date", value=str(sub.get("submitted"))))
        out[acc] = attrs
    return out


def _experiments_for(accession: str) -> list[str]:
    """Any accession -> the SRA experiment accessions under it, via ``labdata``.

    Delegated to :func:`labdata.experiments_for`, whose Entrez ``elink`` route traverses a GEO
    *SuperSeries* (which owns no runs of its own) and a BioProject umbrella transitively — the case
    our own GEO/SOFT recursion missed, silently losing the whole dataset while reporting success.
    ``labdata`` is a lab package that ships with ``liulab-genome`` in ``liulab-runtime``, so
    depending on it adds no environment the cluster does not already have.

    ``labdata`` raises on a malformed accession or a failed request and returns an empty list for a
    record with no SRA data; both become a loud :class:`RemoteError` here, because an accession that
    was *given* and resolves to nothing is a refusal, not a silent omission from a permanent manifest.
    """
    import labdata
    from labdata.exceptions import LabdataError

    # Retry a TRANSIENT labdata failure (intermittent eutils 5xx/429) with backoff, exactly as
    # `_get` does for seqforge's own eutils calls — otherwise a momentary NCBI blip aborts the whole
    # `records` stage and the dataset cannot compile (seen live: GSE274290 hit a bare HTTP 500). A
    # terminal error (malformed accession) still raises on the first attempt.
    attempt = 0
    while True:
        try:
            experiments = labdata.experiments_for(accession)
            break
        except LabdataError as exc:
            if _TRANSIENT_LABDATA.search(str(exc)) and attempt < _MAX_RETRIES:
                time.sleep(retry_delay(None, attempt))
                attempt += 1
                continue
            raise RemoteError(f"{accession}: could not resolve experiments: {exc}") from exc
    accessions = sorted({e.accession for e in experiments})
    if not accessions:
        raise RemoteError(
            f"{accession}: no experiments found. It may be unreleased (status=hup), or a record "
            "that carries no raw SRA data."
        )
    return accessions


def fetch_records(accession: str) -> ArchiveRecordSet:
    """Any accession -> every record the archive holds for it, across all four levels.

    Never guesses and never half-succeeds silently: a level that cannot be fetched is absent from the
    result, and the caller (``resolve``) is the one entitled to have an opinion about that.
    """
    experiments = _experiments_for(accession)

    packages: list[ArchiveRecord] = []
    for i in range(0, len(experiments), _BATCH):
        packages.extend(parse_sra_package_set(_efetch("sra", experiments[i : i + _BATCH])))

    biosamples = [
        r.accession for r in packages if r.level == "sample" and r.accession.startswith("SAM")
    ]
    if biosamples:
        harmonized: dict[str, list[RecordAttribute]] = {}
        for i in range(0, len(biosamples), _BATCH):
            harmonized.update(
                parse_biosample_set(
                    _efetch("biosample", biosamples[i : i + _BATCH], rettype="full")
                )
            )
        packages = merge_biosample_attributes(packages, harmonized)

    projects = [
        r.accession for r in packages if r.level == "project" and r.accession.startswith("PRJ")
    ]
    if projects:
        try:
            extra = parse_bioproject_set(_efetch("bioproject", projects))
        except RemoteError:
            extra = {}  # the study facts we already have came from the SRA package; this only adds
        packages = [
            r.model_copy(update={"attributes": [*r.attributes, *extra[r.accession]]})
            if r.level == "project" and r.accession in extra
            else r
            for r in packages
        ]

    return ArchiveRecordSet(source=SOURCE, query=accession, records=packages)


__all__ = [
    "EUTILS_EFETCH",
    "SOURCE",
    "fetch_records",
    "parse_sra_package_set",
    "parse_biosample_set",
    "parse_bioproject_set",
    "merge_biosample_attributes",
]
