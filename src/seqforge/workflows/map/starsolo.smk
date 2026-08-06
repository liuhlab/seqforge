# workflows/map/starsolo.smk  --  HAND-WRITTEN, VERSIONED, CI-TESTED. NEVER machine-generated.
#
# STARsolo mapping for barcoded single-cell RNA-seq (10x 3' v2/v3, SPLiT-seq, ...). The composer
# emits `config.yaml` + `units.tsv` and selects this module by id `map/starsolo`; it NEVER writes
# rule source. Every chemistry-defining knob arrives via `config["solo"]` (rendered from the KB's
# backend.params and asserted by compose's params gate); the read->role placement arrives via
# `config["read_files_in"]`, cDNA FIRST.
#
# The genome index resolves at RUN TIME from a `liulab-genome` assembly id — no genome path
# is ever baked into a config or a manifest, and we do not reimplement liulab-genome's job here.

import csv

# seqforge's own helpers, imported rather than restated. `h5ad_suffixes` decides both what the
# packaging rule DECLARES below and what `seqforge io h5ad` WRITES, so the two cannot drift; a rule
# that declared its outputs separately from the code producing them would be two sources of truth for
# one fact, which is the bug this repo keeps finding. `memory` is the same move applied to the
# arithmetic that sizes this module's one expensive rule: a Snakefile is not importable, so a
# constant and a closure living here could never be unit-tested, only run. `QC_SUFFIX` is that same
# rule applied to the last name this file used to spell for itself, and it is the one where drift
# would be SILENT: `workflows/qc.py` writes that bundle and now reads it back for the report, so a
# rename here would leave the rule producing a file the reader stops finding -- and a report that
# finds nothing looks exactly like a pipeline that has not run. All three imports make the same
# assumption `rule genome_index` already makes of `genome`: the env running snakemake is the env that
# has them.
from seqforge.workflows.h5ad import (
    STAR_BAM,
    STAR_LOG_FILES,
    h5ad_suffixes,
    solo_filtered_files,
    solo_raw_files,
    solo_stats_files,
)
from seqforge.workflows.memory import STARSOLO_RETRIES, bam_sort_ram, escalated_mem_mb
from seqforge.workflows.qc import QC_SUFFIX
from seqforge.workflows.units import ordered_fastqs


def _load_units(path):
    with open(path, newline="") as fh:
        return list(csv.DictReader(fh, delimiter="\t"))


UNITS = _load_units(config["units_tsv"])
SAMPLES = sorted({u["sample_id"] for u in UNITS})
OUTDIR = config["outdir"]
ASSEMBLY = config["genome"]["assembly"]
SOLO = config["solo"]
# STAR takes --soloFeatures as N space-separated values and writes one Solo.out/<Feature>/ per value.
FEATURES = SOLO["soloFeatures"].split()
PRIMARY = config["primary_feature"]


def fastqs(sample, role):
    # `ordered_fastqs` owns the order and the argument for it; the two other mapping modules read the
    # same one. Mismatched RUNS do FATAL here ("quality string length is not equal to sequence
    # length") -- mismatched LANES are the silent half, and are why the order is not `path` alone.
    return ordered_fastqs(UNITS, sample, role)


def readfilesin(sample, *roles):
    """Render STAR ``--readFilesIn`` for one sample: each role (a mate) is its FASTQs **comma-joined**,
    and the mates are space-separated -- ``cdna1,cdna2 barcode1,barcode2``.

    A sample pooled across N sequencing runs (or across one run's lanes) passes every such file for a
    mate as one comma-list, in the single order ``fastqs`` imposes on every mate alike. This is STAR's
    own multi-file syntax; joining with spaces instead -- the old bug -- makes STAR read the extra
    files as extra mates and crash. A single-run sample renders one file per mate, exactly as before,
    so this generalises to any run and lane count with no special case."""
    return " ".join(",".join(fastqs(sample, role)) for role in roles)


def whitelists():
    """One path for 10x; three for a split-pool chemistry. The config value is the argv rendering."""
    return SOLO["soloCBwhitelist"].split()


