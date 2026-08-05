# 32. A spec declares the shape of a deposit, and compose acts on that declaration without the manifest recording the outcome

Date: 2026-08-05

## Status

Accepted. Called for by [#253 decision 8](https://github.com/liuhlab/seqforge/issues/253), which
named it 0027 before that number was taken, and re-scoped by
[#292](https://github.com/liuhlab/seqforge/issues/292); the number here is the one free when it was
written, which is the only way a number stays true. Extends
[ADR-0030](0030-a-measurement-lives-in-provenance.md), which decided where the *measurement* lives —
this record decides who declares the threshold over it, and what happens to what the threshold
excludes.

## Context

A plate-based, one-cell-one-file library — SMART-seq3, and every assay where demultiplexing happened
at the bench — arrives as 1440 samples of one chemistry, and everything downstream reads it as 1440
independent libraries. Three facts about it are true and none of them is in the bytes:

1. **One `Sample` of this chemistry IS one cell.** Deriving it was tried and is backwards in both
   directions: SMART-seq2 has neither a UMI nor a barcode and is still one cell per file, and
   UMI-tagged bulk has a UMI and no barcode and is one file per specimen. The property is about
   *where demultiplexing happened*, which is outside the bytes entirely.
2. **A cell below some read depth cannot be analysed.** Not *should not* — a well with 400 reads
   produces a matrix column of noise, and the pipeline reports it with the same confidence as a good
   one.
3. **Failed and empty wells are designed into the plate format, not exceptional.** Measured across
   190 well-labelled plate deposits, a dud well is the normal state of a real plate. So a refusal on
   one empty well is a refusal on every real plate, and "refuse rather than guess" — right nearly
   everywhere else in this compiler — is the wrong shape here.

**The obvious reading of (2) is to drop the starved cell where the samples are listed**, in the
dataset manifest. It reaches the right pipeline by the shortest path: nothing downstream has to learn
anything, because the cell is simply not there. It is wrong, and the reason generalises past this
assay: the manifest is *what the data IS*, and you were handed 1440 cells. Dropping there makes
`dataset_hash` a function of a knowledge-base number, so raising the floor from 1000 to 1500 gives the
**same data a different identity** — which is exactly what a content hash invariant under processing
change exists to prevent (ADR-0004), and it would silently invalidate every `processing.yaml` pinned
to the dataset.

The second reading is to keep the cell but freeze the *verdict* — an exclusion list, or a null read
role as a drop marker. It fails for a related reason: the manifest is write-once, so the second
compile under a newer knowledge base re-reads the first one's opinion of a threshold that has since
moved.

## Decision

**A `spec.yaml` may declare a fact about the shape of a deposit that the bytes cannot show. Code
consumes the declaration; the manifest records neither it nor what it decided.**

| | |
| --- | --- |
| **the declaration** | `identity.sample_is_cell` — one `Sample` of this chemistry is one cell. `Spec.min_input_reads` — the read depth a `Sample` must reach to be analysed at all |
| **who reads them** | the dataset reduction (a starved cell *abstains* and inherits its plate's chemistry rather than dissenting) and `compose` (it drops the cell from the pipeline). Nothing else |
| **the unit** | the `Sample`, summed over its runs: the **minimum** within a run, the **sum** across them |
| **when the threshold is applied** | at **every compile**, under whatever knowledge base is loaded then |
| **what the manifest carries** | the per-file measurement, in `provenance` (ADR-0030) — and no threshold, no comparison, no verdict |
| **what leaves the pipeline** | the excluded sample: out of `config["samples"]`, out of `units.tsv`, into the exclusion record |
| **what never leaves the manifest** | the excluded sample. All 1440 stay |

**Applied live, and that is the half that needs the argument.** `run_id` folds the knowledge-base
version, so re-filling under a bumped KB and composing again yields a **new run directory** rather
than overwriting the compile made under the old floor: two thresholds, two pipelines, both auditable,
one unmoved `dataset_hash`. That property is what makes it safe for a knowledge-base number to change
what compiles, and it holds only because the verdict is recomputed rather than stored.

**Every sample below the floor is a refusal**, because an empty `rule all` at exit 0 is the
silent-success failure class. There is deliberately **no drop-rate gate** above it: the count is
reported, not gated — a rate threshold needs a number nobody can defend, and a plate with 60% dud
wells is real.

## Why not derive `sample_is_cell` from the read layout

`umi and not barcode` is the rule everyone reaches for, and it is wrong in both directions at once —
SMART-seq2 (neither, still one cell per file) and UMI-tagged bulk (a UMI, no barcode, one file per
specimen). Archive cardinality was measured as the alternative signal and is dead: deposit shape is
the submitter's choice, not the protocol's, and the SMART-seq3 authors deposited their own data as 10
and 6 samples while a third group deposited 1440 for the same chemistry. No sample-count threshold
fires on the plate and spares four hand-verified non-plates that are strictly 1:1 with **more**
samples than it. What is left is a declaration, and a declaration is cheap: it is one line of a
`spec.yaml`, it round-trips like everything else in an entry, and it says the thing rather than a
proxy for it.

## Why not drop the cell in the manifest

Because `dataset_hash` would become a function of a knowledge-base number. The concrete cost is not
abstract tidiness: a `processing.yaml` pinned to a dataset refuses when the dataset hash moves, so
raising a floor would break every recipe bound to every plate ever compiled, and two corpora built
six months apart would disagree about which datasets they even contain. The split ADR-0004 draws is
what makes a corpus of 10⁴ datasets addressable at all — **each artifact answers its own question** —
and "which cells does this deposit contain" is the dataset's question, while "which cells is this run
producing" is the pipeline's.

The consequence is accepted openly: **`units.tsv` and the manifest now disagree about what exists**,
and that is the first time in this compiler they do. It is not a leak, it is the split working. What
makes it legible rather than confusing is that the disagreement has a written explanation sitting in
the same directory as the shorter of the two lists.

## Why not freeze the exclusion list into the manifest instead

It looks like the compromise that keeps both halves — the cell stays, and the verdict is recorded
beside it — and it is the reading a future maintainer will reach again. It fails because the manifest
is **write-once**: the second compile, under a knowledge base whose floor has moved, would silently
re-read the first one's answer. A stored verdict is a cache with no key, and the key it needs
(`KB_VERSION`) is already folded into the thing that names the *output* directory, which is where the
verdict belongs.

The nearest variant, a null read role as a drop marker, fails ahead of that anyway: it raises an
unassigned-file Blocker, so it refuses rather than drops, and the read role is inside the hash.

## Why the exclusion record, and not an alert

An **alert** is the channel that looks right — post-run evidence, named and non-fatal. ADR-0026 rules
it out cleanly: an alert *"writes no artifact, changes no exit code and produces no refusal."* A drop
changes what compiles, so it is not advisory.

The record lives in the pipeline directory because that is the deliverable a human opens, and it
carries four things:

- each excluded `sample_id` with its **exact** read count — exact, because any file shallow enough to
  fail a floor of this size was read to EOF inside the probe's budget, at no extra bytes;
- the threshold, and the chemistry that declared it;
- **the totals — "240 of 1440 cells dropped"** — which is the line that actually does the work, since
  nobody spots a split cell by reading 240 rows but everybody spots 768 samples on a 384-well plate;
- and, when at least one cell was dropped **and** no sample in the deposit carries an archive
  accession, one line saying the cell axis came from filenames.

That last line is a disclosure of something **unfixable by construction**. With no records to join
on, the filename grouping *is* the sample identity, so a cell sequenced across two runs arrives as two
half-depth samples and a floor gates it twice. Nothing in the bytes or the names says two runs are one
cell. Rejected: a Blocker (it refuses every record-less plate with an empty well, which is every real
record-less plate) and a fill-time warning (it fires on every record-less dataset before any drop
exists — noise on the 89.5% that are strictly 1:1).

## So in code

**Read a deposit-shape declaration off the loaded spec at the moment you act on it, and write what it
decided into your own output — never back into the manifest.** Concretely: `min_input_reads` is
applied by `compose` against the spec it loads at compile time, over the per-file counts in
`provenance`; a sample's depth is the minimum within each run and the sum across them
(`compose.admission.sample_reads`); the surviving samples are what `config["samples"]` and `units.tsv`
carry, because a dropped sample was never *contracted* and must not be reported downstream as a result
that failed to arrive; and the manifest is read and never rewritten. An unmeasured sample is not an
empty one — refuse rather than gate it as zero. When adding the next such declaration, ask what can
move the value with the bytes held constant: if a knowledge-base version can, the value may name a
pipeline and may not name a dataset.

**Enforced by.** The compose half, in `tests/test_compose.py`:
`test_a_cell_below_the_live_kbs_floor_leaves_the_pipeline_and_stays_in_the_manifest`,
`test_a_cells_depth_is_the_minimum_within_a_run_and_the_sum_across_them`,
`test_the_exclusion_record_carries_each_dropped_cell_its_count_the_threshold_and_the_totals`,
`test_the_record_less_caveat_appears_only_when_a_drop_met_a_dataset_with_no_accession`,
`test_compose_refuses_a_dataset_whose_every_cell_is_below_the_floor`,
`test_compose_refuses_a_manifest_that_measured_no_reads_rather_than_gating_it_as_empty`,
`test_the_drop_is_invisible_to_the_dataset_hash` and
`test_no_shipped_spec_declares_a_floor_so_the_composer_adds_no_step`;
`test_compose_says_on_the_human_stream_that_it_dropped_cells` (`tests/test_cli.py`) holds the
reporting line and `test_the_exclusion_record_is_named_here_and_absent_when_nothing_was_excluded`
(`tests/test_pipeline.py`) holds its place in the run directory. The declaration half, in
`tests/test_kb.py`:
`test_no_shipped_spec_says_a_sample_is_a_cell_or_sets_a_read_floor` and
`test_a_read_floor_of_zero_is_a_gate_that_cannot_fire`. The reduction half, in `tests/test_resolve.py`:
`test_the_cell_gate_is_inert_when_no_chemistry_says_a_sample_is_a_cell`,
`test_a_cell_below_the_read_floor_abstains_rather_than_dissenting`,
`test_the_read_floor_is_summed_over_a_samples_runs_and_not_asked_of_each` and
`test_a_cell_deciding_a_different_chemistry_outright_refuses_the_plate`.

## Consequences

- **The whole path is inert today.** `sample_is_cell` is `False` and `min_input_reads` is `None` on
  all sixteen shipped entries, so every dataset seqforge compiles takes the byte-for-byte path it took
  before this existed: no gate, no record, no config key. That is what makes the mechanism cheap to
  carry, and it is also why it needs a written decision — nothing exercises it yet except the tests
  built to.
- **`units.tsv` and the manifest may now disagree about what exists**, deliberately, and the
  exclusion record is the only thing that reconciles them. A reader of a pipeline directory who has
  not read this record will find a shorter sample list and no explanation unless that file is there —
  which is why it is written at the same moment the drop happens rather than derived later.
- **The separation of two compiles rides on the manifest's recorded knowledge-base version.**
  `run_id` folds `provenance.kb_version`, which is stamped at fill time, so the "new directory rather
  than an overwrite" property holds for the ordinary flow (bump the KB, re-fill, re-compose — the
  dataset hash does not move and the run id does). Composing an *old* manifest under a *new* knowledge
  base reuses the old run id and overwrites, and nothing notices; that is a gap in ADR-0005's
  arrangement rather than in this one, recorded here because this is the decision that leans on it.
- **A `min_input_reads` above the probe budget would be compared against an extrapolation.** The
  schema says so and nothing enforces it, because the budget is a CLI flag and the threshold is KB
  data; the gate is safe for any floor small enough to matter, since a file that could fail one was
  read to EOF.
- **The report does not draw the depth distribution.** The counts are all in provenance and a uniform
  halving is visible only in the cells that *lived*, which is the thing a list of the dead cannot
  show. Making that legible is a report change and is not made here.
