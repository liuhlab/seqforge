# What DeepSeek, Jev and Claude allow and charge for bulk use

Query date: 2026-11-08. Sources: each provider's own documentation, all read on 2026-11-08 (list
at the end, cited as [D1], [T1], [A1] …), and the call logs committed on the throwaway branch
`prototype/study-reading-deepseek-jev`. No live load test and no paid call was made for this note.

Terms used once and then abbreviated: **concurrency** is the number of requests in flight at once;
RPM, ITPM and OTPM are requests, input tokens and output tokens per minute; Mtok is a million
tokens; **cache hit** is input the provider has already seen as a prefix and bills cheaply; a
**429** is HTTP "too many requests"; **Retry-After** is the header that says how long to wait.

## Question

What do DeepSeek (the V4 models at `api.deepseek.com`), TypeSafe's Jev (System One API) and one
stronger model behind seqforge's Anthropic adapter allow, and what do they charge, for an
unattended run of about 100,000 independent units from one machine: calls at once, requests and
tokens per minute, tiers, the largest request, prices (batch and off-peak too), how each signals
overload, and any terms on bulk use?

## Answer

- **Read:** DeepSeek limits only **concurrency**, per account: 2,500 for Flash and 500 for Pro,
  raised free on request. It publishes no RPM, no tokens-per-minute limit and no tiers. Over the
  limit it returns 429; a busy server returns 503, or holds the connection open with blank lines
  and closes it if inference has not started after 10 minutes [D2][D3].
- **Read:** DeepSeek's off-peak discount exists in November 2026: every price is halved outside
  01:00–04:00 and 06:00–10:00 UTC, Monday to Friday; weekends and Chinese public holidays are
  off-peak all day. That is 35 peak hours a week, so about 79% of the week is off-peak. There is no
  batch API [D1][D5].
- **Read:** DeepSeek renamed its cheap model on 2026-09-10. Send `deepseek-flash` (served by
  DeepSeek-V4.1-Flash-1017). `deepseek-v4-flash`, the name seqforge pins, is a legacy name that
  still works and is billed at the Flash price. Thinking mode is **on by default** for both models
  [D1][D4][D5].
- **Read:** Jev costs $0.042 per Mtok of input, and output is free. The limits are 64k tokens per
  request and 32k per question, as recorded earlier: **confirmed**. The long-context header raises
  both to 262k at the same price, but answers are slower and less accurate. Rate limits are set
  per organization, in requests and tokens **per second**, and appear only in the logged-in
  console; the docs give no numbers and say nothing about concurrency. Overload signals are 429
  (rate limit) and 529 (overloaded), with `retry-after` only "when the response includes one"
  [T1][T2].
- **Read:** For the stronger model, seqforge's adapter pins Claude Opus 4.8: $5 / $25 per Mtok
  in / out, half that through the Message Batches API. Most batches finish within 1 hour; any
  request not done in 24 hours expires unbilled. Limits are per tier. The Start tier allows 1,000
  RPM, 2M uncached ITPM and 400k OTPM, and caps spend at **$500 a month**. Claude Opus 5.5 is
  newer and costs less ($4 / $20) [A1][A2][A3].
- **Measured (prototype logs, 1,887 live calls on 2026-11-08):** 12 DeepSeek Flash calls took
  2.5 s median, 3.3 s p90 and 3.6 s max, at about 4,100 tokens in and 370 out. 1,875 Jev requests
  took 0.20 s median, 0.31 s p90 and 1.3 s max, at about 1,000 tokens in, with at most 8 requests
  in flight. At peak that was 39 Jev requests and 94k input tokens in one second, and no failure
  reached the log. Failures and retries were not logged, so this is not evidence that no request
  was throttled.
- **Inferred:** At the prototype's token sizes, 100,000 DeepSeek Flash calls cost about $170 at
  peak or $85 off-peak. 400,000 Jev requests cost about $17. 100,000 Opus 4.8 calls cost about
  $3,000, or $1,500 by batch, before any thinking tokens. That is over the Start and Build tiers'
  monthly caps.

## Summary table

