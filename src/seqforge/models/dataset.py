""":class:`DatasetManifest` — a finished assay. Two truths, one lifetime, IMMUTABLE (R9/R13).

``library`` is physical truth about molecules and sequencer output (authority = **evidence**);
``experiment`` is biological/metadata truth (authority = **metadata and humans**). Both are claims
about *what the data is*. A finished assay does not change, so neither does this file: it is
content-addressed, and nothing downstream may write to it. When a new fact arrives, the manifest is
rebuilt from evidence and gets a new hash. It is never patched.

**What is deliberately absent is the point.** There is no ``processing`` section here. What we choose
to *do* with an assay is intent, not truth, and it lives in
:mod:`~seqforge.models.processing` — of which there are many per dataset. The old three-section
manifest made "three truths" (R6's three *bases*) and "three sections" line up by coincidence, and the
pun cost us: ``processing`` inherited the grammar of a truth — ``Evidenced`` fields, an "authority", a
uniform ``basis="inferred"`` stamped on by construction — and then ``compose`` read almost none of
them. A field that is never read can never produce the ``Conflict`` R6 promises. ``base.py`` listing
**four** bases against three sections was the tell.

This module imports nothing from ``processing``, and must not. That independence is R13 as an import
graph: a dataset cannot know how it will be processed, because it will be processed many ways.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from .base import Evidenced, Sha256, Uri
from .evidenced import (
    EvidencedAccessionList,
    EvidencedAssay,
    EvidencedChemistrySet,
    EvidencedStr,
    EvidencedTaxid,
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


class EvidencedReadLayout(Evidenced[ReadLayout]):
    """An ``Evidenced`` read layout."""


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


class DatasetProvenance(BaseModel):
    """Binds a dataset manifest to the bytes and the KB that read them.

    ``workflow_version`` is deliberately ABSENT. Nothing in ``library``/``experiment`` depends on
    which Snakemake module will one day run: the assay happened before we had an opinion about it.
    It belongs to the processing manifest, and folding it in here would make a dataset's identity
    churn every time a rule file changed — which is exactly the coupling R13 removes.
    """

    dataset_hash: str
    kb_version: str
    seqforge_version: str


class DatasetManifest(BaseModel):
    """A finished assay: what the bench did. Two truths, two authorities, one lifetime (R13).

    Structural only: semantic cross-checks (referential integrity — every experiment ``file_uri`` in
    the library inventory — and the R9 no-absolute-path sweep) are enforced by ``manifest validate``
    as ``Blocker``s, not as construction-time ``ValidationError``s.
    """

    model_config = ConfigDict(frozen=True)

    library: LibrarySection
    experiment: ExperimentSection
    provenance: DatasetProvenance


__all__ = [
    "ReadElement",
    "ReadDef",
    "ReadLayout",
    "EvidencedReadLayout",
    "Onlist",
    "FileInventoryItem",
    "LibrarySection",
    "SampleGroup",
    "ExperimentSection",
    "DatasetProvenance",
    "DatasetManifest",
]
