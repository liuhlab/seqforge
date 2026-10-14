#!/usr/bin/env python3
"""The conformance check: the rules a Liu Lab repo keeps after it leaves the template.

    pixi run conformance
    python scripts/conformance.py [--root PATH]

It states the RULE, not the file contents — a pull model, so a repo that legitimately diverges
stays green while the shared conventions stay checked. Fifteen rules: thirteen fail, two only warn.

Three exit statuses, spelled the way `scripts/check.sh` spells its own: **0** every rule passed,
**1** it ran and a rule failed, **2** it could not run at all — no `.git` to list tracked files
from, no `pyproject.toml`, a manifest that will not parse. The last is not a repo that broke a
rule, it is a repo nothing checked, and the two want opposite answers: fix the repo, or fix the
invocation. One exit code for both sends whoever reads the log hunting a violation that is not
there.

Three things it does on purpose:

- **It collects every failure.** One run tells you everything to fix, the same idiom
  `scripts/check.sh` uses. Nothing short-circuits.
- **It names the rule and a fix.** You should never have to read this file to act on its output,
  so nothing here prints a rewritten assert.
- **It says what it did NOT check.** A rule whose premise is absent is vacuous, not failing — a
  repo with no `src/` is not a repo that failed to have tests — but a vacuous rule looks exactly
  like a passing one, so each is reported and says why.

The declared agent-facing set is `[tool.liulab.agent-docs]` in `pyproject.toml`: keys are plain
path prefixes matched against `git ls-files`, values name the Vale Length rule that applies or
`false` where none does. Rules 2 and 3 both read it, so the two consumers cannot drift.

Waivers are `[tool.liulab.waived]`, keyed by RULE NAME with the reason as the value. Per-rule,
never per-file. No expiry, and every waiver is printed on every run — green ones included —
because an escape hatch that stops being visible becomes permanent. Rule 1 refuses to be waived.

This lives in `scripts/` and not `tests/` because one lab repo has neither `src/` nor `tests/`,
and pytest is in no default environment. Each rule is tested against a tree that violates it,
because a rule that checks nothing passes the happy path and a gate that fires on nothing looks
identical to a gate that passes.
"""

from __future__ import annotations

import argparse
import fnmatch
import posixpath
import re
import shlex
import subprocess
import sys
import textwrap
import tomllib
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from functools import cached_property
from pathlib import Path, PurePosixPath
from typing import Any

import yaml

#: The three exit statuses, named so the contract is written once and read where it is returned.
#: `CANNOT_RUN` is the one that says nothing about this repo's rules: `scripts/check.sh` already
#: spells "could not run" as 2 — a usage error, a capture directory it could not make — and a
#: checker that exited 1 for both would have every reader looking for a rule violation instead of
#: for the missing checkout.
PASSED, FAILED, CANNOT_RUN = 0, 1, 2


class CannotRunError(Exception):
    """The tree could not be read at all, so no rule was checked.

    Raised by :func:`load` and turned into `CANNOT_RUN` by :func:`main`. It is deliberately not a
    `SystemExit` carrying a message: Python prints such a message and exits **1**, which is the
    status a failed rule already uses, and that is the conflation this class exists to remove.
    """


#: The template's placeholder module name, SPELLED IN TWO PIECES on purpose. `init-repo`
#: substitutes that name throughout the tree and the dogfood job then greps a rendered repo for
#: it. A literal here would be rewritten — turning rule 1 into a check for the new module's own
#: name, which is everywhere — and would leave a copy for that grep to find. Two pieces, and
#: neither happens.
PLACEHOLDER = "new" + "pkg"

#: Rule 1 and warning 14 are both gated on this directory, and it is the only discriminator either
#: needs. While it is here, `init-repo` has not run: the placeholder is still the repo's own name,
#: and the auto-discovered skill is itself the nag. Once it is gone the repo claims to be its own,
#: and both rules start checking.
INIT_SKILL = "skills/init-repo"

#: Rule 1's name, needed by name because it is the one rule that refuses to be waived.
UNWAIVABLE = "placeholder-rename"

#: The glossary, by the name the upstream skills read and write, and the name each file had
#: before upstream renamed it. A repo still on the old name is failed rather than passed over:
#: "no GLOSSARY.md" would be the quiet answer to a rename that never happened.
GLOSSARY = "GLOSSARY.md"
GLOSSARY_MAP = "GLOSSARY-MAP.md"
GLOSSARY_RENAMES = (("CONTEXT.md", GLOSSARY), ("CONTEXT-MAP.md", GLOSSARY_MAP))

#: An upstream-format glossary entry: a line that is a bold term and a colon and nothing else,
#: the definition on the lines below. Whole-line on purpose. A lab entry may open a paragraph on
#: a bold phrase and a colon and run on, and that is prose.
_UPSTREAM_ENTRY_RE = re.compile(r"^\*\*(?P<term>[^*]+)\*\*:\s*$")

#: The site configuration every repo that publishes a site has, and where the site source sits
#: when it does not say. Rules 2 and 13 are both scoped to the pages the site publishes, so they
#: read the declared source rather than assuming it.
SITE_CONFIG = "mkdocs.yml"
DEFAULT_DOCS_DIR = "docs"

#: A site configuration AT THE REPO ROOT, `mkdocs.yml` and any variant beside it. Root-anchored,
#: so an example config under `docs/` is a document and not a configuration.
#:
#: Rule 13 reads every one of them, not only `SITE_CONFIG`, because a repo may build more than one
#: site from one docs tree: a second configuration `INHERIT`s the first and APPENDS to its nav,
#: and the navbar the build renders carries the entries of both. A repo with one configuration
#: reads one, and this costs nothing.
#:
#: Every one of them INCLUDING a local overlay no task builds, which is the known cost. Reading
#: instead only the configurations a declared docs task names with `-f` was considered and
#: declined: pixi writes a task as a string, as `{cmd = "..."}` or as a list, so it needs a second
#: command-line parser beside rule 12's, and it would stop reading a configuration built by a
#: Makefile or a workflow rather than by a task. A rule that reads one file too many is the
#: cheaper error here.
_SITE_CONFIG_RE = re.compile(r"^mkdocs(?:\..+)?\.ya?ml$")

#: Where GitHub reads workflows, and the two file endings it accepts. Nothing below this
#: directory is a workflow, and nothing above it is read.
WORKFLOW_DIR = ".github/workflows/"
WORKFLOW_SUFFIXES = (".yml", ".yaml")

#: The publishing workflow, named by PATH rather than sniffed out of its contents. That is a
#: deliberate choice and not a shortcut: a package index binds its trusted publisher to an owner,
#: a repository and a WORKFLOW FILENAME, so this name is external configuration — rename the file
#: and the repo cannot publish at all. `init-repo` also deletes it by this exact path. Rules 8 and
#: 9 both read it, so the name is written here once and nowhere else.
RELEASE_WORKFLOW = f"{WORKFLOW_DIR}release.yml"

#: The tag `init_repo.py` creates on a repo's opening day, and the one ref rule 9 asks about. A
#: trigger that admits it runs the publishing workflow before anyone has decided to release.
FIRST_TAG = "v0.0.0"

#: Vale's spelling of on and off. Both forms appear in the wild; the gate accepts either.
VALE_ON = {"YES", "TRUE", "ON"}
VALE_OFF = {"NO", "FALSE", "OFF"}
#: A key holding one of these is a glob, and a glob key matches no file.
GLOB_CHARS = frozenset("*?[")

#: The command a workflow step is allowed to run, and the table its task must be declared in.
#: Both are named here because rule 12's problem text and its fix both spell them.
PIXI_RUN = "pixi run"
TASK_TABLE = "[tool.pixi.tasks]"

#: Every `pixi run` option that takes a SEPARATE value, so the token after one is that value and
#: never the task name. Both spellings of each, because a workflow writes either. The
#: `--option=value` form needs no entry, being one token.
#:
#: READ OFF `pixi run --help`, at pixi 0.71.2, and not guessed. A missing spelling is not a safe
#: error: the option's value is mistaken for the task, and a step that literally is
#: `pixi run -p linux-64 build` is told it runs a command rather than `pixi run <task>`. That
#: shipped — the list held eight spellings of the twelve options pixi has. Re-measure when the
#: pixi version moves, and add both spellings of anything new.
PIXI_VALUE_OPTIONS = frozenset(
    {
        "-e",
        "--environment",
        "-p",
        "--platform",
        "-m",
        "--manifest-path",
        "-w",
        "--workspace",
        "--color",
        "--auth-file",
        "--config-file",
        "--pinning-strategy",
        "--pypi-keyring-provider",
        "--tls-root-certs",
        "--concurrent-solves",
        "--concurrent-downloads",
    }
)

#: What separates a task from the arguments passed to it, and the tokens that are not arguments
#: at all. pixi reads a bare `--durations=10` as its own option and refuses to run, so a step
#: passing one without the separator is broken before this rule sees it. A shell operator after
#: the task starts a second command, which is the drift rule 12 is about.
ARGUMENT_SEPARATOR = "--"
SHELL_OPERATORS = frozenset({"&&", "||", "|", ";", "&"})

#: What a `${{ ... }}` expression is replaced by before a step's command is split into tokens.
#: GitHub substitutes these when the job runs, and `shlex` would split one into three tokens and
#: read the middle as the task name. The stand-in is not a legal task name, so a step that names
#: its task with an expression is reported as unresolved rather than failed.
EXPRESSION = "${{}}"
_EXPRESSION_RE = re.compile(r"\$\{\{.*?\}\}", re.DOTALL)

_SECTION_RE = re.compile(r"^\[(?P<header>.+)\]\s*$")
_SETTING_RE = re.compile(r"^(?P<key>[A-Za-z][^=]*?)\s*=\s*(?P<value>.*?)\s*$")
_SKILL_FILE_RE = re.compile(r"^skills/[^/]+/SKILL\.md$")
#: A skill directory, at the source of truth and at both committed discovery paths. `skills/`
#: needs the trailing slash so `skills/install.py` is not read as a skill called `install.py`;
#: the discovery paths do not, because `git ls-files` reports a symlink to a directory as a plain
#: path with nothing after it — and a symlink is exactly how a name gets shadowed.
_SKILL_DIR_RES = (
    re.compile(r"^skills/(?P<name>[^/]+)/"),
    re.compile(r"^\.(?:claude|agents)/skills/(?P<name>[^/]+)(?:/|$)"),
)

#: The prefix the lab's shared plugin owns, and where it ships every other name: Claude Code's
#: clone of the `liulab` marketplace, relative to the home directory. Kimi Code reads its skills
#: from that clone too. Read from the machine, so CI, with no plugin installed, cannot check it.
LAB_PREFIX = "lab-"
PLUGIN_SKILLS = ".claude/plugins/marketplaces/liulab/skills"


