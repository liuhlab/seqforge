"""The three-section :class:`Manifest` — three authorities, machine-independent (R9).

``library`` (physical truth, authority = evidence), ``experiment`` (biological truth, authority =
metadata + humans), ``processing`` (intent, authority = derived + policy). Every interpretive field
is ``Evidenced[...]``; genome/software/data are names or URIs that resolve at run time, never paths.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from .base import (
    Accession,
    AssayTerm,
    ChemistryId,
    Evidenced,
    NcbiTaxid,
    Sha256,
    Uri,
)


class ReadElement(BaseModel):
    """One ordered sub-region of a read; a superset/adapter over a seqspec Region.

    Adds a derived ``start`` and an interpretive ``role`` that seqspec Regions do not carry. seqspec
    ``sequence_type`` maps: fixed -> ``sequence``; random -> random ACGT; onlist -> ``onlist_ref``.
    """

    role: Literal["CB", "UMI", "cDNA", "gDNA", "index", "linker", "polyT", "polyA"]
    region_type: Literal[
        "barcode",
        "umi",
        "cdna",
        "gdna",
        "index5",
        "index7",
        "linker",
        "poly_A",
        "poly_t",
        "custom_primer",
    ]
    start: int | None = None
    length: int | None = None
    min_len: int | None = None
    max_len: int | None = None
    sequence: str | None = None
    onlist_ref: str | None = None


class ReadDef(BaseModel):
    """A sequencing read (== one FASTQ), aligned to a seqspec Read. ``read_id`` is a role, not a file."""

    read_id: str
    strand: Literal["pos", "neg"]
    min_len: int = Field(ge=0)
    max_len: int = Field(ge=0)
    elements: list[ReadElement]


class ReadLayout(BaseModel):
    """Full physical library structure = ordered reads x ordered elements."""

    modality: Literal["rna", "atac", "protein", "crispr", "dna"]
    reads: list[ReadDef]


class Onlist(BaseModel):
    """Barcode-whitelist registry entry; fetched via pooch + hash-verified, never vendored.

    ``orientation_hint`` is a non-authoritative default; the authoritative orientation lives per-read
    in the KB (one list can be forward for GEX and reverse-complemented for ATAC).
    """

    name: str
    uri: Uri
    sha256: Sha256
    length: int = Field(ge=1)
    orientation_hint: Literal["forward", "reverse_complement"] | None = None
    n_entries: int | None = None


class GenomeRef(BaseModel):
    """liulab-genome selection: UCSC assembly id + a REGISTERED GTF name. Never a path (R9)."""

    assembly: str
    annotation_name: str
    ncbi_taxid: NcbiTaxid | None = None


RuntimeEnv = Literal["align-rna", "align-dna", "ml", "ml-gpu"]
"""A literal liulab-runtime environment name — the env name IS the identifier (no profile layer)."""


class ResourceHints(BaseModel):
    """Advisory resource requests for the workflow scheduler."""

    threads: int = Field(ge=1, default=8)
    mem_gb: int = Field(ge=1, default=32)
    disk_gb: int | None = None
    gpus: int = Field(ge=0, default=0)


# ---- concrete Evidenced specializations: stable, named $defs for schema export ----
class EvidencedStr(Evidenced[str]):
    """An ``Evidenced`` string field."""


class EvidencedBool(Evidenced[bool]):
    """An ``Evidenced`` boolean field."""


class EvidencedTaxid(Evidenced[NcbiTaxid]):
    """An ``Evidenced`` NCBI taxid."""


class EvidencedAssay(Evidenced[AssayTerm]):
    """An ``Evidenced`` EFO/OBI assay CURIE."""


class EvidencedChemistrySet(Evidenced[list[ChemistryId]]):
    """An ``Evidenced`` chemistry equivalence class (benign twins recorded together, R6/§12)."""


class EvidencedAccessionList(Evidenced[list[Accession]]):
    """An ``Evidenced`` list of accessions."""


class EvidencedReadLayout(Evidenced[ReadLayout]):
    """An ``Evidenced`` read layout."""


class EvidencedGenome(Evidenced[GenomeRef]):
    """An ``Evidenced`` genome reference."""


class EvidencedRuntimeEnv(Evidenced[RuntimeEnv]):
    """An ``Evidenced`` liulab-runtime environment name."""


class FileInventoryItem(BaseModel):
    """One physical file + checksum + assigned role. No absolute path (R9).

    Identity (uri/sha256/size) is raw observed truth; the role assignment is the joint-optimization
    output, so ``read_id`` is ``Evidenced``.
    """

    uri: Uri
    basename: str
    sha256: Sha256
    size_bytes: int = Field(gt=0)
    read_id: EvidencedStr | None = None


class LibrarySection(BaseModel):
    """Physical truth. Authority = EVIDENCE."""

    assay: EvidencedAssay
    chemistry: EvidencedChemistrySet
    read_layout: EvidencedReadLayout
    onlists: list[Onlist] = Field(default_factory=list)
    files: list[FileInventoryItem]


class SampleGroup(BaseModel):
    """One biological sample and the files that carry it."""

    sample_id: str
    tissue: EvidencedStr | None = None
    condition: EvidencedStr | None = None
    file_uris: list[Uri] = Field(default_factory=list)


class ExperimentSection(BaseModel):
    """Biological/metadata truth. Authority = METADATA + humans."""

    organism: EvidencedTaxid
    accessions: EvidencedAccessionList
    samples: list[SampleGroup]


class ProcessingSection(BaseModel):
    """Intent. Authority = DERIVED from library + experiment + policy (basis usually ``inferred``)."""

    genome: EvidencedGenome
    aligner: EvidencedStr
    quantification: EvidencedStr
    variant_calling: EvidencedBool
    environment: EvidencedRuntimeEnv
    resources: ResourceHints = Field(default_factory=ResourceHints)


class Provenance(BaseModel):
    """Binds a compiled config to the inputs and tool versions that produced it."""

    manifest_hash: str
    kb_version: str
    workflow_version: str
    seqforge_version: str


class Manifest(BaseModel):
    """One machine-independent manifest. ``compose()`` is a pure function of the three sections.

    Structural only: semantic cross-checks (referential integrity — every experiment ``file_uri`` in
    the library inventory — and the R9 no-absolute-path sweep) are enforced by ``manifest validate``
    as ``Blocker``s, not as construction-time ``ValidationError``s.
    """

    model_config = ConfigDict(frozen=True)

    library: LibrarySection
    experiment: ExperimentSection
    processing: ProcessingSection
    provenance: Provenance
