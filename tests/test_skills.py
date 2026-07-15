"""Tests for the skills layer — do they stay TRUE as the CLI moves?

A skill is documentation that an agent will act on without checking. That makes a stale skill worse
than no skill: it is a confident instruction to run a verb that no longer exists, or to trust a rule
that changed. These tests pin the skill set against the actual CLI surface so drift is a red test.
"""

from __future__ import annotations

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
    """
    from seqforge.cli import app

    registered = {g.name for g in app.registered_groups} | {
        c.name or (c.callback.__name__ if c.callback else "") for c in app.registered_commands
    }
    # declared in the design's CLI surface, stage not yet landed. Listed EXPLICITLY so that adding a
    # verb to a skill without implementing it stays a deliberate act, not an accident.
    planned = {"run", "compile", "status", "journal"}

    used = set(
        re.findall(r"\bseqforge ([a-z][a-z-]*)", _code_spans((skill / "SKILL.md").read_text()))
    )
    unknown = used - registered - planned
    assert not unknown, f"{skill.name} documents non-existent verb(s): {sorted(unknown)}"


#: A CONCRETE path under a held-out-looking root: two real segments. `/scratch/...` in prose is the
#: rule being stated, not a leak — flagging it is the same false-positive class as rejecting a URI
#: for looking like a path, and a check that cries wolf gets deleted.
_CONCRETE_SCRATCH = re.compile(r"/scratch/[A-Za-z0-9_-]+/[A-Za-z0-9_-]+")


@pytest.mark.parametrize("skill", _skill_dirs(), ids=lambda p: p.name)
def test_skill_never_leaks_a_heldout_path(skill: Path) -> None:
    """Design §8: this public repo carries the rule, never the paths."""
    body = (skill / "SKILL.md").read_text()
    found = _CONCRETE_SCRATCH.findall(body)
    assert not found, f"{skill.name} leaks a concrete held-out path: {found}"


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
