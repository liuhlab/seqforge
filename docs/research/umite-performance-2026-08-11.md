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

<!-- filled by #394 -->

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

<!-- filled by #397 -->

## On the cluster, on a real plate

<!-- filled by #398 -->
