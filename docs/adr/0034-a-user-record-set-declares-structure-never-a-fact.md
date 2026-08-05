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
3. **The exclusion needs no new *decision* downstream — though it did need one guard, and this file
   was wrong about that.** `_worth_asking` gates on `has_prose`, which is `any(ft.text.strip() ...)`,
   so a structure-only set renders no document and the plan comes back empty; nothing is asked
   because there is nothing askable. What this record originally claimed follows — that such a set is
   *"invisible to harvest with no guard written"* — did not. An empty plan was a state `harvest
   extract` could not survive: with no documents the loop that builds the extractor never ran and the
   verify step asserted on the `None` it was left holding, so the feature's primary use case ended in
   a traceback, and `run` (which counts `--records` as prose, correctly) took the whole compile down
   with it. **That is a plan-with-no-documents defect and not an attributes one** — an archive
   transcript whose records carry no prose reached the identical state, and always had — and it is
   now an empty extraction at exit 0, decided before a provider is resolved, writing the same empty
   `assertions.json` a real run writes. `_project_facts` and `_organism` do return `None` by paths
   that already exist. Structure-only touches exactly one consumer that had to *decide* anything new:
   `_join`.

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

## What the build settled

Two spellings this record left open. [#270](https://github.com/liuhlab/seqforge/issues/270) decided
both, and they are written down here so the next reader does not re-derive them:

- **The draft emits run records only, and no sample record.** "One sample per run" is sayable two
  ways — an explicit `- level: sample` beside each run, or a run parented to nothing — and only the
  second is the *strict* no-op. An explicit sample record would set `experiment.samples[].accession`
  to the grouping key where the record-less path leaves it absent, and that field is inside
  `dataset_hash`, so the file that must change nothing would move the dataset's identity. The draft
  therefore writes runs, each parentless, and a run with no sample above it is its own sample: the
  same ids, the same files, the same null accession the filenames alone produce.
- **A `source: user` sample carries `accession=None` and no record.** A hand-written id is a grouping
  key and not a specimen an archive named, and `plate7` matches no accession pattern — storing it as
  one was both false and unrepresentable, and it reached the resolver as an uncaught validation error
  rather than as anything a caller could act on. `_join` keeps neither for a declared sample, which
  is the no-attributes rule holding from the other side: a structure-only set leaves `_positions_for`
  nothing to read.
- **And the grouping key itself is constrained, at the loader.** Making the accession `None` says
  what the id is *not*; nothing said what it may be. A `source: user` id becomes `sample_id` — a
  plain `str` — then a `units.tsv` cell, a results directory, an `.h5ad` stem and an unquoted shell
  word, so a tab splits the units row, a `/` or a leading `.` moves the output out of the results
  directory, and a leading `-` is read as an option. The rule is an **allowlist**, `[A-Za-z0-9]` then
  any of letters, digits, `.`, `_`, `-`, refused with a `Blocker` carrying the nearest legal
  spelling: the hazard is open-ended, so what protects the next consumer of a sample id is what this
  admits and not which of today's it forbids. Only the hand-written dialect — an archive id is an
  accession, already well formed, in a cache nobody can re-type. `records new` applies the same rule
  to the run keys it derives, so "a draft always loads" holds by construction.

## So in code

**A record set whose `source` is `user` may declare only `level`, `id`, `parent` and `filenames` — a
loader that lets an attribute through has broken `asserted`, not just widened a schema.** Parse it
with a safe loader like both manifests (`CSafeLoader`, as the KB's does; YAML is a superset of JSON,
so one loader takes the existing `io records` caches and a hand-written file with no extension
dispatch). Keep `records new` in a top-level `records` group, never under `io`: it reads a local
directory and touches no network, and `docs/agents/cli.md` makes `io` the only network surface. Draft
one sample per run — as parentless runs, per the fork above — so applying the draft unedited cannot
change a grouping, and put the `_S<n>` / flowcell candidates in comments.

**Enforced by.** The parse gate, in `tests/test_recordset.py`:
`test_a_user_set_carrying_attributes_is_refused_and_the_same_set_without_them_loads` (the refusal
this record exists for, against the identical set minus the attribute, which loads),
`test_free_text_is_refused_for_the_same_reason_attributes_are`, `test_an_archive_level_is_refused`
(parametrized over `experiment` and `project` — the two-level rule),
`test_a_dangling_parent_is_refused_by_the_id_it_names`,
`test_a_run_parented_to_another_run_is_refused`, `test_a_duplicate_id_is_refused`,
`test_an_unknown_key_on_a_record_is_refused`, `test_an_unknown_key_on_the_set_is_refused`,
`test_an_id_that_could_not_be_a_sample_id_is_refused` (parametrized over the tab, the newline, `..`,
a slash, a space, a `;` and a leading `-` — each one a consumer of the id rather than a taste — and
asserting that the spelling the remedy suggests is one this same loader accepts) against
`test_the_ids_a_human_would_actually_type_still_load`, which is the half an over-tight allowlist
would break silently,
`test_a_run_with_no_filenames_and_a_sample_with_them_are_both_refused`,
`test_one_file_declared_by_two_runs_is_refused` and `test_every_refusal_names_something_to_type`.
One loader over both dialects is `test_json_and_yaml_are_one_code_path`, with the archive spelling
held exactly as tolerant as it was by `test_an_archive_cache_still_loads_unchanged` and
`test_an_archive_cache_carrying_an_unknown_key_still_loads`; a refusal is an object rather than a
traceback in `test_a_missing_file_refuses_rather_than_raising`,
`test_a_file_that_is_not_a_mapping_refuses_rather_than_raising`,
`test_a_broken_archive_cache_refuses_rather_than_raising` and
`test_a_hand_written_file_that_forgot_source_user_is_told_so`.

The fuse note, in `tests/test_records.py`:
`test_a_declared_fuse_compiles_as_one_sample_and_says_the_grouping_was_declared` (one sample, four
files, no Blocker, `accession is None`, and a message naming both runs),
`test_an_archive_set_fusing_its_runs_under_one_biosample_is_silent` (the `source` gate — an ordinary
`SRR`→BioSample fusion says nothing), `test_the_safe_draft_grouping_says_nothing` (an unedited draft
is silent for the same reason), and
`test_a_hand_written_set_that_leaves_a_file_unplaced_is_sent_to_its_own_file` (the join still
refuses, with a remedy naming `seqforge records new` and no re-fetch). Where the fuse LANDS is
`test_a_declared_sample_fuses_two_runs_the_filenames_kept_apart` (`tests/test_recordset.py`).

The draft as a no-op, in `tests/test_recordset.py`:
`test_applying_the_draft_unedited_changes_no_sample` — same ids, same files under each, same absent
accession as resolving the identical bytes with no record set — and
`test_the_draft_is_one_run_per_run_and_loads_clean`, which pins the fork above by asserting the draft
declares no sample record at all. The comments it exists for are
`test_the_draft_names_the_sample_sheet_pair_it_will_not_decide`,
`test_the_draft_names_the_flowcell_pair_it_will_not_decide` and
`test_an_unambiguous_draft_says_the_scan_ran_and_found_nothing`;
`test_a_directory_with_no_fastq_refuses_rather_than_drafting_an_empty_set` and
`test_a_directory_whose_run_keys_could_not_be_sample_ids_refuses_too` keep a draft that would not
load from being written.

The empty plan, in `tests/test_extract.py`: `test_a_structure_only_record_set_plans_nothing_and_asks_nobody`
(no documents, no requests, no estimated tokens — and `extract_planned` over it returns an empty list
against a provider that raises if touched). Where that lands is `tests/test_cli.py`:
`test_a_record_set_with_no_prose_is_an_empty_extraction_and_not_a_crash`, parametrized over both
dialects because the archive one was never new, with the provider stubbed to refuse so that
"resolved before" would fail; and
`test_a_structure_only_records_compile_reaches_the_manifest_with_no_credential`, which drives
`seqforge run` over a fusing set **without** `--no-llm` and no provider reachable, and asserts the
compile reaches the Snakefile with all four files under one `lib01`.

The verbs, in `tests/test_cli.py`:
`test_records_is_a_top_level_group_and_io_records_is_left_where_it_was` (introspected off the live
app — `records new`/`validate` at the top level, `io records` untouched),
`test_records_new_drafts_a_set_the_loader_and_validate_both_accept`,
`test_the_draft_applied_unedited_produces_the_samples_the_filenames_already_did`,
`test_records_validate_refuses_a_typed_attribute_and_names_the_key` (exit 3, and the stdout object
names the offending field),
`test_records_new_refuses_a_directory_with_no_fastq_and_prints_no_traceback` and
`test_records_new_refuses_to_clobber_its_out_file_and_takes_force_to_replace_it`.

The corpus, over `evals/cases/grouping/declared-one-library-across-two-runs/` — two libraries, one
lane each, a committed `records.yaml` with `source: user`, graded `experiment.n_samples: 1` — in
`tests/test_evals.py`: `test_a_declared_record_set_fuses_two_runs_into_one_sample` holds what a count
cannot carry (the declared id, all four files under it, `(accession, attributes) == (None, {})`, and
the warning naming both fused runs), `test_the_same_deposit_without_the_record_set_stays_two_samples`
is the control that makes the 1 falsifiable, and
`test_a_case_commits_its_record_set_under_either_name_and_never_both` holds the corpus to the same
loader the CLI uses.

## Consequences

- **`CONTEXT.md` gains a `Record set` entry and loses an absolute.** **Archive record** no longer says
  an archive is the only declarer; `source` carries that. **Sample** now states that it is the level
  that fuses runs, and that in a `source: user` set the id is a grouping key rather than a specimen
  claim. The `asserted` precedence in [0010](0010-two-resolvers-one-blocks-one-warns.md) is now
  **conditional on the no-attributes rule** — permit attributes later and that justification breaks
  silently, which is the reason this file exists.
- **#270's first checkbox needed no new dataset-side input.** `--records <path>` was already on
  `manifest fill`, `run` and `harvest extract`; what the build added is the validating loader and the
  draft (a top-level `recordset.py`, for the reason `pipeline.py` is top-level — the module that
  writes the file owns reading it), the `records new` / `records validate` verbs, the fuse warning,
  and a `_join_blocker` branch whose remedy names `records new` instead of an archive to re-fetch
  from. One consumer that was not obvious: `evals/case.py` read `<case>/records.json` through the
  container model alone, so a hand-written set was both unnameable and validated more weakly in the
  corpus than in the product — a case may now commit `records.json` **or** `records.yaml`, never
  both, and either goes through the one loader.
- A **Record set** is an *input*, like the FASTQs and the harvested **Document**s, and not a third
  artifact: [0004](0004-two-artifacts-not-one.md)'s two artifacts are unchanged, and the dataset
  manifest stays write-once.
- **The `source` field becomes semantic.** It was reported and never branched on; the fuse warning is
  the first behaviour keyed to it. A user who forges `source: ncbi-sra` suppresses only their own
  warning, which is a self-inflicted and bounded cost.
- Moving the join to the experiment level stays open, and is a measurement question over the deposited
  path rather than a design one.
