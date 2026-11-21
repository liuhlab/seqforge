# What OpenAI's gpt-6-luna costs and allows, and what the lab gateway needs to call it

Query date: 2026-11-20. Sources: OpenAI's and DeepSeek's own documentation, read on 2026-11-20
(cited as [O1], [D1] …), and the code of `liuhlab/liulab-llms` at its `main` (commit `741760a0`,
2026-11-19), read from a local clone and checked against GitHub. No paid call was made, no key was
added and nothing was run.

Every claim is marked **read** (stated by a source), **measured** (counted from code or arithmetic
on read numbers) or **inferred** (my reading of what the source implies). The documentation pages
were fetched through a tool that summarises each page; the model page [O1] was also checked against
its raw HTML for the price, context, output cap, reasoning default, rate-limit table and snapshot.

Terms used once and then abbreviated: **Mtok** is a million tokens; **RPM** and **TPM** are
requests and tokens per minute; **cached input** is input OpenAI has already seen as a prefix and
bills cheaply; **Flex** is OpenAI's slower, half-price processing tier; a **429** is HTTP "too many
requests".

## Question

What does OpenAI's `gpt-6-luna` cost and allow, what does `liulab-llms` need in order to call it
through the lab gateway, and what changed for DeepSeek Flash since the note dated 2026-11-08?
Size both for 50 studies at about 4,000 tokens in and 400 out each, under $25 per model.

## Answer

- **Read:** The model exists under exactly that name. Send `gpt-6-luna`; it is the only snapshot
  listed. OpenAI calls it "our most efficient model for focused, high-volume tasks" and lists it as
  the cheapest text model in its catalogue [O1][O2].
- **Read:** Standard price is $0.10 input, $0.01 cached input and $0.50 output per Mtok. Batch and
  Flex are half: $0.05 / $0.005 / $0.25. Fast mode (formerly Priority) is double: $0.20 / $0.02 /
  $1.00. A prompt over 272k input tokens costs 2x input and 1.5x output on the whole request
  [O1][O3].
- **Read:** Context is 1,050,000 tokens, with at most 922,000 input and 128,000 output. Structured
  outputs (JSON schema) are listed as supported. Reasoning is supported, `reasoning.effort` takes
  `none`, `low`, `medium`, `high`, `xhigh` and `max`, and the **default is `medium`**. Reasoning
  tokens are billed as output tokens and count against the output cap [O1][O4].
- **Read:** The limits table has no Free row for this model ("Not supported"). The lowest tier, Build
  (reached with $5 of total credit purchases), allows 5,000 RPM and 2,000,000 TPM, with a 20M-token
  Batch queue. OpenAI no longer uses "Tier 1"; Build is the nearest equivalent [O1][O5].
- **Read:** Overload is 503 `server_is_overloaded`; too fast a rate is 429 `rate_limit_error`, with
  the code `slow_down` for a sudden rise. Both carry `Retry-After` "when present". A 429 for a used-up
  balance or spend limit is a different case that retrying will not fix [O5][O6].
- **Measured:** `liulab-llms` already routes the `openai` provider through LiteLLM 1.106.6, and that
  release's price table has a `gpt-6-luna` entry whose standard, Batch, Flex and Fast prices match
  OpenAI's page. A call needs no code change; it needs `OPENAI_API_KEY` on the gateway, a separate
  ticket (see "What the gateway needs").
- **Inferred:** One gap is worth one small fix. The gateway treats an OpenAI 429 as "balance used up"
  only when the body says `insufficient_quota`. OpenAI now documents four other codes for the same
  situation, so those 429s would be retried until the retry budget ends [O6].
- **Inferred:** 50 studies cost about **$0.03** on `gpt-6-luna` and about **$0.08** on DeepSeek Flash
  at peak prices ($0.04 off-peak), against a $25 cap. Neither cap can bind, even if every study
  produced the model's largest possible answer (see "Sizing").
- **Read:** DeepSeek Flash prices, concurrency and peak hours are unchanged since 2026-11-08. What
  changed is only that legacy names are now called "retired", plus new web-search and file APIs.

