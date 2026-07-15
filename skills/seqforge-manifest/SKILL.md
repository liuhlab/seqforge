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
seqforge manifest fill FILES… --organism 6239 [--accession …]   # the DATASET: what the data IS
seqforge manifest validate MANIFEST                             # the refusal contract (R4)
seqforge manifest hash MANIFEST

seqforge processing new MANIFEST --assembly ce11 --annotation WS298 \
        [--quantify Gene,GeneFull] [--threads N] [--pin|--template] [-o processing.yaml]
seqforge processing validate PROCESSING [--dataset MANIFEST]
seqforge processing hash PROCESSING
```

## Two artifacts, two lifetimes (R13)

A finished assay is immutable; what you do with it is a choice. So there are two files:

| file | is | authority | lifetime |
|---|---|---|---|
| `manifest.yaml` | `library` + `experiment` — what the data **is** | bytes; metadata + humans | **one** per dataset, content-addressed, write-once |
| `processing.yaml` | intent — what to **do** with it | the user, then policy | **many** per dataset |

The dataset manifest is the compiler's **IR**; the processing manifest is its **flags**. Same IR +
different flags = different binaries. Aligning one dataset three ways is three processing manifests
against one unchanged `dataset_hash` — never three forks of the truth. **If you find yourself editing
`manifest.yaml` to change how something is processed, stop: that is the bug R13 exists to prevent.**

`run_id = H(dataset ⊕ processing ⊕ kb ⊕ workflow)`, and `.seqforge/pipeline/<run_id>/` is keyed by it,
so two recipes over one dataset produce two runs rather than one silently overwriting the other.

`manifest fill` takes **no genome**. Choosing a reference is intent, not something you learn by
probing bytes — it lives in `processing new`.

## basis means different things in the two files (R6)

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
— surface it; do not average it, pick between them, or quietly prefer the "better" one. Note R6's
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
decide whether its own edit was valid (R2). If it blocks, the manifest is wrong — not the hook.

Two refusals worth recognizing on sight:

- `GENOME_ORGANISM_MISMATCH` — the recipe's assembly does not belong to `experiment.organism`. This
  is the most dangerous thing this system can get wrong, because it does **not** look wrong: a
  wrong-but-valid assembly aligns, exits 0, and emits a plausible matrix in the wrong coordinate
  space. Every other Blocker catches something that would look broken.
- `DATASET_PIN_MISMATCH` — a bound processing manifest aimed at a different dataset. Do not "fix" it
  by editing the pin; re-run `processing new`, or use `--template` if you meant it to be portable.

## R9: no absolute paths, ever

A manifest with a machine-specific path is not a manifest; it is a note to one machine. Reference:

- **genome** — UCSC assembly id + a *registered GTF name* (`ce11` + `WS298`), never a path
- **software** — a literal `liulab-runtime` env name (`align-rna`), never a path
- **data** — a URI

`/scratch/...` in a manifest is a bug that a hook will block. If you feel the need to bake a path,
the thing you actually want is a registered name.
