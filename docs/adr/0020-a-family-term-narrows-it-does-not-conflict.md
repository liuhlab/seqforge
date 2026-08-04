# 20. A family term narrows; it does not conflict — and a word that names no chemistry asserts none

Date: 2026-08-02

## Status

Accepted. **Amended by [0028](0028-specificity-not-verbosity-ranks-a-chemistry-match.md)** on the
tie-break: ranking by alias token count measures verbosity, not specificity, so a phrase that only
describes a run outranked a chemistry's own name. The match direction and the family/conflict rule
below are unchanged.

## Context

An asserted chemistry reaches the byte resolver as a bare string, from three channels that share no
other code: a span-verified `library.chemistry` assertion, `manifest fill --chemistry`, and
`resolve score --assert-chemistry`. Before any guard can ask whether it *disagrees* with the bytes,
something has to answer a prior question — **which KB node, if any, does this string name?**

That answer was a three-pass matcher whose last pass tested substring **in both directions**: a
curated alias inside the value, *or* the value inside a curated alias. The second direction is
vacuous, and it turned an archive's filing vocabulary into chemistry claims:

```text
_match_tech('RNA-Seq')       -> 'bulk-rnaseq-pe'      # inside the alias "Illumina PE RNA-seq"
_match_tech('Illumina')      -> 'bulk-rnaseq-pe'      # the same alias, the other word
_match_tech('transcriptome') -> 'bd-rhapsody-wta'     # inside "…Whole Transcriptome Analysis"
_match_tech('WTA')           -> 'bd-rhapsody-wta'     # ...or 'bd-rhapsody-wta-enhanced', by dict order
```

Every transcriptomic run in SRA carries `library_strategy: RNA-Seq`, and a model asked for the
chemistry of an experiment record answers with it, quoting it verbatim. The quote greps back;
entailment is *vacuous* because the value sits inside its own quote; and the draft — naming a whole
field of assays — became a **bulk** assertion. Measured on the benchmark: a byte-provably single-cell
dataset became `conflict-bulk-asserted-single-cell-observed` and asked a human (GSE229022), and on
another the bogus claim displaced the real 5' hypothesis and the run emitted a 3' chemistry, with
`soloStrand Forward` for a Reverse library (GSE317744).

The obvious reading is that this is one bug in one matcher. It is two questions wearing one function:
*does this string name a chemistry at all*, and *does the chemistry it names disagree with the bytes*.
Answer them separately and the second one changes shape too — because once a family term like
"10x 3'" resolves to the family **node**, a resolver that treats any observed-vs-asserted difference
as a disagreement will report one against the family's own leaf.

## Decision

**One matcher, one direction, and a node — then narrowing is agreement.**

