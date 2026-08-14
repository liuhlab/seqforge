# STAR on a chimeric reference: what the BAM actually says about which component a read came from

Measured 2026-08-14 for [#408](https://github.com/liuhlab/seqforge/issues/408), a research ticket
under map [#406](https://github.com/liuhlab/seqforge/issues/406). This is a **measurement of
behaviour**, not a decision: it says what STAR 2.7.11b does when the reference is one FASTA
concatenated from two or more assemblies with chromosome names suffixed `<chrom>__<component>`. What
that behaviour *decides* about the split contract belongs to the map and to whatever record the map
produces.

Every claim below is labelled **VERIFIED** (read off STAR's own source, its `parametersDefault`, or
its manual, with a locator) or **INFERRED** (a consequence I reasoned to and did not observe). An
inferred claim marked as such is useful; an inferred claim dressed as verified is a defect, so the
two are kept apart on purpose. Nothing here was run: no STAR invocation, no cluster job.

## The answer

**The load-bearing claim survives in substance and fails in two named places, and one of those two
is fatal to the split as the map currently describes it.** In detail:

- **A window is confined to one chromosome, so no non-chimeric alignment — and therefore no proper
  pair — can span two components.** A genuinely half-host/half-contaminant fragment does not become
  a cross-component pair; at default filters it becomes `unmapped: too short` and, because
  `--outSAMunmapped` is never passed by any seqforge module, it does not appear in the BAM at all.
  STAR's chimeric detection is OFF by default (`--chimSegmentMin 0`), which is the only machinery
  that could ever have written a cross-component record pair.
- **`NH` and `MAPQ` are computed over the whole chimera, not over a component.** They are both
  functions of `nTrOutSAM`, the count of loci reported anywhere in the concatenated genome. So the
  map's phrasing is right for the reads it describes and wrong at the boundary: STAR reports every
  locus within `--outFilterMultimapScoreRange` (default **1**) of the best, not only the best. A
  read that beats its cross-species hit **by one point** is still `NH:i:2`, `MAPQ:3`, and a
  component-local splitter would hand that record to the host BAM carrying a MAPQ a single-assembly
  run would have written as 255.
- **`--outFilterMultimapNmax` is counted across the whole chimera.** 9 host loci + 2 contaminant
  loci = 11 > 10 and the read is dropped as "mapped to too many loci". Confirmed; the flag that
  changes it is `--outFilterMultimapNmax` itself, and no seqforge module passes it today.
- **The fatal one: `map/star-umi` passes `--outSAMmultNmax 1`, so its BAM contains exactly one
  alignment record per template.** A splitter reading that BAM cannot observe "this template's
  alignments span more than one component", because N−1 of them were never written. The map's
  three-way routing rule is not implementable against that artifact as it stands. The ticket brief
  states the opposite — that `star-umi` deliberately does *not* set the flag — and the brief is
  wrong: `src/seqforge/workflows/map/star-umi.smk:453` passes it, and it is a settled module literal
  (ADR-0022, via #256 decision 7).
- **"Unmapped" is not component-attributable, by construction**: an unmapped record carries
  `refID = -1`, `POS = -1`, `MAPQ = 0`, `NH:i:0`, `HI:i:0`. The contract should say this outright.
  The one wrinkle: the *unmapped mate of a mapped read* carries `RNEXT`/`PNEXT` pointing at a
  chimeric chromosome, so an unmapped record can still name a component in a field a naive rewrite
  would miss.
- **Beyond `@SQ`, the reference is named in `@PG CL:` and in `@CO`, both of which embed
  `--genomeDir`** — the chimera's index path — verbatim. `@HD` and `@RG` do not name it. Any
  "restore the header a single-assembly run would have produced" clause has to reach those two lines
  or record an explicit exception.

## What was read

STAR **2.7.11b** — the version the `align-rna` image pins, per `src/seqforge/workflows/map/star-umi.smk:212`
("2.7.11b in the `align-rna` image (2026-08-05)"). Source tarball
`https://codeload.github.com/alexdobin/STAR/tar.gz/refs/tags/2.7.11b`, unpacked and read locally;
`source/…:line` locators below are into that tree, and are stable for that tag. `doc/STARmanual.pdf`
ships in the same tarball and is cited by section and page. `source/parametersDefault` is STAR's own
authoritative default-and-description table, from which the manual's parameter appendix is generated.

Two secondary sources appear, both for corroboration only and never to establish a fact: a reply
from the STAR author on a combined-genome question,
[alexdobin/STAR#748](https://github.com/alexdobin/STAR/issues/748), and one peer-reviewed
measurement of combined-reference error, quoted in §2.

## 0. The flags this pipeline actually passes

Read off the two modules a chimera would run through today.

`map/star-umi` (`src/seqforge/workflows/map/star-umi.smk:445-453`):

```text
--runMode alignReads --genomeDir <index> --runThreadN N --genomeLoad LoadAndKeep
--readFilesIn <ubam> --readFilesType SAM {PE|SE} --readFilesCommand samtools view
--readFilesSAMattrKeep All  [--clip3pAdapterSeq …]  --outFileNamePrefix <p>
--outSAMtype BAM SortedByCoordinate --limitBAMsortRAM <bytes> --outSAMmultNmax 1
```

`map/star` (`src/seqforge/workflows/map/star.smk:247-253`): the same shape minus the uBAM input and
minus `--outSAMmultNmax`, plus `--quantMode GeneCounts`.

What matters for a chimera is what is **absent**, because every absent flag is a default:

| flag | value in effect | consequence on a chimera |
|---|---|---|
| `--outFilterMultimapNmax` | **10** (default) | counted across all components — §2 |
| `--outFilterMultimapScoreRange` | **1** (default) | near-best cross-component hits enter `NH` — §3 |
| `--outSAMunmapped` | **None** (default) | no unmapped record reaches any BAM at all — §5 |
| `--chimSegmentMin` | **0** (default) | chimeric detection OFF; no cross-component supplementaries — §1 |
| `--outSAMattrRGline` | unset | no `@RG` line to rewrite — §6 |
| `--outSAMattributes` | **Standard** = `NH HI AS nM` | plus input tags kept by `--readFilesSAMattrKeep All` |
| `--outSAMmultNmax` | **1** in `star-umi`; **−1** in `star` | the split is blind in one module and not the other — §4 |

That last row is the whole story of this document, and it means **the two modules do not behave the
same way under a splitter**. `map/star` writes every reported locus; `map/star-umi` writes one.

One aside for whoever prices `map/star`'s twin later: that module gets its counts from STAR itself,
`--quantMode GeneCounts` (`src/seqforge/workflows/map/star.smk:250`), which emits a single
`ReadsPerGene.out.tab` over whatever GTF the index was built with — for a chimera, the merged
annotation, both components' genes in one table. Splitting *that* is a different operation from
splitting a BAM, and the map's constraint 3 (a matrix per component, against its own annotation)
does not currently say which it means for `map/star`. Out of scope here; flagged because it is not
in the map's "Not yet specified" list either.

## 1. Can a proper pair span two components?

**No. VERIFIED.**

STAR builds alignments inside *windows*, and a window carries exactly one chromosome. A new window
records its chromosome once, `WC[iWin][WC_Chr] = mapGen.chrBin[aBin >> P.winBinChrNbits]`
(`source/ReadAlign_createExtendWindowsWithAlign.cpp:66`), and an align may only be merged into a
neighbouring window when the two agree on that chromosome — the guard appears twice, once per
direction:

```cpp
flagMergeLeft  = flagMergeLeft  && (mapGen.chrBin[iBin>>P.winBinChrNbits]==mapGen.chrBin[aBin>>P.winBinChrNbits]);
flagMergeRight = flagMergeRight && (mapGen.chrBin[iBin>>P.winBinChrNbits]==mapGen.chrBin[aBin>>P.winBinChrNbits]);
```

(`source/ReadAlign_createExtendWindowsWithAlign.cpp:28` and `:47`; window extension in
`source/ReadAlign_stitchPieces.cpp:100` and `:107` carries the same guard.) `chrBin` maps a genome
bin to exactly one chromosome (`source/Genome.cpp:214`, `chrBin[ii]=ichr-1`) because
`--genomeChrBinNbits` guarantees "each chromosome will occupy an integer number of bins"
(`source/parametersDefault:68-69`).

Both mates of a paired alignment live in one `Transcript`, distinguished by a mate gap in the exon
list, and `nMates` is 2 only when that gap is present (`source/ReadAlign_alignBAM.cpp:70-82`). One
`Transcript` has one `Chr`. The record shape follows mechanically: for a two-mate alignment `RNEXT`
is written as the alignment's *own* chromosome —

```cpp
//6: next refID
if (nMates>1) { pBAM[6]=trOut.Chr; } else if (mateChr<genOut.nChrReal) { pBAM[6]=mateChr; } else { pBAM[6]=-1; };
```

(`source/ReadAlign_alignBAM.cpp:548-555`) — so a record with `FLAG & 0x2` set
(`source/ReadAlign_alignBAM.cpp:191-193`) has `RNAME == RNEXT` by construction. **A proper pair
whose two mates name different components cannot be emitted.**

### So where does a genuinely half-host / half-contaminant fragment go?

Two hops, and the second is the surprising one.

First hop, VERIFIED: with no window holding both mates, the best transcript holds one mate, so
`mateMapped[0] && mateMapped[1]` is false and STAR sets `unmapType=4`, "unmapped mate of a mapped
paired-end read" (`source/ReadAlign_outputAlignments.cpp:209-214`; manual §5.2.2 p. 13 and §5.4
p. 14 give the same code). The mapped mate would carry `0x1|0x8` and no `0x2`
(`source/ReadAlign_alignBAM.cpp:189-190`), and the unmapped mate would be a separate record.

Second hop, VERIFIED and load-bearing: **at default filters it usually never gets that far.** STAR's
length filters are normalised to the *sum* of mate lengths —

```cpp
Lread=readLength[0]+readLength[1]+1;                        // ReadAlign_oneRead.cpp:36
… (trBest->maxScore < (intScore)(P.outFilterScoreMinOverLread*(Lread-1)))
   || (trBest->nMatch < (uint)(P.outFilterMatchNminOverLread*(Lread-1)))   // ReadAlign_mappedFilter.cpp:8-9
   → statsRA.unmappedShort++; unmapType=1;
```

with both thresholds at **0.66** (`source/parametersDefault:480-487`, "normalized to read length
(sum of mates' lengths for paired-end reads)"). For a 2×150 fragment the bar is 0.66 × 300 ≈ 198,
and a single 150 nt mate cannot clear it however cleanly it aligns. **INFERRED** (the arithmetic is
mine, the filter and the `Lread` definition are verified): a balanced-length PE fragment that is
genuinely half host and half contaminant is filed as `unmapped: too short` and, with
`--outSAMunmapped None`, is written nowhere. This is the same denominator effect
`docs/research/smartseq3-tn5-read-through.md` measured for the Tn5 read-through, arriving from a
different direction.

An **index hop** is the same shape and the opposite outcome: an index hop delivers a *whole*
foreign fragment, both mates from the contaminant, under a host cell's barcode. Both mates align to
one component, the pair is proper, and nothing in the BAM distinguishes it from real contamination.
**INFERRED.** A splitter cannot see index hops; it routes them to the component they came from,
which is the honest answer but not a detection.

### Chimeric detection, and what "off by default" implies

`--chimSegmentMin 0`, "if ==0, no chimeric output" (`source/parametersDefault:690-691`). Manual §6
p. 15: chimeric segments are precisely those that "belong to different chromosomes, or different
strands, or are far from each other" — i.e. STAR's chimeric machinery is *exactly* the machinery
that could emit a cross-component template, and it is switched off. When it is on and
`--chimOutType WithinBAM` is chosen (`source/parametersDefault:682-688`), chimeric alignments enter
the main BAM with `0x800` set on the supplementary records
(`source/ReadAlign_alignBAM.cpp:200`, `alignType` −11/−12/−13 documented at `:47-55`) and the
default `Junctions` mode writes `Chimeric.out.junction` instead.

**The implication for the contract, stated plainly:** with chimeric detection off, the BAM contains
no record whose *own* alignment spans components. Every cross-component signal is therefore a
*multi-locus* signal — several alternative placements of the same read — never a *split-read*
signal. A splitter that reasons about "spanning" is reasoning about a set of alternative loci, not
about one fragment straddling a boundary. If a later effort ever turns chimeric detection on, the
routing rule needs a fourth branch and the `0x800` records need a home; nothing in the current
design anticipates that.

## 2. `--outFilterMultimapNmax` across the whole chimera

**Counted across the whole chimera. VERIFIED. The exposure is real and one-sided.**

`multMapSelect` scans **every** window of the read, over the whole concatenated genome, and collects
every transcript within `--outFilterMultimapScoreRange` of the best:

```cpp
for (uint iW=0; iW<nW; iW++) {            // scan windows  — ReadAlign_multMapSelect.cpp:26
  for (uint iTr=0; iTr<nWinTr[iW]; iTr++) {
    if ( (trAll[iW][iTr]->maxScore + P.outFilterMultimapScoreRange) >= maxScore ) { … nTr++; }
```

and then

```cpp
if (nTr > P.outFilterMultimapNmax || nTr==0) { return; }   // :46
…
} else if (nTr > P.outFilterMultimapNmax) {                 // ReadAlign_mappedFilter.cpp:15-17
    statsRA.unmappedMulti++; unmapType=3;
```

There is no chromosome, contig-group or component term anywhere in that comparison.
`source/parametersDefault:463-465` says the same in words — "maximum number of loci the read is
allowed to map to. Alignments (all of them) will be output only if the read maps to no more loci
than this value" — as does manual §4.1 p. 10. **9 host loci + 2 contaminant loci = 11 > 10, and the
read is discarded entirely, with `uT:A:3` / "mapped to too many loci" in `Log.final.out`
(`source/Stats.cpp:132-133`), where a `ce11`-only run would have kept all 9.** Confirmed.

**The flag that changes it is `--outFilterMultimapNmax`.** Raising it is not free: the manual (§4.1
p. 11) requires `--winAnchorMultimapNmax ≥ --outFilterMultimapNmax`, and `winAnchorMultimapNmax`
(default 50) "also controls the overall sensitivity of mapping: increasing it will change (improve)
the mapping of unique mappers as well, though at the cost of slower speed" — i.e. raising the cap to
protect the split changes the alignment of reads that have nothing to do with the split. That is a
reason to leave it alone, not a reason to raise it.

### Characterising the exposure

The shape of the exposure, all **INFERRED** from the mechanism above:

- **It is one-sided and it is a loss, never a gain.** Adding a component can only add loci, so
  `nTr` on a chimera is ≥ `nTr` on either component alone. A read can cross the cap that would not
  have; no read can fall back under it.
- **Only reads already near the cap are exposed.** A read at 1 host locus is at no risk from any
  plausible number of contaminant loci. The population at risk is reads with 9 or 10 host loci —
  already the tail of the repeat distribution — that additionally hit the other component.
- **The two conditions are close to independent and both are small**, so their conjunction is very
  small. For the `ce11_ecHT115` case specifically the second condition is doubly unlikely: a
  *C. elegans* read multimapping at 9–10 loci is repeat-derived, and *E. coli* HT115 shares
  essentially no repeat family with a nematode. **This is a judgement, not a number** — the
  measurement that would replace it is §"What would settle this cheaply".
- **It is invisible in the output.** The discarded read appears only as a `Log.final.out` counter;
  it is not in the BAM, so no splitter and no downstream metric can recover it. The only honest
  handle is the delta in `% of reads mapped to too many loci` between a chimeric run and a
  single-assembly run of the same cells.
- **It bites `map/star` and `map/star-umi` identically**, because the cap is applied before any
  output flag is consulted.

For scale from the literature, cited as a secondary source and not as our measurement: Choi et al.,
*BMC Bioinformatics* 23 (2022), "Expression-based species deconvolution and realignment removes
misalignment error in multispecies single-cell data"
([PMC9063264](https://pmc.ncbi.nlm.nih.gov/articles/PMC9063264/)), report that on combined-reference
multispecies single-cell data, "all error in combined reference accounted for only 0.4–1.4% of total
reads, these reads were concentrated to few genes, leading to strong false signals", with
"13,000–300,000 fewer reads aligned to human genes in the combined reference than in the human
reference". That paper measures a different quantity — misassignment, not cap-crossing — but it is
direct evidence that the aggregate effect of a combined reference is around a percent while the
per-gene effect is not.

## 3. `MAPQ` semantics

**The convention is confirmed; "computed over the reported alignment count" is confirmed with one
correction; "already component-local" is FALSE at the boundary.**

VERIFIED, `source/ReadAlign_alignBAM.cpp:276-283`:

```cpp
MAPQ=P.outSAMmapqUnique;
if      (nTrOut>=5) { MAPQ=0; }
else if (nTrOut>=3) { MAPQ=1; }
else if (nTrOut==2) { MAPQ=3; }
```

with `outSAMmapqUnique` defaulting to 255 (`source/parametersDefault:372-373`). The map's stated
convention — 255 unique / 3 for two loci / 1 for three or four / 0 for five or more — is exactly
this. The manual states it as a formula, §5.2.1 p. 11: "The mapping quality MAPQ (column 5) is 255
for uniquely mapping reads, and `int(-10*log10(1-1/Nmap))` for multi-mapping reads. This scheme is
same as the one used by TopHat". The identical ladder appears in the SAM text writer
(`source/ReadAlign_outputTranscriptSAM.cpp:210-216`) and the splice-graph writer
(`source/ReadAlign_outputSpliceGraphSAM.cpp:61-67`), so there is one convention and three copies of
it, not three conventions.

`nTrOut` is the *reported* count, and `writeSAM` passes the **full** reported count into every
writer even when the write is truncated:

```cpp
auto nTrOutWrite=min(P.outSAMmultNmax,nTrOutSAM);          // :168  — how many records get written
for (uint iTr=0;iTr<nTrOutWrite;iTr++) {
    outputTranscriptSAM(*(trOutSAM[iTr]), nTrOutSAM, iTr, …);   // :177 — nTrOutSAM, not nTrOutWrite
    alignBAM            (*(trOutSAM[iTr]), nTrOutSAM, iTr, …);  // :184 — same
```

(`source/ReadAlign_outputAlignments.cpp:168-184`). So MAPQ is a function of how many loci STAR
*decided to report*, not of how many records it *wrote*. Confirmed.

**The correction, and it is the one that dents the map's claim.** "Reported" is not "best-scoring".
`--outFilterMultimapScoreRange` defaults to **1** (`source/parametersDefault:460-461`), and
`multMapSelect` admits every transcript whose score is within that range of the maximum
(`source/ReadAlign_multMapSelect.cpp:28`). The map says a read "that beats its cross-species hit
already carries the `NH` and `MAPQ` a single-assembly run would have given it". That is true when it
beats it by **2 or more**. When it beats it by **1** — one mismatch' worth, since AS is "+1/−1 for
matches/mismatches" (manual §5.2.2 p. 12) — the cross-species locus is still reported, and the read
carries `NH:i:2 MAPQ:3` where `ce11` alone would have given `NH:i:1 MAPQ:255`.

**INFERRED, but a direct consequence:** a component-local splitter that routes by RNAME alone will
place such a read into the host BAM with a degraded MAPQ and an inflated NH, and no arithmetic in
the map notices, because the map's "no MAPQ or NH surgery" property is stated for reads whose
alignments all land in one component — and by RNAME this read's *written* alignments may well all
land in one component (see §4). **The margin-of-1 read is the exact case where "no surgery" and
"correct" come apart.** Whether that population is large enough to matter is unmeasured.

This is the same phenomenon alexdobin describes on the combined-genome question in
[STAR#748](https://github.com/alexdobin/STAR/issues/748): "when you map to the combined genome, the
alignments to the human genome are always better (**or equal, if you see human-EBV multimappers**)"
— the parenthesis is the boundary case, in the author's own words.

## 4. `NH`, `HI`, and how a template's alignments are grouped

### `NH` — VERIFIED, and it is chimera-wide

`NH` is written from the same `nTrOut` that feeds MAPQ:

```cpp
case ATTR_NH: attrN+=bamAttrArrayWriteInt(nTrOut,"NH",attrOutArray+attrN,P); break;   // :293
case ATTR_HI: attrN+=bamAttrArrayWriteInt(iTrOut+P.outSAMattrIHstart,"HI",…); break;  // :296
```

(`source/ReadAlign_alignBAM.cpp:292-297`). Manual §5.2.1 p. 11–12 is explicit about the truncation
case: "Note that `NH:i:` tag in STAR will still report the actual number of loci that the reads map
to, while the number of reported alignments for a read in the SAM file is
`min(NH, --outSAMmultNmax)`."

**Version caveat, VERIFIED, and it matters for anyone re-running old data:** this was a *bug* until
STAR **2.7.9a** (2021-05-05) — `CHANGES.md:84`, "Issue #1180: Output the actual number of alignments
in NH attributes even if `--outSAMmultNmax` is set to a smaller value." A BAM produced by an earlier
STAR under `--outSAMmultNmax 1` carries `NH:i:1` for everything. Our 2.7.11b has the fix; a fixture
or a legacy BAM might not.

**So `NH` is a chimera-wide count, and seqforge's umite port reads it directly**
(`src/seqforge/workflows/umite/count.py:129`, `HITS_TAG = "NH"`; the reasoning is at `:66-74`, and
the fate metric at `:922-927`, "Fragments carrying `NH > 1` — placed at several loci, so no gene
owns them"). Consequences, chained:

- A cross-component multimapper arrives at the counter as `NH > 1` and is routed to
  `_multimapping`. **No component's matrix ever counts it.** This is a genuine safety property the
  map does not currently claim, and it holds *today*, before any splitter exists.
- Symmetrically: a read that would have been `NH:i:1` on `ce11` alone and is `NH:i:2` on the chimera
  is **lost from the host's matrix** — moved from a gene into `_multimapping`. That is the counting
  face of the §3 margin-of-1 problem, and it is a systematic under-count of the host, proportional
  to how much of the host's transcriptome has a near-hit in the other component. **INFERRED.**
- The frozen umite fixture gives the order of magnitude for how much traffic rides on `NH` at all:
  1 640 of 12 977 aligned read names, **12.6%**, carry `NH > 1` on a single-assembly run
  (`src/seqforge/workflows/umite/count.py:70`). That is the population whose `NH` a chimera can
  perturb.

### `HI`, and what a splitter actually gets to iterate over

`HI = iTrOut + --outSAMattrIHstart` (default 1) is an **output-order index**, not a stable locus id
(`source/ReadAlign_alignBAM.cpp:296`; `source/parametersDefault:346-347`).

Grouping depends entirely on the output type, and the manual is explicit (§5.3 p. 13–14):

- `BAM Unsorted`: "The paired ends of an alignment are always adjacent, and multiple alignments of a
  read are adjacent as well."
- `BAM SortedByCoordinate`: **no such guarantee**, and this is what both seqforge modules write. The
  coordinate sort is by genomic position (`source/BAMoutput.cpp:77-94`); the per-record tiebreak key
  is `(iReadAll<<32) | (iTr<<8) | mate` (`source/ReadAlign_outputAlignments.cpp:200`), which orders
  records *within* a coordinate bin, not across the file.

**So a splitter iterating a seqforge BAM does not get bundles.** To see all of a template's
alignments it must either name-sort (a full extra pass and, per
`src/seqforge/workflows/map/star-umi.smk:399-403`, 2× peak disk that the module deliberately refuses
to pay) or hold a QNAME → components map across the whole file.

### The fatal interaction: `--outSAMmultNmax 1`

**VERIFIED, and it is the finding that contradicts the map.** `map/star-umi` passes
`--outSAMmultNmax 1` (`src/seqforge/workflows/map/star-umi.smk:453`); it is a settled module literal
(ADR-0022 via #256 decision 7, recorded at `src/seqforge/workflows/umite/count.py:67`). Therefore
`nTrOutWrite = min(1, nTrOutSAM) = 1` and **exactly one alignment record per template reaches the
BAM**, whatever `NH` says.

The consequences for the split contract:

1. **"A template whose alignments span more than one component" is not observable.** The splitter
   sees one RNAME. `NH:i:3` tells it three loci exist; it tells it nothing about *where*. The map's
   three-way routing — one component / ambiguous / unmapped — cannot be computed from this artifact.
   A cross-component multimapper and a within-component multimapper are byte-indistinguishable
   except by re-alignment.
2. **Which component the single record lands in is a tie-break, not a decision.** When
   `--outSAMmultNmax != -1`, `multMapSelect` partitions `trMult` so that top-scoring alignments come
   first (`source/ReadAlign_multMapSelect.cpp:62-68`) and marks `trMult[0]` primary (`:87-88`). Among
   equal-best alignments the survivor is the first in *window order* under the default
   `--outMultimapperOrder Old_2.4` (`source/parametersDefault:282-285`; manual §5.2.1 p. 11, "the
   order of the multi-mapping alignments for each read is not truly random"). Window order is the
   order STAR created windows while scanning the read's anchor seeds, "going through ordered
   positions in the suffix array" (`source/ReadAlign_stitchPieces.cpp:40-49`) — **suffix-array order,
   not coordinate order and not component order**. It is deterministic for a given index and read,
   and it is not something a splitter, a concatenation order or a flag can steer. **So for a read
   that ties exactly across components, which component's BAM it lands in is arbitrary in the precise
   sense that nothing downstream can predict or control it.** The repo already records the tie-break
   half of this at `src/seqforge/workflows/__init__.py:287-292`.
3. **Every written record is primary.** `trMult[0]->primaryFlag=true`, so no `0x100` survives and
   `cram.py`'s `-F 0x100` (`src/seqforge/workflows/cram.py:148`) is the cheap invariant its docstring
   says it is. A splitter must not expect secondaries to exist.
4. **`HI` is always `HI:i:1`** on this module's BAM, since only `iTrOut == 0` is written. It carries
   no information a splitter can use.
5. **`map/star` does not share this**, because it omits the flag: its BAM carries every reported
   locus (up to 10), so spanning *is* observable there — after a name sort or a QNAME pass.

The map's constraint 4 already forces `map/star-umi`'s chimeric twin to be a standalone `.smk` copy.
**INFERRED, and this is the cheap way out:** that copy is free to drop `--outSAMmultNmax 1`, or to
set it to `--outSAMmultNmax -1`, at the cost of a larger sort and a larger BAM (the repo measured
~18% of the sort spent on records `-F 0x100` later discards, `src/seqforge/workflows/__init__.py:280-282`
— so dropping the flag re-incurs roughly that). It cannot be dropped *silently*: it changes
`workflow_version`, hence `run_id`, and the counter's `_multimapping` behaviour is defined against
`NH` rather than bundle length precisely so the flag can move without moving the counts
(`src/seqforge/workflows/umite/count.py:66-74`). That last property is what makes this fixable
rather than fatal.

## 5. `--outSAMunmapped`, and whether "unmapped" is component-attributable

**It is not, by construction. VERIFIED.**

First, the state of the world: **no seqforge module passes `--outSAMunmapped`**, so the default
`None` — "no output" (`source/parametersDefault:349-353`) — is in force and **no unmapped record
reaches any BAM seqforge writes today.** The map's routing branch "an unmapped read stays as STAR
left it" is, against the current modules, a branch over an empty set. The unmapped population is
visible only in `Log.final.out`.

If a chimeric twin turns it on, this is what it gets. An unmapped record is written with:

```cpp
if (alignType<0) { pBAM[1]=trOut.Chr; } else { pBAM[1]=(uint32) -1; }   // refID  — :521-525
if (alignType<0) { pBAM[2]=…;         } else { pBAM[2]=(uint32) -1; }   // POS    — :527-532
```

`MAPQ` left at its initialisation of 0 (`source/ReadAlign_alignBAM.cpp:116`), and

```cpp
attrN+=bamAttrArrayWriteInt(0,"NH",…);  attrN+=bamAttrArrayWriteInt(0,"HI",…);
attrN+=bamAttrArrayWriteInt(trOut.maxScore,"AS",…);  attrN+=bamAttrArrayWriteInt(trOut.nMM,"nM",…);
attrN+=bamAttrArrayWrite((to_string((uint) alignType)).at(0), "uT",…);
```

(`source/ReadAlign_alignBAM.cpp:156-161`). So: **`RNAME = *`, `POS = 0`, `MAPQ = 0`, `NH:i:0`,
`HI:i:0`, plus a `uT` tag giving the reason** — 0 no seed/window, 1 too short, 2 too many
mismatches, 3 too many loci, 4 unmapped mate of a mapped pair (manual §5.2.2 p. 13 and §5.4 p. 14;
`source/ReadAlign_mappedFilter.cpp:4-18` sets 0–3 and
`source/ReadAlign_outputAlignments.cpp:213` sets 4). In the coordinate-sorted BAM every such record
goes to the final bin — `if (bamIn32[1] == ((uint32) -1)) { iBin=P.outBAMcoordNbins-1; }`,
`source/BAMoutput.cpp:89-90` — so they are appended at the end of the file, ordered by read number
(`source/BAMbinSortUnmapped.cpp:16-48`).

**There is no field on such a record that names a component**, and there could not be: STAR did not
place the read. **The contract should say this explicitly**, and should say what it does with the
pile — an unmapped record is not evidence of *either* organism, and duplicating it into every
component's BAM would double-count it while dropping it would lose it. Neither the map nor this
document decides that; the map's "Not yet specified" list should grow a line for it.

**One wrinkle a naive rewrite will miss, VERIFIED.** For `uT:A:4` — the unmapped mate of a mapped
read — STAR fills `RNEXT`/`PNEXT` from the *mapped* mate: `mateChr=trOut.Chr; mateStart=…`
(`source/ReadAlign_alignBAM.cpp:131-134`), reaching `pBAM[6]=mateChr` at `:551-552`. So the record
is `RNAME = *` but `RNEXT = <chrom>__<component>`. **An unmapped record can carry a chimeric
chromosome name in `RNEXT`.** Any `@SQ`/RNAME rewrite that walks RNAME only will leave a dangling
reference id behind and produce a BAM that fails `samtools quickcheck`-grade validation against the
rewritten header.

## 6. What besides `@SQ` names the reference

VERIFIED, entirely from `source/samHeaders.cpp` — one function builds the whole header.

| line | what it emits | names the chimera? |
|---|---|---|
| `:29-31` | `@SQ SN:<chrName> LN:<chrLength>` for every real chromosome | **yes** — this is the `<chrom>__<component>` set |
| `:35-54` | extra `@SQ` lines read verbatim from `<genomeDir>/extraReferences.txt` | **yes**, if the index was built with on-the-fly insertions |
| `:56-62` | optional extra `@PG` from `--outSAMheaderPG` | only if the user puts it there |
| `:64` | `@PG ID:STAR PN:STAR VN:<version> CL:<commandLineFull>` | **yes — this is the one that hurts** |
| `:66-76` | `@CO` lines slurped from `--outSAMheaderCommentFile` | only if the user puts it there |
| `:79-81` | `@RG` from `--outSAMattrRGline` | no; and seqforge passes none, so there is no `@RG` at all |
| `:84` | `@CO user command line: <commandLine>` | **yes** |
| `:86` | `P.samHeaderExtra` | no |
| `:88-100` | `@HD VN:1.4`, plus `SO:coordinate` for the sorted BAM | no |

`commandLineFull` is not the argv — it is STAR's *final effective* command line, rebuilt from every
parameter whose `inputLevel > 0`, i.e. every parameter the user set from any source
(`source/Parameters.cpp:446-456`, logged as "Final effective command line"). `commandLine` is the
verbatim argv (`source/Parameters.cpp:332-362`). **Both contain `--genomeDir <path to the chimera's
index>`**, and `commandLineFull` additionally contains every other flag the run used.

So a "restore the header a single-assembly run would have produced" clause has exactly three lines
to deal with, and they are not equal:

- **`@SQ`** — must be rewritten. Already in the map.
- **`@PG … CL:`** — names the chimera index in a free-text field. Rewriting it fabricates a command
  that was never run; leaving it makes the per-component BAM self-describing as chimera-derived,
  which is arguably *correct* provenance. **This is a real fork in the contract and the map does not
  mention it.**
- **`@CO user command line:`** — the same fork, one line down.

**INFERRED, offered as a recommendation and not a decision:** leaving `@PG`/`@CO` alone and adding a
`@PG` of the splitter's own (with `PP:STAR`, which is what the SAM spec's `PP` chain is for) is both
honest and cheaper than rewriting, and it means the byte-for-byte "identical to a single-assembly
run's header" bar in map constraint 6 cannot be met literally. **That bar needs restating as
"identical `@SQ` block and `@HD`" or it will fail on its first synthetic round-trip** — including on
the `tinyEcDub` fixture, whose whole point is to break naive assumptions.

Two further header facts worth having: the binary BAM reference dictionary is written from
`chrNameAll`/`chrLengthAll`, which is `chrName` plus any extra references
(`source/samHeaders.cpp:33-34`, `:106`; `source/BAMbinSortByCoordinate.cpp:63`), so the text header
and the binary dictionary must be rewritten together. And STAR ships its own *narrow* answer to
"extra sequences in one index" — `--outSAMfilter KeepOnlyAddedReferences` /
`KeepAllAddedReferences` (`source/parametersDefault:396-399`), implemented as an index-range test
`trOutSAM[itr]->Chr < mapGen.genomeInsertChrIndFirst`
(`source/ReadAlign_outputAlignments.cpp:141-164`). It is a *filter on one output*, not a split into
N, it keys on **index contiguity rather than on names**, and it applies only to sequences inserted
at mapping time with `--genomeFastaFiles`, not to a genome built chimeric. **It is not usable here**
— but it is confirmation that STAR itself has no name-based notion of a component, which is why the
map's constraint 1 (names belong to `liulab-genome`) has nothing on the STAR side to consume it.

## 7. Verdict on the map's standing decision 2

The decision reads: *"with the spanning templates removed, each component's BAM needs an `@SQ` and
RNAME rewrite and no MAPQ or NH surgery at all, because STAR only reports best-scoring loci — a read
that beats its cross-species hit already carries the `NH` and `MAPQ` a single-assembly run would have
given it."*

Graded clause by clause:

| clause | verdict |
|---|---|
| a template's alignments all land in one component ⇒ no MAPQ/NH surgery needed | **TRUE, and for a stronger reason than stated.** MAPQ and NH are pure functions of `nTrOutSAM`. If every reported locus is in one component, `nTrOutSAM` is what a single-assembly run would have computed, so both fields are already right. Nothing to fix. |
| "STAR only reports best-scoring loci" | **FALSE as written.** It reports loci within `--outFilterMultimapScoreRange` (default 1) of the best. A read beating its cross-species hit by exactly 1 is still `NH:i:2 MAPQ:3`. |
| the routing rule is computable from the BAM | **FALSE for `map/star-umi` as it stands** — `--outSAMmultNmax 1` writes one record per template, so "spans more than one component" is unobservable. TRUE for `map/star`. |
| `@SQ` + RNAME rewrite is the whole header job | **INCOMPLETE.** `@PG CL:` and `@CO` embed `--genomeDir`. And `RNEXT` on a `uT:A:4` record names a chromosome too. |
| "an unmapped read stays as STAR left it" | **VACUOUS today** — `--outSAMunmapped` defaults to `None` and no module passes it, so there are no unmapped records in any seqforge BAM. |

**Standing decision 2 survives as a design intent and does not survive as a statement about the
artifact `map/star-umi` produces.** The property it wants — no arithmetic on the species BAMs — is
real and rests on a mechanism (MAPQ and NH are functions of the reported-locus count) that is more
robust than the reason given for it. What fails is the *premise* that a splitter can see the loci it
needs to see. The cheapest repair is a one-flag change in the chimeric twin the map already requires
to exist, and the counter is already insulated from that flag by design. The decision should be
amended rather than reversed.

## What is INFERRED and not verified, collected

Kept in one place so nobody has to re-derive which is which:

1. That a balanced-length PE fragment split across components is filed `unmapped: too short` rather
   than as a one-mate alignment. The filter and `Lread` are verified; the arithmetic that a 150 nt
   mate cannot clear 0.66 × 300 is mine and is unobserved.
2. That an index hop presents as an ordinary within-component proper pair and is undetectable.
3. That the `--outFilterMultimapNmax` exposure is negligible for `ce11_ecHT115` specifically. The
   mechanism is verified; the magnitude is a judgement.
4. That the margin-of-1 population — reads beating a cross-species hit by exactly one point — is
   what makes "no surgery" and "correct" come apart, and its size. Unmeasured in either direction.
5. That the systematic host under-count via `NH:i:1 → NH:i:2` is real. Follows from verified
   mechanics; never observed.
6. That dropping `--outSAMmultNmax 1` in the chimeric twin costs roughly the ~18% sort overhead the
   repo measured for the reverse change, and nothing else.
7. That leaving `@PG`/`@CO` alone and chaining a splitter `@PG` with `PP:STAR` is the better of the
   two forks. A recommendation, not a measurement.
8. That `--genomeChrBinNbits` needs scaling for the 94-sequence `ce11_ecHT115` index (manual's own
   guidance, `min(18, log2[max(GenomeLength/NumberOfReferences, ReadLength)])`). This is a memory
   and build concern belonging to `liulab-genome`, and it does not touch correctness of the split.

## What would settle the inferred claims cheaply

All of it fits inside the map's existing bar (constraint 6, the synthetic round-trip on
`tinyCe`/`tinyEc`/`tinySc`/`tinyEcDub`), and none of it needs a benchmark re-run:

- **Claims 1 and 2**: synthesise a fragment with mate 1 from `tinyCe` and mate 2 from `tinyEc`, run
  the twin with `--outSAMunmapped Within`, and read the `uT` tag. One cell, one read.
- **Claims 4 and 5**: plant a read that matches `tinyCe` exactly and `tinyEc` with one mismatch, and
  read `NH`. Confirms or refutes the score-range boundary in a single record.
- **Claim 3**: on the real plate, diff `% of reads mapped to too many loci` in `Log.final.out`
  between the existing `ss3-ce11-ws298-ce321d3fc6d4` run and a chimeric run of the same cells. The
  numbers are already on disk for one side.
- **Claim 6**: measured by the twin's first real run; no separate experiment.

Claims 7 and 8 are decisions and a build parameter respectively, not measurements, and belong to the
map and to `liulab-genome`.
