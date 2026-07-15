"""Processing policy — the default a user gets when they instruct nothing (R15).

seqforge picks the best default pipeline option; a user instruction overrides it. This module is the
"picks" half: small, explicit, and derived. The aligner and runtime env follow from the KB's backend
module, never from a guess, and the runtime env is a **literal** ``liulab-runtime`` name (R9/R12) —
there is no profile-indirection layer to invent here.
"""

from __future__ import annotations

from dataclasses import dataclass

from ..kb.schema import Spec
from ..models.processing import BulkQuant, Quantification, RuntimeEnv, SoloFeature, SoloQuant

#: KB backend module -> the aligner name recorded in `processing.aligner`.
_ALIGNER_FOR_MODULE = {"map/starsolo": "starsolo", "map/star": "star"}

DEFAULT_SOLO_FEATURES: tuple[SoloFeature, ...] = (
    "Gene",
    "GeneFull",
    "GeneFull_ExonOverIntron",
    "GeneFull_Ex50pAS",
    "Velocyto",
)
"""Count everything; do not ask which (R15).

One alignment, five counting rules, one pass. Download and alignment dominate the cost by orders of
magnitude, and count matrices are small — so we emit every answer and let the consumer choose. That
**dissolves** the cells-vs-nuclei question rather than answering it, which is the sibling of the §12
benign rule: §12 says never escalate an ambiguity that cannot change the output; this says never
escalate one whose every answer you can afford to emit.

We measured the alternative. ``--soloFeatures Gene`` silently discards **40.7 %** of a nuclear library
(`kb e2e-introns` on ce11: Gene=1186 exonic-only vs GeneFull=1940). STARsolo exits 0 and the matrix
merely looks thin — the same failure shape as a strand inversion.

**Exactly scRecounter's five, in scRecounter's order, and deliberately no SJ.** Reasons, ranked:

1. ``Gene`` first, so the primary matrix matches the common whole-cell expectation, while
   ``GeneFull`` sits right there for the nuclear case. Order only names the primary; nothing is
   dropped.
2. It satisfies STARsolo's "Velocyto requires Gene" by construction.
3. **SJ is out, and the reason belongs in code rather than inherited silently.** A splice-junction
   matrix has a *different feature axis* — it is not a drop-in alternative count of the same thing,
   nothing downstream consumes it, and it costs disk for no training signal today. One entry away if
   that changes. scRecounter's five is a *precedent*, not a derivation; adopting it wholesale without
   saying this would import someone else's unstated scope decision.
4. Following a known-good precedent that runs at scale on real public data beats our own reasoning
   here, and it is citable: ArcInstitute/scRecounter, workflows/star_full.nf.

The cost is real and is being measured, not assumed — Velocyto in particular is not free. If it proves
pathological the default drops to four and ``--quantify`` restores it: an expensive default is not a
trap precisely *because* the processing manifest exists to override it.
"""


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
        SoloQuant(features=list(DEFAULT_SOLO_FEATURES))
        if module == "map/starsolo"
        else BulkQuant(mode="GeneCounts")
    )
    return ProcessingDefaults(
        module=module,
        aligner=aligner,
        quantification=quantification,
        environment=environment,
        variant_calling=False,
    )