@dataclass(frozen=True)
class SecondToolchain:
    """One resolver other than pixi, and where it leaves the environment it resolved.

    Attributes
    ----------
    tool
        The tool named in the failure, and listed in the note that says what was looked for.
    name
        A regex FULL-matched against a tracked path's FILE NAME, so a marker is found wherever
        it sits. Un-anchored on purpose: `env/poetry.lock` resolves the same environment
        `poetry.lock` does, and anchoring at the root would have made `git mv` the fix.
    table
        A dotted table in `pyproject.toml`, for a resolver configured in the manifest rather
        than in a file of its own.
    """

    tool: str
    name: str = ""
    table: str = ""


#: Every marker of a second resolver. Rule 10 reads this and nothing else, so covering one more
#: resolver is one more line here.
#:
#: It is a constant and not a `[tool.liulab.*]` key because it is not a claim a repo makes about
#: itself: the rule is the same in every Liu Lab repo, and a repo that must keep one of these has
#: `[tool.liulab.waived]`, which is printed on every run. A manifest list could be emptied
#: instead, and an escape hatch nobody can see is the thing this file exists to prevent.
#:
#: One category and nothing wider: a competing resolver's GENERATED artifact, or the table that
#: configures it. Each has no legitimate form, no human reader, and no sensible use in a
#: subdirectory. Lockfiles are named per tool, never `*.lock`, because `pixi.lock` is the lock
#: this rule protects.
#:
#: Four markers were measured firing on correct work and are gone. `requirements.txt`, because a
#: notebook a researcher runs on Colab needs one and cannot run pixi. `environment.yml`, because
#: `binder/environment.yml` is how a repo ships a runnable notebook. `setup.py`, because it
#: builds C extensions while declaring no dependency at all. `.pre-commit-config.yaml`, because
#: the hook shape a Liu Lab repo would write — `language: system`, `entry: pixi run lint` — pins
#: nothing and is the pattern that REMOVES the drift this rule names. Telling a researcher to
#: delete any of them is advice that breaks working science.
SECOND_TOOLCHAINS: tuple[SecondToolchain, ...] = (
    SecondToolchain("poetry", name=r"poetry\.lock", table="tool.poetry"),
    SecondToolchain("uv", name=r"uv\.lock", table="tool.uv"),
    SecondToolchain("pdm", name=r"pdm\.lock", table="tool.pdm"),
    SecondToolchain("pipenv", name=r"Pipfile(\.lock)?"),
)


class _TolerantLoader(yaml.SafeLoader):
    """A SafeLoader that reads a config it does not fully understand.

    mkdocs configurations carry local tags — `!ENV`, `!!python/name:...` for an emoji index — and
    a plain `safe_load` raises on the first one. Rules 2 and 13 only want `nav:`, `docs_dir:` and
    `INHERIT:`, so an unknown tag becomes ``None`` rather than an exception: a derived repo that
    adds one must not turn a conformance rule into a crash.
    """


# `None` is PyYAML's catch-all prefix: it is consulted only for a tag no constructor claimed.
_TolerantLoader.add_multi_constructor(None, lambda _loader, _suffix, _node: None)


def _problem(what: str, why: str, fix: str) -> str:
    """Format one problem for whoever has to act on it.

    Three lines and always the same three — WHAT is wrong, WHY it matters, and how to repair it —
    which is the shape `skills/install.py --check` already prints, so the two commands an agent
    meets in this repo read the same way. The what line is left unwrapped so it stays one
    greppable line.
    """
    body = textwrap.fill(why, width=94, initial_indent="    ", subsequent_indent="    ")
    return f"{what}\n{body}\n    fix: {fix}"


@dataclass
class Result:
    """What one rule found when it ran.

    Attributes
    ----------
    problems
        One entry per violation, formatted by :func:`_problem`. Empty means the rule held.
    notes
        What the rule examined, or — when its premise was absent — why it examined nothing. A
        vacuous rule prints identically to a passing one unless it says so itself.
    """

    problems: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class Step:
    """One step of one job, flattened out of the workflow it came from.

    Attributes
    ----------
    workflow
        Repo-relative path of the file this step was written in.
    job
        The job key it sits under, so a report can say where without a second lookup.
    index
        Its position in that job's `steps:` list, counting from zero. A step need not have a
        `name:`, and this is the only thing that always identifies one.
    name, uses, run
        The three keys a rule asks about, each ``None`` when the step does not set it. A step
        has `uses` or `run` and never both.
    """

    workflow: str
    job: str
    index: int
    name: str | None
    uses: str | None
    run: str | None


@dataclass(frozen=True)
class Workflow:
    """One workflow definition, parsed once for every rule that reads workflows.

    Attributes
    ----------
    path
        Repo-relative path, e.g. `.github/workflows/ci.yml`.
    name
        The `name:` key, or the file stem when the workflow does not name itself.
    triggers
        The `on:` block, normalized to event name -> the configuration under it, with `{}` for
        an event written bare. All three spellings — `on: push`, `on: [push, pull_request]` and
        the mapping form — arrive here identically, so a rule never has to ask which was used.
    steps
        Every step of every job, in file order, each carrying the job it came from. Flat because
        the rules that read steps ask about all of them, and a step knows its own job.
    """

    path: str
    name: str
    triggers: dict[str, Any]
    steps: tuple[Step, ...]


def _string(value: Any) -> str | None:
    """One YAML scalar as a string, or ``None`` when the key was absent or not a scalar."""
    return value if isinstance(value, str) else None


def _triggers(document: dict[Any, Any]) -> dict[str, Any]:
    """Read a workflow's `on:` block as event name -> its configuration.

    Read under two keys, because YAML 1.1 resolves an unquoted `on` to the boolean true: every
    workflow GitHub accepts arrives with its triggers under ``True``, and `document["on"]` finds
    them only in the rare file that quoted the key. Looking under one key alone is not a parse
    bug that shows up later — it makes every rule about triggers pass on nothing.
    """
    raw = document.get("on", document.get(True))
    if isinstance(raw, str):
        return {raw: {}}
    if isinstance(raw, list):
        return {str(event): {} for event in raw}
    if isinstance(raw, dict):
        return {str(event): {} if config is None else config for event, config in raw.items()}
    return {}


def _steps(path: str, document: dict[Any, Any]) -> tuple[Step, ...]:
    """Every step of every job in one workflow, flattened, in file order."""
    jobs = document.get("jobs")
    if not isinstance(jobs, dict):
        return ()
    steps: list[Step] = []
    for job_id, job in jobs.items():
        if not isinstance(job, dict):
            continue
        raw = job.get("steps")
        if not isinstance(raw, list):
            continue
        for index, step in enumerate(raw):
            if not isinstance(step, dict):
                continue
            steps.append(
                Step(
                    workflow=path,
                    job=str(job_id),
                    index=index,
                    name=_string(step.get("name")),
                    uses=_string(step.get("uses")),
                    run=_string(step.get("run")),
                )
            )
    return tuple(steps)


@dataclass(frozen=True)
class SiteConfig:
    """One site configuration, and the nav entries it contributes to a build.

    Attributes
    ----------
    path
        Repo-relative path of the configuration file, e.g. `mkdocs.yml`.
    docs_dir
        The site source this file resolves its nav paths against, repo-relative, without a
        trailing slash, and AFTER `INHERIT`: a configuration that inherits another and names no
        source of its own resolves against the parent's.
    declares_nav
        Whether the file has a `nav:` key at all. A configuration with none is not one whose nav
        is empty — the generator builds the navbar from the directory tree instead — so a rule
        reports it as unchecked rather than green.
    generators
        The page-generating plugins this file declares, along `INHERIT`, in name order. A
        configuration with one builds pages nothing tracks, so its nav cannot be resolved
        against a checkout at all.
    targets
        Repo-relative path -> the nav string that named it, for every entry that names a page.
    unresolved
        The entries that name no page and no link — a directory, an `...` — as written. Not a
        problem and not a target: the rule says it did not resolve them.
    """

    path: str
    docs_dir: str
    declares_nav: bool
    generators: tuple[str, ...]
    targets: dict[str, str]
    unresolved: tuple[str, ...]


