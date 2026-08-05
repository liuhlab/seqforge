# 36. A verb that needs a file list is handed the table that states it

Date: 2026-08-05

## Status

Accepted. Builds on [0027](0027-a-run-spans-its-lanes.md), which put `run` and `lane` in the **Units
table** so one sample's files order identically for every mate, and closes the arity narrowing
[0035](0035-the-mate-is-an-addition-to-umi-extraction.md) named without deciding.

## Context

`ordered_fastqs` returns a **list**. `io umi-extract` declared one `Path` per read, and
`star-umi.smk` rendered `--r1 {input.tagged}`. A sample carrying two files therefore expands to two
values after a one-value option:

```
$ seqforge io umi-extract --r1 a.fastq.gz b.fastq.gz --r2 c.fastq.gz ...
│ Got unexpected extra argument(s) (b.fastq.gz)                      exit 2
```

That sample shape is not exotic. A **Pre-demultiplexed** deposit says **Sample**, not file and not
run, and 20 of 190 well-labelled plate deposits are not strictly 1:1 — a cell topped up across two
runs is the ordinary form of it. The module's own comment already said so before the verb could take
one.

**The compose gate cannot see this, structurally.** `wiring_gate` is `snakemake -n -p`, and `-p`
*formats* every `shell:` block while planning — which is exactly why it catches a param a rule
dereferences and does not have. But formatting a command is not running one. A usage error on a
rendered string plans clean, and dies at job execution on a compute node, past handover.

**Its reachability is narrower than the failure suggests, and the narrowing is load-bearing.** With
no archive record the sample axis comes from filenames, so a cell sequenced across two runs never
rejoins: it is gated as two half-cells, which the exclusion record already discloses at the point of
loss rather than refusing. The break needs **records** — a plate deposit whose sample axis came from
an archive, where two runs rejoin under one `sample_id`. That is not a corner: it is what a corpus of
archived deposits is made of, and it is the half of the plate population that reaches us with its
identity intact.

**The obvious reading is to widen the option to a list and comma-join it**, the way the three other
mapping modules already hand STAR and chromap their per-role files. This record exists because that
reading is available from the same evidence and is the wrong one — those three comma-join because
STAR and chromap *demand* that encoding, not because seqforge chose it, and copying it into a verb we
own imports a constraint we do not have.

## Decision

**A verb that needs a sample's file list is handed the Units table and the sample id, never the
files.**

| | rule |
| --- | --- |
| **the argument** | `--units <units.tsv> --sample <cell>`. The verb resolves its own list through `ordered_fastqs` — the same function, over the same file, the module's `input:` is built from |
| **the pairing** | stated by `run` and `lane` ([0027](0027-a-run-spans-its-lanes.md)), never inferred from two lists that happen to sort in parallel |
| **the direct form** | repeated `--r1`/`--r2` survives for hand invocation and for a test that wants two paths and no table; mutually exclusive with `--units`, refused when both arrive and when neither does |
| **the mate** | still optional, still stated by absence ([0035](0035-the-mate-is-an-addition-to-umi-extraction.md)): no row carrying the mate role means no mate, and nothing new declares it |
| **the scale** | one argument per invocation, whatever the cell count and whatever the file count |
| **the bug class** | there is no file list on the command line, so there is no arity to get wrong — the gate's blindness stops *mattering* rather than being compensated for by a test that remembers to look |

## Why not a comma-joined list

It is the encoding three of four mapping modules already render, which is precisely the trap: they
render it because `--readFilesIn` and chromap's `-1/-2` parse it, and neither tool offers an
alternative. `io umi-extract` is ours. Adopting a third party's argument grammar for a verb we
define, on the evidence that we already emit it for third parties, is how a constraint spreads past
the boundary that imposed it.

It is also the weaker contract. A comma-joined value arrives as one `str` the verb splits by hand, so
typer's per-element `Path` conversion is gone, and a path containing a comma silently becomes two
paths that do not exist — a refusal naming files nobody passed. Every other place seqforge means "N
of these" spells it as a repeated option or a variadic argument, and none of them comma-joins.

## Why not a repeated option

`--r1 a.gz --r1 b.gz` fixes the arity, and it is the right *direct* form — which is why it survives.
It is the wrong form for the module, because it leaves the pairing inferred rather than stated.

Two lists reach the verb, each sorted independently by `(run, lane, path)`. They are parallel in
every deposit we have seen, and nothing makes them parallel. Pairing them by list index is therefore
an assumption about two sorts, and it needs a refusal of its own to notice when the assumption
breaks — the two lists must be the same length, checked before any bytes are read, or a run whose
mate file was never deposited shifts every pair after it. That refusal is the table's `run` and
`lane` columns, reconstructed from two lists that no longer carry them. The table simply says it.

The naive form of this is worse than an assumption, and worth writing down because it is the first
thing anyone tries: concatenate each role's files into one stream and pair record *n* with record
*n*. Given

```
R1 = [runA_R1.gz (100 records), runB_R1.gz  (50 records)]
R2 = [runA_R2.gz  (50 records), runB_R2.gz (100 records)]
```

the totals agree at 150, `zip_longest` yields no `None`, **no refusal fires**, and every record past
the fiftieth pairs run A's cDNA against run B's. Exit 0, plausible size, wrong cell. That is the
failure `workflows/units.py` was written to prevent — *"the mates hold equal read counts either way,
so the run completes and writes an artifact pairing one lane's barcodes with another lane's cDNA"* —
reappearing one level up, at the file boundary instead of the lane boundary, because the ordering
discipline was applied to two lists and then discarded at the argument.

