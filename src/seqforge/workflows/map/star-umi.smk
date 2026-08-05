# workflows/map/star-umi.smk  --  HAND-WRITTEN, VERSIONED, CI-TESTED. NEVER machine-generated.
#
# The pipeline for a PRE-DEMULTIPLEXED, one-cell-one-file library (SMART-seq3 and its relatives):
# demultiplexing happened at the bench, so the cell barcode is the FILE and not a read. The composer
# emits `config.yaml` + `units.tsv` and selects this module by id `map/star-umi`; it NEVER writes
# rule source.
#
# The chain is per cell `extract -> STAR -> one coordinate-sorted BAM`, and then ONCE
# `count(N BAMs) -> one combined .h5ad`. That fan-in is what makes this module a different shape from
# the three beside it, and it is DECLARED on the module (`fan_in_artifact`) rather than left to be
# discovered from the rule graph. Snakemake fans in natively and `rule all` already expands over
# samples, so a failed counting job re-runs only itself: every per-cell BAM is on disk.
#
# The read->role placement arrives via `config["read_files_in"]`, whose `umi_tagged` shape is
# `umi_cdna` and — only where the protocol was sequenced paired — `cdna`, chosen by ROLE. Role and
# not order, and that is this module's sharpest edge: the two reads are NOT symmetric — one opens
# with tag + UMI + motif — so handing the plain one to the extractor tags nothing and finishes with
# an empty matrix at exit 0.
#
# The mate is an ADDITION and never half of the operation (ADR-0035): the tag lives entirely within
# the tagged read, and the mate only inherits the resulting `UB` onto a record emitted alongside.
# Take it away and nothing about the extraction changes — only the uBAM's record count. The layout's
# ONE fact, whether it carries a mate, is stated in `units.tsv` and read from there by both sides:
# the extractor asks the table (ADR-0036), and the aligner's `SAM SE` / `SAM PE` is derived from the
# same per-sample mate list this module stages for it.
#
# Every knob the extraction needs arrives as ONE derived value, `config["umi"]["read_structure"]`,
# computed by compose from the element coordinates. This module's parse namespace is EMPTY: there is
# nothing here for a KB entry to declare, because the layout already states all of it.
#
# The genome index resolves at RUN TIME from a `liulab-genome` assembly id — no genome path is ever
# baked into a config or a manifest.

# seqforge's own helpers, imported rather than restated -- the same contract the other three modules
# state at greater length. `ordered_fastqs` decides the order every mate of one sample is handed over
# in, and all four modules must agree on it exactly; `load_units` is the one reader of the table, and
# `rule umi_extract`'s verb opens the same file through the same function (ADR-0036). `memory` is
# this module's map from the recipe's ONE memory figure to its TWO rule classes: a Snakefile is not
# importable, so arithmetic written here could never be unit-tested, only run. `PLATE_H5AD` is the
# name the module registry DECLARES as this pipeline's dataset-scoped deliverable, so the
# declaration and the rule that produces it cannot come apart.
from seqforge.workflows import PLATE_H5AD
from seqforge.workflows.h5ad import STAR_BAM
from seqforge.workflows.memory import PLATE_RETRIES, bam_sort_ram, fan_in_mem_mb, per_cell_mem_mb
from seqforge.workflows.units import load_units, ordered_fastqs

UNITS_TSV = config["units_tsv"]
UNITS = load_units(UNITS_TSV)
# One cell is one sample here, so this is the plate. `config["samples"]` is the contracted list and
# this is what units.tsv actually carries; they agree by construction because compose writes both.
SAMPLES = sorted({u["sample_id"] for u in UNITS})
OUTDIR = config["outdir"]
ASSEMBLY = config["genome"]["assembly"]
ANNOTATION = config["genome"]["annotation"]
UMI = config["umi"]
READ_FILES_IN = config["read_files_in"]

