# workflows/map/chromap.smk  --  HAND-WRITTEN, VERSIONED, CI-TESTED. NEVER machine-generated.
#
# chromap mapping for barcoded scATAC (10x Multiome ATAC, 10x scATAC). The composer emits
# `config.yaml` + `units.tsv` and selects this module by id `map/chromap`; it NEVER writes rule source.
# The byte-decided parse knob (the barcode whitelist) arrives via `config["chromap"]` (rendered from the
# KB's backend.params and asserted by compose's params gate); the read->role placement arrives via
# `config["read_files_in"]` — two GENOMIC mates (`gdna1`, `gdna2`) and a `barcode` read, the ATAC shape
# of the `atac_barcoded` read layout.
#
# The deliverable is a tabix-indexed `fragments.tsv.gz`, NOT a count matrix — there are no genes to
# count in ATAC. The genome index resolves at RUN TIME from a `liulab-genome` assembly id via
# `get_chromap_index` (the R10 analog of starsolo's `get_star_index`); no genome path is ever baked into
# a config or a manifest, and chromap needs no gene annotation, so there is exactly one index per
# assembly.

import csv

# seqforge's own helpers, imported rather than restated — same contract as starsolo.smk importing from
# `h5ad`: `fragments_suffixes` decides both what the finalize rules DECLARE below and (via the
# `seqforge io fragments*` verbs) what gets written, so the two cannot drift. The import is the same
# assumption `rule genome_index` makes of `genome`: the env running snakemake is the env that has them.
from seqforge.workflows.fragments import RAW_FRAGMENTS, fragments_suffixes


def _load_units(path):
    with open(path, newline="") as fh:
        return list(csv.DictReader(fh, delimiter="\t"))


UNITS = _load_units(config["units_tsv"])
SAMPLES = sorted({u["sample_id"] for u in UNITS})
OUTDIR = config["outdir"]
ASSEMBLY = config["genome"]["assembly"]
CHROMAP = config["chromap"]


def fastqs(sample, role):
    # Ordered by the units.tsv `run` column so a pooled sample's mates pair correctly, exactly as
    # starsolo.smk's `fastqs` does: chromap reads -1/-2/-b mate-by-mate and desyncs if genomic run K is
    # joined with barcode run J. `run` is seqforge's own run grouping — no filename parsing here.
    us = [u for u in UNITS if u["sample_id"] == sample and u["read_id"] == role]
    return [u["path"] for u in sorted(us, key=lambda u: (u["run"], u["path"]))]


def commajoin(sample, role):
    """One mate's FASTQs comma-joined, as chromap's -1/-2/-b take a multi-file mate (STAR-style)."""
    return ",".join(fastqs(sample, role))


def whitelist():
    """The materialized barcode whitelist path — the config value is the argv rendering.

    `config["chromap"]["barcode_whitelist"]` is `onlists/<name>.txt`, built on demand by `rule onlist`
    and `temp()`-deleted after — the same packed-onlist discipline starsolo.smk uses for
    `soloCBwhitelist`, so a 111 MB whitelist is never written into the run directory at compile time.
    """
    return CHROMAP["barcode_whitelist"]


rule all:
    input:
        expand(
            f"{OUTDIR}/{{sample}}/{{sample}}{{suffix}}",
            sample=SAMPLES,
            suffix=fragments_suffixes(),
        ),


rule onlist:
    """Materialize one barcode whitelist for chromap to read once and snakemake to then delete.

    Byte-for-byte the same rule as starsolo.smk's — a barcode whitelist is a barcode whitelist. `temp()`
    is the point: the ARC list is materialized on demand and deleted when the last job needing it is
    done, never written into the run directory at compile time. No `container:`: this runs `seqforge`,
    not an aligner, so the ambient compose environment already has it.
    """
    output:
        temp("onlists/{name}.txt"),
    localrule: True
    shell:
        "seqforge io onlist write {wildcards.name} --out {output}"


rule genome_index:
    """Resolve the chromap index + reference FASTA via liulab-genome at run time (never a baked path).

    Only **looks up**; never builds. `get_chromap_index()` returns the index liulab-genome already built
    for this assembly and RAISES if none exists — the index is liulab-genome's artifact, built ahead of
    the run by its own machinery. chromap maps against both the index (`-x`) and the reference (`-r`), so
    both are symlinked in. No `gtf`: a chromap index carries no annotation, so one serves the assembly.

    A `run:` block, so — like starsolo's `genome_index` — it needs no tool on PATH and no `container:`
    (snakemake wraps a container around a `shell:`, never a `run:`).
    """
    output:
        index=f"{OUTDIR}/index/{ASSEMBLY}/chromap.index",
        fasta=f"{OUTDIR}/index/{ASSEMBLY}/genome.fa",
    params:
        assembly=ASSEMBLY,
    run:
        from pathlib import Path

        from genome import Genome

        g = Genome(params.assembly)
        index = g.get_chromap_index()
        fasta = g.fasta_path
        out_index = Path(output.index)
        out_index.parent.mkdir(parents=True, exist_ok=True)
        out_index.symlink_to(index)
        Path(output.fasta).symlink_to(fasta)


