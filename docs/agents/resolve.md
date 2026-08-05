# Deciding the library from bytes: evaluators, the evidence matrix, the ladder

Read this when you touch `resolve/scoring`, `resolve/assign`, or `resolve/escalate` — the **byte
resolver**, which answers "what IS this library?" from bytes and blocks on disagreement. Its sibling,
the metadata resolver in [`resolve/records.py`](../../src/seqforge/resolve/records.py), answers "which
sample is each file, and what was it?" from records and prose, *decides*, and only warns; the line
between them is [ADR-0010](../adr/0010-two-resolvers-one-blocks-one-warns.md).

There is no LLM anywhere in this file's subject. Terms (`Hypothesis`, `Candidate`, `Role`,
`Role assignment`, `Rung`, `Confusable`) are in [`CONTEXT.md`](../../CONTEXT.md).

## The signature-test evaluators

`local_score(test, role, observation | tech)` returns a value in `[0,1]` for a `supports` test, or one
of `PASS` / `FAIL` / `ABSTAIN` for a `requires` or `excludes` gate. The set is closed and is
**identical to the vocabulary a KB spec may use** ([`kb.md`](kb.md)) — adding an evaluator is adding a
word to the DSL, not a local change.

**`ABSTAIN` is first-class and never gates.** "The probe cannot see this signal" is a different fact
from "the signal is absent", and conflating them would reject every SRA-normalized dataset on a header
test alone.

Gate semantics: a `requires` FAIL forbids the cell, an `excludes` PASS forbids the cell, and
`supports` sum as `Σ weight · score` with weights summing to 1, so a finite cell lands in `[0,1]`.

| evaluator | what it does, and the decision inside it |
|---|---|
| `segment_length` | triangular around the declared length for scoring; passes iff the mode is within a gate tolerance. This is the gate that separates a 28 bp v3 read from a 26 bp v2 read. Open-ended cDNA uses a minimum-length variant. |
| `has_segment` | `constant` is a **proportion**: the share of reads carrying the window's modal consensus to within a per-base slack, gated at a majority. `random` / `polyT` / `polyA` are population properties and stay mean per-cycle evidence — near-uniform, or a base fraction above a minimum. |
| `distinct_ratio` | distinct over total across the window. **Supports-only, never a gate.** Depth-dependent, so only meaningful normalized: a "high" expectation (UMI, cDNA) needs `4^len` far above the sampled read count or the ratio saturates below the band, and a "low" expectation (CB) is conditioned on the estimated reads per cell. It *proposes* CB-versus-UMI; the onlist confirms. |
| `onlist_hit_rate` | the rung-3 hypothesis test. **Width-generic** — the barcode length comes from the registry entry, so SPLiT-seq's 8 bp blocks work without a second code path. Tests forward *and* reverse-complement *and* a small positional offset scan, and records the winning orientation and offset. Floor is `onlist_length / 4^len`; score is the clipped rise from floor to the passing minimum; passes at roughly 0.6. At Q30 the discriminative power is about 500:1. |
| `motif_present` | fraction of reads matching an IUPAC motif within a tolerance, in an **inclusive** window. Used for fixed linkers, and as an `excludes` test to reject 10x when an internal linker turns up in a barcode read. Shares the hit rate's coverage policy: an uncalled base is **not** a substitution, so it never eats the tolerance, and a read never called where the motif was looked for leaves the denominator. Only the offsets the motif *constrains* can be lost — a cycle under an `N` was never evidence. What a loss costs depends on what the search **declares**: `read_start` / `read_end` / a closed `window` name the motif's place, so an uncalled base at any of their constrained offsets costs the whole read; `anywhere`, and a `window` left open at the end, declare nothing and cost only the positions they reach. |
| `base_composition` | element-addressable, so it can target a floating region rather than only the first few cycles. |
| `header_index` | abstains when the header is SRA-normalized — and that abstention is how probe *detects* the normalization. Otherwise it checks for a per-file, roughly constant 8–10 bp index. |

