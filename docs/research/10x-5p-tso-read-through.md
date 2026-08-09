# Does a 10x 5' cDNA read run through into the reverse complement of its own TSO?

Measured 2026-08-07 for [#355](https://github.com/liuhlab/seqforge/issues/355). **Yes — the
mechanism is real, mechanically proven, and absent from 3' data. But its prevalence is a property of
the individual library's insert-size distribution, not of the chemistry: five 5' libraries span
0.094% to 10.41% of R2 reads.** Bounded head-reads only; no FASTQ was downloaded whole.

    TSO      TTTCTTATATGGG   the 13 nt 10x 5' template-switch oligo, on the gel-bead primer
    anchor   CCCATATAAGAAA   its reverse complement — what a short fragment's R2 runs into

That the 5' construct puts the TSO between the UMI and the insert, and the 3' construct puts poly-A
there instead, is the primary-source half of this and is in
[`starsolo-read-preprocessing-per-family.md`](starsolo-read-preprocessing-per-family.md). This file
is the read-level half: is it there, how often, and is it a read-through rather than biology.

## The three headline cohorts

| cohort | reads examined | anchor hits | rate |
|---|---:|---:|---:|
| 10x 5' v2 — PRJNA1415162 / GSE317744, 4 runs | 179,712 | 169 | **0.0940%** |
| 10x 5' v3 — GSE310378 / SRR36092078 | 20,000 | 2,081 | **10.4050%** |
| 10x 3' negative control — 5 libraries, 2 organisms | 500,909 | **0** | **0.0000%** |

The two 5' cohorts were pre-registered as the test and land on opposite sides of the 1% line the
question was framed around. The control does what a control should.

## 1. Method and byte budget

`seqforge io peek` reports record counts and lengths, not sequences, so the counting used the
sanctioned fallback: an HTTP `Range` read of the head of each remote `.gz`, piped straight into a
counter. **4,000,000 compressed bytes per file** — for the 5' v2 R2 files (33–42 GB each) that is
**0.010% of the file**. Nothing was written to the repo.

Runs were resolved with the repo's own verb, and the bounded verb was checked against the raw range
read on the same URL:

```bash
pixi run seqforge io resolve PRJNA1415162   # 4 runs, R1 26 / R2 90, DNBSEQ-G400
pixi run seqforge io resolve PRJNA1027859   # 6 runs, R1 28 / R2 94  (the 3' control)

curl -s -r 0-3999999 <fastq_ftp url> > raw_<tag>_<acc>.gz     # fetch.sh
gzip -dc raw_5pv2_SRR37001528.gz | python3 count_anchor.py     # 4-line record parser

pixi run seqforge io peek https://ftp.sra.ebi.ac.uk/vol1/fastq/SRR370/028/SRR37001528/SRR37001528_2.fastq.gz \
  --max-reads 3 --max-bytes 65536
# -> compressed_bytes_read 65536, decompressed_bytes 164266, read_lengths [90,90,90]
```

R2 — the cDNA read — only. A hit is an exact occurrence of the 13-mer anywhere in the read.

**Caveat on the v3 cohort: it came from our own published fingerprint package, not from ENA.**
`io resolve PRJNA1365850` returns `fastq_ftp: ""` for all 29 runs — that study was never mirrored, so
there is no URL to range-read, and `io probe-sra` needs a `fastq-dump` that no local environment has.
The v3 numbers are therefore counted from `packages/GSE310378-provsv-gfp-til.fingerprint.tar.gz`,
pulled anonymously from the public benchmark, which is by construction the first 20,000 **complete
real records** of `SRR36092078` (`fingerprint/subsample.py`). That is a different transport, so it
was cross-checked on the cohort where both are available: the `GSE317744-ccr9ko-thymic-dc` package
(20,000 records of `SRR37001527`) gives **0.0850%** against **0.0993%** from the 4 MB range read of
the same run. Two transports, same answer.

## 2. Prevalence — every library measured

| library | chemistry | R2 | reads | anchor hits | **anchor rate** | forward TSO |
|---|---|---:|---:|---:|---:|---:|
| PRJNA1415162 / GSE317744, 4 runs (DNBSEQ-G400) | 10x 5' v2 | 90 | 179,712 | 169 | **0.0940%** | 0.0590% |
| SRR36092078 (GSE310378, NovaSeq X Plus) | 10x GEM-X 5' v3 | 90 | 20,000 | 2,081 | **10.4050%** | 0.0800% |
| SRR27150982 (PRJNA1050242, human BMMC) | 10x 5' v2 dual-index | 91 | 93,550 | 284 | **0.3036%** | 0.0620% |
| SRR26193391 (PRJNA1021559) | 10x 5' HT v2 | 90 | 103,850 | 927 | **0.8926%** | 0.0058% |
| SRR26522722 (PRJNA1017479) | "10X_Chromium_5'" | 151 | 73,578 | 1,367 | **1.8579%** | 0.0449% |
| PRJNA1027859, 4 runs (*C. elegans*) | 10x 3' v3 | 94 | 344,886 | **0** | **0.0000%** | 0.0000% |
| SRR34941573 (GSE305031, *C. elegans*) | 10x GEM-X 3' v4 | 90 | 20,000 | **0** | **0.0000%** | 0.0000% |
| SRR29997563 (PRJNA1140231, mouse) | 10x 3' | 101 | 97,956 | **0** | **0.0000%** | 0.0000% |
| SRR33765276 (PRJNA1270000, mouse) | 10x 3' | 150 | 36,067 | **0** | **0.0000%** | 0.0000% |
| SRR34104052 (GSE283483, mouse) | 10x multiome GEX | 151 | 2,000 | **0** | **0.0000%** | 0.0000% |

**Zero hits in 500,909 3' reads across five libraries and two organisms.** Three controls are mouse,
so the zero is not an organism artefact — the mouse 5' and mouse 3' libraries differ by 100–10,000×.
The 95% upper bound on the control rate is 3/500,909 = **0.0006%**, so even the weakest 5' library
sits 150× above the control ceiling. The anchor is diagnostic of 5'.

The controls are not blind, either: the same scan on the 3' control `SRR28716553` finds a 20-base
poly-A run in **7.47%** of its reads and TruSeq-R1-revcomp in 0.53% — the 3' library's own
read-through, exactly where the construct says it should be. A 3' read runs into poly-A past its
insert; a 5' read runs into `CCCATATAAGAAA`. Both are visible, and each only in its own family.

> An earlier summary of this measurement — and the entry comment drawn from it — pooled the five
> controls as 496,909. That is an addition slip in the total alone; every per-library count above,
> and every zero, is as tabulated, and the derived bound is unchanged to the digit quoted.

## 3. It is a read-through, not biology

**The offsets are not pinned.** If the anchor were a fixed feature of the construct it would sit at
one position. It does not:

| library | distinct offsets | min | max | median |
|---|---:|---:|---:|---:|
| 5' v2 GSE317744 (90 bp) | 42 | 7 | 77 | 64 |
| 5' v3 GSE310378 (90 bp) | 78 | 0 | 77 | 54 |
| 5' v2 dual-index SRR27150982 (91 bp) | 66 | 2 | 78 | 61 |
| 5' HT v2 SRR26193391 (90 bp) | 56 | 2 | 77 | 59 |
| 5' 151 bp SRR26522722 | 114 | 6 | 138 | 119 |

Every library shows the same shape: a broad, continuous spread over essentially every offset the read
can hold, rising monotonically toward the 3' end and stopping exactly at `read_length − 13`. That is
the signature of a read-through — the anchor sits wherever the insert happened to end, and short
inserts are rarer than long ones. The 151 bp library pushes its mode to ~130, i.e. **the offset
tracks read length**, which is what an insert-length readout must do and what a fixed feature could
not.

**What follows the match is not biological.** In order: `revcomp(UMI)`, `revcomp(cell barcode)`, then
`AGATCGGAAGAGC…` — the reverse complement of the TruSeq Read 1 primer. Verbatim, with the boundaries
marked:

```text
5' v3, SRR36092078 (R1 = 28 bp: 16 CB + 12 UMI)
off=29 | CCCATATAAGAAA | CTTTTACTGCGC CTAAGGGGAAGCGGAC AGATCGGAAGAG
                         ^--UMI 12--^ ^----CB 16-----^ ^-adapter-^

5' v2, SRR37001527 (R1 = 26 bp: 16 CB + 10 UMI)
off=37 | CCCATATAAGAAA | GTGACGTCTG AACCGATTGCTAACAC AGATCGGAAGAGCG
                         ^-UMI 10-^ ^----CB 16-----^ ^--adapter---^
```

**The nail: the barcode in the tail is this read's own barcode.** `crosscheck.py` takes every anchored
R2 whose tail is long enough to hold UMI + CB, reverse-complements the 16 bases at
`anchor_end + umi_len`, and compares them with the first 16 bases of the **paired R1**:

| library | anchored reads with a full tail | tail reproduces the mate's CB | rate |
|---|---:|---:|---:|
| 5' v3 SRR36092078 | 899 | 807 | **89.8%** |
| 5' v2 SRR37001527 | 5 | 4 | 80% (n=5) |

The comparison is N-tolerant, because that v3 run's R1 cycle 2 is dark and a raw exact match would
read 81% for that reason alone. The residual ~10% is sequencing error in R2's low-quality 3' tail. **A
read whose 3' tail spells out its own cell barcode is a read-through, not a coincidence and not
biology** — and this is proven per read, independent of how often it happens.

**The exact 13-mer undercounts.** A read-through beginning in the last 12 bases leaves only a prefix.
Counting the longest anchor prefix that terminates the read:

| library | full 13-mer | terminal prefix ≥8 | terminal prefix ≥6 |
|---|---:|---:|---:|
| 5' v3 SRR36092078 | 10.4050% | 1.1650% | 1.7350% |
| 5' v2 SRR37001528 | 0.0903% | 0.0506% | 0.0727% |
| 5' HT v2 SRR26193391 | 0.8926% | 0.0404% | 0.0770% |
| 5' 151 bp SRR26522722 | 1.8579% | 0.3805% | 0.5409% |
| 3' control SRR28716553 | 0.0000% | 0.0000% | 0.0213% |
| 3' control SRR29997563 (mouse) | 0.0000% | 0.0000% | 0.0163% |

The controls' ~0.02% at ≥6 is the arithmetic random floor (4⁻⁶ = 0.024%) and their 0.0000% at ≥8
confirms it is noise. So the true burden is roughly full + terminal-partial: **~12.1% for 5' v3,
~0.17% for 5' v2**.

**The forward TSO is a different phenomenon and is not an argument for anything.** `TTTCTTATATGGG`
appears at 0.006%–0.080% in every 5' library and 0.0000% in every 3' control, but its offsets cluster
in the read's first half and it is followed by real genomic or rRNA sequence — a leftover switch
oligo at the *start* of the insert, which is what Cell Ranger's hard-disabled 5' trimming would have
targeted and which STAR's local alignment soft-clips for free. At ≤0.08% it is a rounding error.

## 4. What it decided, and the one thing it did not

**Prevalence is a library property, not a chemistry one.** The five 5' libraries span two orders of
magnitude — 0.094%, 0.30%, 0.89%, 1.86%, 10.41% — and the spread tracks insert size and read length,
not chemistry version: the read-through fraction *is* the fraction of fragments shorter than the read,
set by the submitter's size selection. 5' v2 and 5' v3 share the same 13 nt TSO and byte-identical
cDNA handling, so the hundred-fold gap between the two named cohorts sits between those two
libraries, not between those two chemistries.

That is why **both** 5' entries declare the read-through, and not only the one that measured 10%.
Declaring it only where it happened to be common would file a library property as a chemistry one —
the exact category error a per-chemistry key exists to avoid. Where the anchor is rare the clip is a
no-op, and a no-op costs nothing.

This is a departure from the question's own pre-registration, which said a rare anchor meant no. The
pre-registration assumed one answer per chemistry; the data says the axis was wrong. It is worth
recording that the falsifiable form still did its job — had the anchor been *pinned* to one offset,
or *present in 3' data*, the answer would have been no on every library, and neither happened.

**Not established: the size of the alignment gain, even at 12%.** STAR aligns locally by default and
already soft-clips a non-genomic 3' tail, so the recoverable reads are only those where the tail is
long enough to defeat seed-and-extend. That number is not readable from bytes. It is a
before/after alignment run of the kind [`smartseq3-tn5-read-through.md`](smartseq3-tn5-read-through.md)
reports for the Tn5 case, and **GSE310378, not GSE317744, is the library to run it on**.

## Appendix — reproduction

Scripts and per-run JSON results are in the session scratchpad, not the repo: `fetch.sh`,
`count_anchor.py` (anchor rate + offset histogram), `motifs.py` (terminal partials and the poly-A /
TruSeq motifs), `crosscheck.py` (the own-barcode test), the `urls_*.txt` lists, the 4 MB range-read
slices `raw_*.gz`, the fingerprint packages, and `cnt*_*.json` / `mot*_*.json` / `xc_*.json`. Every
number above is in one of those JSON files; the tables restate them and add nothing.
