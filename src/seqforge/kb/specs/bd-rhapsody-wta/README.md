# BD Rhapsody WTA (Whole Transcriptome Analysis)

Single-cell 3' RNA-seq from BD Biosciences, with no droplets. Cells settle into the wells of a
microwell cartridge, one per well, together with a capture bead. The bead tags each cell's
transcripts with a **cell label** built from three barcode blocks, and each molecule with a **UMI**
(unique molecular identifier, a random tag).

BD has sold two bead generations. seqforge recognizes and compiles both from the reads, so you never
have to say which bead a dataset used. The original bead, with every block at a fixed place, comes
first below. The 2022 [Enhanced bead](#enhanced-beads-2022) follows.

## How it's read

- **R2 is the cDNA** (the transcript), read in the same direction as the mRNA.
- **R1 is the bead read**: three 9 bp cell-label blocks separated by two fixed linker sequences,
  then an 8 bp UMI.

| Part of R1 | Length | Bases |
| --- | --- | --- |
| cell label 1 | 9 bp | 1 to 9 |
| linker 1 (`ACTGGCCTGCGA`) | 12 bp | 10 to 21 |
| cell label 2 | 9 bp | 22 to 30 |
| linker 2 (`GGTAGCGGTGACA`) | 13 bp | 31 to 43 |
| cell label 3 | 9 bp | 44 to 52 |
| UMI | 8 bp | 53 to 60 |

Each cell-label block comes from its own list of 97 9-mers, so there are 97³, about 913,000,
possible cell labels. R1 is often sequenced past base 60 into the poly(T) stretch and the cDNA.
seqforge ignores those extra bases.

## How seqforge tells it apart

The two fixed linkers at known places are the signature. No other supported kit has them. Each
cell-label block also matches its own barcode list. That confirms the call, and it separates BD
Rhapsody from plain paired-end bulk RNA-seq, which has no barcode list to match.

## Strand

The bead's capture oligo ends in poly(dT), which binds the mRNA's poly(A) tail. So R2 reads the cDNA
in the same direction as the transcript, as in any 3' poly(dT) library such as 10x 3'. A wrong strand
would leave most reads uncounted while the aligner still exits with no error, so seqforge takes the
strand from the bead chemistry rather than guessing.

## Enhanced beads (2022)

In 2022 BD introduced Enhanced Cell Capture Beads, the current chemistry. The cell labels work the
same way, three 9 bp blocks read off the bead, but two things change:

- A short **diversity insert** of 0 to 3 bases (nothing, `A`, `GT` or `TCA`) comes at the very start
  of R1. It staggers the reads so the sequencer needs less PhiX control DNA.
- The two linkers shrink to `GTGA` and `GACA`, 4 bp each, from the original bead's 12 bp and 13 bp.

So an Enhanced R1 reads:

```text
[0-3 bp insert] [cell label 1, 9 bp] GTGA [cell label 2, 9 bp] GACA [cell label 3, 9 bp] [UMI, 8 bp]
```

There are two versions, which differ only in their cell-label lists. BD calls them 96-plex and
384-plex. The 96-plex bead uses the same lists as the original bead (97 barcodes each). The 384-plex
bead uses three lists of 384 barcodes, which allow more cell labels. The read layout is the same.

### How seqforge handles the Enhanced bead

You do not name the bead generation, and you do not trim the diversity insert.

- **Recognition.** The insert moves every barcode by a different amount from one read to the next,
  so no position is fixed. seqforge finds the `GTGA...GACA` frame in each read, allowing for the
  0 to 3 base stagger and for sequencing errors, and reads the cell labels and the UMI relative to
  it. That frame is what separates the Enhanced bead from the original one, whose longer linkers sit
  at fixed places.
- **96-plex or 384-plex.** The two have the same read layout, so seqforge picks the one whose lists
  the barcodes hit. It tells 10x v2 from v3 the same way when the reads are long. Both sets of lists
  ship with seqforge.
- **Alignment.** STARsolo, the single-cell mode of the STAR aligner, can find the linker frame by
  itself. seqforge computes the settings from the read layout, the ones STAR's author endorsed in
  [STAR issue 1607](https://github.com/alexdobin/STAR/issues/1607):

  ```bash
  STAR --soloType CB_UMI_Complex \
       --soloAdapterSequence NNNNNNNNNGTGANNNNNNNNNGACA \
       --soloCBposition 2_0_2_8 2_13_2_21 3_1_3_9 \
       --soloUMIposition 3_10_3_17 \
       --soloCBwhitelist BD_CLS1.txt BD_CLS2.txt BD_CLS3.txt
  ```

  The adapter `NNNNNNNNNGTGANNNNNNNNNGACA` is cell label 1 (9 bases), `GTGA`, cell label 2
  (9 bases) and `GACA`. STARsolo finds it in each read and reads the barcodes relative to it, so the
  insert at the front needs no special step. For a reference workflow that covers every bead
  generation, see [rhapsodist](https://github.com/imallona/rhapsodist).

## References

The read structure, linker sequences and barcode lists are taken from
[scg_lib_structs: BD Rhapsody](https://teichlab.github.io/scg_lib_structs/methods_html/BD_Rhapsody.html)
(Teichmann Lab, CC-BY), and checked against the seqspec `bd_rhapsody_v1` example and the settings
the STAR author endorsed. The exact definition seqforge runs is in `spec.yaml`, in this folder and
in the Enhanced entries' folders.
