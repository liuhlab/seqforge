# 30. A collapsed citation is regenerable only from the record set, so harvest writes every member

Date: 2026-08-04

## Status

Accepted. Narrows the promise
[ADR-0021](0021-one-deposit-is-one-source-at-every-layer.md) rests on, and adds a second kind of span
mark beside the one that record makes.

## Context

`render_record`'s docstring states the property the whole span tripwire stands on:

> Deterministic matters more than it looks. This text *is* the document: its sha256 is the identity an
> assertion cites, and the span check greps this exact string. So the rendering must be reproducible
> from the record forever — a human handed the record and this function must be able to regenerate the
> bytes a quote was checked against, or the citation is unfalsifiable.

**From the record. Singular.** That was true while every document held one record's prose (or one
sample's runs, whose members are named in the document's own `members` list). The near-identical
collapse ([#233](https://github.com/liuhlab/seqforge/issues/233) decisions 3–7,
[#283](https://github.com/liuhlab/seqforge/issues/283)) breaks the singular. It folds records that say
the same thing onto one exemplar, and two derived facts then exist that **one record cannot
regenerate**:

- **which spans are invariant.** A `VariantSpan` is computed by comparing the exemplar's tokens
  against every member's, so the marks that decide whether a claim may fan are a function of the
  *set*, not of the document.
- **what a non-exemplar member is even sent as.** Every other member is reduced to its *distinctive
  bytes* (`variant_text`) — which tokens those are is, again, a property of the group. That document
  is a string no record produced on its own, and it is the string a model reads and quotes.
- **which document a fanned claim cites.** A sample-scoped claim is materialized once per member, and
  each copy cites a document that was **never sent to a model**. Its offsets are recomputed against
  that member's own text. Nothing in the send list, the transcript or the usage ledger mentions it.

The obvious reading — and the one a future reader will reach from the same evidence — is that this is
fine because the record set is content-addressed and cached under `seqforge/`, so the bytes are
recoverable. They are. But *recoverable given the cache* is a weaker guarantee than *regenerable from
the citation*, and the difference is exactly a citation's falsifiability: an assertion carries a
`doc_sha256`, a quote and two offsets, and if nothing on disk holds the text those offsets index, the
claim cannot be checked and cannot be refuted either. #233 says so in one line — *"This earns an ADR
line, not a docstring edit."*

## Decision

**`harvest extract` writes every document the plan RENDERED, not every document it sent.**

| | what it holds |
| --- | --- |
| `plan.documents` | the send list — the exemplar (marked), and every other member reduced to its distinctive bytes |
| `plan.collapsed` | exemplar `doc_sha256` -> every other member's FULL rendering, which is what a fanned claim cites |
| `plan.all_documents` | both, in plan order — **what reaches disk** |

So a reduced member owns two documents: the short one that was sent, and its full rendering, which is
never sent and exists to be cited. That is not redundancy — they are two different strings, and only
the second is the one a fanned claim's offsets index.

Two writes, and both are load-bearing rather than tidy:

- `seqforge/records/documents/<name>-<sha>.txt` for every member, so the exact bytes a fanned span
  indexes still exist after the process ends.
- `document_subjects` in `logs/assertions.json` for every member, so `resolve` can place the claim.
  This one is not archival at all: `resolve.records._positions_for` **silently drops** a claim whose
  document it cannot place, so a collapse that recorded only what it paid for would fan a claim onto a
  record and throw it away one stage later.

**And the mark that decides fan-out is its own type.** `VariantSpan` sits beside `DeclaredSpan` on
`NormalizedDoc` and never merges with it. They answer different questions about the same range: a
declared span makes `verify` *refuse* a quote (that range is a typed column, not prose); a variant
span decides whether a verified claim *fans* (that range is where the group disagrees).

## Why not regenerate the collapse instead of storing it

Because "regenerate" means re-deriving the group, and the group is a function of inputs that move.
Recomputing an invariant span needs the record set **and** the tokenizer **and** the same grouping
rule — so a `NORMALIZER_VERSION` bump, a change to `_is_token_char`, or a re-fetch that returns one
record more would each silently re-answer "was this quote invariant?" for a claim already stored. The
stored member document is the artifact that pins the answer, and it costs a few hundred bytes per
record. Disk is state (R5); a derivation that has to agree with a past derivation is not.

## Why not skip the fan-out and simply not collapse

That is guarantee (c) in #233 — *never collapse across records; only shrink text* — and it is safe
and never cheap. The alternative that IS cheap and must not be re-proposed is (a), **fan-out-only**:
fan a claim to every record whose bytes carry the quote and never send the others. It reads the
majority and silently skips the records that *differ*, and `harvest/fields.py` records the pilot in
which a run alias was the only place a WT-vs-mutant contrast was written in plain words. A mechanism
anti-correlated with value is worse than one uniformly lossy.

## Why materialize a claim per member rather than give `Assertion` a subject list

A subject list is the smaller diff and the larger change. `resolve.records._basis_for` reads
`doc.subject` and maps it home through `subject_to_sample`; a claim carrying its own list of subjects
would have to be placed some other way, and the honest other way is dataset scope — which is
`inferred`, the basis that loses to the archive's own typed slot and to every declaration. So the
cheap-looking change **downgrades every collapsed claim**. Materializing keeps `_basis_for`
completely unchanged, keeps the claim `asserted`, moves no schema (`Assertion` is byte-identical
under `schema export`), and teaches no downstream component a new concept: the whole mechanism stays
inside `harvest`.

The price is stated rather than buried: a value written into every sample title of a 1440-sample
deposit materializes 1440 assertions, ~430 KB. That is a cache artifact under `seqforge/`, and such a
manifest already carries 1440 evidence ids either way.

## So in code

**When you add a document to a plan, decide which of the two lists it belongs to, and never write the
send list to disk as if it were the whole set.** `plan.documents` is what a model sees, what
`batch_documents` groups and what the meter charges; `plan.all_documents` is what `documents/` and
`document_subjects` must cover. If you find yourself writing `for nd in plan.documents` beside a
`write_text`, you have picked the wrong one. And when you next touch `NormalizedDoc`'s marks, ask
which question the new one answers — *refuse this quote* or *fan this claim* — because a single field
serving both would silently reject fanned claims in one direction and fan column quotes into 1439
records that never carried them in the other.

**Enforced by.** `tests/test_cli.py`:
`test_a_collapsed_members_bytes_and_subject_both_reach_disk` and
`test_a_records_only_compile_still_reaches_the_harvest_stage`. `tests/test_harvest.py`:
`test_a_variant_document_keeps_the_accession_and_drops_the_labels`,
`test_adjacent_variants_keep_the_separator_the_record_wrote_between_them` and
`test_a_variant_document_is_regenerable_from_the_record_set_and_only_from_it`. `tests/test_evals.py`:
`test_the_eval_path_fans_a_collapsed_claim_exactly_as_the_shipped_path_does`. `tests/test_records.py`:
`test_a_fanned_claim_resolves_as_asserted_against_the_sample_it_names` and
`test_a_withheld_documents_subject_is_load_bearing_not_bookkeeping`, which pins the silent drop this
record exists to prevent. `tests/test_extract.py`:
`test_a_collapsed_member_is_rendered_and_kept_even_though_it_is_never_sent`.

**Nothing enforces the archival half beyond one run.** No gate checks that a stored assertion's
`doc_sha256` still has a file under `documents/`, so a workspace pruned by hand leaves citations that
cannot be checked and nothing says so. Noticing it would take a verb that walks `assertions.json`
against the document directory — `harvest verify` is the natural home, and it does not do this today.

## Consequences

- **`HARVEST_VERSION` bumps to 2026.8.0**, because the send list itself moved: a harvest cached before
  the collapse asked documents this one folds away, and its claims were never fanned. The normalizer
  is untouched — the collapse marks spans and never edits text, so `NORMALIZER_VERSION` and every
  offset computed under it stand.
- **`PlannedDocument.members` now carries the folded records too**, so `--dry-run` prints "one
  document, N members"; `ExtractionPlanReport` gains `n_records_reduced` beside `n_records_collapsed`,
  because a record that cost nothing and a record that cost what it is worth are two facts and one
  number cannot hold both. `Assertion` itself is byte-identical under `schema export`.
- **Six of the eighteen benchmark cases move, and none of them by document count.** `GSE126954`,
  `GSE234962`, `GSE256266`, `PRJNA1027859`, `PRJNA1195922` and `PRJNA658829` each hold records that
  share a skeleton, so 100 records across them are now sent reduced: same documents, same requests,
  between 29 % and 77 % less text. No member anywhere in the corpus is *withheld* by this mechanism —
  the 66 records the corpus does fold away are the pre-existing per-sample run collapse, unchanged.
  Different text reaching a model can move a draft, so this owes a before/after digest pair under
  #225 constraint 3, and those six are where to look.
- **The unit of reproducibility is the deposit, not the record.** That is consistent with
  [ADR-0021](0021-one-deposit-is-one-source-at-every-layer.md) — one deposit is one source at every
  layer — and it is the second place that principle has cost something concrete rather than merely
  explained something.
- **Measured on the 1440-record `GSE207085` dump, 2026-08-04: 786 906 characters over 540 requests
  become 194 038 over 59** (~180 K estimated input tokens under an ask-derived batch width,
  [#282](https://github.com/liuhlab/seqforge/issues/282)) — 53 requests for the sample and run
  documents and 6 for the experiments, which is #233 decision 4's arithmetic to the request. **Not
  one record is withheld**: every level carries a per-cell serial name (`nasal_prox1_270`,
  `GSM6277169_r1`), so 4317 of 4320 are reduced and asked their difference, and the three exemplars
  carry the prose. An earlier reading of "mark, never splice" applied it to every member rather than
  to the exemplar alone, and under it this deposit folded *nothing* and the plan did not move at all
  — the measurement is what showed the reading was wrong, and it is recorded here because the next
  reader will reach the same reading from the same sentence.
- **The residue the `--llm` recall probe was aimed at goes to zero on this deposit.** Before the
  reduction, 71 % of every document's four-token spans occurred verbatim in another document of the
  same request (`quote_residue`, measured), so a misrouted draft would have span-verified against the
  wrong member. After it, **0 % at every batch width from 1 to 250 and every quote length from one
  token to four** — a reduced document holds exactly what distinguishes its record, so there is
  nothing left for two of them to share. The instrument stays, because that is a property of this
  deposit and not a theorem.
- **The eval path and the `run` path each carried the same shape of gap, and both are closed here.**
  `evals/run.py` called `verify_drafts` and stopped, so a graded case whose records collapse would
  have been graded on the exemplar's claims alone — a harness measuring a stage the compiler does not
  run, wrong in the one direction that invites "fixing" something that works. And `seqforge run`
  entered harvest only when a *document* was passed, so a records-only compile skipped the one stage
  that could read its records: the same defect `_roled` carried, one file over, and the same cause —
  a document was the only input harvest had when each guard was written, and `--records` became a
  second one without either noticing.
- The terms this record turns on are defined once in [`CONTEXT.md`](../../CONTEXT.md): a **Document**,
  a **Plan**, and the **Assertion** whose span this is all in service of.
