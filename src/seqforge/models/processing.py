""":class:`ProcessingManifest` — one way to process a dataset. Many per dataset.

A finished assay is immutable; what you *do* with it is a choice, and there are several defensible
ones. So the manifest is two artifacts with two lifetimes:

- :class:`~seqforge.models.dataset.DatasetManifest` — the **IR**. What the data *is*. One per dataset.
- :class:`ProcessingManifest` — the **flags**. What to *do* with it. Many per dataset.

That is the compiler metaphor doing work rather than decorating it. Same IR + different flags =
different binaries; same dataset + different processing manifests = different pipelines, with the
dataset hash unchanged. ``-O2`` does not get to edit the IR, and neither does anything in this file.

**This section is intent, not truth, and it has no authority.** Every field is still ``Evidenced``,
but for a different reason than in ``dataset``: there, ``basis`` records HOW WE KNOW; here it records
WHO DECIDED. A corpus row reading "GeneFull because the user's instruction file said so" is
categorically different from "GeneFull because policy defaults to all five", and that difference has
to survive into the training corpus. ``user_confirmed`` — which has sat in the ``Basis`` literal since
the beginning without a single writer — is the basis this section exists to carry.

This module imports nothing from ``dataset``, and must not (see that module's header).
"""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .base import Accession, Evidenced, NcbiTaxid, Sha256
from .evidenced import EvidencedBool, EvidencedStr


class GenomeRef(BaseModel):
    """liulab-genome selection: UCSC assembly id + a REGISTERED GTF name. Never a path.

    ``annotation_name`` is ``None`` for a pipeline whose aligner index carries no gene model — chromap's
    scATAC index is built from the FASTA alone, and the deliverable is a fragments file, not a count
    matrix, so there is no GTF to name (and nothing would read it). Every counting pipeline (STAR /
    STARsolo) requires it, which the processing policy enforces per-pipeline.

    ``ncbi_taxid`` and ``intron_length_cap`` are both facts about the ASSEMBLY, copied off
    liulab-genome's shipped table by the processing policy — never anything a user states and never a
    copy of the dataset's own organism. They are recorded rather than looked up at run time because
    this file is inside ``run_id``: a value read from the table by a rule would let an upstream edit
    change how a dataset aligns while two compiled pipelines kept one identity.

    They differ on a chimera, and the difference is the point. A taxid is an IDENTITY, and a chimera
    is more than one organism, so its row carries none and this field stays ``None``. A cap is a
    BOUND, and a bound over a union of components is the maximum of theirs, so a chimera has one
    whenever every component does.
    """

    assembly: str
    annotation_name: str | None = None
    ncbi_taxid: NcbiTaxid | None = None
    #: The longest gap STAR may open, in bp, or ``None`` where the lab has not characterised this
    #: assembly — at which the aligner keeps its own defaults and no flag is emitted at all.
    intron_length_cap: int | None = None


class EvidencedGenome(Evidenced[GenomeRef]):
    """An ``Evidenced`` genome reference."""


RuntimeEnv = Literal["align-rna", "align-dna", "ml", "ml-gpu"]
"""A literal liulab-runtime environment name — the env name IS the identifier (no profile layer)."""


class EvidencedRuntimeEnv(Evidenced[RuntimeEnv]):
    """An ``Evidenced`` liulab-runtime environment name."""


SoloFeature = Literal[
    "Gene",
    "SJ",
    "GeneFull",
    "GeneFull_ExonOverIntron",
    "GeneFull_Ex50pAS",
    "Velocyto",
]
"""STARsolo's complete ``--soloFeatures`` vocabulary.

Closed on purpose, and it is the closure that does the safety work: ``verify.entails`` is **vacuous
when value ⊆ quote**, so span-verification only bites for a controlled vocabulary. Because this one is closed,
"aligned in GeneFull mode" entails ``GeneFull`` and "count introns too" does not — and the second
rejection is the *right* answer, not a gap to paper over with aliases.
"""


