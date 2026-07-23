"""Cut a FASTQ down to its first N complete records, and re-emit reproducible gzip.

The probe's read is bounded (:mod:`seqforge.probe.streaming`) but throws the bytes away — its
``StreamSample`` keeps only sequences, not the headers and qualities a *valid* FASTQ record needs. A
fingerprint has to write real records back out, so this reads full 4-line records under the same two
budgets the probe honours (``max_reads`` / decompressed ``max_bytes``) and re-emits them with the
``mtime=0`` idiom so the slice is byte-reproducible: identical reads in, identical gzip out.

Never a whole-file read. The budget loop is the same shape as ``sample_fastq_stream``'s, so a slice
cut at ``max_reads = N`` contains exactly the records a probe with the same budget would consume —
which is what lets a fingerprint run reproduce the full-file observation when ``N`` ≥ the probe budget.
"""

from __future__ import annotations

import gzip
import io
from dataclasses import dataclass, field
from pathlib import Path
from typing import IO

#: One record as four raw lines with trailing newlines stripped: (header, seq, plus, qual). Kept as
#: bytes, not decoded str, so headers and qualities survive byte-for-byte (the probe's ``first_name``
#: and quality-ordinal signals must match the original for the resolve verdict to reproduce).
Record = tuple[bytes, bytes, bytes, bytes]


@dataclass
class RecordSlice:
    """The first N complete records of a FASTQ, plus the accounting that proves it stayed in budget."""

    records: list[Record] = field(default_factory=list)
    decompressed_bytes: int = 0
    truncated: bool = False
    ok: bool = True

    @property
    def n_reads(self) -> int:
        return len(self.records)


def read_records_stream(fileobj: IO[bytes], max_reads: int, max_bytes: int) -> RecordSlice:
    """Read at most ``max_reads`` complete records / ``max_bytes`` decompressed bytes from a gzip FASTQ.

    Mirrors :func:`seqforge.probe.streaming.sample_fastq_stream` line for line — same budget check at
    the top of the loop, same mid-record-cut handling — but keeps the *whole* record, not just the
    sequence. The byte accounting (``len`` of each raw line including its newline) is identical too, so
    for a given ``(max_reads, max_bytes)`` this reads exactly the record count the probe would.
    """
    sl = RecordSlice()
    try:
        with gzip.GzipFile(fileobj=fileobj) as gz:
            line_iter = iter(gz)
            while sl.n_reads < max_reads and sl.decompressed_bytes < max_bytes:
                try:
                    header = next(line_iter, None)
                    if header is None:  # clean EOF before the budget
                        break
                    seq = next(line_iter, None)
                    plus = next(line_iter, None)
                    qual = next(line_iter, None)
                except (EOFError, gzip.BadGzipFile, OSError):
                    sl.truncated = True  # stream cut mid-member
                    break
                if seq is None or plus is None or qual is None:
                    sl.truncated = True  # a partial final record
                    break
                sl.decompressed_bytes += len(header) + len(seq) + len(plus) + len(qual)
                sl.records.append(
                    (
                        header.rstrip(b"\n"),
                        seq.rstrip(b"\n"),
                        plus.rstrip(b"\n"),
                        qual.rstrip(b"\n"),
                    )
                )
    except (gzip.BadGzipFile, OSError):
        sl.ok = False
    return sl


def read_records(path: str | Path, max_reads: int, max_bytes: int) -> RecordSlice:
    """Read a bounded head of a LOCAL gzip FASTQ into full records. Thin wrapper over the stream form."""
    raw = open(path, "rb")  # noqa: SIM115 - closed explicitly in finally
    try:
        return read_records_stream(raw, max_reads, max_bytes)
    finally:
        raw.close()


def records_to_gz_bytes(records: list[Record]) -> bytes:
    """Serialize records to REPRODUCIBLE gzip bytes: same records in, same bytes out.

    The ``mtime=0`` / ``filename=""`` idiom (as in ``kb.generate.write_fastq_gz``) makes the output a
    pure function of the records, so it is byte-reproducible and content-addressable. Factored out so a
    single set of records produces *one* gzip byte string that is used both as the probe input and as
    the package slice — an SRA fingerprint (``io.sra.probe_sra``) probes exactly the bytes it stores,
    with no second serializer that could drift.
    """
    payload = b"".join(h + b"\n" + s + b"\n" + p + b"\n" + q + b"\n" for h, s, p, q in records)
    buf = io.BytesIO()
    with gzip.GzipFile(filename="", mode="wb", fileobj=buf, mtime=0) as gz:
        gz.write(payload)
    return buf.getvalue()


def write_records_gz(path: str | Path, records: list[Record]) -> None:
    """Write records to a REPRODUCIBLE ``.fastq.gz``: same records in, same bytes out.

    A thin file wrapper over :func:`records_to_gz_bytes` — the payload is the real four lines of each
    record rather than a synthesised sequence, so a fingerprint carries the original headers and
    qualities. Byte-reproducibility is what makes the whole package content-addressable and lets
    ``preflight`` run twice to an identical tar.
    """
    Path(path).write_bytes(records_to_gz_bytes(records))


__all__ = [
    "Record",
    "RecordSlice",
    "read_records",
    "read_records_stream",
    "records_to_gz_bytes",
    "write_records_gz",
]
