"""`seqforge kb` -- the executable, self-testing knowledge base (list/show/lint/roundtrip/e2e)."""

from __future__ import annotations

import json
from pathlib import Path

import typer
from pydantic import ValidationError

from ..kb import list_spec_ids, load_spec, run_roundtrip
from ._common import _parse_quantify
from .root import kb_app


@kb_app.command("list")
def kb_list() -> None:
    """List every technology in the knowledge base."""
    for tech_id in list_spec_ids():
        typer.echo(tech_id)


@kb_app.command("show")
def kb_show(tech: str = typer.Argument(..., help="Technology id, e.g. 10x-3p-gex-v3.")) -> None:
    """Dump one technology's validated spec as JSON."""
    try:
        spec = load_spec(tech)
    except FileNotFoundError as exc:
        typer.echo(f"unknown technology {tech!r}", err=True)
        raise typer.Exit(2) from exc
    typer.echo(json.dumps(spec.model_dump(mode="json"), indent=2))


@kb_app.command("lint")
def kb_lint() -> None:
    """Validate every shipped spec.yaml against the schema. Exit 3 if any is invalid."""
    results = []
    ok = True
    for tech_id in list_spec_ids():
        try:
            load_spec(tech_id)
            results.append({"tech": tech_id, "ok": True})
        except (ValidationError, ValueError) as exc:
            ok = False
            results.append({"tech": tech_id, "ok": False, "error": str(exc)})
    typer.echo(json.dumps({"ok": ok, "specs": results}, indent=2))
    if not ok:
        raise typer.Exit(3)


@kb_app.command("roundtrip")
def kb_roundtrip(
    tech: str = typer.Argument(..., help="Technology id to round-trip."),
    seed: int = typer.Option(0, help="RNG seed for the synthetic generator."),
) -> None:
    """Self-test: spec -> synth FASTQ -> probe -> recover; assert recovered == declared. Exit 3 on fail."""
    try:
        result = run_roundtrip(tech, seed=seed)
    except FileNotFoundError as exc:
        typer.echo(f"unknown technology {tech!r}", err=True)
        raise typer.Exit(2) from exc
    typer.echo(json.dumps(result, indent=2))
    if not result["passed"]:
        raise typer.Exit(3)


@kb_app.command("e2e")
def kb_e2e(
    workdir: Path = typer.Option(..., "--workdir", help="Scratch dir for reads + STAR output."),
    assembly: str = typer.Option("sacCer3", help="liulab-genome assembly id."),
    annotation: str = typer.Option("ensembl_R64-1-1", help="Registered GTF name."),
    fasta: Path | None = typer.Option(
        None, help="Override: genome FASTA (else via liulab-genome)."
    ),
    gtf: Path | None = typer.Option(None, help="Override: GTF (else via liulab-genome)."),
    star_index: Path | None = typer.Option(
        None, "--star-index", help="Override: prebuilt STAR index."
    ),
    star: str | None = typer.Option(
        None, "--star", help="STAR binary (e.g. liulab-runtime align-rna)."
    ),
    n_cells: int = typer.Option(8, help="Simulated cells."),
    reads_per_cell: int = typer.Option(250, help="Simulated reads per cell."),
    threads: int = typer.Option(8, help="STAR threads."),
    seed: int = typer.Option(0, help="Simulation seed."),
) -> None:
    """The real count-matrix run: simulate -> resolve -> compose -> STARsolo -> assert vs ground truth.

    Exit 3 if the recovered matrix does not equal what was injected (or if a strand inversion would
    go undetected); exit 1 if the toolchain (STAR / a genome index) is unavailable.
    """
    from ..e2e import E2EUnavailable, discover_assets, run_e2e

    try:
        assets = discover_assets(
            assembly=assembly,
            annotation=annotation,
            fasta=fasta,
            gtf=gtf,
            star_index=star_index,
            star_bin=star,
        )
        result = run_e2e(
            assets,
            workdir=workdir,
            n_cells=n_cells,
            reads_per_cell=reads_per_cell,
            threads=threads,
            seed=seed,
        )
    except E2EUnavailable as exc:
        typer.echo(json.dumps({"skipped": True, "reason": str(exc)}, indent=2), err=True)
        raise typer.Exit(1) from exc
    typer.echo(json.dumps(result, indent=2, default=str))
    if not result.get("passed"):
        raise typer.Exit(3)


