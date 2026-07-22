"""``seqforge preflight`` — build a portable fingerprint package from a dataset's FASTQs.

A fingerprint is a head-slice of every FASTQ (real records, first N of them) plus a pin that carries
the whole-file identity the slice cannot recompute, so the whole pipeline reproduces the same manifest
— hash and all — with the originals gone. This verb is the *producer*; ``run --fingerprint`` (see
:func:`fingerprint_run_inputs`) is the *consumer*.
"""

from __future__ import annotations

import json
from pathlib import Path

import typer

from ..fingerprint.build import build_fingerprint
from ..fingerprint.load import LoadedFingerprint, load_fingerprint, probed_from_fingerprint
from ..models.observation import Observation
from ..probe import DEFAULT_MAX_BYTES, DEFAULT_MAX_READS
from .root import app


@app.command("preflight")
def preflight_cmd(
    files: list[Path] = typer.Argument(..., help="The dataset's FASTQ .gz files."),
    reads: int = typer.Option(
        DEFAULT_MAX_READS,
        "--reads",
        "-n",
        help="N: keep each FASTQ's first N records. Keep N >= the probe budget "
        f"({DEFAULT_MAX_READS}) for the package to reproduce the full manifest hash; a smaller N is a "
        "deliberately lighter fingerprint (see the size/accuracy study).",
    ),
    doc: list[Path] = typer.Option(
        [],
        "--doc",
        help="Reference document(s) — paper .pdf/.txt/.md or supplementary .xlsx — to carry "
        "(original + extracted text/images), so a fingerprint run harvests the same claims.",
    ),
    name: str | None = typer.Option(
        None,
        "--name",
        help="Human slug for the package (default: the dataset root's directory name).",
    ),
    max_bytes: int = typer.Option(
        DEFAULT_MAX_BYTES,
        "--max-bytes",
        help="Decompressed-byte safety cap honoured alongside --reads.",
    ),
    workspace: Path = typer.Option(
        Path("."), "-C", "--workspace", help="Root for seqforge/ state."
    ),
) -> None:
    """Build ``<dataset>.fingerprint.tar.gz``: sliced FASTQs + carried prose + a pin that reproduces
    the full dataset identity. Emits a JSON summary (package path, size, per-file pins) on stdout.

    Reads no whole FASTQ: every touch is bounded by ``--reads`` and ``--max-bytes``. Deterministic —
    the same inputs at the same N produce a byte-identical package under ``seqforge/fingerprint/``.
    """
    try:
        result = build_fingerprint(
            files, workspace=workspace, reads=reads, max_bytes=max_bytes, info_docs=doc, name=name
        )
    except (OSError, ValueError) as exc:
        typer.echo(
            json.dumps({"error": "preflight_failed", "detail": str(exc)}, indent=2), err=True
        )
        raise typer.Exit(1) from exc
    payload = {
        "package": str(result.package),
        "staging": str(result.staging),
        "package_bytes": result.package_bytes,
        "reads": reads,
        "n_files": len(result.manifest.files),
        "total_reads_written": result.total_reads_written,
        "info": result.manifest.info,
        "files": [
            {
                "rel_path": p.rel_path,
                "basename": p.basename,
                "sha256": p.sha256,
                "size_bytes": p.size_bytes,
                "reads_written": p.reads_written,
                "estimated_total_reads": p.estimated_total_reads,
            }
            for p in result.manifest.files
        ],
    }
    typer.echo(json.dumps(payload, indent=2))


def fingerprint_run_inputs(
    package: Path, *, max_reads: int = DEFAULT_MAX_READS, max_bytes: int = DEFAULT_MAX_BYTES
) -> tuple[LoadedFingerprint, list[Path], dict[str, tuple[Observation, list[str]]], list[Path]]:
    """Load a fingerprint package for a run: ``(loaded, slice paths, probed map, carried docs)``.

    The probe map is keyed by slice path with the pinned identity stamped in, ready to hand to
    ``resolve_runs``/``_fill_manifest_pipeline`` as ``_probed``; the carried docs feed ``harvest`` so a
    fingerprint run reproduces the same assertions.
    """
    loaded = load_fingerprint(package)
    paths, probed = probed_from_fingerprint(loaded, max_reads=max_reads, max_bytes=max_bytes)
    return loaded, paths, probed, loaded.info_paths()
