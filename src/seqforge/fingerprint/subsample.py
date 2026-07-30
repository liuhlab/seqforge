"""Cut a FASTQ down to its first N complete records, and re-emit reproducible gzip.

The probe's read is bounded (:mod:`seqforge.probe.streaming`) but throws most of the bytes away — its
:class:`~seqforge.probe.streaming.FastqHead` keeps only sequences, not the headers and qualities a
*valid* FASTQ record needs. A fingerprint has to write real records back out, so this accumulates the
whole record and re-emits it with the ``mtime=0`` idiom so the slice is byte-reproducible: identical
reads in, identical gzip out.

Never a whole-file read: the budget is enforced by
:class:`~seqforge.probe.streaming.BoundedReader`, the same and only loop the probe iterates. A slice
cut at ``max_reads = N`` therefore contains exactly the records a probe with the same budget consumes
— by construction, not by hand — which is what lets a fingerprint run reproduce the full-file
observation when ``N`` ≥ the probe budget.
"""

from __future__ import annotations

import gzip
import io
from dataclasses import dataclass, field
from pathlib import Path

from ..probe.streaming import BoundedReader, Record


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


def read_records(path: str | Path, max_reads: int, max_bytes: int) -> RecordSlice:
    """Read a bounded head of a LOCAL gzip FASTQ into full records.

    An accumulation over :class:`~seqforge.probe.streaming.BoundedReader` — the budget is the reader's,
    not this function's, so the record count matches a probe under the same budget by construction.
    """
    raw = open(path, "rb")  # noqa: SIM115 - closed explicitly in finally
    try:
        reader = BoundedReader(raw, max_reads, max_bytes)
        sl = RecordSlice(records=list(reader))
        sl.decompressed_bytes = reader.decompressed_bytes
        sl.truncated = reader.truncated
        sl.ok = reader.ok
        return sl
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
    "records_to_gz_bytes",
    "write_records_gz",
]