def cb_umi_geometry():
    """Where the CB and UMI live -- and STARsolo spells this two different ways.

    A simple chemistry (10x) has one contiguous barcode, so a start/length pair locates it. A
    combinatorial one (SPLiT-seq, BD Rhapsody) has barcodes scattered between linkers, so each needs a
    position quadruple and no start/length exists to give. This is not a preference: passing
    --soloCBstart to CB_UMI_Complex is an error, and the keys are absent from the config precisely
    because the chemistry has no such value. Compose emits whichever set the soloType implies (the
    params gate proves the block is exactly what its owners declared), so the branch here reads what is
    there.

    GEOMETRY ONLY. This branch used to also pin ``--soloCBmatchWLtype 1MM`` on the Complex side; that
    key is now the KB's (#198) and is emitted once, from the shell block, for BOTH branches. The pin
    was load-bearing and its reason survives in the specs that inherited it -- STAR REJECTS its own
    global default ``1MM_multi`` for CB_UMI_Complex, so a Complex chemistry naming no match type
    FATALs on the default alone -- but it could only ever state one value per soloType, and Parse
    Evercode (``EditDist_2``) and BD Rhapsody (``1MM``) are both Complex and disagree. A branch that
    yields two answers cannot serve three chemistries; a per-chemistry file can.
    """
    if SOLO["soloType"] == "CB_UMI_Complex":
        return (
            f"--soloCBposition {SOLO['soloCBposition']} "
            f"--soloUMIposition {SOLO['soloUMIposition']}"
        )
    return (
        f"--soloCBstart {SOLO['soloCBstart']} --soloCBlen {SOLO['soloCBlen']} "
        f"--soloUMIstart {SOLO['soloUMIstart']} --soloUMIlen {SOLO['soloUMIlen']}"
    )


def barcode_read_length():
    """--soloBarcodeReadLength, and ONLY when the chemistry declares it.

    STARsolo's default (1) FATALs unless the barcode read is exactly CB+UMI long. 10x v2/v3/v3.1 R1 is
    routinely sequenced longer than the 26/28 nt the barcode occupies (a 150 nt R1 is common), so their
    specs set `soloBarcodeReadLength: 0` to disable that check and read CB/UMI from the fixed offsets.
    A chemistry that does not set the key (SPLiT-seq, ...) keeps STAR's default, so the flag is emitted
    iff it is present -- the same "render whatever the chemistry put in the block" contract as the
    geometry above.

    `SOLO.get(...)`, deliberately NOT `SOLO["..."]`: a subscript would make `keys_read_by` (see
    `workflows/__init__.py`) mark `solo.soloBarcodeReadLength` a REQUIRED config key, and the composer
    would then be obliged to emit it for every starsolo chemistry -- including SPLiT-seq, whose params
    gate forbids emitting a key it does not own. `.get` is the honest "optional read" the scanner
    correctly leaves out of `required_config`.
    """
    value = SOLO.get("soloBarcodeReadLength")
    return f"--soloBarcodeReadLength {value}" if value is not None else ""


def adapter_sequence():
    """--soloAdapterSequence, and ONLY when the chemistry declares it (an ANCHORED bead).

    BD Rhapsody Enhanced prepends a variable 0-3 bp diversity insert to the barcode read, so the CB/UMI
    offsets float. STARsolo absorbs the stagger by anchoring to this adapter (`NNN...GTGANNN...GACA`):
    it finds the adapter in each read and reads the barcodes at the anchor-2/anchor-3 positions
    `cb_umi_geometry()` emits. Derived from the linker elements at compose time (compose/params.py) and
    present in `config["solo"]` only for such a chemistry -- `.get`, so a fixed-offset chemistry (10x,
    the original BD bead) neither declares it nor has the scanner mark it a required key.
    """
    value = SOLO.get("soloAdapterSequence")
    return f"--soloAdapterSequence {value}" if value is not None else ""


# Every raw matrix/axis file this run's --soloFeatures must produce, per sample -- declared
# file-by-file, and that is the point. `starsolo_count` used to declare
# `directory(f"{OUTDIR}/{{sample}}/Solo.out")`, under which STAR writing three of five features and
# exiting 0 was indistinguishable from success: the directory exists, snakemake is satisfied, and the
# missing counts surface later as an h5ad nobody can explain. A named output cannot be missing.
# The `{{{{sample}}}}` is snakemake's usual escape -- expand() fills `f` and leaves `sample` a wildcard.
SOLO_MATRICES = expand(f"{OUTDIR}/{{{{sample}}}}/Solo.out/{{f}}", f=solo_raw_files(FEATURES))

