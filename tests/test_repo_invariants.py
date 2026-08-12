"""Repo-wide invariants — checks about the shape of the tree, not about what any function returns.

Nine families live here, and none of them composes anything:

- **Consumer, not parallel universe.** seqforge defines no genome machinery and no aligner
  environments of its own (those belong to ``liulab-genome`` / ``liulab-runtime``), and every
  ``liulab-genome`` attribute it calls really exists on the imported class. AST/attribute guards,
  plus a read of every pixi dependency table — the AST half was here first and watched only the
  *source*, so a feature declaring ``star`` walked straight past it.
- **A skip is green.** Every test that gates itself on a binary this project does not own carries
  the marker routing it to the lane that has one. The two lane selections are each other's boolean
  negation, so they partition the suite only if the marker is total; an unmarked gate sits in the
  lane that was never going to carry the binary and reports green having checked nothing.
- **Prose that stays true.** No comment points at a governing document by number, because a number
  is a mutable label: renumber the document and the comment lies, silently, forever.
- **One owner for a compiled pipeline's layout.** No module outside the two that own those names
  joins the pipeline directory, the wrapper, the config or the units table into a path of its own.
- **One owner for an artifact's name.** No shipped Snakemake module spells an artifact suffix that
  a Python module in ``workflows`` already publishes for it to import.
- **Nothing tracked escapes the type checker.** The declared mypy scope covers every committed
  Python file, and every path the exclusion list hides is already gitignored.
- **The test loop declares what it needs.** What the suite requires of its environment is declared
  in the project configuration rather than remembered by whoever types the command — and the
  declaration is checked where it has to land, in the running process.
- **The gate reports what it ran.** The pre-PR gate's own runner is exercised as a runner: a step
  that fails has to reach a non-zero exit and a printed verdict, and an interrupted gate has to
  leave nothing of itself running.
- **The schema describes the whole machine surface.** No exportable model emits a key that
  ``schema export`` does not declare, because that schema is the contract (R1) and a stdout object
  it cannot describe is one a consumer would be right to reject.

The ``src_trees`` AST parse and ``_src_root`` are shared from ``tests/conftest.py``.
"""

from __future__ import annotations

import ast
import os
import re
import shutil
import signal
import subprocess
import time
import tomllib
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import pytest
from pydantic import BaseModel, computed_field

from conftest import _SPAWNS_SNAKEMAKE, SrcTrees, _src_root
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
    "get_gtf_path",  # io umi-count: the registered GTF, beside the database gffutils built from it
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


#: The ONE table permitted to name a tool liulab-runtime owns. Everything this guard allows is a
#: consequence of that feature being unreachable from anything shipped or executed — see the test.
_ALIGNER_FEATURE = "star"


@pytest.mark.repo
def test_the_aligner_is_confined_to_the_test_only_environment() -> None:
    """...and the same rule read where it was actually broken: the project configuration.

    Four shipped files used to say this repo declares no aligner *anywhere* —
    ``workflows/__init__.py``, ``workflows/cram.py``, ``starsolo.smk``, ``chromap.smk`` — and all four
    were true, unenforced, and quietly falsified when a pixi feature carrying ``star = "*"`` was added
    (#336, reverted in #338): the sibling guard reads the *source tree* for a conda YAML or a
    Dockerfile, and a pixi feature is neither.

    The rule is now narrower and the narrowing is the point. What consumer-not-parallel-universe
    actually protects is that **no Snakemake rule and no wheel ever resolves an aligner from our
    tables** — an alignment environment a rule NAMES is liulab-runtime's to define. It never required
    that a binary two tests exec be absent from the repository. So ``star`` may be declared, in
    exactly one feature, reachable from exactly one environment that ships nothing and that no rule
    can see.

    Three properties keep that true and this test asserts all three, because losing any one of them
    reinstates the failure that forced the revert:

    - the aligner appears in **no other table**, so it cannot reach ``default``, ``test`` or ``docs``;
    - ``test-star`` sets ``no-default-feature``, so it does not inherit ``[tool.pixi.dependencies]``
      — that is where ``pymupdf`` is, and inheriting it puts STAR's ``libdeflate 1.22`` and mupdf's
      ``libdeflate >=1.25`` in one solve, which HAS NO SOLUTION on ``osx-arm64``;
    - ``test-star`` is in **its own solve group**, for the same collision by the other route.

    Those last two are why the reverted attempt could not be built on Apple silicon and had to be
    pinned to ``linux-64``. They are not style. The binaries reach the suite through ``PATH`` (the
    ``test`` feature's activation), never through a shared solve, because nothing imports an aligner.
    """
    permitted = f"tool.pixi.feature.{_ALIGNER_FEATURE}.dependencies"
    offenders = {
        table: sorted(hits)
        for table, packages in _declared_packages().items()
        if table != permitted and (hits := {p for p in packages if p.lower() in _RUNTIME_OWNED})
    }
    assert not offenders, (
        f"a pixi dependency table outside `{permitted}` declares a tool liulab-runtime owns: "
        f"{offenders}.\nAn aligner may be declared for the `external` tests to exec, and NOWHERE a "
        f"rule or the wheel can resolve it. Add it to the `{_ALIGNER_FEATURE}` feature, which only "
        f"`test-star` uses and which reaches the suite through PATH."
    )

    # ...and the permitted table really is the one carrying them, so `offenders` being empty is not
    # empty because the aligner vanished.
    assert {"star", "samtools", "htslib"} <= {p.lower() for p in _declared_packages()[permitted]}, (
        f"`{permitted}` is where the aligner lives; the `external` tests exec these three"
    )

    # ...and the two absences that make confinement real rather than nominal. Either one coming back
    # is the unsolvable-on-osx-arm64 lock that forced the revert.
    envs = _pyproject()["tool"]["pixi"]["environments"]
    star_envs = {
        name
        for name, spec in envs.items()
        if _ALIGNER_FEATURE in (spec.get("features", []) if isinstance(spec, dict) else spec)
    }
    assert star_envs == {"test-star"}, (
        f"only `test-star` may carry the `{_ALIGNER_FEATURE}` feature; found {sorted(star_envs)}"
    )
    star_env = envs["test-star"]
    assert star_env.get("no-default-feature") is True, (
        "`test-star` must not inherit `[tool.pixi.dependencies]` -- pymupdf is there, and its "
        "libdeflate has no common solution with STAR's on osx-arm64"
    )
    assert star_env.get("solve-group") not in {None, "default"}, (
        "`test-star` must solve alone; joining `default` reinstates the same libdeflate collision"
    )

    # ...while leaving what this project legitimately declares alone. `snakemake-minimal` builds the
    # DAG for compose's wiring gate and is not an alignment tool; the aligner check must not creep
    # into forbidding it, or the wiring gate goes back to skipping green.
    assert "snakemake-minimal" not in _RUNTIME_OWNED
    assert "snakemake-minimal" in _declared_packages()["tool.pixi.feature.wf.dependencies"]


