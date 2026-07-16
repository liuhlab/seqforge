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

_REPO = Path(__file__).resolve().parents[1]
SKILLS = _REPO / "skills"

#: The human-facing site. Same guard, same reason: a tutorial telling someone to run a verb that does
#: not exist wastes their afternoon, and `docs/getting-started.md` really did say `seqforge probe` --
#: which the SKILL guard had caught and deleted from the skills, in the skills only, leaving the
#: identical claim standing three directories away. A guard scoped to where the bug was found is a
#: guard scoped to nothing.
DOCS = _REPO / "docs"
EXPECTED = {
    "seqforge-orchestrate",
    "seqforge-exam",
    "seqforge-harvest",
    "seqforge-resolve",
    "seqforge-manifest",
    "seqforge-compose",
    "seqforge-io",
    "seqforge-kb-author",
}


def _skill_dirs() -> list[Path]:
    return sorted(p.parent for p in SKILLS.glob("*/SKILL.md"))


def _frontmatter(path: Path) -> dict:
    text = path.read_text()
    match = re.match(r"^---\n(.*?)\n---\n", text, re.DOTALL)
    assert match, f"{path} has no YAML frontmatter"
    return yaml.safe_load(match.group(1))


def test_ships_exactly_the_expected_skills() -> None:
    """Both directions: a new skill must be added to EXPECTED, and a removed one (the fictional
    `seqforge-journal`, whose four verbs were never built) must leave it — a skill is a client of
    verbs that exist, so one with none is not a skill."""
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


def _doc_pages() -> list[Path]:
    """Every published page. `design.md` is excluded from the SITE and included here: it is the
    agent-facing source of truth, and an agent following a fictional verb fails exactly as a human
    does."""
    return sorted(DOCS.rglob("*.md"))


@pytest.mark.parametrize("page", [*_doc_pages(), _REPO / "README.md"], ids=lambda p: p.name)
def test_docs_document_only_real_cli_verbs(page: Path) -> None:
    """The same check as the skills', over the pages a human reads. See `_verbs_used`."""
    used = _verbs_used(page.read_text())
    real = _real_verbs()
    unknown = sorted(v for v in used if v not in real and v.split()[0] not in _PLANNED)
    assert not unknown, f"{page.name} documents non-existent verb(s): {unknown}"


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
#: `run`/`compile` graduated out of here once they landed — a planned verb that ships must leave this
#: set, or the guard would keep rubber-stamping the very fiction it exists to catch. It is now EMPTY:
#: `status`/`journal` were the last entries, exempting the whole `seqforge-journal` skill, which was
#: the guard blindfolding itself against the one skill that was entirely fiction. The skill is gone;
#: a future planned-but-unbuilt verb is added here deliberately, not inherited.
_PLANNED: set[str] = set()


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


#: A seqforge INVOCATION, as opposed to a mention of the word. Three deliberate narrowings, each
#: earned by a false positive this guard actually produced:
#:
#: - **line-start, or after `pixi run -- `**. That is how a command appears. Mid-sentence it is
#:   English: `design.md` contains "liulab-genome does not fetch annotations; seqforge stages the
#:   GTF", inside a docstring, inside a fence — and `stages` is a verb in the grammatical sense only.
#: - **`[ \t]`, not `\s`**: a newline ends an invocation. `\s` crossed it, so a fenced block reading
#:   `git clone .../seqforge` then `cd seqforge` parsed as the verb `seqforge cd`.
#: - **at most three words**: past that you are reading arguments.
#:
#: A guard that cries wolf gets ignored exactly when it is right, which is the same reason
#: `_CONCRETE_SCRATCH` below insists on two path segments.
_INVOCATION = re.compile(r"(?m)(?:^|(?<=-- ))seqforge((?:[ \t]+[a-z][a-z0-9|-]*){1,3})")


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
    # `[ \t]`, not `\s`: a newline ends the invocation. `\s` crossed it, so a fenced block reading
    # `git clone .../seqforge` then `cd seqforge` parsed as the verb `seqforge cd` -- a guard that
    # cries wolf gets ignored exactly when it is right.
    for match in re.finditer(_INVOCATION, _code_spans(body)):
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


def _real_flags() -> dict[str, set[str]]:
    """verb -> the long options it really takes. Introspected from the live app's click commands."""
    from typer.main import get_command

    from seqforge.cli import app

    out: dict[str, set[str]] = {}

    def _walk(cmd: object, prefix: str) -> None:
        commands = getattr(cmd, "commands", None)
        if commands:
            for name, sub in commands.items():
                _walk(sub, f"{prefix} {name}".strip())
            return
        out[prefix] = {
            opt
            for param in getattr(cmd, "params", [])
            for opt in getattr(param, "opts", [])
            if opt.startswith("--")
        }

    _walk(get_command(app), "")
    return out


@pytest.mark.parametrize(
    "page",
    [*_doc_pages(), *[d / "SKILL.md" for d in _skill_dirs()], _REPO / "README.md"],
    ids=lambda p: f"{p.parent.name}/{p.name}",
)
def test_documented_flags_exist(page: Path) -> None:
    """A verb that exists, called with a flag that does not, fails just as hard.

    `docs/getting-started.md` told people to run `manifest fill ... -o manifest.yaml` (there is no
    `-o`; it writes to the workspace) and `processing new --dataset manifest.yaml` (the manifest is a
    positional argument). Both verbs are real, so the verb check was green, and both commands exit 2.

    Only long options, and only for verbs we can resolve: a short flag is ambiguous in prose, and a
    placeholder like `--profile <your-cluster-profile>` belongs to snakemake, not to us.
    """
    flags = _real_flags()
    verbs, _ = _real_cli()
    body = _code_spans(page.read_text())
    bad: list[str] = []
    for match in _INVOCATION.finditer(body):
        words = match.group(1).split()
        verb = next((" ".join(words[:n]) for n in (3, 2, 1) if " ".join(words[:n]) in flags), None)
        if verb is None:
            continue
        # the rest of THIS line only — the next line is the next command
        line = body[match.end() : body.find("\n", match.end()) % (len(body) + 1)]
        for flag in re.findall(r"(?<![\w-])(--[a-z][a-z0-9-]*)", line):
            if flag not in flags[verb] and flag not in {"--help"}:
                bad.append(f"`seqforge {verb} {flag}` — real: {sorted(flags[verb])}")
    assert not bad, f"{page.name} documents non-existent flag(s):\n" + "\n".join(bad)


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


def test_the_flag_check_catches_a_fictional_flag() -> None:
    """Prove it fires. It found three the day it was written, and two were in the same skill.

    `--json` on `probe`, `io peek`, `io resolve` and `resolve score`: JSON on stdout is the
    default and there IS no flag, and four documented invocations passed one anyway. `processing new
    --dataset manifest.yaml` in `getting-started.md`: the manifest is a positional argument. Every one
    of those verbs is real, so the verb check was green and every command exits 2.
    """
    flags = _real_flags()
    assert "--json" not in flags["probe"], "JSON is the default; there is no --json flag"
    assert "--accession" in flags["manifest fill"]
    assert "--dataset" not in flags["processing new"]
    # and the scanner reads the flag off the line it is on, not the next command's
    assert "--workspace" in flags["manifest fill"]
