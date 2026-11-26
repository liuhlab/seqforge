"""``probe`` — deterministic, bounded FASTQ fingerprinting (no LLM, no network).

Turns bytes into an :class:`~seqforge.models.Observation` from a bounded, head-limited decompressed
stream. Every touch is bounded by a read budget (``--max-reads``, default 200 000) and a byte cap
(``--max-bytes``, default 256 MB decompressed); wall-clock is never a budget, and a code path that
*can* stream a whole multi-GB FASTQ is a bug.

Tier A (this module) computes structural signals with no KB: per-cycle base composition, segmentation
(constant/random/homopolymer), read-length profile, distinct-value ratios, read-name grammar,
quality encoding, gzip integrity, an extrapolated read-count estimate, and — beside each statistic
built from the head — the coverage it was measured over. It assigns **no roles** — that
interpretation belongs to ``resolve``.
"""

from __future__ import annotations

#: CalVer YYYY.M.PATCH; bump when probe output semantics change (folded into the Observation cache key). History: `docs/research/probe-version-history-2026-11-25.md`.
PROBE_VERSION = "2026.8.1"

#: Default bounded-read budget: 2_000 reads, ~100x cheaper than the old arbitrary 200_000.
#: Fingerprint-based probe on these small slices is the routine path; a caller that wants to read more
#: of a full-size FASTQ passes a larger --max-reads (the explicit "use the whole file" opt-in). Every
#: touch stays bounded by this AND --max-bytes — raising one never unbounds the other.
#:
#: **A HEAD SLICE IS NOT A RANDOM SAMPLE**, and the original justification for this number said
#: otherwise. It read: "the benchmarked N-invariant floor is <=1k across every chemistry (issue #63);
#: 2k is a deliberate 2x cushion". That invariance was real across the libraries benchmarked when it was
#: written, but it is a fact about those libraries, never a property of head slices — and 2026-08-01
#: produced the counterexample inside its own stated domain (#177). `evals/benchmark/GSE305031`, a
#: *C. elegans* 10x GEM-X 3' v4 library, carries a DARK CYCLE at the head of the run: N at R1 cycle 2 in
#: 91.35% of the first 2 000 reads and 0.00% of the last 2 000. Sampling the first N reads samples the
#: flow cell's FIRST TILES, which is precisely where such an artefact lives, so no N is large enough to
#: be safe in general and picking one would be a guess dressed as a calibration.
#:
#: The number is therefore NOT the defence, and is deliberately left where it was. What defends the
#: budget is that the statistics read off the sample no longer treat an uncalled base as a miss: the
#: onlist hit rate counts only the reads that could have hit, so a dark cycle costs coverage rather
#: than moving the rate (see the comment above `io.onlist.onlist_hit_rate`). On GSE305031 that is the
#: whole difference between 7.90% over the sampled head and 91.33% — against 92.33% over the package's
#: full 20 000 reads, i.e. the small sample was never the problem. Raising N here would have bought one
#: dataset and left the assumption standing; a caller who genuinely needs more reads still passes
#: --max-reads.
#:
#: That defence only ever covered ONE statistic. Composition, segmentation and the windows
#: `consensus_match_rate` is cut from are read off the same head and cannot count their way around a
#: dark cycle — a cycle nobody called has no dominant base, so it is classified as random with
#: nothing to say it was never read. They now report their coverage instead
#: (`models.observation.HeadCoverage`), which is a weaker remedy than the onlist's on purpose:
#: excluding a read from a hit rate is arithmetic, while excluding a cycle from a segmentation would
#: change what the segmentation IS. Nothing gates on the reported numbers, and a poor one refuses
#: nothing — the corpus distribution has to be visible before a threshold can be argued for, and a
#: threshold picked before it would be the same guess dressed as a calibration as picking N was.
DEFAULT_MAX_READS = 2_000

#: Default decompressed-byte cap. Whichever budget trips first stops the stream.
DEFAULT_MAX_BYTES = 256 * 1024 * 1024

# Imported last: core depends on the budget constants above (keeps the package import acyclic).
from .core import (  # noqa: E402
    WholeFile,
    build_observation,
    content_key_from_md5,
    content_key_from_sra,
    gzip_isize,
    local_whole_file,
    probe_file,
    probe_sample,
    remote_content_key,
)
from .streaming import Budget  # noqa: E402

__all__ = [
    "DEFAULT_MAX_BYTES",
    "DEFAULT_MAX_READS",
    "PROBE_VERSION",
    "Budget",
    "WholeFile",
    "build_observation",
    "content_key_from_md5",
    "content_key_from_sra",
    "gzip_isize",
    "local_whole_file",
    "probe_file",
    "probe_sample",
    "remote_content_key",
]
