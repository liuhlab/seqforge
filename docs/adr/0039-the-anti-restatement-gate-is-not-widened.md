# 39. The anti-restatement gate stays scoped to one page, because widening it was measured and does not work

Date: 2026-08-05

## Status

Accepted. Records a rejected change to
[ADR-0002](0002-no-test-impact-analysis.md)'s neighbourhood — the doc gates in `tests/test_docs.py` —
taken after the 2026-08-05 audit of the agent-facing tree recommended the opposite.

## Context

`test_a_section_that_links_an_adr_glosses_it_rather_than_restating_it` reads
[`docs/agents/rules.md`](../agents/rules.md) and nothing else: `_rule_sections()` is bound to one
path. Twelve other `docs/agents/` pages link ADRs and none is checked.

An audit partitioned across nine readers found that gap independently from seven of the nine
partitions, and it was not theoretical. `docs/agents/toolchain.md` carried five passages of
[ADR-0017](0017-one-type-checker-and-the-editor-runs-it.md) at ~30 lines; `kb.md` re-argued
[ADR-0032](0032-a-spec-declares-the-shape-of-a-deposit.md) in a section that closed with the words
"Argued once in ADR-0032"; `resolve.md` re-argued the *same* record in 72 lines; `models.md` restated
[ADR-0009](0009-llm-provider-is-pluggable.md) in a section about a module that is not `models/`.

The obvious conclusion — point the guard at every page — is the one this record rejects. Each
duplication above was removed by hand in the same change; what is at issue is whether a gate can stop
them coming back.

## Decision

**`rules.md` stays the only gated page, and the duplication the audit found is held by review rather
than by a check.**

Four widenings were implemented and each was validated the same way: run it over the tree *before*
the audit's cleanup and *after*. A gate worth having must score the uncleaned tree worse.

| design | result |
| --- | --- |
| section-scoped line budget, all 13 pages | 17 sections over budget, including honest ones |
| budget normalised by the number of ADRs a section links | fixes a list of 7 links; still fires on long honest sections |
| paragraph-scoped, normalised per ADR | **22 before the cleanup, 21 after** |
| longest contiguous shared word-run, page against the ADR it links | `toolchain.md` scores **21**, ranking *below* four complying pages at 24–31 |

The third row is the finding: the best line-based design cannot distinguish the tree that carried
~250 lines of restatement from the tree that does not. The fourth is why no threshold rescues it.

## Why not a line budget over every page

Because a reference page's length is not evidence of anything. `docs/agents/eval-corpus.md` has a
section of 197 non-blank lines that links one ADR in a single clause; `docs/agents/kb.md` has a
21-line description of the confusability sweep whose only ADR reference is the aside "the question is
an **ordering** one and used to be a validity one". Both are exactly what a standing description of an
area is for, and both are indistinguishable, by line count, from a section that re-argues the record
it cites.

Normalising by the number of ADRs linked was tried because it fixes the sharpest false positive —
`docs/agents/harvest.md`'s reading order spends 25 lines glossing 8 records, which is the *complying*
shape the original guard's docstring describes. It scores 3.1 lines per record, better than most of
`rules.md`. But the same normalisation hands a long section that cites one record a budget of one
record's worth, which is the false positive above, unchanged.

## Why not a plagiarism-style overlap check

It measures the right thing — the defect was **copying**, not length — and it fails on this corpus in
both directions.

It misses the real case. `toolchain.md`'s five passages were *near*-verbatim: reordered clauses,
substituted connectives, an em dash for a semicolon. Small edits break contiguous matching, so its
longest shared run with ADR-0017 was 21 words.

It flags the honest case. `rules.md` shares a 31-word run with
[ADR-0016](0016-no-held-out-dataset.md) and a 29-word run with
[ADR-0010](0010-two-resolvers-one-blocks-one-warns.md). Both are single sentences, and both are the
one-line gloss the rule is *supposed* to carry — a gloss of a decision is a short restatement of it,
which is the whole difficulty. Any threshold that catches 21 words of paraphrase red-lights every
compliant gloss in the tree.

## Why not gate on something else and keep the scope

Considered and not built: requiring that a section linking an ADR contain no `**bold imperative**` of
its own (proxy for "it is arguing"), and requiring a link to appear within N lines of the section
heading (proxy for "it points before it explains"). Both are shape rules rather than content rules,
both are trivially satisfied without changing what a reader gets, and neither was measured because
neither would have fired on `toolchain.md`, whose copied passages sit under a heading that does link
the record — twice.

## So in code

**Do not widen `test_a_section_that_links_an_adr_glosses_it_rather_than_restating_it` past
`rules.md` without re-running the before/after validation in this record, and do not tighten
`_RATIONALE_BUDGET` to reach a section you can see restating an ADR.** The guard catches regrowth past
the worst observed case on one page; it is not, and cannot be made into, a detector of restatement.
When you find a page re-arguing a record — and the audit found four — the fix is the edit, and the
record is the owner.

**The overlap sweep survives as a reading tool, not a gate.** Ranking every `docs/agents/` page by
shared word-runs against the ADRs it links is how the last of the four duplications was found: it
ranked `toolchain.md` second, and that passage had been missed because the finding named a file its
reader did not own. Run it when editing a page that links a record; do not assert on its output.

**Enforced by.** **None exists**, and this record is the reason. `_rule_sections()`
(`tests/test_docs.py`) remains bound to `RULES`, which is what makes the scope visible in one line
rather than spread across a glob. Noticing a violation on the other twelve pages mechanically would
need a measure that separates a gloss from a paraphrase of the same sentence, and the table above is
the evidence that a line count and a substring match are both the wrong instrument. What would change
the answer is a semantic comparison — an embedding of the section against the record's Decision — and
that is a model in the test suite, which R1's neighbourhood declines for the same reason it declines
one anywhere else: nothing downstream could check it.

## Consequences

- **The twelve unchecked pages are checked by review, and the audit is the baseline.** The
  duplications it found are removed; a reader wanting to know whether a page has regrown one has the
  sweep above and this record's numbers to compare against.
- **`rules.md`'s guard keeps its narrow scope and its docstring keeps its warning.** That file's
  derivation — the budget is the widest *complying* section plus two lines — stays the model for any
  future doc gate: derive the number from what passes, never from what you wish failed.
- **A finding that names a file its reader does not own can be dropped silently.** That is how
  `toolchain.md` survived the cleanup pass; the audit partitioned by file ownership, and the finding
  crossed a partition boundary. The lesson is about the process, not the tree: a cross-file finding
  needs an owner assigned explicitly, or it belongs to nobody.
- **This record is a decision not to build something**, which is the shape the tree carries least
  often and the one most likely to be re-opened. It is written down precisely because "just point the
  guard at all of them" is a one-line change that looks obviously right and is measurably not.
