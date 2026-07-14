# seqforge

Compile `(arbitrary FASTQ files) + (unstructured human/DB metadata)` into a validated,
machine-independent **sequencing library manifest**, then into a runnable Snakemake config — for
headless reprocessing of large collections of public sequencing datasets into a genomic-AI training
corpus.

**seqforge is a compiler, not a chatbot.** Deterministic code owns every decision. The LLM has
exactly two jobs: parse prose into span-verified assertions, and arbitrate ambiguity the
deterministic layer has *already flagged*. Everything else is a verifier.

```
probe(files)                     -> Observation    deterministic, no LLM, no network, bytes only
harvest(prose, metadata)         -> Assertion      LLM, each claim span-verified
score(Observation, KB, hypo?)    -> candidates x role_assignment, Conflicts, Questions
compile(Decision)                -> config + workflow-module selection   deterministic
```

**Status: pre-alpha, Milestone 0 in progress.** The design is [`docs/design.md`](docs/design.md);
the rationale is [`PROJECT_BRIEF.md`](PROJECT_BRIEF.md); the rules are [`CLAUDE.md`](CLAUDE.md).

## Develop

Everything runs through [pixi](https://pixi.sh) (not `pip`/`conda`/`venv`):

```bash
pixi install         # build environments
pixi run test        # pytest
pixi run lint        # ruff check .
pixi run typecheck   # mypy --strict on models/ and probe/
pixi run check       # lint + fmt-check + typecheck + test
```

## Consumer of the liulab stack

seqforge references genomes by a `liulab-genome` UCSC assembly id + registered GTF name, and aligner
environments by their literal `liulab-runtime` name (`align-rna`, ...). It never defines genome-file
machinery or aligner environments itself.
