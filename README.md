# seqforge

Compile `(arbitrary FASTQ files) + (unstructured human/DB metadata)` into a validated,
machine-independent **dataset manifest**, then into a runnable Snakemake config — for headless
reprocessing of large collections of public sequencing datasets into a genomic-AI training corpus.

**seqforge is a compiler, not a chatbot.** Deterministic code owns every decision. The LLM has
exactly two jobs: parse prose into span-verified assertions, and arbitrate ambiguity the
deterministic layer has *already flagged*. Everything else is a verifier.

```
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

**Status: Milestone 0 landed; the held-out acceptance case has not been run.** The deterministic
spine is implemented and green, end to end on synthetic yeast and worm fixtures with ground-truth
counts injected. It has not yet processed a real dataset.

Docs: **<https://liuhlab.github.io/seqforge/>** · design: [`docs/design.md`](docs/design.md) ·
rationale: [`PROJECT_BRIEF.md`](PROJECT_BRIEF.md) (its §14 is the running list of what is *not* yet
built) · rules: [`CLAUDE.md`](CLAUDE.md)

## Develop

Everything runs through [pixi](https://pixi.sh) (not `pip`/`conda`/`venv`):

```bash
pixi install                     # build environments
pixi run test                    # pytest
pixi run lint                    # ruff check .
pixi run typecheck               # mypy --strict on models/, probe/, resolve/, manifest/,
                                 #   compose/, workflows/, harvest/, evals/
pixi run check                   # lint + fmt-check + typecheck + test
pixi run -- pre-commit install   # once per clone — runs `check` before every commit
```

Most of the non-negotiable rules in `CLAUDE.md` are enforced by tests, so `pixi run test` is the
mechanism rather than a formality. CI runs the same thing as a backstop.

## Consumer of the liulab stack

seqforge references genomes by a `liulab-genome` UCSC assembly id + registered GTF name, and aligner
environments by their literal `liulab-runtime` name (`align-rna`, ...). It never defines genome-file
machinery or aligner environments itself — and there is a test that says so.
