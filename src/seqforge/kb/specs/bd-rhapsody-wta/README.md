# BD Rhapsody WTA (Whole Transcriptome Analysis)

BD Biosciences' droplet-free single-cell 3' RNA-seq. Cells are captured in a microwell plate and
tagged with a **cell label** built from three barcode blocks on the capture bead, plus a **UMI** on
each molecule.

This entry covers the **original fixed-position cell-label bead**. (The 2022 "Enhanced" bead adds a
variable-length insert that shifts every barcode position — a different chemistry, not yet supported;
see [Scope](#scope-the-enhanced-bead-is-separate).)

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

## Scope: the Enhanced bead is separate

The 2022 Enhanced bead prepends a variable-length (0–3 bp) diversity insert before the first cell
label and shortens the linkers, which moves every downstream position. seqforge models fixed-position
barcodes only, so the Enhanced bead needs its own entry and is out of scope here — never conflate the
two.

## References

Read structure, linker sequences, and barcode lists pinned from
**[scg_lib_structs — BD Rhapsody](https://teichlab.github.io/scg_lib_structs/methods_html/BD_Rhapsody.html)**
(Teichmann Lab, CC-BY), cross-checked against the seqspec `bd_rhapsody_v1` example and the STARsolo
maintainer's endorsed settings. The exact, machine-readable definition seqforge uses lives in this
entry's `spec.yaml`.
