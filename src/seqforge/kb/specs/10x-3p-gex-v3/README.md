# 10x Chromium Single Cell 3' Gene Expression v3 (and v3.1)

Droplet single-cell RNA-seq with 10x Genomics' third-generation 3' kit. Each droplet holds one gel
bead. The bead tags every transcript of its cell with the same **cell barcode**, and each mRNA
molecule with its own **UMI** (unique molecular identifier, a random tag that lets you count
molecules rather than reads).

This page covers both v3 and v3.1. To a sequencer they are the same library, and seqforge processes
them the same way. See [v3 vs v3.1](#v3-vs-v31) below.

## How it's read

| Read | Length | What it holds |
| --- | --- | --- |
| **R1** | 28 bp | a 16 bp cell barcode, then a 12 bp UMI |
| **R2** | any length | the cDNA (the transcript itself), read in the same direction as the mRNA |

The cell barcodes come from 10x's `3M-february-2018` list, about 6.8 million barcodes. Index reads
(I1, I2) may be present or not. seqforge does not need them.

Names you'll see in papers and records: "10x 3' v3", "Chromium 3' v3", "SC3Pv3", "single cell
3-prime v3", "Next GEM 3' v3.1". A paper often writes "10x 3' v3" for a v3.1 run, or names the kit
without a version. That changes nothing, as the next section explains.

## v3 vs v3.1

To seqforge the two are identical: the same read layout, the same barcode list, the same alignment
settings. No check on the reads can tell them apart, and none needs to. So seqforge records both
names and asks you nothing. v3.1 has its own knowledge-base entry only so that the test suite checks
on every build that the two really are identical.

## How seqforge tells it from other kits

- **3' v2** has a 10 bp UMI, so its R1 is 26 bp, not 28 bp. When R1 is the canonical length, that
  2 bp difference settles it. When R1 was sequenced long (100 bp or more), both kits accept it, and
  the barcode list decides: v2 uses a different list.
- **Multiome gene expression, GEM-X 3' v4 and GEM-X 5' v3** all have the same 28 bp layout, with a
  16 bp barcode and a 12 bp UMI. Each uses its own barcode list, so seqforge checks which list the
  barcodes hit. The read layout narrows the choice to these four, and the list picks one. 5' v3 also
  reads the cDNA in the other direction, but the reads cannot show that. Its list is what settles it.
- **The pair the reads cannot settle is one generation back**: 3' v2 against 5' v1 and v2. It does
  not involve v3. See the [3' v2 page](10x-3p-gex-v2.md).

## What goes wrong

- **The barcode read can go missing on SRA.** `fasterq-dump` without `--include-technical` drops
  the 28 bp R1. seqforge then stops rather than inventing a barcode read from a file name. Fetch the
  data again with `--include-technical`, or download the submitter's original files.
- **A pre-trimmed upload** no longer has R1 at one fixed length, which moves the barcode. seqforge
  refuses rather than reading the barcode from the wrong place.
- **SRA rewrites read names**, so seqforge never uses them to group files into samples.

## References

The read structure is checked against
[scg_lib_structs: 10x Chromium 3'](https://teichlab.github.io/scg_lib_structs/methods_html/10xChromium3.html)
(Teichmann Lab, CC-BY). The exact definition seqforge runs is each entry's `spec.yaml`.
