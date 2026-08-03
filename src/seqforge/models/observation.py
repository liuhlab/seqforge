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
    """Base fractions at one 0-based cycle; ``a + c + g + t + n ~= 1.0``.

    ``n_sampled`` is the denominator those fractions were divided by: the sampled reads long enough
    to reach this cycle. It is the primitive every coverage figure here reduces — a segment's
    ``coverage`` and the head's :class:`HeadCoverage` are both sums over it — and it is also the
    denominator of a *window* statistic cut from the same head, because a read too short to span a
    column is dropped before ``consensus_match_rate`` or ``distinct_ratio`` ever sees it. Without it
    a cycle measured over three reads and one measured over two thousand printed identically.
    """

    cycle: int = Field(ge=0)
    a: float
    c: float
    g: float
    t: float
    n: float
    n_sampled: int = Field(ge=0)


class ConstantSegment(BaseModel):
    """A cycle span where one base dominates (>~90%): a linker/adapter/TSO candidate.

    Structural only — the role is NOT assigned here.

    ``purity`` and ``coverage`` are different questions and an uncalled cycle moves both: purity is
    how constant the span *looked*, coverage is how much of the sampled material was there to look
    at. Purity alone cannot tell them apart, because an uncalled base sits in its denominator and so
    reads as "not constant" — which is how a dark cycle turns a linker into a random span. See
    :class:`HeadCoverage`; nothing gates on ``coverage``.
    """

    kind: Literal["constant"] = "constant"
    start: int = Field(ge=0)
    end: int = Field(ge=0)
    consensus: str
    purity: Confidence
    coverage: Confidence


class RandomSegment(BaseModel):
    """A near-uniform ACGT span: a CB/UMI/cDNA candidate (role NOT assigned).

    ``coverage`` is the share of the head's ``(read, cycle)`` cells in this span that carried a
    called base — what this classification was decided over. See :class:`HeadCoverage`; nothing
    gates on it.
    """

    kind: Literal["random"] = "random"
    start: int = Field(ge=0)
    end: int = Field(ge=0)
    mean_entropy_bits: float
    coverage: Confidence


class HomopolymerSegment(BaseModel):
    """A run of one base (polyT capture / polyA tail): structural only.

    ``coverage`` is the share of the head's ``(read, cycle)`` cells in this span that carried a
    called base — what this classification was decided over. See :class:`HeadCoverage`; nothing
    gates on it.
    """

    kind: Literal["homopolymer"] = "homopolymer"
    base: Literal["A", "C", "G", "T"]
    start: int = Field(ge=0)
    end: int = Field(ge=0)
    mean_run: float
    coverage: Confidence


Segment = Annotated[
    ConstantSegment | RandomSegment | HomopolymerSegment,
    Field(discriminator="kind"),
]


class FileIdentity(BaseModel):
    """Content identity of one FASTQ. Observation is internal, so a LOCAL path is allowed here only.

    ``sha256`` is a **content-address**, not a whole-file hash. Locally it is a bounded key over the
    basename + head sample + size + gzip ISIZE (``probe.core._content_key``, issue #37). For hosted
    bytes it is derived from the provider md5 (``probe.core.content_key_from_md5``, issue #39) — a
    64-hex name that is a pure function of the md5, so two hosted files with the same md5 dedup. Either
    way fingerprinting never reads a whole FASTQ; a remote probe range-reads only a bounded head.
    """

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


class HeadCoverage(BaseModel):
    """How much of the sampled head actually fed the statistics computed from it.

    A head slice is not a random sample, so a statistic taken off one is worth only what it was
    taken *over*. The defence already exists one module away: ``io.onlist.onlist_hit_rate`` counts
    only the reads that could have hit, so an uncalled cycle costs it coverage rather than moving
    its rate. The other head-derived statistics — per-cycle composition, the segmentation built on
    it, and the windows ``consensus_match_rate`` is cut from — had no such figure. This is it.

    **Two loss channels, reported apart because they mean different things.** A *cell* is one
    ``(read, cycle)`` pair, and the head could have held ``probe.n_reads_sampled`` x the longest
    read of them. ``reach_fraction`` is the share a read was long enough to occupy: low on a trimmed
    or ragged file, and a fact about read lengths rather than about the run. ``called_fraction`` is
    the share of the *occupied* cells whose base was called — what a dark cycle destroys, and
    exactly the complement of the head-wide N rate that used to sit on the observation unread. Their
    product is the overall coverage; one number could not tell a short read from an uncalled base,
    and only the second says anything is wrong.

    **Nothing reads either to decide anything** — no Blocker, no Conflict, no score. They are
    recorded so the distribution across the corpus can be *seen* before any threshold is proposed,
    because making a poor number refuse is a separate decision with refusal consequences.

    What share of the FILE the head is, is deliberately not repeated here: it is
    ``probe.n_reads_sampled / estimated_total_reads``, already on the observation and already
    qualified by ``est_method`` (an exact count when the file was read to EOF, an ISIZE
    extrapolation, or a compressed-size ratio when — as on a remote range read — there is no tail to
    reach). A stored quotient of two neighbouring fields could only ever disagree with them.

    An empty head covers nothing, and both fields read ``0.0`` rather than a vacuous ``1.0``.
    """

    reach_fraction: Confidence
    called_fraction: Confidence


class GzipIntegrity(BaseModel):
    """Gzip stream integrity — **two** verdicts, never both true, each with its own Blocker.

    ``truncated`` means the bytes ran out mid-member (a cut upload, or the bounded range-read head a
    remote probe takes by design) -> ``TRUNCATED_GZIP``, remedied by re-downloading. ``ok=False``
    means the stream is not readable gzip at all — a header that does not parse, a corrupt member, a
    CRC that disagrees with what came out -> ``CORRUPT_FASTQ``, remedied by asking whether it was ever
    a FASTQ. Which applies is decided by what the decompressor raised, not by a record count
    (``probe.streaming.BoundedReader``); the two collapsed into ``truncated`` until issue #94, leaving
    ``ok`` unreachable and both remedies spelled as one.

    A well-formed slice cannot recompute this about the file it stands in for, which is why a
    fingerprint pins it (``models.fingerprint.FilePin.gzip``) rather than re-observing it.
    """

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
    coverage: HeadCoverage
    estimated_total_reads: int = Field(ge=0)
    est_method: Literal["isize", "compressed_ratio"]
    gzip: GzipIntegrity
