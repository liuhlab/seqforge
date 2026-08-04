"""What the STARsolo mapping rule asks the scheduler for, and how much of it STAR may sort in.

Two numbers that only mean anything together, so they live in one file. ``starsolo.smk`` requests
``mem_mb`` from the scheduler and hands STAR ``--limitBAMsortRAM``; the second is a fraction of the
first, and any change that moves one without moving the other buys memory STAR is still forbidden to
use, or forbids STAR memory the job was never given. They used to be a module constant and a closure
*inside* the ``.smk``, where nothing could reach them: a Snakefile is not importable, so the
arithmetic deciding whether a two-billion-read sample lives or dies was only ever exercised by
running STAR against a two-billion-read sample. Here it is importable and unit-testable, and
``starsolo.smk`` imports it exactly as it already imports ``h5ad`` — seqforge's own helpers,
restated nowhere.

**The defect this file exists to remove (#205).** The 3/4 rule below was not wrong so much as
*incomplete*. ``--limitBAMsortRAM`` bounds the coordinate sort and nothing else, while STAR holds
allocations it does not bound at all — chiefly ``readInfo``, the per-read CB/UMI array STARsolo
carries for the whole run::

    typedef struct { uint64 cb; uint32 umi; } readInfoStruct;   // 16 B with padding
    readInfo.resize(nReadsInput, ...);

**16 bytes × every input read**, sized before a single alignment is sorted. A 215M-read sample
spends 3.4 GB on it; ``PRJNA658829/SAMN15970313``, at 2.23 billion reads, spends **35.7 GB**. STAR
ships eight ``--limit*`` knobs and *none* of them bounds ``readInfo`` or the genome index; there is
no ``--limitSoloRAM`` to reach for.

That matters because ``--limitBAMsortRAM`` **permits rather than reserves**: STAR allocates what the
sort actually needs and refuses only if that need exceeds the cap. So on a large sample the 3/4 rule
authorises a sort allocation *on top of* a 36 GB ``readInfo``, the total overruns the job's request,
and the job is **OOM-killed by the scheduler instead of refused by STAR**. The defect being removed
is that illegible death — not "big samples need more memory", which was never news, but that the
sample dies with a kill signal and no number instead of a FATAL naming what it wanted.

**Why escalation on retry, and not simply a bigger default for everyone.** The distribution is very
long-tailed: nearly every sample fits in today's request, and the handful that do not overrun it by
multiples. Sizing every job for the worst sample would multiply the scheduler footprint of ~10⁴
datasets to buy headroom that ~10⁴ minus a few of them will never touch, and a corpus that queues
badly is a corpus that does not get built. Escalating instead means the common case is byte-identical
to what shipped before (see :func:`escalated_mem_mb` — attempt 1 *is* today's request) and only the
samples that actually failed pay for the headroom, once each.

**The cap has to derive from the escalated request, and that is the whole fix.** A
``--limitBAMsortRAM`` pinned to attempt 1's ``config["mem_mb"]`` would let attempt 2 buy scheduler
memory that STAR was still forbidden to sort in — the retry would raise the ceiling and leave the
floor, and the second attempt would fail the same way for a reason the first attempt had already
recorded. So ``starsolo.smk`` derives ``params.sort_ram`` from ``resources.mem_mb`` rather than from
the config value: the retry raises the scheduler request and STAR's cap **together**.

**A sample that exhausts the retries fails.** That is the accepted outcome of #205, not a bug to
engineer around, and it is the honest end of the escalation: at some point the answer is a recipe
with a bigger ``resources.mem_gb``, decided by a human who looked at the sample, and not another
doubling applied blind. Note the failure is legible in the common case anyway — when the *sort* is
what does not fit, STAR names the number of bytes it needed and exits.

**The known residual, which is why the escape hatch matters.** ``PRJNA658829/SAMN15970313`` — 2.23
billion reads, 2.44 billion alignment records — needs ~36 GB of ``readInfo`` before any sorting
begins, so no linear escalation from a 32 GB default reaches it politely. It wants a per-recipe
``resources.mem_gb`` override, which is exactly the per-dataset escape hatch the two-manifest split
exists to provide: the dataset manifest says what the reads ARE and does not change, and a second
recipe says to spend more memory on them. Worth recording before anyone reprocesses it: STAR packs
the read index into the upper 32 bits of a per-record field (``iReadAll<<32``, three call sites in
``ReadAlign_outputAlignments.cpp``), so there is a hard **2³² read ceiling** of 4.29 billion. That
sample is under it, but not by much.
"""

from __future__ import annotations

#: How many times the scheduler may re-run ``starsolo_count`` after a failure, each time with more
#: memory. It lives here rather than as a literal `retries: 2` in the `.smk` because the retry count
#: and the escalation rule are ONE fact, not two: at :func:`escalated_mem_mb`'s linear escalation,
#: two retries means the last attempt is given 3x the default request, and that product — not either
#: factor — is what anyone reasoning about the worst case actually needs. Split across two files,
#: the count gets raised in the Snakefile by someone who never reads the multiplier, or the
#: multiplier gets changed here by someone who never counts the attempts, and the headroom the pair
#: was chosen to deliver silently becomes some other number.
STARSOLO_RETRIES: int = 2

