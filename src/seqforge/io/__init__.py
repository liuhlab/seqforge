"""``io`` — the network and onlist surface (pooch-cached, sha256-verified).

- :mod:`.onlist` — the **onlist registry** (``resolve``'s Tier B needs it): width-generic packed
  whitelists, a hit-rate test (forward + reverse-complement + offset scan), set-intersection for the
  confusability check, and a synthetic-onlist path for the pilot fixtures.
- :mod:`.remote` — ``resolve_accession`` (accession -> run inventory), ``peek`` (bounded range-read
  diagnostic), and ``probe_remote`` (range-read -> :class:`~seqforge.models.observation.Observation`,
  so a library is fingerprinted straight from a URL with no local file).
- :mod:`.sra` — ``io probe-sra``: fingerprint an SRA run from its first spots, with no download.
- :mod:`.archive` — ``io records``: what a public archive declares about a dataset, at all four
  levels.
- :mod:`.taxonomy` — an organism name -> its NCBI taxid, with a round-trip check.
- :mod:`.benchmark` — pull and publish the benchmark corpus's fingerprint packages.
"""

from __future__ import annotations

from .benchmark import (
    HF_BENCHMARK_REPO,
    HF_PACKAGE_PREFIX,
    BenchmarkPackageAbsent,
    BenchmarkPackageUnavailable,
    default_package_path,
    fetch_benchmark_package,
    hf_package_url,
    publish_benchmark_package,
)
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

#: CalVer YYYY.M.PATCH; bump when onlist packing, registry semantics or what a record set holds
#: change. History: `docs/research/io-version-history-2026-11-25.md`.
IO_VERSION = "2026.11.1"

__all__ = [  # noqa: RUF022 - grouped by concept under comments, not alphabetical
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
    # HF benchmark: anonymous fetch (opt-in / networked eval job) + the maintainer's publish
    "HF_BENCHMARK_REPO",
    "HF_PACKAGE_PREFIX",
    "BenchmarkPackageAbsent",
    "BenchmarkPackageUnavailable",
    "default_package_path",
    "fetch_benchmark_package",
    "hf_package_url",
    "publish_benchmark_package",
]
