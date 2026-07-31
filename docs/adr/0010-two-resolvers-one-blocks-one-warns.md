# 10. Two resolvers, two refusals — the byte resolver blocks, the metadata resolver warns

Date: 2026-07-31

## Status

Accepted.

## Context

`resolve` holds two resolvers. `scoring`/`assign`/`escalate` decide what the library IS from bytes;
[`resolve/records.py`](../../src/seqforge/resolve/records.py) decides which sample each file is
from, and what that sample was, from records + prose.

They read like a stage and a side-input, and the consistent-looking rule is "a disagreement is a
`Conflict`, everywhere". That rule is wrong, and uniformly applying it would stop datasets compiling
over facts no rule reads.

## Decision

**They are siblings, and they part on disagreement.**

- **Byte resolver — refuses.** An `observed` ↔ `asserted` disagreement is a surfaced `Conflict` it
  will **not** arbitrate. That decides what the data *is*, so it **blocks**: the library section
  takes the observed value (authority = evidence), the `Conflict` stays attached, and `compile`
  refuses until `user_confirmed`.
- **Metadata resolver — decides.** Stronger authority wins (`asserted` over `inferred`): keep its
  value, note the weaker source. **Equal authorities that disagree leave the attribute null.**
  Either way it is resolved, so the output is a non-blocking `Warning`, not a `Conflict`.

**Null-over-wrong is a value, not a question.** The `experiment` section is inside `dataset_hash`
and the manifest is never rewritten, so a wrong attribute is *permanent* and a missing one is not —
and code does not get to break a tie between equals.

## Why the asymmetry

A chemistry the bytes contradict cannot be compiled at all: every downstream parameter — soloType,
offsets, whitelist, strand — depends on it. A null `tissue` compiles fine. The pilot's `strain`
already tells its two conditions apart, and most datasets have no such prose at all. Blocking a
dataset on a sample attribute would refuse the compile over a fact no rule reads.

## Why basis is precedence, not a vote

A record's typed slot for a sample is a declaration **about that sample** (`asserted`); a paper's
sentence that holds of one of six samples is **our** inference (`inferred`). `subject_to_sample`
holds that join, computed by code from the record hierarchy, so a run alias is asserted of its
sample exactly as the sample's own alias is.

That is what makes the precedence principled rather than a tiebreak we invented — and it catches
the error class span verification **provably cannot**: a paper saying "we dissected neurons and
body wall muscle" lands beside the record's `Neurons` as a weaker claim, not as a fact, even though
both quotes are real and both entail. A sample covered *only* by a dataset-level `inferred` claim,
for an attribute some other sample owns per-sample, is also left null — the paper's blanket `daf-2`
must not stamp the wild-type samples.

## Consequences

- The metadata resolver is handed **`FileIdentity`, never `Observation`** — no probe signal reaches
  it, so it cannot accidentally re-decide the library. This is also why `isize` never joins
  `FileIdentity` ([ADR 0001](0001-head-and-wholefile.md)).
- **No accession is the normal case, not the degraded one.** Most sequencing data never had one.
  With no record, `resolve/group.py`'s filename grouping *is* the sample identity, samples carry no
  facts, exit 0. The narrower real refusal is a record that **exists** and does not account for the
  files, because half-joining reads as whole.
- Every `Warning` names what disagreed and how it was resolved, so a null is auditable rather than
  indistinguishable from "never mentioned".
