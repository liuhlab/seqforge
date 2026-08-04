"""``io probe-sra`` / ``preflight --accession`` — fingerprint an SRA library with no download.

The archive twin of :func:`seqforge.io.remote.probe_remote`. ``probe_remote`` fingerprints a *hosted
URL* with one bounded HTTP Range read; this fingerprints a *run accession* whose FASTQ was never
mirrored (or was mirrored unfaithfully) by streaming the first N spots of its ``.sra`` straight into
memory — no FASTQ, ``.sra``, or cache ever touches disk. The sra-tools half lives in **liulab-data**
(``labdata.stream_run_reads``); we *consume* it exactly as :mod:`seqforge.io.archive` consumes
``labdata.experiments_for``, and feed its reads through the identical Tier-A pipeline
(``probe.build_observation``), so a run resolves to a library exactly as a local file does.

Two things make the address portable. The stream buckets reads **by their within-spot index** (the
``.N`` tag ``fastq-dump --readids`` writes), so a variable number of reads per spot can never desync
mates. And the content-address follows a precedence: when ENA mirrored the run *faithfully* (a file per
mate, no dropped technical read) the ENA ``fastq_md5`` is adopted as the address
(:func:`~seqforge.probe.content_key_from_md5`) so an SRA fingerprint and a URL/ENA download of the same
file get the **same** ``FileIdentity`` — and therefore the same ``dataset_hash``; otherwise a synthetic,
N-invariant SRA address (:func:`~seqforge.probe.content_key_from_sra`) is used and flagged as
SRA-derived rather than hosted-byte-portable.
"""

from __future__ import annotations

import re
import time
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from ..fingerprint.build import FingerprintResult, assemble_package
from ..fingerprint.subsample import records_to_gz_bytes
from ..models.fingerprint import FilePin
from ..probe import (
    DEFAULT_MAX_BYTES,
    DEFAULT_MAX_READS,
    build_observation,
    content_key_from_md5,
    content_key_from_sra,
)
from ..probe.core import WholeFile
from ..probe.streaming import Budget, FastqHead, Record
from .remote import (
    _MAX_RETRIES,
    ReadStat,
    RemoteError,
    RunStatistics,
    _uri_basename,
    dropped_reads,
    fastq_targets_meta,
    resolve_accession,
    retry_delay,
)

if TYPE_CHECKING:
    from ..models.observation import Observation

#: The same transient-failure shapes :mod:`seqforge.io.archive` retries around ``labdata`` — a momentary
#: NCBI/SDL blip must not abort a stream that a retry would complete. ``labdata.stream_run_reads`` also
#: retries the tool internally; this covers the resolution around it.
_TRANSIENT_LABDATA = re.compile(
    r"\b(?:429|50[0234]|rate.?limit|timed?.?out|temporarily|connection)\b", re.I
)


@dataclass(frozen=True)
class SraMateProbe:
    """One mate (within-spot read index) of an SRA run, fingerprinted from a bounded stream.

    Carries both what a probe produces (the role-free :class:`Observation` and its sampled ``seqs``,
    for ``resolve``) and the raw 4-line ``records`` the slice is written from (for a fingerprint
    package). ``ena_verified`` records which content-address branch chose the identity: ``True`` means
    the ENA ``fastq_md5`` was adopted (the address matches the hosted bytes), ``False`` means a
    synthetic SRA-derived address (portable across probe budgets, but not the hosted-byte identity).
    """

    read_index: int
    observation: Observation
    seqs: list[str]
    records: list[Record]
    basename: str
    ena_verified: bool


def _stream_run(run_accession: str, *, n_spots: int) -> Any:
    """Stream the first ``n_spots`` spots of a run via ``labdata`` — the upstream consume seam.

    Imported lazily (like ``archive._experiments_for``) so ``labdata`` is a runtime dependency only for
    the SRA path, and a transient failure is retried with backoff before becoming a loud
    :class:`RemoteError`. Technical reads are kept — a chemistry fingerprint needs the barcode read.
    """
    import labdata
    from labdata.exceptions import LabdataError

    attempt = 0
    while True:
        try:
            return labdata.stream_run_reads(run_accession, n_spots=n_spots, include_technical=True)
        except LabdataError as exc:
            if _TRANSIENT_LABDATA.search(str(exc)) and attempt < _MAX_RETRIES:
                time.sleep(retry_delay(None, attempt))
                attempt += 1
                continue
            raise RemoteError(f"{run_accession}: could not stream reads: {exc}") from exc


