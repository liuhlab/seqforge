# The STAR splice flags, counted twice: what the fates actually do

Measured **2026-08-22** on the lab's CPU cluster (`ircbc`, node `cpu02`), the gate
[#465](https://github.com/liuhlab/seqforge/issues/465) defines for the change specified in
[#461](https://github.com/liuhlab/seqforge/issues/461) and measured into being by
[#459](https://github.com/liuhlab/seqforge/issues/459). All timestamps below are cluster-local
(`+0800`).

**Both gates pass, and the falsifiable half passes decisively.** On the intron-free *E. coli*
component the spliced fraction of retained alignments falls from 0.132–2.764% to **0.0000–0.0097%**
— two of the four cells end with **zero** spliced records — while on the worm component the counted
UMI total moves by **−0.038% at worst**, twenty-six times inside the one-percent ceiling, and rises
on one cell. The `ambiguous` fate falls on every cell of both components and the fragments it loses
turn up as *counted* fragments rather than disappearing.

#459 brackets the artifact by read-level census (0.16–1.37% of unique reads by gap length, up to 18%
by anchor length) and says plainly that neither is the number that matters. This is the number that
matters: a plate's cells counted twice, with the candidate flags as the only difference.

## The two arms

Nothing here writes into the finished 784-cell plate at
`/share/lhqlab/wormbase/inhouse/aging_SS3/seqforge/`; it is read for its `config.yaml` and nothing
else. Both arms ran on `cpu02` inside an existing Slurm allocation (job `198207`, partition
`compute_cpu`, 56 cores), at `snakemake --cores 24` — three concurrent 8-thread STAR jobs. **Nothing
was submitted with `sbatch` or `srun`**, and the login node was used only for `git fetch`.

| | control (`before`) | treatment (`after`) |
|---|---|---|
| seqforge | `f2fe5996de4dade41e1b21846df92e75d70e2c79` (`origin/main`) | `6d441c7b4a32bc6418f81b3b6a2b6ff71e95c365` (`feat/459-star-splice-params`) |
| liulab-genome | `74a420cd3a391e7f5b4905985820cb0b3be5e3f2` | `ce23e02fdf3100ab05a64b64e639085469fd8a79` |
| `WORKFLOW_VERSION` | 2026.8.18 | **2026.8.19** |
| `kb_version` | 2026.8.8 | 2026.8.8 |
| pipeline | `ss3-gate465-before-d24628c77588` | `ss3-gate465-after2-511c02537307` |
| wall clock | **1 h 15 m 38 s** (03:57:11 → 05:12:49) | **29 m 33 s** (05:52:44 → 06:22:17) |

Four cells, the ones #459 censused: `day1_N2_1`, `day5_N2_5`, `day9_N2_9`, `day13_CF_1`. Both
components (`ce11` worm, `ecHT115` *E. coli*) come out of the one chimeric run. Chemistry
`smartseq3`, assembly `ce11_ecHT115`, annotation `wormbase_ws298+refseq_rs_2025_06_26`, 8 threads and
49152 MB per cell — the plate's own values, which are also the resource-hint defaults, so nothing was
passed. STAR 2.7.11b from `liulab-runtime_align-rna.sif`.

**The composed configs differ in exactly one line**, which is the claim that the arms differ only in
the flags:

```console
$ diff before/.../config.yaml after/.../config.yaml
9a10
>   intron_length_cap: 50000
```

`units.tsv` is byte-identical between the arms.

## What STAR was actually told

Read off `snakemake -n -p`, not off module source — a `shell:` literal is source, and a source claim
about a command is not a claim about a command. The treatment renders this on **all four** cells,
and the control renders **none** of it:

```text
--outFilterType BySJout --alignSJoverhangMin 8 --alignSJDBoverhangMin 1 \
--alignIntronMax 50000 --alignMatesGapMax 50000 --outSAMattributes NH HI AS nM jM jI
```

`50000` is the maximum over the chimera's components — `ce11` 50,000, `ecHT115` 1 — derived at
compose time rather than typed. The registered values were read back through the policy function
that writes them into the recipe:

```console
$ after/sfrun python -c "from seqforge.manifest.policy import intron_length_cap; ..."
ce11 50000        ecHT115 1        ce11_ecHT115 50000
sacCer3 None      hg38 1000000     mm39 1000000
```

`sacCer3` ships unfilled, as #461's Further Notes required.

## Gate 1 — the intron-free component's spliced fraction

*E. coli* has no introns, so **every `N` gap on `ecHT115` is spurious by construction**. This is the
one number with a known right answer, which is what makes it a control rather than an anecdote.
Measured over the retained (`.unique.cram`) selection, full scan, no subsample.

| cell | spliced fraction | spliced records | longest `N` gap |
|---|---|---|---|
| `day1_N2_1` | 1.1854% → **0.0000%** | 258 → **0** | 191,584 → **0** |
| `day5_N2_5` | 2.7639% → **0.0000%** | 1,642 → **0** | 201,757 → **0** |
| `day9_N2_9` | 0.3807% → **0.0097%** | 360 → **9** | 203,499 → 16,093 |
| `day13_CF_1` | 0.1315% → **0.0023%** | 4,981 → **86** | 249,247 → 39,244 |

**Gate 1 passes.** Two cells reach exactly zero. The residual on the other two is 9 and 86 records
out of 92,985 and 3,783,321, and it is worth naming honestly: their longest gaps are 16,093 and
39,244 bp, both **under** the 50,000 cap, so the cap cannot exclude them and the anchor filters did
not. The collapse is not total, and the survivors are precisely the population a length bound was
never going to catch — which is the same argument #461 makes for the cap being the backstop rather
than the fix.

## Gate 2 — the worm component's counted UMIs

A fix that buys cleanliness by discarding real reads is not a fix. The ceiling is one percent per
cell, on `X.sum` — the matrix total.

| cell | `X.sum` before | after | change |
|---|---|---|---|
| `day1_N2_1` | 376,955 | 377,746 | **+0.210%** |
| `day5_N2_5` | 560,122 | 559,908 | **−0.038%** |
| `day9_N2_9` | 693,397 | 693,240 | **−0.023%** |
| `day13_CF_1` | 117,332 | 117,296 | **−0.031%** |

**Gate 2 passes**, at −0.038% worst against a −1.000% ceiling. One cell gains counts. Genes detected
move by +13, 0, +3 and +1.

## The fates, per cell per component

The matrix total sits beside the fates deliberately: `ambiguous` falling while counts hold is a fate
that **moved**, and `ambiguous` falling while counts fall too would be a fate that was **thrown
away**. `counted` is `n_fragments` minus the four named fates, so the row closes.

### `ce11` (worm)

| cell | `n_fragments` | `no_feature` | `ambiguous` | counted | `X.sum` |
|---|---|---|---|---|---|
| `day1_N2_1` | 8,372,483 → 8,368,424 (−0.048%) | 97,909 → 98,893 (+1.005%) | 341,786 → 325,074 (**−4.890%**) | 5,008,011 → 5,031,239 (+0.464%) | 376,955 → 377,746 (+0.210%) |
| `day5_N2_5` | 12,926,912 → 12,924,693 (−0.017%) | 78,785 → 79,800 (+1.288%) | 731,215 → 713,669 (**−2.400%**) | 6,402,283 → 6,427,458 (+0.393%) | 560,122 → 559,908 (−0.038%) |
| `day9_N2_9` | 10,777,920 → 10,773,878 (−0.038%) | 530,403 → 532,841 (+0.460%) | 372,133 → 352,157 (**−5.368%**) | 5,815,801 → 5,848,652 (+0.565%) | 693,397 → 693,240 (−0.023%) |
| `day13_CF_1` | 4,197,934 → 4,187,998 (−0.237%) | 184,806 → 188,196 (+1.834%) | 148,550 → 122,032 (**−17.851%**) | 1,258,038 → 1,273,894 (+1.260%) | 117,332 → 117,296 (−0.031%) |

### `ecHT115` (*E. coli*)

| cell | `n_fragments` | `no_feature` | `ambiguous` | counted | `X.sum` |
|---|---|---|---|---|---|
| `day1_N2_1` | 12,022 → 11,408 (−5.107%) | 1,400 → 779 (−44.357%) | 218 → 153 (**−29.817%**) | 8,756 → 8,840 (+0.959%) | 887 → 887 (0.000%) |
| `day5_N2_5` | 33,414 → 30,870 (−7.614%) | 6,840 → 4,553 (−33.436%) | 766 → 56 (**−92.689%**) | 21,246 → 21,286 (+0.188%) | 3,710 → 3,709 (−0.027%) |
| `day9_N2_9` | 65,454 → 64,543 (−1.392%) | 1,757 → 1,058 (−39.784%) | 200 → 103 (**−48.500%**) | 44,814 → 44,857 (+0.096%) | 5,289 → 5,273 (−0.303%) |
| `day13_CF_1` | 2,571,719 → 2,569,425 (−0.089%) | 4,969 → 3,274 (−34.111%) | 1,800 → 1,321 (**−26.611%**) | 1,883,692 → 1,883,937 (+0.013%) | 32,875 → 32,372 (−1.530%) |

**`ambiguous` falls on all eight rows and counted fragments rise on all eight**, while `X.sum` is
flat — so the fragments leaving `ambiguous` are reaching genes, and the UMI total does not move
because they are duplicates of UMIs already counted. On `ce11`, `no_feature` rises 0.46–1.83%,
which is the other place they go: a fragment whose phantom junction is gone is a shorter span, and a
shorter span can cover no gene at all. This is the mechanism #459 predicts, seen from the counting
end.

**One number over one percent, and it is not a gate number:** `ecHT115`'s `X.sum` on `day13_CF_1`
falls 1.530% (32,875 → 32,372, i.e. 503 UMIs). The one-percent ceiling is the worm component's; on
the bacterium a lost count is a count that should not have existed, since the alignment holding it up
was spurious by construction.

### The same two columns for `ce11`, which are not gates

| cell | spliced fraction | spliced records | longest `N` gap |
|---|---|---|---|
| `day1_N2_1` | 9.3675% → 9.3357% | 1,021,444 → 1,019,256 | 1,326,638 → **49,271** |
| `day5_N2_5` | 17.7935% → 18.1073% | 2,569,303 → 2,617,409 | 1,291,212 → **49,672** |
| `day9_N2_9` | 19.4084% → 19.6923% | 2,610,850 → 2,654,688 | 1,449,548 → **49,795** |
| `day13_CF_1` | 7.4779% → 5.9618% | 241,926 → 191,515 | 1,803,572 → **49,736** |

`--alignIntronMax 50000` binds exactly as designed: every cell's longest gap lands just under the
cap, down from 1.29–1.80 Mb.

The `ce11` spliced fraction barely moves, and *rises* on two cells. That is the expected shape and worth stating: with the GTF in the index nearly
every real worm junction is annotated, `--alignSJDBoverhangMin 1` lowers what an annotated junction
needs, and `BySJout` re-places rejected reads rather than discarding them. The exception is
`day13_CF_1` at 7.478→5.962%, the most degraded library and the one #459 measures as worst on every
axis.

## The wall clock, which was not expected

The treatment ran in **29 m 33 s** against the control's **1 h 15 m 38 s** — the same four cells, the
same node, the same `--cores 24`, both from a cold pipeline directory. A **2.6x speedup**, from a
change whose two-stage `BySJout` mapping should cost time.

The likely mechanism is recorded in #459 as a second-order *benefit* of the tight cap rather than a
performance prediction: setting `--alignIntronMax` re-derives `winBinNbits` (50,000 → 14), dropping
the per-step window reach from ~589,824 bp to ~147,456 bp, so the transitive window merging that
fabricates the gaps is suppressed rather than merely bounded.

**The obvious confound is ruled out, and the per-cell shape is why.** The treatment ran third on a
node with ~89 GB free, after three passes over the same inputs, so a warm page cache is the first
thing to suspect. But page-cache warming is not cell-specific: it would lift every cell by about the
same factor. STAR's own figures, read from each cell's QC bundle (`log_final`), do not have that
shape — the speedup ranges 2.04x to 5.02x and tracks how much of each cell's splicing was
unannotated before the change:

| cell | reads | speed before | speed after | speedup | annotated splices, before → after | unmapped: too short |
|---|---|---|---|---|---|---|
| `day1_N2_1` | 9,039,826 | 115.4 | 235.8 | **2.04x** | 94.8% → 98.4% | 6.76% → 6.84% |
| `day5_N2_5` | 13,942,003 | 100.6 | 229.2 | **2.28x** | 97.3% → 99.2% | 6.62% → 6.69% |
| `day9_N2_9` | 11,950,655 | 77.7 | 195.6 | **2.52x** | 97.6% → 99.4% | 8.64% → 8.72% |
| `day13_CF_1` | 12,607,492 | 12.1 | 60.9 | **5.02x** | **71.4% → 98.5%** | 44.78% → 45.01% |

Speed is STAR's `Mapping speed, Million of reads per hour`. `day13_CF_1` — the most degraded library,
the one #459 measures as worst on every axis, and the only cell where more than a quarter of splices
were unannotated — is both the slowest before and the biggest gainer. The cells already mapping
against ~97% annotated junctions gain about half as much.

That shape fits where STAR actually spends the time, which is stitching rather than seed-finding:
`stitchWindowAligns` recurses over the seeds in a window branching include/exclude, so cost is
superlinear in seeds per window. A window grown transitively to a megabase on a compact genome
accumulates spurious seeds to combine; a 50,000 bp window does not. The cell with the most phantom
junctions had the most to combine.

**`% unmapped: too short` moves by +0.08 to +0.23 percentage points**, so the time is not being saved
by STAR giving up on reads — which is the same conclusion the counted-UMI gate reaches from the other
end.

**What is still not established** is the split between the two flags. `BySJout` is a two-stage
mapping and should *cost* time, which means the cap's own contribution is larger than the headline
2.6x rather than smaller; but the annotated-fraction jump on `day13_CF_1` (71.4% → 98.5%) is partly
`BySJout` rejecting alignments whose junctions failed the filter, and nothing here separates the two.
A third arm setting only the cap would settle it. No sizing or scheduling decision should rest on
this without that arm.

## Method — the exact commands

Workspace `/share/lhqlab/wormbase/inhouse/aging_SS3/splice-gate/`. `before/` is the control arm and
was run first; `collect.sh`, `h5ad_metrics.py`, `cigar_metrics.sh` and `join_metrics.py` are
arm-agnostic and produce both arms' numbers, so the two arms are collected by one script rather than
by two that could differ.

```bash
G=/share/lhqlab/wormbase/inhouse/aging_SS3/splice-gate

# 0. fetch, on the LOGIN node (cpu02 has no internet). Its git is 1.8.3.1 and has no `-C`.
ssh ircbc 'cd /share/home/lhq/src/seqforge      && git fetch origin'
ssh ircbc 'cd /share/home/lhq/src/liulab-genome && git fetch origin'

# 1. the two clones, from inside the SIF on cpu02 (the SIF ships git 2.43).
#    --shared borrows the source object store and writes nothing back into it -- which is also why
#    a later commit needs no second fetch inside the clone: the objects are already reachable.
singularity exec -B /share \
  /share/lhqlab/liulab_data/packages/liulab-runtime_align-rna.sif bash -c '
    git clone --shared /share/home/lhq/src/seqforge      '"$G"'/after/code/seqforge
    git clone --shared /share/home/lhq/src/liulab-genome '"$G"'/after/code/liulab-genome
    cd '"$G"'/after/code/seqforge      && git checkout 6d441c7b4a32bc6418f81b3b6a2b6ff71e95c365
    cd '"$G"'/after/code/liulab-genome && git checkout ce23e02fdf3100ab05a64b64e639085469fd8a79'

# 2. PROVE the shim imports THIS arm's checkout. Two arms importing one tree compare nothing.
$G/after/sfrun python -c 'import seqforge, genome; print(seqforge.__file__, genome.__file__)'
#   -> .../splice-gate/after/code/seqforge/src/seqforge/__init__.py
#      .../splice-gate/after/code/liulab-genome/src/genome/__init__.py
#   (the plate's own script/sfrun prints /share/home/lhq/src/... for the same line)

# 3. compile: FASTQ -> manifest -> recipe -> Snakefile.  ~16 s.
$G/after/compose.sh > $G/after/compose.json 2> $G/after/compose.err

# 4. VERIFY THE FLAGS BEFORE ALIGNING ANYTHING. A dry run formats the shell block without
#    running it, which is the only honest way to read the rendered command line.
cd $G/after/ws/seqforge/pipeline/ss3-gate465-after2-511c02537307
$G/after/sfrun snakemake -n -p --cores 24 | grep -c -- '--alignIntronMax 50000'   # -> 4

# 5. run it. `seqforge run` does NOT invoke snakemake -- it stops at the Snakefile, which is the
#    deliverable -- so the pipeline is run here, in the existing allocation.
$G/after/run.sh > $G/after/run.log 2>&1

# 6. the numbers, then the diff. ~102 s: it decodes every retained alignment in all eight CRAMs.
cd $G && ./collect.sh after
$G/after/sfrun python $G/diff_arms.py $G/numbers
```

`cigar_metrics.sh` coerces every gap length with `+0` before comparing it. awk's `substr` returns a
string and `"94511" > "100912"` is true lexicographically; an earlier pass of #459's own repro
compared them as strings and was wrong by two orders of magnitude.

Machine-readable results are `numbers/before.{tsv,json}`, `numbers/after.{tsv,json}` and
`numbers/gate.{tsv,md}`. Each `.json` carries its arm's two SHAs, the import paths the run actually
used, the workflow version, the pipeline directory and the composed config.

## The first attempt failed, and a reader re-running this needs to know why

**The treatment's first two pipeline directories produced no numbers.** Against seqforge `7c88c39` —
the branch tip at the time, with both flag commits in place — every `split_chimera` job died:

```text
File ".../seqforge/workflows/split.py", line 367, in _rewritten
    out.set_tags(record.get_tags(with_value_type=True))
ValueError: invalid value type 'B'
```

`--outSAMattributes ... jM jI` puts the first `B`-type **array** tags this pipeline has ever carried
into a record — `jM:B:c,22` and `jI:B:i,7111,7157` on a real spliced read from this run. pysam's
reader reports an array's value type as the bare letter `B`, and its writer rejects that letter,
because the subtype is the element width and an array already carries it as its own typecode. The
round trip in `_rewritten` handed the writer what the reader said, so the chimeric split could not
run at all. `split.py` is untouched by the flag commits: the defect is pre-existing code that the new
tags made reachable for the first time.

Fixed in **`6d441c7`**, which keeps the declared type for every scalar — an `i` tag whose value fits
in a byte must not be silently narrowed — and drops it only for arrays. Verified against a real
record before re-running: `NH/HI/AS/nM/jM/jI/RG` round-trip byte-identically, subtypes included.

**Every number on this page comes from the third attempt**, pipeline `ss3-gate465-after2-511c02537307`,
against seqforge `6d441c7` and liulab-genome `ce23e02` at `WORKFLOW_VERSION 2026.8.19`, which ran
32 of 32 steps with no failed rule. The first attempt's directory is kept beside the arm at
`after/abandoned/ss3-gate465-after-2b3f1aef4451` as the record; an intermediate attempt against a
locally patched clone was discarded and reported nothing.

The general lesson, which is the reason this section exists rather than a note in a commit message:
**this change cannot be measured on a chimeric reference by any seqforge older than `6d441c7`**, and
the failure is a hard crash rather than a wrong number.

## What this does not cover

**Four cells of one plate, and one organism pair.** `ce11` + `ecHT115`, Smart-seq3, one library
prep, one sequencing run. The 784-cell plate is not re-measured and #461 puts that out of scope
deliberately.

**Which flag did what.** All four flags changed together, by design — the gate asks whether the
change ships, not which part of it earns its place. Nothing here separates `BySJout` from the two
overhang minimums from the length cap, and the `ecHT115` residual under Gate 1 is the one hint that they
are not interchangeable.

**The mammalian caps.** `hg38` and `mm39` ship at 1,000,000, which is ENCODE's convention and not a
measurement — theirs or ours. This gate is on worm and bacterium and touches neither value. `sacCer3`
ships unfilled and so is unchanged by construction.

**A poorly annotated organism.** `BySJout` exempts annotated junctions outright, and nearly every
real worm junction is annotated in the index used here. That is exactly what makes the risk small on
this pair and says nothing about an organism whose junctions are mostly novel.

**The counter's fragment span.** `_fragment_span` still returns a contiguous interval that does not
excise `N` gaps, and that is unchanged and out of scope — the `ambiguous` improvements above are the
aligner emitting fewer phantom gaps, not the counter handling them differently.

**The two-stage mapping cost at plate scale.** #459 records it as unmeasured. The wall clock above
went the other way on four cells, which is an observation and not a cost model.

**Anything about `--peOverlapNbasesMin`, the ENCODE mismatch pair, or `sjdbScore`.** All three are
recorded in #461 as out of scope, each needing its own measurement.

## Reproduction

The workspace is `/share/lhqlab/wormbase/inhouse/aging_SS3/splice-gate/` on `ircbc`, and it holds
both arms' code clones, both composed pipelines, both run logs and the collection scripts. Re-running
`./collect.sh <arm>` against either arm reproduces that arm's numbers from its own outputs, so an arm
collected twice gives the same answer.
