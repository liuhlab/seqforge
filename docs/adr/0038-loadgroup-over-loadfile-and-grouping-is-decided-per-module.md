# 38. `loadgroup` over `loadfile`, and grouping is decided per module

Date: 2026-07-30

## Status

Accepted. Amended 2026-08-04 with the `composed-plate` group and what it measured; the decision is
unchanged.

## Context

Under xdist a session- or module-scoped fixture is rebuilt once *per worker*, so spreading a module
that shares one expensive fixture buys parallelism by paying for the fixture again. Measured on
`tests/test_report.py` alone, whose tests all read one build: its CPU rose **roughly linearly with
the worker count** — about **5x** from one worker to eight, for identical proof. That is the
suite-wide reason utilisation sat at well under half the available cores, and the reason adding
workers stopped helping.

The obvious fix is `--dist loadfile`, which hands each file to one worker. It was measured and
rejected ([`docs/research/xdist-worker-sweep-2026-08-01.md`](../research/xdist-worker-sweep-2026-08-01.md)),
and a future reader will reach it again from the same evidence — it is the setting whose name
describes the problem. This record exists so they find why it lost rather than re-deriving it.

## Decision

**`test`, `test-fast` and `test-external` run `--dist=loadgroup`, and a module opts into grouping
only after the fixture has been measured against it.**

```python
pytestmark = pytest.mark.xdist_group("report-workspace")   # tests/test_report.py
```

`loadgroup` behaves exactly like the default `load` for every test carrying no `xdist_group` mark —
it splits at test granularity and every file spreads across workers. It groups **only** what is
marked. Verified before adopting it: the whole suite under `--dist=loadgroup` with no marks present
gave an identical result and an identical wall. It is a safe swap.

## Why not `loadfile`

Because it groups *everything*, whether or not the grouping pays. It sat at the same wall at every
worker count, because handing a whole file to one worker makes the longest file the floor on the
suite wall. #113 split the file that was the floor when this was measured (`tests/test_compile.py`)
into `test_manifest.py` / `test_compose.py` / `test_workflows.py`, which moved the floor without
removing it: `loadfile` cannot split whatever the longest file is, and there is always a longest
file.

`loadgroup` is the same mechanism made opt-in. That is the whole difference, and it is the reason
adopting it is not the rejected setting coming back.

## Why not group by default

**Grouping is a trade.** It wins where the fixture is expensive *relative to* the tests that read it,
and loses where the module holds many slow independent tests: a grouped module runs **serially**, so
its serial time becomes a floor on the suite wall — the `loadfile` failure, re-created by hand, one
module at a time. Two questions decide a candidate, in this order:

1. Is the fixture expensive next to the tests reading it? If it is a rounding error, there is nothing
   to save no matter how many workers rebuild it.
2. Is the module's *serial* time comfortably under the suite wall? If not, grouping it makes it the
   new wall.

Applied to this suite (2026-07-30): `test_report.py` and `test_partition.py` **group** — a
second-plus build read by most of a cheap module, and a serial time well under the wall.
`test_observation_sources.py` and `test_records.py` are **left alone**; their fixtures are sub-100ms,
so there is nothing to save.

`synth_10x_v3` is the instructive case and it is **left ungrouped**. It is the session fixture the
compose/manifest tests share and the obvious candidate — but earlier work already made it cheap, so
pinning its readers to one worker would roughly double their serial time for no fixture saving at
all. Measure the fixture against the module before reaching for the mark.

## Why the unit of grouping is the fixture's consumers, not the file

A module-level `pytestmark` is the right shape only when the whole module shares the fixture. Where a
handful of tests share one and the rest of the file does not, mark those tests and leave the file
spread. Some groups here span more than one file, which a file-level mark cannot express at all:

| group | fixture | members |
| ----- | ------- | ------- |
| `enormous-fastq` | the 128 MB-decompressed FASTQ | `test_probe.py` |
| `kb-probes` | every KB spec's reads, probed | `test_kb.py` + `test_resolve.py` |
| `src-trees` | `src/seqforge` parsed | `test_repo_invariants.py` + `test_workflows.py` + `test_cli.py` + `test_probe.py` |
| `composed-plate` | a 96-cell `smartseq3` plate, composed and dry-run | `test_compose.py` |

In each of these the build dominates its readers outright — the `enormous-fastq` write costs tens of
times what the probes it enables do — which is what makes the trade obvious without a sweep.
`composed-plate` is the same shape, measured (2026-08-04): the `snakemake -n -p` behind it is ~1.9s
and the plan-reading tests are ~0.02s each, so ungrouped the suite paid the spawn once per reader and
grouped it pays it once. That group's remaining member is the small-N end-to-end, which skips
wherever STAR is absent and therefore costs the group nothing in the `test` job. It does run in the
`test (external binaries)` job, where the whole group is a small slice of a small selection — so
grouping it costs that job a little parallelism and the trade is unchanged.

The table names files, not counts. Counts here went stale twice while the argument did not depend on
them; `grep -rn 'xdist_group(' tests/` is the live answer.

## So in code

**Do not add an `xdist_group` mark until you have measured the fixture against the module, and never
reach for `--dist loadfile`.** Ask the two questions above in order: a fixture that is a rounding
error next to its readers has nothing to save, and a module whose serial time approaches the suite
wall becomes the wall once it is grouped. Mark the fixture's consumers rather than the file when only
part of the file shares it. `--durations=0 | grep setup` is how you check a mark landed: one setup
line per fixture, not one per worker that happened to draw a consumer. `xdist_group` needs no
`markers` entry — `pytest-xdist` registers it, so `--strict-markers` stays happy.

**Enforced by.** **None exists.** No test reads the `--dist` flag out of the pixi task strings, and
nothing measures a grouped module's serial time against the suite wall — so a mark added without a
measurement, or a swap back to `loadfile`, would show up only as a wall that stopped improving.
Noticing a violation mechanically would need the task strings asserted in
`tests/test_repo_invariants.py` and a recorded per-module serial time to compare against; neither is
built, and the second is the kind of number this repo declines to freeze into a test.

## Consequences

- **Parallelism is the last thing to reach for, and it landed last on purpose.** It hides waste
  rather than removing it: had it come first, the redundant `snakemake` spawns the suite once paid
  would still be there, just spread across workers.
- The cap that accompanies this (`-n auto --maxprocesses 12`) is a separate choice, and a policy
  rather than an inference — the sweep says 16 would be tolerable on a large box. On a small machine
  `auto` resolves below the cap and it never binds, so 12 only ever applies where the box is large,
  and there it bounds what one test run may take from everyone else sharing it.
- The standing rule an agent follows is in [`docs/agents/testing.md`](../agents/testing.md); the
  numbers behind it are in
  [`docs/research/xdist-worker-sweep-2026-08-01.md`](../research/xdist-worker-sweep-2026-08-01.md).
  This record owns only the choice between the two distribution modes and the per-module test.
- `tests/conftest.py` fills the worker flags in for a bare `pytest`, so the grouping applies to the
  gate, the fast verb and CI alike — and a run that names a selector stays serial, where none of this
  binds.
