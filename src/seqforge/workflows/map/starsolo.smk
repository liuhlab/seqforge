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


def fastqs(sample, role):
    return [u["path"] for u in UNITS if u["sample_id"] == sample and u["read_id"] == role]


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
        whitelist=config["solo"]["soloCBwhitelist"],
    output:
        directory(f"{OUTDIR}/{{sample}}/Solo.out"),
    threads: config["threads"]
    params:
        solo=config["solo"],
        prefix=lambda wc: f"{OUTDIR}/{wc.sample}/",
    shell:
        # --readFilesIn takes the cDNA read FIRST, then the barcode read (asserted by the params gate).
        r"""
        STAR --runMode alignReads --genomeDir {input.index} --runThreadN {threads} \
             --readFilesIn {input.cdna} {input.barcode} --readFilesCommand zcat \
             --soloType {params.solo[soloType]} \
             --soloCBstart {params.solo[soloCBstart]} --soloCBlen {params.solo[soloCBlen]} \
             --soloUMIstart {params.solo[soloUMIstart]} --soloUMIlen {params.solo[soloUMIlen]} \
             --soloCBwhitelist {input.whitelist} \
             --soloStrand {params.solo[soloStrand]} \
             --soloFeatures {params.solo[soloFeatures]} \
             --outFileNamePrefix {params.prefix} \
             --outSAMtype BAM Unsorted
        """
