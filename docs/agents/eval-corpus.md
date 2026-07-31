# The eval corpus and the ground-truth runs

Read this when you add an eval case, extend the benchmark, or change what the compose gate asserts.
One lifetime: **how we prove the compiler works, and what the proofs actually measured.** Numbers here
are dated, because a measurement without a date is a claim.

## The demo dataset

**`PRJNA1027859`** is the pilot's worked example — the dataset the tutorial is written from, and what
"it works end to end" means concretely. `ce11` (*C. elegans*, taxid 6239, WBcel235) is confirmed
available in `liulab-genome`.

Two disciplines, and only two, survive from its history:

- **Real data, and its path, stay out of git.** The on-disk root lives in local, out-of-git config; a
  local eval case names an environment variable instead, and `test_skill_never_leaks_a_lab_path`
  enforces it. Real FASTQs are far too large for git, and a lab path is not a project fact.
- **Pre-register `expected.yaml` before a run.** Only a prediction can be wrong. Its claims must be
  *checkable* against the manifest — a per-sample attribute for one accession, or the same attribute
  across all of them.

**There is no held-out dataset.** The designation was retired on 2026-07-15 and its guard and registry
were **deleted, not suspended**; the pre-registration stands in their place
([ADR-0016](../adr/0016-no-held-out-dataset.md)).

**Still open, for the maintainer:** whether pre-registering the pilot's organism sits comfortably with
"don't tune against it". The safe reading, and the one in force: `expected.yaml` uses archive-declared
metadata and a provider-independent prior only, committed before any run, and never a value read out
of the data.

## The benchmark, in two tiers

A dataset enters the corpus through a byte-light **fingerprint package** rather than its FASTQs. The
`fingerprint` recipe kind ([`evals/case.py`](../../src/seqforge/evals/case.py)) unpacks a package,
stamps each slice's pinned whole-file identity back on — so resolve reaches the same verdict, and the
same hash, that the originals would — then grades chemistry from the pinned bytes and sample
attributes from a committed `records.json`. No full FASTQ, and no API key.

A package comes from one of three sources: a path committed in the case directory, a path in the
public Hugging Face benchmark repo (`liuhlab/seqforge-benchmark`, pulled anonymously and pooch-cached
— no token, no `huggingface_hub` dependency), or a root staged out of git behind an environment
variable. **An unreachable package skips; it never fails.**

Two tiers ride that mechanism, in two directories, and they are disjoint on purpose:

| tier | directory | when it runs |
|---|---|---|
| **ci-benchmark** | `evals/cases` — synthetic per-spec recipes covering every leaf spec, plus any committed tiny fingerprint | hermetically, in `test_corpus_is_green`, on every commit |
| **benchmark** | `evals/benchmark` — real datasets whose text-only fingerprint packages live on Hugging Face | only in `benchmark.yml`, on a published release or manual dispatch |

Disjointness is the point: a package pull can never sneak into hermetic CI, so a free Hugging Face
account's rate limit can never gate a PR.

Each benchmark case commits its `records.json` — the archive's own BioSample and SRA transcript — so
sample facts grade deterministically with no NCBI key, while `library.chemistry` grades from the
pinned bytes. **Those expectations were seeded from a run** and are marked pending maintainer review:
they are a regression baseline, categorically different from the pilot's before-the-run
pre-registration. A true held-out *test* set is a later milestone; this is the validation set we
develop against.

Redistributable packages carry **extracted text only**. `preflight --redistributable` builds one from
FASTQs and `seqforge strip-fingerprint` repacks an existing package, dropping the raw paper for
copyright and its figures until the figure pipeline improves — the reads and pins are untouched, so
the dataset hash is preserved. A run falls back to the extracted text, so nothing we may not
redistribute reaches Hugging Face while the harvest input survives.

## The compose gate has three parts, because a dry run cannot catch a strand inversion

`compose` is a pure function of the (manifest, recipe) pair and touches no data on disk. Its gate is
therefore split:

1. **Wiring** — touch zero-byte files at every path in the file inventory **and** at every resolved
   onlist cache path, so a whitelist declared as a Snakemake input does not raise a spurious missing-
   input error; then dry-run and lint. This catches config, wildcard resolution and rule wiring.
2. **Params** — deterministic assertions that the emitted strand, UMI length, CB length and
   `--readFilesIn` **order** (cDNA read before barcode read, derived from the role assignment) match the
   KB's backend params. These are the semantic bugs a linter cannot see, and they must **not** be
   attributed to the dry run.
