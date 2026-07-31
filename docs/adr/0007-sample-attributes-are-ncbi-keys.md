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

## Consequences

- `strain` was available the day the constraint landed, and it is what tells the pilot's two
  conditions apart — the very distinction the removed typed fields could not express.
- Enforced by `SampleGroup._keys_are_ncbi_attributes` and
  `test_every_asked_attribute_is_one_ncbi_defines`; the asked set is a subset of the enforced set by
  construction, not by convention.
- EFO labels ([`io/efo.py`](../../src/seqforge/io/efo.py)) ride the same generated-data-plus-refresh
  pattern, for the same reason.
- A sample attribute is never worth blocking a compile over — see
  [ADR 0010](0010-two-resolvers-one-blocks-one-warns.md) for why the metadata resolver warns
  rather than refuses.
