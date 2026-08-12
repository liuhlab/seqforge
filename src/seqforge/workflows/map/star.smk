# workflows/map/star.smk  --  HAND-WRITTEN, VERSIONED, CI-TESTED. NEVER machine-generated.
#
# Plain STAR mapping for bulk RNA-seq (no cell barcode / UMI demultiplex), single- or paired-end.
# Selected by the composer's module id `map/star`; gene counts come from STAR's own
# `--quantMode GeneCounts`. The genome index resolves at RUN TIME from a `liulab-genome` assembly id.
#
# The read->role placement arrives via `config["read_files_in"]`, whose `mates` shape is 1..2
# biological mates chosen by ORDER — `mate1`, and `mate2` only when the library has a second one
# (ADR-0029). One module serves both, because the layout kind is a property of the MODULE and STAR
# reads a single-end library as the same `--readFilesIn` with one argument instead of two.

import csv

# seqforge's own helpers, imported rather than restated — the same contract starsolo.smk and
# chromap.smk state at greater length. `ordered_fastqs` decides the order every mate of one sample is
# handed to the aligner in, and all three modules must agree on it exactly: a Snakefile is not
# importable, so three copies of that rule could only ever be checked by running three pipelines.
# `memory` is that same move applied to what this module asks the scheduler for and what it lets STAR
# sort in: two numbers that only mean anything together, so a constant and a closure written here
# could never be unit-tested, only run against a sample deep enough to fail.
from seqforge.workflows.memory import BULK_RETRIES, bam_sort_ram, bulk_mem_mb
from seqforge.workflows.units import ordered_fastqs


def _load_units(path):
    with open(path, newline="") as fh:
        return list(csv.DictReader(fh, delimiter="\t"))


UNITS = _load_units(config["units_tsv"])
SAMPLES = sorted({u["sample_id"] for u in UNITS})
OUTDIR = config["outdir"]
ASSEMBLY = config["genome"]["assembly"]
READ_FILES_IN = config["read_files_in"]


def fastqs(sample, role):
    # `ordered_fastqs` owns the order and the argument for it; the two other mapping modules read the
    # same one. Bulk mates are symmetric, so a mispairing here does not even desync the record lengths
    # -- STAR maps mate1 of lane 1 against mate2 of lane 2 and reports a plausible, wrong rate.
    return ordered_fastqs(UNITS, sample, role)


def mates():
    """The mates this library has, in ORDER -- ``[mate1]`` or ``[mate1, mate2]``.

    The `mates` layout kind is 1..2 reads (ADR-0029), so the composer emits `mate2` only for a library
    that HAS a second biological mate. `mate1` is read with a subscript and `mate2` with `.get`, and
    that difference is the whole mechanism: `keys_read_by` scans this source to decide what compose
    owes every dataset composed against this module, so a subscript here would oblige a single-end
    library to emit a key it does not have -- and the params gate, which forbids a key no owner
    declares, would then refuse the very layout this module was widened to run. Exactly the line
    starsolo.smk draws between `soloBarcodeReadLength` (some chemistries) and `soloCBmatchWLtype`
    (all of them)."""
    second = READ_FILES_IN.get("mate2")
    return [READ_FILES_IN["mate1"], *([] if second is None else [second])]


