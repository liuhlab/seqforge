# What each STARsolo family's own pipeline does to a read, and what STAR should be told

Researched 2026-08-07 for [#355](https://github.com/liuhlab/seqforge/issues/355). Four chemistry
families bind `map/starsolo` — five 10x 3' GEX, two 10x 5' GEX, three BD Rhapsody WTA, one SPLiT-seq
— and until this work every one of them was handed `--clipAdapterType CellRanger4`, a 10x 3'
trimmer, as a module literal. This file is the primary-source review that decided what each should
get instead, plus the STAR flag matrix measured against the pinned binary. What it *decided* lives in
the entries and in `Backend`'s docstring; the evidence lives here.

Every claim carries a locator: `file:line` at a named tag, or a first-party document with revision
and page. Third-party reimplementations were used only to locate where a claim came from, never to
establish a fact.

## The answer, per family

| family | specs | what the vendor does to the cDNA read | who does it | our STAR |
|---|---|---|---|---|
| 10x 3' GEX | 5 | 30 nt TSO off R2's 5' end, poly-A off its 3' end; both min score 20, ≤10% error | Cell Ranger's own Rust, before STAR | `CellRanger4` — a faithful reimplementation of exactly that |
| 10x 5' GEX | 2 | **nothing** — trimming is hard-disabled by the kit's endedness | — | `Hamming`, a no-op, plus one measured `read_through` this KB adds |
| BD Rhapsody WTA | 3 | quality trim, handle and capture clips, R1-in-R2 overlap removal — all before the aligner; STAR gets a literal 38-base A-string | BD's `mist_run_qualclalign.py`, then STAR | `Hamming` + BD's own 38 A's |
| SPLiT-seq | 1 | its own 30 nt TSO, searched in read 1's first 20 bases and cut through | the authors' Python, before STAR | `CellRanger4` + `clip5pAdapterSeq` carrying SPLiT-seq's TSO |

## 1. STAR 2.7.11b — the flag matrix, measured

Measured 2026-08-07 against `.pixi/envs/test-star/bin/STAR` (reports `2.7.11b`), parameter
initialization only. A bogus `--genomeDir` means the run reaches genome load exactly when the flag
combination itself was accepted, so the `EXITING` line classifies it.

| combination | result |
|---|---|
| `--clipAdapterType Hamming` | accepted |
| `--clipAdapterType CellRanger4` | accepted |
| `--clipAdapterType None` | **FATAL** — *"= None is not a valid option"* |
| `Hamming` + `--clip3pAdapterSeq SEQ` | accepted |
| `Hamming` + `--clip3pAdapterSeq polyA` | accepted (the keyword: a read-length-long clip, not a literal) |
| `Hamming` + `--clip5pAdapterSeq SEQ` | **FATAL** — *"not supported yet, except for --clipAdapterType CellRanger4"* |
| `CellRanger4` + `--clip3pAdapterSeq SEQ` | **FATAL** — *"uses fixed sequences for 3' polyA adapters"* |
| `CellRanger4` + `--clip5pAdapterSeq SEQ` | accepted |
| `CellRanger4` + both | **FATAL**, on the 3' one |

Three things follow, and all three are load-bearing.

**The two trimmers are exactly complementary.** `CellRanger4` builds its own fixed 3' poly-A and
refuses to be handed another, while taking a 5' sequence in place of the TSO it hardcodes; `Hamming`
is the reverse. So the invariant a spec must satisfy is one rule — *the end a declared clip sits at
must be an end its declared trimmer takes* — and not a list of the two illegal pairs. Note STAR's own
`SOLUTION:` line on the `CellRanger4` × 3' error reads *"Do not use --clip5pAdapter\* options"* and
names the wrong family; three-prime is what is forbidden.

**`None` does not exist, and the shipped help says it does.** `source/parametersDefault:199` still
advertises *"None … no adapter clipping, all other clip\* parameters are disregarded"*, while
`source/ParametersClip_initialize.cpp:11-14` accepts only `Hamming` and `CellRanger4` and exits
fatally on anything else. The staleness runs the other way too: the `#####UnderDevelopment … not
supported - do not use` banner over `clip5pAdapterSeq` (`parametersDefault:217-226`) was accurate
when written, but the `CellRanger4` carve-out landed in 2.7.10a (`CHANGES.md:32,36`, 2022-01-14) and
the banner was never lifted. **The reusable lesson: the shipped help is stale relative to the shipped
code, in both directions. Verify a flag by running the binary.** A chemistry that wants no clipping
emits `Hamming`, which with nothing declared is a genuine no-op — `adSeq` stays `"-"` →
`ClipMate::initialize` sets `type=-1` (`ClipMate_initialize.cpp:22-23`) → `ClipMate::clip` returns
immediately (`ClipMate_clip.cpp:9-10`).

**A supplied 5' sequence REPLACES the hardcoded 10x TSO rather than adding to it.** The default is a
fill guarded by `if (in[0].adSeq[0]=="-")` (`ParametersClip_initialize.cpp:29-31`); `ClipMate` holds
one scalar `adSeq` (`ClipMate.h:14`), with no container that could hold two; and
`ClipMate_clipChunk.cpp:41` is the sole `opalAlign` call site, taking `adSeqNum` — the user's
sequence. The string `AAGCAGTGGTATCAACGCAGAGT` occurs exactly once in the whole tree, inside that
guard. Only mate 1 may carry a 5' adapter under `CellRanger4` (`:24-27`).

**Arity, and where it differs by module.** `--clip3pAdapterSeq` takes one value per mate and
`--clip3pAdapterMMp` must match its arity. Under `--soloType` STARsolo peels the barcode read off the
mate count, so a two-file solo run has *one* mate. The full per-mate table, including the non-solo
`--readFilesType SAM PE` form `map/star-umi` uses, is in
[`smartseq3-tn5-read-through.md`](smartseq3-tn5-read-through.md).

## 2. 10x 3' GEX — five chemistries, one code path

Cell Ranger builds exactly two adapters and no others
(`cellranger-10.0.0:lib/rust/cr_lib/src/aligner.rs:83-111`; every locator in this section is at
`cellranger-10.0.0`, and these files moved between releases, so a line number does not carry to
another tag):

| adapter | sequence | end | search | line |
|---|---|---|---|---|
| `tso` | `AAGCAGTGGTATCAACGCAGAGTACATGGG` (30 nt) | 5' | `Anywhere` | `aligner.rs:99`, `:108` |
| `polyA` | 150 literal `A`s | 3' | `NonInternal` (suffix-anchored) | `aligner.rs:96`, `:102-107` |

Both act on the *RNA* read, whose range is built from the chemistry's `rna` component
(`cr_types/src/rna_read.rs:484-494`), and every 3' chemistry declares
`"rna": {"read_type": "R2", "offset": 0, "length": null}` — `chemistry_defs.json`, keys `SC3Pv2`,
`SC3Pv3-*`, `SC3Pv4-*`, `ARC-v1`. So: TSO off R2's 5' end, poly-A off R2's 3' end. Both are gated at
minimum alignment score **20** (`cr_wrap/src/bin/cellranger.rs:332-333`; the `multi` pipeline
hard-codes the same pair at `mro/rna/sc_multi_cs.mro:182-183`), scored `1×matches − 2×errors`
(`fastq_set/src/adapter_trimmer.rs:48-49`) with `err_rate ≤ 0.1` (`adapter_trimmer.rs:46`). Both are
searched on the untrimmed read and the retained interval is their intersection
(`aligner.rs:204-211`); trimmed bases come back as soft clips and as the `ts:i` / `pa:i` tags.

**Trimming is switched by exactly one predicate** — `endedness() == Some(WhichEnd::FivePrime)`
disables it (`cr_lib/src/stages/align_and_count.rs`, the `WhichEnd::FivePrime` guard) — and there is
no per-chemistry trimming parameter anywhere in the tree. All five of our specs declare
`three_prime` with `rna: R2 / offset 0 / length null`, so v2, v3, v3.1, GEM-X v4 and multiome GEX
take a **byte-identical trimming path**. The one v4-specific predicate in the aligner,
`is_sc_3p_v4()` (`cr_types/src/chemistry/mod.rs:701-704`), picks a UMI-correction mode for
multiplexing feature barcodes (`aligner.rs:365-370`) and has nothing to do with trimming. `SC3Pv2`
differs only in whitelist, UMI length (10 vs 12) and recommended cycles.

**Cell Ranger passes STAR no clip flag at all.** It calls STAR in-process through `orbit`, whose
argument vector is `--genomeDir --runThreadN --readNameSeparator --outSAMunmapped --outSAMtype
--outStd --outSAMorder` (`10XGenomics/orbit@9d967ef:src/lib.rs:101-118`) plus `--outFilterScoreMin`
added later (`align_and_count.rs:465-472`). All trimming is Cell Ranger's own Rust, before STAR sees
a read. `CellRanger4` is Dobin's reimplementation of it, not something Cell Ranger invokes.

**Is the reimplementation faithful? Substantially yes.** Same strings, same ends, same score model.
The poly-A rule is *algebraically identical*: `ClipCR4::polyTail3p` accepts a window when
`score*10 >= ib*7` (`ClipCR4.cpp:93`), which for `m` matches and `e` errors is `m >= 9e`, i.e. error
rate ≤ 1/10 — `fastq_set`'s `ALLOWED_ERROR_RATE`; and the final `score1 < 20 → no clip` gate
(`:103-104`) is Cell Ranger's `trim_polya_min_score = 20` against the identical `m − 2e`. The
divergences are narrow-band. At the recommended R2 lengths (98 for v2, 90 for the rest) the two
length-gated ones cannot fire at all, leaving a tie-break, a marginal-TSO band, and an ordering:

- STAR keeps the **longest** accepted poly-A window, Cell Ranger the one with the **most matches**
  (`adapter_trimmer.rs:417-424`) — differs only on ties.
- STAR breaks the poly-A scan after the 10th mismatch (`ClipCR4.cpp:99-100`); a window Cell Ranger
  would accept with 10 errors needs ≥90 matches, hence a read ≥100 nt.
- STAR searches only a read's **first 91 bases** for the TSO (`ClipCR4.cpp:15`, `:38-79`); Cell
  Ranger searches the whole read.
- STAR's TSO accept test is a three-clause proxy for `score ≥ 20 && err ≤ 10%`:
  `S<20 || (S==20 && L>26) || (S==21 && L>30)` (`ClipMate_clipChunk.cpp:47`), where `L` is the clip
  length from the read start, not the alignment length. A marginal hit (S ∈ {20, 21}) beginning a few
  bases into the read is rejected by STAR and accepted by Cell Ranger. Above S=21 the two agree.
- STAR clips 5' first and computes poly-A on the already-clipped sequence (`readLoad.cpp:60-61`);
  Cell Ranger computes both on the raw read and intersects. Matters only when the two trims overlap.

**A 2×150 run additionally enters the two length-gated bands** — the 91-base TSO window and the
10-mismatch poly-A break. At the recommended cycle counts it cannot.

**Multiome and GEM-X v4 need no separate answer.** 10x states outright that "the Multiome Gene
Expression library is same as the Chromium Single Cell 3' Gene Expression Dual Index library"
(CG000338 Rev F p.71), and `cellranger-arc`'s published algorithm page repeats the 3' trimming
description verbatim. `ARC-v1` is `three_prime`, R2 / offset 0, 16 nt barcode, 12 nt UMI. GEM-X v4's
gel-bead primer and library construct are structurally identical to v3.1 (CG000731 Rev B p.76; Rev A
carries it at p.80).

**The residual read-through Cell Ranger does not close, and neither can we.** Past the poly-A, a
short 3' fragment's R2 runs into UMI-revcomp, then barcode-revcomp, then TruSeq Read 1 revcomp. Cell
Ranger searches no Illumina adapter on the GEX path — the constants exist
(`fastq_set/src/adapters.rs:29-50`) but only the VDJ trimmer uses them
(`vdj_asm_asm/src/adapter.rs:12-46`). Worse, once non-A bases follow the poly-A, *neither*
implementation trims the poly-A either: both are suffix-anchored, so a suffix of `A30 | UMI_rc |
BC_rc` clears no error-rate test and the whole tail survives. `--clip3pAdapterSeq` could not express
it in any case — the sequence is a per-read variable (this read's own UMI and barcode) with the only
fixed part 28 nt behind it. So keeping `CellRanger4` costs nothing relative to the vendor. Reaching
the UMI requires an insert under about `R2_len − 31`, and 10x's double-sided SPRI selection bounds
that population qualitatively (CG000731 Rev B, "Post Library Construction QC"); no first-party
quantification was found. This is the structural reason the 5' measurement's 3' negative control
reads zero: what a 3' read runs into past the insert is poly-A, and the 5' anchor is not in the
molecule at all.

## 3. 10x 5' GEX — the vendor runs no trimmer

Hard-disabled in source, by endedness, before the stage runs — every locator in this section is at
`cellranger-9.0.1` (`lib/rust/cr_lib/src/stages/align_and_count.rs:646-655`):

```rust
// Disable polyA and TSO trimming for 5' gene expression assay.
let args = if args.chemistry_defs.endedness() == Some(WhichEnd::FivePrime) {
    Self::StageInputs { trim_polya_min_score: None, trim_tso_min_score: None, ..args }
} else { args };
```

`None` means off (`align_and_count.rs:124-130`, *"Set to `None` to disable … trimming"*), and the
same block is present unchanged at `cellranger-10.0.0`, so this is not a 9.x quirk. Every `SC5P-*`
and `SCVDJ*` entry in `chemistry_defs.json` declares `"endedness": "five_prime"`, so the switch fires
for all of them. The absence is deliberate and marked as such in the second-mate branch
(`aligner.rs:395-401`): *"normally we would do trimming here, but for 5' chemistries, the usual
trimmer doesn't apply. TODO: let this comment serve as a placeholder for potential future 5'
trimmer."* 10x's own algorithms page scopes itself the same way: *"This section on read trimming
applies to 3' Gene Expression assays."* One 5'-only *metric* survives — `aligner.rs:144-145` aligns
the 3' TSO even when trimming is off, purely to compute `tso_frac`. It never modifies a read.

The two specs we model are `SC5P-R2` and `SC5P-R2-v3`: barcode and UMI on R1, and
`rna: {read_type: R2, offset: 0, length: null}` — **the cDNA read is taken whole, from base 0**. The
offset is applied as a range, not a search (`cr_types/src/rna_read.rs:391-397`; note the naming trap
that `r1_seq()` is the first *RNA* read, which for `SC5P-R2` is the whole of Illumina R2). v2 → v3
differs in UMI length (10 → 12), UMI `min_length` (null → 10) and whitelist (`737K-august-2016` →
`3M-5pgex-jan-2023`). The cDNA read is handled identically in both.

So `Hamming` with nothing declared reproduces Cell Ranger exactly. What `CellRanger4` was doing
instead: asking STAR to search the 5' end of a 5' cDNA read for a 30 nt TSO that this kit puts on the
**RT primer at the opposite end of the molecule**, and to clip poly-A from a 3' end where the 5'
construct puts the 13 nt TSO's reverse complement instead. That is a departure from parity that read
as parity.

**One deliberate departure from the vendor, on a measurement.** The 5' gel-bead primer is
`5'-CTACACGACGCTCTTCCGATCT-N16-N10/12-TTTCTTATATrGrGrG-3'` (CG000331 Rev E p.73; CG000733 Rev A
p.99), and 10x states the 13 nt TSO in words at CG000331 p.16. The barcoded top strand therefore
reads `[R1 handle][CB][UMI][TSO][insert]`, so a fragment shorter than R2 runs off its own cDNA into
`CCCATATAAGAAA` — the first fixed sequence a short 5' fragment's R2 can reach, and cellranger's own
`SPACER_RC` constant (`vdj_asm_asm/src/adapter.rs:8-9`). Cell Ranger does not clip it. Both 5'
entries declare it as a `read_through` anyway, on the prevalence and mechanism measured in
[`10x-5p-tso-read-through.md`](10x-5p-tso-read-through.md). These two entries are the one place this
KB exceeds vendor parity rather than following it.

## 4. BD Rhapsody WTA — one A-string, everything else before the aligner

BD's published preprocessing list, from BD's own docs (`steps_quality.html`, "Read trimming" and
"Read overlap detection"), split by which side of the aligner it happens on:

| clip | read / end | who | where |
|---|---|---|---|
| truncate to what cell label + UMI need | R1, 3' | BD `QualCLAlign` | before |
| 5' CC PCR handle `ACAGGAAACTCATGGTGCGT` | R1, 5' | BD | before |
| quality trim below Q20, BWA algorithm | R2, 3' | BD | before |
| CC PCR handle + CC capture sequence | R2, 5' | BD | before |
| R1 read-through removed from R2 (KMP, ≤9% mismatch, ≥25 bp overlap) | R2, 3' | BD | before |
| **poly-A, 38 literal `A`s** | **R2, 3'** | **STAR** | **inside** |

BD's whole STAR flag set, verbatim from `v3.0/rhapsody_pipeline_3.0.cwl:2373` and repeated in the
user-facing `v3.0/pipeline_inputs_template_3.0.yml:193`: `--outFilterScoreMinOverLread 0
--outFilterMatchNminOverLread 0 --outFilterMultimapScoreRange 0 --clip3pAdapterSeq A×38
--seedSearchStartLmax 50 --outFilterMatchNmin 25 --limitOutSJcollapsed 2000000`. That is the complete
set: one clip flag, and **`--clipAdapterType` is absent from every BD pipeline file**, so BD runs on
STAR's default `Hamming`. The faithful rendering is therefore `Hamming` plus BD's own literal — and
it is faithful in both directions, since `CellRanger4` would refuse BD's own command line and its 10x
TSO is a sequence BD's chemistry never contains. There is **no cutadapt on the WTA path**: the only
`cutadapt` in the entire CWL repo is the log glob of `VDJ_Trim_Reads.sh`, on the TCR/BCR branch.

The pre-aligner work is one CWL node, `#QualCLAlign.cwl`,
`baseCommand: ["mist_run_qualclalign.py"]` (`rhapsody_pipeline_3.0.cwl:5631`), which BD does not ship
as source (see *Not established*).

**Why a 38-base literal and not STAR's `polyA` keyword.** The poly-A that R2 reads into is the
reverse complement of the bead's poly-T capture: 18 dT on the Original V1 bead, 25 dT on both
Enhanced generations (`Misc/rhapsody_cell_label.py:18,29,42`). The keyword is a read-length-long
adapter and clips strictly more; BD passes a literal, byte-identical across 2.2.1, 2.3, 2.4b1-b3 and
3.0, machine-counted `len == 38`, `set == {'A'}`.

**The R2 side of the three chemistries is identical.** BD's own filtering table has three bead rows —
Original V1, Enhanced dT 3', Enhanced CC TCR/BCR — and both Enhanced generations share the middle
one; BD's source says Enhanced V2/V3 "have the same structure as Enhanced beads" with wider cell-key
diversity (`rhapsody_cell_label.py:37-42`). Our three specs differ only in minimum R1 length (60 vs
43), R1 layout, and cell-key pool. One `clipAdapterType` + `read_through` decision covers all three.

**The read-through BD handles in two pieces, and why only one is a flag.** R2's 3' end reads into
poly-A first and then into the reverse complement of R1 itself — cell label and UMI. BD gives STAR
the poly-A and removes the cell-label portion before the aligner with its KMP overlap search, because
it is read-specific and no fixed constant could express it. There is no fixed terminal adapter on the
WTA cDNA read analogous to 10x's TSO; the poly-A is the nearest thing, and it is exactly what BD
hands to STAR.

## 5. SPLiT-seq — the same clip, the right sequence

The authors' own pipeline, `Alex-Rosenberg/split-seq-pipeline@a711b56:split_seq/tools.py:404-407`
(identical at `yjzhang/split-seq-pipeline@c3923ea:split_seq/tools.py:417-420`, added by `62ecdec`,
2018-12-03, *"Added TSO trimming"*):

```python
TSO_location = seq1.find('AAGCAGTGGTATCAACGCAGAGTGAATGGG')
if 0<=TSO_location<20:
    seq1 = seq1[TSO_location+30:]
    qual1 = qual1[TSO_location+30:]
```

Read 1 is the cDNA read — the pipeline computes `cDNA_Q30_sum` from `qual1` while barcodes and UMI
come from `seq2`. The cut is at the **5' end**, accepted only when the match starts in `[0, 20)`, and
it discards everything through the TSO. **No poly-A and no other adapter**: an exhaustive grep of
both repos for `cutadapt|trimmomatic|fastp|trim_galore|polyA|AAAAAAAAAA` returns nothing, and the
align step passes STAR no clip flags at all (`tools.py:440`).

The sequence is the paper's own oligo, Table S12 sheet 1 row 16, `BC_0127`:
`AAGCAGTGGTATCAACGCAGAGTGAATrGrG+G` (`rG` riboguanosine, `+G` LNA-G — synthesis modifications on the
last three bases), i.e. `AAGCAGTGGTATCAACGCAGAGTGAATGGG` as DNA. It shares a 23 nt prefix and a 5 nt
suffix with the 10x 3' TSO and **differs at exactly two of thirty positions, 24 and 25 (1-based)** —
so the module's unconditional `CellRanger4` was firing, with the wrong string, on this chemistry's
cDNA read. That read is 66 nt (ENA reports `base_count / read_count` = 160.0000 for both GSE110823
runs; 160 − 94 for the barcode read), so a 30 nt TSO is about 45% of it. Removing the clip rather
than fixing it would have regressed.

**`clip5pNbases` is not a substitute, for two independent reasons.** The cut is *variable* — the
authors clip to `TSO_location + 30` over a 20-position search window, which a single N cannot express
— and it is *conditional*: their own `'TSO Fraction in Read1'` QC metric exists precisely because the
TSO is not on every read, and `clip5pNbases` cuts unconditionally (`ClipMate_clip.cpp:14-25`). A
blanket 30 bp cut would destroy 30 nt of genuine cDNA on every TSO-less read. The TSO must be
searched, which is what `CellRanger4` + `clip5pAdapterSeq` does. It is also what STAR's maintainer
recommends for Split-seq (alexdobin/STAR#1517, comment of 2022-04-08) — **take the clip flag from
that comment and not the barcode positions**, which are the commercial v2 layout, not the
Science-2018 one this entry models. The `CellRanger4` accept thresholds were tuned for a 30 nt
adapter and SPLiT-seq's is also 30 nt, so the calibration carries over unchanged.

**Accepted divergence.** `CellRanger4` also forces a 3' poly-A clip the authors do not perform, and
it cannot be separated from the TSO fix, `CellRanger4` being the only mode where `clip5pAdapterSeq`
is legal at all. SPLiT-seq is poly-A primed (the Round1 RT primer ends `TTTTTTTTTTTTTTTVN`,
Table S12 sheet 2), so the divergence is from the authors' pipeline, not from the biology, and on a
~36 nt post-clip read the score ≥ 20 bar should fire rarely. Whether it changes any count is
unmeasured.

## 6. What this refuted

Three claims were believed and acted on before this review. All three are wrong.

**BD's "A×38 → A×20 in v3.0" — refuted.** The literal is byte-identical in every version that
documents the flag: `v2.2.1/rhapsody_pipeline_2.2.1.cwl:1925`, `v2.3/…:1925` (and its
`pipeline_inputs_template_2.3.yml:191`), all three 2.4 betas, and `v3.0/…:2373` (and
`pipeline_inputs_template_3.0.yml:193`). **A 20-base A-run appears in no BD pipeline version**, and
neither does STAR's `polyA` keyword. The intuition probably comes from v3.0's release note *"More
aggressive cleanup of polyA sequence in reads to prevent spurious alignments"* — a change in BD's own
unpublished pre-aligner code, since the flag did not move.

**BD's three "R2 adapters" — real BD sequences, wrong oligo.** `ACAGGAAACTCATGGTGCGT` is
`Enh_5p_primer`, `TATGCGTAGTAGGTATG` is `Tso_capture_seq_Enh_EnhV2`, and `GTGGAGTCGTGATTATA` is
`Tso_capture_seq_EnhV3` (`Misc/rhapsody_cell_label.py:85-93`). All three sit on the **5'-capture /
Custom-Capture (TCR-BCR) oligo**, not on the poly-T oligo every WTA library is built from: BD's own
bead table lists the CC PCR handle as `None` for both WTA rows and the capture column as `18 dT` /
`25 dT`. On our three specs they are no-ops on real data. (BD's two pages disagree on the capture
sequence by a terminal `TG` — the trimming page prints the 15-mer `TATGCGTAGTAGGTA`, the source
constant is 17 nt. Neither is a WTA sequence, so it does not bite; do not treat the 15-mer as the
full constant.)

**The 41/43 bp 5' figures — not a clip on anything we model.** They are `rna.offset` on **R1**, and
only for `SC5P-R1`, `SC5P-R1-v3`, `SC5P-R1-OCM-v3` and the V(D)J chemistries — corroborated in
cellranger's own comment *"For the SCVDJ chemistry, we trim the first 41 bases of read1"*
(`vdj_asm_asm/src/adapter.rs:35-37`). Our two 5' specs are `SC5P-R2*`, whose cDNA read is R2 at
offset 0. At the recommended 26/28 R1 cycles (CG000809 Rev A p.19), **base 41 of R1 does not exist**;
cellranger requires R1 ≥ 66 for `SC5P-R1`.

A fourth, weaker claim goes with them: the **`-O 16` cutadapt minimum-overlap** attributed to BD's R1
handle is in no BD CWL, source, or documentation, and BD uses no cutadapt on the WTA path at all. The
one third-party reimplementation that does use cutadapt uses it to standardise Enhanced R1 into fixed
offsets — its own device, not BD's.

## 7. Not established

Labelled gaps, because a labelled gap is more useful than a smoothed-over one.

1. **`cellranger-arc`'s own trimming code.** Closed source. The multiome answer rests on Cell Ranger
   10.0.0's `ARC-v1` chemistry def plus its shared code path, and on 10x's published ARC algorithm
   text — not on the ARC binary. Different score thresholds there cannot be ruled out.
2. **Nobody diffed Cell Ranger's trimming thresholds across 4.x → 10.0.** Only `cellranger-10.0.0`
   was read in full (plus `9.0.1` for the 5' switch). The release-notes wording is unchanged since
   v4.0.0, but that is documentation, not code.
3. **Parse Biosciences `split-pipe`.** Closed source, documentation behind a support-portal login. No
   first-party statement on TSO, poly-A, or adapter trimming was obtained; everything public is
   third-party. Arguably moot — the entry scopes itself to the Science-2018 chemistry and puts
   Evercode out of scope.
4. **BD ships no runtime pipeline source.** `mist_run_qualclalign.py` is not in the CWL, and the
   docker-free install bundle (1.29 GB) contains only `cwl-runner` and its Python dependencies — no
   BD `.py` files. Everything above about BD's command line is BD's *published statement* of its
   defaults, in two independent files per version across three versions. The artifact that would
   settle it is the `bdgenomics/rhapsody:3.0` image, unreachable on the connection used. This is
   confirmatory only; it would not change the answer.
5. **What BD's v3.0 "more aggressive poly-A cleanup" actually changed.** In that same unpublished
   code. The flag is byte-identical 2.3 → 3.0.
6. **Whether `--clip3pAdapterSeq` behaves identically in 2.7.10b (BD's stated target) and 2.7.11b
   (our pin).** Nothing suggests it does not; not checked.
7. **The 5' TSO's length: 13 nt or 15 bp.** Two first-party 10x sources conflict — every user guide
   and cellranger's own `SPACER` constant give the 13-mer `TTTCTTATATGGG`, while
   `cr_types/src/chemistry/mod.rs:1388-1389` says *"there's 15bp of TSO at the beginning of R1"* and
   the 41/43/81/83 constants are built on 15. No source reconciles them. Immaterial here: the
   discrepancy lives entirely on R1, in configurations this KB does not model.
8. **Whether `CellRanger4`'s forced poly-A clip changes any SPLiT-seq count.** Reasoned from the
   score ≥ 20 threshold against a ~36 nt post-clip read, not measured. A cheap A/B on GSE110823.
9. **How often a 3' insert is short enough for R2 to read past the poly-A into the UMI.** No
   first-party number; 10x's size-selection instructions bound it only qualitatively.
10. **Opal's exact `endLocationTarget` semantics under `OPAL_MODE_OV` for a gapped marginal
    alignment.** The ungapped analysis is solid; a gapped marginal hit is not proven.
11. **Why `SC5P-PE` clips 26/28 (barcode + UMI, TSO retained) while `SC5P-R1` clips 41/43 (TSO
    removed)** on the same physical molecule. Both constants are primary; no source explains it.

## Sources

**Code, at pinned revisions, all fetched and read rather than recalled.**

- **STAR 2.7.11b** — `github.com/alexdobin/STAR`, tag `2.7.11b`; measurements against the binary in
  `.pixi/envs/test-star/bin/STAR`. `source/ParametersClip_initialize.cpp`, `ClipCR4.cpp`,
  `ClipMate.h`, `ClipMate_initialize.cpp`, `ClipMate_clip.cpp`, `ClipMate_clipChunk.cpp`,
  `ReadAlignChunk_mapChunk.cpp`, `readLoad.cpp`, `ParametersSolo.cpp`, `Parameters.cpp`,
  `parametersDefault`, `opal/opal.h`, `CHANGES.md`.
- **Cell Ranger** — `github.com/10XGenomics/cellranger`, tag `cellranger-10.0.0` (commit
  `ae7fcd195bd5b0caafa928fd55904663328564bc`) and tag `cellranger-9.0.1`. `lib/rust/cr_lib/src/
  aligner.rs`, `lib/rust/cr_lib/src/stages/align_and_count.rs`, `lib/rust/cr_types/src/rna_read.rs`,
  `lib/rust/cr_types/src/chemistry/mod.rs`, `.../chemistry_defs.json` (a symlink to
  `lib/python/cellranger/chemistry_defs.json`), `lib/rust/fastq_set/src/adapter_trimmer.rs`,
  `.../adapters.rs`, `lib/rust/vdj_asm_asm/src/adapter.rs`, `lib/rust/cr_wrap/src/bin/cellranger.rs`,
  `mro/rna/sc_multi_cs.mro`. **The two agent readings of the 5' endedness switch differ by one line
  at 10.0.0** (669-678 vs 668-677); the 9.0.1 locator `align_and_count.rs:646-655` is agreed and is
  the one cited above.
- **orbit** — `github.com/10XGenomics/orbit`, commit `9d967ef3695d592b6d96248e03d7c8a12b9b98c0` (the
  rev pinned in cellranger's `lib/rust/Cargo.lock`), `src/lib.rs:101-118`.
- **BD Rhapsody CWL** — `bitbucket.org/CRSwDev/cwl` @ `def13396f342953d55d553911bab7fb68e71bc7a`
  (2026-04-27). v3.0 added in `a21a146673826236c0544cbbbe2ccfe262b0006a` (2025-10-28); v2.3 in
  `362581d3486cee5b221aade0465cc269dd44d031` (2025-03-10).
- **BD Rhapsody cell-label utility** — `bitbucket.org/CRSwDev/scripts-for-rhapsody` @
  `2339ff22fc9041241ef1966c2d02af3d6f2c3888`, `Misc/rhapsody_cell_label.py`.
- **SPLiT-seq** — `github.com/Alex-Rosenberg/split-seq-pipeline` @ `a711b56` and
  `github.com/yjzhang/split-seq-pipeline` @ `c3923ea` (co-developed; committers include
  `Alex Rosenberg <abros@uw.edu>`).

**Vendor documents, first-party.**

- Cell Ranger, *Gene Expression Algorithm*, § "Read trimming" (which scopes itself to 3'), and
  *Release Notes* v4.0.0 (2020-07-07); Cell Ranger ARC, *Algorithm Overview*, GEX trimming section —
  `10xgenomics.com/support/software/{cell-ranger,cell-ranger-arc}/latest/algorithms-overview/`.
- **CG000731** *GEM-X Single Cell 3' v4 User Guide* — Rev B p.63 (run parameters), p.76
  (*Oligonucleotide Sequences*); the same construct is at p.80 of Rev A.
- **CG000315 Rev E** *Next GEM Single Cell 3' v3.1 (Dual Index)* p.16 — library schematic.
- **CG000338 Rev F** *Next GEM Multiome ATAC + GEX* p.71 (run parameters, and "same as the … 3' Dual
  Index library"), p.81 (*Sequences*).
- **CG000080 Rev B** *Technical Note — Base Composition of SC3' v2 Libraries*, Table 1 and pp.2-3.
- **CG000331 Rev E** *Next GEM Single Cell 5' v2 (Dual Index)* p.16 ("13 nt template switch oligo"),
  p.73 (gel-bead and RT primers), p.75 (ligation and index-PCR products).
- **CG000733 Rev A** *GEM-X Single Cell 5' v3* p.99 (*Oligonucleotide Sequences*).
- **CG000809 Rev A** *Sequencing Handbook* p.12, p.19 (cycle tables).
- BD Rhapsody Pipeline 3.0 documentation, `bd-rhapsody-bioinfo-docs.genomics.bd.com` —
  `/steps/steps_quality.html` ("Read overlap detection", "Read trimming", "Filtering criteria"),
  `/steps/steps_cell_label.html` ("Read 1 structure by bead version"), `/release_notes.html`,
  `/resources/pipeline_install_bundle.html`.

**Publication and archive.**

- Rosenberg et al., *Science* 2018, `10.1126/science.aam8999`; **Supplementary Table S12**
  (`aam8999_tables12.xlsx`, sha256 `1bf965120d296995ee6793340b5ee0fecf3c06806a55a5318cd5d15cd6bae426`
  — byte-identical to the hash already pinned in the SPLiT-seq entry), sheet 1 rows 14/16, sheet 2.
- ENA Portal API, `filereport?accession=SRR6750041|SRR6750042&result=read_run`.
- `alexdobin/STAR` issues #1517 (Split-seq parameters, 2022-04-08) and #1308 (`clipAdapterType`
  clarification, 2021-07-30 / 2021-08-04).
