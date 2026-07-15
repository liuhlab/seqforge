"""Processing policy — the default a user gets when they instruct nothing (R15).

seqforge picks the best default pipeline option; a user instruction overrides it. This module is the
"picks" half: small, explicit, and derived. The aligner and runtime env follow from the KB's backend
module, never from a guess, and the runtime env is a **literal** ``liulab-runtime`` name (R9/R12) —
there is no profile-indirection layer to invent here.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from ..kb.schema import Spec
from ..models.base import Basis
from ..models.blocker import BlockerSubject, ValidationWarning
from ..models.dataset import DatasetManifest
from ..models.evidenced import EvidencedBool, EvidencedStr
from ..models.processing import (
    BulkQuant,
    EvidencedGenome,
    EvidencedQuantification,
    EvidencedRuntimeEnv,
    GenomeRef,
    ProcessingSection,
    Quantification,
    ResourceHints,
    RuntimeEnv,
    SoloFeature,
    SoloQuant,
)
from .instruct import Instruction


class PolicyError(RuntimeError):
    """Intent cannot be resolved — a required choice has no safe default (R4: refuse, don't guess)."""


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

**The cost is now measured, and it is ~nothing (2026-07-15).** `kb e2e-cost` on hg38 + GENCODE v50 +
10x 3' v3, all five features, 16 threads, 5 000 cells, reads simulated from real hg38 sequence with
barcodes drawn from the real 6 794 880-entry whitelist:

===========  ==============
reads        peak RSS
===========  ==============
10 000 000   34.570 GB
40 000 000   34.600 GB
100 000 000  34.659 GB
===========  ==============

**A 10x increase in depth costs 89 MB** — ~0.95 bytes/read — because the number is the *genome index*
(~30 GB resident before a read is parsed), not the counting. Everything that could scale is either
constant (the 6.8 M whitelist; the 78 733-gene feature axis, which comes from the index and so does
not care which genes the reads came from) or negligible (the sparse matrices are ~100 MB and grow
*sub*-linearly — 4x the reads gave 2x the non-zeros, as each cell's expressed genes fill in).

So Velocyto is not "affordable", it is **free to three significant figures**: it rides on an index we
pay for regardless. Provision ~48-64 GB per hg38 STARsolo job and depth is irrelevant across any real
library size. The pre-registered kill rule (">2x wall-clock or over the mem_gb hint => drop to four")
does not fire, and now it does not fire *because it was tested*.

Read the number with its configuration or not at all: peak RSS includes STAR's per-thread buffers, so
34.7 GB is a peak **at 16 threads**, and the measurement ran ``--outSAMtype None`` while the shipped
module writes ``BAM Unsorted`` (streamed, so buffers — bounded, but not zero and not yet measured).
Both are why ``kb e2e-fit`` refuses to merge runs that differ in either.

If it ever does prove pathological the default drops to four and ``--quantify`` restores it: an
expensive default is not a trap precisely *because* the processing manifest exists to override it.
"""


@dataclass(frozen=True)
class ProcessingOverrides:
    """CLI-typed overrides — the top of the precedence ladder.

    A flag outranks an instruction document because it is more specific and it is later in time: both
    are the user talking to seqforge, one just talks now. It also outranks it in *trust*, which is why
    it may set fields an instruction document may not (``threads``, ``annotation_name``): a flag is
    typed by a human, a document is read by a model.
    """

    assembly: str | None = None
    annotation_name: str | None = None
    features: tuple[SoloFeature, ...] | None = None  # --quantify: EXACT replacement, not a union
    threads: int | None = None
    environment: RuntimeEnv | None = None


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


def resolve_features(
    *,
    instructions: Sequence[Instruction] = (),
    override: tuple[SoloFeature, ...] | None = None,
) -> tuple[list[SoloFeature], Basis, list[str], list[ValidationWarning]]:
    """Fold policy + instructions + a flag into ONE ordered feature list, with its provenance.

    **Prose promotes; it never narrows.** "This dataset should be aligned in GeneFull mode" is
    ambiguous: *instead of* Gene, or *make sure* GeneFull is computed? We take the second — it is the
    charitable reading, it is the cheap one, and it is consistent with counting everything by default
    (R15). So an instructed feature is UNIONed with the default and promoted to the front, where it
    becomes primary. Nothing is dropped.

    That is also the safety argument for letting a model source this field at all: because the default
    computes everything, a hallucinated instruction can only mislabel which matrix is primary — it
    cannot destroy signal. The blast radius of the one failure R5 provably cannot catch is a wrong
    label on a matrix we still computed.

    **A flag replaces exactly.** The user typed the whole list; they mean it. Narrowing is the only
    irreversible act available here, so it warns rather than passing silently.
    """
    warnings: list[ValidationWarning] = []
    default = list(DEFAULT_SOLO_FEATURES)

    if override is not None:
        features = list(dict.fromkeys(override))
        dropped = [f for f in default if f not in features]
        if dropped:
            warnings.append(
                ValidationWarning(
                    code="FEATURES_NARROWED",
                    message=(
                        f"--quantify drops {dropped} from the default. Counting is cheap next to the "
                        f"alignment you are already paying for, and dropping is the only "
                        f"irreversible act here: --soloFeatures Gene alone was measured to discard "
                        f"40.7% of a nuclear library."
                    ),
                    subject=BlockerSubject(kind="field", ref="processing.quantification"),
                )
            )
        return features, "user_confirmed", ["cli:--quantify"], warnings

    named = [i for i in instructions if i.field == "processing.quantification"]
    if named:
        # promote, do not substitute: instructed features move to the front, the rest of the default
        # follows in its own order. `dict.fromkeys` keeps first-seen order and de-duplicates.
        promoted = [i.value for i in named]
        features = list(dict.fromkeys([*promoted, *default]))  # type: ignore[list-item]
        evidence = [e for i in named for e in i.evidence]
        return features, "user_confirmed", evidence, warnings

    return default, "inferred", ["policy:default-solo-features"], warnings


def resolve_processing(
    *,
    spec: Spec,
    dataset: DatasetManifest,
    instructions: Sequence[Instruction] = (),
    overrides: ProcessingOverrides | None = None,
) -> tuple[ProcessingSection, list[ValidationWarning]]:
    """THE single place precedence lives: policy default -> instruction -> CLI flag.

    Pure: no bytes, no disk, no LLM, no network. A pure function of three inputs is exactly what you
    want owning the rule that decides what gets counted.

    Precedence is **silent** by design. A flag overriding an instruction, or an instruction overriding
    a policy default, is not an ambiguity — it is what an instruction IS, and stopping to ask would
    make the pipeline unusable by the people telling it what to do. What IS surfaced is a
    *same-precedence* disagreement, which has no tiebreak; :func:`instructions_from_assertions` raises
    those as ``Conflict``s (exit 4) before this function ever runs.
    """
    ov = overrides or ProcessingOverrides()
    defaults = processing_defaults(spec)
    rung = dataset.library.chemistry.rung

    quant: Quantification
    warnings: list[ValidationWarning] = []
    if isinstance(defaults.quantification, SoloQuant):
        features, basis, evidence, warnings = resolve_features(
            instructions=instructions, override=ov.features
        )
        quant = SoloQuant(features=features)
    else:
        # bulk: counting is module-scoped, and there is nothing here a user needs to instruct —
        # --quantMode GeneCounts already emits all three strand columns.
        quant = defaults.quantification
        basis, evidence = "inferred", ["policy:default-bulk-quant-mode"]

    assembly = ov.assembly or _instructed(instructions, "processing.genome.assembly")
    if assembly is None:
        raise PolicyError(
            f"no genome: this dataset's organism is taxid {dataset.experiment.organism.value}. "
            "Pass --assembly/--annotation, or name an assembly in an --instruction document. "
            "seqforge will not guess: taxid -> preferred assembly (hg38 vs hg19 vs T2T) is a policy "
            "call, and that map belongs to liulab-genome (R12)."
        )
    if ov.annotation_name is None:
        raise PolicyError(
            "no annotation: --annotation names a GTF REGISTERED with liulab-genome (e.g. WS298). "
            "It is a registry name, not something a paper writes, so there is nothing to infer."
        )
    genome_basis: Basis = "user_confirmed" if ov.assembly else "asserted"

    section = ProcessingSection(
        genome=EvidencedGenome(
            value=GenomeRef(
                assembly=assembly,
                annotation_name=ov.annotation_name,
                ncbi_taxid=dataset.experiment.organism.value,
            ),
            basis=genome_basis,
            evidence=["cli:--assembly"] if ov.assembly else [],
            confidence=0.9,
            rung=0,
        ),
        aligner=EvidencedStr(value=defaults.aligner, basis="inferred", confidence=0.95, rung=rung),
        quantification=EvidencedQuantification(
            value=quant, basis=basis, evidence=evidence, confidence=0.9, rung=rung
        ),
        variant_calling=EvidencedBool(
            value=defaults.variant_calling, basis="inferred", confidence=0.9, rung=0
        ),
        environment=EvidencedRuntimeEnv(
            value=ov.environment or defaults.environment,
            basis="user_confirmed" if ov.environment else "inferred",
            confidence=0.95,
            rung=0,
        ),
        resources=ResourceHints(threads=ov.threads) if ov.threads else ResourceHints(),
    )
    return section, warnings


def _instructed(instructions: Sequence[Instruction], field: str) -> str | None:
    """The instructed value for a single-valued field, if any. Same-field conflicts are already out."""
    for i in instructions:
        if i.field == field:
            return i.value
    return None
