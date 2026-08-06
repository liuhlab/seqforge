# 42. A reference page declares what it covers, and a module with no page says so

Date: 2026-08-05

## Status

Accepted. Extends [ADR-0041](0041-four-layers-and-none-is-published.md), which decides that the
reference tree exists and what it is for, with the binding between a page and the code it describes.

## Context

The ADR tree has a mechanism for this and the reference tree had none.
[`README.md`](README.md)'s by-area table maps `src/seqforge/resolve/` to the fourteen records that
govern it, and `test_the_adr_index_and_the_adr_tree_hold_the_same_files` fails when the mapping rots,
so "read the records that touch the area you are about to edit" is an obeyable instruction.

[`docs/agents/resolve.md`](../agents/resolve.md) is 350 lines of standing description of the same
directory, edited 23 times in the three months to 2026-08-05 — it tracks the module closely. The only
thing binding the two was a row in [`AGENTS.md`](../../AGENTS.md)'s pointer table, written by hand and
read by nothing. Two failures follow from that and one of them has already happened:

- A page can go on claiming a directory that was renamed or deleted, and nothing notices.
- A page can exist and be unreachable. `docs/agents/triage-labels.md` was folded into
  `issue-tracker.md`; the row was updated by hand, correctly, by someone who happened to remember.

There is a third thing the router could never express. Eleven of the twenty modules under
`src/seqforge/` — `compose/`, `io/`, `manifest/`, `report/`, `workflows/`, `probe/`, `fingerprint/`,
`assets/`, `pipeline.py`, `project.py`, `recordset.py` — have no page at all. That is a defensible
state; it is not a *legible* one. A reader looking for `compose/`'s page cannot tell whether it is
missing or was never written, and a writer adding one cannot tell whether they are filling a gap or
forking a description that already lives in an ADR.

## Decision

**Every page of the reference tree opens with a `**Covers.**` block naming the paths it is the
standing description for, and every module under `src/seqforge/` either appears in one of those
blocks or in an explicit list of modules that need no page.**

The token is `**Covers.**` for the reason `**Enforced by.**` is one token across both trees: one
claim should not have two spellings. A block naming **no** path is legal and load-bearing — it is how
`rules.md`, `layout.md`, `comments.md` and `issue-tracker.md` say they are the standing description
of no single module, which is true of all four and was previously indistinguishable from an omission.

The router's pointer table and the tree are then held to the same set, in both directions.

## Why not derive the router table from the tree

Because the table's left column is editorial and the tree cannot produce it. *"harvest: the module
flow, the two span marks, the send list vs what reaches disk"* is a sentence written to make an agent
open the right page on the first try; a generated column would say `harvest.md` and buy nothing. What
is mechanical is the *set* of pages, and that is what is checked. The prose stays hand-written and
unchecked, exactly as the by-area table's glosses are.

## Why not require every module to have a page

Eleven pages nobody maintains is worse than eleven declarations. The failure mode is well attested in
this repo — a page written to satisfy a check, never read, and wrong within two releases — and the
alternative already exists here: `MODULES_WITHOUT_STATS` (`workflows/stats.py`,
[ADR-0025](0025-the-module-that-writes-an-artifact-owns-reading-it.md)) makes "this module reports no
QC metrics" a declaration with a reason rather than an absence. `_MODULES_WITHOUT_A_PAGE` is that
instrument pointed at prose, and it carries a reason per entry naming where the module *is* legible:
`layout.md`'s one-line map, plus the records that decide it.

The list is meant to shrink. Deleting a row is how a new page lands.

## Why not check that a page is *current*

The obvious next step is a freshness proxy — compare the page's last-touched commit against the
module's, and fail when the module has moved and the page has not. It is not built, and
[ADR-0039](0039-the-anti-restatement-gate-is-not-widened.md) is why: the same reasoning that killed
four candidate anti-restatement gates kills this one. A page can be entirely current across fifty
commits to the module it describes, because most commits do not change what the page says; a page can
be touched in the same commit and be touched for a typo. The proxy fires on honest prose and misses
the real case, which is the shape ADR-0039 measured and rejected. Presence and existence are checkable
and are checked; currency is a review obligation, and this record says so rather than implying a gate.

## So in code

**A new page under `docs/agents/` opens with a `**Covers.**` block and gets a row in `AGENTS.md`'s
pointer table, in the same commit.** Name real paths in backticks — a path that does not exist fails
the gate — and if the page is the standing description of no one module, say that in the block
instead. **A new module under `src/seqforge/` either gets a page or gets a row in
`_MODULES_WITHOUT_A_PAGE` (`tests/test_docs.py`) saying why it needs none.** A module may not have
both, and two pages may not claim one module.

**Enforced by.** `test_every_reference_page_declares_what_it_covers`,
`test_every_module_has_a_reference_page_or_is_declared_not_to` and
`test_the_router_lists_every_reference_page` (`tests/test_docs.py`). The first checks presence and
that every declared path is on disk; the second is the four-way set comparison against
`src/seqforge/`; the third holds the router's table and the tree to one set. None of the three reads
the prose, and no gate exists for whether a page's contents are still true.

## Consequences

- **Eleven modules are now declared to have no page, with a reason each.** That is the finding this
  record makes countable, and it is more than half the tree. `compose/`, `io/`, `manifest/`,
  `report/` and `workflows/` are the five where "no standing page yet" is a genuine gap rather than a
  deliberate choice; the other six are covered in full by a record or by `layout.md`'s entry.
- **A stray directory can no longer make the path check pass by accident.** The first version of the
  block on `state.md` named the run-time workspace tree in backticks; the tree happened to exist in
  the working copy, so a path that is absent on a clean checkout looked valid. The block now names it
  as a term, and the guard only stats what is written as a path.
- **The reference tree now has two of the three mechanisms the ADR tree has** — a scope declaration
  and a reachable index — and deliberately not the third. The ADR tree's `**Enforced by.**` has no
  counterpart here, because a description obliges nobody
  ([ADR-0041](0041-four-layers-and-none-is-published.md) is why the two trees are not merged, and
  this is one of the differences it turns on).
