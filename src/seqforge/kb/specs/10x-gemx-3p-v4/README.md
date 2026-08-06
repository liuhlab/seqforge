# 10x Chromium GEM-X Single Cell 3′ Gene Expression v4 (`10x-gemx-3p-v4`)

The fourth-generation 10x 3′ gene-expression kit. Structurally it **is** v3 — same reads, same offsets,
same `backend.params` — and the only chemistry difference is which barcode whitelist the beads carry.

## Why it needs its own spec

`10x-3p-gex-v3` and `10x-3p-gex-v3.1` have both declared this id as a `processing_divergent` confusable
since they were written, with **no spec directory to resolve to**. That is the same shape of gap the
Multiome GEX arm had before its spec existed, and it fails the same quiet way: a real GEM-X library
misses the v3 whitelist, matches no positive target, and either mis-resolves or abstains. It also fails
*safely* — it over-asks rather than answering wrongly — which is exactly why nothing was red.

## "GEM-X" on its own is not evidence for this entry

GEM-X is a **platform generation**, not a kit: it spans 3′ v4, 5′ v3, Flex and OCM, and three of those
four are a different entry here or no entry at all. So an alias, or a harvested claim from a paper, has
to name **3′ and v4** before it means this spec — which is why every alias above carries both.

The near-miss to watch for is **"Next GEM"**, which is the *predecessor* generation (3′ v3.1, 5′ v2).
A bare search for "GEM" therefore pulls in the generation this kit replaced, and reads as
corroboration while pointing at the wrong entry.

## What differs from `10x-3p-gex-v3`

| | v3 | GEM-X v4 |
| --- | --- | --- |
| Geometry (R1) | 16 CB + 12 UMI (28 bp) | **identical** |
| Whitelist | `3M-february-2018` (6 794 880) | **`3M-3pgex-may-2023`** (7 372 800) |
| `soloCBwhitelist` | `{onlist:cb_whitelist}` → 3M-feb-2018 | `{onlist:cb_whitelist}` → **3M-3pgex** |
| Everything else in `backend.params` | — | **identical** |

Because the two differ *only* in `soloCBwhitelist`, `backend_identical` is correctly **false** and the
edge is `processing_divergent`, `distinguishable_by: [onlist]`.

## The separation is measured, not assumed

`distinguishable_by: [onlist]` is a claim about bytes, so it was checked rather than asserted. The two
lists share **68 254** barcodes:

- a v4 library hits the v3 list at most **0.9 %**
- a v3 library hits the v4 list at most **1.0 %**

Both are an order of magnitude under the `0.6` threshold each anti-gate uses, so rung 3 separates them
decisively rather than marginally. Three chemistries now occupy the 28 bp / 16+12 geometry — v3/v3.1,
Multiome GEX, and v4 — and all three are told apart pairwise by which of three whitelists hits.

## Provenance

The whitelist is pinned by URL and checksum, as every KB value is: `3M-3pgex-may-2023.txt.gz`, packed
from `scg_lib_structs/data/10X-Genomics/`, the same mirror the other 10x lists came from. The filename was
taken from two independent sources rather than recalled — note that **Cell Ranger v9 renamed it**
`3M-3pgex-may-2023_TRU.txt.gz` to distinguish capture strategies, and only the pre-rename name is
published.

`assay_ontology` is `EFO:0022604` ("10x 3′ v4"), looked up against the live EBI OLS rather than
recalled. It is **not** `EFO:0009922` (v3): filing a kit under its predecessor's term files the spec
under a protocol it does not model, and no test can see that it happened.

## Counting is not chemistry

As everywhere else, `soloFeatures` is absent. It says what to *count*, and counting belongs to the
processing manifest where a user may instruct it.
