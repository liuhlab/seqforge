"""`seqforge study`: the format checks of a study's files.

``check-bounds`` says whether one bounds file is in the current format, and what is wrong with it
when it is not. It reads the file and nothing else: no store, no network, no model.
"""

from __future__ import annotations

from pathlib import Path

import typer
import yaml

from ..study.bounds import FORMAT, check_bounds
from ._common import _emit, _StageOut
from .root import study_app


@study_app.command("check-bounds")
def study_check_bounds(
    file: Path = typer.Argument(..., help="A study's bounds file, such as <ID>/bounds.yaml."),
) -> None:
    """Check one bounds file against the current format.

    Prints one JSON object: the file, the format number the check accepts, whether the file is
    valid, and its problems, each naming a field and the reason. Exit 0 for a valid file; 3 for a
    rejected one; 2 for a file that cannot be read.
    """
    try:
        text = file.read_text()
    except OSError as exc:
        _emit(_StageOut(f"cannot read {file}: {exc}", 2, err=True))
        return
    try:
        problems = check_bounds(yaml.safe_load(text))
    except yaml.YAMLError as exc:
        problems = [{"field": "(file)", "reason": f"not YAML: {' '.join(str(exc).split())}"}]
    payload = {"file": str(file), "format": FORMAT, "valid": not problems, "problems": problems}
    _emit(_StageOut(payload, 3 if problems else 0))
