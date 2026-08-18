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
# declaration and the rule that produces it cannot come apart. `EXTRACT_SUFFIX` is the same contract
# for the per-cell extraction summary: the extractor writes it, the report finds it, and this rule
# declares it -- one owner, imported three times, never spelled twice.
from seqforge.workflows import PLATE_H5AD
from seqforge.workflows.h5ad import STAR_BAM
from seqforge.workflows.memory import (
    PLATE_RETRIES,
    bam_sort_ram,
    fan_in_mem_mb,
    index_mem_mb,
    per_cell_mem_mb,
)
from seqforge.workflows.umite.extract import EXTRACT_SUFFIX
from seqforge.workflows.units import load_units, ordered_fastqs
from seqforge.workflows.units import mate_role as units_mate_role

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

# THE RETAINED ARCHIVE, PARTITIONED BY MAPPABILITY: one cell's uniquely-placed records and one cell's
# multiply-placed ones, as two files rather than one mixed one. Together they are exactly every
# primary mapped record, so nothing is lost against the single archive they replace and no record is
# in both -- the total on disk is what one mixed archive cost. The population a user trusts is the
# one they open by default, and the ambiguous one is a file to open rather than a filter to compose
# from memory. Spelled ONCE each, here, because `rule all` demands them and a rule writes them, and a
# second spelling is the copy that names a file nobody produces while still looking right. The
# chimeric twin partitions the same way, so the same cell processed both ways holds the same
# populations in the same places.
UNIQUE_CRAM = f"{OUTDIR}/{{sample}}/{{sample}}.unique.cram"
MULTIPLACED_CRAM = f"{OUTDIR}/{{sample}}/{{sample}}.multiplaced.cram"


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
    """This cell's mate FASTQs, or an EMPTY LIST where the layout has no mate. CHECKED, not assumed.

    A declared input of NO files rather than a missing one -- the line star.smk draws for its second
    bulk mate, for the same reason: snakemake takes an empty list happily, while a name resolving to
    nothing still claims a mate is there.

    **This is the module's single statement of whether this cell has a mate, and every flag whose
    shape depends on that reaches it through `mate_count`** -- the extractor is handed units.tsv and
    resolves its own (ADR-0036). It is per SAMPLE and not per dataset because that is the granularity
    a staged list has, and because the one state that pulls the branches apart is per sample: a
    `cdna` role declared for the layout that stages no file for THIS cell.

    **So the extractor's mate and this one are two derivations of one fact, and the disagreement is
    REFUSED here rather than left to absence.** This module reads `read_files_in["cdna"]`, which is
    compose's ROLE-checked answer (the non-tagged read carrying a cDNA or gDNA element); the verb
    reads units.tsv, where a role is a column and its elements are not. Those agree on every layout
    compose emits today and part company on one that a future KB entry could reach: a second
    non-index read the layout does NOT call cDNA. There the module stages nothing and renders
    `SAM SE` while the verb finds one non-tagged role and writes an interleaved PAIRED uBAM -- which
    STAR then reads one record at a time and counts twice, at exit 0. That is a wrong matrix and not
    a crash, so it is the half of the divergence nothing downstream would notice.

    `units.mate_role` is the verb's OWN derivation, called here to be compared against this one; a
    raise inside an input function is an `InputFunctionException` at DAG construction, which is what
    compose's wiring gate turns into a refusal before anything is submitted. It also raises for a
    sample carrying two non-tagged roles, which is the same refusal the verb would reach at job time,
    moved to where it is still cheap.
    """
    role = mate_role()
    staged = [] if role is None else fastqs(sample, role)
    resolved = units_mate_role(UNITS, sample, tagged_role())
    if resolved != (role if staged else None):
        raise ValueError(
            f"cell {sample} stages "
            f"{'no mate' if not staged else f'{len(staged)} {role} file(s)'} for the extractor, "
            f"while units.tsv offers it {resolved!r} as the read beside {tagged_role()}. The "
            f"aligner is told `SAM PE`/`SAM SE` from what is staged and the extractor pairs from "
            f"the table, so these must be the same answer: a mismatch writes a uBAM whose shape the "
            f"aligner flag does not describe, and STAR miscounts it rather than refusing it"
        )
    return staged