@kb_app.command("e2e-cost")
def kb_e2e_cost(
    workdir: Path = typer.Option(..., "--workdir", help="Scratch dir for reads + STAR output."),
    whitelist: Path = typer.Option(
        ..., "--whitelist", help="Real 10x whitelist (.txt or .txt.gz)."
    ),
    assembly: str = typer.Option("hg38", help="Assembly to price against."),
    annotation: str = typer.Option("gencode_v50", help="Registered GTF name."),
    sweep: str = typer.Option(
        "2000000,8000000,32000000", help="Comma-separated read depths to measure."
    ),
    n_cells: int = typer.Option(5000, help="Simulated cells (barcodes drawn from the whitelist)."),
    intron_frac: float = typer.Option(0.4, help="Fraction of reads from introns (pre-mRNA)."),
    max_genes: int = typer.Option(2000, help="Gene models to sample reads from."),
    fasta: Path | None = typer.Option(None, help="Override: genome FASTA."),
    gtf: Path | None = typer.Option(None, help="Override: GTF."),
    star_index: Path | None = typer.Option(
        None, "--star-index", help="Override: prebuilt STAR index."
    ),
    star: str | None = typer.Option(None, "--star", help="STAR binary (liulab-runtime align-rna)."),
    threads: int = typer.Option(
        16, help="STAR threads. Peak RSS depends on this — it is recorded."
    ),
    gen_jobs: int = typer.Option(
        16, "--gen-jobs", help="Processes generating reads (it was 40% of wall-clock on one core)."
    ),
    seed: int = typer.Option(0, help="Simulation seed."),
    quantify: str | None = typer.Option(
        None, "--quantify", help="Override soloFeatures. Omit to price the compiler's own default."
    ),
    out_sam_type: str = typer.Option(
        "None",
        "--out-sam-type",
        help="STAR --outSAMtype. The shipped module runs 'BAM Unsorted' — pass it to price the gap.",
    ),
    keep_reads: bool = typer.Option(
        False, "--keep-reads", help="Do not delete FASTQs after a run."
    ),
) -> None:
    """Measure STARsolo's peak RSS against read depth — what a counting rule costs, not whether it is right.

    This is the PRICE arm, not a gate: it asserts nothing about counts and injects no ground truth.
    A single measurement would be almost entirely genome index (~30 GB on hg38, paid before a read is
    parsed), so this fits a line across several depths and reports the intercept you pay per job, the
    slope you pay per read, and the residual that says whether the line deserved to be believed.

    Prints JSON; exit 1 if the toolchain is unavailable. Needs STAR, an index, and real time.
    """
    from ..e2e import E2EUnavailable, discover_assets, run_cost_sweep

    try:
        depths = tuple(int(float(s)) for s in sweep.split(",") if s.strip())
    except ValueError as exc:
        typer.echo(f"--sweep must be comma-separated read counts, got {sweep!r}", err=True)
        raise typer.Exit(2) from exc
    if not depths:
        typer.echo("--sweep is empty", err=True)
        raise typer.Exit(2)

    try:
        assets = discover_assets(
            assembly=assembly,
            annotation=annotation,
            fasta=fasta,
            gtf=gtf,
            star_index=star_index,
            star_bin=star,
        )
        result = run_cost_sweep(
            assets,
            workdir=workdir,
            whitelist=whitelist,
            sweep=depths,
            n_cells=n_cells,
            intron_frac=intron_frac,
            max_genes=max_genes,
            threads=threads,
            gen_jobs=gen_jobs,
            seed=seed,
            features=_parse_quantify(quantify),
            out_sam_type=tuple(out_sam_type.split()),
            keep_reads=keep_reads,
        )
    except E2EUnavailable as exc:
        typer.echo(json.dumps({"skipped": True, "reason": str(exc)}, indent=2), err=True)
        raise typer.Exit(1) from exc
    typer.echo(json.dumps(result, indent=2, default=str))


