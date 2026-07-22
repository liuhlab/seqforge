<!-- A centered, sized logo and title need inline HTML and precede the first heading, which the
     default ruleset forbids (MD033, MD041). Scope the exception to this masthead; every other rule
     stays on for the README. -->
<!-- markdownlint-disable MD033 MD041 -->
<p align="center">
  <img src="docs/assets/logo-readme.png" alt="seqforge" width="200">
</p>

<h1 align="center">seqforge</h1>
<!-- markdownlint-enable MD033 MD041 -->

Compile `(arbitrary FASTQ files) + (unstructured human/DB metadata)` into a validated,
machine-independent **dataset manifest**, then into a runnable Snakemake config — for headless
reprocessing of large collections of public sequencing datasets into a genomic-AI training corpus.

**seqforge is a compiler, not a chatbot.** Deterministic code owns every decision. The LLM has
exactly two jobs: parse prose into span-verified assertions, and arbitrate ambiguity the
deterministic layer has *already flagged*. Everything else is a verifier.

```text
probe(files)                           -> Observation   deterministic, no LLM, no network, bytes only
harvest(prose, instructions)           -> Assertion     LLM, each claim span-verified
resolve(Observations, KB, hypothesis?) -> candidates, Conflicts, Questions, Blockers
──────────────────────────────────────────────────────────────────────────────────────────────────
  => manifest.yaml     what the data IS.   One per dataset. Immutable, content-addressed.

plan(Assertions, flags, policy)        -> ProcessingSection   flag > instruction > policy
──────────────────────────────────────────────────────────────────────────────────────────────────
  => processing.yaml   what to DO with it. Many per dataset.

compose(manifest, processing)          -> config.yaml + units.tsv + module selection
```

Same dataset + a different recipe = a different pipeline, and the dataset's hash **does not move**.

The files can be local or remote: `seqforge io probe-remote <url>` fingerprints a library straight
from a URL via one bounded HTTP Range read — same identification, no download.

**Status: the pilot compiles end to end.** The deterministic spine is implemented and green
(`pixi run check`), and `seqforge run` takes the worm pilot PRJNA1027859 from its raw FASTQs and paper
to a validated manifest + a runnable Snakefile in one headless pass. The ground-truth alignment runs
(`kb e2e`) are still on synthetic yeast/worm fixtures with injected counts — it has not yet executed a
pipeline on real reads at scale.

Docs: **<https://liuhlab.github.io/seqforge/>** · design + rationale + scope delta:
[`docs/design.md`](docs/design.md) (its §9 is the running list of what is *not* yet built) · rules:
[`CLAUDE.md`](CLAUDE.md)

## Develop

Everything runs through [pixi](https://pixi.sh) (not `pip`/`conda`/`venv`):

```bash
pixi install                     # build environments
pixi run test                    # pytest
pixi run lint                    # ruff check .
pixi run typecheck               # mypy --strict on models/, probe/, resolve/, manifest/,
                                 #   compose/, workflows/, harvest/, evals/
pixi run check                   # lint + fmt-check + typecheck + test
pixi run -- pre-commit install   # once per clone — ruff, mypy, shellcheck (not the suite)
```

Most of the non-negotiable rules in `CLAUDE.md` are enforced by tests, so `pixi run check` is the
mechanism rather than a formality — and CI runs it on every push and PR. The pre-commit hooks are
deliberately limited to the fast ones, so run `check` yourself when you change behaviour.

## Consumer of the liulab stack

seqforge references genomes by a `liulab-genome` UCSC assembly id + registered GTF name, and aligner
environments by their literal `liulab-runtime` name (`align-rna`, ...). It never defines genome-file
machinery or aligner environments itself — and there is a test that says so.
