"""Repo-wide R10 invariants — seqforge is a CONSUMER of the liulab stack, not a parallel universe.

AST/attribute guards that read ``src/seqforge`` and never compose anything: it defines no genome
machinery and no aligner environments of its own (those belong to ``liulab-genome`` /
``liulab-runtime``), and every ``liulab-genome`` attribute it calls really exists on the imported
class. The ``src_trees`` AST parse and ``_src_root`` are shared from ``tests/conftest.py``.
"""

from __future__ import annotations

import ast

import pytest

from conftest import SrcTrees, _src_root

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