def readfilesin(sample, *roles):
    """Render STAR ``--readFilesIn`` for one sample: each role (a mate) is its FASTQs **comma-joined**,
    and the mates are space-separated -- ``mate1_run1,mate1_run2 mate2_run1,mate2_run2``.

    A sample pooled across N sequencing runs (or across one run's lanes) passes every such file for a
    mate as one comma-list, in the single order ``fastqs`` imposes on both mates alike. This is STAR's
    multi-file syntax; joining with spaces instead makes STAR read the extra files as extra mates and
    crash. A single-run sample renders one file per mate, so this generalises to any run count.

    Passing ONE role renders one comma-list and no space, which is STAR's single-end form -- the
    single-end case needs no branch here, only a caller that hands it the mates the layout has."""
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
    """Map one bulk sample's mates -- one of them or two -- to per-gene counts (STAR GeneCounts).

    The shell block clears STAR's `_STARtmp` before invoking STAR, so every (re)run is
    preemption-safe: a preempted STAR leaves `results/<sample>/_STARtmp` behind, STAR ABORTS a rerun
    if it already exists, and snakemake cannot remove it (an undeclared output).

    **What this job asks for, and how much of it the sort may claim.** The rule declared NEITHER
    until #377: no `mem_mb`, so the run's largest single allocation was invisible to whatever packs
    the machine, and no `--limitBAMsortRAM`, so a coordinate sort ran on STAR's default of `0` --
    "reuse the genome allocation", which is a budget nobody chose and which tracks the genome's size
    rather than the sample's depth. It is also the one value STAR refuses outright once a run shares
    a single copy of the index, and that refusal fires before the genome directory is read, so it
    would be every sample on the first attempt rather than a slow sample now and then.

    Both numbers follow `attempt`, the arrangement `starsolo_count` already has, and the reasoning is
    NOT the same reasoning. STARsolo escalates against `readInfo`, an allocation that grows with
    every input read and that no `--limit*` flag bounds; bulk counts genes and demultiplexes nothing,
    so it holds no such array. What a retry buys HERE is a deeper sample's coordinate sort against a
    budget one multiple of the figure larger -- depth alone. The count is `BULK_RETRIES` and not
    STARsolo's for exactly that reason: one workflow's headroom may not be a function of the other's.
    The arithmetic and the measurements behind it live in `workflows/memory.py`.
    """
    input:
        mate1=lambda wc: fastqs(wc.sample, mates()[0]),
        # EMPTY for a single-end library, which is a declared input of no files rather than a missing
        # one: snakemake takes an empty list happily, while a `mate2` naming a role no unit carries
        # would resolve to nothing under a name that claims something.
        mate2=lambda wc: [f for role in mates()[1:] for f in fastqs(wc.sample, role)],
        index=rules.genome_index.output,
    output:
        f"{OUTDIR}/{{sample}}/ReadsPerGene.out.tab",
    # liulab-runtime's `align-rna`, resolved by compose. See starsolo.smk's note: consuming their
    # artifact, not defining an env, and honoured only under `--software-deployment-method`.
    container: config["container"]
    threads: config["threads"]
    # `retries:` and `resources:` are ONE mechanism, so they are read together. `config["mem_mb"]`
    # appears as a literal subscript deliberately -- `workflows/__init__.py::keys_read_by` SCANS this
    # source to compute `required_config`, and a key the scanner cannot see is a key the composer is
    # not obliged to emit, i.e. a KeyError on a compute node long after compose exited 0.
    #
    # THE SORT CAP IS A `resources:` ENTRY, NOT A `params:` ONE. That is the only construct snakemake
    # re-evaluates per attempt: `Job.attempt`'s setter clears `self._resources` and NOT `self._params`
    # (measured on the pinned 9.23.1), so a `params:` callable is expanded once, on attempt 1, and
    # every retry reuses it verbatim -- the request escalates and the cap stays where the first
    # attempt died, which is worse than not retrying at all. The name carries its unit because it is
    # the one number here that is not MiB: STAR takes `--limitBAMsortRAM` in bytes, and a resource is
    # a bare integer with nowhere else to say so.
    retries: BULK_RETRIES
    resources:
        mem_mb=lambda wildcards, attempt: bulk_mem_mb(config["mem_mb"], attempt),
        bam_sort_ram_bytes=lambda wildcards, attempt: bam_sort_ram(
            bulk_mem_mb(config["mem_mb"], attempt)
        ),
    params:
        bulk=config["bulk"],
        prefix=lambda wc: f"{OUTDIR}/{wc.sample}/",
        # each mate is its runs comma-joined, so a sample pooled across runs maps in one pass; only
        # the mates the layout HAS are passed, so a single-end library renders STAR's single-end form.
        reads=lambda wc: readfilesin(wc.sample, *mates()),
    shell:
        r"""
        # preemption-safe: STAR aborts a rerun if _STARtmp exists (undeclared, snakemake cannot remove it)
        rm -rf {params.prefix}_STARtmp
        STAR --runMode alignReads --genomeDir {input.index} --runThreadN {threads} \
             --readFilesIn {params.reads} --readFilesCommand zcat \
             --quantMode {params.bulk[quantMode]} \
             --outFileNamePrefix {params.prefix} \
             --outSAMtype BAM SortedByCoordinate \
             --limitBAMsortRAM {resources.bam_sort_ram_bytes}
        """
