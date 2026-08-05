# 35. The mate is an addition to UMI extraction, not half of it

Date: 2026-08-05

## Status

Accepted. Builds on [0029](0029-a-spec-declares-read-sets-not-a-fixed-read-list.md), which gave a
spec the vocabulary for a second sequencing configuration; this record is what lets a plate assay
*run* one.

## Context

Smart-seq3's peer-reviewed Methods publish three sequencing configurations verbatim — *"75-bp single
end, 50-bp single end or 150-bp paired end"* — and the `smartseq3` entry expressed the third only.
0029 built the mechanism for the other two and `bulk-rnaseq` was the only entry that used it, because
the plate half could not follow: `map/star-umi`'s mate-role helper **raised** rather than rendering
`--r2` with nothing after it, and said so out loud — *"the extractor pairs two FASTQs positionally
and has no single-end form yet"*.

**The obvious reading is that single-end is a degenerate pairing**, so it wants a second code path
that does less — or a second verb, so the reduced case cannot contaminate the real one. That reading
is backwards, and this record exists so the next reader does not re-derive it. The tag operation is
already entirely *within* one read: find the anchor, cut the UMI out, trim `geometry.span` bases,
keep the rest. The second FASTQ contributes nothing to it. It only *inherits* the resulting `UB` onto
a record emitted alongside. Take it away and the operation is unchanged; only the record count out
the other end moves.

