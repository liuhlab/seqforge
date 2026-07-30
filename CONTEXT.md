# seqforge

A compiler from `(FASTQ files) + (unstructured metadata)` to a validated `manifest.yaml` and a
runnable Snakemake pipeline. This file is the **glossary** — the words the project uses and the
synonyms it avoids. Rules live in `CLAUDE.md`; rationale lives in `docs/design.md`. Nothing else
belongs here.

## Language

### Reading bytes

**Head**:
The bounded prefix of a FASTQ that any read under the budget produces. Every FASTQ touch yields a
head, never a whole file.
_Avoid_: prefix, chunk, window (see **Window** — that is a range within one read, not a run of them)

**Budget**:
The pair `(max_reads, max_bytes)` that bounds a head. Whichever trips first stops the read.
Wall-clock is never a budget.
_Avoid_: limit, cap, quota

**Record**:
One FASTQ entry: four lines — header, sequence, plus, quality.
_Avoid_: entry, line, sequence (a sequence is only the second line)

**Read**:
One sequencing read — a single record's sequence, as produced by the instrument. `max_reads` counts
these.
_Avoid_: using "read" for the act of reading bytes; say "a head" or "reading a head" instead

**Slice**:
A head kept verbatim as records, so it can be written back out as a valid FASTQ. What a fingerprint
package ships.
_Avoid_: subsample, excerpt; and never for what a window cuts out of a single read — those are
**bases**

### Layout and measures

**Window**:
A `[start, end)` base range within one read. Role-conditioned by definition — a window exists because
something decided that range means something (a KB spec, probe's own segmentation). Calling it a
window says nothing about what it holds.
_Avoid_: region, interval, span (a span is a harvest quote), segment (probe's own structural
segmentation)

**Frame**:
A window recovered per read, for a piece of a read's declared layout whose position floats.
`kb.anchor.resolve_windows` finds one read's frame by phase detection; a read whose frame is not found
contributes nothing rather than a wrong window. A fixed window is constant across reads; a frame is
not.
_Avoid_: offset, phase (phase is how a frame is found, not what it is), alignment

**Distinct ratio**:
distinct/total over the bases a window or frame cuts from each read. The measure, not the phenomenon:
low means recurrence, high means uniqueness. Spelled `distinct_ratio` on the wire — in KB specs and in
the Observation — so the name is fixed.
_Avoid_: uniqueness, diversity, complexity; recurrence (that is what it measures)

**Recurrence**:
The phenomenon a low distinct ratio reports: values drawn from a small pool repeat across reads, as
cell barcodes do and UMIs do not. A property of the data, never the number.
_Avoid_: using it for the distinct ratio itself

### Identity

**Sample**:
A **biological specimen** — what NCBI's BioSample describes. Never a read of bytes. The metadata
resolver answers "which sample is each file from"; the byte resolver never sees one.
_Avoid_: specimen; and never use "sample" for a head. `StreamSample`/`probe_sample`/`sample_fastq_*`
are legacy spellings of the byte sense, being retired.

**Run**:
One sequencing run — the grouping `resolve/group.py` derives from filenames. With no archive record,
that grouping *is* the sample identity.
_Avoid_: lane, experiment (both are narrower archive concepts)
