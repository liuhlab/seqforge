# 2. No test-impact analysis; the ladder is a rule, not a tool

Date: 2026-07-30

## Status

Accepted. Amended 2026-08-01 with the measurements it argues from; the decision is unchanged.

## Context

The agent ran `pixi run check` — the full suite — after every small edit, then opened a PR on which CI
ran the identical suite again. CLAUDE.md said to run it "when you change behaviour", twice, and offered
no other verb, so the behaviour was exactly what the instruction asked for.

The obvious fix is a tool that decides *for you* which tests a change can break: `pytest-testmon`, or
any of the coverage-graph test-impact-analysis (TIA) tools. Run the suite once, record which lines each
test executes, and thereafter select only the tests whose recorded lines the diff touched. It promises
step 1 without needing anyone to choose a file.

That reading is the obvious one and a future reader will reach it again from the same evidence. This
record exists so they find the reason it was rejected rather than re-deriving it.

**And it worked** (2026-08-01). The proposal came back — measure coverage in CI, select with
`pytest-testmon` — and this file was consulted, found, and answered it. That is the record doing its
job, and it is why what follows is an amendment rather than a supersession: the answer was right, and
what it was missing was a way for a reader to *check* it rather than take it. So the arguments below
carry their measurements now, cite the tool's own documentation instead of paraphrasing it, and answer
the CI form of the question separately from the local one. Everything dated 2026-08-01 is that
amendment.

## Decision

**No test-impact analysis.** Instead: two semantic pytest markers (`external`, `repo`), a pair of
narrowing pixi tasks (`test-fast`, `test-failed`), test files mirrored onto module names so "which
file tests what I edited" has an answer, and a three-step ladder written down as a rule in
[`docs/agents/testing.md`](../agents/testing.md).

## Why not TIA

**1. It tracks Python lines, and a large share of this suite's behaviour is data.** `kb/specs/*/spec.yaml`
is the knowledge base; `workflows/map/*.smk` are the shipped pipeline modules; the packed onlists are
barcodes. Editing a spec executes no new Python line, so TIA selects **zero tests and reports green** —
while `kb roundtrip`, the params gate, the confusability biconditional and every composed config could
all have changed. R8 exists because a KB entry must be executable and self-testing; a selector that
cannot see a KB edit silently repeals it.

*Measured* (2026-08-01), because "could all have changed" is not a number: **one line changed in one
`spec.yaml` turns fourteen test files red.** Two of those also change their *collected node count*,
because collection walks the KB directory to parametrize — so a data edit can move what the suite
*is*, not merely what it decides. A line-coverage selector selects none of the fourteen and cannot see
the two at all. How far one edit reaches varies, and this was a wide one out of the sixteen specs the
tree held that day; the blindness does not vary with it. No `spec.yaml` edit executes a new Python
line, so the selection is empty every time.