#: The binaries a test asks PATH for and this project does not own — every one of them a name whose
#: absence turns a test into a skip. `STAR` and `samtools` come from the test-only environment, the
#: two htslib tools beside them, and `snakemake` is a declared dependency that is nonetheless absent
#: from a machine running the lane that drops it.
#:
#: `bash` is deliberately NOT here, and the exclusion is the half worth explaining. It is on every box
#: this suite runs on, the gate-runner tests exec it unconditionally, and the one place `which` asks
#: for it is a **parametrize source** — a case list, one entry per interpreter on the machine — rather
#: than a gate that decides whether a test runs at all. Listing it would make this guard demand the
#: marker on tests that skip nothing and spawn nothing either lane cares about, which is how a guard
#: earns its deletion.
_UNOWNED_BINARIES = frozenset({"STAR", "samtools", "bgzip", "tabix", "snakemake"})

#: The subset the external lane's probe does NOT ask PATH for, and why it need not. `snakemake` is a
#: declared dependency of the test environment itself rather than of the aligner-only one beside it,
#: so any machine that ran `pixi install` has it by construction — there is no state in which the
#: lane starts and it is missing. The rest arrive from an environment that can simply be absent,
#: which is the case worth refusing before a selection rather than discovering as a skip.
_TEST_ENVIRONMENT_OWNS = frozenset({"snakemake"})


def _asks_path_for(node: ast.AST) -> set[str]:
    """The unowned binaries some ``which(...)`` under ``node`` asks PATH for.

    Matched on the CALL, not on the import it arrived through: what this guard is about is "this test
    only runs if a binary is present", and whether the question was spelled ``shutil.which`` or a bare
    ``which`` is not part of that.
    """
    found: set[str] = set()
    for sub in ast.walk(node):
        if not isinstance(sub, ast.Call):
            continue
        if isinstance(sub.func, ast.Attribute):
            called = sub.func.attr
        elif isinstance(sub.func, ast.Name):
            called = sub.func.id
        else:
            continue
        if called != "which":
            continue
        for arg in sub.args:
            if isinstance(arg, ast.Constant) and arg.value in _UNOWNED_BINARIES:
                found.add(arg.value)
    return found


def _gate_constants(tree: ast.Module) -> dict[str, set[str]]:
    """Module-level names whose VALUE asks PATH for an unowned binary — ``name -> binaries``.

    The shape a gate takes once two binaries are wanted at once, or once several tests want the same
    answer: the module resolves it once and each test reads the NAME. A guard that only looked inside
    test bodies and decorators would see ``not _HTSLIB``, find no ``which`` anywhere near it, and wave
    the test through — the exact failure it exists to catch, arriving by the tidier spelling.
    """
    found: dict[str, set[str]] = {}
    for node in tree.body:
        if not isinstance(node, ast.Assign | ast.AnnAssign) or node.value is None:
            continue
        if not (binaries := _asks_path_for(node.value)):
            continue
        targets = node.targets if isinstance(node, ast.Assign) else [node.target]
        for target in targets:
            for name in ast.walk(target):
                if isinstance(name, ast.Name):
                    found[name.id] = binaries
    return found


def _is_external(fn: ast.FunctionDef | ast.AsyncFunctionDef) -> bool:
    """Does the ``external`` marker reach this test — by EITHER of the two routes the suite gives it?

    Written down as a decorator, or derived at collection time from a fixture the test takes: the
    session hook marks anything whose fixtures spawn a subprocess, and most of the suite's external
    tests carry no decorator at all. A guard reading only decorators would report every one of them
    as an offence on its first run, and a guard that cries wolf gets deleted.

    The fixture names are IMPORTED from the session configuration rather than re-spelled here, so a
    fixture joining that set brings this guard with it instead of leaving a second hand-maintained
    list to go stale.
    """
    for decorator in fn.decorator_list:
        marker = decorator.func if isinstance(decorator, ast.Call) else decorator
        if isinstance(marker, ast.Attribute) and marker.attr == "external":
            return True
    taken = {a.arg for a in (*fn.args.posonlyargs, *fn.args.args, *fn.args.kwonlyargs)}
    return bool(taken & _SPAWNS_SNAKEMAKE)


