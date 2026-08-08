# The Tn5 read-through: what it costs, what STAR demands of the flag, and what is still unmeasured

Measured 2026-08-07 for [#356](https://github.com/liuhlab/seqforge/issues/356) and
[#358](https://github.com/liuhlab/seqforge/issues/358), decided in ADR-0048. Three things: what an
unclipped Smart-seq3 library loses at the aligner, what STAR requires of the flag that fixes it, and
whether the fix actually recovers those reads. **All three are settled — the third by a controlled
before/after on real reads in §3.**

## The one-sentence answer

**Under 42% of a Smart-seq3 plate's reads map uniquely, the largest single category is reads
discarded for length, the mechanism is a denominator rather than a mapping failure, and clipping the
mosaic end recovers most of them** — measured at +21 points of unique mapping across four cells.
STAR's
`--outFilterScoreMinOverLread` and `--outFilterMatchNminOverLread` default to `0.66` *of the read
length*; a clipped base leaves that length while a soft-clipped one does not, so a read half of which
is adapter cannot clear 66% of itself however cleanly its genomic half aligns. STAR places it and
then files it under `unmapped: too short`.

## 1. The baseline, from the first production plate

784 in-house *C. elegans* Smart-seq3 worms, `ce11` / `WS298`, `map/star-umi`, pipeline
`ss3-ce11-ws298-ce321d3fc6d4` on `ircbc`. Read straight out of each cell's `Log.final.out`, across
the **63 samples** that had completed alignment when the run was stopped:

| metric | median | mean | p10 | p90 |
|---|---|---|---|---|
| `% of reads unmapped: too short` | **38.25** | **39.50** | 33.50 | 45.74 |
| `Uniquely mapped reads %` | 41.98 | 40.68 | 32.18 | 46.46 |
| `% of reads mapped to multiple loci` | 20.47 | 19.71 | 16.11 | 22.97 |

Three columns rule out the boring explanations, from the wider 43-sample read in discussion #354:
mismatch rate per base 0.32% (the bases that align, align cleanly), `mapped to too many loci` 0.03%
(repeat content is not eating them), and the loss sits in `too short` specifically — STAR's category
for a read whose best alignment failed a length-relative filter, not for one it could not place.

**This is the chemistry, not this deposit.** Deserranno et al., *BMC Genomics* 2026, report
**34.03%** for Smart-seq3xpress through zUMIs at stock settings. An unclipped library of this
chemistry loses about a third of its reads; ours loses 38%.

The sequence is the Tn5 mosaic end `CTGTCTCTTATACACATCT`, and both reference pipelines clip exactly
it — the authors' own `mouse_cross.yaml` and umite's `snakeconfig.yaml`
(see [`smartseq3-analysis-practice.md`](smartseq3-analysis-practice.md)).

## 2. What STAR demands of the flag — verified against the pinned binary

STAR 2.7.11b, parameter initialization only (no alignment). These are hard `FATAL`s, not warnings:

| invocation | result |
|---|---|
| PE, one `--clip3pAdapterSeq` value | **FATAL** — *"has to contain 2 values to match the number of mates … for no clipping use -"* |
| PE, two `--clip3pAdapterSeq`, no `--clip3pAdapterMMp` | **FATAL** — *"`--clip3pAdapterMMp` has to contain 2 values"* |
| PE, two `--clip3pAdapterSeq` + two `--clip3pAdapterMMp` | accepted |
| PE, `SEQ -` + two `--clip3pAdapterMMp` | accepted (`-` is the per-mate "no clipping" sentinel) |
| SE, one of each | accepted |
| `--clipAdapterType CellRanger4` + `--clip3pAdapterSeq` | **FATAL** — mutually exclusive |
| `--clipAdapterType CellRanger4` + `--clip5pAdapterSeq` | accepted |
| **under `--soloType`**, one `--clip3pAdapterSeq` value, no `--clip3pAdapterMMp` | accepted |
| **under `--soloType`**, `SEQ -` | **FATAL** — *"has to contain 1 values to match the number of mates"* |

Three consequences the implementation rests on:

- **The arity is per mate, and `map/star-umi`'s mate count is per SAMPLE** (`SAM PE` where the cell
  has a mate, `SAM SE` where it does not — one plate legally mixes both). A flag rendered once for
  the whole run is fatal on every cell of the other kind, which is why both flags are rendered from
  the single `mate_count` fact `--readFilesType` already used.
- **Under `--soloType` the mate count is 1, not 2**, so the two rows above are not the paired rows.
  The PE/SE rows were measured for this module's **non-solo** invocation and stay true for it;
  STARsolo peels the barcode read off the mate count (`readNmates = readNends - 1`), so a two-file
  solo run has one mate: `SEQ -` is a FATAL there, a single value is correct, and `--clip3pAdapterMMp`
  can be left at its scalar default. Measured on the same pin, 2026-08-07, for
  [#355](https://github.com/liuhlab/seqforge/issues/355). Two modules, one fact, two arities.
- **`CellRanger4` and a 3′ adapter cannot coexist.** *Historical, and kept because it records why the
  work happened:* this is what blocked `map/starsolo` from honouring a `read_through` at all. #355
  resolved it by making `clipAdapterType` a per-chemistry knowledge-base key, so a chemistry that
  declares a read-through declares `Hamming` and the pair never arises; the full per-family evidence
  is in [`starsolo-read-preprocessing-per-family.md`](starsolo-read-preprocessing-per-family.md).
  Note STAR's own `SOLUTION:` line on that error names the wrong family ("do not use
  `--clip5pAdapter*`") — three-prime is what is forbidden.

## 3. The clip recovers the reads — measured

Run 2026-08-07 on `GPU71FM` (chimera), four published Smart-seq3 cells from GSE207085 / PRJNA853582
(mouse, 150 bp paired-end), `mm10` + `star_gencode_vM23`. **The two conditions differ in one thing:
the presence of `--clip3pAdapterSeq CTGTCTCTTATACACATCT ... --clip3pAdapterMMp 0.1 0.1`.** Same
uBAM, produced by seqforge's own `io umi-extract` at the geometry the entry derives; same index,
same shared-memory load, every other flag identical to the shipped rule.

| cell | reads | uniquely mapped % | multi % | `too short` % | mismatch/base % |
|---|---|---|---|---|---|
| SRR19884905 | 63,647 | 46.34 → **69.52** | 3.52 → 7.86 | 49.96 → **18.87** | 0.43 → 0.41 |
| SRR19884906 | 59,557 | 57.53 → **72.63** | 4.11 → 7.04 | 38.26 → **17.85** | 0.44 → 0.44 |
| SRR19884907 | 44,022 | 38.56 → **65.13** | 2.28 → 6.25 | 58.97 → **22.25** | 0.48 → 0.45 |
| SRR19884909 | 12,857 | 27.11 → **47.09** | 2.33 → 10.94 | 70.53 → **32.02** | 0.91 → 0.85 |
| **mean** | | **42.39 → 63.59** (+21.21) | 3.06 → 8.02 | **54.43 → 22.75** (−31.68) | 0.57 → 0.54 |

**The unclipped arm independently reproduces §1.** These are different worms in no sense at all —
different lab, different species, different tissue, published rather than in-house — and left
unclipped they land on **42.39% uniquely mapped** against the in-house plate's 42.0% median, with an
even worse `too short` (54.43% vs 38.25%). Two independent deposits of this chemistry, the same
pathology. That is what makes this dataset a fair stand-in for the plate rather than a separate
question, and it rules out the reading that the in-house run was mis-prepared.

**The predicted signature is what happened, in all four cells: `too short` collapses and uniquely
mapped rises by most of the difference.** 58% of the `too short` category is eliminated, and the
mismatch rate does not rise — so the recovered alignments are not junk being waved through.

Where the freed reads go, in full, for SRR19884907 (the columns sum exactly, so nothing is
unaccounted for):

| fate | Δ points | share of the 36.72 freed |
|---|---|---|
| uniquely mapped | +26.57 | **72.4%** |
| multi-mapped | +3.97 | 10.8% |
| too many loci | +1.27 | 3.5% |
| unmapped: other | +4.91 | 13.4% |

That last row is the honest limit: `other` rises from 0.05% to 4.96% because a read that was almost
entirely adapter becomes a stub too short to seed. **Those reads were never recoverable** — clipping
reveals that, it does not cause it. About seven in ten freed reads become uniquely mapped.

The mechanism is directly visible in the read lengths STAR reports, and it is dose-dependent:
SRR19884907 lost 294 → 198 bp per fragment and gained +26.57 points of unique mapping, while
SRR19884905 lost 293 → 213 bp and gained +23.18. More adapter removed, more reads recovered.

This also settles the question §2 could not: **`--clip3pAdapterSeq` does apply to SAM/BAM input**
read through `--readFilesType SAM PE` with `--readFilesCommand samtools view`, which is the only
form `map/star-umi` uses.

Not answered here: the effect on the 784-worm plate specifically. Its baseline (38.25% `too short`)
is milder than these cells' 54.43%, so expect a smaller absolute gain. Reproduction:
`/scratch/zhoulab/hanliu/SS3_dev/GSE207085-clip-check/` on chimera (`clip_check.sh` beside it).

## 4. A macOS aside, recorded because it cost a day

This was attempted locally first and **cannot be done on macOS**: STAR reads **0 input reads** from
any input there — FASTQ or uBAM, 30/75/150 bp, x86-64 under Rosetta *and* the native osx-arm64
build. `Log.out` reports `end of input stream, nextChar=-1` before read #1, while malformed input
still errors correctly (`wrong read ID line format`), so the stream opens and delivers nothing. This
widens [#345](https://github.com/liuhlab/seqforge/issues/345), which recorded the defect as
osx-64-specific; `tests/conftest.py`'s skip keys on `sys.platform == "darwin"` and so is correct
either way.

One genuinely separate macOS defect was isolated on the way: `--sysShell /bin/bash` removes the
`Failed spawning readFilesCommand` error entirely (exit 102 → 0), and the 0-reads behaviour survives
it. So the two are independent, and neither is worth working around in a shipped rule —
`map/star-umi` runs on Linux.
