# workflows/map/star-umi-chimera.smk  --  HAND-WRITTEN, VERSIONED, CI-TESTED. NEVER machine-generated.
#
# The CHIMERA-AWARE TWIN of `star-umi.smk`, for the same PRE-DEMULTIPLEXED, one-cell-one-file library
# (SMART-seq3 and its relatives) mapped against a CHIMERA -- one reference built from several
# Component assemblies, whose chromosome names carry a `<separator><component>` suffix so every read
# declares which organism it landed on. The composer selects this module by id
# `map/star-umi-chimera` when the recipe's assembly name is spelled like a chimera's, and it selects
# it INSTEAD of the base rather than beside it: the base declares this id as its `chimeric_variant`,
# and nothing else can reach this file. A KB backend naming it is refused at load.
#
# **A full standalone copy, and that is FORCED rather than preferred.** Composition copies exactly
# one `.smk` into the run directory, so an `include:`d fragment would be neither copied nor eligible
# as the default target. FOUR RULES differ out of the ~580 lines below -- `rule all`, the new
# `rule split_chimera`, `rule umi_count`, and this header; the imports and the two module constants
# that serve them move with those, and `rule star_umi_map`'s prose gained a sentence about the
# per-Component figures its flags now feed. What is byte-identical is every COMMAND: genome index
# resolution, the shared genome load, UMI extraction, the mapping invocation and CRAM conversion,
# `--outSAMmultNmax 1` included -- the split's keep rule never asks where a multimapper's other loci
# are, so that flag's measured justification survives intact. Stated as commands rather than as lines
# because prose drifts between two copies and a rendered command line does not: what keeps the copies
# in step is DERIVED from the module
# registry rather than typed beside them -- the shared-genome lifecycle sweep, the wiring gate, the
# config-key scanner and the verb-existence check each pick this file up because it is registered.
# There is deliberately NO same-ness test against the base: its subject would be source text, which a
# rename reddens falsely and an indirection passes falsely.
#
# The chain is per cell `extract -> STAR -> one coordinate-sorted CHIMERIC BAM`, and then, per cell,
# ONE new step: that BAM is SPLIT into one BAM per Component, each restored to the chromosome names,
# the `@SQ` order and the lengths a run against the bare Component would have written. The counting
# fan-in then runs ONCE PER COMPONENT against that Component's own annotation, so the deliverable is
# `combined.<component>.h5ad` per Component rather than one merged object whose columns are two
# organisms' genes with nothing saying which reads were whose.
#
# **The split sits BESIDE the CRAM, not upstream of it.** `umi_to_cram` reads the PRE-split chimeric
# BAM, so a chimeric run's archive keeps every multimapper and is strictly MORE complete than a
# single-assembly run's -- and peak disk drops slightly, because the chimeric BAM is freed as soon as
# the CRAM and the split have both consumed it instead of living until the fan-in. Per-Component BAMs
# are `temp()`; the per-cell split summary beside them is not.
#
# **Every per-Component figure is over uniquely-placed reads only**, which is the split's keep rule
# and is stated here because a reader will otherwise read it as a regression: a read ambiguous ACROSS
# organisms is indistinguishable from a within-organism repeat, so a bacterial fraction read off these
# matrices is a LOWER BOUND. Two of the four read fates -- `unmapped` and `multimapping` -- therefore
# go structurally zero in a chimeric `.h5ad`, and the per-cell split summary is where they now live.
#
# **The memory figures below are the base's, carried over IDENTICAL, and they are honestly
# UNMEASURED.** A chimeric index is larger than any one Component's and nobody has measured one; the
# built `ce11_ecHT115` is a worm plus ~4 Mb of bacterium, so it barely moves, but "barely" is not a
# number and a multiplier invented here could be defended by nothing. Stated rather than guessed.
#
# The counting fan-in is DECLARED on the module (`fan_in_artifact`, carrying a `{component}`
# placeholder here where the base's carries none) rather than left to be discovered from the rule
# graph. Snakemake fans in natively and `rule all` already expands over samples, so a failed counting
# job re-runs only itself: every per-Component BAM is on disk.
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
# computed by compose from the element coordinates. This module's parse namespace is EMPTY and MUST
# stay empty: there is nothing here for a KB entry to declare, because the layout already states all
# of it — and a KB entry naming this module at all is refused, so the namespace has no declarer left.
#
# The genome index resolves at RUN TIME from a `liulab-genome` assembly id — no genome path is ever
# baked into a config or a manifest — and the Chimera's own record is likewise never opened while
# this file is PARSED. Which Components this run has arrives as one config key compose filled from
# the assembly NAME; the per-Component annotations do not, because they are not recoverable from a
# name, so the counting verb resolves each off the completion record inside its own job. Reading that
# record here instead would make every dry run of this module need a real built Chimera on disk,
# which is the same cost `rule genome_index` keeps its `Genome(...)` call in a `run:` block to avoid.

