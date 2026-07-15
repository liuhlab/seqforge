"""Pydantic v2 models — the single source of truth (R1/R2).

The manifest is **two** artifacts with two lifetimes (R13): :class:`DatasetManifest` is what the data
*is* (immutable, one per dataset), :class:`ProcessingManifest` is what to *do* with it (many per
dataset). Their JSON Schemas feed validation and docs; the only LLM-facing schemas are
:class:`AssertionDraft` and the arbitration pair — the processing manifest is deliberately **not**
among them. The LLM emits ``AssertionDraft``; code composes the processing manifest from verified
assertions plus policy. That boundary is what keeps R1 alive.

:func:`export_schema` dumps any model's JSON Schema (the ``seqforge schema export`` backend).
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel

from .assertion import (
    Assertion,
    AssertionDraft,
    ExtractorProvenance,
    SourceSpan,
)
from .base import (
    Accession,
    AssayTerm,
    Basis,
    ChemistryId,
    Confidence,
    Evidenced,
    LocalPath,
    NcbiTaxid,
    Rung,
    Sha256,
    Uri,
)
from .blocker import (
    Blocker,
    BlockerCode,
    BlockerSubject,
    ValidationWarning,
)
from .conflict import (
    Conflict,
    ConflictPosition,
    Decidable,
    Resolution,
)
from .dataset import (
    DatasetManifest,
    DatasetProvenance,
    EvidencedReadLayout,
    ExperimentSection,
    FileInventoryItem,
    LibrarySection,
    Onlist,
    ReadDef,
    ReadElement,
    ReadLayout,
    SampleGroup,
)
from .evidenced import (
    EvidencedAccessionList,
    EvidencedAssay,
    EvidencedBool,
    EvidencedChemistrySet,
    EvidencedStr,
    EvidencedTaxid,
)
from .observation import (
    ConstantSegment,
    CycleComposition,
    FileIdentity,
    GzipIntegrity,
    HomopolymerSegment,
    Observation,
    ProbeProvenance,
    RandomSegment,
    ReadLengthProfile,
    ReadNameGrammar,
    WindowDistinctRatio,
)
from .processing import (
    BulkQuant,
    DatasetPin,
    EvidencedGenome,
    EvidencedQuantification,
    EvidencedRuntimeEnv,
    GenomeRef,
    ProcessingManifest,
    ProcessingProvenance,
    ProcessingSection,
    Quantification,
    ResourceHints,
    RuntimeEnv,
    SoloFeature,
    SoloQuant,
)
from .resolve import (
    ArbitrationRequest,
    ArbitrationResponse,
    Candidate,
    ComposeResult,
    Decision,
    EvalReport,
    ModuleSelection,
    Question,
    ResolveResult,
    RoleAssignment,
    RunResult,
    TechScore,
    ValidationReport,
)

#: Every top-level model that ``seqforge schema export`` can dump, keyed by class name.
SCHEMA_MODELS: dict[str, type[BaseModel]] = {
    m.__name__: m
    for m in (
        # observation
        Observation,
        # harvest
        AssertionDraft,
        Assertion,
        # resolve / conflict / blocker
        Conflict,
        Blocker,
        ValidationWarning,
        Candidate,
        Question,
        Decision,
        ResolveResult,
        ArbitrationRequest,
        ArbitrationResponse,
        ValidationReport,
        # compile / manifest — TWO artifacts, two lifetimes (R13)
        DatasetManifest,
        ProcessingManifest,
        ComposeResult,
        RunResult,
        EvalReport,
    )
}

#: Models the LLM produces or consumes — their exported schema must stay inside the provider subset.
LLM_FACING: frozenset[str] = frozenset(
    {"AssertionDraft", "ArbitrationRequest", "ArbitrationResponse"}
)


def export_schema(model_name: str) -> dict[str, Any]:
    """Return the JSON Schema for one model by class name.

    Raises
    ------
    KeyError
        If ``model_name`` is not a known exportable model.
    """
    try:
        model = SCHEMA_MODELS[model_name]
    except KeyError as exc:
        known = ", ".join(sorted(SCHEMA_MODELS))
        raise KeyError(f"unknown model {model_name!r}; known models: {known}") from exc
    return model.model_json_schema(ref_template="#/$defs/{model}")


def export_all() -> dict[str, dict[str, Any]]:
    """Return the JSON Schema for every exportable model, keyed by class name."""
    return {name: export_schema(name) for name in SCHEMA_MODELS}


__all__ = [
    # export machinery
    "SCHEMA_MODELS",
    "LLM_FACING",
    "export_schema",
    "export_all",
    # base
    "Evidenced",
    "EvidencedStr",
    "EvidencedBool",
    "EvidencedTaxid",
    "EvidencedAssay",
    "EvidencedChemistrySet",
    "EvidencedAccessionList",
    "EvidencedReadLayout",
    "EvidencedGenome",
    "EvidencedRuntimeEnv",
    "EvidencedQuantification",
    "Basis",
    "Sha256",
    "Uri",
    "LocalPath",
    "AssayTerm",
    "NcbiTaxid",
    "Accession",
    "ChemistryId",
    "Confidence",
    "Rung",
    # observation
    "Observation",
    "FileIdentity",
    "ProbeProvenance",
    "CycleComposition",
    "ConstantSegment",
    "RandomSegment",
    "HomopolymerSegment",
    "ReadLengthProfile",
    "WindowDistinctRatio",
    "ReadNameGrammar",
    "GzipIntegrity",
    # assertion
    "SourceSpan",
    "AssertionDraft",
    "Assertion",
    "ExtractorProvenance",
    # conflict
    "Conflict",
    "ConflictPosition",
    "Resolution",
    "Decidable",
    # blocker
    "Blocker",
    "BlockerCode",
    "BlockerSubject",
    "ValidationWarning",
    # dataset manifest — the IR: what the data IS (immutable, one per dataset)
    "DatasetManifest",
    "DatasetProvenance",
    "LibrarySection",
    "ExperimentSection",
    "ReadLayout",
    "ReadDef",
    "ReadElement",
    "Onlist",
    "FileInventoryItem",
    "SampleGroup",
    # processing manifest — the flags: what to DO with it (many per dataset)
    "ProcessingManifest",
    "ProcessingProvenance",
    "ProcessingSection",
    "DatasetPin",
    "GenomeRef",
    "RuntimeEnv",
    "ResourceHints",
    "SoloFeature",
    "SoloQuant",
    "BulkQuant",
    "Quantification",
    # resolve outputs
    "TechScore",
    "RoleAssignment",
    "Candidate",
    "Question",
    "Decision",
    "ResolveResult",
    "ArbitrationRequest",
    "ArbitrationResponse",
    "ValidationReport",
    "ModuleSelection",
    "ComposeResult",
    "RunResult",
    "EvalReport",
]
