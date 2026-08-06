# What the test suite costs, and where the cost sits

Collected **2026-08-05** from the standing testing notes, whose cost figures were measurements and not
rules. The figures below are of different vintages and each carries its own date; what they have in
common is the instrument.

**Method.** Everything here is a **ratio**, not a number of seconds, except where a date and a host
are given beside an absolute. Absolute timings and test counts go stale within a PR or two, and a
stale number in a doc is worse than no number because it is still trusted. What survives a
re-optimisation is the *shape* — which term dominates which, and by roughly how much — so that is what
was recorded. Where an absolute decided something, it is a dated measurement of that decision and not
a claim about today.

The worker-count half of this question is a separate sweep, superseded on its own schedule:
[`xdist-worker-sweep-2026-08-01.md`](xdist-worker-sweep-2026-08-01.md). Nothing about `-n`, the cap of
12, or `--dist` is re-derived here.

## The cost of one run, by shape

| run | cost |
| --- | --- |
| one file with `-k`, in the red→green loop | a second or two |
| one whole test file | a few seconds |
| the full local gate (lint + format + typecheck + the suite) | ~an order of magnitude more than a targeted run |
| reading CI after the PR is open | free |

**A run that names a selector stays serial, and that is deliberate.** Starting twelve workers to run
three tests costs more than it saves. Drop the selector and the parallel flags are filled in, at which
point a bare run of the whole suite costs roughly **a fifth** of what the same suite costs serially.

**Two intermediate rungs were measured and are not worth having.** A `check-fast` step between the
targeted run and the full gate measured *slower than the gate it was a cheap substitute for*, while
checking less — it was deleted rather than tuned. `test-fast` (the suite minus what needs a binary the
project does not own) is **not much faster than the whole suite**: the subprocess cost that used to
dominate is gone, so what remains is mostly real work and the marker deselects a small fraction of it.
It survives as a verb for a machine without those binaries, not as a step in a ladder.

## The external selection (measured 2026-08-05)

With liulab-runtime's `align-rna` environment on `PATH` — `star` 2.7.11b, `samtools` 1.23.1,
`htslib` 1.23.1:

| | |
| --- | --- |
| selection | 25 tests |
| result | 25 pass, **nothing skips** |
| wall | ~28 s at 12 workers |

Without those binaries the same selection **skips** the three STAR-gated tests on every host, and the
bgzip/tabix one wherever htslib is absent. That difference is the finding: a skip is green, and the
CI runner and the developer box disagreed about which tests were skipping, which is why for the life
of the repo the STAR-gated tests — including the end-to-end proof that a UMI tag survives into the
aligner's own output — ran on no host the project's CI could reach, with nothing ever red about it
(#333).

## Where the wall went, when it was an order of magnitude worse

The suite is **more than an order of magnitude faster than it was**, and none of that came from making
a test weaker — nothing removed was a slow *test*. Three shapes account for all of it, and they are
what to look for when the wall creeps back up.

- **A fact being re-proved, because the seam that owned it sat on the wrong interface.** Almost every
  win was this one. The cleanest case: the wiring gate returned a four-character verdict while its
  implementation held the whole dry-run plan text, so every test that wanted the plan spawned its own.
  The fix was to expose what was already computed, not to run less.
- **Contention rather than duplication.** Pinning the workers' thread pools to one thread each removed
  no work at all — the same work had been spent fighting over the machine instead of doing it. When
  the wall swings between repeats rather than sitting high, look for contention before waste.
- **An indivisible block the workers cannot spread.** Parametrizing the corpus-green test per case
  removed **no CPU at all**; it split the suite's longest single item into items. When utilisation is
  the problem, look for the block that cannot be split before looking for work to delete — the marginal
  value of deleting one test is its duration ÷ the worker count, which is almost always less than it
  looks.

**If the number creeps back up, find the fact being re-proved; do not add workers.** Parallelism
landed last on purpose, because it hides waste rather than removing it.

## What grows with the KB

The whole KB partition is a few seconds today. A spec count anchored in prose has gone stale twice, so
the live one is `ls src/seqforge/kb/specs/`; what matters is the *shape* of each term rather than any
timing (#112).

| sweep | term | what holds it down |
| --- | --- | --- |
| every spec round-trips | O(n) | nothing — it *is* the rule that every entry is executable and self-testing, and you do not buy it down |
| the benign-twin biconditional | O(n²) | a microsecond constant |
| no spec pair is confusable without declaring it | O(n²) | a **geometry pre-gate**: the cheap `geometry_could_accept` (µs) runs before the scorer, ~100× |
| the pre-gate's own necessity, and descent narrowing | O(n²) | **cannot** take that pre-gate, and their docstrings say why — the first's *subject* is the pre-gate, and the second scores exactly the pairs the gate excludes |

The single highest-leverage change was **sharing the per-spec probe fixture** (#105): a probe rebuilt
once per worker was the dominant term of the family and descent sweeps, and grouping it cut them
**~90 %**. Both are sub-second after it.

So when a KB sweep's wall creeps up as specs are added, look first for a probe rebuilt per pair, then
for a missing geometry pre-gate — never for a generative axis to narrow. The O(n²) terms are what will
eventually decide this, and the shared fixture and the pre-gate are already what hold them down.

## What this could not establish

- **No absolute for the suite as a whole is recorded here**, deliberately, and the "order of magnitude
  faster" claim is a ratio against a tree nobody can re-run. Treat it as the reason the three shapes
  are worth knowing, not as a figure to diff against.
- **The external selection was measured on one host with one STAR.** The version is quoted because the
  aligner test's own docstring records measuring under it; a different STAR is a different measurement.
- **Where serial stops beating parallel was not measured.** That crossover belongs to work that would
  decide parallelism from the size of the selection, which is not built.
