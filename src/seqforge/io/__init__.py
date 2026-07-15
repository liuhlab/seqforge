"""``io`` — the only network + onlist surface (pooch-cached, sha256-verified).

Milestone 0 ships the **onlist registry** (``resolve``'s Tier B needs it): width-generic packed
whitelists, a hit-rate test (forward + reverse-complement + offset scan), set-intersection for the
confusability check, and a synthetic-onlist path for the pilot fixtures. ``io peek`` / ``io resolve``
(remote range-reads and accession resolution) are declared stubs — see :mod:`seqforge.io.remote`.
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
from .remote import NotYetImplemented, peek, resolve_accession

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
    # remote stubs
    "peek",
    "resolve_accession",
    "NotYetImplemented",
]
