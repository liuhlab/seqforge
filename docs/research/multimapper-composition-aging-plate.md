# What the multimappers are: the chrI rDNA array, 53% of placed molecules, and an 8x spread that rRNA leads but does not explain

Measured 2026-08-21 for [#427](https://github.com/liuhlab/seqforge/issues/427), on the same run as
[the bacterial-fraction measurement](worm-bacterial-fraction-aging-plate.md): the in-house SMART-seq3
aging series against the `ce11_ecHT115` chimera, **784 worms, 9 ages x 3 strains, one animal per
library**, compiled as `ss3-784-chimera-c54b1418fa98` under `WORKFLOW_VERSION 2026.8.18` and run on
`ircbc`.

A 16-worm pilot had shown the multimapper share running **6.1% to 40.5%** across one strain, one
protocol and one sequencing run, with nothing saying what those reads were or why one library carried
six times as many as its neighbour. This is that number, on the whole plate.

**A measurement is not a decision.** What is settled here is what the multiply-placed reads are and
what moves their share; what that implies for grading is not, and the recommendation below argues
rather than asserts.

## The one-sentence answer

**The multiply-placed bucket is chiefly ribosomal RNA: the assembly makes those reads ambiguous, and
library prep sets how many of them there are.** ce11 collapses the 45S rDNA array into a few
near-identical copies at the chrI terminus, so an rRNA read can never map uniquely; how large the
resulting bucket is per library is rRNA carry-over, which varies ~8x across one plate. Neither half
alone is the answer. rRNA is the **largest single identified driver** of the spread and not the
explanation of it — rho^2 = 22% against the headline statistic, with the rest unaccounted for.

## 1. Two statistics share the name, and they are not interchangeable

Both are called "the multimapper share" and they differ by a median of 3 points and a maximum of 50:

- **STAR read-level share** — `% of reads mapped to multiple loci` from each cell's `Log.final.out`.
  Denominator: **all STAR input reads**. **This is the headline statistic**, because it is the axis
  `#427` opened on and the only one defined before the chimera is split.
- **Counter fragment-level share** — `obs/multimapping` / `obs/n_fragments`. Denominator: **ce11
  fragments only**, what survived the chimeric split into the ce11 Component.

| statistic | min | q05 | median | q95 | max |
|---|---|---|---|---|---|
| STAR read-level | 6.060 | 18.067 | **30.880** | 38.667 | 51.470 |
| counter fragment-level | 6.817 | 21.962 | **34.866** | 48.805 | 69.235 |

rho between them is only **0.793** — they do not even rank the plate the same way. The counter share
exceeds the STAR share in **784 of 784 cells** (median gap +3.047 pp, max +49.965 pp), and that is
**by construction rather than a defect**: the denominators differ. Plate-wide the counter saw
**83.335%** of STAR's input; per cell that ratio runs **1.354% to 97.176%**, and **33 cells** are
under 50%.

The worked example makes it obvious. **`day17_DA_1`** reports STAR **7.90%** against counter
**47.88%**, because it is a near-pure bacterial library: **656,460** ce11 records against
**28,273,805** ecHT115 records, out of 23,276,339 STAR input reads. Its ce11 counter legitimately saw
1.354% of the input, so its 47.88% is a share of almost nothing and says nothing about the worm.

**Consequence: never quote the counter-share maximum (69.235%, `day5_N2_26`) bare** — that cell's
counter saw 18.9% of its reads. The STAR maximum is unaffected and is the one to quote.

## 2. What they are: the collapsed 45S array on chrI

Three independent readings, three different denominators, one answer:

| reading | denominator | result |
|---|---|---|
| `obsm/multimapping_hits`, 784 cells | all fragments (3,124,390,004 multiply placed) | **NH=3 is 81.308%**, NH=2 is 15.534% |
| multiplaced CRAM windows | all records | 6.38M of ~6.5M chrI NH=3 records in `day17_N2_12` fall at 15.06-15.07 Mb |
| `umi_multimapping_placement` layer | UMI-deduped, one-gene-body (36,732,376 molecules) | rRNA genes are **49.530%** of placement |

The locus, from the WS298 GTF. chrI is 15,072,434 bp, so this sits at the terminus:

| gene | span on `I__ce11` | biotype |
|---|---|---|
| `rrn-3.56` | 15,060,299-15,061,150 | pseudogene |
| `rrn-1.1` | 15,062,083-15,063,836 | rRNA (18S) |
| `rrn-2.1` | 15,064,301-15,064,453 | rRNA (5.8S) |
| `rrn-3.1` | 15,064,838-15,068,346 | rRNA (26S) |
| `rrn-1.2` | 15,069,280-15,071,033 | rRNA (18S) |

Plate-wide top placed genes, as % of the 36,732,376-molecule placement total, with each gene's
`umi_combined` plate total beside it. **A placed molecule names one locus of the set the fragment
could have come from and never the fragment's origin** (see the caveats), so read the ordering as
STAR's pick among near-identical copies, not as abundance:

| # | gene | biotype | % of placement | `umi_combined` plate total | cells with it in top 5 |
|---|---|---|---|---|---|
| 1 | rrn-1.2 | rRNA | 28.344 | 2 | 784/784 |
| 2 | rrn-1.1 | rRNA | 15.568 | 39,300 | 784/784 |
| 3 | rrn-3.1 | rRNA | 5.574 | 9,320,608 | 534 |
| 4 | F23A7.4 | protein_coding | 3.993 | 2,272,082 | 426 |
| 5 | rrn-3.56 | pseudogene | 3.601 | 395,667 | 430 |
| 6 | pud-1.2 | protein_coding | 2.756 | 2,276 | 301 |
| 7 | pud-2.2 | protein_coding | 2.541 | 39,333 | 237 |
| 8 | vit-4 | protein_coding | 2.106 | 513,192 | 151 |
| 9 | F23A7.8 | protein_coding | 1.802 | 1,836,514 | 81 |
| 10 | Y45F10C.2 | protein_coding | 1.452 | 64,850 | 56 |

**Ranks 1, 2, 3 and 5 are the one chrI 45S repeat unit, and together they are 53.09% of all
placement.** The rest is the well-known tandem and paralogous families — vitellogenins (`vit-3/4/5`),
histones (`his-56/57/58/66`), small heat-shock (`hsp-16.11/48/49`), `nspc`, `pud`, and the adjacent
`F23A7.4`/`F23A7.8` pair, both ~205 bp and 560 bp apart on chrX. Top 30 is **80.571%** of plate
placement.

**Composition is uniform at the top and not below it.** Only **25 distinct genes** ever occupy any
cell's top 5, out of 46,926 in the annotation, and 140 ever occupy a top 20; `rrn-1.1` and `rrn-1.2`
are in the top 5 of all 784 cells. But below rank 3 the ordering moves: the median per-cell profile
correlates with the plate profile at rho **0.707** only. Both halves are true and a claim that quotes
one without the other is wrong.

## 3. Why NH=3 dominates

<!-- PENDING: NH=3 mechanism, to be filled from the tiling probe -->

Established so far, as observations rather than as an explanation:

- **NH is capped at 10** because the module passes no `--outFilterMultimapNmax` and STAR's default is
  10. Reads above the cap are filtered to `too_many_loci` (0.1-0.8% throughout), so **the NH=10 bin
  is a real bin and not a catch-all**, and the mean NH below is exact conditional on that filter.
- **Plate mean NH = 2.9210.** Per cell: mean NH median 2.925 (min 2.794, max 4.722); % at NH==2
  median 14.889; % at NH==3 median 82.357.
- A control probe pushing synthetic 150 bp genomic tiles of these five genes through the plate's own
  STAR index returned **NH=2** for the two 18S copies, **NH=1** for most of 26S, and **NH=1** for
  100/100 control tiles from `III__ce11:6000000-6015000`. **So NH=3 is not explained by annotated rDNA
  copy number**, and why it dominates is the open question this section will answer.

## 4. The spread does not track what a reader would expect

Spearman rho against the **STAR read-level share**, n=784. Ruled out:

| covariate | rho | p | verdict |
|---|---|---|---|
| `fragments` (STAR input) | -0.029 | 0.41 | depth explains nothing |
| `n_fragments` (counter input) | +0.025 | 0.49 | nor on the other denominator |
| `input_read_length` | +0.003 | 0.93 | nothing |
| `age_days` | -0.035 | 0.32 | nothing continuous |
| `genes_detected` | -0.119 | 8.5e-4 | rho^2 = 1.4%, negligible |
| `replicate` index | -0.094 | 8.5e-3 | negligible |
| `split_share_kept_ecHT115` | **-0.147** | 3.7e-5 | significant, **negative**, rho^2 = 2.2% |

That last row is worth stating plainly: **bacteria-heavy cells have FEWER multimappers, so this is
not a contamination story.**

Not ruled out, and why each is weak evidence:

- **`uniquely_mapped_pct`, rho -0.704 (STAR) and -0.947 (counter).** Near-tautological: STAR's
  percentages share one denominator and unique/multi trade off by construction. **Not a mechanism.**
- **`saturation`, rho +0.354** — the strongest non-tautological positive after rRNA, and §5 is about
  which direction it points.
- **Age and strain as factors are highly significant and non-monotonic.** Kruskal-Wallis: age
  H=131.5, p=1.4e-24; strain H=32.9, p=7.0e-8, medians CF 32.60 / N2 30.57 / DA 29.31. But day7 is
  the high group at 33.62 and day13 the low at 24.82 — and day13 also holds the plate maximum. Read
  that as a plate or batch effect with wide within-day spread, not as biology.

## 5. The saturation hypothesis, eliminated and backwards

`#427`'s second comment proposed that multimapper share tracks library complexity, on the lead that
`day17_N2_7` is both the lowest-multimapper cell and the most saturated. **Across 784 cells the
correlation is rho = +0.354** — more saturated cells have MORE multimappers, the opposite direction.
`day17_N2_7` is a genuine outlier against the plate trend, not an instance of it.

The pilot's saturation figures also came from an ad-hoc ledger and differ from the shipped `obs`
column — `day17_N2_7` read 0.634 then and reads 0.810 now — so anything computed off that ledger
wants recomputing before it is quoted.

## 6. The 6.1%-vs-40.5% contrast, resolved: quantity, not composition

| metric | `day17_N2_7` | `day17_N2_12` | ratio |
|---|---|---|---|
| STAR read-level share | 6.06% | 40.52% | 6.69x |
| counter fragment-level share | 6.817% | 46.213% | 6.78x |
| multiply-placed fragments | 503,687 | 3,915,330 | **7.77x** |
| saturation | 0.8104 | 0.5189 | 0.64x |
| genes_detected | 11,015 | 14,307 | 1.30x |
| mean NH | 3.207 | 2.926 | 0.91x |
| % of multiplaced at NH=3 | 43.09 | 84.05 | — |
| rRNA share of placement | 44.993% | 56.720% | 1.26x |
| rRNA share of `umi_combined` | 1.090% | 3.077% | 2.82x |

**Same locus classes, 7.8x the quantity.** Cosine similarity of the two placement composition vectors
is **0.9726**, ranks 1 and 2 are identical, 90.10% of `_7`'s placement sits in genes also placed in
`_12` and 96.10% conversely. This is not a composition difference in the "different locus classes"
sense, and calling it one would be wrong.

The secondary effect is **concentration**: `_12`'s NH profile collapses onto NH=3 (43.1% -> 84.1%)
and its top-30 concentration rises. `_7` is the anomaly rather than the norm — a cleaner library
(78.5% uniquely mapped, saturation 0.81) whose small residual multimapper population is unusually
spread out, 13.3% at NH=4 and 3.49% at NH=10.

The general version holds across the plate. By STAR-share tercile, **high-multimapper cells have a
LOWER mean NH** (2.939 low -> 2.920 high), because their extra multimappers pile into NH=3 (78.8% ->
84.1%) while every bin at NH>=4 shrinks. **A cell with more multimappers is not hitting more loci per
read; it is hitting the same small-copy-number families more often.**

## 7. rRNA is the largest identified driver, and it is sitting in the primary matrix

rRNA here is `gene_biotype "rRNA"`: **38 genes** in the chimeric GTF, **16 of them ecHT115**, so 22
are ce11.

| definition | denominator | min | q05 | median | q95 | max | plate |
|---|---|---|---|---|---|---|---|
| rRNA share of placement | UMI-deduped, one-gene-body | 18.986 | 34.518 | **51.354** | 70.697 | 94.373 | 49.530% |
| rRNA share of `umi_combined` | all UMI-deduped molecules | 0.448 | 1.581 | **3.149** | 6.623 | 32.542 | 2.873% |

Correlation with the STAR read-level share: **+0.4706** for the placement definition and **+0.4480**
for `umi_combined`. **The second is the one that carries the argument** — there rRNA is *uniquely*
mapped and sits in the expression matrices themselves, so the correlation cannot be a circular
consequence of rRNA dominating the placement layer. rho^2 ~ 22% and ~20%: the largest single
identified driver, and nothing like the explanation.

Two things travel with that number.

**The strict share is a floor.** `rrn-3.56` sits inside the 45S repeat and the GTF calls it
`pseudogene`. Widening to the repeat unit (chrI:15,058,000-15,073,000, 30 genes) moves plate placement
share 49.530% -> **53.13%** and the median 51.354% -> **55.19%**, and barely moves either correlation.
The definition is worth ~4 points and somebody has to choose it.

**rRNA is in the primary matrices, and nothing currently says so.** `rrn-3.1` carries **9,320,608**
molecules in the plate's `umi_combined`; `day17_N2_12` alone has 13,074. Anyone drawing an expression
figure off this plate has to drop the `rrn` genes first.

## 8. Ten shallow libraries, reported and not excluded

Ten cells are under 1M fragments: `day1_CF_1`, `day3_N2_28`, `day5_CF_19`, `day7_CF_19`,
`day7_DA_13`, `day9_CF_19`, `day13_CF_28`, `day13_DA_22`, `day15_CF_31`, `day15_DA_26`. **All 784
cells are reported with no exclusions**, because rho(share, depth) = -0.029 says depth does not drive
the share.

Restricting to the 774 cells at >=1M fragments moves **exactly one number**: the plate maximum,
**51.470% -> 44.940%**, because `day13_CF_28` (582,959 fragments) drops out. Every correlation moves
by <=0.007 and every quantile by <=0.16 pp.

Those ten share a signature worth stating: rRNA share of placement **65.1-94.4%**, every one of them
above the plate median. `day13_CF_28` is **94.37%** rRNA in placement and 31.00% in `umi_combined`.
**The plate's maximum multimapper share is an rRNA-dominated library** — the cleanest single
illustration of §7.

## What this recommends: no bar, and a different metric instead

**Neither a threshold nor a report panel for multimapper share.** The reasoning, so a future reader
can disagree with it:

- **The share is substantially a proxy for rRNA carry-over.** Grading a proxy is worse than grading
  the thing it proxies, and it teaches a reader to act on the wrong lever.
- **The number is composite**: an assembly artifact (a collapsed rDNA array) times a prep variable
  (rRNA carry-over), plus a paralogue tail. A bar would be specific to this assembly and this
  annotation and would not survive either being changed.
- **It correlates weakly with anything a reader would act on** — `genes_detected` at rho^2 = 1.4%.

So `map/star-umi` and `map/star-umi-chimera` stay in `MODULES_WITHOUT_CROSS_CHECKS`, and **that
argument now rests on evidence rather than on absence of evidence.**

**What is proposed instead is a direct per-cell rRNA-fraction metric**, and the design is not a
one-repo change:

- **seqforge must not parse gene biotype at all.** That would be seqforge defining genome-file
  machinery, which R10 — *consumer, not parallel universe* — forbids in as many words. The seam is a
  **category query**: given a registered annotation and a category name, `liulab-genome` hands back
  the gene list belonging to that category. `rRNA` and mitochondrial RNA are the two this measurement
  motivates; the mechanism is general and other categories take the same shape.
- **How that list is derived is entirely upstream's business** — a GTF's own biotype attribute where
  one exists, manual curation, or a professional reference database. seqforge must not know which and
  must not care.
- **The derivation is not trivial, which is why it belongs upstream.** Measured across the
  annotations registered on `ircbc`: ce11/WormBase WS298 declares `gene_biotype`, rRNA=22;
  ecHT115/RefSeq `gene_biotype`, rRNA=16; hg38/GENCODE v50 declares `gene_type` instead, rRNA=47 plus
  `rRNA_pseudogene`=497 plus `Mt_rRNA`=2; mm39/GENCODE vM39 `gene_type`, rRNA=354 plus `Mt_rRNA`=2;
  and **sacCer3/ensgene_v101 carries no biotype attribute at all**. Read as a spec for seqforge that
  table is a portability trap; read as upstream's problem it is one curated gap and four different
  spellings of the same fact, in the repo whose Annotation context already owns what a GTF declares
  over one assembly and already keeps a curated row per (Assembly, Registered name). **It is a
  per-annotation fact, not a per-assembly one**, so it cannot ride the per-assembly table ADR-0056's
  intron cap uses.
- **What stays seqforge's is WHICH categories the metric sums, and reporting it.** That is a counting
  decision, on the recipe side of the parse/count split. This plate proves the judgment is worth ~4
  points — 49.5% against 53.1% depending on whether the `rrn-3.56` pseudogene inside the 45S repeat
  counts — which is exactly why the category has to be *requestable* and the choice has to live with
  the counter.
- **An annotation that cannot answer must yield NO rRNA metric rather than 0%**, following ADR-0056's
  "an unfilled row must change nothing rather than impose a number nobody chose."
- **Nothing at the mapping stage should change.** The metric wants count-time data the counter
  already holds. Doing it at mapping means either masking the rDNA — which changes what the data IS,
  the wrong side of the parse/count split — or opening an annotation the counter already has open.

## Caveats

1. **Chimeric arm only.** This run is chimeric, and the measurement cannot separate any contribution
   of the chimeric reference itself to the multimapper share. It does not try. Cross-Component
   attribution (`split.multiplaced` per organism) is `#425`'s item 2 and stays out of scope here.
2. **The representative-locus caveat, on every ranking above.** `--outSAMmultNmax 1` means STAR emits
   one top-scoring record per multimapping fragment, so an emitted record is a **member of the locus
   set, not a draw from the genome**. A count in gene X means one of the loci the fragment could have
   come from is X, never that the fragment is X. Concretely: **`rrn-1.2` at 28.3% against `rrn-1.1`
   at 15.6% is STAR's pick bias between two near-identical copies, not a 2:1 abundance difference.**
3. **The plate is one workflow stamp behind.** It ran under `WORKFLOW_VERSION 2026.8.18`; ADR-0056's
   intron cap ([#464](https://github.com/liuhlab/seqforge/issues/464)) landed afterwards under
   2026.8.19, whose commit message is "the workflow stamp moves, because these modules align
   differently". The expectation is that this barely moves rRNA — rRNA is intron-free and the
   ambiguity is copy-number driven — but the paralogue tail could shift, and **this measurement does
   not test that.**
4. **The placement layer's denominator is small.** 36,732,376 molecules is **1.18%** of the NH
   denominator and 7.07% of `umi_combined`; it covers only UMI-tagged fragments whose representative
   span falls in exactly one gene body. The denominator belongs beside every percentage read off it.
5. **`--outSAMmultNmax` was not changed**, in-tree or out. The locus *set* is therefore not measured
   here at all; the ticket's step 3 was judged not demanded, because the classes are recognisable
   from the representative alone.
6. **[#443](https://github.com/liuhlab/seqforge/issues/443) corroborates on a narrow slice and is not
   the answer.** It measured NH on cross-contig half-mapped fragments of `day1_N2_7`, none of which
   had NH=1.

## Artifacts

`aging_SS3/script/metrics427/plate/` — `summary.json`, `covariate_sweep.tsv`, `group_tests.tsv`,
`rrna_gene_set.tsv`, `rrna_share_correlations.tsv`, `per_cell_top20_placed_genes.tsv`,
`plate_top30_placed_genes.tsv`, `per_cell_composition_uniformity.tsv`,
`contrast_day17_N2_7_vs_12.tsv`, `contrast_top15_genes.tsv`, `contrast_composition_vs_quantity.tsv`,
`nh_histogram_plate.tsv`, `nh_per_cell.tsv`, `nh_profile_by_tercile.tsv`,
`denominator_gap_per_cell.tsv`, `per_cell_multimapper.tsv`, plus `README.md`. Scripts:
`plate_stats.py`, `followup.py`, `rdna_ext.py`, `denom.py`. The tiling probe of §3 is under
`aging_SS3/script/metrics427/nh3/`.
