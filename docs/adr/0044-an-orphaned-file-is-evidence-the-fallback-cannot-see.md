# 44. A read set that orphans the incumbent's barcode read does not anchor the tie band

Date: 2026-08-02

## Status

Accepted (2026-08-02), recorded 2026-08-05 — extracted from
[`docs/agents/kb.md`](../agents/kb.md) and [`docs/agents/resolve.md`](../agents/resolve.md), which
argued it in full in both places. Follows
[ADR-0029](0029-a-spec-declares-read-sets-not-a-fixed-read-list.md), which introduced the read sets
this rule exists to bound.

## Context

Read sets let one spec score each sequencing configuration its protocol publishes.
`bulk-rnaseq`'s single-end set is the consequential one: a one-role set that explains one file and
declines the other.

The leftover penalty was supposed to handle that. A one-role set on a two-file deposit pays `λ/1` for
the mate it declined to explain, and loses to a barcoded incumbent whose whitelist hit — by ~0.22 over
every single-cell leaf. **It is not enough when the incumbent's whitelist misses.** Then a barcodeless
fallback tops the ranking on a deposit that plainly holds a barcode read, and the resolver returns a
bulk gene-count matrix at exit 0 — which `kb.md` ranks as the worst outcome available here, because a
refusal is recoverable and a plausible matrix is not.

The evidence that the fallback is wrong is *in the file it dropped*. No gate on the fallback's own read
can see it, and no per-row cell in the matrix can either: the matrix scores each candidate against the
files it seats, and the whole defect is a file one candidate seats and the other does not.

The obvious remedy is the CI side — demand a `confusable_with` edge from `bulk-rnaseq` to every
chemistry it can outrank, so the pair is declared and the resolver asks. Run over the read sets, that
demand is an edge from `bulk-rnaseq`'s single-end set to all seven 28 bp-barcode leaves at +0.09, which
is the "edge to almost the whole KB" that
[ADR-0029](0029-a-spec-declares-read-sets-not-a-fixed-read-list.md) already rejected once by moving the
guard from validity to ordering. It would arrive by another route.

## Decision

**One predicate, `confuse.seats_a_file_the_fallback_dropped`, read by both the runtime and the CI
guard.**

At runtime, `escalate` will not let a barcodeless top anchor the tie band when its **proper-subset**
read set orphaned a file that a valid barcoded candidate seats as its **barcode role**. The barcoded
candidate anchors instead, so it is inside the band whatever the fallback scored, and the pair becomes
a divergent tie: the resolver **asks** rather than deciding.

In CI, the under-declaration sweep exempts exactly those pairs. The guard's danger is *"the resolver
would pick one and never ask"*, and on these pairs that is now false — so demanding a declaration
would be requiring one for a danger the resolver averts.

The predicate lives in `confuse.py` because that is where both can reach it, and the exemption **reads
it rather than restating it**, so a proxy for a runtime behaviour cannot drift from the behaviour.

**Scoped to a proper subset.** Every pair that predates read sets scores, ranks and escalates
byte-identically; the rule's blast radius equals the feature that introduced it.

## Why not the leftover penalty, tuned up

Because the penalty is per-file and the evidence is per-pair. Raising `λ` until a one-role set loses to
a *whitelist-missing* barcoded candidate also makes it lose to candidates it should beat — genuine
single-end bulk deposits, which are the reason the set exists. The number that fixes the bad case
breaks the good one, in the same direction, which is the signature of tuning a scalar to carry a
structural fact.

## Why not demand the CI edge anyway

Covered above: the demand is unbounded in the KB's size, not in the danger's. It also inverts the
declaration's meaning — `confusable_with` is a claim by an author that two chemistries are genuinely
hard to tell apart, and an edge from bulk to every barcoded leaf says nothing an author believes.

## Why the degenerate twin needs its own condition

**When the deposit is ONE cDNA file, the fallback orphans nothing.** It explains the single read it was
handed; what is missing was never deposited, so no other file carries the evidence the predicate looks
for. The only witness left is the **assertion** — and there the answer is a refusal, not a question: a
barcodeless top on a proper-subset read set, while the asserted chemistry's barcode role is unfillable
and its cDNA role fillable, is `Blocker(MISSING_TECHNICAL_READ)`.

That is the condition `_no_candidate_blocker` has always refused on, reached by a second entrance.
Before read sets, one file made every spec invalid and the dataset fell into that branch;
`bulk-rnaseq`'s `se` set made one candidate valid, and the refusal degraded into a question offering
`bulk-rnaseq` (#309, GSE208154).

Two things follow. **Descent must keep the asserted chemistry in the scored pool** even when
`length_feasible` drops it — its evaluation is the only thing that can answer *why* it could not be
seated, and it used to be scored only by the accident of `pool = [...] or runnable` firing on an empty
pool ([ADR-0043](0043-a-hypothesis-is-not-evidence.md) is where that pool-widening is decided). And
the rule stays scoped to a **proper** subset: a maximal-set bulk winner over an asserted single-cell
chemistry explained every file, so it stays `_single_cell_collapse_conflict`'s open `Conflict` at exit
4, where a human is shown a disagreement rather than a dead end.

## So in code

**When you add a rule that turns on a file a candidate did not seat, put the predicate in
`confuse.py` and have both the runtime and the sweep call it.** Two spellings of one condition is how
a CI guard comes to certify a behaviour the resolver no longer has. **Keep it scoped to a proper
subset read set**, and check what the maximal-set case already does before widening — for this rule it
is a `Conflict` at exit 4, which is a better answer than the question, not a worse one. **A deposit
with nothing orphaned is a refusal, not a question**: reach `MISSING_TECHNICAL_READ` rather than
offering the fallback as an answer.

**Enforced by.** `test_the_orphan_exemption_is_not_a_blanket_one` (`tests/test_kb.py`) strips
`bulk-rnaseq`'s edges and pins exactly which six come back flagged — an exemption nobody has watched
fail may be swallowing everything.
`test_the_never_deposited_blocker_fires_only_on_a_proper_subset_and_an_asserted_chemistry`
(`tests/test_resolve.py`) holds the degenerate twin to both of its conditions, and
`test_a_single_end_bulk_deposit_resolves_and_records_the_se_read_set` (same module) holds the case the
read set exists for, so the rule cannot be tightened into refusing genuine bulk.

## Consequences

- **The CI sweep's guard and the resolver's behaviour cannot disagree**, because they are one
  function. That is the property the record is really about; the exemption is the occasion.
- **A pair the resolver asks about needs no `confusable_with` edge.** The declaration means "hard to
  tell apart", and it stays meaning that, rather than becoming a list of every ordering CI observed.
- **`bulk-rnaseq` keeps six declared edges and no more**, re-derived from `rung02_margin` in
  `tests/test_kb.py` against the danger direction — bulk must not sit *decisively above* a chemistry
  on that chemistry's own reads.
- **The five `[onlist]` edges derived against the opposite arithmetic until #307**, because the
  support normalizer marked each incumbent down by however much whitelist evidence it had the honesty
  to declare. The margins either side of that fix are in
  [`support-normalizer-asymmetry.md`](../research/support-normalizer-asymmetry.md) (2026-08-05).
