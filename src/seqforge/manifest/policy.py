"""Processing policy — the default a user gets when they instruct nothing.

seqforge picks the best default pipeline option; a user instruction overrides it. This module is the
"picks" half: small, explicit, and derived. The aligner and runtime env follow from the KB's backend
module, never from a guess, and the runtime env is a **literal** ``liulab-runtime`` name —
there is no profile-indirection layer to invent here.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from ..harvest.prep import normalize_prep_type as _normalize_prep_type
from ..kb.schema import Spec
from ..models.assertion import Assertion
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
from ..workflows import get_module
from .instruct import Instruction


class PolicyError(RuntimeError):
    """Intent cannot be resolved — a required choice has no safe default (refuse, don't guess)."""


#: KB backend module -> the aligner name recorded in `processing.aligner`.
_ALIGNER_FOR_MODULE = {"map/starsolo": "starsolo", "map/star": "star"}

DEFAULT_SOLO_FEATURES: tuple[SoloFeature, ...] = (
    "Gene",
    "GeneFull",
    "GeneFull_ExonOverIntron",
    "GeneFull_Ex50pAS",
    "Velocyto",
)
"""Count everything; do not ask which.

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

**The cost is measured (2026-07-15), and it has a KNEE.** `kb e2e-cost` on hg38 + GENCODE v50 + 10x
3' v3, all five features, 16 threads, 5 000 cells, reads simulated from real hg38 sequence with
barcodes drawn from the real 6 794 880-entry whitelist:

===========  ==========  ==================
reads        peak RSS    delta
===========  ==========  ==================
10 000 000   34.570 GB   —
40 000 000   34.600 GB   +30 MB
100 000 000  34.659 GB   +59 MB
250 000 000  44.055 GB   **+9.4 GB**
===========  ==========  ==================

Read that bottom row before quoting any of the others. Up to ~100 M reads the number is the *genome
index* (~30 GB resident before a read is parsed) and depth is irrelevant — 10x the reads cost 89 MB.
Then it stops being flat. Peak RSS is really ``max(alignment_peak, solo_peak(reads))``: the alignment
phase is index-bound and flat, the **Solo counting phase grows with depth**, and it overtakes the
index somewhere between 100 M and 250 M. Watching the 250 M run live shows it directly — RSS sits at
~17 GB early in Solo, climbs past the 34.6 GB alignment peak, and tops out at 44 GB while writing
five matrices.

**Provisioning, honestly:**

- ≤ 100 M reads: ~35 GB. Solid — three points.
- 250 M reads: ~44 GB. One point.
- **> 250 M: UNMEASURED.** If ``solo_peak`` is ~linear the crossover implies ~180-190 bytes/read,
  putting 500 M near ~88 GB — but that is arithmetic on a single point, not a measurement, and this
  docstring has now been wrong once for exactly that reason. Give a deep human library **128 GB**
  until somebody measures 500 M.

**How this docstring was wrong for three hours, because the lesson outlives the number.** The first
three points were fitted and reported ``max_residual_gb: 0.0`` — a perfect line — projecting 34.8 GB
at 250 M. Reality: 44.055 GB, a **9.3 GB / 27 % under-estimate from a fit that reported zero error**.
Earlier the same day ``_fit_line`` was fixed to refuse *two*-point fits, on the grounds that a line
through two points fits exactly and so its residual cannot falsify anything. That was right and it was
not enough: three *collinear* points inside one regime cannot falsify either. They were genuinely
linear; the residual was genuinely 0.0; the model was genuinely wrong. **A residual can only falsify
within the range sampled — it can never report that the range itself was too narrow.** The four-point
fit does say so (``max_residual_gb: 2.312``), one point too late to have helped.

The ``--outSAMtype`` gap was measured on the same principle: the sweep ran ``None``, the shipped
module runs ``BAM Unsorted``, and one variable changed gives **34.600 -> 35.345 GB (+745 MB) and
+19 % wall-clock** at 40 M. Measured at one depth, and after the knee, "one depth" is a warning rather
than a footnote.

Read every number with its configuration or not at all: peak RSS includes STAR's per-thread buffers,
so these are peaks **at 16 threads**. That is why ``kb e2e-fit`` refuses to merge runs differing in
threads, cells, assembly or outSAMtype.

Reproducibility is not assumed: the 40 M point was re-measured on a different node, through a
different code path (32 sharded FASTQs vs one file), on *different reads*, and landed on **34.600 GB**
again — identical to three decimals.

**The instrument that produced these had a floor, found 2026-07-15 and fixed.** ``wait4``'s
``ru_maxrss`` reports ``max(parent_rss_at_fork, child_peak)`` on Linux — a child's address space
begins as a copy of its parent's, and ``exec`` never lowers the high-water mark. Measured: beside an
879 MB parent, a child allocating 1 MB reported ``879260 KiB``, the parent's RSS to the byte. **Every
number in this table stands**, because the floor was the harness (~1 GB at the very worst) and these
readings are 34–44 GB — but that is an argument, not a check, so ``kb e2e-cost`` now records
``harness_peak_rss_kib`` beside every reading and takes the peak from the child's own ``/proc``
``VmHWM``, which ``exec`` resets. The lesson is the same one this docstring already carries twice: an
instrument that cannot be wrong in a way you would notice has not been checked.

**None of this changes the default.** Velocyto stays: the knee is a property of counting 250 M reads
at all, the marginal cost of the fifth rule over the first is not what moves this number, and the
pre-registered kill rule (">2x wall-clock or over the mem_gb hint => drop to four") is about the
*feature set*, not the depth. What changed is the memory request, which is what the measurement was
for. If it ever does prove pathological the default drops to four and ``--quantify`` restores it: an
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
    module = spec.require_backend().module
    aligner = _ALIGNER_FOR_MODULE.get(module, module.rsplit("/", 1)[-1])
    # Asked of the MODULE, which is the only thing that knows what software it needs. This was a
    # hardcoded `"align-rna"` sitting beside a module that also declared `align-rna` — two owners of
    # one fact, harmless only because no rule read the env. The moment `starsolo_count` grew a
    # `container:`, that pair could disagree into a container with no STAR in it. An ATAC module
    # declaring `align-dna` is now simply right, instead of being overridden here by a comment
    # promising someone would remember.
    environment: RuntimeEnv = get_module(module).env
    # Counting is MODULE-scoped: soloFeatures is meaningless to plain STAR, and quantMode is
    # meaningless to STARsolo. A processing manifest that carried one shape unconditionally would be
    # a type error the moment it met the other module.
    #
    # Keyed on the block the module READS, not on its name. `if module == "map/starsolo"` was the
    # same silent fall-through as `param_block_key`'s and `_read_files_in`'s before it: any third
    # module quietly gets `quantMode=GeneCounts`, which is a real and wrong instruction to an aligner
    # that may not take it. `param_block` refuses a module whose contract is neither solo nor bulk,
    # so the `else` here can only be bulk — by construction rather than by hope.
    quantification: Quantification = (
        SoloQuant(features=list(DEFAULT_SOLO_FEATURES))
        if get_module(module).param_block == "solo"
        else BulkQuant(mode="GeneCounts")
    )
    return ProcessingDefaults(
        module=module,
        aligner=aligner,
        quantification=quantification,
        environment=environment,
        variant_calling=False,
    )


# `_normalize_prep_type` and its cells-vs-nuclei vocabulary moved to `harvest.prep` (harvest reads the
# prose; this module consumes the normalized result), imported at the top under the same private name.
# `harvest.verify` now shares that exact vocabulary to entail a `library.prep_type` quote, instead of
# mirroring it — one place to teach a new synonym, not two that can silently drift apart.
def prep_type_from_assertions(assertions: Sequence[Assertion]) -> str | None:
    """The cells-vs-nuclei prep, normalized from a span-verified ``library.prep_type`` claim.

    ``None`` if the paper does not say, or if two verified claims disagree — never a guess between
    them. The model FOUND the biology ("single nuclei") with a quote that greps back; this reads that
    record and normalizes its wording. It names no feature: the biology -> feature mapping is
    :func:`resolve_features`'s and code's alone, which is why sourcing this field from prose is safe.
    """
    values: set[str] = set()
    for a in assertions:
        if a.field != "library.prep_type" or not (a.span_verified and a.entailment_ok):
            continue
        norm = _normalize_prep_type(a.value)
        if norm is not None:
            values.add(norm)
    return next(iter(values)) if len(values) == 1 else None


def resolve_features(
    *,
    instructions: Sequence[Instruction] = (),
    override: tuple[SoloFeature, ...] | None = None,
    prep_type: str | None = None,
) -> tuple[list[SoloFeature], Basis, list[str], list[ValidationWarning]]:
    """Fold policy + instructions + a flag into ONE ordered feature list, with its provenance.

    **Prose promotes; it never narrows.** "This dataset should be aligned in GeneFull mode" is
    ambiguous: *instead of* Gene, or *make sure* GeneFull is computed? We take the second — it is the
    charitable reading, it is the cheap one, and it is consistent with counting everything by default.
    So an instructed feature is UNIONed with the default and promoted to the front, where it
    becomes primary. Nothing is dropped.

    That is also the safety argument for letting a model source this field at all: because the default
    computes everything, a hallucinated instruction can only mislabel which matrix is primary — it
    cannot destroy signal. The blast radius of the one failure span-verification provably cannot catch
    is a wrong label on a matrix we still computed.

    **A flag replaces exactly.** The user typed the whole list; they mean it. Narrowing is the only
    irreversible act available here, so it warns rather than passing silently.

    **A single-nucleus prep only REORDERS.** With no flag and no instruction, a span-verified
    ``prep_type`` of ``single-nucleus`` promotes ``GeneFull`` to primary — a nuclear library is
    ~1/3 intronic, so a Gene-first primary silently under-counts it (ce11: Gene=1186 vs GeneFull=1940,
    a 40.7% loss). Still all five features, one alignment, one pass; only ``adata.X`` changes. This is
    the model finding biology and code deciding processing — the split the whole compiler is built on.
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

    if prep_type == "single-nucleus":
        # No flag, no instruction — but a verified nuclei prep. Promote GeneFull to primary the same
        # way an instruction would, using the same union idiom so nothing is dropped and Gene still
        # follows (Velocyto's "requires Gene" holds by construction).
        features = list(dict.fromkeys(["GeneFull", *default]))
        return features, "inferred", ["policy:genefull-primary-for-single-nucleus"], warnings

    return default, "inferred", ["policy:default-solo-features"], warnings


def resolve_processing(
    *,
    spec: Spec,
    dataset: DatasetManifest,
    instructions: Sequence[Instruction] = (),
    overrides: ProcessingOverrides | None = None,
    prep_type: str | None = None,
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
            instructions=instructions, override=ov.features, prep_type=prep_type
        )
        quant = SoloQuant(features=features)
    else:
        # bulk: counting is module-scoped, and there is nothing here a user needs to instruct —
        # --quantMode GeneCounts already emits all three strand columns.
        quant = defaults.quantification
        basis, evidence = "inferred", ["policy:default-bulk-quant-mode"]

    instructed = _instructed_entry(instructions, "processing.genome.assembly")
    assembly = ov.assembly or (instructed.value if instructed else None)
    if assembly is None:
        raise PolicyError(
            f"no genome: this dataset's organism is taxid {dataset.experiment.organism.value}. "
            "Pass --assembly/--annotation, or name an assembly in an --instruction document. "
            "seqforge will not guess: taxid -> preferred assembly (hg38 vs hg19 vs T2T) is a policy "
            "call, and that map belongs to liulab-genome."
        )
    if ov.annotation_name is None:
        raise PolicyError(
            "no annotation: --annotation names a GTF REGISTERED with liulab-genome (e.g. WS298). "
            "It is a registry name, not something a paper writes, so there is nothing to infer."
        )
    # §7's ladder: a CLI flag and an --instruction document are BOTH `user_confirmed` and differ only
    # in PRECEDENCE — they are the same user, one talking later. The channel lives in `evidence`.
    #
    # This read `"user_confirmed" if ov.assembly else "asserted"`, and `asserted` is what a database
    # or a paper says — the opposite of the user talking. The branch could only fire when the assembly
    # came from an instruction, and no production caller ever passed one, so it was dead code AND
    # wrong. Making the path reachable is what surfaced it.
    genome_basis: Basis
    genome_evidence: list[str]
    if ov.assembly:
        genome_basis, genome_evidence = "user_confirmed", ["cli:--assembly"]
    elif instructed is not None:
        genome_basis, genome_evidence = instructed.basis, list(instructed.evidence)
    else:  # pragma: no cover - `assembly is None` already raised above
        genome_basis, genome_evidence = "inferred", []

    section = ProcessingSection(
        genome=EvidencedGenome(
            value=GenomeRef(
                assembly=assembly,
                annotation_name=ov.annotation_name,
                ncbi_taxid=dataset.experiment.organism.value,
            ),
            basis=genome_basis,
            evidence=genome_evidence,
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


def _instructed_entry(instructions: Sequence[Instruction], field: str) -> Instruction | None:
    """The instruction for a single-valued field, if any. Same-field conflicts are already out.

    Returns the Instruction rather than its value, because the basis and evidence are the point: an
    instruction is the USER talking (`user_confirmed`), and its evidence names the assertion whose
    quote greps back into the document. Returning a bare string is what let the caller stamp
    `asserted` — a database's basis — on something a human wrote for us.
    """
    for i in instructions:
        if i.field == field:
            return i
    return None
