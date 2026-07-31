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
| `has_segment` | mean per-cycle evidence over the window — `constant` wants a max-base fraction at or above .9, `random` wants near-uniform, `polyT`/`polyA` want a base fraction above a minimum. |
| `distinct_ratio` | distinct over total across the window. **Supports-only, never a gate.** Depth-dependent, so only meaningful normalized: a "high" expectation (UMI, cDNA) needs `4^len` far above the sampled read count or the ratio saturates below the band, and a "low" expectation (CB) is conditioned on the estimated reads per cell. It *proposes* CB-versus-UMI; the onlist confirms. |
| `onlist_hit_rate` | the rung-3 hypothesis test. **Width-generic** — the barcode length comes from the registry entry, so SPLiT-seq's 8 bp blocks work without a second code path. Tests forward *and* reverse-complement *and* a small positional offset scan, and records the winning orientation and offset. Floor is `onlist_length / 4^len`; score is the clipped rise from floor to the passing minimum; passes at roughly 0.6. At Q30 the discriminative power is about 500:1. |
| `motif_present` | fraction of reads matching an IUPAC motif within a tolerance, in an **inclusive** window. Used for fixed linkers, and as an `excludes` test to reject 10x when an internal linker turns up in a barcode read. |
| `base_composition` | element-addressable, so it can target a floating region rather than only the first few cycles. |
| `header_index` | abstains when the header is SRA-normalized — and that abstention is how probe *detects* the normalization. Otherwise it checks for a per-file, roughly constant 8–10 bp index. |

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

**The exit-code contract** is uniform across every verb: an open `Conflict` or a non-empty
`questions.md` is exit **4**, which a human answer can clear; a hard `Blocker` is exit **3**, which no
human answer can. See [`cli.md`](cli.md).

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
