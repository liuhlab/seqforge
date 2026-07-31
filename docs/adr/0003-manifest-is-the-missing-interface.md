# 3. The manifest is the interface SRAgent and scRecounter never had

Date: 2026-07-31

## Status

Accepted.

## Context

Two ArcInstitute tools bracket this problem and nothing joins them.

- **SRAgent** turns an accession + its prose into structured metadata (organism, tissue,
  single-cell, 10x version).
- **scRecounter** fetches, runs STARsolo, and emits a count matrix — but it does **not trust SRA
  metadata at all**, so it *grid-searches* STAR parameters (whitelist version, CB/UMI length,
  strand, reference) by aligning reads under many combinations and picking the winner by fraction
  of valid barcodes.

scRecounter re-derives, by brute force on real alignments, facts that were sitting in prose SRAgent
had already read. The obvious reading of that is "two tools, wire them together" — a third pipeline
that calls one and then the other.

## Decision

**One package, because the gap is not a missing feature; it is a missing *interface*.**
`manifest.yaml` is that interface: a validated, machine-independent statement of what the data IS,
decided once and hashed. Given it, the two tools collapse into one compiler — `harvest` is
SRAgent's job demoted to *proposing*, and `compose` is scRecounter's job driven by a *decided*
manifest instead of a search.

## Why the interface rather than the integration

Three things neither tool has, none of which a wrapper around both could add:

1. **Span verification** (R2) — extracted metadata carries a tripwire, not a confidence score. A
   claim without a quote that greps back is rejected, so "SRAgent said so" is never a reason.
2. **Cheap eager verification** — one ~100 ms `onlist_hit_rate` check where scRecounter spends a
   subsample alignment. The expensive experiment is the fallback, not the mechanism.
3. **Refusal** (R2) — an undecidable dataset yields a `Blocker` and a nonzero exit, not a
   best-scoring guess. A grid search has no way to say "I don't know"; its argmax always returns
   something.

And because processing is a separate artifact ([ADR 0004](0004-two-artifacts-not-one.md)), uniform
reprocessing across ~10⁴ datasets becomes *one recipe among many* rather than a rerun of everything.

## So in code

**Decide, then refuse — never search.** A stage that tries N parameter combinations over real reads
and keeps the best scorer is a regression against this record, even when the search is cheap: the
answer to an undecidable dataset is a `Blocker` and a nonzero exit, not an argmax. Reach for the
cheap eager check (an `onlist_hit_rate` costs ~100 ms) before the expensive experiment, and keep
alignment where it belongs — escalation rung 6, invoked on an ambiguity code has already flagged.

**Enforced by.** **None exists.** The refusal path a search would route around is covered
(`test_the_cli_surface_exits_and_answers_as_documented`, `tests/test_cli.py`), but *how* an answer
was reached is not a property any test reads, and a grid search that always returned something would
pass every gate in the suite. This one is enforced at review.

## Consequences

- The claim is **architectural, not a track record**: seqforge has compiled the worm pilot end to
  end but has not yet executed a pipeline on real reads at scale.
- Alignment survives only as escalation **rung 6** — a mini-align invoked on an ambiguity code has
  already flagged, never the primary identification mechanism (and it is still unbuilt).
