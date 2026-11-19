"""`seqforge standard-set` -- PROTOTYPE: list the standard set of studies kept under ``evals/``.

Throwaway. It reads the study files as plain YAML with no model and no checks, to show what the
listing looks like. The file format is provisional.
"""

from __future__ import annotations

import json
from pathlib import Path

import typer
import yaml

from .root import standard_set_app


def default_standard_set_dir() -> Path:
    """``<repo>/evals/standard-set``. The set ships with the repo, so this is a relative walk-up."""
    return Path(__file__).resolve().parents[3] / "evals" / "standard-set"


@standard_set_app.command("list")
def standard_set_list(
    set_dir: Path | None = typer.Option(
        None, "--dir", help="Folder of study folders (default: evals/standard-set)."
    ),
) -> None:
    """List the standard set: one object per study, from its ``<id>/study.yaml``."""
    root = set_dir or default_standard_set_dir()
    payload = []
    for path in sorted(root.glob("*/study.yaml")):
        study = yaml.safe_load(path.read_text())
        payload.append(
            {
                "id": study["id"],
                "summary": study["summary"],
                "direction": study["direction"],
                "hard_shape": study["hard_shape"],
                "status": study["status"],
                "reason_picked": " ".join(study["reason_picked"].split()),
                "bounds": {"series": study["bounds"]["series"]},
            }
        )
    typer.echo(json.dumps(payload, indent=2))
