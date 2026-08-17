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

from ..models.blocker import (
    MISSING_TECHNICAL_READ_REMEDY,
    Blocker,
    BlockerCode,
    BlockerSubject,
    ValidationWarning,
)
from ..models.conflict import Conflict
from ..models.dataset import INDEX_ROLE, DatasetManifest, FileInventoryItem
from ..models.processing import ProcessingManifest
from ..models.resolve import ValidationReport
from ..resolve.engine import LANE_LEN_TOL, read_designation

# Confidence below which a decided chemistry is flagged (non-blocking) as a close call. Rung-aware: a
# rung-3 winner had an onlist positively participate (the barcode read matched a whitelist), so it is
# trusted at a lower score than a rung-2 geometry-only winner, whose seat rests on read geometry
# alone. Calibrated conservatively — a clean chemistry scores ~1.0, well clear of these
# floors, while the compile audit's two broken winners scored 0.44 / 0.59; those are now caught
# upstream by the BARCODE_READ_ABSENT blocker, so this warning covers the *residual* lonely-low winner
# that composes silently. A warning, never a gate: many legitimately-moderate datasets score in this
# band and refusing them would be worse than composing them (see the emit site in `validate_manifest`).
_CHEM_CONF_FLOOR_ONLIST = 0.55
_CHEM_CONF_FLOOR_GEOMETRY = 0.65


#: The remedy for a roleless file that is nobody's sibling — one carrying no read designation, or one
#: whose designation no layout role's representative shares. That file really may be a mis-grouped set
#: of runs or a stray from another dataset, which is what this text has always said.
_UNASSIGNED_REMEDY = (
    "Usually this means the files were resolved as one library when they are several runs: use "
    "`seqforge manifest fill` on the whole set (it groups by run and assigns roles per run), or drop "
    "the file if it does not belong to this dataset."
)


