"""`seqforge probe` -- deterministic, bounded FASTQ fingerprinting into role-free Observations."""

from __future__ import annotations

import json
from pathlib import Path

import typer

from ..probe import DEFAULT_MAX_BYTES, DEFAULT_MAX_READS
from .root import app


@app.command("probe")
def probe_cmd(
    files: list[Path] = typer.Argument(..., help="FASTQ .gz files to fingerprint."),
    workspace: Path = typer.Option(
        Path("."), "-C", "--workspace", help="Root for seqforge/ state."
    ),
    max_reads: int = typer.Option(
        DEFAULT_MAX_READS,
        help="Bounded read budget per file (default 2000). Raise it to sample more of a full-size "
        "FASTQ — the explicit opt-in; every touch stays bounded by this AND --max-bytes.",
    ),
    max_bytes: int = typer.Option(DEFAULT_MAX_BYTES, help="Bounded decompressed-byte cap."),
    no_cache: bool = typer.Option(False, "--no-cache", help="Do not write seqforge/ artifacts."),
) -> None:
    """Fingerprint FASTQ bytes into role-free Observations. No LLM, no network, bounded.

    The budget is the point: a 40 GB file costs the same as a 40 MB one, because probe stops at
    --max-reads AND --max-bytes, whichever comes first. Never returns 3/4 — it only observes; refusal
    happens downstream when a validator reads the observation.
    """
    from ..probe import probe_file
    from ..resolve import Cache

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