# seqforge's own helpers, imported rather than restated -- the same contract the other mapping
# modules state at greater length. `ordered_fastqs` decides the order every mate of one sample is
# handed over in, and every module must agree on it exactly; `load_units` is the one reader of the
# table, and `rule umi_extract`'s verb opens the same file through the same function (ADR-0036). `memory` is
# this module's map from the recipe's ONE memory figure to its TWO rule classes: a Snakefile is not
# importable, so arithmetic written here could never be unit-tested, only run. `PLATE_COMPONENT_H5AD`
# is the name the module registry DECLARES as this pipeline's dataset-scoped deliverable, so the
# declaration and the rule that produces it cannot come apart -- it is `PLATE_H5AD`'s discipline one
# arity out, the `{component}` surviving into the rule's output as a snakemake wildcard.
# `EXTRACT_SUFFIX` and `SPLIT_SUFFIX` are the same contract for the two per-cell summaries: the verb
# writes each, the report finds it, and the rule declares it -- one owner, imported, never spelled
# twice, which is what stops a reader that finds nothing from looking exactly like a run that never
# happened.
from seqforge.workflows import PLATE_COMPONENT_H5AD
from seqforge.workflows.h5ad import STAR_BAM
from seqforge.workflows.memory import (
    PLATE_RETRIES,
    bam_sort_ram,
    fan_in_mem_mb,
    index_mem_mb,
    per_cell_mem_mb,
)
from seqforge.workflows.split import SPLIT_SUFFIX
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
# The Chimera's Components, in the order the assembly name spells them, read at PARSE time from the
# one config key compose emits for a chimeric run. It is a plain list of names and no I/O at all,
# which is what lets `snakemake -n` plan this whole module with no built reference anywhere on disk.
# What is NOT here is each Component's annotation: a merged annotation does not record what fed it,
# so that fact is not recoverable from the name and cannot be a config key -- `rule umi_count` hands
# the counting verb a Component and the verb reads the completion record inside its own job.
COMPONENTS = config["genome"]["components"]
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

# One cell's BAM for one Component, with BOTH wildcards left in place: `{sample}` because these are
# per-cell files like everything else under a cell's directory, and `{component}` because the fan-in
# below is one job per Component over the whole plate. Spelled ONCE, here, because the rule that
# writes it and the rule that reads it must name the same path and a second spelling is the copy that
# points at cells which are not there while the ids still look right.
SPLIT_BAM = f"{OUTDIR}/{{sample}}/{{sample}}.{{component}}.bam"


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
        # ONE object PER COMPONENT over every cell, and a CRAM per cell. Each h5ad is demanded by
        # NAME rather than as a directory, and here that carries a second load: a rule whose output
        # is a folder is satisfied by a folder, which is how a counting job that wrote three cells of
        # 1440 exits 0 -- and, one arity out, how a chimeric run that counted two Components of three
        # exits 0 with an organism silently missing. Naming each one closes both.
        expand(f"{OUTDIR}/{PLATE_COMPONENT_H5AD}", component=COMPONENTS),
        expand(f"{OUTDIR}/{{sample}}/{{sample}}.cram", sample=SAMPLES),
        # Per-cell QC needs no target -- STAR writes `Log.final.out` into each cell's directory
        # unasked, and the split summary is written by a rule every h5ad above depends on, so a plate
        # that finishes has written one per cell. The report's readers find both there.


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

    **Two outputs, and only one of them is reclaimed.** The uBAM is consumed and deleted; the summary
    beside it is the durable account of what the extraction saw, and it is declared here rather than
    derived by the verb so that one path is stated once and snakemake owns removing it after a failed
    job. Nothing demands it in `rule all` and nothing needs to: this rule is upstream of every cell's
    CRAM, so a plate that finishes has written one per cell.
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
    such a file; Picard and GATK refuse it, and the per-cell CRAM downstream of here is the RETAINED
    artifact, so the malformed one is what shipped. The split BAMs inherit the fix for free: the
    splitter copies every non-`@SQ` header line verbatim, so an `@RG` STAR emits reaches each
    Component's file without `seqforge io split-chimera` learning what a read group is.

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
        read_through_clip=lambda wc: read_through_clip(wc.sample),
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
             --readFilesCommand samtools view --readFilesSAMattrKeep UB \
             {params.read_through_clip} \
             --outFileNamePrefix {params.prefix} \
             --outSAMtype BAM SortedByCoordinate \
             --outSAMattrRGline ID:{wildcards.sample} SM:{wildcards.sample} \
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


