"""Tests for the docs config — do the two exclusion lists stay the SAME list?

`mkdocs.yml`'s `exclude_docs` and `.markdownlint-cli2.yaml`'s `ignores` answer the same question about
`docs/`: which trees under it are agent-facing rather than site prose. `design.md`, `agents/` and
`adr/` are excluded from the built site for the reason `design.md`'s own comment gives -- agent-facing
material must not read as settled guidance under a docs URL -- and for exactly that reason they are
not linted as site pages either.

They drifted once, and the failure was not theoretical: `agents/` was added to `exclude_docs` and not
to `ignores`, so `docs/agents/domain.md` was linted as a site page, failed MD040 and MD049, and turned
the `markdownlint` job red on every open PR. A comment saying "keep these in sync" is not a mechanism;
this is. The check is one-directional on purpose: everything mkdocs hides from the site must be
ignored by markdownlint, but `ignores` legitimately holds more (the KB wrapper pages and a symlinked
README are published, and skipped for their own reasons).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

_REPO = Path(__file__).resolve().parents[1]
MKDOCS = _REPO / "mkdocs.yml"
MARKDOWNLINT = _REPO / ".markdownlint-cli2.yaml"


class _IgnoreTags(yaml.SafeLoader):
    """`SafeLoader` that tolerates mkdocs-material's `!!python/name:` tags instead of raising.

    `mkdocs.yml:106` carries `format: !!python/name:pymdownx.superfences.fence_code_format`, which
    `safe_load` refuses by design. We only ever read `exclude_docs`, so resolving those tags to `None`
    is enough -- and far less brittle than regexing a YAML block out of the file by hand.
    """


def _ignore_unknown(loader: yaml.SafeLoader, suffix: str, node: yaml.Node) -> None:
    return None


_IgnoreTags.add_multi_constructor("tag:yaml.org,2002:python/name:", _ignore_unknown)


def _load(path: Path) -> dict[str, Any]:
    with path.open() as fh:
        loaded = yaml.load(fh, Loader=_IgnoreTags)
    assert isinstance(loaded, dict), f"{path.name} did not parse to a mapping"
    return loaded


def _markdownlint_ignore_for(excluded: str) -> str:
    """The `ignores` entry that covers one `exclude_docs` entry.

    `exclude_docs` is gitignore-shaped and relative to `docs_dir`; `ignores` holds globs relative to
    the repo root. A trailing slash marks a directory, which needs a recursive glob to cover the files
    inside it -- `docs/agents/` alone matches the directory, not `domain.md`.
    """
    return f"docs/{excluded}**" if excluded.endswith("/") else f"docs/{excluded}"


def test_everything_excluded_from_the_site_is_also_unlinted() -> None:
    """Every tree mkdocs hides from the site must be one markdownlint does not lint as site prose."""
    excluded = [line.strip() for line in _load(MKDOCS)["exclude_docs"].splitlines() if line.strip()]
    assert excluded, "mkdocs.yml has no exclude_docs entries -- has the key moved?"

    ignores = set(_load(MARKDOWNLINT)["ignores"])
    missing = [e for e in excluded if _markdownlint_ignore_for(e) not in ignores]

    assert not missing, (
        "excluded from the site but still linted as a site page:\n"
        + "\n".join(
            f"  mkdocs.yml excludes {e!r} -> add {_markdownlint_ignore_for(e)!r}" for e in missing
        )
        + "\nto `ignores:` in .markdownlint-cli2.yaml. The two lists are the same list."
    )
