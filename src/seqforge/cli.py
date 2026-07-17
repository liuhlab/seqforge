"""The ``seqforge`` Typer application — the CLI is the API.

Every skill action maps to a deterministic ``seqforge <verb>`` (JSON on stdout by default) that runs with no LLM in the
loop (only ``harvest extract`` and the opt-in ``resolve adjudicate`` touch an LLM). Exit codes are
uniform: ``0`` OK, ``1`` ERROR, ``2`` USAGE, ``3`` BLOCKED (a Blocker), ``4`` NEEDS_HUMAN (an open
Conflict / question).

Milestone 0 wires the deterministic spine incrementally; ``schema export`` is live, the remaining
verbs are declared and raise a clear "not yet implemented" until their stage lands.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

import typer
import yaml
from pydantic import ValidationError

from . import __version__
from .compose import ComposeError, compose
from .io import DEFAULT_REGISTRY, default_registry
from .io.remote import NotYetImplemented, peek, resolve_accession
from .io.taxonomy import TaxonomyUnavailable
from .io.taxonomy import resolve as resolve_organism
from .kb import list_spec_ids, load_spec, run_roundtrip
from .manifest import (
    FillError,
    Instruction,
    PolicyError,
    ProcessingInputs,
    dataset_content_hash,
    exit_code_for_report,
    experiment_from_metadata,
    fill_manifest,
    fill_processing,
    instructions_from_assertions,
    processing_content_hash,
    validate_manifest,
    validate_processing,
)
from .models import SCHEMA_MODELS, export_all, export_schema
from .models.assertion import Assertion
from .models.dataset import DatasetManifest
from .models.processing import ProcessingManifest
from .resolve import Hypothesis, resolve_dataset, resolve_runs
from .workspace import STATE_DIRNAME, legacy_state_dir, readable, state_dir

if TYPE_CHECKING:
    from .models.records import ArchiveRecordSet


def _today() -> str:
    """Today, for the ``fetched`` stamp on a generated vocabulary file.

    Local import and a function rather than a module constant: a constant would be evaluated at import
    time, and every artifact seqforge writes is content-addressed — a clock reachable from module
    scope is a clock that eventually ends up inside a hash.
    """
    import datetime

    return datetime.date.today().isoformat()


app = typer.Typer(
    name="seqforge",
    help="Compile FASTQ + metadata into a validated library manifest and a Snakemake config.",
    no_args_is_help=True,
    add_completion=False,
)

schema_app = typer.Typer(help="Export JSON Schema from the Pydantic models (the source of truth).")
app.add_typer(schema_app, name="schema")

kb_app = typer.Typer(help="The executable, self-testing knowledge base.")
app.add_typer(kb_app, name="kb")

io_app = typer.Typer(help="The network + onlist surface (pooch-cached, sha256-verified).")
app.add_typer(io_app, name="io")

onlist_app = typer.Typer(help="Barcode-whitelist (onlist) registry.")
io_app.add_typer(onlist_app, name="onlist")

resolve_app = typer.Typer(help="Score bytes + KB into a ranked, escalated chemistry decision.")
app.add_typer(resolve_app, name="resolve")

manifest_app = typer.Typer(
    help="The DATASET manifest: what the data IS. Immutable, one per dataset."
)
app.add_typer(manifest_app, name="manifest")
processing_app = typer.Typer(
    help="The PROCESSING manifest: what to DO with a dataset. Many per dataset."
)
app.add_typer(processing_app, name="processing")

harvest_app = typer.Typer(
    help="Prose/metadata -> span-verified Assertions (the one LLM touchpoint)."
)
app.add_typer(harvest_app, name="harvest")

eval_app = typer.Typer(help="The evals harness: measure what unit tests cannot (brief §9).")
app.add_typer(eval_app, name="eval")

hook_app = typer.Typer(help="Agent hooks: the rules as mechanism, not aspiration (design §4.2).")
app.add_typer(hook_app, name="hook")


@app.command()
def version() -> None:
    """Print the seqforge version."""
    typer.echo(__version__)


@app.command("probe")
def probe_cmd(
    files: list[Path] = typer.Argument(..., help="FASTQ .gz files to fingerprint."),
    workspace: Path = typer.Option(
        Path("."), "-C", "--workspace", help="Root for seqforge/ state."
    ),
    max_reads: int = typer.Option(200_000, help="Bounded read budget."),
    max_bytes: int = typer.Option(256 * 1024 * 1024, help="Bounded decompressed-byte cap."),
    no_cache: bool = typer.Option(False, "--no-cache", help="Do not write seqforge/ artifacts."),
) -> None:
    """Fingerprint FASTQ bytes into role-free Observations. No LLM, no network, bounded.

    The budget is the point: a 40 GB file costs the same as a 40 MB one, because probe stops at
    --max-reads AND --max-bytes, whichever comes first. Never returns 3/4 — it only observes; refusal
    happens downstream when a validator reads the observation.
    """
    from .probe import probe_file
    from .resolve import Cache

    cache = Cache(workspace) if not no_cache else None
    observations = []
    for path in files:
        try:
            obs = probe_file(path, max_reads=max_reads, max_bytes=max_bytes)
        except (OSError, ValueError) as exc:
            typer.echo(json.dumps({"error": f"{path}: {exc}"}, indent=2), err=True)
            raise typer.Exit(1) from exc
        if cache is not None:
            cache.write_observation(obs)
        observations.append(obs.model_dump(mode="json"))
    typer.echo(json.dumps(observations if len(observations) > 1 else observations[0], indent=2))


@schema_app.command("list")
def schema_list() -> None:
    """List every model whose JSON Schema can be exported."""
    for name in sorted(SCHEMA_MODELS):
        typer.echo(name)


@schema_app.command("export")
def schema_export(
    model: str | None = typer.Argument(
        None, help="Model class name to export (e.g. Manifest). Omit with --all for every model."
    ),
    export_all_models: bool = typer.Option(
        False, "--all", help="Export every model's schema as one JSON object."
    ),
) -> None:
    """Dump one model's (or every model's) JSON Schema to stdout."""
    if export_all_models:
        typer.echo(json.dumps(export_all(), indent=2, sort_keys=True))
        return
    if model is None:
        typer.echo("give a MODEL name or --all; see `seqforge schema list`", err=True)
        raise typer.Exit(2)
    try:
        schema = export_schema(model)
    except KeyError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(2) from exc
    typer.echo(json.dumps(schema, indent=2, sort_keys=True))


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
    from .e2e import E2EUnavailable, discover_assets, run_e2e

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
    from .e2e import E2EUnavailable, discover_assets, run_cost_sweep

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
    from .e2e import _fit_line

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
    from .e2e import E2EUnavailable, discover_assets, run_intron_e2e

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


@onlist_app.command("list")
def io_onlist_list() -> None:
    """List the onlists in the default registry. Shipped ones need no network and no setup."""
    rows = []
    for name in DEFAULT_REGISTRY.names():
        entry = DEFAULT_REGISTRY.get(name)
        rows.append(
            {
                "name": entry.name,
                "width": entry.width,
                "orientation": entry.orientation,
                "n_entries": entry.n_entries,
                "shipped": entry.shipped,
                "fetchable": entry.fetchable,
            }
        )
    typer.echo(json.dumps({"onlists": rows}, indent=2))


@onlist_app.command("show")
def io_onlist_show(
    name: str = typer.Argument(..., help="Registry name, e.g. 3M-february-2018."),
) -> None:
    """Show one onlist registry entry as JSON."""
    if not DEFAULT_REGISTRY.has(name):
        typer.echo(f"unknown onlist {name!r}", err=True)
        raise typer.Exit(2)
    entry = DEFAULT_REGISTRY.get(name)
    typer.echo(
        json.dumps(
            {
                "name": entry.name,
                "uri": entry.uri,
                "sha256": entry.sha256,
                "width": entry.width,
                "orientation": entry.orientation,
                "n_entries": entry.n_entries,
                "shipped": entry.shipped,
                "fetchable": entry.fetchable,
                "source_sha256": entry.source_sha256,
            },
            indent=2,
        )
    )


@onlist_app.command("write")
def io_onlist_write(
    name: str = typer.Argument(..., help="Registry name, e.g. 3M-february-2018."),
    out: Path = typer.Option(..., "--out", "-o", help="Where to write the barcode text."),
    onlist_dir: Path | None = typer.Option(
        None, "--onlist-dir", help="A directory of already-downloaded whitelists."
    ),
) -> None:
    """Materialize a whitelist as the text STARsolo reads. Called BY the composed Snakefile.

    This is the verb behind `rule onlist`, and the reason that rule exists. 10x's v3 whitelist is
    6 794 880 barcodes = 111 MB of text, and `compose` used to write it into every run directory --
    so one dataset compiled three ways cost a third of a gigabyte of identical bytes, permanently,
    for a file STAR opens once. Now the pipeline builds it, uses it, and `temp()` deletes it.

    The shipped form is 522 kB of packed deltas; this is the expansion. Nothing is fetched when the
    list ships with the package, which is the case for every 10x whitelist.
    """
    from .io.onlist import OnlistNotAvailable, write_onlist_text

    registry = (
        default_registry(offline=False, local_dir=onlist_dir) if onlist_dir else DEFAULT_REGISTRY
    )
    try:
        n = write_onlist_text(registry, name, out)
    except OnlistNotAvailable as exc:
        typer.echo(
            json.dumps({"error": "onlist_unavailable", "detail": str(exc)}, indent=2), err=True
        )
        raise typer.Exit(3) from exc
    typer.echo(json.dumps({"onlist": name, "path": str(out), "n_entries": n}, indent=2))


@onlist_app.command("pack")
def io_onlist_pack(
    text: Path = typer.Argument(..., help="The whitelist as text (.txt or .txt.gz)."),
    name: str = typer.Option(..., "--name", help="Registry name, e.g. 3M-february-2018."),
    uri: str = typer.Option(
        "", "--uri", help="Where this list came from, recorded for provenance."
    ),
    orientation: str = typer.Option("forward", "--orientation", help="forward | revcomp | either."),
) -> None:
    """**Maintenance verb.** Pack a whitelist into the shipped form and record it in the index.

    This is how a new barcode list joins the package: `pack` it, commit the `.codes.gz` and the
    updated `index.json`, done. Nothing else to remember and nothing to hand-edit -- this verb is the
    only writer of `index.json`, which is what stops the index drifting from the blobs beside it.

    The shipped form is 2-bit-packed, sorted, de-duplicated, delta-encoded and gzipped: 10x's
    6 794 880-barcode v3 list is 522 kB here against 12 MB as their `.txt.gz`. That is why shipping
    them is cheap, and it also closes the `.npy` precompilation §14 has wanted since the beginning --
    nothing re-packs 6.8M barcodes per process any more.
    """
    import gzip as _gzip
    import hashlib as _hashlib

    from .io.onlist import PackedOnlist, write_shipped

    raw = text.read_bytes()
    if raw[:2] == b"\x1f\x8b":
        raw = _gzip.decompress(raw)
    source_sha = _hashlib.sha256(raw).hexdigest()
    barcodes = [line.strip() for line in raw.decode().splitlines() if line.strip()]
    if not barcodes:
        typer.echo(f"{text} contains no barcodes", err=True)
        raise typer.Exit(2)
    packed = PackedOnlist.from_barcodes(barcodes)
    blob = write_shipped(
        name,
        packed.codes,
        width=packed.width,
        uri=uri,
        orientation=orientation,  # type: ignore[arg-type]
        source_sha256=source_sha,
    )
    typer.echo(
        json.dumps(
            {
                "name": name,
                "packed": str(blob),
                "bytes": blob.stat().st_size,
                "n_entries": packed.n_entries,
                "width": packed.width,
                "source_sha256": source_sha,
            },
            indent=2,
        )
    )


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
    from .models.processing import SoloFeature
    from .workflows.h5ad import SOLO_FEATURE_OUTPUT, H5adError, write_h5ad

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


@io_app.command("peek")
def io_peek(
    uri: str = typer.Argument(..., help="Remote FASTQ URI to range-read."),
    max_reads: int = typer.Option(4, help="Records to report from the fetched prefix."),
    max_bytes: int = typer.Option(1 << 16, help="Compressed bytes to range-read (budget)."),
) -> None:
    """Range-read the head of a remote gzipped FASTQ. Never downloads the file.

    64 KB is ~0.013% of a 517 MB run. Exit 1 if the host ignores Range and answers 200 with the whole
    file — bounded means bounded by the server, not by our intentions.
    """
    from .io.remote import RemoteError

    try:
        typer.echo(json.dumps(peek(uri, max_reads=max_reads, max_bytes=max_bytes), indent=2))
    except (NotYetImplemented, RemoteError) as exc:
        typer.echo(json.dumps({"error": str(exc)}, indent=2), err=True)
        raise typer.Exit(1) from exc


@io_app.command("resolve")
def io_resolve(
    accession: str = typer.Argument(..., help="GSE/GSM, PRJNA/PRJEB, SRP/SRX/SRR, SAMN..."),
    check_reads: bool = typer.Option(
        True,
        "--check-reads/--no-check-reads",
        help="Compare SRA's per-read table to what ENA published (detects a dropped technical read).",
    ),
) -> None:
    """Expand an accession into runs + declared metadata, and flag a dropped technical read.

    The important part is the flag, not the inventory: fasterq-dump skips technical reads BY DEFAULT,
    so a 10x barcode read routinely vanishes from the published FASTQ while staying inside the .sra —
    leaving a dataset that looks like plain single-end RNA-seq. Two metadata calls catch it before a
    byte is downloaded (rung 0). Exit 4 if any run is missing one: a human must re-fetch it.
    """
    from .io.remote import RemoteError

    try:
        result = resolve_accession(accession, check_reads=check_reads)
    except (NotYetImplemented, RemoteError) as exc:
        typer.echo(json.dumps({"error": str(exc)}, indent=2), err=True)
        raise typer.Exit(1) from exc
    typer.echo(json.dumps(result, indent=2))
    if result.get("n_runs_missing_technical_read"):
        raise typer.Exit(4)


@io_app.command("records")
def io_records(
    accession: str = typer.Argument(..., help="GSE/GSM, PRJNA/PRJEB, SRP/SRX/SRR, SAMN..."),
    workspace: Path = typer.Option(
        Path("."), "-C", "--workspace", help="Root for seqforge/ state."
    ),
) -> None:
    """Fetch what the archive DECLARES about a dataset: project, sample, experiment, run.

    A transcriber, not a resolver. It reports the record and stops — `resolve` decides what any of it
    means. This is where per-sample metadata comes from: `strain`, `tissue`, `sex`, `dev_stage` live
    on the BioSample record and were fetched by no code at all until now, which is why the pilot's six
    samples all said `tissue: null`.

    Cached under `seqforge/records/`: a record is a fact about the archive at a moment, so
    re-fetching it should be a choice.
    """
    from .io.archive import fetch_records
    from .io.remote import RemoteError

    try:
        records = fetch_records(accession)
    except RemoteError as exc:
        typer.echo(json.dumps({"error": str(exc)}, indent=2), err=True)
        raise typer.Exit(1) from exc

    state = Path(workspace) / STATE_DIRNAME / "records"
    state.mkdir(parents=True, exist_ok=True)
    target = state / f"{accession}.json"
    target.write_text(json.dumps(records.model_dump(mode="json"), indent=2))
    typer.echo(
        json.dumps(
            {
                "records": str(target),
                "query": records.query,
                "source": records.source,
                "n": {
                    level: len(records.at(level))  # type: ignore[arg-type]
                    for level in ("project", "sample", "experiment", "run")
                },
            },
            indent=2,
        )
    )


@io_app.command("attributes")
def io_attributes(
    name: str | None = typer.Argument(None, help="Show one attribute; omit to list them all."),
    refresh: bool = typer.Option(
        False, "--refresh", help="Re-fetch NCBI's list and rewrite the shipped vocabulary."
    ),
) -> None:
    """NCBI's harmonized BioSample attribute names — the key space a sample fact must use.

    960 curated names with NCBI's own definitions. We enforce against all of them and ask a model for
    a hand-picked few. `condition` is NOT one of them, which is why it is no longer one of ours.
    """
    from .io.attributes import (
        ATTRIBUTES_URL,
        get_attribute,
        load_attributes,
        parse_ncbi_attributes_xml,
        source_provenance,
        write_attributes,
    )

    if refresh:
        from .io.remote import _get

        attrs = parse_ncbi_attributes_xml(_get(ATTRIBUTES_URL, timeout=120))
        path = write_attributes(attrs, fetched=_today())
        typer.echo(json.dumps({"wrote": str(path), "n": len(attrs)}, indent=2))
        return

    if name:
        attr = get_attribute(name)
        typer.echo(
            json.dumps(
                {
                    "name": attr.name,
                    "display": attr.display,
                    "description": attr.description,
                    "synonyms": list(attr.synonyms),
                },
                indent=2,
            )
        )
        return
    typer.echo(
        json.dumps(
            {**source_provenance(), "names": sorted(load_attributes())},
            indent=2,
        )
    )


@io_app.command("efo")
def io_efo(
    refresh: bool = typer.Option(
        False, "--refresh", help="Re-fetch labels for every CURIE the KB declares."
    ),
) -> None:
    """The EFO labels behind `library.assay` — what `EFO:0009922` is actually called.

    `assay: EFO:0009922` is good standardization and unreadable. The name comes from EFO via EBI's
    OLS4, never from us: a label we maintain by hand drifts from the ontology it claims to quote.
    `--refresh` re-fetches every CURIE the KB's specs declare, so adding a technology is: add the
    spec, run this, commit.
    """
    import json as _json
    import urllib.parse
    import urllib.request

    from .io.efo import OLS4_TERMS, EfoTerm, iri_for, load_terms, parse_ols4_term, write_terms
    from .kb import load_all_specs

    if not refresh:
        typer.echo(
            _json.dumps(
                {c: {"name": t.name, "iri": t.iri} for c, t in sorted(load_terms().items())},
                indent=2,
            )
        )
        return

    curies = sorted({c for spec in load_all_specs().values() for c in spec.identity.assay_ontology})
    terms: dict[str, EfoTerm] = {}
    for curie in curies:
        # OLS4 wants the IRI **double**-URL-encoded in the path. A singly-encoded one 404s, which is
        # the kind of thing that belongs in code rather than in someone's memory.
        quoted = urllib.parse.quote(urllib.parse.quote(iri_for(curie), safe=""), safe="")
        with urllib.request.urlopen(OLS4_TERMS + quoted, timeout=60) as response:  # noqa: S310
            terms[curie] = parse_ols4_term(_json.load(response))
    path = write_terms(terms, fetched=_today())
    typer.echo(
        _json.dumps(
            {"wrote": str(path), "terms": {c: t.name for c, t in sorted(terms.items())}}, indent=2
        )
    )


@resolve_app.command("score")
def resolve_score(
    files: list[Path] = typer.Argument(..., help="The dataset's FASTQ .gz files."),
    workspace: Path = typer.Option(
        Path("."), "-C", "--workspace", help="Root for seqforge/ state."
    ),
    assert_chemistry: str | None = typer.Option(
        None,
        "--assert-chemistry",
        help="A metadata-asserted chemistry (the span-verified hypothesis).",
    ),
    explain: bool = typer.Option(
        False, "--explain", help="Also emit the JSON-safe evidence matrices."
    ),
    no_cache: bool = typer.Option(
        False, "--no-cache", help="Do not read/write seqforge/ artifacts."
    ),
    max_reads: int = typer.Option(200_000, help="Bounded read budget."),
    max_bytes: int = typer.Option(256 * 1024 * 1024, help="Bounded decompressed-byte cap."),
    cpus: int = typer.Option(
        0, "--cpus", help="Parallel probe workers. 0 = auto (min(8, CPUs)); 1 = sequential."
    ),
) -> None:
    """Score FASTQ bytes + KB into a ResolveResult. Exit 3 on a Blocker, 4 on an open Conflict/question."""
    hypothesis = Hypothesis(value=assert_chemistry) if assert_chemistry else None
    output = resolve_dataset(
        [str(f) for f in files],
        hypothesis=hypothesis,
        workspace=workspace,
        max_reads=max_reads,
        max_bytes=max_bytes,
        use_cache=not no_cache,
        cpus=_auto_cpus(cpus),
    )
    payload: dict[str, object] = output.result.model_dump(mode="json")
    if explain:
        payload = {"result": payload, "matrices": output.matrices}
    typer.echo(json.dumps(payload, indent=2))
    code = output.exit_code()
    if code != 0:
        raise typer.Exit(code)


@harvest_app.command("normalize")
def harvest_normalize(
    docs: list[Path] = typer.Argument(None, help="Reference documents to cite (.txt/.md/.pdf)."),
    instruction: list[Path] = typer.Option(
        [],
        "--instruction",
        help="Document(s) authored FOR seqforge (e.g. alignment_instruction.md).",
    ),
    workspace: Path = typer.Option(
        Path("."), "-C", "--workspace", help="Root for seqforge/ state."
    ),
) -> None:
    """Extract each document ONCE into the canonical text that spans are computed against.

    A document's ROLE is the flag it arrived under, never its filename: only an --instruction document
    may set processing.*. `alignment_instruction.md` is a convention you pass here, load-bearing
    nowhere — a filename trigger would be spoofable by renaming a downloaded PDF.
    """
    from .harvest import normalize_document

    outdir = state_dir(workspace, "documents")
    outdir.mkdir(parents=True, exist_ok=True)
    rows = []
    for doc, role in _roled(docs, instruction):
        try:
            nd = normalize_document(doc, role=role)
        except (OSError, RuntimeError) as exc:
            typer.echo(f"{doc}: {exc}", err=True)
            raise typer.Exit(1) from exc
        target = outdir / _document_filename(nd)
        target.write_text(nd.text)
        rows.append(
            {
                "source": nd.source_basename,
                "role": nd.role,
                "scope": nd.scope,
                "subject": nd.subject,
                "doc_sha256": nd.doc_sha256,
                "normalized_sha256": nd.normalized_sha256,
                "normalizer_version": nd.normalizer_version,
                "n_chars": nd.n_chars,
                "path": str(target.relative_to(Path(workspace))),
            }
        )
    typer.echo(json.dumps({"normalized": rows}, indent=2))


def _document_filename(doc: Any) -> str:
    """``paper.pdf`` -> ``paper-3f8a1c2d9b04.txt``; a record -> ``sample-SAMN40935621-....txt``.

    The hash stays, because two documents can share a name and the identity is the hash. But a
    directory of bare 64-hex filenames is a directory you cannot read, and an early build's document
    cache was exactly that: nothing in it said which file was the paper. The source
    name is already known -- we opened the file -- so printing it costs nothing and no model is
    involved in producing it.
    """
    return readable(Path(doc.source_basename).stem, doc.doc_sha256) + ".txt"


def _roled(docs: list[Path] | None, instruction: list[Path] | None) -> list[tuple[Path, str]]:
    """Pair each document with the ROLE its flag assigned. Code owns role; a filename never does."""
    pairs: list[tuple[Path, str]] = [(d, "reference") for d in (docs or [])]
    pairs += [(d, "instruction") for d in (instruction or [])]
    if not pairs:
        typer.echo("give at least one document, or --instruction FILE", err=True)
        raise typer.Exit(2)
    return pairs


@harvest_app.command("extract")
def harvest_extract(
    docs: list[Path] = typer.Argument(None, help="Reference documents to cite (.txt/.md/.pdf)."),
    instruction: list[Path] = typer.Option(
        [],
        "--instruction",
        help="Document(s) authored FOR seqforge; only these may set processing.*.",
    ),
    records_path: Path | None = typer.Option(
        None,
        "--records",
        help="A record set from `seqforge io records`. Each record's free text becomes its OWN "
        "document, which is how a claim gets to name a sample.",
    ),
    provider: str | None = typer.Option(
        None, "--provider", help="anthropic | deepseek | openai-compatible (default: auto-detect)."
    ),
    model: str | None = typer.Option(
        None, "--model", help="Override the model (default: the provider's own default)."
    ),
    verify: bool = typer.Option(
        True, "--verify/--no-verify", help="Span-verify the drafts immediately."
    ),
    workspace: Path = typer.Option(
        Path("."), "-C", "--workspace", help="Root for seqforge/ state."
    ),
) -> None:
    """The ONE LLM touchpoint: prose -> AssertionDraft[] -> (verified) Assertion[].

    The model only proposes `{field, value, quote}`; code computes the offsets and decides what
    survives — which is what makes the provider swappable. Auto-detects DEEPSEEK_API_KEY /
    ANTHROPIC_API_KEY. Exit 1 if the LLM surface is unavailable, 4 if any claim fails verification.

    **`--records` is how a claim names a sample.** Each archive record is rendered as its own
    document and asked only what a record at that level can answer: a BioSample's document is asked
    for sample attributes and never for a chemistry; an experiment's protocol paragraph is asked for
    the chemistry and nothing else. Since a sample's document contains one sample's prose, "which
    sample" is answered by which file we handed the model — the model never names one, and cannot.
    """
    _emit(
        _harvest_extract_pipeline(
            docs=docs,
            instruction=instruction,
            records_path=records_path,
            provider=provider,
            model=model,
            verify=verify,
            workspace=workspace,
        )
    )


def _harvest_extract_pipeline(
    *,
    docs: list[Path] | None,
    instruction: list[Path] | None,
    records_path: Path | None,
    provider: str | None,
    model: str | None,
    verify: bool,
    workspace: Path,
) -> _StageOut:
    """The body of ``harvest extract``, returned as a value so ``seqforge run`` can chain it.

    The one LLM stage, and the one place ``run`` cannot be fully deterministic — hence ``--no-llm``,
    which is the caller choosing not to enter here at all. Every exit is a ``_StageOut``: exit 1 if no
    provider or the endpoint fails, exit 4 if a claim fails the span tripwire (a rejected claim needs
    a human, not a silent drop). On success it still writes ``assertions.json`` and the rendered
    documents to disk, because a span citation is only checkable while the exact text survives.
    """
    from .harvest import (
        ExtractionOutcome,
        ExtractUnavailable,
        NormalizedDoc,
        ProviderUnavailable,
        extract_drafts,
        has_prose,
        normalize_document,
        normalize_record,
        resolve_provider,
        verify_drafts,
    )
    from .kb import load_all_specs
    from .models.records import ArchiveRecordSet

    specs = load_all_specs()
    state = state_dir(workspace)
    state.mkdir(parents=True, exist_ok=True)
    try:
        llm = resolve_provider(provider)
    except ProviderUnavailable as exc:
        return _StageOut({"error": "no_provider", "detail": str(exc)}, 1, err=True)
    chosen = model or llm.default_model()

    all_drafts = []
    normalized = []
    usage_total: dict[str, int] = {}
    extractor = None
    sources: list[tuple[object, str]] = [(d, r) for d, r in _roled(docs, instruction)]
    for doc, role in sources:
        nd = normalize_document(doc, role=role)  # type: ignore[arg-type]
        normalized.append(nd)

    if records_path is not None:
        records = ArchiveRecordSet.model_validate_json(records_path.read_text())
        # Only records that HAVE prose, and only levels we ask anything of. A record with an empty
        # ask costs an API call to be told nothing; `fields_for` already knows which those are.
        from .harvest.fields import fields_for

        for record in records.records:
            if has_prose(record) and fields_for(record.level, "reference"):
                normalized.append(normalize_record(record))

    # The one place `run` cannot be deterministic, and now the one it need not be slow. Each document
    # is an independent, network-bound LLM call, so they go out concurrently on a THREAD pool (I/O, so
    # threads release the GIL — processes would only add IPC). Results are reassembled in `normalized`
    # order below, so assertions.json is byte-identical no matter which call returned first.
    def _extract(nd: NormalizedDoc) -> ExtractionOutcome:
        return extract_drafts(nd, specs, provider=llm, model=chosen)

    outcomes: dict[str, ExtractionOutcome] = {}
    try:
        if len(normalized) > 1:
            from concurrent.futures import ThreadPoolExecutor

            with ThreadPoolExecutor(max_workers=min(8, len(normalized))) as pool:
                futures = {pool.submit(_extract, nd): nd for nd in normalized}
                for fut in futures:
                    outcomes[futures[fut].doc_sha256] = fut.result()
        else:
            for nd in normalized:
                outcomes[nd.doc_sha256] = _extract(nd)
    except ExtractUnavailable as exc:
        return _StageOut({"error": "llm_unavailable", "detail": str(exc)}, 1, err=True)

    usage_records: list[dict[str, object]] = []
    for nd in normalized:
        outcome = outcomes[nd.doc_sha256]
        all_drafts.extend(outcome.drafts)
        extractor = outcome.extractor
        for k, v in outcome.usage.items():
            usage_total[k] = usage_total.get(k, 0) + v
        usage_records.append(
            {
                "document": {"scope": nd.scope, "subject": nd.subject, "doc_sha256": nd.doc_sha256},
                "provider": outcome.provider,
                "model": outcome.model,
                "mode": outcome.mode,
                "usage": outcome.usage,
            }
        )

    # The cost ledger (disk is state). Written whether or not we go on to verify, because the call
    # happened and cost tokens regardless. `n_calls` is per-document; `cache_read_tokens > 0` means the
    # stable KB prefix was served from cache, so a second run over the same documents is much cheaper.
    (state / "usage.json").write_text(
        json.dumps(
            {
                "provider": llm.name,
                "model": chosen,
                "prompt_version": extractor.prompt_version if extractor else None,
                "totals": {**usage_total, "n_calls": len(normalized)},
                "calls": usage_records,
            },
            indent=2,
        )
    )

    payload: dict[str, object] = {
        "provider": llm.name,
        "model": chosen,
        "n_drafts": len(all_drafts),
        "usage": {**usage_total, "n_calls": len(normalized)},
        "usage_by_document": usage_records,
        "usage_path": str(state / "usage.json"),
        "drafts": [d.model_dump(mode="json") for d in all_drafts],
    }
    if not verify:
        return _StageOut(payload, 0)

    assert extractor is not None
    report = verify_drafts(all_drafts, normalized, extractor=extractor)
    instruction_docs = frozenset(d.doc_sha256 for d in normalized if d.role == "instruction")
    # An OBJECT, not a bare list, and the `instruction_docs` key is the reason. Which documents were
    # authored FOR seqforge is what decides whether an assertion may touch `processing.*` --
    # and it lived only in this process's memory, so the artifact could not reconstruct the
    # instructable surface and `processing new` had no way to consume it. The join existed in
    # `fill_processing` the whole time and nothing could reach it.
    # `document_subjects` is the same idea one level up: which RECORD each document was rendered
    # from. It is what lets `manifest fill` tell a sample's own alias (a declaration about that
    # sample) from a paper about six samples (an inference about each), and it too lived only in this
    # process's memory. Code owns both mappings because code chose both documents.
    (state / "assertions.json").write_text(
        json.dumps(
            {
                "instruction_docs": sorted(instruction_docs),
                "document_subjects": [
                    {"doc_sha256": d.doc_sha256, "scope": d.scope, "subject": d.subject}
                    for d in sorted(normalized, key=lambda d: d.doc_sha256)
                ],
                "assertions": [a.model_dump(mode="json") for a in report.assertions],
            },
            indent=2,
        )
    )
    # The rendered documents, on disk, under readable names. A span citation is only checkable if the
    # exact text it was greppedded against still exists -- and for a record-derived document these
    # bytes exist nowhere else, because we made them.
    docdir = state / "documents"
    docdir.mkdir(parents=True, exist_ok=True)
    for nd in normalized:
        (docdir / _document_filename(nd)).write_text(nd.text)
    payload["n_accepted"] = report.n_accepted
    payload["n_rejected"] = len(report.rejected)
    # what the user may act on: verified directives, projected onto the instructable surface
    instructions, conflicts = instructions_from_assertions(
        report.assertions, instruction_docs=instruction_docs
    )
    payload["instructions"] = [
        {"field": i.field, "value": i.value, "basis": i.basis, "evidence": i.evidence}
        for i in instructions
    ]
    payload["conflicts"] = [c.model_dump(mode="json") for c in conflicts]
    payload["rejected"] = report.rejected
    payload["assertions"] = [a.model_dump(mode="json") for a in report.assertions]
    # Exit 4 when the author must weigh in: two instructions disagreeing has no tiebreak, and a claim
    # that failed the span tripwire needs a human rather than a silent drop.
    code = 4 if (conflicts or report.rejected) else 0
    return _StageOut(payload, code)


@harvest_app.command("verify")
def harvest_verify(
    drafts_json: Path = typer.Argument(..., help="AssertionDraft[] JSON (from `harvest extract`)."),
    docs: list[Path] = typer.Option(..., "--doc", help="Source document(s) the drafts cite."),
    model_id: str = typer.Option("unknown", help="Model that produced the drafts (provenance)."),
    prompt_version: str = typer.Option("unknown", help="Prompt version (provenance)."),
) -> None:
    """Grep each quote back into the canonical text + check it entails the value. Exit 4 if any fail.

    Both flags are code-owned, so a hallucinated or mis-attributed claim fails closed.
    """
    from .harvest import normalize_document, verify_drafts
    from .models.assertion import AssertionDraft, ExtractorProvenance

    try:
        raw = json.loads(drafts_json.read_text())
        drafts = [AssertionDraft.model_validate(d) for d in raw]
    except (OSError, ValidationError, ValueError) as exc:
        typer.echo(f"cannot read drafts {drafts_json}: {exc}", err=True)
        raise typer.Exit(2) from exc

    normalized = [normalize_document(d) for d in docs]
    report = verify_drafts(
        drafts,
        normalized,
        extractor=ExtractorProvenance(model_id=model_id, prompt_version=prompt_version),
    )
    typer.echo(
        json.dumps(
            {
                "n_drafts": len(drafts),
                "n_accepted": report.n_accepted,
                "n_rejected": len(report.rejected),
                "assertions": [a.model_dump(mode="json") for a in report.assertions],
                "rejected": report.rejected,
            },
            indent=2,
        )
    )
    if report.rejected:
        raise typer.Exit(4)  # a rejected claim needs a human, not a silent drop


@dataclass(frozen=True)
class _StageOut:
    """One stage's result, decoupled from how it is printed.

    A stage decides *what* to say and *whether it is a refusal* (the exit code); the command wrapper
    decides *where* it goes. That split is what lets a single stage body serve both a standalone verb
    (which echoes it and exits) and ``seqforge run`` (which folds it into one summary). ``payload`` is
    a dict rendered as JSON, or a bare string echoed as-is — ``FillError`` prints a plain sentence,
    ``records_unavailable`` prints JSON, and both must keep doing exactly that.
    """

    payload: dict[str, object] | str
    code: int
    err: bool = False


def _emit(out: _StageOut) -> None:
    """Print a stage result the way a standalone verb does, then exit with its code."""
    body = out.payload if isinstance(out.payload, str) else json.dumps(out.payload, indent=2)
    typer.echo(body, err=out.err)
    raise typer.Exit(out.code)


def _auto_cpus(cpus: int) -> int:
    """Resolve ``--cpus``: a positive value is taken as-is; ``0`` means auto = ``min(8, detected)``.

    Files probe in parallel across processes, and cores are not a budget — this only decides how
    fast, never what. ``0`` is the default so the common multicore case is fast without a flag, while a
    shared login node can be pinned with ``--cpus 1``. The cap at 8 keeps a 96-core node from
    fork-bombing itself on a 12-file dataset where the win is already gone by 8.
    """
    if cpus > 0:
        return cpus
    import os

    return max(1, min(8, os.cpu_count() or 1))


def _load_manifest(path: Path) -> DatasetManifest:
    try:
        return DatasetManifest.model_validate(yaml.safe_load(path.read_text()))
    except (OSError, ValidationError, ValueError) as exc:
        typer.echo(f"cannot read manifest {path}: {exc}", err=True)
        raise typer.Exit(2) from exc


def _load_processing(path: Path) -> ProcessingManifest:
    try:
        return ProcessingManifest.model_validate(yaml.safe_load(path.read_text()))
    except (OSError, ValidationError, ValueError) as exc:
        typer.echo(f"cannot read processing manifest {path}: {exc}", err=True)
        raise typer.Exit(2) from exc


def _resolve_organism(value: str, *, offline: bool = False) -> int:
    """`--organism` takes a taxid or a name. A bare integer is taken at face value.

    Not "is it all digits, else look it up" with a fallback -- a name that happens to be numeric is
    not a thing, and a taxid that fails to parse should say so rather than be searched for on NCBI.
    """
    text = value.strip()
    if text.isdigit():
        return int(text)
    return resolve_organism(text, offline=offline)


@manifest_app.command("fill")
def manifest_fill(
    files: list[Path] = typer.Argument(..., help="The dataset's FASTQ .gz files."),
    organism: str | None = typer.Option(
        None,
        "--organism",
        help="NCBI taxid (6239) or scientific name ('Caenorhabditis elegans'). Optional when "
        "--accession is given: the archive record declares the organism. A flag beats the record.",
    ),
    accession: list[str] = typer.Option(
        [],
        "--accession",
        help="Accession(s) for this dataset. Each is FETCHED: the archive's per-sample records are "
        "where strain/tissue/sex/dev_stage come from.",
    ),
    records_path: Path | None = typer.Option(
        None,
        "--records",
        help="An already-fetched record set (`seqforge io records`), instead of fetching now.",
    ),
    assertions: Path | None = typer.Option(
        None,
        "--assertions",
        help="Span-verified assertions from `harvest extract` (seqforge/assertions.json). Without "
        "this, prose contributes nothing and the model might as well not have run.",
    ),
    offline: bool = typer.Option(
        False, "--offline", help="Never reach the network. --accession then REFUSES, never quietly."
    ),
    cpus: int = typer.Option(
        0, "--cpus", help="Parallel probe workers. 0 = auto (min(8, CPUs)); 1 = sequential."
    ),
    workspace: Path = typer.Option(
        Path("."), "-C", "--workspace", help="Root for seqforge/ state."
    ),
) -> None:
    """Probe -> resolve -> assemble the DATASET manifest: what the data IS.

    **Two resolvers, and they answer different questions.** `resolve score` reads the bytes and says
    what the library is. The metadata resolver reads the archive record and any prose and says which
    sample each file is, and what that sample was. Both can refuse; neither is shown the other's
    input.

    **Multi-run by construction.** Files are grouped into runs by name and each run's roles are
    decided from its own bytes, so one sample per run falls out. Hand it all 12 files of a 6-run
    dataset and you get 6 samples, not one guess.

    **An accession is fetched, not decoration.** `--accession PRJNA1027859` pulls the project,
    sample, experiment and run records and joins them to your files. That is where `tissue`, `strain`,
    `sex` and `dev_stage` live; before this they were fetched by no code at all, which is why every
    sample in the pilot's manifest said `tissue: null` under a paper that says "neurons".

    **No accession is fine.** Most sequencing data never had one. You get samples grouped by run with
    no facts attached, exit 0, and a manifest that is quieter and just as true.

    Takes no genome. Choosing a reference is intent, not something you learn by probing bytes, so it
    lives in `seqforge processing new`. Writes manifest.yaml ONLY after a clean validate.
    """
    from .io.remote import RemoteError

    try:
        records = _load_records(accession, records_path, offline=offline)
    except RemoteError as exc:
        # Decision: no network is a refusal, not a quieter answer. You asked for this accession's
        # facts; a manifest that silently omits them is content-addressed and permanent.
        typer.echo(
            json.dumps({"error": "records_unavailable", "detail": str(exc)}, indent=2), err=True
        )
        raise typer.Exit(3) from exc

    _emit(
        _fill_manifest_pipeline(
            files=files,
            organism=organism,
            records=records,
            assertions=assertions,
            offline=offline,
            workspace=workspace,
            cpus=_auto_cpus(cpus),
        )
    )


def _assay_dirname(chemistry: str) -> str:
    """The subdir name for an assay: its chemistry id, made filesystem-safe. Deterministic, no hash."""
    return chemistry.replace("/", "-")


def _fill_one_assay(
    *,
    state: Path,
    result: Any,
    spec: Any,
    observations: list[Any],
    experiment: Any,
    role_of_sha: dict[str, str],
    conflicts: list[Any],
    warnings: list[Any],
    note_workspace: Path | None,
) -> _StageOut:
    """Assemble + validate ONE assay's :class:`DatasetManifest` and write it under ``state``.

    ``state`` is the top-level ``seqforge/`` for a single-assay project (byte-identical to before) or
    ``seqforge/<assay>/`` for one of several. Each manifest is a normal single-chemistry manifest, so
    it flows through today's exact fill/validate/hash code.
    """
    try:
        manifest = fill_manifest(
            result=result,
            spec=spec,
            observations=observations,
            registry=DEFAULT_REGISTRY,
            experiment=experiment,
            seqforge_version=__version__,
            role_of_sha=role_of_sha,
        )
    except FillError as exc:
        return _StageOut(str(exc), 3, err=True)

    report = validate_manifest(manifest, conflicts=conflicts, warnings=warnings)
    state.mkdir(parents=True, exist_ok=True)
    payload = yaml.safe_dump(manifest.model_dump(mode="json"), sort_keys=True)
    # manifest.yaml exists only if it validated clean; otherwise it stays a draft (see _write_manifest).
    target = _write_manifest(state, payload, ok=report.ok)
    out: dict[str, object] = {"manifest": str(target), "report": report.model_dump(mode="json")}
    if note_workspace is not None and (old := legacy_state_dir(note_workspace)) is not None:
        out["note"] = (
            f"{old} is from an older seqforge, which hid its state behind a dot. State now lives in "
            f"{state}/ because it is the output, not plumbing. Nothing reads the old directory; "
            f"delete it when you have what you need."
        )
    return _StageOut(out, exit_code_for_report(report))


def _fill_manifest_pipeline(
    *,
    files: list[Path],
    organism: str | None,
    records: ArchiveRecordSet | None,
    assertions: Path | None,
    offline: bool,
    workspace: Path,
    cpus: int = 1,
) -> _StageOut:
    """Probe -> resolve -> metadata -> PARTITION into assays -> assemble + validate each manifest.

    This is the body of ``manifest fill`` with the network I/O lifted to the caller: ``manifest fill``
    and ``seqforge run`` fetch the archive records differently (one refuses on a miss, one caches to
    disk first), so they hand the already-fetched set in. Every exit is a ``_StageOut`` rather than a
    ``typer.Exit`` — the standalone verb prints it and stops, ``run`` folds it into one summary and
    decides whether to continue. Two resolvers, neither shown the other's input; both can refuse.

    A project splits into **assays** — groups of samples that share one chemistry. One assay yields the
    flat top-level layout, byte-identical to before; several yield one ``seqforge/<assay>/manifest.yaml``
    each and an ``{"assays": [...]}`` summary. The "runs must agree" invariant is now per-SAMPLE (a
    sample split across chemistries blocks); across samples, differing chemistries partition.
    """
    from .resolve import role_of_sha_for
    from .resolve.records import resolve_metadata

    organism_taxid: int | None = None
    if organism is not None:
        # A name, or a taxid typed by hand. `harvest` extracts `experiment.organism` as a NAME with a
        # verified span -- the model already does its job -- so the join it needed was a lookup table.
        try:
            organism_taxid = _resolve_organism(organism, offline=offline)
        except TaxonomyUnavailable as exc:
            return _StageOut(str(exc), 2, err=True)

    parsed, subjects = _assertions_and_subjects(assertions)
    multi = resolve_runs(
        [str(f) for f in files],
        # The protocol paragraph, entering `score` as a SELECTOR and a tie-break -- never as evidence.
        hypothesis=_chemistry_hypothesis(parsed),
        workspace=workspace,
        use_cache=False,
        cpus=cpus,
    )
    if (
        multi.exit_code() != 0
    ):  # a run that itself failed to resolve (no dataset-wide block any more)
        return _StageOut(
            {
                "runs": {r.run_id: r.output.result.model_dump(mode="json") for r in multi.runs},
                "blockers": [b.model_dump(mode="json") for b in multi.blockers],
            },
            multi.exit_code(),
        )

    metadata = resolve_metadata(
        # Identity only: the metadata resolver is handed no probe signal and cannot read one.
        files=[o.file for o in multi.observations],
        records=records,
        assertions=parsed,
        subjects=subjects,
    )
    if metadata.blockers:
        return _StageOut({"blockers": [b.model_dump(mode="json") for b in metadata.blockers]}, 3)

    # The relocated invariant: a single sample whose files span more than one chemistry is a
    # mis-grouping and blocks. Different chemistries across DIFFERENT samples are a legal partition.
    sample_shas = {s.sample_id: list(s.file_shas) for s in metadata.samples}
    if sample_blockers := multi.sample_disagreements(sample_shas):
        return _StageOut({"blockers": [b.model_dump(mode="json") for b in sample_blockers]}, 3)

    groups = multi.by_chemistry()
    if not groups:  # every run carried its own blocker (caught above); nothing to build
        return _StageOut({"error": "no run resolved to a chemistry"}, 3)
    chem_of = multi.chemistry_of_sha()
    multi_assay = len(groups) > 1

    def _build(tech: str, runs: list[Any], state: Path, note_ws: Path | None) -> _StageOut:
        if multi_assay:
            obs = [o for o in multi.observations if chem_of.get(o.file.sha256) == tech]
            samples = [
                s for s in metadata.samples if s.file_shas and chem_of.get(s.file_shas[0]) == tech
            ]
            resolution = metadata.model_copy(update={"samples": samples})
        else:
            obs, resolution = multi.observations, metadata
        # Only the BYTE resolver's conflicts block; a metadata disagreement rides in as a warning.
        conflicts = [c for run in runs for c in run.output.result.conflicts]
        try:
            experiment = experiment_from_metadata(resolution, obs, organism_taxid=organism_taxid)
        except FillError as exc:
            return _StageOut(str(exc), 3, err=True)
        return _fill_one_assay(
            state=state,
            result=runs[0].output.result,  # every run of the assay agreed; any one is the assay's
            spec=load_spec(tech),
            observations=obs,
            experiment=experiment,
            role_of_sha=role_of_sha_for(runs),
            conflicts=conflicts,
            warnings=metadata.warnings,
            note_workspace=note_ws,
        )

    if not multi_assay:
        tech, runs = next(iter(groups.items()))
        return _build(tech, runs, state_dir(workspace), workspace)

    assays: list[dict[str, object]] = []
    worst = 0
    for tech, runs in groups.items():
        n_samples = sum(
            1 for s in metadata.samples if s.file_shas and chem_of.get(s.file_shas[0]) == tech
        )
        out = _build(tech, runs, state_dir(workspace, _assay_dirname(tech)), None)
        worst = max(worst, out.code)
        entry: dict[str, object] = {
            "chemistry": tech,
            "assay_dir": _assay_dirname(tech),
            "n_samples": n_samples,
        }
        entry.update(out.payload if isinstance(out.payload, dict) else {"error": out.payload})
        assays.append(entry)
    return _StageOut({"assays": assays, "n_assays": len(assays)}, worst)


def _write_manifest(state: Path, payload: str, *, ok: bool) -> Path:
    """Write manifest.yaml OR manifest.draft.yaml, and remove the other.

    The removal is the fix. `fill` wrote one name or the other and never unlinked its sibling, so a
    run that failed and was then fixed left `manifest.draft.yaml` sitting next to a good
    `manifest.yaml` forever -- and, far worse, a manifest that USED to validate and now does not left
    the stale clean `manifest.yaml` in place while reporting a draft. Every downstream verb reads
    `manifest.yaml` by name. It would have compiled the old one and said nothing.

    Exactly one of the two exists when this returns. That is the whole contract, and it is what
    "manifest.yaml exists only if it validated clean" was always supposed to mean.
    """
    target = state / ("manifest.yaml" if ok else "manifest.draft.yaml")
    other = state / ("manifest.draft.yaml" if ok else "manifest.yaml")
    target.write_text(payload)
    other.unlink(missing_ok=True)
    return target


def _chemistry_hypothesis(assertions: list[Assertion]) -> Hypothesis | None:
    """The chemistry the prose claims, entering `score` as a hypothesis. ``None`` when it cannot.

    **What this is allowed to do.** `score` builds a grid — one row per read role, one column per
    file — from eight byte-tests, and the hypothesis touches none of them. It orders the candidates
    (so the right whitelist is checked first) and it can break a tie the bytes genuinely cannot
    settle. For prose to move a *score* there would have to be a ninth test, `metadata_says`, and a
    spec could then declare a chemistry that identifies itself by being described rather than by
    what is in its reads. That is the thing we do not build.

    **Agreement or nothing.** Every chemistry claim in the dataset must say the same thing. Two
    experiments describing two protocols is a real dataset, and one dataset-level hypothesis would
    steer both — half of them wrongly. Dropping it costs only a hint: the bytes still decide, and if
    the runs really are two chemistries, `resolve_runs` blocks on the disagreement, which is the right
    answer arrived at honestly.
    """
    values = {a.value for a in assertions if a.field == "library.chemistry"}
    if len(values) != 1:
        return None
    return Hypothesis(value=next(iter(values)), id="harvest", confidence=0.9)


def _load_records(
    accessions: list[str], records_path: Path | None, *, offline: bool
) -> ArchiveRecordSet | None:
    """The archive records for this dataset, or ``None`` if nobody named one.

    ``None`` is the common case and is not a degradation: a plate sequenced last week has no
    accession. But an accession that was *given* and cannot be fetched is a refusal — you asked for
    those facts, and a manifest is content-addressed and never rewritten, so quietly omitting them
    would bake the omission in.
    """
    from .io.archive import fetch_records
    from .io.remote import RemoteError
    from .models.records import ArchiveRecordSet

    if records_path is not None:
        return ArchiveRecordSet.model_validate_json(records_path.read_text())
    if not accessions:
        return None
    if offline:
        raise RemoteError(
            f"--accession {', '.join(accessions)} needs the archive, and --offline forbids it. "
            f"Fetch the records once with `seqforge io records {accessions[0]}` and pass "
            f"`--records`, or drop --accession to compile with no sample facts."
        )
    merged: list[Any] = []
    for acc in accessions:
        merged.extend(fetch_records(acc).records)
    return ArchiveRecordSet(
        source="ncbi-sra+biosample", query=", ".join(accessions), records=merged
    )


def _assertions_and_subjects(path: Path | None) -> tuple[list[Assertion], list[Any]]:
    """Read `harvest extract`'s artifact: the claims, and which record each document came from.

    ``document_subjects`` is the same trick as ``instruction_docs`` beside it — a code-owned mapping
    from document to what code knows about it, written down so a later process can reconstruct it.
    Without it, an assertion's ``doc_sha256`` is an opaque hash and the resolver cannot tell a
    sample's own alias from a paper about six samples, which is the entire difference between a
    declaration and an inference.
    """
    from .resolve.records import DocumentSubject

    if path is None:
        return [], []
    payload = json.loads(path.read_text())
    if isinstance(payload, list):
        raise ValueError(
            "this looks like a pre-2026.7 assertions.json (a bare list). It cannot say which "
            "document each claim came from, so re-run `seqforge harvest extract`."
        )
    parsed = [Assertion.model_validate(a) for a in payload.get("assertions", ())]
    subjects = [
        DocumentSubject(
            doc_sha256=str(d["doc_sha256"]), scope=str(d["scope"]), subject=d.get("subject")
        )
        for d in payload.get("document_subjects", ())
    ]
    return parsed, subjects


@manifest_app.command("validate")
def manifest_validate(
    manifest_path: Path = typer.Argument(..., help="Path to a manifest.yaml."),
) -> None:
    """Validate a manifest. Exit 3 on a Blocker, 4 on an open Conflict."""
    report = validate_manifest(_load_manifest(manifest_path))
    typer.echo(json.dumps(report.model_dump(mode="json"), indent=2))
    raise typer.Exit(exit_code_for_report(report))


@manifest_app.command("hash")
def manifest_hash_cmd(
    manifest_path: Path = typer.Argument(..., help="Path to a manifest.yaml."),
) -> None:
    """Print the dataset manifest's content hash and whether it matches the recorded one."""
    manifest = _load_manifest(manifest_path)
    content = dataset_content_hash(manifest)
    typer.echo(
        json.dumps(
            {
                "dataset_hash": content,
                "recorded_hash": manifest.provenance.dataset_hash,
                "matches": content == manifest.provenance.dataset_hash,
            },
            indent=2,
        )
    )


