# seqforge

A compiler from `(FASTQ files) + (unstructured metadata)` to a validated `manifest.yaml` and a
runnable Snakemake pipeline. This file is the **glossary** — the words the project uses and the
synonyms it avoids. Rules live in `AGENTS.md`; the reference behind each area lives in `docs/agents/`,
and one decision per file in `docs/adr/` (indexed by module in `docs/adr/README.md`). Nothing else
belongs here: a term one of those files already argues at length appears below as a one-line gloss and
a pointer, because two prose definitions of one term is the failure this file exists to prevent.

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

**Record**:
One FASTQ entry: four lines — header, sequence, plus, quality.
_Avoid_: entry, line, sequence (a sequence is only the second line); what an archive declares is an
**Archive record**, never a record

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
identifies. Its **Chemistry** is the only `Evidenced` field describing it; assay, read layout and
per-file roles all follow from that single decision (`docs/adr/0006`).
_Avoid_: dataset (a dataset is the files you were handed), experiment, prep; `assay` is the same
answer in EFO's vocabulary, not a second fact

**Sample**:
A **biological specimen** — what NCBI's BioSample describes. Never a read of bytes. The metadata
resolver answers "which sample is each file from"; the byte resolver never sees one.
_Avoid_: specimen; and never use "sample" for a head. `StreamSample`/`probe_sample`/`sample_fastq_*`
are legacy spellings of the byte sense, being retired.

**Run**:
One sequencing run — the grouping `resolve/group.py` derives from filenames. With no archive record,
that grouping *is* the sample identity.
_Avoid_: lane, experiment (both are narrower archive concepts); and never for the execution of a
composed Snakefile, which is a **Compiled pipeline**

**Archive record**:
What an archive *declared*, transcribed at four levels — project, sample, experiment, run. A
transcript, never an interpretation, and optional: most sequencing data never had an accession, and
that absence is the normal case rather than a degraded one (`docs/adr/0010`).
_Avoid_: metadata (too broad), SRA entry, database row; and never **Record**, which is four lines of
FASTQ

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
Inside one source it wins, whatever the reading says, and the reading is named in a **Warning**
(`docs/adr/0021`). One layer earlier the same fact marks a free-text span byte-equal to a typed
column, and a quote wholly inside one is refused rather than read as prose.
_Avoid_: as a synonym for **Asserted** — a record's typed slot and a model's reading of that record's
title are both asserted, which is precisely the pair this term exists to separate; also stated,
declared-by-the-archive, structured (that is the half of a **Record**, not this)

**Rung**:
The escalation-ladder step `0..7` that settled one field — 0 metadata, 2 bytes and geometry, 3 an
onlist check, 7 ask a human. Recorded per field, because which rung paid for an answer is provenance
and an eval signal (R9).
_Avoid_: level, tier, attempt, retry; rungs 4-6 are unbuilt, so nothing sits between 3 and 7

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
The most tokens one run may spend at the model seam. Counted raw — fresh input, cached input, cache
writes and output all count, because a ceiling is a backstop and not a price. It bounds what may be
**spent**, not what may be started: a request's estimated cost is reserved before it is issued and
reconciled against the real usage when it returns, so the request the remaining budget cannot cover
is refused **un-issued** and carries a `TOKEN_CEILING_EXCEEDED` **Blocker** at exit 3. A ceiling that
only warns is a number nobody sets. The bound is *approximate* and must not be described otherwise —
a response's token count is unknowable until it returns, so a run may finish a little over. Not a
**Budget** — a budget bounds one head in bytes and reads, a ceiling bounds a whole run in tokens, and
neither substitutes for the other.
_Avoid_: limit, cap, quota, token budget, max tokens (that is one response's output bound, per call)

**Plan**:
Which **Document**s one extraction will send, what each will be asked, and the input tokens that
costs — computed before a token is spent. Exact rather than projected: rendering a document is free,
so a plan holds the same send list the paid run uses. `harvest extract --dry-run` is a plan and
nothing else.
_Avoid_: estimate, preview, dry run (that is the flag that prints one), schedule; and never for
`compose`'s output, which is a Snakemake plan

**Transcript**:
Every **Exchange** of one run, assembled: one system prompt plus N (document, response) pairs, since
the prompt is byte-identical across a run. It is a file — stdout is the result object and a thousand
exchanges cannot ride on it — and the meter that records it never chooses its address.
_Avoid_: log, history, conversation, trace; `logs/usage.json` is the **ledger** (what a run spent),
this is what it spent it on

### Deciding the library

**Observation**:
Everything probe reports about one file — composition, segmentation, distinct ratios, header
grammar, integrity — and **no roles**. Deterministic, LLM-free, network-free, cached by content
address (`docs/agents/models.md`).
_Avoid_: probe result, profile, QC report, fingerprint (a fingerprint package ships **Slice**s); and
an Observation never "identifies" anything, it reports

**Hypothesis**:
A span-verified assertion handed to `score` as a selector for which onlist to test first and as a
sub-threshold tie-break. It never enters the evidence matrix, un-gates a forbidden cell, or wins a
Conflict (`docs/agents/resolve.md`).
_Avoid_: hint, expectation, guess, prior (the filename prior is a different thing)

**Candidate**:
One technology scored against the bytes, carried with the role assignment that scored it, the rung
that settled each field, and any equivalence members. Ranked, never merged.
_Avoid_: match, hit, prediction, best guess

**Role**:
What a read *is* within a chemistry — a KB spec's read id (`R1`, `bc`, `cdna`). An open label the
spec names, never a filename claim: `_1/_2` is a weak prior that can only break an exact byte-tie.
_Avoid_: read type, mate, file kind, R1/R2 as identity

**Role assignment**:
The injective map from a spec's roles to the dataset's files that scored best. Half of one decision,
the other half being **Chemistry** — which is why only chemistry carries an envelope
(`docs/adr/0006`).
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
them. Consulting one is what rung 3 costs (~100 ms); a pipeline builds one by rule and deletes it,
never storing it expanded (`docs/adr/0015`).
_Avoid_: allowlist, barcode file, reference list; "whitelist" names the vendor's file, `onlist` is
the spelling on the wire

**Confusable**:
A declared pair of specs the cheap rungs cannot separate, naming the mechanism that can. Declaring
it is mandatory — CI fails a pair that collides at rungs 0-2 in silence (`docs/agents/kb.md`).
_Avoid_: ambiguous, similar, overlapping, competing

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
the builtin

**Conflict**:
A surfaced disagreement between two or more positions on one field, each with its own basis. An
`observed`↔`asserted` one is never auto-picked: it blocks at exit 4 until a human confirms
(`docs/adr/0010`).
_Avoid_: mismatch, discrepancy, error, disagreement (unqualified)

**Question**:
An ambiguity code has already narrowed to a closed list of options, addressed to a human at exit 4.
Asked only where the answers are exclusive — an ambiguity whose every answer we can afford to emit
is dissolved, not asked (`docs/adr/0012`).
_Avoid_: prompt, query, clarification, ask

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