| | DeepSeek Flash (`deepseek-flash`) | DeepSeek Pro (`deepseek-v4-pro`) | Jev 1.14 (`jev-1.14.0`) | Claude Opus 4.8 (`claude-opus-4-8`) |
|---|---|---|---|---|
| Input, per Mtok (cache miss / hit) | $0.30 / $0.006 peak; $0.15 / $0.003 off-peak [D1] | $1.32 / $0.044 peak; $0.66 / $0.022 off-peak [D1] | $0.042; no cache price listed [T1] | $5 / $0.50 [A1] |
| Output, per Mtok | $1.20 peak; $0.60 off-peak [D1] | $3.96 peak; $1.98 off-peak [D1] | free [T1] | $25 [A1] |
| Cheaper mode | off-peak half price; no batch API [D1] | same [D1] | none documented | Batch API half price, ≤24 h [A1][A3] |
| Calls at once | 2,500 per account; free raise on request [D2] | 500 per account [D2] | not documented | not documented (no concurrency limit is listed) [A2] |
| Requests / tokens per minute | not documented; only concurrency is limited [D2] | same [D2] | per organization, per second; numbers only in the console [T1] | Start tier: 1,000 RPM, 2M ITPM (uncached only), 400k OTPM [A2] |
| Largest request | 1M context, 384k output [D1]; body size not documented | same [D1] | 64k per request, 32k per question; 262k with header; body ≤ 32 MiB [T1][T2] | 1M context at standard price [A1]; body ≤ 128 MB, 256 MB per batch [A4] |
| Overload signal | 429 over concurrency; 503 overloaded; blank keep-alive lines; closed after 10 min unstarted [D2][D3] | same | 429 rate limit; 529 overloaded [T2] | 429 rate limit (also "acceleration limits" on sharp growth); 529 overloaded [A2][A4] |
| Retry-After | not mentioned [D3] | same | sent sometimes [T1] | sent on rate-limit 429; **absent** on a spend-cap 429 [A2][A4] |
| Spend stop | 402 when balance runs out [D3] | same | prepaid credits; may decline when credits reach zero [T3] | monthly cap per tier: $500 / $1,000 / $200,000; then 429 until the 1st [A2] |
| Measured seconds per call (prototype) | 2.5 median, 3.3 p90, 3.6 max (n = 12) | not measured | 0.20 median, 0.31 p90, 1.3 max (n = 1,875) | not measured |

## Findings

### DeepSeek

- **Models.** Two models are served: `deepseek-flash` (DeepSeek-V4.1-Flash-1017) and
  `deepseek-v4-pro` (DeepSeek-V4-Pro-0813). Both have a 1M-token context and up to 384k output
  tokens [D1]. V4 Flash was retired on 2026-09-10. `deepseek-v4-flash` is "temporarily routed" to
  V4.1 Flash [D5]. Pro service continues past 2026-09-14 "with the billing method remaining
  unchanged", until further notice [D5].
- **Prices.** See the table. Off-peak prices are half of peak. Peak is 01:00–04:00 and 06:00–10:00
  UTC, Monday to Friday, excluding Chinese public holidays. All other hours are off-peak [D1]. The
  peak/off-peak split began at 16:00 UTC on 2026-08-16 [D5]. Prices were cut again with V4.1
  Flash on 2026-09-10 [D5]. DeepSeek "reserves the right to adjust" prices [D1].
- **Caching.** Prefix caching is on by default and best-effort, with no hit rate guaranteed. A
  cache entry is cleared "within a few hours to a few days" after it stops being used. Each
  response reports `prompt_cache_hit_tokens` and `prompt_cache_miss_tokens` [D6].
- **Thinking.** Thinking mode is on by default, at effort `high`. It is switched with
  `{"thinking": {"type": "disabled"}}` or set to `low`/`high`/`max` [D4]. Whether chain-of-thought
  tokens are billed as output is not stated on the pages read (see Not established).
- **Concurrency.** Limits are 2,500 for Flash and 500 for Pro. A request counts from the moment it
  is sent until the response completes. The limit applies per account across all keys. Above it
  the API returns 429 [D2]. Higher limits come through a "capacity expansion request", which "has
  no additional cost" [D2]. An account with a raised quota also gets a per-`user_id` limit equal to
  the defaults [D2].
- **Overload.** A waiting non-streaming request receives blank lines and a streaming one receives
  `: keep-alive` comments. "If the request has not started inference after 10 minutes, the server
  will close the connection" [D2]. The error table lists 429 ("too quickly, or have exceeded a
  concurrency or QPS limit"), 500 and 503 ("server is overloaded"). The advice is to "retry … after
  a brief wait", with no `Retry-After` and no QPS figure [D3]. The table names a QPS limit, but no
  page states one.
