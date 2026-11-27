"""`seqforge standard-set`: list the standard set of studies kept under ``evals/``.

The commands that drew the 50 are retired. A study swapped into the set is drawn by the bounding job:
``seqforge pass start study-bounds``, then ``seqforge study write-bounds``.
"""

from __future__ import annotations

import json
from pathlib import Path

import typer

from ..study.folder import StudyFolderRefused, default_set_dir, open_study_folder, pick_entries
from .root import standard_set_app

_DIR = typer.Option(None, "--dir", help="Folder of study folders (default: evals/standard-set).")


@standard_set_app.command("list")
def standard_set_list(set_dir: Path | None = _DIR) -> None:
    """List the standard set: one object per study, from its study file and its bounds file.

    The studies are the pick's, in the pick's order, as the jobs read them. Each object gives the
    study's record identifiers, the reason it was picked and its bounds, as its bounds file holds
    them. Exit 2 when a study's folder fails its format checks, naming each problem.
    """
    root = set_dir or default_set_dir()
    payload = []
    for key in pick_entries(root):
        try:
            opened = open_study_folder(root / key)
        except StudyFolderRefused as exc:
            typer.echo(str(exc), err=True)
            raise typer.Exit(2) from exc
        study = opened.study or {}
        payload.append(
            {
                "id": study["id"],
                "summary": study["summary"],
                "direction": study["direction"],
                "hard_shape": study["hard_shape"],
                "status": study["status"],
                "bounds_from": study["bounds_from"],
                "reason_picked": " ".join(study["reason_picked"].split()),
                "records": study["records"],
                "bounds": opened.bounds.model_dump(mode="json", exclude_unset=True),
            }
        )
    typer.echo(json.dumps(payload, indent=2))
