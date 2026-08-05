# 30. A measurement the dataset's identity must exclude lives in provenance, and the threshold over it is applied at compose

Date: 2026-08-04

## Status

Accepted. Called for by [#260 decision 7](https://github.com/liuhlab/seqforge/issues/260), which
named it 0028 before that number was taken — the number here is the one free when it was written,
which is the only way a number stays true.

## Context

The compiler needs numbers *about* bytes at compile time. The first is a read depth: a chemistry may
declare `min_input_reads`, and a cell below it is a starved well to be excluded rather than analysed
([#253](https://github.com/liuhlab/seqforge/issues/253)). Compose cannot obtain one.
`FileInventoryItem` is uri, basename, sha256, size and read id; `SampleGroup` is ids, an accession,
attributes and uris; no read count exists anywhere in the IR. There is no `Observation` under
`compose/`, and `plan` is pure by contract — *"the path is joined, never read"* — so a third input to
the compiler is not available either.

**The obvious reading is to put the number beside the file it describes**, in `library.files`. It is
reachable from the same evidence every time, and it is wrong for two independent reasons:

1. **The number is budget-dependent.** `Observation.estimated_total_reads` is an exact count when the
   probe reaches EOF and an extrapolation from compressed bytes-per-read (or the gzip ISIZE) when the
   budget stops it first — `est_method` says which. `dataset_content_hash` covers `library` +
   `experiment`, so inside them the number makes `dataset_hash` a function of `--max-reads`: the same
   bytes, two identities, and a `processing.yaml` pinned to the first correctly refuses the second.
2. **The verdict is KB-versioned, and the manifest is write-once.** The tempting compression is to
   store the *answer* rather than the number — an excluded-sample list, or `read_id: null` as a drop
   marker. `run_id` folds `KB_VERSION`, which is what makes *two thresholds, two pipelines, dataset
   hash unmoved* true; a verdict frozen into an immutable artifact makes the second compile re-read
   the first KB's opinion. (`read_id: null` fails ahead of that anyway: it raises
   `blk-unassigned-<sha>`, so it refuses rather than drops, and `read_id` is inside the hash.)

Both halves generalise past the assay that found them. Any future gate wanting a measurement at
compile time — a minimum coverage, a duplication ceiling — arrives at the same fork.

## Decision

**A byte measurement the compiler needs downstream but the dataset's identity must not include lives
in `provenance`. A threshold over it is applied at compose under the live KB, never frozen into the
write-once manifest as a verdict.**

| | |
| --- | --- |
| **what is stored** | the measurement, per file, keyed by sha256 — `DatasetProvenance.estimated_reads` |
| **where** | `provenance`, the section no content hash covers and which already *"binds a dataset manifest to the bytes and the KB that read them"* |
| **when** | unconditionally: every file, every manifest, whatever the loaded KB declares |
| **what is never stored** | the threshold, the comparison, or its outcome |
| **a run's number** | the **minimum** over its files; across runs the counts add |
| **an absent number** | `None`, never `0` |

**Unconditional is part of the decision, not a convenience.** Populating the counts only for specs
that declare a threshold makes a manifest's *contents* a function of KB state on the day it was
written: ship a KB later that adds a threshold to an existing chemistry, and every manifest written
before it has nothing to gate against, so compose must either refuse a manifest it should read or
skip the gate in silence. Two manifests of the same bytes behaving differently by date is exactly the
coupling a write-once artifact exists to remove. The price is the widest footprint in
[#278](https://github.com/liuhlab/seqforge/issues/278) — provenance changes shape for every dataset
seqforge compiles, plate or not — and it is paid once, at the only moment it is cheap.

## Why not beside the file it measures

Because `size_bytes` sits there and looks like a precedent, and is not one. A file's size is a
property of the file: two probes at two budgets agree on it, and a fingerprint package reproduces it
from a pin without reading a byte. A read *estimate* is a property of the file **and** the budget the
probe ran under. The two fields differ in what they are a function of, which is the only question
that decides which side of a content hash a value belongs on, and the field name says so:
`estimated_reads`, not `reads`.

The same test is what keeps the rule usable later. Before adding a field to `library` or
`experiment`, ask what can move the value with the bytes held constant. A CLI budget can. A KB
version can. Neither may reach the identity.

## Why not a per-sample sum frozen at fill time

It is a smaller field and it answers the gate directly, so it looks like the economical form. It
freezes two things instead of one: the join (which files are one sample) and the arithmetic (min
within a run, sum across them). The join is free at compose from `file_uris`, and the arithmetic
belongs to whoever is asking — a per-file count answers "which cells are starved" and also "what is
the depth distribution of the cells that *lived*", which is the question a per-sample sum has already
thrown away. You cannot see a uniformly halved plate in a list of the samples that died; you can see
it in the depths of the ones that survived.

**Min within a run, and not the sum.** A run's mates are two views of the same fragments — 900 000
pairs are 1 800 000 FASTQ records and 900 000 reads — so summing a pair reports a library at twice
its depth. Healthy mates are equal by construction, which makes the minimum free rather than
pessimistic, and the pair that is *not* equal has already been refused upstream as a truncated
member. Across runs the counts genuinely add: two runs are two passes over the library.

## Why not a third input to compose

Handing the composer the resolve artifact, or letting it read the probe cache beside `--fastq-dir`,
would deliver the numbers without touching the manifest at all. It breaks the two-artifact closure
R11 states — the compiler's inputs are the dataset and the recipe, and a third one that must be
present for a gate to fire makes the pair no longer sufficient — and it ends `plan`'s purity, which
is what lets a recipe be planned against a manifest with no FASTQ on the machine.

## So in code

**A number about the bytes goes in `provenance`; the comparison against it goes in the consumer.**
When a stage needs a measurement a later stage will threshold, write the measurement — per file,
keyed by sha256, unconditionally — and write nothing about what it means. The consumer loads the KB
it is running under, applies the threshold there, and records the outcome in its own output, never
back into the manifest. Read a stored count through `DatasetProvenance.reads_in_run` rather than by
indexing the dict: it owns "minimum within a run" and it is what returns `None` for a manifest that
measured nothing, which a gate must not read as zero reads.

**Enforced by.** The storage half, in `tests/test_manifest.py`:
`test_the_read_counts_move_neither_the_dataset_hash_nor_the_run_id` (stripped and doubled, against
the fixture's own recorded hash), `test_provenance_counts_the_reads_of_every_file_in_the_inventory`
(unconditional, and complete over the inventory), `test_a_runs_read_count_is_the_minimum_over_its_files`,
and `test_an_unmeasured_file_gates_as_none_rather_than_as_zero`.
`test_a_provenance_read_count_cannot_be_pre_registered_in_a_case` (`tests/test_evals.py`) holds the
third of the three surfaces the number must not reach.

**Nothing enforces the compose half yet, because nothing applies a threshold yet.** No shipped spec
declares `min_input_reads`, so there is no gate to hold to the live KB. What would notice a
violation is a test that composes one manifest twice under two thresholds and asserts two `run_id`s
over one unmoved `dataset_hash` — it must land with the first consumer
([#278](https://github.com/liuhlab/seqforge/issues/278) §E), and until then this half of the record
is an obligation on that ticket rather than a gate.

## Consequences

- **Every manifest changes shape, and no hash moves.** The counts are outside
  `dataset_content_hash`, outside `run_id` (which takes the dataset hash as a *string* plus the kb,
  processing and workflow versions) and outside `CaseGrade`, whose graded field set reaches
  `library.*`, `rung` and `experiment.*` and answers `<unsupported field …>` to anything else. Three
  verified facts, and the map's record on "this should be unchanged" is why they are pinned as tests
  rather than asserted in prose.
- **A manifest written before the field loads unchanged and gates as unmeasured.** The field defaults
  to empty and `reads_in_run` answers `None` rather than `0` — the distinction between *not measured*
  and *empty*, which is the difference between a gate that abstains and a gate that drops every
  sample at exit 0.
- **`estimated_reads` is not a substitute for reading the file.** It is what the probe estimated
  under its budget, and a consumer that needs an exact count of a large file must count it. The gate
  this record was written for is safe by construction — any file small enough to fail a 1000-read
  threshold was read to EOF at zero extra bytes — and a future threshold set above the probe budget
  would be comparing against an extrapolation, which is a fact about that threshold to state where
  it is chosen.
- **A fingerprint package reproduces the counts the same way it reproduces the hash**: the pin
  carries the original's size and ISIZE, the replay rebuilds the estimate from them, and a package
  cut at or above the probe budget lands on the same number. Where it does not — a lighter package —
  only the counts differ, and `dataset_hash` still matches, which is the property this record
  arranges rather than a coincidence.
- **The report does not surface the distribution yet.** `report/collect.py` renders provenance as
  three key/value rows; the depths are in the manifest and nothing draws them. Making a starved-well
  histogram legible is a report change, and it is not made here.