class SoloQuant(BaseModel):
    """STARsolo counting. An ORDERED list; element 0 is the primary matrix.

    Order is a seqforge-side annotation with no aligner-side referent: STARsolo writes one
    ``Solo.out/<Feature>/`` per value and does not care about order, so ``[Gene, GeneFull]`` and
    ``[GeneFull, Gene]`` produce byte-identical output. What the order buys is a deterministic answer
    to "which matrix is THE matrix" without a second field — and because it has no effect on the
    aligner, ``compose`` projects it out to an explicit ``primary_feature`` key rather than leaving a
    positional convention load-bearing for every downstream reader.

    A known cells-vs-nuclei prep fact may only REORDER this list, never shorten it. Compute is spent
    once, count matrices are small, and dropping a feature is the only irreversible act available.
    """

    kind: Literal["solo"] = "solo"
    features: list[SoloFeature] = Field(min_length=1)

    @model_validator(mode="after")
    def _starsolo_rules(self) -> SoloQuant:
        if len(set(self.features)) != len(self.features):
            raise ValueError(f"duplicate soloFeatures: {self.features}")
        # STARsolo's docs: "Velocyto quantification requires Gene features". A real aligner
        # constraint, so it is a validator and not a comment — STAR would error out, but only AFTER
        # the download and the alignment we were amortizing. Refuse first, with a remedy. This
        # is also the clearest proof that a closed vocabulary is not by itself armor: no enum can
        # express "this member requires that one".
        if "Velocyto" in self.features and "Gene" not in self.features:
            raise ValueError(
                "STARsolo requires Gene features for Velocyto quantification; add 'Gene' to features"
            )
        return self


class BulkQuant(BaseModel):
    """Plain-STAR counting.

    There is no strandedness knob here and there never needs to be: ``--quantMode GeneCounts``
    already emits all three strand columns in ``ReadsPerGene.out.tab``. ``bulk-rnaseq``'s spec
    long carried a comment promising that "library strandedness is resolved at compose from
    processing policy", and no code ever implemented it — because there was never a decision to make.
    Same law as ``soloFeatures``: when the aligner computes every alternative in one pass and the
    outputs are small, compute them all and let the consumer choose.
    """

    kind: Literal["bulk"] = "bulk"
    mode: Literal["GeneCounts", "TranscriptomeSAM", "None"] = "GeneCounts"


class AtacQuant(BaseModel):
    """chromap scATAC quantification — the deliverable is a fragments file, so nothing is COUNTED.

    A count matrix needs a feature axis (genes); ATAC has none, so unlike :class:`SoloQuant` /
    :class:`BulkQuant` this carries no counting knob — ``fragments.tsv.gz`` is the whole output. It
    exists so a chromap-pipeline processing manifest stays well-typed: ``quantification`` is still one
    ``Evidenced`` envelope, its value just says "fragments, not a matrix". The parse/count split holds
    trivially — there is no count to instruct — which is why an ATAC recipe never asks a counting
    question the way a nuclear RNA one does (Gene vs GeneFull).
    """

    kind: Literal["atac"] = "atac"


class UmiQuant(BaseModel):
    """The plate assay's counting shape — one combined matrix object over every cell of the deposit.

    Carries no knob, and that is a statement about the counter rather than a placeholder. The
    engine writes every matrix in one pass — UMIs and reads, against exon, intron and the two
    together — for the same reason :class:`BulkQuant` has no strandedness field: when every
    alternative is computed in one pass and the outputs are small, compute them all and let the
    consumer choose. Which one a reader wants is a question asked of the object, not of the pipeline;
    which ones exist is :mod:`seqforge.workflows.umite.count`'s table to say, and counting them here
    is what made this paragraph describe an object one layer smaller than the shipped one.

    It exists so a plate recipe stays well-typed against the module that runs it. Without it the
    module's config block inherits ``quantMode``, a real and wrong instruction to a pipeline whose
    counter has never heard of it — the same silent fall-through the counting shape has already been
    split three ways to prevent.
    """

    kind: Literal["umi"] = "umi"


Quantification = Annotated[
    SoloQuant | BulkQuant | AtacQuant | UmiQuant, Field(discriminator="kind")
]
"""What to COUNT, discriminated by aligner family (the house style: cf. ``Segment``, ``Test``)."""


