# 11. The instructable surface is closed, and split parse-vs-count

Date: 2026-07-31

## Status

Accepted.

## Context

An instruction like "align in GeneFull mode" enters the compiler as an `Assertion` on a
`processing.*` field, with a quote that greps back — we accept instructions only because we never
trust the model to *act* on them, only to *find* them.

If that same key space could also name **how reads are parsed**, a user instruction could contradict
the bytes, and the compiler would need a precedence rule for "the human says 26 bp, the reads say
28 bp". Any answer to that question is wrong: honouring the instruction corrupts the matrix,
refusing it makes the instruction surface a lie.

## Decision

**Two disjoint key sets.**

- **`backend.params` (KB) says how to *parse* reads** — `soloType`, CB/UMI offsets and lengths,
  whitelist, strand. Byte-decided, **never** instructable. It is the chemistry-defining minimum
  only; the single interpolation token allowed anywhere in it is `{onlist:<alias>}`, validated —
  any other `{…}` fails.
- **The processing manifest says what to *count***, and against which genome, aligner, environment
  and resources.

Because the two key sets are disjoint, *"a user instruction contradicts the bytes"* is
**inexpressible**. Enforced by a `Backend` key allowlist (`kb lint` + every `load_spec`), the
`params_gate` disjointness / coverage / three-owner faithfulness checks, and `extra="forbid"` on the
processing models.

**What moved out of the KB backend:** the CellRanger-parity knobs (`soloUMIdedup 1MM_CR`,
`soloUMIfiltering MultiGeneUMI_CR`, `clipAdapterType CellRanger4`, `outFilterScoreMin 30`) are
processing **policy**, not chemistry. They are applied at `compose` time from the recipe, so
`backend_identical` stays sensitive to chemistry and blind to policy.

## Consequence: `backend_identical` means "parses reads identically", so list order is significant

The confusability matrix canonicalizes `backend.params` before comparing. That canonicalization used
to sort **keys and list values**, and its only justification was normalizing `soloFeatures` order.
`soloFeatures` has since left `backend.params` — it says what to *count*.

What the sort would normalize now is the only list-valued **parse** param left: splitseq's
`soloCBwhitelist: [round1, round2, round3]`, which is **positional** — rounds map to CB positions in
order.

Verified before the sort was deleted:

```text
backend_identical(splitseq, splitseq-with-rounds-reversed) == True
```

Two chemistries that parse reads differently, declared benign twins, one config emitted for both. It
had never fired only by the alphabetical accident that `round1 < round2 < round3`.

**Sort keys, never list order.** The predicate is now strictly stronger: two specs differing only in
what they *count* are no longer distinguishable by it, because that is not a chemistry fact at all.
The §2.4 biconditional — `backend_identical(A,B) ⟺ relationship == processing_equivalent` — is
asserted over every loaded spec pair in CI.

## Consequences

- Role placement is part of the canonical form (`readFilesIn` order derived from `reads`), so two
  techs differing only in which read is biological are never falsely labelled benign twins.
- A benign twin pair (v3 / v3.1) is recorded together with **0 questions**; divergence is what
  escalates.
- Once counting lives in the recipe, the counting question can be dissolved rather than asked —
  [ADR 0012](0012-produce-every-answer-rather-than-ask.md).
- This is the mechanism that keeps [ADR 0004](0004-two-artifacts-not-one.md)'s split from leaking
  back: two artifacts with overlapping key spaces would be one artifact with extra steps.