def _binary_gates(
    tree: ast.Module,
) -> list[tuple[ast.FunctionDef | ast.AsyncFunctionDef, list[str]]]:
    """Every test in ``tree`` that skips when a binary is missing, paired with the binaries.

    Both spellings, because both are on disk: the ``which`` inside the test or its decorators, and the
    module-level name holding the answer for it. Marker-blind on purpose — the guard needs this list
    to prove it is watching a tree that really has gates in it, which an offender list cannot do.
    """
    constants = _gate_constants(tree)
    gated: list[tuple[ast.FunctionDef | ast.AsyncFunctionDef, list[str]]] = []
    for fn in tree.body:
        if not isinstance(fn, ast.FunctionDef | ast.AsyncFunctionDef):
            continue
        if not fn.name.startswith("test_"):
            continue
        binaries = _asks_path_for(fn)
        for node in ast.walk(fn):
            if isinstance(node, ast.Name) and node.id in constants:
                binaries |= constants[node.id]
        if binaries:
            gated.append((fn, sorted(binaries)))
    return gated


def _unmarked_binary_gates(tree: ast.Module) -> list[str]:
    """Which tests in ``tree`` gate on an unowned binary without the ``external`` marker reaching them.

    The exact predicate ``test_every_gate_on_an_external_binary_carries_the_marker`` applies to every
    module in the test tree. Shared so its discriminator exercises the real guard, not a
    re-implementation of it. Takes the parsed module rather than its text so the test can walk the
    tree once and ask it two questions.
    """
    return [
        f"{fn.lineno} {fn.name} (gates on {', '.join(binaries)})"
        for fn, binaries in _binary_gates(tree)
        if not _is_external(fn)
    ]


@pytest.mark.repo
def test_every_gate_on_an_external_binary_carries_the_marker() -> None:
    """A skip is green, so the marker that routes a test to the lane which can run it must be total.

    The suite is split by ``external`` against its own negation, and that is a partition only because
    one selection is exactly the complement of the other. A test that gates itself on a binary and
    does not carry the marker therefore lands in the lane that was never going to have that binary,
    where it finds nothing on PATH, skips itself, and contributes a green result having checked
    nothing at all. Nothing raises, nothing is red, and the coverage the test's name claims is simply
    absent.

    This is not hypothetical here. The end-to-end proof that a UMI tag put on a read survives into
    the aligner's own output ran on no host this project's CI could reach, for the life of the repo,
    and the fragments finalize test passed only on a developer box that happened to carry htslib
    (#333). Both read green throughout. The lanes exist now and the external lane proves its binaries
    answer before it selects — but every one of those mechanisms sits downstream of the marker being
    right, and the marker is the one part a human writes by hand on the day they add a test.

    Two routes reach it and both are checked, because most external tests use the second: the
    decorator, and a fixture whose name the collection hook keys on. Two gate spellings are read for
    the same reason — the ``which`` inside a test or its decorators, and the module-level name that
    resolves it once for whoever needs it.
    """
    tests = Path(__file__).resolve().parent
    repo = tests.parent
    trees = {
        py: ast.parse(py.read_text(encoding="utf-8")) for py in sorted(tests.rglob("test_*.py"))
    }
    assert trees, "no test module was found -- this guard would pass over a tree it cannot see"

    offenders = [
        f"{py.relative_to(repo).as_posix()}:{found}"
        for py, tree in trees.items()
        for found in _unmarked_binary_gates(tree)
    ]
    assert not offenders, (
        "a test gates on a binary seqforge does not own and the `external` marker does not reach it:\n"
        + "\n".join(offenders)
        + "\n\nAdd `@pytest.mark.external`, or take a fixture the collection hook already marks. "
        "Without one of those the test sits in the lane that drops the externals, finds no binary "
        "there, skips itself, and reports green having run nothing -- which is how the aligner's "
        "end-to-end proof ran nowhere at all for the life of the repo (#333). The two lanes are a "
        "partition only while the marker is complete."
    )

    # ...and the tree really does hold gates, so an empty offender list is a marker that is complete
    # rather than a walk that found nothing to check. Asserted as a non-empty set of binaries rather
    # than a count or a list of test names: either of those is a number to keep in step with the
    # suite, and this is only trying to prove the walk has a subject.
    watched = {b for tree in trees.values() for _, bins in _binary_gates(tree) for b in bins}
    assert watched, (
        "no test in this tree gates on an unowned binary -- either the gates moved to a spelling this "
        "walk cannot see, or the walk broke; both leave the guard passing while it checks nothing"
    )

    # ...and the lane's own probe asks PATH for the same set, minus what the test environment itself
    # declares. Two lists in two languages, and the drift between them is silent in exactly the
    # direction that matters: a binary this guard knows about but the probe never asks for is one the
    # lane can be missing while still reporting green -- which is the failure both of them exist to
    # close, arriving through the mechanism built to close it.
    probe = (repo / "scripts" / "require_binaries.sh").read_text(encoding="utf-8")
    asked = re.search(r"for binary in ([^;]+);", probe)
    assert asked, "the probe no longer spells its binaries as a loop this can read"
    assert set(asked.group(1).split()) == _UNOWNED_BINARIES - _TEST_ENVIRONMENT_OWNS, (
        f"scripts/require_binaries.sh probes {sorted(asked.group(1).split())}, but the binaries whose "
        f"absence turns a test into a skip are {sorted(_UNOWNED_BINARIES - _TEST_ENVIRONMENT_OWNS)}. "
        "A binary in the second list and not the first is one the external lane can run without, "
        "skipping the tests that need it and exiting 0."
    )

    # ...and the guard discriminates. These call the REAL predicate: it must fire on both gate
    # spellings when nothing marks the test, and stay silent on each way a test legitimately carries
    # the marker -- including the fixture route, which shows up in no decorator. A guard nobody proved
    # fires is a guard that always allows.
    #
    # The cases are SOURCE TEXT handed to the predicate rather than functions in this module, which is
    # also what keeps this file's own scan honest: to the parser a `which` inside a string literal is
    # a constant and never a call, so the walk above reads this file and sees none of them.
    def fires(source: str) -> bool:
        return bool(_unmarked_binary_gates(ast.parse(source)))

    assert fires('def test_a(tmp_path):\n    star = shutil.which("STAR")\n')  # inside the body
    assert fires(
        '@pytest.mark.skipif(shutil.which("snakemake") is None, reason="not on PATH")\n'
        "def test_b():\n    pass\n"
    )  # inside a decorator
    assert fires(
        '_HTS = shutil.which("bgzip") is not None and shutil.which("tabix") is not None\n'
        '@pytest.mark.skipif(not _HTS, reason="no htslib")\n'
        "def test_c():\n    pass\n"
    )  # through a module-level name -- the spelling a body-only walk cannot see

    assert not fires('@pytest.mark.external\ndef test_d():\n    shutil.which("STAR")\n')  # marked
    assert not fires(
        '_HTS = shutil.which("bgzip") is not None\n'
        "@pytest.mark.external\n"
        '@pytest.mark.skipif(not _HTS, reason="no htslib")\n'
        "def test_e():\n    pass\n"
    )  # ...marked, and gated through the name: the shape actually on disk today
    assert not fires(
        'def test_f(dry_run):\n    assert shutil.which("snakemake")\n'
    )  # marked by its FIXTURE, which no decorator shows
    assert not fires(
        'def test_g():\n    for path in (shutil.which("bash"), "/bin/bash"):\n        pass\n'
    )  # bash is on every box: a parametrize source, not a gate
    assert not fires('def helper():\n    return shutil.which("STAR")\n')  # not a test


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

