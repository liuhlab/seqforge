"""``umite`` — the plate-assay counting engine, re-implemented as seqforge code.

A one-cell-one-file library (SMART-seq3 and its relatives) demultiplexed at the bench, so the cell
barcode is the *file* and not a read. What the reference package adds on top of an aligner is small
and entirely ours to own: lift the tagged-molecule UMI out of R1, and count reads and UMIs into a
tagged/internal x exon/intron split. Neither is genome-file machinery and neither is an aligner
environment, so re-implementing them here forks nothing — the same line ``cram.py`` draws, where the
foreign binary stays in the image and the logic is ours. Here there is no foreign binary at all.

The two halves are two modules and two verbs, because they run at different fan-out: extraction is
per cell (one job per file, thousands of them) and counting is the fan-in over all of them. They
land independently, so **this package re-exports nothing** — import from the module that owns the
function (``from .extract import extract_umis``) rather than from here, and neither half's arrival
edits the other's import line.
"""
