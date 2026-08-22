# Copying a kept record instead of rebuilding it: 4.86x at eight threads, 2.12x at one

Measured 2026-08-21 for [#470](https://github.com/liuhlab/seqforge/issues/470). `split_chimera` —
one chimera-mapped BAM into one BAM per Component — spent **81.4% of its wall time rebuilding each
kept record field by field in Python**, and over half of that reading every aux tag back as
`(tag, value, type)` tuples only to re-declare it. There is no alignment, no index and no sort in
that rule: it is decompress -> route -> recompress, and the cost was in neither the routing nor the
codec. Replacing the rebuild with `copy.copy(record)` and a two-field reference-index patch is
**4.86x on this fixture at eight threads**, with byte-identical record payloads, peak memory up 2.3%
and peak disk unchanged.

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
how much of it there is. **The filesystem is not it either**: writing to `/share` rather than
`/tmp` cost 5%.

### The negative result: `wb0` is both slower and 7.4x larger

The split's temp BAMs are `temp()`, written at the default level and consumed immediately by
`unique_to_cram`, which looked like paying full compression for no reader. It is not. Level 0 took
**53.4 s against 46.3 s** for level 6, because output grew **7.4x** — 1317 MB against 178 MB per 4M
records, roughly 6 TB of extra traffic across the plate — for a *slower* pipeline. **Do not re-run
this sweep.** It is recorded here so the idea, which is a natural one, stops costing anybody a
cluster session. What the fix does change is the level 1 question, which was never measured at all
and is re-swept further down against the new implementation.

## The same lever, measured here

Intel Core i7-10700K @ 3.80 GHz, 8 physical / 16 logical cores, 32 GB RAM, macOS 26.5.2, Python
3.13.14, pysam 0.24.0 / htslib 1.23.1 (bundled). One synthetic chimeric cell: **1 000 006 records,
86 321 630 bytes**, two Components, 100 bp reads, every placed record carrying all ten aux tags
`NH HI AS nM NM MD RG UB jM:B:c jI:B:i`. Three arms differing only in how a kept record reaches the
writer; `threads=8`, BGZF level 6, median of n = 3 after a discarded warm-up, each run in its own
process.

| implementation | wall | records/s | | CPU cores | peak RSS |
|---|---|---|---|---|---|
| `rebuild` — the repo's `_rewritten`, field by field | 18.328 s | 54 562 | — | 1.64 | 95.9 MiB |
| **`copy` — `copy.copy` + index patch, shipped** | **3.771 s** | **265 191** | **4.86x** | **4.06** | **98.1 MiB** |
| `inplace` — patch the source record, no copy | 3.810 s | 262 443 | 4.81x | 3.91 | 98.2 MiB |

Within-batch spread is 2.1–3.5% of the median across the three. **Peak memory moves 2.3%** — 95.9 to
98.1 MiB — which is the whole of what a `copy.copy` per kept record costs a process that streams.

The laptop and the cluster agree on cost per record more closely than they have any right to:
18.33 s per million here against a production median of 19.45, and 3.77 s per million after the
change against the cluster's 3.88. The machines differ; the shape of the work does not.

## Every ratio for this change needs its thread budget

The same two implementations, the same input, the codec pool cut to one:

| threads | `rebuild` | `copy` | ratio |
|---|---|---|---|
| 8 | 18.328 s | 3.771 s | **4.86x** |
| 1 | 29.753 s | 14.064 s | **2.12x** |
| cluster, 4M records, production shape | 46.72 s | 15.50 s | **3.01x** |

**All three are the same finding.** The change deletes serial Python work and nothing else; what is
left after it is compression, and how fast *that* residue runs is a function of how many codec
threads it gets and how contended they are. At eight threads the codec keeps up and the ratio
approaches what the loop alone would give; at one thread compression is serial and dominates the
remainder, halving the win. So **a figure quoted for this fix without its thread budget is not a
figure** — 4.86x, 3.01x and 2.12x are one result read at three points, not three results.

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

Level 1 is **22.1% faster, 16.0% larger and 42.4% cheaper in CPU**. **Keep 6**, and the reason is
the margin's size against what it is bought with: 0.80 s is 22% of the *new* wall but 4.4% of the
18.3 s wall the copy had already brought down (5.5% of the 14.56 s it removed), against 16% more
peak disk on a `temp()` artifact that the sharded-execution work
([#448](https://github.com/liuhlab/seqforge/issues/448)) wants to stop being transient. Having just
taken a 4.86x, buying a further 4% of the original with a permanent 16% on disk is the wrong trade
in the wrong order.

**The CPU figure is the one thing that argues the other way, and it is recorded so a later ticket
can find it.** Level 1 finishes faster on 2.35 cores where level 6 needs 4.09 — 42% less CPU for the
same cell. Where cells-per-node rather than disk is the binding constraint, that is a real argument
for level 1 and this is the number to start from.

## Peak disk, per cell and per plate

**The fix does not move peak disk at all.** Every arm — `rebuild`, `copy`, `inplace` — writes the
same 79 570 937 bytes for this cell, 92.2% of the 86 321 630-byte input. Across 784 cells at this
cell size that is **62.4 GB**; at level 1 it would be 72.4 GB, **+9.98 GB per plate**. Only the
codec level moves this number; the record path cannot, because the records are the same bytes.

Scale it by the cell. This fixture is 1M records; the cluster's decomposition cell was 4M, so about
4x these figures; the plate's median cell is around 25M records, and if the 92.2% ratio held there
the plate's split footprint would be on the order of 0.8 TB — an extrapolation from one synthetic
cell's compressibility, flagged as such, and not a measurement.

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
unset.

The identity arms' *headers* differ, and only in one field: the output path is embedded in the `@PG`
`CL` line, and the three arms wrote to three different directories. That is visible in the file
sizes — the two arms whose directory names are the same length produce files of the same size and
the shorter-named one is a byte smaller — and it is why the check slices the header off. Written to
the same path, as in every timing run, all three arms produce files of identical size to the byte
(71 631 582 and 7 939 355), so whole files including headers agree.

## What the envelope did not need to buy

The ticket granted higher peak memory and a moderate increase in peak disk in exchange for measured
time, and explicitly permitted parallelising the routing loop under that envelope. **Neither was
spent.** Peak RSS moved 2.3%; peak disk did not move at all; the routing loop is still one serial
Python pass.

That is a decision and not an omission, and the number behind it is the phase table. After the fix,
the serial Python routing work — the keep rule, the counters, the dictionary lookups — is 33.4% of a
3.5 s wall, and compression is another third. **Parallelising the loop now contends with the codec
rather than with idle cores**: at eight threads the process already draws 4.06 cores, so the work
handed to new workers would come out of the pool the writers are using. The record-copy lever
captured essentially all of the available win, and stopping there is the outcome the ticket said
would be a good one.

## A current utilisation figure, for the `threads:` question

Out of scope here — [#452](https://github.com/liuhlab/seqforge/issues/452) owns the declaration, and
nothing about it changed — but the ticket asked for a current number, so:

| | CPU cores used |
|---|---|
| before the fix, `threads=8` | **1.64** |
| after the fix, `threads=8` | **4.06** |
| after the fix, `threads=8`, level 1 | 2.35 |

**#452's arithmetic was measured against an implementation that no longer exists.** The rule hands
the verb the recipe's `threads` figure — 8 on the plate this was measured on — and that figure feeds
the BGZF codec pools and nothing else, the record loop being one stateless pass on one core. Before
the change those eight cores became 1.64 because the codec was idling behind a slow producer. After
it they become 4.06 against the same declaration. The over-reservation is real and much smaller than
it was, and it now depends on the compression level — the coupling that leaves the declaration to be
decided there rather than here.

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

**Harness.** Each arm is a standalone copy of `split_chimera`'s loop — same keep rule, same discard
categories, same counters, same refusals, same writers — differing only in the maker function that
turns a source record into the record handed to the writer: the repo's `_rewritten` copied verbatim
(`rebuild`), `copy.copy` plus the guarded two-field index patch (`copy`), the same patch applied to
the source record with no copy (`inplace`), and `rebuild` with the tag round-trip deleted (`notags`,
which is not a candidate). The header helpers are vendored from the module at the commit this branch
started from, so the baseline arm is pinned while the module itself is edited. One configuration per
process, because `ru_maxrss` is a process high-water mark; a driver runs each configuration four
times and reports the median of the last three.

**Batches.** Between-batch drift (~6%) exceeds within-batch spread (<3%), so every comparison above
is drawn from a single batch. Two subtractions cross a batch boundary and both are stated: the
`inplace` phase buckets are compared against `copy`'s, and the two batches' `full` runs agree to
0.2%, which is what licenses it; the tag round-trip's 13.04 s has no same-batch counterpart, so read
it as ±6% — the conclusion, that the round-trip is about two thirds of the old wall, survives that
comfortably.

**Identity.** Three arms written with `--keep`, then hashed both ways described above, with a
first-difference reporter that names the record and the differing SAM columns had anything diverged.
Headers were compared separately and are deliberately outside both hashes.

The harness lives in a scratch directory and is **not committed** — it pins a vendored copy of a
module that is being edited, which is exactly the thing that should not sit in the tree. Everything
needed to rebuild it is above.

## What this does not cover

- **A cold or networked filesystem.** Warm page cache and local SSD make the iterate bucket a floor.
  On a cold read the decompress share is larger and the ratio is smaller — which is one more reason
  the cluster's 3.01x and this 4.86x are the same finding at two operating points.
- **The real cluster tag load.** Ten tags with two arrays is a heavier per-record load than the
  plate's mean, which is why the tag round-trip prices at 69% here against 54.8% there.
- **Whether a C tool should do this at all.** The Python loop also computes every counter in the
  summary, so replacing it is a decision about where that accounting lives, and it is its own
  ticket.
- **Sharded execution** ([#448](https://github.com/liuhlab/seqforge/issues/448)). The split
  lengthens that tail and nothing here changes sharding — but it is the reason the `temp()`
  footprint above is quoted per plate rather than waved off as transient.
- **Anything about the declared `threads:` figure.** The utilisation numbers above are inputs to
  [#452](https://github.com/liuhlab/seqforge/issues/452), not a conclusion about it.
