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

## How many tests there are, and the three lanes (measured 2026-08-06)

A count, recorded because the bar a test clears is held at review and nothing mechanises it. A
collected-count ratchet was considered and declined for the same reason the docs line-cap was, so
drift has to be made **observable** rather than gated: diff this number, do not assert on it.

| lane | selection | collected |
| --- | --- | --- |
| unit | `-m 'not external'`, corpus excluded | 1,369 |
| hermetic corpus | `tests/test_evals.py` | 182 |
| external | `-m external` | 25 |
| **whole suite** | | **1,576** |

The arithmetic is not the finding — the **partition** is. 1,369 + 182 + 25 leaves nothing over, so
every test runs exactly once across CI's three jobs and none can fall between them: `external`
against its negation is total by boolean negation, and the corpus is one file. Before this, the
external selection ran twice, once in a job where its binaries were absent and it therefore skipped.

What keeps the split honest is a guard rather than the count. A test that gates on a binary without
carrying the marker would sit in the unit lane and skip itself green — which is the one way a test
could reach the wrong side — so a `repo`-marked guard fails the build on exactly that.

### What each lane costs, and how the workers were divided (osx-arm64, 12 cores)

Each lane run **alone** at 12 workers, to get a cost independent of how they are scheduled:

| lane | wall | CPU (user + sys) |
| --- | --- | --- |
| unit | 21.8 s | 174 s |
| hermetic corpus | 16.6 s | 104 s |
| external | 13.9 s | 73 s |

352 s of CPU over 12 cores puts a **floor of ~29 s** under any concurrent arrangement of the three,
and that floor — not the test count — is what the local gate is up against. The caps were set
proportional to those costs (6 / 4 / 4) rather than equally: an equal split starves the lane that
costs the most, and the first arrangement tried (8 / 4 / 4) asked for 16 workers on 12 cores and
measured **41 s** against the proportional split's **37 s**. At 6/4/4 the three walls land at 33.3 s,
35.1 s and 31.0 s — within four seconds of each other, which is what says the division is right.

**The gate: ~44 s → 37 s.** Modest, and worth stating plainly rather than rounding up. The same tree
as one undivided 12-worker run measures 42.3 s wall on 311 s of CPU — only ~7.4 cores busy, which is
the under-utilisation the split exists to claim. Three sessions recover it (10.6 cores busy) but
**cost 27 % more CPU** doing so, because each pays its own interpreter import across its own workers.
That overhead is why the wall lands at 37 s and not at the 29 s floor.

**Where the split actually pays is CI**, and by much more, because there the lanes are separate
runners rather than three sessions contending for one box: no lane pays another's tail, and each gets
a whole runner. The local gain is a side effect of the same change, not its point.

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
