# Testing: run the narrowest thing that can go red

`pixi run check` is a **pre-PR gate, not a per-edit one**. CLAUDE.md used to say to run it "when you
change behaviour", which is every edit, and offered no other verb — so the loop was: edit one line,
run the whole suite, open the PR, and let CI run the identical suite again.

This file is the rule. There are three rungs and you climb them once per change, not once per edit.

## The ladder

| # | When | Command | Cost |
| - | ---- | ------- | ---- |
| 1 | in the red→green loop | `pixi run -e test pytest tests/test_<module>.py -k <expr>` | seconds |
| 2 | before a commit, and before opening the PR | `pixi run check` | ~an order of magnitude more |
| 3 | after the PR is open | read CI | free |

**This file quotes ratios, not seconds.** Absolute timings and test counts go stale within a PR or
two, and a stale number in a doc is worse than none because it is still trusted. What survives is the
*shape* — which thing dominates which, and by roughly how much. Where a number below decides
something, it is a dated measurement of that decision, not a claim about today.

**There used to be a fourth rung**, `check-fast`, between the targeted run and the full gate. It was
deleted once both gates ran their steps concurrently over a 12-worker pytest: it measured *slower
than the gate it was a cheap substitute for*, and checked less. `test-fast` survives as a standalone verb — `-m 'not external and not repo'` is what you want on a machine
with no `snakemake` — but it is no longer a rung, because a rung that saves nothing is a rung nobody
should be told to climb.

**Rung 1 is where you live.** A single file with `-k` is a second or two; a whole test file is a few.
Nothing about a one-line change is learned by running the whole suite that a targeted run does not
tell you in a fraction of the time.

**Rung 3 is a rule, not a suggestion.** Once the PR is open, `.github/workflows/ci.yml` runs
`pixi run check` on every push. Running it again locally re-proves what CI is already proving and
tells you nothing new. Read the run.

## Which file tests the module you edited

Test files mirror packages, so rung 1 has an answer:

| you edited | run |
| ---------- | --- |
| `probe/` | `tests/test_probe.py` |
| `resolve/` (scoring, assign, escalate, geometry, window) | `tests/test_resolve.py` |
| `resolve/records.py` (the metadata resolver) | `tests/test_records.py` |
| `resolve/provenance.py` (the wrong-PDF gate) | `tests/test_provenance.py` |
| `kb/` | `tests/test_kb.py` |
| `manifest/` (fill, hash, validate, policy/precedence) | `tests/test_manifest.py` |
| `compose/` (plan, config, units, gates, params_gate) | `tests/test_compose.py` |
| `workflows/` (registry + `.smk` source; h5ad, qc, cram, fragments) | `tests/test_workflows.py` |
| tree-wide R10 AST guards (read `src/`, never compose) | `tests/test_repo_invariants.py` |
| `harvest/` | `tests/test_harvest.py`, `tests/test_extract.py` |
| `harvest/fields.py` (the asked/permitted vocabulary) | `tests/test_fields.py` |
| `io/` | `tests/test_io.py`, `tests/test_remote.py`, `tests/test_sra.py`, `tests/test_archive.py` |
| `io/taxonomy.py` | `tests/test_taxonomy.py` |
| `fingerprint/` | `tests/test_fingerprint.py` |
| `report/` | `tests/test_report.py` |
| `models/` | `tests/test_models.py` |
| `evals/`, `e2e.py` | `tests/test_evals.py`, `tests/test_e2e.py` |
| `hooks/` | `tests/test_hooks.py` |
| `cli.py`, `cli/` | `tests/test_cli.py`, `tests/test_partition.py` (the multi-assay `run` path) |
| `skills/`, `docs/` | `tests/test_skills.py`, `tests/test_docs.py` |

Anything that reads an `Observation` from more than one source also owes
`tests/test_observation_sources.py` a look — it is the only place the four callers of
`build_observation` are asserted to agree.

## The markers

`--strict-markers` is on, and there are exactly two. Both are **semantic** — what a test needs, and
what it is about:

| marker | meaning |
| ------ | ------- |
| `external` | needs a binary seqforge does not own (snakemake / STAR / htslib / build) |
| `repo` | checks repo consistency (`test_docs.py`, `test_skills.py`), not `src/` behaviour |

`test-fast` is `-m 'not external and not repo'`. It is not much faster than the whole suite, and that
is the honest state of things: the subprocess cost that used to dominate is gone, so what remains is
mostly real work, and it deselects a small fraction of the suite. Both it and `test` run under
`pytest-xdist` — see below.

