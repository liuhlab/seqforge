# Getting started

seqforge does two jobs, and you can use either one alone:

- **Read a set of studies.** You give it a list of GEO Series (GEO is the Gene Expression Omnibus,
  the public archive of sequencing studies). For each study it writes which Series and papers
  belong to it, and what each sample was.
- **Compile a dataset.** You give it FASTQ files (the raw reads off a sequencer) and any notes or
  papers about them. It writes down what the data is and gives you a pipeline that turns the reads
  into count tables.

This page installs seqforge, checks that it works, and points you to the page for each job.

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

## Pick a job

| Job | What it needs | Start here |
| --- | --- | --- |
| Read a set of studies | The lab's model gateway, and `liulab-data` with its record store, paper store and ontology store | [Read your own studies](guides/read-studies.md) |
| Compile a dataset | `liulab-genome` for the genome. The model gateway only to read a paper or notes. A cluster to run the pipeline | [Compile step by step](tutorials/step-by-step.md) |

Each job is one command. The page in the last column of the table walks you through it.

```bash
# Read a set of studies
seqforge study run --studies <folder> --ceiling-usd 2

# Compile a dataset
seqforge run reads/*.fastq.gz --assembly ce11 --annotation WS298
```

`--ceiling-usd` is the most the run may spend on models, in dollars. A first run costs a few
cents a study. For a compile, the genome is the one choice you must make: `--assembly` names it by
its UCSC id, and `--annotation` names the gene annotation file registered for it.

A coding agent, an AI assistant that runs commands in your terminal, can run either job for you.
[Compile with a coding agent](tutorials/with-agent.md) shows how to give it seqforge's skills.

Here is what each of the needs in the table is.

- **The lab's model gateway** is the lab's server for language models. seqforge sends every model
  question through it, so you need no account with a model provider. Open a tunnel to it and set
  `$LABLLM_GATEWAY` to the tunnel's local address.
  [Reach the lab gateway](https://github.com/liuhlab/liulab-llms/blob/main/docs/reach-the-gateway.md) says how.
- **The record store** is `liulab-data`'s local copy of GEO and SRA records (SRA is the Sequence
  Read Archive, where the reads themselves are kept). `$LABDATA_STORE` points at it. The paper
  store (`$LABDATA_PAPERS`) holds the papers, and the ontology store (`$LABDATA_ONTOLOGIES`) holds
  the term lists that a reading names each tissue, cell type and condition from.
- **A cluster** is a shared computer with many processors. The compiled pipeline runs the aligner
  there. seqforge writes the pipeline but never starts it. You submit it.