def sra_whole_file(
    run_accession: str,
    read_index: int,
    *,
    ena: tuple[str, str, int] | None,
    spot_count: int,
    read_length: int,
) -> WholeFile:
    """Name one mate of an SRA run. ``ena`` is ``(url, md5, size)`` when the mirror is trustworthy.

    Two branches, one value — the content-address precedence :func:`probe_sra` documents:

    1. **ENA-mirrored and faithful** — adopt the hosted identity outright, so a stream and a
       URL/ENA download of that file get the same address and a portable ``dataset_hash``.
    2. **Otherwise** — a synthetic address over stable *whole-run* metadata
       (:func:`~seqforge.probe.content_key_from_sra`), N-invariant but explicitly not the
       hosted-byte identity, with the sra-tools split-layout basename.

    ``isize`` is ``None``: a spot stream is re-serialized on the fly and has no original gzip tail.
    """
    if ena is not None:
        url, md5, size = ena
        return WholeFile(
            basename=_uri_basename(url),
            sha256=content_key_from_md5(md5),
            size_bytes=size if size > 0 else max(1, spot_count * read_length),
            isize=None,
        )
    return WholeFile(
        basename=f"{run_accession}_{read_index}.fastq.gz",
        sha256=content_key_from_sra(
            run_accession, read_index, spot_count=spot_count, read_length=read_length
        ),
        size_bytes=max(1, spot_count * read_length),
        isize=None,
    )


def _observe_records(
    records: Sequence[Record], file: WholeFile, budget: Budget
) -> tuple[Observation, list[str]]:
    """Fingerprint one mate's records through the identical Tier-A pipeline a file would use.

    The records are serialized once (:func:`records_to_gz_bytes`) into the byte string that is *both*
    the probe input here and the package slice later — one serializer, no drift — then re-read through
    the source-agnostic reader and ``build_observation``, exactly as ``probe_remote`` feeds a
    range-read head.
    """
    from io import BytesIO

    gz = records_to_gz_bytes(records)
    return build_observation(FastqHead.read(BytesIO(gz), budget), file)


def _streamed_read_table(
    run_accession: str, preview: Any, mates: Sequence[int], spot_count: int
) -> RunStatistics:
    """SRA's per-read table for a run, read off the stream instead of fetched.

    ``run_new`` answers for one run per request, so asking it about every run of a study is one
    round-trip per run — the pre-pass that made a 1440-experiment plate deposit unaffordable to even
    refuse. A stream taken *with technical reads* already carries what that table says: one entry per
    within-spot index, at the length that index actually came back. So the mirror-faithfulness
    question :func:`~seqforge.io.remote.dropped_reads` answers is answered here, from bytes already
    in hand, and the endpoint is never called on this path.
    """
    return RunStatistics(
        accession=run_accession,
        n_reads=len(mates),
        reads=[
            ReadStat(index=index, average_length=preview.read_lengths[index], count=spot_count)
            for index in mates
        ],
    )


