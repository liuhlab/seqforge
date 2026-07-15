# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

`seqforge` is a **compiler, not a chatbot**. It turns `(arbitrary FASTQ files) + (unstructured
human/DB metadata)` into a validated, machine-independent **dataset manifest** — the IR — and then
compiles that IR, under a **processing manifest** (the recipe: our flags), into a runnable Snakemake
config. Headless, across ~10⁴ public sequencing datasets, into a genomic-AI training corpus.

The two-input shape is the metaphor doing work, not decorating. A finished assay is immutable, so the
IR is content-addressed and never rewritten; what you *do* with it is a choice, so it lives in flags.
Same IR + different flags = different binaries; same dataset + different processing manifests =
different pipelines — GeneFull instead of Gene, a different genome build — with the manifest untouched
and its hash unchanged. `-O2` does not get to edit the IR, and neither does a processing manifest.

Deterministic code owns every decision. The LLM still has exactly **two** jobs: (a) parse prose into
span-verified `Assertion`s; (b) arbitrate ambiguity that the deterministic layer has *already flagged*.
Instruction-following is **not** a third job. "This dataset should be aligned in GeneFull mode" is
prose, so it enters as an `Assertion` on a `processing.*` field carrying a quote that greps back —
exactly like an organism does. Precedence over policy is applied by code, in `plan`. That the
instructable path adds no new LLM authority is the whole point: **we can accept instructions because
we never trust the model to *act* on them, only to *find* them.** Everything else is a verifier — do
not let that line blur.

**Status: pre-implementation.** The authoritative design is [`docs/design.md`](docs/design.md)
(Pydantic model hierarchy, KB `spec.yaml` schema, scoring function + joint role-assignment, CLI
surface); the full rationale is [`PROJECT_BRIEF.md`](PROJECT_BRIEF.md). Read both before writing
code. The pipeline is a five-stage compiler over two artifacts:

```
probe(files)                    -> Observation   deterministic, no LLM, no network, bytes only
harvest(prose, instructions)    -> Assertion     LLM, each claim span-verified
score(Observation, KB, hypo?)   -> candidates x role_assignment, Conflicts, Questions
──────────────────────────────────────────────────────────────────────────────────────────────
  => manifest.yaml     THE IR.    What the data IS.   One per dataset. Immutable, hashed.

plan(Assertions, flags, policy) -> ProcessingSection   precedence: flag > instruction > policy
──────────────────────────────────────────────────────────────────────────────────────────────
  => processing.yaml   THE FLAGS. What to DO with it. Many per dataset. Sparse; empty is legal.

compile(manifest, processing)   -> config + workflow-module selection   deterministic
──────────────────────────────────────────────────────────────────────────────────────────────
  run_id = H(dataset ⊕ processing ⊕ kb ⊕ workflow). Same manifest + different recipes =
  different pipelines, dataset hash unchanged.
```

## Non-negotiable rules (checked, not aspirational)