## Why not a compose-side refusal

[0035](0035-the-mate-is-an-addition-to-umi-extraction.md) deleted a refusal from this exact path, on
the reasoning that *"every placement the `umi_tagged` kind can emit becomes runnable, so the helper's
raise is deleted rather than relocated"*. A refusal here puts one back a deposit-shape over, and
refuses a deposit that is not malformed — merely plural. It would also make `map/star-umi` the only
module that refuses a sample shape the other three compile, for a reason internal to our own verb
rather than to the assay.

It is cheap, and that is its whole case. It is the right answer only if a multi-run plate cell is a
shape we decline to compile, and the 20 of 190 says otherwise.

## Why not concatenating upstream

A rule that cats each role's files into one FASTQ per cell keeps the verb's arity at one, and pays
for it in the currency this tree has already been billed in. Written as an undeclared sibling it is
*"a file the rule never declared, in the tree whose undeclared temp files this project has already
paid 41 GiB to be rid of"*. Written correctly — a `temp()` output snakemake owns — it is the whole
plate again on disk, staged before extraction, for a concatenation the extractor performs for free by
reading two files in sequence.

## Why this is not two derivations of one fact

It reads like one, and `star-umi.smk` is emphatic that *"two derivations of one fact is how a module
comes to contradict itself for exactly one dataset shape"*. The module declares `input:` from
`ordered_fastqs`; the verb resolves its own list from `units.tsv`. Two readers, one fact.

They cannot disagree, because it is **one derivation used twice**: the same function, over the same
file, in the same run directory. What 0035's warning is actually about is deriving one branch from
the declared *role* and the other from the *files* — two different sources that agree on every
published configuration and part company on a role declared with nothing behind it. That hazard is
unchanged and still applies: `read_files_type` must keep deriving from the same per-sample mate list
the `--r2` argument comes from, and the arrival of `--units` neither helps nor excuses it.

The distinction is worth stating rather than assuming, because the next reader will meet an `io` verb
opening the workflow's table and reach for the rule that forbids it.

## So in code

**When a verb needs to know which files belong to a sample, hand it `--units` and `--sample` and let
it ask `ordered_fastqs`; do not render the paths into the command line.** A rendered path list is
what makes a rule's argument grammar a thing the wiring gate must catch, and the wiring gate cannot —
it formats `shell:` blocks and never runs them, so every arity, quoting and ordering fact in a
rendered command is unguarded by construction. A verb reading the table has no such surface. Where a
direct path form also exists, it is for a human and a unit test, and the two forms converge on one
resolution point immediately: one code path downstream, never two.

**Never re-derive placement.** `run`, `lane` and `read_id` are the **Units table**'s statement of
which file is which, and any second spelling of that — a filename parse, a list index, a sort assumed
parallel to another sort — is the copy that goes stale silently, at equal record counts, in a matrix
nobody can tell is wrong.

**Enforced by.** **None exists.** The gate arrives with the implementation and is owed by it: a test
at the **CLI boundary** that invokes the verb over a units table naming two files for one sample and
asserts the uBAM carries every fragment from both, plus one asserting that a mate row missing for one
run is refused by name rather than mispaired. Both must exercise the verb's own argument parsing —
the layer `wiring_gate` structurally cannot reach, and the layer that broke. Until they exist nothing
notices a regression here: `test_a_composed_plate_runs_end_to_end_at_small_n_and_recovers_its_injected_counts`
(`tests/test_compose.py`) drives the composed pipeline's real rendered shell blocks, but over a 1:1
plate, and a 1:1 plate is exactly the deposit shape that never had this defect.

## Consequences

`io umi-extract` gains a dependency on the **Units table**'s columns, which is a workflow concern
reaching an `io` verb. It is bounded and already precedented — `io umi-count` takes `sample_id=path`
per cell, which is the same table flattened — and it is paid for by deleting the argument grammar
that carried the defect.

**The scale ceiling moves from bounded to absent, for this verb.** `rule umi_extract` carries a
`{sample}` wildcard, so its list was never the plate — one cell's one or two files. The fan-in
`io umi-count` is where the plate really does reach one command line: at a measured 216 bytes per
`sample_id=path` and `ARG_MAX` of 2 MiB, that is 266 KiB at 1440 cells and a hard `E2BIG` at roughly
**9,700**. Nothing checks it and nothing needs to yet — 1440 is the figure the module's memory
arithmetic is written around, the failure past the ceiling is loud at job start rather than a wrong
matrix, and this record establishes the escape hatch that verb would take if a plate ever approaches
it.

The `--read-id` refusal becomes reachable-but-unreachable: the verb now selects the tagged read by
the role the geometry names, so a rule wired to the plain mate cannot occur rather than being caught.
Deleting a refusal 0035 treats as load-bearing is a separate decision and is **not** taken here; the
option stays, and the argument for removing it is owed its own record.

Nothing about the **Head**, the **Budget** or the bounded reader moves. The reader's own defect —
that an **Abandoned read** finalised after its caller closed the handle re-entered cleanup on a closed
file — is a property of R3's shared loop, decided against `probe` rather than against this caller,
and deliberately not recorded here.