def probe_sra(
    run: dict[str, Any],
    *,
    n_reads: int = DEFAULT_MAX_READS,
    max_bytes: int = DEFAULT_MAX_BYTES,
) -> list[SraMateProbe]:
    """Fingerprint every mate of one SRA run from a bounded stream — one probe per within-spot read.

    ``run`` is an ENA filereport row (as :func:`seqforge.io.remote.resolve_accession` returns), used for
    both the run accession and the content-address decision. The first ``n_reads`` spots are streamed
    (:func:`_stream_run`) and bucketed by read index; each bucket becomes one :class:`SraMateProbe`.

    Content-address precedence (the ``dataset_hash`` portability crux):

    1. **ENA-mirrored AND faithful** — ENA lists a ``fastq_md5`` per mate, the file count equals the
       streamed mate count, and no technical read was dropped (``technical_read_dropped`` is falsey).
       Then each mate adopts the ENA identity: ``sha256 = content_key_from_md5(md5)``,
       ``size_bytes = fastq_bytes``, ``basename =`` the ENA filename — so a stream and a URL/ENA download
       of that file get the same address and a portable ``dataset_hash``.
    2. **Otherwise** (SRA-only, a BAM original, or a mirror that dropped a read) — a synthetic
       :func:`content_key_from_sra` over stable whole-run metadata, an ``N``-invariant address that is
       not the hosted-byte identity; ``basename = <SRR>_<index>.fastq.gz`` (the sra-tools split layout).
    """
    run_accession = (run.get("run_accession") or "").strip()
    if not run_accession:
        raise RemoteError("probe_sra: run has no 'run_accession'.")

    preview = _stream_run(run_accession, n_spots=n_reads)
    mates = preview.read_indexes()
    if not mates:
        raise RemoteError(
            f"{run_accession}: streamed no reads (empty run, or the run is unavailable)."
        )

    # Whole-run spot count for the synthetic address / size proxy — N-invariant when ENA reports it;
    # the preview count is a last-resort fallback (then the SRA address is only as stable as N).
    spot_count = int(run.get("read_count") or 0) or preview.n_spots_returned

    ena_targets = fastq_targets_meta(run)
    # A faithful mirror is a file per streamed mate AND no bases the archive lost on the way out. The
    # second half arrives on the run row when a caller already paid for it; when nobody did, the
    # stream answers it for free rather than costing a request per run.
    verified = (
        bool(ena_targets)
        and len(ena_targets) == len(mates)
        and not run.get("technical_read_dropped")
        and dropped_reads(run, _streamed_read_table(run_accession, preview, mates, spot_count))
        is None
    )

    probes: list[SraMateProbe] = []
    for pos, index in enumerate(mates):
        records: list[Record] = [
            (rec.header, rec.seq, rec.plus, rec.qual) for rec in preview.reads[index]
        ]
        read_length = preview.read_lengths[index]
        file = sra_whole_file(
            run_accession,
            index,
            ena=ena_targets[pos] if verified else None,
            spot_count=spot_count,
            read_length=read_length,
        )
        observation, seqs = _observe_records(records, file, Budget(n_reads, max_bytes))
        probes.append(
            SraMateProbe(
                read_index=index,
                observation=observation,
                seqs=seqs,
                records=records,
                basename=file.basename,
                ena_verified=verified,
            )
        )
    return probes


#: How many experiments a refusal names before it summarises the rest. A plate deposit has thousands
#: of SRX, and an error string that prints all of them is not an error string anybody reads.
_REFUSAL_LISTING_LIMIT = 8


def _experiment_listing(by_srx: dict[str, list[dict[str, Any]]]) -> str:
    """``SRX (n runs); …`` for a refusal, summarised past :data:`_REFUSAL_LISTING_LIMIT`."""
    items = sorted(by_srx.items())
    shown = "; ".join(
        f"{srx} ({len(rs)} run{'s' if len(rs) != 1 else ''})"
        for srx, rs in items[:_REFUSAL_LISTING_LIMIT]
    )
    remaining = len(items) - _REFUSAL_LISTING_LIMIT
    return f"{shown}; and {remaining} more" if remaining > 0 else shown