#: No file is exempt. `test_docs.py` used to be: its subject was the numbered rules, so a rule id there
#: was *data under test* rather than a pointer, and exempting it was the difference between "no comment
#: cites a rule by number" and "the rule table may not be tested". That test is gone — the rules are
#: stated once, in the router, and nothing parses their ids back out — so the carve-out now only buys
#: one file where a real pointer would go unseen. Kept as an empty set rather than deleted, because the
#: shape is the thing worth having: the day a file legitimately holds a rule id as data, it goes here
#: with its reason beside it.
_EXEMPT: frozenset[str] = frozenset()


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
        "the byte resolver, a benign twin; the per-context CONTEXT.md files are the glossary — and "
        "delete the comment "
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


def _published_artifact_suffixes() -> dict[str, str]:
    """Every filename suffix a ``workflows`` Python module OWNS and PUBLISHES — ``owner -> suffix``.

    **Discovered, not listed.** A hardcoded pair would be a third copy of the very strings this
    guard exists to stop copying, and it would police exactly the two artifacts somebody remembered
    on the day it was written. Read off the package instead: a name is in scope when its module
    exports it (it is in ``__all__``), it is spelled ``*_SUFFIX``, and its value is a filename
    suffix. A fourth aligner's module that publishes one is covered the moment it does.

    **Published is the line, and it is the owner's line to draw, not this guard's.** ``__all__`` is
    where a module says "this name is for someone else to use"; a private ``_FRAGMENTS_SUFFIX`` is a
    module's internal spelling, offered to a rule only through a function like ``fragments_suffixes``
    — so demanding a `.smk` import it would be this file deciding another module's export surface.
    Publishing one is therefore what puts it in scope, which is the same act as making it importable.

    **Every module of the package, at any depth.** ``iter_modules`` sees only the top level, which
    made "covered the moment it does" true for a writer that happens to sit directly under
    ``workflows`` and quietly false for one inside a sub-package — and a guard that is silently
    partial is worse than one that is absent, because it reads as coverage. ``walk_packages``
    imports what it walks, which is what this needs anyway to read a module's ``__all__``.
    """
    import importlib
    import pkgutil

    from seqforge import workflows

    found: dict[str, str] = {}
    names = [workflows.__name__] + [
        info.name for info in pkgutil.walk_packages(workflows.__path__, f"{workflows.__name__}.")
    ]
    for dotted in names:
        module = importlib.import_module(dotted)
        for name in getattr(module, "__all__", ()):
            value = getattr(module, name, None)
            if name.endswith("_SUFFIX") and isinstance(value, str) and value.startswith("."):
                found[f"{dotted.rsplit('.', 1)[-1]}.{name}"] = value
    return found


def _restates_a_published_suffix(source: str, suffixes: Mapping[str, str]) -> list[str]:
    """Where this Snakemake source spells a suffix a Python module already publishes.

    The exact predicate ``test_no_shipped_snakemake_module_restates_a_suffix_its_writer_owns``
    applies to every shipped ``.smk``, shared with its discriminator so what is proven to fire is the
    real thing.

    ``#`` comments are stripped first, for the reason ``keys_read_by`` strips them: these modules
    carry long prose headers about what they write, and a check that fires on a sentence describing
    the artifact is a check somebody deletes. The longest matching owner wins, so a line carrying
    ``.fragments.qc.json.gz`` is reported against the module that writes *that* and not also against
    the shorter suffix it happens to contain.
    """
    by_length = sorted(suffixes.items(), key=lambda kv: len(kv[1]), reverse=True)
    found: list[str] = []
    for i, line in enumerate(source.splitlines(), start=1):
        code = line.split("#")[0]
        for owner, suffix in by_length:
            if suffix in code:
                found.append(f"{i}: {suffix!r} is {owner}'s -- {code.strip()[:72]}")
                break
    return found


