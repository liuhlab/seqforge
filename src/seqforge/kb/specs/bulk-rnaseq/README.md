# Bulk Illumina RNA-seq

Standard bulk RNA-seq on an Illumina sequencer: cDNA reads with no cell barcode and no UMI. Every
base is transcript sequence, and there are no cells to separate. seqforge aligns these reads with
plain STAR and counts reads per gene, instead of using STARsolo, STAR's single-cell mode.

## How it's read

- **R1 and R2** are the two ends of the same cDNA fragment, a mate pair. Both are transcript
  sequence, usually 75 to 150 bp. seqforge accepts 40 to 300 bp.
- There is no barcode, no UMI and no barcode list.

**Paired-end and single-end are one entry.** They are the same chemistry, sequenced two ways. The
entry declares the paired layout and names R1 alone as its single-end form, and the bytes choose
between them. A deposit with one file per run is read as single-end, not refused. The reads are
described once, so the two forms cannot drift apart.

## How seqforge tells it from single-cell data

The absence of a barcode read identifies bulk. seqforge looks for a short technical read whose
first bases repeat across many reads, which is what a cell barcode looks like. In bulk data it finds
none: only two long cDNA mates whose reads are almost all different from the first base. A 10x
barcode read is 26 or 28 bp and repeats the same 16 bp barcodes, so it is never mistaken for bulk
cDNA.

Because this entry demands so little, it also accepts almost any pair of reads. That is on purpose:
it is the fallback for paired-end data. It also means some single-cell kits look like bulk on the
first, cheap checks. seqforge then settles each case on firmer evidence:

| Kit that can look like bulk | What settles it |
| --- | --- |
| BD Rhapsody (both beads), SPLiT-seq | Their barcode lists. Bulk has no list to match |
| 10x Multiome, chromatin (ATAC) arm | Its barcode read matches the Multiome ATAC list |
| Smart-seq3 | The paper or archive record. Neither kit has a barcode list, so if the record does not say, seqforge asks you |

So seqforge never quietly treats a single-cell library as bulk.

## Coverage note

This entry is for poly(A)-selected libraries. seqforge does not model a strand protocol for bulk
data yet.

## References

The exact definition seqforge runs is this entry's `spec.yaml`. scg_lib_structs documents
single-cell library structures, and plain bulk RNA-seq is not one of them, so there is no page to
link.