**Why `constant` counts reads rather than averaging cycles.** A mean per-cycle purity cannot tell
"every read carries this linker" from "most do and the rest of the head is junk" — the two agree only
on a head with no junk in it, which is the one kind of head `kb roundtrip` generates. Calibrated
there, a 0.9 purity bar forbade real SPLiT-seq's barcode read (linker1 0.905, linker2 0.827 over the
head; 0.99+ over the ~61 % of it that is genuinely SPLiT-seq), so `bulk-rnaseq` won on geometry at
exit 0 with three correct whitelists never consulted — the plausible-matrix failure, not a refusal.
The proportion is the shape that survives, and the reason it survives is that junk reads are
**counted, not filtered**: they stay in the denominator, so contamination lowers the statistic instead
of being removed from its own measurement. Select the reads that agree with the consensus and then
measure their agreement and every window in every dataset reads ~1.0, noise included — a gate that
cannot fail is worse than one calibrated too tight. Real SPLiT-seq measures 0.85 / 0.73 against a
majority bar; a window with no fixed sequence in it measures ~0.

One trap worth naming: **a reverse-complement onlist hit means the barcode read is on the other
strand, so supply the reverse-complemented whitelist file.** It does *not* flip the strand parameter,
which is the KB's 3′-versus-5′ property and a different fact entirely.

## The evidence matrix

For a technology with roles `R` and the dataset's files `F`, cell `M[r][f]` is `FORBIDDEN` if any
`requires` gate for that role fails or any `excludes` gate passes, and otherwise the weighted sum of
that role's `supports` evaluators against that file's observation.

`FORBIDDEN` is the internal sentinel `float("-inf")` **for computation only**. Serialized, a cell is
either forbidden or scored, as a tagged object — **no `±inf` ever crosses the JSON boundary**
([ADR-0014](../adr/0014-no-inf-across-the-json-seam.md)).

## The joint optimization

Chemistry is not identified and then assigned; the two are one decision. An assignment is an
**injective** map from a spec's roles to files — each role a distinct file — and leftover files are
unassigned at a penalty. An assignment is valid when no forbidden cell is selected and the
dataset-level `requires` hold. Most of those globals *decompose*: "exactly one 28 bp read and one cDNA
read" falls out of the per-row gates plus injectivity, and only a genuinely non-decomposable global
needs an explicit post-check.

```text
raw(t)   = max over valid A of  Σ_r ( M[r][A(r)] + β · prior(r, A(r)) )
score(t) = raw(t) / |R|  −  (λ / |R|) · |unassigned files|
score(t) = −∞ if no valid assignment exists
```

**Normalizing by the role count is what makes techs comparable.** An unnormalized sum biases the
argmax toward high-role-count technologies, so a six-role SPLiT-seq would beat a two-role 10x on
cardinality alone.

**`R` is the active read set's reads, and the loop over sets is INSIDE the technology evaluation.** A
spec declares a maximal read set and may name subsets of it ([`kb.md`](kb.md)), so scoring a chemistry
means scoring each configuration it publishes and keeping the best — which is why there is still
exactly **one `Candidate` per spec**, and why ranking, equivalence, escalation and the divergent-tie
machinery needed no clause saying a chemistry does not tie with itself. A forbidden set scores `−∞`, so
validity needs no special case, and an exact tie prefers the **larger** set (it explains more of the
data, and the order must be deterministic because the answer feeds a content-addressed artifact). A
signature test addressed to a read outside the active set is *inapplicable*: it has no cell, so it
enters neither the numerator nor its normalizer, which is already how a nonexistent cell behaves. The
winning set is recorded on the `Candidate` — the resolve artifacts, where "how this was decided" lives —
and **not** on the manifest, whose read layout already lists exactly that set's reads.

**Two consequences worth carrying.** The leftover penalty is what makes a smaller set lose when it
should: a one-role set on a two-file deposit pays `λ/1` for the mate it declined to explain, and loses
to a barcoded incumbent whose whitelist hit by ~0.22 (measured over every single-cell leaf,
`tests/test_kb.py`). But the penalty alone is *not* enough when the incumbent's whitelist MISSES — so
`escalate` will not let a barcodeless top anchor the tie when its **proper-subset** read set orphaned a
file a valid barcoded candidate seats as its **barcode role**. A deposit holding a read the winning
chemistry cannot seat at all is not that chemistry's deposit, and that is evidence living in the other
file, which no gate on the fallback's own read could see. The barcoded candidate then anchors the band,
so it is inside it whatever the fallback scored, and the pair becomes a divergent tie: the resolver
**asks** instead of deciding.

