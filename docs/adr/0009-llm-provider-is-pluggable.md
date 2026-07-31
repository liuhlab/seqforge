# 9. The LLM provider is pluggable *because* span verification makes it swappable

Date: 2026-07-31

## Status

Accepted.

## Context

`harvest extract` is the only LLM touchpoint in a headless run. The obvious way to keep it
trustworthy is to require a provider with guaranteed structured output — Anthropic's strict
`json_schema` — and lock to it, treating the vendor's shape guarantee as part of the correctness
story.

## Decision

**Three providers ship, and nothing downstream trusts any of them.**

| provider | shape | caching | default model |
| --- | --- | --- | --- |
| `anthropic` | strict `json_schema`, **guaranteed** | explicit `cache_control` | `claude-opus-4-8` |
| `deepseek` | `json_object` only, **not** enforced | automatic prefix caching | `deepseek-v4-pro` |
| `openai-compatible` | caller's problem | provider's | caller-supplied |

Code re-greps every quote, checks entailment, and validates the batch against `AssertionDraft`
before anything reaches a manifest ([ADR 0008](0008-llm-surface-carries-only-checkable-fields.md)).
**That is precisely what makes the vendor swappable**: the provider choice is about cost and
extraction quality, never about correctness guarantees — those are R2's, and R2 runs after the model
regardless of who the model was.

## Why a json-object provider is allowed at all

The capability gap is **contained, not papered over**. For json-object providers the schema and a
worked example travel in the prompt (DeepSeek *requires* the literal word "json" plus an example),
and `ExtractionResult.model_validate_json` is the gate: a wrong shape fails the **whole batch**
loudly rather than leaking a half-parsed assertion into a manifest. Half a batch is the only outcome
that would have been worse than none.

## Consequences

- **One prompt serves every provider**, so `prompt_version` stays comparable across runs and across
  vendors — a per-provider prompt would make every eval number provider-local.
- `ExtractorProvenance.model_id` records `provider/model`: the same prompt on a different model is a
  **different extractor**, and evals must tell those runs apart.
- Selection is explicit-beats-implicit (`--provider` / `SEQFORGE_LLM_PROVIDER`, else auto-detect
  from `DEEPSEEK_API_KEY` / `ANTHROPIC_API_KEY`) and **refuses rather than guessing** when no
  credential is present — silently extracting with a different model than intended is a provenance
  bug, and a cheap one to make.
- V4-Flash is ≈3× cheaper than V4-Pro, so provider choice is a real cost lever across 10⁴ datasets —
  which is only safe to pull because it cannot move correctness.
- Known gap: a transient provider error is not retried; DeepSeek's empty-content-in-JSON return
  correctly refuses the batch, but `run` then exits 1. A bounded retry inside `extract` is the fix.
