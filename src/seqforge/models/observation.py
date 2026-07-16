"""``Observation`` — deterministic, LLM-free, network-free probe output for ONE file.

Cached by file sha256. Reports structural signals ONLY; it MUST NOT assign roles — mapping
``constant -> linker/TSO``, ``random -> CB|UMI|cDNA``, ``homopolymer-T -> polyT`` is the resolver's
job, scored and second-guessable.
"""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field

from .base import Confidence, LocalPath, Sha256


class CycleComposition(BaseModel):
    """Base fractions at one 0-based cycle; ``a + c + g + t + n ~= 1.0``."""

    cycle: int = Field(ge=0)
    a: float
    c: float
    g: float
    t: float
    n: float


class ConstantSegment(BaseModel):
    """A cycle span where one base dominates (>~90%): a linker/adapter/TSO candidate.

    Structural only — the role is NOT assigned here.
    """

    kind: Literal["constant"] = "constant"
    start: int = Field(ge=0)
    end: int = Field(ge=0)
    consensus: str
    purity: Confidence


class RandomSegment(BaseModel):
    """A near-uniform ACGT span: a CB/UMI/cDNA candidate (role NOT assigned)."""

    kind: Literal["random"] = "random"
    start: int = Field(ge=0)
    end: int = Field(ge=0)
    mean_entropy_bits: float


class HomopolymerSegment(BaseModel):
    """A run of one base (polyT capture / polyA tail): structural only."""

    kind: Literal["homopolymer"] = "homopolymer"
    base: Literal["A", "C", "G", "T"]
    start: int = Field(ge=0)
    end: int = Field(ge=0)
    mean_run: float


Segment = Annotated[
    ConstantSegment | RandomSegment | HomopolymerSegment,
    Field(discriminator="kind"),
]


class FileIdentity(BaseModel):
    """Content identity of one FASTQ. Observation is internal, so a LOCAL path is allowed here only."""

    sha256: Sha256
    size_bytes: int = Field(gt=0)
    basename: str
    local_uri: LocalPath | None = None


class ProbeProvenance(BaseModel):
    """What the bounded probe did under its read/byte budget.

    ``bytes_read`` is decompressed; ``compressed_bytes_read`` drives ``estimated_total_reads``
    (avoids the compression-ratio undercount).
    """

    n_reads_sampled: int = Field(ge=0)
    bytes_read: int = Field(ge=0)
    compressed_bytes_read: int = Field(ge=0)
    tool_version: str
    params_hash: str


class ReadLengthProfile(BaseModel):
    """Read-length summary. ``n_distinct > 1`` on a fixed-geometry read -> PRETRIMMED_VARIABLE_LENGTH."""

    mode: int = Field(ge=0)
    n_distinct: int = Field(ge=1)
    min_len: int = Field(ge=0)
    max_len: int = Field(ge=0)
    percentiles: dict[str, int] | None = None


class WindowDistinctRatio(BaseModel):
    """``distinct/total`` over a candidate cycle window.

    DEPTH-DEPENDENT: a supports signal only, never a gate. Normalize with ``4^len`` and sampled-N
    before interpreting (see the scorer).
    """

    start: int = Field(ge=0)
    end: int = Field(ge=0)
    distinct_ratio: Confidence
    n_sampled: int = Field(ge=1)


class ReadNameGrammar(BaseModel):
    """Parsed Illumina header; all optional. ``sra_normalized`` flags an ``@SRR....N`` rewrite."""

    parsed: bool = False
    instrument: str | None = None
    run: str | None = None
    flowcell: str | None = None
    lane: int | None = None
    tile: int | None = None
    index: str | None = None
    sra_normalized: bool = False


class GzipIntegrity(BaseModel):
    """Gzip stream integrity. ``truncated`` -> TRUNCATED_GZIP Blocker downstream."""

    ok: bool
    truncated: bool
    bgzf: bool | None = None
    member_count: int | None = None


class Observation(BaseModel):
    """Structural, role-free probe output for one file, cached by ``file.sha256``."""

    model_config = ConfigDict(frozen=True)

    file: FileIdentity
    probe: ProbeProvenance
    per_cycle_composition: list[CycleComposition]
    segments: list[Segment]
    read_length: ReadLengthProfile
    distinct_value_windows: list[WindowDistinctRatio]
    read_name: ReadNameGrammar
    quality_encoding: Literal["phred33", "phred64", "unknown"]
    n_rate: Confidence
    estimated_total_reads: int = Field(ge=0)
    est_method: Literal["isize", "compressed_ratio"]
    gzip: GzipIntegrity
