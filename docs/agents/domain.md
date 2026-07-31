# Domain Docs

How the engineering skills should consume this repo's domain documentation when exploring the codebase.

## Before exploring, read these

- **`CONTEXT.md`** at the repo root
- **`docs/adr/`** — read ADRs that touch the area you're about to work in

If any of these files don't exist, **proceed silently**. Don't flag their absence; don't suggest creating them upfront. The `/domain-modeling` skill (reached directly, or via `/grill-with-docs`) creates them lazily when terms or decisions actually get resolved.

## File structure

Single-context repo — one `pyproject.toml`, one distribution, and `docs/agents/layout.md` is explicit
that it must not be split:

```
/
├── CONTEXT.md
├── docs/adr/
│   ├── 0001-....md
│   └── 0002-....md
└── src/seqforge/
```

There is no `CONTEXT-MAP.md` and there should not be one. `src/seqforge/*/` are modules, not bounded
contexts; a per-module glossary would fragment vocabulary that the whole compiler shares.

## `CONTEXT.md` is a glossary, and only that

The agent-facing material is layered, and each layer answers one question:

- **`AGENTS.md`** (`CLAUDE.md` is a symlink to it) — the router: what seqforge is, R1–R11 as
  imperatives, and one pointer per area
- **`docs/agents/`** — the reference behind each pointer: `rules.md` (why each rule, and what enforces
  it), `testing.md`, `toolchain.md`, `layout.md`, `state.md`, `models.md`, `kb.md`, `resolve.md`,
  `cli.md`, `eval-corpus.md`, and this file
- **`docs/adr/`** — one decision per file: the alternatives, and why this one

`CONTEXT.md` is none of them. It maps a domain term to its definition and names the synonyms to avoid —
`manifest` vs `recipe`, `Observation` vs `Assertion`, `asserted` vs `inferred`, `run` vs `sample` vs
`library`. Nothing else belongs in it.

Before adding an entry, ask which of the three it is:

- a **rule** ("never read a whole FASTQ") → `AGENTS.md`, with its rationale in `docs/agents/rules.md`
- **rationale for a decision** ("why the manifest is hashed and the recipe is not") → a new
  `docs/adr/` entry; if it is not a decision with alternatives but a standing description of how one
  area works, it belongs in that area's `docs/agents/` page instead
- a **term** ("a *run* is one sequencing run; the grouping `resolve/group.py` produces from filenames")
  → `CONTEXT.md`

A term already argued at length in `docs/agents/` or an ADR is copied to `CONTEXT.md` as a one-line
gloss with a pointer, never restated at length. Two prose definitions of one term is the failure mode
this file exists to prevent.

## Use the glossary's vocabulary

When your output names a domain concept (in an issue title, a refactor proposal, a hypothesis, a test name), use the term as defined in `CONTEXT.md`. Don't drift to synonyms the glossary explicitly avoids.

If the concept you need isn't in the glossary yet, that's a signal — either you're inventing language the project doesn't use (reconsider) or there's a real gap (note it for `/domain-modeling`).

Two vocabularies are in play and they don't mix. **Domain** terms come from here. **Architecture**
terms (module, interface, depth, seam, adapter, leverage, locality) come from `/codebase-design` and
are fixed — don't substitute "component", "service", "API", or "boundary".

## Flag ADR conflicts

If your output contradicts an existing ADR, surface it explicitly rather than silently overriding:

> _Contradicts ADR-0001 (a head is joined to a whole file; there is no read-source seam) — but worth
> reopening because…_

## Neither tree is published

`docs/` is the mkdocs source for <https://liuhlab.github.io/seqforge/>, so `agents/` and `adr/` are
listed in `exclude_docs` in `mkdocs.yml`. The site is the carefully-designed end-user layer; ADRs and
the agent-facing reference carry open questions and dated measurements, and would read as settled
guidance under a docs URL. Adding a file here does not publish it, and a new agent-facing tree
under `docs/` should be added to `exclude_docs` too.
