"""PROTOTYPE, throwaway: file a reader's exported answers beside their studies.

Takes the one JSON file the reading page exports and writes <ID>.answer.yaml for each answer in it.
An answer that is already filed is never overwritten: the same answer is skipped, a different one is
reported and left alone, and the command exits 1.

    python prototypes/standard_set_page/file_answers.py ANSWERS.json FOLDER [--dry-run]
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import yaml

FORMAT = "seqforge-standard-set-answers/1"
MARKS = ("accept", "fix", "drop")


def answer_file(answer: dict, export: dict) -> dict:
    """The fields of one <ID>.answer.yaml, in reading order."""
    out = {"mark": answer["mark"], "note": answer.get("note", "")}
    if answer["mark"] == "fix":
        out["bounds"] = answer.get("bounds")
    out["who"] = answer.get("who") or export.get("who", "")
    out["when"] = answer.get("when")
    out["read_against"] = answer.get("read_against")
    out["page_built"] = export.get("page_built")
    return out


def main() -> int:
    """File each answer; 1 when any was refused, 2 when the file is not an export."""
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("answers", type=Path, help="the JSON file exported from the page")
    parser.add_argument("folder", type=Path, help="folder of study files")
    parser.add_argument(
        "--dry-run", action="store_true", help="say what would be written, write nothing"
    )
    args = parser.parse_args()

    export = json.loads(args.answers.read_text())
    if export.get("format") != FORMAT:
        print(
            f"{args.answers}: not an answers file from the reading page (format {export.get('format')!r})"
        )
        return 2

    filed, same, refused = [], [], []
    for answer in export.get("answers", []):
        sid, target = answer.get("id"), args.folder / f"{answer.get('id')}.answer.yaml"
        if answer.get("mark") not in MARKS:
            refused.append(f"{sid}: mark {answer.get('mark')!r} is not accept, fix or drop")
        elif not (args.folder / f"{sid}.yaml").exists():
            refused.append(f"{sid}: no such study in {args.folder}")
        elif target.exists():
            new = answer_file(answer, export)
            if yaml.safe_load(target.read_text()) == new:
                same.append(sid)
            else:
                refused.append(
                    f"{sid}: a different answer is already filed in {target.name}; left as it is"
                )
        else:
            if not args.dry_run:
                target.write_text(
                    yaml.safe_dump(answer_file(answer, export), sort_keys=False, allow_unicode=True)
                )
            filed.append(sid)

    verb = "would file" if args.dry_run else "filed"
    print(f"{verb} {len(filed)}: {', '.join(filed) or '-'}")
    if same:
        print(f"already filed, unchanged {len(same)}: {', '.join(same)}")
    for line in refused:
        print(f"not filed  {line}")
    return 1 if refused else 0


if __name__ == "__main__":
    sys.exit(main())
