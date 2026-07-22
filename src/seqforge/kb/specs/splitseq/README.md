# SPLiT-seq (split-pool combinatorial barcoding)

A single-cell RNA-seq method that needs no special instrument. Instead of one droplet per cell, cells
are barcoded by **splitting and pooling** them through several rounds of labeling; after three rounds
each cell carries a unique combination of three barcodes.

This entry covers the **original published SPLiT-seq** (Rosenberg et al., *Science* 2018). Parse
Biosciences' **Evercode** is a separate commercial descendant with different linkers and barcodes — a
different chemistry, not covered here.

## How it's read

- **Read 1 = the cDNA** (the transcript).
- **Read 2 = the barcode read**: a 10 bp UMI, then three 8 bp round-barcodes separated by two fixed
  30 bp linkers.

```text
[10 bp UMI] [8 bp round-3] --linker1-- [8 bp round-2] --linker2-- [8 bp round-1]
```

A cell's identity is the combination of its round-1, round-2, and round-3 barcodes; each round draws
from a list of ~96 barcodes.

## How seqforge tells it apart

Like BD Rhapsody, the **two fixed linker sequences at known positions** are the signature, and each
round-barcode matches its own list. The exact linker lengths matter: the original 2018 chemistry puts
round-1 at a different position than the later Parse chemistry, so a position copied from the wrong
source silently mismatches. seqforge pins the original layout from the paper's own oligos.

## Status: not yet ready for real data

SPLiT-seq is the pilot's generalization test — it exercises machinery the 10x entries don't (8 bp
barcodes instead of 16, a barcode split combinatorially across a read). Two things must land before
it can process real data:

- **The barcode lists must ship.** The entry names three round-barcode lists that aren't bundled yet.
- **The strand must be confirmed on real data.** It's derived from the paper's oligos and the authors'
  own pipeline (both point the same way), but the honest state is that most pipelines never explicitly
  chose the strand — they inherited a default. The decisive check is to run the paper's own data both
  ways; the correct strand assigns far more reads.

Until then this entry is intentionally inert.

## References

Read structure and linker sequences pinned from
**[scg_lib_structs — SPLiT-seq](https://teichlab.github.io/scg_lib_structs/methods_html/SPLiT-seq.html)**
(Teichmann Lab, CC-BY) and independently reconstructed from the paper's oligo tables
([Rosenberg et al., *Science* 2018](https://doi.org/10.1126/science.aam8999)). The exact,
machine-readable definition seqforge uses lives in this entry's `spec.yaml`.