# ---------------------------------------------------------------- processing (the flags)
def _instructions_from(path: Path | None) -> list[Instruction]:
    """Rebuild the instructable surface from `harvest extract`'s artifact.

    The precedence ladder (§7) is flag > instruction > policy, and `resolve_processing` has always
    implemented it — its `PolicyError` even tells you to "name an assembly in an --instruction
    document". That branch was unreachable: `--assembly` was a REQUIRED option, and nothing passed
    `instructions=` from any production caller. This is the last mile of a join that already existed.

    Note what is NOT happening: the model does not decide anything here. It found a claim in prose and
    code verified the quote greps back and entails the value; this reads that record and applies
    precedence. "We can accept instructions because we never trust the model to act on them, only to
    find them."
    """
    if path is None:
        return []
    payload = json.loads(path.read_text())
    if isinstance(payload, list):
        raise ValueError(
            "this looks like a pre-2026.7 assertions.json (a bare list). It cannot say which "
            "documents were --instruction, and only those may set processing.*. Re-run "
            "`seqforge harvest extract`."
        )
    docs = frozenset(payload.get("instruction_docs", ()))
    parsed = [Assertion.model_validate(a) for a in payload.get("assertions", ())]
    instructions, conflicts = instructions_from_assertions(parsed, instruction_docs=docs)
    if conflicts:
        raise ValueError(
            f"{len(conflicts)} instruction(s) disagree with each other; only their author can "
            f"settle that: " + "; ".join(c.field for c in conflicts)
        )
    return instructions


