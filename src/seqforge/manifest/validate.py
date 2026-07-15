"""``manifest validate`` — refusal is an exit code, not a vibe (R4).

Returns a structured :class:`ValidationReport`; the CLI maps it to the uniform exit contract
(``0`` OK, ``3`` BLOCKED, ``4`` NEEDS_HUMAN). ``manifest.yaml`` is written **only** after a clean
validate (R7).

Type-level guards (the ``Uri`` validator's absolute-path rejection, the ``AssayTerm`` CURIE pattern,
the ``Sha256`` pattern) already fail at *construction*; this pass owns the checks Pydantic cannot do
locally — **referential integrity across sections**, controlled-vocabulary presence, and
role/layout/onlist coherence. The absolute-path sweep is kept as defence in depth: R9 is the rule most
expensive to get wrong, so it is enforced twice.
"""

from __future__ import annotations

from ..models.blocker import Blocker, BlockerCode, BlockerSubject, ValidationWarning
from ..models.conflict import Conflict
from ..models.manifest import Manifest
from ..models.resolve import ValidationReport


def _looks_absolute(uri: str) -> bool:
    return (
        uri.startswith(("/", "~"))
        or uri.startswith("file:///")
        or (len(uri) > 1 and uri[1] == ":")
        or uri.startswith("\\\\")
    )


def validate_manifest(
    manifest: Manifest, *, conflicts: list[Conflict] | None = None
) -> ValidationReport:
    """Validate a manifest's cross-section integrity. Any Blocker => not compilable."""
    blockers: list[Blocker] = []
    warnings: list[ValidationWarning] = []
    open_conflicts = [c for c in (conflicts or []) if c.status == "open"]

    # --- R9: no absolute/local path may ever reach a manifest (defence in depth) ---
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
    layout_roles = {r.read_id for r in manifest.library.read_layout.value.reads}
    for f in manifest.library.files:
        if f.read_id is not None and f.read_id.value not in layout_roles:
            blockers.append(
                Blocker(
                    id=f"blk-role-{f.sha256[:8]}",
                    code=BlockerCode.NO_VALID_ROLE_ASSIGNMENT,
                    message=(
                        f"{f.basename} is assigned role {f.read_id.value!r}, which is not a read in "
                        f"the declared layout ({sorted(layout_roles)})."
                    ),
                    remedy="Re-run `seqforge resolve score`; the role assignment must match the layout.",
                    subject=BlockerSubject(kind="file", ref=f.basename),
                )
            )
    for role in sorted(layout_roles):
        if not any(
            f.read_id is not None and f.read_id.value == role for f in manifest.library.files
        ):
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

    # --- onlists: a barcode element naming an unmaterialized whitelist is advisory, not fatal ---
    onlist_names = {o.name for o in manifest.library.onlists}
    for read in manifest.library.read_layout.value.reads:
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


def exit_code_for_report(report: ValidationReport) -> int:
    """Uniform contract: 3 BLOCKED (a hard Blocker), 4 NEEDS_HUMAN (an open Conflict), else 0."""
    if report.blockers:
        return 3
    if report.conflicts:
        return 4
    return 0
