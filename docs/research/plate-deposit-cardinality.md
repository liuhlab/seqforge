# Plate deposit cardinality — is there a sample-count threshold that selects a plate?

Measured 2026-08-04 for [#244](https://github.com/liuhlab/seqforge/issues/244), under map
[#225](https://github.com/liuhlab/seqforge/issues/225).

**Answer: no.** `strict 1:1 ∧ n_samples > T` — the condition [#240] decided — has **no admissible
`T`**. Against 6,861 droplet/bulk deposits its margin is zero, not the 131× that 18 benchmark
deposits suggested. The finding is a falsification, not a number.

## What was measured

Two pools, deposit scope (the whole BioProject, which is what a user handing seqforge an accession
gets), from ENA `filereport?result=read_run`, 2026-08-04.

| pool | how it was built | deposits found | present in ENA |
|---|---|---:|---:|
| **plate** | NCBI full-text `db=bioproject` over 14 plate-protocol terms — `smart-seq2/3`, `smartseq2/3`, `CEL-seq`, `CEL-seq2`, `MARS-seq`, `SMART-Seq v4`, `384-well`, `96-well`, `plate-based single cell`, `Fluidigm C1`, `SCRB-seq`, `STRT-seq` — every hit, deduped | 1,897 | **1,690** |
| **control** | the same, over 13 droplet/bulk terms — `10x Genomics Chromium`, `Chromium Single Cell 3'/5'`, `Drop-seq`, `inDrop`, `sci-RNA-seq`, `SPLiT-seq`, `Parse Biosciences`, `BD Rhapsody`, `single nucleus RNA-seq`, `bulk RNA-seq`, `TruSeq Stranded mRNA`, `polyA selected RNA-seq` | 7,333 | **6,894** |

Plus the 18 benchmark deposits, `PRJNA853582` (GSE207085), and the two SMART-seq3 reference deposits,
fetched by accession for comparison.

Per deposit: distinct BioSample / experiment / run counts, whether **every** sample has exactly one
run (`strict 1:1`), the set of distinct `(library_strategy, library_source, library_selection)`
triples, and a plate-detection pass over `sample_title` + `sample_alias`.

### The plate detector is an instrument, not a proposed predicate

The detector flags a deposit as well-labelled when its sample labels carry ≥12 distinct
`[A-P](1-24)` well tokens on ≥60% of rows, forming a 96- or 384-shaped grid. Uppercase row letters
only — lowercase admits `Perfusate_d9_`, a day label.

**Its recall is poor and that is stated up front: it does not detect GSE207085**, whose records carry
no well coordinates at all ([#228]). It is used to find *examples* of plates, never to argue that
something is not one. Every claim below that a deposit is *not* a plate is hand-verified against its
`library_construction_protocol` and sample titles, not taken from the detector.

## Where real plates fall

170 deposits are well-labelled **and** strictly 1:1.

| | |
|---|---|
| smallest | **35** samples (`PRJNA320953`, Fluidigm C1) |
| next five | 45, 48, 69, 77, 80 |
| deposits at exactly 96 | 9 |
| largest | 16,607 (`PRJEB14363`) |
| 96-well : 384-well split | 101 : 89 |

Fire rate as a function of `T`, well-labelled plates only:

| `T` | fires at | plates firing |
|---:|---:|---|
| 11 | ≥12 | **170/170** (100%) |
| 23 | ≥24 | 170/170 (100%) |
| 47 | ≥48 | 168/170 (98.8%) |
| 71 | ≥72 | 166/170 (97.6%) |
| 95 | ≥96 | **156/170** (91.8%) |
| 191 | ≥192 | 131/170 (77.1%) |
| 383 | ≥384 | 96/170 (56.5%) |

A further **20 of 190** well-labelled plate deposits are not strictly 1:1 and therefore fire at no
`T` at all. The `all`-not-`most` strictness [#240] found load-bearing costs more real plates (10.5%)
than any choice of `T` inside `[11, 34]` costs (0%).

**This narrowed the band and looked like progress.** `T ≥ 11` (from `GSE282765`) and `T ≤ 34` (the
smallest measured plate) leaves `[11, 34]`, every value of which fires on 170/170 plates and 0/18
benchmark deposits. Then the control pool landed.

## Why no `T` exists

17 control deposits are strictly 1:1 with **more samples than GSE207085's 1440**. Each was classified
by hand from its study title and `library_construction_protocol`. Seven turned out to be genuine
one-cell-one-file deposits the detector missed (Fluidigm C1, FACS-into-plate SMART-seq2, ICell8) —
true fires, and further evidence the detector under-counts plates.

**Four are verified non-plates:**

| deposit | samples | runs | what it is |
|---|---:|---:|---|
| `PRJNA637317` | 6,003 | 6,003 | bulk RNA-seq, one library per macaque. *RNA-seq Data from Vaccine-elicited CD8 T-cells* |
| `PRJEB105229` | 2,608 | 2,608 | **bulk** 3′ RNA-seq, one library per cancer cell line — *Mouse Cancer Cell line Atlas, 22 lineages and 46 cancer types* |
| `PRJEB22075` | 2,553 | 2,553 | **droplet** — *scRNA-seq of dermal fibroblasts using microfluidic droplet capture*, samples titled `Sample 3`, `Sample 7` |
| `PRJEB22073` | 2,130 | 2,130 | **droplet**, the stimulated arm of the same study |

Every one is strictly 1:1 and larger than GSE207085. So:

> A `T` that fires on GSE207085 fires on all four. A `T` that spares all four never fires on
> GSE207085. **There is no admissible `T`** — not `34`, and not `1439` fitted to GSE207085 itself.

Two of the four are droplet, which is the path [#225] constraint 3 exists to protect.

`PRJNA1234645` deserves its own line: **11,805 samples, 11,805 runs, strictly 1:1, three assay
triples** — *in-plate chromosome conformation capture (Plate-C)*, 11,747 Hi-C runs beside 54 bulk
RNA-seq and 4 single-cell. Strictly 1:1, heterogeneous, and 8× GSE207085. Not RNA-seq, so it is not
counted among the four, but it shows the shape is not rare even at extreme size.

### The 131× margin was a corpus artifact

[#240] measured `max(n_samples) = 11` among the strictly-1:1 benchmark deposits, against GSE207085's
1440. That is true and it is not generalisable: **11.3% of control deposits (776 of 6,861) are
strictly 1:1 with more than 34 samples.** A large strictly-1:1 deposit is the *normal* shape for a
bulk population study — one BioSample per specimen, one run each — not a rarity. The 18-deposit
corpus simply contains none.

The corresponding contaminated upper bound, stated so nobody quotes it as clean: 2,679 of 6,861
control deposits (39.1%) satisfy `strict 1:1 ∧ n_samples > 11`. That figure includes plates the
detector missed and is **not** a false-fire rate. The four hand-verified deposits are what the
argument rests on.

### Raising `T` does not buy safety either

Share of firing deposits carrying more than one assay triple, plate pool: 5.0% at `T=11`, 5.3% at
`T=23`, 5.0% at `T=47`, 4.0% at `T=95`. The mirror-image hazard [#240] named is flat in `T`.

## The SMART-seq3 reference deposits are not one-cell-one-file

| deposit | samples | runs | strictly 1:1 | sample titles |
|---|---:|---:|:---:|---|
| `PRJEB36367` (E-MTAB-8735, Hagemann-Jensen 2020) | **10** | 10 | yes | `HEK293T salts optimization`, `Mouse Fibroblast GelCut`, `HCA benchmark` |
| `PRJEB50980` (E-MTAB-11452, SMART-seq3xpress) | **6** | 6 | yes | `PBMCs_run1` … `PBMCs_run6` |

The samples are whole multiplexed libraries — hundreds of cells each, not yet demultiplexed. Both sit
*below* the `T > 11` floor and both **should**: they are not in the class [#225] targets, which
assumes demultiplexing already happened upstream.

The consequence worth carrying forward: **the deposit shape is the submitter's choice, not the
protocol's.** GSE207085 deposited 1440 BioSamples; the protocol's own authors deposited 10 and 6 for
the same chemistry. No count over archive records can be a proxy for "this is SMART-seq3".

## Reproducing

**The measurement is committed and the collectors are not, deliberately** (#296). `plate-pool.tsv`
and `control-pool.tsv` beside this page are the derived per-deposit rows exactly as measured on
2026-08-04, so every table above is checkable against them with `awk` and no network. The five
Python collectors that produced them are **not** in git: `test_nothing_tracked_escapes_the_type
_checker` makes tracked Python checked Python, and a script that has to be rewritten to satisfy
`mypy --strict` stops being the record of what ran. Everything needed to write them again is prose
above rather than a dependency on that code — the 14 plate and 13 control search terms, NCBI
full-text `db=bioproject` taking every hit with no curation, ENA `filereport?result=read_run` at
deposit scope for the cardinality, and the detector's rule (≥12 distinct `[A-P](1-24)` well tokens,
uppercase only, on ≥60 % of a deposit's sample labels). Networked, ~45 min, no reads fetched —
records only.

Both pools are snapshots, and re-running them will not reproduce these tables: ENA gains deposits
daily, so the counts drift upward. **That is why the rows are committed rather than regenerable**,
and it is also why the four hand-verified counter-examples are the durable part of the finding and
the totals are not.

[#225]: https://github.com/liuhlab/seqforge/issues/225
[#228]: https://github.com/liuhlab/seqforge/issues/228
[#240]: https://github.com/liuhlab/seqforge/issues/240
