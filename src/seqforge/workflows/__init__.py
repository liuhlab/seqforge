"""``workflows`` — hand-written, versioned, CI-tested Snakemake modules (NEVER generated).

The composer selects a module by id and emits its ``config.yaml`` + ``units.tsv``; it never writes
rule source. Aligner *environments* and genome *indexes* belong to ``liulab-runtime`` / ``liulab-genome``
and resolve at run time — a module names an env and an assembly id, never a path.

The registry (``MODULES``) is the first code in this file. What a module is (``WorkflowModule``)
and the scanners that read its config contract off its source live in ``workflows.module``, and
every public name there is importable from here.

``WORKFLOW_VERSION`` is CalVer and is stamped into a **recipe's** provenance when the recipe is
written, so a compiled config is bound to the module source it was authored against.

**What a bump does, stated once, here.** ``run_id`` folds the stamp the RECIPE RECORDED, never the
live constant. So a bump re-keys nothing: every recipe already on disk keeps its stamp, its
``run_id`` and its pipeline directory, and the alignments under that directory stay where they are
and stay findable. A bump changes only what the NEXT recipe records. The way to take up new module
behaviour is therefore to author a new recipe — composing the old one again does not silently
re-stage the new bytes over the old, it refuses, because a pipeline directory is written once.

That is the whole cost model, and it is the opposite of the one the older entries of
``WORKFLOW_VERSION``'s history assumed (#447). They priced an edit as "one round of ``run_id``
invalidation", meaning every composed dataset re-keyed itself and reprocessed. Nothing ever did
that. The real price of editing a shipped module is paid per pipeline that wants the edit, by
whoever wants it, in a new recipe.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from .module import (
    _CHROMAP_PARSE_KEYS,
    _MODULE_DIR,
    _STARSOLO_PARSE_KEYS,
    PLATE_COMPONENT_H5AD,
    PLATE_H5AD,
    RUNTIME_IMAGE,
    ReadLayoutKind,
    WorkflowModule,
    argv_keys_read_by,
    container_uri,
    keys_read_by,
)

if TYPE_CHECKING:
    from ..kb.schema import Spec

#: CalVer YYYY.M.PATCH; bump when any shipped module's rules/params change. History: `docs/research/workflows-version-history-2026-11-25.md`.
WORKFLOW_VERSION = "2026.8.25"

MODULES: dict[str, WorkflowModule] = {
    "map/starsolo": WorkflowModule(
        name="map/starsolo",
        version=WORKFLOW_VERSION,
        env="align-rna",
        snakefile=_MODULE_DIR / "map" / "starsolo.smk",
        read_layout_kind="barcoded",
        argv_source=Path(__file__).parent / "starsolo_args.py",
        parse_keys=_STARSOLO_PARSE_KEYS,
    ),
    "map/star": WorkflowModule(
        name="map/star",
        version=WORKFLOW_VERSION,
        env="align-rna",
        snakefile=_MODULE_DIR / "map" / "star.smk",
        read_layout_kind="mates",
    ),
    "map/chromap": WorkflowModule(
        name="map/chromap",
        version=WORKFLOW_VERSION,
        env="align-dna",
        snakefile=_MODULE_DIR / "map" / "chromap.smk",
        read_layout_kind="atac_barcoded",
        parse_keys=_CHROMAP_PARSE_KEYS,
        serves_modalities=frozenset({"atac", "multi"}),
    ),
    # The pre-demultiplexed, one-cell-one-file pipeline. `align-rna` is forced rather than chosen —
    # STAR is what needs the image, and the extractor and the counter shell out to nothing at all.
    # `parse_keys` is EMPTY and stays empty: every knob the extractor needs is already in the element
    # model, so the whole geometry is DERIVED into one config key rather than declared six times over
    # (see `compose.params.DERIVED_PARAM_KEYS`). A backend that declares one is refused at load.
    "map/star-umi": WorkflowModule(
        name="map/star-umi",
        version=WORKFLOW_VERSION,
        env="align-rna",
        snakefile=_MODULE_DIR / "map" / "star-umi.smk",
        read_layout_kind="umi_tagged",
        fan_in_artifact=PLATE_H5AD,
        # The one module that can keep a chimera's Components apart downstream of the aligner, so it
        # is the one module that declares a twin. The other three still declare nothing and a recipe
        # naming a chimera against any of them is a compose refusal — better a refusal than a count
        # table merging two organisms with nothing in it saying which reads were whose.
        chimeric_variant="map/star-umi-chimera",
    ),
    # The chimera-aware twin, reachable ONLY through the declaration above: no KB backend may name it
    # (`Backend._not_a_chimeric_variant` refuses one at load), so nothing bypasses the single dispatch
    # rule in `compose.plan`. Its `parse_keys` are empty for the base's reason and one more — with no
    # backend able to name it, there is no declarer left for a namespace to serve.
    #
    # Everything else it declares is the base's, and must be: same env because it runs the same STAR,
    # same read layout because it maps the same plate, same modalities. What differs is the fan-in
    # artifact's ARITY — one object per Component instead of one for the deposit — which is why the
    # name carries a `{component}` and why `rule all` demands each one by name.
    "map/star-umi-chimera": WorkflowModule(
        name="map/star-umi-chimera",
        version=WORKFLOW_VERSION,
        env="align-rna",
        # The basename is FORCED rather than chosen: `CompiledPipeline.module` inverts a run
        # directory's module identity out of the copied `.smk`'s basename, so a file named anything
        # else would compile and then report as no module at all.
        snakefile=_MODULE_DIR / "map" / "star-umi-chimera.smk",
        read_layout_kind="umi_tagged",
        fan_in_artifact=PLATE_COMPONENT_H5AD,
    ),
}

#: The module ids that are a twin of some other module — **derived from the registry above**, so
#: there is no second list for a new twin to be missing from. A KB backend naming one of these is
#: refused: the twin is selected by ``compose.plan`` swapping it in for its base under a chimeric
#: assembly, and a spec naming it directly would reach the same module by a route that never asked
#: whether the recipe's reference is a chimera at all.
CHIMERIC_VARIANTS: frozenset[str] = frozenset(
    m.chimeric_variant for m in MODULES.values() if m.chimeric_variant is not None
)


def get_module(name: str) -> WorkflowModule:
    """Return the workflow module registered under ``name`` (raises ``KeyError`` if unknown)."""
    try:
        return MODULES[name]
    except KeyError as exc:
        known = ", ".join(sorted(MODULES))
        raise KeyError(f"unknown workflow module {name!r}; known: {known}") from exc


def parse_keys_for(module: str) -> frozenset[str]:
    """The parse-param namespace a backend on ``module`` may declare (raises for an unknown module).

    The single source of truth for the parse/count line — consulted by the KB DSL validator
    (``Backend._only_parse_keys``) and the composer's ``params_gate`` alike, so both police one namespace
    per pipeline rather than a global set that every pipeline had to share.
    """
    return get_module(module).parse_keys


def resolve_pipeline(spec: Spec) -> WorkflowModule:
    """Bind an identified chemistry to the pipeline that runs it — the assay↔pipeline adapter.

    ``get_module`` plus one invariant: the spec's modality must be one the pipeline serves. That check
    is the whole reason the adapter exists — it makes "an ATAC chemistry composed against STARsolo"
    a loud refusal at compose time instead of a wrong command line, the same class of silent
    fall-through that ``read_layout_kind`` and ``param_block`` were built to kill. Raises ``KeyError``
    (which the composer surfaces as a ``ComposeError``) for an unknown module or an unserved modality.
    """
    module = get_module(spec.require_backend().module)
    modality = spec.identity.modality
    if modality not in module.serves_modalities:
        raise KeyError(
            f"pipeline {module.name!r} serves modalities {sorted(module.serves_modalities)}, not "
            f"{modality!r} (chemistry {spec.identity.id!r}); a chemistry must be composed against a "
            f"pipeline that serves its modality"
        )
    return module


def list_modules() -> list[str]:
    """Every registered workflow module name, sorted."""
    return sorted(MODULES)


__all__ = [
    "CHIMERIC_VARIANTS",
    "MODULES",
    "PLATE_COMPONENT_H5AD",
    "PLATE_H5AD",
    "RUNTIME_IMAGE",
    "WORKFLOW_VERSION",
    "ReadLayoutKind",
    "WorkflowModule",
    "argv_keys_read_by",
    "container_uri",
    "get_module",
    "keys_read_by",
    "list_modules",
    "parse_keys_for",
    "resolve_pipeline",
]
