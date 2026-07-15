# Getting started

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

---

**Next:** teach it a technology it does not know yet — [Adding a technology](kb-authoring.md).
