# AGENTS.md

The router — `CLAUDE.md` is a symlink to this file: one canonical copy, no fork. Read it in full;
everything else is looked up. Terms are defined once in [`CONTEXT-MAP.md`](CONTEXT-MAP.md) — a
shared kernel of the words every context uses, plus one `CONTEXT.md` per bounded context —
and decisions once in [`docs/adr/`](docs/adr/), the system-wide ones there and the rest beside the
code they govern. This file points at both and restates neither.

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

Imperatives only. Each row cites the records that decided it **by number, never by path** — a record
moves between directories and its number does not; `—` means no record cites that rule today.

| # | Rule | Records |
|---|---|---|
| R1 | **Emit data, never code.** No LLM writes Snakefile or rule source; LLM output validates against an exported JSON Schema. | ADR-0008 |
| R2 | **Agents propose, code decides — and refusal is an exit code.** Nothing enters a manifest unvalidated; every `Assertion` quote must grep back *and* entail its value. | ADR-0008, ADR-0009, ADR-0013, ADR-0020 |
| R3 | **Never read a whole FASTQ.** Every FASTQ touch goes through `probe.streaming.BoundedReader`, bounded by `--max-reads` **and** `--max-bytes`. Never write a second budget loop. | ADR-0001 |
| R4 | **Three truths, never merged.** Interpretive fields are `Evidenced`; observed↔asserted disagreement is a surfaced `Conflict`; one judgement = one envelope. | ADR-0006, ADR-0007 |
| R5 | **Disk is state, context is cache.** Every stage writes a resumable, content-addressed artifact under `seqforge/` — **no leading dot**, because it holds the manifest and the Snakefile: output, not scratch, and only `seqforge/cache/` is safe to delete. Resume is implicit and there is no `--resume`. | ADR-0013, ADR-0015 |
| R6 | **The CLI is the API; the skill is a thin client.** Every skill action maps to a deterministic `seqforge <verb>` emitting JSON on stdout by default. | ADR-0013 |
| R7 | **Machine-independent manifest — no absolute paths, ever.** Genome = UCSC id + registered GTF name; software = a `liulab-runtime` env name; data = a URI. | — |
| R8 | **Every KB entry is executable and self-testing.** Each tech ships a `spec.yaml` that `kb roundtrip` proves recovers what it declares. | — |
| R9 | **Cheap first, expensive only on ambiguity.** Rungs 0–3 by default; escalate past 3 only on a processing-divergent tie. Record which rung resolved each field. | ADR-0011 |
| R10 | **Consumer, not parallel universe.** Never define genome-file machinery, and never let a rule or the wheel resolve an aligner from our tables — they belong to `liulab-genome` (assemblies by UCSC id, and an annotation is a **registered GTF `name`**: it fetches no annotations, so seqforge stages the GTF and calls `register_gtf(gtf, name)`) and to `liulab-runtime` (an aligner environment is its **literal** name — `align-rna`, `align-dna`, `ml`, `ml-gpu` — with no profile indirection). The one exception is `test-star`, which ships nowhere and no rule can see. | ADR-0046 |
| R11 | **Two artifacts, and the instructable surface is closed.** The dataset is write-once, the recipe plural; parse-keys and count-keys are disjoint; produce every answer rather than ask. | ADR-0004, ADR-0005, ADR-0011 |

## Where to read next

- **A term, or a synonym to avoid** — [`CONTEXT-MAP.md`](CONTEXT-MAP.md) and the five per-context
  `CONTEXT.md` files it lists under `src/seqforge/probe|harvest|kb|resolve|compose/`.
- **A decision — why it is this way, and what lost** — [`docs/adr/`](docs/adr/), or
  `src/seqforge/<context>/docs/adr/`; filenames are unique, so `find . -name '0008-*.md'` finds one.
- **A measurement — a number, and the method that produced it** — [`docs/research/`](docs/research/),
  dated. A measurement is not a decision: it goes here, and whatever it *decided* goes to a record.

**A term or a record is the exception.** Before adding either, the answer must not be readable from
the code. A glossary entry is one or two sentences; a record is one paragraph and clears all three
of hard-to-reverse, surprising-without-context, and a real trade-off — prefer editing an existing
entry to adding a neighbour. **This bar is held at review and nothing mechanises it**; a line-cap
test was considered and declined, and is the fallback if the bar drifts.

## Working here

- **Tests: run the narrowest thing that can go red.** `pixi run -e test pytest tests/test_<mod>.py
  -k <expr>` in the loop (files mirror packages), `pixi run check` once before the PR, then read CI.
  Two markers, both semantic: `external` (a binary seqforge does not own) and `repo` (repo
  consistency, not `src/` behaviour).
- **Comments: name the idea, never the number** — no rule number and no document section number in
  `src/`, `tests/`, `skills/`, `evals/` or `pyproject.toml`; a guard in
  `tests/test_repo_invariants.py` fails on one. A number is a mutable label; write the term instead.
- **Versioning: CalVer (`YYYY.M.PATCH`), never SemVer** — including every component stamp
  (`PROBE_VERSION`, `kb_version`, `resolve_version`, `workflow_version`), precisely *because* they
  fold into `run_id` and the content-addressed caches: a date-stamped identity, not a promise.
- **Issues live on GitHub**, and the five triage labels are
  [`.github/ISSUE-CONVENTIONS.md`](.github/ISSUE-CONVENTIONS.md).
