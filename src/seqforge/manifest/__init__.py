"""``manifest`` — assemble, validate, and hash the three-section :class:`Manifest`.

Operations on the manifest live here; the *schema* is :mod:`seqforge.models.manifest`. ``fill``
assembles a Decision into a manifest (each section keeping its own authority), ``validate`` is the R4
refusal contract (structured ``Blocker``s + a nonzero exit), and ``hash`` binds a compiled config to
the exact inputs and tool versions that produced it (R7).
"""

from __future__ import annotations

from .fill import (
    ExperimentInputs,
    FillError,
    ProcessingInputs,
    fill_manifest,
)
from .hash import manifest_content_hash, provenance_id
from .policy import ProcessingDefaults, processing_defaults
from .validate import exit_code_for_report, validate_manifest

__all__ = [
    "fill_manifest",
    "ExperimentInputs",
    "ProcessingInputs",
    "FillError",
    "validate_manifest",
    "exit_code_for_report",
    "manifest_content_hash",
    "provenance_id",
    "processing_defaults",
    "ProcessingDefaults",
]