@processing_app.command("new")
def processing_new(
    dataset_path: Path = typer.Argument(..., help="Path to the dataset manifest.yaml."),
    assembly: str | None = typer.Option(
        None, "--assembly", help="liulab-genome UCSC assembly id (e.g. ce11)."
    ),
    annotation: str | None = typer.Option(
        None, "--annotation", help="Registered GTF name (e.g. WS298)."
    ),
    assertions: Path | None = typer.Option(
        None,
        "--assertions",
        help="Span-verified assertions from `harvest extract` (seqforge/assertions.json). "
        "Instructions in them fill what no flag supplied.",
    ),
    quantify: str | None = typer.Option(
        None,
        "--quantify",
        help="Comma-separated soloFeatures. EXACT replacement of the default (which counts all five).",
    ),
    threads: int | None = typer.Option(None, "--threads", help="Threads per mapping job."),
    processing_id: str = typer.Option("default", "--id", help="Human slug for this recipe."),
    pin: bool = typer.Option(
        True,
        "--pin/--template",
        help="Bind to this dataset's hash, or leave it portable across datasets.",
    ),
    out: Path | None = typer.Option(None, "-o", "--out", help="Write here (default: stdout)."),
) -> None:
    """Author a PROCESSING manifest: what to DO with a dataset. Many per dataset.

    With no flags you get the policy default, which counts every soloFeature — so the common
    case needs no decision from you. --quantify replaces that list exactly; narrowing it warns,
    because dropping a feature is the only irreversible act here.
    """
    dataset = _load_manifest(dataset_path)
    spec = load_spec(dataset.library.chemistry.value[0])
    try:
        instructions = _instructions_from(assertions)
    except (OSError, ValueError, ValidationError) as exc:
        typer.echo(f"{assertions}: {exc}", err=True)
        raise typer.Exit(2) from exc
    try:
        processing, warnings = fill_processing(
            spec=spec,
            dataset=dataset,
            processing=ProcessingInputs(
                assembly=assembly,
                annotation_name=annotation,
                features=_parse_quantify(quantify),
                threads=threads,
            ),
            instructions=instructions,
            processing_id=processing_id,
            pin=pin,
            seqforge_version=__version__,
        )
    except (PolicyError, ValidationError) as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(2) from exc
    report = validate_processing(processing, dataset=dataset)
    payload = yaml.safe_dump(processing.model_dump(mode="json"), sort_keys=True)
    if out is not None:
        out.write_text(payload)
        typer.echo(
            json.dumps(
                {
                    "processing": str(out),
                    "report": report.model_dump(mode="json"),
                    "warnings": [w.model_dump(mode="json") for w in warnings],
                },
                indent=2,
            )
        )
    else:
        typer.echo(payload)
    raise typer.Exit(exit_code_for_report(report))


