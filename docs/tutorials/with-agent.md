# Compile a dataset with a coding agent

You can let a coding agent run seqforge for you. A coding agent is an AI assistant that works in
your terminal. It reads files, runs commands, and edits files when you ask. You tell it in plain
words where your sequencing files are and which genome to use. It runs the seqforge commands in
order and hands back three things:

- a **manifest**, `manifest.yaml`: what your data is (the technology, the samples, which file is
  which read);
- a **recipe**, `processing.yaml`: what to do with it (the genome, what to count);
- a **Snakefile**: the pipeline you run to get one count matrix per sample.

The agent does not decide what your data is. seqforge's code decides that from the bytes of the
files and from the archive's records. The agent's job is to call the commands, read their exit
codes, and stop when the code says stop. If you want to see each command yourself, the
[step-by-step tutorial](step-by-step.md) runs the same pipeline one command at a time.

## Set up once

1. Install seqforge. See [Getting started](../getting-started.md).
2. Make the seqforge skills visible to your agent. A skill is a page of instructions that tells an
   agent how to use a tool. The seqforge skills follow the open Agent Skills format, so the same
   files work in any agent that reads that format. A clone of the seqforge repository already
   holds them, in `.agents/skills/` and `.claude/skills/`. An agent that looks for skills in
   either folder finds them when you start it in the clone. To use them from any folder, link
   them into your home folder once:

    ```bash
    pixi run install-skills --target all
    ```

    This links each skill into every folder under your home folder where agents look for skills,
    `~/.agents/skills/` among them.

3. Optional: add the guard hooks to the folder that holds your data. A hook is a check that the
   agent runs around each of its actions.

    ```bash
    seqforge hook install -C <data-folder>
    ```

    It writes three hooks into `<data-folder>/.claude/settings.json`. The first refuses a command
    that would read a whole FASTQ file or write an absolute path into a manifest. The second checks
    a manifest again after each edit. The third stops the agent from ending its turn while
    `seqforge/questions.md` holds an open question for you. An agent that does not read hooks from
    that file never runs them. The skills work without them.

