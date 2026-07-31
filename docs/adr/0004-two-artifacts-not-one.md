# 4. Two artifacts: the immutable dataset and the plural recipe

Date: 2026-07-31

## Status

Accepted. Supersedes the single-manifest, three-section shape.

## Context

The manifest once had **three sections** — library, experiment, processing — and the justification
was a pun. R4 names **three truths**; the manifest had three sections; the two were assumed to be
the same three.

They are not. R4's three truths are the three *bases*, and nothing in R4 ever depended on there
being three *sections*. §1.0 listing **four** bases (`observed`, `asserted`, `inferred`,
`user_confirmed`) against three sections was the tell nobody read.

The damage was concrete. Because both were three, `processing` inherited the grammar of a truth —
`Evidenced` fields, an "authority", a uniform `basis="inferred"` stamped on by construction — and
then `compose` read almost none of them: **4 of its 6 fields had no reader at all**. A field that is
never read cannot produce the `Conflict` R4 promises. It was decoration with a provenance envelope
on it.

## Decision

**Two artifacts, with different lifetimes.**

- **`manifest.yaml`** — what the data IS. A finished assay is immutable: the bench did what it did.
  Two truths (library + experiment), one lifetime, write-once, content-hashed, one per dataset.
- **`processing.yaml`** — what to DO with it. Plural, sparse (empty is legal), and the *only* place
  `user_confirmed` is written — the basis unwritten anywhere else in seqforge since the beginning.

`basis` in the recipe records **who decided**, not how we know: a CLI flag or an `--instruction`
document → `user_confirmed`, policy → `inferred`. The two `user_confirmed` tiers differ only in
*precedence*; the channel lives in `evidence`.

## Why not one artifact with a processing section

A fact and a choice have different lifetimes. Same manifest + different recipe = a different
pipeline (GeneFull vs Gene, a different genome) with the **manifest hash unchanged**. Folding intent
into the manifest hash destroys that: uniform reprocessing across 10⁴ datasets stops being a recipe
swap and becomes a re-derivation, and the dataset's identity moves every time someone changes their
mind. It also produced a real collision — see [ADR 0005](0005-run-id-is-the-pairing.md).

## Why `dataset` is optional on the recipe

`dataset is None` ⇒ a **template**, portable across 10⁴ datasets; a mandatory pin would destroy the
plurality this split exists for. Set ⇒ **bound**, and `compose` refuses a mismatch with a `Blocker`
and never auto-repins. `compose` ALWAYS writes the bound form it used to `processing.lock.yaml` —
disk is state, not input.

## So in code

**A change of intent must never perturb `dataset_hash`.** A field that says what the data IS goes in
`models/dataset.py` and inside `dataset_content_hash`; a field that says what to DO with it goes in
`models/processing.py` and outside it. Never import a processing type from the dataset models, never
widen `dataset_content_hash` past `library` + `experiment`, and never auto-repin a bound recipe —
`compose` refuses the mismatch and writes the bound form to `processing.lock.yaml`.

**Gate.** `test_dataset_hash_is_invariant_across_a_processing_sweep` and
`test_a_template_is_portable_but_a_bound_one_refuses_a_foreign_dataset` (`tests/test_manifest.py`);
`test_the_module_graph_enforces_the_split` (`tests/test_models.py`), which is the import-graph check;
`test_compose_writes_the_bound_processing_lock` (`tests/test_compose.py`).

## Consequences

- §1.0 needs no distinct `policy_default` basis: once a section carries a *varying* basis,
  `inferred` plus an evidence ref naming the rule is distinguishable by inspection.
- `DatasetProvenance` omits `workflow_version` on purpose — the assay happened before we had an
  opinion about which rules would run over it. That belongs to the recipe.
- The instructable surface must stay closed and disjoint from the parse keys, or the split leaks
  back: [ADR 0011](0011-closed-instructable-surface.md).
- Envelopes now track decisions rather than sections:
  [ADR 0006](0006-one-judgement-one-envelope.md).
