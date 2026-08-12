"""What a mapping rule asks the scheduler for, and how much of it STAR may sort in.

Two numbers that only mean anything together, so they live in one file rather than in the `.smk` that
uses them. `starsolo.smk` requests `mem_mb` from the scheduler and hands STAR `--limitBAMsortRAM`; the
second is a fraction of the first, and any change moving one without the other buys memory STAR is
forbidden to use, or forbids STAR memory the job was never given. They used to be a module constant
and a closure *inside* the `.smk`, where nothing could reach them — a Snakefile is not importable, so
the arithmetic deciding whether a two-billion-read sample lives was only exercised by running STAR
against one. Here it is importable and unit-testable.

**Why the escalation is shaped this way** — the unbounded `readInfo` allocation, why
`--limitBAMsortRAM` permits rather than reserves, why the cap must derive from the *escalated*
request rather than attempt 1's, and why a sample that exhausts the retries is allowed to fail — is
argued once in ADR-0023. Read it before changing a number
here; every one of them is load-bearing in a way the arithmetic alone does not show.

**Two modules' arithmetic, and the second is a map rather than a cap.** `map/starsolo` has one
expensive rule. `map/star-umi` has *three* rule classes that scale differently — a shared-index load,
per-cell alignment whose surplus over that index is a sort buffer, and one plate-wide counting job
that loads no index at all — while the recipe still says exactly one thing, because `resources.mem_gb`
is intent and a recipe carrying every module's rule names would widen its schema on every new module.
So the module turns one figure into three requests (:func:`index_mem_mb`, :func:`per_cell_mem_mb`,
:func:`fan_in_mem_mb`) and escalation applies to each class alone.

**What that one figure covers, and why no single sentence about it stays true.** A mapping job's peak
is three things held at once: the genome index, resident for the life of the process; the aligner's
own working set; and a coordinate sort that grows with the number of alignment records. Residency
scales with the GENOME, sort RAM scales with the sample's DEPTH, and **which term dominates is a
property of the sample rather than of the workflow**. A plate cell of a few thousand reads is
index-dominated — 27.7 GB peak RSS against a 25 GB index, whether the cell holds 901 reads or 3.1M.
A 215M-read droplet sample is sort-dominated — at the ~160 B per alignment record measured below, its
sort alone wants ~32 GB, which is what moved the default 32 -> 48 GB. Both are real samples at two
ends of one curve, so the figure is sized by whichever term is LARGER for the end a recipe is aimed
at. Two consequences. On a small genome the surplus over the index is not waste, it is the sort doing
the work, so shrinking the figure because the index is small is the change that looks obviously right
and FATALs deep samples. And a recipe that shrinks it anyway — ``processing new --mem-gb`` is how —
has a floor to clear, which :func:`bam_sort_ram` derives.

**The cap is a `resources:` entry and not a `params:` one, which is the part that is easy to get
wrong.** Snakemake hands `resources` to a `params:` callable, so
`sort_ram=lambda wildcards, resources: bam_sort_ram(resources.mem_mb)` reads correctly, plans
correctly, and is broken: `Job.attempt`'s setter clears `self._resources` and **not** `self._params`,
and `reset_params_and_resources()` is one-shot behind a `_params_and_resources_resetted` flag. The
params expansion happens once, on attempt 1, and every retry reuses it. Traced on the pinned
snakemake 9.23.1 across three attempts — `mem=1000 cap=750` / `mem=2000 cap=750` / `mem=3000 cap=750`
against `750` / `1500` / `2250` for the same arithmetic declared as a resource. Only a `resources:`
entry taking `attempt` re-evaluates, which is what `starsolo.smk` declares (`bam_sort_ram_bytes`,
named for the unit STAR wants).

**One hard ceiling worth knowing before reprocessing anything large.** STAR packs the read index into
the upper 32 bits of a per-record field (``iReadAll<<32``), so there is a hard **2³² read ceiling** of
4.29 billion. The largest sample in the corpus sits under it, and not by much.
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


#: How many times the scheduler may re-run one of ``map/star-umi``'s two rule classes after a
#: failure. Its own name rather than a reuse of :data:`STARSOLO_RETRIES`, because the escalation
#: applies to each rule class **independently**: a fan-in counter that ran out of memory has nothing
#: to do with the per-cell mapping jobs that already finished, and a shared constant would tie the
#: two together for no reason beyond having the same value today.
PLATE_RETRIES: int = 2

#: What share of the recipe's memory figure the plate-wide counting job asks for, and the floor it
#: may not fall below. **A declared share, not a measurement, and it is here so it can become one.**
#:
#: What is known is the SHAPE, and the shape is what makes one figure wrong for both rules. A
#: per-cell mapping job pays the whole of a mapping job's peak — index residency, working set and
#: sort — and on a PLATE CELL it is the residency that dominates, measured at 27.7 GB peak RSS
#: against a 25 GB index whether the cell holds 901 reads or 3.1M. (On a deep sample the sort is what
#: dominates instead; the module header has the curve.) Either way it wants the whole figure the
#: recipe was sized with. The fan-in counter loads **no genome index at
#: all**: it reads the built annotation database into one interval index and accumulates the plate's
#: sparse matrices, which is a fraction of a mapping job rather than a multiple of it. Asking for the
#: mapping figure would idle three quarters of a node for the one job that must run after every cell
#: has finished, which on a scheduler is exactly when a large request queues worst.
#:
#: No peak has been measured for it, so the number below is a floor-and-share written in ONE
#: importable function precisely so a measurement can replace it without touching a rule.
_FAN_IN_NUMERATOR, _FAN_IN_DENOMINATOR = 1, 4
_MIN_FAN_IN_MEM_MB = 8 * 1024


def per_cell_mem_mb(mem_mb: int, attempt: int) -> int:
    """What ONE cell's mapping job asks for on ``attempt`` — the recipe's whole figure, escalated.

    The recipe says one thing, on purpose: ``resources.mem_gb`` is *intent*, and a recipe carrying
    every module's rule names would make each new module widen the recipe schema. So the module —
    the only artifact that knows its own rule graph — turns that one figure into two requests, and
    this is the first of them.

    It is the figure **unchanged** rather than a share of it, because a mapping job pays all of what
    a mapping job costs: the genome index, the aligner's working set, and the sort. Which of those
    dominates is the sample's business and not this function's — the module header has the curve, and
    a plate sits at its index-dominated end while a deep droplet sample sits at the other. Even the
    residency term is not simply "per process" here: ``map/star-umi``'s ``load_genome`` puts the
    index in SHARED memory with ``LoadAndExit`` and every mapping job depends on that rule's flag, so
    on a plate the index is resident once per node and attached rather than allocated per cell — the
    request still covers it, because a request that assumed the attachment would be wrong the first
    time a cell ran alone.

    That matters when the genome is small. Against the 1.3 GB ce11 index a per-cell request of 48 GB
    is ~35x the residency, and the surplus is doing real work — it is the sort budget. **Shrinking it
    because the index shrank is not free**: three quarters of a smaller figure is a smaller
    ``--limitBAMsortRAM``, and STAR FATALs on a deep sample rather than degrading, which is the exact
    regression the 32 -> 48 move fixed. It is nonetheless a legitimate thing for a recipe to do —
    ``processing new --mem-gb`` states it — against the floor :func:`bam_sort_ram` derives: the
    request must be at least four thirds of the sort the samples being mapped will need. That is the
    honest form of the choice, and it is the recipe's to make rather than a default's.
    """
    return escalated_mem_mb(mem_mb, attempt)


def index_mem_mb(mem_mb: int, attempt: int) -> int:
    """What the shared-index load asks for — the residency every mapping job on the node attaches to.

    ``load_genome`` declared **no memory at all** until this existed, which is the one rule in the
    module that unambiguously should: it is the job that materializes the genome segment, and a
    scheduler told nothing about it will co-schedule anything beside it and then watch the node OOM.
    Every other rule's request was carefully derived while the rule holding the largest single
    allocation asked for zero.

    The recipe's whole figure, because it is the only bound available and it is an upper one: the
    figure covers a mapping job, and a mapping job is this residency plus a sort buffer. Asking for
    an upper bound on the rule that runs **once per node** costs a scheduling slot briefly; asking
    for too little on it costs the node. The number to replace this with is the index's own size,
    which the recipe cannot see and ``liulab-genome`` can — that is the fix, and it is a measurement
    away rather than an opinion away.
    """
    return escalated_mem_mb(mem_mb, attempt)


def fan_in_mem_mb(mem_mb: int, attempt: int) -> int:
    """What the plate-wide counting job asks for on ``attempt`` — a share of the same one figure.

    The second half of the module's map. See :data:`_FAN_IN_NUMERATOR` for why a share and not the
    whole: this rule loads no genome index, so the number that sizes a mapping job says nothing
    about it. Escalation is applied to the share and applies to this rule class alone — a retry here
    does not mean a mapping job wanted more, and nothing re-runs to find out.
    """
    share = mem_mb * _FAN_IN_NUMERATOR // _FAN_IN_DENOMINATOR
    # The floor may not exceed the whole request: on a recipe smaller than the floor, asking the
    # scheduler for more than the pipeline was budgeted is a job that never starts.
    return escalated_mem_mb(min(mem_mb, max(_MIN_FAN_IN_MEM_MB, share)), attempt)


def bam_sort_ram(mem_mb: int) -> int:
    """``--limitBAMsortRAM``, in BYTES, derived from the memory this attempt was actually given.

    Not optional, and not a tuning knob: STAR's default of ``0`` means *"reuse the genome
    allocation"*, so the sort budget silently becomes a function of how big the genome happens to be.
    On a large genome that over-commits; on a small one (the yeast index `kb e2e` runs against) it is
    too small and STAR FATALs. Neither failure has anything to do with how much memory the job was
    actually given, which is the number that should decide this -- so we pass it.

    **The argument is the ESCALATED request, never the static config value.** ``starsolo.smk`` calls
    this inside a ``resources:`` callable over ``attempt``, on the value :func:`escalated_mem_mb`
    produced for that attempt, precisely so that a retry raises the scheduler request and STAR's cap
    together — and as a *resource* rather than a *param*, for the memoization reason this module's
    header sets out. Before #205 this read ``config["mem_mb"]`` directly and was therefore a
    parse-time constant, which is to say it could not have followed anything even if a retry had
    existed to follow.

    **Sizing the job is the caller's business, and it is not free.** At ~160 B/record (measured
    above), a 215M-read sample lands near 32 GB of sort RAM. `mem_gb`'s default was 32 when that was
    measured, which left the sort 24 GB and would have FATAL'd such a sample -- which is why the
    default moved to 48 (#202), where 3/4 is 36 GB and the same sample fits with little to spare. That
    is a real cost of putting CB/UB in the CRAM, since STAR emits those tags only in the sorted BAM,
    and it is the recipe's `resources.mem_gb` that answers it for a sample the default does not hold.
    The failure is at least legible: STAR names the number it needed and exits, rather than producing
    a short BAM.

    **Read backwards, the three quarters is a floor on the recipe: the request must be at least FOUR
    THIRDS of the sort the sample is expected to need.** On a human genome that constraint binds on
    nothing, because a figure big enough to hold a 25 GB index already holds four thirds of almost
    any sort. On a small genome it is the only thing holding the figure up — ce11's index is 1.3 GB,
    so nothing about residency argues against `--mem-gb 8`, and what that recipe actually buys is a
    6 GB sort, roughly 40M alignment records at the ~160 B each measured above. A plate of shallow
    cells sits far inside it; a deep sample does not, and four thirds of its sort is the number to
    state. Sizing DOWN for a small genome is a legitimate recipe decision — it is what
    ``processing new --mem-gb`` exists for — and this inequality is the thing it has to clear.

    STAR takes bytes; `mem_mb` is MiB, and that unit crossing is the whole reason this is a named
    function rather than an expression in the shell block.
    """
    share = mem_mb * _BAM_SORT_RAM_NUMERATOR // _BAM_SORT_RAM_DENOMINATOR
    # The floor may not exceed the budget itself: on a job smaller than the floor, claiming more than
    # the whole request would trade STAR's legible refusal for the scheduler's OOM kill.
    return min(mem_mb, max(_MIN_BAM_SORT_RAM_MB, share)) * 1024 * 1024


__all__ = [
    "PLATE_RETRIES",
    "index_mem_mb",
    "STARSOLO_RETRIES",
    "bam_sort_ram",
    "escalated_mem_mb",
    "fan_in_mem_mb",
    "per_cell_mem_mb",
]