@dataclass
class Repo:
    """The tree under inspection and the declarations it makes about itself."""

    root: Path
    tracked: tuple[str, ...]
    agent_docs: dict[str, Any]
    waived: dict[str, str]

    def exists(self, rel: str) -> bool:
        """Whether a path is present on disk, tracked or not."""
        return (self.root / rel).exists()

    def read(self, rel: str) -> str | None:
        """Read a file, or return ``None`` when it is not there."""
        path = self.root / rel
        if not path.is_file():
            return None
        return path.read_text(encoding="utf-8", errors="replace")

    def under(self, prefix: str) -> list[str]:
        """Every tracked path under one `[tool.liulab.agent-docs]` key.

        Keys are plain path prefixes, deliberately not globs: reimplementing Vale's glob matcher
        would reproduce two known surprises. A key ending in `/` matches everything beneath that
        directory; any other key matches that one path exactly, so `AGENTS.md` cannot also claim
        `AGENTS.md.bak`.
        """
        if prefix.endswith("/"):
            return [p for p in self.tracked if p.startswith(prefix)]
        return [p for p in self.tracked if p == prefix]

    def tracked_under(self, prefix: str) -> bool:
        """Whether anything tracked lives under a directory prefix."""
        return any(p.startswith(prefix) for p in self.tracked)

    def config(self, rel: str) -> dict[str, Any]:
        """One site configuration file, parsed, or an empty mapping when it is not there.

        A file that will not parse, or that is not a mapping, becomes an empty configuration
        rather than an exception, exactly as `workflows` does: a derived repo's unusual site
        config must not turn a conformance rule into a crash.
        """
        text = self.read(rel)
        if text is None:
            return {}
        try:
            # Not `safe_load`: `_TolerantLoader` is SafeLoader plus a shrug at unknown tags.
            loaded = yaml.load(text, Loader=_TolerantLoader)
        except yaml.YAMLError:
            loaded = None
        return loaded if isinstance(loaded, dict) else {}

    def inherit_chain(self, rel: str) -> Iterator[dict[str, Any]]:
        """One configuration and every one it `INHERIT`s, nearest first.

        `INHERIT` is a path relative to the configuration that writes it. Anything a child does
        not declare it takes from the parent, so a rule reading the child alone reads half a
        configuration.

        The visited set is not defensive clutter: a cycle is a configuration nobody can build, and
        it must not be a conformance crash either.
        """
        seen: set[str] = set()
        while rel not in seen:
            seen.add(rel)
            document = self.config(rel)
            yield document
            parent = document.get("INHERIT")
            if not isinstance(parent, str):
                return
            rel = posixpath.normpath(posixpath.join(posixpath.dirname(rel), parent))

    def resolved_docs_dir(self, rel: str) -> str:
        """One configuration's site source, resolved along the `INHERIT` chain.

        Walked rather than read from the one file, because a configuration that inherits another
        and declares no `docs_dir` of its own uses the parent's, and reading only the child would
        resolve its nav against a directory that is not the site source.
        """
        for document in self.inherit_chain(rel):
            declared = document.get("docs_dir")
            if isinstance(declared, str):
                return posixpath.normpath(declared).strip("/")
        return DEFAULT_DOCS_DIR

    def page_generators(self, rel: str) -> tuple[str, ...]:
        """Every page-generating plugin one configuration declares, along the `INHERIT` chain.

        Walked for the same reason the site source is: a child that inherits a parent declaring
        `gen-files` builds the same generated pages, and its own nav names them.
        """
        declared = {name for document in self.inherit_chain(rel) for name in _plugins(document)}
        return tuple(sorted(declared & PAGE_GENERATORS))

    @cached_property
    def mkdocs(self) -> dict[str, Any]:
        """`mkdocs.yml`, or an empty mapping when the repo publishes no site."""
        return self.config(SITE_CONFIG)

    @cached_property
    def docs_dir(self) -> str:
        """The site source directory, repo-relative and without a trailing slash."""
        return self.resolved_docs_dir(SITE_CONFIG)

    @cached_property
    def nav(self) -> dict[str, str]:
        """Repo-relative path -> the `mkdocs.yml` nav entry that names it."""
        targets, _ = _nav_targets(self.docs_dir, self.mkdocs.get("nav"))
        return targets

    @cached_property
    def site_configs(self) -> tuple[SiteConfig, ...]:
        """Every tracked site configuration, in path order, with the nav each contributes.

        TRACKED, like `workflows` and for the same reason: a configuration no checkout carries
        builds no site.

        EVERY one of them and not just `SITE_CONFIG`, because `INHERIT` joins navs rather than
        replacing them: a configuration that inherits another and writes a `nav:` of its own adds
        to the inherited list, and the navbar the build renders carries both. So the entries a
        build renders are the UNION across the files. A rule reading one file would pass the
        other's dead entry, and a rule merging them into a single nav would have to know which
        file won; the union needs neither, because an entry is checked where it was written. A
        repo with one configuration reads one, and this costs nothing.

        The join is ZENSICAL's: it merges an inherited configuration with `deepmerge`'s
        `always_merger`, which CONCATENATES lists. mkdocs' own `INHERIT` replaces them, so a repo
        that takes ADR 0001's fallback back to mkdocs has a child nav that wins outright — and
        the union then checks entries no build renders. It would report a dead entry in a file
        that is no longer read, which is a false failure. Revisit this with the generator.
        """
        found: list[SiteConfig] = []
        for rel in self.tracked:
            if not _SITE_CONFIG_RE.match(rel):
                continue
            document = self.config(rel)
            docs_dir = self.resolved_docs_dir(rel)
            targets, unresolved = _nav_targets(docs_dir, document.get("nav"))
            found.append(
                SiteConfig(
                    path=rel,
                    docs_dir=docs_dir,
                    declares_nav="nav" in document,
                    generators=self.page_generators(rel),
                    targets=targets,
                    unresolved=unresolved,
                )
            )
        return tuple(found)

    @cached_property
    def manifest(self) -> dict[str, Any]:
        """`pyproject.toml`, parsed. `load` refused to build this repo unless it parses."""
        return tomllib.loads(self.read("pyproject.toml") or "")

    @cached_property
    def tasks(self) -> frozenset[str]:
        """Every pixi task name the manifest declares, wherever under `[tool.pixi]` it is written.

        Walked rather than read out of `[tool.pixi.tasks]` alone. pixi lets a task be declared on
        the workspace, on a FEATURE, or on a platform target, and this repo's own `docs-build`
        lives on the `docs` feature — so a rule reading one table would fail the docs job of a
        workflow that is entirely correct.
        """
        return frozenset(_task_names(self.manifest.get("tool", {}).get("pixi")))

    @cached_property
    def workflows(self) -> tuple[Workflow, ...]:
        """Every tracked workflow definition, parsed, in path order.

        TRACKED and not globbed off disk: a workflow GitHub never sees is not a workflow, and
        every other rule here reads the same list.

        The rules that read workflows want different halves of one — what triggers it, or what
        its steps invoke — so :class:`Workflow` exposes both and no rule parses YAML itself. A
        file that will not parse, or that is not a mapping, becomes an empty workflow rather than
        an exception: `_TolerantLoader` already shrugs at an unknown tag, and a derived repo's
        unusual workflow must not turn a conformance rule into a crash.
        """
        found: list[Workflow] = []
        for rel in self.tracked:
            if not rel.startswith(WORKFLOW_DIR) or not rel.endswith(WORKFLOW_SUFFIXES):
                continue
            text = self.read(rel)
            if text is None:
                continue
            try:
                loaded = yaml.load(text, Loader=_TolerantLoader)
            except yaml.YAMLError:
                loaded = None
            document: dict[Any, Any] = loaded if isinstance(loaded, dict) else {}
            found.append(
                Workflow(
                    path=rel,
                    name=_string(document.get("name")) or PurePosixPath(rel).stem,
                    triggers=_triggers(document),
                    steps=_steps(rel, document),
                )
            )
        return tuple(found)


def _walk_strings(node: Any) -> Iterator[str]:
    """Every string value in a `nav:` tree, at any depth.

    The template's nav is a flat list of one-key mappings, but a derived repo will nest sections,
    and a nested entry that escaped this walk would be an agent-facing page silently allowed into
    the navbar.
    """
    if isinstance(node, str):
        yield node
    elif isinstance(node, list):
        for item in node:  # pyright: ignore[reportUnknownVariableType]
            yield from _walk_strings(item)
    elif isinstance(node, dict):
        for value in node.values():  # pyright: ignore[reportUnknownVariableType]
            yield from _walk_strings(value)


#: A nav entry that is not a path into the site source. A URL with a scheme, a protocol-relative
#: one, or a bare anchor on the page the reader is already on — none of them names a file, so none
#: is a file that can be missing. The scheme half is what covers `mailto:`, which has no `//`.
_NAV_LINK_RE = re.compile(r"^[a-z][a-z0-9+.\-]*:|^//|^#", re.IGNORECASE)

#: The anchor a nav entry may carry — `guide.md#install` names `guide.md`, and the anchor is a
#: place on it. A bare `#top` never reaches this, being a link by the rule above.
_ANCHOR_RE = re.compile(r"#.*$")

#: What a nav entry has to end in to name a PAGE. Markdown, a notebook where the repo has the
#: plugin that renders one, and an HTML file the builder passes through.
#:
#: An entry ending in anything else names something this cannot resolve and must not guess at:
#: `- API: reference/` is a directory `literate-nav` expands, `- ...` is `awesome-pages` asking
#: for the rest of the tree. Both were measured failing rule 13, and the fix it printed for the
#: second — "add docs/..." — is what a rule looks like outside its domain. A typo is unaffected:
#: `hnad.md` still ends in `.md`.
PAGE_SUFFIXES = (".md", ".ipynb", ".html")

#: The plugins that WRITE PAGES into the site source while the build runs. A nav entry naming one
#: of them resolves to nothing a checkout carries, so rule 13 has no premise in a repo that
#: declares one and reports itself vacuous instead.
#:
#: That replaces a waiver, and the difference is the point. Waivers are per RULE, which is right
#: for an all-or-nothing rule and wrong for this one: a repo with a single generated section had
#: to turn rule 13 off for every hand-written entry it exists to protect, and the waiver then
#: swallowed the genuine dead links too — measured, two problems suppressed where one was real.
PAGE_GENERATORS = frozenset({"gen-files", "literate-nav", "awesome-pages", "macros"})


def _nav_targets(docs_dir: str, nav: Any) -> tuple[dict[str, str], tuple[str, ...]]:
    """One configuration's nav, read apart: the pages it names, and the entries that name none.

    Nav paths are relative to `docs_dir`, so they are prefixed with it before they can be compared
    with anything `git ls-files` said. Getting that wrong is not a false negative that shows up
    later — it makes rule 2 pass vacuously, which is the failure this whole file exists to prevent.

    Rules 2 and 13 read the same mapping, so what counts as a nav path is decided once. `normpath`
    is what lets rule 13 see an entry reaching ABOVE the site source: `../README.md` under `docs/`
    arrives here as `README.md`, outside the directory the builder copies.

    The second half is everything that is neither a link nor a page — the entries a rule should
    say it did not resolve rather than report as missing.
    """
    targets: dict[str, str] = {}
    unresolved: list[str] = []
    for raw in _walk_strings(nav):
        if _NAV_LINK_RE.match(raw):
            continue
        path = _ANCHOR_RE.sub("", raw)
        if not path.endswith(PAGE_SUFFIXES):
            unresolved.append(raw)
            continue
        targets[posixpath.normpath(f"{docs_dir or '.'}/{path}")] = raw
    return targets, tuple(unresolved)


def _plugins(document: dict[str, Any]) -> set[str]:
    """Every plugin name one site configuration declares.

    Both spellings: a LIST, whose items are bare names or single-key mappings carrying options,
    and a MAPPING of name to options. A theme-namespaced name — `material/search` — is that
    theme's copy of the plugin, so the last segment is the name.
    """
    declared = document.get("plugins")
    names: list[Any] = []
    if isinstance(declared, list):
        for item in declared:  # pyright: ignore[reportUnknownVariableType]
            if isinstance(item, str):
                names.append(item)
            elif isinstance(item, dict):
                names.extend(item)  # pyright: ignore[reportUnknownArgumentType]
    elif isinstance(declared, dict):
        names.extend(declared)  # pyright: ignore[reportUnknownArgumentType]
    return {str(name).rsplit("/", 1)[-1] for name in names}


def _task_names(node: Any) -> Iterator[str]:
    """Every key of every `tasks` table at any depth, wherever pixi accepts one.

    One walk rather than four reads: a task may be declared on the workspace, on a feature, on a
    platform target, or on a target under a feature.
    """
    if not isinstance(node, dict):
        return
    for key, value in node.items():  # pyright: ignore[reportUnknownVariableType]
        if key == "tasks" and isinstance(value, dict):
            yield from (str(name) for name in value)  # pyright: ignore[reportUnknownVariableType]
        else:
            yield from _task_names(value)


@dataclass(frozen=True)
class Invocation:
    """One `pixi run` command line, read apart.

    Attributes
    ----------
    task
        The task name — the first token that is neither an option nor an option's value.
    arguments
        Whatever the step passes to that task after `--`, empty for most steps. Kept rather
        than discarded because rule 12 prints them: an argument only CI passes is a legitimate
        thing to write and a thing a reader should be able to see without opening the workflow.
    """

    task: str
    arguments: tuple[str, ...]


