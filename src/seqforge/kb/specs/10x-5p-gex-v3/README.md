# 10x Chromium GEM-X Single Cell 5′ Gene Expression v3 (`10x-5p-gex-v3`)

The third-generation 5′ gene-expression kit. Structurally it is the same 28 bp / 16 CB + 12 UMI layout
that four other entries already occupy — 3′ v3, 3′ v3.1, GEM-X 3′ v4 and Multiome GEX — so nothing
structural separates it from any of them. What separates it is **its own whitelist**, and unlike its
26 bp sibling it has one.

## How it's read

| read | length | what it holds |
| --- | --- | --- |
| **R1** | 28 bp | a 16 bp **cell barcode** + a 12 bp **UMI** |
| **R2** | 90 cycles recommended (**CG000733 p.86**), open-ended here | the **cDNA**, antisense to the mRNA |

Cell barcodes are drawn from **`3M-5pgex-jan-2023`** (3 686 400 × 16 bp).

## Why "5′ is only decidable by metadata" is a claim about a *version pairing*

`10x-3p-gex-v3` and `10x-3p-gex-v3.1` used to declare a confusable against the bare id `10x-5p-gex`
with `distinguishable_by: [metadata, alignment]`. That was wrong twice over: the geometry-coincident
partner of a 28 bp 3′ entry is **5′ v3**, not 5′ v1/v2 (which is 26 bp), and 5′ v3 does **not** share a
whitelist with anything in the 3′ cohort.

The separation is measured by set intersection over the packed barcode arrays, as the fraction of the
smaller list that also appears in the larger:

| against | list | shared |
| --- | --- | --- |
| 3′ v3 / v3.1 | `3M-february-2018` | **0.62 %** |
| GEM-X 3′ v4 | `3M-3pgex-may-2023` | **6.87 %** |
| Multiome GEX | `737K-arc-v1` | **0.00 %** |

All three sit an order of magnitude or more under the `0.6` threshold every hit-rate gate uses, so
rung 3 separates this entry from the whole 28 bp cohort **decisively**, not marginally. Hence
`distinguishable_by: [onlist]` on all four edges — and the read-undecidable note moved to where it is
actually true, the [26 bp pair](../10x-5p-gex-v2/README.md).

The entry carries one `excludes` anti-gate per foreign occupant of the geometry (the v3 list, the GEM-X
3′ list, the ARC list). `10x-3p-gex-v3` and `v3.1` gained the mirror image, an anti-gate on
`3M-5pgex-jan-2023` — a genuine 3′ v3 library hits that list at 0.62 %, so the gate can only fire on
data that really is 5′.

## What differs from `10x-3p-gex-v3`

| | 3′ v3 | 5′ v3 |
| --- | --- | --- |
| Geometry (R1) | 16 CB + 12 UMI (28 bp) | **identical** |
| Whitelist | `3M-february-2018` | **`3M-5pgex-jan-2023`** |
| `soloStrand` | `Forward` | **`Reverse`** |
| `clipAdapterType` | `CellRanger4` | **`Hamming`**, plus a `read_through` |
| Everything else in `backend.params` | — | **identical** |

Only the whitelist is observable, and it is enough to decide. The trimmer follows from the kit rather
than from the data — see below.

## Why the strand is `Reverse`

Derived from the kit's own oligos — the full derivation, its known-answer control, and the
`SC5P-PE`/`SC5P-R1` caveat live in the sibling entry's
[README](../10x-5p-gex-v2/README.md#why-the-strand-is-reverse-derived-rather-than-recalled). In one
line: the 5′ gel-bead primer ends in the template-switch tail `rGrGrG` rather than poly(dT)
(**CG000733 p.99**), so the barcode-bearing top strand is sense and the Read 2 primer annealing to it
emits the complement — R2 is antisense to the mRNA.

## The read-through, and why it is chemistry

The same primer answers a second question. Its template-switch tail sits between the UMI and the
insert, so any fragment shorter than R2 runs off the end of its own cDNA into **the reverse complement
of that TSO** — the first fixed sequence a short 5′ fragment's R2 can reach. The entry declares it as
`read_through`; the sequence is never restated beside the primer it comes from, and the tests assert
the relationship instead.

Measured at the read level on bounded head-reads, never a whole downloaded FASTQ: **10.41 %** of 20 000
reads of a library of this chemistry (SRR36092078) carry the anchor, against **0.0000 %** across five
10x 3′ libraries. It is a read-through and not biology — offsets spread continuously rather than
sitting at one position, and in 89.8 % of testable reads the tail behind the anchor reverse-complements
to that read's own cell barcode, followed by `AGATCGGAAGAGC`. So past the match lies UMI, barcode and
flowcell adapter: terminal, the whole tail goes.

**Prevalence is a property of a library, not of a kit**, which is why the 26 bp
[sibling](../10x-5p-gex-v2/README.md#the-read-through-and-why-it-is-chemistry) declares the same value
even though its measured libraries ran 0.094 %–1.86 %. The two kits carry the same TSO and handle the
cDNA read identically, so declaring it only where it happened to be common would file a library
property as a chemistry one. Where the anchor is rare the clip is a no-op, and a no-op costs nothing.

This **exceeds Cell Ranger**, deliberately: 10x runs no trimmer at all on a 5′ kit, so it does not clip
this either. `clipAdapterType: Hamming` is what makes the clip renderable — `CellRanger4` refuses a
three-prime adapter outright.

## Counting is not chemistry

As everywhere else, `soloFeatures` is absent. It says what to *count*, and counting belongs to the
processing manifest where a user may instruct it.

## Provenance

`3M-5pgex-jan-2023` is pinned by URL and sha256 in the onlist registry, packed from
`scg_lib_structs/data/10X-Genomics/` — the same mirror every other 10x list came from.
`assay_ontology` is `EFO:0022605` ("10x 5′ v3"), looked up against the live EBI OLS rather than
recalled. It is **not** a 3′ term and not 5′ v1/v2's: filing a kit under a neighbouring term files the
spec under a protocol it does not model, and no test can see that it happened.
