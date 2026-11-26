# Changelog

Versioning is **CalVer `YYYY.M.PATCH`** — year, month without zero-padding, then a patch counter that
increments per release within the month and resets when the month changes. The version is the
release's git tag (`v2026.7.1` is version `2026.7.1`), which hatch-vcs reads at build time;
`pyproject.toml` carries no version.

## Unreleased

### Added

- `seqforge pass start <kind>` runs one pass of a seqforge unit kind through the lab gateway, with
  a dollar ceiling you must give (`--ceiling-usd`), the models per role (`--model`), and which units
  to run (`--keys FILE`, `--only unsettled,refused`, or the kind's own). It prints one JSON object
  with the count of each ending and the dollars spent, and exits 0 at the end of its units, 3 when
  it stops, and 2 for a setup it cannot use. A model name the gateway rejects, such as a misspelled
  one, is such a setup: the gateway's message goes to stderr. Rerunning the same command resumes
  it, and every unit answered before comes back from the gateway's cache at no cost. No kind is
  registered yet. The new "Passes" page of the docs describes it.
- `seqforge pass status <kind>` reports where a kind's task stands: how many units have each
  ending, a person's review included, and, for each pass, its models, store build, ceiling and
  spend, with the total, median and 90th percentile of its dollars, requests and seconds per unit.
  It reads the task folder only, so it works with the gateway down.
- seqforge now depends on liulab-llms (2026.11.2 or later), from PyPI.

### Changed

- `harvest extract`, `run` and `eval run` now exit 2, not 1, for a request the gateway rejects,
  such as one for a model the gateway has no price for. `pass start` already did: the setup is
  wrong, and running the same command again meets the same refusal. The four commands build their
  gateway client, check `--ceiling-usd` and refuse a missing gateway address in one shared module.
- Python 3.13 is now the minimum supported version (`requires-python = ">=3.13"`). Every pixi
  environment already ran 3.13, so the old 3.12 floor had no test behind it.
- The package version now comes from the git tag, through hatch-vcs. The release wheel is built
  by `pixi run build`, the same task every pull request's CI runs, and is checked by
  `pixi run check-dist`.
- Tests now have time limits: 2 seconds per test and 45 seconds per test run, set once in
  `pyproject.toml`. A test marked `slow` is exempt and runs only through `pixi run test-slow`.
- CI is one job that runs on pull requests only and skips drafts, instead of seven jobs that also
  ran on every push to `main`.
- `pixi run check` runs the three test lanes one after another, as CI does, so each lane is timed
  with the machine to itself.
- liulab-genome is now pinned at `2026.11.1.dev15+gb88e2c40e`, up from
  `2026.8.1.dev2+g30e302516`. seqforge now imports from `genome.assembly.metadata` and
  `genome.assembly.chimera`, so an installed liulab-genome older than this pin fails with an
  `ImportError`.
- `harvest extract`, `run` and `eval run --llm` now call their model through the lab gateway, at
  `LABLLM_GATEWAY`, instead of calling a provider directly. A machine that reads prose needs a
  tunnel to the gateway and no provider key; `DEEPSEEK_API_KEY`, `ANTHROPIC_API_KEY` and the
  `SEQFORGE_LLM_*` variables are no longer read. A repeated harvest is answered from the gateway's
  cache at no cost. `--provider` and `--model` name the model directly, with `deepseek` and
  `deepseek-v4-pro` as the defaults; any other provider needs `--model`, and the
  `openai-compatible` provider is gone.
- `--ceiling-usd` replaces `--ceiling` on `harvest extract`, `run` and `eval run`. It is in
  dollars, defaults to one dollar per harvest (per case for `eval run`), and has no "no ceiling"
  value. `eval plan` no longer takes `--ceiling`, and its report drops `ceiling` and
  `estimated_over_ceiling`.
- Harvest's calls now stop with exit 3 and a `MODEL_CALLS_STOPPED` blocker, which replaces
  `TOKEN_CEILING_EXCEEDED`, for four reasons: the ceiling is reached, the provider's balance or
  cap is used up, the provider stays unavailable, or the gateway cannot be reached. The `reason`
  field names which. A request the gateway rejects as malformed or unauthorized exits 1.
- `seqforge/logs/usage.json` is now in dollars: its totals are `spent_usd`, `first_cost_usd`,
  `ceiling_usd`, `provider_requests`, `cache_hits`, `n_calls` and `n_documents`, and each document's
  row has `cost_usd` and `cached`. Each exchange in `transcript.jsonl` has `cost_usd` and `cached`
  too. The eval report shows dollars in place of token counts.
- The Anthropic and OpenAI client libraries are no longer dependencies.
- The manifest's `library.assay` holds, for each chemistry, its id and the first ontology id its
  spec declares, and no name. Every chemistry now gets one; Smart-seq3, BD Rhapsody, the 10x 5'
  chemistries, GEM-X and multiome ATAC used to get none. `manifest fill` opens no ontology store.
  A manifest filled now has a different dataset hash from one filled before. The report takes the
  assay's name from liulab-data's ontology store; with no store, or no liulab-data, it shows the
  ontology id.

### Removed

- Sample attributes are gone from the compiler. `harvest` no longer asks for tissue, cell type,
  strain, genotype, treatment, disease, sex, developmental stage or age, so sample and run records
  are no longer sent to a model; the metadata resolver no longer chooses among attributes or warns
  about them; the manifest's samples carry no `attributes`; `sample_metadata.tsv` has no attribute
  columns; the report's Samples tab lists each sample and its files only; and `seqforge eval` no
  longer grades `experiment.samples.*`. What a sample was now lives only in a study reading.
  Matching files to samples, the organism, cells versus nuclei and the chemistry hint are
  unchanged. `harvest extract` no longer prints `n_stored`, and each `fanned` row names its one
  `assertion_id` instead of `assertion_ids` and `materialized`.
- `seqforge io attributes` is gone, and the wheel no longer ships NCBI's list of 960 sample
  attribute names. `io records` keeps each sample attribute under the tag the submitter typed and
  no longer renames it to NCBI's name, so it makes no separate BioSample request and costs one
  request fewer for every 100 samples. A record attribute has no `harmonized` or `raw_name` field
  any more; a record set written earlier with them still loads. The organism still comes from
  each sample's taxonomy id.
- `seqforge io efo` is gone, and the wheel no longer ships seqforge's table of EFO names.
- A hand-written record set (`source: user`) may no longer carry `free_text`. The loader refuses
  it like any other unknown key, and the remedy says that what a sample was belongs in a study
  reading. No model read that prose. A record set fetched by `io records` keeps its prose.

### Fixed

- `seqforge eval run --trials N` now asks the model for a fresh answer on each trial, by sending
  trial k as replicate k. Through the lab gateway, trials 2 to N used to get trial 1's cached reply,
  so stability always read 1. Needs `liulab-llms>=2026.11.4`; the compiler's own calls are unchanged.
- Scoring reads against a shipped barcode onlist no longer converts the whole onlist to a wider
  integer type on every lookup. Each lookup against the 6.8 million barcodes of `3M-february-2018`
  took about 9 ms and now takes about 0.04 ms; the result is the same.
