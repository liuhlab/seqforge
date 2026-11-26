"""`seqforge standard-set`: list the standard set of studies kept under ``evals/``.

The commands that drew the 50 are retired. A study swapped into the set is drawn by the bounding job:
``seqforge pass start study-bounds``, then ``seqforge study write-bounds``.
"""

from __future__ import annotations

import json
from pathlib import Path

import typer
import yaml

from ..study.folder import BOUNDS, STUDY, default_set_dir, pick_entries
from .root import standard_set_app

_DIR = typer.Option(None, "--dir", help="Folder of study folders (default: evals/standard-set).")


@standard_set_app.command("list")
def standard_set_list(set_dir: Path | None = _DIR) -> None:
    """List the standard set: one object per study, from its ``<id>/study.yaml`` and ``bounds.yaml``.

    The studies are the pick's, in the pick's order, as the jobs read them. Each object gives the
    study's record identifiers, the reason it was picked and its bounds, as its bounds file holds
    them.
    """
    root = set_dir or default_set_dir()
    payload = []
    for key in pick_entries(root):
        path = root / key / STUDY
        study = yaml.safe_load(path.read_text(encoding="utf-8"))
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
                "bounds": yaml.safe_load(path.with_name(BOUNDS).read_text(encoding="utf-8")),
            }
        )
    typer.echo(json.dumps(payload, indent=2))
