"""``workflows`` — hand-written, versioned, CI-tested Snakemake modules (NEVER generated, R1/R12).

The composer selects a module by id and emits its ``config.yaml`` + ``units.tsv``; it never writes
rule source. Aligner *environments* and genome *indexes* belong to ``liulab-runtime`` / ``liulab-genome``
and resolve at run time (R9/R12) — a module names an env and an assembly id, never a path.

``WORKFLOW_VERSION`` is CalVer and is folded into a manifest's provenance so a compiled config is
bound to the exact module source that will run it.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from ..models.processing import RuntimeEnv

#: CalVer YYYY.M.PATCH; bump when any shipped module's rules/params change.
#: 2026.7.2 — starsolo's required_config gains the four soloCB/UMI keys starsolo.smk has always
#: dereferenced and never declared. The contract was wrong, not the module.
#: 2026.7.1 — star.smk hardcodes --outSAMtype (it is a module detail, and starsolo.smk always
#: hardcoded it); required_config gains primary_feature and drops bulk.outSAMtype.
WORKFLOW_VERSION = "2026.7.2"

_MODULE_DIR = Path(__file__).parent


@dataclass(frozen=True)
class WorkflowModule:
    """One selectable workflow module: its id, version, runtime env, Snakefile, and config contract."""

    name: str
    version: str
    env: RuntimeEnv
    snakefile: Path
    #: Dotted config keys the module reads — the composer must emit every one. Derived from the
    #: module source and checked by ``test_required_config_covers_every_key_the_module_reads``:
    #: this list was hand-maintained until it under-declared four keys `starsolo.smk` dereferences,
    #: which is a `KeyError` on a compute node long after compose exited 0. ``units_tsv`` is
    #: deliberately absent — the run wrapper injects it, the composer does not emit it.
    required_config: tuple[str, ...]


MODULES: dict[str, WorkflowModule] = {
    "map/starsolo": WorkflowModule(
        name="map/starsolo",
        version=WORKFLOW_VERSION,
        env="align-rna",
        snakefile=_MODULE_DIR / "map" / "starsolo.smk",
        required_config=(
            "solo.soloType",
            # CB/UMI geometry, spelled two ways because STARsolo spells it two ways: start/len for a
            # simple chemistry, a position quadruple for a combinatorial one. Every chemistry needs
            # exactly ONE of these groups and cannot supply the other — so this tuple is the union of
            # what the module MAY read, and `param_owners` decides which apply to a given spec.
            "solo.soloCBstart",
            "solo.soloCBlen",
            "solo.soloUMIstart",
            "solo.soloUMIlen",
            "solo.soloCBposition",
            "solo.soloUMIposition",
            "solo.soloCBwhitelist",
            "solo.soloStrand",
            "solo.soloFeatures",
            "primary_feature",
            "read_files_in.cdna",
            "read_files_in.barcode",
            "genome.assembly",
            "genome.annotation",
            "env",
            "threads",
            "outdir",
        ),
    ),
    "map/star": WorkflowModule(
        name="map/star",
        version=WORKFLOW_VERSION,
        env="align-rna",
        snakefile=_MODULE_DIR / "map" / "star.smk",
        required_config=(
            "bulk.quantMode",
            "read_files_in.mate1",
            "read_files_in.mate2",
            "genome.assembly",
            "genome.annotation",
            "env",
            "threads",
            "outdir",
        ),
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


__all__ = ["WORKFLOW_VERSION", "WorkflowModule", "MODULES", "get_module", "list_modules"]