# The rest of what STAR writes, declared so the finalize rules can consume it and `temp()` can then
# delete it -- automatic, DAG-ordered cleanup, never a manual `rm`. Same file-by-file discipline as
# SOLO_MATRICES: a declared output STAR did not write fails the rule loudly. The stats + logs + the
# filtered/ tree feed `qc_bundle`; the BAM feeds `solo_to_cram`.
SOLO_STATS = expand(f"{OUTDIR}/{{{{sample}}}}/Solo.out/{{f}}", f=solo_stats_files(FEATURES))
SOLO_FILTERED = expand(f"{OUTDIR}/{{{{sample}}}}/Solo.out/{{f}}", f=solo_filtered_files(FEATURES))
STAR_LOGS = expand(f"{OUTDIR}/{{{{sample}}}}/{{f}}", f=list(STAR_LOG_FILES))


rule all:
    input:
        expand(
            f"{OUTDIR}/{{sample}}/{{sample}}{{suffix}}",
            sample=SAMPLES,
            suffix=h5ad_suffixes(FEATURES),
        ),
        # The retained finalize deliverables: a compact CRAM of the alignment and one gzipped-JSON
        # stats bundle per sample. The raw matrices, filtered tree, stats, logs, and BAM they are
        # built from are all `temp()` and gone by the time these land.
        expand(f"{OUTDIR}/{{sample}}/{{sample}}.cram", sample=SAMPLES),
        expand(f"{OUTDIR}/{{sample}}/{{sample}}{QC_SUFFIX}", sample=SAMPLES),


rule onlist:
    """Materialize one barcode whitelist, for STAR to read once and snakemake to then delete.

    `temp()` is the entire point, and why -- the 111 MB a compiled run used to carry three times over,
    and why an input with no producing rule was `temp()`-able in name only -- is argued once in
    ADR-0015.

    No `container:` directive, deliberately. This runs `seqforge`, which is not an aligner -- the
    ambient environment is the one that just ran `seqforge compose`, so it is by construction the one
    that has it. Naming `align-rna` here would put our own tool inside STAR's image.
    """
    output:
        temp("onlists/{name}.txt"),
    localrule: True
    shell:
        "seqforge io onlist write {wildcards.name} --out {output}"


rule genome_index:
    """Resolve the STAR index via liulab-genome at run time (never a path in the manifest).

    This rule only **looks up** the index; it never builds one. `get_star_index` returns the genomeDir
    liulab-genome already built for this assembly + annotation, and **raises if none exists** -- the
    index is liulab-genome's artifact, built ahead of the run by its own machinery, in its own
    environment. A machine with no prebuilt index fails loudly here ("build it first"), which is the
    failure mode we want: the pipeline consumes the index, it does not decide when or how it is built.

    Because nothing is invoked here -- no STAR, no `genomeGenerate` -- this rule needs no tool on PATH
    and no `container:`. (A `container:` would be moot anyway: snakemake wraps a container around a
    `shell:` command in `shell.py`, but a `run:` block executes Python in the snakemake process and
    never passes through that wrap; snakemake's own linter excludes `is_run` rules from "missing
    software definition".) The container on the alignment rule pins the aligner that does the work.
    """
    output:
        directory(f"{OUTDIR}/index/{ASSEMBLY}"),
    params:
        assembly=ASSEMBLY,
        annotation=config["genome"]["annotation"],
    run:
        from pathlib import Path

        from genome import Genome

        index = Genome(params.assembly).get_star_index(gtf=params.annotation)
        out = Path(output[0])
        out.parent.mkdir(parents=True, exist_ok=True)
        out.symlink_to(index)


