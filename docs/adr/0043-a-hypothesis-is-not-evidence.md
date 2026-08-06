# 43. A hypothesis steers which candidates are computed, and never what they score

Date: 2026-07-20

## Status

Accepted (2026-07-20), recorded 2026-08-05 — extracted from
[`docs/agents/resolve.md`](../agents/resolve.md), which argued it at length in a page whose job is to
describe. Neighbour to [ADR-0040](0040-a-tie-the-prose-broke-is-recorded-as-one.md), which decides
what the artifact **records** of a tie the prose broke; this one decides how the prose is allowed to
reach it at all.

## Context

`score` takes an optional span-verified chemistry hypothesis, from `harvest`'s assertions or from an
operator's `--assert-chemistry`. It is the only place prose touches the byte resolver, and the
compiler's whole claim about that resolver — *what the library IS, from BYTES* — depends on what the
hypothesis is permitted to do.

The obvious reading is that a span-verified claim is evidence, and evidence belongs in the evidence
matrix: weight it, add it as a `supports` row, let a strong assertion lift a candidate the bytes rank
second. It is obvious enough that it has been reached twice from the same starting point, and the
matrix has a natural-looking slot for it — `global_support` already exists, at `γ = 0.001`.

The cost is not a wrong answer on one dataset. It is that the resolver stops being a function of the
bytes, so two datasets with identical FASTQs and different paperwork get different manifests and
different `dataset_hash`es, and nothing downstream can tell which of the two the number describes.

## Decision

**A hypothesis has zero evidential effects and exactly three control-flow ones.**

1. **It keeps the asserted spec in the scored pool** when `length_feasible` descent would drop it —
   the pool only ever widens — so the `MISSING_TECHNICAL_READ` branch has an evaluation to answer
   *why* that chemistry could not be seated.
2. **It breaks a processing-divergent tie, and only there**, through the escalation function.
3. **It is the asserted side of conflict detection** — the three detectors and the barcodeless-subset
   blocker read it, and none of them lets it decide.

It never enters the matrix, never un-gates a forbidden cell, and never wins a `Conflict`.

**The determinism argument.** For a fixed observation, the validity and finite score of any candidate
that gets computed is a pure function of the bytes; the hypothesis changes only *which* candidates are
computed. So the same observation with a different hypothesis yields an identical winner whenever the
bytes are decisive — a wrong hypothesis fails its own gates and forces the ladder down. Only where the
bytes are genuinely non-decisive, which means a processing-divergent pair, may it break the tie.

## Why not weight it in the matrix

Because "genuinely non-decisive" is a property of the bytes that the matrix can state exactly, and a
weight destroys it. At any weight above zero there exists a byte margin the assertion overturns, so
the question "did the data decide this, or did the paperwork?" stops having an answer — and that
question is what the rung recorded on every field is *for* (R9). At weight zero it is the decision
above, written more expensively.

The tie-break is the honest form of the same intent: it fires only where the matrix has already said
the bytes cannot separate two candidates, it is recorded as an `asserted` basis rather than folded
into the observed value, and [ADR-0040](0040-a-tie-the-prose-broke-is-recorded-as-one.md) makes
correcting it move `dataset_hash`, which is what makes the influence auditable.

## Why agreement-or-nothing, rather than the most recent or the best-supported claim

A dataset's verified assertions reduce to at most one hypothesis, and only when every
`library.chemistry` claim among them says the same thing. Two experiments describing two protocols is
a real dataset, and one dataset-level hypothesis would steer both — half of them wrongly. `None` is
the ordinary outcome and costs only a hint.

The rejected alternative is not hypothetical: the eval harness took the *last document to claim one*
while `manifest fill` reduced the same list by agreement, so the benchmark could steer a scorer
production would have left unsteered (#188, "the harness fails differently from production"). That is
why `chemistry_hypothesis` lives in `engine.py` beside `Hypothesis` with both callers reading the one
function.

## Why a record may withhold a hint but never raise one

`chemistry_hypothesis` also reads the deposit's own `library_source`: a record declaring a single-cell
library, standing over a **bulk** hint, makes that hint non-credible, so the hint is dropped. That is
the whole authority — it may not name a chemistry, move a score, or raise anything. The worst it can
do is decline to offer one, and the bytes decide either way.

Deliberately **not** a `Conflict`. Most single-cell deposits carry a bare `TRANSCRIPTOMIC`, so absence
carries no information whatever, and reading the pair as two comparable claims would false-block
correct datasets — a refusal on the strength of a field that is empty by convention. An operator's
`--assert-chemistry` is out of reach by construction: that branch builds its `Hypothesis` inline and
never calls this function, so an operator is never silently overruled by a record.

## So in code

**Do not give a hypothesis a weight, a matrix row, or a `supports` entry.** If you are adding a way
for prose to affect the byte resolver, it belongs in one of the three control-flow slots above or
nowhere: widen the pool, break an already-declared divergent tie, or be read as the asserted side of a
conflict. **Reduce assertions to a hypothesis through `chemistry_hypothesis` and never re-implement
the reduction** — a second reduction is how the harness and the compiler came to disagree — and when
you add a caller, add it to that function rather than beside it.

**Enforced by.** `test_chemistry_hypothesis_is_agreement_or_nothing`,
`test_chemistry_hypothesis_reads_only_the_chemistry_field`,
`test_a_single_cell_record_never_manufactures_a_hypothesis` and
`test_a_single_cell_deposit_rules_a_bulk_hint_out` (`tests/test_resolve.py`) hold the reduction and the
withholding rule; `test_a_family_hypothesis_is_agreement_with_the_leaf_the_bytes_decided`
(same module) holds the tie-break to the leaf the bytes chose;
`test_a_harvest_that_agrees_on_nothing_leaves_the_recipes_hypothesis_standing` and
`test_harvest_hypothesis_steers_resolve` (`tests/test_evals.py`) hold the two callers to one
reduction. **Nothing asserts the zero-weight claim directly** — it is structural, in that no matrix
assembly path reads a `Hypothesis` — so a weight added in future would be caught by review and by the
determinism tests going red on their fixtures, not by a guard that names the invariant.

## Consequences

- **The resolver stays a pure function of the bytes over every dataset where the bytes decide.** That
  is what lets `dataset_hash` mean "what the data IS" rather than "what the data IS, given the
  paperwork we happened to have" ([ADR-0004](0004-two-artifacts-not-one.md)).
- **A wrong assertion is cheap.** It fails its own gates and the ladder descends, so the cost of a
  hallucinated chemistry is a wasted evaluation rather than a wrong manifest — which is the property
  that lets the LLM seam stay a proposer under R2.
- **`--assert-chemistry` is not an override.** An operator who wants a chemistry the bytes reject gets
  a refusal, not a manifest. That is deliberate and occasionally unwelcome; the escape is to fix the
  KB entry the bytes are being scored against, which is the thing that was actually wrong.
- **What a string NAMES is a separate question from whether it disagrees**, answered by `kb.match` and
  decided in [ADR-0020](0020-a-family-term-narrows-it-does-not-conflict.md). A value naming no node
  asserts nothing and never becomes a hypothesis at all.
