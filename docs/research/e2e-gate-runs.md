# What the end-to-end gate runs actually measured

Measured **2026-07-15** on three fixtures — sacCer3, ce11 + WS298, hg38 — moved out of
the eval-corpus reference page on **2026-08-05**. The hg38 cost sweep was **re-measured 2026-08-11**
and its section supersedes what was here. The gate's design is `params_gate` in `compose/params.py`;
the numbers and the boxes they were taken on live here.

**Method.** `seqforge kb e2e` (sacCer3, 2 000 reads, 120 genes, 8 cells), `kb e2e-introns` (ce11 with
WS298 annotation) and `kb e2e-cost` (hg38) — each drives reads simulated from a real transcriptome
with injected barcodes and UMIs through the *whole* compiler and then through STARsolo with the
composed params, asserting the resulting matrix against the injected truth.
`kb e2e-cost`, or `kb e2e-introns --quantify`, is the instrument that reports wall time and peak RSS.
Every genome index came from `liulab-genome`; none of these runs is hermetic and none runs in CI.

**The launcher trap, and why it is now closed.** bioconda installs `bin/STAR` as a bash SIMD-dispatch
script that runs `STAR-avx2` as a *child*, so an instrument timing only its own child times the
script. The 2026-08-11 sweep passed `--star .../bin/STAR-avx2` explicitly to get past it. #372 has
since made the instrument measure the whole process tree and record `star_peak_rss_process`, the name
of whichever process the peak belongs to — so naming the wrapper is fine again, and the per-node SIMD
choice goes back to being automatic. **A reading whose `star_peak_rss_process` is `bash` is a
measurement of the launcher — discard it.**

**What they could not establish.** Anything about SPLiT-seq, whose strand question a simulation
cannot settle at all (simulating the reads requires assuming the strand). Anything about a chemistry
other than the one each run resolved — these fixtures certify one chemistry's strand each. And
**what a sort costs on a real library**: the cost fixture draws from 2 000 gene models, and its sort
requirement comes out ~33x smaller than the one real-data measurement on record. See the caveat
under `kb e2e-cost`.

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
(ADR-0011). Velocyto is unconditional, a
maintainer decision of 2026-07-15 rather than a measurement.

## `kb e2e-cost` — hg38, peak memory at corpus scale

Re-measured **2026-08-11** on arc, and the answer is simpler than the figures it replaces:
**peak RSS is the genome index, and it does not move with read depth.**

| reads | peak RSS | STAR wall | STAR's "max memory needed for sorting" |
| ---: | ---: | ---: | ---: |
| 2 M | 31.078 GB | 26.5 s | 9.8 MB |
| 8 M | 31.119 GB | 51.1 s | 39.3 MB |
| 32 M | 31.124 GB | 122.6 s | 157 MB |
| 64 M | 31.089 GB | 220.4 s | 312 MB |
| 100 M | 31.082 GB | 325.2 s | 493 MB |
| 250 M | 31.175 GB | 772.2 s | 1.23 GB |

hg38 + `gencode_v50`, STAR 2.7.11b, 8 threads (`ResourceHints.threads`, what the rule actually
requests), the shipped `--outSAMtype BAM SortedByCoordinate`, and the module's own argv via
`workflows/starsolo_args.py` — `--limitBAMsortRAM` among it, at the 36 GB a default 48 GB recipe
gives it. Counting is the compiler's own all-five default, `Velocyto` among them, which is both what
the withdrawn figures priced and the rule they claimed was the thing growing with depth.
**97 MB of spread across a 125x read increase**, which is to say none: the 31 GB is the
index, paid before a read is parsed, and everything the depth adds is noise against it. Wall clock is
the thing that scales — linear, ~21 s + 3 s per million reads.

**The sort is not close to being the constraint.** STAR's own reported requirement is linear at
~4.9 B/read and reaches 1.23 GB at 250 M — against a 36 GB cap, 29x headroom. `--limitBAMsortRAM`
is a cap and not an allocation (`workflows/memory.py`), so that headroom costs nothing.

> **The figures this replaces — 34.7 GB at 100 M and 44.1 GB at 250 M — are withdrawn, along with
> the knee between them.** There is no knee; the curve is flat from 2 M to 250 M.
>
> They were taken on 2026-07-15, which dates them to `os.wait4`'s `ru_maxrss` — the path
> `_run_measured` replaced the next day, and whose defect `DEFAULT_SOLO_FEATURES` in
> `manifest/policy.py` records as measured: on Linux it reports `max(parent_rss_at_fork,
> child_peak)`, a floor at whatever the caller weighs.
> They also predate the argv unification, so they priced `--outSAMtype None` at the CLI's
> then-default 16 threads rather than the shipped sorted BAM at the rule's 8.
>
> **They over-state, where the prediction was that they would under-state.** #370 reasoned that the
> unified argv does strictly more work, so the old numbers had to be floors. They are ceilings —
> 44.1 GB against 31.2 GB at 250 M, 41 % above. `--limitBAMsortRAM` is a *cap*, and supplying one
> lowered the peak instead of raising it. A sizing figure derived from them was never unsafe, only
> wasteful.
>
> **Which of those produced the difference is not established, and is not worth establishing** — the
> apparatus that produced them is gone, all six depths measured under its replacement are flat, and no
> sizing decision now rests on the answer. Do not reopen this to reconcile the old numbers with the
> new ones; they were produced by different code against different flags.

> **A reading taken through `bin/STAR` between 2026-07-16 and 2026-08-11 is not one of these, and is
> not a number.**
> The instrument polled the peak of the process it spawned, and the STAR that `liulab-runtime`'s
> `align-rna` env puts on PATH is a bash script that greps `/proc/cpuinfo` and runs `STAR-avx2` one
> level down — so the reading was the wrapper's. Measured on arc 2026-08-11, same reads and same
> params: **0.003 GB** through `bin/STAR` against **31.126 GB** through `bin/STAR-avx2`, at every
> depth, exit 0. Neither curve on this page is such a reading — the table above went through
> `bin/STAR-avx2`, and the withdrawn figures predate the window (2026-07-15, through an older
> `wait4` path that saw descendants). Anything measured *inside* it is the launcher's high-water
> mark and belongs in no curve. The instrument reads the process TREE now and names the process each
> peak belongs to, and its version is in the sweep's resume key so those points cannot resume into a
> new run (#372).

**Caveat, and it is the reason the fixture cannot size a sort.** This simulation draws from 2 000
gene models: STAR asks ~4.9 B per read here against the ~160 B per alignment record measured on real
data (GSE208154/SAMN29720279, recorded in `workflows/memory.py`) — ~33x apart, and not even the same
denominator, which is the second reason this table cannot size a sort. Take the **index intercept** from
this table — an index is an index — and take **sort sizing from the real-data figure**, which is what
`ResourceHints.mem_gb`'s 48 GB default and its 3/4 cap were chosen against and remain chosen against.

The ce11 fixture cannot answer any of this — peak RSS moved only 2.804 to 2.809 GB across a 500x read
increase, because 2.8 GB *is* the ce11 index. The absolute figure needed the real hg38 index.

A resource request is *intent*, so the memory hint lives on the **recipe**, not on a workflow module.
**The 128 GB provisioning note above 250 M reads is retired**: it was extrapolation from the
withdrawn slope, and there is no slope to extrapolate. The recipe default — 48 GB, escalating to 96
and 144 over two retries — stands unchanged, sized by the real-data sort figure rather than by this
table, and it holds a 31 GB index with room for the sort at any depth measured here.
