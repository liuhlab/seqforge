"""Processing policy — the ``inferred`` third section (design §1.6: authority = derived + policy).

``library`` is what the bytes say and ``experiment`` is what humans/DBs say; ``processing`` is *intent*,
derived from those two plus policy defaults. Kept deliberately small and explicit: the aligner and
runtime env follow from the KB's backend module, never from a guess. The runtime env is a **literal**
``liulab-runtime`` name (R9/R12) — there is no profile-indirection layer to invent here.
"""

from __future__ import annotations

from dataclasses import dataclass

from ..kb.schema import Spec
from ..models.processing import BulkQuant, Quantification, RuntimeEnv, SoloQuant

#: KB backend module -> the aligner name recorded in `processing.aligner`.
_ALIGNER_FOR_MODULE = {"map/starsolo": "starsolo", "map/star": "star"}


@dataclass(frozen=True)
class ProcessingDefaults:
    """Policy-derived processing intent for one chemistry."""

    module: str
    aligner: str
    quantification: Quantification
    environment: RuntimeEnv
    variant_calling: bool


def processing_defaults(spec: Spec) -> ProcessingDefaults:
    """Derive the processing section's policy defaults from the identified chemistry's backend."""
    module = spec.backend.module
    aligner = _ALIGNER_FOR_MODULE.get(module, module.rsplit("/", 1)[-1])
    # Every Milestone-0 technology is RNA; ATAC/multiome would select a different env here.
    environment: RuntimeEnv = "align-rna"
    # Counting is MODULE-scoped: soloFeatures is meaningless to plain STAR, and quantMode is
    # meaningless to STARsolo. A processing manifest that carried one shape unconditionally would be
    # a type error the moment it met the other module.
    quantification: Quantification = (
        SoloQuant(features=["Gene"]) if module == "map/starsolo" else BulkQuant(mode="GeneCounts")
    )
    return ProcessingDefaults(
        module=module,
        aligner=aligner,
        quantification=quantification,
        environment=environment,
        variant_calling=False,
    )
