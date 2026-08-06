# 45. The agent-facing material is three layers, and none of them is published

Three layers, each answering one question: the router (`AGENTS.md`, which `CLAUDE.md` symlinks to)
says what this is and where to read next; the glossary (`CONTEXT-MAP.md` and the per-context
`CONTEXT.md` files under `src/seqforge/*/`) maps a term to its definition; the decisions
(`docs/adr/`, at the root and per context) say why it is this way and what lost. The fourth layer —
a standing prose description of how each area works now — is gone: a page maintained against the
code restates it, drifts from it, and is read as current anyway. None of the three is published to
the site, because agent-facing material carries open questions and measurements true only as of a
date, and under a docs URL every one of those reads as settled guidance.

**Status.** Supersedes ADR-0041 and ADR-0042.
