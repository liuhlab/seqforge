# Testing: run the narrowest thing that can go red

**Covers.** `tests/`.

`pixi run check` is a **pre-PR gate, not a per-edit one**.

This file is the rule. There are three steps and you climb them once per change, not once per edit.
**Steps, not rungs**: `CONTEXT.md` reserves *rung* for the escalation ladder that settles one field,
and the two ladders share small numbers without sharing anything else.

## The ladder

| # | When | Command | Cost |
| - | ---- | ------- | ---- |
| 1 | in the red→green loop | `pixi run -e test pytest tests/test_<module>.py -k <expr>` | seconds |
| 2 | before a commit, and before opening the PR | `pixi run check` | ~an order of magnitude more |
| 3 | after the PR is open | read CI | free |

**The selector is what makes step 1 step 1.** A run that names a file (or `-k`, `-m`, `--lf`) stays
serial, because spinning up twelve workers to run three tests costs more than it saves. Drop the
selector and you are running the whole suite — so `tests/conftest.py` fills in the worker flags for
you, and a bare `pytest` costs roughly a fifth of what a serial one would. There is nothing to
remember and no flag to type; an explicit `-n` of your own is always honoured.

**This file quotes ratios, not seconds.** Absolute timings and test counts go stale within a PR or
two, and a stale number in a doc is worse than none because it is still trusted. What survives is the
*shape* — which thing dominates which, and by roughly how much. Where a number below decides
something, it is a dated measurement of that decision, not a claim about today.

A `check-fast` step between the targeted run and the full gate was measured *slower than the gate it
was a cheap substitute for*, and checked less, so it was deleted. `test-fast` survives as a verb
rather than a step — `-m 'not external and not repo'` is what you want on a machine with no
`snakemake` — and `test-external` is its complement, the same marker on the other side of the `-m`,
run with the binaries that satisfy it on `PATH`.

**Step 1 is where you live.** A single file with `-k` is a second or two; a whole test file is a few.
Nothing about a one-line change is learned by running the whole suite that a targeted run does not
tell you in a fraction of the time.

**Step 3 is a rule, not a suggestion.** Once the PR is open, `.github/workflows/ci.yml` runs the
gate's four tasks on every push — `lint`, `fmt-check` and `typecheck` as three steps of one `lint`
job, `test` as a job of its own, all invoked directly and never through `scripts/check.sh`. Running
them again locally re-proves what CI is already proving. Read the run. CI also runs **four things the
gate does not**: `docs-build` (`mkdocs build --strict`, a fourth step of that `lint` job),
the `markdown` job (markdownlint on its own Node runner), the `build` job (`build` + `check-wheel`),
and `test (external binaries)` — `test-external` with liulab-runtime's aligner environment on
`PATH`, and the job that made the marker below stop meaning three things at once. A green
`pixi run check` is not a green CI.

