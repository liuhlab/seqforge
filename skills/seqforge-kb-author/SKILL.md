---
name: seqforge-kb-author
description: >-
  Author a new knowledge-base technology entry (spec.yaml + README.md +
  fixtures) and prove it with `seqforge kb roundtrip|lint|e2e`. Use
  when adding support for a sequencing chemistry seqforge does not know, or when
  editing an existing spec. The skill drafts prose; the verbs that write and
  verify are deterministic.
---

# seqforge kb-author

```bash
seqforge kb list | show TECH | lint
seqforge kb roundtrip TECH        # spec -> synth -> probe -> recover; recovered == declared?
seqforge kb e2e --workdir DIR     # the real count matrix, vs injected truth
seqforge kb e2e-introns --workdir DIR --assembly ce11   # the GeneFull path
```

## R10: every entry is executable and self-testing

A `spec.yaml` that cannot round-trip is not knowledge, it is a note. `kb roundtrip` gates it, and the
pairwise checks live in the SUITE, not in a verb: `test_no_spec_pair_is_confusable_without_declaring_it`
and `test_section_12_biconditional_holds_over_every_loaded_spec_pair` both collect from
`kb.list_spec_ids()`, so your new spec is covered because it exists, not because someone remembered.

**There is no `kb confusability` verb.** This skill documented one for a year and it was never built —
found 2026-07-16 when the verb guard learned to check subcommands instead of only groups.

**`decidable_by` is DERIVED, do not write one.** It is the union of `distinguishable_by` over your
processing-divergent confusables. It used to be a hand-typed field on every spec that nothing read,
two of them carrying the comment "CI-computed union over the divergent confusables" — no CI computed
it. Declare the confusables honestly and the summary follows.

**A whitelist you declare must be one we ship.** `test_a_spec_that_calls_onlists_decisive_can_actually
_reach_one` checks it: a spec whose decisive mechanism cannot fire looks exactly like one that works,
right up until a real dataset arrives. SPLiT-seq is the standing example — it names three barcode
lists, we ship none of them, so its three most important tests silently ABSTAIN.

The generator reads **only** `spec.reads` — never `signature` or `backend`. That is what makes the
round-trip a real test rather than a tautology, so do not "help" by teaching the generator about the
signature.

## Never write a value from memory

This is the rule that matters most here, and it is not stylistic. **Look up every one:**

- **EFO CURIEs** — verify against live EFO (EBI OLS). A recalled CURIE is usually plausible and wrong.
- **Read structure, linkers, offsets** — from the primary paper's oligo tables or scg_lib_structs.
  **Pin the kit version.**
- **`soloStrand`** — derive it from the oligo orientation and cite the derivation. Never pattern-match
  from 10x: a wrong strand leaves most reads unassigned while STARsolo exits 0 and emits a matrix
  that merely looks thin. Neither the params gate nor a simulated e2e can catch it — a simulation
  would test your own assumption against itself, which is circular. Real data or a derivation.
- **`soloCBposition`/`soloUMIposition`** — never hand-enter a quadruple. They are derived from the
  element model at compose time.

The SPLiT-seq entry is the worked example: its strand is derived from the paper's Table S12 oligos,
corroborated by the authors' own code, and it records the honest caveat that the field's practice is
weaker than its claim.

## Declare confusables, including the painful ones

`confusable_with` must list entries that share your geometry, with `distinguishable_by`. Under-declaring
fails CI. Two relationships:

- `processing_equivalent` — byte-identical backend params (v3 vs v3.1) → §12 benign: record both,
  ask **zero** questions.
- `processing_divergent` — the answer changes what runs → must be separable by a declared mechanism
  (usually `onlist`), or it becomes exit 4.

Real gotcha: chemistries can differ by an 8 bp offset (SPLiT-seq v1 has Round1 at 86-93; Parse/v2 at
78-85). Published param quadruples disagree in the wild for this reason — different chemistries, not
typos.
