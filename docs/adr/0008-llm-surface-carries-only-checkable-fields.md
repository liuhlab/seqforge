# 8. The LLM's output surface carries only fields code can re-check

Date: 2026-07-31

## Status

Accepted.

## Context

Two fields were proposed for the model's structured-output surface, and both were rejected for the
same reason.

1. **Exact character offsets.** The original span contract asked the model for a `[start, end)` into
   the normalized document. An LLM cannot count characters, so that contract *false-rejects*: a
   correct extraction with a real quote fails on arithmetic. The tripwire fires on the wrong thing.
2. **A `subject`** — "which sample is this claim about". Without it, a sample-level fact looks
   unattributable: six samples in a dataset, one model, one pile of prose.

## Decision

`AssertionDraft` is `{field, value, span:{doc_sha256, quote, context?}, llm_confidence}` — **no
offsets, no subject**, `value` a plain string. It is the LLM's only structured-output surface. Code
searches the normalized document for the quote, computes the offsets, composes the stored
`Assertion`, and **owns** both verification flags (`span_verified`, `entailment_ok`).

**The subject is the document.** Each level of an archive record is rendered as its **own
document** (`harvest/normalize.normalize_record`), so a sample-level document holds one sample's
prose and nothing else. "Which sample" is answered by *which file code handed the model* — code
knows because code chose. A document's `role` (reference vs instruction) and `scope` are set the
same way, from how the document arrived, never from its contents: a filename trigger would be
magic, unauditable, and spoofable by renaming a download.

## Why no `subject` field

It would be an authority with no quote to check. Every other field on the draft is verifiable
against the document; `subject` would be the model naming something the document need not contain,
and a wrong one silently mis-files a **permanent** fact (the `experiment` section is inside
`dataset_hash` and the manifest is never rewritten) onto the wrong sample. The rule generalizes: a
field the verifier cannot re-derive does not belong on the LLM-facing schema.

## Consequences

- The P4 tripwire is **fail-closed** instead of false-rejecting.
- `span_verified` catches *fabricated provenance*; `entailment_ok` catches a *real quote
  mis-attached to a wrong value* — the more common LLM failure, e.g. a verbatim "single-cell
  RNA-seq" span pinned to "10x 3′ v3.1". Both must hold before an `Assertion` reaches `manifest
  fill`.
- Only free text is rendered into a record document. The structured half (`strain = CQ758`) is
  already a key and a value, so putting it in front of a model would be asking it to transcribe
  something code can copy — a chance to be wrong and no chance to be useful.
- A **dataset**-scoped document (a paper, a README) may make dataset-wide claims, but its
  *sample* claims are recorded `inferred`, never `asserted`; see
  [ADR 0010](0010-two-resolvers-one-blocks-one-warns.md).
- The `LLM_FACING` pin keeps the surface from growing; verification runs inside `harvest extract`,
  with `harvest verify` as a standalone re-checker.
