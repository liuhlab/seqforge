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

## Not a residual: an unobtainable whitelist is not a state a user reaches

**A first draft of this section claimed one, and it was wrong twice over.** It is kept, corrected,
because the error is instructive: the numbers were real and measured what nobody asked.

**There is no "user with no whitelists".** All fifteen lists ship pre-packed in
`src/seqforge/io/onlists/` — 3.0 MB total, 522 kB for the 6 794 880-entry `3M-february-2018`, because
a sorted 2-bit-packed barcode set compresses to a twentieth of the vendor's own `.txt.gz`. Every
onlist reference in all seventeen shipped specs resolves from `DEFAULT_REGISTRY` with **zero gaps**;
that registry is `offline=True` by default and needs no network; and every production CLI path
(`manifest fill`, `compose`, `run`, `io`) uses it or `default_registry(...)`. `offline` governs only
the fallback for a list we do **not** ship.

**And the numbers were taken on the wrong fixture.** They came from `kb.generate_reads`, which draws
*random* barcodes, so an onlist test there legitimately FAILs rather than going unconfirmed. On
`splitseq`'s generated reads the chemistry scores 0.5657 with every list shipped and 0.5500 with its
own three withheld — a 0.0157 difference. The "+0.45 margin against bulk" was the synthetic fixture,
not whitelist availability, and reporting it as the latter is the mistake this paragraph exists to
stop being repeated. §1's margins are unaffected: `confuse` scores every pair on that same fixture by
design, with both sides treated alike, which is what the confusability contract has always measured.

Built from the barcodes we actually ship, the picture is the opposite:

| `splitseq`'s own realistic reads | splitseq | bulk | outcome |
|---|---:|---:|---|
| all lists shipped | 0.7800 | 0.7800 | **exact tie** -> declared edge -> onlist decides -> `splitseq` |
| `splitseq-round{1,2,3}` absent | **0.3300** | 0.7800 | bulk by +0.4500, outside θ — decides silently |

## What IS real, and it is one sentence in a guard

The bottom row can only be reached through `UNSHIPPED_ONLIST_DEBT`
(`tests/test_kb.py`) — the pin that permits a spec to ship while its decisive whitelist does not. It
is **empty**, and `test_a_spec_that_calls_onlists_decisive_can_actually_reach_one` keeps it empty, so
this cannot arrive by accident.

What can arrive is somebody adding an entry, and the guard's own comment tells them it is safe:

> That failure was safe — *it over-asks, it does not answer wrongly* — which is exactly why it
> survived unnoticed

**Measured, it does not over-ask.** With its whitelist withheld `splitseq` falls to 0.3300 against
bulk's 0.7800; at +0.45 it is far outside θ, so it never joins the tie set its own declared
`confusable_with` edge would be consulted for, and the deposit compiles to a bulk gene-count matrix
at exit 0. Over-asking is the safe failure that sentence promises; answering wrongly is the one the
KB most fears, and it is the one on offer. The debt hatch is still the right escape — a KB entry
should be able to land before its whitelist is derived — but the note beside it has to say that
recording a debt makes that chemistry *lose silently to bulk*, not that it merely goes unconfirmed.

Tracked as #321, narrowed to that.