rule starsolo_count:
    """Map one sample's cDNA + barcode reads to a per-cell count matrix, and to a BARCODED alignment.

    The shell block clears STAR's `_STARtmp` before invoking STAR, so every (re)run is
    preemption-safe: a preempted STAR leaves `results/<sample>/_STARtmp` behind, STAR ABORTS a rerun
    if it already exists, and snakemake cannot remove it (an undeclared output).

    `--outSAMtype BAM SortedByCoordinate` is not a preference either (#198). STARsolo writes only the
    cDNA mate into the BAM, and the barcode lives solely in the other mate, so with no CB/UB tag the
    barcode is IRRECOVERABLY absent from the retained alignment -- which is what made 920 GiB of
    shipped CRAM unable to do any of the three things a retained alignment is for. STAR emits those
    tags in the sorted BAM and nowhere else ("CB and/or UB attributes in --outSAMattributes can only
    be output in the sorted BAM"), so the sort is the price of the barcode. It is also a refund: the
    finalize rule no longer re-sorts, and the resulting CRAM measured 12% SMALLER than the
    barcode-less one it replaces.

    **The memory request ESCALATES with the attempt, and STAR's sort cap escalates with it** (#205).
    STARsolo holds allocations `--limitBAMsortRAM` does not bound -- chiefly `readInfo`, at 16 bytes
    times every input read -- so a large sample used to be OOM-killed by the scheduler rather than
    refused by STAR, dying with a signal and no number. `retries:` plus a `mem_mb` that is a function
    of `attempt` gives such a sample two more tries at 2x and 3x, while attempt 1 stays byte-identical
    to what a normal sample was always given. A sample that exhausts the retries FAILS, loudly and
    deliberately: at that point the answer is a recipe with a bigger `resources.mem_gb`, chosen by
    someone who looked at the sample, not a third blind doubling. The arithmetic and the measurements
    behind it live in `workflows/memory.py`.
    """
    input:
        cdna=lambda wc: fastqs(wc.sample, config["read_files_in"]["cdna"]),
        barcode=lambda wc: fastqs(wc.sample, config["read_files_in"]["barcode"]),
        index=rules.genome_index.output,
        whitelist=whitelists(),
    output:
        # `temp()` on everything: the raw matrices are consumed by `solo_to_h5ad`, the stats +
        # filtered tree + logs by `qc_bundle`, and the BAM by `solo_to_cram`. Snakemake deletes each
        # group once its one consumer finishes -- so nothing here survives that is not a `rule all`
        # target. The files stay declared (not just `rm`'d) so a missing one is still a rule failure.
        matrices=temp(SOLO_MATRICES),
        stats=temp(SOLO_STATS),
        filtered=temp(SOLO_FILTERED),
        logs=temp(STAR_LOGS),
        bam=temp(f"{OUTDIR}/{{sample}}/{STAR_BAM}"),
    # The pinned aligner: liulab-runtime's `align-rna`, resolved by compose to a ghcr tag or to a
    # prebuilt .sif on this machine. Naming it here is CONSUMING liulab-runtime's artifact, not
    # defining an environment -- no conda YAML, no Dockerfile, and no dependency table this rule can
    # resolve against. The STAR a test execs lives in a test-only environment no rule can see.
    #
    # Honoured only when the run passes `--software-deployment-method apptainer` (measured: without
    # it, snakemake plans the same jobs and never mentions the image). That is snakemake's contract
    # and it is the user's call -- they submit, we do not.
    container: config["container"]
    threads: config["threads"]
    # `retries:` and `resources:` are ONE mechanism, which is why they are read together. Declaring
    # `mem_mb` gates the scheduler AND gives the coordinate sort a real budget instead of the
    # genome's (see `bam_sort_ram`); it moved here from `solo_to_cram`, which is where the sort used
    # to happen, so the memory is spent in the rule that does the sorting. What is new (#205) is that
    # BOTH numbers are functions of snakemake's 1-based `attempt`: attempt 1 is `config["mem_mb"]`
    # exactly, so nothing changes for a sample that fits, and a sample killed for overrunning it gets
    # 2x and then 3x rather than failing identically twice. `config["mem_mb"]` still appears here as
    # a literal subscript on purpose -- `workflows/__init__.py::keys_read_by` SCANS this source to
    # compute `required_config`, and a key the scanner cannot see is a key the composer is not
    # obliged to emit, i.e. a KeyError on a compute node long after compose exited 0.
    #
    # THE SORT CAP IS A `resources:` ENTRY, NOT A `params:` ONE, and that is not a style choice --
    # it is the only construct snakemake re-evaluates per attempt. MEASURED against the pinned
    # 9.23.1, because the plausible version of this is wrong: `Job.attempt`'s setter (`jobs.py`)
    # clears `self._resources` and NOT `self._params`, and `reset_params_and_resources()` is
    # one-shot behind `_params_and_resources_resetted`. So a `params:` callable -- even one taking
    # `resources`, which snakemake does pass -- is expanded once, on attempt 1, and every retry
    # reuses that value verbatim. A three-attempt run of exactly that shape traced
    # `mem=1000 cap=750 / mem=2000 cap=750 / mem=3000 cap=750`: the request escalates and the cap
    # does not. That is worse than no retry at all -- attempt 2 buys scheduler memory STAR is still
    # forbidden to sort in, spends a second multi-hour queue slot, and fails for the reason attempt
    # 1 already recorded. As a resource the same run traces 750 / 1500 / 2250.
    #
    # The name carries its unit (`_bytes`) because it is the one number here that is not MiB: STAR
    # takes `--limitBAMsortRAM` in bytes, and a resource is a bare integer with nowhere else to say
    # so. It is a custom resource, so nothing constrains it unless a `--resources` flag names it.
    retries: STARSOLO_RETRIES
    resources:
        mem_mb=lambda wildcards, attempt: escalated_mem_mb(config["mem_mb"], attempt),
        bam_sort_ram_bytes=lambda wildcards, attempt: bam_sort_ram(
            escalated_mem_mb(config["mem_mb"], attempt)
        ),
    params:
        solo=SOLO,
        geometry=cb_umi_geometry(),
        barcode_read_length=barcode_read_length(),
        adapter=adapter_sequence(),
        prefix=lambda wc: f"{OUTDIR}/{wc.sample}/",
        # cDNA mate first, then barcode mate (order asserted by the params gate); each mate is its
        # runs and lanes comma-joined, so a pooled sample maps in one STAR pass. See readfilesin().
        reads=lambda wc: readfilesin(
            wc.sample, config["read_files_in"]["cdna"], config["read_files_in"]["barcode"]
        ),
    shell:
        # --readFilesIn takes the cDNA read FIRST, then the barcode read (asserted by the params gate).
        # {params.barcode_read_length} is `--soloBarcodeReadLength 0` for 10x (over-length R1) and empty
        # for a chemistry that does not declare it -- an empty token is a valid line continuation.
        #
        # EXACTLY FIVE OF THE LITERALS BELOW ARE THE CELLRANGER-PARITY SET (#198) -- `--clipAdapterType
        # CellRanger4`, `--outFilterScoreMin 30`, `--soloUMIfiltering MultiGeneUMI_CR`,
        # `--soloUMIdedup 1MM_CR` and `--soloCellFilter EmptyDrops_CR`, and no others. They are the
        # documented "CellRanger >=4 equivalent" set (Kaminow, Yunusov & Dobin 2021); without them we
        # emit STARsolo-DEFAULT counts, which are not comparable to published CellRanger matrices -- a
        # real problem for a corpus whose point is comparability. The SAM/BAM write-path literals at
        # the bottom of the block (`--outSAMtype`, `--limitBAMsortRAM`, `--outSAMattributes`,
        # `--outSAMmultNmax`) are hardcoded for the same OWNERSHIP reason and are NOT part of that
        # set: they shape the alignment we retain, not the counts, and naming them as CellRanger
        # parity would be a claim nobody measured.
        #
        # The shared reason is ADR-0011's: none of these varies by chemistry, so none belongs to the
        # KB, and a literal is the only rendering that says so -- the params gate requires the emitted
        # key set to be EXACTLY union(KB keys, processing keys), and `required_config` is COMPUTED
        # from this source, so a `params.solo[clipAdapterType]` subscript would silently oblige all 11
        # starsolo specs to declare a value that is the same in all 11. `--outSAMtype` has always been
        # hardcoded here for the same reason. Verified against the STAR 2.7.11b binary that every one
        # is accepted for CB_UMI_Simple AND CB_UMI_Complex -- this is the class of change that passes
        # a 10x-only suite and breaks the four Complex specs.
        #
        # `--outSAMmultNmax 1` is the newest of the write-path literals (#205), and it earns its own
        # paragraph because it is the only one that changes WHICH RECORDS come out rather than what
        # each record carries or where it goes. STAR emits every alignment of a multi-mapping
        # read and coordinate-sorts them all; `seqforge io cram` then discards the secondaries with
        # `-F 0x100`. On the measured sample that was 198.8M records sorted against 162.9M retained --
        # ~18% of the sort spent producing bytes the very next rule deletes, paid in both the sort
        # budget above and in wall-clock. `nTrOutWrite = min(P.outSAMmultNmax, nTrOutSAM)` writes only
        # a top-scoring alignment, which is the record the CRAM filter keeps.
        #
        # THE COUNTS ARE UNTOUCHED AND THE CRAM IS NOT BYTE-IDENTICAL, which is the opposite of what
        # #205 claimed and was checked against the STAR 2.7.11b source rather than its manual. Counts:
        # the flag appears ONLY in the SAM/BAM write path and the alignment-ordering code, in NO Solo
        # counting file; `SoloFeature_addBAMtags` keys CB/UB on the read index alone and the gene
        # assignment is an order-independent set union. The CRAM: for a read with `NH > 1`,
        # `outSAMmultNmax != -1` is itself the trigger in `ReadAlign_multMapSelect.cpp` for
        # partitioning `trMult` so max-score alignments come first AND for marking `trMult[0]` primary
        # instead of `trBest` -- and `HI` is an OUTPUT-ORDER index (`iTrOut + outSAMattrIHstart`,
        # `ReadAlign_alignBAM.cpp`), so a multimapper's retained record now always carries `HI:i:1`.
        # Where several loci tie on score it can also be a DIFFERENT one of them: `trBest` breaks the
        # tie on the shorter genomic span (`gLength`), the partition takes the first in window order.
        # Both are top-scoring, so this changes the tie-break and not the quality; `NH` still counts
        # every locus (computed from `nTrOutSAM`, not from the truncated write count) and a uniquely
        # mapping read is bit-for-bit untouched. It is affordable precisely because the
        # `WORKFLOW_VERSION` bump already obliges reprocessing. The name is real, verified against the
        # pinned 2.7.11b binary (a bogus parameter name FATALs with "unrecognized parameter name";
        # this one does not). Per ADR-0011 its value varies with NOTHING -- not the chemistry, not the
        # user's intent, there is one correct value for every dataset seqforge will ever compile --
        # which is precisely what makes it the module's to hardcode rather than the KB's or the
        # recipe's. `-F 0x100` STAYS in `cram.py`, and do not "clean it up": it is now a cheap
        # invariant rather than a load-bearing filter, and an invariant is not deleted for the crime
        # of having stopped firing.
        #
        # `--soloMultiMappers` is deliberately ABSENT (it stays `Unique`): 87% of the multi-gene signal
        # on the measured library was the tandem rDNA array, EM splits identical copies evenly and
        # emits a large arbitrary number that reads as data, and all four multimapper matrices are
        # FRACTIONAL, which breaks pseudobulk. The diagnostic that would justify revisiting it
        # (`Features.stats` MultiFeature) already ships in every QC bundle.
        r"""
        # preemption-safe: STAR aborts a rerun if _STARtmp exists (undeclared, snakemake cannot remove it)
        rm -rf {params.prefix}_STARtmp
        STAR --runMode alignReads --genomeDir {input.index} --runThreadN {threads} \
             --readFilesIn {params.reads} --readFilesCommand zcat \
             --soloType {params.solo[soloType]} \
             {params.geometry} \
             {params.adapter} \
             {params.barcode_read_length} \
             --soloCBwhitelist {input.whitelist} \
             --soloCBmatchWLtype {params.solo[soloCBmatchWLtype]} \
             --soloStrand {params.solo[soloStrand]} \
             --soloFeatures {params.solo[soloFeatures]} \
             --clipAdapterType CellRanger4 \
             --outFilterScoreMin 30 \
             --soloUMIfiltering MultiGeneUMI_CR \
             --soloUMIdedup 1MM_CR \
             --soloCellFilter EmptyDrops_CR \
             --outFileNamePrefix {params.prefix} \
             --outSAMtype BAM SortedByCoordinate \
             --limitBAMsortRAM {resources.bam_sort_ram_bytes} \
             --outSAMmultNmax 1 \
             --outSAMattributes NH HI AS nM CB UB
        """


