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


def commit(clone: Path, path: Path, message: str) -> bool:
    """Commit ``path``, a file in ``clone``, alone, if it differs from the last commit.

    Anything else staged or changed in the clone stays as it was. Returns whether it committed.
    """
    if not path.is_file():
        return False
    relative = str(path.relative_to(clone))
    _checked(clone, "add", "--", relative)
    if _git(clone, "diff", "--cached", "--quiet", "--", relative).returncode == 0:
        return False
    _checked(clone, "commit", "--quiet", "--message", message, "--", relative)
    return True


def push(clone: Path) -> bool:
    """Push the clone's branch, if it has commits its upstream lacks. Returns whether it pushed.

    A push rejected because the upstream moved on pulls and pushes once more; the union merge
    joins the reviews files' lines. A pull that fails leaves no merge half done. Raises
    :class:`GitFailed` when the push still fails, with the commits kept in the clone.
    """
    ahead = _git(clone, "rev-list", "--count", "@{upstream}..HEAD")
    if ahead.returncode == 0 and ahead.stdout.strip() == "0":
        return False
    pushed = _git(clone, "push", "--quiet")
    if pushed.returncode == 0:
        return True
    pulled = _git(clone, "pull", "--quiet", "--no-rebase", "--ff", "--no-edit")
    if pulled.returncode != 0:
        _git(clone, "merge", "--abort")
        raise GitFailed(f"git push: {pushed.stderr.strip()}; git pull: {pulled.stderr.strip()}")
    _checked(clone, "push", "--quiet")
    return True
