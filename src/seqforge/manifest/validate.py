"""``manifest validate`` — refusal is an exit code, not a vibe.

Returns a structured :class:`ValidationReport`; the CLI maps it to the uniform exit contract
(``0`` OK, ``3`` BLOCKED, ``4`` NEEDS_HUMAN). ``manifest.yaml`` is written **only** after a clean
validate.

Type-level guards (the ``Uri`` validator's absolute-path rejection, the ``AssayTerm`` CURIE pattern,
the ``Sha256`` pattern) already fail at *construction*; this pass owns the checks Pydantic cannot do
locally — **referential integrity across sections**, controlled-vocabulary presence, and
role/layout/onlist coherence. The absolute-path sweep is kept as defence in depth: no-absolute-path is
the rule most expensive to get wrong, so it is enforced twice.
"""

from __future__ import annotations

from ..models.blocker import Blocker, BlockerCode, BlockerSubject, ValidationWarning
from ..models.conflict import Conflict
from ..models.dataset import INDEX_ROLE, DatasetManifest
from ..models.processing import ProcessingManifest
from ..models.resolve import ValidationReport


def _looks_absolute(uri: str) -> bool:
    return (
        uri.startswith(("/", "~"))
        or uri.startswith("file:///")
        or (len(uri) > 1 and uri[1] == ":")
        or uri.startswith("\\\\")
    )


