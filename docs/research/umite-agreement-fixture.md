# The frozen umite agreement fixture

Captured 2026-08-04 for [#264](https://github.com/liuhlab/seqforge/issues/264), under the plate-based
map ([#225](https://github.com/liuhlab/seqforge/issues/225)).

[#256](https://github.com/liuhlab/seqforge/issues/256) chose to **re-implement** umite as
`workflows/umite/` rather than call it. The accepted consequence was that the agreement evidence
becomes a one-time frozen reference, capturable only while `umite==0.1.1` is still installed. This is
that reference: every matrix umite emits, cell for cell, on the ten published `GSE207085` cells, with
the exact commands, the environment, and the input checksums that produced them.

**What is in git beside this page, and what is not** (#296). In
[`umite-agreement-fixture/`](umite-agreement-fixture/): the input checksums (`inputs.json`), the two
per-cell extraction tables every rate below is read off, the fuzzy-window log, and `SHA256SUMS` over
all fifteen files — enough to attribute every number here, and enough to verify a downloaded copy of
the rest. Out of git for three different reasons, each of them one of this repo's own rules:

- **the three scripts**, because `test_nothing_tracked_escapes_the_type_checker` makes tracked Python
  checked Python, and a frozen record of what ran on one day cannot be edited to satisfy
  `mypy --strict` without ceasing to be the record;
- **`commands.json` and `env.json`**, because they hold the *resolved* argv and the resolved genome
  root, which means concrete cluster paths — and this repo is public and carries rules and
  accessions, never a path on our filesystem. Sanitising them was the tempting fix and is the wrong
  one: it would edit the very bytes `SHA256SUMS` attests, so the record would no longer be the record;
- **the count matrices** — `counts/{production,nocorrect,combined}/counts.json`,
  `multimapper_probe/counts.json`, `counts_summary.json`, 789 KB of them — because bulk data does not
  go in git, and #264 already said where these belong: beside the fingerprint packages in
  `liuhlab/seqforge-benchmark`, behind the opt-in eval job, which is where this repo keeps large
  per-dataset inputs.

All three ship as **one tarball of this whole directory**, so a download is self-contained and
`sha256sum -c SHA256SUMS` inside it verifies all fifteen members against the list git also carries.
It is **not** a CI fixture: the port's counter is tested against a synthetic annotation and a
hand-built BAM whose every read's fate is known by construction (below), and these numbers are the
agreement evidence behind a choice, never an oracle for a unit test.

## What was run

```
fingerprint packages (10 cells)
  -> normalise read names          strip the sra-tools mate suffix
  -> umiextract (exact)            no --fuzzy_umi
  -> umiextract (fuzzy)            --fuzzy_umi --trailing_hamming_threshold 1   [production]
  -> STAR mm10 / gencode_vM23      per cell, one FASTQ pair in, one BAM out
  -> samtools sort -n
  -> umicount x3                   production / no-correction / --combine_unspliced
```

`umite` is **not** in the `align-rna` image on this filesystem — that image predates
[liulab-runtime#9](https://github.com/liuhlab/liulab-runtime/pull/9). It was installed at the pinned
version into a venv over the *container's own* Python 3.13, so HTSeq 2.1.2 / pysam 0.24.0 /
rapidfuzz 3.14.5 match PR #9's solve. Only `regex` differs (image's 2026.6.28 vs PR #9's 2026.7.19);
it is used solely by the fuzzy anchor match, which reproduced to the read (below).

## The extraction side reproduces #235 exactly

Tagged fraction per cell, measured from the **output read names** rather than umite's log (#235 §5:
the log is written from a `Pool` worker and empties silently under `spawn`/`forkserver`):

| cell | reads | tag@0 | anywhere | exact | fuzzy | gain |
|---|---|---|---|---|---|---|
| SRR19884922 | 2000 | 59.6% | 61.7% | 57.9% | 62.0% | +7.1% |
| SRR19884992 | 2000 | 65.6% | 67.2% | 63.2% | 67.2% | +6.2% |
| SRR19885397 | 2000 | 58.7% | 60.6% | 36.9% | 40.9% | +11.1% |
| SRR19885469 | 2000 | 38.4% | 40.8% | 31.4% | 34.9% | +11.1% |
| SRR19885554 | 2000 | 41.1% | 45.0% | 40.2% | 43.7% | +8.6% |
| SRR19885810 | 2000 | 46.1% | 49.1% | 33.5% | 36.1% | +7.9% |
| SRR19885842 | 901 | 55.4% | 57.9% | 51.5% | 55.3% | +7.3% |
| SRR19885954 | 2000 | 61.9% | 62.7% | 38.9% | 43.8% | +12.5% |
| SRR19886065 | 2000 | 57.2% | 58.6% | 31.6% | 35.0% | +11.1% |
| SRR19886277 | 2000 | 57.9% | 60.3% | 56.6% | 60.4% | +6.6% |
| **all** | **18901** | | | **43.7%** | **47.5%** | **+8.6%** |

Every cell matches #235's table to the tenth of a point, on an independent run from the published
packages. **+8.6%** is umite's published +5–15% fuzzy-matching claim, reproduced twice now.

## Four things the capture found that #235 did not

### 1. The normalisation is a per-record rewrite, not a header rewrite

#235 established that `umiextract` refuses these packages because sra-tools `--readids` names carry a
mate suffix (`SRR19885810.1.1` / `.2`) and `umiextract.py:163-165` requires the two name tokens to be
byte-identical. What it does not record is that these packages **repeat the full ID on the `+`
line**, and HTSeq's `FastqReader` refuses a record whose `+` line disagrees with its `@` line
(`HTSeq/__init__.py:213`). Rewriting only the header trades one refusal for another — a wrong first
attempt here, and the port's input gate must check both lines.

### 2. umite's exact extractor is unanchored — 4.3% of its UMIs come from elsewhere in the read

`umiextract`'s exact pattern is `re.compile("(anchor)[NGCAT]{8}(GGG)")` used with `re.search`
(`umiextract.py:106`), so it finds the tag **anywhere** in R1. Measured over all ten cells: of 8,266
exact hits, 7,912 (95.7%) are at offset 0 and **354 (4.3%) are not** — clustering at offsets 13, 15
and 23, where 23 is the Tn5 mosaic-end read-through #230 measured at 6.5–79.5% of R1.

This is a live divergence risk. #226 established the read structure as *fixed offset, no anchor*, and
#234's resolve-side gate is `motif_present` with `where: read_start`. A port that anchors extraction
at offset 0 — the obvious reading of "derived from the element model" (#256 decision 6) — loses 4.3%
of tagged reads relative to umite. Whichever way it goes, it has to be a decision rather than an
accident.

### 3. umite counts **unstranded**, for tagged and internal reads alike

`parse_gtf` builds both feature arrays as `HTSeq.GenomicArrayOfSets("auto", False)`
(`umicount.py:89-90`) — the second argument is `stranded`. There is no strand branch anywhere in
`umicount.py`, and no per-read-class strand handling.

So #227's finding that *"tagged and internal reads are counted under different strand rules, neither
correcting the other"* describes the published analysis, **not umite**. #264's open question — "if it
is not derivable, `parse_keys` gains exactly one key" — has a cheaper answer available: reproducing
umite means one unstranded rule and `parse_keys` stays empty, at the cost of diverging from the
paper's own analysis. This is a decision, not a lookup, and #257 needs the same answer.

### 4. `--outSAMmultNmax 1` silently zeroes umite's `_multimapping` category, inflating UMI counts +10.2%

This is the sharpest finding, and it contradicts a settled #256 module literal.

umite classifies a read pair as multimapping **purely from bundle length** — if
`filter_aligned_reads` returns more than one aligned pair, it is a multimapper
(`umicount.py:317-320`). It **never reads the `NH` tag** (`grep NH umicount.py` returns nothing).

`--outSAMmultNmax 1` (#256 decision, a module literal) makes STAR emit exactly one record per
multimapper. The bundle is then length 1, and umite classifies it `_unique` and counts it into a
gene. Across the fixture, `_multimapping` is **0 for all ten cells** while 1,640 of 12,977 distinct
aligned read names (12.6%) carry `NH > 1`.

Realigning `SRR19884922` with the flag as the only difference:

| | `_multimapping` | `_no_feature` | `_ambiguous` | UE gene total |
|---|---|---|---|---|
| without `--outSAMmultNmax 1` | **108** | 14 | 74 | 785 |
| with `--outSAMmultNmax 1` | **0** | 19 | 85 | **865 (+10.2%)** |

The inflation of the primary UMI matrix is **larger than umite's entire published fuzzy-matching
gain**. The port must either read `NH` directly — which reproduces umite's *intent* rather than its
mechanism, and is robust to the aligner flag — or the module literal has to go.

## What the fixture cannot discriminate

`U(--combine_unspliced)` equals `UE + UI` **exactly**, for all ten cells (4,377 + 550 = 4,927 both
ways). umite deduplicates UMIs *within* each of the E and I buckets separately
(`umicount.py:452-454`), so the two differ only when one UMI is seen both exonically and intronically
on the same gene — which needs more depth than a 2,000-read slice.

So this reference pins the `inex ≠ exon + intron` arithmetic's **shape** (dedup over the union vs
per-bucket) but not its **value**: at this depth both implementations agree. A port that gets it
backwards passes this fixture. That gap needs a synthetic case, not more real cells.

## The `UB` tag survives STAR — mechanism confirmed

#256 decision 9 settled that the UMI rides in a `UB` BAM tag rather than the read name, which is what
keeps `workflows/cram.py` reusable unchanged (cram.py rewrites every read name to `r<N>` and would
destroy a name-carried UMI). It did not say how the tag gets there, and STAR refuses `UB` in
`--outSAMattributes` outside STARsolo:

```
EXITING because of FATAL INPUT ERROR: --outSAMattributes contains CR/CY/UR/UY/CB/UB tags
which are not allowed for --soloType None
```

The route that works, run and verified here: the extractor emits an **unaligned BAM** carrying
`UB:Z:<umi>`, and STAR reads it with `--readFilesType SAM PE --readFilesCommand samtools view
--readFilesSAMattrKeep All`. 452 of 716 aligned records came out carrying their input `UB` — and
`RG` too — without `UB` ever being named in `--outSAMattributes`. This makes the extractor's output
format a real design choice (uBAM vs FASTQ-plus-a-tagging-step) rather than an open question.

## Measured cost

| step | wall clock | peak RSS |
|---|---|---|
| STAR, per cell (25 GB index, 16 threads) | **15 s** | **27.7 GB** |
| GTF parse, gencode vM23 → pickle | 50 s | 720 MB |
| `umicount`, 10 cells, 10 workers | 46 s | 904 MB |

The **27.7 GB** is the number #225's fog listed as unmeasured ("the RAM our mm10 index needs in
`align-rna`"), and it is what sets the per-cell request under #256's per-rule arithmetic. It is
independent of read count: the smallest and largest cell load the same index. The GTF pickle is
47.5 MB (#235 estimated 53 MB), and it is copied to every `umicount` worker.

## One more trap for the port

A read aligned to a scaffold that carries **no GTF feature at all** (`chrUn_GL456382`,
`chr4_GL456350_random`, …) is caught by a `KeyError` in `find_overlap` and reclassified
**`_unmapped`**, not `_no_feature` (`umicount.py:186-192`), with a warning per read. On this fixture
that is a handful of reads per cell, but the classification is wrong in a way that would be invisible
in a summary.

## What is *not* here, and why

**No GTF, no BAMs, no gene list.** mm10 and gencode vM23 are how this measurement was *made*, not
something seqforge should carry: seqforge compiles any species and any annotation, and a checked-in
gencode slice would bake one of each into the package. The annotation is named here as provenance and
resolved through `liulab-genome` at run time (R7, R10) — it is never a path and never a file in this
repo.

That is also why the count records are **sparse**: `counts/*/counts.json` holds only the non-zero
cells. The dense TSVs umite writes are 55,335 gencode gene ids wide, and shipping them would be
shipping the annotation. The counts are the measurement; the gene list is not.

**So the port's counter is tested against a synthetic annotation, not against these cells.** A
real-genome slice would only prove "agrees with umite on this data"; a GTF and BAM built by
construction, with every read's fate known in advance, proves the counting *rule* — and it is
species-agnostic. That is what #235 used to confirm the output shape, and what umite's own 47 tests
use. The numbers below are the agreement evidence for the choice #256 made; they are not a unit-test
fixture.

If a real-data agreement check is ever wanted in CI, its inputs belong beside the fingerprint
packages in `liuhlab/seqforge-benchmark`, behind the opt-in eval job (`io/benchmark.py`,
`eval-corpus.md`) — the place this repo already keeps large per-dataset inputs. That is where the
matrices go, and the header of this page says which files those are.

## Decisions taken on this evidence (2026-08-04)

| question | decision |
|---|---|
| multimappers | **The port reads `NH` directly** rather than inferring from bundle length. Reproduces umite's intent, immune to the aligner flag, and `--outSAMmultNmax 1` can stay. `multimapper_probe/` — the same cell realigned without the flag — is the reference for this column, not the production matrices. |
| extraction anchoring | **Bounded search window, 46 bp** (see below). |
| strand | **Unstranded, one rule for tagged and internal alike** — reproduce umite. `parse_keys` stays empty, as #256 decision 6 predicted. The divergence from #227's published two-rule analysis is recorded, not resolved. |

### Why the window is 46 bp

The window is `anchor start ≤ 24`, plus the 22 bp the match itself consumes (11 anchor + 8 UMI +
3 trailing) — so the extractor reads the first **46 bp** of R1 and no further.

The bound is mechanistic, not fitted: the longest prefix that can precede the tag is Tn5 mosaic-end
read-through, and **no exact hit anywhere in 18,901 reads starts past offset 24**. Measured:

- **Exact mode loses nothing.** 8,266 hits whole-read, 8,266 in the window.
- **Fuzzy mode loses 113 of 8,976 tagged reads (−1.26%)** — verified through umite's own
  `--search_region 46` flag, per-cell in `extract_window46.tsv`.

Those 113 are a purity gain, not a loss. The `{e<=2}` fuzzy anchor matches spurious 11-mers deep in
the read — as far as **offset 133** — at offsets the fixed-offset chemistry cannot produce, which is
the same intrinsic structured background #234 measured when it rejected an off-offset-tolerant
evaluator at purity 0.539.

## Reproducing

`01_normalise_and_extract.py`, `02_align_and_count.sh` and `04_ub_tag_probe.py` are the exact
scripts, and `commands.json` holds the **resolved argv of every invocation** — all four travel in the
tarball described at the top of this page, not in git. Inputs are the ten
`packages/GSE207085/*.fingerprint.tar.gz` from `liuhlab/seqforge-benchmark`, with per-file SHA-256 in
`inputs.json`, which **is** in git — so what a re-run has to reproduce is pinned here even though
what produced it is not. The genome is `mm10` / `gencode_vM23`, resolved through `liulab-genome`; the
tool versions and container digest are recorded in `env.json` as provenance, and nothing about the
genome is shipped.
