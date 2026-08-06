# 41. The agent-facing material is four layers, and none of them is published

Date: 2026-08-05

## Status

Accepted (2026-07-12), recorded 2026-08-05. Supersedes
`docs/agents/domain.md`, which stated the layering from inside the tree it describes and is deleted
by this record.

## Context

Five files state this policy, each in full, each written for a different reader, and none of them
owns it: `docs/agents/domain.md` (24 lines), `mkdocs.yml`'s pre-`exclude_docs` comment (17),
`.markdownlint-cli2.yaml`'s header and `ignores` comment (11), `tests/test_docs.py`'s module
docstring (9), and the *Docs* section of [`docs/agents/toolchain.md`](../agents/toolchain.md) (5).
Two of the five sit inside the tree they describe, and the circularity closed:
[`README.md`](README.md) said placement was "settled in `docs/agents/domain.md`", and `domain.md`
sent the reader back here for the decisions.

That is the failure [`CONTEXT.md`](../../CONTEXT.md) exists to prevent, applied to a policy instead
of a term — and it had already produced drift. `domain.md` and `toolchain.md` both said "three
trees" while the router's pointer table listed fourteen pages of one of them, and `domain.md`'s own
file-structure diagram showed `CONTEXT.md` and `docs/adr/` and omitted the tree the page was in.

The obvious reading, and the reason this record exists rather than a sixth paragraph: **documentation
is documentation, so put it in one place.** That reading arrives at "fold `docs/agents/` into
`docs/adr/` and keep one source" — which is a specific, plausible, one-afternoon change that has been
proposed at least twice. It is rebutted below with the numbers, so it does not have to be re-derived
a third time.

## Decision

**Four layers, each answering exactly one question, and a new piece of writing belongs to whichever
question it answers.**

| layer | answers | lifetime |
| --- | --- | --- |
| [`AGENTS.md`](../../AGENTS.md) (`CLAUDE.md` symlinks to it) — the router | *what is this, and where do I read next?* | edited when an area appears or moves |
| [`docs/agents/`](../agents/) — the reference | *how does this area work **now**?* | maintained; tracks the code |
| [`docs/adr/`](.) — the record | *why is it this way, and what lost?* | written once, glossed thereafter |
| [`docs/research/`](../research/) — the measurement | *what did one investigation establish, on what date, and what could it **not**?* | superseded, never edited |

[`CONTEXT.md`](../../CONTEXT.md) is none of the four. It maps a domain term to its definition and
names the synonyms to avoid; a term one of the four argues at length appears there as a one-line
gloss and a pointer.

**None of the four is published.** `agents/`, `adr/` and `research/` are listed in `exclude_docs`
(`mkdocs.yml`) and in `ignores` (`.markdownlint-cli2.yaml`), and the router and the glossary live at
the repo root rather than under `docs/`. The site at <https://liuhlab.github.io/seqforge/> is the
human layer built on top of all four.

The placement test a writer applies — rule, decision, term, or measurement — is stated once, in
[`README.md`](README.md), because that is the tree a writer is most often deciding whether to add to.

## Why not one tree

Because the two middle layers have different lifetimes, and merging them costs four things.

**Their churn signature is the genre difference, measured.** Over the three months to 2026-08-05,
commits touching each file:

| file | commits | file | commits |
| --- | --- | --- | --- |
| `agents/eval-corpus.md` | 30 | `adr/0009-llm-provider-is-pluggable.md` | 10 |
| `agents/resolve.md` | 23 | `adr/0025-…owns-reading-it.md` | 5 |
| `agents/kb.md` | 22 | `adr/0033-…transcript-entry….md` | 4 |
| `agents/testing.md` | 21 | `adr/0014-no-inf-across-the-json-seam.md` | 4 |

A reference page is maintained against the module it describes. A record is written once and glossed.
One directory holding both means a reader cannot tell, from the tree, which kind of file they opened.

**The dependency runs both ways.** Thirteen records plus the index and the template link *into*
`docs/agents/`; [ADR-0002](0002-no-test-impact-analysis.md) and
[ADR-0038](0038-loadgroup-over-loadfile-and-grouping-is-decided-per-module.md) both close by handing
the reader `testing.md` for the standing rule, which is the division working. Merging turns fourteen
cross-tree links into self-links and loses the distinction they were drawing.

