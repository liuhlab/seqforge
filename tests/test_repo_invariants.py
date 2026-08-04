"""Repo-wide invariants — checks about the shape of the tree, not about what any function returns.

Five families live here, and none of them composes anything:

- **Consumer, not parallel universe.** seqforge defines no genome machinery and no aligner
  environments of its own (those belong to ``liulab-genome`` / ``liulab-runtime``), and every
  ``liulab-genome`` attribute it calls really exists on the imported class. AST/attribute guards.
- **Prose that stays true.** No comment points at a governing document by number, because a number
  is a mutable label: renumber the document and the comment lies, silently, forever.
- **One owner for a compiled pipeline's layout.** No module outside the two that own those names
  joins the pipeline directory, the wrapper, the config or the units table into a path of its own.
- **Nothing tracked escapes the type checker.** The declared mypy scope covers every committed
  Python file, and every path the exclusion list hides is already gitignored.
- **The test loop declares what it needs.** What the suite requires of its environment is declared
  in the project configuration rather than remembered by whoever types the command — and the
  declaration is checked where it has to land, in the running process.

The ``src_trees`` AST parse and ``_src_root`` are shared from ``tests/conftest.py``.
"""

from __future__ import annotations

import ast
import os
import re
import subprocess
import tomllib
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import pytest

from conftest import SrcTrees, _src_root
from seqforge.pipeline import CONFIG_NAME, SNAKEFILE_NAME, UNITS_TSV_NAME
from seqforge.workspace import PIPELINE_DIRNAME

#: Names owned upstream by `liulab-genome`. seqforge may CALL them; defining one here means we have
#: started reimplementing the package whose whole job this is.
_UPSTREAM_GENOME_NAMES = frozenset({"Genome", "build_star_index", "register_gtf"})


def _defines_upstream_genome(node: ast.AST) -> bool:
    """A node that RE-DEFINES a name liulab-genome owns — the exact predicate
    ``test_seqforge_defines_no_genome_machinery`` applies to every node in the src tree. Shared so its
    discriminator exercises the real guard, not a re-implementation of it.
    """
    return (
        isinstance(node, ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef)
        and node.name in _UPSTREAM_GENOME_NAMES
    )


@pytest.mark.xdist_group("src-trees")
def test_seqforge_defines_no_genome_machinery(src_trees: SrcTrees) -> None:
    """Consumer, not parallel universe, as an AST check rather than a code-review habit.

    `Genome(assembly).build_star_index(gtf=name)` is a *consumer call* and is exactly right. A
    `def build_star_index` or `class Genome` in this tree is the opposite: it means the resolution
    of assemblies, annotations and indexes — liulab-genome's entire remit — is being duplicated
    here, where it will drift and where the "no absolute path in a manifest" rule stops being anybody's
    invariant.
    """
    offenders: list[str] = []
    for py, tree in src_trees.items():
        for node in ast.walk(tree):
            if _defines_upstream_genome(node):
                # The predicate is true only for the three node types that carry `lineno` and `name`;
                # it returns `bool`, so the checker cannot narrow `node` through it.
                offenders.append(f"{py.name}:{node.lineno} defines {node.name!r}")  # type: ignore[attr-defined]
    assert not offenders, "seqforge is redefining liulab-genome's job:\n" + "\n".join(offenders)

    # The guard discriminates (folded from test_the_genome_machinery_check_can_actually_catch_a_reimpl
    # ementation, which used to re-implement this walk locally and so only proved a COPY fires). These
    # call the REAL predicate: it must fire on a reimplementation and tolerate the consumer call.
    assert any(
        _defines_upstream_genome(n) for n in ast.walk(ast.parse("class Genome:\n    pass\n"))
    )
    assert any(
        _defines_upstream_genome(n)
        for n in ast.walk(ast.parse("def build_star_index(gtf):\n    return 1\n"))
    )
    assert not any(  # the real, correct usage must NOT trip it
        _defines_upstream_genome(n)
        for n in ast.walk(
            ast.parse(
                "from genome import Genome\nindex = Genome(assembly).build_star_index(gtf=annotation)\n"
            )
        )
    )


