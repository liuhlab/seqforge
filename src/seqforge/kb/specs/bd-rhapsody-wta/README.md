# BD Rhapsody WTA (Whole Transcriptome Analysis)

BD Biosciences' droplet-free single-cell 3' RNA-seq. Cells are captured in a microwell plate and
tagged with a **cell label** built from three barcode blocks on the capture bead, plus a **UMI** on
each molecule.

BD ships this chemistry in two bead generations, and this page covers both. The **original
fixed-position bead** (described first) is what seqforge recognizes and compiles automatically today.
The 2022 **[Enhanced bead](#enhanced-beads-2022)** is a newer variant with a variable-length diversity
insert; its structure and how to run it are documented below, and automated support is in progress
([#43](https://github.com/liuhlab/seqforge/issues/43)).

## How it's read

Two reads:

- **R2 = the cDNA** (the transcript), read sense to the mRNA.
- **R1 = the bead read**: three 9 bp cell-label blocks separated by two fixed linker sequences, then
  an 8 bp UMI.

| segment on R1 | length | position |
| --- | --- | --- |
| cell label 1 | 9 bp | 0–9 |
| linker 1 (`ACTGGCCTGCGA`) | 12 bp | 9–21 |
| cell label 2 | 9 bp | 21–30 |
| linker 2 (`GGTAGCGGTGACA`) | 13 bp | 30–43 |
| cell label 3 | 9 bp | 43–52 |
| UMI | 8 bp | 52–60 |

Each cell-label block is drawn from its own published list of **97** 9-mers, so there are 97³ ≈
913,000 possible cell labels. R1 is often sequenced past position 60 into poly(T)/cDNA; those extra
bases are ignored.

## How seqforge tells it apart

The **two fixed linker sequences at known positions** are the signature — no other supported
chemistry has them. Each cell-label block also matches its own barcode list, which both confirms the
call and separates BD Rhapsody from a plain bulk paired-end library (bulk has no barcode list to
match).

## Strand

The bead's capture oligo ends in poly(dT), which primes off the mRNA poly(A) tail, so R2 reads the
cDNA **sense** to the transcript — a standard 3' poly-dT library, like 10x 3'. Getting this backwards
would leave most reads uncounted while the aligner still exits cleanly, so seqforge derives it from
the bead chemistry rather than guessing.

## Enhanced beads (2022)

In 2022 BD introduced **Enhanced Cell Capture Beads**, the current chemistry. The cell labels work the
same way — three 9 bp blocks read off the bead — but two things change:

- A short **diversity insert** of 0–3 bp (one of *nothing*, `A`, `GT`, or `TCA`) is added at the very
  start of Read 1, to stagger the reads and cut the amount of PhiX needed.
- The two linkers shrink to **`GTGA`** and **`GACA`** (4 bp each), from v1's 12 and 13 bp.

So Enhanced Read 1 reads:

```text
[0–3 bp insert] [CLS1 · 9] GTGA [CLS2 · 9] GACA [CLS3 · 9] [UMI · 8]
```

There are two sub-versions, differing only in the cell-label lists: **96** or **384** sequences per
block (the 384-list "Enhanced v2" allows more cell labels). The read layout is otherwise identical.

**Why it needs dedicated support.** The diversity insert shifts every barcode by a different amount
from one read to the next, so the positions are no longer fixed. seqforge's byte model reads fixed
positions today, so it does not yet recognize the Enhanced bead on its own — that work is tracked in
**[#43](https://github.com/liuhlab/seqforge/issues/43)**, and this page will be updated when it lands.

**Running Enhanced data now.** STARsolo can map Enhanced reads directly by anchoring to the linkers,
so you can process it today with a hand-written command (endorsed by STAR's author in
[issue #1607](https://github.com/alexdobin/STAR/issues/1607)):

```bash
STAR --soloType CB_UMI_Complex \
     --soloAdapterSequence NNNNNNNNNGTGANNNNNNNNNGACA \
     --soloCBmatchWLtype 1MM multi \
     --soloCBposition 2_0_2_8 2_13_2_21 3_1_3_9 \
     --soloUMIposition 3_10_3_17 \
     --soloCBwhitelist BD_CLS1.txt BD_CLS2.txt BD_CLS3.txt
```

The adapter `NNNNNNNNNGTGANNNNNNNNNGACA` is `CLS1(9) + GTGA + CLS2(9) + GACA`; STARsolo finds it in each
read and reads the barcodes relative to it, so the variable insert at the front takes care of itself.
For a reproducible workflow spanning every bead generation, see
**[rhapsodist](https://github.com/imallona/rhapsodist)**.

## References

Read structure, linker sequences, and barcode lists pinned from
**[scg_lib_structs — BD Rhapsody](https://teichlab.github.io/scg_lib_structs/methods_html/BD_Rhapsody.html)**
(Teichmann Lab, CC-BY), cross-checked against the seqspec `bd_rhapsody_v1` example and the STARsolo
maintainer's endorsed settings. The exact, machine-readable definition seqforge uses lives in this
entry's `spec.yaml`.
