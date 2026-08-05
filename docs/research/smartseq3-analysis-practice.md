# SMART-seq3 analysis practice: what counts a pipeline must produce

Research for [#227](https://github.com/liuhlab/seqforge/issues/227), under the plate-based map
[#225](https://github.com/liuhlab/seqforge/issues/225). Primary sources only — the papers, the tools'
own source code and configuration, and maintainer statements. Every claim carries its source.
Anything that could not be established from a primary source is listed at the end rather than
guessed at.

Access note: the SMART-seq3 paper ([Nat Biotechnol 38:708–714](https://www.nature.com/articles/s41587-020-0497-0))
is paywalled and not in PMC. Its Methods are quoted here from the authors' own preprint
([bioRxiv 10.1101/817924](https://www.biorxiv.org/content/10.1101/817924v1.full)) and from their
published code release, with published figure legends where those were readable. SMART-seq3xpress is
open access ([PMC9546772](https://pmc.ncbi.nlm.nih.gov/articles/PMC9546772/)) and is quoted directly.

## The short answer

A SMART-seq3 library contains **two read populations that must be counted by different rules**.
Crossing that split with the exon/intron split is where the matrices come from:

| | exonic | intronic | combined |
| --- | --- | --- | --- |
| **UMI-tagged 5′ reads** → deduplicated **molecule** counts | yes | yes | yes |
| **internal reads** → **read** counts, undeduplicatable | yes | yes | yes |

Both shipped counters produce that, in different packaging:

- **`umite`** writes five files by default: `umite.UE.tsv`, `umite.UI.tsv`, `umite.RE.tsv`,
  `umite.RI.tsv`, `umite.D.tsv` — UMI/read × exon/intron, plus UMI duplicates for QC.
- **`zUMIs`** writes one `.dgecounts.rds` holding `umicount`, `readcount`, **`readcount_internal`**
  and `rpkm`, the first three each split into `exon` / `intron` / `inex`.

**`D` (umite) and `rpkm` (zUMIs) are not expression matrices.** `D` is an amplification-rate QC
quantity; `rpkm` is a normalized view of one matrix that already exists.

**STARsolo cannot do any of this.** `--soloType SmartSeq` is documented *and implemented* as a
no-UMI mode. That is the single most decision-relevant finding here.

## 1. The read structure, and why there are two populations

Read 1 carries, in order: an 11 bp tag, an 8 bp UMI, `GGG`, then cDNA. The tag is the distal end of
the template-switching oligo: `ATTGCGCAATG`.

> "we constructed a template-switching oligo (TSO) that harbored a primer site consisting of a
> partial Tn5 motif and a novel 11 bp tag sequence, followed by a 8bp UMI sequence and three
> riboguanosines … After sequencing, the 11 bp tag can be used to unambiguously distinguish 5′
> UMI-containing reads from internal reads. Therefore, we obtain strand-specific 5′ UMI-containing
> reads and unstranded internal reads spanning the full-transcript without UMIs in the same
> sequencing reaction"
> — [SMART-seq3 preprint](https://www.biorxiv.org/content/10.1101/817924v1.full)

The oligo itself: `5′-Biotin-AGAGACAGATTGCGCAATGNNNNNNNNrGrGrG-3′`
([SMART-seq3 Methods](https://www.biorxiv.org/content/10.1101/817924v1.full);
[SMART-seq3xpress Methods](https://pmc.ncbi.nlm.nih.gov/articles/PMC9546772/)). So R1 bases 1–11 are
the tag, 12–19 the UMI, 20–22 the `GGG`, and 23 onward the cDNA — which is exactly what the authors'
own zUMIs config declares (§4).

A read whose R1 starts with the tag is a **UMI-tagged read**: it marks a molecule's 5′ end and
carries an 8 bp molecular barcode. A read produced by Tn5 cutting somewhere in the middle of the
amplified cDNA is an **internal read**: no UMI, no fixed orientation, and no way to tell a PCR
duplicate from a second molecule. Both are sequenced together, in the same FASTQ pair, for the same
cell.

**The ratio between them is not a constant.** It is a wet-lab knob:

> "The proportions of 5′ to internal reads could be tuned by altering the Tn5-based tagmentation
> reaction"
> — [SMART-seq3 preprint](https://www.biorxiv.org/content/10.1101/817924v1.full)

It also varies with PCR polymerase, index primers, and even the sequencer. The published Extended
Data Fig. 3 reports that "Sequencing the libraries shown in (a) on an Illumina HiSeq3000 results in
higher fractions of 5′ UMI reads than when the same libraries are sequenced on the Illumina
NextSeq500 … Sequence machine biases are likely fragment length related"
([Nat Biotechnol](https://www.nature.com/articles/s41587-020-0497-0)). Smart-seq3xpress spent much
of its optimization on getting the ratio back under control:

> "the resulting libraries were heavily biased toward 5′ reads that contain the unique molecular
> identifier (UMI) at the expense of the internal reads important for full-transcript coverage
> scRNA-seq. This resulted from inefficient tagmentation and the inability to modulate the ratio of
> UMI-containing and internal reads by Tn5 amounts"
> — [SMART-seq3xpress](https://pmc.ncbi.nlm.nih.gov/articles/PMC9546772/)

Neither paper states a headline UMI-read percentage in prose; the numbers exist only as figure axes.
The one published spread comes from a third party, over 1,440 murine libraries and 500 human T cells:

> "Overall, cells contained 6%–78% UMI-reads"
> — [umite, *Bioinformatics* 2026](https://pmc.ncbi.nlm.nih.gov/articles/PMC12989134/)

**A pipeline must therefore treat the UMI-read fraction as a measured per-library quantity, never an
assumption.**

## 2. What it buys over SMART-seq2 and over 10x

Against **SMART-seq2**: no UMI at all, so every count is a read count and PCR duplicates are
indistinguishable from molecules. SMART-seq3 bolts molecule counting onto full-length coverage:

> "Smart-seq3, which combines full-length transcriptome coverage with a 5′ unique molecular
> identifier RNA counting strategy that enables in silico reconstruction of thousands of RNA
> molecules per cell"
> — [SMART-seq3 abstract](https://www.nature.com/articles/s41587-020-0497-0)

Against **10x**: 10x gives molecule counts but only a short window at one end of the transcript, so
a molecule cannot be phased to an allele or an isoform. In SMART-seq3 the UMI links every fragment of
one molecule, and Tn5's near-random cuts mean those fragments tile the transcript:

> "copies of the same cDNA molecule with the same UMI obtain variable 3′ ends that map to different
> parts of the specific transcript. Therefore, paired-end sequencing of these libraries results in 3′
> end sequences that span different parts of the initial cDNA molecule that we computationally can
> link to the specific molecule based on the 5′ UMI sequence"
> — [SMART-seq3 preprint](https://www.biorxiv.org/content/10.1101/817924v1.full)

What that yields, quantitatively:

- **61%** of detected molecules got an unambiguous allele from a directly sequenced SNP; **53%** were
  assignable to a single annotated Ensembl isoform, **41%** among multi-isoform genes
  ([preprint](https://www.biorxiv.org/content/10.1101/817924v1.full); the published abstract rounds
  this to "60% … to allelic origin and 30–50% to specific isoforms").
- Per cell, **8,710 molecules reconstructed to ≥500 bp**; ~200,000 molecules ≥1 kb and 22,196 ≥1.5 kb
  across the dataset ([preprint](https://www.biorxiv.org/content/10.1101/817924v1.full)).
- Burst-kinetics inference for **11,766 genes vs 8,464** with SMART-seq2-UMI
  ([preprint](https://www.biorxiv.org/content/10.1101/817924v1.full)).

**Internal reads are not waste, and they have their own products.** They are what makes the library
full-length, and the papers use them for things the UMI reads cannot do:

- Isoform expression via Salmon over all reads, used to filter the molecule-level assignments
  ([preprint](https://www.biorxiv.org/content/10.1101/817924v1.full)).
- **TCR reconstruction**: "Reconstruction of T cell receptor (TCR) sequences from the internal reads
  matched the identified T cell clusters" ([xpress](https://pmc.ncbi.nlm.nih.gov/articles/PMC9546772/)).
- **RNA velocity**: full-length coverage gave "significantly (2–5-fold) increased read support over
  exon–exon and exon–intron splice junctions, which form the basis for RNA velocity inference"
  ([xpress](https://pmc.ncbi.nlm.nih.gov/articles/PMC9546772/)).
- **SNP coverage**: "9.2 million versus 2.1 million positions, over all cells … approximately
  nine-fold higher [alternate allele coverage] … with a three-fold-higher coverage per sequenced
  read" vs 10x ([xpress](https://pmc.ncbi.nlm.nih.gov/articles/PMC9546772/)).

**Every one of those analyses reads alignments, not a count matrix.** The Sandberg lab's own pipeline
reconstructs molecules from the BAM with `stitcher.py`, then assigns allele and isoform from the
reconstructed molecules ([sandberg-lab/Smart-seq3
README](https://github.com/sandberg-lab/Smart-seq3/blob/master/README.md), steps 2–4). xpress ran
BRIE2 `brie-count` over "per-cell demultiplexed, aligned and TSO-artifact-filtered … BAM files".

So: **the per-cell alignment is a first-class deliverable, not a by-product.** A SMART-seq3 h5ad on
its own cannot support the analyses people choose SMART-seq3 for.

### What the benchmarks measured

Plate methods win on sensitivity per cell and lose on cells per dollar. The two standard
head-to-heads, both with depth matched:

- Ziegenhain et al. 2017 ([*Mol Cell*](https://doi.org/10.1016/j.molcel.2017.01.023)), 583 mESCs,
  six methods, all downsampled to 1M reads/cell: "Smart-seq2 detected the highest number of genes
  per cell with a median of 9,138", against 4,811 (Drop-seq) and 4,763 (MARS-seq) — "Drop-seq and
  MARS-seq detect nearly 50% fewer genes per cell". Library cost per cell ran from ~$0.1 (Drop-seq)
  to ~$3 (Smart-seq2 with in-house Tn5) and ~$30 with the commercial kit; "Smart-seq/C1 is almost
  13-fold less efficient than Drop-seq" on total cost to reach 80% power.
- Ding et al. 2020 ([PMC7289686](https://pmc.ncbi.nlm.nih.gov/articles/PMC7289686/)), PBMCs: "the
  low-throughput methods Smart-seq2 and CEL-Seq2 had the highest sensitivities … whereas among
  high-throughput methods, 10x Chromium detected the most UMIs and genes per cell" — Smart-seq2
  2,406/2,632 median genes vs 1,482 for 10x v3. And on cost: "Smart-seq2 was the most expensive,
  primarily because there is no pooling during library preparation."
- Tabula Muris ([PMC6642641](https://pmc.ncbi.nlm.nih.gov/articles/PMC6642641/)) ran both on the same
  tissues in one lab: "bladder … ~4,900 (FACS), 2,900 (droplet)", but "kidney … ~1,400 (FACS), 1,900
  (droplet)". Plate is not uniformly better per cell.

SMART-seq3 specifically improved on SMART-seq2 — "typically detecting thousands more transcripts per
cell" ([published abstract](https://www.nature.com/articles/s41587-020-0497-0)) — and xpress beat
matched-donor 10x on cluster agreement with an external reference, "ARI 0.59 vs 0.49" at matched
cells and reads ([xpress](https://pmc.ncbi.nlm.nih.gov/articles/PMC9546772/)).

**The cost that bears on pipeline design is batch structure: a plate is a batch.** Ziegenhain
measured it: "All methods had significantly more genes differentially expressed between batches than
expected from permutations (zero to four genes), with a median of 119 (Drop-seq) to ~1,135
(CEL-seq2/C1) differentially expressed genes." Their conclusion — "experiments need to be designed
in a way that does not confound batches with biological factors" — means the plate and well of every
cell are analysis-relevant metadata, not provenance trivia.

## 3. The count matrices, tool by tool

### zUMIs — what the protocol authors use

zUMIs is what both papers used (2.4.1+ with STAR 2.5.4b for SMART-seq3; 2.8.2+ with STAR 2.7.3 for
xpress) and what the published Code Availability points at. Its expression output is a single
`.dgecounts.rds`.

The [wiki's Output page](https://github.com/sdparekh/zUMIs/wiki/Output) documents this much:

```r
AllCounts$umicount$ {exon, inex, intron}$ {all, downsampling}
AllCounts$readcount${exon, inex, intron}$ {all, downsampling}
```

> "AllCounts is a list of lists with all the count matrices as sparseMatrix. The parent list contains
> UMI and read count quantification. In each of these counting types, you will find the three feature
> types (introns, exons and intron+exon). Each of those contain a sparseMatrix generating using all
> reads observed and a list for the downsampling sizes requested."
> — [zUMIs wiki, Output](https://github.com/sdparekh/zUMIs/wiki/Output)

**The wiki is stale, and for SMART-seq3 it omits half the answer.** Its head commit is 2020-11-24,
predating the SMART-seq3 support it is supposed to document. The source
([`zUMIs-dge2.R`](https://github.com/sdparekh/zUMIs/blob/main/zUMIs-dge2.R), lines 233–256) actually
builds two more top-level slots when the data is SMART-seq3:

```r
if(smart3_flag){
  final <- list( umicount           = convert2countM(alldt=allC, what="umicount"),
                 readcount          = convert2countM(allC, "readcount"),
                 readcount_internal = convert2countM(allC, "readcount_internal"))
}
...
rpkms <- if(smart3_flag) RPKM.calc(final$readcount_internal$exon$all, tx_len) else ...
final$rpkm <- list(exon = list(all = rpkms))
```

So the real SMART-seq3 output is:

| slot | contents | shape |
| --- | --- | --- |
| `umicount` | UMI-collapsed molecules. Only 5′ tagged reads can contribute. | exon / inex / intron × all / downsampling |
| `readcount` | **all** reads — tagged *and* internal | exon / inex / intron × all / downsampling |
| `readcount_internal` | internal (non-UMI) reads only. **SMART-seq3 only.** | exon / inex / intron × all / downsampling |
| `rpkm` | RPKM from `readcount_internal$exon$all` | `exon$all` only — no inex, no intron, no downsampling |

Two things follow that a consumer has to know:

- **`readcount_internal ⊆ readcount`.** They are nested, not disjoint. The count of *UMI-tagged*
  reads is `readcount − readcount_internal`, and zUMIs never materializes it. The counting function
  is explicit ([`UMIstuffFUN.R`](https://github.com/sdparekh/zUMIs/blob/main/UMIstuffFUN.R), lines
  233–244): `umicount = length(unique(UB[!is.na(UB) & UB!=""]))`, `readcount = .N` over every read,
  and `readcount_internal = .N` restricted to `UB==""`.
- **`inex` is not `exon + intron` for UMI counts.** `mapList <- list("exon"="exon",
  "inex"=c("intron","exon"), "intron"="intron")` — a molecule seen by both an exonic and an intronic
  read is *one* UMI in `inex` but one in each of `exon` and `intron`. So `inex ≤ exon + intron` for
  `umicount`, while `inex == exon + intron` for `readcount`. Adding the exon and intron layers to
  reconstruct `inex` is wrong.

zUMIs also writes, with `demultiplex: yes`, **one BAM per cell** under
`zUMIs_output/demultiplexed/` ([wiki, Output](https://github.com/sdparekh/zUMIs/wiki/Output)) — the
option is set in the authors' published config. That is exactly the "one CRAM per cell" shape the map
already fixed. The BAM carries `BC`/`BX` (barcode, raw), `UB`/`UX` (UMI, raw), `QB`/`QU` (qualities),
and gene assignments `GE`/`ES`/`EN` (exon) and `GI`/`IS`/`IN` (intron).

Output formats are **RDS and loom only** — no `.mtx`, no `.h5ad`. `misc/rds2loom.R` walks the RDS two
levels deep and writes `<project>.<type>.<quant>.all.loom` for every combination, densifying with
`as.matrix()` on the way.

**Maintenance status matters for an engine choice.** Last commit on `main` is **2023-03-11**
(`2.9.7e`); last tagged release **2.9.7, 2022-03-27**; 48 open issues; no maintainer replies in the
15 most recent issue comments, spanning 2024-06 to 2026-03. There is no deprecation notice and no
pointer to a successor. `zUMIs.sh` still phones home to GitHub on every run to compare its version
string.

One more sharp edge: **SMART-seq3 detection is a hardcoded string grep over the config.**

```r
smart3_flag <- ifelse(any(grepl(pattern = "ATTGCGCAATG", x = unlist(opt$sequence_files))), TRUE, FALSE)
```

— [`zUMIs-dge2.R`](https://github.com/sdparekh/zUMIs/blob/main/zUMIs-dge2.R), line 35

A SMART-seq3 run declared with any other tag sequence silently loses every SMART-seq3 behaviour: no
`readcount_internal`, no `rpkm`, no split strand rule, no UMI-fragment stats. It does not error.

### umite — newer, purpose-built, third-party

`umite` (Foerster et al., *Bioinformatics* 2026,
[10.1093/bioinformatics/btag075](https://doi.org/10.1093/bioinformatics/btag075);
[PMC12989134](https://pmc.ncbi.nlm.nih.gov/articles/PMC12989134/)) is from the Martin-Villalba lab at
DKFZ, **not** the Sandberg lab. It is two tools around an external aligner: `umiextract`
(FASTQ → FASTQ, UMI trimmed and moved into the read name), STAR + `samtools sort -n`, then `umicount`
(name-sorted BAM + GTF → TSV).

Its default category set is the 2×2 plus duplicates:

> "intronic and exonic gene counts are distinguished as outlined above, being categorized as
> UMI-containing exonic (`UE`), UMI-containing intronic (`UI`), non-UMI exonic (`RE`), non-UMI
> intronic (`RI`), and UMI-duplicate (`D`) by default. These categories each produce an individual
> output file collating that category's counts across all processed cells. UMI-containing and non-UMI
> reads are always distinguished, however whether intronic and exonic reads are treated separately or
> handled together depends on whether the user supplied `--combine_unspliced` or not. If set,
> `--combine_unspliced` will reduced the tracked categories to `U`, `R`, and `D` only."
> — [umite wiki](https://github.com/leoforster/umite/wiki/umite-algorithmic-and-implementation-details)

The README's example output block shows only `umite.U.tsv` / `.R.tsv` / `.D.tsv`
([README](https://github.com/leoforster/umite/blob/main/README.md)) — that is the
`--combine_unspliced` layout and it contradicts the wiki. The shipped workflow is authoritative:
`snakeconfig.yaml` sets `combine_unspliced: False` and the Snakefile builds
`basecols = ['UE','UI','RE','RI'] + ['D']`
([`snakefile_umite_star.smk`](https://github.com/leoforster/umite/blob/main/workflow/snakefile_umite_star.smk)).

Two structural details a consumer must know:

- **Cells are rows, genes are columns** — transposed relative to STARsolo's Matrix Market output.
- **The leading columns are not genes.** They are read-fate counters: `_unmapped`, `_no_feature`,
  `_multimapping`, `_ambiguous`. Only `_unique` reads become gene counts. A reader that treats every
  column as a gene invents four extremely highly expressed ones
  ([README](https://github.com/leoforster/umite/blob/main/README.md);
  [`umicount.py`](https://github.com/leoforster/umite/blob/main/umite/umicount.py)).

Unlike zUMIs, umite's `R*` matrices are **internal reads only** and there is no total-read matrix; the
total is `U + R + D`. Also unlike zUMIs, umite tracks no `inex`: `UE + UI` is an addition over
matrices, and it double-counts a molecule seen both exonically and intronically. Maturity: PyPI
0.1.1, one GitHub star, no independent validation, no published comparison against STARsolo.

### STARsolo — structurally cannot do it

`--soloType SmartSeq` is a **read**-counting mode. From STAR's own parameter documentation:

> `SmartSeq ... Smart-seq: each cell in a separate FASTQ (paired- or single-end), barcodes are`
> `corresponding read-groups, no UMI sequences, alignments deduplicated according to alignment start`
> `and end (after extending soft-clipped bases)`
> — [`extras/doc-latex/parametersDefault.tex`](https://github.com/alexdobin/STAR/blob/master/extras/doc-latex/parametersDefault.tex)

> "Cell barcodes are not incorporated in the read sequences, and there are no UMIs. … (ii) there are
> no UMI sequences, but reads can be deduplicated if they have identical start/end coordinates."
> — [docs/STARsolo.md](https://github.com/alexdobin/STAR/blob/master/docs/STARsolo.md)

This is enforced in source. `ParametersSolo.cpp` hard-errors if `--soloUMIdedup` is `1MM_All`,
`1MM_Directional`, `1MM_Directional_UMItools` or `1MM_CR` under `SmartSeq`, leaving only `Exact`
(positional) and `NoDedup`; `SoloReadFeature_record.cpp` fabricates the "UMI" from the alignment's
start and length rather than reading any sequence. Dobin, asked directly about combining the two:

> "the only possibility to collapse reads based on read start/end position is for SmartSeq data.
> Presently there is no option to collapse based on both position and UMI."
> — [STAR issue #1556](https://github.com/alexdobin/STAR/issues/1556)

Two further restrictions matter for a module design: **`Velocyto` is hard-blocked** under `SmartSeq`
(Dobin has said since 2020 that fixing the segfault is unscheduled —
[#987](https://github.com/alexdobin/STAR/issues/987)), and **multimapper EM is blocked**
(`--soloMultiMappers Unique` only — [#1534](https://github.com/alexdobin/STAR/issues/1534)). There is
also an open cluster of `SmartSeq`-specific segfault reports with no maintainer fix
([#876](https://github.com/alexdobin/STAR/issues/876),
[#912](https://github.com/alexdobin/STAR/issues/912),
[#1107](https://github.com/alexdobin/STAR/issues/1107),
[#1824](https://github.com/alexdobin/STAR/issues/1824),
[#2057](https://github.com/alexdobin/STAR/issues/2057),
[#2595](https://github.com/alexdobin/STAR/issues/2595)).

**Bottom line:** STARsolo `SmartSeq` mode would produce a *SMART-seq2*-grade answer from a SMART-seq3
library — a plausible matrix that is a read count where a molecule count was required, with the
UMI/internal distinction thrown away and no error raised. That is precisely the silent, plausible,
wrong failure class this repo is built against.

### Which matrices are actually used downstream

From the papers' own Methods, not inference:

- **Clustering and cell QC run on exon+intron.** SMART-seq3: "retaining cell with > 500 genes
  detected (intron+exon quantification)". xpress: "more than 500 genes (exon+intron quantification)
  detected per cell". So `inex` is the workhorse.
- **Both UMI and read matrices are analysed, side by side, and the papers say so.** SMART-seq3
  Methods: zUMIs was run "to generate expression profiles for both the 5′ ends containing UMIs as
  well as combined full length and UMI data". Fig. 1e reports reproducibility "at RPKM and UMI
  level"; Fig. 1d reports gene detection "over 0 or 1 RPKM"; Fig. 1f reports "unique error-corrected
  UMI sequences".
- **Allele, isoform, burst-kinetics, TCR and velocity analyses do not read the count matrix at
  all** — they read the per-cell BAM (§2).
- **`D` and `rpkm` are derived, not primary.** umite labels `D` "for QC"; `rpkm` is a normalization of
  a matrix already present.

**No primary source prescribes one canonical matrix.** umite's paper reports the categories
separately and declines to recommend:

> "Gene counts for UMI-containing, internal, or UMI duplicate read pairs are tracked separately, as
> are those for exonic and intronic reads within each group."
> — [umite](https://pmc.ncbi.nlm.nih.gov/articles/PMC12989134/)

Emitting all of them and letting the analyst choose is the documented practice — which maps cleanly
onto the 10x pipeline's `X`-plus-layers shape.

## 4. Tagged vs internal: separately, jointly, or correcting?

**Separately. Neither corrects the other, and no primary source describes such a correction.** They
measure different things: molecules at the 5′ end, and coverage across the body.

- They are separated at the **FASTQ** stage, by pattern match on R1, before alignment. zUMIs uses
  `find_pattern: ATTGCGCAATG`; umite's `umiextract` trims anchor + UMI + `GGG` and appends the UMI to
  the read name, so `umicount` re-derives the class "based on the presence of the UMI in the read
  name" ([umite wiki](https://github.com/leoforster/umite/wiki/umite-algorithmic-and-implementation-details)).
- **Internal reads are kept, never discarded.** In zUMIs the mechanism is subtle and worth knowing:
  for a non-matching read, `fqfilter_v2.pl` empties the UMI *and* resets the cDNA start to base 1
  (so the whole of R1 becomes cDNA rather than starting at 23), then reassigns the pattern variable to
  the read itself so the downstream filter passes it. That reassignment is the entire mechanism by
  which `find_pattern` means "filter" for every other protocol and "classify" for SMART-seq3. umite
  drops internal reads only under `--only_umi`.
- They are counted under **different strand rules**, because a UMI read has known orientation and an
  internal read does not:

  > "16 Mar 2020: zUMIs2.7.1: Smart-seq3 data can be run with the proper consideration of strand
  > information. When setting `strand: 1`, UMI reads will use this strand while non-UMI reads will
  > stay unstranded."
  > — [zUMIs README changelog](https://github.com/sdparekh/zUMIs/blob/main/README.md)

  Implemented by physically splitting the BAM on the `UB` tag, running featureCounts twice with
  different `strand`, and `samtools cat`-ing the halves back together
  ([`zUMIs-dge2.R`](https://github.com/sdparekh/zUMIs/blob/main/zUMIs-dge2.R) lines 69–89;
  [`UMIstuffFUN.R`](https://github.com/sdparekh/zUMIs/blob/main/UMIstuffFUN.R) `split_bam`). The split
  is transient and produces no separate matrix.
- **UMI error correction is applied to the tagged reads only.** zUMIs' Hamming collapse explicitly
  drops internal reads first (`reads <- reads[!UB==""]`,
  [`zUMIs-dge2.R`](https://github.com/sdparekh/zUMIs/blob/main/zUMIs-dge2.R) line 164).

**The one thing that differs between engines is what "read counts" names.** umite's `R*` is internal
only; zUMIs' `readcount` is the total and `readcount_internal` is the internal subset. A pipeline
that swapped engines without renaming its layers would change what a layer contains while keeping its
name. Note also that zUMIs offers no user-facing way to split the *mapping statistics* by read class,
even though it splits the counts — the maintainer, asked exactly that:

> "I don't see any immediate way to do this in zUMIs unfortunately. … Beware that if you give the tag
> match or not in the yaml, you'll always get internal+UMI reads regardless in the case of Smartseq3
> data in zUMIs — they will just not get used correctly if you don't include the pattern."
> — Christoph Ziegenhain, [zUMIs issue #295](https://github.com/sdparekh/zUMIs/issues/295)

## 5. QC metrics, and what a failed well looks like

### The gates the papers actually used

SMART-seq3, HCA benchmark ([preprint Methods](https://www.biorxiv.org/content/10.1101/817924v1.full)):

> "cells were filtered for low quality libraries requiring >10,000 raw reads, >75% of reads mapped to
> the genome and >25% exonic fractions. Further analysis was done within v3.1 of Seurat retaining
> cell with > 500 genes detected (intron+exon quantification)."

with a reported pass rate of "77% of cells passed quality filtering, significantly higher percentages
than the 29% to 63% reported for available protocols".

SMART-seq3xpress, hPBMC atlas ([Methods](https://pmc.ncbi.nlm.nih.gov/articles/PMC9546772/)):

> "Cells were filtered for low-quality libraries, requiring (1) more than 50% of read pairs mapped to
> exons+introns, (2) more than 20,000 read pairs sequenced, (3) more than 500 genes (exon+intron
> quantification) detected per cell and (4) less than 15% of read pairs mapped to mitochondrial
> genes. Furthermore, a gene was required to be expressed in at least ten cells."

and a *looser* gate in the same paper for the depth-matched 10x comparison: "at least 10,000 read
pairs, more than 50% of read pairs mapped to exons+introns and less than 15% read pairs mapped to
mitochondrial genes".

Two observations about the shape of these. **Most of the gates are mapping statistics, not matrix
statistics** — a pipeline that emits only matrices cannot reproduce them. And **the thresholds moved
between the two papers and even within one paper**, which is direct evidence that they are an
analysis-time choice, not a pipeline constant.

### The SMART-seq3-specific metrics

- **UMI-tagged vs internal read counts per cell.** zUMIs emits this, and only for SMART-seq3:
  `misc/countUMIfrags.py` writes `<barcodefile>.BCUMIstats.txt` with header
  `XC \t nNontagged \t nUMItag`. This is the metric that says whether a library is a real SMART-seq3
  or has degenerated into a 5′ assay.
- **Reads per molecule** — `D / U` in umite, `(readcount − readcount_internal) / umicount` in zUMIs.
  The amplification rate.
- **Tagmentation complexity**, "summarized as unique aligned and gene-assigned UMI-containing read
  pairs per 400,000 raw reads" ([xpress](https://pmc.ncbi.nlm.nih.gov/articles/PMC9546772/)).
- **The exon / intron / intergenic / unmapped breakdown, reported separately for the two read
  classes.** The published SMART-seq3 Extended Data Fig. 8c gives it per protocol, and **8d gives it
  "for 5′UMI-containing read pairs" separately** ([Nat Biotechnol](https://www.nature.com/articles/s41587-020-0497-0)).
- **TSO strand-invasion artifact frequency** (xpress; §8).

### What the tools emit

zUMIs writes a `stats/` folder alongside `expression/`
([wiki, Output](https://github.com/sdparekh/zUMIs/wiki/Output)): `<p>.bc.READcounts.rds` and
`<p>.readspercell.txt` (reads per barcode per assignment type, with unretained barcodes collapsed to
`RG=="bad"`), `<p>.genecounts.txt` (genes detected per cell, computed from the **read**-count
matrices), `<p>.UMIcounts.txt` (total UMIs per cell), `<p>.reads_per_gene.txt`, and optionally
`<p>.intronProbability.rds`. Four QC PDFs: `detected_cells` (knee density plus cumulative reads),
`downsampling_thresholds`, `geneUMIcounts` (genes and UMIs per cell split by Exon / Intron /
Intron+Exon), and `features` (a stacked breakdown over
`Exon, Intron+Exon, Intron, Unmapped, Ambiguity, MultiMapping, Intergenic, Unused BC, User`).

umite's per-cell QC is a log line — total reads, uncounted reads and percentage, per-category counts
and percentages, and the number of counts merged by UMI correction
([`umicount.py`](https://github.com/leoforster/umite/blob/main/umite/umicount.py), `process_bam`) —
plus the read-fate columns carried inside the matrices themselves.

**Neither tool produces a gene-body coverage or 5′/3′ bias metric.** For an assay whose selling point
is full-length coverage, that is a real gap: an exhaustive grep of the zUMIs source finds no coverage
plot of any kind, and the string "full-length" appears zero times in its wiki.

### The thresholds differ from droplet thresholds, and the literature shows it directly

Tabula Muris ([PMC6642641](https://pmc.ncbi.nlm.nih.gov/articles/PMC6642641/)) ran both platforms in
one lab and set the cut differently for each:

> "Cells with fewer than 500 detected genes were excluded. … Cells with fewer than 50,000 reads
> (FACS) or 1000 UMI (microfluidic droplet) were excluded."

Same gene threshold, different depth threshold, different *unit* — reads per well for plates, UMIs
per barcode for droplets. Smart-seq3xpress did the same thing within one paper, gating its plate data
at <15% mitochondrial reads and its 10x arm at <10% (§5). Mereu et al. 2020, which unified 13
protocols under one pipeline, used ">10,000 total number of reads", ">65% of the reads mapped", and
"<25% mitochondrial gene content" ([bioRxiv preprint](https://www.biorxiv.org/content/10.1101/630087v1);
the published version is paywalled).

Ding et al. did something a droplet pipeline cannot: because a well's cell count is known by
construction, they fit a model rather than pick a threshold — "For Smart-seq2 and CEL-Seq2, we had a
better estimation of the number of cells as we sorted individual cells into wells. We used a mixture
of two Student's t distribution models to model the read or UMI (log10 transformed) count
distributions of each cell" ([PMC7289686](https://pmc.ncbi.nlm.nih.gov/articles/PMC7289686/)).

### A failed well

There are four failure modes, and they do not look alike.

**Empty well.** The cleanest primary definition comes from the molecular-spikes paper, where
spike-ins in the lysis buffer make an empty well directly identifiable:

> "All empty wells, containing spike-ins in the lysis buffer but received no cell (endogenous reads
> <20% and spike-in mapped reads >80%)"
> — [Ziegenhain et al., *Nat Methods* 2022](https://pmc.ncbi.nlm.nih.gov/articles/PMC9119855/)

Ilicic et al. 2016 ([PMC4758103](https://pmc.ncbi.nlm.nih.gov/articles/PMC4758103/)) — the primary
source on this question, with per-chamber microscopy ground truth for 960 mESCs — found empty wells
easy: "empty wells can be remarkably clearly distinct from the remainder".

**Broken / dying cell.** Harder, and the signature is specific:

> "genes relating to Cytoplasm … are strongly downregulated … in broken cells. Furthermore, broken
> cells have transcriptome-wide increased noise levels."
> "In a situation where cell membrane is broken, cytoplasmic RNA will be lost, but RNAs enclosed in
> the mitochondria will be retained, thus explaining our observation."
> "Only the number of not aligned and non-exonic reads is larger in broken cells"
> — [Ilicic et al. 2016](https://pmc.ncbi.nlm.nih.gov/articles/PMC4758103/)

With spike-ins present, the tell is the ratio: "a subset of the low quality cells has higher ratios
[of ERCC to exonic reads] … due to endogenous transcript loss, most reads map to the spike-in RNA".

**Doublet.** Rare in a sorted plate — Ding measured "<1%, as expected as FACS was used to place a
single cell in each well" — but Ilicic notes it is also hard to distinguish from a broken cell:
"wells containing multiple cells (multiples) show similar expression and noise patterns to broken
cells".

**The "deceptive" well, which is the one that matters for an automated pipeline.** Ilicic found "92
visually intact cells … scattered amongst the damaged cells" — cells that look fine down a
microscope and are transcriptomically damaged, showing "a higher fraction of reads mapped to external
spike-ins (that is, less total RNA) and more expression of mtDNA-encoded genes". Removing them
changed the biology: differential expression went from 116 genes to 855. Their verdict on the usual
gates:

> "Conventional quality control methods were only able to capture half of the visibly damaged cells."

The seven features they found to be protocol- and cell-type-independent are worth recording, because
four of them are mapping statistics rather than matrix statistics: "Cytoplasm, Mitochondrially
localized proteins, mtDNA encoded genes, Mapped reads, Multi-mapped reads, Non-exonic reads, and
Transcriptome variance." Explicitly *not* transferable: "Membrane, Ribosomes, Metabolism, Apoptosis,
and Housekeeping genes are highly cell type specific."

Base rate to expect: "Most of the data we used contained between 10% and 40% low quality cells"
(Ilicic). SMART-seq3 reported "77% of cells passed quality filtering, significantly higher
percentages than the 29% to 63% reported for available protocols"
([preprint](https://www.biorxiv.org/content/10.1101/817924v1.full)).

**The structural difference from droplet data is that an empty well still gets sequenced.** In 10x an
empty droplet is a barcode with almost no reads, and the sheer number of them is what makes the
ambient model estimable — EmptyDrops states the assumption outright: "A key assumption of our
approach is that barcodes with very low UMI totals represent empty droplets. This allows us to use
these barcodes to estimate the ambient profile"
([Lun et al. 2019](https://pmc.ncbi.nlm.nih.gov/articles/PMC6431044/)). In a plate, a well with no
cell still received an index pair and still consumed its share of the lane, so it arrives as a
full-fledged sample with reads in it, and a handful of blank wells does not supply an ambient
distribution. There is no knee to cut on. Every file is a "cell" by construction; the only question
is whether it is a good one.

## 6. Where the cell filter belongs

**Downstream, not in the pipeline** — and the primary sources establish this by what they do rather
than by prescription. In both papers, filtering happens in the analysis step after zUMIs, with
thresholds chosen per dataset. The two different read-pair cutoffs inside the xpress Methods (§5) are
the proof: one pipeline run, two different cuts, each chosen for the comparison at hand.

zUMIs' only in-pipeline cut is `nReadsperCell: 100`
([`mouse_cross.yaml`](https://github.com/sandberg-lab/Smart-seq3/blob/master/allele_level_expression/mouse_cross.yaml)),
which bounds the barcode list rather than calling cells.

For STARsolo the point is sharper: `--soloCellFilter` implements droplet cell-calling
(`CellRanger2.2`, `EmptyDrops_CR`), and those models assume a barcode-per-droplet population with an
ambient background. On a plate they are meaningless. `--soloCellFilter None` exists
([docs/STARsolo.md](https://github.com/alexdobin/STAR/blob/master/docs/STARsolo.md)).

The best-practice tutorials say the same thing prescriptively, and give the reason:

> "As 'sufficient data quality' cannot be determined a priori, it is judged based on downstream
> analysis performance (e.g., cluster annotation). Thus, it may be necessary to revisit quality
> control decisions multiple times when analysing the data."
> "Be as permissive of QC thresholding as possible, and revisit QC if downstream clustering cannot be
> interpreted."
> "If the distribution of QC covariates differ between samples, QC thresholds should be determined
> separately for each sample to account for sample quality differences."
> "Considering any of these three QC covariates in isolation can lead to misinterpretation of cellular
> signals … QC covariates should be considered jointly."
> — [Luecken & Theis 2019](https://pmc.ncbi.nlm.nih.gov/articles/PMC6582955/)

and the [single-cell best practices book](https://www.sc-best-practices.org/preprocessing_visualization/quality_control.html):
"it is advised to exclude fewer cells and be as permissive as possible … it might be reasonable to
re-assess the filtering after the annotation of cells."

A cut that must be revisited, may differ per sample, and can only be judged by what happens after it
is not a compile-time decision. **The pipeline's job is to emit the metrics** — reads per well,
mapping rate, exonic/intronic/intergenic breakdown, genes detected, mitochondrial fraction, spike
fraction, UMI-read fraction, and the plate/well identity — **and not to apply a cell cut.**

This matches what seqforge already does for 10x: build the deliverable from `raw/`, record what was
called as provenance, and leave the decision downstream
(`src/seqforge/workflows/h5ad.py` module docstring).

### Normalization, and why the count type is not interchangeable

The choice of matrix is not cosmetic; it changes which normalization is even valid.

> "scRNA-seq techniques can be divided into full-length and 3′ enrichment methods. Data from
> full-length protocols may benefit from normalization methods that take into account gene length,
> while 3′ enrichment data do not. A commonly used normalization method for full-length scRNA-seq
> data is TPM normalization."
> — [Luecken & Theis 2019](https://pmc.ncbi.nlm.nih.gov/articles/PMC6582955/)

That is precisely why zUMIs computes RPKM for the read side and not the UMI side (§3): a read count
over a full-length library is length-biased, a molecule count at the 5′ end is not. Vieth et al. 2019
([PMC6789098](https://pmc.ncbi.nlm.nih.gov/articles/PMC6789098/)) found the distributional
consequence too: "we assume that UMI counts follow a negative binomial distribution and **only
Smart-seq2 needs the inclusion of zero-inflation**", and "UMI protocols have a noticeably higher power
than Smart-seq2". Extending Pearson-residual normalization to non-UMI data required a whole extra
amplification model ([Lause, Berens & Kobak,
*Genome Biol* 2026](https://genomebiology.biomedcentral.com/articles/10.1186/s13059-026-04161-4)).

So a SMART-seq3 deliverable that ships both count types is not hedging. The two matrices need
different normalizations and support different statistics, and which one an analyst wants depends on
sequencing depth and on the question.

## 7. What full-length coverage demands of the aligner and counter

The authors' own config
([`mouse_cross.yaml`](https://github.com/sandberg-lab/Smart-seq3/blob/master/allele_level_expression/mouse_cross.yaml),
the config for the published fibroblast dataset) plus their README pin most of this down.

- **A splice-aware aligner is mandatory.** Internal reads cross exon-exon junctions. Both reference
  pipelines use STAR.
- **The tag + UMI + GGG must come off R1 before alignment**, or the first 22 bases are non-genomic.
  zUMIs declares `cDNA(23-150)`; umite physically trims in `umiextract`.
- **Clip the Tn5 mosaic end at 3′.** Both reference configs use the same sequence:
  `--clip3pAdapterSeq CTGTCTCTTATACACATCT`
  ([`mouse_cross.yaml`](https://github.com/sandberg-lab/Smart-seq3/blob/master/allele_level_expression/mouse_cross.yaml);
  [umite `snakeconfig.yaml`](https://github.com/leoforster/umite/blob/main/workflow/snakeconfig.yaml)).
  Near-random Tn5 cuts produce short inserts that read into adapter.
- **Raise the splice-junction insert limit**: `--limitSjdbInsertNsj 2000000`, set explicitly in both
  the Methods and the shipped YAML.
- **Filter non-canonical unannotated junctions**: `--outFilterIntronMotifs RemoveNoncanonicalUnannotated`
  ([SMART-seq3 Methods](https://www.biorxiv.org/content/10.1101/817924v1.full)).
- **Do not use two-pass.** Called out by name:
  > "we advise caution when using STARs 2-pass mapping mode, as we have observed some spurious novel
  > splice junctions being used that may distort molecule reconstructions."
  > — [sandberg-lab/Smart-seq3 README](https://github.com/sandberg-lab/Smart-seq3/blob/master/README.md)

  Their config sets `twoPass: no`. Note zUMIs' *own* shipped default is `twoPass: yes`.
- **Paired-end is required for the molecule-level analyses.** "Note that for RNA reconstruction,
  paired-end sequencing data is required." (same README). The base definition differs accordingly:
  `cDNA(23-75)` single-end vs `cDNA(23-150)` paired-end.
- **Intron counting on.** `introns: yes` in the authors' config, and both papers' gene-detection gate
  is on exon+intron.
- **N-mask the genome for allelic work**: "mouse fibroblast cells were mapped against mm10 genome
  with CAST SNPs masked with N to avoid mapping bias"
  ([Methods](https://www.biorxiv.org/content/10.1101/817924v1.full)).
- **Keep multimappers for reconstruction** ("Unique and multi-mapped reads from same molecules
  mapping to exonic regions were used for isoform reconstruction") but drop them for SNP work (xpress
  used MAPQ ≥ 20, "essentially discarding multimapping reads due to the mapping quality encoding of
  the STAR aligner").
- **The BAM must survive, with its tags.** Molecule reconstruction, allele assignment, isoform
  assignment, TCR and velocity all consume alignments (§2). A per-cell CRAM that has lost `UB`/`BC`
  is unrecountable and unreconstructable — the same lossy-CRAM trap already recorded for the 10x path.

**One more structural fact with direct bearing on the map.** In the authors' own workflow the cell
barcode is **the dual Illumina index read**, not anything in R1/R2, and the FASTQs are deliberately
*not* demultiplexed:

> "you should obtain raw fastq files *without demultiplexing*, as the data will be processed in a
> pooled fashion. When running the bcl2fastq conversion, be sure to keep index read fastq files."
> — [sandberg-lab/Smart-seq3 README](https://github.com/sandberg-lab/Smart-seq3/blob/master/README.md)

with `BC(1-8)` declared on both `I1` and `I2`. Index length is not fixed either: the SMART-seq3
Methods describe "index primers containing either 8 or 10 bp indexes", and xpress standardized on
10 bp. Public archives deposit the *demultiplexed* per-cell form, which is why "the cell barcode is
the file" holds for archived data — but it is a property of the deposit, not of the assay.

## 8. What the literature says is commonly got wrong

1. **Treating read counts as molecule counts, or skipping UMI error correction.** Measured against
   ground truth with spike-ins of known copy number:
   > "our direct experimental comparison shows that scRNA-seq data processing should include UMI
   > error-correction to avoid systematically overestimating RNA expression levels"
   > — [Ziegenhain et al. 2022](https://pmc.ncbi.nlm.nih.gov/articles/PMC9119855/)

   The size of the effect, from the same group's method benchmark: extra-Poisson variability medians
   were 2.98 (Drop-seq), 2.17 (MARS-seq), 2.04 (SCRB-seq) on **read** counts, collapsing to 0.29,
   0.41 and 0.15 on **UMI** counts
   ([Ziegenhain 2017](https://doi.org/10.1016/j.molcel.2017.01.023)). "That amplification noise can
   be a major factor is seen by the strong increase of extra Poisson variability when ignoring UMIs
   and considering read counts only." SMART-seq2, having no UMIs at all, "had the highest extra
   Poisson CV" in Ding's independent benchmark. Emitting the SMART-seq3 read matrix and the UMI
   matrix under names that do not distinguish them invites exactly this error.

2. **Getting the collapse wrong in the other direction.** Same paper: at SMART-seq3's 8 nt UMI
   length, "the aggressive collapsing strategies ('cluster' and 'adjacency') underestimate RNA counts
   due to the collapsing of several molecules at higher expression levels", and "the
   'directional-adjacency' method seems to provide a good compromise for UMIs of at least 8 nt", with
   Hamming distance 1 (not 2) appropriate at 8 nt. Both engines implement directional collapse with a
   2× read-support ratio; **umite's is off by default in the CLI** (`--UMI_correct`) though on in the
   shipped workflow. Running `umicount` without it silently overcounts.

3. **TSO strand invasion.** The TSO can mis-prime internally and manufacture a false 5′ end carrying a
   real UMI — a fake molecule at a fake transcription start site. xpress named it, shipped a filter
   (`pyTSOfilter`), and redesigned the TSO (adding `WW` before the `rGrGrG`) to suppress it
   ([xpress](https://pmc.ncbi.nlm.nih.gov/articles/PMC9546772/);
   [cziegenhain/pyTSOfilter](https://github.com/cziegenhain/pyTSOfilter)). The filter has a
   prerequisite that is easy to violate: "Only zUMIs-processed bam files are compatible. Assignment
   of UMI-reads to genes must be performed in **stranded** mode during zUMIs processing" — while the
   authors' own published 2020 config uses `strand: 0`. The reconciliation is zUMIs 2.7.1's split
   strand rule (§4): `strand: 1` makes UMI reads stranded and leaves internal reads unstranded.

4. **STAR two-pass mode** — §7, called out by the protocol authors.

5. **Applying droplet cell-calling to a plate** — §5–6.

6. **Reconstructing `inex` by adding the exon and intron UMI layers** — §3. It is wrong for UMI
   counts and only right for read counts.

7. **Reading umite's matrices as pure gene × cell** — §3. Four leading columns are read-fate
   counters, and the orientation is cells × genes.

8. **Assuming the tag matches exactly.** Both tools tolerate error, and it is worth real signal: zUMIs
   allows 1 mismatch by default and up to a user-set number (`find_pattern: ATTGCGCAATG;2`, since
   2.9.5), and xpress used 2. umite's fuzzy matching "recovered an additional 5%–15% of UMIs" and
   "even among high-quality cells, fuzzy matching boosted UMI recovery by roughly 6%"
   ([umite](https://pmc.ncbi.nlm.nih.gov/articles/PMC12989134/)). Exact matching throws molecules
   away.

9. **Assuming a fixed UMI-read fraction** — §1. It moves with Tn5 dose, polymerase, index primers and
   the sequencer.

10. **Declaring SMART-seq3 to zUMIs with anything but the literal tag** — §3. The SMART-seq3 code path
    is gated on a hardcoded `grepl("ATTGCGCAATG", ...)` over the config and fails open, not loud.

11. **Confounding plate with condition, or treating a plate as one homogeneous batch.** Ziegenhain
    measured 119 to ~1,135 genes differentially expressed *between batches* at FDR < 1%, against
    0–4 expected by permutation, and concluded "experiments need to be designed in a way that does
    not confound batches with biological factors"
    ([2017](https://doi.org/10.1016/j.molcel.2017.01.023)). Luecken & Theis add that plate-based data
    "tend to have batch effects between plates". The practical consequence for a deliverable: plate
    and well must survive into `obs`.

12. **Assuming the pre-processing pipeline is neutral — which is measurably worse for non-UMI data.**
    From the FDA/SEQC multicenter benchmark: "For non-UMI based scRNA-seq data, much larger variation
    was observed in the number of genes detected across the three different pre-processing pipelines"
    ([Chen et al. 2021](https://pmc.ncbi.nlm.nih.gov/articles/PMC11245320/)). Vieth found the aligner
    choice interacts with the protocol too: "for Smart-seq2, we find that kallisto … performs
    slightly better than STAR, while for UMI-methods STAR performs better"
    ([PMC6789098](https://pmc.ncbi.nlm.nih.gov/articles/PMC6789098/)). This is an argument for
    pinning the engine in the manifest hash, which seqforge already does.

13. **Assuming a filtered-out well was a bad cell.** Mereu et al. found the *wet-lab* gate is not
    neutral either: "when we skipped the viability selection step … we observed the same shift in
    composition towards mouse cells, suggesting that cell viability staining excludes cells that are
    amenable for scRNA-seq. Consequently, replacing viability staining with a thorough in silico
    quality filtering in cell atlas experiments might better conserve the composition of the original
    tissue" ([bioRxiv preprint](https://www.biorxiv.org/content/10.1101/630087v1)). Another reason the
    cut belongs downstream, where it can be reconsidered.

## 9. What this implies for seqforge

The 10x deliverable is `X` = `Gene` (exonic) with `GeneFull` and friends as layers, obs = barcode
(`src/seqforge/workflows/h5ad.py`). The SMART-seq3 analogue is the same shape with a larger layer set
and one axis change: **obs is a file, not a barcode.**

A defensible mapping, all from one counting run:

| h5ad slot | zUMIs source | umite source |
| --- | --- | --- |
| `X` | `umicount$inex$all` | `UE + UI` (see caveat below) |
| `layers["umi_exon"]` | `umicount$exon$all` | `UE` |
| `layers["umi_intron"]` | `umicount$intron$all` | `UI` |
| `layers["read_*"]` | `readcount_internal$*$all` | `RE`, `RI` |
| `obs` / `uns` | `BCUMIstats.txt`, `stats/` | log line, read-fate columns |

`obs` has to carry more than it does for 10x: the per-cell mapping statistics (three of the four
published QC gates are mapping statistics, §5), the UMI-read fraction, and **the plate and well** —
because a plate is a batch and the literature's standing warning is not to confound it with a
biological factor (§8, item 11).

Caveat: `umicount$inex` is *not* `exon + intron` (§3), so under umite there is no true `inex` and `X`
would have to be either `UE` alone or an acknowledged over-count. That asymmetry is itself an argument
in the engine bake-off.

Three findings bear directly on open decisions in the map:

- **STARsolo cannot be the engine** (§3). Constraint 4's bake-off is really zUMIs vs umite. If
  STARsolo is benchmarked at all, it should be as the negative control — the thing that produces a
  confident, wrong answer.
- **Neither candidate is comfortable.** zUMIs is what the protocol authors use and what every public
  SMART-seq3 dataset was processed with, but it is unmaintained since March 2023, undocumented for
  half of its SMART-seq3 output, and gated on a hardcoded string. umite is peer-reviewed, faster and
  better documented, but is at version 0.1.1 from a lab with no connection to the protocol, and has
  never been compared against anything but zUMIs.
- **The per-cell CRAM must keep `UB` and `BC`**, or the analyses that justify choosing the assay are
  impossible from the deliverable (§7).
- **`obs` cannot be cell-filtered at compile time** (§6), consistent with the existing `raw/`-only
  rule.

## What could not be established from a primary source

- **The published SMART-seq3 Methods body.** [Nat Biotechnol
  38:708–714](https://www.nature.com/articles/s41587-020-0497-0) is paywalled with no PMC record and
  no OA location. Everything cited as "SMART-seq3 Methods" here is from the authors' preprint or their
  published code release. Where the two could differ (tool versions at revision, thresholds changed in
  review), the published values are unknown.
- **A reported percentage of UMI-tagged reads from either Sandberg-lab paper.** Stated nowhere in
  prose; the numbers exist only as figure axes. The 6–78% range quoted here is from umite's datasets,
  not theirs.
- **Which exact matrix xpress fed to Seurat for the 26,260-cell atlas.** The Methods say
  error-corrected UMI counts, the QC gate is phrased in exon+intron gene detection, and one Extended
  Data legend says "log-normalized read counts". The paper does not reconcile these.
- **A prescriptive statement of which single matrix downstream analysis should use.** No source found
  makes one.
- **A published numeric threshold for spike-in fraction in plate QC.** Ilicic uses the ERCC/exonic
  ratio as a classifier *feature* and Ziegenhain reports 2–5% of reads mapping to ERCC as an
  observation. Neither states "exclude wells above X%".
- **A published numeric duplication-rate threshold for plate data.** Only qualitative use was found.
- **An explicit statement that empty-drop callers must not be run on plate data.** That is an
  inference from EmptyDrops' own stated scope and from Luecken & Theis attributing ambient correction
  to "the large numbers of empty droplets" — not a quote.
- **Mereu et al. 2020 as published.** [Nat
  Biotechnol](https://doi.org/10.1038/s41587-020-0469-4) is paywalled with no PMC record; all Mereu
  quotes here are from the May 2019 bioRxiv preprint and the published values may differ.
- **How the `WW`-improved TSO shifts the zUMIs `UMI(12-19)` / `cDNA(23-)` offsets.** Two extra bases
  must move the cDNA start, but xpress publishes no YAML for it.
- **Whether `readcount_internal` and `rpkm` have ever been verified against a real SMART-seq3 zUMIs
  run.** They are established from source reading
  ([`zUMIs-dge2.R`](https://github.com/sdparekh/zUMIs/blob/main/zUMIs-dge2.R) 233–256,
  [`UMIstuffFUN.R`](https://github.com/sdparekh/zUMIs/blob/main/UMIstuffFUN.R) 230–248); the only RDS
  shipped in the repo is SCRB-seq and contains only `umicount`/`readcount`. Neither slot is documented
  in the wiki. **This should be confirmed on real output before a module depends on it.**
- **The exact field list of STARsolo's `Summary.csv` under `SmartSeq`** — not documented.
- **Whether `GeneFull` / `GeneFull_Ex50pAS` / `SJ` are validated under `SmartSeq`.** No source guard
  blocks them, but there is no positive confirmation and
  [#2057](https://github.com/alexdobin/STAR/issues/2057) is an open segfault at the `GeneFull`
  counting step in that mode.
- **umite's real-world maturity.** Peer-reviewed, but PyPI 0.1.1, one GitHub star, no independent
  validation. The Sandberg lab's public repos still point only at zUMIs.
</content>
