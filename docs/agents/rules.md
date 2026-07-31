# The rules: why each exists, and what enforces it

[`AGENTS.md`](../../AGENTS.md) states R1–R11 as imperatives, and that is all an agent needs to obey
them. This file is the layer behind: for each rule, **why** it exists — or which ADR records that
decision, since rationale that became an ADR is pointed at here, never restated — and the **file you
can open and run** to watch it enforced.

A rule enforced by a fictional mechanism is worse than an unenforced one, so every enforcement cell
below names something real. `PLEDGE:` marks a real debt. `.github/workflows/ci.yml` runs
`pixi run check` on every push and PR; pre-commit runs only the fast hooks, so in the loop run the
narrowest thing that can go red ([`testing.md`](testing.md)).

**The enforcement map is checked.** `test_the_enforcement_map_names_tests_that_exist`
(`tests/test_docs.py`) asserts that every `test_*` name written below is defined somewhere under
`tests/`, so renaming a test turns this file red instead of silently making it fiction. It exists
because the previous mechanism was a comment in `tests/test_skills.py` asking a human not to rename a
function without updating a table that no test read.

## R1 — Emit data, never code

**Why.** The one thing an LLM must never produce is executable text, because there is no validator
for arbitrary code — only for data. So the model's whole output surface is schema-shaped
([ADR-0008](../adr/0008-llm-surface-carries-only-checkable-fields.md)), and the composer's output is
a wrapper (`module` + `use rule *`) over hand-written, versioned Snakemake modules rather than
generated rule bodies. What ships is `Snakefile` / `config.yaml` / `units.tsv`.

**Enforced by.** `test_the_generated_wrapper_contains_no_rule_source` and
`test_shipped_modules_are_hand_written_not_generated` (`tests/test_workflows.py`); the `LLM_FACING`
pin in `tests/test_models.py`, which fails if a model reachable by the LLM grows an unvalidated field.

## R2 — Agents propose, code decides, and refusal is an exit code