#: Every liulab-genome attribute seqforge calls. We are a consumer, and a consumer has an
#: import surface — this is it.
#:
#: **This is a hand-written list, and that is fine here, because it is checked against the REAL
#: package rather than against itself.** That distinction is the whole lesson of this repo: a list
#: mirroring code and validated by a test that reads the same list proves nothing (`required_config`);
#: a list asserted against the actual object is a contract test, and it goes red the moment upstream
#: moves. Nothing here can drift silently.
_GENOME_API = {
    "get_star_index",  # starsolo.smk / star.smk rule genome_index + e2e: resolve the prebuilt index
    "get_chromap_index",  # chromap.smk rule genome_index: resolve the prebuilt scATAC index (no GTF)
    "register_gtf",  # staging an annotation (see the consumer note in CLAUDE.md)
    "fasta_path",  # chromap.smk rule genome_index (chromap maps against -r ref) + e2e: simulate reads
    "default_gtf_path",  # e2e: build gene models
    "annotations",  # e2e/docs: which GTF names are registered
}


def test_seqforge_only_calls_liulab_genome_methods_that_exist() -> None:
    """The consumer surface is real, checked at test time, in every environment.

    `discover_assets` called `Genome.get_star_index(...)` — a method liulab-genome **has never had**.
    It was a lazy import, inside an arm that only runs on a cluster, against a dependency that was not
    declared, so nothing could have noticed: the `AttributeError` simply waited for whoever ran it. It
    waited until 2026-07-15.

    Same shape as the STAR-argv bug one commit earlier: two renderings of "how do I get an index",
    by hand, in two places that could not see each other — `starsolo.smk` said `build_star_index` and
    was right, `e2e.py` said `get_star_index` and was wrong, and the one nobody executed was the
    broken one.

    liulab-genome is a declared dependency now, so this check runs everywhere rather than on a cluster
    nobody visits. That is the fix; renaming the method was just the symptom.
    """
    from genome import Genome

    missing = sorted(name for name in _GENOME_API if not hasattr(Genome, name))
    assert not missing, (
        f"seqforge calls liulab-genome attributes that do not exist: {missing}. "
        f"Either upstream moved and our calls need updating, or this list has grown a name nobody "
        f"calls. Both are real; neither is silent any more."
    )
    # ...and the guard discriminates (folded from test_the_genome_api_check_would_catch_a_method_that
    # _does_not_exist): the name we resolve the prebuilt index through must exist, and a name
    # liulab-genome does not define must NOT resolve — else `missing` being empty would prove nothing.
    assert hasattr(Genome, "get_star_index"), "seqforge resolves the prebuilt index through this"
    assert not hasattr(Genome, "resolve_star_index_please"), (
        "a name liulab-genome does not define must not resolve — else the guard proves nothing"
    )


#: Filenames that would mean seqforge had begun defining aligner environments — `liulab-runtime`'s
#: job. seqforge names an env (`align-rna`); it never says what is inside one.
_ENV_DEFINITION_FILES = ("environment.yml", "environment.yaml", "conda.yml", "Dockerfile")


def test_seqforge_defines_no_aligner_environments() -> None:
    """The other half of consumer-not-parallel-universe: an env is NAMED here and DEFINED in liulab-runtime.

    `RuntimeEnv` is a closed literal of liulab-runtime env names — there is deliberately no profile
    indirection, the name *is* the identifier. A conda YAML or Dockerfile appearing in this tree
    would mean we had started duplicating liulab-runtime, scattering env definitions across two
    repos that then disagree about which STAR ran.
    """
    from typing import get_args

    from seqforge.models.processing import RuntimeEnv

    assert set(get_args(RuntimeEnv)) == {"align-rna", "align-dna", "ml", "ml-gpu"}
    found = [str(p) for name in _ENV_DEFINITION_FILES for p in _src_root().rglob(name)]
    assert not found, f"seqforge is defining an aligner environment: {found}"