def validate_manifest(
    manifest: DatasetManifest,
    *,
    conflicts: list[Conflict] | None = None,
    warnings: list[ValidationWarning] | None = None,
) -> ValidationReport:
    """Validate a manifest's cross-section integrity. Any Blocker => not compilable.

    ``warnings`` seeds the report's advisory notes — the metadata resolver's non-blocking
    sample-attribute decisions (kept-by-precedence or left-null) arrive here — and never touch ``ok``.
    Only an ``open`` conflict or a Blocker makes a manifest non-compilable.
    """
    blockers: list[Blocker] = []
    warnings = list(warnings or [])
    open_conflicts = [c for c in (conflicts or []) if c.status == "open"]

    # --- no absolute/local path may ever reach a manifest (defence in depth) ---
    for f in manifest.library.files:
        if _looks_absolute(f.uri):
            blockers.append(
                Blocker(
                    id=f"blk-abspath-{f.sha256[:8]}",
                    code=BlockerCode.ABSOLUTE_PATH,
                    message=f"file uri {f.uri!r} is an absolute/local path.",
                    remedy="Use a relative path, a non-file scheme (s3://, gs://, https://), or an accession.",
                    subject=BlockerSubject(kind="file", ref=f.basename),
                )
            )
    for ol in manifest.library.onlists:
        if _looks_absolute(ol.uri):
            blockers.append(
                Blocker(
                    id=f"blk-abspath-onlist-{ol.name}",
                    code=BlockerCode.ABSOLUTE_PATH,
                    message=f"onlist {ol.name!r} uri {ol.uri!r} is an absolute/local path.",
                    remedy="Register the onlist by URL + sha256; it resolves to a cache path at run time.",
                    subject=BlockerSubject(kind="field", ref=f"library.onlists.{ol.name}"),
                )
            )

    # --- referential integrity: every experiment file_uri must exist in the library inventory ---
    inventory = {f.uri for f in manifest.library.files}
    for sample in manifest.experiment.samples:
        for uri in sample.file_uris:
            if uri not in inventory:
                blockers.append(
                    Blocker(
                        id=f"blk-refint-{sample.sample_id}-{uri}",
                        code=BlockerCode.UNRESOLVED_CONFLICT,
                        message=(
                            f"sample {sample.sample_id!r} references {uri!r}, which is not in the "
                            "library file inventory."
                        ),
                        remedy="Add the file to library.files, or correct the sample's file_uris.",
                        subject=BlockerSubject(
                            kind="field", ref=f"experiment.samples.{sample.sample_id}"
                        ),
                    )
                )

    # --- controlled vocabulary must be present (the corpus is only filterable if lineage is stable) ---
    if not manifest.library.chemistry.value:
        blockers.append(
            Blocker(
                id="blk-vocab-chemistry",
                code=BlockerCode.MISSING_CONTROLLED_VOCAB,
                message="library.chemistry is empty — no technology was recorded.",
                remedy="Re-run `seqforge resolve score`; a manifest requires a decided chemistry.",
                subject=BlockerSubject(kind="field", ref="library.chemistry"),
            )
        )

    # --- role/layout coherence: an assigned read_id must name a read in the layout ---
    # INDEX_ROLE is exempt: a technical sample-index read is deliberately not in the layout (STARsolo
    # never consumes it), so it is set aside rather than matched to a declared read.
    layout_roles = {r.read_id for r in manifest.library.read_layout.reads}
    for f in manifest.library.files:
        if f.read_id is not None and f.read_id != INDEX_ROLE and f.read_id not in layout_roles:
            blockers.append(
                Blocker(
                    id=f"blk-role-{f.sha256[:8]}",
                    code=BlockerCode.NO_VALID_ROLE_ASSIGNMENT,
                    message=(
                        f"{f.basename} is assigned role {f.read_id!r}, which is not a read in "
                        f"the declared layout ({sorted(layout_roles)})."
                    ),
                    remedy="Re-run `seqforge resolve score`; the role assignment must match the layout.",
                    subject=BlockerSubject(kind="file", ref=f.basename),
                )
            )
    for role in sorted(layout_roles):
        if not any(f.read_id == role for f in manifest.library.files):
            blockers.append(
                Blocker(
                    id=f"blk-unfilled-{role}",
                    code=BlockerCode.MISSING_TECHNICAL_READ,
                    message=f"the declared layout needs read {role!r}, but no file fills it.",
                    remedy=(
                        "Re-fetch with `fasterq-dump --include-technical`, or pull the original "
                        "submitted files `sra-pub-src-*` via the SRA Data Locator / SDL API."
                    ),
                    subject=BlockerSubject(kind="field", ref=f"library.read_layout.{role}"),
                )
            )

    # --- every file must have a role: a file with none is a file we will silently not process ---
    #
    # This is the check that was missing, and its absence is how a 6-run dataset validated clean while
    # 5/6 of it evaporated. `resolve` did ONE global assignment across all 12 files, so ten came back
    # with `read_id=None`; `compose._units` skips those without a word; the manifest was
    # content-addressed and blessed. Exit 0, wrong answer, no symptom.
    #
    # The inverse check above ("is every declared role filled?") passed the whole time, because it
    # only ever needed ONE file per role. Both directions are needed and only one existed.
    #
    # `read_id is None` still means *dropped*, and still blocks: a legitimately-ignored technical
    # index read is tagged INDEX_ROLE (not None) by the resolver's length gate, so it never reaches
    # here. The gate is why that stays honest — it only sets a leftover aside when the bytes say it is
    # index-sized (<= 20 bp); a cDNA-length leftover keeps read_id=None and blocks loudly below.
    for f in manifest.library.files:
        if f.read_id is None:
            blockers.append(
                Blocker(
                    id=f"blk-unassigned-{f.sha256[:8]}",
                    code=BlockerCode.NO_VALID_ROLE_ASSIGNMENT,
                    message=(
                        f"{f.basename} was given no read role, so the pipeline would not read it. "
                        f"Its reads would be dropped, and nothing downstream would say so."
                    ),
                    remedy=(
                        "Usually this means the files were resolved as one library when they are "
                        "several runs: use `seqforge manifest fill` on the whole set (it groups by "
                        "run and assigns roles per run), or drop the file if it does not belong to "
                        "this dataset."
                    ),
                    subject=BlockerSubject(kind="file", ref=f.basename),
                )
            )

    # --- onlists: a barcode element naming an unmaterialized whitelist is advisory, not fatal ---
    onlist_names = {o.name for o in manifest.library.onlists}
    for read in manifest.library.read_layout.reads:
        for el in read.elements:
            if el.onlist_ref and el.onlist_ref not in onlist_names:
                warnings.append(
                    ValidationWarning(
                        code="ONLIST_UNRESOLVED",
                        message=(
                            f"read {read.read_id} element {el.role} references onlist "
                            f"{el.onlist_ref!r}, which is not registered in this manifest; it must "
                            "resolve (URL + sha256) before compose can emit a whitelist path."
                        ),
                        subject=BlockerSubject(
                            kind="field", ref=f"library.read_layout.{read.read_id}"
                        ),
                    )
                )

    return ValidationReport(
        ok=not blockers and not open_conflicts,
        blockers=blockers,
        conflicts=open_conflicts,
        warnings=warnings,
    )