The reference implementation this tree re-implemented had already reached the same shape.
[`umite`](https://github.com/leoforster/umite)'s `umiextract` declares `-1/--read1` required and
`-2/--read2` optional. We narrowed the arity when we re-implemented it, and the narrowing was never
argued for.

**Where the refusal actually landed was measured, not read.** A single-read plate config dry-run at
`7e2488f` gives `InputFunctionException … ValueError: this layout carries only the tagged read`, at
DAG construction. `wiring_gate` turns that into `"fail"` and `compose` into **exit 3** — with no
reason attached, because the gate discards snakemake's output. On a machine with no snakemake the
gate returns `"skip"`, `compose` exits **0**, and the raise lands wherever the user submits. Both
halves of that are worse than the exit-3 refusal the issue was filed to preserve.

## Decision

**A UMI extraction is one read's. The mate is an addition that inherits the tag, and
`seqforge io umi-extract` either takes one or does not.**

| | rule |
| --- | --- |
| **the operation** | find the anchor, cut the UMI, trim `geometry.span` — entirely within the **Tagged read** |
| **the mate** | optional, and untouched. It inherits the resulting `UB` onto a record emitted alongside, and contributes nothing to the extraction |
| **the verb** | one. `--r2` is nullable; the geometry parse, the `--read-id` refusal, the anchor search, the bounded reader and the truncation checks are shared unchanged |
| **the output** | one unaligned record per fragment, or two interleaved ones. The flags say which, so the uBAM is self-describing |
| **the module** | derives both the `--r2` argument and `SAM SE` / `SAM PE` from **one** per-sample list — the mate FASTQs it staged. Not one from the files and the other from the declared role: those agree on both published configurations and disagree on a role declared with nothing behind it, which plans at returncode 0 and dies at STAR exit 104 |
| **the refusal** | there is none left to place. Every placement the `umi_tagged` kind can emit becomes runnable, so the helper's raise is **deleted** rather than relocated |

**The single-end form is the base case and the pairing is the addition.** Written the other way round
— a paired extractor with a reduced mode — every shared step acquires a second caller to stay correct
for, and the reduced mode is the one nobody runs.

## Why not a second verb

A second verb is justified by a second *operation*, and there is not one. It would duplicate the
geometry parse, the `--read-id` refusal that stops a rule wired to the plain mate, the bounded
reader, both truncation checks, the header and the read group — every line of which is about the
tagged read, which both cases have.

It also cannot be selected. A snakemake `shell:` block is a static string, so a module choosing
between two verbs must either render the whole command line through `params` — at which point the
verb boundary buys nothing the argument boundary did not — or split `umi_extract` into two rules
under a `ruleorder`, which is two DAG shapes for one pipeline.

## Why not a config key saying whether the plate is paired

`read_files_in` already states it: a placement carrying `cdna` has a mate and one that does not,
does not. A `paired:` key beside it is the same fact twice, which is the shape this tree keeps
deleting — the `smartseq3` backend block is empty for exactly this reason, and 0029 records
`Spec.decidable_by`, `RegistryEntry.fetchable` and `required_config` as three fields removed for it.

It is not free, either. The module reads the mate with `.get` rather than a subscript so that
`keys_read_by` does not add `read_files_in.cdna` to what compose owes **every** plate; a new key
would be owed by all of them, and the params gate — which refuses a key no owner declares — would
then have to be taught about the layout that has no mate. A change that widens what a module
*accepts* must not widen what it *demands*.

## Why the signature is not tuned to win a read-set contest

Declaring `read_sets` on a plate entry puts two candidates on one deposit, and the losing direction
is not symmetric with the bulk case. The generic entry's `se` set loses to every barcoded leaf
because that leaf's whitelist hits and the one-role set pays `λ/|R|` for the file it declined to
explain. **A plate has neither**: no onlist, because its cell barcode is the file, and with a
single-file deposit there is no orphan to charge. So the margin that decides every row of the shipped
comparison is unavailable here, and a near-tie is the expected result rather than a defect.

**A near-tie between read sets whose specs already declare a `confusable_with` edge routes to that
edge, and that is the designed outcome.** `smartseq3` ↔ `bulk-rnaseq` is declared
`processing_divergent`, `distinguishable_by: [metadata]`; a near-tie becomes a **Question** at exit 4,
which is recoverable. Reaching for the signature to win it outright is what must not happen: #257
measured that every additional R1 support on this entry is a strict liability — the motif already
saturates at 1.0 on every real cell, the trailing-`GGG` support goes *negative* on 4 of 10 published
cells, and dropping the draft's two extra supports roughly **doubled** the thin margins. Trading
measured per-cell margins for a synthetic contest margin is the wrong currency.

What is not tolerable, and is a stop condition rather than a caveat: the generic entry's `se` set
scoring *above* a plate's by more than the tie band. That is a bulk gene-count matrix for a plate
library at exit 0 — the plausible matrix, which `docs/agents/kb.md` ranks as the worst outcome
available, and strictly worse than the refusal the read set was added to remove.

## So in code

**A chemistry whose protocol publishes a single-end configuration gets a read set, and the module
behind it gets a wider extractor — never a second verb, a second module, or a pairing flag in the
config.** The mate is optional at every layer that touches it: nullable on the verb, `.get` in the
module, absent from `required_config`. When you add a branch on whether a mate exists, derive it from
`read_files_in` and from nothing else — that mapping is compose's placement and is already the single
statement of the fact. And before declaring a read set on an entry with no onlist, measure both
directions against the generic entry rather than reasoning about them: the orphan penalty that
protects the barcoded leaves does not protect a plate.

**Measure that contest at the depth the resolver scores a deposit at, and quote no margin without
it.** This one was taken twice and disagreed with itself: the `kb_probes` fixture hands a scorer a
truncated slice, at which the plate and the generic entry tie *exactly*, while on every read — what a
real deposit is scored on — the plate leads. The tie was written down as "structural" before the
second measurement existed, and it is not: it is the point where a saturating support and a
depth-sensitive one happen to cross, and the whole of the difference is one 25-mer collision. Both
readings support the same conclusion here, which is exactly why the disagreement was nearly missed.
**A gate over such a contest is therefore written one-sided** — *the generic entry must not win*
rather than *the specific entry must* — because that is the form that holds at both depths, where the
two-sided version passes on a deposit and fails on the fixture that scores it. The figures, the three
depths and the method are in
[`docs/research/smartseq3-single-end-configuration.md`](../research/smartseq3-single-end-configuration.md).

**Enforced by.** *The mate is an addition*, at the extractor:
`test_the_mate_changes_nothing_about_what_is_extracted_from_the_tagged_read` and
`test_a_single_end_plate_writes_one_unpaired_record_per_read` (`tests/test_workflows.py`) — the first
is this record's central claim as an assertion, the second pins the `0x4` shape that makes the uBAM
self-describing.

*The chain to a matrix*, which is the claim this record must not merely argue:
`test_a_composed_single_end_plate_runs_end_to_end_and_recovers_its_injected_counts`
(`tests/test_compose.py`) drives the composed pipeline's own rendered shell blocks through a real
STAR to an `.h5ad`, and closes the accounting — every read in reaches a matrix, a fate, or the UMI it
repeats. Its sibling `test_a_composed_plate_runs_end_to_end_at_small_n_and_recovers_its_injected_counts`
is the paired control.

*The universal*, over both placement shapes `umi_tagged` can emit:
`test_a_single_end_plate_deposit_compiles_end_to_end` (`tests/test_compose.py`) asserts the wiring
verdict, which *is* the DAG builder and so is the only assertion that speaks to the raise this record
deletes; the paired shape is held by `test_a_composed_plate_plans_every_rule_and_resolves_every_cells_wildcard`
under the same assertion; and `test_a_plate_the_dag_builder_cannot_plan_would_be_caught` proves that
gate is looking, because a verdict that has only ever been green proves nothing about a universal.

*One fact, both branches*: `test_the_extractors_mate_and_the_aligners_read_type_come_from_one_fact`
(`tests/test_workflows.py`), which fails against a module that derives `--r2` and `--readFilesType`
separately — the state that renders no mate and `SAM PE` plans at returncode 0 and dies at STAR exit
104. `test_the_plate_module_plans_a_single_end_run_and_hands_the_extractor_no_mate` holds the
rendering, and `test_the_plate_module_plans_a_whole_run_from_a_hand_written_config` holds that
`read_files_in.cdna` is the one config key the module does **not** demand — with
`test_the_params_gate_refuses_a_plate_wired_to_the_untagged_mate` (`tests/test_compose.py`) holding
the half that stays mandatory.

*The read-set contest*, one-sided by the rule above:
`test_the_plates_maximal_read_set_outranks_its_single_end_subset_on_a_paired_deposit` and
`test_the_generic_single_end_set_does_not_outrank_the_plates_on_a_single_end_plate_deposit`
(`tests/test_kb.py`), generalising
`test_the_single_end_set_does_not_outrank_a_single_cell_chemistry_on_its_own_data`, which is the
measurement this record extends and the shape both copy.

## Consequences

- **The `umi_tagged` role placement stops being guarded by absence.** It has emitted a mate-less
  placement since it was written, and the state was unreachable only because no spec declared a
  single-end plate read set. Declaring one is what unlocks it, which is why the spec edit and the
  extractor are one change and not two — shipping the first alone converts a recoverable refusal
  into a failure past handover.
- **The KB version bumps, so every dataset gets a new `run_id`.** The standing cost of a spec change
  (0029 pays it too), not a new one.
- **The uBAM becomes self-describing and the aligner invocation follows it.** `--readFilesType`
  moves from a module literal to a value derived per dataset. A `SAM PE` over unpaired records is a
  crash, so the derivation is load-bearing rather than cosmetic.
- **Nothing changes in the counter, and that is measured rather than read.** It already treats an
  unpaired record as its own fragment and falls back to that record's own footprint where a proper
  pair would have given it `TLEN`, so the agreement fixture that oracles it is untouched — but a
  no-op argued from the source is the shape this record spent its own Context section rejecting, so
  the run leg above is what actually holds it: a single-end plate through a real aligner into an
  `.h5ad` whose tagged reads land in the UMI matrix, whose internal reads land in the read matrix,
  and whose two sums close against the reads that went in.
- **A known gap, recorded rather than fixed here: `wiring_gate` discards snakemake's output**, so
  every wiring failure is an exit 3 with no reason — which is why this issue's own triage read the
  failure as exit 0. It is argued against every module rather than against this one, and is tracked
  outside this record.
- **A second known gap, adjacent and older: the verb takes one path per read**, while
  `ordered_fastqs` returns a list, so a cell topped up across two runs renders two paths after a
  one-value option and dies with a usage error at job time. `umite`'s own `-1` takes several. It is
  a distinct decision with its own design space and is tracked separately.
- **What is deliberately not recorded on the manifest.** Which read set won lands in the resolve
  artifacts, where *how this was decided* lives — 0029 settled that, and a plate changes nothing
  about it.

The terms this record turns on are defined once in [`CONTEXT.md`](../../CONTEXT.md): a **Tagged
read**, the **Internal read** that is its complement, and the **Read set** a spec declares.