4. Only if you have papers or sample sheets for seqforge to read: set up access to the lab's model
   gateway. Reading prose is the one compile step that uses a language model, and seqforge calls
   that gateway itself. Open the tunnel and set `$LABLLM_GATEWAY` to its local address, as
   [Reach the lab gateway](https://github.com/liuhlab/liulab-llms/blob/main/docs/reach-the-gateway.md) describes. You
   need no model provider key. With no documents to read, skip this step.

## The skills the agent uses

One skill drives a compile, and it calls on the others for single steps.

| Skill | What it does |
| --- | --- |
| `seqforge-orchestrate` | Runs a whole compile: the order of the commands, and what each exit code means |
| `seqforge-exam` | Looks at a bounded head of each FASTQ file: read lengths, barcodes, file health |
| `seqforge-harvest` | Turns a paper or sample sheet into claims, each with a quote that code finds again |
| `seqforge-resolve` | Scores the bytes against the knowledge base to name the technology |
| `seqforge-manifest` | Builds and checks the manifest and the recipe |
| `seqforge-compose` | Writes the Snakefile and its config, and runs the compile checks |
| `seqforge-io` | Fetches archive records, reads the head of a remote file, and manages the barcode lists |
| `seqforge-study` | Bounds and reads a GEO study (separate from a compile; see below) |
| `seqforge-kb-author` | Adds a new technology to the knowledge base |

## Ask for a compile

Start your agent in the folder that holds your data. Name three things: where the files are, any
extra context (an accession, a paper, a sample sheet), and the genome.

> Compile the single-cell RNA-seq data in `./data` with seqforge. It's accession PRJNA1027859, and
> the paper is `./info/paper.pdf`. Align against the ce11 genome, annotation WS298.

The agent runs `seqforge run` over the files. That one command fetches the archive records, reads
the paper, writes the manifest and the recipe, and writes the Snakefile. It stops at the first
refusal. A run takes a minute or two, paper included.

The files alone are enough:

> Compile the FASTQ files in `./reads` with seqforge, against the hg38 genome with the GENCODE
> annotation.

An accession adds the archive's sample for each file and the organism. A paper can add a hint about
the kit. Plenty of data never had either, and seqforge compiles it all the same.

## Decide the genome

seqforge makes one choice only you can make: the genome. A wrong genome still aligns, still writes a
count matrix, and exits with no error, so nothing downstream catches it. seqforge has no default,
and the agent asks you if you did not say. Give two names:

- the **assembly**, a UCSC genome id (the assembly's name in the UCSC Genome Browser) such as `ce11` or `hg38` (`--assembly`);
- the **annotation**, the name of a gene annotation (GTF file) registered with the lab's genome
  package, such as `WS298` (`--annotation`).

Everything else is read from the files: the technology, which read holds the barcode, how long the
barcodes are, and the strand.

## When the agent stops and asks

seqforge refuses rather than guesses. Each command ends with an exit code, and two of them, `3`
and `4`, mean "stop". [Commands](../reference/commands.md#exit-codes) says what each code means.
This is what the agent does on each:

| Exit code | What the agent does |
| --- | --- |
| 0 | Goes on to the next step |
| 1 | Reports the error |
| 2 | Fixes the command, or asks you for what is missing |
| 3 | Stops and shows you the remedy seqforge printed |
| 4 | Stops and asks you |

For codes 3 and 4, the agent reports seqforge's own words and does not work around them. Retrying
with other options would turn a correct refusal into a wrong manifest. An open question is also
written to `seqforge/questions.md`, so you can read it after the session ends.

So if the agent comes back with a question instead of a Snakefile, read it. It names a real
decision.

## Run it without a session

Most coding agents also have a one-shot mode, which runs one request with no interactive session
and then exits. Your agent's documentation names the command. Call it in a loop to compile many datasets,
and give each call a request like this one:

```text
Compile the data in <data-folder> with seqforge. Accession PRJNA1027859, paper in
info/paper.pdf, align against ce11 / WS298. Use this directory as the workspace.
```

Each call ends with seqforge's JSON summary of the run, or with the refusal that stopped it. If you
already know every option, you need no agent. `seqforge run` takes them as flags, and
[Compile step by step](step-by-step.md) starts with it.

## What lands in `seqforge/`

Everything goes into a `seqforge/` folder in the workspace, next to your data. The top level holds
the files you open. The rest is sorted into subfolders.

```text
seqforge/
  manifest.yaml         what your data is: the technology, the samples, which file is which read
  processing.yaml       what to do with it: the genome and what to count
  sample_metadata.tsv   one row per sample
  project.yaml          an index of the project's assays
  questions.md          open questions for you, present only when there are some

  pipeline/             one folder per compiled recipe, holding the Snakefile you run
  records/              what the archive declared, and the text of each document read
  logs/                 the claims taken from the documents, and what reading them cost
  cache/                working files; safe to delete
```

The manifest is written once per dataset and does not change. To process the same data another
way, for example against another genome, you make another recipe and keep the manifest.
[From FASTQ files to a pipeline](../how-it-works/compiler.md) explains why the two are separate
files.

If the files hold more than one technology, for example 10x v2 and v3 libraries in one series, each
technology gets its own subfolder, `seqforge/<assay>/`, with its own manifest and Snakefile. The
bytes decide the split. `sample_metadata.tsv` and `project.yaml` stay at the top and cover every
assay.

## Add what each sample was

A compile records which sample each file is. It does not record what each sample was: its tissue,
its genotype, its treatment. That comes from a study reading, which the separate study chain writes
into a study folder. [Read your own studies](../guides/read-studies.md) shows how to make one, and
the agent uses the `seqforge-study` skill for it.

If you already have a study folder, name it in your request. The agent passes it as `--study`, and
seqforge joins the reading into `sample_metadata.tsv` by each sample's BioSample accession. Each
factor and each label that differs between samples gets a column. A sample the reading does not
cover, or a reading row with no compiled sample, gives a warning and nothing more. The manifest does
not change. A compile never starts the study chain.

## Run the pipeline

seqforge does not submit jobs. The last thing it writes is the Snakefile, and you run it:

```bash
cd seqforge/pipeline/<the-folder-seqforge-named>
snakemake --software-deployment-method apptainer
```

Run it on one machine. You get one `.h5ad` count matrix per sample. If you joined a reading, each
matrix carries the sample's labels. The step-by-step tutorial explains
[the run command](step-by-step.md#run-the-snakefile), including why the pipeline wants one machine.

To see the whole compile on one page, ask the agent for a report. It runs `seqforge report`, which
writes `seqforge/report.html` and the data behind it, `seqforge/report.json`.

## Read next

- [Compile step by step](step-by-step.md): the same pipeline, one command at a time.
- [Why you can trust the output](../how-it-works/trust.md): what a refusal means, and why it is
  cheaper than a wrong answer.