class EvidencedQuantification(Evidenced[Quantification]):
    """An ``Evidenced`` counting decision — the field that used to be decorative.

    Policy set it to the string ``"gene"``, wrote it to the manifest, and ``compose`` then ignored it
    and read ``soloFeatures`` from the KB instead: two sources of truth for one decision, unable to
    disagree only because one was never consulted.
    """


class ResourceHints(BaseModel):
    """Advisory resource requests for the workflow scheduler.

    ``mem_gb`` is advisory to the *scheduler* but load-bearing for STAR, because ``starsolo.smk``
    derives ``--limitBAMsortRAM`` from the memory **this attempt** was granted — 3/4 of the escalated
    request, not 3/4 of the number written here. What is written here is the **first attempt's**
    request: ``starsolo_count`` declares ``retries``, and snakemake re-runs a failed job at 2x and
    then 3x (the arithmetic is ``workflows/memory.py``), so every cap STAR is handed rises with the
    job around it instead of staying pinned to attempt 1. Since #198 the coordinate sort is not
    optional — STAR emits ``CB``/``UB`` only in a sorted BAM — and STAR **refuses rather than
    spilling**: it reports the memory it needed and exits, where the ``samtools sort`` it replaced
    would have spilled to disk and finished.

    **The figure covers three things at once, and which of them dominates is a property of the SAMPLE
    rather than of the pipeline**: the genome index, resident for the life of the mapping process; the
    aligner's working set; and the sort, which grows with alignment records. A plate cell of a few
    thousand reads is index-dominated — 27.7 GB peak against a 25 GB index, whether the cell holds 901
    reads or 3.1M. A 215M-read droplet sample is sort-dominated. Both are real, they are two ends of
    one curve, and the number here has to cover whichever end a recipe is aimed at.

    The default is sized against the end that moved it, which was the sort, measured on
    GSE208154/SAMN29720279 in the pinned image: the requirement is linear at **~160 B per alignment
    record** (1,999,909 records -> 394 MB; 9,844,534 -> 1,590 MB) and is **not** reduced by
    ``--outBAMsortingBinsN``, STAR's documented remedy. At 48 GB the sort gets 36 GB, which covers
    ~225M alignment records — a typical sample here (SAMN29720279: 215M reads, 199M records) with
    headroom. 32 GB gave the sort 24 GB and would have FATAL'd that sample, which is why this moved.

    Sizing it for a different end of the curve is what ``seqforge processing new --mem-gb`` is for,
    and the inequality that decision has to clear is the same three quarters read backwards: **the
    request must be at least four thirds of the sort expected.** Against a small genome — ce11's index
    is 1.3 GB, so 48 GB is ~35x the residency — nothing about the index argues for the default, and
    the four thirds is then the only floor left. ``workflows/memory.py`` derives it.

    **The sort was never the whole memory story**, which is why this number has to cover more than the
    arithmetic above and why the escalation exists at all. STARsolo also holds ``readInfo`` — 16 B for
    every *input* read, allocated before anything is sorted — and no ``--limit*`` option bounds it:
    there are eight of them, none covers ``readInfo`` or the genome index, and there is no
    ``--limitSoloRAM``. So a request whose 3/4 is spent on a *permitted* sort (``--limitBAMsortRAM``
    permits, it does not reserve) can still be exhausted by the allocations the sort does not include,
    and then the kill comes from the scheduler rather than from STAR. A sample can exhaust all three
    attempts and fail; that is an accepted outcome, not a bug to engineer around, and the failure is
    legible in the common case anyway — when the *sort* is what does not fit, STAR names the number it
    needed and exits, so an under-sized job stops rather than producing a short BAM.

    **It does not cover everything, and that is deliberate rather than overlooked.** The largest
    sample in the worm corpus (PRJNA658829/SAMN15970313) is 2.23 **billion** reads / 2.44 billion
    records. Its ``readInfo`` alone is **35.7 GB** — that one is arithmetic over a measured constant,
    16 B x reads, and needs no extrapolation — so 3x of this default is largely spent before a single
    record is sorted. The same linear model puts its *sort* near 390 GB, but that figure is a ~250x
    extrapolation beyond the measured range, so it is a reason to measure that sample before
    reprocessing it, not a number to trust. Either way it wants a per-recipe override, not a bigger
    default for the other hundred samples that need none of it — which is what the two-artifact split
    is for.
    """

    threads: int = Field(ge=1, default=8)
    mem_gb: int = Field(ge=1, default=48)
    disk_gb: int | None = None
    gpus: int = Field(ge=0, default=0)


