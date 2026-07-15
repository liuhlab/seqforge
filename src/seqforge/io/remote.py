"""Remote resolution stubs — ``io peek`` and ``io resolve`` (the only network surface, §4).

These land in a later milestone. ``io peek URI`` will range-read a remote FASTQ header without
downloading it; ``io resolve ACC`` will expand an ENA/SRA/GEO/BioProject accession into a file
inventory (including the SDL ``sra-pub-src-*`` path that recovers a dropped technical read). Both are
declared here so the CLI surface is stable and the not-yet-implemented boundary is explicit.
"""

from __future__ import annotations


class NotYetImplemented(RuntimeError):
    """A declared verb whose stage has not landed yet (distinct from a domain refusal)."""


def peek(uri: str) -> dict[str, object]:
    """Range-read a remote FASTQ's leading bytes into a partial Observation (not yet implemented)."""
    raise NotYetImplemented(
        f"io peek {uri!r}: remote range-read is not implemented yet "
        "(planned: bounded header/first-record fetch via HTTP Range / S3 GET)"
    )


def resolve_accession(accession: str) -> dict[str, object]:
    """Expand an ENA/SRA/GEO/BioProject accession into a file inventory (not yet implemented)."""
    raise NotYetImplemented(
        f"io resolve {accession!r}: accession resolution is not implemented yet "
        "(planned: ENA/SRA/GEO + SDL sra-pub-src-* for dropped technical reads)"
    )