#: The section sign (U+00A7), spelled as a codepoint so this file does not contain the character it
#: forbids. It has no meaning in this domain at all, which is what makes forbidding it outright the
#: unambiguous half of this guard.
_SECTION_SIGN = chr(0xA7)

#: A pointer at a governing document's NUMBERED rule, as a regex over one line of prose.
#:
#: Deliberately NARROW, because the alternative is a guard that blocks the project's own vocabulary.
#: A capital ``R`` followed by a low digit is a **read designation** here and appears everywhere
#: legitimately: the barcode read is one, ``--readFilesIn`` names two, a bcl2fastq basename carries
#: one, a dotted manifest path ends in one, a sacCer3 genome build starts with one, and a worm
#: replicate label in a fixture is another. None of those may be touched, so the low numbers are
#: matched only in a shape that cannot be any of them.
#:
#: Two arms, therefore:
#:
#: - a bare ``R`` plus a rule number of **four or above** — no instrument writes a fourth or a
#:   nineteenth read, so at those numbers the token can only be a pointer. Unless it is
#:   quote-adjacent: a quoted token is *data*, and one eval case names a read id that deliberately
#:   does not exist.
#: - the words ``rule`` or ``per`` in front of ``R`` plus **any** number — a shape prose never uses
#:   for a read.
#:
#: The known gap is therefore a pointer at one of the first three rules written any other way. That
#: is the price of never firing on a read designation, and it is the right side to err on: a guard
#: that cries wolf gets deleted, and this one has to survive.
_NUMBERED_RULE = re.compile(r"(?<![\"'])\bR(?:[4-9]|1[0-9])\b(?![\"'])|\b(?:rule|per)\s+R[0-9]+\b")

#: The same pointer with the section sign transliterated to a bare capital ``S``. Transliterating a
#: forbidden character is not a way around the rule — the one pointer that outlived the first sweep
#: was spelled this way, and it named two documents that had already been deleted.
#:
#: A bare ``S`` plus a digit still may not be matched, for the same reason the low rule numbers are
#: left alone: it is a **supplementary-table citation** in the SPLiT-seq spec (the oligo table that
#: entry's strand is reconstructed from) and an **Illumina sample id** in a fixture filename. Both are
#: legitimate and neither may be touched.
#:
#: So this arm mirrors the ``rule``/``per`` arm — the digits count only directly behind a word that
#: names a governing document. The set is what a pointer here has used or could: the two deleted
#: documents by name (``brief``, ``design``), the generic ``section``, and the two live trees an agent
#: might reach for the same way (``spec``, ``adr``). ``Table`` is deliberately absent, which is the
#: whole reason the supplementary citations survive.
_TRANSLITERATED_SECTION = re.compile(
    r"\b(?i:brief|design|spec|section|adr)s?\s+S[0-9]+(?:\.[0-9]+)*\b"
)

#: The surfaces a human writes prose into. Everything the sweep found lived in one of these.
_PROSE_SUFFIXES = frozenset({".py", ".md", ".yaml", ".yml", ".smk", ".toml"})

#: The one exemption, and it is the file whose SUBJECT is the numbered rules: it parses their ids out
#: of the agent-facing documents and asserts the two lists agree. There, a rule number is *data under
#: test*, not a pointer — exempting it is the difference between "no comment cites a rule by number"
#: and "the rule table may not be tested". The cost is that a real pointer inside that one file goes
#: unseen, which is the smaller of the two harms by a wide margin.
_EXEMPT = frozenset({"test_docs.py"})


def _points_by_number(text: str) -> bool:
    """Does this line point at a governing document by NUMBER instead of naming the thing?

    The exact predicate ``test_no_comment_points_at_a_governing_document_by_number`` applies to every
    line of the surfaces that CONSUME the numbered rules. Shared so its discriminator exercises the
    real guard rather than a re-implementation of it.
    """
    return (
        _SECTION_SIGN in text
        or bool(_NUMBERED_RULE.search(text))
        or bool(_TRANSLITERATED_SECTION.search(text))
    )


def _numbered(n: int) -> str:
    """``R`` plus a number, assembled at run time so this file holds no literal pointer of its own."""
    return f"R{n}"


