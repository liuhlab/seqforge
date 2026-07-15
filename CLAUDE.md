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

**Status: Milestone 0 landed; the held-out case has not been run.** The deterministic spine — models,
probe, kb, resolve, manifest, compose, hooks, evals — is implemented and green (`pixi run check`), and
runs end to end on synthetic sacCer3 and ce11 fixtures. The authoritative design is
[`docs/design.md`](docs/design.md) (Pydantic model hierarchy, KB `spec.yaml` schema, scoring function
+ joint role-assignment, CLI surface); the full rationale is
[`PROJECT_BRIEF.md`](PROJECT_BRIEF.md), whose §14 tracks what is still unbuilt. Read all three before
writing code. The pipeline is a five-stage compiler over two artifacts:

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

## Non-negotiable rules

These encode `PROJECT_BRIEF.md` §3 principles 1–14 (the numbering is deliberately offset: brief #1 →
R2, brief #2 → R1, and R12 derives from §13, not §3). Each names how it is enforced; a change that
violates one must change the rule here first.

**Read the enforcement column literally.** This table was once headed "checked, not aspirational"
while five rows cited a CI that did not exist and three cited checkers that were never built
(`kb confusability`, a "determinism ledger", `--resume`). A rule enforced by a fictional mechanism is
worse than an unenforced one: it buys the feeling of a guarantee at no cost. So every cell below now
names a **file you can open and run**, and where something is still unenforced it says `PLEDGE:` and
means it. A `PLEDGE:` is a debt — the fix is to build the checker and delete the marker, never to
quietly widen the claim.

CI was never the mechanism these rules needed; a *test* is. `.github/workflows/ci.yml` runs
`pixi run check` on every push and PR, and that is what makes the table below true. Pre-commit runs
only the fast hooks (ruff, mypy, shellcheck) — the suite is deliberately **not** in it, so a red
commit can exist locally and is caught at push. Run `pixi run check` yourself when you change
behaviour.

| # | Rule | Enforced by |
|---|---|---|
| R1 | **Emit data, never code.** No LLM generates Snakefile/rule source. LLM output must validate against an exported JSON Schema. The composer emits `config.yaml` / `units.tsv` / a module selection — never rule source. | `test_the_generated_wrapper_contains_no_rule_source` + `test_shipped_modules_are_hand_written_not_generated` (`tests/test_compile.py`); `LLM_FACING` exact-equality pin (`tests/test_models.py`); composer unit tests |
| R2 | **Agents propose, code decides.** No field enters the manifest without passing a validator. LLM `confidence` is advisory and never overrides observed bytes. | Pydantic validators; `manifest validate` |
| R3 | **Never read a whole FASTQ.** Every FASTQ touch is bounded by `--max-reads` (default 200 000) **and** `--max-bytes` (256 MB decompressed). Wall-clock is never a budget. A code path that *can* stream a whole multi-GB FASTQ is a bug. | `PreToolUse` hook (`hooks/guards.py`, size-blind by design) + `test_the_read_budget_bounds_bytes_read_however_large_the_file` / `test_the_byte_budget_binds_when_the_reads_are_long` (`tests/test_probe.py`: a 128 MB-decompressed fixture, 434 KB on disk, of which the probe reads ~10 %) |
| R4 | **Refusal is an exit code.** `manifest validate` returns structured `Blocker`s + a nonzero exit. Code decides whether we may compile; the LLM only decides *what question to ask*. | exit-code contract + `PostToolUse` hook |
| R5 | **Span-verified extraction.** Every `Assertion` carries a `quote` that (a) greps back into the normalized canonical text and (b) *entails* its value; else reject. This is the hallucination tripwire. | `verify_drafts` (`harvest/verify.py`), deterministic, run inside `harvest extract`; `tests/test_harvest.py`. There is no separate `harvest verify` verb |
| R6 | **Three truths, never merged.** Every *interpretive* manifest field is `Evidenced{value, basis, evidence, confidence, rung}` — raw identity (`uri`, `sha256`, `size`) and provenance are not interpretations and carry no basis; `resources` is an advisory hint, not a decision. `observed`↔`asserted` disagreement is a surfaced first-class `Conflict`, never auto-picked. The three truths are the three *bases* — nothing here ever depended on there being three *sections*, and conflating the two is what let `processing` masquerade as a truth for a year. | `Evidenced[T]` type; resolver |
| R7 | **Disk is state, context is cache.** Every stage writes a resumable, content-addressed artifact under `.seqforge/`. Any run is resumable after a kill; the agent never holds state only in context. | artifact layout + content-addressed cache hits (`resolve/cache.py`, atomic write-then-rename). Resume is *implicit* — re-running finds the artifact; `--no-cache` opts out. There is no `--resume` flag |
| R8 | **The CLI is the API; the skill is a thin client.** Every skill action maps to a deterministic `seqforge <verb>` that runs with no LLM in the loop and emits JSON on stdout **by default** (there is no `--json` flag; `kb list` is the one plain-text verb). `harvest extract` is the sole LLM touchpoint in a headless run; `eval run --llm` is opt-in. (`resolve adjudicate` is planned, not built.) | `test_skill_documents_only_real_cli_verbs` (`tests/test_skills.py`) — introspects the live Typer app, so a renamed verb goes red. No "determinism ledger" exists or ever did; the phrase named nothing |
| R9 | **Machine-independent manifest — no absolute paths, ever.** Genome = UCSC assembly id + registered GTF name; software = a literal `liulab-runtime` env name; data = a URI. Everything resolves at run time. | `Uri` validator + `PreToolUse` `/scratch` guard |
| R10 | **Every KB entry is executable and self-testing.** Each technology ships a `spec.yaml`; `kb roundtrip` (spec→synth→probe→recover) proves it recovers what it declares; the §12 biconditional proves two entries with identical `backend.params` are declared processing-equivalent; and a new tech that silently collides with an existing one at rungs 0–2 without declaring it fails. | `kb roundtrip` (`cli.py`, exit 3) + `test_every_kb_spec_roundtrips`, `test_section_12_biconditional_holds_over_every_loaded_spec_pair`, `test_no_spec_pair_is_confusable_without_declaring_it` (`tests/test_kb.py`) — **all three collect from `kb.list_spec_ids()`**, so a new spec is covered because it exists, not because someone remembered. There is no `kb confusability` verb; the pairwise checks live in the suite |
| R11 | **Cheap first, expensive only on ambiguity.** Default path is escalation-ladder rungs 0–3. Escalation past 3 fires on exactly one trigger: a **processing-divergent tie** that metadata cannot settle (a `Conflict` does *not* escalate — it is surfaced in parallel). Record which rung resolved each field. | `resolve score` rung provenance; `resolve/escalate.py`. **Note:** rungs 4–6 (full-panel, k-mer sketch, mini-alignment) are unbuilt, so a surviving tie escalates straight to rung 7 (ask the human) |
| R12 | **Consumer, not parallel universe.** Never define genome-file machinery or aligner environments here — they belong upstream in `liulab-genome` / `liulab-runtime`. If a feature belongs in one of those, it goes there. | `test_seqforge_defines_no_genome_machinery` (AST: no `class Genome` / `def build_star_index` / `def register_gtf` anywhere in `src/`) + `test_seqforge_defines_no_aligner_environments` (`RuntimeEnv` is a closed literal; no conda YAML / Dockerfile in the tree) — `tests/test_compile.py` |
| R13 | **The dataset is immutable; the recipe is plural.** `manifest.yaml` (library + experiment) is what the data *is* — content-addressed, write-once. `processing.yaml` is what to *do* with it, and a dataset carries as many as we care to run. `run_id = H(dataset ⊕ processing ⊕ kb ⊕ workflow)`; a change of intent must **never** perturb `dataset_hash`. If re-running a dataset with a different aligner edits `manifest.yaml`, that is a bug. | `dataset_content_hash` covers 2 sections; recipe-sweep hash-invariance test; `models/{dataset,processing}.py` import-graph test; `compose` refuses a mismatched pin |
| R14 | **The instructable surface is closed, and the line is parse vs count.** `backend.params` says how to **parse** reads (soloType, CB/UMI offsets, whitelist, strand) — byte-decided, never instructable. The processing manifest says what to **count**, and against which genome, aligner, env, resources. The two key sets are **disjoint**, which is what makes "a user instruction contradicts the observed bytes" *inexpressible* rather than merely deprioritized: the user has no vocabulary in which to say it. | `Backend` key-allowlist validator (`kb lint` + every `load_spec`); `params_gate` disjointness + coverage + **three-owner** faithfulness (kb / derived / processing); `extra="forbid"` on `ProcessingSection` + `ProcessingManifest`, so an unknown key is a validation error rather than a silent drop |
| R15 | **Produce every answer rather than ask.** §12 says never escalate an ambiguity that cannot change the output; this is its sibling — never escalate one whose every answer you can afford to emit. `soloFeatures` defaults to all five: one alignment, five counting rules, one pass. Escalate only where the answers are genuinely exclusive (a genome, an aligner). An ambiguity a second counting rule would settle is not a `Question`. | `kb e2e-introns` with the override deleted (`composed_soloFeatures ⊇ {Gene, GeneFull}` on the compiler's own params); eval "questions asked" |

**Held-out acceptance cases — do not touch.** `PRJNA1027859` is the pilot's single real acceptance
test (more will follow). Do not read, sample, list, stat, profile, or tune against a held-out case.
Build against synthetic KB round-trip fixtures only. Each runs **once**, pre-registered, when the
maintainer says so. **Their on-disk locations are deliberately not in this repo** — they live in
local, out-of-git config (`~/.claude/`), so this public repo carries the *rule*, not the paths. A
`PreToolUse` guard blocks access to the held-out roots (e.g. `/scratch/**`).

## Toolchain

Everything runs through **pixi** (do not use `pip`/`conda`/`venv` directly). Conventions follow the
sibling repos; `seqforge` is the family's first real Python library, so it introduced mypy and a test
task with no in-family precedent.

```bash
pixi install                 # build environments
pixi run test                # pytest (unit + KB round-trip + composer gates)
pixi run test -- -k <expr>   # a single test / subset
pixi run lint                # ruff check .
pixi run fmt                 # ruff format .
pixi run typecheck           # mypy --strict on 8 modules (see below)
pixi run check               # lint + fmt-check + typecheck + test — what CI runs
pixi run -e docs docs-build  # mkdocs build --strict
pixi run -- pre-commit install   # once per clone (fast hooks only: ruff, mypy, shellcheck)
```

**`pixi run check` is the mechanism, not a formality** — most rules above are enforced by tests, so a
green suite *is* the guarantee. `.github/workflows/ci.yml` runs it on every push and PR. Pre-commit
runs only the fast hooks; the suite is not among them, so nothing runs `check` for you before a
commit. Run it yourself when you change behaviour.

- **Lint/format** (from `liulab-runtime`): ruff `line-length=100`, `target-version=py312`,
  `select=[E,W,F,I,UP,B]`, `ignore=[E501, UP046, UP047]`. `UP046`/`UP047` are off deliberately:
  classic `Generic[T]` / `TypeVar` has better-tested pydantic-v2 + mypy support than PEP-695 syntax.
- **Typing:** `[tool.mypy] strict = true`, scoped by the `typecheck` task to **`models/`, `probe/`,
  `resolve/`, `manifest/`, `compose/`, `workflows/`, `harvest/`, `evals/`** — everything except
  `cli/`, `io/`, `kb/`, `hooks/`. (The rule of thumb is unchanged: a wrong type in these modules
  silently poisons the corpus.)
- **Tests:** pytest, 15 modules, `strict-markers` / `strict-config` / `xfail_strict` /
  `filterwarnings=error`. *Planned, not built:* `syrupy` snapshots for golden manifests and
  `hypothesis` for the synthetic generator — both are pinned as test deps but neither is imported
  anywhere yet.
- **Packaging** (from `liulab-genome`): hatchling, `src/` layout, `requires-python>=3.12`,
  channels `conda-forge` + `bioconda`. Versioning is **CalVer `YYYY.M.PATCH`** — static in
  `[project].version` + `CHANGELOG.md` (patch resets monthly). **Component/tool-stamp versions use
  CalVer too** (`PROBE_VERSION`, `kb_version`, `resolve_version`, `workflow_version`), since they are
  folded into content-addressed cache keys — never SemVer.
- **Docs:** mkdocs-material → **gh-pages branch**, following `liulab-runtime` (the siblings differ —
  `liulab-genome` uses a Pages artifact). Published from `main` by `.github/workflows/docs.yml`.
  The site is the **human** layer: approachable pages with mermaid diagrams (bundled with
  mkdocs-material — no extra pin). `docs/design.md` is deliberately **excluded** from it
  (`exclude_docs`): it is the agent-facing technical source of truth and carries a pushback appendix
  plus values marked *unverified*, which must not read as settled guidance under a docs URL.

## Repository layout

Single repo, single `pyproject.toml`, clear internal module boundaries; do **not** split into
separate distributions. Everything below is under `src/seqforge/` except the last three, which sit at
the repo root.

```
models/     pydantic v2 schemas; `schema export` is the single source of truth
probe/      deterministic FASTQ fingerprinting (no LLM, no network)
kb/         knowledge base: one directory per technology (spec.yaml + README.md), under kb/specs/
resolve/    candidate scoring, role assignment, confusability, escalation
manifest/   fill/validate/hash both artifacts; `policy.py` is where precedence lives (R13/R15)
compose/    (dataset, processing) -> snakemake config + module selection
io/         remote peeking, ENA/SRA/GEO/SDL resolution, pooch-cached onlists
workflows/  hand-written, versioned Snakemake modules (NOT generated). map/ only — no fetch/ yet
hooks/      PreToolUse/PostToolUse/Stop guards, behind `seqforge hook …` — policy as mechanism
cli.py      a single typer module (NOT a package): root app + 9 sub-typers. JSON by default
e2e.py      the ground-truth end-to-end runs behind `kb e2e` (sacCer3) / `kb e2e-introns` (ce11)
evals/      ground-truth corpus + harness
─── repo root ───
skills/     SKILL.md agent skills (no installer yet)
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

Hooks turn policy into mechanism, and these are real: `PreToolUse` blocks unbounded FASTQ streams
(size-blind — a path that *can* stream a multi-GB file is denied regardless of the file's actual
size) and `/scratch`/absolute-path writes; `PostToolUse` auto-runs `manifest validate` after any
manifest edit; `Stop` refuses to end a turn while `questions.md` is non-empty.

Two things the layout above does **not** yet contain, despite the design calling for them:
`questions.md` is only ever *read* (by the `Stop` hook) — no code writes it, so the hook guards a
file the pipeline never produces. And the journal flywheel — `journal.jsonl` append-only, distilled
to `LESSONS.md` by an explicit human-approved step — is **entirely unbuilt**: no writer, no verb, no
file, and `grep -r journal src/` is empty. The `seqforge-journal` skill wraps four commands that do
not exist.
