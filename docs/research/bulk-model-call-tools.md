# Existing tools for running model calls in bulk

Research date: 2026-11-08. Sources: each tool's own source code at the commit named below, its
official documentation, and its GitHub release and pull-request pages. Public material only.

Terms used here: a **unit** is one independent piece of work (for the first job, one group of
linked GEO records); a **provider** is a model API (DeepSeek, or TypeSafe's Jev); a **response
cache** stores each model response under a key computed from the request that produced it. The
harvest words **Exchange** (one request and its response), **Ceiling** (the most tokens one run may
spend) and **Transcript** (every exchange of a run) are used as in
[`src/seqforge/harvest/GLOSSARY.md`](../../src/seqforge/harvest/GLOSSARY.md).

## How claims are marked

| mark | means |
| --- | --- |
| **[SRC]** | read in the tool's source code; file and lines linked |
| **[DOC]** | stated in the tool's official documentation or README |
| **[API]** | GitHub release, pull-request or repository metadata, queried 2026-11-08 |
| **[INF]** | my inference from the above; not stated anywhere |

Source read at: LiteLLM `main` @ `2f43087` (version string 1.110.0, unreleased) and the latest
stable release v1.108.3; Inspect AI @ `3e7a4d9` (latest release 0.3.288); Bespoke Curator @
`fab184b` (latest release 0.1.31); DSPy @ `8cd665a` (latest release 3.4.1); hishel @ `f008f7a`
(release 2.0.0); distilabel @ `313fac8`. Prefect and Ray were read file by file on their default
branches, not cloned.

## Question

Does an existing tool run model calls over about 100,000 independent units the way the run engine
needs, and fit seqforge? The needs, numbered for the table:

1. **Cache:** stores every response on local disk under a key computed from its request, and replays
   it, so a rerun makes no new call.
2. **Resume:** after a kill, rerunning the same command carries on.
3. **Limits:** caps calls at once per provider, and backs off when a provider signals overload.
4. **Spend:** meters spend per call and stops at a ceiling.
5. **Unattended:** runs on one cluster node with no server or database service to keep up.
6. **Both providers:** calls DeepSeek (OpenAI-shaped chat completions, `json_object` mode) and Jev
   (`POST /v1/systemone` with `{model, state, questions}`).
7. **Maintained, with a licence that allows use in a public package.**

## Answer

- **No candidate meets all seven needs.** Each one misses the response cache as the engine needs
  it (need 1), or the run-wide spend ceiling (need 4), or both.
- **LiteLLM is the closest provider layer, and it now speaks Jev.** It ships a native TypeSafe
  provider, `litellm.systemone(model, state, questions)` [SRC], beside a DeepSeek provider, and it
  has a request-keyed cache, Retry-After-aware backoff, a price table that lists both providers, and
  a global dollar budget. But on 2026-10-30 it **removed its disk cache backend** [API], so on its
  next feature release the only built-in local cache is in memory and does not survive a kill.
  Jev calls also bypass its response cache [SRC].
- **Inspect AI is the closest whole-run engine.** It has a per-request on-disk cache, resume by
  rerunning the same command, per-model concurrency limits with rate-limit-aware retries [SRC,
  DOC]. Its spend limits apply per sample, not per run [SRC, DOC], it has no Jev provider, and it is
  an evaluation framework with a heavy dependency set.
- **DSPy has a per-request disk cache and reaches Jev** through its bundled `lm15` engine [SRC], but
  it has no per-provider concurrency cap or spend ceiling that I found, and its disk cache rests on
  `diskcache`, the package LiteLLM dropped for having had no release since August 2023 [API].
- **Curator caches per run, not per request.** Its cache folder is named by a hash of the whole
  dataset, the prompt function's source code, the model and the parameters [SRC], so adding one
  unit or editing the prompt function starts a cold cache. It has no Jev path, pins exact versions
  of LiteLLM and pandas, and sends telemetry by default [SRC, DOC].
- **Prefect and Ray are general tools.** They can resume and retry, but neither calls a model or
  meters spend. Prefect's default profile starts a background server process [SRC]. Ray Data's HTTP
  stage can POST to any URL with a requests-per-second cap and retries on 429, but it has no
  response cache [SRC, DOC].
- **What would stay in seqforge under any of them:** the token Ceiling and its refusal as a Blocker
  with exit 3, the Exchange/Transcript record, the JSON-schema prompt, and Pydantic validation of
  every response. No candidate reserves budget for requests in flight the way `TokenMeter` does.

## Summary table

Met / partly / not, against the needs above. Each cell is argued, with its source, in the
candidate's section.

| Need | LiteLLM | Inspect AI | Curator | DSPy | Prefect | Ray Data |
| --- | --- | --- | --- | --- | --- | --- |
| 1 Cache by request, on disk | partly | met, with settings | not | met | partly | not |
| 2 Resume by rerun | not | met | met, same dataset only | partly | partly | met |
| 3 Per-provider limit, backoff | partly | met | met | partly | partly | partly |
| 4 Meter spend, stop at ceiling | partly | partly | partly | not | not | not |
| 5 No server or DB service | met | met | met | met | partly | partly |
| 6 DeepSeek and Jev | met | partly | partly | met | not (BYO) | met (raw HTTP) |
| 7 Maintained, licence | met (MIT) | met (MIT) | met (Apache-2.0) | met (MIT) | met (Apache-2.0) | met (Apache-2.0) |

"BYO" means bring your own client: the tool calls no model itself.

## What seqforge has today

- [`src/seqforge/harvest/providers.py`](../../src/seqforge/harvest/providers.py) has two adapters
  (Anthropic, and any OpenAI-shaped endpoint with DeepSeek as a preset), normalizes usage onto one
  set of keys, and classifies each failure as transient or not, carrying `Retry-After`. The retry
  loop itself sits one level up.
- [`src/seqforge/harvest/meter.py`](../../src/seqforge/harvest/meter.py) wraps a provider and is the
  one counter, refuser and recorder: one Exchange per real request (retries included), a token
  Ceiling that reserves an estimate for each request before it is sent, and an in-memory Transcript.
- The throwaway prototype helper on branch `prototype/study-reading-deepseek-jev`
  (`prototypes/study_reading/harness/common.py`) adds what the engine needs and the seam lacks: a
  JSON file per call keyed by a hash of the request body, a `calls.jsonl` log, a dollar spend cap
  read back from that log, a fixed-pool thread runner, and a hand-written Jev client.

So the gap a tool would fill is: the disk cache, resume, per-provider concurrency, backoff, a
dollar meter, and a Jev client.

## LiteLLM

A Python SDK (and an optional proxy server) that puts many providers behind one OpenAI-shaped
call.

- **1 Cache: partly.** The key is a SHA-256 over the request's model and API parameters
  ([`caching.py` L410–472](https://github.com/BerriAI/litellm/blob/2f430873aead937b9112e683703956eb1ab179af/litellm/caching/caching.py#L410-L472))
  [SRC], so it is request-based. But pull request
  [#48114](https://github.com/BerriAI/litellm/pull/48114), merged 2026-10-30, removed the disk
  backend and its `diskcache` dependency; `disk_cache_dir` is now ignored with a warning
  ([L340–341](https://github.com/BerriAI/litellm/blob/2f430873aead937b9112e683703956eb1ab179af/litellm/caching/caching.py#L340-L341))
  [SRC, API]. What remains is in memory, Redis, S3, GCS, Azure Blob and semantic caches
  ([`types/caching.py` L10–18](https://github.com/BerriAI/litellm/blob/2f430873aead937b9112e683703956eb1ab179af/litellm/types/caching.py#L10-L18))
  [SRC]. The stable line v1.108.3 still ships `disk_cache.py` [API]. A custom backend can be
  passed through the underscore-prefixed `_backend` argument
  ([L235](https://github.com/BerriAI/litellm/blob/2f430873aead937b9112e683703956eb1ab179af/litellm/caching/caching.py#L235))
  [SRC], which marks it private. The response cache covers only the call types in
  `CachingSupportedCallTypes`, and `systemone` and `decisions` are not in it
  ([`types/caching.py` L26–41](https://github.com/BerriAI/litellm/blob/2f430873aead937b9112e683703956eb1ab179af/litellm/types/caching.py#L26-L41))
  [SRC], so **Jev calls are never cached**.
- **2 Resume: not.** LiteLLM keeps no run or unit state; a rerun is cheap only as far as the cache
  holds [INF].
- **3 Limits: partly.** Backoff obeys a `Retry-After` of up to 60 s and otherwise doubles, with
  jitter
  ([`utils.py` L8590–8613](https://github.com/BerriAI/litellm/blob/2f430873aead937b9112e683703956eb1ab179af/litellm/utils.py#L8590-L8613))
  [SRC]. Without the Router, the SDK wrapper retries only the `completion` call type
  ([L2297–2325](https://github.com/BerriAI/litellm/blob/2f430873aead937b9112e683703956eb1ab179af/litellm/utils.py#L2297-L2325))
  [SRC]. The Router's `max_parallel_requests` does not queue: a caller that arrives when every slot
  is busy gets a 429
  ([`client_initalization_utils.py` L13–46](https://github.com/BerriAI/litellm/blob/2f430873aead937b9112e683703956eb1ab179af/litellm/router_utils/client_initalization_utils.py#L13-L46))
  [SRC].
- **4 Spend: partly.** Each call is priced from a bundled table, which lists `deepseek/deepseek-v4-pro`
  and `typesafe/jev-1.14.0` ($0.042 per million input tokens, output free)
  ([price table L74478](https://github.com/BerriAI/litellm/blob/2f430873aead937b9112e683703956eb1ab179af/model_prices_and_context_window.json#L74478))
  [SRC]. A global `litellm.max_budget` is checked before each call against an in-memory running
  cost
  ([`utils.py` L2125–2131](https://github.com/BerriAI/litellm/blob/2f430873aead937b9112e683703956eb1ab179af/litellm/utils.py#L2125-L2131))
  [SRC]: in dollars, with nothing reserved for requests in flight, and reset when the process
  restarts [INF].
- **5 Unattended: met** for the SDK; the proxy and its database are optional extras
  ([`pyproject.toml`](https://github.com/BerriAI/litellm/blob/2f430873aead937b9112e683703956eb1ab179af/pyproject.toml#L12))
  [SRC].
- **6 Both providers: met.** DeepSeek is a listed provider. Jev is native:
  `litellm.systemone(model, state, questions, ...)`
  ([`systemone/main.py` L106](https://github.com/BerriAI/litellm/blob/2f430873aead937b9112e683703956eb1ab179af/litellm/systemone/main.py#L106))
  with `TypeSafeDecisionsConfig` defaulting to `https://api.typesafe.ai` and reading
  `TYPESAFE_API_KEY`
  ([`typesafe/decisions/transformation.py` L79–86](https://github.com/BerriAI/litellm/blob/2f430873aead937b9112e683703956eb1ab179af/litellm/llms/typesafe/decisions/transformation.py#L79-L86))
  [SRC]. Both are in v1.108.3 [API].
- **7 Maintained, licence: met.** MIT outside the `enterprise/` folder
  ([`LICENSE`](https://github.com/BerriAI/litellm/blob/2f430873aead937b9112e683703956eb1ab179af/LICENSE))
  [SRC]; several releases a week [API]. The core install pulls in `boto3`, `tokenizers`,
  `huggingface-hub`, `aiohttp`, `tiktoken`, `openai` and `jinja2`, among others [SRC].
- **Would replace:** both adapters in `providers.py`, the prototype's hand-written Jev client, and
  probably the retry classification and its backoff [INF]. **Would stay:** the meter and its token
  Ceiling, the disk cache (written by seqforge, or plugged in through `_backend`), resume, and the
  concurrency pool.
- **Dealbreaker to weigh:** the disk cache was just removed, and Jev calls bypass the cache anyway.

## Inspect AI (UK AI Security Institute)

A framework for running evaluations: a dataset of samples, a solver that calls models, and a
scorer.

- **1 Cache: met, with settings.** One pickle file per request on disk, keyed by an MD5 over the
  generate settings, the messages, the base URL, the tools and the cache policy
  ([`_cache.py` L320–352](https://github.com/UKGovernmentBEIS/inspect_ai/blob/3e7a4d991e355e0c1319ad7a8ca3974a8a58d750/src/inspect_ai/model/_cache.py#L320-L352),
  [L372–401](https://github.com/UKGovernmentBEIS/inspect_ai/blob/3e7a4d991e355e0c1319ad7a8ca3974a8a58d750/src/inspect_ai/model/_cache.py#L372-L401))
  [SRC]. Two defaults must be changed: entries expire after one week
  ([L104](https://github.com/UKGovernmentBEIS/inspect_ai/blob/3e7a4d991e355e0c1319ad7a8ca3974a8a58d750/src/inspect_ai/model/_cache.py#L104)),
  and `per_epoch=True` adds the epoch to the key [SRC]. The folder is set by `INSPECT_CACHE_DIR`
  ([L478–484](https://github.com/UKGovernmentBEIS/inspect_ai/blob/3e7a4d991e355e0c1319ad7a8ca3974a8a58d750/src/inspect_ai/model/_cache.py#L478-L484))
  [SRC]. The files are pickled Inspect `ModelOutput` objects, readable only through Inspect [INF].
- **2 Resume: met.** "Eval sets that don't complete due to errors or cancellation can be
  re-run---simply re-execute the same command and any work not yet completed will be scheduled"
  ([`docs/eval-sets.qmd` L53](https://github.com/UKGovernmentBEIS/inspect_ai/blob/3e7a4d991e355e0c1319ad7a8ca3974a8a58d750/docs/eval-sets.qmd#L53))
  [DOC]. Completed samples are reused when each has a stable ID [DOC].
- **3 Limits: met.** `max_connections` per model and account (default 10), with adaptive
  concurrency
  ([`_model.py` L580–599](https://github.com/UKGovernmentBEIS/inspect_ai/blob/3e7a4d991e355e0c1319ad7a8ca3974a8a58d750/src/inspect_ai/model/_model.py#L580-L599));
  each provider classifies a failure as a rate limit or a transient error and passes on any
  `retry_after`
  ([L601–613](https://github.com/UKGovernmentBEIS/inspect_ai/blob/3e7a4d991e355e0c1319ad7a8ca3974a8a58d750/src/inspect_ai/model/_model.py#L601-L613));
  waits grow exponentially, with jitter, up to 30 min
  ([`_retry.py` L135](https://github.com/UKGovernmentBEIS/inspect_ai/blob/3e7a4d991e355e0c1319ad7a8ca3974a8a58d750/src/inspect_ai/model/_retry.py#L135))
  [SRC].
- **4 Spend: partly.** `cost_limit` is "Limit on total cost (in dollars) for each sample"
  ([`eval.py` L300](https://github.com/UKGovernmentBEIS/inspect_ai/blob/3e7a4d991e355e0c1319ad7a8ca3974a8a58d750/src/inspect_ai/_eval/eval.py#L300))
  [SRC], and it needs price data supplied by the user
  ([`docs/setting-limits.qmd` L217](https://github.com/UKGovernmentBEIS/inspect_ai/blob/3e7a4d991e355e0c1319ad7a8ca3974a8a58d750/docs/setting-limits.qmd#L217))
  [DOC]. Scoped limits wrap a block of code inside a sample [DOC]. I found no ceiling across a whole
  run.
- **5 Unattended: met.** One process, with logs written to files [DOC].
- **6 Both providers: partly.** DeepSeek is a built-in provider
  ([`providers.py` L218](https://github.com/UKGovernmentBEIS/inspect_ai/blob/3e7a4d991e355e0c1319ad7a8ca3974a8a58d750/src/inspect_ai/model/_providers/providers.py#L218))
  [SRC]. Jev would need a custom `ModelAPI` registered with `@modelapi`
  ([`_registry.py` L30](https://github.com/UKGovernmentBEIS/inspect_ai/blob/3e7a4d991e355e0c1319ad7a8ca3974a8a58d750/src/inspect_ai/model/_registry.py#L30))
  [SRC], returning typed answers with probabilities through a chat-shaped `ModelOutput` [INF].
- **7 Maintained, licence: met.** MIT; several releases a month [API]. The required dependencies
  include `fastapi`, `uvicorn`, `textual`, `boto3`, `aiobotocore`, `s3fs`, `numpy` and `debugpy`
  ([`pyproject.toml` L164](https://github.com/UKGovernmentBEIS/inspect_ai/blob/3e7a4d991e355e0c1319ad7a8ca3974a8a58d750/pyproject.toml#L164))
  [SRC].
- **Would replace:** the DeepSeek adapter, the retry loop, the cache, resume and the concurrency
  pool. **Would stay:** the token Ceiling (it is per run), a Jev adapter written as an Inspect
  model, the validation gate, and seqforge's own JSON output. Each unit becomes an Inspect sample
  with a solver and a scorer [INF].
- **Dealbreaker to weigh:** the shape. The run engine becomes an Inspect evaluation, and its
  results live in Inspect's log format [INF].

## Bespoke Curator

A library for generating synthetic data with models over a Hugging Face dataset.

- **1 Cache: not, as asked.** One cache folder per run, named by an xxHash of the dataset
  fingerprint, the hash of the prompt function's source, the model, the response schema and the
  generation parameters
  ([`llm.py` L144–169](https://github.com/bespokelabsai/curator/blob/fab184b26578429568c39c6b1e6b1d67e1650ae1/src/bespokelabs/curator/llm/llm.py#L144-L169),
  [L211–243](https://github.com/bespokelabsai/curator/blob/fab184b26578429568c39c6b1e6b1d67e1650ae1/src/bespokelabs/curator/llm/llm.py#L211-L243))
  [SRC]. Adding a unit to the dataset or editing the prompt function gives a new folder, so a
  response is not found again by its own request [INF].
- **2 Resume: met within one folder.** Rows already in the response files are skipped
  ([`base_online_request_processor.py` L341–362](https://github.com/bespokelabsai/curator/blob/fab184b26578429568c39c6b1e6b1d67e1650ae1/src/bespokelabs/curator/request_processor/online/base_online_request_processor.py#L341-L362))
  [SRC].
- **3 Limits: met** for its own backends: requests and tokens per minute, requests at once, a pause
  on a rate limit, a retry count
  ([`config.py` L92–111](https://github.com/bespokelabsai/curator/blob/fab184b26578429568c39c6b1e6b1d67e1650ae1/src/bespokelabs/curator/request_processor/config.py#L92-L111))
  [SRC].
- **4 Spend: partly.** Cost is computed through LiteLLM's price table
  ([`cost.py`](https://github.com/bespokelabsai/curator/blob/fab184b26578429568c39c6b1e6b1d67e1650ae1/src/bespokelabs/curator/cost.py))
  [SRC]; a search of the source for a budget or cost limit found none.
- **5 Unattended: met.** It writes a SQLite file, `metadata.db`, next to the cache: a file, not a
  service [SRC]. Telemetry to PostHog is on unless `TELEMETRY_ENABLED=False`
  ([`telemetry/client.py` L86](https://github.com/bespokelabsai/curator/blob/fab184b26578429568c39c6b1e6b1d67e1650ae1/src/bespokelabs/curator/telemetry/client.py#L86))
  [SRC, DOC].
- **6 Both providers: partly.** DeepSeek through the LiteLLM or OpenAI backend. The backends are a
  fixed list
  ([`_factory.py`](https://github.com/bespokelabsai/curator/blob/fab184b26578429568c39c6b1e6b1d67e1650ae1/src/bespokelabs/curator/request_processor/_factory.py))
  and the LiteLLM one calls `litellm.acompletion`, a chat call [SRC]; there is no Jev path.
- **7 Maintained, licence: met.** Apache-2.0; releases in 2026-03, 2026-10 (two) [API]. It pins
  exact versions of `litellm` (1.83.7), `pandas` (2.2.2) and `vertexai` (1.71.1), and requires
  `datasets`, `matplotlib`, `instructor` and `posthog`
  ([`pyproject.toml` L24–41](https://github.com/bespokelabsai/curator/blob/fab184b26578429568c39c6b1e6b1d67e1650ae1/pyproject.toml#L24-L41))
  [SRC].
- **Would replace:** the concurrency pool and the retry loop. **Would stay:** everything else.
- **Dealbreakers:** the run-level cache key, and the exact pins, which would fix seqforge's own
  LiteLLM and pandas versions.

## DSPy

A framework for building and optimizing model programs from typed signatures.

- **1 Cache: met.** A memory cache in front of an on-disk `diskcache.FanoutCache`
  ([`cache.py` L94](https://github.com/stanfordnlp/dspy/blob/8cd665adee66f28d26f027b3ab86944340a71e8b/dspy/clients/cache.py#L94)),
  keyed by SHA-256 over the request with sorted keys
  ([L104–113](https://github.com/stanfordnlp/dspy/blob/8cd665adee66f28d26f027b3ab86944340a71e8b/dspy/clients/cache.py#L104-L113)),
  leaving out `api_key`, `api_base` and `base_url`
  ([L238](https://github.com/stanfordnlp/dspy/blob/8cd665adee66f28d26f027b3ab86944340a71e8b/dspy/clients/cache.py#L238))
  [SRC]. The default size limit is 30 GB
  ([`clients/__init__.py` L16](https://github.com/stanfordnlp/dspy/blob/8cd665adee66f28d26f027b3ab86944340a71e8b/dspy/clients/__init__.py#L16))
  [SRC]. LiteLLM removed the same `diskcache` package because it "has not had a release since
  August 2023" ([#48114](https://github.com/BerriAI/litellm/pull/48114)) [API].
- **2 Resume: partly.** A rerun replays from the cache; there is no run or unit state [INF].
- **3 Limits: partly.** Retries wait for the provider's `retry_after` if given, otherwise
  `min(2**attempt, 60)` seconds
  ([`execution.py` L268–270](https://github.com/stanfordnlp/dspy/blob/8cd665adee66f28d26f027b3ab86944340a71e8b/dspy/clients/execution.py#L268-L270))
  [SRC]. Concurrency is a thread count per `batch()` call
  ([`module.py` L270–288](https://github.com/stanfordnlp/dspy/blob/8cd665adee66f28d26f027b3ab86944340a71e8b/dspy/primitives/module.py#L270-L288)),
  not a cap per provider [SRC].
- **4 Spend: not.** I found no ceiling.
- **5 Unattended: met.**
- **6 Both providers: met.** The bundled `lm15` engine has a TypeSafe provider that sends one
  `POST /v1/systemone` per request, turning the messages into Jev's `state` and the judgment fields
  of the output schema into its `questions`
  ([`_vendor/lm15/providers/typesafe.py` L1–16](https://github.com/stanfordnlp/dspy/blob/8cd665adee66f28d26f027b3ab86944340a71e8b/dspy/_vendor/lm15/providers/typesafe.py#L1-L16))
  [SRC]. It is in release 3.4.1 [API]. DeepSeek goes through LiteLLM or the OpenAI-chat provider
  [SRC].
- **7 Maintained, licence: met.** MIT; releases every few weeks [API]. Release 3.4.1 requires
  `litellm`, `openai`, `diskcache` and `gepa` [API]; on `main`, LiteLLM has become optional [SRC].
- **Would replace:** the adapters, the cache and the retry. **Would stay:** the Ceiling and meter,
  resume, and a per-provider concurrency cap. The prompts would become DSPy signatures, and DSPy
  writes the prompt text from them [INF], which sits awkwardly with seqforge's own JSON-schema
  prompt.

## Prefect (general workflow tool)

A Python workflow engine: flows of tasks, with retries, caching and an API server that tracks
every run.

- **1 Cache: partly.** Task results can be persisted to `~/.prefect/storage/`, with file names equal
  to the cache keys; the default key hashes "the inputs provided to the task", "the code definition
  of the task" and the flow run ID, and `INPUTS` keys on inputs alone
  ([caching docs](https://www.prefect.io/docs/v3/concepts/caching)) [DOC]. With the default, every
  new flow run has a new ID, so a rerun misses [INF]. Result persistence is off by default [DOC].
- **2 Resume: partly,** through that task cache [INF].
- **3 Limits: partly.** Retries with delays and backoff are per task [DOC]. Global concurrency and
  `rate_limit` are kept on the server; without a configured limit, "a warning is logged but execution
  continues"
  ([global concurrency docs](https://www.prefect.io/docs/v3/how-to-guides/workflows/global-concurrency-limits))
  [DOC].
- **4 Spend: not.** Prefect calls no model.
- **5 Unattended: partly.** With no `PREFECT_API_URL`, the client raises unless ephemeral mode is on,
  in which case it starts a server subprocess
  ([`client/orchestration/__init__.py`](https://github.com/PrefectHQ/prefect/blob/main/src/prefect/client/orchestration/__init__.py))
  [SRC]; the default profile turns ephemeral mode on
  ([`settings/profiles.toml`](https://github.com/PrefectHQ/prefect/blob/main/src/prefect/settings/profiles.toml))
  [SRC]. The server's default database is SQLite
  ([server docs](https://www.prefect.io/docs/v3/concepts/server)) [DOC].
- **6 Both providers: not;** bring your own client.
- **7 Maintained, licence: met.** Apache-2.0; release 3.9.2 on 2026-11-06, with nightly builds
  [API].
- **Would replace:** the run loop and retry only. **Dealbreaker to weigh:** a server process and a
  database for every run, to gain nothing on the model side.

## Ray Data (general workflow tool)

A distributed data-processing library, with an LLM module (`ray.data.llm`).

- **1 Cache: not.** The HTTP stage has no response cache [SRC]. Ray Data checkpoints by finished
  block, not by request [DOC].
- **2 Resume: met** for pipelines that start with a read and end with a write: set a
  `CheckpointConfig` with an ID column and a path, and "Ray Data discovers the checkpoint and resumes
  from the last successful block" when the same code runs again
  ([working with LLMs](https://docs.ray.io/en/latest/data/working-with-llms.html)) [DOC].
- **3 Limits: partly.** `HttpRequestProcessorConfig` takes `qps`, `concurrency`, `max_retries`
  (default 0) and `base_retry_wait_time_in_s`
  ([API reference](https://docs.ray.io/en/latest/data/api/doc/ray.data.llm.HttpRequestProcessorConfig.html))
  [DOC]. It retries 429, 408, 502, 503 and 504 with doubling waits and does not read `Retry-After`
  ([`http_request_stage.py`](https://github.com/ray-project/ray/blob/master/python/ray/llm/_internal/batch/stages/http_request_stage.py))
  [SRC].
- **4 Spend: not.** Nothing is metered.
- **5 Unattended: partly.** No service to keep up, but `ray.init()` starts a local Ray cluster of
  background processes inside the job [INF from general Ray knowledge; not re-read here].
- **6 Both providers: met as raw HTTP.** The stage POSTs a payload to any `url` with any `headers`
  [DOC, SRC]; it knows neither provider's response shape or usage.
- **7 Maintained, licence: met.** Apache-2.0; release 2.60.0 on 2026-11-04 [API].
- **Would replace:** the concurrency pool and resume. **Would stay:** everything else. **Dealbreaker
  to weigh:** the footprint of Ray for one node and one process.

## Others considered

- **hishel** (an HTTP cache for `httpx`), as a ready-made cache keyed by request: release 2.0.0
  (2026-11-05) never serves a `POST` from the cache, and its migration notes tell users who cache
  `POST` bodies to stay on 1.x
  ([`docs/migration.md`](https://github.com/karpetrosyan/hishel/blob/f008f7a56353620efc4a5ff2cc6e02c65f1f0088/docs/migration.md))
  [DOC]. Both providers are called with `POST`, so it does not fit.
- **distilabel** (Argilla's synthetic-data pipelines): last release 1.5.3 on 2025-01-28 [API].
  Dropped for maintenance.

## Reading of the evidence (not a recommendation)

Each tool covers some of the needs, and none covers the two this engine leans on most: a
per-request disk cache that also holds Jev calls, and a run-wide ceiling that reserves for requests
in flight. The prototype helper already does the first in a few dozen lines, and `TokenMeter` already
does the second. The strongest single thing on offer is LiteLLM's maintained Jev and DeepSeek
clients, with their price table and Retry-After handling, used as a library under seqforge's own
cache, meter and pool. The cost is LiteLLM's dependency weight, and a cache that seqforge writes
itself. The build-or-adopt decision is a separate ticket.

## Not established

- When LiteLLM's next feature release, the first without the disk cache, ships, and how long the
  1.108 line keeps the disk cache with fixes.
- Whether LiteLLM's Router applies its retry and backoff to `adecisions` and `systemone` calls; the
  Router exposes `adecisions`, but I did not trace its retry path.
- Whether LiteLLM's systemone path returns Jev's per-answer probabilities unchanged; I read the
  request side only.
- Inspect AI: a ceiling over a whole run, if one exists outside the documented per-sample and scoped
  limits.
- DSPy: whether `lm15`'s TypeSafe provider can send a hand-built `questions` object of 60 questions
  as seqforge does, rather than one built from a signature.
- Prefect: the default SQLite file path, and whether its task cache can be written without the
  ephemeral server running.
- Ray: the processes and memory `ray.init()` starts on one node (not measured).
- Whether DeepSeek offers a batch API that would replace live calls for this job (not checked).
- Candidates not examined: DocETL, DataDreamer, Daft, Simon Willison's `llm`, and the OpenAI
  cookbook's parallel-request script.