## Summary table

| | `gpt-6-luna` (OpenAI) | `deepseek-flash` (DeepSeek) |
|---|---|---|
| Input, per Mtok (cache miss / hit) | $0.10 / $0.01 [O1] | $0.30 / $0.006 peak; $0.15 / $0.003 off-peak [D1] |
| Output, per Mtok | $0.50 [O1] | $1.20 peak; $0.60 off-peak [D1] |
| Cheaper mode | Batch and Flex, half price [O1][O3] | off-peak half price; no batch API [D1] |
| Context / largest output | 1,050,000 / 128,000 [O1] | 1M / 384K [D1] |
| JSON schema | supported [O1] | no schema; JSON mode only, which the gateway asks for [L1] |
| Reasoning | effort `none` … `max`, default `medium` [O1] | thinking on by default, effort `high` [D2] |
| Limits, new account | Build: 5,000 RPM, 2M TPM, 20M Batch queue [O1] | 2,500 concurrent requests per account [D3] |
| Overload signal | 503 `server_is_overloaded`; 429 `slow_down` and rate limit; `Retry-After` when present [O5][O6] | 429 over concurrency; 503 overloaded; no `Retry-After` mentioned [D3][D4] |
| Spend stop | 429 with `credit_balance_exhausted` and three spend or usage codes [O6] | 402 insufficient balance [D4] |

## Findings

### gpt-6-luna

- **Name.** `gpt-6-luna` is the model ID and the only snapshot. It is also on Amazon Bedrock as
  `openai.gpt-6-luna` and on Google Cloud as `gpt-6-luna` in private preview. Read [O1]. The model
  catalogue lists no `gpt-6.1-luna` or `gpt-6.2-luna`; the current catalogue's other text models are
  `gpt-6.2-astra` ($8 / $40), `gpt-6.2-sol` ($2 / $10) and `gpt-6.1-terra` ($1 / $5). Read [O2].
  The catalogue page says "View all models", so its list may not be complete.
- **Prices.** See the table. Cache writes cost $0.125 per Mtok (1.25x the uncached input rate)
  [O1]. Regional processing adds 10% where available; the pricing page does not say whether that
  applies to this model's release date [O3]. Background requests on the Responses API are
  discounted 30% (2-hour window) or 40% (8-hour window) off Standard [O3]. Search-tool calls carry a
  per-call fee, which a plain chat call does not use [O1].
- **Batch and Flex.** The pricing page lists Batch and Flex for `gpt-6-luna` at $0.05 / $0.005 /
  $0.25 [O3]. The Flex guide does not name the models it covers. It says Flex is slower, can return
  "429 Resource Unavailable" without charge, defaults to a 10-minute SDK timeout, is "being wound
  down as a beta" with no retirement date, and is unavailable on GPT-6.2 Astra and Sol [O7]. Flex is
  chosen with `service_tier: "flex"` [O7]. Whether Flex counts against the Build limits is not
  stated [O7].
- **Features.** Streaming, structured outputs, function calling, prompt caching, image input and
  the Responses API tools are supported. Fine-tuning is not [O1]. Endpoints: Chat Completions,
  Responses and Batch [O1]. Chat Completions supports function calling only when `reasoning_effort`
  is `none`; this does not affect schema output [O1].
- **Structured outputs.** Request a schema with `response_format: {type: "json_schema", …,
  strict: true}` on Chat Completions or `text.format` on Responses. The guide's examples set
  `additionalProperties: false` and list every property as required, and warn that the first request
  with a new schema is slower. A safety refusal comes back in a `refusal` field that may not follow
  the schema, and a cut-off answer has status `incomplete` [O8]. The guide's supported-schema rules
  were in a part of the page this note did not read, so the exact limits are **not established**.
- **Reasoning.** `reasoning.effort` takes `none`, `low`, `medium` (default), `high`, `xhigh`, `max`
  [O1]. The reasoning guide says GPT-6 Luna defaults to `medium` and that reasoning tokens "are
  billed as" output tokens and share the output cap. If the cap is reached the response is
  `incomplete`, possibly with no visible text and still billed, and OpenAI advises reserving at least
  25,000 tokens for reasoning plus output when starting out [O4].
