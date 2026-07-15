"""``io peek`` / ``io resolve`` — the ONLY network surface (design §4, R3).

Two verbs, both bounded:

- ``resolve ACC`` — any GEO/SRA/ENA/BioProject accession -> a run inventory + declared metadata.
- ``peek URI``    — the first records of a remote gzipped FASTQ via an HTTP Range request. Never the
  whole file: a 517 MB run yields its leading records from a **64 KB** read (0.013 % of it).

**The most useful thing here is not fetching — it is detecting what the archive threw away.**

SRA normalizes runs, and ``fasterq-dump`` **skips technical reads by default**
(``skip_tech = !(include-technical)``), so a 10x barcode read routinely vanishes from the
archive-generated FASTQ while remaining inside the ``.sra``. What is published then looks like plain
single-end RNA-seq and is silently unprocessable as single-cell. :func:`run_statistics` reads SRA's
own per-read table and :func:`dropped_reads` compares it against what ENA actually published — so we
learn this from two metadata calls, **before** downloading a byte. That is the R11 rung-0 check.

The comparison is a genuine R6 disagreement rather than a bug: NCBI and ENA report different
``base_count`` for the same run (8 757 663 750 vs 3 980 756 250 for SRR9170959) because they are two
different truths about what the file contains. The disagreement IS the signal.

Endpoint shapes here were verified against the live services, and several widely-repeated assumptions
proved wrong (see the constants). Some endpoints we depend on are undocumented — which is exactly why
they are pinned behind small parsers with offline tests.
"""

from __future__ import annotations

import re
import zlib
from dataclasses import dataclass, field
from typing import Any
from xml.etree import ElementTree

import requests

#: ENA Portal API. Documented. GEO accessions are REJECTED (HTTP 400) — resolve GSE->SRP first.
ENA_FILEREPORT = "https://www.ebi.ac.uk/ena/portal/api/filereport"

#: SRA's per-run read table. **Undocumented**, but authoritative and irreplaceable: it is the only
#: place that exposes reads-per-spot and per-read length, i.e. the only way to see a dropped read.
NCBI_RUN_NEW = "https://trace.ncbi.nlm.nih.gov/Traces/sra-db-be/run_new"

#: GEO SOFT. Preferred over eutils for GSE->SRP: no rate limit (eutils is 3/sec keyless, by IP), and
#: it is the only source that reveals SuperSeries membership.
GEO_ACC = "https://www.ncbi.nlm.nih.gov/geo/query/acc.cgi"

#: SRA Data Locator. GET/POST **form-encoded, not JSON**; `version` is in the path, not a param. To
#: request originals, OMIT `filetype` — there is no `filetype=src`, and `filetype=all` returns
#: nothing (`all`/`any` are client-side sentinels sra-tools strips). Errors arrive as HTTP 200 with a
#: per-accession `status` in the body, so the HTTP code means nothing. `sra-pub-src-1`/`-2` are
#: PUBLIC and anonymously readable — the "requester-pays" folklore is wrong for them.
SDL_RETRIEVE = "https://locate.ncbi.nlm.nih.gov/sdl/2/retrieve"

#: What to ask ENA for. NB ENA has NO per-read-length field at all — only `read_count` (spots) and
#: `base_count` (total). That absence is the whole reason NCBI_RUN_NEW is needed.
ENA_FIELDS = (
    "run_accession,experiment_accession,study_accession,sample_accession,scientific_name,tax_id,"
    "instrument_platform,instrument_model,library_strategy,library_source,library_selection,"
    "library_layout,read_count,base_count,fastq_ftp,fastq_md5,fastq_bytes,submitted_ftp,"
    "submitted_bytes,submitted_format,sra_ftp,experiment_title,sample_title,first_public"
)

#: Patterns lifted from ENA's own HTTP 400 response bodies, so they match what the API accepts.
_ACCESSION_KINDS: tuple[tuple[str, str], ...] = (
    (r"^GSE\d+$", "geo_series"),
    (r"^GSM\d+$", "geo_sample"),
    (r"^PRJ[A-Z]{2}\d+$", "bioproject"),
    (r"^[EDSR]RP\d{6,}$", "study"),
    (r"^[ESDR]RX\d{6,}$", "experiment"),
    (r"^[ESDR]RR\d{6,}$", "run"),
    (r"^[ESDR]RS\d{6,}$", "sample"),
    (r"^(SAME[A]?\d{6,}|SAM[ND]\d{8})$", "biosample"),
    (r"^[EDS]RA\d{6,}$", "submission"),
)