**The runner itself is tested, because it can fail in ways no step can.** `pixi run check` is a
script that once collected no status, printed no verdict and **exited 0** on macOS's bash 3.2 — green,
having verified nothing, on the one host CI does not cover (#241). So `tests/test_repo_invariants.py`
drives it as a runner under every bash on the box. Nothing in it may rely on `set -e` or on an
associative array; `scripts/check.sh` carries the constraint where it binds.

## Which file tests the module you edited

Test files mirror packages, so step 1 has an answer:

| you edited | run |
| ---------- | --- |
| `probe/` | `tests/test_probe.py` |
| `resolve/` (scoring, assign, escalate, geometry, window) | `tests/test_resolve.py` |
| `resolve/records.py` (the metadata resolver) | `tests/test_records.py` |
| `resolve/provenance.py` (the wrong-PDF gate) | `tests/test_provenance.py` |
| `recordset.py` (the loader both dialects go through, and the `records new` draft) | `tests/test_recordset.py`; where a loaded set LANDS is `tests/test_records.py`, and the two verbs are in `tests/test_cli.py` |
| `kb/` | `tests/test_kb.py` |
| `manifest/` (fill, hash, validate, policy/precedence) | `tests/test_manifest.py` |
| `compose/` (plan, config, units, gates, params_gate) | `tests/test_compose.py` |
| `pipeline.py` (the compiled pipeline directory's one owner) | `tests/test_pipeline.py` |
| `workflows/` (registry + `.smk` source; h5ad, qc, cram, fragments, metrics, stats) | `tests/test_workflows.py` |
| tree-wide R10 AST guards (read `src/`, never compose) | `tests/test_repo_invariants.py` |
| `harvest/` | `tests/test_harvest.py`, `tests/test_extract.py` |
| `harvest/fields.py` (the asked/permitted vocabulary) | `tests/test_fields.py` |
| `harvest/meter.py` (the Exchange count, the Ceiling, the transcript) | `tests/test_extract.py` |
| `harvest/plan.py` (records → documents, the per-sample collapse, the near-identical collapse, the fan-out) | `tests/test_extract.py`; the token-boundary invariant itself is `tests/test_harvest.py`, and where a collapsed document's claims LAND is `tests/test_records.py` |
| `harvest/transcript.py` (the `.jsonl` round trip) | `tests/test_extract.py` |
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

**`external` has a job that satisfies it end to end, and seqforge declares none of the binaries.** It
used to mean three things at once, and one of them meant "runs nowhere": the STAR-gated tests —
including the end-to-end proof that a UMI tag survives into the aligner's own output — ran on **no
host this project's CI could reach**, for the life of the repo, and a skip is green, so nothing was
ever red about it (#333).

| | |
| --- | --- |
| the binaries | liulab-runtime's `align-rna` environment: `star` 2.7.11b, `samtools` 1.23.1, `htslib` 1.23.1, already pinned by the repo that owns them |
| the environment | `test`. The same one the main job builds — the only difference between the two jobs is what is on `PATH` |
| the verb | `PATH="<liulab-runtime>/.pixi/envs/align-rna/bin:$PATH" pixi run -e test test-external` |
| the CI job | `test (external binaries)`, which checks out `liuhlab/liulab-runtime` and `pixi install -e align-rna` from its lock |

Measured (2026-08-05): the selection is 25 tests, all 25 pass, ~28s at 12 workers — with STAR 2.7.11b,
the version the aligner test's docstring records measuring under. Nothing skips. Without those binaries
on `PATH` the same selection skips the three STAR-gated tests on every host, and the bgzip/tabix one
wherever htslib is absent — which is the CI runner, and is not the developer box this was first
measured on, and that difference is the whole reason a skip was never noticed.

**There was a `test-external` environment carrying `star`/`samtools`/`htslib`, and it was reverted
(#338).** Declaring them here made this repo an owner of an alignment environment, which four shipped
files say it is not and which the consumer rule forbids. It also could not be solved on `osx-arm64` —
bioconda's only Apple-silicon STAR needs a `libdeflate` older than this project's PDF stack — so it
was pinned to `linux-64`, and the maintainer's own machine could not build the environment its tests
needed. Borrowing the owner's already-locked environment costs one checkout and no table;
`test_no_dependency_table_declares_an_aligner` is what keeps it that way.

The job **re-runs** the snakemake-gated externals the `test` job already ran; that duplication is the
price of leaving the main job's selection alone, and it is the right way round — the job everyone
reads should not change shape because a second job exists.

The binaries are what run these tests, not the environment: `pixi run -e test test-external` on a host
without them reports green having skipped them. That is what the `PATH` prefix is for.

**The marker is applied per test, not per file**, and `external` is applied by mechanism:
`tests/conftest.py`'s `pytest_collection_modifyitems` marks anything requesting a fixture that
**spawns** — `real_wiring_gate` or `dry_run`, both of which live in `conftest.py` precisely so the
marker can see them, because a module-local spawner is a spawn the marker cannot see. A hand-written
list would go stale silently.

`repo` is about what a test is *about*, so one file can hold both kinds. `tests/test_docs.py` is fully
`repo` — it reads the site's prose, not `src/` — so add `pytest tests/test_docs.py` when you want the
doc-consistency check too.

There is deliberately **no `slow` marker**. It would be a hand-maintained list keyed on a number that
changes every time someone optimises, and nothing would go red when it drifted — a marker that lies
about cost is worse than no marker, because it is trusted.

## The verbs

```bash
pixi run -e test pytest tests/test_probe.py -k budget   # step 1
pixi run test-failed                                    # --lf --new-first -x: re-run what broke, worst first
pixi run check                                          # step 2: lint + fmt-check + typecheck + the full suite
pixi run test                                           # the suite alone, 12 workers
pixi run test-fast                                      # the suite minus what needs a binary we do not own
PATH="$LR/.pixi/envs/align-rna/bin:$PATH" pixi run -e test test-external   # ONLY that complement; $LR is a liulab-runtime checkout
```

## Parallelism: `-n auto --maxprocesses 12 --dist=loadgroup`

`test`, `test-fast` and `test-external` carry those three flags. **`addopts` deliberately does not**,
so step 1 stays serial — starting twelve workers to run three tests costs more than it saves — and
`tests/conftest.py` fills them in for a bare `pytest`, so there is nothing to remember and an explicit
`-n` of your own is still honoured.

**The cap of 12 is a decision, not an inference.** Swept 2026-08-01 on the pinned environment: past
the core count more workers only cost, and uncapped `auto` on a 48-core box bought 1.3x the wall of 12
for **3.2x its CPU** — while 16 would have been tolerable there. Twelve is what bounds one test run's
claim on a machine other people share; on a small box `auto` resolves below the cap and it never
binds. The tables, their noise, and what they could not establish:
[`docs/research/xdist-worker-sweep-2026-08-01.md`](../research/xdist-worker-sweep-2026-08-01.md).

**Group a module only after measuring the fixture against it.** `--dist=loadgroup` groups nothing that
carries no `xdist_group` mark, which is why it is not the rejected `loadfile`; and because a grouped
module runs *serially*, grouping one whose serial time approaches the suite wall re-creates by hand
the floor `loadfile` was rejected for. Which modules are grouped, the two questions to ask a
candidate, and why `loadfile` lost:
[`docs/adr/0038-loadgroup-over-loadfile-and-grouping-is-decided-per-module.md`](../adr/0038-loadgroup-over-loadfile-and-grouping-is-decided-per-module.md).

**One thread per worker.** The `test` feature's *activation environment* pins `OMP_NUM_THREADS`,
`OPENBLAS_NUM_THREADS` and `MKL_NUM_THREADS` to `1` — three names because three libraries are
underneath, and every worker is a process that would otherwise size its pools to the whole box and
then fight the other eleven for it. It sits on the environment rather than a task so every invocation
inherits it, and `tests/test_repo_invariants.py::test_the_test_environment_pins_its_thread_pools`
holds it open at both ends: the declaration, and the value a running test can actually see. The second
end is the one that matters — a declaration that never reaches the workers looks identical to success
from the configuration side.

## Why there is no test-impact analysis

`pytest-testmon` and friends track **Python lines**, and a large share of this suite's behaviour is
data-driven: `kb/specs/*/spec.yaml`, the `.smk` modules, the packed onlists. Editing a spec touches no
Python, so impact analysis selects zero tests and reports green — silent under-selection, which is the
exact failure mode this project refuses everywhere else. See
[`docs/adr/0002-no-test-impact-analysis.md`](../adr/0002-no-test-impact-analysis.md).

## What the suite costs, and why

The suite is **more than an order of magnitude faster than it was**, and none of that came from making
a test weaker. Nothing removed was a slow *test*. Three shapes account for all of it, and they are
what to look for when the wall creeps back up.

**A fact being re-proved, because the seam that owned it sat on the wrong interface.** Almost every
win was this. The cleanest example: `wiring_gate` returned a four-character verdict while its
implementation held the whole `snakemake -n -p` plan text, so every test that wanted the plan spawned
its own. The fix was to expose what was already computed, not to run less.

**Contention rather than duplication.** Pinning the workers' thread pools removed no work at all —
the same work was being spent fighting over the machine instead of doing it. When the wall swings
between repeats rather than sitting high, look for contention before looking for waste.

**An indivisible block xdist cannot spread.** Parametrizing `test_corpus_is_green` per case removed
**no CPU at all**; it split the suite's longest single item into items. When utilisation is the
problem, look for the block that cannot be split before looking for work to delete — the marginal
value of deleting one test is its duration ÷ the worker count, which is almost always less than it
looks.

**If this number creeps back up, find the fact being re-proved — do not add workers.** Parallelism
landed last on purpose: it hides waste rather than removing it.

## Adding a KB spec: what it costs the suite

The KB sweeps grow with the spec count, and the *shape* of each term matters more than any timing
(#112) — a spec count anchored in this page has gone stale twice, and `ls src/seqforge/kb/specs/` is
the live one. The whole KB partition is a few seconds; the terms that grow are these:

- The **R8 anchors** are the price of "every KB entry is executable and self-testing," and you do not
  buy them down. `test_every_kb_spec_roundtrips` is O(n) and *is* the rule. The benign-twin
  biconditional is O(n²) with a microsecond constant. `test_no_spec_pair_is_confusable_without_declaring_it` is O(n²)
  **but geometry-pre-gated** — it checks the cheap `geometry_could_accept` (µs) before paying the
  scorer, which is ~100× and is the pattern to copy. Never narrow their generative axes to hit a clock.
- The two other O(n²) sweeps — `test_geometry_could_accept_is_necessary_for_rung02_acceptance` and
  `test_descent_narrowing_never_drops_a_valid_spec` — **cannot take that pre-gate**, and their
  docstrings say why: the first's *subject* is the pre-gate predicate (gating it is circular), and the
  second already gates on `length_feasible` (which *is* `geometry_could_accept`) and then scores
  exactly the pairs the gate excludes, because "an excluded spec never wins" is its whole point. Both
  are sub-second after #105's shared `kb_probes` fixture — the fixture, not a narrower axis, is what
  keeps them affordable as the KB grows.
- The single highest-leverage change was **sharing `kb_probes`** (#105): a per-spec probe rebuilt once
  per worker was the dominant term of the family/descent sweeps, and grouping it under
  `xdist_group("kb-probes")` cut it ~90%.

So when a KB sweep's wall creeps up as specs are added, look first for a probe rebuilt per pair, then
for a missing `geometry_could_accept` pre-gate — not for an axis to narrow. The O(n²) terms are the
ones that will eventually decide this, and they are the ones the shared fixture and the pre-gate
already hold down.

## Adding a test

- It goes in the file that mirrors the module it tests (the table above). Do not start
  `tests/test_<the-issue-i-am-fixing>.py`.
- Shared setup belongs in `tests/conftest.py`. It owns the one FASTQ writer, the synthetic onlist
  registry, the fake range server, the `snakemake` dry run, and the session-scoped immutable products:
  the `10x-3p-gex-v3` / `bulk-rnaseq` / `splitseq` / `smartseq3` datasets, the per-spec `kb_probes`
  sweep, the `src_trees` AST parse, and the composed-and-planned `composed_plate` (which builds on the
  `smartseq3` one rather than repeating it).
- **A fixture that hands code a model must hand it one the loader would accept.** `model_copy` runs
  no validator, so a fixture built that way can express a shape the real schema refuses — and then
  the thing under test is proved against something that cannot exist, green *because* of the defect.
  `declare_read_floor` is the worked example: it re-validates what it produces, so a collision goes
  red at the fixture instead of hiding. Where a *deliberately* unloadable model is the subject, say
  so at the fixture — `tests/test_resolve.py`'s plate stand-in does, because the control case it
  needs is one the schema makes unsayable.
- **What may be shared is immutable products only.** A manifest, a registry, a directory nothing
  writes into. Never a workspace a test writes into: `seqforge/cache/` makes resume implicit (R5), so
  a shared workspace lets a later test collect a cached `Observation` and pass for the wrong reason.
  Every test composes into its own `tmp_path`; a test that must mutate a shared build takes a
  `shutil.copytree` derivative (see `own_workspace` in `tests/test_report.py`).
- If the new test spawns a binary seqforge does not own, mark it `external`.
- Do not add a case that recomputes its expected value with the function's own formula. It cannot
  fail, and it reads as coverage.
