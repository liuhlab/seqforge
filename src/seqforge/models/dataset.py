""":class:`DatasetManifest` — a finished assay. Two truths, one lifetime, IMMUTABLE.

``library`` is physical truth about molecules and sequencer output (authority = **evidence**);
``experiment`` is biological/metadata truth (authority = **metadata and humans**). Both are claims
about *what the data is*. A finished assay does not change, so neither does this file: it is
content-addressed, and nothing downstream may write to it. When a new fact arrives, the manifest is
rebuilt from evidence and gets a new hash. It is never patched.

**What is deliberately absent is the point.** There is no ``processing`` section here. What we choose
to *do* with an assay is intent, not truth, and it lives in
:mod:`~seqforge.models.processing` — of which there are many per dataset. The old three-section
manifest made "three truths" (the three *bases*) and "three sections" line up by coincidence, and the
pun cost us: ``processing`` inherited the grammar of a truth — ``Evidenced`` fields, an "authority", a
uniform ``basis="inferred"`` stamped on by construction — and then ``compose`` read almost none of
them. A field that is never read can never produce the ``Conflict`` the three-truths rule promises. ``base.py`` listing
**four** bases against three sections was the tell.

This module imports nothing from ``processing``, and must not. That independence is the two-artifact
split expressed as an import graph: a dataset cannot know how it will be processed, because it will be
processed many ways.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from .base import Accession, AssayTerm, ChemistryId, Evidenced, Sha256, Uri
from .evidenced import (
    EvidencedAccessionList,
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
    """One physical file + checksum + assigned role. No absolute path.

    ``read_id`` is a plain string, not ``Evidenced``, and that is not a demotion. The role assignment
    and the chemistry come out of **one** joint optimization — ``Candidate`` is literally
    ``(technology, score, role_assignment)``, and the score scores the pair — so the confidence in
    "this file is R1" and the confidence in "this library is v3" are the same number, and
    ``library.chemistry`` is where it lives. Twelve files each carrying a copy of it was the pilot's
    manifest saying one thing thirteen times.
    """

    uri: Uri
    basename: str
    sha256: Sha256
    size_bytes: int = Field(gt=0)
    #: The read role the joint optimization assigned, e.g. ``R1``. ``None`` = unassigned, which
    #: ``validate`` surfaces: an unassigned file is one ``compose`` will silently skip.
    read_id: str | None = None


class AssayLabel(BaseModel):
    """One chemistry, spelled three ways: our key, EFO's id, and EFO's words.

    This exists because ``assay: EFO:0009922`` beside ``chemistry: [10x-3p-gex-v3,
    10x-3p-gex-v3.1]`` was two puzzles at once. *What is the difference between them?* — none, they
    are the same fact in two vocabularies. *Why is one a list and the other not?* — because the
    §12 equivalence class has two members and the assay field could only hold one, so it silently
    dropped v3.1's CURIE.

    Making it a label per chemistry answers both and removes a third problem nobody had noticed: an
    assay CURIE that disagrees with the chemistry it names is now **inexpressible** rather than
    merely absent. There is nowhere to write it.

    ``name`` is EFO's own label, generated into ``io/efo/labels.json`` from EBI's OLS4. Never typed
    here: a label we maintain by hand is a label that drifts from the ontology it claims to quote.
    """

    model_config = ConfigDict(frozen=True)

    #: The KB's primary key, e.g. ``10x-3p-gex-v3``.
    chemistry: ChemistryId
    #: The controlled-vocabulary id, e.g. ``EFO:0009922``.
    curie: AssayTerm
    #: What EFO calls it, e.g. ``10x 3' v3``. The thing a human reads.
    name: str


class LibrarySection(BaseModel):
    """Physical truth. Authority = EVIDENCE.

    **One decision, one confidence.** ``chemistry`` is the decision — the joint optimization over
    (which technology, which file is which read) that ``resolve score`` performs — and it is the only
    ``Evidenced`` field here. Everything else follows from it: ``assay`` is the same answer in EFO's
    vocabulary, ``read_layout`` is the KB's declared structure for that chemistry filled in with
    measured lengths, ``files[].read_id`` is the assignment half of the very same optimization, and
    ``onlists`` are the whitelists that chemistry uses.

    They used to each carry their own ``Evidenced`` envelope, and the pilot's manifest showed what
    that bought: ``confidence: 0.750672`` printed four times, identical, because it was always one
    number about one decision wearing four hats. Four envelopes cannot disagree — they were filled
    from the same variable — so they were never four truths, and we never asked for four. We ask
    that a value not travel without its provenance, which is exactly what one honest envelope does.
    A field repeated is not provenance; it is decoration that looks like provenance, which is worse
    than none.
    """

    chemistry: EvidencedChemistrySet
    #: One per chemistry in the equivalence class, same order. Derived, so it carries no confidence.
    assay: list[AssayLabel] = Field(default_factory=list)
    read_layout: ReadLayout
    onlists: list[Onlist] = Field(default_factory=list)
    files: list[FileInventoryItem]


class SampleGroup(BaseModel):
    """One biological sample, the files that carry it, and what is declared about it.

    ``attributes`` is keyed by an **NCBI harmonized BioSample attribute name** — ``strain``,
    ``tissue``, ``dev_stage``, ``genotype``, all 960 of them — and the validator refuses anything
    else. Two named fields (``tissue``, ``condition``) used to sit here instead, and both were wrong
    in the same way:

    - ``condition`` is not a vocabulary anyone else uses. We invented it, and a field named
      "condition" accepts anything you can call a condition; on the pilot a language model filed worm
      husbandry into it. NCBI's ``treatment`` / ``genotype`` / ``disease`` are the real keys, each
      with a definition somebody else maintains.
    - Two typed fields cannot hold ``strain``, and ``strain`` is the only structured field that
      separates the pilot's wild-type samples from its daf-2 mutants.

    An open dict rather than 960 pydantic fields, because a typed list mirroring somebody else's
    vocabulary is the shape this repo keeps getting bitten by: it rots the moment they add an
    attribute, and nothing goes red. The vocabulary is the file NCBI's list was generated into, and
    the validator reads *it*.
    """

    sample_id: str
    #: The archive's id for this sample, when there is one. ``None`` for a dataset that never went
    #: near an archive — which is most of them, and not a defect.
    accession: Accession | None = None
    attributes: dict[str, EvidencedStr] = Field(default_factory=dict)
    file_uris: list[Uri] = Field(default_factory=list)

    @field_validator("attributes")
    @classmethod
    def _keys_are_ncbi_attributes(cls, value: dict[str, EvidencedStr]) -> dict[str, EvidencedStr]:
        """Fail closed on a key NCBI does not define: no field enters a manifest unvalidated."""
        from ..io.attributes import is_attribute

        unknown = sorted(k for k in value if not is_attribute(k))
        if unknown:
            raise ValueError(
                f"not NCBI harmonized BioSample attribute name(s): {unknown}. A sample attribute's "
                f"key space is NCBI's 960 curated names (`seqforge io attributes list`), not ours."
            )
        return value


class Study(BaseModel):
    """The study these files came from, as the archive declares it.

    Deliberately **not** ``Evidenced``: none of it is an interpretation. The record says the title is
    X and we copied X, exactly as we copy a file's ``sha256``. The abstract is deliberately absent —
    it is prose, it belongs in a document a quote can grep back into, and pasting a paragraph of
    English into a content-addressed manifest would make the dataset's identity depend on it.
    """

    accession: Accession | None = None
    title: str | None = None
    center: str | None = None
    data_type: str | None = None
    released: str | None = None


class ExperimentSection(BaseModel):
    """Biological/metadata truth. Authority = METADATA + humans."""

    organism: EvidencedTaxid
    accessions: EvidencedAccessionList
    samples: list[SampleGroup]
    study: Study | None = None


class DatasetProvenance(BaseModel):
    """Binds a dataset manifest to the bytes and the KB that read them.

    ``workflow_version`` is deliberately ABSENT. Nothing in ``library``/``experiment`` depends on
    which Snakemake module will one day run: the assay happened before we had an opinion about it.
    It belongs to the processing manifest, and folding it in here would make a dataset's identity
    churn every time a rule file changed — which is exactly the coupling the two-artifact split removes.
    """

    dataset_hash: str
    kb_version: str
    seqforge_version: str


class DatasetManifest(BaseModel):
    """A finished assay: what the bench did. Two truths, two authorities, one lifetime.

    Structural only: semantic cross-checks (referential integrity — every experiment ``file_uri`` in
    the library inventory — and the no-absolute-path sweep) are enforced by ``manifest validate``
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
    "AssayLabel",
    "LibrarySection",
    "SampleGroup",
    "Study",
    "ExperimentSection",
    "DatasetProvenance",
    "DatasetManifest",
]
