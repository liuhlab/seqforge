# Testing: run the narrowest thing that can go red

`pixi run check` is a **pre-PR gate, not a per-edit one**. CLAUDE.md used to say to run it "when you
change behaviour", which is every edit, and offered no other verb — so the loop was: edit one line,
run the whole suite, open the PR, and let CI run the identical suite again.

This file is the rule. There are three rungs and you climb them once per change, not once per edit.

## The ladder

| # | When | Command | Cost |
| - | ---- | ------- | ---- |
| 1 | in the red→green loop | `pixi run -e test pytest tests/test_<module>.py -k <expr>` | **~2s** |
| 2 | before a commit, and before opening the PR | `pixi run check` | **~17s** |
| 3 | after the PR is open | read CI | free |

**There used to be a fourth rung**, `check-fast`, between the targeted run and the full gate. It was
deleted once both gates ran their steps concurrently over a 12-worker pytest: it measured 17.8s
against `check`'s 17.1s, so it was *slower than the gate it was a cheap substitute for*, and checked
less. `test-fast` survives as a standalone verb — `-m 'not external'` is what you want on a machine
with no `snakemake` — but it is no longer a rung, because a rung that saves nothing is a rung nobody
should be told to climb.

**Rung 1 is where you live.** A single file with `-k` is one to two seconds; a whole test file is
under ten. Nothing about a one-line change is learned by running 823 tests that a targeted run does
not tell you in a tenth of the time.

**Rung 3 is a rule, not a suggestion.** Once the PR is open, `.github/workflows/ci.yml` runs
`pixi run check` on every push. Running it again locally re-proves what CI is already proving and
tells you nothing new. Read the run.

## Which file tests the module you edited

Test files mirror packages, so rung 1 has an answer:

| you edited | run |
| ---------- | --- |
| `probe/` | `tests/test_probe.py` |
| `resolve/` (scoring, assign, escalate, geometry, window) | `tests/test_resolve.py` |
| `resolve/records.py` (the metadata resolver + provenance gate) | `tests/test_records.py` |
| `kb/` | `tests/test_kb.py` |
| `workflows/` (h5ad, qc, cram, fragments) | `tests/test_workflows.py` |
| `compose/`, `manifest/`, the workflow registry | `tests/test_compile.py` |
| `harvest/` | `tests/test_harvest.py`, `tests/test_extract.py` |
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
mostly real work — 727 tests against 822. Both it and `test` run under `pytest-xdist` — see below.

**One hole to know about.** `repo` is about what a test is *about*, not what it depends on, and
`tests/test_skills.py` is the case where the two come apart: it checks documentation, but it does so
by introspecting the live Typer app, so **renaming a CLI verb breaks it** — which is exactly what R6
names it to catch. Rung 2 cannot see that. If you rename or move a verb, run
`pytest tests/test_skills.py tests/test_docs.py` too, or go straight to rung 3.

There is deliberately **no `slow` marker**. It would be a hand-maintained list keyed on a number that
changes every time someone optimises, and nothing would go red when it drifted — a marker that lies
about cost is worse than no marker, because it is trusted.

`external` is applied by mechanism: `tests/conftest.py`'s `pytest_collection_modifyitems` marks
anything requesting a fixture that **spawns** — `real_wiring_gate` or `dry_run`. A hand-written list
would go stale silently, and the staleness would show up as `test-fast` spawning subprocesses nobody
meant it to.

**Keying that on "asked for the real gate" was wrong in both directions**, and both directions were
live: `test_compile.py` held a module-local `_dry_run` that spawned `snakemake` with no fixture at
all, so two tests shelling out to a binary we do not own were selected by `test-fast` and hard-failed
rather than skipped on a machine without it — the exact thing `test-fast` exists to avoid. Meanwhile
`test_compose_emits_a_snakefile_even_when_no_gate_runs` un-stubs the gate only to pass
`run_wiring_gate=False` and prove it never runs; it spawns nothing, costs 0.01s, and was excluded from
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

The worker count was measured, not chosen (2026-07-30, 48-core box, whole suite):

| workers | wall |
| ------- | ---- |
| serial | 80.2s |
| 8 | 19.9s |
| **12** | **15.3s** |
| 16 | 20.2s |
| `auto` (48) | 26.7s |

