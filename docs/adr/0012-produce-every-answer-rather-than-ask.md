# 12. Produce every answer rather than ask — `soloFeatures` defaults to all five

Date: 2026-07-31

## Status

Accepted. Supersedes the CHANGELOG's exit-4 remedy, which was withdrawn before it was implemented.

## Context

`kb e2e-introns` (ce11 + WS298) measured a real defect and priced it:

```text
gene_signal_lost = 0.407          # --soloFeatures Gene discards 40.7 % of a nuclear library
composed_soloFeatures = [Gene]    # i.e. the compiler WOULD have emitted it
```

The KB filed `soloFeatures` under `backend.params`, but 10x 3′ v3.1 chemistry is **byte-identical**
for cells and nuclei: what differs is the RNA population — a property of **sample prep**, not
chemistry. No probe can see it. The defect was surfaced by pre-registering PRJNA1027859
(single-**nucleus** RNA-seq) from declared metadata, before the run, without touching the data.

The first remedy, written into the CHANGELOG: an unknown prep raises a `Question` and exits 4.

## Decision

**Dissolve the question rather than answer it.** Once counting moved to the recipe
([ADR 0011](0011-closed-instructable-surface.md)), `SoloQuant.features` **defaults to all five** solo
features — one alignment, five counting rules, one pass — so `GeneFull` is computed whether or not
anyone says the prep is nuclear. The exit-4 remedy was **withdrawn, not implemented**.

The general rule — the closing clause of the router's two-artifacts rule, *produce every answer
rather than ask*: **never escalate an ambiguity whose every answer you can afford to emit.** Escalate
only where the answers are genuinely exclusive — a genome, an aligner.

## Why not the question

It traded a silent wrong answer for a question, and the all-five default buys back both: no wrong
answer *and* no question. An exit-4 that never needed to fire only trains people to route around
exit codes, which costs us the exit codes that do need to fire
([ADR 0013](0013-cli-is-a-machine-interface.md)).

## So in code

**Never escalate an ambiguity whose every answer you can afford to emit.** Before writing a
`Question` or an exit code, price the alternative: if one pass can produce all the answers, produce
them and let the recipe narrow afterwards — that is why `SoloQuant.features` defaults to all five.
Ask only where the answers are genuinely exclusive, such as a genome or an aligner. An exit code that
fires when it need not trains callers to route around exit codes, which costs the ones that must
fire.

**Gate.** `test_the_default_counts_the_nuclear_features_without_being_asked` (`tests/test_manifest.py`)
for the default, and `test_bulk_never_gets_solo_features` (same file) for its limit;
`test_solo_quant_rejects_velocyto_without_gene` and `test_solo_quant_rejects_duplicates_and_emptiness`
(`tests/test_models.py`) for the ordering validators; `kb e2e-introns`, with its
`[Gene, GeneFull]` override deleted, asserting `composed_soloFeatures ⊇ {Gene, GeneFull}` on the
compiler's own params. **Nothing counts questions asked** outside the eval's own metric, so "we
dissolved this rather than asking" is a review judgement.

## Consequences

- `SoloQuant.features` is **ordered** (`[0]` is primary), with validators for "no duplicates" and
  "Velocyto requires Gene" — a real STAR constraint no enum can express. `--quantify` still narrows.
- `quantification` is a discriminated union (`SoloQuant | BulkQuant`) and is no longer decorative:
  `params_gate` fails if the emitted config disagrees with it. Policy used to hardcode `"gene"` into
  the manifest and let compose read the KB instead — two sources of truth that could not disagree
  only because one was never read.
- **The fixture that priced the defect is now the gate that prevents it.** With its `[Gene,
  GeneFull]` override deleted, `kb e2e-introns` runs on the compiler's own params and asserts
  `composed_soloFeatures ⊇ {Gene, GeneFull}`.
- Velocyto is unconditional — a **maintainer decision (2026-07-15), not a measurement**. The
  pre-registered rule (">2× wall-clock or over the `mem_gb` hint ⇒ drop to four") was *retired*
  rather than tested-and-passed. The two leave the same trace unless someone records which happened;
  this is that record.
- The cost of "every answer" is bounded because the alignment is shared: five counting rules ride
  one pass, which is exactly what makes this affordable and what limits the rule's reach.
