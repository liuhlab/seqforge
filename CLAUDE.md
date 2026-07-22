# CLAUDE.md

Guidance for Claude Code in this repo. These instructions override defaults; follow them exactly.

## What this is

`seqforge` is a **compiler, not a chatbot**. It turns `(arbitrary FASTQ files) + (unstructured
human/DB metadata)` into a validated, machine-independent **`manifest.yaml`** (what the data IS —
content-addressed, immutable), then compiles that under a **`processing.yaml`** (the recipe: our flags)
into a runnable Snakemake config. Headless, across ~10⁴ public datasets, into a genomic-AI corpus.

The two artifacts have different lifetimes: a finished assay is immutable, so the manifest is hashed
and never rewritten; what you *do* with it is a choice, so it lives in the recipe. Same manifest +
different recipe = a different pipeline (GeneFull vs Gene, a different genome), manifest hash unchanged.

Deterministic code owns every decision. The LLM has exactly **two** jobs: (a) parse prose into
span-verified `Assertion`s; (b) arbitrate ambiguity code has *already flagged* (job (b) is modelled,
its verb unbuilt). Instruction-following is **not** a third job: "align in GeneFull mode" enters as an
`Assertion` on a `processing.*` field with a quote that greps back, and code applies precedence in
`plan` — we accept instructions only because we never trust the model to *act* on them, only to *find*
them. Sample metadata is no third job either: each archive record is rendered as its **own document**,
so "which sample" is answered by which document code handed the model — `AssertionDraft` gets no
`subject` field (that would be an authority with no quote to check). Everything else is a verifier;
don't blur that line. In `resolve`, the model's reading may say *what to check first* and *how to break
a byte-tie*, never *what the answer is*.

**Status: implemented and green** (`pixi run check`). `compose` emits a Snakefile that runs and
`kb e2e` proves its matrix against ground truth (arc 2026-07-15: recovery 0.9545, 0 spurious/inflated,
a strand inversion collapses 2000 counts to 49). The demo dataset PRJNA1027859 was run (2026-07-16) and
compiles end to end. The authoritative design **and rationale** is [`docs/design.md`](docs/design.md)
(models, KB schema, scoring, CLI surface, and §9 — what is still unbuilt); it absorbed the old
`PROJECT_BRIEF.md`. Read it and this file before writing code.

The pipeline is a compiler over two artifacts, and **`resolve` has two resolvers**:

```
probe(files)                    -> Observation   deterministic, no LLM, no network, bytes only; the
                                                 remote twin (io probe-remote) fingerprints a URL via
                                                 one bounded HTTP Range read — no download, provider md5
records(accession?)             -> ArchiveRecord project/sample/experiment/run. OPTIONAL: most
                                                 sequencing data never had an accession.
harvest(documents)              -> Assertion     LLM, each claim span-verified. A document is a
                                                 file you handed us OR one archive record.
──────────────────────────────────────────────────────────────────────────────────────────────
score(Observation, KB, hypo?)   -> candidates x role_assignment, Conflicts, Questions
      "what IS this library?"      from BYTES. The hypothesis selects and breaks ties; it never
                                   enters the evidence matrix.
resolve_metadata(FileIdentity,  -> samples x attributes, Conflicts, Warnings
                 records?,         "which sample is each file, and what was it?" from RECORDS and
                 assertions?)      PROSE. Handed FileIdentity, not Observation — no probe signal.
──────────────────────────────────────────────────────────────────────────────────────────────
  => manifest.yaml     THE IR.    What the data IS.   One per dataset. Immutable, hashed.

plan(Assertions, flags, policy) -> ProcessingSection   precedence: flag > instruction > policy
  => processing.yaml   THE FLAGS. What to DO with it. Many per dataset. Sparse; empty is legal.

compose(manifest, processing)   -> Snakefile + config + units.tsv   deterministic
  => seqforge/pipeline/<recipe>-<run_id[:12]>/Snakefile   THE DELIVERABLE. The user submits this.
  run_id = H(dataset ⊕ processing ⊕ kb ⊕ workflow). Running it ends in <sample>.h5ad (+
  <sample>.velocyto.h5ad). `rule all` demands the matrices, not a folder.
```

