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

    The Complex branch also pins --soloCBmatchWLtype: STAR's global default is ``1MM_multi``, which it
    REJECTS for CB_UMI_Complex ("does not work with --soloType CB_UMI_Complex"; allowed: Exact / 1MM).
    So a Complex chemistry that named no match type would FATAL at STAR on the default alone -- a run
    that dies on the node, not a thin matrix. ``1MM`` is the tolerant valid mode (one mismatch per
    barcode block), the closest Complex-legal analogue of the Simple default. Confirmed against real BD
    Rhapsody Enhanced reads (#43): the endorsed recipe on STAR #1607 intends this, and its "1MM multi"
    is 1MM for a complex barcode.
    """
    if SOLO["soloType"] == "CB_UMI_Complex":
        return (
            f"--soloCBmatchWLtype 1MM "
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
    """Map one sample's cDNA + barcode reads to a per-cell count matrix."""
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
    params:
        solo=SOLO,
        geometry=cb_umi_geometry(),
        barcode_read_length=barcode_read_length(),
        adapter=adapter_sequence(),
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
        r"""
        STAR --runMode alignReads --genomeDir {input.index} --runThreadN {threads} \
             --readFilesIn {params.reads} --readFilesCommand zcat \
             --soloType {params.solo[soloType]} \
             {params.geometry} \
             {params.adapter} \
             {params.barcode_read_length} \
             --soloCBwhitelist {input.whitelist} \
             --soloStrand {params.solo[soloStrand]} \
             --soloFeatures {params.solo[soloFeatures]} \
             --outFileNamePrefix {params.prefix} \
             --outSAMtype BAM Unsorted
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
    """Convert STAR's Aligned.out.bam to a coordinate-sorted CRAM, then let `temp()` drop the BAM.

    A sibling of `solo_to_h5ad`: both consume `starsolo_count` and nothing else, so snakemake runs
    them in parallel. The reference is resolved at run time from the assembly id via liulab-genome
    (never a baked path); no `embed_ref`, so the CRAM carries the reference MD5 in its header and the
    assembly id is recorded in the QC bundle.

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
    # Declared so the scheduler gates on it AND so the sort gets a real `-m` budget instead of
    # samtools' single-thread default -- more cores and more memory both make the sort finish sooner.
    resources:
        mem_mb=config["mem_mb"],
    params:
        assembly=ASSEMBLY,
    shell:
        r"""
        seqforge io cram --bam {input.bam} --assembly {params.assembly} \
             --out {output.cram} --threads {threads} --sort-mem-mb {resources.mem_mb}
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
