# 10x Chromium Single Cell 5′ Gene Expression v1 / v2 (`10x-5p-gex-v2`)

Droplet single-cell RNA-seq read from the **5′ end** of the transcript. Same instrument, same gel
beads-in-emulsion, same cell barcode + UMI counting model as the 3′ kits — and the reads come off the
sequencer looking **exactly** like 3′ v2's. What differs is invisible to any probe: which end of the
mRNA the cDNA read covers, and therefore which strand it is.

One entry covers **both v1 and v2**, because they are byte-identical in everything this KB can express:
26 bp R1, open-ended cDNA R2, the `737K-august-2016` whitelist, the same STARsolo params. Two entries
would be `processing_equivalent` twins — recorded together and never chosen between — i.e. the same
answer in two files. Both ontology terms ride here: `EFO:0011025` (v1) and `EFO:0009900` (v2), each
checked against the live EBI OLS.

## How it's read

| read | length | what it holds |
| --- | --- | --- |
| **R1** | 26 bp | a 16 bp **cell barcode** + a 10 bp **UMI** |
| **R2** | open-ended | the **cDNA**, starting at the transcript's 5′ end — **antisense** to the mRNA |

## Why the strand is `Reverse`, derived rather than recalled

Strand is the value where being wrong is quietest: STARsolo exits 0 and emits a matrix that merely
looks thin. So it is derived from the kit's own oligos, and the derivation is reproducible from the
cited pages rather than from anyone's memory:

1. **In 5′ the barcode is on the TSO, not the RT primer.** The gel-bead primer is
   `5'-CTACACGACGCTCTTCCGATCT-[16 nt CB]-[UMI]-TTTCTTATATrGrGrG-3'`
   (10x **CG000109 Rev D p.5** for v1/v2; **CG000733 p.99** for v3). The `rGrGrG` tail makes it a
   template-switch oligo. Contrast 3′, where the same bead primer ends in poly(dT) and **is** the RT
   primer (**CG000731 p.80**).
2. **RT is primed by a separate, unbarcoded poly-dT oligo**, so first-strand cDNA is antisense and
   carries no barcode. The barcode is appended at the first strand's 3′ end by template switching at
   the transcript's 5′ end (**CG000109 p.1**).
3. So the barcode-bearing (top) strand is **sense**: `[R1 handle][CB][UMI][TSO][insert]…`, the insert
   beginning at the transcript's 5′ end. Exactly inverted from 3′.
4. The Read 2 primer anneals to the top strand, so **R2 emits its complement** — R2 is antisense to the
   mRNA. STARsolo defines `Forward` as "read strand same as the original RNA molecule", so this is
   **`Reverse`**.

**Known-answer control:** the identical derivation run on the 3′ appendix reproduces `Forward`, which
every 3′ entry in this KB already carries.