# The shared genome segment is loaded once and attached by every mapping job, so the rule that loads
# it needs a file to hang a dependency on. A flag rather than a directory: nothing reads its bytes,
# and what a mapping job actually depends on is that the load HAPPENED. It sits BESIDE the resolved
# index rather than inside it -- a file under another rule's directory output is a child of that
# output, which snakemake refuses to build a DAG for at all.
LOADED_FLAG = f"{OUTDIR}/index/{ASSEMBLY}.loaded"
# Where STAR writes the small logs its load and unload invocations produce. Beside the index for the
# same reason, and prefixed so they never collide with a cell's.
LOAD_PREFIX = f"{OUTDIR}/index/_genome_{ASSEMBLY}_"


def fastqs(sample, role):
    # `ordered_fastqs` owns the order and the argument for it; the other three mapping modules read
    # the same one. Here a cell is usually one run, but a cell topped up across two runs is exactly
    # the 10.5% of plate deposits that are not strictly 1:1 -- and those files are STAGED here and
    # resolved again by the extractor off this same table, which is one derivation used twice rather
    # than two (ADR-0036): the rule declares what the job depends on, the verb reads which file is
    # which. What is NOT done is rendering this list into the command line, where the pairing would
    # be two sorts assumed parallel and the arity would be unguarded by the wiring gate.
    return ordered_fastqs(UNITS, sample, role)


def tagged_role():
    """Which layout read carries the tag + UMI -- chosen by ROLE at compose, never by order here."""
    return READ_FILES_IN["umi_cdna"]


def mate_role():
    """The plain cDNA mate's role, or `None` where this layout carries only the tagged read.

    Read with `.get`, not a subscript, and the difference is the whole mechanism: `keys_read_by`
    scans this source to decide what compose owes every dataset composed against this module, so a
    subscript would oblige a single-end plate to emit a key its layout does not have -- and the
    params gate, which refuses a key no owner declares, would then refuse the very layout the
    optional mate exists to serve. A change that widens what a module ACCEPTS must not widen what it
    DEMANDS.

    `None` is an answer and not a gap. It used to raise, on the reading that pairing is the
    operation and one read a degenerate case of it; ADR-0035 calls that backwards -- the single-end
    form is the base case and the mate the addition -- so there is no refusal left here to place.
    The layout is also the only statement of the fact: a `paired:` key beside it would be the same
    thing said twice, and owed by every plate.
    """
    return READ_FILES_IN.get("cdna")


def mate_fastqs(sample):
    """This cell's mate FASTQs, or an EMPTY LIST where the layout has no mate.

    A declared input of NO files rather than a missing one -- the line star.smk draws for its second
    bulk mate, for the same reason: snakemake takes an empty list happily, while a name resolving to
    nothing still claims a mate is there.

    **This is the module's single statement of whether this cell has a mate, and BOTH branches read
    it** -- the `--r2` the extractor is handed, and the `--readFilesType` the aligner is given. It is
    per SAMPLE and not per dataset because that is the granularity a staged list has, and because the
    one state that pulls the two apart is per sample: a `cdna` role declared for the layout that
    stages no file for THIS cell. Rendering one branch off the role and the other off the files is
    what would let the extractor write an unpaired uBAM and the aligner still be told `SAM PE`.
    """
    role = mate_role()
    return [] if role is None else fastqs(sample, role)


def read_files_type(sample):
    """STAR's `--readFilesType`: `SAM PE` over interleaved pairs, `SAM SE` over one record a read.

    Derived per SAMPLE from `mate_fastqs`, which is the SAME list the `--r2` argument is rendered
    from, and that shared source is the whole point of the signature. Reading `mate_role()` here
    instead looks equivalent and is not: a `cdna` role that stages no file for this cell renders no
    `--r2`, so the extractor writes an unpaired uBAM while the aligner is still told `SAM PE`.
    Measured against STAR 2.7.11b in the `align-rna` image (2026-08-05): `FATAL ERROR in input BAM
    file: the consecutive lines in paired-end BAM have different read IDs`, **exit 104** -- a crash
    rather than a wrong number, which is what makes this derivation load-bearing instead of a tidier
    spelling of a literal. Two derivations of one fact is how a module comes to contradict itself for
    exactly one dataset shape, and the shape here is the one this module was just widened to run.
    """
    return "SAM PE" if mate_fastqs(sample) else "SAM SE"