def test_no_shipped_snakemake_module_restates_a_suffix_its_writer_owns() -> None:
    """A rule declares the artifact name the code writing it owns, by importing it.

    A ``.smk`` that spells its output suffix and a Python module that spells the same suffix are two
    sources of truth for one fact, and the pair fails in the direction nothing catches: rename it in
    one place and the rule keeps producing a file the reader stops finding, which on a report page is
    byte-identical to a pipeline that has not run. Nothing raises and nobody is told.

    The mechanism was already there — both shipped modules import helpers from their own package at
    parse time, and ``h5ad_suffixes`` / ``fragments_suffixes`` have always decided what a rule
    declares. What was missing was the forcing function: three suffixes stayed hand-spelled because
    adopting the constant meant editing a shipped module, which bumps ``WORKFLOW_VERSION`` and
    invalidates every ``run_id``, and "do it on the next edit for a real reason" is a rule somebody
    has to remember. This is the check that remembers instead.

    Scanning the source text rather than parsing it is deliberate: a ``.smk`` is Snakemake, not
    Python, so ``ast.parse`` refuses it outright and there is no tree to walk.
    """
    suffixes = _published_artifact_suffixes()
    # The third is inside a sub-package and is why the discovery walks the whole tree: named here so
    # that a discovery narrowed back to the top level fails loudly instead of policing two of three.
    assert {"qc.QC_SUFFIX", "fragments.QC_SUFFIX", "extract.EXTRACT_SUFFIX"} <= set(suffixes), (
        f"the published artifact suffixes are no longer all discovered as constants "
        f"({sorted(suffixes)}); this guard would then police nothing while still passing"
    )

    shipped = sorted(_src_root().rglob("*.smk"))
    assert shipped, "no shipped .smk was found -- this guard would pass over an empty tree"

    offenders = {
        smk.name: found
        for smk in shipped
        if (found := _restates_a_published_suffix(smk.read_text(encoding="utf-8"), suffixes))
    }
    assert not offenders, (
        "a shipped Snakemake module restates a suffix its writer already owns:\n"
        + "\n".join(f"  {name} {line}" for name, lines in offenders.items() for line in lines)
        + "\nImport the constant from the module that writes the artifact, the way these files "
        "already import their other helpers, and remember that editing a shipped module means "
        "bumping WORKFLOW_VERSION. One owner is the point: the second spelling is the one that "
        "goes stale in silence, because a reader finding nothing looks exactly like a pipeline "
        "that never ran."
    )

    # ...and the guard discriminates. These call the REAL predicate against the REAL discovered set:
    # it must fire on each of the three hand-spellings this check removed, and stay silent on the
    # import that replaced them, on prose in a comment, and on a filename no module publishes. The
    # firing cases spell the suffix out, which is the whole point of a discriminator — a case built
    # from the same constant the guard reads could only ever prove the two agree with each other.
    def fires(source: str) -> bool:
        return bool(_restates_a_published_suffix(source, suffixes))

    assert fires('        expand(f"{OUTDIR}/{{sample}}/{{sample}}.qc.json.gz", sample=SAMPLES),')
    assert fires('        f"{OUTDIR}/{{sample}}/{{sample}}.qc.json.gz",')
    assert fires('        f"{OUTDIR}/{{sample}}/{{sample}}.fragments.qc.json.gz",')

    assert not fires('        f"{OUTDIR}/{{sample}}/{{sample}}{QC_SUFFIX}",')  # the fix itself
    assert not fires(
        "    # one gzipped .qc.json.gz per sample, then temp() drops the rest"
    )  # prose
    assert not fires('        temp("onlists/{name}.txt"),')  # a name no module publishes


_REPO = Path(__file__).resolve().parent.parent


def _pyproject() -> dict[str, Any]:
    """The project configuration, parsed. Three guards here read declarations out of it."""
    return tomllib.loads((_REPO / "pyproject.toml").read_text(encoding="utf-8"))


#: Tools ``liulab-runtime`` owns: the aligners this project's modules name, and the read/BAM
#: toolchain that ships beside them. Not a catalogue of bioinformatics — a package here means the
#: consumer boundary has been crossed, and every name is one a contributor plausibly reaches for while
#: trying to make an ``external`` test run.
_RUNTIME_OWNED = frozenset(
    {
        "star",
        "chromap",
        "bwa",
        "bwa-mem2",
        "bowtie2",
        "hisat2",
        "minimap2",
        "salmon",
        "kallisto",
        "samtools",
        "htslib",
        "sambamba",
        "bedtools",
        "fastp",
        "fastqc",
        "multiqc",
        "sra-tools",
    }
)


def _declared_packages() -> dict[str, list[str]]:
    """Every package name this project declares, keyed by the table that declares it.

    Both the workspace tables and every feature's, conda and PyPI alike — a feature is exactly how
    the reverted attempt smuggled an aligner in, so scanning only ``[tool.pixi.dependencies]`` would
    police the one table nobody was going to use.
    """
    pixi = _pyproject()["tool"]["pixi"]
    tables: dict[str, list[str]] = {}
    for key in ("dependencies", "pypi-dependencies"):
        if key in pixi:
            tables[f"tool.pixi.{key}"] = list(pixi[key])
    for name, feature in pixi.get("feature", {}).items():
        for key in ("dependencies", "pypi-dependencies"):
            if key in feature:
                tables[f"tool.pixi.feature.{name}.{key}"] = list(feature[key])
    return tables


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


