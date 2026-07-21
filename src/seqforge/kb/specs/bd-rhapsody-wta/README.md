# BD Rhapsody WTA — original fixed-offset cell-label bead

BD Biosciences Rhapsody **Whole Transcriptome Analysis** (3′ single-cell), the **original**
cell-label bead. STARsolo `CB_UMI_Complex`.

## Read structure (pinned from primary sources)

Read 1 (the bead read), 5′→3′, structural length **60 bp** — R1 is over-sequenced past byte 60 into
poly(T)/cDNA, which the fixed CB/UMI offsets ignore:

| segment | length | offset `[start,end)` | STARsolo position |
|---|---|---|---|
| CLS1 (onlist) | 9 bp | `[0, 9)`   | `0_0_0_8`   |
| linker1 (`ACTGGCCTGCGA`) | 12 bp | `[9, 21)`  | — |
| CLS2 (onlist) | 9 bp | `[21, 30)` | `0_21_0_29` |
| linker2 (`GGTAGCGGTGACA`) | 13 bp | `[30, 43)` | — |
| CLS3 (onlist) | 9 bp | `[43, 52)` | `0_43_0_51` |
| UMI (random)  | 8 bp | `[52, 60)` | `0_52_0_59` (`--soloUMIlen 8`) |

Read 2 = the cDNA read.

Sources, triangulated:
- scg_lib_structs BD Rhapsody page (bead-oligo diagram + linker sequences):
  <https://teichlab.github.io/scg_lib_structs/methods_html/BD_Rhapsody.html>
- seqspec `bd_rhapsody_v1.spec.yaml` (segment lengths):
  <https://github.com/pachterlab/seqspec/blob/main/docs/examples/assays/bd_rhapsody_v1.spec.yaml>
- STARsolo maintainer-endorsed position string (reproduces the offsets exactly):
  <https://github.com/alexdobin/STAR/issues/1111>, <https://github.com/alexdobin/STAR/issues/1607>

The `soloCBposition`/`soloUMIposition` above are **derived at compose time** from the element
coordinates (`compose/params.py::derived_params`), never hand-entered — and they come out byte-identical
to the maintainer-endorsed string, which is an independent cross-check on the geometry.

## Cell-label whitelists — 97 × 9 bp per pool (shipped)

Each CLS pool has **97** sequences, not 96 (BD marketing sometimes cites 96³; the published whitelist
is 97³ = 912,673 combinations). Downloaded, verified 97 unique 9-mers each, and packed into the
registry (`bd-rhapsody-cls1/2/3`):

- <https://teichlab.github.io/scg_lib_structs/data/BD/BD_CLS1.txt>
- <https://teichlab.github.io/scg_lib_structs/data/BD/BD_CLS2.txt>
- <https://teichlab.github.io/scg_lib_structs/data/BD/BD_CLS3.txt>

(Ultimate origin: BD's `BDRhapsody_CellLabelSequences_Sept2017.xlsx`.)

## soloStrand = Forward

The bead oligo ends in `(dT)18`; poly(dT) primes reverse transcription off the mRNA poly(A) tail, so
R2 reads the cDNA **sense** to the mRNA — a standard 3′ poly-dT capture library, like 10x Chromium 3′.
STARsolo's Forward = "read strand same as the original RNA molecule". A working BD Rhapsody STARsolo
command (STAR issue #1607) sets `--soloStrand Forward`; scg_lib_structs relies on the Forward default.
No source claims Reverse.

## Scope: the Enhanced bead is a separate technology

The 2022 **Enhanced** bead prepends a **variable-length diversity insert (0–3 bp)** before CLS1 and
shortens both linkers to 4 bp (`GTGA`/`GACA`), which makes the CLS/UMI offsets non-fixed. seqforge
models fixed-offset elements only, so the Enhanced bead needs anchored-element support first and is
**out of scope** for this entry — a separate future spec (`bd_rhapsody_eb` in seqspec), never conflated
with the original bead here.

## EFO

`EFO:0700003` — *BD Rhapsody Whole Transcriptome Analysis* (verified live on EBI OLS4). The sibling
`EFO:0700004` (*BD Rhapsody Targeted mRNA*) is the panel assay, not this one.
