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

**The gate accepts exactly two envelopes: `{"drafts": [...]}`, and a bare top-level array, which is
rewrapped and falls into the identical per-draft validation.** The whole response must BE the array;
the set is closed and a third shape raises. Salvaging a drafts-shaped fragment out of a response that
carried something else stays forbidden — that is the silent half-parse. Measured cost of refusing the
array instead: 6 of 141 documents lost whole on `deepseek-v4-flash`, each of them valid.

**A wrapper at the seam is not a wider adapter.** `harvest/meter.py` satisfies `LLMProvider` and wraps
one so a run has somewhere to stand *between* two calls — to count real requests, to refuse past a
token **Ceiling**, and to hold the transcript. It reads no meaning from `response.text` and hands the
response back byte-identical, so the two adapters stay untouched. What it must not touch is identity:
`ExtractorProvenance.model_id` is `f"{provider.name}/{model}"`, so it proxies `name` and
`default_model()` rather than naming itself.

**Enforced by.** `test_resolve_provider_walks_the_precedence_table` and `test_provider_defaults`
(`tests/test_extract.py`) for the selection;
`test_deepseek_shaped_provider_requests_json_mode_and_flows_into_verify` (same file) for one prompt
reaching verification through the weakest-shaped provider, and
`test_system_prompt_satisfies_the_json_mode_contract` for the prompt itself;
`test_extract_records_provider_in_provenance` for `provider/model`, and
`test_the_meter_proxies_the_wrapped_providers_identity` for the same fact through the wrapper —
`test_the_meter_hands_the_response_back_untouched` is the other half.

## Consequences

- **One prompt serves every provider**, so `prompt_version` stays comparable across runs and across
  vendors — a per-provider prompt would make every eval number provider-local.
- `ExtractorProvenance.model_id` records `provider/model`: the same prompt on a different model is a
  **different extractor**, and evals must tell those runs apart.
- Selection is explicit-beats-implicit (`--provider` / `SEQFORGE_LLM_PROVIDER`, else auto-detect
  from `DEEPSEEK_API_KEY` / `ANTHROPIC_API_KEY`) and **refuses rather than guessing** when no
  credential is present — silently extracting with a different model than intended is a provenance
  bug, and a cheap one to make.
- **Model choice is a cost lever, and the corpus is what decides which way it points.** Per-token
  price is the wrong currency: the DeepSeek default is `deepseek-v4-pro` (#188, #184), which measured
  head-to-head was **faster and spent fewer output tokens** than flash, whose surplus was claims the
  prompt never asked for and `verify_drafts` throws away. Flash is one `--model` away, and neither is
  an allowlist — the model string is passed through, so an unknown name comes back as a 400 rather
  than needing a release here. The run, and the caveat that it is single-trial draws, is dated in
  `evals/README.md`.
- **The default model must still be named, not assumed.** The same prompt on a different model is a
  *different extractor*, so a run's numbers transfer to no other one. `eval run` stamps
  `EvalReport.extractor` — `{provider, model, prompt_version}`, the provider's default resolved
  rather than echoed as `null` — so a report always names the extractor it is a claim about, and so
  the coverage warning can name the model that ran instead of prescribing one. Absent on `--no-llm`,
  which has none.
- **Transient API errors are retried once the provider says they are transient**, and the loop is
  above both adapters rather than inside either (`_complete_with_retry`, `harvest/extract.py`). The
  provider classifies (`ProviderUnavailable.transient` / `.retry_after`, because by the time the
  exception reaches the loop the SDK's own is gone and "no credential" is indistinguishable from
  "rate limited"), and the loop only obeys. One budget of `_MAX_RETRIES` covers both the transient
  API case and the empty-body hiccup that used to have its own nested loop in
  `OpenAICompatibleProvider`. Usage accrues across attempts, because a refused call still burned
  tokens.
- **The usage keys mean one thing across providers**, which is what makes a token Ceiling possible at
  a pluggable seam at all. `input_tokens` is *every* input token the request was billed for;
  `cache_read_tokens` and `cache_write_tokens` are a breakdown of that same total, not extra tokens
  beside it. DeepSeek's `prompt_tokens` is already inclusive and Anthropic's three input buckets are
  disjoint, so the Anthropic normalizer folds them — without that, one run reads as 3.5M tokens on
  one provider and 675K on the other, and the ceiling would mean a different thing per vendor.