#: Floor for STAR's BAM sort budget, in MiB. A job whose whole memory request is smaller than this is
#: not going to align anything anyway, so the floor costs nothing real and keeps the arithmetic below
#: from handing STAR a value smaller than a trivial sort needs.
_MIN_BAM_SORT_RAM_MB = 1024

#: What share of the job's memory the coordinate sort may claim: THREE QUARTERS, leaving a quarter for
#: the genome index, the aligner's own working set and the OS. Deliberately the same 3/4 the
#: `samtools sort` this replaced was given, because it is the same job against the same budget.
#:
#: Measured before it was chosen, on GSE208154/SAMN29720279 L001 in the pinned image, because the
#: shape of this number is not obvious: `--limitBAMsortRAM` is a CAP, not an allocation. STAR reports
#: "Max memory needed for sorting" and then refuses if that exceeds the cap; it allocates the need,
#: never the cap. So a generous cap costs a small run nothing, and a tight one only converts runs that
#: would have fit into FATALs.
#:
#: | records | max memory STAR needed |
#: | --- | --- |
#: | 1,999,909 | 394 MB |
#: | 9,844,534 | 1,590 MB |
#:
#: Linear, at **~160 bytes per alignment record**, and NOT reducible by binning: `--outBAMsortingBinsN`
#: 200 gave the identical figure and 1000 gave a slightly larger one, so the obvious remedy for
#: STAR's "not enough memory for BAM sorting" does not work here. A tight cap FATALs outright rather
#: than spilling — verified by passing 200 MB against a run needing 394 MB.
#:
#: The quarter this leaves behind is also where the incompleteness described at the top of this
#: module lives: `readInfo` is paid out of it, and on a large sample `readInfo` alone is larger than
#: the whole request. The fraction is not the thing that was wrong, so it is not the thing that
#: changed — what changed is that the request it is a fraction OF now grows with the attempt.
_BAM_SORT_RAM_NUMERATOR, _BAM_SORT_RAM_DENOMINATOR = 3, 4


def escalated_mem_mb(mem_mb: int, attempt: int) -> int:
    """The memory request for attempt ``attempt`` of ``starsolo_count``, in MiB — linear in the attempt.

    Snakemake's ``attempt`` is **1-based**, so attempt 1 returns ``mem_mb`` unchanged, and that
    property is the entire point rather than an accident of the arithmetic: a normal sample's first
    attempt must be byte-identical to what shipped before #205, because the overwhelming majority of
    samples fit in the default request and must not be made more expensive to schedule in order to
    rescue the few that do not. Attempt 2 gets 2x, attempt 3 gets 3x, and with
    :data:`STARSOLO_RETRIES` at 2 that last attempt is where the escalation stops.

    Linear rather than doubling, deliberately. The failures being rescued here are jobs that
    overran a budget by a `readInfo` array whose size is known and finite, not jobs whose appetite is
    unbounded — so a sequence that walks up in units of the original estimate reaches the answer
    while a sequence that doubles overshoots it, and an overshoot on this rule is a multi-hour job
    sitting in a queue waiting for memory it will not use.
    """
    return mem_mb * attempt


def bam_sort_ram(mem_mb: int) -> int:
    """``--limitBAMsortRAM``, in BYTES, derived from the memory this attempt was actually given.

    Not optional, and not a tuning knob: STAR's default of ``0`` means *"reuse the genome
    allocation"*, so the sort budget silently becomes a function of how big the genome happens to be.
    On a large genome that over-commits; on a small one (the yeast index `kb e2e` runs against) it is
    too small and STAR FATALs. Neither failure has anything to do with how much memory the job was
    actually given, which is the number that should decide this -- so we pass it.

    **The argument is the ESCALATED request, never the static config value.** ``starsolo.smk`` calls
    this with ``resources.mem_mb`` — the value :func:`escalated_mem_mb` produced for this attempt —
    precisely so that a retry raises the scheduler request and STAR's cap together. Before #205 this
    read ``config["mem_mb"]`` directly and was therefore a parse-time constant, which is to say it
    could not have followed anything even if a retry had existed to follow.

    **Sizing the job is the caller's business, and it is not free.** At ~160 B/record (measured
    above), a 215M-read sample lands near 32 GB of sort RAM. `mem_gb`'s default was 32 when that was
    measured, which left the sort 24 GB and would have FATAL'd such a sample -- which is why the
    default moved to 48 (#202), where 3/4 is 36 GB and the same sample fits with little to spare. That
    is a real cost of putting CB/UB in the CRAM, since STAR emits those tags only in the sorted BAM,
    and it is the recipe's `resources.mem_gb` that answers it for a sample the default does not hold.
    The failure is at least legible: STAR names the number it needed and exits, rather than producing
    a short BAM.

    STAR takes bytes; `mem_mb` is MiB, and that unit crossing is the whole reason this is a named
    function rather than an expression in the shell block.
    """
    share = mem_mb * _BAM_SORT_RAM_NUMERATOR // _BAM_SORT_RAM_DENOMINATOR
    # The floor may not exceed the budget itself: on a job smaller than the floor, claiming more than
    # the whole request would trade STAR's legible refusal for the scheduler's OOM kill.
    return min(mem_mb, max(_MIN_BAM_SORT_RAM_MB, share)) * 1024 * 1024


__all__ = [
    "STARSOLO_RETRIES",
    "bam_sort_ram",
    "escalated_mem_mb",
]
