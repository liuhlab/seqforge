"""PROTOTYPE, throwaway: write the page a person reads the drawn studies on.

Reads a folder of study files (<ID>.yaml with <ID>.draft.yaml, <ID>.verdict.yaml and, when a person
has already answered, <ID>.answer.yaml) and writes one self-contained HTML file.

    python prototypes/standard_set_page/build_page.py FOLDER OUT.html [--notice TEXT] [--title TEXT]
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import sys
from pathlib import Path

import yaml

HERE = Path(__file__).parent
SIDE_FILES = ("draft", "verdict", "answer")


def load(path: Path) -> dict | None:
    """One YAML file, or nothing when it is not there."""
    return yaml.safe_load(path.read_text()) if path.exists() else None


def read_folder(folder: Path) -> list[dict]:
    """Every study in the folder, each with its draft, verdict and answer attached."""
    studies = []
    for path in sorted(folder.glob("*.yaml")):
        if path.stem.rsplit(".", 1)[-1] in SIDE_FILES:
            continue
        study = load(path) or {}
        study.setdefault("id", path.stem)
        for side in SIDE_FILES:
            study[side] = load(folder / f"{path.stem}.{side}.yaml")
        studies.append(study)
    return studies


def main() -> None:
    """Write the page."""
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("folder", type=Path, help="folder of study files")
    parser.add_argument("out", type=Path, help="the HTML file to write")
    parser.add_argument("--title", default="Studies to read")
    parser.add_argument(
        "--notice", default="", help="a line shown across the top, e.g. that the data is made up"
    )
    parser.add_argument(
        "--built",
        default="",
        help="override the 'page written' time (keeps an example file stable)",
    )
    args = parser.parse_args()

    data = {
        "title": args.title,
        "notice": args.notice,
        "set": args.folder.name,
        "built": args.built or dt.datetime.now(dt.UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "file_command": f"python {Path(sys.argv[0]).with_name('file_answers.py')} ANSWERS {args.folder}",
        "studies": read_folder(args.folder),
    }
    # Dates in YAML load as date objects; "</" would end the script block the data sits in.
    blob = json.dumps(data, default=lambda o: o.isoformat(), ensure_ascii=False).replace(
        "</", "<\\/"
    )
    page = (HERE / "template.html").read_text().replace("/*__DATA__*/", blob)
    args.out.write_text(page)
    print(f"wrote {args.out}: {len(data['studies'])} studies, {len(page) // 1024} KB")


if __name__ == "__main__":
    main()
