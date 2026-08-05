# Smart-seq3

Plate-based single-cell RNA-seq (Hagemann-Jensen et al., *Nature Biotechnology* 2020). Cells are
sorted one per well, so **demultiplexing happened at the bench**: the cell barcode is the *file*, not
a read, and one FASTQ pair is one cell. There is no whitelist anywhere in this entry, because there
is nothing to look a barcode up in.

## How it's read

Each cell's R1 holds a **mixture of two read populations from the same library**, and only one of
them carries structure:

- the **tagged** minority begins with an 11 bp TSO tag `ATTGCGCAATG`, then an 8 bp UMI, then the
  `GGG` a template switch leaves, then cDNA from base 22. These are the 5′ reads a UMI can
  deduplicate;
- the **untagged** majority is internal cDNA fragments, byte-identical to what a plain bulk library
  would deposit.

R2 is plain cDNA in both populations. The entry's `reads` block describes the tagged layout, which is
why the recognition test below is a *proportion* over reads rather than a per-cycle purity.

The tag itself is not sequenced by accident: the Read 1 primer ends `...GTGTATAAGAGACAG`, so the
`AGAGACAG` of the TSO is consumed by the primer and the read genuinely begins at `ATTGCGCAATG`, at
offset 0 with no anchor to find.

## The two sequencing configurations

The Methods publish three — *"75-bp single end, 50-bp single end or 150-bp paired end"* — which are
two read sets here, because the two single-end recipes differ in cycle count and not in structure.
`reads` is the paired one and `read_sets.se` names R1 alone (ADR-0029). R1 and not R2, and that is
structural rather than conventional: R1 is the read carrying `tso_tag` + `umi` + `tso_ggg` + `cdna_r1`
— every element that says Smart-seq3 — so it is the one that survives on its own, while R2 is plain
cDNA indistinguishable from bulk.

A single-end deposit runs the same pipeline. The extraction is entirely *within* the tagged read, so
the mate is an addition rather than half of it (ADR-0033): the extractor takes one FASTQ, writes one
unpaired record per fragment, and the aligner is told `SAM SE` instead of `SAM PE`.

## How seqforge tells it apart from bulk

**By anchoring, not by the motif.** The whole separation is one test: the 11 bp tag at *read start*,
at most 2 mismatches, in at least 2% of reads. Measured at offset 0 over 2000-read slices, ten
published cells run 39.6–67.6% on R1 and 0.00–0.15% on R2, and a bulk control 0.00% / 0.05%. The same
motif searched *anywhere* does not separate anything — this chemistry's own R2 carries it at 8–20%.

Two things are deliberately absent, and both would break the entry:

- **no majority gate.** The tagged fraction is a knob the bench turns — the paper's own figure titles
  it "Effect of tagmentation conditions on the fraction of UMI-containing reads" — and runs 6.9–70.5%
  across five published libraries. A gate at 50% would refuse the authors' own reference data.
- **no exclusion of the Tn5 mosaic end.** It appears in 6.5–79.5% of this chemistry's *own* R1 by
  read-through, so anti-gating it would reject Smart-seq3 itself. It is also non-positional, so it
  cannot confound an anchored test.

Against the generic paired-end fallback the acceptance is one-directional: Smart-seq3 rejects bulk's
reads outright, while bulk accepts a Smart-seq3 pair on read count and length alone. Both entries
therefore declare the other, and the tie is broken by **metadata** — there is no whitelist on either
side for rung 3 to reach for.

## What is not modelled

- **Smart-seq3xpress.** A separate publication with no ontology term of its own and a different cDNA
  start — and that start is exactly the geometry the extractor is derived from, so folding it in here
  would launder an unresolved disagreement between primary sources into a derived parameter. It is a
  future sibling entry, never an alias of this one.

## References

- Hagemann-Jensen et al. 2020, *Nature Biotechnology* — the protocol, the verbatim TSO, and the three
  published sequencing configurations.
- [scg_lib_structs, SMART-seq family](https://teichlab.github.io/scg_lib_structs/methods_html/SMART-seq_family.html#SMART-seq3)
  — the oligo-by-oligo library structure.
- The exact, machine-readable definition seqforge uses lives in this entry's `spec.yaml`, and every
  number above is pinned there beside the value it justifies.
