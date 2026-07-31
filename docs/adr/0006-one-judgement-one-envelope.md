# 6. One judgement, one envelope — `chemistry` is the library section's only `Evidenced` field

Date: 2026-07-31

## Status

Accepted.

## Context

R4 says every *interpretive* field is `Evidenced{value, basis, evidence, confidence, rung}`. Read
field-by-field, that puts an envelope on `chemistry`, on `assay`, on `read_layout`, and on
`files[].read_id` — four interpretive-looking fields, four envelopes.

The pilot's manifest showed what that bought:

```yaml
confidence: 0.750672   # library.chemistry
confidence: 0.750672   # library.assay
confidence: 0.750672   # library.read_layout
confidence: 0.750672   # library.files[].read_id
```

The same number, four times, because it was always **one number about one decision**.

## Decision

**Envelopes track decisions, not fields.** `chemistry` is the only `Evidenced` field in
`LibrarySection`. It carries the joint optimization over *(which technology, which file is which
read)*, as an equivalence class (`EvidencedChemistrySet`) because benign twins (v3 + v3.1) are
recorded together.

Everything else *follows* from it and carries no envelope of its own:

- `assay` is the same answer in EFO's vocabulary;
- `read_layout` is the KB's structure filled with measured lengths;
- `files[].read_id` is the **assignment half of the same optimization** — so it is not `Evidenced`;
  the score rides on `chemistry`.

## Why not one envelope per interpretive field

Four envelopes filled from one variable **cannot disagree** — so they were never four truths, and
R4's `Conflict` machinery had nothing to detect between them. R4 asks only that a value not travel
without its provenance, and one honest envelope does that for all four.

## What is deliberately *not* evidenced

- **`Study` is not `Evidenced` at all.** None of it is an interpretation: the record says the title
  is X and we copy X exactly as we copy a sha256. Its abstract is deliberately absent — it is prose,
  it belongs in a document a quote can grep into, and pasting a paragraph of English into a
  content-addressed manifest would make the dataset's identity depend on it.
- **`resources` carries no basis** — it is a hint, not a decision.
- Raw identity and provenance fields carry no basis for the same reason.

## So in code

**Put the envelope on the decision, not on every field that follows from it.** `chemistry` is the
only `Evidenced` field in `LibrarySection` (`CONTEXT.md` defines both terms); do not wrap `assay`,
`read_layout` or `files[].read_id`, and do not give `Study`, `resources` or any identity field a
basis. Before adding an `Evidenced`, ask whether the new envelope could ever disagree with an
existing one — if it cannot, it is the same judgement and needs no envelope. `confidence: null` is
legal; two confidences for one judgement are not.

**Enforced by.** `test_one_decision_carries_exactly_one_confidence` (`tests/test_models.py`), which
goes red if an envelope is re-added to `assay` or `read_layout`;
`test_the_assay_cannot_disagree_with_the_chemistry_it_names` (same file) for the field that follows.

## Consequences

- The same diagnosis — "the grammar of a truth applied where there was only one decision" — produced
  the artifact split in [ADR 0004](0004-two-artifacts-not-one.md).
