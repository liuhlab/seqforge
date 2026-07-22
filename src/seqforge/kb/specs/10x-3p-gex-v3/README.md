# 10x Chromium Single Cell 3' Gene Expression v3 (and v3.1)

Droplet single-cell RNA-seq — the current-generation 3' kit. Every droplet holds one gel bead, which
tags all of a cell's transcripts with the same **cell barcode** and each mRNA molecule with a unique
**UMI**.

This page covers **both v3 and v3.1**. To a sequencer they are the same library, and seqforge
processes them identically — see [v3 vs v3.1](#v3-vs-v31) below.

## How it's read

| read | length | what it holds |
| --- | --- | --- |
| **R1** | 28 bp | a 16 bp **cell barcode** + a 12 bp **UMI** |
| **R2** | open-ended | the **cDNA** (the transcript itself), read sense to the mRNA |

The cell barcode is drawn from 10x's `3M-february-2018` list (~6.8 million barcodes). Index reads
(I1/I2) may or may not be present; seqforge doesn't need them.

Names you'll see in the wild: "10x 3' v3", "Chromium 3' v3", "SC3Pv3", "single cell 3-prime v3",
"Next GEM 3' v3.1". Papers often write only "10x 3' v3" for a v3.1 run, or name the kit and not the
version — which changes nothing, as the next section explains.

## v3 vs v3.1

They are **identical to seqforge**: same read layout, same barcode list, same alignment settings. No
probe can tell them apart from the reads, and none needs to — so seqforge records both names and asks
you nothing. v3.1 exists as its own knowledge-base entry only so that "these two really are
identical" is something the test suite re-checks on every build, rather than a claim sitting in a
comment.

## How seqforge tells it apart from other chemistries

- **v2** — a 16 bp barcode + a **10** bp UMI makes a **26 bp** R1 (vs v3's 28 bp), on a different
  list. The 2 bp length difference alone separates them.
- **Multiome (ARC) and GEM-X 3' v4** produce the same 28 bp / 16+12 layout — here **only the barcode
  list** tells them apart, because each uses a different one. Geometry narrows to a family; the list
  picks the exact member.
- **5' kits** share the barcode/UMI geometry but read the cDNA in the opposite direction. The reads
  can't reveal that, so it takes metadata or a trial alignment, not geometry.

## Gotchas

- **The barcode read can go missing on SRA.** `fasterq-dump` without `--include-technical` drops the
  28 bp R1; seqforge blocks rather than inventing a barcode read from a filename. Re-fetch with
  `--include-technical`, or pull the submitter's original files.
- **A pre-trimmed upload** (R1 no longer a single fixed length) shifts the barcode offsets — seqforge
  refuses rather than reading the barcode from the wrong place.
- SRA rewrites read-name headers, so seqforge never trusts them when grouping files into samples.

## References

Read structure cross-checked against
**[scg_lib_structs — 10x Chromium 3'](https://teichlab.github.io/scg_lib_structs/methods_html/10xChromium3.html)**
(Teichmann Lab, CC-BY). The exact, machine-readable definition seqforge uses lives in each entry's
`spec.yaml`.
