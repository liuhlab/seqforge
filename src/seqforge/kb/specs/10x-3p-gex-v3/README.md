# 10x Chromium Single Cell 3' Gene Expression v3

Prose context for the harvester (the machine-checkable truth is `spec.yaml`).

## How the assay works
Droplet single-cell 3' RNA-seq. **R1 = 28 bp** technical read: a **16 bp cell barcode (CB)** from the
`3M-february-2018` whitelist followed by a **12 bp UMI**. **R2 = cDNA** (open-ended), sense to the
mRNA (`soloStrand Forward`). Index reads (I1/I2) may or may not be retained.

## Aliases seen in the wild
"10x 3' v3", "Chromium 3' v3", "SC3Pv3", "single cell 3-prime v3", "10x Genomics 3' gene expression".

## How to tell it apart from its siblings
- **v2** = 16 CB + **10** UMI = **26 bp** R1, whitelist `737K-august-2016`. Length alone separates it.
- **v3.1** is *processing-equivalent* to v3 (same geometry, same whitelist, identical STARsolo params).
  Record both; do **not** ask a question.
- **Multiome (ARC) GEX** and **GEM-X 3' v4** also produce a 28 bp / 16+12 R1 — **only the onlist**
  separates them (`737K-arc-v1` and the newer GEM-X list, respectively). Geometry narrows to a family;
  the onlist collapses it to one.
- **5'** shares CB/UMI geometry but reads antisense cDNA (`soloStrand Reverse`) — decidable by
  metadata or alignment, not by read geometry.

## Common SRA failure modes
- `fasterq-dump` without `--include-technical` **drops the 28 bp barcode read** → the only correct
  behaviour is a `MISSING_TECHNICAL_READ` Blocker (re-fetch `--include-technical`, or pull
  `sra-pub-src-*` via the SDL API), never inferring the barcode read from the `_1` filename.
- SRA normalizes the read-name header, so `header_index` abstains — never gate on it.
- A pre-trimmed upload (variable R1 length) shifts the barcode offsets → `PRETRIMMED_VARIABLE_LENGTH`.
