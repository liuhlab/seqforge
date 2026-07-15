# Bulk Illumina paired-end RNA-seq (no cell barcode)

Prose context for the harvester (the machine-checkable truth is `spec.yaml`).

## How the assay works
Standard bulk RNA-seq: two paired-end cDNA reads (R1 forward, R2 reverse mate), **no cell barcode and
no UMI**. This is the no-barcode branch — the resolver recognizes it by the *absence* of a
barcode-shaped technical read plus two diverse (high-distinct-ratio) cDNA mates. Reads are mapped with
plain STAR (`quantMode GeneCounts`), not STARsolo.

## How to tell it apart from single-cell
- A single-cell barcode read is short (26 bp v2 / 28 bp v3) with a low-diversity 16 bp prefix (the
  cell barcode recurs). Bulk mates are longer (>= 40 bp here; typically 75–150 bp) and near-unique
  from the first base. The `min_len >= 40` gate alone keeps a 26/28 bp barcode read out of a bulk role.
- Bulk carries no onlist, so no rung-3 whitelist check applies.

## Grouping
Run/lane come from the Illumina read-name grammar (`instrument:run:flowcell:lane:tile:...`), which the
probe parses into `ReadNameGrammar`; a dataset's files group into units by (run, lane). SRA-normalized
headers strip this, so `header_index` abstains — grouping then falls back to accession/sample metadata.

## Coverage caveat
Single-end bulk and stranded-protocol inference are **not** modeled here yet; this entry is the
paired-end, strand-at-compose slice.
