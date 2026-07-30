"""Bounded FASTQ reading — the bounded-read invariant made mechanical.

Decompress a gzip FASTQ incrementally and stop at whichever budget trips first: ``max_reads`` records
or ``max_bytes`` *decompressed* bytes. There is no random-access seek plan and no whole-file
decompression; a code path that can touch a whole multi-GB FASTQ is a bug.

:class:`BoundedReader` is the **one** loop that enforces that. Everything that reads a FASTQ in this
project iterates it; nothing writes a second budget loop. Two accumulations sit on top, differing only
in what they *retain* — :class:`FastqHead` keeps the signals the probe needs, and
``fingerprint.subsample.RecordSlice`` keeps the records verbatim so a slice can be written back out.
They used to be two hand-synchronised copies of this loop, which is a drift hazard when the property
that matters is that a slice cut at ``max_reads = N`` holds exactly the records a probe with the same
budget consumes.

``Record`` lives here rather than beside the slicer because the reader is what produces one, and both
``fingerprint`` and ``io`` already depend on ``probe``.
"""

from __future__ import annotations

import gzip
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import IO

#: One FASTQ record as four raw lines with trailing newlines stripped: (header, seq, plus, qual).
#: Kept as bytes rather than decoded ``str`` so headers and qualities survive byte-for-byte — a
#: fingerprint slice writes them back out, and the probe's own signals must match the original.
Record = tuple[bytes, bytes, bytes, bytes]


class BoundedReader:
    """Iterate the records of a gzip FASTQ head, stopping at whichever budget trips first.

    Source-agnostic: ``fileobj`` is any binary reader positioned at the gzip magic — a local file, an
    in-memory range-read *prefix* (``io.remote.probe_remote``), or re-serialized SRA records
    (``io.sra``). A prefix that ends mid-member is the normal remote case, and it takes the same path
    as a truncated upload: the cut is caught and the trailing partial record dropped.

    **Single-use, and accounting is valid only once exhausted.** The counters below are filled as the
    iteration advances, so read them *after* the loop, not during and not before. ``compressed_bytes``
    is the reader's final position, taken when iteration finishes; the caller owns opening and closing
    ``fileobj``.

    Parameters
    ----------
    fileobj
        A binary stream of gzip-compressed FASTQ bytes (a whole file, or a bounded head prefix).
    max_reads
        Hard cap on records read.
    max_bytes
        Hard cap on *decompressed* bytes read. Whichever cap trips first stops the stream.

    Attributes
    ----------
    n_reads, decompressed_bytes, compressed_bytes
        The byte/record accounting that proves the read stayed in budget.
    truncated
        The gzip stream ended mid-member before either budget or a clean EOF.
    ok
        False on a gzip/format error.
    """

    def __init__(self, fileobj: IO[bytes], max_reads: int, max_bytes: int) -> None:
        self._fileobj = fileobj
        self._max_reads = max_reads
        self._max_bytes = max_bytes
        self.n_reads = 0
        self.decompressed_bytes = 0
        self.compressed_bytes = 0
        self.truncated = False
        self.ok = True

    @property
    def budget_exhausted(self) -> bool:
        """Did a budget stop the read, rather than a clean EOF?"""
        return self.n_reads >= self._max_reads or self.decompressed_bytes >= self._max_bytes

    def __iter__(self) -> Iterator[Record]:
        try:
            with gzip.GzipFile(fileobj=self._fileobj) as gz:
                line_iter = iter(gz)
                while self.n_reads < self._max_reads and self.decompressed_bytes < self._max_bytes:
                    try:
                        header = next(line_iter, None)
                        if header is None:  # clean EOF, fewer records than the budget
                            break
                        seq = next(line_iter, None)
                        plus = next(line_iter, None)
                        qual = next(line_iter, None)
                    except (EOFError, gzip.BadGzipFile, OSError):
                        # cut mid-member (truncated upload, or a bounded range-read prefix).
                        self.truncated = True
                        break
                    if seq is None or plus is None or qual is None:
                        self.truncated = True  # a partial final record => cut mid-record
                        break
                    # Counted before the yield, on the RAW lines including their newlines, so the
                    # budget check at the top of the next iteration sees this record's cost.
                    self.n_reads += 1
                    self.decompressed_bytes += len(header) + len(seq) + len(plus) + len(qual)
                    yield (
                        header.rstrip(b"\n"),
                        seq.rstrip(b"\n"),
                        plus.rstrip(b"\n"),
                        qual.rstrip(b"\n"),
                    )
        except (gzip.BadGzipFile, OSError):
            self.ok = False
        finally:
            self.compressed_bytes = self._fileobj.tell()


@dataclass
class FastqHead:
    """What the probe keeps from one bounded head: sequences, first header, quality range, accounting.

    The signals a Tier-A observation is built from, and nothing else — headers and qualities are
    reduced to ``first_name`` and an ordinal range rather than retained, which is what keeps a probe
    cheap enough to run across a fork pool. A fingerprint needs the records themselves and so
    accumulates a ``RecordSlice`` over the same :class:`BoundedReader` instead.
    """

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

    @classmethod
    def read(cls, fileobj: IO[bytes], max_reads: int, max_bytes: int) -> FastqHead:
        """Accumulate a head from any binary gzip-FASTQ stream."""
        reader = BoundedReader(fileobj, max_reads, max_bytes)
        head = cls()
        for header, seq, _plus, qual in reader:
            if head.first_name is None:
                head.first_name = header.decode("ascii", "replace").lstrip("@")
            head.seqs.append(seq.decode("ascii", "replace"))
            head._observe_qual(qual.decode("ascii", "replace"))
        head.n_reads = reader.n_reads
        head.decompressed_bytes = reader.decompressed_bytes
        head.compressed_bytes = reader.compressed_bytes
        head.truncated = reader.truncated
        head.ok = reader.ok
        head.budget_exhausted = reader.budget_exhausted
        return head

    @classmethod
    def from_path(cls, path: str | Path, max_reads: int, max_bytes: int) -> FastqHead:
        """Accumulate a head from a LOCAL gzip FASTQ.

        ``gzip.GzipFile`` does not close a ``fileobj`` it was handed, so the reader's ``tell()`` runs
        before this ``close()``.
        """
        raw = open(path, "rb")  # noqa: SIM115 - closed explicitly in finally
        try:
            return cls.read(raw, max_reads, max_bytes)
        finally:
            raw.close()

    def _observe_qual(self, qual: str) -> None:
        """Track the min/max quality-char ordinal (used to infer the Phred offset)."""
        if not qual:
            return
        ords = [ord(c) for c in qual]
        lo, hi = min(ords), max(ords)
        self.qual_min_ord = lo if self.qual_min_ord is None else min(self.qual_min_ord, lo)
        self.qual_max_ord = hi if self.qual_max_ord is None else max(self.qual_max_ord, hi)
