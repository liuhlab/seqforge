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
_Avoid_: prefix, chunk, window (a window is a base range within a read)

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
_Avoid_: subsample, excerpt

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
