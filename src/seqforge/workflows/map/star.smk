# workflows/map/star.smk  --  HAND-WRITTEN, VERSIONED, CI-TESTED. NEVER machine-generated.
#
# Plain STAR mapping for bulk RNA-seq (no cell barcode / UMI demultiplex), single- or paired-end.
# Selected by the composer's module id `map/star`; gene counts come from STAR's own
# `--quantMode GeneCounts`. The genome index is LOOKED UP from a `liulab-genome` assembly id while
# the DAG is built, and no rule owns it.
#
# The read->role placement arrives via `config["read_files_in"]`, whose `mates` shape is 1..2
# biological mates chosen by ORDER — `mate1`, and `mate2` only when the library has a second one
# (ADR-0029). One module serves both, because the layout kind is a property of the MODULE and STAR
# reads a single-end library as the same `--readFilesIn` with one argument instead of two.

import csv
from functools import cache

# seqforge's own helpers, imported rather than restated — the same contract starsolo.smk and
# chromap.smk state at greater length. `ordered_fastqs` decides the order every mate of one sample is
# handed to the aligner in, and all three modules must agree on it exactly: a Snakefile is not
# importable, so three copies of that rule could only ever be checked by running three pipelines.
# `memory` is that same move applied to what this module asks the scheduler for and what it lets STAR
# sort in: two numbers that only mean anything together, so a constant and a closure written here
# could never be unit-tested, only run against a sample deep enough to fail. The STAR filenames this
# rule declares arrive the same way: `h5ad` owns every name STAR writes into a sample's directory,
# and a fourth spelling of one here is the drift that has a rule declaring a file nothing produces.
#
# `splice_args` is the same move applied to STAR's junction flags: they vary with nothing, they are
# identical in all four STAR modules, and a flag list spelled once per module is the drift the
# STARsolo argv owner was created to end. It renders as ONE params slot below.
from seqforge.workflows.h5ad import STAR_JUNCTIONS, STAR_PROGRESS_LOGS
from seqforge.workflows.memory import BULK_RETRIES, bam_sort_ram, bulk_mem_mb, index_mem_mb
from seqforge.workflows.splice_args import splice_shell_args
from seqforge.workflows.units import ordered_fastqs


def _load_units(path):
    with open(path, newline="") as fh:
        return list(csv.DictReader(fh, delimiter="\t"))


UNITS = _load_units(config["units_tsv"])
SAMPLES = sorted({u["sample_id"] for u in UNITS})
OUTDIR = config["outdir"]
GENOME = config["genome"]
ASSEMBLY = GENOME["assembly"]
# The longest gap STAR may open on this assembly, recorded in the recipe by the processing policy off
# liulab-genome's shipped table. `.get`, not a subscript, and that is the whole mechanism by which an
# assembly the lab has not characterised emits no flag instead of refusing to compose: the scanner
# that derives `required_config` counts a subscript and not a `.get`, so a subscript here would make
# the composer owe this key for every dataset in the corpus.
INTRON_MAX = GENOME.get("intron_length_cap")
READ_FILES_IN = config["read_files_in"]

# The shared genome segment is loaded once and attached by every mapping job, so the rule that loads
# it needs a file to hang a dependency on. A flag rather than a directory: nothing reads its bytes,
# and what a mapping job actually depends on is that the load HAPPENED. It is the ONE output in this
# module no sample accounts for, and it is measured safe for concurrent instances over one results
# directory to share -- six at once, five trials, zero errors -- because re-touching a flag that is
# already there says exactly what the flag says.
LOADED_FLAG = f"{OUTDIR}/index/{ASSEMBLY}.loaded"


def fastqs(sample, role):
    # `ordered_fastqs` owns the order and the argument for it; the two other mapping modules read the
    # same one. Bulk mates are symmetric, so a mispairing here does not even desync the record lengths
    # -- STAR maps mate1 of lane 1 against mate2 of lane 2 and reports a plausible, wrong rate.
    return ordered_fastqs(UNITS, sample, role)


