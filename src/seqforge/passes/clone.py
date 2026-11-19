"""The review clone's git: committing the reviews file a command changed, and pushing it.

The reviews files are shared data. Each person's clone adds lines to them, and the review repo sets
git's union merge for them, so a merge keeps both clones' lines. A command commits only the reviews
file it changed, never anything else in the clone, and pushes whatever its upstream lacks, so a
rerun pushes what an earlier push left behind.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path


class GitFailed(Exception):
    """A git step failed. The message is git's own."""


def _git(clone: Path, *argv: str) -> subprocess.CompletedProcess[str]:
    # A push or pull must fail rather than wait for a password nobody is there to type.
    env = {**os.environ, "GIT_TERMINAL_PROMPT": "0"}
    return subprocess.run(
        ["git", "-C", str(clone), *argv], capture_output=True, text=True, check=False, env=env
    )


def _checked(clone: Path, *argv: str) -> str:
    done = _git(clone, *argv)
    if done.returncode != 0:
        raise GitFailed(f"git {argv[0]}: {done.stderr.strip() or f'exited {done.returncode}'}")
    return done.stdout


def share(clone: Path, path: Path, message: str) -> bool:
    """Commit ``path``, a file in ``clone``, alone if it changed, then push what upstream lacks.

    Anything else staged or changed in the clone stays as it was, and a file that is gone is never
    committed as a deletion. Returns whether it pushed: nothing to push is not a failure.

    A push rejected because the upstream moved on pulls and pushes once more; the union merge
    joins the reviews files' lines. A pull that fails leaves no merge half done. Raises
    :class:`GitFailed` when a step fails, with whatever was committed kept in the clone.
    """
    relative = str(path.relative_to(clone))
    # One status read says both whether the file changed and how far the branch is ahead.
    status = _checked(clone, "status", "--porcelain=v2", "--branch", "--", relative).splitlines()
    ahead = next((int(line.split()[2]) for line in status if line.startswith("# branch.ab ")), None)
    changed = [line for line in status if not line.startswith("#")]
    if changed and path.is_file():
        if changed[0].startswith("?"):  # a kind's first reviews: git does not know the file yet
            _checked(clone, "add", "--", relative)
        _checked(clone, "commit", "--quiet", "--message", message, "--", relative)
        ahead = None if ahead is None else ahead + 1
    if ahead == 0:
        return False
    # No upstream to count against: the push itself says what is wrong.
    pushed = _git(clone, "push", "--quiet")
    if pushed.returncode == 0:
        return True
    pulled = _git(clone, "pull", "--quiet", "--no-rebase", "--ff", "--no-edit")
    if pulled.returncode != 0:
        _git(clone, "merge", "--abort")
        raise GitFailed(f"git push: {pushed.stderr.strip()}; git pull: {pulled.stderr.strip()}")
    _checked(clone, "push", "--quiet")
    return True