- **Terms.** The Open Platform Terms (effective 2026-04-29) contain no rate or bulk-use clause
  beyond general good conduct and "not disrupting the normal order of the internet platform". They
  allow outputs to be used for "academic research … training other models (such as model
  distillation)" [D7].

### Jev (TypeSafe System One)

- **Model and price.** `jev-1.14.0` (from 2026-10-14) and `jev-1.13.0` cost $42 per billion input
  tokens ($0.042 per Mtok), and "Output tokens are free" [T1]. `jev-latest` points to 1.14.0
  today. An alias moves on release, so seqforge should keep pinning the versioned ID [T1]. Billing
  draws down prepaid credits. Purchased credits expire after at most 12 months. With no credits and
  no auto-refill, "TypeSafe may decline to generate Output" [T3].
- **Size limits.** "64k per-request / 32k per-question": the request limit covers the state plus
  all questions, and the question limit covers the state plus any one question. "There is no
  separate limit on the number of questions in a request" [T1]. The header
  `TypeSafe-Danger-Zone-Long-Context: true` raises both to 262k. It "costs the same per token" but
  "is slower and less accurate" [T1]. A request over the limit returns 400 `max_tokens_exceeded`.
  The body may be at most 32 MiB, may be gzip- or zstd-compressed, and an oversized body returns
  413 [T2].
- **Rate limits.** "Rate limits are set per organization", "measured in tokens per second and
  requests per second". The organization's numbers are only in the developer console, behind a
  login. Higher limits come with custom and enterprise plans, through a request form [T1].
  Concurrency is not mentioned.
- **Overload.** 429 means "exceeded your rate limit", and 529 means "temporarily overloaded". For
  both, the docs advise exponential backoff [T2]. The SDKs "follow the `retry-after` header when
  the response includes one" [T1]. The Python SDK's default policy retries 408, 429 and every 5xx
  status twice, starting at 0.5 s and capped at 5 s, and honours `Retry-After` and
  `retry-after-ms` [T4].
- **Terms.** The Master Customer Agreement (updated 2026-09-23) forbids exceeding the "Usage
  Limits" in the Order. It also forbids using output "to perform model distillation, train a model
  to imitate the output of the Services, or develop … a similar or competing product". TypeSafe may
  suspend access for breaches, or for actions that "risk harm to … the security, availability, or
  integrity of the Services" [T3]. Training a corpus model on Jev-labelled metadata is not
  obviously "imitating" Jev, but this note does not settle that.

### Claude (the stronger model)

- **Which model.** seqforge's adapter pins `claude-opus-4-8` (`ANTHROPIC_DEFAULT_MODEL` in
  `src/seqforge/harvest/providers.py`). Claude Opus 5.5 is newer and cheaper. It is listed in the
  table below for comparison.

  | | Standard in / out per Mtok | Cache hit | Batch in / out |
  |---|---|---|---|
  | Claude Opus 4.8 | $5 / $25 | $0.50 | $2.50 / $12.50 |
  | Claude Opus 5.5 | $4 / $20 | $0.20 | $2 / $10 |
  | Claude Sonnet 5.5 | $2 / $10 | $0.10 | $1 / $5 |

  Source: [A1]. Claude 4.6 and later models carry the full 1M context at standard price [A1].
- **Tiers.** An organization is placed on a tier automatically "based on usage history and account
  standing". New organizations may start on a lower "Evaluation tier" [A2]. The table below lists
  the limits for Opus 4.x, a bucket that Opus 4.8, 4.7, 4.6 and 4.5 share [A2].

  | Tier | RPM | ITPM | OTPM | Monthly spend cap |
  |---|---|---|---|---|
  | Start | 1,000 | 2,000,000 | 400,000 | $500 |
  | Build | 5,000 | 5,000,000 | 1,000,000 | $1,000 |
  | Scale | 10,000 | 10,000,000 | 2,000,000 | $200,000 |

  Opus 5.5 has its own bucket with higher RPM: 4,000, 10,000 and 13,000 by tier [A2]. Only uncached
  input counts toward ITPM. Limits use a token bucket, so "60 requests per minute might be enforced
  as 1 request per second" [A2]. No deposit amount per tier is published. The docs state no
  concurrency limit.
