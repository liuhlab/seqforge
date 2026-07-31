"""Tests for the docs config and the agent-facing router: do the documents agree with each other?

Two independent claims, both about files no other test reads.

**The two exclusion lists are the same list.** `mkdocs.yml`'s `exclude_docs` and
`.markdownlint-cli2.yaml`'s `ignores` answer the same question about `docs/`: which trees under it are
agent-facing rather than site prose. `agents/` and `adr/` are excluded from the built site because
agent-facing material must not read as settled guidance under a docs URL -- and for exactly that
reason they are not linted as site pages either.
They drifted once, and the failure was not theoretical: `agents/` was added to `exclude_docs` and not
to `ignores`, so `docs/agents/domain.md` was linted as a site page, failed MD040 and MD049, and turned
the `markdownlint` job red on every open PR. A comment saying "keep these in sync" is not a mechanism;
this is. The check is one-directional on purpose: everything mkdocs hides from the site must be
ignored by markdownlint, but `ignores` legitimately holds more (the KB wrapper pages and a symlinked
README are published, and skipped for their own reasons).

**The router and the enforcement map do not go stale.** `AGENTS.md` is the canonical ~70-line router
(`CLAUDE.md` is a symlink to it) and `docs/agents/rules.md` is the layer behind it: one section per
rule, naming the test that enforces it. Both failure modes have happened. `AGENTS.md` claimed the rules
were "R1-R15" for the whole life of the branch that consolidated them to R1-R11, because the count
lived in prose that nothing read. And the enforcement map's previous guard was a *comment* --
`tests/test_skills.py` asked a human not to rename a function "without updating CLAUDE.md's enforcement
table", which is a test's job written as a plea. Both are now mechanisms.
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
ROUTER = _REPO / "AGENTS.md"
RULES = _REPO / "docs" / "agents" / "rules.md"

#: A rule reference, `R7` or the `R1` and `R11` of a range. Rungs are written "rung 3", never "R3".
_RULE_REF = re.compile(r"\bR(\d+)\b")
#: A `## R7 -- ...` section heading in the enforcement map: the rules that document *claims* to cover.
_RULE_SECTION = re.compile(r"^## R(\d+)\b", re.MULTILINE)
#: Backtick-delimited spans, so a prose sentence cannot accidentally name a test.
_CODE_SPAN = re.compile(r"`([^`\n]+)`")
#: A test function as pytest collects it -- module level or in a class.
_TEST_DEF = re.compile(r"^\s*def (test_[A-Za-z0-9_]+)", re.MULTILINE)


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


def test_the_enforcement_map_names_tests_that_exist() -> None:
    """Every `test_*` the enforcement map cites must be a test the suite actually defines.

    `docs/agents/rules.md` is the registry the rule table used to carry inline: rule -> the file you
    can open and run. A registry of test names drifts on every refactor, and its previous guard was a
    comment in `tests/test_skills.py` asking a human to remember. This is the mechanism that comment
    was standing in for -- rename a cited test and this goes red, naming both ends.

    Only backtick-delimited spans count, and only ones that are a bare identifier, so
    ``tests/test_kb.py`` (a path) and prose that happens to contain the word are both ignored.
    """
    cited = {
        span
        for span in _CODE_SPAN.findall(RULES.read_text())
        if re.fullmatch(r"test_[a-z0-9_]+", span)
    }
    assert cited, "no test names found in docs/agents/rules.md -- has the map moved, or the format?"

    defined = {
        name
        for path in sorted((_REPO / "tests").glob("test_*.py"))
        for name in _TEST_DEF.findall(path.read_text())
    }
    missing = sorted(cited - defined)

    assert not missing, (
        "docs/agents/rules.md names test(s) that do not exist:\n"
        + "\n".join(f"  {name}" for name in missing)
        + "\nEither the test was renamed (update the enforcement map) or the map is fiction."
    )


def test_the_router_and_the_enforcement_map_name_the_same_rules() -> None:
    """The two files that enumerate the rules agree -- so nothing can go on saying "R1-R15".

    `AGENTS.md` claimed the rules were "R1-R15" long after `9b399d0` consolidated them to R1-R11,
    because the count sat in prose and the list sat somewhere else. The invariant is *agreement*, not a
    pinned count: `docs/agents/rules.md`'s `## R<n>` sections are the definition, and every rule id
    `AGENTS.md` names must be one of them. Adding an R12 to both files stays green; naming one in only
    one place does not.

    Scoped to those two files on purpose. Every other doc cites rules in prose *and* may legitimately
    contain a read designation -- `R1 = CB + UMI`, the `R64` genome build -- which is the same
    ambiguity `test_no_comment_points_at_a_governing_document_by_number` navigates under `src/`.
    """
    documented = {int(n) for n in _RULE_SECTION.findall(RULES.read_text())}
    assert documented, "docs/agents/rules.md has no `## R<n>` sections -- has its format changed?"

    router = {int(n) for n in _RULE_REF.findall(ROUTER.read_text())}
    assert router == documented, (
        "AGENTS.md and docs/agents/rules.md disagree about which rules exist.\n"
        f"  named in AGENTS.md only:  {sorted(router - documented)}\n"
        f"  documented in rules.md only: {sorted(documented - router)}\n"
        "The router carries every rule as an imperative, and names no rule the map does not cover."
    )