That last sentence is also why the CI under-declaration guard reads the same predicate
(`confuse.seats_a_file_the_fallback_dropped`, which is where it lives so both can). The guard's whole
danger is *"the resolver would pick one and never ask"*, and on those pairs that is now false; a guard
that demanded a `confusable_with` edge anyway would be requiring a declaration for a danger the
resolver averts. Scoping the rule to a proper subset is what keeps its blast radius equal to the
feature that introduced it — every pair that predates read sets scores, ranks and escalates
byte-identically.

**The filename prior is sub-threshold by construction.** `β · prior(r, f)` is 1 when the file's
`_1`/`_2` token matches the role's conventional slot and 0 otherwise, with `β` far below the smallest
evaluator weight — so it can break an **exact byte-tie** and nothing else. It can never override bytes
or flip validity, because `fasterq-dump`'s `_1`/`_2`/`_3` say nothing about which read is which.

**Algorithm.** For the common single-cell case of at most four files, brute-force all 24 injective
maps and filter by validity, which natively enforces the non-decomposable globals. For a large file
count, run Hungarian on the negated matrix with forbidden cells as large-cost edges, **then post-check
that no selected edge is one of those** — an all-forbidden row means the role is unfillable and the
score is `−∞`, not a padded assignment — then re-check the globals, escalating to Murty k-best if a
non-decomposable one fails.

Building the matrix across all techs is dominated by the onlist tests, at roughly 100 ms each. That
cost is precisely why the hypothesis exists.

## How the hypothesis enters, and why it is not evidence

A byte-blind signature cannot implement "test one list first", because *which* list to load is a
metadata fact and is not recoverable from the bytes being identified. So `score` takes an optional
span-verified hypothesis, and it has exactly **two control-flow effects and zero evidential ones**:

1. **selector** — it picks which onlist and signature to evaluate first, enabling an early stop;
2. **tie-break prior** — a sub-threshold nudge on evaluation order.

It never enters the matrix, never un-gates a forbidden cell, and never wins a `Conflict`.

**Where one comes from: `chemistry_hypothesis`, and it is agreement-or-nothing.** A dataset's
verified assertions reduce to at most one hypothesis, and only when every `library.chemistry` claim
among them says the same thing — two experiments describing two protocols is a real dataset, and one
dataset-level hypothesis would steer both, half of them wrongly. `None` is the ordinary outcome and
costs only a hint. It lives in `engine.py` beside `Hypothesis` because it has **two** callers:
`manifest fill` and the eval harness that measures `manifest fill`. Those two used to reduce the
same list differently (the harness took the last document to claim one), so the benchmark could
steer a scorer the compiler would have left unsteered — #188's "the harness fails differently from
production". An operator's `--assert-chemistry` is the other source and outranks this one.

**A record may withhold a hint, and that is its whole authority.** `chemistry_hypothesis` also reads
the deposit's own `library_source`: a record declaring a single-cell library standing over a **bulk**
hint makes that hint non-credible, so the hint is dropped. It may never name a chemistry, never move
a score, and never raise anything — the worst it can do is decline to offer one, and the bytes decide
either way. Deliberately **not** a `Conflict`: most single-cell deposits carry a bare
`TRANSCRIPTOMIC`, so absence carries no information whatever, and reading the pair as two comparable
claims would false-block correct datasets. An operator's `--assert-chemistry` is out of reach by
construction — that branch builds its `Hypothesis` inline and never calls this function.

**What the string NAMES is a separate question from whether it disagrees**, and `kb.match`
answers it: `resolve_chemistry(value) -> Spec | None` matches a node when one of its curated forms is
*carried by* the value — `alias ⊆ needle`, never the reverse. So `RNA-Seq` (SRA's `library_strategy`
on every transcriptomic run) names nothing, while "Chromium Next GEM Single-Cell 5' Reagent Kit v2"
reaches the leaf. It returns a **node**, because a family answer and a leaf answer are different
claims. `harvest verify` rejects a chemistry draft that resolves to `None`; the two operator doors
(`manifest fill --chemistry`, `resolve score --assert-chemistry`) do not pass through verify, so the
matcher — not the rejection — is what closes them.
[ADR-0020](../adr/0020-a-family-term-narrows-it-does-not-conflict.md).