class ProcessingSection(BaseModel):
    """INTENT — what we choose to do to a finished assay. Not a truth; no authority.

    ``basis`` here records WHO DECIDED, on this ladder (highest first):

    ==================  =================  ==========================
    source              basis              evidence
    ==================  =================  ==========================
    a CLI flag          ``user_confirmed`` ``["cli:--quantify"]``
    an instruction doc  ``user_confirmed`` ``["assert-..."]``
    reference prose     ``asserted``       ``["assert-..."]``
    a policy default    ``inferred``       ``["policy:<rule>"]``
    ==================  =================  ==========================

    The first two share a basis and differ only in precedence — both are the user talking to seqforge,
    one just talks later — so the *channel* lives in ``evidence``. That is also why no
    ``policy_default`` basis is needed: once a section can
    carry a **varying** basis, ``inferred`` plus an evidence ref naming the rule is distinguishable by
    inspection.
    """

    # `extra="forbid"` is that enforcement at the model, not just at the gate. The instructable
    # surface is *enumerated*; an unknown key must be a validation error, never a silent drop. It was
    # a silent drop until 2026-07-15 — `ProcessingSection(soloStrand="Reverse")` constructed happily
    # and discarded the field — which is pydantic's default, and the wrong default here: this is the
    # artifact a user hands us, so an unrecognised key is either a typo or an attempt to reach a
    # parse decision, and both deserve to fail loudly rather than be dropped on the floor.
    model_config = ConfigDict(extra="forbid")

    genome: EvidencedGenome
    aligner: EvidencedStr
    quantification: EvidencedQuantification
    variant_calling: EvidencedBool
    environment: EvidencedRuntimeEnv
    resources: ResourceHints = Field(default_factory=ResourceHints)


class DatasetPin(BaseModel):
    """Which dataset a processing manifest is bound to."""

    dataset_hash: Sha256
    accessions: list[Accession] = Field(default_factory=list)  # human-readable, advisory only


class ProcessingProvenance(BaseModel):
    """Binds a processing manifest to the module source that will execute it."""

    processing_hash: str
    workflow_version: str
    seqforge_version: str


class ProcessingManifest(BaseModel):
    """One way to process a dataset. Many per dataset — that plurality IS the design.

    ``dataset is None`` => a **template**: it applies to any dataset, which is what lets one file drive
    10^4 of them (this is scRecounter's uniform reprocessing, and it is the half of the design that a
    mandatory pin would destroy — you would have 10^4 near-identical files, none of which anyone
    reads, and the file would stop carrying signal).

    ``dataset is not None`` => **bound**: ``compose`` refuses any dataset whose hash differs, with a
    ``Blocker`` (exit 3), and never auto-repins.

    ``compose`` always writes the bound form it actually used next to the config it produced, so the
    default path leaves recoverable state on disk without demanding an input file. Disk is
    *state*, not *input*.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    processing_id: str  # a human slug, e.g. "default-2026.7" / "genefull-primary"
    dataset: DatasetPin | None = None
    processing: ProcessingSection
    provenance: ProcessingProvenance


__all__ = [
    "GenomeRef",
    "EvidencedGenome",
    "RuntimeEnv",
    "EvidencedRuntimeEnv",
    "SoloFeature",
    "SoloQuant",
    "BulkQuant",
    "AtacQuant",
    "UmiQuant",
    "Quantification",
    "EvidencedQuantification",
    "ResourceHints",
    "ProcessingSection",
    "DatasetPin",
    "ProcessingProvenance",
    "ProcessingManifest",
]
