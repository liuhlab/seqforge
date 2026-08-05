# 34. A user-written record set declares structure, never a fact

Date: 2026-08-05

## Status

Accepted. Settles the design question [#270](https://github.com/liuhlab/seqforge/issues/270) raised
against [0011](0011-closed-instructable-surface.md) and
[0012](0012-produce-every-answer-rather-than-ask.md), and supplies the input
[0027](0027-a-run-spans-its-lanes.md) deliberately stopped short of.

## Context

[0027](0027-a-run-spans-its-lanes.md) made a run lane-blind and stopped there, because the level
above is not reachable from a filename. A library resequenced for saturation is `_S3` where batch one
was `_S1`, and `_S<n>` is the only token separating two libraries on one flowcell; a library split
across two flowcells has a different flowcell id in every name. Both compile as two samples at
partial depth, exit 0. Deposited data is unaffected — `ancestor(run, "sample")` joins several `SRR`
under one `SRX` under one BioSample — so the exposure is entirely in-house pre-deposit data, which
[0010](0010-two-resolvers-one-blocks-one-warns.md) calls the normal case rather than the degraded one.

The decided direction is to **supply the record rather than guess harder at the name**. The machinery
is already there: `ArchiveRecord.filenames` exists precisely to *"join a record to a file whose name
no longer contains the accession"*, `ancestor()` is cycle-bounded for *"an archive bug, or a
hand-written one"*, and `--records <path>` is already a flag on `manifest fill` and `run`.

**The obvious reading is therefore "accept the existing type and ship it", and that is what this file
exists to refuse.** An `ArchiveRecordSet` carries `attributes`, and `_basis_for` grants `asserted` to
any claim a record makes about its own sample. [0010](0010-two-resolvers-one-blocks-one-warns.md)
justifies that precedence on one sentence — *"a record's typed slot for a sample is a declaration
**about that sample**"* — and that sentence is true of an archive and false of a human typing YAML.
Accept attributes here and a line with no quote, no span and nothing that greps back silently
outranks a harvested claim that has all three. `experiment` is inside `dataset_hash` and the manifest
is never rewritten, so the resulting wrong attribute is permanent.

## Decision

**A `source: user` record set declares structure, never a fact.**

| | carries | does not carry |
|---|---|---|
| **shape** | `level`, `id`, `parent`, `filenames` | `attributes`, `free_text` — rejected at parse |
| **levels** | `run`, `sample` | `experiment` (inert: nothing reads it), `project` |
| **answers** | which files compile together | what the sample was |

- **It loads as `ArchiveRecordSet` with `source: "user"`.** `source` already records where a set came
  from; the container widens, the transcript claim moves onto `source`, and no second near-identical
  model is minted.
- **Two levels, `run → sample`.** `_join` reads exactly these. A `source: user` sample id is a
  **grouping key** — the thing that becomes one `<sample>.h5ad` — and not a claim about a specimen
  (`CONTEXT.md`, **Sample**).
- **Fusing runs that filenames would have separated is a `Warning`, never a `Blocker`.** It names the
  runs fused and that the grouping was declared rather than observed.
- **`seqforge records new <dir>` drafts the safe grouping.** One sample per run — applying it unedited
  is a no-op — with comments naming the run pairs differing only in `_S<n>` or only in flowcell id.
  It makes no guess; it points at the decision.

## Why not permit attributes

Because the whole precedence table rests on their absence. Three further reasons compound it:

1. **[#270](https://github.com/liuhlab/seqforge/issues/270) already rejected this shape one level
   over.** A `path → sample_id` map lost for *"becoming a third source of sample identity"*. Attributes
   here would be a second, unverifiable source of sample *attributes* — the same objection, and it
   would have been re-derived rather than remembered.
2. **R2 has no way to check them.** Every `Assertion` quote must grep back *and* entail its value. A
   YAML attribute has no document to grep, so verification would be vacuous — the defect
   [0021](0021-one-deposit-is-one-source-at-every-layer.md) records under a different name.
3. **The exclusion is self-enforcing downstream.** `_worth_asking` gates on `has_prose`, which is
   `any(ft.text.strip() ...)`, so a set with no free text is invisible to harvest with no guard
   written. `_project_facts` and `_organism` return `None` by paths that already exist. Structure-only
   touches exactly one consumer: `_join`.

A lab that does know its genotypes writes them in a README and harvests them. That path exists, and it
keeps the span.

## Why not the experiment level

`CONTEXT.md` puts a **Library** at an archive's experiment level, so *"these runs are one library"*
reads as an experiment-level claim — and [#270](https://github.com/liuhlab/seqforge/issues/270) said
so. It is inert: `_join` walks `ancestor(run, "sample")`, and **nothing in the tree reads
`at("experiment")`** except `_subject_to_sample`, which walks it only to map down to a sample. Asking
a human to write a level nothing reads is ceremony, and ceremony rots.

Moving the join to `experiment` — so the compile unit is a library and `sample` stays the specimen
honestly — is arguable and is **not** decided here: it regroups the deposited path, which 7 of 18
benchmark cases exercise, and it needs a measurement before it needs an opinion.

## Why this does not open the instructable surface

[0011](0011-closed-instructable-surface.md) closes a **key space**: `backend.params` says how to parse
and is never instructable, the recipe says what to count, and the two are disjoint so *"a user
instruction contradicts the bytes"* is inexpressible. A record set names no key in either. The
stronger reason is structural — [0010](0010-two-resolvers-one-blocks-one-warns.md) mandates the
metadata resolver be handed a `FileIdentity` and **never** an `Observation`, so a record set operates
in the one region where probe signal is deliberately excluded. There is nothing there for it to
contradict.

[0012](0012-produce-every-answer-rather-than-ask.md) forbids escalating *"an ambiguity whose every
answer you can afford to emit"* and permits asking *"where the answers are genuinely exclusive — a
genome, an aligner"*. One library or two is genuinely exclusive: the answers are different dataset
hashes and different matrices, and no pass emits both. `records new` also does not ask — it drafts a
file that may be ignored, and ignoring it leaves the filename grouping unchanged.

## Why not block the fuse

Two libraries of the *same* chemistry declared as one sample is the one shape nothing else catches:
gate 3 (`resolve/engine.py`, *"a sample spans two chemistries"*) refuses a fuse across chemistries,
and `blk-record-join-incomplete` refuses a set that leaves files unclaimed, but same-chemistry
same-sample passes both. It is also the direction
[0027](0027-a-run-spans-its-lanes.md) called expensive — *"a single plausible matrix nobody would
notice"*.

Blocking it would refuse the feature's primary use case on first run, and an exit code that fires when
it need not trains callers to route around exit codes
([0013](0013-cli-is-a-machine-interface.md)). Silence makes a mistyped `parent` permanent and
invisible. A `Warning` is [0010](0010-two-resolvers-one-blocks-one-warns.md)'s own instrument for
exactly this — *"every Warning names what disagreed and how it was resolved"* — and it rides into
`validate_manifest` alongside the metadata resolver's, so it survives into the artifact rather than
scrolling past.

The warning is why `source` is load-bearing rather than decorative: fired on archive sets it would
report every normal `SRR`→BioSample fusion.

## So in code

**A record set whose `source` is `user` may declare only `level`, `id`, `parent` and `filenames` — a
loader that lets an attribute through has broken `asserted`, not just widened a schema.** Parse it
with `yaml.safe_load` like both manifests (YAML is a superset of JSON, so one loader takes the
existing `io records` caches and a hand-written file with no extension dispatch). Keep `records new`
in a top-level `records` group, never under `io`: it reads a local directory and touches no network,
and `docs/agents/cli.md` makes `io` the only network surface. Draft one sample per run so applying the
draft unedited cannot change a grouping, and put the `_S<n>` / flowcell candidates in comments.

**Enforced by.** **None exists.** Nothing is implemented yet — this record precedes the build
([#270](https://github.com/liuhlab/seqforge/issues/270)). Until it is, a loader accepting attributes
would be caught by no test: the gate that would notice is a parse-level rejection test plus an
end-to-end case asserting that a `source: user` set carrying an attribute is refused. The eval half is
already expressible — `SpecRecipe.deposit` (libraries × lanes) and the `experiment.n_samples` grade
path both shipped in [#286](https://github.com/liuhlab/seqforge/issues/286), and cases already load a
record set from `<case>/records.json` — so a two-library deposit fused by a committed record set to
`experiment.n_samples: 1`, and the same case without it grading 2, needs no new eval machinery.

## Consequences

- **`CONTEXT.md` gains a `Record set` entry and loses an absolute.** **Archive record** no longer says
  an archive is the only declarer; `source` carries that. **Sample** now states that it is the level
  that fuses runs, and that in a `source: user` set the id is a grouping key rather than a specimen
  claim. The `asserted` precedence in [0010](0010-two-resolvers-one-blocks-one-warns.md) is now
  **conditional on the no-attributes rule** — permit attributes later and that justification breaks
  silently, which is the reason this file exists.
- **#270's first checkbox was largely already built.** `--records <path>` is on `manifest fill` and
  `run` today. What is missing is the validating loader, `records new`, and the fuse warning — not a
  new dataset-side input.
- A **Record set** is an *input*, like the FASTQs and the harvested **Document**s, and not a third
  artifact: [0004](0004-two-artifacts-not-one.md)'s two artifacts are unchanged, and the dataset
  manifest stays write-once.
- **The `source` field becomes semantic.** It was reported and never branched on; the fuse warning is
  the first behaviour keyed to it. A user who forges `source: ncbi-sra` suppresses only their own
  warning, which is a self-inflicted and bounded cost.
- Moving the join to the experiment level stays open, and is a measurement question over the deposited
  path rather than a design one.