rule all:
    input:
        # ONE object over every cell, and a CRAM per cell. The h5ad is the deliverable the fan-in
        # produces and it is demanded by NAME rather than as a directory: a rule whose output is a
        # folder is satisfied by a folder, which is how a counting job that wrote three cells of 1440
        # exits 0. Per-cell QC needs no target -- STAR writes `Log.final.out` into each cell's
        # directory unasked, and the report's reader finds it there.
        f"{OUTDIR}/{PLATE_H5AD}",
        expand(f"{OUTDIR}/{{sample}}/{{sample}}.cram", sample=SAMPLES),


rule genome_index:
    """Resolve the STAR index via liulab-genome at run time (never a path in the manifest).

    This rule only **looks up** the index; it never builds one. `get_star_index` returns the genomeDir
    liulab-genome already built for this assembly + annotation, and **raises if none exists** -- the
    index is liulab-genome's artifact, built ahead of the run by its own machinery, in its own
    environment. A machine with no prebuilt index fails loudly here ("build it first").

    A `run:` block, so it needs no tool on PATH and no `container:` -- snakemake wraps a container
    around a `shell:` command and never around a `run:` block.
    """
    output:
        directory(f"{OUTDIR}/index/{ASSEMBLY}"),
    params:
        assembly=ASSEMBLY,
        annotation=ANNOTATION,
    run:
        from pathlib import Path

        from genome import Genome

        index = Genome(params.assembly).get_star_index(gtf=params.annotation)
        out = Path(output[0])
        out.parent.mkdir(parents=True, exist_ok=True)
        out.symlink_to(index)


rule load_genome:
    """Load the genome index into SHARED memory once, and hand every mapping job something to attach to.

    **This rule exists because of the plate's arithmetic, not as a tuning preference.** STAR's memory
    is dominated by the index, which is per-process and independent of read count -- a 901-read cell
    and a 3.1M-read cell load exactly the same thing. At a ~30 GB index, 1440 per-cell loads is on
    the order of 40 TB of I/O to align 54 GB of FASTQ: the setup exceeds the work by nearly three
    orders of magnitude. For a dozen droplet samples that ratio is irrelevant, which is why the two
    shipped STAR modules do not do this; for a plate it is the whole cost.

    **`Remove` FIRST, defensively, and it is safe.** `shmctl(IPC_RMID)` *marks* a segment for
    destruction: a process already attached keeps running, and the memory goes when the last one
    detaches. It cannot yank memory out from under a concurrent job on the same index. That is worth
    saying because the line reads dangerous and is not -- and because a stale segment left by a
    killed run is otherwise inherited silently. `|| true` because removing a segment that is not
    there is a STAR error and a no-op, and this rule must not fail for having nothing to clean.

    The same idiom the two shipped mapping rules already open with (`rm -rf ..._STARtmp`), one level
    up: clear the stale thing you cannot otherwise reach, then do the work.

    **What it buys is one load per NODE.** On this rule's own node that is guaranteed; elsewhere
    `LoadAndKeep` gets there organically, since the first mapping job loads and the rest attach. What
    the explicit rule adds beyond that is the defensive removal, a loud failure point if the index
    cannot load at all, and serialization that removes the segment-creation race between STAR
    processes starting at the same instant.

    Two things to verify on a real cluster, which a dry run cannot: that the container runtime is not
    namespacing IPC (apptainer does not by default, but `--ipc` would silently break the sharing),
    and that a node receiving a mapping job without this one degrades to attach-or-load rather than
    failing.
    """
    input:
        index=rules.genome_index.output,
    output:
        # Not `temp()`: deleting the flag would tell snakemake the load never happened, and a rerun
        # would reload a segment that is already resident.
        touch(LOADED_FLAG),
    container: config["container"]
    threads: config["threads"]
    params:
        prefix=LOAD_PREFIX,
    shell:
        r"""
        # Defensive: marks any stale segment for destruction. Attached jobs keep running; a segment
        # that is not there is a no-op we must not fail on.
        STAR --genomeDir {input.index} --genomeLoad Remove \
             --outFileNamePrefix {params.prefix}remove_ > /dev/null 2>&1 || true
        STAR --genomeDir {input.index} --genomeLoad LoadAndExit \
             --outFileNamePrefix {params.prefix}
        """