def resolve_package_runs(accession: str, *, multi_experiment: bool = False) -> list[dict[str, Any]]:
    """Resolve an accession to the runs one fingerprint package will hold.

    **One package is one library, and that is still the default.** A run (``SRR``) or experiment
    (``SRX``) resolves to a single experiment and passes; a project/series that mixes experiments —
    like GSE283483's bulk RNA + Multiome GEX + Multiome ATAC — is refused with the ``SRX`` to pick
    from, because collapsing three modalities into one package is exactly the mistake this guards
    against.

    ``multi_experiment`` is the caller *asserting* that the spanned experiments are nonetheless one
    library, and nothing the data says relaxes the guard on its own. A plate deposit is the case that
    needs it: every cell is its own ``SRX``, so PRJNA853582's 1440 experiments are one plate-based
    library, and no single-``SRX`` package can express the many-cell shape a benchmark case exists to
    exercise. The package format never was the limit — a package already spans several ``fastq/``
    subtrees — only this resolver was.

    The inventory is fetched **alone** (``check_reads=False``). The per-read table costs one request
    per run, and nothing on this path reads it: grouping needs ``experiment_accession`` only, and the
    mirror-faithfulness question it used to answer is answered from the stream in :func:`probe_sra`.
    Checking it here meant a 1440-run study spent 1440 round-trips to reach a refusal.
    """
    result = resolve_accession(accession, check_reads=False)
    runs: list[dict[str, Any]] = result["runs"]
    by_srx: dict[str, list[dict[str, Any]]] = {}
    for run in runs:
        srx = (run.get("experiment_accession") or "").strip() or "?"
        by_srx.setdefault(srx, []).append(run)
    if len(by_srx) > 1 and not multi_experiment:
        raise RemoteError(
            f"{accession} spans {len(by_srx)} experiments — a fingerprint package is one library. "
            "Re-run --accession with a single experiment (SRX) or run (SRR), or pass "
            "--multi-experiment to assert that these experiments ARE one library (a plate deposit "
            f"gives every cell its own SRX). Experiments: {_experiment_listing(by_srx)}"
        )
    return runs


def _slug_for(runs: Sequence[dict[str, Any]], name: str | None) -> str:
    """A human slug for the package: the caller's ``--name``, else the shared SRX, else the shared
    study, else a run acc."""
    if name:
        return name
    srxs = {(r.get("experiment_accession") or "").strip() for r in runs} - {""}
    if len(srxs) == 1:
        return next(iter(srxs))
    # A multi-experiment package has no one SRX to be named after; the study every run came from is
    # the next thing they share, and it is what a plate deposit's package should say it is.
    studies = {(r.get("study_accession") or "").strip() for r in runs} - {""}
    if len(studies) == 1:
        return next(iter(studies))
    accs = [(r.get("run_accession") or "").strip() for r in runs if r.get("run_accession")]
    return accs[0] if len(accs) == 1 else "dataset"


def build_fingerprint_sra(
    runs: Sequence[dict[str, Any]],
    *,
    workspace: str | Path = ".",
    reads: int = DEFAULT_MAX_READS,
    max_bytes: int = DEFAULT_MAX_BYTES,
    info_docs: Sequence[str | Path] | None = None,
    name: str | None = None,
    include_raw: bool = True,
) -> FingerprintResult:
    """Build a fingerprint package from an SRA stream — the no-download twin of ``build_fingerprint``.

    Streams and fingerprints every mate of every run (:func:`probe_sra`), stages each mate's slice under
    ``fastq/<basename>`` with its pinned identity, and hands the pins + slices to the shared
    ``assemble_package`` seam — so the package is byte-identical to one a local ``preflight`` would build
    for the same content, and loads and reproduces through the unchanged ``run --fingerprint`` path.
    """
    if not runs:
        raise RemoteError("build_fingerprint_sra: no runs to fingerprint.")
    pins: list[FilePin] = []
    staged: list[tuple[str, list[Record]]] = []
    for run in runs:
        for mate in probe_sra(run, n_reads=reads, max_bytes=max_bytes):
            pkg_rel = str(Path("fastq") / mate.basename)
            obs = mate.observation
            pins.append(
                FilePin(
                    rel_path=pkg_rel,
                    basename=mate.basename,
                    sha256=obs.file.sha256,
                    size_bytes=obs.file.size_bytes,
                    isize=None,
                    reads_written=len(mate.records),
                    estimated_total_reads=obs.estimated_total_reads,
                    # Why this slice ends, under the same rule the local producer uses: the verdict of
                    # the read that cut it. Here that read is over bytes this process serialized from
                    # already-parsed spots, so it is clean — the rule is shared, not the answer.
                    gzip=obs.gzip,
                )
            )
            staged.append((pkg_rel, mate.records))
    return assemble_package(
        _slug_for(runs, name),
        pins,
        staged,
        workspace=workspace,
        reads=reads,
        max_bytes=max_bytes,
        info_docs=info_docs,
        include_raw=include_raw,
    )


__all__ = [
    "SraMateProbe",
    "build_fingerprint_sra",
    "probe_sra",
    "resolve_package_runs",
]