def mates():
    """The mates this library has, in ORDER -- ``[mate1]`` or ``[mate1, mate2]``.

    The `mates` layout kind is 1..2 reads (ADR-0029), so the composer emits `mate2` only for a library
    that HAS a second biological mate. `mate1` is read with a subscript and `mate2` with `.get`, and
    that difference is the whole mechanism: `keys_read_by` scans this source to decide what compose
    owes every dataset composed against this module, so a subscript here would oblige a single-end
    library to emit a key it does not have -- and the params gate, which forbids a key no owner
    declares, would then refuse the very layout this module was widened to run. Exactly the line
    starsolo.smk draws between `soloBarcodeReadLength` (some chemistries) and `soloCBmatchWLtype`
    (all of them)."""
    second = READ_FILES_IN.get("mate2")
    return [READ_FILES_IN["mate1"], *([] if second is None else [second])]


def readfilesin(sample, *roles):
    """Render STAR ``--readFilesIn`` for one sample: each role (a mate) is its FASTQs **comma-joined**,
    and the mates are space-separated -- ``mate1_run1,mate1_run2 mate2_run1,mate2_run2``.

    A sample pooled across N sequencing runs (or across one run's lanes) passes every such file for a
    mate as one comma-list, in the single order ``fastqs`` imposes on both mates alike. This is STAR's
    multi-file syntax; joining with spaces instead makes STAR read the extra files as extra mates and
    crash. A single-run sample renders one file per mate, so this generalises to any run count.

    Passing ONE role renders one comma-list and no space, which is STAR's single-end form -- the
    single-end case needs no branch here, only a caller that hands it the mates the layout has."""
    return " ".join(",".join(fastqs(sample, role)) for role in roles)


@cache
def star_index():
    """The prebuilt STAR genomeDir for this run, LOOKED UP through liulab-genome. Never built here.

    `get_star_index` returns the genomeDir liulab-genome already built for this assembly +
    annotation, and **raises if none exists** -- the index is liulab-genome's artifact, built ahead
    of the run by its own machinery, in its own environment. A machine with no prebuilt index is
    refused while the DAG is being built, before any job starts, and the refusal names the assembly.
    That is the failure mode we want: the pipeline consumes the index, it does not decide when or
    how it is built.

    **A function the rules call from a `params:` callable, and deliberately not a rule.** Resolving
    this into `{OUTDIR}/index/{ASSEMBLY}` gave one rule a path every concurrent snakemake instance
    over one results directory shares, and snakemake DELETES an output before running the job that
    makes it -- so six instances started at once produced zero successful runs, and an atomic,
    idempotent rule body still produced zero: the window is not the rule's to close. Declaring
    nothing leaves nothing to delete and nothing to race, and the refusal above moves earlier
    rather than being lost. What goes with it is a symlink under `results/`, which costs no
    provenance: the assembly and the annotation are in `config.yaml`, which is where the truth lives.

    `cache`d because a `params:` callable is expanded once per JOB: a plate would otherwise reopen
    the same genome once per cell while the DAG is being built.
    """
    from genome import Genome

    return Genome(ASSEMBLY).get_star_index(gtf=GENOME["annotation"])


rule all:
    input:
        expand(f"{OUTDIR}/{{sample}}/ReadsPerGene.out.tab", sample=SAMPLES),


