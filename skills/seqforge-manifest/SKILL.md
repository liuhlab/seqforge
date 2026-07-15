---
name: seqforge-manifest
description: >-
  Assemble, validate and hash the machine-independent library manifest with
  `seqforge manifest fill|validate|hash`. Use when building a manifest, fixing
  validation Blockers, or asked why a manifest was refused. Loop until validate
  passes clean — a manifest is only written once it does.
---

# seqforge manifest

```bash
seqforge manifest fill --result RESOLVE.json --organism-taxid N --assembly ID --annotation NAME
seqforge manifest validate MANIFEST --json     # the refusal contract (R4)
seqforge manifest hash MANIFEST                # content hash + provenance_id
```

## Three sections, three authorities (R6)

| section | authority | basis |
|---|---|---|
| `library` | the **bytes** | `observed` |
| `experiment` | metadata + humans | `asserted` |
| `processing` | derived from the other two + policy | `inferred` |

Every field is `Evidenced{value, basis, evidence, confidence, rung}`. **Never merge the three.** If
observed and asserted disagree, that is a first-class `Conflict` — surface it; do not average it,
pick between them, or quietly prefer the "better" one.

## Validate is the contract, not a formality

`validate` returns structured `Blocker`s and a nonzero exit. Every `remedy` is actionable by
contract. Loop: fix → re-validate → repeat. `manifest.yaml` is written **only** after a clean pass;
until then it is `manifest.draft.yaml`.

A `PostToolUse` hook re-runs `validate` after any manifest edit, because the model does not get to
decide whether its own edit was valid (R2). If it blocks, the manifest is wrong — not the hook.

## R9: no absolute paths, ever

A manifest with a machine-specific path is not a manifest; it is a note to one machine. Reference:

- **genome** — UCSC assembly id + a *registered GTF name* (`ce11` + `WS298`), never a path
- **software** — a literal `liulab-runtime` env name (`align-rna`), never a path
- **data** — a URI

`/scratch/...` in a manifest is a bug that a hook will block. If you feel the need to bake a path,
the thing you actually want is a registered name.

## Known gap worth carrying

`processing.quantification` is currently **decorative** — policy sets it and `compose` ignores it,
reading `soloFeatures` from the KB instead. Two sources of truth for one decision, unable to disagree
only because one is never consulted. Do not rely on that field meaning anything yet.
