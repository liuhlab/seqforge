"""``probe`` — deterministic, bounded FASTQ fingerprinting (no LLM, no network).

Turns bytes into an :class:`~seqforge.models.Observation` from a bounded, head-limited decompressed
stream. Every touch is bounded by a read budget (``--max-reads``, default 200 000) and a byte cap
(``--max-bytes``, default 256 MB decompressed); wall-clock is never a budget, and a code path that
*can* stream a whole multi-GB FASTQ is a bug (R3).

Tier A (this module) computes structural signals with no KB: per-cycle base composition, segmentation
(constant/random/homopolymer), read-length profile, distinct-value ratios, read-name grammar,
N-rate, quality encoding, gzip integrity, and an extrapolated read-count estimate. It assigns **no
roles** — that interpretation belongs to ``resolve``.
"""

from __future__ import annotations

#: CalVer YYYY.M.PATCH, bumped whenever probe output semantics change; folded into the Observation
#: cache key (R7). Component/tool-stamp versions use CalVer just like the package version.
PROBE_VERSION = "2026.7.0"

#: Default bounded-read budget (R3). Overridable per call by the CLI.
DEFAULT_MAX_READS = 200_000

#: Default decompressed-byte cap (R3). Whichever budget trips first stops the stream.
DEFAULT_MAX_BYTES = 256 * 1024 * 1024

# Imported last: core depends on the budget constants above (keeps the package import acyclic).
from .core import hash_file, probe_file  # noqa: E402

__all__ = [
    "PROBE_VERSION",
    "DEFAULT_MAX_READS",
    "DEFAULT_MAX_BYTES",
    "hash_file",
    "probe_file",
]
