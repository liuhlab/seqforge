# 2. No test-impact analysis; the ladder is a rule, not a tool

Date: 2026-07-30

## Status

Accepted.

## Context

The agent ran `pixi run check` — the full suite — after every small edit, then opened a PR on which CI
ran the identical suite again. CLAUDE.md said to run it "when you change behaviour", twice, and offered
no other verb, so the behaviour was exactly what the instruction asked for.

The obvious fix is a tool that decides *for you* which tests a change can break: `pytest-testmon`, or
any of the coverage-graph test-impact-analysis (TIA) tools. Run the suite once, record which lines each
test executes, and thereafter select only the tests whose recorded lines the diff touched. It promises
rung 1 without needing anyone to choose a file.

That reading is the obvious one and a future reader will reach it again from the same evidence. This
record exists so they find the reason it was rejected rather than re-deriving it.

## Decision

**No test-impact analysis.** Instead: two semantic pytest markers (`external`, `repo`), three pixi
tasks (`test-fast`, `test-failed`, `check-fast`), test files mirrored onto module names so "which file
tests what I edited" has an answer, and a four-rung ladder written down as a rule in
[`docs/agents/testing.md`](../agents/testing.md).

## Why not TIA

**1. It tracks Python lines, and a large share of this suite's behaviour is data.** `kb/specs/*/spec.yaml`
is the knowledge base; `workflows/map/*.smk` are the shipped pipeline modules; the packed onlists are
barcodes. Editing a spec executes no new Python line, so TIA selects **zero tests and reports green** —
while `kb roundtrip`, the params gate, the confusability biconditional and every composed config could
all have changed. R8 exists because a KB entry must be executable and self-testing; a selector that
cannot see a KB edit silently repeals it.

**2. Silent under-selection is the failure mode this project refuses everywhere else.** The whole design
is built around refusing rather than guessing: `manifest validate` returns structured `Blocker`s and a
nonzero exit, `resolve` surfaces a `Conflict` it will not arbitrate, the metadata resolver leaves a
disputed attribute null rather than picking. A green run that skipped the relevant tests is the same
species of wrong as a plausible matrix in the wrong coordinate space — it does not look broken.

**3. The cost it was meant to remove was waste, not work.** The measured suite was **164s** on this
box, dominated by one `snakemake -n -p` spawned ~41 times to re-prove a fact about three hand-written
modules (worth 39s once it was paid per module instead), and by the KB's twelve YAML files re-parsed
~1,500 times (41s). Both are now paid once, and the suite is ~73s. TIA would have hidden that cost
behind a selector instead of removing it, and the waste would have stayed in CI, where the selector's
cache is cold anyway.

**4. A rule an agent can follow does not need a tool.** Rung 1 is `pytest <file> -k <expr>` at ~2s.
What was missing was never a selector; it was a sentence saying the full suite is a pre-PR gate, and a
file layout that makes the targeted run obvious.

## Consequences

- Choosing the file is the agent's job. `docs/agents/testing.md` carries the module→file table, and
  commit `232ca34` mirrored `tests/` onto packages so the table can be short and true.
- `test-fast` is only modestly faster than the full suite (~60s vs ~73s), because after the waste was
  removed there is not much left to deselect. That is the honest number and the doc says so; the
  ladder's real win is rung 1 and rung 4, not rung 2.
- If someone revisits this, the thing that would change the answer is the KB becoming Python rather
  than YAML — i.e. the data-driven premise in (1) going away. Nothing else here is about tooling
  quality.