_SRP_IN_SOFT = re.compile(r"term=([EDSR]RP\d+)")
_SUPERSERIES_OF = re.compile(r"^!Series_relation = SuperSeries of: (GSE\d+)", re.MULTILINE)

_DEFAULT_TIMEOUT = 30


class NotYetImplemented(RuntimeError):
    """A declared verb whose stage has not landed yet (distinct from a domain refusal)."""


class RemoteError(RuntimeError):
    """The network surface failed, or an accession is unknown. Never a silent empty result."""


def classify_accession(accession: str) -> str:
    """Which archive namespace is this? ``unknown`` is a first-class answer, not an exception."""
    acc = accession.strip()
    for pattern, kind in _ACCESSION_KINDS:
        if re.match(pattern, acc, re.IGNORECASE):
            return kind
    return "unknown"


def _get(url: str, params: dict[str, str] | None = None, timeout: int = _DEFAULT_TIMEOUT) -> str:
    try:
        response = requests.get(url, params=params, timeout=timeout)
    except requests.RequestException as exc:
        raise RemoteError(f"GET {url} failed: {exc}") from exc
    if response.status_code != 200:
        raise RemoteError(f"GET {url} -> HTTP {response.status_code}: {response.text[:200]}")
    return response.text


def parse_soft_srp(soft: str) -> list[str]:
    """SRP study accessions declared in a GEO SOFT record."""
    return sorted(set(_SRP_IN_SOFT.findall(soft)))


def parse_soft_superseries(soft: str) -> list[str]:
    """Sub-series of a SuperSeries.

    **A SuperSeries owns no runs.** eutils and runinfo both return zero for one, silently — so a
    resolver that does not recurse loses the whole dataset while reporting success. That is the worst
    kind of wrong, and it is why SuperSeries membership is parsed explicitly instead of trusted away.
    """
    return sorted(set(_SUPERSERIES_OF.findall(soft)))


def geo_soft(accession: str) -> str:
    """Fetch a brief GEO SOFT record."""
    return _get(GEO_ACC, {"acc": accession, "targ": "self", "form": "text", "view": "brief"})


def geo_to_studies(accession: str, *, _depth: int = 0) -> list[str]:
    """GSE -> SRP list, recursing through SuperSeries (which otherwise resolve to nothing)."""
    if _depth > 3:
        raise RemoteError(f"{accession}: SuperSeries nesting too deep; refusing to recurse further")
    soft = geo_soft(accession)
    studies = parse_soft_srp(soft)
    if studies:
        return studies
    subs = parse_soft_superseries(soft)
    if not subs:
        raise RemoteError(
            f"{accession}: no SRA study in the GEO record. It may be a SuperSeries with no declared "
            "sub-series, unreleased (status=hup), or carry no raw data."
        )
    found: list[str] = []
    for sub in subs:
        found.extend(geo_to_studies(sub, _depth=_depth + 1))
    return sorted(set(found))


def parse_filereport(tsv: str) -> list[dict[str, str]]:
    """Parse ENA's TSV. A header-only response is a legitimate empty answer, not an error."""
    lines = [ln for ln in tsv.splitlines() if ln.strip()]
    if not lines:
        return []
    header = lines[0].split("\t")
    return [dict(zip(header, ln.split("\t"), strict=False)) for ln in lines[1:]]


def ena_filereport(accession: str, *, fields: str = ENA_FIELDS) -> list[dict[str, str]]:
    """ENA's run inventory for a study/run/experiment/sample accession."""
    tsv = _get(
        ENA_FILEREPORT,
        {
            "accession": accession,
            "result": "read_run",
            "fields": fields,
            "format": "tsv",
            "limit": "0",
        },
    )
    return parse_filereport(tsv)