def _transliterated(document: str, n: int) -> str:
    """A governing-document word plus the transliterated section sign, likewise assembled at run time."""
    return f"{document} S{n}"


def _prose_files() -> list[Path]:
    """Every file a human writes prose into, on the surfaces that CONSUME the numbered rules.

    The code, the tests, the skills that wrap the CLI, the eval corpus that pre-registers what the
    compiler should decide, and the project config. What is deliberately absent is the other
    direction: the router, the glossary, the agent-facing reference tree and the ADR tree are where
    the numbering is *defined*, and a rule table that may not name its own rules is not a rule table.
    """
    tests = Path(__file__).resolve().parent
    repo = tests.parent
    roots = (_src_root(), tests, repo / "skills", repo / "evals")
    scanned = [p for root in roots for p in root.rglob("*")] + [repo / "pyproject.toml"]
    return sorted(p for p in scanned if p.suffix in _PROSE_SUFFIXES and p.name not in _EXEMPT)


def test_no_comment_points_at_a_governing_document_by_number() -> None:
    """A pointer by number rots; a sentence does not.

    Comments used to cite the governing documents by numbered section and by numbered rule. Four of
    those pointers were already dangling when this guard was written: one named a brief that had been
    absorbed into another file and deleted, two named an old numbering the document no longer used,
    and one named a section that had never existed. Nothing could have noticed — a comment is not
    executable, and the document it points into is not either.

    So the fix is not "update the numbers", it is "stop depending on them". A comment that only
    carried a pointer is deleted; a comment that carried an explanation keeps the explanation and
    says the thing in words. That leaves the comment true under any renumbering, and true for a
    reader who never opens the other document at all.
    """
    offenders = [
        f"{path}:{i}: {line.strip()[:100]}"
        for path in _prose_files()
        for i, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1)
        if _points_by_number(line)
    ]
    assert not offenders, (
        "a comment points at a governing document by number:\n"
        + "\n".join(offenders)
        + "\n\nWrite the idea instead of the label. Name the concept — the read budget, a Blocker, "
        "the byte resolver, a benign twin; CONTEXT.md is the glossary — and delete the comment "
        "outright if the pointer was all it carried. A number is a mutable label, and four of them "
        "had already gone stale by the time this check existed."
    )

    # ...and the guard discriminates. These call the REAL predicate: it must fire on every shape the
    # sweep removed, and stay silent on the read designations, the genome build and the replicate
    # label that share the spelling. A guard nobody proved fires is a guard that always allows.
    assert _points_by_number(f"the count matrix gate (design {_SECTION_SIGN}4.1)")
    assert _points_by_number(f"{_SECTION_SIGN}12 says two entries with identical params")
    assert _points_by_number(f"per {_numbered(10)} we consume it rather than reimplement it")
    assert _points_by_number(f"rule {_numbered(5)}: disk is state, context is cache")
    assert _points_by_number(f"the CLI is the API ({_numbered(6)})")
    assert _points_by_number(f"the metric {_numbered(11)}(c) names")
    assert _points_by_number(
        f"rule {_numbered(1)} — emit data, never code"
    )  # low, in a cited shape
    assert _points_by_number(
        f"the evals harness ({_transliterated('design/brief', 9)})"
    )  # the sign, transliterated
    assert _points_by_number(
        f"mypy is scoped to the modules {_transliterated('the brief', 13)} names"
    )

    assert not _points_by_number("R1 = CB + UMI; R2 = cDNA, sense to the mRNA")
    assert not _points_by_number("assembling the paper's own Table S12 oligos 5'->3'")
    assert not _points_by_number("SRR28716558_S1_L001_R1_001.fastq.gz")
    assert not _points_by_number("--readFilesIn R2,R1")
    assert not _points_by_number("SRR1234567_S1_L001_R1_001.fastq.gz")
    assert not _points_by_number("library.read_layout.R1.length")
    assert not _points_by_number("#!genome-build R64-1-1")
    assert not _points_by_number('a run alias ("N2_wild_type", "daf-2 R3")')
    assert not _points_by_number("the canonical geometry is a 16 bp barcode read (R2)")
    assert not _points_by_number(f'{{"file": "{_numbered(9)}"}}')  # quoted -> a fixture's read id


