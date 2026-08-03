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
# one fact, which is the bug this repo keeps finding. The import is the same assumption
# `rule genome_index` already makes of `genome`: the env running snakemake is the env that has them.
from seqforge.workflows.h5ad import (
    STAR_BAM,
    STAR_LOG_FILES,
    h5ad_suffixes,
    solo_filtered_files,
    solo_raw_files,
    solo_stats_files,
)


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
    # Ordered by the units.tsv `run` column so a pooled sample's mates pair correctly: STAR reads
    # --readFilesIn mate-by-mate and desyncs (FATAL: "quality string length is not equal to sequence
    # length") if cDNA run K is joined with barcode run J. `run` is seqforge's own run grouping, so
    # run N of one mate lines up with run N of the other -- no filename parsing here.
    us = [u for u in UNITS if u["sample_id"] == sample and u["read_id"] == role]
    return [u["path"] for u in sorted(us, key=lambda u: (u["run"], u["path"]))]


def readfilesin(sample, *roles):
    """Render STAR ``--readFilesIn`` for one sample: each role (a mate) is its FASTQs **comma-joined**,
    and the mates are space-separated -- ``cdna1,cdna2 barcode1,barcode2``.

    A sample pooled across N sequencing runs passes every run's file for a mate as one comma-list, in
    matching run order for every mate (``fastqs`` preserves units.tsv order, which lists a sample's
    runs in one order). This is STAR's own multi-file syntax; joining with spaces instead -- the old
    bug -- makes STAR read the extra files as extra mates and crash. A single-run sample renders one
    file per mate, exactly as before, so this generalises to any run count with no special case."""
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
_BAM_SORT_RAM_NUMERATOR, _BAM_SORT_RAM_DENOMINATOR = 3, 4


def bam_sort_ram():
    """``--limitBAMsortRAM``, in BYTES, derived from the job's own memory budget.

    Not optional, and not a tuning knob: STAR's default of ``0`` means *"reuse the genome
    allocation"*, so the sort budget silently becomes a function of how big the genome happens to be.
    On a large genome that over-commits; on a small one (the yeast index `kb e2e` runs against) it is
    too small and STAR FATALs. Neither failure has anything to do with how much memory the job was
    actually given, which is the number that should decide this -- so we pass it.

    **Sizing the job is the caller's business, and it is not free.** At ~160 B/record (measured
    above), a 215M-read sample lands near 32 GB of sort RAM -- more than `mem_gb`'s default of 32
    leaves after the genome. That is a real cost of putting CB/UB in the CRAM, since STAR emits them
    only in the sorted BAM, and it is the recipe's `resources.mem_gb` that answers it. The failure is
    at least legible: STAR names the number it needed and exits, rather than producing a short BAM.

    STAR takes bytes; `config["mem_mb"]` is MiB, and that unit crossing is the whole reason this is a
    named function rather than an expression in the shell block.
    """
    share = config["mem_mb"] * _BAM_SORT_RAM_NUMERATOR // _BAM_SORT_RAM_DENOMINATOR
    # The floor may not exceed the budget itself: on a job smaller than the floor, claiming more than
    # the whole request would trade STAR's legible refusal for the scheduler's OOM kill.
    return min(config["mem_mb"], max(_MIN_BAM_SORT_RAM_MB, share)) * 1024 * 1024


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
        expand(f"{OUTDIR}/{{sample}}/{{sample}}.qc.json.gz", sample=SAMPLES),


rule onlist:
    """Materialize one barcode whitelist, for STAR to read once and snakemake to then delete.

    `temp()` is the entire point. 10x's v3 whitelist is 6 794 880 barcodes = 111 MB of text, and
    `compose` used to write it into the run directory at compile time -- so one dataset compiled
    three ways cost a third of a gigabyte of identical bytes, sitting there forever, for a file STAR
    opens once. Now it is built on demand and deleted when the last job that needs it is done.

    It was also `temp()`-able in name only before this rule existed: the whitelist was bound to
    `starsolo_count.input` with NO producing rule, and snakemake cannot delete what it did not make.
    An input with no rule is a file snakemake merely requires to already be there.

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
    # defining an environment -- no conda YAML, no Dockerfile, no STAR in any dependency table.
    #
    # Honoured only when the run passes `--software-deployment-method apptainer` (measured: without
    # it, snakemake plans the same jobs and never mentions the image). That is snakemake's contract
    # and it is the user's call -- they submit, we do not.
    container: config["container"]
    threads: config["threads"]
    # Declared so the scheduler gates on it AND so the coordinate sort gets a real budget instead of
    # inheriting the genome's (see `bam_sort_ram`). It moved here from `solo_to_cram`, which is where
    # the sort used to happen; the memory is now spent in the rule that does the sorting.
    resources:
        mem_mb=config["mem_mb"],
    params:
        solo=SOLO,
        geometry=cb_umi_geometry(),
        barcode_read_length=barcode_read_length(),
        adapter=adapter_sequence(),
        sort_ram=bam_sort_ram(),
        prefix=lambda wc: f"{OUTDIR}/{wc.sample}/",
        # cDNA mate first, then barcode mate (order asserted by the params gate); each mate is its
        # runs comma-joined, so a sample pooled across runs maps in one STAR pass. See readfilesin().
        reads=lambda wc: readfilesin(
            wc.sample, config["read_files_in"]["cdna"], config["read_files_in"]["barcode"]
        ),
    shell:
        # --readFilesIn takes the cDNA read FIRST, then the barcode read (asserted by the params gate).
        # {params.barcode_read_length} is `--soloBarcodeReadLength 0` for 10x (over-length R1) and empty
        # for a chemistry that does not declare it -- an empty token is a valid line continuation.
        #
        # THE FIVE HARDCODED FLAGS BELOW ARE LITERALS ON PURPOSE (#198). They are the documented
        # "CellRanger >=4 equivalent" set (Kaminow, Yunusov & Dobin 2021); without them we emit
        # STARsolo-DEFAULT counts, which are not comparable to published CellRanger matrices -- a real
        # problem for a corpus whose point is comparability. None of them varies by chemistry, so none
        # of them belongs to the KB, and a literal is the only rendering that says so: the params gate
        # requires the emitted key set to be EXACTLY union(KB keys, processing keys) and
        # `required_config` is COMPUTED from this source, so a `params.solo[clipAdapterType]` subscript
        # would silently oblige all 11 starsolo specs to declare a value that is the same in all 11.
        # `--outSAMtype` has always been hardcoded here for the same reason. Verified against the
        # STAR 2.7.11b binary that every one is accepted for CB_UMI_Simple AND CB_UMI_Complex -- this
        # is the class of change that passes a 10x-only suite and breaks the four Complex specs.
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
             --limitBAMsortRAM {params.sort_ram} \
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
        f"{OUTDIR}/{{sample}}/{{sample}}.qc.json.gz",
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