**`seqforge run` (alias `compile`) chains the whole diagram in one headless pass** — FASTQ + metadata
in, `manifest.yaml` + Snakefile out — stopping at the first refusal and writing a token/mode cost
ledger to `seqforge/logs/usage.json`. It adds no authority (same deterministic verbs), is the entry point a
`claude -p` turn drives via the `orchestrate` skill, and has **no `--resume` flag** — re-running
resumes through each stage's content-addressed cache.

**The two resolvers are siblings, not a stage and a side-input.** They part on disagreement: the byte
resolver surfaces an `observed`↔`asserted` `Conflict` it will not arbitrate (that decides what the data
*is*, and **blocks**); the metadata resolver *decides* a sample-attribute disagreement — stronger
authority wins, equal authorities leave it **null** — and emits a non-blocking **`Warning`**.
Null-over-wrong is a value, not a question. The line is in [`resolve/records.py`](src/seqforge/resolve/records.py).

The last artifact is the point: **a Snakefile the user submits.** `seqforge` does not submit jobs (no
Slurm, no sbatch); its output is artifacts, and the last one runs.

## Non-negotiable rules

Each rule names the file that enforces it — a rule enforced by a fictional mechanism is worse than an
unenforced one, so every cell names a file you can open and run; `PLEDGE:` marks a real debt.
`.github/workflows/ci.yml` runs `pixi run check` on every push/PR; pre-commit runs only fast hooks, so
run `pixi run check` yourself when you change behaviour.

