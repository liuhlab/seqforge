# 27. A run spans its lanes, and filenames group no further

Date: 2026-08-04

## Status

Accepted.

## Context

`resolve/group.py` derives a **Run** from a filename, and with no archive record that grouping *is*
the sample identity ([0010](0010-two-resolvers-one-blocks-one-warns.md)). Two branches derive it, and
until this record they named different things.

`_SRA_RUN` keys a leading run accession. `_MATE` strips a trailing mate token and keys what is left —
which, for the naming bcl2fastq writes, retains the lane:

```
cell_42_S1_L001_R1_001.fastq.gz            -> cell_42_S1_L001
cell_42_S1_L002_R1_001.fastq.gz            -> cell_42_S1_L002    # same library, second SAMPLE
SRR111_x_S1_L001_R1_001.fastq.gz           -> SRR111
SRR111_x_S1_L002_R1_001.fastq.gz           -> SRR111
```

So a four-lane library delivered as a directory — the normal case, not the degraded one — compiled as
**four samples**, each at a quarter depth, each with its own `<sample>.h5ad`. Nothing refused: the
grouping is self-consistent, every file gets a role, `validate` passes, exit 0. It is the failure
class `group.py` was written to prevent, one level up: that bug dropped files, this one split a
library ([#263](https://github.com/liuhlab/seqforge/issues/263)).

**The obvious reading, and why it is wrong.** `_read_designation` (`resolve/engine.py`) already
argues *against* a de-laned basename: the flowcell id differs between flowcells, so de-laning cannot
bridge them, and a surplus file stayed unassigned. That argument is about **role propagation inside a
group already formed** — it says de-laning is *insufficient* there, not that it is wrong here. It
does not carry to grouping, and this record exists so the next reader does not re-derive the
rejection from the same docstring.

**The archive does not settle it either.** Measured across the benchmark tier: 7 of 18 cases carry
several runs under one experiment, and the deposits disagree about what a run is — GSE310378 puts
lanes *inside* one accession (`SRR36109512_..._S1_L005`), GSE126954 deposits each lane as its own
`SRR`. Both come out right today only because the record path joins at the **sample** level, above
the run. A compiler that inherited the archive's notion would inherit the inconsistency.

## Decision

**A run is lane-blind: one library on one pass of a sequencer, spanning every lane it was loaded
into. `run_key` strips the lane token, and stops there.**

| | rule |
| --- | --- |
| **what is stripped** | a separated `L` + **exactly three digits** (`_L001`, `.L001`), as a trailing token once the mate token is off |
| **what is never stripped** | `_S<n>` — the sample-sheet entry, and the one token separating two libraries on one flowcell |
| **when it is stripped** | always, from the filename alone — never conditioned on what else is in the directory |
| **the floor** | a strip that would leave nothing keeps the name |

**A run is a property of the file, not of the folder.** `run_key` stays a pure function of one
basename: the same file resolves to the same run whether or not its siblings have arrived. Grouping
that consulted the directory would let a file's sample identity — and so `dataset_hash` — move when a
neighbour lands.

**The lane survives as data.** `units.tsv` carries a `lane` column, written by `compose` from the
same token `run_key` removed, and the mapping module orders a sample's files by `(run, lane, path)`.

## Why not a looser lane token

`XQTL_F4_N2PTM299_L2_1_S2_L004_R1_001.fastq.gz` — 15 files on the corpus — spells the worm's larval
stage `L2` in the same name it spells the lane `L004`. Be precise about what protects it: the strip
is **trailing-anchored** on the mate-stripped stem, so a mid-name `L2` survives `_L\d+` too. The
digit count earns its keep where such a token lands trailing — `worm_L2_R1_001.fastq.gz` reduces to
`worm_L2`, which a loose rule fuses across larval stages and the strict rule leaves alone.

The corpus is what settles it: all 250 real lane tokens in the tier are three digits, because
bcl2fastq pads and a larval stage does not. `L<n>` is not a lane-only namespace, so the rule reads
the padding rather than the letter.

The asymmetry decides the rest. Splitting a library gives you four quarter-depth matrices, which a
human notices. Merging two samples gives you one plausible matrix, which nobody notices. A grouping
rule may fail toward the first and never toward the second, so the rule strips what bcl2fastq
demonstrably writes and guesses at nothing.

## Why not fuse the runs of one sample too

A library resequenced for saturation arrives as `cell_42_S1_L00*` and `cell_42_S3_L00*` — a second
sample sheet, so a second `_S<n>`, and stripping that is the merge this record forbids. **Two runs of
one sample do not rejoin from filenames, and this record does not make them.** With a record they
already do, at the sample level, and the gap is record-less data only.

That gap is answered by supplying the record rather than by guessing harder at the name: the archive
shape seqforge already consumes has an experiment level, which is exactly where "these runs are one
library" is expressible. Nothing in the LLM surface can carry it — `PERMITTED_FIELDS` names sample
*attributes* and no filename, and prose that would license the join ("libraries were resequenced")
entails no file-level mapping, so a field for it would verify vacuously.

## So in code

**Strip the lane and stop: no `_S<n>`, no directory context, no second notion of a run.** A file's run
comes from `resolve.group.run_key` and nowhere else — `compose` reads the lane from the same token it
removed, and the mapping module parses no filename at all. Adding a grouping rule means naming the
convention it reads and the corpus files that prove it, or the rule is a guess about identity.

**Enforced by.** One test per row of the decision table, all in `tests/test_resolve.py`:
`test_the_lanes_of_one_library_are_one_run` and `test_the_lanes_of_a_single_end_library_are_one_run_too`
(lane-blindness), `test_the_sample_sheet_entry_is_never_stripped_with_the_lane` (`_S<n>`),
`test_a_lane_is_three_digits_because_bcl2fastq_pads` (the digit count),
`test_a_name_that_is_only_a_lane_keeps_it` (the floor),
`test_the_lane_survives_as_data_from_the_same_token_the_run_key_dropped` (`lane_of`), and
`test_run_key_groups_by_accession_and_never_by_role` (the accession branch, unchanged).
`test_the_composer_records_the_run_each_unit_came_from` and
`test_a_sample_pooled_across_lanes_pairs_readfilesin_by_the_lane_column` (`tests/test_compose.py`)
hold `units.tsv` to the same function rather than a second parse, and hold the mates to one order.
`test_a_record_less_multi_lane_deposit_stays_two_samples` (`tests/test_evals.py`) is the corpus guard
the Consequences below promise — it fails with four samples if the strip is reverted.

## Consequences

- **Sample identity moves for record-less data.** `experiment` is inside `dataset_content_hash`, so
  every record-less multi-lane dataset gets a new hash and any `processing.yaml` pinned to the old one
  refuses — correctly, and loudly. Single-lane names change too (`N2_S11_L003` -> `N2_S11`): the price
  of a key that ignores the directory.
- **Both stamps move.** `RESOLVE_VERSION`, because cached resolve results hold the old grouping and
  would be served on resume; `WORKFLOW_VERSION`, because `units.tsv` gained a column.
- **The mate-pairing guarantee changed hands.** It was carried by the run key — four lanes, four runs,
  the sort never reaching `path`. Fusing them would have left it to lexical path order, which holds
  for bcl2fastq names by coincidence of where the read token sits, and whose failure is silent: the
  comma-lists still hold equal read counts, so STAR completes and writes a matrix pairing one lane's
  barcodes with another's cDNA. The `lane` column is what keeps it a fact.
- **Neither eval tier could see this.** The ci tier is record-less but single-lane; every lane-tokened
  benchmark case ships a `records.json` and takes the record path. The corpus hole was the shape of
  the bug. What closes it is the **test** `test_a_record_less_multi_lane_deposit_stays_two_samples`,
  which stages the deposit and runs the case through `run_case`; the case's own corpus node skips
  without its env var, and its `expected.yaml` cannot carry the count — `Expected.fields` reaches
  `library.*`, `rung` and `experiment.*`, and a record-less dataset has no sample attribute, so the
  graded surface is identical at two samples and at four. Making it a pure corpus case needs a
  `SpecRecipe` deposit knob (`libraries` x `lanes`) and an `experiment.n_samples` grade path; until
  then the guard is real but lives one level out from the corpus, and this is why.
- **The role-assignment burden moved, and this is where it now sits.** Four lanes used to be four
  runs, each scored alone, each filling its own roles. They are now ONE run of eight files, and the
  injective assignment fills each role once — the other six are surplus, re-seated by
  `index_tagged_roles` only within `_LANE_LEN_TOL` (3 bp) of their role's representative. A lane whose
  modal read length drifts further gets **no role**, and `compose._units` drops a file with no role
  silently, at exit 0. Real lanes of one library share a cycle count so the modes coincide, and the
  accession path has relied on this since 2026.7.4 — but this record is what extended that reliance to
  every record-less multi-lane dataset, and a trimmed-per-lane delivery is the shape that would find
  it.
- **Two known gaps, stated.** (1) Two runs of one **Sample** stay two samples without a record — the
  follow-up is a user-supplied record set in the archive-neutral shape, not a wider filename rule.
  (2) **Flowcells are not fused either.** A record-less library sequenced across two flowcells carries
  a different flowcell id per file, so the de-laned names still differ and it splits one run per
  flowcell — the same quarter-depth exit-0 shape, one level up (GSE208154 is 2 flowcells x 8 lanes).
  This is the question [#263](https://github.com/liuhlab/seqforge/issues/263) asked to decide, and the
  decision is that it is out of scope for a *filename* rule: a flowcell id is submitter-chosen, not a
  convention, so reading one would be the guess this record's asymmetry forbids. It is the same gap as
  (1) and takes the same answer — supply the record.