rule load_genome:
    """Load the genome index into SHARED memory once, and hand every mapping job something to attach to.

    **This rule exists because of concurrency arithmetic, not as a tuning preference.** STAR's index
    is per-process and resident for the life of the job, so N samples mapping at once on one machine
    cost N copies of it: six samples against a ~25-31 GB human index is ~150-186 GB of index where
    ~31 GB would do. A composed pipeline runs on ONE machine (ADR-0051), which is what makes one
    segment attachable by every job at all -- and it is also what makes the multiplication real,
    since those jobs are concurrent by construction rather than spread out by a scheduler.

    `map/star-umi` reached this same rule from the other direction, and the two arguments are worth
    keeping apart: a plate re-LOADS the index once per cell, which is I/O, while a bulk run holds
    several copies at once, which is footprint. Either one on its own is a reason to share.

    **`Remove` FIRST, defensively, and it is safe.** `shmctl(IPC_RMID)` *marks* a segment for
    destruction: a process already attached keeps running, and the memory goes when the last one
    detaches. It cannot yank memory out from under a concurrent job on the same index. That is worth
    saying because the line reads dangerous and is not -- and because a stale segment left by a
    killed run is otherwise inherited silently. `|| true` because removing a segment that is not
    there is a STAR error and a no-op, and this rule must not fail for having nothing to clean.

    The same idiom `star_count` already opens with (`rm -rf ..._STARtmp`), one level up: clear the
    stale thing you cannot otherwise reach, then do the work.

    **Neither invocation writes into the pipeline directory.** STAR drops a log, a progress log and a
    `_STARtmp/` under whatever prefix it is handed and removes none of them, so these two used to
    leave nine undeclared entries beside the index and the flag, with nothing saying which two of
    the eleven were output. The prefix is therefore a directory this block MAKES and destroys.
    Pointing it somewhere else inside the run directory, or sweeping afterwards by glob, both work
    only for as long as they stay configured correctly; a scratch that cannot outlive the shell has
    nothing to configure and nothing to sweep. The `trap` is what covers the failing path, which is
    the path that leaves the most behind.

    **Nothing verifies separately that the sharing happened, and that is deliberate.** If the index
    cannot be loaded STAR exits non-zero, this rule fails and snakemake stops -- which covers a bad
    index path and a kernel refusing the segment. A container namespacing IPC (apptainer does not by
    default, but `--ipc` would) makes every job load privately with no error anywhere; the cost there
    is speed rather than correctness, and the setting belongs to whoever submits the pipeline.
    """
    output:
        # Not `temp()`: deleting the flag would tell snakemake the load never happened, and a rerun
        # would reload a segment that is already resident.
        touch(LOADED_FLAG),
    container: config["container"]
    threads: config["threads"]
    resources:
        # The one rule that holds the genome segment, so a scheduler told nothing about it packs jobs
        # beside the largest allocation on the machine without knowing it is there. The recipe's whole
        # figure, which is an upper bound on this residency -- see `index_mem_mb`.
        mem_mb=lambda wildcards, attempt: index_mem_mb(config["mem_mb"], attempt),
    params:
        # A `params:` and not an `input:`, because the genomeDir is liulab-genome's artifact and not
        # this DAG's -- see `star_index`, which is also where the concurrency argument lives.
        index=lambda wildcards: star_index(),
    shell:
        r"""
        # STAR's run-files go to a directory made here and destroyed here, so none of them can reach
        # the pipeline directory. The trap fires on the failing path too.
        scratch=$(mktemp -d)
        trap 'rm -rf "$scratch"' EXIT
        # Defensive: marks any stale segment for destruction. Attached jobs keep running; a segment
        # that is not there is a no-op we must not fail on.
        STAR --genomeDir {params.index} --genomeLoad Remove \
             --outFileNamePrefix "$scratch"/remove_ > /dev/null 2>&1 || true
        STAR --genomeDir {params.index} --genomeLoad LoadAndExit \
             --outFileNamePrefix "$scratch"/
        """


