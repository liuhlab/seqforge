# 10x Chromium Single Cell Multiome ATAC + Gene Expression — ATAC arm (`10x-multiome-atac`)

The **scATAC arm** of the 10x Multiome (ARC) kit. A 10x Multiome experiment produces **two libraries
per sample** — a GEX (3′ snRNA) library and an ATAC library — under **separate SRX/SRR**. This spec is
the ATAC one; the GEX arm is [`10x-multiome-gex`](../10x-multiome-gex/README.md).

It is the KB's **first non-STARsolo chemistry**: its `backend.module` is **`map/chromap`**, the second
aligner, and its deliverable is a tabix-indexed **`fragments.tsv.gz`**, not a count matrix.

## Why it is structurally unambiguous

Unlike the GEX arm — which is byte-identical to 3′ v3 and separable only by its whitelist — the ATAC arm
is told apart **structurally**, at rungs 0–2, from every scRNA chemistry:

| | 10x scRNA (v2/v3/GEX) | 10x Multiome ATAC |
| --- | --- | --- |
| Reads | **2** (cDNA + barcode) | **3** (two genomic mates + barcode) |
| Barcode read | 28 bp (16 CB + 12 UMI) | **16 bp** (16 CB, **no UMI**) |
| Biological reads | one cDNA (open-ended) | **two** genomic (open-ended) |
| Aligner | STARsolo | **chromap** |
| Deliverable | count matrix (`.h5ad`) | **fragments** (`fragments.tsv.gz`) |

The read count (3 vs 2) and the barcode-read length (16 vs 28, no UMI) are hard `requires` gates, so a
2-read scRNA chemistry can never claim these reads and vice versa. The ARC whitelist (`737K-arc-v1`,
shared with the GEX arm) hitting the barcode read is the rung-3 positive signal, but the structure
already carries the decision.

Before this spec existed the ATAC arm had no positive target and abstained as
`UNSUPPORTED_TECHNOLOGY`; it now resolves to `10x-multiome-atac` and, through it, to the chromap
pipeline.

## The two symmetric genomic reads

R1 and R3 are both open-ended genomic (`gdna`) reads — the two ends of a Tn5-inserted fragment. They are
**interchangeable** for classification (each scores the same against the genomic role), so the
role assigner's filename prior breaks the tie by matching `_R1_`/`_R3_`; either assignment is correct
because chromap maps them as a pair (`-1`/`-2`) and the units.tsv `run` column, not the R1/R3 order,
pairs a pooled sample's files.

## Counting is not chemistry — there is nothing to count

ATAC has no gene axis, so there is no count matrix and no counting choice to instruct: the deliverable
is the fragments file itself. That makes the parse/count split trivial (the recipe's `AtacQuant` carries
no knob), which is why an ATAC recipe never asks the Gene-vs-GeneFull question a nuclear RNA one does.

## What is byte-decided vs. a module detail

`backend.params` carries exactly one key — `barcode_whitelist` (the ARC list chromap corrects the cell
barcode against, `chromap --barcode-whitelist`), resolved through the same `{onlist:<alias>}` mechanism
as STARsolo's `soloCBwhitelist`. Everything else chromap needs is either a fixed module detail
(`--preset atac`, hardcoded in `chromap.smk` the way `star.smk` hardcodes `--outSAMtype`) or read
geometry the manifest already states (which file is the barcode read arrives via `read_files_in`), so it
is **not** a parse param.

## Real-data note

The canonical geometry here is a 16 bp barcode read (R2). Some ARC ATAC runs sequence R2 longer (a 16 bp
barcode plus a spacer); fitting that against real bytes — and the real ARC ATAC barcode orientation,
which can be the reverse complement of the GEX barcode — is deferred to the GSE283483 end-to-end pass
(real chromap mapping runs on arc-hpc).