def _invoked_task(run: str) -> Invocation | None:
    """Read the pixi task one step's `run:` script invokes, or ``None`` when it runs a command.

    The accepted shape is `pixi run`, any pixi options, the task, and then — optionally — `--`
    and arguments for it. Options are skipped on both sides of `run`, so `pixi run -e test test`
    and `pixi run docs-build` arrive the same way and no spelling is privileged.

    Arguments are ACCEPTED and reported, not forbidden. A flag a workflow wants and a laptop does
    not — `--durations=10` on the CI run of the test suite — is ordinary, and forbidding it only
    moves the divergence into a second task definition, where nothing checks it against the first.
    What the rule can still say is that the command came from the task table.

    Two shapes are still not this one. Tokens after the task that do not start with `--`: pixi
    would read them as its own options and refuse to run, so the step is broken already. And a
    shell operator anywhere after the task — `pixi run build && rm -rf dist` is two commands, and
    the second one is what no `pixi run` does. A script of several lines lands here too.
    """
    script = _EXPRESSION_RE.sub(EXPRESSION, run)
    try:
        tokens = shlex.split(script, comments=True)
    except ValueError:
        # An unbalanced quote. Not a crash and not a pass: whatever it is, it is not this shape.
        return None
    if not tokens or tokens[0] != "pixi":
        return None
    index = _skip_options(tokens, 1)
    if index >= len(tokens) or tokens[index] != "run":
        return None
    index = _skip_options(tokens, index + 1)
    if index >= len(tokens):
        return None
    task, rest = tokens[index], tokens[index + 1 :]
    if rest and rest[0] != ARGUMENT_SEPARATOR:
        return None
    arguments = rest[1:]
    if any(token in SHELL_OPERATORS for token in arguments):
        return None
    return Invocation(task=task, arguments=tuple(arguments))


def _skip_options(tokens: list[str], index: int) -> int:
    """Advance past the options at `index`, taking the value of one that has a separate value."""
    while index < len(tokens) and tokens[index].startswith("-"):
        index += 2 if tokens[index] in PIXI_VALUE_OPTIONS else 1
    return index


def _front_matter(text: str) -> dict[str, Any] | None:
    """Parse a markdown file's YAML front matter, or return ``None`` when it has none."""
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return None
    for index, line in enumerate(lines[1:], start=1):
        if line.strip() in {"---", "..."}:
            loaded = yaml.safe_load("\n".join(lines[1:index]))
            return loaded if isinstance(loaded, dict) else {}
    return None


def _excluded_from_search(text: str) -> bool:
    """Whether a page carries `search: exclude: true`."""
    matter = _front_matter(text)
    if matter is None:
        return False
    search = matter.get("search")
    return isinstance(search, dict) and search.get("exclude") is True


def _vale_sections(text: str) -> list[tuple[str, dict[str, str]]]:
    """`.vale.ini` as (section header, settings) pairs, in file order.

    Hand-parsed rather than handed to `configparser`, which lowercases keys and rejects a
    duplicate header — neither of which is what this file means.
    """
    sections: list[tuple[str, dict[str, str]]] = []
    current: dict[str, str] | None = None
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith(("#", ";")):
            continue
        header = _SECTION_RE.match(stripped)
        if header:
            current = {}
            sections.append((header["header"], current))
            continue
        setting = _SETTING_RE.match(stripped)
        if setting and current is not None:
            current[setting["key"]] = setting["value"]
    return sections


def _glossary_entries(text: str) -> list[tuple[str, int]]:
    """Each `###` entry under `## Glossary` in the glossary, with its word count.

    The cap is per ENTRY and not per file, so a repo with a large vocabulary is not punished for
    the size of its domain. An entry runs from its own heading to the next heading at the same or
    a higher level.
    """
    entries: list[tuple[str, int]] = []
    in_glossary = False
    title: str | None = None
    words = 0
    for line in text.splitlines():
        if line.startswith("## "):
            if title is not None:
                entries.append((title, words))
                title = None
            in_glossary = line[3:].strip().lower() == "glossary"
            continue
        if not in_glossary:
            continue
        if line.startswith("### "):
            if title is not None:
                entries.append((title, words))
            title = line[4:].strip()
            words = len(title.split())
            continue
        if title is not None:
            words += len(line.split())
    if title is not None:
        entries.append((title, words))
    return entries


def _has_table(manifest: dict[str, Any], dotted: str) -> bool:
    """Whether a dotted table — `tool.poetry` — is present in a parsed manifest."""
    node: Any = manifest
    for part in dotted.split("."):
        if not isinstance(node, dict) or part not in node:
            return False
        node = node[part]
    return True


#: A `major.minor` anywhere in a version a tool spells out — `3.13`, `3.13.*`, `>=3.13,<3.14`.
_MINOR_RE = re.compile(r"(?P<major>\d+)\.(?P<minor>\d+)")
#: The lowest version a `requires-python` specifier admits. `<` and `!=` name no floor at all.
_FLOOR_RE = re.compile(r"(?:>=|~=|==)\s*(?P<major>\d+)\.(?P<minor>\d+)")
#: Ruff's spelling: one digit of major, the rest minor, so `py313` is 3.13 and `py39` is 3.9.
_RUFF_TARGET_RE = re.compile(r"^py(?P<major>\d)(?P<minor>\d+)$")

#: Where a repo writes a LANGUAGE LEVEL down, beyond the floor and the pin: the table, the key,
#: the pattern that reads it, and how that tool spells a level back. A repo that adds a tool adds
#: one line here. Trove classifiers are deliberately absent — `Programming Language :: Python ::
#: 3.12` is a list of versions a package supports, not a level, and a library names several.
_LEVEL_SITES: tuple[tuple[tuple[str, ...], str, re.Pattern[str], str], ...] = (
    (("tool", "ruff"), "target-version", _RUFF_TARGET_RE, "py{0}{1}"),
    (("tool", "pyright"), "pythonVersion", _MINOR_RE, "{0}.{1}"),
    (("tool", "mypy"), "python_version", _MINOR_RE, "{0}.{1}"),
)


def _table(root: dict[str, Any], *path: str) -> dict[str, Any]:
    """One nested table of a parsed TOML file, or an empty mapping where the path runs out."""
    node: Any = root
    for key in path:
        node = node.get(key) if isinstance(node, dict) else None
    return node if isinstance(node, dict) else {}


def _version(text: str, pattern: re.Pattern[str]) -> tuple[int, int] | None:
    """Read the (major, minor) a pattern finds in one declaration.

    ``None`` when the pattern finds none, which is a declaration nothing can be held to.
    """
    match = pattern.search(text.strip())
    return (int(match["major"]), int(match["minor"])) if match else None


def _spell(level: tuple[int, int]) -> str:
    """Write a (major, minor) the way a person says it."""
    return f"{level[0]}.{level[1]}"


def _python_pins(pyproject: dict[str, Any]) -> dict[str, str]:
    """Every pixi `python` pin, keyed by the table header that holds it.

    Features are read as well as the default table, because a repo that really supports a range
    gives its floor an environment to resolve in, and rule 11 says so in its notes. A pin may be
    written `python = "3.13.*"` or `python = { version = "3.13.*" }`; both are the same claim.
    """
    tables: set[tuple[str, ...]] = {("tool", "pixi", "dependencies")}
    for name in _table(pyproject, "tool", "pixi", "feature"):
        tables.add(("tool", "pixi", "feature", name, "dependencies"))
    pins: dict[str, str] = {}
    for path in sorted(tables):
        value: Any = _table(pyproject, *path).get("python")
        if isinstance(value, dict):
            value = value.get("version")
        if isinstance(value, str):
            pins[f"[{'.'.join(path)}] python"] = value
    return pins


# --------------------------------------------------------------------------------------
# The rules. Thirteen fail, two warn. Each returns what it found; nothing here exits or prints.
# --------------------------------------------------------------------------------------


def rule_placeholder_rename(repo: Repo) -> Result:
    """1. No tracked file contains the placeholder name."""
    result = Result()
    # The refusal is reported even where the rule is vacuous: a waiver that silently does nothing
    # is the invisible escape hatch this design exists to prevent.
    if UNWAIVABLE in repo.waived:
        result.problems.append(
            _problem(
                f"[tool.liulab.waived] names `{UNWAIVABLE}`, which cannot be waived",
                "a half-renamed repo publishes package metadata and canonical URLs naming a "
                "package that does not exist, and one grep is what proves the rename complete. "
                "The waiver changes nothing; the rule ran anyway",
                f"delete the `{UNWAIVABLE}` line from [tool.liulab.waived]",
            )
        )
    if repo.exists(INIT_SKILL):
        result.notes.append(
            f"not checked: {INIT_SKILL}/ is here, so `init-repo` has not run and "
            f"`{PLACEHOLDER}` is still this repo's own name"
        )
        return result
    for rel in repo.tracked:
        path = repo.root / rel
        if PLACEHOLDER in rel:
            where = "its path"
        elif path.is_symlink():
            where = "its link target" if PLACEHOLDER in str(path.readlink()) else ""
        elif path.is_file() and PLACEHOLDER.encode() in path.read_bytes():
            where = "its contents"
        else:
            where = ""
        if where:
            result.problems.append(
                _problem(
                    f"{rel} still names `{PLACEHOLDER}` in {where}",
                    "one grep proves the rename complete. A leftover copy means an import, a "
                    "distribution name or a site_url still points at a package that does not "
                    "exist, and a redirect can hide that for months",
                    "run `init-repo`, or rename it by hand and re-run this check",
                )
            )
    result.notes.append(f"{len(repo.tracked)} tracked path(s) read for `{PLACEHOLDER}`")
    return result


def rule_agent_docs_unpublished(repo: Repo) -> Result:
    """2. Agent-facing pages the site would publish stay out of the navbar and out of search."""
    result = Result()
    prefix = f"{repo.docs_dir}/"
    # Scoped to the agent-docs keys UNDER THE SITE SOURCE. `AGENTS.md`, `GLOSSARY.md` and
    # `skills/` are agent-facing too, but the site never publishes them: front matter there means
    # nothing, and on a SKILL.md it lands in the frontmatter a skill loader reads.
    keys = sorted(k for k in repo.agent_docs if k.startswith(prefix))
    if not keys:
        result.notes.append(f"not checked: no [tool.liulab.agent-docs] key is under {prefix}")
        return result
    pages = sorted({p for key in keys for p in repo.under(key) if p.endswith(".md")})
    if not pages:
        result.notes.append(f"not checked: no markdown file is under {', '.join(keys)}")
        return result
    for page in pages:
        text = repo.read(page)
        if text is not None and not _excluded_from_search(text):
            result.problems.append(
                _problem(
                    f"{page} has no `search: exclude: true` front matter",
                    "an unlisted page is still indexed, so it turns up in the site's search box "
                    "for a human who was looking for the documentation",
                    "add this to the top of the file:\n"
                    "               ---\n"
                    "               search:\n"
                    "                 exclude: true\n"
                    "               ---",
                )
            )
        if page in repo.nav:
            result.problems.append(
                _problem(
                    f"{page} appears in mkdocs.yml nav as `{repo.nav[page]}`",
                    "nav is the navbar, and this page is written for an agent. Front matter "
                    "does nothing about the navbar and nav does nothing about search, which is "
                    "why both are required",
                    f"delete `{repo.nav[page]}` from the nav: list in mkdocs.yml. The page stays "
                    "reachable by URL",
                )
            )
    result.notes.append(f"{len(pages)} markdown file(s) under {', '.join(keys)}")
    return result


