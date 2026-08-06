# What the end-to-end gate runs actually measured

Measured **2026-07-15** on three fixtures — sacCer3, ce11 + WS298, hg38 — moved out of
`docs/agents/eval-corpus.md` on **2026-08-05**. The compose gate's design lives there; the numbers
and the boxes they were taken on live here.

**Method.** `seqforge kb e2e` (sacCer3, 2 000 reads, 120 genes, 8 cells), `kb e2e-introns` (ce11 with
WS298 annotation) and `kb e2e-cost` (hg38) — each drives reads simulated from a real transcriptome
with injected barcodes and UMIs through the *whole* compiler and then through STARsolo with the
composed params, asserting the resulting matrix against the injected truth.
`kb e2e-cost`, or `kb e2e-introns --quantify`, is the instrument that reports wall time and peak RSS.
Every genome index came from `liulab-genome`; none of these runs is hermetic and none runs in CI.

**What they could not establish.** Anything above 250 M reads: the flat regime ends somewhere between
100 M and 250 M and there is exactly one post-knee point, so a deep human library is extrapolation.
Anything about SPLiT-seq, whose strand question a simulation cannot settle at all (simulating the
reads requires assuming the strand). And anything about a chemistry other than the one each run
resolved — these fixtures certify one chemistry's strand each.

## `kb e2e` — sacCer3

Resolve decided `10x-3p-gex-v3` **unaided** — no metadata hint, chemistry from the bytes alone — and
the matrix recovered with **0 spurious, 0 inflated and 0.7 % unexplained**, the remainder being
STAR's own multimapper loss. The inverted strand **collapsed 2 000 counts to 49**, which is the proof
that the gate can catch an inversion rather than merely claiming to.

## `kb e2e-introns` — ce11 + WS298

Closed the intron-rich fixture. One STARsolo run with two counting features — identical alignment,
only the counting rule differing — counted `Gene` as the exonic truth alone (recovery 0.979) and
`GeneFull` as exon plus intron (0.97), again 0 spurious and 0 inflated, resolve again deciding the
chemistry from the bytes unaided.

**That run priced a real defect, and the defect is fixed.** Gene-only counting silently discarded
**40.7 %** of a nuclear library, and the compiler *would* have emitted exactly that. The fix was not
an exit-4 question but the parse-versus-count split plus an all-five feature default — one alignment,
five counting rules, one pass — so the fixture that priced the defect is now the gate that prevents
it: with its override deleted it asserts the composed feature set against the compiler's own params
([ADR-0012](../adr/0012-produce-every-answer-rather-than-ask.md)). Velocyto is unconditional, a
maintainer decision of 2026-07-15 rather than a measurement.

## `kb e2e-cost` — hg38, peak memory at corpus scale

**34.7 GB at 100 M reads and 44.1 GB at 250 M**, so the flat regime ends between them and peak RSS is
roughly a genome-sized intercept plus a slope in reads. The ce11 fixture cannot answer this — peak RSS
moved only 2.804 to 2.809 GB across a 500× read increase, because 2.8 GB *is* the ce11 index and the
counting is a rounding error on it, so a green ce11 number would have been worse than none. **Only the
slope generalizes off ce11**; the absolute figure needed the real hg38 index.

A resource request is *intent*, so the memory hint lives on the **recipe**, not on a workflow module.
Above 250 M reads a deep human library is provisioned 128 GB until the sweep extends; an expensive
default is not a trap here, because the recipe can override it.