#: The names that make up a compiled pipeline directory, taken from the two modules that own them —
#: never re-typed here. `workspace.py` names the subtree and `pipeline.py` names the three files it
#: holds, so a rename moves this guard with it rather than leaving it policing a name nothing writes.
_PIPELINE_LAYOUT_NAMES = frozenset({PIPELINE_DIRNAME, SNAKEFILE_NAME, CONFIG_NAME, UNITS_TSV_NAME})

#: The two files allowed to spell them, and the split is the decision: one names the DIRECTORY beside
#: every other subtree it names, the other names what is inside one and opens it.
_PIPELINE_LAYOUT_OWNERS = frozenset({"workspace.py", "pipeline.py"})


def _joins_a_path(node: ast.AST) -> list[ast.Constant]:
    """The string literals ``node`` puts somewhere a path or a filename constant is made.

    Three positions, and they are the three the five hand-spellings actually used: an operand of
    ``/``, an argument to a call (``glob("*/Snakefile")``, ``Path(d, "units.tsv")``), and the whole
    right-hand side of an assignment — the last because a private ``_SNAKEFILE_NAME = "Snakefile"``
    is precisely the shape this decision removed from the composer, and a guard that could not see it
    come back would be guarding the symptom.
    """
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Div):
        return [side for side in (node.left, node.right) if isinstance(side, ast.Constant)]
    if isinstance(node, ast.Call):
        args = [*node.args, *(kw.value for kw in node.keywords)]
        return [arg for arg in args if isinstance(arg, ast.Constant)]
    if isinstance(node, ast.Assign | ast.AnnAssign) and isinstance(node.value, ast.Constant):
        return [node.value]
    return []


def _spells_the_pipeline_layout(tree: ast.AST) -> list[str]:
    """Where this module builds a path out of a name the compiled-pipeline owners own.

    The exact predicate ``test_only_the_compiled_pipeline_owner_spells_its_layout`` applies to every
    module in the src tree, shared with its discriminator so what is proven to fire is the real
    thing.

    **Segments, not substrings**, so ``"*/Snakefile"`` is caught and a sentence that merely mentions
    the config is not. **Path positions, not every literal**, and that narrowing is the difference
    between a guard that survives and one that gets deleted: ``pipeline`` is also an ordinary English
    word, and it is legitimately a tab id on the report page and a key in the project index. Neither
    is a path, neither would be fixed by importing a directory name, and a check that demanded they
    change would be crying wolf on its second day.
    """
    found: list[str] = []
    for node in ast.walk(tree):
        for literal in _joins_a_path(node):
            value = literal.value
            if isinstance(value, str) and _PIPELINE_LAYOUT_NAMES & set(value.split("/")):
                found.append(f"{literal.lineno}: {value!r}")
    return sorted(found)


