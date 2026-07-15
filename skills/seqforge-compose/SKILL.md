---
name: seqforge-compose
description: >-
  Compile a validated manifest into a Snakemake config + units.tsv + a workflow
  module selection with `seqforge compose`. Use when asked to emit a pipeline
  config, produce runnable params, or check the compose gates. Never writes rule
  source — the composer emits data, never code.
---

# seqforge compose

```bash
seqforge compose MANIFEST --json --outdir results --threads 8
```

A **pure function** of the manifest: same manifest in, same config out.

## R1: emit data, never code

The composer emits `config.yaml` + `units.tsv` + a **selection** from `workflows/`. It never
generates Snakefile or rule source, and neither do you. The workflow modules are hand-written,
versioned and CI-tested. If a pipeline needs a capability that no module has, the answer is to write
a module and test it — never to synthesise rules on the fly.

## The three gates, and why `skip` exists

| gate | what it proves | when |
|---|---|---|
| `params` | the KB's chemistry params survived compose intact, and agree with the observed layout | always |
| `wiring` | `snakemake -n` / `--lint` | needs snakemake, else `skip` |
| `e2e` | a real count matrix vs injected truth | needs STAR + a genome, else `skip` |

**`skip` is not `pass`.** A gate reporting `pass` because it never ran would let green CI be mistaken
for coverage — that distinction is load-bearing, so never report a skipped gate as passing.

## What the params gate can and cannot catch

It proves the value **survived compose**. It cannot prove the value is **right**. An inverted
`--soloStrand` or a wrong `--soloFeatures` produces a matrix that merely looks like a thin dataset —
STARsolo exits 0, a dry run sees nothing, a linter sees nothing. Only `kb e2e` (count matrix vs
injected truth) catches that class, which is exactly why it exists.

Measured: `kb e2e-introns` on ce11 shows `--soloFeatures Gene` discards **40.7%** of a nuclear
library, silently. If a compose result looks fine but the biology is single-nucleus, that number is
the reason to look harder.

## Exit codes

`0` composed; `3` a gate failed or the manifest was invalid (compose refuses to compile an invalid
manifest — that refusal is the feature).