rule star_count:
    """Map one bulk sample's mates -- one of them or two -- to per-gene counts (STAR GeneCounts).

    The shell block clears STAR's `_STARtmp` before invoking STAR, so every (re)run is
    preemption-safe: a preempted STAR leaves `results/<sample>/_STARtmp` behind, STAR ABORTS a rerun
    if it already exists, and snakemake cannot remove it (an undeclared output).

    **What this job asks for, and how much of it the sort may claim.** The rule declared NEITHER
    until #377: no `mem_mb`, so the run's largest single allocation was invisible to whatever packs
    the machine, and no `--limitBAMsortRAM`, so a coordinate sort ran on STAR's default of `0` --
    "reuse the genome allocation", which is a budget nobody chose and which tracks the genome's size
    rather than the sample's depth. It is also the one value STAR refuses outright under
    `--genomeLoad LoadAndKeep`, which this rule now passes -- there is no genome allocation of its
    own left to reuse -- and that refusal fires before the genome directory is read, so it would be
    every sample on the first attempt rather than a slow sample now and then. `--limitBAMsortRAM` is
    therefore REQUIRED here rather than merely wise, and the two changes had to land in this order.

    **The index is ATTACHED, not loaded.** `load_genome` put one copy in shared memory and this rule
    depends on its flag, so N concurrent samples on the machine hold one index between them rather
    than one apiece. The memory request does not shrink to match: it covers a job that is alone on
    the machine (ADR-0051), which is what it would be the first time one sample ran by itself.

    Both numbers follow `attempt`, the arrangement `starsolo_count` already has, and the reasoning is
    NOT the same reasoning. STARsolo escalates against `readInfo`, an allocation that grows with
    every input read and that no `--limit*` flag bounds; bulk counts genes and demultiplexes nothing,
    so it holds no such array. What a retry buys HERE is a deeper sample's coordinate sort against a
    budget one multiple of the figure larger -- depth alone. The count is `BULK_RETRIES` and not
    STARsolo's for exactly that reason: one workflow's headroom may not be a function of the other's.
    The arithmetic and the measurements behind it live in `workflows/memory.py`.
    """
    input:
        mate1=lambda wc: fastqs(wc.sample, mates()[0]),
        # EMPTY for a single-end library, which is a declared input of no files rather than a missing
        # one: snakemake takes an empty list happily, while a `mate2` naming a role no unit carries
        # would resolve to nothing under a name that claims something.
        mate2=lambda wc: [f for role in mates()[1:] for f in fastqs(wc.sample, role)],
        loaded=rules.load_genome.output,
    output:
        # Named one by one, and the names are the claim. This rule declared the counts alone, so a
        # sample's directory ended a run holding several files STAR wrote with nothing saying which
        # of them a reader was meant to keep. The junction table is one: a junction call is
        # analyzable at bulk depth, unlike a single plate cell's, so it is a real output here and a
        # summary line elsewhere. The two progress logs are not -- nothing reads either once the run
        # is over -- so `temp()` drops them as soon as this job finishes and no manual `rm` is
        # involved. The aligner's end-of-run summary is deliberately in neither list: it survives,
        # and the report reads it off disk with no rule of ours in between (`workflows/stats.py`).
        counts=f"{OUTDIR}/{{sample}}/ReadsPerGene.out.tab",
        junctions=f"{OUTDIR}/{{sample}}/{STAR_JUNCTIONS}",
        progress=temp(expand(f"{OUTDIR}/{{{{sample}}}}/{{f}}", f=list(STAR_PROGRESS_LOGS))),
    # liulab-runtime's `align-rna`, resolved by compose. See starsolo.smk's note: consuming their
    # artifact, not defining an env, and honoured only under `--software-deployment-method`.
    container: config["container"]
    threads: config["threads"]
    # `retries:` and `resources:` are ONE mechanism, so they are read together. `config["mem_mb"]`
    # appears as a literal subscript deliberately -- `workflows/__init__.py::keys_read_by` SCANS this
    # source to compute `required_config`, and a key the scanner cannot see is a key the composer is
    # not obliged to emit, i.e. a KeyError on a compute node long after compose exited 0.
    #
    # THE SORT CAP IS A `resources:` ENTRY, NOT A `params:` ONE. That is the only construct snakemake
    # re-evaluates per attempt: `Job.attempt`'s setter clears `self._resources` and NOT `self._params`
    # (measured on the pinned 9.23.1), so a `params:` callable is expanded once, on attempt 1, and
    # every retry reuses it verbatim -- the request escalates and the cap stays where the first
    # attempt died, which is worse than not retrying at all. The name carries its unit because it is
    # the one number here that is not MiB: STAR takes `--limitBAMsortRAM` in bytes, and a resource is
    # a bare integer with nowhere else to say so.
    retries: BULK_RETRIES
    resources:
        mem_mb=lambda wildcards, attempt: bulk_mem_mb(config["mem_mb"], attempt),
        bam_sort_ram_bytes=lambda wildcards, attempt: bam_sort_ram(
            bulk_mem_mb(config["mem_mb"], attempt)
        ),
    params:
        bulk=config["bulk"],
        # The genomeDir this job attaches to, looked up rather than declared -- see `star_index`.
        index=lambda wc: star_index(),
        prefix=lambda wc: f"{OUTDIR}/{wc.sample}/",
        # each mate is its runs comma-joined, so a sample pooled across runs maps in one pass; only
        # the mates the layout HAS are passed, so a single-end library renders STAR's single-end form.
        reads=lambda wc: readfilesin(wc.sample, *mates()),
        # STAR's junction flags, rendered by their one owner and interpolated whole. The filters vary
        # with nothing, so they are module literals rather than anything's to choose; the length
        # bound varies with the assembly, so it arrives as the argument below and renders as nothing
        # at all where the recipe carries none.
        splice=splice_shell_args(intron_max=INTRON_MAX),
    shell:
        r"""
        # preemption-safe: STAR aborts a rerun if _STARtmp exists (undeclared, snakemake cannot remove it)
        rm -rf {params.prefix}_STARtmp
        STAR --runMode alignReads --genomeDir {params.index} --runThreadN {threads} \
             --genomeLoad LoadAndKeep \
             --readFilesIn {params.reads} --readFilesCommand zcat \
             --quantMode {params.bulk[quantMode]} \
             --outFileNamePrefix {params.prefix} \
             --outSAMtype BAM SortedByCoordinate \
             --limitBAMsortRAM {resources.bam_sort_ram_bytes} \
             {params.splice}
        """


