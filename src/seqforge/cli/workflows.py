"""`seqforge io`, the half that fronts the ``workflows`` package: the steps a compiled pipeline runs.

A rule file's ``shell:`` calls these after alignment: ``h5ad``, ``qc-bundle``, ``fragments``,
``fragments-qc``, ``cram``, ``umi-extract``, ``umi-count`` and ``split-chimera``. They touch no
network. They register on the ``io`` group, beside the network verbs in :mod:`seqforge.cli.io`,
because every pipeline directory already compiled calls them as ``seqforge io <verb>``.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import NoReturn, cast

import typer

from ..workflows.cram import CramError, RecordSelection, bam_to_cram
from .root import io_app


@io_app.command("h5ad")
def io_h5ad(
    solo_dir: Path = typer.Option(..., "--solo-dir", help="A STARsolo `Solo.out` directory."),
    features: str = typer.Option(
        ..., "--features", help="The run's --soloFeatures, space-separated (e.g. 'Gene GeneFull')."
    ),
    primary: str = typer.Option(
        ..., "--primary", help="Which feature becomes X (the rest become layers)."
    ),
    out_prefix: Path = typer.Option(
        ..., "--out-prefix", help="Output path prefix; '.h5ad' / '.velocyto.h5ad' are appended."
    ),
) -> None:
    """Package a Solo.out's raw matrices as .h5ad — the last step of the composed pipeline.

    Called by `starsolo.smk`'s `solo_to_h5ad` rule, which is why it is a verb and not a `run:` block:
    a `shell:` is rendered by `snakemake -n -p`, so compose's wiring gate can see it.

    Exit 3 on a Blocker-shaped refusal — the axes of the features being stacked disagree, or a matrix
    STAR was supposed to write is absent.
    """
    from ..models.processing import SoloFeature
    from ..workflows.h5ad import SOLO_FEATURE_OUTPUT, H5adError, write_h5ad

    requested = features.split()
    unknown = [f for f in [*requested, primary] if f not in SOLO_FEATURE_OUTPUT]
    if unknown:
        typer.echo(
            json.dumps({"error": f"unknown --soloFeatures value(s): {sorted(set(unknown))}"}),
            err=True,
        )
        raise typer.Exit(2)
    try:
        written = write_h5ad(
            solo_dir,
            cast(list[SoloFeature], requested),
            cast(SoloFeature, primary),
            out_prefix,
        )
    except H5adError as exc:
        typer.echo(json.dumps({"error": str(exc)}), err=True)
        raise typer.Exit(3) from exc
    typer.echo(json.dumps({"written": [str(p) for p in written]}, indent=2))


@io_app.command("qc-bundle")
def io_qc_bundle(
    run_dir: Path = typer.Option(
        ..., "--run-dir", help="The sample directory holding STAR's Log.*.out / SJ.out.tab."
    ),
    sample: str = typer.Option(..., "--sample", help="Sample id, recorded in the bundle."),
    out: Path = typer.Option(..., "--out", help="Output path for the gzipped JSON bundle."),
    assembly: str | None = typer.Option(
        None, "--assembly", help="UCSC assembly id, recorded for CRAM-reference provenance."
    ),
    solo_dir: Path | None = typer.Option(
        None, "--solo-dir", help="A STARsolo `Solo.out` directory. Droplet runs only."
    ),
    features: str | None = typer.Option(
        None, "--features", help="The run's --soloFeatures, space-separated. Droplet runs only."
    ),
    extract: Path | None = typer.Option(
        None, "--extract", help="One plate cell's UMI-extraction summary. Plate runs only."
    ),
    split: Path | None = typer.Option(
        None, "--split", help="One plate cell's chimera split summary. Chimeric plate runs only."
    ),
) -> None:
    """Bundle one sample's stats + run logs into one gzipped JSON — a finalize step of the pipeline.

    ONE verb for one artifact kind, with one suffix and one owner. `--solo-dir` and `--features` are
    a droplet sample's, `--extract` and `--split` are a plate cell's, and which pair is given decides
    which bundle is built — a second verb would be a second surface for one artifact. Giving NEITHER
    pair is the bulk shape, and that is a claim rather than a fallback: a bulk sample demultiplexes
    nothing, so it has no summary to name and its record is the aligner's own. Called by the
    `qc_bundle` rule of every STAR module (a `shell:`, so compose's wiring gate sees it). Exit 2 on a
    mixed or incomplete pair, exit 3 if a file the pipeline was supposed to write is missing.
    """
    from ..models.processing import SoloFeature
    from ..workflows.h5ad import SOLO_FEATURE_OUTPUT
    from ..workflows.qc import (
        QcError,
        write_bulk_qc_bundle,
        write_plate_qc_bundle,
        write_qc_bundle,
    )

    def refuse(message: str) -> NoReturn:
        typer.echo(json.dumps({"error": message}), err=True)
        raise typer.Exit(2)

    droplet = [n for n, v in (("--solo-dir", solo_dir), ("--features", features)) if v is not None]
    plate = [n for n, v in (("--extract", extract), ("--split", split)) if v is not None]
    if droplet and plate:
        refuse(
            f"{droplet} is a droplet sample's and {plate} a plate cell's; "
            f"one bundle is one shape or the other"
        )
    if droplet and len(droplet) < 2:
        refuse("a droplet bundle needs both --solo-dir and --features")
    if split is not None and extract is None:
        refuse("a plate bundle needs --extract, the cell's UMI-extraction summary")

    try:
        if solo_dir is not None and features is not None:
            requested = features.split()
            unknown = [f for f in requested if f not in SOLO_FEATURE_OUTPUT]
            if unknown:
                refuse(f"unknown --soloFeatures value(s): {sorted(set(unknown))}")
            written = write_qc_bundle(
                solo_dir,
                run_dir,
                cast(list[SoloFeature], requested),
                out,
                sample=sample,
                assembly=assembly,
            )
        elif extract is not None:
            written = write_plate_qc_bundle(
                run_dir,
                out,
                sample=sample,
                assembly=assembly,
                extract=extract,
                split=split,
            )
        else:
            written = write_bulk_qc_bundle(run_dir, out, sample=sample, assembly=assembly)
    except QcError as exc:
        typer.echo(json.dumps({"error": str(exc)}), err=True)
        raise typer.Exit(3) from exc
    typer.echo(json.dumps({"written": str(written)}, indent=2))


@io_app.command("fragments")
def io_fragments(
    raw: Path = typer.Option(..., "--raw", help="chromap's raw (unsorted) fragments file."),
    out: Path = typer.Option(
        ...,
        "--out",
        help="Output path for the bgzipped fragments.tsv.gz (its .tbi lands beside it).",
    ),
) -> None:
    """Finalize chromap's fragments into a sorted, bgzipped, tabix-indexed `fragments.tsv.gz`.

    Called by `chromap.smk`'s `fragments_finalize` rule (a `shell:`, so compose's wiring gate sees it) —
    the ATAC analog of `io h5ad`. Shells to htslib (`bgzip`/`tabix`) from the `align-dna` container. Exit
    3 if chromap's raw output is missing or htslib is unavailable.
    """
    from ..workflows.fragments import FragmentsError, write_fragments

    try:
        written = write_fragments(raw, out)
    except FragmentsError as exc:
        typer.echo(json.dumps({"error": str(exc)}), err=True)
        raise typer.Exit(3) from exc
    typer.echo(json.dumps({"written": str(written)}, indent=2))


@io_app.command("fragments-qc")
def io_fragments_qc(
    fragments: Path = typer.Option(
        ..., "--fragments", help="A fragments file (plain .tsv or bgzipped .tsv.gz)."
    ),
    sample: str = typer.Option(..., "--sample", help="Sample id, recorded in the summary."),
    assembly: str = typer.Option(
        ..., "--assembly", help="UCSC assembly id, recorded in the summary."
    ),
    out: Path = typer.Option(..., "--out", help="Output path for the gzipped JSON summary."),
) -> None:
    """Summarize a fragments file into one gzipped JSON — the ATAC analog of `io qc-bundle`.

    Called by `chromap.smk`'s `fragments_qc` rule. Pure Python over the fragments text (no external
    binary), so it needs no container. Exit 3 on a malformed fragments file.
    """
    from ..workflows.fragments import FragmentsError, write_fragments_qc

    try:
        written = write_fragments_qc(fragments, out, sample=sample, assembly=assembly)
    except FragmentsError as exc:
        typer.echo(json.dumps({"error": str(exc)}), err=True)
        raise typer.Exit(3) from exc
    typer.echo(json.dumps({"written": str(written)}, indent=2))


@io_app.command("cram")
def io_cram(
    bam: Path = typer.Option(..., "--bam", help="STAR's Aligned.sortedByCoord.out.bam."),
    assembly: str = typer.Option(..., "--assembly", help="UCSC assembly id; the CRAM reference."),
    out: Path = typer.Option(..., "--out", help="Output CRAM path ('.crai' is written beside it)."),
    threads: int = typer.Option(1, "--threads", help="samtools view/index threads."),
    selection: RecordSelection = typer.Option(
        RecordSelection.primary,
        "--selection",
        help="Which records the archive is of: primary (default) | mapped (also drops what never "
        "aligned) | unique | multi (the two halves of the mappability partition).",
    ),
) -> None:
    """Encode STAR's coordinate-sorted BAM as a CRAM against the liulab-genome reference.

    Called by `starsolo.smk`'s `solo_to_cram` rule. The reference FASTA is resolved at run time from
    the assembly id via `liulab-genome` (never a baked path), exactly as the module's own
    `star_index` resolves the genomeDir. Exit 3 on a samtools failure or an unresolvable reference.

    `--selection` names which records the archive is of, and it defaults to what this verb has always
    written: primary alignments. A caller wanting a narrower archive — the mapped records, or one side
    of the mappability partition — names one instead of building a pipe beside this one.

    **It sorts nothing, and there is no memory knob, because there is no longer a sort.** The module
    asks STAR for `BAM SortedByCoordinate` — the only output STAR will put `CB`/`UB` in — so the BAM
    arrives in order and what remains is a streaming pipe: keep the selected records, rewrite each
    read name to `r<N>`, encode CRAM. Nothing here buffers, so nothing here needs a budget. That
    deleted a whole re-sort pass over every BAM, and with it the `samtools.<pid>.<tid>.tmp.*` files a
    killed sort left in the pipeline directory: they were undeclared outputs, so snakemake could not
    clean them, and 41 GiB of them had accumulated across five run directories. Passing `-T` to a
    snakemake-owned directory would have fixed that too; a step that cannot leak beats a step that
    must be configured not to.
    """
    try:
        from genome import (
            Genome,  # untyped lab package; resolved here, off the strict workflow path
        )
    except ImportError as exc:  # pragma: no cover - depends on the host
        typer.echo(json.dumps({"error": f"liulab-genome is not importable: {exc}"}), err=True)
        raise typer.Exit(3) from exc
    try:
        fasta = Path(str(Genome(assembly).fasta_path))
        written = bam_to_cram(bam, fasta, out, threads=threads, selection=selection)
    except CramError as exc:
        typer.echo(json.dumps({"error": str(exc)}), err=True)
        raise typer.Exit(3) from exc
    typer.echo(json.dumps({"written": str(written)}, indent=2))


@io_app.command("umi-count")
def io_umi_count(
    cells: list[str] = typer.Argument(
        ..., help="One `sample_id=/path/to.bam` per cell, in the h5ad's row order."
    ),
    assembly: str = typer.Option(
        ..., "--assembly", help="UCSC assembly id the cells were mapped to."
    ),
    annotation: str | None = typer.Option(
        None,
        "--annotation",
        help="Registered GTF name; its built database is what is read. Exactly one of this and "
        "--component.",
    ),
    component: str | None = typer.Option(
        None,
        "--component",
        help="Count this Component of a Chimera against what it contributed to the merge, read off "
        "the Chimera's completion record. Exactly one of this and --annotation.",
    ),
    out: Path = typer.Option(
        ..., "--out", help="Output .h5ad path — one object for the whole plate."
    ),
    threads: int = typer.Option(
        1, "--threads", help="How many cells to count at once. 1 counts the plate on one core."
    ),
) -> None:
    """Count every cell of a plate into ONE .h5ad — the fan-in of the SMART-seq3 pipeline.

    One counting job over all N per-cell BAMs, not N jobs and a merge, and it writes the object
    directly: a whole plate as a dense TSV is hundreds of megabytes for a sparse object several times
    smaller. `X` is the exonic UMI matrix and the layers carry the rest — intronic UMIs, the two
    together deduplicated as one population, and the untagged-read matrices. Rows are sample ids and
    the four read fates are `obs` columns. Which matrices exist is stated once, in the table on
    `workflows.umite.count`, and deliberately not counted again here.

    A verb rather than a `run:` block for the same reason `io h5ad` is one: `snakemake -n -p`
    renders a `shell:` while planning and cannot see inside a `run:`, so only a verb is visible to
    compose's wiring gate. The counter takes **no container** — pysam and gffutils are plain
    dependencies, like anndata, and only the aligner needs an environment we do not own.

    The annotation database is resolved here rather than in the workflow module, exactly as `io
    cram` resolves the reference FASTA: liulab-genome registers a GTF as `<name>.gtf` beside the
    `<name>.db` it builds from it, and exposes the first, so the second is derived from it and the
    module below stays strictly typed and testable against a synthetic annotation. Exit 2 on a
    malformed cell argument, or on a command line naming neither annotation form or both; exit 3 on
    an unresolvable annotation, an uncountable Component, or a missing BAM.

    **`--component` counts one Component of a Chimera against what THAT Component contributed to the
    merge**, and exactly one of the two forms is required: both is a caller who believes two
    different things about which annotation this is, neither is a caller who has said nothing, and
    each is a bad invocation rather than something the environment refused. A Chimera's merged
    annotation deliberately does not record which Components fed it, so the per-Component registered
    name is not offline-recoverable — nobody can type it and compose cannot put it in a config — and
    it is read off the Chimera's completion record here, at run time. Deliberately not
    `Genome(component).default_gtf`: a Component's default *now* is not necessarily what went into
    this merge, and which annotation a per-Component count is taken against is a decision. The GTF
    is registered under the COMPONENT rather than the Chimera, so the pair handed to the resolution
    below is the Component's own and an `--annotation` caller's route is the one it always was. A
    Component that contributed no annotation is a refusal naming it — a spike-in or a plasmid
    Component ships no GTF — because an uncountable Component is named rather than counted against
    nothing; the cost is *when*, since this verb is the fan-in and the run dies after the whole
    plate has mapped. Rejected: `--component` silently overriding `--annotation`, which leaves a
    rendered command line saying two contradictory things about which GTF was used, in a repo whose
    wiring gate reads rendered commands.

    **Which of that annotation's genes are ribosomal is looked up here too, and deliberately after
    the pair above is settled.** The counter is handed gene ids rather than a category name, for the
    same reason it is handed a resolved database: the package that knows which genes are which is the
    one this verb already imports, and the module below stays testable without it. Asking after the
    `--component` rewrite is what keeps the answer one organism's — the same question put to a
    Chimera concatenates every Component's ids, and a ribosomal share pooling two organisms is a
    share of a population nobody asked about, while this verb is already invoked once per Component.
    An annotation no curated list ships for, or one whose list declares no ribosomes, is not a
    refusal: it costs the plate that one `obs` column and never a zero one, which is what an
    unmeasured thing looks like everywhere else on this path. A curated file that is present and
    broken is a different thing, and stays the refusal below.

    **`--threads` counts cells at once, and defaults to one.** The counting rule asks the scheduler
    for threads and hands them over here; a hand invocation that says nothing gets the single-core
    plate it used to get. The cells are independent and the annotation is read once before the
    fan-out, so the width buys close to itself — and the h5ad's rows stay in the order the cells were
    given on the command line, whatever order they finish in.
    """
    from ..workflows.umite.count import UmiCountError, parse_cells, write_umi_counts

    # Argv alone decides which of the two forms named the annotation, so the pair is settled first
    # and for free: a command line saying two contradictory things should not first buy a genome
    # lookup, and neither should one that never said which annotation it meant at all.
    if annotation is not None and component is not None:
        typer.echo(
            json.dumps(
                {
                    "error": f"--annotation {annotation} and --component {component} name two "
                    f"different annotations; give exactly one"
                }
            ),
            err=True,
        )
        raise typer.Exit(2)
    if annotation is None and component is None:
        typer.echo(
            json.dumps(
                {
                    "error": "one of --annotation (a registered GTF name) or --component (a "
                    "Component of a Chimera, resolved off its completion record) is required"
                }
            ),
            err=True,
        )
        raise typer.Exit(2)

    # The cells are parsed before anything reaches the environment: a typo in an argument is a bad
    # invocation and should not first cost an assembly lookup that may not even be resolvable here.
    try:
        plate = parse_cells(cells)
    except UmiCountError as exc:
        typer.echo(json.dumps({"error": str(exc)}), err=True)
        raise typer.Exit(2) from exc
    try:
        # untyped lab package; resolved here, off the strict workflow path. The two absences are
        # imported by name because they have to be CAUGHT by name -- see the lookup below.
        from genome import GeneCategoryNotDeclaredError, Genome, NoGeneCategoriesError
    except ImportError as exc:  # pragma: no cover - depends on the host
        typer.echo(json.dumps({"error": f"liulab-genome is not importable: {exc}"}), err=True)
        raise typer.Exit(3) from exc
    try:
        if component is not None:
            # `None` for an assembly that is not a chimera, so `--component` against a plain
            # assembly lands in the same refusal as a Component that contributed nothing: neither
            # can be counted, and both are the caller naming a Component this merge does not have.
            contributed = Genome(assembly).component_annotations or {}
            registered = contributed.get(component)
            if registered is None:
                countable = sorted(name for name, gtf_name in contributed.items() if gtf_name)
                typer.echo(
                    json.dumps(
                        {
                            "error": f"{component} contributed no annotation to {assembly}, so "
                            f"there is nothing to count it against; countable: {countable}"
                        }
                    ),
                    err=True,
                )
                raise typer.Exit(3)
            # A Component's GTF is registered under the COMPONENT, not under the Chimera, so the
            # pair becomes the Component's own and the resolution below is the single one both
            # forms take -- and the refusal below names the Component whose GTF went missing.
            assembly, annotation = component, registered
        # Bound once, AFTER the pair above is settled, so both lookups below are put to the same
        # genome -- under `--component` that is the Component's own and never the Chimera's.
        genome = Genome(assembly)
        # The gate above leaves exactly one of the two forms and the arm above has filled the
        # annotation in; the checker cannot see a relation between two options, so it is said here.
        gtf = Path(str(genome.annotations.path(cast(str, annotation))))
        # Which of this annotation's genes are ribosomal, asked of that same settled pair -- what a
        # Chimera would answer instead, and why it may not be asked, is in the docstring above.
        try:
            rrna_gene_ids = genome.gene_list("rRNA", annotation).gene_ids
        except (GeneCategoryNotDeclaredError, NoGeneCategoriesError):
            # No curated list ships for this annotation, or one does and does not declare
            # ribosomes: either way the metric is unmeasurable here, which costs the plate that
            # column and never a zero one. Caught by NAME rather than as `LookupError`, which an
            # unregistered annotation also raises -- swallowing that would turn the refusal below
            # into a silently missing column, and a defect in a shipped curated file lands there
            # too, as the `ValueError` it is.
            rrna_gene_ids = None
        written = write_umi_counts(
            plate, gtf.with_suffix(".db"), out, workers=threads, rrna_gene_ids=rrna_gene_ids
        )
    except UmiCountError as exc:
        typer.echo(json.dumps({"error": str(exc)}), err=True)
        raise typer.Exit(3) from exc
    except (KeyError, ValueError) as exc:  # liulab-genome refuses an unregistered annotation name
        typer.echo(
            json.dumps({"error": f"{annotation} is not registered for {assembly}: {exc}"}), err=True
        )
        raise typer.Exit(3) from exc
    typer.echo(json.dumps({"written": str(written)}, indent=2))


@io_app.command("split-chimera")
def io_split_chimera(
    outputs: list[str] = typer.Argument(
        ..., help="One `component=/path/to.bam` per Component, e.g. `ce11=ce11.bam ecHT115=ec.bam`."
    ),
    bam: Path = typer.Option(..., "--bam", help="The chimera-mapped, coordinate-sorted BAM."),
    assembly: str = typer.Option(
        ...,
        "--assembly",
        help="The Chimera's assembly id. Its completion record — never its name — says which "
        "Components it holds and which separator its chromosome names carry.",
    ),
    summary: Path | None = typer.Option(
        None,
        "--summary",
        help="Where to write this split's counts as JSON. Omitted, the same numbers go to stdout "
        "only — which on a cluster means a scheduler log and nowhere else.",
    ),
    threads: int = typer.Option(
        1,
        "--threads",
        help="BGZF codec threads, divided across the outputs. The record loop is one pass and "
        "stays on one core; this is the compression underneath it.",
    ),
) -> None:
    """Split one chimera-mapped BAM into one BAM per Component, each spelled for a single assembly.

    A Chimera's chromosome names carry a Component suffix, which is what makes the alignment
    interpretable and every artifact downstream of it unusable: `chrI__ce11` is not `chrI`, so a
    browser, a counter and the user's own scripts all refuse it. This is the undo, and it is
    hand-runnable on any chimeric BAM with no pipeline involved.

    Outputs arrive positionally as `<component>=<path>`, the shape `io umi-count` already takes its
    cells in, because a second spelling of a path is the copy that goes stale. Every Component the
    Chimera holds must be named: a partial request is refused rather than served, since served it
    writes exactly the file that was asked for while reads-in-equals-reads-out quietly stops closing.

    `--assembly` is resolved **authoritatively** off the Chimera's completion record, which is the
    only honest source for both facts it supplies. The Component list is not readable from the
    assembly's name — a derived name is a convention and a record is a fact — and the separator
    belongs to one chimera, because a Component whose own chromosome names hold a doubled underscore
    forces a longer run than the default. Resolved here rather than in the workflow module, exactly
    as `io cram` resolves the reference FASTA: what the module does is a pure function of the BAM,
    the outputs and the separator, so it stays strictly typed and testable with no built reference on
    disk.

    Exit 2 on a malformed argument, exit 3 on a refusal or an assembly that will not resolve.
    """
    from ..workflows.split import SplitError, parse_outputs, split_chimera

    # The outputs are parsed before anything reaches the environment: a typo in an argument is a bad
    # invocation and should not first cost an assembly lookup that may not be possible on this host.
    try:
        requested = parse_outputs(outputs)
    except SplitError as exc:
        typer.echo(json.dumps({"error": str(exc)}), err=True)
        raise typer.Exit(2) from exc
    try:
        from genome import (
            Genome,  # the lab package's genome store; resolved here, off the strict workflow path
        )
    except ImportError as exc:  # pragma: no cover - depends on the host
        typer.echo(json.dumps({"error": f"liulab-genome is not importable: {exc}"}), err=True)
        raise typer.Exit(3) from exc
    try:
        chimera = Genome(assembly)
        components, separator = chimera.components, chimera.separator
    except (KeyError, ValueError, OSError) as exc:  # liulab-genome refuses an unknown assembly
        typer.echo(json.dumps({"error": f"{assembly} does not resolve: {exc}"}), err=True)
        raise typer.Exit(3) from exc
    if components is None or separator is None:
        typer.echo(
            json.dumps(
                {
                    "error": f"{assembly} is not a Chimera — its completion record names no "
                    f"Components and no separator, so there is nothing here to split"
                }
            ),
            err=True,
        )
        raise typer.Exit(3)
    if set(requested) != set(components):
        typer.echo(
            json.dumps(
                {
                    "error": f"{assembly} is a Chimera of {sorted(components)} and outputs were "
                    f"named for {sorted(requested)}; every Component needs one, because the reads "
                    f"on the rest have nowhere to go"
                }
            ),
            err=True,
        )
        raise typer.Exit(3)
    try:
        stats = split_chimera(bam, requested, separator, summary=summary, threads=threads)
    except SplitError as exc:
        typer.echo(json.dumps({"error": str(exc)}), err=True)
        raise typer.Exit(3) from exc
    written: dict[str, object] = {"written": {c: str(p) for c, p in requested.items()}}
    if summary is not None:
        written["summary"] = str(summary)
    typer.echo(json.dumps({**written, **stats.to_dict()}, indent=2))


@io_app.command("umi-extract")
def io_umi_extract(
    units: Path | None = typer.Option(
        None,
        "--units",
        help="The pipeline's units.tsv. With --sample it states this cell's files, so none are "
        "named on the command line. Mutually exclusive with --r1/--r2.",
    ),
    r1: list[Path] = typer.Option(
        [], "--r1", help="The tagged read's FASTQ (gzipped). Repeat it for a cell spanning runs."
    ),
    r2: list[Path] = typer.Option(
        [],
        "--r2",
        help="Its mate's FASTQ, repeated alongside --r1 and paired in the order given. Omitted "
        "for a single-end plate.",
    ),
    geometry: str = typer.Option(
        ...,
        "--geometry",
        help="The read structure compose derived from the element model, e.g. "
        "`R1:ATTGCGCAATG@0:umi@11+8:GGG@19:cdna@22`.",
    ),
    sample: str = typer.Option(
        ...,
        "--sample",
        help="Cell id: which sample to resolve out of --units, and the uBAM's read group.",
    ),
    out: Path = typer.Option(..., "--out", help="Output path for the unaligned BAM."),
    summary: Path | None = typer.Option(
        None,
        "--summary",
        help="Where to write this cell's extraction counts as JSON. Omitted, the same numbers go "
        "to stdout only — which on a cluster means they live in a scheduler log and nowhere else.",
    ),
    read_id: str = typer.Option(
        "R1",
        "--read-id",
        help="Which layout read carries the tag. Refused if the geometry disagrees; it also "
        "selects the tagged role out of --units.",
    ),
) -> None:
    """Lift a plate assay's UMI out of R1 and write it as a uBAM carrying `UB:Z:`.

    The per-cell half of the counting engine, and a verb rather than a `run:` block for the reason
    `io h5ad` is one: `snakemake -n -p` renders a `shell:` while planning and cannot see inside a
    `run:`, so only a verb is visible to compose's wiring gate. Needs no container — writing a BAM
    through a library is not aligning reads.

    **It is handed the TABLE and the cell, not the files** (ADR-0036). `--units` plus `--sample`
    resolves this cell's tagged files and their mates through the same `ordered_fastqs` the rule's
    own `input:` is built from, over the same file — one derivation used twice, never two. That is
    what lets a cell topped up across two runs run at all: the file list never reaches the command
    line, so there is no arity to get wrong on the one surface the wiring gate cannot see. It
    formats a `shell:` block while planning and never runs one, so a usage error on a rendered
    command plans clean and dies at job execution, past handover.

    `--r1`/`--r2` remain, repeatable, for a hand invocation and for a test that wants two paths and
    no table. Exactly one of the two forms: both is a caller who believes two different things about
    which files these are, neither is a caller who has said nothing, and each is exit 3.

    **The mate is optional, because it is an addition to the extraction rather than half of it.**
    The tag is found, cut and trimmed entirely within the tagged read; a mate contributes nothing to
    that and only inherits the resulting `UB` onto a record emitted alongside. So a protocol's
    single-end configuration runs through this same verb — one unpaired record per fragment instead
    of two interleaved ones, with the geometry, the `--read-id` refusal, the anchor search and the
    truncation checks all shared — and the flags say which shape was written, which is what the
    aligner's `SAM SE` / `SAM PE` argument is derived from. There is no second verb and no flag
    saying whether the plate is paired: **no row carrying a second role IS the statement**, exactly
    as an absent `--r2` is.

    **`--geometry` is a DERIVATION, not a knob, and it arrives as one value.** The anchor and its
    offset, the UMI's offset and length, the trailing motif and the cDNA start are all read off the
    element coordinates by `compose` and rendered into a single config value — the same move
    chromap's `--read-format` makes, and the reason there is no `--anchor`, `--umi-length` or
    `--window` here for anyone to type. Nothing may declare it: the key is in the composer's derived
    set, so a KB backend that states it is refused at load. `--read-id` names the tagged read and is
    checked against the read the geometry names. Under `--units` that check has become belt and
    braces — the role selects the rows, so a wrong one finds no files rather than the wrong ones —
    and it still earns its keep over `--r1`, where the tagged file is asserted by hand and handing
    over the plain mate would otherwise extract nothing at exit 0. Deleting it is ADR-0036's
    explicitly deferred question, not a tidy-up (ADR-0035 treats the refusal as load-bearing).

    **`--summary` is what makes the numbers outlive the run.** The uBAM is `temp()`, so the only
    lasting account of what the extraction saw — how many fragments, how many carried a tag, and
    where the tag actually started — used to be whatever captured this command's stdout. Given a
    path, the SAME payload lands beside the cell's other outputs and `seqforge report` reads it back,
    the way it already reads STAR's own per-sample log. One payload and not two: stdout keeps every
    key it had, gains the geometry and the version the file carries, and names where the file went.

    Exit 3 on a Blocker-shaped refusal: an unreadable geometry, the wrong mate, a run whose mate was
    never deposited, a half-renamed FASTQ, a pair of unequal length, a truncated input.
    """
    from ..workflows.umite.extract import (
        TagGeometry,
        UmiExtractError,
        extract_umis,
        extraction_inputs,
    )
    from ..workflows.units import UnitsError

    try:
        structure = TagGeometry.parse(geometry)
        if structure.read_id != read_id:
            raise UmiExtractError(
                f"--read-id {read_id} was handed over as the tagged read, but this layout's UMI is "
                f"on {structure.read_id}. Extracting from the wrong mate finds no tags at all and "
                f"exits 0"
            )
        tagged, mates = extraction_inputs(
            units=units, sample=sample, r1=r1, r2=r2, tagged_role=structure.read_id
        )
        stats = extract_umis(tagged, mates, out, structure, sample=sample, summary=summary)
    except (UmiExtractError, UnitsError) as exc:
        typer.echo(json.dumps({"error": str(exc)}), err=True)
        raise typer.Exit(3) from exc
    written: dict[str, object] = {"written": str(out), "read_id": structure.read_id}
    if summary is not None:
        written["summary"] = str(summary)
    typer.echo(json.dumps({**written, **stats.to_dict()}, indent=2))
