"""Tests for the skills layer — do they stay TRUE as the CLI moves?

A skill is documentation that an agent will act on without checking. That makes a stale skill worse
than no skill: it is a confident instruction to run a verb that no longer exists, or to trust a rule
that changed. These tests pin the skill set against the actual CLI surface so drift is a red test.
"""

from __future__ import annotations

import itertools
import re
from pathlib import Path

import pytest
import yaml

SKILLS = Path(__file__).resolve().parents[1] / "skills"
EXPECTED = {
    "seqforge-orchestrate",
    "seqforge-exam",
    "seqforge-harvest",
    "seqforge-resolve",
    "seqforge-manifest",
    "seqforge-compose",
    "seqforge-io",
    "seqforge-kb-author",
    "seqforge-journal",
}


def _skill_dirs() -> list[Path]:
    return sorted(p.parent for p in SKILLS.glob("*/SKILL.md"))


def _frontmatter(path: Path) -> dict:
    text = path.read_text()
    match = re.match(r"^---\n(.*?)\n---\n", text, re.DOTALL)
    assert match, f"{path} has no YAML frontmatter"
    return yaml.safe_load(match.group(1))


def test_brief_section_10_ships_all_nine_skills() -> None:
    assert {p.name for p in _skill_dirs()} == EXPECTED


@pytest.mark.parametrize("skill", _skill_dirs(), ids=lambda p: p.name)
def test_frontmatter_is_valid_and_matches_the_directory(skill: Path) -> None:
    """The Agent Skills standard keys, and `name` must match the dir or discovery breaks."""
    fm = _frontmatter(skill / "SKILL.md")
    assert fm["name"] == skill.name
    assert fm["description"].strip()
    # the description is the ONLY thing an agent sees when deciding whether to load the skill
    assert len(fm["description"]) > 80, "too thin to route on"


def _code_spans(body: str) -> str:
    """Only fenced blocks and inline code — prose says "seqforge is a compiler", which is not a verb."""
    fences = re.findall(r"```[a-z]*\n(.*?)```", body, re.DOTALL)
    inline = re.findall(r"`([^`\n]+)`", body)
    return "\n".join([*fences, *inline])


@pytest.mark.parametrize("skill", _skill_dirs(), ids=lambda p: p.name)
def test_skill_documents_only_real_cli_verbs(skill: Path) -> None:
    """A skill naming a verb that does not exist is a confident instruction to fail.

    Scans `seqforge <verb>` in CODE contexts only and checks it against the real Typer app, so
    renaming a verb turns this red instead of silently misleading an agent. It has already earned
    itself once: it caught that `seqforge probe` was documented everywhere and never registered.

    **It now checks the SUBcommand too, and that is the gap this closes.** Checking only the group
    meant `seqforge io onlist fetch` passed because `io` exists — so the io skill documented
    `onlist list|show|fetch|add` while the app has `list|show|pack|write`, and two of the four were
    fiction. An agent following it runs a command that does not exist. The group is the part least
    likely to be wrong; the leaf is the part that gets renamed.
    """
    used = _verbs_used((skill / "SKILL.md").read_text())
    real = _real_verbs()
    unknown = sorted(v for v in used if v not in real and v.split()[0] not in _PLANNED)
    assert not unknown, (
        f"{skill.name} documents non-existent verb(s): {unknown}\n"
        f"real: {sorted(v for v in real if v.split()[0] in {u.split()[0] for u in unknown})}"
    )


#: Declared in the design's CLI surface, stage not yet landed. Listed EXPLICITLY so that documenting
#: a verb without implementing it stays a deliberate act. A group here exempts its whole subtree,
#: because there is nothing to check a leaf against when the group itself does not exist.
_PLANNED = {"run", "compile", "status", "journal"}


def _real_cli() -> tuple[set[str], set[str]]:
    """The live app's surface: ``(every invocation it answers to, the ones that are GROUPS)``.

    Introspected, never listed. A hand-written list of what the CLI offers is the exact shape this
    repo keeps finding rotted — and here it would rot in the direction of *permitting* fiction.

    Groups are returned separately because they are what makes the check precise: a word after a
    group must be one of its subcommands, and a word after a leaf command is just an argument.
    """
    from seqforge.cli import app

    def _leaves(a: object) -> set[str]:
        return {
            c.name or (c.callback.__name__ if c.callback else "")
            for c in getattr(a, "registered_commands", [])
        }

    verbs: set[str] = _leaves(app)
    groups: set[str] = set()

    def _walk(typer_app: object, prefix: str) -> None:
        for group in getattr(typer_app, "registered_groups", []):
            path = f"{prefix} {group.name}".strip()
            verbs.add(path)
            groups.add(path)
            if group.typer_instance is None:
                continue
            verbs.update(f"{path} {leaf}" for leaf in _leaves(group.typer_instance))
            _walk(group.typer_instance, path)

    _walk(app, "")
    return verbs, groups


