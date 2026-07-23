"""``probe`` — deterministic, bounded FASTQ fingerprinting (no LLM, no network).

Turns bytes into an :class:`~seqforge.models.Observation` from a bounded, head-limited decompressed
stream. Every touch is bounded by a read budget (``--max-reads``, default 200 000) and a byte cap
(``--max-bytes``, default 256 MB decompressed); wall-clock is never a budget, and a code path that
*can* stream a whole multi-GB FASTQ is a bug.

Tier A (this module) computes structural signals with no KB: per-cycle base composition, segmentation
(constant/random/homopolymer), read-length profile, distinct-value ratios, read-name grammar,
N-rate, quality encoding, gzip integrity, and an extrapolated read-count estimate. It assigns **no
roles** — that interpretation belongs to ``resolve``.
"""

from __future__ import annotations

#: CalVer YYYY.M.PATCH, bumped whenever probe output semantics change; folded into the Observation
#: cache key. Component/tool-stamp versions use CalVer just like the package version.
#: 2026.7.1 — content-address from a bounded local key (head + size + gzip ISIZE), not a whole-file
#: sha256 (issue #37); the file identity string changes, so cached observations recompute once.
#: 2026.7.2 — DEFAULT_MAX_READS 200_000 -> 2_000 (issue #63). The resolved chemistry is invariant
#: from 1k to 200k reads across every benchmarked worm library, so the default samples 100x fewer
#: reads. This changes observation values (n_reads_sampled, per-cycle composition, the read-count
#: estimate), hence a probe-version bump: pinned manifests re-hash through their observation values.
PROBE_VERSION = "2026.7.2"

#: Default bounded-read budget: 2_000 reads. The benchmarked N-invariant floor is <=1k across every
#: chemistry (issue #63); 2k is a deliberate 2x cushion, ~100x cheaper than the old arbitrary
#: 200_000. Fingerprint-based probe on these small slices is the routine path; a caller that wants
#: to read more of a full-size FASTQ passes a larger --max-reads (the explicit "use the whole file"
#: opt-in). Every touch stays bounded by this AND --max-bytes — raising one never unbounds the other.
DEFAULT_MAX_READS = 2_000

#: Default decompressed-byte cap. Whichever budget trips first stops the stream.
DEFAULT_MAX_BYTES = 256 * 1024 * 1024

# Imported last: core depends on the budget constants above (keeps the package import acyclic).
from .core import (  # noqa: E402
    build_observation,
    content_key_from_md5,
    content_key_from_sra,
    gzip_isize,
    probe_file,
    probe_sample,
    remote_content_key,
)

__all__ = [
    "PROBE_VERSION",
    "DEFAULT_MAX_READS",
    "DEFAULT_MAX_BYTES",
    "build_observation",
    "content_key_from_md5",
    "content_key_from_sra",
    "gzip_isize",
    "remote_content_key",
    "probe_file",
    "probe_sample",
]