These encode `PROJECT_BRIEF.md` §3 principles 1–14 (the numbering is deliberately offset: brief #1 →
R2, brief #2 → R1, and R12 derives from §13, not §3). Each names how it is enforced; a change that
violates one must change the rule here first.

| # | Rule | Enforced by |
|---|---|---|
| R1 | **Emit data, never code.** No LLM generates Snakefile/rule source. LLM output must validate against an exported JSON Schema. The composer emits `config.yaml` / `units.tsv` / a module selection — never rule source. | CI grep + composer unit tests |
| R2 | **Agents propose, code decides.** No field enters the manifest without passing a validator. LLM `confidence` is advisory and never overrides observed bytes. | Pydantic validators; `manifest validate` |
| R3 | **Never read a whole FASTQ.** Every FASTQ touch is bounded by `--max-reads` (default 200 000) **and** `--max-bytes` (256 MB decompressed). Wall-clock is never a budget. A code path that *can* stream a whole multi-GB FASTQ is a bug. | `PreToolUse` hook + CI "50 GB reads < N bytes" |
| R4 | **Refusal is an exit code.** `manifest validate` returns structured `Blocker`s + a nonzero exit. Code decides whether we may compile; the LLM only decides *what question to ask*. | exit-code contract + `PostToolUse` hook |
| R5 | **Span-verified extraction.** Every `Assertion` carries a `quote` that (a) greps back into the normalized canonical text and (b) *entails* its value; else reject. This is the hallucination tripwire. | `harvest verify` (deterministic) |
| R6 | **Three truths, never merged.** Every manifest field is `Evidenced{value, basis, evidence, confidence, rung}`. `observed`↔`asserted` disagreement is a surfaced first-class `Conflict`, never auto-picked. The three truths are the three *bases* — nothing here ever depended on there being three *sections*, and conflating the two is what let `processing` masquerade as a truth for a year. | `Evidenced[T]` type; resolver |
| R7 | **Disk is state, context is cache.** Every stage writes a resumable, content-addressed artifact under `.seqforge/`. Any run is resumable after a kill; the agent never holds state only in context. | artifact layout + `--resume` |
| R8 | **The CLI is the API; the skill is a thin client.** Every skill action maps to a deterministic `seqforge <verb> --json` that runs with no LLM in the loop. Only `harvest extract` (and opt-in `resolve adjudicate`, off by default) touch an LLM. | determinism ledger + CI |
| R9 | **Machine-independent manifest — no absolute paths, ever.** Genome = UCSC assembly id + registered GTF name; software = a literal `liulab-runtime` env name; data = a URI. Everything resolves at run time. | `Uri` validator + `PreToolUse` `/scratch` guard |
| R10 | **Every KB entry is executable and self-testing.** Each technology ships a `spec.yaml`; `kb roundtrip` (spec→synth→probe→recover) and `kb confusability` gate CI. A new tech that silently collides with an existing one blocks the merge. | `kb roundtrip` / `kb confusability` in CI |
| R11 | **Cheap first, expensive only on ambiguity.** Default path is escalation-ladder rungs 0–3; rungs 4+ only when metadata is absent, rung-3 disagrees, or a `Conflict` surfaces. Record which rung resolved each field. | `resolve score` rung provenance |
| R12 | **Consumer, not parallel universe.** Never define genome-file machinery or aligner environments here — they belong upstream in `liulab-genome` / `liulab-runtime`. If a feature belongs in one of those, it goes there. | code review + CI import check |
| R13 | **The dataset is immutable; the recipe is plural.** `manifest.yaml` (library + experiment) is what the data *is* — content-addressed, write-once. `processing.yaml` is what to *do* with it, and a dataset carries as many as we care to run. `run_id = H(dataset ⊕ processing ⊕ kb ⊕ workflow)`; a change of intent must **never** perturb `dataset_hash`. If re-running a dataset with a different aligner edits `manifest.yaml`, that is a bug. | `dataset_content_hash` covers 2 sections; recipe-sweep hash-invariance test; `models/{dataset,processing}.py` import-graph test; `compose` refuses a mismatched pin |
| R14 | **The instructable surface is closed, and the line is parse vs count.** `backend.params` says how to **parse** reads (soloType, CB/UMI offsets, whitelist, strand) — byte-decided, never instructable. The processing manifest says what to **count**, and against which genome, aligner, env, resources. The two key sets are **disjoint**, which is what makes "a user instruction contradicts the observed bytes" *inexpressible* rather than merely deprioritized: the user has no vocabulary in which to say it. | `Backend` key-allowlist validator (`kb lint` + every `load_spec`); `params_gate` disjointness + coverage + per-owner faithfulness |
| R15 | **Produce every answer rather than ask.** §12 says never escalate an ambiguity that cannot change the output; this is its sibling — never escalate one whose every answer you can afford to emit. `soloFeatures` defaults to all five: one alignment, five counting rules, one pass. Escalate only where the answers are genuinely exclusive (a genome, an aligner). An ambiguity a second counting rule would settle is not a `Question`. | `kb e2e-introns` with the override deleted (`composed_soloFeatures ⊇ {Gene, GeneFull}` on the compiler's own params); eval "questions asked" |

**Held-out acceptance cases — do not touch.** `PRJNA1027859` is the pilot's single real acceptance
test (more will follow). Do not read, sample, list, stat, profile, or tune against a held-out case.
Build against synthetic KB round-trip fixtures only. Each runs **once**, pre-registered, when the
maintainer says so. **Their on-disk locations are deliberately not in this repo** — they live in
local, out-of-git config (`~/.claude/`), so this public repo carries the *rule*, not the paths. A
`PreToolUse` guard blocks access to the held-out roots (e.g. `/scratch/**`).

## Toolchain

Everything runs through **pixi** (do not use `pip`/`conda`/`venv` directly). Conventions follow the
sibling repos; `seqforge` is the family's first real Python library, so it also introduces mypy and a
test task (no in-family precedent — see below).

```bash
pixi install                 # build environments
pixi run test                # pytest (unit + KB round-trip + composer dry-run gates)
pixi run test -- -k <expr>   # a single test / subset
pixi run lint                # ruff check .
pixi run fmt                 # ruff format .
pixi run typecheck           # mypy --strict on models/ and probe/
pixi run docs-build          # mkdocs build --strict
pixi run -- pre-commit install   # once per clone
```

- **Lint/format** (from `liulab-runtime`): ruff `line-length=100`, `target-version=py312`,
  `select=[E,W,F,I,UP,B]`, `ignore=[E501]`; pre-commit = std hooks + ruff + shellcheck.
- **Typing** (new, per brief §13): `[tool.mypy]` **strict on `models/` and `probe/`** — the two
  modules where a wrong type silently poisons the corpus. No in-family precedent; introduce it.
- **Tests:** pytest, `syrupy`/inline snapshots for golden manifests, `hypothesis` for the synthetic
  generator. No in-family `test`/`typecheck`/`build` pixi tasks exist yet — add them.
- **Packaging** (from `liulab-genome`): hatchling, `src/` layout, `requires-python>=3.12`,
  channels `conda-forge` + `bioconda`. Versioning is **CalVer `YYYY.M.PATCH`** — static in
  `[project].version` + `CHANGELOG.md` (patch resets monthly). **Component/tool-stamp versions use
  CalVer too** (`PROBE_VERSION`, `kb_version`, `resolve_version`, `workflow_version`), since they are
  folded into content-addressed cache keys — never SemVer.
- **Docs:** mkdocs-material → gh-pages, same as the sibling repos.

## Repository layout (planned — brief §4)

Single repo, single `pyproject.toml`, clear internal module boundaries; do **not** split into
separate distributions.

```
models/     pydantic v2 schemas; JSON Schema export is the single source of truth
probe/      deterministic FASTQ fingerprinting (no LLM, no network)
kb/         knowledge base: one directory per technology (spec.yaml + README.md)
resolve/    candidate scoring, role assignment, confusability, escalation
manifest/   fill/validate/hash both artifacts; `policy.py` is where precedence lives (R13/R15)
compose/    (dataset, processing) -> snakemake config + module selection
io/         remote peeking, ENA/SRA/GEO/SDL resolution, pooch-cached onlists
workflows/  hand-written, versioned, CI-tested Snakemake modules (NOT generated)
cli/        typer app; every command supports --json
skills/     SKILL.md agent skills + installer
evals/      ground-truth corpus + harness
tests/
```

## Consumer of the existing liulab stack

- **`liulab-genome`** — distribution `liulab-genome`, import name `genome` (`from genome import
  Genome`). Reference assemblies by UCSC id (`sacCer3`, `ce11`, `hg38`); annotation version = a
  **registered GTF `name`** — liulab-genome does **not** fetch annotations, so seqforge stages the
  GTF and calls `register_gtf(gtf, name)`. STAR index via `Genome(assembly).build_star_index(gtf=name)`;
  data root env var `LIULAB_DATA`. Never write a genome path into a manifest.
- **`liulab-runtime`** — reference an aligner environment by its **literal** name
  (`align-rna`, `align-dna`, `ml`, `ml-gpu`) → `pixi run -e <name>` or
  `ghcr.io/liuhlab/liulab-runtime:<name>`. There is **no** profile-indirection layer — the env name
  *is* the identifier. Do not define aligner environments here.

## On-disk state (`.seqforge/`, resumable + content-addressed)

Per-file `Observation` keyed by file `sha256`; dataset `candidates` keyed by
`sha256(sorted(file_shas) ⊕ kb_version)` (with `probe_version`/`resolve_version` folded in).
`manifest.yaml` is written only after a clean `manifest validate`.

Compiled output lives under `.seqforge/pipeline/<run_id>/` — keyed by the **run**, not the workspace,
because one dataset compiled two ways is two runs and a fixed path would silently overwrite the first
with the second. Each run dir carries `config.yaml`, `units.tsv`, materialized onlists, and
`processing.lock.yaml`: the fully-resolved, dataset-**bound** processing manifest that produced it.
`compose` writes the lock even when it was handed no `--processing`, because R7 says disk is *state*,
not that disk is *input* — a mandatory recipe file per dataset would mean 10⁴ boilerplate files nobody
reads.

`journal.jsonl` is append-only and distills to `LESSONS.md` as an explicit, human-approved step. Hooks
turn policy into mechanism: `PreToolUse` blocks unbounded FASTQ streams and `/scratch`/absolute-path
writes; `PostToolUse` auto-runs `manifest validate` after any manifest edit; `Stop` refuses to end a
turn while `questions.md` is non-empty.