def fastq_urls(run: dict[str, str]) -> list[str]:
    """ENA's ``fastq_ftp`` -> https URLs.

    Semicolon-separated, no scheme. **Do not assume ``_1``/``_2``**: with one application read the
    file may be ``ACC.fastq.gz`` *or* ``ACC_1.fastq.gz`` — ENA's own docs say it is not deterministic
    — and ordering is not guaranteed, hence the sort. An empty list is meaningful rather than a
    failure: ENA generates no FASTQ at all for cellranger/longranger BAMs, or BAMs carrying
    CB/CR/CY/RX/QX tags, which is precisely the 10x case.
    """
    raw = (run.get("fastq_ftp") or "").strip()
    if not raw:
        return []
    return sorted(f"https://{p}" for p in raw.split(";") if p.strip())


@dataclass(frozen=True)
class ReadStat:
    """One read within a spot, as SRA itself describes it."""

    index: int
    average_length: int
    count: int


@dataclass
class RunStatistics:
    """SRA's per-read table for a run — the only exposure of reads-per-spot anywhere."""

    accession: str
    n_reads: int = 0
    reads: list[ReadStat] = field(default_factory=list)
    #: e.g. "TBT" = Technical/Biological/Technical. Only present for fastq-load.py submissions.
    read_types: str | None = None

    @property
    def spot_length(self) -> int:
        return sum(r.average_length for r in self.reads)

    def to_json(self) -> dict[str, Any]:
        return {
            "accession": self.accession,
            "n_reads": self.n_reads,
            "spot_length": self.spot_length,
            "reads": [
                {"index": r.index, "average_length": r.average_length, "count": r.count}
                for r in self.reads
            ],
            "read_types": self.read_types,
        }


def parse_run_new(xml: str, accession: str = "") -> RunStatistics:
    """Parse SRA's ``run_new`` XML into a per-read table.

    The endpoint is undocumented, so every field is treated as optional: a missing ``readTypes`` is
    normal (it appears only for fastq-load.py submissions) and must never be an error.
    """
    stats = RunStatistics(accession=accession)
    try:
        root = ElementTree.fromstring(xml)
    except ElementTree.ParseError as exc:
        raise RemoteError(f"{accession}: run_new returned unparsable XML: {exc}") from exc

    node = root.find(".//Statistics")
    if node is not None:
        stats.n_reads = int(node.get("nreads") or 0)
        for read in node.findall("Read"):
            stats.reads.append(
                ReadStat(
                    index=int(read.get("index") or 0),
                    average_length=int(float(read.get("average") or 0)),
                    count=int(read.get("count") or 0),
                )
            )
    for attr in root.findall(".//RUN_ATTRIBUTE"):
        if (attr.findtext("TAG") or "") == "options":
            match = re.search(r"readTypes=(\w+)", attr.findtext("VALUE") or "")
            if match:
                stats.read_types = match.group(1)
    return stats


def run_statistics(accession: str) -> RunStatistics:
    """Fetch SRA's per-read table for one run (undocumented endpoint; see the module docstring)."""
    return parse_run_new(_get(NCBI_RUN_NEW, {"acc": accession}), accession=accession)


@dataclass(frozen=True)
class DroppedReads:
    """Evidence that the archive published fewer bases per spot than the run actually holds."""

    sra_spot_length: int
    ena_spot_length: float
    missing_bases: float
    n_reads_sra: int
    n_files_ena: int
    read_types: str | None

    def to_json(self) -> dict[str, Any]:
        return {
            "sra_spot_length": self.sra_spot_length,
            "ena_spot_length": round(self.ena_spot_length, 2),
            "missing_bases": round(self.missing_bases, 2),
            "n_reads_sra": self.n_reads_sra,
            "n_files_ena": self.n_files_ena,
            "read_types": self.read_types,
        }