**The determinism argument.** For a fixed observation, the validity and finite score of any candidate
that gets computed is a pure function of the bytes. The hypothesis changes only *which* candidates are
computed, at *what* cost, and to *which* rung. So the same observation with the same hypothesis gives
identical output, and the same observation with a *different* hypothesis gives an **identical winner
whenever the bytes are decisive** — a wrong hypothesis simply fails its own gates, blocks the early
stop, and forces the ladder down. Only where the bytes are genuinely non-decisive, which means a
processing-divergent pair, may it break the tie — and then only through the escalation function at
rung 0, recorded with an asserted basis and **surfaced**, never merged into the observed value.

## The escalation ladder

Inputs: the ranked candidates with their scores, assignments and reached rungs; the observation; the
verified assertions; and the CI confusability metadata. The margin is the top score minus the second,
against a small tie threshold.

```text
if no candidate passes its requires:
    a required PHYSICAL read is unfillable by any file -> Blocker(MISSING_TECHNICAL_READ, remedy)
    gzip or integrity failed                           -> Blocker(TRUNCATED_GZIP | CORRUPT_FASTQ)
    otherwise                                          -> Blocker(UNSUPPORTED_TECHNOLOGY)

divergent_ties = confusable ties with the top that are NOT processing-equivalent

margin > θ and no divergent ties                       -> Decision(top)
no divergent ties, tie set is a DECLARED equivalent group
                                                       -> Decision(record ALL); 0 questions, 0 conflicts
otherwise (a processing-DIVERGENT tie), walk decidable_by in ladder order:
    onlist    (rung 3)  already tried during scoring
    metadata  (rung 0)  a span-verified assertion that disambiguates AND is byte-consistent
                        -> Decision(asserted), SURFACED. Matches a tie member by NAME, else by
                        FAMILY when the asserted family picks out exactly one of them — the same
                        authority split conflict detection already runs on (a paper names the assay
                        family reliably and the leaf vaguely, so the bytes pick the leaf). Two tie
                        members under the asserted family is ambiguous and still asks.
    alignment (rung 6)  mini-align to a tiny reference (strand, 3'-versus-5') -> Decision if it resolves
    user      (rung 7)  -> Question, and the batch defers to a human via exit 4
```

`UNSUPPORTED_TECHNOLOGY` is the point of the whole first branch: an unrecognized library is
*unsupported*, not guessed at.

**Rungs 4–6 are unbuilt**, so in practice a tie that survives rung 3 and has no usable assertion goes
straight to rung 7 and asks a human. Rung 4 is likely redundant with rungs 2–3.

**Conflict detection runs unconditionally, in parallel with all of this.** If an observed value
contradicts an asserted one — metadata says 26 bp, the bytes say 28 bp — a `Conflict` is surfaced. The
library section takes the observed value, because there its authority is the evidence; the Conflict
stays attached; and compiling refuses until a human confirms. A `Conflict` does **not** escalate: it
is surfaced alongside, not instead of, the decision.

**A difference is not automatically a contradiction**, and there are exactly three shapes
([ADR-0020](../adr/0020-a-family-term-narrows-it-does-not-conflict.md)):

| asserted vs observed | verdict |
| --- | --- |
| the asserted node is an ANCESTOR of the observed one (`narrows_to`) | agreement — no `Conflict` at all |
| same family, not an ancestor (asserted v2, observed v3) | `resolved` `Conflict`: the bytes decide the leaf, exit 0 |
| cross-family (asserted bulk, observed barcoded, or the reverse) | `open` `Conflict`, exit 4 |

**The exit-code contract** is uniform across every verb: an open `Conflict` or a non-empty
`questions.md` is exit **4**, which a human answer can clear; a hard `Blocker` is exit **3**, which no
human answer can. See [`cli.md`](cli.md).

