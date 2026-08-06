# Context map

`seqforge` compiles `(FASTQ files) + (unstructured metadata)` into a validated `manifest.yaml` and a
runnable Snakemake pipeline. Its vocabulary splits by bounded context: each file below is the
glossary for one part of the source tree, and the shared kernel at the bottom holds the words every
context uses. Rules live in `AGENTS.md`; this map and the five files it lists are a glossary and
nothing else.

**Use these words.** When your output names a domain concept — an issue title, a refactor proposal,
a hypothesis, a test name — use the term as defined, not a synonym an entry lists under *Avoid*. A
concept defined nowhere is a signal either way: usually it is language the project does not use,
occasionally a real gap worth adding.

**Two vocabularies, and they do not mix.** Domain terms come from these files. Architecture terms —
module, interface, depth, seam, adapter, leverage, locality — are fixed, and "component", "service",
"API" and "boundary" are not substitutes for them.

## Contexts

- [Probe](./src/seqforge/probe/CONTEXT.md) — covers `probe/`, `fingerprint/`, `io/`: reading bounded
  bytes, and what a file is without reading it
- [Harvest](./src/seqforge/harvest/CONTEXT.md) — covers `harvest/`: prose, the claims read out of
  it, and what one run spent reading it
- [Knowledge base](./src/seqforge/kb/CONTEXT.md) — covers `kb/`: what a chemistry declares, and how
  a claim about one is ranked
- [Resolve](./src/seqforge/resolve/CONTEXT.md) — covers `resolve/`, `manifest/`: which library and
  which sample, from bytes and from records
- [Compose](./src/seqforge/compose/CONTEXT.md) — covers `compose/`, `workflows/`, `report/`,
  `pipeline.py`: the recipe, the compiled pipeline, and what it reported

## Relationships

- **Probe → Resolve**: probe reads bytes under a budget and reports what it saw, naming no roles;
  the byte resolver is what turns that report into a chemistry and a role assignment.
- **Knowledge base → Resolve**: every candidate the byte resolver ranks is one KB spec. The KB
  declares what a chemistry is; resolve decides which one these bytes are.
- **Harvest → Resolve**: harvest turns prose and archive records into **Assertion**s. The metadata
  resolver applies precedence over them; the byte resolver takes at most a hypothesis from them and
  never lets one into its evidence.
- **Resolve → Compose**: resolve writes the **Manifest**. Compose pairs it with a recipe — the one
  place the two artifacts meet — and emits a runnable pipeline.
- **Compose → back**: what a finished pipeline measured re-enters as advisory evidence against
  decisions already hashed. The compiler's one backward edge, and it carries no exit code.

## Shared kernel

Thirteen words every context uses. Anything defined here is not redefined in a context file.

**Evidenced**:
The envelope every *interpretive* field travels in — `{value, basis, evidence, confidence, rung}`,
frozen once validated. One judgement gets exactly one envelope.
_Avoid_: wrapper, annotated value, provenance record

**Basis**:
How a value came to be known — a closed set of four: `observed`, `asserted`, `inferred`,
`user_confirmed`. On a recipe it answers a different question: there it records *who decided*.
_Avoid_: source, origin, reliability; provenance (that is the whole envelope, not this field)

**Observed**:
**Basis** for a value read out of the bytes, and the authority the library section defers to:
nothing asserted overrides it, and the disagreement surfaces as a **Conflict** instead.
_Avoid_: measured, detected, empirical, ground truth

**Asserted**:
**Basis** for a value a human or a database *claimed* — a span-verified **Assertion**, or a field
ENA declared. A claim, never a measurement, however authoritative the archive.
_Avoid_: declared, reported, known, metadata-derived

**Inferred**:
**Basis** for a value code derived rather than read or was told: every policy default, and a sample
fact a model read in a dataset-level document.
_Avoid_: guessed, assumed, defaulted

**User-confirmed**:
**Basis** for a value a person chose — a CLI flag or an `--instruction` document, distinguished from
each other only by precedence. Almost exclusively a recipe basis.
_Avoid_: manual, override, approved, human-in-the-loop

**Declared**:
Not a fifth **Basis** — a property of one position *within* a source: the submitter typed this value
into a slot **for this attribute**, rather than a model having read it out of that source's prose.
_Avoid_: as a synonym for **Asserted** — a record's typed slot and a model's reading of that
record's title are both asserted, which is the pair this term separates; also stated

**Confidence**:
The advisory number on an **Evidenced** envelope, in `[0,1]` or `null`. Never an authority, and
`null` is the informative value — it says no judgement was made, which is not a low one.
_Avoid_: score (a score is what the resolver computes over candidates), probability, certainty,
quality

**Blocker**:
A structured refusal carrying a remedy and a subject (a basename, a dotted path, a dataset id —
never a path). Always fatal, exit 3: no human answer clears it.
_Avoid_: error, failure, hard warning

**Warning**:
A non-blocking advisory, exit 0 — what a resolver emits once it has *decided* a disagreement,
including deciding to leave the field null.
_Avoid_: soft error, minor blocker, notice; spelled `ValidationWarning` in code

**Conflict**:
A surfaced disagreement between two or more positions on one field, each with its own **Basis**. An
`observed`↔`asserted` one is never auto-picked: it blocks at exit 4 until a human confirms.
_Avoid_: mismatch, discrepancy, error, disagreement (unqualified); and never a post-run **Alert**,
which arrives after a decision is already hashed

**Question**:
An ambiguity code has already narrowed to a closed list of options, addressed to a human at exit 4.
Asked only where the answers are exclusive — an ambiguity whose every answer we can afford to emit
is dissolved, not asked.
_Avoid_: prompt, query, clarification, ask

**Manifest**:
`manifest.yaml` — what the data IS: library + experiment, machine-independent, write-once,
content-hashed. A finished assay is immutable, so it is never rewritten under a change of intent.
_Avoid_: config, metadata file, sample sheet, dataset description
