# 10x Chromium Single Cell Multiome ATAC + Gene Expression — GEX arm (`10x-multiome-gex`)

The **gene-expression arm** of the 10x Multiome (ARC) kit. A 10x Multiome experiment produces **two
libraries per sample** — a GEX (3′ snRNA) library and an ATAC library — under **separate SRX/SRR**. This
spec is the GEX one; the ATAC arm is [`10x-multiome-atac`](../10x-multiome-atac/README.md).

## Why it needs its own spec

The GEX read geometry is **byte-identical to 3′ v3**: R1 = 16 bp cell barcode + 12 bp UMI (28 bp), R2 =
cDNA, `soloStrand Forward`. The **only** chemistry difference is the whitelist — the Multiome kit ships
the **`737K-arc-v1`** barcode list, not the standard `3M-february-2018`. So Multiome GEX and plain 3′ v3
are *processing-divergent*: they share every structural rung and are told apart only at **rung 3** by
which whitelist the barcodes hit.

Before this spec existed, `10x-multiome-gex` was named as a `processing_divergent` confusable in the v3
spec with **no spec directory** to resolve to, and v3's ARC-whitelist anti-gate excluded ARC data from
plain v3 with **no positive target** — so a real Multiome GEX library mis-resolved or abstained. This
spec closes that: it is a child of the `10x-3p-gex` family, declares a **positive** `onlist_hit_rate` on
`737K-arc-v1`, and excludes `3M-february-2018` (the mirror of v3's exclude).

## What differs from `10x-3p-gex-v3`

| | v3 | Multiome GEX |
| --- | --- | --- |
| Geometry (R1) | 16 CB + 12 UMI (28 bp) | **identical** |
| Whitelist | `3M-february-2018` | **`737K-arc-v1`** |
| `soloCBwhitelist` | `{onlist:cb_whitelist}` → 3M | `{onlist:cb_whitelist}` → **ARC** |
| Everything else in `backend.params` | — | **identical** |

Because the two differ *only* in `soloCBwhitelist`, `backend_identical` (design §2.4) is correctly
**false**, and the `confusable_with` edge is `processing_divergent`, `distinguishable_by: [onlist]`.

## Counting is not chemistry

Multiome GEX is single-nucleus, so **GeneFull** is the sensible primary feature — but that is a
**sample-prep** fact the processing policy decides (`prep_type: single-nucleus → GeneFull`), not a
chemistry one. The reads are byte-identical to a single-cell run, so nothing about counting lives in this
spec. Filing `soloFeatures` as chemistry once cost a measured 40.7 % of a nuclear library.
