# seqforge

A compiler from `(FASTQ files) + (unstructured metadata)` to a validated `manifest.yaml` and a
runnable Snakemake pipeline. This file is the **glossary** — the words the project uses and the
synonyms it avoids. Rules live in `AGENTS.md`; the reference behind each area lives in `docs/agents/`,
and one decision per file in `docs/adr/` (indexed by module in `docs/adr/README.md`). Nothing else
belongs here: a term one of those files already argues at length appears below as a one-line gloss and
a pointer, because two prose definitions of one term is the failure this file exists to prevent.
Which of the four layers a new piece of writing belongs to is settled in
[`docs/adr/README.md`](docs/adr/README.md), and why there are four in
[ADR-0041](docs/adr/0041-four-layers-and-none-is-published.md).

**Use these words.** When your output names a domain concept — an issue title, a refactor proposal, a
hypothesis, a test name — use the term as defined below, and not a synonym an entry lists under
*Avoid*. A concept that is not here yet is a signal either way: usually it is language the project
does not use, occasionally it is a real gap worth adding.

**Two vocabularies, and they do not mix.** Domain terms come from here. Architecture terms — module,
interface, depth, seam, adapter, leverage, locality — are fixed, and "component", "service", "API"
and "boundary" are not substitutes for them.

## Language

### Reading bytes

**Head**:
The bounded prefix of a FASTQ that any read under the budget produces. Every FASTQ touch yields a
head, never a whole file.
_Avoid_: prefix, chunk, window (see **Window** — that is a range within one read, not a run of them)

**Budget**:
The pair `(max_reads, max_bytes)` that bounds a head. Whichever trips first stops the read.
Wall-clock is never a budget. A head carries the budget it was read under — not one it is told it had.
_Avoid_: limit, cap, quota

**Abandoned read**:
A **Head** whose compressed byte count is **absent, never zero**: the caller stopped iterating, so no
accounting was taken. A verdict about the read, where *truncated* and `ok` are verdicts about the
stream.
_Avoid_: exhausted — a **Budget** trip is the opposite case and is already `budget_exhausted`; also
cancelled, aborted, interrupted

**Record**:
One FASTQ entry: four lines — header, sequence, plus, quality.
_Avoid_: entry, line, sequence (a sequence is only the second line); what an archive declares is an
**Archive record**, never a record

**Read**:
One sequencing read — a single record's sequence, as produced by the instrument. `max_reads` counts
these.
_Avoid_: using "read" for the act of reading bytes; say "a head" or "reading a head" instead

**Tagged read**:
A **Read** that opens with the protocol's tag — an anchor motif, a UMI, and a closing motif — and so
carries the molecule's UMI. Decided positionally, at offset 0; a *minority* population whose fraction
is a tunable protocol parameter, which is why no gate over it may be a majority one
(`docs/agents/resolve.md`, `kb/specs/smartseq3/`).
_Avoid_: UMI read; barcoded read — a barcode names a cell, a tag names a molecule; and never a layout
id as a synonym (the two populations that share one file are both R1)

**Internal read**:
The complement of a **Tagged read** in the same file: a fragment from the molecule's interior,
carrying no tag and no UMI, byte-identical to bulk cDNA. Counted, never UMI-deduplicated. *Untagged*
is the per-read predicate a scan answers; **internal** names where it came from.
_Avoid_: off-target, junk, discard — an internal read is data

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
A window recovered per read, for a piece of a read's declared layout whose position floats. Found by
phase detection; a read whose frame is not found contributes nothing rather than a wrong window. A
fixed window is constant across reads; a frame is not.
_Avoid_: offset, phase (phase is how a frame is found, not what it is), alignment

**Segment**:
A run of cycles probe found structurally uniform within a read, typed `constant` / `random` /
`homopolymer`. Structural only — mapping `random` onto CB, UMI or cDNA is the resolver's job, scored
and second-guessable.
_Avoid_: element (an element is what a KB **Spec** declares), region, block, feature

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

