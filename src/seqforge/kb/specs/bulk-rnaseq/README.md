# Bulk paired-end RNA-seq

Standard bulk RNA-seq on an Illumina sequencer: two paired-end cDNA reads, **no cell barcode and no
UMI**. Every base is transcript sequence — there's nothing to demultiplex into cells. seqforge aligns
these with plain STAR and counts genes, rather than STARsolo.

## How it's read

- **R1** and **R2** are the two ends of the same cDNA fragment (a mate pair). Both are transcript
  sequence, typically 75–150 bp.
- No barcode, no UMI, no whitelist.

## How seqforge tells it apart from single-cell

It's the **absence** of a barcode that identifies bulk. seqforge looks for a short, low-diversity
technical read — the tell-tale of a cell barcode — and finds none, just two long, near-unique cDNA
mates. A single-cell barcode read is short (26–28 bp) and repeats the same 16 bp prefix across reads;
a bulk mate is long and near-unique from the first base, so a barcode read can never be mistaken for
bulk cDNA.

Because this entry demands so little, it also serves as the **fallback** for any paired-end data.
That's deliberate: when a real single-cell library (BD Rhapsody, SPLiT-seq) happens to share this
loose shape, seqforge falls back to the barcode-list check to make sure it never quietly treats a
single-cell library as bulk.

## Coverage note

This is the paired-end, poly-A branch. Single-end bulk and explicit strand-protocol handling aren't
modeled yet.

## References

The exact, machine-readable definition seqforge uses lives in this entry's `spec.yaml`.
(scg_lib_structs documents single-cell library structures; plain bulk RNA-seq isn't one of them, so
there's no page to link.)
