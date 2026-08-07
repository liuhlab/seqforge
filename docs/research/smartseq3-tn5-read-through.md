# The Tn5 read-through: what it costs, what STAR demands of the flag, and what is still unmeasured

Measured 2026-08-07 for [#356](https://github.com/liuhlab/seqforge/issues/356), decided in ADR-0048.
Three separate things, and only the first two are settled: what an unclipped Smart-seq3 library loses
at the aligner, what STAR requires of the flag that would fix it, and whether the fix actually
recovers those reads — which is **not** answered here.

## The one-sentence answer

**Under 42% of a Smart-seq3 plate's reads map uniquely, the largest single category is reads
discarded for length, and the mechanism is a denominator rather than a mapping failure.** STAR's
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

Two consequences the implementation rests on:

- **The arity is per mate, and `map/star-umi`'s mate count is per SAMPLE** (`SAM PE` where the cell
  has a mate, `SAM SE` where it does not — one plate legally mixes both). A flag rendered once for
  the whole run is fatal on every cell of the other kind, which is why both flags are rendered from
  the single `mate_count` fact `--readFilesType` already used.
- **`CellRanger4` and a 3′ adapter cannot coexist**, which is what blocks `map/starsolo` from
  honouring a `read_through` at all until [#355](https://github.com/liuhlab/seqforge/issues/355) is
  resolved. Note STAR's own `SOLUTION:` line on that error names the wrong family ("do not use
  `--clip5pAdapter*`") — three-prime is what is forbidden.

## 3. What is NOT measured here

**That clipping recovers the reads.** The expected signature is `unmapped: too short` collapsing and
`Uniquely mapped reads %` rising by most of the difference; if uniquely-mapped does *not* rise, the
reads were not the clean-but-short population §1 assumes and the diagnosis is wrong. Re-run a handful
of samples against the baseline table above and compare `Log.final.out`.

It was attempted locally on 2026-08-07 and **cannot be done on macOS**: STAR reads **0 input reads**
from any input there — FASTQ or uBAM, 30/75/150 bp, x86-64 under Rosetta *and* the native osx-arm64
build. `Log.out` reports `end of input stream, nextChar=-1` before read #1, while malformed input
still errors correctly (`wrong read ID line format`), so the stream opens and delivers nothing. This
widens [#345](https://github.com/liuhlab/seqforge/issues/345), which recorded the defect as
osx-64-specific; `tests/conftest.py`'s skip keys on `sys.platform == "darwin"` and so is correct
either way.

One genuinely separate macOS defect was isolated on the way: `--sysShell /bin/bash` removes the
`Failed spawning readFilesCommand` error entirely (exit 102 → 0), and the 0-reads behaviour survives
it. So the two are independent, and neither is worth working around in a shipped rule —
`map/star-umi` runs on Linux.

The portable reproduction (reference, reads, both indexes, `make_data.py`, `run4.sh`) should produce
the real table unchanged on Linux;
[`smartseq3-single-end-configuration.md`](smartseq3-single-end-configuration.md) is the precedent for
where such a run belongs — inside `liulab-runtime_align-rna.sif` with `STAR-avx2`, with positive and
negative controls.
