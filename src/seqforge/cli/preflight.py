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

from ..fingerprint.build import build_fingerprint, strip_to_redistributable
from ..fingerprint.load import LoadedFingerprint, load_fingerprint, probed_from_fingerprint
from ..models.observation import Observation
from ..probe import DEFAULT_MAX_BYTES, DEFAULT_MAX_READS
from .root import app


@app.command("preflight")
def preflight_cmd(
    files: list[Path] = typer.Argument(
        None, help="The dataset's local FASTQ .gz files (omit when using --accession)."
    ),
    accession: str | None = typer.Option(
        None,
        "--accession",
        help="Build the package from an SRA run/experiment (SRR/SRX) by STREAMING the first --reads "
        "spots straight from the .sra — no FASTQ downloaded. A project/series that mixes experiments "
        "(e.g. a GSE with bulk + multiome) is refused with the list of SRX to pick from. Mutually "
        "exclusive with the FASTQ arguments.",
    ),
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
    include_raw: bool = typer.Option(
        True,
        "--include-raw/--redistributable",
        "--raw-docs/--no-raw-docs",
        help="Whether to carry the raw documents. Default (--include-raw) builds a LOCAL package "
        "with the original paper + extracted figures alongside the text. --redistributable "
        "(alias --no-raw-docs) carries only the extracted text under info/text/ — the raw paper is "
        "not redistributed (copyright) and figures are dropped until the figure pipeline improves. A "
        "run falls back to the text, so a redistributable package stays fully usable.",
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

    Two sources, one package format. Local ``FILES`` slice each FASTQ's first N records; ``--accession``
    STREAMS the first N spots of an SRA run/experiment with no download (the archive twin). Either way,
    no whole FASTQ is read — every touch is bounded by ``--reads`` and ``--max-bytes`` — and the same
    inputs at the same N produce a byte-identical package under ``seqforge/fingerprint/``.
    """
    if bool(files) == bool(accession):
        typer.echo(
            json.dumps(
                {
                    "error": "preflight_usage",
                    "detail": "give EITHER local FASTQ files OR --accession, not both and not neither.",
                },
                indent=2,
            ),
            err=True,
        )
        raise typer.Exit(2)

    try:
        if accession:
            from ..io.remote import RemoteError
            from ..io.sra import build_fingerprint_sra, resolve_single_experiment_runs

            try:
                _srx, runs = resolve_single_experiment_runs(accession)
                result = build_fingerprint_sra(
                    runs,
                    workspace=workspace,
                    reads=reads,
                    max_bytes=max_bytes,
                    info_docs=doc,
                    name=name,
                    include_raw=include_raw,
                )
            except RemoteError as exc:
                typer.echo(
                    json.dumps({"error": "preflight_failed", "detail": str(exc)}, indent=2),
                    err=True,
                )
                raise typer.Exit(1) from exc
        else:
            result = build_fingerprint(
                files,
                workspace=workspace,
                reads=reads,
                max_bytes=max_bytes,
                info_docs=doc,
                name=name,
                include_raw=include_raw,
            )
    except (OSError, ValueError) as exc:
        typer.echo(
            json.dumps({"error": "preflight_failed", "detail": str(exc)}, indent=2), err=True
        )
        raise typer.Exit(1) from exc
    payload = {
        "source": "sra-stream" if accession else "local-files",
        "accession": accession,
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


@app.command("strip-fingerprint")
def strip_fingerprint_cmd(
    package: Path = typer.Argument(..., help="An existing .fingerprint.tar.gz (or unpacked dir)."),
    out: Path = typer.Option(
        ..., "--out", "-o", help="Destination path for the redistributable (text-only) .tar.gz."
    ),
) -> None:
    """Repack a fingerprint package as a **redistributable** copy: text only, no raw doc, no figures.

    The retroactive twin of ``preflight --redistributable`` — for packages built before that flag (or
    once the original FASTQs are gone). Drops ``info/docs/`` (the raw paper, a copyright liability) and
    ``info/images/`` (its figures), keeps ``info/text/`` and every FASTQ slice + pin untouched, so the
    dataset hash reproduces byte-for-byte. Emits a JSON summary on stdout.
    """
    try:
        result = strip_to_redistributable(package, out)
    except (OSError, ValueError) as exc:
        typer.echo(json.dumps({"error": "strip_failed", "detail": str(exc)}, indent=2), err=True)
        raise typer.Exit(1) from exc
    typer.echo(
        json.dumps(
            {
                "package": str(result.package),
                "package_bytes": result.package_bytes,
                "n_files": len(result.manifest.files),
                "info": result.manifest.info,
            },
            indent=2,
        )
    )


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
