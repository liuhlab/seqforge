"""The standard set: the small GEO studies kept under ``evals/standard-set/``, and the pick of them.

The 50 studies' files were drawn once, by commands since retired; a study swapped into the set is
drawn by the bounding job, :mod:`seqforge.study.bounding`.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

#: A study's own file and its bounds file, in the study's folder.
STUDY, BOUNDS = "study.yaml", "bounds.yaml"
#: The list the studies were picked into, beside the study folders.
PICK = "pick.yaml"


def default_set_dir() -> Path:
    """``<repo>/evals/standard-set``. The set ships with the repo, so this is a relative walk-up."""
    return Path(__file__).resolve().parents[3] / "evals" / "standard-set"


def pick_entries(set_dir: Path) -> dict[str, dict[str, Any]]:
    """The picked studies by id, in the pick's order. Spares are not studies until one is swapped in."""
    pick = yaml.safe_load((set_dir / PICK).read_text())
    return {str(entry["id"]): entry for entry in pick["studies"]}
