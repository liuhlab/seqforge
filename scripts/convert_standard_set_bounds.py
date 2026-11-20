#!/usr/bin/env python
"""Move each standard-set study's bounds out of ``study.yaml`` into ``bounds.yaml``, once.

    python scripts/convert_standard_set_bounds.py [--dir evals/standard-set]

A one-off for the change that gave the bounds a file and a format of their own. For every study
whose ``study.yaml`` still holds a ``bounds`` block, it writes ``<ID>/bounds.yaml`` in the current
format and then rewrites ``study.yaml`` without the block: the one time a study file is written
twice. Each claim comes out marked inferred, with the reason the drawing model wrote as how, and no
probability. The file names the model whose bounds became the study's: the judging model where
``bounds_from`` says so, else the drafting model. A sample split is dropped: a study is whole
Series.

Running it again changes nothing. A study that is already converted is counted and left alone, and
one cut off between its two writes is finished. It prints one JSON object of counts, and exits 1 if
a bounds file would not pass its format check, having written nothing for that study.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import yaml

from seqforge.standard_set import BOUNDS, DRAFT, STUDY, VERDICT, default_set_dir
from seqforge.standard_set.draft import study_bounds
from seqforge.study.bounds import check_bounds


def convert(set_dir: Path) -> dict[str, list[str]]:
    """Convert every study under ``set_dir``, and say what happened to each, by id."""
    done: dict[str, list[str]] = {"converted": [], "already_converted": [], "rejected": []}
    for path in sorted(set_dir.glob(f"*/{STUDY}")):
        study = yaml.safe_load(path.read_text())
        key = str(study["id"])
        if "bounds" not in study:
            done["already_converted"].append(key)
            continue
        drawn_by = VERDICT if study["bounds_from"] == "judge" else DRAFT
        model = yaml.safe_load(path.with_name(drawn_by).read_text())["model"]
        bounds = study_bounds(key, study.pop("bounds"), str(model))
        if check_bounds(bounds):
            done["rejected"].append(key)
            continue
        # The bounds file first, so a run cut off here leaves the bounds in both places, not none.
        for target, body in ((path.with_name(BOUNDS), bounds), (path, study)):
            target.write_text(yaml.safe_dump(body, sort_keys=False, width=100))
        done["converted"].append(key)
    return done


def main() -> int:
    """Run the conversion and print its counts."""
    parser = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    parser.add_argument("--dir", type=Path, default=default_set_dir(), help="Folder of studies.")
    done = convert(parser.parse_args().dir)
    print(
        json.dumps(
            {**{name: len(ids) for name, ids in done.items()}, "rejected_ids": done["rejected"]}
        )
    )
    return 1 if done["rejected"] else 0


if __name__ == "__main__":
    sys.exit(main())
