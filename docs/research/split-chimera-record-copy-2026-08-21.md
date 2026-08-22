# Copying a kept record instead of rebuilding it: about 5x at eight codec threads, about 2x at one

Measured 2026-08-21 for [#470](https://github.com/liuhlab/seqforge/issues/470). `split_chimera` —
one chimera-mapped BAM into one BAM per Component — spent **81.4% of its wall time rebuilding each
kept record field by field in Python**, and over half of that reading every aux tag back as
`(tag, value, type)` tuples only to re-declare it. There is no alignment, no index and no sort in
that rule: it is decompress -> route -> recompress, and the cost was in neither the routing nor the
codec. Replacing the rebuild with `copy.copy(record)` and a two-field reference-index patch is
**about 5x on this fixture at eight threads** — four batches measured 4.86, 4.95, 5.06 and 5.33, and
the second decimal place is not defensible on this machine — with byte-identical record payloads,
identical summary counts, peak memory up 2%, and peak disk unchanged.

**These are measurements, not decisions.** What they decided is in
[#470](https://github.com/liuhlab/seqforge/issues/470) and in the implementation commit on
`perf/470-split-record-copy` (`e5b62b5`). The production half of this page was measured on the
cluster and is cited rather than re-derived; the raw data is
`aging_SS3/script/metrics784/split_timing/` — `split_timing.tsv` (25 columns) and `collect.py`,
re-runnable now that all 784 cells have finished.

> **Laptop numbers are ratios, not budgets.** Every absolute below the production section comes from
> one desktop machine against a synthetic cell, warm page cache, warm local SSD. The ordering holds;
> the seconds do not travel, and the section on thread budgets says exactly which way the ratio
> itself moves.

## What it cost in production

Measured across **n = 238 cells** spanning all six nodes of the 784-cell worm chimeric plate
([#425](https://github.com/liuhlab/seqforge/issues/425)). Method: snakemake log timestamps matched
on `(shard, jobid)` — never adjacency, so concurrency cannot corrupt the pairing — with `records_in`
read from each cell's own `<sample>.split.json`.

| metric | median | IQR | range |
|---|---|---|---|
| records/sec | 51 480 | 45 620–54 710 | 26 860–92 470 |
| seconds/cell | 498 s (8.3 min) | 388–620 | 35–1094 |
| input MB/sec | 2.26 | 2.05–2.47 | 1.20–4.35 |

**Quote 19.45 s per million records, never a per-cell figure.** Duration is linear in records
(r = 0.920) with a 12.4 s intercept — no meaningful fixed overhead — so a flat minutes-per-cell
number is the wrong unit, and it is the thing a later reader is most likely to get wrong. A cell's
cost is its record count; the plate's median cell is around 25 million records, which is what a
median of 498 s at 19.45 s per million means.

**split : STAR on the same cells** — median **0.548**, IQR 0.369–0.711, aggregate **0.456**, max
1.18. About half of alignment at the median, and above alignment on individual cells.

## Where that time went, and what the cluster ruled out

Cost decomposition on an idle node, 4M records, production thread shape:

| phase | seconds | share of wall |
|---|---|---|
| BGZF decompress + iterate | 2.99 | 6.5% |
| **record rebuild** | **37.68** | **81.4%** |
| — of which the aux-tag round-trip | 25.37 | 54.8% |
| BGZF compress + write | 5.64 | 12.2% |

BGZF is ~56% of raw CPU when serialised but only 12% of *wall*, because it is already threaded and
overlaps behind the serial Python loop. The lever, measured on the cluster rather than proposed:

| implementation | wall (4M records) | throughput | speedup |
|---|---|---|---|
| current, field by field | 46.72 s | 85 617 rec/s | — |
| `copy(record)` + tid patch | **15.50 s** | **258 044 rec/s** | **3.01x** |

Emitted SAM text byte-identical over 4M records.

**Node contention is not inflating the production figures.**
`r(concurrent_threads, rec_per_s) = 0.016`, flat against load. Production runs ~40% slower than an
idle node (51.5k against 85k rec/s), but that is a constant of co-tenancy rather than a function of
how much of it there is. **The filesystem is not it either**: writing to `/share` rather than `/tmp`
cost 5%.

### The negative result: `wb0` is both slower and 7.4x larger

The split's temp BAMs are `temp()`, written at the default level and consumed immediately by
`unique_to_cram`, which looked like paying full compression for no reader. It is not. Level 0 took
**53.4 s against 46.3 s** for level 6, because output grew **7.4x** — 1317 MB against 178 MB per 4M
records, roughly 6 TB of extra traffic across the plate — for a *slower* pipeline. **Do not re-run
this sweep.** It is recorded here so the idea, which is a natural one, stops costing anybody a
cluster session. What the fix does change is the level 1 question, which was never measured at all
and is re-swept further down against the new implementation.

## The before/after, on the shipped verb

Intel Core i7-10700K @ 3.80 GHz, 8 physical / 16 logical cores, 32 GB RAM, macOS 26.5.2, Python
3.13.14, pysam 0.24.0 / htslib 1.23.1 (bundled). One synthetic chimeric cell: **1 000 006 records,
86 321 630 bytes**, two Components, 100 bp reads, every placed record carrying all ten aux tags
`NH HI AS nM NM MD RG UB jM:B:c jI:B:i`.

**This is the real code path, not a copy of it.** `after` is `seqforge.workflows.split` imported
from the tree; `before` is `git show main:src/seqforge/workflows/split.py` loaded as a module with
four relative imports rewritten to absolute ones and nothing else touched, which a diff against the
same `git show` confirms. The clock brackets the `split_chimera` call, so it includes the two
end-of-run checks and the summary write as well as the record loop. The two arms are interleaved rep
by rep rather than run in blocks, so a slow patch of the machine hits both. `threads=8`, level 6,
median of n = 3 after a discarded warm-up.

| arm | wall | spread | records/s | CPU cores | peak RSS |
|---|---|---|---|---|---|
| `before` — `main`, field by field | 19.423 s | 18.612–19.436 | 51 487 | 1.63 | 96.7 MiB |
| **`after` — this branch, copied** | **3.836 s** | 3.747–3.938 | **260 661** | **4.04** | **98.7 MiB** |

**5.06x on this batch.** Peak memory moves 2.0%. Run-to-run spread is 4.3% on the slow arm and 5.0%
on the fast one, which is the band every ratio on this page has to be read through.

**The summary counts are identical between the arms, and stable across every rep.** Not a subset of
the counts: the whole payload compares equal, and the two `cell.split.json` files are the same
612 bytes on disk. That is the ticket's *unchanged summary counts* floor checked on a million
records rather than only on the repo's 38-record fixture, where a counter that lost one record in
ten thousand would not have shown up. The record payload hashes on both shipped arms also equal the
vendored harness's for `rebuild`, `copy` and `inplace` — **one payload across all five ways this
change was run**.

The laptop and the cluster agree on cost per record more closely than they have any right to:
19.42 s per million here against a production median of 19.45, and 3.84 s per million after the
change against the cluster's 3.88. The machines differ; the shape of the work does not.

## The vendored harness, and how the two reconcile

Everything below this section — the phase decomposition, the level sweep, the thread sweep — was
measured with a standalone copy of the verb's loop rather than the verb, because the phases need the
loop cut at four points and the arms need a `rebuild` that no longer exists in the tree. Its three
arms differ only in how a kept record reaches the writer; same input, `threads=8`, level 6, median
of n = 3 after a discarded warm-up, each in its own process.

| implementation | wall | records/s | | CPU cores | peak RSS |
|---|---|---|---|---|---|
| `rebuild` — `main`'s `_rewritten`, field by field | 18.328 s | 54 562 | — | 1.64 | 95.9 MiB |
| **`copy` — `copy.copy` + index patch, shipped** | **3.771 s** | **265 191** | **4.86x** | **4.06** | **98.1 MiB** |
| `inplace` — patch the source record, no copy | 3.810 s | 262 443 | 4.81x | 3.91 | 98.2 MiB |

A fourth batch ran all four configurations interleaved — both harnesses, both arms — which is the
only way to price what the verb does *besides* the record loop:

| | vendored loop | shipped verb | difference |
|---|---|---|---|
| before | 19.064 s | 18.151 s | −0.914 s (−4.8%) |
| after | 3.577 s | 3.664 s | **+0.087 s (+2.4%)** |
| ratio | **5.33x** | **4.95x** | |

**On the fast arm the verb's overhead is real, small and fixed**: +0.087 s, positive on every rep,
and mechanically bounded — both end-of-run checks are O(number of outputs) rather than of records,
and `_write_summary` writes 612 bytes. **On the slow arm the medians say the shipped verb is 4.8%
*faster*, and that is noise rather than a finding**: the per-rep differences there are inconsistent
in sign, against within-configuration spreads of 5.2% and 6.6%. A fixed 0.09 s is 0.5% of a 19 s
wall; nothing in that batch can resolve it. The shipped verb is not faster than the harness, and
this page does not claim it is.

**The ratio across every batch that measured one: 4.86x, 4.95x, 5.06x, 5.33x.** The honest figure is
**about 5x**, and the spread is not a disagreement between the two harnesses — it is one arm's ~5%
run-to-run noise divided by the other's. Every ratio quoted below carries that band whether or not
it is repeated.

## Every ratio for this change needs its thread budget

The same two implementations, the same input, the codec pool cut to one:

| threads | `rebuild` | `copy` | ratio |
|---|---|---|---|
| 8 | 18.328 s | 3.771 s | **4.86x** (the batch band is 4.86–5.33) |
| 1 | 29.753 s | 14.064 s | **2.12x** (one batch only) |
| cluster, 4M records, production shape | 46.72 s | 15.50 s | **3.01x** |

**All three are the same finding.** The change deletes serial Python work and nothing else; what is
left after it is compression, and how fast *that* residue runs is a function of how many codec
threads it gets and how contended they are. At eight threads the codec keeps up and the ratio
approaches what the loop alone would give; at one thread compression is serial and dominates the
remainder, halving the win. So **a figure quoted for this fix without its thread budget is not a
figure** — about 5x, 3.01x and about 2x are one result read at three points, not three results. The
gap between the three is far larger than the ~5% run-to-run band, which is why the thread budget is
the thing that has to travel with the number and the second decimal place is not.

## Where the wall time goes now

Same harness, four cumulative phases: `iter` decompresses and materialises every record and does
nothing else; `route` adds the keep rule, the routing and every summary counter; `work` adds
building the record that would be written, then drops it; `full` hands it to the writer. Each bucket
is the marginal wall time the next stage adds. Writers and their codec threads exist only in `full`,
so `full − work` is exactly compress + write. Timing starts at `AlignmentFile` open.

**These buckets are additive in WALL terms only.** A slower consumer lets the reader's own thread
pool run further ahead, so a bucket is what that stage adds to the wall of this pipeline, not the
isolated cost of the work in it. That is the right unit for deciding where to spend effort and the
wrong one for costing a function.

| bucket | `copy` | share | `rebuild` | share |
|---|---|---|---|---|
| decompress + iterate | 0.430 s | 12.1% | 0.417 s | 2.2% |
| routing, keep rule, counters | 1.184 s | 33.4% | 1.115 s | 5.9% |
| record work | 0.760 s | 21.4% | 16.164 s | 85.5% |
| compress + write | 1.175 s | 33.1% | 1.211 s | 6.4% |
| **total** | **3.549 s** | | **18.907 s** | |

Collapsed to the three buckets the cluster used:

| | cluster, 4M, idle node | `rebuild` here | `copy` here |
|---|---|---|---|
| decompress + iterate | 6.5% | 2.2% | 12.1% |
| the serial record loop | 81.4% | 91.4% | 54.8% |
| compress + write | 12.2% | 6.4% | 33.1% |

The middle column reproduces the cluster's shape on a different machine against a synthetic input,
which is the check that mattered before spending anything on the lever. The right-hand column is
what the rule looks like afterwards: no single bucket above a third, and the largest is now the
codec.

### The tag round-trip, priced on its own

An arm identical to `rebuild` with the `set_tags(get_tags(with_value_type=True))` line deleted — its
output is wrong by construction and it exists only to price that line — costs **13.041 s less** in
the `work` phase. That is **69.0% of the old implementation's whole wall** and **14.21 µs per kept
record**, against the cluster's 54.8%.

Not a contradiction: a fixture effect. Ten tags per record, two of them arrays, is a heavier tag
load per record than the plate's mean, so the round-trip's share here is larger. Same conclusion,
drawn sharper — the single line the ticket named was the majority of the rule.

## The BGZF level, re-swept on the new implementation

The cluster's level-0 verdict was taken against an implementation that no longer exists, and level 1
was never measured. Swept here on `copy`, `threads=8`:

| level | wall | output bytes | CPU cores |
|---|---|---|---|
| 1 | **2.816 s** | 92 294 287 | 2.35 |
| 6 (default, shipped) | 3.614 s | **79 570 937** | 4.09 |

Level 1 is **22.1% faster, 16.0% larger and 42.4% cheaper in CPU**.

**Level 1 wins on wall time. It is declined on the disk envelope, which is the other half of what
the ticket asked.** Saying it lost would be false, and discounting its 22% against the wall this
change had already deleted would be worse — a win is not smaller because something else got faster
first. What decides it is what the 16% costs where the artifact actually lands, and what the 22%
buys at the stage the pipeline is now.

### What the 16% costs, at production cell size rather than fixture size

The fixture's plate arithmetic is not the number to spend against. A production cell, from the three
production medians: 498 s at 51 480 records/s is **~25.6M records** — taking the 12.4 s intercept
off first and dividing by 19.45 instead gives 25.0M — and 498 s at 2.26 input MB/s is **~1 125 MB of
chimeric BAM per cell**. At the measured 92.2% output-to-input ratio that is **~1 038 MB of split
BAM per cell**, and across 784 cells **~810 GB per plate**. Level 1 would add 16.0% of that:
**~130 GB per plate.**

Every assumption in that chain, stated: the three medians are medians of different distributions, so
their product is a typical cell rather than any real one; the 92.2% ratio is a *fixture*
measurement, and it holds up mostly because it is close to the kept-record fraction (91.75%) rather
than to anything about compressibility; and the fixture's sequence is random, so it is close to
incompressible at **86.3 bytes per record against production's 43.9** (2.26 MB/s over
51 480 records/s). That last one is why the **+16.0% inflation itself** is the weakest link here —
it was measured on near-incompressible bytes, and how it scales to real reads is not measured
anywhere on this page. 130 GB is an estimate to re-take on a real cell before anyone spends it, not
a number to treat as settled.

### What the 22% buys, at the stage the pipeline is now

**The split is no longer the pipeline's constraint.** At the cluster's own 3.01x it moves from a
median 0.548x STAR to about **0.18x**. Another 22% off a rule running at 0.18x is about **4% of that
stage** — and the envelope authorises spending disk for a *significant* measured reduction in wall
time. At the level the pipeline is actually measured, this is not one. Before the record copy it
might have been; that is exactly the point.

### The CPU figure argues the other way, and this decision should be revisited

Level 1 finishes faster on **2.35 cores where level 6 needs 4.09** — 42.4% less CPU for the same
cell. That is the strongest thing arguing for it, and it is recorded rather than dismissed: **where
cells-per-node rather than disk is the binding constraint, the trade reverses**, because then the
42% is the resource being bought and the 16% is being paid in the one that is not scarce. Both
numbers a later ticket needs are on this page — the sweep table above and the per-plate arithmetic
beside it — and neither needs a cluster session to re-derive.

## Peak disk, per cell and per plate

**The fix does not move peak disk at all.** Every arm — `rebuild`, `copy`, `inplace` — writes the
same 79 570 937 bytes for this cell, 92.2% of the 86 321 630-byte input. Only the codec level moves
this number; the record path cannot, because the records are the same bytes.

**At fixture scale**, across 784 cells of this size, that is 62.4 GB per plate against level 1's
72.4 GB, **+9.98 GB**. That pair is the 1M-record synthetic plate and nothing else — it is *not* a
production figure, and the production estimate is the ~810 GB and ~130 GB derived above from the
plate's own medians. The cluster's decomposition cell was 4M records, four times this fixture; the
production median cell is around twenty-five times it.

## Mutating the source record instead of copying it buys nothing

`inplace` patches the source record's two reference indexes and hands it straight to the writer,
with no copy anywhere. It is **3.810 s against `copy`'s 3.771**, inside the batch spread — a
difference of 1%, and in the phase batches the two come out at 3.543 and 3.549.

The phases say why, and the answer is not that the copy is free:

| bucket | `copy` | `inplace` |
|---|---|---|
| decompress, iterate, route and count (through `route`) | 1.614 s | 1.533 s |
| record work (`work − route`) | 0.760 s | 0.257 s |
| compress + write (`full − work`) | 1.175 s | 1.753 s |
| **total** | **3.549 s** | **3.543 s** |

`inplace`'s build bucket is 0.503 s smaller, so `copy.copy` genuinely costs about **0.55 µs per kept
record** — and its write bucket is 0.579 s larger, almost exactly the same amount back. At eight
threads the writer is the next constraint, so the copy is absorbed for free by the pipeline behind
it. Correctness is not the argument either way: `inplace` emitted the same bytes on both hashes.
**There is simply no throughput case for mutating the source record**, which is why the shipped
implementation copies and leaves the record the loop keeps reading after the write exactly as the
reader handed it over.

## Byte identity

Two independent hashes per Component, because they fail differently:

- **binary** — the BAM's uncompressed byte stream with the header sliced off by parsing
  `magic / l_text / text / n_ref / refs`. Every record, every field, every tag at its declared
  width, before anything renders it. A header difference cannot mask a record change because the
  header is not in the hash.
- **rendered** — per record, `to_string()` plus `repr(get_tags(with_value_type=True))`, hashed
  cumulatively. Carries each scalar tag's declared type letter and each array tag's typecode, which
  is precisely what a re-declaration from a value would narrow or widen.

**`rebuild` = `copy` = `inplace` on both hashes, on both Components** — 825 780 and 91 762 records.
Nothing diverged on any record shape in the fixture: multiply-placed records, spliced records
carrying real `jM`/`jI` payloads, singletons, unpaired records, and records whose mate pointer is
unset. **The shipped verb's two arms produce those same two hashes**, so there is one record payload
across all five ways this change was run, and the summary counts beside it compare equal whole.

The identity arms' *headers* differ, and only in one field: the output path is embedded in the `@PG`
`CL` line, and the three arms wrote to three different directories. That is visible in the file
sizes — the two arms whose directory names are the same length produce files of the same size and
the shorter-named one is a byte smaller — and it is why the check slices the header off. Written to
the same path, as in every timing run, all three arms produce files of identical size to the byte
(71 631 582 and 7 939 355), so whole files including headers agree.

## What the envelope did not need to buy

The ticket granted higher peak memory and a moderate increase in peak disk in exchange for measured
time, and explicitly permitted parallelising the routing loop under that envelope. **Neither was
spent.** Peak RSS moved 2.0% on the shipped verb and 2.3% on the vendored harness; peak disk did not
move at all; the routing loop is still one serial Python pass.

That is a decision and not an omission, and the number behind it is the phase table. After the fix,
the serial Python routing work — the keep rule, the counters, the dictionary lookups — is 33.4% of a
3.5 s wall, and compression is another third. **Parallelising the loop now contends with the codec
rather than with idle cores**: at eight threads the process already draws 4.06 cores, so the work
handed to new workers would come out of the pool the writers are using. The record-copy lever
captured essentially all of the available win, and stopping there is the outcome the ticket said
would be a good one.

## A thread sweep, for the `threads:` question

[#452](https://github.com/liuhlab/seqforge/issues/452) is the axis this belongs to and
[#475](https://github.com/liuhlab/seqforge/issues/475) is the ticket that takes the `threads:`
question up on the evidence below; [#470](https://github.com/liuhlab/seqforge/issues/470) put it out
of scope here, so this section hands over evidence and a mechanism and stops there. **It decides
nothing about what is declared.**

`copy` throughout, BGZF level 6 pinned explicitly rather than left to the default, median of n = 3
after a discarded warm-up. Batch A holds all four of its points, so no drift can separate them. The
wiring columns are what the verb actually asks for rather than a guess: `per_writer` is
`max(1, threads // len(outputs))`, and a pysam handle opened with `t` threads gets `t - 1` codec
workers.

| threads | per writer | codec workers, dominant writer | total codec workers | wall | spread | records/s | CPU cores |
|---|---|---|---|---|---|---|---|
| 1 | 1 | 0 | 0 | 13.864 s | 13.756–13.990 | 72 129 | 1.05 |
| 2 | 1 | 0 | 1 | 13.394 s | 13.375–13.408 | 74 658 | 1.08 |
| 4 | 2 | 1 | 5 | 10.566 s | 10.535–10.576 | 94 640 | 1.39 |
| 8 | 4 | 3 | 13 | **3.641 s** | 3.630–3.668 | **274 663** | 4.07 |

Batch B is its own batch: it repeats 4 and 8 as anchors and adds 16. **4** -> 10.564 s, 94 661/s,
1.38 cores; **8** -> 3.668 s, 272 615/s, 4.08; **16** -> **2.845 s**, 351 531/s, 5.32 cores, on
`per_writer` 8 and 29 total codec workers. The anchors drift −0.02% at 4 and +0.75% at 8 against
batch A, tight enough to read the two batches together.

Spread within a batch runs 0.25–1.7% across batch A and 0.27–3.1% at batch B's anchors. **Batch B's
16 is the loosest point on this page at 6.8%** — one of its three runs came in at 3.026 s against a
2.832 s best — which is worth knowing before quoting its margin to three digits.

**Both points this document had already reported were re-measured here rather than reused.**
`threads=8` comes back at 4.07 cores against the 4.06 of the earlier batch, and the `threads=1` wall
at 13.864 s against 14.064 — 1.4% apart, inside the ~6% between-batch drift the method section
documents.

### Utilisation has no knee, because it is not the controlling variable

Total CPU is flat across the whole sweep — **14.53, 14.54, 14.63, 14.86 core-seconds** at 1, 2, 4
and 8, and 15.13 at 16. A 16x change in the reservation moves the work done by 4%. Utilisation is
therefore that fixed work divided by the wall, and cores per declared thread goes **1.05, 0.54,
0.35, 0.51, 0.33**: non-monotonic, with a trough at 4 and a bump at 8 that no property of the rule
explains.

**A `threads:` figure picked off a utilisation curve would be picked off an artifact.** That needs
saying because it is exactly how the question reached us — *the rule reserves eight cores and
converts them into 2.4* is a true sentence about a ratio whose denominator is a declaration and
whose numerator is a wall clock, and it moves when either end moves for reasons that have nothing to
do with whether the reservation is right.

### The number that decides it is the wall-time gain per doubling

| doubling | wall gain |
|---|---|
| 1 -> 2 | 0.47 s |
| 2 -> 4 | 2.83 s |
| 4 -> 8 | **6.93 s** |
| 8 -> 16 | 0.82 s |

**The largest gain in the sweep is 4 -> 8.** Declaring 4 would leave 6.93 s per cell on the table,
1.9x the entire post-fix wall at 8. Declaring 16 buys 0.82 s within batch B — 0.80 s if batch A's
anchor is used instead — for double the reservation.

### Two mechanisms, and the low end cannot be read without both

**The per-writer floor.** The verb divides its budget evenly across the outputs,
`max(1, threads // len(outputs))`, which with two Components floors to 1 at `threads=1` *and* at
`threads=2`.

**`t` threads means `t - 1` workers.** pysam 0.24.0's `libchtslib.pyx:530` computes
`threads = self.threads - 1` and hands that to `hts_set_threads`, so a writer handed `per_writer=1`
gets **zero** worker threads and compresses on the calling thread.

Together: at `threads=1` the process has no codec worker anywhere, and at `threads=2` the only thing
that changes is the reader gaining one. That is the whole of the 1 -> 2 segment, and its 0.47 s is a
reader thread rather than a slope. **Two points that differ only in the reader's worker count are
not the start of a curve.**

### The binding term is the codec workers on the dominant component's writer

That term is `max(1, N // len(outputs)) - 1`. The fixture splits **90/10 by output bytes** — ce11
71.6 MB against ecoli 7.9 MB — while `per_writer` divides evenly regardless of load, so the ce11
writer does 90.0% of the compression on the same worker count as the writer doing 10%.

Two constants, both measured: the serial loop is **2.374 s** (the `work` phase above), and total
compression is **11.490 s** (the `threads=1` wall minus that loop), of which ce11's share is
**10.34 s**. The two come from different batches, so how the sum divides between them carries the
usual drift while the sum itself, being one measured wall, does not. Then

    dominant writer has no worker:  wall ~= loop + total compression
    otherwise:                      wall ~= max(loop, 10.34 s / dominant workers)

| N | dominant workers | predicted | observed | residual |
|---|---|---|---|---|
| 1 | 0 | 13.864 s | 13.864 s | *calibration* |
| 2 | 0 | 13.864 s | 13.394 s | +0.470 (+3.5%) |
| 4 | 1 | 10.344 s | 10.566 s | −0.223 (−2.1%) |
| 8 | 3 | 3.448 s | 3.641 s | −0.193 (−5.3%) |
| 16 | 7 | 2.374 s | 2.845 s | −0.471 (−16.5%) |

`N=1` is where both constants come from, so its residual is zero by construction and the other four
points are predictions. Within 5.3% across the 1 -> 8 range where the decision lives; the +3.5% at
`N=2` is the reader's worker, which the model does not carry; the −16.5% at 16 is a wall that is no
longer compression at all. **A model that fits five points is the transferable thing here** — the
five points themselves belong to this fixture.

### Three consequences

**The flattening at 16 is the serial Python routing loop, not the codec.** At `N=16` the dominant
component's compression comes to ~1.5 s, below the 2.374 s loop floor, and **83% of the 2.845 s wall
is the loop**. Above 8, threads buy almost nothing until the routing loop is parallelised — the
lever this ticket declined and priced above. That is the reason to stop at 8, and it is stronger
than any utilisation ratio because it names what the next thread would be waiting for.

**8 is a fact about this component shape, not a constant.** The knee sits where the dominant
component's byte share divided by its writer's worker count crosses the loop floor: a cell whose
bacterial fraction is nearer even flattens earlier, a more lopsided one later. That exposes a
candidate this document does not recommend and only records — giving each writer a budget
proportional to its output load rather than an even division would remove the shape dependency,
since the even split is exactly what makes the ce11 writer carry 90% of the bytes on half the
workers. [#476](https://github.com/liuhlab/seqforge/issues/476) is where that observation went; it
is still an observation, and the ticket existing does not make it a recommendation from here.

**`N=16` is hyperthread-limited here.** This box is 8 physical / 16 logical cores, so its 29 codec
workers at that point are not 29 cores, and 16 might do better on a node with sixteen physical ones.
It does not change the conclusion, because what caps `N=16` is a serial Python figure and not a core
count.

### The caveats this section carries, because it is the one that will be cited

Warm local SSD, warm page cache, one 1M-record synthetic cell, random SEQ. On a cold or networked
filesystem the decompress share grows and the loop floor is reached sooner, so **the knee moves
down**: 8 is an upper bound this shape justifies rather than a central estimate. The compression
level moves it too — the sweep is level 6, and the level-1 point measured above draws 2.35 cores at
eight threads.

And the before/after that opened the question is one point on this curve: **1.64 cores at
`threads=8` under the rebuild, 4.07 under the copy.** The arithmetic that made the rule look 4x
over-reserved was taken against an implementation that no longer exists. What is over-reserved and
by how much now depends on the compression level and on the Components' byte shares, which is the
coupling that leaves the question to [#475](https://github.com/liuhlab/seqforge/issues/475) rather
than to this page.

**That utilisation figure is re-derivable from the tree, not only from the harness.** The shipped
verb measures 1.63 and 1.65 cores on its two `before` batches and 4.04 and 4.02 on its two `after`
batches, against the vendored 1.64 and 4.06–4.07 — so whoever takes the declaration can re-run it
against `seqforge.workflows.split` itself rather than trusting a copy of the loop.

## Method

**Machine.** One desktop: Intel Core i7-10700K @ 3.80 GHz, 8 physical / 16 logical cores, 32 GB RAM,
macOS 26.5.2 (25F84), Python 3.13.14 (conda-forge), pysam 0.24.0 against htslib 1.23.1. Warm local
SSD, warm page cache, no other load. Memory is `ru_maxrss` per process, whose unit was verified as
bytes on this platform by touching a known allocation.

**Input.** One synthetic coordinate-sorted chimeric BAM, seed 20260821: 1 000 006 records,
86 321 630 bytes, two Components — `ce11` at its seven real chromosome lengths and `ecoli` as
`NC_000913.3` — every `@SQ` name suffixed the way the chimera builder spells one, and a `@PG` chain
of STAR then `samtools sort`. Reads are 100 bp with a 300 bp mate gap; every placed record carries
`NH HI AS nM NM MD RG UB` plus `jM:B:c` and `jI:B:i`, so a tag round-trip meets two arrays as well
as six scalars. The mix is a 100-locus cycle chosen so the splitter's two end-of-run checks close
exactly (excess pointers 0, unanswered survivors 0): 917 542 kept records (825 780 worm, 91 762
bacterial), 51 540 unmapped, 20 616 secondary, 10 308 supplementary, and within the kept population
134 004 multiply placed, 97 939 spliced with real junction payloads, 36 078 singletons, 15 462
unpaired and **25 770 whose mate pointer is unset** — the record shape the copy path has to leave
alone rather than resolve. Written as SAM text and sorted with `samtools sort`, because the sort is
what puts the placeless records where a coordinate-sorted BAM has them.

**The shipped-verb arms** call `split_chimera` itself. `after` imports `seqforge.workflows.split`
from the tree at this branch's HEAD; `before` is `git show main:src/seqforge/workflows/split.py`
written to a scratch module with its four relative imports rewritten to absolute ones — `..` to
`seqforge`, `.` to `seqforge.workflows`, and both `.metrics` imports to `seqforge.workflows.metrics`
— and piping the same `git show` through `diff -` confirms those four lines are the only difference.
The repo's working tree is never touched and no worktree is created. pysam is imported before the
clock starts, matching the vendored harness, and the clock brackets the `split_chimera` call alone,
so the two end-of-run checks and the summary write are inside it. Two batches: one with the two arms
interleaved rep by rep, one with all four configurations — both harnesses, both arms — interleaved
the same way, so drift cannot separate a pair. The summary payload is compared whole between arms
and across every rep, not field by field against a list someone chose.

**Harness.** Every other arm is a standalone copy of `split_chimera`'s loop — same keep rule, same
discard categories, same counters, same refusals, same writers — differing only in the maker that
turns a source record into the record handed to the writer: `main`'s `_rewritten` copied verbatim
(`rebuild`), `copy.copy` plus the guarded two-field index patch (`copy`), the same patch applied to
the source record with no copy (`inplace`), and `rebuild` with the tag round-trip deleted (`notags`,
which is not a candidate). The header helpers are vendored from the module at the commit this branch
started from, so the baseline arm is pinned while the module itself is edited. One configuration per
process, because `ru_maxrss` is a process high-water mark; a driver runs each configuration four
times and reports the median of the last three.

**The thread sweep** reuses that driver and that harness unchanged, and the same `cell.bam`, over
two batches: A at 1, 2, 4 and 8, B at 4, 8 and 16 with the first two as anchors. The only difference
from every other run on this page is that the BGZF level is passed as 6 explicitly rather than left
to htslib's default, so a change in that default could not be read as a thread effect. The wiring
columns — `per_writer`, workers per writer, total codec workers — are computed from the verb's own
two expressions rather than observed, and the reader is `threads - 1` while each writer is
`per_writer - 1`.

**Batches.** Between-batch drift (~6%) exceeds within-batch spread (<3%, and under 1.7% everywhere
except batch B's 16), so every comparison above is drawn from a single batch. Four subtractions
cross a batch boundary and all four are stated: the `inplace` phase buckets are compared against
`copy`'s, and the two batches' `full` runs agree to 0.2%, which is what licenses it; the tag
round-trip's 13.04 s has no same-batch counterpart, so read it as ±6% — the conclusion, that the
round-trip is about two thirds of the old wall, survives that comfortably; the 8 -> 16 gain is
quoted within batch B at 0.82 s, with batch A's anchor giving 0.80; and the thread model's two
constants are split by subtracting the phases batch's `work` from batch A's `threads=1` wall, so the
split between loop and compression carries the drift even though their sum, being one measured wall,
does not.

**Identity.** Three arms written with `--keep`, then hashed both ways described above, with a
first-difference reporter that names the record and the differing SAM columns had anything diverged.
Headers were compared separately and are deliberately outside both hashes.

Both harnesses live in a scratch directory and are **not committed** — one pins a vendored copy of a
module that is being edited and the other writes `main`'s version of that module to disk, which are
both exactly the thing that should not sit in the tree. Everything needed to rebuild them is above,
and the shipped-verb arms need nothing but the tree and one `git show`.

## What this does not cover

- **A cold or networked filesystem.** Warm page cache and local SSD make the iterate bucket a floor.
  On a cold read the decompress share is larger and the ratio is smaller — which is one more reason
  the cluster's 3.01x and this machine's ~5x are the same finding at two operating points.
- **The real cluster tag load.** Ten tags with two arrays is a heavier per-record load than the
  plate's mean, which is why the tag round-trip prices at 69% here against 54.8% there.
- **Whether a C tool should do this at all.** The Python loop also computes every counter in the
  summary, so replacing it is a decision about where that accounting lives, and it is its own
  ticket.
- **Sharded execution** ([#448](https://github.com/liuhlab/seqforge/issues/448)). The split
  lengthens that tail; nothing here changes sharding, and nothing here measures what it costs. The
  split BAM's footprint is quoted per plate rather than waved off as transient because the artifact
  stands as long as the run that produced it needs it, a downstream per-Component rule being its
  consumer — not because of anything this page knows about retention.
- **Anything about what `threads:` should declare.** The sweep above is an input to
  [#475](https://github.com/liuhlab/seqforge/issues/475) on the axis
  [#452](https://github.com/liuhlab/seqforge/issues/452) owns, and to
  [#476](https://github.com/liuhlab/seqforge/issues/476) for the writer-budget observation — not a
  conclusion about any of them.