@pytest.mark.xdist_group("src-trees")
def test_only_the_compiled_pipeline_owner_spells_its_layout(src_trees: SrcTrees) -> None:
    """One owner for the compiled pipeline directory, as a mechanism rather than a habit.

    Its layout used to be spelled by hand in five modules — the composer that writes it, the report
    collector, the project index, the compose gates and the ground-truth harness — and two of them
    had grown independently written copies of the same derivation over it. Five copies of a string
    is five chances for one to be stale, and the copy nobody executes is the one that is wrong; this
    repo has already paid that twice, on a STAR command line and on how to resolve a genome index.

    So the check is the sixth consumer's problem, not the reviewer's: join one of these names into a
    path anywhere else in the tree and this goes red naming the file and the line. The fix is always
    the same shape — ask ``CompiledPipeline`` for the path, or ``workspace.pipeline_dir`` for the
    subtree — and it is in the message, because a guard that only says "no" gets worked around.
    """
    offenders = {
        py.name: found
        for py, tree in src_trees.items()
        if py.name not in _PIPELINE_LAYOUT_OWNERS and (found := _spells_the_pipeline_layout(tree))
    }
    assert not offenders, (
        "a module outside the compiled-pipeline owners spells its layout:\n"
        + "\n".join(f"  {name} {line}" for name, lines in offenders.items() for line in lines)
        + "\nAsk `seqforge.pipeline.CompiledPipeline` for a file inside a run directory, and "
        "`seqforge.workspace.pipeline_dir` for the subtree. One owner is the point: a second "
        "spelling is a second thing to keep in step, and nothing tells you when it stops being."
    )

    # ...and the guard discriminates. These call the REAL predicate: it must fire on every shape the
    # five hand-spellings took, and stay silent on the two places the same word is a word. A guard
    # nobody proved fires is a guard that always allows.
    def fires(source: str) -> bool:
        return bool(_spells_the_pipeline_layout(ast.parse(source)))

    assert fires('snakefiles = sorted((base / "pipeline").glob("*/Snakefile"))')  # the report's
    assert fires('config = yaml.safe_load((rundir / "config.yaml").read_text())')  # the harness's
    assert fires('subprocess.run(["snakemake", "-s", str(scratch / "Snakefile")])')  # the gate's
    assert fires('_SNAKEFILE_NAME = "Snakefile"')  # the composer's private constant
    assert fires('units = Path(directory, "units.tsv")')  # a call that builds a path
    assert fires('state_dir(workspace, "pipeline", readable(name, rid))')  # the composer's join

    assert not fires('"""The composed config.yaml, beside the Snakefile."""')  # prose
    assert not fires('portable = k in ("manifest", "snakefile", "pipeline")')  # a key, not a path
    assert not fires('_TABS = [("overview", "Overview"), ("pipeline", "Pipeline")]')  # a tab id
    assert not fires('log.info("no config.yaml here yet")')  # a sentence in a call


_REPO = Path(__file__).resolve().parent.parent


def _pyproject() -> dict[str, Any]:
    """The project configuration, parsed. Two guards here read declarations out of it."""
    return tomllib.loads((_REPO / "pyproject.toml").read_text(encoding="utf-8"))


def _mypy_scope() -> tuple[list[str], list[str]]:
    """The declared type-checking scope: the roots it covers, and the patterns it hides."""
    mypy = _pyproject()["tool"]["mypy"]
    return list(mypy["files"]), list(mypy.get("exclude", []))