rule split_chimera:
    """One cell's chimeric BAM into one BAM per Component, each spelled for a single assembly.

    **BESIDE the CRAM rather than upstream of it**, and that is the decision this rule carries. Both
    this and `umi_to_cram` consume the same pre-split `temp()` BAM, so the archive is made from the
    file STAR actually wrote: it keeps every multimapper and is strictly MORE complete than a
    single-assembly run's, instead of inheriting this rule's filter. Splitting first would buy
    per-Component CRAMs readable against a bare Component -- and the Chimera had to exist to produce
    the run at all, so "readable without it" is not a property anyone here is short of. Peak disk also
    falls slightly: the chimeric BAM is freed once BOTH consumers finish rather than living until the
    fan-in.

    It follows `rule umi_extract` in kind and therefore in shape. A `shell:` calling a seqforge verb,
    because `snakemake -n -p` renders every shell block while planning and cannot see inside a `run:`,
    so only a verb is visible to compose's wiring gate. **No `container:`** -- pysam is a plain
    dependency of this package and only STAR needs an environment we do not own. **No `resources:`**
    -- the split is a stateless per-record filter that holds nothing at all: both mates of a template
    carry one `NH` and sit on one chromosome, so keeping a pair together needs no name sort and no
    buffer, and a ten-million-record BAM streams in constant memory.

    `threads:` is the share of the machine this job takes, and it is HANDED OVER rather than merely
    reserved. The same figure its sibling `umi_to_cram` declares -- the two run against each other
    over one BAM, so a plate's width should not depend on which of them got there first. What the
    verb spends it on is the BGZF codec and nothing else, divided across the outputs: the record loop
    is one stateless pass and stays on one core whatever this says, while the block compression
    underneath it is where writing several BAMs actually spends its wall-clock. Asking the scheduler
    for cores and then handing the verb none of them is the shape this module's own history records
    on `umi_count`, where a whole plate was counted on one core inside an allocation sized for the
    rest.

    Whether one wide split beats several narrow ones on a real plate is UNMEASURED, and the recipe's
    own figure is what is passed rather than a number invented here.

    **The outputs are rendered as `<component>=<path>` from `zip(COMPONENTS, output.bams)`** -- the
    same argument shape `umi_count` takes its cells in, and for the same reason: a Component and where
    its BAM goes travel as one token, so there is no second list for anyone to keep in the same order
    as the first. `strict=True` because a silent zip would write fewer outputs than were declared and
    snakemake would then fail on a missing file rather than on the arity that caused it.

    **`--assembly` is the CHIMERA and never a Component.** The verb resolves `(components,
    separator)` off that Chimera's completion record, which is the only honest source for both: a
    Component list read off a name is a convention, and the separator belongs to one built reference
    -- a Component whose own chromosome names already carry a doubled underscore forces a longer run
    than the default, and only the record knows.

    **Two outputs, and only the BAMs are reclaimed.** They are `temp()` and consumed by exactly one
    rule each, so the whole plate's per-Component BAMs never coexist with the objects counted from
    them. The summary beside them is deliberately NOT `temp()`: it is the durable account of what left
    and where it went, and it is where `unmapped` and `multimapping` LIVE for a chimeric run -- both
    read structurally zero in every h5ad below, because those reads leave here, one rule before the
    counter. Nothing demands it in `rule all` and nothing needs to: this rule is upstream of every
    matrix, so a plate that finishes has written one per cell.
    """
    input:
        bam=rules.star_umi_map.output.bam,
    output:
        # One per Component, with `{sample}` left standing: `allow_missing` is what keeps this a
        # per-cell rule while the Component axis is expanded here and the cell axis by `rule all`.
        bams=temp(expand(SPLIT_BAM, component=COMPONENTS, allow_missing=True)),
        summary=f"{OUTDIR}/{{sample}}/{{sample}}{SPLIT_SUFFIX}",
    threads: config["threads"]
    params:
        assembly=ASSEMBLY,
        outputs=lambda wc, output: " ".join(
            f"{component}={bam}" for component, bam in zip(COMPONENTS, output.bams, strict=True)
        ),
    shell:
        r"""
        seqforge io split-chimera {params.outputs} \
             --bam {input.bam} --assembly {params.assembly} --summary {output.summary} \
             --threads {threads}
        """


