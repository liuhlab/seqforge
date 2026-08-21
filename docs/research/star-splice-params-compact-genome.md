# STAR's splice defaults on a 100 Mb genome: the intron ceiling is a chromosome, not 590 kb

Researched 2026-08-21 after a stack of reads in `day9_N2_9.ce11.unique.bam` (in-house *C. elegans*
Smart-seq3 aging plate, 784 worms, `ce11`, mapped by `map/star-umi` / `map/star-umi-chimera` against
the chimeric `ce11_ecHT115` reference) turned out to carry CIGARs like `12M94511N8M130S` — a 150 bp
read with 20 aligned bases, `MAPQ 255`, `NH:1`, `nM:0`, `AS:36`, mate at `chrII:4,035,578` (−),
`TLEN 94531`, flagged a proper pair. **No decision was taken here.** This records what STAR's defaults
are, what the worm's introns actually look like, what other pipelines set, what each counter does
with such a read, and what a change would cost — so that a record could be written against numbers
instead of against intuition.

**It since was, and the change shipped.** The census this fed is
[#459](https://github.com/liuhlab/seqforge/issues/459), the buildable plan
[#461](https://github.com/liuhlab/seqforge/issues/461); §7.1–§7.4 landed in
[#467](https://github.com/liuhlab/seqforge/pull/467), recorded in
[ADR-0056](../adr/0056-an-aligner-bound-is-a-genome-table-fact-copied-into-the-recipe.md) and in
`liulab-genome`'s ADR-0010; §8's proposed measurement was run
([`star-splice-flags-gate-2026-08-22.md`](star-splice-flags-gate-2026-08-22.md)). Tense and status
claims below are reconciled against those. **The reasoning is not** — the argument for each value is
unchanged by having been taken.

## Provenance of the numbers in this document — read before citing any of them

Three tiers, never mixed. **Tier 1 — verified here**: everything read from the STAR 2.7.11b source
tarball and `parametersDefault` (§1, §2, §5's `GeneCounts` block), every intron statistic computed
from Ensembl release 115 GTFs (§3.2 onward), the gene-overlap and anchor-annotation measurements in
§5.1, and all score and chance-match arithmetic. These were run during this investigation and can be
re-run from the commands and URLs given. **Tier 2 — sourced, not re-derived here**: the published
pipeline configurations in §4, the counter behaviours in §5 other than `GeneCounts`, the WS298/UCSC
statistics and WormBook/Lander quotations in §3, and §4.1. Each carries its URL and file path.

**Tier 3 — measured on the cluster by the coordinating session, not by this document's author.**
The read `12M94511N8M130S` with its tags and mate, the additional CIGARs in §2.2
(`19M56632N95M45N36M`, `14S21M840537N115M`, `1S9M840537N140M`, `9M266439N11M130S`,
`7M55501N12M260615N66M65S`), the plate-side annotation table in §3.1, the `N`-gap and anchor
distributions, the E. coli control, and the plate fate totals were **measured on `ircbc` `cpu02` on
2026-08-21**, inside the `align-rna` Singularity image via `aging_SS3/script/sfrun`, against
`seqforge/pipeline/ss3-784-chimera-c54b1418fa98/results`. This document's author had no cluster
access by instruction and did not re-run them; they are first-hand measurements of the coordinating
session rather than hearsay. The methods:

- CIGARs: `samtools view <cell>.ce11.unique.cram II:4035500-4035700`, and `-s 0.05` per-cell
  subsamples for the distributions.
- Intron statistics of the **installed** annotations: `awk` over the GTFs under
  `/share/lhqlab/liulab_data/genome/{ce11,sacCer3,mm39,hg38}`, taking per-transcript gaps between
  consecutive exons. The first pass of this one was **wrong**, and §3.3 is the diagnosis; §3.1 carries
  the corrected table and states plainly that the installed files agree with the canonical
  annotations after all.
- Plate fates: `anndata.read_h5ad` over `combined.{ce11,ecHT115}.h5ad`, summing the `obs` fate
  columns across all 784 cells.

**One correction carried from that session:** a first pass at the `N`-gap distributions compared gap
lengths lexicographically, because `awk`'s `substr` returns a string and the code omitted a `+0`
coercion. Every gap-length figure quoted here is from the corrected pass. The discarded pass put
`N>20kb` at 8–16% where the true figure is ~0.2%; a number in that range appearing anywhere is from
the bad pass and should be dropped.

**Anything resting on Tier 3 alone is marked in place, and §9 lists what further verification would
settle.** No conclusion in §7 depends on Tier 3 alone.

**Two figures landed after this was finalised, both Tier 3 in kind:** §3.1's corrected per-annotation
table, re-measured 2026-08-22 against the same installed files and quoted from #459's body, and §8's
gate result, which is [`star-splice-flags-gate-2026-08-22.md`](star-splice-flags-gate-2026-08-22.md)
and carries its own method and provenance — nothing of it is restated here beyond the headline.

## The one-sentence answer

**Nothing is broken and nothing is a bug: STAR's shipped defaults impose no intron-length check at
all — the widely quoted `(2^winBinNbits)*winAnchorDistNbins = 589,824 bp` is the maximum *step*
between an anchor and an existing window, not a ceiling on the window, so windows grow transitively
and the only hard bound is the chromosome — while the score charges *log₂* of genomic length (a
2–3 point difference between a 300 bp fragment and a megabase one) and nothing at all for a canonical
GT/AG motif.** On a genome whose median annotated intron is **66 bp**, that makes any anchor of ≥3–5
matched bases a net score *gain*, and the plate's real CIGARs — gaps to **1,049,334 bp** on a genome
whose longest annotated intron is 100,912 bp — are the aligner correctly maximising a function nobody
configured for this organism. All four of our STAR modules set no splice parameter at all when this
was written, and `umite/count.py` spans the fabricated gap contiguously, so one such read consumes
94.5 kb of chrII (21 gene bodies) and is filed `ambiguous`. **The fix is a short-anchor filter, not a
tight intron cap** (§7): the artifact's signature is a 9–13 bp anchor, and the annotation cannot tell
you where to put a length threshold because a GTF's maximum is a floor on biology, not a ceiling.
That is what shipped in #467, and the gate agreed with the ordering — the intron-free component's
spliced fraction collapsed while the two residual cells' longest gaps sat *under* the cap.

---

## 0. What the repo did when this was written — verified, not taken on trust

The first row is the one #467 changed; everything else in this section still holds.

| claim | verdict | where |
|---|---|---|
| all four STAR modules — `map/star`, `map/star-umi`, `map/star-umi-chimera`, `map/starsolo` — set **no** splice/intron parameter | **confirmed**, and reversed by #467 (§7) | [`star.smk:268`](../../src/seqforge/workflows/map/star.smk), [`star-umi.smk:533`](../../src/seqforge/workflows/map/star-umi.smk), [`star-umi-chimera.smk:620`](../../src/seqforge/workflows/map/star-umi-chimera.smk), [`starsolo_args.py`](../../src/seqforge/workflows/starsolo_args.py) |
| the plate twins pass a Tn5 3′ clip | **confirmed** — `--clip3pAdapterSeq CTGTCTCTTATACACATCT --clip3pAdapterMMp 0.1`, rendered per mate by `read_through_clip` (`star-umi.smk:229`) | ADR-0048, [`smartseq3-tn5-read-through.md`](smartseq3-tn5-read-through.md) |
| `_fragment_span()` returns a **contiguous** interval that does not excise `N` gaps | **confirmed** — `count.py:480`; for a proper pair it is `(start, max(end, start+TLEN))`, and `pysam`'s `reference_end` already spans `N` | [`umite/count.py`](../../src/seqforge/workflows/umite/count.py) |
| `_count_fragment()` calls `exonic(contig,start,end)` then `gene_bodies(...)`, filing >1 gene as `ambiguous` | **confirmed** — `count.py:646–684` | same |
| the aligner is pinned at STAR **2.7.11b** | confirmed — `pyproject.toml:167`, `star = "2.7.11b.*"` | — |

The full argv the plate twins ran at the time, with nothing elided:

```text
STAR --runMode alignReads --genomeDir … --runThreadN … --genomeLoad LoadAndKeep \
     --readFilesIn <ubam> --readFilesType SAM PE --readFilesCommand samtools view \
     --readFilesSAMattrKeep UB \
     --clip3pAdapterSeq CTGTCTCTTATACACATCT CTGTCTCTTATACACATCT --clip3pAdapterMMp 0.1 0.1 \
     --outFileNamePrefix … --outSAMtype BAM SortedByCoordinate \
     --outSAMattrRGline … --limitBAMsortRAM … --outSAMmultNmax 1 --outSAMunmapped Within
```

Every alignment-geometry decision in that line was a STAR default nobody chose. Since #467 the same
line carries `--outFilterType BySJout --alignSJoverhangMin 8 --alignSJDBoverhangMin 1
--alignIntronMax 50000 --alignMatesGapMax 50000` and `jM jI` on the attribute list, rendered from one
place ([`workflows/splice_args.py`](../../src/seqforge/workflows/splice_args.py)) for all four
modules.

One consequence worth naming immediately: **the plate's existing junction QC is structurally blind
to this artifact.** `qc.py:_summarise_sj` reads `SJ.out.tab`, and `SJ.out.tab` is already filtered by
`--outSJfilterOverhangMin` (`30 12 12 12`), so a junction with an 8 bp overhang never appears in it.
The BAM has the read; the QC summary cannot see it. `BySJout` closes exactly this by making the two
agree about which junctions exist (§7.1).

---

## 1. STAR's defaults, read from `parametersDefault` and from the code that derives them

All values verbatim from
[`source/parametersDefault`](https://github.com/alexdobin/STAR/blob/2.7.11b/source/parametersDefault)
at tag `2.7.11b` — the pinned build. Derivations read from the `2.7.11b` source tarball.

| parameter | default | what the file says |
|---|---|---|
| `alignIntronMin` | **21** | "genomic gap is considered intron if its length>=alignIntronMin, otherwise it is considered Deletion" |
| `alignIntronMax` | **0** | "if 0, max intron size will be determined by `(2^winBinNbits)*winAnchorDistNbins`" |
| `alignMatesGapMax` | **0** | "if 0, max intron gap will be determined by `(2^winBinNbits)*winAnchorDistNbins`" |
| `alignSJoverhangMin` | **5** | "minimum overhang (i.e. block size) for spliced alignments" |
| `alignSJDBoverhangMin` | **3** | "minimum overhang … for annotated (sjdb) spliced alignments" |
| `winBinNbits` | **16** | `=log2(winBin)`, the window/clustering bin size |
| `winAnchorDistNbins` | **9** | "max number of bins between two anchors that allows aggregation of anchors into one window" |
| `winAnchorMultimapNmax` | **50** | "max number of loci anchors are allowed to map to" |
| `winFlankNbins` | 4 | flank size per window, in bins |
| `alignWindowsPerReadNmax` | 10000 | max windows per read |
| `genomeChrBinNbits` | 18 | ceiling on `winBinNbits` |
| `scoreGap` | **0** | "splice junction penalty (independent on intron motif)" |
| `scoreGapNoncan` | **−8** | non-canonical junction penalty, *in addition to* `scoreGap` |
| `scoreGapGCAG` | **−4** | GC/AG and CT/GC penalty |
| `scoreGapATAC` | **−8** | AT/AC and GT/AT penalty |
| `scoreGenomicLengthLog2scale` | **−0.25** | "extra score logarithmically scaled with genomic length of the alignment: `scoreGenomicLengthLog2scale*log2(genomicLength)`" |
| `sjdbScore` | **+2** | "extra alignment score for alignments that cross database junctions" |
| `outSJfilterIntronMaxVsReadN` | **50000 100000 200000** | "junctions supported by 1 read can have gaps <=50000b, by 2 reads: <=100000b, by 3 reads: <=200000. by >=4 reads any gap <=alignIntronMax… **does not apply to annotated junctions**" |
| `outSJfilterOverhangMin` | **30 12 12 12** | min overhang per motif class (non-canonical, GT/AG, GC/AG, AT/AC) |
| `outSJfilterCountUniqueMin` | 3 1 1 1 | min unique reads per junction, per motif class |
| `outFilterScoreMinOverLread` | **0.66** | "normalized to read length (**sum of mates' lengths** for paired-end reads)" |
| `outFilterMatchNminOverLread` | **0.66** | same, on matched bases |
| `outFilterType` | Normal | `BySJout` is the two-stage alternative |
| `outFilterMultimapNmax` | 10 | |
| `outFilterMismatchNmax` | 10 | |
| `outFilterMismatchNoverLmax` | 0.3 | ratio to *mapped* length |
| `outFilterMismatchNoverReadLmax` | 1.0 | ratio to *read* length |
| `peOverlapNbasesMin` | **0** | ">0 switches on the merging of overlapping mates algorithm" |
| `outSAMattributes` | Standard | `Standard = NH HI AS nM`; `All = NH HI AS nM NM MD jM jI MC ch` |

### 1.1 `alignIntronMax 0` → 589,824 bp: **partially refuted.** It is a step, not a ceiling

The formula is real — `source/Genome_genomeLoad.cpp:382–405`, verbatim:

```cpp
if (P.alignIntronMax==0 && P.alignMatesGapMax==0) {
    P.inOut->logMain << "alignIntronMax=alignMatesGapMax=0, the max intron size will be approximately determined by (2^winBinNbits)*winAnchorDistNbins="
            << (1LLU<<P.winBinNbits)*P.winAnchorDistNbins <<endl;
} else {
    //redefine winBinNbits
    P.winBinNbits = (uint) floor( log2( max( max(4LLU,P.alignIntronMax), (P.alignMatesGapMax==0 ? 1000LLU : P.alignMatesGapMax) ) /4 ) + 0.5);
    P.winBinNbits = max( P.winBinNbits, (uint) floor(log2(nGenome/40000+1)+0.5) );
    //ISSUE - to be fixed in STAR3: if alignIntronMax>0 but alignMatesGapMax==0, winBinNbits will be defined by alignIntronMax
    …
};
…
if (P.alignIntronMax==0 && P.alignMatesGapMax==0) {
} else {
    P.winFlankNbins=max(P.alignIntronMax,P.alignMatesGapMax)/(1LLU<<P.winBinNbits)+1;
    P.winAnchorDistNbins=2*P.winFlankNbins;
};
```

2¹⁶ × 9 = **589,824**, and STAR's own log line hedges it: "**approximately** determined by". Four
things this code says that the number alone does not:

1. **There is no gap check at the default.** `P.alignIntronMax` is compared against a gap in exactly
   one place in the whole tree — `source/stitchAlignToTranscript.cpp:100`,
   `if (Del>P.alignIntronMax && P.alignIntronMax>0) return -1000003; //large gaps not allowed` — and
   that clause is dead when the value is 0. `alignMatesGapMax` is likewise enforced only when
   non-zero (`stitchAlignToTranscript.cpp:354`). At the default, what bounds an intron is purely
   emergent from windowing.
2. **The number does not move with genome size at the default.** The genome-size term
   (`nGenome/40000`) appears only in the `else` branch, i.e. only once you have *already* set
   `alignIntronMax` or `alignMatesGapMax` yourself. A 100 Mb worm index and a 3.1 Gb human index get
   the same emergent behaviour out of the box.
3. **Setting `alignIntronMax` alone silently changes the windowing, and does not cap the mate gap.**
   The `ISSUE` comment is STAR's own. Set both, or accept an emergent cap you did not choose.
4. **589,824 is not the maximum intron.** See §1.2 — this is the finding that matters most.

### 1.2 Why a megabase `N` gap is not a contradiction — windows grow transitively

Observed in the real plate BAM: gaps of `840537N`, and on a wider sweep a maximum of **1,049,334 bp**,
with 1,062,922 and 980,323 in other cells — every one far above 589,824. The source explains it
exactly. `source/ReadAlign_createExtendWindowsWithAlign.cpp` — the merge:

```cpp
uint aBin = (a1 >> P.winBinNbits); //align's bin
…
if (aBin>0) {//merge left only if there are bins on the left
    for (iBin=aBin-1;  iBin >= ( aBin>P.winAnchorDistNbins ? aBin-P.winAnchorDistNbins : 0 );  --iBin) {//go left, find windows in Anchor range
        if (wB[iBin]<uintWinBinMax) { flagMergeLeft=true; break; };
        …
    flagMergeLeft = flagMergeLeft && (mapGen.chrBin[iBin>>P.winBinChrNbits]==mapGen.chrBin[aBin>>P.winBinChrNbits]);
    if (flagMergeLeft) { iWin=wB[iBin]; iBinLeft=WC[iWin][WC_gStart]; … };
…
} else {//record windows after merging
    WC[iWin][WC_gStart]=iBinLeft;
    WC[iWin][WC_gEnd]=iBinRight;
```

Read it carefully: `winAnchorDistNbins` bounds how far a *new anchor* may reach to find an *existing*
window. On merging, the window's `[gStart, gEnd]` is **extended** to swallow the new bin. The next
anchor measures its 9 bins from the window's new edge. **Merging is transitive and the span is
unbounded** — a chain of anchors, each within 9 bins of the last, extends one window arbitrarily far.

The only hard bound is the chromosome: both merge branches are gated on
`mapGen.chrBin[iBin>>P.winBinChrNbits] == mapGen.chrBin[aBin>>P.winBinChrNbits]`, so a window never
crosses a contig boundary. Each surviving window is then padded by `winFlankNbins` on each side,
chromosome-clamped, in `ReadAlign_stitchPieces.cpp:100–110`. Aligns are assigned to whatever window
their bin belongs to (`ReadAlign_assignAlignToWindow.cpp:8`), and `stitchWindowAligns` stitches them
with, at the default, no gap check at all.

**So the effective ceiling on a novel intron at STAR's defaults is the length of the chromosome, not
589,824 bp.** `chrII` of `ce11` is ~15.3 Mb; the largest observed gap, 1,049,334 bp, is 6.9% of it.

Where the intermediate anchors come from is the other half, and it is specific to these reads. An
anchor is any seed piece mapping to ≤ `winAnchorMultimapNmax` = **50** loci
(`ReadAlign_stitchPieces.cpp:46`), and *every one of those up-to-50 suffix-array positions* calls
`createExtendWindowsWithAlign`. A read reduced by adapter clipping to ~20 informative bases yields
short seeds; a 12-mer has ~6 expected occurrences in 100 Mb and a 16-mer ~0.02, so pieces in the
9–16 bp range sit exactly in the band that clears the 50-locus anchor test while scattering across
the genome. **My arithmetic** (a reconstruction, not a measurement): 50 uniform loci on a 100 Mb
genome, same strand, give ~7 pairs within a single 589,824 bp merge step — so chains form routinely,
and a window spanning several hundred kb is the expected outcome rather than an exotic one.

Three sub-questions, answered:

- **Does the chimeric `ce11_ecHT115` reference loosen anything?** No. `nGenome` grows by ~4.6 Mb,
  which does not enter the bin arithmetic at all when `alignIntronMax` is 0, and the E. coli
  replicon is a separate entry in `chrBin`, so the chromosome clamp forbids a window spanning from
  worm to bacterium. If anything the chimera is *protective* at the boundary — and it supplies a free
  control (§6.4). **Checked rather than assumed:** `genomeParameters.txt` for both
  `ce11_ecHT115/index/star_wormbase_ws298+refseq_rs_2025_06_26` and `ce11/index/star_wormbase_ws298`
  records `--genomeChrBinNbits 18` (with `--genomeSAindexNbases 12 --sjdbOverhang 100`,
  versionGenome 2.7.4a). Since 18 > the default `winBinNbits` of 16, the clamp at
  `Genome_genomeLoad.cpp:393` does **not** fire and the 2¹⁶ × 9 arithmetic above holds unmodified.
- **Does `--limitSjdbInsertNsj` matter?** No. It bounds on-the-fly junction insertion, and none of
  the four modules passes `--sjdbGTFfile` or `--sjdbFileChrStartEnd` at *mapping* time — still true
  after #467. (It **was**
  passed at *index build*, so the annotation is in the sjdb — which is what makes §1.5's annotated
  exemption real.)
- **Could the sjdb path explain the megabase gaps?** No. `sjAlignSplit()` reproduces whatever
  junction was inserted at index build, and the longest annotated *C. elegans* intron is 100,912 bp
  (§3). A 1,049,334 bp gap cannot come from the annotation; it is novel, found by general stitching.

**The observed gap distribution is itself evidence for this model, and it is a falsifiable test.**
Two models predict different shapes:

| | hard-ceiling model (the manual's reading) | transitive-merge model (§1.2) |
|---|---|---|
| max gap | ≤ 589,824, with a **hard edge** at that value | unbounded below the contig, decaying smoothly |
| shape near 589,824 | pile-up just under it, nothing above | nothing special happens there |
| max gap vs contig length | irrelevant | well short of it — chaining needs anchors, which thin out |

**The data has no edge at 589,824 and runs to ~1.05 Mb, i.e. 1.8× the single-step reach, while
staying far below chrII's 15.3 Mb and with nothing above 2 Mb.** That is the transitive-merge
prediction and not the hard-ceiling one. The sharper version of the test, still unrun: **plot the
`N`-gap histogram on a log axis and look for a discontinuity at exactly 589,824.** A hard ceiling
must produce one; transitive merging cannot. A second discriminator: under the transitive model the
gap length should correlate with the number of short/multimapping seeds a read supplies, so
**restricting to reads with ≥100 aligned bases (few spare seeds) should shorten the tail**, while a
hard ceiling predicts no such dependence.

**The practical consequence is the opposite of reassuring.** The commonly cited 589,824 understates
the default's reach by nearly 2× in practice, and the case for setting `--alignIntronMax` explicitly
is therefore stronger than the manual implies: it is the only value in the system that produces a
*hard* check.

### 1.3 `--outFilterScoreMinOverLread` / `MatchNminOverLread` normalise to the sum of *clipped* mate lengths: **confirmed**

Two files settle both halves. `source/ReadAlign_oneRead.cpp:36` — the denominator for a pair:

```cpp
if (P.readNmates==2) {//combine two mates together
    Lread=readLength[0]+readLength[1]+1;
    readLengthPairOriginal=readLengthOriginal[0]+readLengthOriginal[1]+1;
```

`source/ReadAlign_mappedFilter.cpp:8` — where it is used:

```cpp
} else if ( (trBest->maxScore < P.outFilterScoreMin) || (trBest->maxScore < (intScore) (P.outFilterScoreMinOverLread*(Lread-1))) \
          || (trBest->nMatch < P.outFilterMatchNmin)  || (trBest->nMatch < (uint) (P.outFilterMatchNminOverLread*(Lread-1))) ) {//too short
```

so the threshold is `0.66 × (readLength[0] + readLength[1])`. And `readLength[im]` is the **post-clip**
length: `readLoad.cpp` sets `Lread=readInStream.gcount()-1`, saves `LreadOriginal=Lread`, then calls
`clipOneMate[0].clip(Lread, SeqNum)` / `clipOneMate[1].clip(...)`, and `ClipMate::clip`
(`source/ClipMate_clip.cpp`) does `Lread -= clippedAdN;` for the 3′ adapter. **Adapter-clipped bases
leave the denominator.** That is precisely the mechanism ADR-0048 rests on, now read off the source
rather than inferred from the +21-point measurement.

Ordinary soft-clipping does **not** leave it: the denominator is a property of the *read*, computed
before any alignment exists, so an alignment that soft-clips 130 bases of a 150 bp read still faces a
threshold built from 150. The two populations are nevertheless indistinguishable in the CIGAR —
`ReadAlign_outputTranscriptSAM.cpp:145,185` builds the leading and trailing `S` from
`clipMates[…].clippedN` **plus** `readLengthOriginal`, so adapter-clipped bases are reported as soft
clip. *A CIGAR's `S` count mixes bases STAR charged the filter for with bases it did not.*

### 1.4 What binds and what does not, for the observed read

| gate | default | `12M94511N8M130S` | passes? |
|---|---|---|---|
| gap ≤ `alignIntronMax` | dead at `0` | 94,511 | n/a — never checked |
| gap within a window | chromosome-bounded (§1.2) | 94,511 | yes |
| donor overhang ≥ `alignSJoverhangMin + shiftSJ` | 5 (+repeat) | 12 | yes |
| acceptor overhang ≥ same | 5 (+repeat) | 8 | **yes** — and would still pass at ENCODE's 8 if the repeat shift is 0 |
| junction reaches `SJ.out.tab` | overhang ≥ 12 (GT/AG) or ≥ 30 (non-canonical) | 8 | **no** |
| gap ≤ `outSJfilterIntronMaxVsReadN[0]` for a 1-read junction | 50,000 | 94,511 | **no** |
| alignment survives `outFilterType` | `Normal` — the two rows above are *output* filters only | — | **yes, it is written to the BAM** |

The last row is the crux. Under `--outFilterType Normal` the `outSJfilter*` family only decides what
enters `SJ.out.tab`; it has no say over the BAM. Under `BySJout`, `stitchWindowAligns.cpp:169`
turns it into an alignment filter:

```cpp
if (P.outFilterBySJoutStage==2) {//junctions have to be present in the filtered set P.sjnovel
    for (uint iex=0;iex<trA.nExons-1;iex++) {
        if (trA.canonSJ[iex]>=0 && trA.sjAnnot[iex]==0) {
            …
            if ( binarySearch2(jS,jE,P.sjNovelStart,P.sjNovelEnd,P.sjNovelN) < 0 ) return;
```

and the filter it consults is `outputSJ.cpp:62–65`:

```cpp
sjFilter=*oneSJ.annot>0 \
        || ( ( *oneSJ.countUnique>=…outSJfilterCountUniqueMin… || …outSJfilterCountTotalMin… )
          && *oneSJ.overhangLeft  >= …outSJfilterOverhangMin…
          && *oneSJ.overhangRight >= …outSJfilterOverhangMin…
          && ( (…countTotal…)>P.outSJfilterIntronMaxVsReadN.size() || *oneSJ.gap<=…outSJfilterIntronMaxVsReadN[countTotal-1] ) );
```

`*oneSJ.annot>0` short-circuits the whole thing, so **annotated junctions are exempt from every one
of those filters.** The observed junction fails two of them.

### 1.5 `--alignIntronMax` caps novel junctions, not annotated ones — mostly

`stitchAlignToTranscript.cpp:18` opens with a branch labelled *"simple stitching if junction belongs
to a database"*, which sets `sjAnnot=1`, adds `sjdbScore`, and returns **before** the
`Del>P.alignIntronMax` check in the general-stitching branch below it. Reads crossing an annotated
junction are found by aligning to the sjdb inserted sequences and split by `sjAlignSplit()`
(`source/sjAlignSplit.cpp`), which consults no length limit either. So a cap does not cost you
annotated long introns for reads that seed on the inserted sequence.

**The exemption is partial and should not be overstated.** A read whose two anchors are seeded
genomically and stitched through the *general* path hits the cap at line 100 before the code that
would have recognised the junction as annotated (lines 198–231). A long annotated intron is therefore
reliably recoverable, but not unconditionally so. This is unmeasured directly; what bounds it is the
gate's Gate 2, where the whole change cost the worm component **−0.038%** of counted UMIs at worst
(§8), so whatever this case costs, it is inside that.

---

## 2. The score arithmetic — why STAR *prefers* the phantom junction

The score function is `Transcript::alignScore` (`source/Transcript_alignScore.cpp`), and the same
arithmetic appears in `stitchWindowAligns.cpp:221`:

```text
AS = (+1 per matching base, −1 per mismatch, summed over BOTH mates)
   + Σ junction terms:  annotated → +sjdbScore(+2)
                        GT/AG     → +scoreGap(0)
                        GC/AG     → scoreGapGCAG(−4) + scoreGap
                        AT/AC     → scoreGapATAC(−8) + scoreGap
                        noncanon  → scoreGapNoncan(−8) + scoreGap
   + ceil( log2(genomicLength) * scoreGenomicLengthLog2scale − 0.5 )      # genomicLength = last exon end − first exon start, over the whole pair
   , floored at 0
```

### 2.1 The mechanism in one table

The genomic-length term is the whole story, because *log₂* barely moves across five orders of
magnitude:

| genomic length | length score |
|---|---|
| 150 bp | −2 |
| 300 bp | −2 |
| 1,000 bp | −2 |
| 94,531 bp | **−4** |
| 266,459 bp | **−5** |
| 840,686 bp | **−5** |

**Flinging an anchor a megabase downstream costs at most 3 points more than keeping the alignment
compact, while the anchor is worth its full length in matched bases.** Break-even for an *n*-base
anchor is therefore roughly *n* > 3 under GT/AG (penalty 0), *n* > 7 under GC/AG (−4), and *n* > 11
under a non-canonical motif (−8). STAR's `alignSJoverhangMin` default of **5** sits almost exactly on
the canonical break-even — which is why the default admits precisely this class of alignment.

### 2.2 The plate's real CIGARs, scored

Reconstruction from the formula above, showing the score *with* the phantom gap against the score of
the same read with the short anchor dropped (i.e. soft-clipped instead). Motif is unknown for every
one of these — `jM` was not in the BAM these were read from — so all three canonical classes are
shown.

| CIGAR | anchor | gap(s) | Δ score if GT/AG | if GC/AG | if non-canonical |
|---|---|---|---|---|---|
| `19M56632N95M45N36M` | 19 bp | 56,632 (+ a plausible real 45) | **+17** | +13 | +9 |
| `14S21M840537N115M` | 21 bp | 840,537 | **+18** | +14 | +10 |
| `7M55501N12M260615N66M65S` | 7 + 12 bp | 55,501 and 260,615 | **+16** | +12 | +8 |
| `1S9M840537N140M` | 9 bp | 840,537 | **+6** | +2 | −2 |
| `9M266439N11M130S` | 9/11 bp | 266,439 | **+5** | +1 | −3 |
| `12M94511N8M130S` | 12/8 bp | 94,511 | **+5** | +1 | −3 |

Two readings fall out. **The top three win under every motif class** — `19M56632N95M45N36M` is a
perfectly good 131-base alignment that STAR *improves* by 17 points by bolting a 19 bp anchor 56 kb
upstream, and no motif penalty is large enough to stop it. **The bottom three win only if the motif
is canonical or semi-canonical**, which means STAR's junction-shift search (`stitchAlignToTranscript`
scans shift positions for the best motif) is doing real work to find a `GT…AG` frame — and, given
~9 chance 8-mer matches per 589,824 bp of window, it usually can.

Chance-match arithmetic, my computation, from window size and 4ⁿ:

| anchor length | expected exact matches in 589,824 bp | in a 25,000 bp cap |
|---|---|---|
| 8 bp | **9.0** | 0.38 |
| 12 bp | 0.035 | 0.0015 |
| 20 bp | 5.4 × 10⁻⁷ | — |

### 2.3 `AS:i:36` on the IGV read, worked

`genomicLength ≈ 94,531` (the pair spans 4,035,578 → 4,130,109), so
`ceil(log2(94531) × −0.25 − 0.5) = ceil(−4.632) = −4`. `nM:i:0` and `NM:i:0` say there are no
mismatches anywhere in the pair, so `nMatch = AS − J + 4 = 40 − J`:

| junction class | J | implied total matched bases | implied aligned bases on the mate (this mate contributes 12+8=20) |
|---|---|---|---|
| annotated (`jM ≥ 20`) | +2 | 38 | 18 |
| **novel GT/AG** (`jM 1/2`) | **0** | **40** | **20** |
| novel GC/AG (`jM 3/4`) | −4 | 44 | 24 |
| non-canonical or AT/AC | −8 | 48 | 28 |

The GT/AG row — mate contributing 20 aligned bases, mirroring this mate's 20 — is the one consistent
with the mirrored-CIGAR signature in §6.3. **Flagged as a reconstruction:** `AS` alone cannot choose
among these rows, and that ambiguity is itself an argument for §7.4.

One further consequence holds whichever row is true: **the read survived
`outFilterScoreMinOverLread` only because most of it was adapter.** `36 ≥ 0.66 × (Lread−1)` forces
`readLength[0] + readLength[1] ≤ 54`. For a 2×150 bp pair the unclipped denominator would have been
300 and the threshold 198 — the alignment would have been discarded as *too short* and never written.
The artifact is a **direct descendant of the read-through this repo already measured** (ADR-0048),
not an independent pathology.

---

## 3. Worm intron lengths — measured independently, and one measurement withdrawn

Measurements were made independently, by different people, with different tools, against different
files. One of them was wrong, and §3.3 — how that was caught and what caused it — is the most useful
thing in this section.

### 3.1 The plate-side measurement — WITHDRAWN, and the pass that replaced it

An `awk` pass over the GTFs installed at `/share/lhqlab/liulab_data/genome` on `ircbc` produced a
per-annotation intron table for ce11/WS298, sacCer3, mm39 and hg38. **Every figure in it was
plus-strand only, and not one of them is printed anywhere in this document.** The pass streamed exon
lines in file order and recorded a gap only when the next exon's start exceeded the previous exon's
end; WormBase and GENCODE list exons in **transcription** order, so for every minus-strand transcript
the differences were negative and silently recorded nothing. §3.3 is the diagnosis, and it is the
most transferable finding here.

**The corrected pass**, sorting each transcript's exons by coordinate before differencing, is
#459's table, re-measured 2026-08-22 against the same installed files:

| genome | annotation | n introns | median | p99 | p99.99 | **longest** |
|---|---|---|---|---|---|---|
| **ce11** | WormBase WS298 | 204,769 | **79** | 4,450 | 19,396 | **100,912** |
| **sacCer3** | ensGene v101 | 380 | 116 | 2,483 | 2,483 | **2,483** |
| mm39 | GENCODE vM39 | 1,836,308 | 1,390 | 67,330 | 358,705 | 2,908,816 |
| hg38 | GENCODE v50 | 2,775,379 | 1,624 | 81,163 | 388,136 | 1,240,120 |

Per-transcript, so comparable to §3.2's per-transcript column and not to its unique-intron one; the
chimeric `ce11_ecHT115` GTF actually used for the plate gives identical ce11 figures. **The installed
files agree with the canonical annotations** — 100,912 either way, p99.99 at 19,396 either way —
which is the other half of §3.3's finding: the disagreement was in the pass, never in the file. §3.2's
Ensembl figures carried the argument meanwhile, and nothing in §7 moved when this landed.

The shape the withdrawn table got right qualitatively, and the corrected one confirms: STAR's ~590 kb
per-step budget is **~6× the longest annotated worm intron** and **two orders of magnitude beyond the
longest yeast intron**, while for human and mouse it sits just about at the top of the real
distribution. **The default is mammal-calibrated**, and seqforge overrode it for nothing until #467.

### 3.2 Independent checks against Ensembl, WS298 itself, and UCSC

**Method (run locally 2026-08-21, no cluster):** downloaded
`Caenorhabditis_elegans.WBcel235.115.gtf.gz`, `Homo_sapiens.GRCh38.115.gtf.gz` and
`Mus_musculus.GRCm39.115.gtf.gz` from
[`ftp.ensembl.org/pub/release-115/gtf/`](https://ftp.ensembl.org/pub/release-115/) (WBcel235 **is**
ce11 — the file header reads `#!genome-build WBcel235`, `#!genome-build-accession GCA_000002985.3`).
Sorted each transcript's exons, took every gap between consecutive exons as one intron,
`length = next.start − prev.end − 1`. "Unique" collapses identical `(chrom, strand, donor, acceptor)`
tuples across transcripts.

**A correction worth recording, because it changes what is checkable.** An earlier draft of this
document said WS298 could not be re-fetched because `downloads.wormbase.org` returns HTTP 403. That
is true of that host and **irrelevant**: WormBase releases are mirrored at EBI, and
[`ftp.ebi.ac.uk/pub/databases/wormbase/releases/WS298/species/c_elegans/PRJNA13758/c_elegans.PRJNA13758.WS298.canonical_geneset.gtf.gz`](https://ftp.ebi.ac.uk/pub/databases/wormbase/releases/WS298/)
downloads without incident. A third measurement was therefore made against **WS298 itself**, plus
[UCSC's `ce11.ncbiRefSeq`](https://hgdownload.soe.ucsc.edu/goldenPath/ce11/bigZips/genes/ce11.ncbiRefSeq.gtf.gz),
so four annotations of one assembly are now in evidence:

| annotation | n (unique) | mode | median | mean | p99 | p99.9 | max |
|---|---|---|---|---|---|---|---|
| WormBase **WS298**, all biotypes | 116,480 | 47 | 66 | 352.9 | 3,939 | 11,092 | 100,912 |
| WormBase **WS298**, CDS introns only | 105,857 | 47 | 66 | 343.5 | 3,655 | 10,525 | 100,912 |
| Ensembl **116** (WBcel235.63) | 116,403 | 47 | 66 | 352.7 | 3,944 | 11,092 | 100,912 |
| Ensembl **115**, computed here | 116,403 | 47 | 66 | 352.7 | 3,944 | 11,092 | 100,912 |
| UCSC **ce11 NCBI RefSeq** | 107,922 | 47 | 67 | 340.8 | 3,572 | 10,404 | 100,912 |

Four annotations built by three groups agree on mode **47**, median **66–67**, mean **341–353** and
max **100,912**.

| statistic | *C. elegans* WBcel235.115, unique | per-transcript | mouse GRCm39.115 | human GRCh38.115 |
|---|---|---|---|---|
| n | 116,403 | 203,116 | 412,783 | 621,899 |
| **median** | **66** | 79 | 1,731 | 2,400 |
| mean | 353 | 391 | 7,023 | 10,421 |
| p99 | 3,944 | 4,438 | 81,472 | 117,372 |
| p99.9 | 11,092 | 11,317 | 229,335 | 279,204 |
| p99.99 | 19,927 | **19,396** | 558,942 | 526,174 |
| **max** | **100,912** | 100,912 | 2,908,816 | 1,240,120 |
| > 10 kb | 157 (0.13%) | 305 (0.15%) | 15.7% | 22.5% |
| > 25 kb | **2 (0.0017%)** | **3** | 6.5% | 10.7% |
| > 50 kb | **2** | 3 | 2.5% | 4.8% |

**The tail counts are the numbers §7.3 needs**, computed here from Ensembl 115 and reproduced
independently from WS298 — identical in every row:

| threshold | unique introns above it | share |
|---|---|---|
| > 10,000 | **157** | 0.135% |
| > 15,000 | **34** | 0.029% |
| > 20,000 | **11** | 0.009% |
| > 25,000 | **2** | 0.0017% |
| > 50,000 | 2 | 0.0017% |
| > 100,000 | 1 | 0.0009% |
| > 589,824 | **0** | — |

56.35% of unique introns are under 100 bp (computed here); 56.5% from WS298 (independent reader).

### 3.3 A disagreement, and the bug behind it — the most transferable finding in this document

For a while two measurements disagreed about the longest *C. elegans* intron. An ad-hoc `awk` pass
over the installed GTF said **86,515 bp** (*cox-6B*, `III:2,699,685–2,786,199`); every canonical
annotation said **100,912 bp**, an intron in ***nhr-27*** (`X:12,707,304–12,808,215`, **− strand**,
WBGene00008901, transcripts `F16H9.2a.1` / `F16H9.2b.1`). WormBase's curators state the latter
verbatim:

> "The largest confirmed intron is 100,912 bp, found in *nhr-27* (F16H9.2b)."
> — Spieth, Lawson, Davis, Williams & Howe, ["Overview of gene structure in *C. elegans*"](https://www.ncbi.nlm.nih.gov/books/NBK19701/), WormBook (2014)

**The canonical source was right and the ad-hoc measurement was wrong.** *nhr-27* is present in the
installed file, unaltered; nothing was filtered out. The cause was **exon ordering**: WormBase lists a
transcript's exons in **transcription** order, so a minus-strand transcript's exons *descend* in
coordinate. A pass that streams exon lines in file order and records a gap only when the next start
exceeds the previous end differences them the wrong way round and silently records **nothing** — for
every minus-strand transcript in the genome. 86,515 was therefore not the worm maximum and not a
property of the installed file; it was the maximum over **plus-strand transcripts only**.

Confirmed directly against WS298 as published (EBI mirror), which also rules out the explanations
that looked plausible before the cause was found — all three *nhr-27* transcripts carry
`gene_biotype "protein_coding"` **and** `transcript_biotype "protein_coding"`, so no biotype filter
separates them, and `F16H9.2b.1`'s long intron is flanked by `CDS` on both sides, so it survives a
CDS-only conversion too:

```text
X  transcript  12705624 12808318  -  transcript_id "F16H9.2a.1"; transcript_biotype "protein_coding";
X  exon        12808216 12808318  -  exon_number "1"      <-- 5' exon
X  exon        12707218 12707303  -  exon_number "2"      <-- next exon, 100,912 bp downstream in coordinate
```

**Why this is the most transferable thing here.** §7.3 considers and then retracts *deriving* the
intron cap from the registered GTF rather than hard-coding an organism constant — a retraction both
records that shipped went on to state as a decision (ADR-0056; `liulab-genome`'s ADR-0010). Deriving
a cap means differencing exon
coordinates — which is exactly the operation that just failed, silently, in a way that produced a
plausible-looking number 14% below the truth and no error at all. A derived cap must therefore sort
exons per transcript before differencing, be validated against a curated value for at least one
organism, carry a margin, and record which file it was computed from. `max()` over a GTF is not a
safe primitive.

Two consolations. First, the corrected worm maximum makes the cap question *easier*, not harder:
100,912 is further from any plausible cap than 86,515 was. Second — and this is what defuses the
question entirely — **recounting the artifact population against the curated 100,912 threshold
instead of 86,515 barely moves it: 0.155–1.287% of aligned reads per cell versus 0.163–1.369%.** No
conclusion in this document depends on which figure is used.

The rankings, worth carrying because the entries below rank 1 are fragile:

| rank | length | gene | status |
|---|---|---|---|
| 1 | **100,912** | *nhr-27* | CDS intron; present in WS298, Ensembl 115/116, UCSC RefSeq and the installed file; curator-confirmed |
| 2 | 86,515 | *cox-6B* | **5′UTR** intron in a single transcript (`Y71H2AM.5.2`) whose first exon is **22 bp** — see §6.1 |
| 3 | 23,952 | *str-81* | ncRNA isoform `T26H2.6b` |
| 4 | 23,892 | *unc-103* | CDS intron; post-dates WS237 |
| 5 | 21,230 | *kin-1* | CDS intron — the longest WormBook knew about at WS237 |

Between rank 2 and rank 3 there is a **3.6× gap**. Between 25 kb and 86 kb the worm genome has
nothing at all.

### 3.4 The short end, because `alignIntronMin` is also a default nobody chose

Modal worm intron length is **47 bp** (9,814 occurrences per transcript in Ensembl 115; 5,884 unique),
with 46/48/49/45 next; **44.9%** of all transcript introns are 40–60 bp. Only **0.096%** are shorter
than STAR's `alignIntronMin` default of 21, and 0.104% shorter than WormBase's own 25 — so this
parameter is very nearly inert on worm and is not where the problem is. It matters only in that gaps
below it are called `D` instead of `N`.

A caveat on that low tail: WS298 contains ~400 unique introns under 40 bp, including some of 1 bp,
which are annotation errors — WormBook records only **three** natural introns ≤ 25 bp (*xbp-1* at
23 bp; `K08E5.6` and `E04A4.3` at 25 bp). Nothing here filtered them; they do not move the median or
any percentile.

### 3.5 Cross-checks against the literature, and one piece of folklore that has no source

Every computed figure above has a published counterpart, and they match:

| computed here / by an independent reader | published | source |
|---|---|---|
| mode 47 bp, median 66 bp (WS298 CDS introns, n = 105,857) | *"There are 108,151 (WS237) unique introns … The most common size of CDS introns is **47 bp** with the median size being **65 bp**"* | [Spieth et al., WormBook (2014)](https://www.ncbi.nlm.nih.gov/books/NBK19701/) |
| 56.35% of unique introns under 100 bp | *"**56%** of C. elegans introns are under 100 nt in length"* | [Zahler, WormBook (2012)](https://www.ncbi.nlm.nih.gov/books/NBK116073/) |
| max 100,912 bp in *nhr-27* | *"The largest confirmed intron is **100,912 bp**, found in nhr-27"* | Spieth et al., as above |
| human mode 88 bp | *"a peak at **87 bp**"* | [Lander et al., *Nature* 409:860 (2001)](https://www.nature.com/articles/35057062.pdf) |
| human max 1,160,411 bp in *ROBO2* (excluding readthrough models) | 1,160,411 bp for ROBO2 | [Piovesan et al. (2019)](https://pmc.ncbi.nlm.nih.gov/articles/PMC6549324/) |

Sixty-one WormBase releases apart, the mode and median agree to 1 bp. That is the strongest evidence
in this section that the method is sound.

**The "average C. elegans intron is ~320 bp" has no primary source.** A targeted search of the
literature for 317–322 bp returns nothing. What does exist:

- **267 bp** — the mean, verbatim from Lander et al. 2001, p. 896: *"most introns near the preferred
  minimum intron length (**47 bp for worm**…) and an extended tail (**overall average length of
  267 bp for worm**…). Intron size is much more variable in humans, with a peak at 87 bp but a very
  long tail resulting in a mean of more than 3,300 bp."*
- **344 nt** — the mean over WS190 confirmed transcripts,
  [Bradnam & Korf (2008)](https://pmc.ncbi.nlm.nih.gov/articles/PMC2518113/). The same paper reports
  **318 nt** for the mean *first* intron of operon-downstream genes — a narrow subset, and a likely
  source of the garble.
- **~300 bp** — but for **outrons**, not introns:
  [Blumenthal, WormBook](https://www.ncbi.nlm.nih.gov/books/NBK19704/) gives *"outrons range from
  60–500 bp, with many around 300 bp"*. An outron is the intron-*like* leader 5′ of the SL1 acceptor
  and is removed by **trans**-splicing (§6.1). This is the most plausible origin of the folklore, and
  it is a different object entirely.
- The 1998 *Science* consortium paper appears to report no intron length at all; its companion review
  gives only *"an average of five introns"* per gene. Anyone citing it for an average intron length
  is citing it wrongly.

**Use the median.** Mean 353 against median 66 against mode 47: the mean is dragged more than 5× the
median by a tail that is 0.13% of the data, which is exactly why WormBase's curators publish mode and
median and pointedly decline to publish a mean.

**Ratio to remember: the worm's median annotated unique intron is 66 bp against human's 2,400 —
36× smaller — while STAR's default reach is the same for both, and is in practice unbounded below
the chromosome (§1.2).**

---

## 4. What published pipelines actually set

Read from the actual config/source files, not from documentation about them.

| pipeline | source | `alignIntronMin` | `alignIntronMax` | `alignMatesGapMax` | `alignSJoverhangMin` | `alignSJDBoverhangMin` | other relevant | organism-scaled? |
|---|---|---|---|---|---|---|---|---|
| **STAR default** | [`parametersDefault`](https://github.com/alexdobin/STAR/blob/2.7.11b/source/parametersDefault) | 21 | 0 → unbounded below the chromosome | 0 → same | 5 | 3 | `outFilterType Normal` | **no** |
| **WormBase, *C. elegans*** | [`scripts/Modules/RNASeq.pm`](https://github.com/WormBase/wormbase-pipeline/blob/master/scripts/Modules/RNASeq.pm) L3445 | **25** | **15,000** | **50,000** | default | default | `outFilterMultimapNmax 2`, `outFilterMismatchNoverLmax 0.02`, `chimSegmentMin 15`, `outSAMstrandField intronMotif` | **yes** |
| ENCODE long-RNA (DCC) | [`ENCODE-DCC/rna-seq-pipeline/src/align.py`](https://github.com/ENCODE-DCC/rna-seq-pipeline/blob/dev/src/align.py) L152–174 | 20 | 1,000,000 | 1,000,000 | 8 | 1 | `outFilterType BySJout`, `outFilterMultimapNmax 20`, `outFilterMismatchNmax 999`, `outFilterMismatchNoverReadLmax 0.04`, `sjdbScore 1` | no |
| ENCODE long-RNA (original shell) | [`long-rna-seq-pipeline/DAC/STAR_RSEM.sh`](https://github.com/ENCODE-DCC/long-rna-seq-pipeline/blob/master/DAC/STAR_RSEM.sh) L33–36 | 20 | 1,000,000 | 1,000,000 | 8 | 1 | + `outSAMattributes NH HI AS NM MD` | no |
| STAR manual §3.3.2 "ENCODE options" | `doc/STARmanual.pdf`, shipped with 2.7.11b | 20 | 1,000,000 | 1,000,000 | 8 | 1 | `outFilterType BySJout` — "reduces the number of ''spurious'' junctions" | no |
| RSEM `--star` | [`rsem-calculate-expression`](https://github.com/deweylab/RSEM/blob/master/rsem-calculate-expression) L462–472 | 20 | 1,000,000 | 1,000,000 | 8 | 1 | hardcoded; doc: "from ENCODE3's STAR-RSEM pipeline" | no |
| nf-core/rnaseq, **default** `star_salmon` | [`conf/modules/align_star.config`](https://github.com/nf-core/rnaseq/blob/master/conf/modules/align_star.config) | — | **not set** | not set | not set | 1 | `twopassMode Basic`, `outFilterMultimapNmax 20`, `outSAMstrandField intronMotif` | no |
| nf-core/rnaseq, `star_rsem` | same file | 20 | 1,000,000 | 1,000,000 | 8 | 1 | `outFilterType BySJout` | no |
| nf-core/rnaseq, `--prokaryotic` | same file | — | **1** | — | — | — | `sjdbGTFfeatureExon CDS` | binary switch only |
| **zUMIs** | [`zUMIs-mapping.R`](https://github.com/sdparekh/zUMIs/blob/main/zUMIs-mapping.R) L110–119 | — | **not set** | not set | not set | not set | `outSAMmultNmax 1`, `outFilterMultimapNmax 50`, `outSAMunmapped Within`, `twopassMode Basic`; `additional_STAR_params` is the passthrough | no |
| **Smart-seq3 authors** (Hagemann-Jensen) | [`sandberg-lab/Smart-seq3/allele_level_expression/mouse_cross.yaml`](https://github.com/sandberg-lab/Smart-seq3/blob/master/allele_level_expression/mouse_cross.yaml) | — | **not set** | not set | not set | not set | `additional_STAR_params: '--limitSjdbInsertNsj 2000000 --clip3pAdapterSeq CTGTCTCTTATACACATCT'`; `twoPass: no` | no |
| WormBase **ParaSite** (other nematodes) | [`parasite/scripts/production/rnaseq/dependencies.py`](https://github.com/WormBase/wormbase-pipeline/blob/master/parasite/scripts/production/rnaseq/dependencies.py) | — | not set | not set | not set | not set | `quantMode GeneCounts`, `sjdbOverhang 100` | no |
| nf-core/scrnaseq (STARsolo) | [`conf/modules.config`](https://github.com/nf-core/scrnaseq/blob/master/conf/modules.config) | — | not set | not set | not set | not set | `twopassMode Basic` | no |
| **ArcInstitute/scRecounter** | [`workflows/star_full.nf`](https://github.com/ArcInstitute/scRecounter/blob/main/workflows/star_full.nf) L131–151 | — | **not set** | not set | not set | not set | `soloType CB_UMI_Simple`, `clipAdapterType CellRanger4`, **`outFilterScoreMin 30`**, `soloFeatures Gene GeneFull GeneFull_ExonOverIntron GeneFull_Ex50pAS Velocyto`, `soloMultiMappers EM Uniform`, `outSAMtype None` | **no — one argv for 26 genomes** |

**Does anyone scale `alignIntronMax` to the genome?** Not as a computed function — nowhere, in any
pipeline read. What exists is a **per-organism constant chosen by whoever curates that organism's
data**, and for *C. elegans* the authoritative one is WormBase's own production pipeline:

```perl
my $options = "--alignIntronMin 25 --outReadsUnmapped Fastx --alignIntronMax 15000 --alignMatesGapMax 50000 --outFilterMultimapNmax 2 --outFilterMismatchNoverLmax 0.02 --chimSegmentMin 15 --chimJunctionOverhangMin 15";
```

The same file carries a pasted "Comments about STAR" block attributed to Alex Dobin, the closest
thing to a primary-source rationale for doing this at all: *"If your species does not have long
introns, you can make STAR (newest version) filter them out with `--alignIntronMax` and
`--alignMatesGapMax` options."*

Six things worth carrying forward:

- **The one pipeline that explicitly spans many organisms does not scale it either.** scRecounter
  runs one hard-coded argv across 26 genomes from nematode to maize (§4.1). "Nobody scales it" is not
  an artefact of having surveyed only mammal pipelines.
- **The Smart-seq3 authors ran mouse at STAR's untouched default.** Their deposited YAML sets exactly
  two extra STAR flags, and neither is about introns. There is no Smart-seq3 precedent to inherit —
  the chemistry's reference pipeline never asked the question, and on mouse the default is roughly
  right anyway (§3.2).
- **zUMIs sets no intron limit either**, and exposes `additional_STAR_params` as the escape hatch.
  It *does* measure genome size, but only to decide how many STAR instances fit in RAM.
- **ENCODE's 1,000,000 is scoped to human and mouse** by its own data standards. Applying it to worm
  would be ~67× WormBase's figure; against the *documented* 590 kb it is looser still, and against
  the *real* default behaviour (§1.2) it is the first hard bound the pipeline would have. The ENCODE
  set is the wrong number for this genome even though `alignIntronMax 1000000` is not worthless —
  it at least turns the check on.
- **nf-core's prose contradicts its config.** `docs/usage.md` claims the ENCODE options "are already
  set as pipeline defaults"; on the default `star_salmon` path they are not.
- No worm-specific STAR configuration exists anywhere in `nf-core/configs` (0 hits for
  `alignIntronMax`). modERN is ChIP-seq; modENCODE worm RNA-seq appears to have been processed
  *through* WormBase's pipeline, i.e. at 15,000.

### 4.1 scRecounter (Arc Institute) — the closest comparator, and it does not scale anything

[`ArcInstitute/scRecounter`](https://github.com/ArcInstitute/scRecounter) recounts public scRNA-seq at
scale into scBaseCount, which makes it the nearest published analogue to what seqforge is for. It is
worth reading precisely *because* nobody is claiming it is correct — the question is what a
serious large-scale recounting pipeline actually does.

**It builds one hard-coded STARsolo command line, inline in the Nextflow process body — there is no
`modules/` directory, no `ext.args`, and no STAR parameter block in `nextflow.config`.** The
production invocation (`workflows/star_full.nf` L131–151) is:

```text
STAR --readFilesIn $R2 $R1 --runThreadN … --genomeDir … \
     --soloCBwhitelist … --soloUMIlen … --soloStrand … --soloCBlen … \
     --soloType CB_UMI_Simple --clipAdapterType CellRanger4 \
     --outFilterScoreMin 30 \
     --soloCBmatchWLtype 1MM_multi_Nbase_pseudocounts --soloCellFilter EmptyDrops_CR \
     --soloUMIfiltering MultiGeneUMI_CR --soloUMIdedup 1MM_CR \
     --soloFeatures Gene GeneFull GeneFull_ExonOverIntron GeneFull_Ex50pAS Velocyto \
     --soloMultiMappers EM Uniform --outSAMtype None --soloBarcodeReadLength 0 \
     --outFileNamePrefix results
```

The parameter-search invocation (`workflows/star_params.nf` L234–254) is byte-identical but for
`--soloFeatures GeneFull --soloMultiMappers EM`. Those two are the only STAR alignment calls in the
repo. **Every one of `alignIntronMin`, `alignIntronMax`, `alignMatesGapMax`, `alignSJoverhangMin`,
`alignSJDBoverhangMin`, `outFilterType`, `outSJfilter*`, `winBinNbits`, `winAnchorDistNbins` and
`scoreGenomicLengthLog2scale` is absent** — verified by grep over the working tree *and*
`git log --all -S<flag>` across all 311 commits, so the strings have never existed there. The only
non-default alignment-side flags are `--outFilterScoreMin 30` and `--clipAdapterType CellRanger4`.

**One parameter set for 26 genomes.** `data/star_indices.csv` maps organism → index directory and
nothing else; `lib/star_params.groovy::loadStarIndices()` validates exactly the two columns
`["organism", "star_index"]`, so there is no code path for a per-organism alignment setting. The 26
include *C. elegans* (WBcel235), *D. melanogaster*, *S. cerevisiae*, *A. thaliana*, rice and maize
alongside human and mouse — **so this is emphatically not a mammals-only pipeline**, and its own QC
notebook (`notebooks/STAR_refs/all-org_star-ref_eval.ipynb`) explicitly evaluates four *C. elegans*
samples without any intron-length, junction or alignment-geometry check. The only genome-size-aware
knob anywhere is `--genomeSAindexNbases` at index build (14 for mammals down to 12 for worm and 10
for yeast), which is a suffix-array memory parameter with **zero** effect on alignment reach.

Two further observations that bear on us:

- **No BAM is ever written** (`--outSAMtype None`) and `SJ.out.tab` is written into the Nextflow work
  dir but never declared as an output, so it is deleted with the work dir. **No CIGAR ever leaves the
  pipeline**, which means neither the authors nor any scBaseCount consumer can audit junction
  geometry after the fact — while `--soloFeatures … Velocyto` derives spliced/unspliced/ambiguous
  assignments from exactly those uninspectable gapped alignments.
- Their chemistry detection (`STAR_PARAMS_WF`) searches five axes — whitelist, CB length, UMI length,
  strand, and *index/species* — and selects on a cell-calling metric. Alignment geometry is not one of
  the axes. When organism metadata is missing it tests all 26 genomes and picks the winner partly on
  apparent mapping rate, which a permissive intron reach can inflate.

**Would it have caught this artifact class?** Split the answer:

- **The specific IGV read: caught, for the wrong reason.** `--outFilterScoreMin 30` plus the untouched
  `outFilterScoreMinOverLread 0.66` / `MatchNminOverLread 0.66` reject a 20-matched-base alignment on
  a 150 bp read. But that is a *soft-clip* filter firing, not an intron filter, and it fires only
  because their chemistry does not remove the clipped bases from the denominator the way our Tn5 clip
  does (§1.3). The 94.5 kb gap contributes about −4 points; it is not what kills the read.
- **The artifact class: missed.** Take the same fabricated gap on a read that aligns well —
  `70M94511N75M5S`, 145 matched bases. Score ≈ 141, which clears `outFilterScoreMin 30` and both
  0.66 filters; overhangs of 70 and 75 clear `alignSJoverhangMin 5`; 94,511 is inside the default
  reach; `outFilterType Normal` means no `BySJout` pass; and `outSJfilterIntronMaxVsReadN` would flag
  it in `SJ.out.tab` — a file they discard, and which by design never affects alignments anyway. The
  read is accepted and its two blocks land in two loci 94.5 kb apart, which on a genome with a 66 bp
  median intron is essentially always two different genes.

**The comparator's lesson for us is not "copy them".** It is that a pipeline built explicitly to
process many organisms uniformly did not treat intron reach as an organism-dependent fact — and that
the failure is invisible from its outputs because it emits no CIGARs. seqforge does emit CIGARs and
does keep `SJ.out.tab`, which is why this artifact was findable here at all.

---

## 5. What each counter does with a long-`N` read

The question is whether the tool uses the **aligned blocks** (excising `N`) or the **full reference
span** (including it).

| tool | blocks or span | source | two blocks, two genes → |
|---|---|---|---|
| STAR `--quantMode GeneCounts` | **blocks** | `Transcriptome_geneCountsAddAlign.cpp`, `for (int ib=a.nExons-1; ib>=0; ib--) {//scan through all blocks of the alignments` | `N_ambiguous` (`gene1[itype]=-2`) — counted for neither |
| featureCounts | **blocks** (split at `N`, `I`, `D`) | `readSummary.c`, `parse_bin()` builds `Starting_Chro_Points_1BASE[]`; `process_line_buffer()` searches once per section | `Unassigned_Ambiguity` by default; both with `-O`; the larger block's gene with `--largestOverlap` |
| HTSeq-count | **blocks** — only `M`/`=`/`X` | `count_features_per_file.py`, `com = ("M","=","X")`; `iv_seq = (co.ref_iv for co in r.cigar if co.type in com …)` | `union` → `__ambiguous`; `intersection-strict` → `__no_feature`; `intersection-nonempty` → `__no_feature` |
| zUMIs | **blocks**, with `largestOverlap=TRUE` hardcoded | `runfeatureCountFUN.R:351–366` | **assigned to the larger block's gene** — no ambiguity call |
| STARsolo `Gene` | blocks **+** whole-span containment **+** SJ concordance | `Transcriptome_classifyAlign.cpp` — `alignSJconcordant` requires the `N` gap to equal an annotated intron | no gene at all |
| STARsolo `GeneFull` | blocks, against gene **bodies** | `Transcriptome_geneFullAlignOverlap.cpp` | both genes enter the set → dropped at UMI collapse |
| STARsolo `GeneFull_ExonOverIntron` | **full span**, in its intronic fallback | `Transcriptome_geneFullAlignOverlap_ExonOverIntron.cpp` | the one STAR path that queries the gap-inclusive interval |
| **our `umite/count.py`** | **full span** | `_fragment_span()` → `exonic(contig, start, end)` | `ambiguous` |

**`--quantMode GeneCounts` uses BLOCKS. Read directly from `2.7.11b`, not taken second-hand** —
`source/Transcriptome_geneCountsAddAlign.cpp` is 55 lines and this is its whole assignment loop:

```cpp
for (int ib=a.nExons-1; ib>=0; ib--) {//scan through all blocks of the alignments
    uint64 g1=a.exons[ib][EX_G]+a.exons[ib][EX_L]-1;//end of the block
    e1=binarySearch1a<uint64>(g1, exG.s, (int32) exG.nEx);
    while (e1>=0 && exG.eMax[e1]>=a.exons[ib][EX_G]) {//these exons may overlap this block
        if (exG.e[e1]>=a.exons[ib][EX_G]) {//this exon overlaps the block
            …
            if (gene1.at(itype)==-1) { gene1[itype]=exG.g[e1]; }          //first gene
            else if (gene1.at(itype)==-2) { continue; }                    //already ambiguous
            else if (gene1.at(itype)!=(int32)exG.g[e1]) { gene1[itype]=-2; }//another gene → ambiguous
```

Each `a.exons[ib]` is one aligned block, tested against its own `[start, end]`. **The `N` gap is
never an interval anything is searched against**, so it contributes no overlap. Two blocks landing in
two different genes set `gene1 = -2`, which the tail of the function tallies as `cAmbig` — the
`N_ambiguous` row of `ReadsPerGene.out.tab`. A `Transcript`'s `exons[]` spans both mates of a pair,
so the manual's "Both ends of the paired-end read are checked for overlaps" is literally this same
loop, and the mate gap is likewise not an interval. Multimappers never reach the loop at all
(`if (nA>1) { quants->geneCounts.cMulti++; }`).

**So the bulk pipeline (`map/star`, which is the only module using `--quantMode GeneCounts`) does not
inherit our counter's span problem.** On the IGV read it would report `N_noFeature`, because neither
block is exonic (§5.1). Its exposure to this artifact is a *misassignment* risk when one block lands
in a real exon, not the 94.5 kb-wide ambiguity the plate counter suffers.

Notes on the two claims most often repeated without a source:

- STAR's "counts coincide with those produced by htseq-count with default parameters" is a
  **documentation** claim (manual §8, p. 18). The mechanism does match union-over-exon-unions; there
  is no test or comment in the source asserting the equivalence.
- HTSeq's block-only handling of `N` is **not documented** anywhere on htseq.readthedocs.io. It is a
  source finding.
- `-J` / `--splitOnly` in featureCounts change nothing about *how* overlap is computed;
  `--splitOnly` is a pre-filter on `is_junction_read` and `-J` only tallies junctions. What *would*
  change the outcome is `--minOverlap` or `--largestOverlap`, which is why zUMIs never sees the
  ambiguity.

### 5.1 What ours does, on this exact read

`_fragment_span()` sees a proper pair with `TLEN 94531`, returns `(4035578, ~4130109)` — one
**contiguous** 94.5 kb interval — and hands it to `exonic()`. Measured against the WBcel235.115
annotation, that window contains **21 gene bodies**, all 21 of which have an exon inside it. So
`len(exonic) > 1` and the fragment is filed **`ambiguous`**.

Where the two anchors actually land, also measured:

| block | interval | annotation |
|---|---|---|
| `12M` | `II:4,035,579–4,035,590` | **intergenic** — between `K07D4.4` (ends 4,032,393) and `pqn-48` (starts 4,038,884) |
| `8M` | `II:4,130,102–4,130,109` | inside the `ast-1` gene body (4,127,837–4,135,359), **not in an `ast-1` exon** |

So the same read is filed four different ways by four engines: STAR `GeneCounts` and featureCounts
would call it `no feature` (neither block is exonic); STARsolo `GeneFull` would credit `ast-1`;
zUMIs' `largestOverlap` would credit whichever gene the 12 bp block hits, i.e. nothing; and **ours
alone converts an intergenic-plus-intronic pair of stubs into 94.5 kb of `ambiguous`.** Our counter
is the most sensitive of the surveyed engines to this artifact, by construction — and
`_fragment_span`'s docstring already argues for the contiguous span on independent grounds (it also
covers the inner mate gap, which a union of two mate intervals does not). The cost is that a
fabricated `N` gap is indistinguishable from a real fragment span.

**This is one read.** How much of the plate looks like this was unmeasured when this was written; §8
is how it was found out, and what the answer turned out to be.

---

## 6. Are such reads ever real?

### 6.1 The real mechanisms, and what each would actually look like

**Trans-splicing (SL1/SL2).** About **70%** of *C. elegans* mRNAs are trans-spliced to one of two
**22-nucleotide** spliced leaders; about **15%** of genes are in operons
([Blumenthal, *WormBook*, "Trans-splicing and operons"](https://www.ncbi.nlm.nih.gov/books/NBK19704/)).
The genome carries **110 SL1 RNA genes on the 1 kb tandem repeat that also contains the 5S rRNA
genes**, and only **18 dispersed SL2 RNA genes**. This matters here, and the direction it points is
*away* from long `N` gaps: the SL is spliced onto the mRNA's 5′ end from a *separate transcription
unit*, so a read carrying it presents as a ~22 bp 5′ soft clip whose sequence is the leader, or as a
chimeric alignment to the SL1 repeat locus — never as a long intragenic gap on chrII. **Trans-splicing
predicts short 5′ soft clips and chimeras, not 94 kb introns.** It is nonetheless the single most
important confounder to keep in mind on this organism, because it guarantees that a large fraction of
worm reads legitimately carry ~22 bases that do not belong to the gene they came from.

**Operons.** "The vast majority of operon genes are separated by only **~100 bp**" (same source). A
polycistron cannot manufacture a 94.5 kb gap; the intercistronic distance is three orders of
magnitude too small.

**Genuine long introns.** They exist, and §3.2 names every one of them: two unique annotated introns
above 25 kb, the longest 100,912 bp. A read supporting one of those would be crossing an
**annotated** junction, would carry `jM ≥ 20`, and would be exempt from every `outSJfilter*` gate.
**No annotated worm intron reaches 590 kb, let alone the ~1.05 Mb gaps actually observed** (§3.2:
zero unique introns above 589,824).

### 6.1a Can trans-splicing survive an `alignIntronMax`? — the safety question for §7.3

This is the one mechanism that could make §7.3 actively harmful, so it is worked through rather than
asserted. **The answer is that a cap at 15,000–25,000 destroys no trans-splicing biology**, for four
independent reasons, three of them measured against WS298 directly.

**1. Trans-splicing removes the outron; it does not create a long genomic gap.** The SL is donated by
a *separate* transcription unit, so in the mature mRNA the 22 nt leader is simply not genomically
adjacent to anything. The intron-like sequence that *is* removed is the **outron**, and WormBook
bounds it: *"these outrons range from **60–500 bp**, with many around 300 bp"*. An alignment that
somehow spanned an outron would show a gap of at most ~500 bp — **thirty times below the tightest cap
under discussion**.

**2. Operon structure is likewise three orders of magnitude too small.** *"The vast majority of
operon genes are separated by only **~100 bp**"*. An SL2 trans-splice site sits ~100 bp downstream of
the upstream gene's 3′ end.

**3. For 98.5% of genes a leader-to-SL-locus junction is geometrically impossible.** Measured in
WS298: the SL1 RNA genes (`sls-1.*`) are **all on chrV**, in a tandem array at
`V:17,118,154–17,132,107` (13,953 bp, 12 annotated copies — WormBook's ~110 real copies are collapsed
in the assembly), interleaved with the 5S rRNA genes (`rrn-4.*`) exactly as WormBook describes. SL2
genes (`sls-2.*`) are dispersed: 4 on chrI, 3 on chrII, 6 on chrIII, 6 on chrIV. Because window
merging is **chromosome-clamped** (§1.2), a junction from a gene to the SL1 array can only ever be an
`N` gap if the gene is *also* on chrV and within merge reach. Counting genes within one merge step
(589,824 bp) of the array: **291 of 19,983 protein-coding genes, or 1.46%.** For the other 98.5%, the
leader can only ever be soft-clipped or emitted as a chimera.

**4. Chimeric output is off, so even the cross-chromosome case is a soft clip.** `chimSegmentMin`
defaults to 0 and none of the four modules sets it, so STAR emits no chimeric alignments at all. And
a 22 nt SL1 seed matches ~110 genomic loci in the real repeat, far above `winAnchorMultimapNmax 50`,
so it cannot even *create or extend* a window.

**What trans-splicing does look like at the aligner: a ≤22 bp 5′ soft clip.** That matters because it
means trans-splicing and the adapter read-through of §6.2 **both produce short anchors and large soft
clips, and anchor length alone cannot tell them apart.** What separates them is the *sequence* of the
clipped bases and its *end*:

| | trans-splicing (real biology) | Tn5 read-through (artifact) |
|---|---|---|
| clipped sequence | the SL1/SL2 leader | the mosaic end `CTGTCTCTTATACACATCT` |
| which end | **5′** of the mRNA sense strand | **3′** of the read |
| clip length | ≤ 22 nt, hard-bounded | up to most of the read (130 bp observed) |
| clipped bases in the filter denominator | **yes** — soft clip, so `Lread` still counts them | **no** — `--clip3pAdapterSeq` removes them (§1.3) |
| implied gap | none, or ≤500 bp (outron) | arbitrary; ~1 Mb observed |
| mate CIGARs | independent | **mirrored** (§6.3) |

The fourth row is the sharpest: a genuinely trans-spliced read keeps its 22 clipped bases in the 66%
denominator and so must still align well over the rest of its length, whereas the artifact class
exists *because* the adapter clip shrank the denominator. **A cap on gap length touches neither
column** — it is orthogonal to the thing trans-splicing actually does.

One curiosity that ties §3.3 to this section: the rank-2 "intron" of 86,515 bp in *cox-6B* belongs to
a transcript (`Y71H2AM.5.2`) whose first exon is **22 bp and entirely 5′UTR** — the exact length of a
spliced leader, separated from the body of the gene by a very long gap. This document does not claim
that annotation is a trans-splicing artifact; it notes the coincidence, and that it is testable by
comparing that 22 bp exon's sequence to the SL1 leader.

### 6.2 The artifact mechanisms

- **Tn5 read-through leaving a short genomic anchor.** Already measured in this repo: 38% of the
  plate's reads were discarded `too short` before clipping, and the mechanism is a denominator
  ([`smartseq3-tn5-read-through.md`](smartseq3-tn5-read-through.md), ADR-0048). §2.3 shows the
  observed read is one of the survivors of exactly that process.
- **Reverse-transcriptase template switching.** The primary source is Houseley & Tollervey,
  [*PLoS ONE* 5(8):e12271 (2010)](https://journals.plos.org/plosone/article?id=10.1371/journal.pone.0012271),
  which reproduced apparent trans-splicing between two RNA molecules *in vitro* with purified
  substrates and concluded that most reported non-canonical splicing in metazoans arises from
  template switching during cDNA preparation. Smart-seq3's chemistry is *built on* template switching
  (the TSO), so this is the mechanism most specific to this library type.
- **PCR chimeras** during pre-amplification, and **index hopping** on patterned flow cells. Index
  hopping moves reads between cells; it does not create long `N` gaps, so it is not a candidate for
  this signature.
- **A short anchor placed by chance.** §2.1–2.2: this needs no wet-lab mechanism at all. Given ~9
  chance 8-mer matches per window and a length penalty that maxes out at 3 points, the aligner
  manufactures the junction unaided.

### 6.3 The signature, from the plate's own CIGARs

Nearly every observed instance pairs a **tiny anchor (7–21 bp)** with a **large soft clip (65–130 bp
of Tn5 mosaic adapter)**, and the mates show **mirrored CIGARs** —
`9M266439N11M130S` opposite `130S9M266439N11M`. Both mates are stacked on the same ~20 real bases.
That is the fingerprint of an **ultra-short fragment plus adapter read-through**: the insert is
shorter than the read, both mates sequence the same few genomic bases and then run into the mosaic
end, and STAR is handed two ~20 bp reads with which to build a paired alignment.

Two instances share the identical `840537N` gap (`14S21M840537N115M` and `1S9M840537N140M`), which
means **recurrence alone does not discriminate**: a phantom junction anchored on a repeat element
will be found again and again by independent reads. That weakens criterion 3 below and should be
stated plainly rather than discovered later.

### 6.4 The free control: E. coli has no introns

The plate is mapped against the chimeric `ce11_ecHT115` reference. **Bacteria have no spliceosomal
introns, so *any* `N` gap on the `ecHT115` **Component** is definitionally spurious** — there is no
biological process that can produce one, and no annotation that could excuse one. That makes the
bacterial component a built-in false-positive assay, already present in every plate BAM, requiring no
new run:

- the **rate** of spliced records per aligned record on the E. coli component is a direct estimate of the
  aligner's phantom-junction rate at this read length and this adapter load;
- the **length distribution** of those gaps calibrates what "too long" means empirically rather than
  from annotation;
- the **anchor-length distribution** of those gaps says exactly where to put `alignSJoverhangMin`.

This is the single most useful measurement available and it is cheap (§8e). Its one limitation: the
E. coli component carries a different sequence composition and a much smaller replicon (~4.6 Mb
versus ~15 Mb per worm chromosome), so the chance-match rate per window is not identical to the
worm's. It bounds the phenomenon; it does not transfer as an exact rate.

**It was run twice** — as #459's census, where every spliced E. coli record in all three cells sits on
a ≤25 bp anchor, at 100%; then as Gate 1 of the change, the falsifiable half of what gated #467 (§8).

### 6.5 Criteria that discriminate real from artifact, for one read

Ordered by how much each decides:

1. **Is the junction annotated?** `jM ≥ 20` (annotated) versus `1/2` (novel GT/AG) versus `0`
   (non-canonical). Requires `jM` in `--outSAMattributes`, which we did not ask for until #467 and
   now do on every module (§7.4). This one criterion resolves most cases.
2. **How long is the shorter anchor?** Real junctions have long anchors on both sides. Eight bases is
   at the noise floor: ~9 chance matches inside 589,824 bp (§2.2). Twelve is 0.035.
   `outSJfilterOverhangMin`'s own threshold for a GT/AG junction is **12**.
3. **How many independent fragments share the exact donor/acceptor pair?** Weaker than it looks —
   §6.3 shows a repeat-anchored phantom recurring across reads. Use it only together with 2 and 4.
4. **Do the anchors sit in the same gene, in the same orientation, with the donor at an annotated
   exon boundary?** The IGV read fails badly: one anchor is intergenic, the other intronic in a
   different gene 94.5 kb away.
5. **Does the soft-clipped sequence match a known non-genomic sequence?** Tn5 mosaic end
   `CTGTCTCTTATACACATCT`, or the 22 nt SL1 leader. If it does, the "junction" is chemistry.
6. **Is the gap longer than the longest annotated intron for this organism?** 100,912 bp for
   *C. elegans* WS298; **zero** for E. coli. A gap above that has no annotated precedent anywhere in
   the genome.
7. **How much headroom does the alignment have over the filter?** `AS = 36` against a threshold of
   `0.66 × 54 ≈ 35` is an alignment that exists only because the denominator shrank.

The IGV read fails 2, 3 (in the weak sense), 4, 6 and 7; criterion 1 could not be evaluated from what
the archives held, which is the whole argument for §7.4.

---

## 7. Recommendations, with the source for each value

None of these was a decision when it was written; each was a candidate with its price named. **They
are ordered by how much of the real work each does**, which is not the order intuition suggests: the
artifact signature is a short anchor (9–13 bp), not a particular gap length, so the junction filters
lead and the intron cap — the flag everyone reaches for first — is third and deliberately loose. Note
also that the modules are **generic across organisms**, so a worm-shaped constant does not belong in a
`shell:` block — `--alignIntronMax` is recipe-shaped (`processing.yaml`), while the overhang and
filter-type flags are organism-independent and could be module literals. ADR-0049 also matters:
STARsolo's argv already has one owner (`workflows/starsolo_args.py`) and the other three map modules
do not, so adding flags in three places re-creates exactly the drift that record was written about.

**§7.1–§7.4 shipped in #467, in the shape this paragraph anticipated**, under the plan in #461: three
module literals, and an intron pair read per-assembly from `liulab-genome`'s table into
`processing.yaml` and so into `run_id` (ADR-0056; `liulab-genome`'s ADR-0010). ADR-0049 mattered as
expected, so all four modules read **one** renderer,
[`workflows/splice_args.py`](../../src/seqforge/workflows/splice_args.py), rather than three `shell:`
blocks. §7.5 did not ship, and says why.

### 7.1 `--outFilterType BySJout` — organism-independent, and it closes the QC blind spot

**Shipped** in #467 as a module literal, on all four STAR modules.

**Value:** `BySJout`. **Source:** STAR manual §3.3.2, ENCODE long-RNA standard; the manual's gloss is
"reduces the number of ''spurious'' junctions", and §17 defines it as "keep only those reads that
contain junctions that passed filtering into `SJ.out.tab`".

**Why it targets these reads:** the observed junctions fail `outSJfilterOverhangMin` (7–21 bp anchors
versus a 12 bp threshold for GT/AG, 30 for non-canonical) and fail `outSJfilterIntronMaxVsReadN`
(94,511 bp up to ~1.05 Mb against 50,000 at one supporting read). Under `Normal` those filters only govern
`SJ.out.tab`; under `BySJout` the alignment carrying the junction is rejected in stage 2
(`stitchWindowAligns.cpp:169`) and STAR falls back to the next-best placement. **It also closes §0's
blind spot** by making `SJ.out.tab` and the BAM agree about which junctions exist.

**Risks and costs:**
- **Two-stage mapping.** Reads whose best alignment contains a novel junction are held and re-mapped
  (`STAR.cpp:203–220`). Cost on a 784-cell plate is still **unmeasured**. On the gate's four cells the
  wall clock went the *other* way — 29 m 33 s against the control's 1 h 15 m 38 s — but that run
  changed four flags at once and the attribution is not established (§8).
- **Junction support is pooled per STAR run, i.e. per CELL on a plate.** A novel GT/AG junction needs
  only `countUnique ≥ 1`, so the count gate does not bite; the overhang and gap gates do. Annotated
  junctions are exempt outright (`*oneSJ.annot>0`), and with the GTF in the index nearly all real
  worm junctions are annotated — which is what makes the risk small here and would make it larger for
  a poorly annotated organism.
- Compatible with our invocation: SAM input is explicitly handled
  (`ReadAlignChunk_processChunks.cpp:28` guards the SAM branch on `outFilterBySJoutStage!=2`), and
  coordinate-sorted output with `--outSAMunmapped Within` is handled by
  `coordUnmappedPrepareBySJout()`. `--outSJtype Standard` is required and is the default.
- Genuinely novel long junctions with short overhangs are lost. That is the point, and it is also the
  cost for anyone doing novel-isoform discovery on this data.

### 7.2 `--alignSJoverhangMin` / `--alignSJDBoverhangMin` — the flag that actually matches the signature

**Shipped** in #467 as module literals, at ENCODE's `8` and `1`, on all four STAR modules — with the
caveat below taken rather than argued away: `8` does not reach the observed anchors, and `BySJout` is
what closes that gap, since the junction filter it enforces uses a 12 bp GT/AG overhang threshold.

**Values:** ENCODE uses `8` and `1`. **Sources:** STAR manual §3.3.2;
`ENCODE-DCC/rna-seq-pipeline/src/align.py`.

Lowering `alignSJDBoverhangMin` 3 → 1 makes *annotated* junctions easier, which is protective: it
moves real splicing onto the exempt path. Raising `alignSJoverhangMin` 5 → 8 removes the shortest
anchors — but note §2.1: break-even for a canonical junction is ~3 bases, so the default of 5 is
already below where the score stops rewarding the trick, and 8 does not reach the observed 8–21 bp
anchors either.

**Caveat that matters:** `stitchWindowAligns.cpp:103` tests
`exons[isj+1][EX_L] < alignSJoverhangMin + shiftSJ`, so with a repeat shift of 0 an exactly-8 bp
anchor **passes at ENCODE's 8**, and every anchor in §2.2 except the 7 bp one survives it. Something
like 12 (matching `outSJfilterOverhangMin`'s own GT/AG threshold) would exclude most of them by
overhang alone, but 12 is not a value any published pipeline uses and would need its own
justification — which §6.4's E. coli control could supply empirically.

**This is the flag that matches the observed signature.** The artifact population is characterised by
a **9–13 bp anchor**, not by any particular gap length — so the anchor filter and §7.1 do the
discriminating work, and §7.3's cap does not. Rank effort accordingly.

### 7.3 `--alignIntronMax` / `--alignMatesGapMax` — a coarse structural bound, deliberately loose

**This flag's job is to exclude the structurally impossible, not to discriminate artifacts.** That
distinction is the whole of this subsection. Discrimination belongs to §7.1 and §7.2; the cap exists
only so that STAR has *some* hard check where it had none at all (§1.2). Setting it tight enough to
separate artifacts from biology would be using the wrong instrument, and would fail silently in the
one direction that cannot be audited.

**The GTF is not ground truth.** An annotation is a catalogue of most *known* transcripts, not of all
*possible* ones. Unannotated isoforms, rare and condition-specific transcripts, and every future
release live past the current maximum. So `max(annotated intron)` is a **floor on reality, not a
ceiling** — and a cap set at or near it is wrong in kind, not merely short of margin. The failure is
invisible: a read from a real-but-unannotated long intron simply stops aligning, is filed `too
short`, and nothing anywhere reports that a length rule caused it.

**Decision (hq, 2026-08-21): `--alignIntronMax 50000` and `--alignMatesGapMax 50000` for ce11** —
**shipped** in #467, and rendered on the plate's chimera as the maximum over its components (`ce11`
50,000, `ecHT115` 1) rather than typed. This section previously recommended 200,000–300,000 on margin
grounds. Two facts moved it, and both are stronger than the margin argument:

**1. The organism's own curators are tighter.** WormBase's production RNA-seq pipeline
([`RNASeq.pm`](https://github.com/WormBase/wormbase-pipeline/blob/master/scripts/Modules/RNASeq.pm)
L3445, §4) sets `alignIntronMin 25`, **`alignIntronMax 15000`**, **`alignMatesGapMax 50000`**. A
15 kb intron ceiling is what the authoritative worm resource accepts for gene-model building. 50,000
is **3.3× more permissive** than that, and equals its mate-gap value exactly — so it is a
conservative reading of the authoritative setting, not an aggressive one.

**2. The band 25,000–86,515 is empty, so margin inside it is free of cost.** Every cap in that range
clips the identical 3 introns of 204,769 (§3.3), all three in the sjdb and therefore largely exempt
(§1.5). Raising the cap from 50,000 to 100,000 buys back exactly one intron in one gene — `cox-6B`,
whose 86,515 bp entry is a 5′UTR intron in a single WormBase transcript RefSeq does not carry — at
the cost of ~19% of the artifacts removed (1,347 vs 1,605 reads on `day9_N2_9`).

| quantity | value | role in the choice |
|---|---|---|
| WormBase's own `alignIntronMax` | **15,000** | the authoritative worm setting; ours is looser |
| longest annotated worm intron | 100,912 (§3.3) | a **floor** on reality, never the target |
| p99.99 of worm introns | 19,396 | the cap sits **2.6×** above this |
| annotated introns above 25,000 | **3 of 204,769** | the whole cost, and all three are sjdb-exempt |
| the effective ceiling before #467 | ~15.3 Mb (chrII, §1.2) | what we ran |
| tightening factor | **~300×** | |
| longest gap observed | 1,049,334 | excluded by a wide margin |

**A second-order benefit specific to the tight value.** Setting the cap re-derives `winBinNbits`
(§1.1): 50,000 → 14, 100,000 → 15, 200,000 → 16 (i.e. unchanged from default). At 50,000 the
per-step window reach falls from 589,824 bp to ~147,456 bp, so the tight cap suppresses the
**transitive merging of §1.2** rather than only the final gap length. The earlier draft counted
"200,000 leaves `winBinNbits` untouched" as a point in its favour; that reading was backwards for
this failure mode. The gate's unexpected 2.6× speedup is consistent with this and is the only
evidence either way, and it is one observation on one cell set with four flags moving together (§8).

**Set both flags.** `alignIntronMax` alone leaves the mate gap uncapped while still redefining the
binning — STAR's own source carries an `ISSUE - to be fixed in STAR3` comment on exactly that.

**The residual risk, unchanged and irreducible:** an unannotated long intron is not in the sjdb and is
hit fully by the cap. That is the real content of "a GTF is not ground truth", and no value makes it
go away — it is bounded only by how empty the tail is (3 introns above 25 kb; p99.99 at 19,396).

**The cap is still the backstop, not the fix.** §7.1 and §7.2 identify these reads by anchor length,
which is what the measured signature actually is (9–13 bp anchor, 66–122 bp soft-clip; 100% of the
E. coli control). A cap cannot discriminate an artifact from biology; it can only exclude the
structurally impossible. **The gate says so directly:** the two E. coli cells that did not reach zero
kept their residual under gaps of 16,093 and 39,244 bp — *below* the 50,000 cap, so the cap could not
have excluded them and only the anchor filters were ever going to (§8).

**Trans-splicing does not argue for loosening it further** — §6.1a works this through: outrons are
60–500 bp, operon spacing ~100 bp, and for 98.5% of genes an SL-leader junction cannot be an `N` gap
at all. The one exception, the 1.46% of protein-coding genes within merge reach of the chrV SL1
array, is bounded by that array's position and is far inside any cap considered here, 50,000
included.

**The same logic on the other genomes, and a measured illustration that these values are conventions
rather than truths.** ENCODE's widely-copied `--alignIntronMax 1000000` is **below** the longest
annotated intron in both mammals it was written for:

| genome | longest annotated unique intron | annotated introns above ENCODE's 1,000,000 |
|---|---|---|
| mm39 / GRCm39 | **2,908,816** (*Fgfr2*) | **6** |
| hg38 / GRCh38 | **1,240,120** (readthrough model); 1,160,411 in *ROBO2* | **8** |

So the canonical mammalian setting already clips real annotated introns — and has done for a decade
without anyone treating it as a defect. That is the clearest available evidence that `alignIntronMax`
is a **convention with margin**, not a measured quantity, and that the right way to choose it is a
deliberately loose round number rather than an arithmetic on an annotation. Anything recommended for
hg38/mm39 here would have to clear 2,908,816, i.e. be looser than ENCODE's.

**Retracted: deriving the cap from the registered GTF.** An earlier draft of this document floated
computing the cap from the annotation being indexed. That idea now has two independent strikes:

1. **It fails silently and in the tight direction.** §3.3 is a worked example — differencing exon
   coordinates without sorting for strand produced a plausible-looking maximum 14% below the truth,
   with no error raised.
2. **Its input is the wrong kind of object.** Per the paragraph above, an annotation's maximum is a
   floor on biology, so deriving a ceiling from it is category-incorrect however carefully it is
   computed.

If a per-organism value is wanted, it should be **a small table of deliberately loose round numbers
with a recorded rationale**, not a `max()` over a file. That is what shipped, and both halves of the
retraction are now recorded as decisions: `liulab-genome`'s ADR-0010 for why the table carries a
hand-set column and derives nothing, ADR-0056 for why seqforge reads it.

**A tension this document left open, and where it was settled.** Genome facts belong to
`liulab-genome`, not to seqforge — an argument for the per-organism value living there. But a
hand-set loose constant is a *different kind of fact* from a derived one: it is a policy choice about
aligner behaviour, not a property of the assembly, and it was not obvious it belonged in the same
place. **ADR-0056 settled it**: the bound is a property of the *reference*, so it is registered
upstream and then **copied** into `processing.yaml` — because `run_id` folds no pin on
`liulab-genome`, and a value read at run time would let one edited table cell change how a dataset
aligns while two compiled pipelines kept one identity.

**Remaining risks, whatever value is chosen:**
- **Set both, not one.** With `alignMatesGapMax` left at 0 the mate gap stays uncapped by the
  explicit check and is instead bounded emergently by the redefined windowing — STAR's own source
  calls this an `ISSUE` (§1.1). Setting both makes the number you chose the number that applies.
- **Annotated long introns are largely but not wholly exempt** (§1.5) — a second reason to stay loose,
  since the exemption cannot be relied on for reads stitched genomically.
- **`winBinNbits` is redefined as a side effect**, and how much depends on the value: at 200,000 on
  ce11 `floor(log2(200000/4)+0.5) = 16`, unchanged from the default, while 50,000 gives 14 and a
  25,000 cap would give 13. The earlier draft counted the loose value's *non*-perturbation as a point
  in its favour; the paragraph above retracts that, because suppressing the transitive merging is the
  point rather than a side effect to minimise.

### 7.4 `--outSAMattributes` — yes, extend it with `jM jI`

**Shipped** in #467, on all four modules — with one word of this subsection withdrawn. It is
**not costless**, and the cost was not in alignments: `jM`/`jI` are the first SAM type-`B` **array**
tags this pipeline has ever carried, and `split.py::_rewritten` fed pysam the bare `'B'` its own
reader reports for an array, which `set_tags` rejects. Every `split_chimera` job on a chimeric plate
died. Pre-existing code the new tags made reachable for the first time, fixed in `6d441c7`. **The
gating run found it and the unit suite did not**, because no synthetic fixture record carried a
non-scalar tag — which is the part worth carrying forward.

**Value:** `NH HI AS nM jM jI` (i.e. `Standard` plus the two junction attributes; `NM MD` optional and
costlier). **Source:** `parametersDefault` — `jM` is "intron motifs for all junctions (i.e. N in
CIGAR): 0: non-canonical; 1: GT/AG, 2: CT/AC, 3: GC/AG, 4: CT/GC, 5: AT/AC, 6: GT/AT. **If splice
junctions database is used, and a junction is annotated, 20 is added to its motif value**"; `jI` is
"start and end of introns for all junctions (1-based)".

This is the cheapest recommendation here and the only one that costs no *alignments*. §2.2 could not
close because `AS` alone cannot say whether a junction was annotated, canonical or not — and for
three of the six CIGARs the answer decides whether STAR preferred the phantom at all. §6.5's first
and best discriminator is exactly `jM`. Without it, the BAM records that a megabase gap happened and
refuses to say what kind.

**Safe against the UB passthrough.** `readFilesSAMattrKeep` tags are written by a separate code path
(`ReadAlign_alignBAM.cpp:484`, gated only on `readFilesTypeN==10`), independent of
`--outSAMattributes`, so naming attributes explicitly does not disturb the `UB` tag the plate modules
depend on. Costs: a few bytes per spliced record in BAM and CRAM.

### 7.5 `--peOverlapNbasesMin` — relevant, still open, and deliberately not in #467

**The one recommendation here that did not ship, and it is still a good idea.** #461 scoped it out on
this subsection's own grounds — it changes alignment for *every* read rather than the pathological
ones, and no pipeline surveyed in §4 enables it — so it needs its own measurement rather than a place
inside someone else's gate.

The manual (§11 "Merging of overlapping mates") says it "improves mapping accuracy for paired-end
libraries with short insert sizes, where many reads have overlapping mates", merging mates and
re-mapping the merged sequence, reporting the merged alignment only "if the score of this alignment
[is] higher than the original one".

**The mirrored-CIGAR signature in §6.3 is exactly that case** — both mates stacked on the same ~20
real bases is a fully dovetailed, insert-shorter-than-read fragment, which is the population this
algorithm exists for. Merging the mates first would give STAR one ~20 bp sequence to place instead of
two, removing the paired-end degrees of freedom that let it build a "proper pair" spanning 94.5 kb.

But: it is off by default, and no Smart-seq3 or worm pipeline read in §4 turns it on.

### 7.6 What NOT to do — all six held, and #467 took none of them on

- **Do not adopt the ENCODE set wholesale.** Take `BySJout`, `alignSJoverhangMin` and
  `alignSJDBoverhangMin` from it; leave its intron numbers behind — and note they do not even fit
  the mammals they were written for (§7.3).
- **Do not change `_fragment_span()` to excise `N` gaps as a fix for this.** Its docstring already
  argues for the contiguous span on independent grounds (it covers the inner mate gap, which a union
  of mate intervals does not). If it changes, it should change because someone decided the inner-gap
  argument is wrong — not to paper over an aligner artifact. Note, though, that §5's survey shows
  every other engine on the market uses blocks, so this is a place where we are deliberately alone.
- **Do not raise `alignIntronMin`.** It is inert on worm (§3.4).
- **Do not set `alignIntronMax` near the longest annotated intron, and do not derive it from a GTF.**
  §7.3 — the annotation's maximum is a floor on biology, not a ceiling, and the derivation fails
  silently in the tight direction. Both halves are now recorded: `liulab-genome`'s ADR-0010 and
  ADR-0056.
- **Do not lead with the cap.** It is a structural bound; the anchor filter and `BySJout` are what
  actually discriminate (§7.2).
- **Do not quote 589,824 as the intron ceiling.** §1.2.

---

## 8. The measurement that sized the blast radius

**It was run, and it is not restated here.**
[`star-splice-flags-gate-2026-08-22.md`](star-splice-flags-gate-2026-08-22.md) is the plate counted
twice — the four censused cells realigned with the candidate flags as the only difference, both
components, on `cpu02` under an existing allocation. **Both gates passed.** The intron-free
component's spliced fraction fell from **0.132–2.764% to 0.0000–0.0097%**, two of four cells reaching
exactly zero; the worm component's counted UMIs moved **−0.038% at worst** against a one-percent
ceiling, and *rose* on one cell. That write-up carries the per-cell fate tables, the two arms' SHAs,
the exact commands and the `split_chimera` failure §7.4 records — read it there rather than here.

Two of its findings bear directly on this document's argument and are cited in place: the E. coli
residual sits *below* the cap, so only the anchor filters could have caught it (§7.3), and the
treatment ran 2.6× faster than the control, which nothing here separates from `BySJout` (§7.1, §7.3).

**The read-side census came first, and it brackets rather than answers.** Run 2026-08-21 over `-s
0.05` subsamples: gaps beyond the installed GTF's longest intron are **0.16–1.37%** of unique reads
per cell, and spliced reads on a `≤12 bp` anchor are **18%** of spliced reads (42.8% in the most
degraded cell). The first is a floor — a fabricated gap shorter than a real intron is not separable
by length — and the second a ceiling, because a short overhang against an *annotated* junction is
legitimate and STAR permits it deliberately. Neither is the number that decided anything, which is
why (f) exists.

**The commands are kept because they are the method behind those two figures** and nothing else
records them. They also carry the correction that a first pass compared gap lengths lexicographically
(`awk`'s `substr` returns a string; it needs `+0`), which put `N`>20 kb at 8–16% where the corrected
figure is ~0.2%. **Every gap figure quoted anywhere in this document is from the corrected pass.** All
are read-only and belong in a Slurm job, never on a login node.

**(a) Every `N` gap in one cell's unique BAM, as a distribution.**

```sh
samtools view -F 0x904 day9_N2_9.ce11.unique.bam \
| awk '{ c=$6
         while (match(c,/[0-9]+[MIDNSHP=X]/)) {
           op = substr(c,RSTART,RLENGTH); c = substr(c,RSTART+RLENGTH)
           if (op ~ /N$/) print substr(op,1,length(op)-1)
         } }' \
| sort -n > n_gaps.txt

awk '{a[NR]=$1; s+=$1}
     END{ n=NR
          printf "n=%d mean=%.1f p50=%d p90=%d p99=%d p99.9=%d max=%d\n",
                 n, s/n, a[int(.50*n)], a[int(.90*n)], a[int(.99*n)], a[int(.999*n)], a[n]
          for (t=1000; t<=1000000; t*=5) { c=0; for(i=1;i<=n;i++) if(a[i]>t) c++
            printf ">%8d: %d (%.4f%%)\n", t, c, 100*c/n } }' n_gaps.txt
```

The thresholds that matter for this genome: **100,912** (longest annotated WS298 intron) and
**589,824** (the number the manual implies is a ceiling). Anything above the first has no annotated
precedent; anything above the second is direct evidence for §1.2.

**(b) Gap length against the shorter anchor** — the two numbers that decide whether a cap or an
overhang is the right lever.

```sh
samtools view -F 0x904 day9_N2_9.ce11.unique.bam \
| awk 'BEGIN{OFS="\t"}
       { c=$6; g=0; m=1e9
         while (match(c,/[0-9]+[MIDNSHP=X]/)) {
           op=substr(c,RSTART,RLENGTH); c=substr(c,RSTART+RLENGTH)
           v=substr(op,1,length(op)-1)+0; t=substr(op,length(op),1)
           if (t=="N" && v>g) g=v
           if (t=="M" && v<m) m=v
         }
         if (g>0) print g, m }' \
| tee gap_anchor.tsv \
| awk '{ n++; if ($1>25000) big++; if ($1>25000 && $2<12) bigshort++ }
       END{ printf "spliced records=%d  gap>25kb=%d (%.3f%%)  and shorter anchor<12bp=%d (%.3f%%)\n",
                   n, big, 100*big/n, bigshort, 100*bigshort/n }'
```

**(c) Do the long gaps recur?** — criterion 3 of §6.5, which §6.3 says will *not* be clean.

```sh
samtools view -F 0x904 day9_N2_9.ce11.unique.bam \
| awk 'BEGIN{OFS="\t"}
       { pos=$4; c=$6
         while (match(c,/[0-9]+[MIDNSHP=X]/)) {
           op=substr(c,RSTART,RLENGTH); c=substr(c,RSTART+RLENGTH)
           v=substr(op,1,length(op)-1)+0; t=substr(op,length(op),1)
           if (t=="N") { print $3, pos, pos+v-1; pos+=v }
           else if (t=="M"||t=="D"||t=="="||t=="X") pos+=v
         } }' \
| sort | uniq -c | sort -rn | head -50
```

**(d) The soft-clip content, to confirm the Tn5 attribution of §6.3.**

```sh
samtools view -F 0x904 day9_N2_9.ce11.unique.bam \
| awk '$6 ~ /N/ && $6 ~ /[0-9][0-9]S/ {print $10}' \
| grep -c CTGTCTCTTATACACATCT
```

**(e) The E. coli false-positive rate — the cheapest and most decisive number here (§6.4).**

```sh
# every N gap on the intron-free Component of the chimeric reference; every one is spurious by construction
EC=$(samtools idxstats day9_N2_9.chimera.bam | awk '$1 ~ /ecHT115|NC_/ {print $1}')
samtools view -F 0x904 day9_N2_9.chimera.bam $EC \
| awk 'BEGIN{OFS="\t"} { c=$6; g=0; m=1e9; sp=0
         while (match(c,/[0-9]+[MIDNSHP=X]/)) {
           op=substr(c,RSTART,RLENGTH); c=substr(c,RSTART+RLENGTH)
           v=substr(op,1,length(op)-1)+0; t=substr(op,length(op),1)
           if (t=="N" && v>g) g=v; if (t=="M" && v<m) m=v; if (t=="S") sp+=v }
         n++; if (g>0) { spl++; print g, m, sp > "/dev/stderr" } }
       END{ printf "ecoli aligned=%d  with N gap=%d  (%.4f%% — ALL spurious)\n", n, spl, 100*spl/n }'
```

Repeat on the worm component and subtract: the difference is an upper bound on how much of the worm's
spliced signal is real.

**(f) Plate-wide, and against the `ambiguous` fate.** This is the one that decided the change, and it
was run as written — a controlled re-count on a handful of cells rather than a BAM statistic, because
the fate is only observable through the counter. Four cells, not the whole plate. The commands as
actually run, and every number they produced, are in the gate write-up.

---

## 9. What is still not settled

Reconciled 2026-08-22, when #459 closed. The blast radius, the corrected annotation table, and where
the cap's value lives were on this list and are not any more — §8, §3.1 and ADR-0056 respectively.
What is left:

- **`_fragment_span` still spans the `N` gap.** §5.1 and §7.6: ours is the only engine of five
  surveyed that hands the counter a contiguous interval including the gap, and its docstring argues
  for that on independent grounds (it also covers the inner mate gap). #461 put it out of scope
  deliberately — an aligner artifact is not a reason to change a counter — and the gate's `ambiguous`
  improvements are the aligner emitting fewer phantom gaps, not the counter handling them
  differently. Still its own question, and it gets *worse* on human and mouse.
- **`--peOverlapNbasesMin` on this chemistry** (§7.5) — the recommendation here that did not ship.
  Promoted by the mirrored-CIGAR signature of §6.3, and it needs its own measurement because it
  changes alignment for every read.
- **The ENCODE mismatch pair** (`outFilterMismatchNmax 999`, `outFilterMismatchNoverReadLmax 0.04`).
  Not adopted: it is a mismatch policy, and it interacts with the Tn5 clip's effect on the
  read-length denominator (§1.3), which nobody has measured.
- **Raising `sjdbScore`.** It is the bonus for crossing an *annotated* junction, so raising it makes
  annotated junctions more competitive against phantom novel ones — a plausible direction, pointing
  opposite to ENCODE's `sjdbScore 1`, and entirely unmeasured.
- **`day13_CF_1`'s confound.** The cell worst on every axis in the census is also the most degraded
  library, and it is the oldest age point of an aging study — so library quality and the biological
  variable move together, and nothing here separates them. Worth a full-plate age trend; not done.
- **`sacCer3` has no registered cap**, and ships unfilled by design (#461's Further Notes): its
  longest annotated intron is 2,483 bp over 380 introns, and no curator's pipeline backs a number the
  way WormBase backs the worm's. Either it earns a rationale of the same standard or yeast keeps
  today's behaviour. Do not invent a number to fill the cell.
- **Whether *cox-6B*'s 22 bp 5′ exon is a spliced leader** (§3.3, §6.1a). Testable by comparing that
  exon's sequence to SL1; nobody has. If it is, the second-longest "intron" in the worm genome is an
  annotation artifact of trans-splicing, and the top of the tail is thinner still.
- **What `BySJout` costs at plate scale.** Two-stage mapping, per cell, 784 of them. On the gate's
  four cells the wall clock went the other way, by 2.6×, with four flags moving at once — an
  observation, not a cost model.
- **Which motif class each observed junction has** (§2.2), and **the mate CIGARs** behind §2.3's four
  consistent readings. Both are now unanswerable rather than merely unanswered: the archives they were
  read from predate `jM`, and under the shipped flags those junctions no longer form, so only a
  deliberate realignment at the *old* settings with `jM` on could recover them. Nobody needs to.
- **How often a long *annotated* worm intron is stitched genomically rather than through the sjdb
  path** (§1.5) — the only case where a cap costs real alignments. Not measured directly; bounded in
  aggregate by Gate 2's −0.038%.
- **scRecounter's STAR version is loosely pinned** (`envs/star.yml`: `star=2.7`, no lockfile) and its
  built container lives in a private Arc registry, so §4.1 describes the *source*, not a verified
  running binary. Nothing in §4.1's argument depends on the patch version.