def mate_count(sample):
    """How many mates this cell's uBAM carries: 2 or 1.

    The one number every per-mate flag below is rendered from. STAR takes several of those, each
    fatal at the wrong arity, and they must agree with each other and with the records the extractor
    actually wrote -- so they share a derivation rather than each asking the layout again.
    """
    return 2 if mate_fastqs(sample) else 1


def read_through_clip(sample):
    """The chemistry's read-through, as the flags STAR takes -- or NOTHING where it declares none.

    A tagmented library cuts at random, so a fragment shorter than the read runs off the end of its
    own cDNA and into the adapter -- and the cost of leaving that in place is a DENOMINATOR, not a
    mapping failure. `outFilterScoreMinOverLread`/`outFilterMatchNminOverLread` are 0.66 OF THE READ
    LENGTH, and a clipped base leaves that length where a soft-clipped one does not, so a read half
    of which is adapter cannot clear 66% of itself however cleanly its genomic half aligns: STAR
    places it and then discards it as `unmapped: too short`.

    **Per MATE, and STAR counts.** Measured against 2.7.11b at parameter init: `--clip3pAdapterSeq`
    must carry one value per mate, and `--clip3pAdapterMMp` must match its arity or the run is
    refused outright. So the arity comes off `mate_count` -- the same fact `--readFilesType` renders
    -- because this module's mate count is per SAMPLE, and a flag rendered once for the whole run
    would be fatal on every cell of the other kind. Table: `docs/research/smartseq3-tn5-read-through.md`.

    `--clip3pAdapterMMp 0.1` is STAR's own default, restated at the arity the paired form demands. It
    varies with nothing and so is a module literal rather than a chemistry's to choose.

    Read with `.get`, not a subscript, for the reason `mate_role` states: a subscript would oblige
    every plate chemistry to name an adapter, and the params gate would then refuse the ones that
    have none. Absence renders as ABSENCE -- an empty flag would match every read.

    Every record it reaches is cDNA, and that is compose's doing rather than this rule's: the uBAM
    holds the tagged read's cDNA span and the mate compose placed by the `cdna` ROLE, which the
    params gate re-checks. Nothing is clipped before EXTRACTION either -- the tag and UMI occupy the
    first bases of the tagged read, so anything trimming it earlier destroys the UMI, and this flag
    rides the aligner, which reads the uBAM the extractor already wrote.
    """
    sequence = UMI.get("read_through")
    if not sequence:
        return ""
    per_mate = lambda value: " ".join([value] * mate_count(sample))
    return f"--clip3pAdapterSeq {per_mate(sequence)} --clip3pAdapterMMp {per_mate('0.1')}"


def read_files_type(sample):
    """STAR's `--readFilesType`: `SAM PE` over interleaved pairs, `SAM SE` over one record a read.

    Derived per SAMPLE from `mate_fastqs`, which is the same list the extractor will resolve for
    itself out of units.tsv -- checked equal there rather than assumed -- and that shared source is
    the whole point of the signature. Reading `mate_role()` here instead looks equivalent and is
    not: a `cdna` role that stages no file for this cell leaves the extractor with no mate to find,
    so it writes an unpaired uBAM while the aligner is still told `SAM PE`. Measured against STAR
    2.7.11b in the `align-rna` image (2026-08-05): `FATAL ERROR in input BAM file: the consecutive
    lines in paired-end BAM have different read IDs`, **exit 104** -- a crash rather than a wrong
    number, which is what makes this derivation load-bearing instead of a tidier spelling of a
    literal. Two derivations of one fact is how a module comes to contradict itself for exactly one
    dataset shape, and the shape here is the one this module was widened to run.
    """
    return "SAM PE" if mate_count(sample) == 2 else "SAM SE"


