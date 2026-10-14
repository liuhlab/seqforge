#!/usr/bin/env python3
"""Install the seqforge skills into each agent product's discovery path.

Every skill here is a thin client: the CLI is the API, and each skill action maps to a deterministic
``seqforge`` verb. So there is nothing to install but prose, and nothing to keep in sync but a path.

Skills follow the open Agent Skills standard (``SKILL.md`` + progressive disclosure), so the CONTENT
ports across Claude Code, Codex CLI, Gemini CLI and friends. Only the **discovery path** differs —
which is the entire reason this installer exists and why it is a dumb copier rather than a framework.

    python skills/install.py --list
    python skills/install.py --target claude          # -> .claude/skills/
    python skills/install.py --target agents --user   # -> ~/.agents/skills/
    python skills/install.py --target all --dry-run
    python skills/install.py --check                  # the invariants, for CI and conformance

Symlinks by default so an edit to the repo is live everywhere; ``--copy`` for environments where a
symlink will not do.

``--check`` is what the conformance rule calls, so the command that reports a broken link is also
the command that repairs it. It holds the two COMMITTED discovery paths to three invariants — a
link in both, nothing dangling, every link relative — reports every problem it finds rather than
the first, and names the fix on each one.
"""

from __future__ import annotations

import argparse
import os
import shutil
import sys
import textwrap
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


#: The targets whose symlinks are COMMITTED, and so the only ones ``--check`` may hold a clone to.
#: `.codex/` and `.gemini/` are gitignored installer output, absent from a fresh clone by design, so
#: checking them would fail every repo that has simply not run the installer.
CHECKED_TARGETS: tuple[str, ...] = ("claude", "agents")


def discover() -> list[Path]:
    """Every directory here holding a SKILL.md."""
    return sorted(p.parent for p in SKILLS_DIR.glob("*/SKILL.md"))


def install_one(
    skill: Path, dest_root: Path, *, copy: bool, dry_run: bool, relative: bool = False
) -> str:
    """Put one skill at its discovery path.

    ``relative`` is what makes a project-local install committable. `.agents/skills` is the
    vendor-neutral path, so those links are the ones that land in the repo — and an ABSOLUTE one
    names a directory that exists on exactly one machine, so every other clone and CI resolve it to
    nothing while ``git status`` stays clean, because the link itself is intact. Written relative,
    the same link is correct in every checkout.

    A ``--user`` install gets an absolute link, which is right precisely because it is never
    committed: ``~/.agents/skills`` and the repo are unrelated trees, and a path computed between
    them would break the moment either moved.
    """
    dest = dest_root / skill.name
    action = "copy" if copy else "link"
    if dry_run:
        return f"[dry-run] {action} {skill.name} -> {dest}"
    dest_root.mkdir(parents=True, exist_ok=True)
    # `is_symlink()` first and on its own: a link pointing at a directory answers `is_dir()` too, so
    # taking the `rmtree` branch would delete the skill it points AT rather than the link.
    if dest.is_symlink() or dest.is_file():
        dest.unlink()
    elif dest.is_dir():
        shutil.rmtree(dest)
    if copy:
        shutil.copytree(skill, dest)
    else:
        target = Path(os.path.relpath(skill, dest.parent)) if relative else skill
        dest.symlink_to(target, target_is_directory=True)
    return f"{action}ed {skill.name} -> {dest}"


def _problem(what: str, why: str, fix: str) -> str:
    """One problem, formatted for whoever has to act on it.

    Three lines and always the same three: WHAT is wrong, WHY it matters, and the command that
    repairs it. The what line is left unwrapped so it stays one greppable line; only the why is
    filled, because an explanation worth reading is longer than a terminal is wide.
    """
    body = textwrap.fill(why, width=94, initial_indent="    ", subsequent_indent="    ")
    return f"{what}\n{body}\n    fix: {fix}"