And this is not this project's reading of the tool; it is the tool's own documentation.
[testmon.org](https://www.testmon.org/) describes `pytest-testmon` as "collecting dependencies between
tests and all executed code (internally using Coverage.py) and comparing the dependencies against
changes", then lists what that does **not** reach — "static files (txt, xml, other project assets)".
A `spec.yaml` is a static file. The same page has the selector reading a database some earlier run
wrote ("build the dependency database and save it to `.testmondata`"), and the vendor's CI advice is
to buy a hosted central one, because that is "far more effective than sharing or caching
`.testmondata` files across CI runners". What a fresh runner does with no database at all is not a
sentence they publish — it follows from the mechanism, and it is the section after next.

**2. Silent under-selection is the failure mode this project refuses everywhere else.** The whole design
is built around refusing rather than guessing: `manifest validate` returns structured `Blocker`s and a
nonzero exit, `resolve` surfaces a `Conflict` it will not arbitrate, the metadata resolver leaves a
disputed attribute null rather than picking. A green run that skipped the relevant tests is the same
species of wrong as a plausible matrix in the wrong coordinate space — it does not look broken.

**3. The cost it was meant to remove was waste, not work.** The measured suite was **164s** on this
box, dominated by one `snakemake -n -p` spawned ~41 times to re-prove a fact about three hand-written
modules (worth 39s once it was paid per module instead), and by the KB's YAML files — twelve at the
decision, sixteen at this amendment, and growing — re-parsed ~1,500 times (41s). Both are now paid once, and the suite was ~73s when this
was written. TIA would have hidden that cost behind a selector instead of removing it, and the waste
would have stayed in CI, where the selector's cache is cold anyway.

**4. A rule an agent can follow does not need a tool.** Step 1 is `pytest <file> -k <expr>`, seconds.
What was missing was never a selector; it was a sentence saying the full suite is a pre-PR gate, and a
file layout that makes the targeted run obvious.

**5. What it would cost, not only what it would miss** (2026-08-01). A record that lists only
objections hides the trade, so here is the other half:

- **Instrumentation adds 25–70% to this suite's wall.** A coverage-graph selector pays that on every
  run, because a selector that does not record the graph as it goes is a selector that goes stale. It
  is a standing tax, levied to buy a discount the blindness in (1) says will not pay out on a data
  edit.
- **The correct partition was not cheaper than everything.** For that same `spec.yaml` edit, running
  exactly the files it reddens measured 17.1s against the suite's 15.0s — **1.14×**, so a *perfect*
  selector was negative, never mind a blind one. **This is the conditional number in this record.** It
  was taken before the test environment pinned its thread pools, and it holds only while naming files
  drops the run to serial and a bare invocation gets workers — a rule
  [`docs/agents/testing.md`](../agents/testing.md) owns, and one that could change. Re-take the pair
  before citing it; the argument that does not depend on it is (1), because a selector that cannot see
  the edit is wrong at any speed.

## Why not "just add coverage to CI"

Because that is two questions in one sentence, and they have different answers. Splitting them is the
whole of this section: answering the pair with one blanket "no" is how a reasonable change gets
refused by a record that was aimed at a different one.

**Coverage as a measurement is permitted.** `test-cov` already exists as a pixi task, and running it
as its own CI job is a reasonable separate change — it tells you which lines nothing executes, which
is a fact about the suite and not a decision about what to run. It is not what this record forbids.
Two conditions come with it, both out of (5): it runs **off the critical path**, because the
instrumentation cost is real and nothing should wait on it to merge; and what it produces informs a
human.

**Coverage as a selector is what is rejected**, in CI more sharply than locally. A selector's database
is state some *previous run on the same machine* produced. A CI runner is fresh, so it has none, and
the first run selects everything — which is the run you were trying to shorten. The two ways out are
both worse than the problem. Persist the database and you are caching a binary artefact whose validity
nothing in the tree can check: it is right or wrong according to a history the runner cannot see, and a
wrong one under-selects silently, which is the failure mode in (2) with a cache in front of it. Or hand
it to a service, and the answer to "which tests must run" now lives off the machine that has the code.
Locally a cold cache costs one full run and then warms; in CI it never warms, so the mechanism pays the
instrumentation tax on every run and returns the discount on none.

## So in code

**Choose the test file yourself; do not add a tool that chooses for you.** Step 1 is
`pytest <file> -k <expr>`, and `tests/` mirrors the package layout so the file is a lookup rather
than a search. Adding `pytest-testmon` or any coverage-graph selector is a regression against this
record. And when a change touches only data — a `spec.yaml`, a `.smk` module, a packed onlist — run
the suite that reads that data, because no line-coverage selector can see the edit at all. Measuring
coverage is a different act from selecting on it: `test-cov` may run in CI as its own non-blocking job.

**Enforced by.** **None exists.** Nothing stops the dependency being added, nothing asserts that
`tests/` still mirrors the packages, and nothing measures the suite's runtime. The ladder is a rule in
[`docs/agents/testing.md`](../agents/testing.md) and the numbers here are dated measurements, two
vintages of them — this record is followed by reading it, which is the honest state and is why it says
so.

## Consequences

- Choosing the file is the agent's job. `docs/agents/testing.md` carries the module→file table, and
  commit `232ca34` mirrored `tests/` onto packages so the table can be short and true.
- `test-fast` is only modestly faster than the full suite (~60s vs ~73s, 2026-07-30), because after
  the waste was removed there is not much left to deselect. That is the honest number and the doc says
  so; the ladder's real win is step 1 and step 3 — the targeted run, and reading CI instead of
  re-running it — not a cheaper way to run almost everything locally.
- **The form of "skip work when nothing changed" that survives every objection here is a key over a
  hash of the tracked tree, not over a coverage graph.** The tree holds the YAML, the workflow modules
  and the packed onlists, so the blindness in (1) does not apply; it needs no instrumentation, so the
  tax in (5) is not paid; it is identical on every runner, so CI can restore one rather than rebuild
  it; and an absent record degrades to a full run, which is the safe direction. It is **deferred, not
  adopted** — nothing here builds it — and it is named so the next reader arrives somewhere instead of
  only being turned away.
- If someone revisits this, the thing that would change the answer is the KB becoming Python rather
  than YAML — i.e. the data-driven premise in (1) going away. Nothing else here is about tooling
  quality.
- The 2026-08-01 figures were taken on 48 cores at load ~9–10, medians of repeated runs, against the
  commit the amendment was written at. They are a snapshot of one box on one day, which is what makes
  them checkable — re-take them, do not trust them, and least of all quote them about another machine.
