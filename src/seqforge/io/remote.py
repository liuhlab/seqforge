"""``io peek`` / ``io resolve`` / ``io probe-remote`` — the ONLY network surface (design §4).

Three verbs, all bounded:

- ``resolve ACC`` — any GEO/SRA/ENA/BioProject accession -> a run inventory + declared metadata.
- ``peek URI``    — the first records of a remote gzipped FASTQ via an HTTP Range request. Never the
  whole file: a 517 MB run yields its leading records from a **64 KB** read (0.013 % of it).
- ``probe_remote URI`` — the same bounded Range read turned into a full role-free
  :class:`~seqforge.models.observation.Observation`, so ``resolve`` fingerprints a library straight
  from a URL with no local file; the provider md5 (ENA ``fastq_md5``) is the content-address (#39).

**The most useful thing here is not fetching — it is detecting what the archive threw away.**

SRA normalizes runs, and ``fasterq-dump`` **skips technical reads by default**
(``skip_tech = !(include-technical)``), so a 10x barcode read routinely vanishes from the
archive-generated FASTQ while remaining inside the ``.sra``. What is published then looks like plain
single-end RNA-seq and is silently unprocessable as single-cell. :func:`run_statistics` reads SRA's
own per-read table and :func:`dropped_reads` compares it against what ENA actually published — so we
learn this from two metadata calls, **before** downloading a byte. That is the rung-0 check.

The comparison is a genuine disagreement rather than a bug: NCBI and ENA report different
``base_count`` for the same run (8 757 663 750 vs 3 980 756 250 for SRR9170959) because they are two
different truths about what the file contains. The disagreement IS the signal.

Endpoint shapes here were verified against the live services, and several widely-repeated assumptions
proved wrong (see the constants). Some endpoints we depend on are undocumented — which is exactly why
they are pinned behind small parsers with offline tests.
"""

from __future__ import annotations

import re
import time
import zlib
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any
from xml.etree import ElementTree

import requests

from ..probe import (
    DEFAULT_MAX_BYTES,
    DEFAULT_MAX_READS,
    build_observation,
    content_key_from_md5,
    remote_content_key,
)
from ..probe.streaming import sample_fastq_stream

if TYPE_CHECKING:
    from ..models.observation import Observation

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

#: Transient HTTP statuses worth a retry rather than a hard abort. 429 is NCBI eutils' rate-limit reply
#: (3 req/sec keyless, by IP), which used to abort the whole `records` stage on a busy accession (#9);
#: the 5xx family covers a momentary gateway/service blip. Everything else is a real, terminal error.
_RETRY_STATUS = frozenset({429, 500, 502, 503, 504})
_MAX_RETRIES = 4
_BACKOFF_BASE = 1.0  # seconds; grows 1, 2, 4, 8 …
_MAX_BACKOFF = 16.0


def retry_delay(retry_after: str | None, attempt: int) -> float:
    """Seconds to wait before retry ``attempt`` (0-indexed): an integer ``Retry-After`` if the server
    sent one, else capped exponential backoff. Shape-agnostic (a header string) so both the ``requests``
    client here and taxonomy's ``urllib`` client can share one policy."""
    if retry_after and retry_after.strip().isdigit():
        return min(float(retry_after.strip()), _MAX_BACKOFF)
    return min(_BACKOFF_BASE * (2.0**attempt), _MAX_BACKOFF)


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
    """A bounded HTTP GET that RETRIES a transient status rather than aborting the stage.

    A single 429 from eutils used to fail the whole `records` fetch, so an accession with many
    experiments could not compile (#9). A transient status now backs off (honoring `Retry-After`) and
    retries a few times; a non-transient status, or an exhausted budget, still raises `RemoteError`.
    The api_key that lifts eutils' cap is added by the caller (`archive._efetch`) — it is a secret and
    belongs only where the request is built, never in this shared error path (`url` here is the base,
    so a key in `params` never reaches a log line).
    """
    attempt = 0
    while True:  # exits only by return (200) or raise (terminal status/error / exhausted budget)
        try:
            response = requests.get(url, params=params, timeout=timeout)
        except (requests.ConnectionError, requests.Timeout) as exc:
            # A dropped connection or a timeout is the transport-level twin of a 5xx: NCBI resets
            # connections under load ("Connection reset by peer" aborted GSE310667's records fetch
            # live). Back off and retry rather than abort the stage; a genuinely dead endpoint still
            # fails once the budget is spent.
            if attempt < _MAX_RETRIES:
                time.sleep(retry_delay(None, attempt))
                attempt += 1
                continue
            raise RemoteError(f"GET {url} failed: {exc}") from exc
        except requests.RequestException as exc:
            raise RemoteError(f"GET {url} failed: {exc}") from exc
        if response.status_code == 200:
            return response.text
        if response.status_code in _RETRY_STATUS and attempt < _MAX_RETRIES:
            time.sleep(retry_delay(response.headers.get("Retry-After"), attempt))
            attempt += 1
            continue
        raise RemoteError(f"GET {url} -> HTTP {response.status_code}: {response.text[:200]}")


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


