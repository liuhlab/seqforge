# Getting started

seqforge does two jobs, and you can use either one alone:

- **Read a set of studies.** You give it a list of GEO Series (GEO is the Gene Expression Omnibus,
  the public archive of sequencing studies). For each study it writes which Series and papers
  belong to it, and what each sample was.
- **Compile a dataset.** You give it FASTQ files (the raw reads off a sequencer) and any notes or
  papers about them. It writes down what the data is and gives you a pipeline that turns the reads
  into count tables.

This page installs seqforge, checks that it works, and says what each job needs.

## Install it

seqforge needs Python 3.13 or newer. Pick one of the two ways to install it.

**Inside the lab, from a clone.** [pixi](https://pixi.sh) is the tool that builds the lab's
software environments. It also installs the two lab packages that seqforge reads from:
`liulab-genome` (genomes and their annotation files) and `liulab-data` (the lab's local copy of
archive records, papers and ontologies).

```bash
git clone https://github.com/liuhlab/seqforge
cd seqforge
pixi install
```

In the clone, put `pixi run` in front of each command on this site, or run `pixi shell` once to
open a shell that has `seqforge` on its path.

**Anywhere, from PyPI.** PyPI is the public index of Python packages.

```bash
pip install seqforge
```

That gives you the `seqforge` command. Reading studies and compiling against a real genome also
need the two lab packages, which are not on PyPI:

```bash
pip install "liulab-genome @ git+https://github.com/liuhlab/liulab-genome.git" \
            "liulab-data   @ git+https://github.com/liuhlab/liulab-data.git"
```

## Check that it works

These three commands need no network, no model and no data of your own.

```bash
seqforge version
seqforge kb list
seqforge kb roundtrip 10x-3p-gex-v3
```

`seqforge version` prints the version number. `seqforge kb list` prints one line for each
sequencing technology seqforge knows, such as `10x-3p-gex-v3` and `smartseq3`. `seqforge kb
roundtrip` makes fake reads for one technology, reads them back, and prints JSON that lists each
check it ran. Look for `"passed": true` near the top.

## Let Claude run it (optional)

seqforge ships skills for [Claude Code](https://claude.com/claude-code), an AI assistant that runs
in your terminal. A skill tells Claude which `seqforge` commands to run for a task. To install them
for your user, run this in the clone:

```bash
pixi run install-skills
```

Then you can ask Claude in plain words, for example "read the studies in this folder" or "compile
the FASTQ files in `./data`".

## What each job needs

| Job | What goes in | What it needs |
| --- | --- | --- |
| Read studies | A list of GEO Series | The lab's model gateway, and `liulab-data` with its record store, paper store and ontology store |
| Compile a dataset | FASTQ files, and optionally an accession and a paper | The model gateway only to read a paper or notes. `liulab-genome` for the genome. A cluster to run the pipeline |

Here is what each of those is.

- **The lab's model gateway** is the lab's server for language models. seqforge sends every model
  question through it, so you need no account with a model provider. Open a tunnel to it and set
  `$LABLLM_GATEWAY` to the tunnel's local address.
  [Reach the lab gateway](https://github.com/liuhlab/liulab-llms/blob/main/docs/reach-the-gateway.md) says how.
- **The record store** is `liulab-data`'s local copy of GEO and SRA records (SRA is the Sequence
  Read Archive, where the reads themselves are kept). `$LABDATA_STORE` points at it. The paper
  store (`$LABDATA_PAPERS`) holds the papers, and the ontology store (`$LABDATA_ONTOLOGIES`) holds
  the term lists that a reading names each tissue, cell type and condition from.
- **A cluster** is a shared computer with many processors. The compiled pipeline runs the aligner
  there. seqforge writes the pipeline but never starts it: you submit it.

## Read a set of studies

One command reads a folder of studies under a spending limit in dollars:

```bash
seqforge study run --studies <folder> --ceiling-usd 2
```

A first run costs a few cents a study. The guide
[Read your own studies](guides/read-studies.md) sets up the folder, explains the three ways a study
can end, and makes a report for each study. To see the output first, open the
[example reports](examples/index.md).

## Compile a dataset

One command takes FASTQ files to a pipeline:

```bash
seqforge run reads/*.fastq.gz --accession PRJNA1027859 --doc paper.pdf \
    --assembly ce11 --annotation WS298
```

The genome is the one choice you must make. `--assembly` names it by its UCSC id (the assembly's name in the UCSC Genome Browser), and
`--annotation` names the gene annotation file registered for it. The accession and the paper are
optional, and `--no-llm` skips the model, so nothing reaches the gateway.

seqforge writes everything under `seqforge/` in the current folder. `seqforge/manifest.yaml`
records what the data is. The pipeline is a `Snakefile` under `seqforge/pipeline/`, which you
submit to the cluster with Snakemake (a tool that runs a pipeline step by step). Add
`--study <folder>/<ID>` to copy a study's reading into the sample table.

Two tutorials walk the whole path:

- [Compile with Claude](tutorials/with-claude.md): describe your data and let Claude run the
  commands.
- [Compile step by step](tutorials/step-by-step.md): run each command yourself and look at what
  it wrote.

## Exit codes

Most commands print one JSON object, and every command reports its result through its exit code,
so a script can act on the code alone. `0` means it worked. `3` and `4` are seqforge refusing to
guess: `3` means blocked or stopped, and `4` means a person must decide.
[Commands](reference/commands.md#exit-codes) lists every code, and every command with what it
needs.
