""":class:`ProcessingManifest` — one way to process a dataset. Many per dataset (R13/R14).

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
    """liulab-genome selection: UCSC assembly id + a REGISTERED GTF name. Never a path (R9)."""

    assembly: str
    annotation_name: str
    ncbi_taxid: NcbiTaxid | None = None


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
when value ⊆ quote**, so R5 only bites for a controlled vocabulary. Because this one is closed,
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
        # the download and the alignment we were amortizing. Refuse first, with a remedy (R4). This
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
    already emits all three strand columns in ``ReadsPerGene.out.tab``. ``bulk-rnaseq-pe``'s spec
    long carried a comment promising that "library strandedness is resolved at compose from
    processing policy", and no code ever implemented it — because there was never a decision to make.
    Same law as ``soloFeatures``: when the aligner computes every alternative in one pass and the
    outputs are small, compute them all and let the consumer choose.
    """

    kind: Literal["bulk"] = "bulk"
    mode: Literal["GeneCounts", "TranscriptomeSAM", "None"] = "GeneCounts"


Quantification = Annotated[SoloQuant | BulkQuant, Field(discriminator="kind")]
"""What to COUNT, discriminated by aligner family (the house style: cf. ``Segment``, ``Test``)."""


class EvidencedQuantification(Evidenced[Quantification]):
    """An ``Evidenced`` counting decision — the field that used to be decorative.

    Policy set it to the string ``"gene"``, wrote it to the manifest, and ``compose`` then ignored it
    and read ``soloFeatures`` from the KB instead: two sources of truth for one decision, unable to
    disagree only because one was never consulted.
    """


class ResourceHints(BaseModel):
    """Advisory resource requests for the workflow scheduler."""

    threads: int = Field(ge=1, default=8)
    mem_gb: int = Field(ge=1, default=32)
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
    one just talks later — so the *channel* lives in ``evidence``. That is what design §1.0's open
    note already prescribed, and it is why no ``policy_default`` basis is needed: once a section can
    carry a **varying** basis, ``inferred`` plus an evidence ref naming the rule is distinguishable by
    inspection.
    """

    # `extra="forbid"` is R14's enforcement at the model, not just at the gate. The instructable
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
    """One way to process a dataset. Many per dataset — that plurality IS the design (R13).

    ``dataset is None`` => a **template**: it applies to any dataset, which is what lets one file drive
    10^4 of them (this is scRecounter's uniform reprocessing, and it is the half of the design that a
    mandatory pin would destroy — you would have 10^4 near-identical files, none of which anyone
    reads, and the file would stop carrying signal).

    ``dataset is not None`` => **bound**: ``compose`` refuses any dataset whose hash differs, with a
    ``Blocker`` (exit 3), and never auto-repins.

    ``compose`` always writes the bound form it actually used next to the config it produced, so the
    default path leaves recoverable state on disk without demanding an input file. R7 says disk is
    *state*, not that disk is *input*.
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
    "Quantification",
    "EvidencedQuantification",
    "ResourceHints",
    "ProcessingSection",
    "DatasetPin",
    "ProcessingProvenance",
    "ProcessingManifest",
]
