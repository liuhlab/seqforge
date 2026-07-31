# Testing: run the narrowest thing that can go red

`pixi run check` is a **pre-PR gate, not a per-edit one**. CLAUDE.md used to say to run it "when you
change behaviour", which is every edit, and offered no other verb — so the loop was: edit one line,
run the whole suite, open the PR, and let CI run the identical suite again.

This file is the rule. There are four rungs and you climb them once per change, not once per edit.

## The ladder

| # | When | Command | Cost |
| - | ---- | ------- | ---- |
| 1 | in the red→green loop | `pixi run -e test pytest tests/test_<module>.py -k <expr>` | **~2s** |
| 2 | before a commit | `pixi run check-fast` | ~18s |
| 3 | before opening the PR | `pixi run check` | **once** — ~17s |
| 4 | after the PR is open | read CI | free |

Those two middle numbers are not a typo, and the honest reading is that **rung 2 has stopped earning
its place**. `test-fast` deselects 95 of 823 tests; once both gates run their steps concurrently and
pytest itself runs on twelve workers, what remains is dominated by the tests they share. Rung 2 is
kept for now because `external` still means something real — it is what you want on a machine with no
`snakemake` — but if it stays inside noise of rung 3, the right move is to delete it and let the
ladder have three rungs.

**Rung 1 is where you live.** A single file with `-k` is one to two seconds; a whole test file is
under ten. Nothing about a one-line change is learned by running 819 tests that a targeted run does
not tell you in a fiftieth of the time.

**Rung 4 is a rule, not a suggestion.** Once the PR is open, `.github/workflows/ci.yml` runs
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

`external` is applied by mechanism where it can be: `tests/conftest.py`'s
`pytest_collection_modifyitems` marks anything requesting the `real_wiring_gate` fixture, because
asking for the real gate *is* asking to spawn `snakemake`. A hand-written list would go stale
silently, and the staleness would show up as `test-fast` spawning subprocesses nobody meant it to.

## The verbs

```bash
pixi run -e test pytest tests/test_probe.py -k budget   # rung 1
pixi run test-fast                                      # rung 2's test half
pixi run check-fast                                     # lint + typecheck + test-fast
pixi run test-failed                                    # --lf --new-first -x: re-run what broke, worst first
pixi run check                                          # rung 3: lint + fmt-check + typecheck + the full suite
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
a whole file to one worker and `tests/test_compile.py` is then the floor. The default `--dist load`
splits at test granularity.

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
