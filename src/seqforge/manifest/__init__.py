"""``manifest`` — assemble, validate, and hash the two artifacts (R13).

Operations live here; the *schemas* are :mod:`seqforge.models.dataset` (the IR: what the data is) and
:mod:`seqforge.models.processing` (the flags: what to do with it). ``fill`` assembles a Decision into
a dataset manifest — each section keeping its own authority — and ``fill_processing`` builds one of
the many processing manifests a dataset may be paired with. ``validate`` is the R4 refusal contract
(structured ``Blocker``s + a nonzero exit), and ``hash`` gives each artifact a content-addressed
identity plus the ``run_id`` that records their pairing (R7).
"""

from __future__ import annotations

from .fill import (
    ExperimentInputs,
    FillError,
    ProcessingInputs,
    dataset_uris,
    experiment_from_metadata,
    fill_manifest,
    fill_processing,
)
from .hash import dataset_content_hash, processing_content_hash, run_id
from .instruct import INSTRUCTABLE_FIELDS, Instruction, instructions_from_assertions
from .policy import (
    DEFAULT_SOLO_FEATURES,
    PolicyError,
    ProcessingDefaults,
    ProcessingOverrides,
    processing_defaults,
    resolve_features,
    resolve_processing,
)
from .validate import exit_code_for_report, validate_manifest, validate_processing

__all__ = [
    "dataset_uris",
    "fill_manifest",
    "fill_processing",
    "ExperimentInputs",
    "experiment_from_metadata",
    "ProcessingInputs",
    "FillError",
    "validate_manifest",
    "validate_processing",
    "exit_code_for_report",
    "dataset_content_hash",
    "processing_content_hash",
    "run_id",
    "processing_defaults",
    "ProcessingDefaults",
    "ProcessingOverrides",
    "resolve_processing",
    "resolve_features",
    "DEFAULT_SOLO_FEATURES",
    "PolicyError",
    "Instruction",
    "INSTRUCTABLE_FIELDS",
    "instructions_from_assertions",
]
