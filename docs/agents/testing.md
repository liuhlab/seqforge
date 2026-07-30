# Testing: run the narrowest thing that can go red

`pixi run check` is a **pre-PR gate, not a per-edit one**. CLAUDE.md used to say to run it "when you
change behaviour", which is every edit, and offered no other verb — so the loop was: edit one line,
run the whole suite, open the PR, and let CI run the identical suite again.

This file is the rule. There are four rungs and you climb them once per change, not once per edit.

## The ladder

| # | When | Command | Cost |
| - | ---- | ------- | ---- |
| 1 | in the red→green loop | `pixi run -e test pytest tests/test_<module>.py -k <expr>` | **~2s** |
| 2 | before a commit | `pixi run check-fast` | ~60s |
| 3 | before opening the PR | `pixi run check` | **once** — ~80s |
| 4 | after the PR is open | read CI | free |

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
| `fingerprint/` | `tests/test_fingerprint.py` |
| `report/` | `tests/test_report.py` |
| `models/` | `tests/test_models.py` |
| `cli.py`, `cli/` | `tests/test_cli.py` |

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
mostly real work. Its value is that it skips what a source edit cannot break.

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

## Why there is no test-impact analysis

`pytest-testmon` and friends track **Python lines**, and a large share of this suite's behaviour is
data-driven: `kb/specs/*/spec.yaml`, the `.smk` modules, the packed onlists. Editing a spec touches no
Python, so impact analysis selects zero tests and reports green — silent under-selection, which is the
exact failure mode this project refuses everywhere else. See
[`docs/adr/0002-no-test-impact-analysis.md`](../adr/0002-no-test-impact-analysis.md).

## What the suite costs, and why

Measured on one box, serial. The suite was **164s**; it is now **~73s**, and none of that came from
making a test weaker:

| change | saved |
| ------ | ----- |
| the wiring gate paid once per workflow module instead of ~41 times per compose | ~39s |
| `kb.load_spec` cached, YAML parsed with `CSafeLoader` | ~41s |
| the 45 repeated manifest builds session-scoped | ~11s |

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
