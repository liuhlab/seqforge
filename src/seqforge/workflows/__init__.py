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
WORKFLOW_VERSION = "2026.7.0"

_MODULE_DIR = Path(__file__).parent


@dataclass(frozen=True)
class WorkflowModule:
    """One selectable workflow module: its id, version, runtime env, Snakefile, and config contract."""

    name: str
    version: str
    env: RuntimeEnv
    snakefile: Path
    #: dotted config keys the module reads — the composer must emit every one (checked in CI).
    required_config: tuple[str, ...]


MODULES: dict[str, WorkflowModule] = {
    "map/starsolo": WorkflowModule(
        name="map/starsolo",
        version=WORKFLOW_VERSION,
        env="align-rna",
        snakefile=_MODULE_DIR / "map" / "starsolo.smk",
        required_config=(
            "solo.soloType",
            "solo.soloCBwhitelist",
            "solo.soloStrand",
            "solo.soloFeatures",
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
            "bulk.outSAMtype",
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