def rule_vale_length_sections(repo: Repo) -> Result:
    """3. `.vale.ini` gives every agent-docs key a section that turns on its declared cap."""
    result = Result()
    text = repo.read(".vale.ini")
    if text is None:
        result.problems.append(
            _problem(
                ".vale.ini is missing",
                "the declared word caps are enforced by Vale and by nothing else, so without "
                "this file every agent-docs key is checked by nothing",
                "restore .vale.ini from the template",
            )
        )
        return result
    sections = _vale_sections(text)
    # The Length rules the repo actually declares, taken from the table rather than hardcoded, so
    # adding a third cap needs no edit here.
    caps = sorted({v for v in repo.agent_docs.values() if isinstance(v, str)})
    for key, declared in sorted(repo.agent_docs.items()):
        first_glob = next((i for i, ch in enumerate(key) if ch in GLOB_CHARS), None)
        if first_glob is not None:
            # A glob in an earlier segment would widen the prefix past the directories it named.
            in_last_segment = "/" not in key[first_glob:].rstrip("/")
            plain = key[: key.rfind("/", 0, first_glob) + 1] if in_last_segment else ""
            result.problems.append(
                _problem(
                    f"`{key}` is a glob, and agent-docs keys are plain path prefixes",
                    "rule 2 matches keys as prefixes, so a glob matches no file and its pages "
                    "are checked by nothing, while its .vale.ini section still looks paired",
                    f"use `{plain}`, or one key per directory it was meant to cover"
                    if plain
                    else "use one plain key per directory it was meant to cover",
                )
            )
            continue
        matched = [(header, settings) for header, settings in sections if key in header]
        if len(matched) != 1:
            result.problems.append(
                _problem(
                    f"`{key}` is spelled in {len(matched)} .vale.ini section header(s), not 1",
                    "conformance pairs a key with its section by looking for the key inside the "
                    "header, which is exact only while each key appears in exactly one. A key in "
                    "none is checked by nothing; a key in two cannot be paired",
                    f"give `{key}` one section header that spells it verbatim, as the comment at "
                    "the top of .vale.ini asks",
                )
            )
            continue
        header, settings = matched[0]
        for cap in caps:
            setting = f"Lab.{cap}"
            wanted = VALE_ON if cap == declared else VALE_OFF
            actual = settings.get(setting, "").upper()
            if actual in wanted:
                continue
            state = "on" if cap == declared else "off"
            saw = f"`{settings[setting]}`" if setting in settings else "nothing"
            result.problems.append(
                _problem(
                    f"[{header}] should set {setting} {state} for `{key}`, and says {saw}",
                    "Vale's `*` matches `/`, so more than one section matches the same file and "
                    "per-rule settings ACCUMULATE, later sections winning. An omitted rule is "
                    "not default-on: it keeps whatever an earlier section said, which can leave "
                    "these files checked by nothing, with zero alerts and green CI",
                    f"write `{setting} = {'YES' if state == 'on' else 'NO'}` in that section. "
                    "Every rule is named in every section for this reason",
                )
            )
    result.notes.append(
        f"{len(repo.agent_docs)} agent-docs key(s) against {len(sections)} .vale.ini section(s)"
    )
    return result


def rule_glossary_format(repo: Repo) -> Result:
    """4. The glossary is `GLOSSARY.md`, and no entry in it runs past 200 words."""
    cap = 200
    result = Result()
    if repo.exists(GLOSSARY_MAP):
        result.notes.append(
            f"not checked: {GLOSSARY_MAP} is here, so the glossary is split by context into "
            "files this rule does not read"
        )
        return result
    for before, after in GLOSSARY_RENAMES:
        if repo.exists(before) and not repo.exists(after):
            result.problems.append(
                _problem(
                    f"{before} is still here, and {after} is not",
                    f"the upstream skills read and write {after} and nothing else, so they find "
                    "no glossary here and start a second one beside this",
                    f"`git mv {before} {after}`, with the entries unchanged",
                )
            )
    text = repo.read(GLOSSARY)
    if text is None:
        if not result.problems:
            result.notes.append(f"not checked: no {GLOSSARY}")
        return result
    for number, line in enumerate(text.splitlines(), start=1):
        upstream = _UPSTREAM_ENTRY_RE.match(line)
        if upstream:
            result.problems.append(
                _problem(
                    f"{GLOSSARY} line {number} is an upstream-format entry, `{line.strip()}`",
                    "a lab glossary entry is a `###` heading under `## Glossary`, and only that "
                    "shape is capped. A term line is one this rule cannot measure, so a glossary "
                    "written this way would pass with every entry unchecked",
                    f"make it `### {upstream['term'].strip()}` under `## Glossary`, with the "
                    "definition below it",
                )
            )
    entries = _glossary_entries(text)
    if not entries:
        if not result.problems:
            result.notes.append(f"not checked: {GLOSSARY} has no entries under `## Glossary`")
        return result
    for title, words in entries:
        if words > cap:
            result.problems.append(
                _problem(
                    f"{GLOSSARY} glossary entry `{title}` is {words} words, over {cap}",
                    "the glossary says what a word MEANS in this repo. An entry that long is an "
                    "explanation, and it belongs in a document the glossary can point at",
                    "cut it to a definition and move the rest to docs/, or to an ADR if it is a "
                    "decision",
                )
            )
    longest = max(words for _, words in entries)
    result.notes.append(f"{len(entries)} glossary entry(s), longest {longest} of {cap} words")
    return result


def rule_skill_file_location(repo: Repo) -> Result:
    """5. Every `SKILL.md` sits at `skills/<name>/SKILL.md`."""
    result = Result()
    found = [p for p in repo.tracked if PurePosixPath(p).name == "SKILL.md"]
    for rel in found:
        if not _SKILL_FILE_RE.match(rel):
            result.problems.append(
                _problem(
                    f"{rel} is a SKILL.md outside skills/<name>/",
                    "the installer discovers skills/*/SKILL.md and nothing else, so a skill "
                    "written anywhere else sits there unnoticed by every agent product",
                    "move it to skills/<name>/SKILL.md, then run "
                    "`python skills/install.py --target all`",
                )
            )
    result.notes.append(f"{len(found)} tracked SKILL.md")
    return result


def _shipped_skills(home: Path) -> frozenset[str] | None:
    """Every skill name the installed `lab` plugin ships, casefolded, or ``None`` if none is found.

    Read from the marketplace clone, where each directory holding a `SKILL.md` is one skill. An
    empty or absent directory is ``None`` and never an empty set: an empty set would let every
    name through, which is the pass this rule must not give when it knows nothing.
    """
    found = frozenset(
        skill.parent.name.casefold() for skill in (home / PLUGIN_SKILLS).glob("*/SKILL.md")
    )
    return found or None


def rule_skill_name_clash(repo: Repo) -> Result:
    """6. No repo-local skill takes a name the `lab` plugin ships."""
    result = Result()
    names: dict[str, str] = {}
    for rel in repo.tracked:
        for pattern in _SKILL_DIR_RES:
            match = pattern.match(rel)
            if match:
                names.setdefault(match["name"], rel)
                break
    try:
        shipped = _shipped_skills(Path.home())
    except RuntimeError:  # no HOME and no password entry: nowhere to look
        shipped = None
    for name, rel in sorted(names.items()):
        if name.casefold().startswith(LAB_PREFIX) or name.casefold() in (shipped or ()):
            result.problems.append(
                _problem(
                    f"{rel} is a repo-local skill named `{name}`",
                    f"the `lab` plugin ships this name or owns it, as it owns every `{LAB_PREFIX}` "
                    "name. A repo-local skill of the same name replaces the plugin's for "
                    "everyone working here — silently, and in Kimi Code even when only the case "
                    "differs",
                    "rename it to a name the plugin does not ship, then run "
                    "`python skills/install.py --target all`",
                )
            )
    if shipped is None:
        result.notes.append(
            f"not checked: no `lab` plugin skills under ~/{PLUGIN_SKILLS}, so the "
            f"{len(names)} skill name(s) here were held to the `{LAB_PREFIX}` prefix alone"
        )
    else:
        result.notes.append(
            f"{len(names)} skill director(ies) named across the discovery paths, against the "
            f"{len(shipped)} name(s) the `lab` plugin ships"
        )
    return result


def rule_skill_symlinks(repo: Repo) -> Result:
    """7. `python skills/install.py --check` passes."""
    result = Result()
    installer = "skills/install.py"
    if not repo.exists(installer):
        result.notes.append(f"not checked: no {installer}")
        return result
    # DELEGATED, not reimplemented: the command that reports a broken link is the command that
    # repairs it, and its own three invariants are not restated here to drift from.
    proc = subprocess.run(
        [sys.executable, installer, "--check"],
        cwd=repo.root,
        capture_output=True,
        text=True,
        check=False,
    )
    if proc.returncode != 0:
        detail = (proc.stdout + proc.stderr).strip() or f"exit {proc.returncode}, no output"
        result.problems.append(
            _problem(
                f"`python {installer} --check` failed:",
                "it holds the two committed discovery paths to three invariants — a link in "
                "both, nothing dangling, every link relative. Its own report follows",
                "run the command it names below",
            )
            + "\n"
            + textwrap.indent(detail, "      ")
        )
    else:
        result.notes.append((proc.stdout.strip() or "ok").splitlines()[-1])
    return result


def rule_repo_shape(repo: Repo) -> Result:
    """8. If there is a package there are tests; if there is a release workflow there is a log."""
    result = Result()
    # Named once, at RELEASE_WORKFLOW, because rule 9 keys on the same file.
    release = RELEASE_WORKFLOW
    # Both halves are CONDITIONAL, and an absent premise is vacuous rather than failing: a repo
    # with no package is not a repo that failed to have tests. That is the whole shape of the
    # rule, and it is why the notes below say which premise was absent.
    if not repo.tracked_under("src/"):
        result.notes.append("not checked: no tracked src/, so tests/ is not required")
    elif not repo.tracked_under("tests/"):
        result.problems.append(
            _problem(
                "src/ is tracked and tests/ is not",
                "a package with no test directory has nothing pytest can find, so the `test` "
                "step of `pixi run check` passes having run nothing",
                "add tests/, or delete src/ if this repo has no package",
            )
        )
    else:
        result.notes.append("src/ is tracked and so is tests/")
    if release not in repo.tracked:
        result.notes.append(f"not checked: no {release}, so CHANGELOG.md is not required")
    elif "CHANGELOG.md" not in repo.tracked:
        result.problems.append(
            _problem(
                f"{release} is tracked and CHANGELOG.md is not",
                "this repo publishes releases, and the people who install it from git have "
                "nowhere to read what changed",
                "add CHANGELOG.md. `init-repo` never subtracts it, so it was deleted by hand",
            )
        )
    else:
        result.notes.append(f"{release} is tracked and so is CHANGELOG.md")
    return result


