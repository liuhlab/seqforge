# 11. The instructable surface is closed, and split parse-vs-count

Date: 2026-07-31

## Status

Accepted. **Amended by [0022](0022-three-owners-for-an-aligner-param.md)** on the CellRanger-parity
knobs below: they are neither the KB's nor the recipe's but the workflow module's, written as literals
in its shell block. The parse/count split this record exists to state is unchanged.

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
`soloUMIfiltering MultiGeneUMI_CR`, `clipAdapterType CellRanger4`, `outFilterScoreMin 30`) are not
chemistry, so `backend_identical` stays sensitive to chemistry and blind to them.

This record originally said they were processing **policy**, applied at `compose` time from the
recipe. **That never happened**: measured on `main` at `70ba9fd`, none of the four appeared in the
module, in `models/processing.py` or in any spec, so they moved out of the KB and landed nowhere, and
every matrix seqforge shipped until then was counted with STARsolo's defaults.
[0022](0022-three-owners-for-an-aligner-param.md) settles where they belong — the workflow module, as
literals, because their value varies with nothing — and states the test that decides an owner.

## Why the confusability matrix sorts keys and not list values

`backend_identical` means *"parses reads identically"*, so list order inside it is significant. The
confusability matrix canonicalizes `backend.params` before comparing, and that canonicalization used
to sort **keys and list values** — its only justification being to normalize `soloFeatures` order.
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
The biconditional — `backend_identical(A,B) ⟺ relationship == processing_equivalent` — is asserted
over every loaded spec pair in CI.

## So in code

**Never make a parse key instructable, and never let a count key be decided by bytes.** A new key
belongs to `backend.params` only if reads cannot be *parsed* without it; everything about what to
count, and against which genome, aligner, environment and resources, belongs to the recipe
(`CONTEXT.md` keeps the two apart as **Backend params** and **Recipe**). Do not add a template token
beyond `{onlist:<alias>}`, do not put a knob that varies with nothing in a spec *or* a recipe — the
CellRanger-parity set is a module literal, see [0022](0022-three-owners-for-an-aligner-param.md) —
and when canonicalizing for comparison, **sort keys, never list values**.

**Enforced by.** The `Backend` key allowlist, with `test_backend_rejects_illegal_template_token`
(`tests/test_kb.py`); `extra="forbid"` on the processing models, with
`test_the_processing_manifest_refuses_an_unknown_key` (`tests/test_models.py`); `params_gate` via
`test_every_chemistry_emits_its_required_keys_and_passes_the_params_gate` (`tests/test_compose.py`);
`test_the_benign_twin_biconditional_holds_over_every_loaded_spec_pair` and
`test_no_spec_pair_is_confusable_without_declaring_it` (`tests/test_kb.py`) for the sort.

## Consequences

- Role placement is part of the canonical form (`readFilesIn` order derived from `reads`), so two
  techs differing only in which read is biological are never falsely labelled benign twins.
- A benign twin pair (v3 / v3.1) is recorded together with **0 questions**; divergence is what
  escalates.
- Once counting lives in the recipe, the counting question can be dissolved rather than asked —
  [ADR 0012](0012-produce-every-answer-rather-than-ask.md).
- This is the mechanism that keeps [ADR 0004](0004-two-artifacts-not-one.md)'s split from leaking
  back: two artifacts with overlapping key spaces would be one artifact with extra steps.
