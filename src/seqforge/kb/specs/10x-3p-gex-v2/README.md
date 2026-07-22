# 10x Chromium Single Cell 3' Gene Expression v2

Droplet single-cell RNA-seq — the generation before v3. Every droplet holds one gel bead, which tags
all of a cell's transcripts with the same **cell barcode** and each individual mRNA molecule with a
unique **UMI**. That lets you count molecules per gene per cell.

## How it's read

Two reads come off the sequencer:

| read | length | what it holds |
| --- | --- | --- |
| **R1** | 26 bp | a 16 bp **cell barcode** + a 10 bp **UMI** |
| **R2** | open-ended | the **cDNA** (the transcript itself), read sense to the mRNA |

The cell barcode is drawn from 10x's published `737K-august-2016` list (~737,000 barcodes).

## How seqforge tells v2 apart from v3

The only difference visible in the reads is **R1 length**: v2 is 16 + **10** = **26 bp**; v3 is
16 + **12** = **28 bp**. That 2 bp gap is decisive on its own, so seqforge separates v2 from v3 by
read geometry alone — before it ever consults a barcode list. The two are never confused. (Their
whitelists differ too — `737K-august-2016` vs `3M-february-2018` — but length settles it first.)

If a paper or database says "v2" while the reads are actually 28 bp, seqforge does **not** quietly
pick one. It surfaces the disagreement (metadata says 26 bp, the bytes say 28 bp) and hands it to a
human — the bytes decide what the data is.

## Gotchas

- **The barcode read can go missing on SRA.** `fasterq-dump` without `--include-technical` drops R1
  entirely; seqforge blocks rather than guessing which file is which. Re-fetch with
  `--include-technical`, or pull the submitter's original files.
- SRA rewrites read-name headers, so seqforge never trusts them when grouping files into samples.

## References

Read structure cross-checked against
**[scg_lib_structs — 10x Chromium 3'](https://teichlab.github.io/scg_lib_structs/methods_html/10xChromium3.html)**
(Teichmann Lab, CC-BY), which lays out the v2–v4 kits side by side. The exact, machine-readable
definition seqforge uses lives in this entry's `spec.yaml`.