**Whole file**:
What is known about a FASTQ *without reading it*: its content address, basename, compressed size, and
gzip ISIZE. The counterpart to a **Head** — a head is the bounded prefix you read, a whole file is
what you read it *about*, and the two are not always the same file (`docs/adr/0001`).
_Avoid_: source, origin (a probe has no read-source abstraction); and never for where the bytes are,
which is a fact about the read

**Content address**:
A `sha256`-shaped **name** for a file: stable for the same file, distinct across files, and never a
hash of the file's contents. Derived from a provider md5, a bounded local key, or whole-run SRA
metadata.
_Avoid_: checksum, file hash, digest — all three imply the whole file was read, which R3 forbids

**Library**:
One sequencing library — the physical construct the reads came out of, and what the byte resolver
identifies. Assay, read layout and per-file roles all follow from its **Chemistry**
(`docs/adr/0006`). An **Archive record**'s experiment level names one, which is why several runs sit
under one — a library sequenced twice.
_Avoid_: dataset (a dataset is the files you were handed), experiment, prep; `assay` is the same
answer in EFO's vocabulary, not a second fact

**Sample**:
A **biological specimen** — what NCBI's BioSample describes. Never a read of bytes. The metadata
resolver answers "which sample is each file from"; the byte resolver never sees one. It is also the
level that **fuses runs** — `ancestor(run, "sample")` is the join, so a sample is what becomes one
`<sample>.h5ad`. In a `source: user` **Record set** the two come apart, and there the sample id is a
*grouping key* and not a claim about a specimen: it carries no attributes, so it declares only which
files compile together (`docs/adr/0034`).
_Avoid_: specimen; and never use "sample" for a head

**Pre-demultiplexed**:
A chemistry whose libraries were split into cells *before* the archive saw them, so the cell barcode
is the **file** and not a range of bases inside a read. Declared and never derived — no byte reports
where a split happened — and it says **Sample**, not file and not run (`docs/adr/0032`).
_Avoid_: demultiplexed (every Illumina run is sample-demultiplexed at bcl2fastq, so a reader would
tick that box for a droplet chemistry too), plate, one-cell-one-file; and never as a second name for
`identity.sample_is_cell`, which is the field that declares the property and is not it