| # | Rule | Enforced by |
|---|---|---|
| R1 | **Emit data, never code.** No LLM generates Snakefile/rule source; LLM output must validate against an exported JSON Schema. The composer emits a `Snakefile` (`module` + `use rule *`, no rule bodies) / `config.yaml` / `units.tsv`. | `test_the_generated_wrapper_contains_no_rule_source`, `test_shipped_modules_are_hand_written_not_generated` (`test_compile.py`); `LLM_FACING` pin (`test_models.py`) |
| R2 | **Agents propose, code decides — and refusal is an exit code.** The model is a proposer/verifier, never an authority. (a) No field enters the manifest without passing a validator; LLM `confidence` is advisory, never overrides observed bytes. (b) Every `Assertion` carries a `quote` that greps back into the normalized canonical text **and** *entails* its value, else reject — the hallucination tripwire. (c) When code decides *no*, `manifest validate` returns structured `Blocker`s + a nonzero exit; the LLM only decides *what question to ask*. | Pydantic validators + `manifest validate`; `verify_drafts` (`harvest/verify.py`) run inside `harvest extract` + `test_harvest.py` (`harvest verify` is a standalone re-checker); exit-code contract + `PostToolUse` hook |
| R3 | **Never read a whole FASTQ.** Every FASTQ touch is bounded by `--max-reads` (default 200 000) **and** `--max-bytes` (256 MB decompressed). Wall-clock is never a budget; a path that *can* stream a multi-GB FASTQ is a bug. | `PreToolUse` hook (`hooks/guards.py`, size-blind) + `test_the_read_budget_bounds_bytes_read_however_large_the_file`, `test_the_byte_budget_binds_when_the_reads_are_long` (`test_probe.py`) |
| R4 | **Three truths, never merged.** Every *interpretive* field is `Evidenced{value, basis, evidence, confidence, rung}`; raw identity/provenance carry no basis, `resources` is a hint. `observed`↔`asserted` disagreement is a surfaced `Conflict`. One judgement = one envelope; `confidence: null` is legal. Sample-fact keys are NCBI's 960 harmonized BioSample names — we **ask** a few, **enforce** all 960 (`condition` was ours, so it accepted worm husbandry). And a claim's subject is the *document* — `AssertionDraft` has no `subject`; each record is rendered as its own document, and a paper's sample claim is `inferred`, not `asserted`. | `Evidenced[T]`, `LLM_FACING` pin, `test_one_decision_carries_exactly_one_confidence` (`test_models.py`); `SampleGroup._keys_are_ncbi_attributes`, `test_every_asked_attribute_is_one_ncbi_defines`, `test_a_record_becomes_its_own_document_scoped_to_itself` (`test_records.py`) |
| R5 | **Disk is state, context is cache.** Every stage writes a resumable, content-addressed artifact under **`seqforge/`** (no leading dot — it holds the manifest and Snakefile, the *output*). Human-readable names keep a stem + 12-char hash (`pipeline/default-d94c737eb677/`). Resume is *implicit*; `--no-cache` opts out; there is no `--resume`. A `.gitignore` entry must be anchored (`/seqforge/`) or it also ignores `src/seqforge/`. | artifact layout + content-addressed cache (`resolve/cache.py`, atomic write-rename); one owner for the name: `workspace.py` |
| R6 | **The CLI is the API; the skill is a thin client.** Every skill action maps to a deterministic `seqforge <verb>` (no LLM) emitting JSON on stdout **by default** (no `--json` flag; `kb list` is the one plain-text verb). `harvest extract` is the sole LLM touchpoint in a headless run. | `test_skill_documents_only_real_cli_verbs` (`test_skills.py`) — introspects the live Typer app, so a renamed verb goes red |
| R7 | **Machine-independent manifest — no absolute paths, ever.** Genome = UCSC assembly id + registered GTF name; software = a literal `liulab-runtime` env name; data = a URI. Everything resolves at run time. | `Uri` validator + `PreToolUse` `/scratch` guard |
| R8 | **Every KB entry is executable and self-testing.** Each tech ships a `spec.yaml`; `kb roundtrip` (spec→synth→probe→recover) proves it recovers what it declares; the biconditional (design §2.4) proves two entries with identical `backend.params` are processing-equivalent; a new tech that silently collides at rungs 0–2 without declaring it fails. | `kb roundtrip` (exit 3) + `test_every_kb_spec_roundtrips`, `test_section_12_biconditional_holds_over_every_loaded_spec_pair`, `test_no_spec_pair_is_confusable_without_declaring_it` (`test_kb.py`) — all three collect from `kb.list_spec_ids()` |
| R9 | **Cheap first, expensive only on ambiguity.** Default path is escalation rungs 0–3. Escalation past 3 fires on one trigger: a **processing-divergent tie** metadata cannot settle (a `Conflict` does *not* escalate — surfaced in parallel). Record which rung resolved each field. | `resolve score` rung provenance; `resolve/escalate.py`. Rungs 4–6 are unbuilt, so a surviving tie escalates straight to rung 7 (ask the human) |
| R10 | **Consumer, not parallel universe.** Never define genome-file machinery or aligner environments here — they belong to `liulab-genome` / `liulab-runtime`. *Depending* on them is the opposite: `liulab-genome` is a declared dependency; STAR is in no dependency table of ours. | `test_seqforge_defines_no_genome_machinery`, `test_seqforge_defines_no_aligner_environments`, `test_seqforge_only_calls_liulab_genome_methods_that_exist` (`test_compile.py`, all AST/closed-literal checks against the real class) |
| R11 | **Two artifacts: the immutable dataset, the plural closed-surface recipe** — what the data *is* vs. what to *do* with it. (a) `manifest.yaml` (library + experiment) is write-once; `processing.yaml` is what to *do*, plural; `run_id = H(dataset ⊕ processing ⊕ kb ⊕ workflow)`, and a change of intent must **never** perturb `dataset_hash`. (b) The instructable surface is **closed**, split parse vs count: `backend.params` says how to **parse** reads (soloType, CB/UMI offsets, whitelist, strand) — byte-decided, never instructable; the recipe says what to **count**, and against which genome/aligner/env/resources. The two key sets are **disjoint**, so "a user instruction contradicts the bytes" is *inexpressible*. (c) Produce every answer rather than ask: never escalate an ambiguity whose every answer you can afford to emit — `soloFeatures` defaults to all five (one alignment, five counting rules, one pass); escalate only where answers are genuinely exclusive (a genome, an aligner). | `dataset_content_hash` covers 2 sections + recipe-sweep hash-invariance test + `models/{dataset,processing}.py` import-graph test + `compose` refuses a mismatched pin; `Backend` key-allowlist (`kb lint` + every `load_spec`) + `params_gate` disjointness/coverage/three-owner faithfulness + `extra="forbid"` on the processing models; `kb e2e-introns` with the override deleted (`composed_soloFeatures ⊇ {Gene, GeneFull}`) + eval "questions asked" |

