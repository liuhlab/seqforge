# Knowledge base

The knowledge base is how seqforge knows what a sequencing library *is*. It holds one entry per
technology — the read layout, the barcode positions, the barcode lists — and seqforge scores your
files against every entry to find the one the bytes actually match. The entry it picks decides how
your data is parsed.

You don't choose an entry; the bytes do. These pages are here so you can see what seqforge recognises,
and understand the answer when it tells you what your data is.

## Supported assays

| assay | what it is | told apart by |
| --- | --- | --- |
| [10x Chromium 3' v3 / v3.1](10x-3p-gex-v3.md) | droplet single-cell, current 3' kit | 28 bp barcode read + 10x's barcode list |
| [10x Chromium 3' v2](10x-3p-gex-v2.md) | droplet single-cell, previous 3' kit | 26 bp barcode read (2 bp shorter than v3) |
| [BD Rhapsody WTA](bd-rhapsody-wta.md) | microwell single-cell, 3-block cell label | two fixed linkers + three barcode lists |
| [SPLiT-seq](splitseq.md) | instrument-free split-pool single-cell | two fixed linkers + three round-barcode lists |
| [Bulk paired-end RNA-seq](bulk-rnaseq-pe.md) | plain bulk RNA-seq, no barcodes | the *absence* of a barcode read |

## How the entries are organized

Related chemistries are grouped into a **family**. The 10x 3' family, for example, is a single
loose pattern ("a 16 bp barcode in a 26–28 bp read") that first says "this is 10x 3' something," then
**descends** to the exact member — v2, v3, or v3.1 — by read length and barcode list. Grouping this
way means seqforge can recognise a family from a fuzzy metadata mention and still pin the precise
chemistry from the bytes.

Two chemistries that are genuinely indistinguishable from the reads *and* processed identically
(v3 and v3.1) are recorded as both names, with no question asked. Two that look alike but would be
processed differently are separated by whatever can tell them apart — usually the barcode list — and
if nothing can, seqforge asks you rather than guessing.

## Adding a technology

Every entry is executable: it declares a chemistry precisely enough that seqforge can generate
synthetic reads from it, probe them, and check it recovers what it declared. If you want to teach
seqforge a new chemistry, the [Adding a technology](../tutorials/adding-a-technology.md) tutorial
walks through it.

For the human-readable descriptions of each library structure, the Teichmann Lab's
[scg_lib_structs](https://teichlab.github.io/scg_lib_structs/) is an excellent reference, and each
assay page below links to its entry there where one exists.
