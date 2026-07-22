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


def _content_key(basename: str, size_bytes: int, isize: int | None, seqs: list[str]) -> str:
    """Content-address a FASTQ from bounded, already-sampled data — never a whole-file read.

    A file's identity here is a *name*: stable for the same file, distinct across files. It combines
    the basename, the compressed size, the gzip ISIZE trailer (uncompressed size mod 2^32), and a hash
    of the bounded head sample — all in hand after :func:`~seqforge.probe.streaming.sample_fastq_gz`,
    so no extra bytes are read. The basename is part of the identity because a dataset's files are
    distinguished by name (``_1``/``_2``, lane, flowcell): two files with identical reads but different
    names are different files, and downstream maps (``dataset_uris``, role assignment) require one
    sha per file. The whole-file sha256 this replaces captured the name incidentally (the gzip filename
    header) and forced the entire file to be read — which was never the point (issue #37). At
    10^4-dataset scale the durable identity is the provider md5, injected via
    ``probe_sample(..., sha256=...)``.
    """
    h = hashlib.sha256()
    h.update(
        f"seqforge-content-key\x00{basename}\x00{size_bytes}\x00{isize}\x00{len(seqs)}\n".encode()
    )
    for s in seqs:
        h.update(s.encode("ascii", "replace"))
        h.update(b"\n")
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
    file_size: int,
    n_reads: int,
    decompressed_bytes: int,
    compressed_bytes: int,
    budget_exhausted: bool,
    isize: int | None,
) -> tuple[int, Literal["isize", "compressed_ratio"]]:
    """Extrapolate total reads without reading the whole file.

    Prefer the gzip ISIZE trailer (uncompressed size / average record size); fall back to the
    compressed-size ratio. If the whole (small) file was read, the sampled count is exact. ``isize``
    is read once by the caller (an O(1) seek) and shared with the content key.
    """
    if n_reads == 0:
        return 0, "compressed_ratio"
    if not budget_exhausted:
        return n_reads, "isize"  # read to EOF: the count is exact
    avg_record = decompressed_bytes / n_reads
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
    which the structural Observation deliberately does not carry. The sample stays within the
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
    isize = _gzip_isize(p)
    estimated_total, est_method = _estimate_reads(
        file_size,
        sample.n_reads,
        sample.decompressed_bytes,
        sample.compressed_bytes,
        sample.budget_exhausted,
        isize,
    )

    observation = Observation(
        file=FileIdentity(
            sha256=sha256 or _content_key(p.name, file_size, isize, sample.seqs),
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
    """Fingerprint one FASTQ gzip into a role-free :class:`Observation` under a bounded budget.

    Parameters
    ----------
    path
        Local path to a gzip-compressed FASTQ.
    max_reads, max_bytes
        The read budget and decompressed-byte cap.
    sha256
        Precomputed content identity (e.g. a provider md5); if omitted, a bounded local content key is
        derived from the head sample + size + gzip ISIZE (see :func:`_content_key`) — never a
        whole-file read.
    """
    observation, _seqs = probe_sample(path, max_reads=max_reads, max_bytes=max_bytes, sha256=sha256)
    return observation
