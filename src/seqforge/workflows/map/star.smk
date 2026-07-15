# workflows/map/star.smk  --  HAND-WRITTEN, VERSIONED, CI-TESTED. NEVER machine-generated (R1).
#
# Plain STAR mapping for bulk paired-end RNA-seq (no cell barcode / UMI demultiplex). Selected by the
# composer's module id `map/star`; gene counts come from STAR's own `--quantMode GeneCounts`. The
# genome index resolves at RUN TIME from a `liulab-genome` assembly id (R9/R12).

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
        expand(f"{OUTDIR}/{{sample}}/ReadsPerGene.out.tab", sample=SAMPLES),


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


rule star_count:
    """Map one bulk sample's two mates to per-gene counts (STAR GeneCounts)."""
    input:
        mate1=lambda wc: fastqs(wc.sample, config["read_files_in"]["mate1"]),
        mate2=lambda wc: fastqs(wc.sample, config["read_files_in"]["mate2"]),
        index=rules.genome_index.output,
    output:
        f"{OUTDIR}/{{sample}}/ReadsPerGene.out.tab",
    threads: config["threads"]
    params:
        bulk=config["bulk"],
        prefix=lambda wc: f"{OUTDIR}/{wc.sample}/",
    shell:
        r"""
        STAR --runMode alignReads --genomeDir {input.index} --runThreadN {threads} \
             --readFilesIn {input.mate1} {input.mate2} --readFilesCommand zcat \
             --quantMode {params.bulk[quantMode]} \
             --outFileNamePrefix {params.prefix} \
             --outSAMtype {params.bulk[outSAMtype]}
        """
