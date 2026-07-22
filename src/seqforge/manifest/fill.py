"""``manifest fill`` — assemble the two-section :class:`DatasetManifest` from a resolve Decision.

Each section keeps its own authority (design §1.6):

- ``library``   = **evidence**. Chemistry, read layout, and the file->role assignment all come from
  the winning candidate, so every field is ``basis="observed"`` with the file shas as evidence.
- ``experiment``= **metadata/humans**. Organism and accessions cannot be read off bytes, so they
  arrive as inputs (normally span-verified Assertions from ``harvest``) and are ``basis="asserted"``.

**There is no third section, and `fill` takes no genome.** Intent lives in a separate
:class:`~seqforge.models.processing.ProcessingManifest`, built by :func:`fill_processing`. That is
also why ``--assembly``/``--annotation`` left this verb: choosing a reference is not something you
learn by probing bytes, and it never belonged on the verb that probes them.

The manifest is machine-independent: a file's ``uri`` is its path **relative to the dataset's
own root**, never the absolute local path the probe read (which stays in
``Observation.file.local_uri``, an internal-only field). Relative, not *flat* — see
:func:`dataset_uris` for the two things a bare basename broke on the first real dataset.
"""

from __future__ import annotations

import os
import re
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

from ..io import OnlistRegistry
from ..kb import KB_VERSION
from ..kb.schema import Element, Spec
from ..models.blocker import ValidationWarning
from ..models.dataset import (
    AssayLabel,
    DatasetManifest,
    DatasetProvenance,
    ExperimentSection,
    FileInventoryItem,
    LibrarySection,
    Onlist,
    ReadDef,
    ReadElement,
    ReadLayout,
    SampleGroup,
    Study,
)
from ..models.evidenced import (
    EvidencedAccessionList,
    EvidencedChemistrySet,
    EvidencedTaxid,
)
from ..models.observation import Observation
from ..models.processing import (
    DatasetPin,
    ProcessingManifest,
    ProcessingProvenance,
    RuntimeEnv,
    SoloFeature,
)
from ..models.resolve import Candidate, MetadataResolution, ResolveResult
from ..workflows import WORKFLOW_VERSION
from .hash import dataset_content_hash, processing_content_hash
from .instruct import Instruction
from .policy import ProcessingOverrides, resolve_processing

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
    """Biological truth that bytes cannot supply.

    Normally built by :func:`experiment_from_metadata` out of a
    :class:`~seqforge.models.resolve.MetadataResolution`, so every value here traces to a record or a
    span-verified assertion. The docstring used to say "normally span-verified Assertions from
    harvest" while the only caller passed a CLI flag and an empty sample list — an aspiration written
    in the present tense, which is how a comment becomes a lie.
    """

    organism: EvidencedTaxid
    accessions: list[str] = field(default_factory=list)
    samples: list[SampleGroup] = field(default_factory=list)
    study: Study | None = None
    #: Confidence for ``accessions`` only: a list of accessions a human typed or a record declared.
    accession_confidence: float | None = None


