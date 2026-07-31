"""Tests for the docs config and the agent-facing router: do the documents agree with each other?

Four independent claims, all about files no other test reads.

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

**The enforcement map points at the ADRs instead of restating them.** It opens by declaring that
policy in prose -- rationale that became an ADR is pointed at here, never restated -- and then
restated four of them wholesale, so the policy is a shape check now. Prose that states a rule about
itself has failed here before, and this is the third time the remedy is a test rather than a
sentence.

**The ADR tree is reachable and its claims are held to the same standard.** Once the enforcement map
points rather than restates, the ADR is where a reader is *sent*, and the index is where they
arrive -- so an ADR the index does not list is an ADR nobody is sent to, and an index row pointing at
a file that was renamed is worse than no row. Each ADR also names the gate that enforces it, which
is the same claim the enforcement map makes and rots the same way; the cited-test-name guard reads
both trees and names which document made the claim.
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
ADR = _REPO / "docs" / "adr"
ADR_INDEX = ADR / "README.md"

#: A rule reference, `R7` or the `R1` and `R11` of a range. Rungs are written "rung 3", never "R3".
_RULE_REF = re.compile(r"\bR(\d+)\b")
#: A `## R7 -- ...` section heading in the enforcement map: the rules that document *claims* to cover.
_RULE_SECTION = re.compile(r"^## R(\d+)\b", re.MULTILINE)
#: Backtick-delimited spans, so a prose sentence cannot accidentally name a test.
_CODE_SPAN = re.compile(r"`([^`\n]+)`")
#: A test function as pytest collects it -- module level or in a class.
_TEST_DEF = re.compile(r"^\s*def (test_[A-Za-z0-9_]+)", re.MULTILINE)
#: One second-level section of the enforcement map: its heading, and its body up to the next one.
_SECTION = re.compile(r"^## (?P<title>[^\n]+)\n(?P<body>.*?)(?=^## |\Z)", re.MULTILINE | re.DOTALL)
#: The block every section owes: what you can open and run to watch the rule enforced.
_ENFORCED_BY = re.compile(r"^\*\*Enforced by\.\*\*", re.MULTILINE)
#: A link into the ADR tree, written relative from `docs/agents/`.
_ADR_LINK = re.compile(r"\]\(\.\./adr/")
#: Non-blank rationale lines a section may spend once it links an ADR. Derived in the guard below.
_RATIONALE_BUDGET = 12
#: An ADR's own filename: four digits and a slug. `README.md` (the index) and `_template.md` (the
#: house structure) match neither, which is how both stay out of an index *of decisions*.
_ADR_FILE = re.compile(r"^\d{4}-[a-z0-9-]+\.md$")
#: The same name written as a link target inside the index.
_ADR_FILE_REF = re.compile(r"\b\d{4}-[a-z0-9-]+\.md\b")


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


def _rule_sections() -> list[tuple[str, str]]:
    """Every second-level section of the enforcement map, as `(heading, body)`.

    Deliberately not restricted to the `## R<n>` ones. The two sections that restated an ADR most
    heavily were the trailing prose ones, which a rule-numbered scope would have walked straight
    past.
    """
    sections = [(m["title"].strip(), m["body"]) for m in _SECTION.finditer(RULES.read_text())]
    assert sections, "docs/agents/rules.md has no `## ` sections -- has its format changed?"
    return sections


def _rationale_lines(body: str) -> list[str]:
    """The non-blank lines of a section that are not its enforcement block.

    The enforcement block is one paragraph, so it runs from `**Enforced by.**` to the next blank
    line. Everything else in the section is rationale, whether it sits above the block or below it.
    """
    kept: list[str] = []
    in_block = False
    for line in body.splitlines():
        if _ENFORCED_BY.match(line):
            in_block = True
        elif in_block and not line.strip():
            in_block = False
        if not in_block and line.strip():
            kept.append(line)
    return kept


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


def _documents_that_name_gates() -> list[Path]:
    """Every document that claims a test enforces something: the enforcement map, and the ADRs.

    Two trees, one claim. `docs/agents/rules.md` names what enforces each rule; each ADR names the
    gate on its decision, in a `**Gate.**` line that is the same promise in a different file. The
    ADR tree was outside this guard when it was one tree, so a test name written into an ADR could
    rot in silence -- and the ADRs are where the enforcement map now *sends* a reader for the
    argument, which makes them the more read of the two.

    The index and the template are read on the same terms and for the same reason: both cite this
    guard by name to explain what holds them honest, and a guard that cannot see its own citation is
    the kind of thing this module exists to stop.
    """
    return [RULES, *sorted(ADR.glob("*.md"))]


def test_the_enforcement_map_names_tests_that_exist() -> None:
    """Every `test_*` a governing document cites must be a test the suite actually defines.

    `docs/agents/rules.md` is the registry the rule table used to carry inline: rule -> the file you
    can open and run. A registry of test names drifts on every refactor, and its previous guard was a
    comment in `tests/test_skills.py` asking a human to remember. This is the mechanism that comment
    was standing in for -- rename a cited test and this goes red, naming both ends.

    It reads the ADR tree too. The name still says "enforcement map" because that is what
    `docs/agents/rules.md` calls this guard, in the paragraph explaining why the map can be trusted;
    renaming it would falsify the sentence that recommends it. What changed is the input, and so the
    message: with two sources, "this name is fiction" is only actionable if it also says *who said
    so*.

    Only backtick-delimited spans count, and only ones that are a bare identifier, so
    ``tests/test_kb.py`` (a path) and prose that happens to contain the word are both ignored.
    """
    cited: dict[str, set[str]] = {}
    for path in _documents_that_name_gates():
        for span in _CODE_SPAN.findall(path.read_text()):
            if re.fullmatch(r"test_[a-z0-9_]+", span):
                cited.setdefault(span, set()).add(path.relative_to(_REPO).as_posix())
    assert cited, (
        "no test names found in docs/agents/rules.md or docs/adr/ -- has the format moved?"
    )

    defined = {
        name
        for path in sorted((_REPO / "tests").glob("test_*.py"))
        for name in _TEST_DEF.findall(path.read_text())
    }
    missing = sorted(
        (document, name)
        for name, documents in cited.items()
        if name not in defined
        for document in documents
    )

    assert not missing, (
        "a governing document names test(s) that do not exist:\n"
        + "\n".join(f"  {document} names {name}" for document, name in missing)
        + "\nEither the test was renamed -- update the document that names it -- or the claim is "
        "fiction. An ADR with no real gate says so in words instead."
    )


def test_every_section_of_the_enforcement_map_names_what_enforces_it() -> None:
    """A rule with no gate must be visible as one, not assumed to have a gate somewhere.

    The file's own promise is "the **file you can open and run** to watch it enforced", and
    `test_the_enforcement_map_names_tests_that_exist` already keeps the names in those blocks honest.
    What that guard cannot see is a section with no block at all -- it reads the whole file for
    backticked test names and never asks which section they came from, so a section that names
    nothing is silently indistinguishable from one whose gates are all elsewhere. Two sections were
    in exactly that state when this landed.

    Scoped to `docs/agents/rules.md`, which is the one file that owes an enforcement block per
    section. It is not a prose linter for `docs/`.
    """
    unenforced = [title for title, body in _rule_sections() if not _ENFORCED_BY.search(body)]

    assert not unenforced, (
        "section(s) of docs/agents/rules.md name nothing that enforces them:\n"
        + "\n".join(f"  ## {title}" for title in unenforced)
        + "\nAdd an `**Enforced by.**` block naming a real gate, or say plainly that none exists."
    )


def test_a_section_that_links_an_adr_glosses_it_rather_than_restating_it() -> None:
    """Where an ADR argues a decision, the section keeps a gloss and the ADR keeps the argument.

    This file's preamble states that policy -- rationale that became an ADR is pointed at here,
    never restated -- and then restated four of them, which is the whole reason the policy is a
    mechanism now. The cost was not theoretical: the compiled run's id formula existed in two
    notations at once, one here and one in the ADR that decides it, with nothing to tell a reader
    there was a second and no way to know which had drifted.

    **Where the budget comes from.** Counting non-blank lines outside the enforcement block, the
    sections that already pointed rather than restated ran 4 to 10, the widest of them spending ten
    on a gloss of three ADRs. The budget is that widest complying section plus two lines of
    headroom. At this file's wrap that buys a two-sentence gloss plus one bulleted link per ADR --
    comfortably more than any complying section needed, and not enough for a precis. The section
    that failed when this landed was a four-ADR precis at 14 lines.

    A line budget rather than a word count because lines are what the reader skims, and the number
    is deliberately generous: a check that fires on honest prose gets deleted, and then nothing is
    checked at all.
    """
    over: list[tuple[str, int]] = []
    for title, body in _rule_sections():
        if not _ADR_LINK.search(body):
            continue
        spent = len(_rationale_lines(body))
        if spent > _RATIONALE_BUDGET:
            over.append((title, spent))

    assert not over, (
        "section(s) of docs/agents/rules.md restate the ADR they link instead of glossing it:\n"
        + "\n".join(f"  ## {title} -- {n} lines, budget {_RATIONALE_BUDGET}" for title, n in over)
        + "\nLeave the imperative and the links; the argument belongs in the ADR, once."
    )


def test_the_adr_index_and_the_adr_tree_hold_the_same_files() -> None:
    """The index is the tree's entry point, so a record it omits is one nobody is sent to.

    "Read the ADRs that touch the area you're about to work in" is only obeyable through an index --
    the alternative is opening all sixteen -- which makes an unlisted ADR invisible in practice and
    a row pointing at a renamed file worse than no row at all. Both directions, because both are the
    same set comparison and the second is the one a rename produces.

    A set comparison and nothing else. Whether a row's gloss is *accurate* is a review problem; the
    prose here is deliberately unchecked, in the same spirit as the *So in code* obligation the
    index itself records as unmechanised.

    `_template.md` is excluded, deliberately: it is the house structure, not a decision, and listing
    it in an index of decisions would make it read as one. Its name is what excludes it -- only
    `NNNN-slug.md` counts -- so a template renamed to look like an ADR would be caught, which is the
    right way round.
    """
    tree = {path.name for path in ADR.glob("*.md") if _ADR_FILE.match(path.name)}
    assert tree, "no numbered ADRs found under docs/adr/ -- has the tree moved, or the naming?"

    listed = set(_ADR_FILE_REF.findall(ADR_INDEX.read_text()))
    unlisted = sorted(tree - listed)
    dangling = sorted(listed - tree)

    assert not (unlisted or dangling), (
        "docs/adr/README.md and the ADR tree disagree about which records exist.\n"
        + "".join(f"  in the tree, missing from the index: {name}\n" for name in unlisted)
        + "".join(f"  in the index, missing from the tree: {name}\n" for name in dangling)
        + "Add the row (both tables), or fix the link the rename left behind."
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
