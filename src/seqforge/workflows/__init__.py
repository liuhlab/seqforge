"""``workflows`` — hand-written, versioned, CI-tested Snakemake modules (NEVER generated, R1/R12).

The composer selects a module by id and emits its ``config.yaml`` + ``units.tsv``; it never writes
rule source. Aligner *environments* and genome *indexes* belong to ``liulab-runtime`` / ``liulab-genome``
and resolve at run time (R9/R12) — a module names an env and an assembly id, never a path.

``WORKFLOW_VERSION`` is CalVer and is folded into a manifest's provenance so a compiled config is
bound to the exact module source that will run it.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from functools import cache
from pathlib import Path
from typing import Literal

from ..models.processing import RuntimeEnv

#: CalVer YYYY.M.PATCH; bump when any shipped module's rules/params change.
#: 2026.7.3 — `required_config` is COMPUTED from the module source instead of typed beside it, so
#: over- and under-declaration are both impossible rather than one being tested. `units_tsv` joins it
#: (the composer emits it now; no wrapper injects it). `read_layout_kind` replaces the hardcoded
#: `module == "map/starsolo"` branch in the composer.
#: 2026.7.2 — starsolo's required_config gains the four soloCB/UMI keys starsolo.smk has always
#: dereferenced and never declared. The contract was wrong, not the module.
#: 2026.7.1 — star.smk hardcodes --outSAMtype (it is a module detail, and starsolo.smk always
#: hardcoded it); required_config gains primary_feature and drops bulk.outSAMtype.
WORKFLOW_VERSION = "2026.7.3"

_MODULE_DIR = Path(__file__).parent


@cache
def keys_read_by(snakefile: Path) -> frozenset[str]:
    """The dotted config keys a module actually reads, **derived from its source**.

    Two forms, because the modules use both: `config["a"]["b"]` directly, and the indirection
    `params: solo=config["solo"]` followed by `{params.solo[soloCBlen]}` in the shell block.

    Comments are stripped first, and that is not fussiness — starsolo.smk's own header prose says
    "every chemistry-defining knob arrives via `config["solo"]`", which a naive scan reads as a bare
    read of the whole block. A check that cries wolf gets deleted.
    """
    code = "\n".join(line.split("#")[0] for line in snakefile.read_text().splitlines())
    keys: set[str] = set()

    # A bare `<name> = config["<section>"]` binds the whole block to a name. Track those, including
    # one rebinding hop (`SOLO = config["solo"]` at module level, then `solo=SOLO` in a params
    # block), because that chain is exactly how the shell reaches `{params.solo[soloType]}`.
    # The lookahead matters: `ASSEMBLY = config["genome"]["assembly"]` is a nested read, not a
    # binding, and must fall through to the direct scan below.
    bound: dict[str, str] = dict(re.findall(r'(\w+)\s*=\s*config\["(\w+)"\](?!\[)', code))
    for name, src in re.findall(r"^\s*(\w+)\s*=\s*(\w+)\s*,?\s*$", code, re.M):
        if src in bound:
            bound.setdefault(name, bound[src])

    for name, section in bound.items():
        # `{params.<name>[<key>]}` in a shell block, or `<NAME>["<key>"]` in Python.
        subscripts = set(re.findall(rf"\{{params\.{name}\[(\w+)\]\}}", code)) | set(
            re.findall(rf"""\b{name}\[["'](\w+)["']\]""", code)
        )
        # Subscripted -> it is a block alias and each subscript is the real read. Never subscripted
        # -> it was a scalar read all along (`OUTDIR = config["outdir"]`), so the section IS the key.
        keys |= {f"{section}.{k}" for k in subscripts} or {section}

    # Direct reads: config["a"]["b"] -> a.b | config["a"] -> a. Binding sites are already accounted
    # for above, so drop them here rather than double-count the block as a bare key.
    direct = re.sub(r'\w+\s*=\s*config\["\w+"\](?!\[)', "", code)
    for section, sub in re.findall(r'config\["(\w+)"\](?:\["(\w+)"\])?', direct):
        keys.add(f"{section}.{sub}" if sub else section)

    return frozenset(keys)


@dataclass(frozen=True)
class WorkflowModule:
    """One selectable workflow module: its id, version, runtime env, Snakefile, and config contract."""

    name: str
    version: str
    env: RuntimeEnv
    snakefile: Path
    #: How this module wants its reads handed to the aligner:
    #:
    #: - ``barcoded`` — ``{cdna, barcode}``, chosen by ROLE (a barcoded single-cell chemistry).
    #: - ``paired``   — ``{mate1, mate2}``, chosen by ORDER (a bulk paired-end library).
    #:
    #: This is deliberately a small closed literal and **not** a general plugin interface: there are
    #: two modules and both are STAR, so any richer abstraction would be generalised from a sample
    #: size of one. What it does buy is that the composer no longer branches on
    #: ``spec.backend.module == "map/starsolo"`` — a string compare in which every module that was not
    #: starsolo silently fell into the bulk mate1/mate2 branch and emitted a wrong command line. A
    #: third module must now pick a kind, or add one, and either is a typed and visible choice.
    read_layout_kind: Literal["barcoded", "paired"]

    @property
    def required_config(self) -> tuple[str, ...]:
        """Dotted config keys the module reads — the composer must emit every one.

        **Computed from the module source, never declared.** This was a hand-written tuple, checked in
        one direction against a scanner that lived in the test suite. It under-declared the four
        soloCB/UMI keys `starsolo.smk` has always dereferenced (a `KeyError` on a compute node, long
        after compose exited 0), and it over-declared `primary_feature` and `env`, which no rule
        reads. Both directions now close by construction: there is one list, and the module source is
        it. A hand-maintained list of what the code does is a comment with a tuple's syntax.

        Deriving is only safe because the module now *executes*: `kb e2e` runs this Snakefile against
        real reads and a ground-truth matrix, so a key this scanner misses fails loudly there. The two
        are complementary — `kb e2e` exercises one chemistry's branch, this covers both statically.
        """
        return tuple(sorted(keys_read_by(self.snakefile)))


MODULES: dict[str, WorkflowModule] = {
    "map/starsolo": WorkflowModule(
        name="map/starsolo",
        version=WORKFLOW_VERSION,
        env="align-rna",
        snakefile=_MODULE_DIR / "map" / "starsolo.smk",
        read_layout_kind="barcoded",
    ),
    "map/star": WorkflowModule(
        name="map/star",
        version=WORKFLOW_VERSION,
        env="align-rna",
        snakefile=_MODULE_DIR / "map" / "star.smk",
        read_layout_kind="paired",
    ),
}


def get_module(name: str) -> WorkflowModule:
    """Return the workflow module registered under ``name`` (raises ``KeyError`` if unknown)."""
    try:
        return MODULES[name]
    except KeyError as exc:
        known = ", ".join(sorted(MODULES))
        raise KeyError(f"unknown workflow module {name!r}; known: {known}") from exc


def list_modules() -> list[str]:
    return sorted(MODULES)


__all__ = [
    "WORKFLOW_VERSION",
    "WorkflowModule",
    "MODULES",
    "get_module",
    "keys_read_by",
    "list_modules",
]
