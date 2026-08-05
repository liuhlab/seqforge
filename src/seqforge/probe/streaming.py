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
import zlib
from collections.abc import Generator
from dataclasses import dataclass, field
from pathlib import Path
from typing import IO

from . import DEFAULT_MAX_BYTES, DEFAULT_MAX_READS

#: One FASTQ record as four raw lines with trailing newlines stripped: (header, seq, plus, qual).
#: Kept as bytes rather than decoded ``str`` so headers and qualities survive byte-for-byte — a
#: fingerprint slice writes them back out, and the probe's own signals must match the original.
Record = tuple[bytes, bytes, bytes, bytes]


@dataclass(frozen=True)
class Budget:
    """The pair that bounds a head: ``max_reads`` records and ``max_bytes`` *decompressed* bytes.

    One value rather than two ints threaded side by side, because they are never meaningful apart —
    the budget is two-part and whichever trips first stops the read, so a function holding one bound
    and not the other cannot enforce it.
    ``CONTEXT.md`` has named this concept **Budget** since the glossary was written; this is the type.

    A head carries the budget it was actually read under (:attr:`FastqHead.budget`), which is what
    lets ``params_hash`` be *derived from the read* rather than recomputed from parameters a caller
    supplies a second time. Wall-clock is never a budget.
    """

    max_reads: int = DEFAULT_MAX_READS
    max_bytes: int = DEFAULT_MAX_BYTES


class BoundedReader:
    """Iterate the records of a gzip FASTQ head, stopping at whichever budget trips first.

    Source-agnostic: ``fileobj`` is any binary reader positioned at the gzip magic — a local file, an
    in-memory range-read head (``io.remote.probe_remote``), or re-serialized SRA records
    (``io.sra``). A range-read head that ends mid-member is the normal remote case, and it takes the
    same path as a truncated upload: the cut is caught and the trailing partial record dropped.

    **Single-use, and accounting is valid only once exhausted.** The counters below are filled as the
    iteration advances, so read them *after* the loop, not during and not before. ``compressed_bytes``
    is the reader's final position, taken when iteration finishes; the caller owns opening and closing
    ``fileobj``.

    **A read the caller stopped may have no byte count at all** (:attr:`abandoned`). Nothing may read
    ``compressed_bytes`` as a measurement without checking it — an abandoned read's count is *absent*,
    and absent is spelled here as a flag beside a zero rather than as a zero, because a measured zero
    is a real and different answer.

    Parameters
    ----------
    fileobj
        A binary stream of gzip-compressed FASTQ bytes (a whole file, or a bounded head).
    budget
        The :class:`Budget` that bounds the read. Whichever half trips first stops the stream.

    Attributes
    ----------
    n_reads, decompressed_bytes, compressed_bytes
        The byte/record accounting that proves the read stayed in budget.
    truncated
        The gzip stream **ended early**: it stopped mid-member, or its last record is short of four
        lines, before either budget or a clean EOF. The bytes present were readable.
    ok
        False when the stream is **not readable gzip**: a header that does not parse, a corrupt
        deflate payload, a CRC that disagrees with what came out.
    abandoned
        The iteration was **stopped by its caller and its position could never be taken**, because
        the handle was already closed when this generator was finalised. A verdict about the *read*
        rather than about the stream, which is what separates it from the two above — and it is not
        ``budget_exhausted``, which names the opposite case: a read that finished, on a bound.
        ``compressed_bytes`` is meaningless while it is true.

    ``truncated`` and ``ok`` are different verdicts and never both true, because they carry different
    remedies — a cut upload is re-downloaded, a corrupt file is re-examined for whether it was ever a
    FASTQ — and
    ``resolve`` raises a different ``Blocker`` for each. Which one applies is decided by *what the
    decompressor raised*, not by a heuristic over the record count: ``EOFError`` means the bytes ran
    out, anything else means they did not say what they claimed. They used to collapse into
    ``truncated`` for both, leaving ``ok`` unreachable (issue #94).
    """

    def __init__(self, fileobj: IO[bytes], budget: Budget) -> None:
        self._fileobj = fileobj
        self.budget = budget
        self.n_reads = 0
        self.decompressed_bytes = 0
        self.compressed_bytes = 0
        self.truncated = False
        self.ok = True
        self.abandoned = False

    @property
    def budget_exhausted(self) -> bool:
        """Did a budget stop the read, rather than a clean EOF?"""
        return (
            self.n_reads >= self.budget.max_reads
            or self.decompressed_bytes >= self.budget.max_bytes
        )

    def __iter__(self) -> Generator[Record, None, None]:
        """The records, as a GENERATOR — `close()` is part of the contract, not an accident.

        Spelled as one rather than as an `Iterator`, because a caller that stops early has to be
        able to finalise this deterministically: `contextlib.closing` around it is what decides
        whether the read ends measured or **abandoned** (see the class docstring).
        """
        max_reads, max_bytes = self.budget.max_reads, self.budget.max_bytes
        try:
            with gzip.GzipFile(fileobj=self._fileobj) as gz:
                line_iter = iter(gz)
                while self.n_reads < max_reads and self.decompressed_bytes < max_bytes:
                    try:
                        header = next(line_iter, None)
                        if header is None:  # clean EOF, fewer records than the budget
                            break
                        seq = next(line_iter, None)
                        plus = next(line_iter, None)
                        qual = next(line_iter, None)
                    except EOFError:
                        # The bytes ran out mid-member: a truncated upload, or — the routine case —
                        # a bounded range-read head that was never meant to reach the end.
                        self.truncated = True
                        break
                    except (zlib.error, OSError):
                        # Not readable gzip. `BadGzipFile` IS an `OSError` (bad magic, a header that
                        # does not parse, a failed CRC); `zlib.error` is not one, and a corrupt
                        # deflate payload raises it — uncaught, it escaped into the caller and killed
                        # the probe instead of producing the refusal (issue #94).
                        self.ok = False
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
        except (zlib.error, OSError):
            self.ok = False  # the same verdict, for a stream that failed before the first record
        finally:
            try:
                self.compressed_bytes = self._fileobj.tell()
            except ValueError:
                # The handle is gone before this generator was finalised, so the position cannot be
                # taken: an **Abandoned read**. It happens to any caller that stops mid-stream and
                # closes its files before the generator is collected — a mid-loop refusal does
                # exactly that — and the finalisation then runs inside `GeneratorExit`, where a
                # raise is UNRAISABLE: it prints at some later, unrelated moment and is ignored.
                # Recorded as a flag instead, because the alternative repair — swallow it and leave
                # `compressed_bytes` at 0 — makes an absent measurement indistinguishable from a
                # measured zero, in the accounting that proves the read stayed in budget.
                # `ValueError` and not `Exception`: a closed handle raises exactly that, and
                # `io.UnsupportedOperation` (a stream with no `tell`) is one too.
                self.abandoned = True


