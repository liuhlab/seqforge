"""The standard set: the small GEO studies kept under ``evals/standard-set/``, and how they are drawn.

A rough first version. A small model drafts each study's bounds as a pass on the run engine
(:mod:`.draft`), a large model judges the draft in one headless call (:mod:`.judge`), and the judged
bounds become the study's bounds file, in the format :mod:`seqforge.study.bounds` checks.
:mod:`.material` is what both read from liulab-data's stores. The other files' format is
provisional, and the bounding stage is free to discard the drafting kind.
"""

from __future__ import annotations

from pathlib import Path

#: The four files of one study's folder.
STUDY, BOUNDS, DRAFT, VERDICT = "study.yaml", "bounds.yaml", "draft.yaml", "verdict.yaml"
#: The list the studies were picked into, beside the study folders.
PICK = "pick.yaml"


def default_set_dir() -> Path:
    """``<repo>/evals/standard-set``. The set ships with the repo, so this is a relative walk-up."""
    return Path(__file__).resolve().parents[3] / "evals" / "standard-set"