rule umi_count:
    """THE FAN-IN, once per COMPONENT: count every cell of the plate into that Component's .h5ad.

    This is the rule the module's `fan_in_artifact` declaration names, and the reason this module is
    a different shape from the three beside it. A per-cell counter followed by a merge would produce
    1440 objects to join back together on a label read off a filename -- which is the trap the
    reference tool had to warn about, and which does not exist here because each cell's `sample_id`
    travels WITH its BAM on the command line.

    **ONE `{component}` wildcard over one config list, and the rule is N-AGNOSTIC by construction.**
    A three-Component Chimera changes the length of `COMPONENTS` and nothing else in this file: the
    input expands the same way, the output names itself from the wildcard, and `rule all` demands one
    more file. There is no Component loop here and no per-Component key anywhere in the config.

    **`--component`, never `--annotation`, and the assembly stays the CHIMERA.** Exactly one of the
    two forms is legal, so rendering both would be a refusal at exit 2 rather than a precedence rule
    somebody has to remember, and rendering the Component as the assembly would resolve the wrong
    reference: the record that says what each Component contributed to the merge lives on the
    Chimera. The verb reads it there and then resolves that Component's OWN GTF -- what it actually
    contributed, not what its default annotation has since become -- so a gene's count means what it
    means in a single-organism run. A Component that contributed no annotation is a refusal naming
    it, and the cost of that is WHEN: this is the fan-in, so an uncountable Component kills the run
    after the whole plate has mapped rather than on the first job. Both Components of the Chimera
    this module exists for are annotated.

    Each Component's matrices are separate objects rather than one hstacked one, so the two organisms
    never have to be told apart downstream by a gene-name prefix. It writes the object directly, with
    no table in between: a whole plate as dense text is hundreds of megabytes for a sparse object
    several times smaller, and a format written solely to be read back one rule later is a seam with
    no interface. Which matrices it holds is `workflows.umite.count`'s table to state.

    A failed counting job re-runs only itself, and only for its own Component. Every per-Component
    BAM is on disk, which is what made the fan-in affordable in the first place.

    No `container:`: counting is not aligning, and pysam, gffutils and anndata are plain
    dependencies of this package. Only STAR needs an environment we do not own.

    Its memory request is the module's own arithmetic over the recipe's ONE figure, not the same
    request the mapping jobs make: this rule loads no genome index at all, and the recipe's figure
    was sized against one that does. UNMEASURED against a chimeric run, like every figure in this
    file: a Component's matrix is slightly SMALLER than its single-assembly counterpart, because
    cross-Component multimappers never entered one, so the arithmetic is if anything generous -- but
    "if anything" is not a measurement and the number is the base's, unchanged.

    **`--threads` is the threads it asked for.** The cells are independent, so the verb forks a
    worker per core and each one inherits the annotation the parent read; the h5ad's rows stay in the
    order this rule listed the cells, not the order they finished. The request stands unchanged
    against that, because a worker's resident growth is a ceiling and not a rate — it dirties the
    interned gene sets once and then nothing more.
    """
    input:
        # Every cell of the plate, for THIS Component: the cell axis is expanded here and the
        # Component axis is left standing as this rule's own wildcard.
        bams=expand(SPLIT_BAM, sample=SAMPLES, allow_missing=True),
    output:
        # The `{component}` in the declared name survives into the output as a snakemake wildcard,
        # so the registry's `fan_in_artifact` and this rule stay ONE owner of that filename.
        h5ad=f"{OUTDIR}/{PLATE_COMPONENT_H5AD}",
    threads: config["threads"]
    retries: PLATE_RETRIES
    resources:
        mem_mb=lambda wildcards, attempt: fan_in_mem_mb(config["mem_mb"], attempt),
    params:
        assembly=ASSEMBLY,
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
             --assembly {params.assembly} --component {wildcards.component} \
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