def dropped_reads(run: dict[str, str], stats: RunStatistics) -> DroppedReads | None:
    """Did the archive drop a read? Compare SRA's own spot length to what ENA published.

    Two metadata calls, no bytes — the R11 rung-0 check. A dropped 10x barcode read turns a
    single-cell dataset into something that merely looks like single-end RNA-seq, and no amount of
    downstream cleverness recovers it from the published FASTQ.

    ``None`` when they agree, or when SRA reports nothing usable: an ABSTAIN, never a false accusation.
    """
    read_count = float(run.get("read_count") or 0)
    base_count = float(run.get("base_count") or 0)
    if not read_count or not base_count or not stats.reads:
        return None
    ena_spot = base_count / read_count
    sra_spot = stats.spot_length
    if sra_spot <= ena_spot + 1:  # +1 absorbs rounding in ENA's averages
        return None
    return DroppedReads(
        sra_spot_length=sra_spot,
        ena_spot_length=ena_spot,
        missing_bases=sra_spot - ena_spot,
        n_reads_sra=stats.n_reads,
        n_files_ena=len(fastq_urls(run)),
        read_types=stats.read_types,
    )


def technical_read_remedy(accession: str) -> str:
    """The operable remedy for a dropped technical read (design §1.5: remedies must be actionable).

    ``fasterq-dump --include-technical`` is the real fix; SDL is a fallback. Originals
    (``sra-pub-src-*``) are published for "select high value and newly-released studies" only, and
    most runs return just ``type=sra`` — so naming SDL first would send people down a dead end.
    """
    return (
        "the technical read is still inside the .sra — fasterq-dump skips it BY DEFAULT. Re-fetch "
        f"with `fasterq-dump --include-technical --split-files {accession}` (ENA's generated FASTQ "
        "omits it, so do not use that). Fallback only: the original submitted files may exist via "
        f"the SRA Data Locator ({SDL_RETRIEVE}; omit `filetype` to request originals), but they are "
        "published for select studies only."
    )


def _annotate(run: dict[str, str]) -> dict[str, Any]:
    """One ENA run + our derived facts. Degrades on error; an undocumented endpoint must not abort."""
    entry: dict[str, Any] = dict(run)
    entry["fastq_urls"] = fastq_urls(run)
    entry["technical_read_dropped"] = False

    if not entry["fastq_urls"] and (run.get("submitted_ftp") or "").strip():
        entry["note"] = (
            "ENA generated no FASTQ for this run — it does not for cellranger/longranger BAMs, or "
            "BAMs carrying CB/CR/CY/RX/QX tags (the 10x case). Only submitted files exist."
        )

    run_acc = (run.get("run_accession") or "").strip()
    if run_acc.upper().startswith("SRR"):  # run_new is an NCBI endpoint; ERR/DRR are not served
        try:
            stats = run_statistics(run_acc)
            entry["run_statistics"] = stats.to_json()
            dropped = dropped_reads(run, stats)
            if dropped is not None:
                entry["technical_read_dropped"] = True
                entry["dropped"] = dropped.to_json()
                entry["remedy"] = technical_read_remedy(run_acc)
        except RemoteError as exc:
            entry["run_statistics_error"] = str(exc)
    return entry


def resolve_accession(accession: str, *, check_reads: bool = True) -> dict[str, Any]:
    """Expand any accession into runs + declared metadata + a dropped-technical-read verdict.

    GEO is resolved to SRP first (ENA rejects GSE outright), recursing through SuperSeries. This
    reports declared facts and abstains loudly; it never guesses a chemistry — that is resolve's job,
    from bytes (R2/R6).
    """
    acc = accession.strip()
    kind = classify_accession(acc)
    if kind == "unknown":
        raise RemoteError(
            f"unrecognized accession {acc!r}. Known: GSE/GSM, PRJNA/PRJEB, SRP/ERP, SRX/ERX, "
            "SRR/ERR, SRS/ERS, SAMN/SAMEA."
        )

    studies: list[str] = []
    if kind in ("geo_series", "geo_sample"):
        studies = geo_to_studies(acc)
        runs = [r for s in studies for r in ena_filereport(s)]
    else:
        runs = ena_filereport(acc)

    if not runs:
        raise RemoteError(
            f"{acc}: ENA returned no runs. It may be unreleased (status=hup), or a SuperSeries whose "
            "sub-series must be resolved individually."
        )

    entries = (
        [_annotate(r) for r in runs]
        if check_reads
        else [{**r, "fastq_urls": fastq_urls(r)} for r in runs]
    )
    return {
        "accession": acc,
        "kind": kind,
        "studies": studies,
        "n_runs": len(entries),
        "n_runs_missing_technical_read": sum(1 for e in entries if e.get("technical_read_dropped")),
        "runs": entries,
    }


