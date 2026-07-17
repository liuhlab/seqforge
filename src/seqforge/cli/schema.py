"""`seqforge schema` -- export JSON Schema from the Pydantic models (the single source of truth)."""

from __future__ import annotations

import json

import typer

from ..models import SCHEMA_MODELS, export_all, export_schema
from .root import schema_app


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
