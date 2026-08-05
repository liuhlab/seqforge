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
    #: The winning barcoded chemistry's barcode role IS filled, but no seated read carries
    #: whitelist-matchable barcodes though the chemistry declares a whitelist — STARsolo would read
    #: barcodes from a read that matches nothing and report ~0 valid barcodes at exit 0. Distinct from
    #: MISSING_TECHNICAL_READ, where the role is structurally UNFILLABLE (no read of the right shape).
    BARCODE_READ_ABSENT = "BARCODE_READ_ABSENT"
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
    #: A staged *document* (a paper/README a human supplied) plainly describes a DIFFERENT study than
    #: the data: no identity signal (named study accession, organism, strain) matches the records, and
    #: the chemistry family it describes contradicts the observed reads. A wrong document silently
    #: steers harvest, so the strongest case refuses rather than bakes a foreign study's facts in.
    PROVENANCE_MISMATCH = "PROVENANCE_MISMATCH"
    #: An archive record was supplied and does not account for the files on disk. Only ever raised
    #: when a record EXISTS: a dataset with no accession has nothing to join and is not a refusal.
    #: Half-joining is the failure this exists to prevent — the files it could not place would get no
    #: sample facts, and a manifest that is confidently right about four samples and silent about two
    #: reads as a manifest about six.
    RECORD_JOIN_INCOMPLETE = "RECORD_JOIN_INCOMPLETE"
    #: One run reached the ceiling on the tokens it may spend at the model seam, so the request that
    #: would have followed was refused and the extraction is incomplete. A ceiling that only warned
    #: would be a number nobody sets: the run it exists to stop is exactly the run whose warning
    #: scrolls past at request 400 of 983.
    TOKEN_CEILING_EXCEEDED = "TOKEN_CEILING_EXCEEDED"


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


#: Where to get the submitter's own upload, in the one phrasing five refusals share. It names the
#: **verb** and never the URI: three of those refusals are raised inside the byte resolver, which may
#: not hold an archive record, so a remedy carrying the value would first have to be handed one
#: (``docs/adr/0033``). It lives here because ``_missing_technical_read`` said it first and said it
#: correctly — two copies of this sentence would be two remedies to keep true — and by the time the
#: fifth site was found there were four, one of them still naming a route that usually dead-ends.
SUBMITTED_FILES_REMEDY = (
    "`seqforge io records <accession>` lists what the deposit declares, each file with its "
    "`sra-pub-src-*` URI"
)

#: The whole remedy for ``MISSING_TECHNICAL_READ``, which the byte resolver and the manifest
#: validator both raise. One code reached from two places is one instruction, not two that happen to
#: agree today.
MISSING_TECHNICAL_READ_REMEDY = (
    "Re-fetch with `fasterq-dump --include-technical`, or go back for the submitter's own upload: "
    f"{SUBMITTED_FILES_REMEDY}. The SDL API reaches those same bytes by another route."
)
