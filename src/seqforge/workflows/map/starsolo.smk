# workflows/map/starsolo.smk  --  HAND-WRITTEN, VERSIONED, CI-TESTED. NEVER machine-generated (R1).
#
# STARsolo mapping for barcoded single-cell RNA-seq (10x 3' v2/v3, SPLiT-seq, ...). The composer
# emits `config.yaml` + `units.tsv` and selects this module by id `map/starsolo`; it NEVER writes
# rule source. Every chemistry-defining knob arrives via `config["solo"]` (rendered from the KB's
# backend.params and asserted by compose's params gate); the read->role placement arrives via
# `config["read_files_in"]`, cDNA FIRST.
#
# The genome index resolves at RUN TIME from a `liulab-genome` assembly id (R9/R12) — no genome path
# is ever baked into a config or a manifest, and we do not reimplement liulab-genome's job here.

import csv


def _load_units(path):
    with open(path, newline="") as fh:
        return list(csv.DictReader(fh, delimiter="\t"))


UNITS = _load_units(config["units_tsv"])
SAMPLES = sorted({u["sample_id"] for u in UNITS})
OUTDIR = config["outdir"]
ASSEMBLY = config["genome"]["assembly"]
SOLO = config["solo"]


def fastqs(sample, role):
    return [u["path"] for u in UNITS if u["sample_id"] == sample and u["read_id"] == role]


def whitelists():
    """One path for 10x; three for a split-pool chemistry. The config value is the argv rendering."""
    return SOLO["soloCBwhitelist"].split()


def cb_umi_geometry():
    """Where the CB and UMI live -- and STARsolo spells this two different ways.

    A simple chemistry (10x) has one contiguous barcode, so a start/length pair locates it. A
    combinatorial one (SPLiT-seq) has barcodes scattered between linkers, so each needs a position
    quadruple and no start/length exists to give. This is not a preference: passing --soloCBstart to
    CB_UMI_Complex is an error, and the keys are absent from the config precisely because the
    chemistry has no such value. Compose emits whichever set the soloType implies (the params gate
    proves the block is exactly what its owners declared), so the branch here reads what is there.
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


rule all:
    input:
        expand(f"{OUTDIR}/{{sample}}/Solo.out", sample=SAMPLES),


rule genome_index:
    """Resolve/build the STAR index via liulab-genome at run time (never a path in the manifest)."""
    output:
        directory(f"{OUTDIR}/index/{ASSEMBLY}"),
    params:
        assembly=ASSEMBLY,
        annotation=config["genome"]["annotation"],
    run:
        from pathlib import Path

        from genome import Genome

        index = Genome(params.assembly).build_star_index(gtf=params.annotation)
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
        directory(f"{OUTDIR}/{{sample}}/Solo.out"),
    threads: config["threads"]
    params:
        solo=SOLO,
        geometry=cb_umi_geometry(),
        prefix=lambda wc: f"{OUTDIR}/{wc.sample}/",
    shell:
        # --readFilesIn takes the cDNA read FIRST, then the barcode read (asserted by the params gate).
        r"""
        STAR --runMode alignReads --genomeDir {input.index} --runThreadN {threads} \
             --readFilesIn {input.cdna} {input.barcode} --readFilesCommand zcat \
             --soloType {params.solo[soloType]} \
             {params.geometry} \
             --soloCBwhitelist {input.whitelist} \
             --soloStrand {params.solo[soloStrand]} \
             --soloFeatures {params.solo[soloFeatures]} \
             --outFileNamePrefix {params.prefix} \
             --outSAMtype BAM Unsorted
        """