def _parse_quantify(value: str | None) -> tuple[str, ...] | None:
    """`--quantify Gene,GeneFull` -> the tuple. The MODEL validates membership, not this parser."""
    if value is None:
        return None
    return tuple(v.strip() for v in value.split(",") if v.strip())


@processing_app.command("validate")
def processing_validate(
    processing_path: Path = typer.Argument(..., help="Path to a processing.yaml."),
    dataset_path: Path | None = typer.Option(
        None, "--dataset", help="Cross-check against this dataset manifest (pin + organism)."
    ),
) -> None:
    """Validate a processing manifest. Exit 3 on a Blocker."""
    processing = _load_processing(processing_path)
    dataset = _load_manifest(dataset_path) if dataset_path is not None else None
    report = validate_processing(processing, dataset=dataset)
    typer.echo(json.dumps(report.model_dump(mode="json"), indent=2))
    raise typer.Exit(exit_code_for_report(report))


@processing_app.command("hash")
def processing_hash_cmd(
    processing_path: Path = typer.Argument(..., help="Path to a processing.yaml."),
) -> None:
    """Print the processing manifest's content hash and whether it matches the recorded one."""
    processing = _load_processing(processing_path)
    content = processing_content_hash(processing)
    typer.echo(
        json.dumps(
            {
                "processing_hash": content,
                "recorded_hash": processing.provenance.processing_hash,
                "matches": content == processing.provenance.processing_hash,
                "pinned_to": processing.dataset.dataset_hash if processing.dataset else None,
            },
            indent=2,
        )
    )


