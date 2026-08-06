# The xdist worker sweep, re-measured on the pinned environment

Measured **2026-08-01**, on the 48-core shared login node, over the whole suite under the `test`
environment *after* that environment began pinning `OMP_NUM_THREADS` / `OPENBLAS_NUM_THREADS` /
`MKL_NUM_THREADS` to `1`. The previous sweep was taken before the pin, so part of what it measured
was oversubscription that no longer exists — this note supersedes it, and is written to be superseded
in turn.

The decision this fed is ADR-0002, which absorbed it; the standing rule is the test ladder in
`AGENTS.md`.

## Method

The whole suite, varying only `-n`, medians of three repeats per setting, with an **identical
pass/skip count in every run** — the sweep changed how the suite ran, never what it ran. The 8-CPU
half is the same box confined with `taskset`, because 8 is the count `auto` yields on a genuinely
8-core machine and that is the reader most likely to be affected by the cap.

Figures are ratios, not seconds, and nothing is quoted past one decimal.

## On 48 cores, relative to the shipped setting of 12

| workers | wall | CPU |
| ------- | ---- | --- |
| 8 | 1.3x | 0.9x |
| **12** | **1.0x** | **1.0x** |
| 16 | 0.8x | 1.1x |
| `auto` (48) | 1.3x | 3.2x |

## On 8 CPUs (`taskset`), relative to 8

| workers | wall | CPU |
| ------- | ---- | --- |
| 4 | 1.0x | 0.9x |
| **8** | **1.0x** | **1.0x** |
| 12 | 1.2x | 1.1x |
| 16 | 1.2x | 1.2x |
| 24 | 1.5x | 1.4x |

## What the numbers say

**Past the core count more workers only cost**, and `auto` uncapped is the clear loser — 1.3x the
wall of 12 and, the part that matters on a shared box, **3.2x its CPU**. Every worker pays the
interpreter import and rebuilds each session-scoped fixture, so once the cores are covered that fixed
cost is all that is left to grow.

Below the core count the picture is flatter than it was before the pin, and that flattening is what
pinning bought: on the 48-core box the wall still improves slightly from 12 to 16, buying about a
fifth of the wall for ~9% more CPU.

**`--dist loadfile` was measured here too, and rejected.** It sat at the same wall at *every* worker
count, because it hands a whole file to one worker, so the longest file becomes the floor. When that
was measured the floor was `tests/test_compile.py`; #113 has since split it into `test_manifest.py` /
`test_compose.py` / `test_workflows.py`, but `loadfile` cannot split whatever the longest file is,
so it stays rejected in favour of `loadgroup`.

## What this could not establish

- **The wall column is noisy.** Both halves were taken on a *shared* node whose load moved between
  ~10 and ~44 during the sweep. Repeats at one setting spread by as much as 1.6x, which is why these
  are medians and why 12 and 16 on the small box are indistinguishable. The CPU column varied by
  under 2% across repeats and is the trustworthy half; read the walls as a shape, not as values.
- **Serial was not re-measured**, so this says nothing about where serial stops beating parallel.
  That crossover belongs to the work that would decide parallelism from the size of the selection,
  which is not built.
- **The cap of 12 is not in these tables.** They say 16 would be tolerable on a large box; the cap is
  a policy about what one run may take from everyone sharing the machine, argued in ADR-0002.
- **The commit was not recorded**, only the date and the environment property that changed. Re-take
  the sweep rather than reconciling it against a tree.
