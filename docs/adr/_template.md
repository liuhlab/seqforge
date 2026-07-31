# Template — the house structure of an ADR

This file is **not a decision.** It is the shape the seventeen records in this tree already have,
written down so a new one is not assembled by copying a neighbour and guessing which of its headings
are structural. It carries no number, so the index-completeness check skips it by name; leave it
that way.

Copy the skeleton below, take the next number, name the file `NNNN-<slug>.md`, and add a row to both
tables in [`README.md`](README.md).

---

## `# N. <the decision, stated as a claim>`

The title is the decision, not the topic. *"Two artifacts: the immutable dataset and the plural
recipe"*, not *"artifact structure"*. A reader scanning the index reads titles and nothing else.

## `Date: YYYY-MM-DD`

One line, immediately under the title. The date the decision was taken, not the date the file was
last edited.

## `## Status`

`Accepted.` — plus, on the same line, what it replaces: `Supersedes the typed tissue / condition
fields.` A decision taken earlier than the record carries its own date: `Accepted (2026-07-15).`

## `## Context`

What was there, what it cost, and **what the obvious reading is**. Several of these records exist
because a future reader will reach the rejected design again from the same evidence — say so, and
say that this file is why they will not have to re-derive it.

## `## Decision`

The decision, in bold, in as few words as it takes. Tables, a formula block or a short list where
that is the clearest form. No hedging: a decision that reads as a preference is not one.

## `## Why not <the obvious alternative>` — one per rejected reading, free-form

The rebuttal sections, and the reason the file is worth its length. Title them for the *alternative*,
not for the section's role: *"Why not the seam"*, *"Why not TIA"*, *"Why a controlled vocabulary
rather than a free-form dict"*, *"What is deliberately not evidenced"*. Number the arguments inside
one when there are several. Do not title one `Consequence:` — that is a rebuttal wearing the
consequences section's name, and an agent scanning for consequences hits it first.

## `## So in code`

Two blocks, and the reason an agent opens this file at all.

- **The imperative** — one bolded sentence stating what a reader must do or must not do when they
  next touch the code, then a few lines of the specifics it turns on. Write it so it would change
  what someone types. *"Key a sample attribute with one of NCBI's 960 names, or take the refusal"* is
  an obligation; *"the vocabulary is controlled"* is a summary of the Decision and buys nothing.
- **`**Enforced by.**`** — the test names or the file that enforces it, in backticks, with the test
  module named beside them. Every `test_<name>` written here is checked to exist
  (`test_the_enforcement_map_names_tests_that_exist`, `tests/test_docs.py`), so a rename turns this
  tree red rather than making it fiction. **If nothing enforces it, write `**None exists.**` and say
  what would have to change to notice a violation.** Do not invent a gate; an unenforced decision is
  worth knowing about.

  **Name the gate; do not gloss it.** [`docs/agents/rules.md`](../agents/rules.md) legitimately names
  many of the same tests — it is a map of what enforces each rule — and that overlap is by design.
  Copied *prose* is not: the enforcement map owns the full statement with its parentheticals, and
  this block is the shortest honest form, the name plus its module. If a sentence here would read
  identically in that file, delete it from here.

Both blocks are checked to be *present* (`test_every_record_names_what_enforces_it`, same module).
Nothing checks that the imperative is *useful* — a sentence can be there and buy nothing — so that
half is a review obligation, and it is stated rather than assumed.

## `## Consequences`

What follows, including the parts that are inconvenient: a cost paid, a field deleted, a known gap,
a claim that is architectural rather than measured. Link the records this one implies, and where a
term is doing work, name it as `CONTEXT.md` defines it so the vocabulary link runs both ways.

## `## Alternatives considered` — optional

A closing list where the rebuttal sections did not exhaust the space: one line per alternative and
the reason it lost. Skip it when the `Why not` sections already carry them.
