# 28. Specificity ranks a chemistry match — a phrase that only describes a run never outranks one that names a chemistry

Date: 2026-08-04

## Status

Accepted. **Amends [0020](0020-a-family-term-narrows-it-does-not-conflict.md)**, whose Decision table
set the tie-break at *"most alias tokens matched, then lowest id"*. The direction of the match, the
family/conflict rule and everything else in 0020 stand unchanged; only the ranking is restated here.

## Context

0020 settled the *direction*: a curated form must sit inside the value, never the reverse. What it
left as an afterthought was the *ranking* — which node wins when a value carries forms of several.
It chose the count of significant tokens in the matched form, and justified it with an example where
both forms name the same node ("BD Rhapsody" outranks "Rhapsody"; a leaf outranks its own family).

Token count is a measure of an alias's **verbosity**. Across nodes it inverts (#266):

```
SPLiT-seq                                             -> splitseq
SPLiT-seq paired-end RNA-seq                          -> bulk-rnaseq-pe    WRONG
10x 3' v3 paired-end RNA-seq                          -> bulk-rnaseq-pe    WRONG
BD Rhapsody paired-end RNA-seq                        -> bulk-rnaseq-pe    WRONG
```

`bulk-rnaseq-pe` carried `paired-end RNA-seq` (4 significant tokens); `splitseq` carries `SPLiT-seq`
(2). Both are genuinely carried, `max` picks 4, and the generic entry wins a value that names a
single-cell chemistry. This is not #184's vacuous direction — `RNA-Seq` alone still correctly names
nothing — it is the ordering among forms that are all entailed.

**The obvious reading, and why it fails.** The KB is a tree, so rank by generality: a leaf beats the
family whose alias it contains. That is already true and already insufficient — `bulk-rnaseq-pe` and
`splitseq` are both root leaves, `parent: None`, neither an ancestor of the other. No tree walk
orders them, because their relationship is not one of descent. Nor can the ranking read genericness
off the KB's token statistics: that would make an answer depend on what else the KB holds, and the
resolved chemistry folds into `run_id`, so adding an unrelated spec would silently re-point an
existing dataset — the failure 0020's own tie-break exists to prevent.

Why it matters beyond tidiness: #257 gave the `smartseq3` ↔ `bulk-rnaseq-pe` confusable edge
`distinguishable_by: [metadata]`, which routes a near-tie to `_metadata_disambiguation`. On a real SRA
`LIBRARY_CONSTRUCTION_PROTOCOL` value — *"Smart-seq3 paired-end RNA-seq libraries were prepared…"* —
the old ranking would confirm `bulk-rnaseq-pe`, turning a near-tie into a **confident wrong answer at
exit 0**. `docs/agents/kb.md` ranks that as the worst outcome available.

## Decision

**A form's rank is its specificity, and specificity is declared or entailed — never counted.**

| | rank component | what it reads |
| --- | --- | --- |
| 1 | Does the form **name** the node, or only **describe** it? | `identity.aliases` vs the new `identity.descriptive_aliases`. A naming form always outranks a describing one, on any node. |
| 2 | Significant tokens of the best form matched in that class | unchanged from 0020 — a leaf outranks the family whose alias it contains |
| 3 | Does one tied form **say strictly more** than the other? | `carries(a, b) and not carries(b, a)` — the module's own entailment, one level up |
| 4 | Lowest id | unchanged from 0020 — the last resort, so the answer is total |

**A form belongs in `descriptive_aliases` when a different chemistry's record could carry it
truthfully.** "paired-end RNA-seq" is as true of a SPLiT-seq library as of a bulk one; "bulk RNA-seq"
is not, and stays an alias.

**A descriptive form still reaches its node** when the value names no other — demotion, not deletion.
Both lists stay in `curated_forms`, so span verification is unchanged; only `identity.aliases` is
shown to the extraction model.

## Why not mark the whole entry as the fallback

A flag on `bulk-rnaseq-pe` saying "lose to anything else" fixes the same six strings and is a smaller
schema change. It also demotes `bulk RNA-seq`, which **names** the chemistry — no single-cell record
says it — and would leave that entry unable to win on its own best evidence. The property being
recorded belongs to a *form*, not to an entry, so it is declared on the form.

## Why not rank by the length of the matched form

Length is the obvious proxy for "says more", and it was tried. It re-opens this record's own defect
one class up: on `10x 3' v3, bulk RNA-seq` both forms name a chemistry and both carry three
significant tokens, so length decides — and `bulk RNA-seq` (12 characters) beats `10x 3' v3` (9).
Verbosity beating a name is the thing being removed. Containment is the evidence length was standing
in for, and this module already has the predicate: `Smart-seq3xpress` carries `Smart-seq3` and is not
carried back, while neither of `bulk RNA-seq` / `10x 3' v3` carries the other, so the second pair
falls through to the id exactly as before.

## So in code

**Declare a describing form as one — never leave it in `aliases` and never delete it.** Deleting it
refuses an archive that describes a real bulk record the only way it knows, which is the
over-strictness #184 measured and rejected; leaving it in `aliases` lets it beat a chemistry name.
When you add a KB entry, read every alias and ask whether another chemistry's record could carry it
truthfully. A change to any of the four components is a change to a cached verdict and to `run_id`:
bump `RESOLVE_VERSION` **and** `KB_VERSION`, and pin every shipped-vocabulary string that moves.

**Enforced by.** `test_a_phrase_that_only_describes_the_format_never_outranks_a_chemistrys_own_name`,
`test_a_descriptive_phrase_still_names_the_bulk_entry_when_nothing_else_is_named`,
`test_a_tie_between_two_names_is_not_broken_by_which_one_is_longer`,
`test_the_longer_matched_name_breaks_a_tie_not_the_alphabetically_lower_id`,
`test_the_strings_this_rule_moves_are_pinned_where_it_moved_them` and
`test_chemistry_matching_does_not_depend_on_the_order_specs_were_loaded_in` (`tests/test_kb.py`);
`test_entailment_accepts_a_form_that_only_describes_the_run` (`tests/test_harvest.py`) holds the
demotion to ranking alone.

## Consequences

- **Ten strings in the KB's own vocabulary resolve differently**, measured over all 121 of them and
  pinned in `_MOVED_BY_266` (`tests/test_kb.py`). Three are the defect above. Six are versioned
  family aliases (`SC3Pv2`, `SC5Pv1`, …) that now reach the leaf declaring them, which is 0020's
  stated leaf-over-family rule finally holding. One was a wrong answer rather than a vague one: the
  Multiome **GEX** arm's verbatim name resolved to the **ATAC** arm, a different pipeline.
- **The benchmark tier could not have caught any of them.** Its grade digest is byte-identical across
  this change (#231, `247a9354…`), because no shipped case carries these strings. A green corpus is
  not evidence that a matcher change is inert, and the sweep is what stands in for that.
- `KB_VERSION` and `RESOLVE_VERSION` both re-key, so every dataset takes a new `run_id`. The stale
  direction is the dangerous one: a cached `bulk-rnaseq-pe` at exit 0 is a confident wrong answer.
- `EXTRACT_PROMPT_VERSION` moves too — the KB block the model reads lists only naming aliases now, so
  the cached prefix changes. `verify` still accepts the descriptive forms, so the asymmetry can only
  accept a draft, never reject one.
- **A value that names two chemistries is still ranked, not refused.** "SPLiT-seq bulk RNA-seq" names
  both and resolves to `bulk-rnaseq-pe` on token count, unchanged by this record. Whether a
  self-contradicting value should be a `Conflict` rather than a winner is 0020's question, left open.