**`PRJNA1027859` is the demo dataset; there are no held-out cases** (the reservation was retired
2026-07-15, its guard/registry deleted). It is the pilot's worked example — read it, run it, write the
tutorial from it. Two disciplines survive: **real data (and its path) stays out of git** — a
`kind: local` eval case names an env var, guarded by `test_skill_never_leaks_a_lab_path`; and
**pre-register `expected.yaml` before a run** — only a prediction can be wrong, and its claims must be
*checkable* (`experiment.samples.*.<attr>` for all, `experiment.samples.<accession>.<attr>` for one).

## Toolchain

Everything runs through **pixi** (not `pip`/`conda`/`venv`).

```bash
pixi install                 # build environments
pixi run test                # pytest (unit + KB round-trip + composer gates)
pixi run test -- -k <expr>   # a single test / subset
pixi run check               # lint + fmt-check + typecheck + test — what CI runs
pixi run -e docs docs-build  # mkdocs build --strict
```

**`pixi run check` is the mechanism** — most rules are enforced by tests, so a green suite *is* the
guarantee; CI runs it on every push/PR, pre-commit runs only fast hooks. Run it yourself when you
change behaviour.

- **Lint/format:** ruff `line-length=100`, `target-version=py312`, `select=[E,W,F,I,UP,B]`,
  `ignore=[E501, UP046, UP047]` (PEP-695 off: classic `Generic[T]`/`TypeVar` has better pydantic-v2 +
  mypy support).
- **Typing:** `mypy --strict` on `models/`, `probe/`, `resolve/`, `manifest/`, `compose/`,
  `workflows/`, `harvest/`, `evals/` (everything but `cli/`, `io/`, `kb/`, `hooks/` — a wrong type
  there poisons the corpus).
- **Versioning: CalVer `YYYY.M.PATCH`**, including component stamps (`PROBE_VERSION`, `kb_version`,
  `resolve_version`, `workflow_version`) since they fold into content-addressed cache keys — never SemVer.
- **Docs:** mkdocs-material → gh-pages, published from `main` by `docs.yml`. The site is the **human**
  layer; `docs/design.md` is **excluded** (`exclude_docs`) — it is the agent-facing source of truth and
  must not read as settled guidance under a docs URL.
- *Planned, not built:* `syrupy` snapshots and `hypothesis` — pinned, not imported.

## Repository layout

Single repo, single `pyproject.toml`; do **not** split into distributions. Under `src/seqforge/` except
the last three.

```
models/     pydantic v2 schemas; `schema export` is the single source of truth
probe/      deterministic FASTQ fingerprinting (no LLM, no network)
kb/         knowledge base, one dir per technology (spec.yaml + README.md) under kb/specs/;
            hierarchical — an abstract family node (10x-3p-gex, no backend) DESCENDS to leaf chemistries
resolve/    TWO resolvers. scoring/assign/escalate decide the library from BYTES; records.py decides
            which sample each file is from RECORDS + PROSE (handed FileIdentity, never Observation).
            group.py splits a dataset into RUNS by filename (bytes assign roles); with no record that
            grouping IS the sample identity — the normal case. Per-run and per-spec scoring runs in
            parallel across a pool
manifest/   fill/validate/hash both artifacts; policy.py owns precedence (R11)
compose/    (dataset, processing) -> Snakefile + config + units.tsv  (the Snakefile is THE product)
io/         remote peek + probe-remote (fingerprint a URL, no download), ENA/SRA/GEO/SDL resolution,
            pooch-cached onlists. archive.py TRANSCRIBES the four record levels (decides nothing).
            attributes.py = NCBI's 960 BioSample names; efo.py = EFO labels. Both ship as GENERATED
            data with a refresh verb
workspace.py the one place `seqforge/` is spelled, and the one place a readable-name-plus-hash lives
workflows/  hand-written, versioned Snakemake modules (NOT generated). map/ only — no fetch/ yet.
            h5ad.py packages Solo.out as the deliverable (its input contract IS STARsolo's layout)
hooks/      PreToolUse/PostToolUse/Stop guards behind `seqforge hook …` — policy as mechanism
cli.py      a single typer module (root app + sub-typers). JSON by default
e2e.py      ground-truth runs behind `kb e2e` (sacCer3) / `kb e2e-introns` (ce11), which RUN THE
            COMPOSED SNAKEFILE. `kb e2e-cost` (hg38) invokes STAR directly — a memory instrument must
            reap STAR itself
evals/      ground-truth corpus + harness
─── repo root ───
skills/     SKILL.md agent skills; `skills/install.py` symlinks them into a product's discovery path
tests/
```