def validate_processing(
    processing: ProcessingManifest,
    *,
    dataset: DatasetManifest | None = None,
    conflicts: list[Conflict] | None = None,
) -> ValidationReport:
    """Validate one processing manifest, and its coherence with the dataset it will be paired with.

    Most of the intent surface needs no checking here: it is closed vocabulary enforced at
    construction (``SoloFeature``, ``RuntimeEnv``), and the parse/count line means a user has no
    vocabulary in which to contradict the bytes at all.

    **Genome is the exception, and it is the one worth the code.** A user may instruct
    ``assembly: hg38`` on a *C. elegans* dataset. That contradicts no byte — the probe cannot see
    organism — it contradicts ``experiment.organism``, which is itself ``asserted``. And a
    wrong-but-valid assembly is the worst failure this system can produce: STAR aligns, exits 0, and
    emits a plausible matrix in the wrong coordinate space. Every other check in this file catches
    something that would otherwise crash or look empty; this one catches something that looks *fine*.

    Deliberately narrow: it fires only when the manifest already carries an ``ncbi_taxid`` for the
    genome. A full assembly->taxid table belongs in ``liulab-genome``, not here.
    """
    blockers: list[Blocker] = []
    open_conflicts = [c for c in (conflicts or []) if c.status == "open"]
    genome = processing.processing.genome.value

    if dataset is not None:
        pin = processing.dataset
        if pin is not None and pin.dataset_hash != dataset.provenance.dataset_hash:
            blockers.append(
                Blocker(
                    id="blk-pin-mismatch",
                    code=BlockerCode.DATASET_PIN_MISMATCH,
                    message=(
                        f"processing manifest {processing.processing_id!r} is pinned to dataset "
                        f"{pin.dataset_hash[:12]}…, not {dataset.provenance.dataset_hash[:12]}…."
                    ),
                    remedy=(
                        "Run `seqforge processing new` against this dataset, or drop the pin to make "
                        "it a portable template."
                    ),
                    subject=BlockerSubject(kind="field", ref="dataset.dataset_hash"),
                )
            )
        organism = dataset.experiment.organism.value
        if genome.ncbi_taxid is not None and genome.ncbi_taxid != organism:
            blockers.append(
                Blocker(
                    id="blk-genome-organism",
                    code=BlockerCode.GENOME_ORGANISM_MISMATCH,
                    message=(
                        f"processing selects assembly {genome.assembly!r} (taxid "
                        f"{genome.ncbi_taxid}), but the dataset's organism is taxid {organism}."
                    ),
                    remedy=(
                        f"Pick an assembly for taxid {organism}, or correct experiment.organism. A "
                        "wrong-but-valid assembly aligns and exits 0 — nothing downstream catches it."
                    ),
                    subject=BlockerSubject(kind="field", ref="processing.genome.assembly"),
                )
            )

    return ValidationReport(
        ok=not blockers and not open_conflicts,
        blockers=blockers,
        conflicts=open_conflicts,
        warnings=[],
    )


def exit_code_for_report(report: ValidationReport) -> int:
    """Uniform contract: 3 BLOCKED (a hard Blocker), 4 NEEDS_HUMAN (an open Conflict), else 0."""
    if report.blockers:
        return 3
    if report.conflicts:
        return 4
    return 0