rule umi_extract:
    """Lift one cell's UMI out of the tagged read and write the uBAM that carries it as `UB:Z:`.

    A `shell:` calling a seqforge verb rather than a `run:` block: `snakemake -n -p` renders every
    shell block while planning and cannot see inside a `run:`, so only a verb is visible to compose's
    wiring gate. No `container:` -- the extractor shells out to nothing at all.

    **The aligner refuses to be ASKED for a UMI** (`UB` in `--outSAMattributes` is rejected outside
    its single-cell mode), so the tag has to arrive on the way IN. That is what makes the uBAM the
    output format here rather than a trimmed FASTQ, and it is also what keeps the CRAM converter
    reusable unchanged: the UMI rides in a tag, so rewriting every QNAME destroys nothing.

    `--geometry` is one DERIVED value, and `--read-id` is the tagged role compose chose by role. The
    verb refuses if the two disagree, which turns a rule wired to the wrong mate into exit 3 instead
    of a uBAM with no UMI anywhere.

    **NO FILE IS NAMED ON THE COMMAND LINE** (ADR-0036). The table and the wildcard are rendered;
    the verb resolves this cell's tagged files and their mates from `units.tsv` through the same
    `ordered_fastqs` the inputs below are declared from. A cell topped up across two runs therefore
    runs, where a rendered `--r1 {input.tagged}` expanded a list after a one-value option and died
    with a usage error at job execution -- past the wiring gate, which formats a `shell:` block
    while planning and never runs one, so no arity or quoting fact in a rendered command is guarded.

    The FASTQs are still declared inputs: what snakemake stages and what the job depends on is this
    rule's to state, and the mate list is also what `read_files_type` reads. `units.tsv` joins them,
    because the command now opens it.
    """
    input:
        units=UNITS_TSV,
        tagged=lambda wc: fastqs(wc.sample, tagged_role()),
        mate=lambda wc: mate_fastqs(wc.sample),
    output:
        # Consumed by exactly one rule (the mapping below), so snakemake deletes it the moment that
        # job finishes -- 1440 uBAMs never coexist with 1440 alignments.
        ubam=temp(f"{OUTDIR}/{{sample}}/{{sample}}.unaligned.bam"),
    params:
        # The whole extraction geometry as one value, derived by compose from the element
        # coordinates: which read is tagged, the anchor and where it is declared, the UMI's offset
        # and width, the motif that closes the tag, and where cDNA begins. Nothing declares it.
        structure=UMI["read_structure"],
        read_id=lambda wc: tagged_role(),
    shell:
        r"""
        seqforge io umi-extract --units {input.units} --sample {wildcards.sample} \
             --geometry {params.structure} --read-id {params.read_id} \
             --out {output.ubam}
        """


