# The support normalizer: what an unanswered test costs, and who it costs it to

Measured 2026-08-05 for [#307](https://github.com/liuhlab/seqforge/issues/307).

**Answer: an unanswered support cannot be treated one way.** Dropping it from the normalizer is right
when the *bytes* were silent and wrong when *we* lacked the whitelist — the two directions fail
oppositely, and each failure is a silent wrong answer at exit 0. A third case, the onlist withheld
from every spec at once, belongs to neither and leaves the signature instead.

## What was measured

`resolve/scoring.py::_score_cell` normalized by every **declared** support weight, so a test that
could not be evaluated contributed `weight · 0.0` to the numerator while keeping its whole weight in
the denominator. Three questions, each scored against the shipped KB with
`kb.generate_reads(spec, n=400, seed=0)` unless stated.

### 1. The handicap is proportional to declared whitelist evidence

Support weight is not declared evenly. Per barcode role, share of weight carried by `onlist_hit_rate`:

| chemistry | onlist weight | total | share |
|---|---:|---:|---:|
| `10x-*` (the whole cohort) | 5.0 | 8.0 | 62.5 % |
| `splitseq`, `bd-rhapsody-wta`, both Enhanced beads | 9.0 | 10.0 | 90.0 % |
| `bulk-rnaseq` | 0.0 | 2.0 | **0 %** |

So withholding the whitelist marked every barcoded chemistry down against the one candidate that must
never win by default, by an amount set by how much whitelist evidence it had the honesty to declare.
Each spec on its **own** reads, onlist withheld, before and after taking it out of the signature:

| spec | before | after |
|---|---:|---:|
| `splitseq`, `bd-rhapsody-wta`, `bd-rhapsody-wta-enhanced-v1`, `-v2` | 0.5500 | 1.0000 |
| `10x-3p-gex-v3`, `-v3.1`, `10x-5p-gex-v3`, `10x-gemx-3p-v4`, `10x-multiome-gex` | 0.6594 | 0.9083 |
| `10x-3p-gex-v2`, `10x-5p-gex-v2` | 0.6575 | 0.9033 |
| `10x-multiome-atac` | 0.7424 | 0.9067 |
| `bulk-rnaseq`, `smartseq3` | 1.0100 | 1.0100 |

`bulk-rnaseq`'s six declared edges, `rung02_margin(bulk, b, probes[b])`, θ = 0.02:

| edge | mechanism | bulk | incumbent | margin | was |
|---|---|---:|---:|---:|---:|
| `splitseq` | `[onlist]` | 1.0000 | 1.0000 | +0.0000 | +0.4500 |
| `bd-rhapsody-wta` | `[onlist]` | 0.9875 | 1.0000 | -0.0125 | +0.4375 |
| `bd-rhapsody-wta-enhanced-v1` | `[onlist]` | 0.9975 | 1.0000 | -0.0025 | +0.4475 |
| `bd-rhapsody-wta-enhanced-v2` | `[onlist]` | 0.9975 | 1.0000 | -0.0025 | +0.4475 |
| `10x-multiome-atac` | `[onlist]` | 0.8800 | 0.9067 | -0.0267 | +0.1376 |
| `smartseq3` | `[metadata]` | 1.0100 | 1.0100 | +0.0000 | +0.0000 |

The under-declaration sweep stays green and no edge is added or deleted.
`10x-multiome-atac` is the one pair the sweep would no longer *demand*: bulk is now decisively below
it, so `could_outrank_at_rungs_0_2` is False. The orphan exemption is **not** what excuses it — bulk
still orphans that 24 bp barcode read from its maximal set — which `test_the_orphan_exemption_is_not_a_
blanket_one` pins separately so the two causes cannot be confused.

### 2. Renormalizing an unobtainable whitelist inverts the ranking

The obvious generalisation — drop *any* unanswered support from the normalizer, at runtime too — was
measured and rejected. Fixture: an over-length (150 bp) 10x 3′ v3 library, barcodes drawn from the 3M
pool, with only the `3M-february-2018` and `737K-arc-v1` lists installed. Full candidate list:

| candidate | score | whitelist |
|---|---:|---|
| `10x-5p-gex-v3`, `10x-gemx-3p-v4`, `10x-multiome-gex`, `10x-3p-gex-v2`, `10x-5p-gex-v2` | 0.7819 | **not installed** |
| `bulk-rnaseq` | 0.7500 | declares none |
| `10x-3p-gex-v3`, `-v3.1` | **0.6083** | installed, **hit** |

The true chemistry ranks **last**, below the generic fallback, behind five siblings nobody could
check. The 10x siblings declare byte-identical geometry and are separated by the whitelist and
nothing else, so renormalizing it away does not merely reweight the comparison — it removes it, and
the specs that saturate on the surviving geometry supports all tie at the top. A spec must not be
credited for evidence nobody was able to check.

### 3. Withholding from everyone is a third case

`accepts_at_rungs_0_2` has always documented itself as leaving "the verdict … on geometry,
segmentation, distinct-value ratios and header grammar alone". It did not: an empty registry emptied
the numerator and left the weight, so the verdict rested on geometry *diluted by a zero*. Removing the
tests from the signature (`confuse.without_rung3_evidence`) is what makes the existing sentence true.
Both sides lose the same tests, so nobody is advantaged — which is exactly what is not true of §2.

## What did not move

The frozen-18 grade digest is
`aeff9af9ce5f626838d26c9c4f9860f51fd297dc25fe94c63495df0fa146807b`, 18/18 `correct`, **before and
after**, both re-taken on this tree (`main` @ `62a4f54`, `--no-llm`, `--cases evals/benchmark`). No
benchmark case exercises a byte-silent support, so the runtime arithmetic change is unobserved on the
corpus. `RESOLVE_VERSION` moves anyway (2026.8.7 → 2026.8.8), because what changed is the *definition*
of a cell.

## Known residual: no whitelist installed, and bulk still wins

**This is not fixed here, and it is the case #307 opened with.** With an empty registry and full
signatures — a user offline, or one whose whitelists never materialized — the fallback still takes
some single-cell libraries outright. §1 removes the handicap **only inside `confuse`**, the
KB-authoring guard, never on the resolve path.

Which pairs are actually exposed is narrower than the scores suggest, because the orphan rule
(`seats_a_file_the_fallback_dropped`) already re-anchors the tie band whenever the fallback wins on a
**proper-subset** read set that drops the incumbent's barcode read:

| on the chemistry's own reads | bulk | bulk's set | incumbent | margin | orphan rule protects |
|---|---:|---|---:|---:|---|
| `splitseq` | 1.0000 | `full` | 0.5500 | +0.4500 | **no** |
| `bd-rhapsody-wta` | 0.9875 | `full` | 0.5500 | +0.4375 | **no** |
| `bd-rhapsody-wta-enhanced-v1` / `-v2` | 0.9975 | `full` | 0.5500 | +0.4475 | **no** |
| `10x-multiome-atac` | 0.8800 | `full` | 0.7424 | +0.1376 | **no** |
| `10x-3p-gex-v3` (the whole 28 bp cohort) | 0.7500 | `se` | 0.6594 | +0.0906 | yes — asks |

The 10x cohort is safe: bulk's 40 bp floor cannot seat a 28 bp barcode read, so it wins on `se`,
orphans that read, and the resolver raises a divergent tie rather than deciding. The five exposed
chemistries put their barcode read at 60–94 bp, which bulk's maximal set admits, so it explains every
file and orphans nothing. **Their declared `confusable_with` edges do not rescue them either** — at
+0.14 to +0.45 the incumbent is far outside θ and so never joins the tie set the edge would be
consulted for. The deposit compiles to a bulk gene-count matrix at exit 0.

It is left because §2 is the reason the obvious fix cannot be applied there: the same renormalization
that would rescue these three inverts the 10x cohort. Closing it needs a different instrument — the
comparison has to become rung-aware, or a barcoded candidate with no obtainable whitelist has to
refuse rather than lose, which is the shape `TechEvaluation.barcode_onlist_available` and escalate's
F1b already reach for. That is a design change with its own blast radius, and it is tracked separately
rather than smuggled in here.