def release_genome_segment():
    """Mark the shared genome segment for destruction. ONE command, called by both end-of-run paths.

    Cleanup is OURS, with the site as the backstop. A well-configured scheduler reclaims a killed
    user's IPC segments, but that is site policy rather than SysV semantics -- a segment otherwise
    persists until reboot, which is why STAR ships a `Remove` mode at all -- and seqforge emits a
    Snakefile the USER submits, possibly somewhere else entirely. Calling this from both handlers
    makes the guarantee ours rather than the scheduler's.

    Honest about its reach: a handler runs in the environment that ran snakemake, outside the
    container that has STAR, and it cannot run at all after a SIGKILL. The defensive `Remove` at the
    head of `load_genome` is the half that always holds -- it runs inside the image, and it covers
    exactly the case a handler cannot. Belt and braces, the same shape as clearing `_STARtmp` on
    entry and declaring the outputs that replace it.

    Written once because the two paths release the SAME segment the same way, and two byte-identical
    copies is two chances to fix one of them: the scratch, the redirect and the trailing `|| true`
    have to stay together, and the copy that lost the `|| true` would turn a finished run into a
    failed one. The scratch is the arrangement `load_genome` argues for, on the path where it matters
    most: this command runs when the run is OVER, so a run-file left under the output prefix would
    land in a directory a user is already reading.

    The segment is named by the SAME lookup every rule makes -- `star_index` -- and not by a path
    under `results/`, which is where this used to point and which no longer exists. It is a cached
    call by the time a handler runs, so the release costs no second genome open.
    """
    shell(
        "scratch=$(mktemp -d); trap 'rm -rf \"$scratch\"' EXIT; "
        f"STAR --genomeDir {star_index()} --genomeLoad Remove "
        '--outFileNamePrefix "$scratch"/unload_ > /dev/null 2>&1 || true'
    )


onsuccess:
    release_genome_segment()


onerror:
    # The same release on the failing path, and it is the one that matters more: a run that died
    # partway through is exactly the run that leaves a ~30 GB segment resident on a machine with
    # nothing left to detach from it.
    release_genome_segment()