def check(root: Path) -> list[str]:
    """Hold the committed discovery paths to their three invariants.

    Parameters
    ----------
    root
        The project root. `.claude/skills` and `.agents/skills` are read beneath it.

    Returns
    -------
    list of str
        One entry per problem, each naming what is wrong and the command that repairs it. Empty
        means the tree is correct.
    """
    problems: list[str] = []
    skills = discover()
    names = {skill.name for skill in skills}
    for target in CHECKED_TARGETS:
        project_dir = TARGETS[target][0]
        dest_root = root / project_dir
        fix = f"python skills/install.py --target {target}"
        for skill in skills:
            dest = dest_root / skill.name
            shown = f"{project_dir}/{skill.name}"
            # `is_symlink()` first and on its own, for the same reason `install_one` does it: a link
            # to a directory answers `is_dir()` and `exists()` too, so asking those first cannot
            # tell a link from the thing it points at.
            if not dest.is_symlink():
                if dest.exists():
                    problems.append(
                        _problem(
                            f"{shown} is not a symlink",
                            "a copy stops tracking the skill the moment the skill changes",
                            fix,
                        )
                    )
                else:
                    problems.append(
                        _problem(
                            f"{shown} is missing",
                            f"skills/{skill.name}/SKILL.md is there but nothing will discover it",
                            fix,
                        )
                    )
                continue
            link = dest.readlink()
            # Absolute is checked BEFORE dangling, because an absolute link usually resolves
            # perfectly here and reports as fine; being absolute is the root cause either way.
            if link.is_absolute():
                problems.append(
                    _problem(
                        f"{shown} is an absolute symlink -> {link}",
                        "it resolves on the machine that wrote it and on no other: every clone "
                        "and CI resolve it to nothing while `git status` stays clean, because "
                        "the link itself is intact. Relink it here, where it was made",
                        fix,
                    )
                )
            elif not dest.exists():
                problems.append(
                    _problem(
                        f"{shown} dangles -> {link}",
                        "the link is committed but resolves to nothing in this tree",
                        fix,
                    )
                )
            elif dest.resolve() != skill:
                problems.append(
                    _problem(
                        f"{shown} points at {link}, not skills/{skill.name}",
                        "the name and the target disagree, so the wrong skill is discovered",
                        fix,
                    )
                )
        # A renamed or deleted skill leaves its old link behind, pointing at nothing. It matches no
        # SKILL.md, so the loop above never looks at it, and the installer will never clean it up.
        if dest_root.is_dir():
            for stray in sorted(dest_root.iterdir()):
                if stray.name in names or not stray.is_symlink() or stray.exists():
                    continue
                problems.append(
                    _problem(
                        f"{project_dir}/{stray.name} dangles -> {stray.readlink()}",
                        "it matches no skills/*/SKILL.md, so it is a leftover, not an install",
                        f"rm {project_dir}/{stray.name}",
                    )
                )
    return problems


def report_check(root: Path) -> int:
    """Print the result of :func:`check` and return the exit status."""
    problems = check(root)
    if not problems:
        where = " and ".join(TARGETS[t][0] for t in CHECKED_TARGETS)
        print(f"--check ok: {len(discover())} skill(s) linked in {where}")
        return 0
    print(f"--check found {len(problems)} problem(s):\n", file=sys.stderr)
    for problem in problems:
        print(f"  {problem}\n", file=sys.stderr)
    return 1


def main(argv: list[str] | None = None) -> int:
    """The installer's command line: list, check or install the skills, and return the exit code."""
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
    parser.add_argument(
        "--check",
        action="store_true",
        help="Verify the committed symlinks and exit. Installs nothing.",
    )
    parser.add_argument("--root", type=Path, default=Path.cwd(), help="Project root.")
    args = parser.parse_args(argv)

    # `--check` returns before anything below can write a link: the conformance rule runs it in CI,
    # and a check that repaired what it checks would always pass.
    if args.check:
        return report_check(args.root.resolve())

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
            print(
                install_one(
                    skill,
                    dest_root,
                    copy=args.copy,
                    dry_run=args.dry_run,
                    relative=not args.user,
                )
            )
    if not args.dry_run:
        print(f"\n{len(skills)} skill(s) installed for: {', '.join(targets)}")
        print(
            "Hooks are separate and are the part that ENFORCES the rules: `seqforge hook install`"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