**Corroboration:** 10x's own Cell Ranger `chemistry_defs.json` declares `SC5P-R2`, `SC5P-R2-v3`,
`SC5PHT` and `SC5P-R2-OCM*` with `strandedness: "-"` and `rna_read: R2`; scg_lib_structs runs
`--soloStrand Reverse` on real 5′ data (ERR4667456); and a published both-ways run
(AlexsLemonade/alsf-scpca PR #137) measured **0.5–0.6** gene-expression correlation under the wrong
orientation versus **>0.98** under the right one.

**The one caveat, recorded so it is not rediscovered as a bug.** `Forward` is correct for the
**`SC5P-PE` / `SC5P-R1`** configurations, where R1 is mapped as the RNA read. seqforge cannot express
those: the STARsolo parse namespace allows neither `soloBarcodeMate` nor `clip5pNbases`. So `Reverse`
holds *unconditionally* for anything this KB can model.

## The read-through, and why it is chemistry

The same derivation answers a second question. Step 3 above puts the barcoded top strand at
`[R1 handle][CB][UMI][TSO][insert]`, so a fragment shorter than R2 runs off the end of its own cDNA and
into **the reverse complement of that TSO** — the first fixed sequence a short 5′ fragment's R2 can
reach. The entry declares it as `read_through`. The sequence is deliberately *not* restated beside the
primer it comes from: the relationship is the claim, and the tests assert it.

Established at the read level rather than inferred from the construct, on bounded head-reads with no
whole FASTQ downloaded:

| library | reads | carrying the anchor |
| --- | --- | --- |
| 5′ v2 (PRJNA1415162, 4 runs) | 179 712 | 0.094 % |
| 5′ v3 (SRR36092078) | 20 000 | **10.41 %** |
| 3′ negative control (5 libraries, 3 mouse) | 500 909 | **0.0000 %** |

It is a read-through and not biology: match offsets spread **continuously** from 0 to `read_len − 13`
rather than sitting at one position, and in 89.8 % of testable 5′ v3 reads the tail behind the anchor
reverse-complements to that same read's *own* cell barcode, followed by `AGATCGGAAGAGC`. Past the match
lies UMI, barcode and flowcell primer — terminal, so the whole tail goes and no recipe could rationally
keep it. Left in place it is a **denominator rather than a mapping failure**: STAR's length-relative
filters are computed over the read the adapter is still part of.

**Prevalence is a property of a library, not of a chemistry**, which is why both 5′ entries declare
this and not only the one that measured 10 %. Three further 5′ libraries measured 0.30 %, 0.89 % and
1.86 %, tracking insert size and read length; v2 and v3 share the same TSO and byte-identical cDNA
handling, so the hundred-fold gap sits between those libraries and not between those kits. Where the
anchor is rare the clip is a no-op, and a no-op costs nothing.

**This exceeds Cell Ranger, deliberately.** 10x runs no trimmer at all on a 5′ kit — hard-disabled in
its source (`trim_polya_min_score: None, trim_tso_min_score: None` under `WhichEnd::FivePrime`,
identical at 9.0.1 and 10.0.0) — so it does not clip this either. These two entries are where this KB
departs from vendor parity on a measurement rather than following it. `clipAdapterType: Hamming` is
what makes the clip renderable at all: `CellRanger4` refuses a three-prime adapter outright, and it is
also what this entry used to be handed by the mapping module, clipping a 3′ kit's TSO and poly-A off a
read whose own pipeline never touches either.

## The pair that cannot be settled from bytes

`10x-5p-gex-v2` and [`10x-3p-gex-v2`](../10x-3p-gex-v2/README.md) share the 26 bp geometry **and** the
`737K-august-2016` whitelist. Every structural rung ties, and rung 3 cannot break it because both
entries point at the same file. Their backends differ in exactly one param — `soloStrand` — which no
probe can observe.

So the edge is `processing_divergent`, `distinguishable_by: [metadata, alignment]`:

- **metadata (rung 0)** — prose reliably says "5′" or "3′" even when it is vague about the version, and
  the resolver's family-level disambiguation picks the leaf under the family the prose named;
- **alignment (rung 6)** — map a handful of reads and read the orientation off the result.

Before this entry existed, a 5′ v1/v2 library resolved silently to 3′ v2 and compiled with
`soloStrand Forward`. Nothing was red. That is exactly the failure mode this KB's confusability
machinery exists for, and it is why the entry is worth the question it now forces.

The entry's `signature` is **deliberately byte-identical** to `10x-3p-gex-v2`'s, test for test and
weight for weight, and it declares **no** `excludes`. An anti-gate is an onlist proving the data belongs
to a geometry-coincident neighbour; here the only neighbour carries the same list, so there is none to
write. Any asymmetry would hand one of the two a systematic edge and turn a genuine tie into a silent
guess wearing a decision's clothes.

## Gotchas

- **This entry models the GEX library only.** The same kit also produces V(D)J / immune-profiling
  libraries under separate SRX/SRR with a different read structure. They are not modelled, and are not
  aliased here — a spec that quietly claimed them would be unfalsifiable.
- **The barcode read can go missing on SRA.** `fasterq-dump` without `--include-technical` drops R1;
  seqforge blocks rather than guessing which file is which.
- SRA rewrites read-name headers, so seqforge never trusts them when grouping files into samples.

## References

Oligo architecture from the 10x user guides cited above. The whitelist is `737K-august-2016`, already
registered (URL + sha256) for 3′ v2. The machine-readable definition seqforge uses is this entry's
`spec.yaml`.