def _tracked_python_files() -> list[str]:
    """Every COMMITTED ``.py`` file, repo-relative and slash-separated.

    Tracked, not on-disk: that single choice is why the gitignore patterns need no second copy here.
    A developer's scratch harness is untracked, so it is never demanded by the coverage assertion,
    and nothing in this module has to know what such a file is called.
    """
    listed = subprocess.run(
        ["git", "ls-files", "-z", "--", "*.py"],
        cwd=_REPO,
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    return sorted(path for path in listed.split("\0") if path)


def _escapes_scope(paths: list[str], roots: list[str]) -> list[str]:
    """Which of ``paths`` no root in ``roots`` covers — the exact predicate the guard applies.

    Shared with the guard's discriminator so that what is proven to fire is the real thing, not a
    re-implementation of it that happens to agree today.
    """
    prefixes = tuple(f"{root.rstrip('/')}/" for root in roots)
    return sorted(path for path in paths if not path.startswith(prefixes))


def _hidden_by(exclude: list[str], paths: list[str]) -> list[str]:
    """Which of ``paths`` an exclusion pattern hides from the checker.

    mypy matches ``exclude`` as a REGULAR EXPRESSION with ``re.search`` against the slash-separated
    path, which is why this is a search over compiled patterns and not a prefix test like the roots.
    """
    patterns = [re.compile(pattern) for pattern in exclude]
    return sorted(path for path in paths if any(p.search(path) for p in patterns))


def _not_gitignored(paths: list[str]) -> list[str]:
    """Which of ``paths`` git would NOT ignore — asked of git, not of a copy of its patterns."""
    if not paths:
        return []
    proc = subprocess.run(
        ["git", "check-ignore", "-z", "--stdin"],
        cwd=_REPO,
        input="\0".join(paths),
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode in (0, 1), f"git check-ignore failed: {proc.stderr}"
    ignored = {path for path in proc.stdout.split("\0") if path}
    return sorted(set(paths) - ignored)


@pytest.mark.repo
def test_nothing_tracked_escapes_the_type_checker() -> None:
    """One checker over the whole tree, held open from both sides.

    The scope used to be a hand-written package list inside the ``typecheck`` task, which meant a new
    top-level package was unchecked until somebody remembered to add it and a *narrowed* scope looked
    exactly like a passing one. Both halves of that are mechanised here:

    - **Coverage.** Every committed ``.py`` file falls inside a declared root. Commit a new top-level
      package and this goes red naming it, with the fix in the message; remove a root and it goes red
      the same way, instead of quietly reducing what CI sees. The root still gets typed by hand —
      what the guard removes is the chance to forget.
    - **The exclusion list cannot grow to hide real code.** Whatever ``exclude`` hides must already
      be gitignored — so the way to make a file that will not check stop failing is to fix it or
      ``git rm`` it, never to add a line here.

    The second assertion is what lets the first be about *tracked* files. A gitignored scratch
    harness in this directory is not demanded by coverage and is legitimately skipped by an
    exclusion, and neither fact needs the two glob patterns copied out of the gitignore into the
    project file. A pair of lists that must agree is the shape this tree has already had to fix
    three times; asking git instead is what removes the pair.
    """
    roots, exclude = _mypy_scope()
    tracked = _tracked_python_files()
    assert tracked, "git ls-files found no Python files -- is this a checkout?"

    escaped = _escapes_scope(tracked, roots)
    assert not escaped, (
        f"tracked Python files the type checker does not see: {escaped}\n"
        f"Declared roots are {roots}. Add the tree to `[tool.mypy] files`, or delete the files -- "
        f"code that ships is code that checks."
    )

    hidden = _not_gitignored(_hidden_by(exclude, tracked))
    assert not hidden, (
        f"`[tool.mypy] exclude` hides tracked, non-gitignored files: {hidden}\n"
        f"An exclusion may only skip what git already ignores. Fix the file or `git rm` it; hiding "
        f"it from the checker is how a gate goes green while the code stays broken."
    )

    # ...and the guard discriminates. These call the REAL predicates: a narrowed scope must be
    # caught, the declared one must be clean, and an exclusion aimed at a committed file must be
    # reported. A guard nobody proved fires is a guard that always allows.
    here = Path(__file__).relative_to(_REPO).as_posix()
    assert _escapes_scope([here], ["src"]) == [here]  # scope narrowed to omit the test tree
    assert _escapes_scope([here], roots) == []  # the declared scope really does cover it
    assert _hidden_by([r"test_repo_invariants\.py"], [here]) == [here]  # an exclusion would hide it
    assert _not_gitignored([here]) == [here]  # ...and this file is committed, so that is illegal
    assert _hidden_by([], [here]) == []  # no exclusions, nothing hidden

    # ...and the SHIPPED exclusions really do hide a scratch harness. Both assertions above pass
    # vacuously if the patterns match nothing, so a typo in one would leave CI green while every
    # developer's editor started reporting a file that is deliberately nobody's deliverable — the
    # one failure this guard could otherwise not see. These two names are the gitignore's two globs.
    for scratch in ("tests/_scratch2_test.py", "tests/_test_scratch.py"):
        assert _hidden_by(exclude, [scratch]) == [scratch], (
            f"`[tool.mypy] exclude` no longer hides {scratch} -- a gitignored scratch harness is "
            f"back in the checker's scope, and only the developer who owns one will notice."
        )
    assert _hidden_by(exclude, [here]) == []  # ...while a committed test is untouched by them


#: The thread-pool variables the numeric stack reads ONCE, at import time, in whichever process
#: imported it. Every xdist worker is a separate process, so each one otherwise sizes its own BLAS
#: and OpenMP pools to the whole machine — twelve workers, each believing it has 48 cores.
#:
#: Three names rather than one because three libraries are underneath: OpenMP is what the runtime
#: itself reads, and the two BLAS builds that arrive with numpy read their own name in preference.
#: Pinning one of the three leaves the other two sizing themselves to the box.
_THREAD_POOL_VARS = ("OMP_NUM_THREADS", "OPENBLAS_NUM_THREADS", "MKL_NUM_THREADS")


def _unpinned(env: Mapping[str, str]) -> list[str]:
    """Which thread-pool variables ``env`` fails to pin to a single thread.

    The exact predicate ``test_the_test_environment_pins_its_thread_pools`` applies to BOTH ends —
    the declaration in the project configuration and the environment of the running process. Shared
    so the guard's discriminator exercises the real thing rather than a copy of it, and so the two
    ends cannot drift into asking different questions.
    """
    return sorted(name for name in _THREAD_POOL_VARS if env.get(name) != "1")


def _test_feature_env() -> dict[str, str]:
    """What the ``test`` feature's environment DECLARES, read from the project configuration.

    A feature's activation environment, not a task's: a task-level spelling would hold only for the
    verb it was written on, and the suite is invoked several other ways.
    """
    feature = _pyproject()["tool"]["pixi"]["feature"]["test"]
    declared = feature.get("activation", {}).get("env", {})
    return {name: str(value) for name, value in declared.items()}


@pytest.mark.repo
def test_the_test_environment_pins_its_thread_pools() -> None:
    """One thread per worker, declared once — and proven to have arrived.

    A process sizes its thread pools to the whole machine the first time the numeric stack is
    imported, and every worker is its own process — so twelve workers each opened a pool wide enough
    for the box and then fought each other for it, spending CPU on the contention and making the wall
    time swing between repeats until an ordinary run could read as a regression.

    Both ends are asserted, and the second is the one that matters. A declaration that is present in
    the project configuration but never reaches the worker processes fails in exactly the way this
    guard exists to prevent, and it looks identical to success from the configuration side alone. So
    the running process is asked directly — and a worker inherits the environment the session was
    started with, so what this process can see is what a worker would have got.

    Being a property of the environment rather than a flag on one task is the whole point — every
    invocation of the suite inherits it, the parallel verb and the narrowed run and CI alike, and
    nobody has to remember a flag.
    """
    declared = _test_feature_env()
    assert not _unpinned(declared), (
        f"the test environment does not pin {_unpinned(declared)} to one thread.\n"
        f"Declare them in the `test` feature's activation environment, where every invocation of "
        f"the suite inherits them -- not on a task, which would hold for one verb only."
    )

    observed = _unpinned(os.environ)
    assert not observed, (
        f"the running test process does not see {observed} pinned to one thread.\n"
        f"The declaration exists but did not reach here, so the workers are still oversubscribing "
        f"the box. Run the suite through the test environment (`pixi run test`), and if it is "
        f"already running there, the activation environment is not being applied."
    )

    # ...and the guard discriminates. These call the REAL predicate: it must fire on an environment
    # that pins nothing, on one that pins some but not all, and on one that pins a name to a count
    # that is not one — and stay silent only on the fully pinned case. A guard nobody proved fires
    # is a guard that always allows.
    # Both the inputs and the expected values are spelled out rather than derived from the tuple
    # above, so dropping a name from it goes red here instead of quietly narrowing what the guard
    # demands of the environment.
    pinned = {"OMP_NUM_THREADS": "1", "OPENBLAS_NUM_THREADS": "1", "MKL_NUM_THREADS": "1"}
    assert _unpinned({}) == ["MKL_NUM_THREADS", "OMP_NUM_THREADS", "OPENBLAS_NUM_THREADS"]
    assert _unpinned(pinned) == []  # ...and the pinned environment this change replaces it with
    assert _unpinned({**pinned, "OMP_NUM_THREADS": "8"}) == [
        "OMP_NUM_THREADS"
    ]  # declared, but wide
    # One of three: the failure a single-variable declaration would leave behind, silently, since the
    # two BLAS names each win over the generic one in the library that reads them.
    assert _unpinned({"OMP_NUM_THREADS": "1"}) == ["MKL_NUM_THREADS", "OPENBLAS_NUM_THREADS"]
