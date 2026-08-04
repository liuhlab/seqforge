# 25. The module that writes a QC artifact owns reading it, and a registry names who does not

Date: 2026-08-03

## Status

Accepted.

## Context

`seqforge report` renders what the compiler decided. Once the user submits the composed Snakefile,
there is a second thing on disk worth rendering — what the **Compiled pipeline** produced: mapping
rates, barcode validity, cells, saturation. Those numbers only mean something with a verdict attached.
"Valid barcodes below 0.5" is not a low number, it is *the* signature of a wrong chemistry call or a
barcode read handed to STAR as the cDNA — a real run of ours failed exactly that way and a human
caught it by eye, months later.

That verdict is **domain knowledge about a tool**, and the question this record settles is where it
lives. Three shipped modules write three genuinely different artifacts: a gzipped JSON bundle
`rule qc_bundle` assembles, a fragments summary chromap's post-processing writes, and — for bulk —
STAR's own `Log.final.out`, which no seqforge rule declares at all.

**The obvious reading is that this belongs to the report**, and a reader will reach it from the same
evidence: the report is the only consumer, the thresholds are only ever rendered, and one
`if module == "map/starsolo"` in the collector ships the feature this afternoon. It is also how the
report acquires a STARsolo vocabulary it spent its whole design avoiding — `report/model.py` types no
aligner's fields on purpose — and how a fourth aligner lands, reports nothing, and nothing fails to
say so. This repo has already paid for that shape: `read_layout_kind` and `param_block` both exist
because a per-module branch fell through silently for a module nobody had added to it.

## Decision

**Who writes a format owns how to read it, and a registry names every module that does not.**

Three pieces, and the split between them is the whole decision:

| module | is | knows |
| --- | --- | --- |
| `workflows/metrics.py` | the **leaf vocabulary** — `Metric`, `SampleStats`, `PipelineStats`, the formatters and `grade` | nothing about any tool; imports nothing else from `workflows` |
| `workflows/qc.py` (and its peers) | the **adapter**, beside the writer | one tool's keys, and its thresholds |
| `workflows/stats.py` | the **registry** — `StatsSpec(artifact, read)` plus `MODULES_WITHOUT_STATS` | which module has an adapter, and which has said "not yet" |

The vocabulary being a leaf is what lets the adapter live beside the writer without a cycle: `qc.py`
imports `metrics`, `stats.py` imports both, and nothing points back. A bundle key and the lookup that
resolves it then change in one file, or fail in one file.

`MODULES_WITHOUT_STATS` is **not** an empty formality. It ships holding `map/star` and `map/chromap`,
because the seam landed with the single-cell adapter alone — which is precisely the case it exists
for: a module says "not yet" out loud instead of being silently absent from every report.

Two rules the adapters keep. **A metric with no defensible threshold is ungraded** (`level="none"`)
rather than given an invented bar — an estimated cell count means nothing without knowing what was
loaded. **A metric the artifact does not carry is absent from the page**, never a zero: a zero is a
number a reader acts on, and the tool did not write it.

## Why not the metrics vocabulary in the models tree

`models/` is where this project's types live, so it is the first place to reach for, and the argument
against it is not "these are internal" — it is that `models/` means something specific here. Every
type in it is exported by `schema export`, is the single source of truth for a **wire** shape, and is
validated at a boundary something untrusted crosses: an LLM's output, a manifest on disk, a CLI's
stdout object.

Apply the deletion test to each candidate home and they answer differently. Delete
`workflows/metrics.py` and exactly two packages notice — `workflows/`, which produces `Metric`s, and
`report/`, which renders them; the type has one producer and one consumer and crosses no validated
boundary. Delete a `models/metrics.py` and `schema export`'s output changes shape, `SCHEMA_MODELS`
grows, and every consumer of the exported schemas has to be told — a blast radius out of all
proportion to a page's column set. A type whose only job is to get a number from the code that
computed it to the code that draws it is a function's return type, not a schema, and publishing it as
one makes a rendering vocabulary part of seqforge's public contract.

The second cost is the one that decided it. Put the vocabulary in `models/` and the *thresholds* end
up a package away from the tool knowledge that justifies them — `ok=0.75` for valid barcodes is a fact
about STARsolo's whitelist matching, and it belongs in the file that also knows which
`Summary.csv` row carries it. Splitting the two is how a renamed bundle key keeps writing and quietly
stops reading.