@app.command("compose")
def compose_cmd(
    manifest_path: Path = typer.Argument(..., help="Path to a validated manifest.yaml."),
    processing_path: Path | None = typer.Option(
        None, "--processing", help="A processing manifest. Omit to use policy defaults."
    ),
    assembly: str | None = typer.Option(
        None, "--assembly", help="Genome, when composing without --processing."
    ),
    annotation: str | None = typer.Option(
        None, "--annotation", help="Registered GTF name, when composing without --processing."
    ),
    workspace: Path = typer.Option(
        Path("."), "-C", "--workspace", help="Root for seqforge/ state."
    ),
    outdir: str = typer.Option(
        "results", help="Pipeline output directory (written into the config)."
    ),
    fastq_dir: Path | None = typer.Option(
        None,
        "--fastq-dir",
        help="Where this machine keeps the FASTQs. Without it units.tsv carries bare basenames "
        "and the pipeline cannot find its input.",
    ),
    onlist_dir: Path | None = typer.Option(
        None,
        "--onlist-dir",
        help="Directory of downloaded barcode whitelists (<name>.txt.gz). Checked before the "
        "network, so a compute node with no internet still composes. Env: SEQFORGE_ONLIST_DIR.",
    ),
    sif_dir: Path | None = typer.Option(
        None,
        "--sif-dir",
        envvar="LIU_LAB_PACKAGES",
        help="Directory of prebuilt liulab-runtime images (liulab-runtime_<env>.sif). Used instead "
        "of the ghcr tag when the file is there, for nodes that cannot reach ghcr.io.",
    ),
) -> None:
    """Compile (dataset, processing) -> Snakefile + config.yaml + units.tsv.

    ``--processing`` is optional: a processing manifest exists because someone wanted something
    non-default, and requiring one per dataset would mean 10^4 boilerplate files nobody reads. Either
    way compose writes the fully-resolved, dataset-bound manifest it used to processing.lock.yaml, so
    the run's state is on disk regardless. Exit 3 if a gate fails.
    """
    manifest = _load_manifest(manifest_path)
    report = validate_manifest(manifest)
    if not report.ok:
        typer.echo(json.dumps(report.model_dump(mode="json"), indent=2), err=True)
        typer.echo("refusing to compose an invalid manifest", err=True)
        raise typer.Exit(exit_code_for_report(report))

    if processing_path is not None:
        processing = _load_processing(processing_path)
    else:
        if assembly is None or annotation is None:
            # The one thing with no safe default. Deriving an assembly from experiment.organism would
            # mean choosing hg38 vs hg19 vs T2T on the user's behalf — a policy call, and that map is
            # liulab-genome's job. Refuse, but make the refusal actionable.
            typer.echo(
                f"compose needs a genome: this dataset's organism is taxid "
                f"{manifest.experiment.organism.value}. Pass --assembly/--annotation, or author one "
                f"with `seqforge processing new`.",
                err=True,
            )
            raise typer.Exit(2)
        processing, _ = fill_processing(
            spec=load_spec(manifest.library.chemistry.value[0]),
            dataset=manifest,
            processing=ProcessingInputs(assembly=assembly, annotation_name=annotation),
            seqforge_version=__version__,
        )

    p_report = validate_processing(processing, dataset=manifest)
    if not p_report.ok:
        typer.echo(json.dumps(p_report.model_dump(mode="json"), indent=2), err=True)
        typer.echo("refusing to compose with an invalid processing manifest", err=True)
        raise typer.Exit(exit_code_for_report(p_report))

    try:
        result = compose(
            manifest,
            processing,
            registry=default_registry(offline=False, local_dir=onlist_dir),
            workspace=workspace,
            outdir=outdir,
            fastq_dir=fastq_dir,
            sif_dir=sif_dir,
        )
    except ComposeError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(3) from exc
    typer.echo(json.dumps(result.model_dump(mode="json"), indent=2))
    if any(v == "fail" for v in result.gate.values()):
        raise typer.Exit(3)


