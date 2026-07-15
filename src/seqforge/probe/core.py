"""``probe_file`` — orchestrate bounded streaming + Tier A signals into an :class:`Observation`."""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Literal

from ..models.observation import (
    FileIdentity,
    GzipIntegrity,
    Observation,
    ProbeProvenance,
)
from . import DEFAULT_MAX_BYTES, DEFAULT_MAX_READS, PROBE_VERSION
from . import signals as sig
from .streaming import sample_fastq_gz

_HASH_CHUNK = 1 << 20  # 1 MiB


def hash_file(path: str | Path) -> str:
    """Return the sha256 of a file's bytes (content-addressing, R7).

    Note
    ----
    This reads the whole *compressed* file (constant memory, no decompression). The R3 bounded-work
    invariant governs the *decompressed parse* — :func:`~seqforge.probe.streaming.sample_fastq_gz` —
    which is the expensive path. At 10^4-dataset scale the content sha256 is expected to come from
    ``io resolve`` (provider md5/sha) so even this linear scan is avoided; the pilot's synthetic
    fixtures are tiny, so hashing them here is free.
    """
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        while chunk := fh.read(_HASH_CHUNK):
            h.update(chunk)
    return h.hexdigest()


def _params_hash(max_reads: int, max_bytes: int) -> str:
    return hashlib.sha256(f"{max_reads}:{max_bytes}".encode()).hexdigest()[:16]


def _gzip_isize(path: Path) -> int | None:
    """The gzip ISIZE trailer: uncompressed size mod 2^32 (O(1); unreliable for >4GB / multi-member)."""
    try:
        with open(path, "rb") as fh:
            fh.seek(-4, 2)
            return int.from_bytes(fh.read(4), "little")
    except OSError:
        return None


def _estimate_reads(
    path: Path,
    file_size: int,
    n_reads: int,
    decompressed_bytes: int,
    compressed_bytes: int,
    budget_exhausted: bool,
) -> tuple[int, Literal["isize", "compressed_ratio"]]:
    """Extrapolate total reads without reading the whole file.

    Prefer the gzip ISIZE trailer (uncompressed size / average record size); fall back to the
    compressed-size ratio. If the whole (small) file was read, the sampled count is exact.
    """
    if n_reads == 0:
        return 0, "compressed_ratio"
    if not budget_exhausted:
        return n_reads, "isize"  # read to EOF: the count is exact
    avg_record = decompressed_bytes / n_reads
    isize = _gzip_isize(path)
    if isize is not None and avg_record > 0 and isize > decompressed_bytes:
        return int(isize / avg_record), "isize"
    if compressed_bytes > 0:
        return int(file_size * n_reads / compressed_bytes), "compressed_ratio"
    return n_reads, "compressed_ratio"


def probe_sample(
    path: str | Path,
    *,
    max_reads: int = DEFAULT_MAX_READS,
    max_bytes: int = DEFAULT_MAX_BYTES,
    sha256: str | None = None,
) -> tuple[Observation, list[str]]:
    """Fingerprint one FASTQ gzip and ALSO return its bounded sampled sequences.

    :class:`Observation` is structural + role-free and cached to disk; the raw sampled ``seqs`` are
    the same bounded, in-memory sample used to build it. ``resolve`` needs those seqs to answer
    role-conditioned distinct-ratio / onlist-hit-rate over arbitrary windows (a ``WindowProbe``),
    which the structural Observation deliberately does not carry. The sample stays within the R3
    budget — this returns it, it does not re-read the file.
    """
    p = Path(path)
    sample = sample_fastq_gz(p, max_reads=max_reads, max_bytes=max_bytes)

    comps = sig.per_cycle_composition(sample.seqs)
    segments = sig.segment(comps)
    read_length = sig.read_length_profile(sample.seqs)
    windows = sig.distinct_ratios(sample.seqs, segments)
    read_name = sig.parse_read_name(sample.first_name)
    quality = sig.quality_encoding(sample.qual_min_ord, sample.qual_max_ord)
    nrate = sig.n_rate(sample.seqs)

    file_size = p.stat().st_size
    estimated_total, est_method = _estimate_reads(
        p,
        file_size,
        sample.n_reads,
        sample.decompressed_bytes,
        sample.compressed_bytes,
        sample.budget_exhausted,
    )

    observation = Observation(
        file=FileIdentity(
            sha256=sha256 or hash_file(p),
            size_bytes=file_size,
            basename=p.name,
            local_uri=str(p),
        ),
        probe=ProbeProvenance(
            n_reads_sampled=sample.n_reads,
            bytes_read=sample.decompressed_bytes,
            compressed_bytes_read=sample.compressed_bytes,
            tool_version=PROBE_VERSION,
            params_hash=_params_hash(max_reads, max_bytes),
        ),
        per_cycle_composition=comps,
        segments=segments,
        read_length=read_length,
        distinct_value_windows=windows,
        read_name=read_name,
        quality_encoding=quality,
        n_rate=nrate,
        estimated_total_reads=estimated_total,
        est_method=est_method,
        gzip=GzipIntegrity(ok=sample.ok, truncated=sample.truncated),
    )
    return observation, sample.seqs


def probe_file(
    path: str | Path,
    *,
    max_reads: int = DEFAULT_MAX_READS,
    max_bytes: int = DEFAULT_MAX_BYTES,
    sha256: str | None = None,
) -> Observation:
    """Fingerprint one FASTQ gzip into a role-free :class:`Observation` under a bounded budget (R3).

    Parameters
    ----------
    path
        Local path to a gzip-compressed FASTQ.
    max_reads, max_bytes
        The read budget and decompressed-byte cap (R3).
    sha256
        Precomputed content hash; if omitted it is computed from the file bytes (see :func:`hash_file`).
    """
    observation, _seqs = probe_sample(path, max_reads=max_reads, max_bytes=max_bytes, sha256=sha256)
    return observation