**The record invariant would have to be relaxed or faked.** Every file in this tree owes
`## So in code` and `**Enforced by.**`, checked by `test_every_record_names_what_enforces_it`. A page
describing the `seqforge/` output tree owes no imperative — it describes, it does not oblige. The
merge ends in fourteen bolted-on blocks that buy nothing, or in dropping the gate for all fifty-four
files.

**The by-area index would stop answering one question.** "Which records govern `resolve/`" returns
fourteen decisions today. After a merge it returns fourteen decisions and a 350-line reference page,
ranked alike — and a permanent number would have been spent on a file that decides nothing.

## Why not publish them

Agent-facing material carries open questions, values deliberately not verified, and measurements that
are true as of a date. Under a docs URL every one of those reads as settled guidance.
`docs/research/` is the sharpest case: a note ends in what its investigation could **not** establish,
which is exactly the part a reader arriving from a search result skips.
[ADR-0018](0018-a-red-benchmark-case-is-published-anyway.md) publishes a red benchmark case *inside*
the repo for the same reason this declines to publish the tree outside it — the audience is the
difference, not the honesty.

## Why not three layers, folding the router into the reference

The router is read in full, every session, by something with a context budget; the reference is read
one page at a time, on demand. That is a different document even when the sentences would be similar,
and it is why `AGENTS.md` carries R1–R11 as bare imperatives while
[`docs/agents/rules.md`](../agents/rules.md) carries the rationale and the gate for each. The two are
held in agreement by `test_the_router_and_the_enforcement_map_name_the_same_rules` rather than by
being one file.

## So in code

**A new tree under `docs/` is added to `exclude_docs` in `mkdocs.yml` and to `ignores` in
`.markdownlint-cli2.yaml` in the same commit — they are one list.** Adding a *file* to an existing
tree needs neither; the directory entries cover it. And before writing, ask which of the four
questions above the writing answers: a rule goes to the router with its rationale in
`docs/agents/rules.md`, a decision with a rejected alternative comes here, a term goes to
`CONTEXT.md`, a measurement goes to `docs/research/` with its date and method. The long form of that
test is in [`README.md`](README.md).

**Enforced by.** `test_everything_excluded_from_the_site_is_also_unlinted` (`tests/test_docs.py`)
holds the two lists to being the same list, one-directionally — everything mkdocs hides must be
unlinted, while `ignores` may legitimately hold more. It is the gate that exists because the lists
drifted once and turned the `markdownlint` job red on every open PR. Nothing enforces the placement
test itself, and nothing can: which of four questions a paragraph answers is a judgement made at
write time, and the closest mechanical proxy was measured and rejected in
[ADR-0039](0039-the-anti-restatement-gate-is-not-widened.md).

## Consequences

- **`docs/agents/domain.md` is deleted.** Its placement test and its ADR-conflict instruction move to
  [`README.md`](README.md); its glossary-vocabulary paragraph moves to `CONTEXT.md`, which owns the
  vocabulary; its layering diagram and publication argument are this record. The four config sites
  keep a one-line pointer here instead of the argument.
- **The reference tree is named for its audience, and every other layer is named for its content.**
  `adr/` holds decisions, `research/` holds measurements, `CONTEXT.md` holds terms, and `agents/`
  holds… agents. Renaming it to `docs/reference/` was considered and deferred: roughly fifty inbound
  links across this tree, the glossary, four research notes, four KB spec READMEs, five test modules
  and both lint configs, for a naming win. Worth doing with a change that already touches those
  sites, not on its own.
- **This record is a decision not to merge**, which is the same shape as
  [ADR-0039](0039-the-anti-restatement-gate-is-not-widened.md) and re-opened for the same reason: the
  merge is a one-line description of a one-afternoon change, and it looks obviously right. The
  numbers above are what make it not.
- **A single-context repo, and no `CONTEXT-MAP.md`.** One `pyproject.toml`, one distribution;
  `src/seqforge/*/` are modules, not bounded contexts, and a per-module glossary would fragment
  vocabulary the whole compiler shares. That is why the glossary is one root file and not one per
  package.
