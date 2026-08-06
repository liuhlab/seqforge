# Probe

What a FASTQ is from its bytes, and what it is without reading a byte at all. This context covers
`probe/`, `fingerprint/` and `io/`: every read is bounded, deterministic, and reports structure
without naming what any of it means.

Words every context shares — **Evidenced**, **Basis**, **Observed**, **Conflict** and the rest — are
defined once in the repo-root `CONTEXT-MAP.md`.

## Language

### Reading bytes

**Head**:
The bounded prefix of a FASTQ that any read under the budget produces. Every FASTQ touch yields a
head, never a whole file.
_Avoid_: prefix, chunk, window (a **Window** is a range within one read, not a run of them)

**Budget**:
The pair `(max_reads, max_bytes)` that bounds a **Head**; whichever trips first stops the read, and
wall-clock is never one. A head carries the budget it was read under, not one it is told it had.
_Avoid_: limit, cap, quota

**Abandoned read**:
A **Head** whose compressed byte count is absent, never zero: the caller stopped iterating, so no
accounting was taken. A verdict about the read, where *truncated* and `ok` are verdicts about the
stream.
_Avoid_: exhausted (a **Budget** trip is the opposite case, and is already `budget_exhausted`);
cancelled, aborted, interrupted

**Record**:
One FASTQ entry: four lines — header, sequence, plus, quality.
_Avoid_: entry, line, sequence (a sequence is only the second line); what an archive declares is an
**Archive record**, never a record

**Read**:
One sequencing read — a single **Record**'s sequence, as produced by the instrument. `max_reads`
counts these.
_Avoid_: using "read" for the act of reading bytes; say "a head" or "reading a head" instead

**Tagged read**:
A **Read** that opens with the protocol's tag — an anchor motif, a UMI, and a closing motif — and so
carries the molecule's UMI. Decided positionally at offset 0, and a minority population by
construction.
_Avoid_: UMI read; barcoded read (a barcode names a cell, a tag names a molecule); and never a
layout id, since the two populations sharing one file are both R1

**Internal read**:
The complement of a **Tagged read** in the same file: a fragment from the molecule's interior,
carrying no tag and no UMI, byte-identical to bulk cDNA. *Untagged* is the per-read predicate a scan
answers; **internal** names where it came from.
_Avoid_: off-target, junk, discard — an internal read is data

**Slice**:
A **Head** kept verbatim as records, so it can be written back out as a valid FASTQ. What a
fingerprint package ships.
_Avoid_: subsample, excerpt; and never for what a **Window** cuts out of a single read — those are
bases

### Layout and measures

**Window**:
A `[start, end)` base range within one **Read**, role-conditioned by definition: a window exists
because something decided that range means something. Calling it a window says nothing about what it
holds.
_Avoid_: region, interval, span (a **Span** is a harvest quote), segment

**Frame**:
A **Window** recovered per read, for a piece of a read's declared layout whose position floats. A
fixed window is constant across reads; a frame is not, and a read whose frame is not found
contributes nothing rather than a wrong window.
_Avoid_: offset, phase (phase is how a frame is found, not what it is), alignment

**Segment**:
A run of cycles probe found structurally uniform within a **Read**, typed `constant` / `random` /
`homopolymer`. Structural only — mapping `random` onto a cell barcode, a UMI or cDNA is the
resolver's job.
_Avoid_: element (an element is what a KB **Spec** declares), region, block, feature

**Distinct ratio**:
distinct/total over the bases a **Window** or **Frame** cuts from each read, spelled
`distinct_ratio` on the wire. The measure, not the phenomenon: low means **Recurrence**, high means
uniqueness.
_Avoid_: uniqueness, diversity, complexity; recurrence (that is what it measures)

**Recurrence**:
The phenomenon a low **Distinct ratio** reports: values drawn from a small pool repeat across reads,
as cell barcodes do and UMIs do not. A property of the data, never the number.
_Avoid_: using it for the distinct ratio itself

### Identity

**Whole file**:
What is known about a FASTQ *without reading it*: its **Content address**, basename, compressed
size, and gzip ISIZE. The counterpart to a **Head** — a head is the bounded prefix you read, a whole
file is what you read it *about*, and the two need not be the same file.
_Avoid_: source, origin; and never for where the bytes are, which is a fact about the read

**Content address**:
A `sha256`-shaped **name** for a file: stable for the same file, distinct across files, and never a
hash of the file's contents. Derived from a provider md5, a bounded local key, or whole-run SRA
metadata.
_Avoid_: checksum, file hash, digest — all three imply the whole file was read