rule solo_to_h5ad:
    """Package Solo.out's raw matrices as .h5ad -- THE deliverable of this pipeline.

    A `shell:` calling a seqforge verb, not a `run:` block, and that is deliberate: `snakemake -n -p`
    renders every shell block while planning and cannot see inside a `run:` block, so this way
    compose's wiring gate covers the packaging step too. It is also the CLI-is-the-API line.

    No `container:`. Writing an .h5ad is seqforge's own output-format job, not an aligner's; `anndata`
    is a plain dependency of this package. Only `starsolo_count` needs liulab-runtime.
    """
    input:
        matrices=rules.starsolo_count.output.matrices,
    output:
        expand(f"{OUTDIR}/{{{{sample}}}}/{{{{sample}}}}{{suffix}}", suffix=h5ad_suffixes(FEATURES)),
    params:
        solo=lambda wc: f"{OUTDIR}/{wc.sample}/Solo.out",
        prefix=lambda wc: f"{OUTDIR}/{wc.sample}/{wc.sample}",
        features=" ".join(FEATURES),
        primary=PRIMARY,
    shell:
        r"""
        seqforge io h5ad --solo-dir {params.solo} --features "{params.features}" \
             --primary {params.primary} --out-prefix {params.prefix}
        """


rule solo_to_cram:
    """Compact STAR's coordinate-sorted BAM into a CRAM, then let `temp()` drop the BAM.

    A sibling of `solo_to_h5ad`: both consume `starsolo_count` and nothing else, so snakemake runs
    them in parallel. The reference is resolved at run time from the assembly id via liulab-genome
    (never a baked path); no `embed_ref`, so the CRAM carries the reference MD5 in its header and the
    assembly id is recorded in the QC bundle.

    It no longer sorts, and that deleted a defect rather than a step (#198). The `samtools sort` this
    verb used to run was built with no `-T`, so its temp files landed in the CWD as
    `samtools.<pid>.<tid>.tmp.NNNN.bam` -- undeclared, so a preempted sort leaked them and snakemake
    could not clean up what it had not made (41.4 GiB had accumulated across five pipeline dirs). The
    fix is not a `-T` pointing somewhere snakemake owns: STAR sorts now, so there is no second sort
    to configure correctly. Prefer the mechanism that cannot leak over the one that must be set up
    right. With no sort there is also nothing here to spend memory on, so the rule declares no
    `resources: mem_mb` -- that budget moved to `starsolo_count`, which is where the sorting went.

    `container:`, unlike `solo_to_h5ad`. This rule shells out to **samtools**, a runtime binary -- so,
    exactly like `starsolo_count`'s STAR, the tool must come from the pinned `align-rna` image and not
    from "whatever the submitting shell happened to have". `align-rna` carries samtools (its base
    layer), seqforge and liulab-genome (its `lab` feature), so `seqforge io cram` runs fully inside it.
    The h5ad/onlist/bundle steps stay container-less because they invoke no external binary; this one
    does, which is the whole distinction.
    """
    input:
        bam=rules.starsolo_count.output.bam,
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


rule qc_bundle:
    """Bundle STAR's stats + run logs into one gzipped JSON, then let `temp()` drop the originals.

    Consumes the per-feature stats, the filtered/ tree (only its barcodes.tsv is read -- kept as
    provenance of STAR's default cell call -- but listing the whole tree here is what triggers its
    deletion), and the top-level logs. A `shell:` verb, not a `run:`, so compose's wiring gate sees it.
    """
    input:
        stats=rules.starsolo_count.output.stats,
        filtered=rules.starsolo_count.output.filtered,
        logs=rules.starsolo_count.output.logs,
    output:
        f"{OUTDIR}/{{sample}}/{{sample}}{QC_SUFFIX}",
    params:
        solo=lambda wc: f"{OUTDIR}/{wc.sample}/Solo.out",
        run_dir=lambda wc: f"{OUTDIR}/{wc.sample}",
        features=" ".join(FEATURES),
        assembly=ASSEMBLY,
    shell:
        r"""
        seqforge io qc-bundle --solo-dir {params.solo} --run-dir {params.run_dir} \
             --features "{params.features}" --sample {wildcards.sample} \
             --assembly {params.assembly} --out {output}
        """