- **Limits.** The model page's default table has Build, Launch and Grow rows and "Not supported"
  for Free [O1]. Build: 5,000 RPM, 2,000,000 TPM, Batch queue 20,000,000. Launch: 10,000 RPM,
  10,000,000 TPM. Grow: 30,000 RPM, 180,000,000 TPM. A tier follows total credit purchases of $5
  (Build, $500 a month), $100 (Launch, $5,000 a month) and $500 (Grow, $200,000 a month); the Free
  tier has a $100 monthly limit and needs an allowed country [O5]. The page does not say how soon
  an upgrade follows a payment or which tier a new organization starts on [O5]. Limits are reported
  in `x-ratelimit-*` headers [O5].
- **Overload and errors.** 503 `service_unavailable_error` / `server_is_overloaded` means the model
  is overloaded: follow `Retry-After` when present, else back off. 429 covers a rate limit, a
  `slow_down` (rate rose too fast, even within RPM and TPM), and used-up money:
  `credit_balance_exhausted`, `organization_spend_limit_exceeded`, `project_spend_limit_exceeded`
  and `organization_usage_limit_exceeded`. OpenAI states that "retrying billing, spend, or quota
  errors won't restore API access" [O5][O6]. 500 is a server fault to retry after a wait [O6].

### What the gateway needs

All lines below are about `liulab-llms` `main`, read from its code and tests.

- **Already there (measured).** `Model("openai", "gpt-6-luna", …)` is accepted. The gateway builds
  the LiteLLM model `openai/gpt-6-luna`, sends `response_format` as strict JSON schema, sends effort
  as `reasoning_effort`, and sets the OpenAI client to zero retries, so the gateway owns retries
  (`src/labllm/server/chat.py`). A test already covers an OpenAI model with schema output and
  `effort="low"` (`tests/test_server.py`).
- **Price entry (measured).** The gateway prices a model from its settings first, then from
  LiteLLM's pinned table (`src/labllm/server/prices.py`). The pinned release, LiteLLM 1.106.6
  (`pyproject.toml`), has a `gpt-6-luna` entry: $0.10 input, $0.50 output, $0.01 cached read,
  Batch and Flex at half, Fast at double, 922,000 input and 128,000 output tokens, schema support
  and every effort value including `none`. Its standard, Batch, Flex and Fast numbers match [O3].
  So no `[prices."gpt-6-luna"]` settings entry is needed. The same table has `deepseek/deepseek-flash`
  at $0.30 / $1.20 with off-peak hours, so DeepSeek Flash needs nothing either.