def _matches(patterns: Any, ref: str) -> bool | None:
    """Whether a GitHub ref filter matches one ref, or ``None`` where it cannot be read.

    The filter syntax is `fnmatch` plus a leading `!` that excludes, and the LAST pattern to
    match decides. That is the whole of the semantics and the whole of this function.

    The two places the dialects differ cannot change this answer for `FIRST_TAG`. GitHub's `*`
    stops at a `/` where `fnmatch`'s does not, and `v0.0.0` has no `/`. GitHub's `+` is a
    quantifier that `fnmatch` reads as a literal `+`, which can only turn a match into a miss —
    and a miss is the answer that lets a workflow through, never the one that fails a correct
    one.

    ``None`` is what a caller cannot clear a workflow on: a filter written as something other
    than strings is one this cannot read, and every caller reads that as the hazard.
    """
    if isinstance(patterns, str):
        patterns = [patterns]
    if not isinstance(patterns, list) or not patterns:
        return None
    matched = False
    for pattern in patterns:  # pyright: ignore[reportUnknownVariableType]
        if not isinstance(pattern, str):
            return None
        if fnmatch.fnmatchcase(ref, pattern.removeprefix("!")):
            matched = not pattern.startswith("!")
    return matched


def _first_tag_events(workflow: Workflow) -> list[str]:
    """List the triggers in one workflow that pushing `FIRST_TAG` fires, spelled as written.

    Two of them are the same mistake — an absent filter is not "no refs", it is EVERY ref — and
    they have no legitimate form, so they are listed whenever they appear:

    - `push:` naming no branch filter at all, which fires on a tag exactly as on a branch.
      `paths:` narrows which files, never which refs.
    - `create`, which fires on a branch or a tag coming into existence, and a pushed tag is a
      tag coming into existence.

    A `tags:` or `tags-ignore:` filter is the other shape, and it is a DECISION rather than an
    accident: someone wrote down which tags reach this workflow. So it is READ, not refused, and
    listed only where it lets `FIRST_TAG` through. Under CalVer `vYYYY.M.PATCH` a filter like
    `tags: ['v20*']` cannot match `v0.0.0`, and such a workflow is not listed here.

    A `push:` that names `branches:` or `branches-ignore:` and no tag filter is a branch trigger
    and is absent from this list: naming any branch filter is what stops tags reaching it.
    """
    fired: list[str] = []
    if "push" in workflow.triggers:
        config = workflow.triggers["push"]
        config = config if isinstance(config, dict) else {}
        if "tags" in config:
            # Read even where `branches:` is also written: the two filters are a union, so a
            # branch filter beside a tag filter narrows nothing about tags.
            if _matches(config["tags"], FIRST_TAG) is not False:
                fired.append(f"`on: push:` has a `tags:` filter that admits `{FIRST_TAG}`")
        elif "tags-ignore" in config:
            # The same call inverted. `tags-ignore:` fires on every tag its list does NOT match,
            # so the hazard is gone only where the list provably matches `FIRST_TAG`.
            if _matches(config["tags-ignore"], FIRST_TAG) is not True:
                fired.append(
                    f"`on: push:` has a `tags-ignore:` filter that does not exclude `{FIRST_TAG}`"
                )
        elif not ("branches" in config or "branches-ignore" in config):
            fired.append("`on: push:` names no branch filter, so it fires on every ref, tags too")
    if "create" in workflow.triggers:
        fired.append("`on: create:` fires when a tag comes into existence")
    return fired


def rule_release_trigger(repo: Repo) -> Result:
    """9. Nothing a `FIRST_TAG` push fires can trigger the publishing workflow.

    KNOWN LIMIT, stated rather than closed: this reads one path, `RELEASE_WORKFLOW`, so the same
    trigger in a workflow under any other name is outside the rule and `git mv` clears it. The
    path is not a guess — a package index binds its trusted publisher to a workflow FILENAME, so
    renaming the file is a change to external configuration and not a way around a check — but a
    repo that publishes under another name is a repo this rule says nothing about. Closing that
    would mean reading every workflow's steps for whatever action publishes, which is a list
    nobody can keep, on every repo made from here. The narrow rule is the honest one.
    """
    result = Result()
    publishing = [w for w in repo.workflows if w.path == RELEASE_WORKFLOW]
    if not publishing:
        result.notes.append(
            f"not checked: no {RELEASE_WORKFLOW}. This rule reads that one path — the filename a "
            "package index binds a trusted publisher to — so a publishing workflow under any "
            "other name is outside it"
        )
        return result
    for workflow in publishing:
        for fired in _first_tag_events(workflow):
            result.problems.append(
                _problem(
                    f"{workflow.path} can be triggered by a tag push: {fired}",
                    f"`init-repo` pushes `{FIRST_TAG}` on a repo's opening day and a "
                    "`git push --tags` pushes every tag at once, so this trigger runs the "
                    "publishing workflow with nobody having decided to release anything. "
                    "Publishing a GitHub Release is that decision, written down",
                    "trigger it on `release:` with `types: [published]`, which is a person "
                    "publishing a GitHub Release on purpose, and add `workflow_dispatch:` for "
                    "re-running one that failed",
                )
            )
    events = ", ".join(sorted(event for w in publishing for event in w.triggers))
    result.notes.append(f"{RELEASE_WORKFLOW} is triggered by {events or 'nothing'}")
    return result


def rule_single_toolchain(repo: Repo) -> Result:
    """10. No competing resolver has been run here: pixi and the manifest are the only ones.

    A LOCKFILE or a resolver's table, and nothing wider. The rule was cut back to that category
    after four of its eight markers were measured firing on correct work — a Colab notebook's
    `requirements.txt`, a `binder/environment.yml`, a `setup.py` that builds a C extension and
    declares no dependency, and a pre-commit hook that only invokes `pixi run`. Each of those is
    a file someone reads or a build someone needs, and the rule's advice for all four was to
    delete it.

    It says nothing about a root `requirements.txt` any more, not even a warning. A researcher
    whose notebook runs on Colab needs that file, cannot run pixi as the kernel, and would get
    nothing from being told about it on every run.
    """
    result = Result()
    # One why for every marker, because it is one failure: the class, not the tool.
    why = (
        "a second resolver is set up to build this repo's environment, and it resolves against "
        "its own inputs: what it installs and what the gate runs in can differ while both look "
        "correct, and nothing reports it. pyproject.toml is the single source of truth for "
        "dependencies, environments and tasks, and pixi.lock is the only lock read here"
    )
    for marker in SECOND_TOOLCHAINS:
        if marker.name:
            for rel in repo.tracked:
                if re.fullmatch(marker.name, PurePosixPath(rel).name):
                    result.problems.append(
                        _problem(
                            f"{rel} belongs to {marker.tool}, a resolver other than pixi",
                            why,
                            f"delete {rel} and declare what it pinned in pyproject.toml — "
                            "`[tool.pixi.dependencies]` for a package, `[tool.pixi.tasks]` for a "
                            "command — then run `pixi install`",
                        )
                    )
        if marker.table and _has_table(repo.manifest, marker.table):
            result.problems.append(
                _problem(
                    f"pyproject.toml has [{marker.table}], which configures {marker.tool}",
                    why,
                    f"delete [{marker.table}] and every table under it, declare what it pinned "
                    "under `[tool.pixi.dependencies]`, then run `pixi install`",
                )
            )
    # This rule is never vacuous — the list is always there and so is the manifest — but a run
    # still has to say WHICH resolvers it knew to look for, or a green means nothing.
    names = ", ".join(marker.tool for marker in SECOND_TOOLCHAINS)
    result.notes.append(
        f"{len(SECOND_TOOLCHAINS)} competing resolver(s) looked for anywhere in "
        f"{len(repo.tracked)} tracked path(s) and in pyproject.toml: {names}"
    )
    return result


def rule_python_version_agreement(repo: Repo) -> Result:
    """11. Every declared Python version agrees with the floor and with the pinned interpreter."""
    result = Result()
    # AGREEMENT, never a count. A repo supporting a range of Pythons declares a floor below its
    # pin and holds its tools to the floor, and a rule that counted declarations would fail it —
    # which would make this template unusable for a library. What cannot legitimately differ is
    # what the declarations SAY.
    pins = _python_pins(repo.manifest)
    pinned: dict[str, tuple[int, int]] = {}
    for where, spelling in pins.items():
        level = _version(spelling, _MINOR_RE)
        if level is not None:
            pinned[where] = level
    if not pinned:
        if pins:
            spelled = ", ".join(f"{where} is `{pins[where]}`" for where in sorted(pins))
            reason = f"no pixi `python` pin names a minor version: {spelled}"
        else:
            reason = "no `python` in [tool.pixi.dependencies] or in any pixi feature"
        result.notes.append(
            f"not checked: {reason} — this repo names no interpreter for the other declarations "
            "to agree with"
        )
        return result

    requires = _table(repo.manifest, "project").get("requires-python")
    requires = requires if isinstance(requires, str) else None
    floor = _version(requires, _FLOOR_RE) if requires is not None else None
    lowest = min(pinned.values())
    for where, level in sorted(pinned.items()):
        if floor is not None and level < floor:
            result.problems.append(
                _problem(
                    f"{where} `{pins[where]}` pins {_spell(level)}, below the "
                    f"[project] requires-python floor `{requires}`",
                    "the repo runs an interpreter its own package metadata says it does not "
                    "support, so every gate is green on a version pip would refuse to install on",
                    f"raise the pin to {_spell(floor)}, or lower requires-python to "
                    f">={_spell(level)}",
                )
            )

    # The language level a tool holds the code to is the OLDEST interpreter the repo supports:
    # the floor where one is declared, and otherwise the lowest thing pinned.
    if floor is not None:
        wanted, source = floor, f"[project] requires-python `{requires}`"
    else:
        wanted = lowest
        at_lowest = next(where for where, level in sorted(pinned.items()) if level == lowest)
        source = f"{at_lowest} `{pins[at_lowest]}`"
    declared = [f"[project] requires-python `{requires}`"] if requires is not None else []
    declared += [f"{where} `{pins[where]}`" for where in sorted(pinned)]
    for path, key, pattern, spelling in _LEVEL_SITES:
        table = _table(repo.manifest, *path)
        if key not in table:
            continue
        where = f"[{'.'.join(path)}] {key}"
        said = str(table[key])
        declared.append(f"{where} `{said}`")
        level = _version(said, pattern)
        if level is None:
            result.problems.append(
                _problem(
                    f"{where} is `{said}`, which names no Python version",
                    "a level this check cannot read is a level nothing can hold to the floor, and "
                    f"{path[-1]} may well be reading it as something else again",
                    f"write it the way {path[-1]} spells a version, or delete the key",
                )
            )
            continue
        if level != wanted:
            result.problems.append(
                _problem(
                    f"{where} `{said}` says {_spell(level)}, and {source} says {_spell(wanted)}",
                    "this is the language level the tool holds the code to, and it has to be the "
                    "oldest interpreter the repo supports. Above the floor it lets through syntax "
                    "that fails on a version the package claims to install on; below it the tool "
                    "rejects code the repo is allowed to write",
                    f'write `{key} = "{spelling.format(*wanted)}"`, or delete the key — with no '
                    f"{key} the level is derived from the floor and the pin",
                )
            )

    result.notes.append(f"{len(declared)} declaration(s): {'; '.join(declared)}")
    # The one thing this rule CANNOT see. A floor below every pin is either a range the repo
    # deliberately supports or a floor nobody lowered when the pin moved up, and the declarations
    # read identically in both cases. Saying so is the point: a rule that quietly cannot tell them
    # apart is worse than one that reports which it checked.
    if floor is not None and floor < lowest:
        result.notes.append(
            f"not checked: whether {_spell(floor)} is ever resolved or tested. Nothing here pins "
            f"it, so a range this repo supports on purpose and a floor left behind by a raised "
            f"pin look the same — pin {_spell(floor)} in a pixi feature to make the claim real"
        )
    elif floor is not None and len(set(pinned.values())) > 1:
        result.notes.append(f"the floor {_spell(floor)} is itself pinned, so pixi resolves it")
    return result


