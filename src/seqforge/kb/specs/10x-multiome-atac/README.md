# 10x Chromium Single Cell Multiome ATAC + Gene Expression — ATAC arm (`10x-multiome-atac`)

The **scATAC arm** of the 10x Multiome (ARC) kit. A 10x Multiome experiment produces **two libraries
per sample**: a GEX (3′ snRNA) library and an ATAC library, under **separate SRX/SRR**. This spec is
the ATAC one. The GEX arm is [`10x-multiome-gex`](../10x-multiome-gex/README.md).

It is the KB's **first non-STARsolo chemistry**. Its `backend.module` is **`map/chromap`**, the second
aligner. Its deliverable is a tabix-indexed **`fragments.tsv.gz`**, not a count matrix.

## Why it is structurally unambiguous

The GEX arm is byte-identical to 3′ v3 and separable only by its whitelist. The ATAC arm is told
apart from every scRNA chemistry **structurally**, at rungs 0–2:

| | 10x scRNA (v2/v3/GEX) | 10x Multiome ATAC |
| --- | --- | --- |
| Reads | **2** (cDNA + barcode) | **3** (two genomic mates + barcode) |
| Barcode read | 28 bp (16 CB + 12 UMI) | **16 bp** (16 CB, **no UMI**) |
| Biological reads | one cDNA (open-ended) | **two** genomic (open-ended) |
| Aligner | STARsolo | **chromap** |
| Deliverable | count matrix (`.h5ad`) | **fragments** (`fragments.tsv.gz`) |

The read count (3 vs 2) and the barcode-read length (16 vs 28, no UMI) are DECLARED, not gated. That
is what separates them. Role assignment gives every read a spec declares a role, and no two reads the same one. So a 2-read
scRNA chemistry cannot fill three roles, and this spec's `min_len`/`max_len` on the barcode read
forbid a 28 bp one. This entry's `requires` is empty. It used to hold a `read_count` gate that
abstained on every input, restating the read list beside it. The ARC whitelist (`737K-arc-v1`, shared
with the GEX arm) hitting the barcode read is the rung-3 positive signal. But the structure already
carries the decision.

Before this spec existed, the ATAC arm had no positive target and abstained as
`UNSUPPORTED_TECHNOLOGY`. It now resolves to `10x-multiome-atac` and, through it, to the chromap
pipeline.

## The two symmetric genomic reads

R1 and R3 are both open-ended genomic (`gdna`) reads: the two ends of a Tn5-inserted fragment. They are
**interchangeable** for classification, since each scores the same against the genomic role. So the
role assigner's filename prior breaks the tie by matching `_R1_`/`_R3_`. Either assignment is correct.
chromap maps them as a pair (`-1`/`-2`), and the units.tsv `run` column pairs a pooled sample's files,
not the R1/R3 order.

## Counting is not chemistry — there is nothing to count

ATAC has no gene axis, so there is no count matrix and no counting choice to instruct. The deliverable
is the fragments file itself. That makes the parse/count split trivial: the recipe's `AtacQuant` carries
no knob. So an ATAC recipe never asks the Gene-vs-GeneFull question a nuclear RNA one does.

## What is byte-decided vs. a module detail

`backend.params` carries exactly one key, `barcode_whitelist`. It is the ARC list chromap corrects the
cell barcode against (`chromap --barcode-whitelist`). It resolves through the same `{onlist:<alias>}`
mechanism as STARsolo's `soloCBwhitelist`. Everything else chromap needs is **not** a parse param. It
is either a fixed module detail or read geometry the manifest already states. `--preset atac` is
hardcoded in `chromap.smk`, the way `star.smk` hardcodes `--outSAMtype`. Which file is the barcode
read arrives via `read_files_in`.

## Real-data note

The canonical geometry here is a 16 bp barcode read (R2). Some ARC ATAC runs sequence R2 longer: a 16 bp
barcode plus a spacer. Two things are deferred to the GSE283483 end-to-end pass, where real chromap
mapping runs on arc-hpc. One is fitting that geometry against real bytes. The other is the real ARC
ATAC barcode orientation, which can be the reverse complement of the GEX barcode.
