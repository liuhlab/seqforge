# 7. Sample attributes are an open dict over NCBI's 960 harmonized names

Date: 2026-07-31

## Status

Accepted. Supersedes the typed `tissue` / `condition` fields.

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

## So in code

**Key a sample attribute with one of NCBI's 960 names, or take the refusal.** Never add a typed
field for one, and never coin a key: a key we invented accepts whatever an extraction wants to put in
it, which is exactly how `condition` swallowed routine husbandry. If the vocabulary is missing a
name, refresh the generated data — an NCBI addition is a data update, never a schema migration. The
same holds for the EFO labels, which ride the same pattern.

**Gate.** `SampleGroup._keys_are_ncbi_attributes` (the validator itself);
`test_a_sample_attribute_key_must_be_one_ncbi_defines` (`tests/test_models.py`) for the refusal, and
`test_every_asked_attribute_is_one_ncbi_defines` (`tests/test_fields.py`), which keeps the asked set
inside the enforced set by construction rather than by convention.

## Consequences

- `strain` was available the day the constraint landed, and it is what tells the pilot's two
  conditions apart — the very distinction the removed typed fields could not express.
- EFO labels ([`io/efo.py`](../../src/seqforge/io/efo.py)) ride the same generated-data-plus-refresh
  pattern, for the same reason.
- A sample attribute is never worth blocking a compile over — see
  [ADR 0010](0010-two-resolvers-one-blocks-one-warns.md) for why the metadata resolver warns
  rather than refuses.