def experiment_from_metadata(
    resolution: MetadataResolution,
    observations: list[Observation],
    *,
    organism_taxid: int | None = None,
    uris: dict[str, str] | None = None,
) -> ExperimentInputs:
    """A :class:`MetadataResolution` -> the manifest's experiment inputs. The only conversion.

    The resolver speaks in file **hashes**, because that is what a sample is carried by and it has no
    business knowing what a URI is; the manifest speaks in URIs, because a manifest is
    machine-independent. :func:`dataset_uris` owns that translation and always has — this is its
    second caller, and the first one that was written knowing it existed. (The first time
    ``SampleGroup.file_uris`` was built beside it out of basenames, ``manifest fill`` refused its own
    manifest with six referential-integrity Blockers.)

    ``organism_taxid`` overrides the record — a flag beats a database, which is the same precedence
    the processing manifest uses and for the same reason: a human typing a taxid is asserting it now,
    about this data, having looked.
    """
    # ``uris`` is optional and, when given, dataset-wide: a multi-assay fill computes ONE map over
    # every assay's files (the dataset root) and threads it in, so a sample in a deeper per-assay
    # subdir still gets a URI relative to the same root ``--fastq-dir`` will use. Omitted (the
    # single-assay / single-run callers), it degenerates to the local set, byte-identical to before.
    if uris is None:
        uris = dataset_uris(observations)
    samples = [
        SampleGroup(
            sample_id=s.sample_id,
            accession=s.accession,
            attributes=dict(s.attributes),
            file_uris=[uris[sha] for sha in s.file_shas if sha in uris],
        )
        for s in resolution.samples
    ]
    if organism_taxid is not None:
        organism = EvidencedTaxid(value=organism_taxid, basis="user_confirmed", rung=0)
    elif resolution.organism is not None:
        organism = resolution.organism
    else:
        raise FillError(
            "no organism: the archive record does not declare one and none was given. Pass "
            "`--organism <taxid>`. There is no default, and there must not be — a wrong taxid aligns "
            "cleanly against the wrong genome, exits 0, and nothing downstream ever asks again."
        )
    accessions = sorted({s.accession for s in resolution.samples if s.accession})
    if resolution.project is not None and resolution.project.accession:
        accessions.append(resolution.project.accession)
    study = Study(**resolution.project.model_dump()) if resolution.project is not None else None
    return ExperimentInputs(
        organism=organism,
        accessions=sorted(set(accessions)),
        samples=samples,
        study=study,
        accession_confidence=None,  # transcribed from the record; no judgement was made
    )


@dataclass(frozen=True)
class ProcessingInputs:
    """CLI-typed processing choices — the top of the precedence ladder.

    Reference selection is a liulab-genome assembly id + a REGISTERED GTF name, never a path.
    This dataclass predates the split and was already the processing manifest in miniature — badly
    named and half-built, sitting beside `fill` instead of owning the artifact it describes.
    """

    assembly: str | None = None
    annotation_name: str | None = None
    features: tuple[SoloFeature, ...] | None = None  # --quantify: EXACT replacement
    threads: int | None = None
    environment: RuntimeEnv | None = None


def fill_manifest(
    *,
    result: ResolveResult,
    spec: Spec,
    observations: list[Observation],
    registry: OnlistRegistry,
    experiment: ExperimentInputs,
    seqforge_version: str,
    role_of_sha: dict[str, str] | None = None,
    specs: dict[str, Spec] | None = None,
    uris: dict[str, str] | None = None,
) -> DatasetManifest:
    """Assemble a :class:`DatasetManifest` from a clean resolve Decision + metadata inputs.

    Bytes and metadata only. Takes no ``processing`` argument, by construction: a dataset does not
    know how it will be processed, because it will be processed many ways.

    ``role_of_sha`` carries the **dataset-level** file->role map, which a single `ResolveResult`
    cannot express: its `RoleAssignment` maps role -> one sha, because it describes one library's
    reads, and a six-run dataset has six R1s. `resolve_runs` resolves each run on its own bytes and
    merges the inverse map; pass it here. Omitted, the winner's own assignment is used — correct for
    a genuinely single-run dataset, and the reason this parameter is optional rather than required.
    """
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
    # ONE decision, so ONE confidence and ONE rung. `chemistry` carries them; everything else in
    # `library` is a consequence of that decision and carries no envelope of its own. See
    # `LibrarySection` for why four copies of one number was never four truths.
    confidence = min(1.0, max(0.0, winner.score.value if winner.score.value is not None else 0.5))
    rung = winner.rung_resolved.get("chemistry", 2)
    chemistry = sorted({winner.technology, *winner.equivalence_members})

    library = LibrarySection(
        chemistry=EvidencedChemistrySet(
            # the §12 equivalence class: benign twins are recorded together, machine-visibly
            value=chemistry,
            basis="observed",
            evidence=sorted(obs_by_sha),
            confidence=confidence,
            rung=rung,
        ),
        assay=_assay_labels(chemistry, specs),
        read_layout=_build_read_layout(spec, winner, obs_by_sha),
        onlists=_build_onlists(spec, registry),
        files=_build_files(winner, observations, role_of_sha, uris=uris),
    )

    experiment_section = ExperimentSection(
        organism=experiment.organism,
        accessions=EvidencedAccessionList(
            value=list(experiment.accessions),
            basis="asserted",
            confidence=experiment.accession_confidence,
            rung=0,
        ),
        samples=list(experiment.samples),
        study=experiment.study,
    )

    draft = DatasetManifest(
        library=library,
        experiment=experiment_section,
        provenance=DatasetProvenance(
            dataset_hash="",
            kb_version=KB_VERSION,
            seqforge_version=seqforge_version,
        ),
    )
    # the hash covers only the two truth sections, so filling it in cannot perturb it
    return draft.model_copy(
        update={
            "provenance": DatasetProvenance(
                dataset_hash=dataset_content_hash(draft),
                kb_version=KB_VERSION,
                seqforge_version=seqforge_version,
            )
        }
    )