3. **A real end-to-end run** — reads simulated from a real transcriptome with injected barcodes and
   UMIs, driven through the *whole* compiler and then through STARsolo with the composed params, with
   the resulting matrix asserted against the injected truth.

**`skip` is first-class.** Parts 1 and 3 depend on a toolchain seqforge does not own — Snakemake for
one, STAR plus `liulab-genome` plus network for the other — and the count-matrix run is a Linux and
cluster operation. So each part reports pass, fail or **skip**, and part 2, which needs no toolchain,
always runs. A gate reporting `pass` because it never ran would let a green CI be mistaken for
coverage.

### The assertion is "accounted", not naively exact

Real transcripts multimap. Paralog and subtelomeric families that STARsolo legitimately drops make
`observed == injected` unachievable, and demanding it would only teach us to weaken the gate. So the
gate asserts the four things that indict **us**:

- **0 spurious pairs** — never count a read for a gene it did not come from;
- **0 inflated counts** — never invent a UMI, which is exactly what a dedup or geometry bug looks like;
- **unexplained loss at or under 2 %** — subtract STAR's own multimapper loss, read from its log; what
  remains is the compiler's error, and it must be near zero;
- **strand sensitivity** — the same reads re-run under an inverted strand must *collapse*, or the gate
  could not have caught an inversion in the first place.

## What the runs measured

**`kb e2e`** (sacCer3, 2 000 reads, 120 genes, 8 cells, measured 2026-07-15): resolve decided
`10x-3p-gex-v3` **unaided** — no metadata hint, chemistry from the bytes alone — and the matrix
recovered with **0 spurious, 0 inflated and 0.7 % unexplained**, the remainder being STAR's own
multimapper loss. The inverted strand **collapsed 2 000 counts to 49**, which is the proof that the
gate can catch an inversion rather than merely claiming to.

**`kb e2e-introns`** (ce11 + WS298) closed the intron-rich fixture. One STARsolo run with two counting
features — identical alignment, only the counting rule differing — counted `Gene` as the exonic truth
alone (recovery 0.979) and `GeneFull` as exon plus intron (0.97), again 0 spurious and 0 inflated,
resolve again deciding the chemistry from the bytes unaided.

**That run priced a real defect, and the defect is fixed.** Gene-only counting silently discarded
**40.7 %** of a nuclear library, and the compiler *would* have emitted exactly that. The fix was not
an exit-4 question but the parse-versus-count split plus an all-five feature default — one alignment,
five counting rules, one pass — so the fixture that priced the defect is now the gate that prevents
it: with its override deleted it asserts the composed feature set against the compiler's own params
([ADR-0012](../adr/0012-produce-every-answer-rather-than-ask.md)). Velocyto is unconditional, a
maintainer decision of 2026-07-15 rather than a measurement.

**`kb e2e-cost`** (hg38, 2026-07-15) measured peak memory at corpus scale: **34.7 GB at 100 M reads
and 44.1 GB at 250 M**, so the flat regime ends between them and peak RSS is roughly a genome-sized
intercept plus a slope in reads. The ce11 fixture cannot answer this — peak RSS moved only 2.804 to
2.809 GB across a 500× read increase, because 2.8 GB *is* the ce11 index and the counting is a
rounding error on it, so a green ce11 number would have been worse than none. **Only the slope
generalizes off ce11**; the absolute figure needed the real hg38 index.

The instrument is `kb e2e-cost`, or `kb e2e-introns --quantify`, reporting wall time and peak RSS.
Note that a resource request is *intent*, so the memory hint lives on the **recipe**, not on a
workflow module.

**Still open:** above 250 M reads is extrapolation from a single post-knee point, so a deep human
library is provisioned 128 GB until the sweep extends. An expensive default is not a trap here,
because the recipe can override it.

## What the runs do not yet cover

A **SPLiT-seq** end-to-end run. The existing runs certify one chemistry's strand and nothing else.

And a simulation cannot settle SPLiT-seq's open strand question on its own, which is worth
understanding before anyone tries: simulating the reads requires assuming the strand, which is
circular. That question needs the original oligo derivation or real published data. Once it is
settled, a simulation can prove only that `compose` stays faithful to whatever the KB declares — which
is a real thing to prove, but a different one.
