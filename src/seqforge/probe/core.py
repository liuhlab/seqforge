"""``probe_file`` — orchestrate bounded streaming + Tier A signals into an :class:`Observation`."""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
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
from .streaming import Budget, FastqHead


@dataclass(frozen=True)
class WholeFile:
    """What is known about a FASTQ **without reading it** — the counterpart to a head.

    A head is the bounded prefix you read; a `WholeFile` is what you read it *about*, and the two are
    not always the same file: a fingerprint slice yields a head while the `WholeFile` describes the
    original it stands in for. That is why identity is a value handed to
    :func:`build_observation` rather than something the probe derives — only the caller knows which
    file it means.

    The four fields are the whole-file facts, and nothing else. ``local_uri`` is deliberately absent:
    it says where bytes were read, not what the file is, so it belongs to the head
    (:attr:`~seqforge.probe.streaming.FastqHead.source_path`). The on-disk format already made this
    split — :class:`~seqforge.models.fingerprint.FilePin` serializes exactly these four plus the
    slice's placement in the package, and carries no ``local_uri`` either.

    There is deliberately **no single owner** for constructing one. The knowledge is irreducibly
    distributed: ``probe`` cannot know about HTTP without forfeiting its stdlib-only foundation
    status, and an SRA address needs whole-run archive metadata the probe has no business seeing. So
    each source builds its own (:func:`local_whole_file`, ``io.remote.hosted_whole_file``,
    ``io.sra.sra_whole_file``, ``FilePin.whole_file``) and probe owns only the *type*. See
    ``docs/adr/0001-head-and-wholefile.md``.
    """

    basename: str
    sha256: str
    size_bytes: int
    isize: int | None = None


def _content_key(basename: str, size_bytes: int, isize: int | None, seqs: list[str]) -> str:
    """Content-address a FASTQ from bounded, already-sampled data — never a whole-file read.

    A file's identity here is a *name*: stable for the same file, distinct across files. It combines
    the basename, the compressed size, the gzip ISIZE trailer (uncompressed size mod 2^32), and a hash
    of the bounded head — all in hand after :meth:`~seqforge.probe.streaming.FastqHead.from_path`,
    so no extra bytes are read. The basename is part of the identity because a dataset's files are
    distinguished by name (``_1``/``_2``, lane, flowcell): two files with identical reads but different
    names are different files, and downstream maps (``dataset_uris``, role assignment) require one
    sha per file. The whole-file sha256 this replaces captured the name incidentally (the gzip filename
    header) and forced the entire file to be read — which was never the point (issue #37). At
    10^4-dataset scale the durable identity is the provider md5, which is adopted where the md5 is
    known: ``io.remote.hosted_whole_file`` and ``io.sra.sra_whole_file``, never here.
    """
    h = hashlib.sha256()
    h.update(
        f"seqforge-content-key\x00{basename}\x00{size_bytes}\x00{isize}\x00{len(seqs)}\n".encode()
    )
    for s in seqs:
        h.update(s.encode("ascii", "replace"))
        h.update(b"\n")
    return h.hexdigest()


def content_key_from_md5(md5: str) -> str:
    """Derive the 64-hex content-address of a file whose PROVIDER md5 is known (issue #39).

    ENA/SRA publish a per-file md5 over the *hosted* bytes. It is 32 hex, but a
    :class:`~seqforge.models.observation.FileIdentity` ``sha256`` is a 64-hex content-address — a
    *name*, not a recomputed file hash (see :func:`_content_key`). This maps the provider md5 into that
    space injectively: identical md5 -> identical address, so two hosted files with the same md5 dedup
    correctly, and **no byte of the file is read**. Unlike the local key it carries no basename — for
    hosted bytes an identical md5 legitimately means identical content. This is the durable, machine-
    independent identity a remote probe (``io.remote.probe_remote``) stamps via ``sha256=``.
    """
    m = md5.strip().lower()
    if not re.fullmatch(r"[0-9a-f]{32}", m):
        raise ValueError(f"not a 32-hex md5: {md5!r}")
    return hashlib.sha256(f"seqforge-provider-md5\x00{m}".encode()).hexdigest()


