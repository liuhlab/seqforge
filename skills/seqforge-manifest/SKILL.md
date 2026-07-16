---
name: seqforge-manifest
description: >-
  Assemble, validate and hash the two machine-independent manifests with
  `seqforge manifest fill|validate|hash` (the dataset: what the data IS) and
  `seqforge processing new|validate|hash` (the recipe: what to DO with it). Use
  when building either, fixing validation Blockers, or asked why one was refused.
  Loop until validate passes clean — a manifest is only written once it does.
---

# seqforge manifest + processing

```bash
seqforge manifest fill FILES… [--accession PRJNA…] [--assertions seqforge/assertions.json] \
        [--organism 6239] [--records seqforge/records/PRJNA….json] [--offline]
                                                                # the DATASET: what the data IS
seqforge manifest validate MANIFEST                             # the refusal contract
seqforge manifest hash MANIFEST

seqforge processing new MANIFEST --assembly ce11 --annotation WS298 \
        [--quantify Gene,GeneFull] [--threads N] [--pin|--template] [-o processing.yaml]
seqforge processing validate PROCESSING [--dataset MANIFEST]
seqforge processing hash PROCESSING
```

## Two artifacts, two lifetimes

A finished assay is immutable; what you do with it is a choice. So there are two files:

| file | is | authority | lifetime |
|---|---|---|---|
| `manifest.yaml` | `library` + `experiment` — what the data **is** | bytes; metadata + humans | **one** per dataset, content-addressed, write-once |
| `processing.yaml` | intent — what to **do** with it | the user, then policy | **many** per dataset |

The dataset manifest is the compiler's **IR**; the processing manifest is its **flags**. Same IR +
different flags = different binaries. Aligning one dataset three ways is three processing manifests
against one unchanged `dataset_hash` — never three forks of the truth. **If you find yourself editing
`manifest.yaml` to change how something is processed, stop: that is the bug this separation exists to prevent.**

`run_id = H(dataset ⊕ processing ⊕ kb ⊕ workflow)`, and `seqforge/pipeline/<recipe>-<run_id[:12]>/`
is keyed by it, so two recipes over one dataset produce two runs rather than one silently overwriting
the other.

`manifest fill` takes **no genome**. Choosing a reference is intent, not something you learn by
probing bytes — it lives in `processing new`.

## An accession is fetched, not decoration

`--accession PRJNA1027859` pulls the project, sample, experiment and run records and joins them to the
files. **That is where per-sample metadata comes from** — `strain`, `tissue`, `sex`, `dev_stage` live
on the BioSample record, and until 2026-07-16 they were fetched by no code at all, which is why every
sample in the pilot's manifest said `tissue: null` under a paper that says "neurons".

`--organism` becomes optional when you pass one: the record declares the taxid. A flag still beats the
record — a human typing a taxid is asserting it now, having looked.

**No accession is fine and is the common case.** Most sequencing data never had one. You get samples
grouped by run, no facts attached, exit 0. What is *not* fine is an accession that cannot be fetched:
that exits 3. You asked for those facts, and a manifest is content-addressed and never rewritten, so
quietly omitting them would bake the omission in. `--offline` with `--accession` refuses for the same
reason; fetch once with `seqforge io records` and pass `--records`.

**`--assertions` is how prose reaches the manifest.** Without it the model might as well not have run:
`harvest extract` writes `seqforge/assertions.json` and nothing read it. Pass
`harvest extract --records` too, and each archive record becomes its own document — which is how a
claim gets to name a sample.

## Sample attributes are NCBI's vocabulary, not ours

`experiment.samples[].attributes` is keyed by an **NCBI harmonized BioSample attribute name** — one of
960, with NCBI's definitions (`seqforge io attributes` lists them; `seqforge io attributes <name>`
explains one). The validator refuses anything else.

There is no `condition`. It was ours, no archive defines it, and a field named "condition" accepts
anything you can call a condition — a model duly filed routine worm husbandry into it. Use NCBI's
`treatment` / `genotype` / `disease`.

## One decision, one confidence

`library` holds exactly one `Evidenced` field: `chemistry`. Everything else there follows from it —
`assay` is the same answer in EFO's vocabulary (one label per chemistry, with EFO's own name),
`read_layout` is the KB's structure filled in with measured lengths, `files[].read_id` is the other
half of the same joint optimization. They used to each carry a copy of the same number.

`confidence: null` is legal and informative: it means nothing was judged. A `strain: CQ758` copied out
of a BioSample record is a transcription, not a judgement — `basis: asserted` plus the record
accession in `evidence` already says everything true about how we know it.

## basis means different things in the two files

| section | authority | basis |
|---|---|---|
| `library` | the **bytes** | `observed` |
| `experiment` | metadata + humans | `asserted` |
| `processing` | the user, then policy | `user_confirmed` \| `inferred` |

In the dataset manifest, `basis` records **how we know**. In the processing manifest it records **who
decided** — a corpus row reading "GeneFull because the user's instruction file said so" is
categorically different from "GeneFull because policy defaults to all five". Every field is
`Evidenced{value, basis, evidence, confidence, rung}`.

**Never merge the three truths.** If observed and asserted disagree, that is a first-class `Conflict`
— surface it; do not average it, pick between them, or quietly prefer the "better" one. Note the
three truths are the three *bases*, not three sections; that pun is exactly what let `processing`
masquerade as a truth until the split.

Precedence in the processing manifest is **flag > `--instruction` document > policy default**, and it
is silent by design: a user overriding a default is not an ambiguity, it is what an instruction *is*.
What IS surfaced is two instructions disagreeing at the same precedence (exit 4) — no tiebreak exists.

## Validate is the contract, not a formality

`validate` returns structured `Blocker`s and a nonzero exit. Every `remedy` is actionable by
contract. Loop: fix → re-validate → repeat. `manifest.yaml` is written **only** after a clean pass;
until then it is `manifest.draft.yaml`.

A `PostToolUse` hook re-runs `validate` after any manifest edit, because the model does not get to
decide whether its own edit was valid. If it blocks, the manifest is wrong — not the hook.

Two refusals worth recognizing on sight:

- `GENOME_ORGANISM_MISMATCH` — the recipe's assembly does not belong to `experiment.organism`. This
  is the most dangerous thing this system can get wrong, because it does **not** look wrong: a
  wrong-but-valid assembly aligns, exits 0, and emits a plausible matrix in the wrong coordinate
  space. Every other Blocker catches something that would look broken.
- `DATASET_PIN_MISMATCH` — a bound processing manifest aimed at a different dataset. Do not "fix" it
  by editing the pin; re-run `processing new`, or use `--template` if you meant it to be portable.

## No absolute paths, ever

A manifest with a machine-specific path is not a manifest; it is a note to one machine. Reference:

- **genome** — UCSC assembly id + a *registered GTF name* (`ce11` + `WS298`), never a path
- **software** — a literal `liulab-runtime` env name (`align-rna`), never a path
- **data** — a URI

`/scratch/...` in a manifest is a bug that a hook will block. If you feel the need to bake a path,
the thing you actually want is a registered name.
