# Context map

`seqforge` compiles `(FASTQ files) + (unstructured metadata)` into a validated `manifest.yaml` and a
runnable Snakemake pipeline. Its vocabulary splits by bounded context: each file below is the
glossary for one part of the source tree, and the shared kernel at the bottom holds the words every
context uses. Rules live in `AGENTS.md`; this map and the six files it lists are a glossary and
nothing else.

**Use these words.** When your output names a domain concept — an issue title, a refactor proposal,
a hypothesis, a test name — use the term as defined, not a synonym an entry lists under *Avoid*. A
concept defined nowhere is a signal either way: usually it is language the project does not use,
occasionally a real gap worth adding.

**Two vocabularies, and they do not mix.** Domain terms come from these files. Architecture terms —
module, interface, depth, seam, adapter, leverage, locality — are fixed, and "component", "service",
"API" and "boundary" are not substitutes for them.

## Contexts

- [Probe](./src/seqforge/probe/GLOSSARY.md) — covers `probe/`, `fingerprint/` and part of `io/`:
  reading bounded bytes, and what a file is without reading it
- [Harvest](./src/seqforge/harvest/GLOSSARY.md) — covers `harvest/`: prose, the claims read out of
  it, and what one run spent reading it
- [Knowledge base](./src/seqforge/kb/GLOSSARY.md) — covers `kb/`, `e2e.py` and part of `io/`: what
  a chemistry declares, and how a claim about one is ranked
- [Resolve](./src/seqforge/resolve/GLOSSARY.md) — covers `resolve/`, `recordset.py`, `project.py`
  and parts of `io/` and `manifest/`: which library and which sample, from bytes and from records
- [Compose](./src/seqforge/compose/GLOSSARY.md) — covers `compose/`, `workflows/`, `report/`,
  `pipeline.py`, `workspace.py` and part of `manifest/`: the recipe, the compiled pipeline, and
  what it reported
- [Study](./src/seqforge/study/GLOSSARY.md) — covers `study/`: a study's bounds and its reading,
  and the fixed set of studies they are built on

## Folders that two or more contexts share

`io/` and `manifest/` hold the words of more than one context. Each module, or each function where a
module is split, uses one context's words:

- `io/` — Probe's words in `sra.py`, `benchmark.py`, and `peek` and `probe_remote` in `remote.py`.
  Resolve's words in `archive.py`, `taxonomy.py`, and `resolve_accession` in `remote.py`. The Knowledge base's words in `onlist.py`.
- `manifest/` — Resolve's words for the dataset manifest: `fill_manifest` in `fill.py` and
  `validate_manifest` in `validate.py`. Compose's words for the recipe and for the hashes that pair
  it with a manifest: `policy.py`, `instruct.py`, `hash.py`, `fill_processing` in `fill.py` and
  `validate_processing` in `validate.py`.

Every context shares the paths below, and none of them has words of its own:

- `cli/` — one module per command group, each in the words of the package it fronts
- `models/` — the Pydantic models that every stage exchanges
- `hooks/` — the agent hook guards, which turn the rules into checks
- `assets/` — the style tokens that the workspace report and the eval report share
- `gateway.py` — the lab gateway's client, the ceiling check and the refusals for a setup a command
  cannot use, which harvest, `run`, `eval run` and the passes share. Its words are liulab-llms's
- `__init__.py` and `py.typed` at the top of the package

The eval harness, `evals/` inside the package, has no glossary. It runs the stages of every context
over the cases in the repo's top-level `evals/`, and its own words, such as case, tier and false
accept, are explained in its code.

## The run engine's words

`passes/` is not a context of its own. It starts passes of seqforge's unit kinds on the run engine,
liulab-llms, whose [glossary](https://github.com/liuhlab/liulab-llms/blob/main/GLOSSARY.md) defines
its words: Unit, Unit kind, Pass, Task, Settled, Unsettled, Refused, Dropped, Unfinished, Held,
Inputs tag, Stamp, Review, Review queue, Gateway, Meter, Ceiling and Waiting call. Nothing here
redefines them. A pass is never a *run*, *batch*, *job* or *round*. Harvest makes its model calls
through the same Gateway and a Meter of its own; its **Exchange** is narrower than liulab-llms's, and
the harvest glossary says how.

## Relationships

- **Probe → Resolve**: probe reads bytes under a budget and reports what it saw, naming no roles;
  the byte resolver is what turns that report into a chemistry and a role assignment.
- **Knowledge base → Resolve**: every candidate the byte resolver ranks is one KB spec. The KB
  declares what a chemistry is; resolve decides which one these bytes are.
- **Harvest → Resolve**: harvest turns prose and archive records into **Assertion**s. The byte
  resolver takes at most a hypothesis from them and never lets one into its evidence; the
  wrong-paper guard checks a staged paper's claims against the records.
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
**Basis** for a value code derived rather than read or was told: every policy default.
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
A non-blocking advisory, exit 0 — what a resolver emits once it has *decided* something a reader
should still see, such as a submitted file whose size disagrees with the archive's.
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