**The marker is applied per test, not per file.** `repo` is about what a test is *about*, and
`tests/test_skills.py` is where one file holds both kinds. Five tests check the shipped skills and the
installer (genuinely `repo`) and carry `@pytest.mark.repo`; four introspect the **live Typer app** and
go red when a CLI verb is renamed — which is exactly what R6 names its anchor to catch — so they carry
**no** mark and `test-fast` runs them (#113). A module-level `pytestmark = repo` used to hide that R6
anchor from `test-fast`; it is gone. `tests/test_docs.py` stays fully `repo` — it reads the site's
prose, not `src/` — so add `pytest tests/test_docs.py` if you want the doc-consistency check too.

There is deliberately **no `slow` marker**. It would be a hand-maintained list keyed on a number that
changes every time someone optimises, and nothing would go red when it drifted — a marker that lies
about cost is worse than no marker, because it is trusted.

`external` is applied by mechanism: `tests/conftest.py`'s `pytest_collection_modifyitems` marks
anything requesting a fixture that **spawns** — `real_wiring_gate` or `dry_run`. A hand-written list
would go stale silently, and the staleness would show up as `test-fast` spawning subprocesses nobody
meant it to.

**Keying that on "asked for the real gate" was wrong in both directions**, and both directions were
live: the composer tests held a module-local `_dry_run` that spawned `snakemake` with no fixture at
all, so two tests shelling out to a binary we do not own were selected by `test-fast` and hard-failed
rather than skipped on a machine without it — the exact thing `test-fast` exists to avoid. Meanwhile
`test_compose_emits_a_snakefile_even_when_no_gate_runs` un-stubs the gate only to pass
`run_wiring_gate=False` and prove it never runs; it spawns nothing, is instant, and was excluded from
`test-fast` for a subprocess it does not make.

So there are two fixtures and they mean different things. `real_wiring_gate` means "I spawn"; the
plan-text spawner is the `dry_run` fixture (in `conftest.py`, so the marker can see it — a module-local
spawner is a spawn the marker cannot see); and `gate_that_must_not_run` means "un-stub the gate, and I
will not reach it". That last one is a mechanism rather than a promise: it installs the real gate
behind a counter and fails at teardown if it is called.

The property is checkable, and worth re-checking if this ever moves: put a refusing `snakemake` decoy
first on `PATH` and run `pixi run test-fast`. It passes.

## The verbs

```bash
pixi run -e test pytest tests/test_probe.py -k budget   # rung 1
pixi run test-failed                                    # --lf --new-first -x: re-run what broke, worst first
pixi run check                                          # rung 2: lint + fmt-check + typecheck + the full suite
pixi run test                                           # the suite alone, 12 workers
pixi run test-fast                                      # the suite minus what needs a binary we do not own
```

## Parallelism: `-n auto --maxprocesses 12`, and why rung 1 is exempt

`test` and `test-fast` run under `pytest-xdist`. **`addopts` deliberately does not**, so rung 1 stays
serial: starting twelve workers to run three tests costs more than it saves, and rung 1 is where you
live.

The worker count was measured, not chosen — swept on a 48-core box over the whole suite, most
recently after the fixture-sharing work of #102. Relative to the best time:

| workers | wall |
| ------- | ---- |
| serial | ~6x |
| 8 | ~1.2x |
| **12** | **1.0x** |
| 16 | 1.0x |
| `auto` (48) | ~2.2x |

**More workers stop helping around twelve and then actively hurt.** Every worker pays the interpreter
import and rebuilds each session-scoped fixture, so past the knee that fixed cost grows faster than
the remaining work shrinks. `-n auto` on a big box means one worker per core and is *slower than 12*;
a bare `-n 12` on a 2-core runner oversubscribes it for the mirrored reason. `auto` capped at 12 is
the one spelling that is right in both places.

12 and 16 are now within noise of each other, which is what the fixture sharing bought: the knee got
flatter because there is less per-worker fixed cost to pay. The cap stays at 12 — it is the smaller
number on a tie, and the 2-core-runner argument is unchanged. Re-sweep before changing it; the knee
moves when the per-worker fixed cost does.

A second sweep on **8 pinned CPUs** (the #102 audit, 2026-07-30) confirms the cap from the
small-machine side: `n=4`/`n=8`/`n=12` were flat within ~4% (34.5s / 33.3s / 34.8s) and it degraded
past 16 (`n=16` 42.3s, `n=24` 42.7s). The two sweeps together — one 48-core box, one 8-core — are what
justify `--maxprocesses 12` on *both* large and small runners: flat up to the core count, never faster
past ~12-16, and never oversubscribed on a small one.

`--dist loadfile` was measured and rejected: it sat at the same wall at *every* worker count, because
it hands a whole file to one worker, so the longest file becomes the floor. When that was measured the
floor was `tests/test_compile.py`; #113 split it into `test_manifest.py` / `test_compose.py` /
`test_workflows.py`, so no single file is that long today — but `loadfile` still cannot split whatever
the longest file is, so it stays rejected in favour of `loadgroup` below.

### `loadgroup` is not the rejected `loadfile`

`test` and `test-fast` run `--dist=loadgroup`, and **that is not `loadfile` coming back.** `loadgroup`
behaves exactly like the default `load` for every test that carries no `xdist_group` mark — it splits
at test granularity, and every file spreads across workers. It groups **only** what is
marked. Verified before adopting it: the whole suite under `--dist=loadgroup` with no marks present
gave an identical result and an identical wall. It is a safe swap; `loadfile` is not, because
`loadfile` groups *everything* whether or not the grouping pays.

Marking a module is how it opts in:

```python
pytestmark = pytest.mark.xdist_group("report-workspace")   # tests/test_report.py
```

**Why a module wants this.** A session- or module-scoped fixture is rebuilt once *per worker*, so
spreading a module that shares one expensive fixture buys parallelism by paying for the fixture again.
Measured on `tests/test_report.py` alone, whose 16 tests all read one build: its CPU rose **roughly
linearly with the worker count** — about **5x** from one worker to eight, for identical proof. That
is the suite-wide reason utilisation sat at well under half the available cores, and the reason adding
workers stopped helping.

**Grouping is a trade, so it is decided per module by measurement**, never applied by default. It wins
where the fixture is expensive *relative to* the tests that read it, and loses where the module holds
many slow independent tests: a grouped module runs *serially*, so its serial time becomes a floor on
the suite wall. The question to ask each candidate, in that order:

1. Is the fixture expensive next to the tests reading it? If it is a rounding error, there is nothing
   to save no matter how many workers rebuild it.
2. Is the module's *serial* time comfortably under the suite wall? If not, grouping it makes it the
   new wall.

Applied to this suite (2026-07-30): `test_report.py` and `test_partition.py` **group** — a
second-plus build read by most of a cheap module, and a serial time well under the wall.
`test_observation_sources.py` and `test_records.py` are **left alone** — their fixtures are
sub-100ms, so there is nothing to save.

`synth_10x_v3` is the instructive case and it is **left ungrouped**. It is the session fixture the
compose/manifest tests share and the obvious candidate, but earlier work already made it cheap — so
pinning its readers to one worker would roughly double their serial time for no fixture saving at all.
That is the `loadfile` floor, re-created by hand. (Before #113 those tests were one 2000-line
`test_compile.py`, the suite's longest file, which made the trap costliest exactly there; the split
into `test_manifest.py`/`test_compose.py`/`test_workflows.py` removed that file but not the reasoning.)
Measure the fixture against the module before reaching for the mark.

**The unit of grouping is the fixture's consumers, not the file.** A module-level `pytestmark` is the
right shape only when the whole module shares the fixture; where a handful of tests share one and the
rest of the file does not, mark those tests and leave the file spread. Three groups here span *two*
files each, which a file-level mark cannot express at all:

| group | fixture | members |
| ----- | ------- | ------- |
| `enormous-fastq` | the 128 MB-decompressed FASTQ | 3 tests in `test_probe.py` |
| `kb-probes` | every KB spec's reads, probed | 3 in `test_kb.py` + 3 in `test_resolve.py` |
| `src-trees` | `src/seqforge` parsed | 1 in `test_repo_invariants.py` + 1 in `test_workflows.py` + 1 in `test_cli.py` |

In each of these the build dominates its readers outright — the `enormous-fastq` write costs tens of
times what the three probes it enables do — which is what makes the trade obvious without a sweep.

`--durations=0 | grep setup` is how you check this landed: one setup line per fixture, not one per
worker that happened to draw a consumer.

`xdist_group` needs no `markers` entry — `pytest-xdist` registers it, so `--strict-markers` is happy.

Parallelism is the **last** thing to reach for, and it landed last on purpose. It hides waste rather
than removing it: had it come first, the ~41 redundant `snakemake` spawns would still be there, just
spread across workers. If this number creeps back up, find the fact being re-proved — do not add
workers.

## Why there is no test-impact analysis

`pytest-testmon` and friends track **Python lines**, and a large share of this suite's behaviour is
data-driven: `kb/specs/*/spec.yaml`, the `.smk` modules, the packed onlists. Editing a spec touches no
Python, so impact analysis selects zero tests and reports green — silent under-selection, which is the
exact failure mode this project refuses everywhere else. See
[`docs/adr/0002-no-test-impact-analysis.md`](../adr/0002-no-test-impact-analysis.md).

## What the suite costs, and why

The suite is **more than an order of magnitude faster than it was**, and none of that came from
making a test weaker. What each change removed, largest first — the point is the *kind* of waste, not
the seconds:

| change | what it removed |
| ------ | --------------- |
| `-n auto --maxprocesses 12` over what remained | serial execution (the single biggest win) |
| `kb.load_spec` cached, YAML parsed with `CSafeLoader` | the same spec parsed hundreds of times |
| the wiring gate paid once per workflow module instead of ~41 times per compose | ~37 redundant `snakemake` spawns |
| the repeated manifest builds session-scoped | ~45 rebuilds of one manifest |
| the composer tests' 13 `snakemake` spawns down to 7 | the default plan re-derived seven times |
| `--dist=loadgroup` + `xdist_group` | shared fixtures rebuilt once per worker |
| four more immutable products shared (`kb_probes`, the 128 MB FASTQ, `src_trees`, two manifests) | duplicated fixture builds |
| three `test_resolve.py` tests off the shipped 6.8M-barcode onlist they never hit | a whitelist scan nothing asserted on |
| `test_corpus_is_green` parametrized per case | **no CPU at all** — see below |

Nothing here was a slow *test*. Every one was a fact being re-proved because the seam that owned it
sat on the wrong interface — which is the shape to look for when the number creeps back up. The
`snakemake` row is the cleanest example: `wiring_gate` returned a four-character verdict while its
implementation held the whole `snakemake -n -p` plan text, so every test that wanted the plan spawned
its own. The fix was to expose what was already computed, not to run less.

The last row is a different lever and worth naming separately: it removed **no CPU at all**. It split
the suite's longest *indivisible* block — one test that ran the whole eval corpus — into one item per
case, which xdist can spread. When utilisation is the problem, look for the block that cannot be split
before looking for work to delete: the marginal value of deleting one test is its duration ÷ the
worker count, which is almost always less than it looks.

## Adding a KB spec: what it costs the suite

The KB sweeps grow with the spec count, and the *shape* of each term matters more than any timing
(#112). At 12 specs the whole KB partition is a few seconds; the terms that grow are these:

- The **R8 anchors** are the price of "every KB entry is executable and self-testing," and you do not
  buy them down. `test_every_kb_spec_roundtrips` is O(n) and *is* the rule. The §12 biconditional is
  O(n²) with a microsecond constant. `test_no_spec_pair_is_confusable_without_declaring_it` is O(n²)
  **but geometry-pre-gated** — it checks the cheap `geometry_could_accept` (µs) before paying the
  scorer, which is ~100× and is the pattern to copy. Never narrow their generative axes to hit a clock.
- The two other O(n²) sweeps — `test_geometry_could_accept_is_necessary_for_rung02_acceptance` and
  `test_descent_narrowing_never_drops_a_valid_spec` — **cannot take that pre-gate**, and their
  docstrings say why: the first's *subject* is the pre-gate predicate (gating it is circular), and the
  second already gates on `length_feasible` (which *is* `geometry_could_accept`) and then scores
  exactly the pairs the gate excludes, because "an excluded spec never wins" is its whole point. Both
  measured cheap after #105's shared `kb_probes` fixture (~0.25s / ~0.10s at 12 specs) — the fixture,
  not a narrower axis, is what keeps them affordable as the KB grows.
- The single highest-leverage change was **sharing `kb_probes`** (#105): a per-spec probe rebuilt once
  per worker was the dominant term of the family/descent sweeps, and grouping it under
  `xdist_group("kb-probes")` cut it ~90%.

Projected pre-#105 on one worker, the partition stayed comfortable to ~25-30 specs and the O(n²) terms
dominate past ~50. So when a KB sweep's wall creeps up as specs are added, look first for a probe
rebuilt per pair, then for a missing `geometry_could_accept` pre-gate — not for an axis to narrow.

## Adding a test

- It goes in the file that mirrors the module it tests (the table above). Do not start
  `tests/test_<the-issue-i-am-fixing>.py`.
- Shared setup belongs in `tests/conftest.py`. It owns the one FASTQ writer, the synthetic onlist
  registry, the fake range server, the `snakemake` dry run, and the session-scoped immutable products:
  the `10x-3p-gex-v3` / `bulk-rnaseq-pe` / `splitseq` datasets, the per-spec `kb_probes` sweep, and the
  `src_trees` AST parse.
- **What may be shared is immutable products only.** A manifest, a registry, a directory nothing
  writes into. Never a workspace a test writes into: `seqforge/cache/` makes resume implicit (R5), so
  a shared workspace lets a later test collect a cached `Observation` and pass for the wrong reason.
  Every test composes into its own `tmp_path`; a test that must mutate a shared build takes a
  `shutil.copytree` derivative (see `own_workspace` in `tests/test_report.py`).
- If the new test spawns a binary seqforge does not own, mark it `external`.
- Do not add a case that recomputes its expected value with the function's own formula. It cannot
  fail, and it reads as coverage.
