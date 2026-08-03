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

#: CalVer YYYY.M.PATCH, bumped whenever probe output semantics change; folded into the Observation
#: cache key. Component/tool-stamp versions use CalVer just like the package version.
#: 2026.7.1 — content-address from a bounded local key (head + size + gzip ISIZE), not a whole-file
#: sha256 (issue #37); the file identity string changes, so cached observations recompute once.
#: 2026.7.2 — DEFAULT_MAX_READS 200_000 -> 2_000 (issue #63), so the default samples 100x fewer
#: reads. This changes observation values (n_reads_sampled, per-cycle composition, the read-count
#: estimate), hence a probe-version bump: pinned manifests re-hash through their observation values.
#: The rationale recorded here at the time — "the resolved chemistry is invariant from 1k to 200k
#: reads across every benchmarked worm library" — IS FALSE, and was falsified 2026-08-01 on a worm
#: library, inside its own stated domain (#177). See the correction below; the budget itself stands.
#: 2026.7.3 — `gzip.ok` and `gzip.truncated` stop meaning the same thing (issue #94). A stream that is
#: not readable gzip now reports `ok=False, truncated=False` where it reported the reverse, and a
#: corrupt deflate payload reports it at all rather than raising. Only malformed inputs move; a clean
#: FASTQ observes identically, so no clean manifest re-hashes — but a *cached* verdict from the old
#: probe would replay the old meaning, which is what this stamp evicts.
#: 2026.8.0 — every head-derived statistic now reports what it was measured OVER (issue #190). The
#: correction below moved the budget's defence onto `onlist_hit_rate`'s denominator and left three
#: statistics built from the same head — composition, segmentation, and the windows
#: `consensus_match_rate` is cut from — with no equivalent figure, which is exactly the gap
#: GSE305031 walks through: a cycle that is 91% uncalled in the first 2 000 reads scores as "random"
#: rather than as "unread", and the segmentation says so with the same confidence it would have on a
#: clean file. So a cycle carries the denominator it divided by, a segment carries the share of the
#: head's cells its classification was decided over, and the head carries both loss channels apart.
#: `n_rate` — the whole-head N fraction, computed since the first probe and read by NOTHING — is
#: gone as a field and survives as `coverage.called_fraction`, which is the same measurement stated
#: as coverage rather than as its complement, and split from the ragged-read-length channel it used
#: to hide. It is not a deletion: it is the number this issue asks for, moved to where it means
#: something and joined by the two figures that localize it.
#: Every clean FASTQ observes DIFFERENTLY (the fields are new and `n_rate` is gone), so every cached
#: observation must recompute — which is what this stamp is for. The manifest hash covers `library`
#: and `experiment`, neither of which carries an observation value that moved, so no pinned dataset
#: re-hashes. NOTHING gates on the new numbers; they exist to be collected first.
PROBE_VERSION = "2026.8.0"

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
    "PROBE_VERSION",
    "DEFAULT_MAX_READS",
    "DEFAULT_MAX_BYTES",
    "Budget",
    "WholeFile",
    "build_observation",
    "local_whole_file",
    "content_key_from_md5",
    "content_key_from_sra",
    "gzip_isize",
    "remote_content_key",
    "probe_file",
    "probe_sample",
]
