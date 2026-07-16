# Getting started

## Install

Everything goes through [pixi](https://pixi.sh) — not `pip`, `conda`, or `venv`. The environments are
locked, and a hand-installed dependency is a difference between your machine and everyone else's that
nobody will find later.

```bash
git clone https://github.com/liuhlab/seqforge
cd seqforge
pixi install
pixi run -- seqforge version
```

## The shape of a session

Every command prints JSON and reports what happened through its **exit code**. There is no `--json`
flag — JSON is simply what comes out.

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
`manifest fill` writes `seqforge/manifest.yaml` only once it validates clean; if it does not, you get
`seqforge/manifest.draft.yaml` and exactly one of the two exists.

For the whole thing on a real dataset, with real output, see the [tutorial](tutorial.md).

## Exit codes are the API

| code | meaning | what to do |
|---|---|---|
| `0` | fine | carry on |
| `1` | an error | it broke; read the message |
| `2` | you used it wrong | check the arguments |
| `3` | **blocked** | the data cannot be processed as-is. The blocker names a remedy |
| `4` | **needs a human** | a real ambiguity. Answer it and re-run |

`3` and `4` are the point of the tool, not an inconvenience. See
[When it refuses](concepts/refusal.md).

---

**Next:** [Adding a technology](kb-authoring.md).
