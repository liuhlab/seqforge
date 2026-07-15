"""The ``seqforge`` Typer application — the CLI is the API (R8).

Every skill action maps to a deterministic ``seqforge <verb> --json`` that runs with no LLM in the
loop (only ``harvest extract`` and the opt-in ``resolve adjudicate`` touch an LLM). Exit codes are
uniform: ``0`` OK, ``1`` ERROR, ``2`` USAGE, ``3`` BLOCKED (a Blocker), ``4`` NEEDS_HUMAN (an open
Conflict / question).

Milestone 0 wires the deterministic spine incrementally; ``schema export`` is live, the remaining
verbs are declared and raise a clear "not yet implemented" until their stage lands.
"""

from __future__ import annotations

import json
from pathlib import Path

import typer
from pydantic import ValidationError

from . import __version__
from .io import DEFAULT_REGISTRY
from .io.remote import NotYetImplemented, peek, resolve_accession
from .kb import list_spec_ids, load_spec, run_roundtrip
from .models import SCHEMA_MODELS, export_all, export_schema
from .resolve import Hypothesis, resolve_dataset

app = typer.Typer(
    name="seqforge",
    help="Compile FASTQ + metadata into a validated library manifest and a Snakemake config.",
    no_args_is_help=True,
    add_completion=False,
)

schema_app = typer.Typer(help="Export JSON Schema from the Pydantic models (the source of truth).")
app.add_typer(schema_app, name="schema")

kb_app = typer.Typer(help="The executable, self-testing knowledge base (R10).")
app.add_typer(kb_app, name="kb")

io_app = typer.Typer(help="The network + onlist surface (pooch-cached, sha256-verified).")
app.add_typer(io_app, name="io")

onlist_app = typer.Typer(help="Barcode-whitelist (onlist) registry.")
io_app.add_typer(onlist_app, name="onlist")

resolve_app = typer.Typer(help="Score bytes + KB into a ranked, escalated chemistry decision.")
app.add_typer(resolve_app, name="resolve")


@app.command()
def version() -> None:
    """Print the seqforge version."""
    typer.echo(__version__)


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


@onlist_app.command("list")
def io_onlist_list() -> None:
    """List the onlists declared in the default registry (none are materialized in the pilot)."""
    rows = []
    for name in DEFAULT_REGISTRY.names():
        entry = DEFAULT_REGISTRY.get(name)
        rows.append(
            {
                "name": entry.name,
                "width": entry.width,
                "orientation": entry.orientation,
                "n_entries": entry.n_entries,
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
                "fetchable": entry.fetchable,
            },
            indent=2,
        )
    )


@io_app.command("peek")
def io_peek(uri: str = typer.Argument(..., help="Remote FASTQ URI to range-read.")) -> None:
    """Range-read a remote FASTQ header into a partial Observation (not yet implemented)."""
    try:
        typer.echo(json.dumps(peek(uri)))
    except NotYetImplemented as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(1) from exc


@io_app.command("resolve")
def io_resolve(
    accession: str = typer.Argument(..., help="ENA/SRA/GEO/BioProject accession."),
) -> None:
    """Expand an accession into a file inventory (not yet implemented)."""
    try:
        typer.echo(json.dumps(resolve_accession(accession)))
    except NotYetImplemented as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(1) from exc


@resolve_app.command("score")
def resolve_score(
    files: list[Path] = typer.Argument(..., help="The dataset's FASTQ .gz files."),
    workspace: Path = typer.Option(
        Path("."), "-C", "--workspace", help="Root for .seqforge/ state."
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
        False, "--no-cache", help="Do not read/write .seqforge/ artifacts."
    ),
    max_reads: int = typer.Option(200_000, help="Bounded read budget (R3)."),
    max_bytes: int = typer.Option(256 * 1024 * 1024, help="Bounded decompressed-byte cap (R3)."),
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
    )
    payload: dict[str, object] = output.result.model_dump(mode="json")
    if explain:
        payload = {"result": payload, "matrices": output.matrices}
    typer.echo(json.dumps(payload, indent=2))
    code = output.exit_code()
    if code != 0:
        raise typer.Exit(code)


if __name__ == "__main__":  # pragma: no cover
    app()