rule chromap_align:
    """Map one sample's two genomic mates + barcode read to a raw scATAC fragments file.

    `--preset atac` applies chromap's ATAC defaults (Tn5 shift, low-MAPQ trimming, per-cell PCR-dup
    removal) BEFORE any other flag, exactly as chromap intends a preset to be used. The barcode read is a
    separate FASTQ (`-b`), corrected against the ARC whitelist; the two genomic mates are `-1`/`-2`. The
    raw fragments file is `temp()` — `fragments_finalize` bgzips+indexes it into the retained deliverable.
    """
    input:
        gdna1=lambda wc: fastqs(wc.sample, config["read_files_in"]["gdna1"]),
        gdna2=lambda wc: fastqs(wc.sample, config["read_files_in"]["gdna2"]),
        barcode=lambda wc: fastqs(wc.sample, config["read_files_in"]["barcode"]),
        index=rules.genome_index.output.index,
        fasta=rules.genome_index.output.fasta,
        whitelist=whitelist(),
    output:
        fragments=temp(f"{OUTDIR}/{{sample}}/{RAW_FRAGMENTS}"),
    # The pinned aligner: liulab-runtime's `align-dna`, resolved by compose to a ghcr tag or a prebuilt
    # .sif. Naming it here CONSUMES liulab-runtime's artifact — no conda YAML, no Dockerfile, no chromap
    # in any dependency table of ours.
    container: config["container"]
    threads: config["threads"]
    params:
        reads1=lambda wc: commajoin(wc.sample, config["read_files_in"]["gdna1"]),
        reads2=lambda wc: commajoin(wc.sample, config["read_files_in"]["gdna2"]),
        barcodes=lambda wc: commajoin(wc.sample, config["read_files_in"]["barcode"]),
        # The byte-decided barcode geometry: where the 16 bp cell barcode sits inside the barcode read
        # and on which strand (`bc:START:END:STRAND`, 0-based inclusive). Derived by compose from the CB
        # element coordinates + the ARC ATAC whitelist orientation, so `--preset atac`'s default
        # `bc:0:15:+` never silently mis-reads the 10x Multiome ATAC lead-in + reverse-complement.
        read_format=CHROMAP["read_format"],
    shell:
        r"""
        chromap --preset atac -t {threads} \
             -x {input.index} -r {input.fasta} \
             --read-format {params.read_format} \
             -1 {params.reads1} -2 {params.reads2} -b {params.barcodes} \
             --barcode-whitelist {input.whitelist} \
             -o {output.fragments}
        """


rule fragments_finalize:
    """Sort + bgzip + tabix chromap's raw fragments into the retained `fragments.tsv.gz` (+ `.tbi`).

    A `shell:` calling a seqforge verb, not a `run:`, so `snakemake -n -p` (compose's wiring gate) sees
    it — same reason as starsolo's `solo_to_h5ad`. `container:`, because the verb shells to htslib
    (`bgzip`/`tabix`), which lives in the `align-dna` image, not in the submitting shell.
    """
    input:
        raw=rules.chromap_align.output.fragments,
    output:
        gz=f"{OUTDIR}/{{sample}}/{{sample}}.fragments.tsv.gz",
        tbi=f"{OUTDIR}/{{sample}}/{{sample}}.fragments.tsv.gz.tbi",
    container: config["container"]
    shell:
        "seqforge io fragments --raw {input.raw} --out {output.gz}"


rule fragments_qc:
    """Summarize the fragments file into one gzipped JSON — the ATAC analog of starsolo's `qc_bundle`.

    Pure Python over the fragments text (no external binary), so no `container:` — same as `solo_to_h5ad`
    and unlike `fragments_finalize`.
    """
    input:
        gz=rules.fragments_finalize.output.gz,
    output:
        f"{OUTDIR}/{{sample}}/{{sample}}.fragments.qc.json.gz",
    params:
        assembly=ASSEMBLY,
    shell:
        r"""
        seqforge io fragments-qc --fragments {input.gz} --sample {wildcards.sample} \
             --assembly {params.assembly} --out {output}
        """