## Why not one branch in the report collector

It is smaller, it needs no registry, and it ships the single-cell case immediately. It also puts
STARsolo's vocabulary inside `report/`, which is modality-general by construction, and it fails in the
one direction that cannot be seen: a fourth aligner is registered, falls through the `if`, reports
nothing, and an empty results section is byte-identical to a pipeline that has not started. Nothing
raises and nobody is told. The registry makes that a *build-time* failure instead — a new module is
either given a reader or named as reporting none, and a test goes red on the day it is registered.

## Why the spec carries a filename rather than a suffix

`{sample}.<suffix>` is the shorter convention and it covers the two artifacts seqforge's own rules
write. It cannot express the third: `map/star` has no QC bundle rule at all, and reports from
`Log.final.out` — a file STAR writes unasked, which carries no sample name and which nothing in
`star.smk` declares or deletes. Reading it as-is means the bulk pipeline can report with **no new
rule**, hence no `WORKFLOW_VERSION` bump, hence no `run_id` invalidation and no reprocessing of
anything already compiled. A suffix convention would have made that inexpressible and the artifact
would have been re-derived by a rule instead, at the cost of recompiling every dataset.

## So in code

**Registering a Workflow module means either adding a `StatsSpec` beside the code that writes its
artifact, or adding the module to `MODULES_WITHOUT_STATS` — there is no third option, and no silent
one.** The
reader function goes in the module that owns the format, keeps a pure `Mapping -> SampleStats`
function underneath it so its thresholds are testable against a literal dict, and imports its
artifact's name rather than re-spelling it. Grade a metric only where a bar is defensible; leave a
metric out where the artifact is silent. And when you widen what the reader tolerates, widen it for
**bytes** only: `KeyError`/`TypeError` are what a bug in a metric table raises, and catching them
turns a logic error into a per-sample note that reads like bad input.

**Enforced by.** `test_every_registered_workflow_module_either_reports_or_says_it_does_not` and
`test_the_registry_guard_can_actually_catch_drift_in_both_directions` (`tests/test_workflows.py`) are
the drift guard and the proof it fires — a module in neither list, and a spec for a module that no
longer exists. `test_the_bundle_the_writer_produces_is_the_one_the_reader_looks_up` drives the real
writer rather than a literal, so a renamed bundle key costs a row here instead of silently costing a
column on the page; `test_a_bug_in_a_metric_table_is_raised_and_not_filed_as_a_corrupt_artifact` and
`test_one_unreadable_artifact_costs_its_own_row_and_not_the_whole_pipeline` hold the two halves of
what the reader may swallow apart. `test_a_key_the_artifact_does_not_carry_becomes_an_absent_metric_never_a_zero`
holds the absence rule.

## Consequences

- The report gains a Results tab and no tool vocabulary. `report/model.py` re-exports
  `PipelineStats` as the type of one field and types none of its contents; the renderer picks a
  colour from a `Level` and never a threshold.
- **Which samples finished is answered from the composed config's own list**, through
  `CompiledPipeline` ([0024](0024-one-owner-for-the-compiled-pipeline.md)), never by globbing the
  results tree — a listing can say what landed and can never say what is missing, so a partial
  **Compiled pipeline** read that way is indistinguishable from a complete one.
- `WORKFLOW_VERSION` is untouched. Nothing here changes a rule, so no `run_id`
  ([0005](0005-run-id-is-the-pairing.md)) is invalidated and nothing already compiled is reprocessed.
  `QC_SUFFIX` is public for the shipped `.smk` to adopt on its next edit; until then the module is
  still a second owner of that string, which is a known and deliberately deferred gap.
- The single-cell rollout leaves two modules on `MODULES_WITHOUT_STATS`, each carrying the ticket that
  lands it. That is the list working, not the list failing — but it is also a page that says less for
  a bulk or ATAC dataset than for a single-cell one, and that asymmetry is visible to users now.
- Nothing here is enforced about the *thresholds*. That a bar is defensible is a review obligation and
  a corpus question; what is mechanised is only that an undefensible one is declared as such.
