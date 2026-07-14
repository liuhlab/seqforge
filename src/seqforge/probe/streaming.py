"""Bounded FASTQ streaming — the R3 invariant made mechanical.

Decompress a gzip FASTQ incrementally and stop at whichever budget trips first: ``max_reads`` records
or ``max_bytes`` *decompressed* bytes. There is no random-access seek plan and no whole-file
decompression; a code path that can touch a whole multi-GB FASTQ is a bug.
"""

from __future__ import annotations

import gzip
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class StreamSample:
    """A bounded sample of a FASTQ, plus the byte/record accounting that proves it stayed in budget."""

    seqs: list[str] = field(default_factory=list)
    first_name: str | None = None
    qual_min_ord: int | None = None
    qual_max_ord: int | None = None
    n_reads: int = 0
    decompressed_bytes: int = 0
    compressed_bytes: int = 0
    truncated: bool = False
    ok: bool = True
    budget_exhausted: bool = False


def sample_fastq_gz(path: str | Path, max_reads: int, max_bytes: int) -> StreamSample:
    """Read at most ``max_reads`` records / ``max_bytes`` decompressed bytes from a gzip FASTQ.

    Parameters
    ----------
    path
        Local path to a gzip-compressed FASTQ.
    max_reads
        Hard cap on records read (R3).
    max_bytes
        Hard cap on *decompressed* bytes read (R3). Whichever cap trips first stops the stream.

    Returns
    -------
    StreamSample
        The sampled sequences and the byte/record accounting. ``truncated`` is set if the gzip
        stream ends mid-member before either budget or a clean EOF; ``ok`` is False on a
        gzip/format error.
    """
    sample = StreamSample()
    raw = open(path, "rb")  # noqa: SIM115 - closed explicitly in finally
    try:
        with gzip.GzipFile(fileobj=raw) as gz:
            line_iter = iter(gz)
            while sample.n_reads < max_reads and sample.decompressed_bytes < max_bytes:
                try:
                    header = next(line_iter, None)
                    if header is None:  # clean EOF, fewer reads than the budget
                        break
                    seq = next(line_iter, None)
                    plus = next(line_iter, None)
                    qual = next(line_iter, None)
                except (EOFError, gzip.BadGzipFile, OSError):
                    # gzip stream cut mid-member (truncated upload); or a bad member.
                    sample.truncated = True
                    break
                if seq is None or plus is None or qual is None:
                    # a partial final record => the file was cut mid-record.
                    sample.truncated = True
                    break

                name = header.decode("ascii", "replace").rstrip("\n")
                seq_s = seq.decode("ascii", "replace").rstrip("\n")
                qual_s = qual.decode("ascii", "replace").rstrip("\n")

                if sample.first_name is None:
                    sample.first_name = name.lstrip("@")
                sample.seqs.append(seq_s)
                _update_qual_ords(sample, qual_s)
                sample.n_reads += 1
                sample.decompressed_bytes += len(header) + len(seq) + len(plus) + len(qual)
    except (gzip.BadGzipFile, OSError):
        sample.ok = False
    finally:
        sample.compressed_bytes = raw.tell()
        raw.close()

    sample.budget_exhausted = sample.n_reads >= max_reads or sample.decompressed_bytes >= max_bytes
    return sample


def _update_qual_ords(sample: StreamSample, qual: str) -> None:
    """Track the min/max quality-char ordinal (used to infer the Phred offset)."""
    if not qual:
        return
    ords = [ord(c) for c in qual]
    lo, hi = min(ords), max(ords)
    sample.qual_min_ord = lo if sample.qual_min_ord is None else min(sample.qual_min_ord, lo)
    sample.qual_max_ord = hi if sample.qual_max_ord is None else max(sample.qual_max_ord, hi)