# ---------------------------------------------------------------- run (the whole pipeline, one pass)
def _run_records_stage(
    accession: list[str], records_path: Path | None, *, workspace: Path, offline: bool
) -> tuple[ArchiveRecordSet | None, Path | None]:
    """Fetch + cache the archive records for `run`, returning (the set, a file harvest can render from).

    Where `manifest fill` fetches into memory, `run` writes each record set under `seqforge/records/`
    — the same place `io records` caches — because `run` is the convenience path and every
    stage leaves a resumable artifact. Harvest renders record documents from a *file*, so `run` hands
    the same file to both harvest and fill. `--offline` with an accession refuses, for the reason fill
    does: you asked for those facts, and the manifest is content-addressed and permanent.
    """
    import hashlib

    from .io.archive import fetch_records
    from .io.remote import RemoteError
    from .models.records import ArchiveRecordSet

    if records_path is not None:
        return ArchiveRecordSet.model_validate_json(records_path.read_text()), records_path
    if not accession:
        return None, None
    if offline:
        raise RemoteError(
            f"--accession {', '.join(accession)} needs the archive, and --offline forbids it. "
            f"Fetch once with `seqforge io records {accession[0]}` and pass --records, or drop "
            f"--accession to compile with no sample facts."
        )
    outdir = state_dir(workspace, "records")
    outdir.mkdir(parents=True, exist_ok=True)
    merged: list[Any] = []
    per_accession: list[Path] = []
    for acc in accession:
        record_set = fetch_records(acc)
        target = outdir / f"{acc}.json"
        target.write_text(json.dumps(record_set.model_dump(mode="json"), indent=2))
        per_accession.append(target)
        merged.extend(record_set.records)
    if len(accession) == 1:
        return ArchiveRecordSet(source="ncbi-sra+biosample", query=accession[0], records=merged), (
            per_accession[0]
        )
    # Two accessions render one dataset: harvest needs them in a single document set, so write a
    # combined file keyed by the accession list (the per-accession caches stay, for `io records`).
    combined = ArchiveRecordSet(
        source="ncbi-sra+biosample", query=", ".join(accession), records=merged
    )
    tag = hashlib.sha256(", ".join(sorted(accession)).encode()).hexdigest()
    combined_path = outdir / (readable("combined", tag) + ".json")
    combined_path.write_text(json.dumps(combined.model_dump(mode="json"), indent=2))
    return combined, combined_path


def _run_finish(stages: dict[str, object], code: int) -> None:
    """Emit the single `run` summary and exit with the pipeline's code. Always raises."""
    summary: dict[str, object] = {"ok": code == 0, "exit_code": code, "stages": stages}
    harvest = stages.get("harvest")
    if isinstance(harvest, dict) and isinstance(harvest.get("usage"), dict):
        # The token cost of understanding the prose, surfaced at the top: the full per-document ledger
        # is on disk (seqforge/usage.json) and in the harvest stage; this is the total a reader wants.
        summary["llm_usage"] = harvest["usage"]
    if code == 0:
        assays = stages.get("assays")
        if isinstance(assays, list):  # multi-assay: one manifest + Snakefile per assay
            summary["assays"] = [
                {
                    "chemistry": a.get("chemistry"),
                    "manifest": a.get("manifest"),
                    "snakefile": cast(dict, a.get("compose", {})).get("snakefile_path"),
                }
                for a in assays
            ]
        else:
            summary["manifest"] = cast(dict, stages.get("manifest", {})).get("manifest")
            summary["processing"] = cast(dict, stages.get("processing", {})).get("processing")
            summary["snakefile"] = cast(dict, stages.get("compose", {})).get("snakefile_path")
    typer.echo(json.dumps(summary, indent=2))
    raise typer.Exit(code)


