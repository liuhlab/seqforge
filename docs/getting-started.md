# Getting started

## The easiest way: ask Claude

You don't have to run seqforge yourself. It ships a set of skills for
[Claude Code](https://claude.com/claude-code), so you can describe your data in plain words and let
Claude drive the compiler — figuring out the technology, which file is which, and how to turn it into
a runnable pipeline.

```bash
git clone https://github.com/liuhlab/seqforge
cd seqforge
pixi install
pixi run install-skills      # teach Claude how to drive seqforge
```

Then open Claude Code in the folder with your data and just say what you have:

> Compile the single-cell RNA-seq data in `./data` with seqforge. It's accession PRJNA1027859, and
> the paper is `./info/paper.pdf`. Align against the ce11 genome, annotation WS298.

Claude hands you back a **manifest** (what your data is) and a **Snakefile** (the pipeline that
processes it). You don't need to know the chemistry, which file holds the barcodes, or what any flag
means — that's exactly what seqforge recovers from the files themselves. The full walkthrough,
including what to do when Claude stops and asks a question, is in
[Compiling with Claude](tutorials/with-claude.md).

One thing worth knowing up front: reading a paper or sample sheet uses a language model, so set an
`ANTHROPIC_API_KEY` (or `DEEPSEEK_API_KEY`) in your environment. If you have no prose to read, you can
skip that and stay fully offline.

## Running it yourself

If you'd rather drive it directly — or you're batching thousands of datasets in a script — seqforge
is a plain command-line tool. Every command prints JSON and reports what happened through its **exit
code**. There is no `--json` flag; JSON is simply what comes out.

```bash
# 1. what do the bytes say?
pixi run -- seqforge probe reads/*.fastq.gz

# 2. which technology is this, and how confident are we?
pixi run -- seqforge resolve score reads/*.fastq.gz

# 3. (optional) what does the archive DECLARE about it?
pixi run -- seqforge io records PRJNA1027859

# 4. write down what the data IS  (immutable, content-addressed)
#    --accession fetches the records and joins them to your files: that is where
#    per-sample strain/tissue/sex/dev_stage come from. Without one you still get a
#    manifest -- samples grouped by run, no facts attached. Most data has no accession.
pixi run -- seqforge manifest fill reads/*.fastq.gz --accession PRJNA1027859
pixi run -- seqforge manifest validate seqforge/manifest.yaml

# 5. write down what you want DONE with it  (one of many)
pixi run -- seqforge processing new seqforge/manifest.yaml \
    --assembly ce11 --annotation WS298 --out seqforge/processing.yaml

# 6. compile the two into something runnable
pixi run -- seqforge compose seqforge/manifest.yaml --processing seqforge/processing.yaml
```

Steps 4 and 5 are separate on purpose — see [The two artifacts](concepts/artifacts.md).

Artifacts land under `seqforge/` in the working directory — visibly, because they are the point.
`manifest fill` writes `seqforge/manifest.yaml` only once it validates clean; if it doesn't, you get
`seqforge/manifest.draft.yaml` instead, and exactly one of the two exists.

For the whole thing on a real dataset, one command at a time with real output, see the
[step-by-step tutorial](tutorials/step-by-step.md).

!!! tip "The data doesn't have to be local"
    seqforge can fingerprint a FASTQ straight from a URL without ever downloading it —
    `seqforge io probe-remote <url>` reads a small bounded slice over HTTP and identifies the library
    from that. Everything about identifying the data works the same whether the bytes are on your disk
    or on a remote server.

## Exit codes are the API

| code | meaning | what to do |
| --- | --- | --- |
| `0` | fine | carry on |
| `1` | an error | it broke; read the message |
| `2` | you used it wrong | check the arguments |
| `3` | **blocked** | the data can't be processed as-is. The blocker names a remedy |
| `4` | **needs a human** | a real ambiguity. Answer it and re-run |

`3` and `4` are the point of the tool, not an inconvenience. See
[When it refuses](concepts/refusal.md).

---

**Next:** [Compiling with Claude](tutorials/with-claude.md), or browse the
[supported assays](kb/index.md).