| | |
| --- | --- |
| **Match** | `resolve_chemistry(value) -> Spec \| None`: a node matches when one of its curated forms is **carried by** the value (substring, or all of the form's significant tokens present). `alias ⊆ needle` only. Tie-break: most alias tokens matched, then lowest id — **restated by [0028](0028-specificity-not-verbosity-ranks-a-chemistry-match.md)**, which puts a naming form above a describing one before the count and entailment between two tied forms after it. |
| **Reject** | a `library.chemistry` draft whose value resolves to `None` is refused `chemistry_names_no_kb_node`. The value must name a chemistry; the KB is what "a chemistry" means. |
| **Conflict** | `observed ∉ subtree(asserted)`. A family term that narrows to the leaf the bytes decided is **satisfied** by it — nothing was discarded, so nothing is surfaced. |

The three shapes an `observed`↔`asserted` chemistry difference can take, and there are only three:

- **narrowing** — asserted is an ancestor of observed. Agreement. No conflict, exit 0.
- **within family, not narrowing** — siblings or cousins (asserted v2, observed v3). The bytes decide
  the leaf and the discarded claim is kept as a `resolved` conflict. Unchanged by this record.
- **cross family** — an `open` conflict, exit 4, a human decides. Unchanged by this record.

## Why not strict exact-alias matching

The intuitive repair — require an exact KB id, name or alias — was prototyped against the live KB and
rejected. It refuses every realistic prose spelling:

```text
True   "10x 5'"                                             <- curated alias
False  "Chromium Next GEM Single-Cell 5' Reagent Kit v2"    <- a real record's own text
False  "Chromium Single Cell 3' v3"                         <- verify.py's own docstring example
False  "10x Genomics Chromium GEM-X Single Cell 5' Chip v3"
```

That closes the metadata-hypothesis channel in production while the benchmark stays green, because
the eval corpus supplies its hypotheses from recipes: the harness failing differently from the
product, which is the trap this work opened with. Entailment keeps the channel and still refuses the
generic word, because "RNA-Seq" carries no curated alias — it is *carried by* several.

## Why not an `rna-seq` family node

The other intuitive repair is to give the KB a node for the generic term so it resolves to something
harmless. It buys nothing once the matcher is one-directional — "RNA-Seq" would still not carry any
alias — and it is not free: every spec is folded into `KB_VERSION`, which is a `run_id` input
([ADR 0005](0005-run-id-is-the-pairing.md)), so a node added to fix a matcher re-keys every compiled
run in the corpus.

## Why a family term is not a weak conflict

It is tempting to record the narrowing case as a `resolved` conflict anyway, for the audit trail —
"three truths, never merged" (R4). But there is no third truth: the prose said "10x 3'", the bytes
said `10x-3p-gex-v3`, and the second is an instance of the first. A `Conflict` whose positions do not
disagree teaches a reader that the file is full of them, which is how a real one stops being read.
The sibling case is where the claim genuinely is discarded, and that one is still recorded.

## So in code

**Ask what a chemistry string NAMES before asking whether it disagrees — and never let the value sit
inside the alias.** Call `kb.resolve_chemistry`; it returns a node, so a family answer is
distinguishable from a leaf answer. Never re-implement the match: `harvest.verify.entails` and
`resolve_chemistry` are the same `carries()` primitive, one asking about a quote and one about the
KB. When comparing an asserted node with an observed one, `resolve.confuse.narrows_to` is the
membership test, and a true answer ends the comparison — a family term is not a disagreement of any
strength. A change here is a change to a cached verdict, so it bumps `RESOLVE_VERSION`.

**Enforced by.** `test_every_chemistry_string_the_corpus_produces_names_what_it_was_measured_to_name`,
`test_chemistry_matching_is_one_directional` and
`test_chemistry_matching_does_not_depend_on_the_order_specs_were_loaded_in` (`tests/test_kb.py`);
`test_verify_rejects_a_chemistry_draft_that_names_no_kb_node` (`tests/test_harvest.py`);
`test_narrows_to_is_directional_subtree_membership`,
`test_a_family_hypothesis_is_agreement_with_the_leaf_the_bytes_decided` and
`test_an_archive_filing_word_asserts_no_chemistry_at_all` (`tests/test_resolve.py`).

## Consequences

- **The dict-order hazard is gone with it.** The old last pass returned the first match in KB
  iteration order, so `WTA` named `bd-rhapsody-wta` only because that directory sorts first — adding
  a spec could silently re-point an unrelated dataset's `run_id`. Scoring every candidate and
  breaking the tie on `(alias tokens, id)` makes the answer a property of the strings. [0028](0028-specificity-not-verbosity-ranks-a-chemistry-match.md)
  keeps that property while replacing the count itself: every component it adds reads only the two
  strings, so no component can move with what else the KB happens to hold.
- **Both operator doors are closed by the same change.** `manifest fill --chemistry` (confidence 1.0)
  and `resolve score --assert-chemistry` never pass through `verify_drafts`, so a rejection there
  would have left them open; the matcher and the narrowing predicate sit below all three channels.
- **The narrowing guard cannot fire on today's KB, and is written anyway.** Every shipped family node
  declares a length *range* (`10x-3p-gex` R1 is 26-28) rather than a fixed one, so the length guard
  returns early, and no barcoded family has a barcodeless descendant. It states the invariant, not an
  observation: a family node that ever pins one number would otherwise manufacture a conflict against
  every leaf under it.
- **`RESOLVE_VERSION` re-keys** (`2026.7.17`). The defect is a cached refusal; without the bump the
  affected datasets keep being served their exit-4 conflict out of the cache. `evals` run with
  `use_cache=False` and cannot report this either way.
- **The divergent-tie fallback is deliberately NOT narrowed.** `_metadata_disambiguation` still picks
  a tie member by `same_family`, which is broader than `narrows_to` on purpose: "10x 3' v3" against a
  v2-versus-5' tie settles the 3'-versus-5' question completely while naming a leaf that is in the tie
  set's *sibling*, not its subtree. Narrowing that one would ask a human a question the document
  already answered. Membership decides what a *conflict* is; kinship decides what a *tie-break* may
  read, and they are different questions.
- The `Conflict` / `Question` split ([ADR 0006](0006-one-judgement-one-envelope.md)) and the
  block-on-bytes rule ([ADR 0010](0010-two-resolvers-one-blocks-one-warns.md)) are untouched: this
  record narrows *what counts as a disagreement*, never what happens once there is one.
