# The three SMART-seq3 counting engines: capability, packaging, outputs

Research for [#229](https://github.com/liuhlab/seqforge/issues/229), under the map
[#225](https://github.com/liuhlab/seqforge/issues/225). Investigated 2026-08-04.

**No code is written or changed under this map** — this is a paper spec, per #225's destination.

## How claims are marked

| mark | means |
| ------ | ------- |
| **[SRC]** | read in the tool's own source code, file and line cited |
| **[DOC]** | stated in the tool's official manual or README |
| **[API]** | GitHub / PyPI / anaconda.org API, queried 2026-08-04 |
| **[PAPER]** | peer-reviewed publication |
| **[INF]** | my inference from the above — not directly attested anywhere |

Source was read from a fresh clone of each upstream repo. STAR was read at
`b1edc1208d91a53bf40ebae8669f71d50b994851` = **v2.7.11b**, which is the exact version
`liulab-runtime`'s `pixi.lock` pins for `align-rna` (`star-2.7.11b`) — so the source below is the
source of the binary the lab actually ships.

---

## The verdict, first

**STARsolo `--soloType SmartSeq` cannot use a SMART-seq3 UMI. It never looks at the read sequence for
one.** This is not a documentation gap or a missing flag; it is the shape of the code. Option 1 is
eliminated as a SMART-seq3 *UMI* counter, and survives only as a read counter.

**Recommendation: adopt `umite`,** with the reservation that it is a one-maintainer, one-year-old
package and the lab should vendor a pinned version rather than track its `main`. The deciding facts
are that umite is the only candidate whose published benchmark ran on **GSE207085 itself, all 1440
cells** [PAPER], that it installs as `pip install umite` with three pure-Python dependencies [SRC,
API], and that its per-cell-BAM-in, layered-matrix-out shape is the shape #225 constraint 5 already
asked for.

---

## 1. STARsolo `--soloType SmartSeq` — where the UMI question is settled

Three places in STAR 2.7.11b, together, close the question.

**(a) The barcode/UMI extractor returns before it reads any sequence.**
`source/SoloReadBarcode_getCBandUMI.cpp:152-160` [SRC]:

```cpp
///////////////////////////SmartSeq
    if (pSolo.type==pSolo.SoloTypes::SmartSeq) {
        cbSeq=cbQual=cbSeqCorrected=""; //TODO make cbSeq=file label
        cbMatch=0;
        cbMatchInd={readFilesIndex};
        cbMatchString=to_string(cbMatchInd[0]);
        addStats(cbMatch);
        return;
    };
```

This is the *only* function in STAR that pulls a cell barcode or a UMI out of read bases. For
`SmartSeq` it sets the cell to the **index of the FASTQ file** and returns. Everything downstream of
that early return — the `bSeq`/`bQual` slicing, `--soloUMIstart` / `--soloUMIlen`, `convertCheckUMI()`
— is never reached. `--soloUMIstart` and `--soloUMIlen` are not rejected for `SmartSeq`; they are
silently ignored, and the source says so in `ParametersSolo.cpp:122`: `//TODO: a lot of parameters
should not be defined for SmartSeq option - check it here` [SRC].

**(b) What STAR calls the "UMI" for SmartSeq is a genomic coordinate.**
`source/SoloReadFeature_record.cpp:216-218` [SRC]:

```cpp
    if (soloBar.pSolo.type==soloBar.pSolo.SoloTypes::SmartSeq && featureType!=-1) {//need to calculate "UMI" from align start/end
        soloBar.umiB=reFe.alignOut[reFe.indAnnotTr]->chrStartLengthExtended();
    };
```

and `source/Transcript.cpp:53-59` [SRC]:

```cpp
uint64 Transcript::chrStartLengthExtended()
{
    uint64 start1  = cStart - exons[0][EX_R];
    uint64 length1 = exons[nExons-1][EX_G] + Lread - exons[nExons-1][EX_R] - exons[0][EX_G] + exons[0][EX_R];
    return (start1 << 32) | length1;
};
```

The 64-bit value the counter treats as a UMI is `(alignment start << 32) | template length`, with
soft-clips extended. It is a positional duplicate key. It carries no information from the molecule's
barcode.

**(c) The dedup methods that need a real UMI are rejected at parse time.**
`source/ParametersSolo.cpp:613-616` [SRC] refuses `--soloUMIdedup All`, `Directional` and `CR` for
`SmartSeq`, leaving only `Exact` and `NoDedup`. And in `SoloFeature_countSmartSeq.cpp:95` [SRC],
"Exact" is literally `if ( fu->umi != (fu-1)->umi )` after a sort — exact equality of that coordinate
key, with no mismatch tolerance, because there is no sequence to tolerate mismatches in.

STAR's own manual agrees, in `docs/STARsolo.md:312` [DOC]: "Cell barcodes are not incorporated in the
read sequences, and **there are no UMIs**", and `source/parametersDefault:776` [DOC]: "no UMI
sequences, alignments deduplicated according to alignment start and end". The manual is not silent —
it is explicit — and the source confirms the manual is describing the implementation and not an
oversight. Corroborated independently by
[nf-core/scrnaseq#497](https://github.com/nf-core/scrnaseq/issues/497), where the same limitation is
reported by users.

### What this costs, concretely

SMART-seq3 libraries are a **mixture**: reads that carry the TSO (`ATTGCGCAATG`) and an 8 bp UMI, and
"internal" reads that carry neither and behave like SMART-seq2. The umite paper measured 6%–78%
UMI-reads per cell on GSE207085 [PAPER]. STARsolo `SmartSeq` would collapse the whole mixture into one
coordinate-deduplicated read count, discarding the molecule counting that is the entire point of
SMART-seq3 over SMART-seq2, and could not produce the separate UMI / internal layers.

### The workaround, and why it is not free

STAR *can* count real UMIs against arbitrary string cell IDs — but not from FASTQ. `--soloCBtype
String` (added in 2.7.11a) reads the cell ID and UMI from `bStrings[0]` and `bStrings[1]`
(`SoloReadBarcode_getCBandUMI.cpp:279-280`) [SRC], and `bStrings` is populated **only** in the
SAM-input branch, from the tags named by `--soloInputSAMattrBarcodeSeq`
(`SoloReadBarcode_getCBandUMI.cpp:184-196`) [SRC]. `ParametersSolo.cpp:153-156` [SRC] spells out the
intended recipe in its own error message: for Smart-seq from BAM, use `--soloType CB_UMI_Simple` with
a whitelist of file names and `--soloInputSAMattrBarcodeSeq`.

So a STARsolo route to real UMI counts exists, but it requires seqforge to first write the
SMART-seq3-specific part itself — find the TSO, extract the UMI, split UMI-reads from internal reads,
emit a tagged BAM — and then run STAR twice (once per read class) to get two layers. That
preprocessing step *is* `umiextract`. Choosing it means writing and maintaining a competitor to a
published tool inside seqforge, which R10 and #225 constraint 4 both point away from. [INF]

### The rest of the STARsolo contract

- **Input** [SRC, `Parameters_readFilesInit.cpp:95-138`]: `--readFilesManifest` is a TSV of exactly
  three tab-separated columns, `R1 <tab> R2 <tab> CellID`, with `-` in column 2 for single-end. The
  cell ID is forced to start with `ID:` and becomes a `@RG` line. 1440 cells = a 1440-line TSV. This
  is a clean, deterministic, machine-independent expression of the dataset and it is by far the
  nicest input contract of the three.
- **Cell whitelist** [SRC, `ParametersSolo.cpp:344`]: `cbWLstr = pP->outSAMattrRG` — the cell list
  *is* the read-group list from the manifest. No whitelist file.
- **Output**: standard `Solo.out/<Feature>/raw/{matrix.mtx,barcodes.tsv,features.tsv}`, i.e. exactly
  what seqforge's `workflows/h5ad.py` already reads — **but only with one dedup type**.
  `--soloUMIdedup Exact NoDedup` writes two files named `umiDedup-Exact.mtx` and
  `umiDedup-NoDedup.mtx` and **no `matrix.mtx` at all** (`SoloFeature_outputResults.cpp:88-107`) [SRC],
  which `h5ad.py`'s `SoloFeatureOutput(matrices=("matrix.mtx",))` would not find. So the "the shipped
  finalize chain is reusable as-is" premise in #229 holds for a single-dedup run and breaks the moment
  a second count layer is asked for. `--soloFeatures Velocyto` is **rejected** for SmartSeq
  (`ParametersSolo.cpp:238-240`) [SRC], and `--soloMultiMappers` is rejected too
  (`ParametersSolo.cpp:483-486`) [SRC].
- **Per-cell BAM**: no. One BAM for all cells, each read carrying `RG:Z:<CellID>` [DOC,
  `docs/STARsolo.md:325`]. Per-cell CRAM would need a `samtools split` by RG. `samtools` is already in
  every `liulab-runtime` environment's base layer, so this is cheap but is a new rule.
- **Resource profile for 1440 cells**: **not documented.** One STAR process, one genome load, 1440
  files streamed sequentially — structurally the cheapest of the three on genome-loading — but no
  published number exists. [INF]

---

## 2. zUMIs

The reference implementation: it is what the Smart-seq3 authors used, and SMART-seq3 support is
first-class and explicit in the source.

- **UMIs**: **yes**, and SMART-seq3-aware. `zUMIs-dge2.R:36` [SRC] sets
  `smart3_flag <- ifelse(any(grepl(pattern = "ATTGCGCAATG", x = unlist(opt$sequence_files))), TRUE, FALSE)`
  — the SMART-seq3 code path is switched on by finding the TSO literal in the YAML. When set, the BAM
  is split into UMI-containing and internal reads (`UMIstuffFUN.R:501-510`, by `grep -v 'UB:Z:[A-Z]'`)
  [SRC], and the output carries three matrices: `umicount`, `readcount`, `readcount_internal`
  (`zUMIs-dge2.R:234-237`) [SRC], plus RPKM computed on internal reads (`zUMIs-dge2.R:247-256`) [SRC].
  UMI error correction is Hamming-distance collapse, `Ham_Dist` in the YAML [DOC].
- **Input contract**: one YAML, with reads described by a `base_definition` vocabulary —
  `BC(n) UMI(n) cDNA(n)` — that assumes **the cell barcode lives in the read**. This is precisely the
  assumption #225 constraint 1 says SMART-seq3 breaks.
- **How 1440 already-demultiplexed cells are expressed — and the problem.** zUMIs' answer is
  `misc/merge_demultiplexed_fastq.R`, which concatenates every per-cell FASTQ into one file and
  synthesises an index read. Line 56 [SRC]:

  ```r
  samples[, sample := tstrsplit(r1, file_delim_r1, keep = 1)][
          , BC := stringi::stri_rand_strings(.N, 8, pattern = "[A-Z]")]
  ```

  Two consequences seqforge cannot ignore. First, the per-cell barcode is a **random string with no
  seed** — rerun it and every cell gets a different barcode, so the step is not reproducible and
  cannot sit inside a content-addressed pipeline unless seqforge generates the index FASTQ itself.
  Second, `pattern = "[A-Z]"` draws from the whole alphabet, not `ACGT`, so the synthetic "barcode" is
  not a nucleotide sequence at all. It also means all 1440 cells are physically concatenated into one
  FASTQ before anything runs.
- **Per-cell BAM**: **yes**, `demultiplex: yes` under `barcodes:` in the YAML — "produce per-cell
  demultiplexed bam files" (`zUMIs.yaml:69`) [SRC/DOC].
- **Output artifacts**: `zUMIs_output/expression/<project>.dgecounts.rds` — a **nested R list of
  sparse matrices** (`saveRDS`, `zUMIs-dge2.R:258`) [SRC], keyed
  `umicount|readcount|readcount_internal` → `exon|intron|inex` → `all|downsampling`. Turning that into
  an h5ad requires R in the loop: either `misc/rds2loom.R` (which needs `loomR`, an
  effectively-unmaintained package) or a new R→h5ad exporter. There is no mtx or TSV.
- **Packaging**: **not on bioconda.** No `zumis` package on anaconda.org and no
  `recipes/zumis/meta.yaml` in bioconda-recipes (both 404, checked 2026-08-04) [API]. *(#229's premise
  "on bioconda" does not hold.)* Install is `git clone` only [DOC]. The repo ships its own conda-pack
  environment as eight split binaries, `zUMIs-miniconda.parta{a..h}`, **734 MB** (700 MiB); the clone is 1.9 GB
  [API/local]. Without that bundle you supply the dependency chain yourself: R ≥ 4.0 with
  data.table, Rsamtools, stringi, optparse, inflection, ggplot2, plus Perl, Python/pysam, samtools,
  pigz and STAR. This is a whole second language runtime landing in `align-rna`.
- **Maintenance** [API, 2026-08-04]:
  - last release **2.9.7, 2022-03-27** (4 years, 4 months ago)
  - last commit on `main` **2023-03-11** ("2.9.7e") — 3 years, 5 months ago
  - the `zUMIs-dev` branch's last commit is **2019-10-01**
  - **44 open issues** (48 by the repo counter, which includes 4 open PRs); 352 closed
  - 297 stars, 71 forks, GPL-3.0, not archived
  - `pushed_at` is 2024-07-12, but no branch carries a 2024 commit — the push did not add code.
- **Resource profile for 1440 cells**: no zUMIs-published figure. The umite paper measures it: on an
  800-cell / ~150 M read-pair sample, zUMIs used **5.7× the peak memory, 1.6× the wall time and 1.8×
  the disk** of umite on 8 cores [PAPER]. Absolute values are in Figure 1F and are not given in the
  text.

---

## 3. umite

- **UMIs**: **yes** — it exists for nothing else. `umiextract` finds the TSO anchor `ATTGCGCAATG`, an
  8 bp UMI and the `GGG` trailer, trims the UMI out of the sequence and appends it to the read name;
  `umicount` then counts per gene and deduplicates, with optional directional-Hamming UMI correction
  (`--UMI_correct`, `--hamming_threshold 1`, `--count_ratio_threshold 2`) [DOC/SRC]. Its distinguishing
  feature is `--fuzzy_umi`: mismatch- and indel-tolerant anchor matching (`--anchor_mismatches 2`,
  `--anchor_indels 1`), which recovered **an additional 5%–15% of UMIs** over exact matching on
  GSE207085 [PAPER].
- **Input contract**: the shipped Snakemake workflow takes a `samples_file` — **one sample name per
  line** — plus `fastq_dir`, `R1_suffix`, `R2_suffix` (`workflow/snakeconfig.yaml`) [SRC]. 1440 cells =
  a 1440-line text file. The CLI form is `umiextract -1 <R1...> -2 <R2...>` and `umicount --bams
  <bam...>`, i.e. plain argv lists, which for 1440 cells means a very long command line — seqforge
  would drive `umicount` per batch or via its own rule rather than reuse their Snakefile. [INF]
- **Per-cell BAM**: **yes, natively.** The model is one STAR invocation per cell producing
  `{sample}_Aligned.out.bam`, name-sorted to `{sample}.namesort.bam`, which `umicount` then consumes
  (`workflow/snakefile_umite_star.smk:119-170`) [SRC]. Their workflow marks these `temp()`; seqforge
  writing its own rules would simply not. This maps one-to-one onto #225's per-cell CRAM deliverable
  with no splitting step.
- **Output artifacts**: five TSVs by default — `umite.UE.tsv`, `umite.UI.tsv`, `umite.RE.tsv`,
  `umite.RI.tsv`, `umite.D.tsv` (UMI/read × exonic/intronic, plus duplicates), collapsing to `U`/`R`/`D`
  under `--combine_unspliced` (`snakefile_umite_star.smk:28-36`) [SRC]. Each is **dense**, cells in
  rows, genes in columns, with leading `_unmapped` / `_multimapping` / `_ambiguous` read-fate columns
  (`umicount.py:476-495`, `ReadCategory`) [SRC]. Five dense TSVs is a straight
  `pandas.read_csv` → `AnnData(layers=...)`, so the h5ad with several count layers is nearly free —
  but 1440 × ~55 k genes × 5 layers of *text* is the one place umite is structurally wasteful versus a
  sparse mtx. [INF] Per-cell intermediates are pickles in `--tmp_dir`.
- **Packaging**: **PyPI, `pip install umite`** — versions 0.1.0 (2025-11-14) and 0.1.1 (2026-01-06)
  [API]. Dependencies are exactly three, all pure-Python and all on conda-forge already: **HTSeq,
  regex, RapidFuzz** (`pyproject.toml:20-24`) [SRC]. Optional extras `tests` (pytest) and `workflow`
  (snakemake ≥8). **Not on bioconda or conda-forge** (both 404) [API] — so it enters `liulab-runtime`
  as a `pypi-dependencies` entry, the same mechanism already used for `seqforge`, `liulab-genome` and
  `liulab-data`. No R, no Perl, no compiler, no bundled interpreter. This is the cheapest adoption of
  the three by a wide margin. One snag to expect: `workflow/umite_conda.yaml` pins `python=3.7` [SRC]
  while `liulab-runtime`'s base layer is `python = "3.13.*"`. `pyproject.toml` only declares
  `requires-python = ">=3.7"`, so the pin looks like a stale env file rather than a real ceiling — but
  it is untested at 3.13 and the bake-off should confirm it before the PR. [INF]
- **Maintenance** [API, 2026-08-04]:
  - created **2025-07-16**; last commit **2026-03-20**
  - **0 releases, 0 tags** on GitHub (PyPI is the only versioned artifact)
  - **0 open issues, 0 issues ever**; 1 star, 0 forks; GPL-3.0
  - one maintainer (Leo Förster), five authors, DKFZ / Martin-Villalba lab
  - 1050 lines of Python across `cli.py`, `umicount.py`, `umiextract.py`, against **1301 lines of
    pytest** in `tests/` — a better test-to-code ratio than either alternative [SRC]
  - "0 open issues" here means *nobody has filed one*, not *everything is fixed*. Read it as no user
    base yet.
- **Published**: **yes, peer-reviewed.** Förster, Frigoli, Sun, Hooli, Goncalves & Martin-Villalba,
  "umite: fast quantification of Smart-seq3 libraries with improved UMI retrieval", *Bioinformatics*
  42(3), 2026-02-15, [doi:10.1093/bioinformatics/btag075](https://doi.org/10.1093/bioinformatics/btag075)
  (open access via [PMC12989134](https://pmc.ncbi.nlm.nih.gov/articles/PMC12989134/)).
- **Resource profile for 1440 cells**: the paper's benchmark **is** GSE207085 — "we analyzed 1440
  Smart-seq3 libraries from the murine nasal vasculature study by Hong et al." [PAPER], and its Data
  Availability names `GSE207085` explicitly. Timings are reported for an 800-cell / ~150 M read-pair
  subset on 8 cores (2 GHz Intel Broadwell): vs zUMIs, **−31% wall time (1.6×), −82% peak memory
  (5.7×), −43% disk (1.8×)**; the counting step alone is 1.7× faster and 99.2× lighter on memory, while
  FASTQ processing is 1.7× *slower* because of fuzzy matching. Absolute seconds and GB are in Figure 1F
  only.
- **Two defects noticed while reading the shipped workflow** [SRC], neither fatal, both worth knowing
  before anyone runs it as-is:
  1. `snakefile_umite_star.smk` imports `from os import rename` but the `rename_umite_output` rule's
     `run:` block calls `os.rename(...)`; bare `os` is never imported (and the `import pandas as pd` on
     line 3 is unused). Whether this raises `NameError` depends on what Snakemake's generated
     preamble happens to put in scope — **I did not execute it, so this is unconfirmed**, but it is a
     latent break in the last rule of the workflow.
  2. `rule prepare_star_indices` builds the STAR index *into the reference genome's own directory*.
     seqforge must never do this — index paths belong to `liulab-genome` (R10) — so their Snakefile
     cannot be reused wholesale regardless.

---

## Comparison table

| | **STARsolo `SmartSeq`** | **zUMIs** | **umite** |
| --- | --- | --- | --- |
| **Uses SMART-seq3 UMIs?** | **No** [SRC] — early-returns before reading sequence | **Yes** [SRC], `smart3_flag` | **Yes** [SRC/PAPER], its whole purpose |
| What it deduplicates by | alignment start+length, `(start<<32)\|len` [SRC] | UMI sequence, Hamming collapse [SRC] | UMI sequence, directional Hamming [SRC] |
| UMI error tolerance | none — `All`/`Directional`/`CR` refused [SRC] | `Ham_Dist` [DOC] | `--hamming_threshold`, `--count_ratio_threshold` [SRC] |
| Fuzzy TSO/anchor matching | n/a | 1–2 mismatches, no indels [DOC] | mismatches **and** indels; +5–15% UMIs [PAPER] |
| Separates UMI vs internal reads | no [INF from SRC] | yes — 3 matrices [SRC] | yes — 4–5 matrices [SRC] |
| **Input for 1440 cells** | 1440-line manifest TSV, `R1\tR2\tCellID` [SRC] | concat all FASTQs + **random unseeded barcodes** [SRC] | 1440-line sample-name file [SRC] |
| Input assumes barcode-in-read | no | **yes** — the assumption #225 says is broken | no |
| **Per-cell BAM** | no; one BAM, `RG:Z:` — needs `samtools split` [DOC] | yes, `demultiplex: yes` [SRC] | **yes, natively** — 1 BAM per cell [SRC] |
| **Output format** | `Solo.out` mtx — seqforge already reads it | `.dgecounts.rds`, **R only** [SRC] | 5 dense TSVs [SRC] |
| h5ad with several layers | layers are read counts only, and 2 dedup types drop `matrix.mtx` [SRC] | needs R in the loop | trivial pandas, but dense text at scale |
| **Packaging** | already in `align-rna` (`star-2.7.11b`) | **git clone only — not on bioconda** [API] | **PyPI `umite`**, not on conda [API] |
| New dependencies | **none** | R ≥4.0 stack + Perl + Python/pysam, or a 734 MB bundled conda-pack | HTSeq, regex, RapidFuzz (3, pure Python) |
| **Last release** | 2.7.11b, 2024-01-26 | **2.9.7, 2022-03-27** | none tagged; PyPI 0.1.1, 2026-01-06 |
| **Last commit** | 2025-03-18 (repo push) | **2023-03-11** | 2026-03-20 |
| **Open issues** | 1004 | **44** (+4 PRs) | 0 (and 0 ever filed) |
| Stars / forks | 2229 / — | 297 / 71 | 1 / 0 |
| Published | Dobin 2013 (STAR); STARsolo 2021 | GigaScience 2018 | ***Bioinformatics* 2026** |
| Benchmarked on GSE207085 | no | as the comparator [PAPER] | **yes, all 1440 cells** [PAPER] |
| Velocyto / multimappers | both **refused** for SmartSeq [SRC] | velocyto yes [DOC] | intron/exon split, no velocyto |

### Published comparisons between them

Only one exists, and it is umite's own paper: **umite vs zUMIs**, on GSE207085 (1440 cells) and
GSE270928 (500 cells) [PAPER]. Concordance is reported as "consistently high" Spearman correlation per
cell and a mean kNN-graph Jaccard index of **0.8** across cells — high agreement, but note that the
only published concordance check on the winning tool was run by its own authors.

**No published comparison involves STARsolo's `SmartSeq` mode at all.** The umite paper does not
mention STARsolo; it names STAR only as "any standard RNA-seq aligner" [PAPER]. That absence is
consistent with the source finding: nobody benchmarks a UMI counter against a tool that does not count
UMIs.

---

## Recommendation

**Adopt umite.** Rank order: umite > zUMIs > STARsolo `SmartSeq`.

1. **STARsolo `SmartSeq` is out on capability.** It is not a close call and no flag fixes it. Its one
   surviving role is as a cheap, zero-dependency *read-count* baseline in the #225 constraint-4
   bake-off — worth running precisely because it costs nothing and gives a floor to measure the other
   two against, but it cannot be the shipped engine for a UMI protocol. Keeping it would mean shipping
   SMART-seq3 as SMART-seq2 with extra steps.

2. **umite over zUMIs on four independent axes**, none of which is "it is newer":
   - *Packaging.* Three pure-Python dependencies through the `pypi-dependencies` mechanism
     `liulab-runtime` already uses, versus dragging an R 4.x + Rsamtools + Perl + pysam chain (or a
     734 MB bundled interpreter) into `align-rna`. This is the single largest cost difference and it
     recurs at every environment rebuild and every SIF rebuild.
   - *Input shape.* umite takes already-demultiplexed per-cell FASTQs as its native input. zUMIs
     requires concatenating 1440 cells into one FASTQ and inventing barcodes with an **unseeded RNG**
     — which is flatly incompatible with a content-addressed, reproducible pipeline unless seqforge
     replaces that script.
   - *Output shape.* umite's per-cell BAM and layered matrices are already the shape #225 constraint 5
     specifies. zUMIs' `.rds` puts R on the critical path to the h5ad.
   - *Evidence.* The only quantitative evidence anyone has published about running a counting engine
     on GSE207085 is umite's, and it favours umite on time, memory and disk while agreeing with zUMIs
     on the counts.

3. **Adopt it with a pin, not a track.** umite is v0.1.1, one maintainer, one star, zero filed issues,
   zero git tags. That is not a reason to reject it — the code is small (850 lines) and better tested
   than either alternative (1298 lines of pytest), and the paper is peer-reviewed — but it is a reason
   to pin an exact version in `liulab-runtime` and treat an upstream disappearance as a live risk. The
   mitigation is cheap and should be stated in the PR: at 850 lines of dependency-light Python under
   GPL-3.0, umite is forkable in an afternoon if upstream goes quiet. zUMIs, at 3 years since its last
   commit, has *already* gone quiet, and is not forkable in an afternoon.

4. **What the bake-off should therefore measure** (#235's input): run all three on a subset of
   GSE207085. STARsolo `SmartSeq` for the read-count floor and to confirm the missing UMI layer
   empirically; zUMIs and umite for UMI-count concordance and resources. The interesting number is not
   which is faster — the paper answers that — but whether umite's counts reproduce zUMIs' on *our*
   subset with *our* genome and GTF, since the only published concordance was self-reported.

---

## What I could not establish from a primary source

- **Absolute runtime and memory for any engine on 1440 cells.** The umite paper's numbers are
  fold-changes on an 800-cell subset; absolute values live in Figure 1F and are not in the text. STAR
  and zUMIs publish no SMART-seq3 figure at all. Anyone sizing a Slurm request must measure it.
- **Whether umite's `rename_umite_output` rule actually breaks.** The `os.rename`-without-`import os`
  is real in the file, but I did not execute Snakemake to see whether its generated preamble supplies
  `os`. Verify before relying on their Snakefile — though seqforge will write its own rules regardless.
- **Peak memory of STARsolo `SmartSeq` at 1440 files.** `countSmartSeq()` holds a
  `{feature, umi}` pair per counted read in memory per redistribution file
  (`SoloFeature_countSmartSeq.cpp:36-53`) [SRC], so it is bounded by total counted reads, not by cell
  count — but STAR documents no figure and I did not run it.
- **Whether zUMIs is unmaintained or merely finished.** The evidence is silence: no commit since
  2023-03, no release since 2022-03, 44 open issues. I found no maintainer statement either way.
- **umite's behaviour on SMART-seq3xpress.** Neither the paper nor the source distinguishes it; the
  anchor and UMI length are flags (`--anchor`, `--umilen`), so it is *probably* a parameter change
  rather than a code change, but nothing primary says so. #225 lists this as unspecified and it stays
  unspecified.

## Sources

- STAR v2.7.11b source and `docs/STARsolo.md` — <https://github.com/alexdobin/STAR>
- zUMIs source, `main` @ 2039a909 — <https://github.com/sdparekh/zUMIs>; paper
  [doi:10.1093/gigascience/giy059](https://doi.org/10.1093/gigascience/giy059)
- umite source @ 0e17c066 — <https://github.com/leoforster/umite>; paper
  [doi:10.1093/bioinformatics/btag075](https://doi.org/10.1093/bioinformatics/btag075),
  [PMC12989134](https://pmc.ncbi.nlm.nih.gov/articles/PMC12989134/)
- GitHub REST API, PyPI JSON API, anaconda.org API — all queried 2026-08-04
- `liulab-runtime` `pyproject.toml` / `pixi.lock` @ 2026.7.22 (`align-rna` = `align-base` + `star`)
- Corroboration only: [nf-core/scrnaseq#497](https://github.com/nf-core/scrnaseq/issues/497)
