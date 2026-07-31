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

## So in code

**Write one prompt for every provider, and let no provider's guarantee stand in for a check of
ours.** Never branch the prompt on the provider — `prompt_version` has to stay comparable across
vendors, or every eval number becomes provider-local. Never widen a provider adapter to
post-process, repair or partially accept a response: a malformed batch fails whole at
`ExtractionResult.model_validate_json`, because half a batch is the only outcome worse than none. And
when no credential names a provider, refuse — never fall back to one, because extracting under a
different model than intended is a provenance bug that looks like success.

**Enforced by.** `test_resolve_provider_walks_the_precedence_table` and `test_provider_defaults`
(`tests/test_extract.py`) for the selection;
`test_deepseek_shaped_provider_requests_json_mode_and_flows_into_verify` (same file) for one prompt
reaching verification through the weakest-shaped provider, and
`test_system_prompt_satisfies_the_json_mode_contract` for the prompt itself;
`test_extract_records_provider_in_provenance` for `provider/model`.

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
- Known gap: **no transient API error is retried, by either adapter.** A 429, a 5xx or a timeout
  becomes `ProviderUnavailable` on the first attempt, `harvest extract` reports `llm_unavailable`,
  and the run exits 1. The only retry that exists is narrower and sits in one adapter:
  `OpenAICompatibleProvider` — and so the DeepSeek preset — re-issues a bounded number of times when
  json_object mode returns an **empty** body, because that is a provider hiccup rather than the
  document saying nothing (which is a well-formed `{"drafts": []}`). The Anthropic path has neither
  retry. A bounded backoff around both calls is the fix.