@kb_app.command("e2e-fit")
def kb_e2e_fit(
    results: list[Path] = typer.Argument(..., help="cost JSONs to merge (one per array task)."),
) -> None:
    """Fit one line across cost runs measured separately — the collector for a job-array sweep.

    The depths are independent, so a sweep parallelises across array tasks and each task emits its
    own JSON. This merges them and fits the same line ``run_cost_sweep`` fits in-process, so an array
    and a single sequential job produce the same answer in the same shape.

    It refuses to merge runs whose soloFeatures, assembly, or thread count differ: the peak depends on
    all three, so splicing them into one line would silently fit a curve through incomparable points —
    the exact failure the per-shard seed and the resume guard exist to prevent elsewhere.
    """
    from ..e2e import _fit_line

    runs = []
    for path in results:
        try:
            runs.append((path, json.loads(path.read_text())))
        except (OSError, json.JSONDecodeError) as exc:
            typer.echo(f"cannot read {path}: {exc}", err=True)
            raise typer.Exit(1) from exc
    if not runs:
        typer.echo("no results given", err=True)
        raise typer.Exit(2)

    def key(r: dict[str, object]) -> tuple[object, ...]:
        # soloFeatures arrives from JSON as a list, which is unhashable — tuple it before it meets a set.
        features = r.get("soloFeatures")
        return (
            tuple(features) if isinstance(features, list) else features,
            r.get("assembly"),
            r.get("threads"),
            r.get("n_cells"),
        )

    keys = {key(r) for _p, r in runs}
    if len(keys) != 1:
        typer.echo(
            json.dumps(
                {
                    "error": "refusing to fit incomparable runs",
                    "detail": "soloFeatures/assembly/threads/n_cells must match across every result",
                    "distinct": [list(k) for k in keys],
                },
                indent=2,
                default=str,
            ),
            err=True,
        )
        raise typer.Exit(3)

    points: list[dict[str, object]] = []
    for _p, r in runs:
        points.extend(p for p in r.get("points", []) if not p.get("failed"))
    points.sort(key=lambda p: int(p["n_reads"]))
    if len({int(p["n_reads"]) for p in points}) != len(points):
        typer.echo("duplicate read depths across results; refusing to fit", err=True)
        raise typer.Exit(3)

    head = runs[0][1]
    typer.echo(
        json.dumps(
            {
                "assembly": head.get("assembly"),
                "annotation": head.get("annotation"),
                "soloFeatures": head.get("soloFeatures"),
                "threads": head.get("threads"),
                "n_cells": head.get("n_cells"),
                "n_runs_merged": len(runs),
                "points": points,
                "fit": _fit_line(
                    [(int(p["n_reads"]), float(p["star_peak_rss_gb"])) for p in points]
                ),
            },
            indent=2,
            default=str,
        )
    )


@kb_app.command("e2e-introns")
def kb_e2e_introns(
    workdir: Path = typer.Option(..., "--workdir", help="Scratch dir for reads + STAR output."),
    assembly: str = typer.Option("ce11", help="Must be intron-rich; sacCer3 cannot test this."),
    annotation: str = typer.Option("WS298", help="Registered GTF name."),
    fasta: Path | None = typer.Option(None, help="Override: genome FASTA."),
    gtf: Path | None = typer.Option(None, help="Override: GTF."),
    star_index: Path | None = typer.Option(
        None, "--star-index", help="Override: prebuilt STAR index."
    ),
    star: str | None = typer.Option(None, "--star", help="STAR binary (liulab-runtime align-rna)."),
    n_cells: int = typer.Option(8, help="Simulated cells."),
    reads_per_cell: int = typer.Option(250, help="Simulated reads per cell."),
    intron_frac: float = typer.Option(0.4, help="Fraction of reads drawn from introns (pre-mRNA)."),
    threads: int = typer.Option(8, help="STAR threads."),
    seed: int = typer.Option(0, help="Simulation seed."),
    quantify: str | None = typer.Option(
        None,
        "--quantify",
        help="Override soloFeatures (comma-separated) — the cost-measurement arm. "
        "Omit to run the compiler's own default, which is what the gate is for.",
    ),
) -> None:
    """The intron-rich / GeneFull gate: inject intronic reads, assert Gene and GeneFull disagree right.

    Yeast is nearly intron-free, so the sacCer3 e2e certifies neither counting rule. This injects a
    known number of intronic reads (what a single-NUCLEUS library actually contains) and asserts Gene
    recovers only the exonic truth while GeneFull recovers exon+intron — both from ONE STARsolo run,
    so the alignment is identical and only the counting rule differs. Reports `gene_signal_lost`: what
    `--soloFeatures Gene` silently discards from a nuclear library. Exit 3 on failure, 1 if the
    toolchain is unavailable.
    """
    from ..e2e import E2EUnavailable, discover_assets, run_intron_e2e

    try:
        assets = discover_assets(
            assembly=assembly,
            annotation=annotation,
            fasta=fasta,
            gtf=gtf,
            star_index=star_index,
            star_bin=star,
        )
        result = run_intron_e2e(
            assets,
            workdir=workdir,
            n_cells=n_cells,
            reads_per_cell=reads_per_cell,
            intron_frac=intron_frac,
            threads=threads,
            seed=seed,
            features=_parse_quantify(quantify),
        )
    except E2EUnavailable as exc:
        typer.echo(json.dumps({"skipped": True, "reason": str(exc)}, indent=2), err=True)
        raise typer.Exit(1) from exc
    typer.echo(json.dumps(result, indent=2, default=str))
    if not result.get("passed"):
        raise typer.Exit(3)
