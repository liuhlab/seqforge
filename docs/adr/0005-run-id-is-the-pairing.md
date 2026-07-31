# 5. `run_id` hashes the pairing, and is recorded at compile time

Date: 2026-07-31

## Status

Accepted. Supersedes `provenance_id(manifest_hash, kb_version, workflow_version)`.

## Context

A compiled pipeline used to be identified by `provenance_id(manifest_hash, kb, wf)`. While intent
lived *inside* the manifest that was sufficient — changing what you wanted to do changed the
manifest hash, so it changed the id.

After the split into two artifacts ([ADR 0004](0004-two-artifacts-not-one.md)) it is not sufficient,
and the failure was silent: **two recipes over one dataset produced a single id**, and `compose`'s
output path is a function of that id, so the second compile **overwrote the first**. The collision
case was exactly the use case the split exists for.

## Decision

```text
run_id = H(dataset_hash ⊕ processing_hash ⊕ kb_version ⊕ workflow_version)
```

The pairing is recorded **at compile time**, in the compiled output — never inside either input.
Compiled output lives under `pipeline/<recipe>-<run_id[:12]>/`, keyed by the **run**, since one
dataset compiled two ways is two runs.

## Why not store the pairing in one of the artifacts

Either input storing the other's hash re-creates exactly what ADR 0004 removed: a manifest whose
identity moves when intent changes, or a recipe that can no longer be a template. The pairing is a
fact about a *compile*, so it belongs to the compile's output and nowhere else.

## So in code

**Compute `run_id` from all four components, and store the pairing in neither input.** A manifest
that records a `processing_hash`, or a recipe that records a `dataset_hash` anywhere but
`processing.lock.yaml`, re-creates the overwrite this record exists to fix. The **precise** component
names, and their order, are fixed by the Decision above and written nowhere else; everywhere else —
the implementation's own docstring included — write the gloss
`H(dataset ⊕ processing ⊕ kb ⊕ workflow)`, which is a reading aid and cannot drift. A *second
precise* statement is the thing that can: this formula has already been written in two notations at
once, with nothing to say which one had moved. Every component that folds into the key is CalVer, so
a version string that sorts wrong or repeats is a cache-key bug, not a cosmetic one.

**Enforced by.** `test_run_id_differs_per_processing_manifest` (`tests/test_manifest.py`) and
`test_two_processing_manifests_do_not_overwrite_each_other` (`tests/test_compose.py`) — the second is
the original collision, run forward; `test_the_precise_run_id_formula_is_written_once`
(`tests/test_docs.py`) for the notation.

## Consequences

- One dataset compiled two ways yields two directories, both retained; neither can shadow the other.
- Each run directory carries `config.yaml`, `units.tsv`, the `Snakefile`, `processing.lock.yaml`
  (the dataset-bound recipe that produced it), and a **copy of the hand-written workflow module**
  the wrapper imports — copied in rather than referenced by package path, so the directory still
  reads and reproduces after it is moved off the composing machine.
- Component versions are **CalVer**, `kb_version` and `workflow_version` included, precisely because
  they fold into this key: a version string that sorts wrong or repeats is a cache-key bug.
- `compose` refuses a mismatched dataset pin rather than silently re-binding, so a `run_id` always
  names the pair it claims to.
