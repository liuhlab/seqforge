# 5. `run_id` hashes the pairing, and is recorded at compile time

Once intent moved out of the manifest, `provenance_id(manifest_hash, kb, wf)` collided silently: two
recipes over one dataset produced one id, and the second compile overwrote the first. So a run is
`run_id = H(dataset_hash ⊕ processing_hash ⊕ kb_version ⊕ workflow_version)`, recorded in the
compiled output and in neither input — either input storing the other's hash re-creates what
ADR-0004 removed, a manifest whose identity moves when intent changes or a recipe that can no longer
be a template. This is the formula's only precise statement; everywhere else carries the drift-proof
gloss `H(dataset ⊕ processing ⊕ kb ⊕ workflow)`.

**Status.** Supersedes `provenance_id(manifest_hash, kb_version, workflow_version)`.
