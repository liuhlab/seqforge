# Getting started

!!! warning "Pre-alpha"

    seqforge runs end to end on synthetic data and has not yet processed a real public dataset. The
    commands below work; the corpus they are meant to build does not exist yet.

## Install

Everything goes through [pixi](https://pixi.sh). Do not use `pip`, `conda`, or `venv` directly — the
environments are locked, and a hand-installed dependency is a difference between your machine and
everyone else's that nobody will find later.

```bash
git clone https://github.com/liuhlab/seqforge
cd seqforge
pixi install
pixi run -- seqforge version
```

If you intend to commit, install the hooks once:

```bash
pixi run -- pre-commit install
```

## The shape of a session

Every command prints JSON to standard output and says what it did through its **exit code**. There is
no `--json` flag — JSON is simply what comes out, because the command line *is* the interface and an
agent is just another caller.

```bash
# 1. what do the bytes say?
pixi run -- seqforge probe reads/*.fastq.gz

# 2. which technology is this, and how confident are we?
pixi run -- seqforge resolve score reads/*.fastq.gz

# 3. write down what the data IS  (immutable, content-addressed)
pixi run -- seqforge manifest fill reads/*.fastq.gz --organism 6239 -o manifest.yaml
pixi run -- seqforge manifest validate manifest.yaml

# 4. write down what you want DONE with it  (one of many)
pixi run -- seqforge processing new --dataset manifest.yaml \
    --assembly ce11 --annotation WS298 -o processing.yaml

# 5. compile the two into something runnable
pixi run -- seqforge compose manifest.yaml --processing processing.yaml
```

Step 3 and step 4 are separate on purpose. See [The two artifacts](concepts/artifacts.md).

## Exit codes are the API

| code | meaning | what to do |
|---|---|---|
| `0` | fine | carry on |
| `1` | an error | it broke; read the message |
| `2` | you used it wrong | check the arguments |
| `3` | **blocked** | the data cannot be processed as-is. The blocker names a remedy |
| `4` | **needs a human** | a real ambiguity. Answer it and re-run |

`3` and `4` are the interesting ones, and they are the point of the tool rather than an inconvenience.
See [When it refuses](concepts/refusal.md).

## Checking it actually works

The interesting test is not "did it run" but "**did it get the right answer**". So the end-to-end
check simulates reads from a real yeast genome with barcodes and molecule identifiers that we chose,
runs the real aligner, and asserts the resulting count matrix equals the truth we injected:

```bash
pixi run -- seqforge kb e2e --assembly sacCer3 --annotation <name>
```

This needs a real aligner and a real genome, so it runs on a cluster rather than on a laptop.

Yeast has almost no introns, which makes it useless for testing intron-aware counting — so there is a
second fixture on a worm genome, which is intron-rich:

```bash
pixi run -- seqforge kb e2e-introns --intron-frac 0.4
```

That is the fixture that measured the 40.7% signal loss described in
[When it refuses](concepts/refusal.md).

## Where the rules live

- [`CLAUDE.md`](https://github.com/liuhlab/seqforge/blob/main/CLAUDE.md) — the rules, and for each
  one, the thing that actually enforces it. Where nothing enforces it yet, it says so.
- [`PROJECT_BRIEF.md`](https://github.com/liuhlab/seqforge/blob/main/PROJECT_BRIEF.md) — why the
  system is shaped this way. Its §14 is the running list of what is designed but not built.
- [`docs/design.md`](https://github.com/liuhlab/seqforge/blob/main/docs/design.md) — the schemas, the
  scoring function, the full CLI surface. Deliberately not published as a site page: it carries open
  arguments and values marked *unverified*, which belong in a repo rather than under a docs URL.