rule star_umi_map:
    """Map ONE cell against the shared index, into one coordinate-sorted BAM.

    One cell per job. Batching cells into one STAR invocation was left off the table deliberately: it
    coarsens retry granularity, and no measurement demands it.

    **The input is a uBAM, and the flags that read it are the format.** `--readFilesType SAM ...`
    with `--readFilesCommand samtools view` is how STAR reads an alignment file as input, and
    `--readFilesSAMattrKeep All` pins a default the whole route depends on -- it is STAR's own
    default rather than an opt-in, and passing it says so rather than leaving the format resting on
    one unstated default. The `UB` tag then arrives in the output without ever being named in
    `--outSAMattributes`, which is the only way to get it there at all outside single-cell mode.

    Its `PE`/`SE` half is the one part of that format DERIVED per dataset rather than a module
    literal like the flags around it, and the uBAM is what it follows: two records a fragment or
    one, exactly as the layout had a mate or did not. Unpaired records read as `SAM PE` crash, so
    a stale literal here would be loud -- but only after the index loaded and the plate was queued.

    **ONE sort, and it is this one.** `--outSAMtype BAM SortedByCoordinate` serves both consumers:
    the CRAM converter and the counter each read this file. A second `samtools sort -n` to give the
    counter name adjacency would cost a full extra pass over every cell and a second BAM each --
    2x peak disk, since every BAM has to survive until the fan-in finishes. The cost lands on the
    counter, which pairs mates from the flags and mate coordinates instead, and it is paid once.

    `--limitBAMsortRAM` is REQUIRED here rather than merely wise: STAR's default of `0` means "reuse
    the genome allocation", which is only legal under `--genomeLoad NoSharedMemory`. Under
    `LoadAndKeep` there is no allocation to reuse, so the budget has to be stated -- and it is stated
    as a fraction of the memory THIS ATTEMPT was granted, so a retry raises the scheduler request and
    STAR's cap together instead of buying memory STAR is still forbidden to sort in.

    A `resources:` entry and not a `params:` one, because only a resource is re-evaluated per attempt:
    `Job.attempt`'s setter clears the resources and not the params, so a params callable is expanded
    once on attempt 1 and every retry reuses it verbatim.
    """
    input:
        ubam=rules.umi_extract.output.ubam,
        index=rules.genome_index.output,
        loaded=rules.load_genome.output,
    output:
        # Two consumers -- the CRAM converter and the fan-in counter -- so snakemake keeps it until
        # BOTH have finished and then deletes it. That is the 2x peak disk the single sort avoids
        # doubling again.
        bam=temp(f"{OUTDIR}/{{sample}}/{STAR_BAM}"),
    container: config["container"]
    threads: config["threads"]
    retries: PLATE_RETRIES
    resources:
        mem_mb=lambda wildcards, attempt: per_cell_mem_mb(config["mem_mb"], attempt),
        bam_sort_ram_bytes=lambda wildcards, attempt: bam_sort_ram(
            per_cell_mem_mb(config["mem_mb"], attempt)
        ),
    params:
        prefix=lambda wc: f"{OUTDIR}/{wc.sample}/",
        read_files_type=lambda wc: read_files_type(wc.sample),
    shell:
        # `--outSAMmultNmax 1` is a module literal for the same reason it is one in starsolo.smk: its
        # value varies with nothing. It writes only a top-scoring alignment, which is exactly the
        # record `seqforge io cram`'s `-F 0x100` would keep, so the sort stops paying for records the
        # next rule deletes. It is safe for the counts HERE because our counter reads `NH` directly
        # rather than inferring multimapping from bundle length -- `NH` still counts every locus.
        r"""
        # preemption-safe: STAR aborts a rerun if _STARtmp exists (undeclared, snakemake cannot remove it)
        rm -rf {params.prefix}_STARtmp
        STAR --runMode alignReads --genomeDir {input.index} --runThreadN {threads} \
             --genomeLoad LoadAndKeep \
             --readFilesIn {input.ubam} --readFilesType {params.read_files_type} \
             --readFilesCommand samtools view --readFilesSAMattrKeep All \
             --outFileNamePrefix {params.prefix} \
             --outSAMtype BAM SortedByCoordinate \
             --limitBAMsortRAM {resources.bam_sort_ram_bytes} \
             --outSAMmultNmax 1
        """


