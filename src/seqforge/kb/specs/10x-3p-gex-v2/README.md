# 10x Chromium Single Cell 3' Gene Expression v2

Droplet single-cell RNA-seq with 10x Genomics' second-generation 3' kit, the one before v3. Each
droplet holds one gel bead. The bead tags every transcript of its cell with the same **cell
barcode**, and each mRNA molecule with its own **UMI** (unique molecular identifier, a random tag).
Together they let you count molecules per gene per cell.

## How it's read

| Read | Length | What it holds |
| --- | --- | --- |
| **R1** | 26 bp | a 16 bp cell barcode, then a 10 bp UMI |
| **R2** | any length | the cDNA (the transcript itself), read in the same direction as the mRNA |

The cell barcodes come from 10x's `737K-august-2016` list, about 737,000 barcodes.

## How seqforge tells v2 from v3

v2's R1 is 16 + 10 = 26 bp. v3's is 16 + 12 = 28 bp. When R1 is the canonical length, that 2 bp
difference settles it before any barcode list is checked.

Some runs sequence R1 long, to 100 bp or more. Then both kits accept the read, and the barcode list
decides: v2 uses `737K-august-2016` and v3 uses `3M-february-2018`.

If a paper or record says "v2" while the reads say v3, seqforge does not quietly pick one. It
reports the disagreement and asks you. The bytes decide what the data is.

## The one neighbour the reads cannot settle

10x **5' v1 and v2** match this kit in every byte. They have the same 26 bp R1 (16 bp barcode and
10 bp UMI), the same cDNA R2, and the same `737K-august-2016` barcode list. So neither the read
layout nor the list can tell them apart.

The two are aligned differently in one setting: the strand. 5' kits read the transcript from the
other end. No check on the reads can see that.

So this is the only pair in the knowledge base that the reads cannot settle. seqforge turns to the
paper or the archive record, which name "5'" or "3'" reliably even when they are vague about the
version. A trial alignment can also settle it. If neither does, seqforge asks you.

The risk is real. Before the 5' entry existed, a 5' library was read as this kit and aligned on the
wrong strand. Its gene expression correlated with the right answer at only 0.5 to 0.6 (more than
0.98 on the right strand), and the run still exited with no error.

v3 has no such twin: each kit that shares its 28 bp layout has its own barcode list.

## What goes wrong

- **The barcode read can go missing on SRA.** `fasterq-dump` without `--include-technical` drops R1.
  seqforge then stops rather than guessing which file is which. Fetch the data again with
  `--include-technical`, or download the submitter's original files.
- **SRA rewrites read names**, so seqforge never uses them to group files into samples.

## References

The read structure is checked against
[scg_lib_structs: 10x Chromium 3'](https://teichlab.github.io/scg_lib_structs/methods_html/10xChromium3.html)
(Teichmann Lab, CC-BY), which shows the v2 to v4 kits side by side. The exact definition seqforge
runs is this entry's `spec.yaml`.