def _harvest_halts_run(payload: dict[str, object] | str, code: int) -> bool:
    """Does a harvest result stop the one-pass, or is it surfaced and stepped past?

    A **conflict** (two instructions disagreeing on a `processing.*` field) or an unavailable provider
    halts `run` — the first decides a value nothing else can, the second means the LLM stage could not
    run at all. A **rejected reference claim** does not: it never entered `assertions.json`, so the
    manifest is built from the accepted claims and the bytes, and chemistry comes from bytes anyway. It
    is reported in the summary (`needs_review` + the `rejected` list), which is what we ask for — "not a
    silent drop" — while letting a paper whose prose the span-checker cannot formally tie to a KB id
    still compile. Standalone `harvest extract` keeps exiting 4 on a rejection; only `run` steps past.
    """
    if code == 0:
        return False
    if code == 4 and isinstance(payload, dict) and not (payload.get("conflicts") or []):
        return False
    return True


def _process_and_compose(
    *,
    manifest: Any,
    state: Path,
    subdir: str | None,
    workspace: Path,
    assembly: str | None,
    annotation: str | None,
    assertions_path: Path | None,
    processing_id: str,
    offline: bool,
    onlist_dir: Path | None,
    outdir: str,
    fastq_dir: Path | None,
    sif_dir: Path | None,
) -> tuple[dict[str, object], int]:
    """Stages 4-5 for ONE assay: the flags (``processing.yaml``) + the deliverable (the Snakefile).

    Writes ``processing.yaml`` under ``state`` and the pipeline under ``seqforge/<subdir>/pipeline/``
    (the flat ``seqforge/pipeline/`` when ``subdir`` is None). Returns ``(summary, exit_code)``; the
    caller folds it into the run summary. Same code the single-assay path always ran, per assay.
    """
    summary: dict[str, object] = {}
    try:
        instructions = _instructions_from(assertions_path)
    except (OSError, ValueError, ValidationError) as exc:
        return {"processing": {"error": str(exc)}}, 2
    try:
        processing, warnings = fill_processing(
            spec=load_spec(manifest.library.chemistry.value[0]),
            dataset=manifest,
            processing=ProcessingInputs(assembly=assembly, annotation_name=annotation),
            instructions=instructions,
            processing_id=processing_id,
            pin=True,
            seqforge_version=__version__,
        )
    except (PolicyError, ValidationError) as exc:
        # The one real decision with no safe default; fill_processing's message already names the
        # organism and how to supply a genome, so pass it through.
        return {"processing": {"error": str(exc)}}, 2
    p_report = validate_processing(processing, dataset=manifest)
    proc_path = state / "processing.yaml"
    proc_path.write_text(yaml.safe_dump(processing.model_dump(mode="json"), sort_keys=True))
    summary["processing"] = {
        "processing": str(proc_path),
        "report": p_report.model_dump(mode="json"),
        "warnings": [w.model_dump(mode="json") for w in warnings],
    }
    if not p_report.ok:
        return summary, exit_code_for_report(p_report)

    try:
        result = compose(
            manifest,
            processing,
            registry=default_registry(offline=offline, local_dir=onlist_dir),
            workspace=workspace,
            outdir=outdir,
            fastq_dir=fastq_dir,
            sif_dir=sif_dir,
            subdir=subdir,
        )
    except ComposeError as exc:
        summary["compose"] = {"error": str(exc)}
        return summary, 3
    summary["compose"] = result.model_dump(mode="json")
    return summary, (3 if any(v == "fail" for v in result.gate.values()) else 0)


@app.command("run")
def run_cmd(
    files: list[Path] = typer.Argument(..., help="The dataset's FASTQ .gz files."),
    accession: list[str] = typer.Option(
        [], "--accession", help="Accession(s): the archive's per-sample records. Optional."
    ),
    records_path: Path | None = typer.Option(
        None, "--records", help="An already-fetched record set, instead of fetching now."
    ),
    doc: list[Path] = typer.Option(
        [], "--doc", help="Reference document(s) — a paper .pdf/.txt/.md — to read for claims."
    ),
    instruction: list[Path] = typer.Option(
        [],
        "--instruction",
        help="Document(s) authored FOR seqforge; only these may set processing.*.",
    ),
    organism: str | None = typer.Option(
        None, "--organism", help="NCBI taxid or name. Optional when --accession declares it."
    ),
    assembly: str | None = typer.Option(
        None,
        "--assembly",
        help="Genome: liulab-genome UCSC assembly id (e.g. ce11). The one decision.",
    ),
    annotation: str | None = typer.Option(
        None, "--annotation", help="Registered GTF name (e.g. WS298)."
    ),
    no_llm: bool = typer.Option(
        False, "--no-llm", help="Skip the one LLM stage; fully deterministic. Ignores --doc."
    ),
    provider: str | None = typer.Option(
        None, "--provider", help="anthropic | deepseek | openai-compatible (default: auto-detect)."
    ),
    model: str | None = typer.Option(None, "--model", help="Override the extraction model."),
    processing_id: str = typer.Option("default", "--id", help="Human slug for the recipe."),
    fastq_dir: Path | None = typer.Option(
        None, "--fastq-dir", help="Where this machine keeps the FASTQs (for units.tsv)."
    ),
    onlist_dir: Path | None = typer.Option(
        None,
        "--onlist-dir",
        envvar="SEQFORGE_ONLIST_DIR",
        help="Directory of downloaded barcode whitelists (<name>.txt.gz).",
    ),
    sif_dir: Path | None = typer.Option(
        None,
        "--sif-dir",
        envvar="LIU_LAB_PACKAGES",
        help="Directory of prebuilt liulab-runtime images (liulab-runtime_<env>.sif).",
    ),
    outdir: str = typer.Option("results", help="Pipeline output directory (written into config)."),
    offline: bool = typer.Option(False, "--offline", help="Never reach the network."),
    cpus: int = typer.Option(
        0, "--cpus", help="Parallel probe workers. 0 = auto (min(8, CPUs)); 1 = sequential."
    ),
    workspace: Path = typer.Option(
        Path("."), "-C", "--workspace", help="Root for seqforge/ state."
    ),
) -> None:
    """One pass: FASTQ + metadata -> manifest.yaml AND a runnable Snakefile.

    Chains the deterministic verbs — records, harvest, manifest fill, processing new, compose — in
    order, stops at the first refusal, and emits ONE JSON summary keyed by stage. It decides nothing
    itself: chemistry, read roles and organism come from the same code the individual verbs run, and
    the exit-code contract is preserved (3 BLOCKED, 4 NEEDS_HUMAN). Re-running is resumable through
    each stage's own content-addressed cache; there is no --resume flag.

    The genome is the one real decision and has no safe default: pass --assembly/--annotation, or state
    it in an --instruction document. Everything else is optional — no accession, no paper, and
    --no-llm each give a quieter, still-true manifest. `harvest extract` is the sole LLM touchpoint and
    calls its own provider (DEEPSEEK_API_KEY / ANTHROPIC_API_KEY), which is why --no-llm exists.
    """
    from .io.remote import RemoteError

    stages: dict[str, object] = {}

    # 1) Archive records (optional): fetch + cache, or refuse offline.
    records: ArchiveRecordSet | None = None
    records_file: Path | None = None
    try:
        records, records_file = _run_records_stage(
            accession, records_path, workspace=workspace, offline=offline
        )
    except RemoteError as exc:
        stages["records"] = {"error": "records_unavailable", "detail": str(exc)}
        _run_finish(stages, 3)
    if records is not None:
        stages["records"] = {
            "source": records.source,
            "n": {
                level: len(records.at(level))  # type: ignore[arg-type]
                for level in ("project", "sample", "experiment", "run")
            },
        }

    # 2) Harvest — the one LLM stage. Skipped by --no-llm or when there is no prose to read.
    assertions_path: Path | None = None
    if no_llm and (doc or instruction):
        stages["harvest"] = {"skipped": "--no-llm: documents were not read"}
    elif not no_llm and (doc or instruction):
        harvested = _harvest_extract_pipeline(
            docs=doc,
            instruction=instruction,
            records_path=records_file,
            provider=provider,
            model=model,
            verify=True,
            workspace=workspace,
        )
        stages["harvest"] = (
            harvested.payload
            if isinstance(harvested.payload, dict)
            else {"error": harvested.payload}
        )
        if _harvest_halts_run(harvested.payload, harvested.code):
            _run_finish(stages, harvested.code)
        if harvested.code == 4:
            # rejected reference claims survived the halt check: surface them, do not stop (see
            # `_harvest_halts_run`). They were dropped from assertions.json already; this is the "not
            # a silent drop" we ask for, in a field a headless caller still sees.
            cast(dict, stages["harvest"])["needs_review"] = (
                "prose claims failed span-verification and were dropped (see 'rejected'); the manifest "
                "was built from the accepted claims and the bytes"
            )
        assertions_path = state_dir(workspace) / "assertions.json"

    # 3) The IR: what the data IS. Probe + resolve + metadata, both resolvers, both able to refuse.
    fill = _fill_manifest_pipeline(
        files=files,
        organism=organism,
        records=records,
        assertions=assertions_path,
        offline=offline,
        workspace=workspace,
        cpus=_auto_cpus(cpus),
    )
    stages["manifest"] = fill.payload if isinstance(fill.payload, dict) else {"error": fill.payload}
    if fill.code != 0:
        _run_finish(stages, fill.code)

    # A project is one assay (the flat, byte-identical layout) or several (one seqforge/<assay>/ each).
    manifest_payload = cast(dict, stages["manifest"])
    if "assays" in manifest_payload:
        targets = [
            (cast(str, a["chemistry"]), cast(str, a["assay_dir"]), Path(cast(str, a["manifest"])))
            for a in cast(list, manifest_payload["assays"])
        ]
    else:
        targets = [(None, None, Path(cast(str, manifest_payload["manifest"])))]

    # 4-5) The flags + the deliverable, per assay. Each is a normal single-chemistry compile.
    compiled: list[tuple[str | None, str, dict[str, object], int]] = []
    worst = 0
    for chemistry, subdir, manifest_path in targets:
        manifest = _load_manifest(manifest_path)
        state = state_dir(workspace, subdir) if subdir else state_dir(workspace)
        summary, code = _process_and_compose(
            manifest=manifest,
            state=state,
            subdir=subdir,
            workspace=workspace,
            assembly=assembly,
            annotation=annotation,
            assertions_path=assertions_path,
            processing_id=processing_id,
            offline=offline,
            onlist_dir=onlist_dir,
            outdir=outdir,
            fastq_dir=fastq_dir,
            sif_dir=sif_dir,
        )
        worst = max(worst, code)
        compiled.append((chemistry, str(manifest_path), summary, code))

    if targets[0][0] is None:  # single assay: flat stages, byte-identical to before
        _, _, summary, code = compiled[0]
        if "processing" in summary:
            stages["processing"] = summary["processing"]
        if "compose" in summary:
            stages["compose"] = summary["compose"]
        _run_finish(stages, code)
    else:  # multi-assay: one complete record per assay
        stages["assays"] = [
            {"chemistry": chem, "manifest": mpath, **summary}
            for chem, mpath, summary, _ in compiled
        ]
        _run_finish(stages, worst)


