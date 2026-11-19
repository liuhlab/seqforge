"""The one small client the review commands reach GitHub through, and the one place they build it.

It drives the GitHub CLI, ``gh``, as the person running the command: their ``gh auth login`` is
its whole authentication, so seqforge holds no token, adds no dependency, and needs only the plain
``repo`` scope. It needs gh 2.48 or later, the first with ``gh api --slurp``. The repo is the one the review clone's ``origin`` remote names. Tests replace
:func:`make_github` with an in-memory stand-in, so the commands must call it through this module
at the moment they need it, never bind it at import.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol
from urllib.parse import quote

from . import SetupRefused


@dataclass(frozen=True)
class Issue:
    """One issue in the review repo, as the review commands read it.

    ``state`` is ``open`` or ``closed``; ``state_reason`` is how it was closed (``completed`` or
    ``not_planned``), ``reopened``, or None.
    """

    number: int
    url: str
    title: str
    body: str
    labels: tuple[str, ...]
    state: str
    state_reason: str | None


class GitHubError(Exception):
    """A call to GitHub failed after the client was made. The message is GitHub's, through ``gh``."""


class GitHub(Protocol):
    """What the review commands ask of the review repo."""

    def labels(self) -> set[str]:
        """Every label's name."""
        ...

    def create_label(self, name: str, *, color: str, description: str) -> None:
        """Make a label the repo does not have yet."""
        ...

    def issues(self, label: str) -> list[Issue]:
        """Every issue carrying ``label``, open and closed."""
        ...

    def create_issue(self, *, title: str, body: str, labels: Sequence[str]) -> Issue:
        """File an issue with ``labels``, each of which the repo already has."""
        ...

    def closer(self, number: int) -> str:
        """The GitHub login of whoever last closed issue ``number``."""
        ...

    def comment(self, number: int, body: str) -> None:
        """Add a comment to issue ``number``."""
        ...

    def reopen(self, number: int) -> None:
        """Reopen issue ``number``."""
        ...

    def add_labels(self, number: int, labels: Sequence[str]) -> None:
        """Add ``labels``, each of which the repo already has, to issue ``number``."""
        ...


def make_github(clone: Path) -> GitHub:
    """A client of the GitHub repo that ``clone``'s ``origin`` remote names, as the person running.

    Raises :class:`SetupRefused` when ``gh`` is not installed or ``clone`` is not a clone of a
    GitHub repo. Makes no network contact: a person not logged in to ``gh`` meets
    :class:`GitHubError` on the first call.
    """
    if shutil.which("gh") is None:
        raise SetupRefused(
            "the review commands reach GitHub through the GitHub CLI: install gh, "
            "then run gh auth login"
        )
    return GhClient(_repo_of(clone))


#: An ``origin`` URL on github.com, over https or ssh, and the ``owner/name`` it names.
_GITHUB_REMOTE = re.compile(r"github\.com[:/](?P<repo>[^/\s:]+/[^/\s]+?)(?:\.git)?/?$")


def _repo_of(clone: Path) -> str:
    """``owner/name`` of the GitHub repo ``clone`` was cloned from.

    The remote's URL is never echoed, since an https remote can carry a token.
    """
    try:
        done = subprocess.run(
            ["git", "-C", str(clone), "remote", "get-url", "origin"],
            capture_output=True,
            text=True,
            check=False,
        )
    except FileNotFoundError:
        raise SetupRefused("the review commands need git") from None
    found = _GITHUB_REMOTE.search(done.stdout.strip()) if done.returncode == 0 else None
    if found is None:
        raise SetupRefused(
            f"{clone} has no origin remote on github.com, so it is not a clone of the review repo"
        )
    return found["repo"]


@dataclass(frozen=True)
class GhClient:
    """:class:`GitHub` over ``gh api``, against the repo ``owner/name``."""

    repo: str

    def labels(self) -> set[str]:
        """Every label's name."""
        return {label["name"] for label in self._pages(f"repos/{self.repo}/labels?per_page=100")}

    def create_label(self, name: str, *, color: str, description: str) -> None:
        """Make a label the repo does not have yet."""
        body = {"name": name, "color": color, "description": description}
        self._call(f"repos/{self.repo}/labels", "--method", "POST", "--input", "-", sent=body)

    def issues(self, label: str) -> list[Issue]:
        """Every issue carrying ``label``, open and closed."""
        path = f"repos/{self.repo}/issues?state=all&per_page=100&labels={quote(label, safe='')}"
        # The issues endpoint lists pull requests too; the review repo should have none.
        return [_issue(raw) for raw in self._pages(path) if "pull_request" not in raw]

    def create_issue(self, *, title: str, body: str, labels: Sequence[str]) -> Issue:
        """File an issue with ``labels``, each of which the repo already has."""
        sent = {"title": title, "body": body, "labels": list(labels)}
        out = self._call(f"repos/{self.repo}/issues", "--method", "POST", "--input", "-", sent=sent)
        return _issue(json.loads(out))

    def closer(self, number: int) -> str:
        """The GitHub login of whoever last closed issue ``number``.

        A listing of issues leaves out who closed each, so this reads the one issue. An account
        since deleted is GitHub's ``ghost``.
        """
        raw = json.loads(self._call(f"repos/{self.repo}/issues/{number}"))
        closed_by = raw.get("closed_by") or {"login": "ghost"}
        return str(closed_by["login"])

    def comment(self, number: int, body: str) -> None:
        """Add a comment to issue ``number``."""
        path = f"repos/{self.repo}/issues/{number}/comments"
        self._call(path, "--method", "POST", "--input", "-", sent={"body": body})

    def reopen(self, number: int) -> None:
        """Reopen issue ``number``."""
        path = f"repos/{self.repo}/issues/{number}"
        self._call(path, "--method", "PATCH", "--input", "-", sent={"state": "open"})

    def add_labels(self, number: int, labels: Sequence[str]) -> None:
        """Add ``labels``, each of which the repo already has, to issue ``number``."""
        path = f"repos/{self.repo}/issues/{number}/labels"
        self._call(path, "--method", "POST", "--input", "-", sent={"labels": list(labels)})

    def _pages(self, path: str) -> list[dict[str, Any]]:
        """Every item of a listing, every page: ``--slurp`` gives one array of the pages' arrays."""
        pages = json.loads(self._call(path, "--paginate", "--slurp"))
        return [item for page in pages for item in page]

    def _call(self, path: str, *flags: str, sent: dict[str, Any] | None = None) -> str:
        done = subprocess.run(
            ["gh", "api", *flags, path],
            input=None if sent is None else json.dumps(sent),
            capture_output=True,
            text=True,
            check=False,
        )
        if done.returncode != 0:
            raise GitHubError(done.stderr.strip() or f"gh api {path} exited {done.returncode}")
        return done.stdout


def _issue(raw: dict[str, Any]) -> Issue:
    return Issue(
        number=raw["number"],
        url=raw["html_url"],
        title=raw["title"],
        body=raw.get("body") or "",
        labels=tuple(label["name"] for label in raw.get("labels", ())),
        state=raw["state"],
        state_reason=raw.get("state_reason"),
    )
