# 10x Chromium Single Cell 3' Gene Expression v2

Prose context for the harvester (the machine-checkable truth is `spec.yaml`).

## How the assay works
Droplet single-cell 3' RNA-seq, the generation before v3. **R1 = 26 bp** technical read: a **16 bp
cell barcode (CB)** from the `737K-august-2016` whitelist followed by a **10 bp UMI**. **R2 = cDNA**
(open-ended), sense to the mRNA (`soloStrand Forward`).

## How to tell it apart from v3
The **only read-visible difference is R1 length**: v2 = 16 + **10** = **26 bp**; v3 = 16 + **12** =
**28 bp**. That 2 bp is a hard `segment_length` gate — v2 and v3 are separated at rungs 0–2 by
geometry alone, before any onlist is consulted, so they are **not** confusable. The whitelists also
differ (`737K-august-2016` vs `3M-february-2018`), but length settles it first.

A dataset whose metadata claims v2 while the reads are 28 bp is a **surfaced `Conflict`** (asserted
26 bp vs observed 28 bp), not a silent pick — the library section takes the observed geometry and the
conflict routes to a human.

## Common SRA failure modes
Same as v3: `fasterq-dump` without `--include-technical` drops the barcode read
(`MISSING_TECHNICAL_READ`); SRA header normalization makes `header_index` abstain.