- **Batches.** Batches cost half price on input and output [A1]. "Most batches complete within 1
  hour". Results are available when all requests finish or after 24 hours. Unfinished requests
  expire unbilled, and "processing might be slowed down based on current demand", which means
  more expiries [A3]. Results stay for 29 days. Limits are 100,000 requests per batch, and 200,000
  queued batch requests on Start (500,000 on Scale) [A2][A3].
- **Overload.** A 429 says which limit was hit, and "when the response has a `retry-after` header,
  wait that many seconds". A sharp rise in usage can trigger 429 "acceleration limits", so traffic
  should grow gradually [A2]. When a tier's monthly spend cap is reached, every request returns 429
  **without** `retry-after` until 00:00 UTC on the 1st [A2][A4]. A spend limit the organization
  sets for itself returns 400 instead [A4]. 529 `overloaded_error` comes from "high traffic across
  all users" [A4].
- **Terms.** Anthropic's commercial terms and usage policy were not read for this note.

## Measured: the prototype's call logs

Four logs (`prototypes/study_reading/runs/<GSE>/calls.jsonl`) hold 1,887 live calls, made on
2026-11-08 between 20:26 and 20:50 UTC. The harness logs only successful calls. Retries sleep
inside the timed call, so a retried call shows as a slow one.

| Provider | Calls | Seconds per call: median / p90 / max | Tokens in: median / p90 / max | Tokens out: median / p90 / max | Logged cost |
|---|---|---|---|---|---|
| DeepSeek (request named `deepseek-chat`; response reported `deepseek-flash`) | 12 | 2.50 / 3.27 / 3.60 | 4,083 / 7,646 / 8,455 | 374 / 672 / 690 | $0.021 at peak prices |
| Jev `jev-1.14.0`, all requests | 1,875 | 0.20 / 0.31 / 1.32 | 1,036 / 2,249 / 10,553 | 20 / 392 / 2,735 | $0.096 |
| Jev, one question per request | 1,248 | 0.20 / 0.31 / 1.28 | 751 / 2,179 / — | 20 median | — |
| Jev, 2–60 questions per request | 627 | 0.20 / 0.31 / 1.32 | 1,300 / 2,820 / — | 364 median | — |

- **Measured:** Jev's latency hardly grew with size. Requests of 5,000–10,553 input tokens took
  0.23 s median (n = 7). The two 60-question requests took 0.23 s and 0.28 s. In total the 1,875
  requests asked 4,939 questions over 2.28M input tokens.
- **Measured:** Jev ran with 8 worker threads, and at most 8 calls were in flight at any time.
  Peak throughput was 39 requests and 94k input tokens in one second, 339 requests in 10 s, and
  906 requests and 836k input tokens in 60 s.
- **Measured:** 19 of the 1,875 Jev calls took over 0.6 s, and 7 took 1.1–1.3 s. A first retry
  sleeps 1 s, so those 7 may hide one retry each. The log cannot tell.
- **Measured:** the DeepSeek requests named `deepseek-chat`, which the changelog said would be
  discontinued on 2026-07-24 [D5]. They still succeeded on 2026-11-08. Their 247–690 output
  tokens fit the alias's documented non-thinking mode. The timings therefore say nothing about a
  thinking-on call, which is what seqforge's adapter now sends by default.
- **Inferred:** the runs fell on a Sunday, which is off-peak [D1]. DeepSeek therefore billed about
  half the logged $0.021.

## Inferred for the scheduling ticket

- **Calls at once.** DeepSeek publishes a concurrency number, and at 2.5 s per call, 2,500 Flash
  slots would allow up to about 1,000 calls per second, far above what 100,000 units need. Jev and
  Claude publish rate limits but no concurrency number. For Jev, the only evidence is 8 in flight
  at 39 requests per second, with no failure logged.
- **Retry rules seqforge does not yet cover.** `_RETRY_STATUS` (429, 500, 502, 503, 504) leaves
  out **529**, the overload code of both Jev and Claude. Claude's SDK retries a 529 twice and
  then raises, and seqforge would then call it a verdict, not a blip. A 429 without
  `retry-after` from Claude can mean the monthly spend cap, which no backoff fixes. DeepSeek's
  402 (balance exhausted) is correctly a verdict.