@dataclass
class PeekResult:
    """What a bounded range-read saw. ``compressed_bytes_read`` is the R3 receipt."""

    uri: str
    compressed_bytes_read: int
    decompressed_bytes: int
    n_records: int
    read_lengths: list[int] = field(default_factory=list)
    example_header: str | None = None

    def to_json(self) -> dict[str, Any]:
        return {
            "uri": self.uri,
            "compressed_bytes_read": self.compressed_bytes_read,
            "decompressed_bytes": self.decompressed_bytes,
            "n_records": self.n_records,
            "read_lengths": self.read_lengths,
            "example_header": self.example_header,
        }


def decompress_prefix(blob: bytes, *, max_bytes: int) -> bytes:
    """Inflate a gzip PREFIX, tolerating the truncated tail, capped at ``max_bytes``.

    ``wbits=31`` = 16 (gzip wrapper) + 15 (window). A truncated deflate stream does **not** raise —
    you simply get fewer bytes back and ``eof`` stays False. So "handling the truncation" means
    stopping and dropping the last partial record, nothing more. ``max_length`` caps expansion per
    call, which makes the budget a real *decompressed*-byte bound (R3) rather than a compressed-byte
    proxy — and incidentally makes a zip bomb a non-event.
    """
    decomp = zlib.decompressobj(31)
    try:
        return bytes(decomp.decompress(blob, max_bytes))
    except zlib.error as exc:  # a corrupt member, as opposed to a merely truncated one
        raise RemoteError(f"gzip stream is not readable: {exc}") from exc


def parse_fastq_prefix(text: str, *, max_reads: int) -> tuple[list[str], list[int]]:
    """Read whole FASTQ records from a decompressed prefix, discarding the trailing partial one."""
    lines = text.split("\n")
    if lines and not text.endswith("\n"):
        lines = lines[:-1]  # the final line was cut mid-write by the range boundary
    headers: list[str] = []
    lengths: list[int] = []
    for i in range(0, max(0, len(lines) - 3), 4):
        if not lines[i].startswith("@"):
            continue
        headers.append(lines[i])
        lengths.append(len(lines[i + 1]))
        if len(headers) >= max_reads:
            break
    return headers, lengths


def peek(
    uri: str, *, max_reads: int = 4, max_bytes: int = 1 << 16, decompressed_cap: int = 1 << 22
) -> dict[str, Any]:
    """Range-read the head of a remote gzipped FASTQ. Never downloads the file (R3).

    The defaults fetch 64 KB — 0.013 % of a 517 MB run, and several thousand reads' worth.

    We assert **HTTP 206**, not the presence of ``Accept-Ranges``. A server may advertise ranges,
    ignore the header, and answer 200 with the entire file; trusting the advertisement is exactly how
    a "bounded" read becomes a 40 GB download. The status code is the contract, so a 200 is a refusal.
    """
    try:
        response = requests.get(
            uri,
            headers={"Range": f"bytes=0-{max_bytes - 1}"},
            timeout=_DEFAULT_TIMEOUT,
            stream=True,
        )
    except requests.RequestException as exc:
        raise RemoteError(f"GET {uri} failed: {exc}") from exc

    try:
        if response.status_code == 200:
            raise RemoteError(
                f"{uri}: the server ignored our Range header and answered 200 — that is the whole "
                "file. Refusing to read it: R3 means bounded by the server, not by our intentions. "
                "Fetch it deliberately, or use a host that honours Range."
            )
        if response.status_code != 206:
            raise RemoteError(
                f"GET {uri} -> HTTP {response.status_code} (expected 206 Partial Content)"
            )
        blob = response.content[:max_bytes]
    finally:
        response.close()

    text = decompress_prefix(blob, max_bytes=decompressed_cap).decode("utf-8", errors="replace")
    headers, lengths = parse_fastq_prefix(text, max_reads=max_reads)
    return PeekResult(
        uri=uri,
        compressed_bytes_read=len(blob),
        decompressed_bytes=len(text),
        n_records=len(headers),
        read_lengths=lengths,
        example_header=headers[0] if headers else None,
    ).to_json()