def remote_content_key(basename: str, size_bytes: int, seqs: list[str]) -> str:
    """A bounded content key for a REMOTE file with no provider md5 — the local key minus the ISIZE.

    The gzip ISIZE trailer is unreachable without the file's tail, so a remote fingerprint passes
    ``isize=None``; otherwise this is exactly :func:`_content_key` (basename + size + head sample).
    Prefer :func:`content_key_from_md5` whenever the provider md5 is known — that is the durable
    identity that matches the hosted bytes.
    """
    return _content_key(basename, size_bytes, None, seqs)


def content_key_from_sra(
    run_accession: str, read_index: int, *, spot_count: int, read_length: int
) -> str:
    """A synthetic, N-invariant content-address for an SRA mate with no usable provider md5.

    When a run is streamed straight from the ``.sra`` (``io.sra.probe_sra``) and ENA has not mirrored
    it — or mirrored it *unfaithfully*, having dropped a technical read — there is no per-mate
    ``fastq_md5`` to adopt as the address (:func:`content_key_from_md5`). This derives one from stable
    *whole-run* metadata instead: the run accession, the within-spot read index, the run's total spot
    count, and that mate's read length. None of those depend on **how many** spots the preview streamed,
    so a fingerprint cut at N=2 000 and one cut at N=200 000 name the same file — the same N-invariance
    the byte content key has by construction. It is deliberately *not* built from the sampled sequences:
    an SRA-derived address is portable across probe budgets, but it is **not** the hosted-byte identity a
    URL/ENA download would get, so a note records that the address is SRA-derived (see ``probe_sra``).
    """
    acc = run_accession.strip().upper()
    key = f"seqforge-sra-run\x00{acc}\x00{read_index}\x00{spot_count}\x00{read_length}"
    return hashlib.sha256(key.encode()).hexdigest()


def _params_hash(budget: Budget) -> str:
    """Stamp the budget a head was read under. Takes the head's own budget, never a caller's copy."""
    return hashlib.sha256(f"{budget.max_reads}:{budget.max_bytes}".encode()).hexdigest()[:16]


def gzip_isize(path: Path) -> int | None:
    """The gzip ISIZE trailer: uncompressed size mod 2^32 (O(1); unreliable for >4GB / multi-member).

    Public because a fingerprint pin captures it: the whole-file ISIZE cannot be recovered from a
    head-slice, so ``preflight`` reads it here and stamps it back onto the stand-in probe.
    """
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


def build_observation(head: FastqHead, file: WholeFile) -> tuple[Observation, list[str]]:
    """Join a head to the file it describes. The pure, source-agnostic core of a probe.

    It runs the signal pipeline over ``head.seqs`` and stamps identity/provenance, reading no bytes
    itself. All four callers — a local probe (:func:`probe_sample`), a remote one
    (``io.remote.probe_remote``), an SRA stream (``io.sra.probe_sra``) and a fingerprint replay
    (``fingerprint.probed_from_fingerprint``) — reach it here, so a URL resolves to a library exactly
    as a local file does.

    Two arguments, and the split between them is the whole design: **the head knows what was read and
    how** (the records, the budget that bounded them, the path they came from); **the `WholeFile`
    knows what the file is** (its content address, name, size, gzip ISIZE). Nothing is passed twice,
    so ``params_hash`` and ``local_uri`` cannot contradict the read that produced them — they used to
    be re-supplied by the caller alongside a budget it merely promised it had used.

    ``file.isize`` is the gzip ISIZE trailer when reachable (a local file) and ``None`` when it is not
    (a range-read head has no tail, a stream has no file), which simply falls the read estimate back
    to the compressed-size ratio.

    The coverage figures take their denominator from ``head.n_reads`` — the same count that is
    stamped as ``probe.n_reads_sampled`` — so a coverage number and the sample size printed beside it
    can never describe different reads. They are computed from the head alone, which is what makes
    them agree across all four callers.
    """
    comps = sig.per_cycle_composition(head.seqs)
    segments = sig.segment(comps, head.n_reads)
    read_length = sig.read_length_profile(head.seqs)
    windows = sig.distinct_ratios(head.seqs, segments)
    read_name = sig.parse_read_name(head.first_name)
    quality = sig.quality_encoding(head.qual_min_ord, head.qual_max_ord)
    coverage = sig.head_coverage(comps, head.n_reads)

    estimated_total, est_method = _estimate_reads(
        file.size_bytes,
        head.n_reads,
        head.decompressed_bytes,
        head.compressed_bytes,
        head.budget_exhausted,
        file.isize,
    )

    observation = Observation(
        file=FileIdentity(
            sha256=file.sha256,
            size_bytes=file.size_bytes,
            basename=file.basename,
            local_uri=str(head.source_path) if head.source_path is not None else None,
        ),
        probe=ProbeProvenance(
            n_reads_sampled=head.n_reads,
            bytes_read=head.decompressed_bytes,
            compressed_bytes_read=head.compressed_bytes,
            tool_version=PROBE_VERSION,
            params_hash=_params_hash(head.budget),
        ),
        per_cycle_composition=comps,
        segments=segments,
        read_length=read_length,
        distinct_value_windows=windows,
        read_name=read_name,
        quality_encoding=quality,
        coverage=coverage,
        estimated_total_reads=estimated_total,
        est_method=est_method,
        gzip=GzipIntegrity(ok=head.ok, truncated=head.truncated),
    )
    return observation, head.seqs


