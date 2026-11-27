"""`seqforge project` -- project-level views over a multi-assay compile (sample_metadata + index)."""

from __future__ import annotations

import json
from pathlib import Path

import typer

from ._common import _load_manifest, _open_study
from .root import project_app

#: `--study`, as `project metadata` and `run` both take it.
STUDY_HELP = (
    "A study folder whose reading to join into sample_metadata.tsv by BioSample accession: one "
    "column per factor and per label that varies, and reading_hash. Mismatches warn and exit 0; "
    "the manifest is untouched. Nothing is joined without it."
)


@project_app.command("metadata")
def project_metadata(
    workspace: Path = typer.Option(Path(), "-C", "--workspace", help="Root holding seqforge/."),
    study: Path | None = typer.Option(None, "--study", help=STUDY_HELP),
) -> None:
    """Regenerate seqforge/sample_metadata.tsv + project.yaml from the per-assay manifest(s).

    Reads whatever manifests are already under seqforge/ — a single top-level manifest.yaml, or one
    per assay subdir — and unions their samples into the flat one-row-per-sample table + the assay
    index. Deterministic: same manifests in, same files out. `run` writes these automatically; this
    verb rebuilds them (after editing a manifest, or naming a newer reading, say) without recompiling.
    """
    from ..project import discover_assays, write_project_views

    folder = _open_study(study)
    assays = discover_assays(workspace)
    if not assays:
        typer.echo(json.dumps({"error": "no manifest.yaml found under seqforge/"}), err=True)
        raise typer.Exit(3)
    infos: list[dict[str, object]] = []
    for subdir, manifest_path in assays:
        manifest = _load_manifest(manifest_path)
        infos.append(
            {
                "chemistry": manifest.library.chemistry.value[0],
                "subdir": subdir,
                "n_samples": len(manifest.experiment.samples),
                "manifest": str(manifest_path),
            }
        )
    tsv_path, project_path, joined = write_project_views(workspace, infos, study=folder)
    said: dict[str, object] = {
        "sample_metadata": str(tsv_path),
        "project": str(project_path),
        "n_assays": len(infos),
    }
    if joined is not None:
        said["study"] = joined
    typer.echo(json.dumps(said, indent=2))
