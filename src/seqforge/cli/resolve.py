"""`seqforge resolve` -- score FASTQ bytes + KB into a ranked, escalated chemistry decision."""

from __future__ import annotations

import json
from pathlib import Path

import typer

from ..probe import DEFAULT_MAX_BYTES, DEFAULT_MAX_READS
from ..resolve import Hypothesis, resolve_dataset
from ._common import _auto_cpus
from .root import resolve_app


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
    max_reads: int = typer.Option(
        DEFAULT_MAX_READS,
        help="Bounded read budget per file (default 2000). Raise it to score more of a full-size "
        "FASTQ — the explicit opt-in; every touch stays bounded by this AND --max-bytes.",
    ),
    max_bytes: int = typer.Option(DEFAULT_MAX_BYTES, help="Bounded decompressed-byte cap."),
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