## Consumer of the liulab stack

- **`liulab-genome`** — import `from genome import Genome`. Assemblies by UCSC id (`sacCer3`, `ce11`,
  `hg38`); annotation = a **registered GTF `name`** (liulab-genome does not fetch annotations, so
  seqforge stages the GTF and calls `register_gtf(gtf, name)`); STAR index via
  `Genome(assembly).build_star_index(gtf=name)`. Never write a genome path into a manifest.
- **`liulab-runtime`** — reference an aligner env by its **literal** name (`align-rna`, `align-dna`,
  `ml`, `ml-gpu`). No profile-indirection layer — the env name *is* the identifier. Do not define
  aligner environments here.

## On-disk state (`seqforge/`, resumable + content-addressed)

One owner for the name: [`workspace.py`](src/seqforge/workspace.py). The top level of `seqforge/`
carries only what a human reaches for — the manifest, the project views, `pipeline/` — and everything
else sorts into one of three subtrees, spelled once in `workspace.py`:
**`cache/`** (content-addressed, resumable, safe to delete): per-file `Observation` by its
content-address — a bounded local key, or a provider md5 for hosted bytes, never a whole-file sha256 —
under `cache/observations/`; dataset `candidates` by `sha256(sorted(file_shas) ⊕ kb_version)`
(probe/resolve versions folded in) under `cache/candidates/`; `cache/taxonomy.json`.
**`records/`**: `records/<accession>.json` (what the archive declared) and `records/documents/<stem>-<hash12>.txt`
(canonical text a span greps into, including docs rendered from records — they live with the records
they came from). **`logs/`** (run/debug, never the deliverable): `logs/usage.json` (the harvest cost
ledger) and `logs/assertions.json`. `manifest.yaml` is written only after a clean `manifest validate`,
and exactly one of `manifest.yaml`/`manifest.draft.yaml` exists (`fill` unlinks the other). Compiled
output lives under `pipeline/<recipe>-<run_id[:12]>/` (config.yaml, units.tsv, Snakefile, a **copy of
the hand-written module** the wrapper imports locally, and processing.lock.yaml) — keyed by the
**run**, since one dataset compiled two ways is two runs. The module is copied in, not referenced by
package path, so a run directory reads and reproduces after it is moved off the composing machine. **Onlists are not stored**: `rule onlist` materializes a whitelist, STAR
reads it, `temp()` deletes it (expanding 6 794 880 barcodes per run dir cost 111 MB of duplicate bytes).

Hooks turn policy into mechanism: `PreToolUse` blocks unbounded FASTQ streams (size-blind) and
`/scratch`/absolute-path writes; `PostToolUse` auto-runs `manifest validate` after a manifest edit;
`Stop` refuses to end a turn while `questions.md` is non-empty. Two design-called-for things are
**unbuilt**: nothing *writes* `questions.md` (the `Stop` hook only reads it), and the journal flywheel
entirely (`journal.jsonl`/`distill`/`LESSONS.md`; its design survives in design.md §9).
