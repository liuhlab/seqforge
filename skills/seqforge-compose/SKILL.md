---
name: seqforge-compose
description: >-
  Compile a (dataset manifest, processing manifest) pair into a Snakemake config
  + units.tsv + a workflow module selection with `seqforge compose`. Use when
  asked to emit a pipeline config, produce runnable params, or check the compose
  gates. Never writes rule source — the composer emits data, never code.
---

# seqforge compose

```bash
seqforge compose MANIFEST --processing PROCESSING --outdir results
seqforge compose MANIFEST --assembly ce11 --annotation WS298   # policy defaults, no recipe file
```

A **pure function of both inputs**: same pair in, same config out. Two inputs, still no I/O — a
processing manifest is data, not a side channel.

`--processing` is optional. A processing manifest exists because someone wanted something
non-default; requiring one per dataset would mean 10⁴ boilerplate files nobody reads. Either way
compose writes the fully-resolved, dataset-**bound** manifest it used to
`seqforge/pipeline/<run_id>/processing.lock.yaml` — disk is *state*, not *input*, so the run's
decisions are recoverable regardless.

Output is keyed by `run_id = H(dataset ⊕ processing ⊕ kb ⊕ workflow)`, **not** by the workspace: one
dataset compiled two ways is two runs, and a fixed path would silently overwrite the first.

`threads` is not a flag — it lives in `processing.resources.threads`. It used to be both, which is
the two-sources-of-truth disease this design cures. `outdir` stays a flag because it is a path, i.e.
a machine fact, and a manifest may not carry one.

## Emit data, never code

The composer emits `config.yaml` + `units.tsv` + a **selection** from `workflows/`. It never
generates Snakefile or rule source, and neither do you. The workflow modules are hand-written,
versioned and CI-tested. If a pipeline needs a capability that no module has, the answer is to write
a module and test it — never to synthesise rules on the fly.

## The three gates, and why `skip` exists

| gate | what it proves | when |
|---|---|---|
| `params` | every emitted param is **owned**, arrives verbatim from its owner, and agrees with the observed layout | always |
| `wiring` | `snakemake -n -p` over a throwaway replica | needs snakemake, else `skip` |
| `e2e` | a real count matrix vs injected truth | needs STAR + a genome, else `skip` |

**`skip` is not `pass`.** A gate reporting `pass` because it never ran would let green CI be mistaken
for coverage — that distinction is load-bearing, so never report a skipped gate as passing.

**Each verdict is `{"status": …, "reason": […]}`, and the reason is the part worth relaying.** A
failing `wiring` gate carries what snakemake actually said — including the module's own message,
written to be read by whoever hit it — and a `skip` says what it was waiting for. Reporting
`gate.wiring: fail` and stopping there is how a refusal becomes indistinguishable from a silent pass.

## One key, one owner

Every aligner param in the emitted config has exactly one source, and the gate proves it:

- the **KB** owns how to **parse** reads — `soloType`, CB/UMI offsets, whitelist, strand. Byte-decided.
- the **processing manifest** owns what to **count** — `soloFeatures`, `quantMode`. Instructable.

The two key sets are disjoint, which is *why* a user instruction cannot contradict the bytes: not
because we rank them, but because the user has no vocabulary in which to say it. If you are tempted
to move a key across that line, that is a design change — start with the parse/count line, not with the spec file.

`primary_feature` is emitted at config **top level**, not inside `config["solo"]`, which must stay
"every key is a STAR CLI flag" for the gate's coverage check. STARsolo has no `--primaryFeature`; it
writes one `Solo.out/<Feature>/` per value and does not care about order, so "which matrix is THE
matrix" is a seqforge-side annotation projected out to an explicit value.

## What the params gate can and cannot catch

It proves the value **survived compose**. It cannot prove the value is **right**. An inverted
`--soloStrand` produces a matrix that merely looks like a thin dataset — STARsolo exits 0, a dry run
sees nothing, a linter sees nothing. Only `kb e2e` (count matrix vs injected truth) catches that
class, which is exactly why it exists.

Measured: `kb e2e-introns` on ce11 showed `--soloFeatures Gene` discards **40.7%** of a nuclear
library, silently. That defect is fixed — `soloFeatures` defaults to all five, so GeneFull is counted
whether or not anyone tells us the prep is nuclear — and the number now survives as a
counterfactual the fixture re-measures on every run. Do not "optimise" the default back down to Gene;
`--quantify` narrowing warns for this reason.

## The sample list is the POST-DROP list

A chemistry may declare `min_input_reads`, a read depth a sample must reach to be analysed at all.
Compose applies it under the **live** knowledge base — summing the per-file counts in the manifest's
provenance, minimum within a run and sum across a sample's runs — and a sample below it is dropped
from `config["samples"]` and from `units.tsv`, while **staying in the manifest**. Dropping it there
would make the dataset's identity a function of a knowledge-base number.

So when the emitted sample list is shorter than the manifest's, that is not data loss to investigate:
read `excluded.md` in the run directory, which names each dropped sample, its exact read count, the
threshold, and the totals. `ComposeResult.admission` carries the same facts as JSON, and is `null`
for every chemistry declaring no floor — which today is all of them but the plate one. Every sample
below the floor is a **refusal**, because a pipeline contracted to produce nothing would otherwise
finish, empty, at exit 0.

## Exit codes

`0` composed; `3` a gate failed, the manifest was invalid, or every sample fell below the chemistry's
read floor (compose refuses to compile an invalid manifest — that refusal is the feature).
