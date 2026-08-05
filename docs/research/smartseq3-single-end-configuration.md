# Smart-seq3's single-end configuration: the read-set contest, and the aligner argument under it

Measured 2026-08-05 for [#267](https://github.com/liuhlab/seqforge/issues/267), decided in
[ADR-0035](../adr/0035-the-mate-is-an-addition-to-umi-extraction.md). Two measurements, joined by one
change rather than by a mechanism: what declaring `read_sets: {se: [R1]}` on the plate entry does to
scoring, and what STAR does when the aligner argument that follows from it is wrong.

## The one-sentence answer

**Declaring `se: [R1]` is safe in both directions, and only one of the two margins is structural.** On
a paired deposit the maximal set beats its own single-end subset by exactly the orphan penalty, at
every depth. On a single-end deposit the plate leads the generic fallback by a margin that **depends
on read depth** — `+0.000000` on the 200-read slice a fixture hands a scorer, `+0.000999` on every
read. The near-tie was written down as *structural* before the second depth existed, and it is not: it
is the point where a saturating support and a depth-sensitive one happen to cross. What survives both
depths, and the only thing a gate may rest on, is that the pair lands **inside `_THETA`** — which
routes to the `confusable_with` edge both entries already declare, a Question at exit 4.

Separately: **`--readFilesType SAM PE` over an unpaired uBAM is a crash, not a wrong number.** That is
what makes the rendered `SAM SE` / `SAM PE` load-bearing rather than a tidier spelling of a literal —
it had been asserted five times across the tree with no measurement behind it.

## Method, for both directions of §1

`kb.generate_reads(spec, n=<N>, seed=0)` → gzipped FASTQ → `probe_file` → **`read_set_evaluations`
directly**, never through `build_tech_evaluation`'s maximum: a claim about *one* read set is not
checkable through a function that returns the best one, because the answer cannot say which set it
measured. `_THETA` = 0.02. The onlist registry is **empty**, which costs nothing here — neither
`smartseq3` nor `bulk-rnaseq` declares an onlist, so there was never a whitelist term in either score.

This is `generate_reads` synthetic data throughout. See [what it could not
establish](#what-these-measurements-could-not-establish).

## 1a. The paired direction: the maximal set wins by the orphan penalty

`smartseq3` on its **own paired reads** (2 files), `full` against `se`:

| n | files | `full` | `se` | margin |
|---:|---:|---:|---:|---:|
| 400 | 2 | 1.010000 | 0.760000 | **+0.250000** |
| 1001 | 2 | 1.010000 | 0.760000 | **+0.250000** |
| 2000 | 2 | 1.010000 | 0.760000 | **+0.250000** |

Flat, because the margin is not evidence at all. `_LAMBDA` = 0.25 and `scoring.py` charges
`λ/|R|` per unassigned file; the `se` set has one role and leaves one file unassigned, so it pays
`0.25 / 1 × 1` exactly. Nothing had to be invented for the maximal set to win, and nothing about it
moves with depth.

`full` at 1.0100 is also the score this entry had *before* it declared a read set — which is the
measurement behind the version log's claim that the bump re-keys `run_id` and regenerates no stored
plate manifest.

## 1b. The single-end direction, and this is the finding

`smartseq3/se` against `bulk-rnaseq/se` on **the plate's own single-end read** (R1 only, 1 file),
n = 1001, the same probe sliced three ways:

| slice | `smartseq3/se` | `bulk-rnaseq/se` | margin | inside θ |
|---|---:|---:|---:|:--:|
| `seqs[:200]` — what the `kb_probes` fixture hands a scorer | 1.010000 | 1.010000 | **+0.000000** | yes |
| `seqs[:400]` | 1.010000 | 1.010000 | **+0.000000** | yes |
| `seqs[:1001]` — every read; what the resolver scores a deposit on | 1.010000 | 1.009001 | **+0.000999** | yes |

Through the full production path — `resolve_dataset` on a one-file deposit, n = 1001, synthetic
registry — the candidate list is

```text
[('smartseq3', 1.010, 'se'), ('bulk-rnaseq', 1.009, 'se')]
```

and it is **filename-independent**: identical for `s_R1.fastq.gz` and for
`cell_S1_L001_R1_001.fastq.gz`.

### Why the two entries are level at all

Term by term, on the single-end deposit (`_BETA` = 0.01, `_LAMBDA` = 0.25, `_GLOBAL_COEF` = 0.001):

| term | `smartseq3/se` | `bulk-rnaseq/se` |
|---|---|---|
| applicable supports on R1 | `motif_present` (w 1.0) | `distinct_ratio` bases 0–25 (w 2.0) |
| supports that do **not** apply | `distinct_ratio` is addressed to R2 | the second `distinct_ratio` is addressed to R2 |
| cell value | `acc / total_w` over one support ⇒ its score | same shape, one support ⇒ its score |
| orphan penalty | none — one file, one role | none — one file, one role |
| filename prior | 0.0100 (`_R1_` hint) | 0.0100 (**the same hint**) |
| read-less (global) support | none | `header_index`, FAIL at 0.0 ⇒ contributes 0 |

`_score_cell` normalizes **within a read** (`value = acc / total_w`), so a lone firing support puts a
cell at exactly 1.0 and the declared weight cancels. Both entries have exactly one such support on R1;
both declare the same `_R1_` hint and so take the same 0.01 prior; and with one file neither pays the
`λ/|R|` that decides §1a and every row of the bulk-vs-barcoded comparison. The plate also has no
onlist to hit — its cell barcode is the *file*. So both of the margins that protect a barcoded leaf
from the generic fallback are unavailable, and level is the expected result.

### What moves with depth is only how the two supports saturate

Evaluated directly, same probe, three depths:

| slice | `smartseq3` `motif_present` | `bulk-rnaseq` `distinct_ratio` 0–25 |
|---|---|---|
| `seqs[:200]` | PASS, score **1.0**, `motif_rate=1.00` | score **1.000000** |
| `seqs[:400]` | PASS, score **1.0**, `motif_rate=1.00` | score **1.000000** |
| `seqs[:1001]` | PASS, score **1.0**, `motif_rate=1.00` | score **0.999001** |

`motif_present` is an anchored rate against a floor: `min_rate` = 0.02, and the observed rate is
1.00 on these synthetic reads and 0.396–0.676 across ten GSE207085 cells (#234). Either way it PASSes
by two orders of magnitude and its score is **flat at 1.0 at any depth**.

`distinct_ratio` counts distinct 25-mers over bases 0–25, and the raw counts are the whole story:

```text
seqs[:200]    200/200  = 1.000000
seqs[:400]    400/400  = 1.000000
seqs[:1001]  1000/1001 = 0.999001     <- exactly one collision
```

So the entire measured margin is one repeated 25-mer in 1001 reads. A truncated slice reads as an
exact tie; a real deposit does not. **Do not quote the tie as structural**, and do not quote either
number without its depth.

### What follows for the gate

`+0.000999` is 20× under `_THETA`, and `+0.000000` is under it too — so the claim neither depth moves
is *inside the tie band*, and that is the designed outcome rather than a defect. A near-tie between
two entries that already declare a `confusable_with` edge (`processing_divergent`,
`distinguishable_by: [metadata]`) routes to that edge and becomes a **Question at exit 4**, which is
recoverable.

The gate over this contest is therefore written **one-sided — the generic entry must not WIN, not
that the plate must** — because that is the form that holds at both depths. A two-sided assertion
passes on a deposit and fails on the fixture that scores it. What is not tolerable, and is a stop
condition rather than a caveat, is `bulk-rnaseq/se` scoring *above* the plate by more than θ: that is
a bulk gene-count matrix for a plate library at exit 0, the plausible-matrix failure, strictly worse
than the refusal the read set was added to remove.

Reaching for the signature to open a margin here is the repair ADR-0035 forbids: #257 measured every
additional R1 support on this entry as a strict liability — the motif already saturates on every real
cell, the trailing-`GGG` support goes negative on 4 of 10 published cells, and dropping the draft's
two extra supports roughly doubled the thin per-cell margins. Trading measured per-cell margins for a
synthetic contest margin is the wrong currency.

## 2. `--readFilesType SAM PE` over an unpaired uBAM

Measured 2026-08-05 against STAR **2.7.11b** (compiled 2025-11-14) in
`liulab-runtime_align-rna.sif`, `/app/.pixi/envs/align-rna/bin/STAR-avx2`. Fixture: one synthetic
single-end Smart-seq3 cell, 40 reads, 26 of them tag-carrying, extracted to a uBAM of 40 records all
flagged `4` — unmapped, and **zero** with the paired bit set.

**Negative.** The same file with the argument the paired layout would have rendered:

```console
$ STAR --readFilesIn se.bam --readFilesType SAM PE --readFilesCommand samtools view \
       --readFilesSAMattrKeep All --genomeDir idx --outSAMtype BAM SortedByCoordinate ...

ReadAlignChunk_processChunks.cpp:55:processChunks EXITING because of FATAL ERROR in input BAM file:
the consecutive lines in paired-end BAM have different read IDs:
read0   vs   read1

 SOLUTION: fix BAM file formatting. Paired-end reads should be always consecutive lines, with
 exactly 2 lines per paired-end read
...... FATAL ERROR, exiting
STAR exit=104
```

**Positive control, same file, `SAM SE`:** exit 0, and

| | |
|---|---:|
| input reads | 40 |
| uniquely mapped | 40 (100.00 %) |
| aligned records carrying `UB:Z:` | 26 of the 26 tagged |
| aligned records carrying `RG:Z:` | 40 of 40 |

So the `PE`/`SE` half of `--readFilesType` is the one part of that invocation derived per dataset
rather than written as a module literal, and a stale literal is **loud** — but only after the genome
index has loaded and the plate has been queued. That is the whole argument for deriving it from
`read_files_in`, the same source the `--r2` argument is rendered from, rather than from a second
reading of the layout that could disagree with it for exactly one dataset shape.

## What these measurements could not establish

- **Absolute values on real data.** §1 is `generate_reads` synthetic data against an empty registry,
  not a deposit. It establishes the **ordering** and the **tie band** — that the maximal set wins a
  paired deposit, that the generic entry does not win a single-end one, and that both contests sit
  inside θ. It does **not** establish what a real GSE207085 cell would score. Every absolute figure
  above is a property of the generator.
- **Whether the crossing point moves on real reads.** `distinct_ratio`'s fall is a birthday-collision
  effect over the generator's own 25-mer diversity; a real library's 5′ ends are far less uniform, so
  the depth at which it drops below 1.0 is a fixture property and nothing was measured against a real
  cell. The *direction* — one support flat, one falling — is a property of the evaluators and does
  carry over. The exact depth does not.
- **Anything at n > 2000, or at other seeds.** Three depths and one seed for §1a, three slices of one
  seed for §1b. Enough to show the paired margin is depth-invariant and the single-end one is not; not
  enough to characterise the curve.
- **What the near-tie does under a *third* candidate.** Only `smartseq3` and `bulk-rnaseq` were
  scored. No other shipped entry reaches a single-file 72 bp deposit, but that was reasoned rather
  than swept.
- **Whether STAR's crash is version-stable.** §2 is one STAR build in one image. The failure is in
  BAM-record pairing rather than in anything version-specific, but 2.7.11b is the only version
  measured — and it is the version `liulab-runtime` pins for `align-rna`, which is why it is the one
  that matters.
- **The uBAM's behaviour with more than one cell's records.** The fixture is one cell. The paired
  path at greater width is covered by the agreement fixture
  ([`umite-agreement-fixture.md`](umite-agreement-fixture.md)), which is untouched by this change.

## Reproducing

§1 needs nothing but the repo: `kb.generate_reads` → `write_fastq_gz` → `probe_file` →
`read_set_evaluations`, ~30 lines, no network and no whole-FASTQ read. The three scripts used
(the two directions at three depths; a per-support evaluation at three depths; the raw 25-mer counts)
were not committed and are described precisely enough above to rewrite.

§2 needs the `align-rna` image, a ~24 kb random contig, 40 reads drawn from it at fixed offsets (26
of them prefixed with the tag, an 8 bp UMI and `GGG`), a `STAR --runMode genomeGenerate` index over
the contig, and the shipped extractor to make the uBAM — the same shape as the end-to-end plate
fixture, narrowed to R1, but a throwaway rather than that fixture itself. The two STAR runs differ in
`SAM PE` vs `SAM SE` and in nothing else, which is what makes the contrast the measurement.
