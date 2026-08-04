# 23. STAR's memory escalates on retry, and a job that still does not fit fails loudly

Date: 2026-08-03

## Status

Accepted. Extends [0022](0022-three-owners-for-an-aligner-param.md) by one flag — `--outSAMmultNmax`
is a fourth literal whose value varies with nothing, so it is the module's by 0022's own test.
Supersedes nothing. What is new is that one of those module-owned caps is no longer a constant: it is
a function of which attempt is running.

## Context

`starsolo_count` requested `resources.mem_gb` once, ran once, and handed STAR a `--limitBAMsortRAM`
set to **3/4 of that request**. Both halves were reasonable and both were incomplete.

The cap is not a budget for the job. It is a budget for **one allocation inside it** — the coordinate
sort, which since [#198](https://github.com/liuhlab/seqforge/issues/198) is not optional, because
STAR writes `CB`/`UB` into no output but the sorted BAM. What it does not bound is everything else
STAR holds while sorting, and on a single-cell run the largest of those is `readInfo`:

```text
typedef struct { uint64 cb; uint32 umi; } readInfoStruct;   // 16 B
readInfo.resize(nReadsInput, ...)                           // one entry per INPUT read
```

Sixteen bytes times every read that entered the aligner, held before a single alignment is sorted.
215M reads is 3.4 GB of it. `PRJNA658829/SAMN15970313` — 2.23 **billion** reads — is **35.7 GB**, and
that figure is arithmetic over a measured constant rather than an extrapolation from a curve.

The two facts then compose badly, because `--limitBAMsortRAM` **permits rather than reserves**: STAR
allocates whatever the sort turns out to need and refuses only if that would exceed the cap. So on a
large sample the 3/4 rule cheerfully authorises a multi-gigabyte sort allocation *on top of* a 36 GB
`readInfo` it cannot see, the job's true footprint clears the scheduler's limit first, and the
process is **killed from outside**. What lands in the log is `Killed`, or an exit 137, or a truncated
tail with no `EXITING because of FATAL ERROR` anywhere in it — and STAR's own diagnostic, the one
that names the number it needed, is never printed, because STAR never refused anything.

**The obvious reading of that log is "big samples need more memory", and the obvious fix is to raise
the default.** A future reader will reach it from the same evidence, and they will not be wrong about
the sample: 2.23 billion reads really does need a lot of memory. They will be wrong about the defect.
The defect is that the failure was **illegible** — indistinguishable, from the outside, from a
preempted node, an oversubscribed queue, a bad `--limit` value, or a bug in our own rule. A pipeline
that runs headless over ~10⁴ datasets cannot afford a failure mode whose diagnosis is "read the
scheduler's accounting and guess". This record exists so that investigation is not run twice; the
measurements behind it are [Discussion #203](https://github.com/liuhlab/seqforge/discussions/203),
and a Discussion is not in the repo, which is exactly why the argument needs an in-tree home.

## Decision

**The memory a STARsolo job requests escalates with the attempt, every memory cap STAR is handed
derives from that escalated request, and a job that exhausts the escalation fails loudly — rather
than every job being sized for the worst sample in the corpus.**

| attempt | `resources.mem_mb` | `--limitBAMsortRAM` | at the default `mem_gb: 48` |
| --- | --- | --- | --- |
| 1 | `mem_mb` | 3/4 of it | 48 GB, sort capped at 36 GB |
| 2 | `2 × mem_mb` | 3/4 of it | 96 GB, sort capped at 72 GB |
| 3 | `3 × mem_mb` | 3/4 of it | 144 GB, sort capped at 108 GB |

Three attempts (`retries: STARSOLO_RETRIES`, which is 2), and the escalation is **linear, not
exponential**. Linear because attempt 1 must ask for exactly what the recipe asked for — a version
bump that silently changed what a first run requests would make every existing `mem_gb` mean
something new — and because a ceiling of 3× is a number an operator can predict from the recipe
instead of one they have to compute. Both columns are `resources:` entries over `attempt`, not one
resource and one derived param, so the retry that was granted three times the memory actually gets to
use it; the alternative is a job that asks for 144 GB and then refuses to sort in more than 36. The
`params:`-shaped version of that second column is a trap and is argued at **So in code**.

The arithmetic — `STARSOLO_RETRIES`, `escalated_mem_mb(mem_mb, attempt)`, `bam_sort_ram(mem_mb)` —
lives in `src/seqforge/workflows/memory.py`, importable and unit-tested, rather than inline in the
`.smk`. A lambda in a rule body is renderable by a dry run at attempt 1 and by nothing else, so the
escalation would have had no gate at all.

And **`--outSAMmultNmax 1`**, which is not part of the escalation but was found by the same
measurement and pays back a slice of it. STAR wrote *every* alignment of a multi-mapping read into
the BAM and sorted them all; `workflows/cram.py` then discarded the secondaries with `-F 0x100`. On
the measured sample that is 198.8M records sorted to retain 162.9M — **~18% of the sort spent on
records nothing keeps**. `nTrOutWrite = min(P.outSAMmultNmax, nTrOutSAM)` writes only a top-scoring
alignment, which is the record that survives the CRAM filter. Verified against the STAR 2.7.11b
source that the parameter appears **only** in the SAM/BAM write path and the alignment-ordering code,
and in no Solo counting file, so the matrices are unaffected; and against the pinned binary that the
name is recognised.

**The retained CRAM is not byte-identical, and #205 was wrong to say it was.** For a uniquely-mapping
read it is bit-for-bit unchanged; for a read with `NH > 1`, two things move, and `--outSAMmultNmax`
is itself the trigger for both:

```cpp
// ReadAlign_multMapSelect.cpp — the condition is the flag, not the multimapper order
if (P.outMultimapperOrder.random || P.outSAMmultNmax != (uint) -1 ) {   // partition trMult
    ...
} else if (P.outMultimapperOrder.random || P.outSAMmultNmax != (uint) -1) {
    trMult[0]->primaryFlag=true;   // ...instead of trBest->primaryFlag=true
```

- **`HI` changes.** It is an output-order index — `iTrOut + P.outSAMattrIHstart` in
  `ReadAlign_alignBAM.cpp`, where `iTrOut` is the write loop's counter — so the retained record now
  always carries `HI:i:1` rather than its position in the unordered list.
- **Which alignment is retained can change.** `trBest` is chosen with a tie-break on the shorter
  genomic span (`trAll[iW1][0]->gLength < trBest->gLength`, `ReadAlign_stitchPieces.cpp`); the
  partition above takes the first equal-scoring alignment in window order. Both are top-scoring, so
  this is a change of tie-break rather than of quality — but for a read tying across loci of
  different span (a spliced gene against a processed pseudogene) the POS, CIGAR, `AS` and `nM` differ.

`NH` is unaffected: it is computed from `nTrOutSAM`, the full locus count, not from the truncated
write count. So is every count matrix — `SoloFeature_addBAMtags` keys `CB`/`UB` on the read index
alone, and the gene assignment is an order-independent set union. This is affordable because the
`WORKFLOW_VERSION` bump already obliges reprocessing; it would not have been, silently, under a
version that claimed nothing had changed.

## Why not size every job for the worst sample

Because the worst sample is not the corpus. Raising `mem_gb` until `SAMN15970313` fits means a
default carrying ~36 GB of `readInfo` headroom that the other hundred worm samples never allocate —
paid on every job, in queue time on a shared cluster, forever, so that one job never has to retry.
That is the trade backwards: the escalation charges the extra memory to the jobs that actually need
it, and charges the rest a retry they will never take.

It is also the wrong artifact. A per-dataset memory ceiling is *intent about one dataset*, and
[0004](0004-two-artifacts-not-one.md) split the recipe out from the manifest precisely so one dataset
can be processed under different intent without moving anyone else's numbers — a `resources.mem_gb`
override in one `processing.yaml`, the manifest hash unchanged. A knob that already exists, in the
artifact that already exists to hold it, beats widening a default that every dataset inherits.

And it would not even be *safe*. There is no value of `mem_gb` that is provably enough, because the
thing that overruns is unbounded in the input (see below) and the corpus keeps growing. A default
picked to cover today's largest sample is a default that will be too small again, with the same
illegible failure — so it buys wasted memory now and no guarantee later.

## Why not bound `readInfo`

Because there is no knob. Read off the pinned STAR 2.7.11b binary rather than remembered, the
complete `--limit*` set is eight options:

| option | what it bounds |
| --- | --- |
| `--limitGenomeGenerateRAM` | index *generation* — not a mapping run at all |
| `--limitIObufferSize` | the input/output buffers |
| `--limitOutSAMoneReadBytes` | one read's SAM output |
| `--limitOutSJoneRead` | junctions from one read |
| `--limitOutSJcollapsed` | the collapsed junction table |
| `--limitBAMsortRAM` | the coordinate sort |
| `--limitSjdbInsertNsj` | junctions inserted on the fly |
| `--limitNreadsSoft` | a soft read count, not bytes |

None of them covers `readInfo`, and none covers the loaded genome index either. **There is no
`--limitSoloRAM`.** The per-read Solo array is not a buffer STAR sizes to a budget; it is a
`resize(nReadsInput)` whose length is a property of the input file, so "bound it" is not a flag we
failed to pass — it is a change to STAR.

Which leaves the honest options: model the allocation and refuse up front, or give the job more
memory and let it retry. Modelling means computing `nReadsInput` before the run, which means counting
reads in a FASTQ — a whole-file read, and R3 says we never do that. `probe`'s bounded head cannot
answer "how many reads are in this file" and was designed not to. So the number we would need to
refuse on is a number we have decided, at the architecture level, not to have.

## Why not drop `Velocyto` from `soloFeatures`, or drop `CB`/`UB` from the CRAM

Both were considered as memory savings, both were measured against what they would cost, and both
were kept.

`Velocyto` is one of the two independent triggers for the per-read `readInfo` array — so it does not
share the cost with the other four features, it *causes* it, and dropping it is a real reduction
rather than a cosmetic one. It is nevertheless the wrong lever.
[0012](0012-produce-every-answer-rather-than-ask.md) makes all five features the default on the
argument that one alignment pass can afford five counting rules, and dropping one for memory does not
dissolve the question it settled — it *reintroduces* it, as "was this dataset one where anybody
wanted spliced/unspliced counts?", asked per dataset, answered by whoever last edited the module.
Worse, it would answer it for every dataset in order to fix one. The `readInfo` cost is a real
operating cost of producing every answer, and it is **accepted**, which is a different thing from
being unnoticed.

`CB`/`UB` in the retained CRAM is the same shape of argument with an even shorter answer.
[#198](https://github.com/liuhlab/seqforge/issues/198) put the barcode in the CRAM so the retained
artifact can be recounted, re-quantified and re-run under another tool; a CRAM carrying raw `CR`/`UR`
instead is a CRAM whose barcodes have not been corrected against the whitelist, which is precisely
the work that makes it recountable. Reverting it would trade the value of every CRAM in the corpus
for headroom on one sample, and it would not even help much — `readInfo` is allocated for the
counting, not for the output attributes.

## Why a loud failure is the right terminal state

Because the alternative to a loud failure here is not a quiet success; it is a *quieter* failure. The
job that does not fit does not fit. The only question is whether the corpus builder can tell what
happened.

An OOM kill says nothing. `Killed` is what a preempted node, an oversubscribed queue and a genuine
overrun all look like, and the operator's next move — retry, raise the memory, check the input, or
file a bug against us — is undetermined by the evidence. STAR refusing is a diagnosis: it names the
allocation, names the number of bytes it wanted, and exits non-zero with `EXITING because of FATAL
ERROR` in the log, which is a sentence a human can act on and a string a script can match.

So the escalation is not there to make every job succeed. It is there to make the common overruns
succeed *without anybody being paged*, and to make the residue fail in the legible way rather than
the illegible one. In the common case the sort is what does not fit, and then even the terminal
failure is a diagnosis rather than a kill. A sample that needs 36 GB before it sorts anything will
exhaust three attempts at the default and stop, and that is the design working: it stops, it says so,
and the fix is a one-line override in one recipe.

## So in code

**A memory cap an aligner is given must be a `resources:` entry over `attempt` — never a `params:`
one, and never `config["mem_mb"]` directly.** In a rule that declares `retries`, `config["mem_mb"]`
is the *first attempt's* number and nothing else. The bug shape to watch for is a rule that escalates
its request and then keeps handing the aligner attempt 1's cap: it looks like it retried, it consumed
the queue time of a retry, and it refuses in exactly the same place. Put the arithmetic in
`workflows/memory.py` and call it; do not re-derive a fraction of a memory request inside a shell
block, where nothing can import it and no unit test can reach it.

**The `params:` half of that imperative is not fastidiousness — it is the bug this record's own first
implementation shipped.** Snakemake does hand `resources` to a `params:` callable, so
`sort_ram=lambda wildcards, resources: bam_sort_ram(resources.mem_mb)` reads correctly, plans
correctly, passes a dry run, and freezes: `Job.attempt`'s setter clears `self._resources` and **not**
`self._params`, and `reset_params_and_resources()` is one-shot behind a
`_params_and_resources_resetted` flag, so the params expansion happens once, on attempt 1, and every
retry reuses it. Traced over three attempts on the pinned snakemake 9.23.1:
`mem=1000 cap=750` / `mem=2000 cap=750` / `mem=3000 cap=750` as a param, against `750` / `1500` /
`2250` for the identical arithmetic as a resource. Every structural check in the suite was green on
the frozen version, because the shape was right and only the semantics were wrong.

**Enforced by.** `test_a_snakemake_retry_re_expands_a_resource_and_never_a_param`,
`test_the_star_rule_escalates_its_memory_on_retry`,
`test_the_module_never_computes_a_star_memory_cap_from_the_config` and
`test_the_sort_budget_follows_the_escalated_memory_request` (`tests/test_workflows.py`);
`test_the_composed_pipeline_plans_the_h5ad_the_whitelist_and_the_command_star_receives`
(`tests/test_compose.py`).

**What is not enforced: that `starsolo_count` itself escalates on a real retry.** The *construct* it
relies on is proven — `test_a_snakemake_retry_re_expands_a_resource_and_never_a_param` runs a real
three-attempt snakemake over a synthetic eleven-line workflow and reads the trace, which is what makes
"a `resources:` callable is re-expanded and a `params:` one is not" a measured fact rather than a
reading of snakemake's source. What no gate here covers is that rule, with that aligner, on a sample
large enough to fail: it would need a genome index, STAR, a scheduler, and a sample big enough to
exhaust memory three times over, none of which this suite owns. So the coverage is the arithmetic
(`escalated_mem_mb` and `bam_sort_ram` as functions), the construct (a real retry, synthetically), and
the wiring that binds the two to the rule (its source shape). A regression would have to be something
that leaves all three green — a STAR that stops honouring `--limitBAMsortRAM`, say — and noticing it
would take the granted `mem_mb` and the emitted cap read out of attempt 2's log on a real cluster.

## Consequences

- **`WORKFLOW_VERSION` is `2026.8.2`, so every `run_id` moves.** That is the intended axis for "the
  module changed" ([0005](0005-run-id-is-the-pairing.md)) and it is not free: a corpus already
  compiled under `2026.8.1` recompiles to new run directories. It is one bump for two changes for
  that reason.
- **No new config key.** `mem_gb` is the key it always was; what changed is that it now names the
  *first attempt's* request. The `ResourceHints` docstring in `src/seqforge/models/processing.py` is
  where that reading is stated for the person who writes the recipe.
- **A known residual, deliberately not solved.** `PRJNA658829/SAMN15970313` (2.23 billion reads, 2.44
  billion alignment records) needs ~36 GB of `readInfo` before it sorts anything and will exhaust the
  escalation at the default. It needs a per-recipe `resources.mem_gb` override — the per-dataset
  escape hatch [0004](0004-two-artifacts-not-one.md) exists for — and that is the intended outcome,
  not an outstanding bug.
- **There is a hard ceiling above all of this, and it is worth knowing before anyone reprocesses that
  sample.** STAR packs the read index into the upper 32 bits of a per-record field (`iReadAll<<32`,
  three call sites in `ReadAlign_outputAlignments.cpp`), so **2³² reads — 4.29 billion — is a limit
  no amount of memory raises**. At 2.23 billion, the largest sample in the worm corpus is under it,
  but not by much. A sample past it does not need a bigger `mem_gb`; it needs to be split.
- **`-F 0x100` stays in `workflows/cram.py`.** With `--outSAMmultNmax 1` there are no secondary
  records left to filter, which makes the filter a cheap invariant rather than a load-bearing step —
  and that is the right reason to keep it, not to delete it: it costs one flag and it is what makes
  the CRAM's contents independent of how STAR was invoked.
- **[0022](0022-three-owners-for-an-aligner-param.md) gains a fourth module-owned literal** and is
  amended in place to say so. `--outSAMmultNmax` varies with nothing — there is no dataset for which
  writing alignments the CRAM step then deletes is correct — so it is the module's, and it is
  invisible to the params gate for the same reason every literal there is.
- **A multimapper's retained record changes, so a re-run CRAM will not diff clean against an old
  one.** `HI` becomes `1`, and where loci tie on score the retained alignment may be a different one
  of them (see the Decision). Unique reads and all counts are unchanged. Anyone comparing a
  `2026.8.2` CRAM against a `2026.8.1` one should compare on `(read name, NH)` and on the matrices,
  not on bytes — and should not read a difference there as a defect.