def fill_processing(
    *,
    spec: Spec,
    dataset: DatasetManifest,
    processing: ProcessingInputs,
    instructions: Sequence[Instruction] = (),
    prep_type: str | None = None,
    processing_id: str = "default",
    pin: bool = True,
    seqforge_version: str,
) -> tuple[ProcessingManifest, list[ValidationWarning]]:
    """Build one :class:`ProcessingManifest` for a dataset: policy -> instructions -> flags.

    ``pin=True`` binds it to this dataset's hash, so ``compose`` refuses any other. ``pin=False``
    leaves it a **template**: portable across datasets, which is what lets a single file drive a whole
    corpus. Both forms are legitimate and they are for different jobs — you publish a bound one and
    you reprocess with a template.

    Precedence itself lives in :func:`~seqforge.manifest.policy.resolve_processing`, and only there.
    """
    section, warnings = resolve_processing(
        spec=spec,
        dataset=dataset,
        instructions=instructions,
        prep_type=prep_type,
        overrides=ProcessingOverrides(
            assembly=processing.assembly,
            annotation_name=processing.annotation_name,
            features=processing.features,
            threads=processing.threads,
            environment=processing.environment,
        ),
    )
    draft = ProcessingManifest(
        processing_id=processing_id,
        dataset=(
            DatasetPin(
                dataset_hash=dataset.provenance.dataset_hash,
                accessions=list(dataset.experiment.accessions.value),
            )
            if pin
            else None
        ),
        processing=section,
        provenance=ProcessingProvenance(
            processing_hash="",
            workflow_version=WORKFLOW_VERSION,
            seqforge_version=seqforge_version,
        ),
    )
    return (
        draft.model_copy(
            update={
                "provenance": ProcessingProvenance(
                    processing_hash=processing_content_hash(draft),
                    workflow_version=WORKFLOW_VERSION,
                    seqforge_version=seqforge_version,
                )
            }
        ),
        warnings,
    )


def _assay_labels(chemistry: list[str], specs: dict[str, Spec] | None) -> list[AssayLabel]:
    """The chemistry set, spelled in EFO. One label per member — including the §12 twin.

    This is where the pilot's ``assay: EFO:0009922`` beside ``chemistry: [v3, v3.1]`` came from: the
    assay field held one CURIE and the chemistry field held two ids, so v3.1's own term
    (``EFO:0022980``) was silently dropped and the two fields read as if they disagreed. They never
    did. They are the same answer, and now they are the same shape.

    A member whose spec declares no CURIE, or whose CURIE has no shipped EFO label, is **skipped
    rather than guessed at** — ``kb lint`` refuses both, so reaching that branch means a spec got in
    without linting and a blank name would hide it.
    """
    from ..io.efo import has_term, term
    from ..kb import load_all_specs

    all_specs = specs if specs is not None else load_all_specs()
    out: list[AssayLabel] = []
    for chem in chemistry:
        s = all_specs.get(chem)
        if s is None or not s.identity.assay_ontology:
            continue
        curie = s.identity.assay_ontology[0]
        if not has_term(curie):
            continue
        out.append(AssayLabel(chemistry=chem, curie=curie, name=term(curie).name))
    return out


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


