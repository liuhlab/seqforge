---
search:
  exclude: true
---

# Writing rules

Three rules. Two are checked by `pixi run vale`, which runs in CI and outside `pixi run check`;
the third is on you.

## 1. Be concise

Shorter beats longer — in documents, issues, commit messages, replies, and code. If a
sentence survives deletion without loss, delete it. The caps below are ceilings, not targets.

Code's share of this rule is its comments and docstrings, which no cap reaches. `AGENTS.md`
says what belongs in one and where each fact lives when it does not.

## 2. Agent-facing documents have word caps

| Files | Cap |
| --- | --- |
| `AGENTS.md`, `docs/agents/*`, `skills/*/SKILL.md` | 1000 (`Lab.LengthDoc`) |
| `docs/adr/*`, `src/seqforge/*/docs/adr/*` | 400 (`Lab.LengthAdr`) |
| `GLOSSARY-MAP.md`, `src/seqforge/*/GLOSSARY.md` | none per file |
| `docs/research/*` | none — research notes are long by nature |

A glossary has no file cap. It grows one term at a time and never shrinks, so a file-level
count measures how big the domain is, not how well the entries are written — a cap set for
fourteen terms fires again at sixteen. A per-entry cap is the unit that matches. What can go
wrong with a long glossary is the number of entries, which no word cap was going to catch.

**Measure with vale, not with `wc`.** `wc -w` counts table pipes, link targets and
shell flags as words; vale does not, and vale is what enforces the cap. The gap scales with
markup — measured across the lab template it runs from 10% on prose to 65% on a page that is mostly
a table, so `wc` will tell you a reference page is near a cap it is nowhere near. Run
`pixi run vale` and believe it.

The caps are dials with tight defaults. Raising one is a one-line diff in `styles/Lab/` —
do that deliberately, and say why in the commit message. Do not raise a cap because a
document ran long; that is the cap working.

## 3. Human-facing prose avoids jargon and stays readable

`README.md`, `CHANGELOG.md`, every `docs/` page a human browses, and the KB assay READMEs the site
includes: no terms from the lab jargon list
(`Lab.Jargon` — architecture-speak and Latinate verbs), and reading grade 11 or below
(`Lab.Readability`). No length cap — a tutorial is as long as the task.

**When `Lab.Readability` fails, match a passing exemplar — do not attack the number.** The
grade is a ratio over structure, so shaving words inside sentences you already committed to
moves it by hundredths. Find text that passes — an older section, a sibling page — and rewrite
toward how it is segmented. Measured on a 21,000-word changelog: optimising the grade directly
moved it 12.46 to 12.41; matching a passing section in the same file moved it under 11, while
cutting only 3% of the words and *raising* the entry count.

**This rule also covers what you say, not only what you write.** Nothing checks a chat
reply, so it is on you: when a human asks, answer in plain language and explain the term
you would otherwise reach for. An unexplained term in conversation is the same failure as
one in the README.

## Which is which

Agent-facing means written for a machine that has to act: capped, exempt from the jargon
and readability rules, kept out of the site navigation. Human-facing means written for a
person reading the published site: checked for jargon and readability, uncapped. A file is
one or the other — if you are adding a document and cannot tell, it is human-facing.