- **Spend ceiling.** At prototype token sizes, an Opus 4.8 pass over 100,000 units costs about
  $3,000, or $1,500 by batch. That exceeds the Start ($500) and Build ($1,000) monthly caps, so
  the account's tier bounds what the ladder can send up. DeepSeek Flash costs about $170 per
  100,000 calls at peak, and Jev about $4 per 100,000 requests of 1,000 tokens.
- **Off-peak timing.** Running DeepSeek outside the 35 weekday peak hours halves its cost, and a
  weekend run is off-peak throughout. No other provider prices by time of day.

## Not established

- Jev's actual requests-per-second and tokens-per-second limits for our organization. They are
  only in the logged-in console.
- Jev's concurrency limit, if any. The docs are silent.
- DeepSeek's requests-per-minute or QPS limit. The error table mentions "QPS", but no page gives a
  number.
- Whether DeepSeek ever sends `Retry-After`. The docs do not say.
- Whether DeepSeek bills chain-of-thought tokens as output, and how many a thinking-on writer call
  produces. The thinking and pricing pages do not say.
- DeepSeek's maximum request body size.
- Claude: our organization's current tier and any deposit or credit needed to move up. The docs
  say tiers are assigned automatically and publish no deposit figures.
- Concurrency limits for Claude. The docs list only RPM, ITPM and OTPM.
- How long DeepSeek keeps routing `deepseek-v4-flash` and `deepseek-chat` ("temporarily").
- Seconds per call for DeepSeek Pro, for thinking-on DeepSeek calls and for any Claude model. The
  prototype measured none of them.
- Whether any request in the prototype was throttled or retried, because the log records only
  successes.
- Anthropic's commercial terms on bulk use (not read), and whether TypeSafe's ban on training a
  model "to imitate the output" covers seqforge's corpus use.

## Method

Each provider's documentation was downloaded as Markdown where offered and read directly. The
pages were DeepSeek's docs site and terms, TypeSafe's Mintlify docs, OpenAPI spec and customer
agreement, and Anthropic's platform docs. The prototype logs were read with `git show` from the
branch named above and summarised with a short script. Percentiles use linear interpolation;
"in flight" is reconstructed from each call's end timestamp minus its duration.

## Sources

| Ref | Page | Read |
|---|---|---|
| D1 | DeepSeek, Models & Pricing — <https://api-docs.deepseek.com/quick_start/pricing> | 2026-11-08 |
| D2 | DeepSeek, Rate Limit & Isolation — <https://api-docs.deepseek.com/quick_start/rate_limit> | 2026-11-08 |
| D3 | DeepSeek, Error Codes — <https://api-docs.deepseek.com/quick_start/error_codes> | 2026-11-08 |
| D4 | DeepSeek, Thinking Mode — <https://api-docs.deepseek.com/guides/thinking_mode> | 2026-11-08 |
| D5 | DeepSeek, Change Log (entries 2026-04-24 to 2026-10-17) — <https://api-docs.deepseek.com/updates> | 2026-11-08 |
| D6 | DeepSeek, Context Caching — <https://api-docs.deepseek.com/guides/kv_cache> | 2026-11-08 |
| D7 | DeepSeek Open Platform Terms of Service — <https://cdn.deepseek.com/policies/en-US/deepseek-open-platform-terms-of-service.html> | 2026-11-08 |
| T1 | TypeSafe, Models (price, context, long context, rate limits) — <https://docs.typesafe.ai/models> | 2026-11-08 |
| T2 | TypeSafe, Request rules and errors — <https://docs.typesafe.ai/api/errors> | 2026-11-08 |
| T3 | TypeSafe, Master Customer Agreement — <https://typesafe.ai/legal/mca> | 2026-11-08 |
| T4 | TypeSafe Python SDK, Retries — <https://docs.typesafe.ai/sdk/python/api/retries> | 2026-11-08 |
| A1 | Anthropic, Pricing — <https://platform.claude.com/docs/en/about-claude/pricing> | 2026-11-08 |
| A2 | Anthropic, Rate limits — <https://platform.claude.com/docs/en/api/rate-limits> | 2026-11-08 |
| A3 | Anthropic, Batch processing — <https://platform.claude.com/docs/en/build-with-claude/batch-processing> | 2026-11-08 |
| A4 | Anthropic, Errors — <https://platform.claude.com/docs/en/api/errors> | 2026-11-08 |
