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
    """Resolve/build the STAR index via liulab-genome at run time (never a path in the manifest).

    **No `container:`, and that is a measured fact rather than an oversight.** Snakemake wraps a
    container around a `shell:` command (in `shell.py`); a `run:` block executes Python in the
    snakemake process and never passes through that wrap, so a `container:` here would be accepted
    and silently ignored. Snakemake's own linter agrees -- it excludes `is_run` rules from
    "missing software definition".

    So this rule borrows the ambient STAR, and only when it has to: liulab-genome caches the index and
    `build_star_index` re-runs `genomeGenerate` only if there is no cached one. On a machine where
    LIULAB_DATA is populated -- the normal case -- no STAR is invoked here at all, and the container on
    the alignment rule pins the aligner that does the work. On a fresh machine the first run needs a
    STAR on PATH. If that STAR and the container's disagree on index version, STAR refuses loudly,
    which is the failure mode we can live with.

    The deeper reason not to fight this: the index is **liulab-genome's artifact** (R12). How it gets
    built, and in what environment, is theirs. We consume it.
    """
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
    # liulab-runtime's `align-rna`, resolved by compose. See starsolo.smk's note: consuming their
    # artifact, not defining an env (R12), and honoured only under `--software-deployment-method`.
    container: config["container"]
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
             --outSAMtype BAM SortedByCoordinate
        """