def fastq_targets(run: dict[str, str]) -> list[tuple[str, str]]:
    """ENA's ``fastq_ftp`` paired with its ``fastq_md5`` -> ``[(https_url, md5), ...]`` (issue #39).

    ENA publishes both fields semicolon-joined and **index-aligned** (the md5 of ``fastq_ftp[i]`` is
    ``fastq_md5[i]``), so the join is positional and done *before* any sort — this is the one place a
    URL and its content hash arrive together, which is what lets the remote probe use the provider md5
    as the content-address without any local-file bridge. A run whose md5 list is missing or a different
    length yields no pairs rather than a mis-aligned guess: an empty list is a legitimate answer (ENA
    generates no FASTQ for the 10x-BAM case), never a silent mispairing. Sorted by URL for determinism.
    """
    urls = [p.strip() for p in (run.get("fastq_ftp") or "").split(";") if p.strip()]
    md5s = [m.strip() for m in (run.get("fastq_md5") or "").split(";") if m.strip()]
    if not urls or len(urls) != len(md5s):
        return []
    return sorted((f"https://{u}", m) for u, m in zip(urls, md5s, strict=True))


def fastq_targets_meta(run: dict[str, str]) -> list[tuple[str, str, int]]:
    """``fastq_targets`` plus each file's ``fastq_bytes`` -> ``[(https_url, md5, size_bytes), ...]``.

    ENA index-aligns ``fastq_ftp``, ``fastq_md5`` **and** ``fastq_bytes`` by ``;``, so the three join
    positionally. The extra field is the hosted file's exact compressed size, which an SRA fingerprint
    adopts (with the md5-derived address) so a library streamed from the ``.sra`` gets the *same*
    ``FileIdentity`` — sha, size, and basename — a URL/ENA download of that file would, and therefore a
    portable ``dataset_hash``. A run whose ``fastq_bytes`` is missing or a different length still yields
    pairs, with size ``0`` — the md5 is the identity that matters; the size is a bonus. Sorted by URL to
    match :func:`fastq_targets`. Empty (never a mispairing) when the URL/md5 lists disagree.
    """
    targets = fastq_targets(run)  # (url, md5) sorted by url, or [] on a url/md5 mismatch
    if not targets:
        return []
    sizes_raw = [b.strip() for b in (run.get("fastq_bytes") or "").split(";") if b.strip()]
    # fastq_bytes is aligned to the UNSORTED fastq_ftp, so pair size to url pre-sort, then look up.
    urls_unsorted = [p.strip() for p in (run.get("fastq_ftp") or "").split(";") if p.strip()]
    size_by_url: dict[str, int] = {}
    if len(sizes_raw) == len(urls_unsorted):
        for url, size in zip(urls_unsorted, sizes_raw, strict=True):
            size_by_url[f"https://{url}"] = int(size) if size.isdigit() else 0
    return [(url, md5, size_by_url.get(url, 0)) for url, md5 in targets]


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

    Two metadata calls, no bytes — the rung-0 check. A dropped 10x barcode read turns a
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
    from bytes.
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
    """What a bounded range-read saw. ``compressed_bytes_read`` is the bounded-read receipt."""

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
    call, which makes the budget a real *decompressed*-byte bound rather than a compressed-byte
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


#: How many COMPRESSED bytes a remote fingerprint range-reads by default. Large enough to decompress to
#: a strong head sample (the read/byte budgets still stop decompression early), small enough to never
#: approach a whole file: 8 MB is ~1.5 % of a 517 MB run. The network cost of a probe is this one GET.
DEFAULT_REMOTE_COMPRESSED_BYTES = 8 << 20

_CONTENT_RANGE = re.compile(r"bytes\s+\d+-\d+/(\d+)", re.IGNORECASE)


def _content_range_total(headers: Any) -> int | None:
    """The total file size a 206 declares in ``Content-Range: bytes 0-N/TOTAL`` (``None`` if absent).

    A server may answer ``.../*`` when it does not know the length; then the size is unknown and the
    caller falls back to the bytes actually read.
    """
    match = _CONTENT_RANGE.search(headers.get("Content-Range", "") or "")
    return int(match.group(1)) if match else None


