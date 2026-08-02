# 7. Sample attributes are an open dict over NCBI's 960 harmonized names

Date: 2026-07-31

## Status

Accepted. Supersedes the typed `tissue` / `condition` fields. Amended 2026-08-01: an attribute
outside the vocabulary is now surfaced rather than skipped silently; the key space is unchanged.

## Context

`SampleGroup` used to carry two typed fields, `tissue` and `condition`. Both were wrong, in
different ways:

- **`condition` was *ours*.** No archive defines it. A field named "condition" accepts anything you
  can call a condition — which is how the pilot's extraction filed routine worm husbandry into it.
  The field could not be wrong, because nothing constrained what it meant.
- **Two typed fields cannot hold `strain`** — the only structured field separating the pilot's
  wild-type samples from its *daf-2* mutants. The fact that mattered had no slot.

## Decision

`SampleGroup.attributes` is an **open dict keyed by NCBI's 960 harmonized BioSample attribute
names**, with NCBI's own definitions ([`io/attributes.py`](../../src/seqforge/io/attributes.py)),
and a validator refuses any other key. We **ask** a few and **enforce** all 960.

## Why not 960 typed pydantic fields

A typed list mirroring somebody else's vocabulary rots the moment they add to it. The vocabulary
ships as **generated data with a refresh verb**, so an NCBI addition is a data update, not a schema
migration and a release.

## Why a controlled vocabulary rather than a free-form dict

An invented key is precisely how `condition` happened: a key that accepts everything checks nothing.
Keys we did not coin cannot be bent to fit an extraction, and a key outside the 960 is a
`MISSING_CONTROLLED_VOCAB` refusal rather than a plausible-looking fact.

## Why not route an unharmonized characteristic into the prose path (2026-08-01)

The asymmetry that forced this question is real and is stated in the consequences: a fact typed into
a structured characteristic reaches no key, while the same sentence in a free-text protocol field
reaches harvest and can become a span-verified `Assertion`. Feeding the leftovers into that path
would *close* the asymmetry rather than merely surface it.

It was rejected for now, not on principle. It changes what the language model is asked to read, which
is a decision of its own and a larger one — the model has exactly two jobs and the input to job (a)
is not a knob to turn while fixing something else. The warning is the cheap half that needs no such
argument, and it makes the expensive half *measurable*: it reports how often a structured declaration
goes unread, which nothing did before.

## So in code

**Key a sample attribute with one of NCBI's 960 names, or take the refusal.** Never add a typed
field for one, and never coin a key: a key we invented accepts whatever an extraction wants to put in
it, which is exactly how `condition` swallowed routine husbandry. If the vocabulary is missing a
name, refresh the generated data — an NCBI addition is a data update, never a schema migration. The
same holds for the EFO labels, which ride the same pattern.

**And when the vocabulary has no name for what the submitter typed, say so rather than skip it in
silence.** An attribute outside the 960 leaves a `sample_attribute_unharmonized` warning naming the
tag, the value and the sample; it still gets no key. Exempt only what is bookkeeping about the
*record* rather than about the biology, by adding the name to `_RECORD_META` in
[`resolve/records.py`](../../src/seqforge/resolve/records.py) — a note on something every archive
stamps on every sample is a note nobody reads.

**Enforced by.** `SampleGroup._keys_are_ncbi_attributes` (the validator itself);
`test_a_sample_attribute_key_must_be_one_ncbi_defines` (`tests/test_models.py`) for the refusal, and
`test_every_asked_attribute_is_one_ncbi_defines` (`tests/test_fields.py`), which keeps the asked set
inside the enforced set by construction rather than by convention. For the note and its noise floor,
`test_an_unharmonized_characteristic_is_surfaced_rather_than_silently_dropped` and
`test_the_bookkeeping_every_archive_stamps_on_a_sample_is_not_surfaced` (`tests/test_records.py`).

## Consequences

- `strain` was available the day the constraint landed, and it is what tells the pilot's two
  conditions apart — the very distinction the removed typed fields could not express.
- EFO labels ([`io/efo.py`](../../src/seqforge/io/efo.py)) ride the same generated-data-plus-refresh
  pattern, for the same reason.
- A sample attribute is never worth blocking a compile over — see
  [ADR 0010](0010-two-resolvers-one-blocks-one-warns.md) for why the metadata resolver warns
  rather than refuses.
- **A submitter can declare a load-bearing fact in a structured field that this key space has no name
  for, and it becomes no manifest fact.** `GSM9270243` declares
  `bd rhapsody_capture_bead_version: enhanced beads` — the single fact separating one BD Rhapsody
  capture bead from the other, on a sample whose extract protocol names the same instrument as a
  dataset that uses the *original* bead. The record keeps it (`RecordAttribute` stores it with
  `harmonized: false` and the submitter's `raw_name`);
  [`resolve/records.py`](../../src/seqforge/resolve/records.py) then declines to key it, and that
  half of the decision stands — an open dict is the alternative this record already rejected.
- **What did not survive review is the silence** (#165). The same sentence in
  `!Sample_extract_protocol_ch1` reaches harvest as prose and can become a span-verified
  `Assertion`, so a submitter who typed the fact into a structured characteristic was *less* legible
  to the compiler than one who buried it in a paragraph. Across 10⁴ datasets that is a systematic
  blind spot rather than a curiosity, and it was invisible: the attribute was skipped with no
  `Warning` at all. It now emits one. Nothing about the key space moved — no key is invented, no
  refusal is weakened, and nothing new reaches the manifest — which is precisely the latitude
  [ADR 0010](0010-two-resolvers-one-blocks-one-warns.md) gives the metadata resolver: it decides,
  and it only warns.
- **The note is worth reading only because it is rare, so the exemption list is part of the
  decision.** Every BioSample NCBI serves carries `center_name`, `biosample_package` and
  `taxonomy_id` unharmonized — facts about the record, read by name, with the taxid becoming
  `experiment.organism`. They are named in `_RECORD_META` and emit nothing. Measured over the
  18-dataset benchmark corpus, exactly three datasets declare a genuinely submitter-invented
  characteristic (`bd rhapsody_capture_bead_version`, `synchronization protocol`, `batch`); noting
  the bookkeeping instead would have been three lines per sample on every dataset that has a record
  at all.