app.command(
    "compile", help="Alias for `run`: FASTQ + metadata -> manifest + Snakefile in one pass."
)(run_cmd)


@eval_app.command("list")
def eval_list(
    cases_dir: Path | None = typer.Option(
        None, "--cases", help="Case root (default: evals/cases)."
    ),
) -> None:
    """List the eval corpus: id, expected outcome, and whether the case needs an LLM."""
    from .evals import CaseError, load_cases

    try:
        cases = load_cases(cases_dir)
    except CaseError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(2) from exc
    payload = [
        {
            "id": c.id,
            "outcome": c.expected.outcome,
            "needs_llm": c.has_prose and c.recipe.hypothesis is None,
            "description": " ".join(c.expected.description.split())[:100],
        }
        for c in cases
    ]
    typer.echo(json.dumps(payload, indent=2))


@eval_app.command("run")
def eval_run(
    case: list[str] = typer.Option(None, "--case", help="Run only these case ids (repeatable)."),
    cases_dir: Path | None = typer.Option(
        None, "--cases", help="Case root (default: evals/cases)."
    ),
    llm: bool = typer.Option(
        False, "--llm/--no-llm", help="Run prose cases through harvest extract (costs tokens)."
    ),
    provider: str | None = typer.Option(
        None, "--provider", help="anthropic | deepseek | openai-compatible (default: auto-detect)."
    ),
    model: str | None = typer.Option(
        None, "--model", help="Override the provider's default model."
    ),
    trials: int = typer.Option(
        1, "--trials", min=1, help="Re-run each prose case N times; extraction is nondeterministic."
    ),
    fail_under: float = typer.Option(
        1.0, "--fail-under", help="Exit 3 if field accuracy drops below this."
    ),
) -> None:
    """Run the eval corpus and report brief §9's metrics.

    `--no-llm` (the default) restricts to deterministic cases, so this runs in a CI with no API key;
    prose cases skip rather than fail. Exit 3 if any false-accept occurs or accuracy drops below
    `--fail-under` — a false accept is never tolerable at any threshold, so it is not on a slider.
    """
    from .evals import CaseError, Grade, load_cases, run_cases
    from .harvest import ProviderUnavailable, resolve_provider

    try:
        cases = load_cases(cases_dir, only=list(case) if case else None)
    except CaseError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(2) from exc
    if not cases:
        typer.echo("no cases found", err=True)
        raise typer.Exit(2)

    llm_provider = None
    if llm:
        try:
            llm_provider = resolve_provider(provider)
        except ProviderUnavailable as exc:
            typer.echo(json.dumps({"error": "no_provider", "detail": str(exc)}, indent=2), err=True)
            raise typer.Exit(1) from exc

    report, runs = run_cases(cases, llm=llm, provider=llm_provider, model=model, trials=trials)
    typer.echo(json.dumps(report.model_dump(mode="json"), indent=2))

    false_accepts = [r for r in runs if r.skipped is None and r.grade.grade is Grade.FALSE_ACCEPT]
    if false_accepts:
        typer.echo(
            f"FALSE ACCEPT in {len(false_accepts)} case(s): "
            f"{[r.case_id for r in false_accepts]} — a confident wrong manifest is the one "
            f"failure the corpus never recovers from",
            err=True,
        )
        raise typer.Exit(3)
    if report.field_accuracy < fail_under:
        typer.echo(
            f"field accuracy {report.field_accuracy:.3f} < --fail-under {fail_under}", err=True
        )
        raise typer.Exit(3)


@hook_app.command("pre-tool-use")
def hook_pre_tool_use() -> None:
    """Deny an unbounded FASTQ stream or an absolute path in a manifest.

    Reads the hook payload on stdin, emits a permissionDecision on stdout. Exit 0 always: the decision
    travels in the JSON, and a crashing guard must never wedge the agent.
    """
    from .hooks import pre_tool_use

    payload = _hook_payload()
    denial = pre_tool_use(payload)
    if denial is None:
        raise typer.Exit(0)
    typer.echo(
        json.dumps(
            {
                "hookSpecificOutput": {
                    "hookEventName": "PreToolUse",
                    "permissionDecision": "deny",
                    "permissionDecisionReason": denial.message(),
                }
            }
        )
    )


@hook_app.command("post-tool-use")
def hook_post_tool_use() -> None:
    """After any manifest edit, re-run `manifest validate`. The model does not grade its own work."""
    from .hooks import post_tool_use_targets

    payload = _hook_payload()
    target = post_tool_use_targets(payload)
    if target is None or not Path(target).is_file():
        raise typer.Exit(0)
    try:
        manifest = _load_manifest(Path(target))
        report = validate_manifest(manifest)
    except (FillError, ValidationError, ValueError, OSError) as exc:
        typer.echo(
            json.dumps(
                {
                    "decision": "block",
                    "reason": f"{target} did not parse as a Manifest: {exc}",
                }
            )
        )
        raise typer.Exit(0) from None
    if report.ok:
        typer.echo(
            json.dumps(
                {
                    "hookSpecificOutput": {
                        "hookEventName": "PostToolUse",
                        "additionalContext": f"manifest validate: OK ({Path(target).name}).",
                    }
                }
            )
        )
        raise typer.Exit(0)
    codes = [str(getattr(b.code, "value", b.code)) for b in report.blockers]
    typer.echo(
        json.dumps(
            {
                "decision": "block",
                "reason": (
                    f"manifest validate FAILED on {Path(target).name} (exit "
                    f"{exit_code_for_report(report)}): {codes}. Refusal is the contract — fix "
                    "the manifest; do not proceed as though it validated."
                ),
            }
        )
    )


@hook_app.command("stop")
def hook_stop(
    workspace: Path = typer.Option(Path("."), "-C", "--workspace", help="Root holding seqforge/."),
) -> None:
    """Refuse to end the turn while questions.md is non-empty — ambiguity routes to a human."""
    from .hooks import stop_decision

    payload = _hook_payload()
    reason = stop_decision(payload, workspace=workspace)
    if reason is None:
        raise typer.Exit(0)
    typer.echo(json.dumps({"decision": "block", "reason": reason}))


@hook_app.command("install")
def hook_install(
    workspace: Path = typer.Option(Path("."), "-C", "--workspace", help="Project root."),
    force: bool = typer.Option(False, "--force", help="Overwrite an existing hooks block."),
) -> None:
    """Write the three hooks into .claude/settings.json, merging with whatever is already there."""
    from .hooks import HOOKS_VERSION

    settings_path = Path(workspace) / ".claude" / "settings.json"
    settings_path.parent.mkdir(parents=True, exist_ok=True)
    settings: dict[str, object] = {}
    if settings_path.is_file():
        try:
            settings = json.loads(settings_path.read_text() or "{}")
        except json.JSONDecodeError as exc:
            typer.echo(f"{settings_path} is not valid JSON: {exc}", err=True)
            raise typer.Exit(1) from exc
    existing = settings.get("hooks")
    if existing and not force:
        typer.echo(
            json.dumps(
                {
                    "ok": False,
                    "reason": f"{settings_path} already defines hooks; re-run with --force to replace",
                },
                indent=2,
            ),
            err=True,
        )
        raise typer.Exit(2)

    run = "${CLAUDE_PROJECT_DIR}/.claude/hooks/seqforge-hook.sh"
    settings["hooks"] = {
        "PreToolUse": [
            {
                "matcher": "Bash|Write|Edit|NotebookEdit|Read|Grep|Glob",
                "hooks": [{"type": "command", "command": f"{run} pre-tool-use", "args": []}],
            }
        ],
        "PostToolUse": [
            {
                "matcher": "Write|Edit|NotebookEdit",
                "hooks": [{"type": "command", "command": f"{run} post-tool-use", "args": []}],
            }
        ],
        "Stop": [{"hooks": [{"type": "command", "command": f"{run} stop", "args": []}]}],
    }
    settings_path.write_text(json.dumps(settings, indent=2) + "\n")

    shim = Path(workspace) / ".claude" / "hooks" / "seqforge-hook.sh"
    shim.parent.mkdir(parents=True, exist_ok=True)
    shim.write_text(
        "#!/usr/bin/env bash\n"
        "# Generated by `seqforge hook install`. The guard logic lives in seqforge.hooks (typed +\n"
        "# tested); this only routes the event. Fails OPEN by design: a broken hook must not wedge\n"
        "# the agent, and a guard that can hang is worse than the risk it manages.\n"
        "set -uo pipefail\n"
        'cd "${CLAUDE_PROJECT_DIR:-.}" || exit 0\n'
        'exec pixi run -q -- python -m seqforge.cli hook "$@" 2>/dev/null || exit 0\n'
    )
    shim.chmod(0o755)
    typer.echo(
        json.dumps(
            {
                "ok": True,
                "hooks_version": HOOKS_VERSION,
                "settings": str(settings_path),
                "shim": str(shim),
                "events": ["PreToolUse", "PostToolUse", "Stop"],
            },
            indent=2,
        )
    )


@hook_app.command("check")
def hook_check(
    workspace: Path = typer.Option(Path("."), "-C", "--workspace", help="Root holding seqforge/."),
) -> None:
    """Self-test: prove each guard fires. A guard nobody has watched deny is not a guard.

    A hook that silently never fires is indistinguishable from one that always allows — so this
    exercises every rule against a known-bad payload and reports what it caught.
    """
    from .hooks import HOOKS_VERSION, pre_tool_use, questions_outstanding

    cases = [
        (
            "unbounded FASTQ",
            {"tool_name": "Bash", "tool_input": {"command": "zcat big.fastq.gz | wc -l"}},
        ),
        (
            "allows a bounded stream",
            {"tool_name": "Bash", "tool_input": {"command": "zcat big.fastq.gz | head -n 400"}},
        ),
        (
            "allows the seqforge verb",
            {"tool_name": "Bash", "tool_input": {"command": "seqforge probe big.fastq.gz"}},
        ),
        (
            "absolute path in manifest",
            {
                "tool_name": "Write",
                "tool_input": {
                    "file_path": "manifest.yaml",
                    "file_text": "genome: /data/ref/hg38.fa\n",
                },
            },
        ),
    ]
    results = []
    for name, payload in cases:
        denial = pre_tool_use(payload)
        results.append(
            {"case": name, "denied": denial is not None, "rule": denial.rule if denial else None}
        )
    typer.echo(
        json.dumps(
            {
                "hooks_version": HOOKS_VERSION,
                "open_questions": [str(p) for p in questions_outstanding(workspace)],
                "checks": results,
            },
            indent=2,
        )
    )


def _hook_payload() -> dict[str, object]:
    """Read the hook event from stdin. A malformed payload means NO OPINION, never a crash."""
    import sys

    try:
        raw = sys.stdin.read()
    except (OSError, ValueError):
        return {}
    if not raw.strip():
        return {}
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


if __name__ == "__main__":  # pragma: no cover
    app()
