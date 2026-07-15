#!/usr/bin/env python3
"""Install the seqforge skills into each agent product's discovery path (brief §10).

Skills follow the open Agent Skills standard (``SKILL.md`` + progressive disclosure), so the CONTENT
ports across Claude Code, Codex CLI, Gemini CLI and friends. Only the **discovery path** differs —
which is the entire reason this installer exists and why it is a dumb copier rather than a framework.

    python skills/install.py --list
    python skills/install.py --target claude          # -> .claude/skills/
    python skills/install.py --target agents --user   # -> ~/.agents/skills/
    python skills/install.py --target all --dry-run

Symlinks by default so an edit to the repo is live everywhere; ``--copy`` for environments where a
symlink will not do.
"""

from __future__ import annotations

import argparse
import shutil
import sys
from pathlib import Path

SKILLS_DIR = Path(__file__).resolve().parent

#: product -> (project-local dir, user-global dir). Kept as data because these paths are the ONLY
#: thing that varies between products; if one moves, this table is the single place to fix it.
TARGETS: dict[str, tuple[str, str]] = {
    "claude": (".claude/skills", ".claude/skills"),
    "agents": (".agents/skills", ".agents/skills"),
    "codex": (".codex/skills", ".codex/skills"),
    "gemini": (".gemini/skills", ".gemini/skills"),
}


def discover() -> list[Path]:
    """Every directory here holding a SKILL.md."""
    return sorted(p.parent for p in SKILLS_DIR.glob("*/SKILL.md"))


def install_one(skill: Path, dest_root: Path, *, copy: bool, dry_run: bool) -> str:
    dest = dest_root / skill.name
    action = "copy" if copy else "link"
    if dry_run:
        return f"[dry-run] {action} {skill.name} -> {dest}"
    dest_root.mkdir(parents=True, exist_ok=True)
    if dest.is_symlink() or dest.is_file():
        dest.unlink()
    elif dest.is_dir():
        shutil.rmtree(dest)
    if copy:
        shutil.copytree(skill, dest)
    else:
        dest.symlink_to(skill, target_is_directory=True)
    return f"{action}ed {skill.name} -> {dest}"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--target",
        default="claude",
        choices=[*TARGETS, "all"],
        help="Agent product to install for.",
    )
    parser.add_argument("--user", action="store_true", help="Install to $HOME, not the project.")
    parser.add_argument("--copy", action="store_true", help="Copy instead of symlinking.")
    parser.add_argument("--dry-run", action="store_true", help="Print what would happen.")
    parser.add_argument("--list", action="store_true", help="List the skills and exit.")
    parser.add_argument("--root", type=Path, default=Path.cwd(), help="Project root.")
    args = parser.parse_args(argv)

    skills = discover()
    if not skills:
        print("no skills found (expected skills/*/SKILL.md)", file=sys.stderr)
        return 1
    if args.list:
        for skill in skills:
            print(skill.name)
        return 0

    targets = list(TARGETS) if args.target == "all" else [args.target]
    base = Path.home() if args.user else args.root.resolve()
    for target in targets:
        project_dir, user_dir = TARGETS[target]
        dest_root = base / (user_dir if args.user else project_dir)
        for skill in skills:
            print(install_one(skill, dest_root, copy=args.copy, dry_run=args.dry_run))
    if not args.dry_run:
        print(f"\n{len(skills)} skill(s) installed for: {', '.join(targets)}")
        print(
            "Hooks are separate and are the part that ENFORCES the rules: `seqforge hook install`"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
