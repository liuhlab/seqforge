# Compiling a dataset with Claude

The easiest way to use seqforge is to not run it yourself. You describe your data to
[Claude Code](https://claude.com/claude-code) in plain words, and Claude drives seqforge for you —
figuring out the technology, which file is which read, and how to compile it into a pipeline you can
run.

You don't need to know what chemistry your data is, which file holds the barcodes, or what any of the
flags mean. That is exactly the knowledge seqforge exists to recover from the files themselves. Your
job is to point at the data and say what you want.

This page is the short, friendly path. When you want to see every step and inspect it, the
[step-by-step tutorial](step-by-step.md) walks the same pipeline one command at a time.

## One-time setup

1. **Install seqforge** — see [Getting started](../getting-started.md). One `pixi install`.
2. **Install the Claude skills.** seqforge ships a set of skills that teach Claude how to drive it
   safely. Install them once:

    ```bash
    pixi run install-skills
    ```

3. **Set an API key for reading prose.** One stage — reading a paper or a sample sheet — uses a
   language model. Put an `ANTHROPIC_API_KEY` (or `DEEPSEEK_API_KEY`) in your environment. If you have
   no paper to read, you can skip this and stay fully offline.

That's it. From here on you just talk to Claude.

## Compile a dataset

Open Claude Code in the folder with your data and describe what you have. A good request names three
things: **where the files are**, **any extra context** (an accession, a paper, a sample sheet), and
**the genome to align against**.

> Compile the single-cell RNA-seq data in `./data` with seqforge. It's accession PRJNA1027859, and
> the paper is `./info/paper.pdf`. Align against the ce11 genome, annotation WS298.

Claude takes it from there. Behind the scenes it runs the seqforge compiler — reading the bytes,
pulling what the archive declares, mining the paper for anything useful — and hands you back two
things: a **manifest** (what your data actually is) and a **Snakefile** (the pipeline that processes
it). A minute or two, paper included.

You don't have to give it all three inputs. The files alone are enough:

> Compile the FASTQ files in `./reads` with seqforge, against the hg38 genome with the GENCODE
> annotation.

The accession and the paper each make the answer richer — per-sample strain, tissue, sex, developmental
stage — but plenty of data never had either, and that's a normal, fully supported case.

## The one thing you have to decide

There is exactly one choice seqforge will not make for you: **the genome.** A wrong genome still aligns
cleanly and still produces a count matrix — it's just quietly wrong, and nothing downstream ever
notices. So seqforge refuses to guess, and Claude will ask you for it if you didn't say. Everything
else — the technology, which read is the barcode, how long the barcodes are, which strand — is read
from the files and never guessed.

## When Claude stops and asks

seqforge is built to **refuse rather than guess.** If something is genuinely ambiguous, or the files
don't match the accession you gave, Claude will stop and tell you exactly what's wrong — it won't
paper over it with a plausible-looking answer. That pause is the tool working. A refusal is cheap; a
confidently wrong count matrix is not, because you may not find out for months.

So if Claude comes back with a question instead of a Snakefile, read it — it's naming a real decision
only you can make.

## Run it in a script (headless)

The same thing works without opening an interactive session, which is how you'd batch many datasets:

```bash
claude -p "Compile the data in $(pwd)/data with seqforge. Accession PRJNA1027859, paper in
info/paper.pdf, align against ce11 / WS298. Use this directory as the workspace."
```

Point it at each dataset in a loop and you have a headless pipeline that turns a folder of FASTQ files
into a runnable Snakefile, one dataset at a time.

## What you get: a look inside `seqforge/`

Everything Claude produces lands in a `seqforge/` folder next to your data. The top level holds the
things you actually reach for; the rest is tidied into named subfolders:

```text
seqforge/
  manifest.yaml         what your data IS — the technology, the samples, their metadata
  processing.yaml       what to DO with it — the genome and the flags
  project.yaml          a one-glance index of the whole project
  sample_metadata.tsv   every sample, one row each

  pipeline/             the Snakefile you submit, and its config
  records/              what the archive declared, and any papers you handed in
  logs/                 what the paper-reading cost, and the claims it found
  cache/                working files, safe to delete
```

Two files are the heart of it. **`manifest.yaml`** is what your data *is* — settled once, never
rewritten. **`processing.yaml`** is what to *do* with it — the genome, the recipe — and you can have
several. Want to align the same data to a different genome? That's a new `processing.yaml`, never an
edit to the manifest. (Why they're split, and why it matters, is in
[the two artifacts](../concepts/artifacts.md).)

The thing you submit lives under **`pipeline/`**: a `Snakefile`, ready to run. seqforge stops there on
purpose — it does not submit jobs, has no opinion about your scheduler, and won't grow one. The last
artifact is a file you run:

```bash
cd seqforge/pipeline/<the-directory-Claude-made>
snakemake --profile <your-cluster-profile> --software-deployment-method apptainer
```

That produces one `.h5ad` count matrix per sample. The
[step-by-step tutorial](step-by-step.md#7-submit-it) explains that command, including why the
`--software-deployment-method apptainer` flag matters.

## What to read next

- [Step-by-step tutorial](step-by-step.md) — the same pipeline, one command at a time, when you want
  to inspect each stage.
- [The two artifacts](../concepts/artifacts.md) — why the manifest and the recipe are separate files.
- [When it refuses](../concepts/refusal.md) — what the refusals mean, and why they're a feature.
