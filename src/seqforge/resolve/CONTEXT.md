# Resolve

Two resolvers over one dataset: the byte resolver decides what the **Library** is from bytes alone,
and the metadata resolver decides which **Sample** each file came from, from records and prose. This
context names the things they identify and the machinery of deciding.

## Language

### Identity

**Library**:
One sequencing library — the physical construct the reads came out of, and what the byte resolver
identifies. Assay, read layout and per-file roles all follow from its **Chemistry**.
_Avoid_: dataset (a dataset is the files you were handed), experiment, prep; `assay` is the same
answer in EFO's vocabulary, not a second fact

**Sample**:
A **biological specimen** — what NCBI's BioSample describes, and never a read of bytes. It is the
level that fuses runs, so one sample is what becomes one `<sample>.h5ad`; in a `source: user`
**Record set** it is a grouping key instead, declaring only which files compile together.
_Avoid_: specimen; and never use "sample" for a head

**Pre-demultiplexed**:
A chemistry whose libraries were split into cells *before* the archive saw them, so the cell barcode
is the **file** and not a range of bases inside a read. Declared and never derived, and it says
**Sample**, not file and not run.
_Avoid_: demultiplexed (every Illumina run is sample-demultiplexed at bcl2fastq), plate,
one-cell-one-file; and never as a second name for `identity.sample_is_cell`, which is the field that
declares the property and is not it

**Run**:
One sequencing run — one **Library** on one pass of a sequencer, spanning every **Lane** it was
loaded into. The grouping is derived from filenames, and with no **Archive record** that grouping is
*taken as* the sample identity — exact for lanes, wrong for a library sequenced twice, since nothing
in a filename rejoins two batches.
_Avoid_: lane (narrower, and its own term); experiment (the archive's level, which is a
**Library**); and never for the execution of a composed Snakefile, which is a **Compiled pipeline**

**Lane**:
One physical lane of a flowcell, written into a filename by bcl2fastq as `_L001`. Always *inside* a
**Run** and never one itself — the same library in four lanes is one run, one library, one sample.
_Avoid_: run; and never a **Sample**

**Read designation**:
The mate a demultiplexed FASTQ declares **in its own name** — bcl2fastq's `R1`/`R2`/`I1`/`I2`, or
fasterq-dump's `_1`/`_2`/`_3`. It carries no **Lane** and no flowcell id, which is exactly what lets
the lanes and flowcells of one read fuse.
_Avoid_: mate (a paired-end sense with no room for `I1`); role or `read_id` — a **Role** comes from
the **Chemistry**'s read layout, a designation from the filename; lane token

**Archive record**:
What an archive *declared*, transcribed at four levels — project, sample, experiment, run. A
transcript, never an interpretation, and optional: most sequencing data never had an accession.
_Avoid_: metadata (too broad), SRA entry, database row; and never **Record**, which is four lines of
FASTQ

**Record set**:
The records handed to the metadata resolver, at whatever levels they were declared, with `source`
naming who declared them. A `source: user` set carries structure only and **no attributes**, which
is what keeps `asserted` meaning *"an archive's typed slot"*.
_Avoid_: records file, sample sheet (a bcl2fastq artefact), manifest — a record set is an *input* to
the dataset manifest and never one of the two artifacts

**Deposit**:
Everything one submission put into an archive under one project, as the archive holds it — every
**Archive record** at every level, and every file, whether or not any of them was fetched. It is the
scope any count over archive records is taken at.
_Avoid_: dataset (that is the files you were handed — a **Download**), project, study, series,
submission; BioProject and GEO series are one archive's spelling of a deposit, not the word

**Download**:
The part of a **Deposit** actually handed to seqforge — the files the resolver is given, which is
the set this glossary calls a dataset everywhere the contrast does not matter. Reach for the word
only where it does: a fingerprint package of 96 cells stands for a 1440-cell deposit.
_Avoid_: as a general synonym for dataset — it earns its keep only against **Deposit**; also fetch,
pull, local copy, working set

**Submitted file**:
What a **Deposit** declares about one file the submitter uploaded — its name, the provider md5, its
size, and where it can be fetched. The md5 is a **Content address** over the bytes at that location,
never computed against a file on disk.
_Avoid_: original, checksum, digest, verify; and never a **Whole file** (what a probe knows about a
FASTQ it *did* read) nor a **Download** (what reached disk)

### Deciding the library

**Observation**:
Everything probe reports about one file — composition, segmentation, distinct ratios, header
grammar, integrity — and **no roles**. Deterministic, LLM-free, network-free, cached by content
address.
_Avoid_: probe result, profile, QC report, fingerprint; and never a **Metric** — an Observation is
read from the bytes *before* anything ran, a metric is what the finished pipeline reported *after*

**Hypothesis**:
A span-verified **Assertion** handed to `score` as a selector for which **Onlist** to test first and
as a sub-threshold tie-break. It never enters the evidence matrix, un-gates a forbidden cell, or
wins a **Conflict**.
_Avoid_: hint, expectation, guess, prior (the filename prior is a different thing)

**Candidate**:
One technology scored against the bytes, carried with the **Read set** and **Role assignment** that
scored it, the **Rung** that settled each field, and any equivalence members. Ranked, never merged —
one per **Spec**, so a chemistry never competes with itself.
_Avoid_: match, hit, prediction, best guess

**Role assignment**:
The injective map from a **Read set**'s roles to the dataset's files that scored best — *total* over
that set, so an unfilled role is an invalid assignment and never a tolerated gap. Half of one
decision, the other half being **Chemistry**.
_Avoid_: mapping, pairing, demultiplexing, layout (a layout is the KB's declared structure)

**Rung**:
The escalation-ladder step `0..7` that settled one field, from metadata at 0 to a human at 7.
Recorded per field — which rung paid is provenance and an eval signal.
_Avoid_: level, tier, attempt, retry
