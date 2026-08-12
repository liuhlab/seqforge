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

<!-- filled by #393 -->

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

<!-- filled by #396 -->

## The fan-in: counting a plate on every core the rule asked for

<!-- filled by #397 -->

## On the cluster, on a real plate

<!-- filled by #398 -->