#: What `npm install` drops in an asset directory. The rebuild procedure in both `VENDOR.md`s ends by
#: deleting the first two, and that is still the design -- node is a build-time tool for one generated
#: file and nothing from npm is a deliverable here. But a `rm` at the end of a procedure only runs if
#: the procedure finishes, and a Ctrl-C, a failed resolve, or a reader who stops after the install
#: step all leave the tree behind, inside the package source, where `packages = ["src/seqforge"]`
#: would carry it into the wheel.
_NPM_LEAVINGS = ("node_modules", "package-lock.json", "package.json")


@pytest.mark.repo
def test_no_npm_artifact_is_tracked_and_none_could_become_tracked() -> None:
    """Node stays a build-time tool, held shut from both ends.

    The two halves answer different failures and neither implies the other. **Nothing tracked** is the
    state that matters: an `npm` tree committed into `src/seqforge/` ships in the wheel. **Nothing
    ignorable** is what stops that state being reached, since the way it would happen is a broad
    `git add` during a half-finished rebuild rather than a deliberate commit.

    The ignore half is asked of *git*, never of a copy of git's patterns -- the same choice
    `test_nothing_tracked_escapes_the_type_checker` makes, and for the same reason: a second copy of
    a matcher agrees with the original right up until it does not.
    """
    tracked = subprocess.run(
        ["git", "ls-files", "-z"],
        cwd=_REPO,
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    committed = sorted(
        path
        for path in tracked.split("\0")
        if path and any(part in _NPM_LEAVINGS for part in path.split("/"))
    )
    assert not committed, (
        f"npm artifacts are committed: {committed}.\n"
        f"They ship inside `src/seqforge/`, so they reach the wheel. Delete them and finish the "
        f"rebuild procedure in the relevant `assets/VENDOR.md`, which ends by removing them."
    )

    # Every directory a documented build runs in, and every name that build drops there. Spelled out
    # rather than globbed: a THIRD vendored stylesheet is exactly the case this should be made to
    # think about, and a glob over today's tree would silently welcome it.
    #
    # `node_modules` is queried as a file INSIDE it, which is both what git can answer and what the
    # failure actually looks like. `git add` never adds a directory -- it adds the files under one --
    # and `git check-ignore` cannot apply a directory-only pattern (`node_modules/`) to a path that
    # does not exist on disk, so asking about the bare directory name would pass or fail depending on
    # whether someone happened to have a build lying around.
    build_dirs = ("src/seqforge/report/assets", "src/seqforge/evals/assets")
    would_land = [
        f"{d}/{name}"
        for d in build_dirs
        for name in ("node_modules/tailwindcss/package.json", "package-lock.json", "package.json")
    ]
    assert _not_gitignored(would_land) == [], (
        f"a half-finished `npm install` would leave sweepable files: {_not_gitignored(would_land)}.\n"
        f"Add them to .gitignore -- the `rm` ending the rebuild procedure only runs if the build does."
    )

    # ...and the guard discriminates, in the direction that actually costs something. An ignore rule
    # wide enough to swallow the build INPUTS would hide the source of record from git while every
    # assertion above still passed, so the sources are asserted visible by the same predicate.
    sources = [
        "src/seqforge/report/assets/report.src.css",
        "src/seqforge/report/assets/report.tw.css",
        "src/seqforge/evals/assets/eval-report.src.css",
        "src/seqforge/assets/sf-tokens.css",
    ]
    assert _not_gitignored(sources) == sorted(sources), (
        "the npm ignore patterns are wide enough to hide a build input -- the vendored stylesheet's "
        "own source of record would stop being tracked, silently."
    )


#: The pre-PR gate's runner. Nothing used to exercise it, and for as long as that was true it ran on
#: macOS having verified nothing and exited 0 -- a gate that errors loudly is recoverable, one that
#: says "ok" is not.
_GATE = _REPO / "scripts" / "check.sh"

#: A stand-in for `pixi`, placed first on PATH. The gate spawns `pixi run --no-progress <task>`, so
#: this answers as a task does and the RUNNER is what gets exercised rather than the four real steps
#: it normally drives. Four lines of output because the gate tails three from a green step and prints
#: a red one whole, which is a difference a test can see.
#:
#: `linger` backgrounds a GRANDCHILD and waits on it, because what a real step costs is not the
#: `pixi` process but the pytest run underneath it -- the orphans that were observed were pytest's.
_FAKE_PIXI = """#!/bin/sh
task=$3
echo "$task said one"
echo "$task said two"
echo "$task said three"
echo "$task said four"
case "$task" in
    boom) exit 7 ;;
    linger*)
        sleep 120 &
        echo $! >"$GATE_TEST_PIDS/$task.pid"
        wait
        ;;
esac
exit 0
"""


def _bash_interpreters() -> list[str]:
    """Every distinct bash on this box — the gate's failure was a bash-version failure.

    macOS ships 3.2 as ``/bin/bash`` and that is the one the gate silently no-opped under; a Linux
    runner has only its own bash 5, where the same script is fine. Asking the box rather than naming
    versions means the macOS case appears wherever a macOS box runs the suite and nowhere else.
    """
    seen: dict[str, str] = {}
    for path in (shutil.which("bash"), "/bin/bash"):
        if path and Path(path).exists():
            seen.setdefault(str(Path(path).resolve()), path)
    return sorted(seen.values())


def _gate_env(bin_dir: Path, tmpdir: Path, pid_dir: Path) -> dict[str, str]:
    """The environment a gate run gets: the fake `pixi` first on PATH, and a private ``TMPDIR``.

    ``TMPDIR`` is redirected so ``mktemp -d`` lands somewhere the test owns and "the gate cleaned up
    after itself" becomes a question about an empty directory.
    """
    env = dict(os.environ)
    env["PATH"] = f"{bin_dir}{os.pathsep}{env['PATH']}"
    env["TMPDIR"] = str(tmpdir)
    env["GATE_TEST_PIDS"] = str(pid_dir)
    return env


def _fake_pixi_dir(tmp_path: Path) -> Path:
    """A directory holding nothing but an executable `pixi` that answers as a task would."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    shim = bin_dir / "pixi"
    shim.write_text(_FAKE_PIXI, encoding="utf-8")
    shim.chmod(0o755)
    return bin_dir


def _alive(pid: int) -> bool:
    """Whether ``pid`` still exists. A signal we may not send is still a process that is running."""
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


@pytest.mark.repo
@pytest.mark.parametrize("bash", _bash_interpreters())
def test_the_gate_exits_non_zero_and_says_which_step_failed(bash: str, tmp_path: Path) -> None:
    """The pre-PR gate is a gate, and this is the test that says so.

    It went years with nothing exercising the runner itself, and under bash 3.2 -- what macOS ships
    as ``/bin/bash``, and what the maintainer's own gate runs on -- it declared an associative array
    the shell does not have, tripped ``set -u`` on the first verdict it tried to record, and exited
    **0** having collected nothing, printed no per-step verdict and no gate line. A gate that fails
    open is worse than no gate, because the green is believed.

    So the assertions are the three things a caller reads: the exit code, the per-step verdict, and
    the summary line. Checking only that a green run stays green would have passed throughout.

    Every bash on the box gets a case. The bug was invisible on bash 5 and fatal on bash 3.2, so a
    single interpreter is exactly the coverage that let it through.
    """
    bin_dir = _fake_pixi_dir(tmp_path)
    tmpdir = tmp_path / "tmp"
    tmpdir.mkdir()
    pid_dir = tmp_path / "pids"
    pid_dir.mkdir()
    env = _gate_env(bin_dir, tmpdir, pid_dir)

    red = subprocess.run(
        [bash, str(_GATE), "alpha", "boom", "gamma"],
        cwd=_REPO,
        env=env,
        capture_output=True,
        text=True,
        check=False,
        timeout=120,
    )
    out = red.stdout + red.stderr

    assert red.returncode != 0, (
        f"the gate reported success with a failing step. stdout+stderr:\n{out}\n"
        f"A gate that exits 0 having verified nothing is the failure this test exists for."
    )
    for verdict in ("=== alpha: ok ===", "=== boom: FAILED ===", "=== gamma: ok ==="):
        assert verdict in out, f"the gate printed no verdict for {verdict!r}. Output:\n{out}"
    assert "=== gate: alpha=ok boom=FAILED gamma=ok " in out, (
        f"the gate printed no summary line. Output:\n{out}"
    )

    # A green step's output is noise and a red one's is the whole point, so the shim says four lines
    # and only the failing step's first line survives the tail.
    assert "boom said one" in out, f"a failed step's output was truncated. Output:\n{out}"
    assert "alpha said one" not in out, f"a green step's output was printed whole. Output:\n{out}"
    assert "alpha said four" in out, f"a green step's tail is missing. Output:\n{out}"

    # ...and the gate discriminates: the same runner over steps that all pass is green, so the
    # non-zero above is the failing step and not the runner refusing everything.
    green = subprocess.run(
        [bash, str(_GATE), "alpha", "gamma"],
        cwd=_REPO,
        env=env,
        capture_output=True,
        text=True,
        check=False,
        timeout=120,
    )
    assert green.returncode == 0, f"the gate failed an all-green run:\n{green.stdout}{green.stderr}"
    assert "=== gate: alpha=ok gamma=ok " in green.stdout


@pytest.mark.repo
@pytest.mark.parametrize("bash", _bash_interpreters())
def test_an_interrupted_gate_leaves_no_step_running(bash: str, tmp_path: Path) -> None:
    """Whatever ends the gate, it takes its steps with it and its scratch directory goes last.

    The steps are background children writing into a ``mktemp -d``, and the cleanup used to delete
    that directory without touching them: when the runner died early it left four live test runs
    writing at a path that no longer existed, and they had to be killed by hand. A signal is the same
    hole reached the ordinary way -- Ctrl-C on a gate you have decided not to wait for.

    The process that must die is a **grandchild**. The gate's own child is `pixi`; the cost is the
    pytest run underneath it, which survives its parent unless the whole group is taken down.

    The scratch directory is asserted gone in the same breath, because "cleaned up" and "left nothing
    running" are the same property seen from two ends: a deleted directory with a live writer still
    pointed at it is the exact state that was observed.
    """
    bin_dir = _fake_pixi_dir(tmp_path)
    tmpdir = tmp_path / "tmp"
    tmpdir.mkdir()
    pid_dir = tmp_path / "pids"
    pid_dir.mkdir()

    proc = subprocess.Popen(
        [bash, str(_GATE), "linger-a", "linger-b"],
        cwd=_REPO,
        env=_gate_env(bin_dir, tmpdir, pid_dir),
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    grandchildren: list[int] = []
    try:
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline and len(list(pid_dir.glob("*.pid"))) < 2:
            time.sleep(0.05)
        grandchildren = [int(p.read_text().strip()) for p in sorted(pid_dir.glob("*.pid"))]
        assert len(grandchildren) == 2, (
            f"the gate did not start both steps; got {grandchildren}. The rest of this test says "
            f"nothing unless there is something running to leave behind."
        )

        proc.send_signal(signal.SIGINT)
        proc.communicate(timeout=60)

        deadline = time.monotonic() + 15
        while time.monotonic() < deadline and any(_alive(pid) for pid in grandchildren):
            time.sleep(0.05)
        survivors = [pid for pid in grandchildren if _alive(pid)]
        assert not survivors, (
            f"the gate exited leaving {survivors} running. Those are the orphaned test runs: the "
            f"cleanup has to take the steps' process groups down, not only the `pixi` processes."
        )
    finally:
        for pid in grandchildren:
            try:
                os.kill(pid, signal.SIGKILL)
            except OSError:
                pass
        if proc.poll() is None:
            proc.kill()
            proc.communicate()

    assert not list(tmpdir.iterdir()), (
        f"the gate left its scratch directory behind: {[p.name for p in tmpdir.iterdir()]}."
    )


#: Constructs bash 4 introduced. Not an exhaustive list of what 3.2 lacks -- the parametrized runs
#: above are the real guard, and on a macOS box they go red on any of these the moment it is
#: reached. This one fires on a Linux runner too, where the gate's own steps are never in doubt but
#: nobody's `bash` can notice, and it names the construct that actually caused the silent no-op.
_BASH_4_ONLY = (
    re.compile(r"^\s*(declare|local|typeset)\s+-[A-Za-z]*A"),
    re.compile(r"^\s*(mapfile|readarray)\b"),
)


@pytest.mark.repo
def test_the_gate_runner_stays_within_the_bash_macos_ships() -> None:
    """Every shell script the gate runs must RUN on bash 3.2, not merely refuse loudly there.

    macOS ships 3.2 as ``/bin/bash`` and that is where the maintainer works, so a version guard would
    only relocate the outage. What broke was `declare -A`: an associative array in a script whose
    ordered step list already indexes everything it needs, and whose failure to declare one was
    invisible because ``set -e`` is deliberately absent -- the runner has to collect *every* step's
    status before it reports, so it cannot abort on the first thing that goes wrong.

    Nothing here needs a map. The steps arrive as an ordered list and every verdict is read back in
    that order, so a plain indexed array parallel to it carries the same information on every shell.

    EVERY script, not only the runner. The second one to arrive was the external lane's binary probe,
    whose own header promises it uses no empty array -- and a promise in a comment is what this guard
    exists to replace. A script reached through a task the gate names fails the same way the runner
    did, and a scan keyed on one filename would not have looked.
    """
    scripts = sorted(_GATE.parent.glob("*.sh"))
    assert len(scripts) > 1, (
        "this walk found at most the runner -- it is keyed on a directory so that a new script is "
        "covered the day it lands, and finding one back means the glob no longer matches them"
    )
    offenders = [
        f"{path.name}: {line}"
        for path in scripts
        for line in path.read_text(encoding="utf-8").splitlines()
        if any(pattern.search(line) for pattern in _BASH_4_ONLY)
    ]
    assert not offenders, (
        f"a gate script uses bash 4 syntax macOS's /bin/bash does not have: {offenders}.\n"
        f"On bash 3.2 this does not abort the script -- there is no `set -e` and there must not be, "
        f"so it runs on and fails somewhere that looks like success. The step list is ordered; index "
        f"a parallel array by position instead."
    )

    # ...and the guard discriminates, on the line this was actually reported for.
    assert [line for line in ["declare -A status"] if _BASH_4_ONLY[0].search(line)]


@pytest.mark.repo
def test_no_exported_model_emits_a_key_its_schema_does_not_declare() -> None:
    """``schema export`` is the schema (R1), so no stdout object may hold a key it does not describe.

    ``export_schema`` calls ``model_json_schema()``, which is pydantic's **validation** schema. A
    ``computed_field`` lands in ``model_dump`` and in the **serialization** schema only — so adopting
    one puts a key on a machine surface the exported schema never mentions, and a consumer validating
    seqforge's own stdout against ``schema export`` would be right to reject a document seqforge
    wrote. Nothing raises on the way: the dump succeeds, the schema exports, both look healthy, and
    only a validator downstream ever finds out.

    The rule is not *never derive a field*. It is that a derived value is either serialised **and**
    exported, or neither. :attr:`~seqforge.models.resolve.ComposeResult.kb_moved` takes the second
    road deliberately — a plain ``property``, so Python callers get one spelling of the comparison
    while the JSON surface carries the two versions it is read off, both exported and both required.
    This is the check that stops the first road being taken by accident, since taking it is a
    one-word decorator and its cost appears nowhere near it.
    """
    from seqforge.models import SCHEMA_MODELS

    offenders = {
        name: sorted(undeclared)
        for name, model in SCHEMA_MODELS.items()
        if (
            undeclared := set(model.model_json_schema(mode="serialization").get("properties", {}))
            - set(model.model_json_schema().get("properties", {}))
        )
    }
    assert not offenders, (
        f"these exportable models emit keys `schema export` does not declare: {offenders}.\n"
        f"A `computed_field` serialises but exports only in serialization mode, so the contract and "
        f"the object disagree with nothing raising. Make it a plain `property` (derived, not "
        f"serialised) and let the JSON carry the fields it is computed from."
    )

    # ...and the guard discriminates, against a model shaped like the mistake it exists to catch.
    class _Computed(BaseModel):
        a: int

        @computed_field  # type: ignore[prop-decorator]
        @property
        def b(self) -> int:
            return self.a

    assert set(_Computed.model_json_schema(mode="serialization")["properties"]) - set(
        _Computed.model_json_schema()["properties"]
    ) == {"b"}
