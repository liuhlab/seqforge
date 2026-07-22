"""``io`` — the only network + onlist surface (pooch-cached, sha256-verified).

Milestone 0 ships the **onlist registry** (``resolve``'s Tier B needs it): width-generic packed
whitelists, a hit-rate test (forward + reverse-complement + offset scan), set-intersection for the
confusability check, and a synthetic-onlist path for the pilot fixtures. The remote surface is live:
``resolve_accession`` (accession -> run inventory), ``peek`` (bounded range-read diagnostic), and
``probe_remote`` (range-read -> :class:`~seqforge.models.observation.Observation`, so a library is
fingerprinted straight from a URL with no local file, issue #39) — see :mod:`seqforge.io.remote`.
"""

from __future__ import annotations

from .onlist import (
    DEFAULT_REGISTRY,
    HitResult,
    OnlistNotAvailable,
    OnlistRegistry,
    Orientation,
    PackedOnlist,
    RegistryEntry,
    default_registry,
    intersect_fraction,
    onlist_hit_rate,
    pack_barcode,
    revcomp,
    synthetic_registry,
)
from .remote import (
    NotYetImplemented,
    fastq_targets,
    peek,
    probe_remote,
    resolve_accession,
)

#: CalVer YYYY.M.PATCH; bump when onlist packing / registry semantics change.
IO_VERSION = "2026.7.0"

__all__ = [
    "IO_VERSION",
    # onlist registry + packing
    "OnlistRegistry",
    "RegistryEntry",
    "PackedOnlist",
    "HitResult",
    "Orientation",
    "OnlistNotAvailable",
    "onlist_hit_rate",
    "intersect_fraction",
    "pack_barcode",
    "revcomp",
    "synthetic_registry",
    "default_registry",
    "DEFAULT_REGISTRY",
    # remote range-read + accession resolution
    "peek",
    "probe_remote",
    "fastq_targets",
    "resolve_accession",
    "NotYetImplemented",
]
