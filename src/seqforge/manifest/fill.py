"""``manifest fill`` — assemble the three-section :class:`Manifest` from a resolve Decision.

Each section keeps its own authority (design §1.6):
- ``library``   = **evidence**. Chemistry, read layout, and the file->role assignment all come from
  the winning candidate, so every field is ``basis="observed"`` with the file shas as evidence.
- ``experiment``= **metadata/humans**. Organism and accessions cannot be read off bytes, so they
  arrive as inputs (normally span-verified Assertions from ``harvest``) and are ``basis="asserted"``.
- ``processing``= **derived + policy** — ``basis="inferred"``.

The manifest is machine-independent (R9): a file's ``uri`` is its *basename*, never the absolute
local path the probe read (which stays in ``Observation.file.local_uri``, an internal-only field).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Literal

from ..io import OnlistRegistry
from ..kb import KB_VERSION
from ..kb.schema import Element, Spec
from ..models.manifest import (
    EvidencedAccessionList,
    EvidencedAssay,
    EvidencedBool,
    EvidencedChemistrySet,
    EvidencedGenome,
    EvidencedReadLayout,
    EvidencedRuntimeEnv,
    EvidencedStr,
    EvidencedTaxid,
    ExperimentSection,
    FileInventoryItem,
    GenomeRef,
    LibrarySection,
    Manifest,
    Onlist,
    ProcessingSection,
    Provenance,
    ReadDef,
    ReadElement,
    ReadLayout,
    SampleGroup,
)
from ..models.observation import Observation
from ..models.resolve import Candidate, ResolveResult
from ..workflows import WORKFLOW_VERSION
from .hash import manifest_content_hash
from .policy import processing_defaults

_SHA256 = re.compile(r"^[0-9a-f]{64}$")

#: KB element type -> the manifest's interpretive read-element role.
_ROLE_FOR_TYPE: dict[str, str] = {
    "barcode": "CB",
    "umi": "UMI",
    "cdna": "cDNA",
    "gdna": "gDNA",
    "linker": "linker",
    "fixed": "linker",
    "poly_t": "polyT",
    "poly_a": "polyA",
    "index": "index",
}

_MODALITY: dict[str, str] = {"rna": "rna", "atac": "atac", "multi": "rna"}


class FillError(RuntimeError):
    """The resolve result cannot be assembled into a manifest (no clean Decision)."""


@dataclass(frozen=True)
class ExperimentInputs:
    """Biological truth that bytes cannot supply — normally span-verified Assertions from harvest."""

    organism_taxid: int
    accessions: list[str] = field(default_factory=list)
    samples: list[SampleGroup] = field(default_factory=list)
    confidence: float = 0.9


@dataclass(frozen=True)
class ProcessingInputs:
    """Reference selection (a liulab-genome assembly id + a REGISTERED GTF name — never a path)."""

    assembly: str
    annotation_name: str


def fill_manifest(
    *,
    result: ResolveResult,
    spec: Spec,
    observations: list[Observation],
    registry: OnlistRegistry,
    experiment: ExperimentInputs,
    processing: ProcessingInputs,
    seqforge_version: str,
) -> Manifest:
    """Assemble a :class:`Manifest` from a clean resolve Decision + metadata/policy inputs."""
    if result.blockers:
        raise FillError(f"cannot fill a manifest over {len(result.blockers)} unresolved Blocker(s)")
    if not result.candidates:
        raise FillError("resolve produced no candidates")
    winner = result.candidates[0]
    if winner.score.status != "scored":
        raise FillError(f"winning candidate {winner.technology!r} is forbidden, not a Decision")
    if winner.technology != spec.identity.id:
        raise FillError(
            f"spec {spec.identity.id!r} does not match the winning candidate {winner.technology!r}"
        )
    if not spec.identity.assay_ontology:
        raise FillError(
            f"{spec.identity.id!r} has no assay_ontology CURIE — controlled vocabulary is required"
        )

    obs_by_sha = {o.file.sha256: o for o in observations}
    confidence = min(1.0, max(0.0, winner.score.value if winner.score.value is not None else 0.5))
    rung = winner.rung_resolved.get("chemistry", 2)
    evidence = sorted(obs_by_sha)

    library = LibrarySection(
        assay=EvidencedAssay(
            value=spec.identity.assay_ontology[0],
            basis="observed",
            evidence=evidence,
            confidence=confidence,
            rung=rung,
        ),
        chemistry=EvidencedChemistrySet(
            # the §12 equivalence class: benign twins are recorded together, machine-visibly
            value=sorted({winner.technology, *winner.equivalence_members}),
            basis="observed",
            evidence=evidence,
            confidence=confidence,
            rung=rung,
        ),
        read_layout=EvidencedReadLayout(
            value=_build_read_layout(spec, winner, obs_by_sha),
            basis="observed",
            evidence=evidence,
            confidence=confidence,
            rung=rung,
        ),
        onlists=_build_onlists(spec, registry),
        files=_build_files(winner, observations, confidence, rung),
    )

    experiment_section = ExperimentSection(
        organism=EvidencedTaxid(
            value=experiment.organism_taxid,
            basis="asserted",
            confidence=experiment.confidence,
            rung=0,
        ),
        accessions=EvidencedAccessionList(
            value=list(experiment.accessions),
            basis="asserted",
            confidence=experiment.confidence,
            rung=0,
        ),
        samples=list(experiment.samples),
    )

    defaults = processing_defaults(spec)
    processing_section = ProcessingSection(
        genome=EvidencedGenome(
            value=GenomeRef(
                assembly=processing.assembly,
                annotation_name=processing.annotation_name,
                ncbi_taxid=experiment.organism_taxid,
            ),
            basis="inferred",
            confidence=0.8,
            rung=0,
        ),
        aligner=EvidencedStr(value=defaults.aligner, basis="inferred", confidence=0.95, rung=rung),
        quantification=EvidencedStr(
            value=defaults.quantification, basis="inferred", confidence=0.8, rung=rung
        ),
        variant_calling=EvidencedBool(
            value=defaults.variant_calling, basis="inferred", confidence=0.9, rung=0
        ),
        environment=EvidencedRuntimeEnv(
            value=defaults.environment, basis="inferred", confidence=0.95, rung=0
        ),
    )

    draft = Manifest(
        library=library,
        experiment=experiment_section,
        processing=processing_section,
        provenance=Provenance(
            manifest_hash="",
            kb_version=KB_VERSION,
            workflow_version=WORKFLOW_VERSION,
            seqforge_version=seqforge_version,
        ),
    )
    # the hash covers only the three truth sections, so filling it in cannot perturb it
    return draft.model_copy(
        update={
            "provenance": Provenance(
                manifest_hash=manifest_content_hash(draft),
                kb_version=KB_VERSION,
                workflow_version=WORKFLOW_VERSION,
                seqforge_version=seqforge_version,
            )
        }
    )


def _build_read_layout(
    spec: Spec, winner: Candidate, obs_by_sha: dict[str, Observation]
) -> ReadLayout:
    """Declared element structure (KB) x observed read geometry (the assigned file's bytes)."""
    reads: list[ReadDef] = []
    for read in spec.reads:
        sha = winner.role_assignment.assignment.get(read.id)
        if sha is None or sha not in obs_by_sha:
            raise FillError(f"role {read.id!r} has no assigned file in the winning candidate")
        profile = obs_by_sha[sha].read_length
        reads.append(
            ReadDef(
                read_id=read.id,
                strand=read.strand,
                min_len=profile.min_len,  # observed, not merely declared
                max_len=profile.max_len,
                elements=[_read_element(el, spec) for el in read.elements],
            )
        )
    modality = _MODALITY.get(spec.identity.modality, "rna")
    return ReadLayout(modality=modality, reads=reads)  # type: ignore[arg-type]


def _read_element(el: Element, spec: Spec) -> ReadElement:
    length = el.end - el.start if (el.start is not None and el.end is not None) else None
    onlist_ref = spec.onlists[el.onlist].registry if el.onlist else None
    return ReadElement(
        role=_ROLE_FOR_TYPE.get(el.type, "linker"),  # type: ignore[arg-type]
        region_type=el.seqspec_region_type,
        start=el.start,
        length=length,
        min_len=el.min_len,
        max_len=el.max_len,
        sequence=el.sequence,
        onlist_ref=onlist_ref,
    )


def _build_onlists(spec: Spec, registry: OnlistRegistry) -> list[Onlist]:
    """Registry-backed whitelist entries for the onlists this chemistry's ELEMENTS actually use.

    An onlist referenced only by an ``excludes`` anti-gate is a detection probe, not part of the
    library, and is not recorded. A registry entry without a real URI + sha256 (a declared-but-not-
    materialized real list) is skipped — ``validate`` surfaces that as a warning, not a silent pass.
    """
    used = {el.onlist for read in spec.reads for el in read.elements if el.onlist}
    out: list[Onlist] = []
    for alias in sorted(used):
        name = spec.onlists[alias].registry
        if not registry.has(name):
            continue
        entry = registry.get(name)
        if not entry.uri or not _SHA256.match(entry.sha256):
            continue  # declared but not materialized (e.g. a license-restricted real list)
        hint: Literal["forward", "reverse_complement"] | None
        hint = {"forward": "forward", "revcomp": "reverse_complement"}.get(entry.orientation)  # type: ignore[assignment]
        out.append(
            Onlist(
                name=name,
                uri=entry.uri,
                sha256=entry.sha256,
                length=entry.width,
                orientation_hint=hint,
                n_entries=entry.n_entries,
            )
        )
    return out


def _build_files(
    winner: Candidate, observations: list[Observation], confidence: float, rung: int
) -> list[FileInventoryItem]:
    """File identity is raw observed truth; the role assignment is the joint-optimization output."""
    role_of_sha = {sha: role for role, sha in winner.role_assignment.assignment.items()}
    items: list[FileInventoryItem] = []
    for obs in observations:
        role = role_of_sha.get(obs.file.sha256)
        read_id = (
            EvidencedStr(
                value=role,
                basis="observed",
                evidence=[obs.file.sha256],
                confidence=confidence,
                rung=rung,
            )
            if role is not None
            else None
        )
        items.append(
            FileInventoryItem(
                uri=obs.file.basename,  # relative; never Observation.file.local_uri (R9)
                basename=obs.file.basename,
                sha256=obs.file.sha256,
                size_bytes=obs.file.size_bytes,
                read_id=read_id,
            )
        )
    return items