def rule_workflow_step_tasks(repo: Repo) -> Result:
    """12. Every workflow step that runs anything invokes a task the manifest declares.

    What this verifies is that the command a step runs is DECLARED — its text lives in the task
    table, where a person can read it and a laptop can run it. It is not a guarantee that CI and
    a laptop run the same thing, and it was once written down as one. The known limits, none of
    them closed here:

    - only the `run:` line is read. `env:` at any of three scopes, `working-directory:`, `shell:`
      and `--manifest-path` all change what a step does without changing that line;
    - a step that `uses:` a local composite action runs whatever that action's steps run, and
      reading those means parsing `.github/actions/**`;
    - a task may pass arguments after `--`. They are printed in the notes rather than forbidden.

    Closing any of them means parsing foreign files in a check that ships to every lab repo, for
    a guarantee no wording here can make true. Say the limits instead.
    """
    result = Result()
    # `uses:` steps are not examined at all, and that is the rule and not an exemption: an action
    # is a dependency the workflow pulls in, not a step of this repo's own build, and there is no
    # task for it to have been.
    running = [step for w in repo.workflows for step in w.steps if step.run is not None]
    if not running:
        result.notes.append("not checked: no tracked workflow has a step that runs a command")
        return result
    unresolved = 0
    passing: list[str] = []
    for step in running:
        where = f"{step.workflow} job `{step.job}` step {step.index}"
        if step.name:
            where += f" ({step.name})"
        invocation = _invoked_task(step.run or "")
        if invocation is None:
            result.problems.append(
                _problem(
                    f"{where} runs a command rather than `{PIXI_RUN} <task>`",
                    "the step list lives in the task table so that what CI runs is something a "
                    "laptop can run too. A step that spells out a command runs something no "
                    "local `pixi run` does, and both stay green while they quietly stop being the "
                    "same claim — until someone notices the two lists disagree, months later",
                    f"declare what it runs as a task in {TASK_TABLE} and make the step "
                    f"`{PIXI_RUN} <that task>`. An argument only CI passes goes after "
                    f"`{ARGUMENT_SEPARATOR}`",
                )
            )
            continue
        if invocation.arguments:
            passing.append(f"{where} -> {shlex.join(invocation.arguments)}")
        if EXPRESSION in invocation.task:
            unresolved += 1
        elif invocation.task not in repo.tasks:
            result.problems.append(
                _problem(
                    f"{where} invokes `{invocation.task}`, which this repo declares no task by",
                    "a step naming a task nobody declared cannot run at all, and the workflow "
                    "that finds out is often one that runs rarely — the publish, the scheduled "
                    "job — so the break surfaces on the day it costs the most. It reads as if it "
                    "were following the convention, which is what makes it worse than a command",
                    f"declare `{invocation.task}` in pyproject.toml under {TASK_TABLE}, or point "
                    "the step at a task that is declared",
                )
            )
    result.notes.append(
        f"{len(running)} step(s) that run a command, across {len(repo.workflows)} workflow(s), "
        f"against {len(repo.tasks)} declared task(s)"
    )
    # Printed on every run that has one, green runs included. An argument a workflow passes and a
    # laptop does not is a real difference between the two, and the rule no longer forbids it —
    # so the one thing left to do about it is make it visible without opening the workflow.
    if passing:
        result.notes.append(
            f"{len(passing)} step(s) pass arguments to their task: {'; '.join(passing)}"
        )
    if unresolved:
        result.notes.append(
            f"{unresolved} step(s) name their task with a `" + EXPRESSION + "` expression, which "
            "GitHub resolves when the job runs and this cannot"
        )
    return result


def rule_nav_target_exists(repo: Repo) -> Result:
    """13. Every `nav:` entry that names a page names a tracked one, under the site source.

    TRACKED and not merely present, because CI builds from a checkout.

    Two shapes are outside it rather than waived. An entry that names no page at all — a
    directory, an `...` — is reported unresolved, because a rule that cannot tell what a menu
    item points at cannot say the page is missing. And a configuration declaring a plugin that
    WRITES pages during the build has no premise here at all: nothing it names is in a checkout,
    so the rule reports itself vacuous rather than failing every generated entry.
    """
    result = Result()
    if not repo.site_configs:
        result.notes.append(
            f"not checked: no tracked {SITE_CONFIG}, so this repo publishes no site"
        )
        return result
    tracked = set(repo.tracked)
    for config in repo.site_configs:
        # A file with no `nav:` at all has not passed: the generator builds the navbar from the
        # directory tree instead, and there is no entry to resolve. Reported per configuration,
        # so a repo whose second config only overrides the identity says so rather than looking
        # like one whose nav was checked.
        if not config.declares_nav:
            result.notes.append(
                f"not checked: {config.path} declares no `nav:`, so the site builds its navbar "
                "from the directory tree and names no page for this rule to resolve"
            )
            continue
        # A page-generating plugin is the shape that used to need a waiver. The pages it writes
        # exist after the build and never in the checkout this reads, so the rule has no premise
        # rather than a repo to fail — and saying so leaves the waiver table free for a decision
        # somebody actually made.
        if config.generators:
            result.notes.append(
                f"not checked: {config.path} declares {', '.join(config.generators)}, which "
                "writes pages into the site source while the build runs, so a nav entry may name "
                "a page no checkout carries"
            )
            continue
        prefix = f"{config.docs_dir}/" if config.docs_dir not in {"", "."} else ""
        for target, raw in sorted(config.targets.items()):
            # OUTSIDE the site source first, because such a target usually does exist — and a
            # file that is right there is the case a "does it exist" test calls fine. It is the
            # same broken menu item, so it is the same rule, with the fix it actually needs.
            if prefix and not target.startswith(prefix):
                result.problems.append(
                    _problem(
                        f"{config.path} nav entry `{raw}` resolves to {target}, outside {prefix}",
                        "a nav path is read relative to the site source, and the builder renders "
                        "that directory and nothing else. A target above it is not a page the "
                        "site has, so the menu item 404s exactly as a missing file does — and the "
                        "file being right there is what makes it read as correct",
                        f"move the page under {prefix}, or write the entry as a URL if it is a "
                        "link out rather than a page of this site",
                    )
                )
            elif target not in tracked:
                # A file that is present but untracked builds on the laptop that wrote it and
                # nowhere else. Same failure, and worth saying apart, because "it is right there"
                # is exactly what the person reading this will be about to say.
                loose = (
                    " The file is present on disk but untracked, and CI builds from a checkout."
                    if repo.exists(target)
                    else ""
                )
                result.problems.append(
                    _problem(
                        f"{config.path} nav entry `{raw}` names {target}, which is not tracked",
                        "the site generator does not validate the nav at all: the build reports "
                        "no issues, exits 0, and publishes a menu item whose link points at a "
                        f"page that was never rendered.{loose} No build setting turns this on, "
                        "which is why the check is here",
                        f"add {target}, correct the entry, or delete it from the `nav:` list in "
                        f"{config.path}",
                    )
                )
        result.notes.append(
            f"{config.path}: {len(config.targets)} nav entry(s) against {prefix or 'the repo root'}"
        )
        # Said out loud rather than passed over. These are the entries the rule declined to
        # resolve, and a reader who expected one of them to be checked should find out here.
        if config.unresolved:
            result.notes.append(
                f"{config.path}: {len(config.unresolved)} nav entry(s) name no page and were not "
                f"resolved: {', '.join(config.unresolved)}"
            )
    return result


def warning_init_sentinel(repo: Repo) -> Result:
    """14. `AGENTS.md` still carries the line telling you to run `/init`."""
    result = Result()
    if repo.exists(INIT_SKILL):
        result.notes.append(f"not checked: {INIT_SKILL}/ is here, and that skill is the nag")
        return result
    text = repo.read("AGENTS.md")
    if text is None:
        result.notes.append("not checked: no AGENTS.md")
        return result
    # The sentinel is a blockquote line naming `/init`. It is phrased as an instruction and it
    # says to delete itself, so replacing the file is what makes this warning stop — "done" needs
    # no separate step. This WARNS and never fails: a new repo must not start red for a task its
    # owner is allowed to defer.
    if any(line.lstrip().startswith(">") and "/init" in line for line in text.splitlines()):
        result.problems.append(
            _problem(
                "AGENTS.md still carries the `/init` sentinel",
                "the file is the template's generic copy: it says how ANY Liu Lab repo works, "
                "not what this one does, and an agent that reads it will act on a description "
                "that was never true here",
                "run `/init`, then delete the sentinel line. This is a warning, not a failure",
            )
        )
    else:
        result.notes.append("AGENTS.md no longer carries it")
    return result


@dataclass(frozen=True)
class Rule:
    """One rule, its number, and the one-line statement printed when it fails."""

    number: int
    name: str
    statement: str
    check: Callable[[Repo], Result]
    warns_only: bool = False