def dataset_uris(observations: list[Observation]) -> dict[str, str]:
    """sha256 -> the file's URI: its path **relative to the dataset's own root**.

    **Public, and that is the point.** The URI form has exactly one owner, because the moment it had
    two they disagreed: this function got it right and `cli.py` built `SampleGroup.file_uris` out of
    basenames beside it, so `manifest fill` refused its own manifest with six referential-integrity
    Blockers ("sample 'SRR28716553' references 'SRR28716553_1.fastq.gz', which is not in the library
    file inventory"). The validator did its job; the duplication was the bug. One function, every
    caller.

    Not the basename, which is what this was. Two things broke on the first real dataset — 6 runs
    that ``fasterq-dump`` had written one directory per accession
    (``SRX24283130/SRR28716558_1.fastq.gz``):

    1. ``compose --fastq-dir <root>`` joins the URI to the root, so bare basenames resolved to
       ``<root>/SRR28716558_1.fastq.gz`` — a path that does not exist, in a `units.tsv` that looks
       perfectly reasonable.
    2. Worse and silent: a basename is **not unique**. Two runs each carrying ``reads_1.fastq.gz`` in
       their own directory collapse to one URI, and `_units` looks files up *by URI* — so one run's
       reads would quietly become the other's. Nothing would have said so.

    A path relative to the common root keeps every URI distinct and machine-independent, which is all
    machine-independence ever asked for: it forbids an *absolute* path, not structure. A flat
    directory degenerates to the basenames this always produced. Files with no shared root fall back
    to basenames — there is no relative name that spans two filesystems, and inventing one would be
    worse than the fallback.
    """
    locals_ = {o.file.sha256: o.file.local_uri for o in observations if o.file.local_uri}
    if len(locals_) == len(observations) > 0:
        paths = [Path(p) for p in locals_.values()]
        try:
            root = Path(os.path.commonpath([str(p.parent.resolve()) for p in paths]))
        except ValueError:  # different drives / no common root
            root = None
        if root is not None:
            return {sha: str(Path(p).resolve().relative_to(root)) for sha, p in locals_.items()}
    return {o.file.sha256: o.file.basename for o in observations}


def _build_files(
    winner: Candidate,
    observations: list[Observation],
    role_of_sha: dict[str, str] | None = None,
    uris: dict[str, str] | None = None,
) -> list[FileInventoryItem]:
    """File identity is raw observed truth; the role is the other half of the chemistry decision.

    No confidence per file: the assignment and the chemistry came out of one joint optimization, and
    ``library.chemistry`` carries its score. Twelve files each restating it is one number thirteen
    times, which is exactly what the pilot's manifest looked like.
    """
    # A dataset-wide ``uris`` (multi-assay fill) overrides the local computation so every assay's
    # files are anchored on the same root; omitted, we compute it here as before.
    if uris is None:
        uris = dataset_uris(observations)
    if role_of_sha is None:
        # Single-run fill: build the map here, tagging index-sized leftovers the same way the
        # dataset-level path does, so a stray 10x I1/I2 is set aside rather than left to block.
        from ..resolve.engine import index_tagged_roles

        role_of_sha = index_tagged_roles(winner, observations)
    return [
        FileInventoryItem(
            # relative to the dataset root; never Observation.file.local_uri, which is absolute
            uri=uris[obs.file.sha256],
            basename=obs.file.basename,
            sha256=obs.file.sha256,
            size_bytes=obs.file.size_bytes,
            read_id=role_of_sha.get(obs.file.sha256),
        )
        # Sorted by content hash so `library.files` — and the dataset content hash computed over it —
        # is byte-identical however the observations were ordered (a forked probe pool need not return
        # them in submission order). GSE208154 hashed differently at --cpus 1 vs 4 before this.
        for obs in sorted(observations, key=lambda o: o.file.sha256)
    ]
