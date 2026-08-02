# 21. One deposit is one source, at every layer — the slot the submitter typed outranks a reading of it

Date: 2026-08-02

## Status

Accepted. Retires the containment fold (`_without_short_readings`, `_is_short_reading_of`) that
[#182](https://github.com/liuhlab/seqforge/issues/182) introduced for the same defect.

## Context

An archive deposit says the same thing twice. The submitter types a value into a structured column,
and the archive renders that value back out inside free text a human reads. `_source_of` already
knows this — every level of one deposit returns `"archive"`, because "every level of one deposit is
one submitter" — but the two layers where that matters were both getting it wrong, in the same shape,
one stage apart.

**The resolver.** A BioSample types `treatment = "Citrobacter rodentium infection"`; a model reads
that same submission's experiment title and asserts `treatment = "Citrobacter rodentium"` — real
quote, entailed, true, one word short. Both arrive `asserted`, they compare unequal, the
equal-authority rule stores null, and **adding a true statement destroyed a fact the archive had
already supplied**. The measured cost is not a missing field: `experiment` is inside `dataset_hash`
and the manifest is never rewritten, so the eval harness grades it `false_accept` — "a confident
wrong manifest is the one failure the corpus never recovers from".

**Harvest.** An SRA experiment record types `library_strategy = "RNA-Seq"` and ends its title with
the same word. A model asked for the chemistry answers with it, quoting verbatim. The quote greps
back, and entailment is *vacuous* because the value sits inside its own quote — so a **transcription
of a column code already read** arrives looking exactly like a reading of prose, and then steers the
byte resolver ([ADR 0020](0020-a-family-term-narrows-it-does-not-conflict.md)).

The obvious reading of the first one is that the two strings nearly agree, so the fix is to notice
when they do. That was the first repair and it is why this record exists: **the two failures that
prove it insufficient are already on the corpus.** `GSE317744` types `treatment = "MC38_3 weeks"` and
the model asserted `treatment = "CCR9 KO"` — the sample's *genotype*, filed under `treatment`. The
two strings have nothing in common and never will.

## Decision

**Within one source, a value the submitter TYPED into a slot beats a model's reading of that source's
prose — and a span that is byte-equal to a typed column is not prose at all.**

| layer | rule | where |
| --- | --- | --- |
| **Resolve** | `_Position.declared` joins basis in precedence. Same basis, same `source`: the declared position wins and the reading is *named* in a `sample_attribute_ambiguous` warning. | `resolve/records.py::_outranking` |
| **Harvest** | A free-text span byte-equal to a value typed on the record that produced this document is **marked**, with the attribute and value as its proof. A draft whose quote lies **wholly inside** a mark is refused `quote_is_a_typed_column`. | `harvest/normalize.py::declared_spans`, `harvest/verify.py` |

They are one principle at two layers — the record's own free text re-rendering its typed column, and
a model's reading of that record — which is why they are one record and landed in one change.

Two boundaries, and both are load-bearing:

- **Only within one source.** A paper is a second author: it still loses on basis, and it still
  leaves the note that says so. Where nothing was typed at all, two prose readings are two guesses
  with no submitter's string behind either, so the tie stands and the attribute is null. That case is
  what the arbitration verb at rungs 4–6 is for, and this sharpens its job description.
- **Only wholly inside.** A quote reaching past the marked column says something the column did not,
  so it is a reading and it survives. Only a quote with nothing of its own to add is refused.

**Safety is that nothing new can reach the manifest.** What gets stored is the submitter's own
string, byte for byte — exactly what a run with no prose at all would store.

## Why not containment, overlap, synonyms or punctuation folding

Every one of them works by comparing the two strings, so every one of them closes the paraphrase and
leaves the misfiling open:

| | GSE282765 `Citrobacter rodentium infection` / `Citrobacter rodentium` | GSE317744 `MC38_3 weeks` / `CCR9 KO` |
| --- | --- | --- |
| containment (the first repair) | closed | **open** |
| overlap within one source (#188's proposal) | closed | **open** |
| BioSample synonym tables | closed | **open** |
| punctuation folding | closed | **open** |
| the typed slot wins | closed | **closed** |

#188 observes there is *"no finite list of shapes"*, and the answer to an unenumerable list is not a
longer list. The misfiling row is also the one span verification structurally cannot reach —
`harvest/verify.py` says so in its own words: *"a real quote filed under the wrong field passes here
by construction"*, and *"tightening the matcher would not help; there is nothing here left to check"*.
It is right. **Declared-beats-read is a defence against field-assignment errors that verification
cannot mount**, for every attribute the archive typed, and it never asks what the reading says.

Punctuation folding lands anyway, in `_norm_value`, and is worth naming as what it is: `-`, `_` and
`/` fold to space so that `wild-type` and `wild type` are one genotype rather than a disagreement to
be warned about. It moves no decision — `mc38 3 weeks` and `mc38 tumor 3 weeks` still differ — it
only keeps a warning quiet where there was nothing to report.

## Why not split the title on the archive's own delimiters

#188 proposes recognising SRA's `<GSM>: <description>; <organism>; <strategy>` and dropping the
trailing fields. **Rejected, and this is not negotiable.** seqforge compiles in-house datasets that
never had an accession and archives that lay their records out differently — "no archive is the
normal case, not the degraded one", as `resolve/records.py` puts it — so a rule keyed on one
archive's display grammar is a rule most datasets cannot obey and the rest obey by luck. The mark is
keyed on *proof* instead — a
byte-equal match against a column of the same record, whole tokens only — which needs no delimiter,
no accession format and no knowledge of who rendered the string, and is a no-op returning `()` where
there are no records at all.

## Why this does not bump `RESOLVE_VERSION`

It changes what the resolver decides, so the question is fair, and the answer is that **there is
nothing to re-key**: no artifact holds the metadata resolver's output. The resolution is recomputed
inside every `manifest fill`, while `RESOLVE_VERSION` keys the *byte* resolver's candidates and
evidence matrix, which this does not touch. Bumping it would re-probe every cached dataset to change
nothing about them — the opposite of ADR 0020's case, where the defect *was* a cached refusal being
served out of the cache. The harvest half re-keys nothing either: `NormalizedDoc.declared` is derived
from the record and the canonical text, and enters neither `doc_sha256` nor `normalized_sha256` — the
span space is unchanged, so every offset computed under the old value still points at the same
characters.

## So in code

**Do not compare the two strings — ask which of them the submitter typed.** In `resolve/records.py`,
precedence is `_outranking`: basis first, then `declared` within one `source`. A rule that reads the
values to decide whether they agree is the design this record retired; if you are about to write one,
the misfiling row above is the case it will not reach. In `harvest/`, a span is marked only where a
byte-equal typed column proves it, and a mark rejects a draft only when the quote lies wholly inside
it — never a delimiter, never an archive's grammar, and always a no-op with no records. Both halves
keep the losing claim visible: the resolver names it in the warning, `verify_drafts` keeps it in
`rejected` with the column that refused it.

**Enforced by.** `test_a_prose_reading_never_outranks_the_slot_the_submitter_typed`,
`test_the_typed_slot_wins_wherever_it_sits_in_the_list`,
`test_two_prose_readings_with_no_typed_slot_are_still_a_disagreement`,
`test_a_short_quote_of_the_records_own_field_does_not_delete_it` and
`test_the_sample_attribute_precedence_table` (`tests/test_records.py`);
`test_a_span_that_re_renders_the_records_own_typed_column_is_marked`,
`test_verify_rejects_a_draft_quoting_the_records_own_typed_column`,
`test_a_quote_reaching_past_the_typed_column_is_still_prose` and
`test_a_document_that_came_from_no_record_carries_no_marks` (`tests/test_harvest.py`).

## Consequences

- **A submitter's placeholder now beats real prose.** Someone who typed `N/A` or `not collected` wins
  where today the attribute is null. The mitigation is deliberately not an enumerated placeholder
  list — that is the unenumerable-list mistake again — it is that the losing reading is always named
  in the `sample_attribute_ambiguous` warning, so the manifest stays auditable and the pattern is
  findable across the corpus.
- **A note now lands wherever a model quotes a record short.** The retired fold was silent there, on
  the argument that a warning on most datasets stops being read. That argument does not survive the
  change: silence was honest while nothing was excluded, and now a real precedence decision is being
  taken. It is recorded like every other one — the same shape as `asserted` beating `inferred`.
- **`records.py`'s "a wrong value is permanent and a missing one is not" does not survive
  [ADR 0004](0004-two-artifacts-not-one.md).** The manifest is write-once and content-hashed, so a
  null is exactly as permanent as a wrong value; what separates them is that the harness grades the
  null `false_accept` and a reader cannot tell it from a fact the archive never supplied. The
  sentence stays true only in the case this record leaves alone — two prose readings, nothing typed.
- **Mark-with-proof is not what closes either measured false accept**, and it is here anyway. Its
  `"RNA-Seq"` draft was already killed on an independent ground by `resolve_chemistry`
  ([ADR 0020](0020-a-family-term-narrows-it-does-not-conflict.md)), and it does not reach
  `GSE317744`'s dropped `treatment` at all — `"MC38_3 weeks"` is not byte-equal to anything in that
  title. It is the generalization, not the repair, and splitting it into another PR would have
  straddled this record across two.
- **A fact typed under a name nobody curates can no longer be laundered into a curated key.** If a
  record types an attribute outside NCBI's 960 and also renders that value into its free text, the
  span is marked, so a model cannot carry it in under one of the nine asked keys. That is the same
  decision `_unharmonized_note` already took — "a key we coined would accept whatever an extraction
  wanted to put in it" — now taken at both ends rather than one, and it is a *narrowing*: the note
  still fires, so the fact stays visible. No case on the corpus exercises it today; the marks that
  exist come from `library_strategy`, `library_selection`, `strain` and `isolate`.
- **Measured reach, with no model involved.** Across the eighteen benchmark cases' committed records,
  43 of 133 planned documents carry at least one mark, and the largest covers 31% of a one-line run
  alias. No mark swallows a paragraph, which is the shape that would have made this a censor rather
  than a filter — re-measure it if the rejection is ever widened beyond record-internal spans.
- **What the model is for narrows again.** Nothing here asks it to arbitrate; code decided, from a
  fact code already had. The remaining arbitration surface is the one this record deliberately does
  not touch: two readings with nothing typed behind either ([R2](../agents/rules.md), rungs 4–6).
- The `Conflict` / `Warning` split ([ADR 0006](0006-one-judgement-one-envelope.md)) and the
  block-on-bytes rule ([ADR 0010](0010-two-resolvers-one-blocks-one-warns.md)) are untouched: this
  record changes which of two positions wins, never whether losing is a refusal.
