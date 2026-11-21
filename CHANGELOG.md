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
  registered yet. The new "Passes and reviews" page of the docs describes it.
- `seqforge review file <kind>` files a small sample of a kind's review queue, the units that
  ended unsettled or refused, as issues in the lab's private review repo: five by default, more
  with `--count`, in the kind's own review order. Each issue shows the unit's records, each pass's
  answer and the checks it failed, and a person answers by ticking boxes and closing it. A unit is
  never filed twice for the same inputs. The command reaches GitHub through the GitHub CLI, `gh`
  2.48 or later, as the person running it. The "Passes and reviews" page of the docs describes it,
  and how the review repo was set up.
- `seqforge review read <kind>` reads people's answers back from a kind's closed issues in the
  review repo. An issue closed as completed stores the answer that its ticks make, and one closed
  as not planned stores a drop with the reasons ticked, each with the GitHub login of the person
  who closed it. The reviews go into the kind's file in the review clone, which the command commits
  alone and pushes. Ticks that make no answer, or an answer that fails one of the kind's checks,
  store nothing: the issue gets a comment that says why and is reopened, and the command exits 4.
  It works while a pass is running.
- `seqforge pass status <kind>` reports where a kind's task stands: how many units have each
  ending, a person's review included, and, for each pass, its models, store build, ceiling and
  spend, with the total, median and 90th percentile of its dollars, requests and seconds per unit.
  It reads the task folder only, so it works with the gateway down.
- seqforge now depends on liulab-llms (2026.11.2 or later), from PyPI.

### Changed

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

### Fixed

- Scoring reads against a shipped barcode onlist no longer converts the whole onlist to a wider
  integer type on every lookup. Each lookup against the 6.8 million barcodes of `3M-february-2018`
  took about 9 ms and now takes about 0.04 ms; the result is the same.
