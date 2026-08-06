"""Tests for the docs config and the agent-facing tree: do the documents agree with each other?

Two independent claims, both about files no other test reads.

**The two exclusion lists are the same list.** `mkdocs.yml`'s `exclude_docs` and
`.markdownlint-cli2.yaml`'s `ignores` answer the same question about `docs/`: which trees under it are
agent-facing rather than site prose. They drifted once, and the failure was not theoretical: a tree
was added to `exclude_docs` and not to `ignores`, so a page of it was linted as a site page, failed
MD040 and MD049, and turned the `markdownlint` job red on every open PR. A comment saying "keep these
in sync" is not a mechanism; this is. The check is one-directional on purpose: everything mkdocs hides
from the site must be ignored by markdownlint, but `ignores` legitimately holds more (the KB wrapper
pages and a symlinked README are published, and skipped for their own reasons).

**The run id is stated precisely once.** The formula that identifies a compiled run is decided in one
record and glossed everywhere else, because a gloss cannot drift out of agreement with a decision it
does not restate. It was written in two notations at once, in two documents that could not see each
other -- which is the same failure as a stale test name, in prose that no rename would ever disturb.
The record is found by *filename* rather than by directory, because records sit beside the code they
govern and only the four-digit name is fixed.

What used to live here -- the enforcement map's shape checks, the ADR index's two set comparisons, the
reference tree's `**Covers.**` trio -- went with their subjects. A table of test names is found by
grep and rots otherwise, and an index of one-paragraph records is `ls`.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import pytest
import yaml

#: Everything here checks the published pages, not `src/`. `pixi run check` runs it; `test-fast` does not.
pytestmark = pytest.mark.repo

_REPO = Path(__file__).resolve().parents[1]
MKDOCS = _REPO / "mkdocs.yml"
MARKDOWNLINT = _REPO / ".markdownlint-cli2.yaml"

#: The run id formula's operator (U+2295), and the two component names only its PRECISE spelling uses
#: -- assembled at run time so this file holds no second statement of the formula it guards, the same
#: trick `tests/test_repo_invariants.py` uses on the rule numbers.
#:
#: `dataset_hash` and `kb_version` are deliberately absent. Both are ordinary prose elsewhere, and
#: `kb_version` is a component of a *different* content address (the per-dataset candidates cache), so
#: a blunter set would red-light a formula this record does not own.
_HASH_JOIN = "⊕"
_PRECISE_RUN_ID = tuple(
    f"{part}_{kind}" for part, kind in (("processing", "hash"), ("workflow", "version"))
)
#: The record that owns the formula, by name alone. Filenames stay globally unique precisely so a
#: record can be found wherever it sits -- `find . -name '0005-*.md'` is the documented lookup.
_RUN_ID_RECORD = re.compile(r"^0005-[a-z0-9-]+\.md$")
#: The suffixes a human writes prose into, mirroring `tests/test_repo_invariants.py`'s sweep.
_PROSE_SUFFIXES = frozenset({".py", ".md", ".yaml", ".yml", ".smk", ".toml"})


class _IgnoreTags(yaml.SafeLoader):
    """`SafeLoader` that tolerates mkdocs-material's `!!python/name:` tags instead of raising.

    `mkdocs.yml` carries `format: !!python/name:pymdownx.superfences.fence_code_format`, which
    `safe_load` refuses by design. We only ever read `exclude_docs`, so resolving those tags to `None`
    is enough -- and far less brittle than regexing a YAML block out of the file by hand.
    """


def _ignore_unknown(loader: yaml.SafeLoader, suffix: str, node: yaml.Node) -> None:
    return None


# The module-level registrar, not `_IgnoreTags.add_multi_constructor`: same call one frame down
# (PyYAML forwards to the Loader), and the classmethod is the one place PyYAML's stubs leave
# unannotated.
yaml.add_multi_constructor("tag:yaml.org,2002:python/name:", _ignore_unknown, Loader=_IgnoreTags)


def _load(path: Path) -> dict[str, Any]:
    with path.open() as fh:
        loaded = yaml.load(fh, Loader=_IgnoreTags)
    assert isinstance(loaded, dict), f"{path.name} did not parse to a mapping"
    return loaded


def _markdownlint_ignore_for(excluded: str) -> str:
    """The `ignores` entry that covers one `exclude_docs` entry.

    `exclude_docs` is gitignore-shaped and relative to `docs_dir`; `ignores` holds globs relative to
    the repo root. A trailing slash marks a directory, which needs a recursive glob to cover the files
    inside it -- `docs/adr/` alone matches the directory, not `0005-run-id-is-the-pairing.md`.
    """
    return f"docs/{excluded}**" if excluded.endswith("/") else f"docs/{excluded}"


def _prose_files() -> list[Path]:
    """Every file a human writes prose into, on every surface a formula could be restated on.

    The two doc trees, the code, the thin clients, the corpus, and the root documents. Records sit
    beside the code they govern, so `src/` is where most of them are found.
    """
    roots = (_REPO / "docs", _REPO / "src", _REPO / "skills", _REPO / "tests", _REPO / "evals")
    return sorted(
        [p for root in roots for p in root.rglob("*") if p.suffix in _PROSE_SUFFIXES]
        + list(_REPO.glob("*.md"))
    )


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


def test_the_precise_run_id_formula_is_written_once() -> None:
    """One precise statement of the run id, and a gloss wherever a reader needs one.

    The record numbered 0005 decides the components and their order, and the precise spelling is
    fixed there and written nowhere else. Everywhere else -- the router, the glossary, both skills,
    the tutorial, and the implementation's own docstring, which is where a formula belongs on the code
    side -- carries the short gloss instead. A gloss cannot drift out of agreement with a decision it
    does not restate; a second *precise* statement can, and did: the id was written in two notations
    at once, with nothing to tell a reader there was a second or which one had moved.

    So this is a set check over one operator, not a ban on mentioning the formula. A line is a
    restatement only if it carries the operator *and* one of the two component names that only the
    precise spelling uses. That is what keeps the per-dataset candidates cache -- a different content
    address, sharing a component -- and every prose mention of a hash out of it.

    This module needs no exemption: it names the operator and the components on separate lines, and a
    restatement is both on one.
    """
    prose = _prose_files()
    owner = [path for path in prose if _RUN_ID_RECORD.match(path.name)]
    assert len(owner) == 1, (
        f"expected exactly one 0005-*.md record, found {[p.name for p in owner]} -- was the run id "
        "record renamed, split, or moved outside the trees this sweep reads?"
    )
    assert any(
        _HASH_JOIN in line and all(name in line for name in _PRECISE_RUN_ID)
        for line in owner[0].read_text(encoding="utf-8").splitlines()
    ), f"{owner[0].name} no longer states the formula it owns -- this guard would pass vacuously."

    offenders = [
        f"  {path.relative_to(_REPO).as_posix()}:{n}: {line.strip()[:90]}"
        for path in prose
        if path != owner[0]
        for n, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1)
        if _HASH_JOIN in line and any(name in line for name in _PRECISE_RUN_ID)
    ]

    assert not offenders, (
        "the run id formula is stated precisely outside the record that decides it:\n"
        + "\n".join(offenders)
        + f"\n\nWrite the gloss -- H(dataset {_HASH_JOIN} processing {_HASH_JOIN} kb {_HASH_JOIN} "
        "workflow) -- and cite ADR-0005. Two precise spellings drift; a gloss beside one precise "
        "statement does not."
    )
