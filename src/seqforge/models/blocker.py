"""``Blocker`` — refusal as an exit code.

A ``Blocker`` is ALWAYS fatal (its presence => nonzero exit). Advisory diagnostics are a separate
:class:`ValidationWarning` (renamed from the design's ``Warning`` to avoid shadowing the builtin), so
branching code never inspects a severity to decide whether something blocks.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, Field


class BlockerCode(StrEnum):
    """The stable contract the CLI/skill branch on."""

    MISSING_TECHNICAL_READ = "MISSING_TECHNICAL_READ"
    TRUNCATED_GZIP = "TRUNCATED_GZIP"
    CORRUPT_FASTQ = "CORRUPT_FASTQ"
    UNSUPPORTED_TECHNOLOGY = "UNSUPPORTED_TECHNOLOGY"
    PRETRIMMED_VARIABLE_LENGTH = "PRETRIMMED_VARIABLE_LENGTH"
    NO_VALID_ROLE_ASSIGNMENT = "NO_VALID_ROLE_ASSIGNMENT"
    ONLIST_VERIFICATION_FAILED = "ONLIST_VERIFICATION_FAILED"
    UNRESOLVED_CONFLICT = "UNRESOLVED_CONFLICT"
    MISSING_CONTROLLED_VOCAB = "MISSING_CONTROLLED_VOCAB"
    ABSOLUTE_PATH = "ABSOLUTE_PATH"
    #: A processing manifest's genome does not belong to the dataset's organism. A wrong-but-VALID
    #: assembly is the most catastrophic silent failure available here: it aligns, exits 0, and emits
    #: a plausible matrix in the wrong coordinate space. Nothing downstream would ever notice.
    GENOME_ORGANISM_MISMATCH = "GENOME_ORGANISM_MISMATCH"
    #: A processing manifest bound to a different dataset than the one being compiled.
    DATASET_PIN_MISMATCH = "DATASET_PIN_MISMATCH"
    #: An archive record was supplied and does not account for the files on disk. Only ever raised
    #: when a record EXISTS: a dataset with no accession has nothing to join and is not a refusal.
    #: Half-joining is the failure this exists to prevent — the files it could not place would get no
    #: sample facts, and a manifest that is confidently right about four samples and silent about two
    #: reads as a manifest about six.
    RECORD_JOIN_INCOMPLETE = "RECORD_JOIN_INCOMPLETE"


class BlockerSubject(BaseModel):
    """What the refusal is about. ``ref`` is a basename / dotted path / dataset id — never a path."""

    kind: Literal["file", "field", "dataset"]
    ref: str


class Blocker(BaseModel):
    """A structured refusal emitted alongside a nonzero exit. ``remedy`` MUST be actionable."""

    id: str
    code: BlockerCode
    message: str
    remedy: str
    subject: BlockerSubject
    evidence: list[str] = Field(default_factory=list)


class ValidationWarning(BaseModel):
    """A non-blocking advisory note (exits 0). Kept distinct from :class:`Blocker`, which is fatal."""

    code: str
    message: str
    subject: BlockerSubject