@dataclass
class FastqHead:
    """What the probe keeps from one bounded head: sequences, first header, quality range, accounting.

    The signals a Tier-A observation is built from, and nothing else — headers and qualities are
    reduced to ``first_name`` and an ordinal range rather than retained, which is what keeps a probe
    cheap enough to run across a fork pool. A fingerprint needs the records themselves and so
    accumulates a ``RecordSlice`` over the same :class:`BoundedReader` instead.

    Two fields describe **the read rather than the records**: ``budget`` (what bounded it) and
    ``source_path`` (the local file it came from, ``None`` for a stream). They are here because a head
    is the only thing that knows them first-hand — ``build_observation`` derives ``params_hash`` and
    ``local_uri`` from them instead of being told, which is what makes those two unable to disagree
    with the read that actually happened. Everything a head does *not* know — the whole file's
    content address, name, and size — is a :class:`~seqforge.probe.core.WholeFile`.
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
    #: The budget this head was read under — not a budget someone would like it to have had.
    budget: Budget = field(default_factory=Budget)
    #: The local path read, or ``None`` for an in-memory stream (a range read, an SRA re-serialization).
    #: Becomes ``FileIdentity.local_uri``: "where the bytes were", never "which file this describes" —
    #: a fingerprint slice sets this to the slice while its ``WholeFile`` describes the original.
    source_path: Path | None = None

    @classmethod
    def read(
        cls, fileobj: IO[bytes], budget: Budget | None = None, *, source_path: Path | None = None
    ) -> FastqHead:
        """Accumulate a head from any binary gzip-FASTQ stream."""
        reader = BoundedReader(fileobj, budget or Budget())
        head = cls(budget=reader.budget, source_path=source_path)
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
    def from_path(cls, path: str | Path, budget: Budget | None = None) -> FastqHead:
        """Accumulate a head from a LOCAL gzip FASTQ, remembering which file it was.

        ``gzip.GzipFile`` does not close a ``fileobj`` it was handed, so the reader's ``tell()`` runs
        before this ``close()``.
        """
        p = Path(path)
        raw = open(p, "rb")  # noqa: SIM115 - closed explicitly in finally
        try:
            return cls.read(raw, budget, source_path=p)
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