**Why.** The model is a proposer and a verifier, never an authority — see
[ADR-0008](../adr/0008-llm-surface-carries-only-checkable-fields.md) (only fields code can re-check
cross the seam), [ADR-0009](../adr/0009-llm-provider-is-pluggable.md) (span verification is what makes
the provider swappable — the check is ours, not the vendor's) and
[ADR-0013](../adr/0013-cli-is-a-machine-interface.md) (a refusal must be machine-readable, so it is an
exit code plus structured `Blocker`s). Three parts: (a) no field enters the manifest without passing a
validator, and LLM `confidence` is advisory — it never overrides observed bytes; (b) every `Assertion`
carries a `quote` that greps back into the normalized canonical text **and** *entails* its value, else
it is rejected — the hallucination tripwire; (c) when code decides *no*, the LLM only chooses *what
question to ask*.

**Enforced by.** Pydantic validators + `manifest validate`; `verify_drafts` (`harvest/verify.py`), run
inside `harvest extract` and covered by `tests/test_harvest.py` (`harvest verify` is a standalone
re-checker); the exit-code contract and the `PostToolUse` hook (`hooks/guards.py`).

## R3 — Never read a whole FASTQ

**Why.** [ADR-0001](../adr/0001-head-and-wholefile.md): a probe joins a head to a whole file, and
there is no read-source seam. Every FASTQ touch is bounded by `--max-reads` (default 2 000 — the
chemistry call is N-invariant well past it, so a larger value is an explicit opt-in to read more of a
full-size file) **and** `--max-bytes` (256 MB decompressed). Wall-clock is never a budget; a path that
*can* stream a multi-GB FASTQ is a bug. **One loop enforces this**: `probe.streaming.BoundedReader`.
Everything that reads a FASTQ iterates it — `FastqHead` (probe's signals) and `RecordSlice` (the
fingerprint's verbatim records) differ only in what they retain. Writing a second budget loop is the
bug this rule is really guarding.

**Enforced by.** The `PreToolUse` hook (`hooks/guards.py`, size-blind);
`test_the_read_budget_bounds_bytes_read_however_large_the_file`,
`test_the_byte_budget_binds_when_the_reads_are_long`, `test_the_reader_stops_at_the_read_budget` and
`test_the_reader_stops_at_the_byte_budget` (`tests/test_probe.py`);
`test_both_accumulations_consume_the_same_records` (`tests/test_fingerprint.py`), which is the one
that would catch a second loop.

## R4 — Three truths, never merged

**Why.** Observed, asserted and inferred are different kinds of knowing, and merging them is how a
corpus quietly acquires wrong facts. Every *interpretive* field is
`Evidenced{value, basis, evidence, confidence, rung}`; raw identity/provenance carry no basis, and
`resources` is a hint. An `observed`↔`asserted` disagreement is a surfaced `Conflict`.
[ADR-0006](../adr/0006-one-judgement-one-envelope.md) settles the granularity — one judgement, one
envelope, and `confidence: null` is legal. [ADR-0007](../adr/0007-sample-attributes-are-ncbi-keys.md)
settles the keys — NCBI's 960 harmonized BioSample names, of which we **ask** a few and **enforce**
all 960. And a claim's subject is the *document*: `AssertionDraft` has no `subject` field, because
each archive record is rendered as its own document, so a paper's sample claim is `inferred`, not
`asserted`.

**Enforced by.** `Evidenced[T]` and the `LLM_FACING` pin;
`test_one_decision_carries_exactly_one_confidence` (`tests/test_models.py`);
`SampleGroup._keys_are_ncbi_attributes` and `test_every_asked_attribute_is_one_ncbi_defines`
(`tests/test_fields.py`); `test_a_record_becomes_its_own_document_scoped_to_itself`
(`tests/test_records.py`).

## R5 — Disk is state, context is cache

**Why.** A headless run over ~10⁴ datasets cannot hold state in a conversation, so every stage writes
a resumable, content-addressed artifact under **`seqforge/`** (no leading dot — it holds the manifest
and the Snakefile, the *output*, not dotfile scratch). Human-readable names keep a stem plus a 12-char
hash (`pipeline/default-d94c737eb677/`). Resume is *implicit*, `--no-cache` opts out, and there is no
`--resume` ([ADR-0013](../adr/0013-cli-is-a-machine-interface.md)). What is **not** stored is as
deliberate: [ADR-0015](../adr/0015-onlists-are-built-and-deleted.md) has barcode whitelists built by a
rule and `temp()`-deleted. A `.gitignore` entry must be anchored (`/seqforge/`) or it also ignores
`src/seqforge/`. The layout itself is [`state.md`](state.md).

**Enforced by.** The artifact layout and the content-addressed cache (`resolve/cache.py`, atomic
write-rename); one owner for the name, `workspace.py`.

## R6 — The CLI is the API; the skill is a thin client

**Why.** [ADR-0013](../adr/0013-cli-is-a-machine-interface.md). Every skill action maps to a
deterministic `seqforge <verb>` with no LLM in it, emitting JSON on stdout **by default** — no
`--json` flag, because a machine interface does not ask to be machine-readable (`kb list` is the one
plain-text verb). `harvest extract` is the sole LLM touchpoint in a headless run. A skill that
documents a verb the app does not have is a confident instruction to fail.

**Enforced by.** `test_skill_documents_only_real_cli_verbs` (`tests/test_skills.py`), which
introspects the live Typer app — verb, subcommand and long flags — so a rename goes red.

## R7 — Machine-independent manifest: no absolute paths, ever

**Why.** The manifest describes an assay, not a filesystem; it has to mean the same thing on the
machine that composes it and the cluster that runs it, years apart. Genome = UCSC assembly id plus a
registered GTF name; software = a literal `liulab-runtime` env name; data = a URI. Everything resolves
at run time. (The compiled run directory copies its module in for the same reason — see
[`state.md`](state.md).)

**Enforced by.** The `Uri` validator and the `PreToolUse` `/scratch`-and-absolute-path guard
(`hooks/guards.py`).

## R8 — Every KB entry is executable and self-testing

**Why.** A knowledge base of prose claims about chemistries rots silently. Each tech therefore ships a
`spec.yaml` that is *runnable*: `kb roundtrip` (spec → synth → probe → recover) proves the entry
recovers what it declares, and the biconditional (design §2.4) proves that two entries with identical
`backend.params` are processing-equivalent. A new tech that silently collides with an existing one at
rungs 0–2 without declaring it fails.

**Enforced by.** `kb roundtrip` (exit 3); `test_every_kb_spec_roundtrips`,
`test_section_12_biconditional_holds_over_every_loaded_spec_pair` and
`test_no_spec_pair_is_confusable_without_declaring_it` (`tests/test_kb.py`) — all three collect from
`kb.list_spec_ids()`, so adding a spec adds its cases automatically.

## R9 — Cheap first, expensive only on ambiguity

**Why.** Identification is a ladder, not a search: the default path is escalation rungs 0–3, and
escalation past 3 fires on exactly one trigger — a **processing-divergent** tie that metadata cannot
settle. A `Conflict` does *not* escalate; it is surfaced in parallel. And most ties are not worth
asking about at all: [ADR-0012](../adr/0012-produce-every-answer-rather-than-ask.md) says produce
every answer rather than ask, whenever every answer is affordable. Record which rung resolved each
field.

**Enforced by.** Rung provenance on `resolve score`; `resolve/escalate.py`. Rungs 4–6 are unbuilt, so
a surviving tie escalates straight to rung 7 (ask the human).

## R10 — Consumer, not parallel universe

**Why.** Genome files and aligner environments belong to `liulab-genome` and `liulab-runtime`;
re-implementing either here forks the lab's stack into two truths. *Depending* on them is the
opposite of defining them: `liulab-genome` is a declared dependency, while STAR appears in no
dependency table of ours. The contracts are in [`layout.md`](layout.md).

**Enforced by.** `test_seqforge_defines_no_genome_machinery`,
`test_seqforge_defines_no_aligner_environments` and
`test_seqforge_only_calls_liulab_genome_methods_that_exist` (`tests/test_repo_invariants.py`) — AST
and closed-literal checks, the last one against the real `Genome` class.

## R11 — Two artifacts, and the instructable surface is closed

**Why.** Three ADRs, one rule.
[ADR-0004](../adr/0004-two-artifacts-not-one.md): what the data *is* and what to *do* with it have
different lifetimes, so `manifest.yaml` (library + experiment) is write-once and `processing.yaml` is
plural — a change of intent must **never** perturb `dataset_hash`.
[ADR-0005](../adr/0005-run-id-is-the-pairing.md): the compiled run is identified by the pairing,
`run_id = H(dataset ⊕ processing ⊕ kb ⊕ workflow)`.
[ADR-0011](../adr/0011-closed-instructable-surface.md): the instructable surface is closed and split
parse-vs-count — `backend.params` says how to **parse** reads (soloType, CB/UMI offsets, whitelist,
strand) and is byte-decided, never instructable; the recipe says what to **count**, and against which
genome, aligner, env and resources. The two key sets are **disjoint**, which makes "a user instruction
contradicts the bytes" *inexpressible* rather than merely refused.
[ADR-0012](../adr/0012-produce-every-answer-rather-than-ask.md): produce every answer rather than ask
— `soloFeatures` defaults to all five (one alignment, five counting rules, one pass); escalate only
where the answers are genuinely exclusive, like a genome or an aligner.

**Enforced by.** `dataset_content_hash` covering exactly two sections, plus the recipe-sweep
hash-invariance test and the `models/{dataset,processing}.py` import-graph test (`tests/test_models.py`,
`tests/test_manifest.py`); `compose` refusing a mismatched pin; the `Backend` key-allowlist (`kb lint`
and every `load_spec`); `params_gate`'s disjointness / coverage / three-owner faithfulness checks and
`extra="forbid"` on the processing models; `kb e2e-introns` with the override deleted
(`composed_soloFeatures ⊇ {Gene, GeneFull}`) and the eval's "questions asked" metric.

## The demo dataset, and the two disciplines that outlived the held-out case

`PRJNA1027859` is the demo dataset and **there are no held-out cases** —
[ADR-0016](../adr/0016-no-held-out-dataset.md) retired the reservation and deleted its guard and
registry. It is the pilot's worked example: read it, run it, write the tutorial from it. Two
disciplines survive the retirement:

- **Real data, and its path, stays out of git.** A `kind: local` eval case names an environment
  variable rather than a path — enforced by `test_skill_never_leaks_a_lab_path`
  (`tests/test_skills.py`).
- **Pre-register `expected.yaml` before a run.** Only a prediction can be wrong, and its claims must
  be *checkable*: `experiment.samples.*.<attr>` for every sample, `experiment.samples.<accession>.<attr>`
  for one.

## Two resolvers, two refusals

`resolve` holds two resolvers, and they are siblings rather than a stage and a side-input. They part
on disagreement: the byte resolver surfaces an `observed`↔`asserted` `Conflict` it will not arbitrate
— that decides what the data *is*, and it **blocks** — while the metadata resolver *decides* a
sample-attribute disagreement (stronger authority wins; equal authorities leave the field **null**)
and emits a non-blocking **`Warning`**. Null-over-wrong is a value, not a question. The line is in
[`resolve/records.py`](../../src/seqforge/resolve/records.py), and the decision is
[ADR-0010](../adr/0010-two-resolvers-one-blocks-one-warns.md).