## From N runs to one dataset verdict: `reduce_dataset`

Everything above answers "what is this ONE library?" — that is `resolve_dataset`, and a dataset is
not one library. `resolve_runs` splits the files into runs by name (`group.py`, a rung-1 prior about
*identity*, never about role) and resolves each on its own bytes; **`reduce_dataset` is what turns
those N answers back into the one verdict a caller acts on**, and the two callers that are handed a
whole dataset — `manifest fill` and the eval harness — both call it. (`resolve score` and `e2e.py`
still call `resolve_dataset` directly, and correctly: each is handed one library's files.)

Four gates, in this order, each a refusal:

| gate | means | exit |
|---|---|---|
| `run` | a run did not resolve on its own bytes, or asked. `MultiRunOutput.exit_code()` is the max over the runs, so one run's blocker or one run's open question is the dataset's | 3 or 4 |
| `metadata` | the record join refused — a record whose runs are not the files on disk | 3 |
| `sample` | one sample's files span two chemistries. This is the relocated "all runs must agree" invariant, now per-SAMPLE: across *different* samples a difference is a legal partition into assays, within one sample it is a mis-grouping | 3 |
| `assay` | nothing named a chemistry. The defensive floor; the `run` gate catches this in practice | 3 |

Through all four, `assays` is the partition — one group per chemistry — and **more than one group is
a verdict, not an error**: a large project holds several assays, and `manifest fill` writes one
manifest each. `DatasetResolution.result` is the one `ResolveResult` a consumer that wants one
answer reads: the first assay's first run (the run `manifest fill` builds that assay's manifest
from), carrying the **union** of every run's conflicts, questions and blockers, deduplicated. Run 0's
result alone would show a dataset exiting 4 with nothing open on it.

**It lives beside the type it reduces, for `chemistry_hypothesis`'s reason.** `manifest fill` made
this reduction inline and the eval harness that measures `manifest fill` skipped it entirely, calling
`resolve_dataset` on a whole dataset's file list — so on 11 of the 18 benchmark cases the benchmark
graded a code path the product had abandoned (#196). It was green only because those corpora are
homogeneous, which is a property of the corpus and not of the compiler. One reduction, both callers.

## Worked example: the benign twin

A synthetic two-file sample, one 28 bp read and one ~90 bp read. The `_1`/`_2` tokens carry no role
information and the header test abstains on both.

| | f1 (`_1`) | f2 (`_2`) |
|---|---|---|
| length (mode, distinct) | 28, 1 | 90, many |
| distinct ratio, bases 0–16 | 0.08 → looks like a CB | ~1.0 |
| distinct ratio, bases 16–28 | 0.98 → looks like a UMI | — |
| composition | uniform ACGT, no linker | gene-biased |

Byte-derived roles: f1 is CB16 + UMI12, f2 is cDNA. The candidate family sharing that 28 bp / 16+12
geometry is {v3, v3.1, GEM-X v4, Multiome}. A hypothesis of "10x 3′ v3" selects the v3 whitelist
first: forward hit 0.82, reverse-complement at the floor.

The matrix leaves exactly one valid injective assignment — the cross-assignments f1-as-cDNA and
f2-as-CB are both forbidden on segment length — giving a raw score of 2.00 over two roles, so 1.00.
**v3.1's matrix is identical, also 1.00.** GEM-X v4 and Multiome each require their own onlist, which
hits at the floor on these reads, so both are forbidden and excluded — and that exclusion is
**certified by the CI cross-hit computation, not by memory**: were one of those whitelists a superset,
the pair would become a divergent tie routed to `decidable_by` rather than being silently gated.

The surviving tie {v3, v3.1} is a CI-proven equivalent group, so the decision **records both** into
the chemistry equivalence class with an observed basis, at rung 3, asking **zero questions** and
raising **zero conflicts**.

The adversarial variants off the same fixture land where the ladder predicts: no technical read gives
`Blocker(MISSING_TECHNICAL_READ)`; swapped `_1`/`_2` gives a byte-identical manifest, because the
filename prior is sub-threshold; and lying "asserted v2" metadata gives a surfaced `Conflict` while
the library keeps the observed answer.
