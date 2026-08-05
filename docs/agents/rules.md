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

**And so is the shape.** Every section below owes an `**Enforced by.**` block, and a section that
links an ADR owes a gloss rather than a précis of it —
`test_every_section_of_the_enforcement_map_names_what_enforces_it` and
`test_a_section_that_links_an_adr_glosses_it_rather_than_restating_it` (`tests/test_docs.py`). This
file stated that policy in its own opening paragraph and then broke it four times, and the compiled
run's id formula ended up written in two notations at once — so it is a check now, not a sentence.

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
it is rejected — the hallucination tripwire, and on `library.chemistry` the value must additionally
name a KB node, because entailment is vacuous when the value sits inside its own quote
([ADR-0020](../adr/0020-a-family-term-narrows-it-does-not-conflict.md)); (c) when code decides *no*,
the LLM only chooses *what question to ask*.

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

**A read the caller stopped carries no byte count, and says so** — a property of the one loop, not of
whichever caller trips it. `BoundedReader.abandoned` (`CONTEXT.md`: **Abandoned read**), never
`exhausted`, which names the opposite case: `compressed_bytes` is *absent* rather than zero, and is
not a measurement while that flag is true.

**Enforced by.** The `PreToolUse` hook (`hooks/guards.py`, size-blind);
`test_the_read_budget_bounds_bytes_read_however_large_the_file`,
`test_the_byte_budget_binds_when_the_reads_are_long`, `test_the_reader_stops_at_the_read_budget`,
`test_the_reader_stops_at_the_byte_budget`,
`test_an_abandoned_read_says_so_instead_of_reaching_into_a_closed_handle` and
`test_a_read_that_finished_is_not_abandoned_however_it_ended` (`tests/test_probe.py`);
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

**Why.** The caller is a machine. Every skill action maps to a deterministic `seqforge <verb>` with
no LLM in it, and the conventions that follow — JSON by default, refusal as an exit code, no
`--resume` — are [ADR-0013](../adr/0013-cli-is-a-machine-interface.md). A skill documenting a verb
the app does not have is a confident instruction to fail.

**Enforced by.** `test_skill_documents_only_real_cli_verbs` (`tests/test_skills.py`), which
introspects the live Typer app — verb, subcommand and long flags — so a rename goes red;
`test_the_cli_surface_exits_and_answers_as_documented` (`tests/test_cli.py`) for the exit contract.

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
recovers what it declares, and the biconditional proves that two entries with identical
`backend.params` are processing-equivalent (the argument is in
[`docs/agents/kb.md`](kb.md)). A new tech that silently collides with an existing one at
rungs 0–2 without declaring it fails.

**Enforced by.** `kb roundtrip` (exit 3); `test_every_kb_spec_roundtrips`,
`test_the_benign_twin_biconditional_holds_over_every_loaded_spec_pair` and
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

**Why.** Four decisions, one rule: the dataset is write-once and the recipe is plural, and the keys
that say how reads are **parsed** are disjoint from the keys that say what to **count** — so "a user
instruction contradicts the bytes" is *inexpressible* rather than merely refused.

- [ADR-0004](../adr/0004-two-artifacts-not-one.md) — two artifacts, different lifetimes; a change of
  intent never perturbs `dataset_hash`.
- [ADR-0005](../adr/0005-run-id-is-the-pairing.md) — the compiled run is identified by the pairing,
  and the `run_id` formula lives there and nowhere else.
- [ADR-0011](../adr/0011-closed-instructable-surface.md) — `backend.params` is byte-decided and
  never instructable; the recipe owns counting.
- [ADR-0012](../adr/0012-produce-every-answer-rather-than-ask.md) — produce every answer rather than
  ask, whenever every answer is affordable.

**Enforced by.** `dataset_content_hash` covering exactly two sections, plus the recipe-sweep
hash-invariance test and the `models/{dataset,processing}.py` import-graph test (`tests/test_models.py`,
`tests/test_manifest.py`); `compose` refusing a mismatched pin; the `Backend` key-allowlist (`kb lint`
and every `load_spec`); `params_gate`'s disjointness / coverage / three-owner faithfulness checks and
`extra="forbid"` on the processing models; `kb e2e-introns` with the override deleted
(`composed_soloFeatures ⊇ {Gene, GeneFull}`) and the eval's "questions asked" metric.

## The demo dataset, and the two disciplines that outlived the held-out case

`PRJNA1027859` is the pilot's worked example — read it, run it, write the tutorial from it — and
**there are no held-out cases**: [ADR-0016](../adr/0016-no-held-out-dataset.md) retired the
reservation and deleted its guard and registry. Two disciplines survive it. Real data, and its path,
stay out of git. And `expected.yaml` is pre-registered before a run, in claims that are *checkable*,
because only a prediction can be wrong.

**Enforced by.** `test_skill_never_leaks_a_lab_path` (`tests/test_skills.py`) for the path — a
`kind: local` case names an environment variable, never a path;
`test_the_pilots_pre_registered_sample_facts_are_checkable_and_hold` (`tests/test_records.py`),
`test_extra_keys_in_expected_are_rejected` and `test_corpus_is_green` (`tests/test_evals.py`) for
the prediction.

## Two resolvers, two refusals

`resolve` holds two resolvers, and they are siblings rather than a stage and a side-input. They part
on disagreement: the byte resolver **blocks**, and the metadata resolver *decides* and only
**warns** — null over wrong, never a question. The asymmetry is
[ADR-0010](../adr/0010-two-resolvers-one-blocks-one-warns.md); the line is
[`resolve/records.py`](../../src/seqforge/resolve/records.py). What *counts* as a disagreement is
narrower than it looks: an asserted family term that narrows to the observed leaf is agreement, and a
string naming no KB node asserts nothing at all —
[ADR-0020](../adr/0020-a-family-term-narrows-it-does-not-conflict.md). And two equal authorities must
be two *sources*: within one deposit the slot the submitter typed outranks a model's reading of that
same deposit's prose, at both the resolve and the harvest layer —
[ADR-0021](../adr/0021-one-deposit-is-one-source-at-every-layer.md).

**Enforced by.** `test_single_cell_metadata_but_bulk_bytes_surfaces_a_collapse_conflict` and
`test_bulk_metadata_but_single_cell_bytes_surfaces_a_reverse_conflict` (`tests/test_resolve.py`) on
the blocking side, with `test_an_archive_filing_word_asserts_no_chemistry_at_all` and
`test_a_family_hypothesis_is_agreement_with_the_leaf_the_bytes_decided` on what does not reach it;
`test_the_sample_attribute_precedence_table` (`tests/test_records.py`),
parametrized over every cell of the table, and
`test_the_metadata_resolver_is_handed_identity_not_signal`, which keeps probe signal out of it.
