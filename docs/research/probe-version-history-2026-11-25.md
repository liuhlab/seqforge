# `PROBE_VERSION`: why each bump was made, to 2026-11-25

Moved on **2026-11-25**, word for word, from the comment above `PROBE_VERSION` in
`src/seqforge/probe/__init__.py`. Each entry gives the reason for one bump, and some give
the measurement behind it. The stamp keeps one line that says what a bump means and points
here. From 2026-11-25 on, a bump's reason goes in its commit message, and this file is not
extended.

The text is the comment as it stood, with only the `#:` comment marker taken off each line.
The entries keep the comment's order, which is not always date order. Where an entry says
"this log", "above" or "below", it means this text.

```text
CalVer YYYY.M.PATCH, bumped whenever probe output semantics change; folded into the Observation
cache key. Component/tool-stamp versions use CalVer just like the package version.
2026.7.1 — content-address from a bounded local key (head + size + gzip ISIZE), not a whole-file
sha256 (issue #37); the file identity string changes, so cached observations recompute once.
2026.7.2 — DEFAULT_MAX_READS 200_000 -> 2_000 (issue #63), so the default samples 100x fewer
reads. This changes observation values (n_reads_sampled, per-cycle composition, the read-count
estimate), hence a probe-version bump: pinned manifests re-hash through their observation values.
The rationale recorded here at the time — "the resolved chemistry is invariant from 1k to 200k
reads across every benchmarked worm library" — IS FALSE, and was falsified 2026-08-01 on a worm
library, inside its own stated domain (#177). See the correction below; the budget itself stands.
2026.7.3 — `gzip.ok` and `gzip.truncated` stop meaning the same thing (issue #94). A stream that is
not readable gzip now reports `ok=False, truncated=False` where it reported the reverse, and a
corrupt deflate payload reports it at all rather than raising. Only malformed inputs move; a clean
FASTQ observes identically, so no clean manifest re-hashes — but a *cached* verdict from the old
probe would replay the old meaning, which is what this stamp evicts.
2026.8.0 — every head-derived statistic now reports what it was measured OVER (issue #190). The
correction below moved the budget's defence onto `onlist_hit_rate`'s denominator and left three
statistics built from the same head — composition, segmentation, and the windows
`consensus_match_rate` is cut from — with no equivalent figure, which is exactly the gap
GSE305031 walks through: a cycle that is 91% uncalled in the first 2 000 reads scores as "random"
rather than as "unread", and the segmentation says so with the same confidence it would have on a
clean file. So a cycle carries the denominator it divided by, a segment carries the share of the
head's cells its classification was decided over, and the head carries both loss channels apart.
`n_rate` — the whole-head N fraction, computed since the first probe and read by NOTHING — is
gone as a field and survives as `coverage.called_fraction`, which is the same measurement stated
as coverage rather than as its complement, and split from the ragged-read-length channel it used
to hide. It is not a deletion: it is the number this issue asks for, moved to where it means
something and joined by the two figures that localize it.
Every clean FASTQ observes DIFFERENTLY (the fields are new and `n_rate` is gone), so every cached
observation must recompute — which is what this stamp is for. The manifest hash covers `library`
and `experiment`, neither of which carries an observation value that moved, so no pinned dataset
re-hashes. NOTHING gates on the new numbers; they exist to be collected first.
2026.8.1 — `read_length` reports `mode_share`, the share of the sampled reads sitting at the modal
length (issue #190). `n_distinct` counts which lengths are present and says nothing about how the
reads divide among them, so one read of 2 000 a base short and a file where half the reads were
trimmed are the same number — and the pre-trimming refusal downstream, which had only that number
to read, refused both. The share is the population statement the refusal was always making, and
the profile could not supply it: `percentiles` is populated only when the lengths vary and, being
p1/p50/p99, cannot express a majority either.
UNLIKE the figures above, this one is added TO BE READ — `resolve.escalate` gates on it — which is
why it is not in `test_probe.REPORT_ONLY_FIGURES`. Every FASTQ observes differently (the field is
new), so every cached observation recomputes; it is REQUIRED rather than defaulted, so an
observation written by an older probe fails validation and is re-probed instead of reading as
"no read sits at the mode" and refusing a dataset that is fine. The manifest hash covers `library`
and `experiment`, and `manifest.fill` reads only `min_len`/`max_len` off this profile, so no
pinned dataset re-hashes.
```
