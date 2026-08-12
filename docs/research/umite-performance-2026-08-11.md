# What the umite counter and extractor actually cost, before and after

Measured from 2026-08-11, one section per change in the performance series
([#352](https://github.com/liuhlab/seqforge/issues/352)). **Every change in the series is
count-neutral**: the same plate in, the same `.h5ad` bytes out. Nothing here trades accuracy for
speed, so a moved number is a bug and not a result.

These are measurements, not decisions. Each section states the machine, the input and the method
that produced its numbers; what any of them *decided* lives in the issue that took the decision.

> Laptop numbers are ratios, not budgets. The absolute microseconds below come from an Apple
> Silicon laptop under Python 3.13 on one core unless a section says otherwise, and the cluster
> re-measurement lives in its own section at the end.

## UMI correction: a neighbour index against a quadratic scan

`correct_umis` compared each seed against every UMI still standing in the bucket. It now blanks one
position at a time to build a key that a UMI's Hamming-1 neighbours share and nothing else does, and
reads its neighbours out of that. **43.7× over a whole cell's genes, 182.6× on the deepest bucket
measured.** The two produce the same mapping in the same key order on every input tried, which is the
point — the change is an index, not a rule.

| n UMIs, one bucket | scan | index | | scan, skewed | index, skewed | |
|---|---|---|---|---|---|---|
| 200 | 2.84 ms | 0.28 ms | 10.1× | 4.61 ms | 0.30 ms | 15.3× |
| 1 000 | 57.58 ms | 1.84 ms | 31.4× | 104.40 ms | 1.93 ms | 54.1× |
| 4 000 | 725.71 ms | 8.77 ms | 82.7× | 1 146.26 ms | 8.80 ms | 130.3× |
| 10 000 | 2 802.61 ms | 24.83 ms | 112.9× | 4 213.10 ms | 23.07 ms | 182.6× |

**The skewed columns are the ones to read.** The parent issue's first numbers came from counts drawn
uniformly from 1–30, and real per-gene UMI counts are nothing like that: most UMIs are seen once or
twice. The skewed distribution below is 62% seen once, 20% twice, 10% three times, 5% four to eight
and 3% nine to sixty — and it makes the scan *worse* at every size, because the scan stops walking at
the first candidate too abundant to absorb and a bucket full of singletons pushes that stop to the
far end. At n = 10 000 the skew costs the scan 1.5× and costs the index nothing.

### There is no size threshold, and here is the measurement that says so

The index is behind below about eleven UMIs — it pays for a dict of `len(umi)` keys per UMI before it
looks anything up, and under that size the scan is simply shorter.

| n UMIs, skewed, µs/bucket | 2 | 4 | 8 | 10 | 11 | 12 | 13 | 16 | 20 | 30 |
|---|---|---|---|---|---|---|---|---|---|---|
| scan | 0.60 | 2.25 | 8.65 | 13.43 | 15.99 | 19.25 | 23.00 | 34.37 | 53.50 | 117.38 |
| index | 2.97 | 5.90 | 11.27 | 14.49 | 15.67 | 17.27 | 18.65 | 23.31 | 29.29 | 44.58 |
| | 0.20× | 0.38× | 0.77× | 0.93× | 1.02× | 1.11× | 1.23× | 1.47× | 1.83× | 2.63× |

That looks like a case for falling back to the scan on small buckets, and it is not, because the
buckets it would apply to cost nothing. Over 8 000 buckets sized like one real cell's genes — median
2, mean 27.5, max 3 851, 219 751 UMIs in total — the sub-crossover buckets are **82% of the genes and
8.1 ms of the cell's 20 428.7 ms**:

| | scan | index | |
|---|---|---|---|
| whole cell, 8 000 buckets | 20 428.7 ms | 467.3 ms | 43.7× |
| only the 6 545 buckets at n < 12 | 8.1 ms | 21.6 ms | 0.4× |

A threshold would recover 13.5 ms of 467.3 — 2.9% — and would buy it with a second path through the
one function in this module whose whole claim is that it is provably the function it replaced. So
there is none, and the crossover the parent issue left unmeasured is eleven.

Holding each UMI's keys instead of rebuilding them when it comes up as a seed is what makes that
affordable: it is worth 1.5× at every size and moves the crossover from about twenty UMIs down to
eleven, which is what takes the sub-crossover buckets from *arguable* to 8 ms of a 20-second cell.
Carrying the blanked position in the key alongside the two surviving fragments is redundant — the
prefix's length already names the position — and dropping it measured 1–4%, so the redundant form
stays for the reader.

### Method

Apple Silicon laptop, Python 3.13.14, one core, `min` of repeated runs. Buckets are synthetic:
random UMIs over `ACGT` at 8 bp, counts drawn from the two distributions above, one fixed seed per
size so the same buckets time both implementations. The scan is the previous implementation copied
verbatim, and every bucket timed was first checked for an identical result — same totals, same key
order — under both. The whole-cell figure is 8 000 buckets whose *sizes* are drawn from a second
skewed distribution (45% one UMI, 20% two, up to a 0.3% tail of 800–4 000); it is a shape, not a
plate, and the real plate is the last section of this page.

The equivalence itself is not a measurement and does not rest on one: the scan's early stop is the
same arithmetic as the index's filter because the survivors are in count order, and the seed's count
is never raised as it absorbs, so absorption order cannot change a total. The property test in
`tests/test_workflows.py` re-derives that on 400 generated buckets over a three-letter alphabet at
mixed lengths, against the scan written out in the test.

## The annotation lookup: `bisect` over `array.array` against `np.searchsorted`

**`_StepIndex.genes` is 1.86x, and the index does not grow by a byte.** Apple M4 Pro, macOS 26.5.2,
Python 3.13.14, numpy 2.5.1, one core, against a gencode-scale index — 55 000 genes, 825 000 exons,
**1 650 001 segments** over 55 001 interned sets.

The four ways to run the two searches the lookup runs, and what each structure weighs:

| the two searches | µs/lookup | resident |
| --- | --- | --- |
| `np.searchsorted(arr, v)` — before | 1.12 | 13.2 MB |
| `arr.searchsorted(v)` — the bound method | 0.73 | 13.2 MB |
| `bisect` on `list[int]` | 0.53 | 65.8 MB |
| **`bisect` on `array.array("q")`** | **0.62** | **13.2 MB** |

`array.array("q")` and a numpy int64 array are the same 13.2 MB, to two decimal places: both are an
8-byte buffer of 1 650 001 items and nothing else. A `list[int]` is that same buffer of pointers plus
1 650 001 separate integer objects, and the +52.6 MB it costs is those objects — measured as process
resident after the source list was freed, 114.4 MB against 61.9. It is **not free memory** for
0.09 µs, and there are two indexes per contig.

**Holding `starts` as an `array.array` also makes the index cheaper to build**, which was not
expected: `array.array("q", starts)` is **12.3 ms** against `np.array(starts, dtype=np.int64)`'s
21.0 ms over 1 650 001 segment starts. Nothing else in the module reads `starts` — the sweep hands
it over as a `list[int]` either way, `set_ids` stays a numpy int32 array, and `Annotation`'s two
lookup methods pass `genes`' answer straight out — so the structure change is four lines.

Whole-lookup, which is the number that reaches a run:

| `_StepIndex.genes`, one span | µs/call | |
| --- | --- | --- |
| before: `np.searchsorted`, no fast path | 1.69 | — |
| `bisect`/`array.array`, no fast path | 1.02 | 1.66x |
| bound method + fast path | 1.07 | 1.59x |
| **`bisect`/`array.array` + fast path — shipped** | **0.91** | **1.86x** |

**The single-segment fast path is worth 0.11 µs of that**, because 80.3% of spans touch exactly one
segment: a 150 bp fragment against segments averaging ~730 bp rarely straddles a boundary. Timed on
single-segment spans alone, against sets of one to four genes, it is 0.97 µs to 0.79 — **1.23x** for
deleting one `set()`, one `|=` and one `frozenset()` copy. That is a floor rather than a typical
figure: the saving is the copy, so it grows with how many genes the segment names.

### What does not reproduce from [#352](https://github.com/liuhlab/seqforge/issues/352)

Every ordering holds and every absolute is smaller — this machine runs the search about 3x faster
than the one the issue was written on, so treat the ratios as the result.

- **The margin between the two `bisect` forms is a third of what the issue priced.** 0.09 µs here
  against 0.24 there. The decision gets easier, not harder: the same +52.6 MB now buys less.
- **`list[int]` costs more than the issue's +46 MB**, at +52.6, which is what 32-byte integer
  objects plus an 8-byte pointer come to at this scale.
- **The bound method is 65% of the win rather than 61%**, and it remains the fallback: it is the two
  lines in `genes` and the one line in `_step_index` that build `starts`, so the cluster ticket can
  still take it. On this machine it loses to `bisect` by 0.16 µs a lookup.

### Method

`timeit`-style best-of-9 whole passes over 200 000 spans, one core, no other load. The index is
built by `_step_index` itself from 825 000 synthetic exons — 55 000 genes of 15 exons, exon lengths
80–300 bp and gaps 200–2 000 bp under one fixed seed — which yields 1 650 001 segments over 1.2 Gb,
the shape the parent issue measured. Spans are 150 bp starting uniformly over the contig.

Memory is one structure per process, each built the way `_step_index` builds it — from the
`list[int]` the sweep accumulates — with that list freed and the garbage collector run before the
reading, so the figure is the steady state an `Annotation` holds for a whole fan-in, not a peak.
`sys.getsizeof` and process resident agree for the two buffers; only the list disagrees, by exactly
its integer objects.

Equivalence was checked before any timing: all four search forms return the same pair of bounds on
every one of the 200 000 spans, and all five whole-lookup forms return sets equal to the previous
implementation's on 20 000 of them. The exhaustive check is in `tests/test_workflows.py`, where the
oracle is a brute-force sweep of the intervals rather than a second reading of the index — every
span of a small index carrying three features that open at one base, two that close at one base, an
adjacent pair, and a zero-length feature that must open no segment at all.

## Building an unaligned record: `fromstring` against attribute-by-attribute

**Parsing one SAM line builds the same record 4.1x cheaper, and a whole extraction is 1.6x faster
for it.** macOS 26.5 on Apple Silicon, Python 3.13.14, pysam 0.24.0 (htslib's own parser), one core.

| step | before | after | |
| --- | --- | --- | --- |
| `_segment`, a tagged record | 2.64 µs | 0.65 µs | 4.1x |
| `_segment`, an untagged one | 2.42 µs | 0.64 µs | 3.8x |
| `find_tag`, one read | 0.346 µs | 0.286 µs | 1.2x |
| `extract_umis`, one read pair | 9.56 µs | 5.95 µs | 1.6x |

`pysam.qualitystring_to_array` alone was 1.75 µs of the old 2.64 — **66% of the cost of building a
record was turning a quality string into a per-base array**, and a SAM line carries the string the
FASTQ already handed over, so that call is gone rather than made faster. The rest is one call into
htslib in place of six into pysam. On a paired plate two records are written per fragment, so record
building was about half of what one extraction cost here — and dropping it took 3.6 µs off the 9.56.

`find_tag`'s per-record prefix slice is the smaller half: `stop` already bounds the search to the
last offset the anchor may start at, and the anchor is never longer than the span a match consumes,
so the slice cut nothing the bound had not already cut. 0.05 µs a read to say nothing.

**What the numbers do not match is [#352](https://github.com/liuhlab/seqforge/issues/352)'s own.**
It priced the old builder at 4.91 µs and the new at 1.08, the quality conversion at 2.82, the slice
at 0.14, and record building at 35% of extraction. Every ratio holds — 4.5x there against 4.1x here,
and the quality conversion is the majority of the old cost either way — but this machine is faster
in absolute terms and its extraction is a smaller total (9.56 µs a pair, against 28.2 there), which
makes record building a larger share of it rather than a smaller one. Treat the ratios as the
result.

### Two fields where a SAM line and a BAM record disagree

Neither is a speed finding, and both would have changed the bytes silently:

- **The index bin.** A record assembled field by field leaves it 0; parsing one fills in the 4680
  htslib computes for an unmapped read. An unsorted, unindexed uBAM has no use for either, but the
  file's bytes are its identity, so it is put back to 0.
- **A quality string of exactly `*`.** SAM reads that as *this read has no qualities*, so a one-base
  read whose Phred happens to be 9 — a legal FASTQ record — would come back carrying none. Its
  quality is restored by hand; every other quality string round-trips as written.

A tab inside a sequence or a quality string is the one input where the two constructions cannot be
reconciled: parsing refuses it where assembling encoded it as a base. That is a FASTQ that is not
one, and both readings are a stopped run rather than a wrong number.

### Method

`timeit`, best of 7 runs of 200,000 calls for the record builder; 20,000 synthetic 150 bp reads (44%
carrying a tag at offset 0, 13, 15 or 23, 6% carrying one past the drift window, the rest random
ACGT) over 20 passes for `find_tag`; three whole runs of `extract_umis` over a 200,000-pair gzipped
FASTQ pair for the end-to-end figure, with the record builder swapped underneath so both halves read
the same input through the same reader and writer.

Byte identity was checked by extracting five ways — a paired cell, a paired cell deposited across
two runs, both of those single-ended, and one whose qualities are a single `*` — and hashing each
uBAM before and after the change, along with every figure in the extraction summary beside it. All
five hashes and all five summaries are unchanged. The 240,000 read/geometry pairs that checked the
slice removal (4,000 random geometries x 60 reads) disagreed on nothing.

## The one FASTQ loop: where `BoundedReader`'s microseconds go

**Everything the loop does besides inflating the bytes gets 1.5x to 2.5x cheaper, and a whole pass
is 1.1x to 2.0x faster** — which of those two numbers you see depends entirely on how much of the
file's cost was inflating it. Apple M4 Pro, macOS 26.5.2, Python 3.13.14, one core.

| a record of | inflate alone | + bulk split | before | after | |
| --- | --- | --- | --- | --- | --- |
| 36 bp, flat qualities | 0.13 µs | 0.21 µs | 0.79 µs | 0.39 µs | 2.04x |
| 100 bp | 0.60 µs | 0.74 µs | 1.27 µs | 0.91 µs | 1.40x |
| 150 bp | 0.88 µs | 1.07 µs | 1.57 µs | 1.26 µs | 1.25x |
| 250 bp | 1.44 µs | 1.70 µs | 2.15 µs | 1.91 µs | 1.13x |

The last column is the least informative one in the table, because most of what a record costs is
**inflating it**, and that share is a property of the file rather than of the loop. Two flat numbers
are the finding. Before, a record cost 0.66–0.71 µs above inflating — whatever the read length —
which is four Python-level `readline` calls, four `next`s and four `rstrip`s, none of whose cost
depends on how long a line is. After, it costs 0.17–0.21 µs above inflate-plus-one-bulk-`split`,
again flat: four list reads, four `len`s and the tuple. **The line handling was the constant, and it
is what shrank; the growth left in the "after" column is the `split` itself, which allocates the
same four line objects `readline` used to.**

**What does not reproduce is [#352](https://github.com/liuhlab/seqforge/issues/352)'s pair of
numbers**, 2.11 µs a record against a 0.9 µs floor — 2.3x headroom. No single input here gives both:
2.15 µs a record is what a 250 bp read costs, and at 250 bp the floor is 1.70, so the headroom there
is 1.3x; 0.9 µs total is near the 100 bp *floor*. The ratio was read off a short-read file and
quoted as a property of the loop. What is a property of the loop is the flat 0.68 µs, and it caps
what any rewrite could return: on a 250 bp file, inflating alone is 67% of the pass and no line
handling can be removed from it.

**Where it lands.** A probe reads 2 000 records of a file by default, so it collects under a
millisecond; the extractor reads every record of every cell, two per fragment on a paired plate, and
it is what this pays. The reader is not the extractor's largest line either way.

**The pull size is not a knob.** Bytes arrive `io.DEFAULT_BUFFER_SIZE` at a time because that is the
gzip line reader's own buffer size: the handle underneath is then read in exactly the steps a
line-at-a-time read reads it in, so `compressed_bytes` — a position on that handle, and the input to
the read-count estimate — keeps the value it had on a read that a budget stopped part-way. A 64 KiB
pull is a further 5–10% (1.08 µs against 1.19 at 150 bp, measured) and moves that number, so it was
not taken.

### Method

Best of 7 whole passes over a 200 000-record gzipped FASTQ per read length, under an unbounded
budget, records counted and dropped. *Inflate alone* is the same `read1` loop discarding what it
gets; *+ bulk split* adds one `split` per pull and the cross-pull carry, and is the floor this loop
can reach. The 36 bp row uses constant `I` qualities and the rest vary them, which is why its file
inflates ~5x cheaper per record than its length alone would suggest.

Equivalence was checked field by field — the records themselves, `n_reads`, `decompressed_bytes`,
`compressed_bytes`, `truncated`, `ok`, `abandoned`, `budget_exhausted` — between the old loop and the
new one over **12 376 (input, budget) pairs**, 28 inputs against 442 budgets. The inputs: CRLF, no
final newline, blank lines, lines longer than one pull, an empty member, two concatenated members,
cuts at eight depths and one in the gzip header, a stream that was never gzip, a plain uncompressed
FASTQ, a corrupt deflate payload, a bad CRC. The budgets include every byte budget across a buffer
boundary and both zeroes. Then **4 000 randomised trials** of random line lengths, random
terminators, random truncations, random bit flips and random budgets. Nothing differed on any of
them.

## The fan-in: counting a plate on every core the rule asked for

**A worker costs 75 MB and buys a core, and the 75 MB is a ceiling rather than a rate.** `count_plate`
was a list comprehension over cells while `rule umi_count` asked the scheduler for every thread it
was configured with; it now forks a worker per thread, and each worker inherits the annotation
instead of being sent one. Apple M4 Pro (10 performance + 4 efficiency cores), macOS 26.5.2, Python
3.13.14, pysam 0.24.0.

| workers | `count_plate` | | of which counting | of which the object |
| --- | --- | --- | --- | --- |
| 1 | 46.76 s | 1.00x | 45.40 s | 1.24 s |
| 2 | 25.71 s | 1.82x | | |
| 4 | 15.02 s | 3.11x | | |
| 8 | 9.30 s | 5.03x | 8.38 s | 0.90 s |
| 10 | 8.04 s | 5.82x | | |
| 14 | 8.07 s | 5.80x | 6.65 s | 0.95 s |

56 synthetic cells of 20 000 to 360 000 fragments against a gencode-scale annotation. Every width
produced the same object — same rows in the same order, same per-row totals, same per-cell fragment
counts. **The parent's own share is ~1 s of the 46.76**, so what stops the last column short of
linear is the cells themselves: fourteen workers on a machine with ten fast cores and four slow ones,
against cells whose depths span 18x. Treat 5x on eight as the shape and not as a budget — the real
plate is the last section of this page.

### Resident growth per worker, which is what chooses the width

A forked child shares every page until it writes to one, and touching a Python object writes to it:
the refcount lives in the object header. So the interned gene sets are what a worker actually copies.
Measured as each child's **unique** resident set (USS) right after the fork and again after counting,
against a parent holding 55 000 genes x 15 exons in both indexes — 1 760 002 segments over 110 002
interned `frozenset`s, 22.7 MB of them by `sys.getsizeof`, in a parent of 212 MB USS.

| a worker that has | USS growth |
| --- | --- |
| done nothing | 0.1 MB |
| counted 20 000 fragments | 75.4 MB |
| counted 200 000 | 75.5 MB |
| counted 2 000 000 | **75.5 MB** |
| 200 000, holding the cell's UMI buckets | 103.9 MB |
| 2 000 000, holding the cell's UMI buckets | 258.3 MB |

**The copy-on-write cost stops growing after the first twenty thousand fragments**, because by then
every interned set has been touched once and there is nothing left to dirty. That is what makes the
width free to be the thread count: it is 75 MB times the workers, not 75 MB times the workers times
the plate. What still grows with depth is the worker's own accumulator — the cell's UMI buckets —
and that is memory the single-core version paid too, one cell at a time instead of N.

75.5 MB against 22.7 MB of frozensets is the granularity: a page is 16 KB here, and the sets were
allocated interleaved with everything else the index build allocated, so touching one set dirties
whatever shares its page. **The two flat buffers are untouched and stay genuinely shared** —
`bisect` allocates a Python int per probe and never writes into the `array.array`, and reading a
numpy element increfs the array object rather than its bytes — which is 13.2 MB per index that costs
nothing per worker.

**A garbage collection in a child costs 22 MB on its own**, and an idle worker that collects goes
from 0.1 MB to 46.3 MB: the cyclic collector walks every tracked object and writes its header.
Nothing here calls `gc.collect()`, and this is the measurement that says not to.

### What crosses the pipe, and why the deduplication happens in the worker

A worker returns two things, and the reason is not the one it looks like:

| one cell's | pickled | `dumps` | `loads` | `deduplicate` |
| --- | --- | --- | --- | --- |
| raw counts, 200 000 fragments / 198 878 UMIs | 2.5 MB | 10.4 ms | 8.9 ms | 905 ms |
| its five matrices + fates | 0.034 MB | 0.1 ms | 0.2 ms | — |
| raw counts, 1 000 000 fragments | 12.5 MB | 69.1 ms | 46.4 ms | 4 622 ms |
| its five matrices + fates | 0.158 MB | 0.4 ms | 0.9 ms | — |

**The pipe was never the bottleneck** — 10 ms to send back a cell that took seconds to count. The
correction is: left in the parent it is 0.9 to 4.6 seconds per cell of strictly serial work, which
over a 784-cell plate is a tail on the order of ten minutes while every worker idles. So the worker
deduplicates and sends both — the matrices, which is what the object needs, and the raw counts,
which is what `count_plate` has always handed back. Sending only the matrices would shrink the pipe
74x and buy nothing measurable.

### Method

`count_plate` timed whole, best of one run per width in one process, cells written once and reused
across widths. The annotation is 55 000 genes of 15 exons under one fixed seed, the same shape the
lookup section above uses; cells are coordinate-sorted BAMs of paired 100 bp records with a UMI on
four fifths of the fragments drawn from a pool a quarter the fragment count, so correction has real
work. Resident growth is `psutil`'s `memory_full_info().uss` inside each child, read after a fork
from a parent that had already built the annotation, with an idle-worker control to separate the fork
itself from what counting dirties. Pickle costs are `pickle.dumps`/`loads` at the default protocol
over a synthetic cell whose gene bucket sizes are drawn from the skewed distribution the correction
section describes.

Equivalence is asserted in `tests/test_workflows.py` rather than measured here: the same plate
counted at width 1 and at width 4 writes byte-identical `.h5ad` files, and a plate whose cells are
held in lockstep by a barrier — so that all of them finish at once and the completion order is
whatever the scheduler chose — still comes back in the caller's row order with each row's fragment
count on its own row.

### What does not reproduce from [#352](https://github.com/liuhlab/seqforge/issues/352)

- **`fork` is available on macOS**, so the serial fallback is not the platform's doing. The issue
  reads "where `fork` is unavailable — macOS, where Python now defaults to `spawn`", but
  `multiprocessing.get_all_start_methods()` returns `['spawn', 'fork', 'forkserver']` on macOS 26.5.2
  under Python 3.13.14: what changed on macOS is the *default*, not the availability. The counter
  asks for the start method it wants rather than accepting the default, so it forks here too — which
  is what let the numbers above be taken on a laptop at all. The serial arm is still reachable and
  still tested, by asking a counter that is told fork is not on offer.
- **The pool is not "near-linear"** on this machine past four workers. 3.11x on four and 5.03x on
  eight, on a laptop whose fourteen cores are ten fast and four slow. A cluster node's cores are
  uniform, which is why this is the number [#398](https://github.com/liuhlab/seqforge/issues/398)
  re-takes.

## On the cluster, on a real plate

<!-- filled by #398 -->
