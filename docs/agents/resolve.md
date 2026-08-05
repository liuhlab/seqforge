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
`supports` sum as `Σ weight · score` normalized by the weight actually consulted, so a finite cell
lands in `[0,1]`.

**A support the BYTES could not answer leaves the normalizer; one WE could not ask keeps its weight;
and one withheld from EVERY spec leaves the signature** (#307). Three situations, and treating them
alike fails in three different directions — all measured, with the numbers and the surviving residual
in [`docs/research/support-normalizer-asymmetry.md`](../research/support-normalizer-asymmetry.md)
(2026-08-05).

`Evaluation.answerable` carries the first distinction and is deliberately *not* derived from
`outcome`: `distinct_ratio` abstains on every input by design (it must never gate) while measuring on
every input, so dropping supports on the gate outcome would empty the normalizer for most of the KB.
A column no read reaches, or a header the archive stripped, leaves numerator and normalizer alike — no
chemistry could have got an answer there, so dropping it advantages nobody, and keeping it marked a
spec down for a question nobody could answer (the rule of #177, #255 and #277). A **whitelist that
failed to materialize** is the opposite case and keeps its weight, because a rival spec whose list did
materialize answered the same question and is paying for every imperfection in its hit rate.
Renormalizing it away makes the unverifiable spec the cheaper one to satisfy, and it is the 10x cohort
— siblings that declare byte-identical geometry and are separated by the whitelist and nothing else —
where that removes the comparison outright. Withholding it from everyone is neither: the question is
asked of nobody, so `confuse.without_rung3_evidence` takes it out of the signature, which is what
finally makes `accepts_at_rungs_0_2`'s own "the verdict rests on geometry ... alone" true.

**"But when is a whitelist ever unobtainable?" — almost never, and that is worth knowing before
reading the paragraph above as urgent.** All fifteen lists ship pre-packed (3.0 MB; a sorted 2-bit
barcode set is a twentieth of the vendor's `.txt.gz`), every onlist reference in all seventeen specs
resolves from `DEFAULT_REGISTRY` with no network and no setup, and every production verb uses it.
There used to be one door left — a recorded-debt pin letting a spec land before its whitelist was
derived — and it is **gone** (#321):
`test_a_spec_that_calls_onlists_decisive_can_actually_reach_one` now demands the gap set be empty
outright, so a chemistry that calls the onlist decisive cannot ship until its list does. The pin's own
comment had promised such a spec merely over-asks; measured on real barcodes, `splitseq` with its
lists withheld falls to 0.3300 against bulk's 0.7800, far outside θ, so nothing asks and the deposit
compiles as bulk. That is not a deferral to record, so there is no longer a way to record it.

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

**The anchored tag gate does a whole chemistry's separation in one existing evaluator, and the
anchor is the whole of it.** `smartseq3`'s `requires` is a single test — `motif_present` on R1 for
the 11 bp TSO tag `ATTGCGCAATG`, `where: read_start`, `max_mismatch: 2`, `min_rate: 0.02` — and
nothing else stands between a plate and the generic paired-end fallback. Measured at offset 0 over
2 000-read slices, ten real `GSE207085` cells score **39.6–67.6 %** on R1 against **0.00–0.15 %** on
their own R2 and 0.00 % / 0.05 % on a bulk control, so the floor sits 13× above the highest observed
negative and 3.45× below the lowest published positive (6.9 %); the schema's own `0.50` default
fails two of those ten. Take `where: read_start` away and the gate stops working, because the same
tag appears *somewhere* in 8–20 % of the chemistry's own untagged mate and the Tn5 mosaic end turns
up in 6.5–79.5 % of its R1 as read-through — a positional claim about a fixed-offset chemistry is
evidence, the same motif unanchored is background, and the mosaic end is abundant but non-positional
(≤ 0.75 % at offset 0), which is why an anchored test can leave `excludes` empty. And it is a
**proportion over reads at a fixed offset, never a per-cycle purity or a majority gate**: the tagged
fraction is a tunable protocol parameter (6.9–70.5 % across published libraries), so what the gate
asserts is that a structured minority exists, and a majority bar would refuse the assay's own
reference data.

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

**The orphan rule has a degenerate twin, and it needs its own condition.** When the deposit is ONE
cDNA file the fallback orphans nothing — it explains the single read it was handed, and what is
missing was never deposited at all, so no other file carries the evidence
`seats_a_file_the_fallback_dropped` looks for. The only witness left is the **assertion**: a
barcodeless top on a proper-subset read set, while the asserted chemistry's barcode role is unfillable
and its cDNA role fillable, is `Blocker(MISSING_TECHNICAL_READ)` and not a question. That is the same
condition `_no_candidate_blocker` has always refused on, reached by a second entrance — before read
sets, one file made every spec invalid and the dataset fell into that branch; `bulk-rnaseq`'s `se` set
made one candidate valid and the refusal degraded into a question offering `bulk-rnaseq`
(#309, GSE208154). Two things follow. Descent must keep the asserted chemistry in the scored pool even
when `length_feasible` drops it — its evaluation is the only thing that can answer *why* it could not
be seated, and it used to be scored only by the accident of `pool = [...] or runnable` firing on an
empty pool. And the rule stays scoped to a PROPER subset: a maximal-set bulk winner over an asserted
single-cell chemistry explained every file, so it stays `_single_cell_collapse_conflict`'s open
Conflict at exit 4, where a human is shown a disagreement rather than a dead end.

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

a BARCODELESS top on a PROPER-SUBSET read set, while the ASSERTED chemistry's barcode role
is unfillable and its cDNA role fillable          -> Blocker(MISSING_TECHNICAL_READ, remedy)

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

Five gates, in this order, each a refusal:

| gate | means | exit |
|---|---|---|
| `cell` | one cell of a plate dissented outright. Only reachable under a chemistry declaring `identity.sample_is_cell`, which is `smartseq3` and no other shipped spec | 3 |
| `run` | a run did not resolve on its own bytes, or asked. `MultiRunOutput.exit_code()` is the max over the runs, so one run's blocker or one run's open question is the dataset's | 3 or 4 |
| `metadata` | the record join refused — a record whose runs are not the files on disk | 3 |
| `sample` | one sample's files span two chemistries. This is the relocated "all runs must agree" invariant, now per-SAMPLE: across *different* samples a difference is a legal partition into assays, within one sample it is a mis-grouping | 3 |
| `assay` | nothing named a chemistry. The defensive floor; the `run` gate catches this in practice | 3 |

Through all five, `assays` is the partition — one group per chemistry — and **more than one group is
a verdict, not an error**: a large project holds several assays, and `manifest fill` writes one
manifest each. `DatasetResolution.result` is the one `ResolveResult` a consumer that wants one
answer reads: the first assay's first run (the run `manifest fill` builds that assay's manifest
from), carrying the **union** of every run's conflicts, questions and blockers, deduplicated. Run 0's
result alone would show a dataset exiting 4 with nothing open on it.

## A chemistry that declares one Sample is one cell

Gates `cell` and `sample` read the *same* cross-sample chemistry difference and part on one declared
bit. Across different samples that difference is a legal partition into assays — correct for a real
multi-assay project, catastrophic for a plate, where it splits one experiment in two at exit 0 with
nothing raised. `identity.sample_is_cell` is the only thing that tells the two apart, which is why it
is **declared and never derived**: `umi ∧ ¬barcode` is backwards for SMART-seq2 (neither, still one
cell per file) and for UMI-tagged bulk (a UMI, no barcode, one file per specimen). The property is
about *where demultiplexing happened* — at the bench — which no byte reports. It says `Sample`, not
file and not run, because 20 of 190 well-labelled plate deposits are not strictly 1:1.

Three outcomes per cell, judged against the chemistry the plate itself decided:

| outcome | trigger | effect |
|---|---|---|
| conforms | decides the plate's chemistry | role-assigned, in the manifest — exactly as today |
| contradicts | decides a *different* chemistry outright | `Blocker`, gate `cell`, exit 3 |
| abstains | asked with the plate's chemistry in its tie set, **or** below `Spec.min_input_reads` | inherits it, and is **recorded** |

**A conjunction, not a vote.** A single outright dissent refuses, so no cell is ever outvoted and the
gate creates no new authority over anyone's bytes — every verdict it reads is one a run already
reached alone. Accepted consequence: a deposit genuinely holding a plate *and* a separate bulk
library now refuses, which is the safe direction, and the remedy is to compile them separately.

**An inherited cell is a claim the bytes never confirmed**, so it is not silent: a `Conflict` with
`status: "resolved"` — the existing auditable-but-non-blocking channel, no model change and no hash
movement. It surfaces in the report as *"37 of 1440 cells were admitted without byte confirmation"*.
Not a fifth judgement type: four is a deliberate ceiling
([ADR-0006](../adr/0006-one-judgement-one-envelope.md)).

**`min_input_reads` gates the Sample, summed over its runs** — per-run count is the *minimum* over
its files (R1 and R2 are two views of one fragment), per-cell is the *sum* over its runs. Gating the
run would make a floor of 1000 silently mean 500 on exactly the plates that are not 1:1. It is asked
*before* the dissent, because a starved cell deciding the wrong chemistry outright is the measured
case and would otherwise refuse the plate before its depth was consulted.

Abstaining is where this half stops: the cell is admitted to the manifest, and it is `compose` that
drops it from the pipeline, under whatever KB is loaded then and over the per-file counts in
`provenance` — the same arithmetic, computed independently, because the manifest between the two
stages records the measurement and never the verdict
([ADR-0032](../adr/0032-a-spec-declares-the-shape-of-a-deposit.md)).

**None of this crosses [ADR-0010](../adr/0010-two-resolvers-one-blocks-one-warns.md).** A
sample→files map is the *join* the reduction already owns for gate `sample`; summing read counts over
it consults nothing the metadata resolver decided. Scoring stays per run and untouched — the sum
happens in the reduction, after every run has independently resolved. **Nothing pools**: a pooled
winner's role assignment would map roles to the pool's pseudo-shas, leaving every real file
role-less, so pooling does not remove the per-cell pass, it removes the honest one. And `group.py`
never learns what a cell is — merging two runs of one cell there re-introduces the
global-role-assignment bug that module exists to prevent.

**Any count over archive records is over the deposit, not the download.** Nothing consults such a
count today, and that is the point: `sample_is_cell` is what replaced one. The predicate it replaced
was `strict 1:1 ∧ n_samples > T`, and measured over 1 690 plate and 6 894 droplet/bulk deposits at
deposit scope it has **no admissible `T`** — four hand-verified non-plates (two of them droplet) are
strictly 1:1 with *more* samples than the 1 440-cell plate the threshold was written for, so a `T`
that fires on the plate fires on all four and a `T` that spares them never fires on the plate. The
131× margin that made it look safe was an artifact of an 18-deposit corpus holding no large
strictly-1:1 bulk study, which is 11.3 % of the control pool
([`docs/research/plate-deposit-cardinality.md`](../research/plate-deposit-cardinality.md),
2026-08-04). The **scope** rule outlives the number, and it is recorded here because the pooling
decision is the only place a count would ever be consulted: `resolve_runs` is handed the files that
reached disk, so a count taken there answers "how many samples did I download" — and the corpus's own
96-cell fingerprint package standing in for a 1 440-cell deposit would answer it wrong by a factor of
15. **Deposit** and **Download** are two words in [`CONTEXT.md`](../../CONTEXT.md) for exactly this.

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