rule umi_to_cram:
    """Compact one cell's coordinate-sorted BAM into a CRAM, then let `temp()` drop the BAM.

    The converter is reused UNCHANGED, and that is the largest single thing carrying the UMI in a tag
    bought: its QNAME rewrite touches field 1 only and rebuilds the record tab-joined, so every tag
    survives and its -16.2% comes back. A UMI carried in the read name would have been destroyed by
    exactly that rewrite.

    It does not sort -- STAR did. `container:`, unlike the extractor and the counter, because this
    verb shells out to samtools, which comes from the pinned image and not from whatever the
    submitting shell happened to have.

    The retained CRAM is not a FASTQ substitute: the tagged mate lost its structural prefix before
    STAR ever saw it, because the tag, the UMI and the motif are not genomic. The same property the
    droplet chain already records.
    """
    input:
        bam=rules.star_umi_map.output.bam,
    output:
        cram=f"{OUTDIR}/{{sample}}/{{sample}}.cram",
        crai=f"{OUTDIR}/{{sample}}/{{sample}}.cram.crai",
    container: config["container"]
    threads: config["threads"]
    params:
        assembly=ASSEMBLY,
    shell:
        r"""
        seqforge io cram --bam {input.bam} --assembly {params.assembly} \
             --out {output.cram} --threads {threads}
        """


rule umi_count:
    """THE FAN-IN: count every cell of the plate into ONE .h5ad. One job, not one per cell.

    This is the rule the module's `fan_in_artifact` declaration names, and the reason this module is
    a different shape from the three beside it. A per-cell counter followed by a merge would produce
    1440 objects to join back together on a label read off a filename -- which is the trap the
    reference tool had to warn about, and which does not exist here because each cell's `sample_id`
    travels WITH its BAM on the command line.

    It writes the object directly, with no table in between: 1440 cells x ~55 000 genes x 4 matrices
    is ~630 MB of dense text for a sparse object several times smaller, and a format written solely
    to be read back one rule later is a seam with no interface.

    A failed counting job re-runs only itself. Every per-cell BAM is on disk, which is what made the
    fan-in affordable in the first place.

    No `container:`: counting is not aligning, and pysam, gffutils and anndata are plain
    dependencies of this package. Only STAR needs an environment we do not own.

    Its memory request is the module's own arithmetic over the recipe's ONE figure, not the same
    request the mapping jobs make: this rule loads no genome index at all, and the recipe's figure
    was sized against one that does.
    """
    input:
        bams=expand(rules.star_umi_map.output.bam, sample=SAMPLES),
    output:
        h5ad=f"{OUTDIR}/{PLATE_H5AD}",
    threads: config["threads"]
    retries: PLATE_RETRIES
    resources:
        mem_mb=lambda wildcards, attempt: fan_in_mem_mb(config["mem_mb"], attempt),
    params:
        assembly=ASSEMBLY,
        annotation=ANNOTATION,
        # `sample_id=path` per cell, in the object's row order. The ids come from units.tsv, which is
        # seqforge's own grouping, and the paths come from `input.bams` rather than being rebuilt --
        # a second spelling of a path is the copy that goes stale, and here it would point the
        # counter at cells that are not there while the ids still look right. `strict=True` because
        # the failure of a silent zip is a SHORTER plate: cells dropped from the object with every
        # remaining row still correctly labelled, which no reader could notice.
        cells=lambda wc, input: " ".join(
            f"{sample}={bam}" for sample, bam in zip(SAMPLES, input.bams, strict=True)
        ),
    shell:
        r"""
        seqforge io umi-count {params.cells} \
             --assembly {params.assembly} --annotation {params.annotation} \
             --out {output.h5ad}
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
    copies is two chances to fix one of them: the prefix, the redirect and the trailing `|| true`
    have to stay together, and the copy that lost the `|| true` would turn a finished plate into a
    failed run.
    """
    shell(
        f"STAR --genomeDir {OUTDIR}/index/{ASSEMBLY} --genomeLoad Remove "
        f"--outFileNamePrefix {LOAD_PREFIX}unload_ > /dev/null 2>&1 || true"
    )


onsuccess:
    release_genome_segment()


onerror:
    # The same release on the failing path, and it is the one that matters more: a run that died
    # mid-plate is exactly the run that leaves a ~30 GB segment resident on a node with nothing left
    # to detach from it.
    release_genome_segment()
