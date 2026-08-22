# The split's writer budget: dividing it evenly cost the low end of the sweep

Measured **2026-08-22** on the same 8-core desktop and the same method as
[the record-copy write-up](split-chimera-record-copy-2026-08-21.md), for
[#476](https://github.com/liuhlab/seqforge/issues/476). One question: what the chimeric split's BGZF
budget buys when it is **divided evenly across the writers** against when each writer is **handed it
whole**.

**Handed whole, the pass reaches its serial-loop floor at 4 threads instead of 8, and total CPU does
not move.** The wall at 2 falls 31% and at 4 falls 64%; at 8 the two are the same number because
both arms are already on the floor at this fixture's depth. Nothing about the outputs changes.

## Arms

Two, differing in one expression. `divided` is `max(1, threads // len(outputs))` per writer — what
shipped. `whole` hands every writer the caller's figure. The reader keeps the whole figure in both.

## Fixture

A synthetic two-Component chimeric BAM, **400 000 templates / 800 000 records**, 400 bp reads with
per-record pseudo-random sequence — repeated sequence makes deflate trivially cheap and would hide
the very term being measured. Every record is a clean mapped proper pair carrying `NH` and `UB`, so
there are no singletons, no placeless records and no refusal in the path: the pass is the routing
loop plus the codec and nothing else.

Written **90/10 by record count between the Components**, which with one record shape is 90/10 by
bytes: **58.87 MB** against **6.63 MB**, or 89.9/10.1 — deliberately the ratio the record-copy
fixture had, because that is the ratio whose consequences #476 was filed about.

BGZF level is htslib's default here rather than the pinned 6 of the record-copy sweep, so these walls
are not comparable to that page's; only the two arms on this page are comparable to each other.

## Numbers

One run per cell, wall from `time.perf_counter()`, CPU from `os.times()` (user + system + children).

| threads | `divided` wall | `whole` wall | gain | `divided` CPU | `whole` CPU |
|---|---|---|---|---|---|
| 1 | 9.954 s | 9.845 s | — | 9.70 s | 9.58 s |
| 2 | 9.332 s | **6.427 s** | −31.1% | 9.66 s | 10.15 s |
| 4 | 6.372 s | **2.281 s** | −64.2% | 9.86 s | 10.10 s |
| 8 | 2.452 s | 2.472 s | — | 10.23 s | 10.78 s |

**The mechanism is the one the record-copy page names**, and this is that page's model with the
starvation removed. pysam hands `threads - 1` to htslib, so a writer allotted one gets **zero**
workers: with two Components an even division floored to one at both 1 and 2, and the second thread
only ever bought the reader. Handed whole, `threads=2` gives each writer one worker and the dominant
one stops doing 90% of the compression on the calling thread.

**Total CPU is flat across both arms and the whole sweep** — 9.58 to 10.78 core-seconds, a 12% band
against the ~6% between-batch drift that page records. This is the answer to the objection the
divided budget was written against: *n* writers each holding *n* threads is *n* threads that
**exist** per writer, not *n* that **run**. htslib's workers block on their queue with nothing to
compress, and what bounds the runnable count is the single serial record loop feeding them.

## Identity

Records and counters are **unchanged**, checked at threads 1, 2, 4 and 8 on both of the suite's
Chimera fixtures: a SHA-256 over every output record's SAM text in order, a second over the output
header with `@PG` excluded, and a third over the summary's counter dict. All nine digests per arm
match across every thread count and across both arms.

**`@PG` is excluded because the split stamps its own command line, `--threads` and all, into it** —
a thread count showing up there is provenance, not a record.

**Compressed bytes are NOT the subject of that check, and are not invariant even without this
change.** The *E. coli* output differs by one byte between `threads=1` and `threads=2` on the shipped
code, which is BGZF framing rather than content. What must not move is the record stream, and it does
not.

## Harness

Two scratch scripts, **not committed** — one builds the fixture and times one arm at one figure per
process, the other hashes the suite fixtures' outputs across the sweep. Both are a few dozen lines
over `pysam` and `seqforge.workflows.split.split_chimera` directly, with no snakemake in the path.