def local_whole_file(path: Path, seqs: list[str]) -> WholeFile:
    """Name a LOCAL file from facts already in hand — no extra read beyond the head.

    The one constructor that can reach the gzip ISIZE trailer (an O(1) seek to the tail), which is
    both an input to the bounded content key and the preferred basis for the read-count estimate.
    ``seqs`` come from the head that was just read; nothing here re-opens the file to iterate it.
    """
    size_bytes = path.stat().st_size
    isize = gzip_isize(path)
    return WholeFile(
        basename=path.name,
        sha256=_content_key(path.name, size_bytes, isize, seqs),
        size_bytes=size_bytes,
        isize=isize,
    )


def probe_sample(
    path: str | Path,
    *,
    max_reads: int = DEFAULT_MAX_READS,
    max_bytes: int = DEFAULT_MAX_BYTES,
) -> tuple[Observation, list[str]]:
    """Fingerprint one LOCAL FASTQ gzip and ALSO return its bounded sampled sequences.

    :class:`Observation` is structural + role-free and cached to disk; the raw sampled ``seqs`` are
    the same bounded, in-memory head used to build it. ``resolve`` needs those seqs to answer
    role-conditioned distinct-ratio / onlist-hit-rate over arbitrary windows (a ``WindowProbe``),
    which the structural Observation deliberately does not carry. The head stays within the
    budget — this returns it, it does not re-read the file.

    ``max_reads``/``max_bytes`` stay as keywords here rather than a :class:`Budget`: this is the
    outward-facing verb the CLI drives, and the budget is an internal value of the probe.
    """
    p = Path(path)
    head = FastqHead.from_path(p, Budget(max_reads, max_bytes))
    return build_observation(head, local_whole_file(p, head.seqs))


def probe_file(
    path: str | Path,
    *,
    max_reads: int = DEFAULT_MAX_READS,
    max_bytes: int = DEFAULT_MAX_BYTES,
) -> Observation:
    """Fingerprint one FASTQ gzip into a role-free :class:`Observation` under a bounded budget.

    Parameters
    ----------
    path
        Local path to a gzip-compressed FASTQ.
    max_reads, max_bytes
        The read budget and decompressed-byte cap.

    A caller that needs a *different* identity for these bytes — a staged ENA download whose provider
    md5 should be adopted, so the staged run and a remote run share a ``dataset_hash`` — builds the
    :class:`WholeFile` itself and calls :func:`build_observation`. That is the injection path; there
    is no ``sha256=`` parameter (there was one, and in its whole lifetime nothing ever passed it).
    """
    observation, _seqs = probe_sample(path, max_reads=max_reads, max_bytes=max_bytes)
    return observation
