# The bacterial fraction of a worm plate: 5.9% pooled, a 42x rise with age, and a trajectory that is not monotonic

Measured 2026-08-21 for [#425](https://github.com/liuhlab/seqforge/issues/425). The first
whole-plate run of the in-house SMART-seq3 aging series against the `ce11_ecHT115` chimera: **784
worms, 9 ages x 3 strains, one animal per library**, compiled as `ss3-784-chimera-c54b1418fa98` under
`WORKFLOW_VERSION 2026.8.18` and run across six nodes on `ircbc`.

*C. elegans* is fed on *E. coli*. Every figure this plate has ever produced was counted against plain
`ce11`, so the bacterial share of each library was never a number — it was whatever fell into
`unmapped` and `too many loci` with nothing saying which. This is that number.

**A measurement is not a decision.** What is settled here is what the libraries contain. What it
implies for the aging series is not, and §5 says so explicitly.

## The one-sentence answer

**The plate is 5.886% bacterial by pooled kept records and 0.753% by the typical worm, the share rises
~42x from day1 to day17, and the rise is a step function rather than a smooth trend — day3 falls
below day1, day7 collapses back to baseline, and the peak is day15, not day17.**

## 1. Method

`seqforge io split-chimera` routes every primary mapped record to a Component by the `__` separator in
its reference name, per cell. Counts are read from each cell's `<sample>.qc.json.gz`, aggregated by
`script/collect_784.py`; per-cell rows and group aggregates are at
`aging_SS3/script/metrics784/final/{per_cell.tsv,summary.json}`.

**Three statistics have all been called "the bacterial share" and they differ by ~8x.** They are
reported separately throughout:

| statistic | plate value |
|---|---|
| pooled depth-weighted share of kept records | **5.886%** |
| per-cell median of the same | **0.753%** |
| per-cell median of `unique / kept_total` | **0.581%** |

The pooled/median gap is **not** depth weighting — the unweighted per-cell mean is 5.488%, essentially
the pooled figure, and share correlates *negatively* with depth (rho = -0.225). It is right skew:
14.5% of cells exceed 10% bacterial and 1.8% exceed 50%, and those carry the pooled number while the
typical worm sits under 1%.

Coverage is complete: 784 of 784 bundles, one `seqforge` stamp, one assembly, `kept + dropped ==
records_in` for every cell.

## 2. The share is not a lower bound, and the honest bracket is wide

`#425` framed this as a lower bound on the reasoning that an organism-ambiguous read is dropped as a
multimapper. **That is not what the splitter does.** A fragment placed at more than one locus is
routed by its representative record's Component and **kept**, marked rather than dropped; `dropped`
holds only `unmapped`, `secondary` and `supplementary`. So the point value is contaminated in *both*
directions, not floored.

Plate-wide bracket: **lower 4.928% <= point 5.886% <= upper 37.521%.**

The ceiling is near-vacuous by construction — **32.593% of kept records are multiply placed**, and the
upper bound charges every one to the bacterium when on a worm library they are overwhelmingly
within-worm repeats. **Quote 4.93%-5.89% as the working range.** The 37.5% figure means only "a third
of the library could not be resolved to one locus at all".

Lower and point stay tightly coupled at every age (day1 0.185/0.212, day15 17.47/21.94), so no
conclusion below depends on which end is read.

## 3. The aging series

Bacterial share as % of kept records, by age:

| age | n | pooled | median | mean | min | max |
|---|---|---|---|---|---|---|
| day1 | 95 | 0.212 | 0.191 | 0.223 | 0.068 | 0.806 |
| day3 | 80 | 0.174 | **0.115** | 0.198 | 0.027 | 3.271 |
| day5 | 95 | 7.962 | 0.628 | 4.465 | 0.108 | 92.657 |
| day7 | 94 | 0.702 | **0.205** | 0.674 | 0.048 | 13.397 |
| day9 | 95 | 4.295 | 1.521 | 4.245 | 0.248 | 47.477 |
| day11 | 88 | 6.293 | 1.819 | 4.571 | 0.204 | 89.225 |
| day13 | 95 | 6.488 | 1.219 | 4.115 | 0.269 | 52.466 |
| **day15** | 95 | **21.939** | **17.645** | 20.264 | 0.134 | 71.017 |
| day17 | 47 | 14.066 | 8.011 | 13.961 | 0.638 | 97.731 |

**The rise is real and it is a whole-distribution shift.** Spearman rho(share, age in days) =
**+0.699** over all 784 cells, holding inside every strain (CF +0.646, DA +0.658, N2 +0.803). The
fraction of cells above 1% runs 0.0% (day1) -> 42% (day5) -> 58% (day9) -> 81% (day15) -> 98%
(day17), and day1's p90 (0.36%) lies below day15's p10 (0.43%).

Fold change on the per-cell median: **day1 -> day17 = 42x**, **day1 -> day15 = 92x**.

**The monotonic reading fails, in three places, and none is an outlier artifact:**

- **day3 < day1** (0.115% vs 0.191% median). The series minimum is not the youngest age.
- **day7 collapses to baseline** (median 0.205%, pooled 0.702%) after day5's 7.962% pooled. day7's
  p10-p90 of 0.086-1.18% is indistinguishable from day1.
- **day17 < day15**, roughly halved on the median.

The defensible description is **near-zero through day3, an unstable middle from day5 to day13
swinging between 0.17% and 8% pooled, and a high plateau at day15-day17.** A smooth monotonic curve is
not what this plate shows.

## 4. Strain, and why the ranking depends on the statistic

| strain | n | pooled | median | max |
|---|---|---|---|---|
| CF | 259 | 5.325 | **2.107** | 68.130 |
| DA | 262 | 3.854 | 0.615 | 97.731 |
| N2 | 263 | **8.398** | **0.443** | 71.017 |

**N2 has the highest pooled share and the lowest per-cell median.** It is bimodal, driven by day15_N2
(median 35.5%, 11 of 31 cells above 45%). By the typical worm, N2 is the *least* bacterial of the
three. Any sentence that mixes pooled and median will invert this conclusion, so the statistic must
travel with the claim.

day15 also carries a large strain interaction: CF 21.0% and N2 35.5% medians against **DA 0.97%**.

## 5. Two groups that look like handling, not biology

Recorded as observations, not conclusions. Both want checking before any figure is drawn.

- **day5 is a CF-only spike.** Its 7.962% pooled comes almost entirely from CF (median 4.625%); day5
  DA and N2 medians are 0.413% and 0.218%, each inflated by a single extreme cell (`day5_DA_29` at
  92.7%, `day5_N2_26` at 69.5%). A one-strain excursion at one age, with the other two strains at
  baseline, reads as a batch or handling event on the day5 CF wells.
- **day15 DA sits an order of magnitude below its own day9**, while CF and N2 at day15 are at their
  series maxima.

**day17 rests on half the sample.** n=47 (12 N2 / 16 DA / 19 CF) against ~95 elsewhere. That is the
design rather than attrition, but the day15 -> day17 decline is neither well powered nor
strain-balanced.

## 6. Sanity checks, and one that does not work here

- **`input_read_length` median 232 bp** (mean 228.4, p05 200, p95 244) against ~300 bp sequenced. The
  Tn5 read-through clip is engaging, as [the Tn5 measurement](smartseq3-tn5-read-through.md)
  predicted. The 16-worm pilot's median was 224.5 bp — same regime.
- **`unmapped_too_short` median 8.235%** (mean 10.672, p95 23.61) against this plate's own unclipped
  median of 38.25%.
- **That second check is weak here and should not be leaned on.** `unmapped_too_short` correlates with
  the headline at **rho = +0.723** — bacterial-heavy libraries are also the ones with the most
  unplaceable material, so the two are not independent evidence. Weaker co-variation elsewhere:
  `uniquely_mapped_pct` -0.365, `input_read_length` -0.253, `fragments` -0.225.

## 7. Two validations

**The three cells re-run under the [#451](https://github.com/liuhlab/seqforge/issues/451) fix are
unremarkable.** Robust (MAD) z-scores within each cell's own age x strain group across nine metrics:
the largest |z| anywhere among the three is **1.79**, percentile ranks 6-87, accounting balancing
exactly. The parity-check refusal left no fingerprint on the data.

**The 16-cell pilot reproduces bit-for-bit.** All 16 `pilot16r2` cells are in this plate and their
shares agree to every printed digit — ratio 1.0000 on all 16, identical fragment counts.

That also resolves the apparent discrepancy with the pilot's 14.5x age effect. In the pilot's exact
statistic (N2 only, `unique/kept_total`, per-cell median) this plate gives day1 0.132% -> day17 4.082%
= **31x**. The pilot's 8 day17_N2 worms are a subset of the plate's 12, and the 4 it did not sample
(6.6, 9.7, 14.2, 7.1%) are the high ones. **The pilot underestimated by sampling, and its 1.445%
pooled share was a two-age, one-strain, 16-worm design that this plate shows was not representative —
the true plate figure is 4x higher.**

## What this does not settle

The bacterial share is a property of the *library*, not of the animal. Whether a rising share reflects
more bacteria in the worm, fewer worm transcripts per animal, or a change in how the library was
built is not answerable from these counts. The negative correlation with depth (rho = -0.225) is
consistent with more than one of those and discriminates none.

Anything these numbers *decide* belongs in a record, not here.
