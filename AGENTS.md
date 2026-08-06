# AGENTS.md

The router — `CLAUDE.md` is a symlink to this file: one canonical copy, no fork. Read it in full, then
read a pointer-table row only when you touch that area. Terms are defined once in
[`CONTEXT.md`](CONTEXT.md) and decisions once in [`docs/adr/`](docs/adr/); this file points at both.

## What this is

`seqforge` is a **compiler, not a chatbot**. It turns `(arbitrary FASTQ files) + (unstructured
human/DB metadata)` into a validated, machine-independent **`manifest.yaml`** (what the data IS —
content-addressed, immutable), then compiles that under a **`processing.yaml`** (the recipe: what to
DO with it) into a runnable Snakemake config — headless, across ~10⁴ public datasets, into a
genomic-AI corpus. Same manifest + a different recipe = a different pipeline, hash unchanged; the last
artifact is the point, **a Snakefile the user submits**, and `seqforge` does not submit jobs. It is
implemented and green (`pixi run check`); the demo dataset PRJNA1027859 compiles end to end.

Deterministic code owns every decision. The LLM has exactly **two** jobs: (a) parse prose into
span-verified `Assertion`s; (b) arbitrate ambiguity code has *already flagged* (modelled, verb
unbuilt). Instruction-following is **not** a third job, and sample metadata is no third job either —
both enter as `Assertion`s carrying a quote that greps back, and code applies precedence. Everything
else is a verifier; don't blur that line. An assay that seems to need the model to **act** is the
signal to re-read this paragraph, not to relax it.

```text
probe(files)                    -> Observation   bytes only, no LLM, no network; `io probe-remote`
                                                 is the same bounded read against a URL
records(accession?)             -> ArchiveRecord project/sample/experiment/run. OPTIONAL — most
                                                 sequencing data never had an accession
harvest(documents)              -> Assertion     LLM, span-verified. A document is a file you handed
                                                 us OR one archive record
──────────────────────────────────────────────────────────────────────────────────────────────
score(Observation, KB, hypo?)   -> candidates x role_assignment, Conflicts, Questions. What the
                                   library IS, from BYTES; a hypothesis breaks ties and never enters
                                   the evidence matrix. BLOCKS on observed↔asserted
resolve_metadata(FileIdentity,  -> samples x attributes, Conflicts, Warnings. Which sample each file
                 records?,         is and what it was, from RECORDS + PROSE, no probe signal.
                 assertions?)      DECIDES, and only WARNS (ADR-0010)
──────────────────────────────────────────────────────────────────────────────────────────────
  => manifest.yaml     THE IR.    What the data IS. One per dataset. Immutable, hashed.
plan(Assertions, flags, policy) -> ProcessingSection   precedence: flag > instruction > policy
  => processing.yaml   THE FLAGS. What to DO with it. Many per dataset. Sparse; empty is legal.
compose(manifest, processing)   -> Snakefile + config + units.tsv   deterministic
  => seqforge/pipeline/<recipe>-<run_id[:12]>/Snakefile   THE DELIVERABLE. run_id = H(dataset ⊕
     processing ⊕ kb ⊕ workflow); running it ends in <sample>.h5ad, `rule all` demanding the
     matrices and not a folder. `seqforge run` chains the whole diagram headless, stops at the
     first refusal, adds no authority, and has no `--resume` — each stage resumes from its cache.
```

## Non-negotiable rules: R1–R11

Imperatives only. Rationale, and the file enforcing each: [`docs/agents/rules.md`](docs/agents/rules.md).

| # | Rule |
|---|---|
| R1 | **Emit data, never code.** No LLM writes Snakefile or rule source; LLM output validates against an exported JSON Schema. |
| R2 | **Agents propose, code decides — and refusal is an exit code.** Nothing enters a manifest unvalidated; every `Assertion` quote must grep back *and* entail its value. |
| R3 | **Never read a whole FASTQ.** Every FASTQ touch goes through `probe.streaming.BoundedReader`, bounded by `--max-reads` **and** `--max-bytes`. Never write a second budget loop. |
| R4 | **Three truths, never merged.** Interpretive fields are `Evidenced`; observed↔asserted disagreement is a surfaced `Conflict`; one judgement = one envelope. |
| R5 | **Disk is state, context is cache.** Every stage writes a resumable, content-addressed artifact under `seqforge/`; resume is implicit and there is no `--resume`. |
| R6 | **The CLI is the API; the skill is a thin client.** Every skill action maps to a deterministic `seqforge <verb>` emitting JSON on stdout by default. |
| R7 | **Machine-independent manifest — no absolute paths, ever.** Genome = UCSC id + registered GTF name; software = a `liulab-runtime` env name; data = a URI. |
| R8 | **Every KB entry is executable and self-testing.** Each tech ships a `spec.yaml` that `kb roundtrip` proves recovers what it declares. |
| R9 | **Cheap first, expensive only on ambiguity.** Rungs 0–3 by default; escalate past 3 only on a processing-divergent tie. Record which rung resolved each field. |
| R10 | **Consumer, not parallel universe.** Never define genome-file machinery or aligner environments here — they belong to `liulab-genome` / `liulab-runtime`. |
| R11 | **Two artifacts, and the instructable surface is closed.** The dataset is write-once, the recipe plural; parse-keys and count-keys are disjoint; produce every answer rather than ask. |

## Where to read next

| when you touch | read |
|----------------|------|
| any rule — its rationale, and the file that enforces it | [`docs/agents/rules.md`](docs/agents/rules.md) |
| tests: which one to run, the two markers, what the suite costs | [`docs/agents/testing.md`](docs/agents/testing.md) |
| pixi, ruff, mypy, CalVer, mkdocs, GitHub Discussions | [`docs/agents/toolchain.md`](docs/agents/toolchain.md) |
| where a module lives; the `liulab-genome` / `liulab-runtime` contracts | [`docs/agents/layout.md`](docs/agents/layout.md) |
| the `seqforge/` output tree, its caches, and the hooks | [`docs/agents/state.md`](docs/agents/state.md) |
| where a new piece of writing goes — a rule, a decision, a term, or a measurement | [`docs/adr/README.md`](docs/adr/README.md) |
| issues, and the five triage labels | [`docs/agents/issue-tracker.md`](docs/agents/issue-tracker.md) |
| a comment in `src/`, `tests/`, `skills/`, `evals/` or `pyproject.toml` | [`docs/agents/comments.md`](docs/agents/comments.md) |
| `models/`: the decisions behind the schemas (`schema export` is the schema) | [`docs/agents/models.md`](docs/agents/models.md) |
| a KB entry: `spec.yaml`, confusability, the round-trip, what is covered | [`docs/agents/kb.md`](docs/agents/kb.md) |
| scoring: the evaluators, the evidence matrix, the escalation ladder | [`docs/agents/resolve.md`](docs/agents/resolve.md) |
| harvest: the module flow, the two span marks, the send list vs what reaches disk | [`docs/agents/harvest.md`](docs/agents/harvest.md) |
| a CLI verb: the stream split, the exit codes, what costs network or a model | [`docs/agents/cli.md`](docs/agents/cli.md) |
| the demo dataset, the benchmark tiers, the compose gate and what it measured | [`docs/agents/eval-corpus.md`](docs/agents/eval-corpus.md) |