def _verbs_used(body: str) -> set[str]:
    """`seqforge io onlist write --out x` -> {"io onlist write"}. Every claimed invocation, expanded.

    Two things a naive scanner gets wrong, and both were live here:

    **Falling back to a shorter prefix hides the bad leaf.** `io onlist fetch` is not real, but
    `io onlist` is — so "longest real prefix wins" quietly reports `io onlist` and passes. The rule
    that actually works: a word following a **group** must be one of its subcommands; a word
    following a **leaf command** is an argument and is ignored. `seqforge manifest fill FILES` stops
    at `manifest fill` because that is a command; `seqforge io onlist fetch` does not stop at
    `io onlist`, because that is a group and `fetch` is claiming to be one of its verbs.

    **`a|b|c` must be expanded.** Every skill documents its surface as
    `seqforge manifest fill|validate|hash`, so a scanner that stops at the first `|` checks the first
    alternative and blesses the rest. That is exactly how `seqforge io onlist list|show|fetch|add`
    survived: `list` and `show` are real, `fetch` and `add` never existed, and only `list` was ever
    looked at.
    """
    verbs, groups = _real_cli()
    out: set[str] = set()
    for match in re.finditer(r"\bseqforge((?:\s+[a-z][a-z0-9|-]*){1,3})", _code_spans(body)):
        for combo in itertools.product(*[w.split("|") for w in match.group(1).split()]):
            path = ""
            for word in combo:
                candidate = f"{path} {word}".strip()
                if path and path not in groups:
                    break  # `path` is a command; `word` is its argument
                path = candidate
                if path not in verbs:
                    break  # unknown: report it at the depth it went wrong
            out.add(path)
    return out


def _real_verbs() -> set[str]:
    return _real_cli()[0]


#: A CONCRETE lab path: two real segments under a cluster root. `/scratch/...` in prose is the rule
#: being stated, not a leak — flagging it is the same false-positive class as rejecting a URI for
#: looking like a path, and a check that cries wolf gets deleted.
_CONCRETE_SCRATCH = re.compile(r"/scratch/[A-Za-z0-9_-]+/[A-Za-z0-9_-]+")


@pytest.mark.parametrize("skill", _skill_dirs(), ids=lambda p: p.name)
def test_skill_never_leaks_a_lab_path(skill: Path) -> None:
    """This repo is public: it carries rules and accessions, never a path on our cluster.

    The held-out designation that first motivated this check was retired on 2026-07-15; the check was
    not, because it never depended on it. A lab path in a public repo is a leak regardless of what the
    data behind it is for.
    """
    body = (skill / "SKILL.md").read_text()
    found = _CONCRETE_SCRATCH.findall(body)
    assert not found, f"{skill.name} leaks a concrete lab path: {found}"


def test_the_leak_check_can_actually_catch_a_leak() -> None:
    """Prove the guard fires — a leak check that has never caught one proves nothing."""
    assert _CONCRETE_SCRATCH.findall("data at /scratch/somelab/someproject/reads")
    assert not _CONCRETE_SCRATCH.findall("`/scratch/...` in a manifest is a bug")


def test_installer_discovers_every_skill() -> None:
    import importlib.util

    spec = importlib.util.spec_from_file_location("sf_install", SKILLS / "install.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert {p.name for p in module.discover()} == EXPECTED
    # the paths are the only thing that varies per product — that is why they are a table
    assert set(module.TARGETS) >= {"claude", "agents"}


def test_the_verb_check_catches_a_fictional_SUBcommand() -> None:
    """Prove the guard fires on the thing it was blind to. It has never been green honestly before.

    Checking only the group meant every one of these passed:

      - `seqforge io onlist fetch` / `add` — the io skill's own listed surface; neither ever existed.
      - `seqforge kb confusability` — documented for a year. CLAUDE.md says outright "There is no
        `kb confusability` verb"; the skill said there was.
      - `seqforge resolve apply` / `adjudicate` — modelled, never built.

    Three skills, five fictional verbs, all found the day the guard learned to look one level down.
    """
    real = _real_verbs()
    assert "io onlist write" in real, "the check must know real subcommands"
    assert "io onlist fetch" not in real

    # a group's leaf is checked...
    assert _verbs_used("`seqforge io onlist fetch`") == {"io onlist fetch"}
    assert _verbs_used("`seqforge kb confusability`") == {"kb confusability"}
    # ...and every alternative in a `a|b|c` listing, not just the first
    assert _verbs_used("`seqforge io onlist list|show|fetch`") == {
        "io onlist list",
        "io onlist show",
        "io onlist fetch",
    }


def test_the_verb_check_does_not_cry_wolf_over_arguments() -> None:
    """A word after a COMMAND is its argument. A guard that flags `manifest fill FILES` gets deleted.

    This is the same false-positive class the lab-path check is careful about: the rule has to tell a
    claim apart from a mention. `fill` is a command, so `files` is an argument; `onlist` is a group,
    so the next word is claiming to be a verb.
    """
    assert _verbs_used("`seqforge manifest fill files`") == {"manifest fill"}
    assert _verbs_used("`seqforge kb show tech`") == {"kb show"}
    # prose is not code: the scanner never looks outside a fence or an inline span
    assert _verbs_used("seqforge kb confusability is not a thing") == set()
