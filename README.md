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

It produces two artifacts, and only the second is plural:

- **`manifest.yaml`** — what the data **is**. One per dataset, immutable, content-addressed.
- **`processing.yaml`** — what to **do** with it: genome, aligner, introns or not. Many per dataset.

## Install

```bash
pip install seqforge
```

That gives you the compiler and the `seqforge` CLI. The two lab-only stages — `compose` against a real
genome and `kb e2e` — additionally need the lab's `liulab-genome` and `liulab-data`, which are not on
PyPI:

```bash
pip install "liulab-genome @ git+https://github.com/liuhlab/liulab-genome.git" \
            "liulab-data   @ git+https://github.com/liuhlab/liulab-data.git"
```

Inside the lab, `pixi install` already pulls both.

## Develop

Everything runs through [pixi](https://pixi.sh) (not `pip`/`conda`/`venv`):

```bash
pixi install                     # build environments
pixi run check                   # lint + fmt-check + typecheck + test
pixi run test                    # pytest only
pixi run -- pre-commit install   # once per clone — the fast hooks, not the suite
```

Most of the non-negotiable rules are enforced by tests, so `pixi run check` is the mechanism rather
than a formality — CI runs it on every push and PR.

## Where to look

| For | Read |
| --- | --- |
| Using it — the tour, the tutorials, the concepts | **<https://liuhlab.github.io/seqforge/>** |
| Working on it — the rules, and where to read next | [`AGENTS.md`](AGENTS.md) (`CLAUDE.md` symlinks to it) |
| One decision per file, one paragraph | [`docs/adr/`](docs/adr/), and beside the code each governs |
| What a term means | [`CONTEXT-MAP.md`](CONTEXT-MAP.md), and one `CONTEXT.md` per context |
| What changed, and when | [`CHANGELOG.md`](CHANGELOG.md) |
| What is *not* yet built | the [open issues](https://github.com/liuhlab/seqforge/issues) |
