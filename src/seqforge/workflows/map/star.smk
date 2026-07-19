# workflows/map/star.smk  --  HAND-WRITTEN, VERSIONED, CI-TESTED. NEVER machine-generated.
#
# Plain STAR mapping for bulk paired-end RNA-seq (no cell barcode / UMI demultiplex). Selected by the
# composer's module id `map/star`; gene counts come from STAR's own `--quantMode GeneCounts`. The
# genome index resolves at RUN TIME from a `liulab-genome` assembly id.

import csv


def _load_units(path):
    with open(path, newline="") as fh:
        return list(csv.DictReader(fh, delimiter="\t"))


UNITS = _load_units(config["units_tsv"])
SAMPLES = sorted({u["sample_id"] for u in UNITS})
OUTDIR = config["outdir"]
ASSEMBLY = config["genome"]["assembly"]


def fastqs(sample, role):
    # Ordered by the units.tsv `run` column so a pooled sample's two mates pair correctly (STAR
    # desyncs otherwise). `run` is seqforge's own run grouping -- no filename parsing here.
    us = [u for u in UNITS if u["sample_id"] == sample and u["read_id"] == role]
    return [u["path"] for u in sorted(us, key=lambda u: (u["run"], u["path"]))]


def readfilesin(sample, *roles):
    """Render STAR ``--readFilesIn`` for one sample: each role (a mate) is its FASTQs **comma-joined**,
    and the mates are space-separated -- ``mate1_run1,mate1_run2 mate2_run1,mate2_run2``.

    A sample pooled across N sequencing runs passes every run's file for a mate as one comma-list, in
    matching run order for every mate (``fastqs`` preserves units.tsv order). This is STAR's own
    multi-file syntax; joining with spaces instead makes STAR read the extra files as extra mates and
    crash. A single-run sample renders one file per mate, so this generalises to any run count."""
    return " ".join(",".join(fastqs(sample, role)) for role in roles)


rule all:
    input:
        expand(f"{OUTDIR}/{{sample}}/ReadsPerGene.out.tab", sample=SAMPLES),


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


rule star_count:
    """Map one bulk sample's two mates to per-gene counts (STAR GeneCounts)."""
    input:
        mate1=lambda wc: fastqs(wc.sample, config["read_files_in"]["mate1"]),
        mate2=lambda wc: fastqs(wc.sample, config["read_files_in"]["mate2"]),
        index=rules.genome_index.output,
    output:
        f"{OUTDIR}/{{sample}}/ReadsPerGene.out.tab",
    # liulab-runtime's `align-rna`, resolved by compose. See starsolo.smk's note: consuming their
    # artifact, not defining an env, and honoured only under `--software-deployment-method`.
    container: config["container"]
    threads: config["threads"]
    params:
        bulk=config["bulk"],
        prefix=lambda wc: f"{OUTDIR}/{wc.sample}/",
        # each mate is its runs comma-joined, so a sample pooled across runs maps in one pass.
        reads=lambda wc: readfilesin(
            wc.sample, config["read_files_in"]["mate1"], config["read_files_in"]["mate2"]
        ),
    shell:
        r"""
        STAR --runMode alignReads --genomeDir {input.index} --runThreadN {threads} \
             --readFilesIn {params.reads} --readFilesCommand zcat \
             --quantMode {params.bulk[quantMode]} \
             --outFileNamePrefix {params.prefix} \
             --outSAMtype BAM SortedByCoordinate
        """