rule all:
    input:
        # ONE object over every cell, and BOTH HALVES of that cell's archive. The h5ad is the
        # deliverable the fan-in produces and it is demanded by NAME rather than as a directory: a
        # rule whose output is a folder is satisfied by a folder, which is how a counting job that
        # wrote three cells of 1440 exits 0. The two archives are demanded by name for the same
        # reason one arity down -- neither is anyone's input, so a half that stopped being produced
        # would simply stop appearing. Per-cell QC needs no target -- STAR writes `Log.final.out`
        # into each cell's directory unasked, and the report's reader finds it there.
        f"{OUTDIR}/{PLATE_H5AD}",
        expand(UNIQUE_CRAM, sample=SAMPLES),
        expand(MULTIPLACED_CRAM, sample=SAMPLES),


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
    orders of magnitude.

    That is an argument about REPEATED LOADING, and it is not the only reason to share a copy. It
    used to end "for a dozen droplet samples that ratio is irrelevant, which is why the two shipped
    STAR modules do not do this" -- true about I/O and silent about the other half. The other half is
    CONCURRENT RESIDENCY: samples mapping at the same instant each hold an index, so six droplet
    samples against a ~31 GB human index cost ~186 GB where ~31 GB would do, however few times each
    one loaded it. `map/star` and `map/starsolo` carry this same rule for that reason (#379). One
    workflow's case is I/O and the others' is footprint; a plate happens to have both.

    **`Remove` FIRST, defensively, and it is safe.** `shmctl(IPC_RMID)` *marks* a segment for
    destruction: a process already attached keeps running, and the memory goes when the last one
    detaches. It cannot yank memory out from under a concurrent job on the same index. That is worth
    saying because the line reads dangerous and is not -- and because a stale segment left by a
    killed run is otherwise inherited silently. `|| true` because removing a segment that is not
    there is a STAR error and a no-op, and this rule must not fail for having nothing to clean.

    The same idiom the two shipped mapping rules already open with (`rm -rf ..._STARtmp`), one level
    up: clear the stale thing you cannot otherwise reach, then do the work.

    **Neither invocation writes into the pipeline directory.** STAR drops a log, a progress log and a
    `_STARtmp/` under whatever prefix it is handed and removes none of them, so these two used to
    leave nine undeclared entries beside the index and the flag, with nothing saying which two of
    the eleven were output. The prefix is therefore a directory this block MAKES and destroys.
    Pointing it somewhere else inside the run directory, or sweeping afterwards by glob, both work
    only for as long as they stay configured correctly; a scratch that cannot outlive the shell has
    nothing to configure and nothing to sweep. The `trap` is what covers the failing path, which is
    the path that leaves the most behind.

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
    resources:
        # The one rule that holds the genome segment, and it asked for nothing until now -- so a
        # scheduler packed jobs beside the largest allocation on the node without knowing it existed.
        mem_mb=lambda wildcards, attempt: index_mem_mb(config["mem_mb"], attempt),
    shell:
        r"""
        # STAR's run-files go to a directory made here and destroyed here, so none of them can reach
        # the pipeline directory. The trap fires on the failing path too.
        scratch=$(mktemp -d)
        trap 'rm -rf "$scratch"' EXIT
        # Defensive: marks any stale segment for destruction. Attached jobs keep running; a segment
        # that is not there is a no-op we must not fail on.
        STAR --genomeDir {input.index} --genomeLoad Remove \
             --outFileNamePrefix "$scratch"/remove_ > /dev/null 2>&1 || true
        STAR --genomeDir {input.index} --genomeLoad LoadAndExit \
             --outFileNamePrefix "$scratch"/
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

    **Two outputs, and only one of them is reclaimed.** The uBAM is consumed and deleted; the summary
    beside it is the durable account of what the extraction saw, and it is declared here rather than
    derived by the verb so that one path is stated once and snakemake owns removing it after a failed
    job. Nothing demands it in `rule all` and nothing needs to: this rule is upstream of every cell's
    archive, so a plate that finishes has written one per cell.
    """
    input:
        units=UNITS_TSV,
        tagged=lambda wc: fastqs(wc.sample, tagged_role()),
        mate=lambda wc: mate_fastqs(wc.sample),
    output:
        # Consumed by exactly one rule (the mapping below), so snakemake deletes it the moment that
        # job finishes -- 1440 uBAMs never coexist with 1440 alignments.
        ubam=temp(f"{OUTDIR}/{{sample}}/{{sample}}.unaligned.bam"),
        # And NOT `temp()`, which is the whole point of it: what the extraction measured has to
        # outlive the records it measured. `workflows.umite.extract` argues why those numbers are
        # worth keeping; what belongs here is that the uBAM above is the reason they need a file.
        summary=f"{OUTDIR}/{{sample}}/{{sample}}{EXTRACT_SUFFIX}",
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
             --out {output.ubam} --summary {output.summary}
        """


rule star_umi_map:
    """Map ONE cell against the shared index, into one coordinate-sorted BAM.

    One cell per job. Batching cells into one STAR invocation was left off the table deliberately: it
    coarsens retry granularity, and no measurement demands it.

    **The input is a uBAM, and the flags that read it are the format.** `--readFilesType SAM ...`
    with `--readFilesCommand samtools view` is how STAR reads an alignment file as input, and
    `--readFilesSAMattrKeep` names which of the input record's tags ride through to the output. The
    `UB` tag then arrives in the output without ever being named in `--outSAMattributes`, which is
    the only way to get it there at all outside single-cell mode.

    **`UB` and not `All`, and the read group is why** (#416). The extractor writes two tags, `UB:Z:`
    and `RG:Z:`, and `All` carried both. But STAR builds its output header from the genome and its
    own parameters -- it does not inherit the input BAM's -- so the `RG` that rode through named a
    group no `@RG` line introduced, which the SAM specification forbids. samtools and pysam tolerate
    such a file; Picard and GATK refuse it, and the per-cell CRAMs downstream of here are the
    RETAINED artifacts, so the malformed one is what shipped.

    `--outSAMattrRGline` is STAR's only input to an `@RG` header line, and setting it also makes
    STAR stamp its own `RG` on every record -- `RG` is not a word `--outSAMattributes` accepts, so
    header and tag are one decision. That is exactly why the keep list had to stop saying `All`:
    STAR appends the kept input tags AFTER writing its own attributes and de-duplicates nothing
    against them (`ReadAlign_alignBAM.cpp` calls `bamAttrArrayWriteSAMtags`, which filters on the
    keep list alone), so `All` plus the flag would put `RG` on a record TWICE -- a worse file than
    the one this fixes. Naming `UB` drops the input `RG` and leaves STAR's as the only one. Nothing
    is lost by dropping it: the id is `{wildcards.sample}` on both sides, the same wildcard
    `rule umi_extract` hands the extractor, so the tag STAR writes and the tag the uBAM carried are
    the same string and cannot drift.

    Its `PE`/`SE` half is the one part of that format DERIVED per dataset rather than a module
    literal like the flags around it, and the uBAM is what it follows: two records a fragment or
    one, exactly as the layout had a mate or did not. Unpaired records read as `SAM PE` crash, so
    a stale literal here would be loud -- but only after the index loaded and the plate was queued.

    **ONE sort, and it is this one.** `--outSAMtype BAM SortedByCoordinate` serves every consumer:
    the two archive conversions and the counter each read this file. A second `samtools sort -n` to
    give the counter name adjacency would cost a full pass over every cell and a second BAM each --
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
        # Three consumers -- the two halves of the archive and the fan-in counter -- so snakemake
        # keeps it until ALL have finished and then deletes it. That is the 2x peak disk the single
        # sort avoids doubling again.
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
        read_through_clip=lambda wc: read_through_clip(wc.sample),
    shell:
        # `--outSAMmultNmax 1` is a module literal for the same reason it is one in starsolo.smk: its
        # value varies with nothing. It writes only a top-scoring alignment, which is exactly the
        # record the archive's secondary-alignment filter would keep, so the sort stops paying for
        # records the next rule deletes. It is safe for the counts HERE because our counter reads
        # `NH` directly rather than inferring multimapping from bundle length -- `NH` still counts
        # every locus.
        #
        # `--outSAMunmapped Within` puts the fragments that never aligned into the same BAM, and it
        # is the only thing that can make the counter's FIRST fate a real number: that counter reads
        # what the BAM holds, so a library with no unmapped record in it reports zero unmapped
        # fragments and is indistinguishable from one where everything aligned. Every plate object
        # written before this flag carried that column structurally empty.
        #
        # Bare `Within`, never `Within KeepPairs`. The second token keeps an unmapped record
        # adjacent to its mate in UNSORTED output only, and this rule writes coordinate-sorted
        # output -- so it would buy nothing and would state an intent this module does not have.
        # The retained archives do not grow to pay for the extra records either: each names a
        # record selection, and neither selection holds a record that never aligned.
        r"""
        # preemption-safe: STAR aborts a rerun if _STARtmp exists (undeclared, snakemake cannot remove it)
        rm -rf {params.prefix}_STARtmp
        STAR --runMode alignReads --genomeDir {input.index} --runThreadN {threads} \
             --genomeLoad LoadAndKeep \
             --readFilesIn {input.ubam} --readFilesType {params.read_files_type} \
             --readFilesCommand samtools view --readFilesSAMattrKeep UB \
             {params.read_through_clip} \
             --outFileNamePrefix {params.prefix} \
             --outSAMtype BAM SortedByCoordinate \
             --outSAMattrRGline ID:{wildcards.sample} SM:{wildcards.sample} \
             --limitBAMsortRAM {resources.bam_sort_ram_bytes} \
             --outSAMmultNmax 1 \
             --outSAMunmapped Within
        """


rule unique_to_cram:
    """One cell's UNIQUELY-PLACED records, compacted into the archive a reader opens by default.

    The converter is reused rather than rebuilt, and that is the largest single thing carrying the
    UMI in a tag bought: its QNAME rewrite touches field 1 only and rebuilds the record tab-joined,
    so every tag survives and its -16.2% comes back. A UMI carried in the read name would have been
    destroyed by exactly that rewrite.

    It does not sort -- STAR did. `container:`, unlike the extractor and the counter, because this
    verb shells out to samtools, which comes from the pinned image and not from whatever the
    submitting shell happened to have.

    The retained CRAM is not a FASTQ substitute: the tagged mate lost its structural prefix before
    STAR ever saw it, because the tag, the UMI and the motif are not genomic. The same property the
    droplet chain already records -- and the reason a selection that drops what never aligned is
    right here rather than merely smaller. A file that already cannot give the reads back gains
    nothing by carrying the ones that never aligned, and it would grow by exactly the share of the
    library that did not. `rule star_umi_map` asks STAR for those records so the COUNTER can measure
    them; the archives are where they stop.

    **The selection is NAMED and not a filter spelled out again**, so a misspelling is a refusal at
    the verb's gate rather than a quietly wrong file. `unique` and the `multi` its sibling names are
    a partition of the mapped records: every primary mapped record is in exactly one of the two, so
    the pair holds what one mixed archive held and duplicates no bytes.
    """
    input:
        bam=rules.star_umi_map.output.bam,
    output:
        cram=UNIQUE_CRAM,
        crai=f"{UNIQUE_CRAM}.crai",
    container: config["container"]
    threads: config["threads"]
    params:
        assembly=ASSEMBLY,
    shell:
        r"""
        seqforge io cram --bam {input.bam} --assembly {params.assembly} \
             --out {output.cram} --threads {threads} --selection unique
        """


rule multiplaced_to_cram:
    """One cell's MULTIPLY-PLACED records, in their own file, from the same BAM its sibling reads.

    The other half of the partition, and a separate rule rather than a second command beside the
    first: a shell block reports only its last command's status, so two conversions in one job would
    make a failed first one an archive silently missing while the rule exits 0 -- the same
    silent-plausible-wrong the converter's own pipe is waited on stage by stage to prevent.

    **A record here is a PLACEMENT and not an assignment.** The aligner emits one of the loci the
    fragment fitted, chosen among equals, so this file says where a fragment could be and never
    where it belongs. That caveat rides the artifact rather than this docstring -- the name says
    which population, and the converter stamps the sentence into the CRAM header, which is what
    survives the file being copied somewhere this module is not.

    The chimeric twin's copy of this rule is byte-identical, and that is deliberate: a multiply-
    placed fragment has no Component to be filed under, so on both arms this is one file per cell
    cut from what the aligner actually wrote.
    """
    input:
        bam=rules.star_umi_map.output.bam,
    output:
        cram=MULTIPLACED_CRAM,
        crai=f"{MULTIPLACED_CRAM}.crai",
    container: config["container"]
    threads: config["threads"]
    params:
        assembly=ASSEMBLY,
    shell:
        r"""
        seqforge io cram --bam {input.bam} --assembly {params.assembly} \
             --out {output.cram} --threads {threads} --selection multi
        """


rule umi_count:
    """THE FAN-IN: count every cell of the plate into ONE .h5ad. One job, not one per cell.

    This is the rule the module's `fan_in_artifact` declaration names, and the reason this module is
    a different shape from the three beside it. A per-cell counter followed by a merge would produce
    1440 objects to join back together on a label read off a filename -- which is the trap the
    reference tool had to warn about, and which does not exist here because each cell's `sample_id`
    travels WITH its BAM on the command line.

    It writes the object directly, with no table in between: a whole plate as dense text is hundreds
    of megabytes for a sparse object several times smaller, and a format written solely to be read
    back one rule later is a seam with no interface. Which matrices it holds is `workflows.umite.count`'s
    table to state, and this sentence used to restate the count -- which is how it came to claim four
    of them for a release that shipped five.

    A failed counting job re-runs only itself. Every per-cell BAM is on disk, which is what made the
    fan-in affordable in the first place.

    No `container:`: counting is not aligning, and pysam, gffutils and anndata are plain
    dependencies of this package. Only STAR needs an environment we do not own.

    Its memory request is the module's own arithmetic over the recipe's ONE figure, not the same
    request the mapping jobs make: this rule loads no genome index at all, and the recipe's figure
    was sized against one that does.

    **`--threads` is the threads it asked for, and it used to ask and then use one.** The cells are
    independent, so the verb forks a worker per core and each one inherits the annotation the parent
    read; the h5ad's rows stay in the order this rule listed the cells, not the order they finished.
    The request stands unchanged against that, because a worker's resident growth is a ceiling and
    not a rate — it dirties the interned gene sets once and then nothing more.
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
             --out {output.h5ad} --threads {threads}
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
    have to stay together, and the copy that lost the `|| true` would turn a finished plate into a
    failed run. The scratch is the arrangement `load_genome` argues for, on the path where it matters
    most: this command runs when the run is OVER, so a run-file left under the output prefix would
    land in a directory a user is already reading.
    """
    shell(
        "scratch=$(mktemp -d); trap 'rm -rf \"$scratch\"' EXIT; "
        f"STAR --genomeDir {OUTDIR}/index/{ASSEMBLY} --genomeLoad Remove "
        '--outFileNamePrefix "$scratch"/unload_ > /dev/null 2>&1 || true'
    )


onsuccess:
    release_genome_segment()


onerror:
    # The same release on the failing path, and it is the one that matters more: a run that died
    # mid-plate is exactly the run that leaves a ~30 GB segment resident on a node with nothing left
    # to detach from it.
    release_genome_segment()
