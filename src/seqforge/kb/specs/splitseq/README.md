# SPLiT-seq (split-pool combinatorial barcoding)

Single-cell RNA-seq that needs no special instrument. Instead of one droplet per cell, cells are
split into the wells of a plate, labelled, pooled, and split again, for three rounds. After three
rounds, each cell carries its own combination of three barcodes.

This entry covers the original published SPLiT-seq (Rosenberg et al., *Science* 2018). Parse
Biosciences' Evercode is a commercial descendant with different linkers and barcode positions. It is
a different chemistry, and seqforge does not cover it.

## How it's read

- **Read 1 is the cDNA** (the transcript), read in the same direction as the mRNA.
- **Read 2 is the barcode read**, exactly 94 bp: a 10 bp UMI (unique molecular identifier, a random
  tag on each molecule), then three 8 bp round barcodes separated by two fixed 30 bp linkers.

```text
[10 bp UMI] [8 bp round 3] [30 bp linker 1] [8 bp round 2] [30 bp linker 2] [8 bp round 1]
```

A cell's identity is the combination of its round 1, round 2 and round 3 barcodes. Each round draws
from a list of 96 barcodes, and all three lists ship with seqforge.

The cDNA read can start with the method's template-switch oligo, a 30-base sequence the library
preparation adds. The compiled pipeline clips SPLiT-seq's own version of it, which differs from
10x's at two of thirty bases.

## How seqforge tells it apart

As for BD Rhapsody, the two fixed linker sequences at known places are the signature, and each round
barcode matches its own list. The exact positions matter. The original 2018 chemistry puts the round
1 barcode at bases 87 to 94 of Read 2, while Parse's chemistry puts it eight bases earlier. A position
copied from the wrong source silently misses. seqforge takes the original layout from the paper's own
oligo table, and real data from the paper (GEO series GSE110823) confirms it.

The barcode lists also separate SPLiT-seq from plain paired-end bulk RNA-seq, which has no list to
match.

## One open question: the strand

The strand has not yet been confirmed by aligning real data. seqforge derives it from the paper's
oligos, and the authors' own pipeline agrees. No source claims the opposite. But most pipelines never
chose a strand; they kept the aligner's default. So their agreement is weaker evidence than it
looks. The decisive check is to align the paper's own data both ways: the right strand should assign
far more reads to genes.

## References

The read structure and linker sequences are taken from
[scg_lib_structs: SPLiT-seq](https://teichlab.github.io/scg_lib_structs/methods_html/SPLiT-seq.html)
(Teichmann Lab, CC-BY), and rebuilt on their own from the paper's oligo tables
([Rosenberg et al., *Science* 2018](https://doi.org/10.1126/science.aam8999)). The exact definition
seqforge runs is this entry's `spec.yaml`.