- **Request shape (inferred from reading LiteLLM's source).** LiteLLM's OpenAI path lists `gpt-6` as a
  reasoning series and maps `max_tokens` to `max_completion_tokens`, so the gateway's `max_tokens`
  works. No live call was made to confirm it.
- **Key.** The gateway reads `OPENAI_API_KEY` from its environment (`docs/gateway.md`). This is the
  separate key ticket and is not part of this note.
- **Rate-limit entry, optional (inferred).** The gateway keeps all callers under limits that an
  operator enters per provider or per model (`docs/gateway.md`, `settings.py`). Build's numbers as
  per-second values would be `[limits.openai."gpt-6-luna"]` with `requests_per_second` about 83 and
  `input_tokens_per_second` about 33,000. Without it the gateway does not pace, and OpenAI's own 429
  would pace it. For 50 studies the limit cannot be reached (see "Sizing").
- **The one gap worth fixing (inferred).** `_USED_UP["openai"]` in `chat.py` holds only
  `insufficient_quota`. A 429 whose body says one of the four codes OpenAI now documents [O6]
  would not match, so it would be raised as "provider unavailable" and retried until the retry
  budget ends, instead of stopping as "provider exhausted". The fix is one tuple of four strings
  and one test beside the existing exhausted-balance test: about 3 lines of code and 10 of test.
- **Flex and Batch are not reachable (inferred).** A form carries only provider, model, thinking,
  effort, temperature and `max_output_tokens` (`src/labllm/wire.py`), and there is no Batch route.
  Flex needs a `service_tier` setting through `wire.py`, `chat.py`, docs and tests, roughly 40 to
  80 lines, and must be added to the form only when set, or every existing cache key changes. At the
  sizes below Flex saves under two cents, so this note does not recommend it.
- **Caller settings that matter (inferred).** The gateway rejects `thinking` for any provider but
  DeepSeek, so a caller must leave it unset for OpenAI and use `effort`. Because the default effort
  is `medium` and reasoning tokens share the output cap, a caller sending `max_output_tokens` near
  the 400-token answer size with no `effort` risks an `incomplete` reply with no text, billed
  [O4]. Setting `effort="none"` or `"low"` avoids it. Strict schema output also needs every property
  required and `additionalProperties: false` [O8], which a seqforge schema with optional fields would
  have to meet.

### DeepSeek Flash: what changed since 2026-11-08

- **Read, unchanged:** peak and off-peak prices ($0.30 / $1.20 peak, $0.15 / $0.60 off-peak per Mtok,
  cache hit $0.006 / $0.003), peak hours (01:00–04:00 and 06:00–10:00 UTC, Monday to Friday,
  excluding Chinese public holidays), 1M context, 384K output, concurrency of 2,500 for Flash and
  500 for Pro, the 429 over concurrency, and the 10-minute close of an unstarted request [D1][D3].
  The error table is unchanged and still does not mention `Retry-After` [D4]. Thinking is still on
  by default at effort `high` [D2].
- **Read, changed:** the pricing page now says the legacy names `deepseek-v4-flash` and
  `deepseek-v4-flash-vision-exp` are still accepted, "the corresponding models have been retired",
  and the requests are served by DeepSeek-V4.1-Flash-1017 at the Flash price. The earlier note
  said "temporarily routed". No end date is given [D1]. The page no longer mentions
  `deepseek-chat` at all, which the earlier note saw still working on 2026-11-08 [D1].
- **Read, new:** the change log has two entries since 2026-10-17. On 2026-11-13, web search, file
  input, URL reading and an optional model-safety check became available through the existing
  APIs, with `deepseek-flash` supporting them. From 12:00 Beijing time on 2026-11-18, server-side
  search and fetch calls the model makes are no longer billed separately; their results are billed
  as ordinary tokens [D1][D5]. Neither entry changes a price, a limit or the thinking default for a
  plain chat call.
- **Inferred:** nothing changes for seqforge. The pinned name `deepseek-v4-flash` still works but
  is a retired name. `deepseek-flash` is the documented one, and the pinned LiteLLM table has an
  entry for both.

## Sizing: one study, 4,000 tokens in and 400 out, 50 studies, $25 cap per model

Arithmetic on the read prices; the token sizes are the ticket's.

| | per study | 50 studies | Studies the $25 cap allows |
|---|---:|---:|---:|
| `gpt-6-luna`, Standard | $0.00060 | $0.030 | about 41,700 |
| `gpt-6-luna`, Batch or Flex | $0.00030 | $0.015 | about 83,300 |
| `deepseek-flash`, peak | $0.00168 | $0.084 | about 14,900 |
| `deepseek-flash`, off-peak | $0.00084 | $0.042 | about 29,800 |

- **Measured:** luna per study is 4,000 × $0.10 / 1M = $0.0004 for input plus 400 × $0.50 / 1M =
  $0.0002 for output. Flash at peak is 4,000 × $0.30 / 1M = $0.0012 plus 400 × $1.20 / 1M = $0.00048.
- **Measured:** a $25 cap over 50 studies is $0.50 per study. To spend that, a luna study would need
  about 999,000 output tokens, and a Flash study about 416,000. Both are above the models' output
  caps (128,000 and 384,000), so **neither cap can bind** on these sizes, even with reasoning tokens
  billed as output.
- **Measured:** if luna spent 10,000 reasoning tokens per study at the default `medium`, the cost
  would be $0.0056 per study, $0.28 for 50 studies. This is an assumed figure, not a measurement;
  neither provider's reasoning-token count for this task is known.
- **Measured:** 50 studies are 220,000 tokens in total. The Build tier allows 2,000,000 TPM and
  5,000 RPM, and Flash allows 2,500 concurrent requests, so one minute of work at any pace stays
  far below every limit. Prompt caching would lower both bills further if the 4,000 input tokens
  share a prefix, but this note does not size that.

## Not established

- Which tier a brand-new OpenAI organization starts on, and how soon an upgrade follows a $5
  purchase. The pages give thresholds only [O5].
- Whether `gpt-6-luna` is covered by Flex. The pricing page lists a Flex price for it; the Flex
  guide names no models [O3][O7]. Whether Flex requests count against rate limits is not stated.
- The exact limits of OpenAI's strict schema mode (nesting depth, property count); the supported-schema
  section of the guide was not read [O8].
- How many reasoning tokens `gpt-6-luna` spends at `medium`, `low` or `none` on a study-reading
  prompt, and its seconds per call. No call was made.
- That the gateway's real call to `gpt-6-luna` works end to end. This note read the code and the
  pinned price table and ran nothing.
- Whether DeepSeek bills thinking tokens as output; its thinking page has no billing text [D2].
- DeepSeek's `Retry-After` behaviour, QPS limit and maximum body size, as before [D3][D4].
- OpenAI's terms on bulk use and on using outputs to train a model; the service terms were not read.

## Method

OpenAI and DeepSeek pages were fetched on 2026-11-20 through a summarising fetch tool. The model
page was also downloaded as HTML, its text extracted, and the price, context window, output cap,
reasoning default, rate-limit table, supported-feature list and snapshot list read directly; the 922,000
input cap was not found in that raw text and is taken from the summary and the LiteLLM table. The
`liulab-llms` code, tests, docs and the pinned LiteLLM price table were read in a local clone of
`main`; GitHub confirmed that its newest commit is the one the clone's changelog describes.

## Sources

| Ref | Page | Read |
|---|---|---|
| O1 | OpenAI, GPT-6 Luna model page — <https://developers.openai.com/api/docs/models/gpt-6-luna> | 2026-11-20 |
| O2 | OpenAI, Models — <https://developers.openai.com/api/docs/models> | 2026-11-20 |
| O3 | OpenAI, Pricing — <https://developers.openai.com/api/docs/pricing> | 2026-11-20 |
| O4 | OpenAI, Reasoning guide — <https://developers.openai.com/api/docs/guides/reasoning> | 2026-11-20 |
| O5 | OpenAI, Rate limits guide — <https://developers.openai.com/api/docs/guides/rate-limits> | 2026-11-20 |
| O6 | OpenAI, Error codes — <https://developers.openai.com/api/docs/guides/error-codes> | 2026-11-20 |
| O7 | OpenAI, Flex processing — <https://developers.openai.com/api/docs/guides/flex-processing> | 2026-11-20 |
| O8 | OpenAI, Structured outputs (first 100,000 of 151,840 characters) — <https://developers.openai.com/api/docs/guides/structured-outputs> | 2026-11-20 |
| D1 | DeepSeek, Models & Pricing — <https://api-docs.deepseek.com/quick_start/pricing> | 2026-11-20 |
| D2 | DeepSeek, Thinking Mode — <https://api-docs.deepseek.com/guides/thinking_mode> | 2026-11-20 |
| D3 | DeepSeek, Rate Limit & Isolation — <https://api-docs.deepseek.com/quick_start/rate_limit> | 2026-11-20 |
| D4 | DeepSeek, Error Codes — <https://api-docs.deepseek.com/quick_start/error_codes> | 2026-11-20 |
| D5 | DeepSeek, Change Log — <https://api-docs.deepseek.com/updates> | 2026-11-20 |
| L1 | `liuhlab/liulab-llms` `main`, `src/labllm/server/chat.py`, `prices.py`, `settings.py`, `wire.py`, `model.py`, `tests/test_server.py`, `docs/gateway.md`, `pyproject.toml`, and LiteLLM 1.106.6's price table | 2026-11-20 |