def _lane_surplus_remedy(
    roleless: FileInventoryItem, sibling: FileInventoryItem, designation: str
) -> str:
    """The remedy for the one roleless shape ADR-0027 created: an unseated lane of a fused run.

    A run is lane-blind, so a four-lane library is **one** run of eight files. The injective
    assignment fills each role once and ``resolve.engine.index_tagged_roles`` re-seats the surplus
    only within :data:`~seqforge.resolve.engine.LANE_LEN_TOL` of its role's modal read length; a lane
    that drifts further gets no role and lands on this blocker. Every clause of
    :data:`_UNASSIGNED_REMEDY` is wrong for it — the files ARE one run, deliberately; ``manifest
    fill`` is the thing that just produced the refusal; and dropping the lane is exactly the
    partial-depth loss ADR-0027 exists to refuse, so the old text recommended the destructive action.

    The branch keys on the read designation because it is the only signal available here: a
    :class:`~seqforge.models.dataset.FileInventoryItem` carries no read length and ``validate`` is
    handed no ``Observation``. Nothing about the refusal changes — only what it tells the user to do.
    """
    return (
        f"{sibling.basename} carries the same read designation ({designation}) and is seated as "
        f"{sibling.read_id!r}: one run spans its lanes (ADR-0027), so this is normally that read's "
        "lane/flowcell sibling, and its reads are this library's depth. Re-running `seqforge manifest "
        "fill` is what produced this refusal and dropping the file loses that depth, so neither is "
        f"the fix. A sibling joins a role only within {LANE_LEN_TOL} bp of that role's modal read "
        f"length: compare them with `seqforge probe {sibling.basename} {roleless.basename}`. Re-fetch "
        "a lane truncated in transfer or trimmed on its own; a delivery legitimately trimmed per lane "
        "needs a wider tolerance in the resolver, which is an issue to open rather than a manifest to "
        "edit."
    )


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
                    # A validator holds a manifest and nothing else — no accession, no record set —
                    # so the remedy names the verb that does hold one rather than the URI it would
                    # print. That is the pointer ADR-0033 asks for, and it is also all this
                    # function could honestly carry: a manifest names no host and no absolute path,
                    # so there is no local answer here to bake in even if we wanted one.
                    remedy=MISSING_TECHNICAL_READ_REMEDY,
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
    #
    # The refusal is one; the way out of it is two, because ADR-0027 made a run lane-blind and so
    # created a roleless file that is nobody's mistake — see `_lane_surplus_remedy`. Which one a file
    # gets turns on whether a LAYOUT role's representative shares its read designation.
    #
    # INDEX_ROLE is excluded, and that is the branch's whole honesty. `index_tagged_roles` builds its
    # representatives from `role_assignment.assignment` alone, so an index-tagged file is not one a
    # surplus lane was ever compared against; and a roleless file designated `I1` is by construction
    # LONGER than the length gate (<= 20 bp) that would have tagged it — a cDNA-length stray, which is
    # exactly the shape the old text was written for. Admitting index files here would hand the lane
    # remedy to that stray, and tell it its reads are depth STARsolo never reads.
    representatives: dict[str, FileInventoryItem] = {}
    for f in manifest.library.files:
        if f.read_id is None or f.read_id == INDEX_ROLE:
            continue
        designation = read_designation(f.basename)
        if designation is not None:
            representatives.setdefault(designation, f)  # they are lanes of one read; any names it

    for f in manifest.library.files:
        if f.read_id is None:
            designation = read_designation(f.basename)
            sibling = representatives.get(designation) if designation is not None else None
            blockers.append(
                Blocker(
                    id=f"blk-unassigned-{f.sha256[:8]}",
                    code=BlockerCode.NO_VALID_ROLE_ASSIGNMENT,
                    message=(
                        f"{f.basename} was given no read role, so the pipeline would not read it. "
                        f"Its reads would be dropped, and nothing downstream would say so."
                    ),
                    remedy=(
                        _lane_surplus_remedy(f, sibling, designation)
                        if designation is not None and sibling is not None
                        else _UNASSIGNED_REMEDY
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

    # --- a lonely low-confidence chemistry composes exactly like a certain one; flag the close call ---
    #
    # Scoring's only magnitude comparison is the *relative* tie band (`_THETA` in escalate); nothing
    # anywhere gates on an *absolute* floor, so a byte-marginal winner reaches compose at exit 0 with
    # no signal the call was close. This surfaces that as a non-blocking note. It stays a WARNING, not
    # a Blocker/Question, on purpose: the danger cases (a barcode read that hits nothing, a
    # single-cell library collapsed to bulk) are already refused upstream by their own Blockers, so
    # what is left in this band is mostly *legitimately* moderate — refusing it would trade a rare
    # silent-wrong for a common wrong-refusal. The manifest stays compilable; the note rides along.
    chem = manifest.library.chemistry
    if chem.confidence is not None:
        floor = _CHEM_CONF_FLOOR_ONLIST if chem.rung >= 3 else _CHEM_CONF_FLOOR_GEOMETRY
        if chem.confidence < floor:
            warnings.append(
                ValidationWarning(
                    code="LOW_CONFIDENCE_CHEMISTRY",
                    message=(
                        f"chemistry {chem.value} was decided at confidence {chem.confidence:.2f} "
                        f"(rung {chem.rung}, floor {floor:.2f}) — a close call for the byte evidence. "
                        "It composes normally, but is weaker than a typical decision; review the read "
                        "geometry / whitelist match before submitting."
                    ),
                    subject=BlockerSubject(kind="field", ref="library.chemistry"),
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

    **The two sides must be independently sourced or there is nothing to compare.** The genome's
    ``ncbi_taxid`` is the *assembly's*, read off ``liulab-genome``'s shipped cross-reference by the
    processing policy; ``experiment.organism`` is the *dataset's*, asserted. It used to be a copy of
    that same organism, which made this check a tautology that could fire only for a hand-edited
    ``processing.yaml`` — never for a recipe seqforge wrote, which is the case it exists for.

    Deliberately narrow: it fires only when the recipe carries an ``ncbi_taxid`` for the genome. A
    full assembly->taxid table belongs in ``liulab-genome``, and consuming that one is what the
    ``is not None`` guard now means — an assembly the table does not list, and a Chimera, whose row
    carries no single taxid because it is more than one organism. Both stay silent here rather than
    growing a refusal of their own.

    A correct recipe can still block when the organism came from prose: the organism-name->taxid seed
    resolves *Saccharomyces cerevisiae* to the species ``4932`` while the assembly table carries the
    S288C strain ``559292``, and the same split exists for *E. coli* HT115. That is accepted rather
    than papered over with lineage or rank normalization — a loud refusal naming both exits beats a
    silent matrix in the wrong coordinate space, which is the whole reason for this check.
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
                        f"Pick an assembly for taxid {organism}, or correct the dataset's organism "
                        "(`seqforge manifest fill --organism <taxid>` overrides what the record "
                        "said). The second exit is the one to take when both names are the same "
                        "organism at different ranks — species against strain. A wrong-but-valid "
                        "assembly aligns and exits 0 — nothing downstream catches it."
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