def _range_get(uri: str, *, max_bytes: int) -> tuple[bytes, int | None]:
    """Range-read the first ``max_bytes`` of a remote file. Returns ``(blob, total_size_or_None)``.

    The one bounded-fetch primitive behind both :func:`peek` and :func:`probe_remote`. We assert
    **HTTP 206**, not the presence of ``Accept-Ranges``: a server may advertise ranges, ignore the
    header, and answer 200 with the entire file, and trusting the advertisement is exactly how a
    "bounded" read becomes a multi-GB download. The status code is the contract, so a 200 is a refusal.
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
                "file. Refusing to read it: 'bounded' means bounded by the server, not by our intentions. "
                "Fetch it deliberately, or use a host that honours Range."
            )
        if response.status_code != 206:
            raise RemoteError(
                f"GET {uri} -> HTTP {response.status_code} (expected 206 Partial Content)"
            )
        blob = response.content[:max_bytes]
        total = _content_range_total(response.headers)
    finally:
        response.close()
    return blob, total


def peek(
    uri: str, *, max_reads: int = 4, max_bytes: int = 1 << 16, decompressed_cap: int = 1 << 22
) -> dict[str, Any]:
    """Range-read the head of a remote gzipped FASTQ. Never downloads the file.

    The defaults fetch 64 KB — 0.013 % of a 517 MB run, and several thousand reads' worth. This is the
    diagnostic (headers + read lengths); :func:`probe_remote` is the same bounded read turned into a
    full :class:`~seqforge.models.observation.Observation` that ``resolve`` can score.
    """
    blob, _total = _range_get(uri, max_bytes=max_bytes)
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


def _uri_basename(uri: str) -> str:
    """The filename a URL ends in — the remote analog of a local ``Path.name``.

    Strips a query/fragment and a trailing slash. Feeds a no-md5 remote content key exactly as
    ``Path.name`` feeds the local one; when a provider md5 is known it is irrelevant (the md5 carries
    no name, by design — for hosted bytes an identical md5 means identical content).
    """
    tail = uri.split("?", 1)[0].split("#", 1)[0].rstrip("/")
    return tail.rsplit("/", 1)[-1] or uri


def probe_remote(
    uri: str,
    *,
    md5: str | None = None,
    max_reads: int = DEFAULT_MAX_READS,
    max_bytes: int = DEFAULT_MAX_BYTES,
    max_compressed_bytes: int = DEFAULT_REMOTE_COMPRESSED_BYTES,
) -> tuple[Observation, list[str]]:
    """Fingerprint a remote gzipped FASTQ into ``(Observation, seqs)`` WITHOUT staging it (issue #39).

    The remote twin of ``probe.probe_sample``: one bounded HTTP Range read (``max_compressed_bytes``)
    is decompressed under the same head budget (``max_reads`` / ``max_bytes``) and fed through the
    identical Tier-A pipeline (``probe.build_observation``), so a URL resolves to a library exactly as a
    local file does — the returned pair drops straight into ``resolve.resolve_dataset`` via its
    ``_probed`` map, no local file anywhere.

    When the provider ``md5`` (ENA ``fastq_md5``, paired with the URL by :func:`fastq_targets`) is
    known it IS the content-address (``content_key_from_md5``), matching the hosted bytes with zero read
    of the body beyond the head; otherwise a bounded remote key over (basename + total size + head) is
    derived. ``size_bytes`` is the total the 206 declares in Content-Range, else the bytes read.
    """
    from io import BytesIO

    blob, total = _range_get(uri, max_bytes=max_compressed_bytes)
    if not blob:
        raise RemoteError(f"{uri}: range read returned no bytes")
    sample = sample_fastq_stream(BytesIO(blob), max_reads, max_bytes)
    if not sample.seqs:
        raise RemoteError(
            f"{uri}: no FASTQ records in the {len(blob)}-byte head — not a gzipped FASTQ, or the "
            "range was too small to hold one record."
        )
    size_bytes = total if total and total > 0 else len(blob)
    basename = _uri_basename(uri)
    sha256 = (
        content_key_from_md5(md5) if md5 else remote_content_key(basename, size_bytes, sample.seqs)
    )
    return build_observation(
        sample,
        size_bytes=size_bytes,
        sha256=sha256,
        basename=basename,
        local_uri=None,
        isize=None,
        max_reads=max_reads,
        max_bytes=max_bytes,
    )