RULES: tuple[Rule, ...] = (
    Rule(
        1,
        UNWAIVABLE,
        f"no tracked file contains `{PLACEHOLDER}` — the one grep that proves the rename "
        f"complete. Checked only once {INIT_SKILL}/ is gone, and never waivable",
        rule_placeholder_rename,
    ),
    Rule(
        2,
        "agent-docs-unpublished",
        "every markdown file under an agent-docs key below the site source carries "
        "`search: exclude: true` and appears in no nav: entry",
        rule_agent_docs_unpublished,
    ),
    Rule(
        3,
        "vale-length-sections",
        ".vale.ini has one section per agent-docs key, turning its declared Length rule on and "
        "every other Length rule off",
        rule_vale_length_sections,
    ),
    Rule(
        4,
        "glossary-format",
        f"the glossary is {GLOSSARY}, never a CONTEXT.md left unrenamed; each entry is a `###` "
        "heading under `## Glossary`, never an upstream `**Term**:` line, and none exceeds 200 "
        f"words. Where a {GLOSSARY_MAP} splits it by context, it is reported unchecked",
        rule_glossary_format,
    ),
    Rule(
        5,
        "skill-file-location",
        "every SKILL.md lives at skills/<name>/SKILL.md, where the installer looks",
        rule_skill_file_location,
    ),
    Rule(
        6,
        "skill-name-clash",
        f"no repo-local skill takes a name the `lab` plugin ships: no `{LAB_PREFIX}` name, and no "
        f"name in the plugin's marketplace clone under ~/{PLUGIN_SKILLS}. Where no clone is "
        "found the prefix alone is held, and the rule says it did not check the rest",
        rule_skill_name_clash,
    ),
    Rule(
        7,
        "skill-symlinks",
        "`python skills/install.py --check` passes: a link in both discovery paths, nothing "
        "dangling, every link relative",
        rule_skill_symlinks,
    ),
    Rule(
        8,
        "repo-shape",
        "if src/ is tracked then tests/ is too; if release.yml is tracked then CHANGELOG.md is",
        rule_repo_shape,
    ),
    Rule(
        9,
        "release-trigger",
        f"{RELEASE_WORKFLOW} names no trigger a `{FIRST_TAG}` push fires — not a `push:` with no "
        "branch filter, not `create`, and not a `tags:` filter that admits it. A tag filter that "
        f"cannot match `{FIRST_TAG}` is read and allowed; this rule reads that one path only",
        rule_release_trigger,
    ),
    Rule(
        10,
        "single-toolchain",
        "no competing resolver is set up here — no poetry.lock, uv.lock, pdm.lock or Pipfile "
        "anywhere in the tree, and no [tool.poetry], [tool.uv] or [tool.pdm] in pyproject.toml. "
        "A generated artifact of another resolver, and nothing a person reads or writes",
        rule_single_toolchain,
    ),
    Rule(
        11,
        "python-version-agreement",
        "every pixi python pin satisfies the requires-python floor, and every tool that writes a "
        "language level down writes the floor — agreement, not a count, so a range still passes",
        rule_python_version_agreement,
    ),
    Rule(
        12,
        "workflow-step-tasks",
        f"every workflow step that runs a command invokes a declared task — `{PIXI_RUN} <task>`, "
        f"with the task in {TASK_TABLE}, so the command CI runs is one a laptop can run too. "
        f"Arguments are allowed after `{ARGUMENT_SEPARATOR}` and printed in the notes; a shell "
        "operator is not an argument. A step that `uses:` an action is not a step of this build",
        rule_workflow_step_tasks,
    ),
    Rule(
        13,
        "nav-target-exists",
        "every `nav:` entry that names a page — `.md`, `.ipynb`, `.html` — names a tracked one "
        "under that configuration's site source. This is the one broken site the generator does "
        "not validate at all: it builds green, reports no issues, and publishes a menu item that "
        "404s. An entry naming no page, and a configuration whose plugins write pages during the "
        "build, are reported unchecked rather than failed",
        rule_nav_target_exists,
    ),
    Rule(
        14,
        "init-sentinel",
        "AGENTS.md is still the template's generic copy",
        warning_init_sentinel,
        warns_only=True,
    ),
)

#: Warning 15 is not a rule with a tree to inspect: it is the waiver table itself, printed on
#: every run. It lives in `report` because only the reporter knows what each waiver suppressed.
WAIVER_RULE = (15, "waivers")


def load(root: Path) -> Repo:
    """Read the tree and the declarations, or raise :class:`CannotRunError` saying why not.

    Every condition here is the same one: the inputs every rule reads are not there, so the answer
    is unknown rather than bad. None of them is a rule this repo broke, and :func:`main` gives them
    all `CANNOT_RUN` for that reason.
    """
    proc = subprocess.run(
        ["git", "-C", str(root), "ls-files", "-z"],
        capture_output=True,
        text=True,
        check=False,
    )
    if proc.returncode != 0:
        raise CannotRunError(f"cannot list tracked files in {root}\n{proc.stderr.strip()}")
    tracked = tuple(sorted(p for p in proc.stdout.split("\0") if p))
    pyproject = root / "pyproject.toml"
    if not pyproject.is_file():
        raise CannotRunError(f"no pyproject.toml in {root}")
    try:
        parsed = tomllib.loads(pyproject.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError) as reason:
        # A manifest nothing can parse is the declarations MISSING, not a rule broken — and a
        # traceback here would exit 1, reading as a failed rule with an unusually bad report.
        raise CannotRunError(f"cannot read pyproject.toml in {root}\n{reason}") from reason
    tool = parsed.get("tool", {})
    liulab = tool.get("liulab", {})
    return Repo(
        root=root,
        tracked=tracked,
        agent_docs=liulab.get("agent-docs", {}),
        waived=liulab.get("waived", {}),
    )


def _verdict(repo: Repo, rule: Rule, result: Result) -> tuple[str, list[str]]:
    """One rule's status word and the lines printed beside it.

    `ok` and `--` are deliberately different words. A rule whose premise was absent checked
    nothing, and a gate that fires on nothing looks exactly like one that passes unless the run
    says which happened.
    """
    waiver = repo.waived.get(rule.name) if rule.name != UNWAIVABLE else None
    if result.problems and waiver is not None:
        return "waived", [f"{len(result.problems)} problem(s) suppressed: {waiver}"]
    if result.problems and rule.warns_only:
        return "warn", [problem.splitlines()[0] for problem in result.problems]
    if result.problems:
        return "FAIL", [f"{len(result.problems)} problem(s), listed below"]
    if not result.notes:
        return "ok", ["nothing to check"]
    # `--` only when EVERY note says the premise was absent. Rule 8 has two independent halves,
    # and a run that really checked one of them has not checked nothing.
    if all(note.startswith("not checked") for note in result.notes):
        return "--", result.notes
    return "ok", result.notes


def _waiver_lines(repo: Repo, results: list[tuple[Rule, Result]]) -> list[str]:
    """Warning 15: every waiver, with what it actually suppressed.

    A waiver naming no rule is called out rather than ignored, and so is one naming rule 1, which
    refuses to be waived. A waiver that quietly does nothing is the same invisible escape hatch
    this table exists to keep visible.
    """
    known = {rule.name: result for rule, result in results}
    lines: list[str] = []
    for name, reason in sorted(repo.waived.items()):
        if name == UNWAIVABLE:
            lines.append(f"{name}: REFUSED, this rule cannot be waived — {reason}")
        elif name not in known:
            lines.append(f"{name}: matches no rule, so it waives nothing — {reason}")
        else:
            lines.append(f"{name}: {len(known[name].problems)} problem(s) suppressed — {reason}")
    return lines or ["none"]


def report(repo: Repo, results: list[tuple[Rule, Result]]) -> int:
    """Print every rule's verdict and every waiver, and return the exit status.

    The status table is printed whatever happened, failures included: what a run did NOT check is
    as much a part of the answer as what it did. Failures go to stderr, in the same
    what / why / fix shape `python skills/install.py --check` prints.
    """
    width = max(len(rule.name) for rule, _ in results)
    rows: list[tuple[str, str, str, list[str]]] = []
    failed: list[tuple[Rule, Result]] = []
    warned: list[tuple[Rule, Result]] = []
    for rule, result in results:
        status, notes = _verdict(repo, rule, result)
        rows.append(
            (f"{'warn' if rule.warns_only else 'rule'} {rule.number}", rule.name, status, notes)
        )
        if status == "FAIL":
            failed.append((rule, result))
        elif status == "warn":
            warned.append((rule, result))
    rows.append((f"warn {WAIVER_RULE[0]}", WAIVER_RULE[1], "--", _waiver_lines(repo, results)))

    print("conformance — the rules a Liu Lab repo keeps")
    for label, name, status, notes in rows:
        head = f"  {label:<8}{name:<{width}}  {status:<7}"
        for index, note in enumerate(notes):
            print(
                textwrap.fill(
                    note,
                    width=110,
                    initial_indent=head if index == 0 else " " * len(head),
                    subsequent_indent=" " * len(head),
                    # Notes are mostly identifiers — `target-version`, `agent-docs`, a path — and
                    # a wrap inside one leaves a name no reader can grep for.
                    break_on_hyphens=False,
                )
            )

    for rule, result in warned:
        print(f"\nwarning {rule.number}  {rule.name} — {rule.statement}")
        for problem in result.problems:
            print(textwrap.indent(problem, "  "))

    total = sum(1 for rule, _ in results if not rule.warns_only)
    if not failed:
        print(f"\nAll {total} rules pass.")
        return PASSED
    # Flushed before the first byte reaches stderr. `check.sh` captures both streams into one
    # file, where stdout is block-buffered and stderr is not, so without this the failures print
    # above the table that says which rules they came from.
    sys.stdout.flush()
    print(f"\nconformance: {len(failed)} of {total} rules failed.\n", file=sys.stderr)
    for rule, result in failed:
        statement = textwrap.fill(
            rule.statement, width=94, initial_indent="  ", subsequent_indent="  "
        )
        print(f"rule {rule.number}  {rule.name}\n{statement}", file=sys.stderr)
        for problem in result.problems:
            print(textwrap.indent(problem, "  "), file=sys.stderr)
        print("", file=sys.stderr)
    return FAILED


def main(argv: list[str] | None = None) -> int:
    """Run every rule against one tree and report, returning the exit status.

    `PASSED` or `FAILED` once the rules have run, and `CANNOT_RUN` when :func:`load` says the tree
    could not be read — the one answer that is about the invocation and not about the repo.
    """
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--root",
        type=Path,
        default=Path(__file__).resolve().parent.parent,
        help="Repository to check. Defaults to the repo this script lives in.",
    )
    args = parser.parse_args(argv)
    try:
        repo = load(args.root.resolve())
    except CannotRunError as reason:
        print(f"conformance: {reason}", file=sys.stderr)
        print("conformance: nothing was checked. This is not a rule failure.", file=sys.stderr)
        return CANNOT_RUN
    results = [(rule, rule.check(repo)) for rule in RULES]
    return report(repo, results)


if __name__ == "__main__":
    raise SystemExit(main())
