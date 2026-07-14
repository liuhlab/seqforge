"""Pydantic v2 models — the single source of truth (R1/R2).

``Manifest.model_json_schema()`` feeds validation and docs; the only LLM-facing schemas are
:class:`AssertionDraft` and the arbitration pair. :func:`export_schema` dumps any model's JSON Schema
(the ``seqforge schema export`` backend).
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
from .manifest import (
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
    ResourceHints,
    RuntimeEnv,
    SampleGroup,
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
        # compile / manifest
        Manifest,
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
    # manifest
    "Manifest",
    "LibrarySection",
    "ExperimentSection",
    "ProcessingSection",
    "Provenance",
    "ReadLayout",
    "ReadDef",
    "ReadElement",
    "Onlist",
    "GenomeRef",
    "RuntimeEnv",
    "ResourceHints",
    "FileInventoryItem",
    "SampleGroup",
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