**Run**:
One sequencing run — one **Library** on one pass of a sequencer, spanning every **Lane** it was
loaded into. The grouping is derived from filenames. With no archive record that
grouping is *taken as* the sample identity, which is exact for lanes and wrong for a library
sequenced more than once: nothing in a filename rejoins two batches (`docs/adr/0027`).
_Avoid_: lane (narrower, and its own term); experiment (the archive's level, which is a **Library**);
and never for the execution of a composed Snakefile, which is a **Compiled pipeline**

**Lane**:
One physical lane of a flowcell, written into a filename by bcl2fastq as `_L001`. Always *inside* a
**Run** and never one itself — the same library in four lanes is one run, one library, one sample.
Retained only to order a run's files identically for every mate.
_Avoid_: run; and never a **Sample**, which is what reading it as one produced (`docs/adr/0027`)

**Read designation**:
The mate a demultiplexed FASTQ declares **in its own name** — bcl2fastq's `R1`/`R2`/`I1`/`I2`, or
fasterq-dump's `_1`/`_2`/`_3`. It carries no **Lane** and no flowcell id, which is exactly what lets
the lanes and flowcells of one read fuse (`docs/adr/0027`).
_Avoid_: mate (a paired-end sense with no room for `I1`); role or `read_id` — a role comes from the
**Chemistry**'s read layout, a designation from the filename; lane token

**Units table**:
The table `compose` writes beside a **Compiled pipeline**'s config: one row per FASTQ, naming its
**Sample**, its **Run**, its **Lane**, and which layout read it is. It is the only statement of where
a run's files are and what order they arrive in (`docs/adr/0027`), so every consumer — a module's
declared inputs and the verbs that module calls — reads placement from here rather than deriving it a
second time (`docs/adr/0036`).
_Avoid_: **sample sheet** — bcl2fastq's `SampleSheet.csv` maps indices to samples *upstream* of us,
which is a different mapping one layer up, and the collision is why we do not reuse the phrase; also
manifest (the IR — what the data IS, never where its files sit) and `units.tsv` (a filename)

**Archive record**:
What an archive *declared*, transcribed at four levels — project, sample, experiment, run. A
transcript, never an interpretation, and optional: most sequencing data never had an accession, and
that absence is the normal case rather than a degraded one (`docs/adr/0010`). An archive is not the
only declarer — a **Record set** is the container, and its `source` says who declared it.
_Avoid_: metadata (too broad), SRA entry, database row; and never **Record**, which is four lines of
FASTQ

**Record set**:
The records handed to the metadata resolver, at whatever levels they were declared, with `source`
naming who declared them. A `source: user` set carries structure only and **no attributes**, which is
what keeps `asserted` meaning *"an archive's typed slot"* (`docs/adr/0034`, `docs/adr/0010`).
_Avoid_: records file, sample sheet (a bcl2fastq artefact), manifest — a record set is an *input* to
the dataset manifest and never one of the two artifacts

**Deposit**:
Everything one submission put into an archive under one project, as the archive holds it — every
**Archive record** at every level, and every file, whether or not any of them was fetched. One
deposit is one source at every level (`docs/adr/0021`), and it is the scope any count over archive
records is taken at, because such a count asks what *kind* of dataset this is and that is a property
of the submission rather than of what reached disk (`docs/agents/resolve.md`).
_Avoid_: dataset (that is the files you were handed — a **Download**), project, study, series,
submission; BioProject and GEO series are one archive's spelling of a deposit, not the word

**Download**:
The part of a **Deposit** actually handed to seqforge — the files the resolver is given, which is
the set this glossary calls a dataset everywhere the contrast does not matter. Reach for the word
only where it does: a fingerprint package of 96 cells stands for a 1440-cell deposit, so a count
taken here reports how much was fetched and never what the deposit is.
_Avoid_: as a general synonym for dataset — it earns its keep only against **Deposit**; also fetch,
pull, local copy, working set

**Submitted file**:
What a **Deposit** declares about one file the submitter uploaded — its name, the provider md5, its
size, and where it can be fetched. The md5 is a **Content address** over the bytes at that location,
never computed against a file on disk (`docs/adr/0033`).
_Avoid_: original, checksum, digest, verify (all imply the file was read); and never a **Whole file**
(what a probe knows about a FASTQ it *did* read) nor a **Download** (what reached disk)

### Evidence

**Evidenced**:
The envelope every *interpretive* field travels in — `{value, basis, evidence, confidence, rung}`,
frozen once validated. One judgement gets exactly one envelope (`docs/adr/0006`).
_Avoid_: wrapper, annotated value, provenance record; raw identity and `resources` carry no envelope
at all

**Basis**:
How a value came to be known — a closed set of four: `observed`, `asserted`, `inferred`,
`user_confirmed`. On a **Recipe** it answers a different question: there `basis` records *who
decided*.
_Avoid_: source, origin, reliability; provenance (that is the whole envelope, not this field)

**Observed**:
Basis for a value read out of the bytes, and the authority the library section defers to: nothing
asserted overrides it, and the disagreement surfaces as a **Conflict** instead (`docs/adr/0010`).
_Avoid_: measured, detected, empirical, ground truth

**Asserted**:
Basis for a value a human or a database *claimed* — a span-verified **Assertion**, or a field ENA
declared. A claim, never a measurement, however authoritative the archive.
_Avoid_: declared, reported, known, metadata-derived — each reads as though the claim had been
checked

**Inferred**:
Basis for a value code derived rather than read or was told: every policy default (whose `evidence`
names the rule that fired), and a sample fact a model read in a dataset-level document.
_Avoid_: guessed, assumed, defaulted; there is no `policy_default` basis, and whether to add one is
still open (`docs/agents/models.md`)

**User-confirmed**:
Basis for a value a person chose — a CLI flag or an `--instruction` document, distinguished from
each other only by precedence. Almost exclusively a recipe basis: it is what the **Recipe** exists to
carry (`docs/adr/0004`).
_Avoid_: manual, override, approved, human-in-the-loop

**Declared**:
Not a fifth **Basis** — a property of one position *within* a source: the submitter typed this value
into a slot **for this attribute**, rather than a model having read it out of that source's prose.
Inside one source it wins, and the reading is named in a **Warning** (`docs/adr/0021`).
_Avoid_: as a synonym for **Asserted** — a record's typed slot and a model's reading of that record's
title are both asserted, which is precisely the pair this term exists to separate; also stated

**Rung**:
The escalation-ladder step `0..7` that settled one field, from metadata at 0 to a human at 7 (R9,
`docs/agents/resolve.md`). Recorded per field — which rung paid is provenance and an eval signal.
_Avoid_: level, tier, attempt, retry

**Confidence**:
The advisory number on an envelope, in `[0,1]` or `null`. Never an authority, and `null` is the
informative value — it says no judgement was made, which is not a low one.
_Avoid_: score (a score is what the resolver computes over candidates), probability, certainty,
quality

### Prose and claims

**Document**:
A unit of canonical normalized text a quote can grep into: one file you handed us, or one archive
record rendered on its own. Identified by `doc_sha256` over its source bytes, in a span space pinned
by `normalizer_version`.
_Avoid_: source, corpus, paper, input text; "which sample" is answered by *which* document, which is
why a draft carries no subject

**Instruction**:
A document whose role is `instruction` — the only role permitted to touch `processing.*` fields. It
is read, never obeyed: a claim enters as an **Assertion** and code applies precedence
(`docs/adr/0011`).
_Avoid_: prompt, command, directive, config; a `reference` document (a paper, a README) is the other
role and can never reach the recipe

**Span**:
Where in a document a claim came from — `{doc_sha256, quote, char_start, char_end}`. The model
supplies the quote only; code computes the offsets, so a fabricated span fails closed rather than
false-rejecting (`docs/adr/0008`).
_Avoid_: window (a window is a base range inside a read), citation, location, offset

**Variant span**:
A range of an *exemplar*'s text where the near-identical records it stands for *disagree* — marked
in place, never spliced out, so the model still reads coherent prose. Neither a **Span** (that is
where a claim came *from*) nor the **Declared** mark sitting beside it on the same document: a
declared span makes `verify` **refuse** a quote, a variant span decides whether a verified claim may
*fan*. One field per question asked of a range. Argued once in
[ADR-0031](docs/adr/0031-a-collapsed-citation-is-regenerable-only-from-the-record-set.md).
_Avoid_: diff, mask, hole, gap; and never for the *reduced* text of a **Collapse**, which is a
document of its own and carries no marks at all

**Quote**:
The verbatim substring a claim rests on. It must grep back into the canonical text *and* entail the
value, or the claim is rejected — R2's hallucination tripwire.
_Avoid_: excerpt, snippet, passage; and never `evidence`, which on an envelope is a list of record
ids

**Entailment**:
The check that a quote *supports* the value pinned to it, not merely that the quote exists. It
catches what span verification provably cannot: a real quote attached to a wrong value.
_Avoid_: relevance, similarity, semantic match; it is vacuous where the value is not a controlled
vocabulary, and it is no defence against a wrong `field`

**Assertion**:
A claim from prose that survived verification — field, value, span, and the two code-owned flags
`span_verified` and `entailment_ok`. It proposes; it is never an authority.
_Avoid_: fact, extraction, annotation, LLM output

**AssertionDraft**:
The model's only structured-output surface: `{field, value, span:{doc_sha256, quote, context?},
llm_confidence}`. It carries no offsets and no `subject` by design — both would be authority with
nothing to check (`docs/adr/0008`).
_Avoid_: proposal, raw assertion, candidate (a candidate is a scored technology)

**Exchange**:
One request and the response it got at the model seam, kept whole: system prompt, user text, returned
text, usage, mode. The unit a transcript is a list of, and the unit a call is counted in — a retry is
its own exchange, because tokens were spent on it.
_Avoid_: call, turn, message, completion, round-trip; a **Document** is what an exchange is *about*,
not the exchange

**Ceiling**:
The most tokens one run may spend at the model seam, counted raw. It bounds what may be **spent**,
not what may be started, it refuses rather than warns, and the bound is *approximate*
(`docs/agents/cli.md`).
_Avoid_: limit, cap, quota, token budget, max tokens (that is one response's output bound); and
never a **Budget**, which bounds one head in bytes and reads

**Plan**:
Which **Document**s one extraction will send, what each will be asked, and the input tokens that
costs — computed before a token is spent. Exact rather than projected: rendering a document is free,
so a plan holds the same send list the paid run uses. `harvest extract --dry-run` is a plan and
nothing else.
_Avoid_: estimate, preview, dry run (that is the flag that prints one), schedule; and never for
`compose`'s output, which is a Snakemake plan

**Collapse**:
Reading several archive records through one **Document**: the runs of one sample become one document,
and near-identical records at one level fold onto one *exemplar* whose non-shared spans are marked.
What a collapse never does is drop a byte. Argued once in
[ADR-0031](docs/adr/0031-a-collapsed-citation-is-regenerable-only-from-the-record-set.md), which
defines *reduced*, *withheld*, and how a claim *fans*.
_Avoid_: dedup (that is byte-equality, and it misses records that differ only in an accession), merge,
cluster, summarize; and never for `reduce_dataset`'s partition into assays, a **Manifest** decision

**Transcript**:
Every **Exchange** of one run, assembled: one system prompt plus N (document, response) pairs, since
the prompt is byte-identical across a run. It is a file — stdout is the result object and a thousand
exchanges cannot ride on it — and the meter that records it never chooses its address.
_Avoid_: log, history, conversation, trace; `logs/usage.json` is the **Ledger** (what a run spent),
this is what it spent it on

**Ledger**:
What one run spent at the model seam — per-document token and mode costs against the **Ceiling** —
written to `logs/usage.json`. The numbers, where a **Transcript** is what they were spent on.
_Avoid_: usage log, receipt, bill; the *meter* is what writes one, never the file

### Deciding the library

**Observation**:
Everything probe reports about one file — composition, segmentation, distinct ratios, header
grammar, integrity — and **no roles**. Deterministic, LLM-free, network-free, cached by content
address (`docs/agents/models.md`).
_Avoid_: probe result, profile, QC report, fingerprint (a fingerprint package ships **Slice**s); and
never a **Metric** — an Observation is read from the bytes *before* anything ran, a metric is what the
finished pipeline reported *after*, and both are measurements about sequencing data; also, an
Observation never "identifies" anything, it reports

**Hypothesis**:
A span-verified assertion handed to `score` as a selector for which onlist to test first and as a
sub-threshold tie-break. It never enters the evidence matrix, un-gates a forbidden cell, or wins a
Conflict (`docs/agents/resolve.md`).
_Avoid_: hint, expectation, guess, prior (the filename prior is a different thing)

**Candidate**:
One technology scored against the bytes, carried with the **Read set** and role assignment that
scored it, the rung that settled each field, and any equivalence members. Ranked, never merged — one
per **Spec**, so a chemistry never competes with itself.
_Avoid_: match, hit, prediction, best guess

**Role**:
What a read *is* within a chemistry — a KB spec's read id (`R1`, `bc`, `cdna`). An open label the
spec names, never a filename claim: `_1/_2` is a weak prior that can only break an exact byte-tie.
_Avoid_: read type, mate, file kind, R1/R2 as identity

**Read set**:
One complete set of a **Spec**'s roles that a **Role assignment** may fill. A spec declares a maximal
set and may name subsets of it, so a chemistry covers the paired-end and single-end configurations its
protocol publishes as one entry rather than two (`docs/adr/0029`). A subset of read ids, never a
second declaration of a read.
_Avoid_: configuration (a config is what `compose` emits), layout (a layout is the KB's declared
structure), mode, variant, flavour; and never for how many files a deposit happens to hold

**Role assignment**:
The injective map from a **Read set**'s roles to the dataset's files that scored best — *total* over
that set, so an unfilled role is an invalid assignment and never a tolerated gap. Half of one
decision, the other half being **Chemistry** (`docs/adr/0006`).
_Avoid_: mapping, pairing, demultiplexing, layout (a layout is the KB's declared structure)

**Chemistry**:
The library construction the bytes are evidence for, named by KB spec ids. Carried as an equivalence
class, because CI-proven twins (v3 and v3.1) are recorded together rather than chosen between, and it
is the one judgement the library section's envelope belongs to (`docs/adr/0006`).
_Avoid_: kit, platform, protocol, version; `technology` is the field name in code — prefer chemistry
in prose

**Family term**:
A chemistry claim naming a family **Spec** rather than a leaf — "10x 3'", not `10x-3p-gex-v3`. It
*narrows*: an observed leaf inside that node's subtree satisfies it, so it is agreement and never a
**Conflict** (`docs/adr/0020`). A *sibling* claim is not one — there the bytes decide the leaf and the
discarded claim is kept as a resolved Conflict.
_Avoid_: partial match, vague chemistry, family-level conflict; a string naming no node at all is not
a weak family term, it asserts nothing and is rejected

**Spec**:
One node of the KB — a directory holding `spec.yaml` (read layout, onlist refs, detection signature,
backend params) plus a `README.md`. Executable and self-testing: `kb roundtrip` proves it recovers
what it declares (R8).
_Avoid_: config, definition, rule, profile; the *schema* is what validates a spec, not the spec
itself

**Backend params**:
A spec's parse half — how to *read* reads (`soloType`, CB/UMI offsets, whitelist, strand, barcode
match mode). Decided by bytes and never instructable; what to *count* belongs to the recipe, and the
two key sets are disjoint (`docs/adr/0011`). A flag whose value varies with neither the chemistry nor
the user's intent is the workflow module's, as a literal, and is in neither (`docs/adr/0022`).
_Avoid_: settings, options, aligner flags; the CellRanger-parity knobs are module literals and belong
to no artifact

**Onlist**:
A barcode whitelist, identified by the *set* of barcodes it holds rather than by the file carrying
them. Consulting one is what rung 3 costs; a pipeline builds one by rule and deletes it,
never storing it expanded (`docs/adr/0015`).
_Avoid_: allowlist, barcode file, reference list; "whitelist" names the vendor's file, `onlist` is
the spelling on the wire

**Confusable**:
A declared pair of specs the cheap rungs cannot separate, naming the mechanism that can. Declaring
it is mandatory — CI fails a pair that collides at rungs 0-2 in silence (`docs/agents/kb.md`).
_Avoid_: ambiguous, similar, overlapping, competing

**Answerable**:
Whether *these bytes* could have answered a signature test at all. An unanswerable test leaves the
support numerator **and its normalizer**, because no chemistry could have got an answer there. Not
readable off the `ABSTAIN` outcome (`docs/research/support-normalizer-asymmetry.md`).
_Avoid_: inapplicable (that is the **Read set** rule below), unmeasured, missing, N/A

**Unconfirmed**:
A test the bytes *were* willing to answer and we could not ask — the whitelist was not registered or
would not materialize. It keeps its full weight in the normalizer: a spec is never credited for
evidence nobody was able to check (`docs/research/support-normalizer-asymmetry.md` §2).
_Avoid_: unavailable, failed, missing whitelist; "the onlist abstained" names the outcome, not this

**Inapplicable**:
Reserved for the **Read set** rule: a signature test addressed to a read the *active* set does not
carry has no cell at all (`docs/adr/0029`). The same arithmetic as an unanswerable test, reached from
the declaration rather than from the bytes.
_Avoid_: using it for either of the two above

**Processing-equivalent**:
Two specs whose canonical backend params — onlists resolved, role placement included — are
byte-equal: they parse reads identically (`docs/adr/0011`). A tie between them is recorded as an
equivalence class and asks zero questions.
_Avoid_: identical, interchangeable, duplicate; "benign" is the **Conflict** status this produces,
not the relationship

**Processing-divergent**:
Two confusable specs that would parse reads differently. A tie between them is the one trigger that
escalates past rung 3, and only after metadata fails to settle it.
_Avoid_: incompatible, contradictory, mutually exclusive

### Refusal

**Blocker**:
A structured refusal carrying a remedy and a subject (a basename, a dotted path, a dataset id —
never a path). Always fatal, exit 3: no human answer clears it (`docs/adr/0013`).
_Avoid_: error, failure, hard warning; severity is the type, never a field to branch on

**Warning**:
A non-blocking advisory, exit 0 — what the metadata resolver emits once it has *decided* a
sample-attribute disagreement, including deciding to leave it null (`docs/adr/0010`).
_Avoid_: soft error, minor blocker, notice; spelled `ValidationWarning` in code so it never shadows
the builtin. Not every non-blocking advisory is one — evidence a *finished pipeline* raises against a
decision already compiled is an **Alert**, which no exit code carries at all

**Conflict**:
A surfaced disagreement between two or more positions on one field, each with its own basis. An
`observed`↔`asserted` one is never auto-picked: it blocks at exit 4 until a human confirms
(`docs/adr/0010`).
_Avoid_: mismatch, discrepancy, error, disagreement (unqualified); and never for a finished
pipeline's numbers disagreeing with a decision already hashed — nothing arbitrates that, so it is an
**Alert** and not a Conflict raised late

**Question**:
An ambiguity code has already narrowed to a closed list of options, addressed to a human at exit 4.
Asked only where the answers are exclusive — an ambiguity whose every answer we can afford to emit
is dissolved, not asked (`docs/adr/0012`).
_Avoid_: prompt, query, clarification, ask

**Alert**:
Post-run evidence contradicting a decision already made and already hashed — a threshold comparison
over the **Metric**s a finished **Compiled pipeline** wrote, naming the decision it implicates and
the value that decision currently carries. The compiler's one backward edge, and advisory by
construction (`docs/adr/0026`). Firing on every landed sample and firing on one are different
claims: the first implicates the decision, the second one well.
_Avoid_: **Conflict**, **Warning**, **Blocker**, **Question** — all four are compile-time, decided
before or while a manifest exists and carried by an exit code, and an alert is none of them arriving
late; also issue, diagnostic, QC failure, recommendation

**Absent**:
Why a benchmark case produced no grade: the corpus does not hold its package. A standing fact about
the corpus, published rather than hidden, and the `skip_kind` that never poisons a rate
(`docs/adr/0018`). Its counterpart is **unavailable** — the package exists and this run could not
reach it, which is transient. Two states, and folding them together is how a case sits out of the
corpus for a release behind a word that reads as temporary.
_Avoid_: skipped, missing, failed for either alone — each names both and so distinguishes neither

### Artifacts

**Manifest**:
`manifest.yaml` — what the data IS: library + experiment, machine-independent, write-once,
content-hashed. A finished assay is immutable, so it is never rewritten under a change of intent
(`docs/adr/0004`).
_Avoid_: config, metadata file, sample sheet, dataset description

**Recipe**:
`processing.yaml` — what to DO with a manifest: genome, aligner, what to count, environment,
resource hints. Plural per dataset and sparse; empty is legal, and unpinned it is a template
(`docs/adr/0004`).
_Avoid_: config, settings, pipeline, params (`backend.params` is the disjoint parse half);
`ProcessingManifest` is the class, "recipe" is the word

**Dataset hash**:
The sha256 over exactly the manifest's `library` and `experiment` sections. Invariant under every
change of intent — that invariance is what lets one manifest compile many ways (`docs/adr/0004`).
_Avoid_: manifest hash (the provenance block carries the hash and sits outside it), checksum,
dataset id

**`run_id`**:
`H(dataset ⊕ processing ⊕ kb ⊕ workflow)` — the identity of one *pairing*, computed at compile time
and stored inside neither input (`docs/adr/0005`, which holds the formula in full). It names the
pipeline directory, so two recipes over one dataset cannot overwrite each other.
_Avoid_: build id, job id, provenance id; and never for `RunResolution.run_id`, which is a
filename-derived **Run** key

**Compiled pipeline**:
One `(manifest, recipe)` pairing made runnable — the directory `compose` writes (the Snakefile, its
config, the units table, and a copy of the **Workflow module**), and the execution of that Snakefile.
One word for both, because the directory is where the execution's outputs land, and one module owns
every question about either (`docs/adr/0024`).
_Avoid_: **Run**, which is one *sequencing* run, and `run_id`, which names the pairing rather than
its execution; also build, job, workflow run

**Metric**:
One number a finished **Compiled pipeline** wrote, with the words a human reads it by, the stage it
speaks about, and a **level** — `ok`/`warn`/`bad`, or `none` where no bar is defensible, which is a
verdict and not a missing value. The module that wrote the artifact decides the level (`docs/adr/0025`).
_Avoid_: stat, QC number, score, grade; and never an **Observation** — see that entry for the
before/after split. One sample's are its *sample stats*, one execution's its *pipeline stats*.

**Counting grid**:
The two axes a plate assay's matrices are crossed from: the **counting unit** (a UMI — a deduplicated
molecule — or a read, which has no UMI and cannot be deduplicated) against the **feature region**
(exon, intron, or the two **combined**). Six cells, five of them materialised as matrices, because a
matrix earns its place by being **non-derivable**: the combined UMI figure is a third deduplication
over both populations at once and no arithmetic on the other two recovers it, while the combined read
figure is exactly their sum and is left to whoever wants it. Its 3-column half is the grid
`docs/research/smartseq3-analysis-practice.md` heads *exonic | intronic | combined*, which is where
the word comes from.
_Avoid_: `inex` (zUMIs' spelling) and `U`/`UE`/`UI`/`RE`/`RI` (the reference tool's) — both need the
tool read first; also "spliced/unspliced", which is Velocyto's question about a **Read** and not this
one about where a fragment landed

**Fan-in artifact**:
The deliverable a **Compiled pipeline** produces **once for the whole deposit** rather than once per
**Sample** — one file, carrying one row per sample. Dataset-scoped as a *file* and sample-scoped as
*data*, and that split is the whole term: it has no sample in its path, so nothing addressed per
sample can find it, while everything inside it still belongs to one sample. A **Workflow module**
declares it (`fan_in_artifact`), which is what lets the rule that writes it and the readers that find
it share one name. Only a **Pre-demultiplexed** plate has one today: 1440 cells counted in one job
into one `.h5ad`.
_Avoid_: merged output, aggregate, summary (all three read as a *derived* second copy — a fan-in
artifact is the primary result, produced once); combined file; and never for the **Manifest**, which
is dataset-scoped and holds no per-sample rows of counts

**Workspace**:
The user's project root, and the `seqforge/` state directory under it. No leading dot, because it
holds the manifest and the Snakefile — it is output, not cache; only `seqforge/cache/` is safe to
delete.
_Avoid_: `.seqforge`, output dir, cache dir, build dir

**Workflow module**:
A hand-written, versioned Snakemake module under `workflows/`. `compose` selects one and copies it
beside the emitted `Snakefile`, so a run directory reproduces after it is moved; nothing generates
rule source (R1).
_Avoid_: template, generated rules, pipeline code, snippet