**More workers stop helping at twelve and then actively hurt.** Every worker pays the interpreter
import and rebuilds each session-scoped fixture — `synth_10x_v3` among them — so past the knee that
fixed cost grows faster than the remaining work shrinks. `-n auto` on this box means 48 workers and
is *slower than 12*; a bare `-n 12` on a 2-core runner oversubscribes it for the mirrored reason.
`auto` capped at 12 is the one spelling that is right in both places.

`--dist loadfile` was measured and rejected: it sits at ~28s at every worker count, because it hands
a whole file to one worker and `tests/test_compile.py` is then the floor.

### `loadgroup` is not the rejected `loadfile`

`test` and `test-fast` run `--dist=loadgroup`, and **that is not `loadfile` coming back.** `loadgroup`
behaves exactly like the default `load` for every test that carries no `xdist_group` mark — it splits
at test granularity, and `test_compile.py` still spreads across workers. It groups **only** what is
marked. Verified before adopting it: the whole suite under `--dist=loadgroup` with no marks present
gave an identical result and an identical wall. It is a safe swap; `loadfile` is not, because
`loadfile` groups *everything* whether or not the grouping pays.

Marking a module is how it opts in:

```python
pytestmark = pytest.mark.xdist_group("report-workspace")   # tests/test_report.py
```

**Why a module wants this.** A session- or module-scoped fixture is rebuilt once *per worker*, so
spreading a module that shares one expensive fixture buys parallelism by paying for the fixture again.
`tests/test_report.py` alone, on 8 pinned CPUs: 4.05s of CPU on one worker, 6.52s on two, 10.53s on
four, **20.60s on eight** — 5x the CPU for identical proof.

**Grouping is a trade, so it is decided per module by measurement**, never applied by default. It wins
where the fixture is expensive relative to the tests, and loses where the module holds many slow
independent tests: a grouped module runs *serially*, so its serial time becomes a floor on the suite
wall. What was measured (2026-07-30) and what it decided:

| module | fixture setup | serial | verdict |
| ------ | ------------- | ------ | ------- |
| `test_report.py` | 1.95s, read by all 16 tests | 4.8s | **group** — 8 setups became 1 |
| `test_partition.py` | 1.18s, read by 4 of 6 | 4.9s | **group** |
| `test_observation_sources.py` | 0.04s | 0.3s | leave — nothing to save |
| `test_records.py` | 0.01s | 1.2s | leave — nothing to save |
| `test_compile.py` (`synth_10x_v3`) | 0.05s | **22.3s** | **leave** — grouping it *is* the `loadfile` floor |

That last row is the rule in one line: `synth_10x_v3` is session-scoped and looks like the obvious
candidate, but it already costs 0.05s, and pinning the suite's longest file to one worker would take
the wall from 13.5s to over 22s. Measure the fixture against the module before reaching for the mark.

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

Measured on one box. The suite was **164s serial**; it is now **~15s on twelve workers** (78s if you
force it serial), and none of that came from making a test weaker:

| change | saved |
| ------ | ----- |
| the wiring gate paid once per workflow module instead of ~41 times per compose | ~39s |
| `kb.load_spec` cached, YAML parsed with `CSafeLoader` | ~41s |
| the 45 repeated manifest builds session-scoped | ~11s |
| `-n auto --maxprocesses 12` over what remained | ~63s |

Nothing here was a slow *test*. Every one was a fact being re-proved because the seam that owned it
sat on the wrong interface — which is the shape to look for when the number creeps back up.

## Adding a test

- It goes in the file that mirrors the module it tests (the table above). Do not start
  `tests/test_<the-issue-i-am-fixing>.py`.
- Shared setup belongs in `tests/conftest.py`. It owns the one FASTQ writer, the synthetic onlist
  registry, the fake range server, and the session-scoped `10x-3p-gex-v3` dataset.
- **What may be shared is immutable products only.** A manifest, a registry, a directory nothing
  writes into. Never a workspace a test writes into: `seqforge/cache/` makes resume implicit (R5), so
  a shared workspace lets a later test collect a cached `Observation` and pass for the wrong reason.
  Every test composes into its own `tmp_path`; a test that must mutate a shared build takes a
  `shutil.copytree` derivative (see `own_workspace` in `tests/test_report.py`).
- If the new test spawns a binary seqforge does not own, mark it `external`.
- Do not add a case that recomputes its expected value with the function's own formula. It cannot
  fail, and it reads as coverage.
